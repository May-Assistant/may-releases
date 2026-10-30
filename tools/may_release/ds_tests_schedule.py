"""May's design-system tests on a schedule (ds-tests-schedule.yml): every 15 minutes, dispatch ds-tests-pr.yml for at
most one may-design-system pull request head that has not had a design-system test run in the last 30 days.

Samuel's decision (28 Oct): every may-design-system pull request into claude/may-integration or main runs both of its
suites here. The same model as backend_ci_schedule.py, whose run check, list reader, staleness rule, dispatch and its
refusals it reuses: it adds no new way to build or test anything, and chooses only which head goes next. The dispatched
ds-tests-pr.yml admits and checks that head again itself (gate.py --action ds-compile --check-pull).

The job is split so that no step holds two credentials (static_check.py pins it):
- admit (no environment): the run is this repository's schedule or a manual dispatch, from main, and may-ds-renders
  keeps its policy (administrator bypass off, main only, no required reviewer).
- runs (GITHUB_TOKEN alone): reads this repository's ds-tests-pr runs from the last 30 days. Their run-name carries the
  pull request number and head SHA, so a head with a run in that window is skipped: a head runs once. When a run is
  queued or running, nothing is dispatched (a second pending run in the same concurrency group would cancel the first).
- pick (MAY_DS_PR_READ_TOKEN alone, held in may-ds-renders): lists may-design-system's open pull requests against
  claude/may-integration and against main, oldest first, keeps those from a may-design-system branch (never a fork)
  updated in the last 14 days, and picks the first head not run in the window.
- dispatch (GITHUB_TOKEN alone, the job's one write scope, actions: write): one POST to ds-tests-pr.yml's dispatches
  endpoint, ref main, with exactly that pull request number and head SHA. control_dispatch refuses any other path, ref
  or input before anything is sent, and it is the only write this script makes.

may-design-system is private and this repository public, so nothing read from it is printed: every line is fixed, apart
from counts, the picked pull request's number and its head SHA (both already in ds-tests-pr.yml's public run name and
log). Between steps only numbers and SHAs are carried, in files under $RUNNER_TEMP/may-ds-tests-ci.
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
import urllib.parse
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_ci_schedule import (API, BUSY, MAX_LOOKED, NUMBER, SHA, STALE, WINDOW, ScheduleError, check_run,
                                 dispatch_payload, listing, updated_since)
from gate import CONTROL_REPO, DS_BASES, DS_REPO, ENVIRONMENTS, GateDenial, get_json, validate_no_reviewer_environment

ENVIRONMENT = ENVIRONMENTS["ds-compile"]
READ_TOKEN = "MAY_DS_PR_READ_TOKEN"
# ds-tests-pr.yml's run-name, exactly (static_check.py pins both).
RUN_NAME = "May DS tests for may-design-system #{number} at {sha}"
RUNS_PATH = "actions/workflows/ds-tests-pr.yml/runs"
DISPATCH_PATH = "actions/workflows/ds-tests-pr.yml/dispatches"
STATE = "may-ds-tests-ci"
RUNS_FILE, PICK_FILE = "runs.json", "pick.json"


def state(fresh=False):
    temp = os.environ.get("RUNNER_TEMP")
    if not temp or not Path(temp).is_dir():
        raise ScheduleError("Runner temporary directory is unavailable")
    path = Path(temp) / STATE
    if fresh:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(mode=0o700)
    return path


def admit(get=get_json):
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
    print("May DS tests schedule admission passed")


def control_get(path, token):
    """One read of this repository's Actions API: (status, body). Bodies are never printed."""
    if not path.startswith(RUNS_PATH + "?"):
        raise ScheduleError("Only ds-tests-pr.yml's runs are read from this repository")
    request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def control_dispatch(path, token, payload, opener=None):
    """The one write this script makes: ds-tests-pr.yml's workflow dispatch, on main, with exactly a pull request
    number and a head SHA. Any other path, ref or input is refused before anything is sent. Returns the status."""
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if path != DISPATCH_PATH:
        raise ScheduleError("The schedule writes nothing but ds-tests-pr.yml's dispatch")
    if set(payload) != {"ref", "inputs"} or payload["ref"] != "main" or not isinstance(inputs, dict) or \
            set(inputs) != {"pr_number", "source_sha"} or not isinstance(inputs["pr_number"], str) or \
            not NUMBER.fullmatch(inputs["pr_number"]) or not isinstance(inputs["source_sha"], str) or \
            not SHA.fullmatch(inputs["source_sha"]):
        raise ScheduleError("The dispatch is not exactly main with a pull request number and a head SHA")
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


def recent_runs(token, now, get=control_get):
    """(busy, tested): whether a design-system test run is queued or running, and the (number, sha) of every head that
    has had one created in the last 30 days, read from the run name. A name not in RUN_NAME's form counts for nothing."""
    since = (now - WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = listing(get, RUNS_PATH + "?created=" + urllib.parse.quote(">=" + since), token, "workflow_runs")
    busy, tested = False, set()
    lead, _, rest = RUN_NAME.partition("{number}")
    middle, _, tail = rest.partition("{sha}")
    pattern = re.compile(re.escape(lead) + "([1-9][0-9]{0,6})" + re.escape(middle) + "([0-9a-f]{40})" + re.escape(tail))
    for run in runs:
        if not isinstance(run, dict):
            continue
        busy = busy or run.get("status") in BUSY
        title = run.get("display_title")
        found = pattern.fullmatch(title) if isinstance(title, str) else None
        if found:
            tested.add((int(found.group(1)), found.group(2)))
    return busy, tested


def ds_get(path, token):
    """One read of may-design-system's open pull requests with the pull-request read token: (status, body). Never
    printed."""
    if not re.fullmatch(r"pulls\?state=open&base=(?:claude/may-integration|main)&sort=created&direction=asc"
                        r"&per_page=100&page=[1-9][0-9]?", path):
        raise ScheduleError("Only may-design-system's open pull requests are read")
    request = urllib.request.Request(f"{API}/repos/{DS_REPO}/{path}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def pick(token, tested, get=ds_get, now=None):
    """(number, sha, looked): the oldest open may-design-system pull request against claude/may-integration or main
    from a may-design-system branch, updated in the last 14 days, whose head has had no design-system test run in the
    window, or None for number and sha; looked counts the heads considered. Nothing read here is printed."""
    looked = 0
    since = (now or datetime.datetime.now(datetime.timezone.utc)) - STALE
    candidates = []
    for base_ref in DS_BASES:
        for candidate in listing(get, f"pulls?state=open&base={base_ref}&sort=created&direction=asc", token):
            if isinstance(candidate, dict):
                candidates.append(candidate)
    candidates.sort(key=lambda c: (str(c.get("created_at")), c.get("number") if isinstance(c.get("number"), int) else 0))
    for candidate in candidates:
        number, head, base = candidate.get("number"), candidate.get("head") or {}, candidate.get("base") or {}
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or not NUMBER.fullmatch(str(number)) or \
                not isinstance(sha, str) or not SHA.fullmatch(sha) or candidate.get("state") != "open" or \
                not isinstance(base, dict) or base.get("ref") not in DS_BASES or \
                (base.get("repo") or {}).get("full_name") != DS_REPO or \
                (head.get("repo") or {}).get("full_name") != DS_REPO or (number, sha) in tested or \
                not updated_since(candidate, since):
            continue
        if looked == MAX_LOOKED:
            break
        looked += 1
        return number, sha, looked
    return None, None, looked


def read_state(name):
    try:
        return json.loads((state() / name).read_text())
    except (OSError, ValueError):
        return None


def write_state(name, value):
    (state() / name).write_text(json.dumps(value))


def runs_step(now=None, get=control_get):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    state(fresh=True)
    busy, tested = recent_runs(token, now or datetime.datetime.now(datetime.timezone.utc), get)
    write_state(RUNS_FILE, {"busy": busy, "tested": sorted([n, s] for n, s in tested)})
    count = len(tested)
    if busy:
        print("May DS tests schedule: a design-system test run is queued or running; nothing is picked this time")
    else:
        print(f"May DS tests schedule: {int(count)} heads had a design-system test run in the last 30 days")


def pick_step(get=ds_get, now=None):
    check_run()
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(READ_TOKEN, "")
    if not token:
        raise GateDenial("Pull-request read token unavailable (MAY_DS_PR_READ_TOKEN in may-ds-renders)")
    runs = read_state(RUNS_FILE)
    if not isinstance(runs, dict) or not isinstance(runs.get("busy"), bool) or not isinstance(runs.get("tested"), list):
        raise ScheduleError("The recent design-system test runs were not recorded")
    if runs["busy"]:
        print("May DS tests schedule: a design-system test run is queued or running; nothing picked")
        return
    tested = {(n, s) for n, s in (e for e in runs["tested"] if isinstance(e, list) and len(e) == 2)}
    number, sha, looked = pick(token, tested, get, now)
    if number is None:
        print(f"May DS tests schedule: no head to run ({int(looked)} heads looked at)")
        return
    write_state(PICK_FILE, {"number": number, "sha": sha})
    print(f"May DS tests schedule: picked may-design-system #{int(number)} at {sha} ({int(looked)} heads looked at)")


def dispatch_step(post=control_dispatch):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    picked = read_state(PICK_FILE)
    if picked is None:
        print("May DS tests schedule: nothing to dispatch")
        return
    if not isinstance(picked, dict) or set(picked) != {"number", "sha"}:
        raise ScheduleError("The pick is malformed; nothing was dispatched")
    number, sha = picked["number"], picked["sha"]
    status = post(DISPATCH_PATH, token, dispatch_payload(number, sha))
    if status not in (200, 204):
        raise ScheduleError("ds-tests-pr.yml could not be dispatched")
    print(f"May DS tests schedule: dispatched the design-system tests for may-design-system #{int(number)} at {sha}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("admit", "runs", "pick", "dispatch"))
    args = parser.parse_args()
    {"admit": admit, "runs": runs_step, "pick": pick_step, "dispatch": dispatch_step}[args.step]()


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May DS tests schedule denied: " + denial.args[0]) from None
    except ScheduleError as error:
        raise SystemExit("May DS tests schedule stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May DS tests schedule stopped; check may-ds-renders, its secret and the API") from None
