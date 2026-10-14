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
import signing_fixtures as fx
from store import SECRET_NAMES, StoreError
from test_play_upload import (FIXTURE_CERT, OTHER_CERT, PLAYGROUND_ACCOUNT, PRODUCTION_ACCOUNT, Session,
                              refusal, upload_keys)
from test_sign import fixture_ipa
import verify

HERE = Path(__file__).resolve().parent
STORE = static_check.ROOT / ".github/workflows/store-release-draft.yml"
SHA = "a" * 40
# MAY_DRY_RUN values that must not get past either upload script's guard: only the exact "false" may.
NOT_FALSE = (None, "true", "True", "False", "FALSE", " false", "false\n", "", "0", "no")


def runs(condition, dry_run_input, failed=False, sign_only_input=None):
    """Whether a step runs, for the only `if:` forms static_check allows in store jobs.

    `dry_run_input` is github.event.inputs.dry_run: the string 'true' or 'false',
    or None if absent. Expression comparison of strings ignores case. The event is
    always workflow_dispatch, so a condition requiring the inert event never holds.
    `failed` is whether an earlier step of the job failed: a condition without a
    status function is implicitly `success() && ...`, so it then does not run.
    `sign_only_input` is github.event.inputs.sign_only, modelled the same way.
    """
    value = (dry_run_input or "").lower() if dry_run_input is not None else None
    signing = (sign_only_input or "").lower() if sign_only_input is not None else None
    if condition == "always()":
        return True
    if condition == static_check.CONFIRM_IF:  # !cancelled() && dry_run == 'false'
        return value == "false"
    if condition == static_check.CONFIRM_SIGNED_IF:  # !cancelled(), in the sign-only job
        return True
    if failed:
        return False
    if condition is None:
        return True
    if condition == static_check.HELD_UPLOAD_IF:  # held inert
        return False
    if condition == static_check.DRY_RUN_UPLOAD_IF:  # an active upload
        return value == "false"
    if condition == static_check.REPORT_IF:
        return value != "false"
    raise AssertionError("unmodelled condition: " + condition)


def job_runs(job, platform, sign_only_input):
    """Whether an Android or admission job runs, for the job-level `if:` forms STORE_JOB_IF pins."""
    condition = static_check.STORE_JOB_IF[job]
    signing = (sign_only_input or "").lower() if sign_only_input is not None else None
    if condition is None:
        return True
    if condition == "${{ inputs.platform == 'android' }}":
        return platform == "android"
    if condition == "${{ inputs.platform == 'android' && github.event.inputs.sign_only != 'true' }}":
        return platform == "android" and signing != "true"
    if condition == "${{ inputs.platform == 'android' && github.event.inputs.sign_only == 'true' }}":
        return platform == "android" and signing == "true"
    if static_check.INERT in condition:
        return False
    raise AssertionError("unmodelled job condition: " + condition)


def run_job(job, dry_run_input, upload_outcome="success", workflow=None, sign_only_input="false",
            publish_outcome="success"):
    """(job result, {step: outcome}) for a release job whose other steps all succeed.

    `upload_outcome` is what the upload step ends with when it runs (e.g. a
    failure). The loud-failure steps are modelled as confirm_upload.py behaves
    (ConfirmUploadTests, ConfirmSignOnlyTests): each fails unless the outcome it
    reads is success. `publish_outcome` is what the sign-only publish step ends with.
    """
    _, steps = static_check.job_steps(workflow or STORE.read_text(), job)
    outcomes, by_id, failed = {}, {}, False
    for name, text in steps:
        if not runs(static_check.step_condition(text), dry_run_input, failed, sign_only_input):
            outcome = "skipped"
        elif name == static_check.UPLOADS.get(job):
            outcome = upload_outcome
        elif name == static_check.PUBLISH_SIGNED:
            outcome = publish_outcome
        elif name in (static_check.CONFIRM, static_check.CONFIRM_SIGNED):
            # steps.<id>.outcome: an id no earlier step carries reads as empty.
            env = dict(static_check.env_entries(text))
            read = env.get("MAY_UPLOAD_OUTCOME" if name == static_check.CONFIRM else "MAY_PUBLISH_OUTCOME", "")
            match = re.fullmatch(r"\$\{\{ steps\.(\w+)\.outcome \}\}", read)
            outcome = "success" if match and by_id.get(match.group(1)) == "success" else "failure"
        else:
            outcome = "success"
        failed |= outcome == "failure"
        outcomes[name or text.split("\n")[0]] = outcome
        step_id = re.search(r"(?m)^(?:        )?id: (\w+)$", text)
        if step_id:
            by_id[step_id.group(1)] = outcome
    return ("failure" if failed else "success"), outcomes


def steps_that_run(job, dry_run_input):
    return [name for name, outcome in run_job(job, dry_run_input)[1].items() if outcome != "skipped"]


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
                    result, outcomes = run_job(job, value)
                    ran = steps_that_run(job, value)
                    self.assertEqual(result, "success")
                    self.assertNotIn(upload, ran)
                    self.assertNotIn(static_check.CONFIRM, ran)
                    self.assertIn(sign, ran)
                    self.assertIn("Decrypt and check the build handoff", ran)
                    self.assertIn(static_check.REPORT, ran)
        # A plain dry run (or upload) runs android-release, never the sign-only job.
        for value in ("false", "FALSE", None, ""):
            with self.subTest(sign_only=value):
                self.assertTrue(job_runs("android-release", "android", value))
                self.assertFalse(job_runs(static_check.SIGN_ONLY_JOB, "android", value))
        # No store upload step anywhere else; build jobs are unaffected by the input.
        for job in ("android-build", "ios-build"):
            self.assertEqual(steps_that_run(job, "true"), steps_that_run(job, "false"))
        text = STORE.read_text()
        self.assertEqual(text.count("tools/may_release/play_upload.py"), 1)
        self.assertEqual(text.count("tools/may_release/ios_upload.py"), 1)

    def test_sign_only_runs_its_own_job_which_signs_and_publishes_but_never_uploads(self):
        for value in ("true", "TRUE"):
            with self.subTest(value=value):
                # android-release (which references MAY_PLAY_JSON) does not run at all.
                self.assertFalse(job_runs("android-release", "android", value))
                self.assertTrue(job_runs(static_check.SIGN_ONLY_JOB, "android", value))
                result, outcomes = run_job(static_check.SIGN_ONLY_JOB, "true", sign_only_input=value)
                self.assertEqual(result, "success")
                for step in ("Decrypt and check the build handoff", "Sign and verify Android App Bundle",
                             static_check.SEAL_SIGNED, static_check.PUBLISH_SIGNED, static_check.CONFIRM_SIGNED):
                    self.assertEqual(outcomes.get(step), "success")
                self.assertFalse({"Upload update to the internal track only", static_check.CONFIRM} & set(outcomes))
        # No iOS or build job runs a sign-only step, and nothing in the sign-only job names a Play secret.
        for job in ("ios-release", "android-build", "android-release"):
            self.assertFalse(set(run_job(job, "true", sign_only_input="true")[1]) &
                             {static_check.SEAL_SIGNED, static_check.PUBLISH_SIGNED, static_check.CONFIRM_SIGNED})
        self.assertNotIn("MAY_PLAY", static_check.code_lines(
            static_check.job_text(STORE.read_text(), static_check.SIGN_ONLY_JOB)))

    def test_sign_only_never_ends_green_without_publishing(self):
        for publish in ("failure", "skipped"):
            with self.subTest(publish=publish):
                result, outcomes = run_job(static_check.SIGN_ONLY_JOB, "true", sign_only_input="true",
                                           publish_outcome=publish)
                self.assertEqual(result, "failure")
                self.assertEqual(outcomes.get(static_check.CONFIRM_SIGNED), "failure")
        # After an earlier failure the check still runs (unless the run was cancelled), and fails the job.
        _, steps = static_check.job_steps(STORE.read_text(), static_check.SIGN_ONLY_JOB)
        self.assertEqual(static_check.step_condition(dict(steps).get(static_check.CONFIRM_SIGNED, "")),
                         static_check.CONFIRM_SIGNED_IF)
        self.assertTrue(runs(static_check.CONFIRM_SIGNED_IF, "true", failed=True, sign_only_input="true"))

    def test_stage_3_uploads_android_only_when_dry_run_is_false(self):
        for value in ("false", "FALSE"):
            with self.subTest(value=value):
                result, outcomes = run_job("android-release", value)
                self.assertEqual(result, "success")
                self.assertEqual(outcomes["Upload update to the internal track only"], "success")
                self.assertEqual(outcomes[static_check.CONFIRM], "success")
                self.assertEqual(outcomes[static_check.REPORT], "skipped")
        # The iOS upload is still held: even inside its (inert) job, it never runs.
        self.assertEqual(run_job("ios-release", "false")[1]["Upload verified IPA for internal TestFlight"], "skipped")

    def test_dry_run_false_never_ends_green_without_uploading(self):
        # Android: the upload ran and failed, or an earlier step failed so it was skipped.
        for upload_outcome in ("failure", "skipped"):
            with self.subTest(job="android-release", upload_outcome=upload_outcome):
                result, outcomes = run_job("android-release", "false", upload_outcome)
                self.assertEqual(result, "failure")
                self.assertEqual(outcomes.get(static_check.CONFIRM), "failure")
        # iOS: the upload is still held (skipped), so if the job ever ran with
        # dry_run: false, the loud-failure step fails it rather than going green.
        result, outcomes = run_job("ios-release", "false")
        self.assertEqual(result, "failure")
        self.assertEqual(outcomes.get("Upload verified IPA for internal TestFlight"), "skipped")
        self.assertEqual(outcomes.get(static_check.CONFIRM), "failure")
        # Android with its guard put back: the skipped upload must not go green either.
        held = STORE.read_text().replace("        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n",
                                         "        if: " + static_check.HELD_UPLOAD_IF + "\n", 1)
        result, outcomes = run_job("android-release", "false", workflow=held)
        self.assertEqual(result, "failure")
        self.assertEqual(outcomes.get("Upload update to the internal track only"), "skipped")


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

    def summary(self, target="playground", build="2", account=PLAYGROUND_ACCOUNT, keys=None):
        with patch.dict(os.environ, MAY_PLAY_ACCOUNT=account), \
                patch("store.ANDROID_UPLOAD_KEYS", keys or upload_keys()):
            return dry_run.summary("android", target, SHA, build)

    def test_android_report_names_package_version_track_and_signed_sha256(self):
        bundle = self.root / "may-release/signed.aab"
        bundle.write_bytes(fx.signed_bundle("io.github.mayassistant.may.playground", "2"))
        lines = self.summary()
        self.assertEqual(lines, [
            "Dry run: no store upload was made. Google Play would have received:",
            "  package: io.github.mayassistant.may.playground",
            "  versionCode: 2",
            "  track: internal",
            "  signed AAB SHA-256: " + hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "  signer: this target's pinned upload certificate",
            "  Play account (MAY_PLAY_ACCOUNT): " + PLAYGROUND_ACCOUNT])
        self.assertEqual(play_upload.TRACK, "internal")  # the track the real upload writes

    def test_may_production_dry_run_checks_everything_but_the_play_client(self):
        (self.root / "may-release/signed.aab").write_bytes(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
        mine = upload_keys(playground=OTHER_CERT, production=FIXTURE_CERT)
        lines = self.summary("production", "3", PRODUCTION_ACCOUNT, mine)
        self.assertEqual(lines[1:3], ["  package: io.github.mayassistant.may", "  versionCode: 3"])
        self.assertEqual(lines[-1], "  Play account (MAY_PLAY_ACCOUNT): " + PRODUCTION_ACCOUNT)
        cases = (
            (("production", "2", PRODUCTION_ACCOUNT, mine), "Build number is below this app's floor (May production starts at 3)"),
            (("production", "3", PLAYGROUND_ACCOUNT, mine), "MAY_PLAY_ACCOUNT is not this target's Play service account"),
            (("production", "3", PRODUCTION_ACCOUNT, upload_keys()),  # the fixture key is Playground's
             "Signed Android App Bundle is not signed with this target's upload key"),
            (("playground", "3", PLAYGROUND_ACCOUNT, mine),  # the fixture key is May production's
             "Signed Android App Bundle is not signed with this target's upload key"),
            (("playground", "3", PRODUCTION_ACCOUNT, upload_keys()), "MAY_PLAY_ACCOUNT is not this target's Play service account"),
        )
        for args, message in cases:
            with self.subTest(args=args[:3]):
                self.assertEqual(refusal(self, StoreError, self.summary, *args).args[0], message)
        # The package and versionCode it reports are read from the signed bundle itself.
        for manifest, build, message in ((fx.MANIFEST_PLAYGROUND_VC9, "9", "Bundle package is not this target's app"),
                                         (fx.MANIFEST_PRODUCTION_VC3, "4",
                                          "Bundle versionCode is not the requested build number")):
            (self.root / "may-release/signed.aab").write_bytes(fx.with_manifest(manifest))
            with self.subTest(build=build):
                self.assertEqual(refusal(self, verify.VerifyError, self.summary, "production", build,
                                         PRODUCTION_ACCOUNT, mine).args[0], message)
        (self.root / "may-release/signed.aab").write_bytes(b"not a signed bundle")
        with self.assertRaises(verify.VerifyError):
            self.summary("production", "3", PRODUCTION_ACCOUNT, mine)

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
            self.summary()
        (self.root / "may-release/signed.aab").write_bytes(fx.signed_bundle("io.github.mayassistant.may.playground", "2"))
        for name in SECRET_NAMES:
            with self.subTest(name=name), patch.dict(os.environ, {name: "present"}), self.assertRaises(StoreError):
                self.summary()

    def test_report_cli_runs_isolated_and_refuses_another_targets_key(self):
        # The committed pins: the fixture bundle is signed by neither app's key, so the report refuses it.
        (self.root / "may-release/signed.aab").write_bytes(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(HERE / "dry_run.py"), "--platform", "android",
                                 "--target", "production", "--source-sha", SHA, "--build-number", "3"],
                                capture_output=True, text=True, cwd=self.directory.name,
                                env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": self.directory.name,
                                     "MAY_PLAY_ACCOUNT": PRODUCTION_ACCOUNT})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "May dry-run report refused: Signed Android App Bundle is not signed "
                                                "with this target's upload key")


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
