"""Refuse to continue unless the checkout is exactly its commit.

Run as `python3 -I -S -B tools/may_release/integrity.py` in each release job
before every secret step. It imports only the standard library. It fails if git
reports any modified, untracked or ignored file anywhere in the checkout (for
example a module planted beside these scripts, or a stray __pycache__), and if
any file under tools/may_release/ differs from the blob, mode or file list that
`git ls-files -s` records for the checked-out commit.
"""

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = "tools/may_release"


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, stdin=subprocess.DEVNULL,
                            env=env)
    if result.returncode:
        raise ValueError("git failed")
    return result.stdout


def problems(root=None):
    root = root or ROOT
    if git(root, "status", "--porcelain=v1", "--ignored", "--untracked-files=all", "-z"):
        return ["Checkout has modified, untracked or ignored files"]
    recorded = {}
    for line in git(root, "ls-files", "-s", "-z", "--", SCRIPTS).split(b"\0"):
        if line:
            meta, _, path = line.partition(b"\t")
            mode, blob, _ = meta.split()
            recorded[path.decode()] = (mode.decode(), blob.decode())
    present = {str(p.relative_to(root)) for p in (root / SCRIPTS).rglob("*") if not p.is_dir() or p.is_symlink()}
    if present != set(recorded):
        return ["Release scripts differ from the commit's file list"]
    for path, (mode, blob) in recorded.items():
        full = root / path
        info = full.lstat()
        if not stat.S_ISREG(info.st_mode):
            return ["A release script is not a regular file"]
        data = full.read_bytes()
        actual = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if actual != blob or mode != ("100755" if info.st_mode & 0o100 else "100644"):
            return ["A release script differs from the checked-out commit"]
    return []


if __name__ == "__main__":
    try:
        found = problems()
    except (OSError, ValueError):
        found = ["Checkout integrity could not be read"]
    if found:
        raise SystemExit("Release checkout integrity refused: " + found[0])
    print("Release checkout matches its commit")
