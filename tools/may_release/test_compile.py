import tempfile
import unittest
from pathlib import Path
import zipfile

from compile import extract_generator


class CompileTests(unittest.TestCase):
    def test_pinned_zip_binary_becomes_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "generator.zip"
            info = zipfile.ZipInfo("xcodegen/bin/xcodegen")
            info.external_attr = 0o100755 << 16
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(info, b"test executable")
            binary = extract_generator(archive, root / "extracted")
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
