"""may-app's MayCore Swift package suite at a may-app pull request head (ios-core-pr.yml, ios_core.py). Swift is faked
by canned output in the shapes the pinned Swift 6.2.4 prints on Linux; GitHub is faked."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import ios_core as tool
import sandbox
import static_check
import store
from test_compile_pr import pull

SHA = "a" * 40
TOKEN = "ghp_" + "t" * 36
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"
WORKFLOW = ".github/workflows/ios-core-pr.yml"
PRIVATE = "PRIVATE-MAYCORE-TEXT-5e1c"
GIT_ENV = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
MANIFEST = ('// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n    name: "MayCore",\n'
            '    targets: [.target(name: "MayCore"), .testTarget(name: "MayCoreTests", dependencies: ["MayCore"])]\n)\n')


def passing(tests=2705, skipped=8):
    return (f"Test Suite 'All tests' passed at 2026-10-25 23:20:49.827\n"
            f"\t Executed {tests} tests, with {skipped} tests skipped and 0 failures (0 unexpected) in 31.5 (31.5) seconds\n"
            "◇ Test run started.\n↳ Testing Library Version: 6.2.4 (5ee435b15ad40ec)\n"
            "✔ Test run with 0 tests in 0 suites passed after 0.001 seconds.\n")


def failing(source):
    test = source / "ios/MayCore/Tests/MayCoreTests/FixtureTests.swift"
    return (f"{test}:162: error: FixtureTests.testFixtureValue : "
            f"XCTAssertNotEqual failed: (\"{PRIVATE}\") is equal to (\"{PRIVATE}\")\n"
            "Test Case 'FixtureTests.testFixtureValue' failed (0.0 seconds)\n"
            "Test Suite 'All tests' failed at 2026-10-25 23:20:49.827\n"
            "\t Executed 2705 tests, with 8 tests skipped and 1 failure (0 unexpected) in 31.5 (31.5) seconds\n"
            "◇ Test run started.\n"
            f"✘ Test fixtureRows() recorded an issue at FixtureRowTests.swift:12:3: Expectation failed: {PRIVATE}\n"
            "✘ Test run with 3 tests in 1 suite failed after 0.002 seconds with 1 issue.\n")


def broken(source):
    line = f"{source}/ios/MayCore/Sources/MayCore/Browser.swift:3:7: error: cannot find '{PRIVATE}' in scope\n"
    return "[1/9] Compiling MayCore Browser.swift\n" + line + "    let x = " + PRIVATE + "\n" + line


class RunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/build-deps/may-design-system").mkdir(parents=True)
        self.private.mkdir()
        self.bin = self.root / "may-tools/swift/bin"
        self.calls = []
        # No test makes the real user or copies as root: a run that reaches admin without run_it's fake fails here.
        guard = patch.object(sandbox, "admin", side_effect=AssertionError("a real sudo command in a test"))
        guard.start()
        self.addCleanup(guard.stop)

    def tearDown(self):
        self.directory.cleanup()

    def commit(self, manifest=MANIFEST, extra=None):
        """A one-commit may-source with ios/MayCore's manifest as given (or no package at all); its SHA."""
        shutil.rmtree(self.source / ".git", ignore_errors=True)
        shutil.rmtree(self.source / "ios", ignore_errors=True)
        files = dict(extra or {})
        if manifest is not None:
            files["ios/MayCore/Package.swift"] = manifest
        files["README"] = "x\n"
        for name, text in files.items():
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text(text)
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "x"]):
            subprocess.run(argv, cwd=self.source, env=GIT_ENV, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.source, env=GIT_ENV, capture_output=True,
                              check=True).stdout.decode().strip()

    def fake(self, build=0, test=0, build_output="Build complete!\n", test_output=None):
        """run_tool as Swift would answer: (status or None for out of time) and its output, written to the log."""
        def run_tool(argv, log, cwd, deadline):
            self.calls.append((list(argv), cwd, {}))
            self.assertGreater(deadline, time.monotonic())
            self.admin_calls.append(["run_tool"])
            argv = argv[argv.index("swift"):] if "swift" in argv else argv
            with log.open("a") as stream:
                if argv[:2] == ["swift", "build"]:
                    stream.write(build_output)
                    return build
                if argv[:2] == ["swift", "test"]:
                    stream.write(passing() if test_output is None else test_output)
                    return test
            raise AssertionError("unexpected tool " + argv[0])
        return run_tool

    def run_it(self, sha, run_tool=None, version="6.2.4", extra=None, admin_fails=None, reachable=(),
               survivors=0):
        environ = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "HOME": "/home/runner",
                   "SOMETHING_ELSE": "x", **(extra or {})}
        out = io.StringIO()
        self.admin_calls = []

        self.as_sandbox_calls, left = [], [survivors]

        def admin(argv, log, data=None):
            self.admin_calls.append(list(argv) + ([data] if data is not None else []))
            if argv[0] == "pgrep":  # 1: none left; a surviving process answers 0 for that many rounds
                left[0] -= 1
                return 0 if left[0] >= 0 else 1
            return 1 if admin_fails and argv[0] == admin_fails else 0

        def as_sandbox(argv, log):
            self.as_sandbox_calls.append(list(argv))
            return 1 if argv[-1] in reachable else 0
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", out), \
                patch.object(sandbox, "admin", admin), patch.object(sandbox, "as_sandbox", as_sandbox), \
                patch.object(sandbox.time, "sleep", lambda seconds: None), \
                patch.object(sandbox, "step", run_tool or self.fake()), \
                patch.object(tool, "swift_version", lambda env: version), \
                patch.object(tool, "pinned_swift_bin", lambda: self.bin):
            tool.run_suite(sha)
        outcome = json.loads((self.private / tool.OUTCOME).read_text())
        failures = json.loads((self.private / tool.FAILURES).read_text())
        return out.getvalue(), outcome, failures

    def test_a_passing_suite_prints_fixed_lines_and_counts(self):
        out, outcome, failures = self.run_it(self.commit())
        self.assertEqual(out.splitlines(), [
            "May iOS core build: passed", "May iOS core tests: passed",
            "May iOS core tests: Executed 2705 tests, with 8 skipped and 0 failures"])
        self.assertEqual(outcome, {"result": "passed", "stages": {"build": "passed", "tests": "passed"},
                                   "tests": [2705, 8, 0], "problems": 0})
        self.assertEqual(failures, {"failures": [], "total": 0})

    def test_the_tests_read_project_yml_as_committed_not_as_prepare_rewrote_it(self):
        """N, 30 Oct: prepare --platform ios points MayDesignSystem at the local checkout, so MayCore's pin test, which
        reads project.yml for `revision: <the lock's sha>`, failed on every pull request; the head's own blob goes back."""
        pinned = "packages:\n  MayDesignSystem:\n    url: x\n    revision: " + "3" * 40 + "\n"
        sha = self.commit(extra={"ios/project.yml": pinned})
        (self.source / "ios/project.yml").write_text("packages:\n  MayDesignSystem:\n    path: /tmp/ds\n")
        seen = []

        def ran(source, swift_top, private, log):
            seen.append((source / "ios/project.yml").read_text())
            tool.record(private, "passed", {"build": "passed", "tests": "passed"}, None, [], 0)

        with patch.object(tool, "run_sandboxed", side_effect=ran):
            self.run_it(sha)
        self.assertEqual(seen, [pinned], "the build and tests see project.yml exactly as the head has it")

    def test_a_project_yml_that_cannot_be_read_back_or_is_a_link_runs_nothing(self):
        sha = self.commit()
        (self.source / "ios/project.yml").write_text("rewritten, but not in the head\n")
        with patch.object(tool, "run_sandboxed", side_effect=AssertionError("ran")), \
                self.assertRaises(tool.IosCoreError):
            self.run_it(sha)
        sha = self.commit(extra={"ios/project.yml": "revision: x\n"})
        (self.source / "ios/project.yml").unlink()
        (self.source / "ios/project.yml").symlink_to(self.root / "elsewhere.yml")
        with patch.object(tool, "run_sandboxed", side_effect=AssertionError("ran")), \
                self.assertRaises(tool.IosCoreError):
            self.run_it(sha)
        self.assertFalse((self.root / "elsewhere.yml").exists(), "nothing is written through the link")
        sha = self.commit()
        ran = []
        with patch.object(tool, "run_sandboxed", side_effect=lambda source, swift_top, private, log: ran.append(1) or
                          tool.record(private, "passed", {"build": "passed", "tests": "passed"}, None, [], 0)):
            self.run_it(sha)
        self.assertEqual(ran, [1], "a head with no project.yml runs as before")

    def test_build_then_tests_in_the_package_with_no_fetch_no_secret_and_private_folders(self):
        self.run_it(self.commit(), extra={"MAY_APP_PR_TOKEN_UNRELATED": "x"})
        scratch = ["--scratch-path", str(sandbox.ROOT / "build")]
        self.assertEqual([argv for argv, _, _ in self.calls], [
            tool.swift(["swift", "build", "--build-tests", "--disable-automatic-resolution"] + scratch),
            tool.swift(["swift", "test", "--skip-build", "--disable-automatic-resolution"] + scratch)])
        self.assertEqual(tool.swift(["x"]), [
            "sudo", "-n", "-u", "may-sandbox", "--", "/usr/bin/env", "-i",
            f"PATH={sandbox.ROOT}/swift/usr/bin:/usr/bin:/bin", f"HOME={sandbox.ROOT}/home", "x"])
        for argv, cwd, env in self.calls:
            with self.subTest(tool=argv[:2]):
                self.assertEqual(cwd, sandbox.ROOT / "src/ios/MayCore")
                for name in ("SOMETHING_ELSE", "MAY_APP_PR_TOKEN_UNRELATED"):
                    self.assertNotIn(name, env)
        with self.assertRaises(store.StoreError):
            self.run_it(self.commit(), extra={"MAY_APP_PR_COMMENT_TOKEN": TOKEN})

    def test_failing_tests_are_recorded_privately_and_counted_publicly(self):
        out, outcome, failures = self.run_it(self.commit(), self.fake(test=1, test_output=failing(self.source)))
        self.assertEqual(out.splitlines(), [
            "May iOS core build: passed", "May iOS core tests: failed",
            "May iOS core tests: Executed 2708 tests, with 8 skipped and 2 failures",
            "May iOS core suite: 2 failures found, which go to the may-app pull request only"])
        self.assertEqual(outcome["tests"], [2708, 8, 2])
        self.assertEqual(outcome["result"], "failed")
        self.assertEqual(failures["total"], 2)
        first, second = failures["failures"]
        self.assertEqual(first["test"], "FixtureTests.testFixtureValue")
        self.assertEqual(first["at"], "ios/MayCore/Tests/MayCoreTests/FixtureTests.swift:162")
        self.assertIn(PRIVATE, first["message"])
        self.assertEqual((second["test"], second["at"]), ("fixtureRows()", "FixtureRowTests.swift:12"))
        self.assertNotIn(PRIVATE, out)
        self.assertNotIn("FixtureTests", out)

    def test_a_test_run_that_exits_non_zero_fails_even_with_a_clean_summary(self):
        out, outcome, _ = self.run_it(self.commit(), self.fake(test=1))
        self.assertEqual(outcome["result"], "failed")
        self.assertIn("May iOS core tests: failed", out)
        for summary in ("", "Test Suite 'All tests' passed\n\t Executed 0 tests, with 0 failures (0 unexpected) in 0 "
                            "(0) seconds\n"):
            with self.subTest(summary=summary[:20]):
                self.calls = []
                out, outcome, _ = self.run_it(self.commit(), self.fake(test_output=summary))
                self.assertEqual(outcome["result"], "failed", "no test at all is not a pass")

    def test_a_failed_build_records_its_errors_and_runs_no_test(self):
        out, outcome, failures = self.run_it(self.commit(), self.fake(build=1, build_output=broken(self.source)))
        self.assertEqual(out.splitlines(), [
            "May iOS core build: failed", "May iOS core tests: did not run (the build stopped it)",
            "May iOS core suite: 1 failures found, which go to the may-app pull request only"])
        self.assertEqual([argv[argv.index("swift"):][:2] for argv, _, _ in self.calls], [["swift", "build"]])
        self.assertEqual(outcome, {"result": "failed", "stages": {"build": "failed", "tests": "not run"},
                                   "tests": None, "problems": 1})
        self.assertEqual(failures["failures"], [{"test": None, "message": f"cannot find '{PRIVATE}' in scope",
                                                 "at": "ios/MayCore/Sources/MayCore/Browser.swift:3:7"}])
        self.assertNotIn(PRIVATE, out)

    def test_a_suite_that_does_not_finish_is_a_timeout(self):
        out, outcome, _ = self.run_it(self.commit(), self.fake(build=None))
        self.assertEqual(outcome["result"], "timeout")
        self.assertEqual(outcome["stages"], {"build": "timeout", "tests": "not run"})
        self.assertIn("May iOS core build: did not finish in time", out)
        self.calls = []
        out, outcome, _ = self.run_it(self.commit(), self.fake(test=None, test_output=""))
        self.assertEqual(outcome["stages"], {"build": "passed", "tests": "timeout"})
        self.assertEqual(outcome["result"], "timeout")

    def test_a_package_dependency_is_refused_before_anything_runs(self):
        cases = {
            "a manifest dependency": (MANIFEST.replace("    targets:", '    dependencies: [.package(url: "https://'
                                                        'example.invalid/x.git", from: "1.0.0")],\n    targets:'), None),
            "a spaced dependency": (MANIFEST + "let more = .package (path: \"../x\")\n", None),
            "a resolved file": (MANIFEST, {"ios/MayCore/Package.resolved": "{}\n"}),
        }
        for case, (manifest, extra) in cases.items():
            with self.subTest(case=case):
                self.calls = []
                with self.assertRaises(tool.IosCoreError):
                    self.run_it(self.commit(manifest, extra))
                self.assertEqual(self.calls, [])

    def test_a_commit_without_maycore_fails_and_never_skips(self):
        """Review of #128: removing or linking the package away was a green skip."""
        cases = {"no package": (None, None), "a linked package": (None, "link")}
        for case, (manifest, how) in cases.items():
            with self.subTest(case=case):
                self.calls = []
                sha = self.commit(manifest)
                if how == "link":
                    (self.source / "elsewhere").mkdir(exist_ok=True)
                    (self.source / "elsewhere/Package.swift").write_text(MANIFEST)
                    (self.source / "ios").mkdir(exist_ok=True)
                    (self.source / "ios/MayCore").symlink_to(self.source / "elsewhere")
                    for argv in (["git", "add", "-A"], ["git", "commit", "-q", "-m", "link"]):
                        subprocess.run(argv, cwd=self.source, env=GIT_ENV, check=True, capture_output=True)
                    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.source, env=GIT_ENV,
                                         capture_output=True, check=True).stdout.decode().strip()
                out, outcome, failures = self.run_it(sha)
                self.assertEqual(out, "May iOS core suite: this commit has no ios/MayCore package; failed\n")
                self.assertEqual((outcome["result"], failures), ("failed", {"failures": [], "total": 0}))
                self.assertEqual(self.calls, [])

    def test_the_pull_requests_swift_runs_as_the_unprivileged_user_in_its_own_copies_and_is_stopped_after_each_step(self):
        self.run_it(self.commit())
        swift_top = self.bin.parent.parent
        setup = sandbox.setup_commands(self.source, {"swift": swift_top})
        kill = [["-u", "may-sandbox", "/usr/bin/kill", "-KILL", "-1"], ["pgrep", "-u", "may-sandbox"]]
        self.assertEqual(self.admin_calls, [
            ["chmod", "0700", "--", "/home/runner"], *setup,
            ["tee", "-a", "--", "/etc/cron.deny", b"may-sandbox\n"], ["tee", "-a", "--", "/etc/at.deny", b"may-sandbox\n"],
            ["run_tool"], *kill, ["run_tool"], *kill,
            *kill, sandbox.leftovers(), ["rm", "-rf", "--", str(sandbox.ROOT)], ["userdel", "may-sandbox"]])
        self.assertEqual(self.as_sandbox_calls, [["/usr/bin/test", "!", "-x", "/home/runner"],
                                                 ["/usr/bin/test", "!", "-x", str(self.root)]],
                         "as the build user, neither the runner's home nor runner temp can be entered")
        with patch.object(sandbox.os.path, "isdir", lambda path: path != "/var/lib/php/sessions"):
            self.assertEqual(sandbox.leftovers(), [
                "find", "/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue", "/var/crash", "/mnt",
                "/opt/may-sandbox", "-xdev", "-user", "may-sandbox", "-delete"],
                "the world-writable places and the sandbox, never the root")
        useradd = [argv for argv in setup if argv[0] == "useradd"]
        self.assertEqual(len(useradd), 1)
        for flag in ("-G", "--groups", "-aG", "sudo"):
            self.assertNotIn(flag, useradd[0], "the user joins no group but its own")
        self.assertEqual(useradd[0][-1], "may-sandbox")
        self.assertIn(["chown", "-R", "may-sandbox:may-sandbox", "--", str(sandbox.ROOT / "src")], setup)
        self.assertNotIn(str(swift_top), [a for argv in setup if argv[0] == "chown" for a in argv],
                         "the toolchain's copy is not theirs")

    def test_a_sandbox_that_cannot_be_made_runs_nothing_and_is_still_removed(self):
        for step in ("useradd", "cp", "chown", "install"):
            with self.subTest(step=step):
                self.calls = []
                with self.assertRaises(sandbox.SandboxError):
                    self.run_it(self.commit(), admin_fails=step)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.admin_calls[-3:], [sandbox.leftovers(), ["rm", "-rf", "--", str(sandbox.ROOT)],
                                                         ["userdel", "may-sandbox"]])

    def test_a_cleanup_that_cannot_remove_the_build_users_files_says_so_in_a_fixed_line(self):
        out, outcome, _ = self.run_it(self.commit(), admin_fails="find")
        self.assertEqual(outcome["result"], "passed")
        self.assertEqual(out.splitlines()[-1], "May iOS core suite: some files of the build user could not be removed")

    def test_a_build_user_that_can_reach_the_runners_files_runs_nothing(self):
        """Second re-review of #128: the token mints' outputs and the job's command files sit under the runner's
        home and runner temp; if the build user can enter either, nothing runs."""
        for path in ("/home/runner", str(self.root)):
            with self.subTest(path=path):
                self.calls = []
                with self.assertRaises(sandbox.SandboxError):
                    self.run_it(self.commit(), reachable=(path,))
                self.assertEqual(self.calls, [])
                self.assertEqual(self.admin_calls[-1], ["userdel", "may-sandbox"])

    def test_the_build_user_is_killed_until_none_is_left_and_a_survivor_fails_the_step(self):
        out, outcome, _ = self.run_it(self.commit(), survivors=3)
        self.assertEqual(outcome["result"], "passed", "three rounds, then none left")
        kills = [argv for argv in self.admin_calls if argv[-1:] == ["-1"]]
        self.assertGreaterEqual(len(kills), 4)
        self.calls = []
        with self.assertRaises(sandbox.SandboxError):
            self.run_it(self.commit(), survivors=10 * sandbox.STOP_ROUNDS)
        self.assertEqual(len(self.calls), 1, "the tests never start after a build whose processes survive")

    def test_failures_in_the_sandboxs_copy_are_repo_relative(self):
        out, outcome, failures = self.run_it(self.commit(), self.fake(test=1, test_output=failing(sandbox.ROOT / "src")))
        self.assertEqual(failures["failures"][0]["at"], "ios/MayCore/Tests/MayCoreTests/FixtureTests.swift:162")

    def test_only_the_pinned_swift_runs(self):
        for version in ("6.2.3", None):
            with self.subTest(version=version):
                with self.assertRaises(tool.IosCoreError):
                    self.run_it(self.commit(), version=version)
                self.assertEqual(self.calls, [])

    def test_run_tool_stops_at_the_shared_deadline(self):
        log, stopped = self.private / "x.log", []
        with patch.object(sandbox, "stop_all", lambda log, strict=True: stopped.append(True)):
            self.assertIsNone(sandbox.step(["true"], log, self.root, time.monotonic() - 1))
            self.assertEqual(sandbox.step(["true"], log, self.root, time.monotonic() + 30), 0)
            self.assertEqual(stopped, [])
            self.assertIsNone(sandbox.step(["sleep", "5"], log, self.root, time.monotonic() + 0.2))
        self.assertEqual(stopped, [True], "past the deadline every sandbox process is stopped")
        self.assertEqual(tool.RUN_SECONDS, 25 * 60)

    def test_a_timed_out_sudo_child_is_never_signalled_and_the_timeout_is_recorded(self):
        """Re-review of #128: run_tool's child is root's sudo, which the runner may not signal; killing it raised
        PermissionError, so the step crashed and no timeout was recorded."""
        test = self

        class RootSudo:
            stopped = False

            def __init__(self, argv, **kwargs):
                self.argv = argv

            def wait(self, timeout=None):
                if not RootSudo.stopped:
                    raise subprocess.TimeoutExpired(self.argv, timeout)
                return -9

            def kill(self):
                raise PermissionError(1, "Operation not permitted")

            terminate = send_signal = kill

        def stop(log, strict=True):
            RootSudo.stopped = True
        with patch.object(sandbox.subprocess, "Popen", RootSudo), patch.object(sandbox, "stop_all", stop):
            try:
                status = sandbox.step(tool.swift(["swift", "build"]), self.private / "x.log", self.root,
                                      time.monotonic() + 0.1)
            except Exception as error:  # the bug: signalling root's sudo raised
                self.fail(f"run_tool raised {type(error).__name__} instead of recording a timeout")
            self.assertIsNone(status)
        self.assertTrue(RootSudo.stopped)
        # And through the whole run: the build's timeout is recorded, never a crash.
        RootSudo.stopped = False
        sha, real, popen = self.commit(), sandbox.step, subprocess.Popen
        only_sudo = lambda argv, **kwargs: RootSudo(argv) if argv[0] == "sudo" else popen(argv, **kwargs)
        with patch.object(sandbox.subprocess, "Popen", only_sudo):
            try:
                out, outcome, _ = self.run_it(sha, run_tool=lambda argv, log, cwd, deadline: real(
                    argv, log, cwd, time.monotonic() + 0.1))
            except Exception as error:
                self.fail(f"the run raised {type(error).__name__} instead of recording a timeout")
        self.assertEqual((outcome["result"], outcome["stages"]), ("timeout", {"build": "timeout", "tests": "not run"}))
        self.assertIn("May iOS core build: did not finish in time", out)


class ParseTests(unittest.TestCase):
    def test_counts_read_xctests_last_summary_plus_swift_testing(self):
        self.assertEqual(tool.test_counts(passing()), (2705, 8, 0))
        self.assertEqual(tool.test_counts(failing(Path("/r/may-source"))), (2708, 8, 2))
        early = "\t Executed 3 tests, with 1 failure (0 unexpected) in 0.1 (0.1) seconds\n"
        self.assertEqual(tool.test_counts(early + passing(10, 0)), (10, 0, 0), "the last summary counts")
        thrown = "\t Executed 1 test, with 1 failure (1 unexpected) in 0.1 (0.1) seconds\n"
        self.assertEqual(tool.test_counts(thrown), (1, 0, 1), "an unexpected failure is counted once")
        self.assertIsNone(tool.test_counts("✔ Test run with 3 tests in 1 suite passed after 0.001 seconds.\n"))
        self.assertIsNone(tool.test_counts("Build complete!\n"))

    def test_entries_are_repo_relative_and_capped(self):
        roots = ["/r/may-source"]
        many = "".join(f"/r/may-source/ios/MayCore/Sources/A.swift:{n}:1: error: e{n}\n" for n in range(1, 150))
        errors, total = tool.compiler_errors(many, roots)
        self.assertEqual((len(errors), total), (tool.MAX_FAILURES, 149))
        self.assertEqual(errors[0]["at"], "ios/MayCore/Sources/A.swift:1:1")
        other = tool.compiler_errors("/home/runner/x/B.swift:2:3: error: at /home/runner/x/B.swift\n", roots)[0][0]
        self.assertEqual((other["at"], other["message"]), ("[runner path]:2:3", "at [runner path]"))


def recorded(result="failed", entries=None, stages=None, tests=(2705, 8, 1)):
    entries = [{"test": "FixtureTests.testValues", "message": f"XCTAssertNotEqual failed: {PRIVATE}",
                "at": "ios/MayCore/Tests/MayCoreTests/FixtureTests.swift:162"}] if entries is None else entries
    return {"result": result, "stages": stages or {"build": "passed", "tests": "failed"},
            "tests": list(tests) if tests is not None else None, "problems": len(entries)}, \
        {"failures": entries, "total": len(entries)}


class PostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "may-private").mkdir()
        (self.root / "may-source").mkdir()
        self.write(*recorded())
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def write(self, outcome, failed):
        (self.root / "may-private" / tool.OUTCOME).write_text(json.dumps(outcome))
        (self.root / "may-private" / tool.FAILURES).write_text(json.dumps(failed))

    def api(self, pull_answer=None, comments=()):
        def call(method, path, payload, token):
            self.calls.append((method, path, payload))
            if method == "GET" and path == PULLS:
                return 200, pull_answer if pull_answer is not None else pull()
            if method == "GET" and path.startswith(COMMENTS):
                return 200, list(comments)
            if method == "POST" and path == COMMENTS:
                return 201, {}
            raise AssertionError("unexpected call " + method + " " + path)
        return call

    def post(self, call, token=TOKEN):
        environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), "GITHUB_RUN_ID": "9",
                   "MAY_CI_APP_SLUG": "may-ci"}
        if token is not None:
            environ["MAY_APP_PR_COMMENT_TOKEN"] = token
        out = io.StringIO()
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", out), \
                patch.object(tool, "head_revision", lambda checkout: SHA):
            code = tool.post(SHA, "42", call_api=call)
        return code, out.getvalue()

    def posted(self):
        return [payload["body"] for method, path, payload in self.calls if method == "POST"]

    def test_failures_are_posted_once_on_the_pull_request_and_the_job_fails(self):
        code, out = self.post(self.api())
        self.assertEqual((code, out), (1, "May iOS core suite failed; 1 failures posted to may-app #42\n"))
        [body] = self.posted()
        self.assertIn(PRIVATE, body)
        self.assertIn("ios/MayCore/Tests/MayCoreTests/FixtureTests.swift:162: FixtureTests.testValues",
                      body)
        self.assertIn("Executed 2705 tests, with 8 skipped and 1 failures.", body)
        self.assertIn(f"<!-- {tool.MARKER} head={SHA} -->", body)
        self.assertNotIn(PRIVATE, out)

    def test_a_build_failure_names_compiler_errors(self):
        self.write(*recorded(entries=[{"test": None, "message": "cannot find 'x' in scope",
                                       "at": "ios/MayCore/Sources/MayCore/A.swift:3:7"}],
                             stages={"build": "failed", "tests": "not run"}, tests=None))
        self.post(self.api())
        [body] = self.posted()
        self.assertIn("**1 compiler error**", body)
        self.assertIn("ios/MayCore/Sources/MayCore/A.swift:3:7\n    cannot find 'x' in scope", body)

    def test_a_pass_or_a_skip_posts_nothing(self):
        for result in ("passed", "skipped"):
            with self.subTest(result=result):
                self.calls = []
                self.write(*recorded(result, [], {"build": "passed", "tests": "passed"}))
                if result == "skipped":
                    with self.assertRaises(tool.IosCoreError, msg="a skip is no longer a result"):
                        self.post(self.api())
                    continue
                self.assertEqual(self.post(self.api()), (0, "May iOS core suite: nothing to post\n"))
                self.assertEqual(self.calls, [])

    def test_a_missing_package_is_posted_as_such(self):
        self.write(*recorded("failed", [], {"build": "not run", "tests": "not run"}, None))
        code, _ = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertIn("This commit has no `ios/MayCore` package (or it is a link), so nothing ran.", self.posted()[0])

    def test_a_timeout_is_posted_and_fails(self):
        self.write(*recorded("timeout", [], {"build": "timeout", "tests": "not run"}, None))
        code, _ = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertIn("did not finish within 25 minutes", self.posted()[0])

    def test_without_the_token_nothing_is_posted_and_the_job_still_fails(self):
        code, out = self.post(self.api(), token=None)
        self.assertEqual(code, 1)
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN is not set", out)
        self.assertEqual(self.calls, [])

    def test_a_moved_head_or_an_earlier_post_posts_nothing(self):
        with self.assertRaises(tool.IosCoreError):
            self.post(self.api(pull(head__sha="b" * 40)))
        self.assertEqual(self.posted(), [])
        self.calls = []
        earlier = [{"body": f"x <!-- {tool.MARKER} head={SHA} -->", "user": {"type": "Bot", "login": "may-ci[bot]"}}]
        code, out = self.post(self.api(comments=earlier))
        self.assertEqual(code, 1)
        self.assertIn("already posted", out)
        self.assertEqual(self.posted(), [])
        # Review of may-releases #137: a person's or another app's comment quoting the marker is not this post.
        for user in ({"type": "User", "login": "may-ci[bot]"}, {"type": "Bot", "login": "x[bot]"}, {"login": "x"}):
            with self.subTest(user=user):
                self.calls = []
                self.post(self.api(comments=[dict(earlier[0], user=user)]))
                self.assertEqual(len(self.posted()), 1)

    def test_a_malformed_record_is_refused(self):
        good_outcome, good_failed = recorded()
        cases = {
            "an unknown result": (dict(good_outcome, result="maybe"), good_failed),
            "an unknown stage": (dict(good_outcome, stages={"build": "passed"}), good_failed),
            "a count that is not an integer": (dict(good_outcome, tests=[1, 0, "1"]), good_failed),
            "a multi-line message": (good_outcome, {"failures": [dict(good_failed["failures"][0], message="a\nb")],
                                                    "total": 1}),
            "an extra field": (good_outcome, {"failures": [dict(good_failed["failures"][0], log="x")], "total": 1}),
        }
        for case, (outcome, failed) in cases.items():
            with self.subTest(case=case):
                self.write(outcome, failed)
                with self.assertRaises(tool.IosCoreError):
                    self.post(self.api())


class CliTests(unittest.TestCase):
    def test_an_unprepared_runner_prints_only_a_fixed_line(self):
        script = Path(tool.__file__)
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", str(script), "run", "--source-sha", SHA],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(), "May iOS core suite stopped: Exact private checkout is not prepared")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def flagged(self, relative, old, new, message, case):
        self.tearDown()
        self.setUp()
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, "mutation anchor not found: " + case)
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        self.assertTrue(any(message in problem for problem in found), (case, found))

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.ios_core_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.ios_core_print_problems(static_check.ROOT), [])

    def test_each_widening_is_flagged(self):
        name = "ios-core-pr.yml: "
        secret = name + "a secret or env entry other than"
        suite = "      - name: Build and test MayCore without any credential, printing only pass or fail and counts\n" \
                "        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n"
        post = "      - name: Post the MayCore failures on the may-app pull request, never in this log\n"
        cases = {
            "a read key on the suite": (suite, suite + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the comment token on the suite": (suite, suite + "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n", secret),
            "a GitHub token on the suite": (suite, suite + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a longer timeout": ("    timeout-minutes: 35\n", "    timeout-minutes: 120\n", name + "sets a shell, defaults"),
            "a cache": (post, "      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830 # v4\n        with:\n"
                              "          path: ~/.swiftpm\n          key: swift\n" + post, "ios-core-pr.yml: uses an Actions cache"),
            "a container": ("    timeout-minutes: 35\n", "    timeout-minutes: 35\n    container: swift:6.2.4-noble\n",
                            name + "sets a shell, defaults, a container"),
            "a post that always runs": (post, post + "        if: always()\n", name + "sets a shell, defaults"),
            "printing the private log": (post, "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/ios-core.log\"\n" + post,
                                         name + "runs something other than"),
            "an exported toolchain": ("toolchain.py swift\n", "toolchain.py swift --export\n", name + "is not the Linux MayCore run"),
            "a macOS runner": ("  core:\n    needs: admission\n    runs-on: ubuntu-24.04\n",
                               "  core:\n    needs: admission\n    runs-on: macos-15\n", name + "is not the Linux MayCore run"),
            "a cleanup that is not always run": ("        if: always()\n", "", name + "core's steps are not exactly"),
            "a pull_request trigger": ("on:\n  workflow_dispatch:\n", "on:\n  pull_request:\n  workflow_dispatch:\n",
                                       name + "is not dispatched with exactly pr_number and source_sha"),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: write\nconcurrency:",
                                 name + "permissions are not exactly read-only"),
            "one group for every pull request": ("  group: may-ios-core-pr-${{ inputs.pr_number }}\n",
                                                 "  group: may-ios-core-pr\n", name + "does not run one at a time"),
            "another lane's group": ("  group: may-ios-core-pr-${{ inputs.pr_number }}\n",
                                     "  group: may-native-compile-pr\n", name + "does not run one at a time"),
            "a store environment": ("    environment: ${{ needs.admission.outputs.environment }}\n",
                                    "    environment: may-ios-playground-internal\n", name + "core's environment is not"),
            "inputs in shell": ('ios_core.py run --source-sha "$MAY_SOURCE_SHA"',
                                'ios_core.py run --source-sha "${{ inputs.source_sha }}"', name + "runs something other than"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_pull_requests_swift_must_run_sandboxed(self):
        message = "ios_core.py: the pull request's code does not run as the sandbox's unprivileged user alone"
        held = "sandbox.py: the unprivileged user is not exactly a fresh, groupless, sealed-off user"
        cases = {
            "the build as the runner": ("tools/may_release/ios_core.py", "sandbox.step(swift(BUILD + scratch)",
                                        "sandbox.step(BUILD + scratch", message),
            "the tests as the runner": ("tools/may_release/ios_core.py", "sandbox.step(swift(TEST + scratch)",
                                        "sandbox.step(TEST + scratch", message),
            "no stop after the build": ("tools/may_release/ios_core.py", "    stop_all(log)\n    stages[\"build\"]",
                                        "    stages[\"build\"]", message),
            "the version as the runner": ("tools/may_release/ios_core.py", 'swift(["swift", "--version"])',
                                          '["swift", "--version"]', message),
            "the user in sudo": ("tools/may_release/sandbox.py", '"--shell", "/usr/sbin/nologin", USER]',
                                 '"--shell", "/usr/sbin/nologin", "-G", "sudo", USER]', held),
            "the runner's environment kept": ("tools/may_release/sandbox.py", '"/usr/bin/env", "-i",',
                                              '"/usr/bin/env",', held),
            "no cron deny": ("tools/may_release/sandbox.py", 'DENY_FILES = ("/etc/cron.deny", "/etc/at.deny")',
                             'DENY_FILES = ("/etc/at.deny",)', held),
            "one kill round": ("tools/may_release/sandbox.py", "    for _ in range(STOP_ROUNDS):",
                               "    for _ in range(1):", held),
            "no leftover sweep": ("tools/may_release/sandbox.py", "    swept = admin(leftovers(), log) == 0\n",
                                  "    swept = True\n", held),
            "a whole-root sweep": ("tools/may_release/sandbox.py", 'LEFTOVER_DIRS = ("/tmp",',
                                   'LEFTOVER_DIRS = ("/", "/tmp",', held),
            "no reach check": ("tools/may_release/sandbox.py", "    for path in (home, runner_temp):",
                               "    for path in ():", held),
        }
        for case, (path, old, new, msg) in cases.items():
            with self.subTest(case=case):
                self.flagged(path, old, new, msg, case)

    def test_printing_anything_from_may_app_is_flagged(self):
        anchor = '        print("May iOS core suite: this commit has no ios/MayCore package; failed")\n'
        for mutation in ('        print(text)\n', '        print(entries[0])\n', '        sys.stdout.write(text)\n',
                         '        raise SystemExit(text)\n', '        print(f"{manifest}")\n'):
            with self.subTest(mutation=mutation.strip()):
                self.flagged("tools/may_release/ios_core.py", anchor, mutation + anchor, "ios_core.py:", mutation)


if __name__ == "__main__":
    unittest.main()


class CompileGateTests(unittest.TestCase):
    """Coordinator, 30 Oct: may-app #1493 merged with a failing MayCore test that the iOS compile alone could not see.
    native-compile-pr.yml, which the coordinator's queue dispatches for every pull request, runs ios-core-pr.yml's core
    job exactly, so its build and tests run on every compile and a failing test fails the run (post exits 1)."""

    COMPILE = ".github/workflows/native-compile-pr.yml"

    def test_the_compile_runs_the_core_job_exactly_as_ios_core_pr_does(self):
        compile_text = (static_check.ROOT / self.COMPILE).read_text()
        own = (static_check.ROOT / WORKFLOW).read_text()
        self.assertEqual(static_check.compile_pr_workflow_problems(compile_text), [])
        core = compile_text[slice(*static_check.job_span(compile_text, "core"))]
        self.assertEqual(core.strip("\n"), own[slice(*static_check.job_span(own, "core"))].strip("\n"))
        self.assertIn('ios_core.py run --source-sha "$MAY_SOURCE_SHA"', core)
        self.assertIn('ios_core.py post --source-sha "$MAY_SOURCE_SHA" --pr-number "$MAY_PR_NUMBER"', core)

    def test_a_core_job_that_differs_from_ios_core_prs_or_is_missing_is_refused(self):
        text = (static_check.ROOT / self.COMPILE).read_text()
        span = static_check.job_span(text, "core")
        for label, changed in (
                ("dropped", text[:span[0]] + text[span[1]:]),
                ("no post", text.replace("      - name: Post the MayCore failures on the may-app pull request, never in "
                                         "this log\n", "      - name: Skipped\n", 1)),
                ("no tests", text.replace('ios_core.py run --source-sha "$MAY_SOURCE_SHA"', 'ios_core.py post --source-sha "$MAY_SOURCE_SHA"', 1)),
                ("longer", text.replace("    timeout-minutes: 35\n", "    timeout-minutes: 90\n", 1))):
            with self.subTest(label=label):
                self.assertNotEqual(changed, text)
                self.assertTrue(any("core job" in problem
                                    for problem in static_check.compile_pr_workflow_problems(changed)),
                                static_check.compile_pr_workflow_problems(changed))
