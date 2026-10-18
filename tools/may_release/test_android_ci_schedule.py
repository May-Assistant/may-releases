"""May's Android unit suites on a schedule (android-ci-schedule.yml, android_ci_schedule.py)."""

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

import android_ci_schedule as tool
from android_ci_schedule import ScheduleError, control_dispatch, dispatch_payload, pick, recent_runs, touches
from gate import GateDenial
import static_check

SHA, OTHER, THIRD = "a" * 40, "b" * 40, "c" * 40
TOKEN = "github_pat_" + "E" * 50
WORKFLOW = ".github/workflows/android-ci-schedule.yml"
UNIT = ".github/workflows/android-unit-pr.yml"
TOOL = "tools/may_release/android_ci_schedule.py"
PRIVATE = "PRIVATE-MAY-APP-NAME-7c2e"
NOW = datetime.datetime(2026, 10, 18, 13, 0, tzinfo=datetime.timezone.utc)
RUN = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule"}


def pull(number, sha, head_repo="May-Assistant/may-app", base="main", updated="2026-10-18T12:00:00Z"):
    return {"number": number, "state": "open", "title": PRIVATE, "updated_at": updated, "head": {"sha": sha, "ref": PRIVATE, "repo": {"full_name": head_repo}},
            "base": {"ref": base, "repo": {"full_name": "May-Assistant/may-app"}}}


class FakeApp:
    """may-app's pull request list and each pull request's files, answering app_get's paths."""

    def __init__(self, pulls, files):
        self.pulls, self.files, self.paths = pulls, files, []

    def __call__(self, path, token):
        self.paths.append(path)
        if path.startswith("pulls?state=open&base=main&sort=created&direction=asc&per_page=100&page="):
            return 200, json.dumps(self.pulls if path.endswith("page=1") else []).encode()
        number = int(path.split("/")[1])
        found = self.files.get(number)
        if found is None:
            return 404, b""
        return 200, json.dumps([{"filename": f} if isinstance(f, str) else f for f in found]).encode()


def runs_page(*runs):
    return lambda path, token: (200, json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}).encode())


def run(number, sha, status="completed"):
    return {"display_title": f"May Android unit suites for may-app #{number} at {sha}", "status": status}


class RunTests(unittest.TestCase):
    def test_only_the_schedule_or_a_dispatch_from_main_runs(self):
        for event in ("schedule", "workflow_dispatch"):
            with patch.dict(os.environ, {**RUN, "GITHUB_EVENT_NAME": event}):
                tool.check_run()
        for case, change in {"a pull request": {"GITHUB_EVENT_NAME": "pull_request"},
                             "pull_request_target": {"GITHUB_EVENT_NAME": "pull_request_target"},
                             "another branch": {"GITHUB_REF": "refs/heads/claude/x"},
                             "a pull request ref": {"GITHUB_REF": "refs/pull/1/merge"},
                             "a fork": {"GITHUB_REPOSITORY": "someone/may-releases"}}.items():
            with self.subTest(case=case), patch.dict(os.environ, {**RUN, **change}):
                with self.assertRaises(GateDenial):
                    tool.check_run()

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
            self.assertEqual(out.getvalue(), "May Android CI schedule admission passed\n")
            reviewer = {**policy, "protection_rules": [{"type": "required_reviewers"}]}
            with patch.dict(os.environ, {**RUN, "GH_TOKEN": TOKEN, "GITHUB_OUTPUT": str(output)}):
                with self.assertRaises(GateDenial):
                    tool.admit(lambda path, token: branches if "deployment-branch-policies" in path else reviewer)


class RecentRunTests(unittest.TestCase):
    def test_heads_run_in_the_last_day_are_read_from_the_run_name(self):
        seen = []

        def get(path, token):
            seen.append(path)
            return runs_page(run(7, SHA), run(8, OTHER, "completed"), {"display_title": PRIVATE, "status": "completed"},
                             {"display_title": f"May Android unit suites for may-app #9 at {THIRD} (again)"})(path, token)
        busy, tested = recent_runs(TOKEN, NOW, get)
        self.assertFalse(busy)
        self.assertEqual(tested, {(7, SHA), (8, OTHER)})
        self.assertEqual(seen, ["actions/workflows/android-unit-pr.yml/runs?created=%3E%3D2026-09-18T13%3A00%3A00Z"
                                "&per_page=100&page=1"])

    def test_a_head_runs_once_and_a_unit_run_comment_alone_never_runs_it_again(self):
        # The run's comment refreshes the pull request's updated_at. The head stays tested for 30 days; by then that
        # comment is more than 14 days old, so the pull request is stale and passed over: no daily re-run.
        self.assertGreater(tool.WINDOW, tool.STALE)
        commented = (NOW - datetime.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        app = FakeApp([pull(3, SHA, updated=commented)], {3: ["android/x"]})
        self.assertEqual(pick(TOKEN, {(3, SHA)}, app, NOW)[:2], (None, None), "tested 20 days ago")
        self.assertEqual(pick(TOKEN, set(), app, NOW + datetime.timedelta(days=11))[:2], (None, None),
                         "out of the window, but stale")

    def test_a_queued_or_running_unit_run_makes_the_schedule_wait(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            with self.subTest(status=status):
                self.assertTrue(recent_runs(TOKEN, NOW, runs_page(run(7, SHA), run(8, OTHER, status)))[0])

    def test_an_unreadable_run_list_stops_the_run(self):
        with self.assertRaises(ScheduleError):
            recent_runs(TOKEN, NOW, lambda path, token: (500, b""))


class PickTests(unittest.TestCase):
    def test_a_head_touches_android_by_these_paths_only(self):
        for path in ("android/app/build.gradle.kts", "android/design-system.lock", "contracts/openapi.yaml", "shared/x.json",
                     "gradle/libs.versions.toml", "tools/release/android.sh"):
            with self.subTest(path=path):
                self.assertTrue(touches([{"filename": path}]))
        self.assertTrue(touches([{"filename": "docs/x.md", "previous_filename": "android/x.kt"}]), "a rename out of android/")
        for path in ("backend/android/x.ts", "docs/android.md", "tools/release/android.sh.bak", "ios/shared/x.swift",
                     "androidx/x", "tools/release/ios.sh"):
            with self.subTest(path=path):
                self.assertFalse(touches([{"filename": path}]))
        self.assertFalse(touches(["android/x", None, {"filename": 3}]))

    def test_the_oldest_untested_same_repository_head_touching_android_is_picked(self):
        app = FakeApp([pull(1, SHA, head_repo="someone/may-app"), pull(2, OTHER, base="claude/x"), pull(3, THIRD),
                       pull(4, SHA), pull(5, OTHER), pull(6, THIRD)],
                      {1: ["android/x"], 2: ["android/x"], 3: ["android/x"], 4: ["backend/x.ts"], 5: ["android/x"], 6: ["android/x"]})
        self.assertEqual(pick(TOKEN, {(3, THIRD)}, app, NOW), (5, OTHER, 2, 1))
        self.assertEqual([p.split("?")[0] for p in app.paths[1:]], ["pulls/4/files", "pulls/5/files"],
                         "a fork, another base and a head already run have no files read")

    def test_a_new_head_on_a_tested_pull_request_is_picked(self):
        app = FakeApp([pull(3, OTHER)], {3: ["android/x"]})
        self.assertEqual(pick(TOKEN, {(3, SHA)}, app, NOW)[:2], (3, OTHER))

    def test_unreadable_files_pass_the_pull_request_over_and_nothing_found_picks_nothing(self):
        app = FakeApp([pull(3, SHA), pull(4, OTHER)], {4: ["docs/x.md"]})
        self.assertEqual(pick(TOKEN, set(), app, NOW), (None, None, 2, 0))

    def test_at_most_forty_heads_have_their_files_read_in_one_run(self):
        app = FakeApp([pull(n, SHA) for n in range(1, 60)], {n: ["docs/x.md"] for n in range(1, 60)} | {59: ["android/x"]})
        self.assertEqual(pick(TOKEN, set(), app, NOW), (None, None, 40, 0))
        self.assertEqual(len(app.paths), 41)

    def test_a_pull_request_not_updated_in_fourteen_days_is_passed_over_without_a_read(self):
        app = FakeApp([pull(1, SHA, updated="2026-07-01T09:00:00Z"), pull(2, SHA, updated="2026-10-04T12:59:59Z"),
                       pull(3, SHA, updated=None), pull(4, SHA, updated="18 Oct"), pull(5, OTHER, updated="2026-10-04T13:00:00Z")],
                      {n: ["android/x"] for n in range(1, 6)})
        self.assertEqual(pick(TOKEN, set(), app, NOW), (5, OTHER, 1, 1))
        self.assertEqual([p.split("?")[0] for p in app.paths[1:]], ["pulls/5/files"])

    def test_malformed_candidates_are_skipped(self):
        bad = [None, {"number": True, "head": {"sha": SHA}}, {**pull(2, SHA), "state": "closed"}, pull(3, "A" * 40),
               {**pull(4, SHA), "head": None}, {**pull(5, SHA), "number": "5"}]
        app = FakeApp(bad + [pull(6, SHA)], {n: ["android/x"] for n in range(1, 7)})
        self.assertEqual(pick(TOKEN, set(), app, NOW)[:2], (6, SHA))


class WriteTests(unittest.TestCase):
    class Opener:
        def __init__(self, status=204):
            self.requests, self.status = [], status

        def __call__(self, request, timeout):
            self.requests.append(request)
            opener = self

            class Response:
                status = opener.status

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False
            return Response()

    def test_the_dispatch_posts_exactly_main_a_number_and_a_head(self):
        opener = self.Opener()
        self.assertEqual(control_dispatch(tool.DISPATCH_PATH, TOKEN, dispatch_payload(42, SHA), opener), 204)
        [request] = opener.requests
        self.assertEqual((request.get_method(), request.full_url),
                         ("POST", "https://api.github.com/repos/May-Assistant/may-releases/actions/workflows/android-unit-pr.yml/dispatches"))
        self.assertEqual(json.loads(request.data), {"ref": "main", "inputs": {"pr_number": "42", "source_sha": SHA}})

    def test_any_other_write_is_refused_before_anything_is_sent(self):
        payload = dispatch_payload(42, SHA)
        paths = ["actions/workflows/store-release-draft.yml/dispatches", "actions/workflows/native-compile-pr.yml/dispatches",
                 "actions/runs/1/cancel", "actions/runs/1/rerun", "actions/runs/1", "actions/workflows/android-unit-pr.yml/disable",
                 "dispatches", "actions/workflows/android-unit-pr.yml/dispatches/", "../may-app/issues/1/comments",
                 "actions/workflows/android-unit-pr.yml/dispatches?ref=x"]
        payloads = [{**payload, "ref": "claude/x"}, {**payload, "ref": "refs/heads/main"},
                    {**payload, "inputs": {**payload["inputs"], "core_breakdown": "true"}},
                    {**payload, "inputs": {"pr_number": "42"}}, {**payload, "inputs": {"pr_number": 42, "source_sha": SHA}},
                    {**payload, "inputs": {"pr_number": "0", "source_sha": SHA}},
                    {**payload, "inputs": {"pr_number": "42", "source_sha": SHA.upper()}},
                    {**payload, "return_run_details": True}, {"inputs": payload["inputs"]}]
        for case in [(p, payload) for p in paths] + [(tool.DISPATCH_PATH, p) for p in payloads]:
            opener = self.Opener()
            with self.subTest(case=case), self.assertRaises(ScheduleError):
                control_dispatch(*case[:1], TOKEN, case[1], opener)
            self.assertEqual(opener.requests, [])

    def test_reads_reach_only_the_unit_runs_and_may_app_pull_requests(self):
        for path in ("actions/runs?x=1", "actions/workflows/store-release-draft.yml/runs?x", "environments/x", "contents/x"):
            with self.subTest(path=path), self.assertRaises(ScheduleError):
                tool.control_get(path, TOKEN)
        for path in ("issues/1/comments?x", "contents/android?x", "pulls/1/merge?x", "pulls/1/files/../../x", "git/refs?x"):
            with self.subTest(path=path), self.assertRaises(ScheduleError):
                tool.app_get(path, TOKEN)

    def test_a_bad_pick_is_never_dispatched(self):
        for number, sha in ((0, SHA), (True, SHA), ("42", SHA), (42, "main"), (42, SHA[:39])):
            with self.subTest(number=number, sha=sha), self.assertRaises(ScheduleError):
                dispatch_payload(number, sha)


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
        app = FakeApp([pull(3, SHA), pull(4, OTHER)], {3: [{"filename": PRIVATE + ".md"}], 4: ["android/" + PRIVATE + ".kt"]})
        posted, out = self.steps(runs_page(run(9, THIRD)), app)
        self.assertEqual(posted, [(tool.DISPATCH_PATH, TOKEN, {"ref": "main", "inputs": {"pr_number": "4", "source_sha": OTHER}})])
        self.assertEqual(out.splitlines(), [
            "May Android CI schedule: 1 heads had a unit run in the last 30 days",
            f"May Android CI schedule: picked may-app #4 at {OTHER} (2 heads looked at)",
            f"May Android CI schedule: dispatched the unit suites for may-app #4 at {OTHER}"])
        self.assertNotIn(PRIVATE, out)

    def test_a_busy_unit_lane_reads_no_pull_request_and_dispatches_nothing(self):
        app = FakeApp([pull(4, OTHER)], {4: ["android/x"]})
        posted, out = self.steps(runs_page(run(9, THIRD, "in_progress")), app)
        self.assertEqual((posted, app.paths), ([], []))
        self.assertIn("nothing to dispatch", out)

    def test_nothing_to_run_dispatches_nothing(self):
        posted, out = self.steps(runs_page(run(4, OTHER)), FakeApp([pull(4, OTHER)], {4: ["android/x"]}))
        self.assertEqual(posted, [])
        self.assertIn("no head to run (0 heads looked at, none touching Android)", out)

    def test_a_refused_dispatch_fails_the_run(self):
        with self.assertRaises(ScheduleError):
            self.steps(runs_page(), FakeApp([pull(4, OTHER)], {4: ["android/x"]}), status=403)

    def test_pick_refuses_to_run_without_the_recorded_runs(self):
        with patch.dict(os.environ, {"MAY_APP_PR_READ_TOKEN": TOKEN}), self.assertRaises(ScheduleError):
            tool.pick_step(FakeApp([], {}))

    def test_a_tampered_pick_is_not_dispatched(self):
        tool.state(fresh=True)
        for value in ({"number": 4, "sha": OTHER, "ref": "x"}, {"number": "4", "sha": OTHER}, {"number": 4, "sha": "x"}, [4, OTHER]):
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
        self.assertEqual(static_check.android_ci_schedule_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.android_ci_schedule_tool_problems(static_check.ROOT), [])

    def test_no_step_holds_both_credentials_and_only_pick_can_write(self):
        name = "android-ci-schedule.yml: "
        secret = name + "a step holds a credential other than"
        permissions = name + "permissions are not read-only but for actions: write in pick alone"
        read = "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n"
        token = "          GH_TOKEN: ${{ github.token }}\n"
        listed = "      - name: List the unit runs of the last 30 days, with no may-app credential\n        env:\n"
        cleanup = "        if: always()\n"
        cases = {
            "GITHUB_TOKEN beside the read token": (read, read + token, secret),
            "the read token on runs": (listed, listed + read, secret),
            "the read token on dispatch": ("      - name: Dispatch the unit suites for the picked head, with no may-app credential\n        env:\n",
                                           "      - name: Dispatch the unit suites for the picked head, with no may-app credential\n        env:\n" + read, secret),
            "the read token in admission": ("        env:\n          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B tools/may_release/android_ci_schedule.py admit",
                                            "        env:\n" + read + "          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B tools/may_release/android_ci_schedule.py admit", secret),
            "a job-level env": ("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n" + read.replace("      ", "    ", 1), secret),
            "the comment token": (read, read.replace("READ", "COMMENT") * 1 + read, secret),
            "a cleanup with a token": (cleanup, cleanup + "        env:\n" + token, secret),
            "contents: write": ("      contents: read\n      actions: write\n", "      contents: write\n      actions: write\n", permissions),
            "actions: write in admission": ("      contents: read\n      actions: read\n    outputs:", "      contents: read\n      actions: write\n    outputs:", permissions),
            "pull-requests: write": ("      actions: write\n", "      actions: write\n      pull-requests: write\n", permissions),
            "write-all": ("permissions:\n  contents: read\nconcurrency:", "permissions: write-all\nconcurrency:", permissions),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_schedule_runs_from_main_only_never_from_a_pull_request(self):
        name = "android-ci-schedule.yml: "
        trigger = name + "is not scheduled every 15 minutes and dispatched with no input, only"
        cases = {
            "a pull_request trigger": ("  workflow_dispatch:\npermissions:", "  workflow_dispatch:\n  pull_request:\npermissions:", trigger),
            "pull_request_target": ("  workflow_dispatch:\npermissions:", "  workflow_dispatch:\n  pull_request_target:\npermissions:", trigger),
            "a push trigger": ("  schedule:\n", "  push:\n  schedule:\n", trigger),
            "a dispatch input": ("  workflow_dispatch:\n", "  workflow_dispatch:\n    inputs:\n      pr_number:\n        type: string\n", trigger),
            "every minute": ('"*/15 * * * *"', '"* * * * *"', trigger),
            "another environment": ("    environment: ${{ needs.admission.outputs.environment }}\n", "    environment: may-android-internal\n",
                                    name + "pick's environment is not the one admission chose"),
            "a fourth tool step": ("        run: python3 -I -S -B tools/may_release/android_ci_schedule.py dispatch\n",
                                   "        run: python3 -I -S -B tools/may_release/android_ci_schedule.py dispatch && curl x\n",
                                   name + "runs something other than"),
            "cancelling runs": ("  cancel-in-progress: false\n", "  cancel-in-progress: true\n", name + "does not run one at a time"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_unit_run_name_is_the_number_and_head_the_schedule_reads(self):
        message = "android-unit-pr.yml: run-name is not exactly the pull request number and head SHA"
        line = "run-name: May Android unit suites for may-app #${{ inputs.pr_number }} at ${{ inputs.source_sha }}\n"
        for case, new in {"no run-name": "", "the head first": line.replace("#${{ inputs.pr_number }} at ${{ inputs.source_sha }}",
                                                                            "#${{ inputs.source_sha }} at ${{ inputs.pr_number }}"),
                          "a may-app value": line.replace("\n", " ${{ github.event.inputs.title }}\n")}.items():
            with self.subTest(case=case):
                self.flagged(UNIT, line, new, message if new else "top-level keys", case)
        self.flagged(TOOL, 'RUN_NAME = "May Android unit suites for may-app #{number} at {sha}"',
                     'RUN_NAME = "May Android unit suites for #{number} at {sha}"', "RUN_NAME is not", "the tool's run name")

    def test_the_tool_writes_nothing_but_the_one_dispatch(self):
        write = "android_ci_schedule.py: a request writes somewhere other than control_dispatch"
        check = "    if path != DISPATCH_PATH:\n        raise ScheduleError"
        get_request = 'request = urllib.request.Request(f"{API}/repos/{APP_REPO}/{path}", headers={'
        cases = {
            "no path check": (check, "    if not path:\n        raise ScheduleError", write),
            "a write from the may-app read": (get_request, 'request = urllib.request.Request(f"{API}/repos/{APP_REPO}/{path}", method="DELETE", headers={', write),
            "a body on a read": ('request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", headers={',
                                 'request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", data=b"{}", headers={', write),
            "a posted path": ('f"{API}/repos/{CONTROL_REPO}/{DISPATCH_PATH}", data=', 'f"{API}/repos/{CONTROL_REPO}/{path}", data=', write),
            "another endpoint": ('DISPATCH_PATH = "actions/workflows/android-unit-pr.yml/dispatches"',
                                 'DISPATCH_PATH = "actions/workflows/store-release-draft.yml/dispatches"', "DISPATCH_PATH is not"),
            "a printed title": ('print(f"May Android CI schedule: picked may-app #{int(number)} at {sha} ({int(looked)} heads looked at)")',
                                'print(f"May Android CI schedule: picked {candidate}")', "prints something other than a fixed line"),
            "an Actions file outside admit": ('    write_state(PICK_FILE, {"number": number, "sha": sha})\n',
                                              '    write_state(PICK_FILE, {"number": number, "sha": sha})\n    os.environ.get("GITHUB_STEP_SUMMARY")\n',
                                              "writes a GitHub Actions file"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(TOOL, old, new, message, case)


if __name__ == "__main__":
    unittest.main()
