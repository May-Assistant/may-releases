"""Protected, quiet adapter for the reviewed May app release scripts.

This file does not retrieve credentials. It runs only after gate.py, a human
environment review, and prepare.py's exact private main-history checkout.
Provider output and private build diagnostics stay under RUNNER_TEMP.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

APP_IDS = {
    ("ios", "production"): "io.github.mayassistant.may",
    ("ios", "playground"): "io.github.mayassistant.may.playground",
    ("android", "production"): "io.github.mayassistant.may",
    ("android", "playground"): "io.github.mayassistant.may.playground",
}
APPLE_TEAM = "R6A892D599"
APPLE_CERT_SHA1 = "A4AB0BB2786164506FDB2538320703B41B7ED30A"
APPLE_KEY_ID = "23VX669FGY"
PLAY_ACCOUNT_DOMAIN = "@may-assistant-app.iam.gserviceaccount.com"


class StoreError(ValueError):
    pass


def identity(platform, target, source_sha, build_number):
    app_id = APP_IDS.get((platform, target))
    if not app_id or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise StoreError("Store target or exact source SHA is invalid")
    if not re.fullmatch(r"[1-9][0-9]{0,8}", build_number):
        raise StoreError("Store build number is invalid")
    return app_id


def play_identity(value, expected_account):
    if not expected_account.endswith(PLAY_ACCOUNT_DOMAIN):
        raise StoreError("Protected May Play account metadata is invalid")
    try:
        credential = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise StoreError("Protected May Play credential is invalid") from None
    if (credential.get("type") != "service_account" or
            credential.get("client_email") != expected_account or
            credential.get("project_id") != "may-assistant-app" or
            not credential.get("private_key")):
        raise StoreError("Protected May Play identity does not match")


def prepared(root, source_sha):
    source = root / "may-source"
    private = root / "may-private"
    ds = source / "android/build-deps/may-design-system"
    if not source.is_dir() or not private.is_dir() or not ds.is_dir():
        raise StoreError("Exact private checkout is not prepared")
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode or result.stdout.decode().strip() != source_sha:
        raise StoreError("Prepared source SHA differs")
    return source, private


def run_private(argv, cwd, log, env):
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise StoreError("Protected native release failed; inspect private runner diagnostics")


def release(platform, target, source_sha, build_number):
    app_id = identity(platform, target, source_sha, build_number)
    temp = os.environ.get("RUNNER_TEMP")
    if not temp:
        raise StoreError("Protected runner temp is unavailable")
    root = Path(temp).resolve()
    source, private = prepared(root, source_sha)
    script = source / "tools/release" / ("ios.sh" if platform == "ios" else "android.sh")
    if not script.is_file() or script.is_symlink():
        raise StoreError("Reviewed private release script is unavailable")
    env = os.environ.copy()
    env.update(MAY_TARGET=target, MAY_SOURCE_SHA=source_sha,
               MAY_BUILD_NUMBER=build_number, MAY_IOS_APP_ID=app_id,
               MAY_ANDROID_APP_ID=app_id, MAY_TEAM_ID=APPLE_TEAM,
               MAY_IOS_CERT_SHA1=APPLE_CERT_SHA1)
    # prepare.py has already fetched and verified the exact pinned DS locally.
    # This is intentionally not a GitHub credential. If a private script tries
    # another authenticated dependency fetch, that fetch must fail closed.
    env["MAY_DS_READ_TOKEN"] = "prepared-local-design-system-only"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if platform == "ios":
        if env.get("MAY_ASC_KEY_ID") != APPLE_KEY_ID:
            raise StoreError("Approved Apple team key ID differs")
    else:
        play_identity(env.get("MAY_PLAY_JSON"), env.get("MAY_PLAY_ACCOUNT", ""))
        # Signing needs the keystore, never the Play API private key.
        env.pop("MAY_PLAY_JSON", None)
        env.pop("MAY_PLAY_ACCOUNT", None)
    run_private(["bash", str(script)], source, private / (platform + "-release.log"), env)
    if platform == "android":
        bundles = list(root.glob("may-android.*/signed.aab"))
        if len(bundles) != 1 or not bundles[0].is_file() or bundles[0].is_symlink():
            raise StoreError("Expected exactly one signed Android App Bundle")
        out = root / "may-release"
        out.mkdir(mode=0o700, exist_ok=False)
        shutil.move(str(bundles[0]), str(out / "signed.aab"))
    print("Protected May store package prepared; provider acceptance requires separate readback")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        release(args.platform, args.target, args.source_sha, args.build_number)
    except (StoreError, OSError, subprocess.SubprocessError):
        raise SystemExit("Protected May release failed; no private detail was published") from None
