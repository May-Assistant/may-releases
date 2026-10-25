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
import signal
import subprocess
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
COMPILE_SECONDS = 300
# A launch argument (UserDefaults' argument domain), never the environment: may-app #1194's in-app touch overlay.
SHOW_TOUCHES = ("-MayShowTouches", "YES")
# The coordinator's A/B switch (25 Oct): with ios_reduce_motion dispatched true, the simulator's Reduce Motion is on
# before the app first starts, which stops the chat header's mark redrawing every frame (N's audit).
REDUCE_MOTION = "MAY_IOS_REDUCE_MOTION"
REDUCE_MOTION_ON = ("defaults", "write", "com.apple.Accessibility", "ReduceMotionEnabled", "-bool", "true")
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

    def encode(video, target):
        return not renders.quiet([str(binary), str(video), str(target), str(device_tests.PREVIEW_WIDTH),
                                  str(device_tests.PREVIEW_MAX_SECONDS), str(device_tests.PREVIEW_FPS),
                                  str(device_tests.PREVIEW_BITRATE)], log,
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
    """The simulator's screen for one scenario (simctl recordVideo until SIGINT), then converted (CONVERSIONS) for
    publishing under MAX_RECORDING, or kept as recorded when no conversion works and it fits."""

    def __init__(self, udid, env, scenario, private, log):
        self.udid, self.env, self.scenario, self.log = udid, env, scenario, log
        self.raw = private / (scenario + "-recording.mov")
        self.process = None
        # Why stop() kept no recording, in fixed words and a size in MB, for the public log (None when one was kept),
        # and how a recording was kept when it is not the usual conversion.
        self.dropped = self.kept = None
        self.private = private
        self.started = self.stopped = None

    def length(self):
        """How long the recording ran, for the public log ("" when it was never started here)."""
        return f" after {round(self.stopped - self.started)} s of recording" if self.started is not None else ""

    def start(self):
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264",
                                             "--force", str(self.raw)], stdout=output, stderr=output,
                                            stdin=subprocess.DEVNULL, env=self.env)
        self.started = time.monotonic()

    def stop(self, folder):
        if self.process is None:
            return []
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.dropped = "the recorder did not stop"
            return []
        self.stopped = time.monotonic()
        seconds = int(self.stopped - self.started) if self.started is not None else 0
        out = folder / (self.scenario + ".mp4")
        # Smoke run 39683880243: the M4V preset's conversion to .mp4 failed. Each preset writes the file type it
        # supports, in the private folder; the first one that works and fits the cap is published as .mp4 (an M4V is
        # an MPEG-4 file). When none does, the original H.264 recording is published if it fits.
        # Run 40035089599 dropped five recordings with "no conversion worked", which did not say whether avconvert
        # failed or wrote a file still over the cap; each preset's outcome is now named, in fixed words and MB.
        outcomes, smallest = [], None  # smallest: (bytes, preset, suffix) of a conversion that was over the cap
        for preset, suffix in CONVERSIONS:
            converted = self.private / (self.scenario + "-converted" + suffix)
            failed = self.convert(["--preset", preset], converted)
            if not failed and converted.is_file() and converted.stat().st_size <= MAX_RECORDING:
                converted.replace(out)
                self.raw.unlink(missing_ok=True)
                return [out.name]
            outcomes.append(f"{preset} " + outcome(failed, converted))
            if not failed and converted.is_file() and (smallest is None or converted.stat().st_size < smallest[0]):
                smallest = (converted.stat().st_size, preset, suffix)
            converted.unlink(missing_ok=True)
        tried = ", ".join(outcomes)
        if self.raw.is_file() and self.raw.stat().st_size <= MAX_RECORDING:
            self.raw.replace(out)
            self.kept = f"unconverted: no conversion fit the cap ({tried}){self.length()}"
            return [out.name]
        tail, tried_tail = self.tail(out, seconds, smallest)
        if tail:
            self.raw.unlink(missing_ok=True)
            self.kept = f"as its last {tail} s alone: the whole did not fit the cap ({tried}){self.length()}"
            return [out.name]
        size = megabytes(self.raw) if self.raw.is_file() else 0
        self.raw.unlink(missing_ok=True)
        self.dropped = (f"no conversion fit the cap ({tried}) and the original is {size} MB, over it" +
                        (f"; {tried_tail}" if tried_tail else "") + self.length())
        return []

    def convert(self, options, converted):
        """One avconvert of the raw recording to converted: its exit status, or "timed out" past CONVERT_SECONDS."""
        with self.log.open("ab") as output:
            try:
                return subprocess.run(["avconvert", "--source", str(self.raw), "--output", str(converted), *options,
                                       "--replace"], stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                      env=self.env, timeout=CONVERT_SECONDS).returncode
            except subprocess.TimeoutExpired:
                return "timed out"

    def tail(self, out, seconds, smallest):
        """The recording's last part converted to out with smallest's preset, sized to fit the cap: (its length in
        seconds, or 0 when none was kept; how a tail that was tried and refused came out, for the log, or None)."""
        if smallest is None or not self.raw.is_file():
            return 0, None
        size, preset, suffix = smallest
        tail = max(MIN_TAIL_SECONDS, int(seconds * MAX_RECORDING * TAIL_MARGIN / size))
        if tail >= seconds:
            return 0, None
        converted = self.private / (self.scenario + "-tail" + suffix)
        failed = self.convert(["--preset", preset, "--start", str(seconds - tail), "--duration", str(tail)], converted)
        if not failed and converted.is_file() and converted.stat().st_size <= MAX_RECORDING:
            converted.replace(out)
            return tail, None
        tried = f"its last {tail} s alone: {outcome(failed, converted)}"
        converted.unlink(missing_ok=True)
        return 0, tried


def megabytes(path):
    return path.stat().st_size // (1024 * 1024)


def outcome(failed, converted):
    """One conversion's outcome for the public log, in fixed words and MB."""
    return (f"failed (exit {failed})" if type(failed) is int and failed else "failed (timed out)" if failed else
            f"{megabytes(converted)} MB" if converted.is_file() else "wrote no file")


# may-app #1272: the debug Playground's main-thread watchdog logs one line per stall of 250 ms or more, alone in its
# category. The predicate names the Playground's own subsystem, so the log of no other process is ever read.
WATCHDOG = 'subsystem == "io.github.mayassistant.may.playground" AND category == "watchdog"'
STALL = re.compile(rb"main-thread stall ([0-9]{1,7}) ms")
# may-app #1281: beside the stalls, one line per named span of main-thread work of 250 ms or more, in the same category
# ("main-thread span event-store 1180 ms"). Every span line is kept, in the log's own order among the stalls; the public
# log counts the names agreed with N alone, each with its longest: five from #1281, and three on chat's path (the
# chat's publish, its inbox rows and each transcript page's merge), after run 40164384433's chat stalled every 3 s.
SPAN = re.compile(rb"main-thread span ([a-z-]{1,40}) ([0-9]{1,7}) ms[ \t]*\r?\n?$")
SPAN_NAMES = ("event-store", "recognizer", "dictation-capability", "calendar-access", "agenda-state", "chat-publish",
              "chat-inbox", "chat-merge")
MAX_SPAN_SECTION = device_tests.MAX_DIAG // 8
# The simulator's crash reports land in the host's own folder; only the Playground's process is kept.
CRASH_REPORTS = Path.home() / "Library" / "Logs" / "DiagnosticReports"
CRASH_REPORT = re.compile(r"MayPlayground[-_][A-Za-z0-9._-]{1,120}\.(?:ips|crash)")
# The file name alone is not proof (the security read of #115): a report is kept only when its own header names the
# Playground's bundle id, as an .ips report's JSON first line ("bundleID") or a .crash report's Identifier line does.
OWN_REPORT = re.compile(rb'"bundleID"[ \t]*:[ \t]*"' + re.escape(BUNDLE.encode()) + rb'"|^Identifier:[ \t]+' +
                        re.escape(BUNDLE.encode()) + rb"[ \t]*\r?$", re.MULTILINE)
REPORT_HEAD = 16 * 1024


def show_log(udid, env, start):
    """The simulator's unified log since start, the watchdog's lines alone; b"" when it could not be read."""
    done = device_tests.bounded_output(["xcrun", "simctl", "spawn", udid, "log", "show", "--style", "compact",
                                        "--start", start, "--predicate", WATCHDOG], env, limit=device_tests.MAX_DIAG)
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

    def keep(self, folder, scenario, token, driver=None):
        """Writes <scenario>.diag.txt when there is anything to keep; never an error. driver: the agent's DRIVER_FILE,
        each refused act's Maestro lines (device_mcp.refused)."""
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
        lines = [line for line in (self.shown(self.udid, self.env, self.start) if self.start else b"")
                 .splitlines(keepends=True) if STALL.search(line) or SPAN.search(line)]
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
        self.drivers += bool(refused)
        # Counted from what kept_lines kept, so a span line it dropped is neither published nor counted.
        spans = [line for line in watchdog.splitlines(keepends=True) if SPAN.search(line)]
        for line in spans:
            name, ms = SPAN.search(line).group(1).decode(), int(SPAN.search(line).group(2))
            if name in SPAN_NAMES:
                count, longest = self.spans.get(name, [0, 0])
                self.spans[name] = [count + 1, max(longest, ms)]
        crashes = crashes[:device_tests.MAX_DIAG // 2]
        room = device_tests.MAX_DIAG - len(crashes) - len(refused) - 192  # the section headers' room
        # The watchdog's newest lines fill the rest; when that trims any, every span line goes ahead of it too.
        trimmed = len(watchdog) > room
        section = b"".join(spans)[:MAX_SPAN_SECTION] if trimmed else b""
        room -= len(section)
        watchdog = watchdog[len(watchdog) - room:] if len(watchdog) > room else watchdog
        if not (crashes or refused or watchdog):
            return
        text = (crashes + (b"== driver ==\n" + refused if refused else b"") +
                (b"== spans ==\n" + section if section else b"") +
                (b"== watchdog ==\n" + watchdog if watchdog else b"")).decode("utf-8", "replace").encode()
        text = text[:device_tests.MAX_DIAG].decode("utf-8", "ignore").encode()
        try:
            (folder / f"{scenario}{device_tests.DIAG}").write_bytes(text)
        except OSError:
            return
        self.kept += 1
        self.dropped[scenario] = from_crashes + from_driver + from_log


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


def launch(udid, env, token, log):
    """The app, stopped and started again with a fresh test session in its launch environment (never an argument);
    signed out, with no token (the smoke scenario). SHOW_TOUCHES draws each tap in the app (may-app #1194, May
    Playground alone), as Android's show_touches does, since the simulator's recording never shows touches."""
    renders.quiet(["xcrun", "simctl", "terminate", udid, BUNDLE], log, env=env)
    launched = renders.quiet(["xcrun", "simctl", "launch", udid, BUNDLE, *SHOW_TOUCHES], log,
                             env=dict(env, SIMCTL_CHILD_MAY_TEST_SESSION=token) if token else env)
    if launched.returncode:
        raise DeviceTestsError("May Playground did not launch on the simulator")


def run_scenarios(selection, minted=device_tests.mint, checked=device_tests.server_check, truth=device_tests.tagged,
                  asked=device_tests.question_truth, cleaned=device_tests.cleanup, agent=device_agent.run_agent,
                  install=device_agent.install, budgeted=None, delivered=None,
                  deleted_by=device_tests.delete_fresh, calls_left=device_tests.model_calls_left,
                  revoked_by=device_tests.revoke, diagnosed=None, noted=device_tests.commitment_note):
    delivered = delivered or device_tests.message_codes
    # A selection may name Android-only scenarios too; this job runs only the iOS ones among them.
    chosen = device_tests.scenarios(selection, "ios")
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
    tag, since = device_tests.run_tag(), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            device_tests.finish_onboarding(row, source, "ios", lambda path: not renders.quiet(
                [str(launcher), "--device", udid, "test", str(path)], log,
                env=maestro_env,
                timeout=device_tests.ONBOARDING_SECONDS).returncode)
            if row["driver"] != "maestro":
                at(f"{row['id']}: the agent")
                device = {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": udid, "MAY_DEVICE_MAESTRO": str(launcher),
                          "MAY_DEVICE_BUNDLE": BUNDLE, "DEVELOPER_DIR": str(renders.XCODE),
                          **({"JAVA_HOME": env["JAVA_HOME"]} if env.get("JAVA_HOME") else {})}
                records[row["id"]] = agent(cli, row["goal"], own, device, private / "agent" / row["id"], claude[0],
                                           claude[1], seconds=device_agent.IOS_AGENT_SECONDS)
            entry["seconds"] = int(time.monotonic() - started)
            at(f"{row['id']}: screenshot, recording and server reads")
            shot = screenshot(udid, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = [shot] if shot else []
            entry["videos"] = recorder.stop(folder)
            if recorder.dropped:
                scenario, why = row["id"], recorder.dropped
                print(f"May device tests (ios): {scenario}: its recording was dropped ({why})")
            elif recorder.kept:
                scenario, why = row["id"], recorder.kept
                print(f"May device tests (ios): {scenario}: its recording was kept {why}")
            device_tests.server_reads(row, entry, base, token, own, checked, truth, asked, delivered, noted)
            # After the server reads, which need the session while it lasts: a slow log read costs them nothing.
            diagnostics.keep(folder, row["id"], token, private / "agent" / row["id"] / device_tests.device_mcp.DRIVER_FILE)
            accounts.finish(row, base, token)
            token = ""
            device_tests.settle(entry, row, passed if flow is not None else None)
        if accounts.shared:
            at("cleanup")
            try:
                token = accounts.track(minted(base, secret))  # signed out by abandon, below
            except device_tests.MintFailed:
                token = ""  # read as a cleanup that did not answer: the report is kept
            cleaned_count = cleaned(base, token, since, tag) if token else None
    finally:
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
    if kept:
        print(f"May device tests (ios): diagnostics kept for {kept} scenarios ({stalls} main-thread stalls, the longest "
              f"{longest} ms; {crashes} with a crash; {drivers} with refused device acts)")
    for name in SPAN_NAMES:
        if name in diagnostics.spans:
            count, longest = diagnostics.spans[name]
            print(f"May device tests (ios): main-thread span {name}: {count}, the longest {longest} ms")
    for scenario, dropped in diagnostics.dropped.items():
        print(f"May device tests (ios): {scenario} diagnostics: {dropped} lines dropped")
    if cleaned_count is not None:
        print(f"May device tests (ios): the test session's cleanup removed {cleaned_count} tagged items")
    elif accounts.shared:
        print("May device tests (ios): the test session's cleanup did not answer; tagged items may remain")
    accounts.report("ios")
