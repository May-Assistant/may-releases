"""The macOS native compile's unprivileged user (mac_sandbox.py, compile.py --sandbox; #65)."""

import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import compile as compile_tool
import mac_sandbox
from mac_sandbox import ROOT, SandboxError, USER
import static_check


class Admin:
    """admin's stand-in: each root command recorded, its status from fail (argv[0] or a stage's command → status)."""

    def __init__(self, fail=None, pgrep=1):
        self.calls, self.fail, self.pgrep = [], fail or {}, pgrep

    def __call__(self, argv, log, data=None):
        self.calls.append(argv)
        if argv[0] == "pgrep":
            return self.pgrep
        return self.fail.get(tuple(argv[:3]), self.fail.get(argv[0], 0))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "log"
        self.copies = {"src": Path("/runner/may-source"), "xcodegen": Path("/runner/may-private/xcodegen")}
        self.said = []

    def tearDown(self):
        self.temp.cleanup()

    def setup(self, admin, checks=lambda argv, log: 0, ids=lambda: False, project=None):
        def said(argv, log, data=None):
            status = admin(argv, log, data)
            return status, None if status == 0 else "did not start" if status is None else "no such file"

        with mock.patch("mac_sandbox.admin", admin), mock.patch("mac_sandbox.admin_said", side_effect=said), \
                mock.patch("mac_sandbox.as_sandbox", side_effect=checks) as sandbox:
            try:
                mac_sandbox.setup(self.log, self.copies, Path("/Users/runner"), Path("/runner"), project, ids=ids,
                                  say=lambda stage, status, cause=None: self.said.append((stage, status, cause)))
                return None, sandbox.call_args_list
            except SandboxError as error:
                return error, sandbox.call_args_list

    def test_a_hidden_user_and_group_of_its_own_and_no_other_group(self):
        commands = mac_sandbox.setup_commands(self.copies)
        user, group = f"/Users/{USER}", f"/Groups/{USER}"
        self.assertIn(["dscl", ".", "-create", group, "PrimaryGroupID", "497"], commands)
        self.assertIn(["dscl", ".", "-create", user, "UniqueID", "497"], commands)
        self.assertIn(["dscl", ".", "-create", user, "PrimaryGroupID", "497"], commands)
        self.assertIn(["dscl", ".", "-create", user, "UserShell", "/usr/bin/false"], commands)
        self.assertIn(["dscl", ".", "-create", user, "IsHidden", "1"], commands)
        flat = [part for argv in commands for part in argv]
        for membership in ("GroupMembership", "-append", "-merge", "admin", "staff"):
            self.assertNotIn(membership, flat)
        self.assertEqual(commands[0], ["rm", "-rf", "--", str(ROOT)])

    def test_the_checkout_is_its_own_and_the_generator_is_readable_but_roots(self):
        commands = mac_sandbox.setup_commands(self.copies)
        # Run 41170609342: macOS's getopt stops at the first operand, so "--" comes before the owner or mode.
        self.assertIn(["chown", "-R", "--", f"{USER}:{USER}", str(ROOT / "src")], commands)
        self.assertIn(["chown", "-R", "--", "root:wheel", str(ROOT / "xcodegen")], commands)
        self.assertIn(["chmod", "-R", "--", "u=rwX,go=rX", str(ROOT / "xcodegen")], commands)
        self.assertNotIn(["chmod", "-R", "--", "u=rwX,go=rX", str(ROOT / "src")], commands)
        for argv in commands:
            if argv[0] in ("chmod", "chown") and "--" in argv:
                self.assertEqual(argv.index("--"), len(argv) - 3, "the owner or mode follows --, never the other way")
        self.assertIn(["install", "-d", "-o", "root", "-g", "wheel", "-m", "0755", str(ROOT)], commands)
        self.assertEqual(commands[-1], ["install", "-d", "-o", USER, "-g", USER, "-m", "0700",
                                        *(str(ROOT / d) for d in ("home", "tmp", "derived", "packages"))])

    def test_a_sandbox_that_is_made_and_checked_reports_every_stage(self):
        admin = Admin()
        error, checks = self.setup(admin, project=Path("/runner/may-private/sandbox-project.yml"))
        self.assertIsNone(error)
        self.assertEqual(admin.calls[0], ["chmod", "--", "0700", "/Users/runner"])
        self.assertIn(["install", "-o", USER, "-g", USER, "-m", "0644", "/runner/may-private/sandbox-project.yml",
                       str(ROOT / "src/ios/project.yml")], admin.calls)
        self.assertEqual([c for c in admin.calls if c[0] == "tee"],
                         [["tee", "-a", "--", "/usr/lib/cron/cron.deny"], ["tee", "-a", "--", "/usr/lib/cron/at.deny"]])
        self.assertEqual([c.args[0] for c in checks], [["/bin/test", "!", "-x", "/Users/runner"],
                                                       ["/bin/test", "!", "-x", "/runner"],
                                                       ["/bin/test", "-w", str(ROOT / "src")]])
        stages = [stage for stage, _, _ in self.said]
        self.assertEqual(stages[:2], ["ids", "home"])
        self.assertEqual(stages[-6:], ["project", "deny", "deny", "unreachable", "unreachable", "writable"])
        self.assertEqual(len(stages), 2 + len(mac_sandbox.setup_commands(self.copies)) + 6)
        self.assertTrue(all(status == 0 and cause is None for _, status, cause in self.said))

    def test_it_fails_closed_at_the_first_stage_that_does_not_succeed(self):
        # Coordinator, 31 Oct: any sandbox step that fails ends the job red, with nothing compiled and nothing run as
        # the runner user instead.
        for case, fail, checks, refusal in (
                ("the home not closed", {"chmod": 1}, None, "home could not be closed"),
                ("the user not made", {("dscl", ".", "-create"): 1}, None, "could not be set up"),
                ("a copy not made", {"cp": 1}, None, "could not be set up"),
                ("sudo refused", {"rm": None}, None, "could not be set up"),
                ("no project", {"install": 0, ("install", "-o", USER): 1}, None, "could not be pointed"),
                ("no cron deny", {"tee": 1}, None, "denied cron and at"),
                ("the home reachable", {}, lambda argv, log: 1 if argv[:2] == ["/bin/test", "!"] else 0,
                 "can reach the runner's files"),
                ("no check run", {}, lambda argv, log: None, "can reach the runner's files"),
                ("its copy not writable", {}, lambda argv, log: 1 if argv[1] == "-w" else 0, "cannot write")):
            with self.subTest(case=case):
                self.said = []
                admin = Admin(fail)
                error, ran = self.setup(admin, checks or (lambda argv, log: 0),
                                        project=Path("/runner/may-private/sandbox-project.yml"))
                self.assertIsInstance(error, SandboxError)
                self.assertIn(refusal, str(error))
                self.assertTrue(str(error).endswith("; nothing ran"))
                self.assertNotEqual(self.said[-1][1], 0, "the failing stage's status is the last one said")
                self.assertIn(self.said[-1][2], mac_sandbox.ERROR_WORDS, "and its class, in fixed words")

    def test_a_taken_name_or_id_refuses_before_any_root_command(self):
        admin = Admin()
        error, _ = self.setup(admin, ids=lambda: True)
        self.assertIn("already taken", str(error))
        self.assertEqual(admin.calls, [])
        self.assertEqual(self.said, [("ids", 1, "other")])

    def test_ids_are_read_from_both_users_and_groups(self):
        def listing(users, groups):
            def run(argv, **kwargs):
                out = users if argv[3] == "/Users" else groups
                return mock.Mock(returncode=0, stdout=out.encode())
            return run
        free = listing("root 0\nrunner 501\n", "wheel 0\nstaff 20\n")
        self.assertFalse(mac_sandbox.ids_taken(free))
        for case, run in (("the name", listing(f"{USER} 600\n", "")), ("a user id", listing("other 497\n", "")),
                          ("a group id", listing("", "other 497\n")),
                          ("an unreadable list", lambda argv, **kwargs: mock.Mock(returncode=1, stdout=b"")),
                          ("dscl missing", mock.Mock(side_effect=OSError))):
            with self.subTest(case=case):
                self.assertTrue(mac_sandbox.ids_taken(run))


class StopAndTeardownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "log"

    def tearDown(self):
        self.temp.cleanup()

    def test_a_survivor_fails_after_every_round(self):
        admin = Admin(pgrep=0)
        with mock.patch("mac_sandbox.admin", admin), mock.patch("mac_sandbox.time.sleep"):
            with self.assertRaisesRegex(SandboxError, "could not be stopped"):
                mac_sandbox.stop_all(self.log)
        kills = [c for c in admin.calls if c[:3] == ["-u", USER, "/bin/kill"]]
        self.assertEqual(len(kills), mac_sandbox.STOP_ROUNDS)
        self.assertEqual(kills[0], ["-u", USER, "/bin/kill", "-KILL", "-1"])

    def test_teardown_kills_sweeps_and_removes_the_user_and_group(self):
        admin = Admin()
        with mock.patch("mac_sandbox.admin", admin), mock.patch("mac_sandbox.os.path.isdir", return_value=True):
            self.assertEqual(mac_sandbox.teardown(self.log, exists=lambda kind: True), [])
        self.assertEqual(admin.calls[-3:], [["rm", "-rf", "--", str(ROOT)], ["dscl", ".", "-delete", f"/Users/{USER}"],
                                            ["dscl", ".", "-delete", f"/Groups/{USER}"]])
        self.assertEqual([c for c in admin.calls if c[0] == "find"],
                         [["find", folder, "-xdev", "-user", USER, "-delete"] for folder in mac_sandbox.LEFTOVER_DIRS])
        self.assertIn(["-u", USER, "/bin/kill", "-KILL", "-1"], admin.calls)

    def test_teardown_names_what_it_could_not_remove(self):
        # Run 41170609342 said "some files of the build user could not be removed" and nothing of which.
        admin = Admin({("find", "/private/var/folders", "-xdev"): 1, ("dscl", ".", "-delete"): 1})
        with mock.patch("mac_sandbox.admin", admin), mock.patch("mac_sandbox.os.path.isdir", return_value=True):
            left = mac_sandbox.teardown(self.log, exists=lambda kind: True)
        self.assertEqual(left, ["/private/var/folders", "the user", "the group"])
        self.assertTrue(set(left) <= set(mac_sandbox.TEARDOWN_NAMES))

    def test_a_setup_refused_before_the_user_was_made_leaves_nothing_to_report(self):
        # Run 41170609342: the home step failed before the user existed, so find -user and dscl -delete failed on a
        # user that was never made, and teardown said files were left.
        admin = Admin({"find": 1, "dscl": 1, "pgrep": 0})
        with mock.patch("mac_sandbox.admin", admin), mock.patch("mac_sandbox.os.path.isdir", return_value=True):
            self.assertEqual(mac_sandbox.teardown(self.log, exists=lambda kind: False), [])
        self.assertEqual(admin.calls, [["rm", "-rf", "--", str(ROOT)]], "no user, files or processes are looked for")

    def test_a_failed_commands_words_are_sorted_into_fixed_classes(self):
        for said, word in ((b"chmod: --: No such file or directory\n", "no such file"),
                           (b"chmod: /Users/runner: Operation not permitted\n", "not permitted"),
                           (b"sudo: a password is required\n", "sudo refused"),
                           (b"usage: chmod [-fhv] mode file\n", "usage"),
                           (b"<dscl_cmd> DS Error: -14135 (eDSRecordAlreadyExists)\n", "exists"),
                           (b"something else /Users/runner\n", "other")):
            with self.subTest(word=word):
                self.assertEqual(mac_sandbox.error_class(said), word)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            log.write_bytes(b"before\n")
            done = mock.Mock(returncode=1)

            def run(argv, stdout, **kwargs):
                stdout.write(b"chmod: --: No such file or directory\n")
                return done

            with mock.patch("mac_sandbox.subprocess.run", side_effect=run):
                self.assertEqual(mac_sandbox.admin_said(["chmod", "0700", "--", "/x"], log), (1, "no such file"))
            done.returncode = 0
            with mock.patch("mac_sandbox.subprocess.run", side_effect=run):
                self.assertEqual(mac_sandbox.admin_said(["chmod", "--", "0700", "/x"], log), (0, None))
            with mock.patch("mac_sandbox.subprocess.run", side_effect=OSError):
                self.assertEqual(mac_sandbox.admin_said(["chmod"], log), (None, "did not start"))

    def test_commands_run_as_the_user_with_none_of_the_runners_environment(self):
        argv = mac_sandbox.sandboxed(["xcodegen"], [ROOT / "xcodegen/xcodegen/bin"], ["DEVELOPER_DIR=/x"])
        self.assertEqual(argv[:7], ["sudo", "-n", "-u", USER, "--", "/usr/bin/env", "-i"])
        self.assertEqual(argv[7], f"PATH={ROOT}/xcodegen/xcodegen/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(argv[8:], [f"HOME={ROOT}/home", f"TMPDIR={ROOT}/tmp/", "DEVELOPER_DIR=/x", "xcodegen"])


class SandboxedCompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.source, self.private = base / "may-source", base / "may-private"
        (self.source / "ios").mkdir(parents=True)
        self.private.mkdir()
        self.log = self.private / "ios-compile.log"
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    said = b"Error: Spec validation failed\n"
    envs = []

    def build(self, statuses, setup_error=None, target="playground"):
        statuses = iter(statuses)

        def setup(log, copies, home, runner_temp, project, say):
            self.events.append(("setup", sorted(copies), project))
            if setup_error:
                raise setup_error

        def step(argv, log, cwd, deadline):
            command = argv[argv.index("-i") + 6:]  # PATH, HOME, TMPDIR, DEVELOPER_DIR, XDG_CACHE_HOME
            self.envs.append(" ".join(argv[argv.index("-i") + 1:argv.index("-i") + 6]))
            self.events.append(("step", command[0] if command[0] != "xcodegen" else " ".join(command[:2]), cwd))
            status = next(statuses, 0)
            if status:
                with log.open("ab") as written:
                    written.write(self.said)
            return status

        out = io.StringIO()
        (self.source / "ios/project.yml").write_text("name: May\n")
        with mock.patch("compile.mac_sandbox.setup", side_effect=setup), \
                mock.patch("compile.mac_sandbox.step", side_effect=step), \
                mock.patch("compile.stop_all", side_effect=lambda log: self.events.append(("stop",))), \
                mock.patch.dict("os.environ", {"RUNNER_TEMP": self.temp.name, "HOME": "/Users/runner"}), \
                contextlib.redirect_stdout(out):
            try:
                passed = compile_tool.build_sandboxed(self.source, self.private, self.private / "xcodegen",
                                                      Path("/Applications/Xcode_26.3.app/Contents/Developer"), target,
                                                      self.log)
            except SandboxError as error:
                passed = error
        return passed, out.getvalue()

    def test_each_stage_runs_as_the_user_is_stopped_after_and_prints_its_status(self):
        passed, out = self.build([0, 0])
        self.assertIs(passed, True)
        self.assertEqual(self.events, [("setup", ["src", "xcodegen"], None), ("step", "xcodegen version", "/"),
                                       ("stop",), ("step", "xcodegen dump", "/"), ("stop",),
                                       ("step", "xcodegen generate", "/"), ("stop",),
                                       ("step", "/usr/bin/xcodebuild", "/"), ("stop",)])
        self.assertEqual(out, "May iOS compile xcodegen version: exit 0\nMay iOS compile xcodegen dump: exit 0\n"
                              "May iOS compile xcodegen: exit 0\nMay iOS compile xcodebuild MayPlayground: exit 0\n")

    def test_a_failed_or_late_stage_ends_the_build_there(self):
        # Run 41173870659: a failed stage's line names the class of what it wrote, never its words.
        for statuses, line in (([65], "May iOS compile xcodegen version: exit 65 (spec invalid)\n"),
                               ([None], "May iOS compile xcodegen version: did not finish\n")):
            with self.subTest(status=statuses[0]):
                self.events = []
                passed, out = self.build(statuses)
                self.assertIs(passed, False)
                self.assertEqual([e[0] for e in self.events], ["setup", "step", "stop"])
                self.assertEqual(out, line)
        self.events = []
        passed, out = self.build([0, 65], target=None)
        self.assertIs(passed, False)
        # Run 41177789639: a build stage's line carries the mark of its telling line, the same failure the same mark.
        self.assertEqual(out.splitlines()[-1], "May iOS compile xcodegen dump: exit 65 (spec invalid #6c86c912)")
        self.assertEqual(len([e for e in self.events if e[0] == "step"]), 2)
        self.events = []
        passed, out = self.build([0, 0, 65], target=None)
        self.assertEqual(out.splitlines()[-1], "May iOS compile xcodegen: exit 65 (spec invalid #6c86c912)")
        self.events = []
        self.said = b"xcodebuild: error: '/private/var/may-sandbox/src/ios/May.xcodeproj' does not exist.\n"
        passed, out = self.build([0, 0, 0, 0, 65], target=None)
        self.assertEqual(out.splitlines()[-1], "May iOS compile xcodebuild MayPlayground: exit 65 (not found #240a0d8e)")
        self.assertNotIn("may-sandbox", out, "the stage's words never reach the line")

    def test_finer_classes_and_a_mark_never_carry_a_word(self):
        for said, word in ((b"error: Could not write project: File exists\n", "exists"),
                           (b"Error: The file couldn't be saved because the volume is read-only.\n", "read-only"),
                           (b"error: cannot create cache directory\n", "cache"),
                           (b"error: /private/var/may-sandbox/src/ios is not a directory\n", "invalid path"),
                           (b"error: You don't have permission to save the file\n", "permission"),
                           (b"Writing project...\nfatal: something unforeseen\n", "other")):
            with self.subTest(word=word):
                self.assertEqual(mac_sandbox.error_class(said), word)
        log = self.private / "marks.log"
        log.write_bytes("before\n\u2699\ufe0f  Generating plists...\nError: something /Users/runner\nmore\n".encode())
        mark = mac_sandbox.said_mark(log, len(b"before\n"))
        self.assertRegex(mark, "^[0-9a-f]{8}$")
        self.assertEqual(mark, mac_sandbox.said_mark(log, len(b"before\n")), "the same words, the same mark")
        log.write_bytes(b"Generating\nlast line alone\n")
        self.assertEqual(mac_sandbox.said_mark(log, 0), hashlib.sha256(b"last line alone").hexdigest()[:8])
        self.assertIsNone(mac_sandbox.said_mark(log, log.stat().st_size))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            compile_tool.say("xcodegen", 1, "other", "/Users/r")
            compile_tool.say("xcodegen", 1, "free words", "0123abcd")
        self.assertEqual(out.getvalue().splitlines(), ["May iOS compile xcodegen: exit 1 (other)",
                                                       "May iOS compile xcodegen: exit 1"])

    def test_the_tools_caches_are_the_users_own(self):
        self.build([0, 0])
        self.assertTrue(any(f"XDG_CACHE_HOME={ROOT}/home/.cache" in e for e in self.envs))

    def test_a_sandbox_that_cannot_be_made_runs_nothing(self):
        passed, out = self.build([0, 0], setup_error=SandboxError("The unprivileged user could not be set up; nothing ran"))
        self.assertIsInstance(passed, SandboxError)
        self.assertEqual([e[0] for e in self.events], ["setup"])
        self.assertEqual(out, "")

    def test_a_project_still_naming_runner_temp_is_refused(self):
        project = self.source / "ios/project.yml"
        project.write_text(f"packages:\n  Other:\n    path: {self.source.parent}/may-design-system\n")
        with mock.patch.dict("os.environ", {"RUNNER_TEMP": str(self.source.parent)}):
            with self.assertRaisesRegex(SandboxError, "still names runner temp"):
                compile_tool.sandbox_project(self.source, self.private)

    def test_the_projects_design_system_is_pointed_at_the_sandboxs_copy(self):
        project = self.source / "ios/project.yml"
        project.write_text(f"packages:\n  MayDesignSystem:\n    path: {self.source}/android/build-deps/may-design-system\n")
        pointed = compile_tool.sandbox_project(self.source, self.private)
        self.assertEqual(pointed.read_text(), "packages:\n  MayDesignSystem:\n    path: "
                                              f"{ROOT}/src/android/build-deps/may-design-system\n")
        project.write_text("packages:\n  MayDesignSystem:\n    url: https://example.invalid/ds\n")
        self.assertIsNone(compile_tool.sandbox_project(self.source, self.private))

    def native(self, sandbox, build=True):
        """compile_native for iOS, recorded, with the runner's tools faked: compile.run's commands, and whether the
        sandboxed build was asked for. The build passes (True), fails (False) or its sandbox fails (an error)."""
        root = Path(self.temp.name)
        (root / "may-source").mkdir(exist_ok=True)
        ran, sandboxed = [], []

        def run(argv, log, cwd=None, env=None):
            ran.append(Path(argv[0]).name)
            if argv[0] == "curl":
                Path(argv[argv.index("-o") + 1]).write_bytes(b"zip")

        def build_sandboxed(*args):
            sandboxed.append(args[4])
            if isinstance(build, Exception):
                raise build
            return build

        version = mock.Mock(returncode=0, stdout=b"Xcode 26.3\nBuild version 17C529\n")
        digest = mock.Mock(hexdigest=mock.Mock(return_value=compile_tool.XCODEGEN_SHA256))
        with mock.patch.dict("os.environ", {"RUNNER_TEMP": str(root), "HOME": "/Users/runner"}, clear=True), \
                mock.patch("compile.run", side_effect=run), mock.patch("compile.build_sandboxed", side_effect=build_sandboxed), \
                mock.patch("compile.mac_sandbox.teardown", return_value=[]) as teardown, \
                mock.patch("compile.subprocess.run", return_value=version), \
                mock.patch("compile.hashlib.sha256", return_value=digest), \
                mock.patch("compile.extract_generator", return_value=self.private / "xcodegen/xcodegen/bin/xcodegen"), \
                mock.patch("compile.os.statvfs", return_value=mock.Mock(f_bavail=1 << 40, f_frsize=1)), \
                mock.patch("compile.Path.is_dir", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            try:
                compile_tool.compile_native("ios", "playground", record=True, sandbox=sandbox)
                error = None
            except SandboxError as raised:
                error = raised
        outcome = self.private / compile_tool.OUTCOME
        return ran, sandboxed, teardown.call_count, error, outcome.read_text() if outcome.exists() else None

    def test_the_default_builds_as_today_and_never_sandboxed(self):
        ran, sandboxed, teardowns, error, outcome = self.native(sandbox=False)
        self.assertEqual((ran, sandboxed, teardowns, error), (["curl", "xcodegen", "xcodebuild"], [], 0, None))
        self.assertEqual(outcome, '{"result": "passed"}')

    def test_the_sandbox_never_falls_through_to_the_runner_build(self):
        for build, result in ((True, '{"result": "passed"}'), (False, '{"result": "failed"}'),
                              (SandboxError("The unprivileged user could not be set up; nothing ran"), None)):
            with self.subTest(build=build):
                (self.private / compile_tool.OUTCOME).unlink(missing_ok=True)
                ran, sandboxed, teardowns, error, outcome = self.native(sandbox=True, build=build)
                self.assertEqual(ran, ["curl"], "nothing but the download ran as the runner user")
                self.assertEqual((sandboxed, teardowns), (["playground"], 1))
                self.assertEqual(outcome, result, "a sandbox that fails records no outcome; the step ends red")
                self.assertEqual(error is not None, isinstance(build, Exception))

    def test_a_failed_stage_line_names_its_class_in_fixed_words_alone(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            compile_tool.say("home", 1, "no such file")
            compile_tool.say("setup 3 dscl", 0)
            compile_tool.say("xcodegen", None)
            compile_tool.say("home", 1, "/Users/runner: whatever")
        self.assertEqual(out.getvalue().splitlines(), ["May iOS compile home: exit 1 (no such file)",
                                                       "May iOS compile setup 3 dscl: exit 0",
                                                       "May iOS compile xcodegen: did not finish",
                                                       "May iOS compile home: exit 1"])

    def test_only_the_recorded_ios_compile_runs_sandboxed(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(compile_tool.CompileError, "Only the recorded iOS compile"):
                compile_tool.compile_native("ios", "playground", record=False, sandbox=True)


class SandboxStaticCheckTests(unittest.TestCase):
    TOOL = "tools/may_release/compile.py"
    SANDBOX = "tools/may_release/mac_sandbox.py"

    def flagged(self, path, old, new, message):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("compile.py", "mac_sandbox.py"):
                (root / "tools/may_release").mkdir(parents=True, exist_ok=True)
                (root / "tools/may_release" / name).write_text((static_check.ROOT / "tools/may_release" / name).read_text())
            target = root / path
            text = target.read_text()
            self.assertIn(old, text)
            target.write_text(text.replace(old, new, 1))
            problems = static_check.compile_sandbox_problems(root)
        self.assertTrue(any(message in p for p in problems), problems)

    def test_the_committed_tools_pass(self):
        self.assertEqual(static_check.compile_sandbox_problems(static_check.ROOT), [])
        self.assertTrue(static_check.COMPILE_IOS_PR.endswith(" --record-outcome --sandbox"))
        self.assertTrue(static_check.COMPILE_IOS_PR_RUNNER.endswith(" --record-outcome"))

    def test_the_workflow_builds_as_today_unless_dispatched_with_the_sandbox(self):
        # Coordinator, 31 Oct: the iOS compile gates every may-app pull request, so until a sandboxed run is green the
        # sandbox is opt-in (sandbox=true on a manual dispatch) and the default, which every automatic dispatch takes,
        # is today's build.
        text = (static_check.ROOT / ".github/workflows/native-compile-pr.yml").read_text()
        self.assertIn(static_check.COMPILE_PR_SANDBOX_INPUT, text)
        runner = ("        if: ${{ !inputs.sandbox }}\n        run: " + static_check.COMPILE_IOS_PR_RUNNER + "\n")
        sandboxed = ("        if: ${{ inputs.sandbox }}\n        run: " + static_check.COMPILE_IOS_PR + "\n")
        self.assertIn(runner, text)
        self.assertIn(sandboxed, text)
        self.assertEqual(text.count("tools/may_release/compile.py"), 2)

    def test_each_widening_is_flagged(self):
        compile_message = "compile.py: the pull request's code does not run as the sandbox's unprivileged user alone"
        held = "mac_sandbox.py: the unprivileged user is not exactly a fresh, groupless, sealed-off user"
        cases = {
            "xcodegen as the runner": (self.TOOL, 'mac_sandbox.step(mac_sandbox.sandboxed(["xcodegen"',
                                       'mac_sandbox.step((["xcodegen"', compile_message),
            "no stop after xcodegen": (self.TOOL, "    stop_all(log)\n    say(\"xcodegen\", status",
                                       "    say(\"xcodegen\", status", compile_message),
            "no teardown": (self.TOOL, "left = mac_sandbox.teardown(log)", "left = mac_sandbox.teardown(None)",
                            compile_message),
            "a fallback to the runner": (self.TOOL, "            return\n        try:\n            run([str(binary)",
                                         "        try:\n            run([str(binary)", compile_message),
            "unrecorded sandbox": (self.TOOL, "    if sandbox and not record:", "    if sandbox and record:",
                                   compile_message),
            "a run in the build": (self.TOOL, "    root = mac_sandbox.ROOT\n",
                                   "    root = mac_sandbox.ROOT\n    run(['true'], log)\n", compile_message),
            "the user in admin": (self.SANDBOX, '["dscl", ".", "-create", user, "IsHidden", "1"]',
                                  '["dscl", ".", "-append", "/Groups/admin", "GroupMembership", USER]', held),
            "a login shell": (self.SANDBOX, '"UserShell", "/usr/bin/false"', '"UserShell", "/bin/zsh"', held),
            "no cron deny": (self.SANDBOX, 'DENY_FILES = ("/usr/lib/cron/cron.deny", "/usr/lib/cron/at.deny")',
                             'DENY_FILES = ("/usr/lib/cron/at.deny",)', held),
            "the runner's environment kept": (self.SANDBOX, '"/usr/bin/env", "-i",', '"/usr/bin/env",', held),
            "one kill round": (self.SANDBOX, "    for _ in range(STOP_ROUNDS):", "    for _ in range(1):", held),
            "no cache sweep": (self.SANDBOX, '"/private/var/folders", str(ROOT))', "str(ROOT))", held),
            "no reach check": (self.SANDBOX, "    for path in (home, runner_temp):", "    for path in ():", held),
            "the group left": (self.SANDBOX, '    if exists("/Groups") and admin(["dscl", ".", "-delete", f"/Groups/{USER}"], '
                                             'log) != 0:\n        left.append("the group")\n', "", held),
            "the id not checked": (self.SANDBOX, '1 if ids() else 0', "0", held),
            "the generator its own": (self.SANDBOX, '"--", "root:wheel" if name != "src" else',
                                      '"--", "root:wheel" if name == "x" else',
                                      held),
        }
        for case, (path, old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(path, old, new, message)


if __name__ == "__main__":
    unittest.main()
