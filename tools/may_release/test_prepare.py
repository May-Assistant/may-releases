import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare import PrepareError, exact_sha, fetch, pins, prepare
from store import StoreError


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

    def test_git_gets_an_allowlisted_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            seen = []
            env = {"PATH": "/usr/bin", "HOME": directory, "MAY_APP_READ_KEY": "k", "GIT_DIR": "/elsewhere",
                   "LD_PRELOAD": "/tmp/x.so", "PYTHONPATH": "/tmp", "SOME_TOKEN": "t"}
            with patch.dict(os.environ, env, clear=True), \
                    patch("prepare.quiet", side_effect=lambda argv, log, env: seen.append(env)), \
                    patch("prepare.git_status", return_value=1):
                with self.assertRaises(PrepareError):
                    fetch(Path(directory) / "may-source", "git@example.invalid:x.git", "a" * 40,
                          "fixture read key", private, "app")
            for git_env in seen:
                self.assertFalse({"MAY_APP_READ_KEY", "GIT_DIR", "LD_PRELOAD", "PYTHONPATH", "SOME_TOKEN"} & set(git_env))
                self.assertEqual(git_env["GIT_TERMINAL_PROMPT"], "0")
            self.assertTrue(seen)

    def test_prepare_refuses_any_other_steps_secret(self):
        for name in ("MAY_PLAY_JSON", "MAY_ANDROID_KEYSTORE_BASE64", "MAY_ARTIFACT_DECRYPT_KEY"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"RUNNER_TEMP": directory, "MAY_APP_READ_KEY": "k", name: "x"}), \
                    patch("prepare.fetch") as fetching:
                with self.assertRaises(StoreError):
                    prepare("a" * 40)
                fetching.assert_not_called()


if __name__ == "__main__":
    unittest.main()
