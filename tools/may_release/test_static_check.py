from pathlib import Path
import shutil
import tempfile
import unittest

import static_check

ROOT = static_check.ROOT
STORE = ".github/workflows/store-release-draft.yml"


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
        self.assertTrue(old in text, "mutation anchor not found: " + old[:60])
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

    def test_unverified_jdk_and_pip_are_refused(self):
        self.mutate(".github/workflows/control-check.yml", "      - run: |\n          python3 tools/may_release/toolchain.py jdk --export\n",
                    "      - uses: actions/setup-java@cf277c60eb25467037889841efdb72551f06f6c3 # v4\n"
                    "      - run: |\n          python3 tools/may_release/toolchain.py jdk --export\n")
        self.assertFlagged("setup-java does not verify its JDK")
        self.fresh()
        self.mutate(STORE, "          python3 tools/may_release/toolchain.py age\n",
                    "          python3 tools/may_release/toolchain.py age\n          python3 -m pip install requests\n")
        self.assertFlagged("installs Python packages")

    def test_secrets_context_only_by_name(self):
        for form in ("${{ secrets['MAY_PLAY_JSON'] }}", "${{ toJSON(secrets) }}", "${{ secrets[format('MAY_{0}', 'X')] }}"):
            with self.subTest(form=form):
                self.fresh()
                self.mutate(".github/workflows/claude.yml", "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", form)
                self.assertFlagged("claude.yml: secrets are referenced other than as secrets.NAME")
        self.fresh()
        self.mutate(".github/workflows/native-compile-draft.yml", "    timeout-minutes: 90\n",
                    "    timeout-minutes: 90\n    secrets: inherit\n")
        self.assertFlagged("native-compile-draft.yml: secrets are referenced other than as secrets.NAME")

    def test_checkouts_do_not_persist_credentials_and_marketplace_is_pinned(self):
        self.mutate(".github/workflows/claude.yml", "          persist-credentials: false\n", "")
        self.assertFlagged("claude.yml: a checkout persists its credentials")
        self.fresh()
        self.mutate(".github/workflows/claude-code-review.yml",
                    "plugin_marketplaces: ${{ github.workspace }}/.claude-marketplace/claude-code",
                    "plugin_marketplaces: 'https://github.com/anthropics/claude-code.git'")
        self.assertFlagged("claude-code-review.yml: plugin marketplace is a moving URL")

    def test_deployments_permission_is_refused(self):
        self.mutate(".github/workflows/native-compile-draft.yml", "      actions: read\n    outputs:",
                    "      actions: read\n      deployments: read\n    outputs:")
        self.assertFlagged("requests deployments permission")

    def test_secrets_stay_in_their_own_job_and_step(self):
        moves = (
            ("android-build", "        run: python3 tools/may_release/store.py --platform android",
             "        env:\n          MAY_ANDROID_KEY_PASSWORD: ${{ secrets.MAY_ANDROID_KEY_PASSWORD }}\n"
             "        run: python3 tools/may_release/store.py --platform android"),
            ("android-release", "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n",
             "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n"
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"),
            ("android-release", "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n",
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
             "          MAY_ARTIFACT_DECRYPT_KEY: ${{ secrets.MAY_ARTIFACT_DECRYPT_KEY }}\n"),
            ("ios-build", "        run: python3 tools/may_release/store.py --platform ios",
             "        env:\n          MAY_IOS_CERT_PASSWORD: ${{ secrets.MAY_IOS_CERT_PASSWORD }}\n"
             "        run: python3 tools/may_release/store.py --platform ios"),
            ("ios-release", "    timeout-minutes: 60\n",
             "    timeout-minutes: 60\n    env:\n      MAY_ASC_KEY_BASE64: ${{ secrets.MAY_ASC_KEY_BASE64 }}\n"),
        )
        for job, old, new in moves:
            with self.subTest(job=job, new=new.strip()[:60]):
                self.fresh()
                self.mutate(STORE, old, new)
                found = static_check.problems(self.root)
                self.assertTrue(any(problem.startswith("store " + job) for problem in found), found)

    def test_build_and_release_must_be_separate_jobs(self):
        # Signing moved back into the build job (same VM as third-party build code).
        self.mutate(STORE, "      - name: Encrypt unsigned bundle to the target's public key\n",
                    "      - name: Sign and verify Android App Bundle\n        env:\n"
                    "          MAY_ANDROID_KEY_PASSWORD: ${{ secrets.MAY_ANDROID_KEY_PASSWORD }}\n"
                    "        run: python3 tools/may_release/sign.py --platform android\n"
                    "      - name: Encrypt unsigned bundle to the target's public key\n")
        self.assertFlagged("store android-build: a step holds secrets it does not need")
        self.fresh()
        self.mutate(STORE, "    needs: [admission, android-build]\n", "    needs: admission\n")
        self.assertFlagged("store android-release: does not run after its own build job")
        self.fresh()
        self.mutate(STORE, "    environment: ${{ needs.admission.outputs.build_environment }}\n",
                    "    environment: ${{ needs.admission.outputs.environment }}\n")
        self.assertFlagged("environment is not the one admission checked")

    def test_release_job_runs_only_this_repositorys_python(self):
        self.mutate(STORE, "        run: python3 tools/may_release/sign.py --platform android",
                    "        run: ./gradlew signingReport && python3 tools/may_release/sign.py --platform android")
        self.assertFlagged("store android-release: runs a command that is not this repository's Python")
        self.fresh()
        self.mutate(STORE, "      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4\n"
                           "        with:\n          name: may-handoff-ios\n",
                    "      - uses: some/action@" + "a" * 40 + "\n"
                    "      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4\n"
                    "        with:\n          name: may-handoff-ios\n")
        self.assertFlagged("store ios-release: uses an action it does not need")

    def test_only_the_encrypted_handoff_is_uploaded(self):
        self.mutate(STORE, "          path: ${{ runner.temp }}/may-handoff/handoff.age\n",
                    "          path: ${{ runner.temp }}/may-release/unsigned.aab\n")
        self.assertFlagged("uploads something other than the encrypted handoff")

    def test_printed_error_messages_must_be_literals(self):
        cases = (
            ("tools/may_release/play_upload.py", 'raise StoreError("Google Play returned a different version code")',
             'raise StoreError(f"Google Play returned {uploaded}")'),
            ("tools/may_release/gate.py", 'raise GateDenial("Protected environment metadata unavailable") from None',
             'raise GateDenial("Protected environment metadata unavailable: " + str(error)) from None'),
            ("tools/may_release/ios_upload.py", 'verify.ipa_identity(ipa, APPLE_TEAM, app_id, build_number)',
             'raise verify.VerifyError(str(ipa))'),
            ("tools/may_release/handoff.py", 'raise StoreError("Build handoff contents are unexpected")',
             'raise HandoffError(str(names))'),
            ("tools/may_release/handoff.py", 'raise StoreError("Encrypted build handoff is unavailable")',
             'raise StoreError'),
        )
        for path, old, new in cases:
            with self.subTest(new=new):
                self.fresh()
                self.mutate(path, old, new)
                if "HandoffError" in new:
                    self.mutate(path, "FORMAT = ", "class HandoffError(StoreError):\n    pass\n\n\nFORMAT = ")
                for found in (static_check.message_problems(self.root), static_check.problems(self.root)):
                    self.assertTrue(any(Path(path).name in problem for problem in found), found)

    def test_workflows_stay_inert(self):
        self.mutate(STORE, "if: ${{ github.event_name == 'may-store-disabled-pending-review' }}",
                    "if: ${{ github.event_name == 'workflow_dispatch' }}")
        self.assertFlagged("store-release-draft.yml is not inert")

    def test_protected_job_must_wait_for_admission(self):
        self.mutate(STORE, "  android-build:\n    needs: admission\n", "  android-build:\n")
        self.assertFlagged("protected job android-build does not wait for admission")


if __name__ == "__main__":
    unittest.main()
