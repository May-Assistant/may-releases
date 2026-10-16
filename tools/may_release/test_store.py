import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import store
from store import POSTHOG_SECRETS, SECRET_NAMES, StoreError, build, child_env, identity, play_identity

HERE = Path(__file__).resolve().parent
ACCOUNT = "may-play-upload@may-assistant-app.iam.gserviceaccount.com"
CREDENTIAL = json.dumps({"type": "service_account", "project_id": "may-assistant-app",
                         "client_email": ACCOUNT, "private_key": "dummy"})


def prepared_tree(root):
    source, private = root / "may-source", root / "may-private"
    (source / "tools/release").mkdir(parents=True)
    for name in ("android.sh", "ios.sh"):
        (source / "tools/release" / name).write_text("exit 0\n")
    private.mkdir()
    return source, private


class StoreTests(unittest.TestCase):
    def test_four_distinct_app_id_routes(self):
        sha = "a" * 40
        self.assertEqual(identity("ios", "playground", sha, "9"), "io.github.mayassistant.may.playground")
        self.assertEqual(identity("android", "production", sha, "9"), "io.github.mayassistant.may")
        for platform, target, build_number in (("rem", "production", "9"), ("ios", "rem", "9"),
                                               ("ios", "production", "09")):
            with self.assertRaises(StoreError):
                identity(platform, target, sha, build_number)

    def test_build_number_ceiling(self):
        self.assertEqual(identity("android", "production", "a" * 40, "2000000000"), "io.github.mayassistant.may")
        for value in ("2000000001", "2100000000", "12345678901", "0", ""):
            with self.subTest(value=value), self.assertRaises(StoreError):
                identity("android", "production", "a" * 40, value)

    def test_play_account_must_be_may_only(self):
        play_identity(CREDENTIAL, ACCOUNT)
        with self.assertRaises(StoreError):
            play_identity(CREDENTIAL, "rem-play-upload@rem-claw.iam.gserviceaccount.com")

    def test_non_object_play_credential_is_a_fixed_store_error(self):
        for value in ("[]", "null", '"text"', "1", "[{\"type\": \"service_account\"}]"):
            with self.subTest(value=value), self.assertRaises(StoreError):
                play_identity(value, ACCOUNT)

    def test_non_object_play_credential_cli_prints_only_the_fixed_message(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": directory,
                   "MAY_PLAY_JSON": '["private-looking-value"]', "MAY_DRY_RUN": "false",
                   # May Playground's own account, so the credential itself is what is refused.
                   "MAY_PLAY_ACCOUNT": "may-playground-upload@may-assistant-app.iam.gserviceaccount.com"}
            result = subprocess.run([sys.executable, "-s", str(HERE / "play_upload.py"), "--target", "playground",
                                     "--source-sha", "a" * 40, "--build-number", "9"],
                                    capture_output=True, text=True, env=env, cwd=directory)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "May Play upload stopped: Protected May Play credential is not a "
                                                "JSON object; inspect the store before any retry")

    def test_child_env_is_an_allowlist(self):
        with patch.dict(os.environ, {"PATH": "/bin", "JAVA_HOME_21_X64": "/jdk", "SOME_TOKEN": "x",
                                     "GITHUB_TOKEN": "x", "MAY_PLAY_JSON": "x"}, clear=True):
            env = child_env(EXTRA="1")
        self.assertEqual(env, {"PATH": "/bin", "JAVA_HOME_21_X64": "/jdk", "EXTRA": "1"})

    def test_unsigned_build_gets_no_secret_and_requests_unsigned_product(self):
        for platform, product, command in (("android", "unsigned.aab", "build-unsigned"),
                                           ("ios", "May.xcarchive", "archive-unsigned")):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, private = prepared_tree(root)
                captured = {}

                def fake_private(argv, cwd, log, env):
                    captured.update(argv=argv, env=env)
                    made = Path(env["MAY_ARTIFACT_DIR"]) / product
                    made.mkdir() if platform == "ios" else made.write_bytes(b"unsigned")

                with patch.dict(os.environ, {"RUNNER_TEMP": directory, "UNRELATED_TOKEN": "nope"}), \
                        patch("store.prepared", return_value=(source, private)), \
                        patch("store.run_private", side_effect=fake_private):
                    build(platform, "playground", "a" * 40, "9")
                self.assertEqual(captured["argv"][-1], command)
                self.assertFalse(set(captured["env"]) & set(SECRET_NAMES))
                self.assertNotIn("UNRELATED_TOKEN", captured["env"])
                self.assertEqual(captured["env"]["MAY_APP_ID"], "io.github.mayassistant.may.playground")
                self.assertEqual(captured["env"]["MAY_BUILD_NUMBER"], "9")
                self.assertTrue((root / "may-release" / product).exists())

    def test_unsigned_build_refuses_when_any_secret_is_in_its_step(self):
        # The Android build may hold the PostHog key (its own tests below); iOS refuses every secret.
        cases = [("android", n) for n in SECRET_NAMES if n not in POSTHOG_SECRETS] + [("ios", n) for n in SECRET_NAMES]
        for platform, name in cases:
            with self.subTest(platform=platform, name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, private = prepared_tree(root)
                with patch.dict(os.environ, {"RUNNER_TEMP": directory, name: "present"}), \
                        patch("store.prepared", return_value=(source, private)), \
                        patch("store.run_private") as runner:
                    with self.assertRaises(StoreError):
                        build(platform, "playground", "a" * 40, "9")
                runner.assert_not_called()

    def android_build(self, environ, runs=None):
        """Runs the Android build with environ; returns (env given to android.sh, printed text). Appends to runs."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, private = prepared_tree(root)
            captured = {}

            def fake_private(argv, cwd, log, env):
                captured.update(env)
                if runs is not None:
                    runs.append(argv)
                (Path(env["MAY_ARTIFACT_DIR"]) / "unsigned.aab").write_bytes(b"unsigned")

            printed = io.StringIO()
            with patch.dict(os.environ, {"RUNNER_TEMP": directory, **environ}, clear=True), \
                    patch("store.prepared", return_value=(source, private)), \
                    patch("store.run_private", side_effect=fake_private), \
                    contextlib.redirect_stdout(printed):
                build("android", "playground", "a" * 40, "9")
            return captured, printed.getvalue()

    def test_every_other_release_step_refuses_the_posthog_key(self):
        from store import ANDROID_SIGNING_SECRETS, HANDOFF_SECRETS, PLAY_UPLOAD_SECRETS, SOURCE_READ_SECRETS, \
            require_step_secrets
        for allowed in ((), SOURCE_READ_SECRETS, HANDOFF_SECRETS, ANDROID_SIGNING_SECRETS, PLAY_UPLOAD_SECRETS):
            with self.subTest(allowed=allowed), patch.dict(os.environ, {"MAY_POSTHOG_ANDROID_KEY": "phc_x"}):
                with self.assertRaises(StoreError):
                    require_step_secrets(allowed)

    def test_the_posthog_key_and_host_reach_the_android_build(self):
        env, printed = self.android_build({"MAY_POSTHOG_ANDROID_KEY": "phc_Abc-123_x",
                                           "MAY_POSTHOG_HOST": "https://eu.i.posthog.com"})
        self.assertEqual(env.get("MAY_POSTHOG_ANDROID_KEY"), "phc_Abc-123_x")
        self.assertEqual(env.get("MAY_POSTHOG_HOST"), "https://eu.i.posthog.com")
        self.assertIn("PostHog crash-report key passed to the Android build", printed)
        self.assertNotIn("phc_Abc-123_x", printed)

    def test_without_the_key_the_build_has_none_and_says_so_once(self):
        for environ in ({}, {"MAY_POSTHOG_ANDROID_KEY": ""}, {"MAY_POSTHOG_HOST": "https://eu.i.posthog.com"}):
            with self.subTest(environ=environ):
                env, printed = self.android_build(environ)
                self.assertNotIn("MAY_POSTHOG_ANDROID_KEY", env)
                self.assertNotIn("MAY_POSTHOG_HOST", env)
                self.assertEqual(printed.count("No PostHog key in this build environment; this build sends no crash reports"), 1)

    def test_a_malformed_key_or_host_is_refused_before_building(self):
        for environ in ({"MAY_POSTHOG_ANDROID_KEY": "phc key\nINJECT"},
                        {"MAY_POSTHOG_ANDROID_KEY": "phc_ok", "MAY_POSTHOG_HOST": "http://plain.example"},
                        {"MAY_POSTHOG_ANDROID_KEY": "phc_ok", "MAY_POSTHOG_HOST": "https://x.example/path"}):
            with self.subTest(environ=environ):
                runs = []
                with self.assertRaises(StoreError):
                    self.android_build(environ, runs)
                self.assertEqual(runs, [])

    def test_unsigned_build_must_leave_exactly_its_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, private = prepared_tree(root)

            def extra_output(argv, cwd, log, env):
                (Path(env["MAY_ARTIFACT_DIR"]) / "unsigned.aab").write_bytes(b"unsigned")
                (Path(env["MAY_ARTIFACT_DIR"]) / "signed.aab").write_bytes(b"signed by the script")

            with patch.dict(os.environ, {"RUNNER_TEMP": directory}), \
                    patch("store.prepared", return_value=(source, private)), \
                    patch("store.run_private", side_effect=extra_output):
                with self.assertRaises(StoreError):
                    build("android", "playground", "a" * 40, "9")

    def test_cleanup_removes_only_task_owned_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("may-private", "may-release", "may-handoff-download", "may-tools", "_work"):
                (root / name).mkdir()
                (root / name / "f").write_text("x")
            result = subprocess.run([sys.executable, "-s", str(HERE / "cleanup.py"), "release"],
                                    env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": directory},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["_work", "may-tools"])


if __name__ == "__main__":
    unittest.main()
