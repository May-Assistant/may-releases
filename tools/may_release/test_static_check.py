from pathlib import Path
import re
import shutil
import tempfile
import unittest

import static_check

ROOT = static_check.ROOT
STORE = ".github/workflows/store-release-draft.yml"
INERT = static_check.INERT
UPLOAD_LINE = "        if: " + static_check.UPLOAD_IF + "\n"
ANDROID_UPLOAD = "      - name: Upload update to the internal track only\n"
IOS_UPLOAD = "      - name: Upload verified IPA for internal TestFlight\n"


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

    def test_stage_1_is_exactly_android_dry_runs(self):
        store = (ROOT / STORE).read_text()
        for job, expected in static_check.STORE_JOB_IF.items():
            header = static_check.job_text(store, job).partition("    steps:\n")[0]
            with self.subTest(job=job):
                self.assertEqual([c.strip() for c in re.findall(r"(?m)^    if: (.+)$", header)],
                                 [expected] if expected else [])
        self.assertEqual(static_check.STORE_JOB_IF["admission"], None)
        self.assertNotIn(INERT, static_check.STORE_JOB_IF["android-build"] + static_check.STORE_JOB_IF["android-release"])
        self.assertIn(INERT, static_check.STORE_JOB_IF["ios-build"])
        self.assertIn(INERT, static_check.STORE_JOB_IF["ios-release"])
        self.assertEqual(static_check.UPLOAD_IF, "${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}")
        self.assertEqual(store.count(UPLOAD_LINE), 2)
        self.assertEqual(store.count("        if: " + static_check.REPORT_IF + "\n"), 2)

    def test_upload_guard_cannot_be_removed(self):
        # Removing the guard leaves stage 2's condition: the dry-run rule alone would accept it.
        for job, nth in (("android-release", 0), ("ios-release", 1)):
            with self.subTest(job=job):
                self.fresh()
                self.mutate_nth(UPLOAD_LINE, "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n", nth)
                found = static_check.problems(self.root)
                # gate.py's UPLOADS_HELD must follow, or admission would still refuse dry_run: false.
                self.assertEqual(sorted(found), sorted([
                    "store " + job + ": the upload is not held inert (stage 1)",
                    "store-release-draft.yml: the inert guard appears or is missing somewhere stage 1 does not expect",
                    "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held, so stage 1 "
                    "would not refuse dry_run: false at admission"]))

    def test_upload_guard_must_be_exactly_the_stage_1_form(self):
        for new in ("${{ " + INERT + " }}",
                    "${{ " + INERT + " && github.event.inputs.dry_run != 'true' }}",
                    "${{ " + INERT + " || github.event.inputs.dry_run == 'false' }}",
                    "${{ github.event.inputs.dry_run == 'false' && " + INERT + " }}",
                    "${{ " + INERT + " && github.event.inputs.dry_run == 'false' || always() }}"):
            with self.subTest(new=new):
                self.fresh()
                self.mutate_nth(UPLOAD_LINE, "        if: " + new + "\n", 0)
                self.assertFlagged("store android-release: ")
                self.assertTrue(any(fragment in problem for problem in static_check.problems(self.root)
                                    for fragment in ("the upload is not held inert", "a dry run would run the store upload")))

    def test_ios_jobs_stay_inert(self):
        guarded = "    if: ${{ " + INERT + " && inputs.platform == 'ios' }}\n"
        for job, nth in (("ios-build", 0), ("ios-release", 1)):
            for new, expected in (("    if: ${{ inputs.platform == 'ios' }}\n", "the job is not held inert (stage 1)"),
                                  ("", "the job is not held inert (stage 1)"),
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
                self.assertFlagged("store " + job + ": the job is held inert, but stage 1 activates it (stage mismatch")
                self.assertFlagged("the inert guard appears or is missing somewhere stage 1 does not expect")

    def test_full_revert_to_inert_is_flagged_not_accepted(self):
        # The pre-activation layout: all five jobs guarded, uploads only dry-run-conditional.
        self.mutate(STORE, "  admission:\n", "  admission:\n    if: ${{ " + INERT + " }}\n")
        self.mutate(STORE, "    if: ${{ inputs.platform == 'android' }}\n",
                    "    if: ${{ " + INERT + " && inputs.platform == 'android' }}\n", 2)
        self.mutate(STORE, UPLOAD_LINE, "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n", 2)
        found = static_check.problems(self.root)
        for job in ("admission", "android-build", "android-release"):
            self.assertTrue(any(p.startswith("store " + job + ": the job is held inert") for p in found), found)
        for job in ("android-release", "ios-release"):
            self.assertIn("store " + job + ": the upload is not held inert (stage 1)", found)

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
        self.assertFlagged("the inert guard appears or is missing somewhere stage 1 does not expect")
        self.fresh()
        # Named in a comment, it is prose and does not count.
        self.mutate(STORE, "  admission:\n", "  admission:\n    # was: if: ${{ " + INERT + " }}\n")
        self.assertEqual(static_check.problems(self.root), [])

    def test_dash_line_if_is_still_caught(self):
        # The upload's guard moved to the dash line is the same condition and passes.
        self.mutate_nth(UPLOAD_LINE, "", 0)
        self.mutate(STORE, ANDROID_UPLOAD, "      - if: " + static_check.UPLOAD_IF + "\n        " + ANDROID_UPLOAD[8:])
        self.assertEqual(static_check.problems(self.root), [])
        # Without the guard, on the dash line, it is not held inert.
        for job, header, nth in (("android-release", ANDROID_UPLOAD, 0), ("ios-release", IOS_UPLOAD, 1)):
            with self.subTest(job=job):
                self.fresh()
                self.mutate_nth(UPLOAD_LINE, "", nth)
                self.mutate(STORE, header, "      - if: " + static_check.DRY_RUN_UPLOAD_IF + "\n        " + header[8:])
                self.assertFlagged("store " + job + ": the upload is not held inert (stage 1)")
        # A second, dash-line if: beside the guarded one is refused whichever one GitHub would read.
        self.fresh()
        self.mutate(STORE, ANDROID_UPLOAD, "      - if: " + static_check.DRY_RUN_UPLOAD_IF + "\n        " + ANDROID_UPLOAD[8:])
        self.assertFlagged("store android-release: a step has more than one if:")
        self.fresh()
        self.mutate(STORE, UPLOAD_LINE, UPLOAD_LINE + "        if: " + static_check.DRY_RUN_UPLOAD_IF + "\n")
        self.assertFlagged("store android-release: a step has more than one if:")
        # A dash-line guard on another step is conditional and an unexpected guard.
        self.fresh()
        self.mutate(STORE, "      - name: Sign and verify Android App Bundle\n",
                    "      - if: ${{ " + INERT + " }}\n        name: Sign and verify Android App Bundle\n")
        self.assertFlagged("store android-release: a step other than the upload or dry-run report is conditional")
        self.assertFlagged("the inert guard appears or is missing somewhere stage 1 does not expect")

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

    def test_stage_1_refuses_dry_run_false_at_admission(self):
        cases = (
            (STORE, ' --dry-run "$MAY_DRY_RUN"\n', "\n",
             "store admission: does not run exactly the gate, unconditionally"),
            (STORE, "          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "store admission: does not pass the dispatch's dry_run to the gate"),
            (STORE, "          MAY_DRY_RUN: ${{ github.event.inputs.dry_run }}\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "          MAY_DRY_RUN: 'true'\n        run: python3 -I -S -B tools/may_release/gate.py --action \"$MAY_PLATFORM\"",
             "store admission: does not pass the dispatch's dry_run to the gate"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"android", "ios"}', 'UPLOADS_HELD = {"ios"}',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"android", "ios"}', 'UPLOADS_HELD = set(["android", "ios"])',
             "gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held"),
            ("tools/may_release/gate.py", 'UPLOADS_HELD = {"android", "ios"}', 'UPLOADS_HELD = {"android", "ios", "web"}',
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
        upload_if = UPLOAD_LINE
        held = "        if: ${{ " + INERT + " && "
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
        for job in ("android-release", "ios-release"):
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
