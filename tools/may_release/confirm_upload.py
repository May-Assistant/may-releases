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
   committed Play edit; for iOS, the App Store Connect build upload that
   ios_upload.py read back (an altool exit status alone is never an upload).

It then prints the upload report. It holds no secret and makes no network call.

With --sign-only (a May production sign_only run, which never uploads), it
instead confirms a "signed, not uploaded" outcome, and never reports an upload.
The workflow runs it right after the publish step whenever sign_only is true (and
the run was not cancelled). It refuses unless:

1. the publish step's outcome (MAY_PUBLISH_OUTCOME) is exactly `success`;
2. the run left no upload record (a sign-only run must never upload);
3. the published folder (store.SIGN_ONLY_DIR) holds exactly the encrypted bundle,
   its SHA-256 file and the sign-only record, and that record names this app,
   build number and mode, the SHA-256 of the signed bundle still on this runner
   and the SHA-256 of the encrypted file that was published.

The upload check (no --sign-only) likewise refuses a run that published a
sign-only bundle, so neither mode can report the other's outcome.
"""

import argparse
import json
import os
import re
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (IOS_TRACK, SIGN_ONLY_DIR, SIGN_ONLY_RECORD, UPLOAD_RECORD, StoreError, file_sha256, identity,
                   require_step_secrets, runner_root, upload_report)
import ios_upload
import play_upload
import sign_only

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
    if (runner_root() / SIGN_ONLY_DIR).exists():
        raise StoreError("This run published a sign-only bundle, which is never an upload; nothing was confirmed uploaded")
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
    if platform == "ios" and not (isinstance(store_id, str) and ios_upload.UPLOAD_ID.fullmatch(store_id)):
        raise StoreError("The upload record names no App Store Connect build upload; the upload is not confirmed")
    return upload_report(record)


def confirm_signed(platform, target, source_sha, build_number):
    """The sign-only report, or StoreError unless the signed bundle was published and nothing uploaded."""
    app_id = identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    if os.environ.get("MAY_PUBLISH_OUTCOME") != "success":
        raise StoreError("sign_only was true, but the signed bundle was not published (the publish step failed, "
                         "was skipped or was cancelled); nothing is ready for Play Console")
    if platform != "android":
        raise StoreError("Sign-only is Android only; nothing is ready for Play Console")
    root = runner_root()
    if (root / "may-release" / UPLOAD_RECORD).exists():
        raise StoreError("A sign-only run left an upload record; it must never upload")
    out, signed = root / SIGN_ONLY_DIR, root / "may-release" / ARTIFACTS["android"]
    if not out.is_dir() or out.is_symlink() or not signed.is_file() or signed.is_symlink():
        raise StoreError("The sign-only step left no signed bundle to publish; nothing is ready for Play Console")
    files = {path.name: path for path in out.iterdir()}
    if set(files) != sign_only.FILES or any(path.is_symlink() or not path.is_file() for path in files.values()):
        raise StoreError("The published sign-only folder holds something other than its three files")
    try:
        record = json.loads(files[SIGN_ONLY_RECORD].read_text())
    except (OSError, ValueError):
        raise StoreError("The sign-only record is unreadable; nothing is ready for Play Console") from None
    sha256 = file_sha256(signed)
    expected = {"mode": sign_only.MODE, "platform": platform, "app_id": app_id, "build_number": build_number,
                "sha256": sha256, "encrypted_sha256": file_sha256(files[sign_only.SEALED])}
    if record != expected or files[sign_only.HASH_FILE].read_text() != sign_only.hash_line(sha256):
        raise StoreError("The sign-only record does not match the signed bundle and the published file")
    return sign_only.report(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--sign-only", action="store_true")
    args = parser.parse_args()
    try:
        lines = (confirm_signed if args.sign_only else confirm)(
            args.platform, args.target, args.source_sha, args.build_number)
    except StoreError as error:
        raise SystemExit(("May sign-only check failed: " if args.sign_only else "May upload check failed: ") +
                         error.args[0]) from None
    except Exception:
        raise SystemExit("May sign-only check failed; nothing is ready for Play Console" if args.sign_only else
                         "May upload check failed; the requested upload is not confirmed") from None
    print("\n".join(["Signed, not uploaded: confirmed." if args.sign_only else "Requested upload confirmed."] + lines))
