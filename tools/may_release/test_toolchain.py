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
            # GitHub releases, or Node.js's own dist host for Node alone.
            self.assertTrue(url.startswith("https://github.com/") or
                            (name == "node" and url.startswith("https://nodejs.org/dist/v")), url)
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")
        self.assertIn("21.0.12.1", toolchain.TOOLS[("jdk", "Linux", "x86_64")][0])
        # may-app backend-ci.yml's Node, exactly.
        url, _, top = toolchain.TOOLS[("node", "Linux", "x86_64")]
        self.assertEqual((url, top), ("https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.gz",
                                      "node-v22.22.0-linux-x64"))

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
