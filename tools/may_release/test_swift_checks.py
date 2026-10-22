"""may-app's Linux Swift checks before the iOS compile of a may-app pull request (native-compile-pr.yml's swift-checks
job, swift_checks.py). Real unittest runs on fake check files; GitHub is faked."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import static_check
import store
import swift_checks as tool
from test_compile_pr import pull

SHA = "a" * 40
TOKEN = "ghp_" + "t" * 36
PULLS = "/repos/May-Assistant/may-app/pulls/42"
COMMENTS = "/repos/May-Assistant/may-app/issues/42/comments"
WORKFLOW = ".github/workflows/native-compile-pr.yml"
FAILING = '''import unittest
class Inits(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(["App/Settings/About.swift:12: MaySettingsItem(_, info:, status:) matches none of its inits"], [])
    def test_fine(self):
        pass
    def test_import(self):
        raise ImportError("no module at /home/runner/work/_temp/may-source/ios/App/X.swift")
'''
PASSING = "import unittest\nclass Inits(unittest.TestCase):\n    def test_fine(self):\n        pass\n"


class RunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "android/build-deps/may-design-system").mkdir(parents=True)
        self.private.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def commit(self, checks=None):
        """A one-commit may-source with tools/ios's checks as given ({name: text}), or none; its SHA."""
        shutil.rmtree(self.source / ".git", ignore_errors=True)
        shutil.rmtree(self.source / "tools", ignore_errors=True)
        for name, text in (checks or {}).items():
            path = self.source / "tools/ios" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.source / "README").write_text("x\n")
        env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "x"]):
            subprocess.run(argv, cwd=self.source, env=env, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.source, env=env, capture_output=True,
                              check=True).stdout.decode().strip()

    def run_it(self, sha, extra=None):
        environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), **(extra or {})}
        out = io.StringIO()
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", out):
            tool.run_checks(sha)
        outcome = json.loads((self.private / tool.OUTCOME).read_text())
        failures = json.loads((self.private / tool.FAILURES).read_text())
        return out.getvalue(), outcome, failures

    def test_failing_checks_are_recorded_privately_and_counted_publicly(self):
        out, outcome, failures = self.run_it(self.commit({"test_swift_inits.py": FAILING}))
        self.assertEqual(out, "May Swift checks failed: 2 failing checks, which go to the may-app pull request only\n")
        self.assertEqual(outcome, {"result": "failed", "problems": 2})
        by_test = {entry["test"]: entry for entry in failures["failures"]}
        self.assertEqual(set(by_test), {"test_swift_inits.Inits.test_labels", "test_swift_inits.Inits.test_import"})
        self.assertEqual(by_test["test_swift_inits.Inits.test_labels"]["kind"], "fail")
        self.assertTrue(any("About.swift:12: MaySettingsItem(_, info:, status:)" in line
                            for line in by_test["test_swift_inits.Inits.test_labels"]["lines"]))
        self.assertEqual(by_test["test_swift_inits.Inits.test_import"]["lines"], ["ImportError: no module at [runner path]"])
        for entry in failures["failures"]:
            self.assertFalse(any(line.startswith(("Ran ", "FAILED")) for line in entry["lines"]), "the block ends")
        self.assertNotIn("About.swift", out)

    def test_passing_checks_and_commits_without_any(self):
        out, outcome, _ = self.run_it(self.commit({"test_swift_inits.py": PASSING}))
        self.assertEqual((out, outcome), ("May Swift checks passed\n", {"result": "passed", "problems": 0}))
        for checks in (None, {"test_other.py": PASSING}, {"test_swift_empty.py": "import unittest\n"}):
            with self.subTest(checks=sorted(checks or {})):
                out, outcome, failures = self.run_it(self.commit(checks))
                self.assertEqual(outcome, {"result": "skipped", "problems": 0}, "no check is not a failure")
                self.assertIn("skipped", out)
                self.assertEqual(failures, {"failures": [], "total": 0})

    def test_the_checks_run_from_tools_ios_with_no_secret(self):
        seen = {}
        real = subprocess.run

        def spy(argv, **kwargs):
            if "unittest" in argv:
                seen.update(argv=argv, cwd=kwargs.get("cwd"), env=kwargs.get("env"))
            return real(argv, **kwargs)

        sha = self.commit({"test_swift_inits.py": PASSING})
        with patch("swift_checks.subprocess.run", side_effect=spy):
            self.run_it(sha, extra={"MAY_APP_PR_TOKEN_UNRELATED": "x"})
        self.assertIn("argv", seen, "the checks did not run")
        self.assertEqual(seen["argv"][1:], ["-I", "-m", "unittest", "discover", "-s", "tools/ios", "-p", "test_swift_*.py"])
        self.assertEqual(Path(seen["cwd"]), self.source)
        self.assertNotIn("MAY_APP_PR_TOKEN_UNRELATED", seen["env"])
        with self.assertRaises(store.StoreError):
            self.run_it(sha, extra={"MAY_APP_PR_COMMENT_TOKEN": TOKEN})

    def test_a_check_that_does_not_finish_is_a_timeout(self):
        sha = self.commit({"test_swift_inits.py": PASSING})
        real = subprocess.run

        def slow(argv, **kwargs):
            if "unittest" in argv:
                raise subprocess.TimeoutExpired("python3", 1)
            return real(argv, **kwargs)

        with patch("swift_checks.subprocess.run", side_effect=slow):
            out, outcome, _ = self.run_it(sha)
        self.assertEqual((out, outcome), ("May Swift checks did not finish within 10 minutes\n",
                                          {"result": "timeout", "problems": 0}))


def recorded(result="failed", entries=None, total=None):
    entries = [{"kind": "fail", "test": "test_swift_inits.Inits.test_labels",
                "lines": ["AssertionError: About.swift:12: MaySettingsItem(_, info:, status:)"]}] if entries is None else entries
    return {"result": result, "problems": len(entries) if total is None else total}, \
        {"failures": entries, "total": len(entries) if total is None else total}


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
        (self.root / "may-private" / tool.OUTCOME).write_text(json.dumps(outcome))
        (self.root / "may-private" / tool.FAILURES).write_text(json.dumps(failed))

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

    def post(self, call, token=TOKEN):
        environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), "GITHUB_RUN_ID": "9"}
        if token is not None:
            environ["MAY_APP_PR_COMMENT_TOKEN"] = token
        out = io.StringIO()
        with patch.dict(os.environ, environ, clear=True), patch("sys.stdout", out), \
                patch.object(tool, "head_revision", lambda checkout: SHA):
            code = tool.post(SHA, "42", call_api=call)
        return code, out.getvalue()

    def posted(self):
        return [payload["body"] for method, path, payload in self.calls if method == "POST"]

    def test_failures_are_posted_once_and_the_job_fails_so_the_compile_never_starts(self):
        code, out = self.post(self.api())
        self.assertEqual((code, out), (1, "May Swift checks failed; 1 failing checks posted to may-app #42\n"))
        [body] = self.posted()
        self.assertIn("MaySettingsItem(_, info:, status:)", body)
        self.assertIn("The iOS compile did not run.", body)
        self.assertIn(f"<!-- {tool.MARKER} head={SHA} -->", body)
        self.assertNotIn("MaySettingsItem", out)

    def test_a_pass_or_a_skip_posts_nothing_and_lets_the_compile_run(self):
        for result in ("passed", "skipped"):
            with self.subTest(result=result):
                self.calls = []
                self.write(*recorded(result, []))
                self.assertEqual(self.post(self.api()), (0, "May Swift checks: nothing to post; the compile may run\n"))
                self.assertEqual(self.calls, [])

    def test_a_timeout_is_posted_and_fails(self):
        self.write(*recorded("timeout", []))
        code, _ = self.post(self.api())
        self.assertEqual(code, 1)
        self.assertIn("did not finish within 10 minutes", self.posted()[0])

    def test_without_the_token_nothing_is_posted_and_the_job_still_fails(self):
        code, out = self.post(self.api(), token=None)
        self.assertEqual(code, 1)
        self.assertIn("MAY_APP_PR_COMMENT_TOKEN is not set", out)
        self.assertEqual(self.calls, [])

    def test_a_moved_head_or_an_earlier_post_posts_nothing(self):
        with self.assertRaises(tool.SwiftChecksError):
            self.post(self.api(pull(head__sha="b" * 40)))
        self.assertEqual(self.posted(), [])
        self.calls = []
        earlier = [{"body": f"x <!-- {tool.MARKER} head={SHA} -->", "user": {"login": "x"}}]
        code, out = self.post(self.api(comments=earlier))
        self.assertEqual(code, 1)
        self.assertIn("already posted", out)
        self.assertEqual(self.posted(), [])

    def test_a_malformed_record_is_refused(self):
        for outcome, failed in (({"result": "maybe", "problems": 0}, {"failures": [], "total": 0}),
                                ({"result": "failed", "problems": 1},
                                 {"failures": [{"kind": "fail", "test": "x", "lines": ["a\nb"]}], "total": 1})):
            with self.subTest(outcome=outcome["result"]):
                self.write(outcome, failed)
                with self.assertRaises(tool.SwiftChecksError):
                    self.post(self.api())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def flagged(self, relative, old, new, message):
        self.tearDown()
        self.setUp()
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, "mutation anchor not found")
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        self.assertTrue(any(message in problem for problem in found), (old, new, found))

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.compile_pr_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.swift_checks_print_problems(static_check.ROOT), [])

    def test_the_compile_waits_for_the_checks_and_each_widening_is_flagged(self):
        name = "native-compile-pr.yml: "
        secret = name + "a secret or env entry other than"
        checks = "      - name: Run the Swift checks without any credential, printing only pass or fail and a count\n" \
                 "        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n"
        swift_post = "      - name: Post the failing Swift checks on the may-app pull request, never in this log\n"
        cases = {
            "the compile not waiting for the checks": ("    needs: [admission, swift-checks]\n", "    needs: admission\n",
                                                       name + "native's environment is not the one admission chose"),
            "a read key on the checks": (checks, checks + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the comment token on the checks": (checks, checks + "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n", secret),
            "another env entry on the checks": (checks, checks + "          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n", secret),
            "a longer timeout": ("    timeout-minutes: 20\n", "    timeout-minutes: 60\n", name + "sets a shell, defaults, a condition"),
            "a post that always runs": (swift_post, swift_post + "        if: always()\n", name + "sets a shell, defaults, a condition"),
            "printing the private log": (swift_post, "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/swift-checks.log\"\n" + swift_post,
                                         name + "runs something other than"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message)
        workflow = (static_check.ROOT / WORKFLOW).read_text()
        swift = workflow[workflow.index("  swift-checks:\n"):]
        self.flagged(WORKFLOW, swift, swift.replace("    runs-on: ubuntu-24.04\n", "    runs-on: macos-15\n", 1),
                     name + "is not the iOS-only May Playground compile with no artifact")
        self.flagged(WORKFLOW, swift, "", name + "jobs are not exactly admission, native and swift-checks")

    def test_printing_anything_from_may_app_is_flagged(self):
        anchor = '        print("May Swift checks passed")\n'
        for mutation in ('        print(text)\n', '        print(entries[0])\n', '        sys.stdout.write(text)\n',
                         '        raise SystemExit(text)\n'):
            with self.subTest(mutation=mutation.strip()):
                self.flagged("tools/may_release/swift_checks.py", anchor, mutation + anchor, "swift_checks.py:")


if __name__ == "__main__":
    unittest.main()
