import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from play_upload import PlayRefusal, upload
from store import StoreError

ACCOUNT = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
CREDENTIAL = json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                         "client_email": ACCOUNT, "private_key": "dummy"})
BUNDLE = b"fixture signed bundle"
PLAYGROUND = "io.github.mayassistant.may.playground"


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
                              MAY_PLAY_ACCOUNT=ACCOUNT)
        self.env.start()

    def tearDown(self):
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

    def test_upload_step_refuses_signing_secrets(self):
        session = Session()
        with patch.dict(os.environ, MAY_ANDROID_KEYSTORE_BASE64="present"):
            with self.assertRaises(StoreError):
                upload("playground", "a" * 40, "9", session)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
