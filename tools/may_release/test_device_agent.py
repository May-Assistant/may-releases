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
# A third agent row whose truth is an approval (as a routine proposal would be): not in the committed list, since
# K's cleanup leaves approvals, so a nightly run would leave Samuel a pending one each night.
ROUTINES = {
    "id": "routines", "title": "Ask May for a routine", "driver": "agent", "platforms": ["android"],
    "outward": False, "server": [], "truth": [{"get": "/v1/approvals"}],
    "goal": ("In May Playground, open Chats and start a new chat with May. Send this message: Create a routine called "
             "{tag} stretch that reminds me to stretch every weekday at 9am. Wait for May's answer. If May shows a "
             "proposal or approval card, leave it as it is: do not approve or dismiss it.")}
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
        listed = self.root / "scenarios.json"
        listed.write_text(json.dumps(json.loads(device_tests.SCENARIOS.read_text()) + [ROUTINES]))
        patcher = patch("device_tests.SCENARIOS", listed)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                    "MAY_STAGING_API_URL": "https://staging.example", "PATH": "/usr/bin:/bin",
                    "CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "MAY_DEVICE_TESTS_MODEL": MODEL, "GITHUB_RUN_ID": "39557415738"}

    def tearDown(self):
        self.directory.cleanup()

    def run_and_judge(self, records, judged, truth=(200, True), cleaned=3, env=None):
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

        def agent(cli, goal, tag, adb, serial, work, token, model):
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
            device_tests.run_scenarios("chat,notes,routines", minted=mint, checked=lambda *a: 200,
                                       truth=lambda base, token, path, tag: truth, cleaned=clean, agent=agent,
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
        self.assertEqual([row["verdict"] for row in report["scenarios"]], ["ui-pass", "pass", "pass"])
        self.assertEqual(report["scenarios"][0]["reason"], "server checks not configured")
        self.assertEqual(report["scenarios"][1]["server"], [{"get": "/v1/event-notes", "status": 200, "pass": True}])
        self.assertEqual(report["scenarios"][0]["notes"], "fine <b>")
        self.assertEqual(run_out.splitlines(), [
            "May device tests (android): 3 scenarios ran; verdicts follow the judge step",
            "May device tests (android): the test session's cleanup removed 3 tagged items"])
        self.assertEqual(judge_out.splitlines(), [
            "May device tests (android): chat: ui-pass", "May device tests (android): notes: pass",
            "May device tests (android): routines: pass",
            "May device tests (android): 2 passed, 1 passed on screen alone (server checks not configured), 0 failed, "
            "0 skipped"])
        for secret in (TOKEN, OAUTH, "s" * 32, "saw it", "fine"):
            self.assertNotIn(secret, run_out + judge_out)
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["ui-pass", "pass", "pass"])

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
            [record(), record(), record()], [("pass", ""), ("pass", ""), ("error", "")], truth=(200, False)))
        self.assertEqual([(row["verdict"], row.get("reason")) for row in report["scenarios"]], [
            ("ui-pass", device_tests.UNCONFIGURED), ("fail", device_tests.NO_TRUTH), ("fail", device_tests.NOT_JUDGED)])

    def reset_and(self, again):
        import shutil
        shutil.rmtree(self.root / device_tests.OUT)
        for name in (device_tests.VERDICTS, device_tests.AGENTS):
            (self.private / name).unlink(missing_ok=True)
        return again()

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

    def test_cleanup_sends_since_and_tag_and_counts(self):
        seen = {}

        def post(request, timeout):
            seen.update(url=request.full_url, body=json.loads(request.data), auth=request.headers.get("Authorization"))
            return FakeReply(json.dumps({"chats": 1, "tasks": 0, "notes": 2, "routines": 1, "for_you": 0,
                                         "skipped": 5}).encode())

        self.assertEqual(device_tests.cleanup("https://s", TOKEN, "2026-10-21T08:00:00Z", "qa-12345678", post=post), 4)
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
        with patch("review.CLI_SHA512", digest):
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
            found = device_agent.run_agent(Path("/cli/claude"), "Do {tag} now", "qa-12345678", "/sdk/adb",
                                           "emulator-5554", self.root / "agent", OAUTH, MODEL)
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
        self.assertEqual(set(server["env"]), {"MAY_DEVICE_ADB", "MAY_DEVICE_SERIAL", "MAY_DEVICE_AGENT_RESULT"})

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
