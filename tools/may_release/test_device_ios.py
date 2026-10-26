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
import publish
import renders
from test_device_tests import network_tripwire, FLOW, MP4, SHA, TOKEN
from test_renders import png


TRIPWIRE = network_tripwire()


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()

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
        (self.source / device_tests.FLOWS_DIR / "sign-in.yaml").write_text(FLOW)
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

    notes = ()

    def halt(self):
        return True

    def stop(self, folder):
        (folder / (self.scenario + ".mp4")).write_bytes(MP4)
        return [self.scenario + ".mp4"]


class RunTests(Workspace):
    def run_it(self, selection="sign-in,connections", env=None, maestro_fails=False, agent=None, budgeted=None,
               calls_left=None, fail_mint=(), diagnosed=None, log_config_fails=False):
        calls, minted, self.maestro_timeouts, self.delivered = [], [], [], []
        self.built()
        shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
        (self.private / device_tests.VERDICTS).unlink(missing_ok=True)

        def quiet(argv, log, cwd=None, env=None, capture=False, timeout=None):
            calls.append((list(argv), dict(env or {})))
            if getattr(self, "quiet_hook", None):
                self.quiet_hook(argv, log, timeout)  # may raise, as a timed-out run does
            if "test" in argv and "--device" in argv:
                self.maestro_timeouts.append(timeout)
            if argv[:4] == ["xcrun", "simctl", "io", UDID] and argv[4] == "screenshot":
                Path(argv[-1]).write_bytes(png())
            if log_config_fails and "config" in argv:
                return Done(1)
            return Done(1 if maestro_fails and "test" in argv and "--device" in argv else 0)

        failures = list(fail_mint)

        def mint(base, secret, fresh=False):
            status = failures.pop(0) if failures else None
            if status is not None:
                raise device_tests.MintFailed(status)
            minted.append("fresh" if fresh else secret)
            return TOKEN + str(len(minted))

        self.deleted, self.cleaned_tags = [], []

        environ = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                   "MAY_STAGING_API_URL": "https://staging-api.usemay.co", "PATH": "/usr/bin", **(env or {})}
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("renders.quiet", side_effect=quiet), patch("renders.simulator", return_value=UDID), \
                patch("device_tests.flows.sleep"), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.head_revision", return_value=SHA), patch("device_ios.Recorder", FakeRecorder), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.signed_out = []
            device_ios.run_scenarios(selection, minted=mint, checked=lambda base, token, path: 200,
                                     revoked_by=lambda base, token: self.signed_out.append(token) or True,
                                     truth=lambda base, token, path, tag: (200, True),
                                     noted=lambda base, token, tag, note: (200, "found"),
                                     asked=lambda base, token, state, tag: (200, True),
                                     cleaned=lambda base, token, since, tag: self.cleaned_tags.append(tag) or 2,
                                     agent=agent or (lambda *a, **k: self.fail("an agent ran")),
                                     install=lambda root: root / "may-claude" / "claude", budgeted=budgeted,
                                     deleted_by=lambda base, token: self.deleted.append(token) or True,
                                     calls_left=calls_left or (lambda base, token: 100),
                                     delivered=lambda base, token, tag: self.delivered.append(token) or
                                     ["succeeded"],
                                     diagnosed=diagnosed or (lambda udid, env: device_ios.Diagnostics(
                                         udid, env, reports=self.root / "no-reports",
                                         shown=lambda *a: b"")))
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
            if "CLAUDE_CODE_OAUTH_TOKEN" in environ:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = environ["CLAUDE_CODE_OAUTH_TOKEN"]
            device_tests.judge_scenarios(judged_by=lambda *a: ("pass", "fine"), platform="ios")
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "ios" / "report.json").read_text())
        return calls, minted, report, out.getvalue(), runs[0]

    def test_each_scenarios_diagnostics_are_kept_while_its_session_lasts_and_the_log_gives_counts(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        events, drivers = [], []
        test = self

        class Keeping(device_ios.Diagnostics):
            def __init__(self, udid, env):
                super().__init__(udid, env, reports=test.root / "no-reports",
                                 shown=lambda *a: b"main-thread stall 300 ms\nmain-thread span recognizer 410 ms\n"
                                                  b"main-thread stall 820 ms\nmain-thread span event-store 1180 ms\n")

            def begin(self):
                events.append("begin")
                super().begin()

            def keep(self, folder, scenario, token, driver=None, onboarding=b""):
                drivers.append(driver)
                events.append(("keep", scenario, token, list(test.signed_out)))
                super().keep(folder, scenario, token, driver, onboarding)

        calls, minted, report, out, run = self.run_it(diagnosed=Keeping, env={device_ios.DIAGNOSTICS: "true"})
        self.assertEqual(events, ["begin", ("keep", "sign-in", TOKEN + "1", []),
                                  "begin", ("keep", "connections", TOKEN + "2", [TOKEN + "1"])],
                         "kept before its own sign-out, with its own session for the redaction")
        self.assertEqual(drivers, [self.private / "agent" / scenario / device_tests.device_mcp.DRIVER_FILE
                                   for scenario in ("sign-in", "connections")], "each scenario's own agent folder")
        self.assertEqual(sorted(path.name for path in (run / "ios").glob("*" + device_tests.DIAG)),
                         ["connections.diag.txt", "sign-in.diag.txt"])
        self.assertIn("May device tests (ios): diagnostics kept for 2 scenarios (4 main-thread stalls, the longest "
                      "820 ms; 0 with a crash; 0 with refused device acts)", out.splitlines())
        self.assertIn("May device tests (ios): sign-in diagnostics: 0 lines dropped", out.splitlines())
        self.assertNotIn("main-thread stall 300", out, "the public log gives counts alone")
        spans = [line for line in out.splitlines() if "main-thread span" in line]
        self.assertEqual(spans, ["May device tests (ios): main-thread span event-store: 2, the longest 1180 ms",
                                 "May device tests (ios): main-thread span recognizer: 2, the longest 410 ms"],
                         "per name across the scenarios, in the agreed order, only the names seen")

    def test_the_watchdog_and_change_log_are_asked_for_at_launch_only_when_dispatched(self):
        """may-app #1338: both are opt-in at launch; ios_diagnostics asks for them on evidence runs alone."""
        def launches(calls):
            return [env for argv, env in calls if argv[:3] == ["xcrun", "simctl", "launch"]]

        calls, minted, report, out, run = self.run_it(env={device_ios.DIAGNOSTICS: "true"})
        self.assertTrue(launches(calls))
        for env in launches(calls):
            self.assertEqual((env.get("SIMCTL_CHILD_MAY_DIAG_WATCHDOG"), env.get("SIMCTL_CHILD_MAY_DIAG_LOG_CHANGES")),
                             ("1", "1"))
        self.assertIn("May device tests (ios): the main-thread watchdog and SwiftUI's change log are on for this run",
                      out.splitlines())
        for value in (None, "false", "", "TRUE "):
            with self.subTest(value=value):
                (self.private / device_tests.AGENTS).unlink(missing_ok=True)
                calls, minted, report, out, run = self.run_it(env={device_ios.DIAGNOSTICS: value})
                self.assertTrue(launches(calls))
                for env in launches(calls):
                    self.assertFalse([k for k in env if k.startswith("SIMCTL_CHILD_MAY_DIAG_")])
                self.assertNotIn("watchdog", out)

    def test_a_plain_run_says_its_stalls_were_not_collected_never_zero(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        test = self

        class Keeping(device_ios.Diagnostics):
            def __init__(self, udid, env):
                super().__init__(udid, env, reports=test.root / "no-reports", shown=lambda *a: b"")

            def keep(self, folder, scenario, token, driver=None, onboarding=b""):
                super().keep(folder, scenario, token, driver, b"onboarding line\n")

        calls, minted, report, out, run = self.run_it(diagnosed=Keeping)
        self.assertIn("May device tests (ios): diagnostics kept for 2 scenarios (main-thread stalls not collected: "
                      "dispatch with ios_diagnostics for them; 0 with a crash; 0 with refused device acts)",
                      out.splitlines())
        self.assertNotIn("0 main-thread stalls", out)

    def test_reduce_motion_is_turned_on_before_the_app_starts_only_when_dispatched(self):
        """The coordinator's A/B switch (25 Oct): ios_reduce_motion, off unless dispatched true."""
        reduce = ["xcrun", "simctl", "spawn", UDID, "defaults", "write", "com.apple.Accessibility",
                  "ReduceMotionEnabled", "-bool", "true"]
        calls, minted, report, out, run = self.run_it(env={device_ios.REDUCE_MOTION: "true"})
        argvs = [argv for argv, _ in calls]
        self.assertIn(reduce, argvs)
        self.assertLess(argvs.index(reduce), argvs.index(["xcrun", "simctl", "install", UDID,
                                                         str(self.root / device_ios.APP)]),
                        "on before the app is installed and first started")
        self.assertIn("May device tests (ios): Reduce Motion is on for this run", out.splitlines())
        for value in (None, "false", "", "TRUE "):
            with self.subTest(value=value):
                (self.private / device_tests.AGENTS).unlink(missing_ok=True)
                calls, minted, report, out, run = self.run_it(env={device_ios.REDUCE_MOTION: value})
                self.assertNotIn(reduce, [argv for argv, _ in calls])
                self.assertNotIn("Reduce Motion", out)

    def test_swiftuis_info_lines_are_persisted_for_that_subsystem_alone_before_the_app_starts(self):
        """Run 40232904242: the body-change lines stayed in memory alone, so most were gone by the read."""
        persist = ["xcrun", "simctl", "spawn", UDID, "log", "config", "--subsystem", "com.apple.SwiftUI", "--mode",
                   "level:info,persist:info"]
        calls, _, _, out, _ = self.run_it()
        argvs = [argv for argv, _ in calls]
        self.assertIn(persist, argvs)
        self.assertLess(argvs.index(persist), argvs.index(["xcrun", "simctl", "install", UDID,
                                                          str(self.root / device_ios.APP)]),
                        "before the app is installed and first started")
        self.assertEqual([argv for argv in argvs if "config" in argv], [persist], "that subsystem alone")
        self.assertNotIn("SwiftUI", out, "a config that took says nothing")

    def test_a_log_config_that_fails_is_said_and_never_stops_the_run(self):
        calls, _, report, out, _ = self.run_it(log_config_fails=True)
        self.assertIn("May device tests (ios): SwiftUI's body changes are kept for the last minutes alone in this run",
                      out)
        self.assertTrue(report["scenarios"], "the run went on")

    def test_the_session_goes_in_the_launch_environment_alone_and_fresh_per_scenario(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
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
            "May device tests (ios): 2 test sessions were signed out at their end",
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

    def test_a_flow_tagged_android_alone_is_skipped_on_ios(self):
        """connections.yaml was tagged android alone and ran on iOS (the coordinator, 24 Oct)."""
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW.replace("  - ios\n", ""))
        calls, minted, report, _, _ = self.run_it()
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("sign-in", "ui-pass", device_tests.UNCONFIGURED), ("connections", "skipped", "skipped (flow not tagged for ios)")])
        self.assertEqual(len(minted), 1, "no session is minted for the skipped scenario")

    def test_a_flow_naming_an_id_the_app_lacks_fails_on_ios_too(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(
            FLOW + "- assertVisible:\n    id: demo-connections\n")
        (self.source / "android/app/src/main/A.kt").parent.mkdir(parents=True, exist_ok=True)
        (self.source / "android/app/src/main/A.kt").write_text('val tag = "demo-connections"\n')
        calls, minted, report, _, _ = self.run_it()
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("sign-in", "ui-pass", device_tests.UNCONFIGURED), ("connections", "fail", device_tests.FLOW_IDS)],
            "an id in Android's sources alone is not in iOS's")
        self.assertEqual(len(minted), 1, "no session is minted for the failed scenario")

    def test_maestro_keeps_its_debug_output_out_of_the_runners_home_on_ios_too(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        calls, minted, report, _, _ = self.run_it()
        home = str(self.root / "may-maestro-home")
        isolated = {(env.get("HOME"), env.get("XDG_STATE_HOME"), env.get("MAESTRO_OPTS"))
                    for argv, env in calls if "test" in argv and "--device" in argv}
        self.assertEqual(isolated, {(home, home, "-Duser.home=" + home)})
        self.assertFalse((self.root / "may-maestro-home").exists(), "removed at the run's end")

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
        device, token, model, seconds, tag = seen[-1]
        self.assertEqual({key: device[key] for key in ("MAY_DEVICE_PLATFORM", "MAY_DEVICE_UDID", "MAY_DEVICE_MAESTRO",
                                                        "MAY_DEVICE_BUNDLE")},
                         {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": UDID, "MAY_DEVICE_MAESTRO": "/fake/maestro",
                          "MAY_DEVICE_BUNDLE": device_ios.BUNDLE})
        self.assertNotIn(TOKEN, json.dumps(device), "the staging session never reaches the agent")
        self.assertEqual((token[:13], model, seconds, tag), ("sk-ant-oat01-", "claude-sonnet-5-5",
                                                             device_agent.IOS_AGENT_SECONDS, "qa-39557415738-i-chat"))
        self.assertEqual(len(minted), 3, "one session per scenario and one for the cleanup")
        self.assertEqual([(row["id"], row["verdict"]) for row in report["scenarios"]], [("poll", "pass"),
                                                                                         ("chat", "pass")],
                         "chat runs last on iOS")
        self.assertEqual(self.delivered, [TOKEN + "1", TOKEN + "2"], "each scenario's messages, with its own session")
        self.assertEqual([row.get("delivery") for row in report["scenarios"]], [["succeeded"]] * 2)
        self.assertIn("May device tests (ios): the test session's cleanup removed 2 tagged items", out)
        self.assertEqual(self.cleaned_tags, ["qa-39557415738-i"], "iOS's cleanup never matches Android's items")
        self.assertEqual(self.signed_out, [TOKEN + "1", TOKEN + "2", TOKEN + "3"],
                         "each scenario's shared session, then the cleanup's, signed out")

    def test_notes_and_chat_run_last_on_ios(self):
        """Run 40203481816: chat ran second and wedged, so poll and poll-skip found no budget left (coordinator,
        25 Oct: sign-out, poll, poll-skip, notes, then chat)."""
        seen = []

        def agent(cli, goal, tag, device, work, token, model, seconds):
            seen.append(tag.rsplit("-i-", 1)[1])
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        _, _, report, _, _ = self.run_it(
            selection="chat,notes,poll,poll-skip,sign-out", agent=agent,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5",
                 "GITHUB_RUN_ID": "39557415738"})
        self.assertEqual([row["id"] for row in report["scenarios"]], ["sign-out", "poll", "poll-skip", "notes", "chat"])
        self.assertEqual(seen, ["poll", "poll-skip", "notes", "chat"])

    def test_scenarios_stopped_by_the_fail_fast_are_counted(self):
        def agent(cli, goal, tag, device, work, token, model, seconds):
            return {"state": "ok", "outcome": "blocked", "summary": "stopped", "hierarchy": "h", "actions": 4,
                    "wedged": True}

        _, _, _, out, _ = self.run_it(
            selection="chat,poll", agent=agent,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5"})
        self.assertIn("May device tests (ios): 2 scenarios stopped after refused device acts in a row (fail-fast)", out)

    def test_the_server_reads_and_diagnostics_come_before_the_conversion(self):
        """Run 40181517145: poll's read came after a long conversion and got 401 (coordinator, 25 Oct)."""
        order = []

        class Ordered(FakeRecorder):
            notes = ("part 1 Preset960x540: exit 205; avconvert said: nothing",)

            def halt(self):
                order.append(("halt", self.scenario))
                return True

            def stop(self, folder):
                order.append(("convert", self.scenario))
                return super().stop(folder)

        class Diagnosed(device_ios.Diagnostics):
            def keep(self, folder, scenario, token, driver=None, onboarding=b""):
                order.append(("diagnostics", scenario))
                return super().keep(folder, scenario, token, driver, onboarding)

            def recording(self, folder, scenario, token, notes):
                order.append(("recording notes", scenario, list(notes)))
                return super().recording(folder, scenario, token, notes)

        def agent(cli, goal, tag, device, work, token, model, seconds):
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        reads = device_tests.server_reads

        def read(row, *args):
            order.append(("reads", row["id"]))
            return reads(row, *args)

        with patch("test_device_ios.FakeRecorder", Ordered), patch("device_tests.server_reads", side_effect=read):
            self.run_it(selection="chat", agent=agent, diagnosed=lambda udid, env: Diagnosed(
                udid, env, reports=self.root / "no-reports", shown=lambda *a: b""),
                env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40,
                     "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5"})
        assert order, "the scenario never ran"
        self.assertEqual(order, [("halt", "chat"), ("reads", "chat"), ("diagnostics", "chat"), ("convert", "chat"),
                                 ("recording notes", "chat", list(Ordered.notes))],
                         "the recording's notes join the private diag after its conversion")

    def test_poll_skip_gives_the_device_server_its_own_step_limit(self):
        devices = {}

        def agent(cli, goal, tag, device, work, token, model, seconds):
            devices[tag.rsplit("-i-", 1)[1]] = device
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        self.run_it(selection="poll,poll-skip", agent=agent,
                    env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40,
                         "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5", "GITHUB_RUN_ID": "39557415738"})
        assert set(devices) == {"poll", "poll-skip"}, devices
        self.assertEqual((devices["poll"].get("MAY_DEVICE_STEPS"), devices["poll-skip"].get("MAY_DEVICE_STEPS")),
                         ("120", "120"), "both type slowly (run 40232904242: poll ran out at 80)")

    def test_an_agent_scenario_runs_the_onboarding_subflow_before_the_agent(self):
        """Run 40046272749: the shared account lands in onboarding, not Chats; may-app's subflow taps through it."""
        (self.source / device_tests.FLOWS_DIR / "subflows").mkdir()
        subflow = self.source / device_tests.FLOWS_DIR / device_tests.ONBOARDING
        subflow.write_text("appId: x\n")
        order = []

        def agent(cli, goal, tag, device, work, token, model, seconds):
            order.append("agent")
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        calls, _, _, out, _ = self.run_it(
            selection="chat", agent=agent,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5"})
        tests = [argv for argv, _ in calls if argv[0] == "/fake/maestro"]
        self.assertEqual(tests, [["/fake/maestro", "--device", UDID, "test", str(subflow)]])
        self.assertEqual(self.maestro_timeouts, [device_tests.ONBOARDING_SECONDS])
        self.assertEqual(order, ["agent"])
        self.assertNotIn("onboarding subflow", out, "a subflow that passed says nothing")

    def test_an_onboarding_subflow_that_does_not_pass_leaves_its_last_lines_in_the_diag(self):
        """Run 40181517145: chat's and notes' subflow did not pass, with nothing to say where it stopped."""
        (self.source / device_tests.FLOWS_DIR / "subflows").mkdir()
        subflow = self.source / device_tests.FLOWS_DIR / device_tests.ONBOARDING
        subflow.write_text("appId: x\n")
        kept = []

        class Diagnosed(device_ios.Diagnostics):
            def keep(self, folder, scenario, token, driver=None, onboarding=b""):
                kept.append(onboarding)
                return super().keep(folder, scenario, token, driver, onboarding)

        def agent(cli, goal, tag, device, work, token, model, seconds):
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        def hang(argv, log, timeout):
            if str(subflow) in argv:
                with log.open("ab") as output:
                    output.write(b"".join(b"Maestro %d\n" % n for n in range(60)))
                raise subprocess.TimeoutExpired(argv, timeout)

        self.quiet_hook = hang
        _, _, report, out, _ = self.run_it(
            selection="chat", agent=agent, diagnosed=lambda udid, env: Diagnosed(
                udid, env, reports=self.root / "no-reports", shown=lambda *a: b""),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5"})
        assert kept, "no diagnostics were kept"
        self.assertEqual(kept[0], b"".join(b"Maestro %d\n" % n for n in range(20, 60)), "its last 40 lines")
        self.assertIn("chat: the onboarding subflow did not pass", out)
        self.assertNotIn("Maestro 59", out, "never in the public log")
        self.assertFalse((self.private / "chat-onboarding.log").exists(), "moved into the private run log")
        self.assertIn(b"Maestro 0\n", (self.private / "device-run.log").read_bytes())

    def test_the_public_log_never_names_a_changed_property(self):
        shown = (b"2026-10-25 07:00:01.000 Df MayPlayground[1:2a] [com.apple.SwiftUI:Changed Body Properties] "
                 b"ChatView: _position changed.\n"
                 b"2026-10-25 07:00:02.000 Df MayPlayground[1:2a] [io.github.mayassistant.may.playground:watchdog] "
                 b"main-thread stall 330 ms\n"
                 b"2026-10-25 07:00:02.000 Df MayPlayground[1:2a] [io.github.mayassistant.may.playground:watchdog] "
                 b"main-thread stall-after view-chat\n")
        _, _, _, out, _ = self.run_it(diagnosed=lambda udid, env: device_ios.Diagnostics(
            udid, env, reports=self.root / "no-reports", shown=lambda *a: shown))
        self.assertIn("main-thread stall-after view-chat: 1", out)
        self.assertNotIn("_position", out)
        self.assertNotIn("ChatView", out)

    def test_the_run_prints_both_a_kept_and_a_dropped_part(self):
        class Both(FakeRecorder):
            kept, dropped = "part 1: unconverted: no conversion fit the cap", "part 2: the original is 50 MB, over it"

        with patch("test_device_ios.FakeRecorder", Both):
            _, _, _, out, _ = self.run_it()
        self.assertIn("sign-in: its recording was dropped (part 2: the original is 50 MB, over it)", out)
        self.assertIn("sign-in: its recording was kept part 1: unconverted: no conversion fit the cap", out)

    def test_fresh_scenarios_delete_their_own_accounts_on_ios_too(self):
        def agent(cli, goal, tag, device, work, token, model, seconds):
            return {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 3}

        _, minted, report, out, _ = self.run_it(
            selection="chat,poll", agent=agent,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5",
                 device_tests.FRESH: "all"})
        self.assertEqual(minted, ["fresh", "fresh"], "no shared session, not even for a cleanup")
        self.assertEqual(self.deleted, [TOKEN + "1", TOKEN + "2"])
        self.assertIn("May device tests (ios): 2 fresh test accounts were deleted by their cleanup", out)
        self.assertNotIn("the test session's cleanup", out)

    def test_a_fresh_account_is_deleted_when_its_scenario_stops_part_way_on_ios(self):
        def agent(*args, **kwargs):
            raise OSError("agent died")

        with self.assertRaises(OSError):
            self.run_it(selection="chat", agent=agent,
                        env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40,
                             "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5", device_tests.FRESH: "all"})
        self.assertEqual(self.deleted, [TOKEN + "1"], "the run's finally deleted chat's account")

    def test_an_agent_scenario_near_the_model_call_cap_is_skipped_on_ios_too(self):
        def agent(*args, **kwargs):
            self.fail("an agent ran near the model-call cap")

        _, minted, report, _, _ = self.run_it(
            selection="chat", agent=agent, calls_left=lambda base, token: 0,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "o" * 40, "MAY_DEVICE_TESTS_MODEL": "claude-sonnet-5-5"})
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]],
                         [("chat", "skipped", device_tests.MODEL_CAP)])

    def test_a_mint_that_fails_twice_skips_its_scenario_alone_on_ios_too(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        try:
            _, minted, report, out, _ = self.run_it(fail_mint=(None, 0, 0))
        except DeviceTestsError as error:
            self.fail("a failed mint stopped the iOS run: " + error.args[0])
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]],
                         [("sign-in", "ui-pass", device_tests.UNCONFIGURED),
                          ("connections", "skipped", device_tests.MINT_FAILED)])
        self.assertIn("May device tests (ios): connections: staging did not mint a session (HTTP status 0, 0 for no "
                      "reply), twice; the scenario is skipped", out)

    def test_refusals(self):
        for env, message in (({"MAY_TEST_SIGNIN_SECRET": None}, "MAY_TEST_SIGNIN_SECRET is not set"),
                             ({"MAY_STAGING_API_URL": "http://staging.example"}, "MAY_STAGING_API_URL is not https://staging-api.usemay.co"),
                             ({"MAY_APP_RENDERS_WRITE_KEY": "x"}, "A secret for another release step")):
            with self.subTest(message=message):
                with self.assertRaises(Exception) as refused:
                    self.run_it(env=env)
                self.assertIn(message, refused.exception.args[0])


class DiagnosticsTests(Workspace):
    """Android's twin: the watchdog's stalls (may-app #1272) and the Playground's own new crash reports, redacted."""

    STALLS = (b"2026-10-25 00:10:01.100 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
              b"main-thread stall 412 ms\n"
              b"2026-10-25 00:10:05.300 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
              b"main-thread stall 1250 ms\n")

    def diagnostics(self, shown=None, reports=None):
        self.shown = []
        reports = reports or self.root / "reports"
        reports.mkdir(exist_ok=True)

        def show(udid, env, start):
            self.shown.append((udid, start))
            return self.STALLS if shown is None else shown

        return device_ios.Diagnostics(UDID, {}, reports=reports, shown=show)

    SPANS = (b"2026-10-25 03:11:31.900 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread span event-store 1180 ms\n"
             b"2026-10-25 03:11:32.380 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread stall 1177 ms\n"
             b"2026-10-25 03:11:33.000 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread span recognizer 410 ms\n"
             b"2026-10-25 03:11:33.100 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread span not-agreed 300 ms\n"
             b"2026-10-25 03:11:34.000 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread span event-store 600 ms\n"
             b"2026-10-25 03:11:35.000 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:watchdog] "
             b"main-thread span event-store 700 ms during sam.avery@example.com\n")

    def test_the_watchdogs_spans_are_kept_among_the_stalls_and_counted_by_agreed_name(self):
        """may-app #1281: N's named spans of main-thread work, beside the stalls."""
        diagnostics = self.diagnostics(shown=self.SPANS)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "notes", TOKEN)
        text = (folder / ("notes" + device_tests.DIAG)).read_bytes()
        watchdog = text.split(b"== watchdog ==\n")[1].splitlines()
        self.assertEqual([line.split(b"] ")[-1] for line in watchdog],
                         [b"main-thread span event-store 1180 ms", b"main-thread stall 1177 ms",
                          b"main-thread span recognizer 410 ms", b"main-thread span not-agreed 300 ms",
                          b"main-thread span event-store 600 ms"], "in the log's own order, stalls and spans together")
        self.assertNotIn(b"== spans ==", text, "a separate section only when the log is trimmed")
        self.assertEqual(diagnostics.spans, {"event-store": [2, 1180], "recognizer": [1, 410]},
                         "an unknown name is kept privately but not counted, and a line not ending at ms is no span")
        self.assertEqual((diagnostics.stalls, diagnostics.longest), (1, 1177), "spans are never stalls")
        self.assertTrue(publish.diagnostics_ok(text))

    def test_chats_three_spans_are_counted_by_name(self):
        """N, 25 Oct: run 40164384433's chat stalled every 3 s in its idle sync; three spans name that path."""
        lines = b"".join(b"2026-10-25 05:23:%02d.000 Df MayPlayground[1:2] [io.github.mayassistant.may.playground:"
                         b"watchdog] main-thread span %s %d ms\n" % (second, name, ms)
                         for second, (name, ms) in enumerate([(b"chat-publish", 410), (b"chat-inbox", 300),
                                                              (b"chat-merge", 260), (b"chat-publish", 520),
                                                              (b"chat-other", 900)]))
        diagnostics = self.diagnostics(shown=lines)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        self.assertEqual(diagnostics.spans, {"chat-publish": [2, 520], "chat-inbox": [1, 300], "chat-merge": [1, 260]})
        self.assertIn(b"main-thread span chat-other 900 ms", (folder / ("chat" + device_tests.DIAG)).read_bytes(),
                      "a name not agreed is kept privately, never counted")

    AFTER = b"".join(b"2026-10-25 06:00:%02d.000 Df MayPlayground[1:2] [io.github.mayassistant.may.playground:watchdog] "
                     b"main-thread %s\n" % (second, line) for second, line in enumerate([
                         b"stall 900 ms", b"stall-after sync-publish", b"stall 400 ms", b"stall-after view-chat",
                         b"stall 3800 ms", b"stall-after sync-publish", b"stall 300 ms", b"stall-after not-agreed"]))

    def test_each_stall_after_name_is_counted_and_any_other_as_other(self):
        """may-app #1309 (N, 25 Oct): the step that ran before each stall, from twelve agreed names."""
        diagnostics = self.diagnostics(shown=self.AFTER)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        self.assertEqual(diagnostics.after, {"sync-publish": 2, "view-chat": 1, "other": 1})
        self.assertEqual((diagnostics.stalls, diagnostics.longest), (4, 3800), "a stall-after line is never a stall")
        text = (folder / ("chat" + device_tests.DIAG)).read_bytes()
        self.assertEqual([line.split(b"main-thread ")[-1] for line in text.split(b"== watchdog ==\n")[1].splitlines()],
                         [b"stall 900 ms", b"stall-after sync-publish", b"stall 400 ms", b"stall-after view-chat",
                          b"stall 3800 ms", b"stall-after sync-publish", b"stall 300 ms", b"stall-after not-agreed"],
                         "each after its own stall, in the log's order")
        self.assertEqual(len(device_ios.STALL_AFTER_NAMES), 13, "N's twelve and may-app #1318's view-chat-row")

    def test_the_chat_bodys_changes_are_kept_privately_near_each_chat_stall_and_never_counted(self):
        """may-app #1318 (coordinator, 25 Oct): ChatView's changed properties, private, capped around each stall."""
        def line(second, text, process=b"MayPlayground[1:2a]", where=b"io.github.mayassistant.may.playground:watchdog"):
            return b"2026-10-25 07:00:%02d.000 Df %s [%s] %s\n" % (second, process, where, text)

        body = b"com.apple.SwiftUI:Changed Body Properties"
        shown = b"".join([
            line(1, b"ChatView: _early1 changed.", where=body), line(2, b"ChatView: _early2 changed.", where=body),
            line(3, b"ChatView: _a changed.", where=body), line(4, b"ChatView: _b changed.", where=body),
            line(5, b"ChatView: _position changed." + b" and then some more" * 20, where=body),
            line(6, b"main-thread stall 330 ms"), line(6, b"main-thread stall-after view-chat"),
            line(7, b"ChatView: _lonely changed.", where=body),
            line(8, b"main-thread stall 300 ms"), line(8, b"main-thread stall-after sync-end"),
            line(9, b"ChatView: _row changed.", where=body),
            line(10, b"main-thread stall 280 ms"), line(10, b"main-thread stall-after view-chat-row"),
            line(11, b"main-thread span chat-body 290 ms"),
            line(12, b"OtherView: _x changed.", process=b"Other[3:4]", where=body)])
        diagnostics = self.diagnostics(shown=shown)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        whole = (folder / ("chat" + device_tests.DIAG)).read_bytes().split(b"== watchdog ==\n")[1].splitlines()
        kept = [entry.split(b"] ", 2)[-1] for entry in whole]
        self.assertEqual(kept[:2], [b"ChatView: _a changed.", b"ChatView: _b changed."],
                         "the three nearest before the chat stall")
        self.assertTrue(kept[2].startswith(b"ChatView: _position changed. and then"), kept[2])
        self.assertEqual(len(whole[2]), device_ios.MAX_BODY_LINE, "each cut")
        self.assertNotIn(b"ChatView: _early1 changed.", kept)
        self.assertNotIn(b"ChatView: _lonely changed.", kept, "none before a stall that is not the chat's")
        self.assertIn(b"ChatView: _row changed.", kept, "the row breadcrumb counts as the chat's")
        self.assertFalse(any(b"OtherView" in entry for entry in kept), "never another process's lines")
        self.assertEqual(diagnostics.after, {"view-chat": 1, "sync-end": 1, "view-chat-row": 1})
        self.assertEqual(diagnostics.spans, {"chat-body": [1, 290]})
        self.assertEqual(diagnostics.stalls, 3)

    def test_the_onboarding_lines_and_the_recording_notes_land_redacted_in_the_diag(self):
        diagnostics = self.diagnostics()
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        onboarding = b"".join(b"Maestro line %d\n" % n for n in range(39)) + b"Authorization: Bearer " + \
            TOKEN.encode() + b"\n"
        diagnostics.keep(folder, "chat", TOKEN, onboarding=onboarding)
        diagnostics.recording(folder, "chat", "", ["part 1 PresetAppleM4V720pHD: exit 205; avconvert said: "
                                                   "AVFoundationErrorDomain -11800", "x token " + TOKEN])
        text = (folder / ("chat" + device_tests.DIAG)).read_bytes()
        self.assertIn(b"== onboarding ==\nMaestro line 0\n", text)
        self.assertIn(b"Maestro line 38\n== watchdog ==", text)
        self.assertTrue(text.endswith(b"== recording ==\npart 1 PresetAppleM4V720pHD: exit 205; avconvert said: "
                                      b"AVFoundationErrorDomain -11800\n"), text[-200:])
        self.assertNotIn(TOKEN.encode(), text)
        self.assertEqual((diagnostics.kept, diagnostics.dropped["chat"]), (1, 2), "one file, two lines dropped")
        self.assertTrue(publish.diagnostics_ok(text))
        diagnostics.recording(folder, "notes", "", ["part 2 Preset960x540: timed out; avconvert said: nothing"])
        self.assertEqual((folder / ("notes" + device_tests.DIAG)).read_bytes(),
                         b"== recording ==\npart 2 Preset960x540: timed out; avconvert said: nothing\n")
        self.assertEqual(diagnostics.kept, 2, "a recording's notes alone make a diag of their own")
        diagnostics.recording(folder, "poll", "", [])
        self.assertFalse((folder / ("poll" + device_tests.DIAG)).exists(), "no notes, no file")

    def test_a_full_diag_still_has_room_for_the_onboarding_lines_and_the_recording_notes(self):
        flood = b"".join(b"2026-10-25 03:12:%02d.000 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:"
                         b"watchdog] main-thread stall %d ms\n" % (i % 60, 300 + i) for i in range(6000))
        diagnostics = self.diagnostics(shown=flood)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN, onboarding=b"Maestro: " + b"x" * 100 + b"\n" * 1 + b"y\n" * 9000)
        diagnostics.recording(folder, "chat", "", ["note %d" % n for n in range(5000)])
        text = (folder / ("chat" + device_tests.DIAG)).read_bytes()
        self.assertLessEqual(len(text), device_tests.MAX_DIAG)
        self.assertIn(b"== recording ==\nnote 0\n", text)
        self.assertIn(b"main-thread stall 6299 ms\n", text)
        self.assertTrue(publish.diagnostics_ok(text), "every section ends on a whole line")

    def test_a_trimmed_watchdog_log_never_loses_its_spans(self):
        flood = b"".join(b"2026-10-25 03:12:%02d.000 Df MayPlayground[812:9a1] [io.github.mayassistant.may.playground:"
                         b"watchdog] main-thread stall %d ms\n" % (i % 60, 300 + i) for i in range(6000))
        diagnostics = self.diagnostics(shown=self.SPANS + flood)
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "notes", TOKEN)
        text = (folder / ("notes" + device_tests.DIAG)).read_bytes()
        self.assertLessEqual(len(text), device_tests.MAX_DIAG)
        self.assertIn(b"== spans ==\n", text)
        spans, watchdog = text.split(b"== spans ==\n")[1].split(b"== watchdog ==\n")
        self.assertIn(b"main-thread span event-store 1180 ms", spans)
        self.assertNotIn(b"span event-store 1180", watchdog, "the trimmed log kept only its newest lines")
        self.assertIn(b"main-thread stall 6299 ms", watchdog)
        self.assertEqual(diagnostics.spans["event-store"], [2, 1180])

    def test_each_scenario_keeps_its_stalls_and_the_playgrounds_new_crash_reports_redacted(self):
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "MayPlayground-2026-10-24-230000.ips").write_bytes(b"old crash\n")
        diagnostics = self.diagnostics(reports=reports, shown=self.STALLS + b"Authorization: Bearer " +
                                       TOKEN.encode() + b"\nmain-thread stall 9 ms from sam.avery@example.com\n")
        (reports / "MayPlayground-2026-10-25-001002.ips").write_bytes(
            b'{"app_name":"MayPlayground","bundleID":"io.github.mayassistant.may.playground"}\nThread 0 Crashed:\n'
            b"0 MayPlayground ChatView.body\n"
            b"cookie: sid=" + TOKEN.encode() + b"\n")
        (reports / "SpringBoard-2026-10-25-001002.ips").write_bytes(b"another process\n")
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        text = (folder / ("chat" + device_tests.DIAG)).read_bytes()
        self.assertIn(b"== crash ==\n", text)
        self.assertIn(b"Thread 0 Crashed:", text)
        self.assertIn(b"== watchdog ==\n", text)
        self.assertIn(b"main-thread stall 1250 ms", text)
        self.assertIn(b"main-thread stall 9 ms from ***@example.com", text)
        for absent in (b"old crash", b"another process", TOKEN.encode(), b"sam.avery", b"Bearer"):
            self.assertNotIn(absent, text)
        self.assertEqual(device_tests.kept_lines(text, ""), (text, 0), "publish's line-by-line check keeps it whole")
        self.assertTrue(publish.diagnostics_ok(text))
        self.assertEqual((diagnostics.kept, diagnostics.stalls, diagnostics.longest, diagnostics.crashes,
                          diagnostics.dropped), (1, 3, 1250, 1, {"chat": 1}),
                         "the crash report's cookie line; a log line that is no stall is never read")
        self.assertEqual(self.shown[0][0], UDID)
        diagnostics.begin()
        diagnostics.keep(folder, "notes", TOKEN)
        self.assertEqual(diagnostics.crashes, 1, "a crash report is kept once, by the scenario it came in")

    def test_a_report_named_for_the_playground_is_kept_only_when_its_header_names_its_bundle(self):
        """The security read of #115: the file name alone is not proof."""
        reports = self.root / "reports"
        reports.mkdir()
        diagnostics = self.diagnostics(reports=reports, shown=b"")
        (reports / "MayPlayground-2026-10-25-010000.ips").write_bytes(
            b'{"app_name":"MayPlayground","bundleID":"com.example.other"}\nsomeone else\n')
        (reports / "MayPlayground-2026-10-25-010100.crash").write_bytes(
            b"Process:         MayPlayground [812]\nIdentifier:      io.github.mayassistant.may.playground\n"
            b"Thread 0 Crashed:: Dispatch queue: com.apple.main-thread\n")
        (reports / "MayPlayground-2026-10-25-010200.ips").write_bytes(
            b'{"bundleID":"io.github.mayassistant.may.playground.evil"}\nlookalike\n')
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        text = (folder / ("chat" + device_tests.DIAG)).read_bytes()
        self.assertIn(b"Dispatch queue: com.apple.main-thread", text)
        for absent in (b"someone else", b"lookalike"):
            self.assertNotIn(absent, text)
        self.assertEqual(device_ios.BUNDLE, "io.github.mayassistant.may.playground")

    def test_the_refused_acts_driver_lines_land_redacted_in_the_diagnostics(self):
        """Coordinator, 25 Oct: N reads them; through kept_lines with the session, private, counted in the log."""
        diagnostics = self.diagnostics(shown=b"")
        driver = self.root / "driver.txt"
        header = b"== refused: - tapOn: (action 7, 2026-10-25 03:02:57.120 to 2026-10-25 03:04:27.125 UTC) ==\n"
        driver.write_bytes(header + b"Tap on point (60,40)... FAILED\n"
                           b"launch env MAY_TEST_SESSION=" + TOKEN.encode() + b"\nraw " + TOKEN.encode() + b"\n")
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "notes", TOKEN, driver)
        text = (folder / ("notes" + device_tests.DIAG)).read_bytes()
        self.assertIn(b"== driver ==\n" + header + b"Tap on point (60,40)... FAILED\n", text)
        self.assertNotIn(TOKEN.encode(), text, "the session itself is dropped too, not only secret-shaped lines")
        self.assertEqual((diagnostics.drivers, diagnostics.dropped), (1, {"notes": 2}))
        self.assertTrue(publish.diagnostics_ok(text))
        link = self.root / "link.txt"
        link.symlink_to(driver)
        diagnostics.keep(folder, "chat", TOKEN, link)
        self.assertFalse((folder / ("chat" + device_tests.DIAG)).exists(), "a link is never read")

    def test_nothing_new_writes_nothing(self):
        diagnostics = self.diagnostics(shown=b"Timestamp               Ty Process[PID:TID]\n")
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "sign-in", TOKEN)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual((diagnostics.kept, diagnostics.stalls, diagnostics.crashes), (0, 0, 0))

    def test_the_log_read_names_the_playgrounds_watchdog_alone_and_never_fails(self):
        """Coordinator, 25 Oct: bounded to the Playground's subsystem, so nothing from another process is read."""
        self.assertEqual(device_ios.WATCHDOG,
                         'subsystem == "io.github.mayassistant.may.playground" AND category == "watchdog"')
        seen = []

        def bounded(argv, env, limit):
            seen.append((argv, limit))
            return 0, b"main-thread stall 300 ms\n"

        with patch("device_tests.bounded_output", side_effect=bounded):
            self.assertEqual(device_ios.show_log(UDID, {}, "2026-10-25 00:10:00"), b"main-thread stall 300 ms\n")
        self.assertEqual(seen, [(["xcrun", "simctl", "spawn", UDID, "log", "show", "--style", "compact", "--info",
                                  "--start", "2026-10-25 00:10:00", "--predicate",
                                  '(subsystem == "io.github.mayassistant.may.playground" AND category == "watchdog") OR '
                                  '(process == "MayPlayground" AND subsystem == "com.apple.SwiftUI" AND category == '
                                  '"Changed Body Properties")'], device_tests.MAX_DIAG)],
                         "SwiftUI's lines from the Playground's own process alone (may-app #1318)")
        with patch("device_tests.bounded_output", return_value=None):
            self.assertEqual(device_ios.show_log(UDID, {}, "2026-10-25 00:10:00"), b"", "no answer in time")

    def test_a_link_or_another_name_in_the_reports_folder_is_never_read(self):
        reports = self.root / "reports"
        reports.mkdir()
        secret = self.root / "secret.txt"
        secret.write_bytes(b"outside\n")
        diagnostics = self.diagnostics(reports=reports, shown=b"")
        (reports / "MayPlayground-link.ips").symlink_to(secret)
        (reports / "MayPlayground.ips.bak").write_bytes(b"not a report\n")
        folder = self.root / "out"
        folder.mkdir()
        diagnostics.begin()
        diagnostics.keep(folder, "chat", TOKEN)
        self.assertEqual(list(folder.iterdir()), [])


class RecorderTests(Workspace):
    BIG = device_ios.MAX_RECORDING + 1

    def recorder(self, outcomes, raw=None, seconds=None):
        """stop() with each preset's (exit code, output size) from outcomes, a tail's under ("tail", preset); a preset
        not listed fails. seconds: how long the recording ran."""
        sent, presets = [], []
        self.tails = []

        class Process:
            def send_signal(self, number):
                sent.append(number)

            def wait(self, timeout):
                return 0

        self.timeouts = []

        def run(argv, stdout, stderr, stdin, env, timeout):
            self.timeouts.append(timeout)
            preset = argv[argv.index("--preset") + 1]
            if "--start" in argv:
                self.tails.append((preset, Path(argv[argv.index("--output") + 1]).suffix,
                                   argv[argv.index("--start") + 1], argv[argv.index("--duration") + 1]))
                preset = ("tail", preset)
            else:
                presets.append((preset, Path(argv[argv.index("--output") + 1]).suffix))
            code, size = outcomes.get(preset, (1, 0))
            if code == "hang":
                raise subprocess.TimeoutExpired(argv, timeout)
            if size:
                Path(argv[argv.index("--output") + 1]).write_bytes(b"\0" * size)
            return Done(code)

        folder = self.root / "out"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log",
                                       clock=lambda: float(seconds or 0))
        recorder.process = Process()
        recorder.raws, recorder.marks = [self.private / "sign-in-recording-1.mov"], [0.0]
        # Over the cap unless the test says otherwise: a part that fits is never converted.
        recorder.raws[0].write_bytes(b"\0" * (device_ios.MAX_RECORDING + 1) if raw is None else raw)
        if seconds is not None:
            recorder.started = 0.0
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

    def test_a_part_that_fits_is_published_as_recorded_without_any_conversion(self):
        """Run 40203481816: avconvert failed parts already under the cap (exit 205) (coordinator, 25 Oct)."""
        names, _, presets, folder, recorder, leftovers = self.recorder({}, raw=b"h264" * 10)
        self.assertEqual(presets, [], "no avconvert ran")
        self.assertEqual(names, ["sign-in.mp4"])
        self.assertEqual((folder / "sign-in.mp4").read_bytes(), b"h264" * 10)
        self.assertEqual((leftovers, recorder.dropped, recorder.kept, recorder.notes), ([], None, None, []))

    def test_nothing_that_fits_is_dropped_and_says_why(self):
        names, _, _, folder, recorder, leftovers = self.recorder(
            {"PresetAppleM4V720pHD": (0, 0), "Preset960x540": (0, self.BIG)}, raw=b"\0" * self.BIG)
        self.assertEqual((names, list(folder.iterdir()), leftovers), ([], [], []))
        # Each preset's outcome, so a public log tells a failed avconvert from a conversion still over the cap.
        self.assertEqual(recorder.dropped, "no conversion fit the cap (PresetAppleM4V720pHD wrote no file, "
                                           "Preset1280x720 failed (exit 1), Preset960x540 38 MB) and the original is "
                                           "38 MB, over it")

    # Run 40138900610's iOS chat, scaled to a 1,000-byte cap: 1809 s recorded, every conversion over the cap.
    RUN = {"PresetAppleM4V720pHD": (0, 1290), "Preset1280x720": (0, 2974), "Preset960x540": (0, 2579)}

    def test_a_recording_too_long_for_the_cap_keeps_its_last_part(self):
        """The stuck scenario is the one most worth watching (coordinator, 25 Oct)."""
        with patch("device_ios.MAX_RECORDING", 1000):
            names, _, _, folder, recorder, leftovers = self.recorder(
                {**self.RUN, ("tail", "PresetAppleM4V720pHD"): (0, 900)}, raw=b"\0" * 5474, seconds=1809)
        # The smallest whole conversion's preset, for the share of the recording that size suggests fits, less a
        # fifth: 1809 s x 1000 / 1290 x 0.8 is 1121 s, from 688 s on (the security read of #116: fixed numbers, so a
        # changed margin is caught).
        tail = 1121
        self.assertEqual(self.tails, [("PresetAppleM4V720pHD", ".m4v", "688", "1121")])
        self.assertEqual(self.timeouts, [device_ios.CONVERT_SECONDS] * 4, "no avconvert holds the job")
        self.assertEqual((names, (folder / "sign-in.mp4").stat().st_size, leftovers), (["sign-in.mp4"], 900, []))
        self.assertEqual(recorder.kept, f"as its last {tail} s alone: the whole did not fit the cap "
                                        "(PresetAppleM4V720pHD 0 MB, Preset1280x720 0 MB, Preset960x540 0 MB) after "
                                        "1809 s of recording")
        self.assertIsNone(recorder.dropped)

    def test_a_last_part_still_over_the_cap_is_dropped_as_before(self):
        with patch("device_ios.MAX_RECORDING", 1000):
            names, _, _, folder, recorder, leftovers = self.recorder(
                {**self.RUN, ("tail", "PresetAppleM4V720pHD"): (0, 1200)}, raw=b"\0" * 5474, seconds=1809)
        self.assertEqual((names, list(folder.iterdir()), leftovers, recorder.kept), ([], [], [], None))
        self.assertEqual(recorder.dropped, "no conversion fit the cap (PresetAppleM4V720pHD 0 MB, Preset1280x720 0 MB, "
                                           "Preset960x540 0 MB) and the original is 0 MB, over it; its last 1121 s "
                                           "alone: 0 MB after 1809 s of recording",
                         "the log says a tail was tried and refused")

    def test_a_conversion_that_hangs_is_given_up_and_named(self):
        with patch("device_ios.MAX_RECORDING", 1000):
            names, _, _, folder, recorder, leftovers = self.recorder(
                {**self.RUN, "PresetAppleM4V720pHD": ("hang", 0), ("tail", "Preset960x540"): ("hang", 0)},
                raw=b"\0" * 5474, seconds=1809)
        self.assertEqual((names, leftovers), ([], []))
        self.assertIn("PresetAppleM4V720pHD failed (timed out)", recorder.dropped)
        self.assertIn("its last 561 s alone: failed (timed out)", recorder.dropped)

    def test_the_last_part_is_never_shorter_than_a_minute_nor_tried_on_a_shorter_recording(self):
        with patch("device_ios.MAX_RECORDING", 1000):
            self.recorder({"PresetAppleM4V720pHD": (0, 10 ** 6)}, raw=b"\0" * 5474, seconds=1809)
            self.assertEqual(self.tails, [("PresetAppleM4V720pHD", ".m4v", str(1809 - device_ios.MIN_TAIL_SECONDS),
                                           str(device_ios.MIN_TAIL_SECONDS))])
            self.recorder({"PresetAppleM4V720pHD": (0, 10 ** 6)}, raw=b"\0" * 5474, seconds=device_ios.MIN_TAIL_SECONDS)
            self.assertEqual(self.tails, [], "a recording no longer than the tail has nothing to cut")
            self.recorder({}, raw=b"\0" * 5474, seconds=1809)
            self.assertEqual(self.tails, [], "no conversion worked, so none to cut with")

    def test_a_dropped_recording_says_how_long_it_ran(self):
        """The length sets each recording's bitrate against the cap, so the public log carries it in seconds."""
        ticks = iter([100.0])
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log",
                                       clock=lambda: next(ticks, 820.4))
        with patch("device_ios.subprocess.Popen"), patch("device_ios.subprocess.run", return_value=Done(1)):
            recorder.start()
            recorder.raws[0].write_bytes(b"\0" * self.BIG)
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

        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        with patch("test_device_ios.FakeRecorder", Dropping):
            _, _, report, out, _ = RunTests.run_it(self)
        self.assertIn("May device tests (ios): sign-in: its recording was dropped (over the cap at 51 MB)", out)
        self.assertEqual(report["scenarios"][0]["videos"], [])

    def test_the_recording_comes_in_parts_published_as_android_names_them(self):
        """Run 40181517145's 30-minute recordings failed every preset (coordinator, 25 Oct: 5-minute parts)."""
        processes, outputs = [], []

        class Process:
            def __init__(self, argv, **kwargs):
                outputs.append(Path(argv[-1]))
                Path(argv[-1]).write_bytes(b"raw")
                self.signals = []
                processes.append(self)

            def send_signal(self, number):
                self.signals.append(number)

            def wait(self, timeout):
                return 0

        def run(argv, stdout, stderr, stdin, env, timeout):
            Path(argv[argv.index("--output") + 1]).write_bytes(b"\0" * 100)
            return Done(0)

        recorder = device_ios.Recorder(UDID, {}, "chat", self.private, self.private / "log", part_seconds=0,
                                       clock=lambda: 0.0)
        with patch("device_ios.MAX_PARTS", 3), patch("device_ios.subprocess.Popen", side_effect=Process), \
                patch("device_ios.subprocess.run", side_effect=run):
            recorder.start()
            recorder.thread.join(timeout=10)
            self.assertTrue(recorder.halt())
            names = recorder.stop(self.root)
        self.assertEqual(names, ["chat-part1.mp4", "chat-part2.mp4", "chat-part3.mp4"])
        self.assertEqual([path.name for path in outputs], ["chat-recording-1.mov", "chat-recording-2.mov",
                                                           "chat-recording-3.mov"])
        self.assertEqual([process.signals for process in processes], [[signal.SIGINT]] * 3, "each part ended once")
        self.assertEqual((recorder.dropped, recorder.kept, recorder.notes), (None, None, []))
        self.assertEqual([path.name for path in self.private.iterdir() if path.name.startswith("chat-")], [])

    def test_each_part_says_how_it_was_kept_or_dropped(self):
        folder = self.root / "out"
        folder.mkdir()
        recorder = device_ios.Recorder(UDID, {}, "chat", self.private, self.private / "log", clock=lambda: 0.0)
        recorder.raws = [self.private / "chat-recording-1.mov", self.private / "chat-recording-2.mov",
                         self.private / "chat-recording-3.mov"]
        for raw, data in zip(recorder.raws, (b"\0" * self.BIG, b"b" * 10, b"\0" * self.BIG)):
            raw.write_bytes(data)
        recorder.marks, recorder.stopped = [0.0, 300.0, 600.0], 0.0
        sizes = iter([(0, 100), (1, 0), (1, 0), (1, 0)])
        converted = []

        def run(argv, stdout, stderr, stdin, env, timeout):
            converted.append(Path(argv[argv.index("--source") + 1]).name)
            code, size = next(sizes, (1, 0))
            if size:
                Path(argv[argv.index("--output") + 1]).write_bytes(b"\0" * size)
            stdout.write(b"Error Domain=AVFoundationErrorDomain Code=-11800 \"The operation could not be completed\" "
                         b"UserInfo={NSUnderlyingError=0x6000 {Error Domain=NSOSStatusErrorDomain Code=-12780}}\n")
            return Done(code)

        recorder.process = type("P", (), {"send_signal": lambda self, n: None, "wait": lambda self, timeout: 0})()
        with patch("device_ios.subprocess.run", side_effect=run):
            names = recorder.stop(folder)
        self.assertEqual(names, ["chat-part1.mp4", "chat-part2.mp4"])
        self.assertEqual((folder / "chat-part2.mp4").read_bytes(), b"b" * 10, "part 2 fit, so it is the recording")
        self.assertNotIn("chat-recording-2.mov", converted, "a part that fits is never converted")
        self.assertIsNone(recorder.kept)
        self.assertEqual(recorder.dropped, "part 3: no conversion fit the cap (PresetAppleM4V720pHD failed (exit 1), "
                                           "Preset1280x720 failed (exit 1), Preset960x540 failed (exit 1)) and the "
                                           "original is 38 MB, over it")
        self.assertEqual(recorder.notes[0], "part 3 PresetAppleM4V720pHD: exit 1; avconvert said: "
                                            "AVFoundationErrorDomain -11800, NSOSStatusErrorDomain -12780")
        self.assertEqual(len(recorder.notes), 3, "each failed conversion, privately")

    def test_the_scenarios_conversions_share_one_budget(self):
        now = [0.0]
        timeouts = []

        def run(argv, stdout, stderr, stdin, env, timeout):
            timeouts.append(timeout)
            now[0] += 400  # each conversion takes 400 s and fails
            stdout.write(b"conversion failed\n")
            return Done(1)

        recorder = device_ios.Recorder(UDID, {}, "chat", self.private, self.private / "log", clock=lambda: now[0])
        recorder.raws = [self.private / "chat-recording-1.mov", self.private / "chat-recording-2.mov"]
        for raw in recorder.raws:
            raw.write_bytes(b"\0" * self.BIG)
        recorder.marks, recorder.stopped = [0.0, 0.0], 0.0
        recorder.process = type("P", (), {"send_signal": lambda self, n: None, "wait": lambda self, timeout: 0})()
        with patch("device_ios.subprocess.run", side_effect=run):
            self.assertEqual(recorder.stop(self.root), [])
        self.assertEqual(timeouts, [device_ios.CONVERT_SECONDS, device_ios.CONVERSION_BUDGET_SECONDS - 400],
                         "the second conversion gets what is left, and none starts after that")
        self.assertIn("part 1: no conversion fit the cap (PresetAppleM4V720pHD failed (exit 1), Preset1280x720 failed "
                      "(exit 1), Preset960x540 not tried (the scenario's conversion budget was spent))", recorder.dropped)
        self.assertIn("part 2: no conversion fit the cap (PresetAppleM4V720pHD not tried", recorder.dropped)
        self.assertEqual(recorder.notes[:3], ["part 1 PresetAppleM4V720pHD: exit 1; avconvert said: conversion failed",
                                              "part 1 Preset1280x720: exit 1; avconvert said: conversion failed",
                                              "part 1 Preset960x540: not tried, the scenario's conversion budget was "
                                              "spent"])

    def test_avconvert_error_class(self):
        self.assertEqual(device_ios.error_class(b"Error Domain=AVFoundationErrorDomain Code=-11800 x Error "
                                                b"Domain=AVFoundationErrorDomain Code=-11800"),
                         "AVFoundationErrorDomain -11800")
        self.assertEqual(device_ios.error_class(b"one\n  last words  \n\n"), "last words")
        self.assertEqual(device_ios.error_class(b""), "nothing")

    def test_start_records_the_configured_simulator(self):
        recorder = device_ios.Recorder(UDID, {}, "sign-in", self.private, self.private / "log")
        with patch("device_ios.subprocess.Popen") as popen:
            recorder.start()
            # Halted here, so its part thread never wakes after the test's folder is gone.
            self.assertTrue(recorder.halt())
        self.assertFalse(recorder.thread.is_alive())
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
