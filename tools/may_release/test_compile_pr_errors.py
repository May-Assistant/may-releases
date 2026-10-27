"""iOS compile errors of a may-app pull request head, posted on that pull request only (native-compile-pr.yml,
compile.py --record-outcome, compile_pr_errors.py)."""

import base64
import contextlib
import hashlib
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
import urllib.error

import compile as compile_tool
import compile_pr_errors as tool
from compile_pr_errors import CompilePrError, comment_body
from ds_compile import DsCompileError, diagnostics
from gate import GateDenial
import prepare
import static_check
from store import SECRET_NAMES, StoreError, require_step_secrets

SHA, OTHER = "a" * 40, "b" * 40
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))
TOKEN = "github_pat_" + "D" * 50
WORKFLOW = ".github/workflows/native-compile-pr.yml"
TOOL = "tools/may_release/compile_pr_errors.py"
# Text that stands for may-app source: it must never reach stdout, stderr or anything but the comment.
MARKER = "PRIVATE-APP-SOURCE-7c2e"
CK = "/Users/runner/work/_temp/may-source"
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"


def app_pull(**changes):
    """An open may-app pull request against main from may-app itself, head SHA, number 42."""
    value = {"number": 42, "state": "open", "user": {"login": "lane-bot"},
             "base": {"ref": "main", "repo": {"full_name": "May-Assistant/may-app"}},
             "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": "May-Assistant/may-app"}}}
    for path, new in changes.items():
        node = value
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = new
    return value


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---- extraction and redaction (ds_compile.diagnostics, with the design system excluded) ----

class ExtractionTests(unittest.TestCase):
    def errors(self, text, roots=(CK,)):
        return diagnostics(text, list(roots), exclude=tool.EXCLUDED)

    def test_keeps_only_may_app_file_errors_repo_relative_with_no_source_warning_or_note(self):
        log = "\n".join([
            f"{CK}/ios/May/Home/HomeView.swift:12:5: error: cannot find 'Foo' in scope",
            f"   12 |     let key = \"{MARKER}\"",
            "      |               ^~~",
            f"{CK}/ios/May/Home/HomeView.swift:12:5: warning: {MARKER}",
            f"{CK}/ios/May/Home/HomeView.swift:13:1: note: {MARKER}",
            f"{CK}/android/build-deps/may-design-system/swift/Sources/A.swift:1:1: error: {MARKER}",
            f"/Users/runner/work/_temp/may-derived/Build/{MARKER}.swift:1:1: error: {MARKER}",
            f"/Users/runner/work/_temp/may-packages/checkouts/x/{MARKER}.swift:1:1: error: {MARKER}",
            f"error: {MARKER} no such module",
            "** BUILD FAILED **",
            f"{CK}/ios/MayPlayground/App.swift:3:9: error: expected '}}'",
        ])
        errors, total = self.errors(log)
        self.assertEqual(errors, ["ios/May/Home/HomeView.swift:12:5: error: cannot find 'Foo' in scope",
                                  "ios/MayPlayground/App.swift:3:9: error: expected '}'"])
        self.assertEqual(total, 2)
        self.assertNotIn(MARKER, "\n".join(errors))

    def test_paths_outside_the_checkout_or_in_the_design_system_are_dropped(self):
        for path in (CK + "-other/ios/A.swift", CK + "/../may-private/ios-compile.log", CK,
                     CK + "/android/build-deps/may-design-system/A.swift", "relative/A.swift",
                     "/Applications/Xcode_26.3.app/Contents/Developer/SDK/Foo.h"):
            with self.subTest(path=path):
                self.assertEqual(self.errors(f"{path}:1:1: error: {MARKER}"), ([], 0))
        # A may-app folder that merely starts like the excluded one is kept.
        self.assertEqual(self.errors(f"{CK}/android/build-deps2/A.kt:1:1: error: e")[0], ["android/build-deps2/A.kt:1:1: error: e"])

    def test_duplicates_once_in_log_order_and_the_cap(self):
        line = f"{CK}/ios/A.swift:1:2: error: e"
        self.assertEqual(self.errors("\n".join([line, f"{CK}/ios/B.swift:1:2: error: f", line])),
                         (["ios/A.swift:1:2: error: e", "ios/B.swift:1:2: error: f"], 2))
        errors, total = self.errors("\n".join(f"{CK}/ios/A.swift:{n}:1: error: e" for n in range(1, 151)))
        self.assertEqual((len(errors), total, errors[-1]), (100, 150, "ios/A.swift:100:1: error: e"))

    def test_token_shaped_text_is_redacted(self):
        errors, _ = self.errors(f"{CK}/ios/A.swift:1:1: error: cannot find 'github_pat_{'Q' * 40}' in scope")
        self.assertEqual(errors, ["ios/A.swift:1:1: error: cannot find '[redacted]' in scope"])

    def test_the_design_system_compile_still_keeps_every_checkout_file(self):
        # The exclusion is this tool's alone: ds_compile's default excludes nothing.
        self.assertEqual(diagnostics(f"{CK}/android/build-deps/x.swift:1:1: error: e", [CK]),
                         (["android/build-deps/x.swift:1:1: error: e"], 1))


class CommentBodyTests(unittest.TestCase):
    URL = "https://github.com/May-Assistant/may-releases/actions/runs/123"

    def test_a_pass_names_the_head_scheme_and_xcode(self):
        body = comment_body(SHA, self.URL, True, [], 0)
        self.assertIn(f"`{SHA}` compiles with Xcode 26.3 (`xcodebuild`, scheme `MayPlayground`", body)
        self.assertTrue(body.endswith(f"<!-- may-ios-compile-pr head={SHA} -->" + tool.FOOTER))

    def test_a_failure_is_one_fenced_block_with_the_count_and_the_overflow(self):
        errors = [f"ios/A.swift:{n}:1: error: e" for n in range(1, 101)]
        body = comment_body(SHA, None, False, errors, 130)
        self.assertEqual(body.count("```text"), 1)
        self.assertIn("**130 errors**", body)
        self.assertIn("Showing the first 100; 30 more not shown.", body)
        self.assertNotIn("Run:", body)

    def test_a_failure_with_no_located_error_says_so_and_backticks_cannot_close_the_block(self):
        self.assertIn("holds no error located in a file of this repository", comment_body(SHA, None, False, [], 0))
        body = comment_body(SHA, None, False, ["ios/A.swift:1:1: error: ```` x"], 1)
        self.assertIn("`````text", body)
        with self.assertRaises(CompilePrError):
            comment_body("HEAD", None, True, [], 0)


# ---- the compile records its outcome (compile.py --record-outcome) ----

class RecordOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "may-source/ios").mkdir(parents=True)
        (self.root / "may-private").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def compile(self, record, fail_at=None, env=None):
        archive_bytes = b"fixture generator"
        is_dir = Path.is_dir
        calls = []

        def fake_run(argv, log, cwd=None, env=None):
            calls.append(argv[0])
            if argv[0] == "curl":
                Path(argv[argv.index("-o") + 1]).write_bytes(archive_bytes)
            elif fail_at and fail_at in argv[0]:
                raise compile_tool.CompileError("Native compile failed; diagnostics remain on protected runner")

        version = subprocess.CompletedProcess([], 0, b"Xcode 26.3\nBuild version x\n", b"")
        space = os.statvfs_result((4096, 4096, 10**8, 10**8, 10**8, 0, 0, 0, 0, 255))
        out = io.StringIO()
        with patch.dict(os.environ, env or {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin"}, clear=True), \
                patch.object(Path, "is_dir", lambda p: str(p).startswith("/Applications/Xcode_26.3.app") or is_dir(p)), \
                patch("compile.run", fake_run), patch("compile.subprocess.run", return_value=version), \
                patch("compile.XCODEGEN_SHA256", hashlib.sha256(archive_bytes).hexdigest()), \
                patch("compile.extract_generator", return_value=self.root / "gen/bin/xcodegen"), \
                patch("os.statvfs", return_value=space), contextlib.redirect_stdout(out):
            try:
                compile_tool.compile_native("ios", "playground", record)
                error = None
            except (compile_tool.CompileError, StoreError) as raised:
                error = raised
        recorded = self.root / "may-private" / compile_tool.OUTCOME
        return error, (json.loads(recorded.read_text()) if recorded.exists() else None), out.getvalue(), calls

    def test_a_failed_build_is_recorded_privately_and_does_not_fail_the_step(self):
        for fail_at in ("xcodebuild", "xcodegen"):
            with self.subTest(fail_at=fail_at):
                error, outcome, out, _ = self.compile(True, fail_at)
                self.assertIsNone(error)
                self.assertEqual(outcome, {"result": "failed"})
                self.assertEqual(out, "Protected May iOS compile finished; its outcome and log stay in this runner's private files\n")

    def test_a_passing_build_is_recorded_as_passed(self):
        error, outcome, _, calls = self.compile(True)
        self.assertEqual((error, outcome), (None, {"result": "passed"}))
        self.assertIn("xcodebuild", calls)

    def test_without_the_flag_a_failed_build_still_fails_and_records_nothing(self):
        error, outcome, out, _ = self.compile(False, "xcodebuild")
        self.assertIsInstance(error, compile_tool.CompileError)
        self.assertIsNone(outcome)
        self.assertEqual(out, "")

    def test_the_compile_refuses_the_comment_token_or_any_secret_and_android_records_nothing(self):
        for name in ("MAY_APP_PR_COMMENT_TOKEN", "MAY_APP_PR_READ_TOKEN", "MAY_APP_READ_KEY"):
            with self.subTest(secret=name):
                error, outcome, _, calls = self.compile(True, env={"RUNNER_TEMP": str(self.root), name: TOKEN})
                self.assertIsInstance(error, StoreError)
                self.assertEqual((outcome, calls), (None, []))
        with self.assertRaises(compile_tool.CompileError):
            compile_tool.compile_native("android", None, True)


# ---- the post ----

class PostCase(unittest.TestCase):
    LOG = "\n".join([
        f"{CK}/ios/May/Home/HomeView.swift:12:5: error: cannot find 'Foo' in scope",
        f"   12 |     let secret = \"{MARKER}\"",
        "      |     ^",
        f"{CK}/ios/May/Home/HomeView.swift:13:5: warning: {MARKER}",
        f"/Users/runner/work/_temp/may-derived/{MARKER}.swift:1:1: error: {MARKER}",
        f"{CK}/android/build-deps/may-design-system/{MARKER}.swift:1:1: error: {MARKER}",
        f"{CK}/ios/MayPlayground/App.swift:2:1: error: expected '}}'",
        f"{CK}/ios/MayPlayground/App.swift:2:1: error: expected '}}'",
        "** BUILD FAILED **",
    ])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.checkout, self.private = self.root / "may-source", self.root / "may-private"
        self.checkout.mkdir()
        self.private.mkdir()
        self.pull, self.calls, self.pages = app_pull(), [], {}

    def tearDown(self):
        self.temp.cleanup()

    def environ(self, **extra):
        env = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "GITHUB_RUN_ID": "987",
               "GITHUB_STEP_SUMMARY": str(self.root / "summary"), "GITHUB_OUTPUT": str(self.root / "output")}
        env.update(extra)
        return env

    def write(self, result="failed", log=None):
        """The native job's outcome and log, and the record its seal step makes of them, opened in the post job."""
        (self.private / compile_tool.OUTCOME).write_text(json.dumps({"result": result}))
        (self.private / tool.LOG).write_text((log or self.LOG).replace(CK, str(self.checkout)))
        self.record()

    def record(self, head=SHA):
        with patch("compile_pr_errors.head_revision", return_value=head):
            record = tool.compile_record(SHA, self.root)
        (self.private / tool.RECORD).write_text(json.dumps(record))
        return record

    def call_api(self, method, path, payload, token, status=201):
        self.calls.append((method, path, payload, token))
        if method == "GET" and "/comments?" in path:
            return 200, self.pages.get(int(path.rsplit("=", 1)[1]), [])
        return (200, self.pull) if method == "GET" else (status, {})

    def post(self, env=None, call_api=None, head=SHA):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env if env is not None else self.environ(MAY_APP_PR_COMMENT_TOKEN=TOKEN), clear=True), \
                patch("compile_pr_errors.head_revision", return_value=head), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                value, error = tool.post(SHA, "42", call_api or self.call_api), None
            except (CompilePrError, DsCompileError, GateDenial, StoreError) as raised:
                value, error = None, raised
        return value, error, out.getvalue(), err.getvalue()


class PostTests(PostCase):
    def test_a_failure_posts_one_comment_of_redacted_errors_and_prints_only_the_count(self):
        self.write()
        code, error, out, err = self.post()
        self.assertEqual((code, error, err), (1, None, ""))
        self.assertEqual(out, "May iOS compile failed; 2 errors posted to may-app #42\n")
        self.assertEqual([(m, p) for m, p, _, _ in self.calls],
                         [("GET", PULLS), ("GET", COMMENTS + "?per_page=100&page=1"), ("POST", COMMENTS)])
        body = self.calls[2][2]["body"]
        self.assertIn("ios/May/Home/HomeView.swift:12:5: error: cannot find 'Foo' in scope\n"
                      "ios/MayPlayground/App.swift:2:1: error: expected '}'\n```", body)
        self.assertEqual(body.count("App.swift"), 1, "deduped")
        self.assertNotIn(MARKER, body)
        self.assertNotIn(str(self.root), body, "paths are repo-relative")
        self.assertIn("https://github.com/May-Assistant/may-releases/actions/runs/987", body)
        self.assertNotIn(TOKEN, body)
        self.assertFalse((self.root / "summary").exists() or (self.root / "output").exists())

    def test_a_log_replaced_by_a_link_is_not_followed(self):
        # The build runs first and could leave a link to another file (security review of 2cf86ce, #3).
        self.write()
        elsewhere = self.root / "elsewhere.txt"
        elsewhere.write_text(f"{self.checkout}/ios/Secret.swift:1:1: error: {MARKER}")
        (self.private / tool.LOG).unlink()
        (self.private / tool.LOG).symlink_to(elsewhere)
        self.record()
        code, error, out, _ = self.post()
        self.assertEqual((code, error), (1, None))
        body = self.calls[-1][2]["body"]
        self.assertNotIn(MARKER, body)
        self.assertNotIn("Secret.swift", body)
        self.assertIn("holds no error located in a file of this repository", body)

    def test_a_pass_posts_that_it_compiles(self):
        self.write("passed", log=f"{CK}/ios/A.swift:1:1: error: {MARKER}")
        code, error, out, _ = self.post()
        self.assertEqual((code, error), (0, None))
        self.assertEqual(out, "May iOS compile passed with Xcode 26.3; result posted to may-app #42\n")
        self.assertNotIn(MARKER, self.calls[-1][2]["body"])

    def test_the_token_leaves_the_environment_at_once_and_is_only_a_bearer(self):
        self.write()
        with patch.dict(os.environ, self.environ(MAY_APP_PR_COMMENT_TOKEN=TOKEN), clear=True), \
                patch("compile_pr_errors.head_revision", return_value=SHA), contextlib.redirect_stdout(io.StringIO()):
            tool.post(SHA, "42", self.call_api)
            self.assertNotIn("MAY_APP_PR_COMMENT_TOKEN", os.environ)
        self.assertEqual({t for _, _, _, t in self.calls}, {TOKEN})

    def test_without_the_token_nothing_is_posted_one_fixed_line_and_the_job_keeps_its_outcome(self):
        for result, code in (("failed", 1), ("passed", 0)):
            for env in (self.environ(), self.environ(MAY_APP_PR_COMMENT_TOKEN="")):
                with self.subTest(result=result, set="MAY_APP_PR_COMMENT_TOKEN" in env):
                    self.write(result)
                    self.calls = []
                    value, error, out, err = self.post(env=env)
                    self.assertEqual((value, error, err, self.calls), (code, None, "", []))
                    self.assertEqual(out, "May iOS compile: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; "
                                          "no comment posted\n")

    def test_nothing_is_posted_once_the_pull_request_moved_closed_or_is_unreadable(self):
        for name, pull in (("moved", app_pull(head__sha=OTHER)), ("closed", app_pull(state="closed")),
                           ("retargeted", app_pull(base__ref="release")), ("fork", app_pull(head__repo=None))):
            with self.subTest(case=name):
                self.write()
                self.calls, self.pull = [], pull
                _, error, out, _ = self.post()
                self.assertEqual(error.args[0], "The pull request is no longer open at the compiled head; nothing was posted")
                self.assertEqual([m for m, _, _, _ in self.calls], ["GET"])
                self.assertEqual(out, "")
        _, error, _, _ = self.post(call_api=lambda *a: (404, None))
        self.assertIn("could not be read again", error.args[0])

    def test_nothing_is_posted_without_a_record_or_with_another_secret(self):
        """With no key pair set yet nothing is sealed, so the post job has no record: one fixed line, and native's own
        verdict stands (independent review of may-releases #130)."""
        code, error, out, err = self.post()
        self.assertEqual((code, error, err), (0, None, ""))
        self.assertEqual(out, "May iOS compile: no record to post (MAY_RENDERS_RECIPIENT or MAY_RENDERS_DECRYPT_KEY may "
                              "be unset in may-native-compile-pr); the native job's verdict stands\n")
        self.write()
        for name in ("MAY_APP_READ_KEY", "MAY_APP_PR_READ_TOKEN", "MAY_DS_PR_COMMENT_TOKEN"):
            with self.subTest(secret=name):
                _, error, _, _ = self.post(env=self.environ(MAY_APP_PR_COMMENT_TOKEN=TOKEN, **{name: "k"}))
                self.assertIsInstance(error, StoreError)
        _, error, _, _ = self.post(env=self.environ(MAY_APP_PR_COMMENT_TOKEN="short"))
        self.assertIn("malformed", error.args[0])
        self.assertEqual(self.calls, [])

    def test_a_refused_post_or_a_body_holding_the_token_prints_nothing(self):
        self.write()
        _, error, out, _ = self.post(call_api=lambda m, p, b, t: self.call_api(m, p, b, t, status=403))
        self.assertEqual(error.args[0], "GitHub did not accept the comment; nothing from the compile is shown here")
        self.assertEqual(out, "")
        self.calls = []
        self.write(log=f"{CK}/ios/A.swift:1:1: error: {base64.b64encode(TOKEN.encode()).decode()}")
        _, error, _, _ = self.post()
        self.assertEqual(error.args[0], "The comment holds a credential; nothing was posted")
        self.assertEqual(self.calls, [])


class DedupeTests(PostCase):
    def marked(self, sha, name="may-ios-compile-pr"):
        return {"body": f"x\n\n<!-- {name} head={sha} -->\n---"}

    def test_a_head_already_reported_is_not_posted_again(self):
        self.write()
        self.pages = {1: [{"body": "other"}] * 100, 2: [self.marked(SHA)]}
        code, error, out, _ = self.post()
        self.assertEqual((code, error), (1, None))
        self.assertEqual(out, "May iOS compile result already posted for this head on may-app #42; nothing posted again\n")
        self.assertNotIn("POST", [m for m, _, _, _ in self.calls])

    def test_another_head_or_the_design_system_marker_does_not_stop_the_post(self):
        self.write()
        self.pages = {1: [self.marked(OTHER), self.marked(SHA, "may-ds-swift-compile"), {"body": None}, "x"]}
        self.assertEqual(self.post()[:2], (1, None))
        self.assertEqual([m for m, _, _, _ in self.calls].count("POST"), 1)


class GitHubCallTests(unittest.TestCase):
    def test_only_the_may_app_pull_request_and_its_comments_are_reachable(self):
        seen = []

        def urlopen(request, timeout):
            seen.append((request.get_method(), request.full_url, dict(request.header_items())))
            return FakeResponse(b"{}", 201 if request.get_method() == "POST" else 200)

        with patch("urllib.request.urlopen", urlopen):
            self.assertEqual(tool.github("GET", PULLS, None, TOKEN)[0], 200)
            self.assertEqual(tool.github("GET", COMMENTS + "?per_page=100&page=10", None, TOKEN)[0], 200)
            self.assertEqual(tool.github("POST", COMMENTS, {"body": "b"}, TOKEN)[0], 201)
            for method, path in (("POST", "/repos/May-Assistant/may-design-system/issues/42/comments"),
                                 ("GET", "/repos/May-Assistant/may-design-system/pulls/42"),
                                 ("PATCH", PULLS), ("PUT", PULLS + "/merge"), ("POST", PULLS),
                                 ("POST", PULLS + "/reviews"), ("POST", PULLS + "/comments"),
                                 ("GET", COMMENTS), ("POST", COMMENTS + "?per_page=100&page=1"),
                                 ("GET", COMMENTS + "?per_page=100&page=11"), ("GET", COMMENTS + "?per_page=100&page=1&x=1"),
                                 ("POST", COMMENTS + "/../../43/comments"), ("PATCH", "/repos/May-Assistant/may-app/issues/comments/5"),
                                 ("DELETE", COMMENTS), ("GET", "/repos/May-Assistant/may-app/contents/ios"),
                                 ("POST", "/repos/May-Assistant/may-releases/issues/42/comments")):
                with self.subTest(method=method, path=path), self.assertRaises(CompilePrError):
                    tool.github(method, path, {"body": "b"}, TOKEN)
        self.assertEqual([(m, u.split("api.github.com", 1)[1]) for m, u, _ in seen],
                         [("GET", PULLS), ("GET", COMMENTS + "?per_page=100&page=10"), ("POST", COMMENTS)])
        self.assertTrue(all(h["Authorization"] == "Bearer " + TOKEN for _, _, h in seen))

    def test_an_http_error_is_a_status_never_its_body(self):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 422, MARKER, {}, io.BytesIO(MARKER.encode()))
        with patch("urllib.request.urlopen", urlopen):
            self.assertEqual(tool.github("GET", PULLS, None, TOKEN), (422, None))

    def test_the_may_app_pull_request_read_is_tried_again_after_no_answer(self):
        """Run 40468630357: android_unit_post's read of the may-app pull request (this github()) failed once, and two
        failing tests were never posted (coordinator, 27 Oct)."""
        answers, slept = [0, 502], []

        def urlopen(request, timeout):
            status = answers.pop(0) if answers else 200
            if status == 0:
                raise urllib.error.URLError("no answer")
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, MARKER, {}, io.BytesIO(MARKER.encode()))
            return FakeResponse(b"{}", 200)

        with patch("urllib.request.urlopen", urlopen), patch("time.sleep", slept.append):
            self.assertEqual(tool.github("GET", PULLS, None, TOKEN), (200, {}))
        self.assertEqual(len(slept), 2)


class CommandLineTests(PostCase):
    def test_the_cli_prints_fixed_lines_and_exits_non_zero_on_a_failed_compile(self):
        self.write(log=f"{CK}/ios/A.swift:1:1: error: {MARKER}\n")
        pull = app_pull()

        def urlopen(request, timeout):
            if request.get_method() == "GET":
                return FakeResponse(json.dumps([] if "/comments?" in request.full_url else pull).encode())
            return FakeResponse(b"{}", 201)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["compile_pr_errors.py", "post", "--source-sha", SHA, "--pr-number", "42"]), \
                patch.dict(os.environ, self.environ(MAY_APP_PR_COMMENT_TOKEN=TOKEN), clear=True), \
                patch("urllib.request.urlopen", urlopen), patch("compile_pr_errors.head_revision", return_value=SHA), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main()
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "May iOS compile failed; 1 errors posted to may-app #42\n")
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())
        self.assertNotIn("A.swift", out.getvalue() + err.getvalue())

    def test_the_comment_token_is_a_secret_no_other_step_may_hold(self):
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN", SECRET_NAMES)
        with patch.dict(os.environ, {"MAY_APP_PR_COMMENT_TOKEN": TOKEN}), self.assertRaises(StoreError):
            require_step_secrets(("MAY_APP_READ_KEY", "MAY_DS_READ_KEY"))


# ---- the native job seals its record; the post job opens it (independent review of may-releases #130) ----

class SealTests(PostCase):
    def seal(self, env, head=SHA, tools=None):
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("compile_pr_errors.head_revision", return_value=head), \
                patch("compile_pr_errors.toolchain.tool_path", side_effect=tools or (lambda name, rel: rel)), \
                contextlib.redirect_stdout(out):
            tool.seal(SHA)
        return out.getvalue()

    def test_the_record_comes_from_the_log_and_the_compiled_head(self):
        (self.private / compile_tool.OUTCOME).write_text(json.dumps({"result": "failed"}))
        (self.private / tool.LOG).write_text(self.LOG.replace(CK, str(self.checkout)))
        with patch("compile_pr_errors.head_revision", return_value=SHA):
            record = tool.compile_record(SHA, self.root)
        self.assertEqual(record, {"sha": SHA, "passed": False, "total": 2, "errors": [
            "ios/May/Home/HomeView.swift:12:5: error: cannot find 'Foo' in scope",
            "ios/MayPlayground/App.swift:2:1: error: expected '}'"]})
        with patch("compile_pr_errors.head_revision", return_value=OTHER), self.assertRaisesRegex(
                CompilePrError, "not the dispatched commit"):
            tool.compile_record(SHA, self.root)
        (self.private / compile_tool.OUTCOME).unlink()
        with self.assertRaisesRegex(CompilePrError, "No compile outcome is recorded"):
            tool.compile_record(SHA, self.root)

    def test_without_the_recipient_nothing_is_sealed_and_one_fixed_line_says_so(self):
        self.write()
        for env in (self.environ(), self.environ(MAY_RENDERS_RECIPIENT=" ")):
            with self.subTest(set="MAY_RENDERS_RECIPIENT" in env):
                self.assertEqual(self.seal(env), "May iOS compile: MAY_RENDERS_RECIPIENT is not set in "
                                                 "may-native-compile-pr; nothing is sealed, so nothing will be posted\n")
                self.assertFalse((self.root / tool.SEALED).exists())

    def test_the_seal_holds_no_secret_and_refuses_a_bad_recipient(self):
        self.write()
        for name in ("MAY_APP_PR_COMMENT_TOKEN", "MAY_RENDERS_DECRYPT_KEY", "MAY_APP_READ_KEY"):
            with self.subTest(secret=name), self.assertRaises(StoreError):
                self.seal(self.environ(**{name: "k"}))
        with self.assertRaisesRegex(CompilePrError, "not an age public key"):
            self.seal(self.environ(MAY_RENDERS_RECIPIENT="age1short"))

    def test_the_verdict_fails_the_native_job_on_a_failed_compile(self):
        for result, code, line in (("failed", 1, "May iOS compile failed; its errors go to the may-app pull request "
                                                 "only\n"), ("passed", 0, "May iOS compile passed with Xcode 26.3\n")):
            with self.subTest(result=result):
                self.write(result)
                out = io.StringIO()
                with patch.dict(os.environ, self.environ(), clear=True), contextlib.redirect_stdout(out):
                    self.assertEqual(tool.verdict(), code)
                self.assertEqual(out.getvalue(), line)
        with patch.dict(os.environ, self.environ(MAY_APP_PR_COMMENT_TOKEN=TOKEN), clear=True), \
                self.assertRaises(StoreError):
            tool.verdict()

    def test_a_record_is_checked_whole(self):
        good = {"sha": SHA, "passed": False, "errors": ["ios/A.swift:1:1: error: e"], "total": 3}
        self.assertEqual(tool.checked_record(dict(good), SHA), good)
        for change in ({"sha": OTHER}, {"passed": "no"}, {"errors": "x"}, {"errors": ["a\nb"]}, {"errors": [""]},
                       {"errors": ["x" * 601]}, {"errors": ["e"] * 101, "total": 101}, {"total": 0}, {"total": -1},
                       {"total": True}, {"passed": True}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(CompilePrError):
                tool.checked_record(dict(good, **change), SHA)
        self.assertEqual(tool.checked_record({"sha": SHA, "passed": True, "errors": [], "total": 0}, SHA)["passed"], True)

    def test_opening_with_nothing_sealed_or_a_bad_key(self):
        def opened(env):
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
                tool.open_record(SHA)
            return out.getvalue()

        self.assertEqual(opened(self.environ(MAY_RENDERS_DECRYPT_KEY="AGE-SECRET-KEY-1x")),
                         "May iOS compile: no sealed record from this run attempt; nothing will be posted\n")
        (self.root / tool.DOWNLOAD).mkdir()
        (self.root / tool.DOWNLOAD / tool.SEALED_NAME).write_bytes(b"sealed")
        with self.assertRaisesRegex(CompilePrError, "MAY_RENDERS_RECIPIENT is not set"):
            opened(self.environ(MAY_RENDERS_DECRYPT_KEY="k"))
        recipient = "age1" + "q" * 58
        with self.assertRaisesRegex(CompilePrError, "decryption key is malformed"):
            opened(self.environ(MAY_RENDERS_DECRYPT_KEY="AGE-SECRET-KEY-1short", MAY_RENDERS_RECIPIENT=recipient))
        for name in ("MAY_APP_PR_COMMENT_TOKEN", "MAY_APP_READ_KEY"):
            with self.subTest(secret=name), self.assertRaises(StoreError):
                opened(self.environ(MAY_RENDERS_DECRYPT_KEY="k", **{name: "k"}))
        self.assertFalse((self.private / tool.RECORD).exists())


@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class SealedRoundTripTests(PostCase):
    """The native job's seal, the one upload, the post job's open and post, with the system's age."""

    def setUp(self):
        super().setUp()
        # Found now, on the job's own PATH: each step below runs with a cleared environment whose PATH is
        # /usr/bin:/bin, which CI's pinned age (toolchain.py age --export) is not on.
        self.found = {name: shutil.which(name) for name in ("age", "age-keygen")}
        self.secret = next(line for line in subprocess.run(["age-keygen"], capture_output=True, text=True,
                                                            check=True).stdout.splitlines()
                           if line.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run(["age-keygen", "-y"], input=self.secret, capture_output=True, text=True,
                                     check=True).stdout.strip()

    def tools(self, name, relative):
        return self.found[relative]

    def run_step(self, call, env):
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("compile_pr_errors.head_revision", return_value=SHA), \
                patch("compile_pr_errors.toolchain.tool_path", side_effect=self.tools), contextlib.redirect_stdout(out):
            value = call()
        return value, out.getvalue()

    def sealed_and_handed_over(self):
        (self.private / compile_tool.OUTCOME).write_text(json.dumps({"result": "failed"}))
        (self.private / tool.LOG).write_text(self.LOG.replace(CK, str(self.checkout)))
        _, out = self.run_step(lambda: tool.seal(SHA), self.environ(MAY_RENDERS_RECIPIENT=self.public))
        self.assertEqual(out, "May iOS compile: its record is encrypted for the post job\n")
        sealed = (self.root / tool.SEALED / tool.SEALED_NAME).read_bytes()
        self.assertNotIn(b"HomeView", sealed, "nothing in the clear leaves the native job")
        self.assertEqual([p.name for p in self.private.iterdir() if p.name == tool.RECORD], [], "no plain record left")
        # The upload and download stand in for the artifact; the post job is a fresh runner with no native files.
        shutil.rmtree(self.private)
        self.private.mkdir()
        shutil.rmtree(self.root / tool.DOWNLOAD, ignore_errors=True)
        (self.root / tool.DOWNLOAD).mkdir()
        (self.root / tool.DOWNLOAD / tool.SEALED_NAME).write_bytes(sealed)

    def test_the_record_crosses_from_native_to_post_sealed_and_is_posted(self):
        self.sealed_and_handed_over()
        _, out = self.run_step(lambda: tool.open_record(SHA),
                               self.environ(MAY_RENDERS_RECIPIENT=self.public, MAY_RENDERS_DECRYPT_KEY=self.secret))
        self.assertEqual(out, "May iOS compile: its record is decrypted and checked\n")
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), [tool.RECORD], "the key file is gone")
        code, error, out, _ = self.post()
        self.assertEqual((code, error, out), (1, None, "May iOS compile failed; 2 errors posted to may-app #42\n"))
        self.assertIn("ios/MayPlayground/App.swift:2:1: error: expected '}'", self.calls[-1][2]["body"])

    def test_every_sealed_record_is_the_same_size(self):
        """Independent review of #133: the ciphertext's size must not say how many errors a compile had."""
        sizes = []
        for result, log in (("passed", ""), ("failed", self.LOG),
                            ("failed", "\n".join(f"{CK}/ios/A{n}.swift:1:1: error: {'x' * 200}" for n in range(150)))):
            (self.private / compile_tool.OUTCOME).write_text(json.dumps({"result": result}))
            (self.private / tool.LOG).write_text(log.replace(CK, str(self.checkout)))
            self.run_step(lambda: tool.seal(SHA), self.environ(MAY_RENDERS_RECIPIENT=self.public))
            sizes.append((self.root / tool.SEALED / tool.SEALED_NAME).stat().st_size)
        self.assertEqual(len(set(sizes)), 1, sizes)
        # An unpadded record, sealed by anything but seal, is refused when opened.
        plain = self.root / "short.json"
        plain.write_text(json.dumps({"sha": SHA, "passed": True, "errors": [], "total": 0}))
        (self.root / tool.DOWNLOAD).mkdir()
        subprocess.run(["age", "-r", self.public, "-o", str(self.root / tool.DOWNLOAD / tool.SEALED_NAME), str(plain)],
                       check=True)
        with self.assertRaisesRegex(CompilePrError, "failed to decrypt or authenticate"):
            self.run_step(lambda: tool.open_record(SHA), self.environ(MAY_RENDERS_RECIPIENT=self.public,
                                                                    MAY_RENDERS_DECRYPT_KEY=self.secret))

    def test_another_key_a_tampered_record_or_another_commit_is_refused(self):
        self.sealed_and_handed_over()
        other = next(line for line in subprocess.run(["age-keygen"], capture_output=True, text=True,
                                                     check=True).stdout.splitlines() if line.startswith("AGE-SECRET"))
        env = self.environ(MAY_RENDERS_RECIPIENT=self.public, MAY_RENDERS_DECRYPT_KEY=other)
        with self.assertRaisesRegex(CompilePrError, "does not match MAY_RENDERS_RECIPIENT"):
            self.run_step(lambda: tool.open_record(SHA), env)
        sealed = self.root / tool.DOWNLOAD / tool.SEALED_NAME
        data = bytearray(sealed.read_bytes())
        data[-5] ^= 1
        sealed.write_bytes(bytes(data))
        env = self.environ(MAY_RENDERS_RECIPIENT=self.public, MAY_RENDERS_DECRYPT_KEY=self.secret)
        with self.assertRaisesRegex(CompilePrError, "failed to decrypt or authenticate"):
            self.run_step(lambda: tool.open_record(SHA), env)
        self.sealed_and_handed_over()
        with self.assertRaisesRegex(CompilePrError, "for another commit"):
            self.run_step(lambda: tool.open_record(OTHER), env)
        self.assertEqual(list(self.private.iterdir()), [], "nothing opened is left for the post")


# ---- the workflow and the printing are pinned ----

class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def flagged(self, relative_path, old, new, message):
        self.tearDown()
        self.setUp()
        path = self.root / relative_path
        text = path.read_text()
        self.assertIn(old, text, "mutation anchor not found")
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        self.assertTrue(any(message in problem for problem in found), (old, new, found))

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.compile_pr_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.compile_pr_errors_print_problems(static_check.ROOT), [])

    def test_each_widening_of_the_post_is_flagged(self):
        name = "native-compile-pr.yml: "
        secret = name + "a secret or env entry other than"
        runs = name + "runs something other than"
        build = "      - name: Compile without signing, store access or any credential\n"
        post = "      - name: Post the errors on the may-app pull request, never in this log\n"
        comment = "          MAY_APP_PR_COMMENT_TOKEN: ${{ steps.ci-comment.outputs.token || secrets.MAY_APP_PR_COMMENT_TOKEN }}\n"
        post_block = post + "        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n" \
            "          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n" + comment
        seal = "      - name: Seal the errors for the post job, with no secret\n        env:\n" \
            "          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n"
        recipient = "          MAY_RENDERS_RECIPIENT: ${{ vars.MAY_RENDERS_RECIPIENT }}\n"
        decrypt = "          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n"
        fetch_key = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        mint = ("      - name: Mint the May CI token for MAY_APP_PR_COMMENT_TOKEN, before any pull request code\n"
                "        id: ci-comment\n        if: vars.MAY_CI_APP_ID != ''\n"
                "        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0\n"
                "        with:\n          app-id: ${{ vars.MAY_CI_APP_ID }}\n"
                "          private-key: ${{ secrets.MAY_CI_APP_PRIVATE_KEY }}\n          owner: May-Assistant\n"
                "          repositories: may-app\n          permission-pull-requests: write\n")
        age = "      - name: Install pinned age, before any pull request code\n"
        upload_path = "          path: ${{ runner.temp }}/may-compile-sealed/compile.age\n"
        verdict = "      - name: Fail if the compile failed\n        run: python3 -I -S -B tools/may_release/compile_pr_errors.py verdict\n"
        cases = {
            "the comment token on the compile": (build, build + "        env:\n" + comment, secret),
            "the comment token in the fetch": (fetch_key, fetch_key + comment, secret),
            "the comment token on the seal": (seal, seal + comment, secret),
            "the decrypt key on the seal": (seal, seal + decrypt, secret),
            "the decrypt key on the post": (post_block, post_block + decrypt, secret),
            "a seal without the environment's key": (seal + recipient, seal, secret),
            "a read key in the post": (post_block, post_block + fetch_key, secret),
            "the read token in the post": (post_block, post_block + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "the runner token in the post": (post_block, post_block + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a compile that fails before the post": ("--target playground --record-outcome\n", "--target playground\n", runs),
            "no verdict in native": (verdict, "", runs),
            "the comment token minted in native again": (age, mint + age, name + "a GitHub App token is minted or used other "
                                                         "than by its pinned"),
            "the private log uploaded": (upload_path, "          path: ${{ runner.temp }}/may-private/ios-compile.log\n",
                                         name + "is not the iOS-only"),
            "a post that does not wait for native": ("    needs: [admission, native]\n", "    needs: admission\n",
                                                     name + "native's environment is not the one admission chose"),
            "no post": (post + "        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n", "      - name: Other\n        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n", name + "the jobs' steps are not exactly"),
            "printing the log": (post, "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/ios-compile.log\"\n" + post, runs),
            "a step summary": (post, "      - name: Summary\n        run: echo x >> \"$GITHUB_STEP_SUMMARY\"\n" + post, name + "is not the iOS-only"),
            "a post that always runs": (post, post + "        if: always()\n", name + "sets a shell, defaults, a condition"),
            "continue on error": (build, build + "        continue-on-error: true\n", name + "sets a shell, defaults, a condition"),
            "a write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  pull-requests: write\nconcurrency:",
                                   name + "permissions are not exactly read-only"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message)

    def test_printing_anything_from_may_app_is_flagged(self):
        anchor = '        print("May iOS compile: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")\n'
        cases = (
            '        print(str(checkout))\n', '        print(f"{number} {errors}")\n', '        print(errors[0])\n',
            '        sys.stdout.write("x")\n', '        open(os.environ["GITHUB_STEP_SUMMARY"], "a")\n',
            '        print("::notice::x")\n', '        os.write(1, b"x")\n', '        print("x", file=None)\n',
            '        raise SystemExit(errors[0])\n', '        getattr(sys, "stdout")\n', "        import logging\n",
        )
        for mutation in cases:
            with self.subTest(mutation=mutation.strip()):
                self.flagged(TOOL, anchor, mutation + anchor, "compile_pr_errors.py:")

    def test_its_errors_are_fixed_literals(self):
        self.flagged(TOOL, 'raise CompilePrError("The comment would be too long to post")',
                     'raise CompilePrError("The comment would be too long to post: " + body)',
                     "printed error message is not one string literal")


if __name__ == "__main__":
    unittest.main()
