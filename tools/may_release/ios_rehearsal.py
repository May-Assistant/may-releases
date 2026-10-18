"""Rehearsal of may-app's `tools/release/ios.sh archive-unsigned` at an open may-app pull request head
(ios-archive-rehearsal.yml). No signing identity, store call or credential: ios.sh signs the archive ad hoc.

This repository is public and may-app and the design system are private, so nothing ios.sh prints reaches this
log or the artifact. Its output goes to a private log under may-private, which the cleanup deletes. ios.sh's own
build record (its `may-release-record:` line) names the design-system revision and digests of private files, so it
is never published either: this script checks it against what was asked and prepared, and writes a fixed-shape
summary of its own (RECORD, in may-rehearsal), whose every value is one of the dispatch's inputs, a constant of
this repository, a fixed word, an integer exit status or a SHA-256 this script recomputed over the archive.

  archive  (no secret; runs ios.sh, which runs the pull request's own build code) records the outcome privately and
           writes the summary; a failed archive does not fail this step, so the summary is still published.
  record   (no secret) rechecks the summary's shape against the inputs and rewrites it canonically before upload.
  verdict  (no secret) fails the job unless the summary says the archive was made and checked.

REHEARSAL ONLY: ios.sh requires the commit to be in origin/main's history. A pull request head is not, so `archive`
points this runner's own refs/remotes/origin/main, in the private checkout, at the head before ios.sh runs (as
may-app's ios-release-rehearsal.yml does). Nothing is pushed; the summary says so. A release builds only from main.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from compile import credentials_left
from store import (APP_IDS, APPLE_TEAM, DESIGN_SYSTEM_RECORD, XCODE_DEVELOPER_DIR, StoreError, build_number_ok,
                   child_env, require_step_secrets)

# The toolchain may-app's .github/workflows/ios-ci.yml pins, and ios.sh requires: Xcode 26.3, build 17C529.
XCODE_VERSION, XCODE_BUILD = "26.3", "17C529"
XCODE = XCODE_VERSION + " (" + XCODE_BUILD + ")"
PRODUCT = "May.xcarchive"
SCHEMA = "may-ios-archive-rehearsal/1"
RECORD_DIR, RECORD = "may-rehearsal", "ios-archive-rehearsal.json"
LOG = "ios-rehearsal.log"
# An archive build (derived data, packages, the archive and its copy) needs room; refuse early rather than fail late.
MIN_FREE_BYTES = 15 * 1024**3
RECORD_PREFIX = b"may-release-record: "
# ios.sh's record, exactly these keys (may-app PR #116), plus, from ios.sh with PostHog crash reports on, a
# posthog_key flag (whether MAY_POSTHOG_IOS_KEY was given: never the key). A rehearsal holds no key, so the flag
# must be false when present. A pull request head from before that flag still records without it. The record
# stays in the private log.
IOS_RECORD_KEYS = {"platform", "target", "app_id", "source_sha", "build_number", "version", "team_id_unverified",
                   "design_system", "xcode", "minimum_ios", "package_resolved_sha256", "product", "sha256", "rehearsal"}
IOS_RECORD_POSTHOG = "posthog_key"
# Why a rehearsal did not archive: fixed words only, never anything ios.sh printed.
REASONS = ("archived and checked", "ios.sh exited non-zero", "ios.sh did not start",
           "ios.sh's record is missing or malformed", "ios.sh's record differs from the request",
           "the product is not exactly May.xcarchive", "the archive differs from ios.sh's record")
ORIGIN_MAIN = "pointed at the pull request head on this runner only; nothing was pushed"
SUMMARY_KEYS = {"schema", "result", "reason", "ios_sh_exit", "pr_number", "source_sha", "target", "app_id",
                "build_number", "xcode", "product", "archive_tree_sha256", "design_system_pin", "origin_main",
                "signing", "archive_uploaded"}
SIGNING = "ad hoc only (no team, certificate or profile)"
DESIGN_SYSTEM_CHECKED = "the iOS pin prepare.py checked out"


class RehearsalError(ValueError):
    pass


def inputs(target, source_sha, build_number, pr_number):
    if (("ios", target) not in APP_IDS or not re.fullmatch(r"[0-9a-f]{40}", source_sha or "") or
            not build_number_ok(build_number) or not re.fullmatch(r"[1-9][0-9]{0,6}", pr_number or "")):
        raise RehearsalError("Rehearsal inputs are not exact (target, source SHA, build number, pull request)")
    return APP_IDS[("ios", target)]


def tree_sha256(top):
    """A digest of a whole tree: every path, its type, executable bits, and file contents or link target, in sorted
    order. The same digest ios.sh records as `sha256`, recomputed here so the summary does not take it on trust."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(top):
        dirs.sort()
        for name in sorted(dirs + files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, top).encode()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                digest.update(b"L\0" + rel + b"\0" + os.readlink(path).encode() + b"\0")
            elif stat.S_ISDIR(info.st_mode):
                digest.update(b"D\0" + rel + b"\0")
            elif stat.S_ISREG(info.st_mode):
                content = hashlib.sha256()
                with Path(path).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        content.update(chunk)
                digest.update(b"F\0" + rel + b"\0" + oct(info.st_mode & 0o111).encode() + b"\0" +
                              content.hexdigest().encode() + b"\0")
            else:
                raise RehearsalError("The archive holds an unexpected file type")
    return digest.hexdigest()


def ios_record(log):
    """ios.sh's one `may-release-record:` line from the private log, parsed, or None."""
    lines = [line for line in log.read_bytes().splitlines() if line.startswith(RECORD_PREFIX)]
    if len(lines) != 1:
        return None
    try:
        record = json.loads(lines[0][len(RECORD_PREFIX):])
    except ValueError:
        return None
    if not isinstance(record, dict) or set(record) not in (IOS_RECORD_KEYS, IOS_RECORD_KEYS | {IOS_RECORD_POSTHOG}):
        return None
    return record


def record_matches(record, target, app_id, source_sha, build_number, ios_pin):
    expected = {"platform": "ios", "target": target, "app_id": app_id, "source_sha": source_sha,
                "build_number": build_number, "team_id_unverified": APPLE_TEAM, "design_system": ios_pin,
                "xcode": XCODE, "product": PRODUCT, "rehearsal": True}
    return all(record.get(key) == value for key, value in expected.items()) and \
        record.get(IOS_RECORD_POSTHOG, False) is False and \
        isinstance(record.get("sha256"), str) and bool(re.fullmatch(r"[0-9a-f]{64}", record["sha256"]))


def summary(reason, status, pr_number, source_sha, target, app_id, build_number, sha256=None):
    archived = reason == REASONS[0]
    return {"schema": SCHEMA, "result": "archived" if archived else "failed", "reason": reason,
            "ios_sh_exit": status, "pr_number": int(pr_number), "source_sha": source_sha, "target": target,
            "app_id": app_id, "build_number": build_number, "xcode": XCODE,
            "product": PRODUCT if archived else None, "archive_tree_sha256": sha256 if archived else None,
            "design_system_pin": DESIGN_SYSTEM_CHECKED if archived else None, "origin_main": ORIGIN_MAIN,
            "signing": SIGNING, "archive_uploaded": False}


def git(argv, log):
    env = child_env(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    with log.open("ab") as output:
        return subprocess.run(["git", "-c", "core.fsmonitor=false"] + argv, stdout=output, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, env=env).returncode


def xcode_is_pinned():
    env = child_env(DEVELOPER_DIR=XCODE_DEVELOPER_DIR)
    result = subprocess.run(["xcodebuild", "-version"], capture_output=True, stdin=subprocess.DEVNULL, env=env)
    return not result.returncode and result.stdout.splitlines()[:2] == [b"Xcode " + XCODE_VERSION.encode(),
                                                                       b"Build version " + XCODE_BUILD.encode()]


def runner_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise RehearsalError("Runner temporary directory is unavailable")
    return Path(raw).resolve()


def write_summary(root, value):
    folder = root / RECORD_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    (folder / RECORD).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def archive(target, source_sha, build_number, pr_number, run=subprocess.run, pinned=xcode_is_pinned):
    """Run ios.sh archive-unsigned in the prepared checkout with store.py's environment, and write the summary."""
    # ios.sh runs the pull request's own build code (XcodeGen, SwiftPM, Xcode build phases): no secret at all.
    require_step_secrets(())
    app_id = inputs(target, source_sha, build_number, pr_number)
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not private.is_dir():
        raise RehearsalError("Exact private source is not prepared")
    if credentials_left(private):
        raise RehearsalError("A read key or host file is still on disk; nothing was archived")
    try:
        prepared = json.loads((private / DESIGN_SYSTEM_RECORD).read_text())
    except (OSError, ValueError):
        raise RehearsalError("The prepared design-system record is missing") from None
    pins = prepared.get("design_system") if isinstance(prepared, dict) else None
    ios_pin = pins.get("ios") if isinstance(pins, dict) else None
    if ios_pin is None or (prepared.get("platform"), prepared.get("target"), prepared.get("source_sha")) != ("ios", target, source_sha) or \
            not isinstance(ios_pin, str) or not re.fullmatch(r"[0-9a-f]{40}", ios_pin):
        raise RehearsalError("The prepared checkout is not this rehearsal's (platform, target or source SHA)")
    stats = os.statvfs(root)
    if stats.f_bavail * stats.f_frsize < MIN_FREE_BYTES:
        raise RehearsalError("Runner has insufficient free space for an archive")
    if not pinned():
        raise RehearsalError("Pinned Xcode 26.3 (17C529) is unavailable")
    script = source / "tools/release/ios.sh"
    if not script.is_file() or script.is_symlink():
        raise RehearsalError("tools/release/ios.sh is not in this may-app commit")
    log = private / LOG
    # REHEARSAL ONLY: a pull request head is not in main; ios.sh checks origin/main's history (module docstring).
    if git(["-C", str(source), "rev-parse", "--verify", "-q", "refs/remotes/origin/main"], log) == 0 or \
            git(["-C", str(source), "update-ref", "refs/remotes/origin/main", source_sha], log):
        raise RehearsalError("Could not point this runner's origin/main at the pull request head")
    out = root / "may-release"
    out.mkdir(mode=0o700)
    # store.py's environment for ios.sh (store.build), plus MAY_REHEARSAL=1, which only marks ios.sh's record.
    env = child_env(MAY_TARGET=target, MAY_SOURCE_SHA=source_sha, MAY_BUILD_NUMBER=build_number, MAY_APP_ID=app_id,
                    MAY_IOS_APP_ID=app_id, MAY_TEAM_ID=APPLE_TEAM, MAY_ARTIFACT_DIR=str(out),
                    DEVELOPER_DIR=XCODE_DEVELOPER_DIR, GIT_TERMINAL_PROMPT="0",
                    MAY_DS_READ_TOKEN="prepared-local-design-system-only", MAY_REHEARSAL="1")
    try:
        with log.open("ab") as output:
            status = run(["bash", str(script), "archive-unsigned"], cwd=source, env=env, stdout=output,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        status = None
    sha256 = None
    if status is None:
        reason, status = REASONS[2], -1
    elif status:
        reason = REASONS[1]
    else:
        record = ios_record(log)
        made = out / PRODUCT
        if record is None:
            reason = REASONS[3]
        elif not record_matches(record, target, app_id, source_sha, build_number, ios_pin):
            reason = REASONS[4]
        elif [p.name for p in out.iterdir()] != [PRODUCT] or made.is_symlink() or not made.is_dir():
            reason = REASONS[5]
        else:
            try:
                sha256 = tree_sha256(made)
            except (RehearsalError, OSError):
                sha256 = None
            reason = REASONS[0] if sha256 == record["sha256"] else REASONS[6]
    write_summary(root, summary(reason, int(status), pr_number, source_sha, target, app_id, build_number, sha256))
    print(f"May iOS archive rehearsal finished; ios.sh exit status {int(status)}; its log stays on this runner")


def checked_summary(value, target, source_sha, build_number, pr_number):
    """The summary, only if it has exactly the fixed shape and the dispatch's own values; otherwise None."""
    app_id = inputs(target, source_sha, build_number, pr_number)
    if not isinstance(value, dict) or set(value) != SUMMARY_KEYS or value.get("reason") not in REASONS:
        return None
    status = value.get("ios_sh_exit")
    if not isinstance(status, int) or isinstance(status, bool) or not -1 <= status <= 255:
        return None
    sha256 = value.get("archive_tree_sha256")
    expected = summary(value["reason"], status, pr_number, source_sha, target, app_id, build_number,
                       sha256 if isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256) else None)
    if value["reason"] == REASONS[0] and (status != 0 or expected["archive_tree_sha256"] is None):
        return None
    return expected if value == expected else None


def record(target, source_sha, build_number, pr_number):
    """Recheck the summary before it is published (the build ran code from the pull request), and rewrite it."""
    require_step_secrets(())
    path = runner_root() / RECORD_DIR / RECORD
    if not path.is_file() or path.is_symlink() or [p.name for p in path.parent.iterdir()] != [RECORD]:
        raise RehearsalError("The rehearsal summary is missing, or its folder holds anything else")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = None
    checked = checked_summary(value, target, source_sha, build_number, pr_number)
    if checked is None:
        raise RehearsalError("The rehearsal summary is not exactly its fixed shape; nothing was published")
    path.write_text(json.dumps(checked, sort_keys=True, indent=2) + "\n")
    print("May iOS archive rehearsal summary checked; only it is published, never the archive")


def verdict(target, source_sha, build_number, pr_number):
    require_step_secrets(())
    try:
        value = json.loads((runner_root() / RECORD_DIR / RECORD).read_text())
    except (OSError, ValueError):
        value = None
    checked = checked_summary(value, target, source_sha, build_number, pr_number)
    if checked is None or checked["result"] != "archived":
        raise RehearsalError("ios.sh archive-unsigned did not archive and pass every check; see the published summary")
    print("May iOS archive rehearsal passed: one ad-hoc-signed May.xcarchive, checked, never uploaded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("archive", "record", "verdict"))
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--pr-number", required=True)
    args = parser.parse_args()
    step = {"archive": archive, "record": record, "verdict": verdict}[args.command]
    try:
        step(args.target, args.source_sha, args.build_number, args.pr_number)
    except (RehearsalError, StoreError) as error:
        raise SystemExit("May iOS archive rehearsal refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May iOS archive rehearsal failed; no private detail was published") from None
