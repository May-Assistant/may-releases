import copy
import io
from pathlib import Path
import plistlib
import tempfile
import unittest
import warnings
import zipfile

import signing_fixtures as fx
import verify
from verify import VerifyError

TEAM, PLAYGROUND, MAY = "R6A892D599", "io.github.mayassistant.may.playground", "io.github.mayassistant.may"


def rewrite_zip(source, target, extra=(), drop=()):
    with zipfile.ZipFile(io.BytesIO(source)) as old, zipfile.ZipFile(target, "w") as new:
        for item in old.infolist():
            if item.filename not in drop:
                new.writestr(item, old.read(item.filename))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # duplicate-name fixtures are deliberate
            for name, data in extra:
                new.writestr(name, data)


def ipa(path, profile=fx.PROFILE_BER, bundle_id=PLAYGROUND, build="9", apps=("May.app",)):
    with zipfile.ZipFile(path, "w") as archive:
        for app in apps:
            archive.writestr(f"Payload/{app}/embedded.mobileprovision", profile)
            archive.writestr(f"Payload/{app}/Info.plist", plistlib.dumps(
                {"CFBundleIdentifier": bundle_id, "CFBundleVersion": build}, fmt=plistlib.FMT_BINARY))
            archive.writestr(f"Payload/{app}/May", b"binary")


class AabVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_signer_certificate_sha256_matches_keytool(self):
        path = self.root / "signed.aab"
        path.write_bytes(fx.SIGNED_AAB)
        self.assertEqual(verify.aab_signer_sha256(path), verify.normalize_fingerprint(fx.AAB_CERT_SHA256))
        self.assertEqual(verify.signature_entries(path), ["META-INF/FIXTURE.SF", "META-INF/FIXTURE.RSA"])

    def test_unsigned_extra_or_duplicate_entries_are_refused(self):
        for name, extra in (("added", [("base/dex/classes.dex", b"unsigned code")]),
                            ("duplicate", [("BundleConfig.pb", b"shadowing entry")]),
                            ("second-signer", [("META-INF/OTHER.RSA", b"x"), ("META-INF/OTHER.SF", b"x")]),
                            ("sig-entry", [("META-INF/SIG-EXTRA", b"x")])):
            with self.subTest(name=name):
                path = self.root / (name + ".aab")
                rewrite_zip(fx.SIGNED_AAB, path, extra=extra)
                with self.assertRaises(VerifyError):
                    verify.aab_signer_sha256(path)

    def test_pre_sign_check_sees_every_signature_entry(self):
        path = self.root / "presigned.aab"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("BundleConfig.pb", b"x")
            archive.writestr("META-INF/SIG-ANDROID", b"x")
        self.assertEqual(verify.signature_entries(path), ["META-INF/SIG-ANDROID"])

    def test_unsigned_bundle_is_refused(self):
        path = self.root / "unsigned.aab"
        rewrite_zip(fx.SIGNED_AAB, path, drop=("META-INF/FIXTURE.SF", "META-INF/FIXTURE.RSA", "META-INF/MANIFEST.MF"))
        self.assertEqual(verify.signature_entries(path), [])
        with self.assertRaises(VerifyError):
            verify.aab_signer_sha256(path)

    def test_signature_block_must_be_cms_signed_data(self):
        with zipfile.ZipFile(io.BytesIO(fx.SIGNED_AAB)) as archive:
            block = archive.read("META-INF/FIXTURE.RSA")
        verify.sole_signer_certificate(block)
        for broken in (block[:-10], b"\x30\x03\x02\x01\x01", block[:20] + b"\x00" * (len(block) - 20), b""):
            with self.assertRaises(VerifyError):
                verify.sole_signer_certificate(broken)

    def test_fingerprint_normalization(self):
        expected = "a91dcfcf6650b9ac4ec3a577468f50a9f9b728a13bb2ff80fe540b179a4417d9"
        self.assertEqual(verify.normalize_fingerprint(fx.AAB_CERT_SHA256), expected)
        self.assertEqual(verify.normalize_fingerprint(expected.upper()), expected)
        for value in ("", "abc", expected + "00", "zz" * 32, None):
            with self.assertRaises(VerifyError):
                verify.normalize_fingerprint(value)


class IosVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.profile = verify.profile_plist(fx.PROFILE_BER)

    def tearDown(self):
        self.directory.cleanup()

    def test_ber_profile_is_read(self):
        self.assertEqual(verify.check_profile(self.profile, TEAM, PLAYGROUND, fx.PROFILE_CERT_SHA1), fx.PROFILE_UUID)

    def test_profile_must_match_team_bundle_store_and_certificate(self):
        with self.assertRaises(VerifyError):
            verify.check_profile(self.profile, "ABCDE12345", PLAYGROUND)
        with self.assertRaises(VerifyError):
            verify.check_profile(self.profile, TEAM, MAY)
        with self.assertRaises(VerifyError):
            verify.check_profile(self.profile, TEAM, PLAYGROUND, "0" * 40)
        for change in ({"ProvisionedDevices": ["00008110-000000000000001E"]}, {"ProvisionsAllDevices": True},
                       {"TeamIdentifier": [TEAM, "ABCDE12345"]}):
            candidate = dict(self.profile, **change)
            with self.subTest(change=sorted(change)), self.assertRaises(VerifyError):
                verify.check_profile(candidate, TEAM, PLAYGROUND)
        candidate = copy.deepcopy(self.profile)
        candidate["Entitlements"]["get-task-allow"] = True
        with self.assertRaises(VerifyError):
            verify.check_profile(candidate, TEAM, PLAYGROUND)

    def test_ipa_embedded_profile_bundle_and_build(self):
        good = self.root / "good.ipa"
        ipa(good)
        verify.ipa_identity(good, TEAM, PLAYGROUND, "9")
        # Each case changes one thing; the expected bundle is the one in Info.plist
        # unless the case is about Info.plist itself.
        cases = {
            "profile-for-other-app": (dict(bundle_id=MAY), MAY),  # Playground profile inside a May IPA
            "info-bundle": (dict(bundle_id=MAY), PLAYGROUND),
            "build": (dict(build="10"), PLAYGROUND),
            "two-apps": (dict(apps=("May.app", "Other.app")), PLAYGROUND),
            "not-cms": (dict(profile=b"<plist/>"), PLAYGROUND),
        }
        for name, (options, expected_bundle) in cases.items():
            with self.subTest(name=name):
                path = self.root / (name + ".ipa")
                ipa(path, **options)
                with self.assertRaises(VerifyError):
                    verify.ipa_identity(path, TEAM, expected_bundle, "9")


if __name__ == "__main__":
    unittest.main()
