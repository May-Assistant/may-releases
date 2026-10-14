import copy
import io
from pathlib import Path
import plistlib
import tempfile
import time
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



class AabManifestTests(unittest.TestCase):
    """verify.manifest_identity: the bundle's own package and versionCode, read on the release side."""

    PRODUCTION, PLAYGROUND = "io.github.mayassistant.may", "io.github.mayassistant.may.playground"

    def refused(self, data, message="Bundle manifest is not a readable aapt2 manifest"):
        try:
            verify.manifest_identity(data)
            raised = None
        except Exception as error:  # any other outcome is an assertion failure, never a test error
            raised = error
        self.assertIsInstance(raised, verify.VerifyError)
        self.assertEqual(raised.args[0], message)

    def test_real_aapt2_manifests_are_read(self):
        self.assertEqual(verify.manifest_identity(fx.MANIFEST_PRODUCTION_VC3), (self.PRODUCTION, "3"))
        self.assertEqual(verify.manifest_identity(fx.MANIFEST_PLAYGROUND_VC9), (self.PLAYGROUND, "9"))
        # The test encoder writes what aapt2 writes for these two attributes.
        self.assertEqual(verify.manifest_identity(fx.proto_manifest(self.PRODUCTION, "3")), (self.PRODUCTION, "3"))

    def test_a_real_bundle_entry_is_read_and_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signed.aab"
            path.write_bytes(fx.with_manifest(fx.MANIFEST_PRODUCTION_VC3))
            self.assertEqual(verify.aab_identity(path), (self.PRODUCTION, "3"))
            self.assertEqual(verify.check_aab_identity(path, self.PRODUCTION, "3"), (self.PRODUCTION, "3"))
            for package, number, message in ((self.PLAYGROUND, "3", "Bundle package is not this target's app"),
                                             (self.PRODUCTION, "4", "Bundle versionCode is not the requested build number"),
                                             (self.PRODUCTION, "03", "Bundle versionCode is not the requested build number")):
                with self.subTest(package=package, number=number):
                    try:
                        verify.check_aab_identity(path, package, number)
                        raised = None
                    except Exception as error:  # an assertion failure, never a test error
                        raised = error
                    self.assertIsInstance(raised, verify.VerifyError)
                    self.assertEqual(raised.args[0], message)

    def test_mutated_manifests_are_refused(self):
        ns = fx.ANDROID_NS
        cases = {
            "Bundle manifest must name exactly one package and one versionCode": [
                fx._field(1, fx._field(3, b"manifest") + fx.version_code("3")),  # no package
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode())),  # no versionCode
                fx.proto_manifest(self.PRODUCTION, "3", fx.attribute(b"package", self.PLAYGROUND.encode())),
                fx.proto_manifest(self.PRODUCTION, "3", fx.version_code("4")),
                fx.proto_manifest("may", "3"), fx.proto_manifest("", "3"), fx.proto_manifest("io.github.may/x", "3"),
            ],
            "Bundle manifest sets versionCodeMajor": [
                fx.proto_manifest(self.PRODUCTION, "3", fx.attribute(b"versionCodeMajor", b"1", ns, 1)),
                fx.proto_manifest(self.PRODUCTION, "3", fx.attribute(b"versionCodeMajor", b"0", ns, 0)),
            ],
            "Bundle manifest versionCode is malformed": [
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode()) +
                          fx.version_code("3", compiled=4)),  # string and compiled value disagree
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode()) +
                          fx.attribute(b"versionCode", b"03", ns, 3, 0x0101021B)),
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode()) +
                          fx.attribute(b"versionCode", b"0x3", ns, 3, 0x0101021B)),
            ],
            "Bundle manifest is not a readable aapt2 manifest": [
                b"", b"fixture manifest", fx.MANIFEST_PRODUCTION_VC3[:40], fx.MANIFEST_PRODUCTION_VC3[:-300],
                fx.proto_manifest(self.PRODUCTION, "3", name=b"application"),
                fx.MANIFEST_PRODUCTION_VC3 + fx.MANIFEST_PRODUCTION_VC3,  # two root elements
                fx._field(1, fx._field(3, b"manifest") + fx._field(3, b"manifest") +
                          fx.attribute(b"package", self.PRODUCTION.encode()) + fx.version_code("3")),  # name twice
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode()) +
                          fx.attribute(b"versionCode", b"3", ns, resource_id=0x0101021B)),  # no compiled value
                fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", self.PRODUCTION.encode()) +
                          fx._field(4, fx._field(1, ns) + fx._field(2, b"versionCode") + fx._field(3, b"3") +
                                    fx._field(3, b"4") + fx._field(5, 0x0101021B) +
                                    fx._field(6, fx._field(7, fx._field(6, 3))))),  # value twice
                b"\x0b\x0c",  # a group
                b"\x0a\xff\xff\xff\xff\x0f",  # a length past the end
                fx._field(1, b"\xff" * 11),  # an over-long varint
            ],
        }
        for message, manifests in cases.items():
            for manifest in manifests:
                with self.subTest(message=message[:40], manifest=manifest[:30]):
                    self.refused(manifest, message)

    def crafted(self, *attributes, name_field=None):
        """A <manifest> element from raw attribute bodies (each an XmlAttribute message)."""
        name = name_field if name_field is not None else fx._field(3, b"manifest")
        return fx._field(1, name + b"".join(fx._field(4, body) for body in attributes))

    PACKAGE = fx._field(2, b"package") + fx._field(3, b"io.github.mayassistant.may")

    @staticmethod
    def version(text=b"3", namespace=fx.ANDROID_NS, name=b"versionCode", resource_id=0x0101021B, item=None):
        body = (fx._field(1, namespace) if namespace else b"") + fx._field(2, name) + fx._field(3, text)
        if resource_id is not None:
            body += fx._field(5, resource_id)
        return body + fx._field(6, item if item is not None else fx._field(7, fx._field(6, int(text))))

    def test_identity_is_read_the_way_bundletool_reads_it(self):
        """No manifest may show one identity here and another to bundletool (re-review N1, N2)."""
        version = self.version
        # The well-formed crafted manifest is read; each crafted variant below is refused.
        self.assertEqual(verify.manifest_identity(self.crafted(self.PACKAGE, version())),
                         ("io.github.mayassistant.may", "3"))
        cases = {
            # L1: bundletool prefers the compiled string, so the package may not carry one (nor an ID).
            "Bundle manifest package is not a plain string": [
                self.PACKAGE + fx._field(6, fx._field(2, fx._field(1, b"com.evil.app"))),
                self.PACKAGE + fx._field(5, 0x01010001),
            ],
            # L2: another attribute carrying versionCode's ID, before the real one, under another name.
            # L3: android:versionCode carrying versionName's ID, the real ID under another name.
            "Bundle manifest versionCode is not exactly android:versionCode": [
                version(b"2100000000", b"http://x", b"vc"),
                version(resource_id=0x0101021C),
                version(resource_id=None),
                version(namespace=b"", name=b"versionCode"),
            ],
            # L4: versionCodeMajor's ID under another name, or its name under any namespace.
            "Bundle manifest sets versionCodeMajor": [
                version(b"1", b"http://x", b"major", 0x01010576),
                version(b"1", b"", b"versionCodeMajor", None),
            ],
            # L8: the compiled item must be exactly one primitive: no reference beside it.
            "Bundle manifest versionCode is malformed": [
                version(item=fx._field(7, fx._field(6, 3)) + fx._field(1, fx._field(1, 5))),
                version(item=fx._field(7, fx._field(6, 3) + fx._field(7, 3))),
            ],
        }
        for message, extras in cases.items():
            for extra in extras:
                with self.subTest(message=message[:40], extra=extra[:30]):
                    if message == "Bundle manifest package is not a plain string":
                        manifest = self.crafted(extra, version())
                    elif extra.startswith(fx._field(1, fx.ANDROID_NS) + fx._field(2, b"versionCode")):
                        manifest = self.crafted(self.PACKAGE, extra)  # the only versionCode, malformed
                    else:
                        manifest = self.crafted(self.PACKAGE, extra, version())  # before the real one
                    self.refused(manifest, message)
        # The reviewer's L2, L3 and L4 exactly as reported, each of which the earlier decoder read as ('…may', '3').
        exact = {
            "L2": self.crafted(self.PACKAGE, version(b"2100000000", b"http://x", b"vc"), version()),
            "L3": self.crafted(self.PACKAGE, version(resource_id=0x0101021C), version(b"7", b"http://x", b"vc")),
            "L4": self.crafted(self.PACKAGE, version(), version(b"1", b"http://x", b"major", 0x01010576)),
        }
        for case, manifest in exact.items():
            with self.subTest(case=case):
                try:
                    verify.manifest_identity(manifest)
                    raised = None
                except Exception as error:
                    raised = error
                self.assertIsInstance(raised, verify.VerifyError)

    def test_wrong_wire_types_are_refused(self):
        # N3: a varint where a string or message belongs, and a string where a varint belongs.
        version = self.version
        for manifest in (self.crafted(self.PACKAGE, version(), name_field=fx._field(3, 7)),
                         self.crafted(self.PACKAGE, fx._field(1, fx.ANDROID_NS) + fx._field(2, b"versionCode") +
                                      fx._field(3, 3) + fx._field(5, 0x0101021B) +
                                      fx._field(6, fx._field(7, fx._field(6, 3)))),
                         self.crafted(self.PACKAGE, fx._field(1, fx.ANDROID_NS) + fx._field(2, b"versionCode") +
                                      fx._field(3, b"3") + fx._field(5, b"\x01\x01\x02\x1b") +
                                      fx._field(6, fx._field(7, fx._field(6, 3)))),
                         fx._field(1, fx._field(3, b"manifest") + fx._varint(4 << 3) + fx._varint(5)),
                         fx._varint(1 << 3) + fx._varint(7)):
            with self.subTest(manifest=manifest[:30]):
                self.refused(manifest)

    def test_a_pathological_field_count_is_refused_at_once(self):
        # N4: 10,001 harmless attributes; refused by the field cap, not after minutes of parsing.
        filler = fx._field(4, fx._field(2, b"x") + fx._field(3, b"y"))
        manifest = fx._field(1, fx._field(3, b"manifest") + fx.attribute(b"package", b"io.github.mayassistant.may") +
                             fx.version_code("3") + filler * 10_001)
        started = time.monotonic()
        self.refused(manifest)
        self.assertLess(time.monotonic() - started, 5)
        # Below the cap the same attributes are fine.
        self.assertEqual(verify.manifest_identity(fx._field(1, fx._field(3, b"manifest") + fx.attribute(
            b"package", b"io.github.mayassistant.may") + fx.version_code("3") + filler * 100)),
            ("io.github.mayassistant.may", "3"))

    def test_a_bundle_without_exactly_one_readable_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.aab"
            for build, message in ((lambda z: z.writestr("base/other", b"x"), "Bundle must carry exactly one base manifest"),
                                   (None, "Bundle is not a readable zip archive")):
                if build:
                    with zipfile.ZipFile(path, "w") as archive:
                        build(archive)
                else:
                    path.write_bytes(b"not a zip")
                with self.subTest(message=message), self.assertRaises(verify.VerifyError) as refused:
                    verify.aab_identity(path)
                self.assertEqual(refused.exception.args[0], message)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base/manifest/AndroidManifest.xml", fx.MANIFEST_PRODUCTION_VC3)
                archive.writestr("base/manifest/AndroidManifest.xml", fx.MANIFEST_PLAYGROUND_VC9)
            with self.assertRaises(verify.VerifyError) as refused:
                verify.aab_identity(path)
            self.assertEqual(refused.exception.args[0], "Bundle must carry exactly one base manifest")


if __name__ == "__main__":
    unittest.main()
