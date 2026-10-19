import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ios_upload
import sign
import signing_fixtures as fx
import store
from store import SECRET_NAMES, StoreError
import verify

COMMITTED_KEYS = store.ANDROID_UPLOAD_KEYS


def keys_with(target, cert):
    """The committed table, with `target`'s certificate pin replaced by a test key's."""
    return dict(COMMITTED_KEYS, **{target: dict(COMMITTED_KEYS[target], cert=cert)})

PLAYGROUND = "io.github.mayassistant.may.playground"
HAS_JDK = bool(shutil.which("jarsigner") and shutil.which("keytool"))


def unsigned_aab(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BundleConfig.pb", b"fixture config")
        archive.writestr("base/manifest/AndroidManifest.xml", b"fixture manifest")


def fixture_ipa(path, build="9"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/May.app/embedded.mobileprovision", fx.PROFILE_BER)
        archive.writestr("Payload/May.app/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": PLAYGROUND, "CFBundleVersion": build, "CFBundleShortVersionString": "1.0"}))


class Workspace:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("may-private", "may-release", "home"):
            (self.root / name).mkdir()

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


# Skipped only off CI; in CI a missing JDK makes these fail rather than vanish.
@unittest.skipUnless(HAS_JDK or os.environ.get("CI"), "jarsigner and keytool are required for the signing round trip")
class AndroidSigningTests(unittest.TestCase):
    """Real jarsigner round trip with a keystore generated for this test only."""

    def setUp(self):
        self.work = Workspace()
        keys = self.work.root / "keys"
        keys.mkdir()
        self.keystore = keys / "upload.p12"
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["keytool", "-genkeypair", "-keystore", str(self.keystore), "-storetype", "PKCS12",
                        "-storepass", "fixturepass", "-keypass", "fixturepass", "-alias", "may-playground-upload",
                        "-keyalg", "RSA", "-keysize", "2048", "-validity", "2",
                        "-dname", "CN=May test upload key"], **quiet)
        certificate = keys / "upload.der"
        subprocess.run(["keytool", "-exportcert", "-keystore", str(self.keystore), "-storepass", "fixturepass",
                        "-alias", "may-playground-upload", "-file", str(certificate)], **quiet)
        self.fingerprint = hashlib.sha256(certificate.read_bytes()).hexdigest()
        unsigned_aab(self.work.root / "may-release/unsigned.aab")
        self.env = {"RUNNER_TEMP": str(self.work.root), "PATH": os.environ.get("PATH", ""),
                    "HOME": str(self.work.root / "home"),
                    "MAY_ANDROID_KEYSTORE_BASE64": base64.b64encode(self.keystore.read_bytes()).decode(),
                    "MAY_ANDROID_STORE_PASSWORD": "fixturepass", "MAY_ANDROID_KEY_PASSWORD": "fixturepass",
                    "MAY_ANDROID_KEY_ALIAS": "may-playground-upload", "MAY_ANDROID_CERT_SHA256": self.fingerprint}
        for name in ("JAVA_HOME",):
            if name in os.environ:
                self.env[name] = os.environ[name]
        self.keystore.unlink()
        certificate.unlink()

    def tearDown(self):
        self.work.directory.cleanup()

    def test_signs_verifies_and_removes_keystore(self):
        with patch.dict(os.environ, self.env, clear=True), patch("sign.jarsigner", return_value=shutil.which("jarsigner")), \
                patch("store.ANDROID_UPLOAD_KEYS", keys_with("playground", self.fingerprint)):
            sign.sign_android("playground", "a" * 40, "9")
        signed = self.work.root / "may-release/signed.aab"
        self.assertEqual(verify.aab_signer_sha256(signed), self.fingerprint)
        self.assertEqual([p.name for p in (self.work.root / "may-release").iterdir()], ["signed.aab"])
        self.assertFalse((self.work.root / "may-private/upload.keystore").exists())

    def test_wrong_expected_certificate_is_refused_and_bundle_removed(self):
        self.env["MAY_ANDROID_CERT_SHA256"] = "ab" * 32
        with patch.dict(os.environ, self.env, clear=True), patch("sign.jarsigner", return_value=shutil.which("jarsigner")), \
                patch("store.ANDROID_UPLOAD_KEYS", keys_with("playground", "ab" * 32)):
            with self.assertRaises(StoreError):
                sign.sign_android("playground", "a" * 40, "9")
        self.assertFalse((self.work.root / "may-release/signed.aab").exists())
        self.assertFalse((self.work.root / "may-private/upload.keystore").exists())

    def test_a_playground_key_never_signs_may_production(self):
        # The real keystore is Playground's (alias and pin); May production refuses it before jarsigner runs.
        with patch.dict(os.environ, self.env, clear=True), patch("sign.jarsigner", return_value=shutil.which("jarsigner")), \
                patch("store.ANDROID_UPLOAD_KEYS", keys_with("playground", self.fingerprint)), \
                patch("sign.run_private") as runner:
            with self.assertRaises(StoreError) as refused:
                sign.sign_android("production", "a" * 40, "9")
            runner.assert_not_called()
        self.assertEqual(refused.exception.args[0], "Android key alias is not this target's upload key")
        self.assertFalse((self.work.root / "may-release/signed.aab").exists())


class AndroidSigningIsolationTests(unittest.TestCase):
    def setUp(self):
        self.pinned = patch("sign.jarsigner", return_value="/pinned/jdk/bin/jarsigner")
        self.pinned.start()
        self.addCleanup(self.pinned.stop)
        self.work = Workspace()
        unsigned_aab(self.work.root / "may-release/unsigned.aab")
        self.env = {"RUNNER_TEMP": str(self.work.root), "PATH": "/usr/bin",
                    "MAY_ANDROID_KEYSTORE_BASE64": base64.b64encode(b"keystore").decode(),
                    "MAY_ANDROID_STORE_PASSWORD": "store-secret", "MAY_ANDROID_KEY_PASSWORD": "key-secret",
                    "MAY_ANDROID_KEY_ALIAS": "may-playground-upload", "MAY_ANDROID_CERT_SHA256": fx.AAB_CERT_SHA256}
        self.keys = patch("store.ANDROID_UPLOAD_KEYS",
                          keys_with("playground", verify.normalize_fingerprint(fx.AAB_CERT_SHA256)))
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def tearDown(self):
        self.work.directory.cleanup()

    def test_each_target_signs_only_with_its_own_alias_and_certificate_pin(self):
        production = COMMITTED_KEYS["production"]["cert"]
        playground_like = COMMITTED_KEYS["playground"]["cert"] + "0" * 48
        cases = (
            # (target, alias, MAY_ANDROID_CERT_SHA256, expected refusal)
            ("production", "may-playground-upload", production, "Android key alias is not this target's upload key"),
            ("production", "may-upload", playground_like,
             "MAY_ANDROID_CERT_SHA256 is not this target's upload certificate"),
            ("production", "may-upload", fx.AAB_CERT_SHA256,
             "MAY_ANDROID_CERT_SHA256 is not this target's upload certificate"),
            ("playground", "may-upload", fx.AAB_CERT_SHA256, "Android key alias is not this target's upload key"),
            ("playground", "may-playground-upload", production,
             "MAY_ANDROID_CERT_SHA256 is not this target's upload certificate"),
            ("playground", "upload", fx.AAB_CERT_SHA256, "Android key alias is not this target's upload key"),
        )
        for target, alias, cert, message in cases:
            env = dict(self.env, MAY_ANDROID_KEY_ALIAS=alias, MAY_ANDROID_CERT_SHA256=cert)
            with self.subTest(target=target, alias=alias, cert=cert), patch.dict(os.environ, env, clear=True), \
                    patch("sign.run_private") as runner:
                raised = None
                try:
                    sign.sign_android(target, "a" * 40, "9")
                except Exception as error:  # any other outcome is an assertion failure, never a test error
                    raised = error
                self.assertIsInstance(raised, StoreError)
                runner.assert_not_called()
                self.assertEqual(raised.args[0], message)
        # The committed production alias and pin pass this check (jarsigner itself is faked here).
        env = dict(self.env, MAY_ANDROID_KEY_ALIAS="may-upload", MAY_ANDROID_CERT_SHA256=production.upper())
        with patch.dict(os.environ, env, clear=True), patch("store.ANDROID_UPLOAD_KEYS", COMMITTED_KEYS), \
                patch("sign.run_private") as runner, patch("sign.verify_signed_aab"):
            sign.sign_android("production", "a" * 40, "9")
            self.assertEqual(runner.call_args[0][0][-1], "may-upload")

    def test_jarsigner_gets_only_its_password_variables(self):
        captured = {}

        def fake_jarsigner(argv, cwd, log, env):
            captured.update(argv=argv, env=env)
            (self.work.root / "may-release/signed.aab").write_bytes(fx.SIGNED_AAB)

        with patch.dict(os.environ, self.env, clear=True), \
                patch("sign.run_private", side_effect=fake_jarsigner), \
                patch("sign.verify_signed_aab"):
            sign.sign_android("playground", "a" * 40, "9")
        self.assertEqual(captured["argv"][0], "/pinned/jdk/bin/jarsigner")
        self.assertNotIn("MAY_ANDROID_KEYSTORE_BASE64", captured["env"])
        self.assertEqual(captured["env"]["MAY_ANDROID_STORE_PASSWORD"], "store-secret")
        self.assertNotIn("store-secret", captured["argv"])
        self.assertNotIn("key-secret", captured["argv"])
        self.assertFalse((self.work.root / "may-private/upload.keystore").exists())

    def test_signing_step_refuses_other_steps_secrets(self):
        for name in set(SECRET_NAMES) - set(sign.ANDROID_SIGNING_SECRETS):
            with self.subTest(name=name), patch.dict(os.environ, dict(self.env, **{name: "x"}), clear=True), \
                    patch("sign.run_private") as runner:
                with self.assertRaises(StoreError):
                    sign.sign_android("playground", "a" * 40, "9")
                runner.assert_not_called()

    def test_key_alias_cannot_be_an_option(self):
        for alias in ("-J-Dx=y", "-storepass", "", "a" * 65, "up load", "upload"):
            with self.subTest(alias=alias), patch.dict(os.environ, dict(self.env, MAY_ANDROID_KEY_ALIAS=alias),
                                                       clear=True), patch("sign.run_private") as runner:
                with self.assertRaises(StoreError):
                    sign.sign_android("playground", "a" * 40, "9")
                runner.assert_not_called()

    def test_pinned_jarsigner_is_required(self):
        self.pinned.stop()
        with patch.dict(os.environ, self.env, clear=True), patch("sign.run_private") as runner:
            with self.assertRaises(StoreError):
                sign.sign_android("playground", "a" * 40, "9")
            runner.assert_not_called()
        self.pinned.start()

    def test_already_signed_input_is_refused(self):
        (self.work.root / "may-release/unsigned.aab").write_bytes(fx.SIGNED_AAB)
        with patch.dict(os.environ, self.env, clear=True), patch("sign.run_private") as runner:
            with self.assertRaises(StoreError):
                sign.sign_android("playground", "a" * 40, "9")
            runner.assert_not_called()


class IosSigningTests(unittest.TestCase):
    """Orchestration only: keychain and xcodebuild are faked on Linux."""

    def setUp(self):
        self.work = Workspace()
        (self.work.root / "may-release/May.xcarchive").mkdir()
        self.env = {"RUNNER_TEMP": str(self.work.root), "PATH": "/usr/bin", "HOME": str(self.work.root / "home"),
                    "MAY_IOS_CERT_BASE64": base64.b64encode(b"p12").decode(),
                    "MAY_IOS_CERT_PASSWORD": "p12-secret",
                    "MAY_IOS_PROFILE_BASE64": base64.b64encode(fx.PROFILE_BER).decode()}

    def tearDown(self):
        self.work.directory.cleanup()

    def run_signing(self, build="9"):
        """Run sign_ios for build 9; the faked export produces an IPA for `build`."""
        calls = []

        def fake_tool(argv, cwd, log, env):
            calls.append((argv, env))
            if argv[:2] == [sign.XCODEBUILD, "-exportArchive"]:
                exported = Path(argv[argv.index("-exportPath") + 1])
                exported.mkdir()
                fixture_ipa(exported / "May.ipa", build)
                options = plistlib.loads(Path(argv[argv.index("-exportOptionsPlist") + 1]).read_bytes())
                calls.append(("options", options))

        def fake_query(argv, **kwargs):
            calls.append((argv, kwargs.get("env")))
            out = (fx.PROFILE_CERT_SHA1.encode() + b' "Apple Distribution"') if "find-identity" in argv else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

        with patch.dict(os.environ, self.env, clear=True), patch("sign.APPLE_CERT_SHA1", fx.PROFILE_CERT_SHA1), \
                patch("sign.run_private", side_effect=fake_tool), patch("sign.subprocess.run", side_effect=fake_query):
            sign.sign_ios("playground", "a" * 40, "9")
        return calls

    def test_exports_verified_ipa_and_cleans_up(self):
        calls = self.run_signing()
        self.assertTrue((self.work.root / "may-release/May.ipa").is_file())
        options = next(c[1] for c in calls if c[0] == "options")
        self.assertEqual(options["provisioningProfiles"], {PLAYGROUND: fx.PROFILE_UUID})
        self.assertIs(options["testFlightInternalTestingOnly"], True)
        self.assertEqual(options["method"], "app-store-connect")
        self.assertIn([sign.SECURITY, "delete-keychain"], [c[0][:2] for c in calls if c[0] != "options"])
        for argv, env in calls:
            if argv != "options":
                self.assertFalse(set(env or {}) & set(SECRET_NAMES))
        self.assertEqual(self.work.files(), ["may-release/May.ipa"])

    def test_export_with_wrong_build_is_removed(self):
        with self.assertRaises(verify.VerifyError):
            self.run_signing(build="10")
        self.assertFalse((self.work.root / "may-release/May.ipa").exists())
        self.assertEqual(self.work.files(), [])

    def test_profile_for_another_app_is_refused_before_any_tool(self):
        with patch.dict(os.environ, self.env, clear=True), patch("sign.APPLE_CERT_SHA1", fx.PROFILE_CERT_SHA1), \
                patch("sign.run_private") as runner:
            with self.assertRaises(verify.VerifyError):
                sign.sign_ios("production", "a" * 40, "9")
            runner.assert_not_called()


class IosUploadTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()
        fixture_ipa(self.work.root / "may-release/May.ipa")
        self.env = {"RUNNER_TEMP": str(self.work.root), "PATH": "/usr/bin",
                    "MAY_ASC_KEY_BASE64": base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nfixture\n").decode(),
                    "MAY_ASC_KEY_ID": "23VX669FGY", "MAY_ASC_ISSUER_ID": "00000000-1111-2222-3333-444444444444",
                    "MAY_DRY_RUN": "false"}

    def tearDown(self):
        self.work.directory.cleanup()

    def test_uploads_with_key_file_only_for_the_upload_tool(self):
        captured = {}

        def fake_altool(argv, cwd, log, env):
            key_dir = Path(env["API_PRIVATE_KEYS_DIR"])
            captured.update(argv=argv, env=env, keys=sorted(p.name for p in key_dir.iterdir()))
            log.write_text("No errors uploading '" + argv[4] + "'.\nDelivery UUID: 0a1b2c3d-1111-2222-3333-444455556666\n")

        def fake_token(key_path, issuer):
            captured.update(token_key=key_path.name, token_key_present=key_path.is_file(), issuer=issuer)
            return "token"

        def fake_get(token, url):
            captured.update(url=url)
            return 200, {"data": [{"id": "upload-1", "attributes": {
                "cfBundleVersion": "9", "cfBundleShortVersionString": "1.0", "platform": "IOS",
                "createdDate": "2099-01-01T00:00:00Z", "state": {"state": "PROCESSING"}}}]}

        with patch.dict(os.environ, self.env, clear=True), patch("ios_upload.run_private", side_effect=fake_altool), \
                patch("ios_upload.asc_token", side_effect=fake_token), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            ios_upload.upload("playground", "a" * 40, "9", get=fake_get, sleep=lambda _: None)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[1], "  delivery UUID: 0a1b2c3d-1111-2222-3333-444455556666")
        self.assertNotIn(str(self.work.root), output.getvalue())
        self.assertEqual(lines[0], "altool output (scrubbed; the full log stays private and is deleted at cleanup):")
        self.assertEqual(lines[4], "App Store Connect build upload upload-1 is PROCESSING for build 9")
        self.assertEqual(lines[8], "  track: TestFlight internal testing only")
        self.assertEqual(lines[-1], "  App Store Connect build upload: upload-1 (read back from App Store Connect; "
                                    "TestFlight processing and testers are checked there)")
        # The token is signed from the same key, while it is still on disk; the readback asks for May Playground.
        self.assertEqual((captured["token_key"], captured["token_key_present"]), ("AuthKey_23VX669FGY.p8", True))
        self.assertEqual(captured["issuer"], self.env["MAY_ASC_ISSUER_ID"])
        self.assertTrue(captured["url"].startswith("https://api.appstoreconnect.apple.com/v1/apps/6822072535/"
                                                   "buildUploads?filter%5BcfBundleVersion%5D=9&"))
        self.assertEqual(captured["keys"], ["AuthKey_23VX669FGY.p8"])
        self.assertFalse(set(captured["env"]) & set(SECRET_NAMES))
        self.assertIn("--upload-app", captured["argv"])
        self.assertFalse((self.work.root / "may-private/asc-keys").exists())
        # It records the upload for the loud-failure step (confirm_upload.py).
        record = json.loads((self.work.root / "may-release/upload-record.json").read_text())
        self.assertEqual(record, {"platform": "ios", "app_id": PLAYGROUND, "build_number": "9",
                                  "track": "TestFlight internal testing only", "store_id": "upload-1",
                                  "sha256": hashlib.sha256((self.work.root / "may-release/May.ipa").read_bytes()).hexdigest()})

    def test_failed_upload_leaves_no_record(self):
        def failed_altool(argv, cwd, log, env):
            log.write_text("*** Error: Error uploading '" + argv[4] + "'. ITMS-90062\n")
            raise StoreError("Protected native release failed")

        with patch.dict(os.environ, self.env, clear=True), \
                patch("ios_upload.run_private", side_effect=failed_altool), \
                patch("ios_upload.asc_token") as token, patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(StoreError):
                ios_upload.upload("playground", "a" * 40, "9")
            token.assert_not_called()
        self.assertFalse((self.work.root / "may-release/upload-record.json").exists())
        # The scrubbed excerpt is printed even when altool fails, before cleanup removes its log.
        self.assertEqual(output.getvalue().splitlines()[1:], ["  delivery UUID: none",
                                                              "  lines naming an error: 1; naming a warning: 0",
                                                              "  codes: ITMS-90062"])
        self.assertFalse((self.work.root / "may-private/asc-keys").exists())

    def test_altool_exit_zero_without_a_build_upload_fails_and_leaves_no_record(self):
        """Run 39192306300: altool exited 0, and App Store Connect had no build upload."""
        clock = iter(range(0, 10000, 30))
        with patch.dict(os.environ, self.env, clear=True), patch("ios_upload.run_private"), \
                patch("ios_upload.asc_token", return_value="token"), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(StoreError) as refused:
                ios_upload.upload("playground", "a" * 40, "9", get=lambda token, url: (200, {"data": []}),
                                  sleep=lambda _: None, clock=lambda: next(clock))
        self.assertIn("before the timeout", refused.exception.args[0])
        self.assertEqual(output.getvalue().splitlines()[-1], "altool finished, so the upload may be in App Store "
                                                             "Connect; check there before retrying")
        self.assertFalse((self.work.root / "may-release/upload-record.json").exists())
        self.assertFalse((self.work.root / "may-private/asc-keys").exists())

    def test_unsigned_token_after_altool_says_the_upload_may_be_there(self):
        with patch.dict(os.environ, self.env, clear=True), patch("ios_upload.run_private"), \
                patch("ios_upload.OPENSSL", "/nonexistent/openssl"), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(StoreError) as refused:
                ios_upload.upload("playground", "a" * 40, "9", get=lambda token, url: self.fail("no readback"))
        self.assertEqual(refused.exception.args[0], "App Store Connect token could not be signed")
        self.assertEqual(output.getvalue().splitlines()[-1], "altool finished, so the upload may be in App Store "
                                                             "Connect; check there before retrying")
        self.assertFalse((self.work.root / "may-private/asc-keys").exists())

    def test_mismatched_ipa_or_other_secrets_are_refused_before_upload(self):
        with patch.dict(os.environ, self.env, clear=True), patch("ios_upload.run_private") as runner:
            with self.assertRaises(verify.VerifyError):
                ios_upload.upload("playground", "a" * 40, "10")
            with self.assertRaises(verify.VerifyError):
                ios_upload.upload("production", "a" * 40, "9")
            runner.assert_not_called()
        for name in ("MAY_IOS_CERT_PASSWORD", "MAY_PLAY_JSON", "MAY_APP_READ_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, dict(self.env, **{name: "x"}), clear=True), \
                    patch("ios_upload.run_private") as runner:
                with self.assertRaises(StoreError):
                    ios_upload.upload("playground", "a" * 40, "9")
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
