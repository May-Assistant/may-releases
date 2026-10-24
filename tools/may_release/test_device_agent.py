"""Device-test agent scenarios (device-tests PR B): device_tests.py run and judge for agent rows, device_agent.py and
device_mcp.py. Claude Code, the emulator and staging are all faked; nothing here reaches the network."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import device_agent
from device_agent import AgentError
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
                      asked=None, exhausted=False, delivered=None, calls_left=None, fail_mint=()):
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

        def agent(cli, goal, tag, device, work, token, model):
            # The agent's process never sees the staging secret or the OAuth token in this step's environment.
            seen.setdefault("environ", set(os.environ))
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
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.flows.sleep", side_effect=lambda s: seen.setdefault("slept", []).append(s)), \
                patch("device_tests.adb_shell", side_effect=lambda adb, env, log, line: calls.append(
                    [STDIN, *line.split()]) or 0), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.run_scenarios(selection, minted=mint, checked=lambda *a: 200,
                                       truth=lambda base, token, path, tag: truth,
                                       asked=asked or (lambda base, token, state, tag: truth),
                                       cleaned=clean, agent=agent, deleted_by=delete,
                                       delivered=delivered or deliver,
                                       calls_left=calls_left or (lambda base, token: 100),
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
        self.assertEqual({tag for _, tag, _, _, _ in goals}, {"qa-39557415738"})
        self.assertEqual([(token, model) for _, _, token, model, _ in goals], [(OAUTH, MODEL)] * 3)
        self.assertTrue(all("{tag}" in goal for goal, *_ in goals), "the goal goes to the agent with its placeholder")
        self.assertNotIn("MAY_TEST_SIGNIN_SECRET", seen["environ"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["environ"])
        self.assertIn("cleanup", seen, "the test session's cleanup ran")
        self.assertEqual(seen["cleanup"][0], TOKEN + "4")
        self.assertEqual(seen["cleanup"][2], "qa-39557415738")
        self.assertRegex(seen["cleanup"][1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["verdict"] for row in report["scenarios"]], ["ui-pass", "ui-pass", "pass"])
        self.assertEqual(report["scenarios"][0]["reason"], "server checks not configured")
        self.assertEqual(report["scenarios"][2]["server"], [{"get": "/v1/routines", "status": 200, "pass": True},
                                                            {"get": "/v1/agents", "status": 200, "pass": True}])
        self.assertEqual(report["scenarios"][0]["notes"], "fine <b>")
        self.assertEqual([row.get("agent") for row in report["scenarios"]],
                         [{"outcome": "completed", "actions": 7, "summary": "saw it"}] * 3,
                         "the agent's own account, sealed into the private report")
        self.assertNotIn("saw it", run_out + judge_out, "never in the public log")
        self.assertEqual(seen.get("delivered"), [(TOKEN + "1", "qa-39557415738"), (TOKEN + "3", "qa-39557415738")],
                         "chat and routines send May a message: their messages are read with their own session")
        self.assertEqual([row.get("delivery") for row in report["scenarios"]],
                         [["succeeded"], None, ["succeeded"]])
        self.assertEqual(run_out.splitlines(), [
            # This may-app fixture has no onboarding subflow, so each agent starts from launch and the log says so.
            *[f"May device tests (android): {scenario}: may-app has no onboarding subflow; the agent starts from launch"
              for scenario in ("chat", "notes", "routines")],
            "May device tests (android): 3 scenarios ran; verdicts follow the judge step",
            "May device tests (android): the test session's cleanup removed 3 tagged items"])
        self.assertEqual(judge_out.splitlines(), [
            "May device tests (android): chat: ui-pass", "May device tests (android): notes: ui-pass",
            "May device tests (android): routines: pass",
            "May device tests (android): 1 passed, 2 passed on screen alone (server checks not configured), 0 failed, "
            "0 skipped"])
        for secret in (TOKEN, OAUTH, "s" * 32, "saw it", "fine"):
            self.assertNotIn(secret, run_out + judge_out)
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["ui-pass", "ui-pass", "pass"])

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
            ("ui-pass", device_tests.UNCONFIGURED), ("fail", device_tests.NOT_JUDGED), ("fail", device_tests.NO_TRUTH)])

    def test_the_judge_sees_delivery_codes_alone_and_a_failed_reply_is_never_a_pass(self):
        """Coordinator, 24 Oct: the server's record outranks the screen; a failed code can't be judged into a pass."""
        codes = iter([["failed (model_rejected_request)"], ["succeeded"]])
        _, _, seen, report, _, judge_out = self.run_and_judge(
            [record(), record(), record()], [("pass", "it replied"), ("pass", ""), ("pass", "")],
            delivered=lambda base, token, tag: next(codes))
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("fail", device_tests.DELIVERY_FAILED), ("ui-pass", device_tests.UNCONFIGURED), ("pass", None)])
        self.assertEqual([evidence.get("delivery") for evidence in seen["evidence"]],
                         [["failed (model_rejected_request)"], [], ["succeeded"]],
                         "each scenario's own codes, and nothing for one that sends no message")
        self.assertIn("May device tests (android): chat: fail", judge_out)
        prompt = device_agent.JUDGE_PROMPT.format(goal="g", outcome="completed", actions=1, summary="s",
                                                  hierarchy="h", delivery="succeeded")
        self.assertIn("delivery codes only", prompt)
        self.assertIn("read when it finished", prompt)

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
        self.assertEqual(asked, [("answered", "qa-39557415738"), ("closed", "qa-39557415738")])
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
            ("chat", "skipped", device_tests.SWITCHED_OFF), ("notes", "ui-pass", device_tests.UNCONFIGURED),
            ("routines", "skipped", device_tests.SWITCHED_OFF)])
        self.assertEqual(len(seen["goals"]), 1, "only notes ran")

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
            ("chat", "skipped", device_tests.FRESH_EXHAUSTED), ("notes", "ui-pass", device_tests.UNCONFIGURED),
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
            ("chat", device_tests.NOT_FRESH), ("notes", device_tests.UNCONFIGURED), ("routines", device_tests.NOT_FRESH)])
        self.assertEqual(len(seen.get("refused", [])), 1)
        self.assertNotIn("deleted", seen, "a session that is not on a fresh account never reaches delete_fresh")

    def test_a_fresh_account_is_deleted_when_its_scenario_stops_part_way(self):
        with self.assertRaises(OSError):
            self.run_and_judge([OSError("agent died")], [], env={device_tests.FRESH: "chat"}, selection="chat")
        self.assertEqual(self.last_seen.get("deleted"), [TOKEN + "1"], "the run's finally deleted chat's account")

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
            ("chat", "skipped", device_tests.MODEL_CAP), ("notes", "ui-pass", device_tests.UNCONFIGURED),
            ("routines", "pass", None)])
        self.assertEqual([goal[1] for goal in seen["goals"]], ["qa-39557415738"] * 2, "chat's agent never ran")

    def test_a_fresh_account_skipped_near_the_cap_is_deleted_then_and_there(self):
        try:
            _, _, seen, report, _, _ = self.run_and_judge(
                [record()], [("pass", "")], selection="chat,notes", env={device_tests.FRESH: "all"},
                calls_left=lambda base, token: 0 if token == TOKEN + "1" else 100)
        except IndexError:
            self.fail("a scenario near the model-call cap ran")
        self.assertEqual([row.get("reason") for row in report["scenarios"]],
                         [device_tests.MODEL_CAP, device_tests.UNCONFIGURED])
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
                         [device_tests.UNCONFIGURED, device_tests.UNCONFIGURED, None])

    def test_a_mint_that_fails_twice_skips_its_scenario_alone_and_the_report_is_kept(self):
        """Run 40077167994: one failed mint 50 minutes into the iOS run stopped it, and the report was lost."""
        try:
            _, _, seen, report, run_out, _ = self.run_and_judge([record()] * 2, [("pass", "")] * 2,
                                                                fail_mint=(None, 429, 429))
        except device_tests.DeviceTestsError as error:
            self.fail("a failed mint stopped the run: " + error.args[0])
        self.assertEqual([(row["id"], row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("chat", "ui-pass", device_tests.UNCONFIGURED), ("notes", "skipped", device_tests.MINT_FAILED),
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
    def test_tagged_finds_the_tag_anywhere_in_the_reply_case_insensitively(self):
        def get(body):
            return lambda request, timeout: FakeReply(json.dumps(body).encode())

        tag = "qa-39557415738"
        self.assertEqual(device_tests.tagged("https://s", TOKEN, "/v1/routines", tag,
                                             get=get({"routines": [{"name": "QA-39557415738 stretch"}]})), (200, True))
        self.assertEqual(device_tests.tagged("https://s", TOKEN, "/v1/routines", tag,
                                             get=get({"routines": [{"name": "stretch"}]})), (200, False))

    def test_question_truth_finds_the_card_in_the_tagged_chat_or_mays_own(self):
        tag = "qa-39557415738"
        question = {"id": "m2", "kind": "question", "question": {"state": "answered", "answer": {"choice_id": "c1"}}}

        server = chat_server

        get, seen = server([{"id": "may", "name": "May"}, {"id": "a7", "name": "Week " + tag}],
                           {"/v1/conversations/a7/messages": [{"kind": "text", "text": "Ask me, tag " + tag}, question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, True))
        self.assertEqual(seen, ["/v1/agents", "/v1/conversations/a7/messages"])
        get, _ = server([{"id": "may", "name": "May"}],
                        {"/v1/conversations/may/messages": [{"kind": "text", "text": tag}, question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, True),
                         "May's own chat, first in the list, is read too")
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "closed", tag, get=get), (200, False))
        get, _ = server([{"id": "may", "name": "May"}], {"/v1/conversations/may/messages": [question]})
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "a question in a chat without the tag is someone else's")
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
        self.assertEqual(sorted(codes), sorted(["failed (model_cap_reached)", "succeeded", "no run", "unknown"]))
        self.assertEqual(set(seen), {"/v1/agents", "/v1/conversations/may/messages", "/v1/conversations/a7/messages"})
        for text in ("Hello", "Plan", "Sent!", "<b>", "other"):
            self.assertNotIn(text, json.dumps(codes), "codes alone leave the reader")
        get, _ = server(may, {"/v1/conversations/may/messages": [{"author": "user", "text": "untagged"}]})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), [device_tests.NONE_STORED],
                         "no tagged message at all in the chats read")
        get, _ = server(may, {}, agents_status=401)
        self.assertIsNone(device_tests.message_codes("https://s", TOKEN, tag, get=get))
        many = [{"author": "user", "text": tag, "latest_run": {"status": "queued"}}] * 30
        get, _ = server(may[:1], {"/v1/conversations/may/messages": many})
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
        get, seen = chat_server([{"id": "may", "name": "May"}],
                                {"/v1/conversations/may/messages": history[:200] + [old_card] + history[200:] + ours})
        self.assertEqual(device_tests.message_codes("https://s", TOKEN, tag, get=get), ["succeeded"])
        self.assertEqual(seen.count("/v1/conversations/may/messages"), 3, "453 messages: two pages of 200 and one of 53")
        self.assertEqual(device_tests.question_truth("https://s", TOKEN, "answered", tag, get=get), (200, False),
                         "an older run's answered card, before this run's first tagged message, never passes")
        card = {"author": "may", "kind": "question", "body": "Which?", "question": {"state": "answered"}}
        get, _ = chat_server([{"id": "may", "name": "May"}], {"/v1/conversations/may/messages": history + ours + [card]})
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
                                 "actions": 12})
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

        evidence = dict(record(), delivery=["failed (model_rejected_request)", "succeeded"])
        with patch("device_agent.subprocess.run", side_effect=run):
            device_agent.judge(Path("/cli/claude"), "Do {tag}", "qa-12345678", evidence, self.root / "judge", OAUTH,
                               MODEL)
        self.assertIn("(delivery codes only; none when the goal sends nothing): failed (model_rejected_request), "
                      "succeeded", seen["prompt"])
        with patch("device_agent.subprocess.run", side_effect=run):
            device_agent.judge(Path("/cli/claude"), "Do {tag}", "qa-12345678", record(), self.root / "judge", OAUTH,
                               MODEL)
        self.assertIn("sends nothing): none", seen["prompt"])

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
        _, content = self.answer_type(b"x" * 400)
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

    def test_with_no_fresh_screen_at_done_the_last_look_is_kept_and_said_so(self):
        answers = [subprocess.CompletedProcess([], 0, HIERARCHY, b""), subprocess.CompletedProcess([], 1, b"", b"")]

        def run(argv, capture_output, stdin, timeout):
            return answers.pop(0)

        with patch("device_mcp.subprocess.run", side_effect=run):
            device_mcp.call("hierarchy", {}, self.config)
            device_mcp.call("done", {"outcome": "blocked", "summary": "x"}, self.config)
        stored = json.loads(self.result.read_text())
        self.assertTrue(stored["hierarchy"].startswith("(no fresh hierarchy at done; the agent's last look follows)"))
        self.assertIn("text='Chats'", stored["hierarchy"])

    def test_type_takes_plain_text_alone(self):
        for text in ("a; reboot", "$(id)", "a`b`", "it's", "a|b", "a\nb", "x" * 201, "", "a>b", "a&b", 7):
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

    def test_settings_need_an_emulator_serial(self):
        self.assertIsNone(device_mcp.settings({"MAY_DEVICE_ADB": "/sdk/adb", "MAY_DEVICE_SERIAL": "192.168.0.2:5555",
                                               "MAY_DEVICE_AGENT_RESULT": "/r"}))


IOS_UDID = "0A1B2C3D-0000-4000-8000-000000000001"
IOS_TREE = {"attributes": {"bounds": "[0,0][393,852]"}, "children": [
    {"attributes": {"text": "Chats", "resource-id": "tab", "clickable": "true", "bounds": "[10,20][110,60]"}},
    {"attributes": {"accessibilityText": "New chat", "bounds": "[300,780][380,840]"}}]}


class IosDeviceServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.work = Path(self.directory.name)
        self.config = device_mcp.settings({"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": IOS_UDID,
                                           "MAY_DEVICE_MAESTRO": "/m/maestro",
                                           "MAY_DEVICE_BUNDLE": "io.github.mayassistant.may.playground",
                                           "MAY_DEVICE_AGENT_RESULT": str(self.work / "result.json"),
                                           "PATH": "/usr/bin", "JAVA_HOME": "/java", "SECRET_THING": "x"})
        self.calls, self.flows = [], []

    def tearDown(self):
        self.directory.cleanup()

    def call(self, name, **args):
        def run(argv, capture_output, stdin, timeout, env):
            self.calls.append((argv, env))
            if "screenshot" in argv:
                Path(argv[-1]).write_bytes(png(1179, 2556))
            if argv[-1].endswith("act.yaml"):
                self.flows.append(Path(argv[-1]).read_text())
            out = json.dumps(IOS_TREE).encode() if argv[-1] == "hierarchy" else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with patch("device_mcp.subprocess.run", side_effect=run):
            return device_mcp.call(name, args, self.config)

    def test_each_act_is_one_fixed_maestro_command_in_points(self):
        self.call("tap", x=180, y=120)
        self.assertEqual(self.flows[-1], 'appId: io.github.mayassistant.may.playground\n---\n- tapOn:\n'
                                         '    point: "60,40"\n', "screenshot pixels become points (3x)")
        self.call("type", text="Hello May, tag qa-123")
        self.assertEqual(self.flows[-1].splitlines()[-1], '- inputText: "Hello May, tag qa-123"')
        self.call("swipe", x1=600, y1=1800, x2=600, y2=600, ms=500)
        self.assertEqual(self.flows[-1].splitlines()[2:], ['- swipe:', '    start: "200,600"', '    end: "200,200"',
                                                           '    duration: 500'])
        self.call("back")
        self.assertIn('start: "1%,50%"', self.flows[-1])
        acts = [argv for argv, _ in self.calls if argv[0] == "/m/maestro" and argv[-2] == "test"]
        self.assertEqual(acts[0][:4], ["/m/maestro", "--device", IOS_UDID, "test"])
        self.assertTrue(all(env.get("JAVA_HOME") == "/java" and "SECRET_THING" not in env for _, env in self.calls))

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
