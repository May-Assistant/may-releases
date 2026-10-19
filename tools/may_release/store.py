"""Protected, quiet unsigned build of the reviewed May app, plus shared helpers.

This runs in the `build` job, which holds only the read-only source keys (in an
earlier step) and never a signing or upload secret. It runs after gate.py, a
human review of `may-native-compile`, and prepare.py's exact private
main-history checkout. It asks the private app's release script for an unsigned
product; handoff.py then encrypts it to the target's committed public key for
the separate `release` job, which runs on a fresh VM and signs (sign.py) and
uploads (play_upload.py or ios_upload.py).

Provider output and private build diagnostics stay under RUNNER_TEMP. Every
StoreError message is a fixed literal (static_check.py enforces this), so the
CLI prints it.
"""

import argparse
import base64
import binascii
import hashlib
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
# The one list of targets whose iOS and Android design-system pins may differ
# (design_system_pins). Only the Playground apps (Samuel's decision, fail closed).
# The real May app (`production`) is left out: its internal-track or TestFlight
# build can be promoted to production by hand in Play Console or App Store
# Connect, where no control here runs, so its pins must match. It is
# deliberately not derived from APP_IDS, so a new target needs matching pins
# until someone adds it here on purpose.
INTERNAL_TARGETS = frozenset({("ios", "playground"), ("android", "playground")})
APPLE_TEAM = "R6A892D599"
APPLE_CERT_SHA1 = "A4AB0BB2786164506FDB2538320703B41B7ED30A"
APPLE_KEY_ID = "23VX669FGY"
PLAY_ACCOUNT_DOMAIN = "@may-assistant-app.iam.gserviceaccount.com"
XCODE_DEVELOPER_DIR = "/Applications/Xcode_26.3.app/Contents/Developer"
# Play's hard limit is 2,100,000,000; stay clear of it.
MAX_BUILD_NUMBER = 2_000_000_000
# Stage 3: the Play uploads this repository may make, as target -> the one track
# that upload writes. Only "internal" may appear (static_check.py pins this table
# exactly for the stage), and there is no track input, so no production, beta or
# alpha track is reachable. Samuel's decision (2026-10-14): the real May app
# (`production`) may be uploaded to its Play internal track only, after a passing
# production dry run; promoting a build from internal to production stays a
# manual step in Play Console, where no control here runs. gate.py refuses
# dry_run: false for any other target at admission, and play_upload.py refuses it
# again before any request.
PLAY_UPLOAD_TRACKS = {"playground": "internal", "production": "internal"}
# Each Android target's own upload key and Play service account, fixed in reviewed
# code so that one target's key, certificate or credential can never stand in for
# the other's (check_android_upload_key, check_android_signer, play_account).
# alias: the keystore alias sign.py passes to jarsigner (MAY_ANDROID_KEY_ALIAS must
#   equal it).
# cert: the upload certificate's SHA-256, lowercase hex. MAY_ANDROID_CERT_SHA256
#   and the signed bundle's sole signer must equal it. Production's full value is
#   recorded (may-app docs/engineering/merge-deploy-plan.md, the pinned
#   may-upload SHA-256). Playground's full value is not recorded in a reviewed
#   file, so its entry is the recorded prefix of that distinct key, and the full
#   value stays the environment's MAY_ANDROID_CERT_SHA256, which the signer must
#   equal exactly. Replace the prefix with the full value once it is recorded.
# account: the Play service account; MAY_PLAY_ACCOUNT must equal it.
ANDROID_UPLOAD_KEYS = {
    "production": {"alias": "may-upload",
                   "cert": "9e2fe9c8e70421fcbfbbfb4ea8f30942915ecfbd8ffef18989a309fc1e899815",
                   "account": "may-play-upload@may-assistant-app.iam.gserviceaccount.com"},
    "playground": {"alias": "may-playground-upload",
                   "cert": "db087802f802f2a1",
                   "account": "may-playground-upload@may-assistant-app.iam.gserviceaccount.com"},
}
# The lowest build number (versionCode) each store app may use here. May
# production's first release is versionCode 2 (0.1.0), signed on Samuel's machine
# and uploaded by hand to the internal track on 14 Oct; workflow uploads start at
# 3, so the floor keeps every later build above vc2 (and above vc1, which was
# built but never uploaded) even before Play's own check sees it. That is stricter than
# may-app's tools/release/build-number-floors (production 1), which android.sh
# applies in the build job. gate.py refuses a lower one at admission (dry runs
# included), and dry_run.py, sign_only.py and play_upload.py refuse it again;
# Play's own check (greater than every versionCode on every track) still applies
# on upload.
MIN_BUILD_NUMBERS = {("android", "production"): 3}
# Sign-only (stage 3): the targets whose android-sign-only job may publish the
# signed bundle, encrypted, for a manual Play Console upload instead of uploading
# it: a CI-signed bundle without the upload keystore touching any other machine,
# for a build that has to go through Console by hand. Only the real May app.
SIGN_ONLY_TARGETS = frozenset({("android", "production")})
# The folder sign_only.py writes and the workflow publishes (3-day artifact):
# the encrypted signed bundle, its SHA-256 file and SIGN_ONLY_RECORD. Nothing else.
SIGN_ONLY_DIR = "may-signed"
SIGN_ONLY_RECORD = "sign-only-record.json"
# The repository is public, so its Actions artifacts are readable by anyone: the
# published bundle is encrypted with age to this committed public key
# (artifact_keys/<name>.age.pub), whose private half only Samuel holds. Admission
# denies sign_only while it is a placeholder.
SIGN_ONLY_RECIPIENT = "may-android-console-upload"
# The only TestFlight destination ios_upload.py's export can reach
# (testFlightInternalTestingOnly); reported by dry_run.py and confirm_upload.py.
IOS_TRACK = "TestFlight internal testing only"
# Written under may-release by play_upload.py or ios_upload.py once an upload has
# succeeded; confirm_upload.py requires it whenever dry_run is false.
UPLOAD_RECORD = "upload-record.json"

# Every secret any May release step can hold. Each step names the subset it may
# see; a step that finds any other one present refuses to run.
SECRET_NAMES = (
    "MAY_APP_READ_KEY", "MAY_DS_READ_KEY",
    "MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD", "MAY_ANDROID_KEY_PASSWORD",
    "MAY_PLAY_JSON",
    "MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD", "MAY_IOS_PROFILE_BASE64",
    "MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID",
    "MAY_ARTIFACT_DECRYPT_KEY",
    "MAY_POSTHOG_ANDROID_KEY", "MAY_POSTHOG_IOS_KEY",
    # The repository secret gate.py reads may-app pull requests with (compile-pr); no tool step may hold it.
    "MAY_APP_PR_READ_TOKEN",
    # ds-swift-compile.yml: the may-design-system pull-request read token (gate.py --check-pull alone) and the token
    # that comments on that pull request (ds_compile.py post alone). No other step may hold either.
    "MAY_DS_PR_READ_TOKEN", "MAY_DS_PR_COMMENT_TOKEN",
    # native-compile-pr.yml: the token that comments a compile's errors on the may-app pull request
    # (compile_pr_errors.py alone). No other step may hold it.
    "MAY_APP_PR_COMMENT_TOKEN",
)
SOURCE_READ_SECRETS = ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY")
# Crash reports (PostHog): each platform's build step alone may hold its project key, and
# hands it to its build script under the same name: android.sh, where Gradle compiles it
# into the app (may-app's app/build.gradle.kts reads MAY_POSTHOG_ANDROID_KEY and
# MAY_POSTHOG_HOST), or ios.sh, which gives it to xcodebuild archive as a build setting the
# app's Info.plist reads (may-app's tools/release/ios.sh takes MAY_POSTHOG_IOS_KEY and
# MAY_POSTHOG_IOS_HOST). Every other step refuses both keys, the other platform's build
# step included. Without its key an app is built with none and sends no crash report.
# The host is a variable, not a secret: PostHog's ingestion host for the project.
# GitHub resolves a variable (and a secret) from the environment, then the repository,
# then the organization, so the host is pinned here: crash reports can only ever go to
# PostHog's US or EU cloud, whoever sets the variable. Unset means PostHog's US cloud.
# platform -> (its key secret, its host variable).
POSTHOG_NAMES = {"android": ("MAY_POSTHOG_ANDROID_KEY", "MAY_POSTHOG_HOST"),
                 "ios": ("MAY_POSTHOG_IOS_KEY", "MAY_POSTHOG_IOS_HOST")}
POSTHOG_SECRETS = tuple(key for key, _ in POSTHOG_NAMES.values())
POSTHOG_KEY = re.compile(r"phc_[A-Za-z0-9_-]+")
POSTHOG_HOSTS = ("https://us.i.posthog.com", "https://eu.i.posthog.com")
HANDOFF_SECRETS = ("MAY_ARTIFACT_DECRYPT_KEY",)
ANDROID_SIGNING_SECRETS = ("MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                           "MAY_ANDROID_KEY_PASSWORD")
IOS_SIGNING_SECRETS = ("MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD", "MAY_IOS_PROFILE_BASE64")
PLAY_UPLOAD_SECRETS = ("MAY_PLAY_JSON",)
IOS_UPLOAD_SECRETS = ("MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID")
# prepare.py writes this under may-private; handoff.py seals it into the manifest.
DESIGN_SYSTEM_RECORD = "design-system-pins.json"
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


def build_number_floor_ok(platform, target, build_number):
    """False when the build number is below this store app's floor (MIN_BUILD_NUMBERS)."""
    return int(build_number) >= MIN_BUILD_NUMBERS.get((platform, target), 1)


def android_upload_key(target):
    key = ANDROID_UPLOAD_KEYS.get(target)
    if not isinstance(key, dict):
        raise StoreError("This Android target has no committed upload key")
    return key


def android_cert_matches(target, sha256_hex):
    """Whether a certificate SHA-256 (64 lowercase hex) is this target's pinned upload certificate."""
    pin = android_upload_key(target)["cert"]
    return bool(re.fullmatch(r"[0-9a-f]{64}", sha256_hex or "")) and \
        bool(re.fullmatch(r"[0-9a-f]{16,64}", pin)) and sha256_hex.startswith(pin)


def check_android_upload_key(target, alias, cert_sha256):
    """Refuse signing material that is not this target's own upload key."""
    if alias != android_upload_key(target)["alias"]:
        raise StoreError("Android key alias is not this target's upload key")
    if not android_cert_matches(target, cert_sha256):
        raise StoreError("MAY_ANDROID_CERT_SHA256 is not this target's upload certificate")


def check_android_signer(target, signer_sha256):
    """Refuse a signed bundle whose sole signer (verify.aab_signer_sha256) is not this target's key."""
    if not android_cert_matches(target, signer_sha256):
        raise StoreError("Signed Android App Bundle is not signed with this target's upload key")


def is_internal(platform, target):
    return (platform, target) in INTERNAL_TARGETS


def design_system_pins(record, platform, target):
    """Both design-system pins of one build, checked; refuse drift for a non-internal target.

    `record` is {"ios": <40-hex>, "android": <40-hex>}. Samuel's decision
    (2026-10-14): an internal build checks only its own platform's pin against
    the checkout, so the two may differ; any other target, or no target (the
    native compile), still requires them to match. Both are always recorded so
    drift stays visible on every release.
    """
    if (not isinstance(record, dict) or sorted(record) != ["android", "ios"] or
            not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v) for v in record.values())):
        raise StoreError("Design-system pin record is missing or malformed")
    if record["ios"] != record["android"] and not is_internal(platform, target):
        raise StoreError("iOS and Android design-system pins differ; only an internal target may differ")
    return {"ios": record["ios"], "android": record["android"]}


def play_account(target, configured):
    """MAY_PLAY_ACCOUNT, refused unless it is exactly this target's committed Play account."""
    if configured != android_upload_key(target)["account"]:
        raise StoreError("MAY_PLAY_ACCOUNT is not this target's Play service account")
    return configured


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


def upload_report(record):
    """The lines a successful upload prints, from its upload record."""
    if record["platform"] == "android":
        return ["Store upload made: Google Play accepted and committed:",
                "  package: " + record["app_id"],
                "  versionCode: " + record["build_number"],
                "  track: " + record["track"],
                "  signed AAB SHA-256: " + record["sha256"],
                "  Play edit (committed): " + record["store_id"]]
    return ["Store upload made: App Store Connect accepted:",
            "  bundle: " + record["app_id"],
            "  build number (CFBundleVersion): " + record["build_number"],
            "  track: " + record["track"],
            "  signed IPA SHA-256: " + record["sha256"],
            "  App Store Connect build upload: " + record["store_id"] +
            " (read back from App Store Connect; TestFlight processing and testers are checked there)"]


def write_upload_record(platform, app_id, build_number, track, sha256, store_id=None):
    """Record a successful upload for confirm_upload.py, and return its report lines."""
    record = {"platform": platform, "app_id": app_id, "build_number": build_number, "track": track,
              "sha256": sha256, "store_id": store_id}
    write_private(runner_root() / "may-release" / UPLOAD_RECORD, json.dumps(record, sort_keys=True).encode())
    return upload_report(record)


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


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_private(argv, cwd, log, env):
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise StoreError("Protected native release failed; inspect private runner diagnostics")


def posthog_env(platform="android"):
    """The platform's PostHog key and its pinned host for its build script (POSTHOG_NAMES); {} when the key is
    absent. Refuses anything else, never naming the value."""
    key_name, host_name = POSTHOG_NAMES[platform]
    key = os.environ.get(key_name, "").strip()
    host = os.environ.get(host_name, "").strip().rstrip("/") or POSTHOG_HOSTS[0]
    if not key:
        return {}
    if not POSTHOG_KEY.fullmatch(key):
        if platform == "ios":
            raise StoreError("MAY_POSTHOG_IOS_KEY is not a PostHog project key (phc_ then letters, digits, _ and -)")
        raise StoreError("MAY_POSTHOG_ANDROID_KEY is not a PostHog project key (phc_ then letters, digits, _ and -)")
    if host not in POSTHOG_HOSTS:
        if platform == "ios":
            raise StoreError("MAY_POSTHOG_IOS_HOST is not PostHog's US or EU cloud (https://us.i.posthog.com, "
                             "https://eu.i.posthog.com)")
        raise StoreError("MAY_POSTHOG_HOST is not PostHog's US or EU cloud (https://us.i.posthog.com, "
                         "https://eu.i.posthog.com)")
    return {key_name: key, host_name: host}


def build(platform, target, source_sha, build_number):
    app_id = identity(platform, target, source_sha, build_number)
    key_name, host_name = POSTHOG_NAMES[platform]
    require_step_secrets((key_name,))
    crash_reports = posthog_env(platform)
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
        env.update(MAY_IOS_APP_ID=app_id, MAY_TEAM_ID=APPLE_TEAM, DEVELOPER_DIR=XCODE_DEVELOPER_DIR, **crash_reports)
    else:
        env.update(MAY_ANDROID_APP_ID=app_id, **crash_reports)
    if crash_reports:
        # The host, already checked to be one of POSTHOG_HOSTS, so whoever approves the run sees where
        # reports go; never the key.
        if platform == "ios":
            print("PostHog crash-report key passed to the iOS build; reports go to " + crash_reports[host_name])
        else:
            print("PostHog crash-report key passed to the Android build; reports go to " + crash_reports[host_name])
    else:
        print("No PostHog key in this build environment; this build sends no crash reports")
    run_private(["bash", str(script), command], source, private / (platform + "-build.log"), env)
    made = out / product
    if [p.name for p in out.iterdir()] != [product] or made.is_symlink():
        raise StoreError("Unsigned build must leave exactly its one expected product")
    if not (made.is_dir() if platform == "ios" else made.is_file()):
        raise StoreError("Unsigned build must leave exactly its one expected product")
    print("Protected unsigned May build prepared; signing and upload run in a separate job")


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
