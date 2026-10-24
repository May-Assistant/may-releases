"""The iOS device job (device_ios.py, device-tests PR C): build, run, record and the platform-aware judge, seal and
verdict. Xcode, the simulator, Maestro and staging are all faked."""

import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import device_agent
import device_ios
import device_tests
from device_tests import DeviceTestsError
import renders
from test_device_tests import MP4, SHA, TOKEN
from test_renders import png

UDID = "0A1B2C3D-0000-4000-8000-000000000001"


def ticking_budget():
    """A budget of one Maestro flow on a clock that has used it up by the second scenario."""
    ticks = iter([0, 0, device_tests.FLOW_SECONDS])
    return device_tests.Budget(total=device_tests.FLOW_SECONDS + 1, clock=lambda: next(ticks))


class Done:
    def __init__(self, code=0):
        self.returncode, self.stdout = code, b""


class Workspace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "ios").mkdir(parents=True)
        (self.source / device_tests.FLOWS_DIR).mkdir(parents=True)
        (self.source / device_tests.FLOWS_DIR / "sign-in.yaml").write_text("appId: x\n")
        self.private.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def built(self):
        app = self.root / device_ios.APP
        app.mkdir(parents=True, exist_ok=True)
        (app / "Info.plist").write_bytes(b"plist")


class BuildTests(Workspace):
    def build(self, products=True, fail=False):
        calls = []

        def tool(argv, log, cwd=None, env=None):
            calls.append((argv, cwd))
            if fail:
                raise renders.RendersError("x")
            if products and argv[0] == "xcodebuild":
                self.built()

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin"}, clear=True), \
                patch("renders.xcode_env", return_value=({"DEVELOPER_DIR": "/X"}, Path("/x/xcodegen"))), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_ios.build(tool=tool)
        return calls, out.getvalue()

    def test_builds_a_debug_playground_for_the_simulator_signed_ad_hoc(self):
        calls, out = self.build()
        self.assertEqual(calls[0], (["/x/xcodegen", "generate"], self.source / "ios"))
        xcodebuild = calls[1][0]
        for flag, value in (("-scheme", "MayPlayground"), ("-configuration", "Debug"),
                            ("-destination", "generic/platform=iOS Simulator")):
            self.assertEqual(xcodebuild[xcodebuild.index(flag) + 1], value)
        # Signed ad hoc so the Keychain works (run 40035089599: unsigned, it refused the test session).
        self.assertEqual(xcodebuild[-4:], ["CODE_SIGN_IDENTITY=-", "CODE_SIGN_STYLE=Manual", "DEVELOPMENT_TEAM=",
                                           "build"])
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", xcodebuild)
        self.assertEqual(out.strip(), "May device tests: Playground debug app built for the iOS simulator")

    def test_a_failed_or_missing_build_stops(self):
        for kwargs in ({"fail": True}, {"products": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DeviceTestsError):
                self.build(**kwargs)

    def test_another_steps_secret_is_refused(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s"}, clear=True), \
                self.assertRaises(Exception) as refused:
            device_ios.build(tool=lambda *a, **k: None)
        self.assertIn("A secret for another release step", refused.exception.args[0])


class FakeRecorder:
    dropped = kept = None

    def __init__(self, udid, env, scenario, private, log):
        self.scenario = scenario

    def start(self):
        pass

    def stop(self, folder):
        (folder / (self.scenario + ".mp4")).write_bytes(MP4)
        return [self.scenario + ".mp4"]


class RunTests(Workspace):
    def run_it(self, selection="sign-in,connections", env=None, maestro_fails=False, agent=None, budgeted=None):
        calls, minted, self.maestro_timeouts, self.delivered = [], [], [], []
        self.built()
        shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
        (self.private / device_tests.VERDICTS).unlink(missing_ok=True)

        def quiet(argv, log, cwd=None, env=None, capture=False, timeout=None):
            calls.append((list(argv), dict(env or {})))
            if "test" in argv and "--device" in argv:
                self.maestro_timeouts.append(timeout)
            if argv[:4] == ["xcrun", "simctl", "io", UDID] and argv[4] == "screenshot":
                Path(argv[-1]).write_bytes(png())
            return Done(1 if maestro_fails and "test" in argv and "--device" in argv else 0)

        def mint(base, secret):
            minted.append(secret)
            return TOKEN + str(len(minted))

        environ = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                   "MAY_STAGING_API_URL": "https://staging-api.usemay.co", "PATH": "/usr/bin", **(env or {})}
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("renders.quiet", side_effect=quiet), patch("renders.simulator", return_value=UDID), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.head_revision", return_value=SHA), patch("device_ios.Recorder", FakeRecorder), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_ios.run_scenarios(selection, minted=mint, checked=lambda base, token, path: 200,
                                     truth=lambda base, token, path, tag: (200, True),
                                     asked=lambda base, token, state, tag: (200, True),
                                     cleaned=lambda base, token, since, tag: 2,
                                     agent=agent or (lambda *a, **k: self.fail("an agent ran")),
                                     install=lambda root: root / "may-claude" / "claude", budgeted=budgeted,
                                     delivered=lambda base, token, tag: self.delivered.append(token) or
                                     [device_tests.NONE_STORED])
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
            if "CLAUDE_CODE_OAUTH_TOKEN" in environ:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = environ["CLAUDE_CODE_OAUTH_TOKEN"]
            device_tests.judge_scenarios(judged_by=lambda *a: ("pass", "fine"), platform="ios")
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "ios" / "report.json").read_text())
        return calls, minted, report, out.getvalue(), runs[0]

    def test_the_session_goes_in_the_launch_environment_alone_and_fresh_per_scenario(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text("appId: x\n")
        calls, minted, report, out, run = self.run_it()
        launches = [(argv, env) for argv, env in calls if argv[:3] == ["xcrun", "simctl", "launch"]]
        # may-app #1194's in-app touch overlay, as a launch argument (Samuel, 24 Oct); the session stays out of argv.
        self.assertEqual([argv for argv, _ in launches],
                         [["xcrun", "simctl", "launch", UDID, device_ios.BUNDLE, "-MayShowTouches", "YES"]] * 2)
        self.assertEqual([env.get("SIMCTL_CHILD_MAY_TEST_SESSION") for _, env in launches], [TOKEN + "1", TOKEN + "2"])
        self.assertTrue(all(TOKEN not in " ".join(argv) for argv, _ in calls), "the token is never an argument")
        self.assertEqual(minted, ["s" * 32] * 2)
        self.assertIn(["xcrun", "simctl", "install", UDID, str(self.root / device_ios.APP)], [a for a, _ in calls])
        tests = [argv for argv, _ in calls if argv[0] == "/fake/maestro"]
        self.assertEqual([argv[:4] for argv in tests], [["/fake/maestro", "--device", UDID, "test"]] * 2)
        # Run 40051935128: each flow gets its own scenario's session in Maestro's environment, for ${MAESTRO_...}.
        self.assertEqual([env.get(device_tests.MAESTRO_SESSION) for argv, env in calls if argv[0] == "/fake/maestro"],
                         [TOKEN + "1", TOKEN + "2"])
        self.assertEqual([row["id"] for row in report["scenarios"]], ["sign-in", "connections"],
                         "Android's agent scenarios are not run here")
        self.assertEqual(report["device"], "ios")
        self.assertEqual([(row["verdict"], row["videos"], row["screenshots"]) for row in report["scenarios"]],
                         [("ui-pass", ["sign-in.mp4"], ["sign-in-end.png"]),
                          ("ui-pass", ["connections.mp4"], ["connections-end.png"])])
        self.assertEqual(out.splitlines(), [
            "May device tests (ios): 2 scenarios ran; verdicts follow the judge step",
            "May device tests (ios): sign-in: ui-pass", "May device tests (ios): connections: ui-pass",
            "May device tests (ios): 0 passed, 2 passed on screen alone (server checks not configured), 0 failed, "
            "0 skipped"])
        self.assertNotIn(TOKEN, out + (run / "ios" / "report.json").read_text())
        self.assertIn((["xcrun", "simctl", "shutdown", UDID]), [a for a, _ in calls])

    def test_maestro_is_bounded_and_the_budget_skips_what_cannot_fit_on_ios_too(self):
        calls, minted, report, out, _ = self.run_it(
            budgeted=ticking_budget)
        self.assertEqual(self.maestro_timeouts, [device_tests.FLOW_SECONDS], "a hung flow no longer runs unbounded")
        self.assertEqual([(row["id"], row["verdict"]) for row in report["scenarios"]],
                         [("sign-in", "ui-pass"), ("connections", "skipped")])
        self.assertEqual(report["scenarios"][1]["reason"], device_tests.BUDGET)
        self.assertEqual(len(minted), 1, "no session for a scenario that never started")

    def test_a_missing_flow_or_failing_maestro_fails(self):
        _, _, report, _, _ = self.run_it(maestro_fails=True)
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("fail", None), ("fail", "may-app has no Maestro flow for this scenario")])

    def test_a_switched_off_scenario_is_skipped(self):
        calls, minted, report, _, _ = self.run_it(env={"MAY_DEVICE_TESTS_SKIP": "sign-in"})
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]][0],
                         ("sign-in", "skipped", device_tests.SWITCHED_OFF))
        self.assertEqual(len(minted), 0, "connections has no flow, sign-in is switched off: no session minted")

    def test_agent_scenarios_drive_the_simulator_through_maestro(self):
        seen = []

        def agent(cli, goal, tag, device, work, token, model, seconds):
            seen.append((device, token, model, seconds, tag))
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        calls, minted, report, out, _ = self.run_it(
            selection="chat,poll", agent=agent,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5",
                 "GITHUB_RUN_ID": "39557415738"})
        device, token, model, seconds, tag = seen[0]
        self.assertEqual({key: device[key] for key in ("MAY_DEVICE_PLATFORM", "MAY_DEVICE_UDID", "MAY_DEVICE_MAESTRO",
                                                        "MAY_DEVICE_BUNDLE")},
                         {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": UDID, "MAY_DEVICE_MAESTRO": "/fake/maestro",
                          "MAY_DEVICE_BUNDLE": device_ios.BUNDLE})
        self.assertNotIn(TOKEN, json.dumps(device), "the staging session never reaches the agent")
        self.assertEqual((token[:13], model, seconds, tag), ("sk-ant-oat01-", "claude-sonnet-5-5",
                                                             device_agent.IOS_AGENT_SECONDS, "qa-39557415738"))
        self.assertEqual(len(minted), 3, "one session per scenario and one for the cleanup")
        self.assertEqual([(row["id"], row["verdict"]) for row in report["scenarios"]], [("chat", "ui-pass"),
                                                                                         ("poll", "pass")])
        self.assertEqual(self.delivered, [TOKEN + "1", TOKEN + "2"], "each scenario's messages, with its own session")
        self.assertEqual([row.get("delivery") for row in report["scenarios"]], [[device_tests.NONE_STORED]] * 2)
        self.assertIn("May device tests (ios): the test session's cleanup removed 2 tagged items", out)

    def test_refusals(self):
        for env, message in (({"MAY_TEST_SIGNIN_SECRET": None}, "MAY_TEST_SIGNIN_SECRET is not set"),
                             ({"MAY_STAGING_API_URL": "http://staging.example"}, "MAY_STAGING_API_URL is not https://staging-api.usemay.co"),
                             ({"MAY_APP_RENDERS_WRITE_KEY": "x"}, "A secret for another release step")):
            with self.subTest(message=message):
                with self.assertRaises(Exception) as refused:
                    self.run_it(env=env)
                self.assertIn(message, refused.exception.args[0])


class RecorderTests(Workspace):
    BIG = device_ios.MAX_RECORDING + 1

    def recorder(self, outcomes, raw=b"raw"):
        """stop() with each preset's (exit code, output size) from outcomes; a preset not listed fails."""
        sent, presets = [], []

        class Process:
            def send_signal(self, number):
                sent.append(number)

            def wait(self, timeout):
                return 0

        def run(argv, stdout, stderr, stdin, env):
            preset = argv[argv.index("--preset") + 1]
            presets.append((preset, Path(argv[argv.index("--output") + 1]).suffix))
            code, size = outcomes.get(preset, (1, 0))
            if size:
                Path(argv[argv.index("--output") + 1]).write_bytes(b"\0" * size)
            return Done(code)

        folder = self.root / "out"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log")
        recorder.process = Process()
        recorder.raw.write_bytes(raw)
        with patch("device_ios.subprocess.run", side_effect=run):
            names = recorder.stop(folder)
        leftovers = [path.name for path in self.private.iterdir() if path.name.startswith("sign-in-")]
        return names, sent, presets, folder, recorder, leftovers

    def test_the_first_conversion_that_works_is_published_as_mp4(self):
        names, sent, presets, folder, recorder, leftovers = self.recorder({"PresetAppleM4V720pHD": (0, 1000)})
        self.assertEqual((names, sent), (["sign-in.mp4"], [signal.SIGINT]))
        self.assertEqual(presets, [("PresetAppleM4V720pHD", ".m4v")], "each preset writes its own file type")
        self.assertEqual((folder / "sign-in.mp4").stat().st_size, 1000)
        self.assertEqual((leftovers, recorder.dropped, recorder.kept), ([], None, None))

    def test_a_failed_or_oversized_conversion_falls_through_to_the_next_preset(self):
        names, _, presets, folder, recorder, leftovers = self.recorder(
            {"PresetAppleM4V720pHD": (1, 0), "Preset1280x720": (0, self.BIG), "Preset960x540": (0, 2000)})
        self.assertEqual(presets, [("PresetAppleM4V720pHD", ".m4v"), ("Preset1280x720", ".mp4"),
                                   ("Preset960x540", ".mp4")])
        self.assertEqual(names, ["sign-in.mp4"], "no preset's recording was kept")
        self.assertEqual((folder / "sign-in.mp4").stat().st_size, 2000)
        self.assertEqual((leftovers, recorder.dropped, recorder.kept), ([], None, None))

    def test_no_conversion_keeps_the_original_when_it_fits(self):
        names, _, presets, folder, recorder, leftovers = self.recorder({}, raw=b"h264" * 10)
        self.assertEqual(len(presets), len(device_ios.CONVERSIONS))
        self.assertEqual(names, ["sign-in.mp4"], "the original was not kept")
        self.assertEqual((folder / "sign-in.mp4").read_bytes(), b"h264" * 10)
        self.assertEqual((leftovers, recorder.dropped), ([], None))
        self.assertEqual(recorder.kept, "unconverted: no conversion fit the cap (PresetAppleM4V720pHD failed (exit 1), "
                                        "Preset1280x720 failed (exit 1), Preset960x540 failed (exit 1))")

    def test_nothing_that_fits_is_dropped_and_says_why(self):
        names, _, _, folder, recorder, leftovers = self.recorder(
            {"PresetAppleM4V720pHD": (0, 0), "Preset960x540": (0, self.BIG)}, raw=b"\0" * self.BIG)
        self.assertEqual((names, list(folder.iterdir()), leftovers), ([], [], []))
        # Each preset's outcome, so a public log tells a failed avconvert from a conversion still over the cap.
        self.assertEqual(recorder.dropped, "no conversion fit the cap (PresetAppleM4V720pHD wrote no file, "
                                           "Preset1280x720 failed (exit 1), Preset960x540 38 MB) and the original is "
                                           "38 MB, over it")

    def test_a_dropped_recording_says_how_long_it_ran(self):
        """The length sets each recording's bitrate against the cap, so the public log carries it in seconds."""
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log")
        with patch("device_ios.subprocess.Popen"), patch("device_ios.time.monotonic", side_effect=[100.0, 820.4]), \
                patch("device_ios.subprocess.run", return_value=Done(1)):
            recorder.start()
            recorder.raw.write_bytes(b"\0" * self.BIG)
            self.assertEqual(recorder.stop(self.root), [])
        self.assertTrue((recorder.dropped or "").endswith(" over it after 720 s of recording"), recorder.dropped)

    def test_a_recorder_that_does_not_stop_is_dropped_and_says_why(self):
        class Stuck:
            def send_signal(self, number):
                pass

            def wait(self, timeout):
                raise subprocess.TimeoutExpired("recordVideo", timeout)

            def kill(self):
                pass

        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log")
        recorder.process = Stuck()
        self.assertEqual(recorder.stop(self.root), [])
        self.assertEqual(recorder.dropped, "the recorder did not stop")

    def test_the_run_prints_how_a_recording_was_kept(self):
        class Keeping(FakeRecorder):
            kept = "unconverted: no conversion worked"

        with patch("test_device_ios.FakeRecorder", Keeping):
            _, _, report, out, _ = RunTests.run_it(self)
        self.assertIn("May device tests (ios): sign-in: its recording was kept unconverted: no conversion worked", out)
        self.assertEqual(report["scenarios"][0]["videos"], ["sign-in.mp4"])

    def test_the_run_prints_why_a_recording_was_dropped(self):
        class Dropping(FakeRecorder):
            dropped = "over the cap at 51 MB"

            def stop(self, folder):
                return []

        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text("appId: x\n")
        with patch("test_device_ios.FakeRecorder", Dropping):
            _, _, report, out, _ = RunTests.run_it(self)
        self.assertIn("May device tests (ios): sign-in: its recording was dropped (over the cap at 51 MB)", out)
        self.assertEqual(report["scenarios"][0]["videos"], [])

    def test_start_records_the_configured_simulator(self):
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log")
        with patch("device_ios.subprocess.Popen") as popen:
            recorder.start()
        self.assertEqual(popen.call_args.args[0][:6], ["xcrun", "simctl", "io", UDID, "recordVideo", "--codec=h264"])


class SealTests(Workspace):
    def test_the_ios_report_is_sealed_under_its_own_name(self):
        folder = self.root / device_tests.OUT / ("20261021-" + SHA[:12]) / "ios"
        folder.mkdir(parents=True)
        (folder / "report.json").write_text("{}")
        seen = []

        def run(argv, stdout, stderr, stdin, env):
            seen.append(argv)
            Path(argv[argv.index("-o") + 1]).write_bytes(b"age")
            return Done()

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_RECIPIENT": "age1" + "q" * 58},
                        clear=True), patch("device_tests.subprocess.run", side_effect=run), \
                patch("device_tests.toolchain.tool_path", return_value="/age"), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.seal("ios")
        self.assertTrue((self.root / device_tests.SEALED / "ios.age").is_file())
        self.assertFalse((self.root / device_tests.SEALED / "ios.tar").exists())
        self.assertEqual(out.getvalue().strip(), "May device tests: the ios report encrypted for the publish job")
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), self.assertRaises(DeviceTestsError):
            device_tests.seal("android")


if __name__ == "__main__":
    unittest.main()
