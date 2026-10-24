"""The flow board (board.py): verdicts from the checked reports, the lanes' curated text from may-app, a run history
that outlives the run folders, and a page no value can break out of."""
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

import board
from board import BoardError
import device_tests
import publish

NOW = datetime.datetime(2026, 10, 24, 3, 3, tzinfo=datetime.timezone.utc)
FLOWS = {"about": "x", "flows": [
    {"id": "2", "title": "Sign in and out", "scenarios": ["sign-in"], "components": ["SignInScreen"]},
    {"id": "3", "title": "Connect an app", "scenarios": ["connections"], "components": ["ConnectionsPage"]},
    {"id": "3.1", "title": "Gmail", "scenarios": [], "components": []},
    {"id": "4", "title": "Chat", "scenarios": ["chat", "poll"], "components": ["ChatPage"]}]}
NOTES = {"banner": {"title": "Sign-in passes", "body": "Both apps sign in."},
         "flows": {"2": {"means": "Sign in, see your chats.", "next": ["Run every flow."], "issue": 12,
                         "changed": [1178]}},
         "results": {"android/sign-in": {"headline": "Signs in", "note": "Lands on Chats.",
                                         "links": [{"text": "#1178", "url": "https://github.com/May-Assistant/may-app/pull/1178"}]}}}


def row(scenario, verdict, shot=True, video=True, reason=None):
    return {"id": scenario, "verdict": verdict, "reason": reason, "screenshots": [scenario + "-end.png"] if shot else [],
            "videos": [scenario + ".mp4"] if video else []}


def url(folder, device, video):
    return publish.asset_url(folder, device, video)


def folder(n):
    return "202610%02d-%012x" % (n % 28 + 1, n)


class InputTests(unittest.TestCase):
    def test_flows_are_checked_against_a_fixed_shape(self):
        self.assertEqual([f["id"] for f in board.checked_flows(FLOWS)], ["2", "3", "3.1", "4"])
        good = FLOWS["flows"][0]
        for change in ({"id": "two"}, {"id": "2/../x"}, {"title": ""}, {"title": "x" * 161}, {"scenarios": ["Sign In"]},
                       {"components": ["<b>"]}, {"scenarios": "sign-in"}):
            with self.subTest(change=change), self.assertRaises(BoardError):
                board.checked_flows({"flows": [dict(good, **change)]})
        for data in (None, {}, {"flows": []}, {"flows": [good, good]}):
            with self.subTest(data=data), self.assertRaises(BoardError):
                board.checked_flows(data)

    def test_notes_are_checked_and_links_go_only_to_may_assistant(self):
        self.assertEqual(board.checked_notes(NOTES)["banner"]["title"], "Sign-in passes")
        self.assertEqual(board.checked_notes({}), {"banner": None, "flows": {}, "results": {}})
        bad = ({"extra": 1}, {"banner": {"title": "x"}}, {"flows": {"two": {}}}, {"flows": {"2": {"means": 7}}},
               {"flows": {"2": {"issue": "12"}}}, {"flows": {"2": {"changed": [-1]}}},
               {"results": {"web/sign-in": {}}}, {"results": {"android/sign-in": {"colour": "red"}}},
               {"results": {"android/sign-in": {"links": [{"text": "x", "url": "https://evil.example/pull/1"}]}}},
               {"results": {"android/sign-in": {"links": [{"text": "x", "url": "javascript:alert(1)"}]}}},
               {"results": {"android/sign-in": {"links": [{"text": "x",
                                                           "url": "https://github.com/someone/may-app/pull/1"}]}}})
        for data in bad:
            with self.subTest(data=data), self.assertRaises(BoardError):
                board.checked_notes(data)


class HistoryTests(unittest.TestCase):
    def test_an_entry_takes_verdicts_from_the_reports_alone(self):
        new = board.entry(folder(1), "9", {"android": [row("sign-in", "ui-pass"), row("smoke", device_tests.SMOKE_PASS)],
                                           "ios": [row("sign-in", "fail", video=False, reason="r")]}, NOW)
        self.assertEqual(new["when"], "24 Oct, 03:03 UTC")
        self.assertEqual(new["results"], {
            "android/sign-in": {"verdict": "pass", "reason": None, "shot": "sign-in-end.png", "videos": ["sign-in.mp4"]},
            "ios/sign-in": {"verdict": "fail", "reason": "r", "shot": "sign-in-end.png", "videos": []}})

    def test_history_keeps_the_newest_runs_and_drops_malformed_entries(self):
        old = [board.entry(folder(n), str(n), {"android": [row("sign-in", "pass")]}, NOW) for n in range(70, 0, -1)]
        old.insert(3, {"folder": "../../etc", "run": "1", "when": "x", "results": {}})
        new = board.entry(folder(71), "71", {"android": [row("sign-in", "fail")]}, NOW)
        kept, removed = board.history_after(old, new)
        self.assertEqual(len(kept), board.HISTORY)
        self.assertEqual(kept[0]["run"], "71")
        self.assertNotIn("../../etc", [item["folder"] for item in kept])
        self.assertEqual(len(removed), 70 + 1 - board.HISTORY)
        again, _ = board.history_after(kept, board.entry(folder(71), "72", {"android": [row("sign-in", "pass")]}, NOW))
        self.assertEqual([item["run"] for item in again[:2]], ["72", "70"], "a re-run replaces its folder's entry")


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_folder(self, name, reports):
        for device, rows in reports.items():
            (self.base / name / device).mkdir(parents=True)
            for r in rows:
                for shot in r["screenshots"]:
                    (self.base / name / device / shot).write_bytes(b"\x89PNG")
        return board.update(self.base, name, str(int(name[-12:], 16)), reports, FLOWS, NOTES, url, 10, now=NOW)

    def data(self, name):
        page = (self.base / name / "board.html").read_text()
        blob = page.split('<script type="application/json" id="board-data">')[1].split("</script>")[0]
        return json.loads(blob), page

    def test_the_board_takes_verdicts_from_the_reports_and_notes_on_the_newest_run_alone(self):
        self.assertEqual(self.run_folder(folder(1), {"android": [row("sign-in", "pass")]}), "written")
        self.assertEqual(self.run_folder(folder(2), {"android": [row("sign-in", "ui-pass"), row("chat", "fail")],
                                                    "ios": [row("sign-in", "pass"), row("chat", "pass")]}), "written")
        data, page = self.data(folder(2))
        self.assertEqual(data["current"], folder(2))
        self.assertEqual([r["folder"] for r in data["runs"]], [folder(1), folder(2)], "oldest first: newest on the right")
        flows = {f["id"]: f for f in data["flows"]}
        self.assertEqual((flows["2"]["state"], flows["2"]["android"], flows["2"]["ios"]), ("pass", "pass", "pass"))
        self.assertEqual(flows["4"]["state"], "partial", "chat failed on Android, passed on iOS; poll did not run")
        self.assertEqual((flows["3"]["state"], flows["3.1"]["parent"]), ("none", "3"))
        self.assertEqual((flows["2"]["means"], flows["2"]["issue"], flows["2"]["changed"]),
                         ("Sign in, see your chats.", 12, [1178]))
        self.assertEqual(flows["2"]["last_pass"], "3 Oct 2026, run 2")
        newest, older = data["results"][folder(2)], data["results"][folder(1)]
        self.assertEqual(newest["android/sign-in"]["headline"], "Signs in")
        self.assertNotIn("headline", older["android/sign-in"], "curated text describes the newest run alone")
        self.assertEqual(newest["android/sign-in"]["thumb"], f"../history/{folder(2)}/android/sign-in-end.png")
        self.assertEqual(newest["android/sign-in"]["video"], url(folder(2), "android", "sign-in.mp4"))
        self.assertTrue((self.base / "history" / folder(2) / "android" / "sign-in-end.png").is_file())
        self.assertIn("Updated 24 Oct, 03:03 UTC", page)
        self.assertEqual(json.loads(page.split('id="board-banner">')[1].split("</script>")[0])["title"], "Sign-in passes")

    def test_runs_beyond_the_kept_releases_are_archived_with_verdict_and_end_screen_but_no_recording(self):
        for n in range(1, 13):
            self.run_folder(folder(n), {"android": [row("sign-in", "pass" if n % 2 else "fail")]})
        data, _ = self.data(folder(12))
        archived = [r["folder"] for r in data["runs"] if r["archived"]]
        self.assertEqual(archived, [folder(1), folder(2)], "the 10 newest keep their releases")
        oldest = data["results"][folder(1)]["android/sign-in"]
        self.assertEqual((oldest["verdict"], oldest["thumb"]), ("pass", f"../history/{folder(1)}/android/sign-in-end.png"))
        self.assertNotIn("video", oldest)
        self.assertIn("video", data["results"][folder(3)]["android/sign-in"])

    def test_screenshots_beyond_the_history_are_removed(self):
        with patch("board.HISTORY", 3):
            for n in range(1, 5):
                self.run_folder(folder(n), {"android": [row("sign-in", "pass")]})
        self.assertFalse((self.base / "history" / folder(1)).exists())
        self.assertTrue((self.base / "history" / folder(2)).exists())
        self.assertEqual(len(json.loads((self.base / "history.json").read_text())), 3)

    def test_no_value_can_break_out_of_the_page(self):
        hostile = "</script><script>alert(1)</script><!--"
        notes = {"banner": {"title": hostile, "body": hostile},
                 "flows": {"2": {"means": hostile, "next": [hostile]}},
                 "results": {"android/sign-in": {"headline": hostile, "note": hostile}}}
        flows = {"flows": [dict(FLOWS["flows"][0], title=hostile)]}
        (self.base / folder(1) / "android").mkdir(parents=True)
        board.update(self.base, folder(1), "1", {"android": [row("sign-in", "pass", shot=False)]}, flows, notes, url, 10,
                     now=NOW)
        page = (self.base / folder(1) / "board.html").read_text()
        template = board.TEMPLATE.read_text()
        self.assertNotIn(hostile, page)
        self.assertEqual(page.count("<script"), template.count("<script"))
        self.assertEqual(page.count("</script>"), template.count("</script>"))
        data = json.loads(page.split('id="board-data">')[1].split("</script>")[0])
        self.assertEqual(data["flows"][0]["title"], hostile, "the value itself survives, escaped")

    def test_missing_flows_write_no_board_and_malformed_notes_a_board_without_them(self):
        (self.base / folder(1) / "android").mkdir(parents=True)
        reports = {"android": [row("sign-in", "pass", shot=False)]}
        self.assertEqual(board.update(self.base, folder(1), "1", reports, None, NOTES, url, 10), "no flows")
        self.assertFalse((self.base / folder(1) / "board.html").exists())
        self.assertEqual(board.update(self.base, folder(1), "1", reports, FLOWS, {"banner": 7}, url, 10), "no notes")
        data = json.loads((self.base / folder(1) / "board.html").read_text()
                          .split('id="board-data">')[1].split("</script>")[0])
        self.assertIsNone(data["flows"][0]["means"])


class DeviceInputTests(unittest.TestCase):
    def test_the_device_jobs_carry_may_apps_board_files_into_the_sealed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / "src", Path(directory) / "out"
            (source / "docs/evidence").mkdir(parents=True)
            out.mkdir()
            (source / device_tests.BOARD_FLOWS).write_text(json.dumps(FLOWS))
            (source / device_tests.BOARD_NOTES).symlink_to(source / device_tests.BOARD_FLOWS)
            device_tests.board_inputs(source, out)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["flows.json"], "a link is never followed")
            (source / device_tests.BOARD_NOTES).unlink()
            (source / device_tests.BOARD_NOTES).write_text("x" * (board.MAX_INPUT + 1))
            (out / "flows.json").unlink()
            device_tests.board_inputs(source, out)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["flows.json"], "an oversized file is left out")
        self.assertTrue(publish.DEVICE_FILE.fullmatch("flows.json"))
        self.assertTrue(publish.DEVICE_FILE.fullmatch("board-notes.json"))


if __name__ == "__main__":
    unittest.main()
