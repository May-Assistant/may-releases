"""May's Android unit suites on a schedule (android-ci-schedule.yml): every 15 minutes, dispatch android-unit-pr.yml
for at most one may-app pull request head that touches Android and has not had a unit run in the last 24 hours.

This adds no new way to build or test anything. It picks a pull request and dispatches the existing, reviewed
android-unit-pr.yml, whose own admission and pull request check (gate.py --action compile-pr) decide again whether
that head may run. So the schedule chooses only which head goes next, never what a run may do.

The job is split so that no step holds two credentials (static_check.py pins it):
- admit (no environment): the run is this repository's schedule or a manual dispatch, from main, and
  may-native-compile-pr keeps its policy (administrator bypass off, main only, no required reviewer). A missing
  environment is denied on its 404, never auto-created.
- runs (GITHUB_TOKEN alone): reads this repository's android-unit-pr runs from the last 24 hours. Their run-name
  carries the pull request number and head SHA, so a head with a run in that window is skipped (one run a day per
  head: nothing records a head as done while the comment token is missing, and a passed head is not run twice in a
  day). When a unit run is queued or running, nothing is dispatched (a second pending run in the same concurrency
  group would cancel the first).
- pick (MAY_APP_PR_READ_TOKEN alone, held in may-native-compile-pr): lists may-app's open pull requests against
  main, oldest first, keeps those from a may-app branch (never a fork), and for each head not run in the window
  reads its changed files and keeps it when one is under android/, contracts/, shared/ or gradle/, or is
  tools/release/android.sh (the design-system lock is android/design-system.lock). The first such head is the pick.
- dispatch (GITHUB_TOKEN alone, the job's one write scope, actions: write): one POST to android-unit-pr.yml's
  dispatches endpoint, ref main, with exactly that pull request number and head SHA. control_dispatch refuses any
  other path, ref or input before anything is sent, and it is the only write this script makes.

may-app is private and this repository public, so nothing read from may-app is printed: every line is fixed, apart
from counts, the picked pull request's number and its head SHA (both already in android-unit-pr.yml's public run
name and log). Between steps only numbers and SHAs are carried, in files under $RUNNER_TEMP/may-android-ci.
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

from gate import APP_REPO, CONTROL_REPO, ENVIRONMENTS, GateDenial, get_json, validate_no_reviewer_environment

ENVIRONMENT = ENVIRONMENTS["compile-pr"]
READ_TOKEN = "MAY_APP_PR_READ_TOKEN"
# android-unit-pr.yml's run-name, exactly (static_check.py pins both).
RUN_NAME = "May Android unit suites for may-app #{number} at {sha}"
RUNS_PATH = "actions/workflows/android-unit-pr.yml/runs"
DISPATCH_PATH = "actions/workflows/android-unit-pr.yml/dispatches"
# The paths whose change makes a head worth the unit suites (coordinator, 18 Oct).
PREFIXES = ("android/", "contracts/", "shared/", "gradle/")
FILES = ("tools/release/android.sh",)
WINDOW = datetime.timedelta(hours=24)
# A unit run in any of these is still to finish: the schedule waits for it.
BUSY = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
# At most this many heads have their files read in one run, so a long queue of non-Android pull requests costs a
# bounded number of reads; the rest are looked at on a later run.
MAX_LOOKED = 40
PAGES = 30
SHA = re.compile(r"[0-9a-f]{40}")
NUMBER = re.compile(r"[1-9][0-9]{0,6}")
API = "https://api.github.com"
STATE = "may-android-ci"
RUNS_FILE, PICK_FILE = "runs.json", "pick.json"


class ScheduleError(ValueError):
    pass


def check_run():
    """The schedule or a manual dispatch, of this repository, from main; anything else is refused."""
    run = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"), os.environ.get("GITHUB_EVENT_NAME")
    if run[:2] != (CONTROL_REPO, "refs/heads/main") or run[2] not in ("schedule", "workflow_dispatch"):
        raise GateDenial("May control must run by its schedule or manual dispatch, from its main branch")


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
    print("May Android CI schedule admission passed")


def control_get(path, token):
    """One read of this repository's Actions API: (status, body). Bodies are never printed."""
    if not path.startswith(RUNS_PATH + "?"):
        raise ScheduleError("Only android-unit-pr.yml's runs are read from this repository")
    request = urllib.request.Request(f"{API}/repos/{CONTROL_REPO}/{path}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def app_get(path, token):
    """One read of may-app's pull requests with the pull-request read token: (status, body). Never printed."""
    if not path.startswith("pulls?") and not re.fullmatch(r"pulls/[1-9][0-9]{0,6}/files\?.*", path):
        raise ScheduleError("Only may-app's pull requests and their files are read")
    request = urllib.request.Request(f"{API}/repos/{APP_REPO}/{path}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def dispatch_payload(number, sha):
    if not isinstance(number, int) or isinstance(number, bool) or not NUMBER.fullmatch(str(number)) or \
            not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ScheduleError("The pick is not a pull request number and an exact head SHA")
    return {"ref": "main", "inputs": {"pr_number": str(number), "source_sha": sha}}


def control_dispatch(path, token, payload, opener=None):
    """The one write this script makes: android-unit-pr.yml's workflow dispatch, on main, with exactly a pull request
    number and a head SHA. Any other path, ref or input is refused before anything is sent. Returns the status."""
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if path != DISPATCH_PATH:
        raise ScheduleError("The schedule writes nothing but android-unit-pr.yml's dispatch")
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


def listing(get, path, token, key=None):
    """Every item of a paginated list (key: the list's field in an object page), up to PAGES pages of 100."""
    items = []
    for page in range(1, PAGES + 1):
        status, body = get(f"{path}&per_page=100&page={page}", token)
        if status != 200:
            raise ScheduleError("A GitHub list could not be read")
        try:
            batch = json.loads(body)
        except ValueError:
            raise ScheduleError("A GitHub list could not be read") from None
        batch = batch.get(key) if key and isinstance(batch, dict) else batch
        if not isinstance(batch, list):
            raise ScheduleError("A GitHub list could not be read")
        items += batch
        if len(batch) < 100:
            return items
    raise ScheduleError("A GitHub list is longer than 3000 items")


def recent_runs(token, now, get=control_get):
    """(busy, tested): whether a unit run is queued or running, and the (number, sha) of every head that has had a
    unit run created in the last 24 hours, read from the run name. A name not in RUN_NAME's form counts for nothing."""
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


def touches(files):
    """A changed file (or a renamed file's old path) is under PREFIXES or is one of FILES."""
    for entry in files:
        if not isinstance(entry, dict):
            continue
        for path in (entry.get("filename"), entry.get("previous_filename")):
            if isinstance(path, str) and (path.startswith(PREFIXES) or path in FILES):
                return True
    return False


def pick(token, tested, get=app_get):
    """(number, sha, looked, android): the oldest open may-app pull request against main from a may-app branch whose
    head has had no unit run in the window and touches the paths, or None for number and sha; looked counts the heads
    whose files were read, android those that touch the paths. A pull request whose files can't be read, or that has
    more than 3000, is passed over, not the run stopped. Nothing read here is printed."""
    looked = android = 0
    for candidate in listing(get, "pulls?state=open&base=main&sort=created&direction=asc", token):
        if not isinstance(candidate, dict):
            continue
        number, head, base = candidate.get("number"), candidate.get("head") or {}, candidate.get("base") or {}
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or not NUMBER.fullmatch(str(number)) or \
                not isinstance(sha, str) or not SHA.fullmatch(sha) or candidate.get("state") != "open" or \
                not isinstance(base, dict) or base.get("ref") != "main" or \
                (base.get("repo") or {}).get("full_name") != APP_REPO or \
                (head.get("repo") or {}).get("full_name") != APP_REPO or (number, sha) in tested:
            continue
        if looked == MAX_LOOKED:
            break
        looked += 1
        try:
            files = listing(get, f"pulls/{number}/files?", token)
        except ScheduleError:
            continue
        if touches(files):
            android += 1
            return number, sha, looked, android
    return None, None, looked, android


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
        print("May Android CI schedule: a unit run is queued or running; nothing is picked this time")
    else:
        print(f"May Android CI schedule: {int(count)} heads had a unit run in the last 24 hours")


def pick_step(get=app_get):
    check_run()
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(READ_TOKEN, "")
    if not token:
        raise GateDenial("Pull-request read token unavailable (MAY_APP_PR_READ_TOKEN in may-native-compile-pr)")
    runs = read_state(RUNS_FILE)
    if not isinstance(runs, dict) or not isinstance(runs.get("busy"), bool) or not isinstance(runs.get("tested"), list):
        raise ScheduleError("The recent unit runs were not recorded")
    if runs["busy"]:
        print("May Android CI schedule: a unit run is queued or running; nothing picked")
        return
    tested = {(n, s) for n, s in (e for e in runs["tested"] if isinstance(e, list) and len(e) == 2)}
    number, sha, looked, android = pick(token, tested, get)
    if number is None:
        print(f"May Android CI schedule: no head to run ({int(looked)} heads looked at, none touching Android)")
        return
    write_state(PICK_FILE, {"number": number, "sha": sha})
    print(f"May Android CI schedule: picked may-app #{int(number)} at {sha} ({int(looked)} heads looked at)")


def dispatch_step(post=control_dispatch):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    picked = read_state(PICK_FILE)
    if picked is None:
        print("May Android CI schedule: nothing to dispatch")
        return
    if not isinstance(picked, dict) or set(picked) != {"number", "sha"}:
        raise ScheduleError("The pick is malformed; nothing was dispatched")
    number, sha = picked["number"], picked["sha"]
    status = post(DISPATCH_PATH, token, dispatch_payload(number, sha))
    if status not in (200, 204):
        raise ScheduleError("android-unit-pr.yml could not be dispatched")
    print(f"May Android CI schedule: dispatched the unit suites for may-app #{int(number)} at {sha}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("admit", "runs", "pick", "dispatch"))
    args = parser.parse_args()
    {"admit": admit, "runs": runs_step, "pick": pick_step, "dispatch": dispatch_step}[args.step]()


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May Android CI schedule denied: " + denial.args[0]) from None
    except ScheduleError as error:
        raise SystemExit("May Android CI schedule stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May Android CI schedule stopped; check may-native-compile-pr, its secret and the API") from None
