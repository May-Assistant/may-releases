"""May's Android unit suites at an open may-app pull request head: pass or fail, and counts.

Runs may-app's three Android unit suites (SUITES: the client core's JVM tests, and the app's Robolectric tests for
May Playground and for May production, both debug) on a free public Linux runner, at the head prepare.py fetched
(--pr-number), with the pinned JDK (toolchain.py) and the Android SDK packages the app builds with (SDK_PACKAGES).
android/scripts/fetch-design-system.sh runs first, as on a developer's machine: it rechecks that the design system
prepare.py placed is exactly design-system.lock's revision, clean, and points its build at the runner's SDK.

This repository is public, so nothing the app, its build or its tests print reaches this log: every tool writes to a
private log on the runner. Public output is one fixed line per suite, built only from this file's own words and
integers: the suite's name from SUITES, whether it passed, and its test, failure and skip counts summed from the
JUnit reports' <testsuite> attributes (never a test name, message or class). The job fails unless every suite
reported at least one test, none failed or errored, and Gradle finished.

Test results committed to the pull request are refused before Gradle runs, so a report can only be this run's.

With --record-outcome (android-unit-pr.yml), a suite that fails, errors, ran no test or did not report, and Gradle
failing or timing out, do not fail this step. The outcome, each suite's counts, and each failing test (its class and
method, its kind, the first line of its message and the line of the test's own source it failed at, repo-relative)
go to private files (OUTCOME, FAILURES), and android_unit_post.py, the next step, posts them on the may-app pull
request and then fails the job. Every check before Gradle (secrets, the pinned JDK, the SDK, fetch-design-system.sh,
committed results) still fails here. The failing tests are never printed.

With --core-breakdown true (the dispatch's core_breakdown input), each failing core test is also located by numbers
alone: the 1-based index of its report among the core reports sorted by class name, out of how many, and its
1-based position among that report's test cases. Anyone with the same may-app commit maps them to a class and
method; no name reaches this log, and none goes into a dispatch input either (those are public too).
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from review_mcp import TOKEN_SHAPED
from store import StoreError, child_env, prepared, require_step_secrets, runner_root
import toolchain

# Suite name -> (Gradle task, its JUnit report directory under android/). Order is the printed order.
SUITES = {
    "core": (":core:test", "core/build/test-results/test"),
    "playground": (":app:testPlaygroundDebugUnitTest", "app/build/test-results/testPlaygroundDebugUnitTest"),
    "production": (":app:testProductionDebugUnitTest", "app/build/test-results/testProductionDebugUnitTest"),
}
# platforms;android-37.0 is the app's compileSdk 37 and build-tools 36.0.0 what its Android Gradle Plugin builds
# with: the same pins as flows.py, without the emulator or a system image (unit tests need neither).
SDK_PACKAGES = ("platform-tools", "platforms;android-37.0", "build-tools;36.0.0")
SDK_INSTALL_ATTEMPTS = 2
SDK_RETRY_SECONDS = 30
SDK_INSTALL_SECONDS = 900
GRADLE_SECONDS = 50 * 60
FETCH_DESIGN_SYSTEM = "android/scripts/fetch-design-system.sh"
# Only the head of a report is read: the <testsuite> tag comes right after the XML declaration.
REPORT_HEAD_BYTES = 16 * 1024
MAX_REPORTS = 5000
COUNTS = ("tests", "failures", "errors", "skipped")
# --record-outcome's private files, under may-private, for android_unit_post.py.
OUTCOME, FAILURES = "android-unit-result.json", "android-unit-failures.json"
# Each suite's Gradle module, for a failing test's source path.
MODULES = {"core": "android/core", "playground": "android/app", "production": "android/app"}
# Failing tests recorded (the rest are counted), and the longest name, message and path kept.
MAX_FAILURES = 100
# Failing core test cases located by --core-breakdown.
MAX_BREAKDOWN = 20
MAX_NAME, MAX_MESSAGE = 200, 240
# A report read whole for its failures; a larger one (or one with a DOCTYPE, which Gradle never writes) is counted as
# unreadable rather than parsed.
MAX_REPORT_BYTES = 8 * 1024 * 1024
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# What is left of an absolute path once the checkout's own prefix is taken off: a runner path, never posted.
ABSOLUTE = re.compile(r"(?<![\w.:/~-])/(?:[^\s/:;,'\"()<>\[\]]+/)+[^\s/:;,'\"()<>\[\]]*")
# A stack frame: `at <class>.<method>(<File>.kt:<line>)`.
FRAME = re.compile(r"\s*at ([\w$.]+)\.([\w$<>-]+)\(([\w$-]+\.(?:kt|java)):([0-9]{1,7})\)")


class UnitTestsError(ValueError):
    pass


def quiet(argv, log, *, cwd=None, env=None, timeout=None):
    """The tool's exit status; its output goes to the private log alone."""
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=timeout)
    return result.returncode


def sdk_home():
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home or not Path(home).is_dir():
        raise UnitTestsError("The Android SDK is unavailable on this runner")
    return Path(home)


def sleep(seconds):
    time.sleep(seconds)


def install_sdk(home, log, env):
    manager = home / "cmdline-tools/latest/bin/sdkmanager"
    if not manager.is_file():
        raise UnitTestsError("The Android SDK manager is unavailable on this runner")
    with log.open("ab") as output:
        subprocess.run([str(manager), "--licenses"], input=b"y\n" * 20, stdout=output, stderr=subprocess.STDOUT,
                       env=env, check=False)
    # One retry, as flows.py: a download from Google's SDK repository can fail on a runner before any build.
    for attempt in range(SDK_INSTALL_ATTEMPTS):
        try:
            if not quiet([str(manager), "--install", *SDK_PACKAGES], log, env=env, timeout=SDK_INSTALL_SECONDS):
                return
        except subprocess.TimeoutExpired:
            pass
        if attempt + 1 < SDK_INSTALL_ATTEMPTS:
            sleep(SDK_RETRY_SECONDS)
    raise UnitTestsError("The Android SDK did not install after one retry; no suite ran")


def pinned_java_home():
    """The pinned JDK toolchain.py installed (never the runner's own), by absolute path."""
    try:
        return Path(toolchain.tool_path("jdk", "bin/java")).parents[1]
    except StoreError:
        raise UnitTestsError("The pinned JDK is not installed; no suite ran") from None


def refuse_committed_results(android):
    for _, results in SUITES.values():
        path = android / results
        if path.exists() or path.is_symlink():
            raise UnitTestsError("The pull request carries test results of its own; no suite ran")


def report_counts(text):
    """{tests, failures, errors, skipped} from a JUnit report's <testsuite> tag, or None if it has no such tag with
    each count exactly once as a plain number."""
    tag = re.search(r"<testsuite(\s[^<>]*)>", text)
    if not tag:
        return None
    attributes = re.findall(r'\s([\w:.-]+)="([^"]*)"', tag.group(1))
    counts = {}
    for name, value in attributes:
        if name in COUNTS:
            if name in counts or not re.fullmatch(r"[0-9]{1,6}", value):
                return None
            counts[name] = int(value)
    return counts if set(counts) == set(COUNTS) else None


def suite_result(directory):
    """(tests, failed, skipped) summed over a suite's reports, or None if it left no readable report."""
    if directory.is_symlink() or not directory.is_dir():
        return None
    reports = [p for p in sorted(directory.iterdir()) if p.name.startswith("TEST-") and p.name.endswith(".xml")]
    if not reports or len(reports) > MAX_REPORTS:
        return None
    tests = failed = skipped = 0
    for report in reports:
        if report.is_symlink() or not report.is_file():
            return None
        with report.open("rb") as stream:
            counts = report_counts(stream.read(REPORT_HEAD_BYTES).decode("utf-8", errors="replace"))
        if counts is None:
            return None
        tests += counts["tests"]
        failed += counts["failures"] + counts["errors"]
        skipped += counts["skipped"]
    return tests, failed, skipped


def clean(text, roots, limit):
    """One line of report text as it may be posted on the private pull request: no ANSI or control characters,
    whitespace collapsed, the checkout's own prefix taken off (repo-relative), any other absolute path replaced,
    token-shaped strings redacted, and cut to limit characters."""
    text = " ".join(CONTROL.sub(" ", ANSI.sub("", text or "")).split())
    for root in roots:
        text = text.replace(root.rstrip("/") + "/", "")
    text = TOKEN_SHAPED.sub("[redacted]", ABSOLUTE.sub("[runner path]", text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def source_of(source, suite, test_class, file_name):
    """The failing test's own source file, repo-relative, when exactly one file of that name sits in its package
    folder under the suite module's src/*/kotlin or src/*/java; else None."""
    package = test_class.split("$", 1)[0].rpartition(".")[0].replace(".", "/")
    module = source / MODULES[suite] / "src"
    if not module.is_dir() or module.is_symlink():
        return None
    found = [p for language in ("kotlin", "java") for p in module.glob(f"*/{language}/{package}/{file_name}")
             if p.is_file() and not p.is_symlink()]
    if len(found) != 1:
        return None
    return found[0].relative_to(source).as_posix()


def failure_of(case, problem, source, suite, roots):
    """{suite, test, kind, message, at} for one failing <testcase>: its first message line, and the first stack frame
    in the test's own class, as `path:line` (repo-relative) or `File.kt:line` when the file isn't found."""
    test_class, method = case.get("classname") or "", case.get("name") or ""
    body = problem.text or ""
    message = problem.get("message") or next((line for line in body.splitlines() if line.strip()), "")
    at = None
    for line in body.splitlines():
        frame = FRAME.fullmatch(line)
        if frame and frame.group(1).split("$", 1)[0] == test_class.split("$", 1)[0]:
            path = source_of(source, suite, test_class, frame.group(3))
            at = f"{path or frame.group(3)}:{int(frame.group(4))}"
            break
    return {"suite": suite, "test": clean(f"{test_class} > {method}", roots, MAX_NAME),
            "kind": "error" if problem.tag == "error" else "failure",
            "message": clean(message.splitlines()[0] if message.strip() else "", roots, MAX_MESSAGE),
            "at": clean(at, roots, MAX_NAME) if at else None}


def parsed_report(report):
    """A report's root element, or None when it is too large, linked, or not plain JUnit XML."""
    if report.is_symlink() or not report.is_file() or report.stat().st_size > MAX_REPORT_BYTES:
        return None
    text = report.read_text(encoding="utf-8", errors="replace")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None


def core_breakdown(source):
    """[(index, count, case, cases)] for each failing core test case, at most MAX_BREAKDOWN: index is its report's
    1-based place among the core reports sorted by class name, out of count; case its 1-based place among that
    report's test cases, out of cases. An unreadable report is left out of the order, so count says how many were read."""
    folder = source / "android" / SUITES["core"][1]
    if folder.is_symlink() or not folder.is_dir():
        return []
    trees = [parsed_report(r) for r in sorted(folder.iterdir()) if r.name.startswith("TEST-") and r.name.endswith(".xml")]
    trees = sorted((tree for tree in trees if tree is not None), key=lambda tree: tree.get("name") or "")
    found = []
    for index, tree in enumerate(trees, 1):
        cases = list(tree.iter("testcase"))
        for case, element in enumerate(cases, 1):
            if any(child.tag in ("failure", "error") for child in element) and len(found) < MAX_BREAKDOWN:
                found.append((index, len(trees), case, len(cases)))
    return found


def print_breakdown(found):
    for index, count, case, cases in found:
        print(f"May Android unit suite core: report {int(index)} of {int(count)} by class name, "
              f"test case {int(case)} of {int(cases)} failed")
    if not found:
        print("May Android unit suite core: no failing test case to locate")


def failing_tests(source, roots):
    """(failures, total, unreadable): the failing tests of every suite's reports, in report order, at most
    MAX_FAILURES of them; total counts all; unreadable counts reports too large, linked, or not plain JUnit XML."""
    failures, total, unreadable = [], 0, 0
    for suite, (_, directory) in SUITES.items():
        folder = source / "android" / directory
        if folder.is_symlink() or not folder.is_dir():
            continue
        for report in sorted(folder.iterdir()):
            if not (report.name.startswith("TEST-") and report.name.endswith(".xml")):
                continue
            tree = parsed_report(report)
            if tree is None:
                unreadable += 1
                continue
            for case in tree.iter("testcase"):
                problem = next((child for child in case if child.tag in ("failure", "error")), None)
                if problem is None:
                    continue
                total += 1
                if len(failures) < MAX_FAILURES:
                    failures.append(failure_of(case, problem, source, suite, roots))
    return failures, total, unreadable


def record_outcome(private, source, results, gradle, passed):
    roots = [str(source), str(source.resolve())]
    failures, total, unreadable = failing_tests(source, roots)
    (private / FAILURES).write_text(json.dumps({"failures": failures, "total": total, "unreadable": unreadable}))
    (private / OUTCOME).write_text(json.dumps({
        "result": "passed" if passed else "failed",
        "gradle": "timeout" if gradle is None else "failed" if gradle else "ok",
        "suites": {name: (list(results[name]) if results.get(name) is not None else None) for name in SUITES}}))


def run_suites(source_sha, record=False, breakdown="false"):
    if breakdown not in ("true", "false"):
        raise UnitTestsError("core_breakdown must be true or false")
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    android = source / "android"
    log = private / "android-unit.log"
    if record:
        for name in (OUTCOME, FAILURES):
            (private / name).unlink(missing_ok=True)
    java_home = pinned_java_home()
    home = sdk_home()
    env = child_env(JAVA_HOME=str(java_home), PATH=str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
                    ANDROID_HOME=str(home), GRADLE_USER_HOME=str(root / "may-gradle"), GIT_TERMINAL_PROMPT="0",
                    JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=" + str(root))
    install_sdk(home, log, env)
    if not (source / FETCH_DESIGN_SYSTEM).is_file() or (source / FETCH_DESIGN_SYSTEM).is_symlink():
        raise UnitTestsError("The pull request has no android/scripts/fetch-design-system.sh; no suite ran")
    if quiet(["bash", FETCH_DESIGN_SYSTEM], log, cwd=source, env=env, timeout=600):
        raise UnitTestsError("fetch-design-system.sh refused the pinned design system; no suite ran")
    refuse_committed_results(android)
    try:
        gradle = quiet(["./gradlew", "--no-daemon", "--continue", *(task for task, _ in SUITES.values())], log,
                       cwd=android, env=env, timeout=GRADLE_SECONDS)
    except subprocess.TimeoutExpired:
        gradle = None
    results = {name: suite_result(android / directory) for name, (_, directory) in SUITES.items()}
    if not record:
        try:
            report(results, gradle)
        finally:
            if breakdown == "true":
                print_breakdown(core_breakdown(source))
        return
    try:
        report(results, gradle)
        passed = True
    except UnitTestsError:
        passed = False
    if breakdown == "true":
        print_breakdown(core_breakdown(source))
    record_outcome(private, source, results, gradle, passed)
    print("May Android unit suites finished; any failing tests stay in this runner's private files for the next step")


def report(results, gradle):
    """One fixed line per suite, then pass only if every suite ran tests, none failed, and Gradle finished cleanly."""
    for name in SUITES:
        result = results.get(name)
        if result is None:
            print(f"May Android unit suite {name}: did not run (its build or Gradle stopped before a report)")
            continue
        tests, failed, skipped = (int(value) for value in result)
        verdict = "passed" if tests and not failed else "failed"
        print(f"May Android unit suite {name}: {verdict}, {int(tests)} tests, {int(failed)} failed, {int(skipped)} skipped")
    if gradle is None:
        raise UnitTestsError("Gradle did not finish within 50 minutes")
    if any(results.get(name) is None for name in SUITES):
        raise UnitTestsError("At least one May Android unit suite did not run")
    if any(not tests or failed for tests, failed, _ in results.values()):
        raise UnitTestsError("At least one May Android unit suite did not pass")
    if gradle:
        raise UnitTestsError("Gradle failed although every suite reported; its log stays on this runner")
    print("Every May Android unit suite passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--record-outcome", action="store_true")
    parser.add_argument("--core-breakdown", default="false")
    args = parser.parse_args()
    try:
        run_suites(args.source_sha, args.record_outcome, args.core_breakdown)
    except (UnitTestsError, StoreError) as error:
        raise SystemExit("May Android unit tests: " + error.args[0]) from None
    except Exception:
        # Any other failure, whatever its type or message, prints only this.
        raise SystemExit("May Android unit tests failed; no private detail was published") from None
