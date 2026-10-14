"""Fail the release job unless the store upload a dispatch asked for really happened.

The workflow runs this right after the upload step whenever `dry_run` is false
(and the run was not cancelled), even after an earlier failure, so a dispatch
with dry_run: false can never end green without uploading. It refuses unless:

1. the upload step's outcome (MAY_UPLOAD_OUTCOME, from `steps.<id>.outcome`) is
   exactly `success`. A skipped step (for example one still held inert), a
   failed one, a cancelled one, or a missing step (an empty outcome) all refuse;
2. that step left its upload record (store.UPLOAD_RECORD) for exactly this
   platform, app, build number and track, and for the SHA-256 of the signed
   artifact still on this runner. For Android the record must also name the
   committed Play edit.

It then prints the upload report. It holds no secret and makes no network call.
"""

import argparse
import json
import os
import re
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (IOS_TRACK, UPLOAD_RECORD, StoreError, file_sha256, identity, require_step_secrets,
                   runner_root, upload_report)
import play_upload

ARTIFACTS = {"android": "signed.aab", "ios": "May.ipa"}
TRACKS = {"android": play_upload.TRACK, "ios": IOS_TRACK}
EDIT_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}")


def confirm(platform, target, source_sha, build_number):
    """The upload report, or StoreError unless the requested upload succeeded."""
    app_id = identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    if os.environ.get("MAY_UPLOAD_OUTCOME") != "success":
        raise StoreError("dry_run was false, but the store upload step did not succeed "
                         "(it failed, was skipped or was cancelled); nothing was confirmed uploaded")
    release = runner_root() / "may-release"
    artifact, path = release / ARTIFACTS[platform], release / UPLOAD_RECORD
    if not artifact.is_file() or artifact.is_symlink() or not path.is_file() or path.is_symlink():
        raise StoreError("The upload step left no upload record; the upload is not confirmed")
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        raise StoreError("The upload record is unreadable; the upload is not confirmed") from None
    expected = {"platform": platform, "app_id": app_id, "build_number": build_number,
                "track": TRACKS[platform], "sha256": file_sha256(artifact)}
    if not isinstance(record, dict) or set(record) != set(expected) | {"store_id"} or \
            any(record[key] != value for key, value in expected.items()):
        raise StoreError("The upload record does not match this run; the upload is not confirmed")
    store_id = record["store_id"]
    if platform == "android" and not (isinstance(store_id, str) and EDIT_ID.fullmatch(store_id)):
        raise StoreError("The upload record names no committed Play edit; the upload is not confirmed")
    if platform == "ios" and store_id is not None:
        raise StoreError("The upload record does not match this run; the upload is not confirmed")
    return upload_report(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        lines = confirm(args.platform, args.target, args.source_sha, args.build_number)
    except StoreError as error:
        raise SystemExit("May upload check failed: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May upload check failed; the requested upload is not confirmed") from None
    print("\n".join(["Requested upload confirmed."] + lines))
