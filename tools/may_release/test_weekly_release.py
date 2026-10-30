"""May's weekly store release (weekly-release.yml, weekly_release.py; 30 Oct). Nothing here reaches
the network: every GitHub read and the one dispatch are fakes."""

import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import static_check
import weekly_release as tool
from android_ci_schedule import ScheduleError

RUN = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule"}
CUT, OLD, NEW = "c" * 40, "a" * 40, "e" * 40
UTC = datetime.timezone.utc
THURSDAY = datetime.datetime(2026, 11, 5, 15, 7, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def store_run(platform, build, sha=None, dry=False, sign=False, status="completed", conclusion="success"):
    name = f"May Playground · {platform} · build {build}" + (f" at {sha}" if sha else "") + \
        (" (dry run)" if dry else "") + (" (sign only)" if sign else "")
    return {"display_title": name, "status": status, "conclusion": conclusion}


def runs_get(runs):
    def get(path, token):
        assert path.startswith(tool.RUNS_PATH + "?") and token == "t"
        page = int(path.rsplit("page=", 1)[1])
        return 200, json.dumps({"workflow_runs": runs if page == 1 else []}).encode()
    return get


def merged(number, sha, when, base="main", repo="May-Assistant/may-app"):
    return {"number": number, "merge_commit_sha": sha, "merged_at": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
            "base": {"ref": base, "repo": {"full_name": repo}}, "title": "PRIVATE-TITLE-7f3c"}


def pulls_get(pulls):
    def get(path, token):
        assert path.startswith("pulls?state=closed&base=main") and token == "t"
        page = int(path.rsplit("page=", 1)[1])
        return 200, json.dumps(pulls if page == 1 else []).encode()
    return get


class CutTests(unittest.TestCase):
    def test_the_cut_is_the_latest_thursday_1507_utc(self):
        for now, expected in ((at(5, 15, 7), at(5, 15, 7)), (at(5, 15, 6), at(5, 15, 7) - datetime.timedelta(days=7)),
                              (at(6, 23, 7), at(5, 15, 7)), (at(4, 9), at(5, 15, 7) - datetime.timedelta(days=7)),
                              (at(12, 16), at(12, 15, 7))):
            with self.subTest(now=now):
                self.assertEqual(tool.cut_time(now), expected)
        self.assertEqual(at(5, 15, 7).weekday(), 3, "5 Nov 2026 is a Thursday")

    def test_the_cut_is_the_last_merge_into_may_app_main_at_or_before_it(self):
        pulls = [merged(9, NEW, THURSDAY + datetime.timedelta(minutes=1)),
                 merged(8, CUT, THURSDAY),
                 merged(7, OLD, THURSDAY - datetime.timedelta(days=1)),
                 merged(6, "b" * 40, THURSDAY - datetime.timedelta(hours=1), base="release"),
                 merged(5, "d" * 40, THURSDAY - datetime.timedelta(hours=2), repo="someone/may-app"),
                 merged(4, "f" * 40, None)]
        self.assertEqual(tool.cut_sha("t", THURSDAY, pulls_get(pulls)), CUT)
        self.assertIsNone(tool.cut_sha("t", THURSDAY, pulls_get([merged(9, NEW, THURSDAY + datetime.timedelta(hours=1))])))
        with self.assertRaises(ScheduleError):
            tool.cut_sha("t", THURSDAY, lambda path, token: (500, b""))


class DecideTests(unittest.TestCase):
    def runs(self, *entries):
        busy, runs = tool.store_runs("t", runs_get(list(entries)))
        return busy, runs

    def test_store_run_names_are_read_with_their_sha_and_mode(self):
        busy, runs = self.runs(store_run("iOS", 14, CUT), store_run("Android", 9, dry=True),
                               store_run("iOS", 12, sign=True), {"display_title": "May · iOS · build 40 at " + CUT},
                               store_run("iOS", 15, NEW, status="waiting"))
        self.assertTrue(busy)
        self.assertEqual(runs[0], {"platform": "ios", "build": 14, "sha": CUT, "dry": False, "done": True,
                                   "succeeded": True})
        self.assertEqual([(r["platform"], r["build"], r["sha"], r["dry"]) for r in runs],
                         [("ios", 14, CUT, False), ("android", 9, None, True), ("ios", 12, None, False),
                          ("ios", 15, NEW, False)], "May production's runs are not the weekly app's")

    def test_ios_first_then_android_once_ios_succeeded_then_nothing(self):
        cases = (
            ("nothing built: iOS, one above every iOS build", [store_run("iOS", 14, OLD), store_run("iOS", 16, dry=True),
                                                                store_run("Android", 30, OLD)], ("ios", 17)),
            ("iOS built: Android", [store_run("iOS", 17, CUT), store_run("Android", 30, OLD)], ("android", 31)),
            ("iOS failed: nothing more", [store_run("iOS", 17, CUT, conclusion="failure")], (None, "ios failed")),
            ("iOS cancelled: nothing more", [store_run("iOS", 17, CUT, conclusion="cancelled")], (None, "ios failed")),
            ("both built: nothing", [store_run("Android", 31, CUT), store_run("iOS", 17, CUT)], (None, "done")),
            ("a dry run of the cut is no build", [store_run("iOS", 17, CUT, dry=True)], ("ios", 18)),
            ("a run pending: nothing", [store_run("iOS", 17, OLD, status="waiting", conclusion=None)], (None, "busy")))
        for label, runs, expected in cases:
            with self.subTest(label):
                busy, read = self.runs(*runs)
                self.assertEqual(tool.decide(busy, read, CUT), expected)
        self.assertEqual(tool.decide(False, [], None), (None, "no cut"))
        self.assertEqual(tool.decide(False, [], CUT), ("ios", 1))

    def test_main_unchanged_since_last_week_dispatches_nothing(self):
        busy, runs = self.runs(store_run("Android", 31, CUT), store_run("iOS", 17, CUT))
        self.assertEqual(tool.decide(busy, runs, CUT), (None, "done"), "next week's cut is the same merge")


class DispatchTests(unittest.TestCase):
    def test_only_store_release_draft_with_the_weekly_inputs_is_sent(self):
        sent = []

        class Reply:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            sent.append((request.full_url, request.get_method(), json.loads(request.data)))
            return Reply()

        payload = tool.dispatch_payload("ios", CUT, 17)
        self.assertEqual(tool.control_dispatch(tool.DISPATCH_PATH, "t", payload, opener), 204)
        self.assertEqual(sent, [("https://api.github.com/repos/May-Assistant/may-releases/actions/workflows/"
                                 "store-release-draft.yml/dispatches", "POST",
                                 {"ref": "main", "inputs": {"platform": "ios", "target": "playground", "source_sha": CUT,
                                                            "build_number": "17", "dry_run": "false",
                                                            "sign_only": "false"}})])
        for label, path, change in (("another workflow", "actions/workflows/x.yml/dispatches", {}),
                                    ("production", tool.DISPATCH_PATH, {"target": "production"}),
                                    ("a dry run", tool.DISPATCH_PATH, {"dry_run": "true"}),
                                    ("sign only", tool.DISPATCH_PATH, {"sign_only": "true"}),
                                    ("a short sha", tool.DISPATCH_PATH, {"source_sha": "abc"}),
                                    ("an extra input", tool.DISPATCH_PATH, {"extra": "x"})):
            with self.subTest(label), self.assertRaises(ScheduleError):
                tool.control_dispatch(path, "t", {"ref": "main", "inputs": {**payload["inputs"], **change}},
                                      lambda *a, **k: self.fail("sent"))

    def test_the_steps_carry_numbers_and_the_cut_alone_and_print_no_title(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {**RUN, "RUNNER_TEMP": temp, "GH_TOKEN": "t", tool.READ_TOKEN: "t"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            tool.runs_step(runs_get([store_run("iOS", 14, OLD), store_run("Android", 30, OLD)]))
            tool.pick_step(pulls_get([merged(8, CUT, THURSDAY)]), now=THURSDAY + datetime.timedelta(hours=2))
            self.assertNotIn(tool.READ_TOKEN, os.environ, "the read token is popped")
            posted = []
            tool.dispatch_step(lambda path, token, payload: posted.append(payload) or 204)
            carried = {p.name: json.loads(p.read_text()) for p in (Path(temp) / tool.STATE).iterdir()}
        self.assertEqual(posted[0]["inputs"]["platform"], "ios")
        self.assertEqual(posted[0]["inputs"]["build_number"], "15")
        self.assertEqual(carried["cut.json"], {"cut": CUT})
        self.assertNotIn("PRIVATE-TITLE", out.getvalue() + json.dumps(carried))
        self.assertEqual(out.getvalue().splitlines(), [
            "May weekly release: 2 May Playground store runs read",
            f"May weekly release: this week's cut of may-app main is {CUT}",
            f"May weekly release: dispatched May Playground iOS build 15 at {CUT}; it waits for approval"])

    def test_nothing_is_sent_when_nothing_is_due(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {**RUN, "RUNNER_TEMP": temp, "GH_TOKEN": "t", tool.READ_TOKEN: "t"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            tool.runs_step(runs_get([store_run("iOS", 14, CUT, status="waiting", conclusion=None)]))
            tool.pick_step(pulls_get([merged(8, CUT, THURSDAY)]), now=THURSDAY)
            tool.dispatch_step(lambda *a: self.fail("dispatched"))
        self.assertIn("a store run is queued, waiting for approval or running; nothing dispatched", out.getvalue())


class WorkflowTests(unittest.TestCase):
    def test_the_workflow_and_tool_are_pinned_and_the_store_run_name_carries_the_sha(self):
        text = (static_check.ROOT / ".github/workflows/weekly-release.yml").read_text()
        self.assertEqual(static_check.weekly_release_workflow_problems(text), [])
        self.assertEqual(static_check.weekly_release_tool_problems(static_check.ROOT), [])
        store = (static_check.ROOT / ".github/workflows/store-release-draft.yml").read_text()
        self.assertIn(" at ${{ inputs.source_sha }}", store.partition("\non:")[0])
        self.assertEqual(static_check.layout_problems(store), [])
        self.assertIn("store-release-draft.yml: run-name is not exactly the app, platform, build number, source SHA and mode "
                      "from its inputs",
                      static_check.layout_problems(store.replace(" at ${{ inputs.source_sha }}", "", 1)))
        for old, new in (('    - cron: "7 * * * 4,5"', '    - cron: "*/15 * * * *"'),
                         ("  group: may-weekly-release\n", "  group: may-store-release\n"),
                         ("run: python3 -I -S -B tools/may_release/weekly_release.py dispatch\n",
                          "run: python3 -I -S -B tools/may_release/weekly_release.py runs\n")):
            with self.subTest(new=new):
                self.assertEqual(text.count(old), 1, old)
                self.assertNotEqual(static_check.weekly_release_workflow_problems(text.replace(old, new)), [])

    def test_the_target_is_one_constant_and_may_playground(self):
        self.assertEqual(tool.TARGET, "playground")
        self.assertEqual((tool.CUT_WEEKDAY, tool.CUT_HOUR, tool.CUT_MINUTE), (3, 15, 7))


if __name__ == "__main__":
    unittest.main()
