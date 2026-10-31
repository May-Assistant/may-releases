"""TestFlight "What to test" notes for an iOS store build (coordinator, 30 Oct): what changed in may-app since the
previous build, as testers read it in TestFlight.

may-app squash-merges its pull requests, so each first-parent commit subject on main is a pull request's title
("Brief: needs_you_count, everything waiting on the person (173) (#1503)"). The notes are those titles since the
previous iOS build of the same app, grouped by their area (the text before the first colon) and written as
"Try:" lines, at most NOTES_LIMIT characters (App Store Connect's whatsNew limit).

may-app is private and this repository public: the notes are private. This module only composes them; nothing here
prints a title, and the steps that use it print counts alone.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_ci_schedule import ScheduleError
from ios_upload import ASC_API, ASC_APP_IDS, OPENER, asc_get, asc_token
from store import (APPLE_KEY_ID, IOS_UPLOAD_SECRETS, StoreError, child_env, identity, require_step_secrets,
                   runner_root, secret_bytes, write_private)
from weekly_release import RUNS_PATH, control_get

NOTES_LIMIT = 4000
# The notes' private file, in the runner's may-private folder of the build job (compose) and the release job (open).
NOTES_FILE = "testflight-notes.txt"
# With no previous build to compare against (the first build, or one whose commit is not an ancestor of this one),
# the notes are the newest FALLBACK_COUNT titles.
FALLBACK_COUNT = 20
NOTHING_NEW = "No new changes since the previous build."
PULL_NUMBER = re.compile(r"\s*\(#\d+\)\s*$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# store-release-draft.yml's run-name, as static_check.STORE_RUN_NAME renders it.
RUN_NAME = re.compile(r"(May Playground|May) · (iOS|Android) · build ([1-9][0-9]{0,8}) at ([0-9a-f]{40})"
                      r"( \(dry run\))?( \(sign only\))?")
APPS = {"playground": "May Playground", "production": "May"}


def checked_notes(text):
    """The notes when they are what compose makes (text, at most NOTES_LIMIT characters, no control characters but
    newlines), else None."""
    if not isinstance(text, str) or not 0 < len(text) <= NOTES_LIMIT or CONTROL.search(text.replace("\n", "")):
        return None
    return text


def previous_build(runs, target, build_number, source_sha):
    """The commit of the newest successful iOS upload of target before build_number, from store-release-draft.yml's
    runs ({display_title, status, conclusion}), or None. Dry runs, sign-only runs, other targets and this commit are
    not previous builds."""
    best = None
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        match = RUN_NAME.fullmatch(str(run.get("display_title") or ""))
        if not match or match.group(1) != APPS[target] or match.group(2) != "iOS" or match.group(5) or match.group(6):
            continue
        number, sha = int(match.group(3)), match.group(4)
        if number < build_number and sha != source_sha and (best is None or number > best[0]):
            best = (number, sha)
    return best[1] if best else None


def area_of(title):
    """(area, rest): the text before the first colon, when it is short enough to be an area, and the rest."""
    head, colon, rest = title.partition(":")
    if colon and 0 < len(head.strip()) <= 40 and rest.strip():
        return head.strip(), rest.strip()
    return "Other", title.strip()


def compose(titles):
    """The notes for these pull-request titles (newest first): grouped by area in the order each area first
    appears, one "Try:" line each, cut whole lines short of NOTES_LIMIT with a count of what was left out."""
    cleaned = []
    for title in titles:
        title = PULL_NUMBER.sub("", CONTROL.sub(" ", str(title))).strip()
        if title:
            cleaned.append(title)
    if not cleaned:
        return NOTHING_NEW
    groups = {}
    for title in cleaned:
        area, rest = area_of(title)
        groups.setdefault(area, []).append(rest)
    lines = []
    for area, items in groups.items():
        lines += [f"Try: {area}: {item}" for item in items]
    text = "\n".join(lines)
    if len(text) <= NOTES_LIMIT:
        return text
    # Room for the closing "…and N more" line, then whole lines only.
    kept, size = [], len(f"\n…and {len(lines)} more")
    for line in lines:
        if size + len(line) + 1 > NOTES_LIMIT:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept + [f"…and {len(lines) - len(kept)} more"])


# ---- compose (ios-build, GITHUB_TOKEN alone) and send (ios-release, the App Store Connect key alone) ----

NOISE = re.compile(r"\[skip[^\]]*\]|\bco-authored-by:.*$", re.IGNORECASE)
MAX_TITLES = 200
STORE_PAGES = 5
LOCALE = "en-US"
# Apple keeps a build's record a little after its upload is accepted; the notes wait this long for it.
BUILD_WAIT_SECONDS, BUILD_POLL_SECONDS = 15 * 60, 30
LOCALIZATIONS = "betaBuildLocalizations"


def cleaned(subject):
    """A commit subject as testers read it: its first line, without [skip …] marks or Co-Authored-By trailers."""
    first = str(subject).splitlines()[0] if str(subject).strip() else ""
    return " ".join(NOISE.sub(" ", first).split())


def titles(source, previous, cut, run_git):
    """(titles newest first, whether they are since the previous build): the first-parent subjects of may-app's
    main from previous (exclusive) to cut, or the newest FALLBACK_COUNT when previous is None or not an ancestor."""
    since = previous is not None and run_git(["merge-base", "--is-ancestor", previous, cut], source)[0] == 0
    span = [f"{previous}..{cut}", "-n", str(MAX_TITLES)] if since else [cut, "-n", str(FALLBACK_COUNT)]
    code, out = run_git(["log", "--first-parent", "--format=%s", *span], source)
    if code:
        raise StoreError("may-app's history could not be read for the TestFlight notes")
    return [t for t in (cleaned(line) for line in out.splitlines()) if t], since


def git_in(argv, source):
    result = subprocess.run(["git", "-C", str(source), *argv], capture_output=True, stdin=subprocess.DEVNULL,
                            env=child_env(GIT_TERMINAL_PROMPT="0"), timeout=120)
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def store_runs(token, get):
    runs = []
    for page in range(1, STORE_PAGES + 1):
        status, body = get(f"{RUNS_PATH}?per_page=100&page={page}", token)
        try:
            batch = json.loads(body).get("workflow_runs") if status == 200 else None
        except (ValueError, AttributeError):
            batch = None
        if not isinstance(batch, list):
            raise StoreError("store-release-draft.yml's runs could not be read for the TestFlight notes")
        runs += batch
        if len(batch) < 100:
            break
    return runs


def compose_step(target, source_sha, build_number, get=None, run_git=git_in):
    identity("ios", target, source_sha, build_number)
    require_step_secrets(())
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise StoreError("GitHub metadata token unavailable for the TestFlight notes")
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or source.is_symlink() or not private.is_dir():
        raise StoreError("The exact may-app source is not prepared; the TestFlight notes run after prepare")
    previous = previous_build(store_runs(token, get or control_get), target, int(build_number), source_sha)
    found, since = titles(source, previous, source_sha, run_git)
    notes = compose(found)
    (private / NOTES_FILE).unlink(missing_ok=True)
    write_private(private / NOTES_FILE, notes.encode("utf-8"))
    count = int(len(found))
    if since:
        print(f"May TestFlight notes: {count} pull requests since the previous build, sealed with the build")
    else:
        print(f"May TestFlight notes: no earlier build to compare with; the newest {count} pull requests, "
              "sealed with the build")


def asc_send(token, method, url, payload, opener=None):
    """One write of a build's TestFlight notes: (HTTP status, parsed JSON or None). Anything but a POST to
    betaBuildLocalizations or a PATCH of one is refused before it is sent."""
    if not ((method == "POST" and url == ASC_API + LOCALIZATIONS) or
            (method == "PATCH" and re.fullmatch(re.escape(ASC_API + LOCALIZATIONS + "/") + r"[A-Za-z0-9-]{1,80}", url))):
        raise StoreError("The TestFlight notes write nothing but a build's beta localization")
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with (opener or OPENER).open(request, timeout=30) as reply:
            status, body = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def build_id(token, app, build_number, get, sleep, clock):
    """The App Store Connect build of this app and build number, waiting up to BUILD_WAIT_SECONDS for its record."""
    deadline = clock() + BUILD_WAIT_SECONDS
    while True:
        status, body = get(token, f"{ASC_API}builds?filter[app]={app}&filter[version]={build_number}&limit=1")
        data = body.get("data") if status == 200 and isinstance(body, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict) and \
                re.fullmatch(r"[A-Za-z0-9-]{1,80}", str(data[0].get("id"))):
            return data[0]["id"], None
        if status == 403:
            return None, 403
        if clock() >= deadline:
            raise StoreError("The uploaded build did not appear in App Store Connect in time for its notes")
        sleep(BUILD_POLL_SECONDS)


def send_step(target, source_sha, build_number, get=asc_get, send=asc_send, sleep=time.sleep, clock=time.monotonic):
    identity("ios", target, source_sha, build_number)
    require_step_secrets(IOS_UPLOAD_SECRETS)
    if os.environ.get("MAY_DRY_RUN") != "false":
        raise StoreError("Dry run: no TestFlight notes are sent")
    if os.environ.get("MAY_ASC_KEY_ID") != APPLE_KEY_ID:
        raise StoreError("Approved Apple team key ID differs")
    issuer = os.environ.get("MAY_ASC_ISSUER_ID", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", issuer):
        raise StoreError("App Store Connect issuer metadata is invalid")
    private = runner_root() / "may-private"
    path = private / NOTES_FILE
    try:
        notes = checked_notes(path.read_text(encoding="utf-8")) if not path.is_symlink() else None
    except (OSError, UnicodeDecodeError):
        notes = None
    if notes is None:
        raise StoreError("The TestFlight notes did not arrive in the handoff")
    key = secret_bytes("MAY_ASC_KEY_BASE64")
    if not key.startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise StoreError("App Store Connect key is not a PEM private key")
    keys = private / "asc-notes-keys"
    shutil.rmtree(keys, ignore_errors=True)
    keys.mkdir(mode=0o700)
    try:
        key_path = keys / ("AuthKey_" + APPLE_KEY_ID + ".p8")
        write_private(key_path, key)
        token = asc_token(key_path, issuer)
    finally:
        shutil.rmtree(keys, ignore_errors=True)
    build, refused = build_id(token, ASC_APP_IDS[target], build_number, get, sleep, clock)
    if refused == 403:
        print("May TestFlight notes: the App Store Connect key has no write access to builds; no notes were set")
        return
    status, body = get(token, f"{ASC_API}builds/{build}/{LOCALIZATIONS}?limit=50")
    if status == 403:
        print("May TestFlight notes: the App Store Connect key has no write access to builds; no notes were set")
        return
    found = body.get("data") if status == 200 and isinstance(body, dict) else None
    if not isinstance(found, list):
        raise StoreError("The build's TestFlight localizations could not be read")
    existing = next((item.get("id") for item in found if isinstance(item, dict) and
                     (item.get("attributes") or {}).get("locale") == LOCALE), None)
    if existing is not None:
        status, _ = send(token, "PATCH", f"{ASC_API}{LOCALIZATIONS}/{existing}",
                         {"data": {"type": LOCALIZATIONS, "id": existing, "attributes": {"whatsNew": notes}}})
    else:
        status, _ = send(token, "POST", ASC_API + LOCALIZATIONS,
                         {"data": {"type": LOCALIZATIONS, "attributes": {"locale": LOCALE, "whatsNew": notes},
                                   "relationships": {"build": {"data": {"type": "builds", "id": build}}}}})
    if status == 403:
        print("May TestFlight notes: the App Store Connect key has no write access to builds; no notes were set")
        return
    if status not in (200, 201):
        raise StoreError("App Store Connect did not accept the TestFlight notes")
    number, lines = int(build_number), int(notes.count("\n") + 1)
    print(f"May TestFlight notes: What to test set on build {number} ({lines} lines)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("compose", "send"))
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    if args.step == "compose":
        compose_step(args.target, args.source_sha, args.build_number)
    else:
        send_step(args.target, args.source_sha, args.build_number)


if __name__ == "__main__":
    try:
        main()
    except (StoreError, ScheduleError) as error:
        raise SystemExit("May TestFlight notes stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May TestFlight notes stopped; no title was printed") from None
