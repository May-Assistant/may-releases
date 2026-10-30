"""May's weekly store release (weekly-release.yml; 30 Oct): once a week, cut May Playground from
may-app main and dispatch store-release-draft.yml for iOS, then Android, one at a time. The environment's approval is
unchanged: a required reviewer still approves each run, so the schedule only starts what a person would have dispatched.

It fires every hour on Thursday and Friday (UTC) and decides afresh each time, with no state of its own: a job that
waited for the approval would die at GitHub's six hours, and store-release-draft.yml's own runs (their run-name carries
the platform, the build number and, from now on, the source SHA) say everything that has happened.
- The cut is may-app main as of the week's CUT (Thursday 15:07 UTC; the day is the coordinator's placeholder until
  Samuel confirms it): the merge commit of the last pull request merged into main at or before then. Later merges that
  day wait for next week, so the hourly firings never make a second build of the week.
- A firing after Friday 23:07 UTC, or between 10:00 and 11:30 UTC (the device-test window): nothing is dispatched.
- A store run queued, waiting for its approval or running: nothing is dispatched.
- No iOS run for the cut: iOS is dispatched. This is also "no run if main hasn't changed": a cut already built is
  never dispatched again.
- iOS for the cut succeeded and Android has no run for it: Android is dispatched.
- iOS for the cut failed or was cancelled: nothing more is dispatched; the log says so.
Dry runs count for nothing but their build numbers. Each dispatch's build number is one above the highest that
platform's TARGET has ever had in store-release-draft.yml's run names (and at least the store floor), so it increments
as by hand; gate.py refuses it again if it is not.

The job is split as the other schedules are (static_check.py pins it with them), so no step holds two credentials:
- admit (no environment): the schedule or a manual dispatch, from main; may-native-compile-pr keeps its policy.
- runs (GITHUB_TOKEN alone): reads store-release-draft.yml's runs (statuses, outcomes and run names).
- pick (MAY_APP_PR_READ_TOKEN alone): reads may-app's merged pull requests to find the cut.
- dispatch (GITHUB_TOKEN alone, actions: write): decides, and makes at most one POST to store-release-draft.yml's
  dispatches endpoint, on main, with exactly a platform, TARGET, the cut, a build number and no dry run.
Nothing read from may-app is printed: only fixed lines, build numbers and the cut's SHA, which the store run's own name
carries in public anyway.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_ci_schedule import BUSY, ENVIRONMENT, ScheduleError, app_get, check_run
from gate import APP_REPO, CONTROL_REPO, GateDenial, get_json, validate_no_reviewer_environment
from store import MIN_BUILD_NUMBERS

READ_TOKEN = "MAY_APP_PR_READ_TOKEN"
# The weekly app: May Playground alone until May launches (coordinator, 30 Oct); production is one more entry here.
TARGET = "playground"
# store-release-draft.yml's run-name, as this tool reads it (static_check.py pins both): the app, the platform, the
# build number and the source SHA, then the dry-run and sign-only marks.
RUN_NAME = "May Playground · {platform} · build {number} at {sha}"
RUNS_PATH = "actions/workflows/store-release-draft.yml/runs"
DISPATCH_PATH = "actions/workflows/store-release-draft.yml/dispatches"
# The week's cut: Thursday (weekday 3) at 15:07 UTC, off the round minutes and outside the 10:00-11:30 device window.
CUT_WEEKDAY, CUT_HOUR, CUT_MINUTE = 3, 15, 7
# Firings dispatch only from the cut to Friday 23:07 UTC (coordinator, 30 Oct), and never in the device-test window,
# 10:00 to 11:30 UTC, so a release never overlaps it.
WINDOW = datetime.timedelta(hours=32)
DEVICE_WINDOW = (datetime.time(10, 0), datetime.time(11, 30))
PLATFORMS = ("ios", "android")
LABELS = {"ios": "iOS", "android": "Android"}
STORE_PAGES = 5
MERGED_PAGES = 10
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"May Playground · (iOS|Android) · build ([1-9][0-9]{0,8})(?: at ([0-9a-f]{40}))?"
                  r"( \(dry run\))?(?: \(sign only\))?")
API = "https://api.github.com"
STATE = "may-weekly-release"
RUNS_FILE, CUT_FILE = "runs.json", "cut.json"


def state(fresh=False):
    temp = os.environ.get("RUNNER_TEMP")
    if not temp or not Path(temp).is_dir():
        raise ScheduleError("Runner temporary directory is unavailable")
    path = Path(temp) / STATE
    if fresh:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(mode=0o700)
    return path


def read_state(name):
    try:
        return json.loads((state() / name).read_text())
    except (OSError, ValueError):
        return None


def write_state(name, value):
    (state() / name).write_text(json.dumps(value))


def cut_time(now):
    """The latest CUT at or before now."""
    day = now.replace(hour=CUT_HOUR, minute=CUT_MINUTE, second=0, microsecond=0)
    day -= datetime.timedelta(days=(now.weekday() - CUT_WEEKDAY) % 7)
    return day if day <= now else day - datetime.timedelta(days=7)


def quiet(now):
    """The fixed reason this firing dispatches nothing whatever the runs say, or None."""
    if now - cut_time(now) > WINDOW:
        return "outside"
    if DEVICE_WINDOW[0] <= now.time() < DEVICE_WINDOW[1]:
        return "device window"
    return None


def admit(get=get_json):
    """The schedule or a manual dispatch, from main, and may-native-compile-pr's policy (its pick reads may-app's
    pull requests with that environment's read token, as the other schedules do)."""
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    base = "environments/" + ENVIRONMENT
    validate_no_reviewer_environment(get(base, token), get(base + "/deployment-branch-policies?per_page=100", token))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("environment=" + ENVIRONMENT + "\n")
    print("May weekly release admission passed")


def control_get(path, token):
    """One read of this repository's store-release-draft runs: (status, body). Bodies are never printed."""
    if not path.startswith(RUNS_PATH + "?"):
        raise ScheduleError("Only store-release-draft.yml's runs are read from this repository")
    request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def store_runs(token, get=control_get):
    """(busy, runs): whether any store run is still to finish, and every May Playground run, newest first, as
    {platform, build, sha (or None for a name from before the SHA was added), dry, done, succeeded}."""
    busy, runs = False, []
    for page in range(1, STORE_PAGES + 1):
        status, body = get(f"{RUNS_PATH}?per_page=100&page={page}", token)
        try:
            batch = json.loads(body).get("workflow_runs") if status == 200 else None
        except (ValueError, AttributeError):
            batch = None
        if not isinstance(batch, list):
            raise ScheduleError("store-release-draft.yml's runs could not be read")
        for run in batch:
            if not isinstance(run, dict):
                continue
            busy = busy or run.get("status") in BUSY
            title = run.get("display_title")
            found = NAME.fullmatch(title) if isinstance(title, str) else None
            if found:
                runs.append({"platform": found.group(1).lower(), "build": int(found.group(2)), "sha": found.group(3),
                             "dry": bool(found.group(4)), "done": run.get("status") == "completed",
                             "succeeded": run.get("conclusion") == "success"})
        if len(batch) < 100:
            return busy, runs
    return busy, runs


def merged_at(candidate):
    value = candidate.get("merged_at") if isinstance(candidate, dict) else None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def cut_sha(token, cut, get=app_get):
    """The merge commit of the last pull request merged into may-app main at or before cut, or None. Nothing read here
    is printed."""
    best = None
    for page in range(1, MERGED_PAGES + 1):
        status, body = get(f"pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page={page}", token)
        try:
            batch = json.loads(body) if status == 200 else None
        except ValueError:
            batch = None
        if not isinstance(batch, list):
            raise ScheduleError("may-app's merged pull requests could not be read")
        for candidate in batch:
            when, sha = merged_at(candidate), candidate.get("merge_commit_sha") if isinstance(candidate, dict) else None
            base = (candidate.get("base") or {}) if isinstance(candidate, dict) else {}
            if when is None or when > cut or not isinstance(sha, str) or not SHA.fullmatch(sha) or \
                    base.get("ref") != "main" or (base.get("repo") or {}).get("full_name") != APP_REPO:
                continue
            if best is None or when > best[0]:
                best = (when, sha)
        if len(batch) < 100:
            break
    return best[1] if best else None


def next_build(runs, platform):
    floor = MIN_BUILD_NUMBERS.get((platform, TARGET), 1)
    return max([floor - 1] + [run["build"] for run in runs if run["platform"] == platform]) + 1


def decide(busy, runs, cut):
    """(platform, build number) to dispatch for the cut, or (None, the fixed reason)."""
    if busy:
        return None, "busy"
    if cut is None:
        return None, "no cut"
    built = {platform: [run for run in runs if run["platform"] == platform and run["sha"] == cut and not run["dry"]]
             for platform in PLATFORMS}
    if not built["ios"]:
        return "ios", next_build(runs, "ios")
    if not built["ios"][0]["succeeded"]:
        return None, "ios failed"
    if not built["android"]:
        return "android", next_build(runs, "android")
    return None, "done"


def dispatch_payload(platform, cut, build):
    if platform not in PLATFORMS or not isinstance(cut, str) or not SHA.fullmatch(cut) or \
            not isinstance(build, int) or isinstance(build, bool) or not 1 <= build <= 999999999:
        raise ScheduleError("The weekly dispatch is not a platform, an exact cut and a build number")
    return {"ref": "main", "inputs": {"platform": platform, "target": TARGET, "source_sha": cut,
                                      "build_number": str(build), "dry_run": "false", "sign_only": "false"}}


def control_dispatch(path, token, payload, opener=None):
    """The one write this script makes: store-release-draft.yml's dispatch, on main, with exactly a platform, TARGET,
    an exact SHA, a build number, and neither a dry run nor sign-only. Anything else is refused before it is sent."""
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if path != DISPATCH_PATH:
        raise ScheduleError("The weekly release writes nothing but store-release-draft.yml's dispatch")
    if set(payload) != {"ref", "inputs"} or payload["ref"] != "main" or not isinstance(inputs, dict) or \
            set(inputs) != {"platform", "target", "source_sha", "build_number", "dry_run", "sign_only"} or \
            inputs["platform"] not in PLATFORMS or inputs["target"] != TARGET or \
            not isinstance(inputs["source_sha"], str) or not SHA.fullmatch(inputs["source_sha"]) or \
            not isinstance(inputs["build_number"], str) or not re.fullmatch(r"[1-9][0-9]{0,8}", inputs["build_number"]) or \
            inputs["dry_run"] != "false" or inputs["sign_only"] != "false":
        raise ScheduleError("The dispatch is not exactly main with a platform, the target, a cut and a build number")
    request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{DISPATCH_PATH}", data=json.dumps(payload).encode(),
                                     method="POST", headers={
                                         "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                         "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def runs_step(get=control_get):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    state(fresh=True)
    busy, runs = store_runs(token, get)
    write_state(RUNS_FILE, {"busy": busy, "runs": runs})
    count = len(runs)
    print(f"May weekly release: {int(count)} May Playground store runs read")


def pick_step(get=app_get, now=None):
    check_run()
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(READ_TOKEN, "")
    if not token:
        raise GateDenial("Pull-request read token unavailable (MAY_APP_PR_READ_TOKEN in may-native-compile-pr)")
    sha = cut_sha(token, cut_time(now or datetime.datetime.now(datetime.timezone.utc)), get)
    write_state(CUT_FILE, {"cut": sha})
    if sha is None:
        print("May weekly release: no may-app pull request was merged into main before this week's cut")
    else:
        print(f"May weekly release: this week's cut of may-app main is {sha}")


def dispatch_step(post=control_dispatch, now=None):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    runs, cut = read_state(RUNS_FILE), read_state(CUT_FILE)
    if not isinstance(runs, dict) or not isinstance(runs.get("busy"), bool) or not isinstance(runs.get("runs"), list) or \
            not isinstance(cut, dict) or set(cut) != {"cut"}:
        raise ScheduleError("The store runs or the cut were not recorded; nothing was dispatched")
    reason = quiet(now or datetime.datetime.now(datetime.timezone.utc))
    platform, number = (None, reason) if reason else decide(runs["busy"], runs["runs"], cut["cut"])
    if number == "outside":
        print("May weekly release: outside Thursday 15:07 to Friday 23:07 UTC; nothing dispatched")
    elif number == "device window":
        print("May weekly release: inside the 10:00 to 11:30 UTC device-test window; nothing dispatched")
    elif number == "busy":
        print("May weekly release: a store run is queued, waiting for approval or running; nothing dispatched")
    elif number == "no cut":
        print("May weekly release: there is no cut this week; nothing dispatched")
    elif number == "ios failed":
        print("May weekly release: this week's iOS build did not succeed; Android is not dispatched")
    elif number == "done":
        print("May weekly release: this week's cut is already built for iOS and Android; nothing dispatched")
    if platform is None:
        return
    sha = cut["cut"]
    status = post(DISPATCH_PATH, token, dispatch_payload(platform, sha, number))
    if status not in (200, 204):
        raise ScheduleError("store-release-draft.yml could not be dispatched")
    if platform == "ios":
        print(f"May weekly release: dispatched May Playground iOS build {int(number)} at {sha}; it waits for approval")
    else:
        print(f"May weekly release: dispatched May Playground Android build {int(number)} at {sha}; it waits for "
              "approval")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("admit", "runs", "pick", "dispatch"))
    args = parser.parse_args()
    {"admit": admit, "runs": runs_step, "pick": pick_step, "dispatch": dispatch_step}[args.step]()


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May weekly release denied: " + denial.args[0]) from None
    except ScheduleError as error:
        raise SystemExit("May weekly release stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May weekly release stopped; check may-native-compile-pr, its secret and the API") from None
