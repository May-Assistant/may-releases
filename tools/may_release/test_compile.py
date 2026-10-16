import tempfile
import unittest
from pathlib import Path
import zipfile

from compile import IOS_SCHEMES, extract_generator


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


    def test_the_playground_compile_builds_only_may_playground(self):
        # The internal target may build at iOS's own design-system pin; the May scheme needs the pins to match.
        self.assertEqual(IOS_SCHEMES["playground"], ("MayPlayground",))
        self.assertEqual(IOS_SCHEMES[None], ("May", "MayPlayground"))


    def test_the_compile_refuses_any_secret_in_its_step(self):
        # Security review of #16, F1: XcodeGen, SwiftPM and Xcode run here; no secret may reach them.
        import os
        from unittest import mock
        from compile import compile_native
        from store import StoreError
        for name in ("MAY_APP_READ_KEY", "MAY_ASC_KEY_BASE64"):
            with self.subTest(secret=name), mock.patch.dict(os.environ, {name: "x"}):
                try:
                    compile_native("ios", "playground")
                    raised = None
                except Exception as error:  # noqa: BLE001 - which refusal comes first is the point
                    raised = error
                self.assertIsInstance(raised, StoreError, "refused for another reason, or not at all")


if __name__ == "__main__":
    unittest.main()
