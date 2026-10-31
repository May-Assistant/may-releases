"""May Playground device tests on the iOS simulator (device-tests.yml, the ios job): device_tests.py's twin of the
Android job, with the same scenario list, verdicts, report and seal.

- build: the pinned Xcode and XcodeGen (renders.xcode_env, as ios-renders builds), then a debug May Playground for
  the simulator, unsigned. No secret is in the step.
- run_scenarios: an iPhone 16 Pro simulator on the newest iOS runtime (renders.simulator), booted, the app installed, and each
  of the platform's scenarios: a fresh staging test session (device_tests.mint), the app launched with it in
  SIMCTL_CHILD_MAY_TEST_SESSION (N's hook, may-app #921: the debug Playground reads MAY_TEST_SESSION at launch, never
  as an argument), the screen recorded (simctl recordVideo, then avconvert, CONVERSIONS, so a recording stays well under
  publish.py's 40 MB a file), the scenario's Maestro flow (the same flows as Android: one bundle id), a screenshot and
  its read-only server checks.

Agent scenarios run here too: device_agent.py with device_mcp.py's iOS backend, which acts through the pinned
Maestro's driver (one fixed command a flow), with the same tag, server truth, judge and cleanup as Android. The public
log carries scenario ids, verdict words and counts only.
"""

import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import device_agent
import device_tests
from device_tests import DeviceTestsError, at, kept_lines
import renders
from store import child_env, require_step_secrets, runner_root, write_private

SCHEME = "MayPlayground"
APP = "may-derived/Build/Products/Debug-iphonesimulator/MayPlayground.app"
BUNDLE = device_tests.APP_ID
MAX_RECORDING = 38 * 1024 * 1024
# avconvert's presets in the order tried, each with the file type it writes: 720p HD as M4V, then the plain H.264
# presets as MPEG-4 at 720p and 540p.
CONVERSIONS = (("PresetAppleM4V720pHD", ".m4v"), ("Preset1280x720", ".mp4"), ("Preset960x540", ".mp4"))
STOP_SECONDS = 30
# Run 40138900610: iOS chat recorded for 1809 s, and even its smallest conversion (49 MB) was over the cap, so the
# stuck scenario, the one most worth watching, kept no recording. Then its last part alone is converted, as long as the
# smallest whole conversion suggests will fit (with TAIL_MARGIN to spare), and never less than MIN_TAIL_SECONDS.
TAIL_MARGIN, MIN_TAIL_SECONDS = 0.8, 60
# One avconvert, however long the recording, never holds the job (the security read of #116).
CONVERT_SECONDS = 600
# Run 40181517145 (coordinator, 25 Oct): each scenario is recorded in parts of PART_SECONDS, up to MAX_PARTS, and all
# its conversions share CONVERSION_BUDGET_SECONDS. The run's budget (device_tests.Budget) counts a scenario's agent
# and onboarding alone: the job's 30 minutes past RUN_BUDGET_SECONDS cover the last scenario's conversions.
PART_SECONDS, MAX_PARTS, CONVERSION_BUDGET_SECONDS = 5 * 60, 12, 10 * 60
# Onboarding after them (coordinator, 26 Oct, run 40290647340): its new account's 20 minutes left chat and notes none.
IOS_LAST = ("notes", "chat", "onboarding")
COMPILE_SECONDS = 300
# A launch argument (UserDefaults' argument domain), never the environment: may-app #1194's in-app touch overlay.
SHOW_TOUCHES = ("-MayShowTouches", "YES")
# The coordinator's A/B switch (25 Oct): with ios_reduce_motion dispatched true, the simulator's Reduce Motion is on
# before the app first starts, which stops the chat header's mark redrawing every frame (N's audit).
REDUCE_MOTION = "MAY_IOS_REDUCE_MOTION"
REDUCE_MOTION_ON = ("defaults", "write", "com.apple.Accessibility", "ReduceMotionEnabled", "-bool", "true")
# may-app #1338 (N, 26 Oct): the debug Playground's main-thread watchdog and SwiftUI's change log run only when the
# launch environment asks (MAY_DIAG_WATCHDOG=1, MAY_DIAG_LOG_CHANGES=1); the watchdog's 10 Hz ping was the suspected cause
# of the idle hangs. The two are asked for apart (coordinator, 26 Oct). The watchdog (spans, steps and stalls): every
# launch asks unless ios_diagnostics is dispatched false, so the nightly run, which has no inputs, asks too (the
# scripted rows of runs 40309200520 and 40319309252 took no longer with diagnostics on). Off, no stall, span or
# stall-after line exists to read, and the public log says the stalls were not collected, never 0. SwiftUI's change
# log: only when ios_log_changes is dispatched true, since it brought the idle hang back itself (may-app #1302).
DIAGNOSTICS, LOG_CHANGES = "MAY_IOS_DIAGNOSTICS", "MAY_IOS_LOG_CHANGES"
WATCHDOG_LAUNCH = {"SIMCTL_CHILD_MAY_DIAG_WATCHDOG": "1"}
LOG_CHANGES_LAUNCH = {"SIMCTL_CHILD_MAY_DIAG_LOG_CHANGES": "1"}
# N, 31 Oct (may-app claude/ios-loop-census, DS #240): the debug Playground's loop census, asked for only when the run is
# dispatched with ios_loop_census. Every 5 s while a design-system loop drew a frame in the last 2 s, it logs
# "may-loops: <Name>, <Name>" (names from a fixed set, no user content) in the watchdog's category, so the private diag
# keeps those lines beside each refused act's times. Run it with ios_diagnostics off: the watchdog's pings blur idle.
LOOP_CENSUS = "MAY_IOS_LOOP_CENSUS"
LOOP_CENSUS_LAUNCH = {"SIMCTL_CHILD_MAY_DIAG_LOOPS": "1"}
SWIFTUI_PERSIST = ("log", "config", "--subsystem", "com.apple.SwiftUI", "--mode", "level:info,persist:info")
# Ad-hoc simulator signing, with no identity, team or secret: Xcode embeds the Playground's entitlements as simulated
# ones. Unsigned (CODE_SIGNING_ALLOWED=NO), the app had none, so the Keychain refused its test session
# (errSecMissingEntitlement) and the app signed out with "Something went wrong on this device" (run 40035089599).
ADHOC = ("CODE_SIGN_IDENTITY=-", "CODE_SIGN_STYLE=Manual", "DEVELOPMENT_TEAM=")


def build(tool=renders.build):
    require_step_secrets(())
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not private.is_dir():
        raise DeviceTestsError("Exact private source is not prepared")
    log = private / "device-build.log"
    try:
        env, generator = renders.xcode_env(private, log)
        tool([str(generator), "generate"], log, cwd=source / "ios", env=env)
        tool(["xcodebuild", "-project", str(source / "ios/May.xcodeproj"), "-scheme", SCHEME, "-configuration", "Debug",
              "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(root / "may-derived"),
              "-clonedSourcePackagesDirPath", str(root / "may-packages"), *ADHOC, "build"],
             log, env=env)
    except renders.RendersError:
        raise DeviceTestsError("The Playground app did not build for the simulator; no scenario ran") from None
    if not (root / APP / "Info.plist").is_file():
        raise DeviceTestsError("The Playground app did not build for the simulator; no scenario ran")
    print("May device tests: Playground debug app built for the iOS simulator")


def preview_encoder(log, tool=renders.quiet):
    """preview.swift (AVFoundation), compiled with the pinned Xcode's swiftc into the private folder, as stitch.swift,
    within COMPILE_SECONDS: the function that makes one preview. Raises DeviceTestsError when it will not compile."""
    binary = log.parent / "preview"
    env = dict(os.environ, DEVELOPER_DIR=str(renders.XCODE))
    if tool(["xcrun", "swiftc", "-O", "-swift-version", "5", "-o", str(binary),
             str(Path(__file__).resolve().parent / "preview.swift")], log, env=env, timeout=COMPILE_SECONDS).returncode:
        raise DeviceTestsError("preview.swift did not compile")

    def encode(videos, target):
        return not renders.quiet([str(binary), str(target), str(device_tests.PREVIEW_WIDTH),
                                  str(device_tests.PREVIEW_MAX_SECONDS), str(device_tests.PREVIEW_FPS),
                                  str(device_tests.PREVIEW_BITRATE), *(str(video) for video in videos)], log,
                                 env=xcode(), timeout=device_tests.PREVIEW_SECONDS).returncode
    return encode


def xcode():
    env = child_env(DEVELOPER_DIR=str(renders.XCODE))
    # Maestro runs on the runner's Java 17 when the image names one.
    java = os.environ.get("JAVA_HOME_17_arm64") or os.environ.get("JAVA_HOME_17_X64")
    if java:
        env["JAVA_HOME"] = java
    return env


class Recorder:
    """The simulator's screen for one scenario (simctl recordVideo until SIGINT) in parts of PART_SECONDS, each then
    converted (CONVERSIONS) for publishing under MAX_RECORDING, or kept as recorded when no conversion works and it
    fits. One part is <scenario>.mp4; more are <scenario>-part<n>.mp4, as Android's are."""

    def __init__(self, udid, env, scenario, private, log, part_seconds=None, clock=None):
        self.udid, self.env, self.scenario, self.log = udid, env, scenario, log
        self.private, self.clocked = private, clock
        self.part_seconds = PART_SECONDS if part_seconds is None else part_seconds
        self.raws, self.marks = [], []  # each part's raw recording, and when it started
        self.process = None
        # Why stop() kept no recording, in fixed words and a size in MB, for the public log (None when one was kept),
        # and how a recording was kept when it is not the usual conversion.
        self.dropped = self.kept = None
        self.started = self.stopped = None
        self.halting, self.stuck = threading.Event(), False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        # Each conversion's outcome and avconvert's error class (error_class), for the private diag alone.
        self.notes = []

    def clock(self):
        return (self.clocked or time.monotonic)()

    def length(self, part=0):
        """How long a part ran, for the public log ("" when it was never started here)."""
        if self.started is None or not self.marks or self.stopped is None:
            return ""
        end = self.marks[part + 1] if part + 1 < len(self.marks) else self.stopped
        return f" after {round(end - self.marks[part])} s of recording"

    def begin_part(self):
        raw = self.private / f"{self.scenario}-recording-{len(self.raws) + 1}.mov"
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264",
                                             "--force", str(raw)], stdout=output, stderr=output,
                                            stdin=subprocess.DEVNULL, env=self.env)
        self.raws.append(raw)
        self.marks.append(self.clock())

    def end_part(self):
        """SIGINT, so simctl finishes the part's file: False when it did not stop and was killed."""
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return False
        return True

    def start(self):
        self.begin_part()
        self.started = self.marks[0]
        self.thread.start()

    def loop(self):
        """Run 40181517145's 30-minute recordings (330 and 394 MB) failed every preset: a new part every
        PART_SECONDS keeps each one short, up to MAX_PARTS (the last then runs until halt)."""
        while not self.halting.wait(self.part_seconds) and len(self.raws) < MAX_PARTS:
            if not self.end_part():
                self.stuck = True
                return
            if self.halting.is_set():  # halt came while the part ended: no new one
                return
            self.begin_part()

    def halt(self):
        """Ends the recording ahead of the server reads; stop() converts it. False when the recorder did not stop."""
        if self.process is None or self.dropped:
            return False
        if self.stopped is not None:
            return True
        self.halting.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2 * STOP_SECONDS + 5)
        if self.thread.is_alive() or self.stuck or not self.end_part():
            self.dropped = "the recorder did not stop"
            return False
        self.stopped = self.clock()
        return True

    def stop(self, folder):
        if not self.halt():
            return []
        names, kept, dropped = [], [], []
        # A budget for all of the scenario's conversions together, so no scenario's recording holds the job.
        deadline = self.clock() + CONVERSION_BUDGET_SECONDS
        for part, raw in enumerate(self.raws):
            many = len(self.raws) > 1
            out = folder / (f"{self.scenario}-part{part + 1}.mp4" if many else f"{self.scenario}.mp4")
            name, how, why = self.publish(part, raw, out, deadline)
            prefix = f"part {part + 1}: " if many else ""
            if name:
                names.append(name)
            if how:
                kept.append(prefix + how)
            if why:
                dropped.append(prefix + why)
        self.kept = "; ".join(kept) or None
        self.dropped = "; ".join(dropped) or None
        return names

    def publish(self, part, raw, out, deadline):
        """One part converted to out: (its name or None, how it was kept when not the usual conversion, why it was
        dropped), the last two for the public log in fixed words and MB."""
        seconds = int((self.marks[part + 1] if part + 1 < len(self.marks) else self.stopped) - self.marks[part])
        # Smoke run 39683880243: the M4V preset's conversion to .mp4 failed. Each preset writes the file type it
        # supports, in the private folder; the first one that works and fits the cap is published as .mp4 (an M4V is
        # an MPEG-4 file). When none does, the original H.264 recording is published if it fits.
        # Run 40035089599 dropped five recordings with "no conversion worked", which did not say whether avconvert
        # failed or wrote a file still over the cap; each preset's outcome is now named, in fixed words and MB.
        # Run 40203481816: avconvert failed parts that were already under the cap (exit 205, with no cause given), so
        # a part that fits is published as recorded, and only a part over the cap is converted (coordinator, 25 Oct).
        if raw.is_file() and raw.stat().st_size <= MAX_RECORDING:
            raw.replace(out)
            return out.name, None, None
        outcomes, smallest = [], None  # smallest: (bytes, preset, suffix) of a conversion that was over the cap
        for preset, suffix in CONVERSIONS:
            converted = self.private / (f"{self.scenario}-converted-{part + 1}" + suffix)
            failed = self.convert(raw, ["--preset", preset], converted, deadline, f"part {part + 1} {preset}")
            if not failed and converted.is_file() and converted.stat().st_size <= MAX_RECORDING:
                converted.replace(out)
                raw.unlink(missing_ok=True)
                return out.name, None, None
            outcomes.append(f"{preset} " + outcome(failed, converted))
            if not failed and converted.is_file() and (smallest is None or converted.stat().st_size < smallest[0]):
                smallest = (converted.stat().st_size, preset, suffix)
            converted.unlink(missing_ok=True)
        tried = ", ".join(outcomes)
        tail, tried_tail = self.tail(part, raw, out, seconds, smallest, deadline)
        if tail:
            raw.unlink(missing_ok=True)
            return (out.name, f"as its last {tail} s alone: the whole did not fit the cap ({tried}){self.length(part)}",
                    None)
        size = megabytes(raw) if raw.is_file() else 0
        raw.unlink(missing_ok=True)
        return None, None, (f"no conversion fit the cap ({tried}) and the original is {size} MB, over it" +
                            (f"; {tried_tail}" if tried_tail else "") + self.length(part))

    def convert(self, raw, options, converted, deadline, what):
        """One avconvert of a raw part to converted, within the scenario's conversion budget: its exit status,
        "timed out" past CONVERT_SECONDS or the budget's end, or "budget spent" when none was left to start it. Its
        output goes to the private log, and its error class (error_class) to the private diag's notes."""
        left = int(deadline - self.clock())
        if left <= 0:
            self.notes.append(f"{what}: not tried, the scenario's conversion budget was spent")
            return "budget spent"
        with tempfile.TemporaryFile() as output:
            try:
                status = subprocess.run(["avconvert", "--source", str(raw), "--output", str(converted), *options,
                                         "--replace"], stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                        env=self.env, timeout=min(CONVERT_SECONDS, left)).returncode
            except subprocess.TimeoutExpired:
                status = "timed out"
            output.seek(0)
            said = output.read(MAX_CONVERT_OUTPUT)
        with self.log.open("ab") as log:
            log.write(said)
        if status:
            self.notes.append(f"{what}: " + (f"exit {status}" if type(status) is int else status) +
                              f"; avconvert said: {error_class(said)}")
        return status

    def tail(self, part, raw, out, seconds, smallest, deadline):
        """A part's last stretch converted to out with smallest's preset, sized to fit the cap: (its length in
        seconds, or 0 when none was kept; how a tail that was tried and refused came out, for the log, or None)."""
        if smallest is None or not raw.is_file():
            return 0, None
        size, preset, suffix = smallest
        tail = max(MIN_TAIL_SECONDS, int(seconds * MAX_RECORDING * TAIL_MARGIN / size))
        if tail >= seconds:
            return 0, None
        converted = self.private / (f"{self.scenario}-tail-{part + 1}" + suffix)
        failed = self.convert(raw, ["--preset", preset, "--start", str(seconds - tail), "--duration", str(tail)],
                              converted, deadline, f"part {part + 1} {preset} last {tail} s")
        if not failed and converted.is_file() and converted.stat().st_size <= MAX_RECORDING:
            converted.replace(out)
            return tail, None
        tried = f"its last {tail} s alone: {outcome(failed, converted)}"
        converted.unlink(missing_ok=True)
        return 0, tried


# avconvert's own words for a failure (run 40181517145: exit 205 on every preset, with nothing in the public log to
# say why): each NSError's domain and code it printed, else its last line, for the private diag alone.
ERROR_CLASS = re.compile(rb"Domain=([A-Za-z][A-Za-z0-9_.]{0,80}) Code=(-?[0-9]{1,9})")
MAX_CONVERT_OUTPUT = 64 * 1024


def error_class(said):
    found = []
    for domain, code in ERROR_CLASS.findall(said):
        named = f"{domain.decode()} {code.decode()}"
        if named not in found:
            found.append(named)
    if found:
        return ", ".join(found[:4])
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    return lines[-1][:200].decode("utf-8", "replace") if lines else "nothing"


def megabytes(path):
    return path.stat().st_size // (1024 * 1024)


def outcome(failed, converted):
    """One conversion's outcome for the public log, in fixed words and MB."""
    return (f"failed (exit {failed})" if type(failed) is int and failed else
            "not tried (the scenario's conversion budget was spent)" if failed == "budget spent" else
            "failed (timed out)" if failed else
            f"{megabytes(converted)} MB" if converted.is_file() else "wrote no file")


# may-app #1272: the debug Playground's main-thread watchdog logs one line per stall of 250 ms or more, alone in its
# category. The predicate names the Playground's own subsystem, so the log of no other process is ever read.
WATCHDOG = 'subsystem == "io.github.mayassistant.may.playground" AND category == "watchdog"'
# may-app #1318 (N and the coordinator, 25 Oct): the debug Playground's ChatView logs which of its properties changed
# for each body update (SwiftUI's _logChanges). Read from the Playground's own process alone, and kept in the private
# diag alone, never counted or printed: those lines name may-app's properties. Only the BODY_BEFORE nearest before each
# chat breadcrumb (CHAT_AFTER) are kept, each cut to MAX_BODY_LINE bytes.
BODY_CHANGES = 'process == "MayPlayground" AND subsystem == "com.apple.SwiftUI" AND category == "Changed Body Properties"'
PREDICATE = f"({WATCHDOG}) OR ({BODY_CHANGES})"
BODY_CHANGE = re.compile(rb"MayPlayground\[[0-9]+:[0-9a-f]+\] \[com\.apple\.SwiftUI:Changed Body Properties\] ")
CHAT_AFTER = (b"view-chat", b"view-chat-row")
BODY_BEFORE, MAX_BODY_LINE = 3, 300
STALL = re.compile(rb"main-thread stall ([0-9]{1,7}) ms")
# may-app #1281: beside the stalls, one line per named span of main-thread work of 250 ms or more, in the same category
# ("main-thread span event-store 1180 ms"). Every span line is kept, in the log's own order among the stalls; the public
# log counts the names agreed with N alone, each with its longest: five from #1281, and three on chat's path (the
# chat's publish, its inbox rows and each transcript page's merge), after run 40164384433's chat stalled every 3 s.
SPAN = re.compile(rb"main-thread span ([a-z-]{1,40}) ([0-9]{1,7}) ms[ \t]*\r?\n?$")
SPAN_NAMES = ("event-store", "recognizer", "dictation-capability", "calendar-access", "agenda-state", "chat-publish",
              "chat-inbox", "chat-merge", "chat-body")
MAX_SPAN_SECTION = device_tests.MAX_DIAG // 8
# may-app #1309 (N, 25 Oct): after each stall line, "main-thread stall-after <name>", the last main-actor step that ran
# before it, from a fixed set agreed with N. The public log counts each name, and any other as "other".
STALL_AFTER = re.compile(rb"main-thread stall-after ([a-z-]{1,40})[ \t]*\r?\n?$")
STALL_AFTER_NAMES = ("launch", "sync-start", "sync-transcript", "sync-publish", "sync-inbox", "sync-end", "agenda-load",
                     "connections-load", "pill-read", "view-root", "view-shell", "view-chat", "view-chat-row")
# Run 40181517145: chat's and notes' onboarding subflow did not pass, with nothing to say where it stopped. Its last
# ONBOARDING_LINES of Maestro output go to the scenario's private diag when it does not pass. The private diag keeps
# DIAG_RESERVE for them and for the recording's notes, written after the conversion.
ONBOARDING_LINES, DIAG_RESERVE = 40, 16 * 1024
# The simulator's crash reports land in the host's own folder; only the Playground's process is kept.
CRASH_REPORTS = Path.home() / "Library" / "Logs" / "DiagnosticReports"
CRASH_REPORT = re.compile(r"MayPlayground[-_][A-Za-z0-9._-]{1,120}\.(?:ips|crash)")
# The file name alone is not proof (the security read of #115): a report is kept only when its own header names the
# Playground's bundle id, as an .ips report's JSON first line ("bundleID") or a .crash report's Identifier line does.
OWN_REPORT = re.compile(rb'"bundleID"[ \t]*:[ \t]*"' + re.escape(BUNDLE.encode()) + rb'"|^Identifier:[ \t]+' +
                        re.escape(BUNDLE.encode()) + rb"[ \t]*\r?$", re.MULTILINE)
REPORT_HEAD = 16 * 1024


def body_changes(lines):
    """The watchdog's lines with only the BODY_BEFORE body-change lines nearest before each chat breadcrumb kept (the
    rest dropped), each cut to MAX_BODY_LINE bytes; the log's order is kept."""
    keep, waiting = set(), []
    for number, line in enumerate(lines):
        if BODY_CHANGE.search(line):
            waiting = (waiting + [number])[-BODY_BEFORE:]
            continue
        after = STALL_AFTER.search(line)
        if after:  # each breadcrumb closes its stall: only the chat's keep the changes before them
            if after.group(1) in CHAT_AFTER:
                keep.update(waiting)
            waiting = []
    return [line if not BODY_CHANGE.search(line) else line.rstrip(b"\r\n")[:MAX_BODY_LINE] + b"\n"
            for number, line in enumerate(lines) if not BODY_CHANGE.search(line) or number in keep]


def show_log(udid, env, start):
    """The simulator's unified log since start, the watchdog's lines alone; b"" when it could not be read."""
    done = device_tests.bounded_output(["xcrun", "simctl", "spawn", udid, "log", "show", "--style", "compact",
                                        "--info", "--start", start, "--predicate", PREDICATE], env,
                                       limit=device_tests.MAX_DIAG)
    return b"" if done is None else done[1]


class Diagnostics:
    """Android's twin (device_tests.Diagnostics): each scenario's main-thread stalls from the watchdog's log, and any
    new crash report of the Playground's, through the same redaction, as <scenario>.diag.txt. Reports already there at
    the start are left out, so each scenario keeps its own."""

    def __init__(self, udid, env, reports=CRASH_REPORTS, shown=show_log):
        self.udid, self.env, self.reports, self.shown = udid, env, reports, shown
        self.seen = set(self.crash_reports())
        self.start = None
        self.kept = self.stalls = self.longest = self.crashes = self.drivers = 0
        self.spans = {}  # name: [count, longest ms], for SPAN_NAMES alone
        self.after = {}  # name: count, for STALL_AFTER_NAMES and "other"
        self.dropped = {}  # scenario: lines dropped, for the public log's counts

    def crash_reports(self):
        try:
            return sorted(path for path in self.reports.iterdir()
                          if CRASH_REPORT.fullmatch(path.name) and path.is_file() and not path.is_symlink())
        except OSError:
            return []

    def begin(self):
        """The scenario's start, in the log's own local time."""
        self.start = time.strftime("%Y-%m-%d %H:%M:%S")

    def keep(self, folder, scenario, token, driver=None, onboarding=b""):
        """Writes <scenario>.diag.txt when there is anything to keep; never an error. driver: the agent's DRIVER_FILE,
        each refused act's Maestro lines (device_mcp.refused); onboarding: the subflow's last lines when it did not
        pass."""
        found = []
        for path in self.crash_reports():
            if path not in self.seen:
                self.seen.add(path)
                try:
                    with path.open("rb") as report:
                        data = report.read(device_tests.MAX_DIAG // 2)
                except OSError:
                    continue
                if OWN_REPORT.search(data[:REPORT_HEAD]):
                    found.append(b"== crash ==\n" + data + b"\n")
        self.crashes += bool(found)
        lines = body_changes([line for line in (self.shown(self.udid, self.env, self.start) if self.start else b"")
                              .splitlines(keepends=True) if STALL.search(line) or SPAN.search(line) or
                              STALL_AFTER.search(line) or BODY_CHANGE.search(line)])
        stalls = [int(STALL.search(line).group(1)) for line in lines if STALL.search(line)]
        self.stalls += len(stalls)
        self.longest = max([self.longest, *stalls])
        refused = b""
        try:
            if driver is not None and driver.is_file() and not driver.is_symlink():
                with driver.open("rb") as stream:
                    refused = stream.read(device_tests.device_mcp.MAX_DRIVER)
        except OSError:
            refused = b""
        # The crash reports first, at most half; the refused acts' driver lines, capped where device_mcp wrote them;
        # the watchdog's newest lines fill the rest. All through kept_lines with the session.
        (crashes, from_crashes), (watchdog, from_log) = kept_lines(b"".join(found), token), kept_lines(b"".join(lines),
                                                                                                         token)
        refused, from_driver = kept_lines(refused, token)
        onboarding, from_onboarding = kept_lines(onboarding, token)
        onboarding = whole_lines(onboarding, DIAG_RESERVE // 2 - 64, newest=True)
        self.drivers += bool(refused)
        # Counted from what kept_lines kept, so a span line it dropped is neither published nor counted.
        spans = [line for line in watchdog.splitlines(keepends=True) if SPAN.search(line)]
        for line in spans:
            name, ms = SPAN.search(line).group(1).decode(), int(SPAN.search(line).group(2))
            if name in SPAN_NAMES:
                count, longest = self.spans.get(name, [0, 0])
                self.spans[name] = [count + 1, max(longest, ms)]
        for line in watchdog.splitlines(keepends=True):
            after = STALL_AFTER.search(line)
            if after:
                name = after.group(1).decode() if after.group(1).decode() in STALL_AFTER_NAMES else "other"
                self.after[name] = self.after.get(name, 0) + 1
        crashes = crashes[:device_tests.MAX_DIAG // 2]
        # The section headers' room, the onboarding lines and the recording's notes (DIAG_RESERVE).
        room = device_tests.MAX_DIAG - len(crashes) - len(refused) - 192 - DIAG_RESERVE
        # The watchdog's newest lines fill the rest; when that trims any, every span line goes ahead of it too.
        trimmed = len(watchdog) > room
        section = b"".join(spans)[:MAX_SPAN_SECTION] if trimmed else b""
        room -= len(section)
        watchdog = watchdog[len(watchdog) - room:] if len(watchdog) > room else watchdog
        if not (crashes or refused or watchdog or onboarding):
            return
        text = (crashes + (b"== onboarding ==\n" + onboarding if onboarding else b"") +
                (b"== driver ==\n" + refused if refused else b"") +
                (b"== spans ==\n" + section if section else b"") +
                (b"== watchdog ==\n" + watchdog if watchdog else b"")).decode("utf-8", "replace").encode()
        text = text[:device_tests.MAX_DIAG].decode("utf-8", "ignore").encode()
        try:
            (folder / f"{scenario}{device_tests.DIAG}").write_bytes(text)
        except OSError:
            return
        self.kept += 1
        self.dropped[scenario] = from_crashes + from_driver + from_log + from_onboarding

    def recording(self, folder, scenario, token, notes):
        """The recording's notes (Recorder.notes: each failed conversion and avconvert's error class), added to the
        scenario's private diag after the conversion, within DIAG_RESERVE; never an error."""
        section, dropped = kept_lines("".join(note + "\n" for note in notes).encode(), token)
        if not section:
            return
        section = b"== recording ==\n" + whole_lines(section, DIAG_RESERVE // 2 - 64)
        path = folder / f"{scenario}{device_tests.DIAG}"
        try:
            had = path.is_file() and not path.is_symlink()
            if not had and path.exists():
                return
            text = (path.read_bytes() if had else b"") + section
            path.write_bytes(text[:device_tests.MAX_DIAG].decode("utf-8", "ignore").encode())
        except OSError:
            return
        self.kept += not had
        self.dropped[scenario] = self.dropped.get(scenario, 0) + dropped


def whole_lines(data, limit, newest=False):
    """At most limit bytes of data in whole lines: its first, or with newest its last."""
    lines, kept, size = data.splitlines(keepends=True), [], 0
    for line in reversed(lines) if newest else lines:
        if size + len(line) > limit:
            break
        kept.append(line)
        size += len(line)
    return b"".join(reversed(kept) if newest else kept)


def last_lines(path, log):
    """The last ONBOARDING_LINES of a private Maestro log, which is then added to the run's private log and removed:
    b"" when there is none."""
    try:
        if path.is_symlink() or not path.is_file():
            return b""
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 64 * 1024))
            data = stream.read()
        with log.open("ab") as output:
            output.write(data)
        path.unlink()
    except OSError:
        return b""
    return b"".join(data.splitlines(keepends=True)[-ONBOARDING_LINES:])


def screenshot(udid, env, path, log):
    if renders.quiet(["xcrun", "simctl", "io", udid, "screenshot", "--type=png", str(path)], log,
                     env=env).returncode or not path.is_file():
        path.unlink(missing_ok=True)
        return None
    return path.name


def boot(log, env):
    udid = renders.simulator(log, env)
    renders.quiet(["xcrun", "simctl", "boot", udid], log, env=env)
    if renders.quiet(["xcrun", "simctl", "bootstatus", udid, "-b"], log, env=env).returncode:
        raise DeviceTestsError("The iOS simulator did not boot; no scenario ran")
    return udid


def diagnostics_wanted():
    return os.environ.get(DIAGNOSTICS, "").strip() != "false"


def log_changes_wanted():
    return os.environ.get(LOG_CHANGES, "").strip() == "true"


def loop_census_wanted():
    return os.environ.get(LOOP_CENSUS, "").strip() == "true"


def launch(udid, env, token, log):
    """The app, stopped and started again with a fresh test session in its launch environment (never an argument);
    signed out, with no token (the smoke scenario). SHOW_TOUCHES draws each tap in the app (may-app #1194, May
    Playground alone), as Android's show_touches does, since the simulator's recording never shows touches. The
    watchdog is asked for unless the run turned it off (WATCHDOG_LAUNCH), SwiftUI's change log only when the run
    turned it on (LOG_CHANGES_LAUNCH), the loop census only when the run turned it on (LOOP_CENSUS_LAUNCH)."""
    renders.quiet(["xcrun", "simctl", "terminate", udid, BUNDLE], log, env=env)
    env = dict(env, **WATCHDOG_LAUNCH) if diagnostics_wanted() else env
    env = dict(env, **LOG_CHANGES_LAUNCH) if log_changes_wanted() else env
    env = dict(env, **LOOP_CENSUS_LAUNCH) if loop_census_wanted() else env
    launched = renders.quiet(["xcrun", "simctl", "launch", udid, BUNDLE, *SHOW_TOUCHES], log,
                             env=dict(env, SIMCTL_CHILD_MAY_TEST_SESSION=token) if token else env)
    if launched.returncode:
        raise DeviceTestsError("May Playground did not launch on the simulator")


def look_shots(scenario, record, private, folder):
    """A card row's look screenshots (device_agent.looks), copied from its agent's folder into the run's folder as
    <scenario>-look-<n>.png, in order; the names that were copied."""
    names = []
    for look in (record or {}).get("looks", []):
        source = private / "agent" / scenario / str(look.get("shot"))
        if look.get("shot") in device_agent.LOOK_SHOTS and source.is_file() and not source.is_symlink():
            name = f"{scenario}-{look['shot']}"
            shutil.copyfile(source, folder / name)
            names.append(name)
    return names


def run_scenarios(selection, minted=device_tests.mint, checked=device_tests.server_check, truth=device_tests.tagged,
                  asked=device_tests.question_truth, cleaned=device_tests.cleanup, agent=device_agent.run_agent,
                  install=device_agent.install, budgeted=None, delivered=None,
                  deleted_by=device_tests.delete_fresh, calls_left=device_tests.model_calls_left,
                  revoked_by=device_tests.revoke, diagnosed=None, noted=device_tests.commitment_note,
                  made=device_tests.open_chat, carded=device_tests.open_card):
    delivered = delivered or device_tests.message_codes
    # A selection may name Android-only scenarios too; this job runs only the iOS ones among them.
    chosen = device_tests.scenarios(selection, "ios")
    # Run 40203481816: chat ran second and wedged for 19 minutes, so poll and poll-skip found no budget left. The
    # scenarios most likely to wedge run last on iOS (coordinator, 25 Oct); the rest keep the list's order.
    chosen.sort(key=lambda row: IOS_LAST.index(row["id"]) + 1 if row["id"] in IOS_LAST else 0)
    agents, staged = device_tests.uses(chosen)
    require_step_secrets((device_tests.SIGNIN_SECRET, device_agent.OAUTH))
    secret = os.environ.pop(device_tests.SIGNIN_SECRET, "")
    claude = device_agent.settings(os.environ) if agents else (os.environ.pop(device_agent.OAUTH, ""), None)
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    literals = device_tests.app_sources(root, "ios")
    if not (root / APP / "Info.plist").is_file():
        raise DeviceTestsError("The Playground app is not built")
    base = device_tests.staging(secret, staged)
    outward = os.environ.get(device_tests.OUTWARD, "").strip() != "off"
    skipped, accounts = device_tests.switched_off(), device_tests.Accounts(chosen, minted, deleted_by, "ios", revoked_by)
    folder = root / device_tests.OUT / device_tests.run_folder(source) / "ios"
    folder.mkdir(parents=True, mode=0o700)
    device_tests.board_inputs(source, folder)
    log = private / "device-run.log"
    at("downloading Maestro")
    launcher = device_tests.maestro(root, private, log)
    at("installing Claude Code")
    cli = install(root) if agents else None
    tag, since = device_tests.run_tag("ios"), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = xcode()
    maestro_env = dict(env, MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true",
                       **device_tests.maestro_isolation(device_tests.maestro_home(root)))
    at("booting the simulator")
    udid = boot(log, env)
    if os.environ.get(REDUCE_MOTION, "").strip() == "true":
        if renders.quiet(["xcrun", "simctl", "spawn", udid, *REDUCE_MOTION_ON], log, env=env).returncode:
            print("May device tests (ios): Reduce Motion could not be turned on; the run goes on without it")
        else:
            print("May device tests (ios): Reduce Motion is on for this run")
    if diagnostics_wanted():
        print("May device tests (ios): the main-thread watchdog is on for this run")
    if log_changes_wanted():
        print("May device tests (ios): SwiftUI's change log is on for this run")
    if loop_census_wanted():
        print("May device tests (ios): the design-system loop census is on for this run")
    # Run 40232904242: SwiftUI's info lines stayed in the simulator's memory alone, so a scenario's later read kept
    # only its last minutes of body changes. They are persisted for that subsystem alone, before the app starts; the
    # read and its rules are unchanged (private diag alone).
    if renders.quiet(["xcrun", "simctl", "spawn", udid, *SWIFTUI_PERSIST], log, env=env).returncode:
        print("May device tests (ios): SwiftUI's body changes are kept for the last minutes alone in this run")
    diagnostics = (diagnosed or Diagnostics)(udid, env)
    results, records, cleaned_count = [], {}, None
    try:
        at("installing the app")
        if renders.quiet(["xcrun", "simctl", "install", udid, str(root / APP)], log, env=env).returncode:
            raise DeviceTestsError("May Playground did not install on the simulator; no scenario ran")
        budget = (budgeted or device_tests.Budget)()
        for row in chosen:
            entry = {"id": row["id"], "title": row["title"], "verdict": "skipped", "videos": [], "screenshots": [],
                     "server": []}
            results.append(entry)
            if row["outward"] and not outward:
                entry["reason"] = "outward actions are off (MAY_DEVICE_TESTS_OUTWARD)"
                continue
            if row["id"] in skipped:
                entry["reason"] = device_tests.SWITCHED_OFF
                continue
            if not budget.fits(row, device_tests.cap(row, device_agent.IOS_AGENT_SECONDS)):
                entry["reason"] = device_tests.BUDGET
                continue
            flow = device_tests.scenario_flow(row, source, private)
            if flow is not None and (not flow.is_file() or flow.is_symlink()):
                entry.update(verdict="fail", reason="may-app has no Maestro flow for this scenario")
                continue
            if flow is not None and device_tests.untagged(row, flow, "ios"):
                entry["reason"] = device_tests.UNTAGGED["ios"]
                continue
            if flow is not None and device_tests.flow_lacks_ids(row, flow, literals, "ios"):
                entry.update(verdict="fail", reason=device_tests.FLOW_IDS)
                continue
            token = accounts.session(row, base, secret, entry)
            if token is None:
                continue
            if device_tests.near_model_cap(row, base, token, calls_left):
                entry["reason"] = device_tests.MODEL_CAP
                accounts.finish(row, base, token)
                token = ""
                continue
            own = device_tests.scenario_tag(tag, row)  # the scenario's own tag
            if not (device_tests.own_chat(row, entry, base, token, own, made) and
                    device_tests.own_card(row, entry, base, token, own, carded)):
                accounts.finish(row, base, token)
                token = ""
                continue
            at(f"{row['id']}: launching the app")
            diagnostics.begin()
            launch(udid, env, token, log)
            recorder = Recorder(udid, env, row["id"], private, log)
            recorder.start()
            started = time.monotonic()
            if flow is not None:
                at(f"{row['id']}: Maestro")
                try:
                    passed = not renders.quiet([str(launcher), "--device", udid, "test", str(flow), "--format",
                                                "junit", "--output", str(folder / (row["id"] + ".xml"))], log,
                                               env=device_tests.with_session(
                                                   maestro_env, token),
                                               timeout=device_tests.FLOW_SECONDS).returncode
                except subprocess.SubprocessError:
                    passed = False
            onboarding_log = private / f"{row['id']}-onboarding.log"
            onboarded = device_tests.finish_onboarding(row, source, "ios", lambda path: not renders.quiet(
                [str(launcher), "--device", udid, "test", str(path)], onboarding_log,
                env=maestro_env,
                timeout=device_tests.ONBOARDING_SECONDS).returncode)
            onboarding = last_lines(onboarding_log, log) if onboarded is False else b""
            if row["driver"] != "maestro":
                at(f"{row['id']}: the agent")
                device = {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": udid, "MAY_DEVICE_MAESTRO": str(launcher),
                          "MAY_DEVICE_BUNDLE": BUNDLE, "DEVELOPER_DIR": str(renders.XCODE),
                          **({"JAVA_HOME": env["JAVA_HOME"]} if env.get("JAVA_HOME") else {}),
                          **device_tests.steps(row),
                          **({device_tests.device_mcp.LOOK_ENV: "chat"} if row.get("card") else {})}
                records[row["id"]] = agent(cli, row["goal"], own, device, private / "agent" / row["id"], claude[0],
                                           claude[1], seconds=device_tests.agent_seconds(row, device_agent.IOS_AGENT_SECONDS))
            entry["seconds"] = int(time.monotonic() - started)
            at(f"{row['id']}: screenshot, recording and server reads")
            shot = screenshot(udid, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = look_shots(row["id"], records.get(row["id"]), private, folder) + \
                ([shot] if shot else [])
            recorder.halt()
            # Run 40181517145: poll's server read came after the conversion, half an hour past its session's mint,
            # and got 401. The server reads and the diagnostics come first; the conversion, however long, after.
            device_tests.server_reads(row, entry, base, token, own, checked, truth, asked, delivered, noted)
            # After the server reads, which need the session while it lasts: a slow log read costs them nothing.
            diagnostics.keep(folder, row["id"], token, private / "agent" / row["id"] / device_tests.device_mcp.DRIVER_FILE,
                             onboarding=onboarding)
            accounts.finish(row, base, token)
            token = ""
            entry["videos"] = recorder.stop(folder)
            diagnostics.recording(folder, row["id"], "", recorder.notes)
            if recorder.dropped:
                scenario, why = row["id"], recorder.dropped
                print(f"May device tests (ios): {scenario}: its recording was dropped ({why})")
            if recorder.kept:
                scenario, why = row["id"], recorder.kept
                print(f"May device tests (ios): {scenario}: its recording was kept {why}")
            device_tests.settle(entry, row, passed if flow is not None else None)
    finally:
        cleaned_count = device_tests.tag_cleanup(accounts, base, secret, since, tag, minted, cleaned)
        accounts.abandon(base)
        token = secret = ""
        device_tests.forget_maestro_home(root)
        renders.quiet(["xcrun", "simctl", "shutdown", udid], log, env=env)
    at("writing the report")
    report = {"run": folder.parent.name, "device": "ios", "scenarios": results}
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_private(private / device_tests.AGENTS, json.dumps({"tag": tag, "agents": records}).encode())
    write_private(private / device_tests.VERDICTS, json.dumps([entry["verdict"] for entry in results]).encode())
    ran = sum(1 for entry in results if entry["verdict"] != "skipped")
    print(f"May device tests (ios): {ran} scenarios ran; verdicts follow the judge step")
    kept, stalls, longest, crashes = diagnostics.kept, diagnostics.stalls, diagnostics.longest, diagnostics.crashes
    drivers = diagnostics.drivers
    if kept and diagnostics_wanted():
        print(f"May device tests (ios): diagnostics kept for {kept} scenarios ({stalls} main-thread stalls, the longest "
              f"{longest} ms; {crashes} with a crash; {drivers} with refused device acts)")
    elif kept:
        print(f"May device tests (ios): diagnostics kept for {kept} scenarios (main-thread stalls not collected: "
              f"dispatched with ios_diagnostics off; {crashes} with a crash; {drivers} with refused device acts)")
    for name in SPAN_NAMES:
        if name in diagnostics.spans:
            count, longest = diagnostics.spans[name]
            print(f"May device tests (ios): main-thread span {name}: {count}, the longest {longest} ms")
    for name in (*STALL_AFTER_NAMES, "other"):
        if name in diagnostics.after:
            count = diagnostics.after[name]
            print(f"May device tests (ios): main-thread stall-after {name}: {count}")
    wedged = sum(1 for record in records.values() if isinstance(record, dict) and record.get("wedged") is True)
    if wedged:
        print(f"May device tests (ios): {wedged} scenarios stopped after the device stopped taking acts (fail-fast)")
    for scenario, dropped in diagnostics.dropped.items():
        print(f"May device tests (ios): {scenario} diagnostics: {dropped} lines dropped")
    if cleaned_count is not None:
        print(f"May device tests (ios): the test session's cleanup removed {cleaned_count} tagged items")
    elif accounts.shared:
        print("May device tests (ios): the test session's cleanup did not answer; tagged items may remain")
    accounts.report("ios")
