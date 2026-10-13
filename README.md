# May release control

This **public** repository holds release-control workflows only. May app and design-system source remain private. No private source, assets, signing material, provider response, native build output, or credential belongs in a commit, public log, or artifact here (the one artifact the store workflow publishes is age-encrypted; see below).

## Store records

The separate May Playground iOS app is registered in App Store Connect as `6822072535` with bundle `io.github.mayassistant.may.playground`, active App Store profile `T5Y2C9URKX`, and empty internal TestFlight group `8a709a78-c828-4e7b-818c-b6ce62998ec8`. The real May iOS app is `6821782401` with bundle `io.github.mayassistant.may`. The corresponding Play apps are separate records: May `4974275831988601568` and May Playground `4972137188420176977`.

Google Play accepts only `status: draft` releases through the API until an app's first release has been rolled out, so **each Play app's first release is uploaded by hand in Play Console**. That manual upload is in progress. This repository only performs later updates; it refuses an app without a rolled-out release with the fixed message `First release must be uploaded in Play Console.`

## Current state

The draft [native compile workflow](.github/workflows/native-compile-draft.yml) and [store-release workflow](.github/workflows/store-release-draft.yml) are **inert**: their jobs require event names the workflows do not register (`may-compile-disabled-pending-review`, `may-store-disabled-pending-review`). A separate reviewed change must remove those guards. The pull-request check runs only dependency-free control tests and static checks with a read-only token.

The admission gate also **denies every store run while the target's artifact public key is a placeholder** (all four are, until the setup lane completes [the key-pair steps](#artifact-key-pair-setup)).

Confirmed from the repository's Actions secrets page on 2026-10-13. All secrets below are environment-scoped. The only repository secret is `CLAUDE_CODE_OAUTH_TOKEN`, and no organization secrets are available to this repository.

| Environment | Secrets present |
| --- | --- |
| `may-native-compile` | `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` |
| `may-android-internal`, `may-android-playground-internal` | `MAY_ANDROID_KEYSTORE_BASE64`, `MAY_ANDROID_KEY_PASSWORD`, `MAY_ANDROID_STORE_PASSWORD`, `MAY_PLAY_JSON`, `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` |
| `may-ios-internal`, `may-ios-playground-internal` | `MAY_IOS_CERT_BASE64`, `MAY_IOS_CERT_PASSWORD`, `MAY_IOS_PROFILE_BASE64`, `MAY_ASC_KEY_BASE64`, `MAY_ASC_ISSUER_ID`, `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` |

Still needed:

- `MAY_ARTIFACT_DECRYPT_KEY` in each store environment (see the key-pair steps below).
- Confirmation that the environment **variables** `MAY_ANDROID_KEY_ALIAS`, `MAY_ANDROID_CERT_SHA256`, `MAY_PLAY_ACCOUNT` (Android) and `MAY_ASC_KEY_ID=23VX669FGY` (iOS) are set. The secrets page does not show variables.

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

**Self-review: current policy.** *Prevent self-review* is **off**, by Samuel's decision for internal testers only, so Samuel may dispatch and approve his own run. The gate denies only the deadlock case: self-review prevented while the sole reviewer dispatched. That policy is acceptable because nothing in this repository can target anything but internal testing. Play uploads go only to the `internal` track, TestFlight exports are `testFlightInternalTestingOnly`, there is no track input, and `static_check.py` refuses a production track in the workflow.

### Before any non-internal release (required)

Before the first production or public-beta release:

1. Turn **Prevent self-review on** in every environment involved.
2. Add a **second required reviewer**.
3. Update `gate.py` (`OWNER_ID` sole-reviewer rule and self-review handling) and its tests in the same reviewed change that adds any non-internal track.

The current code must not be extended to a non-internal track without these.

**No repository- or organization-level secrets with the same names.** GitHub falls back from an environment secret to a repository or organization secret of the same name. That would hand a step a value without the environment's protection, and the gate cannot see it. Confirmed absent on 2026-10-13; keep it that way.

## Release jobs and what the isolation does and does not give

Each store run is **two jobs on separate fresh VMs**:

1. **`*-build`** (`may-native-compile`, read-only source keys only):
   - fetches the exact private source (`prepare.py`);
   - runs the private build script for an **unsigned** product (`store.py`). This step runs third-party code: Gradle and its plugins, SwiftPM, and Xcode build phases.
   - encrypts the product with [age](https://age-encryption.org) to the target's public key committed in `tools/may_release/artifact_keys/` (`handoff.py seal`);
   - uploads only the encrypted file, as a one-day artifact.
2. **`*-release`** (the store environment; `needs: [admission, *-build]`):
   - on a fresh VM, runs no build toolchain. Its only actions are SHA-pinned `actions/checkout` and `actions/download-artifact`, used before any secret is present. Every command is this repository's Python, which calls the pinned, checksum-verified Temurin `jarsigner`, the pinned `age`, the system `openssl`, and Apple's `/usr/bin` tools.
   - `handoff.py open` decrypts with `MAY_ARTIFACT_DECRYPT_KEY` and checks that key against the committed public key. It refuses the payload unless its manifest names exactly this repository, run ID, platform, target, source SHA and build number.
   - `sign.py` signs and verifies, then `play_upload.py` or `ios_upload.py` uploads.

Within a job, each step receives only its own secrets, and each script refuses to run if it finds another step's secret. Tools get an allowlisted environment. `static_check.py` enforces:

- the job and step layout and the step-to-secret map;
- the allowed actions and commands in release jobs;
- that only the `.age` file is uploaded;
- that no `setup-java` or `pip install` appears in any workflow.

**What this gives, and what it does not:**

- **Separate jobs are the real boundary.** Code from the build cannot reach the signing keystore, certificate, Play key or App Store Connect key, because it never runs on the VM that holds them. Steps *within* one job are not a security boundary: same user, same VM, shared files. That is why signing moved to its own job.
- **The build job is trusted only for what it builds.** Third-party build code could make the build produce a malicious product, or tamper with the encryption step. Separation cannot prevent that. Signing re-verifies what it can (signature coverage and signer for the AAB; profile, bundle ID and build number for the IPA). It cannot vouch for the code inside.
- **age gives confidentiality and integrity, not sender authentication.** Anyone holding the public key can encrypt. The run-bound manifest refuses another run's or target's artifact, and a tampered file fails decryption.
- The read-only source keys exist in the build job, in the fetch step only, and stay readable to the build within the limits of a same-VM step.

## Artifact key-pair setup

This is done by the setup lane, not by Claude. Repeat for each of `may-ios-internal`, `may-ios-playground-internal`, `may-android-internal` and `may-android-playground-internal`, with a **distinct** key pair per environment. Run on a trusted machine:

1. Install age v1.3.1 (`brew install age`, or the release binary verified with the Sigsum proof as in age's `SIGSUM.md`).
2. Generate the key pair. `age-keygen` prints `Public key: age1…` on stderr:

   ```
   umask 077
   age-keygen -o ENV.agekey
   ```

3. Store the whole file as the environment secret. The comment lines are accepted. Paste nowhere else.

   ```
   gh secret set MAY_ARTIFACT_DECRYPT_KEY --repo May-Assistant/may-releases --env ENV < ENV.agekey
   ```

4. Send **only the public key** (`age-keygen -y ENV.agekey`, a single `age1…` line) for a reviewed PR. That PR replaces the placeholder text in `tools/may_release/artifact_keys/ENV.age.pub` with exactly that line.
5. Delete `ENV.agekey`. No backup is needed: handoffs expire after one day, and a lost key is replaced by repeating these steps.

The gate keeps denying the target until step 4 is merged. `handoff.py open` refuses a decrypt key that does not derive the committed public key.

## Secrets and variables per environment

- **`may-native-compile`:** `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` (read-only deploy keys for `may-app` and `may-design-system`).
- **iOS store environments:**
  - Secrets: `MAY_IOS_CERT_BASE64` and `MAY_IOS_CERT_PASSWORD` (the approved existing distribution certificate), `MAY_IOS_PROFILE_BASE64` (that target's App Store profile), `MAY_ASC_KEY_BASE64`, `MAY_ASC_ISSUER_ID` and `MAY_ARTIFACT_DECRYPT_KEY`.
  - Variable: `MAY_ASC_KEY_ID=23VX669FGY`.
  - The code fixes the Apple team, bundle IDs and certificate fingerprint.
- **Android store environments:**
  - Secrets: `MAY_ANDROID_KEYSTORE_BASE64`, `MAY_ANDROID_STORE_PASSWORD`, `MAY_ANDROID_KEY_PASSWORD`, `MAY_PLAY_JSON` and `MAY_ARTIFACT_DECRYPT_KEY`.
  - Variables: `MAY_ANDROID_KEY_ALIAS`, `MAY_ANDROID_CERT_SHA256` (the upload certificate's SHA-256) and `MAY_PLAY_ACCOUNT`.
  - The Play service account belongs to project `may-assistant-app` and has only the selected app's view and testing-release grants, with no Rem, production or account-wide access. The Rem uploader `rem-play-upload@rem-claw.iam.gserviceaccount.com` is not a May credential.

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
  5. commits with `ERROR_IF_IN_REVIEW`.

  Any failure deletes the edit.
- **Play client.** It uses only the standard library and the system `openssl`, with no third-party package:
  - The RS256 service-account assertion goes once to `https://oauth2.googleapis.com/token`.
  - Requests are allowed only to the Play Developer API host, and redirects are refused.
  - **Nothing is retried**: no refresh-and-resend on 401 for the upload or the commit. A 401 fails the run and deletes the edit.
- **Build numbers** above 2,000,000,000 are refused everywhere.

**Pinned toolchain** (`toolchain.py`; `actions/setup-java` is not used because it does not verify its download):

- Temurin JDK `21.0.12.1+1` for linux x64. Its SHA-256 equals Adoptium's published checksum.
- age `v1.3.1` for linux amd64 and darwin arm64. Each archive was verified with `sigsum-verify` against age's published key and proofs.

## Private release script interface

`may-app` main does not yet have `android/`, `ios/` or `tools/release/*.sh`; that work is in progress and tracked in [May app issue #52](https://github.com/May-Assistant/may-app/issues/52). This repository expects:

- `bash tools/release/android.sh build-unsigned`
  - Inputs: `MAY_TARGET`, `MAY_SOURCE_SHA`, `MAY_BUILD_NUMBER`, `MAY_APP_ID`/`MAY_ANDROID_APP_ID` and `MAY_ARTIFACT_DIR`, with `JAVA_HOME` set to the pinned JDK.
  - Leaves exactly `$MAY_ARTIFACT_DIR/unsigned.aab`, with no signature, `applicationId` = `MAY_ANDROID_APP_ID` and `versionCode` = `MAY_BUILD_NUMBER`.
- `bash tools/release/ios.sh archive-unsigned`
  - Inputs: the same variables plus `MAY_IOS_APP_ID`, `MAY_TEAM_ID` and `DEVELOPER_DIR` (Xcode 26.3).
  - Leaves exactly `$MAY_ARTIFACT_DIR/May.xcarchive` with `CFBundleIdentifier` = `MAY_IOS_APP_ID` and `CFBundleVersion` = `MAY_BUILD_NUMBER`.
  - Signs with no team identity: ad-hoc (`CODE_SIGN_IDENTITY=-`) with the app's entitlements embedded, so that the release job's `xcodebuild -exportArchive` can re-sign with the distribution identity and keep them.

Both scripts exit non-zero on failure and keep their output in the private log. They never see a signing or upload secret.

Exporting and re-signing a non-distribution-signed archive in a separate job is expected to work, but is **unverified until a macOS rehearsal**. That job uses only Apple's tools, with no build phases, SwiftPM or scripts. If that rehearsal shows that entitlements or app extensions cannot be carried across, the iOS path cannot yet be split safely, and the fallback has to be decided then. Signing must not move back into the build job.

## Activation prerequisites

Before dispatch, these must all be in place:

- the four artifact public keys committed and their private keys set;
- an exact-head app review and build;
- a store-ready `may-app` main commit;
- a protected, exact-head rehearsal;
- a separate, reviewed activation change.

The Play client, keychain, export and upload paths have unit tests, but none of them has yet run against a real store.

No May backend deployment should be inferred from a mobile compile. Model-spend limits and default-off voice activation are separate staging admission gates.

Public GitHub Actions logs are visible to everyone. Every failure path prints only fixed, nonsensitive messages (`static_check.py` checks that printed error messages are string literals). See [GitHub's environment availability rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) for why the control repository is public while the app repositories remain private.
