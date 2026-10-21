"""May Playground device tests against live staging (device-tests.yml): the Android job.

This repository is public and may-app is private: the public log carries the scenario ids from SCENARIOS (this
repository's own file), verdict words and counts, nothing else. Screenshots, recordings, Maestro's output and the
report leave the runner only as one age-encrypted file to MAY_RENDERS_RECIPIENT; the publish job decrypts it on a
fresh runner and publishes it to the private May-Assistant/may-renders (publish.py device).

Trust: v1 runs may-app main commits alone (prepare.py checks main history; a nightly run takes main's head). The run
step holds MAY_TEST_SIGNIN_SECRET, the staging secret that mints a short-lived session for the test account, so no
pull request's code is ever built here.

Commands, one per workflow step:
  build    the Playground debug APK (no secret in the step)
  run      start the emulator, install the app, install the pinned Maestro, mint a staging test session and hand it
           to the app (a Playground-debug-only intent extra), then each scenario: record the screen, run its Maestro
           flow (may-app's FLOWS_DIR), take screenshots and run its read-only server checks. A failed scenario does
           not fail this step, so its evidence is still published; `verdict` fails the job afterwards.
  seal     one tar of the run folder (<UTC date>-<may-app short sha>/android/), encrypted to MAY_RENDERS_RECIPIENT
  verdict  fails the job if any scenario failed

Scenarios with "outward": true (one action outside staging: the self-addressed Gmail send) run only while
MAY_DEVICE_TESTS_OUTWARD is not "off". Samuel's go (21 Oct, in session) covers that one scenario alone, with the
server's guard for test sessions as the backstop.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import flows
from gate import AGE_RECIPIENT
from prepare import head_revision
from store import StoreError, child_env, require_step_secrets, runner_root, write_private
import toolchain

SCENARIOS = Path(__file__).resolve().parent / "device_scenarios.json"
FLOWS_DIR = "tools/device-tests/flows"
SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
FLOW_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\.yaml")
SERVER_PATH = re.compile(r"/v1/[a-z0-9/_-]{1,120}")
PLATFORMS = ("android", "ios")
APP_ID = "io.github.mayassistant.may.playground"
APK = "android/app/build/outputs/apk/playground/debug/app-playground-debug.apk"
AVD = "may-device-tests"
MAESTRO_URL = "https://github.com/mobile-dev-inc/maestro/releases/download/cli-2.8.0/maestro.zip"
MAESTRO_SHA256 = "b3e561161904fb391875ca5834d5b22cf0b01c052dd1b408ad83e30d8f8951b3"
SIGNIN_SECRET, OUTWARD, STAGING, RECIPIENT = ("MAY_TEST_SIGNIN_SECRET", "MAY_DEVICE_TESTS_OUTWARD",
                                              "MAY_STAGING_API_URL", "MAY_RENDERS_RECIPIENT")
TEST_EMAIL = "harlahke@gmail.com"
STAGING_URL = re.compile(r"https://[a-z0-9][a-z0-9.-]{0,200}")
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")
# Recordings: about 720p at about 1 Mbps, in screenrecord's 3-minute chunks: under 25 MB a chunk.
RECORD_SIZE, RECORD_BITRATE, RECORD_SECONDS = "720x1600", "1000000", 180
FLOW_SECONDS = 600
OUT, SEALED, VERDICTS = "may-device-out", "may-device-sealed", "device-verdicts.json"


class DeviceTestsError(ValueError):
    pass


def scenarios(selection="all", platform="android"):
    """SCENARIOS, checked, for one platform; "all" or a comma-separated list of ids."""
    try:
        rows = json.loads(SCENARIOS.read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("The scenario list is unreadable") from None
    checked, ids = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and SCENARIO_ID.fullmatch(row["id"]) and
                row["id"] not in ids and row.get("driver") == "maestro" and isinstance(row.get("flow"), str) and
                FLOW_NAME.fullmatch(row["flow"]) and isinstance(row.get("title"), str) and
                isinstance(row.get("platforms"), list) and set(row["platforms"]) <= set(PLATFORMS) and
                isinstance(row.get("outward"), bool) and isinstance(row.get("server"), list) and
                all(isinstance(c, dict) and isinstance(c.get("get"), str) and SERVER_PATH.fullmatch(c["get"]) and
                    c.get("expect") == 200 for c in row["server"])):
            raise DeviceTestsError("The scenario list has a malformed row")
        ids.add(row["id"])
        checked.append(row)
    if not checked:
        raise DeviceTestsError("The scenario list is empty")
    wanted = None if selection == "all" else selection.split(",")
    if wanted is not None and not set(wanted) <= ids:
        raise DeviceTestsError("A selected scenario is not in the scenario list")
    return [row for row in checked if platform in row["platforms"] and (wanted is None or row["id"] in wanted)]


def run_folder(source):
    """<UTC date>-<may-app short sha>: the run's folder in may-renders' device-tests/."""
    sha = head_revision(source)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeviceTestsError("The prepared checkout has no exact commit")
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + "-" + sha[:12]


def build():
    require_step_secrets(())
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not private.is_dir():
        raise DeviceTestsError("Exact private source is not prepared")
    log = private / "device-build.log"
    env = child_env(GRADLE_USER_HOME=str(root / "may-gradle"), GIT_TERMINAL_PROMPT="0",
                    JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=" + str(root))
    flows.install_sdk(flows.sdk(), log, env)
    if flows.run_private(["./gradlew", "--no-daemon", ":app:assemblePlaygroundDebug"], log, cwd=source / "android",
                         env=env, check=False) or not (source / APK).is_file():
        raise DeviceTestsError("The Playground app did not build; no scenario ran")
    print("May device tests: Playground debug app built for the emulator")


def mint(base, secret, post=urllib.request.urlopen):
    """A short-lived staging session for the test account: POST <staging>/v1/auth/test-session. The token is held in
    memory alone; nothing from the reply is printed."""
    request = urllib.request.Request(base + "/v1/auth/test-session", method="POST",
                                     data=json.dumps({"email": TEST_EMAIL}).encode(),
                                     headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
    try:
        with post(request, timeout=30) as reply:
            data = json.loads(reply.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise DeviceTestsError("The staging test session could not be minted") from None
    token = data.get("token") if isinstance(data, dict) else None
    if not (isinstance(token, str) and TOKEN.fullmatch(token)):
        raise DeviceTestsError("The staging test session reply has no token")
    return token


def server_check(base, token, path, get=urllib.request.urlopen):
    """One read-only staging call with the test session: its HTTP status, or 0 when there was no reply."""
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    try:
        with get(request, timeout=30) as reply:
            return reply.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError):
        return 0


class Recorder:
    """The emulator's screen in screenrecord's 3-minute chunks, one after another, until stop()."""

    def __init__(self, adb, env, scenario, log):
        self.adb, self.env, self.scenario, self.log = adb, env, scenario, log
        self.stopped, self.parts, self.process = threading.Event(), 0, None
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stopped.is_set() and self.parts < 20:
            self.parts += 1
            with self.log.open("ab") as output:
                self.process = subprocess.Popen(
                    [self.adb, "shell", "screenrecord", "--size", RECORD_SIZE, "--bit-rate", RECORD_BITRATE,
                     "--time-limit", str(RECORD_SECONDS), f"/sdcard/may-rec/{self.scenario}-{self.parts}.mp4"],
                    stdout=output, stderr=output, stdin=subprocess.DEVNULL, env=self.env)
            self.process.wait()

    def start(self):
        flows.run_private([self.adb, "shell", "mkdir", "-p", "/sdcard/may-rec"], self.log, env=self.env, check=False)
        self.thread.start()

    def stop(self, folder):
        self.stopped.set()
        # SIGINT lets screenrecord finish its file.
        flows.run_private([self.adb, "shell", "pkill", "-2", "screenrecord"], self.log, env=self.env, check=False)
        if self.thread.ident is not None:
            self.thread.join(timeout=30)
        names = []
        for part in range(1, self.parts + 1):
            name = f"{self.scenario}.mp4" if self.parts == 1 else f"{self.scenario}-part{part}.mp4"
            if not flows.run_private([self.adb, "pull", f"/sdcard/may-rec/{self.scenario}-{part}.mp4",
                                      str(folder / name)], self.log, env=self.env, check=False):
                names.append(name)
        return names


def screenshot(adb, env, path, log):
    with path.open("wb") as output, log.open("ab") as errors:
        result = subprocess.run([adb, "exec-out", "screencap", "-p"], stdout=output, stderr=errors,
                                stdin=subprocess.DEVNULL, env=env)
    if result.returncode:
        path.unlink(missing_ok=True)
        return None
    return path.name


def boot(root, log):
    """The emulator, as flows.run starts it (API 35 image, KVM, no window), with animations off: (adb, process, env)."""
    home = flows.sdk()
    env = child_env(ANDROID_AVD_HOME=str(root / "may-avd"), ANDROID_EMULATOR_HOME=str(root / "may-avd"))
    adb = flows.sdk_tool(home, "platform-tools/adb")
    (root / "may-avd").mkdir(mode=0o700, exist_ok=True)
    with log.open("ab") as output:
        subprocess.run([flows.sdk_tool(home, "cmdline-tools/latest/bin/avdmanager"), "create", "avd", "--force",
                        "-n", AVD, "-k", flows.SYSTEM_IMAGE, "-d", flows.DEVICE], input=b"no\n", stdout=output,
                       stderr=output, env=env, check=True)
        emulator = subprocess.Popen([flows.sdk_tool(home, "emulator/emulator"), "-avd", AVD, "-no-window", "-no-audio",
                                     "-no-boot-anim", "-no-snapshot", "-gpu", "swiftshader_indirect"],
                                    stdout=output, stderr=output, stdin=subprocess.DEVNULL, env=env)
    flows.run_private([adb, "wait-for-device"], log, env=env, timeout=flows.BOOT_SECONDS)
    deadline = time.monotonic() + flows.BOOT_SECONDS
    while subprocess.run([adb, "shell", "getprop", "sys.boot_completed"], capture_output=True,
                         stdin=subprocess.DEVNULL, env=env).stdout.strip() != b"1":
        if time.monotonic() > deadline or emulator.poll() is not None:
            emulator.kill()
            raise DeviceTestsError("The emulator did not boot; no scenario ran")
        time.sleep(5)
    for setting in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
        flows.run_private([adb, "shell", "settings", "put", "global", setting, "0"], log, env=env)
    return adb, emulator, env


def maestro(root, private, log):
    """The pinned Maestro CLI, its archive checked against MAESTRO_SHA256: the path of its launcher."""
    archive = private / "maestro.zip"
    flows.run_private(["curl", "-fsSL", "--retry", "2", "-o", str(archive), MAESTRO_URL], log)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != MAESTRO_SHA256:
        raise DeviceTestsError("The Maestro download's hash differs; no scenario ran")
    with zipfile.ZipFile(archive) as package:
        package.extractall(root / "may-maestro")
    launcher = root / "may-maestro/maestro/bin/maestro"
    launcher.chmod(0o755)
    return launcher


def run_scenarios(selection, minted=mint, checked=server_check):
    require_step_secrets((SIGNIN_SECRET,))
    secret = os.environ.pop(SIGNIN_SECRET, "")
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not (source / APK).is_file():
        raise DeviceTestsError("The Playground app is not built")
    chosen = scenarios(selection, "android")
    base = os.environ.get(STAGING, "").strip().rstrip("/")
    if not STAGING_URL.fullmatch(base):
        raise DeviceTestsError("MAY_STAGING_API_URL is not an https host in may-device-tests")
    if not secret:
        raise DeviceTestsError("MAY_TEST_SIGNIN_SECRET is not set in may-device-tests; no scenario ran")
    outward = os.environ.get(OUTWARD, "").strip() != "off"
    folder = root / OUT / run_folder(source) / "android"
    folder.mkdir(parents=True, mode=0o700)
    log = private / "device-run.log"
    launcher = maestro(root, private, log)
    adb, emulator, env = boot(root, log)
    results = []
    try:
        flows.run_private([adb, "install", "-r", "-t", str(source / APK)], log, env=env)
        token = minted(base, secret)
        secret = ""
        flows.run_private([adb, "shell", "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c",
                           "android.intent.category.LAUNCHER", "-p", APP_ID, "--es", "may_test_session", token],
                          log, env=env)
        maestro_env = child_env(MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true",
                                ANDROID_HOME=str(flows.sdk()))
        for row in chosen:
            entry = {"id": row["id"], "title": row["title"], "verdict": "skipped", "videos": [], "screenshots": [],
                     "server": []}
            results.append(entry)
            if row["outward"] and not outward:
                entry["reason"] = "outward actions are off (MAY_DEVICE_TESTS_OUTWARD)"
                continue
            flow = source / FLOWS_DIR / row["flow"]
            if not flow.is_file() or flow.is_symlink():
                entry.update(verdict="fail", reason="may-app has no Maestro flow for this scenario")
                continue
            recorder = Recorder(adb, env, row["id"], log)
            recorder.start()
            started = time.monotonic()
            try:
                passed = not flows.run_private(
                    [str(launcher), "--device", "emulator-5554", "test", str(flow), "--format", "junit", "--output",
                     str(folder / (row["id"] + ".xml"))], log, env=maestro_env, check=False, timeout=FLOW_SECONDS)
            except subprocess.TimeoutExpired:
                passed = False
            entry["seconds"] = int(time.monotonic() - started)
            shot = screenshot(adb, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = [shot] if shot else []
            entry["videos"] = recorder.stop(folder)
            for check in row["server"]:
                status = checked(base, token, check["get"])
                entry["server"].append({"get": check["get"], "status": status, "pass": status == check["expect"]})
            server_ok = all(check["pass"] for check in entry["server"])
            entry["verdict"] = ("fail" if not passed or not server_ok else "pass" if row["server"] else "ui-pass")
            if entry["verdict"] == "ui-pass":
                entry["reason"] = "server checks not configured"
    finally:
        token = ""
        emulator.terminate()
        try:
            emulator.wait(timeout=60)
        except subprocess.TimeoutExpired:
            emulator.kill()
    report = {"run": folder.parent.name, "device": "android", "scenarios": results}
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_private(private / VERDICTS, json.dumps([entry["verdict"] for entry in results]).encode())
    for entry in results:
        scenario, word = entry["id"], entry["verdict"]
        print(f"May device tests (android): {scenario}: {word}")
    counts = {word: sum(1 for entry in results if entry["verdict"] == word) for word in ("pass", "ui-pass", "fail",
                                                                                          "skipped")}
    passed, ui_passed, failed, skipped = counts["pass"], counts["ui-pass"], counts["fail"], counts["skipped"]
    print(f"May device tests (android): {passed} passed, {ui_passed} passed on screen alone (server checks not "
          f"configured), {failed} failed, {skipped} skipped")


def seal():
    """One tar of the run folder, encrypted to MAY_RENDERS_RECIPIENT; nothing in the clear leaves the runner."""
    require_step_secrets(())
    root = runner_root()
    out = root / OUT
    runs = [path for path in out.iterdir() if path.is_dir()] if out.is_dir() else []
    if len(runs) != 1 or not (runs[0] / "android" / "report.json").is_file():
        raise DeviceTestsError("There is no report to seal")
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("May device tests: MAY_RENDERS_RECIPIENT is not set in may-device-tests; nothing was sealed, so nothing "
              "will be published")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise DeviceTestsError("MAY_RENDERS_RECIPIENT is not an age public key")
    sealed = root / SEALED
    sealed.mkdir(mode=0o700)
    plain = sealed / "android.tar"
    try:
        with tarfile.open(plain, "w") as archive:
            for path in sorted(out.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise DeviceTestsError("The run folder holds something other than files and folders")
                archive.add(path, arcname=path.relative_to(out).as_posix(), recursive=False)
        result = subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(sealed / "android.age"),
                                 str(plain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=child_env())
        if result.returncode:
            raise DeviceTestsError("The report could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print("May device tests: the android report encrypted for the publish job")


def verdict():
    require_step_secrets(())
    try:
        verdicts = json.loads((runner_root() / "may-private" / VERDICTS).read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("No scenario verdicts are recorded") from None
    if not isinstance(verdicts, list) or "fail" in verdicts:
        raise DeviceTestsError("A scenario failed")
    print("May device tests (android): no scenario failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "run", "seal", "verdict"))
    parser.add_argument("--scenarios", default="all")
    args = parser.parse_args()
    try:
        if args.command == "run":
            run_scenarios(args.scenarios or "all")
        else:
            {"build": build, "seal": seal, "verdict": verdict}[args.command]()
    except (DeviceTestsError, StoreError) as error:
        raise SystemExit("May device tests stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May device tests failed; private logs and evidence were not published") from None
