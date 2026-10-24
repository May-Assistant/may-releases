import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from store import StoreError
import toolchain


def archive_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size, info.mode = len(data), 0o755
                archive.addfile(info, io.BytesIO(data))
            else:
                info.type, info.linkname = tarfile.SYMTYPE, data
                archive.addfile(info)
    return buffer.getvalue()


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, RUNNER_TEMP=self.directory.name)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def pin(self, data, checksum=None, top="age"):
        source = self.root / "download.tar.gz"
        source.write_bytes(data)
        return patch.dict(toolchain.TOOLS, {("age", "Linux", "x86_64"): (
            source.as_uri(), checksum or hashlib.sha256(data).hexdigest(), top)})

    def test_pins_are_exact_https_sha256(self):
        for (name, system, machine), (url, checksum, top) in toolchain.TOOLS.items():
            self.assertTrue(url.startswith("https://github.com/"), url)
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")
        self.assertIn("21.0.12.1", toolchain.TOOLS[("jdk", "Linux", "x86_64")][0])

    def test_verified_archive_installs(self):
        with self.pin(archive_bytes([("age/age", b"#!/bin/sh\n", "file")])):
            home = toolchain.install("age", "Linux", "x86_64")
            with patch("platform.system", return_value="Linux"), patch("platform.machine", return_value="x86_64"):
                self.assertEqual(toolchain.tool_path("age", "age"), str(home / "age"))
        self.assertFalse((self.root / "may-tools/age.tar.gz").exists())

    def test_checksum_mismatch_is_refused_and_nothing_is_left(self):
        with self.pin(archive_bytes([("age/age", b"tampered", "file")]), checksum="0" * 64):
            with self.assertRaises(StoreError):
                toolchain.install("age", "Linux", "x86_64")
        self.assertEqual(sorted(p.name for p in (self.root / "may-tools").iterdir()), [])

    def test_unsafe_archive_member_is_refused(self):
        for entries in ([("../escape", b"x", "file")], [("age/link", "/etc/passwd", "link")]):
            with self.subTest(entries=entries[0][0]), self.pin(archive_bytes(entries)):
                with self.assertRaises(Exception):
                    toolchain.install("age", "Linux", "x86_64")
                self.assertFalse((self.root / "escape").exists())
                self.assertFalse((self.root / "may-tools/age").exists())

    def test_missing_tool_is_refused(self):
        with patch("platform.system", return_value="Linux"), patch("platform.machine", return_value="x86_64"):
            with self.assertRaises(StoreError):
                toolchain.tool_path("jdk", "bin/jarsigner")
        with self.assertRaises(StoreError):
            toolchain.install("age", "Plan9", "mips")


if __name__ == "__main__":
    unittest.main()


class AptPinTests(unittest.TestCase):
    """ffmpeg for the flow board's previews: Ubuntu's own package, at exactly the recorded version."""

    def run_with(self, version):
        calls = []

        class Done:
            def __init__(self, code=0, out=b""):
                self.returncode, self.stdout = code, out

        def run(argv, stdout, stderr, stdin, env, timeout):
            calls.append(argv)
            return Done(out=version.encode()) if argv[0] == "dpkg-query" else Done()

        with patch("toolchain.platform.system", return_value="Linux"):
            toolchain.apt_install("ffmpeg", run=run)
        return calls

    def test_the_pinned_version_is_installed_and_checked(self):
        calls = self.run_with(toolchain.APT_PINS["ffmpeg"])
        self.assertEqual(calls[1][-1], "ffmpeg=7:6.1.1-3ubuntu5")
        self.assertEqual(calls[0], ["sudo", "apt-get", "update", "-qq"])
        self.assertEqual(calls[2][:2], ["dpkg-query", "-W"])

    def test_another_version_or_runner_is_refused(self):
        with self.assertRaises(StoreError):
            self.run_with("7:6.1.1-3ubuntu6")
        with patch("toolchain.platform.system", return_value="Darwin"), self.assertRaises(StoreError):
            toolchain.apt_install("ffmpeg", run=lambda *a, **k: self.fail("ran"))
        with self.assertRaises(StoreError):
            toolchain.apt_install("curl", run=lambda *a, **k: self.fail("ran"))
