import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from store import identity, play_identity, release, StoreError
from play_upload import upload


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, tracks=None):
        self.calls = []
        self.tracks = tracks if tracks is not None else []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        if url.endswith("/edits"):
            return Response({"id": "edit-1"})
        if "bundles?" in url:
            return Response({"versionCode": 9})
        return Response({"id": "edit-1"})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return Response({"tracks": self.tracks})

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs["json"]))
        return Response({"track": "internal"})


class StoreTests(unittest.TestCase):
    def test_four_distinct_app_id_routes(self):
        sha = "a" * 40
        self.assertEqual(identity("ios", "playground", sha, "9"),
                         "io.github.mayassistant.may.playground")
        self.assertEqual(identity("android", "production", sha, "9"),
                         "io.github.mayassistant.may")
        for platform, target, build in (("rem", "production", "9"),
                                        ("ios", "rem", "9"), ("ios", "production", "09")):
            with self.assertRaises(StoreError):
                identity(platform, target, sha, build)

    def test_play_account_must_be_may_only(self):
        credential = {"type": "service_account", "project_id": "may-assistant-app",
                      "client_email": "may-play-upload@may-assistant-app.iam.gserviceaccount.com",
                      "private_key": "dummy"}
        play_identity(json.dumps(credential), credential["client_email"])
        with self.assertRaises(StoreError):
            play_identity(json.dumps(credential), "rem-play-upload@rem-claw.iam.gserviceaccount.com")

    def test_native_android_signing_does_not_receive_play_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, private = root / "may-source", root / "may-private"
            (source / "tools/release").mkdir(parents=True)
            (source / "tools/release/android.sh").write_text("exit 0\n")
            private.mkdir()
            account = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
            credential = json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                                     "client_email": account, "private_key": "dummy"})
            captured = {}

            def fake_private(argv, cwd, log, env):
                captured.update(env)
                signed = root / "may-android.fixture/signed.aab"
                signed.parent.mkdir()
                signed.write_bytes(b"signed")

            with patch.dict(os.environ, RUNNER_TEMP=directory, MAY_PLAY_JSON=credential,
                            MAY_PLAY_ACCOUNT=account), patch("store.prepared", return_value=(source, private)), \
                    patch("store.run_private", side_effect=fake_private):
                release("android", "playground", "a" * 40, "9")
            self.assertNotIn("MAY_PLAY_JSON", captured)
            self.assertNotIn("MAY_PLAY_ACCOUNT", captured)
            self.assertTrue((root / "may-release/signed.aab").is_file())

    def test_first_internal_upload_commits_without_touching_other_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "may-release").mkdir()
            (root / "may-release/signed.aab").write_bytes(b"fixture")
            account = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
            credential = json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                                     "client_email": account, "private_key": "dummy"})
            with patch.dict(os.environ, RUNNER_TEMP=directory, MAY_PLAY_JSON=credential,
                            MAY_PLAY_ACCOUNT=account):
                session = Session()
                upload("playground", "a" * 40, "9", session)
                self.assertEqual([call[0] for call in session.calls],
                                 ["POST", "GET", "POST", "PUT", "POST"])
                self.assertIn("/tracks/internal", session.calls[3][1])
                self.assertEqual(session.calls[3][2]["releases"][0]["status"], "completed")
                self.assertIn("ERROR_IF_IN_REVIEW", session.calls[4][1])
                self.assertTrue(all("io.github.mayassistant.may.playground" in call[1]
                                    for call in session.calls))
                existing = Session([{"track": "internal", "releases": [{"versionCodes": ["8"]}]}])
                with self.assertRaises(StoreError):
                    upload("playground", "a" * 40, "9", existing)
                self.assertEqual([call[0] for call in existing.calls], ["POST", "GET"])


if __name__ == "__main__":
    unittest.main()
