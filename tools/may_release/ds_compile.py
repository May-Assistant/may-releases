"""Swift compile of a may-design-system pull request head, with the errors posted on that pull request only
(ds-swift-compile.yml).

may-app's iOS app can fail to compile against a may-design-system commit while the app compile's log stays private,
so the lane cannot see why. This compiles the design system's Swift package alone and posts its compile errors as
one comment on the may-design-system pull request (Samuel's decision). This repository is public, and so are its
logs, step summaries and artifacts; may-design-system is private. So nothing derived from the design system (a
path, a file name, a compiler message, a scheme name, a log line) is printed, summarised or uploaded here: every
line this script prints is fixed, apart from the dispatch's own inputs (commit and pull request number) and the
number of errors. Every error is printed as a fixed literal (static_check.py), any other failure as one fixed line.

Steps, one per workflow step, each holding only what it needs:
- fetch (MAY_DS_READ_KEY alone): the exact commit, with prepare.fetch (0600 key and pinned known_hosts, both
  removed in a finally), from the pull request's head ref, which must be exactly that commit. gate.py --check-pull
  has already read the pull request (open, in may-design-system itself, against claude/may-integration or main).
- compile (no secret in its environment or on disk): the pinned Xcode 26.3 builds the package for the iOS Simulator
  without signing. Every tool writes to a private log; the outcome is recorded in a private file. A failed build
  (a timeout or a tool that will not start included) does not fail this step: verdict fails the job after the seal.
- seal (the same job, no secret): the private log is reduced to file-located errors inside the checkout,
  repo-relative (diagnostics), into a record ({sha, passed, errors, total}) padded to MAX_RECORD and age-encrypted to
  MAY_RENDERS_RECIPIENT; only that sealed file leaves the job (one day). The design system's code ran in this job, so
  it holds no token (coordinator, 30 Oct: the split native-compile-pr.yml and ds-tests-pr.yml already have).
- verdict (the same job): fails it when the compile failed, whether or not the post job can post.
- open (a fresh post job, MAY_RENDERS_DECRYPT_KEY alone): decrypts the record after checking the key against the
  recipient, and checks it whole and for this commit.
- post (that job, MAY_DS_PR_COMMENT_TOKEN alone): posts the record as one comment on the pull request (comment_body),
  or, with no record, the native job's pass or fail alone (MAY_NATIVE_RESULT), after the pull request is read again
  and is still open at that head, and unless a comment already carries this head's marker. Only the pull request's
  comments endpoint is ever written.
"""

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from compile import credentials_left
from gate import AGE_RECIPIENT, CONTROL_REPO, DS_REPO, GateDenial, pull_number, validate_ds_pull_request
from prepare import DS, PrepareError, fetch as fetch_private, head_revision
from review_mcp import FOOTER, TOKEN_SHAPED, holds
from store import StoreError, child_env, require_step_secrets, write_private
import toolchain

READ_KEY = "MAY_DS_READ_KEY"
COMMENT_TOKEN = "MAY_DS_PR_COMMENT_TOKEN"
SOURCE, PRIVATE, DERIVED, PACKAGES = "may-ds-source", "may-ds-private", "may-ds-derived", "may-ds-packages"
DEVELOPER = Path("/Applications/Xcode_26.3.app/Contents/Developer")
XCODE = "26.3"
DESTINATION = "generic/platform=iOS Simulator"
BUILD_SECONDS = 45 * 60
MAX_SCHEMES = 20
# At most this many errors are posted; the rest are counted.
MAX_ERRORS = 100
# The sealed record between the native and post jobs (the same scheme as compile_pr_errors.py's): every record is padded
# to MAX_RECORD bytes, so its size says nothing about how many errors it holds.
RECIPIENT, DECRYPT_KEY = "MAY_RENDERS_RECIPIENT", "MAY_RENDERS_DECRYPT_KEY"
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")
SEALED, DOWNLOAD, SEALED_NAME, RECORD = "may-ds-compile-sealed", "may-ds-compile-download", "compile.age", "record.json"
MAX_RECORD, MAX_ERROR_LINE, MAX_TOTAL = 256 * 1024, 600, 1_000_000
NATIVE_RESULT, RESULTS = "MAY_NATIVE_RESULT", {"success": True, "failure": False}
MAX_PATH, MAX_MESSAGE = 240, 240
MAX_BODY = 65_000
API = "https://api.github.com"
SHA = re.compile(r"[0-9a-f]{40}")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# `<absolute path>:<line>:<column>: error: <message>`, as swift-frontend and clang print it, from the start of the
# line. The path is the shortest prefix followed by the location, so a path holding ':' still parses. A line holding
# any other control character (a tab in the message aside) is dropped.
ERROR_LINE = re.compile(r"(/[^\x00-\x1f\x7f]*?):([0-9]{1,7}):([0-9]{1,5}): (?:fatal )?error: ([^\x00-\x08\x0a-\x1f\x7f]+)")
MARKER = "may-ds-swift-compile"


class DsCompileError(ValueError):
    pass


# ---- Pure functions: what may leave the runner, and only for the private pull request ----

def relative(path, roots):
    """path relative to the first root it lies inside, or None (outside every root, or escaping by '..')."""
    norm = posixpath.normpath(path)
    for root in roots:
        top = posixpath.normpath(str(root))
        if top != "/" and norm.startswith(top + "/"):
            rel = norm[len(top) + 1:]
            if rel and rel != ".." and not rel.startswith("../") and not rel.startswith("/"):
                return rel
    return None


def diagnostics(log_text, roots, limit=MAX_ERRORS, exclude=()):
    """(errors, total): the unique compile errors located in a file inside the checkout (roots: its path as the build
    saw it, and its resolved path), each `relative/path:line:col: error: message`, in log order, at most limit of
    them; total counts every unique one. Warnings, notes, remarks, source and caret lines, errors in files outside
    the checkout (derived data, the SDK, other packages), errors in a checkout folder named in exclude (repo-relative
    prefixes ending in '/') and every other line are dropped. Shared with compile_pr_errors.py."""
    seen, ordered = set(), []
    for raw in log_text.split("\n"):
        line = ANSI.sub("", raw.rstrip("\r"))
        match = ERROR_LINE.fullmatch(line)
        if not match:
            continue
        path, row, column, message = match.groups()
        rel = relative(path, roots)
        message = " ".join(message.split())
        if rel is None or len(rel) > MAX_PATH or not message or rel.startswith(tuple(exclude)):
            continue
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE - 1] + "…"
        entry = TOKEN_SHAPED.sub("[redacted]", f"{rel}:{int(row)}:{int(column)}: error: {message}")
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered[:limit], len(ordered)


def fence_for(lines):
    """A backtick fence longer than any backtick run in lines, so nothing in them can close the block."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


def comment_body(sha, run_url, passed, errors, total):
    """The one comment posted on the pull request. sha is the compiled head; run_url this run, or None."""
    if not SHA.fullmatch(sha or ""):
        raise DsCompileError("The compiled commit is not an exact SHA")
    if passed:
        lines = ["### may-design-system Swift compile: passed", "",
                 f"`{sha}` compiles with Xcode {XCODE} (`xcodebuild`, {DESTINATION}, Release, no signing)."]
    else:
        lines = ["### may-design-system Swift compile: failed", "",
                 f"`{sha}` does not compile with Xcode {XCODE} (`xcodebuild`, {DESTINATION}, Release, no signing)."]
        if total:
            shown = f" Showing the first {len(errors)}; {total - len(errors)} more not shown." if total > len(errors) else ""
            fence = fence_for(errors)
            lines += ["", f"**{total} error{'s' if total != 1 else ''}** in files of this repository (warnings, notes, "
                      f"source lines and errors outside the checkout are left out).{shown}", "", fence + "text", *errors, fence]
        else:
            lines += ["", "The build failed, but its output holds no error located in a file of this repository (for "
                      "example a package resolution, scheme or link failure). Nothing else from the log is posted."]
    if run_url:
        lines += ["", f"Run: {run_url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        raise DsCompileError("The comment would be too long to post")
    return body


def run_url(environ):
    run = environ.get("GITHUB_RUN_ID", "")
    return f"https://github.com/{CONTROL_REPO}/actions/runs/{run}" if re.fullmatch(r"[1-9][0-9]{0,19}", run) else None


# ---- Runner paths ----

def runner_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise DsCompileError("Runner temporary directory is unavailable")
    return Path(raw).resolve()


def exact(sha):
    if not SHA.fullmatch(sha or ""):
        raise DsCompileError("Design-system SHA is not an exact lowercase commit SHA")
    return sha


# ---- fetch ----

def fetch(sha, pr):
    """The exact commit, which must be the pull request's head ref, with the read key; nothing of it is printed."""
    require_step_secrets((READ_KEY,))
    exact(sha)
    number = pull_number(pr)
    root = runner_root()
    private = root / PRIVATE
    private.mkdir(mode=0o700)
    checkout = root / SOURCE
    fetch_private(checkout, DS, sha, os.environ.get(READ_KEY, ""), private, "ds", pull=str(number))
    if credentials_left(private):
        raise DsCompileError("A read key or host file is still on disk after the fetch")
    if head_revision(checkout) != sha:
        raise DsCompileError("The checkout is not the exact design-system commit")
    print("Exact may-design-system commit fetched quietly; the read key and host file are removed")


# ---- compile ----

def build_env(root):
    """Only what the toolchain needs: no secret, no GitHub Actions file (step summary, output, env) or token."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(DEVELOPER_DIR=str(DEVELOPER), TMPDIR=str(root / PRIVATE / "tmp"), NSUnbufferedIO="YES")
    return env


def schemes_of(listing):
    """The schemes to build from `xcodebuild -list -json` in the package: the package-wide `*-Package` scheme when
    Xcode makes one (several products), otherwise every scheme. Their names are never printed."""
    try:
        data = json.loads(listing)
    except ValueError:
        return []
    found = ((data.get("workspace") or data.get("project") or {}).get("schemes") if isinstance(data, dict) else None)
    if not isinstance(found, list):
        return []
    names = [s for s in found if isinstance(s, str) and s and not s.startswith("-")]
    package = [s for s in names if s.endswith("-Package")]
    return (package or names)[:MAX_SCHEMES]


def compile_ds(run=subprocess.run, developer=DEVELOPER):
    # The compile runs the design system's own build code (SwiftPM, macros, plugins) and holds no secret at all:
    # not in its environment, not on disk.
    require_step_secrets(())
    root = runner_root()
    checkout, private = root / SOURCE, root / PRIVATE
    if not checkout.is_dir() or not private.is_dir():
        raise DsCompileError("The exact design-system commit is not fetched")
    if credentials_left(private):
        raise DsCompileError("A read key or host file is still on disk; nothing was compiled")
    stats = os.statvfs(root)
    if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
        raise DsCompileError("Protected runner has insufficient free space")
    if not developer.is_dir():
        raise DsCompileError("Pinned Xcode 26.3 is unavailable")
    (private / "tmp").mkdir(mode=0o700, exist_ok=True)
    env = build_env(root)
    version = run(["xcodebuild", "-version"], capture_output=True, stdin=subprocess.DEVNULL, env=env)
    if version.returncode or version.stdout.splitlines()[:1] != [b"Xcode " + XCODE.encode()]:
        raise DsCompileError("Pinned Xcode version differs")
    log, listing = private / "compile.log", private / "schemes.json"
    # From here every failure of the build itself (a non-zero exit, a timeout, a tool that will not start) is recorded
    # as a failed compile, so the post step still tells the pull request the build failed.
    schemes, codes = [], []
    try:
        with log.open("ab") as output, listing.open("wb") as listed:
            done = run(["xcodebuild", "-list", "-json"], cwd=checkout, env=env, stdout=listed, stderr=output,
                       stdin=subprocess.DEVNULL, timeout=600)
        schemes = schemes_of(listing.read_text(errors="replace")) if not done.returncode else []
    except (subprocess.SubprocessError, OSError, ValueError):
        schemes = []
    for scheme in schemes:
        try:
            with log.open("ab") as output:
                done = run(["xcodebuild", "-scheme", scheme, "-configuration", "Release", "-destination", DESTINATION,
                            "-derivedDataPath", str(root / DERIVED), "-clonedSourcePackagesDirPath", str(root / PACKAGES),
                            "-skipMacroValidation", "-skipPackagePluginValidation", "CODE_SIGNING_ALLOWED=NO", "build"],
                           cwd=checkout, env=env, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=BUILD_SECONDS)
            codes.append(done.returncode if isinstance(done.returncode, int) else -1)
        except (subprocess.SubprocessError, OSError, ValueError):
            codes.append(-1)
    passed = bool(schemes) and not any(codes)
    result = {"result": "passed" if passed else "failed", "schemes": len(schemes), "codes": codes}
    (private / "result.json").write_text(json.dumps(result))
    print("DS Swift compile finished; its output and outcome stay in this runner's private files")


# ---- post ----

_PR = r"/repos/" + re.escape(DS_REPO) + r"/{}/[1-9][0-9]{{0,6}}"
ALLOWED_CALLS = (("GET", _PR.format("pulls")),
                 ("GET", _PR.format("issues") + r"/comments\?per_page=100&page=(?:[1-9]|10)"),
                 ("POST", _PR.format("issues") + r"/comments"))
# Pages of existing comments read for the marker before posting (100 each).
MAX_COMMENT_PAGES = 10
# The comment mint's app-slug, in each post step (static_check pins it): its bot's own comments alone count as this
# head's earlier post (review of may-releases #137).
APP_SLUG = "MAY_CI_APP_SLUG"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")


def post_or_update(call_api, repo, number, sha, body, token, name=MARKER, author=None):
    """Post body on the pull request, or, when a comment of author's already carries this head's marker, edit that
    comment in place: "posted", "updated", or "same" when it already reads body. The newest result for a head wins: a
    re-run's pass replaces an earlier run's failure, and a detailed comment replaces a result-only one (review of
    may-releases #137). Only author's own comments count (a bot login, "<app slug>[bot]"): a person's or another app's
    comment quoting the marker is never edited or taken as posted. Without author (the PAT fallback, whose user is not
    known here) nothing is edited and body is posted anew. A marked comment GitHub will not let the token edit is left,
    and body is posted anew."""
    found = None
    for page in range(1, MAX_COMMENT_PAGES + 1) if author else ():
        status, comments = call_api("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}", None, token)
        if status != 200 or not isinstance(comments, list):
            raise DsCompileError("The pull request's comments could not be read; nothing was posted")
        marked = [c for c in comments if already_posted([c], sha, name, author) and type(c.get("id")) is int and
                  c["id"] > 0]
        found = marked[-1] if marked else found
        if len(comments) < 100:
            break
    if found is not None and found.get("body") == body:
        return "same"
    if found is not None:
        status, _ = call_api("PATCH", f"/repos/{repo}/issues/comments/{found['id']}", {"body": body}, token)
        if status == 200:
            return "updated"
    status, _ = call_api("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, token)
    if status != 201:
        raise DsCompileError("GitHub did not accept the comment; nothing from the compile is shown here")
    return "posted"


def already_posted(comments, sha, name=MARKER, author=None):
    """Whether a comment in one page of the pull request's comments already carries this head's marker; with author, a
    comment by that bot login alone counts."""
    marker = f"<!-- {name} head={sha} -->"
    return any(isinstance(c, dict) and isinstance(c.get("body"), str) and marker in c["body"] and
               (author is None or isinstance(c.get("user"), dict) and c["user"].get("type") == "Bot" and
                c["user"].get("login") == author) for c in comments)


def github(method, path, payload, token):
    """One may-design-system API call: (status, parsed JSON or None). Only ALLOWED_CALLS are made: the pull request
    read, a page of its comments, and its comment post. Nothing from the call is printed."""
    if not any(method == allowed and re.fullmatch(pattern, path) for allowed, pattern in ALLOWED_CALLS):
        raise DsCompileError("Only the design-system pull request and its comments are reachable")
    return request(method, path, payload, token)


# Run 40468630357 (coordinator, 27 Oct): the post's read of the may-app pull request failed once, and two failing tests
# were never posted; a review run that started at the same moment failed too. A read (GET alone, which changes nothing)
# that gets no answer, a rate limit or a server error is tried again after each of RETRY_SECONDS; a post or an edit is
# never repeated.
RETRY_STATUSES, RETRY_SECONDS = frozenset((0, 429, 500, 502, 503, 504)), (2, 5, 15)


def request(method, path, payload, token):
    """The HTTP call behind github() and compile_pr_errors.github(), each of which checks its own allowlist first. A GET
    whose status is in RETRY_STATUSES is tried again after each of RETRY_SECONDS; the last status is returned."""
    for wait in RETRY_SECONDS:
        status, data = request_once(method, path, payload, token)
        if method != "GET" or status not in RETRY_STATUSES:
            return status, data
        time.sleep(wait)
    return request_once(method, path, payload, token)


def request_once(method, path, payload, token):
    call = urllib.request.Request(API + path, data=None if payload is None else json.dumps(payload).encode(),
                                     method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(call, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, None


def comment_author(environ):
    """The comment app's own bot login, "<slug>[bot]", from the mint's app-slug output in APP_SLUG; "" (no comment's
    author) when it is unset or odd, as with the PAT fallback, whose user is not known here (review of #137)."""
    slug = environ.get(APP_SLUG, "")
    return slug + "[bot]" if SLUG.fullmatch(slug) else ""


def post_once(call_api, repo, number, sha, body, token, name=MARKER, author=None):
    """Post body on the pull request unless a comment already carries this head's marker: True when posted, False
    when it was already there. One comment per head: a re-run (or a second dispatch) for a head already reported
    posts nothing again. With author (comment_author), only that bot's own comments count, so a person's or another
    app's comment quoting the marker never stops the post; "" counts none. Every message is repository-neutral."""
    for page in range(1, MAX_COMMENT_PAGES + 1):
        status, comments = call_api("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}", None, token)
        if status != 200 or not isinstance(comments, list):
            raise DsCompileError("The pull request's comments could not be read; nothing was posted")
        if already_posted(comments, sha, name, author):
            return False
        if len(comments) < 100:
            break
    status, _ = call_api("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, token)
    if status != 201:
        raise DsCompileError("GitHub did not accept the comment; nothing from the compile is shown here")
    return True


def outcome(private):
    """True when the compile passed, False when it failed; refuses when no outcome is recorded."""
    try:
        result = json.loads((private / "result.json").read_text())
    except (OSError, ValueError):
        raise DsCompileError("No compile outcome is recorded; nothing was posted") from None
    if not isinstance(result, dict) or result.get("result") not in ("passed", "failed"):
        raise DsCompileError("No compile outcome is recorded; nothing was posted")
    return result["result"] == "passed"


def checked_record(data, sha):
    """The record seal wrote, checked whole: {sha, passed, errors, total}, its commit the dispatched one, its errors
    single lines of a bounded length; refuses otherwise."""
    count = lambda v: type(v) is int and 0 <= v <= MAX_TOTAL
    if not (isinstance(data, dict) and set(data) == {"sha", "passed", "errors", "total"} and data["sha"] == sha and
            type(data["passed"]) is bool and isinstance(data["errors"], list) and len(data["errors"]) <= MAX_ERRORS and
            all(isinstance(e, str) and 0 < len(e) <= MAX_ERROR_LINE and "\n" not in e and "\r" not in e
                for e in data["errors"]) and count(data["total"]) and data["total"] >= len(data["errors"]) and
            (not data["passed"] or (data["errors"] == [] and data["total"] == 0))):
        raise DsCompileError("The compile record is malformed or for another commit; nothing was posted")
    return data


def compile_record(sha, root):
    """The compile's record from the native job's private files, the errors reduced from its log."""
    checkout, private = root / SOURCE, root / PRIVATE
    passed = outcome(private)
    # A sanity check only: the build could have changed the checkout. The pull request's own head, read again by the
    # post job, is the check that counts.
    if head_revision(checkout) != sha:
        raise DsCompileError("The compiled checkout is not the dispatched commit; nothing was sealed")
    errors, total = [], 0
    if not passed:
        log = private / "compile.log"
        # The build ran before this step and could leave a link in place of its log: only a regular file is read.
        try:
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() and not log.is_symlink() else ""
        except OSError:
            text = ""
        errors, total = diagnostics(text, [checkout, checkout.resolve()])
        errors = [e for e in errors if 0 < len(e) <= MAX_ERROR_LINE and "\n" not in e and "\r" not in e]
    return checked_record({"sha": sha, "passed": passed, "errors": errors, "total": total}, sha)


def seal(sha):
    """The native job's record, age-encrypted to RECIPIENT for the post job; nothing in the clear leaves the runner.
    Without the recipient nothing is sealed, and the post job posts the job's result alone."""
    require_step_secrets(())
    exact(sha)
    root = runner_root()
    record, private = compile_record(sha, root), root / PRIVATE
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("DS Swift compile: MAY_RENDERS_RECIPIENT is not set in may-ds-renders; nothing is sealed, so only the "
              "result will be posted")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise DsCompileError("MAY_RENDERS_RECIPIENT is not an age public key")
    plain, sealed = private / RECORD, root / SEALED
    sealed.mkdir(mode=0o700, exist_ok=True)
    data = json.dumps(record).encode()
    if len(data) > MAX_RECORD:
        raise DsCompileError("The compile record is too large to seal")
    try:
        plain.unlink(missing_ok=True)
        write_private(plain, data + b" " * (MAX_RECORD - len(data)))
        if subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(sealed / SEALED_NAME),
                           str(plain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=child_env()).returncode:
            raise DsCompileError("The compile record could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print("DS Swift compile: its record is encrypted for the post job")


def verdict():
    """The native job's last word: it fails when the compile failed, whether or not the post job can post."""
    require_step_secrets(())
    if outcome(runner_root() / PRIVATE):
        print(f"DS Swift compile passed with Xcode {XCODE}")
        return 0
    print("DS Swift compile failed; its errors go to the design-system pull request only")
    return 1


def open_record(sha):
    """The post job's sealed record, decrypted with DECRYPT_KEY after the key is checked against RECIPIENT, then
    checked whole into the private folder. No sealed record: a fixed line."""
    require_step_secrets((DECRYPT_KEY,))
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    exact(sha)
    root = runner_root()
    sealed = root / DOWNLOAD / SEALED_NAME
    if not sealed.is_file() or sealed.is_symlink():
        print("DS Swift compile: no sealed record from this run attempt; only the result will be posted")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise DsCompileError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-ds-renders")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise DsCompileError("The decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-ds-renders)")
    private = root / PRIVATE
    private.mkdir(mode=0o700, exist_ok=True)
    key, plain, env = private / "compile.key", private / (RECORD + ".part"), child_env()
    try:
        write_private(key, (lines[0] + "\n").encode())
        derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)], capture_output=True,
                                 stdin=subprocess.DEVNULL, env=env)
        if derived.returncode or derived.stdout.decode().strip() != recipient:
            raise DsCompileError("The decryption key does not match MAY_RENDERS_RECIPIENT")
        plain.unlink(missing_ok=True)
        write_private(plain, b"")
        if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain), str(sealed)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          env=env).returncode or plain.stat().st_size != MAX_RECORD:
            raise DsCompileError("The compile record failed to decrypt or authenticate")
        try:
            record = checked_record(json.loads(plain.read_text()), sha)
        except (ValueError, RecursionError):
            raise DsCompileError("The compile record is malformed or for another commit; nothing was posted") from None
        (private / RECORD).unlink(missing_ok=True)
        write_private(private / RECORD, json.dumps(record).encode())
    finally:
        key.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
    print("DS Swift compile: its record is decrypted and checked")


def result_body(sha, url, passed):
    """The comment when no record reached the post job: the native job's pass or fail, and why there is no detail."""
    exact(sha)
    lines = [f"### May DS Swift compile: {'passed' if passed else 'failed'}", "",
             f"`{sha}`: the compile job {'passed' if passed else 'failed'}. Its details are not posted: no sealed record "
             "reached the post job (`MAY_RENDERS_RECIPIENT` or `MAY_RENDERS_DECRYPT_KEY` may be unset in may-ds-renders, "
             "or the job stopped before its seal)."]
    if url:
        lines += ["", f"Run: {url}"]
    return "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    exact(sha)
    number = pull_number(pr)
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    path = runner_root() / PRIVATE / RECORD
    sealed = path.is_file() and not path.is_symlink()
    if sealed:
        try:
            record = checked_record(json.loads(path.read_text()), sha)
        except (OSError, ValueError, RecursionError):
            raise DsCompileError("The compile record is malformed or for another commit; nothing was posted") from None
        passed, errors, total = record["passed"], record["errors"], record["total"]
    else:
        passed, errors, total = RESULTS.get(os.environ.get(NATIVE_RESULT, "")), [], 0
        if passed is None:
            print("DS Swift compile: no record and no compile result to post; nothing posted")
            return 0
    if len(token) < 20:
        raise DsCompileError("Design-system comment token unavailable (MAY_DS_PR_COMMENT_TOKEN in may-ds-renders)")
    url = run_url(os.environ)
    body = comment_body(sha, url, passed, errors, total) if sealed else result_body(sha, url, passed)
    if holds(body, token):
        raise DsCompileError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{DS_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise DsCompileError("The design-system pull request could not be read again; nothing was posted")
    try:
        validate_ds_pull_request(pull, number, sha)
    except GateDenial:
        raise DsCompileError("The pull request is no longer open at the compiled head; nothing was posted") from None
    if not post_once(call_api, DS_REPO, number, sha, body, token, author=comment_author(os.environ)):
        print(f"DS Swift compile result already posted for this head on may-design-system #{number}; nothing posted again")
        return 0 if passed else 1
    if not sealed:
        print(f"DS Swift compile result alone posted to may-design-system #{number} (no sealed record: no details)")
        return 0 if passed else 1
    if passed:
        print(f"DS Swift compile passed with Xcode {XCODE}; result posted to may-design-system #{number}")
        return 0
    print(f"DS Swift compile failed; {int(total)} errors posted to may-design-system #{number}")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("fetch", "compile", "seal", "verdict", "open", "post"))
    parser.add_argument("--ds-sha")
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    takes = {"fetch": (True, True), "compile": (False, False), "seal": (True, False), "verdict": (False, False),
             "open": (True, False), "post": (True, True)}[args.step]
    if (args.ds_sha is not None, args.pr_number is not None) != takes:
        raise DsCompileError("fetch and post take --ds-sha and --pr-number, seal and open --ds-sha alone, compile and "
                             "verdict neither")
    if args.step == "fetch":
        fetch(args.ds_sha, args.pr_number)
    elif args.step == "compile":
        compile_ds()
    elif args.step == "seal":
        seal(args.ds_sha)
    elif args.step == "verdict":
        return verdict()
    elif args.step == "open":
        open_record(args.ds_sha)
    else:
        return post(args.ds_sha, args.pr_number)
    return 0


if __name__ == "__main__":
    try:
        failed = main()
    except GateDenial as denial:
        raise SystemExit("May DS Swift compile denied: " + denial.args[0]) from None
    except (DsCompileError, StoreError) as error:
        raise SystemExit("May DS Swift compile stopped: " + error.args[0]) from None
    except (PrepareError, OSError, subprocess.SubprocessError):
        raise SystemExit("May DS Swift compile stopped; no design-system source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May DS Swift compile stopped; check the may-ds-renders environment, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
