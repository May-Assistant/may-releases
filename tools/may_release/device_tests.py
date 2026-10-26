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
           an item carrying the scenario's tag (qa-<run id>-<scenario id>), or a question card in the tagged chat in a given state. Last, the test session's cleanup removes what the run
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
import functools
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import board
# The one redaction: the device server's (it keeps typed text out of the agent's view), the diagnostics' and publish's.
import device_mcp
from device_mcp import EMAIL, MASKED, SECRET_LINE, kept_lines
import device_agent
from device_agent import AgentError
import device_prototypes
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
# Server truth a row can ask for besides those reads (coordinator, 25 Oct): {"delivery": "succeeded"}, every tagged
# message's reply run succeeded (chat); {"get": NOTE_PATH, "task": "{tag} task", "note": "{tag} ..."}, the Agenda task
# titled exactly "task" has those words in its notes, which the server keeps as the commitment's description, and no
# task of that title is left open (CLOSED: the goal deletes it at its end), so a note typed into another task never
# passes (coordinator, 26 Oct: run 40243708096's notes left lines in an older task). A row's plain "server" reads
# expect one of EXPECTS: 401 is a session the app signed out (sign-out).
DELIVERY_TRUTH, NOTE_PATH, EXPECTS, MAX_NOTE = {"delivery": "succeeded"}, "/v1/commitments", (200, 401), 200
CLOSED = ("completed", "cancelled")
NOTE_GOAL = "Do not open any existing task; create a new one."
# A row with "chat": true talks to May in a chat of its own, made for it before its agent starts (make_chat: POST
# /v1/agents, named CHAT_NAME with its tag, removed by the test session's cleanup with the tag), never in the shared
# May chat that every earlier run wrote to (coordinator, 26 Oct). Its goal opens that chat by name.
CHAT_NAME, CHAT_GOAL = "{tag} chat", "open the chat named {tag} chat"
# Run 40290647340: on the new chat's empty state the iOS driver refused every act, so the chat gets its first message
# over the API (first_message) and the device never lands on it empty (coordinator, 26 Oct). The message holds no tag,
# so no truth, delivery code or question card ever counts it or May's reply to it (run_messages starts at the tag).
FIRST_MESSAGE = "This chat is for an automated device test. Reply with the word ready."
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
MAX_CHATS = 10
MAX_GOAL = 1500
# The least a row may give its agent (its own "seconds"): enough to launch, look and act a few times.
MIN_AGENT_SECONDS = 5 * 60
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
# A staging test session's lifetime (it has no refresh): each scenario's cap must fit inside it.
SESSION_SECONDS = 30 * 60
# The scenario's test session, handed to its Maestro flow in Maestro's environment, never as an argument: Maestro's CLI
# makes each MAESTRO_-prefixed variable a flow variable, so a flow reads it as ${MAESTRO_MAY_TEST_SESSION} (run
# 40051935128: sign-in.yaml's own launchApp with the session got none, so it never signed in).
MAESTRO_SESSION = "MAESTRO_MAY_TEST_SESSION"
# Maestro gets a home of its own under the runner's temp folder, never the runner's: its debug output holds each flow's
# evaluated commands, the test session among them, and later steps run may-app code on the same runner. It is removed
# at the run's end; on Android it carries the runner's adb key pair, which the emulator trusts. Maestro writes that
# output under $XDG_STATE_HOME/maestro, else Java's user.home, which Java reads from the passwd entry, not $HOME (second
# security review of #104), so maestro_isolation sets all three; any ~/.maestro/tests a run leaves is removed too.
MAESTRO_HOME = "may-maestro-home"


def maestro_home(root):
    """A fresh, private HOME for Maestro (with the runner's adb keys when there are some)."""
    home = root / MAESTRO_HOME
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(mode=0o700)
    keys = Path.home() / ".android"
    for name in ("adbkey", "adbkey.pub"):
        if (keys / name).is_file() and not (keys / name).is_symlink():
            (home / ".android").mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(keys / name, home / ".android" / name)
    return home


def maestro_isolation(home):
    """The environment that keeps Maestro's state and debug output in home: HOME, XDG_STATE_HOME and, through the
    launcher's MAESTRO_OPTS, Java's user.home."""
    return {"HOME": str(home), "XDG_STATE_HOME": str(home), "MAESTRO_OPTS": f"-Duser.home={home}"}


def forget_maestro_home(root):
    shutil.rmtree(root / MAESTRO_HOME, ignore_errors=True)
    # Java's user.home (the passwd entry) and $HOME, in case either Maestro call ever wrote there.
    for home in {Path(pwd.getpwuid(os.getuid()).pw_dir), Path.home()}:
        shutil.rmtree(home / ".maestro" / "tests", ignore_errors=True)


# may-app's subflow that taps a signed-in account through onboarding ("Hi, I'm May") to Chats, run before each agent
# scenario: the shared test account lands in onboarding, not Chats (run 40046272749), and the agent starts from launch.
ONBOARDING, ONBOARDING_SECONDS = "subflows/finish-onboarding.yaml", 180
# A row whose "onboarding" is RECORD is the onboarding scenario itself: the subflow is not run before it, so its agent and
# recording start at onboarding's first step. Only a fresh agent row may say so (the shared account is past onboarding).
RECORD = "record"
# Each device job has 120 minutes (device-tests.yml). Its build takes about 7, and after the scenarios come the judge
# (up to device_agent.JUDGE_SECONDS for each agent scenario that ran), the seal and the handover. The scenarios and
# their judging get RUN_BUDGET_SECONDS from the run step's start, so the job always seals and publishes what ran
# (run 40035089599: four iOS agent scenarios at up to 25 minutes each could outlast the job). A scenario that would
# not fit is not started.
RUN_BUDGET_SECONDS = 90 * 60
BUDGET = "skipped: the run's time budget left no room for it (RUN_BUDGET_SECONDS)"
OUT, SEALED, VERDICTS = "may-device-out", "may-device-sealed", "device-verdicts.json"
AGENTS = "device-agents.json"
SERIAL = "emulator-5554"
# The fixed reasons a report may give (publish.py accepts these words alone).
RATE_LIMITED = "Claude is rate limited (skipped, never a pass)"
UNFINISHED = "the agent did not finish"
BLOCKED = "the agent reported it was blocked"
WEDGED = f"the device refused {device_mcp.FAIL_FAST} acts in a row, so the agent was stopped (iOS)"
NO_TRUTH = "the server did not show the tagged item"
NOT_JUDGED = "not judged"
DELIVERY_FAILED = "May's server did not record a succeeded reply"
UNCONFIGURED = "server checks not configured"
UNVERIFIED = "unverified"
NOT_REACHED = "the agenda's first page on the server did not reach the tagged task (unverified, never a pass)"
SMOKE_MAESTRO = "Maestro did not drive the app"
SMOKE, SMOKE_PASS = "smoke", "smoke-pass"
# The smoke scenario's flow: this repository's own, written to the private folder at run time.
SMOKE_FLOW = "appId: " + APP_ID + "\n---\n- launchApp\n- waitForAnimationToEnd\n"
SWITCHED_OFF = "switched off (MAY_DEVICE_TESTS_SKIP)"
SKIP = "MAY_DEVICE_TESTS_SKIP"
# A scenario on its own new account (POST /v1/auth/test-session {fresh: true}, may-app 150), deleted by its cleanup at
# the scenario's end: the row's "fresh": true, or the variable MAY_DEVICE_TESTS_FRESH ("all", or comma-separated ids).
# Staging makes at most 50 a UTC day; past that a fresh scenario is skipped with FRESH_EXHAUSTED, never run on the
# shared account instead.
FRESH = "MAY_DEVICE_TESTS_FRESH"
FRESH_EXHAUSTED = "skipped: staging's fresh test accounts for today are used up"
# A fresh mint whose reply does not say fresh (a staging older than may-app 150, or one that ignores it) is a session on
# the shared account: never used as a fresh one, never deleted.
NOT_FRESH = "skipped: staging did not put this scenario on a fresh account"
# Staging caps a test account's model calls a UTC day (MAY_TEST_SESSION_CALLS_PER_DAY, may-app 125): past it, replies fail
# test_session_limit_reached. Before each agent scenario the run reads what is left (GET /v1/auth/test-session/state,
# model_calls) and skips one that MODEL_CALL_MARGIN calls might not cover, rather than run it into the cap.
MODEL_CALL_MARGIN = 10
MODEL_CAP = "skipped: the test account's model calls for today are nearly used up"
# A session staging would not mint part-way through a run (run 40077167994: one failed 50 minutes into the iOS run and
# the whole report was lost) is asked for once more after MINT_RETRY_SECONDS, then only its scenario is skipped. A
# refusal that means the setup is wrong (MINT_FATAL: the secret, or the test sign-in switched off) still stops the run.
MINT_FAILED = "skipped: staging did not mint a test session for it"
MINT_RETRY_SECONDS = 30
MINT_FATAL = (401, 403, 404)
NO_CHAT = "skipped: staging did not make the scenario's own chat"
NO_FIRST = "skipped: staging did not take the first message of the scenario's own chat"
NOT_STARTED = "May Playground did not start on the emulator after one retry (its diagnostics are sealed)"
HALTED = "skipped: the run stopped after May Playground did not start"


def fresh_switch():
    """The ids MAY_DEVICE_TESTS_FRESH lists, or {"all"}."""
    return {part.strip() for part in os.environ.get(FRESH, "").split(",") if part.strip()}


def wants_fresh(row, switched):
    """Whether a scenario runs on its own fresh account; never the smoke scenario, which has no session."""
    return row["driver"] != SMOKE and (row.get("fresh") is True or "all" in switched or row["id"] in switched)


def switched_off():
    """The scenario ids the variable MAY_DEVICE_TESTS_SKIP lists (comma-separated), skipped until it no longer does: a
    scenario whose server side is merged but not yet deployed, say."""
    return {part.strip() for part in os.environ.get(SKIP, "").split(",") if part.strip()}


class DeviceTestsError(ValueError):
    pass


class LaunchFailed(DeviceTestsError):
    """The app did not start twice: said is the last attempt's `am start` output, for the private diagnostics."""

    def __init__(self, said):
        super().__init__("May Playground did not start on the emulator after one retry")
        self.said = said


class MintFailed(DeviceTestsError):
    """Staging did not mint a session: status is its HTTP status, or 0 when it did not answer."""

    def __init__(self, status):
        super().__init__("The staging test session could not be minted (" +
                         (f"HTTP {status}" if status else "no reply") + ")")
        self.status = status


class FreshRefused(DeviceTestsError):
    """A fresh mint that gave no fresh account; its reason is the skipped scenario's."""
    reason = NOT_FRESH


class FreshExhausted(FreshRefused):
    """Staging made its last fresh test account for today (429 fresh_accounts_exhausted)."""
    reason = FRESH_EXHAUSTED


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
                                                 set(c) == {"question"} and c["question"] in QUESTION_STATES or
                                                 c == DELIVERY_TRUTH and "Send this message" in row["goal"] or
                                                 set(c) == {"get", "task", "note"} and c["get"] == NOTE_PATH and
                                                 c["task"] == "{tag} task" and NOTE_GOAL in row["goal"] and
                                                 row["goal"].endswith(f"then delete the {c['task']}.") and
                                                 isinstance(c["note"], str) and c["note"].startswith("{tag} ") and
                                                 len(c["note"]) <= MAX_NOTE and c["note"][6:] in row["goal"])
                        for c in row["truth"]) and
                    ("Send this message" in row["goal"]) == (row.get("chat") is True) and
                    ("chat" not in row or CHAT_GOAL in row["goal"]) or
                    driver == SMOKE and "flow" not in row and "truth" not in row and
                    isinstance(row.get("goal"), str) and len(row["goal"]) <= MAX_GOAL and row.get("server") == [])
                and isinstance(row.get("title"), str) and
                isinstance(row.get("platforms"), list) and set(row["platforms"]) <= set(PLATFORMS) and
                isinstance(row.get("outward"), bool) and isinstance(row.get("fresh", False), bool) and
                ("steps" not in row or driver == "agent" and type(row["steps"]) is int and
                 device_mcp.MAX_ACTIONS < row["steps"] <= device_mcp.MOST_ACTIONS) and
                ("seconds" not in row or driver == "agent" and type(row["seconds"]) is int and
                 MIN_AGENT_SECONDS <= row["seconds"] <= device_agent.AGENT_SECONDS) and
                ("onboarding" not in row or row["onboarding"] == RECORD and driver == "agent" and
                 row.get("fresh") is True) and ("chat" not in row or driver == "agent") and
                isinstance(row.get("server"), list) and
                all(isinstance(c, dict) and isinstance(c.get("get"), str) and SERVER_PATH.fullmatch(c["get"]) and
                    c.get("expect") in EXPECTS and type(c.get("expect")) is int for c in row["server"])):
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
    """<UTC date>-<may-app short sha>-<GitHub run id>: the run's folder in may-renders' device-tests/, the same for both
    device jobs of one workflow run, and its own for each run (run 40148200485 overwrote 40148178587's: one commit, one
    day). Without a run id (a local run) the folder has none."""
    sha = head_revision(source)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeviceTestsError("The prepared checkout has no exact commit")
    run = os.environ.get("GITHUB_RUN_ID", "")
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + "-" + sha[:12] + \
        ("-" + run if re.fullmatch(r"[1-9][0-9]{4,19}", run) else "")


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


def mint(base, secret, post=urllib.request.urlopen, *, fresh=False):
    """A short-lived staging session for the test account: POST <staging>/v1/auth/test-session {secret}. Staging fixes
    the account itself (MAY_TEST_SIGNIN_EMAIL) and replies {token, expires_at, user_id}. With fresh, the session is on
    a new account of its own, which can never get a second session ({secret, fresh: true}); FreshExhausted past the
    day's cap. The token is held in memory alone; nothing from the reply is printed."""
    request = urllib.request.Request(base + "/v1/auth/test-session", method="POST",
                                     data=json.dumps(dict({"secret": secret}, **({"fresh": True} if fresh else {})))
                                     .encode(), headers={"Content-Type": "application/json"})
    try:
        with post(request, timeout=30) as reply:
            data = json.loads(reply.read())
    except urllib.error.HTTPError as error:
        if fresh and error.code == 429 and error_code(error) == "fresh_accounts_exhausted":
            raise FreshExhausted("Staging's fresh test accounts for today are used up") from None
        raise MintFailed(error.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise MintFailed(0) from None
    token = data.get("token") if isinstance(data, dict) else None
    if not (isinstance(token, str) and TOKEN.fullmatch(token)):
        raise DeviceTestsError("The staging test session reply has no token")
    if fresh and data.get("fresh") is not True:
        raise FreshRefused("The staging test session reply is not on a fresh account")
    return token


def error_code(error):
    """An API refusal's code ({"error": {"code": ...}}), or None."""
    try:
        data = json.loads(error.read(64 * 1024))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    found = data.get("error", {}).get("code") if isinstance(data, dict) and isinstance(data.get("error"), dict) else None
    return found if isinstance(found, str) else None


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
    """Whether any string in a JSON value holds tag (case-insensitive) as a whole word: qa-1-poll is not in
    qa-1-poll-skip, so one scenario's reads never count another's."""
    if depth > 20:
        return False
    if isinstance(data, str):
        return re.search(r"(?<![a-z0-9_-])" + re.escape(tag.lower()) + r"(?![a-z0-9_-])", data.lower()) is not None
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


# A chat's messages come oldest first, MESSAGE_PAGE at a time after a seq cursor (GET .../messages?after=&limit=): the
# shared account's May chat holds far more than one page, so a run's own messages are only on its last pages (run
# 40087626039: May replied, yet the report said "none stored", because only the first 100 were read).
MESSAGE_PAGE, MAX_MESSAGE_PAGES = 200, 50


def chat_messages(base, token, conversation, get):
    """(HTTP status of the first page, every message of a chat, oldest first), page by page to its end (at most
    MAX_MESSAGE_PAGES pages)."""
    messages, cursor, first = [], "0", None
    for _ in range(MAX_MESSAGE_PAGES):
        status, data = read_json(base, token, f"/v1/conversations/{conversation}/messages?after={cursor}"
                                 f"&limit={MESSAGE_PAGE}", get)
        first = status if first is None else first
        page = listed(data, "messages") if status == 200 else []
        messages += page
        following = data.get("next_cursor") if isinstance(data, dict) else None
        if len(page) < MESSAGE_PAGE or not (isinstance(following, str) and following.isdigit()) or \
                int(following) <= int(cursor):
            break
        cursor = following
    return first, messages


def run_messages(messages, tag):
    """A chat's messages from the run's first tagged one on: what this run said and what came after it."""
    start = next((at for at, message in enumerate(messages) if holds(message, tag)), len(messages))
    return messages[start:]


def run_chats(data, tag):
    """The chats a scenario's messages are read in (GET /v1/agents' reply): those named with its tag alone (its own,
    made by make_chat), at most MAX_CHATS. Never May's shared chat: with no chat of its own listed, nothing is read and
    its truth fails (independent review of may-releases #132)."""
    agents = [row for row in listed(data, "agents") if isinstance(row.get("id"), str) and AGENT_ID.fullmatch(row["id"])]
    chosen = [row for row in agents if holds(row.get("name"), tag)]
    return list({row["id"]: row for row in chosen}.values())[:MAX_CHATS]


def question_truth(base, token, state, tag, get=urllib.request.urlopen):
    """K's route to a question card (21 Oct): every chat is an agent (GET /v1/agents, May's own first), and a chat's
    id is its conversation's. True when the run's chat (run_chats) holds the tag, and the first question card after
    it is in state (answered, or closed: the app's Skipped): a later card, or another run's, never passes."""
    status, data = read_json(base, token, "/v1/agents", get)
    if status != 200:
        return status, False
    for agent in run_chats(data, tag):
        status, messages = chat_messages(base, token, agent["id"], get)
        # A question card counts only after the run's own first tagged message: an older run's card never passes.
        card = next((message["question"] for message in run_messages(messages, tag)
                     if message.get("kind") == "question" and isinstance(message.get("question"), dict)), None)
        if card is not None and card.get("state") == state:
            return 200, True
    return 200, False


# What became of a chat scenario's messages (the coordinator, 24 Oct: "Not delivered" with no reason on the board).
# Codes alone ever leave the runner: a reply run's status and its error code, as the server names them.
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
NONE_STORED = "none stored"
MAX_DELIVERY = 10


def commitment_note(base, token, title, note, get=urllib.request.urlopen):
    """The Agenda's first page (GET NOTE_PATH, without a cursor: up to the server's 200 items) read for the tasks titled
    exactly title (case and outer spaces aside; a deleted task is still listed, closed): (HTTP status, "found" when
    one's notes, the commitment's description, hold note and none is left open; "open" when the note is there but a
    task of that title is still open; "absent" when no such task is there, or none's notes hold it; "beyond" when none
    is there and the page has more after it, so the run cannot tell). Only the title and the note are looked for;
    nothing read is kept or printed."""
    status, data = read_json(base, token, NOTE_PATH, get)
    if status != 200:
        return status, "absent"
    tasks = [task for task in listed(data, "commitments")
             if isinstance(task.get("title"), str) and task["title"].strip().lower() == title.lower()]
    if not tasks:
        more = isinstance(data, dict) and isinstance(data.get("next_cursor"), str) and data["next_cursor"] != ""
        return status, "beyond" if more else "absent"
    if not any(isinstance(task.get("description"), str) and note in task["description"] for task in tasks):
        return status, "absent"
    return status, "open" if any(task.get("status") not in CLOSED for task in tasks) else "found"


# A one-off (coordinator, 26 Oct): earlier notes runs typed their note into the shared account's "file taxes" task
# instead of a task of their own. `tidy` removes only those lines from it: a line made of nothing but such notes
# (LEFTOVER_NOTE, the notes goal's words after a run's tag, each with its full stop if it has one), give or take
# MAX_STRAY other characters that are not spaces: run 40290565440 found two notes glued into one line with a stray
# character between them (".qa-<run> note from the device testkqa-<run> note from the device test"). Any other line,
# one holding such a note among more words included, is kept and counted.
TIDY_TITLE = "file taxes"
LEFTOVER_NOTE = re.compile(r"qa-[0-9]+(?:-[a-z0-9]+)* note from the device test\.?", re.IGNORECASE)
MAX_STRAY = 2
MAX_AGENDA_PAGES = 20


def tidy_lines(text):
    """(notes without the leftover note lines, lines removed, mixed lines kept: those holding such a note among other
    words)."""
    kept, removed, mixed = [], 0, 0
    for line in text.split("\n"):
        if LEFTOVER_NOTE.search(line) is not None:
            if len("".join(LEFTOVER_NOTE.sub("", line).split())) <= MAX_STRAY:
                removed += 1
                continue
            mixed += 1
        kept.append(line)
    return "\n".join(kept), removed, mixed


def tidy_notes(base, token, get=urllib.request.urlopen, post=urllib.request.urlopen):
    """The shared account's tasks titled TIDY_TITLE, every Agenda page read (at most MAX_AGENDA_PAGES), each with
    leftover note lines saved without them (PATCH /v1/commitments/<id> at its revision): counts alone, {"tasks",
    "lines", "mixed", "saved", "failed"}, or None when the Agenda could not be read. Nothing read is kept or printed."""
    tasks, cursor = [], None
    for _ in range(MAX_AGENDA_PAGES):
        status, data = read_json(base, token, NOTE_PATH + (f"?cursor={urllib.parse.quote(cursor)}" if cursor else ""),
                                 get)
        if status != 200 or not isinstance(data, dict):
            return None
        tasks += [task for task in listed(data, "commitments")
                  if isinstance(task.get("title"), str) and task["title"].strip().lower() == TIDY_TITLE]
        cursor = data.get("next_cursor")
        if not (isinstance(cursor, str) and cursor):
            break
    counts = {"tasks": len(tasks), "lines": 0, "mixed": 0, "saved": 0, "failed": 0}
    for task in tasks:
        if not (isinstance(task.get("description"), str) and isinstance(task.get("id"), str) and
                AGENT_ID.fullmatch(task["id"]) and type(task.get("revision")) is int):
            continue
        notes, removed, mixed = tidy_lines(task["description"])
        counts["mixed"] += mixed
        if not removed:
            continue
        counts["lines"] += removed
        request = urllib.request.Request(base + f"{NOTE_PATH}/{task['id']}", method="PATCH",
                                         data=json.dumps({"expected_revision": task["revision"],
                                                          "description": notes if notes.strip() else None})
                                         .encode(),
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
        try:
            with post(request, timeout=60) as reply:
                saved = reply.status == 200
        except (urllib.error.URLError, TimeoutError, ValueError):
            saved = False
        counts["saved" if saved else "failed"] += 1
    return counts


def tidy(minted=mint, tidied=tidy_notes, revoked_by=None):
    """The `tidy` step (device-tests.yml, tidy_file_taxes): one shared-account session, the leftover note lines
    removed from its TIDY_TITLE tasks, the session signed out (revoke, defined below). Prints counts alone."""
    revoked_by = revoked_by or revoke
    require_step_secrets((SIGNIN_SECRET,))
    secret = os.environ.pop(SIGNIN_SECRET, "")
    base = staging(secret, True)
    token = minted(base, secret)
    secret = ""
    try:
        counts = tidied(base, token)
    finally:
        revoked_by(base, token)
        token = ""
    if counts is None:
        raise DeviceTestsError("The Agenda could not be read; nothing was changed")
    tasks, lines, saved, failed, mixed = (counts[key] for key in ("tasks", "lines", "saved", "failed", "mixed"))
    print(f"May device tests: {tasks} file taxes tasks found; {lines} leftover note lines removed from {saved} of "
          f"them, {failed} not saved; {mixed} mixed lines kept")
    if failed:
        raise DeviceTestsError("A task's notes could not be saved")


def server_reads(row, entry, base, token, own, checked, truth, asked, delivered, noted):
    """A scenario's server reads with its own session, while it lasts, into entry: its plain reads (each expecting a
    status), its delivery codes when it sends May a message, and its truth, every read with the scenario's own tag."""
    for check in row["server"]:
        status = checked(base, token, check["get"])
        entry["server"].append({"get": check["get"], "status": status, "pass": status == check["expect"]})
    codes = delivered(base, token, own) if sends_messages(row) else None
    if codes is not None:
        entry["delivery"] = codes
    for check in row.get("truth", []):
        if "question" in check:
            path, (status, found) = "/v1/agents", asked(base, token, check["question"], own)
        elif check == DELIVERY_TRUTH:
            path, status = "/v1/agents", 0 if codes is None else 200
            found = bool(codes) and all(code == "succeeded" for code in codes)
        elif "note" in check:
            status, state = noted(base, token, check["task"].replace("{tag}", own), check["note"].replace("{tag}", own))
            if state == "beyond":
                entry["server"].append({"get": NOTE_PATH, "status": status, "pass": False, "unverified": True})
                continue
            path, found = NOTE_PATH, state == "found"
        else:
            path, (status, found) = check["get"], truth(base, token, check["get"], own)
        entry["server"].append({"get": path, "status": status, "pass": status == 200 and found})


def sends_messages(row):
    """An agent scenario whose goal sends May a message (chat, poll, routines): its messages are read afterwards."""
    return row["driver"] == "agent" and "Send this message" in row["goal"]


def message_codes(base, token, tag, get=urllib.request.urlopen):
    """For each of the user's messages holding the tag, in the run's chats (run_chats, as question_truth),
    its reply run's "<status>" or "<status> (<error_code>)" (latest_run), or "no run": at most MAX_DELIVERY. None
    stored at all ([NONE_STORED]) means no tagged user message was found in the chats read. None when the chats
    could not be read. The text is read here only to find the tag."""
    status, data = read_json(base, token, "/v1/agents", get)
    if status != 200:
        return None
    codes = []
    for agent in run_chats(data, tag):
        status, messages = chat_messages(base, token, agent["id"], get)
        for message in messages:
            if message.get("author") == "user" and holds(message, tag):
                codes.append(run_code(message.get("latest_run")))
    return codes[:MAX_DELIVERY] or [NONE_STORED]


def run_code(run):
    if not isinstance(run, dict):
        return "no run"
    status = run.get("status") if isinstance(run.get("status"), str) and CODE.fullmatch(run["status"]) else "unknown"
    error = run.get("error_code")
    return f"{status} ({error})" if isinstance(error, str) and CODE.fullmatch(error) else status


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


def make_chat(base, token, tag, post=urllib.request.urlopen):
    """A scenario's own chat (CHAT_NAME with its tag), made with its session before its agent starts: POST /v1/agents
    {client_key, name}, keyed by the tag so a repeat makes no second one. The chat's id when staging answered 200 or
    201 with one, else None. Nothing else from the reply is kept, and nothing is printed."""
    request = urllib.request.Request(base + "/v1/agents", method="POST",
                                     data=json.dumps({"client_key": tag, "name": CHAT_NAME.replace("{tag}", tag)})
                                     .encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with post(request, timeout=60) as reply:
            status, data = reply.status, json.loads(reply.read(1024 * 1024))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    found = data.get("agent", data) if isinstance(data, dict) else None
    ok = status in (200, 201) and isinstance(found, dict) and isinstance(found.get("id"), str) and \
        AGENT_ID.fullmatch(found["id"]) is not None
    return found["id"] if ok else None


def first_message(base, token, chat, tag, post=urllib.request.urlopen):
    """FIRST_MESSAGE in the chat (a chat's id is its conversation's): POST /v1/conversations/<chat>/messages, keyed by
    the tag so a repeat sends no second one. True when staging answered 200 or 202; nothing of the reply is kept."""
    request = urllib.request.Request(base + f"/v1/conversations/{chat}/messages", method="POST",
                                     data=json.dumps({"client_message_id": tag + "-first", "body": FIRST_MESSAGE})
                                     .encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with post(request, timeout=60) as reply:
            return reply.status in (200, 202)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def open_chat(base, token, tag, post=urllib.request.urlopen):
    """The scenario's own chat, made (make_chat) and given its first message (first_message): True, or the reason it
    could not start (NO_CHAT or NO_FIRST)."""
    chat = make_chat(base, token, tag, post)
    if chat is None:
        return NO_CHAT
    return True if first_message(base, token, chat, tag, post) else NO_FIRST


def tag_cleanup(accounts, base, secret, since, tag, minted, cleaned):
    """K's cleanup of what the run made on the shared account (its tagged items and chats), with a session of its own
    (signed out by abandon). Called from the run's finally, so a run that stops part-way cleans up too (independent
    review of may-releases #132). The count removed, or None when there was nothing shared or it did not answer."""
    if not accounts.shared:
        return None
    at("cleanup")
    try:
        token = accounts.track(minted(base, secret))
    except DeviceTestsError:
        return None  # read as a cleanup that did not answer: the report is kept
    return cleaned(base, token, since, tag)


def own_chat(row, entry, base, token, own, made):
    """Whether a scenario may start: True, or, for a row with "chat" whose chat staging did not make or give its first
    message (made: open_chat), False with entry's reason NO_CHAT or NO_FIRST."""
    if not row.get("chat"):
        return True
    at(f"{row['id']}: making its own chat")
    opened = made(base, token, own)
    if opened is True:
        return True
    if opened == NO_FIRST:
        entry["reason"] = NO_FIRST
    else:
        entry["reason"] = NO_CHAT
    return False


def model_calls_left(base, token, get=urllib.request.urlopen):
    """The test account's model calls left today (model_calls.limit - used), or None when the state read does not
    give both as counts (a staging without the cap reports no model_calls)."""
    status, data = read_json(base, token, "/v1/auth/test-session/state", get)
    calls = data.get("model_calls") if status == 200 and isinstance(data, dict) else None
    used, limit = (calls.get("used"), calls.get("limit")) if isinstance(calls, dict) else (None, None)
    if type(used) is int and type(limit) is int and used >= 0 and limit >= 1:
        return limit - used
    return None


def near_model_cap(row, base, token, calls_left):
    """Whether an agent scenario is to be skipped because the account's model calls left today are fewer than
    MODEL_CALL_MARGIN. An unknown count runs the scenario."""
    if row["driver"] != "agent":
        return False
    at(f"{row['id']}: reading the model calls left")
    left = calls_left(base, token)
    return left is not None and left < MODEL_CALL_MARGIN


def delete_fresh(base, token, post=urllib.request.urlopen):
    """A fresh account's cleanup: the same POST, which on a fresh account deletes the whole account (since and tag are
    ignored there). True when staging answered {account_deleted: true}."""
    request = urllib.request.Request(base + "/v1/auth/test-session/cleanup", method="POST", data=b"{}",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with post(request, timeout=60) as reply:
            data = json.loads(reply.read(64 * 1024))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False
    return isinstance(data, dict) and data.get("account_deleted") is True


def revoke(base, token, post=urllib.request.urlopen):
    """A shared-account session signed out at its end, so none outlives the run (it would last SESSION_SECONDS): POST
    /v1/auth/logout with the session alone, never `everywhere` (the account's other sessions are not the run's). True
    when staging answered 204, or 401: already gone."""
    request = urllib.request.Request(base + "/v1/auth/logout", method="POST", data=b"{}",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with post(request, timeout=60) as reply:
            return reply.status == 204
    except urllib.error.HTTPError as error:
        return error.code == 401
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


class Accounts:
    """Each scenario's staging session: on the shared test account, or on a fresh one of its own (wants_fresh),
    deleted at the scenario's end, or, when the run stops part-way, by the run's finally (abandon). Once staging gives no
    fresh account (the day's are used up, or its reply is not fresh), every later fresh scenario is skipped for that
    reason, never run on the shared account."""

    def __init__(self, chosen, minted, deleted_by, platform="android", revoked_by=revoke):
        self.ids, self.minted, self.deleted_by, self.platform = fresh_switch(), minted, deleted_by, platform
        self.refused, self.open, self.deleted, self.kept = None, None, 0, 0
        # The shared account's sessions still open, each signed out at its end (or by abandon), with the counts.
        self.revoked_by, self.live, self.revoked, self.unrevoked = revoked_by, [], 0, 0
        # What ran on the shared account is undone by tag at the run's end; a fresh account goes at its scenario's end.
        self.shared = any(row["driver"] == "agent" and not wants_fresh(row, self.ids) for row in chosen)

    def session(self, row, base, secret, entry):
        """The scenario's token ("" for the smoke scenario), or None when it is skipped (entry's reason says why)."""
        if row["driver"] == SMOKE:
            return ""
        fresh = wants_fresh(row, self.ids)
        if fresh and self.refused is not None:
            entry["reason"] = self.refused
            return None
        for attempt in range(2):
            try:
                token = self.minted(base, secret, fresh=True) if fresh else self.minted(base, secret)
            except FreshRefused as refused:
                self.refused = refused.reason
                entry["reason"] = self.refused
                return None
            except MintFailed as failed:
                if failed.status in MINT_FATAL:
                    raise
                if attempt:
                    scenario, status, platform = row["id"], failed.status, self.platform
                    print(f"May device tests ({platform}): {scenario}: staging did not mint a session (HTTP status "
                          f"{status}, 0 for no reply), twice; the scenario is skipped")
                    entry["reason"] = MINT_FAILED
                    return None
                flows.sleep(MINT_RETRY_SECONDS)
                continue
            if fresh:
                self.open = token
            else:
                self.live.append(token)
            return token
        return None

    def finish(self, row, base, token):
        """A fresh scenario's cleanup, after its reads: its whole account deleted, with its own session (it can't get
        another)."""
        if row["driver"] != SMOKE and wants_fresh(row, self.ids):
            at(f"{row['id']}: deleting its fresh account")
            self.delete(base, token)
        else:
            self.sign_out(base, token)

    def track(self, token):
        """A shared-account session minted outside a scenario (the run's cleanup), signed out like the others."""
        self.live.append(token)
        return token

    def sign_out(self, base, token):
        if token in self.live:
            self.live.remove(token)
            if self.revoked_by(base, token):
                self.revoked += 1
            else:
                self.unrevoked += 1

    def abandon(self, base):
        """The run's finally: a fresh account whose scenario stopped part-way is deleted all the same, and every shared
        session still open is signed out."""
        if self.open:
            at("deleting a fresh account left open")
            self.delete(base, self.open)
        if self.live:
            at("signing out the test sessions left open")
        for token in list(self.live):
            self.sign_out(base, token)

    def delete(self, base, token):
        self.open = None
        if self.deleted_by(base, token):
            self.deleted += 1
        else:
            self.kept += 1

    def report(self, platform):
        """Prints the public lines on the fresh accounts and the shared sessions the run made: counts alone."""
        revoked, unrevoked = self.revoked, self.unrevoked
        if revoked:
            print(f"May device tests ({platform}): {revoked} test sessions were signed out at their end")
        if unrevoked:
            print(f"May device tests ({platform}): {unrevoked} test sessions' sign-out did not answer; they expire on "
                  "their own")
        deleted, kept = self.deleted, self.kept
        if deleted:
            print(f"May device tests ({platform}): {deleted} fresh test accounts were deleted by their cleanup")
        if kept:
            print(f"May device tests ({platform}): {kept} fresh test accounts' cleanup did not answer; staging sweeps "
                  "them after 2 hours")


def scenario_tag(tag, row):
    """The platform's run tag with the scenario's id (qa-<run>-a-<scenario>), in everything that scenario creates and
    reads, so poll's messages never count as poll-skip's. The cleanup keeps the platform's tag, which every scenario's
    on that platform holds."""
    return f"{tag}-{row['id']}"


# Run 40181517145: both device jobs of one run shared qa-<run>, so iOS's notes found the task Android's notes had
# saved and passed, and either job's cleanup could remove the other's items mid-run. Each platform's tag is its own.
PLATFORM_LETTERS = {"android": "a", "ios": "i"}


def run_tag(platform):
    """qa-<GitHub run id>-<a or i>: K's cleanup takes a tag of at least 8 characters, matched in names and titles, so
    one platform's cleanup never matches the other's (qa-1-a is in no qa-1-i-... title)."""
    run = os.environ.get("GITHUB_RUN_ID", "")
    return "qa-" + (run if re.fullmatch(r"[1-9][0-9]{4,19}", run) else str(int(time.time()))) + "-" + \
        PLATFORM_LETTERS[platform]


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
    just after boot; a second failure raises LaunchFailed, with that attempt's output from the private log."""
    flows.run_private([adb, "shell", "am", "force-stop", APP_ID], log, env=env, check=False)
    # The session goes to the emulator's shell on adb's stdin, never in an argument the runner's process list shows.
    start = " ".join(["am", "start", "-W", "-n", APP_ID + "/" + ACTIVITY] +
                     (["--es", "may_test_session", shlex.quote(token)] if token else [])) + "\n"
    for attempt in range(2):
        mark = log.stat().st_size if log.is_file() else 0
        if not adb_shell(adb, env, log, start):
            return
        if not attempt:
            flows.sleep(LAUNCH_RETRY_SECONDS)
    try:
        with log.open("rb") as output:
            output.seek(mark)
            said = output.read(MAX_DIAG // 8 + 1)
    except OSError:
        said = b""
    # Cut at a line's end, never inside one: launch_diagnostics redacts line by line, and a token cut short would no
    # longer match its pattern or the session (review of may-releases #136).
    if len(said) > MAX_DIAG // 8:
        said = said[:said.rfind(b"\n", 0, MAX_DIAG // 8) + 1]
    raise LaunchFailed(said)


ADB_SHELL_SECONDS = 120


def adb_shell(adb, env, log, line, run=subprocess.run):
    """`adb shell` running the one command line it reads on stdin: its exit status (the command's), or 1 when it did
    not finish within ADB_SHELL_SECONDS (a hung `am start -W`: a failed attempt, never a stopped run). Its output goes
    to the private log alone."""
    with log.open("ab") as output:
        try:
            return run([adb, "shell"], env=env, input=line.encode(), stdout=output, stderr=subprocess.STDOUT,
                       timeout=ADB_SHELL_SECONDS).returncode
        except subprocess.TimeoutExpired:
            output.write(b"adb shell timed out\n")
            return 1


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

    def halt(self):
        """Ends the recording, so the server reads that follow it are not recorded; stop() then pulls it."""
        if self.stopped.is_set():
            return
        self.stopped.set()
        # SIGINT lets screenrecord finish its file.
        flows.run_private([self.adb, "shell", "pkill", "-2", "screenrecord"], self.log, env=self.env, check=False)
        if self.thread.ident is not None:
            self.thread.join(timeout=30)

    def stop(self, folder):
        self.halt()
        names = []
        for part in range(1, self.parts + 1):
            name = f"{self.scenario}.mp4" if self.parts == 1 else f"{self.scenario}-part{part}.mp4"
            if not flows.run_private([self.adb, "pull", f"/sdcard/may-rec/{self.scenario}-{part}.mp4",
                                      str(folder / name)], self.log, env=self.env, check=False):
                names.append(name)
        return names


# Each Android scenario's diagnostics (coordinator, 24 Oct: sign-out's recording showed "May Playground isn't
# responding"): any ANR or crash the system's dropbox kept since the scenario before, then the emulator's last
# LOGCAT_LINES (cleared after, so each file is its own scenario's), as <scenario>.diag.txt in the sealed run folder.
# The dropbox, not /data/anr: the shell user reads it without `adb root`, which would restart adbd under Maestro. A
# line that could carry the session is dropped; the public log gives counts alone.
DIAG = ".diag.txt"
LOGCAT_LINES, MAX_DIAG, DIAG_SECONDS = 2000, 512 * 1024, 60
LOG_BUFFERS = "main,system,crash"
ANR, CRASH = "data_app_anr", "data_app_crash"
# may-app #1279: the debug Playground logs each failed request under the MayNetwork tag (method, path template and
# exception class alone). They get a section of their own ahead of the log, so trimming the log to its newest lines
# never loses them, and the public log counts them.
NETWORK_TAG = re.compile(rb"(?:^|[ /])MayNetwork\s*(?:\(\s*[0-9]+\))?:")
MAX_NETWORK_LINES = 200


MAX_COMMAND_OUTPUT = 16 * 1024 * 1024


def bounded_output(argv, env, seconds=DIAG_SECONDS, limit=MAX_COMMAND_OUTPUT):
    """(exit status, at most limit bytes of its output) for one diagnostics command, or None when it could not start
    or did not finish within seconds. The output goes to a file, never a pipe, in a session of its own: after
    subprocess.run's kill a pipe is read to its end, which waits on any child still holding it (the simulator's own
    log, adb's), while a file is never waited on (the security read of #115)."""
    with tempfile.TemporaryFile() as output:
        try:
            done = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
                                  start_new_session=True, timeout=seconds)
        except (OSError, subprocess.SubprocessError):
            return None
        output.seek(0)
        return done.returncode, output.read(limit)


def adb_output(adb, env, argv):
    """One adb command's output, or None when it failed or did not finish within DIAG_SECONDS: never an error."""
    done = bounded_output([adb, *argv], env)
    return done[1] if done is not None and done[0] == 0 else None


class Diagnostics:
    """The dropbox's ANR and crash entries already there at the start are counted, so each scenario keeps its own."""

    def __init__(self, adb, env):
        self.adb, self.env = adb, env
        self.seen = {tag: set(self.entries(tag)) for tag in (ANR, CRASH)}
        self.kept = self.anrs = self.crashes = self.network = 0
        self.dropped = {}  # scenario: lines dropped, for the public log's counts

    def entries(self, tag):
        """{header line: entry} for the dropbox's entries of tag now; {} when it could not be read. Each entry is known
        by its own timestamped header, so a failed read or the dropbox's rotation never makes old entries new."""
        data = adb_output(self.adb, self.env, ["shell", "dumpsys", "dropbox", "--print", tag]) or b""
        header = re.compile(rb"(?m)^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} " + tag.encode() + rb"\b.*$")
        found = {}
        for entry in re.split(rb"(?m)^={20,}[ \t]*$", data):
            match = header.search(entry)
            if match:
                found[match.group(0)] = entry.strip()
        return found

    def keep(self, folder, scenario, token):
        """Writes <scenario>.diag.txt when there is anything to keep; never an error."""
        found = []
        for tag in (ANR, CRASH):
            entries = self.entries(tag)
            new = [entry for key, entry in entries.items() if key not in self.seen[tag]]
            self.seen[tag] |= set(entries)
            found += [b"== " + tag.encode() + b" ==\n" + entry + b"\n" for entry in new]
            self.anrs += tag == ANR and bool(new)
            self.crashes += tag == CRASH and bool(new)
        log = adb_output(self.adb, self.env, ["logcat", "-d", "-t", str(LOGCAT_LINES), "-b", LOG_BUFFERS])
        adb_output(self.adb, self.env, ["logcat", "-b", LOG_BUFFERS, "-c"])
        # The stacks first, at most half; the log's newest lines fill the rest.
        (stacks, from_stacks), (logcat, from_log) = kept_lines(b"".join(found), token), kept_lines(log or b"", token)
        stacks = stacks[:MAX_DIAG // 2]
        failures = [line for line in logcat.splitlines(keepends=True) if NETWORK_TAG.search(line)]
        network = b"".join(failures[:MAX_NETWORK_LINES])[:MAX_DIAG // 8]
        room = MAX_DIAG - len(stacks) - len(network) - 128  # the section headers' room
        logcat = logcat[len(logcat) - room:] if len(logcat) > room else logcat
        if not (stacks or logcat):
            return
        # UTF-8 throughout, still within MAX_DIAG once any stray byte is replaced.
        text = (stacks + (b"== network ==\n" + network if network else b"") +
                (b"== logcat ==\n" + logcat if logcat else b"")).decode("utf-8", "replace").encode()
        text = text[:MAX_DIAG].decode("utf-8", "ignore").encode()
        try:
            (folder / f"{scenario}{DIAG}").write_bytes(text)
        except OSError:
            return
        self.kept += 1
        self.network += len(failures)
        self.dropped[scenario] = from_stacks + from_log


# Run 40290647340 (coordinator and M, 26 Oct): the app did not start, and the run removed everything, so the cause
# could not be named. On a launch failure the scenario keeps, as its .diag.txt, the failed `am start`'s output, the
# emulator's crash and main logs (their last LAUNCH_LOG_LINES) and the top activity's dump, each redacted and capped.
LAUNCH_LOG_LINES = 1000


def launch_diagnostics(adb, env, folder, scenario, token, said):
    """Writes <scenario>.diag.txt for a launch that failed; the lines it dropped, or None when nothing was written.
    Never an error."""
    sections = ((b"am start", said or b""),
                (b"logcat crash,main", adb_output(adb, env, ["logcat", "-d", "-t", str(LAUNCH_LOG_LINES), "-b",
                                                             "crash,main"]) or b""),
                (b"dumpsys activity top", adb_output(adb, env, ["shell", "dumpsys", "activity", "top"]) or b""))
    text, dropped = b"", 0
    for name, data in sections:
        kept, lost = kept_lines(data, token)
        dropped += lost
        # The logs' newest lines, the dump's first: each section at most a third.
        kept = kept[-(MAX_DIAG // 3):] if name.startswith(b"logcat") else kept[:MAX_DIAG // 3]
        text += b"== " + name + b" ==\n" + kept + (b"" if kept.endswith(b"\n") or not kept else b"\n")
    text = text.decode("utf-8", "replace").encode()[:MAX_DIAG].decode("utf-8", "ignore").encode()
    try:
        (folder / f"{scenario}{DIAG}").write_bytes(text)
    except OSError:
        return None
    return dropped


def screenshot(adb, env, path, log):
    with path.open("wb") as output, log.open("ab") as errors:
        result = subprocess.run([adb, "exec-out", "screencap", "-p"], stdout=output, stderr=errors,
                                stdin=subprocess.DEVNULL, env=env)
    if result.returncode:
        path.unlink(missing_ok=True)
        return None
    return path.name


# The emulator's three animation scales, at normal speed so a recording shows the app's real design (its mascot ring:
# at 0, Compose's reduced motion stops it). flows.py's renders keep theirs at 0, for still screens.
ANIMATION_SETTINGS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
ANIMATION_SCALE = "1"


def boot(root, log):
    """The emulator, as flows.run starts it (API 35 image, KVM, no window), with animations at ANIMATION_SCALE: (adb,
    process, env)."""
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
    settle_boot(adb, env, log)
    for setting in ANIMATION_SETTINGS:
        flows.run_private([adb, "shell", "settings", "put", "global", setting, ANIMATION_SCALE], log, env=env)
    return adb, emulator, env


# After sys.boot_completed, the emulator is still starting services and compiling apps: dev.bootcomplete, then a
# one-minute load average under SETTLE_LOAD, before the app is installed and launched (coordinator, 24 Oct: an "isn't
# responding" in the first scenario). At most SETTLE_SECONDS; an emulator that never settles still runs.
SETTLE_SECONDS, SETTLE_LOAD, SETTLE_POLL = 120, 2.0, 5


def settle_boot(adb, env, log, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + SETTLE_SECONDS
    while True:
        ready = adb_output(adb, env, ["shell", "getprop", "dev.bootcomplete"])
        load = adb_output(adb, env, ["shell", "cat", "/proc/loadavg"])
        try:
            quiet = float(load.split()[0]) < SETTLE_LOAD
        except (AttributeError, IndexError, ValueError):
            quiet = False
        if ready is not None and ready.strip() == b"1" and quiet:
            settled = True
            break
        if clock() >= deadline:
            settled = False
            break
        sleep(SETTLE_POLL)
    with log.open("ab") as output:
        output.write(b"emulator settled\n" if settled else b"emulator did not settle; going on\n")
    return settled


# Run 40148200485: Android chat, the first scenario after boot, sent nothing to staging for its 99 s and showed "Can't
# reach May"; notes, next, passed (D's staging read, 25 Oct). Before the first scenario the emulator's network has to
# be validated: Android's own NetworkMonitor has resolved and reached its probe host, so DNS and the route both work.
# At most NETWORK_SECONDS; an emulator whose network never validates still runs, and the log says so.
NETWORK_SECONDS, NETWORK_POLL, NETWORK_PROBE_SECONDS = 60, 3, 10
VALIDATED = re.compile(rb"Capabilities:[^\n]*(?<![A-Za-z0-9_])VALIDATED(?![A-Za-z0-9_])")


def network_ready(adb, env, clock=time.monotonic, sleep=time.sleep):
    """Seconds until the emulator's network validated, or None when it did not within NETWORK_SECONDS."""
    start = clock()
    while True:
        done = bounded_output([adb, "shell", "dumpsys", "connectivity"], env, seconds=NETWORK_PROBE_SECONDS)
        if done is not None and done[0] == 0 and VALIDATED.search(done[1]):
            return int(clock() - start)
        if clock() - start >= NETWORK_SECONDS:
            return None
        sleep(NETWORK_POLL)


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


# A flow's own tags name the platforms it is written for; a scenario whose flow is not tagged for this job's platform is
# skipped (the coordinator, 24 Oct: connections.yaml was tagged android alone and ran on iOS).
UNTAGGED = {"android": "skipped (flow not tagged for android)", "ios": "skipped (flow not tagged for ios)"}
FLOW_TAG = re.compile(r"[A-Za-z0-9_-]{1,40}")
MAX_FLOW_BYTES = 64 * 1024


def flow_tags(path):
    """The tags a Maestro flow's header (the lines before its first ---) lists, as a block list or [a, b]."""
    tags, listing = set(), False
    for line in path.read_text(errors="replace")[:MAX_FLOW_BYTES].splitlines():
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue  # a blank or comment line, inside a tag list or not, ends nothing
        item = re.fullmatch(r"\s+-\s+(\S+)\s*", line)
        if listing and item:
            tags.add(item.group(1))
            continue
        listing = False
        inline = re.fullmatch(r"tags:\s*(\[.*\])?\s*", line)
        if inline and inline.group(1):
            tags.update(part.strip() for part in inline.group(1)[1:-1].split(","))
        elif inline:
            listing = True
    return {tag for tag in tags if FLOW_TAG.fullmatch(tag)}


def untagged(row, flow, platform):
    """Whether a Maestro scenario's flow (present) is not tagged with this job's platform: the scenario is skipped with
    UNTAGGED[platform], and the run goes on (one bad tag never costs a whole run)."""
    return row["driver"] == "maestro" and platform not in flow_tags(flow)


def scenario_flow(row, source, private):
    """The Maestro flow a scenario runs: may-app's for a Maestro row, SMOKE_FLOW for the smoke row, none for an agent."""
    if row["driver"] == "maestro":
        return source / FLOWS_DIR / row["flow"]
    if row["driver"] == SMOKE:
        path = private / "smoke-flow.yaml"
        path.write_text(SMOKE_FLOW)
        return path
    return None


# Every id a Maestro scenario's flow (and the flows it runs) taps or waits on must be in the app's own sources for the
# job's platform: may-app's and its design system's Kotlin or Swift, test code left out. A flow naming one that is not
# fails without being driven (FLOW_IDS), and the log gives a count alone. An id is there as a string literal, or as a
# literal's fixed start before an interpolation ("connections.row.${c.wire}") whose remainder is itself a literal
# ("google_calendar"), so a misspelt row id is still caught.
FLOW_IDS = "the flow names an id the app's sources lack"
# prepare.py moves the design system's checkout into may-app's Android build: its Kotlin under android/, its Swift under
# swift/. Each platform reads may-app's own folder (the design system's checkout left out of Android's) and its own
# part of the design system.
DESIGN_SYSTEM_BUILD = "build-deps/may-design-system"
PLATFORM_SOURCES = {"android": ("android", "android"), "ios": ("ios", "swift")}
SOURCE_SUFFIXES = (".kt", ".swift")
# A flow's ids in either form, "id: x" on its own line or inline in {id: x, …}, and the flows it runs, as runFlow: x.yaml
# or a runFlow block's file: x.yaml (a runScript's file is JavaScript, never followed).
FLOW_ID = re.compile(r"(?<![\w-])id:\s*([\"']?)([^\"',{}\s]+)\1")
RUN_FLOW = re.compile(r"(?<![\w-])(?:runFlow|file):\s*([\"']?)([A-Za-z0-9_./-]+\.yaml)\1")
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
ID_PATTERN = re.compile(r"[\\*+?()\[\]{}^$]")
INTERPOLATION = re.compile(r"\$\{|\$[A-Za-z_]|\\\(")


def flow_ids(flow, seen=None):
    """The ids a flow and every flow it runs (runFlow, beside it) name, as written."""
    seen = set() if seen is None else seen
    if flow in seen or not flow.is_file() or flow.is_symlink():
        return set()
    seen.add(flow)
    ids = set()
    for line in flow.read_text(errors="replace")[:MAX_FLOW_BYTES].splitlines():
        line = uncommented(line)
        ids.update(found[1] for found in FLOW_ID.findall(line))
        for _, nested in RUN_FLOW.findall(line):
            if ".." not in Path(nested).parts:
                ids |= flow_ids(flow.parent / nested, seen)
    return ids


def uncommented(line):
    """A flow line without its YAML comment: from a # at its start or after a space, outside quotes."""
    quote = None
    for at, char in enumerate(line):
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (at == 0 or line[at - 1].isspace()):
            return line[:at]
    return line


def app_literals(roots):
    """The string literals in the Kotlin and Swift under roots, test code left out: (whole literals, fixed starts of
    interpolated ones)."""
    whole, starts = set(), set()
    for root in roots:
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if path.suffix not in SOURCE_SUFFIXES or path.is_symlink() or not path.is_file() or test_code(
                    path.relative_to(root)):
                continue
            for literal in LITERAL.findall(path.read_text(errors="replace")):
                cut = INTERPOLATION.search(literal)
                if cut is None:
                    whole.add(literal)
                elif cut.start():
                    starts.add(literal[:cut.start()])
    return whole, starts


def test_code(relative):
    """Whether a source file (relative to may-app's android/ or ios/, or the design system's android/ or swift/) says
    nothing about the shipped screens: a test source set (test, androidTest, test<Variant>, androidTest<Variant>) or the
    previews set, a test or snapshot target, a fixture, the emulator flows module or the design system's build copy."""
    folders, name = relative.parts[:-1], relative.stem
    source_set = folders[folders.index("src") + 1] if "src" in folders[:-1] else ""
    return (name.endswith(("Test", "Tests")) or "Fixture" in name or folders[:1] in (("flows",), ("build-deps",)) or
            re.fullmatch(r"(?:test|androidTest)(?:[A-Z]\w*)?|previews", source_set) is not None or
            any(part.endswith("Tests") or part == "Snapshots" for part in folders))


def lacking_ids(ids, literals):
    """The ids no literal (or fixed start plus a literal remainder) accounts for. Maestro reads an id as a regular
    expression: each alternative of an a|b id must be there, and an id using any other pattern syntax is not checked."""
    whole, starts = literals
    names = {name for i in ids if not ID_PATTERN.search(i) for name in i.split("|")}
    return sorted(n for n in names if n not in whole and not any(n.startswith(s) and n[len(s):] in whole for s in starts))


def app_sources(root, platform):
    """The literals of a platform's app code, read once a run and only when asked: may-app's own and its design
    system's (PLATFORM_SOURCES) where prepare.py left them."""
    app, design = PLATFORM_SOURCES[platform]
    source = root / "may-source"
    return functools.cache(lambda: app_literals([source / app, source / "android" / DESIGN_SYSTEM_BUILD / design]))


def flow_lacks_ids(row, flow, literals, platform):
    """Whether a Maestro scenario's flow names an id the app's sources lack (literals() reads them): the scenario fails
    with FLOW_IDS, never driven, and the public log gives the count alone."""
    if row["driver"] != "maestro":
        return False
    scenario, missing = row["id"], len(lacking_ids(flow_ids(flow), literals()))
    if missing:
        print(f"May device tests ({platform}): {scenario}: {missing} id(s) its flow names are not in the app's sources")
    return bool(missing)


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


def agent_seconds(row, default=device_agent.AGENT_SECONDS):
    """How long the agent may drive a row: the platform's default, or less when the row sets its own seconds (the
    onboarding row, 600: run 40290647340's onboarding took its whole 20 minutes and left chat and notes no budget)."""
    return min(row.get("seconds", default), default)


def cap(row, default=device_agent.AGENT_SECONDS):
    """The longest a scenario may take: its Maestro flow's timeout, or the agent's (agent_seconds)."""
    if row["driver"] == "maestro":
        return FLOW_SECONDS
    onboarding = ONBOARDING_SECONDS if row["driver"] == "agent" and not records_onboarding(row) else 0
    return agent_seconds(row, default) + onboarding


def steps(row):
    """The device server's step limit for a row that sets its own (MAY_DEVICE_STEPS), or nothing for the default."""
    return {"MAY_DEVICE_STEPS": str(row["steps"])} if "steps" in row else {}


def records_onboarding(row):
    """Whether the scenario records onboarding itself, so no subflow runs before it."""
    return row.get("onboarding") == RECORD


def finish_onboarding(row, source, platform, drive):
    """may-app's ONBOARDING subflow before an agent scenario, so the agent starts from Chats; drive(flow) is the
    platform's Maestro call, true when the flow passed. Without the subflow (an older may-app) or when it does not
    pass, the agent starts where the app is, and the public log says so. A row that records onboarding gets none.
    Whether the subflow passed, or None when none ran."""
    if row["driver"] != "agent" or records_onboarding(row):
        return
    flow, scenario = source / FLOWS_DIR / ONBOARDING, row["id"]
    if not flow.is_file() or flow.is_symlink():
        print(f"May device tests ({platform}): {scenario}: may-app has no onboarding subflow; the agent starts "
              "from launch")
        return
    at(f"{scenario}: onboarding subflow")
    try:
        passed = drive(flow)
    except subprocess.SubprocessError:
        passed = False
    if not passed:
        print(f"May device tests ({platform}): {scenario}: the onboarding subflow did not pass; the agent starts "
              "where it stopped")
    return passed


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
                  agent=device_agent.run_agent, install=device_agent.install, budgeted=None, delivered=None,
                  deleted_by=delete_fresh, calls_left=model_calls_left, diagnosed=None, revoked_by=revoke,
                  noted=commitment_note, made=open_chat):
    delivered = delivered or message_codes
    chosen = scenarios(selection, "android")
    agents, staged = uses(chosen)
    require_step_secrets((SIGNIN_SECRET, device_agent.OAUTH))
    secret = os.environ.pop(SIGNIN_SECRET, "")
    # The OAuth token is popped (held in memory, for the agent's environment alone) whether or not it is used.
    claude = device_agent.settings(os.environ) if agents else (os.environ.pop(device_agent.OAUTH, ""), None)
    root = runner_root()
    source, private = root / "may-source", root / "may-private"
    literals = app_sources(root, "android")
    if not source.is_dir() or not (source / APK).is_file():
        raise DeviceTestsError("The Playground app is not built")
    base = staging(secret, staged)
    outward = os.environ.get(OUTWARD, "").strip() != "off"
    skipped, accounts = switched_off(), Accounts(chosen, minted, deleted_by, "android", revoked_by)
    folder = root / OUT / run_folder(source) / "android"
    folder.mkdir(parents=True, mode=0o700)
    board_inputs(source, folder)
    log = private / "device-run.log"
    at("downloading Maestro")
    launcher = maestro(root, private, log)
    at("installing Claude Code")
    cli = install(root) if agents else None
    tag, since = run_tag("android"), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    at("booting the emulator")
    adb, emulator, env = boot(root, log)
    results, records, cleaned_count = [], {}, None
    try:
        at("installing the app")
        flows.run_private([adb, "install", "-r", "-t", str(source / APK)], log, env=env)
        show_touches(adb, env, log, True)
        if base:
            at("waiting for the emulator's network")
            waited = network_ready(adb, env)
            if waited is None:
                print("May device tests (android): network not ready within a minute; going on")
            else:
                print(f"May device tests (android): network ready after {waited} s")
        diagnostics = (diagnosed or Diagnostics)(adb, env)
        maestro_env = child_env(MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true",
                                ANDROID_HOME=str(flows.sdk()), **maestro_isolation(maestro_home(root)))
        budget = (budgeted or Budget)()
        halted = False
        for row in chosen:
            entry = {"id": row["id"], "title": row["title"], "verdict": "skipped", "videos": [], "screenshots": [],
                     "server": []}
            results.append(entry)
            if halted:
                entry["reason"] = HALTED
                continue
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
            if flow is not None and untagged(row, flow, "android"):
                entry["reason"] = UNTAGGED["android"]
                continue
            if flow is not None and flow_lacks_ids(row, flow, literals, "android"):
                entry.update(verdict="fail", reason=FLOW_IDS)
                continue
            # A fresh session per scenario: one lasts SESSION_SECONDS and has no refresh. The smoke scenario has none.
            token = accounts.session(row, base, secret, entry)
            if token is None:
                continue
            if near_model_cap(row, base, token, calls_left):
                entry["reason"] = MODEL_CAP
                accounts.finish(row, base, token)
                token = ""
                continue
            own = scenario_tag(tag, row)  # the scenario's own tag
            if not own_chat(row, entry, base, token, own, made):
                accounts.finish(row, base, token)
                token = ""
                continue
            at(f"{row['id']}: launching the app")
            try:
                launch(adb, env, token, log)
            except LaunchFailed as failed:
                # The report and the diagnostics are kept and sealed; the scenarios after it are not started.
                dropped = launch_diagnostics(adb, env, folder, row["id"], token, failed.said)
                if dropped is not None:
                    diagnostics.kept += 1
                    diagnostics.dropped[row["id"]] = dropped
                entry.update(verdict="fail", reason=NOT_STARTED)
                accounts.finish(row, base, token)
                token = ""
                halted = True
                print("May device tests (android): May Playground did not start on the emulator after one retry; the "
                      "run stopped there, and its diagnostics are sealed")
                continue
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
            finish_onboarding(row, source, "android", lambda path: not flows.run_private(
                [str(launcher), "--device", SERIAL, "test", str(path)], log, env=maestro_env, check=False,
                timeout=ONBOARDING_SECONDS))
            if row["driver"] != "maestro":
                at(f"{row['id']}: the agent")
                device = {"MAY_DEVICE_PLATFORM": "android", "MAY_DEVICE_ADB": str(adb), "MAY_DEVICE_SERIAL": SERIAL,
                          **steps(row)}
                records[row["id"]] = agent(cli, row["goal"], own, device, private / "agent" / row["id"],
                                           claude[0], claude[1], seconds=agent_seconds(row))
            entry["seconds"] = int(time.monotonic() - started)
            at(f"{row['id']}: screenshot, recording and server reads")
            shot = screenshot(adb, env, folder / (row["id"] + "-end.png"), log)
            entry["screenshots"] = [shot] if shot else []
            recorder.halt()
            # Run 40181517145: iOS read poll's server truth half an hour after its session was minted, behind the
            # recording's conversion, and got 401. The server reads come first, then the diagnostics, and only then
            # the recording's pull (and on iOS its conversion), on both platforms.
            server_reads(row, entry, base, token, own, checked, truth, asked, delivered, noted)
            # After the server reads, which need the session while it lasts: a slow dumpsys costs them nothing.
            diagnostics.keep(folder, row["id"], token)
            accounts.finish(row, base, token)
            entry["videos"] = recorder.stop(folder)
            settle(entry, row, passed if flow is not None else None)
            token = ""
    finally:
        cleaned_count = tag_cleanup(accounts, base, secret, since, tag, minted, cleaned)
        accounts.abandon(base)
        token = secret = ""
        forget_maestro_home(root)
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
    kept, anrs, crashes, network = diagnostics.kept, diagnostics.anrs, diagnostics.crashes, diagnostics.network
    if kept:
        print(f"May device tests (android): diagnostics kept for {kept} scenarios ({anrs} with an ANR, {crashes} with "
              f"a crash, {network} network failures)")
    for scenario, dropped in diagnostics.dropped.items():
        print(f"May device tests (android): {scenario} diagnostics: {dropped} lines dropped")
    if cleaned_count is not None:
        print(f"May device tests (android): the test session's cleanup removed {cleaned_count} tagged items")
    elif accounts.shared:
        print("May device tests (android): the test session's cleanup did not answer; tagged items may remain")
    accounts.report("android")


def combined(entry, record, judged, sends=False):
    """An agent scenario's verdict from its run, the judge and the server truth; the smoke scenario's from Maestro, its
    agent run and the judge, never "pass". For one that sends May a message (sends), the server's delivery codes
    outrank the screen: every code must be succeeded, and chats that could not be read leave it not judged."""
    if entry.get("maestro") is False:
        return {"verdict": "fail", "reason": SMOKE_MAESTRO}
    if record.get("state") == "rate_limited" or judged[0] == "rate_limited":
        return {"verdict": "skipped", "reason": RATE_LIMITED}
    if record.get("wedged") is True:
        return {"verdict": "fail", "reason": WEDGED}
    if record.get("outcome") == "blocked":
        return {"verdict": "fail", "reason": BLOCKED}
    if record.get("outcome") != "completed":
        return {"verdict": "fail", "reason": UNFINISHED}
    # The server's record outranks the screen (the May chat shows earlier runs' replies too): anything but succeeded
    # for every tagged message, or none at all, is a fail, whatever the judge read on screen.
    if sends:
        codes = entry.get("delivery")
        if codes is None:
            return {"verdict": "fail", "reason": NOT_JUDGED}
        if not codes or any(code != "succeeded" for code in codes):
            return {"verdict": "fail", "reason": DELIVERY_FAILED}
    if judged[0] != "pass":
        return {"verdict": "fail", "reason": None if judged[0] == "fail" else NOT_JUDGED}
    if "maestro" in entry:
        return {"verdict": SMOKE_PASS, "reason": None}
    if not entry["server"]:
        return {"verdict": "ui-pass", "reason": UNCONFIGURED}
    # A pass needs the screen and every server read; a read that failed is a fail, one that could not tell is
    # unverified, never a pass (coordinator, 25 Oct).
    if any(not check["pass"] and not check.get("unverified") for check in entry["server"]):
        return {"verdict": "fail", "reason": NO_TRUTH}
    if any(check.get("unverified") for check in entry["server"]):
        return {"verdict": UNVERIFIED, "reason": NOT_REACHED}
    return {"verdict": "pass", "reason": None}


# The agent's own account of its run, sealed into the private report.json alone (never a log or the job summary): its
# outcome, how many look-or-act steps it took, and its summary through kept_lines, at most MAX_AGENT_SUMMARY characters.
MAX_AGENT_SUMMARY = 300


def sealed_agent(record):
    # Line by line, a dropped line that ends in its key (":" or "=") taking the next non-blank line with it, its
    # value ("may_token:" then an opaque value); then joined into one line and redacted again, since joining could
    # make a secret-shaped line of two harmless ones, which publish would refuse with the whole report.
    lines, after = [], False
    for line in str(record.get("summary") or "").splitlines():
        if not line.strip():
            continue
        kept, dropped = kept_lines(line.encode(), "")
        if not dropped and not after:
            lines.append(kept.decode("utf-8", "replace"))
        after = bool(dropped) and line.rstrip().endswith((":", "="))
    summary, _ = kept_lines(" ".join(" ".join(lines).split()).encode(), "")
    actions = record.get("actions")
    return {"outcome": record.get("outcome") if record.get("outcome") in ("completed", "blocked") else None,
            "actions": actions if type(actions) is int and 0 <= actions <= device_mcp.MOST_ACTIONS else 0,
            "summary": summary.decode("utf-8", "replace").strip()[:MAX_AGENT_SUMMARY],
            # Why no screen could be read at done (device_mcp.DUMP_FAILURES' fixed words), or None.
            "hierarchy_failure": record.get("hierarchy_failure") if record.get("hierarchy_failure") in
            device_mcp.DUMP_FAILURES else None,
            # What the iOS driver said when that read, or the last refused act, failed (device_mcp.DRIVER_CAUSES), or
            # None (coordinator, 26 Oct).
            **{key: record.get(key) if record.get(key) in device_mcp.DRIVER_CAUSES else None
               for key in ("hierarchy_cause", "refusal_cause")}}


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
    rows = {row["id"]: row for row in scenarios("all", platform)}
    sending = {row["id"] for row in scenarios("all", platform) if sends_messages(row)}
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
                # Its delivery codes alone, never a message's text; None when its chats could not be read.
                evidence = dict(record, sends=entry["id"] in sending, delivery=entry.get("delivery"),
                                truth=truth_lines(rows.get(entry["id"]), entry))
                judged = judged_by(cli, goals[entry["id"]], scenario_tag(stored.get("tag", ""), entry), evidence,
                                   private / "judge" / entry["id"], token, model)
            except (OSError, subprocess.SubprocessError, KeyError):
                judged = ("error", "")
        verdict_reason = combined(entry, record, judged, entry["id"] in sending)
        entry["agent"] = sealed_agent(record)
        entry["verdict"] = verdict_reason["verdict"]
        entry.pop("reason", None)
        if verdict_reason["reason"]:
            entry["reason"] = verdict_reason["reason"]
        note = device_agent.redacted(judged[1])
        if note:
            entry["notes"] = note
    token = ""
    path.write_text(json.dumps(report, indent=2) + "\n")
    (private / VERDICTS).unlink(missing_ok=True)
    write_private(private / VERDICTS, json.dumps([entry["verdict"] for entry in report["scenarios"]]).encode())
    for entry in report["scenarios"]:
        scenario, word = entry["id"], entry["verdict"]
        print(f"May device tests ({platform}): {scenario}: {word}")
    counts = {word: sum(1 for entry in report["scenarios"] if entry["verdict"] == word)
              for word in ("pass", "ui-pass", "fail", "skipped", SMOKE_PASS, UNVERIFIED)}
    passed, ui_passed, failed, skipped = counts["pass"], counts["ui-pass"], counts["fail"], counts["skipped"]
    print(f"May device tests ({platform}): {passed} passed, {ui_passed} passed on screen alone (server checks not "
          f"configured), {failed} failed, {skipped} skipped")
    unverified = counts[UNVERIFIED]
    if unverified:
        print(f"May device tests ({platform}): {unverified} could not be checked on the server (unverified, never a "
              "pass)")
    smoke_passed = counts[SMOKE_PASS]
    if smoke_passed:
        print(f"May device tests ({platform}): {smoke_passed} passed the harness smoke check (not a test of May)")


def truth_lines(row, entry):
    """What each of a scenario's server reads checked and how it came out, in fixed words, for the judge (coordinator,
    25 Oct: run 40203481816's Android poll was judged a fail on May's own words, against a confirmed answered card).
    entry["server"] holds the row's plain reads, then one read for each truth row, in order (server_reads)."""
    if row is None:
        return []
    reads = entry.get("server", [])
    lines = []
    for check, read in zip(row["server"], reads):
        lines.append(f"GET {check['get']} answered {read.get('status')}, expected {check['expect']}: " +
                     ("confirmed" if read.get("pass") else "not confirmed"))
    for check, read in zip(row.get("truth", []), reads[len(row["server"]):]):
        if "question" in check:
            what = f"May's question card in this run's tagged chat is {check['question']}"
        elif check == DELIVERY_TRUTH:
            what = "every tagged message this run sent has a succeeded reply"
        elif "note" in check:
            what = "the task titled with the tag holds the goal's note and none of that title is left open"
        else:
            what = f"an item holding the tag is listed by GET {check['get']}"
        lines.append(what + ": " + ("could not be checked" if read.get("unverified") else
                                    "confirmed" if read.get("pass") else "not confirmed"))
    return lines


def smoke_rows(platform):
    """The smoke scenario rows for one platform (scenarios("all") leaves them out)."""
    try:
        rows = json.loads(SCENARIOS.read_text())
    except (OSError, ValueError):
        raise DeviceTestsError("The scenario list is unreadable") from None
    ids = [row["id"] for row in rows if isinstance(row, dict) and row.get("driver") == SMOKE and
           isinstance(row.get("id"), str)]
    return scenarios(",".join(ids), platform) if ids else []


# The flow board's previews (board.py): made here, in the device job that already runs may-app code, never in the
# publish job that holds the publish secrets (review of may-releases #88). A dot is never in a scenario id, so a
# preview never shares a name with a recording; publish.py only checks and uploads them.
PREVIEW, PREVIEW_WIDTH, PREVIEW_SECONDS = ".preview.mp4", 270, 300
# A preview is a timelapse of the whole recording, every part of it in order (coordinator, 26 Oct: previews showed only
# the first part, 3 minutes on Android and 5 on iOS, of a 15-minute run), at most PREVIEW_MAX_SECONDS long at
# PREVIEW_FPS and about PREVIEW_BITRATE, so it stays well under MAX_PREVIEW (run 40075535088: a 10-minute iOS chat
# preview came to 15.9 MB, over the artifact's 15 MB limit). One over MAX_PREVIEW is left out. On Android a small
# "×N" in a corner says how much faster than life it plays, when the image has PREVIEW_FONT.
PREVIEW_MAX_SECONDS, PREVIEW_FPS, PREVIEW_BITRATE = 60, 8, 160_000
PREVIEW_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MAX_PREVIEW = 2 * 1024 * 1024
# The preview step's own timeout is 8 minutes (device-tests.yml), so the seal always has its turn before the job's 120
# (run 40075535088: a 25-minute step after a full 100-minute run could outlast the job and lose the report). No new
# preview starts after PREVIEW_STEP_SECONDS; each is made in may-private and moved into the run folder only whole, so
# a step stopped part-way never leaves a half-written preview to be sealed.
PREVIEW_STEP_SECONDS = 6 * 60


def preview_sources(folder):
    """(scenario, recordings) for each scenario the device report recorded, keyed by the report's own rows (never
    worked out from a file name, which an id ending "-partN" would confuse): its recordings in the report's order, the
    whole run, each a plain MP4 in the run folder."""
    try:
        rows = json.loads((folder / "report.json").read_text())["scenarios"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return
    for row in rows if isinstance(rows, list) else []:
        videos = row.get("videos") if isinstance(row, dict) else None
        if not (isinstance(row.get("id") if isinstance(row, dict) else None, str) and SCENARIO_ID.fullmatch(row["id"])
                and isinstance(videos, list) and videos and all(isinstance(video, str) for video in videos)):
            continue
        parts = [folder / video for video in videos]
        parts = [part for part in parts if part.parent == folder and part.suffix == ".mp4" and part.is_file() and
                 not part.is_symlink()]
        if parts:
            yield row["id"], parts


def mp4_seconds(path):
    """A recording's length from its MP4 header (moov/mvhd), or None when the header is not one."""
    try:
        data = path.read_bytes()
    except OSError:
        return None

    def boxes(start, end):
        while start + 8 <= end:
            size, kind = int.from_bytes(data[start:start + 4], "big"), data[start + 4:start + 8]
            if size < 8 or start + size > end:
                return
            yield kind, start + 8, start + size
            start += size

    for kind, body, end in boxes(0, len(data)):
        if kind == b"moov":
            for inner, at, stop in boxes(body, end):
                if inner == b"mvhd" and stop - at >= 32:
                    wide = data[at] == 1
                    scale_at = at + (20 if wide else 12)
                    scale = int.from_bytes(data[scale_at:scale_at + 4], "big")
                    length = int.from_bytes(data[scale_at + 4:scale_at + (12 if wide else 8)], "big")
                    return length / scale if scale else None
    return None


def timelapse(seconds):
    """How many times faster than the recording its preview plays, so it lasts PREVIEW_MAX_SECONDS at most."""
    return max(1.0, (seconds or 0) / PREVIEW_MAX_SECONDS)


def recorded_seconds(videos):
    """A scenario's recorded length, its parts' together; a part whose header gives none counts as 0."""
    return sum(mp4_seconds(video) or 0 for video in videos)


def ffmpeg_encoder(root, log):
    """Ubuntu's ffmpeg from apt (its current, patched build; apt checks the archive's signature): the function that
    makes one preview. Raises DeviceTestsError when it will not install."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"}
    for argv in (["sudo", "apt-get", "update", "-qq"],
                 ["sudo", "apt-get", "install", "-y", "-qq", "--no-install-recommends", "ffmpeg"]):
        if flows.run_private(argv, log, env=env, check=False, timeout=600):
            raise DeviceTestsError("ffmpeg did not install")

    def encode(videos, target):
        return not flows.run_private(preview_argv(videos, target), log, env=env, check=False, timeout=PREVIEW_SECONDS)
    return encode


def preview_argv(videos, target):
    """ffmpeg's command for one preview: the parts joined in order (concat), sped up (setpts) so the whole lasts
    PREVIEW_MAX_SECONDS at most, then thinned, scaled and, when the image has PREVIEW_FONT and the preview is sped up,
    marked "×N" (N the speed-up, rounded)."""
    speed, font = timelapse(recorded_seconds(videos)), PREVIEW_FONT
    joined = "".join(f"[{at}:v:0]" for at in range(len(videos))) + f"concat=n={len(videos)}:v=1:a=0,"
    label = f",drawtext=fontfile={font}:text=\u00d7{round(speed)}:fontsize=14:fontcolor=white:box=1:" \
        "boxcolor=black@0.5:boxborderw=3:x=w-tw-6:y=6" if font.is_file() and speed > 1 else ""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            *(arg for video in videos for arg in ("-i", str(video))), "-an", "-filter_complex",
            f"{joined}setpts=PTS/{speed:.4f},fps={PREVIEW_FPS},scale={PREVIEW_WIDTH}:-2{label}", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "34", "-maxrate", str(PREVIEW_BITRATE), "-bufsize", str(2 * PREVIEW_BITRATE),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", str(PREVIEW_MAX_SECONDS), "-n", str(target)]


def render_prototypes(copied=device_prototypes.copy_paths, rendered=device_prototypes.render):
    """The flow prototypes' paths and expected screens (device_prototypes.py), into the Android run folder before the
    seal. Its own step, with no secret, which the workflow also lets fail and bounds; nothing here ever fails the job."""
    require_step_secrets(())
    try:
        root = runner_root()
        runs = [path for path in (root / OUT).iterdir() if path.is_dir()] if (root / OUT).is_dir() else []
        folder = runs[0] / "android" if len(runs) == 1 else None
        if not (folder and folder.is_dir()):
            made = failed = None
        else:
            copied(root / "may-source", folder)
            scratch = root / "may-private" / "prototypes"
            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Outside the runner's home: the render user reads only this, and none of private.
            # The runner's home itself too (Java's passwd entry and $HOME): on a hosted image it may be 755, and it
            # holds the runner's own files (its .credentials, ~/.docker/config.json) under default permissions.
            homes = {Path(pwd.getpwuid(os.getuid()).pw_dir), Path.home()}
            private = (root, *(root / name for name in ("may-claude", "may-source", "may-private", MAESTRO_HOME)),
                       *homes, *(home / name for home in homes for name in (".maestro", ".android")))
            parent = Path(tempfile.mkdtemp(prefix="may-render-"))
            try:
                made, failed = rendered(root / "may-source", folder, parent / "prototypes", scratch, private=private)
            finally:
                # The stage itself is removed by the render; its empty temporary folder goes here.
                try:
                    parent.rmdir()
                except OSError:
                    pass
    except Exception:
        print("May device tests (android): no prototype screens (an error stopped them; the seal goes on)")
        return
    if made is None:
        print("May device tests (android): no prototype screens (no named path, or the build or Chrome is unavailable)")
    else:
        print(f"May device tests (android): {made} prototype screens rendered, {failed} failed")


def make_previews(platform="android", encoder=None):
    """A PREVIEW_WIDTH-wide, silent preview of each scenario's recording, beside it in the run folder before the seal.
    Its own step, with no secret, which the workflow also lets fail (continue-on-error) and bounds (timeout-minutes),
    so the seal always runs. Nothing here ever fails the job: a preview that will not make is left out, and the board
    shows the end screen instead."""
    require_step_secrets(())
    try:
        made, failed = previews_in(platform, encoder)
    except Exception:
        print(f"May device tests ({platform}): no previews (an error stopped them; the seal goes on)")
        return
    if made is None:
        print(f"May device tests ({platform}): no recordings, so no previews")
    elif failed is None:
        print(f"May device tests ({platform}): no previews (the encoder is unavailable)")
    else:
        print(f"May device tests ({platform}): {made} previews made, {failed} failed")


def previews_in(platform, encoder, clock=time.monotonic):
    """(made, failed); (None, None) with no recording; (0, None) when the encoder is unavailable. A recording left
    for want of time (PREVIEW_STEP_SECONDS) counts as failed."""
    root = runner_root()
    runs = [path for path in (root / OUT).iterdir() if path.is_dir()] if (root / OUT).is_dir() else []
    folder = runs[0] / platform if len(runs) == 1 else None
    sources = list(preview_sources(folder)) if folder else []
    if not sources:
        return None, None
    started = clock()
    log = root / "may-private" / "previews.log"
    log.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        if encoder is None:
            if platform == "ios":
                sys.modules.setdefault("device_tests", sys.modules[__name__])
                import device_ios
                encoder = device_ios.preview_encoder(log)
            else:
                encoder = ffmpeg_encoder(root, log)
    except Exception:
        return 0, None
    made = failed = 0
    for scenario, videos in sources:
        if clock() - started > PREVIEW_STEP_SECONDS:
            failed += 1
            continue
        target, part = folder / (scenario + PREVIEW), log.parent / (scenario + PREVIEW)
        try:
            part.unlink(missing_ok=True)
            ok = encoder(videos, part) and part.is_file() and not part.is_symlink() and \
                part.stat().st_size <= MAX_PREVIEW and part.read_bytes()[4:8] == b"ftyp"
            if ok:
                os.replace(part, target)
        except Exception:
            ok = False
        if ok:
            made += 1
        else:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            failed += 1
    return made, failed


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
    parser.add_argument("command", choices=("build", "run", "judge", "previews", "prototypes", "seal", "verdict",
                                            "tidy"))
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
             "previews": lambda: make_previews(args.platform), "prototypes": render_prototypes,
             "verdict": lambda: verdict(args.platform), "tidy": tidy}[args.command]()
    except (DeviceTestsError, StoreError, AgentError) as error:
        raise SystemExit("May device tests stopped: " + error.args[0]) from None
    except Exception as error:
        failure_line(error)
        raise SystemExit("May device tests failed; private logs and evidence were not published") from None
