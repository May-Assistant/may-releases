import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import integrity

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SHADOW = "import pathlib; pathlib.Path(__file__).with_name('SHADOW-RAN').write_text('x')\n"


class IsolatedModeTests(unittest.TestCase):
    """A module planted beside the scripts must not run in place of a stdlib import."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.copy = Path(self.directory.name) / "may_release"
        shutil.copytree(HERE, self.copy, ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        for name in ("json.py", "shutil.py", "subprocess.py", "tarfile.py", "argparse.py", "base64.py",
                     "binascii.py", "platform.py", "plistlib.py", "hashlib.py"):
            (self.copy / name).write_text(SHADOW)
        self.env = {"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": self.directory.name}

    def tearDown(self):
        self.directory.cleanup()

    def run_script(self, *argv):
        return subprocess.run([sys.executable, *argv], capture_output=True, text=True, env=self.env, cwd=self.copy)

    def test_planted_stdlib_shadow_is_ignored_in_isolated_mode(self):
        for script, args in (("cleanup.py", ["release"]), ("handoff.py", ["--help"]), ("sign.py", ["--help"]),
                             ("play_upload.py", ["--help"]), ("gate.py", ["--help"]), ("integrity.py", [])):
            with self.subTest(script=script):
                self.run_script("-I", "-S", "-B", str(self.copy / script), *args)
                self.assertFalse((self.copy / "SHADOW-RAN").exists())

    def test_the_shadow_would_run_without_isolation(self):
        # Positive control: the planted module is live, so the test above means something.
        self.run_script(str(self.copy / "cleanup.py"), "release")
        self.assertTrue((self.copy / "SHADOW-RAN").exists())


@unittest.skipUnless(shutil.which("git"), "git is required")
class CheckoutIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / ".gitignore").write_text("__pycache__/\n*.py[cod]\n.DS_Store\n")  # as in this repository
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.directory.name, "GIT_CONFIG_NOSYSTEM": "1"}
        for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                     "commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(self.root), *argv], check=True, env=env, capture_output=True)
        self.scripts = self.root / "tools/may_release"

    def tearDown(self):
        self.directory.cleanup()

    def test_clean_checkout_passes(self):
        self.assertEqual(integrity.problems(self.root), [])

    def test_git_status_catches_planted_modified_and_ignored_files(self):
        for change in (lambda: (self.scripts / "json.py").write_text(SHADOW),
                       lambda: (self.scripts / "__pycache__").mkdir() or (self.scripts / "__pycache__/store.cpython-312.pyc").write_bytes(b"x"),
                       lambda: (self.scripts / "store.py").write_text((self.scripts / "store.py").read_text() + "\n#")):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                change()
                self.assertEqual(integrity.problems(self.root), ["Checkout has modified, untracked or ignored files"])

    def test_git_ignores_system_global_and_home_config(self):
        with patch.dict(os.environ, {"HOME": "/home/attacker", "XDG_CONFIG_HOME": "/x", "GIT_DIR": "/elsewhere",
                                     "GIT_CONFIG_GLOBAL": "/home/attacker/.gitconfig", "GIT_EXEC_PATH": "/x"}):
            argv, env = integrity.git_command(self.root, "status")
        self.assertEqual(argv[:3], ["git", "-c", "core.fsmonitor=false"])
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertFalse({"HOME", "XDG_CONFIG_HOME", "GIT_DIR", "GIT_EXEC_PATH"} & set(env))
        # A hostile global config (here: an fsmonitor hook and an excludesFile that
        # would hide a planted file) is never read.
        home = Path(self.directory.name) / "home"
        home.mkdir()
        (home / "hidden").write_text("json.py\n")
        (home / ".gitconfig").write_text("[core]\n\texcludesFile = %s\n\tfsmonitor = %s\n"
                                         % (home / "hidden", home / "monitor.sh"))
        (home / "monitor.sh").write_text("#!/bin/sh\ntouch %s\n" % (home / "MONITOR-RAN"))
        (home / "monitor.sh").chmod(0o755)
        (self.scripts / "json.py").write_text(SHADOW)
        with patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}):
            self.assertEqual(integrity.problems(self.root), ["Checkout has modified, untracked or ignored files"])
        self.assertFalse((home / "MONITOR-RAN").exists())

    def test_file_list_and_hashes_are_checked_even_if_git_status_is_fooled(self):
        real_git = integrity.git

        def quiet_status(root, *args):
            return b"" if args[0] == "status" else real_git(root, *args)

        with patch("integrity.git", side_effect=quiet_status):
            self.assertEqual(integrity.problems(self.root), [])
            (self.scripts / "json.py").write_text(SHADOW)
            self.assertEqual(integrity.problems(self.root), ["Release scripts differ from the commit's file list"])
            (self.scripts / "json.py").unlink()
            (self.scripts / "cleanup.py").write_text((self.scripts / "cleanup.py").read_text().replace("removed", "gone"))
            self.assertEqual(integrity.problems(self.root), ["A release script differs from the checked-out commit"])


if __name__ == "__main__":
    unittest.main()
