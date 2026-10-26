"""A pull request lane's record, sealed in the job that ran the pull request's code and opened in the post job
(sealed.py; android-unit-pr.yml and backend-unit-pr.yml)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sealed
from sealed import SealedError

SHA, OTHER = "a" * 40, "b" * 40
NAMES = ("outcome.json", "failures.json")
PRIVATE_TEXT = "PRIVATE-TEST-NAME-é-7c2e"
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))
PUBLIC = "age1" + "q" * 58


def keypair():
    secret = next(line for line in subprocess.run(["age-keygen"], capture_output=True, text=True,
                                                  check=True).stdout.splitlines() if line.startswith("AGE-SECRET-KEY-1"))
    public = subprocess.run(["age-keygen", "-y"], input=secret, capture_output=True, text=True, check=True).stdout.strip()
    return secret, public


class Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.private = self.root / sealed.PRIVATE
        self.private.mkdir()
        self.record()
        self.ran = []

    def tearDown(self):
        self.directory.cleanup()

    def record(self, failures=None):
        (self.private / NAMES[0]).write_text(json.dumps({"result": "failed"}))
        (self.private / NAMES[1]).write_text(json.dumps({"failures": failures if failures is not None else [PRIVATE_TEXT]}))

    def tools(self, name, relative):
        return relative

    def run_step(self, call, env):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin", **env}, clear=True), \
                patch("sealed.toolchain.tool_path", side_effect=self.tools):
            return call()


class SealRefusalTests(Case):
    """What seal refuses or skips before age is ever run."""

    def tools(self, name, relative):
        raise AssertionError("age was run")

    def test_without_the_recipient_nothing_is_sealed(self):
        self.assertFalse(self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), {}))
        self.assertFalse((self.root / sealed.SEALED).exists())

    def test_an_inexact_sha_a_bad_recipient_a_missing_link_or_non_json_file_is_refused(self):
        env = {sealed.RECIPIENT: PUBLIC}
        with self.assertRaisesRegex(SealedError, "exact lowercase commit SHA"):
            self.run_step(lambda: sealed.seal(self.root, SHA.upper(), NAMES), env)
        with self.assertRaisesRegex(SealedError, "not an age public key"):
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), {sealed.RECIPIENT: "ssh-ed25519 AAAA"})
        (self.private / NAMES[1]).write_text("{not json")
        with self.assertRaisesRegex(SealedError, "is not JSON"):
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), env)
        (self.private / NAMES[1]).unlink()
        outside = self.root / "outside.json"
        outside.write_text("{}")
        (self.private / NAMES[1]).symlink_to(outside)
        with self.assertRaisesRegex(SealedError, "is missing"):
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), env)
        (self.private / NAMES[1]).unlink()
        with self.assertRaisesRegex(SealedError, "is missing"):
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), env)
        self.assertFalse((self.root / sealed.SEALED).exists())

    def test_a_record_larger_than_the_padding_is_refused(self):
        self.record(["x" * sealed.SIZE])
        with self.assertRaisesRegex(SealedError, "too large to seal"):
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), {sealed.RECIPIENT: PUBLIC})

    def test_nothing_downloaded_opens_nothing_and_the_key_leaves_the_environment(self):
        seen = {}
        def opened():
            value = sealed.open_sealed(self.root, SHA, NAMES)
            seen["key"] = os.environ.get(sealed.DECRYPT_KEY)
            return value
        self.assertFalse(self.run_step(opened, {sealed.DECRYPT_KEY: "AGE-SECRET-KEY-1x", sealed.RECIPIENT: PUBLIC}))
        self.assertIsNone(seen["key"])

    def test_a_malformed_key_or_recipient_is_refused_before_age(self):
        (self.root / sealed.DOWNLOAD).mkdir()
        (self.root / sealed.DOWNLOAD / sealed.NAME).write_bytes(b"x")
        with self.assertRaisesRegex(SealedError, "MAY_RENDERS_DECRYPT_KEY"):
            self.run_step(lambda: sealed.open_sealed(self.root, SHA, NAMES),
                          {sealed.DECRYPT_KEY: "not a key", sealed.RECIPIENT: PUBLIC})
        with self.assertRaisesRegex(SealedError, "MAY_RENDERS_RECIPIENT"):
            self.run_step(lambda: sealed.open_sealed(self.root, SHA, NAMES), {sealed.DECRYPT_KEY: "x"})


@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class RoundTripTests(Case):
    """seal, the one upload, open, with the system's age."""

    def setUp(self):
        super().setUp()
        # Found now, on the job's own PATH: each step runs with PATH /usr/bin:/bin, which CI's pinned age is not on.
        self.found = {name: shutil.which(name) for name in ("age", "age-keygen")}
        self.secret, self.public = keypair()

    def tools(self, name, relative):
        return self.found[relative]

    def handed_over(self):
        self.assertTrue(self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), {sealed.RECIPIENT: self.public}))
        data = (self.root / sealed.SEALED / sealed.NAME).read_bytes()
        self.assertNotIn(PRIVATE_TEXT.encode(), data)
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), sorted(NAMES), "no plain bundle is left")
        # The post job is a fresh runner: nothing of the unit job's but the download.
        shutil.rmtree(self.private)
        (self.root / sealed.DOWNLOAD).mkdir(exist_ok=True)
        (self.root / sealed.DOWNLOAD / sealed.NAME).write_bytes(data)

    def opened(self, sha=SHA, key=None, names=NAMES):
        return self.run_step(lambda: sealed.open_sealed(self.root, sha, names),
                             {sealed.RECIPIENT: self.public, sealed.DECRYPT_KEY: key or self.secret})

    def test_the_record_crosses_sealed_and_opens_as_it_was(self):
        self.handed_over()
        self.assertTrue(self.opened())
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), sorted(NAMES), "the key file is gone")
        self.assertEqual(json.loads((self.private / NAMES[1]).read_text()), {"failures": [PRIVATE_TEXT]})

    def test_every_sealed_record_is_the_same_size(self):
        sizes = []
        for failures in ([], [PRIVATE_TEXT], ["é" * 300] * 1000):
            self.record(failures)
            self.run_step(lambda: sealed.seal(self.root, SHA, NAMES), {sealed.RECIPIENT: self.public})
            sizes.append((self.root / sealed.SEALED / sealed.NAME).stat().st_size)
        self.assertEqual(len(set(sizes)), 1, sizes)

    def test_another_key_a_tampered_or_unpadded_record_another_commit_or_other_files_are_refused(self):
        self.handed_over()
        with self.assertRaisesRegex(SealedError, "does not match MAY_RENDERS_RECIPIENT"):
            self.opened(key=keypair()[0])
        download = self.root / sealed.DOWNLOAD / sealed.NAME
        good = download.read_bytes()
        tampered = bytearray(good)
        tampered[-5] ^= 1
        download.write_bytes(bytes(tampered))
        with self.assertRaisesRegex(SealedError, "failed to decrypt or authenticate"):
            self.opened()
        plain = self.root / "short.json"
        plain.write_text(json.dumps({"sha": SHA, "files": {}}))
        subprocess.run([self.found["age"], "-r", self.public, "-o", str(download), str(plain)], check=True)
        with self.assertRaisesRegex(SealedError, "failed to decrypt or authenticate"):
            self.opened()
        download.write_bytes(good)
        with self.assertRaisesRegex(SealedError, "for another commit"):
            self.opened(sha=OTHER)
        with self.assertRaisesRegex(SealedError, "for another commit"):
            self.opened(names=NAMES[:1])
        self.assertEqual(list(self.private.iterdir()), [], "nothing refused is left for the post")


if __name__ == "__main__":
    unittest.main()
