"""Quiet, exact private-source checkout for a protected May runner.

No private source, commit metadata, provider output, or credential is printed.
The build-local design-system path replacement is made only after exact pin checks.

Design-system pins (Samuel's decision, 2026-10-14): a store build checks the
pin of the platform it builds, which must be well-formed, trusted and exactly
what is checked out. For an internal target (store.INTERNAL_TARGETS) the iOS and
Android pins may differ; for any other target, and for the native compile, they
must match. Both pins are recorded for the sealed handoff on every build.

A pull-request compile (--pr-number, native-compile-pr.yml) fetches may-app's
refs/pull/<number>/head instead of main and requires it to be exactly the source
SHA; gate.py has already checked, through the API, that it is an open pull request
against main from may-app itself. Nothing else differs.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import subprocess

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (DESIGN_SYSTEM_RECORD, SOURCE_READ_SECRETS, StoreError, child_env, design_system_pins,
                   require_step_secrets, write_private)

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


def pins(ios_project, android_lock, platform=None, target=None):
    """Both design-system pins, each well-formed and trusted; returns (record, iOS spec).

    The record is {"ios": ..., "android": ...}. store.design_system_pins then
    requires the two to match unless (platform, target) is internal.

    Both pins are checked for form and trust on every build, including the one
    that is not built or compared. The iOS spec parsed here is the text this
    script later replaces in project.yml, and the record carries both values
    into the sealed manifest: drift is only meaningful between two exact
    revisions of the one trusted repository. A pin that points elsewhere, or
    at a branch or path, is not drift but a different dependency for the same
    commit, which will also be built for the other platform.
    """
    match = re.search(r"(?m)^  MayDesignSystem:\n    url: " + re.escape(DS_URL) +
                      r"\n    revision: ([0-9a-f]{40})$", ios_project)
    if not match:
        raise PrepareError("iOS design-system pin is malformed or untrusted")
    lock = dict(line.split("=", 1) for line in android_lock.splitlines() if "=" in line)
    if (lock.get("repository") not in (DS_URL, DS_URL + ".git") or
            not re.fullmatch(r"[0-9a-f]{40}", lock.get("revision") or "")):
        raise PrepareError("Android design-system pin is malformed or untrusted")
    record = design_system_pins({"ios": match.group(1), "android": lock["revision"]}, platform, target)
    return record, match.group(0)


def head_revision(checkout):
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"],
                            capture_output=True, stdin=subprocess.DEVNULL,
                            env=child_env(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull))
    return result.stdout.decode().strip() if not result.returncode else ""


def checkout_is(checkout, revision):
    """Whether checkout is exactly revision, with no change to a tracked file. The build could write
    the checkout's own config, so no fsmonitor hook from it runs (as integrity.py)."""
    env = child_env(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return head_revision(checkout) == revision and not git_status(
        ["git", "-c", "core.fsmonitor=false", "-C", str(checkout), "diff", "--quiet", "HEAD", "--"], env=env)


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


def pull_ref(value):
    if not re.fullmatch(r"[1-9][0-9]{0,6}", str(value)):
        raise PrepareError("Pull request number is not exact")
    return f"refs/pull/{value}/head"


def fetch(dest, remote, sha, key_text, private, label, *, main_history=False, pull=None):
    exact_sha(sha)
    if main_history and pull is not None:
        raise PrepareError("Main history and a pull request head are exclusive")
    pull_head = pull_ref(pull) if pull is not None else None
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
        ref = ("+refs/heads/main:refs/remotes/origin/main" if main_history else
               "+" + pull_head + ":refs/remotes/origin/pull-head" if pull_head else sha)
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
    if pull_head and head_of(dest, "refs/remotes/origin/pull-head", env) != sha:
        raise PrepareError("May app source SHA is not the pull request's head")
    quiet(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "--quiet",
           "--detach", sha], log, env=env)
    if git_status(["git", "-C", str(dest), "diff", "--quiet", "HEAD", "--"], env=env):
        raise PrepareError("Private checkout differs from exact commit")


def head_of(checkout, ref, env):
    result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", ref + "^{commit}"],
                            capture_output=True, stdin=subprocess.DEVNULL, env=env)
    return result.stdout.decode().strip() if not result.returncode else ""


def prepare(source_sha, platform=None, target=None, pull=None):
    """Fetch the exact app commit and the design system pinned for `platform`.

    With a platform and target (a store build), only that platform's pin is
    fetched, and for an internal target the other pin may differ. Without them
    (the native compile), the two pins must match. Both pins are written to
    may-private/DESIGN_SYSTEM_RECORD for handoff.py to seal.
    """
    require_step_secrets(SOURCE_READ_SECRETS)
    if (platform is None) != (target is None):
        raise PrepareError("Platform and target must be given together")
    root = private_runner_root()
    private = root / "may-private"
    private.mkdir(mode=0o700)
    source = root / "may-source"
    if pull is None:
        fetch(source, APP, exact_sha(source_sha), os.environ.get("MAY_APP_READ_KEY", ""),
              private, "app", main_history=True)
    else:
        fetch(source, APP, exact_sha(source_sha), os.environ.get("MAY_APP_READ_KEY", ""),
              private, "app", pull=pull)
    project_path, lock_path = source / "ios/project.yml", source / "android/design-system.lock"
    record, old_spec = pins(project_path.read_text(), lock_path.read_text(), platform, target)
    revision = record[platform or "ios"]
    ds_checkout = root / "may-design-system"
    fetch(ds_checkout, DS, revision, os.environ.get("MAY_DS_READ_KEY", ""), private, "ds")
    ds_local = source / "android/build-deps/may-design-system"
    if ds_local.exists():
        raise PrepareError("Design-system build path already exists")
    if not ds_local.parent.resolve().is_relative_to(source.resolve()):
        raise PrepareError("Design-system build path escapes private source")
    ds_local.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ds_checkout), str(ds_local))
    if head_revision(ds_local) != revision:
        raise PrepareError("Design-system checkout is not the built platform's pinned revision")
    # project.yml is pointed only at a local checkout of its own pinned revision, and only for a build that reads it
    # (iOS, or the native compile). An Android build leaves it exactly as committed, even when the pins match: may-app's
    # own tests read its pin (the design-system pin parity test, run by tools/release/android.sh and android-unit-pr.yml).
    if platform != "android" and revision == record["ios"]:
        project = project_path.read_text()
        project_path.write_text(project.replace(old_spec, "  MayDesignSystem:\n    path: " + str(ds_local), 1))
    write_private(private / DESIGN_SYSTEM_RECORD,
                  json.dumps({"platform": platform, "target": target, "source_sha": source_sha,
                              "design_system": record}, sort_keys=True).encode())
    print("Exact private May app and design-system sources prepared on protected runner")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    # A store build names both; the native compile names neither (pins must match).
    parser.add_argument("--platform", choices=("ios", "android"))
    parser.add_argument("--target", choices=("production", "playground"))
    # A pull-request compile names the may-app pull request whose head is --source-sha.
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    try:
        prepare(args.source_sha, args.platform, args.target, args.pr_number)
    except (PrepareError, StoreError, OSError, subprocess.SubprocessError):
        raise SystemExit("Private May source preparation failed; no source or credential was printed") from None
