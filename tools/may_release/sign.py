"""Sign and verify the unsigned May product with this public repository's code.

This runs in the `release` job, on a fresh VM where no build toolchain or other
third-party build code has run: only this repository's Python, the pinned and
checksum-verified Temurin jarsigner, and Apple's own tools. The step that runs
it receives only its platform's signing secrets: no source read key, artifact
key or store upload key. Secret material is written to
0600 files under RUNNER_TEMP/may-private and removed before this exits.
Signing tools get a minimal environment that holds only the password variables
they read by name. Tool output stays in private runner logs.

Android: jarsigner signs the AAB; jarsigner -verify checks every digest, and
verify.py checks that every entry is covered and that the one signer
certificate's SHA-256 equals MAY_ANDROID_CERT_SHA256. Before signing, the alias
and that pin must be the target's own upload key (store.ANDROID_UPLOAD_KEYS), so a
Playground key can never sign May production, or the reverse.

iOS: a throwaway keychain holds the distribution identity; xcodebuild exports
an internal-TestFlight-only IPA; verify.py checks the embedded profile's team
and bundle ID and the app's bundle ID and build number.
"""

import argparse
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import sys
import subprocess

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (ANDROID_SIGNING_SECRETS, APPLE_CERT_SHA1, APPLE_TEAM, IOS_SIGNING_SECRETS,
                   XCODE_DEVELOPER_DIR, StoreError, check_android_upload_key, child_env, identity,
                   require_step_secrets, run_private, runner_root, secret_bytes, write_private)
import toolchain
import verify

# Apple's tools by absolute path; xcodebuild honours DEVELOPER_DIR.
SECURITY, XCODEBUILD = "/usr/bin/security", "/usr/bin/xcodebuild"
CODESIGN, DITTO = "/usr/bin/codesign", "/usr/bin/ditto"


def jarsigner():
    return toolchain.tool_path("jdk", "bin/jarsigner")


def verify_signed_aab(signed, expected_sha256, log):
    expected = verify.normalize_fingerprint(expected_sha256)
    result = subprocess.run([jarsigner(), "-verify", "-verbose", "-certs", str(signed)],
                            capture_output=True, stdin=subprocess.DEVNULL, env=child_env())
    with log.open("ab") as output:
        output.write(result.stdout + result.stderr)
    if result.returncode or b"jar verified." not in result.stdout:
        raise StoreError("Signed Android App Bundle failed jarsigner verification")
    if verify.aab_signer_sha256(signed) != expected:
        raise StoreError("Android signer certificate differs from MAY_ANDROID_CERT_SHA256")


def sign_android(target, source_sha, build_number):
    identity("android", target, source_sha, build_number)
    require_step_secrets(ANDROID_SIGNING_SECRETS)
    alias = os.environ.get("MAY_ANDROID_KEY_ALIAS", "")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}", alias):
        raise StoreError("Android key alias metadata is invalid")
    expected = verify.normalize_fingerprint(os.environ.get("MAY_ANDROID_CERT_SHA256", ""))
    # The environment's alias and certificate pin must be this target's own upload
    # key (store.ANDROID_UPLOAD_KEYS), so the other app's key never signs this one.
    check_android_upload_key(target, alias, expected)
    root = runner_root()
    private, out = root / "may-private", root / "may-release"
    unsigned, signed = out / "unsigned.aab", out / "signed.aab"
    if not unsigned.is_file() or unsigned.is_symlink() or signed.exists():
        raise StoreError("Unsigned Android App Bundle is unavailable")
    if verify.signature_entries(unsigned):
        raise StoreError("Unsigned Android App Bundle already carries a signature")
    log = private / "android-sign.log"
    keystore = private / "upload.keystore"
    try:
        write_private(keystore, secret_bytes("MAY_ANDROID_KEYSTORE_BASE64"))
        env = child_env(MAY_ANDROID_STORE_PASSWORD=os.environ.get("MAY_ANDROID_STORE_PASSWORD", ""),
                        MAY_ANDROID_KEY_PASSWORD=os.environ.get("MAY_ANDROID_KEY_PASSWORD", ""))
        run_private([jarsigner(), "-keystore", str(keystore),
                     "-storepass:env", "MAY_ANDROID_STORE_PASSWORD",
                     "-keypass:env", "MAY_ANDROID_KEY_PASSWORD",
                     "-signedjar", str(signed), str(unsigned), alias], private, log, env)
    finally:
        keystore.unlink(missing_ok=True)
    try:
        verify_signed_aab(signed, expected, log)
    except BaseException:
        signed.unlink(missing_ok=True)
        raise
    unsigned.unlink()
    print("May Android App Bundle signed and its signer certificate verified")


def sign_ios(target, source_sha, build_number):
    app_id = identity("ios", target, source_sha, build_number)
    require_step_secrets(IOS_SIGNING_SECRETS)
    root = runner_root()
    private, out = root / "may-private", root / "may-release"
    archive, ipa = out / "May.xcarchive", out / "May.ipa"
    if not archive.is_dir() or archive.is_symlink() or ipa.exists():
        raise StoreError("Unsigned iOS archive is unavailable")
    profile_bytes = secret_bytes("MAY_IOS_PROFILE_BASE64")
    uuid = verify.check_profile(verify.profile_plist(profile_bytes), APPLE_TEAM, app_id, APPLE_CERT_SHA1)
    home = Path(os.environ.get("HOME", "")).resolve()
    installed = [home / "Library/Developer/Xcode/UserData/Provisioning Profiles" / (uuid + ".mobileprovision"),
                 home / "Library/MobileDevice/Provisioning Profiles" / (uuid + ".mobileprovision")]
    work = private / "ios-signing"
    work.mkdir(mode=0o700)
    keychain = work / "may-signing.keychain-db"
    log = private / "ios-sign.log"
    env = child_env(DEVELOPER_DIR=XCODE_DEVELOPER_DIR)
    password = secrets.token_hex(32)
    created, written = False, []
    try:
        p12 = work / "distribution.p12"
        write_private(p12, secret_bytes("MAY_IOS_CERT_BASE64"))
        for path in installed:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private(path, profile_bytes)  # O_EXCL: never replaces an existing profile
            written.append(path)
        run_private([SECURITY, "create-keychain", "-p", password, str(keychain)], work, log, env)
        created = True
        run_private([SECURITY, "set-keychain-settings", "-lut", "3600", str(keychain)], work, log, env)
        run_private([SECURITY, "unlock-keychain", "-p", password, str(keychain)], work, log, env)
        # security(1) takes the PKCS#12 password only as an argument. It is
        # visible to same-user processes for this call, which could equally read
        # the 0600 .p12 itself; no step or tool environment exposes it.
        run_private([SECURITY, "import", str(p12), "-k", str(keychain), "-f", "pkcs12",
                     "-P", os.environ.get("MAY_IOS_CERT_PASSWORD", ""), "-T", "/usr/bin/codesign"],
                    work, log, env)
        p12.unlink()
        run_private([SECURITY, "set-key-partition-list", "-S", "apple-tool:,apple:", "-s",
                     "-k", password, str(keychain)], work, log, env)
        listed = subprocess.run([SECURITY, "list-keychains", "-d", "user"], capture_output=True,
                                stdin=subprocess.DEVNULL, env=env)
        if listed.returncode:
            raise StoreError("Keychain search list is unavailable")
        existing = [line.strip().strip('"') for line in listed.stdout.decode().splitlines() if line.strip()]
        run_private([SECURITY, "list-keychains", "-d", "user", "-s", str(keychain), *existing],
                    work, log, env)
        identities = subprocess.run([SECURITY, "find-identity", "-v", "-p", "codesigning", str(keychain)],
                                    capture_output=True, stdin=subprocess.DEVNULL, env=env)
        if identities.returncode or APPLE_CERT_SHA1.encode() not in identities.stdout.upper():
            raise StoreError("Approved Apple distribution identity is not in the signing keychain")
        options = work / "ExportOptions.plist"
        options.write_bytes(plistlib.dumps({
            "method": "app-store-connect", "destination": "export", "teamID": APPLE_TEAM,
            "signingStyle": "manual", "signingCertificate": APPLE_CERT_SHA1,
            "provisioningProfiles": {app_id: uuid}, "testFlightInternalTestingOnly": True,
            "manageAppVersionAndBuildNumber": False, "uploadSymbols": True}))
        exported = work / "export"
        run_private([XCODEBUILD, "-exportArchive", "-archivePath", str(archive),
                     "-exportPath", str(exported), "-exportOptionsPlist", str(options)], work, log, env)
        ipas = list(exported.glob("*.ipa"))
        if len(ipas) != 1 or ipas[0].is_symlink():
            raise StoreError("iOS export must produce exactly one IPA")
        shutil.move(str(ipas[0]), str(ipa))
    finally:
        if created:
            subprocess.run([SECURITY, "delete-keychain", str(keychain)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
        for path in written:
            path.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    try:
        verify.ipa_identity(ipa, APPLE_TEAM, app_id, build_number)
        verify.ipa_icon(ipa)
        ipa_push(ipa, private, env)
    except BaseException:
        ipa.unlink(missing_ok=True)
        raise
    print("May iOS IPA signed for internal TestFlight and its profile verified")
    print("IPA app icon: Info.plist names CFBundleIconName AppIcon, and Assets.car names AppIcon")
    print("IPA push: the app's signed entitlements have aps-environment, set to production")


def ipa_push(ipa, private, env):
    """The exported app's signed entitlements, as codesign reads them from the IPA unpacked into private scratch,
    checked by verify.check_push; the scratch is removed whatever happens."""
    unpacked = private / "ios-entitlements"
    unpacked.mkdir(mode=0o700)
    try:
        if subprocess.run([DITTO, "-x", "-k", str(ipa), str(unpacked)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env).returncode:
            raise StoreError("IPA push check failed: the IPA could not be unpacked")
        payload = unpacked / "Payload"
        apps = [app for app in payload.iterdir() if app.suffix == ".app" and app.is_dir() and not app.is_symlink()] \
            if payload.is_dir() and not payload.is_symlink() else []
        if len(apps) != 1:
            raise StoreError("IPA push check failed: the IPA does not hold exactly one app")
        read = subprocess.run([CODESIGN, "-d", "--entitlements", "-", "--xml", str(apps[0])], capture_output=True,
                              stdin=subprocess.DEVNULL, env=env)
        verify.check_push(read.stdout if not read.returncode else b"")
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        (sign_ios if args.platform == "ios" else sign_android)(
            args.target, args.source_sha, args.build_number)
    except (StoreError, verify.VerifyError) as error:
        raise SystemExit("Protected May signing refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("Protected May signing failed; no private detail was published") from None
