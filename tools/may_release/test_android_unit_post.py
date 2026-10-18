"""Failing Android unit tests, posted on the may-app pull request only (android_unit.py --record-outcome,
android_unit_post.py, android-unit-pr.yml)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape, quoteattr

import android_unit
import android_unit_post as tool
from android_unit_post import UnitPostError, comment_body
import static_check
from store import StoreError
from test_android_unit import PRIVATE, Fixture

SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "D" * 50
WORKFLOW = ".github/workflows/android-unit-pr.yml"
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"
SECRET_TEXT = "PRIVATE-ASSERTION-TEXT-91f3"


def junit(tests, name=PRIVATE):
    """A passing report, as Gradle writes it: no <failure> or <error> in any testcase."""
    cases = "".join(f'  <testcase name="t{n}" classname="{name}" time="0.1"/>\n' for n in range(tests))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="{name}" tests="{tests}" skipped="0" '
            f'failures="0" errors="0" timestamp="2026-10-18T00:00:00" hostname="runner" time="1.2">\n{cases}</testsuite>\n')


def failing_report(source, name=PRIVATE, method="shows the private screen", message=None, kind="failure", frame=True):
    """One JUnit report with one passing and one failing test of class name, as Gradle writes it."""
    message = message if message is not None else f"expected:<{SECRET_TEXT}> but was:<other>"
    file_name = name.rpartition(".")[2] + ".kt"
    stack = (f"java.lang.AssertionError: {message}\n\tat org.junit.Assert.fail(Assert.java:89)\n"
             + (f"\tat {name}.shows(" + file_name + ":42)\n" if frame else "") + "\tat java.base/Thread.run(Thread.java:1583)")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="{name}" tests="2" skipped="0" failures="1" '
            f'errors="0" timestamp="2026-10-18T00:00:00" hostname="runner" time="1.2">\n'
            f'  <testcase name="passes" classname="{name}" time="0.1"/>\n'
            f'  <testcase name={quoteattr(method)} classname="{name}" time="0.2"><{kind} message={quoteattr(message)} '
            f'type="java.lang.AssertionError">{escape(stack)}</{kind}></testcase>\n</testsuite>\n')


def pull(**changes):
    value = {"number": 42, "state": "open", "user": {"login": "lane-bot"},
             "base": {"ref": "main", "repo": {"full_name": "May-Assistant/may-app"}},
             "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": "May-Assistant/may-app"}}}
    value.update(changes)
    return value


class RecordTests(Fixture):
    def source_file(self):
        path = self.root / "may-source/android/core/src/test/kotlin/io/github/mayassistant/may/PrivateScreenTest.kt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("class PrivateScreenTest\n")
        return path

    def record(self, quiet):
        out = io.StringIO()
        with patch.dict(os.environ, self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(android_unit, "quiet", quiet), patch.object(android_unit, "install_sdk", lambda *a: None), \
                patch.object(android_unit, "pinned_java_home", lambda: self.jdk):
            android_unit.run_suites(self.sha, record=True)
        private = self.root / "may-private"
        return (out.getvalue(), json.loads((private / android_unit.OUTCOME).read_text()),
                json.loads((private / android_unit.FAILURES).read_text()))

    def test_a_failing_suite_is_recorded_privately_and_does_not_fail_the_step(self):
        self.source_file()
        source = str(self.root / "may-source")
        quiet = self.fake(gradle=1, suites={"core": [failing_report(source)], "playground": [junit(5)],
                                            "production": [junit(2)]})
        out, outcome, failed = self.record(quiet)
        self.assertEqual(out.splitlines(), [
            "May Android unit suite core: failed, 2 tests, 1 failed, 0 skipped",
            "May Android unit suite playground: passed, 5 tests, 0 failed, 0 skipped",
            "May Android unit suite production: passed, 2 tests, 0 failed, 0 skipped",
            "May Android unit suites finished; any failing tests stay in this runner's private files for the next step"])
        for private in (PRIVATE, SECRET_TEXT, "shows the private screen", source):
            self.assertNotIn(private, out)
        self.assertEqual(outcome, {"result": "failed", "gradle": "failed",
                                   "suites": {"core": [2, 1, 0], "playground": [5, 0, 0], "production": [2, 0, 0]}})
        self.assertEqual(failed, {"total": 1, "unreadable": 0, "failures": [{
            "suite": "core", "test": PRIVATE + " > shows the private screen", "kind": "failure",
            "message": f"expected:<{SECRET_TEXT}> but was:<other>",
            "at": "android/core/src/test/kotlin/io/github/mayassistant/may/PrivateScreenTest.kt:42"}]})

    def test_a_passing_run_is_recorded_as_passed(self):
        out, outcome, failed = self.record(self.fake(suites={n: [junit(3)] for n in android_unit.SUITES}))
        self.assertEqual(outcome["result"], "passed")
        self.assertEqual(outcome["gradle"], "ok")
        self.assertEqual(failed, {"failures": [], "total": 0, "unreadable": 0})

    def test_gradle_that_does_not_finish_is_recorded(self):
        out, outcome, failed = self.record(self.fake(timeout=True))
        self.assertEqual((outcome["result"], outcome["gradle"]), ("failed", "timeout"))
        self.assertEqual(outcome["suites"], {"core": None, "playground": None, "production": None})

    def test_a_check_before_gradle_still_fails_the_step_and_records_nothing(self):
        (self.root / "may-source/android/scripts/fetch-design-system.sh").unlink()
        with self.assertRaises(android_unit.UnitTestsError):
            self.record(self.fake())
        self.assertFalse((self.root / "may-private" / android_unit.OUTCOME).exists())

    def test_paths_are_repo_relative_and_runner_paths_and_tokens_never_kept(self):
        source = str(self.root / "may-source")
        message = (f"file {source}/android/core/src/x.json missing; cache /home/runner/.gradle/caches/x.bin; "
                   f"key ghp_{'A' * 36}; see https://example.com/a/b")
        quiet = self.fake(gradle=1, suites={"core": [failing_report(source, message=message, frame=False)],
                                            "playground": [junit(5)], "production": [junit(2)]})
        _, _, failed = self.record(quiet)
        entry = failed["failures"][0]
        self.assertEqual(entry["message"], "file android/core/src/x.json missing; cache [runner path]; key [redacted]; "
                                           "see https://example.com/a/b")
        self.assertIsNone(entry["at"], "no frame in the test's own class")

    def test_a_frame_whose_file_is_not_found_keeps_the_file_name_only(self):
        quiet = self.fake(gradle=1, suites={"core": [failing_report("x")], "playground": [junit(5)],
                                            "production": [junit(2)]})
        _, _, failed = self.record(quiet)
        self.assertEqual(failed["failures"][0]["at"], "PrivateScreenTest.kt:42")

    def test_an_error_is_recorded_as_an_error(self):
        quiet = self.fake(gradle=1, suites={"core": [failing_report("x", kind="error")], "playground": [junit(5)],
                                            "production": [junit(2)]})
        _, _, failed = self.record(quiet)
        self.assertEqual(failed["failures"][0]["kind"], "error")

    def test_a_doctype_or_oversized_report_is_counted_unreadable_not_parsed(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>\n<testsuite name="x" tests="1" skipped="0" '
                'failures="1" errors="0"><testcase name="m" classname="x"><failure message="&a;"/></testcase></testsuite>')
        quiet = self.fake(gradle=1, suites={"core": [bomb], "playground": [junit(5)], "production": [junit(2)]})
        with patch.object(android_unit, "MAX_REPORT_BYTES", 10 ** 9):
            _, _, failed = self.record(quiet)
        self.assertEqual((failed["failures"], failed["total"], failed["unreadable"]), ([], 0, 1))
        self.fresh_private()
        quiet = self.fake(gradle=1, suites={"core": [failing_report("x")], "playground": [junit(5)],
                                            "production": [junit(2)]})
        with patch.object(android_unit, "MAX_REPORT_BYTES", 10):
            _, _, failed = self.record(quiet)
        self.assertEqual((failed["total"], failed["unreadable"]), (0, 3))

    def fresh_private(self):
        for name in android_unit.SUITES.values():
            shutil.rmtree(self.root / "may-source/android" / name[1], ignore_errors=True)

    def test_at_most_max_failures_are_kept_and_all_counted(self):
        reports = [failing_report("x", method=f"m{n}") for n in range(5)]
        quiet = self.fake(gradle=1, suites={"core": reports, "playground": [junit(5)], "production": [junit(2)]})
        with patch.object(android_unit, "MAX_FAILURES", 3):
            _, _, failed = self.record(quiet)
        self.assertEqual((len(failed["failures"]), failed["total"]), (3, 5))


class BreakdownTests(Fixture):
    def run_breakdown(self, quiet, breakdown="true", record=True):
        out = io.StringIO()
        with patch.dict(os.environ, self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(android_unit, "quiet", quiet), patch.object(android_unit, "install_sdk", lambda *a: None), \
                patch.object(android_unit, "pinned_java_home", lambda: self.jdk):
            try:
                android_unit.run_suites(self.sha, record=record, breakdown=breakdown)
            except android_unit.UnitTestsError as error:
                return out.getvalue(), error.args[0]
        return out.getvalue(), None

    def core(self, *reports):
        return self.fake(gradle=1, suites={"core": list(reports), "playground": [junit(5)], "production": [junit(2)]})

    def test_failing_core_cases_are_located_by_numbers_alone(self):
        lines = self.located({"TEST-io.x.core.CTest.xml": junit(3, name="io.x.core.CTest"),
                              "TEST-io.x.core.ATest.xml": failing_report("x", name="io.x.core.ATest"),
                              "TEST-io.x.core.BTest.xml": failing_report("x", name="io.x.core.BTest", kind="error")})
        out = self.out
        self.assertEqual(lines, ["May Android unit suite core: report 1 of 3 by class name, test case 2 of 2 by name failed",
                                 "May Android unit suite core: report 2 of 3 by class name, test case 2 of 2 by name failed"])
        self.assertTrue((self.root / "may-private" / android_unit.OUTCOME).is_file(), "the outcome is written first")
        for private in ("ATest", "CTest", "io.x.core", SECRET_TEXT, "shows the private screen"):
            self.assertNotIn(private, out)

    def test_the_outcome_is_written_before_the_breakdown_runs(self):
        def broken(source):
            raise RuntimeError("breakdown failed")
        with patch.object(android_unit, "core_breakdown", broken), self.assertRaises(RuntimeError):
            self.run_breakdown(self.core(failing_report("x", name="io.x.core.ATest")))
        self.assertTrue((self.root / "may-private" / android_unit.OUTCOME).is_file())

    def test_it_is_off_unless_asked_and_says_so_when_nothing_failed(self):
        quiet = self.core(failing_report("x", name="io.x.core.ATest"))
        out, _ = self.run_breakdown(quiet, breakdown="false")
        self.assertNotIn("by class name", out)
        for _, folder in android_unit.SUITES.values():
            shutil.rmtree(self.root / "may-source/android" / folder, ignore_errors=True)
        out, _ = self.run_breakdown(self.fake(suites={n: [junit(3)] for n in android_unit.SUITES}))
        self.assertIn("May Android unit suite core: no failing test case to locate", out)

    def test_it_also_runs_without_record_outcome(self):
        out, error = self.run_breakdown(self.core(failing_report("x", name="io.x.core.ATest")), record=False)
        self.assertEqual(error, "At least one May Android unit suite did not pass")
        self.assertIn("report 1 of 1 by class name, test case 2 of 2 by name failed", out)

    def write_core(self, files):
        """Core reports under the given file names, as Gradle names them (TEST-<class>.xml)."""
        folder = self.root / "may-source/android" / android_unit.SUITES["core"][1]
        folder.mkdir(parents=True, exist_ok=True)
        for file_name, text in files.items():
            (folder / file_name).write_text(text)

    def located(self, files):
        def quiet(argv, log, **_):
            if argv[0] == "./gradlew":
                self.write_core(files)
                for name in ("playground", "production"):
                    self.reports(**{name: [junit(2)]})
                return 1
            return 0
        self.out, _ = self.run_breakdown(quiet)
        return [line for line in self.out.splitlines()
                if line.startswith("May Android unit suite core: ") and ("locate" in line or "by class name" in line or "could not" in line)]

    def test_an_unreadable_report_keeps_its_place_and_is_counted(self):
        lines = self.located({"TEST-io.x.core.ATest.xml": "<!DOCTYPE x><testsuite name='io.x.core.ATest'/>",
                              "TEST-io.x.core.BTest.xml": failing_report("x", name="io.x.core.BTest")})
        self.assertEqual(lines, ["May Android unit suite core: report 2 of 2 by class name, test case 2 of 2 by name failed",
                                 "May Android unit suite core: 1 of 2 reports could not be read, so their failing test cases are not located"])

    def test_reports_rank_by_file_name_and_cases_by_name_not_run_order(self):
        # Run order puts the failing case second; by name ("a fails" < "passes") it is first.
        lines = self.located({"TEST-io.x.core.ZTest.xml": junit(1, name="io.x.core.ZTest"),
                              "TEST-io.x.core.ATest.xml": failing_report("x", name="io.x.core.ATest", method="a fails")})
        self.assertEqual(lines, ["May Android unit suite core: report 1 of 2 by class name, test case 1 of 2 by name failed"])

    def test_core_that_did_not_run_and_too_many_reports_say_so(self):
        out, _ = self.run_breakdown(self.fake(timeout=True))
        self.assertIn("May Android unit suite core: no report to locate failing test cases in (core did not run)", out)
        self.write_core({"TEST-a.xml": junit(1), "TEST-b.xml": failing_report("x")})
        parsed = []
        with patch.object(android_unit, "MAX_REPORTS", 1), patch.object(android_unit, "parsed_report", parsed.append):
            result = android_unit.core_breakdown(self.root / "may-source")
            out = io.StringIO()
            with patch("sys.stdout", out):
                android_unit.print_breakdown(result)
        self.assertEqual((result, parsed), (([], 2, 2), []), "no report is parsed past the cap")
        self.assertEqual(out.getvalue(), "May Android unit suite core: too many reports to locate failing test cases in\n")

    def test_at_most_max_breakdown_lines(self):
        quiet = self.core(*[failing_report("x", name=f"io.x.core.T{n:02d}Test") for n in range(5)])
        with patch.object(android_unit, "MAX_BREAKDOWN", 2):
            out, _ = self.run_breakdown(quiet)
        self.assertEqual(sum("by class name" in line for line in out.splitlines()), 2)

    def test_anything_but_true_or_false_is_refused_before_any_tool(self):
        for value in ("", "yes", "True", "io.x.core.ATest"):
            with self.subTest(value=value):
                self.calls = []
                out, error = self.run_breakdown(self.fake(), breakdown=value)
                self.assertEqual(error, "core_breakdown must be true or false")
                self.assertEqual(self.calls, [])


def recorded(result="failed", failures=None, total=None, gradle="ok", unreadable=0):
    entries = failures if failures is not None else [
        {"suite": "core", "test": PRIVATE + " > shows", "kind": "failure", "message": SECRET_TEXT,
         "at": "android/core/src/test/kotlin/io/github/mayassistant/may/PrivateScreenTest.kt:42"}]
    outcome = {"result": result, "gradle": gradle,
               "suites": {"core": [2, 1 if result == "failed" else 0, 0], "playground": [5, 0, 0], "production": [2, 0, 0]}}
    return outcome, {"failures": entries, "total": len(entries) if total is None else total, "unreadable": unreadable}


class CommentTests(unittest.TestCase):
    def test_a_failing_run_lists_each_failing_test_in_a_fence_with_the_marker(self):
        body = comment_body(SHA, "https://github.com/May-Assistant/may-releases/actions/runs/9", *recorded())
        self.assertIn("### May Android unit suites: failed", body)
        self.assertIn("| core (`:core:test`) | failed | 2 | 1 | 0 |", body)
        self.assertIn("**1 failing test**", body)
        self.assertIn(f"core · {PRIVATE} > shows (failure)\n    {SECRET_TEXT}\n    at android/core/src/test/kotlin/", body)
        self.assertIn(f"<!-- may-android-unit-pr head={SHA} -->", body)
        self.assertIn("Run: https://github.com/May-Assistant/may-releases/actions/runs/9", body)
        self.assertTrue(body.endswith(tool.FOOTER))

    def test_a_backtick_in_a_message_cannot_close_the_fence(self):
        outcome, failed = recorded()
        failed["failures"][0]["message"] = "has ``` inside"
        body = comment_body(SHA, None, outcome, failed)
        self.assertIn("````text", body)

    def test_a_pass_and_a_failure_with_no_failing_test(self):
        self.assertIn("### May Android unit suites: passed", comment_body(SHA, None, *recorded("passed", [])))
        body = comment_body(SHA, None, *recorded("failed", [], gradle="timeout"))
        self.assertIn("Gradle did not finish within 50 minutes.", body)
        self.assertIn("No failing test was reported", body)

    def test_more_failures_than_shown_and_a_body_cut_to_fit(self):
        outcome, failed = recorded(total=250)
        self.assertIn("Showing the first 1; 249 more not shown.", comment_body(SHA, None, outcome, failed))
        many = [{"suite": "core", "test": "T > m" + "x" * 190, "kind": "failure", "message": "y" * 240, "at": None}] * 100
        body = comment_body(SHA, None, *recorded(failures=many))
        self.assertLessEqual(len(body), tool.MAX_BODY)

    def test_an_inexact_sha_is_refused(self):
        with self.assertRaises(UnitPostError):
            comment_body("main", None, *recorded())


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
        (self.root / "may-private" / android_unit.OUTCOME).write_text(json.dumps(outcome))
        (self.root / "may-private" / android_unit.FAILURES).write_text(json.dumps(failed))

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
        self.assertEqual(out, "May Android unit suites failed; 1 failing tests posted to may-app #42\n")
        self.assertEqual(len(self.posted()), 1)
        self.assertIn(SECRET_TEXT, self.posted()[0])
        self.assertNotIn(SECRET_TEXT, out)

    def test_a_pass_is_posted_and_passes(self):
        self.write(*recorded("passed", []))
        code, out = self.post(self.api())
        self.assertEqual((code, out), (0, "May Android unit suites passed; result posted to may-app #42\n"))

    def test_without_the_token_nothing_is_posted_and_the_job_still_fails(self):
        code, out = self.post(self.api(), token=None)
        self.assertEqual(code, 1)
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN is not set", out)
        self.assertEqual(self.calls, [])

    def test_the_step_summary_carries_counts_only_with_or_without_the_token(self):
        for token in (None, TOKEN):
            with self.subTest(token=bool(token)):
                summary = self.root / "summary.md"
                summary.unlink(missing_ok=True)
                self.write(*recorded(unreadable=2))
                self.post(self.api(), token=token, extra={"GITHUB_STEP_SUMMARY": str(summary)})
                self.assertEqual(summary.read_text(), "\n".join([
                    "### May Android unit suites: failed", "", "| Suite | Tests | Failed | Skipped |", "|---|---|---|---|",
                    "| core | 2 | 1 | 0 |", "| playground | 5 | 0 | 0 |", "| production | 2 | 0 | 0 |", "",
                    "1 failing tests and 2 unreadable reports. Which tests failed goes only to the may-app pull request, "
                    "never here."]) + "\n")
                self.assertNotIn(PRIVATE, summary.read_text())
                self.assertNotIn(SECRET_TEXT, summary.read_text())

    def test_a_suite_that_did_not_run_is_summarised_as_such(self):
        outcome, failed = recorded()
        outcome["suites"]["production"] = None
        self.assertIn("| production | did not run | – | – |", tool.summary_lines(outcome, failed))

    def test_a_head_already_reported_is_not_posted_again(self):
        code, out = self.post(self.api(comments=[{"body": f"x <!-- may-android-unit-pr head={SHA} -->"}]))
        self.assertEqual(code, 1)
        self.assertEqual(self.posted(), [])
        self.assertIn("already posted", out)

    def test_a_moved_closed_or_forked_pull_request_gets_nothing(self):
        for case, answer in (("moved", pull(head={"sha": OTHER, "ref": "f", "repo": {"full_name": "May-Assistant/may-app"}})),
                             ("closed", pull(state="closed")),
                             ("fork", pull(head={"sha": SHA, "ref": "f", "repo": {"full_name": "someone/may-app"}}))):
            with self.subTest(case=case):
                self.calls = []
                with self.assertRaises(UnitPostError):
                    self.post(self.api(pull_answer=answer))
                self.assertEqual(self.posted(), [])

    def test_a_body_holding_the_token_is_never_posted(self):
        outcome, failed = recorded()
        failed["failures"][0]["message"] = "x " + TOKEN
        self.write(outcome, failed)
        with self.assertRaises(UnitPostError) as caught:
            self.post(self.api())
        self.assertIn("holds a credential", caught.exception.args[0])
        self.assertEqual(self.calls, [])

    def test_a_missing_or_malformed_record_is_refused(self):
        outcome, failed = recorded()
        cases = {
            "an unknown result": ({**outcome, "result": "maybe"}, failed),
            "a missing suite": ({**outcome, "suites": {"core": [1, 0, 0]}}, failed),
            "a negative count": ({**outcome, "suites": {**outcome["suites"], "core": [1, -1, 0]}}, failed),
            "a boolean count": ({**outcome, "suites": {**outcome["suites"], "core": [True, 0, 0]}}, failed),
            "an unknown suite": (outcome, {**failed, "failures": [{**failed["failures"][0], "suite": "other"}]}),
            "a multi-line message": (outcome, {**failed, "failures": [{**failed["failures"][0], "message": "a\nb"}]}),
            "a too-long name": (outcome, {**failed, "failures": [{**failed["failures"][0], "test": "x" * 201}]}),
            "a total below the list": (outcome, {**failed, "total": 0}),
        }
        for case, (o, f) in cases.items():
            with self.subTest(case=case):
                self.write(o, f)
                self.calls = []
                with self.assertRaises(UnitPostError):
                    self.post(self.api())
                self.assertEqual(self.calls, [])
        (self.root / "may-private" / android_unit.OUTCOME).unlink()
        with self.assertRaises(UnitPostError):
            self.post(self.api())

    def test_another_steps_secret_refuses_before_anything(self):
        for secret in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN"):
            with self.subTest(secret=secret), self.assertRaises(StoreError):
                self.post(self.api(), extra={secret: "x"})
        self.assertEqual(self.calls, [])

    def test_only_the_pull_request_and_its_comments_are_reachable(self):
        with self.assertRaises(Exception):
            tool.github("GET", "/repos/May-Assistant/may-releases/pulls/1", None, TOKEN)
        with self.assertRaises(Exception):
            tool.github("DELETE", COMMENTS + "/1", None, TOKEN)


class CliTests(unittest.TestCase):
    def test_a_refusal_prints_only_a_fixed_line(self):
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", tool.__file__, "--source-sha", SHA, "--pr-number", "42"],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(),
                         "May Android unit post stopped: No unit-suite outcome is recorded; nothing was posted")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def flagged(self, relative, old, new, message, case):
        self.tearDown()
        self.setUp()
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, "mutation anchor not found: " + case)
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        self.assertTrue(any(message in problem for problem in found), (case, found))

    def test_the_post_step_holds_the_comment_token_alone(self):
        name = "android-unit-pr.yml: "
        secret = name + "a secret or env entry other than"
        runs = name + "runs something other than the gate"
        suites = "--source-sha \"$MAY_SOURCE_SHA\" --record-outcome --core-breakdown \"$MAY_CORE_BREAKDOWN\"\n"
        post_env = "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n"
        suites_step = "      - name: Run the unit suites without any credential, printing only pass or fail and counts\n        env:\n"
        cases = {
            "the comment token on the suites": (suites_step, suites_step + post_env, secret),
            "GH_TOKEN on the post": (post_env, post_env + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a read key on the post": (post_env, post_env + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the PR read token on the post": (post_env, post_env + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "the post without its token": (post_env, "", secret),
            "the post without the source SHA": ("          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n"
                                                "          MAY_APP_PR_COMMENT_TOKEN", "          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n"
                                                "          MAY_APP_PR_COMMENT_TOKEN", secret),
            "the suites failing the step": (suites, "--source-sha \"$MAY_SOURCE_SHA\" --core-breakdown \"$MAY_CORE_BREAKDOWN\"\n", runs),
            "no post": ("      - name: Post the failing tests on the may-app pull request, never in this log\n", "      - name: Gone\n",
                        name + "unit's steps are not exactly"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_breakdown_input_is_an_optional_boolean_off_by_default_on_the_suites_alone(self):
        name = "android-unit-pr.yml: "
        dispatch = name + "is not dispatched with exactly pr_number, source_sha and an optional core_breakdown boolean"
        secret = name + "a secret or env entry other than"
        breakdown_env = "          MAY_CORE_BREAKDOWN: ${{ inputs.core_breakdown }}\n"
        post_env = "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n"
        cases = {
            "a string input (names)": ("        type: boolean\n", "        type: string\n", dispatch),
            "on by default": ("        default: false\n", "        default: true\n", dispatch),
            "required": ("        required: false\n        type: boolean\n", "        required: true\n        type: boolean\n", dispatch),
            "a second input": ("        default: false\n", "        default: false\n      tests:\n        description: x\n"
                               "        required: false\n        type: string\n", dispatch),
            "the breakdown on the post": (post_env, post_env + breakdown_env, secret),
            "the suites without it": (breakdown_env, "", secret),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_breakdown_prints_numbers_only(self):
        line = 'print("May Android unit suite core: no failing test case to locate")'
        self.flagged("tools/may_release/android_unit.py", line, 'print(f"located {tree}")',
                     "prints something other than a fixed line", "a printed report element")

    def test_the_step_summary_formats_nothing_but_suite_names_and_counts(self):
        message = "the step summary formats something other than fixed words, suite names and counts"
        row = 'lines.append(f"| {name} | {int(counts[0])} | {int(counts[1])} | {int(counts[2])} |")'
        cases = {
            "a failing test's name": (row, row + "\n    lines += [f\"{failed['failures'][0]['test']}\"]", message),
            "a raw count": ("{int(counts[0])}", "{counts[0]}", message),
            "another loop": ("    for name in SUITES:\n", "    for name in failed['failures']:\n", message),
            "the summary from outside write_summary": ("    write_summary(summary_lines(outcome, failed))\n",
                                                       "    write_summary(summary_lines(outcome, failed))\n    os.environ.get(\"GITHUB_STEP_SUMMARY\")\n",
                                                       "writes a GitHub Actions file"),
        }
        for case, (old, new, problem) in cases.items():
            with self.subTest(case=case):
                self.flagged("tools/may_release/android_unit_post.py", old, new, problem, case)

    def test_the_post_tool_prints_only_fixed_lines_the_number_and_the_count(self):
        posted = 'print(f"May Android unit suites failed; {int(total)} failing tests posted to may-app #{number}")'
        cases = {
            "a printed body": (posted, posted + "\n    print(body)", "prints something other than a fixed line"),
            "a printed failure": (posted, 'print(f"failed: {failed}")', "prints something other than a fixed line"),
            "a formatted refusal": ('raise UnitPostError("The tested checkout is not the dispatched commit; nothing was posted")',
                                    'raise UnitPostError("not " + str(checkout))', "printed error message is not one string literal"),
            "an exit built from data": ('raise SystemExit("May Android unit post stopped; check may-native-compile-pr, its secrets and the inputs") from None',
                                        'raise SystemExit("stopped: " + str(sys.exc_info()))', "exits with a message that is not a fixed literal"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged("tools/may_release/android_unit_post.py", old, new, message, case)

    def test_the_failing_tests_are_never_printed_by_the_suites_tool(self):
        done = 'print("May Android unit suites finished; any failing tests stay in this runner\'s private files for the next step")'
        self.flagged("tools/may_release/android_unit.py", done, done + "\n    print(json.dumps(results))",
                     "prints something other than a fixed line", "a printed record")


if __name__ == "__main__":
    unittest.main()
