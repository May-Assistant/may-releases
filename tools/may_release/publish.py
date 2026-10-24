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

from ds_compile import API, DsCompileError, MAX_BODY, post_once, request, run_url
from gate import AGE_RECIPIENT, APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import GITHUB_HOST_KEY
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
RUN_FOLDER = re.compile(r"(?:[0-9]{8}-)?[0-9a-f]{12}")
DEVICE_RUN = re.compile(r"[0-9]{8}-[0-9a-f]{12}")
DEVICES = ("android", "ios")
DEVICE_FILE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}(?:-end\.png|\.mp4|-part[0-9]{1,2}\.mp4|\.xml)|report\.json")
DEVICE_DOWNLOAD, DEVICE_PREFIX = "may-device-download", "device-tests/"
VERDICT_WORDS = ("pass", "ui-pass", "fail", "skipped", device_tests.SMOKE_PASS)
REASONS = ("outward actions are off (MAY_DEVICE_TESTS_OUTWARD)", "may-app has no Maestro flow for this scenario",
           device_tests.UNCONFIGURED, device_tests.RATE_LIMITED, device_tests.UNFINISHED, device_tests.BLOCKED,
           device_tests.NO_TRUTH, device_tests.NOT_JUDGED, device_tests.SWITCHED_OFF, device_tests.SMOKE_MAESTRO,
           device_tests.BUDGET)
MAX_NOTES = 600
# The sealed device artifact's retention (device-tests.yml): the recordings' fallback when the release upload fails.
VIDEO_DAYS = 7
# The recordings go to one may-renders release per run (tag device-tests-<run>), never into its git history; the
# newest KEEP such releases are kept. Only device-release holds the token, a fine-grained token on may-renders alone.
RELEASE_TOKEN, RENDERS_REPO = "MAY_RENDERS_RELEASE_TOKEN", "May-Assistant/may-renders"
UPLOADS = "https://uploads.github.com"
VIDEOS = "may-device-videos"
RELEASE_TAG = re.compile(r"device-tests-[0-9]{8}-[0-9a-f]{12}")
_RELEASES = r"/repos/" + re.escape(RENDERS_REPO) + r"/releases"
RELEASE_CALLS = (("POST", API, _RELEASES), ("GET", API, _RELEASES + r"/tags/" + RELEASE_TAG.pattern),
                 ("GET", API, _RELEASES + r"\?per_page=100"),
                 ("GET", API, _RELEASES + r"/[1-9][0-9]{0,15}/assets\?per_page=100"),
                 ("POST", UPLOADS, _RELEASES + r"/[1-9][0-9]{0,15}/assets\?name=(?:android|ios)-[a-z0-9][a-z0-9-]{0,60}\.mp4"),
                 ("DELETE", API, _RELEASES + r"/[1-9][0-9]{0,15}"),
                 ("DELETE", API, r"/repos/" + re.escape(RENDERS_REPO) + r"/git/refs/tags/" + RELEASE_TAG.pattern))
MAX_FILES = 2000
MAX_FILE = 40 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
DOWNLOAD, TREE = "may-renders-download/renders.age", "may-publish-tree"
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
        if path.suffix == ".mp4" and data[4:8] != b"ftyp":
            raise PublishError("A recording is not an MP4")
        if path.name in ("screens.json", "report.json"):
            try:
                json.loads(data)
            except ValueError:
                raise PublishError("The manifest copy is not JSON") from None
        if path.name in ("index.html", "README.md") and re.search(rb"(?i)<\s*(script|iframe|object|embed)|javascript:", data):
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
            RUN_FOLDER.fullmatch(run["folder"]) and run["folder"] != entry["folder"]]
    runs.insert(0, entry)
    return runs[:KEEP], [run["folder"] for run in runs[KEEP:]]


def publish_tree(tree, prefix, folder, entry, message, key_text, run_git=git):
    """Add tree as prefix + folder in may-renders' main, record entry first in prefix + runs.json, remove the run
    folders beyond the newest KEEP there, and push main (never by force; the first push creates it). Returns
    (kept, removed)."""
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
        kept, removed = runs_after(existing if isinstance(existing, list) else [], entry)
        for old in removed + [folder]:
            if (base / old).exists():
                run_git(["rm", "-r", "-q", "--ignore-unmatch", "--", prefix + old], log, env, work)
                shutil.rmtree(base / old, ignore_errors=True)
        shutil.copytree(tree, base / folder, symlinks=False)
        index.write_text(json.dumps(kept, indent=2) + "\n")
        (work / "README.md").write_text(README)
        run_git(["add", "-A", "--", "."], log, env, work)
        run_git([*AUTHOR, "commit", "-q", "-m", message], log, env, work)
        run_git(["push", "--quiet", "origin", "HEAD:refs/heads/" + BRANCH], log, env, work)
    finally:
        key.unlink(missing_ok=True)
        known.unlink(missing_ok=True)
    return kept, removed


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
    kept, removed = publish_tree(tree, "", folder, entry, f"Renders of may-app {sha}", key_text, run_git)
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


def device_report(path, files, titles):
    """A device's report.json, every value checked against a fixed shape and the files actually extracted; titles come
    from this repository's own scenario list, never the report."""
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
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
                (isinstance(row.get("notes", ""), str) and len(row.get("notes", "")) <= MAX_NOTES)):
            raise PublishError("A device report row is malformed")
        checked.append({"id": row["id"], "title": titles[row["id"]], "verdict": row["verdict"],
                        "reason": row.get("reason"), "videos": row.get("videos", []),
                        "screenshots": row.get("screenshots", []),
                        "server": [(c["get"], c["status"]) for c in row.get("server", [])],
                        "seconds": row.get("seconds") if type(row.get("seconds")) is int else None,
                        "notes": row.get("notes", "")})
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
    titles = {row["id"]: row["title"] for platform in DEVICES for row in
              device_tests.scenarios("all", platform) + device_tests.smoke_rows(platform)}
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
                    extract(archive, members, tree)
            except tarfile.TarError:
                raise PublishError("A device report is not a readable archive") from None
            finally:
                plain.unlink(missing_ok=True)
            folder = tree / run / device
            reports[device] = device_report(folder / "report.json", {p.name for p in folder.iterdir()}, titles)[1]
        run = runs.pop()
        # Checked as MP4s above, then moved out of the tree: recordings never enter may-renders history; device-release
        # uploads them from here.
        videos = private / VIDEOS / run
        for video in sorted((tree / run).glob("*/*.mp4")):
            (videos / video.parent.name).mkdir(mode=0o700, parents=True, exist_ok=True)
            video.rename(videos / video.parent.name / video.name)
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        readme, page = device_pages(run, reports, recordings_note(
            run_url(os.environ), attempt if re.fullmatch(r"[1-9][0-9]{0,3}", attempt) else "<attempt>"))
        (tree / run / "README.md").write_text(readme)
        (tree / run / "index.html").write_text(page)
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
    kept, removed = publish_tree(runs[0], DEVICE_PREFIX, folder, entry, f"Device tests {folder}", key_text, run_git)
    print(f"May device tests published to may-renders as device-tests/{folder}/ ({len(kept)} run folders kept, "
          f"{len(removed)} removed)")


def renders_api(method, host, path, token, payload=None, data=None):
    """One may-renders release call. Only RELEASE_CALLS are made; nothing from a reply is printed."""
    if not any(method == allowed and host == base and re.fullmatch(pattern, path)
               for allowed, base, pattern in RELEASE_CALLS):
        raise PublishError("Only may-renders' device-tests releases are reachable")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "Content-Type": "video/mp4" if data is not None else "application/json"}
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
              if DEVICE_FILE.fullmatch(video.name) and video.suffix == ".mp4" and video.is_file()
              and not video.is_symlink()]
    release, present = release_for(run, token, api)
    uploaded = 0
    for device, video in videos:
        name = asset_name(device, video.name)
        if name in present:
            continue
        status, _ = api("POST", UPLOADS, f"/repos/{RENDERS_REPO}/releases/{release}/assets?name={name}", token,
                        data=video.read_bytes())
        if status != 201:
            raise PublishError("A recording could not be uploaded to the may-renders release")
        uploaded += 1
    kept, removed = prune_releases(token, api)
    print(f"May device tests: {uploaded} recordings uploaded to the may-renders release for device-tests/{run}/ "
          f"({kept} releases kept, {removed} removed)")


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
    if post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
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
