"""Post an iOS compile's errors on the may-app pull request it compiled, never in this log (native-compile-pr.yml).

The pull-request compile (compile.py --record-outcome) keeps its log private, so the iOS lane cannot see why a may-app
pull request fails to compile. This step posts the compile's errors as one comment on that pull request, the same
model as ds_compile.py for may-design-system, whose redaction, dedupe, cap, fence and marker it reuses. This
repository is public, and so are its logs, step summaries and artifacts; may-app is private. So nothing derived from
may-app (a path, a file name, a compiler message, a log line) is printed, summarised or uploaded here: every line
this script prints is fixed, apart from the dispatch's own pull request number and the number of errors, and every
error is a fixed literal (static_check.py).

The compile runs the pull request's own build code (Xcode build scripts, SwiftPM plugins) as the runner user, so
the job that compiles never holds the comment token (independent review of may-releases #130): the post is a job of
its own, on a fresh Linux runner, after it.

  seal     (native job, no secret, after the compile) reduces the private log to file-located errors inside the
           may-app checkout, repo-relative (the design system checked out inside it, android/build-deps/, is left out:
           ds-swift-compile.yml reports that), and age-encrypts that record ({sha, passed, errors, total}) to
           MAY_RENDERS_RECIPIENT, the environment's own key pair (never device tests' or renders'), for the one
           upload. Without the recipient nothing is sealed, and one fixed line says nothing will be posted.
  verdict  (native job, no secret) fails the job when the compile failed, so a failure shows whether or not the
           post job can post it.
  open     (post job, MAY_RENDERS_DECRYPT_KEY alone) decrypts the record after checking the key against the
           recipient, and checks its shape and its commit before anything reads it.
  post     (post job, MAY_APP_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts it as one comment on
           the pull request after the pull request is read again and is still open at the compiled head, and unless a
           comment already carries this head's marker. Only that pull request, its comments and its comment post are
           reachable. Without the token or a record nothing is posted and one fixed line says so; it fails when the
           record says the compile failed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from compile import OUTCOME
from ds_compile import (DsCompileError, MAX_BODY, MAX_ERRORS, SHA, XCODE, diagnostics, fence_for, post_once, request,
                        run_url)
from gate import AGE_RECIPIENT, APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import PrepareError, head_revision
from review_mcp import FOOTER, holds
from store import StoreError, child_env, require_step_secrets, runner_root, write_private
import toolchain

COMMENT_TOKEN = "MAY_APP_PR_COMMENT_TOKEN"
RECIPIENT, DECRYPT_KEY = "MAY_RENDERS_RECIPIENT", "MAY_RENDERS_DECRYPT_KEY"
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")
SOURCE, PRIVATE = "may-source", "may-private"
# The sealed record: written by seal into SEALED, uploaded, downloaded by the post job into DOWNLOAD, opened into RECORD.
SEALED, DOWNLOAD, SEALED_NAME, RECORD = "may-compile-sealed", "may-compile-download", "compile.age", "compile-record.json"
MAX_RECORD, MAX_ERROR_LINE, MAX_TOTAL = 256 * 1024, 600, 1_000_000
LOG = "ios-compile.log"
SCHEME, DESTINATION = "MayPlayground", "generic/platform=iOS Simulator"
# The design system prepare.py checks out inside the may-app checkout: not may-app's own files.
EXCLUDED = ("android/build-deps/",)
MARKER = "may-ios-compile-pr"


class CompilePrError(ValueError):
    pass


def comment_body(sha, url, passed, errors, total):
    """The one comment posted on the pull request. sha is the compiled head; url this run, or None."""
    if not SHA.fullmatch(sha or ""):
        raise CompilePrError("The compiled commit is not an exact SHA")
    how = f"Xcode {XCODE} (`xcodebuild`, scheme `{SCHEME}`, {DESTINATION}, Release, no signing)"
    if passed:
        lines = ["### May iOS compile: passed", "", f"`{sha}` compiles with {how}."]
    else:
        lines = ["### May iOS compile: failed", "", f"`{sha}` does not compile with {how}."]
        if total:
            shown = f" Showing the first {len(errors)}; {total - len(errors)} more not shown." if total > len(errors) else ""
            fence = fence_for(errors)
            lines += ["", f"**{total} error{'s' if total != 1 else ''}** in files of this repository (warnings, notes, "
                      f"source lines, the design system and errors outside the checkout are left out).{shown}", "",
                      fence + "text", *errors, fence]
        else:
            lines += ["", "The build failed, but its output holds no error located in a file of this repository (for "
                      "example a project generation, package resolution, design-system or link failure). Nothing "
                      "else from the log is posted."]
    if url:
        lines += ["", f"Run: {url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        raise CompilePrError("The comment would be too long to post")
    return body


_PR = r"/repos/" + re.escape(APP_REPO) + r"/{}/[1-9][0-9]{{0,6}}"
ALLOWED_CALLS = (("GET", _PR.format("pulls")),
                 ("GET", _PR.format("issues") + r"/comments\?per_page=100&page=(?:[1-9]|10)"),
                 ("POST", _PR.format("issues") + r"/comments"))


def github(method, path, payload, token):
    """One may-app API call: (status, parsed JSON or None). Only ALLOWED_CALLS are made: the pull request read, a page
    of its comments, and its comment post. Nothing from the call is printed."""
    if not any(method == allowed and re.fullmatch(pattern, path) for allowed, pattern in ALLOWED_CALLS):
        raise CompilePrError("Only the may-app pull request and its comments are reachable")
    return request(method, path, payload, token)


def outcome(private):
    """True when the compile passed, False when it failed; refuses when no outcome is recorded."""
    try:
        result = json.loads((private / OUTCOME).read_text())
    except (OSError, ValueError):
        raise CompilePrError("No compile outcome is recorded; nothing was posted") from None
    if not isinstance(result, dict) or result.get("result") not in ("passed", "failed"):
        raise CompilePrError("No compile outcome is recorded; nothing was posted")
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
        raise CompilePrError("The compile record is malformed or for another commit; nothing was posted")
    return data


def private_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise CompilePrError("Runner temporary directory is unavailable")
    return Path(raw).resolve()


def compile_record(sha, root):
    """The compile's record from the native job's private files: {sha, passed, errors, total}, the errors reduced from
    its log (checked_record)."""
    checkout, private = root / SOURCE, root / PRIVATE
    passed = outcome(private)
    # A sanity check only: the build could have changed the checkout. The pull request's own head, read again by the
    # post job, is the check that counts.
    if head_revision(checkout) != sha:
        raise CompilePrError("The compiled checkout is not the dispatched commit; nothing was sealed")
    errors, total = [], 0
    if not passed:
        log = private / LOG
        # The build ran before this step and could leave a link to another file in place of its log: only a regular
        # file is read (security review of 2cf86ce, #3).
        try:
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() and not log.is_symlink() else ""
        except OSError:
            text = ""
        errors, total = diagnostics(text, [checkout, checkout.resolve()], exclude=EXCLUDED)
        errors = [e for e in errors if 0 < len(e) <= MAX_ERROR_LINE and "\n" not in e and "\r" not in e]
    return checked_record({"sha": sha, "passed": passed, "errors": errors, "total": total}, sha)


def seal(sha):
    """The native job's record of the compile, age-encrypted to RECIPIENT for the post job; nothing in the clear leaves
    the runner. Without the recipient nothing is sealed."""
    require_step_secrets(())
    if not SHA.fullmatch(sha or ""):
        raise CompilePrError("Source SHA is not an exact lowercase commit SHA")
    root = private_root()
    record, private = compile_record(sha, root), root / PRIVATE
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("May iOS compile: MAY_RENDERS_RECIPIENT is not set in may-native-compile-pr; nothing is sealed, so "
              "nothing will be posted")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise CompilePrError("MAY_RENDERS_RECIPIENT is not an age public key")
    plain, sealed = private / RECORD, root / SEALED
    sealed.mkdir(mode=0o700, exist_ok=True)
    try:
        write_private(plain, json.dumps(record).encode())
        if subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(sealed / SEALED_NAME),
                           str(plain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=child_env()).returncode:
            raise CompilePrError("The compile record could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print("May iOS compile: its record is encrypted for the post job")


def verdict():
    """The native job's last word: it fails when the compile failed, whether or not the post job can post."""
    require_step_secrets(())
    if outcome(private_root() / PRIVATE):
        print(f"May iOS compile passed with Xcode {XCODE}")
        return 0
    print("May iOS compile failed; its errors go to the may-app pull request only")
    return 1


def open_record(sha):
    """The post job's sealed record (DOWNLOAD/SEALED_NAME), decrypted with DECRYPT_KEY after the key is checked against
    RECIPIENT, then checked whole (checked_record) into the private folder. No sealed record: a fixed line."""
    require_step_secrets((DECRYPT_KEY,))
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    if not SHA.fullmatch(sha or ""):
        raise CompilePrError("Source SHA is not an exact lowercase commit SHA")
    root = runner_root()
    sealed = root / DOWNLOAD / SEALED_NAME
    if not sealed.is_file() or sealed.is_symlink():
        print("May iOS compile: no sealed record from this run attempt; nothing will be posted")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise CompilePrError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-native-compile-pr")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise CompilePrError("The decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-native-compile-pr)")
    private = root / PRIVATE
    private.mkdir(mode=0o700, exist_ok=True)
    key, plain, env = private / "compile.key", private / (RECORD + ".part"), child_env()
    try:
        write_private(key, (lines[0] + "\n").encode())
        derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)], capture_output=True,
                                 stdin=subprocess.DEVNULL, env=env)
        if derived.returncode or derived.stdout.decode().strip() != recipient:
            raise CompilePrError("The decryption key does not match MAY_RENDERS_RECIPIENT")
        write_private(plain, b"")
        if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain), str(sealed)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          env=env).returncode or plain.stat().st_size > MAX_RECORD:
            raise CompilePrError("The compile record failed to decrypt or authenticate")
        try:
            record = checked_record(json.loads(plain.read_text()), sha)
        except (ValueError, RecursionError):
            raise CompilePrError("The compile record is malformed or for another commit; nothing was posted") from None
        write_private(private / RECORD, json.dumps(record).encode())
    finally:
        key.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
    print("May iOS compile: its record is decrypted and checked")


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    if not SHA.fullmatch(sha or ""):
        raise CompilePrError("Source SHA is not an exact lowercase commit SHA")
    number = pull_number(pr)
    path = private_root() / PRIVATE / RECORD
    if not path.is_file() or path.is_symlink():
        print("May iOS compile: no record to post (MAY_RENDERS_RECIPIENT or MAY_RENDERS_DECRYPT_KEY may be unset in "
              "may-native-compile-pr); the native job's verdict stands")
        return 0
    try:
        record = checked_record(json.loads(path.read_text()), sha)
    except (OSError, ValueError, RecursionError):
        raise CompilePrError("The compile record is malformed or for another commit; nothing was posted") from None
    passed, errors, total = record["passed"], record["errors"], record["total"]
    if not token:
        print("May iOS compile: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")
        return 0 if passed else 1
    if len(token) < 20:
        raise CompilePrError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    body = comment_body(sha, run_url(os.environ), passed, errors, total)
    if holds(body, token):
        raise CompilePrError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise CompilePrError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise CompilePrError("The pull request is no longer open at the compiled head; nothing was posted") from None
    if not post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May iOS compile result already posted for this head on may-app #{number}; nothing posted again")
        return 0 if passed else 1
    if passed:
        print(f"May iOS compile passed with Xcode {XCODE}; result posted to may-app #{number}")
        return 0
    print(f"May iOS compile failed; {int(total)} errors posted to may-app #{number}")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seal", "verdict", "open", "post"))
    parser.add_argument("--source-sha")
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    if args.command == "seal":
        return seal(args.source_sha)
    if args.command == "verdict":
        return verdict()
    if args.command == "open":
        return open_record(args.source_sha)
    return post(args.source_sha, args.pr_number)


if __name__ == "__main__":
    try:
        failed = main()
    except GateDenial as denial:
        raise SystemExit("May iOS compile post denied: " + denial.args[0]) from None
    except (CompilePrError, DsCompileError, StoreError) as error:
        raise SystemExit("May iOS compile post stopped: " + error.args[0]) from None
    except (PrepareError, OSError):
        raise SystemExit("May iOS compile post stopped; no may-app source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May iOS compile post stopped; check may-native-compile-pr, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
