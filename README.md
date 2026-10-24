# May release control

This **public** repository holds release-control workflows only. May app and design-system source remain private. No private source, assets, signing material, provider response, native build output, or credential belongs in a commit, public log, or artifact here (the one artifact the store workflow publishes is age-encrypted; see below).

## Store records

The separate May Playground iOS app is registered in App Store Connect as `6822072535` with bundle `io.github.mayassistant.may.playground`, active App Store profile `T5Y2C9URKX`, and empty internal TestFlight group `8a709a78-c828-4e7b-818c-b6ce62998ec8`. The real May iOS app is `6821782401` with bundle `io.github.mayassistant.may`. The corresponding Play apps are separate records: May `4974275831988601568` and May Playground `4972137188420176977`.

**Each Play app's first upload is made by hand in Play Console; this repository cannot make it.** Google's Play Developer API guide ([Edits](https://developers.google.com/android-publisher/edits)) says: "You can only use this API to make changes to an existing app (that has at least one APK uploaded); thus, you will have to upload at least one APK through the Play Console before you can use this API." Play also accepts only `status: draft` releases through the API until an app's first release has been rolled out. May Playground's first release (versionCode 1) was uploaded by hand on 13 Oct, which is why its later uploads work. **May production's first release is versionCode 2 (0.1.0)**: the local lane signed `May-production-vc2-1b73881` with the `may-upload` keystore on Samuel's machine, and Samuel uploaded it by hand to the internal track on 14 Oct (evidence: may-app `docs/evidence/m1-production-install-2026-10-14/`). So stage 3's workflow uploads of May production start at **versionCode 3** ([May production after its first release](#may-production-after-its-first-release)).

This repository only performs later updates. It refuses an app whose tracks show no rolled-out release, before uploading anything, with the fixed message `First release must be uploaded in Play Console: no track of this app has a rolled-out release, and Play's API cannot make an app's first release. …`. If Play refuses the edit itself, the log names the request (`edit insert`) and the HTTP status, and says that an app with no Console upload is the cause to check first.

## Current state

**Activation status: stage 4, Android uploads to the Play internal track and iOS uploads to internal TestFlight (May and May Playground), plus sign-only for May production.** The [store-release workflow](.github/workflows/store-release-draft.yml) can run an Android **dry run**, or, with `dry_run: false`, upload **May** or **May Playground** to its Play **`internal`** track, or, for May production with `sign_only: true` and `dry_run: true`, sign and verify in a separate `android-sign-only` job (no Play secret) and then publish the signed bundle, encrypted, for a manual Play Console upload. Promoting a build from internal to production stays a manual step in Play Console (Samuel's decision, 14 Oct). An iOS dispatch can dry-run or, with `dry_run: false`, upload May or May Playground to **internal TestFlight** only (Samuel's go, 19 Oct). Earlier stages held a job or step **inert** with a condition that requires an event name the workflow never registers (`may-store-disabled-pending-review`); in stage 4 that guard appears nowhere. In stage 4:

| Part of the store workflow | Stage 4 |
| --- | --- |
| `admission` | active (runs on every dispatch) |
| `android-build`, `android-release` | active when `platform` is `android` (`android-release` only when `sign_only` is not `true`) |
| Android upload step (`play_upload.py`) | **active** when `dry_run` is `false`; admission admits that for target `playground` or `production` (track `internal` only) |
| Android loud-failure check (`confirm_upload.py`) | runs whenever `dry_run` is `false`; fails the job unless the upload step succeeded |
| `android-sign-only` (`sign_only.py`, the publish step, `confirm_upload.py --sign-only`) | runs instead of `android-release` when `sign_only` is `true`; admission admits that only for May production Android with `dry_run: true`, and only once Samuel's Console-upload key is committed (it is a placeholder today). The job references no Play secret or variable |
| `ios-build`, `ios-release` | active when `platform` is `ios` (`tools/release/ios.sh archive-unsigned`, rehearsed on macOS in runs 39100919695 and 39101524181) |
| iOS upload step (`ios_upload.py`) | **active** when `dry_run` is `false`; admission admits that for target `playground` or `production` (`gate.TESTFLIGHT_TARGETS`), and the export is `testFlightInternalTestingOnly` |
| iOS loud-failure check (`confirm_upload.py`) | runs whenever `dry_run` is `false`; fails the job unless the upload step succeeded |
| Dry-run report (`dry_run.py`) | runs whenever `dry_run` is not `false`; for Android it now also rechecks the build-number floor, the signer and the Play account |

`static_check.py` pins exactly this layout (`STAGE`, `STORE_JOB_IF`, `UPLOAD_IF`, `CONFIRM_IF`, `PLAY_UPLOAD_TRACKS`, and the three sign-only steps, `PUBLISH_SIGNED_STEP` exactly), and requires `gate.py`'s `UPLOADS_HELD` to name exactly the platforms whose upload step is held (stage 4: none, `set()`). A guard that reappears on any job or upload step is flagged as a stage mismatch rather than accepted, so going back to an earlier stage, or to fully inert, is a deliberate, reviewed change to those constants too.

The [pull-request compile workflow](.github/workflows/native-compile-pr.yml) compiles iOS May Playground at the head of an open may-app pull request against `main`, in its own environment `may-native-compile-pr`. It needs no approval (Samuel, 17 Oct), and it is not usable until Samuel creates that environment and its secrets (see [iOS compile of a pull request head](#ios-compile-of-a-pull-request-head)).

The [iOS archive rehearsal workflow](.github/workflows/ios-archive-rehearsal.yml) runs may-app's `tools/release/ios.sh archive-unsigned` at the head of an open may-app pull request, on `macos-15` with the pinned Xcode 26.3 (17C529), with the same admission, fetch and environment as the pull-request compile (`may-native-compile-pr`). It signs nothing for distribution, uploads nothing to a store, and publishes only a fixed-shape summary, never the archive (see [iOS archive rehearsal of a pull request head](#ios-archive-rehearsal-of-a-pull-request-head)).

The [Android unit-suite workflow](.github/workflows/android-unit-pr.yml) runs may-app's three Android unit suites (`:core:test`, `:app:testPlaygroundDebugUnitTest`, `:app:testProductionDebugUnitTest`) at the head of an open may-app pull request, on `ubuntu-24.04` with the pinned JDK, with the same admission, fetch and environment as the pull-request compile (`may-native-compile-pr`). It publishes only each suite's pass or fail and its test, failure and skip counts (see [Android unit suites at a pull request head](#android-unit-suites-at-a-pull-request-head)). [`android-ci-schedule.yml`](.github/workflows/android-ci-schedule.yml) dispatches it every 15 minutes for one open may-app head that touches Android (see [Android unit suites on a schedule](#android-unit-suites-on-a-schedule)).

The [backend checks workflow](.github/workflows/backend-unit-pr.yml) runs may-app's backend CI (its own `backend-ci.yml` is switched off, because may-app's Actions are blocked by billing) at the head of an open may-app pull request: the pinned Node 22.22.0 against a Postgres 16.15 service container pinned by digest, then `npm ci`, the typecheck, migrations from scratch twice with the ledger check, and `npm test`, on `ubuntu-24.04`, with the same admission, fetch and environment as the Android unit suites (`may-native-compile-pr`). It publishes only each stage's pass or fail and the test, pass, failure and skip counts; the failures go only to the may-app pull request (see [Backend checks at a pull request head](#backend-checks-at-a-pull-request-head)). [`backend-ci-schedule.yml`](.github/workflows/backend-ci-schedule.yml) dispatches it every 15 minutes for one open may-app head that touches `backend/` (see [Backend checks on a schedule](#backend-checks-on-a-schedule)).

The [design-system Swift compile workflow](.github/workflows/ds-swift-compile.yml) compiles may-design-system's Swift package at the head of an open may-design-system pull request and posts its compile errors on that pull request, never in this log, in its own environment `may-ds-renders`. It needs no approval, and it is not usable until Samuel creates that environment's secrets (see [Design-system Swift compile of a pull request head](#design-system-swift-compile-of-a-pull-request-head)).

The [may-app review workflow](.github/workflows/may-app-review.yml) runs Claude's code review of an open may-app pull request and posts it on that pull request, because may-app's own Actions are blocked. It runs in its own environment `may-claude-review`, and nothing from may-app reaches this public repository's log. It is not usable until Samuel creates that environment and its secrets (see [Claude code review of a may-app pull request](#claude-code-review-of-a-may-app-pull-request)).

The [native compile workflow](.github/workflows/native-compile-draft.yml) is **active for iOS only**: each build, the coordinator dispatches it to check that **May Playground** compiles. It runs on `macos-15` with the pinned Xcode 26.3 and XcodeGen 2.46.0, makes a simulator Release build with `CODE_SIGNING_ALLOWED=NO`, and has no signing, store call or artifact (see [iOS compile each build](#ios-compile-each-build)). The pull-request check runs only dependency-free control tests and static checks with a read-only token.

The admission gate **denies any store run whose target's artifact public key is a placeholder or malformed**. As of 2026-10-13 all four real public keys are committed (generated by Dots on Samuel's Mac; see [the key-pair steps](#artifact-key-pair-setup)), so that check passes.

Confirmed from the repository's Actions secrets page on 2026-10-13. All secrets below are environment-scoped. The only repository secret is `CLAUDE_CODE_OAUTH_TOKEN`, and no organization secrets are available to this repository.

| Environment | Secrets present |
| --- | --- |
| `may-native-compile` | `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY`; optional `MAY_POSTHOG_ANDROID_KEY` (and variable `MAY_POSTHOG_HOST`) and `MAY_POSTHOG_IOS_KEY` (and variable `MAY_POSTHOG_IOS_HOST`); optional `MAY_GOOGLE_SERVICES_JSON` |
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

1. **`*-build`** (`may-native-compile`, read-only source keys and the platform's own PostHog crash-report key only):
   - fetches the exact private source and the design system pinned for the platform being built (`prepare.py`; see [Design-system pins](#design-system-pins));
   - runs the private build script for an **unsigned** product (`store.py`). This step runs third-party code: Gradle and its plugins, SwiftPM, and Xcode build phases. On Android it holds one secret, `MAY_POSTHOG_ANDROID_KEY`, which Gradle compiles into the app (it ships inside the APK, as PostHog's project keys are meant to); without it the app is built with no key and sends no crash reports, and the step says so in one fixed line. On iOS it holds `MAY_POSTHOG_IOS_KEY` in the same way: `ios.sh` gives it to `xcodebuild archive` as a build setting the app's Info.plist reads, never prints it (xcodebuild's own echo of its build settings is redacted), and fails the build unless the archived app carries exactly that key. The Android build step also holds `MAY_GOOGLE_SERVICES_JSON` (the [Firebase config](#prerequisites-that-remain) for push), which `android.sh` receives only as the path of a private file;
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
   - `sign.py` signs and verifies, then `play_upload.py` or `ios_upload.py` uploads, unless this is a [dry run](#dry-run). In [stage 4](#current-state) both upload steps can run (May and May Playground, Play internal or internal TestFlight), or, for May production Android, the [sign-only](#sign-only) job instead, which publishes the encrypted signed bundle and holds no Play secret.
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
- **In stage 4 no upload step is held.** Admission (`gate.py --dry-run`) refuses `dry_run: false` for a held platform (`UPLOADS_HELD`, empty), for any Android target `store.PLAY_UPLOAD_TRACKS` does not map to `internal` and for any iOS target outside `gate.TESTFLIGHT_TARGETS`, before any approval, build or secret, rather than finishing green without uploading.
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
  - Optional secret `MAY_POSTHOG_ANDROID_KEY`: PostHog's project key for Android crash reports (`phc_` then letters, digits, `_` and `-`). Only the Android build step receives it; every other step refuses it (`store.SECRET_NAMES`). This one environment builds both May and May Playground, so both apps report to the same PostHog project.
  - Optional secret `MAY_GOOGLE_SERVICES_JSON`: the base64 of the Firebase project's `google-services.json`, for push (FCM). Only the Android build step receives it; every other step refuses it, the iOS build step included. `store.py` checks it is a JSON object (at most 256 KB) with a `project_info.project_id` and an Android client whose package is the target's own app ID, so one file must carry both May and May Playground. It writes it 0600 to `may-private/firebase/google-services.json` in the runner's temp folder (never in the checkout, where `android.sh` refuses any untracked file), hands `android.sh` only that path as `MAY_GOOGLE_SERVICES_FILE`, and deletes it when the build ends, failed or not (`cleanup.py build` removes `may-private` as well). It is never printed: the step's public line names only the package. Without it the app is built with no Firebase config, and the step says so in one fixed line. may-app's Android build does not read `MAY_GOOGLE_SERVICES_FILE` yet; its FCM change will, and until then the file is unused.
  - Optional variable `MAY_POSTHOG_HOST`: `https://eu.i.posthog.com` for a project on PostHog's EU cloud; unset means `https://us.i.posthog.com`. `store.py` refuses any other host, and the build step's public line names the host reports will go to.
  - Optional secret `MAY_POSTHOG_IOS_KEY` and variable `MAY_POSTHOG_IOS_HOST`: the same for iOS crash reports (may-app's `tools/release/ios.sh`). Only the iOS build step receives them; every other step refuses the key, the Android build step and the iOS archive rehearsal included, and the Android build never reads the iOS host (nor the iOS build Android's). The iOS key may be a different PostHog project from Android's.
  - Who sets them: only Samuel, and only in `may-native-compile`, never in `may-native-compile-pr` (no required reviewer; it builds pull request heads). GitHub resolves a variable or secret from the environment first, then the repository, then the organization, so a repository- or organization-level `MAY_POSTHOG_ANDROID_KEY`, `MAY_POSTHOG_HOST`, `MAY_POSTHOG_IOS_KEY` or `MAY_POSTHOG_IOS_HOST` would otherwise count. Don't create any of them at those levels. The host allowlist limits what a stray value can do, but a stray key would still send crash reports to another PostHog project.
- **`may-native-compile-pr`** (proposed; [compile of a pull request head](#ios-compile-of-a-pull-request-head); no required reviewer, main only): `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` (dedicated deploy keys, rotated when someone leaves may-app), `MAY_APP_PR_READ_TOKEN` (may-app pull requests: read, short-lived) and, optionally, `MAY_APP_PR_COMMENT_TOKEN` (may-app pull requests: read and write; [the compile's errors on the pull request](#the-compiles-errors-on-the-pull-request)). Each is bound in exactly one step.
- **`may-claude-review`** ([Claude code review of a may-app pull request](#claude-code-review-of-a-may-app-pull-request); no required reviewer, main only): `MAY_APP_REVIEW_TOKEN` (fine-grained, may-app only, expiring; Contents read, Pull requests read and write) in the fetch and review steps, and `MAY_REVIEW_CLAUDE_TOKEN` (a Claude Code OAuth token) in the review step alone.
- **The May CI GitHub App** (replaces the five pull-request PATs above and in the sections below; the PATs stay as the fallback until a follow-up drops them). In each of `may-native-compile-pr`, `may-ios-renders`, `may-ds-renders` and `may-claude-review`: variable `MAY_CI_APP_ID` (the app's ID) and secret `MAY_CI_APP_PRIVATE_KEY` (its private key, PEM). Each job that runs pull-request code mints its token in a step right after its first checkout, before any pull-request code, with the pinned `actions/create-github-app-token` (v3.2.0, by commit SHA), owner `May-Assistant`, one repository and the PAT's own permissions alone:

  | Mint (step id) | Replaces | Repository | Permissions |
  | --- | --- | --- | --- |
  | `ci-read` | `MAY_APP_PR_READ_TOKEN` | may-app | Pull requests: read |
  | `ci-comment` | `MAY_APP_PR_COMMENT_TOKEN` | may-app | Pull requests: write |
  | `ci-ds-read` | `MAY_DS_PR_READ_TOKEN` | may-design-system | Pull requests: read |
  | `ci-ds-comment` | `MAY_DS_PR_COMMENT_TOKEN` | may-design-system | Pull requests: write |
  | `ci-review` | `MAY_APP_REVIEW_TOKEN` | may-app | Contents: read, Pull requests: write |

  The private key is in the mint step's own `with:` alone, never a job's environment, another step or a file. The consumer step gets `steps.<id>.outputs.token || secrets.<PAT>`. With `MAY_CI_APP_ID` unset the mint is skipped and the PAT is used. With it set, a failed mint fails the job (no `continue-on-error`), so a misconfigured app never falls back quietly. The action revokes the token when the job ends. Install the app on may-app and may-design-system only, with Pull requests read and write and Contents read. Its comments and the review's STARTED marker appear as `<slug>[bot]`: the review's fetch step gets the mint's `app-slug` as `MAY_REVIEW_APP_SLUG`, because an installation token has no user for `GET /user`. `static_check.py` pins every mint, consumer and the slug (`unminted`), and refuses any other trace of the app. The May Renders app's one mint, in device-tests' `publish` job, follows the same rules ([Device tests](#device-tests-of-may-playground-against-staging)). An installation token lasts one hour, so the jobs that mint at their start and post at their end (`native-compile-pr.yml`'s `native`, `android-unit-pr.yml`'s `unit` and `ds-swift-compile.yml`'s `native`) time out at 55 minutes: a run that would outlive its token fails visibly instead of failing its post. `backend-unit-pr.yml`'s `backend` mints and posts the same way, and keeps may-app `backend-ci.yml`'s 20 minutes, well inside the hour.
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
- Node.js `v22.22.0` for linux x64 (the backend checks alone; `actions/setup-node` is not used either). Its SHA-256 equals the line in nodejs.org's `SHASUMS256.txt` for that release; the file's detached signature was not checked when the pin was recorded (no working gpg on that machine), which a later review can add.

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

### Stage 3: May production uploads to its internal track, and sign-only (done)

Samuel's decision (14 Oct): may-releases may upload the production May app to its Play **internal** track only (`"production": "internal"` in `PLAY_UPLOAD_TRACKS`). A passing production dry run comes first. Promoting a build from internal to production stays a manual step in Play Console. This change:

- adds `"production": "internal"` to `store.PLAY_UPLOAD_TRACKS` (and to `static_check.PLAY_UPLOAD_TRACKS`); admission and `play_upload.py` now admit May production, to `internal` only;
- binds every Android target to its own upload key and Play account in code (`store.ANDROID_UPLOAD_KEYS`): `sign.py` refuses another target's alias or certificate pin before signing, and `dry_run.py`, `play_upload.py` and `sign_only.py` refuse a signed bundle whose sole signer is not the target's key; `play_upload.py` and `dry_run.py` refuse another target's `MAY_PLAY_ACCOUNT`;
- adds the build-number floor (`store.MIN_BUILD_NUMBERS`: May production starts at 3, above its first release, vc2; see [Verification and upload](#verification-and-upload)), checked at admission (dry runs included), in the dry-run report, in `sign_only.py` and in `play_upload.py`, next to Play's own check that the versionCode exceeds every versionCode on every track;
- refuses in code any track write but one completed internal release of the new build, and any commit but `ERROR_IF_IN_REVIEW`;
- names, in the log, the Play request that did not complete;
- adds [sign-only](#sign-only), in its own `android-sign-only` job with no Play secret, for a CI-signed May production bundle to upload in Play Console by hand;
- reads every signed bundle's own manifest on the release side (`verify.check_aab_identity`) and refuses another package or versionCode, in `dry_run.py`, `sign_only.py` and `play_upload.py`;
- moves `static_check.py` to stage 3.

`confirm_upload.py` checks May production exactly as it checks Playground: package, versionCode, track `internal`, signed SHA-256 and committed edit ID.

### Stage 4: iOS uploads to internal TestFlight (this change)

Samuel's go, 19 Oct, in the build-lane session: "lift the iOS hold and ship TestFlight". may-app's `tools/release/ios.sh archive-unsigned` exists, and the macOS archive rehearsal passed twice (runs [39100919695](https://github.com/May-Assistant/may-releases/actions/runs/39100919695) and [39101524181](https://github.com/May-Assistant/may-releases/actions/runs/39101524181)). This change:

- takes the inert guard off `ios-build`, `ios-release` and the iOS upload step, which keep their platform and dry-run conditions;
- empties `gate.UPLOADS_HELD`, and admits an iOS `dry_run: false` only for `gate.TESTFLIGHT_TARGETS` (May and May Playground); `sign.py` exports for `testFlightInternalTestingOnly`, so neither can reach external testers or the App Store;
- moves `static_check.py` to stage 4: the guard must appear nowhere, and putting it back on any job or upload is a stage mismatch.

Nothing else changes: the same two approvals per run (`may-native-compile`, then `may-ios-playground-internal` or `may-ios-internal`), the same encrypted handoff, and the same loud-failure check after the upload. The iOS paths have not yet run against App Store Connect, so the first dispatch is a **dry run** (it builds, signs and verifies the IPA, and uploads nothing), then the upload. TestFlight processing and the internal group's testers are checked in App Store Connect: `ios_upload.py` stops at a successful `altool` upload, and May Playground's internal group (`8a709a78-…`) is empty until Samuel adds testers.

**altool's exit status is not an upload.** The first upload, run 39192306300 (May Playground build 2), went green: `altool` exited 0, yet App Store Connect then held no build upload and no build for app `6822072535`, and cleanup had deleted `altool`'s private log. So `ios_upload.py` now:

- prints a scrubbed excerpt of `altool`'s output before cleanup: the delivery UUID (only when it is a UUID), the number of lines naming an error (not counting "No errors") or a warning, and the error-code tokens on those lines (`ITMS-NNNNN`, negative numeric codes, and upper-case words joined by `_` or `.` such as `ENTITY_ERROR.ATTRIBUTE.INVALID`; a bare upper-case run, which could be a fragment of an ID, never matches). Never a raw line: this repository and its logs are public, and those lines carry runner paths and the key's ID. It also names the **kind** of each error line from a fixed list (file format, altool option refused or deprecated, authentication or API key, permission or key role, build number already used or conflict, no matching app or bundle ID, signing or provisioning, invalid binary or Info.plist, network or timeout, otherwise "unrecognised"), never the line itself;
- signs one App Store Connect API token (ES256, by `/usr/bin/openssl`, from the same upload key, 19 minutes, a minute under Apple's maximum) and polls `GET /v1/apps/{id}/buildUploads?filter[cfBundleVersion]=N` every 30 seconds for up to 15 minutes. Of the `IOS` build uploads for this build number and the IPA's `CFBundleShortVersionString` created after the step started (5 minutes' clock-skew margin), the newest decides: the step fails unless it is `PROCESSING` or `COMPLETE`. `FAILED` fails at once; `AWAITING_UPLOAD`, no reply, 429 and 5xx keep polling; 401, 403, 404 and other 4xx fail at once. Only GETs, only to `https://api.appstoreconnect.apple.com/v1/`, redirects refused; an error body is read only for its checked JSON:API codes (below). Any failure after `altool` finished (an unsigned token, a refusal, the timeout) also prints that the upload may be in App Store Connect and to check there before retrying, and a timeout names the fields it could not read (names, never values);
- prints that build upload's ID and state, and records the ID as the upload record's `store_id`. `confirm_upload.py` refuses an iOS record without one.

**`--upload-package`, with the app named (20 Oct).** TestFlight 3 (run 39363085983, May Playground build 3) failed the same way as build 2: `altool --upload-app` exited 0 with three error lines and no delivery UUID, and no build upload appeared. Since then `ios_upload.py`:

- uploads with `xcrun altool --upload-package <ipa> --type ios --apple-id <ASC app ID> --bundle-id <bundle> --bundle-version <build> --bundle-short-version-string <IPA's version> --apiKey … --apiIssuer …`. Apple's altool guide: "The `--upload-app` command is deprecated. Use the `--upload-package` command instead." ([Using altool](https://help.apple.com/asc/appsaltool/en.lproj/static.html); also `altool(7)`). On Xcode 26, altool can upload to the wrong app when two bundle IDs share a prefix unless `--apple-id` names it ([fastlane #29698](https://github.com/fastlane/fastlane/issues/29698), quoting Apple), and May's two do (`io.github.mayassistant.may`, `…may.playground`). The `--upload-app` path has also been reported failing on Xcode 26 with "isn't in the correct format. (259)" ([Kinematic-iOS #336](https://github.com/Shaggywize63/Kinematic-iOS/pull/336));
- fails at once, before the readback, when any line of altool's output names an error, since Xcode 26's altool exits 0 on a failed upload ([Bitrise App Store Connect step v2.0.0](https://discuss.bitrise.io/t/deploy-to-app-store-connect-application-loader-formerly-itunes-connect-v2-0-0/25564)). It still prints that the upload may be in App Store Connect.

**Diagnostics (20 Oct).** Build 4 (run 39417850241) failed fast with the error kinds "build number already used or conflict, unrecognised", one label covering two causes. The kinds now separate "conflict (409)" from "build number already used", and add "agreement or contract pending" and "missing app version record". Before altool, and again after any failure once it has run, the upload step prints the IPA's bundle ID, build number and version, then what App Store Connect holds for **both** apps (6822072535 and 6821782401):
- each app's bundle ID;
- its ten newest build uploads (build number, version, platform, state, created date, error codes);
- its ten newest builds (build number, processing state, uploaded date).

Only GETs are sent. Every value is printed only if it matches a fixed shape (digits and dots, a known state, an ISO date, a code token), and anything else prints as "?". A failed read prints its HTTP status alone. The diagnostics never fail the step.

**Apple's error codes (20 Oct).** Build 5 (run 39423195473) failed at the upload. altool printed three error lines with no code in them, and its kinds were "conflict (409), unrecognised". The build uploads diagnostics read returned HTTP 400 on both apps. Since then:

- the diagnostics list build uploads with `limit=200` and no `sort`, and order them newest first by `createdDate` themselves. The `sort=-createdDate` query got the 400, while the readback's query without a sort has been answered;
- any 4xx from the App Store Connect API, in the diagnostics or the readback, prints Apple's JSON:API `errors[]`: `code`, `status` and `source.parameter`, each checked against a fixed shape. The `title` and `detail` are never printed. An error body is read up to 64 KB;
- altool runs with `--output-format json`. Each entry of its report's `product-errors` counts as an error, so it fails the step at once, and prints one line with:
  - its codes: numeric codes, code-shaped `code` or `iris-code` values such as `STATE_ERROR.VALIDATION_ERROR`, and `ITMS-NNNNN` in its messages;
  - its HTTP status;
  - its `title`.

  A title is printed only when it is at most 160 characters of letters, digits, spaces and `,.'()&:;?!-`, and holds no UUID and not the key ID or issuer. Otherwise it prints "?". Each message is classified into the fixed kinds on its own. The delivery UUID is also read from the report's `details`. Code tokens are now read from every plain line of altool's output, since its error dictionaries span lines that don't say "error".

**Validation messages, and validation before the upload (20 Oct).** Build 6 (run 39481657122) failed with one product error: codes `-19241`, `STATE_ERROR.VALIDATION_ERROR`, status 409, title "Validation failed". The title doesn't say which rule. Since then:

- each product error's messages print under it when they name an Info.plist key (`CFBundle…`, `NS…UsageDescription`, `UI…`, `LS…`, `MinimumOSVersion`, `DT…`, `aps-environment`, `Info.plist`), an `ITMS-NNNNN` code, or a known error kind. Each message is scrubbed before it prints:
  - UUIDs, the key ID and the issuer are replaced;
  - a quoted value is kept only when it is empty or a list of Info.plist keys, codes or version numbers; otherwise it reads `'<quoted>'`;
  - any word with `/ \ @ ~` and similar characters reads `<path>`;
  - the message is cut to 300 characters, and printed only if what remains is plain text (otherwise "?").
- before the upload, `xcrun altool --validate-app -f <ipa> -t ios --apiKey … --apiIssuer … --output-format json` runs, and its output gets the same excerpt, headed "altool validation". Its product errors stop the step with nothing uploaded. If the validation can't run (an option refused, a non-zero exit), that is printed and the upload goes ahead, because the upload validates again.

**The IPA's icon (21 Oct).** Build 7 (run 39517516246) uploaded cleanly and was `PROCESSING`, but Samuel saw no app icon on it in App Store Connect. The encrypted IPA can't be inspected outside the release job, so `sign.py` now checks it there with `verify.ipa_icon`, right after the signed IPA's identity check. The check passes only if both of these hold:

- the app's `Info.plist` names `CFBundleIconName` `AppIcon`, either at the top level or in `CFBundleIcons` › `CFBundlePrimaryIcon`;
- the app's compiled asset catalog, `Assets.car`, exists and names `AppIcon`.

If either is missing, signing fails with a fixed message naming the missing part, and the IPA is deleted before any upload. When both hold, the log says so in one fixed line. Nothing from the IPA itself is printed.

TestFlight processing (the build itself) and the internal group's testers are still checked in App Store Connect.

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

In the Actions UI, choose **May protected store release (stage 4, Android internal and internal TestFlight uploads)**, **Run workflow** from `main`, and set the inputs above. A run that did not do what it was asked is **red**: admission refuses what it does not admit, the upload step fails on any Play refusal or error (deleting the edit), and the loud-failure checks fail the job whenever the upload, or the sign-only publication, did not succeed.

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


**Linux Swift checks first.** A `swift-checks` job runs before the Mac compile, and the compile needs it. It runs on ubuntu with the same pull request check and fetch, and the same secrets in the same steps. It runs may-app's `tools/ios/test_swift_*.py` (may-app #1050: an init's labels out of order, or a missing import, against the pinned May DS) with `python3 -I -m unittest discover -s tools/ios -p 'test_swift_*.py'`, then its parity checks, `tools/ios/test_parity_*.py` (a `screens.json` row iOS doesn't draw, such as `test_parity_snapshots`), with the same command and `-p 'test_parity_*.py'`. Each pattern runs only when the commit has a file for it, and a failure in either fails the job. Discovery must start at `-s tools/ios`: neither `tools/` nor `tools/ios/` is a package, so `-s tools` finds no test.
- The log shows one fixed line and a count.
- The failing checks go only to the may-app pull request, as one comment (`swift_checks.py`, the comment token in its post step alone).
- A commit with no such check is skipped and the compile runs.
- A failure or a timeout fails the job, so the Mac compile never starts.

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
- **The compile** (`compile.py --record-outcome`, no `env` at all) builds `MayPlayground` for the iOS Simulator, Release, `CODE_SIGNING_ALLOWED=NO`. A failure of the build itself (XcodeGen or `xcodebuild`) is recorded in a private file and does not fail this step, so the post can report it; every check before the build (keys on disk, secrets, Xcode, XcodeGen's hash, free space) still fails here. `native-compile-draft.yml` runs `compile.py` without the flag and is unchanged.
- **The post** (`compile_pr_errors.py`, `MAY_APP_PR_COMMENT_TOKEN` alone) puts the compile's errors on the pull request; see below. A failed compile then fails the job.
- **What the job holds, and why that is enough:** on one GitHub-hosted macOS job, everything the job references is available to the pull request's build code (XcodeGen, SwiftPM and Xcode build phases run it). The runner gives the job's user passwordless `sudo`, and GitHub hands the runner every secret the job references when the job starts. So the design goal is that the job holds **nothing a may-app writer doesn't already have**:
  - two read-only deploy keys (may-app, may-design-system);
  - a pull-request read token;
  - a pull-request comment token (may-app pull requests: read and write), once Samuel adds it;
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

#### The compile's errors on the pull request

The compile's log stays private, so on its own a failed run says only that it failed. The post step gives the lane the reason, the same way `ds-swift-compile.yml` does for may-design-system (it reuses `ds_compile.py`'s extraction, redaction, dedupe, cap, fence, marker and comment loop):

- It takes `MAY_APP_PR_COMMENT_TOKEN` out of its environment at once and refuses to run if any other known secret is present. The compile step before it holds no secret, so the pull request's build code never sees this token in its environment.
- **Without the token** (until Samuel adds it) it posts nothing, prints the one fixed line `May iOS compile: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted`, and passes or fails the job with the compile. The workflow works as before.
- From the private log it keeps only `<path>:<line>:<col>: error: <message>` lines whose path is inside the may-app checkout, rewritten repo-relative. It drops warnings, notes, source and caret lines, and errors outside the checkout (derived data, the SDK, packages). It also drops errors in the design system that `prepare.py` checks out inside it (`android/build-deps/`), which `ds-swift-compile.yml` reports. Token-shaped text becomes `[redacted]`, and a body holding the token in any spelling is refused. It dedupes, posts at most 100 and counts the rest.
- It checks the checkout is still `source_sha`, then reads the pull request again (open, against may-app `main`, from may-app itself, head still `source_sha`). It reads up to 10 pages of its comments and posts nothing if one already carries `<!-- may-ios-compile-pr head=<sha> -->`. Otherwise it posts **one** comment, with the head SHA, Xcode, the scheme, this run's URL, the marker and the footer. A passing compile posts that it compiles, as ds-compile does.
- It can reach only `GET /repos/May-Assistant/may-app/pulls/<number>`, `GET …/issues/<number>/comments?per_page=100&page=<1–10>` and `POST …/issues/<number>/comments`. Any other method or path is refused before a request is made.
- **What reaches the public log:** fixed lines, the pull request number and the number of errors, for example `May iOS compile failed; 12 errors posted to may-app #42`. There is no step summary, output, artifact or cache. `static_check.py` pins the step (its command, its one secret, no `GH_TOKEN`, no condition or `continue-on-error`, a compile with no `env`). It also lints `compile_pr_errors.py` with the same output rules as `ds_compile.py`: each `print` is a literal or carries only the number, the count and the Xcode version, exits are fixed messages, and stdout, descriptors, dynamic lookups and logging are refused.
- **Residual risk (the same as the compile's):** the build runs the pull request's own code on the same runner before the post. That code cannot read the token from its environment, but with the runner's `sudo` it could change the files the post step later runs (this repository's checkout, Python), or write a fake log or outcome. So treat the token as available to a may-app writer. It is scoped so that this gives them nothing new: may-app only, pull requests read and write, which every may-app writer already has. Hosted runners are discarded after the job. Popping the token clears only Python's copy: code left running by the build could still read it from the post step's process, so rotate it if a may-app writer leaves (security review of 2cf86ce, #1).
- **What the post's result can and can't prove:** the outcome file and log are written while the pull request's code runs, so a hostile build can make this job pass, post "passed", or leave its log as a link (only a regular file is read). The local checkout's head check is a sanity check for the same reason; the pull request's head, read again over the API, is the check that counts. Don't let a merge rest on this job alone: it shows errors to the author, it doesn't certify code (security review of 2cf86ce, #2, #6).

**What Samuel sets up before the first run** (admission denies until then). Create **environment `may-native-compile-pr`** with:
- **no required reviewers**;
- administrator bypass off (**Allow administrators to bypass** unchecked);
- **Deployment branches: Selected branches**, with only `main`.

Its secrets, and nothing else:
- `MAY_APP_READ_KEY`: a **new** read-only deploy key on may-app;
- `MAY_DS_READ_KEY`: a **new** read-only deploy key on may-design-system;
- `MAY_APP_PR_READ_TOKEN`: may-app only, Pull requests: read and Metadata: read, short-lived (see above);
- `MAY_APP_PR_COMMENT_TOKEN` (**new, optional**; the workflow runs without it and posts nothing): a fine-grained personal access token, resource owner May-Assistant, **only** the may-app repository, **Pull requests: Read and write** (Metadata: read is always included), expiring **15 Nov** like the others. Comments appear under its owner's account.

No repository secret is needed.

### iOS archive rehearsal of a pull request head

`ios-archive-rehearsal.yml` rehearses may-app's `tools/release/ios.sh archive-unsigned` (may-app #116) on a hosted macOS runner before the iOS store jobs are switched on. It is the README's "macOS rehearsal" for the build half only: export, distribution signing and TestFlight stay in the inert `ios-release` job.

```
gh workflow run ios-archive-rehearsal.yml -f pr_number=<may-app PR number> -f source_sha=<that PR's current head SHA> -f target=playground -f build_number=2 --repo May-Assistant/may-releases --ref main
```

- **Admission, pull request check and fetch** are exactly the pull-request compile's: `gate.py --action compile-pr` (main only, manual, `may-native-compile-pr` with no reviewer, admin bypass off, main only), then `--check-pull` with `MAY_APP_PR_READ_TOKEN` (open, against may-app `main`, from may-app itself, head exactly `source_sha`), then `prepare.py --platform ios --target <target> --pr-number` with the two read-only deploy keys. `target: production` is refused by `prepare.py` while the iOS and Android design-system pins differ.
- **The archive** (`ios_rehearsal.py archive`, no secret, no `GH_TOKEN`) refuses while a key or host file is on disk or any known secret is in its environment, checks `xcodebuild -version` is exactly Xcode 26.3, build 17C529 (may-app `ios-ci.yml`'s pin), and runs `bash tools/release/ios.sh archive-unsigned` with exactly `store.py`'s environment for iOS plus `MAY_REHEARSAL=1`. **Rehearsal only:** ios.sh requires the commit in `origin/main`'s history, which a pull request head is not, so the script first points the runner's own `refs/remotes/origin/main` in the private checkout at the head (as may-app's `ios-release-rehearsal.yml` does). Nothing is pushed, and the summary says so.
- **What stays private:** everything ios.sh prints goes to `may-private/ios-rehearsal.log`, and the cleanup always removes the source, the design system, that log, the archive and ios.sh's work folders. ios.sh's own `may-release-record:` line names the design-system revision, the app version and a digest of `Package.resolved`, so it is **not** published: the script checks it against the request (platform, target, bundle ID, SHA, build number, team, the iOS pin `prepare.py` checked out, Xcode, product, `rehearsal: true`), checks the product folder is exactly `May.xcarchive`, and recomputes the archive's tree SHA-256 to compare with the record's.
- **What is published:** one artifact, `may-ios-archive-rehearsal-<attempt>`, kept 7 days, holding one file, `ios-archive-rehearsal.json`. Every value in it is a dispatch input (pull request, SHA, target, build number), a constant of this repository (bundle ID, Xcode, fixed wording), one of seven fixed reasons, ios.sh's integer exit status, or the recomputed archive SHA-256. `ios_rehearsal.py record` re-reads it after the build and refuses to publish anything but exactly that shape; `verdict` then fails the job unless the archive was made and every check passed. A failed run still publishes its summary, with the fixed reason and exit status, before failing.
- **Pinned by `static_check.py`:** the workflow whole (inputs, permissions, the seven commands, each secret in its one step, no env but the inputs on the archive and after it, exactly the one upload step) and the output lint of `ios_rehearsal.py` (fixed prints, plus only `int(status)`).
- **Residual risk (as the pull-request compile):** ios.sh runs the pull request's own build code on a runner that holds the deploy keys and the pull-request read token for the job. That code could also rewrite the summary between the check and the upload. Only same-repository pull request heads are admitted, so this publishes nothing a may-app writer does not already hold.

### Android unit suites at a pull request head

`android-unit-pr.yml` runs may-app's Android unit suites on an open may-app pull request, because may-app's own Actions are blocked: the client core's JVM tests and the app's Robolectric tests for May Playground and May production (debug).

```
gh workflow run android-unit-pr.yml -f pr_number=<may-app PR number> -f source_sha=<that PR's current head SHA> --repo May-Assistant/may-releases --ref main
```

- **Admission, pull request check and fetch** are exactly the pull-request compile's: `gate.py --action compile-pr` (main only, manual, `may-native-compile-pr`: no reviewer, admin bypass off, main only), then `--check-pull` with `MAY_APP_PR_READ_TOKEN` (open, against may-app `main`, from may-app itself, head exactly `source_sha`), then `prepare.py --platform android --target playground --pr-number` with the two read-only deploy keys, which places the design system at its Android pin. No new environment is needed; the post uses `may-native-compile-pr`'s existing `MAY_APP_PR_COMMENT_TOKEN`.
- **The suites** (`android_unit.py`, no secret and no `GH_TOKEN`; it refuses if any known secret is in its environment) use the pinned Temurin JDK 21 that `toolchain.py jdk` installed. They find it by path, so no value is carried to later steps. The script then installs the SDK packages the app builds with (`platform-tools`, `platforms;android-37.0`, `build-tools;36.0.0`, with one retry) and runs may-app's `android/scripts/fetch-design-system.sh`, which rechecks that the placed design system is exactly `design-system.lock`'s revision and is clean. Test results committed to the pull request are refused, and then Gradle runs the three tasks with `--continue`, so one failing suite does not hide another.
- **What is published:** one line per suite, `May Android unit suite <core|playground|production>: passed|failed, <n> tests, <n> failed, <n> skipped`, or `did not run` when its build stopped before a report. Failures and errors both count as failed. The counts are summed from the JUnit reports' `<testsuite>` attributes alone: no test name, message, class or Gradle output. Everything else goes to `may-private/android-unit.log`, and the cleanup always removes the source, the design system, that log, the Gradle home and the pinned JDK. The job fails unless every suite reported at least one test, none failed, and Gradle finished cleanly within 50 minutes.
- **The failing tests go to the pull request, never here.** The suites step runs with `--record-outcome`: a red suite is recorded in `may-private` (each failing test's class and method, its kind, the first line of its message, and where in its own source it failed, as a repo-relative `path:line`) and doesn't fail the step. Absolute runner paths are replaced, token-shaped strings are redacted, and a report with a DOCTYPE or over 8 MB is counted unreadable rather than parsed.
  - `android_unit_post.py` then works like the iOS compile's post. It runs in its own step, holding `MAY_APP_PR_COMMENT_TOKEN` alone, reads the pull request again (still open, from may-app, at the tested head), and posts one comment with a per-suite table and up to 100 failing tests in a fence, unless this head's marker is already there. It never posts a body that holds the token.
  - It prints only fixed lines, the pull request number and the count. It fails the job when the suites failed, with or without the token.
- **Locating failing core tests without the comment token** (`core_breakdown: true`, off by default). Each failing core test case is also printed by numbers alone: `report <i> of <n> by class name, test case <j> of <m> by name failed`. i is its report's place among every core `TEST-<class>.xml` sorted by file name, readable or not (an unreadable report keeps its place, and their count is printed). j is its place among that report's test cases sorted by name, never run order. Both orders come from the source, so anyone with the same may-app commit can map them to a class and method. Parameterized or generated case names show only in a local run's reports. A filter by class name was not used, because dispatch inputs are public in this repository too.
- **Pinned by `static_check.py`:** the whole workflow (two inputs and the optional `core_breakdown` boolean, permissions, Linux runners, the seven commands, each secret in its one step, only `MAY_SOURCE_SHA` on the suites, exactly the inputs and the comment token on the post, the run name, no artifact or `--export`, and no step summary but the post's counts) and the output lint of `android_unit.py` and `android_unit_post.py` (fixed prints, plus only the suite name, verdict and integer counts, and the pull request number and failing-test count).
- **Residual risk (as the pull-request compile):** the suites run the pull request's own build and test code on a runner whose job holds the deploy keys and the pull-request read token. That code could also write reports that claim a pass. Only same-repository pull request heads are admitted, so a pass is as trustworthy as a may-app writer, and nothing is published that such a writer does not already hold.
- **Step summary:** the post also writes each suite's test, failure and skip counts and the failing-test total to the step summary, with or without the comment token (numbers and the suite names only, pinned by `static_check.py`).
- **Run name:** `May Android unit suites for may-app #<number> at <head SHA>`, the dispatch's own two inputs. The schedule below reads it.

### Android unit suites on a schedule

`android-ci-schedule.yml` runs every 15 minutes (and can be dispatched by hand, with no input). It dispatches `android-unit-pr.yml` for **at most one** may-app pull request head a run, and it decides only which head goes next: the dispatched run admits and checks that head again itself.

- **admission** (no environment, `actions: read`): `android_ci_schedule.py admit` checks that the run is this repository's schedule or a dispatch from `main`, and that `may-native-compile-pr` keeps its policy (no reviewer, admin bypass off, main only). A missing environment is denied on its 404.
- **pick** (in `may-native-compile-pr`, `contents: read` and `actions: write`), in three steps, so no step holds two credentials:
  1. `runs`, with `GITHUB_TOKEN` alone, lists `android-unit-pr.yml`'s runs created in the last 30 days. If one is queued or running, nothing is picked: a second pending run in its concurrency group would cancel the first. Otherwise each run name gives a pull request number and head that has already run.
  2. `pick`, with `MAY_APP_PR_READ_TOKEN` alone, lists may-app's open pull requests against `main`, oldest first. It keeps those from may-app itself (never a fork) whose head has not run and that were updated in the last 14 days (so months-old drafts never crowd out newer work), and reads their changed files (at most 40 heads a run). The first one with a change under `android/`, `contracts/`, `shared/` or `gradle/`, or to `tools/release/android.sh`, is the pick. `android/design-system.lock` is under `android/`. A renamed file counts by either path.
  3. `dispatch`, with `GITHUB_TOKEN` alone, sends one `workflow_dispatch` of `android-unit-pr.yml` on `main` with exactly that number and head SHA.
- **One run per head.** A head with a run in the last 30 days is skipped, whatever its result; dispatch `android-unit-pr.yml` by hand to run it again. The 30 days are longer than the 14-day staleness cut, so a pull request whose only activity is a unit run's own comment goes stale before its head could run again (no daily re-run once comments post). A new push, force-pushes included, is a new head and runs on a later tick. Repeated pushes cost at most one dispatch a tick, and the 75-minute unit run, one at a time, bounds them further.
- **What is printed:** fixed lines, counts, and the picked number and head SHA. Those are already public in `android-unit-pr.yml`'s run name and log. Nothing read from may-app (titles, branches, file names) is printed, and only the number and SHA are carried between steps, in `$RUNNER_TEMP/may-android-ci`, which is always removed.
- **Threat model.**
  - `MAY_APP_PR_READ_TOKEN` is the existing fine-grained read token in `may-native-compile-pr`. It reads may-app's pull requests and their file lists here, and nothing else (`app_get` refuses any other path). It is reachable only by jobs in that environment, whose branch policy is `main` alone. This workflow's only triggers are `schedule` and `workflow_dispatch`, both running `main`'s code, and `static_check.py` refuses `pull_request`, `pull_request_target`, `push` or any input here. A pull request in this repository (`control-check.yml`) names no environment and no secret, and a fork gets no secrets.
  - `actions: write` is on `GITHUB_TOKEN` in `pick` alone, which runs only `main`'s code. The token could, in principle, dispatch, cancel, re-run or delete any workflow run in this repository. The tool uses it for one request: `control_dispatch` refuses any path but `actions/workflows/android-unit-pr.yml/dispatches`, any ref but `main`, and any input but a pull request number and an exact SHA, before it sends anything. The only other use is `control_get`, which reads that workflow's runs. `static_check.py` pins that `control_dispatch` is the tool's only request with a body or method, and that it checks the path first. A dispatch of a store workflow would still stop at Samuel's required review.
  - The dispatched `android-unit-pr.yml` is unchanged apart from its run name. It is triggered by `github-actions[bot]` and runs the same gate, pull request check, fetch and suites as a manual dispatch.
- **Pinned by `static_check.py`:** the whole workflow (two triggers, the 15-minute cron, one run at a time, the two jobs, permissions, the four commands and the cleanup, each credential in its own steps), `RUN_NAME` and `DISPATCH_PATH`, the write rule above, and the output lint.

### Backend checks at a pull request head

`backend-unit-pr.yml` runs may-app's backend CI on an open may-app pull request, because may-app's own Actions are blocked by billing and its `backend-ci.yml` is switched off. It is that workflow's job, moved here: the same toolchain, stages and timeouts.

```
gh workflow run backend-unit-pr.yml -f pr_number=<may-app PR number> -f source_sha=<that PR's current head SHA> --repo May-Assistant/may-releases --ref main
```

- **Admission, pull request check and fetch** are exactly the Android unit suites': `gate.py --action compile-pr` (main only, manual, `may-native-compile-pr`: no reviewer, admin bypass off, main only), then `--check-pull` with `MAY_APP_PR_READ_TOKEN` (open, against may-app `main`, from may-app itself, head exactly `source_sha`), then `prepare.py --platform android --target playground --pr-number` with the two read-only deploy keys. That fetch also places the design system at its Android pin, which the backend never reads; it is kept so the lane adds no new way to fetch. **No new environment, secret or variable is needed**: the post uses `may-native-compile-pr`'s existing `MAY_APP_PR_COMMENT_TOKEN` (or the May CI app's `ci-comment` mint).
- **The job** keeps `backend-ci.yml`'s shape: `ubuntu-24.04`, 20 minutes, and its service container, `postgres:16.15` pinned by digest, superuser `may_admin` with trust auth on the runner's loopback, healthy before any step runs. `toolchain.py node` installs the pinned, checksum-verified Node 22.22.0 (with its own npm) under `$RUNNER_TEMP/may-tools`, and `backend_unit.py` finds it by path. No `actions/setup-node`, and no npm cache: this repository allows no Actions cache, so npm's cache is a folder under `$RUNNER_TEMP` that the cleanup removes.
- **The stages** (`backend_unit.py`, no secret and no `GH_TOKEN`; it refuses if any known secret is in its environment), in `backend-ci.yml`'s order:
  1. **toolchain**: Node is exactly `v22.22.0`, npm runs, and the service answers `SHOW server_version` with exactly 16.15. A mismatch fails the step (a fault of the runner, not the pull request).
  2. **install**: `npm ci --no-audit --no-fund` in `backend/`. A failed install stops the stages after it.
  3. **typecheck**: `npm run typecheck`.
  4. **migrations**: as `backend-ci.yml`'s "Migrations from scratch": create `may_owner` (`NOSUPERUSER NOBYPASSRLS CREATEROLE`) and the `may_ci_migrate` database it owns, run `npm run -s migrate` twice as `may_owner`, and require every `backend/migrations/NNN_name.sql` applied exactly once in `schema_migrations`, the first run to say `applied <n> migration(s)` and the second to end with `schema up to date`.
  5. **tests**: `npm test` with `MAY_TEST_ADMIN_URL` pointing at the service, stopped after 15 minutes (`backend-ci.yml`'s step timeout; `npm test` already bounds each file at 120 s).

  A failed typecheck or migration still runs the stages after it, so one red stage does not hide another.
- **What is published:** one line per stage, `May backend <stage>: passed|failed|did not finish in time|did not run (…)`, and one line of counts, `May backend tests: <n> tests, <n> passed, <n> failed, <n> skipped`, read from `node --test`'s own closing summary in the private log (failed counts `fail` and `cancelled`; skipped counts `skipped` and `todo`). No test name, message, path or log line. Every tool writes to private logs under `may-private`, and the cleanup always removes the source, the design system, those logs, `node_modules` (inside the source), npm's cache and the pinned Node. `backend-ci.yml`'s log upload on failure is **dropped**: this repository's artifacts are public. The job fails unless every stage passed with at least one test.
- **The failures go to the pull request, never here.** The checks step runs with `--record-outcome`: a red stage is recorded in `may-private` and doesn't fail the step. The record holds each failing test's name, the first line of its error and its location (from `node --test`'s TAP report, leaving out suites and files that failed only because a test inside them did), `tsc`'s error lines, and, for a failed stage with neither, its last 40 log lines. Paths are made repo-relative, other runner paths replaced and token-shaped strings redacted.
  - `backend_unit_post.py` then works like `android_unit_post.py`. It runs in its own step, holding `MAY_APP_PR_COMMENT_TOKEN` alone, reads the pull request again (still open, from may-app, at the tested head), and posts one comment with a per-stage table, the test and migration counts and the detail in fences (at most 100 failing tests and 100 type errors, cut to fit), unless this head's marker (`may-backend-unit-pr`) is already there. It never posts a body that holds the token.
  - It prints only fixed lines, the pull request number and two counts (failing tests and type errors), and writes each stage's verdict and the test counts to the step summary (numbers and fixed words only). It fails the job when the checks failed, with or without the token.
- **Pinned by `static_check.py`:** the whole workflow (two inputs, permissions, Linux runners, the service block exactly, the seven commands, each secret in its one step, only `MAY_SOURCE_SHA` on the checks, exactly the inputs and the comment token on the post, the run name, the 20-minute timeout, no artifact, `--export` or step summary but the post's counts) and the output lint of `backend_unit.py` and `backend_unit_post.py` (fixed prints, plus only the stage name, verdict and integer counts, and the pull request number and the two counts).
- **Residual risk (as the Android unit suites):** `npm ci` runs the dependencies' install scripts and the tests run the pull request's own code on a runner whose job holds the deploy keys and the pull-request read token. That code could also write a summary that claims a pass. Only same-repository pull request heads are admitted, so a pass is as trustworthy as a may-app writer, and nothing is published that such a writer does not already hold.
- **Run name:** `May backend checks for may-app #<number> at <head SHA>`, the dispatch's own two inputs. The schedule below reads it.

### Backend checks on a schedule

`backend-ci-schedule.yml` runs every 15 minutes (and can be dispatched by hand, with no input). It is `android-ci-schedule.yml` for the backend lane: `backend_ci_schedule.py` reuses `android_ci_schedule.py`'s run check, list reader, staleness rule, may-app reader and dispatch payload, and `static_check.py` holds both schedules to the same rules (`SCHEDULES`). It dispatches `backend-unit-pr.yml` for **at most one** may-app pull request head a run, and decides only which head goes next: the dispatched run admits and checks that head again itself.

- **admission** (no environment, `actions: read`): `backend_ci_schedule.py admit` checks the run (this repository's schedule, or a dispatch from `main`) and `may-native-compile-pr`'s policy.
- **pick** (in `may-native-compile-pr`, `contents: read` and `actions: write`), in three steps, so no step holds two credentials: `runs` (`GITHUB_TOKEN` alone) lists `backend-unit-pr.yml`'s runs of the last 30 days and waits while one is queued or running; `pick` (`MAY_APP_PR_READ_TOKEN` alone) takes the oldest open may-app pull request against `main`, from may-app itself, updated in the last 14 days, whose head has not run, with a change under `backend/` or to may-app's `.github/workflows/backend-ci.yml` (`backend-ci.yml`'s own paths filter; a renamed file counts by either path; at most 40 heads' files read a run); `dispatch` (`GITHUB_TOKEN` alone) sends one `workflow_dispatch` of `backend-unit-pr.yml` on `main` with exactly that number and head SHA.
- **One run per head**, as the Android schedule: a head with a backend run in the last 30 days is skipped, whatever its result; dispatch `backend-unit-pr.yml` by hand to run it again. A new push is a new head.
- **What is printed:** fixed lines, counts, and the picked number and head SHA, which `backend-unit-pr.yml`'s run name makes public anyway. Only the number and SHA are carried between steps, in `$RUNNER_TEMP/may-backend-ci`, which is always removed.
- **Threat model:** the Android schedule's, unchanged. `control_dispatch` refuses any path but `actions/workflows/backend-unit-pr.yml/dispatches`, any ref but `main` and any input but a pull request number and an exact SHA, before it sends anything; `static_check.py` pins that it is the tool's only request with a body or method, and that the tool reads may-app only through `android_ci_schedule.app_get`.

### Design-system Swift compile of a pull request head

`ds-swift-compile.yml` lets the iOS lane see why may-app fails to compile against a may-design-system commit (the app compile keeps its log private). It compiles may-design-system's Swift package alone, at the head of an **open may-design-system pull request** against `claude/may-integration` or `main`, from may-design-system itself, and posts the result as **one comment on that pull request** (Samuel's decision). It has no signing, store call, artifact or approval.

```
gh workflow run ds-swift-compile.yml -f ds_sha=<that PR's current head SHA> -f ds_pr=<may-design-system PR number> --repo May-Assistant/may-releases --ref main
```

**This repository is public and may-design-system is private**, so nothing derived from the design system (paths, file or scheme names, compiler messages, logs) reaches this run's log, step summary, artifacts or outputs. The log shows fixed lines, the dispatch's own inputs (commit and pull request number), the dispatcher's login and the number of errors, for example `DS Swift compile failed; 12 errors posted to may-design-system #40`. `static_check.py` pins the workflow whole and lints `ds_compile.py` against accidental output: each `print` is a literal or carries only the pull request number, the error count and the Xcode version; `print` is never aliased; every subprocess names where its output goes; exits carry only fixed messages; and `sys.stdout`, `getattr`, `os.system` and the like, descriptors 1 and 2, debuggers and logging are refused. It is a lint against accidents, not a proof: Python has more ways to reach a descriptor than any rule list, and the design system's own build code runs on the same runner (below).

- **Admission** (`gate.py --action ds-compile`, no environment) checks the dispatch and inputs, and that `may-ds-renders` exists with administrator bypass off, a custom branch policy of exactly `main`, and **no** required-reviewer rule.
- **The pull request check** (`--check-pull`, first step on the `may-ds-renders` runner) reads the pull request with `MAY_DS_PR_READ_TOKEN`: open, base `claude/may-integration` or `main` of may-design-system, head in may-design-system itself, head exactly `ds_sha`. It prints nothing read from the pull request.
- **The fetch** (`ds_compile.py fetch`, `MAY_DS_READ_KEY` alone) fetches `refs/pull/<number>/head` over SSH exactly as `prepare.py` does (a `0600` key file and pinned `known_hosts`, both removed in a `finally`), requires it to be exactly `ds_sha`, and checks it out.
- **The compile** (`ds_compile.py compile`, no `env` at all) refuses to start while a key or host file is on disk or any known secret is in its environment, checks for Xcode 26.3, and builds the package-wide `*-Package` scheme (otherwise each scheme) for `generic/platform=iOS Simulator`, Release, `CODE_SIGNING_ALLOWED=NO`, with derived data and packages under `$RUNNER_TEMP`. `xcodebuild` gets only `PATH`, `HOME`, the user and locale, `DEVELOPER_DIR` and `TMPDIR` (no GitHub Actions file paths), and all its output goes to a private log. The outcome is recorded in a private file; a failed build, including one that times out or whose tool will not start, is recorded as failed and does not fail this step, so the post still reports it.
- **The post** (`ds_compile.py post`, `MAY_DS_PR_COMMENT_TOKEN` alone, taken out of the environment at once) keeps only `<path>:<line>:<col>: error: <message>` lines whose path is inside the checkout, rewritten repo-relative; warnings, notes, source and caret lines, errors outside the checkout and everything else are dropped. It dedupes them, posts at most 100 and counts the rest, reads the pull request again (still open, same base rules, head still `ds_sha`), reads its existing comments (up to 10 pages of 100) and posts nothing if one already carries this head's marker (the log then says it was already posted), and otherwise posts one comment on it (`POST /repos/May-Assistant/may-design-system/issues/<number>/comments`, the only write it can make), with the head SHA, this run's URL, a hidden `<!-- may-ds-swift-compile head=<sha> -->` marker and the footer. A passing compile posts that it compiles. A failing compile then fails the job.
- **The cleanup** always removes `may-ds-source`, `may-ds-private`, `may-ds-derived` and `may-ds-packages` under `$RUNNER_TEMP`.
- **What the job holds:** as with the pull-request compile above, the design system's own build code (SwiftPM, macros, plugins) runs on a runner that holds every secret the job references. Those are a read-only deploy key and two tokens scoped to may-design-system, which a may-design-system writer already has the equivalent of. The steps add hygiene against accidents, not a boundary against a hostile pull request; in particular a pull request's own build code could still write to the run deliberately.

**What Samuel sets up before the first run** (admission denies until then). Environment **`may-ds-renders`** with no required reviewers, administrator bypass off, and deployment branches **Selected branches: `main`** only. Its secrets:
- `MAY_DS_READ_KEY`: a read-only deploy key on may-design-system (a new one, not another environment's);
- `MAY_DS_PR_READ_TOKEN`: may-design-system only, Pull requests: read and Metadata: read, short-lived;
- `MAY_DS_PR_COMMENT_TOKEN` (**new**): a fine-grained personal access token, may-design-system only, Pull requests: read and write (Metadata: read is always included), with an expiry. Comments appear under its owner's account.

### Claude code review of a may-app pull request

`may-app-review.yml` runs the pinned code-review command (the same `anthropics/claude-code` commit as `claude-code-review.yml`) on an **open, ready-for-review may-app pull request**, and posts its comments on that pull request: inline comments on the reviewed head, or a "No issues found" summary. Each comment ends with May's attribution footer and a hidden `<!-- may-app-review head=<sha> -->` marker. It exists because may-app's own Actions are blocked by billing (Samuel, option B).

It also runs **every 15 minutes on its own** (Samuel, 17 Oct), reviewing at most one pull request each time (below). To review one now, or again:

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
- **Admission** (`review.py admit`, no environment) checks the run (from main, by manual dispatch or the schedule), the pull request number (a dispatch's: digits only, no leading zero; the schedule takes none) and `may-claude-review`'s policy: administrator bypass off, a custom branch policy of exactly `main`, and **no required reviewer** (`gate.validate_no_reviewer_environment`, as for `may-native-compile-pr`). A reviewer rule denies the run, since it would only hold it. A missing environment is denied on its 404, never auto-created.
- **No approval per run** (Samuel, 17 Oct). Who can start a run is unchanged: a dispatch needs write access here, and the schedule runs only this repository's `main`. Runs never overlap and are never cancelled by the next (concurrency group `may-app-review`). If one is still running, GitHub keeps one more waiting and replaces an older waiting one, so a dispatch can be replaced by a scheduled run; dispatch it again.
- **The schedule's pick** (`review.py fetch`, on the environment's runner, `pick`). It reads may-app's open pull requests against `main`, oldest first, and takes the first that is ready for review, from a may-app branch (never a fork), opened by an owner or member (`author_association`), and whose current head has no **started marker** posted by the review token's own user (anyone else's copy of the line counts for nothing). It looks at every open pull request, so reviewed heads never hold the queue up, and passes over one whose conversation can't be read rather than stopping. It prints nothing it read, and the log names a pull request by number and head only, never its author. With nothing to pick, the run stops there: nothing is fetched, installed or reviewed.
- **The fetch** (`review.py fetch`) reads the pull request with `MAY_APP_REVIEW_TOKEN`. The pull request must be open, ready for review, against may-app `main` and from may-app itself. It fetches exactly that head into `/tmp/may-review-view/source`. The token goes to git through its environment, never its arguments or config, and git's output is discarded. The review job has `contents: read` alone. Only admission reads the environment's policy (M3).
- **What the model reads of the conversation:** only comments, reviews and review comments by may-app owners and members. Anyone else's are left out of `pull.json`, which counts them (`left_out_from_others`).
- **The review** (`review.py review`) refuses a head that moved since the fetch, then posts the **started marker** on the pull request, a comment reading exactly `May review started at <head SHA>`, before Claude Code runs. So the schedule reviews each head at most once, even when the review fails or finds nothing to post; a new push is a new head. A marker that can't be posted stops the run before anything is reviewed. A manual dispatch reviews its head whatever markers it has. Then it writes the pull request's files and runs the command.

**Before relying on it:** after it merges, one real run on a may-app pull request, then a read of that run's public log to confirm nothing private appears.

**What Samuel sets up before the first run** (admission denies until then). Create **environment `may-claude-review`** with:
- **Required reviewers:** none (Samuel, 17 Oct). Created with Samuel as its reviewer before that, it needs him removed: Settings, Environments, `may-claude-review`, untick **Required reviewers**, **Save protection rules**. Until then admission denies every run, the schedule's included;
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

### iOS renders of May Playground (parity with Android)

`ios-renders.yml` renders May Playground in the simulator and publishes the images to the private repository `May-Assistant/may-renders`, never here. It runs on a may-app main commit, or on the current head of an open may-app pull request (`pr_number`). An optional `ds_sha` renders against another may-design-system commit, which must be in its `main` or `claude/may-integration` history. It runs on the same runner and Xcode as the iOS compile: `macos-15`, Xcode 26.3, an iPhone 16 Pro simulator, light and dark. It never signs anything.

This repository is public and may-app is private, and anyone signed in to GitHub can download a public repository's artifacts. So the images are never uploaded here in the clear, and the log shows only counts and fixed words. The renders go to `may-renders`, not a may-app branch: may-app is on GitHub's free plan, where no branch can be protected, so a write deploy key there could push to its `main`. The workflow has three jobs:

1. **admission** (`gate.py --action renders`) checks the inputs and the environment `may-ios-renders`, which allows main only and has no required reviewer.
2. **render**, on macOS, holds no secret once may-app's code runs:
   - `prepare.py` fetches the commit with the two read-only deploy keys, which only that step holds.
   - `renders.py run` builds with the pinned XcodeGen and runs `xcodebuild test` of `MayPlaygroundSnapshots`, with `TEST_RUNNER_MAY_SNAPSHOT_DIR` set to a private folder. Each test writes `<id>-light.png` and `<id>-dark.png`, at 1179 × 2556.
   - `check` compares the PNGs with may-app's `docs/evidence/parity/screens.json`, an array of `{id, title, spec, android: {light, dark}, ios: {light, dark}}` with paths relative to `docs/evidence/parity/`. Every row is expected in light and dark, except where its iOS path is null (`"ios": null` for the row, or one mode's): that is a screen iOS doesn't draw yet, shown on the sheet with its Android render and the row's `note`, and never counted as missing. It prints rendered against expected, per mode, and how many renders are not the 1179 × 2556 frame. If the commit has no usable manifest (not there yet, or malformed), it says so, and the `<id>-<mode>.png` renders in the snapshot folder stand in for its rows. The run still publishes them and the build log, without an Android column or `screens.json`, and `verdict` fails.
   - `sheet` builds the run folder:
     - `index.html`, with Android and iOS side by side per row and mode, and the same table as `README.md`, which GitHub shows when the folder is opened in may-renders;
     - one stitched PNG per row and mode, from `stitch.swift`;
     - the manifest;
     - `build.log`, the last 8 MB of the build and test log, for the may-app lane.
   - `seal` age-encrypts the run folder to the environment variable `MAY_RENDERS_RECIPIENT`. Only that encrypted file is uploaded, kept for one day.
   - `verdict` then fails the job if the build or tests failed, the manifest was not usable, or a render is missing. A failed build is still published first, with its log.
3. **publish**, on a fresh Linux runner that runs no may-app code, has three steps, each holding one secret:
   - `publish.py open` decrypts with `MAY_RENDERS_DECRYPT_KEY`, after checking it against `MAY_RENDERS_RECIPIENT`, and accepts only the run folder's fixed layout.
   - `push` adds `<may-app short sha>/` to may-renders' `main` with `MAY_APP_RENDERS_WRITE_KEY`, a deploy key on may-renders alone. It lists the run folders in `runs.json` and keeps the newest 10, and it never force-pushes.
   - For a pull request, `comment` links the contact sheet first, as the run's `README.md` (GitHub renders it, on a phone too, where `index.html` shows as source), then the side-by-side PNGs, the run folder and the log, using `MAY_APP_PR_COMMENT_TOKEN`.

**Setup** in the environment `may-ios-renders` (main only, administrator bypass off, no required reviewer):
- `MAY_APP_RENDERS_WRITE_KEY`: the deploy key `may-ios-renders`, with write access on may-renders only;
- `MAY_RENDERS_DECRYPT_KEY`, and the variable `MAY_RENDERS_RECIPIENT`: its age key pair;
- copies of `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY`, `MAY_APP_PR_READ_TOKEN` and `MAY_APP_PR_COMMENT_TOKEN`.

Without `MAY_RENDERS_RECIPIENT`, the workflow renders and counts, seals nothing and publishes nothing, and says so. Without the write key it pushes nothing. Without `MAY_APP_PR_COMMENT_TOKEN`, it still publishes to may-renders and logs "comment skipped: no token". Without `MAY_APP_PR_READ_TOKEN`, a pull request's renders skip the API read of the pull request, so nothing checks that it is open, against main and not from a fork. The fetch still requires `refs/pull/<number>/head` to be exactly `source_sha`, which means the commit is still the pull request's current head.

### Device tests of May Playground against staging

`device-tests.yml` runs May Playground on an Android emulator against live staging, signed in as the staging test account, and drives it with may-app's Maestro flows and with an agent: Claude Code driving the emulator. It runs on dispatch (`source_sha`, empty for main's head, and `scenarios`) and nightly at 03:17 UTC on may-app main's head, on Android and on the iOS simulator. This repository is public, so the log shows scenario ids, verdicts and counts only. Screenshots, recordings and reports leave the runner age-encrypted. Screenshots and reports go to the private `May-Assistant/may-renders`, under `device-tests/<UTC date>-<may-app short sha>/`, and recordings go to that run's may-renders release.

1. **admission** (`gate.py --action device-tests`) checks the inputs and the environment `may-device-tests`, which allows main only and has no required reviewer, because the nightly run can't wait for one. Only this action may start from a schedule.
2. **android**, on Linux with KVM:
   - the fetch (`prepare.py`, read keys) admits may-app main commits only, never a pull request, because the run step holds the sign-in secret;
   - `device_tests.py build` builds the Playground debug APK without secrets;
   - `run`:
     - installs the app and the pinned Maestro CLI 2.8.0, its zip checked against a SHA-256, and, when an agent scenario is chosen, review.py's pinned Claude Code release, checked against its sha512 (npm's linux-x64 package on the Android runner, the same release's darwin-arm64 package on the iOS job's macOS runner);
     - runs the Android emulator's animations at normal speed (`window_animation_scale`, `transition_animation_scale` and `animator_duration_scale` at 1), so the recordings show the app's real motion, such as the mascot ring (at 0, Compose's reduced motion stops it); the renders workflow keeps them at 0 for still screens;
     - settles the Android emulator before installing the app: after `sys.boot_completed`, it waits for `dev.bootcomplete` and a one-minute load average under 2, for at most 120 seconds, then goes on either way;
     - keeps each Android scenario's diagnostics as `<scenario>.diag.txt` in the sealed run folder:
       - any new ANR or crash from the system's dropbox (`dumpsys dropbox --print data_app_anr` and `data_app_crash`; the shell user reads it without `adb root`; each entry known by its timestamped header, so a failed read or the dropbox's rotation never makes an old entry new), then the last 2,000 lines of `logcat -b main,system,crash`, which is then cleared so each file holds its own scenario; all of it after the scenario's server reads, so a slow `dumpsys` never spends the session they need;
       - any line that could carry a credential is dropped: the session; an Authorization, Bearer, cookie or API-key header; an Anthropic or Google key; any token, secret, password, session, key or signature set to a value (`token=`, `"access_token":`); a URL query naming one, or an OAuth `code=`; May's own keys, plain, dashed or URL-encoded; a JWT; a hex run of 32 or more, or a base64 run of 40 or more. Each email address, `@` or `%40`, keeps its domain alone (`***@example.com`, `***@localhost`). Each pattern stays inside one line, so the check is line by line; each file is at most 512 KB;
       - `device-open` checks each file again with the very same rule, line by line (a file it would change at all is dropped; one it leaves alone is never dropped); like the recordings, the files never enter may-renders history, and `device-release` uploads them as optional assets of the run's release;
       - the log gives counts alone, with each file's number of dropped lines;
     - shows each tap in the recordings: on Android the emulator's own touch dots (`settings put system show_touches 1`) before the scenarios and off after, a setting that will not take never stopping the run; on iOS, whose simulator recording never shows touches, the launch argument `-MayShowTouches YES`, which turns on may-app #1194's in-app touch overlay (May Playground alone);
     - for each scenario in `device_scenarios.json`, mints a fresh staging session for the test account (`POST /v1/auth/test-session` with `{"secret": MAY_TEST_SIGNIN_SECRET}` in the body; staging fixes the account and replies `{token, expires_at, user_id}`, of which only the token is kept; it lasts 30 minutes, with no refresh) and restarts the app with it through a Playground-debug-only intent extra (`may_test_session`), whose `am start` line reaches the emulator's shell on `adb shell`'s stdin, so the session is in no argument on the runner's process list;
     - skips a Maestro scenario whose flow is not tagged with the job's platform (`tags:` in the flow's header, before its `---`), with the reason "skipped (flow not tagged for <platform>)", and goes on with the rest: a flow written for one device never runs unnoticed on the other, and one bad tag never costs the run;
     - fails a Maestro scenario, without driving it, when its flow (or a flow it runs) names an `id` that is not in the app's own sources for the job's platform: may-app's own `android/` or `ios/`, and the design system's `android/` or `swift/` where prepare.py leaves it (`may-source/android/build-deps/may-design-system`), Kotlin and Swift alike. Left out are test source sets (`test`, `androidTest`, `test<Variant>`, `androidTest<Variant>`), the `previews` set, test and snapshot targets, fixtures, and the emulator `flows` module. A flow's ids are read in both forms (`id: x` on its own line, or inline `{id: x}`), and the flows it runs are followed (`runFlow: x.yaml`, or a `runFlow` block's `file: x.yaml`, `when:` or not). An id is there as a string literal, or as a literal's fixed start before an interpolation (`"connections.row.${c.wire}"`) whose remainder is itself a literal, so a misspelt row id is caught; each alternative of an `a|b` id must be there, and an id using other pattern syntax is not checked. The reason is "the flow names an id the app's sources lack"; the log gives the count alone. Maestro itself parses a whole flow, its subflows included, before the first step, so a syntax error already fails the scenario;
     - records the screen (about 720p, about 1 Mbps, 3-minute parts) and runs the scenario: a Maestro scenario runs its flow from may-app's `tools/device-tests/flows/`; an agent scenario runs Claude Code headless (`device_agent.py`) with the scenario's goal;
     - takes a screenshot and reads the server: a Maestro scenario's status checks, and for an agent scenario whether K's reads (approvals, inbox, commitments, event notes, routines) list an item carrying the run's tag, `qa-<run id>`;
     - after a scenario whose goal sends May a message (chat, poll, poll-skip, routines), reads May's own chat and any chat named with the tag (`GET /v1/agents`, then each chat's `/v1/conversations/<id>/messages`, page by page from the oldest, 200 at a time and at most 50 pages, since the shared account's chat holds far more than one page: run 40087626039 read only the first 100 and wrongly said "none stored") and keeps, for each of the user's messages holding the tag (at most 10), only its reply run's code: `<status>` or `<status> (<error_code>)` from `latest_run`, each checked against `[a-z][a-z0-9_]{0,63}` (else `unknown`), or `no run`. No tagged message at all is `none stored`, shown as "none stored (no tagged message found in this chat)": the chats read hold no user message with the tag. The message text never leaves this read. The codes go in the report row as `delivery`, which `publish.py` checks to that shape before the README and the board show them ("Messages: …");
     - last, `POST /v1/auth/test-session/cleanup` with the tag and the run's start time removes what the run created.

     A scenario with no server checks configured passes on screen alone ("ui-pass").
   - **The agent** has no built-in tool (`--tools ""`), no settings beyond an empty HOME, and one MCP server, `device_mcp.py` (`--strict-mcp-config`, permission mode `dontAsk`, only `mcp__device` allowed). Its tools are screenshot, hierarchy, tap, type (plain text only), swipe, back, wait and done, all on the emulator through adb, with at most 80 steps. Its environment holds `CLAUDE_CODE_OAUTH_TOKEN` (the repository's, as the code review uses) and nothing else secret; the model is the variable `MAY_DEVICE_TESTS_MODEL`. Its system prompt forbids signing out, account, device, billing or connection settings, approving, paying and sending outside May's chat, and puts the tag in everything it creates. On Android, type is one `input text` call, and its answer quotes what the focused field then holds (from the hierarchy, through the same redaction as the diagnostics: a field that could hold a credential reads `(hidden)`, an email address keeps its domain alone, at most 300 characters), so the agent never types it again piece by piece. Its output stays on the runner, in the encrypted report only, apart from its own account: the judge step seals its outcome, its number of steps and its summary (through the same redaction, at most 300 characters) into the scenario's row of the private report.json, which `device-open` checks again; the pages, the log and the job summary never show it.
   - `judge` (the token and the model, nothing else) runs Claude Code with no tool at all on each agent run: the goal, the agent's own report, the screen's hierarchy as the agent left it (taken by `device_mcp.py` itself at done, never the agent's last look, through the same redaction and within the same size), and, for a scenario that sends May a message, the server's delivery codes alone (never a message's text), answered in a fixed JSON schema (pass or fail, and a short note). A delivery code that reads failed always makes the scenario fail ("May's server recorded a failed reply"), whatever the judge read on screen. A scenario passes when the agent completed, the judge passed it and the server shows the tagged item; with no server reads configured, it is "ui-pass". A rate limit or usage limit is **skipped, never a pass**. This step prints every scenario's verdict.
   - `seal` age-encrypts the run folder to `MAY_RENDERS_RECIPIENT`. Only that file is uploaded, kept for 7 days, as the recordings' fallback.
   - `verdict` fails the job if any scenario failed.
3. **ios**, on macOS (`device_ios.py`), runs the same scenario list's iOS rows:
   - the fetch, as Android's, with the iOS platform;
   - `build --platform ios` builds a debug May Playground for the simulator, signed ad hoc (`CODE_SIGN_IDENTITY=-`, no team or identity) so that its entitlements are embedded and the Keychain can hold the test session, with the pinned Xcode 26.3 and XcodeGen (`renders.xcode_env`, as ios-renders builds), and no secret;
   - `run --platform ios` boots an iPhone 16 Pro simulator on the newest iOS runtime and installs the app. For each scenario it mints a fresh test session and launches the app with it in `SIMCTL_CHILD_MAY_TEST_SESSION` (may-app #921's hook: a debug Playground reads `MAY_TEST_SESSION` at launch; the token is never an argument). It records the screen (`simctl recordVideo`, made 720p with `avconvert`, and dropped if it would still be over 38 MB), runs the same Maestro flow as Android (one bundle id), takes a screenshot and runs the server checks. On both platforms the flow gets the scenario's session in Maestro's environment as `MAESTRO_MAY_TEST_SESSION` (Maestro's CLI makes each `MAESTRO_` variable a flow variable, so a flow reads `${MAESTRO_MAY_TEST_SESSION}`), never as an argument;
   - agent scenarios run here too, with the same tag, server truth, cleanup and judge (`judge --platform ios`, the Claude token and model in its scenarios and judge steps alone). `device_mcp.py`'s iOS backend keeps the agent's eight tools, acting through the pinned Maestro's driver: each act is one Maestro flow the server writes itself, holding exactly one fixed command (`tapOn` point, `inputText`, `swipe`) for the app's bundle id, so the agent never writes Maestro YAML and Maestro's scripts, links and other apps are out of its reach. Screenshots come from simctl and the hierarchy from `maestro hierarchy`; screenshot pixels become iOS points by the ratio to the hierarchy's root, and back is a swipe in from the left edge. An iOS agent run may take 20 minutes (`IOS_AGENT_SECONDS`), so one 30-minute test session always covers a whole scenario;
   - `seal --platform ios` and `verdict --platform ios`, as Android's, as the artifact `may-device-ios-<attempt>`.
   - **Time budget, on both platforms:**
     - The scenarios and their judging share `RUN_BUDGET_SECONDS` (90 minutes) from the run step's start, inside each job's 120 minutes.
     - A scenario starts only when its own cap still fits, together with the judging (`JUDGE_SECONDS` for each agent scenario started, its own included). A Maestro flow's cap is `FLOW_SECONDS`; an agent's is `AGENT_SECONDS` on Android and `IOS_AGENT_SECONDS` on iOS, plus `ONBOARDING_SECONDS` for its onboarding subflow (none for the row that records onboarding).
     - Before each agent scenario, on both platforms, the run plays may-app's `tools/device-tests/flows/subflows/finish-onboarding.yaml` (bounded by `ONBOARDING_SECONDS`), which taps the signed-in shared account through onboarding to Chats (run 40046272749: it landed in onboarding, not Chats). Without the subflow (an older may-app), or when it does not pass, the agent starts where the app is and the log says so in a fixed line; the report is unchanged. The `onboarding` scenario is the exception: its row says `"onboarding": "record"`, so no subflow runs before it and its agent and recording start at onboarding's first step, on a new account of its own (only a `"fresh": true` agent row may say so: the shared account is already past onboarding).
     - A scenario that wouldn't fit is skipped with "the run's time budget left no room for it". It is reported as skipped, so the job always seals and hands over what ran.
     - The iOS Maestro flow is now bounded by `FLOW_SECONDS` too.
4. **publish**, on a fresh Linux runner that runs no may-app code:
   - `publish.py device-open` decrypts with `MAY_RENDERS_DECRYPT_KEY` and accepts only the fixed layout: `report.json`, MP4s, end screenshots and Maestro reports. It builds the run folder's `README.md`, which GitHub shows when the folder is opened, and `index.html`, both from the checked reports and this repository's own scenario titles. It moves the checked MP4s out of the folder, so they never enter git history. It also accepts may-app's `flows.json` and `board-notes.json` (JSON, from the device jobs), which it sets aside for the flow board.
   - `device-push` adds the folder under `device-tests/` with `MAY_APP_RENDERS_WRITE_KEY`, keeping the newest 10 in `device-tests/runs.json`, and never force-pushes.
   - In the same commit, `device-push` writes the **flow board** (`board.py`, `board_template.html`) as one self-contained folder, `device-tests/board/`: `index.html` and `media/`, with every path relative and below it, so the folder can be uploaded as-is. Only the latest board is kept; its history is inside it:
     - **Verdicts** come from the checked reports alone (`ui-pass` shows as passed).
     - **Flows** come from may-app's `docs/evidence/flows.json`. A flow with several tests gets a nested row per test (titled from `scenario_titles` in board-notes.json, else this repository's scenario list), each with its own state; the flow's page lists them with their counts.
     - **Curated text** comes from may-app's `docs/evidence/board-notes.json`, which the lanes edit: a banner; per flow, what it means when it works, "Before the next run", an open issue and the pull requests changed since; per result, a headline, a note and links to May-Assistant pull requests or issues only. It may also name each test's row (`scenario_titles`). The text describes the latest results alone.
     - **How the files arrive:** each device job copies the two files from its may-app checkout into the sealed report. `device-open` sets them aside and never commits them; every field is checked against a fixed shape, and the page escapes every value.
     - **Missing or malformed files:** may-app's two files never stop a run's publishing. A file that is not JSON (deep nesting included) is dropped when the report is opened, and the log says the board went without it. Without a well-formed `flows.json` there is no board (the log says so); with malformed notes, the board is written without them. Any other error while building the board skips the board alone, and the run is still published. The board and `history.json` are built beside the current ones and swapped in only at the end, so a failure part way leaves both as they were.
     - **History:** `device-tests/history.json` keeps the newest 60 runs, each with its verdicts and its end screenshots, copied to `device-tests/board/media/<folder>/` so they outlive the run folder. By default the board shows each test's **latest result from any run**: the newest run that ran it, else its newest skip. A subset or smoke run never hides an older result. It also shows one dot per run, newest on the right; a click shows that run's two phones. Runs keep their release (and recordings) only while among the 10 newest runs with a recording, so runs without one never push the others out. An older run with a recording shows as **Archived**: verdict and end screen, no recording. Entries with an impossible date are dropped, and media of any run no longer in the history is removed, but only when `history.json` was read: a missing or unreadable one never deletes past media.
     - **Previews** are made in each device job, which already runs may-app code and holds no publish secret, never in the publish job. A step with no secret, `device_tests.py previews`, comes before the seal. It makes a 270px-wide, silent H.264 preview of each scenario's recording (its first part when it has several): a timelapse of the whole recording, at most 60 seconds at 8 frames a second and about 160 kbit/s, so about 1 MB and never over 2 MB (run 40075535088: a 10-minute iOS preview came to 15.9 MB, over the artifact's 15 MB limit; one over 2 MB is left out), as `<scenario>.preview.mp4` (a dot is never in a scenario id, so a preview never shares a name with a recording). On Android it uses Ubuntu's current ffmpeg from apt; on iOS, `preview.swift` (AVFoundation), compiled with the pinned Xcode like `stitch.swift`. A preview that fails is left out, and nothing in that step fails the job: the step is also `continue-on-error` with an 8-minute timeout (the iOS compile has its own five-minute bound), and starts no new preview after six minutes, so the seal always runs before the job's 120 minutes (build, the 90-minute run, previews and the seal together). Each preview is made in `may-private` and moved into the run folder only when whole, so a step stopped part-way never leaves a half-written one to be sealed. Previews are keyed by the report's rows, never by file names. The publish job never decodes them: `device-open` keeps a preview only when it starts as an MP4 (`ftyp`) and is at most 2 MB. `device-release` uploads the recordings first and the previews after, each preview optional, as `<platform>-<scenario>.preview.mp4`. No video enters any git tree. The page expects each at `media/<folder>/<platform>-<scenario>.preview.mp4`, for runs whose release is kept. `media/previews.json` names the release tag and asset for each such path, so the folder can be filled before it is uploaded. Where a preview is absent, the end screen shows instead.
     - **Expected next to the recording** (review pages 1.1). may-app's flow prototypes give each flow's expected path. The Android job carries each `docs/evidence/prototypes/paths/<flow>.json` whose top-level `"board"` names a flows.json id, and whose `"flow"` is its own file name, into the report as `path-<board>.json` (small, never through a link). After the previews, a step with no secret, `device_tests.py prototypes` (`device_prototypes.py`), copies those paths (never the run step, which holds the sign-in secret; at most 40, and two naming one board are both dropped), then renders. may-app's code never runs as the runner's user or with a network: the prototypes folder alone is copied to a folder under `/tmp` owned by a user of its own (`may-render`). Before the build, the runner's temp folder, its checkout, Claude Code and private folders, Maestro's home, the runner's own home (which a hosted image may leave readable to all), and `~/.maestro` and `~/.android` are made the runner's alone and then checked as `may-render`; if any is reachable, or the check itself cannot run, nothing renders. The `/tmp` folder is readable by all, which is fine because it holds the prototypes alone, and its stdlib build (`src/build.py`) and the image's headless Chrome run as that user, in a network namespace of their own with no interface (`sudo unshare --net`), with a clean environment; Chrome also proxies everything to a closed port, resolves no host name and sends no WebRTC outside the proxy, and keeps its own sandbox: it is never given `--no-sandbox`, so if its sandbox cannot start, nothing renders. Each shot is read back without following any link the render user could have put in its place. The folder, and its temporary parent, are removed at the end. Chrome shoots each main-path step, per platform, from `pages/<flow>.html#n=<n>&platform=<p>&shot=1`: the device screen alone at its logical size (Android 412×915, iOS 393×852) at half scale. (Maestro, for its part, keeps its state and debug output in a folder of its own under the runner's temp folder: `HOME`, `XDG_STATE_HOME` and, through `MAESTRO_OPTS`, Java's `user.home`, which Java reads from the passwd entry rather than `HOME`. That output holds each flow's evaluated commands, the test session among them. The folder is removed at the run's end, with any `~/.maestro/tests` left in the runner's own home.) A shot is kept only when the same page, under the same virtual-time budget, reports `<html data-ready="1">`, and only as a PNG of at most 512 KB, 8 MB a run, as `proto-<board>-<platform>-<n>.png`. Each shot is written aside and renamed into place, so a stopped step never leaves half a picture. Four shots run at once, each call bounded at 30 seconds; from the build's start, none starts after six minutes, and the step is `continue-on-error` with an 8-minute timeout; it never fails the job, and the log gives counts alone. `device-open` drops a path that is not JSON, or a screen that is not a small PNG, rather than stop. `board.py` checks each path's shape (`checked_path`: at most 200 steps and 20 cases, every text at most 160 characters, the main case required) and keeps the newest set in `board/expected/`. That set is rebuilt only from a run that brings paths and at least one screen, so a run whose render failed keeps the last good one; a path for a flow the board does not list is not shown. On the page, each platform's card shows Expected beside Recording when the card is wide, and a Recording | Expected toggle when it is narrow. The expected side plays its steps with a bar attached under it, and a flow with no test yet shows its expected path alone.
   - `device-release` uploads the MP4s, as `<device>-<file>.mp4`, to the may-renders release `device-tests-<run>` with `MAY_RENDERS_RELEASE_TOKEN`. On a re-run it reuses the release and skips assets already there. It keeps the newest 10 `device-tests-*` releases and deletes older ones with their tags, never touching other releases. Its only calls are those releases on may-renders. Without the token, it uploads nothing and says so, and the recordings stay in the encrypted artifact.

**The harness smoke check:** dispatch with `scenarios: smoke` to check the harness itself before staging or the sign-in secret exist. It is never part of `all` and needs neither `MAY_STAGING_API_URL` nor `MAY_TEST_SIGNIN_SECRET`. On each device it:
- boots the emulator or the simulator and installs the build;
- starts the app signed out, with no test session minted;
- runs this repository's own two-line Maestro flow (launch the app, wait), never one of may-app's;
- runs the agent once with a read-only goal (take a screenshot, read the hierarchy, call done), then its judge;
- records, seals and publishes as usual.
Its verdict is "smoke-pass" or "fail" ("Maestro did not drive the app" when the flow fails), never "pass": it checks the harness, not May. Nothing is cleaned up, since nothing was created.

**When the run step fails unexpectedly**, its public log names where, in one line: the stage (for instance `smoke: the agent`, from fixed words and this repository's scenario ids), the exception's class and, for an OS error, its errno. It never prints the exception's text, which may hold private paths or tool output.

**Switching a scenario off:** the variable `MAY_DEVICE_TESTS_SKIP` lists scenario ids, comma-separated, that both jobs skip ("switched off"), for instance one whose server side is merged but not yet deployed. The test session's cleanup also expires the pending approvals test sessions asked for since the run began (`approvals_expired`, counted with the removed items).

**Fresh accounts:** a scenario runs on a new account of its own when its row says `"fresh": true` or the variable `MAY_DEVICE_TESTS_FRESH` names it (`all`, or ids comma-separated; never the smoke scenario). Its session is minted with `{"secret": …, "fresh": true}` (may-app 150), and its reply must say `"fresh": true`: one that does not (a staging that ignores `fresh`) is a session on the shared account, never used as a fresh one or deleted, and the scenario, with every later fresh one, is skipped with "skipped: staging did not put this scenario on a fresh account". That one session does everything: a fresh account can never get a second. At the scenario's end, after its server reads, the same `POST /v1/auth/test-session/cleanup` deletes the whole account (`{"account_deleted": true}`); one whose scenario stops part-way is deleted all the same by the run's `finally`; one whose cleanup does not answer is counted in the log, and staging sweeps it after 2 hours. The run-wide cleanup by tag runs only when a scenario ran on the shared account. Staging makes at most 50 fresh accounts a UTC day: past that (429 `fresh_accounts_exhausted`) the scenario is skipped with "skipped: staging's fresh test accounts for today are used up", never run on the shared account instead, and staging is not asked again that run. The log gives counts alone.

**A session staging won't mint:** a mint that fails part-way through a run (a 429, a 5xx or no reply) is asked for once more after 30 seconds. If it fails again, only that scenario is skipped, with "skipped: staging did not mint a test session for it", and a public line names its HTTP status. A 401, 403 or 404 (the secret, or the test sign-in switched off) still stops the run, and the line names the status. A cleanup whose session isn't minted reads as a cleanup that did not answer. Before run 40077167994, one failed mint 50 minutes into the iOS run stopped it, and the report was lost.

**Model-call cap:** staging caps a test account's model calls a UTC day (`MAY_TEST_SESSION_CALLS_PER_DAY`, may-app 125); past it, replies fail `test_session_limit_reached`. Before each agent scenario, with its own session, the run reads `GET /v1/auth/test-session/state` and skips the scenario with "skipped: the test account's model calls for today are nearly used up" when `model_calls.limit - used` is below `MODEL_CALL_MARGIN` (10). It never runs one into the cap. A fresh account skipped this way is deleted then and there. A state read without both counts (a staging without the cap) runs the scenario.

**Outward actions:** a scenario marked `"outward": true` runs unless `MAY_DEVICE_TESTS_OUTWARD` is `off`. So far only the self-addressed Gmail send to harlahke@gmail.com is planned to use this. Samuel approved it directly on 21 Oct, conditional on staging's guard for test sessions.

**Setup** in the environment `may-device-tests`:
- the May Renders GitHub App, in place of `MAY_RENDERS_RELEASE_TOKEN`: variable `MAY_RENDERS_APP_ID` and secret `MAY_RENDERS_APP_PRIVATE_KEY`. Install the app on may-renders only, with Contents read and write. The `publish` job mints its token right after its checkout (`renders-release`: repository may-renders, Contents: write), and the release step gets `steps.renders-release.outputs.token || secrets.MAY_RENDERS_RELEASE_TOKEN`, as the May CI app's mints do ([Secrets and variables per environment](#secrets-and-variables-per-environment)). The PAT stays as the fallback until a follow-up drops it; the deploy key `MAY_APP_RENDERS_WRITE_KEY` stays;
- secrets: `MAY_TEST_SIGNIN_SECRET`, `MAY_RENDERS_RELEASE_TOKEN` (fine-grained, may-renders only, Contents read and write; the fallback), plus copies of `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY`, `MAY_RENDERS_DECRYPT_KEY` and `MAY_APP_RENDERS_WRITE_KEY`; `CLAUDE_CODE_OAUTH_TOKEN` is the repository secret;
- variables: `MAY_STAGING_API_URL` (exactly `https://staging-api.usemay.co`: `device_tests.STAGING_BASE` refuses any other host, so `api.usemay.co`, production from launch, is never driven), `MAY_RENDERS_RECIPIENT`, `MAY_DEVICE_TESTS_MODEL` and, optionally, `MAY_DEVICE_TESTS_OUTWARD`, `MAY_DEVICE_TESTS_SKIP` and `MAY_DEVICE_TESTS_FRESH`.

### Prerequisites that remain

`gate.py` checks these, before any secret and again after each review. Any one failing denies the run. The environment checks apply to **both** `may-native-compile` and the target's store environment (`may-android-internal` for May production):

- the environment exists (a 404 denies; it is never auto-created);
- **administrator bypass is off** (`can_admins_bypass: false`);
- a **custom** deployment branch policy (not "protected branches") with exactly one rule, `main`;
- exactly **one required-reviewer rule**, whose **sole** reviewer is Samuel (user ID `58840187`);
- the self-review setting is readable, and it is not the deadlock case: the gate denies only when *Prevent self-review* is **on and** the sole reviewer (Samuel) dispatched the run, because that run could never be approved. **In practice it must be off**, because Samuel dispatches and approves his own runs. That is Samuel's decision for internal builds only (see [Self-review](#environments-and-protection));
- in stage 4, a dispatch with `dry_run: false` is May or May Playground, to the Play internal track or internal TestFlight, and one with `sign_only: true` is May production Android with `dry_run: true` and a committed Console-upload key (admission only; see [Dry run](#dry-run));
- the build number is at least the app's floor (May production: 3);
- the target's **artifact public key is committed** and well-formed: `tools/may_release/artifact_keys/may-android-internal.age.pub` (`age1kpcl…skyd7vg`) for May production, `may-android-playground-internal.age.pub` (`age1cv26…sex4n5j`) for Playground, both committed 2026-10-13. A placeholder or malformed key denies before any request. Its private half is that environment's `MAY_ARTIFACT_DECRYPT_KEY`, which `handoff.py open` checks against it.

Also needed, which the gate cannot see:

- in `may-native-compile`: `MAY_APP_READ_KEY` and `MAY_DS_READ_KEY` (and, for crash reports, `MAY_POSTHOG_ANDROID_KEY` and `MAY_POSTHOG_IOS_KEY`; for push, `MAY_GOOGLE_SERVICES_JSON`);
- in `may-android-internal`: the secrets `MAY_ARTIFACT_DECRYPT_KEY`, `MAY_ANDROID_KEYSTORE_BASE64` (the `may-upload` keystore), `MAY_ANDROID_STORE_PASSWORD`, `MAY_ANDROID_KEY_PASSWORD` and, for an upload, `MAY_PLAY_JSON` for `may-play-upload@may-assistant-app.iam.gserviceaccount.com`; the variables `MAY_ANDROID_KEY_ALIAS=may-upload`, `MAY_ANDROID_CERT_SHA256` (must equal the committed pin) and `MAY_PLAY_ACCOUNT=may-play-upload@may-assistant-app.iam.gserviceaccount.com`;
- that service account holds May's "Release apps to testing tracks" permission in Play Console (confirmed 14 Oct, and nothing more: it lacks "Manage testing tracks", so testers are set by hand in Console).

`android-release` references `MAY_PLAY_JSON`, so once it is set the runner receives it with every `android-release` job, dry runs included, even though only the upload step is given it. A sign-only run does not run `android-release`; its own job references no Play secret.

The Play client has run against Play for May Playground (build 5). It has not yet run for May production, and the iOS paths have not yet run against App Store Connect (stage 4's first dispatches do that).

No May backend deployment should be inferred from a mobile compile. Model-spend limits and default-off voice activation are separate staging admission gates.

Public GitHub Actions logs are visible to everyone. Every failure path prints only fixed, nonsensitive messages (`static_check.py` checks that printed error messages are string literals). The one exception is `play_upload.PlayStatusError`, which adds the numeric HTTP status, and only when it is an integer from 100 to 999. `static_check.py` pins that class whole: it must exist exactly once, at the top level of `play_upload.py`, and parse to exactly the copy in `STATUS_ERROR_SOURCE`, so any edit to it fails the check until that copy is updated in the same reviewed change. It may be called only in `play_upload.py`, with `<name>.status_code`. `play_upload.py` may not rebind `str`, `type`, `int`, `super`, `StoreError` or the class itself, or use `builtins`, `globals()`, `vars()`, `setattr`, `exec`, `eval` or `__import__`. No module may assign an exception's `.args`. These rules guard against accidental drift, not hostile code. A plain `print(os.environ[...])` anywhere in a script is not detected, and never was. The follow-ups in [#9](https://github.com/May-Assistant/may-releases/pull/9) list further known routes round the pin. Review is what stops a deliberate leak. See [GitHub's environment availability rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) for why the control repository is public while the app repositories remain private.
