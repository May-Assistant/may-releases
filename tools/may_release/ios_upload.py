"""Upload the signed, verified May IPA for internal TestFlight.

The workflow step that runs this holds only the App Store Connect key and
issuer: no source read key and no signing material. It re-checks the IPA's
embedded profile team and bundle ID and its build number before upload.
TestFlight processing and group assignment need separate readback.

altool's exit status is not proof of an upload: run 39192306300 exited 0 and
left no build upload in App Store Connect. So, after altool, this script signs
one App Store Connect API token (ES256, by /usr/bin/openssl, from the same key)
and polls `GET /v1/apps/{id}/buildUploads` for this CFBundleVersion, every
POLL_SECONDS for up to POLL_TIMEOUT. Of the iOS build uploads for this
CFBundleVersion and the IPA's CFBundleShortVersionString created after this step
started, the newest decides: the step fails unless it is PROCESSING or COMPLETE
(FAILED fails at once; AWAITING_UPLOAD keeps polling). Any failure after altool
finished says the upload may be in App Store Connect, and a timeout also names
any fields it could not read (names only). It prints
that build upload's ID and state and records the ID as the upload's store_id,
which confirm_upload.py requires. Only GETs are sent, only to ASC_API, with
redirects refused; Apple's error bodies are discarded unread.

altool's output stays in the private log, which cleanup deletes. Before that,
altool_excerpt prints a scrubbed summary to this public log: the delivery UUID
(only if it is a UUID), the number of lines naming an error or a warning, and
the error codes on those lines (ITMS-NNNNN, negative numeric codes, and
upper-case code tokens such as ENTITY_ERROR.ATTRIBUTE.INVALID). Never a raw
line, a path, the key ID or the issuer.

Dry run: the workflow skips this step unless `dry_run` is false, and this
script also refuses unless MAY_DRY_RUN is exactly "false". dry_run.py reports
instead.

Report: after a successful upload it records it (store.UPLOAD_RECORD) and prints
the bundle, build number, track and signed IPA SHA-256. The workflow's next step
(confirm_upload.py) fails the job unless this step succeeded and left that
record. Both steps are inert while the iOS jobs are.
"""

import argparse
import base64
import datetime
import http.client
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
import urllib.error
from urllib.parse import quote, urlencode
import urllib.request
import sys
import zipfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (APPLE_KEY_ID, ASC_UPLOAD_ID, APPLE_TEAM, IOS_TRACK, IOS_UPLOAD_SECRETS, XCODE_DEVELOPER_DIR, StoreError,
                   child_env, file_sha256, identity, require_step_secrets, run_private, runner_root,
                   secret_bytes, write_private, write_upload_record)
import verify

ASC_API = "https://api.appstoreconnect.apple.com/v1/"
# App Store Connect app IDs (README): May and May Playground.
ASC_APP_IDS = {"production": "6821782401", "playground": "6822072535"}
OPENSSL = "/usr/bin/openssl"
POLL_SECONDS, POLL_TIMEOUT = 30, 900
TOKEN_LIFETIME = 1140  # Under Apple's 20-minute maximum, with a minute's margin; it outlasts the poll.
CLOCK_SKEW = 300  # A build upload created this long before the step started still counts as this one.
UPLOAD_STATES = {"AWAITING_UPLOAD", "PROCESSING", "FAILED", "COMPLETE"}
ACCEPTED_STATES = {"PROCESSING", "COMPLETE"}
SHORT_VERSION = re.compile(r"\d{1,9}(\.\d{1,9}){0,2}")
UUID = re.compile(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
# Only code-shaped tokens: ITMS-NNNNN, a negative numeric code, or upper-case words joined by _ or . (such as
# ENTITY_ERROR.ATTRIBUTE.INVALID). A bare upper-case run (which could be a fragment of an ID) never matches.
CODE_TOKENS = re.compile(r"ITMS-\d{4,6}|(?<![\w.-])-\d{3,6}\b|\b[A-Z]{2,30}(?:[_.][A-Z]{2,30}){1,5}\b")
MAX_CODES = 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A redirect becomes an HTTP error; the token never leaves Apple's API host.


OPENER = urllib.request.build_opener(NoRedirect)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def der_to_jose(signature):
    """An ECDSA P-256 signature as openssl writes it (DER SEQUENCE of r, s) to JOSE's 64-byte r||s."""
    def integer(at):
        if signature[at:at + 1] != b"\x02" or at + 2 > len(signature):
            raise StoreError("App Store Connect token signature was invalid")
        length = signature[at + 1]
        value = signature[at + 2:at + 2 + length]
        if not 1 <= length <= 33 or len(value) != length or (length == 33 and value[0] != 0):
            raise StoreError("App Store Connect token signature was invalid")
        return value.lstrip(b"\x00").rjust(32, b"\x00"), at + 2 + length

    if len(signature) < 8 or signature[0] != 0x30 or signature[1] != len(signature) - 2:
        raise StoreError("App Store Connect token signature was invalid")
    r, at = integer(2)
    s, at = integer(at)
    if at != len(signature):
        raise StoreError("App Store Connect token signature was invalid")
    return r + s


def asc_token(key_path, issuer, now=None):
    """An App Store Connect API token signed with the upload key (ES256, via the system openssl)."""
    issued = int(now if now is not None else time.time())
    header = {"alg": "ES256", "kid": APPLE_KEY_ID, "typ": "JWT"}
    claims = {"iss": issuer, "iat": issued, "exp": issued + TOKEN_LIFETIME, "aud": "appstoreconnect-v1"}
    signing_input = b64url(json.dumps(header).encode()) + b"." + b64url(json.dumps(claims).encode())
    try:
        result = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key_path)], input=signing_input,
                                capture_output=True, env=child_env())
    except OSError:
        result = None
    if result is None or result.returncode or not result.stdout:
        raise StoreError("App Store Connect token could not be signed")
    return (signing_input + b"." + b64url(der_to_jose(result.stdout))).decode()


def asc_get(token, url, opener=OPENER):
    """(HTTP status, parsed JSON or None) for one GET; None status when there was no usable reply."""
    if not url.startswith(ASC_API):
        raise StoreError("Refusing a request outside the App Store Connect API")
    request = urllib.request.Request(url, method="GET", headers={"Authorization": "Bearer " + token})
    try:
        with opener.open(request, timeout=30) as reply:
            status, body = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None  # Apple's error bodies are discarded unread.
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def upload_state(item):
    """A build upload's state: the attribute is an object with its own `state` (or, defensively, a string)."""
    state = item.get("attributes", {}).get("state") if isinstance(item.get("attributes"), dict) else None
    if isinstance(state, dict):
        state = state.get("state")
    return state if state in UPLOAD_STATES else None


def created_at(item):
    value = item.get("attributes", {}).get("createdDate")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def matching_upload(items, build_number, short_version, started, unreadable):
    """This run's build upload in a listing: iOS, this CFBundleVersion and CFBundleShortVersionString, created
    since the step started. The newest such upload decides, so a newer FAILED beats an older PROCESSING.
    Returns (id, state) or None. The names of fields that could not be read are added to `unreadable`."""
    found = []
    for item in items:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attributes, dict):
            unreadable.add("attributes")
            continue
        version = attributes.get("cfBundleVersion")
        if not isinstance(version, str):
            unreadable.add("cfBundleVersion")
        if version != build_number:
            continue
        when = created_at(item)
        if when is None:
            unreadable.add("createdDate")
        checks = (("platform", attributes.get("platform"), "IOS"),
                  ("cfBundleShortVersionString", attributes.get("cfBundleShortVersionString"), short_version))
        for name, value, _ in checks:
            if not isinstance(value, str):
                unreadable.add(name)
        if when is None or when < started - CLOCK_SKEW or any(value != expected for _, value, expected in checks):
            continue
        upload_id, state = item.get("id"), upload_state(item)
        valid_id = isinstance(upload_id, str) and ASC_UPLOAD_ID.fullmatch(upload_id)
        if not valid_id:
            unreadable.add("id")
        if state is None:
            unreadable.add("state")
        if valid_id and state is not None:
            found.append((when, upload_id, state))
    return max(found)[1:] if found else None


def await_build_upload(token, asc_app_id, build_number, short_version, started, get=asc_get, sleep=time.sleep,
                       clock=time.monotonic):
    """Poll App Store Connect until this build upload exists and is not FAILED. Returns (id, state)."""
    url = ASC_API + "apps/" + quote(asc_app_id, safe="") + "/buildUploads?" + urlencode(
        {"filter[cfBundleVersion]": build_number, "limit": "200"})
    deadline, unreadable = clock() + POLL_TIMEOUT, set()
    while True:
        status, body = get(token, url)
        if status in (401, 403):
            raise StoreError("App Store Connect refused the build upload readback (HTTP 401 or 403)")
        if status == 404:
            raise StoreError("App Store Connect found no such app or build upload list (HTTP 404)")
        if status == 200:
            items = body.get("data") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise StoreError("App Store Connect build upload list was invalid")
            match = matching_upload(items, build_number, short_version, started, unreadable)
            if match and match[1] == "FAILED":
                raise StoreError("App Store Connect build upload FAILED; the build was not accepted")
            if match and match[1] in ACCEPTED_STATES:
                return match
        elif status is not None and not (status == 429 or 500 <= status <= 599):
            raise StoreError("App Store Connect build upload readback was rejected")
        # Not there yet, still AWAITING_UPLOAD, or a transient reply (no reply, 429, 5xx): poll again.
        if clock() + POLL_SECONDS > deadline:
            if unreadable:
                # Field names only (a fixed set this script reads), never their values.
                print("App Store Connect build upload fields that could not be read: " + ", ".join(sorted(unreadable)))
            raise StoreError("No accepted App Store Connect build upload for this build number appeared "
                             "before the timeout; altool's exit status is not an upload")
        sleep(POLL_SECONDS)


def altool_excerpt(log):
    """The scrubbed lines this public log may carry about altool's output: no raw line, path or credential."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return ["altool output: none recorded"]
    lines = text.splitlines()
    errors = [line for line in lines if re.search(r"(?i)(?<!no )\berror", line)]  # not "No errors uploading."
    warnings = [line for line in lines if re.search(r"(?i)\bwarning", line)]
    uuid = re.search(r"(?i)delivery\s*uuid\W{0,5}([0-9A-Fa-f-]{36})", text)
    codes = sorted({code for line in errors + warnings for code in CODE_TOKENS.findall(line)})
    return ["altool output (scrubbed; the full log stays private and is deleted at cleanup):",
            "  delivery UUID: " + (uuid.group(1) if uuid and UUID.fullmatch(uuid.group(1)) else "none"),
            "  lines naming an error: " + str(len(errors)) + "; naming a warning: " + str(len(warnings)),
            "  codes: " + (", ".join(codes[:MAX_CODES]) if codes else "none") +
            (" (" + str(len(codes) - MAX_CODES) + " more)" if len(codes) > MAX_CODES else "")]


def ipa_short_version(ipa):
    """CFBundleShortVersionString of the IPA's one app (verify.ipa_identity has already checked its shape)."""
    try:
        with zipfile.ZipFile(ipa) as archive:
            plists = [name for name in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
            info = plistlib.loads(archive.read(plists[0])) if len(plists) == 1 else None
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException):
        info = None
    version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    if not (isinstance(version, str) and SHORT_VERSION.fullmatch(version)):
        raise StoreError("IPA has no valid CFBundleShortVersionString")
    return version


def upload(target, source_sha, build_number, get=asc_get, sleep=time.sleep, clock=time.monotonic):
    app_id = identity("ios", target, source_sha, build_number)
    require_step_secrets(IOS_UPLOAD_SECRETS)
    if os.environ.get("MAY_DRY_RUN") != "false":
        raise StoreError("Dry run: no store upload was requested")
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
    ipa_sha256, short_version = file_sha256(ipa), ipa_short_version(ipa)
    key = secret_bytes("MAY_ASC_KEY_BASE64")
    if not key.startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise StoreError("App Store Connect key is not a PEM private key")
    keys = private / "asc-keys"
    keys.mkdir(mode=0o700)
    started, altool_finished = time.time(), False
    try:
        try:
            key_path = keys / ("AuthKey_" + APPLE_KEY_ID + ".p8")
            write_private(key_path, key)
            env = child_env(DEVELOPER_DIR=XCODE_DEVELOPER_DIR, API_PRIVATE_KEYS_DIR=str(keys))
            try:
                run_private(["/usr/bin/xcrun", "altool", "--upload-app", "--file", str(ipa), "--type", "ios",
                             "--apiKey", APPLE_KEY_ID, "--apiIssuer", issuer],
                            private, private / "ios-upload.log", env)
            finally:
                print("\n".join(altool_excerpt(private / "ios-upload.log")), flush=True)
            altool_finished = True
            token = asc_token(key_path, issuer)
        finally:
            shutil.rmtree(keys, ignore_errors=True)
        upload_id, state = await_build_upload(token, ASC_APP_IDS[target], build_number, short_version, started,
                                              get, sleep, clock)
    except StoreError:
        if altool_finished:
            print("altool finished, so the upload may be in App Store Connect; check there before retrying",
                  flush=True)
        raise
    print("App Store Connect build upload " + upload_id + " is " + state + " for build " + build_number)
    print("\n".join(write_upload_record("ios", app_id, build_number, IOS_TRACK, ipa_sha256, upload_id)))


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
