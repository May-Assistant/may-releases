"""Upload the signed, verified May IPA for internal TestFlight.

The workflow step that runs this holds only the App Store Connect key and
issuer: no source read key and no signing material. It re-checks the IPA's
embedded profile team and bundle ID and its build number before upload.
TestFlight processing and group assignment need separate readback.
"""

import argparse
import os
import re
import shutil
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (APPLE_KEY_ID, APPLE_TEAM, IOS_UPLOAD_SECRETS, XCODE_DEVELOPER_DIR, StoreError,
                   child_env, identity, require_step_secrets, run_private, runner_root, secret_bytes,
                   write_private)
import verify


def upload(target, source_sha, build_number):
    app_id = identity("ios", target, source_sha, build_number)
    require_step_secrets(IOS_UPLOAD_SECRETS)
    if os.environ.get("MAY_ASC_KEY_ID") != APPLE_KEY_ID:
        raise StoreError("Approved Apple team key ID differs")
    issuer = os.environ.get("MAY_ASC_ISSUER_ID", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", issuer):
        raise StoreError("App Store Connect issuer metadata is invalid")
    root = runner_root()
    private, ipa = root / "may-private", root / "may-release/May.ipa"
    if not ipa.is_file() or ipa.is_symlink():
        raise StoreError("Signed iOS IPA is unavailable")
    verify.ipa_identity(ipa, APPLE_TEAM, app_id, build_number)
    key = secret_bytes("MAY_ASC_KEY_BASE64")
    if not key.startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise StoreError("App Store Connect key is not a PEM private key")
    keys = private / "asc-keys"
    keys.mkdir(mode=0o700)
    try:
        write_private(keys / ("AuthKey_" + APPLE_KEY_ID + ".p8"), key)
        env = child_env(DEVELOPER_DIR=XCODE_DEVELOPER_DIR, API_PRIVATE_KEYS_DIR=str(keys))
        run_private(["/usr/bin/xcrun", "altool", "--upload-app", "--file", str(ipa), "--type", "ios",
                     "--apiKey", APPLE_KEY_ID, "--apiIssuer", issuer],
                    private, private / "ios-upload.log", env)
    finally:
        shutil.rmtree(keys, ignore_errors=True)
    print("May IPA submitted for internal TestFlight; verify processing separately")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        upload(args.target, args.source_sha, args.build_number)
    except (StoreError, verify.VerifyError) as error:
        raise SystemExit("May TestFlight upload stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May TestFlight upload failed or is uncertain; inspect App Store Connect "
                         "before any retry") from None
