"""The harness smoke scenario (device_tests.py, device_ios.py, publish.py): signed out, with no staging and no sign-in
secret; the harness's own Maestro flow, one read-only agent run and its judge; "smoke-pass" or "fail", never "pass".
The emulator, the simulator, Maestro, Claude Code and staging are all faked."""

import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import device_agent
import device_ios
import device_tests
from device_tests import DeviceTestsError
import publish
from test_device_agent import MODEL, OAUTH, record
from test_device_ios import Done, FakeRecorder as FakeIosRecorder, UDID
from test_device_tests import network_tripwire, FLOW, STDIN, FakeRecorder, MP4, RECIPIENT, RUN, SHA, TOKEN, device_tar
from test_renders import png


TRIPWIRE = network_tripwire()


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()

NO_STAGING = {"MAY_TEST_SIGNIN_SECRET": None, "MAY_STAGING_API_URL": None}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/app/build/outputs/apk/playground/debug").mkdir(parents=True)
        (self.source / device_tests.APK).write_bytes(b"apk")
        (self.source / device_tests.FLOWS_DIR).mkdir(parents=True)
        (self.source / device_tests.FLOWS_DIR / "sign-in.yaml").write_text(FLOW)
        app = self.root / device_ios.APP
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(b"plist")
        self.private.mkdir()
        self.env = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
                    "MAY_DEVICE_TESTS_MODEL": MODEL, "GITHUB_RUN_ID": "39557415738"}

    def tearDown(self):
        self.directory.cleanup()

    def environ(self, env):
        environ = dict(self.env, **(env or {}))
        return patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True)

    def mint(self, base, secret):
        self.minted.append(secret)
        return TOKEN

    def agent(self, records):
        def run(cli, goal, tag, device, work, token, model, seconds=device_agent.AGENT_SECONDS):
            self.goals.append((goal, token, model, device["MAY_DEVICE_PLATFORM"]))
            return records.pop(0)
        return run

    def judge(self, verdicts):
        def judged(cli, goal, tag, agent_record, work, token, model):
            self.judged.append((goal, token, model))
            return verdicts.pop(0)
        return judged

    def android(self, selection="smoke", env=None, records=None, verdicts=None, maestro_fails=False,
                checked=False):
        self.calls, self.minted, self.goals, self.judged, self.flows = [], [], [], [], []

        class Process:
            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            self.calls.append(list(argv))
            if argv[0] == "/fake/maestro":
                self.flows.append(Path(argv[4]).read_text())
                return 1 if maestro_fails else 0
            return 0

        def screenshot(adb, env, path, log):
            path.write_bytes(png())
            return path.name

        with self.environ(env), patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.network_ready", return_value=4), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.adb_shell", side_effect=lambda adb, env, log, line: self.calls.append(
                    [STDIN, *line.split()]) or 0), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.run_scenarios(selection, minted=self.mint, checked=lambda *a: 200,
                                       revoked_by=lambda base, token: True,
                                       truth=lambda *a: (200, True), asked=lambda *a: (200, True),
                                       cleaned=lambda *a: self.fail("the smoke scenario cleaned up"),
                                       agent=self.agent(records or [record()]),
                                       install=lambda root: root / "may-claude" / "claude")
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
            device_tests.judge_scenarios(judged_by=self.judge(verdicts or [("pass", "")]))
            if checked:
                device_tests.verdict()
        runs = list((self.root / device_tests.OUT).iterdir())
        return json.loads((runs[0] / "android" / "report.json").read_text()), out.getvalue()


class AndroidSmokeTests(Workspace):
    def test_runs_signed_out_without_staging_and_passes_as_smoke_alone(self):
        try:
            report, out = self.android(env=NO_STAGING, checked=True)
        except DeviceTestsError as error:
            self.fail("the smoke scenario needed staging: " + error.args[0])
        self.assertEqual(self.minted, [], "no test session is minted")
        start = [argv for argv in self.calls if "start" in argv][0]
        self.assertNotIn("may_test_session", start, "the app starts signed out")
        self.assertEqual(self.flows, [device_tests.SMOKE_FLOW], "Maestro runs the harness's own flow")
        self.assertIn("appId: " + device_tests.APP_ID, self.flows[0])
        smoke = device_tests.scenarios("smoke")[0]
        self.assertEqual(self.goals, [(smoke["goal"], OAUTH, MODEL, "android")])
        self.assertEqual(self.judged, [(smoke["goal"], OAUTH, MODEL)])
        [row] = report["scenarios"]
        self.assertEqual((row["id"], row["verdict"], row.get("reason"), row["videos"], row["screenshots"]),
                         ("smoke", "smoke-pass", None, ["smoke.mp4"], ["smoke-end.png"]))
        self.assertEqual(out.splitlines(), [
            "May device tests (android): 1 scenarios ran; verdicts follow the judge step",
            "May device tests (android): smoke: smoke-pass",
            "May device tests (android): 0 passed, 0 passed on screen alone (server checks not configured), 0 failed, "
            "0 skipped",
            "May device tests (android): 1 passed the harness smoke check (not a test of May)",
            "May device tests (android): no scenario failed"])

    def test_maestro_the_agent_or_the_judge_failing_fails_it(self):
        for kwargs, expected in (
                ({"maestro_fails": True}, ("fail", device_tests.SMOKE_MAESTRO)),
                ({"records": [record(outcome=None, state="timeout")]}, ("fail", device_tests.UNFINISHED)),
                ({"verdicts": [("fail", "no")]}, ("fail", None)),
                ({"records": [record(state="rate_limited", outcome=None)]}, ("skipped", device_tests.RATE_LIMITED))):
            with self.subTest(kwargs=list(kwargs)):
                shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
                for name in (device_tests.VERDICTS, device_tests.AGENTS):
                    (self.private / name).unlink(missing_ok=True)
                try:
                    report, _ = self.android(env=NO_STAGING, **kwargs)
                except DeviceTestsError as error:
                    self.fail("the smoke scenario needed staging: " + error.args[0])
                row = report["scenarios"][0]
                self.assertEqual((row["verdict"], row.get("reason")), expected)

    def test_all_leaves_it_out_and_any_other_scenario_still_needs_staging(self):
        self.assertNotIn("smoke", [row["id"] for row in device_tests.scenarios()])
        self.assertNotIn("smoke", [row["id"] for row in device_tests.scenarios("all", "ios")])
        self.assertEqual([row["id"] for row in device_tests.smoke_rows("ios")], ["smoke"])
        with self.assertRaises(DeviceTestsError) as refused:
            self.android(selection="smoke,sign-in", env=NO_STAGING)
        self.assertIn("MAY_STAGING_API_URL is not https://staging-api.usemay.co", refused.exception.args[0])

    def test_malformed_smoke_rows_are_refused(self):
        good = device_tests.scenarios("smoke")[0]
        for change in ({"flow": "smoke.yaml"}, {"truth": []}, {"server": [{"get": "/v1/me", "expect": 200}]},
                       {"goal": "x" * 1501}, {"goal": None}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "s.json"
                path.write_text(json.dumps([dict(good, **change)]))
                with patch("device_tests.SCENARIOS", path), self.assertRaises(DeviceTestsError):
                    device_tests.scenarios("smoke")


class IosSmokeTests(Workspace):
    def test_runs_signed_out_on_the_simulator_without_staging(self):
        self.minted, self.goals, self.judged, calls, flows = [], [], [], [], []

        def quiet(argv, log, cwd=None, env=None, capture=False, timeout=None):
            calls.append((list(argv), dict(env or {})))
            if argv[:4] == ["xcrun", "simctl", "io", UDID] and argv[4] == "screenshot":
                Path(argv[-1]).write_bytes(png())
            if argv[0] == "/fake/maestro":
                flows.append(Path(argv[4]).read_text())
            return Done(0)

        with self.environ(NO_STAGING), patch("renders.quiet", side_effect=quiet), \
                patch("renders.simulator", return_value=UDID), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.head_revision", return_value=SHA), patch("device_ios.Recorder", FakeIosRecorder), \
                patch("sys.stdout", new_callable=io.StringIO):
            try:
                device_ios.run_scenarios("smoke", minted=self.mint, cleaned=lambda *a: self.fail("cleaned up"),
                                         agent=self.agent([record()]),
                                         install=lambda root: root / "may-claude" / "claude")
            except DeviceTestsError as error:
                self.fail("the smoke scenario needed staging: " + error.args[0])
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
            device_tests.judge_scenarios(judged_by=self.judge([("pass", "")]), platform="ios")
        launches = [env for argv, env in calls if argv[:3] == ["xcrun", "simctl", "launch"]]
        self.assertEqual(len(launches), 1)
        self.assertNotIn("SIMCTL_CHILD_MAY_TEST_SESSION", launches[0], "the app starts signed out")
        self.assertEqual(self.minted, [])
        self.assertEqual(flows, [device_tests.SMOKE_FLOW])
        self.assertEqual([platform for *_, platform in self.goals], ["ios"])
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "ios" / "report.json").read_text())
        self.assertEqual([(row["id"], row["verdict"]) for row in report["scenarios"]], [("smoke", "smoke-pass")])


class PublishSmokeTests(unittest.TestCase):
    def test_the_report_accepts_smoke_words_and_titles(self):
        titles = {"smoke": device_tests.smoke_rows("android")[0]["title"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for verdict, reason in (("smoke-pass", None), ("fail", device_tests.SMOKE_MAESTRO)):
                row = {"id": "smoke", "verdict": verdict, "videos": [], "screenshots": [], "server": []}
                if reason:
                    row["reason"] = reason
                path.write_text(json.dumps({"device": "android", "scenarios": [row]}))
                with self.subTest(verdict=verdict):
                    device, [checked] = publish.device_report(path, set(), titles)
                    self.assertEqual((device, checked["verdict"], checked["reason"]), ("android", verdict, reason))

    def test_a_smoke_report_publishes_with_its_title(self):
        report = {"run": RUN, "device": "android", "scenarios": [
            {"id": "smoke", "verdict": "smoke-pass", "videos": ["smoke.mp4"], "screenshots": ["smoke-end.png"],
             "server": []}]}
        entries = [(RUN, None), (RUN + "/android", None), (RUN + "/android/report.json", json.dumps(report).encode()),
                   (RUN + "/android/smoke.mp4", MP4), (RUN + "/android/smoke-end.png", png())]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / publish.DEVICE_DOWNLOAD).mkdir()
            (root / publish.DEVICE_DOWNLOAD / "android.age").write_bytes(b"sealed")

            def decrypt(sealed, plain, identity, recipient, private):
                plain.write_bytes(device_tar(entries))

            with patch.dict(os.environ, {"RUNNER_TEMP": str(root), "MAY_RENDERS_DECRYPT_KEY": "AGE-SECRET-KEY-1" +
                                         "Q" * 58, "MAY_RENDERS_RECIPIENT": RECIPIENT, "GITHUB_RUN_ID": "9",
                                         "GITHUB_RUN_ATTEMPT": "1"}, clear=True), \
                    patch("publish.decrypt", side_effect=decrypt), patch("sys.stdout", new_callable=io.StringIO):
                try:
                    publish.open_device()
                except publish.PublishError as error:
                    self.fail("the smoke report was refused: " + error.args[0])
            readme = (root / publish.TREE / RUN / "README.md").read_text()
        self.assertIn("## smoke: " + device_tests.smoke_rows("android")[0]["title"], readme)
        self.assertIn("**android: smoke-pass**", readme)

if __name__ == "__main__":
    unittest.main()
