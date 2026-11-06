"""Web failures, posted on the may-app pull request only (web_tests.py --record-outcome, web_tests_post.py,
web-tests-pr.yml)."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import static_check
import web_tests
import web_tests_post as tool
from web_tests_post import WebPostError, comment_body
from store import StoreError

SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "D" * 50
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"
PRIVATE = "PRIVATE-WEB-TEST-NAME-5e2a"
SECRET_TEXT = "PRIVATE-WEB-ASSERTION-6b90"
STAGES_PASSED = dict.fromkeys(web_tests.STAGES, "passed")


def recorded(result="failed", failures=None, total=None, stages=None, typecheck=None, tails=None, tests=None):
    entries = failures if failures is not None else [
        {"test": PRIVATE, "message": SECRET_TEXT, "at": "web/src/private.test.tsx"}]
    errors = typecheck if typecheck is not None else []
    outcome = {"result": result,
               "stages": stages or dict(STAGES_PASSED, tests="failed" if result == "failed" else "passed"),
               "tests": tests if tests is not None else [3, 2 if result == "failed" else 3, 1 if result == "failed" else 0, 0]}
    return outcome, {"failures": entries, "total": len(entries) if total is None else total, "typecheck": errors,
                     "typecheck_total": len(errors), "tails": tails or {}}


def pull(**changes):
    value = {"number": 42, "state": "open", "user": {"login": "lane-bot"},
             "base": {"ref": "main", "repo": {"full_name": "May-Assistant/may-app"}},
             "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": "May-Assistant/may-app"}}}
    value.update(changes)
    return value


class CommentTests(unittest.TestCase):
    def test_a_failing_run_lists_each_failing_test_in_a_fence_with_the_marker(self):
        body = comment_body(SHA, "https://github.com/May-Assistant/may-releases/actions/runs/9", *recorded())
        self.assertIn("### May web checks: failed", body)
        self.assertIn("| Tests (`npx vitest run`) | failed |", body)
        self.assertIn("| Design system (`npm run fetch-ds`) | passed |", body)
        self.assertIn("Tests: 3 tests, 2 passed, 1 failed, 0 skipped.", body)
        self.assertIn(f"{PRIVATE}\n    {SECRET_TEXT}\n    in web/src/private.test.tsx", body)
        self.assertIn(f"<!-- may-web-tests-pr head={SHA} -->", body)
        self.assertTrue(body.endswith(tool.FOOTER))

    def test_type_errors_and_log_tails_are_fenced(self):
        outcome, failed = recorded(failures=[], typecheck=[{"at": "web/src/x.tsx:1:2", "code": "TS2322", "message": "``` no"}],
                                   tails={"design-system": ["one", "two"]})
        body = comment_body(SHA, None, outcome, failed)
        self.assertIn("**1 type error** in the build.", body)
        self.assertIn("````text\nweb/src/x.tsx:1:2: TS2322: ``` no\n````", body)
        self.assertIn("Last lines of the design-system log:\n\n```text\none\ntwo\n```", body)

    def test_a_body_too_long_is_cut_to_fit(self):
        many = [{"test": PRIVATE + str(n), "message": "m" * 230, "at": "web/x.ts"} for n in range(100)]
        body = comment_body(SHA, None, *recorded(failures=many, total=400))
        self.assertLessEqual(len(body), tool.MAX_BODY)
        self.assertIn("more not shown", body)


class PostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "may-private").mkdir()
        (self.root / "may-source").mkdir()
        self.write(*recorded())
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def write(self, outcome, failed):
        (self.root / "may-private" / web_tests.OUTCOME).write_text(json.dumps(outcome))
        (self.root / "may-private" / web_tests.FAILURES).write_text(json.dumps(failed))

    def api(self, pull_answer=None, comments=()):
        def call(method, path, payload, token):
            self.calls.append((method, path, payload))
            if method == "GET" and path == PULLS:
                return 200, pull_answer if pull_answer is not None else pull()
            if method == "GET" and path.startswith(COMMENTS):
                return 200, list(comments)
            if method == "POST" and path == COMMENTS:
                return 201, {}
            raise AssertionError("unexpected call " + method + " " + path)
        return call

    def post(self, call, token=TOKEN, extra=None):
        environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), "GITHUB_RUN_ID": "9"}
        if token is not None:
            environ["MAY_APP_PR_COMMENT_TOKEN"] = token
        environ.update(extra or {})
        out = io.StringIO()
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", out), \
                patch.object(tool, "head_revision", lambda checkout: SHA):
            code = tool.post(SHA, "42", call_api=call)
        return code, out.getvalue()

    def posted(self):
        return [payload["body"] for method, path, payload in self.calls if method == "POST"]

    def test_failures_are_posted_once_and_the_job_fails_with_a_fixed_line(self):
        code, out = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertEqual(out, "May web checks failed; 1 failing tests and 0 type errors posted to may-app #42\n")
        self.assertEqual(len(self.posted()), 1)
        self.assertIn(SECRET_TEXT, self.posted()[0])

    def test_a_pass_is_posted_and_passes(self):
        self.write(*recorded("passed", []))
        self.assertEqual(self.post(self.api()), (0, "May web checks passed; result posted to may-app #42\n"))

    def test_without_the_token_nothing_is_posted_and_the_job_still_fails(self):
        code, out = self.post(self.api(), token=None)
        self.assertEqual(code, 1)
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN is not set", out)
        self.assertEqual(self.calls, [])

    def test_the_step_summary_carries_verdicts_and_counts_only(self):
        summary = self.root / "summary.md"
        self.write(*recorded(stages=dict(STAGES_PASSED, tests="failed", build="timeout"),
                             typecheck=[{"at": "web/x.ts:1:1", "code": "TS1", "message": SECRET_TEXT}]))
        self.post(self.api(), extra={"GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(summary.read_text(), "\n".join([
            "### May web checks: failed", "", "| Stage | Result |", "|---|---|",
            "| toolchain | passed |", "| design-system | passed |", "| install | passed |", "| tests | failed |",
            "| build | did not finish in time |", "", "Tests: 3 tests, 2 passed, 1 failed, 0 skipped.", "",
            "1 failing tests and 1 type errors. Which ones goes only to the may-app pull request, never here."]) + "\n")

    def test_a_head_already_reported_is_not_posted_again_but_the_backends_marker_does_not_count(self):
        code, out = self.post(self.api(comments=[{"body": f"x <!-- may-web-tests-pr head={SHA} -->"}]))
        self.assertEqual((code, self.posted()), (1, []))
        self.assertIn("already posted", out)
        self.calls = []
        self.post(self.api(comments=[{"body": f"x <!-- may-backend-unit-pr head={SHA} -->"}]))
        self.assertEqual(len(self.posted()), 1)

    def test_a_moved_closed_or_forked_pull_request_gets_nothing(self):
        for case, answer in (("moved", pull(head={"sha": OTHER, "ref": "f", "repo": {"full_name": "May-Assistant/may-app"}})),
                             ("closed", pull(state="closed")),
                             ("fork", pull(head={"sha": SHA, "ref": "f", "repo": {"full_name": "someone/may-app"}}))):
            with self.subTest(case=case):
                self.calls = []
                with self.assertRaises(WebPostError):
                    self.post(self.api(pull_answer=answer))
                self.assertEqual(self.posted(), [])

    def test_a_body_holding_the_token_is_never_posted(self):
        self.write(*recorded(tails={"install": ["x " + TOKEN]}))
        with self.assertRaises(WebPostError):
            self.post(self.api())
        self.assertEqual(self.calls, [])

    def test_a_missing_or_malformed_record_is_refused(self):
        outcome, failed = recorded()
        entry = failed["failures"][0]
        cases = {
            "an unknown result": ({**outcome, "result": "maybe"}, failed),
            "a backend outcome": ({**outcome, "migrations": [1, 1]}, failed),
            "a missing stage": ({**outcome, "stages": {"tests": "passed"}}, failed),
            "an unknown verdict": ({**outcome, "stages": {**outcome["stages"], "tests": "flaky"}}, failed),
            "a boolean count": ({**outcome, "tests": [True, 0, 0, 0]}, failed),
            "an extra field": (outcome, {**failed, "failures": [{**entry, "suite": "x"}]}),
            "a multi-line message": (outcome, {**failed, "failures": [{**entry, "message": "a\nb"}]}),
            "an unknown tail": (outcome, {**failed, "tails": {"toolchain": ["x"]}}),
        }
        for case, (o, f) in cases.items():
            with self.subTest(case=case):
                self.write(o, f)
                self.calls = []
                with self.assertRaises(WebPostError):
                    self.post(self.api())
                self.assertEqual(self.calls, [])

    def test_another_steps_secret_refuses_before_anything(self):
        for secret in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN"):
            with self.subTest(secret=secret), self.assertRaises(StoreError):
                self.post(self.api(), extra={secret: "x"})
        self.assertEqual(self.calls, [])

    def test_a_record_written_by_the_checks_is_accepted(self):
        logs = {stage: self.root / "may-private" / name for stage, name in web_tests.STAGES.items()}
        logs["design-system"].write_text("trouble\n")
        stages = dict(STAGES_PASSED, **{"design-system": "failed", "install": "not run", "tests": "not run",
                                        "build": "not run"})
        web_tests.record_outcome(self.root / "may-private", self.root / "may-source", logs, stages, None, None, False)
        code, _ = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertIn("Last lines of the design-system log", self.posted()[0])


class CliTests(unittest.TestCase):
    def test_a_refusal_prints_only_a_fixed_line(self):
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", tool.__file__, "--source-sha", SHA, "--pr-number", "42"],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(),
                         "May web post stopped: No web outcome is recorded; nothing was posted")


class WorkflowTests(unittest.TestCase):
    def test_the_post_tool_prints_only_fixed_lines_the_number_and_the_counts(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(static_check.web_tests_print_problems(root), [])
        text = (root / "tools/may_release/web_tests_post.py").read_text()
        with tempfile.TemporaryDirectory() as raw:
            copy = Path(raw)
            (copy / "tools/may_release").mkdir(parents=True)
            for name in ("web_tests.py", "web_tests_post.py"):
                (copy / "tools/may_release" / name).write_text((root / "tools/may_release" / name).read_text())
            (copy / "tools/may_release/web_tests_post.py").write_text(
                text.replace('print(f"May web checks passed; result posted to may-app #{number}")',
                             'print(f"May web checks passed; {body}")', 1))
            self.assertTrue(static_check.web_tests_print_problems(copy))


if __name__ == "__main__":
    unittest.main()
