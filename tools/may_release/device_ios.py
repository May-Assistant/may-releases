"""May Playground device tests on the iOS simulator (device-tests.yml, the ios job): device_tests.py's twin of the
Android job, with the same scenario list, verdicts, report and seal.

- build: the pinned Xcode and XcodeGen (renders.xcode_env, as ios-renders builds), then a debug May Playground for
  the simulator, unsigned. No secret is in the step.
- run_scenarios: an iPhone 16 Pro simulator on the newest iOS runtime (renders.simulator), booted, the app installed, and each
  of the platform's scenarios: a fresh staging test session (device_tests.mint), the app launched with it in
  SIMCTL_CHILD_MAY_TEST_SESSION (N's hook, may-app #921: the debug Playground reads MAY_TEST_SESSION at launch, never
  as an argument), the screen recorded (simctl recordVideo, then avconvert to 720p so a recording stays well under
  publish.py's 40 MB a file), the scenario's Maestro flow (the same flows as Android: one bundle id), a screenshot and
  its read-only server checks.

Agent scenarios are Android's alone for now: the simulator has no adb, and an input tool for it (idb or Maestro's
driver) is a later decision. The public log carries scenario ids, verdict words and counts only.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import time

import device_tests
from device_tests import DeviceTestsError
import renders
from store import child_env, require_step_secrets, runner_root, write_private

SCHEME = "MayPlayground"
APP = "may-derived/Build/Products/Debug-iphonesimulator/MayPlayground.app"
BUNDLE = device_tests.APP_ID
MAX_RECORDING = 38 * 1024 * 1024
STOP_SECONDS = 30


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
              "-clonedSourcePackagesDirPath", str(root / "may-packages"), "CODE_SIGNING_ALLOWED=NO", "build"],
             log, env=env)
    except renders.RendersError:
        raise DeviceTestsError("The Playground app did not build for the simulator; no scenario ran") from None
    if not (root / APP / "Info.plist").is_file():
        raise DeviceTestsError("The Playground app did not build for the simulator; no scenario ran")
    print("May device tests: Playground debug app built for the iOS simulator")


def xcode():
    env = child_env(DEVELOPER_DIR=str(renders.XCODE))
    # Maestro runs on the runner's Java 17 when the image names one.
    java = os.environ.get("JAVA_HOME_17_arm64") or os.environ.get("JAVA_HOME_17_X64")
    if java:
        env["JAVA_HOME"] = java
    return env


class Recorder:
    """The simulator's screen for one scenario (simctl recordVideo until SIGINT), then made 720p for publishing."""

    def __init__(self, udid, env, scenario, private, log):
        self.udid, self.env, self.scenario, self.log = udid, env, scenario, log
        self.raw = private / (scenario + "-recording.mov")
        self.process = None

    def start(self):
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264",
                                             "--force", str(self.raw)], stdout=output, stderr=output,
                                            stdin=subprocess.DEVNULL, env=self.env)

    def stop(self, folder):
        if self.process is None:
            return []
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return []
        out = folder / (self.scenario + ".mp4")
        with self.log.open("ab") as output:
            converted = subprocess.run(["avconvert", "--source", str(self.raw), "--output", str(out), "--preset",
                                        "PresetAppleM4V720pHD", "--replace"], stdout=output, stderr=output,
                                       stdin=subprocess.DEVNULL, env=self.env)
        self.raw.unlink(missing_ok=True)
        if converted.returncode or not out.is_file() or out.stat().st_size > MAX_RECORDING:
            out.unlink(missing_ok=True)
            return []
        return [out.name]


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
    """The app, stopped and started again with a fresh test session in its launch environment (never an argument)."""
    renders.quiet(["xcrun", "simctl", "terminate", udid, BUNDLE], log, env=env)
    launched = renders.quiet(["xcrun", "simctl", "launch", udid, BUNDLE], log,
                             env=dict(env, SIMCTL_CHILD_MAY_TEST_SESSION=token))
    if launched.returncode:
        raise DeviceTestsError("May Playground did not launch on the simulator")


def run_scenarios(selection, minted=device_tests.mint, checked=device_tests.server_check):
    # A selection may name Android's agent scenarios too; this job runs only the iOS ones among them.
    chosen = device_tests.scenarios(selection, "ios")
    require_step_secrets((device_tests.SIGNIN_SECRET,))
    secret = os.environ.pop(device_tests.SIGNIN_SECRET, "")
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not (root / APP / "Info.plist").is_file():
        raise DeviceTestsError("The Playground app is not built")
    base = os.environ.get(device_tests.STAGING, "").strip().rstrip("/")
    if not device_tests.STAGING_URL.fullmatch(base):
        raise DeviceTestsError("MAY_STAGING_API_URL is not an https host in may-device-tests")
    if not secret:
        raise DeviceTestsError("MAY_TEST_SIGNIN_SECRET is not set in may-device-tests; no scenario ran")
    outward = os.environ.get(device_tests.OUTWARD, "").strip() != "off"
    folder = root / device_tests.OUT / device_tests.run_folder(source) / "ios"
    folder.mkdir(parents=True, mode=0o700)
    log = private / "device-run.log"
    launcher = device_tests.maestro(root, private, log)
    env = xcode()
    udid = boot(log, env)
    results = []
    try:
        if renders.quiet(["xcrun", "simctl", "install", udid, str(root / APP)], log, env=env).returncode:
            raise DeviceTestsError("May Playground did not install on the simulator; no scenario ran")
        for row in chosen:
            entry = {"id": row["id"], "title": row["title"], "verdict": "skipped", "videos": [], "screenshots": [],
                     "server": []}
            results.append(entry)
            if row["outward"] and not outward:
                entry["reason"] = "outward actions are off (MAY_DEVICE_TESTS_OUTWARD)"
                continue
            flow = source / device_tests.FLOWS_DIR / row["flow"]
            if not flow.is_file() or flow.is_symlink():
                entry.update(verdict="fail", reason="may-app has no Maestro flow for this scenario")
                continue
            token = minted(base, secret)
            launch(udid, env, token, log)
            recorder = Recorder(udid, env, row["id"], private, log)
            recorder.start()
            started = time.monotonic()
            try:
                passed = not renders.quiet([str(launcher), "--device", udid, "test", str(flow), "--format", "junit",
                                            "--output", str(folder / (row["id"] + ".xml"))], log,
                                           env=dict(env, MAESTRO_CLI_NO_ANALYTICS="1",
                                                    MAESTRO_DISABLE_UPDATE_CHECK="true")).returncode
            except subprocess.SubprocessError:
                passed = False
            entry["seconds"] = int(time.monotonic() - started)
            shot = screenshot(udid, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = [shot] if shot else []
            entry["videos"] = recorder.stop(folder)
            for check in row["server"]:
                status = checked(base, token, check["get"])
                entry["server"].append({"get": check["get"], "status": status, "pass": status == check["expect"]})
            token = ""
            server_ok = all(check["pass"] for check in entry["server"])
            entry["verdict"] = "fail" if not passed or not server_ok else "pass" if row["server"] else "ui-pass"
            if entry["verdict"] == "ui-pass":
                entry["reason"] = device_tests.UNCONFIGURED
    finally:
        token = secret = ""
        renders.quiet(["xcrun", "simctl", "shutdown", udid], log, env=env)
    report = {"run": folder.parent.name, "device": "ios", "scenarios": results}
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_private(private / device_tests.VERDICTS, json.dumps([entry["verdict"] for entry in results]).encode())
    ran = sum(1 for entry in results if entry["verdict"] != "skipped")
    print(f"May device tests (ios): {ran} scenarios ran; verdicts follow the judge step")
