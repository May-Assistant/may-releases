"""Claude code review of a may-app pull request (may-app-review.yml, review.py, review_mcp.py)."""

import base64
import hashlib
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

import gate
from gate import GateDenial
import review
from review import ReviewError
import review_mcp
import static_check

HERE = Path(__file__).resolve().parent
SHA, OTHER = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "Z" * 40
MARKER = "PRIVATE-MAY-APP-CONTENT-7f3c"
DISPATCH = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_TRIGGERING_ACTOR": "samuelalake", "GITHUB_ACTOR_ID": "58840187"}
REVIEWER_RULE = {"type": "required_reviewers", "prevent_self_review": False, "reviewers": [
    {"type": "User", "reviewer": {"id": 58840187, "login": "samuelalake"}}]}
ENVIRONMENT = {"can_admins_bypass": False, "protection_rules": [REVIEWER_RULE],
               "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}
POLICIES = {"branch_policies": [{"name": "main"}]}


def pull(**changes):
    value = {"number": 42, "state": "open", "draft": False, "user": {"login": "samuelalake"},
             "base": {"ref": "main", "repo": {"full_name": "May-Assistant/may-app"}},
             "head": {"sha": SHA, "ref": "feature", "repo": {"full_name": "May-Assistant/may-app"}}}
    value.update(changes)
    return value


def environment_answers(environment=ENVIRONMENT, policies=POLICIES):
    def answer(path, token):
        return policies if path.endswith("deployment-branch-policies?per_page=100") else environment
    return answer


class AdmissionTests(unittest.TestCase):
    def test_an_open_ready_pull_request_against_main_from_may_app_is_reviewed_at_its_head(self):
        self.assertEqual(review.review_pull(pull(), 42), SHA)

    def test_everything_else_is_refused(self):
        cases = {
            "a draft": (pull(draft=True), "draft"),
            "a draft flag that is missing": ({k: v for k, v in pull().items() if k != "draft"}, "draft"),
            "a closed pull request": (pull(state="closed"), "not open"),
            "a fork": (pull(head={"sha": SHA, "repo": {"full_name": "someone/may-app"}}), "forks are refused"),
            "another base branch": (pull(base={"ref": "release", "repo": {"full_name": "May-Assistant/may-app"}}), "not against may-app main"),
            "a head that is not a SHA": (pull(head={"sha": "main", "repo": {"full_name": "May-Assistant/may-app"}}), "unreadable"),
            "another pull request's answer": (pull(number=43), "unreadable"),
            "no answer": (None, "unreadable"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(GateDenial) as caught:
                    review.review_pull(value, 42)
                self.assertIn(message, caught.exception.args[0])

    def test_admission_needs_main_dispatch_and_the_environment_policy(self):
        with tempfile.NamedTemporaryFile("r") as output, \
                patch.dict(os.environ, {**DISPATCH, "GH_TOKEN": "t", "GITHUB_OUTPUT": output.name}, clear=True), \
                patch.object(review, "get_json", side_effect=environment_answers()), \
                patch("sys.stdout", new_callable=io.StringIO) as printed:
            review.admit("42")
            self.assertEqual(output.read(), "environment=may-claude-review\n")
        self.assertEqual(printed.getvalue(), "May app review admission passed for may-app #42 (dispatched by samuelalake)\n")
        refusals = {
            "another branch": ({**DISPATCH, "GITHUB_REF": "refs/heads/x"}, environment_answers(), "manual dispatch from its main"),
            "a push": ({**DISPATCH, "GITHUB_EVENT_NAME": "push"}, environment_answers(), "manual dispatch from its main"),
            "no required reviewer": (DISPATCH, environment_answers({**ENVIRONMENT, "protection_rules": []}), "required-reviewer rule"),
            "administrator bypass": (DISPATCH, environment_answers({**ENVIRONMENT, "can_admins_bypass": True}), "bypass"),
            "another branch allowed": (DISPATCH, environment_answers(policies={"branch_policies": [{"name": "main"}, {"name": "x"}]}), "only main"),
        }
        for name, (env, answers, message) in refusals.items():
            with self.subTest(case=name), patch.dict(os.environ, {**env, "GH_TOKEN": "t"}, clear=True), \
                    patch.object(review, "get_json", side_effect=answers):
                with self.assertRaises(GateDenial) as caught:
                    review.admit("42")
                self.assertIn(message, caught.exception.args[0])

    def run_cli(self, *args):
        env = {**DISPATCH, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GH_TOKEN": "t"}
        return subprocess.run([sys.executable, "-I", "-S", "-B", str(HERE / "review.py"), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_pull_request_number_is_digits_only_and_refused_before_anything_is_read(self):
        for value in ("12a", "", "0", "-1", "1 ", " 1", "1e3", "0x2a", "42;id", "$(id)", "１２", "12345678"):
            with self.subTest(value=value):
                done = self.run_cli("admit", "--pr-number", value)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertEqual(done.stderr, "May app review admission denied: Pull request number must be a positive integer\n")

    def test_only_install_runs_without_a_pull_request_number(self):
        for args in (("admit",), ("fetch",), ("review",), ("install", "--pr-number", "42")):
            with self.subTest(args=args):
                done = self.run_cli(*args)
                self.assertEqual(done.stderr, "May app review admission denied: A pull request number is given with "
                                              "admit, fetch and review, and only then\n")


def package(binary=b"#!/bin/sh\n"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/claude")
        info.size = len(binary)
        archive.addfile(info, io.BytesIO(binary))
    return data.getvalue()


class InstallTests(unittest.TestCase):
    def test_the_pinned_release_is_exact(self):
        self.assertRegex(review.CLI_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertTrue(review.CLI_URL.startswith("https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/"))
        self.assertTrue(review.CLI_URL.endswith("-" + review.CLI_VERSION + ".tgz"))
        self.assertRegex(review.CLI_SHA512, r"^sha512-[A-Za-z0-9+/]{86}==$")

    def test_only_a_download_matching_the_pinned_sha512_is_installed(self):
        data = package(b"the binary")
        pinned = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        with patch.object(review, "CLI_SHA512", pinned):
            self.assertEqual(review.checked_cli(data), b"the binary")
            with self.assertRaises(ReviewError):
                review.checked_cli(package(b"another binary"))


class FetchTests(unittest.TestCase):
    def test_the_token_is_never_in_git_arguments_links_are_plain_files_and_git_says_nothing(self):
        calls = []

        def fake_run(argv, **options):
            calls.append((argv, options))
            return subprocess.CompletedProcess(argv, 0, stdout=SHA + "\n" if "rev-parse" in argv else None)

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RUNNER_TEMP": temp}), \
                patch.object(review.subprocess, "run", side_effect=fake_run):
            review.fetch_head(TOKEN, SHA, Path(temp) / "view" / "source")
        self.assertEqual([argv[3:5] for argv, _ in calls], [["init", "-q"], ["fetch", "-q"], ["checkout", "-q"], ["rev-parse", "HEAD"]])
        for argv, options in calls:
            self.assertFalse(any(TOKEN in part for part in argv))
            env = options["env"]
            self.assertEqual(env["GIT_CONFIG_VALUE_0"],
                             "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + TOKEN).encode()).decode())
            self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]), ("2", "core.symlinks", "false"))
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
        for _, options in calls[:3]:
            self.assertEqual((options["stdout"], options["stderr"]), (subprocess.DEVNULL, subprocess.DEVNULL))
        self.assertIn(SHA, calls[1][0])

    def test_a_failed_fetch_another_commit_or_a_link_in_the_checkout_stops_with_a_fixed_line(self):
        def linked(argv, **options):
            if "checkout" in argv:
                os.symlink("/proc/self/environ", Path(argv[2]) / "environ")
            return subprocess.CompletedProcess(argv, 0, stdout=SHA if "rev-parse" in argv else None)
        for name, fake, message in (("fetch fails", lambda argv, **o: subprocess.CompletedProcess(argv, 128), "could not be fetched"),
                                    ("another commit", lambda argv, **o: subprocess.CompletedProcess(argv, 0, stdout=OTHER), "not the pull request head"),
                                    ("a symbolic link", linked, "symbolic link")):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RUNNER_TEMP": temp}), \
                    patch.object(review.subprocess, "run", side_effect=fake):
                with self.assertRaises(ReviewError) as caught:
                    review.fetch_head(TOKEN, SHA, Path(temp) / "view" / "source")
                self.assertIn(message, caught.exception.args[0])


def api_answers(files=1, diff=(200, b"diff --git a/x b/x\n"), extra=None):
    """A fake app_get: the pull request's lists and its diff, with `files` changed files over pages of 100."""
    def get(path, token, accept="application/vnd.github+json", limit=None):
        assert token == TOKEN
        if accept == "application/vnd.github.diff":
            return diff
        page = int(path.rsplit("page=", 1)[1])
        if path.startswith("pulls/42/files"):
            items = [{"filename": f"f{i}.ts", "status": "modified", "additions": 1, "deletions": 0, "patch": "x"}
                     for i in range(files)][(page - 1) * 100:page * 100]
        elif path.startswith("issues/42/comments"):
            items = [{"user": {"login": "claude"}, "body": "## Code review", "created_at": "t", "id": 9}] if page == 1 else []
        else:
            items = (extra or {}).get(path.split("?")[0], []) if page == 1 else []
        return 200, json.dumps(items).encode()
    return get


class PrepareViewTests(unittest.TestCase):
    def test_the_pull_request_is_written_as_files_for_the_model(self):
        with tempfile.TemporaryDirectory() as temp:
            view = Path(temp)
            (view / "source" / "backend").mkdir(parents=True)
            (view / "source" / "CLAUDE.md").write_text("rules")
            (view / "source" / "backend" / "CLAUDE.md").write_text("rules")
            (view / "source" / ".git").mkdir()
            (view / "source" / ".git" / "CLAUDE.md").write_text("not a rule file")
            review.prepare_view(TOKEN, 42, pull(title="T", body="B"), view, api_answers(files=150, extra={
                "pulls/42/reviews": [{"user": {"login": "r"}, "state": "APPROVED", "body": "ok", "submitted_at": "t"}]}))
            data = json.loads((view / "pr" / "pull.json").read_text())
            self.assertEqual((data["title"], data["description"], data["head"], data["draft"], data["diff_available"]), ("T", "B", SHA, False, True))
            self.assertEqual(len(data["changed_files"]), 150, "every page")
            self.assertNotIn("patch", data["changed_files"][0])
            self.assertEqual(data["comments"], [{"author": "claude", "created_at": "t", "body": "## Code review"}])
            self.assertEqual(data["reviews"][0]["state"], "APPROVED")
            self.assertEqual(data["claude_md_files"], ["CLAUDE.md", "backend/CLAUDE.md"])
            self.assertEqual((view / "pr" / "diff.patch").read_bytes(), b"diff --git a/x b/x\n")
            self.assertEqual(list((view / "work").iterdir()), [], "the working directory is empty")

    def test_a_diff_github_will_not_give_is_marked_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "source").mkdir()
            review.prepare_view(TOKEN, 42, pull(), Path(temp), api_answers(diff=(406, b"")))
            self.assertFalse(json.loads((Path(temp) / "pr" / "pull.json").read_text())["diff_available"])
            self.assertEqual((Path(temp) / "pr" / "diff.patch").read_bytes(), b"")


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, stat, sys
argv = sys.argv[1:]
server = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]["github_inline_comment"]
posted = server["env"]["MAY_REVIEW_POSTED"]
token_file = server["env"]["MAY_REVIEW_TOKEN_FILE"]
token = open(token_file).read()
values = list(os.environ.values()) + argv + [open(argv[argv.index("--mcp-config") + 1]).read()]
seen = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "token_file": token_file,
        "token_mode": oct(stat.S_IMODE(os.stat(token_file).st_mode)), "token_seen": any(token in v for v in values),
        "work_empty": os.listdir(os.getcwd()) == []}
json.dump(seen, open(os.path.join(os.path.dirname(posted), "seen.json"), "w"))
mode = open(os.path.join(os.path.dirname(posted), "..", "mode")).read()
if mode != "no-server":
    open(posted, "a").write("started\n" + ("inline\ninline\nsummary\nrefused\n" if mode == "posted" else "failed\n" if mode == "failed" else ""))
print("transcript: " + "PRIVATE-MAY-APP-CONTENT-7f3c", file=sys.stderr)
print(json.dumps({"type": "result", "is_error": mode == "error", "num_turns": 31, "total_cost_usd": 1.234,
                  "permission_denials": [{"tool": "PRIVATE-MAY-APP-CONTENT-7f3c"}], "result": "PRIVATE-MAY-APP-CONTENT-7f3c"}))
sys.exit(3 if mode == "exit" else 0)
'''


class ReviewRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "may-review"
        self.view = Path(self.temp.name) / "view"
        (self.view / "source").mkdir(parents=True)
        self.root.mkdir()
        (self.root / "head").write_text(SHA + "\n")
        self.cli = Path(self.temp.name) / "claude"
        self.cli.write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.cli.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def run_review(self, mode, head=SHA):
        """review.review with the fake CLI; everything this process writes to fds 1 and 2 is captured."""
        (self.root / "mode").write_text(mode)
        env = {**DISPATCH, "RUNNER_TEMP": self.temp.name, "PATH": os.environ.get("PATH", ""), "MAY_APP_REVIEW_TOKEN": TOKEN,
               "MAY_REVIEW_CLAUDE_TOKEN": "sk-ant-oat01-claude", "GITHUB_STEP_SUMMARY": "/tmp/summary", "GITHUB_ENV": "/tmp/env",
               "ACTIONS_RUNTIME_TOKEN": "runtime", "ACTIONS_CACHE_URL": "https://cache", "GITHUB_WORKSPACE": self.temp.name + "/ws"}
        error = None
        with tempfile.TemporaryFile() as captured, patch.dict(os.environ, env, clear=True), \
                patch.object(review, "get_app_pull", return_value=pull(head={"sha": head, "repo": {"full_name": "May-Assistant/may-app"}})):
            saved = os.dup(1), os.dup(2)
            sys.stdout.flush()
            os.dup2(captured.fileno(), 1)
            os.dup2(captured.fileno(), 2)
            stdout = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), write_through=True)
            try:
                with patch("sys.stdout", new=stdout):
                    review.review("42", cli=self.cli, plugin=Path(self.temp.name) / "plugin", view=self.view, get=api_answers())
            except (ReviewError, GateDenial) as caught:
                error = caught
            finally:
                stdout.close()
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
            captured.seek(0)
            printed = captured.read().decode()
        seen_path = self.root / "output" / "seen.json"
        seen = json.loads(seen_path.read_text()) if seen_path.exists() else None
        return printed, error, seen

    def test_nothing_from_the_review_reaches_this_log_only_numbers_and_the_head(self):
        printed, error, _ = self.run_review("posted")
        self.assertIsNone(error)
        self.assertNotIn(MARKER, printed)
        self.assertEqual(printed, "Claude Code exited with status 0; error: False; turns: 31; cost: $1.23; tool calls denied: 1\n"
                                  f"May app review of may-app #42 at {SHA}: 2 inline and 1 summary comment(s) posted, 0 post(s) failed, 1 refused\n")

    def test_a_failed_review_stops_with_a_fixed_line_and_still_shows_nothing(self):
        for mode, message in (("exit", "did not finish cleanly"), ("error", "did not finish cleanly"),
                              ("failed", "did not finish cleanly"), ("no-server", "comment server never started")):
            with self.subTest(mode=mode):
                printed, error, _ = self.run_review(mode)
                self.assertIsInstance(error, ReviewError)
                self.assertIn(message, error.args[0])
                self.assertNotIn(MARKER, printed)

    def test_a_head_that_moved_after_the_fetch_is_not_reviewed(self):
        printed, error, seen = self.run_review("posted", head=OTHER)
        self.assertIsInstance(error, ReviewError)
        self.assertIn("head moved", error.args[0])
        self.assertIsNone(seen, "Claude Code never ran")

    def test_claude_code_never_holds_the_review_token_and_starts_in_an_empty_directory(self):
        # Security review of #21, H1 and M2.
        _, _, seen = self.run_review("posted")
        self.assertFalse(seen["token_seen"], "the review token is in no variable, argument or config Claude Code has")
        self.assertEqual(seen["token_file"], str(self.root / "secret" / "token"))
        self.assertEqual(seen["token_mode"], "0o600")
        self.assertFalse((self.root / "secret" / "token").exists(), "removed once Claude Code exits")
        self.assertEqual(seen["cwd"], str(self.view / "work"))
        self.assertTrue(seen["work_empty"])
        env = seen["env"]
        self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertNotIn("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", env, "it needs bubblewrap in 2.1.296; Bash is denied anyway")
        self.assertNotIn("GITHUB_ACTIONS", env, "which would turn that scrub on")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-claude")
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_REPO", "GITHUB_STEP_SUMMARY", "GITHUB_ENV", "ACTIONS_RUNTIME_TOKEN",
                     "ACTIONS_CACHE_URL", "MAY_APP_REVIEW_TOKEN", "MAY_REVIEW_CLAUDE_TOKEN", "GITHUB_REPOSITORY", "GITHUB_WORKSPACE"):
            self.assertNotIn(name, env)

    def test_the_model_has_no_shell_and_reads_only_the_view(self):
        _, _, seen = self.run_review("posted")
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["-p", "/code-review:code-review --comment May-Assistant/may-app/pull/42"])
        for flag, value in (("--setting-sources", "user"), ("--permission-mode", "dontAsk"), ("--output-format", "json"),
                            ("--allowedTools", review.ALLOWED_TOOLS)):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--add-dir", argv)
        allowed, denied = review.ALLOWED_TOOLS.split(","), argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertEqual([t for t in allowed if t.startswith(("Bash", "Read", "Grep", "Glob", "Web", "Write", "Edit"))],
                         ["Read(//tmp/may-review-view/**)"])
        for rule in ("Bash", "Grep", "Glob", "Write", "Edit", "WebFetch", "WebSearch", "Read(//proc/**)", "Read(//sys/**)",
                     "Read(//home/**)", f"Read(/{self.root.resolve()}/**)", f"Read(/{Path(self.temp.name, 'ws').resolve()}/**)"):
            self.assertIn(rule, denied)
        prompt = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(f"{self.view}/pr/pull.json", prompt)
        self.assertIn(f"{self.view}/source", prompt)
        self.assertIn("no shell and no gh", prompt)


class CommentServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.posted = Path(self.temp.name) / "posted"
        self.config = {"number": 42, "head": SHA, "token": TOKEN, "posted": str(self.posted), "count": 0}
        self.posts = []
        self.pull = pull()

    def tearDown(self):
        self.temp.cleanup()

    def converse(self, *calls, status=201):
        def call_api(method, path, payload, token):
            self.assertEqual(token, TOKEN)
            if method == "GET":
                self.assertEqual(path, "/repos/May-Assistant/may-app/pulls/42")
                return 200, self.pull
            self.posts.append((path, payload))
            return status, {}
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        messages += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                     for i, (n, a) in enumerate(calls)]
        out = io.StringIO()
        review_mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out, self.config, call_api)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([a["id"] for a in answers], [1, 2] + [10 + i for i in range(len(calls))], "a notification gets no answer")
        self.assertEqual([t["name"] for t in answers[1]["result"]["tools"]], ["create_inline_comment", "create_summary_comment"])
        return [a["result"] for a in answers[2:]]

    def test_an_inline_comment_goes_on_the_reviewed_head_with_the_footer(self):
        [result] = self.converse(("create_inline_comment", {"path": "backend/src/a.ts", "line": 12, "startLine": 10,
                                                            "body": "Bug here. ghp_" + "Q" * 36 + " sk-ant-oat01-abc", "confirmed": True}))
        self.assertFalse(result["isError"])
        [(path, payload)] = self.posts
        self.assertEqual(path, "/repos/May-Assistant/may-app/pulls/42/comments")
        self.assertEqual({k: payload[k] for k in ("commit_id", "path", "line", "start_line", "side", "start_side")},
                         {"commit_id": SHA, "path": "backend/src/a.ts", "line": 12, "start_line": 10, "side": "RIGHT", "start_side": "RIGHT"})
        self.assertEqual(payload["body"], f"Bug here. [redacted] [redacted]\n\n<!-- may-app-review head={SHA} -->"
                                          "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
        self.assertEqual(self.posted.read_text(), "started\ninline\n")

    def test_a_summary_goes_to_the_pull_request_conversation(self):
        [result] = self.converse(("create_summary_comment", {"body": "## Code review\n\nNo issues found."}))
        self.assertFalse(result["isError"])
        [(path, payload)] = self.posts
        self.assertEqual(path, "/repos/May-Assistant/may-app/issues/42/comments")
        self.assertTrue(payload["body"].startswith("## Code review\n\nNo issues found.\n\n<!-- may-app-review head="))
        self.assertTrue(payload["body"].endswith(review_mcp.FOOTER))

    def test_a_body_carrying_the_review_token_in_any_spelling_is_refused(self):
        # Security review of #21, M1.
        raw = TOKEN.encode()
        for body in (TOKEN, " ".join(TOKEN), "-".join(TOKEN[i:i + 4] for i in range(0, len(TOKEN), 4)), TOKEN.upper(),
                     base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="), raw.hex(),
                     raw.hex().upper(), "see " + base64.b64encode(raw).decode()[:40] + "\n" + base64.b64encode(raw).decode()[40:]):
            with self.subTest(body=body[:30]):
                self.posts.clear()
                [result] = self.converse(("create_summary_comment", {"body": body}))
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["text"], "Not posted: body holds a credential")
                self.assertEqual(self.posts, [])

    def test_at_most_max_comments_post_in_one_run(self):
        results = self.converse(*[("create_summary_comment", {"body": f"c{i}"}) for i in range(review_mcp.MAX_COMMENTS + 2)])
        self.assertEqual(len(self.posts), review_mcp.MAX_COMMENTS)
        self.assertTrue(all(r["isError"] for r in results[review_mcp.MAX_COMMENTS:]))
        self.assertIn("already posted", results[-1]["content"][0]["text"])

    def test_nothing_is_posted_once_the_pull_request_changed(self):
        # Security review of #21, L1: closed, drafted, retargeted or pushed to during the run.
        for change in ({"state": "closed"}, {"draft": True}, {"base": {"ref": "release"}}, {"head": {"sha": OTHER}}):
            with self.subTest(change=change):
                self.posts.clear()
                self.pull = pull(**change)
                [result] = self.converse(("create_summary_comment", {"body": "x"}))
                self.assertTrue(result["isError"])
                self.assertEqual(self.posts, [])

    def test_nothing_is_posted_unconfirmed_off_the_head_or_outside_the_repository(self):
        base = {"path": "a.ts", "line": 3, "body": "x", "confirmed": True}
        results = self.converse(
            ("create_inline_comment", {**base, "confirmed": None}),
            ("create_inline_comment", {**base, "commit_id": OTHER}),
            ("create_inline_comment", {**base, "path": "../secrets"}),
            ("create_inline_comment", {**base, "path": "/etc/passwd"}),
            ("create_inline_comment", {**base, "line": 0}),
            ("create_inline_comment", {**base, "line": True}),
            ("create_inline_comment", {**base, "startLine": 9}),
            ("create_inline_comment", {**base, "side": "BOTH"}),
            ("create_inline_comment", {**base, "body": "  "}),
            ("create_summary_comment", {"body": "x" * (review_mcp.MAX_BODY + 1)}),
            ("delete_pull_request", {}))
        self.assertEqual(self.posts, [])
        self.assertFalse(results[0]["isError"], "an unconfirmed call is not an error, as in the action")
        self.assertTrue(all(r["isError"] for r in results[1:]))

    def test_a_refused_post_is_counted_and_answered_with_its_status_only(self):
        [result] = self.converse(("create_summary_comment", {"body": "x"}), status=422)
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "Not posted: GitHub answered 422")
        self.assertEqual(self.posted.read_text(), "started\nfailed\n")

    def test_it_starts_only_with_a_pull_request_a_head_and_a_readable_token_file(self):
        token_file = Path(self.temp.name) / "token"
        token_file.write_text(TOKEN)
        good = {"MAY_PR_NUMBER": "42", "MAY_REVIEW_HEAD": SHA, "MAY_REVIEW_TOKEN_FILE": str(token_file)}
        self.assertEqual(review_mcp.settings(good)["token"], TOKEN)
        self.assertIsNone(review_mcp.settings({**good, "MAY_REVIEW_TOKEN_FILE": "", "GH_TOKEN": TOKEN}), "never from the environment")
        for change in ({"MAY_REVIEW_TOKEN_FILE": str(token_file) + ".missing"}, {"MAY_PR_NUMBER": "4a"}, {"MAY_REVIEW_HEAD": "main"}):
            with self.subTest(change=change):
                self.assertIsNone(review_mcp.settings({**good, **change}))


class ReviewWorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(static_check.problems(self.root), [])

    def tearDown(self):
        self.temp.cleanup()

    def flagged(self, relative, old, new, fragment):
        path = self.root / relative
        text = path.read_text()
        self.assertEqual(text.count(old), 1, old)
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        path.write_text(text)
        self.assertTrue(any(fragment in p for p in found), found)

    def test_no_workflow_may_print_claudes_full_output_or_report(self):
        old = "          plugins: 'code-review@may-pinned-claude-code'\n"
        for line in ("          show_full_output: true\n", "          show_full_output: 'true'\n", "          display_report: true\n",
                     '          display_report: "True"\n'):
            with self.subTest(line=line):
                self.flagged(".github/workflows/claude-code-review.yml", old, old + line, "prints Claude's full output or report")

    def test_the_review_workflow_is_pinned_whole(self):
        name = ".github/workflows/may-app-review.yml"
        checkout = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4\n        with:\n          persist-credentials: false\n      - id: gate\n"
        review_permissions = "    permissions:\n      contents: read\n    steps:\n"
        cases = {
            "an unpinned action": (checkout, checkout.replace("@11d5960a326750d5838078e36cf38b85af677262 # v4", "@v4"), "not pinned"),
            "claude-code-action": ("      - name: Install the pinned", "      - uses: anthropics/claude-code-action@1d6de8cb0c237e7c15e9e1bdf973826ebae490cc\n"
                                   "      - name: Install the pinned", "two checkouts"),
            "a schedule": ("on:\n  workflow_dispatch:\n", "on:\n  schedule:\n    - cron: '0 * * * *'\n  workflow_dispatch:\n", "exactly a pr_number"),
            "a second input": ("        type: string\n", "        type: string\n      model:\n        type: string\n", "exactly a pr_number"),
            "a write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: read\n  pull-requests: write\nconcurrency:", "read-only"),
            "an OIDC token": (review_permissions, review_permissions.replace("read\n", "read\n      id-token: write\n"), "read-only"),
            "actions: read in review": (review_permissions, review_permissions.replace("read\n", "read\n      actions: read\n"), "read-only"),
            "a GitHub token in the fetch": ("        env:\n          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n          MAY_APP_REVIEW_TOKEN: ${{ secrets.MAY_APP_REVIEW_TOKEN }}\n        run: python3 -I -S -B tools/may_release/review.py fetch",
                                            "        env:\n          GH_TOKEN: ${{ github.token }}\n          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n          MAY_APP_REVIEW_TOKEN: ${{ secrets.MAY_APP_REVIEW_TOKEN }}\n        run: python3 -I -S -B tools/may_release/review.py fetch", "secret or env"),
            "another secret": ("          MAY_REVIEW_CLAUDE_TOKEN: ${{ secrets.MAY_REVIEW_CLAUDE_TOKEN }}\n",
                               "          MAY_REVIEW_CLAUDE_TOKEN: ${{ secrets.MAY_REVIEW_CLAUDE_TOKEN }}\n          X: ${{ secrets.MAY_APP_READ_KEY }}\n", "secret or env"),
            "the Claude token in the fetch": ("          MAY_APP_REVIEW_TOKEN: ${{ secrets.MAY_APP_REVIEW_TOKEN }}\n        run: python3 -I -S -B tools/may_release/review.py fetch",
                                              "          MAY_APP_REVIEW_TOKEN: ${{ secrets.MAY_APP_REVIEW_TOKEN }}\n          MAY_REVIEW_CLAUDE_TOKEN: ${{ secrets.MAY_REVIEW_CLAUDE_TOKEN }}\n"
                                              "        run: python3 -I -S -B tools/may_release/review.py fetch", "secret or env"),
            "an input in shell": ('review.py review --pr-number "$MAY_PR_NUMBER"', "review.py review --pr-number '${{ inputs.pr_number }}'", "runs something other"),
            "another command": ("run: python3 -I -S -B tools/may_release/review.py install", "run: cat \"$RUNNER_TEMP/may-review/output/stdout.json\"", "runs something other"),
            "a step summary": ("      - name: Remove private source", "      - name: Summary\n        run: echo x >> \"$GITHUB_STEP_SUMMARY\"\n      - name: Remove private source", "runs something other"),
            "a cleanup that can be skipped": ("        if: always()\n", "", "always-run cleanup"),
            "a cleanup that leaves the view": (' /tmp/may-review-view\n', "\n", "runs something other"),
            "another plugin commit": ("ref: 0d7f14dd352fa67affd4e172eb1b6f524dbd709e", "ref: " + "c" * 40, "pinned plugin commit"),
            "the environment named in the workflow": ("    environment: ${{ needs.admission.outputs.environment }}\n", "    environment: may-claude-review\n", "admission chose"),
            "a longer timeout": ("    timeout-minutes: 60\n", "    timeout-minutes: 600\n", "timeout"),
            "an artifact": ("      - name: Remove private source", "      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02\n      - name: Remove private source", "artifact"),
        }
        for case, (old, new, fragment) in cases.items():
            with self.subTest(case=case):
                self.flagged(name, old, new, fragment)

    def test_the_models_tools_and_environment_are_pinned(self):
        # Security review of #21, H1 and M2: each of these reopens a way to a token or out of the runner.
        name = "tools/may_release/review.py"
        allowed = '"Agent,Task,Read(//tmp/may-review-view/**)")'
        cases = {
            "a gh command": (allowed, '"Agent,Task,Read(//tmp/may-review-view/**),Bash(gh pr view:*)")', "ALLOWED_TOOLS"),
            "a bare Read": (allowed, '"Agent,Task,Read")', "ALLOWED_TOOLS"),
            "Read of the whole runner": (allowed, '"Agent,Task,Read(//**)")', "ALLOWED_TOOLS"),
            "Grep": (allowed, '"Agent,Task,Read(//tmp/may-review-view/**),Grep")', "ALLOWED_TOOLS"),
            "the shell no longer denied": ('DENIED_TOOLS = ("Bash,Write,', 'DENIED_TOOLS = ("Write,', "DENIED_TOOLS"),
            "/proc readable": ("Read(//proc/**),", "", "DENIED_TOOLS"),
            "/home readable": ("Read(//home/**),", "", "DENIED_TOOLS"),
            "the GitHub token handed to Claude Code": ('"CLAUDE_CODE_OAUTH_TOKEN": claude_token,', '"CLAUDE_CODE_OAUTH_TOKEN": claude_token, "GH_TOKEN": os.environ.get("MAY_APP_REVIEW_TOKEN"),', "claude_env"),
            "CLAUDE.md files loaded": ('"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",', "", "claude_env"),
            "the token in the server's config": ('"env": {"MAY_REVIEW_TOKEN_FILE": str(token_file),', '"env": {"GH_TOKEN": "${GH_TOKEN}", "MAY_REVIEW_TOKEN_FILE": str(token_file),', "comment server"),
            "the checkout as working directory": ("cwd=view / \"work\",", "cwd=view / \"source\",", "empty work directory"),
            "an added directory": ('"--plugin-dir", str(plugin),', '"--plugin-dir", str(plugin), "--add-dir", str(view / "source"),', "empty work directory"),
            "another view": ('VIEW = Path("/tmp/may-review-view")', 'VIEW = Path("/home/runner/view")', "VIEW is not"),
        }
        for case, (old, new, fragment) in cases.items():
            with self.subTest(case=case):
                self.flagged(name, old, new, fragment)


if __name__ == "__main__":
    unittest.main()
