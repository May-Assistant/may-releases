"""May's iOS core suite on a schedule (ios-core-schedule.yml, ios_core_schedule.py)."""

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

import android_ci_schedule
import ios_core_schedule as tool
from ios_core_schedule import ScheduleError, control_dispatch, dispatch_payload, pick, recent_runs, touches
from gate import GateDenial
import static_check
import test_android_ci_schedule
from test_android_ci_schedule import FakeApp, pull, runs_page

SHA, OTHER, THIRD = "a" * 40, "b" * 40, "c" * 40
TOKEN = "github_pat_" + "E" * 50
WORKFLOW = ".github/workflows/ios-core-schedule.yml"
LANE = ".github/workflows/ios-core-pr.yml"
TOOL = "tools/may_release/ios_core_schedule.py"
PRIVATE = "PRIVATE-MAY-APP-NAME-7c2e"
NOW = datetime.datetime(2026, 10, 24, 13, 0, tzinfo=datetime.timezone.utc)
RUN = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule"}


def run(number, sha, status="completed"):
    return {"display_title": f"May iOS core suite for may-app #{number} at {sha}", "status": status}


def updated(days_ago=0):
    return (NOW - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunTests(unittest.TestCase):
    def test_it_reuses_the_android_schedules_run_check_and_rules(self):
        self.assertIs(tool.check_run, android_ci_schedule.check_run)
        self.assertIs(tool.app_get, android_ci_schedule.app_get)
        self.assertEqual((tool.WINDOW, tool.STALE, tool.MAX_LOOKED), (android_ci_schedule.WINDOW, android_ci_schedule.STALE, 40))
        self.assertEqual(tool.ENVIRONMENT, "may-native-compile-pr")

    def test_admission_checks_the_environment_has_no_reviewer_and_main_only(self):
        policy = {"can_admins_bypass": False, "protection_rules": [],
                  "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}
        branches = {"branch_policies": [{"name": "main"}]}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            with patch.dict(os.environ, {**RUN, "GH_TOKEN": TOKEN, "GITHUB_OUTPUT": str(output)}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                tool.admit(lambda path, token: branches if "deployment-branch-policies" in path else policy)
            self.assertEqual(output.read_text(), "environment=may-native-compile-pr\n")
            self.assertEqual(out.getvalue(), "May iOS core schedule admission passed\n")
            reviewer = {**policy, "protection_rules": [{"type": "required_reviewers"}]}
            with patch.dict(os.environ, {**RUN, "GH_TOKEN": TOKEN, "GITHUB_OUTPUT": str(output)}):
                with self.assertRaises(GateDenial):
                    tool.admit(lambda path, token: branches if "deployment-branch-policies" in path else reviewer)
            with patch.dict(os.environ, {**RUN, "GITHUB_EVENT_NAME": "pull_request", "GH_TOKEN": TOKEN}):
                with self.assertRaises(GateDenial):
                    tool.admit(lambda path, token: branches if "deployment-branch-policies" in path else policy)


class RecentRunTests(unittest.TestCase):
    def test_heads_run_in_the_window_are_read_from_the_core_run_name_only(self):
        seen = []

        def get(path, token):
            seen.append(path)
            return runs_page(run(7, SHA), {"display_title": f"May Android unit suites for may-app #8 at {OTHER}",
                                           "status": "completed"},
                             {"display_title": PRIVATE, "status": "completed"})(path, token)
        busy, tested = recent_runs(TOKEN, NOW, get)
        self.assertFalse(busy)
        self.assertEqual(tested, {(7, SHA)}, "an Android run's head is not a core run")
        self.assertEqual(seen, ["actions/workflows/ios-core-pr.yml/runs?created=%3E%3D2026-09-24T13%3A00%3A00Z"
                                "&per_page=100&page=1"])

    def test_a_queued_or_running_core_run_makes_the_schedule_wait(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            with self.subTest(status=status):
                self.assertTrue(recent_runs(TOKEN, NOW, runs_page(run(7, SHA), run(8, OTHER, status)))[0])

    def test_an_unreadable_run_list_stops_the_run(self):
        with self.assertRaises(ScheduleError):
            recent_runs(TOKEN, NOW, lambda path, token: (500, b""))


class PickTests(unittest.TestCase):
    def test_a_head_touches_maycore_by_its_package_and_what_its_tests_read_only(self):
        for path in ("ios/MayCore/Sources/MayCore/Chat.swift", "ios/MayCore/Package.swift", "contracts/openapi.yaml",
                     "shared/test-vectors/chat/1.json", "ios/App/Info.plist", "ios/project.yml"):
            with self.subTest(path=path):
                self.assertTrue(touches([{"filename": path}]))
        self.assertTrue(touches([{"filename": "docs/x.md", "previous_filename": "ios/MayCore/x.swift"}]),
                        "a rename out of ios/MayCore/")
        for path in ("ios/MayCoreX/y.swift", "ios/Playground/x.swift", "ios/project.yml.bak", "shared/strings.json",
                     "backend/src/x.ts", "android/x.kt", "tools/release/ios.sh", "ios/Package.resolved"):
            with self.subTest(path=path):
                self.assertFalse(touches([{"filename": path}]))
        self.assertFalse(touches(["ios/MayCore/x", None, {"filename": 3}]))

    def test_the_oldest_untested_same_repository_head_touching_maycore_is_picked(self):
        app = FakeApp([pull(1, SHA, head_repo="someone/may-app", updated=updated()), pull(2, OTHER, base="claude/x", updated=updated()),
                       pull(3, THIRD, updated=updated()), pull(4, SHA, updated=updated()), pull(5, OTHER, updated=updated())],
                      {1: ["ios/MayCore/x"], 2: ["ios/MayCore/x"], 3: ["ios/MayCore/x"], 4: ["android/x.kt"], 5: ["ios/MayCore/x"]})
        self.assertEqual(pick(TOKEN, {(3, THIRD)}, app, NOW), (5, OTHER, 2, 1))
        self.assertEqual([p.split("?")[0] for p in app.paths[1:]], ["pulls/4/files", "pulls/5/files"])

    def test_a_stale_pull_request_is_passed_over_and_forty_heads_at_most_are_read(self):
        app = FakeApp([pull(1, SHA, updated=updated(15)), pull(2, OTHER, updated=updated(1))], {1: ["ios/MayCore/x"], 2: ["ios/MayCore/x"]})
        self.assertEqual(pick(TOKEN, set(), app, NOW)[:2], (2, OTHER))
        app = FakeApp([pull(n, SHA, updated=updated()) for n in range(1, 60)], {n: ["docs/x.md"] for n in range(1, 60)})
        self.assertEqual(pick(TOKEN, set(), app, NOW), (None, None, 40, 0))


class WriteTests(unittest.TestCase):
    Opener = test_android_ci_schedule.WriteTests.Opener

    def test_the_dispatch_posts_exactly_main_a_number_and_a_head_to_the_core_lane(self):
        opener = self.Opener()
        self.assertEqual(control_dispatch(tool.DISPATCH_PATH, TOKEN, dispatch_payload(42, SHA), opener), 204)
        [request] = opener.requests
        self.assertEqual((request.get_method(), request.full_url),
                         ("POST", "https://api.github.com/repos/May-Assistant/may-releases/actions/workflows/ios-core-pr.yml/dispatches"))
        self.assertEqual(json.loads(request.data), {"ref": "main", "inputs": {"pr_number": "42", "source_sha": SHA}})

    def test_any_other_write_is_refused_before_anything_is_sent(self):
        payload = dispatch_payload(42, SHA)
        paths = [android_ci_schedule.DISPATCH_PATH, "actions/workflows/store-release-draft.yml/dispatches",
                 "actions/runs/1/cancel", "actions/workflows/ios-core-pr.yml/disable",
                 "actions/workflows/ios-core-pr.yml/dispatches/"]
        payloads = [{**payload, "ref": "claude/x"}, {**payload, "inputs": {**payload["inputs"], "core_breakdown": "true"}},
                    {**payload, "inputs": {"pr_number": 42, "source_sha": SHA}}, {"inputs": payload["inputs"]}]
        for case in [(p, payload) for p in paths] + [(tool.DISPATCH_PATH, p) for p in payloads]:
            opener = self.Opener()
            with self.subTest(case=case), self.assertRaises(ScheduleError):
                control_dispatch(case[0], TOKEN, case[1], opener)
            self.assertEqual(opener.requests, [])

    def test_reads_reach_only_the_core_runs(self):
        for path in (android_ci_schedule.RUNS_PATH + "?x", "actions/runs?x=1", "environments/x"):
            with self.subTest(path=path), self.assertRaises(ScheduleError):
                tool.control_get(path, TOKEN)


class StepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {**RUN, "RUNNER_TEMP": self.temp.name, "GH_TOKEN": TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def steps(self, runs, app, status=204):
        posted, out = [], io.StringIO()

        def post(path, token, payload):
            posted.append((path, token, payload))
            return status
        with contextlib.redirect_stdout(out):
            tool.runs_step(NOW, runs)
            with patch.dict(os.environ, {"MAY_APP_PR_READ_TOKEN": TOKEN}):
                del os.environ["GH_TOKEN"]
                tool.pick_step(app, NOW)
                self.assertFalse("MAY_APP_PR_READ_TOKEN" in os.environ, "the read token leaves the environment at once")
                os.environ["GH_TOKEN"] = TOKEN
            tool.dispatch_step(post)
        return posted, out.getvalue()

    def test_one_head_is_picked_and_dispatched_and_nothing_from_may_app_is_printed(self):
        app = FakeApp([pull(3, SHA, updated=updated()), pull(4, OTHER, updated=updated())],
                      {3: [{"filename": "android/" + PRIVATE + ".kt"}], 4: ["ios/MayCore/" + PRIVATE + ".swift"]})
        posted, out = self.steps(runs_page(run(9, THIRD)), app)
        self.assertEqual(posted, [(tool.DISPATCH_PATH, TOKEN, {"ref": "main", "inputs": {"pr_number": "4", "source_sha": OTHER}})])
        self.assertEqual(out.splitlines(), [
            "May iOS core schedule: 1 heads had a core run in the last 30 days",
            f"May iOS core schedule: picked may-app #4 at {OTHER} (2 heads looked at)",
            f"May iOS core schedule: dispatched the iOS core suite for may-app #4 at {OTHER}"])
        self.assertNotIn(PRIVATE, out)
        self.assertTrue((Path(self.temp.name) / "may-ios-core-ci").is_dir(), "its own carried-state folder")

    def test_a_busy_core_lane_reads_no_pull_request_and_dispatches_nothing(self):
        app = FakeApp([pull(4, OTHER, updated=updated())], {4: ["ios/MayCore/x"]})
        posted, out = self.steps(runs_page(run(9, THIRD, "in_progress")), app)
        self.assertEqual((posted, app.paths), ([], []))
        self.assertIn("nothing to dispatch", out)

    def test_nothing_to_run_dispatches_nothing(self):
        posted, out = self.steps(runs_page(run(4, OTHER)), FakeApp([pull(4, OTHER, updated=updated())], {4: ["ios/MayCore/x"]}))
        self.assertEqual(posted, [])
        self.assertIn("no head to run (0 heads looked at, none touching MayCore)", out)

    def test_a_refused_dispatch_fails_the_run(self):
        with self.assertRaises(ScheduleError):
            self.steps(runs_page(), FakeApp([pull(4, OTHER, updated=updated())], {4: ["ios/MayCore/x"]}), status=403)

    def test_a_tampered_pick_is_not_dispatched(self):
        tool.state(fresh=True)
        for value in ({"number": 4, "sha": OTHER, "ref": "x"}, {"number": "4", "sha": OTHER}, [4, OTHER]):
            with self.subTest(value=value):
                tool.write_state(tool.PICK_FILE, value)
                with self.assertRaises(ScheduleError):
                    tool.dispatch_step(lambda *a: self.fail("dispatched"))


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

    def test_the_committed_schedule_passes(self):
        self.assertEqual(static_check.ios_core_schedule_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.ios_core_schedule_tool_problems(static_check.ROOT), [])

    def test_the_schedule_is_held_to_the_android_schedules_rules(self):
        name = "ios-core-schedule.yml: "
        secret = name + "a step holds a credential other than"
        read = "          MAY_APP_PR_READ_TOKEN: ${{ steps.ci-read.outputs.token || secrets.MAY_APP_PR_READ_TOKEN }}\n"
        token = "          GH_TOKEN: ${{ github.token }}\n"
        cases = {
            "GITHUB_TOKEN beside the read token": (read, read + token, secret),
            "contents: write": ("      contents: read\n      actions: write\n", "      contents: write\n      actions: write\n",
                                name + "permissions are not read-only but for actions: write in pick alone"),
            "a pull_request trigger": ("  workflow_dispatch:\npermissions:", "  workflow_dispatch:\n  pull_request:\npermissions:",
                                       name + "is not scheduled every 15 minutes and dispatched with no input, only"),
            "the Android schedule's tool": ("tools/may_release/ios_core_schedule.py pick", "tools/may_release/android_ci_schedule.py pick",
                                            name + "runs something other than ios_core_schedule.py's admit"),
            "the Android schedule's group": ("  group: may-ios-core-schedule\n", "  group: may-android-ci-schedule\n",
                                             name + "does not run one at a time"),
            "the Android schedule's carried folder": ('"$RUNNER_TEMP/may-ios-core-ci"', '"$RUNNER_TEMP/may-android-ci"',
                                                      name + "runs something other than"),
            "another environment": ("    environment: ${{ needs.admission.outputs.environment }}\n", "    environment: may-android-internal\n",
                                    name + "pick's environment is not the one admission chose"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_lane_run_name_is_the_number_and_head_the_schedule_reads(self):
        line = "run-name: 'May iOS core suite for may-app #${{ inputs.pr_number }} at ${{ inputs.source_sha }}'\n"
        self.flagged(LANE, line, line.replace("#${{ inputs.pr_number }} at ${{ inputs.source_sha }}",
                                              "#${{ inputs.source_sha }} at ${{ inputs.pr_number }}"),
                     "ios-core-pr.yml: run-name is not exactly the pull request number and head SHA", "swapped")
        self.flagged(TOOL, 'RUN_NAME = "May iOS core suite for may-app #{number} at {sha}"',
                     'RUN_NAME = "May Android unit suites for may-app #{number} at {sha}"', "RUN_NAME is not ios-core-pr.yml's",
                     "the Android lane's run name")

    def test_the_tool_writes_nothing_but_the_one_dispatch(self):
        write = "ios_core_schedule.py: a request writes somewhere other than control_dispatch's ios-core-pr.yml dispatch"
        cases = {
            "no path check": ("    if path != DISPATCH_PATH:\n        raise ScheduleError", "    if not path:\n        raise ScheduleError", write),
            "a body on a read": ('request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", headers={',
                                 'request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", data=b"{}", headers={', write),
            "the Android lane's endpoint": ('DISPATCH_PATH = "actions/workflows/ios-core-pr.yml/dispatches"',
                                            'DISPATCH_PATH = "actions/workflows/android-unit-pr.yml/dispatches"', "DISPATCH_PATH is not"),
            "a may-app reader of its own": ("def control_get(path, token):",
                                            "def app_get(path, token):\n    return f\"{API}/repos/{APP_REPO}/{path}\"\n\n\ndef control_get(path, token):",
                                            "reads may-app other than through android_ci_schedule.app_get"),
            "a printed title": ('print(f"May iOS core schedule: picked may-app #{int(number)} at {sha} ({int(looked)} heads looked at)")',
                                'print(f"May iOS core schedule: picked {candidate}")', "prints something other than a fixed line"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(TOOL, old, new, message, case)


if __name__ == "__main__":
    unittest.main()
