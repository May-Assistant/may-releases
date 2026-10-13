import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import handoff
from store import StoreError

SHA = "a" * 40
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))
RUN = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_RUN_ID": "123456"}


def system_tool(name, relative):
    return shutil.which(Path(relative).name)


def expected(build="9", run="123456", platform="android"):
    with patch.dict(os.environ, dict(RUN, GITHUB_RUN_ID=run)):
        return handoff.expected_manifest(platform, "playground", SHA, build)


class TarRulesTests(unittest.TestCase):
    """The plaintext rules, without encryption."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def tar(self, manifest, members):
        path = self.root / "handoff.tar"
        with tarfile.open(path, "w") as archive:
            for name, data in [("manifest.json", json.dumps(manifest).encode())] + members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_matching_manifest_yields_product(self):
        path = self.tar(expected(), [("product/unsigned.aab", b"bundle")])
        product = handoff.read_tar(path, self.root / "staging", expected())
        self.assertEqual(product.read_bytes(), b"bundle")

    def test_mismatched_run_target_source_or_build_is_refused(self):
        for name, manifest in (("build", expected(build="10")), ("run", expected(run="999")),
                               ("platform", dict(expected(), platform="ios")),
                               ("source", dict(expected(), source_sha="b" * 40)),
                               ("extra-field", dict(expected(), note="x"))):
            with self.subTest(name=name):
                path = self.tar(manifest, [("product/unsigned.aab", b"bundle")])
                with self.assertRaises(StoreError):
                    handoff.read_tar(path, self.root / ("staging-" + name), expected())

    def test_unexpected_members_are_refused(self):
        for members in ([("product/unsigned.aab", b"x"), ("product/other.aab", b"y")],
                        [("product/../../escape", b"x")], [("evil.sh", b"x")], []):
            with self.subTest(members=[m[0] for m in members]):
                path = self.tar(expected(), members)
                with self.assertRaises(StoreError):
                    handoff.read_tar(path, self.root / "staging-x", expected())
                self.assertFalse((self.root / "escape").exists())


# Skipped only off CI; in CI a missing age makes these fail rather than vanish.
@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class AgeRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        keys = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        self.keyfile = keys  # what the setup lane pastes: the whole age-keygen output
        self.secret = next(l for l in keys.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run(["age-keygen", "-y"], input=self.secret, capture_output=True,
                                     text=True, check=True).stdout.strip()
        (self.root / "may-release").mkdir()
        (self.root / "may-release/unsigned.aab").write_bytes(b"unsigned bundle")
        self.patches = [patch.dict(os.environ, dict(RUN, RUNNER_TEMP=self.directory.name, PATH=os.environ["PATH"]),
                                   clear=True),
                        patch("handoff.toolchain.tool_path", side_effect=system_tool),
                        patch("handoff.artifact_recipient", return_value=self.public)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def carry(self):
        """Simulate the artifact hop to a fresh release VM."""
        download = self.root / "may-handoff-download"
        download.mkdir()
        shutil.move(str(self.root / "may-handoff/handoff.age"), str(download / "handoff.age"))
        shutil.rmtree(self.root / "may-release")
        shutil.rmtree(self.root / "may-handoff")

    def test_round_trip_restores_exact_product_and_leaves_no_key(self):
        handoff.seal("android", "playground", SHA, "9")
        sealed = (self.root / "may-handoff/handoff.age").read_bytes()
        self.assertNotIn(b"unsigned bundle", sealed)
        self.assertEqual(sorted(p.name for p in (self.root / "may-handoff").iterdir()), ["handoff.age"])
        self.carry()
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.keyfile):
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertEqual((self.root / "may-release/unsigned.aab").read_bytes(), b"unsigned bundle")
        self.assertEqual(sorted(p.name for p in (self.root / "may-private").iterdir()), [])

    def test_other_build_number_or_run_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        for changes, build in (({}, "10"), ({"GITHUB_RUN_ID": "654321"}, "9")):
            with self.subTest(changes=changes, build=build), \
                    patch.dict(os.environ, dict(changes, MAY_ARTIFACT_DECRYPT_KEY=self.secret)):
                shutil.rmtree(self.root / "may-release", ignore_errors=True)
                with self.assertRaises(StoreError):
                    handoff.open_handoff("android", "playground", SHA, build)
                self.assertFalse((self.root / "may-release/unsigned.aab").exists())

    def test_wrong_key_or_tampered_ciphertext_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        other = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        other_secret = next(l for l in other.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=other_secret), self.assertRaises(StoreError) as refused:
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertIn("does not match this target's committed public key", refused.exception.args[0])
        sealed = self.root / "may-handoff-download/handoff.age"
        data = bytearray(sealed.read_bytes())
        data[-20] ^= 0x01
        sealed.write_bytes(bytes(data))
        shutil.rmtree(self.root / "may-release", ignore_errors=True)
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.secret), self.assertRaises(StoreError):
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertFalse((self.root / "may-release/unsigned.aab").exists())

    def test_malformed_decrypt_key_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        for value in ("", self.secret + "\n" + self.secret, self.public, "AGE-SECRET-KEY-1short"):
            with self.subTest(value=value[:20]), patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=value):
                shutil.rmtree(self.root / "may-release", ignore_errors=True)
                with self.assertRaises(StoreError):
                    handoff.open_handoff("android", "playground", SHA, "9")

    def test_steps_refuse_other_secrets_and_placeholders(self):
        with patch.dict(os.environ, MAY_PLAY_JSON="x"), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.secret), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")  # the build job never holds the key
        with patch("handoff.artifact_recipient", return_value=None), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")


if __name__ == "__main__":
    unittest.main()
