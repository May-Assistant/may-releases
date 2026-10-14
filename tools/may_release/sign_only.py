"""Sign-only: publish the signed May production bundle, encrypted, for a manual Play Console upload.

Sign-only gives Samuel a May production bundle built, sealed, signed and verified
by this workflow exactly as an upload would be, without the upload keystore
touching any machine but the protected runner, for a build that has to go
through Play Console by hand. (Google's Play Developer API cannot make an app's
first release: its Edits guide says the API changes only an existing app "that
has at least one APK uploaded". May production's first release, vc2, was
uploaded by hand on 14 Oct, signed on Samuel's machine; later builds upload
through the workflow, so sign-only is for the exceptions.)

This runs in the release job, after the dry-run report, only when the dispatch
asked for it (`sign_only: true`, never the default) together with `dry_run: true`.
Admission (gate.py) admits that only for May production Android, whose release
job needs the same `may-android-internal` approval as an upload. The upload step
does not run (dry_run is true), so no Play credential reaches any step, and this
script makes no network call and holds no secret. It refuses unless:

1. MAY_SIGN_ONLY is exactly "true" and MAY_DRY_RUN exactly "true";
2. the target is in store.SIGN_ONLY_TARGETS and the build number is at least its
   floor (store.MIN_BUILD_NUMBERS: May production starts at 3);
3. Samuel's Console-upload public key (store.SIGN_ONLY_RECIPIENT) is committed;
4. the Console-upload key is not any environment's handoff key (whose private
   half is a CI secret);
5. the signed bundle's sole signer is the target's own upload certificate
   (store.ANDROID_UPLOAD_KEYS);
6. the signed bundle's own manifest (verify.check_aab_identity, read here on the
   release side, not trusted from the build job) is exactly this app's package
   at the requested versionCode. The record and the report carry those verified
   values only: a bundle uploaded by hand gets none of play_upload.py's checks.

The repository is public and so are its Actions artifacts, so the bundle is
encrypted with age to Samuel's committed public key. A throwaway identity is
added as a second recipient so that this step can decrypt the result and prove it
is byte-for-byte the signed bundle; that identity is deleted before the step
ends, so afterwards only Samuel's key opens it. The folder the workflow publishes
(store.SIGN_ONLY_DIR, a 3-day artifact) then holds exactly three files:

- signed.aab.age: the signed bundle, encrypted;
- signed.aab.sha256: its SHA-256 in `shasum -c` form;
- sign-only-record.json: mode, package, versionCode, the signed SHA-256 and the
  encrypted file's SHA-256.

No keystore, Play credential, decrypted handoff or other file is in it.
confirm_upload.py --sign-only then fails the job unless the publish step
succeeded and the record matches the files, and it reports "signed, not
uploaded", never an upload.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gate import AGE_RECIPIENT, ENVIRONMENTS, artifact_recipient
from store import (SIGN_ONLY_DIR, SIGN_ONLY_RECIPIENT, SIGN_ONLY_RECORD, SIGN_ONLY_TARGETS, StoreError,
                   build_number_floor_ok, check_android_signer, child_env, file_sha256, identity,
                   require_step_secrets, runner_root, write_private)
import toolchain
import verify

SEALED = "signed.aab.age"
HASH_FILE = "signed.aab.sha256"
FILES = frozenset({SEALED, HASH_FILE, SIGN_ONLY_RECORD})
MODE = "sign-only"


def hash_line(sha256):
    return sha256 + "  signed.aab\n"


def report(record):
    """The lines a sign-only run prints, from its record. It never claims an upload."""
    return ["Signed, not uploaded: nothing was sent to Google Play. Published for one manual Play Console upload:",
            "  package: " + record["app_id"],
            "  versionCode: " + record["build_number"],
            "  signed AAB SHA-256: " + record["sha256"],
            "  artifact: " + SEALED + " (encrypted to the Console-upload key), " + HASH_FILE + " and " +
            SIGN_ONLY_RECORD + ", kept 3 days",
            "  encrypted file SHA-256: " + record["encrypted_sha256"],
            "Next (Samuel): download the artifact, decrypt it with age -d -i <your key> -o signed.aab " + SEALED +
            ", check shasum -a 256 -c " + HASH_FILE + ", then upload signed.aab once in Play Console "
            "(Testing > Internal testing)."]


def seal_signed(target, source_sha, build_number):
    app_id = identity("android", target, source_sha, build_number)
    require_step_secrets(())
    if os.environ.get("MAY_SIGN_ONLY") != "true":
        raise StoreError("Sign-only was not requested; nothing was published")
    if os.environ.get("MAY_DRY_RUN") != "true":
        raise StoreError("Sign-only runs only with dry_run: true; nothing was published")
    if ("android", target) not in SIGN_ONLY_TARGETS:
        raise StoreError("Sign-only is only for the May production Android app; nothing was published")
    if not build_number_floor_ok("android", target, build_number):
        raise StoreError("Build number is below this app's floor (May production starts at 3)")
    recipient = artifact_recipient(SIGN_ONLY_RECIPIENT)
    if recipient is None:
        raise StoreError("The Console-upload public key is still a placeholder; nothing was published")
    if recipient in {artifact_recipient(environment) for environment in ENVIRONMENTS.values()}:
        raise StoreError("The Console-upload key must not be an environment's handoff key; nothing was published")
    root = runner_root()
    signed = root / "may-release/signed.aab"
    if not signed.is_file() or signed.is_symlink() or signed.stat().st_size == 0:
        raise StoreError("Signed Android App Bundle is unavailable")
    try:
        signer = verify.aab_signer_sha256(signed)
    except verify.VerifyError:
        raise StoreError("Signed Android App Bundle has no single verifiable signer") from None
    check_android_signer(target, signer)
    package, version_code = verify.check_aab_identity(signed, app_id, build_number)
    sha256 = file_sha256(signed)
    out = root / SIGN_ONLY_DIR
    if out.exists() or out.is_symlink():
        raise StoreError("The sign-only folder already exists; nothing was published")
    private = root / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    check_key, check_plain = private / "sign-only-check.key", private / "sign-only-check.aab"
    sealed = out / SEALED
    quiet = dict(stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=child_env())
    out.mkdir(mode=0o700)
    try:
        try:
            if subprocess.run([toolchain.tool_path("age", "age-keygen"), "-o", str(check_key)],
                              stdout=subprocess.DEVNULL, **quiet).returncode:
                raise StoreError("Sign-only check identity could not be made; nothing was published")
            derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(check_key)],
                                     stdout=subprocess.PIPE, **quiet)
            check_recipient = derived.stdout.decode().strip()
            if derived.returncode or not AGE_RECIPIENT.fullmatch(check_recipient) or check_recipient == recipient:
                raise StoreError("Sign-only check identity could not be made; nothing was published")
            if subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-r", check_recipient,
                               "-o", str(sealed), str(signed)], stdout=subprocess.DEVNULL, **quiet).returncode:
                raise StoreError("Signed bundle encryption failed; nothing was published")
            write_private(check_plain, b"")  # 0600 before age writes the plaintext into it
            if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(check_key), "-o",
                               str(check_plain), str(sealed)], stdout=subprocess.DEVNULL, **quiet).returncode or \
                    file_sha256(check_plain) != sha256:
                raise StoreError("The encrypted bundle does not decrypt to the signed bundle; nothing was published")
        finally:
            check_key.unlink(missing_ok=True)
            check_plain.unlink(missing_ok=True)
        record = {"mode": MODE, "platform": "android", "app_id": package, "build_number": version_code,
                  "sha256": sha256, "encrypted_sha256": file_sha256(sealed)}
        write_private(out / SIGN_ONLY_RECORD, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
        write_private(out / HASH_FILE, hash_line(sha256).encode())
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return report(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        lines = seal_signed(args.target, args.source_sha, args.build_number)
    except (StoreError, verify.VerifyError) as error:
        raise SystemExit("May sign-only refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May sign-only failed; nothing was published") from None
    print("\n".join(lines))
