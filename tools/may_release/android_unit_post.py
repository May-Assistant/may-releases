"""Post the Android unit suites' outcome and failing tests on the may-app pull request, never in this log
(android-unit-pr.yml).

The unit suites (android_unit.py --record-outcome) keep their log and reports private, so a red run cannot say which
tests failed. This step posts them as one comment on that pull request, the same model as compile_pr_errors.py,
whose pull request recheck, allowlisted calls, dedupe marker and fence it reuses. This repository is public, and so
are its logs, step summaries and artifacts; may-app is private. So nothing derived from may-app (a test name, a
message, a path) is printed, summarised or uploaded here: every line this script prints is fixed, apart from the
dispatch's own pull request number and the number of failing tests, and every error is a fixed literal
(static_check.py). The step summary carries each suite's counts and the failing-test total, nothing else.

The suites run the pull request's own Gradle build as the runner user, so their job references no GitHub App key and
no token (independent review of may-releases #133); the post is a job of its own, on a fresh runner:

  seal     (unit job, no secret, after the suites) checks the checkout is still the dispatched commit and seals the
           outcome and failing tests to the environment's own age key (sealed.py) for the one upload. Without the key
           pair nothing is sealed and one fixed line says so.
  verdict  (unit job, no secret) writes the step summary (counts) and fails the job when the suites failed, so a
           failure shows whether or not the post job can post it.
  open     (post job, MAY_RENDERS_DECRYPT_KEY alone) opens the sealed record into the private folder (sealed.py).
  post     (post job, MAY_APP_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts it after the pull
           request is read again and is still open at the tested head, and unless a comment already carries this
           head's marker. Only that pull request, its comments and its comment post are reachable. Without the token or
           a record, nothing is posted and one fixed line says so; it fails when the record says the suites failed.
"""

import argparse
import json
import os
from pathlib import Path
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_unit import FAILURES, MAX_FAILURES, MAX_MESSAGE, MAX_NAME, OUTCOME, SUITES
from compile_pr_errors import CompilePrError, github
from ds_compile import DsCompileError, MAX_BODY, SHA, fence_for, post_once, run_url
from gate import APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import PrepareError, head_revision
from review_mcp import FOOTER, holds
import sealed
from sealed import SealedError
from store import StoreError, require_step_secrets

COMMENT_TOKEN = "MAY_APP_PR_COMMENT_TOKEN"
SOURCE, PRIVATE = "may-source", "may-private"
MARKER = "may-android-unit-pr"
LABELS = {"core": "core (`:core:test`)", "playground": "May Playground (`:app:testPlaygroundDebugUnitTest`)",
          "production": "May production (`:app:testProductionDebugUnitTest`)"}
GRADLE = {"ok": None, "failed": "Gradle exited with a failure.", "timeout": "Gradle did not finish within 50 minutes."}


class UnitPostError(ValueError):
    pass


def text_or_none(value, limit):
    return value is None or (isinstance(value, str) and len(value) <= limit and "\n" not in value)


def read_record(private):
    """(outcome, failures) from android_unit.py's private files, each checked for its shape; refuses otherwise."""
    try:
        outcome = json.loads((private / OUTCOME).read_text())
        failed = json.loads((private / FAILURES).read_text())
    except (OSError, ValueError):
        raise UnitPostError("No unit-suite outcome is recorded; nothing was posted") from None
    suites = outcome.get("suites") if isinstance(outcome, dict) else None
    count = lambda v: type(v) is int and 0 <= v <= 10_000_000
    if not isinstance(outcome, dict) or outcome.get("result") not in ("passed", "failed") or \
            outcome.get("gradle") not in GRADLE or not isinstance(suites, dict) or set(suites) != set(SUITES) or \
            any(v is not None and not (isinstance(v, list) and len(v) == 3 and all(count(n) for n in v))
                for v in suites.values()):
        raise UnitPostError("The unit-suite outcome is malformed; nothing was posted")
    entries = failed.get("failures") if isinstance(failed, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_FAILURES or not count(failed.get("total")) or \
            not count(failed.get("unreadable")) or failed["total"] < len(entries) or any(
                not isinstance(e, dict) or e.get("suite") not in SUITES or e.get("kind") not in ("failure", "error") or
                not isinstance(e.get("test"), str) or not text_or_none(e["test"], MAX_NAME) or
                not text_or_none(e.get("message"), MAX_MESSAGE) or not text_or_none(e.get("at"), MAX_NAME)
                for e in entries):
        raise UnitPostError("The failing-test record is malformed; nothing was posted")
    return outcome, failed


def summary_lines(outcome, failed):
    """This run's step summary: each suite's counts and the failing-test total, numbers and fixed words only (this
    repository is public; static_check.py pins every value formatted here). Written whether or not a comment is
    posted, so the result is readable here while MAY_APP_PR_COMMENT_TOKEN is not set."""
    verdict = "passed" if outcome["result"] == "passed" else "failed"
    lines = [f"### May Android unit suites: {verdict}", "", "| Suite | Tests | Failed | Skipped |", "|---|---|---|---|"]
    for name in SUITES:
        counts = outcome["suites"][name]
        if counts is None:
            lines.append(f"| {name} | did not run | – | – |")
        else:
            lines.append(f"| {name} | {int(counts[0])} | {int(counts[1])} | {int(counts[2])} |")
    lines += ["", f"{int(failed['total'])} failing tests and {int(failed['unreadable'])} unreadable reports. Which tests "
              "failed goes only to the may-app pull request, never here."]
    return lines


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def comment_body(sha, url, outcome, failed, shown=None):
    """The one comment posted on the pull request. sha is the tested head; url this run, or None."""
    if not SHA.fullmatch(sha or ""):
        raise UnitPostError("The tested commit is not an exact SHA")
    passed = outcome["result"] == "passed"
    lines = [f"### May Android unit suites: {'passed' if passed else 'failed'}", "",
             f"`{sha}`, on ubuntu-24.04 with the pinned Temurin JDK 21.", "",
             "| Suite | Result | Tests | Failed | Skipped |", "|---|---|---|---|---|"]
    for name in SUITES:
        counts = outcome["suites"][name]
        if counts is None:
            lines.append(f"| {LABELS[name]} | did not run | – | – | – |")
        else:
            tests, failures, skipped = counts
            verdict = "passed" if tests and not failures else "failed"
            lines.append(f"| {LABELS[name]} | {verdict} | {tests} | {failures} | {skipped} |")
    if GRADLE[outcome["gradle"]]:
        lines += ["", GRADLE[outcome["gradle"]]]
    entries = failed["failures"] if shown is None else failed["failures"][:shown]
    if entries:
        block = []
        for entry in entries:
            block.append(f"{entry['suite']} · {entry['test']} ({entry['kind']})")
            if entry.get("message"):
                block.append(f"    {entry['message']}")
            if entry.get("at"):
                block.append(f"    at {entry['at']}")
        more = failed["total"] - len(entries)
        extra = f" Showing the first {len(entries)}; {more} more not shown." if more > 0 else ""
        fence = fence_for(block)
        lines += ["", f"**{failed['total']} failing test{'s' if failed['total'] != 1 else ''}**: each one's class and "
                  f"method, the first line of its message, and where in its own source it failed.{extra}", "",
                  fence + "text", *block, fence]
    elif not passed:
        lines += ["", "No failing test was reported: a suite's build or Gradle stopped before its tests ran, or a "
                  "suite ran no test. Nothing else from the log is posted."]
    if failed["unreadable"]:
        lines += ["", f"{failed['unreadable']} report file(s) could not be read and are not shown."]
    if url:
        lines += ["", f"Run: {url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        if not entries:
            raise UnitPostError("The comment would be too long to post")
        return comment_body(sha, url, outcome, failed, len(entries) // 2)
    return body


def runner_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise UnitPostError("Runner temporary directory is unavailable")
    return Path(raw).resolve()


def seal(sha):
    """The unit job's outcome and failing tests, checked and sealed for the post job (sealed.py)."""
    require_step_secrets(())
    if not SHA.fullmatch(sha or ""):
        raise UnitPostError("Source SHA is not an exact lowercase commit SHA")
    root = runner_root()
    read_record(root / PRIVATE)
    # A sanity check only: the build could have changed the checkout. The pull request's own head, read again by the
    # post job, is the check that counts.
    if head_revision(root / SOURCE) != sha:
        raise UnitPostError("The tested checkout is not the dispatched commit; nothing was sealed")
    if sealed.seal(root, sha, (OUTCOME, FAILURES)):
        print("May Android unit suites: their record is encrypted for the post job")
    else:
        print("May Android unit suites: MAY_RENDERS_RECIPIENT is not set in may-native-compile-pr; nothing is sealed, "
              "so nothing will be posted")


def verdict():
    """The unit job's last word: the step summary (counts), and a failure when the suites failed."""
    require_step_secrets(())
    outcome, failed = read_record(runner_root() / PRIVATE)
    write_summary(summary_lines(outcome, failed))
    if outcome["result"] == "passed":
        print("May Android unit suites passed")
        return 0
    print("May Android unit suites failed; which tests goes to the may-app pull request only")
    return 1


def open_record(sha):
    require_step_secrets((sealed.DECRYPT_KEY,))
    if sealed.open_sealed(runner_root(), sha, (OUTCOME, FAILURES)):
        print("May Android unit suites: their record is decrypted")
    else:
        print("May Android unit suites: no sealed record from this run attempt; nothing will be posted")


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    if not SHA.fullmatch(sha or ""):
        raise UnitPostError("Source SHA is not an exact lowercase commit SHA")
    number = pull_number(pr)
    private = runner_root() / PRIVATE
    if not (private / OUTCOME).is_file():
        print("May Android unit suites: no record to post (MAY_RENDERS_RECIPIENT or MAY_RENDERS_DECRYPT_KEY may be "
              "unset in may-native-compile-pr); the unit job's verdict stands")
        return 0
    outcome, failed = read_record(private)
    passed = outcome["result"] == "passed"
    total = failed["total"]
    if not token:
        print("May Android unit suites: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")
        return 0 if passed else 1
    if len(token) < 20:
        raise UnitPostError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    body = comment_body(sha, run_url(os.environ), outcome, failed)
    if holds(body, token):
        raise UnitPostError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise UnitPostError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise UnitPostError("The pull request is no longer open at the tested head; nothing was posted") from None
    if not post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May Android unit suites' result already posted for this head on may-app #{number}; nothing posted again")
        return 0 if passed else 1
    if passed:
        print(f"May Android unit suites passed; result posted to may-app #{number}")
        return 0
    print(f"May Android unit suites failed; {int(total)} failing tests posted to may-app #{number}")
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
        raise SystemExit("May Android unit post denied: " + denial.args[0]) from None
    except (UnitPostError, SealedError, CompilePrError, DsCompileError, StoreError) as error:
        raise SystemExit("May Android unit post stopped: " + error.args[0]) from None
    except (PrepareError, OSError):
        raise SystemExit("May Android unit post stopped; no may-app source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May Android unit post stopped; check may-native-compile-pr, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
