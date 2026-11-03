"""May iOS push-tap spike: a debug May Playground taps a notification itself (device-tests.yml ios_push_tap).

Run 41580341109 showed the simulator never delivers a tap on May's notification, so the tap crash (may-app #1577)
had no device repro. N's may-app #1652 (HOOK) gives a debug Playground a hook: launched with MAY_TEST_PUSH_TAP set
to a fixture's name, it taps that fixture's notification itself, once, after the launch's test-session sign-in,
through the delegate's own entry point, and logs one line in its push-tap-hook category (CATEGORY) saying how it
went. Coordinator, 3 Nov: one cold launch per fixture, the line read back, and May's process watched for an exit or
a crash.

  - The fixtures' names come from may-app's LIST in the fetched checkout at run time (one name a line, NAME's
    shape, at most MAX_FIXTURES, no `#` comments or blank lines counted), never from this public repository. No
    list, or one that is a link, too long, unreadable, or holds anything else: no fixture runs, and the run is
    infra, which fails it.
  - A checkout that does not hold HOOK runs nothing either: its May has no hook to answer.
  - Each fixture: pass when its line says the completion handler ran on the main thread and May is still running
    with no new crash report; fail when it ran off the main thread, the tap went unanswered, or May exited or
    crashed; infra when no line came (within WAIT_SECONDS), the line says the hook could not build or run the tap,
    or May did not launch.
  - The run fails when any fixture failed, or when none passed or failed (all infra).

The public log gets counts and fixed words alone, never a fixture's name or anything May logged. Each fixture's
name, verdict, the hook's line and the process's state go to the private diag (DIAG) beside any crash report (the
run's sealed may-renders release).
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import time

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import device_tests  # noqa: E402
import renders  # noqa: E402
from store import write_private  # noqa: E402

PUSH_TAP = "MAY_IOS_PUSH_TAP"
HOOK = "6feeca19589332626f043cfb2a79a47fd5a0fa8d"  # may-app #1652, squashed on main
LIST = "ios/push-tap-fixtures.txt"
# may-app's rule for a name (its list's header): lowercase ASCII letters, digits and hyphens, 1 to 32; never a
# leading hyphen here, so no name reads as an option.
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MAX_FIXTURES, MAX_LIST = 16, 4096
CATEGORY = "push-tap-hook"
LINE = re.compile(rb"push-tap fixture=(?P<name>\S{1,32}) (?P<said>delivered completion_on_main=(?:true|false)|"
                  rb"unanswered|unbuildable|unknown)\b")
# SETTLE_SECONDS: as the notification spike's, so a slow ReportCrash still counts for its fixture.
WAIT_SECONDS, POLL_SECONDS, SETTLE_SECONDS, LAUNCH_SECONDS = 20, 2, 8, 120
MAX_HOOK_LINES = 20  # the hook's newest lines kept for each fixture (it writes one)
DIAG = "push-tap"
# The private marker a failed spike leaves: device_tests.py verdict, after the seal, fails the job on it.
FAILED = device_tests.PUSH_TAP_FAILED
PASS, FAIL, INFRA = "pass", "fail", "infra"


def wanted():
    return os.environ.get(PUSH_TAP, "").strip() == "true"


def fixtures(source):
    """(state, names) from the checkout's LIST: ("missing", []), ("malformed", []) or ("listed", [name, ...])."""
    path = source / LIST
    if not path.exists() and not path.is_symlink():
        return "missing", []
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LIST:
            return "malformed", []
        text = path.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError):
        return "malformed", []
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines or len(lines) > MAX_FIXTURES or len(set(lines)) != len(lines) or \
            not all(NAME.fullmatch(line) for line in lines):
        return "malformed", []
    return "listed", lines


def hooked(source):
    """True when the checkout's commit holds HOOK (it is HOOK or a descendant of it)."""
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(source), "merge-base", "--is-ancestor", HOOK,
                              "HEAD"], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def said(lines, name):
    """What the hook's newest line for this fixture said ("delivered completion_on_main=true" and the like), or
    None when there is none."""
    found = None
    for match in LINE.finditer(lines):
        if match["name"].decode() == name or match["said"] == b"unknown":
            found = match["said"].decode()
    return found


def judged(words, alive, crashed, launched=True):
    """A fixture's verdict from the hook's words, whether May is still running, and whether a crash report came."""
    if crashed or (words is not None and not alive):
        return FAIL
    if not launched or words in (None, "unbuildable", "unknown"):
        return INFRA
    if words == "delivered completion_on_main=true":
        return PASS
    return FAIL  # off the main thread, or unanswered


def verdicts_fail(verdicts):
    """True when the run fails: no fixture ran, any failed, or every one was infra."""
    return not verdicts or FAIL in verdicts or all(word == INFRA for word in verdicts)


def read_lines(udid, env, start, bounded=device_tests.bounded_output):
    """The hook's lines in May's own subsystem since start; b"" when the log could not be read."""
    predicate = f'subsystem == "{device_tests.APP_ID}" AND category == "{CATEGORY}"'
    done = bounded(["xcrun", "simctl", "spawn", udid, "log", "show", "--style", "compact", "--info", "--start",
                    start, "--predicate", predicate], env, limit=device_tests.MAX_DIAG)
    return b"" if done is None or done[0] else done[1]


def tap_fixtures(udid, env, source, private, folder, log, token, diagnostics, pid, reports, symbols=None,
                 launch=renders.quiet, lines=read_lines, clock=time.monotonic, sleep=time.sleep):
    """The spike: each listed fixture once, on a cold launch. Writes the private diag and, when the run fails, the
    FAILED marker; returns the verdicts. pid(udid, env): May's process id or None; reports(before): May's crash
    reports that came in since; symbols(data): file:line lines for a report."""
    def say(why):
        # why is made of this function's fixed words and counts alone (never a fixture's name or May's lines).
        print(f"May device tests (ios): push tap: {why}")

    (private / FAILED).unlink(missing_ok=True)
    state, names = fixtures(source)
    verdicts, kept = [], []
    if not hooked(source):
        say("the may-app commit does not hold the push-tap hook, so no fixture ran")
    elif state == "missing":
        say("may-app has no push-tap fixture list, so no fixture ran (no fixtures)")
    elif state == "malformed":
        say("may-app's push-tap fixture list is malformed, so no fixture ran")
    else:
        say(f"{len(names)} fixtures listed")
        seen = set(diagnostics.crash_reports())
        crashes = []
        for name in names:
            start = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                launched = not launch(["xcrun", "simctl", "launch", "--terminate-running-process", udid,
                                       device_tests.APP_ID], log, env=dict(env, SIMCTL_CHILD_MAY_TEST_SESSION=token,
                                                                          SIMCTL_CHILD_MAY_TEST_PUSH_TAP=name),
                                      timeout=LAUNCH_SECONDS).returncode
            except subprocess.SubprocessError:
                launched = False
            before = pid(udid, env) if launched else None
            words, read = None, b""
            deadline = clock() + WAIT_SECONDS
            while launched:
                read = lines(udid, env, start)
                words = said(read, name)
                if words is not None or clock() >= deadline:
                    break
                sleep(POLL_SECONDS)
            sleep(SETTLE_SECONDS)
            after = pid(udid, env)
            alive = after is not None and (before is None or after == before)
            new = reports(seen)
            seen.update(diagnostics.crash_reports())
            crashes.extend(new)
            verdict = judged(words, alive, bool(new), launched)
            verdicts.append(verdict)
            kept.append(f"{name}: {verdict}; launched {'yes' if launched else 'no'}, hook said "
                        f"{words or 'nothing'}, May running {'yes' if alive else 'no'}, crash report "
                        f"{'yes' if new else 'no'}\n".encode() + b"".join(
                            line + b"\n" for line in read.splitlines()[-MAX_HOOK_LINES:] if LINE.search(line)))
        try:
            launch(["xcrun", "simctl", "terminate", udid, device_tests.APP_ID], log, env=env, timeout=LAUNCH_SECONDS)
        except subprocess.SubprocessError:
            pass
        say(f"{verdicts.count(PASS)} passed, {verdicts.count(FAIL)} failed, {verdicts.count(INFRA)} infra")
        if crashes:
            say(f"{len(crashes)} May crash reports came in")
        diagnostics.keep(folder, DIAG, token)
        found = b""
        for path in crashes:
            try:
                found += symbols(path.read_bytes()) if symbols else b""
            except OSError:
                continue
        lines_kept, _ = device_tests.kept_lines(b"".join(kept), token)
        path = folder / (DIAG + device_tests.DIAG)
        before_bytes = path.read_bytes() if path.is_file() else b""
        extra = b"== push tap, per fixture ==\n" + lines_kept
        if found:
            extra += b"== May's frames, file:line ==\n" + device_tests.kept_lines(found, token)[0]
        path.write_bytes((before_bytes + extra)[:device_tests.MAX_DIAG])
    if verdicts_fail(verdicts):
        say("the spike failed")
        write_private(private / FAILED, b"true")
    else:
        say("the spike passed")
    return verdicts
