"""The device server for device_agent.py: the only tools the device-test agent has, over stdio as `device`.

Claude Code runs the agent with no built-in tool (--tools ""), no settings, and this server alone
(--strict-mcp-config); its permission mode is dontAsk and only mcp__device is allowed. So everything the agent can do
is here: look (screenshot, hierarchy), act (tap, type, swipe, back, wait) and finish (done). It cannot run a command,
read or write a file, or reach the network itself.

- Android (MAY_DEVICE_PLATFORM android): the emulator through adb.
- iOS (ios): the simulator, through the pinned Maestro's driver (the coordinator's choice, 21 Oct, over adding idb).
  Each act is one Maestro flow this server writes itself, holding exactly one fixed command (tapOn point, inputText,
  swipe) for the app's bundle id, run by the session's one long-lived `maestro mcp` driver (its run tool); the agent
  never gives Maestro YAML, so Maestro's scripts, links and other apps are out of its reach. Screenshots come from
  simctl, the hierarchy from the driver's inspect_screen. Coordinates are the screenshot's pixels, as on Android;
  they become iOS points by the ratio of the screenshot's width to the hierarchy's root. back is a swipe in from the
  left edge.

- type takes plain text only (letters, digits, spaces and . , ! : - _ @; never ? or *, a glob in the device shell), so nothing reaches adb's shell as a
  command.
- On iOS, after FAIL_FAST refused acts in a row (or one refused on a snapshot timeout right after a screen read that
  timed out), every call but done answers that the agent must stop, blocked.
- At most MAX_ACTIONS look or act calls, or a scenario's own steps (MAY_DEVICE_STEPS, up to MOST_ACTIONS); after
  that every call but done answers that the limit is reached.
- Every call but done rewrites MAY_DEVICE_AGENT_RESULT as unfinished, with the steps so far and the last hierarchy.
- done records the agent's own outcome (completed or blocked) and a short summary to MAY_DEVICE_AGENT_RESULT, which
  device_tests.py keeps on the runner (it goes to the encrypted report only). The last hierarchy is kept beside it
  for the judge.

It writes nothing to stdout but its JSON-RPC answers, and nothing at all to stderr.
"""

import base64
import datetime
import json
import os
import plistlib
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree

PROTOCOL = "2025-06-18"
MAX_ACTIONS = 80
# Run 40181517145: Android's poll-skip reached 80 steps typing its free reply (about 17 characters a type call), so a
# scenario row may carry its own "steps" (coordinator, 25 Oct: poll-skip 120), never above MOST_ACTIONS.
MOST_ACTIONS = 200
# Run 40203481816: on iOS, every act after the first refusal was refused too, each holding the agent 90 s, so a wedged
# scenario spent its whole 20 minutes. After FAIL_FAST refused acts in a row the server stops every call but done, and
# the agent's result says so (coordinator, 25 Oct). iOS alone. Run 40309200520: three refusals, each a 90 s snapshot
# timeout, still cost poll and poll-skip 9 minutes apiece and chat its budget; the stop now comes at the second refusal
# in a row, or at once when a refusal's snapshot timeout follows a screen read that timed out too (the screen is not
# coming back), and each act's Maestro call is held ACT_SECONDS at most (coordinator, 26 Oct). Since #175 an act or read
# the driver did not answer in time ("no answer") is such a time-out (STUCK_CAUSES); any other refusal only counts
# toward the two in a row.
FAIL_FAST, ACT_SECONDS = 2, 60
UNFINISHED = "unfinished"  # the result's outcome until done replaces it
WEDGED_TEXT = "The device stopped taking acts: call done now with outcome blocked"
MAX_HIERARCHY = 40_000
MAX_SUMMARY = 1000
MAX_FIELD = 300
# No ? or *: the device's shell would read them as a glob in input text's argument.
TEXT = re.compile(r"[A-Za-z0-9 .,!:@_-]{1,200}")
# A line that could carry a session or a credential is dropped whole. Every pattern stays inside one line ([ \t],
# never \s), so a line kept here is a line publish keeps. Dropped:
SECRET_LINE = re.compile(
    rb"(?i)"
    # headers and named credentials: Authorization, Bearer, cookies, API keys, Anthropic and Google keys;
    rb"authorization|bearer|cookie|x-api-key|api[_-]?key|sk-ant-|AIza[0-9A-Za-z_-]{20}|"
    # prefixed opaque keys whatever their length: sk_live_, pk_test_, rk_, ghp_, gho_, xoxb-, ...;
    rb"\b(?:sk|pk|rk|whsec|ghp|gho|ghs|ghu|github_pat|xox[abpr])[_-](?:live_|test_)?[A-Za-z0-9_-]{12,}|"
    # any token, secret, password, session, key or signature set to a value: token=, "access_token":, secret=, ...;
    rb"\b(?:[a-z0-9]+[_-])*(?:token|secret|password|passwd|session|key|sig|signature|credential)[\"']?[ \t]*[:=]|"
    # a URL's query naming one, or an OAuth code;
    rb"[?&](?:access_token|refresh_token|id_token|token|code|key|session|sig|signature|auth)=|"
    # May's own keys, plain, dashed or URL-encoded (may_test_session, may-key, may%5Ftest%5Fsession);
    rb"may(?:_|-|%5f)[a-z0-9_%-]*(?:key|token|secret|session)|"
    # a JWT, a long hex run, or a long base64 run with no keyword at all.
    rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|\b[0-9a-f]{32,}\b|[A-Za-z0-9+/_-]{40,}={0,2}")
# An email address keeps its domain alone, @ or URL-encoded %40, a dotted domain or localhost. It starts where no
# address character (or a mask) comes before it, so masking twice changes nothing.
EMAIL = re.compile(rb"(?<![A-Za-z0-9._%+'!*@-])[A-Za-z0-9._%+'!-]{1,256}(@|%40)"
                   rb"((?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}|localhost)\b", re.IGNORECASE)
MASKED = rb"***\1\2"


def kept_lines(data, token):
    """(data without any line that could carry the session or a credential (SECRET_LINE, or the token itself), with
    each email address masked to its domain, the number of lines dropped)."""
    secret = token.encode() if token else None
    lines = data.splitlines(keepends=True)
    kept = [EMAIL.sub(MASKED, line) if b"@" in line or b"%40" in line.lower() else line
            for line in lines if not SECRET_LINE.search(line) and not (secret and secret in line)]
    return b"".join(kept), len(lines) - len(kept)
COORD = {"type": "integer", "minimum": 0, "maximum": 4000}
TOOLS = [
    {"name": "screenshot", "description": "A PNG screenshot of the emulator's screen (1080 x 2400 pixels)",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hierarchy", "description": ("The screen's accessibility hierarchy: one line per element with its text, "
                                          "content description, resource id, whether it is clickable, and the "
                                          "pixel centre to tap"),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "tap", "description": "Tap the screen at pixel x, y",
     "inputSchema": {"type": "object", "required": ["x", "y"], "properties": {"x": COORD, "y": COORD}}},
    {"name": "type", "description": ("Type text into the focused field: letters, digits, spaces and . , ! : - _ @ "
                                     "only, up to 200 characters"),
     "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}},
    {"name": "swipe", "description": "Swipe from x1, y1 to x2, y2 over ms milliseconds (scroll with this)",
     "inputSchema": {"type": "object", "required": ["x1", "y1", "x2", "y2"], "properties": {
         "x1": COORD, "y1": COORD, "x2": COORD, "y2": COORD,
         "ms": {"type": "integer", "minimum": 50, "maximum": 3000}}}},
    {"name": "back", "description": "Press the Android back button", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wait", "description": "Wait for the app (a reply, a load) up to 30 seconds",
     "inputSchema": {"type": "object", "required": ["seconds"], "properties": {
         "seconds": {"type": "integer", "minimum": 1, "maximum": 30}}}},
    {"name": "done", "description": ("Finish: outcome completed when the goal is done on screen, blocked when it "
                                     "cannot be done; summary says what you saw, in a few sentences"),
     "inputSchema": {"type": "object", "required": ["outcome", "summary"], "properties": {
         "outcome": {"type": "string", "enum": ["completed", "blocked"]},
         "summary": {"type": "string"}}}},
]


UDID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
BUNDLE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*){2,5}")


# M and the coordinator, 6 Nov (#1785): Android chat's sent text was cut or doubled in runs 82 to 84, under adb's
# fast input text. A control run (device-tests.yml's android_slow_typing, the chat scenario alone) types each word on
# its own, SLOW_GAP seconds apart, and reads the focused field's length after each: lengths alone, never the text, kept
# in the result (typed) for the scenario's private diag. Slow input that lands whole points at the app's composer
# under fast input; slow input that is still cut points at the harness.
SLOW_ENV, SLOW_GAP, MAX_TYPED, MAX_CHUNKS = "MAY_DEVICE_SLOW_TYPING", 0.3, 20, 60


def chunks(text):
    """text as slow typing sends it: each word with the spaces before it, so the chunks join to exactly text."""
    return re.findall(r" *[^ ]+| +$", text)


def slow_type(config, text):
    """Types text word by word, SLOW_GAP apart, and records the focused field's length after each word (None when it
    could not be read): (failed, nothing). Nothing typed is kept, only lengths."""
    lengths = []
    for chunk in chunks(text):
        status, _ = adb(config, "shell", "input", "text", chunk.replace(" ", "%s"))
        if status:
            return True
        time.sleep(SLOW_GAP)
        xml, _ = android_dump(config)
        field = None if xml is None else focused_text(xml, raw=True)
        if len(lengths) < MAX_CHUNKS:
            lengths.append(None if field is None else len(field))
    if len(config["typed"]) < MAX_TYPED:
        config["typed"].append({"want": len(text), "after": lengths})
    return False


def settings(environ):
    result = environ.get("MAY_DEVICE_AGENT_RESULT", "")
    if not result:
        return None
    steps = environ.get("MAY_DEVICE_STEPS", str(MAX_ACTIONS))
    if not (re.fullmatch(r"[1-9][0-9]{0,2}", steps) and int(steps) <= MOST_ACTIONS):
        return None
    base = {"result": result, "actions": 0, "hierarchy": "", "steps": int(steps), "refusals": 0, "wedged": False}
    if environ.get("MAY_DEVICE_PLATFORM", "android") == "android":
        adb, serial = environ.get("MAY_DEVICE_ADB", ""), environ.get("MAY_DEVICE_SERIAL", "")
        if not (adb and re.fullmatch(r"emulator-[0-9]{4}", serial)):
            return None
        return dict(base, platform="android", adb=adb, serial=serial,
                    **({"slow": True, "typed": []} if environ.get(SLOW_ENV) == "1" else {}))
    if environ.get("MAY_DEVICE_PLATFORM") != "ios":
        return None
    udid, maestro, bundle = (environ.get(name, "") for name in ("MAY_DEVICE_UDID", "MAY_DEVICE_MAESTRO",
                                                                 "MAY_DEVICE_BUNDLE"))
    if not (UDID.fullmatch(udid) and maestro and BUNDLE.fullmatch(bundle)):
        return None
    env = {key: environ[key] for key in ("PATH", "HOME", "TMPDIR", "JAVA_HOME", "DEVELOPER_DIR") if environ.get(key)}
    env.update(MAESTRO_CLI_NO_ANALYTICS="1", MAESTRO_DISABLE_UPDATE_CHECK="true",
               MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED="true")
    return dict(base, platform="ios", udid=udid, maestro=maestro, bundle=bundle, env=env, scale=None,
                flows=os.path.dirname(result), look=environ.get(LOOK_ENV) == "chat", looks=[])


def adb(config, *args, timeout=60):
    """One adb call on the configured emulator: (exit status, stdout bytes). Nothing reaches this server's output."""
    try:
        done = subprocess.run([config["adb"], "-s", config["serial"], *args], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 1, b""
    return done.returncode, done.stdout


def tool(config, argv, timeout=90, both=False):
    """One simctl or Maestro call for the iOS simulator: (exit status, stdout bytes, or stdout and stderr with both).
    Nothing reaches this server's output."""
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=config["env"])
    except subprocess.TimeoutExpired as late:
        return 1, (late.stdout or b"") + (late.stderr or b"") + b"\n(timed out)\n" if both else b""
    except OSError:
        return 1, b""
    return done.returncode, done.stdout + done.stderr if both else done.stdout


# Coordinator, 25 Oct (runs 40138900610 and 40148178587): iOS notes lost every tap after the Agenda tab, and the
# driver's own words were never kept. Each refused act's Maestro output, its last DRIVER_LINES lines through kept_lines,
# is added to DRIVER_FILE beside the agent's result (the first ones, up to MAX_DRIVER bytes), for device_ios to keep in
# the scenario's private diagnostics.
DRIVER_FILE, DRIVER_LINES, MAX_DRIVER = "driver.txt", 40, 32 * 1024


# Coordinator, 31 Oct (run 41164564379): in a card's thread a tap never returned and a swipe hung after COMPLETED, on a
# screen whose pixels did not change, and nothing kept said what the XCTest runner was waiting for. For each refused
# act, the runner's own unified log over the act's window is read and its lines counted into RUNNER_KINDS' fixed words
# (first match, else other); only the counts are kept, on one line after the act's driver lines. Waits for the app to
# idle that never end point at the app (a repeating animation, or main-queue work), not at the harness.
RUNNER_LOG = 'process BEGINSWITH "maestro-driver"'
RUNNER_KINDS = (("idle waits", re.compile(rb"to (?:become )?idle|wait(?:ing)? for [^\n]*idle", re.IGNORECASE)),
                ("animations not complete", re.compile(rb"animations? complete", re.IGNORECASE)),
                ("quiescence", re.compile(rb"quiescen", re.IGNORECASE)),
                ("event synthesis", re.compile(rb"synthesi[sz]", re.IGNORECASE)),
                ("snapshots", re.compile(rb"snapshot", re.IGNORECASE)),
                ("timeouts", re.compile(rb"timed? ?out|timeout", re.IGNORECASE)))
RUNNER_SECONDS = 20
# Run 41180883244: 133 of the runner's lines in a hung act matched no kind. The other lines are also counted by their
# log category, "[subsystem:category]" in log show's compact style, a name alone and only when it has a name's shape
# (else "no category"); the RUNNER_TOP most frequent are kept (coordinator, 31 Oct).
# A name starts with a letter, so the process's [pid:tid] is never one.
LOG_CATEGORY = re.compile(rb"\[([A-Za-z][A-Za-z0-9._-]{0,47}:[A-Za-z][A-Za-z0-9._-]{0,31})\]")
RUNNER_TOP = 5

# Runs 41289745884 and 41302227814 (coordinator, 1 Nov): every refused act's own Maestro log said no runner was alive,
# so Maestro installed a new one and polled it 44 to 63 times while the act's 60 s ran out; the runner died with each
# Maestro process that started it. So one Maestro process now serves the whole agent session: `maestro mcp` (its own
# MCP server over stdio, with no viewer), which keeps one session, and so one XCTest runner, per device for its life.
# Only its "run" (this server's own one-command flow, as before) and "inspect_screen" tools are ever called (MCP_TOOLS);
# its cloud and viewer tools never are. Its first start, and a restart, read the screen once within MCP_START_SECONDS,
# Maestro's own 120 s start allowance and a margin, so the runner's start is never charged to an act. A call that does
# not answer within its time kills the child, as the act's Maestro process was killed before; the next call starts a
# new one, DRIVER_STARTS at most for a session, after which every act is refused as "driver died" and the agent stops.
# Independent review of #175: the start's initialize and warm-up read share one MCP_START_SECONDS deadline; a child that
# does not answer in time is "no answer" and one that could not be started or made ready is "did not start" (neither is
# a snapshot time-out); the child runs in its own process group, which is what is signalled, so its runner goes with it.
MCP_TOOLS, MCP_START_SECONDS, DRIVER_STARTS, MCP_STOP_SECONDS = ("run", "inspect_screen"), 150, 2, 10
DIED_LINE = b"driver died\n"
NO_ANSWER_LINE, NO_START_LINE = b"maestro mcp: no answer\n", b"maestro mcp: did not start\n"
# Independent review of #175: device_agent kills only claude when a scenario runs out of time, which would leave this
# server, and its driver, running into the next scenario. So once this server's parent is gone (checked every
# PARENT_SECONDS) the driver's process group is killed and this server exits at once.
PARENT_SECONDS = 1


class McpDriver:
    """One `maestro mcp` child for this server's life: a JSON-RPC client over its stdin and stdout."""

    def __init__(self, config, popen=None, killpg=None):
        self.config, self.popen, self.killpg = config, popen or subprocess.Popen, killpg or os.killpg
        self.child, self.group, self.lines, self.serial, self.starts = None, None, None, 0, 0

    def alive(self):
        return self.child is not None and self.child.poll() is None

    @staticmethod
    def read(stream, lines):
        try:
            for line in stream:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    def start(self):
        """A new child, initialized and holding a runner; False when it could not be made ready (then stopped)."""
        self.starts += 1
        self.lines = queue.Queue()
        deadline = time.monotonic() + MCP_START_SECONDS
        try:
            self.child = self.popen([self.config["maestro"], "mcp", "--no-viewer", "--working-dir", self.config["flows"]],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=self.config["env"], start_new_session=True)
        except OSError:
            self.child = None
            return False
        self.group = self.child.pid
        threading.Thread(target=self.read, args=(self.child.stdout, self.lines), daemon=True).start()
        reply = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                            "clientInfo": {"name": "may-device-tests", "version": "1"}},
                             deadline - time.monotonic())
        ready = reply is not None and "error" not in reply and isinstance(reply.get("result"), dict)
        if ready:
            try:
                self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except (OSError, ValueError, AttributeError):
                ready = False
        if ready:
            ready = self.call("inspect_screen", {}, deadline - time.monotonic())[0]
        if not ready:
            self.stop()
        return ready

    def send(self, message):
        self.child.stdin.write((json.dumps(message) + "\n").encode())
        self.child.stdin.flush()

    def request(self, method, params, seconds):
        """The reply to one request, or None when the child closed, broke or did not answer within seconds."""
        self.serial += 1
        serial = self.serial
        try:
            self.send({"jsonrpc": "2.0", "id": serial, "method": method, "params": params})
        except (OSError, ValueError, AttributeError):
            return None
        deadline = time.monotonic() + seconds
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            # Only a reply is ours: a request or notification from the child never is, whatever its id.
            if isinstance(message, dict) and message.get("id") == serial and "method" not in message and \
                    ("result" in message or "error" in message):
                return message

    def call(self, name, arguments, seconds):
        """(taken, what it said): one of MCP_TOOLS on the configured simulator. A call that does not answer stops
        the child."""
        if name not in MCP_TOOLS:
            raise ValueError("Only Maestro's run and inspect_screen tools are called")
        reply = self.request("tools/call", {"name": name, "arguments": dict(arguments, device_id=self.config["udid"])},
                             seconds)
        if reply is None:
            self.stop()
            return False, NO_ANSWER_LINE
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        text = "".join(part.get("text", "") for part in result.get("content") or []
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
        return "error" not in reply and not result.get("isError"), text.encode("utf-8", "replace")

    def signal_group(self, number):
        """Signals the child's whole process group (Maestro and the runner it started); never an error."""
        if self.group is not None:
            try:
                self.killpg(self.group, number)
            except OSError:
                pass

    def kill(self):
        """Kills the child's process group at once (a server that is going away); never an error."""
        self.signal_group(signal.SIGKILL)

    def stop(self):
        """Ends the child: its stdin closed, then its group asked to stop, then killed; never an error. The group is
        killed last whatever happened, so nothing the child started is left."""
        child, self.child = self.child, None
        if child is None:
            return
        for end in (lambda: child.stdin.close(), lambda: self.signal_group(signal.SIGTERM),
                    lambda: self.signal_group(signal.SIGKILL)):
            try:
                end()
                child.wait(MCP_STOP_SECONDS)
                break
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
        self.kill()
        self.group = None


def driver_call(config, name, arguments, seconds):
    """(status, what the driver said) for one Maestro tool call, starting the session's driver first when it has none
    running: status 0 when taken. Once DRIVER_STARTS children have been started, every call is refused, driver died."""
    driver = config.setdefault("driver", McpDriver(config))
    if not driver.alive():
        if driver.child is not None:
            # A child that ended on its own may have left its runner and xcodebuild running: its group goes first.
            driver.stop()
        if driver.starts >= DRIVER_STARTS or not driver.start():
            config["driver_died"] = driver.starts >= DRIVER_STARTS
            return 1, DIED_LINE if config["driver_died"] else NO_START_LINE
    taken, said = driver.call(name, arguments, seconds)
    return (0 if taken else 1), said


def driver_line(config):
    """The fixed line the session's driver starts are kept as, after a refused act's runner counts."""
    driver = config.get("driver")
    return f"maestro driver: starts {int(driver.starts if driver else 0)}\n".encode()


def runner_counts(config, started, ended):
    """RUNNER_KINDS' counts, and other, over the runner's log lines from started to a second after ended; None when the
    log could not be read."""
    status, out = tool(config, ["xcrun", "simctl", "spawn", config["udid"], "log", "show", "--style", "compact",
                                "--start", stamp(started)[:19], "--end",
                                stamp(ended + datetime.timedelta(seconds=1))[:19], "--predicate", RUNNER_LOG],
                       timeout=RUNNER_SECONDS)
    if status:
        return None
    counts = dict.fromkeys([kind for kind, _ in RUNNER_KINDS] + ["other"], 0)
    categories = {}
    for line in out.splitlines():
        # log show's own header ("Timestamp ...", "Filtering the log data using ...") is not one of the runner's lines.
        if not line.strip() or line.startswith((b"Timestamp", b"Filtering", b"Skipping")):
            continue
        kind = next((kind for kind, pattern in RUNNER_KINDS if pattern.search(line)), "other")
        counts[kind] += 1
        if kind == "other":
            named = LOG_CATEGORY.search(line)
            name = named.group(1).decode() if named else "no category"
            categories[name] = categories.get(name, 0) + 1
    top = sorted(categories.items(), key=lambda item: (-item[1], item[0]))[:RUNNER_TOP]
    return dict(counts, **({"other by category": top} if top else {}))


def runner_line(counts):
    """The fixed line the counts are kept as."""
    if counts is None:
        return b"runner log: unreadable\n"
    top = counts.get("other by category", [])
    line = "runner log: " + ", ".join(f"{kind} {int(n)}" for kind, n in counts.items() if kind != "other by category")
    return (line + "\n" + ("runner other by category: " + ", ".join(f"{name} {int(n)}" for name, n in top) + "\n"
                           if top else "")).encode()


def flow(config, command):
    """One Maestro flow holding exactly one command this server wrote, run on the configured simulator by the
    session's driver."""
    started = utc_now()
    status, said = driver_call(config, "run", {"yaml": f"appId: {config['bundle']}\n---\n{command}\n"}, ACT_SECONDS)
    if status:
        ended = utc_now()
        refused(config, command, said, started, ended,
                runner_line(runner_counts(config, started, ended)) + driver_line(config))
        config["refusal_cause"] = "driver died" if said == DIED_LINE else driver_cause(said)
    return status


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(moment):
    """2026-10-25 03:02:57.755: the form `log show` gives the watchdog's stalls, on the runner's UTC clock."""
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def refused(config, command, said, started, ended, runner=b""):
    """One refused act's last lines, redacted, added to DRIVER_FILE while it has room; never an error. Its header has
    the act's start and end in UTC (coordinator, 25 Oct), so the watchdog's stalls line up with the acts; runner, the
    XCTest runner's counts for the act (runner_line), follows its lines."""
    lines, _ = kept_lines(b"".join(said.splitlines(keepends=True)[-DRIVER_LINES:]), "")
    act, _ = kept_lines(command.splitlines()[0].encode(), "")
    lines = (lines if not lines or lines.endswith(b"\n") else lines + b"\n") + runner
    part = (b"== refused: " + act.strip() + b" (action " + str(config["actions"]).encode() + b", " +
            stamp(started).encode() + b" to " + stamp(ended).encode() + b" UTC) ==\n" + lines)
    path = os.path.join(config["flows"], DRIVER_FILE)
    try:
        if os.path.getsize(path) + len(part) > MAX_DRIVER if os.path.exists(path) else len(part) > MAX_DRIVER:
            return
        with open(path, "ab") as output:
            output.write(part if part.endswith(b"\n") else part + b"\n")
    except OSError:
        pass


def ios_screenshot(config):
    path = os.path.join(config["flows"], "screen.png")
    status, _ = tool(config, ["xcrun", "simctl", "io", config["udid"], "screenshot", "--type=png", path])
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return None
    return data if not status and data.startswith(b"\x89PNG") and len(data) > 24 else None


# inspect_screen's compact tree (Maestro's ViewHierarchyFormatters, iOS schema): abbreviated keys, children under "c",
# defaults left out. Expanded back into the attributes this server reads from `maestro hierarchy`'s tree.
COMPACT_KEYS = {"b": "bounds", "txt": "text", "rid": "resource-id", "a11y": "accessibilityText", "hint": "hintText",
                "val": "value", "cls": "class"}


def expanded(elements, defaults, depth=0):
    """inspect_screen's elements as `maestro hierarchy`'s tree: {"attributes": {...}, "children": [...]}."""
    nodes = []
    for element in elements if isinstance(elements, list) and depth <= 200 else []:
        if not isinstance(element, dict):
            continue
        attributes = {name: value for name, value in defaults.items() if name not in COMPACT_KEYS}
        attributes.update({COMPACT_KEYS.get(name, name): value for name, value in element.items() if name != "c"})
        nodes.append({"attributes": attributes, "children": expanded(element.get("c"), defaults, depth + 1)})
    return nodes


def ios_nodes(node, depth=0):
    """Every element of `maestro hierarchy`'s JSON tree, as (attributes) dicts."""
    if depth > 200 or not isinstance(node, dict):
        return
    attributes = node.get("attributes")
    if isinstance(attributes, dict):
        yield attributes
    for child in node.get("children") or []:
        yield from ios_nodes(child, depth + 1)


def ios_tree(config):
    """The screen's tree in `maestro hierarchy`'s form, from the session driver's inspect_screen, or None (and the
    driver's cause in config["dump_cause"])."""
    status, data = driver_call(config, "inspect_screen", {}, ACT_SECONDS)
    try:
        found = json.loads(data.decode("utf-8", "replace")) if not status else None
    except ValueError:
        found = None
    schema = found.get("ui_schema") if isinstance(found, dict) else None
    defaults = schema.get("defaults") if isinstance(schema, dict) and isinstance(schema.get("defaults"), dict) else {}
    tree = {"attributes": {}, "children": expanded(found.get("elements"), defaults)} \
        if isinstance(found, dict) and isinstance(found.get("elements"), list) else None
    config["dump_cause"] = None if isinstance(tree, dict) else driver_cause(data)
    return tree if isinstance(tree, dict) else None


def ios_focused_value(config):
    """The focused element's value (its text when it has none) as the simulator holds it, unredacted, for the check
    alone: "" when it shows only its placeholder (an empty field's value can be its placeholder); None when the
    hierarchy could not be read or no element has focus."""
    field = next((node for node in ios_nodes(ios_tree(config)) if node.get("focused") in (True, "true")), None)
    if field is None:
        return None
    value = str(field.get("value") or field.get("text") or "")
    return "" if value == str(field.get("hintText") or "") else value


def ios_hierarchy(config):
    """The hierarchy in the screenshot's pixels (the same lines as Android's); "" when the screen's scale is unknown."""
    scale = screen_scale(config)
    if not scale:
        config["dump_cause"] = "scale unknown"
        return ""
    nodes = list(ios_nodes(ios_tree(config)))
    boxes = [re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(\d+),(\d+)\]", str(n.get("bounds", ""))) for n in nodes]
    if config.get("look"):
        chat_look(config, nodes, boxes)
    lines = []
    for attributes, box in zip(nodes, boxes):
        text = str(attributes.get("text") or "")
        desc = str(attributes.get("accessibilityText") or "")
        rid = str(attributes.get("resource-id") or "")
        clickable = attributes.get("clickable") in (True, "true")
        if not (text or desc or rid or clickable) or not box:
            continue
        centre = f"({round((int(box[1]) + int(box[3])) / 2 * scale)}, {round((int(box[2]) + int(box[4])) / 2 * scale)})"
        parts = [f"text={text!r}" if text else "", f"desc={desc!r}" if desc else "", f"id={rid}" if rid else "",
                 "clickable" if clickable else "", centre]
        lines.append(" | ".join(part for part in parts if part))
    return "\n".join(lines)[:MAX_HIERARCHY]


# N, 30 Oct (Samuel's "May chat is blank until I scroll"): the chat is a lazy stack anchored at its bottom, and after
# it opens from a For you card the viewport can be left past its content, so no message row is laid out where the
# screen shows. A row's MAY_DEVICE_LOOK=chat has every iOS hierarchy read that shows the chat (its header and its
# composer both listed) counted: the text elements between the header's bottom and the composer's top, within the
# header's width, that are not the chat's own chrome (an id starting "chat."). Each such look keeps that count and a
# screenshot taken at once (look-<n>.png in the agent's folder, for people to see whether the screen agrees), at most
# MAX_LOOKS of them; the count alone reaches the record, never a word of the text.
LOOK_ENV, MAX_LOOKS = "MAY_DEVICE_LOOK", 4
# Coordinator, 31 Oct (run 41146800805): a For you card opens its thread ("In May's chat"), not the May chat, and the
# thread is where Samuel saw the blank, so a thread counts too. Each conversation view's (header, composer) ids, as
# may-app names them (N, iOS #1527): the May chat, a message or item thread, a task thread. A read is a look when it
# shows one view's header and composer; that view's own ids (its prefix) are chrome, never messages.
LOOK_ANCHORS = (("chat.header", "chat.composer"), ("messageThread.header", "messageThread.composer"),
                ("thread.header", "thread.composer"))
# A thread's replies rule ("No replies yet", "N replies") is chrome in any view, whoever names it (N and the
# coordinator, 31 Oct): iOS's message thread (messageThread.replies), a task thread (thread.replies), and the design
# system's, which Android shows (mayThread.replies).
LOOK_CHROME = ("messageThread.replies", "thread.replies", "mayThread.replies")


def shown_words(attributes):
    """Whether a node shows words: its text, or its label. Run 41169912308: on iOS, Maestro puts a SwiftUI Text's string
    in accessibilityText (its label) and leaves text empty, so a thread showing May's message and its card counted
    none, and the row failed as blank. Android's content-desc counts the same way."""
    return any(str(attributes.get(key) or "").strip() for key in ("text", "accessibilityText", "content-desc"))


def chat_shown(nodes, boxes):
    """How many message texts a conversation view shows on screen, or None when the read shows none of them."""
    marks = {}
    for attributes, box in zip(nodes, boxes):
        rid = str(attributes.get("resource-id") or "")
        if box and any(rid in pair for pair in LOOK_ANCHORS) and rid not in marks:
            marks[rid] = [int(value) for value in box.groups()]
    pair = next((pair for pair in LOOK_ANCHORS if pair[0] in marks and pair[1] in marks), None)
    if pair is None:
        return None
    left, _, right, top = marks[pair[0]]
    bottom = marks[pair[1]][1]
    if bottom <= top:
        return None
    chrome = pair[0].rpartition(".")[0] + "."
    shown = 0
    for attributes, box in zip(nodes, boxes):
        rid = str(attributes.get("resource-id") or "")
        if not box or rid.startswith(chrome) or rid in LOOK_CHROME or not shown_words(attributes):
            continue
        x, y = (int(box[1]) + int(box[3])) / 2, (int(box[2]) + int(box[4])) / 2
        shown += left <= x <= right and top < y < bottom
    return shown


def chat_look(config, nodes, boxes):
    """One look at the chat (chat_shown) into config["looks"], with its screenshot, while fewer than MAX_LOOKS."""
    shown = chat_shown(nodes, boxes)
    if shown is None or len(config["looks"]) >= MAX_LOOKS:
        return
    data = ios_screenshot(config)
    name = f"look-{len(config['looks']) + 1}.png"
    if data is not None:
        with open(os.path.join(config["flows"], name), "wb") as output:
            output.write(data)
    config["looks"].append({"shown": shown, "shot": name if data is not None else None})


# Run 40155638626: the scale was the screenshot's width over the hierarchy's widest box, which was about 322 points on
# a 402-point screen, so it came to 3.75 and every tap the agent took from the screenshot landed at 0.8 of its target
# (the Agenda tab, about (193, 765), became (153, 613)). The scale is now the simulator's own: its device type's
# mainScreenScale, which every iPhone has at 2 or 3. Anything else is "scale unknown", and no tap or swipe is guessed.
NATIVE_SCALES, PROFILE = (2, 3), "Contents/Resources/profile.plist"
SCALE_UNKNOWN = "scale unknown: the simulator's device type gave no screen scale of 2 or 3"


def simctl_json(config, *args):
    status, data = tool(config, ["xcrun", "simctl", "list", *args, "-j"], timeout=60)
    try:
        return json.loads(data) if not status else None
    except ValueError:
        return None


def screen_scale(config):
    """The simulator's points-to-pixels scale, read once from its device type's profile; 0 when unknown."""
    if config["scale"] is None:
        config["scale"] = 0
        devices, types = simctl_json(config, "devices"), simctl_json(config, "devicetypes")
        found = [device.get("deviceTypeIdentifier") for runtime in ((devices or {}).get("devices") or {}).values()
                 if isinstance(runtime, list) for device in runtime
                 if isinstance(device, dict) and device.get("udid") == config["udid"]]
        bundles = [kind.get("bundlePath") for kind in (types or {}).get("devicetypes") or []
                   if isinstance(kind, dict) and found and kind.get("identifier") == found[0]]
        if bundles and isinstance(bundles[0], str) and bundles[0].endswith(".simdevicetype"):
            try:
                with open(os.path.join(bundles[0], PROFILE), "rb") as stream:
                    scale = plistlib.load(stream).get("mainScreenScale")
            except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
                scale = None
            if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale in NATIVE_SCALES:
                config["scale"] = int(scale)
        if not config["scale"]:
            refused(config, SCALE_UNKNOWN, b"", utc_now(), utc_now())
    return config["scale"]


def points(config, x, y):
    """A screenshot pixel as an iOS point, for Maestro's tapOn and swipe; None when the screen's scale is unknown."""
    scale = screen_scale(config)
    return f"{round(x / scale)},{round(y / scale)}" if scale else None


def compact(xml):
    """uiautomator's dump as one line per element that carries text, a description, an id or a click."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return ""
    lines = []
    for node in root.iter("node"):
        text, desc, rid = node.get("text", ""), node.get("content-desc", ""), node.get("resource-id", "")
        clickable = node.get("clickable") == "true"
        if not (text or desc or rid or clickable):
            continue
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        centre = f"({(int(bounds[1]) + int(bounds[3])) // 2}, {(int(bounds[2]) + int(bounds[4])) // 2})" \
            if bounds else "(?)"
        parts = [f"text={text!r}" if text else "", f"desc={desc!r}" if desc else "", f"id={rid}" if rid else "",
                 "clickable" if clickable else "", centre]
        lines.append(" | ".join(part for part in parts if part))
    return "\n".join(lines)[:MAX_HIERARCHY]


# Run 40138900610: the done-time dump came back empty for Android's notes and poll-skip, so the judge had nothing to
# read. A failed dump is tried once more, DUMP_RETRY_SECONDS later and --compressed, and its failure is named in the
# agent's private record, in fixed words: DUMP_FAILURES.
DUMP_RETRY_SECONDS = 2
DUMP_FAILURES = ("adb failed", "no idle state", "unreadable", "iOS hierarchy failed")
# Coordinator, 26 Oct (run 40290647340): "iOS hierarchy failed" never said why. The driver's own output for a failed
# iOS hierarchy read, and for the last refused act, is sorted into DRIVER_CAUSES' fixed words (first match in
# CAUSE_PATTERNS' order, else "other"); only the word is kept in the agent's record and the sealed report.
# Run 40309200520's diagnostics (coordinator, 26 Oct): a tap printed COMPLETED and then the act ran out of time, so
# Maestro was waiting for the screen to settle after it ("settle timeout": Maestro's own status line for the command
# ending "... COMPLETED", then tool's "(timed out)"). Anchored to that line's end, so typed text holding the word is
# not one (independent review of #139).
# Run 41164564379: a tap whose status line never reached COMPLETED (Maestro's line for the command ending "...", then
# nothing but the tool's "(timed out)") is "hung in act", not a snapshot time-out: the driver never came back from the
# act itself.
# Independent review of #175: "settle timeout" and "hung in act" read `maestro test`'s own step lines, which Maestro's
# MCP run never prints, so both are retired with that driver. A call the driver did not answer in time is "no answer",
# and a driver that could not be started or made ready is "did not start": this client's own fixed lines, whole.
CAUSE_PATTERNS = (("no answer", re.compile(rb"\A" + re.escape(NO_ANSWER_LINE) + rb"\Z")),
                  ("did not start", re.compile(rb"\A" + re.escape(NO_START_LINE) + rb"\Z")),
                  ("not idle", re.compile(rb"idle|quiescen", re.IGNORECASE)),
                  ("snapshot timeout", re.compile(rb"timed? ?out|timeout|snapshot", re.IGNORECASE)),
                  ("lost connection", re.compile(rb"connection (?:refused|reset|lost|closed)|broken pipe|unavailable|"
                                                 rb"failed to connect|lost connection|xctestdriver|end of stream",
                                                 re.IGNORECASE)),
                  ("app not running", re.compile(rb"not running|terminated|crash", re.IGNORECASE)))
# "typed nothing": an iOS type act Maestro took, after which the focused field holds none of the typed words
# (coordinator, 28 Oct: inputText COMPLETED and no text appeared).
STUCK_CAUSES = ("snapshot timeout", "no answer")
DRIVER_CAUSES = (*(cause for cause, _ in CAUSE_PATTERNS), "scale unknown", "typed nothing", "driver died", "other")


def driver_cause(output):
    """The fixed word for what the driver said when it failed (CAUSE_PATTERNS), "other" when nothing matches."""
    return next((cause for cause, pattern in CAUSE_PATTERNS if pattern.search(output or b"")), "other")


def android_dump(config):
    """(uiautomator's XML, None), or (None, why) once both tries failed."""
    why = None
    for argv in (("dump", "/dev/tty"), ("dump", "--compressed", "/dev/tty")):
        if why:
            time.sleep(DUMP_RETRY_SECONDS)
        status, data = adb(config, "exec-out", "uiautomator", *argv)
        xml = data.split(b"UI hierchary dumped")[0].split(b"UI hierarchy dumped")[0]
        if status:
            why = "adb failed"
        elif b"idle state" in data:
            why = "no idle state"
        else:
            try:
                ElementTree.fromstring(xml)
            except ElementTree.ParseError:
                why = "unreadable"
            else:
                return xml, None
    return None, why


# M, 25 Oct: the chat header's idle mark loops forever (an infinite transition), so Compose never goes idle and
# uiautomator's dump waits in vain. The done-time dump runs with the emulator's three animation scales at 0, which the
# app reads as reduced motion and stops its loops; the scales before it are put back whatever happens, so the
# recording keeps its motion. device_tests.ANIMATION_SETTINGS names the same three.
ANIMATION_SETTINGS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
SCALE = re.compile(rb"[0-9]{1,3}(?:\.[0-9]{1,3})?")
STILL_SECONDS = 1


def stilled_dump(config):
    """android_dump with the animation scales at 0, each put back to its earlier value (1 when unreadable) after."""
    before = {}
    for setting in ANIMATION_SETTINGS:
        status, value = adb(config, "shell", "settings", "get", "global", setting)
        before[setting] = value.strip().decode() if not status and SCALE.fullmatch(value.strip()) else "1"
    try:
        for setting in ANIMATION_SETTINGS:
            adb(config, "shell", "settings", "put", "global", setting, "0")
        time.sleep(STILL_SECONDS)
        return android_dump(config)
    finally:
        for setting, value in before.items():
            adb(config, "shell", "settings", "put", "global", setting, value)


def current_hierarchy(config, still=False):
    """The screen's hierarchy now, compacted (one element per line), or "" when it could not be read (and why, in
    config["dump_failure"]). still: Android's animations stopped for the dump (stilled_dump), for the judge's look."""
    if config["platform"] == "ios":
        text = ios_hierarchy(config)
        config["dump_failure"] = None if text else "iOS hierarchy failed"
        config["dump_cause"] = None if text else config.get("dump_cause") or "other"
        return text
    xml, config["dump_failure"] = (stilled_dump if still else android_dump)(config)
    return "" if xml is None else compact(xml)


def record(config, outcome, summary):
    """The agent's result for the judge: its hierarchy through kept_lines and within MAX_HIERARCHY, as the summary is
    redacted where it is sealed."""
    kept, _ = kept_lines(config["hierarchy"].encode(), "")
    # Written beside it and moved into place, so a server that exits at once (gone()) never leaves it cut short.
    partial = config["result"] + ".partial"
    try:
        with open(partial, "w", encoding="utf-8") as output:
            json.dump({"outcome": outcome, "summary": summary[:MAX_SUMMARY], "actions": config["actions"],
                       "hierarchy": kept.decode("utf-8", "replace")[:MAX_HIERARCHY],
                       "hierarchy_failure": config.get("hierarchy_failure"),
                       "hierarchy_cause": config.get("hierarchy_cause"), "refusal_cause": config.get("refusal_cause"),
                       **({"wedged": True} if config.get("wedged") else {}),
                       **({"typing_failed": True} if config.get("typing_failed") else {}),
                       **({"typed": config["typed"]} if config.get("slow") else {}),
                       **({"looks": config["looks"]} if config.get("look") else {})}, output)
        os.replace(partial, config["result"])
    finally:
        if os.path.exists(partial):
            os.remove(partial)


def call(name, args, config):
    """(failed, content) for one tool call. After each call but done, the result is rewritten as "unfinished" with
    the steps so far and the last hierarchy, so an agent that runs out of time still leaves how far it got (run
    40209923275: iOS notes saved its task, then timed out with nothing recorded; coordinator, 25 Oct)."""
    failed, content = act(name, args, config)
    if name != "done" and not config.get("done") and not config.get("wedged"):
        record(config, UNFINISHED, "")
    return failed, content


def act(name, args, config):
    """(failed, content) for one tool call, before call() records the progress."""
    if not isinstance(args, dict):
        args = {}
    if name == "done":
        outcome, summary = args.get("outcome"), args.get("summary")
        if outcome not in ("completed", "blocked") or not isinstance(summary, str):
            return True, [{"type": "text", "text": "done needs outcome (completed or blocked) and a summary"}]
        # The judge's evidence is the screen as the agent leaves it, taken here (proof run 40123018291: the judge
        # read the agent's last look, from before it sent the message, and failed a delivered chat).
        fresh = current_hierarchy(config, still=True)
        config["hierarchy_failure"] = None if fresh else config.get("dump_failure")
        config["hierarchy_cause"] = None if fresh else config.get("dump_cause")
        why = config["hierarchy_failure"] or "an empty screen"
        config["hierarchy"] = fresh or (f"(no fresh hierarchy at done: {why}; the agent's last look follows)\n" +
                                        config["hierarchy"] if config["hierarchy"] else
                                        f"(no hierarchy at done: {why}; the agent never read one)")
        record(config, outcome, summary)
        config["done"] = True
        return False, [{"type": "text", "text": "Recorded. Stop now."}]
    if name not in {tool["name"] for tool in TOOLS}:
        return True, [{"type": "text", "text": "Unknown tool"}]
    if config.get("wedged"):
        return True, [{"type": "text", "text": WEDGED_TEXT}]
    if config["actions"] >= config["steps"]:
        return True, [{"type": "text", "text": f"The {config['steps']}-step limit is reached: call done now"}]
    config["actions"] += 1
    ios = config["platform"] == "ios"
    if name == "screenshot":
        if ios:
            data = ios_screenshot(config)
            status = data is None
        else:
            status, data = adb(config, "exec-out", "screencap", "-p")
        if status or not data.startswith(b"\x89PNG"):
            return True, [{"type": "text", "text": "No screenshot this time; try again"}]
        return False, [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}]
    if name == "hierarchy":
        text = current_hierarchy(config)
        if not text:
            return True, [{"type": "text", "text": "No hierarchy this time; take a screenshot instead"}]
        config["hierarchy"] = text
        # What the agent reads goes through the same redaction as everything else it sees.
        kept, _ = kept_lines(text.encode(), "")
        return False, [{"type": "text", "text": kept.decode("utf-8", "replace")}]
    ints = {key: args.get(key) for key in ("x", "y", "x1", "y1", "x2", "y2", "ms", "seconds")}
    if any(value is not None and (type(value) is not int or not 0 <= value <= 4000) for value in ints.values()):
        return True, [{"type": "text", "text": "Coordinates and times are whole numbers in range"}]
    if name == "wait" and ints["seconds"] is not None and 1 <= ints["seconds"] <= 30:
        time.sleep(ints["seconds"])
        return False, [{"type": "text", "text": "Waited"}]
    if ios:
        return ios_act(name, args, ints, config)
    if name == "tap" and ints["x"] is not None and ints["y"] is not None:
        argv = ["shell", "input", "tap", str(ints["x"]), str(ints["y"])]
    elif name == "type" and isinstance(args.get("text"), str) and TEXT.fullmatch(args["text"]):
        # adb's input text reads %s as a space; every other allowed character is literal to the device shell.
        argv = ["shell", "input", "text", args["text"].replace(" ", "%s")]
    elif name == "swipe" and None not in (ints["x1"], ints["y1"], ints["x2"], ints["y2"]):
        argv = ["shell", "input", "swipe", *(str(ints[k]) for k in ("x1", "y1", "x2", "y2")), str(ints["ms"] or 300)]
    elif name == "back":
        argv = ["shell", "input", "keyevent", "4"]
    else:
        return True, [{"type": "text", "text": "Those arguments are not allowed for " + name}]
    if name == "type" and config.get("slow"):
        status = slow_type(config, args["text"])
    else:
        status, _ = adb(config, *argv)
    if status:
        return True, [{"type": "text", "text": "The device did not take it"}]
    if name != "type":
        return False, [{"type": "text", "text": "Done"}]
    # What landed, so the agent never types it again piece by piece (run 40119835677: three minutes on one sentence).
    xml = settled_dump(config, args["text"])
    landed = None if xml is None else focused_text(xml)
    if landed is None:
        return False, [{"type": "text", "text": "Done; the focused field could not be read, so take a hierarchy"}]
    # M and the coordinator, 3 Nov (runs 41504861439 and 41435985226): adb's input text cut a sentence short ("with
    # three cho") and the cut message was sent. The field is read back: when it does not end with the whole text, the
    # part that did not land is typed once more, and the field read again; still short, the run is marked a typing
    # failure (the harness's, never May's: the judge reports it infra) and the agent is told to stop.
    rest = untyped(args["text"], focused_text(xml, raw=True), landed)
    if rest:
        status, _ = adb(config, "shell", "input", "text", rest.replace(" ", "%s"))
        xml = settled_dump(config, args["text"])
        landed = None if status or xml is None else focused_text(xml)
        if landed is None or untyped(args["text"], focused_text(xml, raw=True), landed):
            config["typing_failed"] = True
            shown = "could not be read" if landed is None else f"reads {landed!r}"
            return True, [{"type": "text", "text": f"The text did not land whole, even typed again: the field {shown}. "
                                                   "Do not send it; call done now with outcome blocked"}]
    return False, [{"type": "text", "text": f"Done. The focused field now reads: {landed!r}"}]


# M and the coordinator, 6 Nov (run 42244126293's push-open: "did not land whole", yet the judge saw the exact message
# sent): Compose updates the field on a later frame than adb's input text returns, so a read at once can see a prefix,
# and typing that "rest" again doubles it once the late frame lands. The field is read until it holds the whole text
# or two reads SETTLE_GAP apart agree, at most SETTLE_READS reads, before it is compared or anything is typed again.
SETTLE_GAP, SETTLE_READS = 0.3, 7


def settled_dump(config, text):
    """uiautomator's XML once the focused field has settled: it ends with text, or two reads SETTLE_GAP apart hold the
    same field; the last read when it never settles; None when no read worked."""
    xml, _ = android_dump(config)
    for _ in range(SETTLE_READS - 1):
        field = None if xml is None else focused_text(xml, raw=True)
        if field is not None and not untyped(text, field, ""):
            return xml
        time.sleep(SETTLE_GAP)
        again, _ = android_dump(config)
        if again is not None and field is not None and focused_text(again, raw=True) == field:
            return again
        xml = again if again is not None else xml
    return xml


def untyped(text, field, shown):
    """The end of text that did not land in the field (the field's whole text, field, ends with the part that did),
    "" when the field ends with the whole text, or when it is not compared: the agent was shown it hidden as a
    credential (shown), so nothing of it is typed again."""
    field, text = field.rstrip(), text.rstrip()  # a trailing space is the field's, or the next word's, never a cut
    if shown == "(hidden)" or not text or field.endswith(text):
        return ""
    for cut in range(len(text) - 1, 0, -1):
        if field.endswith(text[:cut]):
            return text[cut:]
    return text


def focused_text(xml, raw=False):
    """The focused field's text as the agent may see it: through kept_lines (a field that could hold a credential is
    "(hidden)", an email address keeps its domain alone), at most MAX_FIELD characters; None when no field is focused.
    raw: the field's whole text, for comparing with what was typed alone, never shown to the agent or kept."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    field = next((node for node in root.iter("node") if node.get("focused") == "true"), None)
    if field is None:
        return None
    return field.get("text", "") if raw else shown_field(field.get("text", ""))


def shown_field(text):
    """A field's text through kept_lines (a field that could hold a credential is "(hidden)", an email address keeps
    its domain alone), on one line and at most MAX_FIELD characters."""
    kept, dropped = kept_lines(text.replace("\n", " ").replace("\r", " ").encode(), "")
    return "(hidden)" if dropped else kept.decode("utf-8", "replace")[:MAX_FIELD]


# Maestro waits after a tap or swipe for the screen to settle; in runs 40148178587 and 40155638626 an iOS tap was
# COMPLETED and then never returned, and the driver took nothing after it. Each act's settle wait is capped (Maestro
# calls the cap best effort), so a screen that keeps redrawing cannot hold the act.
SETTLE_MS = 3000
# Coordinator, 28 Oct (runs 40478288501 and 40666988126): iOS inputText into a focused field came back COMPLETED and
# no text appeared, and the agent was told "Done". After a type Maestro took, the hierarchy is read once and the agent
# is told what the focused field holds, as on Android. A field holding none of the typed words is a refused act
# (refusal_cause "typed nothing", counted toward FAIL_FAST, kept in DRIVER_FILE); a field that cannot be read is said
# so and counts as taken.
TYPED_NOTHING_LINE = b"typed nothing: Maestro took the act, and the focused field holds none of the typed words\n"


def ios_act(name, args, ints, config):
    """One act on the simulator: a Maestro flow of one fixed command, its values checked here first."""
    settle = f"\n    waitToSettleTimeoutMs: {SETTLE_MS}"
    if name in ("tap", "swipe") and not screen_scale(config):
        return True, [{"type": "text", "text": "The device's screen scale is unknown, so no tap or swipe can be placed"}]
    if name == "tap" and ints["x"] is not None and ints["y"] is not None:
        command = f'- tapOn:\n    point: "{points(config, ints["x"], ints["y"])}"{settle}'
    elif name == "type" and isinstance(args.get("text"), str) and TEXT.fullmatch(args["text"]):
        # TEXT holds no quote, backslash or newline, so the double-quoted YAML scalar is exactly the text.
        command = f'- inputText: "{args["text"]}"'
    elif name == "swipe" and None not in (ints["x1"], ints["y1"], ints["x2"], ints["y2"]):
        command = (f'- swipe:\n    start: "{points(config, ints["x1"], ints["y1"])}"\n'
                   f'    end: "{points(config, ints["x2"], ints["y2"])}"\n    duration: {ints["ms"] or 300}{settle}')
    elif name == "back":
        command = f'- swipe:\n    start: "1%,50%"\n    end: "80%,50%"\n    duration: 300{settle}'
    else:
        return True, [{"type": "text", "text": "Those arguments are not allowed for " + name}]
    started = utc_now() if name == "type" else None
    status = flow(config, command)
    landed = None
    if name == "type" and not status:
        landed = ios_focused_value(config)
        if landed is not None and not typed_landed(args["text"], landed):
            status = 1
            refused(config, command, TYPED_NOTHING_LINE, started, utc_now())
            config["refusal_cause"] = "typed nothing"
    config["refusals"] = config["refusals"] + 1 if status else 0
    # An act the driver did not answer, after a screen read it did not answer either, stops the agent at once, as a
    # snapshot time-out after a timed-out read did (independent review of #175: the MCP driver's time-out is "no answer").
    stuck = bool(status) and config.get("refusal_cause") in STUCK_CAUSES and config.get("dump_cause") in STUCK_CAUSES
    if not status:
        # A taken act changes the screen, so an earlier read's time-out no longer says it is stuck (independent review
        # of #139).
        config["dump_cause"] = None
    if config["refusals"] >= FAIL_FAST or stuck:
        config["wedged"] = True
        # Recorded now, so a stopped agent that never calls done still leaves its reason; a later done replaces it.
        record(config, "blocked", WEDGED_TEXT)
        return True, [{"type": "text", "text": WEDGED_TEXT}]
    if name == "type" and landed is not None:
        said = "Done. The focused field now reads:" if not status else "The device did not take it: the focused field reads"
        return bool(status), [{"type": "text", "text": f"{said} {shown_field(landed)!r}"}]
    if name == "type" and not status:
        return False, [{"type": "text", "text": "Done; the focused field could not be read, so take a hierarchy"}]
    return bool(status), [{"type": "text", "text": "Done" if not status else "The device did not take it"}]


WORD = re.compile(r"[\w@.:-]+")


def typed_landed(text, value):
    """Whether any whole word of the typed text is a word of the field after the act (autocorrect may change some of
    them; a part of a word never counts, so "note" is not in "Add notes")."""
    held = set(WORD.findall(value.casefold()))
    return any(word in held for word in WORD.findall(text.casefold()))


def answer(message, config):
    """The JSON-RPC answer to one message, or None for a notification."""
    if not isinstance(message, dict) or "id" not in message:
        return None
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "may-device", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        failed, content = call(params.get("name"), params.get("arguments"), config)
        result = {"content": content, "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(stdin, stdout, config):
    for line in stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = answer(message, config)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def gone(config, exit=os._exit):
    """Kills the session driver's process group and exits now: for a server that was stopped or lost its parent, even
    in the middle of an act or of stopping the driver."""
    if config.get("driver"):
        config["driver"].kill()
    exit(0)


def watch_parent(config, parent, getppid=os.getppid, sleep=time.sleep, exit=os._exit):
    """Waits for this server's parent (claude) to go, then ends as gone() does. A parent of 1 is gone already: this
    server was orphaned before it started watching."""
    while parent != 1 and getppid() == parent:
        sleep(PARENT_SECONDS)
    gone(config, exit)


def guard(config, parent, handle=signal.signal, thread=threading.Thread):
    """iOS alone: a SIGTERM, or the loss of the parent this server started under, ends it as gone() does. Android
    keeps its own exits, so stilled_dump always puts the animation scales back (re-review of #175)."""
    if config["platform"] != "ios":
        return False
    handle(signal.SIGTERM, lambda number, frame: gone(config))
    thread(target=watch_parent, args=(config, parent), daemon=True).start()
    return True


if __name__ == "__main__":
    # The parent this server started under, read before anything else, so a claude killed meanwhile is still seen.
    started_under = os.getppid()
    sys.stderr = open(os.devnull, "w")
    found = settings(os.environ)
    if found is None:
        sys.exit(1)
    # On iOS a stopped server, or one whose claude was killed, kills its Maestro driver's group and exits at once, so
    # the driver never outlives this scenario (independent review of #175).
    guard(found, started_under)
    try:
        serve(sys.stdin, sys.stdout, found)
    except Exception:
        sys.exit(1)
    finally:
        # The session's Maestro driver ends with this server, so its runner never outlives the agent.
        if found.get("driver"):
            found["driver"].stop()
