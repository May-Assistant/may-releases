"""may-app's Linux Swift checks at an open may-app pull request head, before its iOS compile (native-compile-pr.yml).

may-app's tools/ios/test_swift_*.py (may-app #1050: tools/ios/test_swift_inits.py, with test_swift_call_sites.py) read
ios/App, MayCore/Sources and the pinned May DS prepare.py places at android/build-deps/may-design-system, and catch an
init's labels out of order or a missing import without a Mac. The swift-checks job runs them on a Linux runner and the
macOS compile needs it, so a slip they catch fails in a minute instead of after the Mac build.

  run   (no secret) runs `python3 -I -m unittest discover -s tools/ios -p 'test_swift_*.py'` in the checkout, every
        line it prints going to a private log. The outcome (passed, failed, skipped: no such check in this commit, or
        timeout) and each failing check (its test id and the lines of its assertion, repo-relative, cleaned as
        android_unit.py cleans) go to private files. Public output is one fixed line and a count. It never fails the
        step: post does.
  post  (MAY_APP_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts a failed outcome's checks as one
        comment on the pull request, after reading it again and finding it still open at the checked head, unless a
        comment already carries this head's marker (compile_pr_errors.py's model and calls). A passed or skipped
        outcome posts nothing. It fails the job when the checks failed or did not finish, so the compile never starts.

-s tools/ios, not -s tools: neither tools/ nor tools/ios/ is a package, so discovery from tools/ finds no test.

This repository is public; may-app is private. Nothing derived from may-app (a test name, a message, a path) is
printed, summarised or uploaded here: every printed line is fixed, apart from the dispatch's own pull request number
and the number of failing checks, and every error is a fixed literal (static_check.py).
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

from android_unit import clean
from compile_pr_errors import CompilePrError, github
from ds_compile import DsCompileError, MAX_BODY, SHA, fence_for, post_once, run_url
from gate import APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import PrepareError, head_revision
from review_mcp import FOOTER, holds
from store import StoreError, child_env, prepared, require_step_secrets, runner_root

COMMENT_TOKEN = "MAY_APP_PR_COMMENT_TOKEN"
CHECKS_DIR, PATTERN = "tools/ios", "test_swift_*.py"
LOG, OUTCOME, FAILURES = "swift-checks.log", "swift-checks-result.json", "swift-checks-failures.json"
RESULTS = ("passed", "failed", "skipped", "timeout")
CHECK_SECONDS = 10 * 60
# unittest's exit status when discovery found no test (Python 3.12 and later).
NO_TESTS = 5
MAX_FAILURES, MAX_LINES, MAX_LINE, MAX_NAME = 50, 20, 300, 200
MARKER = "may-swift-checks-pr"
SEPARATOR = re.compile(r"^(?:=|-){70}$")
HEADER = re.compile(r"^(FAIL|ERROR): ([\w.]+) \(([\w.]+)\)$")
RAISED = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Failure)\b")
SUMMARY = re.compile(r"^FAILED \((.*)\)$")


class SwiftChecksError(ValueError):
    pass


def failing_checks(text, roots):
    """[(kind, test id, [assertion lines])] from unittest's output: each FAIL or ERROR block's header and the lines from
    its raised exception to the block's end, cleaned and capped."""
    found, current, lines, raised = [], None, [], False
    for line in text.splitlines() + ["=" * 70]:
        header = HEADER.match(line)
        if header or SEPARATOR.match(line) and line.startswith("="):
            if current is not None:
                found.append((current[0], current[1], lines[:MAX_LINES]))
            current, lines, raised = ((header.group(1).lower(), clean(header.group(3), roots, MAX_NAME))
                                      if header else None), [], False
            continue
        if current is not None and SEPARATOR.match(line) and raised:
            # The dashes after a block's traceback end it ("Ran N tests" follows the last one).
            found.append((current[0], current[1], lines[:MAX_LINES]))
            current, lines, raised = None, [], False
            continue
        if current is None or SEPARATOR.match(line):
            continue
        if not raised and RAISED.match(line):
            raised = True
        if raised and line.strip():
            lines.append(clean(line, roots, MAX_LINE))
    return found[:MAX_FAILURES], len(found)


def problem_count(text, blocks):
    """The failures and errors unittest's summary line counts, or the blocks found when it has none."""
    for line in reversed(text.splitlines()):
        summary = SUMMARY.match(line.strip())
        if summary:
            counts = dict(part.split("=", 1) for part in summary.group(1).split(", ") if "=" in part)
            return sum(int(counts.get(k, "0")) for k in ("failures", "errors") if counts.get(k, "0").isdigit())
    return blocks


def run_checks(source_sha):
    require_step_secrets(())
    if not SHA.fullmatch(source_sha or ""):
        raise SwiftChecksError("Source SHA is not an exact lowercase commit SHA")
    source, private = prepared(runner_root(), source_sha)
    log = private / LOG
    checks = source / CHECKS_DIR
    if not checks.is_dir() or checks.is_symlink() or not any(p.is_file() for p in checks.glob(PATTERN)):
        result, code = "skipped", NO_TESTS
    else:
        try:
            with log.open("wb") as output:
                code = subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", CHECKS_DIR, "-p",
                                       PATTERN], cwd=source, env=child_env(), stdout=output, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, timeout=CHECK_SECONDS).returncode
            result = "passed" if code == 0 else "skipped" if code == NO_TESTS else "failed"
        except subprocess.TimeoutExpired:
            result, code = "timeout", None
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() and not log.is_symlink() else ""
    roots = [str(source), str(source.resolve())]
    entries, blocks = failing_checks(text, roots) if result == "failed" else ([], 0)
    problems = problem_count(text, blocks) if result == "failed" else 0
    (private / FAILURES).write_text(json.dumps({"failures": [{"kind": k, "test": t, "lines": l} for k, t, l in entries],
                                                "total": problems}))
    (private / OUTCOME).write_text(json.dumps({"result": result, "problems": problems}))
    if result == "passed":
        print("May Swift checks passed")
    elif result == "skipped":
        print("May Swift checks: this commit has none (tools/ios/test_swift_*.py); skipped")
    elif result == "timeout":
        print("May Swift checks did not finish within 10 minutes")
    else:
        print(f"May Swift checks failed: {int(problems)} failing checks, which go to the may-app pull request only")


def read_record(private):
    """(outcome, failures) from run's private files, each checked for its shape; refuses otherwise."""
    try:
        outcome = json.loads((private / OUTCOME).read_text())
        failed = json.loads((private / FAILURES).read_text())
    except (OSError, ValueError):
        raise SwiftChecksError("No Swift checks outcome is recorded; nothing was posted") from None
    count = lambda v: type(v) is int and 0 <= v <= 1_000_000
    if not isinstance(outcome, dict) or outcome.get("result") not in RESULTS or not count(outcome.get("problems")):
        raise SwiftChecksError("The Swift checks outcome is malformed; nothing was posted")
    entries = failed.get("failures") if isinstance(failed, dict) else None
    line = lambda v, limit: isinstance(v, str) and len(v) <= limit and "\n" not in v
    if not isinstance(entries, list) or len(entries) > MAX_FAILURES or not count(failed.get("total")) or any(
            not isinstance(e, dict) or e.get("kind") not in ("fail", "error") or not line(e.get("test"), MAX_NAME) or
            not isinstance(e.get("lines"), list) or len(e["lines"]) > MAX_LINES or
            not all(line(x, MAX_LINE) for x in e["lines"]) for e in entries):
        raise SwiftChecksError("The failing-check record is malformed; nothing was posted")
    return outcome, failed


def comment_body(sha, url, outcome, failed, shown=None):
    """The one comment posted on the pull request when the checks failed or did not finish."""
    if not SHA.fullmatch(sha or ""):
        raise SwiftChecksError("The checked commit is not an exact SHA")
    lines = ["### May Swift checks: failed", "",
             f"`{sha}`: `python3 -I -m unittest discover -s {CHECKS_DIR} -p '{PATTERN}'` on ubuntu-24.04, with the pinned "
             "May DS checked out. The iOS compile did not run."]
    if outcome["result"] == "timeout":
        lines += ["", "The checks did not finish within 10 minutes. Nothing else from the log is posted."]
    entries = failed["failures"] if shown is None else failed["failures"][:shown]
    if entries:
        block = []
        for entry in entries:
            block.append(f"{entry['test']} ({entry['kind']})")
            block += ["    " + text for text in entry["lines"]]
        more = failed["total"] - len(entries)
        extra = f" Showing the first {len(entries)}; {more} more not shown." if more > 0 else ""
        fence = fence_for(block)
        lines += ["", f"**{failed['total']} failing check{'s' if failed['total'] != 1 else ''}**: each one's test and "
                  f"the lines of its assertion.{extra}", "", fence + "text", *block, fence]
    elif outcome["result"] == "failed":
        lines += ["", "The checks failed without a readable failing test (for example an import error before any test "
                  "ran). Nothing else from the log is posted."]
    if url:
        lines += ["", f"Run: {url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        if not entries:
            raise SwiftChecksError("The comment would be too long to post")
        return comment_body(sha, url, outcome, failed, len(entries) // 2)
    return body


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    if not SHA.fullmatch(sha or ""):
        raise SwiftChecksError("Source SHA is not an exact lowercase commit SHA")
    number = pull_number(pr)
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise SwiftChecksError("Runner temporary directory is unavailable")
    root = Path(raw).resolve()
    checkout, private = root / "may-source", root / "may-private"
    outcome, failed = read_record(private)
    if outcome["result"] in ("passed", "skipped"):
        print("May Swift checks: nothing to post; the compile may run")
        return 0
    total = failed["total"]
    if not token:
        print("May Swift checks: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")
        return 1
    if len(token) < 20:
        raise SwiftChecksError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    if head_revision(checkout) != sha:
        raise SwiftChecksError("The checked checkout is not the dispatched commit; nothing was posted")
    body = comment_body(sha, run_url(os.environ), outcome, failed)
    if holds(body, token):
        raise SwiftChecksError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise SwiftChecksError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise SwiftChecksError("The pull request is no longer open at the checked head; nothing was posted") from None
    if not post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May Swift checks result already posted for this head on may-app #{number}; nothing posted again")
        return 1
    print(f"May Swift checks failed; {int(total)} failing checks posted to may-app #{number}")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "post"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    if args.command == "run":
        run_checks(args.source_sha)
        return 0
    return post(args.source_sha, args.pr_number)


if __name__ == "__main__":
    try:
        failed = main()
    except GateDenial as denial:
        raise SystemExit("May Swift checks denied: " + denial.args[0]) from None
    except (SwiftChecksError, CompilePrError, DsCompileError, StoreError) as error:
        raise SystemExit("May Swift checks stopped: " + error.args[0]) from None
    except (PrepareError, OSError, subprocess.SubprocessError):
        raise SystemExit("May Swift checks stopped; no may-app source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May Swift checks stopped; check may-native-compile-pr, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
