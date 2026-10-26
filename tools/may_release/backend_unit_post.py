"""Post the backend checks' outcome and failures on the may-app pull request, never in this log (backend-unit-pr.yml).

The backend checks (backend_unit.py --record-outcome) keep their logs private, so a red run cannot say what failed.
This step posts it as one comment on that pull request, as android_unit_post.py does for the Android unit suites,
reusing compile_pr_errors.py's pull request recheck and allowlisted calls and ds_compile.py's dedupe marker and fence.
This repository is public, and so are its logs, step summaries and artifacts; may-app is private. So nothing derived
from may-app (a test name, a message, a path, a log line) is printed, summarised or uploaded here: every line this
script prints is fixed, apart from the dispatch's own pull request number and two counts, and every error is a fixed
literal (static_check.py). The step summary carries each stage's verdict and the test counts, nothing else.

The checks run the pull request's own npm scripts and tests as the runner user, so their job references no GitHub App
key and no token (independent review of may-releases #133); the post is a job of its own, on a fresh runner:

  seal     (backend job, no secret, after the checks) checks the checkout is still the dispatched commit and seals the
           outcome and failures to the environment's own age key (sealed.py) for the one upload. Without the key pair
           nothing is sealed and one fixed line says so.
  verdict  (backend job, no secret) writes the step summary (verdicts and counts) and fails the job when the checks
           failed, so a failure shows whether or not the post job can post it.
  open     (post job, MAY_RENDERS_DECRYPT_KEY alone) opens the sealed record into the private folder (sealed.py).
  post     (post job, MAY_APP_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts it after the pull
           request is read again and is still open at the tested head, and unless a comment already carries this
           head's marker. Only that pull request, its comments and its comment post are reachable. Without the token or
           a record, nothing is posted and one fixed line says so; it fails when the record says the checks failed.
"""

import argparse
import json
import os
from pathlib import Path
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from backend_unit import FAILURES, MAX_FAILURES, MAX_MESSAGE, MAX_NAME, OUTCOME, STAGES, TAIL_LINES, VERDICTS
from compile_pr_errors import CompilePrError, github
from ds_compile import DsCompileError, MAX_BODY, SHA, fence_for, post_once, run_url
from gate import APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import PrepareError, head_revision
from review_mcp import FOOTER, holds
import sealed
from sealed import SealedError
from store import StoreError, require_step_secrets

COMMENT_TOKEN = "MAY_APP_PR_COMMENT_TOKEN"
# The backend job's own result (needs.backend.result), for the post job when no record reached it (coordinator, 26 Oct:
# with the key pair unset a post would be silent and the may-app merge gate would have no result to read). Only
# success and failure are posted, as the result line alone: no detail, still head-marked.
JOB_RESULT, RESULTS = "MAY_BACKEND_RESULT", {"success": True, "failure": False}
SOURCE, PRIVATE = "may-source", "may-private"
MARKER = "may-backend-unit-pr"
LABELS = {"toolchain": "Toolchain (Node 22.22.0, Postgres 16.15)", "install": "Install (`npm ci`)",
          "typecheck": "Typecheck (`npm run typecheck`)", "migrations": "Migrations from scratch, twice",
          "tests": "Test suite (`npm test`)"}
# Each recorded verdict, in words: the only stage values formatted into the public step summary.
WORDS = {"passed": "passed", "failed": "failed", "timeout": "did not finish in time", "not run": "did not run"}
TAILED = ("install", "typecheck", "migrations", "tests")


class BackendPostError(ValueError):
    pass


def text_or_none(value, limit):
    return value is None or (isinstance(value, str) and len(value) <= limit and "\n" not in value)


def read_record(private):
    """(outcome, failures) from backend_unit.py's private files, each checked for its shape; refuses otherwise."""
    try:
        outcome = json.loads((private / OUTCOME).read_text())
        failed = json.loads((private / FAILURES).read_text())
    except (OSError, ValueError):
        raise BackendPostError("No backend outcome is recorded; nothing was posted") from None
    count = lambda v: type(v) is int and 0 <= v <= 10_000_000
    stages = outcome.get("stages") if isinstance(outcome, dict) else None
    tests, migrations = (outcome.get("tests"), outcome.get("migrations")) if isinstance(outcome, dict) else (None, None)
    if not isinstance(outcome, dict) or outcome.get("result") not in ("passed", "failed") or \
            not isinstance(stages, dict) or set(stages) != set(STAGES) or any(v not in VERDICTS for v in stages.values()) or \
            not (tests is None or (isinstance(tests, list) and len(tests) == 4 and all(count(n) for n in tests))) or \
            not (migrations is None or (isinstance(migrations, list) and len(migrations) == 2 and
                                        all(count(n) for n in migrations))):
        raise BackendPostError("The backend outcome is malformed; nothing was posted")
    if not isinstance(failed, dict):
        raise BackendPostError("The backend failure record is malformed; nothing was posted")
    entries, errors, tails = failed.get("failures"), failed.get("typecheck"), failed.get("tails")
    if not isinstance(entries, list) or len(entries) > MAX_FAILURES or not count(failed.get("total")) or \
            failed["total"] < len(entries) or any(
                not isinstance(e, dict) or set(e) != {"test", "message", "at"} or not isinstance(e["test"], str) or
                not text_or_none(e["test"], MAX_NAME) or not text_or_none(e["message"], MAX_MESSAGE) or
                not text_or_none(e["at"], MAX_NAME) for e in entries) or \
            not isinstance(errors, list) or len(errors) > MAX_FAILURES or not count(failed.get("typecheck_total")) or \
            failed["typecheck_total"] < len(errors) or any(
                not isinstance(e, dict) or set(e) != {"at", "code", "message"} or
                not all(isinstance(e[k], str) for k in e) or not text_or_none(e["at"], MAX_NAME) or
                not text_or_none(e["code"], 16) or not text_or_none(e["message"], MAX_MESSAGE) for e in errors) or \
            not isinstance(tails, dict) or not set(tails) <= set(TAILED) or any(
                not isinstance(lines, list) or len(lines) > TAIL_LINES or
                not all(isinstance(line, str) and text_or_none(line, MAX_MESSAGE) for line in lines)
                for lines in tails.values()):
        raise BackendPostError("The backend failure record is malformed; nothing was posted")
    return outcome, failed


def summary_lines(outcome, failed):
    """This run's step summary: each stage's verdict, the test counts and how many failures were found, numbers and
    fixed words only (this repository is public; static_check.py pins every value formatted here). Written whether or
    not a comment is posted, so the result is readable here while MAY_APP_PR_COMMENT_TOKEN is not set."""
    verdict = "passed" if outcome["result"] == "passed" else "failed"
    lines = [f"### May backend checks: {verdict}", "", "| Stage | Result |", "|---|---|"]
    for name in STAGES:
        lines.append(f"| {name} | {WORDS[outcome['stages'][name]]} |")
    counts = outcome["tests"]
    if counts is None:
        lines += ["", "Tests: no test summary."]
    else:
        lines += ["", f"Tests: {int(counts[0])} tests, {int(counts[1])} passed, {int(counts[2])} failed, "
                  f"{int(counts[3])} skipped."]
    lines += ["", f"{int(failed['total'])} failing tests and {int(failed['typecheck_total'])} type errors. Which ones "
              "goes only to the may-app pull request, never here."]
    return lines


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def fenced(block):
    fence = fence_for(block)
    return [fence + "text", *block, fence]


def comment_body(sha, url, outcome, failed, shown=None):
    """The one comment posted on the pull request. sha is the tested head; url this run, or None. shown, when given,
    caps the failing tests, type errors and log lines listed, so the body fits."""
    if not SHA.fullmatch(sha or ""):
        raise BackendPostError("The tested commit is not an exact SHA")
    passed = outcome["result"] == "passed"
    lines = [f"### May backend checks: {'passed' if passed else 'failed'}", "",
             f"`{sha}`, on ubuntu-24.04 with the pinned Node 22.22.0 and a Postgres 16.15 service container "
             "(may-app's backend-ci.yml, run from may-releases).", "", "| Stage | Result |", "|---|---|"]
    for name in STAGES:
        lines.append(f"| {LABELS[name]} | {WORDS[outcome['stages'][name]]} |")
    counts, migrations = outcome["tests"], outcome["migrations"]
    if counts is not None:
        lines += ["", f"Tests: {counts[0]} tests, {counts[1]} passed, {counts[2]} failed, {counts[3]} skipped."]
    if migrations is not None:
        lines += ["", f"Migrations: {migrations[0]} applied of {migrations[1]} file(s)."]
    entries = failed["failures"] if shown is None else failed["failures"][:shown]
    if entries:
        block = []
        for entry in entries:
            block.append(entry["test"])
            if entry.get("message"):
                block.append(f"    {entry['message']}")
            if entry.get("at"):
                block.append(f"    at {entry['at']}")
        more = failed["total"] - len(entries)
        extra = f" Showing the first {len(entries)}; {more} more not shown." if more > 0 else ""
        lines += ["", f"**{failed['total']} failing test{'s' if failed['total'] != 1 else ''}**: each one's name, the "
                  f"first line of its error, and where it failed.{extra}", "", *fenced(block)]
    errors = failed["typecheck"] if shown is None else failed["typecheck"][:shown]
    if errors:
        more = failed["typecheck_total"] - len(errors)
        extra = f" Showing the first {len(errors)}; {more} more not shown." if more > 0 else ""
        lines += ["", f"**{failed['typecheck_total']} type error{'s' if failed['typecheck_total'] != 1 else ''}**.{extra}",
                  "", *fenced([f"{e['at']}: {e['code']}: {e['message']}" for e in errors])]
    for stage in TAILED:
        tail = failed["tails"].get(stage)
        if tail:
            tail = tail if shown is None else tail[-shown:] if shown else []
            if tail:
                lines += ["", f"Last lines of the {stage} log:", "", *fenced(tail)]
    if not passed and not entries and not errors and not any(failed["tails"].values()):
        lines += ["", "No detail was recorded for the failure. Nothing else from the logs is posted."]
    if url:
        lines += ["", f"Run: {url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        widest = max([len(failed["failures"]), len(failed["typecheck"]),
                      *(len(t) for t in failed["tails"].values())]) if shown is None else shown
        if not widest:
            raise BackendPostError("The comment would be too long to post")
        return comment_body(sha, url, outcome, failed, widest // 2)
    return body


def result_body(sha, url, passed):
    """The comment when no record reached the post job: the backend job's pass or fail, and why there is no detail."""
    if not SHA.fullmatch(sha or ""):
        raise BackendPostError("The tested commit is not an exact SHA")
    lines = [f"### May backend checks: {'passed' if passed else 'failed'}", "",
             f"`{sha}`: the backend job {'passed' if passed else 'failed'}. Its details are not posted: no sealed record "
             "reached the post job (`MAY_RENDERS_RECIPIENT` or `MAY_RENDERS_DECRYPT_KEY` may be unset in "
             "may-native-compile-pr, or the job stopped before its seal)."]
    if url:
        lines += ["", f"Run: {url}"]
    return "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER


def runner_root():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise BackendPostError("Runner temporary directory is unavailable")
    return Path(raw).resolve()


def seal(sha):
    """The backend job's outcome and failures, checked and sealed for the post job (sealed.py)."""
    require_step_secrets(())
    if not SHA.fullmatch(sha or ""):
        raise BackendPostError("Source SHA is not an exact lowercase commit SHA")
    root = runner_root()
    read_record(root / PRIVATE)
    # A sanity check only: the pull request's own code could have changed the checkout. The pull request's own head,
    # read again by the post job, is the check that counts.
    if head_revision(root / SOURCE) != sha:
        raise BackendPostError("The tested checkout is not the dispatched commit; nothing was sealed")
    if sealed.seal(root, sha, (OUTCOME, FAILURES)):
        print("May backend checks: their record is encrypted for the post job")
    else:
        print("May backend checks: MAY_RENDERS_RECIPIENT is not set in may-native-compile-pr; nothing is sealed, so "
              "nothing will be posted")


def verdict():
    """The backend job's last word: the step summary (verdicts and counts), and a failure when the checks failed."""
    require_step_secrets(())
    outcome, failed = read_record(runner_root() / PRIVATE)
    write_summary(summary_lines(outcome, failed))
    if outcome["result"] == "passed":
        print("May backend checks passed")
        return 0
    print("May backend checks failed; which tests goes to the may-app pull request only")
    return 1


def open_record(sha):
    require_step_secrets((sealed.DECRYPT_KEY,))
    if sealed.open_sealed(runner_root(), sha, (OUTCOME, FAILURES)):
        print("May backend checks: their record is decrypted")
    else:
        print("May backend checks: no sealed record from this run attempt; nothing will be posted")


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    if not SHA.fullmatch(sha or ""):
        raise BackendPostError("Source SHA is not an exact lowercase commit SHA")
    number = pull_number(pr)
    private = runner_root() / PRIVATE
    sealed = (private / OUTCOME).is_file()
    if sealed:
        outcome, failed = read_record(private)
        passed = outcome["result"] == "passed"
        total, errors = failed["total"], failed["typecheck_total"]
    else:
        passed = RESULTS.get(os.environ.get(JOB_RESULT, ""))
        if passed is None:
            print("May backend checks: no record and no backend result to post; nothing posted")
            return 0
    if not token:
        print("May backend checks: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")
        return 0 if passed else 1
    if len(token) < 20:
        raise BackendPostError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    url = run_url(os.environ)
    body = comment_body(sha, url, outcome, failed) if sealed else result_body(sha, url, passed)
    if holds(body, token):
        raise BackendPostError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise BackendPostError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise BackendPostError("The pull request is no longer open at the tested head; nothing was posted") from None
    if not post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May backend checks' result already posted for this head on may-app #{number}; nothing posted again")
        return 0 if passed else 1
    if not sealed and passed:
        print(f"May backend checks passed; the result alone posted to may-app #{number} (no sealed record: no details)")
        return 0
    if not sealed:
        print(f"May backend checks failed; the result alone posted to may-app #{number} (no sealed record: no details)")
        return 1
    if passed:
        print(f"May backend checks passed; result posted to may-app #{number}")
        return 0
    print(f"May backend checks failed; {int(total)} failing tests and {int(errors)} type errors posted to may-app #{number}")
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
        raise SystemExit("May backend post denied: " + denial.args[0]) from None
    except (BackendPostError, SealedError, CompilePrError, DsCompileError, StoreError) as error:
        raise SystemExit("May backend post stopped: " + error.args[0]) from None
    except (PrepareError, OSError):
        raise SystemExit("May backend post stopped; no may-app source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May backend post stopped; check may-native-compile-pr, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
