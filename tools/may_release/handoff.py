"""Encrypted, run-bound handoff of the unsigned product from `build` to `release`.

The repository is public, so its Actions artifacts are readable. `seal` (in the
build job) puts the product and a manifest into a tar and encrypts it with age
to the target's public key, committed under artifact_keys/. `open` (in the
release job, on a fresh VM) decrypts it with that environment's
MAY_ARTIFACT_DECRYPT_KEY, checks the key matches the committed public key, and
refuses the payload unless its manifest names exactly this repository, run,
platform, target, source SHA and build number.

age is authenticated encryption (ChaCha20-Poly1305), so the payload cannot be
altered without failing decryption. It is not sender authentication: anyone
holding the public key can encrypt. The run-bound manifest stops replay or
swapping of another run's or target's artifact. It cannot vouch for what the
build job produced, which is why signing re-verifies the product itself.
"""

import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from gate import ENVIRONMENTS, artifact_recipient
from store import (HANDOFF_SECRETS, StoreError, child_env, identity, require_step_secrets, runner_root,
                   write_private)
import toolchain

FORMAT = "may-handoff-1"
PRODUCTS = {"android": "unsigned.aab", "ios": "May.xcarchive"}
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")


def expected_manifest(platform, target, source_sha, build_number):
    return {"format": FORMAT, "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""), "platform": platform, "target": target,
            "source_sha": source_sha, "build_number": build_number, "product": PRODUCTS[platform]}


def recipient_for(platform, target):
    recipient = artifact_recipient(ENVIRONMENTS[(platform, target)])
    if recipient is None:
        raise StoreError("Artifact encryption public key for this target is still a placeholder")
    return recipient


def write_tar(path, manifest, product):
    with tarfile.open(path, "w") as archive:
        data = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        archive.add(product, arcname="product/" + product.name)


def read_tar(path, staging, expected):
    """Extract safely and return the product path, refusing anything unexpected."""
    if not hasattr(tarfile, "data_filter"):
        raise StoreError("Python lacks safe tar extraction")
    with tarfile.open(path) as archive:
        names = archive.getnames()
        if len(names) != len(set(names)) or names[:1] != ["manifest.json"] or not all(
                n == "product/" + expected["product"] or n.startswith("product/" + expected["product"] + "/")
                for n in names[1:]) or len(names) < 2:
            raise StoreError("Build handoff contents are unexpected")
        try:
            manifest = json.loads(archive.extractfile("manifest.json").read())
        except (ValueError, AttributeError):
            raise StoreError("Build handoff manifest is invalid") from None
        if manifest != expected:
            raise StoreError("Build handoff does not match this run, target, source or build number")
        staging.mkdir(mode=0o700)
        archive.extractall(staging, members=archive.getmembers()[1:], filter="data")
    return staging / "product" / expected["product"]


def seal(platform, target, source_sha, build_number):
    identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    recipient = recipient_for(platform, target)
    root = runner_root()
    product = root / "may-release" / PRODUCTS[platform]
    if not product.exists() or product.is_symlink():
        raise StoreError("Unsigned product is unavailable")
    handoff = root / "may-handoff"
    handoff.mkdir(mode=0o700)
    plain = handoff / "handoff.tar"
    try:
        write_tar(plain, expected_manifest(platform, target, source_sha, build_number), product)
        result = subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o",
                                 str(handoff / "handoff.age"), str(plain)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=child_env())
        if result.returncode:
            raise StoreError("Build handoff encryption failed")
    finally:
        plain.unlink(missing_ok=True)
    print("Unsigned May product encrypted to the target's public key for the release job")


def open_handoff(platform, target, source_sha, build_number):
    identity(platform, target, source_sha, build_number)
    require_step_secrets(HANDOFF_SECRETS)
    recipient = recipient_for(platform, target)
    # The secret may be the whole age-keygen file; its "#" lines are comments.
    lines = [l.strip() for l in os.environ.get("MAY_ARTIFACT_DECRYPT_KEY", "").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise StoreError("Artifact decryption key is malformed")
    secret = lines[0]
    root = runner_root()
    sealed = root / "may-handoff-download/handoff.age"
    if not sealed.is_file() or sealed.is_symlink():
        raise StoreError("Encrypted build handoff is unavailable")
    private, out = root / "may-private", root / "may-release"
    private.mkdir(mode=0o700, exist_ok=True)
    out.mkdir(mode=0o700)
    key, plain, staging = private / "handoff.key", private / "handoff.tar", private / "handoff-open"
    env = child_env()
    quiet = dict(stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    try:
        try:
            write_private(key, (secret + "\n").encode())
            derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)],
                                     stdout=subprocess.PIPE, **quiet)
            if derived.returncode or derived.stdout.decode().strip() != recipient:
                raise StoreError("Artifact decryption key does not match this target's committed public key")
            if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain),
                               str(sealed)], stdout=subprocess.DEVNULL, **quiet).returncode:
                raise StoreError("Build handoff failed to decrypt or authenticate")
        finally:
            key.unlink(missing_ok=True)
        product = read_tar(plain, staging, expected_manifest(platform, target, source_sha, build_number))
        shutil.move(str(product), str(out / product.name))
    finally:
        plain.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    print("Build handoff decrypted and bound to this run, target, source and build number")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seal", "open"))
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    try:
        (seal if args.mode == "seal" else open_handoff)(
            args.platform, args.target, args.source_sha, args.build_number)
    except StoreError as error:
        raise SystemExit("May build handoff refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May build handoff failed; no private detail was published") from None
