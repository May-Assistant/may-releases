"""The device server for device_agent.py: the only tools the device-test agent has, over stdio as `device`.

Claude Code runs the agent with no built-in tool (--tools ""), no settings, and this server alone
(--strict-mcp-config); its permission mode is dontAsk and only mcp__device is allowed. So everything the agent can do
is here: look (screenshot, hierarchy), act (tap, type, swipe, back, wait) and finish (done). It cannot run a command,
read or write a file, or reach the network itself.

- Android (MAY_DEVICE_PLATFORM android): the emulator through adb.
- iOS (ios): the simulator, through the pinned Maestro's driver (the coordinator's choice, 21 Oct, over adding idb).
  Each act is one Maestro flow this server writes itself, holding exactly one fixed command (tapOn point, inputText,
  swipe) for the app's bundle id, run with `maestro --device <udid> test`; the agent never gives Maestro YAML, so
  Maestro's scripts, links and other apps are out of its reach. Screenshots come from simctl, the hierarchy from
  `maestro hierarchy`. Coordinates are the screenshot's pixels, as on Android; they become iOS points by the ratio of
  the screenshot's width to the hierarchy's root. back is a swipe in from the left edge.

- type takes plain text only (letters, digits, spaces and . , ? ! : - _ @), so nothing reaches adb's shell as a
  command.
- At most MAX_ACTIONS look or act calls; after that every call but done answers that the limit is reached.
- done records the agent's own outcome (completed or blocked) and a short summary to MAY_DEVICE_AGENT_RESULT, which
  device_tests.py keeps on the runner (it goes to the encrypted report only). The last hierarchy is kept beside it
  for the judge.

It writes nothing to stdout but its JSON-RPC answers, and nothing at all to stderr.
"""

import base64
import json
import os
import re
import struct
import subprocess
import sys
import time
from xml.etree import ElementTree

PROTOCOL = "2025-06-18"
MAX_ACTIONS = 80
MAX_HIERARCHY = 40_000
MAX_SUMMARY = 1000
TEXT = re.compile(r"[A-Za-z0-9 .,?!:@_-]{1,200}")
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
    {"name": "type", "description": ("Type text into the focused field: letters, digits, spaces and . , ? ! : - _ @ "
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


def settings(environ):
    result = environ.get("MAY_DEVICE_AGENT_RESULT", "")
    if not result:
        return None
    base = {"result": result, "actions": 0, "hierarchy": ""}
    if environ.get("MAY_DEVICE_PLATFORM", "android") == "android":
        adb, serial = environ.get("MAY_DEVICE_ADB", ""), environ.get("MAY_DEVICE_SERIAL", "")
        if not (adb and re.fullmatch(r"emulator-[0-9]{4}", serial)):
            return None
        return dict(base, platform="android", adb=adb, serial=serial)
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
                flows=os.path.dirname(result))


def adb(config, *args, timeout=60):
    """One adb call on the configured emulator: (exit status, stdout bytes). Nothing reaches this server's output."""
    try:
        done = subprocess.run([config["adb"], "-s", config["serial"], *args], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 1, b""
    return done.returncode, done.stdout


def tool(config, argv, timeout=90):
    """One simctl or Maestro call for the iOS simulator: (exit status, stdout bytes). Nothing reaches this server's
    output."""
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=config["env"])
    except (OSError, subprocess.TimeoutExpired):
        return 1, b""
    return done.returncode, done.stdout


def flow(config, command):
    """One Maestro flow holding exactly one command this server wrote, run on the configured simulator."""
    path = os.path.join(config["flows"], "act.yaml")
    with open(path, "w", encoding="utf-8") as output:
        output.write(f"appId: {config['bundle']}\n---\n{command}\n")
    status, _ = tool(config, [config["maestro"], "--device", config["udid"], "test", path])
    return status


def ios_screenshot(config):
    path = os.path.join(config["flows"], "screen.png")
    status, _ = tool(config, ["xcrun", "simctl", "io", config["udid"], "screenshot", "--type=png", path])
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return None
    return data if not status and data.startswith(b"\x89PNG") and len(data) > 24 else None


def ios_nodes(node, depth=0):
    """Every element of `maestro hierarchy`'s JSON tree, as (attributes) dicts."""
    if depth > 200 or not isinstance(node, dict):
        return
    attributes = node.get("attributes")
    if isinstance(attributes, dict):
        yield attributes
    for child in node.get("children") or []:
        yield from ios_nodes(child, depth + 1)


def ios_hierarchy(config):
    """The hierarchy in the screenshot's pixels (the same lines as Android's), and the points-to-pixels scale."""
    if config["scale"] is None:
        shot = ios_screenshot(config)
        if shot is None:
            return ""
        width = struct.unpack(">I", shot[16:20])[0]
    status, data = tool(config, [config["maestro"], "--device", config["udid"], "hierarchy"])
    try:
        tree = json.loads(data[data.find(b"{"):]) if not status else None
    except ValueError:
        tree = None
    nodes = list(ios_nodes(tree))
    boxes = [re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(\d+),(\d+)\]", str(n.get("bounds", ""))) for n in nodes]
    if config["scale"] is None:
        root = max((int(box[3]) for box in boxes if box), default=0)
        if not root:
            return ""
        config["scale"] = width / root
    lines = []
    for attributes, box in zip(nodes, boxes):
        text = str(attributes.get("text") or "")
        desc = str(attributes.get("accessibilityText") or "")
        rid = str(attributes.get("resource-id") or "")
        clickable = attributes.get("clickable") in (True, "true")
        if not (text or desc or rid or clickable) or not box:
            continue
        scale = config["scale"]
        centre = f"({round((int(box[1]) + int(box[3])) / 2 * scale)}, {round((int(box[2]) + int(box[4])) / 2 * scale)})"
        parts = [f"text={text!r}" if text else "", f"desc={desc!r}" if desc else "", f"id={rid}" if rid else "",
                 "clickable" if clickable else "", centre]
        lines.append(" | ".join(part for part in parts if part))
    return "\n".join(lines)[:MAX_HIERARCHY]


def points(config, x, y):
    """A screenshot pixel as an iOS point, for Maestro's tapOn and swipe."""
    if config["scale"] is None:
        ios_hierarchy(config)
    scale = config["scale"] or 3
    return f"{round(x / scale)},{round(y / scale)}"


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


def record(config, outcome, summary):
    with open(config["result"], "w", encoding="utf-8") as output:
        json.dump({"outcome": outcome, "summary": summary[:MAX_SUMMARY], "actions": config["actions"],
                   "hierarchy": config["hierarchy"]}, output)


def call(name, args, config):
    """(failed, content) for one tool call."""
    if not isinstance(args, dict):
        args = {}
    if name == "done":
        outcome, summary = args.get("outcome"), args.get("summary")
        if outcome not in ("completed", "blocked") or not isinstance(summary, str):
            return True, [{"type": "text", "text": "done needs outcome (completed or blocked) and a summary"}]
        record(config, outcome, summary)
        return False, [{"type": "text", "text": "Recorded. Stop now."}]
    if name not in {tool["name"] for tool in TOOLS}:
        return True, [{"type": "text", "text": "Unknown tool"}]
    if config["actions"] >= MAX_ACTIONS:
        return True, [{"type": "text", "text": f"The {MAX_ACTIONS}-step limit is reached: call done now"}]
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
        if ios:
            status, text = 0, ios_hierarchy(config)
        else:
            status, data = adb(config, "exec-out", "uiautomator", "dump", "/dev/tty")
            text = compact(data.split(b"UI hierchary dumped")[0].split(b"UI hierarchy dumped")[0])
        if status or not text:
            return True, [{"type": "text", "text": "No hierarchy this time; take a screenshot instead"}]
        config["hierarchy"] = text
        return False, [{"type": "text", "text": text}]
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
    status, _ = adb(config, *argv)
    return bool(status), [{"type": "text", "text": "Done" if not status else "The device did not take it"}]


def ios_act(name, args, ints, config):
    """One act on the simulator: a Maestro flow of one fixed command, its values checked here first."""
    if name == "tap" and ints["x"] is not None and ints["y"] is not None:
        command = f'- tapOn:\n    point: "{points(config, ints["x"], ints["y"])}"'
    elif name == "type" and isinstance(args.get("text"), str) and TEXT.fullmatch(args["text"]):
        # TEXT holds no quote, backslash or newline, so the double-quoted YAML scalar is exactly the text.
        command = f'- inputText: "{args["text"]}"'
    elif name == "swipe" and None not in (ints["x1"], ints["y1"], ints["x2"], ints["y2"]):
        command = (f'- swipe:\n    start: "{points(config, ints["x1"], ints["y1"])}"\n'
                   f'    end: "{points(config, ints["x2"], ints["y2"])}"\n    duration: {ints["ms"] or 300}')
    elif name == "back":
        command = '- swipe:\n    start: "1%,50%"\n    end: "80%,50%"\n    duration: 300'
    else:
        return True, [{"type": "text", "text": "Those arguments are not allowed for " + name}]
    status = flow(config, command)
    return bool(status), [{"type": "text", "text": "Done" if not status else "The device did not take it"}]


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


if __name__ == "__main__":
    sys.stderr = open(os.devnull, "w")
    found = settings(os.environ)
    if found is None:
        sys.exit(1)
    try:
        serve(sys.stdin, sys.stdout, found)
    except Exception:
        sys.exit(1)
