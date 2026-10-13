from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare import PrepareError, exact_sha, fetch, pins


class PrepareTests(unittest.TestCase):
    def test_exact_sha(self):
        self.assertEqual(exact_sha("a" * 40), "a" * 40)
        for value in ("A" * 40, "abc", "a" * 39 + "/"):
            with self.assertRaises(PrepareError):
                exact_sha(value)

    def test_cross_platform_pin_must_match(self):
        project = ("packages:\n  MayDesignSystem:\n"
                   "    url: https://github.com/May-Assistant/may-design-system\n"
                   "    revision: " + "a" * 40 + "\n")
        lock = "repository=https://github.com/May-Assistant/may-design-system\nrevision=" + "a" * 40
        self.assertEqual(pins(project, lock)[0], "a" * 40)
        with self.assertRaises(PrepareError):
            pins(project, lock.replace("a" * 40, "b" * 40))
        with self.assertRaises(PrepareError):
            pins(project, lock.replace("May-Assistant", "Somewhere-Else"))

    def test_read_key_is_removed_when_setup_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            real_write_text = Path.write_text

            def failing_write(path, *args, **kwargs):
                if path.name == "known_hosts":
                    raise OSError("disk full")
                return real_write_text(path, *args, **kwargs)

            with patch.object(Path, "write_text", failing_write), patch("prepare.quiet") as quiet:
                with self.assertRaises(OSError):
                    fetch(Path(directory) / "may-source", "git@example.invalid:x.git", "a" * 40,
                          "fixture read key", private, "app")
                quiet.assert_not_called()
            self.assertEqual(list(private.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
