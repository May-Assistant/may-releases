"""Backend failures, posted on the may-app pull request only (backend_unit.py --record-outcome, backend_unit_post.py,
backend-unit-pr.yml)."""

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

import backend_unit
import backend_unit_post as tool
from backend_unit_post import BackendPostError, comment_body
import sealed
import static_check
from store import StoreError

SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "D" * 50
WORKFLOW = ".github/workflows/backend-unit-pr.yml"
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"
PRIVATE = "PRIVATE-BACKEND-TEST-NAME-4b1d"
SECRET_TEXT = "PRIVATE-ASSERTION-TEXT-91f3"
STAGES_PASSED = dict.fromkeys(backend_unit.STAGES, "passed")
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))


def recorded(result="failed", failures=None, total=None, stages=None, typecheck=None, tails=None, tests=None):
    entries = failures if failures is not None else [
        {"test": PRIVATE, "message": SECRET_TEXT, "at": "backend/test/private.test.ts:12:3"}]
    errors = typecheck if typecheck is not None else []
    outcome = {"result": result,
               "stages": stages or dict(STAGES_PASSED, tests="failed" if result == "failed" else "passed"),
               "tests": tests if tests is not None else [3, 2 if result == "failed" else 3, 1 if result == "failed" else 0, 0],
               "migrations": [2, 2]}
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
        self.assertIn("### May backend checks: failed", body)
        self.assertIn("| Test suite (`npm test`) | failed |", body)
        self.assertIn("| Migrations from scratch, twice | passed |", body)
        self.assertIn("Tests: 3 tests, 2 passed, 1 failed, 0 skipped.", body)
        self.assertIn("Migrations: 2 applied of 2 file(s).", body)
        self.assertIn("**1 failing test**", body)
        self.assertIn(f"{PRIVATE}\n    {SECRET_TEXT}\n    at backend/test/private.test.ts:12:3", body)
        self.assertIn(f"<!-- may-backend-unit-pr head={SHA} -->", body)
        self.assertIn("Run: https://github.com/May-Assistant/may-releases/actions/runs/9", body)
        self.assertTrue(body.endswith(tool.FOOTER))

    def test_type_errors_and_log_tails_are_fenced(self):
        outcome, failed = recorded(failures=[], typecheck=[{"at": "backend/src/x.ts:1:2", "code": "TS2322", "message": "``` no"}],
                                   tails={"install": ["npm ERR! one", "npm ERR! two"]})
        body = comment_body(SHA, None, outcome, failed)
        self.assertIn("**1 type error**.", body)
        self.assertIn("````text\nbackend/src/x.ts:1:2: TS2322: ``` no\n````", body)
        self.assertIn("Last lines of the install log:\n\n```text\nnpm ERR! one\nnpm ERR! two\n```", body)

    def test_a_pass_and_a_failure_with_no_detail(self):
        self.assertIn("### May backend checks: passed", comment_body(SHA, None, *recorded("passed", [])))
        body = comment_body(SHA, None, *recorded("failed", [], tests=None))
        self.assertIn("No detail was recorded for the failure", body)

    def test_more_failures_than_shown_and_a_body_cut_to_fit(self):
        outcome, failed = recorded(total=250)
        self.assertIn("Showing the first 1; 249 more not shown.", comment_body(SHA, None, outcome, failed))
        many = [{"test": "T " + "x" * 190, "message": "y" * 240, "at": None}] * 100
        tails = {stage: ["z" * 240] * 40 for stage in ("install", "typecheck", "migrations", "tests")}
        body = comment_body(SHA, None, *recorded(failures=many, tails=tails))
        self.assertLessEqual(len(body), tool.MAX_BODY)
        self.assertIn("more not shown", body)

    def test_an_inexact_sha_is_refused(self):
        with self.assertRaises(BackendPostError):
            comment_body("main", None, *recorded())


class PostCase(unittest.TestCase):
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
        (self.root / "may-private" / backend_unit.OUTCOME).write_text(json.dumps(outcome))
        (self.root / "may-private" / backend_unit.FAILURES).write_text(json.dumps(failed))

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


class PostTests(PostCase):
    def verdict(self, extra=None):
        out = io.StringIO()
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), **(extra or {})}, clear=True), patch("sys.stdout", out):
            code = tool.verdict()
        return code, out.getvalue()

    def test_the_verdict_fails_the_backend_job_when_the_checks_failed_in_fixed_lines(self):
        self.assertEqual(self.verdict(), (1, "May backend checks failed; which tests goes to the may-app pull request "
                                             "only\n"))
        self.write(*recorded("passed", []))
        self.assertEqual(self.verdict(), (0, "May backend checks passed\n"))
        with self.assertRaises(StoreError):
            self.verdict({"MAY_APP_READ_KEY": "x"})

    def test_without_a_record_the_post_posts_nothing_and_leaves_the_verdict_to_backend(self):
        (self.root / "may-private" / backend_unit.OUTCOME).unlink()
        code, out = self.post(self.api())
        self.assertEqual((code, out), (0, "May backend checks: no record to post (MAY_RENDERS_RECIPIENT or "
                                          "MAY_RENDERS_DECRYPT_KEY may be unset in may-native-compile-pr); the backend "
                                          "job's verdict stands\n"))
        self.assertEqual(self.calls, [])

    def test_failures_are_posted_once_and_the_job_fails_with_a_fixed_line(self):
        code, out = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertEqual(out, "May backend checks failed; 1 failing tests and 0 type errors posted to may-app #42\n")
        self.assertEqual(len(self.posted()), 1)
        self.assertIn(SECRET_TEXT, self.posted()[0])
        self.assertNotIn(SECRET_TEXT, out)

    def test_a_pass_is_posted_and_passes(self):
        self.write(*recorded("passed", []))
        code, out = self.post(self.api())
        self.assertEqual((code, out), (0, "May backend checks passed; result posted to may-app #42\n"))

    def test_without_the_token_nothing_is_posted_and_the_job_still_fails(self):
        code, out = self.post(self.api(), token=None)
        self.assertEqual(code, 1)
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN is not set", out)
        self.assertEqual(self.calls, [])

    def test_the_step_summary_carries_verdicts_and_counts_only(self):
        summary = self.root / "summary.md"
        stages = dict(STAGES_PASSED, typecheck="failed", migrations="timeout", tests="failed")
        self.write(*recorded(stages=stages, typecheck=[{"at": "backend/x.ts:1:1", "code": "TS1", "message": SECRET_TEXT}]))
        self.verdict({"GITHUB_STEP_SUMMARY": str(summary)})
        self.post(self.api(), extra={"GITHUB_STEP_SUMMARY": str(self.root / "post-summary.md")})
        self.assertFalse((self.root / "post-summary.md").exists(), "the post job writes no summary")
        self.assertEqual(summary.read_text(), "\n".join([
            "### May backend checks: failed", "", "| Stage | Result |", "|---|---|",
            "| toolchain | passed |", "| install | passed |", "| typecheck | failed |",
            "| migrations | did not finish in time |", "| tests | failed |", "",
            "Tests: 3 tests, 2 passed, 1 failed, 0 skipped.", "",
            "1 failing tests and 1 type errors. Which ones goes only to the may-app pull request, never here."]) + "\n")
        self.assertNotIn(PRIVATE, summary.read_text())
        self.assertNotIn(SECRET_TEXT, summary.read_text())

    def test_a_run_with_no_test_summary_is_summarised_as_such(self):
        outcome, failed = recorded(stages=dict(STAGES_PASSED, install="failed", typecheck="not run", migrations="not run",
                                               tests="not run"))
        outcome["tests"] = None
        lines = tool.summary_lines(outcome, failed)
        self.assertIn("Tests: no test summary.", lines)
        self.assertIn("| tests | did not run |", lines)

    def test_a_head_already_reported_is_not_posted_again(self):
        code, out = self.post(self.api(comments=[{"body": f"x <!-- may-backend-unit-pr head={SHA} -->"}]))
        self.assertEqual(code, 1)
        self.assertEqual(self.posted(), [])
        self.assertIn("already posted", out)

    def test_the_android_lanes_marker_does_not_count(self):
        code, _ = self.post(self.api(comments=[{"body": f"x <!-- may-android-unit-pr head={SHA} -->"}]))
        self.assertEqual(len(self.posted()), 1)

    def test_a_moved_closed_or_forked_pull_request_gets_nothing(self):
        for case, answer in (("moved", pull(head={"sha": OTHER, "ref": "f", "repo": {"full_name": "May-Assistant/may-app"}})),
                             ("closed", pull(state="closed")),
                             ("fork", pull(head={"sha": SHA, "ref": "f", "repo": {"full_name": "someone/may-app"}}))):
            with self.subTest(case=case):
                self.calls = []
                with self.assertRaises(BackendPostError):
                    self.post(self.api(pull_answer=answer))
                self.assertEqual(self.posted(), [])

    def test_a_body_holding_the_token_is_never_posted(self):
        outcome, failed = recorded(tails={"install": ["x " + TOKEN]})
        self.write(outcome, failed)
        with self.assertRaises(BackendPostError) as caught:
            self.post(self.api())
        self.assertIn("holds a credential", caught.exception.args[0])
        self.assertEqual(self.calls, [])

    def test_a_missing_or_malformed_record_is_refused(self):
        outcome, failed = recorded()
        entry = failed["failures"][0]
        cases = {
            "an unknown result": ({**outcome, "result": "maybe"}, failed),
            "a missing stage": ({**outcome, "stages": {"tests": "passed"}}, failed),
            "an unknown verdict": ({**outcome, "stages": {**outcome["stages"], "tests": "flaky"}}, failed),
            "a negative count": ({**outcome, "tests": [1, -1, 0, 0]}, failed),
            "a boolean count": ({**outcome, "tests": [True, 0, 0, 0]}, failed),
            "three counts": ({**outcome, "tests": [1, 1, 0]}, failed),
            "an extra field": (outcome, {**failed, "failures": [{**entry, "suite": "x"}]}),
            "a multi-line message": (outcome, {**failed, "failures": [{**entry, "message": "a\nb"}]}),
            "a too-long name": (outcome, {**failed, "failures": [{**entry, "test": "x" * 201}]}),
            "a total below the list": (outcome, {**failed, "total": 0}),
            "an unknown tail": (outcome, {**failed, "tails": {"toolchain": ["x"]}}),
            "a too-long tail": (outcome, {**failed, "tails": {"install": ["x"] * 41}}),
            "a malformed type error": (outcome, {**failed, "typecheck": [{"at": "x", "code": "TS1"}], "typecheck_total": 1}),
        }
        for case, (o, f) in cases.items():
            with self.subTest(case=case):
                self.write(o, f)
                self.calls = []
                with self.assertRaises(BackendPostError):
                    self.post(self.api())
                self.assertEqual(self.calls, [])
        (self.root / "may-private" / backend_unit.FAILURES).unlink()
        with self.assertRaises(BackendPostError):
            self.post(self.api())

    def test_another_steps_secret_refuses_before_anything(self):
        for secret in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN"):
            with self.subTest(secret=secret), self.assertRaises(StoreError):
                self.post(self.api(), extra={secret: "x"})
        self.assertEqual(self.calls, [])

    def test_a_record_written_by_the_checks_is_accepted(self):
        source = self.root / "may-source"
        logs = {stage: self.root / "may-private" / name for stage, name in backend_unit.STAGES.items()}
        logs["install"].write_text("npm ERR! x\n")
        stages = dict(STAGES_PASSED, install="failed", typecheck="not run", migrations="not run", tests="not run")
        backend_unit.record_outcome(self.root / "may-private", source, logs, stages, None, None, False)
        code, out = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertIn("Last lines of the install log", self.posted()[0])


class SealTests(PostCase):
    """The backend job's seal (no secret) and the post job's open (the decrypt key alone), before age is run."""

    def step(self, call, extra=None, tools=None):
        out = io.StringIO()
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", **(extra or {})}, clear=True), \
                patch("sys.stdout", out), patch.object(tool, "head_revision", lambda checkout: SHA), \
                patch("sealed.toolchain.tool_path", side_effect=tools or self.no_age):
            value = call()
        return value, out.getvalue()

    def no_age(self, name, relative):
        raise AssertionError("age was run")

    def test_without_the_public_key_nothing_is_sealed_and_one_fixed_line_says_so(self):
        _, out = self.step(lambda: tool.seal(SHA))
        self.assertEqual(out, "May backend checks: MAY_RENDERS_RECIPIENT is not set in may-native-compile-pr; nothing is "
                              "sealed, so nothing will be posted\n")
        self.assertFalse((self.root / sealed.SEALED).exists())

    def test_a_moved_checkout_a_bad_record_or_any_secret_seals_nothing(self):
        with patch.object(tool, "head_revision", lambda checkout: OTHER), self.assertRaisesRegex(
                BackendPostError, "not the dispatched commit; nothing was sealed"):
            with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True):
                tool.seal(SHA)
        for secret in ("MAY_APP_READ_KEY", "MAY_APP_PR_COMMENT_TOKEN", "MAY_RENDERS_DECRYPT_KEY"):
            with self.subTest(secret=secret), self.assertRaises(StoreError):
                self.step(lambda: tool.seal(SHA), {secret: "x"})
        self.write({"result": "maybe"}, {})
        with self.assertRaises(BackendPostError):
            self.step(lambda: tool.seal(SHA), {sealed.RECIPIENT: "age1" + "q" * 58})
        self.assertFalse((self.root / sealed.SEALED).exists())

    def test_with_nothing_downloaded_the_open_says_so_and_holds_no_other_secret(self):
        _, out = self.step(lambda: tool.open_record(SHA), {sealed.DECRYPT_KEY: "k"})
        self.assertEqual(out, "May backend checks: no sealed record from this run attempt; nothing will be posted\n")
        for secret in ("MAY_APP_READ_KEY", "MAY_APP_PR_COMMENT_TOKEN"):
            with self.subTest(secret=secret), self.assertRaises(StoreError):
                self.step(lambda: tool.open_record(SHA), {sealed.DECRYPT_KEY: "k", secret: "x"})


@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class RoundTripTests(SealTests):
    """seal in the backend job, the one upload, open and post in the post job, with the system's age."""

    def setUp(self):
        super().setUp()
        # Found now, on the job's own PATH: each step runs with PATH /usr/bin:/bin, which CI's pinned age is not on.
        self.found = {name: shutil.which(name) for name in ("age", "age-keygen")}
        self.secret = next(line for line in subprocess.run([self.found["age-keygen"]], capture_output=True, text=True,
                                                           check=True).stdout.splitlines()
                           if line.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run([self.found["age-keygen"], "-y"], input=self.secret, capture_output=True, text=True,
                                     check=True).stdout.strip()

    def with_age(self, name, relative):
        return self.found[relative]

    def test_the_failures_cross_sealed_and_are_posted(self):
        _, out = self.step(lambda: tool.seal(SHA), {sealed.RECIPIENT: self.public}, self.with_age)
        self.assertEqual(out, "May backend checks: their record is encrypted for the post job\n")
        data = (self.root / sealed.SEALED / sealed.NAME).read_bytes()
        self.assertNotIn(SECRET_TEXT.encode(), data)
        # The post job is a fresh runner: nothing of the backend job's but the download.
        shutil.rmtree(self.root / "may-private")
        (self.root / sealed.DOWNLOAD).mkdir()
        (self.root / sealed.DOWNLOAD / sealed.NAME).write_bytes(data)
        _, out = self.step(lambda: tool.open_record(SHA), {sealed.RECIPIENT: self.public, sealed.DECRYPT_KEY: self.secret},
                           self.with_age)
        self.assertEqual(out, "May backend checks: their record is decrypted\n")
        code, out = self.post(self.api())
        self.assertEqual((code, out), (1, "May backend checks failed; 1 failing tests and 0 type errors posted to may-app #42\n"))
        self.assertIn(SECRET_TEXT, self.posted()[0])


class CliTests(unittest.TestCase):
    def test_a_refusal_prints_only_a_fixed_line(self):
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", tool.__file__, "seal", "--source-sha", SHA],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(),
                         "May backend post stopped: No backend outcome is recorded; nothing was posted")


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
        name = "backend-unit-pr.yml: "
        secret = name + "a secret or env entry other than"
        post_env = "          MAY_APP_PR_COMMENT_TOKEN: ${{ steps.ci-comment.outputs.token || secrets.MAY_APP_PR_COMMENT_TOKEN }}\n"
        checks_step = "      - name: Run the backend checks without any credential, printing only pass or fail and counts\n        env:\n"
        cases = {
            "the comment token on the checks": (checks_step, checks_step + post_env, secret),
            "GH_TOKEN on the post": (post_env, post_env + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a read key on the post": (post_env, post_env + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the post without its token": (post_env, "", secret),
            "no post": ("      - name: Post the backend failures on the may-app pull request, never in this log\n", "      - name: Gone\n",
                        name + "the jobs' steps are not exactly"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_job_that_runs_the_pull_requests_code_holds_no_app_key_token_or_decrypt_key(self):
        """The independent review of #133: the runner user has sudo and can read every secret its job references."""
        name = "backend-unit-pr.yml: "
        secret = name + "a secret or env entry other than"
        node = "      - name: Install the pinned, checksum-verified Node\n"
        mint = ("      - name: Mint the May CI token for MAY_APP_PR_COMMENT_TOKEN, before any pull request code\n"
                "        id: ci-comment\n        if: vars.MAY_CI_APP_ID != ''\n"
                "        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0\n"
                "        with:\n          app-id: ${{ vars.MAY_CI_APP_ID }}\n"
                "          private-key: ${{ secrets.MAY_CI_APP_PRIVATE_KEY }}\n          owner: May-Assistant\n"
                "          repositories: may-app\n          permission-pull-requests: write\n")
        seal_env = "          MAY_RENDERS_RECIPIENT: ${{ vars.MAY_RENDERS_RECIPIENT }}\n        run: python3 -I -S -B tools/may_release/backend_unit_post.py seal"
        open_env = "          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n"
        guard = "      - name: Check the database log holds no statement text, never printing it\n        if: always()\n"
        cases = {
            "the comment mint in backend": (node, mint + node, "a GitHub App token is minted or used other than by its pinned"),
            "the decrypt key on the seal": (seal_env, open_env + seal_env, secret),
            "the open without its key": (open_env, "", secret),
            "the record in the clear": ("          path: ${{ runner.temp }}/may-sealed/record.age\n",
                                        "          path: ${{ runner.temp }}/may-private\n", "sealed record as the only artifact"),
            "the post only on success": ("    if: ${{ !cancelled() && needs.admission.result == 'success' }}\n", "",
                                         "a condition or a timeout other than its own"),
            "the log guard only on success": (guard, guard.replace("        if: always()\n", ""),
                                              name + "the jobs' steps are not exactly"),
            "no service": ("    services:\n", "    servicez:\n", "the Postgres service is not exactly"),
            "no verdict": ("        run: python3 -I -S -B tools/may_release/backend_unit_post.py verdict\n",
                           "        run: echo\n", name + "runs something other than"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_step_summary_formats_nothing_but_stage_names_verdicts_and_counts(self):
        message = "the step summary formats something other than fixed words, stage names and counts"
        row = "lines.append(f\"| {name} | {WORDS[outcome['stages'][name]]} |\")"
        cases = {
            "a failing test's name": (row, row + "\n    lines += [f\"{failed['failures'][0]['test']}\"]", message),
            "a raw count": ("{int(counts[0])}", "{counts[0]}", message),
            "a raw verdict": ("{WORDS[outcome['stages'][name]]}", "{outcome['stages'][name]}", message),
            "another loop": ("    for name in STAGES:\n        lines.append(f\"| {name} | {WORDS",
                             "    for name in failed['tails']:\n        lines.append(f\"| {name} | {WORDS", message),
            "the summary from outside write_summary": ("    write_summary(summary_lines(outcome, failed))\n",
                                                       "    write_summary(summary_lines(outcome, failed))\n    os.environ.get(\"GITHUB_STEP_SUMMARY\")\n",
                                                       "writes a GitHub Actions file"),
        }
        for case, (old, new, problem) in cases.items():
            with self.subTest(case=case):
                self.flagged("tools/may_release/backend_unit_post.py", old, new, problem, case)

    def test_the_post_tool_prints_only_fixed_lines_the_number_and_the_counts(self):
        posted = ('print(f"May backend checks failed; {int(total)} failing tests and {int(errors)} type errors posted to '
                  'may-app #{number}")')
        cases = {
            "a printed body": (posted, posted + "\n    print(body)", "prints something other than a fixed line"),
            "a printed failure": (posted, 'print(f"failed: {failed}")', "prints something other than a fixed line"),
            "a formatted refusal": ('raise BackendPostError("The tested checkout is not the dispatched commit; nothing was sealed")',
                                    'raise BackendPostError("not " + str(checkout))', "printed error message is not one string literal"),
            "an exit built from data": ('raise SystemExit("May backend post stopped; check may-native-compile-pr, its secrets and the inputs") from None',
                                        'raise SystemExit("stopped: " + str(sys.exc_info()))', "exits with a message that is not a fixed literal"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged("tools/may_release/backend_unit_post.py", old, new, message, case)


if __name__ == "__main__":
    unittest.main()
