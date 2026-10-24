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
import signal
import subprocess
import time

import device_agent
import device_tests
from device_tests import DeviceTestsError, at
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
COMPILE_SECONDS = 300
# A launch argument (UserDefaults' argument domain), never the environment: may-app #1194's in-app touch overlay.
SHOW_TOUCHES = ("-MayShowTouches", "YES")
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
        return not renders.quiet([str(binary), str(video), str(target), str(device_tests.PREVIEW_WIDTH)], log,
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
        self.started = None

    def length(self):
        """How long the recording ran, for the public log ("" when it was never started here)."""
        return f" after {round(time.monotonic() - self.started)} s of recording" if self.started is not None else ""

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
        out = folder / (self.scenario + ".mp4")
        # Smoke run 39683880243: the M4V preset's conversion to .mp4 failed. Each preset writes the file type it
        # supports, in the private folder; the first one that works and fits the cap is published as .mp4 (an M4V is
        # an MPEG-4 file). When none does, the original H.264 recording is published if it fits.
        # Run 40035089599 dropped five recordings with "no conversion worked", which did not say whether avconvert
        # failed or wrote a file still over the cap; each preset's outcome is now named, in fixed words and MB.
        outcomes = []
        for preset, suffix in CONVERSIONS:
            converted = self.private / (self.scenario + "-converted" + suffix)
            with self.log.open("ab") as output:
                failed = subprocess.run(["avconvert", "--source", str(self.raw), "--output", str(converted),
                                         "--preset", preset, "--replace"], stdout=output, stderr=output,
                                        stdin=subprocess.DEVNULL, env=self.env).returncode
            if not failed and converted.is_file() and converted.stat().st_size <= MAX_RECORDING:
                converted.replace(out)
                self.raw.unlink(missing_ok=True)
                return [out.name]
            outcomes.append(f"{preset} " + (f"failed (exit {failed})" if failed else
                                            f"{megabytes(converted)} MB" if converted.is_file() else "wrote no file"))
            converted.unlink(missing_ok=True)
        tried = ", ".join(outcomes)
        if self.raw.is_file() and self.raw.stat().st_size <= MAX_RECORDING:
            self.raw.replace(out)
            self.kept = f"unconverted: no conversion fit the cap ({tried}){self.length()}"
            return [out.name]
        size = megabytes(self.raw) if self.raw.is_file() else 0
        self.raw.unlink(missing_ok=True)
        self.dropped = f"no conversion fit the cap ({tried}) and the original is {size} MB, over it{self.length()}"
        return []


def megabytes(path):
    return path.stat().st_size // (1024 * 1024)


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
                  deleted_by=device_tests.delete_fresh):
    delivered = delivered or device_tests.message_codes
    # A selection may name Android-only scenarios too; this job runs only the iOS ones among them.
    chosen = device_tests.scenarios(selection, "ios")
    agents, staged = device_tests.uses(chosen)
    require_step_secrets((device_tests.SIGNIN_SECRET, device_agent.OAUTH))
    secret = os.environ.pop(device_tests.SIGNIN_SECRET, "")
    claude = device_agent.settings(os.environ) if agents else (os.environ.pop(device_agent.OAUTH, ""), None)
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not (root / APP / "Info.plist").is_file():
        raise DeviceTestsError("The Playground app is not built")
    base = device_tests.staging(secret, staged)
    outward = os.environ.get(device_tests.OUTWARD, "").strip() != "off"
    skipped, accounts = device_tests.switched_off(), device_tests.Accounts(chosen, minted, deleted_by)
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
    at("booting the simulator")
    udid = boot(log, env)
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
            token = accounts.session(row, base, secret, entry)
            if token is None:
                continue
            at(f"{row['id']}: launching the app")
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
                                                   dict(env, MAESTRO_CLI_NO_ANALYTICS="1",
                                                        MAESTRO_DISABLE_UPDATE_CHECK="true"), token),
                                               timeout=device_tests.FLOW_SECONDS).returncode
                except subprocess.SubprocessError:
                    passed = False
            device_tests.finish_onboarding(row, source, "ios", lambda path: not renders.quiet(
                [str(launcher), "--device", udid, "test", str(path)], log,
                env=dict(env, MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true"),
                timeout=device_tests.ONBOARDING_SECONDS).returncode)
            if row["driver"] != "maestro":
                at(f"{row['id']}: the agent")
                device = {"MAY_DEVICE_PLATFORM": "ios", "MAY_DEVICE_UDID": udid, "MAY_DEVICE_MAESTRO": str(launcher),
                          "MAY_DEVICE_BUNDLE": BUNDLE, "DEVELOPER_DIR": str(renders.XCODE),
                          **({"JAVA_HOME": env["JAVA_HOME"]} if env.get("JAVA_HOME") else {})}
                records[row["id"]] = agent(cli, row["goal"], tag, device, private / "agent" / row["id"], claude[0],
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
            for check in row["server"]:
                status = checked(base, token, check["get"])
                entry["server"].append({"get": check["get"], "status": status, "pass": status == check["expect"]})
            for check in row.get("truth", []):
                if "question" in check:
                    path, (status, found) = "/v1/agents", asked(base, token, check["question"], tag)
                else:
                    path, (status, found) = check["get"], truth(base, token, check["get"], tag)
                entry["server"].append({"get": path, "status": status, "pass": status == 200 and found})
            if device_tests.sends_messages(row):
                codes = delivered(base, token, tag)
                if codes is not None:
                    entry["delivery"] = codes
            accounts.finish(row, base, token)
            token = ""
            device_tests.settle(entry, row, passed if flow is not None else None)
        if accounts.shared:
            at("cleanup")
            token = minted(base, secret)
            cleaned_count = cleaned(base, token, since, tag)
    finally:
        accounts.abandon(base)
        token = secret = ""
        renders.quiet(["xcrun", "simctl", "shutdown", udid], log, env=env)
    at("writing the report")
    report = {"run": folder.parent.name, "device": "ios", "scenarios": results}
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_private(private / device_tests.AGENTS, json.dumps({"tag": tag, "agents": records}).encode())
    write_private(private / device_tests.VERDICTS, json.dumps([entry["verdict"] for entry in results]).encode())
    ran = sum(1 for entry in results if entry["verdict"] != "skipped")
    print(f"May device tests (ios): {ran} scenarios ran; verdicts follow the judge step")
    if cleaned_count is not None:
        print(f"May device tests (ios): the test session's cleanup removed {cleaned_count} tagged items")
    elif accounts.shared:
        print("May device tests (ios): the test session's cleanup did not answer; tagged items may remain")
    accounts.report("ios")
