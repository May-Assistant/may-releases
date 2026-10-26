"""A pull request lane's private record, carried from the job that runs the pull request's own code to a fresh job that
posts it, sealed (independent reviews of may-releases #130 and #133).

The job that runs a may-app head's own code (its Gradle build, its npm tests) runs it as the runner user, who has
passwordless sudo, and the runner keeps every secret a job references in memory. So that job references no GitHub App
key and no token: only read-only deploy keys. Its record (its outcome and failing tests, which name may-app's own
tests) leaves it only sealed:

  seal  (the job that ran the code, no secret) reads the named private JSON files, bundles them with the tested commit
        ({sha, files}), pads the bundle to SIZE so its size says nothing, and age-encrypts it to RECIPIENT, the
        environment's own key pair (never device tests' or renders'), as SEALED/NAME for the one upload. Without the
        recipient nothing is sealed (the caller says so in a fixed line).
  open  (the post job, DECRYPT_KEY alone) checks the key against RECIPIENT, decrypts the downloaded DOWNLOAD/NAME,
        checks its size, commit and file names, and writes each file back into the private folder, where the lane's
        own post reads and checks it as before. Nothing sealed: nothing opened.

Nothing here prints; errors are fixed literals (static_check.py).
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gate import AGE_RECIPIENT
from store import child_env, write_private
import toolchain

RECIPIENT, DECRYPT_KEY = "MAY_RENDERS_RECIPIENT", "MAY_RENDERS_DECRYPT_KEY"
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")
SHA = re.compile(r"[0-9a-f]{40}")
PRIVATE, SEALED, DOWNLOAD, NAME = "may-private", "may-sealed", "may-sealed-download", "record.age"
# Every sealed record is exactly SIZE bytes before encryption: its UTF-8 JSON (not escaped to ASCII, so a lane's
# longest record of non-ASCII names and messages fits) then spaces. A bundle that would not fit is refused.
SIZE = 1024 * 1024


class SealedError(ValueError):
    pass


def seal(root, sha, names):
    """The private files names (in root/PRIVATE), with sha, sealed to RECIPIENT as root/SEALED/NAME; True, or False
    when RECIPIENT is unset (nothing sealed)."""
    if not SHA.fullmatch(sha or ""):
        raise SealedError("Source SHA is not an exact lowercase commit SHA")
    private, files = root / PRIVATE, {}
    for name in names:
        path = private / name
        # The pull request's code ran before this step: only a regular file is read, never a link it left.
        if not path.is_file() or path.is_symlink():
            raise SealedError("A private record file is missing; nothing was sealed")
        try:
            files[name] = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            raise SealedError("A private record file is not JSON; nothing was sealed") from None
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        return False
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise SealedError("MAY_RENDERS_RECIPIENT is not an age public key")
    data = json.dumps({"sha": sha, "files": files}, ensure_ascii=False).encode()
    if len(data) > SIZE:
        raise SealedError("The record is too large to seal")
    out, plain = root / SEALED, private / "record.plain"
    out.mkdir(mode=0o700, exist_ok=True)
    try:
        write_private(plain, data + b" " * (SIZE - len(data)))
        if subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(out / NAME), str(plain)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          env=child_env()).returncode:
            raise SealedError("The record could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    return True


def open_sealed(root, sha, names):
    """The downloaded root/DOWNLOAD/NAME, decrypted with DECRYPT_KEY (taken out of the environment at once) after the
    key is checked against RECIPIENT, checked (its size, its commit sha, exactly the files names) and written back into
    root/PRIVATE; True, or False when nothing was sealed."""
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    if not SHA.fullmatch(sha or ""):
        raise SealedError("Source SHA is not an exact lowercase commit SHA")
    sealed = root / DOWNLOAD / NAME
    if not sealed.is_file() or sealed.is_symlink():
        return False
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise SealedError("MAY_RENDERS_RECIPIENT is not set to an age public key")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise SealedError("The decryption key is malformed (MAY_RENDERS_DECRYPT_KEY)")
    private = root / PRIVATE
    private.mkdir(mode=0o700, exist_ok=True)
    key, plain, env = private / "record.key", private / "record.part", child_env()
    try:
        write_private(key, (lines[0] + "\n").encode())
        derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)], capture_output=True,
                                 stdin=subprocess.DEVNULL, env=env)
        if derived.returncode or derived.stdout.decode().strip() != recipient:
            raise SealedError("The decryption key does not match MAY_RENDERS_RECIPIENT")
        write_private(plain, b"")
        if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain), str(sealed)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          env=env).returncode or plain.stat().st_size != SIZE:
            raise SealedError("The record failed to decrypt or authenticate")
        try:
            record = json.loads(plain.read_bytes().decode())
        except (ValueError, RecursionError):
            raise SealedError("The record is malformed or for another commit") from None
        if not (isinstance(record, dict) and set(record) == {"sha", "files"} and record["sha"] == sha and
                isinstance(record["files"], dict) and set(record["files"]) == set(names)):
            raise SealedError("The record is malformed or for another commit")
        for name in names:
            write_private(private / name, json.dumps(record["files"][name]).encode())
    finally:
        key.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
    return True
