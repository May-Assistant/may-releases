"""The push-tap spike (ios_push_tap.py): may-app's fixture list, the hook check, each fixture's verdict, the run.
The simulator, May's process, its log and git are faked."""

import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import device_tests
import ios_push_tap
import publish

UDID = "SIM-1"
TOKEN = "may_session_secret_value"
TRUE, FALSE = b"delivered completion_on_main=true", b"delivered completion_on_main=false"


class Done:
    def __init__(self, code=0):
        self.returncode = code


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.private, self.folder = self.root / "may-source", self.root / "may-private", self.root / "ios"
        (self.source / "ios").mkdir(parents=True)
        self.private.mkdir()
        self.folder.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def listed(self, text):
        (self.source / ios_push_tap.LIST).write_bytes(text.encode("utf-8"))


class FixtureListTests(Workspace):
    def test_the_list_is_one_name_a_line_with_comments_and_blank_lines_skipped(self):
        self.listed("# may-app's header\n" * 40 + "\nnothing\nchat\n")
        self.assertEqual(ios_push_tap.fixtures(self.source), ("listed", ["nothing", "chat"]),
                         "a long comment header still fits")
        self.listed("1x\n" + "a" * 32 + "\n")
        self.assertEqual(ios_push_tap.fixtures(self.source), ("listed", ["1x", "a" * 32]), "may-app's rule: 1 to 32")
        self.listed("# N's fixtures\nnothing\n\n  chat-thread  \ncomputer-run\n")
        self.assertEqual(ios_push_tap.fixtures(self.source), ("listed", ["nothing", "chat-thread", "computer-run"]))

    def test_no_list_is_missing(self):
        self.assertEqual(ios_push_tap.fixtures(self.source), ("missing", []))

    def test_anything_else_is_malformed(self):
        for case, text in (("empty", "# nothing yet\n\n"), ("a space", "chat thread\n"), ("upper case", "Chat\n"),
                           ("a payload", '{"aps": {}}\n'), ("a path", "../chat\n"), ("twice", "chat\nchat\n"),
                           ("a leading hyphen", "-chat\n"), ("too long a name", "a" * 33 + "\n"),
                           ("too many", "".join(f"f{n}\n" for n in range(ios_push_tap.MAX_FIXTURES + 1))),
                           ("too long a list", "chat\n" + "#" * ios_push_tap.MAX_LIST + "\n"), ("not ASCII", "chaté\n")):
            with self.subTest(case=case):
                self.listed(text)
                self.assertEqual(ios_push_tap.fixtures(self.source), ("malformed", []))
        (self.source / ios_push_tap.LIST).unlink()
        (self.root / "elsewhere.txt").write_text("chat\n")
        (self.source / ios_push_tap.LIST).symlink_to(self.root / "elsewhere.txt")
        self.assertEqual(ios_push_tap.fixtures(self.source), ("malformed", []), "a link is never read")
        (self.source / ios_push_tap.LIST).unlink()
        (self.source / ios_push_tap.LIST).mkdir()
        self.assertEqual(ios_push_tap.fixtures(self.source), ("malformed", []))


class HookTests(Workspace):
    def test_the_checkout_must_hold_may_apps_hook_commit(self):
        seen = []

        def git(argv, **kwargs):
            seen.append(argv)
            return Done(0)
        with patch("subprocess.run", git):
            self.assertTrue(ios_push_tap.hooked(self.source))
        self.assertEqual(seen[0][-4:], ["merge-base", "--is-ancestor", ios_push_tap.HOOK, "HEAD"])
        self.assertEqual(seen[0][seen[0].index("-C") + 1], str(self.source))
        self.assertRegex(ios_push_tap.HOOK, r"\A[0-9a-f]{40}\Z", "the exact commit, never a short one")
        with patch("subprocess.run", lambda argv, **kwargs: Done(1)):
            self.assertFalse(ios_push_tap.hooked(self.source), "an older commit")

        def fails(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)
        with patch("subprocess.run", fails):
            self.assertFalse(ios_push_tap.hooked(self.source))


class VerdictTests(unittest.TestCase):
    def test_the_newest_line_for_the_fixture_is_what_the_hook_said(self):
        lines = (b"12:00 MayPlayground: [io.github.mayassistant.may.playground:push-tap-hook] push-tap fixture=chat "
                 b"unanswered\n12:01 push-tap fixture=brief " + TRUE + b"\n12:02 push-tap fixture=chat " + FALSE + b"\n")
        self.assertEqual(ios_push_tap.said(lines, "chat"), FALSE.decode())
        self.assertEqual(ios_push_tap.said(lines, "brief"), TRUE.decode())
        self.assertIsNone(ios_push_tap.said(lines, "place"), "another fixture's line is never this one's")
        self.assertEqual(ios_push_tap.said(b"push-tap fixture=- unknown\n", "place"), "unknown")
        self.assertIsNone(ios_push_tap.said(b"push-tap fixture=chat delivered completion_on_main=maybe\n", "chat"))

    def test_each_outcome(self):
        judged = ios_push_tap.judged
        self.assertEqual(judged(TRUE.decode(), alive=True, crashed=False), "pass")
        self.assertEqual(judged(FALSE.decode(), alive=True, crashed=False), "fail", "#1577 regressed")
        self.assertEqual(judged("unanswered", alive=True, crashed=False), "fail")
        self.assertEqual(judged(TRUE.decode(), alive=False, crashed=False), "fail", "May exited after the tap")
        self.assertEqual(judged(TRUE.decode(), alive=True, crashed=True), "fail", "a crash report came in")
        self.assertEqual(judged(None, alive=False, crashed=True), "fail", "a crash with no line is still a crash")
        for words in (None, "unbuildable", "unknown"):
            self.assertEqual(judged(words, alive=True, crashed=False), "infra", words)
        self.assertEqual(judged(None, alive=False, crashed=False, launched=False), "infra")

    def test_a_run_fails_on_any_failure_or_when_nothing_passed_or_failed(self):
        fails = ios_push_tap.verdicts_fail
        self.assertFalse(fails(["pass", "pass"]))
        self.assertFalse(fails(["pass", "infra"]))
        self.assertTrue(fails(["pass", "fail"]))
        self.assertTrue(fails(["infra", "infra"]))
        self.assertTrue(fails([]), "no fixture ran")


class FakeDiagnostics:
    def __init__(self, reports):
        self.reports = reports
        self.kept = []

    def crash_reports(self):
        return sorted(self.reports)

    def keep(self, folder, scenario, token):
        self.kept.append((scenario, token))
        (folder / (scenario + device_tests.DIAG)).write_bytes(b"== crash reports ==\n")


class RunTests(Workspace):
    def tap(self, said=None, pids=None, crash=(), launch_fails=(), hooked=True, fixtures="nothing\nchat\nbrief\n"):
        """said: fixture -> the hook's words (missing: no line); pids: fixture -> (pid after launch, pid after)."""
        said = {"nothing": TRUE, "chat": TRUE, "brief": TRUE} if said is None else said
        pids = pids or {}
        if fixtures is not None:
            self.listed(fixtures)
        self.launches, self.reads, self.now = [], [], [0.0]
        reports = set()
        diagnostics = FakeDiagnostics(reports)
        current = {}

        def launch(argv, log, env=None, timeout=None):
            if argv[:3] == ["xcrun", "simctl", "terminate"]:
                return Done(0)
            name = env["SIMCTL_CHILD_MAY_TEST_PUSH_TAP"]
            current["name"] = name
            self.launches.append((argv, env, timeout))
            return Done(1 if name in launch_fails else 0)

        calls = {}

        def pid(udid, env):
            name = current["name"]
            calls[name] = calls.get(name, 0) + 1
            return pids.get(name, (100, 100))[min(calls[name], 2) - 1]

        def lines(udid, env, start):
            self.reads.append(current["name"])
            words = said.get(current["name"])
            line = b"" if words is None else (f"12:00:00 MayPlayground[100] [push-tap-hook] push-tap fixture="
                                              f"{current['name']} ".encode() + words + b"\n")
            return line + b"12:00:00 session " + TOKEN.encode() + b"\n12:00:01 another line May logged\n"

        def came(seen):
            if current["name"] in crash:
                path = self.root / f"MayPlayground-{current['name']}.ips"
                reports.add(path)
                path.write_bytes(b"{}\n{}")
                return [path] if path not in seen else []
            return []

        def sleep(seconds):
            self.now[0] += seconds

        self.shots = []

        def shoot(udid, env, path, log):
            self.shots.append((current["name"], path.name))
            path.write_bytes(b"\x89PNG fake")
            return path.name

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("ios_push_tap.hooked", lambda source: hooked):
            verdicts = ios_push_tap.tap_fixtures(UDID, {"DEVELOPER_DIR": "/X"}, self.source, self.private, self.folder,
                                                 self.private / "log", TOKEN, diagnostics, pid, came,
                                                 lambda data: b"frame 0 main (in MayPlayground) (App.swift:7)\n",
                                                 launch=launch, lines=lines, clock=lambda: self.now[0], sleep=sleep,
                                                 shoot=shoot)
        self.diagnostics = diagnostics
        return verdicts, out.getvalue()

    def diag(self):
        path = self.folder / (ios_push_tap.DIAG + device_tests.DIAG)
        return path.read_bytes() if path.is_file() else None

    def failed(self):
        return (self.private / ios_push_tap.FAILED).exists()

    SAY = "May device tests (ios): push tap: "

    def test_each_fixture_cold_launches_once_with_its_name_and_the_session_and_all_passing_passes(self):
        verdicts, out = self.tap()
        self.assertEqual(verdicts, ["pass", "pass", "pass"])
        self.assertEqual([env["SIMCTL_CHILD_MAY_TEST_PUSH_TAP"] for _, env, _ in self.launches],
                         ["nothing", "chat", "brief"])
        for argv, env, timeout in self.launches:
            self.assertEqual(argv, ["xcrun", "simctl", "launch", "--terminate-running-process", UDID,
                                    device_tests.APP_ID], "a cold launch: never a payload or an argument")
            self.assertEqual(env["SIMCTL_CHILD_MAY_TEST_SESSION"], TOKEN)
            self.assertEqual(timeout, ios_push_tap.LAUNCH_SECONDS)
        self.assertEqual(out.splitlines(), [self.SAY + "3 fixtures listed", self.SAY + "3 passed, 0 failed, 0 infra",
                                            self.SAY + "the spike passed"])
        self.assertFalse(self.failed())

    def test_the_public_log_never_names_a_fixture_or_says_what_may_logged(self):
        verdicts, out = self.tap(said={"nothing": TRUE, "chat": FALSE}, crash=("brief",))
        for word in ("nothing", "chat", "brief", "completion_on_main", "fixture=", TOKEN):
            self.assertNotIn(word, out)

    def test_the_private_diag_holds_each_fixtures_name_verdict_and_the_hooks_line_without_the_session(self):
        self.tap(said={"nothing": TRUE, "chat": FALSE}, crash=("brief",))
        diag = self.diag()
        self.assertIn(b"== push tap, per fixture ==\n", diag)
        self.assertIn(b"nothing: pass; launched yes, hook said delivered completion_on_main=true, May running yes, "
                      b"crash report no, screen kept\n", diag)
        self.assertIn(b"chat: fail; launched yes, hook said delivered completion_on_main=false", diag)
        self.assertIn(b"push-tap fixture=chat delivered completion_on_main=false\n", diag)
        self.assertIn(b"brief: fail; launched yes, hook said nothing, May running yes, crash report yes, screen kept\n",
                      diag)
        self.assertIn(b"== May's frames, file:line ==\nframe 0 main", diag)
        self.assertNotIn(TOKEN.encode(), diag)
        self.assertNotIn(b"another line May logged", diag, "only the hook's own lines are kept")
        self.assertTrue(diag.startswith(b"== crash reports =="), "the crash reports, as every diag keeps them")
        self.assertEqual(self.diagnostics.kept, [(ios_push_tap.DIAG, TOKEN)])
        self.assertTrue(publish.DEVICE_FILE.fullmatch(ios_push_tap.DIAG + device_tests.DIAG), "a name publish takes")
        self.assertTrue(publish.diagnostics_ok(diag), "publish keeps it")

    def test_each_launched_fixture_keeps_its_end_screen_in_the_private_folder_alone(self):
        """Coordinator, 3 Nov (run 41632669712): the hook says delivered, never which screen the route opened."""
        verdicts, out = self.tap(launch_fails=("chat",))
        self.assertEqual(self.shots, [("nothing", "push-tap-nothing-end.png"), ("brief", "push-tap-brief-end.png")],
                         "after each launched fixture's settle; a launch that failed has no screen")
        for _, name in self.shots:
            self.assertTrue((self.folder / name).is_file())
            self.assertTrue(publish.DEVICE_FILE.fullmatch(name), "a name publish takes")
            self.assertNotIn(name, out)
        self.assertIn(b"chat: infra; launched no, hook said nothing, May running yes, crash report no, screen not kept",
                      self.diag())
        self.assertTrue(publish.DEVICE_FILE.fullmatch(ios_push_tap.SHOT.format("a" * 32)), "the longest name fits")
        self.assertNotIn("screen", out, "the log is unchanged")

    def test_a_failure_fails_the_run(self):
        for case, kwargs in (("off the main thread", {"said": {"nothing": TRUE, "chat": FALSE, "brief": TRUE}}),
                             ("unanswered", {"said": {"nothing": TRUE, "chat": b"unanswered", "brief": TRUE}}),
                             ("a crash", {"crash": ("chat",)}),
                             ("May exited", {"pids": {"chat": (100, None)}}),
                             ("another process", {"pids": {"chat": (100, 101)}})):
            with self.subTest(case=case):
                (self.private / ios_push_tap.FAILED).unlink(missing_ok=True)
                verdicts, out = self.tap(**kwargs)
                self.assertEqual(verdicts, ["pass", "fail", "pass"])
                self.assertIn(self.SAY + "2 passed, 1 failed, 0 infra", out)
                self.assertIn(self.SAY + "the spike failed", out)
                self.assertTrue(self.failed())

    def test_no_line_within_the_wait_is_infra_and_the_wait_is_bounded(self):
        verdicts, out = self.tap(said={"nothing": TRUE, "brief": b"unbuildable"})
        self.assertEqual(verdicts, ["pass", "infra", "infra"])
        reads = self.reads.count("chat")
        self.assertLessEqual(reads, ios_push_tap.WAIT_SECONDS // ios_push_tap.POLL_SECONDS + 1, "polled, then given up")
        self.assertGreater(reads, 1, "polled until the wait ran out, not read once")
        self.assertEqual(self.reads.count("nothing"), 1, "a line that came is read once")
        self.assertFalse(self.failed(), "a pass among infra passes")

    def test_a_run_that_is_all_infra_fails(self):
        verdicts, out = self.tap(said={}, launch_fails=("brief",))
        self.assertEqual(verdicts, ["infra", "infra", "infra"])
        self.assertNotIn("brief", self.reads, "a launch that failed is never read")
        self.assertIn(self.SAY + "0 passed, 0 failed, 3 infra", out)
        self.assertTrue(self.failed())

    def test_no_list_a_malformed_one_or_an_older_commit_runs_nothing_and_fails(self):
        for case, kwargs, line in (
                ("no list", {"fixtures": None}, "may-app has no push-tap fixture list, so no fixture ran (no fixtures)"),
                ("malformed", {"fixtures": "chat thread\n"}, "may-app's push-tap fixture list is malformed, so no "
                                                             "fixture ran"),
                ("older", {"hooked": False}, "the may-app commit does not hold the push-tap hook, so no fixture ran")):
            with self.subTest(case=case):
                (self.source / ios_push_tap.LIST).unlink(missing_ok=True)
                (self.private / ios_push_tap.FAILED).unlink(missing_ok=True)
                verdicts, out = self.tap(**kwargs)
                self.assertEqual(verdicts, [])
                self.assertEqual(self.launches, [])
                self.assertEqual(out.splitlines(), [self.SAY + line, self.SAY + "the spike failed"])
                self.assertTrue(self.failed())

    def test_a_marker_from_an_earlier_run_never_stays(self):
        (self.private / ios_push_tap.FAILED).write_bytes(b"true")
        self.tap()
        self.assertFalse(self.failed())

    def test_the_verdict_step_fails_the_job_on_the_marker(self):
        (self.private / device_tests.VERDICTS).write_text("[]")
        with patch("device_tests.runner_root", lambda: self.root), patch("device_tests.require_step_secrets"):
            device_tests.verdict("ios")
            (self.private / ios_push_tap.FAILED).write_bytes(b"true")
            with self.assertRaises(device_tests.DeviceTestsError) as raised:
                device_tests.verdict("ios")
        self.assertEqual(str(raised.exception), "The push-tap spike failed")


if __name__ == "__main__":
    unittest.main()
