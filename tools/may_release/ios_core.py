"""may-app's MayCore Swift package suite at an open may-app pull request head (ios-core-pr.yml), on Linux.

ios/MayCore is Foundation only, so it builds and tests on Linux, and nothing else ran its tests before a merge: may-app
#1329 broke one of them on main unseen. This runs it at the head prepare.py fetched (the whole checkout: the
tests read contracts/, shared/test-vectors/, ios/App and ios/project.yml, and skip without them) with the pinned Swift
6.2.4 (toolchain.py swift), in two steps:

  run   (no secret) checks the pinned Swift answers `swift --version` with exactly 6.2.4, refuses a package that
        declares a dependency (nothing is fetched: --disable-automatic-resolution), then runs `swift build
        --build-tests` and `swift test --skip-build` in ios/MayCore, together within RUN_SECONDS, as SANDBOX_USER (no
        sudo, no write access to this job's files) in its own copies of the checkout and toolchain under SANDBOX, all
        removed at the end (no cache). Every line they print goes to a private log. The outcome (passed, failed, also
        when this commit has no ios/MayCore, or timeout), XCTest's own
        closing counts (`Executed N tests, with S tests skipped and F failures`) plus Swift Testing's (`Test run with N
        tests ... with K issues`), and each compiler error or failing test (where, which, and its message, repo-relative,
        cleaned as android_unit.py cleans) go to private files. Public output is fixed lines and counts. It never fails
        the step: post does.
  post  (MAY_APP_PR_COMMENT_TOKEN alone, taken out of the environment at once) posts a failed outcome as one comment
        on the pull request, after reading it again and finding it still open at the checked head, unless a comment
        already carries this head's marker (swift_checks.py's model and calls). A passed outcome posts nothing. It fails the job when the suite failed or did not finish.

This repository is public; may-app is private. Nothing derived from may-app (a test name, a message, a path, a compiler
line) is printed, summarised or uploaded here: every printed line is fixed apart from the pull request number and
integer counts, and every error is a fixed literal (static_check.py).
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_unit import clean
from compile_pr_errors import CompilePrError, github
from ds_compile import DsCompileError, MAX_BODY, SHA, fence_for, post_once, run_url
from gate import APP_REPO, GateDenial, pull_number, validate_pull_request
from prepare import PrepareError, head_revision
from review_mcp import FOOTER, holds
from store import StoreError, child_env, prepared, require_step_secrets, runner_root
import toolchain

COMMENT_TOKEN = "MAY_APP_PR_COMMENT_TOKEN"
PACKAGE = "ios/MayCore"
SWIFT_VERSION = "6.2.4"
BUILD = ["swift", "build", "--build-tests", "--disable-automatic-resolution"]
TEST = ["swift", "test", "--skip-build", "--disable-automatic-resolution"]
COMMANDS = "`swift build --build-tests` and `swift test --skip-build` in `ios/MayCore`"
# The build and the tests together (the job's 35 minutes leave room for the fetch and the 1 GB toolchain).
RUN_SECONDS = 25 * 60
# Independent review of #128 (26 Oct): the runner user has passwordless sudo, and the post step that holds the comment
# token runs in this job after the pull request's own Swift. So that Swift runs as SANDBOX_USER, a system user with no
# sudo, no login shell and no write access to the workspace, the runner's files or this job's command files, in its own
# copies of the checkout and the toolchain under SANDBOX (made and removed here as root, never by the pull request's
# code), with its output on the private log's descriptor alone. After each step, and on a timeout, every process of
# that user is killed, so nothing it started outlives its step.
SANDBOX_USER = "may-core"
SANDBOX = Path("/opt/may-core")
ADMIN_SECONDS = 10 * 60
# Second re-review of #128: that user may not schedule work to outlive the run (DENY_FILES), may not enter the runner's
# home or runner temp (where the job's command files and the token mints' outputs live: checked as that user), is killed
# until none of its processes is left (STOP_ROUNDS), and its files anywhere on the runner go with it (LEFTOVERS).
DENY_FILES = ("/etc/cron.deny", "/etc/at.deny")
STOP_ROUNDS = 20
# Third re-review of #128: the world-writable places, each its own mount or not (-xdev keeps each scan in its own), and the
# sandbox; never the whole root, which cost minutes of the job.
LEFTOVER_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue", "/var/crash", "/var/lib/php/sessions",
                 "/mnt", str(SANDBOX))
LOG, OUTCOME, FAILURES = "ios-core.log", "ios-core-result.json", "ios-core-failures.json"
RESULTS = ("passed", "failed", "timeout")
STAGES = ("build", "tests")
MAX_FAILURES, MAX_NAME, MAX_MESSAGE = 100, 200, 300
MARKER = "may-ios-core-pr"
# A dependency would be fetched from the network by the pull request's own manifest; MayCore has none.
DEPENDENCY = re.compile(r"\.package\s*\(")
VERSION = re.compile(r"Swift version ([0-9.]+) ")
# swiftc's errors: `/path/File.swift:12:5: error: message`.
COMPILER_ERROR = re.compile(r"(.+?\.swift):([0-9]{1,7}):([0-9]{1,7}): error: (.*)")
# XCTest's failures on Linux: `/path/FileTests.swift:162: error: Suite.testName : XCTAssert... failed: message`.
XCTEST_FAILURE = re.compile(r"(.+?\.swift):([0-9]{1,7}): error: ([\w.]+) : (.*)")
# Swift Testing's: `✘ Test name() recorded an issue at FileTests.swift:12:3: Expectation failed: message`.
TESTING_FAILURE = re.compile(r"✘ Test (.+?) recorded an issue at (.+?\.swift):([0-9]{1,7}):[0-9]{1,7}: (.*)")
# XCTest's closing summary (the last one is All tests'), and Swift Testing's.
XCTEST_SUMMARY = re.compile(r"Executed ([0-9]{1,7}) tests?, with (?:([0-9]{1,7}) tests? skipped and )?([0-9]{1,7}) "
                            r"failures? \(([0-9]{1,7}) unexpected\)")
TESTING_SUMMARY = re.compile(r"Test run with ([0-9]{1,7}) tests?\b.*?\b(?:passed|failed)\b(?:.*? with ([0-9]{1,7}) "
                             r"issues?)?")


class IosCoreError(ValueError):
    pass


def test_counts(text):
    """(tests, skipped, failures) from XCTest's last closing summary plus Swift Testing's, or None without XCTest's."""
    xctest = testing = None
    for line in text.splitlines():
        line = line.strip()
        match = XCTEST_SUMMARY.search(line)
        if match:
            xctest = match
        match = TESTING_SUMMARY.search(line)
        if match:
            testing = match
    if xctest is None:
        return None
    tests, skipped = int(xctest.group(1)), int(xctest.group(2) or 0)
    # XCTest's failure count already holds the unexpected ones (thrown errors) it names in brackets.
    failures = int(xctest.group(3))
    if testing is not None:
        tests += int(testing.group(1))
        failures += int(testing.group(2) or 0)
    return tests, skipped, failures


def compiler_errors(text, roots):
    """(errors, total): swiftc's error lines, deduplicated, at most MAX_FAILURES, as {test: None, message, at}."""
    seen, errors = set(), []
    for line in text.splitlines():
        match = COMPILER_ERROR.fullmatch(line.strip())
        if not match or match.group(0) in seen:
            continue
        seen.add(match.group(0))
        errors.append({"test": None, "message": clean(match.group(4), roots, MAX_MESSAGE),
                       "at": clean(f"{match.group(1)}:{int(match.group(2))}:{int(match.group(3))}", roots, MAX_NAME)})
    return errors[:MAX_FAILURES], len(errors)


def failing_tests(text, roots):
    """(failures, total): each XCTest or Swift Testing failure line, at most MAX_FAILURES, as {test, message, at}."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        match = XCTEST_FAILURE.fullmatch(line)
        if match:
            found.append({"test": clean(match.group(3), roots, MAX_NAME), "message": clean(match.group(4), roots,
                                                                                            MAX_MESSAGE),
                          "at": clean(f"{match.group(1)}:{int(match.group(2))}", roots, MAX_NAME)})
            continue
        match = TESTING_FAILURE.fullmatch(line)
        if match:
            found.append({"test": clean(match.group(1), roots, MAX_NAME), "message": clean(match.group(4), roots,
                                                                                            MAX_MESSAGE),
                          "at": clean(f"{match.group(2)}:{int(match.group(3))}", roots, MAX_NAME)})
    return found[:MAX_FAILURES], len(found)


def admin(argv, log, data=None):
    """One root command of the sandbox's own setup or teardown (never the pull request's code), with data on its
    stdin when given; its status, or None."""
    try:
        with log.open("ab") as output:
            return subprocess.run(["sudo", "-n", *argv], stdout=output, stderr=subprocess.STDOUT, input=data,
                                  stdin=None if data is not None else subprocess.DEVNULL,
                                  timeout=ADMIN_SECONDS).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def as_sandbox(argv, log):
    """A check run as SANDBOX_USER (never the pull request's code); its status, or None."""
    try:
        with log.open("ab") as output:
            return subprocess.run(sandboxed(argv), stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  cwd=SANDBOX, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def sandboxed(argv):
    """argv as SANDBOX_USER, with only the pinned Swift's copy and the system on PATH and its own HOME."""
    return ["sudo", "-n", "-u", SANDBOX_USER, "--", "/usr/bin/env", "-i",
            f"PATH={SANDBOX / 'swift/usr/bin'}:/usr/bin:/bin", f"HOME={SANDBOX / 'home'}", *argv]


def sandbox_setup(source, swift_top):
    """The root commands that make the sandbox: a fresh system user, the toolchain's copy (readable, not theirs), and
    the checkout's copy, HOME and build folder (theirs)."""
    user = f"{SANDBOX_USER}:{SANDBOX_USER}"
    return [["rm", "-rf", "--", str(SANDBOX)],
            ["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", str(SANDBOX / "home"),
             "--shell", "/usr/sbin/nologin", SANDBOX_USER],
            ["install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(SANDBOX)],
            ["cp", "-a", "--", str(swift_top), str(SANDBOX / "swift")],
            ["cp", "-a", "--", str(source), str(SANDBOX / "src")],
            ["chown", "-R", user, "--", str(SANDBOX / "src")],
            ["install", "-d", "-o", SANDBOX_USER, "-g", SANDBOX_USER, "-m", "0700", str(SANDBOX / "home"),
             str(SANDBOX / "build")]]


def stop_all(log, strict=True):
    """Every process of SANDBOX_USER killed, as that user (kill -1: all it may signal, forks during a scan included),
    round after round until pgrep finds none; strict, refuses if any is left."""
    for _ in range(STOP_ROUNDS):
        admin(["-u", SANDBOX_USER, "/usr/bin/kill", "-KILL", "-1"], log)
        if admin(["pgrep", "-u", SANDBOX_USER], log) == 1:
            return
        time.sleep(0.25)
    if strict:
        raise IosCoreError("A process of the unprivileged build user could not be stopped")


def leftovers():
    """The root command that deletes SANDBOX_USER's files in each of LEFTOVER_DIRS that exists here."""
    return ["find", *(d for d in LEFTOVER_DIRS if os.path.isdir(d)), "-xdev", "-user", SANDBOX_USER, "-delete"]


def teardown(log):
    """The sandbox gone: its user's processes, its files anywhere on the runner, its copies and the user itself."""
    stop_all(log, strict=False)
    if admin(leftovers(), log) != 0:
        print("May iOS core suite: some files of the build user could not be removed")
    admin(["rm", "-rf", "--", str(SANDBOX)], log)
    admin(["userdel", SANDBOX_USER], log)


def pinned_swift_bin():
    try:
        return Path(toolchain.tool_path("swift", "usr/bin/swift-driver")).parent
    except StoreError:
        raise IosCoreError("The pinned Swift is not installed; nothing ran") from None


def run_tool(argv, log, cwd, env, deadline):
    """The tool's exit status, or None when it ran past the deadline; its output goes to the private log alone. Its
    direct child is root's sudo, which this runner user may not signal (re-review of #128): past the deadline, every
    SANDBOX_USER process is killed instead (stop_all), and sudo then exits on its own."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    with log.open("ab") as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
    try:
        return process.wait(timeout=left)
    except subprocess.TimeoutExpired:
        stop_all(log, strict=False)  # a process that survives fails the run at the step's own stop_all
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return None


def swift_version(env):
    try:
        result = subprocess.run(sandboxed(["swift", "--version"]), env=env, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60, cwd=SANDBOX)
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION.search(result.stdout.decode("utf-8", errors="replace"))
    return match.group(1) if match and not result.returncode else None


def record(private, result, stages, counts, entries, total):
    (private / FAILURES).write_text(json.dumps({"failures": entries, "total": total}))
    (private / OUTCOME).write_text(json.dumps({"result": result, "stages": stages,
                                               "tests": list(counts) if counts is not None else None,
                                               "problems": total}))


def run_suite(source_sha):
    require_step_secrets(())
    if not SHA.fullmatch(source_sha or ""):
        raise IosCoreError("Source SHA is not an exact lowercase commit SHA")
    root = runner_root()
    source, private = prepared(root, source_sha)
    for name in (OUTCOME, FAILURES):
        (private / name).unlink(missing_ok=True)
    package = source / PACKAGE
    manifest = package / "Package.swift"
    if package.is_symlink() or not package.is_dir() or manifest.is_symlink() or not manifest.is_file():
        # Review of #128: a pull request that removes or links the package away fails; it is never a green skip.
        record(private, "failed", dict.fromkeys(STAGES, "not run"), None, [], 0)
        print("May iOS core suite: this commit has no ios/MayCore package; failed")
        return
    if DEPENDENCY.search(manifest.read_text(errors="replace")) or (package / "Package.resolved").exists():
        raise IosCoreError("ios/MayCore declares a package dependency, which this job does not fetch; nothing ran")
    swift_top = pinned_swift_bin().parent.parent
    log = private / LOG
    log.write_bytes(b"")
    try:
        run_sandboxed(source, swift_top, private, log)
    finally:
        teardown(log)


def run_sandboxed(source, swift_top, private, log):
    home = Path(os.environ.get("HOME") or Path.home())
    if admin(["chmod", "0700", "--", str(home)], log) != 0:
        raise IosCoreError("The unprivileged build user could not be set up; nothing ran")
    for argv in sandbox_setup(source, swift_top):
        if admin(argv, log) != 0:
            raise IosCoreError("The unprivileged build user could not be set up; nothing ran")
    for path in DENY_FILES:
        if admin(["tee", "-a", "--", path], log, data=(SANDBOX_USER + "\n").encode()) != 0:
            raise IosCoreError("The unprivileged build user could not be set up; nothing ran")
    for path in (home, runner_root()):
        if as_sandbox(["/usr/bin/test", "!", "-x", str(path)], log) != 0:
            raise IosCoreError("The unprivileged build user can reach the runner's files; nothing ran")
    env = child_env()
    if swift_version(env) != SWIFT_VERSION:
        raise IosCoreError("Swift is not the pinned 6.2.4; nothing ran")
    package = SANDBOX / "src" / PACKAGE
    scratch = ["--scratch-path", str(SANDBOX / "build")]
    roots = [str(SANDBOX / "src"), str(source), str(source.resolve())]
    deadline = time.monotonic() + RUN_SECONDS
    stages = dict.fromkeys(STAGES, "not run")
    status = run_tool(sandboxed(BUILD + scratch), log, package, env, deadline)
    stop_all(log)
    stages["build"] = "timeout" if status is None else "failed" if status else "passed"
    counts, entries, total = None, [], 0
    if stages["build"] == "passed":
        start = log.stat().st_size
        status = run_tool(sandboxed(TEST + scratch), log, package, env, deadline)
        stop_all(log)
        with log.open("rb") as written:
            written.seek(start)
            text = written.read().decode("utf-8", errors="replace")
        counts = test_counts(text)
        stages["tests"] = "timeout" if status is None else \
            "passed" if not status and counts and counts[0] and not counts[2] else "failed"
        if stages["tests"] != "passed":
            entries, total = failing_tests(text, roots)
    elif stages["build"] == "failed":
        entries, total = compiler_errors(log.read_text(errors="replace"), roots)
    result = "timeout" if "timeout" in stages.values() else \
        "passed" if all(v == "passed" for v in stages.values()) else "failed"
    record(private, result, stages, counts, entries, total)
    for name in STAGES:
        verdict = stages[name]
        if verdict == "timeout":
            print(f"May iOS core {name}: did not finish in time")
        elif verdict == "not run":
            print(f"May iOS core {name}: did not run (the build stopped it)")
        else:
            print(f"May iOS core {name}: {verdict}")
    if counts is not None:
        tests, skipped, failures = (int(n) for n in counts)
        print(f"May iOS core tests: Executed {int(tests)} tests, with {int(skipped)} skipped and {int(failures)} "
              "failures")
    if result != "passed":
        print(f"May iOS core suite: {int(total)} failures found, which go to the may-app pull request only")


def read_record(private):
    """(outcome, failures) from run's private files, each checked for its shape; refuses otherwise."""
    try:
        outcome = json.loads((private / OUTCOME).read_text())
        failed = json.loads((private / FAILURES).read_text())
    except (OSError, ValueError):
        raise IosCoreError("No iOS core outcome is recorded; nothing was posted") from None
    count = lambda v: type(v) is int and 0 <= v <= 10_000_000
    line = lambda v, limit: isinstance(v, str) and len(v) <= limit and "\n" not in v
    stages = outcome.get("stages") if isinstance(outcome, dict) else None
    tests = outcome.get("tests") if isinstance(outcome, dict) else None
    if not isinstance(outcome, dict) or outcome.get("result") not in RESULTS or not count(outcome.get("problems")) or \
            not isinstance(stages, dict) or set(stages) != set(STAGES) or \
            any(v not in ("passed", "failed", "timeout", "not run") for v in stages.values()) or \
            not (tests is None or (isinstance(tests, list) and len(tests) == 3 and all(count(n) for n in tests))):
        raise IosCoreError("The iOS core outcome is malformed; nothing was posted")
    entries = failed.get("failures") if isinstance(failed, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_FAILURES or not count(failed.get("total")) or \
            failed["total"] < len(entries) or any(
                not isinstance(e, dict) or set(e) != {"test", "message", "at"} or
                not (e["test"] is None or line(e["test"], MAX_NAME)) or not line(e["message"], MAX_MESSAGE) or
                not line(e["at"], MAX_NAME) for e in entries):
        raise IosCoreError("The iOS core failure record is malformed; nothing was posted")
    return outcome, failed


def comment_body(sha, url, outcome, failed, shown=None):
    """The one comment posted on the pull request when the suite failed or did not finish."""
    if not SHA.fullmatch(sha or ""):
        raise IosCoreError("The checked commit is not an exact SHA")
    stages, counts = outcome["stages"], outcome["tests"]
    lines = ["### May iOS core suite: failed", "",
             f"`{sha}`: {COMMANDS} on ubuntu-24.04 with the pinned Swift {SWIFT_VERSION}, from may-releases.", "",
             f"Build: {stages['build']}. Tests: {stages['tests']}."]
    if counts is not None:
        lines += ["", f"Executed {counts[0]} tests, with {counts[1]} skipped and {counts[2]} failures."]
    if outcome["result"] == "timeout":
        lines += ["", "The suite did not finish within 25 minutes."]
    entries = failed["failures"] if shown is None else failed["failures"][:shown]
    if entries:
        block = []
        for entry in entries:
            block.append(f"{entry['at']}: {entry['test']}" if entry["test"] else entry["at"])
            block.append(f"    {entry['message']}")
        more = failed["total"] - len(entries)
        extra = f" Showing the first {len(entries)}; {more} more not shown." if more > 0 else ""
        what = "compiler error" if stages["build"] == "failed" else "failure"
        fence = fence_for(block)
        lines += ["", f"**{failed['total']} {what}{'s' if failed['total'] != 1 else ''}**: where, which, and the "
                  f"message.{extra}", "", fence + "text", *block, fence]
    elif outcome["result"] == "failed" and stages["build"] == "not run":
        lines += ["", "This commit has no `ios/MayCore` package (or it is a link), so nothing ran."]
    elif outcome["result"] == "failed":
        lines += ["", "The suite failed without a readable error or failing test. Nothing else from the log is posted."]
    if url:
        lines += ["", f"Run: {url}"]
    body = "\n".join(lines) + f"\n\n<!-- {MARKER} head={sha} -->" + FOOTER
    if len(body) > MAX_BODY:
        if not entries:
            raise IosCoreError("The comment would be too long to post")
        return comment_body(sha, url, outcome, failed, len(entries) // 2)
    return body


def post(sha, pr, call_api=github):
    require_step_secrets((COMMENT_TOKEN,))
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop(COMMENT_TOKEN, "")
    if not SHA.fullmatch(sha or ""):
        raise IosCoreError("Source SHA is not an exact lowercase commit SHA")
    number = pull_number(pr)
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise IosCoreError("Runner temporary directory is unavailable")
    root = Path(raw).resolve()
    checkout, private = root / "may-source", root / "may-private"
    outcome, failed = read_record(private)
    if outcome["result"] == "passed":
        print("May iOS core suite: nothing to post")
        return 0
    total = failed["total"]
    if not token:
        print("May iOS core suite: MAY_APP_PR_COMMENT_TOKEN is not set in may-native-compile-pr; no comment posted")
        return 1
    if len(token) < 20:
        raise IosCoreError("The may-app comment token is malformed (MAY_APP_PR_COMMENT_TOKEN); nothing was posted")
    if head_revision(checkout) != sha:
        raise IosCoreError("The checked checkout is not the dispatched commit; nothing was posted")
    body = comment_body(sha, run_url(os.environ), outcome, failed)
    if holds(body, token):
        raise IosCoreError("The comment holds a credential; nothing was posted")
    status, pull = call_api("GET", f"/repos/{APP_REPO}/pulls/{number}", None, token)
    if status != 200:
        raise IosCoreError("The may-app pull request could not be read again; nothing was posted")
    try:
        validate_pull_request(pull, number, sha)
    except GateDenial:
        raise IosCoreError("The pull request is no longer open at the checked head; nothing was posted") from None
    if not post_once(call_api, APP_REPO, number, sha, body, token, MARKER):
        print(f"May iOS core result already posted for this head on may-app #{number}; nothing posted again")
        return 1
    print(f"May iOS core suite failed; {int(total)} failures posted to may-app #{number}")
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "post"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pr-number")
    args = parser.parse_args()
    if args.command == "run":
        run_suite(args.source_sha)
        return 0
    return post(args.source_sha, args.pr_number)


if __name__ == "__main__":
    try:
        failed = main()
    except GateDenial as denial:
        raise SystemExit("May iOS core suite denied: " + denial.args[0]) from None
    except (IosCoreError, CompilePrError, DsCompileError, StoreError) as error:
        raise SystemExit("May iOS core suite stopped: " + error.args[0]) from None
    except (PrepareError, OSError, subprocess.SubprocessError):
        raise SystemExit("May iOS core suite stopped; no may-app source, path or credential was printed") from None
    except Exception:
        raise SystemExit("May iOS core suite stopped; check may-native-compile-pr, its secrets and the inputs") from None
    if failed:
        raise SystemExit(1)
