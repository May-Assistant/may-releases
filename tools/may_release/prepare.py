"""Quiet, exact private-source checkout for a protected May runner.

No private source, commit metadata, provider output, or credential is printed.
The build-local design-system path replacement is made only after exact pin checks.
"""

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from store import SOURCE_READ_SECRETS, StoreError, child_env, require_step_secrets

APP = "git@github.com:May-Assistant/may-app.git"
DS = "git@github.com:May-Assistant/may-design-system.git"
DS_URL = "https://github.com/May-Assistant/may-design-system"
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


class PrepareError(ValueError):
    pass


def exact_sha(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise PrepareError("Source SHA is not exact")
    return value


def pins(ios_project, android_lock):
    match = re.search(r"(?m)^  MayDesignSystem:\n    url: " + re.escape(DS_URL) +
                      r"\n    revision: ([0-9a-f]{40})$", ios_project)
    lock = dict(line.split("=", 1) for line in android_lock.splitlines() if "=" in line)
    if not match or lock.get("repository") not in (DS_URL, DS_URL + ".git") or lock.get("revision") != match.group(1):
        raise PrepareError("iOS and Android design-system pins differ or are untrusted")
    return match.group(1), match.group(0)


def private_runner_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw:
        raise PrepareError("Runner temporary directory is unavailable")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise PrepareError("Runner temporary directory is unavailable")
    return root


def quiet(argv, log, *, env=None):
    with log.open("ab") as output:
        result = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
    if result.returncode:
        raise PrepareError("Private checkout failed; inspect protected runner diagnostics")


def git_status(argv, *, env=None):
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=env).returncode


def fetch(dest, remote, sha, key_text, private, label, *, main_history=False):
    exact_sha(sha)
    if dest.exists() or not key_text.strip():
        raise PrepareError("Private checkout path or scoped read key is unavailable")
    key, known, log = private / (label + ".key"), private / "known_hosts", private / (label + ".log")
    ssh = " ".join(shlex.quote(p) for p in (
        "ssh", "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UserKnownHostsFile=" + str(known)))
    # git and ssh get an allowlisted environment: never the read keys or any GIT_* override.
    env = child_env(GIT_SSH_COMMAND=ssh, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                    GIT_CONFIG_GLOBAL=os.devnull, GIT_LFS_SKIP_SMUDGE="1")
    try:
        # The key is written inside the try so that a failed write (for example
        # a full disk part-way through) still removes the partial key file.
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as secret_file:
            secret_file.write(key_text.strip() + "\n")
        known.write_text(GITHUB_HOST_KEY + "\n")
        known.chmod(0o600)
        quiet(["git", "init", "-q", str(dest)], log, env=env)
        quiet(["git", "-C", str(dest), "remote", "add", "origin", remote], log, env=env)
        ref = "+refs/heads/main:refs/remotes/origin/main" if main_history else sha
        quiet(["git", "-C", str(dest), "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
               "origin", ref], log, env=env)
    finally:
        key.unlink(missing_ok=True)
        known.unlink(missing_ok=True)
    if git_status(["git", "-C", str(dest), "cat-file", "-e", sha + "^{commit}"], env=env):
        raise PrepareError("Exact private commit was not fetched")
    if main_history and git_status(["git", "-C", str(dest), "merge-base", "--is-ancestor", sha,
                                    "refs/remotes/origin/main"], env=env):
        raise PrepareError("May app source SHA is not in main history")
    quiet(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "--quiet",
           "--detach", sha], log, env=env)
    if git_status(["git", "-C", str(dest), "diff", "--quiet", "HEAD", "--"], env=env):
        raise PrepareError("Private checkout differs from exact commit")


def prepare(source_sha):
    require_step_secrets(SOURCE_READ_SECRETS)
    root = private_runner_root()
    private = root / "may-private"
    private.mkdir(mode=0o700)
    source = root / "may-source"
    fetch(source, APP, exact_sha(source_sha), os.environ.get("MAY_APP_READ_KEY", ""),
          private, "app", main_history=True)
    project_path, lock_path = source / "ios/project.yml", source / "android/design-system.lock"
    revision, old_spec = pins(project_path.read_text(), lock_path.read_text())
    ds_checkout = root / "may-design-system"
    fetch(ds_checkout, DS, revision, os.environ.get("MAY_DS_READ_KEY", ""), private, "ds")
    ds_local = source / "android/build-deps/may-design-system"
    if ds_local.exists():
        raise PrepareError("Design-system build path already exists")
    if not ds_local.parent.resolve().is_relative_to(source.resolve()):
        raise PrepareError("Design-system build path escapes private source")
    ds_local.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ds_checkout), str(ds_local))
    project = project_path.read_text()
    project_path.write_text(project.replace(old_spec, "  MayDesignSystem:\n    path: " + str(ds_local), 1))
    print("Exact private May app and design-system sources prepared on protected runner")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        prepare(args.source_sha)
    except (PrepareError, StoreError, OSError, subprocess.SubprocessError):
        raise SystemExit("Private May source preparation failed; no source or credential was printed") from None
