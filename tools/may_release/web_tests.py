"""May's web checks at an open may-app pull request head: pass or fail per stage, and test counts.

may-app's own Actions are switched off (private Actions are blocked by billing), so web/ is checked here, on a free
public Linux runner, at the head prepare.py fetched (--pr-number --web): the pinned Node 22.22.0 (toolchain.py, which
web/package.json's engines allow), in STAGES order:
- toolchain: Node is exactly v22.22.0 and its npm runs (a check of this runner: it fails the step, as backend_unit.py);
- design-system: `npm run fetch-ds` in web/, may-app's own fetch-design-system.sh. prepare.py --web has already put the
  design system's web pin at web/build-deps/may-design-system, so the script fetches nothing: it checks that checkout is
  exactly the pin and clean, then runs `npm ci` and `npm run build` in its web/ folder;
- install: `npm ci --no-audit --no-fund` in web/, with npm's cache under $RUNNER_TEMP/may-npm;
- tests: `npx --no-install vitest run`, with vitest's json reporter writing to a private file, for the counts;
- build: `npm run build` (tsc --noEmit, the Vite builds, the landing prerender and check-build.mjs).
A design-system or install stage that fails stops the stages after it; failed tests still run the build, so one red
stage does not hide another.

This repository is public, so nothing the web app, the design system, their dependencies or their tests print reaches
this log: every tool writes to a private log on the runner. Public output is one fixed line per stage (its name from
STAGES and a fixed verdict) and one line of test counts from vitest's own json report: tests, passed, failed and
skipped (skipped, pending and todo). Never a test name, message or path.

With --record-outcome (web-tests-pr.yml), a red stage does not fail this step. The outcome and the detail (each failing
test's name, the first line of its error and its file, repo-relative; tsc's errors from the build; and, for a stage
with neither, the last lines of its log, cleaned) go to private files (OUTCOME, FAILURES), and web_tests_post.py, the
next step, posts them on the may-app pull request and then fails the job. Every check before the stages (secrets, the
prepared checkout, the pinned Node) still fails here. The detail is never printed.
"""

import argparse
import json
import os
from pathlib import Path
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_unit import clean
from backend_unit import TSC_ERROR, log_text, pinned_node_bin, quiet, tail, value, BackendTestsError
from store import StoreError, child_env, prepared, require_step_secrets, runner_root

# Stage name -> its private log, under may-private. Order is the run and printed order.
STAGES = {"toolchain": "web-toolchain.log", "design-system": "web-design-system.log", "install": "web-install.log",
          "tests": "web-tests.log", "build": "web-build.log"}
NODE_VERSION = "v22.22.0"
DS_SECONDS = 10 * 60
INSTALL_SECONDS = 10 * 60
TEST_SECONDS = 15 * 60
BUILD_SECONDS = 10 * 60
# vitest's json reporter's file, under may-private: read for the counts and the failing tests, never printed.
REPORT = "web-vitest.json"
# --record-outcome's private files, under may-private, for web_tests_post.py.
OUTCOME, FAILURES = "web-tests-result.json", "web-tests-failures.json"
MAX_FAILURES = 100
MAX_NAME, MAX_MESSAGE = 200, 240
MAX_REPORT_BYTES = 32 * 1024 * 1024
VERDICTS = ("passed", "failed", "timeout", "not run")
TAILED = ("design-system", "install", "tests", "build")


class WebTestsError(ValueError):
    pass


def count(value):
    return type(value) is int and 0 <= value <= 10_000_000


def vitest_report(path):
    """vitest's json report as a dict, or None when it is missing, linked, too large or not its shape."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(report, dict) or not all(count(report.get(k)) for k in
                                               ("numTotalTests", "numPassedTests", "numFailedTests")):
        return None
    return report


def test_counts(report):
    """(tests, passed, failed, skipped) from vitest's json report, or None without one. skipped counts pending and
    todo; a file that failed to load (a suite with no tests) is not a test, so it shows only in the detail."""
    if report is None:
        return None
    skipped = sum(report.get(k) for k in ("numPendingTests", "numTodoTests") if count(report.get(k)))
    return report["numTotalTests"], report["numPassedTests"], report["numFailedTests"], skipped


def failed_files(report):
    """How many test files failed with no failing test inside them (a file that could not load or import)."""
    if report is None:
        return 0
    return sum(1 for f in report.get("testResults") or [] if isinstance(f, dict) and f.get("status") == "failed" and
               not any(isinstance(a, dict) and a.get("status") == "failed" for a in f.get("assertionResults") or []))


def first_line(text):
    return next((line.strip() for line in str(text).splitlines() if line.strip()), "")


def relative(name, source):
    """A test file's path under may-app, or its bare name; never an absolute runner path."""
    path = str(name or "")
    for root in (str(source.resolve()), str(source)):
        if path.startswith(root + "/"):
            return path[len(root) + 1:]
    return path


def failing_tests(report, source, roots):
    """(failures, total): each failing test, and each test file that failed to run, in report order, at most
    MAX_FAILURES of them, as {test, message, at}."""
    failures, total = [], 0
    for result in (report or {}).get("testResults") or []:
        if not isinstance(result, dict):
            continue
        at = clean(relative(result.get("name"), source), roots, MAX_NAME) or None
        assertions = [a for a in result.get("assertionResults") or []
                      if isinstance(a, dict) and a.get("status") == "failed"]
        if not assertions and result.get("status") == "failed":
            total += 1
            if len(failures) < MAX_FAILURES:
                failures.append({"test": "(the file did not run)",
                                 "message": clean(first_line(result.get("message") or ""), roots, MAX_MESSAGE),
                                 "at": at})
        for entry in assertions:
            total += 1
            if len(failures) < MAX_FAILURES:
                messages = entry.get("failureMessages") or []
                failures.append({"test": clean(str(entry.get("fullName") or entry.get("title") or ""), roots, MAX_NAME),
                                 "message": clean(first_line(messages[0] if messages else ""), roots, MAX_MESSAGE),
                                 "at": at})
    return failures, total


def type_errors(text, roots):
    """(errors, total): tsc's error lines from the build, at most MAX_FAILURES of them, as {at, code, message}; paths
    under web/."""
    errors, total = [], 0
    for line in text.splitlines():
        match = TSC_ERROR.fullmatch(line.strip())
        if not match:
            continue
        total += 1
        if len(errors) < MAX_FAILURES:
            path = match.group(1) if match.group(1).startswith("/") else "web/" + match.group(1)
            errors.append({"at": clean(f"{path}:{int(match.group(2))}:{int(match.group(3))}", roots, MAX_NAME),
                           "code": match.group(4), "message": clean(match.group(5), roots, MAX_MESSAGE)})
    return errors, total


def check_toolchain(env, log):
    """Node is exactly NODE_VERSION and its npm runs; refuses otherwise."""
    if value(["node", "--version"], env) != NODE_VERSION:
        raise WebTestsError("Node is not the pinned v22.22.0; nothing ran")
    if quiet(["npm", "--version"], log, env=env, timeout=60) != 0:
        raise WebTestsError("The pinned Node's npm does not run; nothing ran")


def stage_verdict(status):
    return "timeout" if status is None else "failed" if status else "passed"


def run_checks(source_sha, record=False):
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    web = source / "web"
    lock, ds = web / "package-lock.json", web / "build-deps/may-design-system"
    if web.is_symlink() or not web.is_dir() or lock.is_symlink() or not lock.is_file() or ds.is_symlink() or \
            not (ds / ".git").is_dir():
        raise WebTestsError("The pull request has no prepared web/ and design system; nothing ran")
    if record:
        for name in (OUTCOME, FAILURES):
            (private / name).unlink(missing_ok=True)
    logs = {stage: private / name for stage, name in STAGES.items()}
    report_path = private / REPORT
    report_path.unlink(missing_ok=True)
    try:
        node_bin = pinned_node_bin()
    except BackendTestsError:
        raise WebTestsError("The pinned Node is not installed; nothing ran") from None
    env = child_env(PATH=str(node_bin) + os.pathsep + os.environ.get("PATH", ""), CI="true",
                    npm_config_cache=str(root / "may-npm"), npm_config_update_notifier="false",
                    npm_config_fund="false", npm_config_audit="false", GIT_TERMINAL_PROMPT="0",
                    GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    check_toolchain(env, logs["toolchain"])
    stages = dict.fromkeys(STAGES, "not run")
    stages["toolchain"] = "passed"
    counts, report = None, None
    stages["design-system"] = stage_verdict(quiet(["npm", "run", "fetch-ds"], logs["design-system"], cwd=web, env=env,
                                            timeout=DS_SECONDS))
    if stages["design-system"] == "passed":
        stages["install"] = stage_verdict(quiet(["npm", "ci", "--no-audit", "--no-fund"], logs["install"], cwd=web, env=env,
                                          timeout=INSTALL_SECONDS))
    if stages["install"] == "passed":
        status = quiet(["npx", "--no-install", "vitest", "run", "--reporter=default", "--reporter=json",
                        "--outputFile.json=" + str(report_path)], logs["tests"], cwd=web, env=env, timeout=TEST_SECONDS)
        report = vitest_report(report_path)
        counts = test_counts(report)
        stages["tests"] = "timeout" if status is None else \
            "passed" if not status and counts and counts[0] and not counts[2] and not failed_files(report) else "failed"
        stages["build"] = stage_verdict(quiet(["npm", "run", "build"], logs["build"], cwd=web, env=env, timeout=BUILD_SECONDS))
    if not record:
        report_lines(stages, counts)
        return
    try:
        report_lines(stages, counts)
        passed = True
    except WebTestsError:
        passed = False
    record_outcome(private, source, logs, stages, counts, report, passed)
    print("May web checks finished; any failures stay in this runner's private files for the next step")


def record_outcome(private, source, logs, stages, counts, report, passed):
    roots = [str(source), str(source.resolve())]
    failures, total = failing_tests(report, source, roots)
    errors, error_total = type_errors(log_text(logs["build"]), roots)
    tails = {}
    for stage in TAILED:
        if stages[stage] in ("failed", "timeout") and not (stage == "build" and errors) and \
                not (stage == "tests" and failures):
            tails[stage] = tail(log_text(logs[stage]), roots)
    (private / FAILURES).write_text(json.dumps({"failures": failures, "total": total, "typecheck": errors,
                                                "typecheck_total": error_total, "tails": tails}))
    (private / OUTCOME).write_text(json.dumps({
        "result": "passed" if passed else "failed", "stages": stages,
        "tests": list(counts) if counts is not None else None}))


def report_lines(stages, counts):
    """One fixed line per stage and one of test counts, then pass only if every stage passed with at least one test."""
    for name in STAGES:
        verdict = stages[name]
        if verdict == "timeout":
            print(f"May web {name}: did not finish in time")
        elif verdict == "not run":
            print(f"May web {name}: did not run (an earlier stage stopped it)")
        else:
            print(f"May web {name}: {verdict}")
    if counts is None:
        print("May web tests: no test summary (vitest stopped before it reported)")
    else:
        tests, passed, failed, skipped = (int(n) for n in counts)
        print(f"May web tests: {int(tests)} tests, {int(passed)} passed, {int(failed)} failed, {int(skipped)} skipped")
    if any(stages[name] != "passed" for name in STAGES):
        raise WebTestsError("At least one May web check did not pass")
    print("Every May web check passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--record-outcome", action="store_true")
    args = parser.parse_args()
    try:
        run_checks(args.source_sha, args.record_outcome)
    except (WebTestsError, StoreError) as error:
        raise SystemExit("May web checks: " + error.args[0]) from None
    except Exception:
        # Any other failure, whatever its type or message, prints only this.
        raise SystemExit("May web checks failed; no private detail was published") from None
