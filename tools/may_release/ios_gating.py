"""May iOS gating tests: the may-app tests whose failure blocks a may-app pull request (native-compile-pr.yml).

Coordinator, 3 Nov: N's may-app #1630 put PushDelegateThreadTests (they fail without #1577, control #1631) in the
MayPlaygroundSnapshots bundle, which only ios-renders ran, and its verdict gated nothing. The native job's compile is
the iOS merge gate for may-app pull requests, so after a passed compile (compile.py --record-outcome) it now also runs
the tests may-app lists in LIST (N owns it): one `xcodebuild -only-testing:` selector a line, `Bundle/Class` or
`Bundle/Class/method`, ASCII identifiers alone, at most MAX_SELECTORS; blank lines and `#` comments are skipped.

  - No list, or one with no selector: state "none", and nothing runs (a pull request before the list exists passes).
  - A list that is a link, too long, unreadable, or holds a line that is not a selector (or one twice): "malformed",
    nothing runs, and the job fails (a typo never silently passes).
  - Otherwise the bundle's scheme runs those tests on the simulator, with no signing and no credential, its snapshot
    images written to the private folder alone: "passed" when xcodebuild exits 0 and at least one test ran with none
    failing, else "failed" (no test ran counts as failed too, so a selector matching nothing never passes).
  - A failed compile, or a sandboxed one (compile.py --sandbox: the simulator as the sandbox's user is not proven yet),
    records "not run", which fails nothing.

The record (RESULT, in the private folder) holds the state, how many selectors were listed and tests ran, and the
failing tests' names (each a selector's shape, else counted alone). compile_pr_errors.py seals it with the compile's
record: the names go to the may-app pull request alone; this public log gets the counts.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

LIST = "ios/gating-tests.txt"
RESULT = "ios-gating-result.json"
BUNDLE = "may-gating.xcresult"
SNAPSHOTS = "may-gating-snapshots"
SCHEME = "MayPlaygroundSnapshots"
SELECTOR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}(?:/[A-Za-z_][A-Za-z0-9_]{0,99}){1,2}")
MAX_SELECTORS, MAX_LIST, MAX_FAILED, MAX_COUNT = 20, 4096, 50, 100_000
TEST_SECONDS, RESULTS_SECONDS = 25 * 60, 120
STATES = ("none", "malformed", "not run", "passed", "failed")
FAILING = ("malformed", "failed")


def selectors(source):
    """(state, selectors) from the checkout's LIST: ("none", []), ("malformed", []) or ("listed", [selector, ...])."""
    path = source / LIST
    if not path.exists() and not path.is_symlink():
        return "none", []
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LIST:
            return "malformed", []
        text = path.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError):
        return "malformed", []
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        return "none", []
    if len(lines) > MAX_SELECTORS or len(set(lines)) != len(lines) or \
            not all(SELECTOR.fullmatch(line) for line in lines):
        return "malformed", []
    return "listed", lines


def record(state, listed=0, ran=0, failed=(), total=0):
    return {"state": state, "listed": listed, "ran": ran, "failed": list(failed)[:MAX_FAILED], "total": total}


def checked(data):
    """The record, checked whole (as compile_pr_errors.py's record is); None when it is not one."""
    count = lambda value: type(value) is int and 0 <= value <= MAX_COUNT
    if not (isinstance(data, dict) and set(data) == {"state", "listed", "ran", "failed", "total"} and
            data["state"] in STATES and count(data["listed"]) and data["listed"] <= MAX_SELECTORS and
            count(data["ran"]) and count(data["total"]) and isinstance(data["failed"], list) and
            len(data["failed"]) <= MAX_FAILED and len(data["failed"]) <= data["total"] and
            all(isinstance(name, str) and SELECTOR.fullmatch(name) for name in data["failed"])):
        return None
    if data["state"] != "failed" and (data["failed"] or data["total"]):
        return None
    if data["state"] in ("none", "malformed", "not run") and (data["listed"] or data["ran"]):
        return None
    return data


def write(private, result):
    (private / RESULT).write_text(json.dumps(result))
    return result


def read(private):
    """The native job's gating record, or "not run" when none was written or it is not one."""
    path = private / RESULT
    try:
        data = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None
    except (OSError, ValueError, RecursionError):
        data = None
    return checked(data) or record("not run")


def not_run(private):
    return write(private, record("not run"))


def outcomes(data):
    """(tests that ran, failing names in a selector's shape, how many failed) from `xcresulttool get test-results
    tests`'s JSON: each test case under its bundle and suite."""
    ran, names, total = 0, [], 0

    def walk(nodes, bundle=None, suite=None, depth=0):
        nonlocal ran, total
        if depth > 32 or not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind, name = node.get("nodeType"), node.get("name")
            name = name if isinstance(name, str) else ""
            if kind in ("Unit test bundle", "UI test bundle"):
                walk(node.get("children"), name, None, depth + 1)
            elif kind == "Test Suite":
                walk(node.get("children"), bundle, name, depth + 1)
            elif kind == "Test Case":
                ran += 1
                if node.get("result") == "Failed":
                    total += 1
                    full = f"{bundle}/{suite}/{name.removesuffix('()')}"
                    if SELECTOR.fullmatch(full) and full not in names:
                        names.append(full)
            else:
                walk(node.get("children"), bundle, suite, depth + 1)
    walk(data.get("testNodes") if isinstance(data, dict) else None)
    return min(ran, MAX_COUNT), names[:MAX_FAILED], min(total, MAX_COUNT)


def results(bundle, env):
    """outcomes() of the result bundle, or (0, [], 0) when it cannot be read."""
    if not bundle.is_dir() or bundle.is_symlink():
        return 0, [], 0
    try:
        done = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(bundle)],
                              capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=RESULTS_SECONDS)
        return outcomes(json.loads(done.stdout)) if not done.returncode else (0, [], 0)
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        return 0, [], 0


def run(source, root, private, env, log, tool=None, simulator=None, read_results=results):
    """The listed tests on the simulator after a passed compile, recorded in RESULT; the record."""
    state, chosen = selectors(source)
    if state != "listed":
        return write(private, record(state))
    import renders  # here, not at the top: renders imports compile, which calls this
    tool, simulator = tool or renders.quiet, simulator or renders.simulator
    bundle, snapshots = private / BUNDLE, private / SNAPSHOTS
    shutil.rmtree(bundle, ignore_errors=True)
    snapshots.mkdir(mode=0o700, exist_ok=True)
    code = None
    try:
        udid = simulator(log, env)
        code = tool(["xcodebuild", "test", "-project", str(source / "ios/May.xcodeproj"), "-scheme", SCHEME,
                     "-destination", "id=" + udid, "-derivedDataPath", str(root / "may-derived"),
                     "-clonedSourcePackagesDirPath", str(root / "may-packages"), "-resultBundlePath", str(bundle),
                     *("-only-testing:" + selector for selector in chosen), "CODE_SIGNING_ALLOWED=NO"], log,
                    env=dict(env, MAY_SNAPSHOT_DIR=str(snapshots), TEST_RUNNER_MAY_SNAPSHOT_DIR=str(snapshots)),
                    timeout=TEST_SECONDS).returncode
    except (OSError, subprocess.SubprocessError, renders.RendersError):
        code = None
    ran, names, total = read_results(bundle, env)
    passed = code == 0 and ran > 0 and total == 0
    return write(private, record("passed" if passed else "failed", len(chosen), ran, names if not passed else (),
                                 total if not passed else 0))
