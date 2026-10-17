"""Swift compile of a may-design-system pull request head (ds-swift-compile.yml, gate.py --action ds-compile, ds_compile.py)."""

import base64
import contextlib
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

import ds_compile
from ds_compile import DsCompileError, comment_body, diagnostics, relative, schemes_of
import gate
from gate import GateDenial, validate_ds_pull_request, validate_run
import prepare
import static_check
from store import SECRET_NAMES, StoreError

REPO, MAIN, DISPATCH = "May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch"
SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "C" * 50
WORKFLOW = ".github/workflows/ds-swift-compile.yml"
# Text that stands for design-system source: it must never reach stdout, stderr or anything but the comment.
MARKER = "PRIVATE-DS-SOURCE-91ab"
CK = "/Users/runner/work/_temp/may-ds-source"
ENVIRONMENT = {"can_admins_bypass": False, "protection_rules": [],
               "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}
POLICIES = {"branch_policies": [{"name": "main"}]}
REVIEWER_RULE = {"type": "required_reviewers", "prevent_self_review": False, "reviewers": [
    {"type": "User", "reviewer": {"id": 58840187, "login": "samuelalake"}}]}


def ds_pull(**changes):
    """An open may-design-system pull request against claude/may-integration from may-design-system itself, head SHA,
    number 40."""
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


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---- redaction ----

class DiagnosticsTests(unittest.TestCase):
    def errors(self, text, roots=(CK,), limit=ds_compile.MAX_ERRORS):
        return diagnostics(text, list(roots), limit)

    def test_keeps_only_file_errors_inside_the_checkout_repo_relative(self):
        log = "\n".join([
            f"{CK}/swift/Sources/A/View.swift:12:5: error: cannot find 'Foo' in scope",
            f"{CK}/swift/Sources/A/View.swift:12:5: warning: {MARKER} unused",
            f"{CK}/swift/Sources/A/View.swift:13:1: note: {MARKER} did you mean",
            f"{CK}/swift/Sources/A/View.swift:13:1: remark: {MARKER}",
            f"   12 |     let x = {MARKER}()",
            "      |             `- error: cannot find 'Foo' in scope",
            "      |             ^~~",
            f"error: {MARKER} no such module",
            f"\tSwiftCompile normal arm64 {CK}/swift/Sources/A/View.swift (in target 'A' from project '{MARKER}')",
            "** BUILD FAILED **",
            f"The following build commands failed: {MARKER}",
            f"{CK}/packages/b/Sources/B/B.swift:1:2: error: expected declaration",
        ])
        errors, total = self.errors(log)
        self.assertEqual(errors, ["swift/Sources/A/View.swift:12:5: error: cannot find 'Foo' in scope",
                                  "packages/b/Sources/B/B.swift:1:2: error: expected declaration"])
        self.assertEqual(total, 2)
        self.assertNotIn(MARKER, "\n".join(errors))

    def test_paths_outside_the_checkout_are_dropped(self):
        for path in ("/Users/runner/work/_temp/may-ds-derived/Build/Intermediates/X.swift",
                     "/Applications/Xcode_26.3.app/Contents/Developer/Platforms/SDK/Foo.h",
                     CK + "-other/swift/A.swift",  # a sibling sharing the prefix
                     CK + "/../may-ds-private/compile.log",  # escaping by '..'
                     CK,  # the checkout itself, no file
                     "//" + CK.lstrip("/") + "/A.swift",
                     "relative/A.swift"):
            with self.subTest(path=path):
                self.assertEqual(self.errors(f"{path}:1:1: error: {MARKER}"), ([], 0))

    def test_the_resolved_checkout_path_is_also_inside(self):
        errors, _ = self.errors("/private/var/ck/A.swift:3:4: error: e", roots=(CK, "/private/var/ck"))
        self.assertEqual(errors, ["A.swift:3:4: error: e"])
        self.assertEqual(relative(CK + "/./x/../A.swift", [CK]), "A.swift")

    def test_duplicates_are_posted_once_in_log_order(self):
        line = f"{CK}/A.swift:1:2: error: e"
        errors, total = self.errors("\n".join([line, f"{CK}/B.swift:1:2: error: f", line, f"{CK}/./A.swift:1:2: error: e"]))
        self.assertEqual(errors, ["A.swift:1:2: error: e", "B.swift:1:2: error: f"])
        self.assertEqual(total, 2)

    def test_the_cap_posts_the_first_hundred_and_counts_the_rest(self):
        errors, total = self.errors("\n".join(f"{CK}/A.swift:{n}:1: error: e" for n in range(1, 151)))
        self.assertEqual(len(errors), 100)
        self.assertEqual(errors[0], "A.swift:1:1: error: e")
        self.assertEqual(errors[-1], "A.swift:100:1: error: e")
        self.assertEqual(total, 150)

    def test_a_path_holding_a_colon_crlf_and_odd_characters(self):
        log = (f"{CK}/Sources/A:B/Wéird file ✓.swift:7:9: error: type 'X' has no member `y` @mention <b>\r\n"
               f"{CK}/T.swift:1:1: error: tab\there\r\n"
               f"\x1b[1m{CK}/C.swift:2:3: \x1b[31merror: \x1b[0mcoloured\x1b[0m\r\n"
               f"{CK}/D.swift:2:3: error: bell \x07 {MARKER}\r\n"
               f"{CK}/E\x01.swift:2:3: error: {MARKER}\r\n")
        errors, total = self.errors(log)
        self.assertEqual(errors, ["Sources/A:B/Wéird file ✓.swift:7:9: error: type 'X' has no member `y` @mention <b>",
                                  "T.swift:1:1: error: tab here", "C.swift:2:3: error: coloured"])
        self.assertEqual(total, 3)
        self.assertFalse(any("\r" in e or "\x1b" in e for e in errors))

    def test_fatal_errors_count_and_long_text_is_bounded(self):
        errors, _ = self.errors(f"{CK}/A.h:1:10: fatal error: 'B.h' file not found\n"
                                f"{CK}/A.swift:1:1: error: {'m' * 1000}\n"
                                f"{CK}/{'d/' * 200}A.swift:1:1: error: {MARKER}")
        self.assertEqual(errors[0], "A.h:1:10: error: 'B.h' file not found")
        self.assertEqual(len(errors), 2)
        self.assertLessEqual(len(errors[1]), len("A.swift:1:1: error: ") + ds_compile.MAX_MESSAGE)

    def test_token_shaped_text_is_redacted(self):
        errors, _ = self.errors(f"{CK}/A.swift:1:1: error: cannot find 'ghp_{'Q' * 36}' in scope")
        self.assertEqual(errors, ["A.swift:1:1: error: cannot find '[redacted]' in scope"])

    def test_an_empty_or_unrelated_log_has_no_error(self):
        self.assertEqual(self.errors(""), ([], 0))
        self.assertEqual(self.errors("** BUILD SUCCEEDED **\n"), ([], 0))


class CommentBodyTests(unittest.TestCase):
    URL = "https://github.com/May-Assistant/may-releases/actions/runs/123"

    def test_a_pass_names_the_head_and_xcode(self):
        body = comment_body(SHA, self.URL, True, [], 0)
        self.assertIn(f"`{SHA}` compiles with Xcode 26.3", body)
        self.assertIn("Run: " + self.URL, body)
        self.assertTrue(body.endswith(f"<!-- may-ds-swift-compile head={SHA} -->" + ds_compile.FOOTER))

    def test_a_failure_lists_the_errors_in_one_fenced_block_with_the_count(self):
        body = comment_body(SHA, self.URL, False, ["A.swift:1:2: error: e"], 1)
        self.assertIn(f"`{SHA}` does not compile with Xcode 26.3", body)
        self.assertIn("**1 error** in files of this repository", body)
        self.assertIn("```text\nA.swift:1:2: error: e\n```", body)
        self.assertNotIn("more not shown", body)

    def test_a_capped_failure_says_how_many_are_not_shown(self):
        body = comment_body(SHA, None, False, ["A.swift:1:1: error: e"] * 2, 150)
        self.assertIn("**150 errors**", body)
        self.assertIn("Showing the first 2; 148 more not shown.", body)
        self.assertNotIn("Run:", body)

    def test_a_failure_with_no_file_error_says_so(self):
        body = comment_body(SHA, self.URL, False, [], 0)
        self.assertIn("holds no error located in a file of this repository", body)
        self.assertNotIn("```", body)

    def test_backticks_in_a_message_cannot_close_the_block(self):
        body = comment_body(SHA, None, False, ["A.swift:1:1: error: ```` x"], 1)
        self.assertIn("\n`````text\nA.swift:1:1: error: ```` x\n`````\n", body)

    def test_the_head_must_be_exact(self):
        for sha in ("", "A" * 40, "a" * 39, None):
            with self.subTest(sha=sha), self.assertRaises(DsCompileError):
                comment_body(sha, None, True, [], 0)

    def test_the_run_url_is_this_repositorys_run_or_nothing(self):
        self.assertEqual(ds_compile.run_url({"GITHUB_RUN_ID": "123"}), self.URL)
        for value in ("", "x", "1/../2", "0"):
            self.assertIsNone(ds_compile.run_url({"GITHUB_RUN_ID": value}))


# ---- gate.py --action ds-compile ----

class GateTests(unittest.TestCase):
    def test_ds_compile_has_its_own_environment_with_no_reviewer(self):
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, SHA, "ds-compile"), "may-ds-renders")
        self.assertNotIn("may-ds-renders", {v for k, v in gate.ENVIRONMENTS.items() if k != "ds-compile"})
        self.assertEqual(gate.NO_REVIEWER_ACTIONS, {"compile-pr", "ds-compile"})
        for repo, ref, event in (("May-Assistant/may-design-system", MAIN, DISPATCH), (REPO, "refs/heads/x", DISPATCH),
                                 (REPO, MAIN, "push")):
            with self.subTest(repo=repo, ref=ref, event=event), self.assertRaises(GateDenial):
                validate_run(repo, ref, event, SHA, "ds-compile")
        with self.assertRaises(GateDenial):
            validate_run(REPO, MAIN, DISPATCH, "A" * 40, "ds-compile")

    def test_accepts_the_head_of_an_open_pull_request_against_the_integration_branch_or_main(self):
        validate_ds_pull_request(ds_pull(), 40, SHA)
        validate_ds_pull_request(ds_pull(base__ref="main"), 40, SHA)

    def test_refuses_everything_else(self):
        cases = {
            "not the head": (ds_pull(), OTHER, "not the pull request's current head"),
            "closed": (ds_pull(state="closed"), SHA, "not open"),
            "a fork": (ds_pull(head__repo={"full_name": "someone/may-design-system"}), SHA, "forks are refused"),
            "a deleted fork": (ds_pull(head__repo=None), SHA, "forks are refused"),
            "another base branch": (ds_pull(base__ref="release"), SHA, "not against may-design-system"),
            "another base repository": (ds_pull(base__repo={"full_name": "May-Assistant/may-app"}), SHA, "not against"),
            "another number": (ds_pull(number=41), SHA, "unreadable"),
            "no answer": (None, SHA, "unreadable"),
        }
        for name, (value, sha, message) in cases.items():
            with self.subTest(case=name), self.assertRaises(GateDenial) as caught:
                validate_ds_pull_request(value, 40, sha)
            self.assertIn(message, caught.exception.args[0])

    def run_gate(self, args, answers, env=None):
        seen = []

        def urlopen(request, timeout):
            seen.append((request.full_url, request.headers.get("Authorization")))
            answer = answers.get(request.full_url.split("api.github.com/repos/", 1)[1])
            if answer is None:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return FakeResponse(json.dumps(answer).encode())

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            environ = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                       "GITHUB_TRIGGERING_ACTOR": "coordinator[bot]", "GH_TOKEN": "control-token",
                       "MAY_DS_PR_READ_TOKEN": "ds-read-token", "GITHUB_OUTPUT": str(output)}
            environ.update(env or {})
            environ = {k: v for k, v in environ.items() if v is not None}
            with patch.dict(os.environ, environ, clear=True), patch("urllib.request.urlopen", urlopen), \
                    patch.object(sys, "argv", ["gate.py"] + args), patch("sys.stdout", out):
                try:
                    gate.main()
                    denial = None
                except GateDenial as error:
                    denial = error.args[0]
            written = output.read_text() if output.exists() else ""
        return denial, seen, written, out.getvalue()

    def answers(self, value, environment=ENVIRONMENT, policies=POLICIES):
        return {"May-Assistant/may-design-system/pulls/40": value,
                "May-Assistant/may-releases/environments/may-ds-renders": environment,
                "May-Assistant/may-releases/environments/may-ds-renders/deployment-branch-policies?per_page=100": policies}

    ADMIT = ["--action", "ds-compile", "--source-sha", SHA, "--pr-number", "40"]
    CHECK = ADMIT + ["--check-pull"]

    def test_admission_reads_only_its_environment_and_holds_no_token(self):
        denial, seen, written, out = self.run_gate(self.ADMIT, self.answers(ds_pull()), env={"MAY_DS_PR_READ_TOKEN": None})
        self.assertIsNone(denial)
        self.assertEqual(written, "environment=may-ds-renders\nbuild_environment=may-ds-renders\n")
        self.assertFalse([u for u, _ in seen if "/may-design-system/" in u])
        self.assertEqual({u.split("/environments/", 1)[1].split("/", 1)[0] for u, _ in seen}, {"may-ds-renders"})
        self.assertEqual(out.strip(), f"May ds-compile admission passed for {SHA} (may-design-system #40; dispatched by coordinator[bot])")

    def test_the_check_reads_the_pull_request_with_its_own_token_and_prints_nothing_from_it(self):
        denial, seen, _, out = self.run_gate(self.CHECK, self.answers(ds_pull(user={"login": MARKER}, title=MARKER)))
        self.assertIsNone(denial)
        self.assertIn(("https://api.github.com/repos/May-Assistant/may-design-system/pulls/40", "Bearer ds-read-token"), seen)
        for url, auth in seen:
            self.assertEqual(auth, "Bearer ds-read-token" if "/may-design-system/" in url else "Bearer control-token")
        self.assertEqual(out.strip(), f"May ds-compile pull request check passed for {SHA} (may-design-system #40; "
                                      "dispatched by coordinator[bot])")
        self.assertNotIn(MARKER, out)

    def test_the_check_refuses_a_moved_head_a_closed_pull_a_fork_or_a_missing_token(self):
        for name, value, env, message in (
                ("moved", ds_pull(head__sha=OTHER), None, "not the pull request's current head"),
                ("closed", ds_pull(state="closed"), None, "not open"),
                ("fork", ds_pull(head__repo={"full_name": "x/may-design-system"}), None, "forks"),
                ("no token", ds_pull(), {"MAY_DS_PR_READ_TOKEN": None}, "read token unavailable")):
            with self.subTest(case=name):
                denial, seen, written, _ = self.run_gate(self.CHECK, self.answers(value), env=env)
                self.assertIn(message, denial)
                self.assertEqual(written, "")
                self.assertFalse([u for u, _ in seen if "/environments/" in u])

    def test_the_environment_has_no_reviewer_no_admin_bypass_and_main_only(self):
        cases = {"reviewer": (dict(ENVIRONMENT, protection_rules=[REVIEWER_RULE]), POLICIES, "must have no required reviewer"),
                 "bypass": (dict(ENVIRONMENT, can_admins_bypass=True), POLICIES, "Administrator bypass"),
                 "branches": (ENVIRONMENT, {"branch_policies": [{"name": "main"}, {"name": "x"}]}, "allow only main"),
                 "missing": (None, POLICIES, "does not exist")}
        for name, (environment, policies, message) in cases.items():
            for args in (self.ADMIT, self.CHECK):
                with self.subTest(case=name, check="--check-pull" in args):
                    denial, _, written, _ = self.run_gate(args, self.answers(ds_pull(), environment, policies))
                    self.assertIn(message, denial)
                    self.assertEqual(written, "")

    def test_the_number_is_given_with_ds_compile_and_only_then(self):
        for args in (["--action", "ds-compile", "--source-sha", SHA], ["--action", "compile", "--source-sha", SHA, "--check-pull"]):
            with self.subTest(args=args):
                denial, seen, _, _ = self.run_gate(args, self.answers(ds_pull()))
                self.assertIsNotNone(denial)
                self.assertEqual(seen, [])

    def test_pull_requests_are_read_only_from_may_app_or_may_design_system(self):
        with self.assertRaises(GateDenial):
            gate.get_app_pull(1, "t", "May-Assistant/may-releases")


# ---- the runner steps ----

class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.checkout = self.root / ds_compile.SOURCE
        self.private = self.root / ds_compile.PRIVATE

    def tearDown(self):
        self.temp.cleanup()

    def environ(self, **extra):
        env = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp",
               "GITHUB_RUN_ID": "987", "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
               "GITHUB_OUTPUT": str(self.root / "output")}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def captured(self, function, *args, env=None, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env if env is not None else self.environ(), clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                value, error = function(*args, **kwargs), None
            except (DsCompileError, GateDenial, StoreError, prepare.PrepareError) as raised:
                value, error = None, raised
        return value, error, out.getvalue(), err.getvalue()


class FetchTests(RunnerCase):
    def fetch(self, env=None, head=SHA):
        calls = []
        with patch("prepare.quiet", side_effect=lambda argv, log, env: calls.append(argv)), \
                patch("prepare.git_status", return_value=0), patch("prepare.head_of", return_value=head), \
                patch("ds_compile.head_revision", return_value=head):
            result = self.captured(ds_compile.fetch, SHA, "40", env=env or self.environ(MAY_DS_READ_KEY="fixture read key"))
        return result, calls

    def test_fetches_the_pull_request_head_with_the_read_key_and_removes_it(self):
        (_, error, out, err), calls = self.fetch()
        self.assertIsNone(error)
        self.assertEqual(out, "Exact may-design-system commit fetched quietly; the read key and host file are removed\n")
        self.assertEqual(err, "")
        self.assertIn(["git", "-C", str(self.checkout), "remote", "add", "origin", "git@github.com:May-Assistant/may-design-system.git"], calls)
        self.assertEqual([a for a in calls if "fetch" in a][0][-1], "+refs/pull/40/head:refs/remotes/origin/pull-head")
        left = [p.name for p in self.private.iterdir()]
        self.assertEqual([n for n in left if n.endswith(".key") or n == "known_hosts"], [], "key and host file removed")
        self.assertEqual(oct(self.private.stat().st_mode & 0o777), "0o700")

    def test_a_moved_head_is_not_checked_out(self):
        (_, error, out, _), calls = self.fetch(head=OTHER)
        self.assertIsInstance(error, prepare.PrepareError)
        self.assertFalse(any("checkout" in a for a in calls))
        self.assertEqual(out, "")

    def test_refuses_another_secret_or_no_key(self):
        for name, env in (("comment token too", self.environ(MAY_DS_READ_KEY="k", MAY_DS_PR_COMMENT_TOKEN=TOKEN)),
                          ("read token too", self.environ(MAY_DS_READ_KEY="k", MAY_DS_PR_READ_TOKEN="t")),
                          ("no key", self.environ())):
            with self.subTest(case=name):
                shutil.rmtree(self.private, ignore_errors=True)
                (_, error, _, _), calls = self.fetch(env=env)
                self.assertIsNotNone(error)
                self.assertFalse([a for a in calls if "fetch" in a])


class FakeXcode:
    """Answers xcodebuild: -version, -list -json, and each build (writing MARKER text to the private log)."""

    def __init__(self, schemes=("MayDesignSystem-Package", "MayDesignTokens"), code=0, version=b"Xcode 26.3\nBuild version 17C1\n"):
        self.schemes, self.code, self.version, self.calls = list(schemes), code, version, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1:] == ["-version"]:
            return subprocess.CompletedProcess(argv, 0, self.version, b"")
        if argv[1:] == ["-list", "-json"]:
            kwargs["stdout"].write(json.dumps({"workspace": {"name": MARKER, "schemes": self.schemes}}).encode())
            return subprocess.CompletedProcess(argv, 0)
        kwargs["stdout"].write(f"{CK}/swift/A.swift:1:2: error: {MARKER}\n   1 | {MARKER}\n".encode())
        return subprocess.CompletedProcess(argv, self.code)


class CompileTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.checkout.mkdir()
        self.private.mkdir(mode=0o700)
        self.developer = self.root / "Xcode_26.3.app"
        self.developer.mkdir()

    def compile(self, xcode, env=None):
        space = os.statvfs_result((4096, 4096, 10**8, 10**8, 10**8, 10**6, 10**6, 10**6, 0, 255))
        with patch("os.statvfs", return_value=space):
            return self.captured(ds_compile.compile_ds, xcode, self.developer, env=env)

    def test_builds_the_package_scheme_for_the_simulator_without_signing_or_any_secret(self):
        xcode = FakeXcode()
        _, error, out, err = self.compile(xcode)
        self.assertIsNone(error)
        self.assertEqual(out, "DS Swift compile finished; its output and outcome stay in this runner's private files\n")
        self.assertEqual(err, "")
        builds = [argv for argv, _ in xcode.calls if "build" in argv]
        self.assertEqual(len(builds), 1, "only the package-wide scheme")
        self.assertEqual(builds[0][builds[0].index("-scheme") + 1], "MayDesignSystem-Package")
        for flag, value in (("-destination", "generic/platform=iOS Simulator"), ("-configuration", "Release"),
                            ("-derivedDataPath", str(self.root / "may-ds-derived")),
                            ("-clonedSourcePackagesDirPath", str(self.root / "may-ds-packages"))):
            self.assertEqual(builds[0][builds[0].index(flag) + 1], value)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", builds[0])
        for argv, kwargs in xcode.calls:
            env = kwargs["env"]
            self.assertFalse(set(env) & set(SECRET_NAMES), argv)
            self.assertFalse([k for k in env if k.startswith(("GITHUB_", "ACTIONS_", "GH_"))], "no Actions file or token")
            self.assertEqual(env["DEVELOPER_DIR"], str(ds_compile.DEVELOPER))
        self.assertEqual(json.loads((self.private / "result.json").read_text())["result"], "passed")
        self.assertIn(MARKER, (self.private / "compile.log").read_text(), "the full output stays in the private log")

    def test_a_failed_build_is_recorded_privately_and_does_not_print(self):
        _, error, out, _ = self.compile(FakeXcode(code=65))
        self.assertIsNone(error)
        self.assertNotIn(MARKER, out)
        self.assertEqual(json.loads((self.private / "result.json").read_text())["result"], "failed")

    def test_every_scheme_is_built_when_there_is_no_package_scheme_and_none_is_a_failure(self):
        xcode = FakeXcode(schemes=["A", "B"])
        self.compile(xcode)
        self.assertEqual([argv[argv.index("-scheme") + 1] for argv, _ in xcode.calls if "build" in argv], ["A", "B"])
        (self.private / "result.json").unlink()
        self.compile(FakeXcode(schemes=[]))
        self.assertEqual(json.loads((self.private / "result.json").read_text())["result"], "failed")
        self.assertEqual(schemes_of("not json"), [])
        self.assertEqual(schemes_of(json.dumps({"workspace": {"schemes": ["-x", 3, "Ok"]}})), ["Ok"])

    def test_refuses_a_key_on_disk_a_secret_in_its_environment_or_another_xcode(self):
        (self.private / "ds.key").write_text("x")
        _, error, _, _ = self.compile(FakeXcode())
        self.assertEqual(error.args[0], "A read key or host file is still on disk; nothing was compiled")
        (self.private / "ds.key").unlink()
        for name in ("MAY_DS_READ_KEY", "MAY_DS_PR_COMMENT_TOKEN", "MAY_DS_PR_READ_TOKEN"):
            with self.subTest(secret=name):
                xcode = FakeXcode()
                _, error, _, _ = self.compile(xcode, env=self.environ(**{name: "x"}))
                self.assertIsInstance(error, StoreError)
                self.assertEqual(xcode.calls, [])
        _, error, _, _ = self.compile(FakeXcode(version=b"Xcode 26.4\n"))
        self.assertEqual(error.args[0], "Pinned Xcode version differs")
        self.assertFalse((self.private / "result.json").exists())


class PostTests(RunnerCase):
    LOG = "\n".join([
        f"{CK}/swift/Sources/A/View.swift:12:5: error: cannot find 'Foo' in scope",
        f"   12 |     let secret = \"{MARKER}\"",
        "      |     ^",
        f"{CK}/swift/Sources/A/View.swift:13:5: warning: {MARKER}",
        f"/Users/runner/work/_temp/may-ds-derived/{MARKER}.swift:1:1: error: {MARKER}",
        f"{CK}/swift/Sources/B/B.swift:2:1: error: expected '}}'",
        "** BUILD FAILED **",
    ])

    def setUp(self):
        super().setUp()
        self.checkout.mkdir()
        self.private.mkdir()
        self.pull = ds_pull()
        self.calls = []

    def write(self, result="failed", log=None):
        (self.private / "result.json").write_text(json.dumps({"result": result}))
        # The log names the checkout as the runner would.
        (self.private / "compile.log").write_text((log or self.LOG).replace(CK, str(self.checkout)))

    def call_api(self, method, path, payload, token, status=201):
        self.calls.append((method, path, payload, token))
        return (200, self.pull) if method == "GET" else (status, {})

    def post(self, env=None, call_api=None, head=SHA):
        with patch("ds_compile.head_revision", return_value=head):
            return self.captured(ds_compile.post, SHA, "40", call_api or self.call_api,
                                 env=env if env is not None else self.environ(MAY_DS_PR_COMMENT_TOKEN=TOKEN))

    def test_a_failure_posts_one_comment_of_redacted_errors_and_prints_only_the_count(self):
        self.write()
        code, error, out, err = self.post()
        self.assertIsNone(error)
        self.assertEqual(code, 1, "a failed compile fails the job after posting")
        self.assertEqual(out, "DS Swift compile failed; 2 errors posted to may-design-system #40\n")
        self.assertEqual(err, "")
        self.assertEqual([(m, p) for m, p, _, _ in self.calls],
                         [("GET", "/repos/May-Assistant/may-design-system/pulls/40"),
                          ("POST", "/repos/May-Assistant/may-design-system/issues/40/comments")])
        body = self.calls[1][2]["body"]
        self.assertEqual(set(self.calls[1][2]), {"body"})
        self.assertIn("swift/Sources/A/View.swift:12:5: error: cannot find 'Foo' in scope\n"
                      "swift/Sources/B/B.swift:2:1: error: expected '}'\n", body)
        self.assertNotIn(MARKER, body)
        self.assertNotIn(str(self.root), body, "paths are repo-relative")
        self.assertIn(SHA, body)
        self.assertIn("https://github.com/May-Assistant/may-releases/actions/runs/987", body)
        self.assertNotIn(TOKEN, body)
        self.assertFalse((self.root / "summary").exists() or (self.root / "output").exists())

    def test_a_pass_posts_that_it_compiles(self):
        self.write("passed", log=f"{CK}/A.swift:1:1: error: {MARKER}")
        code, error, out, _ = self.post()
        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(out, "DS Swift compile passed with Xcode 26.3; result posted to may-design-system #40\n")
        self.assertIn(f"`{SHA}` compiles with Xcode 26.3", self.calls[-1][2]["body"])
        self.assertNotIn(MARKER, self.calls[-1][2]["body"])

    def test_the_token_is_taken_out_of_the_environment_and_used_only_as_a_bearer(self):
        self.write()
        env = self.environ(MAY_DS_PR_COMMENT_TOKEN=TOKEN)
        with patch.dict(os.environ, env, clear=True), patch("ds_compile.head_revision", return_value=SHA), \
                contextlib.redirect_stdout(io.StringIO()):
            ds_compile.post(SHA, "40", self.call_api)
            self.assertNotIn("MAY_DS_PR_COMMENT_TOKEN", os.environ)
        self.assertEqual({t for _, _, _, t in self.calls}, {TOKEN})

    def test_nothing_is_posted_once_the_pull_request_moved_closed_or_is_unreadable(self):
        for name, pull in (("moved", ds_pull(head__sha=OTHER)), ("closed", ds_pull(state="closed")),
                           ("retargeted", ds_pull(base__ref="release"))):
            with self.subTest(case=name):
                self.write()
                self.calls, self.pull = [], pull
                code, error, out, _ = self.post()
                self.assertEqual(error.args[0], "The pull request is no longer open at the compiled head; nothing was posted")
                self.assertEqual([m for m, _, _, _ in self.calls], ["GET"])
                self.assertEqual(out, "")
        self.calls = []
        _, error, _, _ = self.post(call_api=lambda *a: (404, None))
        self.assertIn("could not be read again", error.args[0])

    def test_nothing_is_posted_without_an_outcome_a_token_the_compiled_head_or_with_another_secret(self):
        _, error, _, _ = self.post()
        self.assertEqual(error.args[0], "No compile outcome is recorded; nothing was posted")
        self.write()
        _, error, _, _ = self.post(env=self.environ())
        self.assertIn("comment token unavailable", error.args[0])
        _, error, _, _ = self.post(head=OTHER)
        self.assertIn("not the dispatched commit", error.args[0])
        _, error, _, _ = self.post(env=self.environ(MAY_DS_PR_COMMENT_TOKEN=TOKEN, MAY_DS_READ_KEY="k"))
        self.assertIsInstance(error, StoreError)
        self.assertEqual(self.calls, [])

    def test_a_refused_post_prints_a_fixed_line(self):
        self.write()
        _, error, out, _ = self.post(call_api=lambda m, p, b, t: self.call_api(m, p, b, t, status=403))
        self.assertEqual(error.args[0], "GitHub did not accept the comment; nothing from the compile is shown here")
        self.assertEqual(out, "")

    def test_a_body_holding_the_token_is_refused(self):
        # Token-shaped text is redacted; the token in another spelling (base64 here) refuses the post.
        self.write(log=f"{CK}/A.swift:1:1: error: {base64.b64encode(TOKEN.encode()).decode()}")
        _, error, _, _ = self.post()
        self.assertEqual(error.args[0], "The comment holds a credential; nothing was posted")
        self.assertEqual(self.calls, [])


class GitHubCallTests(unittest.TestCase):
    """ds_compile.github over a fake urlopen: only the pull request read and its comments endpoint, token as a header."""

    def test_only_the_pull_request_and_its_comments_are_reachable(self):
        seen = []

        def urlopen(request, timeout):
            seen.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
            return FakeResponse(b"{}", 201 if request.get_method() == "POST" else 200)

        with patch("urllib.request.urlopen", urlopen):
            self.assertEqual(ds_compile.github("GET", "/repos/May-Assistant/may-design-system/pulls/40", None, TOKEN)[0], 200)
            self.assertEqual(ds_compile.github("POST", "/repos/May-Assistant/may-design-system/issues/40/comments",
                                               {"body": "b"}, TOKEN)[0], 201)
            for method, path in (("POST", "/repos/May-Assistant/may-app/issues/40/comments"),
                                 ("PATCH", "/repos/May-Assistant/may-design-system/pulls/40"),
                                 ("PUT", "/repos/May-Assistant/may-design-system/pulls/40/merge"),
                                 ("GET", "/repos/May-Assistant/may-design-system/issues/40/comments"),
                                 ("POST", "/repos/May-Assistant/may-design-system/pulls/40"),
                                 ("POST", "/repos/May-Assistant/may-design-system/issues/40/comments/../../41/comments"),
                                 ("POST", "/repos/May-Assistant/may-design-system/issues/1/comments?x=1")):
                with self.subTest(method=method, path=path), self.assertRaises(DsCompileError):
                    ds_compile.github(method, path, {"body": "b"}, TOKEN)
        self.assertEqual([(m, u) for m, u, _, _ in seen],
                         [("GET", "https://api.github.com/repos/May-Assistant/may-design-system/pulls/40"),
                          ("POST", "https://api.github.com/repos/May-Assistant/may-design-system/issues/40/comments")])
        for _, _, headers, data in seen:
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN.encode(), data or b"")

    def test_an_http_error_is_a_status_never_its_body(self):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 422, MARKER, {}, io.BytesIO(MARKER.encode()))
        with patch("urllib.request.urlopen", urlopen):
            self.assertEqual(ds_compile.github("GET", "/repos/May-Assistant/may-design-system/pulls/40", None, TOKEN), (422, None))


class CommandLineTests(RunnerCase):
    def test_the_cli_prints_fixed_lines_and_exits_non_zero_on_a_failed_compile(self):
        self.checkout.mkdir()
        self.private.mkdir()
        (self.private / "result.json").write_text(json.dumps({"result": "failed"}))
        (self.private / "compile.log").write_text(f"{self.checkout}/A.swift:1:1: error: {MARKER}\n")
        pull = ds_pull()

        def urlopen(request, timeout):
            if request.get_method() == "GET":
                return FakeResponse(json.dumps(pull).encode())
            return FakeResponse(b"{}", 201)

        with patch.object(sys, "argv", ["ds_compile.py", "post", "--ds-sha", SHA, "--pr-number", "40"]), \
                patch("urllib.request.urlopen", urlopen), patch("ds_compile.head_revision", return_value=SHA):
            code, error, out, err = self.captured(ds_compile.main, env=self.environ(MAY_DS_PR_COMMENT_TOKEN=TOKEN))
        self.assertEqual((code, error), (1, None))
        self.assertEqual(out, "DS Swift compile failed; 1 errors posted to may-design-system #40\n")
        self.assertNotIn(MARKER, out + err)
        self.assertNotIn("A.swift", out + err)

    def test_the_cli_refuses_misplaced_arguments(self):
        for argv in (["compile", "--ds-sha", SHA], ["post"], ["fetch", "--pr-number", "40"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["ds_compile.py"] + argv):
                _, error, _, _ = self.captured(ds_compile.main)
                self.assertIsNotNone(error)

    def test_the_new_tokens_are_secrets_no_other_step_may_hold(self):
        self.assertIn("MAY_DS_PR_READ_TOKEN", SECRET_NAMES)
        self.assertIn("MAY_DS_PR_COMMENT_TOKEN", SECRET_NAMES)


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
        self.assertEqual(static_check.ds_compile_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.ds_compile_print_problems(static_check.ROOT), [])

    def test_each_widening_of_the_workflow_is_flagged(self):
        name = "ds-swift-compile.yml: "
        secret = name + "a secret or env entry other than"
        runs = name + "runs something other than"
        build = "      - name: Compile the Swift package without signing or any credential\n"
        fetch_key = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        comment = "          MAY_DS_PR_COMMENT_TOKEN: ${{ secrets.MAY_DS_PR_COMMENT_TOKEN }}\n"
        cases = {
            "the read key on the compile": (build, build + "        env:\n          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n", secret),
            "any env on the compile": (build, build + "        env:\n          MAY_DS_SHA: ${{ inputs.ds_sha }}\n", secret),
            "the comment token in the fetch": (fetch_key, fetch_key + comment, secret),
            "the read key in the post": (comment, comment + fetch_key, secret),
            "the read token in the post": (comment, comment + "          MAY_DS_PR_READ_TOKEN: ${{ secrets.MAY_DS_PR_READ_TOKEN }}\n", secret),
            "a job-level env": ("    timeout-minutes: 60\n", "    timeout-minutes: 60\n    env:\n      CI: '1'\n", secret),
            "printing the log": (build, "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-ds-private/compile.log\"\n" + build, runs),
            "a step summary": (build, "      - name: Summary\n        run: echo x >> \"$GITHUB_STEP_SUMMARY\"\n" + build, name + "publishes an artifact or summary"),
            "an artifact": (build, "      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n" + build,
                            name + "publishes an artifact or summary"),
            "an input in shell": ('--pr-number "$MAY_DS_PR"\n  native:', "--pr-number '${{ inputs.ds_pr }}'\n  native:", runs),
            "no recheck": ('"$MAY_DS_PR" --check-pull\n', '"$MAY_DS_PR"\n', runs),
            "a fixed environment": ("    environment: ${{ needs.admission.outputs.environment }}\n", "    environment: may-ds-renders\n",
                                    name + "native's environment is not the one admission chose"),
            "a push trigger": ("on:\n  workflow_dispatch:\n", "on:\n  push:\n  workflow_dispatch:\n", name + "is not dispatched with exactly"),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  pull-requests: write\nconcurrency:",
                                 name + "permissions are not exactly read-only"),
            "cleanup not always": ("        if: always()\n", "", name + "native's steps are not exactly"),
            "continue on error": (build, build + "        continue-on-error: true\n", name + "sets a shell, defaults, a condition"),
            "a longer timeout": ("    timeout-minutes: 60\n", "    timeout-minutes: 360\n", name + "sets a shell"),
            "a shared concurrency group": ("  group: may-ds-swift-compile\n", "  group: may-native-compile-pr\n", name + "does not run one at a time"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message)

    def test_printing_anything_from_the_design_system_is_flagged(self):
        tool = "tools/may_release/ds_compile.py"
        message = "ds_compile.py:"
        anchor = '    print("Exact may-design-system commit fetched quietly; the read key and host file are removed")\n'
        cases = {
            "a path": f'    print(str(checkout))\n{anchor}',
            "an f-string of a path": f'    print(f"fetched {{checkout}}")\n{anchor}',
            "the count with a path": f'    print(f"{{number}} {{checkout}}")\n{anchor}',
            "sys.stdout": f'    sys.stdout.write("x")\n{anchor}',
            "sys.stderr": f'    sys.stderr.write("x")\n{anchor}',
            "the step summary": f'    open(os.environ["GITHUB_STEP_SUMMARY"], "a")\n{anchor}',
            "a workflow command": f'    print("::notice::x")\n{anchor}',
            "os.write": f'    os.write(1, b"x")\n{anchor}',
            "print to a file": f'    print("x", file=None)\n{anchor}',
        }
        for case, new in cases.items():
            with self.subTest(case=case):
                self.flagged(tool, anchor, new, message)
        self.flagged(tool, "import argparse\n", "import argparse\nimport logging\n", "imports a module that writes to stderr")

    def test_its_errors_are_fixed_literals(self):
        self.flagged("tools/may_release/ds_compile.py",
                     'raise DsCompileError("Pinned Xcode version differs")',
                     'raise DsCompileError("Pinned Xcode version differs: " + str(version.stdout))',
                     "printed error message is not one string literal")

    def test_no_other_action_joins_the_no_reviewer_policy(self):
        self.flagged("tools/may_release/gate.py", 'NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile"}',
                     'NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile", "ios"}', "NO_REVIEWER_ACTIONS is not exactly")


if __name__ == "__main__":
    unittest.main()
