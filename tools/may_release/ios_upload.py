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
redirects refused; an error body is read only for api_errors (below).

The upload is `altool --upload-package` with the app's App Store Connect ID,
bundle ID, CFBundleVersion and CFBundleShortVersionString (all from the target
and the verified IPA). Apple's altool guide: "The --upload-app command is
deprecated. Use the --upload-package command instead." Xcode 26's altool also
picks the wrong app when two bundle IDs share a prefix unless --apple-id names
it (fastlane #29698, quoting Apple), and May's two do. Runs 39192306300 and
39363085983 used --upload-app and reached no build upload.

Xcode 26's altool does not exit non-zero on a failed upload (Bitrise's App Store
Connect step v2.0.0 changelog), so any line of its output naming an error fails
this step at once, before the readback, with the same "may be in App Store
Connect" note.

altool's output stays in the private log, which cleanup deletes. Before that,
altool_excerpt prints a scrubbed summary to this public log: the delivery UUID
(only if it is a UUID), the number of lines naming an error or a warning, the
error codes on those lines (ITMS-NNNNN, negative numeric codes, and upper-case
code tokens such as ENTITY_ERROR.ATTRIBUTE.INVALID), and the kinds of error
those lines describe, each a fixed label from ERROR_KINDS. Never a raw line, a
path, the key ID or the issuer.

altool runs with --output-format json (run 39423195473: its plain error lines
carried no code). Its JSON report's product-errors each add to the error count
and print as one line: their codes (numeric, code-shaped strings such as
STATE_ERROR.VALIDATION_ERROR, ITMS-NNNNN in messages), HTTP status, and title.
A title is Apple's fixed string, printed only as short plain text (ERROR_TITLE)
holding neither the key ID, the issuer nor any UUID; otherwise "?".

Each product error's messages that name an Info.plist key, an ITMS-NNNNN code
or a known ERROR_KINDS topic print too (run 39481657122: the title alone was
"Validation failed"), scrubbed by apple_message: UUIDs, the key ID and issuer
replaced, quoted values other than keys, codes and versions replaced, words with
a path or address character replaced, cut to 300 characters, plain text only.

Before the upload, altool --validate-app runs on the IPA with the same JSON
output and excerpt ("altool validation"). Its product errors stop the step with
nothing uploaded; if the validation itself can't run, that is printed and the
upload goes ahead (the upload validates again).

Any 4xx from the App Store Connect API (diagnostics or readback) prints Apple's
JSON:API errors[] code, status and source.parameter, each checked against a
fixed shape (api_errors); never its title or detail.

Diagnostics: before altool, and again after any failure once altool has run,
this prints what App Store Connect holds for both May apps (asc_diagnostics):
each app's bundle ID and its newest build uploads and builds (build number,
version, state, date, error codes), every value checked against a fixed shape
so no free text from Apple is printed. It first prints the IPA's own bundle ID,
build number and version. A token is signed for the diagnostics and a fresh one
after altool for the readback, so a slow upload can't expire it.

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
MAX_PRODUCT_ERRORS = 5
# An Apple error title (a fixed string such as "The provided entity includes an attribute with a value that has already
# been used") is printed only as short plain text: no path, address, markup or quoting characters.
ERROR_TITLE = re.compile(r"[A-Za-z0-9 ,.'()&:;?!-]{1,160}")
# The Info.plist keys (and Info.plist itself) an Apple validation message can name; a message naming one, an
# ITMS-NNNNN code or a known ERROR_KINDS topic is printed, scrubbed (apple_message).
PLIST_KEY = re.compile(r"\b(?:CFBundle\w+|NS\w*UsageDescription|UI[A-Z]\w+|LS[A-Z]\w+|MinimumOSVersion|"
                       r"ITSAppUsesNonExemptEncryption|DT[A-Z]\w+|aps-environment|Info\.plist)\b")
ITMS_CODE = re.compile(r"\bITMS-\d{4,6}\b")
ERROR_MESSAGE = re.compile(r"[A-Za-z0-9 ,.'()&:;?!_<>\[\]=+%#*-]{1,320}")
MESSAGE_LIMIT = 300
MAX_MESSAGES = 8
# What an altool error line is about, as fixed labels (the line itself is never printed). The first match wins;
# a line that matches none counts as "unrecognised".
ERROR_KINDS = (
    ("agreement or contract pending", r"agreement|contract|program license|paid apps|terms (and|&) conditions"),
    ("file format (the package could not be read)",
     r"correct format|\(259\)|could ?n.?t be opened|not a valid (ipa|package|archive)|unsupported file"),
    ("altool option refused or deprecated", r"unrecogni[sz]ed (option|argument)|unknown (option|argument)|"
                                            r"invalid (option|argument)|deprecated|usage:"),
    ("authentication or API key", r"authenticat|unauthori[sz]ed|\b401\b|credential|invalid (api )?key|"
                                  r"private key|jwt|issuer"),
    ("permission or API key role", r"forbidden|\b403\b|permission|not allowed|\brole\b|not authori[sz]ed to"),
    ("conflict (409)", r"\b409\b|conflict"),
    ("build number already used", r"already (been )?(used|uploaded|exists?)|redundant binary|"
                                  r"must be (higher|greater)|duplicate"),
    ("missing app version record", r"app ?store version|version record|no (app )?version|create (a )?new version|"
                                   r"(pre-?release )?train .*(closed|not open)"),
    ("no matching App Store Connect app or bundle ID", r"no suitable application|could ?n.?t find (the )?(app|"
                                                        r"application|bundle)|apple ?id|bundle ?id|"
                                                        r"no (app|application) (record|found)|wrong app"),
    ("signing, provisioning or entitlements", r"provision|code ?sign|signature|entitlement|certificate|team"),
    ("invalid binary or Info.plist", r"info\.plist|cfbundle|invalid (binary|bundle)|missing (required )?"
                                     r"(key|icon|purpose)|itsappusesnonexemptencryption|export compliance|"
                                     r"icon|minimumosversion|architecture"),
    ("network or timeout", r"timed? ?out|network|connection|could ?n.?t connect|offline|\b5\d\d\b|"
                           r"service unavailable|try again later"),
)


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
        # Apple's JSON:API error body is read (at most ERROR_BODY_LIMIT bytes) so api_errors can name its code,
        # status and parameter; nothing else from it is ever printed.
        try:
            raw = error.read(ERROR_BODY_LIMIT)
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            error.close()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


ERROR_BODY_LIMIT = 65536
API_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_.]{1,80}")
API_ERROR_PARAMETER = re.compile(r"[A-Za-z0-9_.\[\]-]{1,60}")


def api_errors(body):
    """Apple's JSON:API errors[] as "CODE (status N, parameter P)" strings, each part checked against a fixed shape
    (anything else reads "?"); no title or detail is printed. Empty when there are none."""
    errors = body.get("errors") if isinstance(body, dict) else None
    found = []
    for error in errors[:5] if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        source = error.get("source") if isinstance(error.get("source"), dict) else {}
        parameter = source.get("parameter")
        found.append(shown(error.get("code"), API_ERROR_CODE) + " (status " +
                     shown(error.get("status"), re.compile(r"\d{3}")) +
                     (", parameter " + shown(parameter, API_ERROR_PARAMETER) if parameter is not None else "") + ")")
    return found


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
        if status is not None and 400 <= status <= 499 and api_errors(body):
            print("App Store Connect said: " + "; ".join(api_errors(body)), flush=True)
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


DIAGNOSTIC_LIMIT = 10
DIAGNOSTIC_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})")
DIAGNOSTIC_CODE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
BUILD_STATES = {"PROCESSING", "FAILED", "INVALID", "VALID"}
BUNDLE_ID = re.compile(r"[A-Za-z0-9.-]{1,155}")


def shown(value, pattern=None, allowed=None):
    """A value from Apple fit for this public log, or "?": a string matching pattern or in allowed, never free text."""
    if not isinstance(value, str) or (pattern is not None and not pattern.fullmatch(value)) or \
            (allowed is not None and value not in allowed):
        return "?"
    return value


def said(body):
    errors = api_errors(body)
    return "; Apple says " + "; ".join(errors) if errors else ""


def http(status):
    return "HTTP " + str(status) if type(status) is int else "no reply"


def diagnostics(token, get=asc_get):
    """asc_diagnostics, or one fixed line if reading them failed in any way: they never fail the step."""
    try:
        return asc_diagnostics(token, get)
    except Exception:
        return ["App Store Connect diagnostics unavailable"]


def asc_diagnostics(token, get=asc_get):
    """What App Store Connect holds for both May apps, as lines fit for the public log: each app's bundle ID, and
    its newest build uploads and builds (build number, version, state, date, error codes). Only GETs; every value is
    checked against a fixed shape (shown) before it is printed, so no free text from Apple reaches the log. A failed
    read prints its HTTP status and nothing else; diagnostics never fail the step."""
    lines = []
    for target in ("playground", "production"):
        asc_id = ASC_APP_IDS[target]
        base = ASC_API + "apps/" + asc_id
        status, body = get(token, base + "?" + urlencode({"fields[apps]": "bundleId"}))
        data = body.get("data") if isinstance(body, dict) else None
        bundle = shown((data.get("attributes") or {}).get("bundleId") if isinstance(data, dict) else None, BUNDLE_ID)
        lines.append("App Store Connect app " + asc_id + " (" + target + "): bundle " +
                     (bundle if status == 200 else "unreadable (" + http(status) + ")" + said(body)))
        # Run 39423195473: this list with sort=-createdDate got HTTP 400 on both apps, while the readback's query (no
        # sort, limit 200) has been answered; so no sort, and newest first is done here from createdDate.
        status, body = get(token, base + "/buildUploads?" + urlencode({"limit": "200"}))
        items = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(items, list):
            lines.append("  build uploads: unreadable (" + http(status) + ")" + said(body))
        else:
            items = sorted(items, key=lambda item: (created_at(item) if isinstance(item, dict) and
                                                    isinstance(item.get("attributes"), dict) else None) or 0,
                           reverse=True)
            lines.append("  build uploads: " + str(len(items)) + (" (newest first)" if items else ""))
            for item in items[:DIAGNOSTIC_LIMIT]:
                attributes = item.get("attributes") if isinstance(item, dict) else None
                attributes = attributes if isinstance(attributes, dict) else {}
                state = attributes.get("state")
                errors = state.get("errors") if isinstance(state, dict) else None
                codes = sorted({shown(error.get("code"), DIAGNOSTIC_CODE) for error in errors or []
                                if isinstance(error, dict)}) if isinstance(errors, list) else []
                lines.append("    build " + shown(attributes.get("cfBundleVersion"), SHORT_VERSION) + " version " +
                             shown(attributes.get("cfBundleShortVersionString"), SHORT_VERSION) + " " +
                             shown(attributes.get("platform"), allowed={"IOS", "MAC_OS", "TV_OS", "VISION_OS"}) + " " +
                             ((upload_state(item) if isinstance(item, dict) else None) or "?") + " created " +
                             shown(attributes.get("createdDate"), DIAGNOSTIC_DATE) +
                             (" errors " + ", ".join(codes) if codes else ""))
        status, body = get(token, ASC_API + "builds?" + urlencode({"filter[app]": asc_id, "limit": str(DIAGNOSTIC_LIMIT),
                                                                   "sort": "-uploadedDate"}))
        items = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(items, list):
            lines.append("  builds: unreadable (" + http(status) + ")" + said(body))
        else:
            lines.append("  builds: " + str(len(items)) + (" (newest first)" if items else ""))
            for item in items[:DIAGNOSTIC_LIMIT]:
                attributes = item.get("attributes") if isinstance(item, dict) else None
                attributes = attributes if isinstance(attributes, dict) else {}
                lines.append("    build " + shown(attributes.get("version"), SHORT_VERSION) + " " +
                             shown(attributes.get("processingState"), allowed=BUILD_STATES) + " uploaded " +
                             shown(attributes.get("uploadedDate"), DIAGNOSTIC_DATE))
    return lines


def altool_output(log):
    """altool's output as (lines outside its JSON report, the JSON report or None); None when there is no log.
    With --output-format json altool prints one JSON object (Apple's product-errors and details); anything else
    it prints (such as "Running altool at path ...") stays as plain lines."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    decoder = json.JSONDecoder()
    for start in (match.start(1) for match in re.finditer(r"(?m)^[ \t]*(\{)", text)):
        try:
            report, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(report, dict):
            return (text[:start] + text[end:]).splitlines(), report
    return text.splitlines(), None


def product_errors(report):
    """The product-errors of altool's JSON report, as dicts (at most MAX_PRODUCT_ERRORS are described)."""
    errors = report.get("product-errors") if isinstance(report, dict) else None
    return [error for error in errors if isinstance(error, dict)] if isinstance(errors, list) else []


def walk(value, depth=0):
    """(key, value) for every entry of a product error and of the dicts and lists nested in it (userInfo,
    NSUnderlyingError ...), to a fixed depth."""
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            yield from walk(item, depth + 1)


def error_codes(error):
    """A product error's codes: numeric codes (such as -19232), code-shaped strings under "code" or "iris-code"
    (such as STATE_ERROR.VALIDATION_ERROR), and code tokens in its messages (such as ITMS-90062)."""
    codes = set()
    for key, value in walk(error):
        if key in ("code", "iris-code") and type(value) is int and -999999 <= value <= 999999:
            codes.add(str(value))
        elif key in ("code", "iris-code") and isinstance(value, str) and API_ERROR_CODE.fullmatch(value):
            codes.add(value)
        elif isinstance(value, str):
            codes.update(CODE_TOKENS.findall(value))
    return codes


def apple_error(error, hidden):
    """One product error as a line fit for the public log: its codes, HTTP status and title. The title is Apple's
    fixed string, printed only if it is short plain text holding nothing in `hidden` and no UUID; else "?"."""
    statuses = sorted({value for key, value in walk(error) if key == "status" and isinstance(value, str) and
                       re.fullmatch(r"\d{3}", value)})
    titles = [value for key, value in walk(error) if key == "title" and isinstance(value, str)]
    title = titles[0] if titles else None
    if title is not None and (not ERROR_TITLE.fullmatch(title) or UUID.search(title) or
                              any(item and item.lower() in title.lower() for item in hidden)):
        title = "?"
    codes = sorted(error_codes(error))
    return ("    codes " + (", ".join(codes[:MAX_CODES]) if codes else "none") +
            ("; status " + ", ".join(statuses) if statuses else "") +
            ("; title " + title if title is not None else ""))


def apple_message(text, hidden):
    """One of Apple's validation messages fit for the public log, or None: whitespace collapsed; UUIDs, the key
    ID and the issuer replaced; a quoted value kept only if it is empty or a comma list of Info.plist keys, codes
    or version numbers (else <quoted>); any word with a path, address or home character replaced by <path>; cut to MESSAGE_LIMIT;
    then printed only if every character is plain text (ERROR_MESSAGE), else "?"."""
    text = UUID.sub("<uuid>", " ".join(text.split()))
    for item in hidden:
        if item:
            text = re.sub(re.escape(item), "<hidden>", text, flags=re.I)

    def quoted(match):
        inner = match.group(2)
        keep = inner == "" or all(PLIST_KEY.fullmatch(part) or CODE_TOKENS.fullmatch(part) or
                                  SHORT_VERSION.fullmatch(part) for part in re.split(r"\s*,\s*", inner))
        return "'" + (inner if keep else "<quoted>") + "'"

    text = re.sub(r"(['\"‘“])([^'\"‘’“”]{0,200})['\"’”]", quoted, text)
    text = " ".join("<path>" if re.search(r"[/\\@~`$|{}]", word) else word for word in text.split(" "))
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT].rsplit(" ", 1)[0] + " ..."
    return text if ERROR_MESSAGE.fullmatch(text) else "?"


def apple_messages(error, hidden):
    """The messages of one product error worth printing (each scrubbed by apple_message, duplicates dropped): those
    naming an Info.plist key, an ITMS-NNNNN code, or a known ERROR_KINDS topic. Codes, statuses, IDs and the title
    are printed elsewhere."""
    found = []
    for key, value in walk(error):
        if key in ("code", "iris-code", "status", "title", "id") or not isinstance(value, str):
            continue
        if PLIST_KEY.search(value) or ITMS_CODE.search(value) or error_kind(value) != "unrecognised":
            message = apple_message(value, hidden)
            if message not in found:
                found.append(message)
    return ["      message: " + message for message in found[:MAX_MESSAGES]]


def error_kinds(error):
    """The ERROR_KINDS labels of a product error's messages, each message read on its own; "unrecognised" only
    when none is recognised."""
    kinds = {error_kind(value) for _, value in walk(error) if isinstance(value, str)} - {"unrecognised"}
    return kinds or {"unrecognised"}


def altool_excerpt(log, hidden=(), label="altool output"):
    """The scrubbed lines this public log may carry about altool's output: no raw line, path or credential.
    `hidden` (the issuer) and the key ID never reach an Apple error title or message that is printed."""
    output = altool_output(log)
    if output is None:
        return [label + ": none recorded"]
    lines, report = output
    hidden = (APPLE_KEY_ID,) + tuple(hidden)
    errors = [line for line in lines if re.search(r"(?i)(?<!no )\berror", line)]  # not "No errors uploading."
    warnings = [line for line in lines if re.search(r"(?i)\bwarning", line)]
    reported = product_errors(report)
    details = report.get("details") if isinstance(report, dict) else None
    uuid = details.get("delivery-uuid") if isinstance(details, dict) else None
    if not (isinstance(uuid, str) and UUID.fullmatch(uuid)):
        uuid = re.search(r"(?i)delivery[\s_-]*uuid\W{0,5}([0-9A-Fa-f-]{36})", "\n".join(lines))
        uuid = uuid.group(1) if uuid and UUID.fullmatch(uuid.group(1)) else None
    # Codes from every plain line (altool's own error dictionaries span lines that don't say "error"), and from
    # the JSON report's product errors.
    codes = sorted({code for line in lines for code in CODE_TOKENS.findall(line)} |
                   {code for error in reported for code in error_codes(error)})
    kinds = sorted({error_kind(line) for line in errors} | {kind for error in reported for kind in error_kinds(error)})
    excerpt = [label + " (scrubbed; the full log stays private and is deleted at cleanup):",
               "  delivery UUID: " + (uuid or "none"),
               "  lines naming an error: " + str(len(errors)) + "; naming a warning: " + str(len(warnings)),
               "  codes: " + (", ".join(codes[:MAX_CODES]) if codes else "none") +
               (" (" + str(len(codes) - MAX_CODES) + " more)" if len(codes) > MAX_CODES else ""),
               "  error kinds: " + (", ".join(kinds) if kinds else "none")]
    if report is not None:
        excerpt.append("  JSON report: " + str(len(reported)) + " product error" + ("" if len(reported) == 1 else "s"))
        for error in reported[:MAX_PRODUCT_ERRORS]:
            excerpt += [apple_error(error, hidden)] + apple_messages(error, hidden)
    return excerpt


def validation_errors(log):
    """How many product errors altool's JSON report from --validate-app names; 0 without a log or a report."""
    output = altool_output(log)
    return len(product_errors(output[1])) if output is not None else 0


def validate(ipa, issuer, private, env):
    """altool --validate-app before the upload, so App Store Connect's validation errors are named before anything
    is sent. Its product errors stop the step (nothing has been uploaded); a validation altool itself could not run
    (an option refused, a non-zero exit) is reported and the upload goes ahead, since the upload validates again."""
    log = private / "ios-validate.log"
    try:
        run_private(["/usr/bin/xcrun", "altool", "--validate-app", "-f", str(ipa), "-t", "ios",
                     "--apiKey", APPLE_KEY_ID, "--apiIssuer", issuer, "--output-format", "json"], private, log, env)
    except StoreError:
        print("altool validation exited non-zero; the upload validates again", flush=True)
    finally:
        print("\n".join(altool_excerpt(log, (issuer,), "altool validation")), flush=True)
    if validation_errors(log):
        raise StoreError("App Store Connect validation reported errors before the upload; nothing was uploaded; "
                         "see the validation excerpt above")


def error_lines(log):
    """How many errors altool reported: lines of its output naming an error (not "No errors uploading."), plus
    the product errors in its JSON report; 0 when there is no log."""
    output = altool_output(log)
    if output is None:
        return 0
    lines, report = output
    return sum(1 for line in lines if re.search(r"(?i)(?<!no )\berror", line)) + len(product_errors(report))


def error_kind(line):
    """The fixed ERROR_KINDS label for one altool error line, or "unrecognised"."""
    for label, pattern in ERROR_KINDS:
        if re.search("(?i)" + pattern, line):
            return label
    return "unrecognised"


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
    started, altool_finished, token = time.time(), False, None
    # The IPA's own numbers, read from its Info.plist (verify.ipa_identity checked them against the dispatch).
    print("IPA to upload: " + app_id + " build " + build_number + " version " + short_version +
          " to App Store Connect app " + ASC_APP_IDS[target], flush=True)
    try:
        try:
            key_path = keys / ("AuthKey_" + APPLE_KEY_ID + ".p8")
            write_private(key_path, key)
            # A token now for the diagnostics, and a fresh one after altool for the readback, so a slow upload
            # can't leave the readback with an expired token.
            print("Before the upload:\n" + "\n".join(diagnostics(asc_token(key_path, issuer), get)), flush=True)
            env = child_env(DEVELOPER_DIR=XCODE_DEVELOPER_DIR, API_PRIVATE_KEYS_DIR=str(keys))
            validate(ipa, issuer, private, env)
            try:
                run_private(["/usr/bin/xcrun", "altool", "--upload-package", str(ipa), "--type", "ios",
                             "--apple-id", ASC_APP_IDS[target], "--bundle-id", app_id,
                             "--bundle-version", build_number, "--bundle-short-version-string", short_version,
                             "--apiKey", APPLE_KEY_ID, "--apiIssuer", issuer, "--output-format", "json"],
                            private, private / "ios-upload.log", env)
            finally:
                print("\n".join(altool_excerpt(private / "ios-upload.log", (issuer,))), flush=True)
            altool_finished = True
            if error_lines(private / "ios-upload.log"):
                # Xcode 26's altool exits 0 on a failed upload; its own error lines are the failure.
                raise StoreError("altool reported errors (exit status 0 notwithstanding); see the error kinds above")
        finally:
            if altool_finished:
                try:
                    token = asc_token(key_path, issuer)
                except StoreError:
                    token = None
            shutil.rmtree(keys, ignore_errors=True)
        if token is None:
            raise StoreError("App Store Connect token could not be signed")
        upload_id, state = await_build_upload(token, ASC_APP_IDS[target], build_number, short_version, started,
                                              get, sleep, clock)
    except StoreError:
        if altool_finished:
            if token is not None:
                print("After the upload:\n" + "\n".join(diagnostics(token, get)), flush=True)
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
