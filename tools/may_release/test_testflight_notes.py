"""testflight_notes.py: the TestFlight "What to test" notes composed from may-app's pull-request titles."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import static_check
import testflight_notes as notes
from store import StoreError

SHA, OLD, OLDER = "c" * 40, "a" * 40, "b" * 40


def run(name, conclusion="success", status="completed"):
    return {"display_title": name, "status": status, "conclusion": conclusion}


class PreviousBuildTests(unittest.TestCase):
    def test_the_newest_successful_upload_of_the_same_app_before_this_build(self):
        runs = [run(f"May Playground · iOS · build 12 at {OLDER}"),
                run(f"May Playground · iOS · build 14 at {OLD}"),
                run(f"May Playground · iOS · build 15 at {OLDER} (dry run)"),
                run(f"May Playground · iOS · build 16 at {OLDER}", conclusion="failure"),
                run(f"May · iOS · build 20 at {OLDER}"),
                run(f"May Playground · Android · build 30 at {OLDER}"),
                run(f"May Playground · iOS · build 13 at {OLDER} (sign only)"),
                run(f"May Playground · iOS · build 18 at {OLDER}"),
                run("May Playground · iOS · build 11")]
        self.assertEqual(notes.previous_build(runs, "playground", 17, SHA), OLD)
        self.assertEqual(notes.previous_build(runs, "production", 21, SHA), OLDER)
        self.assertIsNone(notes.previous_build(runs, "production", 20, SHA), "not this build itself")
        self.assertIsNone(notes.previous_build([run(f"May Playground · iOS · build 14 at {SHA}")], "playground", 17,
                                               SHA), "the same commit is not a previous build")
        self.assertIsNone(notes.previous_build([], "playground", 17, SHA))


class ComposeTests(unittest.TestCase):
    def test_titles_are_grouped_by_area_as_try_lines_without_their_numbers(self):
        text = notes.compose(["Brief: needs_you_count, everything waiting on the person (173) (#1503)",
                              "Agenda: the soft edge under the pinned week strip (#1492)",
                              "Brief: a second change (#1510)",
                              "A title with no area at all (#1499)",
                              "Bad\x07control: kept\nsafe (#1)"])
        self.assertEqual(text.splitlines(), [
            "Try: Brief: needs_you_count, everything waiting on the person (173)",
            "Try: Brief: a second change",
            "Try: Agenda: the soft edge under the pinned week strip",
            "Try: Other: A title with no area at all",
            "Try: Bad control: kept safe"])

    def test_nothing_new_says_so(self):
        self.assertEqual(notes.compose([]), notes.NOTHING_NEW)
        self.assertEqual(notes.compose(["  ", "(#3)"]), notes.NOTHING_NEW)

    def test_long_notes_keep_whole_lines_under_the_limit_and_count_the_rest(self):
        titles = [f"Area {i % 3}: change number {i} " + "x" * 80 for i in range(100)]
        text = notes.compose(titles)
        self.assertLessEqual(len(text), notes.NOTES_LIMIT)
        lines = text.splitlines()
        self.assertRegex(lines[-1], r"^…and \d+ more$")
        self.assertEqual(len(lines) - 1 + int(lines[-1].split()[1]), 100, "every title is shown or counted")
        self.assertTrue(all(line.startswith("Try: Area ") for line in lines[:-1]))


class Root(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "may-source").mkdir()
        (self.root / "may-private").mkdir(mode=0o700)
        env = patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def quiet(self, function, *args, **kwargs):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            function(*args, **kwargs)
        return out.getvalue()

    def passes(self, function, *args):
        try:
            return self.quiet(function, *args)
        except StoreError as refused:
            self.fail("refused: " + refused.args[0])


class ComposeStepTests(Root):
    SUBJECTS = ["Brief: PRIVATE-TITLE-a1 [skip ci] (#1503)", "Agenda: PRIVATE-TITLE-b2 (#1492)"]

    def runs_get(self, runs):
        def get(path, token):
            self.assertTrue(path.startswith(notes.RUNS_PATH + "?") and token == "t")
            return 200, json.dumps({"workflow_runs": runs}).encode()
        return get

    def git(self, ancestor=True, subjects=None):
        self.calls = []

        def run(argv, source):
            self.calls.append(argv)
            self.assertEqual(source, self.root / "may-source")
            if argv[0] == "merge-base":
                return (0 if ancestor else 1), ""
            return 0, "\n".join(subjects or self.SUBJECTS) + "\n"
        return run

    def test_titles_since_the_previous_build_are_sealed_privately_and_only_counted(self):
        os.environ["GH_TOKEN"] = "t"
        out = self.quiet(notes.compose_step, "playground", SHA, "17",
                         self.runs_get([run(f"May Playground · iOS · build 14 at {OLD}")]), self.git())
        self.assertEqual(self.calls[1], ["log", "--first-parent", "--format=%s", f"{OLD}..{SHA}", "-n", "200"])
        written = self.root / "may-private" / notes.NOTES_FILE
        self.assertEqual(written.read_text(), "Try: Brief: PRIVATE-TITLE-a1\nTry: Agenda: PRIVATE-TITLE-b2")
        self.assertEqual(oct(written.stat().st_mode & 0o777), "0o600")
        self.assertEqual(out, "May TestFlight notes: 2 pull requests since the previous build, sealed with the build\n")
        self.assertNotIn("PRIVATE-TITLE", out)

    def test_no_previous_build_or_a_diverged_one_takes_the_newest_twenty(self):
        os.environ["GH_TOKEN"] = "t"
        for runs, ancestor in (([], True), ([run(f"May Playground · iOS · build 14 at {OLD}")], False)):
            with self.subTest(runs=len(runs)):
                out = self.quiet(notes.compose_step, "playground", SHA, "17", self.runs_get(runs), self.git(ancestor))
                self.assertEqual(self.calls[-1], ["log", "--first-parent", "--format=%s", SHA, "-n", "20"])
                self.assertIn("no earlier build to compare with; the newest 2 pull requests", out)

    def test_trailers_and_skip_marks_never_reach_testers(self):
        self.assertEqual(notes.cleaned("Agenda: fix [skip ci] the strip  Co-Authored-By: X <x@y>"), "Agenda: fix the strip")
        self.assertEqual(notes.cleaned("One line\nsecond line"), "One line")
        self.assertEqual(notes.cleaned(""), "")

    def test_no_token_or_no_source_or_another_steps_secret_is_refused(self):
        with self.assertRaises(StoreError):
            notes.compose_step("playground", SHA, "17", self.runs_get([]), self.git())
        os.environ["GH_TOKEN"] = "t"
        with patch.dict(os.environ, {"MAY_ASC_KEY_BASE64": "x"}), self.assertRaises(StoreError):
            notes.compose_step("playground", SHA, "17", self.runs_get([]), self.git())
        (self.root / "may-source").rmdir()
        with self.assertRaises(StoreError):
            notes.compose_step("playground", SHA, "17", self.runs_get([]), self.git())


class SendStepTests(Root):
    NOTES = "Try: Brief: PRIVATE-TITLE-a1\nTry: Agenda: PRIVATE-TITLE-b2"

    def setUp(self):
        super().setUp()
        os.environ.update({"MAY_DRY_RUN": "false", "MAY_ASC_KEY_ID": notes.APPLE_KEY_ID,
                           "MAY_ASC_ISSUER_ID": "12345678-1234-1234-1234-123456789012",
                           "MAY_ASC_KEY_BASE64": "LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tCg=="})
        (self.root / "may-private" / notes.NOTES_FILE).write_text(self.NOTES)
        token = patch("testflight_notes.asc_token", return_value="jwt")
        token.start()
        self.addCleanup(token.stop)
        self.sent = []

    def get(self, builds=(200, {"data": [{"id": "B1"}]}), localizations=(200, {"data": []})):
        def get(token, url):
            self.assertEqual(token, "jwt")
            return builds if "/builds?" in url else localizations
        return get

    def send(self, status=201):
        def send(token, method, url, payload):
            self.sent.append((method, url, payload))
            return status, None
        return send

    def test_new_notes_are_posted_for_the_build_and_only_counted(self):
        out = self.quiet(notes.send_step, "playground", SHA, "17", self.get(), self.send(), lambda s: None, lambda: 0)
        method, url, payload = self.sent[0]
        self.assertEqual((method, url), ("POST", notes.ASC_API + "betaBuildLocalizations"))
        self.assertEqual(payload["data"]["attributes"], {"locale": "en-US", "whatsNew": self.NOTES})
        self.assertEqual(payload["data"]["relationships"]["build"]["data"], {"type": "builds", "id": "B1"})
        self.assertEqual(out, "May TestFlight notes: What to test set on build 17 (2 lines)\n")

    def test_existing_english_notes_are_replaced(self):
        found = (200, {"data": [{"id": "L9", "attributes": {"locale": "en-US"}}]})
        self.quiet(notes.send_step, "playground", SHA, "17", self.get(localizations=found), self.send(200),
                   lambda s: None, lambda: 0)
        self.assertEqual(self.sent[0][:2], ("PATCH", notes.ASC_API + "betaBuildLocalizations/L9"))

    def test_no_write_access_skips_with_one_line_and_other_refusals_fail(self):
        out = self.passes(notes.send_step, "playground", SHA, "17", self.get(), self.send(403), lambda s: None, lambda: 0)
        self.assertIn("no write access", out)
        with self.assertRaises(StoreError):
            self.quiet(notes.send_step, "playground", SHA, "17", self.get(), self.send(409), lambda s: None, lambda: 0)

    def test_the_build_is_waited_for_then_given_up(self):
        clock = iter(range(0, 10 ** 6, 60))
        with self.assertRaises(StoreError):
            self.quiet(notes.send_step, "playground", SHA, "17", self.get(builds=(200, {"data": []})), self.send(),
                       lambda s: None, lambda: next(clock))
        self.assertEqual(self.sent, [])

    def test_a_dry_run_or_missing_notes_send_nothing(self):
        with patch.dict(os.environ, {"MAY_DRY_RUN": "true"}), self.assertRaises(StoreError):
            notes.send_step("playground", SHA, "17", self.get(), self.send())
        (self.root / "may-private" / notes.NOTES_FILE).unlink()
        with self.assertRaises(StoreError):
            notes.send_step("playground", SHA, "17", self.get(), self.send())
        self.assertEqual(self.sent, [])

    def test_only_a_beta_localization_is_ever_written(self):
        for method, url in (("POST", notes.ASC_API + "builds"), ("DELETE", notes.ASC_API + "betaBuildLocalizations/L"),
                            ("PATCH", notes.ASC_API + "betaBuildLocalizations/../apps"),
                            ("POST", "https://example.com/betaBuildLocalizations")):
            with self.subTest(method=method, url=url), self.assertRaises(StoreError):
                notes.asc_send("jwt", method, url, {}, opener=self)

    def open(self, request, timeout=None):
        raise AssertionError("a write outside a beta localization was sent")

    def test_no_title_reaches_the_log_on_any_path(self):
        outs = [self.passes(notes.send_step, "playground", SHA, "17", self.get(), self.send(code), lambda s: None,
                            lambda: 0) for code in (201, 403)]
        self.assertNotIn("PRIVATE-TITLE", "".join(outs))


class WorkflowTests(unittest.TestCase):
    def test_compose_runs_in_the_build_and_send_only_after_a_successful_upload(self):
        store = (static_check.ROOT / ".github/workflows/store-release-draft.yml").read_text()
        self.assertEqual(static_check.store_problems(store), [])
        self.assertEqual(static_check.testflight_notes_problems(static_check.ROOT), [])
        condition = "        if: ${{ !cancelled() && steps.testflight_upload.outcome == 'success' }}\n"
        compose = "      - name: " + static_check.NOTES_COMPOSE + "\n"
        for old, new in ((condition, "        if: ${{ !cancelled() }}\n"),
                         (condition, ""),
                         (compose, "      - name: Compose them\n"),
                         ("        run: python3 -I -S -B tools/may_release/testflight_notes.py send",
                          "        run: python3 -I -S -B tools/may_release/testflight_notes.py compose"),
                         ("      - name: Check the checkout is exactly its commit before setting the notes\n"
                          "        run: python3 -I -S -B tools/may_release/integrity.py\n", ""),
                         ("          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B tools/may_release/"
                          "testflight_notes.py compose",
                          "          GH_TOKEN: ${{ github.token }}\n          MAY_ASC_ISSUER_ID: "
                          "${{ secrets.MAY_ASC_ISSUER_ID }}\n        run: python3 -I -S -B tools/may_release/"
                          "testflight_notes.py compose")):
            with self.subTest(new=new.strip()[:60]):
                self.assertEqual(store.count(old), 1, old)
                self.assertNotEqual(static_check.store_problems(store.replace(old, new)), [])


if __name__ == "__main__":
    unittest.main()
