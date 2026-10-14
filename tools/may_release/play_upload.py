"""May internal-track update upload, with fixed destination and quiet failures.

Update mode only. Google's Edits guide says the API can change only an existing
app "that has at least one APK uploaded", so the first upload must go through
Play Console (and Play accepts only `status: draft` releases through the API
until an app's first release has been rolled out). Each app's first release is
therefore a manual Play Console upload (for May production: sign_only.py, then
one Console upload). This adapter refuses an app whose tracks show no rolled-out
release, with a message saying so. Otherwise, inside one edit, it:

1. reads every track and requires the new versionCode to be greater than every
   versionCode on every track, and no more than MAX_VERSION_JUMP above it;
2. uploads the signed AAB and requires Play's returned versionCode and sha256
   to match the build number and the local file;
3. replaces only the `internal` track with one `completed` release, leaving
   every other track untouched;
4. commits with ERROR_IF_IN_REVIEW, so an in-review change is never cancelled.

Track: only `internal`, and only for the targets in store.PLAY_UPLOAD_TRACKS
(stage 3: May and May Playground). TRACK is the one track this script ever
writes; there is no track input, and a target the table does not map to TRACK
is refused before any request (gate.py refuses it at admission first). The one
track write is checked by require_internal_release before it is sent: exactly one
`completed` release of exactly the new build on `internal`, with no rollout
fraction, country targeting or other field, so no staged, halted or draft release
and no promotion of an existing build. The commit must be exactly
ERROR_IF_IN_REVIEW (require_commit); changesNotSentForReview is refused.

Target binding: before any request, MAY_PLAY_ACCOUNT must be the target's own
Play account, the signed bundle's sole signer must be the target's own upload
certificate (store.ANDROID_UPLOAD_KEYS), the bundle's own manifest must be
exactly store.APP_IDS[target] at the requested versionCode
(verify.check_aab_identity, read on this side rather than trusted from the build
job), and the build number must be at least the app's floor
(store.MIN_BUILD_NUMBERS: May production starts at 3).

Report: after the commit, it records the upload (store.UPLOAD_RECORD) and prints
the package, versionCode, track, signed AAB SHA-256 and the committed edit ID.
The workflow's next step (confirm_upload.py) fails the job unless this step
succeeded and left that record, so dry_run: false can never end green without
an upload.

Dry run: the workflow skips this step unless `dry_run` is false, and this
script also refuses unless MAY_DRY_RUN is exactly "false", so a real upload is
always an explicit choice. dry_run.py reports instead.

Any failure deletes the edit, and the log names the Play request that did not
complete (edit insert, track read, bundle upload, internal track update or
commit; a fixed literal). A failed or uncertain commit is never retried
automatically; inspect Play before retry. The caller must already hold
Samuel's protected-environment approval.

Failure messages are fixed literals. The one exception is a non-2xx reply,
whose message carries Play's numeric HTTP status and nothing else (for example
"(HTTP 403)"), so a disabled API, a bad credential and a missing app can be
told apart in the public log. Error bodies are still discarded unread; no body,
header or URL is ever printed. A network error or timeout has no status and
says "(no HTTP status)".

No third-party code: this runs in the release job, where only this repository's
Python, the system openssl and pinned tools run. The OAuth service-account
grant is built here (RS256 JWT signed by /usr/bin/openssl, exchanged once at
Google's fixed token endpoint), and requests use urllib. Nothing is retried:
there is no refresh-and-resend on 401 for the upload or the commit, and
redirects are refused so the bearer token is only ever sent to Google's API
host. A 401 fails the run and deletes the edit.
"""

import argparse
import base64
import http.client
import json
import os
import re
import subprocess
import time
import urllib.error
from urllib.parse import quote, urlencode, urlsplit
import urllib.request
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (PLAY_UPLOAD_SECRETS, PLAY_UPLOAD_TRACKS, StoreError, build_number_floor_ok,
                   check_android_signer, child_env, file_sha256, identity, play_account, play_identity,
                   require_step_secrets, runner_root, write_private, write_upload_record)
import verify

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
TOKEN_URI = "https://oauth2.googleapis.com/token"
OPENSSL = "/usr/bin/openssl"
ROLLED_OUT = ("completed", "inProgress", "halted")
MAX_VERSION_JUMP = 1000
# The only track this repository ever writes. dry_run.py reports this value.
TRACK = "internal"


class PlayRefusal(StoreError):
    """A deliberate refusal before any change. Its message is a fixed literal."""


# Play answered with a non-2xx HTTP status: rejected, or an uncertain outcome.
# The message is a fixed literal plus the numeric status alone, and only when the
# status is an int in 100..999 (http.client's own range for a status line);
# anything else reads "no HTTP status". Only `<response>.status_code` is passed
# in, and send() discards the error body unread, so no body, header or URL can
# reach the message. This is the one printed error whose message is not a single
# literal, so static_check.py pins this whole class (STATUS_ERROR_SOURCE): any
# edit to it fails the check until that pinned copy is updated in the same
# reviewed change.
class PlayStatusError(StoreError):
    """A non-2xx Play reply; the message names its numeric HTTP status and nothing else."""

    def __init__(self, status):
        if type(status) is int and 100 <= status <= 999:
            super().__init__("Google Play request was rejected or uncertain (HTTP " + str(status) + ")")
        else:
            super().__init__("Google Play request was rejected or uncertain (no HTTP status)")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A redirect becomes an HTTP error; the token never leaves Google's hosts.


OPENER = urllib.request.build_opener(NoRedirect)


class Response:
    def __init__(self, status_code, body):
        self.status_code, self.body = status_code, body

    def json(self):
        return json.loads(self.body)


def send(opener, request, timeout):
    try:
        with opener.open(request, timeout=timeout) as reply:
            return Response(reply.status, reply.read())
    except urllib.error.HTTPError as error:
        error.close()
        return Response(error.code, b"")  # Provider error bodies are discarded unread.
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        # No usable status (network, timeout, or a malformed or cut-off HTTP reply); nothing is read.
        raise StoreError("Google Play request was rejected or uncertain (no HTTP status)") from None


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def rs256(message, private_key_pem, work):
    """Sign with the service-account key via the system openssl; the key file is 0600 and removed."""
    key = work / "play-sa.pem"
    try:
        write_private(key, private_key_pem.encode())
        result = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key)], input=message,
                                capture_output=True, env=child_env())
    finally:
        key.unlink(missing_ok=True)
    if result.returncode or not result.stdout:
        raise StoreError("Play service-account assertion could not be signed")
    return result.stdout


def access_token(credential, work, opener=OPENER, now=None):
    if credential.get("token_uri", TOKEN_URI) != TOKEN_URI:
        raise StoreError("Play credential token endpoint is not Google's")
    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if isinstance(credential.get("private_key_id"), str):
        header["kid"] = credential["private_key_id"]
    claims = {"iss": credential["client_email"], "scope": SCOPE, "aud": TOKEN_URI,
              "iat": issued, "exp": issued + 600}
    signing_input = b64url(json.dumps(header).encode()) + b"." + b64url(json.dumps(claims).encode())
    assertion = signing_input + b"." + b64url(rs256(signing_input, credential["private_key"], work))
    body = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": assertion.decode()}).encode()
    reply = send(opener, urllib.request.Request(
        TOKEN_URI, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}), 30)
    token = checked_json(reply).get("access_token")
    if not isinstance(token, str) or not token:
        raise StoreError("Google did not issue a Play access token")
    return token


class PlaySession:
    """The small requests-like surface upload() uses, over urllib with a fixed bearer token."""

    def __init__(self, token, opener=OPENER):
        self.token, self.opener = token, opener

    def _request(self, method, url, json_body=None, data=None, headers=None, timeout=30):
        if not url.startswith(("https://androidpublisher.googleapis.com/androidpublisher/",
                               "https://androidpublisher.googleapis.com/upload/androidpublisher/")):
            raise StoreError("Refusing a request outside the Play Developer API")
        all_headers = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            all_headers["Content-Type"] = "application/json"
        return send(self.opener, urllib.request.Request(url, data=data, method=method, headers=all_headers),
                    timeout)

    def post(self, url, json=None, data=None, headers=None, timeout=30):
        return self._request("POST", url, json, data, headers, timeout)

    def get(self, url, timeout=30):
        return self._request("GET", url, timeout=timeout)

    def put(self, url, json=None, timeout=30):
        return self._request("PUT", url, json, timeout=timeout)

    def delete(self, url, timeout=30):
        return self._request("DELETE", url, timeout=timeout)


def checked_json(response):
    if not 200 <= response.status_code < 300:
        raise PlayStatusError(response.status_code)  # The numeric status only; the body was never read.
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
        raise PlayRefusal("First release must be uploaded in Play Console: no track of this app has a "
                          "rolled-out release, and Play's API cannot make an app's first release. "
                          "Upload the first build by hand (May production: a sign_only run, then one "
                          "Console upload to internal testing), then dispatch the next build here.")
    return max(codes)


def check_version(build_number, current):
    number = int(build_number)
    if number <= current:
        raise PlayRefusal("Build number must be greater than every versionCode on every Play track.")
    if number - current > MAX_VERSION_JUMP:
        raise PlayRefusal("Build number is more than 1000 above the highest Play versionCode.")


def internal_release(build_number):
    """The one track write this script makes: the new build, completed, on the internal track."""
    return {"track": TRACK, "releases": [{"versionCodes": [build_number], "status": "completed"}]}


def require_internal_release(track_url, body, build_number):
    """Refuse any track write but one completed internal release of exactly the new build."""
    folder, _, name = track_url.rpartition("/")
    if TRACK != "internal" or name != "internal" or not folder.endswith("/tracks") or \
            not isinstance(body, dict) or body.get("track") != "internal":
        raise PlayRefusal("Only the Play internal track may be written.")
    releases = body.get("releases")
    if set(body) != {"track", "releases"} or not isinstance(releases, list) or len(releases) != 1 or \
            not isinstance(releases[0], dict):
        raise PlayRefusal("The internal track write must carry exactly one release.")
    if set(releases[0]) != {"versionCodes", "status"}:
        raise PlayRefusal("A Play release may not carry a rollout fraction, country targeting or any other field.")
    if releases[0]["status"] != "completed":
        raise PlayRefusal("A Play release must be completed; staged, halted and draft releases are refused.")
    if releases[0]["versionCodes"] != [build_number]:
        raise PlayRefusal("A Play release may carry only the new build; promoting an existing build is refused.")


def require_commit(commit_url, edit_url):
    """Refuse any commit but ERROR_IF_IN_REVIEW of this edit; changesNotSentForReview is refused."""
    parts = urlsplit(commit_url)
    if parts.query != "changesInReviewBehavior=ERROR_IF_IN_REVIEW" or parts.fragment or \
            commit_url.partition("?")[0] != edit_url + ":commit":
        raise PlayRefusal("The Play commit must be exactly ERROR_IF_IN_REVIEW; changesNotSentForReview is refused.")


def name_failed_request(step):
    """Say in the log which Play request did not complete. `step` is always a literal from upload()."""
    print("May Play upload: the request that did not complete was the " + step +
          ("; no edit was made." if step == "edit insert" else "; the edit was deleted."), file=sys.stderr)
    if step in ("edit insert", "internal track update"):
        print("May Play upload: if this app has never had an upload in Play Console, that is the cause: "
              "Play's API cannot make an app's first release, so it must be uploaded by hand (README, "
              "\"First production upload\").", file=sys.stderr)


def delete_edit(session, url):
    try:
        session.delete(url, timeout=30)
    except Exception:
        pass  # Best effort; an abandoned edit expires and is never committed.


def upload(target, source_sha, build_number, session=None):
    package = identity("android", target, source_sha, build_number)
    require_step_secrets(PLAY_UPLOAD_SECRETS)
    if os.environ.get("MAY_DRY_RUN") != "false":
        raise PlayRefusal("Dry run: no store upload was requested.")
    if TRACK != "internal" or PLAY_UPLOAD_TRACKS.get(target) != TRACK:
        raise PlayRefusal("Only May and May Playground uploads to the Play internal track are allowed.")
    if not build_number_floor_ok("android", target, build_number):
        raise PlayRefusal("Build number is below this app's floor (May production starts at 3).")
    raw = os.environ.get("MAY_PLAY_JSON", "")
    play_identity(raw, play_account(target, os.environ.get("MAY_PLAY_ACCOUNT", "")))
    bundle = runner_root() / "may-release/signed.aab"
    if not bundle.is_file() or bundle.is_symlink() or bundle.stat().st_size == 0:
        raise StoreError("Signed Android App Bundle is unavailable")
    try:
        signer = verify.aab_signer_sha256(bundle)
    except verify.VerifyError:
        raise StoreError("Signed Android App Bundle has no single verifiable signer") from None
    check_android_signer(target, signer)
    verify.check_aab_identity(bundle, package, build_number)
    local_sha256 = file_sha256(bundle)
    if session is None:
        work = runner_root() / "may-private"
        work.mkdir(mode=0o700, exist_ok=True)
        session = PlaySession(access_token(json.loads(raw), work))
    path = quote(package, safe="")
    base = f"{API}/{path}/edits"
    try:
        edit = checked_json(session.post(base, json={}, timeout=30)).get("id")
    except StoreError:
        name_failed_request("edit insert")
        raise
    if not isinstance(edit, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", edit):
        raise StoreError("Google Play edit ID was invalid")
    step = "track read"
    try:
        tracks = checked_json(session.get(f"{base}/{edit}/tracks", timeout=30)).get("tracks", [])
        check_version(build_number, highest_version_code(tracks))
        step = "bundle upload"
        with bundle.open("rb") as stream:
            uploaded = checked_json(session.post(
                f"{UPLOAD_API}/{path}/edits/{edit}/bundles?uploadType=media",
                data=stream, headers={"Content-Type": "application/octet-stream",
                                      "Content-Length": str(bundle.stat().st_size)}, timeout=300))
        if str(uploaded.get("versionCode")) != build_number:
            raise StoreError("Google Play returned a different version code")
        if str(uploaded.get("sha256", "")).lower() != local_sha256:
            raise StoreError("Google Play returned a different bundle SHA-256")
        step = "internal track update"
        track_url, release = f"{base}/{edit}/tracks/{TRACK}", internal_release(build_number)
        require_internal_release(track_url, release, build_number)
        checked_json(session.put(track_url, json=release, timeout=30))
        step = "commit"
        commit_url = f"{base}/{edit}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW"
        require_commit(commit_url, f"{base}/{edit}")
        checked_json(session.post(commit_url, json={}, timeout=30))
    except BaseException as failure:
        delete_edit(session, f"{base}/{edit}")
        if isinstance(failure, StoreError) and not isinstance(failure, PlayRefusal):
            name_failed_request(step)
        raise
    report = write_upload_record("android", package, build_number, TRACK, local_sha256, edit)
    print("\n".join(report + ["Verify processing in Play Console separately."]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args(argv)
    try:
        upload(args.target, args.source_sha, args.build_number)
    except PlayRefusal as refusal:
        raise SystemExit("May Play upload refused before any change: " + refusal.args[0]) from None
    except verify.VerifyError as refusal:  # the bundle's own manifest, checked before any request
        raise SystemExit("May Play upload refused before any change: " + refusal.args[0]) from None
    except StoreError as error:
        raise SystemExit("May Play upload stopped: " + error.args[0] +
                         "; inspect the store before any retry") from None
    except Exception:
        # Provider/client exceptions can contain private response bodies or IDs.
        raise SystemExit("May Play upload failed or is uncertain; inspect the store before any retry") from None


if __name__ == "__main__":
    main()
