import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare import PrepareError, exact_sha, fetch, pins, prepare
import store
from store import DESIGN_SYSTEM_RECORD, StoreError

DS_URL = "https://github.com/May-Assistant/may-design-system"
IOS_PIN, ANDROID_PIN = "1" * 40, "2" * 40  # differing, as on may-app main today


def project_yml(revision=IOS_PIN, url=DS_URL):
    return "packages:\n  MayDesignSystem:\n    url: " + url + "\n    revision: " + revision + "\n"


def lock_file(revision=ANDROID_PIN, url=DS_URL):
    return "repository=" + url + "\nrevision=" + revision + "\n"


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
        self.assertEqual(pins(project, lock)[0], {"ios": "a" * 40, "android": "a" * 40})
        # Without a store target (the native compile) the two must still match.
        with self.assertRaises(StoreError):
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



class PerPlatformPinTests(unittest.TestCase):
    """Samuel's decision: internal builds check only their own platform's pin; others need a match."""

    def run_prepare(self, platform, target, project=None, lock=None, checked_out=None):
        """prepare() with git replaced: returns (DS revisions fetched, record, project.yml after)."""
        fetched = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_fetch(dest, remote, sha, key_text, private, label, main_history=False):
                dest.mkdir()
                if label == "app":
                    (dest / "ios").mkdir()
                    (dest / "android").mkdir()
                    (dest / "ios/project.yml").write_text(project if project is not None else project_yml())
                    (dest / "android/design-system.lock").write_text(lock if lock is not None else lock_file())
                else:
                    fetched.append(sha)

            def fake_head(checkout):
                return checked_out if checked_out is not None else fetched[-1]

            with patch.dict(os.environ, {"RUNNER_TEMP": directory, "MAY_APP_READ_KEY": "k",
                                         "MAY_DS_READ_KEY": "k"}), \
                    patch("prepare.fetch", side_effect=fake_fetch), patch("prepare.head_revision", side_effect=fake_head):
                try:
                    prepare("a" * 40, platform, target)
                finally:
                    record_path = root / "may-private" / DESIGN_SYSTEM_RECORD
                    self.record = json.loads(record_path.read_text()) if record_path.exists() else None
                    self.fetched = fetched
            return fetched, self.record, (root / "may-source/ios/project.yml").read_text()

    def test_internal_android_run_with_mismatched_pins_passes(self):
        for target in ("playground", "production"):
            with self.subTest(target=target):
                fetched, record, project = self.run_prepare("android", target)
                self.assertEqual(fetched, [ANDROID_PIN])  # its own platform's pin, nothing else
                self.assertEqual(project, project_yml())  # iOS spec left untouched: it pins another revision
                self.assertEqual(record, {"platform": "android", "target": target,
                                          "design_system": {"ios": IOS_PIN, "android": ANDROID_PIN}})

    def test_internal_ios_run_with_mismatched_pins_builds_the_ios_pin(self):
        fetched, record, project = self.run_prepare("ios", "playground")
        self.assertEqual(fetched, [IOS_PIN])
        self.assertIn("  MayDesignSystem:\n    path: ", project)
        self.assertNotIn(IOS_PIN, project)
        self.assertEqual(record["design_system"], {"ios": IOS_PIN, "android": ANDROID_PIN})

    def test_internal_run_whose_own_pin_is_wrong_or_untrusted_fails(self):
        cases = (
            ("android", dict(lock=lock_file(url="https://github.com/Somewhere-Else/may-design-system"))),
            ("android", dict(lock=lock_file(revision="2" * 39))),
            ("android", dict(lock=lock_file(revision="B" * 40))),
            ("android", dict(lock="repository=" + DS_URL + "\n")),
            ("ios", dict(project=project_yml(url="https://github.com/Somewhere-Else/may-design-system"))),
            ("ios", dict(project=project_yml(revision="main"))),
            ("ios", dict(project="packages:\n  MayDesignSystem:\n    path: ../may-design-system\n")),
        )
        for platform, files in cases:
            with self.subTest(platform=platform, files=files), self.assertRaises(PrepareError):
                self.run_prepare(platform, "playground", **files)
            self.assertEqual(self.fetched, [])  # refused before the design system is fetched
            self.assertIsNone(self.record)

    def test_internal_run_whose_own_pin_is_not_what_is_checked_out_fails(self):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform), self.assertRaises(PrepareError):
                self.run_prepare(platform, "playground", checked_out="3" * 40)
            self.assertIsNone(self.record)

    def test_ios_pin_must_stay_trusted_even_when_android_is_built(self):
        # Not compared for an internal Android build, but still checked and recorded.
        for project in (project_yml(url="https://github.com/Somewhere-Else/may-design-system"),
                        project_yml(revision="v1.0"), "packages: {}\n"):
            with self.subTest(project=project), self.assertRaises(PrepareError):
                self.run_prepare("android", "playground", project=project)
            self.assertEqual(self.fetched, [])

    def test_non_internal_target_with_mismatched_pins_fails(self):
        # No such target exists yet: model one by taking a pair off the internal list.
        with patch.object(store, "INTERNAL_TARGETS", store.INTERNAL_TARGETS - {("android", "production")}):
            with self.assertRaises(StoreError):
                self.run_prepare("android", "production")
            self.assertEqual(self.fetched, [])
            fetched, record, _ = self.run_prepare("android", "production", lock=lock_file(IOS_PIN))
            self.assertEqual(fetched, [IOS_PIN])
            # The internal pair next to it is unaffected.
            self.run_prepare("android", "playground")
        for platform, target in (("android", "public-beta"), (None, None)):
            with self.subTest(target=target), self.assertRaises(StoreError):
                store.design_system_pins({"ios": IOS_PIN, "android": ANDROID_PIN}, platform, target)
        # The native compile names no target, so it still requires the match.
        with self.assertRaises(StoreError):
            self.run_prepare(None, None)
        self.assertEqual(self.run_prepare(None, None, lock=lock_file(IOS_PIN))[0], [IOS_PIN])

    def test_platform_and_target_are_given_together(self):
        for platform, target in (("android", None), (None, "playground")):
            with self.subTest(platform=platform), self.assertRaises(PrepareError):
                self.run_prepare(platform, target)

    def test_internal_targets_are_one_explicit_list(self):
        self.assertEqual(store.INTERNAL_TARGETS, {("ios", "production"), ("ios", "playground"),
                                                  ("android", "production"), ("android", "playground")})
        self.assertLessEqual(store.INTERNAL_TARGETS, set(store.APP_IDS))
        # Adding an app ID does not make it internal.
        with patch.dict(store.APP_IDS, {("android", "public"): "io.github.mayassistant.may"}):
            self.assertFalse(store.is_internal("android", "public"))


if __name__ == "__main__":
    unittest.main()
