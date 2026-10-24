"""The device-test agent and judge (device_tests.py): Claude Code, headless, with the repository's
CLAUDE_CODE_OAUTH_TOKEN, as review.py runs it.

- The CLI is review.py's pinned release (CLI_URL, checked against CLI_SHA512 before the binary is written), installed
  once per job under $RUNNER_TEMP/may-claude. The iOS job's macOS runner takes the same release's darwin-arm64 package
  (DARWIN_URL, DARWIN_SHA512): the linux-x64 binary cannot run there.
- The model is the variable MAY_DEVICE_TESTS_MODEL, never this repository.
- The agent has no built-in tool (--tools ""), user settings only from a fresh, empty HOME, only the device server
  (device_mcp.py, --strict-mcp-config) and permission mode dontAsk with only mcp__device allowed: it can look at and
  act on the emulator, and nothing else. It runs in an empty directory with a fresh HOME, and its environment holds
  the OAuth token alone: never the staging secret or session.
- The judge has no tool and no server at all; it reads the goal, the agent's own report and the last hierarchy, and
  answers in a fixed JSON schema.
- A rate limit or usage limit (Claude Code's own result words, review.outcome) is "rate_limited", which
  device_tests.py records as skipped, never as a pass.

Nothing from either run is printed: their output stays in the run's private folder and the encrypted report.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

from review import CLI_MAX_BYTES, CLI_SHA512, CLI_URL, checked_cli, header_safe, outcome

# review.py's pinned release for macOS on Apple silicon (macos-15): npm's darwin-arm64 platform package, a single
# Mach-O binary; DARWIN_SHA512 is the registry's dist.integrity.
DARWIN_URL = CLI_URL.replace("linux-x64", "darwin-arm64")
DARWIN_SHA512 = "sha512-vqtx3kS3r1hNLMk+N8JMG5J4kcLHIbI3t/NSIdcFQZrRVCFA6NtcxJyn/B+Sf3GxJaFJOCEfoBQXsFo10+Oj9A=="
OAUTH, MODEL_VAR = "CLAUDE_CODE_OAUTH_TOKEN", "MAY_DEVICE_TESTS_MODEL"
MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,60}")
SERVER = Path(__file__).resolve().parent / "device_mcp.py"
# iOS acts are a Maestro run each, several seconds apiece, so its agent has longer.
# IOS_AGENT_SECONDS leaves room inside one test session (device_tests.SESSION_SECONDS) for the launch, recording and
# server reads, so a session never ends mid-scenario (run 40063426822; the coordinator kept sessions at 30 minutes).
AGENT_SECONDS, IOS_AGENT_SECONDS, JUDGE_SECONDS = 15 * 60, 20 * 60, 5 * 60
RATE_LIMITED = ("blocking_limit", "rapid_refill_breaker")
AGENT_PROMPT = (
    "You are testing the May Playground app on an Android emulator, signed in to a staging test account. You have "
    "only the device tools. Rules: never sign out; never open account, device, billing or connection settings; "
    "never connect or disconnect an app; never approve, pay, or send anything to anyone outside the May app's own "
    "chat. Put the tag {tag} in every name, title or message you create, exactly as written. Start with a screenshot "
    "and the hierarchy. When the goal is done and visible on screen, call done with outcome completed; if you cannot "
    "do it, call done with outcome blocked. Either way, say in the summary what you saw.")
JUDGE_PROMPT = (
    "You judge one device test of the May app. Decide from the evidence below, nothing else.\n\nGoal: {goal}\n\n"
    "The agent's own report: outcome {outcome}, after {actions} actions. Its summary:\n{summary}\n\n"
    "The screen as the agent left it, read when it finished (accessibility hierarchy, one element per line):\n"
    "{hierarchy}\n\n"
    "May's server, for each message this run sent with its tag (delivery codes only; none when the goal sends "
    "nothing): {delivery}\n\n"
    "verdict is pass only if the hierarchy and the report together show the goal achieved; otherwise fail. A "
    "delivery code of succeeded is the server's record that May answered this run's message. note says why in at "
    "most three sentences.")
JUDGE_SCHEMA = json.dumps({"type": "object", "additionalProperties": False, "required": ["verdict", "note"],
                           "properties": {"verdict": {"type": "string", "enum": ["pass", "fail"]},
                                          "note": {"type": "string", "maxLength": 600}}})


class AgentError(ValueError):
    pass


def package(system=sys.platform):
    """(URL, sha512) of the pinned Claude Code package for this runner: darwin-arm64 on macOS, linux-x64 otherwise."""
    return (DARWIN_URL, DARWIN_SHA512) if system == "darwin" else (CLI_URL, CLI_SHA512)


def install(root, fetch=None, system=sys.platform):
    """review.py's pinned Claude Code release, for this runner's platform, under root/may-claude, once per job: the
    binary's path."""
    binary = root / "may-claude" / "claude"
    if binary.is_file() and not binary.is_symlink():
        return binary
    url, integrity = package(system)
    if fetch is None:
        with urllib.request.urlopen(url, timeout=300) as response:
            data = response.read(CLI_MAX_BYTES + 1)
    else:
        data = fetch(url)
    if len(data) > CLI_MAX_BYTES:
        raise AgentError("The Claude Code download is larger than expected")
    try:
        cli = checked_cli(data, integrity)
    except Exception:
        raise AgentError("The Claude Code download does not match its pinned sha512") from None
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary.write_bytes(cli)
    binary.chmod(0o700)
    return binary


def settings(environ):
    """(OAuth token, model) from this step's environment, both checked; the token is popped."""
    token = environ.pop(OAUTH, "").strip()
    model = environ.get(MODEL_VAR, "").strip()
    if not token or not header_safe(token):
        raise AgentError("CLAUDE_CODE_OAUTH_TOKEN is not set, or holds a character a header can't carry")
    if not MODEL.fullmatch(model):
        raise AgentError("MAY_DEVICE_TESTS_MODEL is not a Claude model name")
    return token, model


def fresh(path):
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700, parents=True)
    return path


def claude_env(work, token):
    """Only what Claude Code needs: the OAuth token, a fresh HOME and TMPDIR, no other secret or Actions variable."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fresh(work / "home")),
            "TMPDIR": str(fresh(work / "tmp")), "LANG": "C.UTF-8", "NO_COLOR": "1", OAUTH: token,
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1"}


def base_command(cli, prompt, model):
    return [str(cli), "-p", prompt, "--model", model, "--output-format", "json", "--no-session-persistence",
            "--setting-sources", "user", "--strict-mcp-config", "--tools", "", "--permission-mode", "dontAsk"]


def finished(stdout_path):
    """Claude Code's own status for a run: "rate_limited", "error" or "ok" (review.outcome reads no free text)."""
    found = outcome(stdout_path)
    if found.get("api_status") == 429 or found.get("api_error") == "usage_limit_reached" or \
            found.get("terminal_reason") in RATE_LIMITED:
        return "rate_limited"
    return "error" if not found or found.get("is_error") else "ok"


def run_agent(cli, goal, tag, device, work, token, model, seconds=AGENT_SECONDS):
    """One scenario's agent run: {"state": ok|rate_limited|error|timeout, and the device server's record}. device is the
    device server's settings (MAY_DEVICE_PLATFORM and the emulator's or the simulator's handles), never a secret."""
    work = fresh(work)
    result = work / "result.json"
    config = {"mcpServers": {"device": {"type": "stdio", "command": sys.executable,
                                        "args": ["-I", "-S", "-B", str(SERVER)],
                                        "env": dict(device, MAY_DEVICE_AGENT_RESULT=str(result))}}}
    (work / "mcp.json").write_text(json.dumps(config))
    command = base_command(cli, goal.replace("{tag}", tag), model) + [
        "--mcp-config", str(work / "mcp.json"), "--allowedTools", "mcp__device",
        "--append-system-prompt", AGENT_PROMPT.format(tag=tag)]
    try:
        with (work / "agent.json").open("wb") as out, (work / "agent.err").open("wb") as err:
            subprocess.run(command, cwd=fresh(work / "cwd"), env=claude_env(work, token), stdout=out, stderr=err,
                           stdin=subprocess.DEVNULL, timeout=seconds)
    except subprocess.TimeoutExpired:
        state = "timeout"
    else:
        state = finished(work / "agent.json")
    try:
        record = json.loads(result.read_text())
    except (OSError, ValueError):
        record = {}
    record = record if isinstance(record, dict) else {}
    return {"state": state, "outcome": record.get("outcome") if record.get("outcome") in ("completed", "blocked")
            else None, "summary": str(record.get("summary", ""))[:1000], "hierarchy": str(record.get("hierarchy", "")),
            "actions": record.get("actions") if type(record.get("actions")) is int else 0}


def judge(cli, goal, tag, agent, work, token, model):
    """The judge's verdict on one agent run: ("pass" | "fail" | "rate_limited" | "error", note)."""
    work = fresh(work)
    prompt = JUDGE_PROMPT.format(goal=goal.replace("{tag}", tag), outcome=agent.get("outcome") or "none",
                                 actions=agent.get("actions", 0), summary=agent.get("summary") or "(none)",
                                 hierarchy=agent.get("hierarchy") or "(none)",
                                 delivery=", ".join(agent.get("delivery") or []) or "none")
    command = base_command(cli, prompt, model) + ["--json-schema", JUDGE_SCHEMA]
    try:
        with (work / "judge.json").open("wb") as out, (work / "judge.err").open("wb") as err:
            subprocess.run(command, cwd=fresh(work / "cwd"), env=claude_env(work, token), stdout=out, stderr=err,
                           stdin=subprocess.DEVNULL, timeout=JUDGE_SECONDS)
    except subprocess.TimeoutExpired:
        return "error", ""
    state = finished(work / "judge.json")
    if state != "ok":
        return state, ""
    try:
        data = json.loads((work / "judge.json").read_text())
        answer = data.get("structured_output")
        if not isinstance(answer, dict):
            answer = json.loads(data.get("result", ""))
    except (OSError, ValueError, AttributeError, TypeError):
        return "error", ""
    if not isinstance(answer, dict) or answer.get("verdict") not in ("pass", "fail"):
        return "error", ""
    return answer["verdict"], str(answer.get("note", ""))[:600]
