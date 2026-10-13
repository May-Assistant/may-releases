"""First May internal-track upload, with fixed destination and quiet failures.

The caller must already hold Samuel's protected-environment approval. A failed
or uncertain commit is never retried automatically; inspect Play before retry.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote

from store import identity, play_identity, StoreError

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"


def checked_json(response):
    if not 200 <= response.status_code < 300:
        raise StoreError("Google Play request was rejected or uncertain")
    try:
        return response.json()
    except (ValueError, TypeError):
        raise StoreError("Google Play response was invalid") from None


def upload(target, source_sha, build_number, session=None):
    package = identity("android", target, source_sha, build_number)
    raw = os.environ.get("MAY_PLAY_JSON", "")
    play_identity(raw, os.environ.get("MAY_PLAY_ACCOUNT", ""))
    root_value = os.environ.get("RUNNER_TEMP")
    if not root_value:
        raise StoreError("Protected runner temp is unavailable")
    bundle = Path(root_value).resolve() / "may-release/signed.aab"
    if not bundle.is_file() or bundle.is_symlink() or bundle.stat().st_size == 0:
        raise StoreError("Signed Android App Bundle is unavailable")
    if session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        credentials = Credentials.from_service_account_info(json.loads(raw), scopes=[SCOPE])
        session = AuthorizedSession(credentials)
    path = quote(package, safe="")
    base = f"{API}/{path}/edits"
    created = checked_json(session.post(base, json={}, timeout=30))
    edit = created.get("id")
    if not isinstance(edit, str) or not edit or "/" in edit:
        raise StoreError("Google Play edit ID was invalid")
    tracks = checked_json(session.get(f"{base}/{edit}/tracks", timeout=30)).get("tracks", [])
    if not isinstance(tracks, list) or any(item.get("releases") for item in tracks):
        raise StoreError("Play already has a release; review an update separately")
    with bundle.open("rb") as stream:
        uploaded = checked_json(session.post(
            f"{UPLOAD_API}/{path}/edits/{edit}/bundles?uploadType=media",
            data=stream, headers={"Content-Type": "application/octet-stream"}, timeout=300))
    if str(uploaded.get("versionCode")) != build_number:
        raise StoreError("Google Play returned a different version code")
    checked_json(session.put(
        f"{base}/{edit}/tracks/internal",
        json={"track": "internal", "releases": [{"versionCodes": [build_number],
                                                "status": "completed"}]}, timeout=30))
    checked_json(session.post(
        f"{base}/{edit}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW",
        json={}, timeout=30))
    print("May App Bundle submitted to Google Play internal testing; verify processing separately")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        upload(args.target, args.source_sha, args.build_number)
    except Exception:
        # Provider/client exceptions can contain private response bodies or IDs.
        raise SystemExit("May Play upload failed or is uncertain; inspect the store before any retry") from None
