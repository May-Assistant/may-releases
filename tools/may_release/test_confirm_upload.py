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
from store import SECRET_NAMES, UPLOAD_RECORD, StoreError, write_upload_record
from test_play_upload import ACCOUNT, BUNDLE, CREDENTIAL, Session

HERE = Path(__file__).resolve().parent
SHA = "a" * 40
PLAYGROUND = "io.github.mayassistant.may.playground"
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

    def upload(self):
        """A real play_upload.upload() against the fake Play session; it writes the record."""
        with patch.dict(os.environ, {"MAY_PLAY_JSON": CREDENTIAL, "MAY_PLAY_ACCOUNT": ACCOUNT,
                                     "MAY_DRY_RUN": "false"}), patch("sys.stdout", new_callable=io.StringIO):
            play_upload.upload("playground", SHA, "9", Session())

    def confirm(self, outcome="success", platform="android", build_number="9"):
        env = {} if outcome is None else {"MAY_UPLOAD_OUTCOME": outcome}
        with patch.dict(os.environ, env):
            return confirm_upload.confirm(platform, "playground", SHA, build_number)

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


if __name__ == "__main__":
    unittest.main()
