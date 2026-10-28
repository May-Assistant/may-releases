#!/usr/bin/env python3
"""The design system's own test suites at a may-design-system pull request head, with the result posted on that pull
request only (ds-tests-pr.yml).

Samuel's decision (27-28 Oct): every may-design-system pull request into claude/may-integration or main runs both of
its suites here, on GitHub's runners, so no local Mac has to: the Swift package's `xcodebuild test` on an iPhone 16 Pro
simulator (Xcode 26.3, macos-15) and Android's `./gradlew :components:testDebugUnitTest` (the pinned Temurin JDK 21,
ubuntu-24.04). Each suite's pass or fail and counts, and its failing tests, go as one comment on that private pull
request. This repository is public, and so are its logs, step summaries and artifacts; may-design-system is private.
So nothing derived from the design system (a test, file, class or scheme name, a message, a log line) is printed,
summarised or uploaded here in the clear: every line this script prints is fixed, apart from the dispatch's own
commit and pull request number and counts (static_check.py).

The suites run the pull request's own code as the runner user, so their jobs hold no secret but the read-only deploy
key in the fetch (ds_compile.py fetch), and never a token: each seals its record to the environment's own age key and
hands over only that; a fresh post job opens both and posts (the split of may-releases #133).

  swift    (macos-15, no secret) the package's -Package scheme, `xcodebuild test` on the simulator; the result bundle's
           summary (xcresulttool) is reduced to counts and the failing tests, in a private file.
  android  (ubuntu-24.04, no secret) `./gradlew --no-daemon --continue :components:testDebugUnitTest`; its JUnit
           reports are reduced the same way.
  seal     (the same job, no secret) that suite's record ({sha, suite, ran, passed, total, failed, skipped, failures})
           padded to MAX_RECORD and age-encrypted to MAY_RENDERS_RECIPIENT; without the recipient nothing is sealed.
  verdict  (the same job, no secret) fails the job when the suite did not pass, whether or not the post job can post.
  open     (post job, MAY_RENDERS_DECRYPT_KEY alone) decrypts each sealed record after checking the key against the
           recipient, and checks its shape and commit before anything reads it.
  status   (post job, MAY_DS_STATUS_TOKEN alone, before post) sets the head commit's "may-ds-tests" status: success
           when both suites passed, failure otherwise (error when neither finished), with counts alone, so a merge can
           gate on one check (coordinator, 28 Oct).
  post     (post job, MAY_DS_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts one comment for both
           suites on the pull request, after it is read again and is still open at that head, unless a comment already
           carries this head's marker. A suite whose record never arrived is reported from its job's result alone
           (MAY_SWIFT_RESULT, MAY_ANDROID_RESULT: success, failure). It fails when a suite did not pass.
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

from android_unit import UnitTestsError, clean, install_sdk, parsed_report, pinned_java_home, sdk_home
from compile import credentials_left
from ds_compile import (DEVELOPER, PACKAGES, PRIVATE, SOURCE, XCODE, DsCompileError, build_env, fence_for, github,
                        post_once, request, run_url, runner_root, schemes_of)
from gate import AGE_RECIPIENT, DS_REPO, GateDenial, pull_number, validate_ds_pull_request
from prepare import PrepareError, head_revision
from renders import DEVICE, RendersError, quiet, simulator
from review_mcp import FOOTER, holds
from store import StoreError, child_env, require_step_secrets, write_private
import toolchain

COMMENT_TOKEN = "MAY_DS_PR_COMMENT_TOKEN"
STATUS_TOKEN = "MAY_DS_STATUS_TOKEN"
# The one call status makes: a commit status on a design-system commit.
STATUS_CALL = r"/repos/May-Assistant/may-design-system/statuses/[0-9a-f]{40}"
CONTEXT = "may-ds-tests"
RECIPIENT, DECRYPT_KEY = "MAY_RENDERS_RECIPIENT", "MAY_RENDERS_DECRYPT_KEY"
AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[02-9AC-HJ-NP-Z]{58}")
SHA = re.compile(r"[0-9a-f]{40}")
SUITES = ("swift", "android")
RESULT_ENV = {"swift": "MAY_SWIFT_RESULT", "android": "MAY_ANDROID_RESULT"}
DERIVED = "may-ds-tests-derived"
SEALED, DOWNLOAD = "may-ds-tests-sealed", "may-ds-tests-download"
# Every sealed record is padded to MAX_RECORD bytes, so its size says nothing about how many tests failed.
MAX_RECORD = 262144  # 256 KiB
MAX_FAILURES, MAX_NAME, MAX_MESSAGE, MAX_COUNT = 100, 200, 240, 1_000_000
TEST_SECONDS = 45 * 60
GRADLE_TASK = ":components:testDebugUnitTest"
REPORTS = "components/build/test-results/testDebugUnitTest"
# The suites' sizes at b0e9c28 (coordinator, 27 Oct): fewer tests than this adds a warning line, never a failure.
BASELINE_SHA = "b0e9c28"
BASELINES = {"swift": 283, "android": 1533}
MARKER = "may-ds-tests"
TITLES = {"swift": f"Swift (`xcodebuild test`, Xcode {XCODE}, {DEVICE} simulator)",
          "android": f"Android (`{GRADLE_TASK}`, JDK 21)"}


class DsTestsError(ValueError):
    pass


def exact(sha):
    if not SHA.fullmatch(sha or ""):
        raise DsTestsError("Design-system SHA is not an exact lowercase commit SHA")
    return sha


def checked_record(data, sha, suite):
    """A suite's record, checked whole; refuses otherwise."""
    count = lambda v: type(v) is int and 0 <= v <= MAX_COUNT
    if not (isinstance(data, dict) and set(data) == {"sha", "suite", "ran", "passed", "total", "failed", "skipped",
                                                     "failures"} and data["sha"] == sha and data["suite"] == suite and
            type(data["ran"]) is bool and type(data["passed"]) is bool and all(count(data[k]) for k in
                                                                               ("total", "failed", "skipped")) and
            data["failed"] + data["skipped"] <= data["total"] and isinstance(data["failures"], list) and
            len(data["failures"]) <= MAX_FAILURES and len(data["failures"]) <= data["failed"] and
            all(isinstance(f, dict) and set(f) == {"name", "message"} and isinstance(f["name"], str) and
                0 < len(f["name"]) <= MAX_NAME and isinstance(f["message"], str) and len(f["message"]) <= MAX_MESSAGE and
                "\n" not in f["name"] + f["message"] for f in data["failures"]) and
            (not data["passed"] or (data["ran"] and data["total"] > 0 and data["failed"] == 0))):
        raise DsTestsError("A suite record is malformed or for another commit; nothing was posted")
    return data


def prepared():
    """(root, checkout, private) once the exact commit is fetched and no read key or host file is left on disk."""
    root = runner_root()
    checkout, private = root / SOURCE, root / PRIVATE
    if not checkout.is_dir() or not private.is_dir():
        raise DsTestsError("The exact design-system commit is not fetched")
    if credentials_left(private):
        raise DsTestsError("A read key or host file is still on disk; no suite ran")
    return root, checkout, private


def write_record(private, record):
    (private / f"{record['suite']}.json").unlink(missing_ok=True)
    (private / f"{record['suite']}.json").write_text(json.dumps(record))


def record_of(sha, suite, ran, total, failed, skipped, failures):
    passed = ran and total > 0 and failed == 0
    return checked_record({"sha": sha, "suite": suite, "ran": ran, "passed": passed, "total": total, "failed": failed,
                           "skipped": skipped, "failures": failures[:min(MAX_FAILURES, failed)]}, sha, suite)


# ---- swift ----

def swift_summary(data, roots):
    """(total, failed, skipped, failures) from `xcresulttool get test-results summary`, or None."""
    if not isinstance(data, dict):
        return None
    numbers = [data.get(k) for k in ("totalTestCount", "failedTests", "skippedTests")]
    if not all(type(n) is int and 0 <= n <= MAX_COUNT for n in numbers) or numbers[1] + numbers[2] > numbers[0]:
        return None
    failures = []
    for entry in data.get("testFailures") if isinstance(data.get("testFailures"), list) else []:
        if not isinstance(entry, dict) or len(failures) == MAX_FAILURES:
            continue
        name = clean(f"{entry.get('targetName') or ''} > {entry.get('testName') or ''}", roots, MAX_NAME)
        text = entry.get("failureText") if isinstance(entry.get("failureText"), str) else ""
        failures.append({"name": name or "unnamed test", "message": clean(text.splitlines()[0] if text.strip() else "",
                                                                           roots, MAX_MESSAGE)})
    return numbers[0], numbers[1], numbers[2], failures


def run_swift(sha, run=subprocess.run, find=simulator):
    # The suite runs the design system's own code (SwiftPM, macros, plugins, tests) and holds no secret at all.
    require_step_secrets(())
    exact(sha)
    root, checkout, private = prepared()
    if head_revision(checkout) != sha:
        raise DsTestsError("The checkout is not the exact design-system commit")
    if not DEVELOPER.is_dir():
        raise DsTestsError("Pinned Xcode 26.3 is unavailable")
    (private / "tmp").mkdir(mode=0o700, exist_ok=True)
    env, log, bundle = build_env(root), private / "swift.log", private / "swift.xcresult"
    version = run(["xcodebuild", "-version"], capture_output=True, stdin=subprocess.DEVNULL, env=env)
    if version.returncode or version.stdout.splitlines()[:1] != [b"Xcode " + XCODE.encode()]:
        raise DsTestsError("Pinned Xcode version differs")
    summary = None
    try:
        udid = find(log, env)
        with log.open("ab") as output, (private / "schemes.json").open("wb") as listed:
            done = run(["xcodebuild", "-list", "-json"], cwd=checkout, env=env, stdout=listed, stderr=output,
                       stdin=subprocess.DEVNULL, timeout=600)
        schemes = [s for s in schemes_of((private / "schemes.json").read_text(errors="replace"))
                   if s.endswith("-Package")] if not done.returncode else []
        if len(schemes) == 1:
            with log.open("ab") as output:
                run(["xcodebuild", "test", "-scheme", schemes[0], "-destination", "id=" + udid,
                     "-derivedDataPath", str(root / DERIVED), "-clonedSourcePackagesDirPath", str(root / PACKAGES),
                     "-resultBundlePath", str(bundle), "-skipMacroValidation", "-skipPackagePluginValidation",
                     "CODE_SIGNING_ALLOWED=NO"], cwd=checkout, env=env, stdout=output, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, timeout=TEST_SECONDS)
            read = run(["xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(bundle)],
                       capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=600)
            summary = swift_summary(json.loads(read.stdout or b"null"), [str(checkout), str(checkout.resolve())]) \
                if not read.returncode else None
    except (subprocess.SubprocessError, OSError, ValueError, RendersError):
        summary = None
    total, failed, skipped, failures = summary or (0, 0, 0, [])
    record = record_of(sha, "swift", summary is not None, total, failed, skipped, failures)
    write_record(private, record)
    report(record)


# ---- android ----

def junit_results(folder, roots):
    """(total, failed, skipped, failures) from the JUnit reports in folder, or None when it holds none it can read."""
    if folder.is_symlink() or not folder.is_dir():
        return None
    total = failed = skipped = 0
    failures, read = [], 0
    for report_file in sorted(folder.iterdir()):
        if not (report_file.name.startswith("TEST-") and report_file.name.endswith(".xml")):
            continue
        tree = parsed_report(report_file)
        if tree is None:
            continue
        read += 1
        for case in tree.iter("testcase"):
            total += 1
            problem = next((child for child in case if child.tag in ("failure", "error")), None)
            if any(child.tag == "skipped" for child in case):
                skipped += 1
            elif problem is not None:
                failed += 1
                if len(failures) < MAX_FAILURES:
                    message = problem.get("message") or next((l for l in (problem.text or "").splitlines() if l.strip()), "")
                    failures.append({"name": clean(f"{case.get('classname') or ''} > {case.get('name') or ''}", roots,
                                                   MAX_NAME) or "unnamed test",
                                     "message": clean(message.splitlines()[0] if message.strip() else "", roots,
                                                      MAX_MESSAGE)})
    return (min(total, MAX_COUNT), min(failed, MAX_COUNT), min(skipped, MAX_COUNT), failures) if read else None


def run_android(sha, tool=quiet, sdk=install_sdk):
    require_step_secrets(())
    exact(sha)
    root, checkout, private = prepared()
    if head_revision(checkout) != sha:
        raise DsTestsError("The checkout is not the exact design-system commit")
    reports = checkout / REPORTS
    if reports.exists() or reports.is_symlink():
        raise DsTestsError("The pull request carries test results of its own; no suite ran")
    gradlew = checkout / "gradlew"
    log = private / "android.log"
    results = None
    if gradlew.is_file() and not gradlew.is_symlink():
        try:
            java_home, home = pinned_java_home(), sdk_home()
            env = child_env(JAVA_HOME=str(java_home), PATH=str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
                            ANDROID_HOME=str(home), GRADLE_USER_HOME=str(root / "may-ds-gradle"), GIT_TERMINAL_PROMPT="0",
                            JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=" + str(root))
            sdk(home, log, env)
            tool(["bash", "./gradlew", "--no-daemon", "--continue", GRADLE_TASK], log, cwd=checkout, env=env,
                 timeout=TEST_SECONDS)
        except (subprocess.SubprocessError, OSError, UnitTestsError):
            pass
        results = junit_results(reports, [str(checkout), str(checkout.resolve())])
    total, failed, skipped, failures = results or (0, 0, 0, [])
    record = record_of(sha, "android", results is not None, total, failed, skipped, failures)
    write_record(private, record)
    report(record)


def report(record):
    """One fixed line: the suite and its counts, never a name or a message."""
    suite = record["suite"]
    if not record["ran"]:
        print(f"May DS tests {suite}: did not run (the build or the tool stopped before a result)")
        return
    verdict = "passed" if record["passed"] else "failed"
    total, failed, skipped = int(record["total"]), int(record["failed"]), int(record["skipped"])
    print(f"May DS tests {suite}: {verdict}, {total} tests, {failed} failed, {skipped} skipped")


# ---- seal, verdict, open ----

def suite_of(suite):
    if suite not in SUITES:
        raise DsTestsError("The suite is neither swift nor android")
    return suite


def seal(sha, suite):
    require_step_secrets(())
    exact(sha)
    suite_of(suite)
    root = runner_root()
    private = root / PRIVATE
    try:
        record = checked_record(json.loads((private / f"{suite}.json").read_text()), sha, suite)
    except (OSError, ValueError):
        raise DsTestsError("The suite left no record; nothing was sealed") from None
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("May DS tests: MAY_RENDERS_RECIPIENT is not set in may-ds-renders; nothing is sealed, so only the result "
              "will be posted")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise DsTestsError("MAY_RENDERS_RECIPIENT is not an age public key")
    data = json.dumps(record).encode()
    if len(data) > MAX_RECORD:
        raise DsTestsError("The suite record is too large to seal")
    plain, sealed = private / f"{suite}.plain", root / SEALED
    sealed.mkdir(mode=0o700, exist_ok=True)
    try:
        plain.unlink(missing_ok=True)
        write_private(plain, data + b" " * (MAX_RECORD - len(data)))
        if subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(sealed / f"{suite}.age"),
                           str(plain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=child_env()).returncode:
            raise DsTestsError("The suite record could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print(f"May DS tests {suite}: its record is encrypted for the post job")


def verdict(suite):
    require_step_secrets(())
    suite_of(suite)
    try:
        record = json.loads((runner_root() / PRIVATE / f"{suite}.json").read_text())
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict) and record.get("passed") is True:
        return 0
    print(f"May DS tests {suite}: did not pass; its failing tests go to the design-system pull request only")
    return 1


def open_records(sha):
    require_step_secrets((DECRYPT_KEY,))
    lines = [l.strip() for l in os.environ.pop(DECRYPT_KEY, "").splitlines() if l.strip() and not l.startswith("#")]
    exact(sha)
    root = runner_root()
    found = [s for s in SUITES if (root / DOWNLOAD / s / f"{s}.age").is_file() and
             not (root / DOWNLOAD / s / f"{s}.age").is_symlink()]
    if not found:
        print("May DS tests: no sealed record reached the post job; only the suites' results will be posted")
        return
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise DsTestsError("MAY_RENDERS_RECIPIENT is not set to an age public key in may-ds-renders")
    if len(lines) != 1 or not AGE_IDENTITY.fullmatch(lines[0]):
        raise DsTestsError("The decryption key is malformed (MAY_RENDERS_DECRYPT_KEY in may-ds-renders)")
    private = root / PRIVATE
    private.mkdir(mode=0o700, exist_ok=True)
    key, env = private / "ds-tests.key", child_env()
    try:
        write_private(key, (lines[0] + "\n").encode())
        derived = subprocess.run([toolchain.tool_path("age", "age-keygen"), "-y", str(key)], capture_output=True,
                                 stdin=subprocess.DEVNULL, env=env)
        if derived.returncode or derived.stdout.decode().strip() != recipient:
            raise DsTestsError("The decryption key does not match MAY_RENDERS_RECIPIENT")
        for suite in found:
            plain = private / f"{suite}.part"
            try:
                plain.unlink(missing_ok=True)
                write_private(plain, b"")
                if subprocess.run([toolchain.tool_path("age", "age"), "-d", "-i", str(key), "-o", str(plain),
                                   str(root / DOWNLOAD / suite / f"{suite}.age")], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env).returncode or \
                        plain.stat().st_size != MAX_RECORD:
                    raise DsTestsError("A suite record failed to decrypt or authenticate")
                try:
                    record = checked_record(json.loads(plain.read_text()), sha, suite)
                except (ValueError, RecursionError):
                    raise DsTestsError("A suite record is malformed or for another commit; nothing was posted") from None
                write_record(private, record)
            finally:
                plain.unlink(missing_ok=True)
    finally:
        key.unlink(missing_ok=True)
    print(f"May DS tests: {len(found)} suite records decrypted and checked")


# ---- post ----

def comment_body(sha, url, records, results):
    """The one comment: both suites' results and counts, each failing test, and a warning when a suite ran fewer tests
    than its baseline. records holds the opened records; results each job's own result, for a suite with none."""
    passed = all((records[s]["passed"] if s in records else results.get(s) == "success") for s in SUITES)
    lines = [f"### May design-system tests: {'passed' if passed else 'failed'}", "",
             f"At `{sha}`.", "", "| Suite | Result | Tests | Failed | Skipped |", "| --- | --- | --- | --- | --- |"]
    details = []
    for suite in SUITES:
        record = records.get(suite)
        if record is None:
            state = {"success": "passed", "failure": "failed"}.get(results.get(suite), "no result")
            lines.append(f"| {TITLES[suite]} | {state} (no detail reached the post job) | | | |")
            continue
        state = "passed" if record["passed"] else "failed" if record["ran"] else "did not run"
        lines.append(f"| {TITLES[suite]} | {state} | {int(record['total'])} | {int(record['failed'])} | "
                     f"{int(record['skipped'])} |")
        if record["ran"] and record["total"] < BASELINES[suite]:
            details.append(f"> **Warning:** {suite} ran {int(record['total'])} tests, fewer than the "
                           f"{BASELINES[suite]} at `{BASELINE_SHA}`.")
        if record["failures"]:
            entries = [f"{f['name']}: {f['message']}" if f["message"] else f["name"] for f in record["failures"]]
            fence = fence_for(entries)
            more = int(record["failed"]) - len(record["failures"])
            details += ["", f"**{suite} failing tests**" + (f" (first {len(entries)}; {more} more)" if more else ""),
                        "", fence, *entries, fence]
    lines += details and [""] + details
    if url:
        lines += ["", f"Run: {url}"]
    return "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER


def gathered(sha):
    """(records, results, passed, finished): the opened records, each suite job's own result, whether both suites
    passed, and whether either finished at all."""
    private = runner_root() / PRIVATE
    records = {}
    for suite in SUITES:
        path = private / f"{suite}.json"
        if path.is_file() and not path.is_symlink():
            try:
                records[suite] = checked_record(json.loads(path.read_text()), sha, suite)
            except (OSError, ValueError, RecursionError):
                raise DsTestsError("A suite record is malformed or for another commit; nothing was posted") from None
    results = {suite: os.environ.get(RESULT_ENV[suite], "") for suite in SUITES}
    passed = all((records[s]["passed"] if s in records else results[s] == "success") for s in SUITES)
    finished = bool(records) or any(results[s] in ("success", "failure") for s in SUITES)
    return records, results, passed, finished


def status_api(method, path, payload, token):
    """The one call status makes; anything else is refused before a request is made."""
    if method != "POST" or not re.fullmatch(STATUS_CALL, path):
        raise DsTestsError("Only a design-system commit's status is written")
    return request(method, path, payload, token)


def status_description(records, results):
    parts = []
    for suite in SUITES:
        record = records.get(suite)
        if record is None:
            parts.append(f"{suite} {'passed' if results.get(suite) == 'success' else 'failed'}")
        elif not record["ran"]:
            parts.append(f"{suite} did not run")
        else:
            parts.append(f"{suite} {int(record['total'])} tests, {int(record['failed'])} failed")
    return "; ".join(parts)[:140]


def set_status(sha, call_api=status_api):
    require_step_secrets((STATUS_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(STATUS_TOKEN, "")
    exact(sha)
    records, results, passed, finished = gathered(sha)
    state = "success" if passed and finished else "failure" if finished else "error"
    if not token:
        print("May DS tests: MAY_DS_STATUS_TOKEN is not set in may-ds-renders; no commit status set")
        return 0
    if len(token) < 20:
        raise DsTestsError("The design-system status token is malformed (MAY_DS_STATUS_TOKEN); no status was set")
    payload = {"state": state, "context": CONTEXT,
               "description": status_description(records, results) if finished else "neither suite finished"}
    url = run_url(os.environ)
    if url:
        payload["target_url"] = url
    status, _ = call_api("POST", f"/repos/{DS_REPO}/statuses/{sha}", payload, token)
    if status != 201:
        raise DsTestsError("GitHub did not accept the commit status")
    print(f"May DS tests: the may-ds-tests commit status is {state} on the design-system head")
    return 0


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    exact(sha)
    number = pull_number(pr)
    records, results, passed, finished = gathered(sha)
    if not finished:
        print("May DS tests: neither suite finished (cancelled or skipped); nothing posted")
        return 1
    if not token:
        print("May DS tests: MAY_DS_PR_COMMENT_TOKEN is not set in may-ds-renders; no comment posted")
        return 0 if passed else 1
    if len(token) < 20:
        raise DsTestsError("The design-system comment token is malformed (MAY_DS_PR_COMMENT_TOKEN); nothing was posted")
    body = comment_body(sha, run_url(os.environ), records, results)
    if holds(body, token):
        raise DsTestsError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{DS_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise DsTestsError("The design-system pull request could not be read again; nothing was posted")
    try:
        validate_ds_pull_request(pull, number, sha)
    except GateDenial:
        raise DsTestsError("The pull request is no longer open at the tested head; nothing was posted") from None
    if not post_once(call_api, DS_REPO, number, sha, body, token, MARKER):
        print(f"May DS tests: the result is already posted for this head on may-design-system #{number}")
        return 0 if passed else 1
    print(f"May DS tests {'passed' if passed else 'failed'}; result posted to may-design-system #{number}")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("swift", "android", "seal", "verdict", "open", "status", "post"))
    parser.add_argument("--ds-sha")
    parser.add_argument("--pr-number")
    parser.add_argument("--suite")
    args = parser.parse_args()
    if args.command == "swift":
        return run_swift(args.ds_sha)
    if args.command == "android":
        return run_android(args.ds_sha)
    if args.command == "seal":
        return seal(args.ds_sha, args.suite)
    if args.command == "verdict":
        return verdict(args.suite)
    if args.command == "open":
        return open_records(args.ds_sha)
    if args.command == "status":
        return set_status(args.ds_sha)
    return post(args.ds_sha, args.pr_number)


if __name__ == "__main__":
    try:
        failed = main()
    except GateDenial as denial:
        raise SystemExit("May DS tests denied: " + denial.args[0]) from None
    except (DsTestsError, DsCompileError, StoreError) as error:
        raise SystemExit("May DS tests stopped: " + error.args[0]) from None
    except (PrepareError, OSError):
        raise SystemExit("May DS tests stopped; no design-system source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May DS tests stopped; check may-ds-renders, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
