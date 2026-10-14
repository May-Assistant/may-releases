import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import confirm_upload
import sign_only
import signing_fixtures as fx
import verify
from store import SECRET_NAMES, SIGN_ONLY_DIR, SIGN_ONLY_RECORD, SIGN_ONLY_TARGETS, StoreError
from test_play_upload import FIXTURE_CERT, OTHER_CERT, upload_keys

HERE = Path(__file__).resolve().parent
SHA = "a" * 40
PRODUCTION = "io.github.mayassistant.may"
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))


def system_tool(name, relative):
    return shutil.which(Path(relative).name)


# Skipped only off CI; in CI a missing age makes these fail rather than vanish.
@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class SignOnlyTests(unittest.TestCase):
    """sign_only.py with the real age: May production's signed bundle, encrypted to Samuel's key."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        keys = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        self.secret = next(l for l in keys.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run(["age-keygen", "-y"], input=self.secret, capture_output=True,
                                     text=True, check=True).stdout.strip()
        (self.root / "may-release").mkdir()
        # The fixture signer's bundle with a real aapt2 manifest: May production, versionCode 3.
        self.bundle = fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3)
        (self.root / "may-release/signed.aab").write_bytes(self.bundle)
        self.patches = [
            patch.dict(os.environ, {"RUNNER_TEMP": self.directory.name, "PATH": os.environ["PATH"],
                                    "MAY_SIGN_ONLY": "true", "MAY_DRY_RUN": "true"}, clear=True),
            patch("sign_only.toolchain.tool_path", side_effect=system_tool),
            patch("sign_only.artifact_recipient", side_effect=self.recipients),
            # The fixture bundle's signer is May production's upload key in these tests.
            patch("store.ANDROID_UPLOAD_KEYS", upload_keys(playground=OTHER_CERT, production=FIXTURE_CERT)),
        ]
        for item in self.patches:
            item.start()

    def recipients(self, environment):
        """The committed keys: Samuel's Console-upload key, and distinct environment handoff keys."""
        return self.public if environment == "may-android-console-upload" else "age1" + "q" * 57 + str(len(environment) % 8)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def seal(self, target="production", build="3"):
        return sign_only.seal_signed(target, SHA, build)

    def decrypt(self):
        key = self.root / "samuel.key"
        key.write_text(self.secret + "\n")
        out = self.root / "decrypted.aab"
        subprocess.run(["age", "-d", "-i", str(key), "-o", str(out), str(self.root / SIGN_ONLY_DIR / sign_only.SEALED)],
                       check=True, capture_output=True)
        return out.read_bytes()

    def test_publishes_exactly_the_encrypted_signed_bundle_its_hash_and_record(self):
        lines = self.seal()
        out = self.root / SIGN_ONLY_DIR
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(sign_only.FILES))
        sha = hashlib.sha256(self.bundle).hexdigest()
        sealed = (out / sign_only.SEALED).read_bytes()
        self.assertNotIn(self.bundle[:200], sealed)
        self.assertTrue(sealed.startswith(b"age-encryption.org/v1\n"))
        # Samuel's key opens it to exactly the signed bundle, which the hash file and record name.
        self.assertEqual(self.decrypt(), self.bundle)
        self.assertEqual((out / sign_only.HASH_FILE).read_text(), sha + "  signed.aab\n")
        self.assertEqual(json.loads((out / SIGN_ONLY_RECORD).read_text()), {
            "mode": "sign-only", "platform": "android", "app_id": PRODUCTION, "build_number": "3",
            "sha256": sha, "encrypted_sha256": hashlib.sha256(sealed).hexdigest()})
        # The throwaway check identity and its plaintext are gone; nothing else was written.
        self.assertEqual(sorted(p.name for p in (self.root / "may-private").iterdir()), [])
        self.assertEqual(lines[:4], [
            "Signed, not uploaded: nothing was sent to Google Play. Published for one manual Play Console upload:",
            "  package: " + PRODUCTION, "  versionCode: 3", "  signed AAB SHA-256: " + sha])
        # The loud-failure check then confirms it, as signed and not uploaded.
        with patch.dict(os.environ, {"MAY_PUBLISH_OUTCOME": "success"}):
            self.assertEqual(confirm_upload.confirm_signed("android", "production", SHA, "3"), lines)

    def test_only_two_recipients_and_the_check_key_is_deleted(self):
        self.seal()
        header = (self.root / SIGN_ONLY_DIR / sign_only.SEALED).read_bytes().split(b"\n---", 1)[0]
        self.assertEqual(header.count(b"\n-> X25519 "), 2)  # Samuel's key and the deleted check key
        self.assertFalse((self.root / "may-private/sign-only-check.key").exists())

    def assertRefused(self, message, **kwargs):
        """The seal refuses with exactly `message` and publishes nothing (any other outcome fails the test)."""
        try:
            self.seal(**kwargs)
            raised = None
        except Exception as error:  # an assertion, not an error, whatever a broken seal does
            raised = error
        self.assertIsInstance(raised, StoreError)
        self.assertEqual(raised.args[0], message)
        self.assertFalse((self.root / SIGN_ONLY_DIR).exists())

    def test_refuses_unless_sign_only_is_exactly_true(self):
        for value in (None, "", "false", "True", "TRUE", " true", "true\n", "1"):
            with self.subTest(value=value), patch.dict(os.environ, {"MAY_SIGN_ONLY": value or ""}):
                if value is None:
                    os.environ.pop("MAY_SIGN_ONLY")
                self.assertRefused("Sign-only was not requested; nothing was published")

    def test_refuses_unless_dry_run_is_exactly_true(self):
        for value in ("false", "", "True", "true "):
            with self.subTest(value=value), patch.dict(os.environ, {"MAY_DRY_RUN": value}):
                self.assertRefused("Sign-only runs only with dry_run: true; nothing was published")

    def test_refuses_any_target_but_may_production_android(self):
        self.assertEqual(SIGN_ONLY_TARGETS, frozenset({("android", "production")}))
        self.assertRefused("Sign-only is only for the May production Android app; nothing was published",
                           target="playground")

    def test_refuses_a_build_number_below_the_floor(self):
        for build in ("1", "2"):
            with self.subTest(build=build):
                self.assertRefused("Build number is below this app's floor (May production starts at 3)", build=build)

    def test_refuses_while_samuels_key_is_a_placeholder(self):
        with patch("sign_only.artifact_recipient", return_value=None):
            self.assertRefused("The Console-upload public key is still a placeholder; nothing was published")

    def test_refuses_a_console_key_that_is_an_environment_handoff_key(self):
        # The handoff keys' private halves are CI secrets, so CI could open the bundle with one.
        for environment in ("may-android-internal", "may-android-playground-internal", "may-ios-internal"):
            reuse = lambda name, environment=environment: self.public if name in (
                "may-android-console-upload", environment) else None
            with self.subTest(environment=environment), patch("sign_only.artifact_recipient", side_effect=reuse):
                self.assertRefused("The Console-upload key must not be an environment's handoff key; "
                                   "nothing was published")

    def test_publishes_only_this_apps_manifest_at_this_build(self):
        """The record and report carry the package and versionCode read from the signed bundle itself."""
        cases = (
            (fx.MANIFEST_PLAYGROUND_VC9, "9", "Bundle package is not this target's app"),
            (fx.proto_manifest("io.github.mayassistant.may", "4"), "3",
             "Bundle versionCode is not the requested build number"),
            (fx.proto_manifest("io.github.mayassistant.may", "3",
                               fx.attribute(b"versionCodeMajor", b"1", fx.ANDROID_NS, 1)), "3",
             "Bundle manifest sets versionCodeMajor"),
            (b"fixture manifest", "3", "Bundle manifest is not a readable aapt2 manifest"),
        )
        for manifest, build, message in cases:
            (self.root / "may-release/signed.aab").write_bytes(fx.with_manifest(manifest))
            with self.subTest(message=message, build=build):
                try:
                    self.seal(build=build)
                    raised = None
                except Exception as error:
                    raised = error
                self.assertIsInstance(raised, verify.VerifyError)
                self.assertEqual(raised.args[0], message)
                self.assertFalse((self.root / SIGN_ONLY_DIR).exists())

    def test_a_playground_key_is_never_published_as_may_production(self):
        with patch("store.ANDROID_UPLOAD_KEYS", upload_keys(playground=FIXTURE_CERT, production=OTHER_CERT)):
            self.assertRefused("Signed Android App Bundle is not signed with this target's upload key")
        (self.root / "may-release/signed.aab").write_bytes(b"unsigned")
        self.assertRefused("Signed Android App Bundle has no single verifiable signer")

    def test_refuses_an_encrypted_file_that_does_not_decrypt_to_the_signed_bundle(self):
        real = subprocess.run

        def tampering(argv, **kwargs):
            result = real(argv, **kwargs)
            if "-d" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(b"something else")
            return result

        with patch("sign_only.subprocess.run", side_effect=tampering):
            self.assertRefused("The encrypted bundle does not decrypt to the signed bundle; nothing was published")
        self.assertEqual(sorted(p.name for p in (self.root / "may-private").iterdir()), [])

    def test_refuses_an_existing_folder_and_any_secret(self):
        (self.root / SIGN_ONLY_DIR).mkdir()
        try:
            self.seal()
            raised = None
        except Exception as error:
            raised = error
        self.assertIsInstance(raised, StoreError)
        self.assertEqual(raised.args[0], "The sign-only folder already exists; nothing was published")
        self.assertEqual(list((self.root / SIGN_ONLY_DIR).iterdir()), [])
        (self.root / SIGN_ONLY_DIR).rmdir()
        for name in SECRET_NAMES:
            with self.subTest(name=name), patch.dict(os.environ, {name: "present"}):
                self.assertRefused("A secret for another release step is present in this step")

    def test_cli_refuses_with_a_fixed_message(self):
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(HERE / "sign_only.py"), "--target", "playground",
                                 "--source-sha", SHA, "--build-number", "3"], capture_output=True, text=True,
                                cwd=self.directory.name, env=dict(os.environ))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "May sign-only refused: Sign-only is only for the May production "
                                                "Android app; nothing was published")


if __name__ == "__main__":
    unittest.main()
