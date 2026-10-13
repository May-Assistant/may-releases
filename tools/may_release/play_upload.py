"""May internal-track update upload, with fixed destination and quiet failures.

Update mode only. Google Play accepts only `status: draft` releases through the
API until an app's first release has been rolled out, so each app's first
release is a manual Play Console upload. This adapter refuses an app without
one. Otherwise, inside one edit, it:

1. reads every track and requires the new versionCode to be greater than every
   versionCode on every track, and no more than MAX_VERSION_JUMP above it;
2. uploads the signed AAB and requires Play's returned versionCode and sha256
   to match the build number and the local file;
3. replaces only the `internal` track with one `completed` release, leaving
   every other track untouched;
4. commits with ERROR_IF_IN_REVIEW, so an in-review change is never cancelled.

Any failure deletes the edit. A failed or uncertain commit is never retried
automatically; inspect Play before retry. The caller must already hold
Samuel's protected-environment approval.
"""

import argparse
import hashlib
import json
import os
import re
from urllib.parse import quote

from store import (PLAY_UPLOAD_SECRETS, StoreError, identity, play_identity, require_step_secrets,
                   runner_root)

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
ROLLED_OUT = ("completed", "inProgress", "halted")
MAX_VERSION_JUMP = 1000


class PlayRefusal(StoreError):
    """A deliberate refusal before any change. Its message is a fixed literal."""


def checked_json(response):
    if not 200 <= response.status_code < 300:
        raise StoreError("Google Play request was rejected or uncertain")
    try:
        body = response.json()
    except (ValueError, TypeError):
        raise StoreError("Google Play response was invalid") from None
    if not isinstance(body, dict):
        raise StoreError("Google Play response was invalid")
    return body


def highest_version_code(tracks):
    """Highest versionCode on any track, refusing an app with no rolled-out release.

    A release made by the first manual Console upload is completed (or in
    progress or halted). Draft releases still count toward the maximum.
    """
    if not isinstance(tracks, list):
        raise StoreError("Google Play track list was invalid")
    codes, rolled_out = [], False
    for track in tracks:
        releases = (track.get("releases") or []) if isinstance(track, dict) else None
        if not isinstance(releases, list):
            raise StoreError("Google Play track list was invalid")
        for release in releases:
            if not isinstance(release, dict) or not isinstance(release.get("versionCodes") or [], list):
                raise StoreError("Google Play track list was invalid")
            rolled_out |= release.get("status") in ROLLED_OUT
            for code in release.get("versionCodes") or []:
                if not re.fullmatch(r"[0-9]{1,10}", str(code)):
                    raise StoreError("Google Play track list was invalid")
                codes.append(int(code))
    if not rolled_out or not codes:
        raise PlayRefusal("First release must be uploaded in Play Console.")
    return max(codes)


def check_version(build_number, current):
    number = int(build_number)
    if number <= current:
        raise PlayRefusal("Build number must be greater than every versionCode on every Play track.")
    if number - current > MAX_VERSION_JUMP:
        raise PlayRefusal("Build number is more than 1000 above the highest Play versionCode.")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def delete_edit(session, url):
    try:
        session.delete(url, timeout=30)
    except Exception:
        pass  # Best effort; an abandoned edit expires and is never committed.


def upload(target, source_sha, build_number, session=None):
    package = identity("android", target, source_sha, build_number)
    require_step_secrets(PLAY_UPLOAD_SECRETS)
    raw = os.environ.get("MAY_PLAY_JSON", "")
    play_identity(raw, os.environ.get("MAY_PLAY_ACCOUNT", ""))
    bundle = runner_root() / "may-release/signed.aab"
    if not bundle.is_file() or bundle.is_symlink() or bundle.stat().st_size == 0:
        raise StoreError("Signed Android App Bundle is unavailable")
    local_sha256 = file_sha256(bundle)
    if session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        credentials = Credentials.from_service_account_info(json.loads(raw), scopes=[SCOPE])
        session = AuthorizedSession(credentials)
    path = quote(package, safe="")
    base = f"{API}/{path}/edits"
    edit = checked_json(session.post(base, json={}, timeout=30)).get("id")
    if not isinstance(edit, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", edit):
        raise StoreError("Google Play edit ID was invalid")
    try:
        tracks = checked_json(session.get(f"{base}/{edit}/tracks", timeout=30)).get("tracks", [])
        check_version(build_number, highest_version_code(tracks))
        with bundle.open("rb") as stream:
            uploaded = checked_json(session.post(
                f"{UPLOAD_API}/{path}/edits/{edit}/bundles?uploadType=media",
                data=stream, headers={"Content-Type": "application/octet-stream"}, timeout=300))
        if str(uploaded.get("versionCode")) != build_number:
            raise StoreError("Google Play returned a different version code")
        if str(uploaded.get("sha256", "")).lower() != local_sha256:
            raise StoreError("Google Play returned a different bundle SHA-256")
        checked_json(session.put(
            f"{base}/{edit}/tracks/internal",
            json={"track": "internal", "releases": [{"versionCodes": [build_number],
                                                    "status": "completed"}]}, timeout=30))
        checked_json(session.post(
            f"{base}/{edit}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW",
            json={}, timeout=30))
    except BaseException:
        delete_edit(session, f"{base}/{edit}")
        raise
    print("May App Bundle submitted to Google Play internal testing; verify processing separately")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        upload(args.target, args.source_sha, args.build_number)
    except PlayRefusal as refusal:
        raise SystemExit("May Play upload refused before any change: " + refusal.args[0]) from None
    except StoreError as error:
        raise SystemExit("May Play upload stopped: " + error.args[0] +
                         "; inspect the store before any retry") from None
    except Exception:
        # Provider/client exceptions can contain private response bodies or IDs.
        raise SystemExit("May Play upload failed or is uncertain; inspect the store before any retry") from None
