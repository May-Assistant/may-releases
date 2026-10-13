"""Protected, quiet unsigned build of the reviewed May app, plus shared helpers.

This step runs with no signing or upload secret in its environment. It runs
only after gate.py, a human environment review, and prepare.py's exact private
main-history checkout. It asks the private app's release script for an
unsigned product; this public repository signs it in sign.py and uploads it in
play_upload.py or ios_upload.py, each in its own workflow step that receives
only the secrets it needs.

Provider output and private build diagnostics stay under RUNNER_TEMP. Every
StoreError message is a fixed literal (static_check.py enforces this), so the
CLI prints it.
"""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
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
XCODE_DEVELOPER_DIR = "/Applications/Xcode_26.3.app/Contents/Developer"
# Play's hard limit is 2,100,000,000; stay clear of it.
MAX_BUILD_NUMBER = 2_000_000_000

# Every secret any May release step can hold. Each step names the subset it may
# see; a step that finds any other one present refuses to run.
SECRET_NAMES = (
    "MAY_APP_READ_KEY", "MAY_DS_READ_KEY",
    "MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD", "MAY_ANDROID_KEY_PASSWORD",
    "MAY_PLAY_JSON",
    "MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD", "MAY_IOS_PROFILE_BASE64",
    "MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID",
)
ANDROID_SIGNING_SECRETS = ("MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                           "MAY_ANDROID_KEY_PASSWORD")
IOS_SIGNING_SECRETS = ("MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD", "MAY_IOS_PROFILE_BASE64")
PLAY_UPLOAD_SECRETS = ("MAY_PLAY_JSON",)
IOS_UPLOAD_SECRETS = ("MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID")
# Only these reach a child tool. Nothing is inherited wholesale from os.environ.
PASS_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CI",
                "RUNNER_TEMP", "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT")
PASS_THROUGH_PREFIXES = ("JAVA_HOME_",)
# Private-script contract: subcommand and the single product it must leave in
# $MAY_ARTIFACT_DIR. See README "Private release script interface".
UNSIGNED_BUILDS = {
    "android": ("android.sh", "build-unsigned", "unsigned.aab"),
    "ios": ("ios.sh", "archive-unsigned", "May.xcarchive"),
}


class StoreError(ValueError):
    pass


def build_number_ok(value):
    return bool(re.fullmatch(r"[1-9][0-9]{0,9}", value or "")) and int(value) <= MAX_BUILD_NUMBER


def identity(platform, target, source_sha, build_number):
    app_id = APP_IDS.get((platform, target))
    if not app_id or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise StoreError("Store target or exact source SHA is invalid")
    if not build_number_ok(build_number):
        raise StoreError("Store build number is invalid")
    return app_id


def play_identity(value, expected_account):
    if not expected_account.endswith(PLAY_ACCOUNT_DOMAIN):
        raise StoreError("Protected May Play account metadata is invalid")
    try:
        credential = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise StoreError("Protected May Play credential is invalid") from None
    if not isinstance(credential, dict):
        raise StoreError("Protected May Play credential is not a JSON object")
    if (credential.get("type") != "service_account" or
            credential.get("client_email") != expected_account or
            credential.get("project_id") != "may-assistant-app" or
            not credential.get("private_key")):
        raise StoreError("Protected May Play identity does not match")


def require_step_secrets(allowed):
    """Refuse to run if a secret meant for another step is in this step's env."""
    if any(name in os.environ and name not in allowed for name in SECRET_NAMES):
        raise StoreError("A secret for another release step is present in this step")


def child_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k in PASS_THROUGH or k.startswith(PASS_THROUGH_PREFIXES)}
    env.update(extra)
    return env


def runner_root():
    temp = os.environ.get("RUNNER_TEMP")
    if not temp:
        raise StoreError("Protected runner temp is unavailable")
    return Path(temp).resolve()


def secret_bytes(name):
    try:
        value = base64.b64decode(os.environ.get(name, ""), validate=True)
    except (binascii.Error, ValueError):
        raise StoreError("A protected base64 secret is malformed") from None
    if not value:
        raise StoreError("A protected base64 secret is missing")
    return value


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def prepared(root, source_sha):
    source = root / "may-source"
    private = root / "may-private"
    ds = source / "android/build-deps/may-design-system"
    if not source.is_dir() or not private.is_dir() or not ds.is_dir():
        raise StoreError("Exact private checkout is not prepared")
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            capture_output=True, stdin=subprocess.DEVNULL, env=child_env())
    if result.returncode or result.stdout.decode().strip() != source_sha:
        raise StoreError("Prepared source SHA differs")
    return source, private


def run_private(argv, cwd, log, env):
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise StoreError("Protected native release failed; inspect private runner diagnostics")


def build(platform, target, source_sha, build_number):
    app_id = identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    script_name, command, product = UNSIGNED_BUILDS[platform]
    script = source / "tools/release" / script_name
    if not script.is_file() or script.is_symlink():
        raise StoreError("Reviewed private release script is unavailable")
    out = root / "may-release"
    out.mkdir(mode=0o700)
    env = child_env(MAY_TARGET=target, MAY_SOURCE_SHA=source_sha, MAY_BUILD_NUMBER=build_number,
                    MAY_APP_ID=app_id, MAY_ARTIFACT_DIR=str(out), GIT_TERMINAL_PROMPT="0",
                    # prepare.py has already fetched and verified the exact pinned DS
                    # locally. This is intentionally not a GitHub credential; another
                    # authenticated dependency fetch must fail closed.
                    MAY_DS_READ_TOKEN="prepared-local-design-system-only")
    if platform == "ios":
        env.update(MAY_IOS_APP_ID=app_id, MAY_TEAM_ID=APPLE_TEAM, DEVELOPER_DIR=XCODE_DEVELOPER_DIR)
    else:
        env.update(MAY_ANDROID_APP_ID=app_id)
    run_private(["bash", str(script), command], source, private / (platform + "-build.log"), env)
    made = out / product
    if [p.name for p in out.iterdir()] != [product] or made.is_symlink():
        raise StoreError("Unsigned build must leave exactly its one expected product")
    if not (made.is_dir() if platform == "ios" else made.is_file()):
        raise StoreError("Unsigned build must leave exactly its one expected product")
    print("Protected unsigned May build prepared; signing and upload are separate steps")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        build(args.platform, args.target, args.source_sha, args.build_number)
    except StoreError as error:
        raise SystemExit("Protected May build refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("Protected May build failed; no private detail was published") from None
