"""Compile of a may-app pull request head (native-compile-pr.yml, gate.py --action compile-pr, prepare.py --pr-number)."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import compile as compile_tool
import gate
from gate import GateDenial, validate_pull_request, validate_run
import prepare
from prepare import PrepareError, fetch
import static_check

REPO, MAIN, DISPATCH = "May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch"
SHA, OTHER = "a" * 40, "b" * 40
PR_WORKFLOW = ".github/workflows/native-compile-pr.yml"
# may-native-compile-pr as Samuel decided (17 Oct): no required reviewer, admin bypass off, main only.
ENVIRONMENT = {
    "can_admins_bypass": False,
    "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False},
    "protection_rules": [],
}
REVIEWER_RULE = {"type": "required_reviewers", "prevent_self_review": False, "reviewers": [
    {"type": "User", "reviewer": {"id": 58840187, "login": "samuelalake"}}]}
POLICIES = {"branch_policies": [{"name": "main"}]}


def pull(**changes):
    """An open may-app pull request against main from may-app itself, head SHA, number 42."""
    value = {"number": 42, "state": "open",
             "base": {"ref": "main", "repo": {"full_name": "May-Assistant/may-app"}},
             "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": "May-Assistant/may-app"}}}
    for path, new in changes.items():
        node = value
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = new
    return value


class PullRequestAdmissionTests(unittest.TestCase):
    def test_compile_pr_has_its_own_environment(self):
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, SHA, "compile-pr"), "may-native-compile-pr")
        self.assertNotIn("may-native-compile-pr", {v for k, v in gate.ENVIRONMENTS.items() if k != "compile-pr"})
        for repo, ref, event in (("May-Assistant/may-app", MAIN, DISPATCH), (REPO, "refs/heads/x", DISPATCH), (REPO, MAIN, "push")):
            with self.subTest(repo=repo, ref=ref, event=event), self.assertRaises(GateDenial):
                validate_run(repo, ref, event, SHA, "compile-pr")

    def test_accepts_the_current_head_of_an_open_pull_request_against_main(self):
        validate_pull_request(pull(), 42, SHA)
        validate_pull_request(pull(draft=True), 42, SHA)

    def test_refuses_everything_else(self):
        cases = {
            "a commit that is not the head": (pull(), OTHER, "Source SHA is not the pull request's current head"),
            "a closed pull request": (pull(state="closed"), SHA, "Pull request is not open"),
            "a merged pull request": (pull(state="closed", merged=True), SHA, "Pull request is not open"),
            "a fork": (pull(head__repo={"full_name": "someone/may-app"}), SHA, "forks are refused"),
            "a deleted fork": (pull(head__repo=None), SHA, "forks are refused"),
            "another base branch": (pull(base__ref="release"), SHA, "not against may-app main"),
            "another base repository": (pull(base__repo={"full_name": "someone/may-app"}), SHA, "not against may-app main"),
            "another pull request's answer": (pull(number=43), SHA, "unreadable"),
            "no answer": (None, SHA, "unreadable"),
        }
        for name, (value, sha, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(GateDenial) as caught:
                    validate_pull_request(value, 42, sha)
                self.assertIn(message, caught.exception.args[0], name)

    def test_pull_request_number_is_exact(self):
        self.assertEqual(gate.pull_number("42"), 42)
        for value in ("0", "042", "-1", "4 2", "1e3", "12345678", "", None):
            with self.subTest(value=value), self.assertRaises(GateDenial):
                gate.pull_number(value)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AdmissionCommandTests(unittest.TestCase):
    """gate.py's CLI for compile-pr, with GitHub's API answered by a fake."""

    def run_gate(self, args, answers, env=None):
        seen = []

        def urlopen(request, timeout):
            seen.append((request.full_url, request.headers.get("Authorization")))
            answer = answers.get(request.full_url.split("api.github.com/repos/", 1)[1])
            if answer is None:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return FakeResponse(json.dumps(answer).encode())

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            environ = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                       "GH_TOKEN": "control-token", "MAY_APP_PR_READ_TOKEN": "pr-token", "GITHUB_OUTPUT": str(output)}
            environ.update(env or {})
            environ = {k: v for k, v in environ.items() if v is not None}
            with patch.dict(os.environ, environ, clear=True), patch("urllib.request.urlopen", urlopen), \
                    patch.object(sys, "argv", ["gate.py"] + args):
                try:
                    gate.main()
                    denial = None
                except GateDenial as error:
                    denial = error.args[0]
            written = output.read_text() if output.exists() else ""
        return denial, seen, written

    def answers(self, pull_value, environment=ENVIRONMENT, policies=POLICIES):
        return {"May-Assistant/may-app/pulls/42": pull_value,
                "May-Assistant/may-releases/environments/may-native-compile-pr": environment,
                "May-Assistant/may-releases/environments/may-native-compile-pr/deployment-branch-policies?per_page=100": policies}

    ADMIT = ["--action", "compile-pr", "--source-sha", SHA, "--pr-number", "42"]
    CHECK = ADMIT + ["--check-pull"]

    def test_admission_checks_inputs_and_its_own_environment_with_no_pull_request_token(self):
        # Admission has no environment, so it never holds the PR token (security review of #18, M2).
        denial, seen, written = self.run_gate(self.ADMIT, self.answers(pull()), env={"MAY_APP_PR_READ_TOKEN": None})
        self.assertIsNone(denial)
        self.assertEqual(written, "environment=may-native-compile-pr\nbuild_environment=may-native-compile-pr\n")
        self.assertFalse([u for u, _ in seen if "/may-app/" in u], "admission reads no pull request")
        environments = {url.split("/environments/", 1)[1].split("/", 1)[0] for url, _ in seen if "/environments/" in url}
        self.assertEqual(environments, {"may-native-compile-pr"}, "never may-native-compile or a store environment")

    def test_the_pull_request_check_admits_the_head_and_names_who_wrote_and_dispatched_it(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            denial, seen, _ = self.run_gate(self.CHECK, self.answers(pull(user={"login": "dev-one"})),
                                            env={"GITHUB_TRIGGERING_ACTOR": "coordinator[bot]"})
        self.assertIsNone(denial)
        self.assertIn(("https://api.github.com/repos/May-Assistant/may-app/pulls/42", "Bearer pr-token"), seen)
        for url, auth in seen:
            self.assertEqual(auth, "Bearer pr-token" if "/may-app/" in url else "Bearer control-token", url)
        self.assertEqual(out.getvalue().strip(), "May compile-pr pull request check passed for " + SHA +
                         " (may-app #42; pull request by dev-one; dispatched by coordinator[bot])")
        # Anything but a login is never printed.
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.run_gate(self.CHECK, self.answers(pull(user={"login": "x\n::error::"})), env={"GITHUB_TRIGGERING_ACTOR": "a b"})
        self.assertIn("pull request by unknown; dispatched by unknown", out.getvalue())

    def test_the_pull_request_check_refuses_a_non_head_a_closed_pull_or_a_fork_and_writes_no_environment(self):
        for name, value, sha in (("not the head", pull(), OTHER), ("closed", pull(state="closed"), SHA),
                                 ("fork", pull(head__repo={"full_name": "someone/may-app"}), SHA)):
            with self.subTest(case=name):
                denial, seen, written = self.run_gate(["--action", "compile-pr", "--source-sha", sha, "--pr-number", "42",
                                                       "--check-pull"], self.answers(value))
                self.assertIsNotNone(denial)
                self.assertEqual(written, "")
                self.assertFalse([u for u, _ in seen if "/environments/" in u], "denied before any environment read")

    def test_a_missing_pull_request_or_token_denies_the_check(self):
        denial, _, written = self.run_gate(self.CHECK, {k: v for k, v in self.answers(pull()).items() if "/pulls/" not in k})
        self.assertIn("does not exist", denial)
        self.assertEqual(written, "")
        denial, seen, _ = self.run_gate(self.CHECK, self.answers(pull()), env={"MAY_APP_PR_READ_TOKEN": None})
        self.assertEqual(denial, "Pull-request read token unavailable")
        self.assertEqual(seen, [], "nothing is asked without the token")

    def test_the_environment_has_no_reviewer_no_admin_bypass_and_main_only(self):
        # Samuel, 17 Oct: compile-only runs need no approval. A reviewer rule would only hang the run (review of #18, H1).
        with_reviewer = dict(ENVIRONMENT, protection_rules=[REVIEWER_RULE])
        bypass = dict(ENVIRONMENT, can_admins_bypass=True)
        protected = dict(ENVIRONMENT, deployment_branch_policy={"custom_branch_policies": False, "protected_branches": True})
        cases = {
            "a required reviewer": (with_reviewer, POLICIES, "must have no required reviewer"),
            "administrator bypass": (bypass, POLICIES, "Administrator bypass must be disabled"),
            "protected branches instead of a policy": (protected, POLICIES, "explicit branch policy"),
            "another branch allowed": (ENVIRONMENT, {"branch_policies": [{"name": "main"}, {"name": "dev"}]}, "allow only main"),
        }
        for name, (environment, policies, message) in cases.items():
            for args in (self.ADMIT, self.CHECK):
                with self.subTest(case=name, check="--check-pull" in args):
                    denial, _, written = self.run_gate(args, self.answers(pull(), environment, policies))
                    self.assertIsNotNone(denial)
                    self.assertIn(message, denial)
                    self.assertEqual(written, "")

    def test_only_compile_pr_runs_without_a_reviewer(self):
        # And the design-system Swift compile (ds-compile), which compiles and comments only (test_ds_compile.py).
        self.assertEqual(gate.NO_REVIEWER_ACTIONS, {"compile-pr", "ds-compile"})
        # The existing compile still requires Samuel as the sole reviewer.
        with self.assertRaises(GateDenial):
            gate.validate_environment(ENVIRONMENT, POLICIES)

    def test_the_pull_request_number_comes_with_compile_pr_and_only_then(self):
        for args, message in ((["--action", "compile-pr", "--source-sha", SHA], "given with compile-pr or ds-compile and only then"),
                              (["--action", "compile", "--source-sha", SHA, "--pr-number", "42"], "given with compile-pr or ds-compile and only then"),
                              (["--action", "compile", "--source-sha", SHA, "--check-pull"], "Only compile-pr and ds-compile read a pull request")):
            with self.subTest(args=args):
                denial, seen, _ = self.run_gate(args, self.answers(pull()))
                self.assertIn(message, denial)
                self.assertEqual(seen, [])


class PullRequestFetchTests(unittest.TestCase):
    def fetch_pull(self, head):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            calls = []
            with patch("prepare.quiet", side_effect=lambda argv, log, env: calls.append(argv)), \
                    patch("prepare.git_status", return_value=0), patch("prepare.head_of", return_value=head):
                try:
                    fetch(Path(directory) / "may-source", "git@example.invalid:x.git", SHA, "fixture read key",
                          private, "app", pull="42")
                    error = None
                except PrepareError as raised:
                    error = raised.args[0]
            left = sorted(p.name for p in private.iterdir())
        return error, calls, left

    def test_fetches_the_pull_request_head_ref_and_checks_it_is_the_sha(self):
        error, calls, left = self.fetch_pull(SHA)
        self.assertIsNone(error)
        fetches = [argv for argv in calls if "fetch" in argv]
        self.assertEqual(fetches[0][-1], "+refs/pull/42/head:refs/remotes/origin/pull-head")
        self.assertTrue(any("checkout" in argv and SHA in argv for argv in calls))
        self.assertEqual([n for n in left if n.endswith(".key") or n == "known_hosts"], [], "the key is removed")

    def test_refuses_when_the_pull_request_head_moved(self):
        error, calls, left = self.fetch_pull(OTHER)
        self.assertEqual(error, "May app source SHA is not the pull request's head")
        self.assertFalse(any("checkout" in argv for argv in calls), "nothing is checked out")
        self.assertEqual([n for n in left if n.endswith(".key") or n == "known_hosts"], [])

    def test_a_pull_request_number_is_exact_and_excludes_main_history(self):
        for value in ("0", "x", "1/2", "../main"):
            with self.subTest(value=value), self.assertRaises(PrepareError):
                prepare.pull_ref(value)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(PrepareError):
            fetch(Path(directory) / "s", "git@example.invalid:x.git", SHA, "k", Path(directory), "app",
                  main_history=True, pull="42")


class CompileCredentialTests(unittest.TestCase):
    def test_the_compile_refuses_while_a_key_or_host_file_is_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "may-source").mkdir()
            private = root / "may-private"
            private.mkdir()
            self.assertFalse(compile_tool.credentials_left(private))
            for name in ("app.key", "known_hosts"):
                with self.subTest(file=name):
                    (private / name).write_text("x")
                    self.assertTrue(compile_tool.credentials_left(private))
                    with patch.dict(os.environ, {"RUNNER_TEMP": directory}, clear=True), \
                            self.assertRaises(compile_tool.CompileError) as caught:
                        compile_tool.compile_native("ios", "playground")
                    self.assertEqual(caught.exception.args[0], "A read key or host file is still on disk; nothing was compiled")
                    (private / name).unlink()

    def test_the_pr_token_is_a_secret_no_tool_step_may_hold(self):
        from store import SECRET_NAMES, StoreError, require_step_secrets
        self.assertIn("MAY_APP_PR_READ_TOKEN", SECRET_NAMES)
        with patch.dict(os.environ, {"MAY_APP_PR_READ_TOKEN": "t"}), self.assertRaises(StoreError):
            require_step_secrets(("MAY_APP_READ_KEY", "MAY_DS_READ_KEY"))


class PullRequestWorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def test_the_committed_workflow_passes(self):
        self.assertEqual(static_check.compile_pr_workflow_problems((static_check.ROOT / PR_WORKFLOW).read_text()), [])

    def test_each_widening_is_flagged(self):
        name = "native-compile-pr.yml: "
        secret = name + "a secret or env entry other than the pull request check's PR token and the fetch step's two source-read keys"
        runs = name + "runs something other than the gate, the fetch, the compile and the cleanup"
        compile_step = "      - name: Compile without signing, store access or any credential\n"
        fetch_env = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        cases = {
            "a source key in the compile step": (compile_step, compile_step +
                "        env:\n          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the PR token in the fetch step": (fetch_env, fetch_env +
                "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "any env on the compile step": (compile_step, compile_step + "        env:\n          CI: '1'\n", secret),
            # Only the later-steps rule catches this: the entry is one the gates may have, the compile none.
            "an allowed entry on the compile step": (compile_step, compile_step +
                "        env:\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n", secret),
            "a store secret anywhere": (fetch_env, fetch_env + "          MAY_ASC_KEY_BASE64: ${{ secrets.MAY_ASC_KEY_BASE64 }}\n", secret),
            "a job-level env": ("    timeout-minutes: 90\n", "    timeout-minutes: 90\n    env:\n      CI: '1'\n", secret),
            "the main-history fetch instead": ('--source-sha "$MAY_SOURCE_SHA" --pr-number "$MAY_PR_NUMBER"\n      - name: Compile',
                '--source-sha "$MAY_SOURCE_SHA"\n      - name: Compile', runs),
            "an input interpolated into shell": ('--pr-number "$MAY_PR_NUMBER"\n  native:', "--pr-number '${{ inputs.pr_number }}'\n  native:", runs),
            "a store environment": ("    environment: ${{ needs.admission.outputs.environment }}\n",
                "    environment: may-ios-playground-internal\n", name + "native's environment is not the one admission chose"),
            "an artifact": (compile_step, "      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n" + compile_step,
                name + "is not the iOS-only May Playground compile with no artifact"),
            "a push trigger": ("on:\n  workflow_dispatch:\n", "on:\n  push:\n  workflow_dispatch:\n",
                name + "is not dispatched with exactly pr_number and source_sha"),
            "the PR token in admission": ('          GH_TOKEN: ${{ github.token }}\n          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n          MAY_PR_NUMBER',
                '          GH_TOKEN: ${{ github.token }}\n          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n'
                '          MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n          MAY_PR_NUMBER', secret),
            "the check without --check-pull": ('--pr-number "$MAY_PR_NUMBER" --check-pull\n', '--pr-number "$MAY_PR_NUMBER"\n', runs),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: write\nconcurrency:",
                name + "permissions are not exactly read-only"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                path = self.root / PR_WORKFLOW
                text = path.read_text()
                self.assertIn(old, text, "mutation anchor not found")
                path.write_text(text.replace(old, new, 1))
                found = static_check.problems(self.root)
                self.assertTrue(any(message in problem for problem in found), (case, found))


    def test_no_workflow_may_use_an_actions_cache(self):
        # Security review of #18, M1: the runtime token a pull request's code can read writes cache; nothing reads one.
        for workflow, old in (("control-check.yml", "      - uses: actions/checkout@"),
                              ("native-compile-pr.yml", "      - uses: actions/checkout@")):
            for new in ("      - uses: actions/cache@0123456789012345678901234567890123456789\n" + old,
                        "      - uses: actions/setup-python@0123456789012345678901234567890123456789\n        with:\n          cache: pip\n" + old,
                        # Re-review of #18, L5: a flow-style input, and a third-party cache action with no cache: input.
                        "      - uses: actions/setup-python@0123456789012345678901234567890123456789\n        with: {cache: pip}\n" + old,
                        "      - uses: irgaly/xcode-cache@0123456789012345678901234567890123456789\n        with:\n          key: x\n" + old):
                with self.subTest(workflow=workflow, new=new.split("\n")[0]):
                    self.tearDown()
                    self.setUp()
                    path = self.root / ".github/workflows" / workflow
                    text = path.read_text()
                    self.assertIn(old, text)
                    path.write_text(text.replace(old, new, 1))
                    self.assertTrue(any(workflow + ": uses an Actions cache" in p for p in static_check.problems(self.root)))

    def test_no_other_action_may_join_the_no_reviewer_policy(self):
        path = self.root / "tools/may_release/gate.py"
        text = path.read_text()
        self.assertIn('NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile"}', text)
        path.write_text(text.replace('NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile"}',
                                     'NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile", "ios"}', 1))
        self.assertTrue(any("NO_REVIEWER_ACTIONS is not exactly" in p for p in static_check.problems(self.root)))


if __name__ == "__main__":
    unittest.main()
