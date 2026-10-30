"""testflight_feedback.py: TestFlight feedback filed as private may-app issues, with nothing of it in this log.
Nothing here reaches the network: App Store Connect, the images, git and GitHub are all fakes."""

import base64
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import publish
import static_check
import testflight_feedback as tool
from gate import GateDenial
from testflight_feedback import FeedbackError

NOW = datetime.datetime(2026, 10, 27, 18, 0, tzinfo=datetime.timezone.utc)
TOKEN = "ghs_" + "t" * 36
KEY_ID, ISSUER = "ABCDE12345", "69a6de70-0000-47e3-e053-5b8c7c11a4d1"
P8 = b"-----BEGIN PRIVATE KEY-----\nMIGT\n-----END PRIVATE KEY-----\n"
# Feedback content: none of it may ever reach stdout or stderr.
COMMENT = "the send button froze PRIVATE-COMMENT-7f3a"
DEVICE = "iPhone17,1-PRIVATE-DEVICE"
LOG = "Thread 0 Crashed PRIVATE-CRASHLOG-91c2"
EMAIL = "tester-PRIVATE@example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def iso(days_ago, minutes=0):
    return (NOW - datetime.timedelta(days=days_ago, minutes=minutes)).isoformat().replace("+00:00", "Z")


def submission(sid, days_ago, minutes=0, shots=1):
    return {"id": sid, "type": "x", "attributes": {
        "createdDate": iso(days_ago, minutes), "comment": COMMENT, "email": EMAIL, "deviceModel": DEVICE,
        "osVersion": "26.1", "buildBundleId": "io.github.mayassistant.may.playground",
        "screenshots": [{"url": f"https://tf-feedback.itunes.apple.com/{sid}/{n}.png", "width": 1, "height": 1,
                         "expirationDate": iso(-1)} for n in range(shots)]}}


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def quiet(self, function, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = function(*args, **kwargs)
        return result, out.getvalue(), err.getvalue()

    def known_ids(self, ids):
        folder = self.root / tool.STATE
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / "known.json").write_text(json.dumps(ids))

    def asc_env(self, key_id=KEY_ID):
        os.environ.update({tool.KEY_SECRET: base64.b64encode(P8).decode(), tool.KEY_ID: key_id, tool.ISSUER: ISSUER})


class AdmitTests(Case):
    def test_only_the_schedule_or_a_dispatch_of_main_is_admitted_into_a_no_reviewer_environment(self):
        policy = {"can_admins_bypass": False, "protection_rules": [],
                  "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        branches = {"branch_policies": [{"name": "main", "type": "branch"}]}

        def get(path, token):
            self.assertEqual(token, "gh")
            return branches if path.endswith("deployment-branch-policies?per_page=100") else policy

        output = self.root / "output"
        os.environ.update({"GH_TOKEN": "gh", "GITHUB_REPOSITORY": "May-Assistant/may-releases",
                           "GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(output)})
        for event in ("schedule", "workflow_dispatch"):
            os.environ["GITHUB_EVENT_NAME"] = event
            _, out, _ = self.quiet(tool.admit, get)
            self.assertEqual(out, "May TestFlight feedback admission passed\n")
        self.assertIn("environment=may-testflight-feedback\n", output.read_text())
        for event, ref in (("pull_request", "refs/heads/main"), ("schedule", "refs/heads/other")):
            with self.subTest(event=event, ref=ref):
                os.environ.update({"GITHUB_EVENT_NAME": event, "GITHUB_REF": ref})
                with self.assertRaises(GateDenial):
                    tool.admit(get)


class KnownTests(Case):
    def test_the_filed_ids_come_from_the_markers_of_every_page(self):
        pages = {1: [{"body": f"x\n<!-- testflight-feedback id=s{n} -->\n"} for n in range(100)],
                 2: [{"body": "<!-- testflight-feedback id=crash-1 -->"}, {"body": None}, "junk"]}
        calls = []

        def api(method, path, payload, token):
            calls.append(path)
            self.assertEqual((method, payload, token), ("GET", None, TOKEN))
            return 200, pages[int(path.rsplit("=", 1)[1])]

        os.environ[tool.ISSUES_TOKEN] = TOKEN
        _, out, _ = self.quiet(tool.known, api)
        self.assertEqual(out, "May TestFlight feedback: 101 submissions already filed\n")
        self.assertEqual(len(calls), 2)
        self.assertNotIn(tool.ISSUES_TOKEN, os.environ, "the token leaves the environment at once")
        self.assertEqual(len(json.loads((self.root / tool.STATE / "known.json").read_text())), 101)

    def test_an_unreadable_list_files_nothing_and_no_token_reads_nothing(self):
        os.environ[tool.ISSUES_TOKEN] = TOKEN
        with self.assertRaises(FeedbackError):
            self.quiet(tool.known, lambda *a: (502, None))
        self.assertFalse((self.root / tool.STATE / "known.json").exists())
        _, out, _ = self.quiet(tool.known, lambda *a: self.fail("no call without a token"))
        self.assertIn("MAY_APP_ISSUES_TOKEN is not set", out)
        _, out, _ = self.quiet(tool.read, lambda *a: self.fail("App Store Connect is not read"))
        self.assertIn("no filed submissions were listed", out)


class ReadTests(Case):
    def setUp(self):
        super().setUp()
        self.lists = {}
        self.calls = []
        self.kids = []
        signing = patch("testflight_feedback.asc_token", side_effect=lambda key, issuer, kid: self.kids.append(
            (Path(key).read_bytes(), issuer, kid)) or "asc-token")
        signing.start()
        self.addCleanup(signing.stop)

    def get(self, token, url):
        self.assertEqual(token, "asc-token")
        self.calls.append(url)
        if url.endswith("/crashLog"):
            return 200, {"data": {"attributes": {"logText": LOG}}}
        for (app, kind), items in self.lists.items():
            if f"apps/{app}/{tool.KINDS[kind]}?" in url:
                return 200, {"data": items, "links": {}}
        return 200, {"data": [], "links": {}}

    def read(self):
        return self.quiet(tool.read, self.get, lambda url: (PNG, "png"), now=NOW)

    def test_new_submissions_are_read_oldest_first_with_their_screenshots_and_crash_logs(self):
        self.known_ids(["old-filed"])
        self.asc_env()
        self.lists = {("6822072535", "screenshot"): [submission("s-new", 1), submission("old-filed", 2),
                                                     submission("s-stale", tool.LOOKBACK_DAYS, 1)],
                      ("6821782401", "crash"): [submission("c-new", 3, shots=0)]}
        _, out, err = self.read()
        self.assertEqual(out, "May TestFlight feedback: 2 new submissions, 2 read this run (0 left for later runs, 0 "
                              "screenshots not fetched)\n")
        self.assertEqual(err, "")
        records = json.loads((self.root / tool.STATE / "records.json").read_text())
        self.assertEqual([(r["id"], r["kind"], r["app"]) for r in records],
                         [("c-new", "crash", "May"), ("s-new", "screenshot", "May Playground")])
        self.assertEqual(records[0]["log"], LOG)
        self.assertEqual(records[1]["images"], ["1.png"])
        self.assertEqual((self.root / tool.STATE / "images" / "s-new" / "1.png").read_bytes(), PNG)
        self.assertNotIn(EMAIL, json.dumps(records), "the tester's email is never copied")
        self.assertEqual(self.kids, [(P8, ISSUER, KEY_ID)], "signed once, with the feedback key's own ID")
        self.assertFalse((self.root / tool.STATE / "asc.p8").exists(), "the key file is removed after signing")
        self.assertTrue(all(url.startswith(tool.ASC_API) for url in self.calls))
        for name in (tool.KEY_SECRET, tool.KEY_ID, tool.ISSUER):
            self.assertNotIn(name, os.environ)

    def test_at_most_max_new_are_read_a_run_the_oldest_first(self):
        self.known_ids([])
        self.asc_env()
        self.lists = {("6822072535", "screenshot"): [submission(f"s{n}", 1, minutes=n) for n in range(30)]}
        _, out, _ = self.read()
        self.assertIn("30 new submissions, 20 read this run (10 left for later runs", out)
        records = json.loads((self.root / tool.STATE / "records.json").read_text())
        self.assertEqual([r["id"] for r in records], [f"s{n}" for n in range(29, 9, -1)])

    def test_the_release_key_or_a_malformed_key_is_refused(self):
        for key_id in (tool.RELEASE_KEY_ID, "short", ""):
            with self.subTest(key_id=key_id):
                self.known_ids([])
                self.asc_env(key_id)
                with self.assertRaises(FeedbackError):
                    self.read()
                self.assertEqual(self.calls, [])
        self.known_ids([])
        _, out, _ = self.read()
        self.assertIn("MAY_ASC_FEEDBACK_KEY_BASE64 is not set", out)

    def test_an_unreadable_list_says_which_roles_can_read_it(self):
        self.known_ids([])
        self.asc_env()
        with self.assertRaises(FeedbackError) as caught:
            self.quiet(tool.read, lambda token, url: (403, {"errors": [{"detail": COMMENT}]}), lambda url: None, now=NOW)
        self.assertIn("Admin, App Manager or Developer", caught.exception.args[0])
        self.assertNotIn(COMMENT, caught.exception.args[0])

    def test_screenshots_come_only_from_apples_hosts_over_https_as_png_or_jpeg(self):
        class Reply(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opened = []

        class Opener:
            def open(self, request, timeout):
                opened.append(request.full_url)
                return Reply(PNG if "png" in request.full_url else b"GIF89a")

        self.assertEqual(tool.fetch_image("https://tf.itunes.apple.com/a.png", Opener()), (PNG, "png"))
        for url in ("http://tf.itunes.apple.com/a.png", "https://apple.com.evil.example/a.png", "https://example.com/a.png",
                    "file:///etc/passwd", None, "https://tf.itunes.apple.com/a.gif"):
            with self.subTest(url=url):
                self.assertIsNone(tool.fetch_image(url, Opener()))
        self.assertEqual(opened, ["https://tf.itunes.apple.com/a.png", "https://tf.itunes.apple.com/a.gif"])


class StoreAndFileTests(Case):
    def records(self, folder, count=1, images=True, log=""):
        records = [{"id": f"s{n}", "kind": "screenshot", "app": "May Playground", "fields": {"deviceModel": DEVICE},
                    "comment": COMMENT + " ``` closing", "images": ["1.png"] if images else [], "log": log}
                   for n in range(count)]
        (folder / "records.json").write_text(json.dumps(records))
        for record in records:
            if record["images"]:
                (folder / "images" / record["id"]).mkdir(parents=True, exist_ok=True)
                (folder / "images" / record["id"] / "1.png").write_bytes(PNG)

    def test_screenshots_go_to_may_renders_and_are_never_pruned(self):
        folder = self.root / tool.STATE
        folder.mkdir()
        self.records(folder)
        os.environ.update({"MAY_APP_RENDERS_WRITE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 120 +
                           "\n-----END OPENSSH PRIVATE KEY-----", "GITHUB_RUN_ID": "4242"})
        seen = []
        with patch("testflight_feedback.publish_tree", side_effect=lambda *a, **k: seen.append((a, k))):
            _, out, _ = self.quiet(tool.store)
        self.assertEqual(out, "May TestFlight feedback: 1 screenshots stored in may-renders\n")
        (tree, prefix, run, entry, _message, _key, _git), options = seen[0]
        self.assertEqual((Path(tree), prefix, run, entry, options), (folder / "images", "testflight-feedback/", "4242",
                                                                     {"folder": "4242", "run": "4242"}, {"keep": None}))
        self.assertEqual(publish.runs_after([{"folder": "a" * 12}] * 30, {"folder": "b" * 12}, None)[1], [],
                         "keep None removes nothing")

    def test_each_record_becomes_one_marked_issue_with_its_content_fenced(self):
        folder = self.root / tool.STATE
        folder.mkdir()
        self.records(folder, count=2, log=LOG * 3000)
        (folder / "stored.json").write_text(json.dumps({"run": "4242"}))
        posted = []
        os.environ.update({tool.ISSUES_TOKEN: TOKEN, "GITHUB_RUN_ID": "4242"})
        _, out, _ = self.quiet(tool.file_issues, lambda m, p, payload, t: posted.append((m, p, payload, t)) or (201, {}))
        self.assertEqual(out, "May TestFlight feedback: 2 issues filed in may-app\n")
        method, path, payload, token = posted[0]
        self.assertEqual((method, path, token, payload["labels"]), ("POST", "/repos/May-Assistant/may-app/issues", TOKEN,
                                                                     ["testflight-feedback"]))
        body = payload["body"]
        self.assertTrue(body.endswith("<!-- testflight-feedback id=s0 -->\n"))
        self.assertEqual(tool.marked_ids(body), ["s0"])
        self.assertIn("````\n" + COMMENT, body, "the comment's own ``` cannot close its fence")
        self.assertIn("https://github.com/May-Assistant/may-renders/blob/main/testflight-feedback/4242/s0/1.png?raw=true",
                      body)
        self.assertIn("(cut to fit)", body)
        self.assertLessEqual(len(body), tool.MAX_BODY)

    def test_a_step_refuses_another_steps_credential(self):
        folder = self.root / tool.STATE
        folder.mkdir()
        self.records(folder)
        for step, other in ((tool.store, tool.ISSUES_TOKEN), (tool.file_issues, "MAY_APP_RENDERS_WRITE_KEY"),
                            (tool.known, tool.KEY_SECRET), (tool.read, tool.ISSUES_TOKEN)):
            with self.subTest(step=step.__name__, other=other), patch.dict(os.environ, {other: "x" * 40}):
                with self.assertRaises(tool.StoreError):
                    self.quiet(step)

    def test_a_refused_issue_stops_the_run_and_no_token_files_nothing(self):
        folder = self.root / tool.STATE
        folder.mkdir()
        self.records(folder)
        os.environ[tool.ISSUES_TOKEN] = TOKEN
        with self.assertRaises(FeedbackError):
            self.quiet(tool.file_issues, lambda *a: (403, None))
        _, out, _ = self.quiet(tool.file_issues, lambda *a: self.fail("no call without a token"))
        self.assertIn("MAY_APP_ISSUES_TOKEN is not set", out)

    def test_only_the_labels_issues_and_a_new_issue_are_reachable(self):
        with patch("testflight_feedback.request", return_value=(200, [])) as sent:
            tool.github("GET", "/repos/May-Assistant/may-app/issues?labels=testflight-feedback&state=all&per_page=100&page=10",
                        None, TOKEN)
            tool.github("POST", "/repos/May-Assistant/may-app/issues", {"body": "b"}, TOKEN)
            for method, path in (("DELETE", "/repos/May-Assistant/may-app/issues/1"),
                                 ("PATCH", "/repos/May-Assistant/may-app/issues/1"),
                                 ("GET", "/repos/May-Assistant/may-app/issues?labels=testflight-feedback&state=all&per_page=100&page=11"),
                                 ("POST", "/repos/May-Assistant/may-releases/issues"),
                                 ("POST", "/repos/May-Assistant/may-app/issues/1/comments")):
                with self.subTest(method=method, path=path), self.assertRaises(FeedbackError):
                    tool.github(method, path, None, TOKEN)
        self.assertEqual(sent.call_count, 2)


class NothingOnStdoutTests(ReadTests):
    def test_no_content_reaches_stdout_or_stderr_from_read_to_file(self):
        """The coordinator's check (27 Oct): a whole run, its comment, device, crash log and email, prints counts."""
        self.known_ids([])
        self.asc_env()
        self.lists = {("6822072535", "crash"): [submission("c1", 1)], ("6821782401", "screenshot"): [submission("s1", 2)]}
        _, out, err = self.read()
        os.environ["MAY_APP_RENDERS_WRITE_KEY"] = ""
        _, out2, err2 = self.quiet(tool.store)
        os.environ.pop("MAY_APP_RENDERS_WRITE_KEY", None)
        os.environ[tool.ISSUES_TOKEN] = TOKEN
        _, out3, err3 = self.quiet(tool.file_issues, lambda *a: (201, {}))
        printed = out + err + out2 + err2 + out3 + err3
        for secret in (COMMENT, DEVICE, LOG, EMAIL, "c1", "s1", TOKEN, ISSUER, KEY_ID):
            self.assertNotIn(secret, printed)
        self.assertEqual(printed.count("\n"), 3)


class ShapeTests(unittest.TestCase):
    def workflow(self):
        return (static_check.ROOT / ".github/workflows" / static_check.FEEDBACK).read_text()

    def refused(self, old, new):
        text = self.workflow()
        self.assertIn(old, text, "mutation anchor not found: " + old[:60])
        return static_check.testflight_feedback_workflow_problems(text.replace(old, new, 1))

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.testflight_feedback_workflow_problems(self.workflow()), [])
        self.assertEqual(static_check.testflight_feedback_tool_problems(static_check.ROOT), [])

    def test_each_credential_stays_in_its_own_step(self):
        key = "          MAY_ASC_FEEDBACK_KEY_BASE64: ${{ secrets.MAY_ASC_FEEDBACK_KEY_BASE64 }}\n"
        renders = "          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}\n"
        issues = "          MAY_APP_ISSUES_TOKEN: ${{ steps.ci-issues.outputs.token || secrets.MAY_APP_ISSUES_TOKEN }}\n"
        for old, new in ((renders, renders + key), (key, key + renders), (issues, issues + key), (renders, renders + issues),
                         ("          GH_TOKEN: ${{ github.token }}\n",
                          "          GH_TOKEN: ${{ github.token }}\n" + renders), (key, "")):
            with self.subTest(new=new.strip()[:80]):
                self.assertTrue(self.refused(old, new))

    def test_triggers_permissions_and_runs_are_pinned(self):
        for old, new in (('cron: "41 * * * *"', 'cron: "*/5 * * * *"'),
                         ("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n"),
                         ("    permissions:\n      contents: read\n    steps:", "    permissions:\n      contents: write\n    steps:"),
                         ("testflight_feedback.py file\n", "testflight_feedback.py file && env\n"),
                         ("    timeout-minutes: 15\n", "    timeout-minutes: 300\n"),
                         ("        if: always()\n", ""),
                         ("    environment: ${{ needs.admission.outputs.environment }}\n", "    environment: may-ios-internal\n")):
            with self.subTest(new=new.strip()[:60]):
                self.assertTrue(self.refused(old, new))

    def test_the_mint_reaches_may_apps_issues_alone(self):
        self.assertEqual(static_check.CI_MINTS["ci-issues"], ("MAY_APP_ISSUES_TOKEN", "may-app", (("issues", "write"),)))
        self.assertTrue(self.refused("          permission-issues: write\n",
                                     "          permission-issues: write\n          permission-contents: write\n"))

    def test_the_tool_pins_its_calls_and_refuses_the_release_key(self):
        path = static_check.ROOT / "tools/may_release" / static_check.FEEDBACK_TOOL
        source = path.read_text()
        for old, new in (('("POST", r"/repos/May-Assistant/may-app/issues"))', '("POST", r"/repos/May-Assistant/may-app/.*"))'),
                         ("key_id == RELEASE_KEY_ID", "key_id == 'nothing'"),
                         ('print("May TestFlight feedback: nothing new to file")', "print(records)")):
            with self.subTest(new=new[:60]):
                self.assertIn(old, source)
                root = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                (root / "tools/may_release").mkdir(parents=True)
                (root / "tools/may_release" / static_check.FEEDBACK_TOOL).write_text(source.replace(old, new, 1))
                self.assertTrue(static_check.testflight_feedback_tool_problems(root))


if __name__ == "__main__":
    unittest.main()
