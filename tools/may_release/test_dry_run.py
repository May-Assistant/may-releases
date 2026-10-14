import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dry_run
import ios_upload
import play_upload
import static_check
from store import SECRET_NAMES, StoreError
from test_play_upload import Session
from test_sign import fixture_ipa
import verify

HERE = Path(__file__).resolve().parent
STORE = static_check.ROOT / ".github/workflows/store-release-draft.yml"
SHA = "a" * 40
# MAY_DRY_RUN values that must not get past either upload script's guard: only the exact "false" may.
NOT_FALSE = (None, "true", "True", "False", "FALSE", " false", "false\n", "", "0", "no")


def runs(condition, dry_run_input):
    """Whether a step runs, for the only `if:` forms static_check allows in store jobs.

    `dry_run_input` is github.event.inputs.dry_run: the string 'true' or 'false',
    or None if absent. Expression comparison of strings ignores case. The event is
    always workflow_dispatch, so a condition requiring the inert event never holds.
    """
    if condition in (None, "always()"):
        return True
    value = (dry_run_input or "").lower() if dry_run_input is not None else None
    if condition == static_check.UPLOAD_IF:  # stage 1: held inert
        return False
    if condition == static_check.DRY_RUN_UPLOAD_IF:  # what stage 2 leaves on the Android upload
        return value == "false"
    if condition == static_check.REPORT_IF:
        return value != "false"
    raise AssertionError("unmodelled condition: " + condition)


def steps_that_run(job, dry_run_input):
    _, steps = static_check.job_steps(STORE.read_text(), job)
    return [name or text.split("\n")[0] for name, text in steps if runs(static_check.step_condition(text), dry_run_input)]


class DryRunWorkflowTests(unittest.TestCase):
    def test_dry_run_defaults_to_true(self):
        dispatch = STORE.read_text().partition("\njobs:\n")[0]
        block = re.search(r"(?m)^      dry_run:\n((?:        .+\n)+)", dispatch)
        self.assertIsNotNone(block)
        fields = dict(line.strip().split(": ", 1) for line in block.group(1).splitlines())
        self.assertEqual((fields["type"], fields["default"], fields["required"]), ("boolean", "true", "true"))

    def test_dry_run_skips_every_upload_but_still_signs_and_reports(self):
        for job, upload, sign in (("android-release", "Upload update to the internal track only",
                                   "Sign and verify Android App Bundle"),
                                  ("ios-release", "Upload verified IPA for internal TestFlight",
                                   "Sign and verify internal-TestFlight IPA")):
            for value in ("true", "TRUE", None, ""):  # default, any casing, or no value at all
                with self.subTest(job=job, value=value):
                    ran = steps_that_run(job, value)
                    self.assertNotIn(upload, ran)
                    self.assertIn(sign, ran)
                    self.assertIn("Decrypt and check the build handoff", ran)
                    self.assertIn(static_check.REPORT, ran)
            with self.subTest(job=job, value="false"):
                # Stage 1: admission refuses dry_run: false before these jobs start
                # (gate.UPLOADS_HELD, test_gate). Even past it, the held upload never runs.
                ran = steps_that_run(job, "false")
                self.assertNotIn(upload, ran)
                self.assertIn(sign, ran)
                self.assertNotIn(static_check.REPORT, ran)
        # Once only the guard is removed (stage 2), the dry-run condition alone decides.
        for value, uploads in (("false", True), ("FALSE", True), ("true", False), (None, False), ("", False)):
            with self.subTest(stage=2, value=value):
                self.assertEqual(runs(static_check.UPLOAD_IF.replace(static_check.INERT + " && ", ""), value), uploads)
        # No store upload step anywhere else; build jobs are unaffected by the input.
        for job in ("android-build", "ios-build"):
            self.assertEqual(steps_that_run(job, "true"), steps_that_run(job, "false"))
        text = STORE.read_text()
        self.assertEqual(text.count("tools/may_release/play_upload.py"), 1)
        self.assertEqual(text.count("tools/may_release/ios_upload.py"), 1)


class DryRunReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "may-release").mkdir()
        self.env = patch.dict(os.environ, {"RUNNER_TEMP": self.directory.name}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_android_report_names_package_version_track_and_signed_sha256(self):
        bundle = self.root / "may-release/signed.aab"
        bundle.write_bytes(b"signed fixture bundle")
        lines = dry_run.summary("android", "playground", SHA, "2")
        self.assertEqual(lines, [
            "Dry run: no store upload was made. Google Play would have received:",
            "  package: io.github.mayassistant.may.playground",
            "  versionCode: 2",
            "  track: internal",
            "  signed AAB SHA-256: " + hashlib.sha256(b"signed fixture bundle").hexdigest()])
        self.assertEqual(play_upload.TRACK, "internal")  # the track the real upload writes

    def test_ios_report_reverifies_the_ipa_and_names_bundle_build_and_sha256(self):
        ipa = self.root / "may-release/May.ipa"
        fixture_ipa(ipa, build="9")
        lines = dry_run.summary("ios", "playground", SHA, "9")
        self.assertEqual(lines[1:4], ["  bundle: io.github.mayassistant.may.playground",
                                      "  build number (CFBundleVersion): 9",
                                      "  track: TestFlight internal testing only"])
        self.assertEqual(lines[4], "  signed IPA SHA-256: " + hashlib.sha256(ipa.read_bytes()).hexdigest())
        with self.assertRaises(verify.VerifyError):
            dry_run.summary("ios", "playground", SHA, "10")  # the IPA carries build 9

    def test_report_refuses_missing_artifact_and_any_secret(self):
        with self.assertRaises(StoreError):
            dry_run.summary("android", "playground", SHA, "2")
        (self.root / "may-release/signed.aab").write_bytes(b"x")
        for name in SECRET_NAMES:
            with self.subTest(name=name), patch.dict(os.environ, {name: "present"}), self.assertRaises(StoreError):
                dry_run.summary("android", "playground", SHA, "2")

    def test_report_cli_runs_isolated_and_prints_the_summary(self):
        (self.root / "may-release/signed.aab").write_bytes(b"signed fixture bundle")
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(HERE / "dry_run.py"), "--platform", "android",
                                 "--target", "production", "--source-sha", SHA, "--build-number", "3"],
                                capture_output=True, text=True, cwd=self.directory.name,
                                env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": self.directory.name})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  package: io.github.mayassistant.may\n", result.stdout)
        self.assertIn("  versionCode: 3\n", result.stdout)


class UploadScriptsRefuseADryRunTests(unittest.TestCase):
    """Even if the workflow's `if:` were lost, the upload scripts refuse unless dry_run is exactly false."""

    def test_play_upload_refuses_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "may-release").mkdir()
            (Path(directory) / "may-release/signed.aab").write_bytes(b"x")
            for value in NOT_FALSE:
                env = {"RUNNER_TEMP": directory}
                if value is not None:
                    env["MAY_DRY_RUN"] = value
                session = Session()
                with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(play_upload.PlayRefusal) as refused:
                        play_upload.upload("playground", SHA, "9", session)
                    self.assertEqual(refused.exception.args[0], "Dry run: no store upload was requested.")
                    self.assertEqual(session.calls, [])

    def test_ios_upload_refuses_before_the_upload_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"RUNNER_TEMP": directory, "PATH": "/usr/bin", "MAY_ASC_KEY_ID": "23VX669FGY",
                   "MAY_ASC_KEY_BASE64": base64.b64encode(b"-----BEGIN PRIVATE KEY-----\n").decode(),
                   "MAY_ASC_ISSUER_ID": "00000000-1111-2222-3333-444444444444"}
            for value in NOT_FALSE + ("false",):
                with self.subTest(value=value), \
                        patch.dict(os.environ, dict(env, **({"MAY_DRY_RUN": value} if value is not None else {})),
                                   clear=True), \
                        patch("ios_upload.run_private") as runner:
                    with self.assertRaises(StoreError) as refused:
                        ios_upload.upload("playground", SHA, "9")
                    runner.assert_not_called()
                    if value == "false":  # past the guard; stops later, at the missing IPA
                        self.assertEqual(refused.exception.args[0], "Signed iOS IPA is unavailable")
                    else:
                        self.assertEqual(refused.exception.args[0], "Dry run: no store upload was requested")


if __name__ == "__main__":
    unittest.main()
