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
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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


def run_suites(source_sha):
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    android = source / "android"
    log = private / "android-unit.log"
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
    report(results, gradle)


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
    args = parser.parse_args()
    try:
        run_suites(args.source_sha)
    except (UnitTestsError, StoreError) as error:
        raise SystemExit("May Android unit tests: " + error.args[0]) from None
    except Exception:
        # Any other failure, whatever its type or message, prints only this.
        raise SystemExit("May Android unit tests failed; no private detail was published") from None
