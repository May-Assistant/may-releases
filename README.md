# May release control

This **public** repository holds release-control workflows only. May app and design-system source remain private. No private source, assets, signing material, provider response, native build output, or credential belongs in a commit, public log, or artifact here (the one artifact the store workflow publishes is age-encrypted; see below).

## Store records

The separate May Playground iOS app is registered in App Store Connect as `6822072535` with bundle `io.github.mayassistant.may.playground`, active App Store profile `T5Y2C9URKX`, and empty internal TestFlight group `8a709a78-c828-4e7b-818c-b6ce62998ec8`. The real May iOS app is `6821782401` with bundle `io.github.mayassistant.may`. The corresponding Play apps are separate records: May `4974275831988601568` and May Playground `4972137188420176977`.

**Each Play app's first upload is made by hand in Play Console; this repository cannot make it.** Google's Play Developer API guide ([Edits](https://developers.google.com/android-publisher/edits)) says: "You can only use this API to make changes to an existing app (that has at least one APK uploaded); thus, you will have to upload at least one APK through the Play Console before you can use this API." Play also accepts only `status: draft` releases through the API until an app's first release has been rolled out. May Playground's first release (versionCode 1) was uploaded by hand on 13 Oct, which is why its later uploads work. **May production's first release is versionCode 2 (0.1.0)**: the local lane signed `May-production-vc2-1b73881` with the `may-upload` keystore on Samuel's machine, and Samuel uploaded it by hand to the internal track on 14 Oct (evidence: may-app `docs/evidence/m1-production-install-2026-10-14/`). So stage 3's workflow uploads of May production start at **versionCode 3** ([May production after its first release](#may-production-after-its-first-release)).

This repository only performs later updates. It refuses an app whose tracks show no rolled-out release, before uploading anything, with the fixed message `First release must be uploaded in Play Console: no track of this app has a rolled-out release, and Play's API cannot make an app's first release. …`. If Play refuses the edit itself, the log names the request (`edit insert`) and the HTTP status, and says that an app with no Console upload is the cause to check first.

## Current state

**Activation status: stage 3, Android uploads to the Play internal track (May and May Playground), plus sign-only for May production.** The [store-release workflow](.github/workflows/store-release-draft.yml) can run an Android **dry run**, or, with `dry_run: false`, upload **May** or **May Playground** to its Play **`internal`** track, or, for May production with `sign_only: true` and `dry_run: true`, sign and verify in a separate `android-sign-only` job (no Play secret) and then publish the signed bundle, encrypted, for a manual Play Console upload. Promoting a build from internal to production stays a manual step in Play Console (Samuel's decision, 14 Oct). A job or step is held **inert** by a condition that requires an event name the workflow never registers (`may-store-disabled-pending-review`), so it is always skipped. In stage 3:

| Part of the store workflow | Stage 3 |
| --- | --- |
| `admission` | active (runs on every dispatch) |
| `android-build`, `android-release` | active when `platform` is `android` (`android-release` only when `sign_only` is not `true`) |
| Android upload step (`play_upload.py`) | **active** when `dry_run` is `false`; admission admits that for target `playground` or `production` (track `internal` only) |
| Android loud-failure check (`confirm_upload.py`) | runs whenever `dry_run` is `false`; fails the job unless the upload step succeeded |
| `android-sign-only` (`sign_only.py`, the publish step, `confirm_upload.py --sign-only`) | runs instead of `android-release` when `sign_only` is `true`; admission admits that only for May production Android with `dry_run: true`, and only once Samuel's Console-upload key is committed (it is a placeholder today). The job references no Play secret or variable |
| `ios-build`, `ios-release` (and the iOS upload step) | **inert**: `tools/release/ios.sh` does not exist yet. The iOS loud-failure check is in place but inert with its job |
| Dry-run report (`dry_run.py`) | runs whenever `dry_run` is not `false`; for Android it now also rechecks the build-number floor, the signer and the Play account |

`static_check.py` pins exactly this layout (`STAGE`, `STORE_JOB_IF`, `UPLOAD_IF`, `CONFIRM_IF`, `PLAY_UPLOAD_TRACKS`, and the three sign-only steps, `PUBLISH_SIGNED_STEP` exactly), and requires `gate.py`'s `UPLOADS_HELD` to name exactly the platforms whose upload step is held (stage 3: `{"ios"}`). A guard that disappears from an iOS job or the iOS upload step fails the check. So does a guard that reappears on `admission`, an Android job or the Android upload step: that is flagged as a stage mismatch rather than accepted, so going back to an earlier stage, or to fully inert, is a deliberate, reviewed change to those constants too.

The [pull-request compile workflow](.github/workflows/native-compile-pr.yml) compiles iOS May Playground at the head of an open may-app pull request against `main`, in its own environment `may-native-compile-pr`. It needs no approval (Samuel, 17 Oct), and it is not usable until Samuel creates that environment and its secrets (see [iOS compile of a pull request head](#ios-compile-of-a-pull-request-head)).

The [design-system Swift compile workflow](.github/workflows/ds-swift-compile.yml) compiles may-design-system's Swift package at the head of an open may-design-system pull request and posts its compile errors on that pull request, never in this log, in its own environment `may-ds-renders`. It needs no approval, and it is not usable until Samuel creates that environment's secrets (see [Design-system Swift compile of a pull request head](#design-system-swift-compile-of-a-pull-request-head)).

The [may-app review workflow](.github/workflows/may-app-review.yml) runs Claude's code review of an open may-app pull request and posts it on that pull request, because may-app's own Actions are blocked. It runs in its own environment `may-claude-review`, and nothing from may-app reaches this public repository's log. It is not usable until Samuel creates that environment and its secrets (see [Claude code review of a may-app pull request](#claude-code-review-of-a-may-app-pull-request)).

The [native compile workflow](.github/workflows/native-compile-draft.yml) is **active for iOS only**: each build, the coordinator dispatches it to check that **May Playground** compiles. It runs on `macos-15` with the pinned Xcode 26.3 and XcodeGen 2.46.0, makes a simulator Release build with `CODE_SIGNING_ALLOWED=NO`, and has no signing, store call or artifact (see [iOS compile each build](#ios-compile-each-build)). The pull-request check runs only dependency-free control tests and static checks with a read-only token.

The admission gate **denies any store run whose target's artifact public key is a placeholder or malformed**. As of 2026-10-13 all four real public keys are committed (generated by Dots on Samuel's Mac; see [the key-pair steps](#artifact-key-pair-setup)), so that check passes.

Confirmed from the repository's Actions secrets page on 2026-10-13. All secrets below are environment-scoped. The only repository secret is `CLAUDE_CODE_OAUTH_TOKEN`, and no organization secrets are available to this repository.

| Environment | Secrets present |
| --- | --- |
| `may-native-compile` | `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY`; optional `MAY_POSTHOG_ANDROID_KEY` (and variable `MAY_POSTHOG_HOST`) |
| `may-android-internal`, `may-android-playground-internal` | `MAY_ANDROID_KEYSTORE_BASE64`, `MAY_ANDROID_KEY_PASSWORD`, `MAY_ANDROID_STORE_PASSWORD`, `MAY_PLAY_JSON`, `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` |
| `may-ios-internal`, `may-ios-playground-internal` | `MAY_IOS_CERT_BASE64`, `MAY_IOS_CERT_PASSWORD`, `MAY_IOS_PROFILE_BASE64`, `MAY_ASC_KEY_BASE64`, `MAY_ASC_ISSUER_ID`, `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` |

Since that check, `MAY_PLAY_JSON` was deleted from `may-android-playground-internal` for the stage-1 dry run and re-added before stage 2's uploads (May Playground build 5 uploaded through the workflow; see [Stage 2](#stage-2-android-uploads-to-the-internal-track-done)).

Still needed:

- Confirmation that `MAY_ARTIFACT_DECRYPT_KEY` has been entered in each store environment (being done separately; the private halves never leave Samuel's Mac otherwise).
- Confirmation that the environment **variables** `MAY_ANDROID_KEY_ALIAS`, `MAY_ANDROID_CERT_SHA256`, `MAY_PLAY_ACCOUNT` (Android) and `MAY_ASC_KEY_ID=23VX669FGY` (iOS) are set. The secrets page does not show variables. For `may-android-internal` they were read from Samuel's screenshot of 13 Oct as `MAY_ANDROID_KEY_ALIAS=may-upload`, `MAY_ANDROID_CERT_SHA256=9E2FE9C8E70421FCBFBBFB4EA8…` and `MAY_PLAY_ACCOUNT=may-play-upload@…`; stage 3's code requires exactly the values in `store.ANDROID_UPLOAD_KEYS`, so the production dry run confirms them.

The store environments' copies of `MAY_APP_READ_KEY` and `MAY_DS_READ_KEY` are no longer referenced by the store workflow, because the build job fetches source in `may-native-compile`. They may be deleted.

## Environments and protection

| Target | Build job environment | Release job environment | Store app |
| --- | --- | --- | --- |
| May iOS | `may-native-compile` | `may-ios-internal` | ASC `6821782401` |
| May Playground iOS | `may-native-compile` | `may-ios-playground-internal` | ASC `6822072535` |
| May Android | `may-native-compile` | `may-android-internal` | Play `4974275831988601568` |
| May Playground Android | `may-native-compile` | `may-android-playground-internal` | Play `4972137188420176977` |

Every environment needs:

- a main-only custom deployment branch policy;
- Samuel (GitHub ID `58840187`) as the **sole** required reviewer;
- administrator bypass disabled.

A no-secret admission job checks both environments a run will use before any secret becomes available, and each protected job rechecks its own after review. Each store run therefore needs **two approvals**: the build environment, then the store environment.

**Environments are never auto-created.** A job's `environment:` key would create a missing environment with no protection. The admission job runs first and denies on the API's 404, so create each environment by hand before any dispatch.

**Self-review: current policy.** *Prevent self-review* is **off**, by Samuel's decision for internal testers only, so Samuel may dispatch and approve his own run. The gate denies only the deadlock case: self-review prevented while the sole reviewer dispatched. That policy is acceptable because nothing in this repository can target anything but internal testing. Play uploads go only to the `internal` track (`play_upload.TRACK`, and `store.PLAY_UPLOAD_TRACKS`, which in stage 3 maps `playground` and `production`, both to `internal`), TestFlight exports are `testFlightInternalTestingOnly`, there is no track input, and `static_check.py` refuses a production track in the workflow, any track but `internal` in either constant, and a second track write in `play_upload.py`. `play_upload.py` also refuses, before sending it, any track write but one `completed` release of exactly the new build on `internal` (no rollout fraction, staged, halted or draft release, or promotion of an existing build), and any commit but `ERROR_IF_IN_REVIEW` (no `changesNotSentForReview`). Stage 3 lets the real May app (`production`) upload to **its internal track**, by Samuel's decision of 14 Oct, after a passing production dry run. Its internal build can still be promoted to production by hand in Play Console, where no control here runs: that stays a manual Console step, and the [non-internal release rules](#before-any-non-internal-release-required) below apply before any change in this repository targets anything but internal testing.

### Before any non-internal release (required)

Before the first production or public-beta release:

1. Turn **Prevent self-review on** in every environment involved.
2. Add a **second required reviewer**.
3. Update `gate.py` (`OWNER_ID` sole-reviewer rule and self-review handling) and its tests in the same reviewed change that adds any non-internal track.
4. Leave the new target out of `INTERNAL_TARGETS` in `store.py`. Anything not listed there is non-internal, so its iOS and Android design-system pins must match.

The current code must not be extended to a non-internal track without these.

**No repository- or organization-level secrets with the same names.** GitHub falls back from an environment secret to a repository or organization secret of the same name. That would hand a step a value without the environment's protection, and the gate cannot see it. Confirmed absent on 2026-10-13; keep it that way.

## Release jobs and what the isolation does and does not give

Each store run is **two jobs on separate fresh VMs**:

1. **`*-build`** (`may-native-compile`, read-only source keys and, for Android, the PostHog crash-report key only):
   - fetches the exact private source and the design system pinned for the platform being built (`prepare.py`; see [Design-system pins](#design-system-pins));
   - runs the private build script for an **unsigned** product (`store.py`). This step runs third-party code: Gradle and its plugins, SwiftPM, and Xcode build phases. On Android it holds one secret, `MAY_POSTHOG_ANDROID_KEY`, which Gradle compiles into the app (it ships inside the APK, as PostHog's project keys are meant to); without it the app is built with no key and sends no crash reports, and the step says so in one fixed line;
   - encrypts the product, with a manifest that also records both design-system pins, using [age](https://age-encryption.org) to the target's public key committed in `tools/may_release/artifact_keys/` (`handoff.py seal`);
   - uploads only the encrypted file, as a one-day artifact.
2. **`*-release`**, or **`android-sign-only`** for a [sign-only](#sign-only) dispatch (the store environment; `needs: [admission, *-build]`):
   - on a fresh VM, runs no build toolchain. Its only actions are SHA-pinned `actions/checkout` and `actions/download-artifact`, used before any secret is present. Every command is this repository's Python, run as `python3 -I -S -B` (isolated: neither the script folder nor site-packages comes before the standard library, so a planted file cannot shadow a stdlib import), which calls the pinned, checksum-verified Temurin `jarsigner`, the pinned `age`, the system `openssl`, and Apple's `/usr/bin` tools.
   - `handoff.py open` decrypts with `MAY_ARTIFACT_DECRYPT_KEY` and checks that key against the committed public key. It refuses the payload unless its manifest names exactly this repository, run ID, **run attempt**, platform, target, source SHA and build number. The artifact name also carries the attempt.
   - It then validates every tar member before writing anything, and extracts them one by one. It does not use `tarfile`'s extraction filters.
     - Only regular files and directories are allowed, plus, for iOS, relative symlinks.
     - Each symlink, followed through any chain of other links, must land on an existing entry inside its own bundle (`.app`, `.framework`, …) of the final product.
     - Refused: absolute, escaping and dangling links, hard links, devices, fifos, `..`, absolute or non-normal paths, and anything written through a link.
     - The macOS runner's APFS ignores case and Unicode normalization, so names are also compared the way it sees them (`NFC` + `casefold`). Two members, or a link-target component and a member, that fold to the same name but are not byte-identical are refused.
     - Before every file write or `mkdir` (one level at a time), the parent must resolve on the real filesystem to exactly the folder the member names, so a symlink anywhere in the chain, under any case or normalization alias, is refused.
     - Every link is re-resolved in its final location before any signing secret is on the VM.
   - `integrity.py` runs before each secret step (decrypt, sign, upload). It refuses unless `git status --ignored` is empty and every file under `tools/may_release/` matches the checked-out commit's blob, mode and file list. It runs git with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `-c core.fsmonitor=false` and only `PATH` in its environment, so no system, global or `HOME`-based setting (hooks, fsmonitor, excludes) applies.
   - Because the attempt is bound, re-running a store run means **re-running all jobs**. Re-running only the release job finds no artifact for the new attempt and stops with "Re-run all jobs: this release only accepts a build from the same run attempt".
   - `sign.py` signs and verifies, then `play_upload.py` or `ios_upload.py` uploads, unless this is a [dry run](#dry-run). In [stage 3](#current-state) only the Android upload step can run (May and May Playground), or, for May production, the [sign-only](#sign-only) job instead, which publishes the encrypted signed bundle and holds no Play secret.
   - Whenever `dry_run` is `false`, `confirm_upload.py` then fails the job unless the upload step's outcome is `success` and it left its upload record (see [Fail loudly](#fail-loudly)).

Within a job, each step receives only its own secrets, and each script refuses to run if it finds another step's secret. Tools get an allowlisted environment. `static_check.py` enforces:

- the job and step layout and the step-to-secret map;
- the allowed actions and commands in release jobs;
- that only the `.age` file is uploaded;
- the activation stage: exactly which jobs and steps carry the inert guard, that the active Android jobs keep their platform condition, and that the guard appears nowhere else (see [Current state](#current-state));
- the loud-failure check: right after each upload step, exactly one `confirm_upload.py` step for that platform, under exactly `${{ !cancelled() && github.event.inputs.dry_run == 'false' }}` (never the guard), reading only that upload step's `outcome` by its `id`;
- the reachable tracks: `store.PLAY_UPLOAD_TRACKS` is exactly `{"playground": "internal"}`, `play_upload.TRACK` is `internal`, and `play_upload.py` writes no other track;
- the `dry_run` input (a required boolean defaulting to `true`), that only the store upload, the loud-failure check and the dry-run report steps are conditional (an `if:` on a step's dash line counts, and a step or job may have only one `if:`), and their exact conditions;
- that `admission` runs exactly `gate.py`, passing the dispatch's `dry_run` (`--dry-run`), with no conditional step and no `continue-on-error`, and that only the handoff download may continue on error;
- that `gate.py`'s `UPLOADS_HELD` is exactly the set of platforms whose upload step carries the inert guard;
- that an unconditional `integrity.py` step comes right before each secret step;
- that each build job prepares the design system for its own platform and target;
- that no `setup-java` or `pip install` appears in any workflow.

**What this gives, and what it does not:**

- **Separate jobs are the real boundary.** Code from the build cannot reach the signing keystore, certificate, Play key or App Store Connect key, because it never runs on the VM that holds them. Steps *within* one job are not a security boundary: same user, same VM, shared files. That is why signing moved to its own job.
- **The build job is trusted only for what it builds.** Third-party build code could make the build produce a malicious product, or tamper with the encryption step. Separation cannot prevent that. Signing re-verifies what it can (signature coverage and signer for the AAB; profile, bundle ID and build number for the IPA). It cannot vouch for the code inside.
- **age gives confidentiality and integrity, not sender authentication, and is not proof of reviewed build provenance.** Anyone holding the public key can encrypt. The manifest bound to the run and attempt refuses another run's, attempt's or target's artifact, and a tampered file fails decryption. The job split protects the keys, not the integrity of the product.
- **age itself is installed in the build job before the third-party build runs.** A compromised build could replace it and leave the handoff unencrypted. The release side's authenticated decryption rejects that, so the risk is to confidentiality only, and a compromised build can break the product's confidentiality anyway.
- The read-only source keys exist in the build job, in the fetch step only, and stay readable to the build within the limits of a same-VM step.

## Design-system pins

`may-app` pins the May design system twice: `ios/project.yml` (`MayDesignSystem` `url` + `revision`) and `android/design-system.lock` (`repository=` + `revision=`). They can drift, and on may-app main today they differ.

Samuel's decision (2026-10-14): relax the design-system pin match for internal builds only; production still requires it; drift recorded on every build. Follow-up decision (fail closed): only the Playground targets count as internal. A build of the real May app (`production`) goes to Play `internal` or internal TestFlight too, but can be promoted to production by hand in Play Console or App Store Connect, where no control in this repository runs, so its pins must match.

- **Every build** requires both pins to be well-formed (40-hex) and trusted (`https://github.com/May-Assistant/may-design-system`), including the pin that is not built. Drift is only meaningful between two exact revisions of the one trusted repository. A pin that points elsewhere, or at a branch or path, is a different dependency for the same commit, which will also be built for the other platform.
- **Internal targets** (`INTERNAL_TARGETS` in `store.py`, the single list; today only iOS and Android `playground`): `prepare.py --platform P --target T` fetches only P's pin and refuses unless that exact revision is what is checked out. The other platform's pin may differ.
- **Any other target**, including iOS and Android `production` and any future target, which is non-internal unless it is added to that list on purpose: the two pins must match. The iOS compile builds the internal Playground target (`prepare.py --platform ios --target playground`), so it uses iOS's own pin and compiles only the `MayPlayground` scheme. The production `May` scheme is not compiled until the two pins match.
- **Record.** `prepare.py` writes both pins, with the platform, target and source SHA, to the build job's private folder. `handoff.py seal` checks that record is for this build and that the design-system checkout is still exactly the built platform's pin with no tracked change, then puts both pins into the encrypted manifest (`design_system`). `handoff.py open` re-applies the same rule for its own target. The public log says only whether they **match** or **differ**; the revisions stay in the encrypted handoff, because the no-commit-metadata rule covers the private design system too.

## Dry run

`store-release-draft.yml` has a `dry_run` boolean input, **default `true`**, so a real upload is always an explicit choice at dispatch.

- A dry run still builds, seals, opens, signs and verifies.
- It skips both store upload steps (`play_upload.py`, `ios_upload.py`), so the upload credentials are never exposed to any step. (The runner still receives a secret the job references if it is set in the environment; see [Prerequisites that remain](#prerequisites-that-remain).)
- `dry_run.py` reports what would have been uploaded: package or bundle ID, versionCode or build number, track, and the signed artifact's SHA-256. For iOS, it re-verifies the IPA first.
- Upload steps run only when `github.event.inputs.dry_run == 'false'`. That context holds the boolean as a string, so a missing value never uploads. The comparison ignores case, so any casing of `false` passes it.
- Each upload script also refuses unless `MAY_DRY_RUN` is exactly `false`.
- **In stage 3 the iOS upload step is still held inert.** Admission (`gate.py --dry-run`) refuses `dry_run: false` for iOS (`UPLOADS_HELD`) and for any Android target `store.PLAY_UPLOAD_TRACKS` does not map to `internal`, before any approval, build or secret, rather than finishing green without uploading.
- **What an Android dry run proves.** It runs everything except the Play client: admission and both environments' protection, the build-number floor, the build, seal and open, signing with the target's own key (`sign.py` refuses another target's alias or certificate pin), verification, and then `dry_run.py`, which rechecks, as `play_upload.py` would before any request, the floor, that `MAY_PLAY_ACCOUNT` is the target's own Play account, that the signed bundle's sole signer is the target's pinned upload certificate, and that the bundle's own manifest names the target's package at the requested versionCode. The package and versionCode it prints are the ones read from the signed bundle. It does not contact Play: the token, edit, track read, bundle upload, track update and commit run only in a real upload. So a dry run cannot see whether the app has a first release; a real run refuses that case clearly (see [Store records](#store-records)).

### Sign-only

`sign_only` is a second boolean dispatch input, **default `false`**, so it is always an explicit choice. It produces a May production bundle signed in CI, for a build that has to be uploaded in Play Console by hand, without the `may-upload` keystore touching any machine but the protected runner. May production's first release (vc2) was signed on Samuel's machine before sign-only existed; normal production builds now upload through the workflow, so sign-only is for the exceptions. Nothing limits it to one use.

- **Admission** admits `sign_only: true` only for `platform: android`, `target: production`, with `dry_run: true`, and only once Samuel's Console-upload public key is committed (`tools/may_release/artifact_keys/may-android-console-upload.age.pub`; a placeholder today, so admission denies it until then) and is not any environment's handoff key. Everything else is denied before any approval, build or secret.
- **Its own job, the same approvals.** A sign-only dispatch runs `android-sign-only` **instead of** `android-release` (each job's `if:` names `sign_only`). It uses the same environment, so it needs Samuel's approval for `may-native-compile` and then for `may-android-internal`, and it decrypts, signs (with `may-upload`, checked against the committed pin) and verifies exactly as `android-release` does. **The job references no Play secret or variable**, so `MAY_PLAY_JSON` never reaches its runner: GitHub sends a job's referenced secrets to the runner even for a step that is skipped, which is why sign-only is not a step of `android-release`. `static_check.py` pins that.
- **What it checks and publishes.** `sign_only.py` (no secret, no network) rechecks the signer, then reads the signed bundle's own manifest (`base/manifest/AndroidManifest.xml`, aapt2's protobuf `XmlNode`, decoded by `verify.py` without any dependency). It refuses unless the package is `io.github.mayassistant.may` and `android:versionCode` is the requested build number, with no `versionCodeMajor`. A bundle uploaded by hand gets none of `play_upload.py`'s checks, so the build job's own check is not trusted for it. It then encrypts the bundle with age to Samuel's key plus a throwaway check key, decrypts it with the check key to prove it is byte-for-byte the signed bundle, and deletes the check key. The job publishes `may-signed-android-<attempt>` for **3 days**, holding exactly three files: `signed.aab.age`, `signed.aab.sha256` and `sign-only-record.json`. The record holds the mode, the package and versionCode as read from the bundle, the signed SHA-256 and the encrypted file's SHA-256. No keystore, Play credential or decrypted handoff is in it. It is encrypted because this repository is public, and so are its Actions artifacts.
- **Loud failure.** `Fail unless the signed bundle was published, not uploaded` (`confirm_upload.py --sign-only`) runs unless the run was cancelled, even after an earlier failure. It fails the job unless the publish step succeeded, the folder holds exactly those three files, and the record matches the signed bundle still on the runner and the encrypted file that was published. It reports **"Signed, not uploaded"**, never an upload; the upload check, for its part, refuses a run that published a sign-only bundle.

### Fail loudly

A dispatch with `dry_run: false` must never end green without uploading.

- Right after each upload step, `Fail unless the requested store upload succeeded` runs whenever `dry_run` is `false` and the run was not cancelled, even after an earlier failure. It runs `confirm_upload.py`, which holds no secret.
- It fails the job unless the upload step's `outcome` (`steps.play_upload.outcome`, `steps.testflight_upload.outcome`) is exactly `success`. A skipped upload (for example one still held inert), a failed one, or a missing one fails: `May upload check failed: dry_run was false, but the store upload step did not succeed (it failed, was skipped or was cancelled); nothing was confirmed uploaded`.
- It also requires the upload record the upload script writes after success (`may-release/upload-record.json`) to match this platform, app, build number, track and the signed artifact's SHA-256, and, for Android, to name the committed Play edit. It then prints the upload report.
- **Report.** A successful upload prints, and the check prints again: package (or bundle), versionCode (or build number), track, signed SHA-256, and for Android the committed Play edit ID. The edit ID is the only provider-supplied value printed. It is checked against `[A-Za-z0-9_.-]{1,200}` and is no longer usable once committed. A dry run still prints its own report.
- The iOS job has the same check, without the guard. It is inert only because the job is, so it is already in place when the iOS guard comes off.
- A [sign-only](#sign-only) run has its own check after its publish step, which confirms "signed, not uploaded" and fails the job unless the encrypted bundle and its matching record were published.

## Artifact key-pair setup

This is done by the setup lane, not by Claude. It was done on 2026-10-13; the committed public keys are:

| Environment | Committed public key (`tools/may_release/artifact_keys/<environment>.age.pub`) |
| --- | --- |
| `may-ios-internal` | `age1rxygmrkqmgaweevxdwpgpwwlgvf9haeulg4qwvv03mv7ml4rhqxsjsxfwl` |
| `may-ios-playground-internal` | `age1q8mp85cgc9w5uyvjqy57y0r8dm4d5s88x7q24fmpel8vsw777gtsa8gxrf` |
| `may-android-internal` | `age1kpcl62lf4hssansdlcu5zwde0esfwazvg3uuwddn4rru2zlrcceskyd7vg` |
| `may-android-playground-internal` | `age1cv26vadk855hhzs6fljc6wvh3t96r2kklkg8uul62hwfjj5u8v9sex4n5j` |

Each one was checked with the pinned age v1.3.1 binary (encrypting a test payload succeeds, so each bech32 string is well-formed), and they are distinct. To rotate or replace a key, repeat these steps for each of `may-ios-internal`, `may-ios-playground-internal`, `may-android-internal` and `may-android-playground-internal`, with a **distinct** key pair per environment. Run on a trusted machine:

1. Install age (`brew install age`, or the release binary verified with the Sigsum proof as in age's `SIGSUM.md`). Homebrew currently installs **v1.3.2**. That is fine for `age-keygen`, because the X25519 recipient and identity format is stable across these versions. CI pins **v1.3.1** for encrypting and decrypting.
2. Generate the key pair. `age-keygen` prints `Public key: age1…` on stderr:

   ```
   umask 077
   age-keygen -o ENV.agekey
   ```

3. Store the whole file as the environment secret. The comment lines are accepted. Paste nowhere else.

   ```
   gh secret set MAY_ARTIFACT_DECRYPT_KEY --repo May-Assistant/may-releases --env ENV < ENV.agekey
   ```

4. Send **only the public key** (`age-keygen -y ENV.agekey`, a single `age1…` line) for a reviewed PR. That PR replaces the contents of `tools/may_release/artifact_keys/ENV.age.pub` with exactly that line.
5. Delete `ENV.agekey`. No backup is needed: handoffs expire after one day, and a lost key is replaced by repeating these steps.

The gate denies a target whose committed key is missing, a placeholder or malformed. `handoff.py open` refuses a decrypt key that does not derive the committed public key.

### Samuel's Console-upload key (sign-only)

[Sign-only](#sign-only) encrypts May production's signed bundle to one more committed public key, `tools/may_release/artifact_keys/may-android-console-upload.age.pub`, whose private half **only Samuel keeps, on his Mac**. It is not an environment secret: nothing in CI ever decrypts with it. It must be a new key pair: admission and `sign_only.py` refuse a Console key that equals any environment's handoff key, because those private halves are CI secrets. It is a placeholder today, and admission denies `sign_only: true` until it is committed. On Samuel's Mac:

```
mkdir -p ~/.may && chmod 700 ~/.may
umask 077
age-keygen -o ~/.may/console-upload.agekey     # keep this file; it opens each sign-only artifact
age-keygen -y ~/.may/console-upload.agekey     # the one age1… line for a reviewed PR
```

A reviewed PR replaces the placeholder with exactly that `age1…` line. Unlike the environment keys, keep the private file: it is needed to decrypt each sign-only artifact.

## Secrets and variables per environment

- **`may-native-compile`:** `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` (read-only deploy keys for `may-app` and `may-design-system`).
- **`may-native-compile-pr`** (proposed; [compile of a pull request head](#ios-compile-of-a-pull-request-head); no required reviewer, main only): `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` (dedicated deploy keys, rotated when someone leaves may-app) and `MAY_APP_PR_READ_TOKEN` (may-app pull requests: read, short-lived). Each is bound in exactly one step.
  - Optional secret `MAY_POSTHOG_ANDROID_KEY`: PostHog's project key for Android crash reports (`phc_` then letters, digits, `_` and `-`). Only the Android build step receives it; every other step refuses it (`store.SECRET_NAMES`). This one environment builds both May and May Playground, so both apps report to the same PostHog project.
  - Optional variable `MAY_POSTHOG_HOST`: `https://eu.i.posthog.com` for a project on PostHog's EU cloud; unset means `https://us.i.posthog.com`. `store.py` refuses any other host, and the build step's public line names the host reports will go to.
  - Who sets them: only Samuel, and only in `may-native-compile`. GitHub resolves a variable or secret from the environment first, then the repository, then the organization, so a repository- or organization-level `MAY_POSTHOG_ANDROID_KEY` or `MAY_POSTHOG_HOST` would otherwise count. Don't create either at those levels. The host allowlist limits what a stray value can do, but a stray key would still send crash reports to another PostHog project.
- **`may-claude-review`** (proposed; [Claude code review of a may-app pull request](#claude-code-review-of-a-may-app-pull-request); Samuel the sole required reviewer, main only): `MAY_APP_REVIEW_TOKEN` (fine-grained, may-app only, expiring; Contents read, Pull requests read and write) in the fetch and review steps, and `MAY_REVIEW_CLAUDE_TOKEN` (a Claude Code OAuth token) in the review step alone.
- **iOS store environments:**
  - Secrets: `MAY_IOS_CERT_BASE64` and `MAY_IOS_CERT_PASSWORD` (the approved existing distribution certificate), `MAY_IOS_PROFILE_BASE64` (that target's App Store profile), `MAY_ASC_KEY_BASE64`, `MAY_ASC_ISSUER_ID` and `MAY_ARTIFACT_DECRYPT_KEY`.
  - Variable: `MAY_ASC_KEY_ID=23VX669FGY`.
  - The code fixes the Apple team, bundle IDs and certificate fingerprint.
- **Android store environments:**
  - Secrets: `MAY_ANDROID_KEYSTORE_BASE64`, `MAY_ANDROID_STORE_PASSWORD`, `MAY_ANDROID_KEY_PASSWORD`, `MAY_PLAY_JSON` and `MAY_ARTIFACT_DECRYPT_KEY`.
  - Variables: `MAY_ANDROID_KEY_ALIAS`, `MAY_ANDROID_CERT_SHA256` (the upload certificate's SHA-256) and `MAY_PLAY_ACCOUNT`.
  - The Play service account belongs to project `may-assistant-app` and has only the selected app's view and testing-release grants, with no Rem, production or account-wide access. The Rem uploader `rem-play-upload@rem-claw.iam.gserviceaccount.com` is not a May credential.
  - **Each target's values are fixed in code** (`store.ANDROID_UPLOAD_KEYS`), and every step refuses another target's: `may-android-internal` must hold alias `may-upload`, the certificate `9E2FE9C8…1E899815` (the full pinned may-upload SHA-256) and the account `may-play-upload@may-assistant-app.iam.gserviceaccount.com`; `may-android-playground-internal` must hold alias `may-playground-upload`, a certificate starting `DB087802F802F2A1` (the recorded prefix of that distinct key; the environment variable still pins the full value, which the signer must equal exactly) and the account `may-playground-upload@may-assistant-app.iam.gserviceaccount.com`. Replace the Playground prefix with its full SHA-256 once it is recorded in a reviewed file.

## Verification and upload

- **Android:**
  - `jarsigner -verify` checks every entry digest.
  - `verify.py` then requires exactly one signer, with no `SIG-*` or second block, and requires the manifest to cover every entry. The signer certificate's SHA-256 must equal `MAY_ANDROID_CERT_SHA256`.
  - An input that already carries any signature entry is refused before signing.
- **iOS:** `verify.py` checks that the IPA's embedded provisioning profile is an App Store profile for team `R6A892D599` and the target's bundle ID, and checks `CFBundleIdentifier` and `CFBundleVersion`. It runs after export and again right before upload.
- **Play update mode.** Inside one edit, `play_upload.py`:
  1. reads every track;
  2. requires the new versionCode to be greater than every versionCode on every track, and at most 1,000 above the highest;
  3. uploads the AAB and requires Play's `sha256` to match;
  4. replaces only the `internal` track with one `completed` release;
  5. commits with `ERROR_IF_IN_REVIEW`;
  6. after the commit, records the upload and prints the report (see [Fail loudly](#fail-loudly)).

  Any failure deletes the edit, and the log names the request that did not complete (`edit insert`, `track read`, `bundle upload`, `internal track update` or `commit`; a fixed line). Before step 4 is sent, `require_internal_release` refuses anything but exactly one `completed` release of exactly the new build on `internal`: no other track, no `userFraction`, `countryTargeting`, `inAppUpdatePriority` or other field, no `inProgress`, `halted` or `draft` status, and no existing versionCode (a promotion). Before step 5, `require_commit` refuses any query but `changesInReviewBehavior=ERROR_IF_IN_REVIEW`, so `changesNotSentForReview` is refused.

  Before any request it refuses: a target that `store.PLAY_UPLOAD_TRACKS` does not map to `internal` (stage 3: `playground` and `production`); a build number below the app's floor (`store.MIN_BUILD_NUMBERS`: May production starts at **3**; admission refuses it first, dry runs included); a `MAY_PLAY_ACCOUNT` that is not the target's own account; a signed bundle whose sole signer is not the target's own upload certificate; and a signed bundle whose own manifest is not exactly the target's package (`store.APP_IDS[target]`) at the requested versionCode. The manifest is read on the release side by `verify.check_aab_identity` (aapt2's protobuf `XmlNode`, decoded without a dependency); the build job's own check is not trusted for it. So a Playground key, account or package can never be uploaded as May production, or the reverse.

  **The floor.** May production's first release is versionCode 2, uploaded by hand on 14 Oct (versionCode 1 was built but never uploaded). Workflow uploads start at 3, and the floor of 3 keeps every later build above vc2 at admission, before Play's own check ever sees it. That is stricter than may-app's `tools/release/build-number-floors` (production `1`), which `android.sh` applies in the build job, and is not derived from it.

  **Testers.** Internal testers are set by hand in Play Console. `may-play-upload@…` holds "Release apps to testing tracks" on May and nothing more; it deliberately lacks "Manage testing tracks", so it cannot change tester lists, and nothing here tries to.

  **`changesNotSentForReview`.** If Play ever answers a commit with HTTP 400 asking for `changesNotSentForReview` (Play does this when an app has changes it rejected or will not review automatically), the fix is in Play Console: resolve or send the pending changes there. The workflow will not send that parameter; `require_commit` refuses it.
- **Play client.** It uses only the standard library and the system `openssl`, with no third-party package:
  - The RS256 service-account assertion goes once to `https://oauth2.googleapis.com/token`.
  - Requests are allowed only to the Play Developer API host, and redirects are refused.
  - **Nothing is retried**: no refresh-and-resend on 401 for the upload or the commit. A 401 fails the run and deletes the edit.
  - **A failed request names its HTTP status, and nothing else.** A non-2xx reply stops the run with, for example, `May Play upload stopped: Google Play request was rejected or uncertain (HTTP 403); inspect the store before any retry`. A network error, timeout or malformed HTTP reply says `(no HTTP status)`. The error body is discarded unread, and no header, URL or token is printed. The message itself does not say which request failed, but for a Play Developer API request (not the token exchange at `oauth2.googleapis.com`) the line before it names the request. As a rough guide: 400, a request that was not accepted, which includes a credential problem at the token step (`invalid_grant`); 401, a credential problem; 403, a missing permission or a disabled API (see [Stage 2](#stage-2-android-uploads-to-the-internal-track-done)); 404, the wrong package or an app Play does not know.
- **Build numbers** above 2,000,000,000 are refused everywhere.

**Pinned toolchain** (`toolchain.py`; `actions/setup-java` is not used because it does not verify its download). The JDK is pinned for linux x64 only, so every Android job, including the native compile, runs on `ubuntu-24.04`; `static_check.py` refuses the JDK on macOS:

- Temurin JDK `21.0.12.1+1` for linux x64. Its SHA-256 equals Adoptium's published checksum.
- age `v1.3.1` for linux amd64 and darwin arm64. Each archive was verified with `sigsum-verify` against age's published key and proofs.

## Private release script interface

`may-app` main has `tools/release/android.sh build-unsigned` (May app PR #105, hardened in #107; present at main `31f3cbb`), which also refuses a build number at or below its app's floor in `tools/release/build-number-floors`. There is no `ios.sh archive-unsigned` yet; it is a follow-up, which is why the iOS jobs stay inert. The work is tracked in [May app issue #52](https://github.com/May-Assistant/may-app/issues/52). This repository expects:

- `bash tools/release/android.sh build-unsigned`
  - Inputs: `MAY_TARGET`, `MAY_SOURCE_SHA`, `MAY_BUILD_NUMBER`, `MAY_APP_ID`/`MAY_ANDROID_APP_ID` and `MAY_ARTIFACT_DIR`, with `JAVA_HOME` set to the pinned JDK.
  - Leaves exactly `$MAY_ARTIFACT_DIR/unsigned.aab`, with no signature, `applicationId` = `MAY_ANDROID_APP_ID` and `versionCode` = `MAY_BUILD_NUMBER`.
- `bash tools/release/ios.sh archive-unsigned`
  - Inputs: the same variables plus `MAY_IOS_APP_ID`, `MAY_TEAM_ID` and `DEVELOPER_DIR` (Xcode 26.3).
  - Leaves exactly `$MAY_ARTIFACT_DIR/May.xcarchive` with `CFBundleIdentifier` = `MAY_IOS_APP_ID` and `CFBundleVersion` = `MAY_BUILD_NUMBER`.
  - Signs with no team identity: ad-hoc (`CODE_SIGN_IDENTITY=-`) with the app's entitlements embedded, so that the release job's `xcodebuild -exportArchive` can re-sign with the distribution identity and keep them.

Both scripts exit non-zero on failure and keep their output in the private log. They never see a signing or upload secret.

Exporting and re-signing a non-distribution-signed archive in a separate job is expected to work, but is **unverified until a macOS rehearsal**. That job uses only Apple's tools, with no build phases, SwiftPM or scripts. If that rehearsal shows that entitlements or app extensions cannot be carried across, the iOS path cannot yet be split safely, and the fallback has to be decided then. Signing must not move back into the build job.

## Activation

The store workflow runs only from `main`: `gate.py` refuses any ref but `refs/heads/main` and any event but `workflow_dispatch`.

### Stage 1: Android dry runs (done)

Stage 1 (#7, `eb972b9`) made the Android dry run reachable and held every upload. Samuel's dry run [38566158807](https://github.com/May-Assistant/may-releases/actions/runs/38566158807) passed at `eb972b9`: admission, `android-build` (7m21s) and `android-release` succeeded, and the iOS jobs and the upload step were skipped. It signed with the real upload key, verified against `MAY_ANDROID_CERT_SHA256`, and reported package `io.github.mayassistant.may.playground`, versionCode `3`, track `internal`.

That proved, on real infrastructure: admission and both environments' protection (before any secret, and again in each job after review); the build on a fresh runner (private source, the Android design-system pin, Gradle, the unsigned AAB); the seal, the one-day encrypted artifact and the open with the real `MAY_ARTIFACT_DECRYPT_KEY`; signing with the real keystore and verification; and the report. It did **not** prove the upload: the Play client has never run against Play.

### Stage 2: Android uploads to the internal track (done)

Stage 2 (#8, `1c2e0b1`) took the guard off the Android upload, set `UPLOADS_HELD = {"ios"}`, admitted only `PLAY_UPLOAD_TRACKS = {"playground": "internal"}`, and added the [loud-failure check](#fail-loudly). #9 (`4202cf9`) made a Play failure name its HTTP status. The first real upload, run [38592266470](https://github.com/May-Assistant/may-releases/actions/runs/38592266470), failed with 403 `SERVICE_DISABLED` until `androidpublisher.googleapis.com` was enabled on `may-assistant-app`. After that, **May Playground build 5 uploaded to the internal track through the workflow** (run [38596661962](https://github.com/May-Assistant/may-releases/actions/runs/38596661962)). That proved the Play client end to end: token, edit, track read, bundle upload, track update, commit and the upload record.

**Before the first automated upload for any new Play app, confirm that `androidpublisher.googleapis.com` (the Google Play Android Developer API) is enabled on the Cloud project that owns the service account** (`may-assistant-app`). Play Console does not show it, and manual Console uploads never need it. It is now enabled on that project, which both May service accounts belong to.

### Stage 3: May production uploads to its internal track, and sign-only (this change)

Samuel's decision (14 Oct): may-releases may upload the production May app to its Play **internal** track only (`"production": "internal"` in `PLAY_UPLOAD_TRACKS`). A passing production dry run comes first. Promoting a build from internal to production stays a manual step in Play Console. This change:

- adds `"production": "internal"` to `store.PLAY_UPLOAD_TRACKS` (and to `static_check.PLAY_UPLOAD_TRACKS`); admission and `play_upload.py` now admit May production, to `internal` only;
- binds every Android target to its own upload key and Play account in code (`store.ANDROID_UPLOAD_KEYS`): `sign.py` refuses another target's alias or certificate pin before signing, and `dry_run.py`, `play_upload.py` and `sign_only.py` refuse a signed bundle whose sole signer is not the target's key; `play_upload.py` and `dry_run.py` refuse another target's `MAY_PLAY_ACCOUNT`;
- adds the build-number floor (`store.MIN_BUILD_NUMBERS`: May production starts at 3, above its first release, vc2; see [Verification and upload](#verification-and-upload)), checked at admission (dry runs included), in the dry-run report, in `sign_only.py` and in `play_upload.py`, next to Play's own check that the versionCode exceeds every versionCode on every track;
- refuses in code any track write but one completed internal release of the new build, and any commit but `ERROR_IF_IN_REVIEW`;
- names, in the log, the Play request that did not complete;
- adds [sign-only](#sign-only), in its own `android-sign-only` job with no Play secret, for a CI-signed May production bundle to upload in Play Console by hand;
- reads every signed bundle's own manifest on the release side (`verify.check_aab_identity`) and refuses another package or versionCode, in `dry_run.py`, `sign_only.py` and `play_upload.py`;
- moves `static_check.py` to stage 3.

`confirm_upload.py` checks May production exactly as it checks Playground: package, versionCode, track `internal`, signed SHA-256 and committed edit ID. The iOS jobs and the iOS upload stay inert until `ios.sh` exists and a macOS rehearsal has passed.

### What Samuel does

Each run below needs Samuel's two approvals, as every store run does: `may-native-compile` for `android-build`, then `may-android-internal` for `android-release` (or `android-sign-only`).

1. **Production dry run first.** Dispatch `platform: android`, `target: production`, a reviewed may-app `main` SHA, `build_number` **3** or higher (above vc2, May's first release, and every other Play versionCode), `dry_run: true`, `sign_only: false`, and approve both gates. It must pass: it proves May's key, pin, account variable, floor and the bundle's own package and versionCode, and reports package `io.github.mayassistant.may`, the versionCode, track `internal` and the signed SHA-256.
2. **Then the real upload**, a normal stage-3 upload: dispatch the same with `dry_run: false` and approve both gates. This needs vc2 **rolled out** on the internal track (a completed release, not a draft): `play_upload.py` counts only a rolled-out release as the app's first, so with vc2 still a draft it refuses with the first-release message. The run ends with the upload report and the committed Play edit ID; confirm the release in Play Console's internal testing track. Later builds follow the same pattern, each with a higher versionCode.
3. **Testers** are set by hand in Play Console (May > Testing > Internal testing > Testers): the service account can release to testing tracks but cannot manage tester lists.
4. **Promotion to production, if Samuel ever wants it,** is done by hand in Play Console (Testing > Internal testing > Promote release). Nothing in this repository can do it.

### May production after its first release

May production's first release, versionCode 2 (0.1.0), was uploaded by hand to the internal track on 14 Oct: signed with `may-upload` on Samuel's machine by the local lane (checklist 0b), with Play App Signing set up then. Play's API could not have made it (see [Store records](#store-records)). From here, production builds go through the workflow at versionCode **3** or higher ([What Samuel does](#what-samuel-does)). A dispatch for any app that has no rolled-out release yet is still refused, red, before anything is uploaded, with `First release must be uploaded in Play Console: …`.

### Sign-only: a CI-signed bundle for a Console upload

If a production build ever has to go through Play Console by hand, [sign-only](#sign-only) produces it signed in CI, so the `may-upload` keystore stays in the protected environment:

1. Once: generate the [Console-upload key](#samuels-console-upload-key-sign-only) on Samuel's Mac (`mkdir -p ~/.may` first) and commit its public half in a reviewed PR. Until then admission denies sign-only.
2. Dispatch `platform: android`, `target: production`, a reviewed may-app `main` SHA, `build_number` **3** or higher, `dry_run: true`, `sign_only: true`. Approve `may-native-compile`, then `may-android-internal` (for the `android-sign-only` job).
3. The run ends with "Signed, not uploaded", the package and versionCode read from the bundle, and the signed SHA-256. Within 3 days, download the artifact into an empty folder, either with the CLI:

   ```
   mkdir -p ~/may-signed && cd ~/may-signed
   gh run download <run-id> -n may-signed-android-<attempt> -R May-Assistant/may-releases
   ```

   or from the run page (Artifacts), then `unzip may-signed-android-<attempt>.zip` in that folder.
4. Decrypt and check it on the Mac:

   ```
   age -d -i ~/.may/console-upload.agekey -o signed.aab signed.aab.age
   shasum -a 256 -c signed.aab.sha256
   ```

5. Upload `signed.aab` in Play Console: May > Testing > Internal testing > Create new release, then roll it out. Play App Signing is already set up (with vc2), and `may-upload` is the upload key, matching the pin committed here (`9E2FE9C8…1E899815`). For a brand-new app's first release, keep the app signing key Google generates, so that the first bundle's certificate becomes the upload key.

The next workflow upload must then use a versionCode above it, and above every versionCode on every track.

### Dispatch examples

| Input | Production dry run | Production upload | Sign-only (Console upload) | Playground upload |
| --- | --- | --- | --- | --- |
| `platform` | `android` | `android` | `android` | `android` |
| `target` | `production` | `production` | `production` | `playground` |
| `source_sha` | reviewed may-app `main` SHA | same | same | same |
| `build_number` | 3 or higher (above vc2) | the same number as its passing dry run, above every Play versionCode | 3 or higher, above every Play versionCode | above every Play versionCode (6 or higher after build 5) |
| `dry_run` | `true` | `false` | `true` | `false` |
| `sign_only` | `false` | `false` | `true` | `false` |

```
gh workflow run store-release-draft.yml -f platform=android -f target=production -f source_sha=<reviewed may-app main SHA> -f build_number=3 -f dry_run=true -f sign_only=false --repo May-Assistant/may-releases --ref main
```

In the Actions UI, choose **May protected store release (stage 3, Android internal uploads; iOS inert)**, **Run workflow** from `main`, and set the inputs above. A run that did not do what it was asked is **red**: admission refuses what it does not admit, the upload step fails on any Play refusal or error (deleting the edit), and the loud-failure checks fail the job whenever the upload, or the sign-only publication, did not succeed.

### iOS compile each build

`native-compile-draft.yml` checks that iOS May Playground still compiles, at the may-app commit of each build. It is a compile only: no signing, no TestFlight, no artifact. The coordinator dispatches it; Samuel approves `may-native-compile` (the same environment and read-only keys as every store build).

```
gh workflow run native-compile-draft.yml -f platform=ios -f source_sha=<reviewed may-app main SHA> --repo May-Assistant/may-releases --ref main
```

- **What it builds:** `prepare.py --platform ios --target playground` fetches the exact may-app commit and iOS's own design-system pin. `compile.py --platform ios --target playground` then checks Xcode 26.3, runs the pinned XcodeGen, and builds the `MayPlayground` scheme for the iOS Simulator, Release, with `CODE_SIGNING_ALLOWED=NO`.
- **Not compiled yet:** the production `May` scheme. Its pins must match Android's first.
- **What it reports:** pass or fail only. The compiler's log stays on the runner, which is deleted with it, because it contains private source.
- **Pinned by `static_check.py`:** it is iOS-only, on `macos-15`, for Playground, with no JDK and no inert guard (`COMPILE_IOS_LINES`).

### iOS compile of a pull request head

`native-compile-pr.yml` gives the iOS lane compile evidence for an **open may-app pull request**, before it merges. It is the same May Playground simulator compile as above. It is a separate workflow, so the release path and `native-compile-draft.yml`, which accept only commits on may-app `main`, are unchanged. It has no signing, no store call and no artifact.

**No approval** (Samuel, 17 Oct, `decisions.md` "Build 11 decisions": compile-only runs on may-releases for open may-app PR heads don't need his approval; signing, TestFlight and store releases keep theirs). The environment's policy is its own (`gate.NO_REVIEWER_ACTIONS`, pinned by `static_check.py` to exactly `{"compile-pr", "ds-compile"}`): no other action may run without his review.

```
gh workflow run native-compile-pr.yml -f pr_number=<may-app PR number> -f source_sha=<that PR's current head SHA> --repo May-Assistant/may-releases --ref main
```

- **Admission** (`gate.py --action compile-pr`, no environment) checks the dispatch (main only, manual) and the inputs. It also checks that `may-native-compile-pr` exists and has exactly the right policy:
  - administrator bypass off;
  - a custom branch policy of exactly `main`;
  - **no** required-reviewer rule. A reviewer rule is denied, because it would only hang the run.

  A missing environment is denied on its 404 and never auto-created. Admission holds no secret.
- **The pull request check** (`--check-pull`, the first step on the `may-native-compile-pr` runner, before any pull-request code runs) reads `GET /repos/May-Assistant/may-app/pulls/<number>` with `MAY_APP_PR_READ_TOKEN`, which that environment holds. It admits the commit only if all of these hold:
  - the pull request is **open**;
  - its base is **may-app `main`**;
  - its head is in **may-app itself** (a fork, or a deleted fork, is refused);
  - its head is **exactly** `source_sha`.

  The public line then names the pull request, its author and the dispatcher (GitHub logins only, otherwise `unknown`), so a run is attributable.
- **The fetch** (`prepare.py --pr-number`) fetches `refs/pull/<number>/head`, requires it to be exactly `source_sha`, and checks out that commit. The design-system pin is checked as for every build.
- **The compile** builds `MayPlayground` for the iOS Simulator, Release, `CODE_SIGNING_ALLOWED=NO`.
- **What the job holds, and why that is enough:** on one GitHub-hosted macOS job, everything the job references is available to the pull request's build code (XcodeGen, SwiftPM and Xcode build phases run it). The runner gives the job's user passwordless `sudo`, and GitHub hands the runner every secret the job references when the job starts. So the design goal is that the job holds **nothing a may-app writer doesn't already have**:
  - two read-only deploy keys (may-app, may-design-system);
  - a pull-request read token;
  - the job's read-only `GITHUB_TOKEN`.

  Only same-repo heads are admitted, so the author already holds that code and those repos. What the steps add is hygiene against accidents, not a boundary against a malicious pull request:
  - the read keys are bound only in the fetch step's `env`, written to `0600` files and deleted in a `finally`;
  - the compile refuses to start while any `*.key` or `known_hosts` file is in `may-private`, and refuses any known secret in its environment (`store.SECRET_NAMES`);
  - `static_check.py` pins each secret to exactly one step, and allows no `env` at all on the compile or cleanup.
- **Kept that way:**
  - **Dedicated deploy keys:** `may-native-compile-pr`'s keys are never `may-native-compile`'s, so revoking them never touches the release path. Rotate them whenever someone leaves may-app, because a copied key outlives their GitHub access.
  - **A short-lived pull-request token:** preferably a GitHub App installation token, otherwise a fine-grained token with an expiry. It covers may-app only, with Pull requests: read and Metadata: read.
  - **No Actions cache in any workflow:** `static_check.py` refuses `actions/cache` and `cache:` inputs. The runner's own token can write cache entries, and with nothing reading them that reaches nothing.
- **What reaches the public log:** this repository's tools print only fixed lines, `xcodebuild` writes to a private log that the cleanup deletes, and nothing is uploaded. A pull request's own build code can still write to the run (its step summary, for one). That is its author publishing code they already hold, not something this workflow can stop.

**What Samuel sets up before the first run** (admission denies until then). Create **environment `may-native-compile-pr`** with:
- **no required reviewers**;
- administrator bypass off (**Allow administrators to bypass** unchecked);
- **Deployment branches: Selected branches**, with only `main`.

Its secrets, and nothing else:
- `MAY_APP_READ_KEY`: a **new** read-only deploy key on may-app;
- `MAY_DS_READ_KEY`: a **new** read-only deploy key on may-design-system;
- `MAY_APP_PR_READ_TOKEN`: may-app only, Pull requests: read and Metadata: read, short-lived (see above).

No repository secret is needed.

### Design-system Swift compile of a pull request head

`ds-swift-compile.yml` lets the iOS lane see why may-app fails to compile against a may-design-system commit (the app compile keeps its log private). It compiles may-design-system's Swift package alone, at the head of an **open may-design-system pull request** against `claude/may-integration` or `main`, from may-design-system itself, and posts the result as **one comment on that pull request** (Samuel's decision). It has no signing, store call, artifact or approval.

```
gh workflow run ds-swift-compile.yml -f ds_sha=<that PR's current head SHA> -f ds_pr=<may-design-system PR number> --repo May-Assistant/may-releases --ref main
```

**This repository is public and may-design-system is private**, so nothing derived from the design system (paths, file or scheme names, compiler messages, logs) reaches this run's log, step summary, artifacts or outputs. The log shows fixed lines, the dispatch's own inputs (commit and pull request number), the dispatcher's login and the number of errors, for example `DS Swift compile failed; 12 errors posted to may-design-system #40`. `static_check.py` pins the workflow whole and every `print` in `ds_compile.py` (a literal, or only the pull request number, the error count and the Xcode version).

- **Admission** (`gate.py --action ds-compile`, no environment) checks the dispatch and inputs, and that `may-ds-renders` exists with administrator bypass off, a custom branch policy of exactly `main`, and **no** required-reviewer rule.
- **The pull request check** (`--check-pull`, first step on the `may-ds-renders` runner) reads the pull request with `MAY_DS_PR_READ_TOKEN`: open, base `claude/may-integration` or `main` of may-design-system, head in may-design-system itself, head exactly `ds_sha`. It prints nothing read from the pull request.
- **The fetch** (`ds_compile.py fetch`, `MAY_DS_READ_KEY` alone) fetches `refs/pull/<number>/head` over SSH exactly as `prepare.py` does (a `0600` key file and pinned `known_hosts`, both removed in a `finally`), requires it to be exactly `ds_sha`, and checks it out.
- **The compile** (`ds_compile.py compile`, no `env` at all) refuses to start while a key or host file is on disk or any known secret is in its environment, checks for Xcode 26.3, and builds the package-wide `*-Package` scheme (otherwise each scheme) for `generic/platform=iOS Simulator`, Release, `CODE_SIGNING_ALLOWED=NO`, with derived data and packages under `$RUNNER_TEMP`. `xcodebuild` gets only `PATH`, `HOME`, the user and locale, `DEVELOPER_DIR` and `TMPDIR` (no GitHub Actions file paths), and all its output goes to a private log. The outcome is recorded in a private file; a failed build does not fail this step.
- **The post** (`ds_compile.py post`, `MAY_DS_PR_COMMENT_TOKEN` alone, taken out of the environment at once) keeps only `<path>:<line>:<col>: error: <message>` lines whose path is inside the checkout, rewritten repo-relative; warnings, notes, source and caret lines, errors outside the checkout and everything else are dropped. It dedupes them, posts at most 100 and counts the rest, reads the pull request again (still open, same base rules, head still `ds_sha`), and posts one comment on it (`POST /repos/May-Assistant/may-design-system/issues/<number>/comments`, the only write it can make), with the head SHA, this run's URL, a hidden `<!-- may-ds-swift-compile head=<sha> -->` marker and the footer. A passing compile posts that it compiles. A failing compile then fails the job.
- **The cleanup** always removes `may-ds-source`, `may-ds-private`, `may-ds-derived` and `may-ds-packages` under `$RUNNER_TEMP`.
- **What the job holds:** as with the pull-request compile above, the design system's own build code (SwiftPM, macros, plugins) runs on a runner that holds every secret the job references. Those are a read-only deploy key and two tokens scoped to may-design-system, which a may-design-system writer already has the equivalent of. The steps add hygiene against accidents, not a boundary against a hostile pull request; in particular a pull request's own build code could still write to the run deliberately.

**What Samuel sets up before the first run** (admission denies until then). Environment **`may-ds-renders`** with no required reviewers, administrator bypass off, and deployment branches **Selected branches: `main`** only. Its secrets:
- `MAY_DS_READ_KEY`: a read-only deploy key on may-design-system (a new one, not another environment's);
- `MAY_DS_PR_READ_TOKEN`: may-design-system only, Pull requests: read and Metadata: read, short-lived;
- `MAY_DS_PR_COMMENT_TOKEN` (**new**): a fine-grained personal access token, may-design-system only, Pull requests: read and write (Metadata: read is always included), with an expiry. Comments appear under its owner's account.

### Claude code review of a may-app pull request

`may-app-review.yml` runs the pinned code-review command (the same `anthropics/claude-code` commit as `claude-code-review.yml`) on an **open, ready-for-review may-app pull request**, and posts its comments on that pull request: inline comments on the reviewed head, or a "No issues found" summary. Each comment ends with May's attribution footer and a hidden `<!-- may-app-review head=<sha> -->` marker. It exists because may-app's own Actions are blocked by billing (Samuel, option B).

```
gh workflow run may-app-review.yml -f pr_number=<may-app PR number> --repo May-Assistant/may-releases --ref main
```

**This repository is public and may-app is private**, so nothing from may-app may reach this run's log, step summary, artifacts or caches: no code, diff, file names, transcript or model output. That decides how it works:
- **No claude-code-action.** The action prints its options and its own setup, and its inline-comment server exists only for a pull request event in the workflow's own repository. `review.py` runs the pinned Claude Code release itself (`CLI_VERSION`, npm's linux-x64 package, checked against its pinned sha512; no install script runs).
- **All output stays in files.** Claude Code's stdout and stderr go to files under `$RUNNER_TEMP/may-review`, which the last step always removes (with `/tmp/may-review-view`). The log shows only fixed lines, the reviewed head SHA, logins and numbers (exit status, turns, cost, how many tool calls were denied, how many comments were posted).
- **The model holds nothing worth stealing and has no way out** (security review of #21, H1). A may-app pull request is written by anyone with push access, so the review treats it as hostile input:
  - **No shell and no `gh`.** Bash is denied outright, and `gh` could reach any host through `-R HOST/OWNER/REPO`. Instead, `review.py` reads the pull request with the token itself and writes it as files under `/tmp/may-review-view`: the head's checkout, `pr/pull.json` (title, description, changed files, conversation, reviews, review comments, and the checkout's CLAUDE.md paths) and `pr/diff.patch`.
  - **Read of that directory only.** Reads of `/proc`, `/sys`, `/home` (the runner, this job's `$RUNNER_TEMP` and workspace), `/etc` and the rest are denied, as are Grep, Glob, Write, Edit and the web. Symbolic links are checked out as plain files, so nothing under the view leads outside it.
  - **No GitHub token in Claude Code's environment.** The review token reaches `review_mcp.py` only through a `0600` file in a denied directory, which is removed when Claude Code exits. Claude Code holds its own OAuth token, which no allowed tool can read.
  - **Nothing from the checkout is loaded as instructions** (M2). Claude Code starts in an empty directory with `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, user settings only (a fresh home directory, never the checkout's settings, hooks, skills, agents or commands), only the comment server, and permission mode `dontAsk`. The checkout is read only as material.
  - **Checked against the real CLI (2.1.296, 17 Oct):** a file in the view reads; `/proc/self/environ` is denied; there is no Bash tool; and a CLAUDE.md in the working directory is ignored.
- **Comments go through `review_mcp.py`**, a local stand-in for the action's comment server under the same tool name, so the command runs unchanged. It:
  - posts only to the checked pull request at the fetched head, and reads the pull request again before each post, posting nothing once it is closed, a draft, retargeted or pushed to;
  - posts at most 30 comments per run, counted from its record file, so a restart can't reset the count;
  - lets nothing make a viewer's browser or GitHub's image proxy fetch from elsewhere. Outside code, raw HTML is escaped, image syntax becomes a plain link, and every link or bare URL whose host isn't github.com is replaced. Code blocks and spans are left as written, so a suggestion's code is intact;
  - refuses any body holding the review token in any spelling (raw, with separators, base64, hex);
  - adds the footer and marker, and redacts token-shaped text.

  `gh pr comment` is unavailable, so a summary goes through it too.
- **Pinned whole:** `static_check.py` pins the workflow's triggers, permissions, steps, commands and secrets. It also refuses `show_full_output` or `display_report` set to true in **any** workflow here.

How a run goes:
- **Admission** (`review.py admit`, no environment) checks the dispatch (main only, manual), the pull request number (digits only, no leading zero) and `may-claude-review`'s policy: administrator bypass off, a custom branch policy of exactly `main`, and Samuel the sole required reviewer (the store environments' policy, `gate.validate_environment`). A missing environment is denied on its 404, never auto-created.
- **Samuel approves the run** (the required reviewer). Running without approval, for example a scheduled scan of open pull requests, would need his decision first. `gate.NO_REVIEWER_ACTIONS` stays `{"compile-pr", "ds-compile"}`.
- **The fetch** (`review.py fetch`, on the environment's runner) reads the pull request with `MAY_APP_REVIEW_TOKEN`. The pull request must be open, ready for review, against may-app `main` and from may-app itself. It fetches exactly that head into `/tmp/may-review-view/source`. The token goes to git through its environment, never its arguments or config, and git's output is discarded. The review job has `contents: read` alone. Only admission reads the environment's policy (M3).
- **The review** (`review.py review`) refuses a head that moved since the fetch, writes the pull request's files, then runs the command.

**Before relying on it:** after it merges, one real run on a may-app pull request, then a read of that run's public log to confirm nothing private appears.

**What Samuel sets up before the first run** (admission denies until then). Create **environment `may-claude-review`** with:
- **Required reviewers:** only Samuel, with **Prevent self-review off** (he dispatches and approves his own runs);
- **Allow administrators to bypass** unchecked;
- **Deployment branches: Selected branches**, with only `main`.

Its secrets, and nothing else:
- `MAY_APP_REVIEW_TOKEN`: a fine-grained personal access token, resource owner May-Assistant, **only** the may-app repository, with an expiry of 90 days or less, and these permissions:
  - Contents: Read-only (the checkout);
  - Pull requests: Read and write (reading the pull request, and posting inline and summary comments);
  - Metadata: Read-only (always included).

  Comments appear under the token owner's account; the footer marks them as Claude's. Only `review.py` and `review_mcp.py` ever hold the token, never the model. They make only the reads above and the two kinds of comment post. Pull requests: write would also allow editing or closing a pull request, but no code here does that. The token cannot merge, which needs Contents: write.
- `MAY_REVIEW_CLAUDE_TOKEN`: a Claude Code OAuth token from `claude setup-token`, the same kind as the repository's `CLAUDE_CODE_OAUTH_TOKEN`. A separate token is better, so either can be revoked alone.

No repository secret is needed.

### Prerequisites that remain

`gate.py` checks these, before any secret and again after each review. Any one failing denies the run. The environment checks apply to **both** `may-native-compile` and the target's store environment (`may-android-internal` for May production):

- the environment exists (a 404 denies; it is never auto-created);
- **administrator bypass is off** (`can_admins_bypass: false`);
- a **custom** deployment branch policy (not "protected branches") with exactly one rule, `main`;
- exactly **one required-reviewer rule**, whose **sole** reviewer is Samuel (user ID `58840187`);
- the self-review setting is readable, and it is not the deadlock case: the gate denies only when *Prevent self-review* is **on and** the sole reviewer (Samuel) dispatched the run, because that run could never be approved. **In practice it must be off**, because Samuel dispatches and approves his own runs. That is Samuel's decision for internal builds only (see [Self-review](#environments-and-protection));
- in stage 3, a dispatch with `dry_run: false` is Android May or May Playground, and one with `sign_only: true` is May production Android with `dry_run: true` and a committed Console-upload key (admission only; see [Dry run](#dry-run));
- the build number is at least the app's floor (May production: 3);
- the target's **artifact public key is committed** and well-formed: `tools/may_release/artifact_keys/may-android-internal.age.pub` (`age1kpcl…skyd7vg`) for May production, `may-android-playground-internal.age.pub` (`age1cv26…sex4n5j`) for Playground, both committed 2026-10-13. A placeholder or malformed key denies before any request. Its private half is that environment's `MAY_ARTIFACT_DECRYPT_KEY`, which `handoff.py open` checks against it.

Also needed, which the gate cannot see:

- in `may-native-compile`: `MAY_APP_READ_KEY` and `MAY_DS_READ_KEY` (and, for crash reports, `MAY_POSTHOG_ANDROID_KEY`);
- in `may-android-internal`: the secrets `MAY_ARTIFACT_DECRYPT_KEY`, `MAY_ANDROID_KEYSTORE_BASE64` (the `may-upload` keystore), `MAY_ANDROID_STORE_PASSWORD`, `MAY_ANDROID_KEY_PASSWORD` and, for an upload, `MAY_PLAY_JSON` for `may-play-upload@may-assistant-app.iam.gserviceaccount.com`; the variables `MAY_ANDROID_KEY_ALIAS=may-upload`, `MAY_ANDROID_CERT_SHA256` (must equal the committed pin) and `MAY_PLAY_ACCOUNT=may-play-upload@may-assistant-app.iam.gserviceaccount.com`;
- that service account holds May's "Release apps to testing tracks" permission in Play Console (confirmed 14 Oct, and nothing more: it lacks "Manage testing tracks", so testers are set by hand in Console).

`android-release` references `MAY_PLAY_JSON`, so once it is set the runner receives it with every `android-release` job, dry runs included, even though only the upload step is given it. A sign-only run does not run `android-release`; its own job references no Play secret.

The Play client has run against Play for May Playground (build 5). It has not yet run for May production, and the iOS paths have not yet run against App Store Connect.

No May backend deployment should be inferred from a mobile compile. Model-spend limits and default-off voice activation are separate staging admission gates.

Public GitHub Actions logs are visible to everyone. Every failure path prints only fixed, nonsensitive messages (`static_check.py` checks that printed error messages are string literals). The one exception is `play_upload.PlayStatusError`, which adds the numeric HTTP status, and only when it is an integer from 100 to 999. `static_check.py` pins that class whole: it must exist exactly once, at the top level of `play_upload.py`, and parse to exactly the copy in `STATUS_ERROR_SOURCE`, so any edit to it fails the check until that copy is updated in the same reviewed change. It may be called only in `play_upload.py`, with `<name>.status_code`. `play_upload.py` may not rebind `str`, `type`, `int`, `super`, `StoreError` or the class itself, or use `builtins`, `globals()`, `vars()`, `setattr`, `exec`, `eval` or `__import__`. No module may assign an exception's `.args`. These rules guard against accidental drift, not hostile code. A plain `print(os.environ[...])` anywhere in a script is not detected, and never was. The follow-ups in [#9](https://github.com/May-Assistant/may-releases/pull/9) list further known routes round the pin. Review is what stops a deliberate leak. See [GitHub's environment availability rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) for why the control repository is public while the app repositories remain private.
