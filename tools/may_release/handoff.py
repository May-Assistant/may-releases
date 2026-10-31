"""Encrypted, run-bound handoff of the unsigned product from `build` to `release`.

The repository is public, so its Actions artifacts are readable. `seal` (in the
build job) puts the product and a manifest into a tar and encrypts it with age
to the target's public key, committed under artifact_keys/. `open` (in the
release job, on a fresh VM) decrypts it with that environment's
MAY_ARTIFACT_DECRYPT_KEY, checks the key matches the committed public key, and
refuses the payload unless its manifest names exactly this repository, run,
platform, target, source SHA and build number.

An iOS manifest also carries the build's TestFlight "What to test" notes (`notes`, testflight_notes.py), its one
field besides the pins that the release job reads: private may-app pull-request titles, so they travel only inside
the encrypted handoff, and open writes them to the release job's private folder for the notes step. An Android
manifest has none.

The manifest also carries both design-system pins (`design_system`: iOS and
Android) that prepare.py recorded, so drift is recorded on every release. Both
sides re-apply store.design_system_pins: the pins may differ only for an
internal target. `seal` also rechecks that the design-system checkout is still
exactly the built platform's pin. The public log says only whether they match;
the revisions themselves stay inside the encrypted handoff.

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
import posixpath
import re
import shutil
import sys
import subprocess
import tarfile
import unicodedata

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gate import ENVIRONMENTS, artifact_recipient
from prepare import checkout_is
from testflight_notes import NOTES_FILE, checked_notes
from store import (DESIGN_SYSTEM_RECORD, HANDOFF_SECRETS, StoreError, child_env, design_system_pins, identity,
                   require_step_secrets, runner_root, write_private)
import toolchain

FORMAT = "may-handoff-1"
PRODUCTS = {"android": "unsigned.aab", "ios": "May.xcarchive"}
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")


def expected_manifest(platform, target, source_sha, build_number):
    return {"format": FORMAT, "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""), "platform": platform, "target": target,
            "source_sha": source_sha, "build_number": build_number, "product": PRODUCTS[platform]}


def recorded_pins(root, platform, target, source_sha):
    """Both pins prepare.py recorded for exactly this build, rechecked against the checkout."""
    try:
        record = json.loads((root / "may-private" / DESIGN_SYSTEM_RECORD).read_text())
    except (OSError, ValueError):
        raise StoreError("Design-system pin record is missing or malformed") from None
    if not isinstance(record, dict) or sorted(record) != ["design_system", "platform", "source_sha", "target"] or \
            (record["platform"], record["target"], record["source_sha"]) != (platform, target, source_sha):
        raise StoreError("Design-system pin record is not for this platform, target and source")
    pins = design_system_pins(record["design_system"], platform, target)
    # The build ran in between: the design system it used must still be exactly the built pin.
    if not checkout_is(root / "may-source/android/build-deps/may-design-system", pins[platform]):
        raise StoreError("Design-system checkout is no longer exactly the built platform's pin")
    return pins


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


BUNDLE_SUFFIXES = (".app", ".appex", ".framework", ".bundle", ".xctest", ".dSYM", ".xcarchive")


def bundle_root(name, product_root):
    """The deepest bundle directory (an .app, .framework, ...) containing `name`."""
    parts = name.split("/")
    root = product_root
    for depth in range(len(product_root.split("/")), len(parts)):
        if parts[depth - 1].endswith(BUNDLE_SUFFIXES):
            root = "/".join(parts[:depth])
    return root


def inside(path, root):
    return path == root or path.startswith(root + "/")


def fold(name):
    """How a case- and normalization-insensitive filesystem (APFS on the macOS runner) sees a name."""
    return unicodedata.normalize("NFC", name).casefold()


def claim(path, by_fold):
    """Register path and each of its prefixes; refuse a name that aliases a different one."""
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        exact = "/".join(parts[:depth])
        if by_fold.setdefault(fold(exact), exact) != exact:
            raise StoreError("Build handoff names alias each other on a case-insensitive filesystem")


def resolve(path, links, product_root, by_fold, limit=40):
    """Resolve an archive path through the archive's own symlinks, never leaving product_root.

    Every component is compared as APFS would see it: one that folds to an
    existing member's name but is not byte-identical is refused.
    """
    pending, resolved, hops = path.split("/"), [], 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                raise StoreError("Build handoff contains an unsafe symlink")
            resolved.pop()
            if not inside("/".join(resolved), product_root):
                raise StoreError("Build handoff contains an unsafe symlink")
            continue
        candidate = "/".join(resolved + [part])
        if by_fold.get(fold(candidate), candidate) != candidate:
            raise StoreError("Build handoff names alias each other on a case-insensitive filesystem")
        if candidate in links:
            hops += 1
            if hops > limit:
                raise StoreError("Build handoff contains a symlink loop")
            # links[] holds the full archive path (link's directory + target).
            pending, resolved = links[candidate].split("/") + pending, []
            continue
        resolved.append(part)
    return "/".join(resolved)


def checked_members(archive, product_root, allow_symlinks):
    """Validate every product member before anything is written.

    Only regular files, directories and (iOS only) relative symlinks. Every
    symlink, followed through any chain of other symlinks in the archive, must
    land on an existing file or directory inside its own bundle (an .app,
    .framework, ... or else the product root). No absolute, dangling or escaping
    target, no hard link, device or fifo, and nothing written through a symlink.
    Paths must already be normal, relative and under product_root. This does not
    rely on tarfile's extraction filters.
    """
    members, seen, links, by_fold = archive.getmembers()[1:], set(), {}, {}
    if not members or members[0].name != product_root:
        raise StoreError("Build handoff contents are unexpected")
    for member in members:
        name = member.name
        if (not name or name.startswith("/") or "\\" in name or "\0" in name or name in seen
                or posixpath.normpath(name) != name or ".." in name.split("/")
                or not inside(name, product_root)):
            raise StoreError("Build handoff contains an unsafe path")
        claim(name, by_fold)
        if any(inside(name, link) for link in links):
            raise StoreError("Build handoff writes through a symlink")
        if member.issym():
            target = member.linkname
            if not allow_symlinks or not target or target.startswith("/") or "\0" in target or "\\" in target:
                raise StoreError("Build handoff contains an unsafe symlink")
            links[name] = posixpath.dirname(name) + "/" + target
        elif not (member.isfile() and member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)) and not member.isdir():
            raise StoreError("Build handoff contains an unsupported entry type")
        seen.add(name)
    entries = seen - set(links)
    for name, joined in links.items():
        final = resolve(joined, links, product_root, by_fold)
        if final not in entries or not inside(final, bundle_root(name, product_root)):
            raise StoreError("Build handoff contains an unsafe symlink")
    return members


def strict_resolve(path):
    return path.resolve(strict=True)


def extract(archive, members, product_root, destination, resolver=strict_resolve):
    """Write the validated members under destination, one by one, never following links.

    Before every write, the parent must resolve (through the real filesystem, so
    with APFS case and normalization folding) to exactly the folder the member
    names: no symlink anywhere in the chain. Directories are created one level at
    a time and checked the same way. `resolver` is injectable so tests can model
    a case-insensitive filesystem on Linux.
    """
    base = len(product_root) - len(product_root.split("/")[-1])
    real_root = resolver(destination)
    created = []
    for member in members:
        relative = member.name[base:].split("/")
        path = destination.joinpath(*relative)
        expected_parent = real_root.joinpath(*relative[:-1])
        try:
            actual_parent = resolver(path.parent)
        except (OSError, RuntimeError):
            raise StoreError("Build handoff entry has no extracted parent directory") from None
        if actual_parent != expected_parent:
            raise StoreError("Build handoff would write outside its own folder")
        if member.isdir():
            path.mkdir(mode=0o700)
            if resolver(path) != expected_parent / relative[-1]:
                raise StoreError("Build handoff would write outside its own folder")
        elif member.issym():
            os.symlink(member.linkname, path)
            created.append((path, member))
        else:
            mode = 0o700 if member.mode & 0o100 else 0o600
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(archive.extractfile(member), output)
    # Re-check every symlink where it now lives, before any signing secret exists
    # on this VM: it must resolve, strictly (so not dangling), inside its own bundle.
    for path, member in created:
        bundle = (destination / bundle_root(member.name, product_root)[base:]).resolve(strict=True)
        try:
            final = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise StoreError("Build handoff contains an unsafe symlink") from None
        if not final.is_relative_to(bundle):
            raise StoreError("Build handoff contains an unsafe symlink")


def read_tar(path, destination, expected, allow_symlinks):
    """Validate, then extract, the product into destination; return (its path, both pins, the iOS notes or None)."""
    product_root = "product/" + expected["product"]
    with tarfile.open(path) as archive:
        names = archive.getnames()
        if len(names) < 2 or names[0] != "manifest.json" or not archive.getmembers()[0].isfile():
            raise StoreError("Build handoff contents are unexpected")
        try:
            manifest = json.loads(archive.extractfile("manifest.json").read())
        except (ValueError, AttributeError):
            raise StoreError("Build handoff manifest is invalid") from None
        if not isinstance(manifest, dict):
            raise StoreError("Build handoff manifest is invalid")
        pins = manifest.pop("design_system", None)
        notes = manifest.pop("notes", None)
        if manifest != expected:
            raise StoreError("Build handoff does not match this run, target, source or build number")
        pins = design_system_pins(pins, expected["platform"], expected["target"])
        if (notes is None) != (expected["platform"] != "ios") or (notes is not None and checked_notes(notes) is None):
            raise StoreError("Build handoff notes are missing, malformed or not for this platform")
        members = checked_members(archive, product_root, allow_symlinks)
        if (members[0].isdir()) != (expected["platform"] == "ios"):
            raise StoreError("Build handoff contents are unexpected")
        extract(archive, members, product_root, destination)
    return destination / expected["product"], pins, notes


def seal(platform, target, source_sha, build_number):
    identity(platform, target, source_sha, build_number)
    require_step_secrets(())
    recipient = recipient_for(platform, target)
    root = runner_root()
    pins = recorded_pins(root, platform, target, source_sha)
    extra = {}
    if platform == "ios":
        path = root / "may-private" / NOTES_FILE
        try:
            notes = checked_notes(path.read_text(encoding="utf-8")) if not path.is_symlink() else None
        except (OSError, UnicodeDecodeError):
            notes = None
        if notes is None:
            raise StoreError("TestFlight notes are missing or malformed; testflight_notes.py compose runs first")
        extra["notes"] = notes
    product = root / "may-release" / PRODUCTS[platform]
    if not product.exists() or product.is_symlink():
        raise StoreError("Unsigned product is unavailable")
    handoff = root / "may-handoff"
    handoff.mkdir(mode=0o700)
    plain = handoff / "handoff.tar"
    try:
        write_tar(plain, dict(expected_manifest(platform, target, source_sha, build_number), design_system=pins,
                              **extra), product)
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
        raise StoreError("Re-run all jobs: this release only accepts a build from the same run attempt")
    private, out = root / "may-private", root / "may-release"
    private.mkdir(mode=0o700, exist_ok=True)
    out.mkdir(mode=0o700)
    key, plain = private / "handoff.key", private / "handoff.tar"
    env = child_env()
    quiet = dict(stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    try:
        try:
            write_private(key, (secret + "\n").encode())
            derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)],
                                     stdout=subprocess.PIPE, **quiet)
            if derived.returncode or derived.stdout.decode().strip() != recipient:
                raise StoreError("Artifact decryption key does not match this target's committed public key")
            write_private(plain, b"")  # 0600 before age writes the plaintext into it
            if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain),
                               str(sealed)], stdout=subprocess.DEVNULL, **quiet).returncode:
                raise StoreError("Build handoff failed to decrypt or authenticate")
        finally:
            key.unlink(missing_ok=True)
        try:
            _, pins, notes = read_tar(plain, out, expected_manifest(platform, target, source_sha, build_number),
                                      allow_symlinks=platform == "ios")
            if notes is not None:
                write_private(private / NOTES_FILE, notes.encode("utf-8"))
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)
            raise
    finally:
        plain.unlink(missing_ok=True)
    print("Build handoff decrypted and bound to this run, target, source and build number")
    if pins["ios"] == pins["android"]:
        print("Design-system pins recorded in the sealed manifest: iOS and Android match")
    else:
        print("Design-system pins recorded in the sealed manifest: iOS and Android differ "
              "(allowed for this internal target only)")


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
