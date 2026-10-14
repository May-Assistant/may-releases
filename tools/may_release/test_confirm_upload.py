import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import confirm_upload
import play_upload
import sign_only
from store import SECRET_NAMES, SIGN_ONLY_DIR, SIGN_ONLY_RECORD, UPLOAD_RECORD, StoreError, write_upload_record
from test_play_upload import (ACCOUNT, BUNDLE, CREDENTIAL, FIXTURE_CERT, OTHER_CERT, PRODUCTION_ACCOUNT, Session,
                              credential, upload_keys)

HERE = Path(__file__).resolve().parent
SHA = "a" * 40
PLAYGROUND = "io.github.mayassistant.may.playground"
PRODUCTION = "io.github.mayassistant.may"
NOT_SUCCESS = (None, "", "skipped", "failure", "cancelled", "Success", "success ")


class ConfirmUploadTests(unittest.TestCase):
    """confirm_upload.py: with dry_run false, the job fails unless the upload step succeeded."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "may-release").mkdir()
        (self.root / "may-release/signed.aab").write_bytes(BUNDLE)
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": self.directory.name}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def upload(self, target="playground"):
        """A real play_upload.upload() against the fake Play session; it writes the record."""
        account = ACCOUNT if target == "playground" else PRODUCTION_ACCOUNT
        keys = upload_keys() if target == "playground" else upload_keys(playground=OTHER_CERT, production=FIXTURE_CERT)
        with patch.dict(os.environ, {"MAY_PLAY_JSON": credential(account), "MAY_PLAY_ACCOUNT": account,
                                     "MAY_DRY_RUN": "false"}), patch("sys.stdout", new_callable=io.StringIO), \
                patch("store.ANDROID_UPLOAD_KEYS", keys):
            play_upload.upload(target, SHA, "9", Session())

    def confirm(self, outcome="success", platform="android", build_number="9", target="playground"):
        env = {} if outcome is None else {"MAY_UPLOAD_OUTCOME": outcome}
        with patch.dict(os.environ, env):
            return confirm_upload.confirm(platform, target, SHA, build_number)

    def test_a_successful_upload_is_confirmed_with_its_report(self):
        self.upload()
        self.assertTrue((self.root / "may-release" / UPLOAD_RECORD).is_file(), "the upload left no record")
        self.assertEqual(self.confirm(), [
            "Store upload made: Google Play accepted and committed:",
            "  package: " + PLAYGROUND,
            "  versionCode: 9",
            "  track: internal",
            "  signed AAB SHA-256: " + hashlib.sha256(BUNDLE).hexdigest(),
            "  Play edit (committed): edit-1"])

    def test_may_production_is_confirmed_exactly_as_playground_is(self):
        self.upload("production")
        self.assertEqual(self.confirm(target="production"), [
            "Store upload made: Google Play accepted and committed:",
            "  package: " + PRODUCTION,
            "  versionCode: 9",
            "  track: internal",
            "  signed AAB SHA-256: " + hashlib.sha256(BUNDLE).hexdigest(),
            "  Play edit (committed): edit-1"])
        # The record is May's: it confirms nothing for May Playground, and the checks are the same.
        with self.assertRaises(StoreError):
            self.confirm(target="playground")
        good = json.loads((self.root / "may-release" / UPLOAD_RECORD).read_text())
        for change in ({"build_number": "10"}, {"track": "production"}, {"sha256": "0" * 64},
                       {"app_id": PLAYGROUND}, {"store_id": None}, {"store_id": "edit 1"}):
            with self.subTest(change=change):
                (self.root / "may-release" / UPLOAD_RECORD).write_text(json.dumps(dict(good, **change)))
                with self.assertRaises(StoreError):
                    self.confirm(target="production")
        for outcome in NOT_SUCCESS:
            (self.root / "may-release" / UPLOAD_RECORD).write_text(json.dumps(good))
            with self.subTest(outcome=outcome), self.assertRaises(StoreError):
                self.confirm(outcome, target="production")

    def test_a_run_that_published_a_sign_only_bundle_confirms_no_upload(self):
        self.upload()
        (self.root / SIGN_ONLY_DIR).mkdir()
        with self.assertRaises(StoreError) as refused:
            self.confirm()
        self.assertEqual(refused.exception.args[0], "This run published a sign-only bundle, which is never an "
                                                    "upload; nothing was confirmed uploaded")

    def test_an_upload_step_that_did_not_succeed_fails_the_job(self):
        self.upload()  # even with a valid record present, the outcome decides first
        for outcome in NOT_SUCCESS:
            with self.subTest(outcome=outcome):
                with self.assertRaises(StoreError) as refused:
                    self.confirm(outcome)
                self.assertEqual(refused.exception.args[0],
                                 "dry_run was false, but the store upload step did not succeed "
                                 "(it failed, was skipped or was cancelled); nothing was confirmed uploaded")

    def test_success_without_a_matching_record_fails_the_job(self):
        with self.assertRaises(StoreError):
            self.confirm()  # no record at all
        good = {"platform": "android", "app_id": PLAYGROUND, "build_number": "9", "track": "internal",
                "sha256": hashlib.sha256(BUNDLE).hexdigest(), "store_id": "edit-1"}
        for change in ({"build_number": "10"}, {"track": "production"}, {"track": "alpha"},
                       {"sha256": "0" * 64}, {"app_id": "io.github.mayassistant.may"},
                       {"platform": "ios"}, {"store_id": None}, {"store_id": "edit 1"}, {"extra": "x"}):
            with self.subTest(change=change):
                path = self.root / "may-release" / UPLOAD_RECORD
                path.write_text(json.dumps(dict(good, **change)))
                with self.assertRaises(StoreError):
                    self.confirm()
        (self.root / "may-release" / UPLOAD_RECORD).write_text(json.dumps(good))
        self.assertEqual(self.confirm()[3], "  track: internal")
        (self.root / "may-release" / UPLOAD_RECORD).write_text("[]")
        with self.assertRaises(StoreError):
            self.confirm()

    def test_ios_record_has_no_store_id_and_its_own_track(self):
        ipa = self.root / "may-release/May.ipa"
        ipa.write_bytes(b"fixture ipa")
        write_upload_record("ios", PLAYGROUND, "9", "TestFlight internal testing only",
                            hashlib.sha256(b"fixture ipa").hexdigest())
        lines = self.confirm(platform="ios")
        self.assertEqual(lines[1:4], ["  bundle: " + PLAYGROUND, "  build number (CFBundleVersion): 9",
                                      "  track: TestFlight internal testing only"])
        for outcome in ("skipped", None):
            with self.subTest(outcome=outcome), self.assertRaises(StoreError):
                self.confirm(outcome, platform="ios")

    def test_holds_no_secret(self):
        self.upload()
        for name in SECRET_NAMES:
            with self.subTest(name=name), patch.dict(os.environ, {name: "present"}), self.assertRaises(StoreError):
                self.confirm()

    def test_cli_fails_loudly_on_a_skipped_upload_and_prints_the_report_on_success(self):
        argv = [sys.executable, "-I", "-S", "-B", str(HERE / "confirm_upload.py"), "--platform", "android",
                "--target", "playground", "--source-sha", SHA, "--build-number", "9"]
        base = {"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": self.directory.name}
        skipped = subprocess.run(argv, capture_output=True, text=True, cwd=self.directory.name,
                                 env=dict(base, MAY_UPLOAD_OUTCOME="skipped"))
        self.assertEqual(skipped.returncode, 1)
        self.assertEqual(skipped.stdout, "")
        self.assertEqual(skipped.stderr.strip(), "May upload check failed: dry_run was false, but the store upload "
                         "step did not succeed (it failed, was skipped or was cancelled); nothing was confirmed uploaded")
        self.upload()
        made = subprocess.run(argv, capture_output=True, text=True, cwd=self.directory.name,
                              env=dict(base, MAY_UPLOAD_OUTCOME="success"))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertTrue(made.stdout.startswith("Requested upload confirmed.\n"))
        self.assertIn("  track: internal\n", made.stdout)
        self.assertIn("  Play edit (committed): edit-1\n", made.stdout)


class ConfirmSignOnlyTests(unittest.TestCase):
    """confirm_upload.py --sign-only: signed, not uploaded, and only if the bundle was published."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "may-release").mkdir()
        (self.root / "may-release/signed.aab").write_bytes(BUNDLE)
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": self.directory.name}, clear=True)
        self.env.start()
        self.sha = hashlib.sha256(BUNDLE).hexdigest()
        out = self.root / SIGN_ONLY_DIR
        out.mkdir()
        (out / sign_only.SEALED).write_bytes(b"age-encrypted fixture")
        (out / sign_only.HASH_FILE).write_text(self.sha + "  signed.aab\n")
        self.record = {"mode": "sign-only", "platform": "android", "app_id": PRODUCTION, "build_number": "9",
                       "sha256": self.sha, "encrypted_sha256": hashlib.sha256(b"age-encrypted fixture").hexdigest()}
        self.write(self.record)

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def write(self, record):
        (self.root / SIGN_ONLY_DIR / SIGN_ONLY_RECORD).write_text(json.dumps(record))

    def confirm(self, outcome="success", target="production"):
        with patch.dict(os.environ, {} if outcome is None else {"MAY_PUBLISH_OUTCOME": outcome}):
            return confirm_upload.confirm_signed("android", target, SHA, "9")

    def test_a_published_bundle_is_confirmed_signed_not_uploaded(self):
        lines = self.confirm()
        self.assertEqual(lines[:4], [
            "Signed, not uploaded: nothing was sent to Google Play. Published for one manual Play Console upload:",
            "  package: " + PRODUCTION, "  versionCode: 9", "  signed AAB SHA-256: " + self.sha])
        self.assertFalse(any("upload made" in line or "Play edit" in line or "track:" in line for line in lines))

    def test_a_publish_step_that_did_not_succeed_fails_the_job(self):
        for outcome in NOT_SUCCESS:
            with self.subTest(outcome=outcome), self.assertRaises(StoreError) as refused:
                self.confirm(outcome)
            self.assertEqual(refused.exception.args[0], "sign_only was true, but the signed bundle was not published "
                             "(the publish step failed, was skipped or was cancelled); nothing is ready for Play Console")

    def test_a_sign_only_run_that_uploaded_fails(self):
        (self.root / "may-release" / UPLOAD_RECORD).write_text("{}")
        with self.assertRaises(StoreError) as refused:
            self.confirm()
        self.assertEqual(refused.exception.args[0], "A sign-only run left an upload record; it must never upload")

    def test_the_record_must_match_the_signed_bundle_and_the_published_file(self):
        for change in ({"sha256": "0" * 64}, {"encrypted_sha256": "0" * 64}, {"build_number": "10"},
                       {"app_id": PLAYGROUND}, {"mode": "upload"}, {"platform": "ios"}, {"extra": "x"}):
            with self.subTest(change=change):
                self.write(dict(self.record, **change))
                with self.assertRaises(StoreError) as refused:
                    self.confirm()
                self.assertEqual(refused.exception.args[0],
                                 "The sign-only record does not match the signed bundle and the published file")
        self.write(self.record)
        (self.root / "may-release/signed.aab").write_bytes(BUNDLE + b"changed")
        with self.assertRaises(StoreError):
            self.confirm()
        (self.root / "may-release/signed.aab").write_bytes(BUNDLE)
        (self.root / SIGN_ONLY_DIR / sign_only.HASH_FILE).write_text("0" * 64 + "  signed.aab\n")
        with self.assertRaises(StoreError) as refused:
            self.confirm()
        self.assertEqual(refused.exception.args[0],
                         "The sign-only record does not match the signed bundle and the published file")
        (self.root / SIGN_ONLY_DIR / sign_only.HASH_FILE).write_text(self.sha + "  signed.aab\n")
        self.write(["not", "a", "record"])
        with self.assertRaises(StoreError):
            self.confirm()

    def test_the_published_folder_holds_exactly_its_three_files(self):
        (self.root / SIGN_ONLY_DIR / "upload.keystore").write_bytes(b"key")
        with self.assertRaises(StoreError) as refused:
            self.confirm()
        self.assertEqual(refused.exception.args[0], "The published sign-only folder holds something other than its three files")
        (self.root / SIGN_ONLY_DIR / "upload.keystore").unlink()
        (self.root / SIGN_ONLY_DIR / sign_only.HASH_FILE).unlink()
        with self.assertRaises(StoreError):
            self.confirm()

    def test_only_the_android_production_record_is_confirmed(self):
        with self.assertRaises(StoreError):
            self.confirm(target="playground")
        with patch.dict(os.environ, {"MAY_PUBLISH_OUTCOME": "success"}), self.assertRaises(StoreError):
            confirm_upload.confirm_signed("ios", "production", SHA, "9")

    def test_holds_no_secret(self):
        for name in SECRET_NAMES:
            with self.subTest(name=name), patch.dict(os.environ, {name: "present"}), self.assertRaises(StoreError):
                self.confirm()

    def test_cli_says_signed_not_uploaded(self):
        argv = [sys.executable, "-I", "-S", "-B", str(HERE / "confirm_upload.py"), "--platform", "android",
                "--target", "production", "--source-sha", SHA, "--build-number", "9", "--sign-only"]
        base = {"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": self.directory.name}
        skipped = subprocess.run(argv, capture_output=True, text=True, cwd=self.directory.name,
                                 env=dict(base, MAY_PUBLISH_OUTCOME="skipped"))
        self.assertEqual(skipped.returncode, 1)
        self.assertTrue(skipped.stderr.startswith("May sign-only check failed: sign_only was true"))
        made = subprocess.run(argv, capture_output=True, text=True, cwd=self.directory.name,
                              env=dict(base, MAY_PUBLISH_OUTCOME="success"))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertTrue(made.stdout.startswith("Signed, not uploaded: confirmed.\n"))
        self.assertNotIn("Requested upload confirmed", made.stdout)


if __name__ == "__main__":
    unittest.main()
