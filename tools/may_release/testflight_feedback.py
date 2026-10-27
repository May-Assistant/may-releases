#!/usr/bin/env python3
"""TestFlight beta feedback, filed as private may-app issues (testflight-feedback.yml).

Samuel sends "Share Beta Feedback" reports from TestFlight (coordinator, 27 Oct). Each hour this reads App Store
Connect's beta feedback, the screenshot and the crash submissions of May and May Playground, and files each new one
as an issue in the private May-Assistant/may-app, labelled testflight-feedback. This repository is public, so nothing
from a submission (its comment, device, build, screenshots or crash log, nor its ID) is printed, summarised or uploaded
here: the log carries fixed lines and counts alone, and the content lives in the runner's private folder until the
issue holds it.

- admit: the schedule or a manual dispatch of main, and may-testflight-feedback's policy (no required reviewer,
  administrator bypass off, main only).
- known (MAY_APP_ISSUES_TOKEN): the submission IDs already filed, from the label's issues (open and closed).
- read (MAY_ASC_FEEDBACK_KEY_BASE64, MAY_ASC_FEEDBACK_KEY_ID, MAY_ASC_ISSUER_ID: a Developer-role key of its own,
  never the release key): the submissions of the last LOOKBACK_DAYS not filed yet, oldest first and MAX_NEW at most,
  each with its screenshots (downloaded now: Apple's image URLs expire) and, for a crash, its crash log. Only GETs.
- store (MAY_APP_RENDERS_WRITE_KEY): the screenshots, into the private May-Assistant/may-renders under
  testflight-feedback/, where the issues link them (the GitHub API cannot attach an image to an issue).
- file (MAY_APP_ISSUES_TOKEN): one issue per submission, head-marked with its ID, so a later run files it never again.

Each credential is in its own step alone. A submission is never deleted in App Store Connect.
"""

import argparse
import base64
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ds_compile import DsCompileError, fence_for, request, run_url
from gate import CONTROL_REPO, GateDenial, get_json, validate_no_reviewer_environment
from ios_upload import ASC_API, ASC_APP_IDS, OPENER, asc_get, asc_token
from publish import PublishError, git, publish_tree, write_key
from store import StoreError, require_step_secrets, runner_root, secret_bytes, write_private

ENVIRONMENT = "may-testflight-feedback"
APP_REPO = "May-Assistant/may-app"
LABEL = "testflight-feedback"
MARKER = "testflight-feedback"
STATE = "may-testflight"
ISSUES_TOKEN = "MAY_APP_ISSUES_TOKEN"
KEY_SECRET, KEY_ID, ISSUER = "MAY_ASC_FEEDBACK_KEY_BASE64", "MAY_ASC_FEEDBACK_KEY_ID", "MAY_ASC_ISSUER_ID"
# A dedicated team key's ID (never the release key's, 23VX669FGY) and its issuer, as App Store Connect shows them.
ASC_KEY_ID = re.compile(r"[A-Z0-9]{10}")
ISSUER_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
RELEASE_KEY_ID = "23VX669FGY"
# What each run reads and files: the last LOOKBACK_DAYS, PAGE_LIMIT a page and MAX_PAGES pages of each list, and at
# most MAX_NEW new issues (the coordinator's cap, 27 Oct), oldest first, so a backlog drains in order.
LOOKBACK_DAYS, PAGE_LIMIT, MAX_PAGES, MAX_NEW = 14, 50, 4, 20
MAX_ISSUE_PAGES = 10
KINDS = {"screenshot": "betaFeedbackScreenshotSubmissions", "crash": "betaFeedbackCrashSubmissions"}
APPS = {"production": "May", "playground": "May Playground"}
SUBMISSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# The attributes shown in an issue, each as plain text cut to MAX_FIELD (the comment and crash log apart, in fences);
# the tester's email is never copied.
FIELDS = ("createdDate", "buildBundleId", "appPlatform", "devicePlatform", "deviceFamily", "deviceModel", "osVersion",
          "locale", "timeZone", "architecture", "connectionType", "batteryPercentage", "appUptimeInMilliseconds",
          "diskBytesAvailable", "diskBytesTotal", "screenWidthInPoints", "screenHeightInPoints", "pairedAppleWatch")
MAX_FIELD, MAX_COMMENT, MAX_LOG, MAX_BODY = 200, 8000, 40000, 60000
# Screenshots: Apple's signed image URLs, fetched over HTTPS from Apple's own hosts alone, PNG or JPEG, at most
# MAX_IMAGE bytes and MAX_IMAGES a submission.
IMAGE_HOST = re.compile(r"(?:[a-z0-9-]+\.)*(?:apple\.com|mzstatic\.com|cdn-apple\.com|icloud-content\.com)")
MAX_IMAGE, MAX_IMAGES = 8 * 1024 * 1024, 6
IMAGE_TYPES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"))
RENDERS_PREFIX = "testflight-feedback/"
RENDERS_BLOB = "https://github.com/May-Assistant/may-renders/blob/main/" + RENDERS_PREFIX
# The only GitHub calls: the label's issues, a page at a time, and a new issue.
ALLOWED_CALLS = (("GET", r"/repos/May-Assistant/may-app/issues\?labels=testflight-feedback&state=all&per_page=100"
                         r"&page=(?:[1-9]|10)"),
                 ("POST", r"/repos/May-Assistant/may-app/issues"))


class FeedbackError(ValueError):
    pass


def check_run():
    """The schedule or a manual dispatch, of this repository, from main; anything else is refused."""
    run = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"), os.environ.get("GITHUB_EVENT_NAME")
    if run[:2] != (CONTROL_REPO, "refs/heads/main") or run[2] not in ("schedule", "workflow_dispatch"):
        raise GateDenial("May control must run by its schedule or manual dispatch, from its main branch")


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
    print("May TestFlight feedback admission passed")


def state(fresh=False):
    path = runner_root() / STATE
    if fresh:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(mode=0o700)
    elif not path.is_dir():
        raise FeedbackError("This run's earlier step left no state")
    return path


def read_json(path, default):
    try:
        return json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else default
    except (OSError, ValueError):
        return default


def save_json(path, value):
    path.unlink(missing_ok=True)
    write_private(path, json.dumps(value).encode())


def github(method, path, payload, token):
    """One may-app issues call: (status, parsed JSON or None). Only ALLOWED_CALLS are made; nothing is printed."""
    if not any(method == allowed and re.fullmatch(pattern, path) for allowed, pattern in ALLOWED_CALLS):
        raise FeedbackError("Only may-app's testflight-feedback issues are reachable")
    return request(method, path, payload, token)


def issues_token():
    require_step_secrets((ISSUES_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(ISSUES_TOKEN, "")
    if token and len(token) < 20:
        raise FeedbackError("The may-app issues token is malformed (MAY_APP_ISSUES_TOKEN)")
    return token


def marked_ids(body):
    return re.findall(r"<!-- " + MARKER + r" id=([A-Za-z0-9_-]{1,100}) -->", body if isinstance(body, str) else "")


def known(call_api=github):
    token = issues_token()
    folder = state(fresh=True)
    if not token:
        print("May TestFlight feedback: MAY_APP_ISSUES_TOKEN is not set in may-testflight-feedback; nothing will be "
              "read or filed")
        return
    ids = set()
    for page in range(1, MAX_ISSUE_PAGES + 1):
        status, issues = call_api("GET", f"/repos/{APP_REPO}/issues?labels={LABEL}&state=all&per_page=100&page={page}",
                                  None, token)
        if status != 200 or not isinstance(issues, list):
            raise FeedbackError("may-app's testflight-feedback issues could not be read; nothing will be filed")
        for issue in issues:
            if isinstance(issue, dict):
                ids.update(marked_ids(issue.get("body")))
        if len(issues) < 100:
            break
    save_json(folder / "known.json", sorted(ids))
    print(f"May TestFlight feedback: {len(ids)} submissions already filed")


def parse_time(value):
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return moment if moment.tzinfo else None


def plain(value, limit=MAX_FIELD):
    """A field as one short line of plain text: no newline, backquote, pipe or angle bracket, cut to limit."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = re.sub(r"[\x00-\x1f\x7f`|<>]", " ", str(value)).strip()
    return text[:limit] or None


def fetch_image(url, opener=OPENER):
    """(bytes, extension) of one screenshot, or None: HTTPS on Apple's own hosts alone, PNG or JPEG, MAX_IMAGE at most.
    Apple's URL is signed, so no token goes with it."""
    match = re.fullmatch(r"https://([a-z0-9.-]{1,200})(/[^\s]*)?", url) if isinstance(url, str) else None
    if not match or not IMAGE_HOST.fullmatch(match.group(1)):
        return None
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=30) as reply:
            data = reply.read(MAX_IMAGE + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if len(data) > MAX_IMAGE:
        return None
    return next(((data, extension) for magic, extension in IMAGE_TYPES if data.startswith(magic)), None)


def submissions(get, token, app_id, kind, since):
    """The submissions of one kind for one app created at or after since, newest first, MAX_PAGES pages at most."""
    found, url = [], f"{ASC_API}apps/{app_id}/{KINDS[kind]}?sort=-createdDate&limit={PAGE_LIMIT}"
    for _ in range(MAX_PAGES):
        status, body = get(token, url)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise FeedbackError("App Store Connect beta feedback could not be read (check the key's role: Admin, "
                                "App Manager or Developer)")
        older = False
        for item in body["data"]:
            attributes = item.get("attributes") if isinstance(item, dict) else None
            created = parse_time(attributes.get("createdDate")) if isinstance(attributes, dict) else None
            if created is None or not SUBMISSION_ID.fullmatch(str(item.get("id", ""))):
                continue
            if created < since:
                older = True
                continue
            found.append((created, item))
        following = (body.get("links") or {}).get("next") if isinstance(body.get("links"), dict) else None
        if older or not isinstance(following, str) or not following.startswith(ASC_API):
            break
        url = following
    return found


def asc_key(folder):
    """The dedicated feedback key, written private for openssl, and its ID and issuer, or None when unset."""
    key_id, issuer = os.environ.pop(KEY_ID, "").strip(), os.environ.pop(ISSUER, "").strip()
    if not os.environ.get(KEY_SECRET):
        os.environ.pop(KEY_SECRET, None)
        return None
    key = secret_bytes(KEY_SECRET)
    os.environ.pop(KEY_SECRET, None)
    if not ASC_KEY_ID.fullmatch(key_id) or key_id == RELEASE_KEY_ID:
        raise FeedbackError("The feedback key's ID is not a dedicated App Store Connect key ID (MAY_ASC_FEEDBACK_KEY_ID)")
    if not ISSUER_ID.fullmatch(issuer):
        raise FeedbackError("The App Store Connect issuer ID is malformed (MAY_ASC_ISSUER_ID)")
    if b"-----BEGIN PRIVATE KEY-----" not in key:
        raise FeedbackError("The feedback key is not an App Store Connect .p8 key (MAY_ASC_FEEDBACK_KEY_BASE64)")
    path = folder / "asc.p8"
    path.unlink(missing_ok=True)
    write_private(path, key)
    return path, key_id, issuer


def read(get=asc_get, image=fetch_image, now=None):
    require_step_secrets((KEY_SECRET, ISSUER))
    folder = state()
    known_ids = read_json(folder / "known.json", None)
    if not isinstance(known_ids, list):
        os.environ.pop(KEY_SECRET, None)
        print("May TestFlight feedback: no filed submissions were listed, so App Store Connect is not read")
        return
    signing = asc_key(folder)
    if signing is None:
        print("May TestFlight feedback: MAY_ASC_FEEDBACK_KEY_BASE64 is not set in may-testflight-feedback; nothing read")
        return
    key, key_id, issuer = signing
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        token = asc_token(key, issuer, kid=key_id)
    finally:
        key.unlink(missing_ok=True)
    since, seen, found = moment - datetime.timedelta(days=LOOKBACK_DAYS), set(known_ids), []
    for target, app_id in sorted(ASC_APP_IDS.items()):
        for kind in KINDS:
            for created, item in submissions(get, token, app_id, kind, since):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    found.append((created, target, kind, item))
    found.sort(key=lambda entry: entry[0])
    records, skipped_images = [], 0
    for created, target, kind, item in found[:MAX_NEW]:
        attributes = item.get("attributes") or {}
        record = {"id": item["id"], "kind": kind, "app": APPS[target],
                  "fields": {name: plain(attributes.get(name)) for name in FIELDS if plain(attributes.get(name))},
                  "comment": attributes.get("comment")[:MAX_COMMENT] if isinstance(attributes.get("comment"), str) else "",
                  "images": [], "log": ""}
        shots = attributes.get("screenshots") if isinstance(attributes.get("screenshots"), list) else []
        for number, shot in enumerate(shots[:MAX_IMAGES], 1):
            fetched = image(shot.get("url")) if isinstance(shot, dict) else None
            if fetched is None:
                skipped_images += 1
                continue
            data, extension = fetched
            name = f"{number}.{extension}"
            (folder / "images" / item["id"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            write_private(folder / "images" / item["id"] / name, data)
            record["images"].append(name)
        skipped_images += max(0, len(shots) - MAX_IMAGES)
        if kind == "crash":
            status, body = get(token, f"{ASC_API}betaFeedbackCrashSubmissions/{item['id']}/crashLog")
            text = (((body or {}).get("data") or {}).get("attributes") or {}).get("logText") \
                if status == 200 and isinstance(body, dict) else None
            record["log"] = text[:MAX_LOG] if isinstance(text, str) else ""
        records.append(record)
    save_json(folder / "records.json", records)
    print(f"May TestFlight feedback: {len(found)} new submissions, {len(records)} read this run "
          f"({len(found) - len(records)} left for later runs, {skipped_images} screenshots not fetched)")


def store(run_git=git):
    require_step_secrets(("MAY_APP_RENDERS_WRITE_KEY",))
    key_text = write_key()
    folder = state()
    images = folder / "images"
    if not images.is_dir() or not any(images.iterdir()):
        print("May TestFlight feedback: no screenshots to store")
        return
    if not key_text:
        print("May TestFlight feedback: MAY_APP_RENDERS_WRITE_KEY is not set in may-testflight-feedback; the issues "
              "will not link screenshots")
        return
    run = os.environ.get("GITHUB_RUN_ID", "")
    run = run if re.fullmatch(r"[1-9][0-9]{0,19}", run) else "local"
    entry = {"folder": run, "run": run}
    publish_tree(images, RENDERS_PREFIX, run, entry, f"TestFlight feedback screenshots, run {run}", key_text, run_git,
                 keep=None)
    save_json(folder / "stored.json", {"run": run})
    count = sum(1 for path in images.rglob("*") if path.is_file())
    print(f"May TestFlight feedback: {count} screenshots stored in may-renders")


def issue_body(record, run):
    lines = [f"### TestFlight feedback: {record['kind']} ({record['app']})", ""]
    for name, value in record["fields"].items():
        lines.append(f"- **{name}**: {value}")
    if record["comment"]:
        fence = fence_for([record["comment"]])
        lines += ["", "**Tester's comment**", "", fence, record["comment"], fence]
    if record["images"]:
        lines += ["", "**Screenshots**", ""]
        if run:
            lines += [f"- [{name}]({RENDERS_BLOB}{run}/{record['id']}/{name}?raw=true)" for name in record["images"]]
        else:
            lines.append(f"{len(record['images'])} screenshots were fetched but could not be stored.")
    if record["log"]:
        room = MAX_BODY - len("\n".join(lines)) - 400
        log = record["log"][:max(0, room)]
        fence = fence_for([log])
        lines += ["", "**Crash log**" + (" (cut to fit)" if len(log) < len(record["log"]) else ""), "", fence, log,
                  fence]
    url = run_url(os.environ)
    if url:
        lines += ["", f"Filed by {url}"]
    return "\n".join(lines) + f"\n\n<!-- {MARKER} id={record['id']} -->\n"


def file_issues(call_api=github):
    token = issues_token()
    folder = state()
    records = read_json(folder / "records.json", [])
    if not records:
        print("May TestFlight feedback: nothing new to file")
        return
    if not token:
        print("May TestFlight feedback: MAY_APP_ISSUES_TOKEN is not set in may-testflight-feedback; nothing filed")
        return
    run = (read_json(folder / "stored.json", {}) or {}).get("run")
    filed = 0
    for record in records[:MAX_NEW]:
        title = f"TestFlight feedback: {record['kind']} in {record['app']}"
        status, _ = call_api("POST", f"/repos/{APP_REPO}/issues",
                             {"title": title, "body": issue_body(record, run), "labels": [LABEL]}, token)
        if status != 201:
            raise FeedbackError("A may-app issue could not be filed; the ones not filed are read again next run")
        filed += 1
    print(f"May TestFlight feedback: {filed} issues filed in may-app")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("admit", "known", "read", "store", "file"))
    args = parser.parse_args()
    {"admit": admit, "known": known, "read": read, "store": store, "file": file_issues}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May TestFlight feedback denied: " + denial.args[0]) from None
    except (FeedbackError, DsCompileError, PublishError, StoreError) as error:
        raise SystemExit("May TestFlight feedback stopped: " + error.args[0]) from None
    except OSError:
        raise SystemExit("May TestFlight feedback stopped; no feedback, path or credential was printed") from None
    except Exception:
        raise SystemExit("May TestFlight feedback stopped; check may-testflight-feedback, its secrets and variables")
