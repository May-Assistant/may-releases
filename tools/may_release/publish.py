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
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ds_compile import DsCompileError, MAX_BODY, post_once, request, run_url
from gate import AGE_RECIPIENT, APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import GITHUB_HOST_KEY
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
MAX_FILES = 2000
MAX_FILE = 40 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
DOWNLOAD, TREE = "may-renders-download/renders.age", "may-publish-tree"
MARKER = "may-ios-renders"
AUTHOR = ("-c", "user.name=May renders", "-c", "user.email=renders@may-releases.invalid")
README = ("# May parity renders\n\nWritten by may-releases' ios-renders.yml, never by hand: one folder per may-app "
          "commit (its first 12 hex digits), the newest " + str(KEEP) + " kept (runs.json lists them, newest first). "
          "Each holds index.html (Android | iOS per screen and mode), sheet/ (the two side by side), ios/, android/, "
          "the manifest copy and build.log.\n")


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
        return "dir" if parts[1] in KINDS else "file" if parts[1] in ("index.html", "screens.json", "build.log") else None
    return "file" if parts[1] in KINDS and FILE.fullmatch(parts[2]) else None


def checked_members(archive, folder):
    members, total, seen = archive.getmembers(), 0, set()
    if not members or len(members) > MAX_FILES:
        raise PublishError("The renders hold no run folder or too many files")
    for member in members:
        name = member.name
        kind = allowed(name, folder)
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
    if total > MAX_TOTAL or folder + "/index.html" not in seen:
        raise PublishError("The renders are too large or have no contact sheet")
    # Parents before children, as renders.py seal writes them.
    for name in seen:
        if "/" in name and name.rsplit("/", 1)[0] not in seen:
            raise PublishError("The renders hold a path outside the run folder's layout")
    return members


def extract(archive, members, destination):
    for member in sorted(members, key=lambda m: m.name.count("/")):
        path = destination.joinpath(*member.name.split("/"))
        if member.isdir():
            path.mkdir(mode=0o700)
            continue
        data = archive.extractfile(member).read()
        # "x": created here, never an existing file or a link to one.
        with path.open("xb") as output:
            output.write(data)
        if path.suffix == ".png" and not data.startswith(PNG):
            raise PublishError("A render is not a PNG")
        if path.name == "screens.json":
            try:
                json.loads(data)
            except ValueError:
                raise PublishError("The manifest copy is not JSON") from None
        if path.name == "index.html" and re.search(rb"(?i)<\s*(script|iframe|object|embed)|javascript:", data):
            raise PublishError("The contact sheet holds active content")


def open_renders(sha):
    require_step_secrets((DECRYPT_KEY,))
    folder = folder_of(sha)
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    root = runner_root()
    sealed = root / DOWNLOAD
    if not sealed.is_file() or sealed.is_symlink():
        print("May iOS renders: no sealed renders from this run attempt; nothing will be published")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise PublishError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-ios-renders")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise PublishError("The renders decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-ios-renders)")
    private = root / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    key, plain, tree = private / "renders.key", private / "renders.tar", root / TREE
    env = child_env()
    try:
        try:
            write_private(key, (lines[0] + "\n").encode())
            derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)],
                                     capture_output=True, stdin=subprocess.DEVNULL, env=env)
            if derived.returncode or derived.stdout.decode().strip() != recipient:
                raise PublishError("The renders decryption key does not match MAY_RENDERS_RECIPIENT")
            write_private(plain, b"")
            if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain),
                               str(sealed)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, env=env).returncode:
                raise PublishError("The renders failed to decrypt or authenticate")
        finally:
            key.unlink(missing_ok=True)
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


def git(argv, log, env, cwd, capture=False):
    """One git command with its output in the private log (or its stdout captured, its stderr logged)."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false", *argv]
    if capture:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True)
        with log.open("ab") as output:
            output.write(result.stderr)
    else:
        with log.open("ab") as output:
            result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
    if result.returncode:
        raise PublishError("may-renders could not be updated; inspect protected runner diagnostics")
    return result.stdout


def runs_after(existing, entry):
    """runs.json after adding entry first: (the list kept, the folders to remove)."""
    runs = [run for run in existing if isinstance(run, dict) and isinstance(run.get("folder"), str) and
            re.fullmatch(r"[0-9a-f]{12}", run["folder"]) and run["folder"] != entry["folder"]]
    runs.insert(0, entry)
    return runs[:KEEP], [run["folder"] for run in runs[KEEP:]]


def push(sha, pr=None, run_git=git):
    require_step_secrets((WRITE_KEY,))
    folder = folder_of(sha)
    key_text = os.environ.pop(WRITE_KEY, "").strip()
    root = runner_root()
    tree = root / TREE / folder
    if not tree.is_dir():
        print("May iOS renders: nothing was decrypted, so nothing was pushed")
        return
    if not key_text:
        print("May iOS renders: MAY_APP_RENDERS_WRITE_KEY is not set in may-ios-renders; nothing was pushed")
        return
    if not SSH_KEY.fullmatch(key_text):
        raise PublishError("The may-renders write key is malformed (MAY_APP_RENDERS_WRITE_KEY); nothing was pushed")
    number = pull_number(pr) if pr is not None else None
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
        run_git(["init", "-q", str(work)], log, env, root)
        run_git(["remote", "add", "origin", REMOTE], log, env, work)
        heads = run_git(["ls-remote", "--heads", "origin", "refs/heads/" + BRANCH], log, env, work, capture=True)
        if heads.strip():
            run_git(["fetch", "--quiet", "--no-tags", "--depth", "1", "origin",
                     f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"], log, env, work)
            run_git(["checkout", "-q", "-B", "renders", "refs/remotes/origin/" + BRANCH], log, env, work)
        else:
            run_git(["checkout", "-q", "--orphan", "renders"], log, env, work)
        index = work / "runs.json"
        try:
            existing = json.loads(index.read_text()) if index.is_file() and not index.is_symlink() else []
        except (OSError, ValueError):
            existing = []
        entry = {"folder": folder, "source_sha": sha, "pull": number,
                 "run": os.environ.get("GITHUB_RUN_ID") if re.fullmatch(r"[1-9][0-9]{0,19}",
                                                                         os.environ.get("GITHUB_RUN_ID", "")) else None}
        kept, removed = runs_after(existing if isinstance(existing, list) else [], entry)
        for old in removed + [folder]:
            if (work / old).exists():
                run_git(["rm", "-r", "-q", "--ignore-unmatch", "--", old], log, env, work)
                shutil.rmtree(work / old, ignore_errors=True)
        shutil.copytree(tree, work / folder, symlinks=False)
        index.write_text(json.dumps(kept, indent=2) + "\n")
        (work / "README.md").write_text(README)
        run_git(["add", "-A", "--", "."], log, env, work)
        run_git([*AUTHOR, "commit", "-q", "-m", f"Renders of may-app {sha}"], log, env, work)
        run_git(["push", "--quiet", "origin", "HEAD:refs/heads/" + BRANCH], log, env, work)
    finally:
        key.unlink(missing_ok=True)
        known.unlink(missing_ok=True)
    print(f"May iOS renders published to may-renders as {folder}/ ({len(kept)} run folders kept, "
          f"{len(removed)} removed)")


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
    lines = [f"**iOS renders** for {sha}: {images} images (iPhone 16 Pro, light and dark).", "",
             f"- Run folder: {base}", f"- Contact sheet (Android | iOS): "
             f"https://github.com/{RENDERS_REPO}/blob/{BRANCH}/{folder}/index.html",
             f"- Side by side, one PNG per screen and mode: {base}/sheet",
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
    if post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May iOS renders linked on may-app #{number}")
    else:
        print(f"May iOS renders already linked for this head on may-app #{number}; nothing posted again")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("open", "push", "comment"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    try:
        if args.command == "open":
            open_renders(args.source_sha)
        elif args.command == "push":
            push(args.source_sha, args.pr_number or None)
        else:
            comment(args.source_sha, args.pr_number or None)
    except (PublishError, StoreError, GateDenial, DsCompileError) as error:
        raise SystemExit("May iOS renders publish stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May iOS renders publish failed; nothing private was printed") from None
