"""Report what a dry run would have uploaded, instead of uploading it.

This runs in the release job only when the `dry_run` input is not false, after
the handoff was opened and the product signed and verified. The store upload
step is skipped, so no upload credential is exposed to any step. This one holds no
secret at all and makes no network call.

It prints the store destination this repository's own code fixes (package or
bundle ID, track), the build number the artifact carries, and the SHA-256 of
the signed artifact. The IPA is re-verified first, as ios_upload.py would. Every
value is an input already validated by store.identity, a code constant, or a
hex digest: nothing from the private source.
"""

import argparse
import os
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import APPLE_TEAM, StoreError, file_sha256, identity, require_step_secrets, runner_root
import play_upload
import verify


def summary(platform, target, source_sha, build_number):
    """The lines a dry run prints: what the skipped upload step would have sent."""
    app_id = identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    root = runner_root()
    if platform == "android":
        artifact = root / "may-release/signed.aab"
        if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size == 0:
            raise StoreError("Signed Android App Bundle is unavailable")
        return ["Dry run: no store upload was made. Google Play would have received:",
                "  package: " + app_id,
                "  versionCode: " + build_number,
                "  track: " + play_upload.TRACK,
                "  signed AAB SHA-256: " + file_sha256(artifact)]
    artifact = root / "may-release/May.ipa"
    if not artifact.is_file() or artifact.is_symlink():
        raise StoreError("Signed iOS IPA is unavailable")
    verify.ipa_identity(artifact, APPLE_TEAM, app_id, build_number)
    return ["Dry run: no store upload was made. App Store Connect would have received:",
            "  bundle: " + app_id,
            "  build number (CFBundleVersion): " + build_number,
            "  track: TestFlight internal testing only",
            "  signed IPA SHA-256: " + file_sha256(artifact)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        lines = summary(args.platform, args.target, args.source_sha, args.build_number)
    except (StoreError, verify.VerifyError) as error:
        raise SystemExit("May dry-run report refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May dry-run report failed; no private detail was published") from None
    print("\n".join(lines))
