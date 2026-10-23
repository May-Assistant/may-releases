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
from unittest.mock import patch

import device_agent
from device_agent import AgentError
import device_mcp
import device_tests
import publish
import review
import store
from test_device_tests import FakeRecorder, SHA, TOKEN
from test_renders import png

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
                      asked=None):
        calls, minted, seen = [], [], {}

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

        def mint(base, secret):
            minted.append(secret)
            return TOKEN + str(len(minted))

        def agent(cli, goal, tag, device, work, token, model):
            # The agent's process never sees the staging secret or the OAuth token in this step's environment.
            seen.setdefault("environ", set(os.environ))
            seen.setdefault("goals", []).append((goal, tag, token, model, cli))
            return records[len(seen["goals"]) - 1]

        def clean(base, token, since, tag):
            seen["cleanup"] = (token, since, tag)
            return cleaned

        def judge(cli, goal, tag, agent_record, work, token, model):
            seen.setdefault("judged", []).append((goal, tag, token, model))
            return judged.pop(0)

        environ = dict(self.env, **(env or {}))
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.run_scenarios(selection, minted=mint, checked=lambda *a: 200,
                                       truth=lambda base, token, path, tag: truth,
                                       asked=asked or (lambda base, token, state, tag: truth),
                                       cleaned=clean, agent=agent,
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
        self.assertEqual(run_out.splitlines(), [
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

        def server(chats, messages):
            seen = []

            def get(request, timeout):
                path = request.full_url.removeprefix("https://s")
                seen.append(path)
                if path == "/v1/agents":
                    return FakeReply(json.dumps({"agents": chats}).encode())
                return FakeReply(json.dumps({"messages": messages.get(path, [])}).encode())
            return get, seen

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
        self.assertEqual(self.adb[-1][-1], "This%sis%sdevice%stest%sqa-123.%sReply!")
        self.call("swipe", x1=1, y1=2, x2=3, y2=4)
        self.assertEqual(self.adb[-1][-6:], ["swipe", "1", "2", "3", "4", "300"])
        self.call("back")
        self.assertEqual(self.adb[-1][-2:], ["keyevent", "4"])
        failed, content = self.call("screenshot")
        self.assertEqual((failed, content[0]["type"], base64.b64decode(content[0]["data"])),
                         (False, "image", b"\x89PNG data"))
        failed, content = self.call("hierarchy")
        self.assertEqual(content[0]["text"], "text='Chats' | id=tab | clickable | (50, 30)")

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


class SecretNameTests(unittest.TestCase):
    def test_the_claude_token_is_refused_by_every_other_step(self):
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", store.SECRET_NAMES)
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}, clear=True), self.assertRaises(store.StoreError):
            store.require_step_secrets(("MAY_APP_RENDERS_WRITE_KEY",))


if __name__ == "__main__":
    unittest.main()
