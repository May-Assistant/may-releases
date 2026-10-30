"""Publish the iOS renders to the private repository May-Assistant/may-renders (ios-renders.yml, the publish job).

may-renders holds nothing but renders: may-app is on GitHub's free plan, where a branch cannot be protected, so a
write deploy key there could push to its main. The deploy key here (MAY_APP_RENDERS_WRITE_KEY) writes may-renders
alone.

The publish job runs on a fresh runner that never runs may-app code: the render job, which ran may-app's build and
snapshot tests, holds no secret and hands over only one age-encrypted file (renders.py seal). Each step here holds
exactly one secret, read into memory and removed from the environment before any child process starts:

- open (MAY_RENDERS_DECRYPT_KEY): checks the key against the environment's public key (the variable
  MAY_RENDERS_RECIPIENT), decrypts, and extracts only the run folder: <may-app short sha>/ holding index.html,
  screens.json, build.log, and PNGs under ios/, android/ and sheet/, every name of a fixed shape, every entry a plain
  file or folder, with size caps. Anything else refuses. With no sealed file it says so and the steps after it
  publish nothing.
- push (MAY_APP_RENDERS_WRITE_KEY): adds the run folder to may-renders' main, records it first in runs.json, removes
  the run folders beyond the newest KEEP, and pushes main, never by force (the first push creates it).
- comment (MAY_APP_PR_COMMENT_TOKEN), for a pull request's renders alone: one comment per head on that may-app pull
  request linking the run folder and its contact sheet, after reading the pull request again to check it is still
  open at that head.

This repository is public, so its log carries counts, the dispatch's own inputs and fixed words alone: nothing read
from may-app, and no git or API output.
"""

import argparse
import html
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ds_compile import API, DsCompileError, MAX_BODY, comment_author, post_once, request, run_url
from gate import AGE_RECIPIENT, APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import GITHUB_HOST_KEY
import board
import device_tests
from renders import PNG, RECIPIENT
from review_mcp import FOOTER, holds
from store import StoreError, child_env, require_step_secrets, runner_root, write_private
import toolchain

RENDERS_REPO = "May-Assistant/may-renders"
REMOTE = "git@github.com:" + RENDERS_REPO + ".git"
BRANCH = "main"
KEEP = 10
DECRYPT_KEY, WRITE_KEY, COMMENT_TOKEN = "MAY_RENDERS_DECRYPT_KEY", "MAY_APP_RENDERS_WRITE_KEY", "MAY_APP_PR_COMMENT_TOKEN"
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")
SSH_KEY = re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----\n[A-Za-z0-9+/=\n]{100,10000}\n-----END OPENSSH PRIVATE KEY-----")
SHA = re.compile(r"[0-9a-f]{40}")
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,140}\.png")
KINDS = ("ios", "android", "sheet")
# A renders run folder (a may-app short SHA) or a device-tests one (<UTC date>-<short SHA>).
RUN_FOLDER = re.compile(r"[0-9a-f]{12}|" + board.FOLDER.pattern)
DEVICE_RUN = board.FOLDER
DEVICES = ("android", "ios")
# may-app's flows and board notes, sealed by each device job and set aside by device-open for the flow board
# (board.py), never committed.
BOARD_INPUTS, BOARD, DROPPED = ("flows.json", "board-notes.json"), "may-device-board", ".dropped"
DEVICE_FILE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}(?:-end\.png|-look-[1-4]\.png|\.mp4|-part[0-9]{1,2}\.mp4|\.preview\.mp4|\.xml|"
                         r"\.diag\.txt)|report\.json|"
                         + "|".join(re.escape(name) for name in BOARD_INPUTS) + "|" + board.PATH_FILE.pattern + "|" +
                         board.SHOT_FILE.pattern)
DEVICE_DOWNLOAD, DEVICE_PREFIX = "may-device-download", "device-tests/"
VERDICT_WORDS = ("pass", "ui-pass", "fail", "skipped", device_tests.SMOKE_PASS, device_tests.UNVERIFIED)
REASONS = ("outward actions are off (MAY_DEVICE_TESTS_OUTWARD)", "may-app has no Maestro flow for this scenario",
           device_tests.UNCONFIGURED, device_tests.RATE_LIMITED, device_tests.UNFINISHED, device_tests.BLOCKED,
           device_tests.NO_TRUTH, device_tests.NOT_JUDGED, device_tests.SWITCHED_OFF, device_tests.SMOKE_MAESTRO,
           device_tests.BUDGET, *device_tests.UNTAGGED.values(), device_tests.FRESH_EXHAUSTED, device_tests.NOT_FRESH,
           device_tests.MODEL_CAP, device_tests.MINT_FAILED, device_tests.FLOW_IDS, device_tests.DELIVERY_FAILED,
           device_tests.NOT_REACHED, device_tests.WEDGED, device_tests.NO_CHAT,
           device_tests.NO_FIRST, device_tests.NOT_STARTED, device_tests.HALTED, device_tests.NO_LINE,
           device_tests.NO_CARD, device_tests.LOOK_SCROLLED, device_tests.LOOK_EMPTY, device_tests.LOOK_UNREAD)
MAX_NOTES = 600
# A chat scenario's message codes (device_tests.message_codes): codes alone, in a fixed shape.
DELIVERY = re.compile(r"(?:[a-z][a-z0-9_]{0,63}(?: \([a-z][a-z0-9_]{0,63}\))?|no run|" + re.escape(device_tests.NONE_STORED)
                      + ")")
# The sealed device artifact's retention (device-tests.yml): the recordings' fallback when the release upload fails.
VIDEO_DAYS = 7
# The recordings go to one may-renders release per run (tag device-tests-<run>), never into its git history; the
# newest KEEP such releases are kept. Only device-release holds the token, a fine-grained token on may-renders alone.
RELEASE_TOKEN, RENDERS_REPO = "MAY_RENDERS_RELEASE_TOKEN", "May-Assistant/may-renders"
UPLOADS = "https://uploads.github.com"
VIDEOS = "may-device-videos"
RELEASE_TAG = re.compile(r"device-tests-(?:" + board.FOLDER.pattern + ")")
_RELEASES = r"/repos/" + re.escape(RENDERS_REPO) + r"/releases"
RELEASE_CALLS = (("POST", API, _RELEASES), ("GET", API, _RELEASES + r"/tags/" + RELEASE_TAG.pattern),
                 ("GET", API, _RELEASES + r"\?per_page=100"),
                 ("GET", API, _RELEASES + r"/[1-9][0-9]{0,15}/assets\?per_page=100"),
                 ("POST", UPLOADS, _RELEASES + r"/[1-9][0-9]{0,15}/assets\?name=(?:android|ios)-[a-z0-9][a-z0-9-]{0,60}"
                  r"(?:(?:\.preview)?\.mp4|\.diag\.txt)"),
                 ("DELETE", API, _RELEASES + r"/[1-9][0-9]{0,15}"),
                 ("DELETE", API, r"/repos/" + re.escape(RENDERS_REPO) + r"/git/refs/tags/" + RELEASE_TAG.pattern))
MAX_FILES = 2000
MAX_FILE = 40 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
DOWNLOAD, TREE = "may-renders-download/renders.age", "may-publish-tree"
# The render job's upload's artifact id, set when it handed sealed renders over (ios-renders.yml, run 41068959429).
HANDED = "MAY_RENDERS_HANDED"
MARKER = "may-ios-renders"
AUTHOR = ("-c", "user.name=May renders", "-c", "user.email=renders@may-releases.invalid")
README = ("# May parity renders and device tests\n\nWritten by may-releases, never by hand.\n\n"
          "- ios-renders.yml: one folder per may-app commit (its first 12 hex digits), the newest " + str(KEEP) +
          " kept (runs.json lists them, newest first). Each holds README.md (open the folder) and index.html (Android | "
          "iOS per screen and mode), "
          "sheet/ (the two side by side), ios/, android/, the manifest copy and build.log.\n"
          "- device-tests.yml: device-tests/<UTC date>-<may-app short sha>/, the newest " + str(KEEP) + " kept "
          "(device-tests/runs.json). Each holds README.md (open the folder: verdicts and screenshots), index.html, and "
          "per device the screenshots, Maestro reports and report.json. Recordings are never committed: they are assets "
          "of the release device-tests-<folder> (the newest " + str(KEEP) + " kept), and the run's encrypted workflow "
          "artifact holds them for " + str(VIDEO_DAYS) + " days.\n")


class PublishError(ValueError):
    pass


def folder_of(sha):
    if not SHA.fullmatch(sha or ""):
        raise PublishError("Source SHA is not an exact lowercase commit SHA")
    return sha[:12]


def allowed(name, folder):
    """Whether a tar member name is part of the run folder's fixed layout: "dir", "file" or None."""
    parts = name.split("/")
    if parts[0] != folder or len(parts) > 3:
        return None
    if len(parts) == 1:
        return "dir"
    if len(parts) == 2:
        return "dir" if parts[1] in KINDS else "file" if parts[1] in ("index.html", "README.md", "screens.json", "build.log") \
            else None
    return "file" if parts[1] in KINDS and FILE.fullmatch(parts[2]) else None


def checked_members(archive, folder, layout=allowed, required="index.html"):
    """The archive's members, every one checked against layout(name, folder) before anything is written."""
    members, total, seen = archive.getmembers(), 0, set()
    if not members or len(members) > MAX_FILES:
        raise PublishError("The renders hold no run folder or too many files")
    for member in members:
        name = member.name
        kind = layout(name, folder)
        if kind is None or name in seen or "\\" in name or "\0" in name:
            raise PublishError("The renders hold a path outside the run folder's layout")
        seen.add(name)
        if kind == "dir" and not member.isdir() or kind == "file" and not (
                member.isfile() and member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)):
            raise PublishError("The renders hold an entry that is not a plain file or folder")
        if member.isfile():
            if member.size > MAX_FILE:
                raise PublishError("A render is too large")
            total += member.size
    if total > MAX_TOTAL or not any(name == folder + "/" + required or name.endswith("/" + required) and
                                    name.startswith(folder + "/") for name in seen):
        raise PublishError("The renders are too large or have no contact sheet")
    # Parents before children, as renders.py seal writes them.
    for name in seen:
        if "/" in name and name.rsplit("/", 1)[0] not in seen:
            raise PublishError("The renders hold a path outside the run folder's layout")
    return members


def is_json(data):
    try:
        json.loads(data)
    except (ValueError, RecursionError):  # deep nesting raises RecursionError, not ValueError
        return False
    return True


def diagnostics_ok(data):
    """An Android scenario's diagnostics (device_tests.Diagnostics): small UTF-8 text with no line that could carry a
    session or a credential and no unmasked email address, checked again here."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # The very rule that wrote it, line by line: a file kept_lines would change at all (a line to drop, an address to
    # mask) is refused, and a file it leaves alone is never refused.
    return len(data) <= device_tests.MAX_DIAG and device_tests.kept_lines(data, "") == (data, 0)


def extract(archive, members, destination):
    """The checked members, written under destination; returns the names of optional files it dropped: may-app board
    files that are not JSON, and previews that are not small MP4s."""
    dropped = []
    for member in sorted(members, key=lambda m: m.name.count("/")):
        path = destination.joinpath(*member.name.split("/"))
        if member.isdir():
            path.mkdir(mode=0o700)
            continue
        data = archive.extractfile(member).read()
        # The flow prototypes' paths and screens are optional too: a bad one is dropped (board.py checks the rest).
        if board.PATH_FILE.fullmatch(path.name) and not is_json(data) or \
                board.SHOT_FILE.fullmatch(path.name) and (not data.startswith(PNG) or len(data) > board.MAX_SHOT):
            dropped.append(path.name)
            continue
        if path.name in BOARD_INPUTS and not is_json(data):
            # may-app's board files are optional: a malformed one costs the board its content, never the run.
            dropped.append(path.name)
            continue
        # "x": created here, never an existing file or a link to one.
        with path.open("xb") as output:
            output.write(data)
        if path.suffix == ".png" and not data.startswith(PNG):
            raise PublishError("A render is not a PNG")
        if path.name.endswith(device_tests.DIAG) and not diagnostics_ok(data):
            path.unlink()  # optional: one too large, not UTF-8, or with a line that could carry a session is dropped
            dropped.append(path.name)
            continue
        if path.name.endswith(device_tests.PREVIEW) and (data[4:8] != b"ftyp" or len(data) > device_tests.MAX_PREVIEW):
            path.unlink()  # a preview is optional: one that is not a small MP4 is dropped, never decoded here
            dropped.append(path.name)
            continue
        if path.suffix == ".mp4" and data[4:8] != b"ftyp":
            raise PublishError("A recording is not an MP4")
        if path.name in ("screens.json", "report.json"):
            try:
                json.loads(data)
            except (ValueError, RecursionError):
                raise PublishError("The manifest copy is not JSON") from None
        if path.name in ("index.html", "README.md") and re.search(rb"(?i)<\s*(script|iframe|object|embed)|javascript:", data):
            raise PublishError("The contact sheet holds active content")
    return dropped


def open_renders(sha):
    require_step_secrets((DECRYPT_KEY,))
    folder = folder_of(sha)
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    root = runner_root()
    sealed = root / DOWNLOAD
    if not sealed.is_file() or sealed.is_symlink():
        # Run 41068959429: publish alone re-run as attempt 2 looked for attempt 2's renders, found none, and the run
        # went green with nothing published. Renders the render job handed over (its upload's artifact id) that never
        # arrived fail the run.
        if os.environ.get(HANDED, "").strip():
            raise PublishError("The render job handed over sealed renders, but they did not reach this job; nothing "
                               "was published")
        print("May iOS renders: no sealed renders from this run attempt; nothing will be published")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise PublishError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-ios-renders")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise PublishError("The renders decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-ios-renders)")
    private = root / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    plain, tree = private / "renders.tar", root / TREE
    try:
        decrypt(sealed, plain, lines[0], recipient, private)
        tree.mkdir(mode=0o700)
        try:
            with tarfile.open(plain) as archive:
                extract(archive, checked_members(archive, folder), tree)
        except BaseException:
            shutil.rmtree(tree, ignore_errors=True)
            raise
    except tarfile.TarError:
        raise PublishError("The renders are not a readable archive") from None
    finally:
        plain.unlink(missing_ok=True)
    pngs = sum(1 for _ in (tree / folder).rglob("*.png"))
    print(f"May iOS renders decrypted and checked: run folder {folder}/ with {pngs} images")


def decrypt(sealed, plain, identity, recipient, private):
    """age -d of sealed into plain (0600), after checking identity's public half is recipient."""
    key, env = private / "renders.key", child_env()
    try:
        write_private(key, (identity + "\n").encode())
        derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)],
                                 capture_output=True, stdin=subprocess.DEVNULL, env=env)
        if derived.returncode or derived.stdout.decode().strip() != recipient:
            raise PublishError("The renders decryption key does not match MAY_RENDERS_RECIPIENT")
        write_private(plain, b"")
        if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain), str(sealed)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          env=env).returncode:
            raise PublishError("The renders failed to decrypt or authenticate")
    finally:
        key.unlink(missing_ok=True)


# Run 41068959429 (N, 30 Oct): a push failed with nothing said, and its git output went with the job's private log. A
# failed git step now names its class in the public log, from git's own words, which never leave the private log:
# rejected (may-renders moved after the fetch), auth, network, or other. A rejected push is tried once more from
# may-renders' new head (PUSH_ATTEMPTS), never by force.
GIT_CAUSES = (("rejected", re.compile(rb"\[rejected\]|non-fast-forward|\(fetch first\)")),
              ("auth", re.compile(rb"Permission denied|publickey|Host key verification failed|Authentication failed|"
                                  rb"Repository not found", re.IGNORECASE)),
              ("network", re.compile(rb"Could not resolve host|timed out|Connection (?:reset|refused|closed)|"
                                     rb"Network is unreachable|hung up|early EOF|unable to access", re.IGNORECASE)))
PUSH_ATTEMPTS = 2


class GitFailed(PublishError):
    """A git step that failed, with its class (git_cause) alone."""

    def __init__(self, cause):
        super().__init__(f"may-renders could not be updated ({cause}); inspect protected runner diagnostics")
        self.cause = cause


def git_cause(output):
    """A failed git step's class from its output: the first GIT_CAUSES word whose pattern it holds, else "other"."""
    return next((word for word, pattern in GIT_CAUSES if pattern.search(output)), "other")


def git(argv, log, env, cwd, capture=False):
    """One git command with its output in the private log (or its stdout captured, its stderr logged)."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false", *argv]
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True)
    with log.open("ab") as output:
        output.write(result.stderr if capture else result.stdout + result.stderr)
    if result.returncode:
        raise GitFailed(git_cause(result.stdout + result.stderr))
    return result.stdout


def runs_after(existing, entry, keep=KEEP):
    """runs.json after adding entry first: (the list kept, the folders to remove). keep None keeps every folder
    (testflight_feedback.py: its issues link each one)."""
    runs = [run for run in existing if isinstance(run, dict) and isinstance(run.get("folder"), str) and
            RUN_FOLDER.fullmatch(run["folder"]) and run["folder"] != entry["folder"]]
    runs.insert(0, entry)
    if keep is None:
        return runs, []
    return runs[:keep], [run["folder"] for run in runs[keep:]]


def publish_tree(tree, prefix, folder, entry, message, key_text, run_git=git, extra=None, keep=KEEP):
    """Add tree as prefix + folder in may-renders' main, record entry first in prefix + runs.json, remove the run
    folders beyond the newest KEEP there, let extra(base) add to the commit, and push main (never by force; the first
    push creates it). Returns (kept, removed, extra's result)."""
    root = runner_root()
    private = root / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    key, known, log, work = private / "renders-write.key", private / "known_hosts", private / "publish.log", root / "may-publish-git"
    ssh = " ".join(shlex.quote(p) for p in (
        "ssh", "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", "UserKnownHostsFile=" + str(known)))
    env = child_env(GIT_SSH_COMMAND=ssh, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                    GIT_CONFIG_GLOBAL=os.devnull, GIT_LFS_SKIP_SMUDGE="1")
    try:
        write_private(key, (key_text + "\n").encode())
        write_private(known, (GITHUB_HOST_KEY + "\n").encode())
        for attempt in range(1, PUSH_ATTEMPTS + 1):
            try:
                return commit_tree(tree, prefix, folder, entry, message, run_git, extra, log, env, root, work, keep)
            except GitFailed as failed:
                if failed.cause != "rejected" or attempt == PUSH_ATTEMPTS:
                    raise
                print("May renders publish: may-renders moved during the push; trying once more from its new head")
    finally:
        key.unlink(missing_ok=True)
        known.unlink(missing_ok=True)


def commit_tree(tree, prefix, folder, entry, message, run_git, extra, log, env, root, work, keep=KEEP):
    """One attempt of publish_tree, in a fresh clone of may-renders' main at work: (kept, removed, extra's result)."""
    shutil.rmtree(work, ignore_errors=True)
    run_git(["init", "-q", str(work)], log, env, root)
    run_git(["remote", "add", "origin", REMOTE], log, env, work)
    heads = run_git(["ls-remote", "--heads", "origin", "refs/heads/" + BRANCH], log, env, work, capture=True)
    if heads.strip():
        run_git(["fetch", "--quiet", "--no-tags", "--depth", "1", "origin",
                 f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"], log, env, work)
        run_git(["checkout", "-q", "-B", "renders", "refs/remotes/origin/" + BRANCH], log, env, work)
    else:
        run_git(["checkout", "-q", "--orphan", "renders"], log, env, work)
    base = work / prefix if prefix else work
    base.mkdir(parents=True, exist_ok=True)
    index = base / "runs.json"
    try:
        existing = json.loads(index.read_text()) if index.is_file() and not index.is_symlink() else []
    except (OSError, ValueError):
        existing = []
    kept, removed = runs_after(existing if isinstance(existing, list) else [], entry, keep)
    for old in removed + [folder]:
        if (base / old).exists():
            run_git(["rm", "-r", "-q", "--ignore-unmatch", "--", prefix + old], log, env, work)
            shutil.rmtree(base / old, ignore_errors=True)
    shutil.copytree(tree, base / folder, symlinks=False)
    index.write_text(json.dumps(kept, indent=2) + "\n")
    added = extra(base) if extra else None
    (work / "README.md").write_text(README)
    run_git(["add", "-A", "--", "."], log, env, work)
    run_git([*AUTHOR, "commit", "-q", "-m", message], log, env, work)
    run_git(["push", "--quiet", "origin", "HEAD:refs/heads/" + BRANCH], log, env, work)
    landed(prefix, folder, run_git, log, env, work)
    return kept, removed, added


def landed(prefix, folder, run_git, log, env, work):
    """After a push git called a success: may-renders' main, fetched again, holds the run folder and lists it first in
    runs.json (N, 30 Oct: check it rather than trust a green step), else the publish fails."""
    try:
        run_git(["fetch", "--quiet", "--no-tags", "--depth", "1", "origin",
                 f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"], log, env, work)
        run_git(["cat-file", "-e", f"refs/remotes/origin/{BRANCH}:{prefix}{folder}"], log, env, work)
        listed = json.loads(run_git(["show", f"refs/remotes/origin/{BRANCH}:{prefix}runs.json"], log, env, work,
                                    capture=True))
    except (GitFailed, ValueError):
        listed = None
    if not (isinstance(listed, list) and any(isinstance(run, dict) and run.get("folder") == folder for run in listed)):
        raise PublishError("may-renders main does not hold the run folder after the push; check may-renders")


def write_key():
    """MAY_APP_RENDERS_WRITE_KEY, out of the environment: the key, or "" when it is not set."""
    key_text = os.environ.pop(WRITE_KEY, "").strip()
    if key_text and not SSH_KEY.fullmatch(key_text):
        raise PublishError("The may-renders write key is malformed (MAY_APP_RENDERS_WRITE_KEY); nothing was pushed")
    return key_text


def run_id():
    value = os.environ.get("GITHUB_RUN_ID", "")
    return value if re.fullmatch(r"[1-9][0-9]{0,19}", value) else None


def push(sha, pr=None, run_git=git):
    require_step_secrets((WRITE_KEY,))
    folder = folder_of(sha)
    key_text = write_key()
    tree = runner_root() / TREE / folder
    if not tree.is_dir():
        print("May iOS renders: nothing was decrypted, so nothing was pushed")
        return
    if not key_text:
        print("May iOS renders: MAY_APP_RENDERS_WRITE_KEY is not set in may-ios-renders; nothing was pushed")
        return
    number = pull_number(pr) if pr is not None else None
    entry = {"folder": folder, "source_sha": sha, "pull": number, "run": run_id()}
    kept, removed, _ = publish_tree(tree, "", folder, entry, f"Renders of may-app {sha}", key_text, run_git)
    print(f"May iOS renders published to may-renders as {folder}/ ({len(kept)} run folders kept, "
          f"{len(removed)} removed)")


def device_layout(name, run):
    """Whether a member name is part of a device report's fixed layout: "dir", "file" or None."""
    parts = name.split("/")
    if parts[0] != run or len(parts) > 3 or len(parts) > 1 and parts[1] not in DEVICES:
        return None
    if len(parts) < 3:
        return "dir"
    return "file" if DEVICE_FILE.fullmatch(parts[2]) else None


def agent_ok(agent):
    """A report row's sealed agent account (device_tests.sealed_agent): its fixed shape, the summary capped and with
    nothing kept_lines would drop or mask. It stays in report.json; the pages never show it."""
    summary = agent.get("summary") if isinstance(agent, dict) else None
    # A report sealed before hierarchy_failure (#116) or the driver's causes were has no such key: read as null, never
    # as malformed.
    agent = {"hierarchy_failure": None, "hierarchy_cause": None, "refusal_cause": None, **agent} \
        if isinstance(agent, dict) else agent
    return (isinstance(agent, dict) and
            set(agent) == {"outcome", "actions", "summary", "hierarchy_failure", "hierarchy_cause", "refusal_cause"} and
            agent["outcome"] in ("completed", "blocked", None) and type(agent["actions"]) is int and
            agent["hierarchy_failure"] in (*device_tests.device_mcp.DUMP_FAILURES, None) and
            all(agent[key] in (*device_tests.device_mcp.DRIVER_CAUSES, None)
                for key in ("hierarchy_cause", "refusal_cause")) and
            0 <= agent["actions"] <= device_tests.device_mcp.MOST_ACTIONS and isinstance(summary, str) and
            len(summary) <= device_tests.MAX_AGENT_SUMMARY and
            device_tests.kept_lines(summary.encode(), "") == (summary.encode(), 0))


def device_report(path, files, titles):
    """A device's report.json, every value checked against a fixed shape and the files actually extracted; titles come
    from this repository's own scenario list, never the report."""
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        raise PublishError("A device report is not JSON") from None
    rows = report.get("scenarios") if isinstance(report, dict) else None
    if not isinstance(rows, list) or report.get("device") not in DEVICES:
        raise PublishError("A device report is malformed")
    checked = []
    for row in rows:
        if not (isinstance(row, dict) and row.get("id") in titles and row.get("verdict") in VERDICT_WORDS and
                row.get("reason", REASONS[0]) in REASONS and
                all(isinstance(name, str) and name in files for name in row.get("videos", []) + row.get("screenshots", []))
                and all(isinstance(c, dict) and isinstance(c.get("get"), str) and re.fullmatch(r"/v1/[a-z0-9/_-]{1,120}", c["get"])
                        and type(c.get("status")) is int for c in row.get("server", [])) and
                (isinstance(row.get("notes", ""), str) and len(row.get("notes", "")) <= MAX_NOTES) and
                agent_ok(row.get("agent", {"outcome": None, "actions": 0, "summary": "", "hierarchy_failure": None})) and
                (isinstance(row.get("delivery", []), list) and len(row.get("delivery", [])) <= device_tests.MAX_DELIVERY and
                 all(isinstance(code, str) and DELIVERY.fullmatch(code) for code in row.get("delivery", [])))):
            raise PublishError("A device report row is malformed")
        checked.append({"id": row["id"], "title": titles[row["id"]], "verdict": row["verdict"],
                        "reason": row.get("reason"), "videos": row.get("videos", []),
                        "screenshots": row.get("screenshots", []),
                        "server": [(c["get"], c["status"]) for c in row.get("server", [])],
                        "seconds": row.get("seconds") if type(row.get("seconds")) is int else None,
                        "notes": row.get("notes", ""), "delivery": row.get("delivery", [])})
    return report["device"], checked


def recordings_note(url, attempt):
    """Where the recordings are: never in git; assets of this run's may-renders release, and the encrypted artifact."""
    return (f"Recordings are not committed: each links to an asset of this run's may-renders release (the newest {KEEP} "
            f"are kept). If a link is missing, the upload did not happen; the encrypted artifact "
            f"may-device-<device>-{attempt} of {url or 'the workflow run'} holds them for {VIDEO_DAYS} days: download it "
            "and decrypt with the renders key (age -d -i key.txt android.age | tar -x).")


def asset_name(device, video):
    return f"{device}-{video}"


def asset_url(run, device, video):
    return f"https://github.com/{RENDERS_REPO}/releases/download/device-tests-{run}/{asset_name(device, video)}"


def device_pages(run, reports, note):
    """The run folder's README.md (GitHub shows it when the folder is opened) and index.html, from checked reports."""
    order = [row["id"] for rows in reports.values() for row in rows]
    ids = list(dict.fromkeys(order))
    lines = [f"# May device tests: {run}", "", "Each scenario on each device: its verdict, the last screenshot and the "
             "link to each recording.", "", note, ""]
    html_parts = ["<!doctype html>", '<meta charset="utf-8">', f"<title>May device tests {run}</title>",
                  "<style>body{font:14px system-ui;margin:24px}img{max-width:360px}td{vertical-align:top;"
                  "padding:8px;border:1px solid #ccc}table{border-collapse:collapse}</style>",
                  f"<h1>May device tests: {run}</h1>", f"<p>{html.escape(note)}</p>", "<table>"]
    for scenario in ids:
        title = next(row["title"] for rows in reports.values() for row in rows if row["id"] == scenario)
        lines += [f"## {scenario}: {title}", ""]
        html_parts.append(f"<tr><th colspan=\"{len(reports)}\">{scenario}: {title}</th></tr><tr>")
        for device, rows in reports.items():
            row = next((r for r in rows if r["id"] == scenario), None)
            if row is None:
                continue
            note = f" ({row['reason']})" if row["reason"] else ""
            server = "; ".join(f"GET {path}: {status}" for path, status in row["server"])
            if row.get("delivery"):
                server = "; ".join(filter(None, (server, "Messages: " + board.delivery_words(row["delivery"]))))
            lines.append(f"**{device}: {row['verdict']}**{note}" + (f". Server: {server}" if server else ""))
            # The judge's note is model text: escaped, inside an HTML block, where GitHub reads no Markdown.
            judged = f"<blockquote>Judge: {html.escape(row['notes'])}</blockquote>" if row["notes"] else ""
            if judged:
                lines += ["", judged, ""]
            lines += [f'<img src="{device}/{shot}" width="300">' for shot in row["screenshots"]]
            lines += [f"[Recording: {video}]({asset_url(run, device, video)})" for video in row["videos"]]
            lines.append("")
            media = "".join(f'<img src="{device}/{shot}" alt="screenshot">' for shot in row["screenshots"]) + \
                "".join(f'<br><a href="{asset_url(run, device, video)}">Recording: {video}</a>' for video in row["videos"])
            html_parts.append(f"<td><b>{device}: {row['verdict']}</b>{note}<br>{server}<br>{judged}{media}</td>")
        html_parts.append("</tr>")
    html_parts.append("</table>")
    return "\n".join(lines) + "\n", "\n".join(html_parts) + "\n"


def scenario_titles():
    return {row["id"]: row["title"] for platform in DEVICES for row in
            device_tests.scenarios("all", platform) + device_tests.smoke_rows(platform)}


def open_device():
    """Each device's sealed report (may-device-download/<device>.age), decrypted, checked and extracted into one run
    folder, with README.md and index.html built here from the checked reports."""
    require_step_secrets((DECRYPT_KEY,))
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    root = runner_root()
    sealed = [root / DEVICE_DOWNLOAD / (device + ".age") for device in DEVICES]
    sealed = [path for path in sealed if path.is_file() and not path.is_symlink()]
    if not sealed:
        print("May device tests: no sealed report from this run attempt; nothing will be published")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise PublishError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-device-tests")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise PublishError("The renders decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-device-tests)")
    titles = scenario_titles()
    private, tree = root / "may-private", root / TREE
    private.mkdir(mode=0o700, exist_ok=True)
    tree.mkdir(mode=0o700)
    runs, reports = set(), {}
    try:
        for path in sealed:
            device = path.name[:-len(".age")]
            plain = private / (device + ".tar")
            try:
                decrypt(path, plain, lines[0], recipient, private)
                with tarfile.open(plain) as archive:
                    names = archive.getnames()
                    run = names[0].split("/")[0] if names else ""
                    if not DEVICE_RUN.fullmatch(run) or (runs and run not in runs) or \
                            any(n.split("/")[1:2] not in ([], [device]) for n in names):
                        raise PublishError("A device report is not one run folder for its own device")
                    runs.add(run)
                    members = [m for m in checked_members(archive, run, device_layout, "report.json")
                               if not (m.isdir() and m.name == run and (tree / run).exists())]
                    for name in extract(archive, members, tree):
                        # device-push reads this, so the log says the board went without the dropped file.
                        (private / BOARD).mkdir(mode=0o700, exist_ok=True)
                        (private / BOARD / f"{device}-{name}{DROPPED}").touch()
            except tarfile.TarError:
                raise PublishError("A device report is not a readable archive") from None
            finally:
                plain.unlink(missing_ok=True)
            folder = tree / run / device
            # The board's inputs leave the tree here: they are read by device-push, never committed.
            (private / BOARD).mkdir(mode=0o700, exist_ok=True)
            for name in BOARD_INPUTS:
                if (folder / name).is_file():
                    (folder / name).rename(private / BOARD / f"{device}-{name}")
            reports[device] = device_report(folder / "report.json", {p.name for p in folder.iterdir()}, titles)[1]
        run = runs.pop()
        # Checked above, then moved out of the tree: recordings and Android's diagnostics never enter may-renders
        # history; device-release uploads them from here, to a release pruned after KEEP runs.
        videos = private / VIDEOS / run
        for video in sorted([*(tree / run).glob("*/*.mp4"), *(tree / run).glob("*/*" + device_tests.DIAG)]):
            (videos / video.parent.name).mkdir(mode=0o700, parents=True, exist_ok=True)
            video.rename(videos / video.parent.name / video.name)
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        readme, page = device_pages(run, reports, recordings_note(
            run_url(os.environ), attempt if re.fullmatch(r"[1-9][0-9]{0,3}", attempt) else "<attempt>"))
        (tree / run / "README.md").write_text(readme)
        (tree / run / "index.html").write_text(page)
        write_private(private / BOARD / "reports.json", json.dumps(reports).encode())
    except BaseException:
        shutil.rmtree(tree, ignore_errors=True)
        raise
    devices = len(reports)
    scenarios = sum(len(rows) for rows in reports.values())
    print(f"May device tests decrypted and checked: run folder device-tests/{run}/ with {scenarios} scenario reports "
          f"from {devices} devices")


def push_device(run_git=git):
    require_step_secrets((WRITE_KEY,))
    key_text = write_key()
    root = runner_root()
    runs = [path for path in (root / TREE).iterdir() if path.is_dir()] if (root / TREE).is_dir() else []
    if len(runs) != 1 or not DEVICE_RUN.fullmatch(runs[0].name):
        print("May device tests: nothing was decrypted, so nothing was pushed")
        return
    if not key_text:
        print("May device tests: MAY_APP_RENDERS_WRITE_KEY is not set in may-device-tests; nothing was pushed")
        return
    folder = runs[0].name
    entry = {"folder": folder, "run": run_id()}
    inputs = root / "may-private" / BOARD
    # The reports device-open checked (the recordings have left the tree since, so they are not checked again).
    reports = board.read_json(inputs / "reports.json") or {}
    # The previews the device jobs made (checked by device-open): the board points at them; device-release uploads them.
    videos = root / "may-private" / VIDEOS / folder
    previews = {f"{device.name}/{video.name[:-len(device_tests.PREVIEW)]}"
                for device in (videos.iterdir() if videos.is_dir() else []) if device.name in DEVICES
                for video in device.iterdir() if video.name.endswith(device_tests.PREVIEW)}
    # Android's copy of may-app's files first, else iOS's: both jobs check out the same commit.
    found = {name: next((board.read_json(inputs / f"{device}-{name}") for device in DEVICES
                         if (inputs / f"{device}-{name}").is_file()), None) for name in BOARD_INPUTS}

    def build_board(base):
        # The board never costs the run its publishing: any error in it skips the board alone (the log says so).
        try:
            return board.update(base, folder, entry["run"], reports, found["flows.json"], found["board-notes.json"],
                                asset_url, KEEP, scenario_titles(), notes_dropped=notes_dropped, previews=previews)
        except Exception:
            return "error"

    notes_dropped = found["board-notes.json"] is None and \
        any((inputs / f"{device}-board-notes.json{DROPPED}").exists() for device in DEVICES)
    kept, removed, built = publish_tree(runs[0], DEVICE_PREFIX, folder, entry, f"Device tests {folder}", key_text,
                                        run_git, extra=build_board)
    print(f"May device tests published to may-renders as device-tests/{folder}/ ({len(kept)} run folders kept, "
          f"{len(removed)} removed)")
    if built == "written":
        print("May device tests: the flow board is device-tests/board/ in may-renders")
    elif built == "no notes":
        print("May device tests: the flow board is device-tests/board/ in may-renders, without notes (may-app's "
              "board-notes.json is malformed)")
    elif built == "no flows":
        print("May device tests: no flow board (may-app's flows.json is missing or malformed)")
    else:
        print("May device tests: no flow board (it could not be built; the run is published)")


def renders_api(method, host, path, token, payload=None, data=None):
    """One may-renders release call. Only RELEASE_CALLS are made; nothing from a reply is printed."""
    if not any(method == allowed and host == base and re.fullmatch(pattern, path)
               for allowed, base, pattern in RELEASE_CALLS):
        raise PublishError("Only may-renders' device-tests releases are reachable")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "Content-Type": "application/json" if data is None else
               "text/plain; charset=utf-8" if path.endswith(device_tests.DIAG) else "video/mp4"}
    body = data if data is not None else None if payload is None else json.dumps(payload).encode()
    call = urllib.request.Request(host + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=300) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, None


def release_for(run, token, api):
    """This run's release, created (or found, on a re-run) at may-renders' main: its id and its asset names."""
    tag = "device-tests-" + run
    status, release = api("POST", API, f"/repos/{RENDERS_REPO}/releases", token, {
        "tag_name": tag, "target_commitish": "main", "name": "Device tests " + run, "make_latest": "false",
        "body": f"Recordings of device-tests/{run}/, written by may-releases. Never edited by hand."})
    if status == 422:
        status, release = api("GET", API, f"/repos/{RENDERS_REPO}/releases/tags/{tag}", token)
    if status not in (200, 201) or not isinstance(release, dict) or type(release.get("id")) is not int:
        raise PublishError("The may-renders release for this run could not be created or read")
    status, assets = api("GET", API, f"/repos/{RENDERS_REPO}/releases/{release['id']}/assets?per_page=100", token)
    if status != 200 or not isinstance(assets, list):
        raise PublishError("The may-renders release's assets could not be read")
    return release["id"], {a.get("name") for a in assets if isinstance(a, dict)}


def prune_releases(token, api):
    """The newest KEEP device-tests releases (by tag: <UTC date>-<sha>, then creation) kept; the rest and their tags
    deleted. Other releases are never touched."""
    status, releases = api("GET", API, f"/repos/{RENDERS_REPO}/releases?per_page=100", token)
    if status != 200 or not isinstance(releases, list):
        raise PublishError("may-renders' releases could not be listed")
    ours = [r for r in releases if isinstance(r, dict) and type(r.get("id")) is int and
            isinstance(r.get("tag_name"), str) and RELEASE_TAG.fullmatch(r["tag_name"])]
    ours.sort(key=lambda r: (r["tag_name"][len("device-tests-"):][:8], str(r.get("created_at", ""))), reverse=True)
    removed = 0
    for release in ours[KEEP:]:
        status, _ = api("DELETE", API, f"/repos/{RENDERS_REPO}/releases/{release['id']}", token)
        if status not in (204, 404):
            raise PublishError("An old may-renders release could not be deleted")
        api("DELETE", API, f"/repos/{RENDERS_REPO}/git/refs/tags/{release['tag_name']}", token)
        removed += 1
    return min(len(ours), KEEP), removed


def release_device(api=renders_api):
    """The recordings device-open moved aside, uploaded as assets of this run's may-renders release."""
    require_step_secrets((RELEASE_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(RELEASE_TOKEN, "").strip()
    root = runner_root()
    folders = [p for p in (root / "may-private" / VIDEOS).iterdir() if p.is_dir()] \
        if (root / "may-private" / VIDEOS).is_dir() else []
    if len(folders) != 1 or not DEVICE_RUN.fullmatch(folders[0].name):
        print("May device tests: no recordings were decrypted, so none were uploaded")
        return
    if len(token) < 20:
        print("May device tests: MAY_RENDERS_RELEASE_TOKEN is not set in may-device-tests; recordings stay only in "
              "the encrypted artifact")
        return
    run = folders[0].name
    videos = [(device.name, video) for device in sorted(folders[0].iterdir())
              if device.name in DEVICES and device.is_dir() and not device.is_symlink()
              for video in sorted(device.iterdir())
              if DEVICE_FILE.fullmatch(video.name) and (video.suffix == ".mp4" or video.name.endswith(device_tests.DIAG))
              and video.is_file() and not video.is_symlink()]
    release, present = release_for(run, token, api)
    uploaded = previews = diagnostics = 0
    # The recordings first, each one required; then the board's previews and Android's diagnostics, each one optional
    # (a missing preview shows the end screen), so neither can ever cost the run its recordings.
    for device, video in sorted(videos, key=lambda pair: not pair[1].name.endswith(".mp4") or
                                pair[1].name.endswith(device_tests.PREVIEW)):
        name = asset_name(device, video.name)
        if name in present:
            continue
        if not video.name.endswith(".mp4") or video.name.endswith(device_tests.PREVIEW):
            # Optional: no error on one (a status, a reset connection, a short read) stops the rest or the pruning.
            try:
                status, _ = api("POST", UPLOADS, f"/repos/{RENDERS_REPO}/releases/{release}/assets?name={name}", token,
                                data=video.read_bytes())
            except (OSError, http.client.HTTPException, PublishError, DsCompileError):
                status = None
            if video.name.endswith(device_tests.PREVIEW):
                previews += status == 201
            else:
                diagnostics += status == 201
            continue
        status, _ = api("POST", UPLOADS, f"/repos/{RENDERS_REPO}/releases/{release}/assets?name={name}", token,
                        data=video.read_bytes())
        if status != 201:
            raise PublishError("A recording could not be uploaded to the may-renders release")
        uploaded += 1
    kept, removed = prune_releases(token, api)
    print(f"May device tests: {uploaded} recordings, {previews} previews and {diagnostics} diagnostics uploaded to the "
          f"may-renders release for device-tests/{run}/ ({kept} releases kept, {removed} removed)")


_PR = r"/repos/" + re.escape(APP_REPO) + r"/{}/[1-9][0-9]{{0,6}}"
ALLOWED_CALLS = (("GET", _PR.format("pulls")),
                 ("GET", _PR.format("issues") + r"/comments\?per_page=100&page=(?:[1-9]|10)"),
                 ("POST", _PR.format("issues") + r"/comments"))


def github(method, path, payload, token):
    """One may-app API call. Only ALLOWED_CALLS are made: the pull request read, a page of its comments, and its
    comment post. Nothing from the call is printed."""
    if not any(method == allowed and re.fullmatch(pattern, path) for allowed, pattern in ALLOWED_CALLS):
        raise PublishError("Only the may-app pull request and its comments are reachable")
    return request(method, path, payload, token)


def comment_body(sha, folder, images, url):
    base = f"https://github.com/{RENDERS_REPO}/tree/{BRANCH}/{folder}"
    # The README first: GitHub renders it, on a phone too, where it shows index.html as source (Samuel, 23 Oct).
    lines = [f"**iOS renders** for {sha}: {images} images (iPhone 16 Pro, light and dark).", "",
             f"- Contact sheet (Android | iOS): https://github.com/{RENDERS_REPO}/blob/{BRANCH}/{folder}/README.md",
             f"- Side by side, one PNG per screen and mode: {base}/sheet", f"- Run folder: {base}",
             f"- Build and test log: https://github.com/{RENDERS_REPO}/blob/{BRANCH}/{folder}/build.log"]
    if url:
        lines += ["", f"Run: {url}"]
    return "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER


def comment(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    token = os.environ.pop(COMMENT_TOKEN, "")
    folder = folder_of(sha)
    if pr is None:
        print("May iOS renders: not a pull request's renders; no comment posted")
        return
    number = pull_number(pr)
    tree = runner_root() / TREE / folder
    if not tree.is_dir():
        print("May iOS renders: nothing was published, so no comment posted")
        return
    if not token:
        print("May iOS renders: comment skipped: no token (MAY_APP_PR_COMMENT_TOKEN is not set in may-ios-renders)")
        return
    if len(token) < 20:
        raise PublishError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    body = comment_body(sha, folder, sum(1 for _ in tree.rglob("*.png")), run_url(os.environ))
    if len(body) > MAX_BODY or holds(body, token):
        raise PublishError("The comment is too long or holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise PublishError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise PublishError("The pull request is no longer open at the rendered head; nothing was posted") from None
    if post_once(call_api, APP_REPO, number, sha, body, token, MARKER, comment_author(os.environ)):
        print(f"May iOS renders linked on may-app #{number}")
    else:
        print(f"May iOS renders already linked for this head on may-app #{number}; nothing posted again")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("open", "push", "comment", "device-open", "device-push", "device-release"))
    parser.add_argument("--source-sha")
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    try:
        if args.command == "device-open":
            open_device()
        elif args.command == "device-push":
            push_device()
        elif args.command == "device-release":
            release_device()
        elif args.command == "open":
            open_renders(args.source_sha)
        elif args.command == "push":
            push(args.source_sha, args.pr_number or None)
        else:
            comment(args.source_sha, args.pr_number or None)
    except (PublishError, StoreError, GateDenial, DsCompileError) as error:
        raise SystemExit("May iOS renders publish stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May iOS renders publish failed; nothing private was printed") from None
