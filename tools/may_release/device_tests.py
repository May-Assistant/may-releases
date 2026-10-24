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
  run      start the emulator, install the app and the pinned Maestro (and Claude Code when an agent scenario is
           chosen), then each scenario: mint a fresh staging test session (it lasts 30 minutes) and start the app
           with it (a Playground-debug-only intent extra), record the screen, run its Maestro flow (may-app's
           FLOWS_DIR) or its agent (device_agent.py: Claude Code driving the emulator through device_mcp.py), take a
           screenshot and read the server: status checks, and for an agent scenario whether K's read endpoints show
           an item carrying the run's tag (qa-<run id>), or a question card in the tagged chat in a given state. Last, the test session's cleanup removes what the run
           created (POST /v1/auth/test-session/cleanup with the tag). A failed scenario does not fail this step, so
           its evidence is still published; `verdict` fails the job afterwards.
  judge    Claude Code, with no tool, judges each agent run from its goal, the agent's report and the last screen's
           hierarchy, then every verdict is printed. A rate limit skips the scenario; it is never a pass.
  seal     one tar of the run folder (<UTC date>-<may-app short sha>/android/), encrypted to MAY_RENDERS_RECIPIENT
  verdict  fails the job if any scenario failed

The "smoke" scenario checks the harness itself, and runs only when it is selected by name (never in "all"): it needs
no staging and no sign-in secret. The app starts signed out; the harness's own Maestro flow (SMOKE_FLOW, never
may-app's) launches it; then the agent, with a read-only goal, looks at the screen; the screen is recorded, sealed and
published as usual. Its verdict is "smoke-pass" or "fail", never "pass".

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
import shutil
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

import board
import device_agent
from device_agent import AgentError
import flows
from gate import AGE_RECIPIENT
from prepare import head_revision
from store import StoreError, child_env, require_step_secrets, runner_root, write_private
import toolchain

SCENARIOS = Path(__file__).resolve().parent / "device_scenarios.json"
FLOWS_DIR = "tools/device-tests/flows"
BOARD_FLOWS, BOARD_NOTES = "docs/evidence/flows.json", "docs/evidence/board-notes.json"
SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
FLOW_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\.yaml")
SERVER_PATH = re.compile(r"/v1/[a-z0-9/_-]{1,120}")
# K's reads that list what a test session's run created (server truth for agent scenarios), 21 Oct. Event notes are
# read only by the phone's own event keys, so they are judged on screen. A question card's truth is read through the
# chat that holds it (question_truth).
TRUTH_PATHS = ("/v1/approvals", "/v1/inbox", "/v1/commitments", "/v1/routines")
QUESTION_STATES = ("answered", "closed")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
MAX_CHATS = 10
MAX_GOAL = 1500
PLATFORMS = ("android", "ios")
APP_ID = "io.github.mayassistant.may.playground"
# May Playground's launcher activity (may-app's namespace, not its application ID) and the pause before a second start.
ACTIVITY, LAUNCH_RETRY_SECONDS = "io.github.mayassistant.may.MainActivity", 10
APK = "android/app/build/outputs/apk/playground/debug/app-playground-debug.apk"
AVD = "may-device-tests"
MAESTRO_URL = "https://github.com/mobile-dev-inc/maestro/releases/download/cli-2.8.0/maestro.zip"
MAESTRO_SHA256 = "b3e561161904fb391875ca5834d5b22cf0b01c052dd1b408ad83e30d8f8951b3"
SIGNIN_SECRET, OUTWARD, STAGING, RECIPIENT = ("MAY_TEST_SIGNIN_SECRET", "MAY_DEVICE_TESTS_OUTWARD",
                                              "MAY_STAGING_API_URL", "MAY_RENDERS_RECIPIENT")
# Staging alone (Samuel, may-app #104, 23 Oct): api.usemay.co becomes production at launch, and the agent must never
# drive production, so any other host is refused, production included.
STAGING_BASE = "https://staging-api.usemay.co"
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")
# Recordings: about 720p at about 1 Mbps, in screenrecord's 3-minute chunks: under 25 MB a chunk.
RECORD_SIZE, RECORD_BITRATE, RECORD_SECONDS = "720x1600", "1000000", 180
FLOW_SECONDS = 600
# The scenario's test session, handed to its Maestro flow in Maestro's environment, never as an argument: Maestro's CLI
# makes each MAESTRO_-prefixed variable a flow variable, so a flow reads it as ${MAESTRO_MAY_TEST_SESSION} (run
# 40051935128: sign-in.yaml's own launchApp with the session got none, so it never signed in).
MAESTRO_SESSION = "MAESTRO_MAY_TEST_SESSION"
# Each device job has 120 minutes (device-tests.yml). Its build takes about 7, and after the scenarios come the judge
# (up to device_agent.JUDGE_SECONDS for each agent scenario that ran), the seal and the handover. The scenarios and
# their judging get RUN_BUDGET_SECONDS from the run step's start, so the job always seals and publishes what ran
# (run 40035089599: four iOS agent scenarios at up to 25 minutes each could outlast the job). A scenario that would
# not fit is not started.
RUN_BUDGET_SECONDS = 100 * 60
BUDGET = "skipped: the run's time budget left no room for it (RUN_BUDGET_SECONDS)"
OUT, SEALED, VERDICTS = "may-device-out", "may-device-sealed", "device-verdicts.json"
AGENTS = "device-agents.json"
SERIAL = "emulator-5554"
# The fixed reasons a report may give (publish.py accepts these words alone).
RATE_LIMITED = "Claude is rate limited (skipped, never a pass)"
UNFINISHED = "the agent did not finish"
BLOCKED = "the agent reported it was blocked"
NO_TRUTH = "the server did not show the tagged item"
NOT_JUDGED = "not judged"
UNCONFIGURED = "server checks not configured"
SMOKE_MAESTRO = "Maestro did not drive the app"
SMOKE, SMOKE_PASS = "smoke", "smoke-pass"
# The smoke scenario's flow: this repository's own, written to the private folder at run time.
SMOKE_FLOW = "appId: " + APP_ID + "\n---\n- launchApp\n- waitForAnimationToEnd\n"
SWITCHED_OFF = "switched off (MAY_DEVICE_TESTS_SKIP)"
SKIP = "MAY_DEVICE_TESTS_SKIP"


def switched_off():
    """The scenario ids the variable MAY_DEVICE_TESTS_SKIP lists (comma-separated), skipped until it no longer does: a
    scenario whose server side is merged but not yet deployed, say."""
    return {part.strip() for part in os.environ.get(SKIP, "").split(",") if part.strip()}


class DeviceTestsError(ValueError):
    pass


# Where the run step is, for the public failure line: fixed words and this repository's scenario ids alone.
STAGE = ["starting"]


def at(stage):
    STAGE[0] = stage


def failure_line(error):
    """Prints the public line for an unexpected failure: the stage, the exception's class and, for an OS error, its
    errno (a number). Never the exception's text, which may hold private paths or output."""
    stage, detail = STAGE[0], type(error).__name__
    if isinstance(error, OSError) and type(error.errno) is int:
        detail += f", errno {error.errno}"
    print(f"May device tests failed at {stage} ({detail})")


def scenarios(selection="all", platform="android"):
    """SCENARIOS, checked, for one platform; "all" (every scenario but the smoke one) or a comma-separated list of
    ids."""
    try:
        rows = json.loads(SCENARIOS.read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("The scenario list is unreadable") from None
    checked, ids = [], set()
    for row in rows if isinstance(rows, list) else []:
        driver = row.get("driver") if isinstance(row, dict) else None
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and SCENARIO_ID.fullmatch(row["id"]) and
                row["id"] not in ids and (
                    driver == "maestro" and isinstance(row.get("flow"), str) and FLOW_NAME.fullmatch(row["flow"]) and
                    "goal" not in row and "truth" not in row or
                    driver == "agent" and "flow" not in row and isinstance(row.get("goal"), str) and
                    "{tag}" in row["goal"] and len(row["goal"]) <= MAX_GOAL and isinstance(row.get("truth"), list) and
                    all(isinstance(c, dict) and (set(c) == {"get"} and c["get"] in TRUTH_PATHS or
                                                 set(c) == {"question"} and c["question"] in QUESTION_STATES)
                        for c in row["truth"]) or
                    driver == SMOKE and "flow" not in row and "truth" not in row and
                    isinstance(row.get("goal"), str) and len(row["goal"]) <= MAX_GOAL and row.get("server") == [])
                and isinstance(row.get("title"), str) and
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
    return [row for row in checked if platform in row["platforms"] and
            (row["id"] in wanted if wanted is not None else row["driver"] != SMOKE)]


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
    """A short-lived staging session for the test account: POST <staging>/v1/auth/test-session {secret}. Staging fixes
    the account itself (MAY_TEST_SIGNIN_EMAIL) and replies {token, expires_at, user_id}. The token is held in memory
    alone; nothing from the reply is printed."""
    request = urllib.request.Request(base + "/v1/auth/test-session", method="POST",
                                     data=json.dumps({"secret": secret}).encode(),
                                     headers={"Content-Type": "application/json"})
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


def holds(data, tag, depth=0):
    """Whether any string in a JSON value holds tag (case-insensitive)."""
    if depth > 20:
        return False
    if isinstance(data, str):
        return tag.lower() in data.lower()
    if isinstance(data, dict):
        return any(holds(value, tag, depth + 1) for value in data.values())
    if isinstance(data, list):
        return any(holds(value, tag, depth + 1) for value in data)
    return False


def read_json(base, token, path, get):
    """One read with the test session: (HTTP status, its JSON or None). Nothing is printed."""
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    try:
        with get(request, timeout=30) as reply:
            return reply.status, json.loads(reply.read(8 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, RecursionError):
        return 0, None


def tagged(base, token, path, tag, get=urllib.request.urlopen):
    """Server truth: (HTTP status, whether the read lists an item carrying the run's tag)."""
    status, data = read_json(base, token, path, get)
    return status, status == 200 and holds(data, tag)


def listed(data, key):
    """A reply's list, bare or under key."""
    rows = data.get(key) if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def question_truth(base, token, state, tag, get=urllib.request.urlopen):
    """K's route to a question card (21 Oct): every chat is an agent (GET /v1/agents, May's own first), and a chat's
    id is its conversation's. True when a chat named with the tag, or May's own, holds the tag and a question
    message whose state is state (answered, or closed: the app's Skipped)."""
    status, data = read_json(base, token, "/v1/agents", get)
    if status != 200:
        return status, False
    agents = [row for row in listed(data, "agents") if isinstance(row.get("id"), str) and AGENT_ID.fullmatch(row["id"])]
    chosen = [row for row in agents if holds(row.get("name"), tag)] + agents[:1]
    for agent in list({row["id"]: row for row in chosen}.values())[:MAX_CHATS]:
        status, data = read_json(base, token, f"/v1/conversations/{agent['id']}/messages", get)
        messages = listed(data, "messages") if status == 200 else []
        if holds(messages, tag) and any(message.get("kind") == "question" and isinstance(message.get("question"), dict)
                                        and message["question"].get("state") == state for message in messages):
            return 200, True
    return 200, False


def cleanup(base, token, since, tag, post=urllib.request.urlopen):
    """K's cleanup of what the run created (only items made after since whose name or title holds tag): how many
    were removed, or None when it did not answer 200."""
    request = urllib.request.Request(base + "/v1/auth/test-session/cleanup", method="POST",
                                     data=json.dumps({"since": since, "tag": tag}).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with post(request, timeout=60) as reply:
            data = json.loads(reply.read(1024 * 1024))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    counts = [data.get(key) for key in ("chats", "tasks", "notes", "routines", "for_you", "approvals_expired")] \
        if isinstance(data, dict) else []
    return sum(count for count in counts if type(count) is int and count >= 0)


def run_tag():
    """qa-<GitHub run id>: K's cleanup takes a tag of at least 8 characters, matched in names and titles."""
    run = os.environ.get("GITHUB_RUN_ID", "")
    return "qa-" + (run if re.fullmatch(r"[1-9][0-9]{4,19}", run) else str(int(time.time())))


def show_touches(adb, env, log, on):
    """Android's own touch dots (Settings > Developer options > Show taps) on the emulator, so each recording shows
    where every tap lands (Samuel, 24 Oct); on before the scenarios and off after. A setting that will not take
    never stops the run."""
    flows.run_private([adb, "shell", "settings", "put", "system", "show_touches", "1" if on else "0"], log, env=env,
                      check=False)


def launch(adb, env, token, log):
    """The app, stopped and started again with a fresh test session (signed out, with no token: the smoke scenario).
    It names MainActivity, as may-app's hook documents (TestSession.kt), rather than resolving the launcher: smoke run
    39677407348 failed at this start. One retry after LAUNCH_RETRY_SECONDS, for an activity manager still settling
    just after boot; a second failure stops the run."""
    flows.run_private([adb, "shell", "am", "force-stop", APP_ID], log, env=env, check=False)
    start = [adb, "shell", "am", "start", "-W", "-n", APP_ID + "/" + ACTIVITY] + \
        (["--es", "may_test_session", token] if token else [])
    for attempt in range(2):
        if not flows.run_private(start, log, env=env, check=False):
            return
        if not attempt:
            flows.sleep(LAUNCH_RETRY_SECONDS)
    raise DeviceTestsError("May Playground did not start on the emulator after one retry")


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
        if subprocess.run([flows.sdk_tool(home, "cmdline-tools/latest/bin/avdmanager"), "create", "avd", "--force",
                           "-n", AVD, "-k", flows.SYSTEM_IMAGE, "-d", flows.DEVICE], input=b"no\n", stdout=output,
                          stderr=output, env=env).returncode:
            raise DeviceTestsError("The emulator's virtual device could not be created; no scenario ran")
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


def uses(chosen):
    """(any scenario needs the agent, any needs staging): the smoke scenario needs the agent and no staging."""
    return (any(row["driver"] in ("agent", SMOKE) for row in chosen),
            any(row["driver"] != SMOKE for row in chosen))


def staging(secret, staged):
    """The staging API's base URL, checked with the sign-in secret, when a chosen scenario needs staging; "" when none
    does (the smoke scenario alone), and then neither is needed."""
    if not staged:
        return ""
    base = os.environ.get(STAGING, "").strip().rstrip("/")
    if base != STAGING_BASE:
        raise DeviceTestsError(f"MAY_STAGING_API_URL is not {STAGING_BASE} in may-device-tests")
    if not secret:
        raise DeviceTestsError("MAY_TEST_SIGNIN_SECRET is not set in may-device-tests; no scenario ran")
    return base


def with_session(env, token):
    """Maestro's environment for one scenario's flow: the session in MAESTRO_SESSION when there is one."""
    return dict(env, **{MAESTRO_SESSION: token}) if token else dict(env)


def board_inputs(source, folder):
    """may-app's flows and the lanes' curated board text, copied into the sealed report for the publish job, which
    runs no may-app code (board.py checks both)."""
    for name, target in ((BOARD_FLOWS, "flows.json"), (BOARD_NOTES, "board-notes.json")):
        path = source / name
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= board.MAX_INPUT:
            shutil.copyfile(path, folder / target)


def scenario_flow(row, source, private):
    """The Maestro flow a scenario runs: may-app's for a Maestro row, SMOKE_FLOW for the smoke row, none for an agent."""
    if row["driver"] == "maestro":
        return source / FLOWS_DIR / row["flow"]
    if row["driver"] == SMOKE:
        path = private / "smoke-flow.yaml"
        path.write_text(SMOKE_FLOW)
        return path
    return None


class Budget:
    """RUN_BUDGET_SECONDS from the run's start: a scenario starts only when its own cap, the judging of every agent
    scenario started so far and its own judging still fit."""

    def __init__(self, total=RUN_BUDGET_SECONDS, clock=time.monotonic):
        self.total, self.clock, self.start, self.judging = total, clock, clock(), 0

    def fits(self, row, cap):
        judge = device_agent.JUDGE_SECONDS if row["driver"] == "agent" else 0
        if self.clock() - self.start + cap + self.judging + judge > self.total:
            return False
        self.judging += judge
        return True


def cap(row, agent_seconds=device_agent.AGENT_SECONDS):
    """The longest a scenario may take: its Maestro flow's timeout, or the agent's."""
    return FLOW_SECONDS if row["driver"] == "maestro" else agent_seconds


def settle(entry, row, passed):
    """A scenario's verdict after its run: a Maestro row's from its flow and server checks; an agent or smoke row waits
    for the judge (a smoke row keeps whether Maestro drove the app)."""
    if row["driver"] == "maestro":
        server_ok = all(check["pass"] for check in entry["server"])
        entry["verdict"] = "fail" if not passed or not server_ok else "pass" if row["server"] else "ui-pass"
        if entry["verdict"] == "ui-pass":
            entry["reason"] = UNCONFIGURED
        return
    entry.update(verdict="fail", reason=NOT_JUDGED)
    if row["driver"] == SMOKE:
        entry["maestro"] = bool(passed)


def run_scenarios(selection, minted=mint, checked=server_check, truth=tagged, asked=question_truth, cleaned=cleanup,
                  agent=device_agent.run_agent, install=device_agent.install, budgeted=None):
    chosen = scenarios(selection, "android")
    agents, staged = uses(chosen)
    require_step_secrets((SIGNIN_SECRET, device_agent.OAUTH))
    secret = os.environ.pop(SIGNIN_SECRET, "")
    # The OAuth token is popped (held in memory, for the agent's environment alone) whether or not it is used.
    claude = device_agent.settings(os.environ) if agents else (os.environ.pop(device_agent.OAUTH, ""), None)
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not (source / APK).is_file():
        raise DeviceTestsError("The Playground app is not built")
    base = staging(secret, staged)
    outward = os.environ.get(OUTWARD, "").strip() != "off"
    skipped = switched_off()
    folder = root / OUT / run_folder(source) / "android"
    folder.mkdir(parents=True, mode=0o700)
    board_inputs(source, folder)
    log = private / "device-run.log"
    at("downloading Maestro")
    launcher = maestro(root, private, log)
    at("installing Claude Code")
    cli = install(root) if agents else None
    tag, since = run_tag(), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    at("booting the emulator")
    adb, emulator, env = boot(root, log)
    results, records, cleaned_count = [], {}, None
    try:
        at("installing the app")
        flows.run_private([adb, "install", "-r", "-t", str(source / APK)], log, env=env)
        show_touches(adb, env, log, True)
        maestro_env = child_env(MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true",
                                ANDROID_HOME=str(flows.sdk()))
        budget = (budgeted or Budget)()
        for row in chosen:
            entry = {"id": row["id"], "title": row["title"], "verdict": "skipped", "videos": [], "screenshots": [],
                     "server": []}
            results.append(entry)
            if row["outward"] and not outward:
                entry["reason"] = "outward actions are off (MAY_DEVICE_TESTS_OUTWARD)"
                continue
            if row["id"] in skipped:
                entry["reason"] = SWITCHED_OFF
                continue
            if not budget.fits(row, cap(row)):
                entry["reason"] = BUDGET
                continue
            flow = scenario_flow(row, source, private)
            if flow is not None and (not flow.is_file() or flow.is_symlink()):
                entry.update(verdict="fail", reason="may-app has no Maestro flow for this scenario")
                continue
            # A fresh session per scenario: one lasts 30 minutes and has no refresh. The smoke scenario has none.
            token = minted(base, secret) if row["driver"] != SMOKE else ""
            at(f"{row['id']}: launching the app")
            launch(adb, env, token, log)
            recorder = Recorder(adb, env, row["id"], log)
            recorder.start()
            started = time.monotonic()
            if flow is not None:
                at(f"{row['id']}: Maestro")
                try:
                    passed = not flows.run_private(
                        [str(launcher), "--device", SERIAL, "test", str(flow), "--format", "junit", "--output",
                         str(folder / (row["id"] + ".xml"))], log, env=with_session(maestro_env, token), check=False,
                        timeout=FLOW_SECONDS)
                except subprocess.TimeoutExpired:
                    passed = False
            if row["driver"] != "maestro":
                at(f"{row['id']}: the agent")
                device = {"MAY_DEVICE_PLATFORM": "android", "MAY_DEVICE_ADB": str(adb), "MAY_DEVICE_SERIAL": SERIAL}
                records[row["id"]] = agent(cli, row["goal"], tag, device, private / "agent" / row["id"],
                                           claude[0], claude[1])
            entry["seconds"] = int(time.monotonic() - started)
            at(f"{row['id']}: screenshot, recording and server reads")
            shot = screenshot(adb, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = [shot] if shot else []
            entry["videos"] = recorder.stop(folder)
            for check in row["server"]:
                status = checked(base, token, check["get"])
                entry["server"].append({"get": check["get"], "status": status, "pass": status == check["expect"]})
            for check in row.get("truth", []):
                if "question" in check:
                    path, (status, found) = "/v1/agents", asked(base, token, check["question"], tag)
                else:
                    path, (status, found) = check["get"], truth(base, token, check["get"], tag)
                entry["server"].append({"get": path, "status": status, "pass": status == 200 and found})
            settle(entry, row, passed if flow is not None else None)
            token = ""
        if any(row["driver"] == "agent" for row in chosen):
            at("cleanup")
            token = minted(base, secret)
            cleaned_count = cleaned(base, token, since, tag)
    finally:
        token = secret = ""
        show_touches(adb, env, log, False)
        emulator.terminate()
        try:
            emulator.wait(timeout=60)
        except subprocess.TimeoutExpired:
            emulator.kill()
    at("writing the report")
    report = {"run": folder.parent.name, "device": "android", "scenarios": results}
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_private(private / AGENTS, json.dumps({"tag": tag, "agents": records}).encode())
    write_private(private / VERDICTS, json.dumps([entry["verdict"] for entry in results]).encode())
    ran = sum(1 for entry in results if entry["verdict"] != "skipped")
    print(f"May device tests (android): {ran} scenarios ran; verdicts follow the judge step")
    if cleaned_count is not None:
        print(f"May device tests (android): the test session's cleanup removed {cleaned_count} tagged items")
    elif any(row["driver"] == "agent" for row in chosen):
        print("May device tests (android): the test session's cleanup did not answer; tagged items may remain")


def combined(entry, record, judged):
    """An agent scenario's verdict from its run, the judge and the server truth; the smoke scenario's from Maestro, its
    agent run and the judge, never "pass"."""
    if entry.get("maestro") is False:
        return {"verdict": "fail", "reason": SMOKE_MAESTRO}
    if record.get("state") == "rate_limited" or judged[0] == "rate_limited":
        return {"verdict": "skipped", "reason": RATE_LIMITED}
    if record.get("outcome") == "blocked":
        return {"verdict": "fail", "reason": BLOCKED}
    if record.get("outcome") != "completed":
        return {"verdict": "fail", "reason": UNFINISHED}
    if judged[0] != "pass":
        return {"verdict": "fail", "reason": None if judged[0] == "fail" else NOT_JUDGED}
    if "maestro" in entry:
        return {"verdict": SMOKE_PASS, "reason": None}
    if not entry["server"]:
        return {"verdict": "ui-pass", "reason": UNCONFIGURED}
    if all(check["pass"] for check in entry["server"]):
        return {"verdict": "pass", "reason": None}
    return {"verdict": "fail", "reason": NO_TRUTH}


def judge_scenarios(judged_by=device_agent.judge, platform="android"):
    """Each agent scenario judged, then every verdict printed; the report and the recorded verdicts updated. The iOS job
    runs it too, with nothing to judge (agent scenarios are Android's), for its verdict lines."""
    require_step_secrets((device_agent.OAUTH,))
    root = runner_root()
    private = root / "may-private"
    runs = [path for path in (root / OUT).iterdir() if path.is_dir()] if (root / OUT).is_dir() else []
    if len(runs) != 1 or not (runs[0] / platform / "report.json").is_file():
        raise DeviceTestsError("There is no report to judge")
    path = runs[0] / platform / "report.json"
    report = json.loads(path.read_text())
    try:
        stored = json.loads((private / AGENTS).read_text())
    except (OSError, ValueError):
        stored = {}
    records = stored.get("agents", {}) if isinstance(stored, dict) else {}
    goals = {row["id"]: row["goal"] for row in scenarios("all", platform) if row["driver"] == "agent"}
    goals.update((row["id"], row["goal"]) for row in smoke_rows(platform))
    pending = [entry for entry in report["scenarios"] if entry.get("reason") == NOT_JUDGED and entry["id"] in records]
    token = model = None
    if pending:
        token, model = device_agent.settings(os.environ)
    else:
        os.environ.pop(device_agent.OAUTH, None)
    cli = root / "may-claude" / "claude"
    for entry in pending:
        record = records[entry["id"]]
        judged = ("skipped", "")
        if record.get("state") != "rate_limited" and record.get("outcome") == "completed":
            try:
                judged = judged_by(cli, goals[entry["id"]], stored.get("tag", ""), record,
                                   private / "judge" / entry["id"], token, model)
            except (OSError, subprocess.SubprocessError, KeyError):
                judged = ("error", "")
        verdict_reason = combined(entry, record, judged)
        entry["verdict"] = verdict_reason["verdict"]
        entry.pop("reason", None)
        if verdict_reason["reason"]:
            entry["reason"] = verdict_reason["reason"]
        if judged[1]:
            entry["notes"] = judged[1]
    token = ""
    path.write_text(json.dumps(report, indent=2) + "\n")
    (private / VERDICTS).unlink(missing_ok=True)
    write_private(private / VERDICTS, json.dumps([entry["verdict"] for entry in report["scenarios"]]).encode())
    for entry in report["scenarios"]:
        scenario, word = entry["id"], entry["verdict"]
        print(f"May device tests ({platform}): {scenario}: {word}")
    counts = {word: sum(1 for entry in report["scenarios"] if entry["verdict"] == word)
              for word in ("pass", "ui-pass", "fail", "skipped", SMOKE_PASS)}
    passed, ui_passed, failed, skipped = counts["pass"], counts["ui-pass"], counts["fail"], counts["skipped"]
    print(f"May device tests ({platform}): {passed} passed, {ui_passed} passed on screen alone (server checks not "
          f"configured), {failed} failed, {skipped} skipped")
    smoke_passed = counts[SMOKE_PASS]
    if smoke_passed:
        print(f"May device tests ({platform}): {smoke_passed} passed the harness smoke check (not a test of May)")


def smoke_rows(platform):
    """The smoke scenario rows for one platform (scenarios("all") leaves them out)."""
    try:
        rows = json.loads(SCENARIOS.read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("The scenario list is unreadable") from None
    ids = [row["id"] for row in rows if isinstance(row, dict) and row.get("driver") == SMOKE and
           isinstance(row.get("id"), str)]
    return scenarios(",".join(ids), platform) if ids else []


def seal(platform="android"):
    """One tar of the run folder, encrypted to MAY_RENDERS_RECIPIENT; nothing in the clear leaves the runner."""
    require_step_secrets(())
    root = runner_root()
    out = root / OUT
    runs = [path for path in out.iterdir() if path.is_dir()] if out.is_dir() else []
    if len(runs) != 1 or not (runs[0] / platform / "report.json").is_file():
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
    plain = sealed / (platform + ".tar")
    try:
        with tarfile.open(plain, "w") as archive:
            for path in sorted(out.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise DeviceTestsError("The run folder holds something other than files and folders")
                archive.add(path, arcname=path.relative_to(out).as_posix(), recursive=False)
        result = subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o", str(sealed / (platform + ".age")),
                                 str(plain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=child_env())
        if result.returncode:
            raise DeviceTestsError("The report could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print(f"May device tests: the {platform} report encrypted for the publish job")


def verdict(platform="android"):
    require_step_secrets(())
    try:
        verdicts = json.loads((runner_root() / "may-private" / VERDICTS).read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("No scenario verdicts are recorded") from None
    if not isinstance(verdicts, list) or "fail" in verdicts:
        raise DeviceTestsError("A scenario failed")
    print(f"May device tests ({platform}): no scenario failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "run", "judge", "seal", "verdict"))
    parser.add_argument("--scenarios", default="all")
    parser.add_argument("--platform", choices=PLATFORMS, default="android")
    args = parser.parse_args()
    try:
        if args.platform == "ios" and args.command in ("build", "run"):
            # Imported here: device_ios builds on this module. Run as a script this module is __main__, and
            # device_ios's own `import device_tests` would load a second copy: its DeviceTestsError would escape the
            # handler below and its stage would never reach failure_line (runs 39749763794, 39926037739 printed
            # "failed at starting (DeviceTestsError)" for an unset sign-in secret). So it gets this module.
            sys.modules.setdefault("device_tests", sys.modules[__name__])
            import device_ios
            device_ios.run_scenarios(args.scenarios or "all") if args.command == "run" else device_ios.build()
        elif args.command == "run":
            run_scenarios(args.scenarios or "all")
        elif args.command == "build":
            build()
        else:
            {"judge": lambda: judge_scenarios(platform=args.platform), "seal": lambda: seal(args.platform),
             "verdict": lambda: verdict(args.platform)}[args.command]()
    except (DeviceTestsError, StoreError, AgentError) as error:
        raise SystemExit("May device tests stopped: " + error.args[0]) from None
    except Exception as error:
        failure_line(error)
        raise SystemExit("May device tests failed; private logs and evidence were not published") from None
