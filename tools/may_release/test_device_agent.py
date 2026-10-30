"""Device-test agent scenarios (device-tests PR B): device_tests.py run and judge for agent rows, device_agent.py and
device_mcp.py. Claude Code, the emulator and staging are all faked; nothing here reaches the network."""

import base64
import datetime
import hashlib
import inspect
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import device_agent
from device_agent import AgentError
import device_ios
import device_mcp
import device_tests
import publish
import review
import store
from test_device_tests import network_tripwire, STDIN, FakeRecorder, SHA, TOKEN
from test_renders import png


TRIPWIRE = network_tripwire()


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()

OAUTH = "sk-ant-oat01-" + "o" * 40
MODEL = "claude-sonnet-5-5"


def record(outcome="completed", state="ok"):
    return {"state": state, "outcome": outcome, "summary": "saw it", "hierarchy": "text='x' | (1, 2)", "actions": 7}


class AgentRunTests(unittest.TestCase):
    """run then judge, for agent scenarios: a fresh session each, the tag, server truth, cleanup and the verdicts."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/app/build/outputs/apk/playground/debug").mkdir(parents=True)
        (self.source / device_tests.APK).write_bytes(b"apk")
        self.private.mkdir()
        self.env = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                    "MAY_STAGING_API_URL": "https://staging-api.usemay.co", "PATH": "/usr/bin:/bin",
                    "CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "MAY_DEVICE_TESTS_MODEL": MODEL, "GITHUB_RUN_ID": "39557415738"}

    def tearDown(self):
        self.directory.cleanup()

    def run_and_judge(self, records, judged, truth=(200, True), cleaned=3, env=None, selection="chat,notes,routines",
                      asked=None, exhausted=False, delivered=None, calls_left=None, fail_mint=(), noted=None,
                      made=None):
        calls, minted, seen = [], [], {}
        self.last_seen = seen

        def deliver(base, token, tag):
            seen.setdefault("delivered", []).append((token, tag))
            seen.setdefault("order", []).append(("read", token))
            return ["succeeded"]

        class Process:
            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            calls.append(list(argv))
            return 0

        def screenshot(adb, env, path, log):
            path.write_bytes(png())
            return path.name

        failures = list(fail_mint)

        def mint(base, secret, fresh=False):
            status = failures.pop(0) if failures else None
            if status is not None:
                seen.setdefault("mint_failed", []).append(status)
                raise device_tests.MintFailed(status)
            if fresh and exhausted:
                seen.setdefault("refused", []).append(secret)
                raise (device_tests.FreshRefused if exhausted == "not fresh" else device_tests.FreshExhausted)("no")
            minted.append(secret if not fresh else "fresh")
            return TOKEN + str(len(minted))

        def delete(base, token):
            seen.setdefault("deleted", []).append(token)
            seen.setdefault("order", []).append(("delete", token))
            return token != TOKEN + "2"

        def agent(cli, goal, tag, device, work, token, model, seconds):
            # The agent's process never sees the staging secret or the OAuth token in this step's environment.
            seen.setdefault("environ", set(os.environ))
            seen.setdefault("seconds", []).append(seconds)
            seen.setdefault("devices", []).append(device)
            seen.setdefault("goals", []).append((goal, tag, token, model, cli))
            if isinstance(records[len(seen["goals"]) - 1], Exception):
                raise records[len(seen["goals"]) - 1]
            return records[len(seen["goals"]) - 1]

        def clean(base, token, since, tag):
            seen["cleanup"] = (token, since, tag)
            return cleaned

        def judge(cli, goal, tag, agent_record, work, token, model):
            seen.setdefault("judged", []).append((goal, tag, token, model))
            seen.setdefault("evidence", []).append(agent_record)
            return judged.pop(0)

        environ = dict(self.env, **(env or {}))
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.network_ready", return_value=4), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.flows.sleep", side_effect=lambda s: seen.setdefault("slept", []).append(s)), \
                patch("device_tests.adb_shell", side_effect=lambda adb, env, log, line: calls.append(
                    [STDIN, *line.split()]) or 0), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.signed_out = []
            device_tests.run_scenarios(selection, minted=mint, checked=lambda *a: 200,
                                       revoked_by=lambda base, token: self.signed_out.append(token) or True,
                                       truth=lambda base, token, path, tag: truth,
                                       noted=noted or (lambda base, token, tag, note: (
                                           truth[0], "found" if truth[1] else "absent")),
                                       asked=asked or (lambda base, token, state, tag: truth),
                                       cleaned=clean, agent=agent, deleted_by=delete,
                                       delivered=delivered or deliver,
                                       calls_left=calls_left or (lambda base, token: 100),
                                       made=made or (lambda base, token, tag: seen.setdefault("chats", []).append(
                                           (token, tag, [argv[-1] for argv in calls if "start" in argv])) or True),
                                       install=lambda root: root / "may-claude" / "claude")
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)
            run_out = out.getvalue()
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
            device_tests.judge_scenarios(judged_by=judge)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "android" / "report.json").read_text())
        return calls, minted, seen, report, run_out, out.getvalue()[len(run_out):]

    def test_each_agent_scenario_gets_a_fresh_session_the_tag_and_server_truth(self):
        calls, minted, seen, report, run_out, judge_out = self.run_and_judge(
            [record(), record(), record()], [("pass", "fine <b>"), ("pass", ""), ("pass", "")])
        self.assertEqual(minted, ["s" * 32] * 4, "one session per scenario, and one for the cleanup")
        starts = [argv for argv in calls if "start" in argv]
        self.assertEqual([argv[-1] for argv in starts], [TOKEN + "1", TOKEN + "2", TOKEN + "3"])
        self.assertEqual(len([argv for argv in calls if "force-stop" in argv]), 3)
        goals = seen["goals"]
        self.assertEqual([tag for _, tag, _, _, _ in goals],
                         ["qa-39557415738-a-chat", "qa-39557415738-a-notes", "qa-39557415738-a-routines"],
                         "each scenario's own tag (coordinator, 25 Oct)")
        self.assertEqual([tag for _, tag, _, _ in seen["judged"]],
                         ["qa-39557415738-a-chat", "qa-39557415738-a-notes", "qa-39557415738-a-routines"],
                         "the judge reads the goal with the same tag the agent had")
        self.assertEqual([(token, model) for _, _, token, model, _ in goals], [(OAUTH, MODEL)] * 3)
        self.assertTrue(all("{tag}" in goal for goal, *_ in goals), "the goal goes to the agent with its placeholder")
        self.assertNotIn("MAY_TEST_SIGNIN_SECRET", seen["environ"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["environ"])
        self.assertIn("cleanup", seen, "the test session's cleanup ran")
        self.assertEqual(seen["cleanup"][0], TOKEN + "4")
        self.assertEqual(seen["cleanup"][2], "qa-39557415738-a")
        self.assertRegex(seen["cleanup"][1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["verdict"] for row in report["scenarios"]], ["pass", "pass", "pass"])
        self.assertNotIn("reason", report["scenarios"][0])
        self.assertEqual(report["scenarios"][0]["server"], [{"get": "/v1/agents", "status": 200, "pass": True}],
                         "chat's succeeded delivery is its server truth")
        self.assertEqual(report["scenarios"][1]["server"], [{"get": "/v1/commitments", "status": 200, "pass": True}],
                         "notes' tagged task holds the note on the server")
        self.assertEqual(report["scenarios"][2]["server"], [{"get": "/v1/routines", "status": 200, "pass": True},
                                                            {"get": "/v1/agents", "status": 200, "pass": True}])
        self.assertEqual(report["scenarios"][0]["notes"], "fine <b>")
        self.assertEqual([row.get("agent") for row in report["scenarios"]],
                         [{"outcome": "completed", "actions": 7, "summary": "saw it", "hierarchy_failure": None,
                           "hierarchy_cause": None, "refusal_cause": None}] * 3,
                         "the agent's own account, sealed into the private report")
        self.assertNotIn("saw it", run_out + judge_out, "never in the public log")
        self.assertEqual(seen.get("delivered"), [(TOKEN + "1", "qa-39557415738-a-chat"),
                                                 (TOKEN + "3", "qa-39557415738-a-routines")],
                         "chat and routines send May a message: their messages are read with their own session")
        self.assertEqual([row.get("delivery") for row in report["scenarios"]],
                         [["succeeded"], None, ["succeeded"]])
        self.assertEqual(run_out.splitlines(), [
            "May device tests (android): emulator unknown, system image revision unknown",
            "May device tests (android): network ready after 4 s",
            # This may-app fixture has no onboarding subflow, so each agent starts from launch and the log says so.
            *[f"May device tests (android): {scenario}: may-app has no onboarding subflow; the agent starts from launch"
              for scenario in ("chat", "notes", "routines")],
            "May device tests (android): 3 scenarios ran; verdicts follow the judge step",
            "May device tests (android): the test session's cleanup removed 3 tagged items",
            "May device tests (android): 4 test sessions were signed out at their end"])
        self.assertEqual(self.signed_out, [TOKEN + "1", TOKEN + "2", TOKEN + "3", TOKEN + "4"],
                         "each scenario's shared session at its end, then the cleanup's: none outlives the run")
        self.assertEqual(judge_out.splitlines(), [
            "May device tests (android): chat: pass", "May device tests (android): notes: pass",
            "May device tests (android): routines: pass",
            "May device tests (android): 3 passed, 0 passed on screen alone (server checks not configured), 0 failed, "
            "0 skipped"])
        for secret in (TOKEN, OAUTH, "s" * 32, "saw it", "fine"):
            self.assertNotIn(secret, run_out + judge_out)
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["pass", "pass", "pass"])

    def test_rate_limits_skip_and_never_pass(self):
        _, _, seen, report, _, _ = self.run_and_judge(
            [record(state="rate_limited", outcome=None), record(), record()],
            [("rate_limited", ""), ("pass", "")])
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("skipped", device_tests.RATE_LIMITED), ("skipped", device_tests.RATE_LIMITED), ("pass", None)])
        self.assertEqual(len(seen["judged"]), 2, "a rate-limited agent run is not judged")

    def test_blocked_unfinished_judge_fail_and_missing_truth_fail(self):
        _, _, _, report, _, _ = self.run_and_judge(
            [record(outcome="blocked"), record(outcome=None, state="timeout"), record()], [("fail", "no")])
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("fail", device_tests.BLOCKED), ("fail", device_tests.UNFINISHED), ("fail", None)])
        _, _, _, report, _, _ = self.reset_and(lambda: self.run_and_judge(
            [record(), record(), record()], [("pass", ""), ("error", ""), ("pass", "")], truth=(200, False)))
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("pass", None), ("fail", device_tests.NOT_JUDGED), ("fail", device_tests.NO_TRUTH)])

    def test_each_chat_scenario_talks_in_a_chat_of_its_own_made_before_the_app_starts(self):
        """Coordinator, 26 Oct: every run wrote to the shared May chat, so one run's cards and replies met the next's.
        A row with "chat" gets its own chat, named with its tag, made with its own session before the app starts; the
        test session's cleanup removes it with the run's tag."""
        _, _, seen, report, run_out, _ = self.run_and_judge([record(), record(), record()], [("pass", "")] * 3)
        self.assertEqual(seen.get("chats"), [(TOKEN + "1", "qa-39557415738-a-chat", []),
                                         (TOKEN + "3", "qa-39557415738-a-routines", [TOKEN + "1", TOKEN + "2"])],
                         "chat and routines alone, each with its own session and tag, each before its app starts")
        self.assertTrue(seen["cleanup"][2] in "qa-39557415738-a-chat chat", "the run's cleanup tag covers the chat")
        self.assertNotIn("qa-39557415738", run_out)
        refused = []
        _, _, seen, report, run_out, _ = self.reset_and(lambda: self.run_and_judge(
            [record()], [("pass", "")], made=lambda base, token, tag: refused.append(tag) or False))
        self.assertEqual(refused, ["qa-39557415738-a-chat", "qa-39557415738-a-routines"])
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "skipped", device_tests.NO_CHAT), ("notes", "pass", None),
            ("routines", "skipped", device_tests.NO_CHAT)],
            "a scenario whose chat staging did not make never runs in the shared chat instead")
        self.assertEqual([tag for _, tag, *_ in seen["goals"]], ["qa-39557415738-a-notes"])
        self.assertEqual(sorted(self.signed_out), [TOKEN + str(n) for n in range(1, 5)],
                         "a skipped scenario's session is signed out all the same")

    def test_server_truth_counts_chats_delivery_and_never_passes_a_task_it_could_not_reach(self):
        """Coordinator, 25 Oct: chat's succeeded delivery is its server truth; notes' tagged task beyond the agenda's
        first page is unverified, never a pass; a fail on the screen or any read is a fail."""
        seen_notes = []

        def beyond(base, token, tag, note):
            seen_notes.append((token, tag, note))
            return 200, "beyond"

        _, _, _, report, run_out, judge_out = self.run_and_judge(
            [record(), record(), record()], [("pass", "")] * 3, noted=beyond)
        self.assertEqual(seen_notes, [(TOKEN + "2", "qa-39557415738-a-notes task",
                                       "qa-39557415738-a-notes note from the device test")],
                         "the notes read, with notes' own session, its task's title and the goal's words")
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "pass", None), ("notes", device_tests.UNVERIFIED, device_tests.NOT_REACHED),
            ("routines", "pass", None)])
        self.assertEqual(report["scenarios"][1]["server"],
                         [{"get": "/v1/commitments", "status": 200, "pass": False, "unverified": True}])
        self.assertIn("May device tests (android): notes: unverified", judge_out)
        self.assertIn("May device tests (android): 1 could not be checked on the server (unverified, never a pass)",
                      judge_out)
        self.assertNotIn("note from the device test", run_out + judge_out, "the note's words are never printed")
        _, _, _, report, _, _ = self.reset_and(lambda: self.run_and_judge(
            [record(), record(), record()], [("pass", ""), ("fail", "no note on screen"), ("pass", "")], noted=beyond))
        self.assertEqual(report["scenarios"][1]["verdict"], "fail", "a fail on the screen outranks an unverified read")
        _, _, _, report, _, _ = self.reset_and(lambda: self.run_and_judge(
            [record(), record(), record()], [("pass", "")] * 3, noted=lambda *a: (200, "absent")))
        self.assertEqual((report["scenarios"][1]["verdict"], report["scenarios"][1]["reason"]),
                         ("fail", device_tests.NO_TRUTH), "a task the server does not hold is a fail")
        _, _, _, report, _, _ = self.reset_and(lambda: self.run_and_judge(
            [record(), record(), record()], [("pass", "")] * 3, delivered=lambda *a: None))
        self.assertEqual(report["scenarios"][0]["server"], [{"get": "/v1/agents", "status": 0, "pass": False}],
                         "chats that could not be read are no delivery truth")
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text())[0], "fail")

    def test_the_judge_sees_delivery_codes_alone_and_a_failed_reply_is_never_a_pass(self):
        """Coordinator, 24 Oct: the server's record outranks the screen; a failed code can't be judged into a pass."""
        codes = iter([["failed (model_rejected_request)"], ["succeeded"]])
        _, _, seen, report, _, judge_out = self.run_and_judge(
            [record(), record(), record()], [("pass", "it replied"), ("pass", ""), ("pass", "")],
            delivered=lambda base, token, tag: next(codes))
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("fail", device_tests.DELIVERY_FAILED), ("pass", None), ("pass", None)])
        self.assertEqual([evidence.get("truth") for evidence in seen["evidence"]],
                         [["every tagged message this run sent has a succeeded reply: not confirmed"],
                          ["the task titled with the tag holds the goal's note and none of that title is left "
                           "open: confirmed"],
                          ["an item holding the tag is listed by GET /v1/routines: confirmed",
                           "May's question card in this run's tagged chat is answered: confirmed"]],
                         "the judge sees what each server read checked and how it came out")
        self.assertEqual([(evidence.get("sends"), evidence.get("delivery")) for evidence in seen["evidence"]],
                         [(True, ["failed (model_rejected_request)"]), (False, None), (True, ["succeeded"])],
                         "each scenario's own codes, and nothing for one that sends no message")
        self.assertIn("May device tests (android): chat: fail", judge_out)
        prompt = device_agent.JUDGE_PROMPT.format(goal="g", outcome="completed", actions=1, summary="s",
                                                  hierarchy="h", delivery="succeeded", truth="t")
        self.assertIn("delivery codes only", prompt)
        self.assertIn("read when it finished", prompt)

    def test_the_recording_is_pulled_after_the_server_reads_and_diagnostics(self):
        """Run 40181517145 (coordinator, 25 Oct): the server reads come before any recording is pulled or
        converted; the recording itself is ended first, so the reads are not in it."""
        test = self

        class Fake:
            def __init__(self, adb, env):
                self.kept = self.anrs = self.crashes = self.network = 0
                self.dropped = {}

            def keep(self, folder, scenario, token):
                test.last_seen.setdefault("order", []).append(("diagnostics", token))

            def report(self, platform):
                pass

        class Ordered(FakeRecorder):
            def halt(self):
                test.last_seen.setdefault("order", []).append(("halt", self.scenario))

            def stop(self, folder):
                test.last_seen.setdefault("order", []).append(("pull", self.scenario))
                return super().stop(folder)

        with patch("device_tests.Diagnostics", Fake), patch("test_device_agent.FakeRecorder", Ordered):
            _, _, seen, report, _, _ = self.run_and_judge([record()], [("pass", "")], selection="chat")
        self.assertEqual([step for step, _ in seen.get("order", [])], ["halt", "read", "diagnostics", "pull"])
        self.assertEqual(report["scenarios"][0]["videos"], ["chat.mp4"])

    def test_each_platform_has_its_own_run_tag(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "39557415738"}):
            android, ios = device_tests.run_tag("android"), device_tests.run_tag("ios")
        self.assertEqual((android, ios), ("qa-39557415738-a", "qa-39557415738-i"))
        row = {"id": "notes"}
        self.assertNotIn(android, device_tests.scenario_tag(ios, row), "Android's cleanup never matches iOS's items")
        self.assertNotIn(ios, device_tests.scenario_tag(android, row))
        self.assertTrue(device_tests.holds(device_tests.scenario_tag(android, row) + " task",
                                           device_tests.scenario_tag(android, row)))
        self.assertFalse(device_tests.holds(device_tests.scenario_tag(ios, row) + " task",
                                            device_tests.scenario_tag(android, row)),
                         "run 40181517145: iOS's notes passed on Android's task")

    def test_diagnostics_wait_until_the_server_reads_are_done(self):
        """The security read of #108: a slow dumpsys must not spend the session the server reads still need."""
        test = self

        class Fake:
            def __init__(self, adb, env):
                self.kept = self.anrs = self.crashes = self.network = 0
                self.dropped = {}

            def keep(self, folder, scenario, token):
                test.last_seen.setdefault("order", []).append(("diagnostics", token))

        with patch("device_tests.Diagnostics", Fake):
            _, _, seen, _, _, _ = self.run_and_judge([record(), record(), record()],
                                                     [("pass", ""), ("pass", ""), ("pass", "")])
        order = seen["order"]
        reads = [token for step, token in order if step == "read"]
        self.assertEqual(len([step for step in order if step[0] == "diagnostics"]), 3)
        for token in reads:
            self.assertLess(order.index(("read", token)), order.index(("diagnostics", token)),
                            "each scenario's diagnostics after its own delivery read")

    def test_the_judges_note_is_stored_redacted(self):
        _, _, _, report, _, _ = self.run_and_judge(
            [record(), record(), record()],
            [("pass", "Saw the reply.\ntoken=abc123"), ("pass", ""), ("pass", "")])
        self.assertEqual(report["scenarios"][0].get("notes"), "Saw the reply.")

    def test_the_poll_scenarios_read_the_question_card_in_the_tagged_chat(self):
        asked = []

        def ask(base, token, state, tag):
            asked.append((state, tag))
            return (200, state == "answered")

        try:
            _, _, _, report, _, _ = self.run_and_judge([record(), record()], [("pass", ""), ("pass", "")],
                                                       selection="poll,poll-skip", asked=ask)
        except (KeyError, TypeError) as error:
            self.fail("a question truth row was not read as a question: " + type(error).__name__)
        self.assertEqual(asked, [("answered", "qa-39557415738-a-poll"), ("closed", "qa-39557415738-a-poll-skip")])
        self.assertEqual([device.get("MAY_DEVICE_STEPS") for device in self.last_seen["devices"]], ["120", "120"],
                         "both have 120 to type slowly (run 40232904242: poll ran out at 80)")
        self.assertEqual([(row["verdict"], row.get("reason"), row["server"]) for row in report["scenarios"]], [
            ("pass", None, [{"get": "/v1/agents", "status": 200, "pass": True}]),
            ("fail", device_tests.NO_TRUTH, [{"get": "/v1/agents", "status": 200, "pass": False}])])

    def reset_and(self, again):
        import shutil
        shutil.rmtree(self.root / device_tests.OUT)
        for name in (device_tests.VERDICTS, device_tests.AGENTS):
            (self.private / name).unlink(missing_ok=True)
        return again()

    def test_a_switched_off_scenario_is_skipped_and_never_run(self):
        try:
            _, minted, seen, report, _, _ = self.run_and_judge([record(), record()], [("pass", ""), ("pass", "")],
                                                               env={"MAY_DEVICE_TESTS_SKIP": "routines, ../x,chat"})
        except IndexError:
            self.fail("a switched-off scenario ran")
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "skipped", device_tests.SWITCHED_OFF), ("notes", "pass", None),
            ("routines", "skipped", device_tests.SWITCHED_OFF)])
        self.assertEqual(len(seen["goals"]), 1, "only notes ran")

    def test_each_agent_drives_for_its_rows_own_seconds_or_the_default(self):
        """Run 40290647340: onboarding took its whole agent time; its row now gives it 600 s (coordinator, 26 Oct)."""
        self.run_and_judge([record()] * 2, [("pass", "")] * 2, selection="chat,onboarding")
        self.assertEqual(self.last_seen["seconds"], [device_agent.AGENT_SECONDS, 600])

    def test_a_fresh_scenario_runs_on_its_own_account_and_deletes_it_with_its_own_session(self):
        _, minted, seen, report, run_out, _ = self.run_and_judge(
            [record()] * 3, [("pass", "")] * 3, env={device_tests.FRESH: "chat"})
        self.assertEqual(minted, ["fresh", "s" * 32, "s" * 32, "s" * 32],
                         "chat on a fresh account; notes and routines, and their run-wide cleanup, on the shared one")
        self.assertEqual(seen.get("deleted"), [TOKEN + "1"], "only chat's account, with chat's own session")
        self.assertEqual(seen["order"][:2], [("read", TOKEN + "1"), ("delete", TOKEN + "1")],
                         "chat's messages are read before its account is deleted")
        self.assertEqual(seen["cleanup"][0], TOKEN + "4")
        self.assertIn("May device tests (android): 1 fresh test accounts were deleted by their cleanup", run_out)

    def test_every_scenario_fresh_needs_no_shared_cleanup_and_counts_what_did_not_answer(self):
        _, minted, seen, report, run_out, _ = self.run_and_judge(
            [record()] * 3, [("pass", "")] * 3, env={device_tests.FRESH: "all"})
        self.assertEqual(minted, ["fresh"] * 3)
        self.assertEqual(seen.get("deleted"), [TOKEN + "1", TOKEN + "2", TOKEN + "3"])
        self.assertNotIn("cleanup", seen, "nothing ran on the shared account")
        self.assertEqual(run_out.splitlines()[-2:], [
            "May device tests (android): 2 fresh test accounts were deleted by their cleanup",
            "May device tests (android): 1 fresh test accounts' cleanup did not answer; staging sweeps them after 2 "
            "hours"])
        self.assertNotIn("the test session's cleanup", run_out)

    def test_used_up_fresh_accounts_skip_with_their_own_reason_and_never_fall_back_to_the_shared_one(self):
        try:
            _, minted, seen, report, run_out, _ = self.run_and_judge(
                [record()], [("pass", "")], env={device_tests.FRESH: "chat,routines"}, exhausted=True)
        except IndexError:
            self.fail("a scenario whose fresh account was refused ran anyway")
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "skipped", device_tests.FRESH_EXHAUSTED), ("notes", "pass", None),
            ("routines", "skipped", device_tests.FRESH_EXHAUSTED)])
        self.assertEqual(len(seen.get("refused", [])), 1, "once used up, staging is not asked again")
        self.assertEqual(minted, ["s" * 32, "s" * 32], "notes and the cleanup alone, on the shared account")
        self.assertNotIn("deleted", seen)

    def test_unreadable_chats_leave_no_delivery_in_the_row(self):
        _, _, _, report, _, _ = self.run_and_judge([record()] * 3, [("pass", "")] * 3,
                                                   delivered=lambda base, token, tag: None)
        self.assertEqual([row.get("delivery") for row in report["scenarios"]], [None, None, None])

    def test_a_reply_that_is_not_fresh_is_never_used_or_deleted(self):
        try:
            _, minted, seen, report, run_out, _ = self.run_and_judge(
                [record()], [("pass", "")], env={device_tests.FRESH: "chat,routines"}, exhausted="not fresh")
        except IndexError:
            self.fail("a scenario whose fresh account was refused ran anyway")
        except device_tests.FreshRefused:
            self.fail("a reply that is not fresh stopped the run instead of skipping its scenario")
        self.assertEqual([(row["id"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", device_tests.NOT_FRESH), ("notes", None), ("routines", device_tests.NOT_FRESH)])
        self.assertEqual(len(seen.get("refused", [])), 1)
        self.assertNotIn("deleted", seen, "a session that is not on a fresh account never reaches delete_fresh")

    def test_a_fresh_account_is_deleted_when_its_scenario_stops_part_way(self):
        with self.assertRaises(OSError):
            self.run_and_judge([OSError("agent died")], [], env={device_tests.FRESH: "chat"}, selection="chat")
        self.assertEqual(self.last_seen.get("deleted"), [TOKEN + "1"], "the run's finally deleted chat's account")

    def test_a_run_that_stops_part_way_still_cleans_up_its_tag(self):
        """Independent review of #132: the tag cleanup ran only on completion, so a run that stopped part-way left its
        "<tag> chat" agents and items on the shared account. It runs in the finally, before the sessions are signed
        out."""
        with self.assertRaises(RuntimeError):
            self.run_and_judge([record(), RuntimeError("the emulator went away"), record()], [])
        seen = self.last_seen
        self.assertEqual(seen.get("cleanup", (None,))[0:3:2], (TOKEN + "3", "qa-39557415738-a"),
                         "the run's tag, with a session minted for the cleanup")
        self.assertIn(TOKEN + "3", self.signed_out, "and that session signed out by abandon after it")
        self.assertEqual([tag for _, tag, *_ in seen["goals"]], ["qa-39557415738-a-chat", "qa-39557415738-a-notes"])

    def test_an_agent_scenario_near_the_model_call_cap_is_skipped_and_never_run(self):
        read = []

        def left(base, token):
            read.append(token)
            return device_tests.MODEL_CALL_MARGIN - 1 if token == TOKEN + "1" else device_tests.MODEL_CALL_MARGIN

        try:
            _, minted, seen, report, _, _ = self.run_and_judge([record(), record()], [("pass", ""), ("pass", "")],
                                                               calls_left=left)
        except IndexError:
            self.fail("a scenario near the model-call cap ran")
        self.assertEqual(read, [TOKEN + "1", TOKEN + "2", TOKEN + "3"], "read with each scenario's own session")
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "skipped", device_tests.MODEL_CAP), ("notes", "pass", None),
            ("routines", "pass", None)])
        self.assertEqual([goal[1] for goal in seen["goals"]], ["qa-39557415738-a-notes", "qa-39557415738-a-routines"],
                         "chat's agent never ran")

    def test_a_fresh_account_skipped_near_the_cap_is_deleted_then_and_there(self):
        try:
            _, _, seen, report, _, _ = self.run_and_judge(
                [record()], [("pass", "")], selection="chat,notes", env={device_tests.FRESH: "all"},
                calls_left=lambda base, token: 0 if token == TOKEN + "1" else 100)
        except IndexError:
            self.fail("a scenario near the model-call cap ran")
        self.assertEqual([row.get("reason") for row in report["scenarios"]],
                         [device_tests.MODEL_CAP, None])
        self.assertEqual(seen.get("deleted"), [TOKEN + "1", TOKEN + "2"],
                         "chat's fresh account is deleted when it is skipped, before notes' is minted")

    def test_a_mint_that_fails_once_is_retried_and_the_scenario_runs(self):
        try:
            _, minted, seen, report, _, _ = self.run_and_judge([record()] * 3, [("pass", "")] * 3,
                                                               fail_mint=(None, 502))
        except device_tests.DeviceTestsError as error:
            self.fail("a mint that failed once stopped the run: " + error.args[0])
        self.assertEqual(seen["mint_failed"], [502])
        self.assertEqual(seen.get("slept"), [device_tests.MINT_RETRY_SECONDS], "one pause, then the one retry")
        self.assertEqual([row.get("reason") for row in report["scenarios"]],
                         [None, None, None])

    def test_a_mint_that_fails_twice_skips_its_scenario_alone_and_the_report_is_kept(self):
        """Run 40077167994: one failed mint 50 minutes into the iOS run stopped it, and the report was lost."""
        try:
            _, _, seen, report, run_out, _ = self.run_and_judge([record()] * 2, [("pass", "")] * 2,
                                                                fail_mint=(None, 429, 429))
        except device_tests.DeviceTestsError as error:
            self.fail("a failed mint stopped the run: " + error.args[0])
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "pass", None), ("notes", "skipped", device_tests.MINT_FAILED),
            ("routines", "pass", None)])
        self.assertIn("May device tests (android): notes: staging did not mint a session (HTTP status 429, 0 for no "
                      "reply), twice; the scenario is skipped", run_out)
        self.assertEqual(len(seen["goals"]), 2, "notes' agent never ran")

    def test_a_skipped_scenario_leaves_no_session_for_the_next_one(self):
        """Coordinator, review of the mint fix: each scenario's session stays empty until its own mint succeeds."""
        import shutil
        # (fresh, failed mints in order, the sessions launched, the fresh accounts deleted)
        cases = (({}, (None, 503, 503), [TOKEN + "1", TOKEN + "2"], None),
                 ({device_tests.FRESH: "all"}, (None, 503, 503), [TOKEN + "1", TOKEN + "2"], [TOKEN + "1", TOKEN + "2"]),
                 ({device_tests.FRESH: "all"}, (None, None, 503, 503), [TOKEN + "1", TOKEN + "2"],
                  [TOKEN + "1", TOKEN + "2"]))
        for env, fails, launched, deleted in cases:
            shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
            for name in (device_tests.VERDICTS, device_tests.AGENTS):
                (self.private / name).unlink(missing_ok=True)
            with self.subTest(fresh=bool(env), fails=fails):
                try:
                    calls, _, seen, _, _, _ = self.run_and_judge([record()] * 2, [("pass", "")] * 2, env=env,
                                                                 fail_mint=fails)
                except IndexError:
                    self.fail("a scenario whose session was not minted ran")
                starts = [argv[-1] for argv in calls if argv and argv[0] == STDIN and "start" in argv]
                self.assertEqual(starts, launched, "each scenario launches with its own session; a skipped one never does")
                self.assertEqual(seen.get("deleted"), deleted, "only the accounts actually minted are deleted, once each")

    def test_a_refused_secret_still_stops_the_run_and_says_its_status(self):
        import shutil
        for status in device_tests.MINT_FATAL:
            shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
            for name in (device_tests.VERDICTS, device_tests.AGENTS):
                (self.private / name).unlink(missing_ok=True)
            with self.subTest(status=status):
                with self.assertRaises(device_tests.MintFailed) as stopped:
                    self.run_and_judge([record()] * 3, [("pass", "")] * 3, fail_mint=(status,))
                self.assertEqual(stopped.exception.args[0],
                                 f"The staging test session could not be minted (HTTP {status})")

    def test_a_cleanup_whose_session_is_not_minted_keeps_the_report(self):
        try:
            _, _, seen, report, run_out, _ = self.run_and_judge([record()] * 3, [("pass", "")] * 3,
                                                                fail_mint=(None, None, None, 503))
        except device_tests.DeviceTestsError as error:
            self.fail("the cleanup's failed mint stopped the run: " + error.args[0])
        self.assertNotIn("cleanup", seen, "no cleanup without a session")
        self.assertIn("the test session's cleanup did not answer; tagged items may remain", run_out)
        self.assertEqual(len(report["scenarios"]), 3)

    def test_a_cleanup_that_does_not_answer_is_said(self):
        _, _, _, _, run_out, _ = self.run_and_judge([record()] * 3, [("pass", "")] * 3, cleaned=None)
        self.assertIn("the test session's cleanup did not answer; tagged items may remain", run_out)

    def test_refusals(self):
        for env, message in (({"MAY_DEVICE_TESTS_MODEL": "gpt-5"}, "MAY_DEVICE_TESTS_MODEL is not a Claude model"),
                             ({"CLAUDE_CODE_OAUTH_TOKEN": None}, "CLAUDE_CODE_OAUTH_TOKEN is not set"),
                             ({"MAY_APP_RENDERS_WRITE_KEY": "x"}, "A secret for another release step")):
            with self.subTest(message=message):
                with self.assertRaises((AgentError, store.StoreError)) as refused:
                    self.run_and_judge([record()] * 3, [("pass", "")] * 3, env=env)
                self.assertIn(message, refused.exception.args[0])


def chat_server(chats, messages, agents_status=200):
    """A fake staging for the chat readers: GET /v1/agents, and each chat's messages oldest first, served page by page
    as the API does (?after=<seq>&limit=<n>, next_cursor the page's last seq). Each message gets its seq here."""
    import urllib.parse
    seen = []
    numbered = {path: [dict(m, seq=str(at + 1)) for at, m in enumerate(rows)] for path, rows in messages.items()}

    def get(request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        seen.append(url.path)
        if url.path == "/v1/agents":
            if agents_status != 200:
                raise urllib.error.HTTPError(request.full_url, agents_status, "no", {}, None)
            return FakeReply(json.dumps({"agents": chats}).encode())
        query = dict(urllib.parse.parse_qsl(url.query))
        after, limit = int(query.get("after", "0")), int(query.get("limit", "100"))
        page = [m for m in numbered.get(url.path, []) if int(m["seq"]) > after][:limit]
        return FakeReply(json.dumps({"messages": page, "next_cursor": page[-1]["seq"] if page else str(after)}).encode())
    return get, seen


class ServerTruthTests(unittest.TestCase):
    def test_the_note_is_read_from_the_task_titled_with_the_tag_which_is_left_closed(self):
        """may-app: GET /v1/commitments gives {commitments, next_cursor}, 200 to a page; a task's notes are its
        description, and a deleted (cancelled) task is still listed. Coordinator, 26 Oct: the note must be in the task
        titled exactly "<tag> task", and none of that title may be left open, so a note typed into another task (run
        40243708096's "file taxes") never passes."""
        tag, note = "qa-39557415738-notes task", "qa-39557415738-notes note from the device test"
        asked = []

        def get(body, status=200):
            def reply(request, timeout):
                asked.append(request.full_url)
                if status != 200:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                return FakeReply(json.dumps(body).encode())
            return reply

        mine = {"title": "qa-39557415738-notes task", "description": note, "status": "cancelled"}
        other = {"title": "qa-39557415738-notes-old task", "description": note, "status": "cancelled"}
        taxes = {"title": "file taxes", "description": "keep receipts\n" + note, "status": "accepted"}
        cases = (({"commitments": [{"title": "file taxes", "description": None}, mine], "next_cursor": None},
                  200, (200, "found")),
                 ({"commitments": [dict(mine, status="completed")], "next_cursor": None}, 200, (200, "found")),
                 ({"commitments": [dict(mine, title=" QA-39557415738-notes Task ")], "next_cursor": None},
                  200, (200, "found")),
                 ({"commitments": [dict(mine, status="accepted")], "next_cursor": None}, 200, (200, "open")),
                 ({"commitments": [mine, dict(mine, description=None, status="accepted")], "next_cursor": None},
                  200, (200, "open")),
                 ({"commitments": [dict(mine, description="qa-39557415738-notes note from the d")], "next_cursor": None},
                  200, (200, "absent")),
                 ({"commitments": [taxes], "next_cursor": None}, 200, (200, "absent")),
                 ({"commitments": [dict(mine, title="qa-39557415738-notes task copy")], "next_cursor": None},
                  200, (200, "absent")),
                 ({"commitments": [dict(mine, description=None)], "next_cursor": "c2"}, 200, (200, "absent")),
                 ({"commitments": [other, taxes], "next_cursor": "c2"}, 200, (200, "beyond")),
                 ({"commitments": [other], "next_cursor": None}, 200, (200, "absent")),
                 ({"commitments": [], "next_cursor": ""}, 200, (200, "absent")),
                 ({"commitments": [mine]}, 401, (401, "absent")),
                 ([mine], 200, (200, "found")))
        for body, status, expected in cases:
            with self.subTest(body=body, status=status):
                self.assertEqual(device_tests.commitment_note("https://s", TOKEN, tag, note, get=get(body, status)),
                                 expected)
        self.assertEqual(set(asked), {"https://s/v1/commitments"}, "the first page alone, no cursor")

    def test_make_chat_posts_the_tagged_name_with_the_session_and_reads_its_id(self):
        seen = []

        def post(status, body):
            def answer(request, timeout):
                seen.append((request.full_url, request.get_method(), json.loads(request.data),
                             request.headers.get("Authorization")))
                if status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                reply = FakeReply(json.dumps(body).encode())
                reply.status = status
                return reply
            return answer

        tag = "qa-39557415738-a-chat"
        self.assertEqual(device_tests.make_chat("https://s", TOKEN, tag, post(201, {"agent": {"id": "a7"}})), "a7")
        self.assertEqual(seen, [("https://s/v1/agents", "POST", {"client_key": tag, "name": tag + " chat"},
                                 "Bearer " + TOKEN)])
        self.assertTrue(device_tests.make_chat("https://s", TOKEN, tag, post(200, {"agent": {"id": "a7"}})),
                        "a repeat with the same key is the same chat")
        for status, body in ((201, {"agent": {}}), (201, {"agent": {"id": "../x"}}), (201, []), (403, {}),
                             (202, {"agent": {"id": "a7"}})):
            with self.subTest(status=status, body=body):
                self.assertIsNone(device_tests.make_chat("https://s", TOKEN, tag, post(status, body)))

    def test_the_chat_gets_its_untagged_first_message_over_the_api_before_the_agent_opens_it(self):
        """Run 40290647340: the iOS driver refused every act on the new chat's empty state; the chat now opens with a
        message the API sent (coordinator, 26 Oct)."""
        seen = []

        def post(answers):
            def answer(request, timeout):
                seen.append((request.full_url, request.get_method(), json.loads(request.data),
                             request.headers.get("Authorization")))
                status, body = answers.pop(0)
                if status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                reply = FakeReply(json.dumps(body).encode())
                reply.status = status
                return reply
            return answer

        tag, chat = "qa-39557415738-i-poll", "0f0e0d0c-0b0a-4908-8706-050403020100"
        self.assertIs(device_tests.open_chat("https://s", TOKEN, tag, post([(201, {"agent": {"id": chat}}),
                                                                             (202, {"message": {}})])), True)
        self.assertEqual([url for url, *_ in seen], ["https://s/v1/agents", f"https://s/v1/conversations/{chat}/messages"])
        self.assertEqual(seen[1], (f"https://s/v1/conversations/{chat}/messages", "POST",
                                   {"client_message_id": tag + "-first", "body": device_tests.FIRST_MESSAGE},
                                   "Bearer " + TOKEN))
        self.assertNotIn("qa-", device_tests.FIRST_MESSAGE, "untagged: no truth or delivery code counts it")
        self.assertEqual(device_tests.run_messages([{"text": device_tests.FIRST_MESSAGE}, {"text": "ready"},
                                                    {"text": "hello " + tag}], tag), [{"text": "hello " + tag}])
        self.assertIs(device_tests.open_chat("https://s", TOKEN, tag, post([(201, {"agent": {"id": chat}}),
                                                                             (200, {"replayed": True})])), True,
                      "a repeat with the same key is the same message")
        for answers, reason in (([(403, {})], device_tests.NO_CHAT),
                                ([(201, {"agent": {"id": chat}}), (429, {})], device_tests.NO_FIRST),
                                ([(201, {"agent": {"id": chat}}), (201, {})], device_tests.NO_FIRST)):
            with self.subTest(answers=answers):
                seen.clear()
                self.assertEqual(device_tests.open_chat("https://s", TOKEN, tag, post(list(answers))), reason)
        self.assertIn(device_tests.NO_FIRST, publish.REASONS)

    def test_both_platforms_open_each_chat_with_its_first_message_by_default(self):
        for run in (device_tests.run_scenarios, device_ios.run_scenarios):
            with self.subTest(run=run.__module__):
                self.assertIs(inspect.signature(run).parameters["made"].default, device_tests.open_chat)

    def test_a_chat_without_its_first_message_skips_its_scenario_with_that_reason(self):
        for opened, reason in ((device_tests.NO_FIRST, device_tests.NO_FIRST), (False, device_tests.NO_CHAT),
                               (device_tests.NO_CHAT, device_tests.NO_CHAT)):
            with self.subTest(opened=opened), patch("sys.stdout", io.StringIO()):
                entry = {}
                self.assertFalse(device_tests.own_chat({"chat": True, "id": "poll"}, entry, "https://s", TOKEN, "qa-1",
                                                       lambda base, token, tag: opened))
                self.assertEqual(entry["reason"], reason)

    def test_tidy_removes_only_whole_leftover_note_lines_from_file_taxes(self):
        """Coordinator, 26 Oct: earlier notes runs left their lines in the shared account's "file taxes" task."""
        note = "qa-40243708096-i-notes note from the device test"
        text = "\n".join(["Gather W-2s", note, note + ". " + note.upper(), "  " + note + "  ",
                          "call the accountant about " + note, "qa-1 notes", ""])
        kept, removed, mixed = device_tests.tidy_lines(text)
        self.assertEqual((kept, removed, mixed),
                         ("\n".join(["Gather W-2s", "call the accountant about " + note, "qa-1 notes", ""]), 3, 1))

    def test_tidy_removes_notes_glued_into_one_line_with_a_stray_character_or_two(self):
        """Run 40290565440: the leftover was two notes glued into one line, with a stray character between them; it was
        kept as mixed (coordinator, 26 Oct). A line goes when its notes leave at most MAX_STRAY other characters."""
        glued = ".qa-40063426822 note from the device testkqa-40057045702 note from the device test"
        three = " ".join(f"qa-{n} note from the device test." for n in (1, 2, 3))
        for line, gone in ((glued, True), (glued + " ", True), (three, True), ("xy" + glued, False),
                           (glued + " ok", False), ("Pay: qa-1 note from the device test", False),
                           ("ab", False), ("qa-1 note", False)):
            with self.subTest(line=line):
                kept, removed, mixed = device_tests.tidy_lines("Gather W-2s\n" + line)
                self.assertEqual((kept, removed, mixed),
                                 ("Gather W-2s", 1, 0) if gone else
                                 ("Gather W-2s\n" + line, 0, int("note from the device test" in line)))

    def test_tidy_saves_the_glued_line_away(self):
        glued = ".qa-40063426822 note from the device testkqa-40057045702 note from the device test"
        agenda = {"/v1/commitments": {"commitments": [
            {"id": "11111111-1111-1111-1111-111111111111", "title": "file taxes", "revision": 9,
             "description": "Gather W-2s\n" + glued, "status": "accepted"}], "next_cursor": None}}
        saved = []

        def patch_(request, timeout):
            saved.append(json.loads(request.data))
            return FakeReply(b"{}")

        counts = device_tests.tidy_notes("https://s", TOKEN, get=lambda request, timeout: FakeReply(
            json.dumps(agenda["/v1/commitments"]).encode()), post=patch_)
        self.assertEqual((saved, counts), ([{"expected_revision": 9, "description": "Gather W-2s"}],
                                           {"tasks": 1, "lines": 1, "mixed": 0, "saved": 1, "failed": 0}))

    def test_tidy_reads_every_page_and_saves_at_each_revision(self):
        note = "qa-40243708096-i-notes note from the device test"
        agenda = {"/v1/commitments": {"commitments": [
                      {"id": "11111111-1111-1111-1111-111111111111", "title": "File taxes ", "revision": 4,
                       "description": "  Gather W-2s  \n" + note, "status": "accepted"},
                      {"id": "44444444-4444-4444-4444-444444444444", "title": "file taxes", "revision": 7,
                       "description": note + "\n  ", "status": "accepted"},
                      {"id": "22222222-2222-2222-2222-222222222222", "title": "qa-1 task", "revision": 1,
                       "description": note}], "next_cursor": "p2"},
                  "/v1/commitments?cursor=p2": {"commitments": [
                      {"id": "33333333-3333-3333-3333-333333333333", "title": "file taxes", "revision": 2,
                       "description": "only mine"}], "next_cursor": None}}
        asked, saved = [], []

        def get(request, timeout):
            asked.append(request.full_url[len("https://s"):])
            return FakeReply(json.dumps(agenda[asked[-1]]).encode())

        def patch_(status):
            def answer(request, timeout):
                saved.append((request.full_url, request.get_method(), json.loads(request.data)))
                if status != 200:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                return FakeReply(b"{}")
            return answer

        counts = device_tests.tidy_notes("https://s", TOKEN, get=get, post=patch_(200))
        self.assertEqual(asked, ["/v1/commitments", "/v1/commitments?cursor=p2"], "every page of the Agenda")
        self.assertEqual(saved, [("https://s/v1/commitments/11111111-1111-1111-1111-111111111111", "PATCH",
                                  {"expected_revision": 4, "description": "  Gather W-2s  "}),
                                 ("https://s/v1/commitments/44444444-4444-4444-4444-444444444444", "PATCH",
                                  {"expected_revision": 7, "description": None})],
                         "the file taxes tasks alone, at their revisions, with their own lines kept as they were "
                         "(independent review of #132: no trimming), and none when only whitespace is left")
        self.assertEqual(counts, {"tasks": 3, "lines": 2, "mixed": 0, "saved": 2, "failed": 0})
        saved.clear()
        self.assertEqual(device_tests.tidy_notes("https://s", TOKEN, get=get, post=patch_(409))["failed"], 2)
        self.assertIsNone(device_tests.tidy_notes("https://s", TOKEN, get=lambda r, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(r.full_url, 401, "no", {}, None)), post=patch_(200)))

    def test_the_tidy_step_prints_counts_and_signs_its_session_out(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environ = {"RUNNER_TEMP": directory.name, "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                   "MAY_STAGING_API_URL": device_tests.STAGING_BASE, "PATH": "/usr/bin"}
        signed_out = []
        counts = {"tasks": 1, "lines": 3, "mixed": 1, "saved": 1, "failed": 0}
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.tidy(minted=lambda base, secret: TOKEN, tidied=lambda base, token: counts,
                              revoked_by=lambda base, token: signed_out.append(token) or True)
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
        self.assertEqual(out.getvalue(), "May device tests: 1 file taxes tasks found; 3 leftover note lines removed "
                                         "from 1 of them, 0 not saved; 1 mixed lines kept\n")
        self.assertEqual(signed_out, [TOKEN])
        for answer, message in ((None, "The Agenda could not be read"), (dict(counts, failed=1), "could not be saved")):
            with self.subTest(answer=answer), patch.dict(os.environ, environ, clear=True), \
                    patch("sys.stdout", new_callable=io.StringIO), self.assertRaisesRegex(device_tests.DeviceTestsError, message):
                device_tests.tidy(minted=lambda base, secret: TOKEN, tidied=lambda base, token: answer,
                                  revoked_by=lambda base, token: signed_out.append(token) or True)
        self.assertEqual(signed_out, [TOKEN] * 3, "signed out whatever the tidy found")

    def test_the_backfill_tags_are_the_fifty_finished_runs_ids_each_fourteen_characters(self):
        """Coordinator, 28 Oct: one cleanup per earlier run, tag "qa-<run id>", the run list fixed in the code and the
        run then in progress (40747123757) left out."""
        tags = device_tests.backfill_tags()
        self.assertEqual(len(tags), 50)
        self.assertEqual((tags[0], tags[-1]), (("qa-39668495406", "2026-10-21"), ("qa-40708551639", "2026-10-28")))
        self.assertNotIn("qa-40747123757", [tag for tag, _ in tags])
        self.assertTrue(all(len(tag) == 14 for tag, _ in tags))
        self.assertLess(device_tests.BACKFILL_SINCE, "2026-10-21T20:13:58Z", "since is before run 1")
        for runs in ((("1234", "2026-10-21"),), (("4066698812x", "2026-10-28"),), (("04066698812", "2026-10-28"),),
                     (("406669881260", "2026-10-28"),), (("40666988126", "2026-13-28"),), ((40666988126, "2026-10-28"),),
                     (("40666988126", "2026-10-28"), ("40666988126", "2026-10-28"))):
            with self.subTest(runs=runs), self.assertRaises(device_tests.DeviceTestsError):
                device_tests.backfill_tags(runs)

    def test_a_backfill_cleanup_call_sends_the_tag_since_and_dry_run_and_needs_every_count(self):
        seen = []
        counts = dict.fromkeys(device_tests.BACKFILL_KEYS, 0)

        def post(body, status=200):
            def answer(request, timeout):
                seen.append((request.full_url, request.get_method(), json.loads(request.data),
                             request.headers.get("Authorization")))
                if status != 200:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                return FakeReply(json.dumps(body).encode())
            return answer

        self.assertEqual(device_tests.cleanup_counts("https://s", TOKEN, "qa-40666988126", True,
                                                     post=post(dict(counts, hidden=3, extra=1))), dict(counts, hidden=3))
        self.assertEqual(seen, [("https://s/v1/auth/test-session/cleanup", "POST",
                                 {"since": "2026-10-21T20:00:00Z", "tag": "qa-40666988126", "dry_run": True},
                                 "Bearer " + TOKEN)])
        for body, status in ((dict(counts, hidden=-1), 200), ({k: v for k, v in counts.items() if k != "skipped"}, 200),
                             (dict(counts, hidden="3"), 200), (dict(counts, hidden=True), 200), ([], 200), (counts, 409)):
            with self.subTest(body=body, status=status):
                self.assertIsNone(device_tests.cleanup_counts("https://s", TOKEN, "qa-40666988126", False,
                                                              post=post(body, status)))

    def backfill(self, mode, expect="", answer=None):
        """device_tests.backfill with a fake cleanup: (its stdout, the calls (tag, dry), the sessions signed out)."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environ = {"RUNNER_TEMP": directory.name, "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                   "MAY_STAGING_API_URL": device_tests.STAGING_BASE, "PATH": "/usr/bin",
                   "MAY_BACKFILL_MODE": mode, "MAY_BACKFILL_EXPECT": expect}
        calls, signed_out = [], []
        self.calls = calls

        def cleaned(base, token, tag, dry):
            calls.append((tag, dry))
            if answer is not None:
                return answer(tag, dry)
            counts = dict.fromkeys(device_tests.BACKFILL_KEYS, 0)
            # Each dry call counts the same pending approvals; the first real call closes them.
            counts.update(hidden=2, tasks=1 if tag.endswith("6") else 0,
                          approvals_expired=4 if dry or len(calls) == 51 else 0)
            return counts

        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                device_tests.backfill(minted=lambda base, secret: TOKEN, cleaned=cleaned,
                                      revoked_by=lambda base, token: signed_out.append(token) or True)
            finally:
                self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
                self.output = out.getvalue()
        return self.output, calls, signed_out

    def test_a_dry_backfill_prints_totals_and_per_day_sums_alone_and_changes_nothing(self):
        out, calls, signed_out = self.backfill("dry")
        self.assertEqual([dry for _, dry in calls], [True] * 50, "dry calls alone")
        lines = out.splitlines()
        totals = "chats=0 tasks=6 notes=0 routines=0 for_you=0 messages=0 approvals_expired=4 hidden=100 skipped=0"
        self.assertEqual(lines[0], "May device tests: backfill dry run over 50 runs: " + totals,
                         "the approvals every call counts are counted once")
        self.assertEqual(lines[1], "May device tests: backfill dry run, runs of 2026-10-21: chats=0 tasks=1 notes=0 "
                                   "routines=0 for_you=0 messages=0 approvals_expired=0 hidden=10 skipped=0")
        self.assertEqual([line.split(": ")[1] for line in lines[1:]],
                         [f"backfill dry run, runs of 2026-10-{day}" for day in range(21, 29)])
        self.assertEqual(len(lines), 9)
        self.assertNotIn("qa-", out, "no tag, run id or title in the log")
        self.assertEqual(signed_out, [TOKEN])

    def test_a_real_backfill_runs_only_after_the_same_jobs_dry_pass_equals_the_approved_totals(self):
        totals = "chats=0 tasks=6 notes=0 routines=0 for_you=0 messages=0 approvals_expired=4 hidden=100 skipped=0"
        for expect in ("", totals.replace("hidden=100", "hidden=99"), "approve"):
            with self.subTest(expect=expect):
                with self.assertRaisesRegex(device_tests.DeviceTestsError, "not backfill_expect"):
                    self.backfill("real", expect=expect)
                self.assertEqual([dry for _, dry in self.calls], [True] * 50, "nothing real before the approval")
        out, calls, signed_out = self.backfill("real", expect=totals)
        self.assertEqual([dry for _, dry in calls], [True] * 50 + [False] * 50, "the dry pass first, then the real one")
        self.assertEqual(out.splitlines()[0], "May device tests: backfill dry run over 50 runs: " + totals)
        self.assertEqual(out.splitlines()[-1], "May device tests: backfill done over 50 runs: " + totals)

    def test_a_backfill_refuses_an_odd_mode_and_stops_at_a_call_that_does_not_answer(self):
        for mode in ("", "none", "REAL", "real "):
            with self.subTest(mode=mode), self.assertRaisesRegex(device_tests.DeviceTestsError, "neither dry nor real"):
                self.backfill(mode)
        answers = iter([dict.fromkeys(device_tests.BACKFILL_KEYS, 0), None])
        with self.assertRaisesRegex(device_tests.DeviceTestsError, "did not answer"):
            self.backfill("real", expect="x", answer=lambda tag, dry: next(answers))

    def test_a_delivery_truth_records_whether_every_tagged_message_succeeded(self):
        row = {"driver": "agent", "goal": "Send this message: hi {tag}", "server": [], "truth": [{"delivery": "succeeded"}]}
        for codes, expected in ((["succeeded", "succeeded"], (200, True)), (["succeeded", "failed (x)"], (200, False)),
                                ([device_tests.NONE_STORED], (200, False)), ([], (200, False)), (None, (0, False))):
            with self.subTest(codes=codes):
                entry = {"server": []}
                device_tests.server_reads(row, entry, "https://s", TOKEN, "qa-1-chat", None, None, None,
                                          lambda base, token, tag: codes, None)
                self.assertEqual(entry["server"], [{"get": "/v1/agents", "status": expected[0], "pass": expected[1]}])
                self.assertEqual(entry.get("delivery"), codes)

    def test_tagged_finds_the_tag_anywhere_in_the_reply_case_insensitively(self):
        def get(body):
            return lambda request, timeout: FakeReply(json.dumps(body).encode())

        tag = "qa-39557415738"
        self.assertEqual(device_tests.tagged("https://s", TOKEN, "/v1/routines", tag,
                                             get=get({"routines": [{"name": "QA-39557415738 stretch"}]})), (200, True))
        self.assertEqual(device_tests.tagged("https://s", TOKEN, "/v1/routines", tag,
                                             get=get({"routines": [{"name": "stretch"}]})), (200, False))

    def test_question_truth_finds_the_card_in_the_runs_own_chat_alone(self):
        tag = "qa-39557415738"
        question = {"id": "m2", "kind": "question", "question": {"state": "answered", "answer": {"choice_id": "c1"}}}

        server = chat_server

        get, seen = server([{"id": "may", "name": "May"}, {"id": "a7", "name": "Week " + tag}],
                           {"/v1/conversations/a7/messages": [{"kind": "text", "text": "Ask me, tag " + tag}, question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, True))
        self.assertEqual(seen, ["/v1/agents", "/v1/conversations/a7/messages"])
        get, seen = server([{"id": "may", "name": "May"}],
                           {"/v1/conversations/may/messages": [{"kind": "text", "text": tag}, question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "independent review of #132: with no chat of its own listed, May's shared chat is never read")
        self.assertEqual(seen, ["/v1/agents"])
        get, _ = server([{"id": "may", "name": "May"}], {"/v1/conversations/may/messages": [question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "a question in a chat without the tag is someone else's")
        closed = {"id": "m3", "kind": "question", "question": {"state": "closed"}}
        get, _ = server([{"id": "a7", "name": tag + " chat"}],
                        {"/v1/conversations/a7/messages": [{"kind": "text", "text": tag}, closed, question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "coordinator, 26 Oct: the first card after the run's message is the one judged, never a later "
                         "one")
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "closed", tag, get=get), (200, True))
        get, seen = server([{"id": "may", "name": "May"}, {"id": "a7", "name": tag + " chat"}],
                           {"/v1/conversations/may/messages": [{"kind": "text", "text": tag}, question],
                            "/v1/conversations/a7/messages": [{"kind": "text", "text": tag}]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "with a chat of its own, a card in May's shared chat is never the run's")
        self.assertEqual(seen, ["/v1/agents", "/v1/conversations/a7/messages"])
        get, seen = server([{"id": "../../v1/account", "name": tag}], {})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False))
        self.assertEqual(seen, ["/v1/agents"], "an id that is not a plain id is never put in a path")

    def test_delete_fresh_posts_with_the_session_and_reads_account_deleted(self):
        seen = {}

        def post(body):
            def answer(request, timeout):
                seen.update(url=request.full_url, body=request.data, auth=request.headers.get("Authorization"))
                return FakeReply(json.dumps(body).encode())
            return answer

        self.assertTrue(device_tests.delete_fresh("https://s", TOKEN, post({"account_deleted": True})))
        self.assertEqual(seen, {"url": "https://s/v1/auth/test-session/cleanup", "body": b"{}", "auth": "Bearer " + TOKEN})
        for body in ({"account_deleted": "yes"}, {"chats": 1}, []):
            self.assertFalse(device_tests.delete_fresh("https://s", TOKEN, post(body)))

        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)

        self.assertFalse(device_tests.delete_fresh("https://s", TOKEN, refused))

    def test_one_scenarios_delivery_codes_never_count_another_scenarios_messages(self):
        """Coordinator, 25 Oct (run 40138900610): poll-skip's codes held poll's two messages as well, so a failed poll
        would fail poll-skip too. Each scenario's tag is its own, and a tag is matched as a whole word."""
        run = "qa-40138900610"
        poll, skip = device_tests.scenario_tag(run, {"id": "poll"}), device_tests.scenario_tag(run, {"id": "poll-skip"})
        self.assertEqual((poll, skip), ("qa-40138900610-poll", "qa-40138900610-poll-skip"))
        mixed = [
            {"author": "user", "text": "Ask me one question, tag " + poll, "latest_run": {"status": "failed"}},
            {"author": "user", "text": "Ask me one question, tag " + skip, "latest_run": {"status": "succeeded"}},
            {"author": "user", "text": "I will answer myself: apples, " + skip + ".", "latest_run": {"status": "succeeded"}}]
        # Each reads its own chat alone; "<poll> chat" is not named with poll-skip's tag, nor the other way round.
        get, _ = chat_server([{"id": "p", "name": poll + " chat"}, {"id": "k", "name": skip + " chat"}],
                             {"/v1/conversations/p/messages": mixed, "/v1/conversations/k/messages": mixed})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, skip, get=get), ["succeeded", "succeeded"])
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, poll, get=get), ["failed"],
                         "poll's tag is a prefix of poll-skip's, yet never matches it")

    def test_a_tag_is_held_only_as_a_whole_word(self):
        for text, held in (("tag qa-1-poll", True), ("(qa-1-poll)", True), ("QA-1-POLL.", True), ("qa-1-poll\n", True),
                           ("tag qa-1-poll-skip", False), ("xqa-1-poll", False), ("qa-1-polls", False),
                           ("qa-1-poll_2", False)):
            with self.subTest(text=text):
                self.assertEqual(device_tests.holds({"body": [text]}, "qa-1-poll"), held)
        self.assertTrue(device_tests.holds("qa-1-poll-skip task", "qa-1-poll-skip"))

    def test_message_codes_are_each_tagged_user_messages_run_status_and_error_code(self):
        tag = "qa-39557415738"

        server = chat_server

        may = [{"id": "may", "name": "May"}, {"id": "a7", "name": "Week " + tag}]
        get, seen = server(may, {
            "/v1/conversations/may/messages": [
                {"author": "user", "text": "Hello " + tag, "latest_run": {"status": "failed", "error_code": "model_cap_reached"}},
                {"author": "may", "text": "Reply " + tag, "latest_run": {"status": "succeeded"}},
                {"author": "user", "text": "untagged", "latest_run": {"status": "failed", "error_code": "other"}}],
            "/v1/conversations/a7/messages": [
                {"author": "user", "text": "Plan " + tag, "latest_run": {"status": "succeeded", "error_code": None}},
                {"author": "user", "text": "Again " + tag},
                {"author": "user", "text": "Odd " + tag, "latest_run": {"status": "Sent!", "error_code": "<b>"}}]})
        codes = device_tests.message_codes("https://s", TOKEN, tag, get=get)
        self.assertEqual(sorted(codes), sorted(["succeeded", "no run", "unknown"]),
                         "coordinator, 26 Oct: the run's own chat alone, never May's shared one beside it")
        self.assertEqual(set(seen), {"/v1/agents", "/v1/conversations/a7/messages"})
        get, seen = server(may[:1], {"/v1/conversations/may/messages": [
            {"author": "user", "text": "Hello " + tag, "latest_run": {"status": "failed", "error_code": "model_cap_reached"}}]})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), [device_tests.NONE_STORED],
                         "independent review of #132: with no chat of its own, May's shared chat is never read")
        for text in ("Hello", "Plan", "Sent!", "<b>", "other"):
            self.assertNotIn(text, json.dumps(codes), "codes alone leave the reader")
        get, _ = server(may, {"/v1/conversations/may/messages": [{"author": "user", "text": "untagged"}]})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), [device_tests.NONE_STORED],
                         "no tagged message at all in the chats read")
        get, _ = server(may, {}, agents_status=401)
        self.assertIsNone(device_tests.message_codes("https://s", TOKEN, tag, get=get))
        many = [{"author": "user", "text": tag, "latest_run": {"status": "queued"}}] * 30
        get, _ = server(may, {"/v1/conversations/a7/messages": many})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get),
                         ["queued"] * device_tests.MAX_DELIVERY)
        get, seen = server([{"id": "../../v1/account", "name": tag}], {})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), [device_tests.NONE_STORED])
        self.assertEqual(seen, ["/v1/agents"], "an id that is not a plain id is never put in a path")

    def test_model_calls_left_reads_the_state_counts_alone(self):
        seen = []

        def state(body, status=200):
            def get(request, timeout):
                seen.append((request.full_url, request.headers.get("Authorization")))
                if status != 200:
                    raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
                return FakeReply(json.dumps(body).encode())
            return get

        self.assertEqual(device_tests.model_calls_left("https://s", TOKEN, state(
            {"agents": 1, "model_calls": {"used": 120, "limit": 150}})), 30)
        self.assertEqual(seen, [("https://s/v1/auth/test-session/state", "Bearer " + TOKEN)])
        for body in ({"agents": 1}, {"model_calls": {"used": "3", "limit": 150}}, {"model_calls": {"used": 3}},
                     {"model_calls": {"used": -1, "limit": 150}}, {"model_calls": {"used": 1, "limit": 0}},
                     {"model_calls": {"used": True, "limit": 150}}, {"model_calls": [1, 2]}, []):
            with self.subTest(body=body):
                self.assertIsNone(device_tests.model_calls_left("https://s", TOKEN, state(body)))
        self.assertIsNone(device_tests.model_calls_left("https://s", TOKEN, state({}, status=404)),
                          "a staging without the state read runs the scenarios")

    def test_only_an_agent_scenario_short_of_the_margin_is_near_the_cap(self):
        agent, maestro = {"id": "chat", "driver": "agent"}, {"id": "sign-in", "driver": "maestro"}
        margin = device_tests.MODEL_CALL_MARGIN
        for row, left, near in ((agent, margin - 1, True), (agent, margin, False), (agent, None, False),
                                (agent, -5, True), (maestro, 0, False)):
            with self.subTest(row=row["id"], left=left):
                self.assertIs(device_tests.near_model_cap(row, "https://s", TOKEN, lambda base, token: left), near)

    def test_the_readers_page_through_a_long_chat_to_this_runs_messages(self):
        """Run 40087626039: May replied, yet "none stored", because only a chat's first 100 messages were read."""
        tag = "qa-40087626039"
        history = [{"author": "user" if at % 2 else "may", "kind": "chat", "body": f"older message {at}"}
                   for at in range(450)]
        old_card = {"author": "may", "kind": "question", "body": "Pick one", "question": {"state": "answered"}}
        ours = [{"author": "user", "kind": "chat", "body": "This is device test " + tag,
                 "latest_run": {"status": "succeeded", "error_code": None}},
                {"author": "may", "kind": "chat", "body": "Done."}]
        chat = [{"id": "c1", "name": tag + " chat"}]
        get, seen = chat_server(chat,
                                {"/v1/conversations/c1/messages": history[:200] + [old_card] + history[200:] + ours})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), ["succeeded"])
        self.assertEqual(seen.count("/v1/conversations/c1/messages"), 3, "453 messages: two pages of 200 and one of 53")
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "an older run's answered card, before this run's first tagged message, never passes")
        card = {"author": "may", "kind": "question", "body": "Which?", "question": {"state": "answered"}}
        get, _ = chat_server(chat, {"/v1/conversations/c1/messages": history + ours + [card]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, True))

    def test_paging_stops_at_a_cursor_that_does_not_move_and_at_the_page_cap(self):
        calls = []

        def stuck(request, timeout):
            calls.append(request.full_url)
            page = [{"author": "user", "body": "x", "seq": "5"}] * device_tests.MESSAGE_PAGE
            return FakeReply(json.dumps({"messages": page, "next_cursor": "0"}).encode())

        status, messages = device_tests.chat_messages("https://s", TOKEN, "may", stuck)
        self.assertEqual((status, len(calls)), (200, 1), "a cursor that does not move ends the read")
        calls.clear()
        count = iter(range(1, 10 ** 6))

        def endless(request, timeout):
            calls.append(request.full_url)
            seq = str(next(count) * device_tests.MESSAGE_PAGE)
            page = [{"author": "user", "body": "x", "seq": seq}] * device_tests.MESSAGE_PAGE
            return FakeReply(json.dumps({"messages": page, "next_cursor": seq}).encode())

        device_tests.chat_messages("https://s", TOKEN, "may", endless)
        self.assertEqual(len(calls), device_tests.MAX_MESSAGE_PAGES, "never more than MAX_MESSAGE_PAGES reads")
        self.assertIn("?after=0&limit=200", calls[0])

    def test_cleanup_sends_since_and_tag_and_counts(self):
        seen = {}

        def post(request, timeout):
            seen.update(url=request.full_url, body=json.loads(request.data), auth=request.headers.get("Authorization"))
            return FakeReply(json.dumps({"chats": 1, "tasks": 0, "notes": 2, "routines": 1, "for_you": 0,
                                         "approvals_expired": 2, "skipped": 5}).encode())

        self.assertEqual(device_tests.cleanup("https://s", TOKEN, "2026-10-21T08:00:00Z", "qa-12345678", post=post), 6,
                         "removed items and expired test approvals; skipped ones are not counted")
        self.assertEqual(seen, {"url": "https://s/v1/auth/test-session/cleanup", "auth": "Bearer " + TOKEN,
                                "body": {"since": "2026-10-21T08:00:00Z", "tag": "qa-12345678"}})


class FakeReply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_cli_package(binary=b"\x7fELF fake"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(review.CLI_MEMBER)
        info.size = len(binary)
        archive.addfile(info, io.BytesIO(binary))
    return stream.getvalue()


class AgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_install_checks_the_pinned_sha512(self):
        package = fake_cli_package()
        digest = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode()
        with patch("device_agent.CLI_SHA512", digest):
            binary = device_agent.install(self.root, fetch=lambda url: package)
        self.assertEqual(binary.read_bytes(), b"\x7fELF fake")
        binary.unlink()
        with self.assertRaises(AgentError):
            device_agent.install(self.root, fetch=lambda url: package + b"x")
        self.assertFalse(binary.exists())

    def run_agent(self, stdout, result=None):
        seen = {}

        def run(command, cwd, env, stdout, stderr, stdin, timeout):
            seen.update(command=command, cwd=cwd, env=env, timeout=timeout)
            stdout.write(json.dumps(stdout_json).encode())
            if result is not None:
                config = json.loads((Path(command[command.index("--mcp-config") + 1])).read_text())
                Path(config["mcpServers"]["device"]["env"]["MAY_DEVICE_AGENT_RESULT"]).write_text(json.dumps(result))

        stdout_json = stdout
        with patch.dict(os.environ, {"PATH": "/usr/bin", "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                                     "GITHUB_TOKEN": "g" * 20}, clear=True), \
                patch("device_agent.subprocess.run", side_effect=run):
            found = device_agent.run_agent(Path("/cli/claude"), "Do {tag} now", "qa-12345678",
                                           {"MAY_DEVICE_PLATFORM": "android", "MAY_DEVICE_ADB": "/sdk/adb",
                                            "MAY_DEVICE_SERIAL": "emulator-5554"}, self.root / "agent", OAUTH, MODEL)
        return found, seen

    def test_the_agent_has_only_the_device_server_and_only_the_oauth_token(self):
        found, seen = self.run_agent({"type": "result", "subtype": "success", "is_error": False, "num_turns": 9},
                                     {"outcome": "completed", "summary": "ok", "hierarchy": "h", "actions": 12})
        self.assertEqual(found, {"state": "ok", "outcome": "completed", "summary": "ok", "hierarchy": "h",
                                 "actions": 12, "hierarchy_failure": None, "hierarchy_cause": None,
                                 "refusal_cause": None})
        for stored, kept in (("snapshot timeout", "snapshot timeout"), ("lost connection", "lost connection"),
                             ("Connection refused at 10.0.0.1", None), (7, None)):
            with self.subTest(cause=stored):
                found, _ = self.run_agent({"type": "result", "subtype": "success", "is_error": False, "num_turns": 9},
                                          {"outcome": "blocked", "summary": "s", "hierarchy": "", "actions": 1,
                                           "hierarchy_cause": stored, "refusal_cause": stored})
                self.assertEqual((found["hierarchy_cause"], found["refusal_cause"]), (kept, kept), "fixed words alone")
        for stored, kept in (("no idle state", "no idle state"), ("adb failed", "adb failed"),
                             ("<script>", None), (7, None)):
            with self.subTest(stored=stored):
                found, _ = self.run_agent({"type": "result", "subtype": "success", "is_error": False},
                                          {"outcome": "completed", "summary": "ok", "actions": 1,
                                           "hierarchy_failure": stored})
                self.assertEqual(found["hierarchy_failure"], kept, "fixed words alone")
        for stored, kept in ((True, True), ("yes", None), (1, None)):
            with self.subTest(wedged=stored):
                found, _ = self.run_agent({"type": "result", "subtype": "success", "is_error": False},
                                          {"outcome": "blocked", "summary": "x", "actions": 3, "wedged": stored})
                self.assertEqual(found.get("wedged"), kept, "the fail-fast's mark, true alone")
        found, _ = self.run_agent({"type": "result", "subtype": "success", "is_error": False},
                                  {"outcome": "unfinished", "summary": "", "hierarchy": "h", "actions": 9})
        self.assertEqual((found["outcome"], found["actions"], found["hierarchy"]), (None, 9, "h"),
                         "a run that never called done keeps its steps and screen, with no outcome of its own")
        command = seen["command"]
        self.assertEqual(command[:3], ["/cli/claude", "-p", "Do qa-12345678 now"])
        for flag, value in (("--tools", ""), ("--strict-mcp-config", None), ("--permission-mode", "dontAsk"),
                            ("--allowedTools", "mcp__device"), ("--model", MODEL), ("--setting-sources", "user"),
                            ("--no-session-persistence", None)):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)
                if value is not None:
                    self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn(OAUTH, " ".join(command), "the token is never an argument")
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)
        self.assertFalse({"MAY_TEST_SIGNIN_SECRET", "GITHUB_TOKEN"} & set(seen["env"]))
        self.assertEqual(list(seen["cwd"].iterdir()), [], "an empty working directory")
        config = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())
        server = config["mcpServers"]["device"]
        self.assertEqual(server["args"][-1], str(device_agent.SERVER))
        self.assertEqual(set(server["env"]), {"MAY_DEVICE_PLATFORM", "MAY_DEVICE_ADB", "MAY_DEVICE_SERIAL",
                                              "MAY_DEVICE_AGENT_RESULT"})

    def test_rate_and_usage_limits_are_rate_limited(self):
        for result in ({"is_error": True, "api_error_status": 429}, {"is_error": True, "api_error": "usage_limit_reached"},
                       {"is_error": True, "terminal_reason": "blocking_limit"}):
            with self.subTest(result=result):
                found, _ = self.run_agent(dict(result, type="result"))
                self.assertEqual(found["state"], "rate_limited")
                self.assertIsNone(found["outcome"])
        found, _ = self.run_agent({"type": "result", "is_error": True, "api_error_status": 500})
        self.assertEqual(found["state"], "error")

    def test_the_judges_prompt_carries_the_delivery_codes_and_no_message_text(self):
        seen = {}

        def run(command, cwd, env, stdout, stderr, stdin, timeout):
            seen["prompt"] = command[command.index("-p") + 1]
            stdout.write(json.dumps({"is_error": False, "structured_output": {"verdict": "pass", "note": ""}}).encode())

        for evidence, line in (
                (dict(record(), sends=True, delivery=["failed (model_rejected_request)", "succeeded"]),
                 "failed (model_rejected_request), succeeded"),
                (dict(record(), sends=True, delivery=None), "not read: the chats could not be read"),
                (dict(record(), sends=True, delivery=[]), "none found"),
                (record(), "none: the goal sends no message")):
            with self.subTest(line=line), patch("device_agent.subprocess.run", side_effect=run):
                device_agent.judge(Path("/cli/claude"), "Do {tag}", "qa-12345678", evidence, self.root / "judge",
                                   OAUTH, MODEL)
                self.assertIn("(delivery codes only): " + line + "\n", seen["prompt"])

    # Modelled on run 40203481816's Android poll (synthetic words, none from the run): the card answered on the server,
    # the choice shown as a quoted reply, and May's chat reply denying it ever asked.
    POLL_HIERARCHY = ("mayPoll.question Which fruit would you like as a snack? (qa-1-a-poll)\n"
                      "mayPoll.picked You answered: Banana\n"
                      "message.user Banana (quoting: Which fruit would you like as a snack?)\n"
                      "message.may I never posted a tap-to-answer question; your typed reply is the only answer.")

    def test_a_confirmed_server_read_reaches_the_judge_and_outranks_mays_own_words(self):
        """Coordinator, 25 Oct: run 40203481816's poll was judged a fail on May's words and a guess about the app,
        against a card the server confirmed answered."""
        seen = {}

        def run(command, cwd, env, stdout, stderr, stdin, timeout):
            seen["prompt"] = command[command.index("-p") + 1]
            stdout.write(json.dumps({"is_error": False, "structured_output": {"verdict": "pass", "note": ""}}).encode())

        row = device_tests.scenarios("poll")[0]
        entry = {"server": [{"get": "/v1/agents", "status": 200, "pass": True}], "delivery": ["succeeded"] * 2}
        evidence = dict(record(), hierarchy=self.POLL_HIERARCHY, sends=True, delivery=entry["delivery"],
                        truth=device_tests.truth_lines(row, entry))
        with patch("device_agent.subprocess.run", side_effect=run):
            device_agent.judge(Path("/cli/claude"), row["goal"], "qa-1-a-poll", evidence, self.root / "judge", OAUTH,
                               MODEL)
        prompt = seen.get("prompt", "")
        self.assertIn("each with this scenario's tag: May's question card in this run's tagged chat is answered: "
                      "confirmed\n", prompt)
        self.assertIn("it outranks May's own words in the chat", prompt)
        self.assertIn("A choice tapped on May's question card shows in the chat as a reply quoting the card.", prompt)
        self.assertIn("an error on screen, or the goal's own state missing from it, is a fail", prompt,
                      "the screen must still not contradict the truth")
        self.assertIn("mayPoll.picked You answered: Banana", prompt)

    def test_each_kind_of_server_read_is_named_for_the_judge(self):
        rows = {row["id"]: row for row in device_tests.scenarios("all")}
        cases = (
            ("poll-skip", [{"get": "/v1/agents", "status": 200, "pass": False}],
             ["May's question card in this run's tagged chat is closed: not confirmed"]),
            ("notes", [{"get": "/v1/commitments", "status": 200, "pass": False, "unverified": True}],
             ["the task titled with the tag holds the goal's note and none of that title is left open: could "
              "not be checked"]),
            ("chat", [{"get": "/v1/agents", "status": 200, "pass": True}],
             ["every tagged message this run sent has a succeeded reply: confirmed"]))
        for scenario, reads, lines in cases:
            with self.subTest(scenario=scenario):
                self.assertEqual(device_tests.truth_lines(rows[scenario], {"server": reads}), lines)
        routines = rows["routines"]
        found = device_tests.truth_lines(routines, {"server": [{"get": "/v1/routines", "status": 200, "pass": True},
                                                               {"get": "/v1/agents", "status": 200, "pass": False}]})
        self.assertEqual(found[0], "an item holding the tag is listed by GET /v1/routines: confirmed")
        self.assertIsNone(device_tests.truth_lines(None, {}) or None)
        self.assertEqual(device_agent.truth_text({}), "none: this scenario has no server read")

    def test_the_judges_prompt_holds_the_summary_redacted(self):
        """The security read of #110: the agent's summary reached the judge's prompt, an argument, as it was."""
        seen = {}

        def run(command, cwd, env, stdout, stderr, stdin, timeout):
            seen["prompt"] = command[command.index("-p") + 1]
            stdout.write(json.dumps({"is_error": False, "structured_output": {"verdict": "pass", "note": ""}}).encode())

        evidence = dict(record(), summary="Sent it.\nAuthorization: Bearer abc\nsk_live_51HabcdefGHIJKLMNOP")
        with patch("device_agent.subprocess.run", side_effect=run):
            device_agent.judge(Path("/cli/claude"), "Do {tag}", "qa-12345678", evidence, self.root / "judge", OAUTH,
                               MODEL)
        self.assertIn("Sent it.", seen["prompt"])
        for secret in ("Bearer", "sk_live_"):
            self.assertNotIn(secret, seen["prompt"])

    def test_the_judge_has_no_tool_and_answers_in_the_schema(self):
        seen = {}

        def run(command, cwd, env, stdout, stderr, stdin, timeout):
            seen["command"] = command
            stdout.write(json.dumps(answer).encode())

        for answer, expected in (
                ({"is_error": False, "structured_output": {"verdict": "pass", "note": "ok"}}, ("pass", "ok")),
                ({"is_error": False, "result": json.dumps({"verdict": "fail", "note": "no"})}, ("fail", "no")),
                ({"is_error": False, "result": "pass"}, ("error", "")),
                ({"is_error": False, "structured_output": {"verdict": "great"}}, ("error", "")),
                ({"is_error": True, "api_error_status": 429}, ("rate_limited", ""))):
            with self.subTest(answer=answer), patch("device_agent.subprocess.run", side_effect=run):
                self.assertEqual(device_agent.judge(Path("/cli/claude"), "Do {tag}", "qa-12345678", record(),
                                                    self.root / "judge", OAUTH, MODEL), expected)
        command = seen["command"]
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("--allowedTools", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1])["properties"]["verdict"]["enum"],
                         ["pass", "fail"])


class DeviceServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.result = Path(self.directory.name) / "result.json"
        self.config = device_mcp.settings({"MAY_DEVICE_ADB": "/sdk/adb", "MAY_DEVICE_SERIAL": "emulator-5554",
                                           "MAY_DEVICE_AGENT_RESULT": str(self.result)})
        self.adb = []

    def tearDown(self):
        self.directory.cleanup()

    def call(self, name, **args):
        def run(argv, capture_output, stdin, timeout):
            self.adb.append(argv)
            out = b"\x89PNG data" if "screencap" in argv else HIERARCHY if "uiautomator" in argv else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with patch("device_mcp.subprocess.run", side_effect=run), patch("device_mcp.time.sleep"):
            return device_mcp.call(name, args, self.config)

    def test_tools_act_on_the_configured_emulator_alone(self):
        self.assertEqual(self.call("tap", x=540, y=1200), (False, [{"type": "text", "text": "Done"}]))
        self.assertEqual(self.adb[-1], ["/sdk/adb", "-s", "emulator-5554", "shell", "input", "tap", "540", "1200"])
        self.call("type", text="This is device test qa-123. Reply!")
        self.assertEqual(self.adb[-2][-1], "This%sis%sdevice%stest%sqa-123.%sReply!", "one input text call")
        self.assertEqual(self.adb[-1][-3:], ["uiautomator", "dump", "/dev/tty"], "then what landed is read")
        self.call("swipe", x1=1, y1=2, x2=3, y2=4)
        self.assertEqual(self.adb[-1][-6:], ["swipe", "1", "2", "3", "4", "300"])
        self.call("back")
        self.assertEqual(self.adb[-1][-2:], ["keyevent", "4"])
        failed, content = self.call("screenshot")
        self.assertEqual((failed, content[0]["type"], base64.b64decode(content[0]["data"])),
                         (False, "image", b"\x89PNG data"))
        failed, content = self.call("hierarchy")
        self.assertEqual(content[0]["text"], "text='Chats' | id=tab | clickable | (50, 30)")

    def answer_type(self, field):
        """The type tool's answer when the focused field holds field after the input."""
        xml = (b'<hierarchy><node text="Ask May" focused="false" bounds="[0,0][9,9]"/><node text="' + field +
               b'" focused="true" bounds="[0,0][9,9]"/></hierarchy>')

        def run(argv, capture_output, stdin, timeout):
            self.adb.append(argv)
            return subprocess.CompletedProcess(argv, 0, xml if "uiautomator" in argv else b"", b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            return device_mcp.call("type", {"text": "hello"}, self.config)

    def test_type_says_what_landed_so_the_agent_never_types_it_again(self):
        """Run 40119835677: Android chat typed one sentence in pieces for three minutes and never tapped send."""
        failed, content = self.answer_type(b"This is device test qa-123. Reply with the word received.")
        self.assertFalse(failed)
        self.assertEqual(content[0]["text"],
                         "Done. The focused field now reads: 'This is device test qa-123. Reply with the word received.'")
        self.assertEqual([argv for argv in self.adb if "text" in argv], [self.adb[0]], "a single input text call")

    def test_what_landed_comes_back_through_the_redaction(self):
        """Coordinator, 24 Oct: nothing secret-shaped reaches the agent through type's answer."""
        jwt = b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln"
        for field in (b"Bearer abc", b"token " + jwt, b"refresh_token=x", b"Cookie: sid=1",
                      b"may_session_key=Zm9vYmFyYmF6cXV4cXV1eHF1dXg"):
            with self.subTest(field=field):
                _, content = self.answer_type(field)
                self.assertEqual(content[0]["text"], "Done. The focused field now reads: '(hidden)'")
        _, content = self.answer_type(b"write to sam.avery@example.com")
        self.assertEqual(content[0]["text"], "Done. The focused field now reads: 'write to ***@example.com'")
        _, content = self.answer_type(b"word " * 80)
        self.assertEqual(len(content[0]["text"]), len("Done. The focused field now reads: ''") + device_mcp.MAX_FIELD)

    def test_the_judge_sees_the_screen_at_done_not_the_agents_last_look(self):
        """Proof run 40123018291: the judge read the agent's last hierarchy, from before it sent the message."""
        before = (b'<hierarchy><node text="This is device test qa" resource-id="composer" focused="true" '
                  b'bounds="[0,0][9,9]"/></hierarchy>')
        after = (b'<hierarchy><node text="This is device test qa-1. Reply with the word received." '
                 b'bounds="[0,0][9,9]"/><node text="Received." bounds="[0,0][9,9]"/>'
                 b'<node text="Authorization: Bearer abc" bounds="[0,0][9,9]"/>'
                 b'<node text="sam@example.com" bounds="[0,0][9,9]"/></hierarchy>')
        screens = [before, after]

        def run(argv, capture_output, stdin, timeout):
            return subprocess.CompletedProcess(argv, 0, screens[0] if "uiautomator" in argv else b"", b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            device_mcp.call("hierarchy", {}, self.config)
            screens.pop(0)
            self.assertFalse(device_mcp.call("done", {"outcome": "completed", "summary": "sent"}, self.config)[0])
        stored = json.loads(self.result.read_text())
        self.assertIn("text='Received.'", stored["hierarchy"], "the screen at done")
        self.assertNotIn("This is device test qa'", stored["hierarchy"], "not the agent's last look")
        self.assertNotIn("Bearer", stored["hierarchy"], "through the same redaction")
        self.assertIn("***@example.com", stored["hierarchy"])
        self.assertNotIn("sam@", stored["hierarchy"])
        self.assertEqual(stored["actions"], 1, "the look at done is not one of the agent's steps")

    @staticmethod
    def device(answers, settings=None, dumps=None, scales=None):
        """A fake adb: settings get answers from scales ({setting: bytes}, b"1" by default), settings put is recorded
        in settings, and each uiautomator dump takes the next of answers."""
        def run(argv, capture_output, stdin, timeout):
            if "settings" in argv:
                if argv[argv.index("settings") + 1] == "put":
                    (settings if settings is not None else []).append(tuple(argv[-2:]))
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                return subprocess.CompletedProcess(argv, 0, (scales or {}).get(argv[-1], b"1\n"), b"")
            if dumps is not None:
                dumps.append(argv[argv.index("uiautomator") + 1:])
            return answers.pop(0)
        return run

    def test_with_no_fresh_screen_at_done_the_last_look_is_kept_and_said_so(self):
        answers = [subprocess.CompletedProcess([], 0, HIERARCHY, b""), subprocess.CompletedProcess([], 1, b"", b""),
                   subprocess.CompletedProcess([], 1, b"", b"")]
        dumps = []
        run = self.device(answers, dumps=dumps)

        with patch("device_mcp.subprocess.run", side_effect=run), patch("device_mcp.time.sleep") as slept:
            device_mcp.call("hierarchy", {}, self.config)
            device_mcp.call("done", {"outcome": "blocked", "summary": "x"}, self.config)
        stored = json.loads(self.result.read_text())
        self.assertTrue(stored["hierarchy"].startswith(
            "(no fresh hierarchy at done: adb failed; the agent's last look follows)"))
        self.assertIn("text='Chats'", stored["hierarchy"])
        self.assertEqual(stored["hierarchy_failure"], "adb failed")
        self.assertEqual(dumps, [["dump", "/dev/tty"], ["dump", "/dev/tty"], ["dump", "--compressed", "/dev/tty"]],
                         "a failed dump is tried once more, compressed")
        self.assertEqual([call.args for call in slept.call_args_list],
                         [(device_mcp.STILL_SECONDS,), (device_mcp.DUMP_RETRY_SECONDS,)])

    def test_the_done_dump_stills_the_animations_and_puts_them_back_after_a_failed_dump(self):
        """M, 25 Oct: the chat header's mark loops forever, so the dump never sees idle with the animations on."""
        settings, failed = [], subprocess.CompletedProcess([], 0, b"ERROR: could not get idle state.", b"")
        scales = {"window_animation_scale": b"1\n", "transition_animation_scale": b"0.5\n",
                  "animator_duration_scale": b"not a number\n"}
        with patch("device_mcp.subprocess.run", side_effect=self.device([failed, failed], settings, scales=scales)), \
                patch("device_mcp.time.sleep"):
            device_mcp.call("done", {"outcome": "completed", "summary": "x"}, self.config)
        stilled = [(setting, "0") for setting in device_mcp.ANIMATION_SETTINGS]
        self.assertEqual(settings, stilled + [("window_animation_scale", "1"), ("transition_animation_scale", "0.5"),
                                              ("animator_duration_scale", "1")],
                         "each scale back to what it was (1 when it could not be read), after a failed dump too")
        self.assertEqual(device_mcp.ANIMATION_SETTINGS, device_tests.ANIMATION_SETTINGS)

    def test_the_scales_are_put_back_even_when_the_dump_raises(self):
        settings = []
        with patch("device_mcp.subprocess.run", side_effect=self.device([], settings)), \
                patch("device_mcp.time.sleep"), patch("device_mcp.android_dump", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            device_mcp.stilled_dump(self.config)
        self.assertEqual(settings[-3:], [(setting, "1") for setting in device_mcp.ANIMATION_SETTINGS])

    def test_the_agents_own_looks_never_touch_the_animations(self):
        settings = []
        with patch("device_mcp.subprocess.run",
                   side_effect=self.device([subprocess.CompletedProcess([], 0, HIERARCHY, b"")], settings)):
            device_mcp.call("hierarchy", {}, self.config)
        self.assertEqual(settings, [], "the recording keeps its motion while the agent works")

    def test_a_dump_that_fails_once_is_retried_and_its_screen_kept(self):
        """Run 40138900610: the done-time dump came back empty for Android's notes and poll-skip."""
        answers = [subprocess.CompletedProcess([], 0, b"ERROR: could not get idle state.", b""),
                   subprocess.CompletedProcess([], 0, HIERARCHY, b"")]

        with patch("device_mcp.subprocess.run", side_effect=self.device(answers)), patch("device_mcp.time.sleep"):
            device_mcp.call("done", {"outcome": "completed", "summary": "x"}, self.config)
        stored = json.loads(self.result.read_text())
        self.assertIn("text='Chats'", stored["hierarchy"])
        self.assertIsNone(stored["hierarchy_failure"])

    def test_each_dump_failure_is_named_in_fixed_words(self):
        for answer, why in ((subprocess.CompletedProcess([], 0, b"ERROR: could not get idle state.", b""),
                             "no idle state"),
                            (subprocess.CompletedProcess([], 0, b"<hierarchy><node", b""), "unreadable"),
                            (subprocess.CompletedProcess([], 1, b"", b""), "adb failed")):
            with self.subTest(why=why), patch("device_mcp.subprocess.run", side_effect=self.device([answer, answer])), \
                    patch("device_mcp.time.sleep"):
                config = dict(self.config, hierarchy="")
                device_mcp.call("done", {"outcome": "completed", "summary": "x"}, config)
                stored = json.loads(self.result.read_text())
                self.assertEqual(stored["hierarchy_failure"], why)
                self.assertIn(why, device_mcp.DUMP_FAILURES)
                self.assertEqual(stored["hierarchy"], f"(no hierarchy at done: {why}; the agent never read one)",
                                 "the judge is told why, never handed an empty screen")

    def test_the_hierarchy_the_agent_reads_is_redacted(self):
        """The security read of #110: the hierarchy tool gave the agent the tree as it was."""
        xml = (b'<hierarchy><node text="Chats" bounds="[0,0][9,9]"/><node text="Authorization: Bearer abc" '
               b'bounds="[0,0][9,9]"/><node text="sam@example.com" bounds="[0,0][9,9]"/></hierarchy>')

        def run(argv, capture_output, stdin, timeout):
            return subprocess.CompletedProcess(argv, 0, xml, b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            failed, content = device_mcp.call("hierarchy", {}, self.config)
        self.assertFalse(failed)
        self.assertIn("text='Chats'", content[0]["text"])
        self.assertNotIn("Bearer", content[0]["text"])
        self.assertIn("***@example.com", content[0]["text"])

    def test_type_takes_plain_text_alone(self):
        for text in ("a; reboot", "$(id)", "a`b`", "it's", "a|b", "a\nb", "x" * 201, "", "a>b", "a&b", 7,
                     "why?", "a*b", "/sdcard/*"):
            with self.subTest(text=text):
                failed, _ = self.call("type", text=text)
                self.assertTrue(failed)
        self.assertEqual(self.adb, [], "nothing reached adb")

    def test_out_of_range_or_odd_arguments_are_refused(self):
        for name, args in (("tap", {"x": -1, "y": 5}), ("tap", {"x": "5", "y": 5}), ("tap", {"x": 5}),
                           ("tap", {"x": True, "y": 5}), ("wait", {"seconds": 31}), ("swipe", {"x1": 1})):
            with self.subTest(name=name, args=args):
                self.assertTrue(self.call(name, **args)[0])
        self.assertEqual(self.adb, [])

    def test_the_step_limit_and_done(self):
        for _ in range(device_mcp.MAX_ACTIONS):
            self.call("back")
        failed, content = self.call("back")
        self.assertTrue(failed)
        self.assertIn("limit is reached", content[0]["text"])
        self.assertEqual(len(self.adb), device_mcp.MAX_ACTIONS)
        self.assertTrue(self.call("done", outcome="finished", summary="x")[0])
        self.assertFalse(self.call("done", outcome="completed", summary="All good " * 200)[0])
        stored = json.loads(self.result.read_text())
        self.assertEqual((stored["outcome"], len(stored["summary"]), stored["actions"]),
                         ("completed", device_mcp.MAX_SUMMARY, device_mcp.MAX_ACTIONS))

    def test_the_protocol(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), "not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                 json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "rm", "arguments": {}}}),
                 json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})]
        out = io.StringIO()
        device_mcp.serve(io.StringIO("\n".join(lines) + "\n"), out, self.config)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "x")
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]],
                         ["screenshot", "hierarchy", "tap", "type", "swipe", "back", "wait", "done"])
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual(replies[3]["error"]["code"], -32601)

    def test_a_scenarios_own_step_limit(self):
        config = device_mcp.settings({"MAY_DEVICE_ADB": "/sdk/adb", "MAY_DEVICE_SERIAL": "emulator-5554",
                                      "MAY_DEVICE_AGENT_RESULT": str(self.result), "MAY_DEVICE_STEPS": "120"})
        assert config is not None, "120 steps were refused"
        self.config = config
        for _ in range(120):
            self.assertFalse(self.call("back")[0])
        failed, content = self.call("back")
        self.assertTrue(failed)
        self.assertEqual(content[0]["text"], "The 120-step limit is reached: call done now")
        self.assertEqual(len(self.adb), 120)
        self.assertFalse(self.call("done", outcome="completed", summary="typed it")[0])
        stored = json.loads(self.result.read_text())
        self.assertEqual(stored["actions"], 120)
        sealed = device_tests.sealed_agent(stored)
        self.assertEqual(sealed["actions"], 120, "sealed as taken, not as 0")
        self.assertTrue(publish.agent_ok(sealed))
        for steps in ("0", "201", "080", "-1", "12a", "", " 120"):
            with self.subTest(steps=steps):
                self.assertIsNone(device_mcp.settings({"MAY_DEVICE_ADB": "/sdk/adb", "MAY_DEVICE_SERIAL":
                                                       "emulator-5554", "MAY_DEVICE_AGENT_RESULT": "/r",
                                                       "MAY_DEVICE_STEPS": steps}))

    def test_each_call_leaves_the_steps_so_far_until_done(self):
        """Run 40209923275: iOS notes timed out with nothing recorded; now each call rewrites the result (coordinator,
        25 Oct)."""
        self.call("hierarchy")
        self.call("back")
        assert self.result.is_file(), "no result after two calls"
        stored = json.loads(self.result.read_text())
        self.assertEqual((stored["outcome"], stored["actions"], stored["summary"]), ("unfinished", 2, ""))
        self.assertIn("Chats", stored["hierarchy"], "the last screen the agent read, for the judge")
        self.assertFalse(self.call("done", outcome="completed", summary="ok")[0])
        self.call("back")
        stored = json.loads(self.result.read_text())
        self.assertEqual((stored["outcome"], stored["summary"]), ("completed", "ok"), "done is never overwritten")

    def test_android_never_stops_on_refused_acts(self):
        """The fail-fast is iOS's alone (coordinator, 25 Oct)."""
        with patch("device_mcp.adb", return_value=(1, b"")):
            answers = [self.call("back") for _ in range(5)]
        self.assertEqual({content[0]["text"] for _, content in answers}, {"The device did not take it"})
        self.assertFalse(self.config["wedged"])

    def test_settings_need_an_emulator_serial(self):
        self.assertIsNone(device_mcp.settings({"MAY_DEVICE_ADB": "/sdk/adb", "MAY_DEVICE_SERIAL": "192.168.0.2:5555",
                                               "MAY_DEVICE_AGENT_RESULT": "/r"}))


IOS_UDID = "0A1B2C3D-0000-4000-8000-000000000001"
IOS_TREE = {"attributes": {"bounds": "[0,0][393,852]"}, "children": [
    {"attributes": {"text": "Chats", "resource-id": "tab", "clickable": "true", "bounds": "[10,20][110,60]"}},
    {"attributes": {"accessibilityText": "New chat", "bounds": "[300,780][380,840]"}}]}


def device_type(folder, scale):
    """A simulator device type bundle whose profile gives scale (None: no mainScreenScale)."""
    bundle = Path(folder) / "iPhone 16 Pro.simdevicetype"
    (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    profile = {"mainScreenWidth": 1206} if scale is None else {"mainScreenScale": scale, "mainScreenWidth": 1206}
    (bundle / device_mcp.PROFILE).write_bytes(plistlib.dumps(profile))
    return str(bundle)


def simctl_list(argv, bundle, udid=IOS_UDID):
    """`xcrun simctl list devices|devicetypes -j` for one booted simulator of the device type at bundle."""
    kind = "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro"
    if argv[:4] == ["xcrun", "simctl", "list", "devices"]:
        return json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-2": [
            {"udid": "11111111-0000-4000-8000-000000000000", "deviceTypeIdentifier": "other"},
            {"udid": udid, "deviceTypeIdentifier": kind, "state": "Booted"}]}}).encode()
    if argv[:4] == ["xcrun", "simctl", "list", "devicetypes"]:
        return json.dumps({"devicetypes": [{"identifier": "other", "bundlePath": "/x/Other.simdevicetype"},
                                           {"identifier": kind, "bundlePath": bundle}]}).encode()
    return None


class IosDeviceServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.work = Path(self.directory.name)
        self.config = device_mcp.settings({"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": IOS_UDID,
                                           "MAY_DEVICE_MAESTRO": "/m/maestro",
                                           "MAY_DEVICE_BUNDLE": "io.github.mayassistant.may.playground",
                                           "MAY_DEVICE_AGENT_RESULT": str(self.work / "result.json"),
                                           "PATH": "/usr/bin", "JAVA_HOME": "/java", "SECRET_THING": "x"})
        self.calls, self.flows, self.failing = [], [], {}
        self.bundle = device_type(self.work / "types", 3.0)

    def tearDown(self):
        self.directory.cleanup()

    def call(self, name, **args):
        def run(argv, capture_output, stdin, timeout, env):
            self.calls.append((argv, env))
            listed = simctl_list(argv, self.bundle)
            if listed is not None:
                return subprocess.CompletedProcess(argv, 0, listed, b"")
            if "screenshot" in argv:
                Path(argv[-1]).write_bytes(png(1179, 2556))
            if argv[-1].endswith("act.yaml"):
                self.flows.append(Path(argv[-1]).read_text())
            failing = self.failing.get("act" if argv[-1].endswith("act.yaml") else argv[-1])
            if failing is not None:
                return subprocess.CompletedProcess(argv, 1, b"", failing)
            out = json.dumps(IOS_TREE).encode() if argv[-1] == "hierarchy" else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            return device_mcp.call(name, args, self.config)

    def test_two_refused_acts_in_a_row_stop_the_agent_blocked(self):
        """Run 40203481816: every iOS act after the first refusal was refused too, 90 s each (coordinator, 25 Oct). Run
        40309200520: three still cost 9 minutes a scenario, so two in a row now stop it (coordinator, 26 Oct)."""
        with patch("device_mcp.flow", side_effect=[1, 0, 1, 0, 1, 1]) as flow:
            answers = [self.call("tap", x=180, y=120) for _ in range(4)]
            self.assertEqual([failed for failed, _ in answers], [True, False, True, False],
                             "a taken act starts the count again")
            self.assertFalse(self.config["wedged"])
            self.assertTrue(self.call("tap", x=180, y=120)[0])
            self.assertFalse(self.config["wedged"], "one refusal alone does not stop it")
            failed, content = self.call("tap", x=180, y=120)
        self.assertTrue(failed)
        self.assertEqual(content[0]["text"], device_mcp.WEDGED_TEXT)
        self.assertEqual(flow.call_count, 6)
        self.assertTrue(Path(self.config["result"]).is_file(), "the stop was not recorded at once")
        stored = json.loads(Path(self.config["result"]).read_text())
        self.assertEqual((stored["outcome"], stored.get("wedged")), ("blocked", True),
                         "recorded at once, so an agent that never calls done still leaves its reason")
        for name, args in (("screenshot", {}), ("hierarchy", {}), ("tap", {"x": 1, "y": 1}), ("wait", {"seconds": 1})):
            with self.subTest(name=name):
                self.assertEqual(self.call(name, **args), (True, [{"type": "text", "text": device_mcp.WEDGED_TEXT}]))
        self.assertFalse(self.call("done", outcome="completed", summary="it worked")[0])
        stored = json.loads(Path(self.config["result"]).read_text())
        self.assertEqual((stored["outcome"], stored.get("wedged")), ("completed", True), "done never clears it")
        verdict = device_tests.combined({"server": []}, {"state": "ok", **stored}, ("pass", ""))
        self.assertEqual(verdict, {"verdict": "fail", "reason": device_tests.WEDGED},
                         "a stopped agent is never a pass, and the report says why")
        self.assertIn(device_tests.WEDGED, publish.REASONS)

    def test_a_snapshot_timeout_after_a_read_that_timed_out_stops_the_agent_at_once(self):
        """Run 40309200520: the chat's snapshot never came back; one more refusal there only spends another minute. Its
        diagnostics: in poll-skip and onboarding the tap was COMPLETED and the screen never settled (a settle timeout),
        which counts toward the two in a row and never stops it alone (independent review of #139)."""
        def refused_with(cause):
            def flow(config, command):
                config["refusal_cause"] = cause
                return 1
            return flow

        for read, act, wedged in (("snapshot timeout", "snapshot timeout", True), (None, "snapshot timeout", False),
                                  (None, "settle timeout", False), ("snapshot timeout", "settle timeout", False),
                                  ("snapshot timeout", "lost connection", False), ("not idle", "snapshot timeout", False)):
            with self.subTest(read=read, act=act):
                self.config.update(refusals=0, wedged=False, dump_cause=read)
                with patch("device_mcp.flow", side_effect=refused_with(act)):
                    failed, content = self.call("tap", x=180, y=120)
                self.assertTrue(failed)
                self.assertEqual(self.config["wedged"], wedged)
                self.assertEqual(content[0]["text"] == device_mcp.WEDGED_TEXT, wedged)

    def test_a_settle_timeout_counts_toward_two_in_a_row(self):
        def refused(config, command):
            config["refusal_cause"] = "settle timeout"
            return 1

        with patch("device_mcp.flow", side_effect=refused):
            self.call("tap", x=180, y=120)
            self.assertFalse(self.config["wedged"], "one slow settle while a reply streams does not end the scenario")
            failed, content = self.call("tap", x=180, y=120)
        self.assertEqual((failed, content[0]["text"], self.config["wedged"]), (True, device_mcp.WEDGED_TEXT, True))

    def test_a_taken_act_clears_an_earlier_reads_time_out(self):
        """Independent review of #139: a read that timed out before an act that was taken says nothing of the screen
        after it, so a later snapshot timeout alone does not stop the agent."""
        def refused(config, command):
            config["refusal_cause"] = "snapshot timeout"
            return 1

        self.config.update(dump_cause="snapshot timeout")
        with patch("device_mcp.flow", return_value=0):
            self.assertFalse(self.call("tap", x=180, y=120)[0])
        self.assertIsNone(self.config["dump_cause"])
        with patch("device_mcp.flow", side_effect=refused):
            failed, content = self.call("tap", x=180, y=120)
        self.assertTrue(failed)
        self.assertFalse(self.config["wedged"])
        self.assertNotEqual(content[0]["text"], device_mcp.WEDGED_TEXT)

    def test_each_act_is_held_act_seconds_at_most(self):
        seen = []
        with patch("device_mcp.tool", side_effect=lambda config, argv, timeout=90, both=False: seen.append(timeout) or
                   (0, b"")):
            device_mcp.flow(self.config, "- back")
        self.assertEqual(seen, [device_mcp.ACT_SECONDS])
        self.assertLessEqual(device_mcp.ACT_SECONDS, 60)

    def test_each_act_is_one_fixed_maestro_command_in_points(self):
        self.call("tap", x=180, y=120)
        self.assertEqual(self.flows[-1], 'appId: io.github.mayassistant.may.playground\n---\n- tapOn:\n'
                                         '    point: "60,40"\n    waitToSettleTimeoutMs: 3000\n',
                         "screenshot pixels become points (3x), and the settle wait after the tap is capped")
        self.call("type", text="Hello May, tag qa-123")
        self.assertEqual(self.flows[-1].splitlines()[-1], '- inputText: "Hello May, tag qa-123"')
        self.call("swipe", x1=600, y1=1800, x2=600, y2=600, ms=500)
        self.assertEqual(self.flows[-1].splitlines()[2:], ['- swipe:', '    start: "200,600"', '    end: "200,200"',
                                                           '    duration: 500', '    waitToSettleTimeoutMs: 3000'])
        self.call("back")
        self.assertIn('start: "1%,50%"', self.flows[-1])
        self.assertTrue(self.flows[-1].endswith("    waitToSettleTimeoutMs: 3000\n"))
        acts = [argv for argv, _ in self.calls if argv[0] == "/m/maestro" and argv[-2] == "test"]
        self.assertEqual(acts[0][:4], ["/m/maestro", "--device", IOS_UDID, "test"])
        self.assertTrue(all(env.get("JAVA_HOME") == "/java" and "SECRET_THING" not in env for _, env in self.calls))

    def test_a_refused_acts_driver_lines_are_kept_redacted_and_capped(self):
        """Coordinator, 25 Oct: iOS notes lost every tap after the Agenda tab, and the driver's words were never kept."""
        said = (b"".join(b"Running on simulator, step %d\n" % n for n in range(60)) +
                b"Authorization: Bearer abc\nsam@example.com was signed in\nTap on point (60,40)... FAILED\n")

        def run(argv, capture_output, stdin, timeout, env):
            listed = simctl_list(argv, self.bundle)
            if listed is not None:
                return subprocess.CompletedProcess(argv, 0, listed, b"")
            return subprocess.CompletedProcess(argv, 1, said, b"Error: could not find the element\n")

        start = datetime.datetime(2026, 10, 25, 3, 2, 57, 120456, tzinfo=datetime.timezone.utc)
        moments = iter([start, start + datetime.timedelta(seconds=90, milliseconds=5)])
        with patch("device_mcp.subprocess.run", side_effect=run), patch("device_mcp.utc_now", side_effect=moments):
            failed, content = device_mcp.call("tap", {"x": 180, "y": 120}, self.config)
        self.assertEqual((failed, content[0]["text"]), (True, "The device did not take it"))
        self.assertTrue((self.work / device_mcp.DRIVER_FILE).is_file(), "a refused act is kept")
        kept = (self.work / device_mcp.DRIVER_FILE).read_bytes()
        self.assertTrue(kept.startswith(b"== refused: - tapOn: (action 1, 2026-10-25 03:02:57.120 to "
                                        b"2026-10-25 03:04:27.125 UTC) ==\n"), kept[:100])
        self.assertIn(b"Tap on point (60,40)... FAILED\nError: could not find the element\n", kept)
        self.assertIn(b"***@example.com was signed in", kept)
        for absent in (b"Bearer", b"sam@", b"step 0\n", b"step 20\n"):
            self.assertNotIn(absent, kept)
        self.assertEqual(len(kept.splitlines()), 1 + device_mcp.DRIVER_LINES - 1, "the last 40 lines, one dropped")
        with patch("device_mcp.subprocess.run", side_effect=run):
            for _ in range(200):
                device_mcp.call("tap", {"x": 180, "y": 120}, self.config)
        self.assertLessEqual((self.work / device_mcp.DRIVER_FILE).stat().st_size, device_mcp.MAX_DRIVER)
        self.assertTrue((self.work / device_mcp.DRIVER_FILE).read_bytes().startswith(kept), "the first refusals stay")

    def test_refused_acts_are_stamped_on_the_utc_clock_the_watchdog_lines_use(self):
        self.assertEqual(device_mcp.utc_now().utcoffset(), datetime.timedelta(0))

    def test_a_taken_act_keeps_nothing(self):
        self.call("tap", x=180, y=120)
        self.assertFalse((self.work / device_mcp.DRIVER_FILE).exists())

    def tap_on(self, profile_scale, tree, x=579, y=2295):
        """One tap with the simulator's device type giving profile_scale: (the agent's reply, the flows run)."""
        bundle, flows = device_type(self.work / f"types-{profile_scale}", profile_scale), []
        self.config["scale"] = None

        def run(argv, capture_output, stdin, timeout, env):
            listed = simctl_list(argv, bundle)
            if listed is not None:
                return subprocess.CompletedProcess(argv, 0, listed, b"")
            if argv[-1].endswith("act.yaml"):
                flows.append(Path(argv[-1]).read_text())
            out = json.dumps(tree).encode() if argv[-1] == "hierarchy" else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            return device_mcp.call("tap", {"x": x, "y": y}, self.config), flows

    def test_the_scale_is_the_simulators_own_never_the_hierarchys_widest_box(self):
        """Run 40155638626: an iPhone 16 Pro hierarchy whose widest box was 322 points gave 3.75, and the Agenda tab's
        tap at screenshot pixel (579, 2295) went to (153, 613) instead of (193, 765)."""
        narrow = {"attributes": {"bounds": "[0,0][322,700]"}, "children": [
            {"attributes": {"text": "Agenda", "clickable": "true", "bounds": "[160,740][226,790]"}}]}
        for scale, point in ((3.0, "193,765"), (2, "290,1148")):
            _, flows = self.tap_on(scale, narrow)
            self.assertEqual(len(flows), 1, "the tap was placed")
            self.assertIn(f'point: "{point}"', flows[0])

    def test_an_unknown_scale_places_no_tap_and_says_so_in_the_private_log(self):
        for scale in (None, 3.5, 1, True):
            with self.subTest(scale=scale):
                (self.work / device_mcp.DRIVER_FILE).unlink(missing_ok=True)
                (failed, content), flows = self.tap_on(scale, IOS_TREE)
                self.assertTrue(failed)
                self.assertIn("screen scale is unknown", content[0]["text"])
                self.assertEqual(flows, [], "no tap is guessed")
                self.assertTrue((self.work / device_mcp.DRIVER_FILE).is_file(), "the private log says the scale is unknown")
                self.assertIn(b"== refused: scale unknown:", (self.work / device_mcp.DRIVER_FILE).read_bytes())

    def test_a_failed_hierarchy_or_refused_act_keeps_the_drivers_cause_in_fixed_words(self):
        """Run 40290647340: "iOS hierarchy failed" said nothing of why; the driver's output is sorted into
        DRIVER_CAUSES and only the word is kept (coordinator, 26 Oct)."""
        cases = {b"Tap on point (189,250)... COMPLETED\nWARNING: GL pipe is running in software mode\n\n(timed out)\n":
                 "settle timeout",
                 b"Tap on point (133,577)...\n(timed out)\n": "snapshot timeout",
                 b"Timed out waiting for the snapshot of the accessibility hierarchy": "snapshot timeout",
                 b"Failed to wait for app to idle (quiescence)": "not idle",
                 b"io.grpc.StatusRuntimeException: UNAVAILABLE: Connection refused": "lost connection",
                 b"Application io.github.mayassistant.may.playground is not running": "app not running",
                 b"Bearer abc.def.ghi something else": "other"}
        for said, cause in cases.items():
            with self.subTest(cause=cause):
                self.failing = {"hierarchy": said, "act": said}
                self.config.update(actions=0, refusals=0, done=False, hierarchy="", wedged=False)
                self.assertTrue(self.call("tap", x=180, y=120)[0])
                self.assertFalse(self.call("done", outcome="blocked", summary="stuck")[0])
                stored = json.loads(Path(self.config["result"]).read_text())
                self.assertEqual((stored["hierarchy_failure"], stored["hierarchy_cause"], stored["refusal_cause"]),
                                 ("iOS hierarchy failed", cause, cause))
                self.assertNotIn(b"Bearer", Path(self.config["result"]).read_bytes())
        self.failing = {}
        self.config.update(done=False)
        self.call("done", outcome="completed", summary="fine")
        stored = json.loads(Path(self.config["result"]).read_text())
        self.assertEqual((stored["hierarchy_failure"], stored["hierarchy_cause"]), (None, None),
                         "a read that worked keeps no cause")
        self.assertEqual(device_mcp.DRIVER_CAUSES, ("settle timeout", "not idle", "snapshot timeout", "lost connection", "app not running",
                                                    "scale unknown", "other"))
        self.assertEqual(device_mcp.driver_cause(b"Tap on point (201,803)... COMPLETED\nidle\n"), "not idle",
                         "COMPLETED alone, with no act that ran out of time, is no settle timeout")
        for typed in (b"Input text COMPLETED...\n(timed out)\n", b"Input text a COMPLETED b...\n(timed out)\n"):
            with self.subTest(typed=typed):
                self.assertEqual(device_mcp.driver_cause(typed), "snapshot timeout",
                                 "typed text holding COMPLETED is not Maestro's status line (independent review of #139)")
        self.assertEqual(device_mcp.driver_cause(b"Swipe from (1,2) to (3,4)... COMPLETED\n(timed out)\n"),
                         "settle timeout")

    def test_the_hierarchy_is_in_screenshot_pixels(self):
        failed, content = self.call("hierarchy")
        self.assertFalse(failed)
        self.assertEqual(content[0]["text"].splitlines(), ["text='Chats' | id=tab | clickable | (180, 120)",
                                                           "desc='New chat' | (1020, 2430)"])
        failed, content = self.call("screenshot")
        self.assertEqual((failed, content[0]["type"]), (False, "image"))

    def test_no_yaml_or_odd_text_reaches_maestro(self):
        for text in ('a"\n- runScript: x', "a\\b", "x: y\n- openLink: z", "it's"):
            with self.subTest(text=text):
                self.assertTrue(self.call("type", text=text)[0])
        self.assertEqual(self.flows, [])

    def test_settings_refuse_an_odd_simulator_or_bundle(self):
        good = {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": IOS_UDID, "MAY_DEVICE_MAESTRO": "/m",
                "MAY_DEVICE_BUNDLE": "io.github.mayassistant.may.playground", "MAY_DEVICE_AGENT_RESULT": "/r"}
        self.assertIsNotNone(device_mcp.settings(good))
        for change in ({"MAY_DEVICE_UDID": "booted"}, {"MAY_DEVICE_BUNDLE": "com.apple.mobilesafari; x"},
                       {"MAY_DEVICE_PLATFORM": "web"}, {"MAY_DEVICE_MAESTRO": ""}):
            with self.subTest(change=change):
                self.assertIsNone(device_mcp.settings(dict(good, **change)))


HIERARCHY = (b'<?xml version="1.0"?><hierarchy><node text="" content-desc="" resource-id="" clickable="false" '
             b'bounds="[0,0][1080,2400]"><node text="Chats" content-desc="" resource-id="tab" clickable="true" '
             b'bounds="[0,0][100,60]"/></node></hierarchy>UI hierchary dumped to: /dev/tty')


class PublishNotesTests(unittest.TestCase):
    def test_new_reasons_and_judge_notes_are_accepted_and_escaped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            row = {"id": "notes", "verdict": "skipped", "reason": device_tests.RATE_LIMITED, "videos": [],
                   "screenshots": [], "server": [], "notes": "<script>alert(1)</script> & [x](javascript:y)"}
            path.write_text(json.dumps({"device": "android", "scenarios": [row]}))
            titles = {"notes": "Add a note"}
            _, rows = publish.device_report(path, set(), titles)
            readme, page = publish.device_pages("20261021-" + SHA[:12], {"android": rows}, "note")
            for text in (readme, page):
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; [x](javascript:y)", text)
                self.assertNotIn("<script>", text)
            path.write_text(json.dumps({"device": "android", "scenarios": [dict(row, notes="x" * 601)]}))
            with self.assertRaises(publish.PublishError):
                publish.device_report(path, set(), titles)
            path.write_text(json.dumps({"device": "android", "scenarios": [dict(row, reason="free text")]}))
            with self.assertRaises(publish.PublishError):
                publish.device_report(path, set(), titles)


    def test_delivery_codes_are_shown_and_anything_else_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            row = {"id": "chat", "verdict": "fail", "reason": device_tests.NO_TRUTH, "videos": [], "screenshots": [],
                   "server": [{"get": "/v1/agents", "status": 200}], "delivery": ["failed (model_cap_reached)", "no run"]}
            titles = {"chat": "Chat with May"}
            path.write_text(json.dumps({"device": "android", "scenarios": [row]}))
            _, rows = publish.device_report(path, set(), titles)
            readme, _ = publish.device_pages("20261021-" + SHA[:12], {"android": rows}, "note")
            self.assertIn("Server: GET /v1/agents: 200; Messages: failed (model_cap_reached), no run", readme)
            path.write_text(json.dumps({"device": "android", "scenarios": [dict(row, delivery=[device_tests.NONE_STORED])]}))
            _, rows = publish.device_report(path, set(), titles)
            readme, _ = publish.device_pages("20261021-" + SHA[:12], {"android": rows}, "note")
            self.assertIn("Messages: none stored (no tagged message found in this chat)", readme)
            for delivery in (["Failed"], ["failed (x) (y)"], ["no run "], ["succeeded"] * 11, "succeeded", [1],
                             ["failed (" + "a" * 70 + ")"]):
                with self.subTest(delivery=delivery):
                    path.write_text(json.dumps({"device": "android", "scenarios": [dict(row, delivery=delivery)]}))
                    with self.assertRaises(publish.PublishError):
                        publish.device_report(path, set(), titles)


class SecretNameTests(unittest.TestCase):
    def test_the_claude_token_is_refused_by_every_other_step(self):
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", store.SECRET_NAMES)
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}, clear=True), self.assertRaises(store.StoreError):
            store.require_step_secrets(("MAY_APP_RENDERS_WRITE_KEY",))


if __name__ == "__main__":
    unittest.main()
