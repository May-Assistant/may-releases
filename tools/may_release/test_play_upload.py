import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import traceback
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs

import play_upload
from play_upload import PlayRefusal, PlaySession, PlayStatusError, access_token, upload
from store import StoreError

HAS_OPENSSL = os.path.exists(play_upload.OPENSSL)

ACCOUNT = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
CREDENTIAL = json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                         "client_email": ACCOUNT, "private_key": "dummy"})
BUNDLE = b"fixture signed bundle"
PLAYGROUND = "io.github.mayassistant.may.playground"
# Planted in every part of a fake Play error (body, reason, header, URL) and in
# the bearer token: none of it may reach any output or exception text.
SECRET = "MAY-PLANTED-SECRET-7f3c9a"
TOKEN = "ya29." + SECRET + "-bearer"


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


class Session:
    """Records calls; answers like Play for an app whose first release is live."""

    def __init__(self, tracks=None, uploaded=None, commit_status=200):
        self.calls = []
        self.tracks = tracks if tracks is not None else [
            {"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}]
        self.uploaded = uploaded if uploaded is not None else {
            "versionCode": 9, "sha256": hashlib.sha256(BUNDLE).hexdigest()}
        self.commit_status = commit_status

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs.get("json")))
        if url.endswith("/edits"):
            return Response({"id": "edit-1"})
        if "bundles?" in url:
            return Response(self.uploaded)
        return Response({"id": "edit-1"}, self.commit_status)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        return Response({"tracks": self.tracks})

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs["json"]))
        return Response({"track": "internal"})

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, None))
        return Response({})

    def methods(self):
        return [call[0] for call in self.calls]


class PlayUploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "may-release").mkdir()
        (root / "may-release/signed.aab").write_bytes(BUNDLE)
        self.env = patch.dict(os.environ, RUNNER_TEMP=self.directory.name, MAY_PLAY_JSON=CREDENTIAL,
                              MAY_PLAY_ACCOUNT=ACCOUNT, MAY_DRY_RUN="false")
        self.env.start()
        self.output = patch("sys.stdout", new_callable=io.StringIO)  # the upload report
        self.output.start()

    def tearDown(self):
        self.output.stop()
        self.env.stop()
        self.directory.cleanup()

    def test_update_commits_only_internal_completed_release(self):
        session = Session(tracks=[
            {"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]},
            {"track": "alpha", "releases": [{"versionCodes": ["7"], "status": "completed"}]},
            {"track": "production", "releases": []}])
        upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "PUT", "POST"])
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0][1].endswith("/edits/edit-1/tracks/internal"))
        self.assertEqual(puts[0][2], {"track": "internal",
                                      "releases": [{"versionCodes": ["9"], "status": "completed"}]})
        self.assertIn(":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW", session.calls[-1][1])
        self.assertTrue(all("/" + PLAYGROUND + "/" in call[1] for call in session.calls))

    def test_app_without_a_release_is_refused_with_fixed_message(self):
        for tracks in ([], [{"track": "internal"}], [{"track": "internal", "releases": []}],
                       [{"track": "internal", "releases": [{"versionCodes": ["3"], "status": "draft"}]}]):
            with self.subTest(tracks=tracks):
                session = Session(tracks=tracks)
                with self.assertRaises(PlayRefusal) as refused:
                    upload("playground", "a" * 40, "9", session)
                self.assertEqual(refused.exception.args[0], "First release must be uploaded in Play Console.")
                self.assertEqual(session.methods(), ["POST", "GET", "DELETE"])

    def test_version_code_must_exceed_every_track(self):
        for build_number, tracks in (
                ("9", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]},
                       {"track": "production", "releases": [{"versionCodes": ["12"], "status": "completed"}]}]),
                ("8", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}]),
                ("9", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"},
                                                         {"versionCodes": ["10"], "status": "draft"}]}])):
            with self.subTest(build_number=build_number):
                session = Session(tracks=tracks)
                with self.assertRaises(PlayRefusal):
                    upload("playground", "a" * 40, build_number, session)
                self.assertEqual(session.methods(), ["POST", "GET", "DELETE"])

    def test_version_code_jump_ceiling(self):
        session = Session(tracks=[{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}],
                          uploaded={"versionCode": 1008, "sha256": hashlib.sha256(BUNDLE).hexdigest()})
        upload("playground", "a" * 40, "1008", session)
        session = Session()
        with self.assertRaises(PlayRefusal):
            upload("playground", "a" * 40, "1009", session)
        self.assertEqual(session.methods(), ["POST", "GET", "DELETE"])

    def test_play_sha256_must_match_local_bundle(self):
        session = Session(uploaded={"versionCode": 9, "sha256": "0" * 64})
        with self.assertRaises(StoreError):
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "DELETE"])
        session = Session(uploaded={"versionCode": 9})
        with self.assertRaises(StoreError):
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.methods()[-1], "DELETE")

    def test_play_version_code_must_match(self):
        session = Session(uploaded={"versionCode": 10, "sha256": hashlib.sha256(BUNDLE).hexdigest()})
        with self.assertRaises(StoreError):
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "DELETE"])

    def test_failed_commit_deletes_edit(self):
        session = Session(commit_status=400)
        with self.assertRaises(StoreError):
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "PUT", "POST", "DELETE"])
        self.assertTrue(session.calls[-1][1].endswith("/edits/edit-1"))

    def test_malformed_tracks_delete_edit(self):
        for tracks in ("tracks", [None], [{"releases": "x"}], [{"releases": [{"versionCodes": ["x"]}]}]):
            with self.subTest(tracks=tracks):
                session = Session(tracks=tracks)
                with self.assertRaises(StoreError):
                    upload("playground", "a" * 40, "9", session)
                self.assertEqual(session.methods()[-1], "DELETE")

    def test_only_the_internal_track_of_may_playground_is_reachable(self):
        # The one write is the internal track (test above); nothing else can be chosen.
        self.assertEqual(play_upload.TRACK, "internal")
        self.assertEqual(play_upload.PLAY_UPLOAD_TRACKS, {"playground": "internal"})
        # The real May app is refused in stage 2, before any request.
        session = Session()
        with self.assertRaises(PlayRefusal) as refused:
            upload("production", "a" * 40, "9", session)
        self.assertEqual(refused.exception.args[0], "Only May Playground uploads to the Play internal track are allowed.")
        self.assertEqual(session.calls, [])
        # Neither a non-internal track in the table nor a changed TRACK gets past it.
        for track in ("production", "beta", "alpha", "qa"):
            for patched in (patch("play_upload.PLAY_UPLOAD_TRACKS", {"playground": track}),
                            patch("play_upload.TRACK", track)):
                session = Session()
                with self.subTest(track=track), patched, self.assertRaises(PlayRefusal):
                    upload("playground", "a" * 40, "9", session)
                self.assertEqual(session.calls, [])
        with patch("play_upload.TRACK", "production"), \
                patch("play_upload.PLAY_UPLOAD_TRACKS", {"playground": "production"}), self.assertRaises(PlayRefusal):
            upload("playground", "a" * 40, "9", Session())

    def test_successful_upload_records_and_reports_what_it_committed(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            upload("playground", "a" * 40, "9", Session())
        record = json.loads((Path(self.directory.name) / "may-release/upload-record.json").read_text())
        self.assertEqual(record, {"platform": "android", "app_id": PLAYGROUND, "build_number": "9",
                                  "track": "internal", "sha256": hashlib.sha256(BUNDLE).hexdigest(),
                                  "store_id": "edit-1"})
        self.assertEqual(output.getvalue().splitlines()[1:6], [
            "  package: " + PLAYGROUND, "  versionCode: 9", "  track: internal",
            "  signed AAB SHA-256: " + hashlib.sha256(BUNDLE).hexdigest(), "  Play edit (committed): edit-1"])
        # A refused or failed upload leaves no record, so the loud-failure step fails.
        (Path(self.directory.name) / "may-release/upload-record.json").unlink()
        for session in (Session(commit_status=400), Session(uploaded={"versionCode": 9, "sha256": "0" * 64})):
            with self.assertRaises(StoreError):
                upload("playground", "a" * 40, "9", session)
            self.assertFalse((Path(self.directory.name) / "may-release/upload-record.json").exists())

    def run_cli(self, failure):
        """Run play_upload's CLI against Play answering the tracks read with `failure`.

        Returns (exit message, every printed line, the full formatted exit).
        """
        opener = Opener([Reply(200, b'{"id": "edit-1"}'), failure, Reply(204, b"")])
        with patch("play_upload.access_token", return_value=TOKEN), \
                patch("play_upload.PlaySession", lambda token: PlaySession(token, opener)), \
                patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("sys.stderr", new_callable=io.StringIO) as errors, \
                self.assertRaises(SystemExit) as stopped:
            play_upload.main(["--target", "playground", "--source-sha", "a" * 40, "--build-number", "9"])
        self.assertEqual([request.get_method() for request in opener.requests], ["POST", "GET", "DELETE"])
        exit_text = "".join(traceback.format_exception(stopped.exception))
        return stopped.exception.code, output.getvalue() + errors.getvalue(), exit_text

    def assertNoSecret(self, *texts):
        for text in texts:
            self.assertNotIn(SECRET, text)

    def play_error(self, status):
        return urllib.error.HTTPError(
            "https://androidpublisher.googleapis.com/x?access_token=" + SECRET, status, "Denied " + SECRET,
            {"X-Debug": SECRET}, io.BytesIO(b'{"error": {"status": "SERVICE_DISABLED", "detail": "' +
                                            SECRET.encode() + b'"}}'))

    def test_each_play_http_status_is_named_and_the_body_is_never_printed(self):
        for status in (400, 401, 403, 404, 500):
            with self.subTest(status=status):
                message, printed, exit_text = self.run_cli(self.play_error(status))
                self.assertEqual(message, "May Play upload stopped: Google Play request was rejected or uncertain "
                                          f"(HTTP {status}); inspect the store before any retry")
                self.assertNoSecret(message, printed, exit_text)
                # The StoreError itself: the code alone, still a StoreError, never a refusal.
                opener = Opener([Reply(200, b'{"id": "edit-1"}'), self.play_error(status), Reply(204, b"")])
                with self.assertRaises(StoreError) as raised:
                    upload("playground", "a" * 40, "9", PlaySession(TOKEN, opener))
                self.assertNotIsInstance(raised.exception, PlayRefusal)
                self.assertEqual(raised.exception.args, (
                    f"Google Play request was rejected or uncertain (HTTP {status})",))
                self.assertNoSecret(str(raised.exception), repr(raised.exception),
                                    "".join(traceback.format_exception(raised.exception)))

    def test_network_failure_says_no_http_status(self):
        for failure in (urllib.error.URLError("unreachable " + SECRET), TimeoutError(SECRET), OSError(SECRET)):
            with self.subTest(failure=type(failure).__name__):
                message, printed, exit_text = self.run_cli(failure)
                self.assertEqual(message, "May Play upload stopped: Google Play request was rejected or uncertain "
                                          "(no HTTP status); inspect the store before any retry")
                self.assertNoSecret(message, printed, exit_text)

    def test_status_message_carries_only_an_integer_status(self):
        self.assertEqual(PlayStatusError(403).args, ("Google Play request was rejected or uncertain (HTTP 403)",))
        for status in (None, "403", "403 " + SECRET, True, 403.0, 99, 1000, -1):
            with self.subTest(status=status):
                self.assertEqual(PlayStatusError(status).args,
                                 ("Google Play request was rejected or uncertain (no HTTP status)",))

    def test_upload_step_refuses_signing_secrets(self):
        session = Session()
        with patch.dict(os.environ, MAY_ANDROID_KEYSTORE_BASE64="present"):
            with self.assertRaises(StoreError):
                upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.calls, [])


class Reply:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def open(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class StdlibClientTests(unittest.TestCase):
    """The release job's Play client: stdlib urllib plus the system openssl, no retries."""

    def test_session_sends_bearer_only_to_the_play_api_and_never_retries(self):
        opener = Opener([Reply(200, b'{"id": "edit-1"}'),
                         urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"secret body"))])
        session = PlaySession("token-1", opener)
        self.assertEqual(session.post(play_upload.API + "/x/edits", json={}).json(), {"id": "edit-1"})
        unauthorized = session.put(play_upload.API + "/x/edits/1/tracks/internal", json={})
        self.assertEqual((unauthorized.status_code, unauthorized.body), (401, b""))
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer token-1")
        for url in ("https://example.com/androidpublisher/v3", "http://androidpublisher.googleapis.com/androidpublisher/"):
            with self.assertRaises(StoreError):
                session.get(url)
        self.assertEqual(len(opener.requests), 2)

    def test_redirects_are_not_followed(self):
        handler = play_upload.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/"))

    def test_token_endpoint_must_be_googles(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StoreError):
                access_token({"token_uri": "https://evil.example/token", "client_email": "x",
                              "private_key": "x"}, Path(directory), Opener([]))

    @unittest.skipUnless(HAS_OPENSSL or os.environ.get("CI"), "system openssl is required")
    def test_service_account_assertion_is_a_valid_rs256_jwt(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            subprocess.run([play_upload.OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(work / "k.pem")], check=True, capture_output=True)
            subprocess.run([play_upload.OPENSSL, "pkey", "-in", str(work / "k.pem"), "-pubout",
                            "-out", str(work / "pub.pem")], check=True, capture_output=True)
            credential = {"type": "service_account", "client_email": ACCOUNT, "private_key_id": "kid-1",
                          "private_key": (work / "k.pem").read_text(), "token_uri": play_upload.TOKEN_URI}
            opener = Opener([Reply(200, b'{"access_token": "issued", "expires_in": 3599}')])
            self.assertEqual(access_token(credential, work, opener, now=1_700_000_000), "issued")
            request = opener.requests[0]
            self.assertEqual(request.full_url, play_upload.TOKEN_URI)
            assertion = parse_qs(request.data.decode())["assertion"][0]
            header, claims, signature = assertion.split(".")
            pad = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            self.assertEqual(json.loads(pad(header)), {"alg": "RS256", "typ": "JWT", "kid": "kid-1"})
            self.assertEqual(json.loads(pad(claims)), {"iss": ACCOUNT, "scope": play_upload.SCOPE,
                                                       "aud": play_upload.TOKEN_URI,
                                                       "iat": 1_700_000_000, "exp": 1_700_000_600})
            (work / "sig").write_bytes(pad(signature))
            verified = subprocess.run([play_upload.OPENSSL, "dgst", "-sha256", "-verify", str(work / "pub.pem"),
                                       "-signature", str(work / "sig")], input=(header + "." + claims).encode(),
                                      capture_output=True)
            self.assertEqual(verified.returncode, 0)
            self.assertEqual(sorted(p.name for p in work.iterdir()), ["k.pem", "pub.pem", "sig"])


if __name__ == "__main__":
    unittest.main()
