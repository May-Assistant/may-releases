import base64
import hashlib
import http.client
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
from play_upload import (PlayRefusal, PlaySession, PlayStatusError, access_token, internal_release,
                         require_commit, require_internal_release, upload)
import signing_fixtures as fx
import store
from store import StoreError
import verify

HAS_OPENSSL = os.path.exists(play_upload.OPENSSL)

PLAYGROUND_ACCOUNT = "may-playground-upload@may-assistant-app.iam.gserviceaccount.com"
PRODUCTION_ACCOUNT = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
ACCOUNT = PLAYGROUND_ACCOUNT


def credential(account):
    return json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                       "client_email": account, "private_key": "dummy"})


CREDENTIAL = credential(ACCOUNT)
# A real jarsigner-signed fixture bundle (signing_fixtures.SIGNED_AAB), signed by a
# throwaway key: Play never sees it. Tests make that key one target's upload key
# (upload_keys) to show the other target refuses it.
PLAYGROUND = "io.github.mayassistant.may.playground"
PRODUCTION = "io.github.mayassistant.may"
BUNDLE = fx.signed_bundle(PLAYGROUND, "9")  # May Playground, versionCode 9
PRODUCTION_BUNDLE = fx.signed_bundle(PRODUCTION, "3")  # May production, versionCode 3
FIXTURE_CERT = verify.normalize_fingerprint(fx.AAB_CERT_SHA256)
OTHER_CERT = "0123456789abcdef" * 4
COMMITTED_KEYS = store.ANDROID_UPLOAD_KEYS  # as committed, before any test patches it


def refusal(test, kind, call, *args):
    """The exception call(*args) raises, failing the test on an assertion unless it is a `kind`."""
    try:
        call(*args)
    except Exception as error:  # any other outcome is an assertion failure, never a test error
        test.assertIsInstance(error, kind)
        return error
    test.fail(kind.__name__ + " not raised")


def upload_keys(playground=FIXTURE_CERT, production=OTHER_CERT):
    """store.ANDROID_UPLOAD_KEYS with the real aliases and accounts and the given certificate pins."""
    return {"production": dict(COMMITTED_KEYS["production"], cert=production),
            "playground": dict(COMMITTED_KEYS["playground"], cert=playground)}
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

    def __init__(self, tracks=None, uploaded=None, commit_status=200, insert_status=200, put_status=200):
        self.calls = []
        self.insert_status, self.put_status = insert_status, put_status
        self.tracks = tracks if tracks is not None else [
            {"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}]
        self.uploaded = uploaded if uploaded is not None else {
            "versionCode": 9, "sha256": hashlib.sha256(BUNDLE).hexdigest()}
        self.commit_status = commit_status

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs.get("json")))
        if url.endswith("/edits"):
            return Response({"id": "edit-1"}, self.insert_status)
        if "bundles?" in url:
            return Response(self.uploaded)
        return Response({"id": "edit-1"}, self.commit_status)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        return Response({"tracks": self.tracks})

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs["json"]))
        return Response({"track": "internal"}, self.put_status)

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
        # The fixture bundle's signer is the Playground upload key in these tests.
        self.keys = patch("store.ANDROID_UPLOAD_KEYS", upload_keys())
        self.keys.start()
        self.output = patch("sys.stdout", new_callable=io.StringIO)  # the upload report
        self.output.start()

    def bundle(self, content):
        """Put `content` on the runner as the signed bundle, returning what Play would report for it."""
        (Path(self.directory.name) / "may-release/signed.aab").write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def tearDown(self):
        self.output.stop()
        self.keys.stop()
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
                self.assertEqual(refused.exception.args[0], FIRST_RELEASE)
                self.assertEqual(session.methods(), ["POST", "GET", "DELETE"])

    def test_version_code_must_exceed_every_track(self):
        for build_number, tracks in (
                ("9", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]},
                       {"track": "production", "releases": [{"versionCodes": ["12"], "status": "completed"}]}]),
                ("8", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}]),
                ("9", [{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"},
                                                         {"versionCodes": ["10"], "status": "draft"}]}])):
            with self.subTest(build_number=build_number):
                self.bundle(fx.signed_bundle(PLAYGROUND, build_number))
                session = Session(tracks=tracks)
                with self.assertRaises(PlayRefusal):
                    upload("playground", "a" * 40, build_number, session)
                self.assertEqual(session.methods(), ["POST", "GET", "DELETE"])

    def test_version_code_jump_ceiling(self):
        sha = self.bundle(fx.signed_bundle(PLAYGROUND, "1008"))
        session = Session(tracks=[{"track": "internal", "releases": [{"versionCodes": ["8"], "status": "completed"}]}],
                          uploaded={"versionCode": 1008, "sha256": sha})
        upload("playground", "a" * 40, "1008", session)
        self.bundle(fx.signed_bundle(PLAYGROUND, "1009"))
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

    def test_only_the_internal_track_of_may_and_may_playground_is_reachable(self):
        # The one write is the internal track (test above); nothing else can be chosen.
        self.assertEqual(play_upload.TRACK, "internal")
        self.assertEqual(play_upload.PLAY_UPLOAD_TRACKS, {"playground": "internal", "production": "internal"})
        # Neither a non-internal track in the table nor a changed TRACK gets past it, for either app.
        for target in ("playground", "production"):
            for track in ("production", "beta", "alpha", "qa"):
                for patched in (patch("play_upload.PLAY_UPLOAD_TRACKS", {"playground": track, "production": track}),
                                patch("play_upload.TRACK", track)):
                    session = Session()
                    with self.subTest(target=target, track=track), patched:
                        self.assertEqual(refusal(self, PlayRefusal, upload, target, "a" * 40, "9", session).args[0],
                                         "Only May and May Playground uploads to the Play internal track are allowed.")
                        self.assertEqual(session.calls, [])
        with patch("play_upload.PLAY_UPLOAD_TRACKS", {"playground": "internal"}), self.assertRaises(PlayRefusal):
            upload("production", "a" * 40, "9", Session())

    def production(self, production_cert=FIXTURE_CERT, playground_cert=OTHER_CERT, account=PRODUCTION_ACCOUNT):
        """Environment and key table for a May production upload whose signer is `production_cert`'s key."""
        return patch.dict(os.environ, MAY_PLAY_ACCOUNT=account, MAY_PLAY_JSON=credential(account)), \
            patch("store.ANDROID_UPLOAD_KEYS", upload_keys(playground=playground_cert, production=production_cert))

    def test_may_production_uploads_to_its_own_internal_track(self):
        env, keys = self.production()
        sha = self.bundle(PRODUCTION_BUNDLE)
        session = Session(tracks=[{"track": "internal", "releases": [{"versionCodes": ["2"], "status": "completed"}]}],
                          uploaded={"versionCode": 3, "sha256": sha})
        with env, keys, patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("play_upload.require_internal_release", wraps=require_internal_release) as track_guard, \
                patch("play_upload.require_commit", wraps=require_commit) as commit_guard:
            upload("production", "a" * 40, "3", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "PUT", "POST"])
        # Both writes went through their guards, with exactly what was sent.
        self.assertEqual((track_guard.call_count, commit_guard.call_count), (1, 1))
        self.assertEqual(track_guard.call_args[0][:2], (session.calls[3][1], session.calls[3][2]))
        self.assertEqual(commit_guard.call_args[0][0], session.calls[4][1])
        # Every request names exactly the May package, never May Playground's.
        self.assertTrue(all("/" + PRODUCTION + "/edits" in call[1] for call in session.calls))
        self.assertFalse(any(PLAYGROUND in call[1] for call in session.calls))
        put = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(put), 1)
        self.assertTrue(put[0][1].endswith("/edits/edit-1/tracks/internal"))
        self.assertEqual(put[0][2], {"track": "internal", "releases": [{"versionCodes": ["3"], "status": "completed"}]})
        record = json.loads((Path(self.directory.name) / "may-release/upload-record.json").read_text())
        self.assertEqual((record["app_id"], record["track"], record["build_number"]), (PRODUCTION, "internal", "3"))
        self.assertIn("  package: " + PRODUCTION + "\n", output.getvalue())

    def test_a_playground_key_or_account_never_uploads_as_may_production(self):
        # The fixture key is Playground's upload key; May production's is another.
        env, keys = self.production(production_cert=OTHER_CERT, playground_cert=FIXTURE_CERT)
        session = Session()
        with env, keys:
            self.assertEqual(refusal(self, StoreError, upload, "production", "a" * 40, "9", session).args[0],
                             "Signed Android App Bundle is not signed with this target's upload key")
        self.assertEqual(session.calls, [])
        # Nor with the committed production pin: the fixture key is not May's.
        env, _ = self.production()
        session = Session()
        with env, patch("store.ANDROID_UPLOAD_KEYS", COMMITTED_KEYS), self.assertRaises(StoreError):
            upload("production", "a" * 40, "9", session)
        self.assertEqual(session.calls, [])
        # Playground's Play account is refused for May production, before any request.
        env, keys = self.production(account=PLAYGROUND_ACCOUNT)
        session = Session()
        with env, keys:
            self.assertEqual(refusal(self, StoreError, upload, "production", "a" * 40, "9", session).args[0],
                             "MAY_PLAY_ACCOUNT is not this target's Play service account")
        self.assertEqual(session.calls, [])

    def test_a_may_production_key_or_account_never_uploads_as_playground(self):
        # The fixture key is now May production's upload key; Playground's is another.
        session = Session()
        with patch("store.ANDROID_UPLOAD_KEYS", upload_keys(playground=OTHER_CERT, production=FIXTURE_CERT)), \
                self.assertRaises(StoreError) as refused:
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(refused.exception.args[0], "Signed Android App Bundle is not signed with this target's upload key")
        self.assertEqual(session.calls, [])
        session = Session()
        with patch.dict(os.environ, MAY_PLAY_ACCOUNT=PRODUCTION_ACCOUNT, MAY_PLAY_JSON=credential(PRODUCTION_ACCOUNT)), \
                self.assertRaises(StoreError) as refused:
            upload("playground", "a" * 40, "9", session)
        self.assertEqual(refused.exception.args[0], "MAY_PLAY_ACCOUNT is not this target's Play service account")
        self.assertEqual(session.calls, [])

    def test_the_bundles_own_package_and_version_code_must_be_this_build(self):
        """The release side reads the signed bundle's manifest; it does not trust the build job's check."""
        env, keys = self.production()
        cases = (
            # (target, the bundle's own manifest, requested build number, refusal)
            ("production", fx.proto_manifest(PLAYGROUND, "3"), "3", "Bundle package is not this target's app"),
            ("production", fx.proto_manifest(PRODUCTION, "4"), "3",
             "Bundle versionCode is not the requested build number"),
            ("production", fx.MANIFEST_PLAYGROUND_VC9, "9", "Bundle package is not this target's app"),
            ("production", fx.proto_manifest(PRODUCTION, "3", fx.attribute(b"versionCodeMajor", b"1", fx.ANDROID_NS, 1)),
             "3", "Bundle manifest sets versionCodeMajor"),
            ("production", b"fixture manifest", "3", "Bundle manifest is not a readable aapt2 manifest"),
        )
        for target, manifest, number, message in cases:
            self.bundle(fx.with_manifest(manifest))
            session = Session()
            with self.subTest(manifest=manifest[:40], number=number), env, keys:
                self.assertEqual(refusal(self, verify.VerifyError, upload, target, "a" * 40, number, session).args[0],
                                 message)
                self.assertEqual(session.calls, [])
        # And the reverse: May's own manifest is never uploaded as May Playground.
        self.bundle(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
        session = Session()
        self.assertEqual(refusal(self, verify.VerifyError, upload, "playground", "a" * 40, "3", session).args[0],
                         "Bundle package is not this target's app")
        self.assertEqual(session.calls, [])

    def test_a_real_aapt2_manifest_uploads(self):
        env, keys = self.production()
        sha = self.bundle(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
        session = Session(tracks=[{"track": "internal", "releases": [{"versionCodes": ["2"], "status": "completed"}]}],
                          uploaded={"versionCode": 3, "sha256": sha})
        with env, keys:
            upload("production", "a" * 40, "3", session)
        self.assertEqual(session.methods(), ["POST", "GET", "POST", "PUT", "POST"])

    def test_cli_refuses_another_apps_manifest_before_any_change(self):
        self.bundle(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as stopped:
            play_upload.main(["--target", "playground", "--source-sha", "a" * 40, "--build-number", "9"])
        self.assertEqual(stopped.exception.code,
                         "May Play upload refused before any change: Bundle package is not this target's app")

    def test_an_unsigned_or_doubly_signed_bundle_is_refused_before_any_request(self):
        bundle = Path(self.directory.name) / "may-release/signed.aab"
        for content in (b"not a zip", b""):
            bundle.write_bytes(content)
            session = Session()
            with self.subTest(content=content), self.assertRaises(StoreError):
                upload("playground", "a" * 40, "9", session)
            self.assertEqual(session.calls, [])

    def test_may_production_build_number_floor_is_3(self):
        env, keys = self.production()
        for number in ("1", "2"):
            session = Session()
            with self.subTest(number=number), env, keys:
                self.assertEqual(refusal(self, PlayRefusal, upload, "production", "a" * 40, number, session).args[0],
                                 "Build number is below this app's floor (May production starts at 3).")
                self.assertEqual(session.calls, [])
        # Playground has no floor here beyond Play's own check.
        sha = self.bundle(fx.signed_bundle(PLAYGROUND, "2"))
        session = Session(tracks=[{"track": "internal", "releases": [{"versionCodes": ["1"], "status": "completed"}]}],
                          uploaded={"versionCode": 2, "sha256": sha})
        upload("playground", "a" * 40, "2", session)
        self.assertEqual(session.methods()[-1], "POST")

    def test_only_one_completed_internal_release_of_the_new_build_is_written(self):
        base = play_upload.API + "/" + PRODUCTION + "/edits/edit-1"
        url = base + "/tracks/internal"
        self.assertIsNone(require_internal_release(url, internal_release("9"), "9"))
        release = {"versionCodes": ["9"], "status": "completed"}
        cases = {
            "Only the Play internal track may be written.": [
                (base + "/tracks/production", {"track": "internal", "releases": [release]}),
                (base + "/tracks/beta", {"track": "internal", "releases": [release]}),
                (base + "/internal", {"track": "internal", "releases": [release]}),
                (url, {"track": "production", "releases": [release]}),
                (url, ["internal"])],
            "The internal track write must carry exactly one release.": [
                (url, {"track": "internal", "releases": [release, release]}),
                (url, {"track": "internal", "releases": []}),
                (url, {"track": "internal", "releases": [release], "countryTargeting": {}})],
            "A Play release may not carry a rollout fraction, country targeting or any other field.": [
                (url, {"track": "internal", "releases": [dict(release, userFraction=0.1)]}),
                (url, {"track": "internal", "releases": [dict(release, countryTargeting={"countries": ["US"]})]}),
                (url, {"track": "internal", "releases": [dict(release, inAppUpdatePriority=5)]}),
                (url, {"track": "internal", "releases": [dict(release, name="x")]})],
            "A Play release must be completed; staged, halted and draft releases are refused.": [
                (url, {"track": "internal", "releases": [dict(release, status=status)]})
                for status in ("inProgress", "halted", "draft", "statusUnspecified")],
            "A Play release may carry only the new build; promoting an existing build is refused.": [
                (url, {"track": "internal", "releases": [dict(release, versionCodes=codes)]})
                for codes in (["8"], ["8", "9"], [9], [])],
        }
        for message, writes in cases.items():
            for track_url, body in writes:
                with self.subTest(track_url=track_url, body=body):
                    self.assertEqual(refusal(self, PlayRefusal, require_internal_release, track_url, body, "9").args[0],
                                     message)
        with patch("play_upload.TRACK", "production"), self.assertRaises(PlayRefusal):
            require_internal_release(url, internal_release("9"), "9")

    def test_a_changed_track_write_is_refused_before_it_is_sent_and_the_edit_deleted(self):
        for body in ({"track": "internal", "releases": [{"versionCodes": ["9"], "status": "inProgress",
                                                          "userFraction": 0.5}]},
                     {"track": "production", "releases": [{"versionCodes": ["9"], "status": "completed"}]}):
            session = Session()
            with self.subTest(body=body), patch("play_upload.internal_release", return_value=body), \
                    self.assertRaises(PlayRefusal):
                upload("playground", "a" * 40, "9", session)
            self.assertEqual(session.methods(), ["POST", "GET", "POST", "DELETE"])
            self.assertFalse((Path(self.directory.name) / "may-release/upload-record.json").exists())

    def test_the_commit_is_only_error_if_in_review(self):
        edit = play_upload.API + "/" + PRODUCTION + "/edits/edit-1"
        self.assertIsNone(require_commit(edit + ":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW", edit))
        for url in (edit + ":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW&changesNotSentForReview=true",
                    edit + ":commit?changesNotSentForReview=true&changesInReviewBehavior=ERROR_IF_IN_REVIEW",
                    edit + ":commit?changesNotSentForReview=true",
                    edit + ":commit?changesInReviewBehavior=CANCEL_IN_REVIEW_AND_SUBMIT",
                    edit + ":commit",
                    edit + ":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW#x",
                    edit + "-2:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW",
                    edit + ":validate?changesInReviewBehavior=ERROR_IF_IN_REVIEW"):
            with self.subTest(url=url):
                self.assertEqual(refusal(self, PlayRefusal, require_commit, url, edit).args[0],
                                 "The Play commit must be exactly ERROR_IF_IN_REVIEW; changesNotSentForReview is refused.")

    def test_the_log_names_the_play_request_that_did_not_complete(self):
        for session, step, hint in ((Session(insert_status=403), "edit insert", True),
                                    (Session(put_status=400), "internal track update", True),
                                    (Session(commit_status=400), "commit", False),
                                    (Session(uploaded={"versionCode": 9, "sha256": "0" * 64}), "bundle upload", False)):
            with self.subTest(step=step), patch("sys.stderr", new_callable=io.StringIO) as errors, \
                    self.assertRaises(StoreError):
                upload("playground", "a" * 40, "9", session)
            self.assertIn("May Play upload: the request that did not complete was the " + step +
                          ("; no edit was made.\n" if step == "edit insert" else "; the edit was deleted.\n"),
                          errors.getvalue())
            self.assertEqual("cannot make an app's first release" in errors.getvalue(), hint)
        # A refusal (no release yet, versionCode) names no failed request: its own message says why.
        with patch("sys.stderr", new_callable=io.StringIO) as errors, self.assertRaises(PlayRefusal):
            upload("playground", "a" * 40, "9", Session(tracks=[]))
        self.assertEqual(errors.getvalue(), "")

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
        for failure in (urllib.error.URLError("unreachable " + SECRET), TimeoutError(SECRET), OSError(SECRET),
                        http.client.BadStatusLine(SECRET), http.client.RemoteDisconnected(SECRET),
                        http.client.IncompleteRead(SECRET.encode())):
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


class CommittedUploadKeyTests(unittest.TestCase):
    """The committed table itself, unpatched."""

    def test_the_committed_upload_keys_are_distinct_per_target(self):
        keys = store.ANDROID_UPLOAD_KEYS
        self.assertIs(keys, COMMITTED_KEYS)
        self.assertEqual(sorted(keys), ["playground", "production"])
        self.assertEqual(keys["production"], {
            "alias": "may-upload", "account": PRODUCTION_ACCOUNT,
            "cert": "9e2fe9c8e70421fcbfbbfb4ea8f30942915ecfbd8ffef18989a309fc1e899815"})
        self.assertEqual(keys["playground"], {"alias": "may-playground-upload", "account": PLAYGROUND_ACCOUNT,
                                              "cert": "db087802f802f2a1"})
        production, playground = keys["production"]["cert"], keys["playground"]["cert"]
        self.assertFalse(production.startswith(playground) or playground.startswith(production))
        self.assertTrue(store.android_cert_matches("production", production))
        self.assertFalse(store.android_cert_matches("playground", production))
        self.assertFalse(store.android_cert_matches("production", playground + "0" * 48))
        self.assertTrue(store.android_cert_matches("playground", playground + "0" * 48))
        self.assertEqual(store.MIN_BUILD_NUMBERS, {("android", "production"): 3})


FIRST_RELEASE = ("First release must be uploaded in Play Console: no track of this app has a rolled-out "
                 "release, and Play's API cannot make an app's first release. Upload the first build by hand in "
                 "Play Console (a sign_only run can produce it, signed in CI), then dispatch the next build here.")


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
