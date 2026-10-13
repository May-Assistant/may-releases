from pathlib import Path
import shutil
import tempfile
import unittest

import static_check

ROOT = static_check.ROOT


class StaticCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(ROOT / ".github", self.root / ".github")
        shutil.copytree(ROOT / "tools", self.root / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def fresh(self):
        self.tearDown()
        self.setUp()

    def mutate(self, relative, old, new, count=1):
        path = self.root / relative
        text = path.read_text()
        self.assertTrue(old in text, "mutation anchor not found")
        path.write_text(text.replace(old, new, count))

    def assertFlagged(self, fragment):
        found = static_check.problems(self.root)
        self.assertTrue(any(fragment in problem for problem in found), found)

    def test_repository_passes(self):
        self.assertEqual(static_check.problems(ROOT), [])
        self.assertEqual(static_check.problems(self.root), [])

    def test_every_workflow_must_pin_actions_by_sha(self):
        for workflow, old, new in (
                ("claude.yml", "anthropics/claude-code-action@1d6de8cb0c237e7c15e9e1bdf973826ebae490cc",
                 "anthropics/claude-code-action@v1"),
                ("claude-code-review.yml", "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
                 "actions/checkout@v4"),
                ("control-check.yml", "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
                 "actions/checkout@11d5960")):
            with self.subTest(workflow=workflow):
                self.fresh()
                self.mutate(".github/workflows/" + workflow, old, new)
                self.assertFlagged(workflow + ": action reference is not pinned")

    def test_new_workflow_is_covered(self):
        (self.root / ".github/workflows/extra.yaml").write_text(
            "on: push\njobs:\n  x:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: actions/setup-node@v4\n")
        self.assertFlagged("extra.yaml: action reference is not pinned")

    def test_deployments_permission_is_refused(self):
        self.mutate(".github/workflows/native-compile-draft.yml", "      actions: read\n",
                    "      actions: read\n      deployments: read\n")
        self.assertFlagged("requests deployments permission")

    def test_secrets_stay_in_their_own_step(self):
        store = ".github/workflows/store-release-draft.yml"
        moves = (
            ("android", "        run: python3 tools/may_release/store.py --platform android",
             "        env:\n          MAY_ANDROID_KEY_PASSWORD: ${{ secrets.MAY_ANDROID_KEY_PASSWORD }}\n"
             "        run: python3 tools/may_release/store.py --platform android"),
            ("android", "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n",
             "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n"
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"),
            ("android", "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n",
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
             "          MAY_ANDROID_KEYSTORE_BASE64: ${{ secrets.MAY_ANDROID_KEYSTORE_BASE64 }}\n"),
            ("ios", "        run: python3 tools/may_release/store.py --platform ios",
             "        env:\n          MAY_IOS_CERT_PASSWORD: ${{ secrets.MAY_IOS_CERT_PASSWORD }}\n"
             "        run: python3 tools/may_release/store.py --platform ios"),
            ("ios", "      PYTHONDONTWRITEBYTECODE: '1'\n",
             "      PYTHONDONTWRITEBYTECODE: '1'\n      MAY_ASC_KEY_BASE64: ${{ secrets.MAY_ASC_KEY_BASE64 }}\n"),
        )
        for job, old, new in moves:
            with self.subTest(job=job, new=new.strip()[:60]):
                self.fresh()
                self.mutate(store, old, new)
                found = static_check.problems(self.root)
                self.assertTrue(any(problem.startswith("store " + job) for problem in found), found)

    def test_combined_build_sign_step_is_refused(self):
        store = ".github/workflows/store-release-draft.yml"
        self.mutate(store, "      - name: Build unsigned Android App Bundle without secrets\n"
                           "        run: python3 tools/may_release/store.py --platform android"
                           " --target \"$MAY_TARGET\" --source-sha \"$MAY_SOURCE_SHA\" --build-number \"$MAY_BUILD_NUMBER\"\n", "")
        self.assertFlagged("store android: build, sign and upload steps are missing or out of order")

    def test_pip_must_be_hash_locked_wheels(self):
        store = ".github/workflows/store-release-draft.yml"
        for flag in ("--require-hashes ", "--only-binary=:all: "):
            with self.subTest(flag=flag):
                self.fresh()
                self.mutate(store, flag, "")
                self.assertFlagged("installs packages without hash-locked wheels")
        self.fresh()
        self.mutate("tools/may_release/requirements-play.txt", "rsa==4.9.1 \\\n    --hash=sha256:", "rsa>=4\n    # ")
        self.assertFlagged("requirement without a pinned version and hash")

    def test_printed_error_messages_must_be_literals(self):
        self.mutate("tools/may_release/play_upload.py",
                    'raise StoreError("Google Play returned a different version code")',
                    'raise StoreError(f"Google Play returned {uploaded}")')
        self.assertFlagged("play_upload.py")
        self.fresh()
        self.mutate("tools/may_release/gate.py", 'raise GateDenial("Protected environment metadata unavailable") from None',
                    'raise GateDenial("Protected environment metadata unavailable: " + str(error)) from None')
        self.assertFlagged("printed error message is not one string literal")

    def test_workflows_stay_inert(self):
        self.mutate(".github/workflows/store-release-draft.yml",
                    "if: ${{ github.event_name == 'may-store-disabled-pending-review' }}",
                    "if: ${{ github.event_name == 'workflow_dispatch' }}")
        self.assertFlagged("store-release-draft.yml is not inert")

    def test_protected_job_must_wait_for_admission(self):
        self.mutate(".github/workflows/store-release-draft.yml", "  android:\n    needs: admission\n", "  android:\n")
        self.assertFlagged("protected job android does not wait for admission")


if __name__ == "__main__":
    unittest.main()
