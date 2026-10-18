"""May emulator flows on a free public runner: pass or fail only.

Runs may-app's scripted flows (android/flows in may-app, at --flows-sha) against the
May Playground app built at --source-sha, on an Android emulator. The flows are copied
over the prepared source and applied with their Gradle init script, so one copy of the
flows runs against any app commit: the build before a fix (the broken-build control)
and the build after it.

This repository is public. Every tool writes to a private runner log that is never
published. Public output is one fixed line per flow test, built only from this file's
own words: the flow's class name from FLOWS (anything else is "an unlisted flow"), the
test's number within it, and its outcome from outcome_of. Nothing the app, its tests or
the instrumentation printed reaches the public log: no method name, exception class,
assertion message, stack trace, screenshot or app output. The one exception is a number: a
failed assertion's line in the flow's own file, so it can be found in may-app's source.

A test that is ignored or whose assumption failed fails the run, on purpose: a flow
that did not check what it is for is not a pass.

Trust: the flows' Gradle init script runs on a runner whose job holds the read-only
keys, so flows_sha is a trust boundary of its own. By default it must be in may-app's
main history, like source_sha. flows_from_branch: true admits any may-app commit, for
trying flows before they merge (their broken-build controls).

Subcommands, one per workflow step:
  admit    the dispatch is the flows workflow's own, from main, with exact SHAs, and the
           may-flows environment exists (never auto-created)
  overlay  fetch the flows at --flows-sha and copy them over the prepared source
  build    build the Playground app and its flow APK (no secret in the step)
  run      start the emulator, install, run the selected flows, report
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from prepare import APP, exact_sha, fetch
from gate import GateDenial, get_json
from store import child_env, prepared, require_step_secrets, runner_root

CONTROL_REPO = "May-Assistant/may-releases"
# Dispatch from main only. Admission refuses any other ref before a secret is read.
ALLOWED_REFS = ("refs/heads/main",)
ENVIRONMENT = "may-flows"
FLOWS_READ_SECRET = "MAY_APP_READ_KEY"
FLOWS_PACKAGE = "io.github.mayassistant.may.flows"
# Which flow classes a dispatch runs. "all" runs the whole package.
FLOWS = {
    "all": None,
    "chat-reply": "ChatReplyFlow",
    "task-detail": "TaskDetailFlow",
    "connections": "ConnectionsFlow",
    "empty-states": "EmptyStatesFlow",
    "message-menu": "MessageMenuFlow",
    "sending-status": "SendingStatusFlow",
    "sign-in": "SignInFlow",
    "plan-your-day": "PlanYourDayFlow",
    "approvals": "ApprovalsFlow",
    "onboarding-b": "OnboardingBFlow",
    # Build 12's sheet (may-app docs/evidence/build-12).
    "take-notes": "TakeNotesFlow",
    "voice-notes-off": "VoiceNotesOffFlow",
    "any-time-event": "AnyTimeEventFlow",
    "pin": "PinFlow",
    "report-bug": "ReportBugFlow",
    "files-off": "TaskFilesOffFlow",
    "computer-resting": "ComputerRestingFlow",
    # Build 13 (may-app android/flows README, flows 18-21).
    "new-account-confirm": "NewAccountConfirmFlow",
    "thread-reply-reconnect": "ThreadReplyReconnectFlow",
    "draft-kill": "DraftKillFlow",
    "sign-out-queued": "SignOutQueuedFlow",
}
# may-app's android/flows/manifest.txt: one "ClassName count" line per flow class. A run's results must match it
# exactly for the classes it ran: every listed test reported once, nothing missing, added or unlisted.
MANIFEST = "android/flows/manifest.txt"
# Three-button navigation (settings secure navigation_mode), so a flow checking clearance has a full bar.
THREE_BUTTON = "0"
NAV_SECONDS = 30
MANIFEST_BYTES = 64 * 1024
APP_ID = "io.github.mayassistant.may.playground"
TEST_ID = APP_ID + ".test"
RUNNER = TEST_ID + "/androidx.test.runner.AndroidJUnitRunner"
# SDK: platforms;android-37.0 is the app's compileSdk 37, and build-tools 36.0.0 is what
# its Android Gradle Plugin (9.4.1) builds with (a local build fetched no other). The
# system image is the emulator's own OS, separate from both.
# Emulator: API 35 (edge-to-edge is enforced for the app's targetSdk), a phone shape,
# and three-button navigation, so the navigation bar a flow must stay clear of is a
# real, full-height one.
SYSTEM_IMAGE = "system-images;android-35;google_apis;x86_64"
SDK_PACKAGES = ("platform-tools", "emulator", "platforms;android-37.0", "build-tools;36.0.0", SYSTEM_IMAGE)
SDK_INSTALL_ATTEMPTS = 2
SDK_RETRY_SECONDS = 30
SDK_INSTALL_SECONDS = 900
DEVICE = "pixel_7"
AVD = "may-flows"
BOOT_SECONDS = 600
APKS = ("android/app/build/outputs/apk/playground/debug/app-playground-debug.apk",
        "android/app/build/outputs/apk/androidTest/playground/debug/app-playground-debug-androidTest.apk")
# am instrument -r status codes (androidx.test InstrumentationResultPrinter).
STATUS = {0: "pass", -1: "error", -2: "fail", -3: "ignored", -4: "assumption failed"}
# A failure is an assertion only when the thrown class is one of these (JUnit's, and
# what Compose's test APIs throw); anything else, an app class included, is reported
# as "an exception" without its name.
ASSERTIONS = {"java.lang.AssertionError", "junit.framework.AssertionFailedError", "org.junit.ComparisonFailure",
              "junit.framework.ComparisonFailure", "org.junit.internal.ArrayComparisonFailure"}


class FlowsError(ValueError):
    pass


def admit(repo, ref, event, source_sha, flows_sha, flows, from_branch):
    if (repo, event) != (CONTROL_REPO, "workflow_dispatch") or ref not in ALLOWED_REFS:
        raise FlowsError("Flows run only by manual dispatch from an allowed branch")
    for sha in (source_sha, flows_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise FlowsError("Source and flows must be exact lowercase commit SHAs")
    if flows not in FLOWS:
        raise FlowsError("Unknown flow selection")
    if from_branch not in ("true", "false"):
        raise FlowsError("flows_from_branch must be true or false")


QUEUE_MAX = 8


def queue(source_sha, flows, then):
    """The runs one dispatch makes, one at a time: its own source and flows, then each `then` entry ("source_sha:flow",
    separated by spaces or commas), all with the dispatch's flows_sha. Each entry is held to admission's own checks."""
    runs = [(source_sha, flows)]
    for entry in re.split(r"[\s,]+", (then or "").strip()):
        if not entry:
            continue
        sha, sep, selection = entry.partition(":")
        if not sep:
            raise FlowsError("Each queued run must be source_sha:flow")
        runs.append((sha, selection))
    if len(runs) > QUEUE_MAX:
        raise FlowsError("At most 8 runs per dispatch")
    for sha, selection in runs:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise FlowsError("Source and flows must be exact lowercase commit SHAs")
        if selection not in FLOWS:
            raise FlowsError("Unknown flow selection")
    return [{"source_sha": sha, "flows": selection} for sha, selection in runs]


def environment_exists(token, get=get_json):
    """The job's environment must already exist: an `environment:` that names a missing one auto-creates it."""
    if not token:
        raise FlowsError("GitHub metadata token unavailable")
    try:
        get("environments/" + ENVIRONMENT, token)
    except GateDenial:
        raise FlowsError("The may-flows environment does not exist or is unavailable") from None


def overlay(source_sha, flows_sha, from_branch):
    """Fetch may-app at flows_sha (in main history unless from_branch) and put its android/flows over the source."""
    require_step_secrets((FLOWS_READ_SECRET,))
    if from_branch not in ("true", "false"):
        raise FlowsError("flows_from_branch must be true or false")
    root = runner_root()
    source, private = prepared(root, source_sha)
    checkout = root / "may-flows-source"
    fetch(checkout, APP, exact_sha(flows_sha), os.environ.get(FLOWS_READ_SECRET, ""), private, "flows",
          main_history=from_branch != "true")
    flows = checkout / "android/flows"
    if not (flows / "flows.init.gradle").is_file() or flows.is_symlink() or \
            any(p.is_symlink() for p in flows.rglob("*")):
        raise FlowsError("Flows commit has no plain android/flows directory")
    target = source / "android/flows"
    if target.is_symlink():
        raise FlowsError("Prepared source has a symlinked android/flows")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(flows, target, symlinks=False)
    shutil.rmtree(checkout)


def run_private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=timeout)
    if check and result.returncode:
        raise FlowsError("A private flows tool failed; diagnostics remain on the runner")
    return result.returncode


def sdk():
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home or not Path(home).is_dir():
        raise FlowsError("Android SDK is unavailable on this runner")
    return Path(home)


def sdk_tool(home, *names):
    for name in names:
        path = home / name
        if path.is_file():
            return str(path)
    raise FlowsError("An Android SDK tool is unavailable on this runner")


def sleep(seconds):
    time.sleep(seconds)


def install_sdk(home, log, env):
    manager = sdk_tool(home, "cmdline-tools/latest/bin/sdkmanager")
    with log.open("ab") as output:
        subprocess.run([manager, "--licenses"], input=b"y\n" * 20, stdout=output,
                       stderr=subprocess.STDOUT, env=env, check=False)
    # One retry: a download from Google's SDK repository can fail on a runner before any
    # build (run 38848477790). A second failure is real and fails the run.
    for attempt in range(SDK_INSTALL_ATTEMPTS):
        try:
            if not run_private([manager, "--install", *SDK_PACKAGES], log, env=env, check=False,
                               timeout=SDK_INSTALL_SECONDS):
                return
        except subprocess.TimeoutExpired:
            pass
        if attempt + 1 < SDK_INSTALL_ATTEMPTS:
            sleep(SDK_RETRY_SECONDS)
    raise FlowsError("The Android SDK did not install after one retry; no flow ran")


def build(source_sha):
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    log = private / "flows-build.log"
    home = sdk()
    env = child_env(GRADLE_USER_HOME=str(root / "may-gradle"), GIT_TERMINAL_PROMPT="0",
                    JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=" + str(root))
    install_sdk(home, log, env)
    if run_private(["./gradlew", "--no-daemon", "--init-script", "flows/flows.init.gradle",
                    ":app:assemblePlaygroundDebug", ":app:assemblePlaygroundDebugAndroidTest"],
                   log, cwd=source / "android", env=env, check=False):
        raise FlowsError("The Playground app or its flows did not build; no flow ran")
    for apk in APKS:
        if not (source / apk).is_file():
            raise FlowsError("The build left no Playground or flow APK")


def assertion_line(stack, flow):
    """The line, in [flow]'s own file, of the innermost stack frame there: digits only, or None."""
    if not flow:
        return None
    # Frames only ("\tat …(File.kt:N)"): the first line is the thrown class and its message, which is never read.
    found = re.search(r"(?m)^\s+at [\w.$]+\(" + re.escape(flow) + r"\.kt:(\d{1,5})\)$", stack)
    return int(found.group(1)) if found else None


def outcome_of(code, stack, flow=None):
    """One of this file's fixed outcome words for an `am instrument` status code (and a failed assertion's line in [flow])."""
    if code == 0:
        return "pass"
    if code in (-1, -2):
        thrown = stack.split(":", 1)[0].strip()
        if thrown not in ASSERTIONS:
            return "fail (an exception, not an assertion)"
        line = assertion_line(stack, flow)
        return "fail (assertion)" if line is None else "fail (assertion, line %d)" % line
    return {-3: "ignored", -4: "assumption failed"}[code]


def flow_label(test_class):
    """The flow's class name if it is one of FLOWS, else a fixed word: never the reported text."""
    for name in FLOWS.values():
        if name and test_class == FLOWS_PACKAGE + "." + name:
            return name
    return "an unlisted flow"


def parse_instrumentation(text):
    """[(label, outcome)] from `am instrument -r` output, in this file's own words only."""
    results, status, counts, key = [], {}, {}, None
    for line in text.splitlines():
        if line.startswith("INSTRUMENTATION_STATUS: "):
            key, _, value = line[len("INSTRUMENTATION_STATUS: "):].partition("=")
            status[key] = value
        elif key == "stack" and not line.startswith("INSTRUMENTATION_"):
            # The stack's frames follow on lines of their own.
            status[key] += "\n" + line
        elif line.startswith("INSTRUMENTATION_STATUS_CODE: "):
            code = line.split(": ", 1)[1].strip()
            if re.fullmatch(r"-?\d+", code) and int(code) in STATUS and int(code) != 1:
                label = flow_label(status.get("class", ""))
                counts[label] = counts.get(label, 0) + 1
                flow = label if label in FLOWS.values() else None
                results.append((label + " test " + str(counts[label]), outcome_of(int(code), status.get("stack", ""), flow)))
            status, key = {}, None
    completed = "INSTRUMENTATION_CODE: -1" in text
    return results, completed


def read_manifest(path):
    """{class: count} from android/flows/manifest.txt; every class one of FLOWS', every count a positive number."""
    try:
        if path.stat().st_size > MANIFEST_BYTES:
            raise FlowsError("android/flows/manifest.txt is larger than 64 KiB")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise FlowsError("The flows commit has no readable android/flows/manifest.txt") from None
    known, expected = {name for name in FLOWS.values() if name}, {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in known or parts[0] in expected or not re.fullmatch(r"[1-9][0-9]{0,2}", parts[1]):
            raise FlowsError("android/flows/manifest.txt is malformed or names a flow this runner does not know")
        expected[parts[0]] = int(parts[1])
    if not expected:
        raise FlowsError("android/flows/manifest.txt lists no flow")
    return expected


def manifest_difference(results, expected, flows):
    """None when the run reported exactly the manifest's tests for the selected flows; else "missing" (a listed test
    or class did not report) or "extra" (a test or class the manifest does not list, an unlisted flow included)."""
    selection = FLOWS[flows]
    if selection and selection not in expected:
        return "missing"
    wanted = {selection: expected[selection]} if selection else expected
    counts = {}
    for label, _ in results:
        name = label.rpartition(" test ")[0]
        counts[name] = counts.get(name, 0) + 1
    if any(name not in wanted or count > wanted[name] for name, count in counts.items()):
        return "extra"
    return "missing" if counts != wanted else None


def three_button_navigation(adb, log, env):
    """Switch the emulator to three-button navigation and confirm it took."""
    if run_private([adb, "shell", "cmd", "overlay", "enable-exclusive", "--category",
                    "com.android.internal.systemui.navbar.threebutton"], log, env=env, check=False):
        raise FlowsError("The emulator did not switch to three-button navigation; no flow ran")
    deadline = time.monotonic() + NAV_SECONDS
    while True:
        mode = subprocess.run([adb, "shell", "settings", "get", "secure", "navigation_mode"], capture_output=True,
                              stdin=subprocess.DEVNULL, env=env)
        if mode.stdout.strip() == THREE_BUTTON.encode():
            return
        if time.monotonic() > deadline:
            raise FlowsError("The emulator did not switch to three-button navigation; no flow ran")
        sleep(2)


def run(source_sha, flows, from_branch):
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    expected = read_manifest(source / MANIFEST)
    log = private / "flows-run.log"
    home = sdk()
    env = child_env(ANDROID_AVD_HOME=str(root / "may-avd"), ANDROID_EMULATOR_HOME=str(root / "may-avd"))
    adb = sdk_tool(home, "platform-tools/adb")
    emulator_bin = sdk_tool(home, "emulator/emulator")
    avdmanager = sdk_tool(home, "cmdline-tools/latest/bin/avdmanager")
    (root / "may-avd").mkdir(mode=0o700, exist_ok=True)
    with log.open("ab") as output:
        subprocess.run([avdmanager, "create", "avd", "--force", "-n", AVD, "-k", SYSTEM_IMAGE, "-d", DEVICE],
                       input=b"no\n", stdout=output, stderr=subprocess.STDOUT, env=env, check=True)
        emulator = subprocess.Popen([emulator_bin, "-avd", AVD, "-no-window", "-no-audio", "-no-boot-anim",
                                     "-no-snapshot", "-gpu", "swiftshader_indirect", "-camera-back", "none"],
                                    stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    try:
        run_private([adb, "wait-for-device"], log, env=env, timeout=BOOT_SECONDS)
        deadline = time.monotonic() + BOOT_SECONDS
        while True:
            booted = subprocess.run([adb, "shell", "getprop", "sys.boot_completed"], capture_output=True,
                                    stdin=subprocess.DEVNULL, env=env)
            if booted.stdout.strip() == b"1":
                break
            if time.monotonic() > deadline or emulator.poll() is not None:
                raise FlowsError("The emulator did not boot; no flow ran")
            time.sleep(5)
        for setting in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            run_private([adb, "shell", "settings", "put", "global", setting, "0"], log, env=env)
        three_button_navigation(adb, log, env)
        for apk in APKS:
            run_private([adb, "install", "-r", "-t", str(source / apk)], log, env=env)
        selection = FLOWS[flows]
        target = ["-e", "class", FLOWS_PACKAGE + "." + selection] if selection else ["-e", "package", FLOWS_PACKAGE]
        result = subprocess.run([adb, "shell", "am", "instrument", "-w", "-r", *target, RUNNER],
                                capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=3600)
        text = result.stdout.decode(errors="replace")
        with log.open("a") as output:
            output.write(text)
        results, completed = parse_instrumentation(text)
    finally:
        emulator.terminate()
        try:
            emulator.wait(timeout=60)
        except subprocess.TimeoutExpired:
            emulator.kill()
    report(results, completed, expected, flows, from_branch)


def report(results, completed, expected, flows, from_branch):
    """Prints one line per test, then passes only a finished run whose every test passed and matches the manifest.
    A run of flows taken from a branch (flows_from_branch) says so first: it is not release evidence."""
    if from_branch == "true":
        print("May flows: flows taken from a branch; this run is not release evidence")
    for name, outcome in results:
        print("May flow " + name + ": " + outcome)
    if not results:
        raise FlowsError("No flow test reported a result")
    if not completed:
        raise FlowsError("The flow run did not finish (the app or its tests stopped part-way)")
    if any(outcome != "pass" for _, outcome in results):
        raise FlowsError("At least one May flow did not pass")
    difference = manifest_difference(results, expected, flows)
    if difference == "missing":
        raise FlowsError("A flow or test in android/flows/manifest.txt is missing from the run")
    if difference == "extra":
        raise FlowsError("The run reported a flow or test that is not in android/flows/manifest.txt")
    print("Every selected May flow passed")


def cleanup():
    root = runner_root()
    for name in ("may-source", "may-design-system", "may-flows-source", "may-private", "may-gradle", "may-avd"):
        shutil.rmtree(root / name, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("admit", "overlay", "build", "run", "cleanup"))
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--flows-sha", default="")
    parser.add_argument("--flows", default="all")
    parser.add_argument("--flows-from-branch", default="false")
    parser.add_argument("--then", default="")
    args = parser.parse_args()
    try:
        if args.command == "admit":
            admit(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
                  os.environ.get("GITHUB_EVENT_NAME"), args.source_sha, args.flows_sha, args.flows,
                  args.flows_from_branch)
            runs = queue(args.source_sha, args.flows, args.then)
            environment_exists(os.environ.get("GH_TOKEN"))
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("runs=" + json.dumps(runs) + "\n")
            print("May flows admitted: %d run%s, one at a time" % (len(runs), "" if len(runs) == 1 else "s"))
        elif args.command == "overlay":
            overlay(args.source_sha, args.flows_sha, args.flows_from_branch)
            print("May flows placed over the prepared source")
        elif args.command == "build":
            build(args.source_sha)
            print("May Playground app and its flows built")
        elif args.command == "run":
            run(args.source_sha, args.flows, args.flows_from_branch)
        else:
            cleanup()
    except FlowsError as error:
        raise SystemExit("May flows: " + error.args[0]) from None
    except Exception:
        # Any other failure, whatever its type or message, prints only this.
        raise SystemExit("May flows failed; no private detail was published") from None
