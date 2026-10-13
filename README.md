# May release control

This **public** repository holds release-control workflows only. May app and design-system source remain private. No private source, assets, signing material, provider response, native build output, or credential belongs in a commit, public log, or artifact here.

## Store records

The separate May Playground iOS app is registered in App Store Connect as `6822072535` with bundle `io.github.mayassistant.may.playground`, active App Store profile `T5Y2C9URKX`, and empty internal TestFlight group `8a709a78-c828-4e7b-818c-b6ce62998ec8`. The real May iOS app is `6821782401` with bundle `io.github.mayassistant.may`. The corresponding Play apps are separate records: May `4974275831988601568` and May Playground `4972137188420176977`.

Google Play accepts only `status: draft` releases through the API until an app's first release has been rolled out, so **each Play app's first release is uploaded by hand in Play Console**. That manual upload is in progress. This repository only performs later updates; it refuses an app without a rolled-out release with the fixed message `First release must be uploaded in Play Console.`

## Current state

The draft [native compile workflow](.github/workflows/native-compile-draft.yml) and [store-release workflow](.github/workflows/store-release-draft.yml) are **inert**: their jobs require event names the workflows do not register (`may-compile-disabled-pending-review`, `may-store-disabled-pending-review`). A separate reviewed change must remove those guards. The pull-request check runs only dependency-free control tests and static checks with a read-only token.

According to the setup lane, the Android store environments **`android-production`** (May) and **`android-playground`** (May Playground) now exist, their credentials have been transferred, and the May Android upload keystore has been chosen. The iOS store environment names have not been confirmed; `gate.py` still uses `may-ios-internal` and `may-ios-playground-internal`, each marked `CONFIRM with setup lane`. Secret presence is not visible to this repository: the setup lane is the record of what was entered, and admission checks protection settings, not values.

## Protected environments

| Target | Environment | Store app |
| --- | --- | --- |
| May iOS | `may-ios-internal` (confirm with setup lane) | ASC `6821782401` |
| May Playground iOS | `may-ios-playground-internal` (confirm with setup lane) | ASC `6822072535` |
| May Android | `android-production` | Play `4974275831988601568` |
| May Playground Android | `android-playground` | Play `4972137188420176977` |

Each environment, and `may-native-compile`, needs:

- a main-only custom deployment branch policy;
- Samuel (GitHub ID `58840187`) as the **sole** required reviewer;
- administrator bypass disabled.

A no-secret admission job checks the chosen environment before any secret becomes available; the protected job rechecks it after review.

**Environments are never auto-created.** A job's `environment:` key would create a missing environment with no protection, so the admission job runs first and denies on the API's 404. Create each environment by hand before any dispatch.

**Self-review.** If an environment has *Prevent self-review* turned on, Samuel cannot approve a run he dispatched himself, and the run would wait until it times out. The gate denies that case up front with a fixed message. Either another maintainer dispatches, or the setting stays off so that Samuel's approval is a deliberate second confirmation of his own dispatch.

**No repository- or organization-level secrets with the same names.** GitHub falls back from an environment secret to a repository or organization secret of the same name. If one of the names below also exists at repository or organization level, a step could receive it without the environment's protection. The gate cannot see this, so whoever manages secrets must check it. None of these names may exist outside the protected environments.

## Secrets and variables per environment

Every store environment has its **own** `MAY_APP_READ_KEY` and `MAY_DS_READ_KEY` (read-only deploy keys for `may-app` and `may-design-system`).

- **iOS:** secrets `MAY_IOS_CERT_BASE64`, `MAY_IOS_CERT_PASSWORD` (approved existing distribution certificate), `MAY_IOS_PROFILE_BASE64` (that target's App Store profile), `MAY_ASC_KEY_BASE64`, `MAY_ASC_ISSUER_ID`; variable `MAY_ASC_KEY_ID=23VX669FGY`. The code fixes the Apple team, bundle IDs and certificate fingerprint.
- **Android:** secrets `MAY_ANDROID_KEYSTORE_BASE64`, `MAY_ANDROID_STORE_PASSWORD`, `MAY_ANDROID_KEY_PASSWORD`, `MAY_PLAY_JSON`; variables `MAY_ANDROID_KEY_ALIAS`, `MAY_ANDROID_CERT_SHA256` (the upload certificate's SHA-256), `MAY_PLAY_ACCOUNT`. The Play service account belongs to project `may-assistant-app` and has only the selected app's view and testing-release grants, with no Rem, production or account-wide access. The Rem uploader `rem-play-upload@rem-claw.iam.gserviceaccount.com` is not a May credential.

## Release steps and secret isolation

Each workflow step receives only the secrets it needs, and each script refuses to run if it finds a secret that belongs to another step. Tools get an allowlisted environment, never a copy of the step's environment. `static_check.py` enforces the step-to-secret map.

| Step | Secrets | Code |
| --- | --- | --- |
| Install the Play client (Android) | none | `pip --require-hashes --only-binary=:all: -r tools/may_release/requirements-play.txt` |
| Fetch exact private source | `MAY_APP_READ_KEY`, `MAY_DS_READ_KEY` | `prepare.py` |
| Build unsigned | none | `store.py` runs the private `android.sh build-unsigned` or `ios.sh archive-unsigned` |
| Sign and verify | Android keystore and passwords, or iOS certificate, password and profile | `sign.py` |
| Upload | `MAY_PLAY_JSON`, or `MAY_ASC_KEY_BASE64` and `MAY_ASC_ISSUER_ID` | `play_upload.py`, `ios_upload.py` |

**Verification before upload:**

- **Android:** `jarsigner -verify` checks every entry digest. `verify.py` then checks that the manifest covers every entry and that there is exactly one signer, and that its certificate SHA-256 equals `MAY_ANDROID_CERT_SHA256`. Play's returned `sha256` and `versionCode` must match the local bundle.
- **iOS:** `verify.py` checks that the IPA's embedded provisioning profile is an App Store profile for team `R6A892D599` and the target's bundle ID. It also checks the app's `CFBundleIdentifier` and `CFBundleVersion`, both after export and again right before upload.

**Play update mode.** Inside one edit, `play_upload.py`:

1. reads every track;
2. requires the new versionCode to be greater than every versionCode on every track, and at most 1,000 above the highest;
3. uploads the AAB and checks Play's `sha256`;
4. replaces only the `internal` track with one `completed` release;
5. commits with `ERROR_IF_IN_REVIEW`.

Any failure deletes the edit, and the script never retries. Build numbers above 2,000,000,000 are refused everywhere.

## Private release script interface

`may-app` main does not yet have `android/`, `ios/` or `tools/release/*.sh`; that work is in progress and tracked in [May app issue #52](https://github.com/May-Assistant/may-app/issues/52). This repository expects:

- `bash tools/release/android.sh build-unsigned`
  - Inputs: `MAY_TARGET`, `MAY_SOURCE_SHA`, `MAY_BUILD_NUMBER`, `MAY_APP_ID`/`MAY_ANDROID_APP_ID` and `MAY_ARTIFACT_DIR`.
  - Leaves exactly `$MAY_ARTIFACT_DIR/unsigned.aab`, with no signature, `applicationId` = `MAY_ANDROID_APP_ID` and `versionCode` = `MAY_BUILD_NUMBER`.
  - Reads no signing or store variable.
- `bash tools/release/ios.sh archive-unsigned`
  - Inputs: the same variables plus `MAY_IOS_APP_ID`, `MAY_TEAM_ID` and `DEVELOPER_DIR` (Xcode 26.3).
  - Leaves exactly `$MAY_ARTIFACT_DIR/May.xcarchive`, archived with code signing disabled, with `CFBundleIdentifier` = `MAY_IOS_APP_ID` and `CFBundleVersion` = `MAY_BUILD_NUMBER`.
  - Reads no certificate, profile or App Store Connect key.

Both scripts exit non-zero on failure and keep their output in the private log.

## Activation prerequisites

Before dispatch, these must all be in place:

- an exact-head app review and build;
- a store-ready `may-app` main commit;
- owner-entered credentials in each environment;
- a protected, exact-head rehearsal;
- a separate, reviewed activation change.

The API client, keychain/export and upload paths have unit tests, but none of them has yet run against a real store.

No May backend deployment should be inferred from a mobile compile. Model-spend limits and default-off voice activation are separate staging admission gates.

Public GitHub Actions logs are visible to everyone. Every failure path prints only fixed, nonsensitive messages (`static_check.py` checks that printed error messages are string literals). The workflows publish no artifacts and no raw private build logs. See [GitHub's environment availability rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) for why the control repository is public while the app repositories remain private.
