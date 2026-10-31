"""ds_tests.py and ds_tests_schedule.py: the design system's own suites at a may-design-system pull request head, with
nothing derived from the design system in this log. Nothing here reaches the network: xcodebuild, Gradle, the
simulator and GitHub are all fakes; age is the system's own, as test_compile_pr_errors uses it."""

import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ds_tests as tool
import ds_tests_schedule as schedule
import static_check
from ds_tests import DsTestsError
from gate import GateDenial
from store import StoreError

HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))

SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "ghs_" + "C" * 40
# Text that stands for design-system source: it must never reach stdout, stderr or anything but the comment.
MARKER = "PRIVATE-DS-SOURCE-5e1d"


def ds_pull(**changes):
    value = {"number": 40, "state": "open", "user": {"login": "lane-bot"},
             "base": {"ref": "claude/may-integration", "repo": {"full_name": "May-Assistant/may-design-system"}},
             "head": {"sha": SHA, "ref": "fix", "repo": {"full_name": "May-Assistant/may-design-system"}}}
    for path, new in changes.items():
        node = value
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = new
    return value


def junit(cases):
    rows = []
    for name, kind in cases:
        inner = {"failure": f'<failure message="{MARKER} expected 1">trace</failure>', "error": '<error message="boom"/>',
                 "skipped": "<skipped/>", "pass": ""}[kind]
        rows.append(f'<testcase classname="com.may.ds.{MARKER}Test" name="{name}">{inner}</testcase>')
    return f'<?xml version="1.0"?><testsuite>{"".join(rows)}</testsuite>'


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.checkout, self.private = self.root / tool.SOURCE, self.root / tool.PRIVATE
        self.checkout.mkdir()
        self.private.mkdir()
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        head = patch("ds_tests.head_revision", return_value=SHA)
        head.start()
        self.addCleanup(head.stop)

    def quiet(self, function, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            value = function(*args, **kwargs)
        return value, out.getvalue() + err.getvalue()

    def record(self, which, **changes):
        value = {"sha": SHA, "suite": which, "ran": True, "passed": True, "total": tool.BASELINES.get(which, 12), "failed": 0,
                 "skipped": 0, "failures": []}
        value.update(changes)
        (self.private / f"{which}.json").write_text(json.dumps(value))
        return value


class SwiftTests(Case):
    SUMMARY = {"totalTestCount": 290, "passedTests": 287, "failedTests": 2, "skippedTests": 1,
               "testFailures": [{"targetName": "MayDesignSystemTests", "testName": f"test{MARKER}()",
                                 "failureText": f"XCTAssertEqual failed: {MARKER}\nsecond line"},
                                {"targetName": "T", "testName": "two()", "failureText": ""}]}

    def run_swift(self, schemes=("MayDesignSystem-Package", "MayDesignSystem"), summary=None, version=b"Xcode 26.3\n"):
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:2] == ["xcodebuild", "-version"]:
                return subprocess.CompletedProcess(argv, 0, version + b"Build version 17\n", b"")
            if argv[:2] == ["xcodebuild", "-list"]:
                kwargs["stdout"].write(json.dumps({"workspace": {"schemes": list(schemes)}}).encode())
                return subprocess.CompletedProcess(argv, 0)
            if argv[:2] == ["xcrun", "xcresulttool"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(summary or self.SUMMARY).encode(), b"")
            self.assertIn("stdout", kwargs)
            return subprocess.CompletedProcess(argv, 65)

        with patch("ds_tests.DEVELOPER", self.root):
            _, out = self.quiet(tool.run_swift, SHA, run, lambda log, env: "0A1B2C3D-0000-4000-8000-000000000001")
        return calls, out, json.loads((self.private / "swift.json").read_text())

    def test_the_package_scheme_is_tested_on_the_simulator_and_counted(self):
        calls, out, record = self.run_swift()
        test = next(c for c in calls if "test" in c)
        self.assertEqual(test[test.index("-scheme") + 1], "MayDesignSystem-Package")
        self.assertEqual(test[test.index("-destination") + 1], "id=0A1B2C3D-0000-4000-8000-000000000001")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", test)
        self.assertEqual((record["ran"], record["passed"], record["total"], record["failed"], record["skipped"]),
                         (True, False, 290, 2, 1))
        self.assertEqual(record["failures"][0], {"name": f"MayDesignSystemTests > test{MARKER}()",
                                                 "message": f"XCTAssertEqual failed: {MARKER}"})
        self.assertEqual(out, "May DS tests swift: failed, 290 tests, 2 failed, 1 skipped\n")

    def test_no_package_scheme_or_no_summary_did_not_run_and_another_xcode_is_refused(self):
        _, out, record = self.run_swift(schemes=("Only",))
        self.assertEqual((record["ran"], record["passed"]), (False, False))
        self.assertEqual(out, "May DS tests swift: did not run (the build or the tool stopped before a result)\n")
        _, _, record = self.run_swift(summary={"totalTestCount": 1, "failedTests": 2, "skippedTests": 0})
        self.assertFalse(record["ran"], "counts that do not add up are no result")
        with self.assertRaises(DsTestsError):
            self.run_swift(version=b"Xcode 26.2\n")


class AndroidTests(Case):
    def run_android(self, cases, gradlew=True, sdk_ok=True):
        if gradlew:
            (self.checkout / "android").mkdir(exist_ok=True)
            (self.checkout / "android" / "gradlew").write_text("#!/bin/sh\n")
        commands = []

        def tool_run(argv, log, cwd=None, env=None, timeout=None):
            commands.append((argv, cwd, sorted(env)))
            reports = self.checkout / "android/components/build/test-results/testDebugUnitTest"
            reports.mkdir(parents=True)
            (reports / "TEST-one.xml").write_text(junit(cases))
            (reports / "notes.txt").write_text(MARKER)

        with patch("ds_tests.pinned_java_home", return_value=self.root / "jdk"), \
                patch("ds_tests.sdk_home", return_value=self.root / "sdk"):
            _, out = self.quiet(tool.run_android, SHA, tool_run, lambda home, log, env: None)
        return commands, out, json.loads((self.private / "android.json").read_text())

    def test_the_components_unit_tests_run_with_the_pinned_jdk_and_are_counted(self):
        commands, out, record = self.run_android([("a", "pass"), ("b", "failure"), ("c", "error"), ("d", "skipped")])
        argv, cwd, env = commands[0]
        self.assertEqual(argv, ["bash", "./gradlew", "--no-daemon", "--continue", ":components:testDebugUnitTest"])
        self.assertEqual(cwd, self.checkout / "android", "the Gradle build is in android/")
        self.assertTrue({"JAVA_HOME", "ANDROID_HOME", "GRADLE_USER_HOME"} <= set(env))
        self.assertEqual((record["total"], record["failed"], record["skipped"], record["passed"]), (4, 2, 1, False))
        self.assertEqual(record["failures"][0]["name"], f"com.may.ds.{MARKER}Test > b")
        self.assertEqual(out, "May DS tests android: failed, 4 tests, 2 failed, 1 skipped\n")
        shutil.rmtree(self.checkout / "android" / "components")
        _, out, record = self.run_android([("a", "pass")] * 3)
        self.assertTrue(record["passed"])

    def test_no_gradlew_did_not_run_and_committed_results_are_refused(self):
        commands, out, record = self.run_android([], gradlew=False)
        self.assertEqual((commands, record["ran"]), ([], False))
        (self.checkout / "gradlew").write_text("#!/bin/sh\n")
        commands, out, record = self.run_android([], gradlew=False)
        self.assertEqual((commands, record["ran"]), ([], False), "a gradlew outside android/ is not the build")
        (self.checkout / "gradlew").unlink()
        (self.checkout / tool.REPORTS).mkdir(parents=True)
        with self.assertRaises(DsTestsError):
            self.run_android([("a", "pass")])


class NodeCase(Case):
    """The tokens and web suites, with the pinned Node a fake folder and every command a fake."""

    def setUp(self):
        super().setUp()
        node = self.root / "node" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_text("")
        tools = patch("ds_tests.toolchain.tool_path", return_value=str(node))
        tools.start()
        self.addCleanup(tools.stop)
        self.calls = []

    def fake(self, codes, writes=None):
        """A quiet() stand-in: each command's exit code by its joined argv prefix, and what it writes."""
        def tool(argv, log, cwd=None, env=None, timeout=None):
            self.calls.append((list(argv), cwd, env, timeout))
            self.assertNotIn(MARKER, json.dumps(env))
            self.assertTrue(env["PATH"].startswith(str(self.root / "node" / "bin")))
            joined = " ".join(argv)
            for prefix, write in (writes or {}).items():
                if joined.startswith(prefix):
                    write(argv, log)
            with log.open("a") as stream:
                stream.write(f"{MARKER} output of {joined}\n")
            return subprocess.CompletedProcess(argv, next((c for p, c in codes.items() if joined.startswith(p)), 0))
        return tool


class TokensTests(NodeCase):
    def junit_writer(self, cases):
        def write(argv, log):
            destination = next(a for a in argv if a.startswith("--test-reporter-destination="))
            Path(destination.split("=", 1)[1]).write_text(junit(cases))
        return write

    def run_tokens(self, codes=None, cases=(("a", "pass"), ("b", "pass")), tests=("a.test.mjs", "b.test.mjs")):
        (self.checkout / "tools").mkdir(exist_ok=True)
        for name in tests:
            (self.checkout / "tools" / name).write_text("")
        _, out = self.quiet(tool.run_tokens, SHA, self.fake(codes or {}, {"node --test": self.junit_writer(cases)}))
        return out, json.loads((self.private / "tokens.json").read_text())

    def test_both_checks_and_node_test_run_with_the_pinned_node_and_are_counted(self):
        out, record = self.run_tokens()
        self.assertEqual([c[0][:3] for c in self.calls], [["node", "tools/generate-tokens.mjs", "--check"],
                                                          ["node", "tools/generate-agent-marks.mjs", "--check"],
                                                          ["node", "--test", "--test-reporter=junit"]])
        self.assertEqual(self.calls[2][0][4:], ["tools/a.test.mjs", "tools/b.test.mjs"])
        self.assertTrue(all(c[1] == self.checkout and c[3] == tool.NODE_SECONDS for c in self.calls))
        self.assertEqual((record["passed"], record["total"], record["failed"]), (True, 4, 0))
        self.assertEqual(out, "May DS tests tokens: passed, 4 tests, 0 failed, 0 skipped\n")

    def test_a_stale_check_or_a_failing_test_fails_and_is_named_only_in_the_record(self):
        out, record = self.run_tokens({"node tools/generate-agent-marks.mjs": 1},
                                      cases=(("a", "pass"), (f"{MARKER}", "failure"), ("c", "skipped")))
        self.assertEqual((record["passed"], record["total"], record["failed"], record["skipped"]), (False, 5, 2, 1))
        self.assertEqual(sorted(f["name"] for f in record["failures"]),
                         sorted([f"com.may.ds.{MARKER}Test > {MARKER}", "tools/generate-agent-marks.mjs --check"]))
        self.assertEqual(out, "May DS tests tokens: failed, 5 tests, 2 failed, 1 skipped\n")
        self.assertNotIn(MARKER, out)

    def test_node_test_without_a_report_or_without_test_files_fails(self):
        _, record = self.run_tokens({"node --test": 1}, cases=None or ())
        self.assertEqual((record["passed"], record["failed"]), (False, 1))
        self.assertEqual(record["failures"][-1]["name"], "node --test tools/*.test.mjs")
        shutil.rmtree(self.checkout / "tools")
        self.calls.clear()
        _, record = self.run_tokens(tests=())
        self.assertEqual((record["passed"], record["failures"][-1]["name"]),
                         (False, "node --test tools/*.test.mjs (no test file)"))
        self.assertEqual(len(self.calls), 2, "node --test is not run with no file")

    def test_no_pinned_node_runs_nothing(self):
        with patch("ds_tests.toolchain.tool_path", side_effect=StoreError("Pinned tool is not installed")), \
                self.assertRaises(DsTestsError):
            self.quiet(tool.run_tokens, SHA, self.fake({}))
        self.assertEqual(self.calls, [])


class WebTests(NodeCase):
    def run_web(self, codes=None, summary="      Tests  9 passed | 1 skipped (10)\n"):
        def write(argv, log):
            with log.open("a") as stream:
                stream.write(f"\x1b[32m {MARKER} \x1b[0m\n" + summary)
        try:
            _, out = self.quiet(tool.run_web, SHA, self.fake(codes or {}, {"npm test": write}))
        except DsTestsError as error:
            self.fail("the web suite's record was refused: " + error.args[0])
        return out, json.loads((self.private / "web.json").read_text())

    def web(self):
        (self.checkout / "web").mkdir()
        (self.checkout / "web" / "package.json").write_text("{}")

    def test_no_web_folder_is_skipped_and_passes(self):
        out, record = self.run_web()
        self.assertEqual(out, "May DS tests web: skipped: no web/\n")
        self.assertEqual((record["ran"], record["passed"], record["total"]), (False, True, 0))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.quiet(tool.verdict, "web"), (0, ""))
        (self.root / "only-a-link").mkdir()
        (self.checkout / "web").symlink_to(self.root / "only-a-link")
        self.assertEqual(self.run_web()[1]["ran"], False, "a linked web/ is not the design system's")

    def test_install_test_typecheck_and_build_run_in_web_and_vitest_counts(self):
        self.web()
        out, record = self.run_web()
        self.assertEqual([c[0] for c in self.calls], [["npm", "ci", "--no-audit", "--no-fund"], ["npm", "test"],
                                                      ["npm", "run", "typecheck"], ["npm", "run", "build"]])
        self.assertTrue(all(c[1] == self.checkout / "web" for c in self.calls))
        self.assertEqual(self.calls[0][2]["npm_config_cache"], str(self.root / "may-ds-npm"))
        self.assertEqual((record["passed"], record["total"], record["failed"], record["skipped"]), (True, 13, 0, 1))
        self.assertEqual(out, "May DS tests web: passed, 13 tests, 0 failed, 1 skipped\n")

    def test_a_failed_install_stops_the_rest_and_a_failed_step_or_test_fails(self):
        self.web()
        out, record = self.run_web({"npm ci": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((record["passed"], record["failed"], record["total"]), (False, 4, 4))
        self.calls.clear()
        out, record = self.run_web({"npm test": 1, "npm run build": 2}, summary=" Tests  2 failed | 8 passed (10)\n")
        self.assertEqual((record["passed"], record["total"], record["failed"]), (False, 13, 3))
        self.assertEqual([f["name"] for f in record["failures"]], ["npm test", "npm run build"])
        self.calls.clear()
        out, record = self.run_web({"npm test": 1}, summary="no summary\n")
        self.assertEqual((record["passed"], record["total"], record["failed"]), (False, 4, 1))
        self.assertNotIn(MARKER, out)


class PrototypeTests(NodeCase):
    def run_prototype(self, codes=None, summary=" Tests  5 passed (5)\n"):
        def write(argv, log):
            with log.open("a") as stream:
                stream.write(summary)
        try:
            _, out = self.quiet(tool.run_prototype, SHA, self.fake(codes or {}, {"npm --prefix prototype test": write}))
        except DsTestsError as error:
            self.fail("the prototype suite's record was refused: " + error.args[0])
        return out, json.loads((self.private / "prototype.json").read_text())

    def test_no_prototype_folder_is_skipped_and_passes(self):
        out, record = self.run_prototype()
        self.assertEqual(out, "May DS tests prototype: skipped: no prototype/\n")
        self.assertEqual((record["ran"], record["passed"], self.calls), (False, True, []))
        self.assertEqual(self.quiet(tool.verdict, "prototype"), (0, ""))

    def test_install_and_test_run_from_the_checkout_and_vitest_counts(self):
        (self.checkout / "prototype").mkdir()
        (self.checkout / "prototype" / "package.json").write_text("{}")
        out, record = self.run_prototype()
        self.assertEqual([c[0] for c in self.calls], [["npm", "--prefix", "prototype", "ci", "--no-audit", "--no-fund"],
                                                      ["npm", "--prefix", "prototype", "test"]])
        self.assertTrue(all(c[1] == self.checkout for c in self.calls), "run from the checkout, so its pretest finds web/")
        self.assertEqual((record["passed"], record["total"]), (True, 6))
        self.assertEqual(out, "May DS tests prototype: passed, 6 tests, 0 failed, 0 skipped\n")
        self.calls.clear()
        out, record = self.run_prototype({"npm --prefix prototype test": 1}, summary="pretest failed\n")
        self.assertEqual((record["passed"], record["failures"]), (False, [{"name": "npm --prefix prototype test",
                                                                           "message": "failed"}]))


class VerdictTests(Case):
    def test_the_suite_job_fails_unless_its_suite_passed(self):
        self.record("swift")
        self.assertEqual(self.quiet(tool.verdict, "swift"), (0, ""))
        self.record("swift", passed=False, failed=1)
        code, out = self.quiet(tool.verdict, "swift")
        self.assertEqual(code, 1)
        self.assertNotIn(MARKER, out)
        self.assertEqual(self.quiet(tool.verdict, "android")[0], 1, "no record is no pass")
        with self.assertRaises(DsTestsError):
            tool.verdict("ios")


class StatusTests(Case):
    def status(self, **env):
        os.environ.update({tool.STATUS_TOKEN: TOKEN, "GITHUB_RUN_ID": "88", **env})
        sent = []
        code, out = self.quiet(tool.set_status, SHA, lambda m, p, payload, t: sent.append((m, p, payload, t)) or (201, {}))
        return code, out, sent

    def test_the_head_gets_success_only_when_every_suite_passed(self):
        for suite in tool.SUITES:
            self.record(suite)
        code, out, sent = self.status()
        self.assertEqual((code, out), (0, "May DS tests: the may-ds-tests commit status is success on the design-system "
                                          "head\n"))
        method, path, payload, token = sent[0]
        self.assertEqual((method, path, token), ("POST", f"/repos/May-Assistant/may-design-system/statuses/{SHA}", TOKEN))
        self.assertEqual(payload, {"state": "success", "context": "may-ds-tests", "target_url":
                                   "https://github.com/May-Assistant/may-releases/actions/runs/88",
                                   "description": "swift 283 tests, 0 failed; android 1533 tests, 0 failed; tokens 12 "
                                                  "tests, 0 failed; web 12 tests, 0 failed; prototype 12 tests, 0 failed"})
        self.record("android", passed=False, failed=2, failures=[{"name": MARKER, "message": MARKER}])
        _, out, sent = self.status()
        self.assertEqual(sent[0][2]["state"], "failure")
        self.assertNotIn(MARKER, json.dumps(sent[0][2]) + out, "no test name reaches the status or the log")

    def test_a_suite_with_no_record_counts_by_its_job_and_none_finished_is_an_error(self):
        _, _, sent = self.status(MAY_SWIFT_RESULT="success", MAY_ANDROID_RESULT="failure", MAY_TOKENS_RESULT="success",
                                 MAY_WEB_RESULT="success", MAY_PROTOTYPE_RESULT="success")
        self.assertEqual((sent[0][2]["state"], sent[0][2]["description"]),
                         ("failure", "swift passed; android failed; tokens passed; web passed; prototype passed"))
        _, _, sent = self.status(MAY_SWIFT_RESULT="cancelled", MAY_ANDROID_RESULT="skipped", MAY_TOKENS_RESULT="skipped",
                                 MAY_WEB_RESULT="cancelled", MAY_PROTOTYPE_RESULT="skipped")
        self.assertEqual((sent[0][2]["state"], sent[0][2]["description"]), ("error", "neither suite finished"))

    def test_no_token_sets_nothing_and_only_a_status_is_writable(self):
        code, out = self.quiet(tool.set_status, SHA, lambda *a: self.fail("no call without a token"))
        self.assertEqual((code, out), (0, "May DS tests: MAY_DS_STATUS_TOKEN is not set in may-ds-renders; no commit "
                                          "status set\n"))
        with patch("ds_tests.request", return_value=(201, {})) as sent:
            tool.status_api("POST", f"/repos/May-Assistant/may-design-system/statuses/{SHA}", {}, TOKEN)
            for method, path in (("POST", f"/repos/May-Assistant/may-app/statuses/{SHA}"),
                                 ("POST", "/repos/May-Assistant/may-design-system/statuses/main"),
                                 ("GET", f"/repos/May-Assistant/may-design-system/statuses/{SHA}"),
                                 ("POST", "/repos/May-Assistant/may-design-system/issues/40/comments")):
                with self.subTest(method=method, path=path), self.assertRaises(DsTestsError):
                    tool.status_api(method, path, {}, TOKEN)
        self.assertEqual(sent.call_count, 1)
        self.record("swift")
        os.environ.update({tool.STATUS_TOKEN: TOKEN})
        with self.assertRaises(DsTestsError):
            self.quiet(tool.set_status, SHA, lambda *a: (422, None))


@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class SealOpenPostTests(Case):
    def setUp(self):
        # Found before Case narrows PATH: CI's pinned age is on the runner's PATH, not in /usr/bin.
        self.found = {name: shutil.which(name) for name in ("age", "age-keygen")}
        super().setUp()
        self.secret = next(line for line in subprocess.run([self.found["age-keygen"]], capture_output=True, text=True,
                                                            check=True).stdout.splitlines()
                           if line.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run([self.found["age-keygen"], "-y"], input=self.secret, capture_output=True, text=True,
                                     check=True).stdout.strip()
        tools = patch("ds_tests.toolchain.tool_path", side_effect=lambda name, relative: self.found[relative])
        tools.start()
        self.addCleanup(tools.stop)

    def api(self, pull=None, comments=()):
        calls = []

        def call(method, path, payload, token):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/pulls/40"):
                return 200, pull or ds_pull()
            if method == "GET":
                return 200, list(comments)
            return 201, {}
        return call, calls

    def seal_and_hand_over(self):
        self.record("swift", passed=False, failed=1, failures=[{"name": f"T > {MARKER}", "message": "boom"}])
        self.record("android", total=1000)
        self.record("tokens", passed=False, failed=1, failures=[{"name": "tools/generate-tokens.mjs --check",
                                                                 "message": "failed"}])
        self.record("web", ran=False, total=0)
        self.record("prototype", ran=False, total=0)
        os.environ[tool.RECIPIENT] = self.public
        for suite in tool.SUITES:
            try:
                _, out = self.quiet(tool.seal, SHA, suite)
            except DsTestsError as error:
                self.fail(f"{suite}'s record was refused: " + error.args[0])
            self.assertEqual(out, f"May DS tests {suite}: its record is encrypted for the post job\n")
            sealed = (self.root / tool.SEALED / f"{suite}.age").read_bytes()
            self.assertNotIn(MARKER.encode(), sealed)
            (self.root / tool.DOWNLOAD / suite).mkdir(parents=True)
            (self.root / tool.DOWNLOAD / suite / f"{suite}.age").write_bytes(sealed)
        shutil.rmtree(self.private)
        self.private.mkdir()

    def test_both_records_cross_sealed_and_one_comment_is_posted(self):
        self.seal_and_hand_over()
        os.environ[tool.DECRYPT_KEY] = self.secret
        _, out = self.quiet(tool.open_records, SHA)
        self.assertEqual(out, "May DS tests: 5 suite records decrypted and checked\n")
        self.assertNotIn(tool.DECRYPT_KEY, os.environ)
        del os.environ[tool.RECIPIENT]
        os.environ.update({tool.COMMENT_TOKEN: TOKEN, "MAY_SWIFT_RESULT": "failure", "MAY_ANDROID_RESULT": "success",
                           "MAY_TOKENS_RESULT": "failure", "MAY_WEB_RESULT": "success", "MAY_PROTOTYPE_RESULT": "success",
                           "GITHUB_RUN_ID": "77"})
        call, calls = self.api()
        code, out = self.quiet(tool.post, SHA, "40", call)
        self.assertEqual((code, out), (1, "May DS tests failed; result posted to may-design-system #40\n"))
        body = calls[-1][2]["body"]
        self.assertEqual([c[:2] for c in calls], [("GET", "/repos/May-Assistant/may-design-system/pulls/40"),
                                                   ("GET", "/repos/May-Assistant/may-design-system/issues/40/comments"
                                                           "?per_page=100&page=1"),
                                                   ("POST", "/repos/May-Assistant/may-design-system/issues/40/comments")])
        self.assertIn("### May design-system tests: failed", body)
        self.assertIn(f"T > {MARKER}: boom", body)
        self.assertIn("**Warning:** android ran 1000 tests, fewer than the 1533 at `b0e9c28`.", body)
        self.assertIn("tools/generate-tokens.mjs --check: failed", body)
        self.assertIn("| skipped (no `web/` yet) |", body)
        self.assertIn("| skipped (no `prototype/` yet) |", body)
        self.assertNotIn("tokens ran", body, "the new suites have no baseline")
        self.assertIn(f"<!-- may-ds-tests head={SHA} -->", body)
        self.assertNotIn(MARKER, out)

    def test_a_suite_with_no_record_is_posted_from_its_jobs_result(self):
        os.environ.update({tool.COMMENT_TOKEN: TOKEN, "MAY_SWIFT_RESULT": "success", "MAY_ANDROID_RESULT": "success",
                           "MAY_TOKENS_RESULT": "success", "MAY_WEB_RESULT": "success", "MAY_PROTOTYPE_RESULT": "success"})
        call, calls = self.api()
        code, _ = self.quiet(tool.post, SHA, "40", call)
        self.assertEqual(code, 0)
        self.assertIn("| passed (no detail reached the post job) |", calls[-1][2]["body"])
        os.environ.update({tool.COMMENT_TOKEN: TOKEN, "MAY_SWIFT_RESULT": "cancelled", "MAY_ANDROID_RESULT": "skipped",
                           "MAY_TOKENS_RESULT": "skipped", "MAY_WEB_RESULT": "skipped", "MAY_PROTOTYPE_RESULT": "skipped"})
        code, out = self.quiet(tool.post, SHA, "40", self.api()[0])
        self.assertEqual((code, out), (1, "May DS tests: neither suite finished (cancelled or skipped); nothing posted\n"))

    def test_a_moved_head_or_an_earlier_comment_posts_nothing(self):
        for suite in tool.SUITES:
            self.record(suite)
        os.environ.update({tool.COMMENT_TOKEN: TOKEN})
        call, calls = self.api(pull=ds_pull(head__sha=OTHER))
        with self.assertRaises(DsTestsError):
            self.quiet(tool.post, SHA, "40", call)
        os.environ.update({tool.COMMENT_TOKEN: TOKEN})
        call, calls = self.api(comments=[{"body": f"<!-- may-ds-tests head={SHA} -->"}])
        code, out = self.quiet(tool.post, SHA, "40", call)
        self.assertEqual((code, [c[0] for c in calls]), (0, ["GET", "GET"]))
        self.assertIn("already posted", out)

    def test_a_hundred_long_non_ascii_failures_still_seal(self):
        """Coordinator, 31 Oct (#65): ensure_ascii made a hundred long non-ASCII failures too large to seal."""
        failures = [{"name": "ü" * tool.MAX_NAME, "message": "é" * tool.MAX_MESSAGE}] * tool.MAX_FAILURES
        self.record("swift", passed=False, failed=300, total=500, failures=failures)
        os.environ[tool.RECIPIENT] = self.public
        try:
            self.quiet(tool.seal, SHA, "swift")
        except DsTestsError as refused:
            self.fail("the record was not sealed: " + refused.args[0])
        data = tool.sealed_bytes({**self.record("swift", passed=False, failed=300, total=500, failures=failures)})
        kept = json.loads(data.decode("utf-8"))
        self.assertLessEqual(len(data), tool.MAX_RECORD)
        self.assertEqual(kept["failed"], 300)
        self.assertEqual(kept["failures"], failures, "as UTF-8, every listed failure fits")
        big = [{"name": "x", "message": "\U0001F600" * 800}] * tool.MAX_FAILURES
        data = tool.sealed_bytes({"sha": SHA, "suite": "swift", "ran": True, "passed": False, "total": 500,
                                  "failed": 300, "skipped": 0, "failures": big})
        cut = json.loads(data.decode("utf-8"))
        self.assertLessEqual(len(data), tool.MAX_RECORD)
        self.assertTrue(70 < len(cut["failures"]) < tool.MAX_FAILURES, "only as many as must be are dropped")
        self.assertEqual(cut["failed"], 300)

    def test_a_record_for_another_commit_or_suite_is_refused(self):
        for changes in ({"sha": OTHER}, {"suite": "android"}, {"passed": True, "failed": 1}, {"total": -1},
                        {"failures": [{"name": "x\ny", "message": ""}], "failed": 1, "passed": False}):
            with self.subTest(changes=changes):
                self.record("swift", **changes)
                with self.assertRaises(DsTestsError):
                    self.quiet(tool.seal, SHA, "swift")

    def test_each_step_refuses_another_steps_secret(self):
        self.record("swift")
        for step, other in ((lambda: tool.seal(SHA, "swift"), tool.DECRYPT_KEY),
                            (lambda: tool.post(SHA, "40", self.api()[0]), tool.DECRYPT_KEY),
                            (lambda: tool.open_records(SHA), tool.COMMENT_TOKEN),
                            (lambda: tool.set_status(SHA), tool.COMMENT_TOKEN),
                            (lambda: tool.post(SHA, "40", self.api()[0]), tool.STATUS_TOKEN),
                            (lambda: tool.run_android(SHA), "MAY_DS_READ_KEY")):
            with self.subTest(other=other), patch.dict(os.environ, {other: "x" * 40}):
                with self.assertRaises(StoreError):
                    self.quiet(step)


class ScheduleTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 28, 3, 0, tzinfo=datetime.timezone.utc)

    def pulls(self, base, number, sha="c" * 40, created="2026-10-27T00:00:00Z", fork=False, updated="2026-10-27T10:00:00Z"):
        repo = "someone/may-design-system" if fork else "May-Assistant/may-design-system"
        return {"number": number, "state": "open", "created_at": created, "updated_at": updated,
                "base": {"ref": base, "repo": {"full_name": "May-Assistant/may-design-system"}},
                "head": {"sha": sha, "repo": {"full_name": repo}}}

    def test_the_oldest_untested_head_into_either_base_is_picked(self):
        listed = {"claude/may-integration": [self.pulls("claude/may-integration", 7, created="2026-10-26T00:00:00Z"),
                                             self.pulls("claude/may-integration", 8, fork=True,
                                                        created="2026-10-20T00:00:00Z")],
                  "main": [self.pulls("main", 3, sha="d" * 40, created="2026-10-25T00:00:00Z"),
                           self.pulls("main", 2, created="2026-10-24T00:00:00Z", updated="2026-09-01T00:00:00Z")]}

        def get(path, token):
            self.assertEqual(token, TOKEN)
            base = "main" if "base=main" in path else "claude/may-integration"
            return 200, json.dumps(listed[base] if path.endswith("page=1") else []).encode()

        self.assertEqual(schedule.pick(TOKEN, set(), get, self.NOW)[:2], (3, "d" * 40))
        self.assertEqual(schedule.pick(TOKEN, {(3, "d" * 40)}, get, self.NOW)[:2], (7, "c" * 40))
        self.assertEqual(schedule.pick(TOKEN, {(3, "d" * 40), (7, "c" * 40)}, get, self.NOW)[:2], (None, None))

    def test_only_open_pull_requests_are_read_and_only_ds_tests_pr_is_dispatched(self):
        with self.assertRaises(schedule.ScheduleError):
            schedule.ds_get("pulls/1/files?per_page=100&page=1", TOKEN)
        with self.assertRaises(schedule.ScheduleError):
            schedule.ds_get("pulls?state=open&base=release&sort=created&direction=asc&per_page=100&page=1", TOKEN)
        payload = schedule.dispatch_payload(40, SHA)
        self.assertEqual(payload, {"ref": "main", "inputs": {"pr_number": "40", "source_sha": SHA}})
        with self.assertRaises(schedule.ScheduleError):
            schedule.control_dispatch("actions/workflows/android-unit-pr.yml/dispatches", TOKEN, payload)
        self.assertEqual(schedule.RUN_NAME.format(number=40, sha=SHA),
                         f"May DS tests for may-design-system #40 at {SHA}")


class ShapeTests(unittest.TestCase):
    def workflow(self, name):
        return (static_check.ROOT / ".github/workflows" / name).read_text()

    def refused(self, name, check, old, new):
        text = self.workflow(name)
        self.assertIn(old, text, "mutation anchor not found: " + old[:60])
        return check(text.replace(old, new, 1))

    def test_the_committed_workflows_and_tools_pass(self):
        self.assertEqual(static_check.ds_tests_workflow_problems(self.workflow(static_check.DS_TESTS)), [])
        self.assertEqual(static_check.ds_tests_schedule_workflow_problems(self.workflow(static_check.DS_TESTS_SCHEDULE)), [])
        self.assertEqual(static_check.ds_tests_print_problems(static_check.ROOT), [])
        self.assertEqual(static_check.ds_tests_schedule_tool_problems(static_check.ROOT), [])

    def test_no_secret_or_token_reaches_a_job_that_runs_the_design_systems_code(self):
        check = static_check.ds_tests_workflow_problems
        token = "          MAY_DS_PR_COMMENT_TOKEN: ${{ steps.ci-ds-comment.outputs.token || secrets.MAY_DS_PR_COMMENT_TOKEN }}\n"
        run = '        run: python3 -I -S -B tools/may_release/ds_tests.py swift --ds-sha "$MAY_DS_SHA"\n'
        for old, new in ((run, "          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n" + run),
                         (run, "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n" + run),
                         ("          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n", ""),
                         (token, token + "          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n"),
                         ("    runs-on: macos-15\n", "    runs-on: macos-15\n    env:\n      X: ${{ secrets.MAY_DS_READ_KEY }}\n")):
            with self.subTest(new=new.strip()[:70]):
                self.assertTrue(self.refused(static_check.DS_TESTS, check, old, new))

    def test_uploads_triggers_and_conditions_are_pinned(self):
        check = static_check.ds_tests_workflow_problems
        for old, new in (("${{ runner.temp }}/may-ds-tests-sealed/swift.age", "${{ runner.temp }}/may-ds-private"),
                         ("          retention-days: 1\n", "          retention-days: 30\n"),
                         ("  workflow_dispatch:\n", "  pull_request_target:\n  workflow_dispatch:\n"),
                         ("    if: ${{ !cancelled() && needs.admission.result == 'success' }}\n", "    if: always()\n"),
                         ("      contents: read\n      actions: read\n    steps:", "      contents: write\n      actions: read\n    steps:"),
                         ("ds_tests.py verdict --suite android", "ds_tests.py verdict --suite swift"),
                         ("    needs: [admission, swift, android, tokens, web, prototype]\n",
                          "    needs: [admission, swift, android, tokens, web]\n"),
                         ("          MAY_PROTOTYPE_RESULT: ${{ needs.prototype.result }}\n", ""),
                         ('ds_tests.py prototype --ds-sha "$MAY_DS_SHA"', 'ds_tests.py web --ds-sha "$MAY_DS_SHA"'),
                         ("          MAY_WEB_RESULT: ${{ needs.web.result }}\n", ""),
                         ("ds_tests.py verdict --suite web", "ds_tests.py verdict --suite tokens"),
                         ("may-ds-tests-sealed/tokens.age", "may-ds-private/tokens.json"),
                         ('ds_tests.py tokens --ds-sha "$MAY_DS_SHA"', 'ds_tests.py web --ds-sha "$MAY_DS_SHA"'),
                         ("          name: may-ds-tests-web-${{ github.run_attempt }}\n          path: ${{ runner.temp }}/may-ds-tests-download/web\n",
                          "          name: may-ds-tests-web-${{ github.run_attempt }}\n          path: ${{ runner.temp }}/may-ds-tests-download/tokens\n")):
            with self.subTest(new=new.strip()[:60]):
                self.assertTrue(self.refused(static_check.DS_TESTS, check, old, new))

    def test_a_mint_in_a_suite_job_is_refused(self):
        text = self.workflow(static_check.DS_TESTS)
        mint = static_check.mint_step("ci-ds-read")
        first = "      - name: Install pinned age, before any pull request code\n"
        self.assertTrue(static_check.ds_tests_workflow_problems(text.replace(first, mint + first, 1)))

    def test_the_tool_prints_only_fixed_lines_and_counts(self):
        path = static_check.ROOT / "tools/may_release" / static_check.DS_TESTS_TOOL
        source = path.read_text()
        for old, new in (('    print(f"May DS tests {suite}: {verdict}, {total} tests, {failed} failed, {skipped} skipped")',
                          '    print(f"May DS tests {suite}: {verdict}, {record}")'),
                         ('MARKER = "may-ds-tests"', 'MARKER = "may-ds"'),
                         ("validate_ds_pull_request(pull, number, sha)", "pull")):
            with self.subTest(new=new[:60]):
                self.assertIn(old, source)
                root = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                (root / "tools/may_release").mkdir(parents=True)
                (root / "tools/may_release" / static_check.DS_TESTS_TOOL).write_text(source.replace(old, new, 1))
                self.assertTrue(static_check.ds_tests_print_problems(root))


if __name__ == "__main__":
    unittest.main()
