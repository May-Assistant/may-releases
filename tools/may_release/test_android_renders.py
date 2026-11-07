"""May Playground's Android renders (android-renders.yml): android_renders.py run, seal and verdict, and publish.py
android-open, android-push and android-comment. Gradle, age and GitHub are faked; nothing here reaches the network."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import android_renders
from android_renders import AndroidRendersError
import publish
from publish import PublishError
import static_check
from store import StoreError
from test_compile_pr import pull

SHA = "a" * 40
FOLDER = SHA[:12]
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/android-renders.yml"
PNG = android_renders.PNG + b"rest"


class Workspace:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "may-source/android").mkdir(parents=True)
        (self.root / "may-private").mkdir()
        java = self.root / "may-tools/jdk/bin/java"
        java.parent.mkdir(parents=True)
        java.write_text("")
        self.java = java

    def cleanup(self):
        self.directory.cleanup()


class RunTests(unittest.TestCase):
    def setUp(self):
        self.space = Workspace()
        self.root = self.space.root
        self.output = self.root / "github-output"
        self.calls = []

    def tearDown(self):
        self.space.cleanup()

    def run_tests(self, status=0, write=None, timeout=False, **extra):
        def gradle(argv, cwd, env, stdout, stderr, stdin, timeout):
            self.calls.append((argv, cwd, env))
            stdout.write(b"gradle output from may-app\n")
            snapshots = Path(argv[-1].split("=", 1)[1])
            for relative, data in (write or {}).items():
                path = snapshots / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if data is None:
                    path.symlink_to(self.root / "may-private/android-renders.log")
                else:
                    path.write_bytes(data)
            if raise_timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, status)

        raise_timeout = timeout
        env = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(self.output), **extra}
        with patch.dict(os.environ, env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(android_renders.toolchain, "tool_path", lambda tool, rel: str(self.space.java)):
            android_renders.run_tests(SHA, gradle)
        return out.getvalue()

    def test_the_fixed_task_and_filter_run_with_the_pinned_jdk_and_no_secret(self):
        out = self.run_tests(write={"flow-a/phone-light.png": PNG, "top.png": PNG})
        (argv, cwd, env), = self.calls
        self.assertEqual(argv[:5], ["./gradlew", "--no-daemon", ":app:testPlaygroundDebugUnitTest", "--tests",
                                    "*Renders*"])
        self.assertEqual(argv[5], "-Dmay.renders.dir=" + str(self.root / android_renders.SNAPSHOTS))
        self.assertEqual(cwd, self.root / "may-source/android")
        self.assertEqual(env["JAVA_HOME"], str(self.root / "may-tools/jdk"))
        self.assertEqual(env["GRADLE_USER_HOME"], str(self.root / "may-gradle"))
        self.assertEqual(out, "May Android renders: tests passed; 2 images\n")
        self.assertEqual(self.output.read_text(), "tests=passed\n")
        folder = self.root / android_renders.OUT / FOLDER
        self.assertEqual(sorted(p.name for p in (folder / "android").iterdir()),
                         ["flow-a__phone-light.png", "top.png"])
        self.assertIn("gradle output", (folder / "build.log").read_text())
        self.assertIn("android/flow-a__phone-light.png", (folder / "README.md").read_text())
        self.assertIn("tests passed, 2 images", (folder / "index.html").read_text())
        self.assertEqual(json.loads((self.root / "may-private" / android_renders.OUTCOME).read_text()),
                         {"tests": "passed", "images": 2})

    def test_nothing_from_may_app_reaches_the_log(self):
        out = self.run_tests(status=1, write={"SecretFlowName/x.png": PNG})
        self.assertEqual(out, "May Android renders: tests failed; 1 images\n")
        self.assertNotIn("SecretFlowName", out)
        self.assertEqual(self.output.read_text(), "tests=failed\n")

    def test_a_timeout_is_recorded_and_the_renders_made_are_kept(self):
        out = self.run_tests(timeout=True, write={"a.png": PNG})
        self.assertEqual(out, "May Android renders: tests timeout; 1 images\n")

    def test_only_real_pngs_of_a_publishable_name_are_kept(self):
        out = self.run_tests(write={"good.png": PNG, "fake.png": b"GIF89a", "link.png": None, "-dash.png": PNG,
                                    "bad name.png": PNG, "notes.txt": b"x"})
        self.assertEqual(out, "May Android renders: tests passed; 1 images\n")
        self.assertEqual([p.name for p in (self.root / android_renders.OUT / FOLDER / "android").iterdir()],
                         ["good.png"])

    def test_without_the_pinned_jdk_nothing_runs(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch.object(android_renders.toolchain, "tool_path", side_effect=StoreError("x")), \
                self.assertRaises(AndroidRendersError):
            android_renders.run_tests(SHA, lambda *a, **k: self.fail("ran"))

    def test_another_steps_secret_or_a_malformed_sha_refuses(self):
        with self.assertRaises(StoreError):
            self.run_tests(MAY_APP_READ_KEY="x")
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), self.assertRaises(AndroidRendersError):
            android_renders.run_tests("A" * 40, lambda *a, **k: self.fail("ran"))
        self.assertEqual(self.calls, [])


class SealVerdictTests(unittest.TestCase):
    def setUp(self):
        self.space = Workspace()
        self.root = self.space.root
        folder = self.root / android_renders.OUT / FOLDER
        (folder / "android").mkdir(parents=True)
        (folder / "android/a.png").write_bytes(PNG)
        (folder / "index.html").write_text("x")

    def tearDown(self):
        self.space.cleanup()

    def test_without_a_recipient_nothing_is_sealed(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            android_renders.seal(SHA)
        self.assertIn("nothing was sealed", out.getvalue())
        self.assertFalse((self.root / android_renders.SEALED).exists())

    def test_seal_encrypts_one_tar_and_keeps_no_plaintext(self):
        calls = []

        def age(argv, **kwargs):
            calls.append(argv)
            Path(argv[argv.index("-o") + 1]).write_bytes(b"sealed")
            return subprocess.CompletedProcess(argv, 0)

        recipient = "age1" + "q" * 58
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), android_renders.RECIPIENT: recipient}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch.object(android_renders.toolchain, "tool_path", lambda tool, name: "/fake/" + name), \
                patch("android_renders.subprocess.run", side_effect=age):
            android_renders.seal(SHA)
        (argv,), = [calls]
        self.assertEqual(argv[:3], ["/fake/age", "-r", recipient])
        sealed = self.root / android_renders.SEALED
        self.assertEqual(sorted(p.name for p in sealed.iterdir()), ["renders.age"])
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), android_renders.RECIPIENT: "not a key"},
                        clear=True), self.assertRaises(AndroidRendersError):
            shutil.rmtree(sealed)
            android_renders.seal(SHA)

    def test_verdict(self):
        outcome = self.root / "may-private" / android_renders.OUTCOME
        for recorded, passes in (({"tests": "passed", "images": 3}, True), ({"tests": "passed", "images": 0}, False),
                                 ({"tests": "failed", "images": 3}, False), ({"tests": "timeout", "images": 3}, False),
                                 (None, False)):
            with self.subTest(recorded=recorded), patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                    patch("sys.stdout", new_callable=io.StringIO):
                outcome.unlink(missing_ok=True)
                if recorded is not None:
                    outcome.write_text(json.dumps(recorded))
                if passes:
                    android_renders.verdict(SHA)
                else:
                    with self.assertRaises(AndroidRendersError):
                        android_renders.verdict(SHA)


class PublishAndroidTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        tree = self.root / publish.TREE / FOLDER / "android"
        tree.mkdir(parents=True)
        (tree / "a.png").write_bytes(PNG)
        (tree / "b.png").write_bytes(PNG)

    def tearDown(self):
        self.directory.cleanup()

    def test_the_run_folder_layout_is_the_ios_one(self):
        for name, kind in ((FOLDER, "dir"), (f"{FOLDER}/android", "dir"), (f"{FOLDER}/android/Flow__a.png", "file"),
                           (f"{FOLDER}/README.md", "file"), (f"{FOLDER}/build.log", "file"),
                           (f"{FOLDER}/index.html", "file"), (f"{FOLDER}/android/x/y.png", None)):
            with self.subTest(name=name):
                self.assertEqual(publish.allowed(name, FOLDER), kind)

    def test_push_publishes_under_its_own_prefix(self):
        calls = []

        def publish_tree(tree, prefix, folder, entry, message, key_text, run_git):
            calls.append((tree, prefix, folder, entry, message))
            return ["x"], [], None

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": "k",
                                     "GITHUB_RUN_ID": "77"}, clear=True), \
                patch.object(publish, "write_key", lambda: "key"), patch.object(publish, "publish_tree", publish_tree), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.push_android(SHA, "42")
        (tree, prefix, folder, entry, message), = calls
        self.assertEqual((tree, prefix, folder), (self.root / publish.TREE / FOLDER, "android-renders/", FOLDER))
        self.assertEqual(entry["pull"], 42)
        self.assertEqual(out.getvalue(), f"May Android renders published to may-renders as android-renders/{FOLDER}/ "
                                         "(1 run folders kept, 0 removed)\n")

    def comment(self, tests="passed", head=SHA, comments=()):
        calls = []

        def api(method, path, payload, token):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/pulls/42"):
                return 200, pull(head__sha=head)
            if method == "GET":
                return 200, list(comments)
            return 201, {}

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_PR_COMMENT_TOKEN": "c" * 30,
                                     "MAY_CI_APP_SLUG": "may-ci", "MAY_RENDERS_TESTS": tests, "GITHUB_RUN_ID": "77"},
                        clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                publish.android_comment(SHA, "42", call_api=api)
                error = None
            except PublishError as raised:
                error = raised.args[0]
        return calls, out.getvalue(), error

    def test_one_comment_with_the_outcome_count_and_links(self):
        calls, out, error = self.comment(tests="failed")
        self.assertIsNone(error)
        body = [payload for method, _, payload in calls if method == "POST"][0]["body"]
        self.assertIn(f"**Android renders** for {SHA}: tests failed, 2 images.", body)
        self.assertIn(f"https://github.com/May-Assistant/may-renders/blob/main/android-renders/{FOLDER}/README.md", body)
        self.assertIn(f"<!-- may-android-renders head={SHA} -->", body)
        self.assertEqual(out.strip(), "May Android renders linked on may-app #42")

    def test_an_unknown_outcome_word_is_not_carried_into_the_comment(self):
        calls, _, _ = self.comment(tests="passed; injected")
        body = [payload for method, _, payload in calls if method == "POST"][0]["body"]
        self.assertIn("tests unknown, 2 images", body)
        self.assertNotIn("injected", body)

    def test_a_moved_head_or_an_earlier_link_posts_nothing(self):
        calls, _, error = self.comment(head="b" * 40)
        self.assertEqual(error, "The pull request is no longer open at the rendered head; nothing was posted")
        self.assertFalse(any(method == "POST" for method, _, _ in calls))
        bot = {"type": "Bot", "login": "may-ci[bot]"}
        calls, out, _ = self.comment(comments=[{"body": f"<!-- may-android-renders head={SHA} -->", "user": bot}])
        self.assertFalse(any(method == "POST" for method, _, _ in calls))
        self.assertIn("already linked", out)
        # The iOS marker is not this link.
        calls, _, _ = self.comment(comments=[{"body": f"<!-- may-ios-renders head={SHA} -->", "user": bot}])
        self.assertTrue(any(method == "POST" for method, _, _ in calls))

    def test_nothing_sealed_publishes_nothing(self):
        shutil.rmtree(self.root / publish.TREE)
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_DECRYPT_KEY": "x"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.open_android(SHA)
        self.assertEqual(out.getvalue().strip(), "May Android renders: no sealed renders from this run attempt; nothing "
                                                 "will be published")


class WorkflowPinTests(unittest.TestCase):
    """android-renders.yml is pinned whole by static_check.android_renders_workflow_problems."""

    def setUp(self):
        self.text = (ROOT / WORKFLOW).read_text()

    def flagged(self, old, new, message):
        self.assertEqual(self.text.count(old), 1, old)
        problems = static_check.android_renders_workflow_problems(self.text.replace(old, new))
        self.assertTrue(any(message in p for p in problems), problems)

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.android_renders_workflow_problems(self.text), [])
        self.assertEqual(static_check.android_renders_print_problems(ROOT), [])

    def test_no_test_pattern_is_an_input(self):
        self.flagged("      pr_number:\n", "      pattern:\n        type: string\n      pr_number:\n",
                     "is not dispatched with exactly source_sha and pr_number")

    def test_secrets_stay_in_their_one_step(self):
        self.flagged("      - name: Run the render tests without any credential\n        id: run\n        env:\n",
                     "      - name: Run the render tests without any credential\n        id: run\n        env:\n"
                     "          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}\n",
                     "a secret or env entry other than")
        self.flagged("          MAY_RENDERS_TESTS: ${{ needs.render.outputs.tests }}\n", "",
                     "a secret or env entry other than")

    def test_only_the_encrypted_file_is_handed_over(self):
        self.flagged("${{ runner.temp }}/may-renders-sealed/renders.age", "${{ runner.temp }}/may-android-renders-out",
                     "hands over something other than exactly the one encrypted renders file")

    def test_runs_and_conditions_are_pinned(self):
        self.flagged('android_renders.py run --source-sha "$MAY_SOURCE_SHA"',
                     'android_renders.py run --source-sha "$MAY_SOURCE_SHA"; env', "runs something other than")
        self.flagged("        if: inputs.pr_number != ''\n", "", "a condition or a timeout other than its own")
        self.flagged("    timeout-minutes: 60\n", "    timeout-minutes: 600\n", "a condition or a timeout other than its own")

    def test_a_print_of_anything_else_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            shutil.copytree(ROOT / "tools", copy / "tools", ignore=shutil.ignore_patterns("__pycache__"))
            path = copy / "tools/may_release/android_renders.py"
            text = path.read_text()
            old = '    print(f"May Android renders: tests {word}; {int(count)} images")\n'
            self.assertEqual(text.count(old), 1)
            path.write_text(text.replace(old, old + "    print(names)\n"))
            self.assertTrue(static_check.android_renders_print_problems(copy))


if __name__ == "__main__":
    unittest.main()
