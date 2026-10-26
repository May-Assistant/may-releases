"""A throwaway unprivileged user for a pull request's own code, on a runner whose user has passwordless sudo.

The pull-request lanes run a may-app head's own code (MayCore's Swift in ios_core.py, tools/ios's Python in
swift_checks.py) in the job whose post step later holds the may-app comment token. As the runner user, that code
could use sudo, rewrite tools/may_release or the checked head, write the job's environment and output files, or read
the token mints' outputs. So it runs as USER, a system user made for the run here (reviews of may-releases #128):

- no sudo, no login shell, no group but its own; denied cron and at (DENY_FILES);
- in its own copies under ROOT of the checkout (src, its own) and of any toolchain (readable, not its own), with its
  own HOME and build folder, and its output on the private log's descriptor alone;
- unable to enter the runner's home (set to 0700 here) or runner temp, checked as that user before anything runs;
- killed after each step and on a timeout, as itself (kill -KILL -1), round after round until pgrep finds none, with a
  survivor failing the run (STOP_ROUNDS);
- removed at the end with its files in the world-writable places (LEFTOVER_DIRS), its copies and the user itself.

Every root command here is the sandbox's own setup or teardown, never the pull request's code. Nothing is printed;
errors are fixed literals (static_check.py).
"""

import os
from pathlib import Path
import subprocess
import time

USER = "may-sandbox"
ROOT = Path("/opt/may-sandbox")
ADMIN_SECONDS = 10 * 60
DENY_FILES = ("/etc/cron.deny", "/etc/at.deny")
STOP_ROUNDS = 20
# The world-writable places, each its own mount or not (-xdev keeps each scan in its own), and the sandbox; never the
# whole root, which cost minutes of a job (third re-review of #128).
LEFTOVER_DIRS = ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue", "/var/crash", "/var/lib/php/sessions",
                 "/mnt", str(ROOT))


class SandboxError(ValueError):
    pass


def admin(argv, log, data=None):
    """One root command of the sandbox's own setup or teardown, with data on its stdin when given; its status, or
    None."""
    try:
        with log.open("ab") as output:
            return subprocess.run(["sudo", "-n", *argv], stdout=output, stderr=subprocess.STDOUT, input=data,
                                  stdin=None if data is not None else subprocess.DEVNULL,
                                  timeout=ADMIN_SECONDS).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def sandboxed(argv, path=()):
    """argv as USER, with nothing of the runner's environment: the given folders (under ROOT) and the system on PATH,
    and its own HOME."""
    return ["sudo", "-n", "-u", USER, "--", "/usr/bin/env", "-i",
            "PATH=" + ":".join([*(str(p) for p in path), "/usr/bin", "/bin"]), f"HOME={ROOT / 'home'}", *argv]


def as_sandbox(argv, log):
    """A check run as USER (never the pull request's code); its status, or None."""
    try:
        with log.open("ab") as output:
            return subprocess.run(sandboxed(argv), stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  cwd=ROOT, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def setup_commands(source, copies):
    """The root commands that make the sandbox: a fresh system user, each toolchain's copy (readable, not its own) at
    ROOT/<name>, and the checkout's copy, HOME and build folder (its own)."""
    owner = f"{USER}:{USER}"
    return [["rm", "-rf", "--", str(ROOT)],
            ["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", str(ROOT / "home"),
             "--shell", "/usr/sbin/nologin", USER],
            ["install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(ROOT)],
            *(["cp", "-a", "--", str(path), str(ROOT / name)] for name, path in copies.items()),
            ["cp", "-a", "--", str(source), str(ROOT / "src")],
            ["chown", "-R", owner, "--", str(ROOT / "src")],
            ["install", "-d", "-o", USER, "-g", USER, "-m", "0700", str(ROOT / "home"), str(ROOT / "build")]]


def setup(log, source, runner_temp, copies=None):
    """The sandbox made (setup_commands), denied cron and at, and checked as USER unable to enter the runner's home
    or runner temp; refuses otherwise, before any of the pull request's code runs."""
    home = Path(os.environ.get("HOME") or Path.home())
    if admin(["chmod", "0700", "--", str(home)], log) != 0:
        raise SandboxError("The unprivileged user could not be set up; nothing ran")
    for argv in setup_commands(source, copies or {}):
        if admin(argv, log) != 0:
            raise SandboxError("The unprivileged user could not be set up; nothing ran")
    for path in DENY_FILES:
        if admin(["tee", "-a", "--", path], log, data=(USER + "\n").encode()) != 0:
            raise SandboxError("The unprivileged user could not be set up; nothing ran")
    for path in (home, runner_temp):
        if as_sandbox(["/usr/bin/test", "!", "-x", str(path)], log) != 0:
            raise SandboxError("The unprivileged user can reach the runner's files; nothing ran")


def stop_all(log, strict=True):
    """Every process of USER killed, as that user (kill -1: all it may signal, forks during a scan included), round
    after round until pgrep finds none; strict, refuses if any is left."""
    for _ in range(STOP_ROUNDS):
        admin(["-u", USER, "/usr/bin/kill", "-KILL", "-1"], log)
        if admin(["pgrep", "-u", USER], log) == 1:
            return
        time.sleep(0.25)
    if strict:
        raise SandboxError("A process of the unprivileged user could not be stopped")


def leftovers():
    """The root command that deletes USER's files in each of LEFTOVER_DIRS that exists here."""
    return ["find", *(d for d in LEFTOVER_DIRS if os.path.isdir(d)), "-xdev", "-user", USER, "-delete"]


def teardown(log):
    """The sandbox gone: its user's processes, its files in the world-writable places, its copies and the user itself.
    True when its files were all removed (the caller says so in a fixed line otherwise)."""
    stop_all(log, strict=False)
    swept = admin(leftovers(), log) == 0
    admin(["rm", "-rf", "--", str(ROOT)], log)
    admin(["userdel", USER], log)
    return swept


def step(argv, log, cwd, deadline):
    """A sandboxed command's exit status, or None when it ran past the deadline; its output goes to the private log
    alone. Its direct child is root's sudo, which the runner user may not signal: past the deadline, every USER process
    is killed instead (stop_all), and sudo then exits on its own."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    with log.open("ab") as output:
        process = subprocess.Popen(argv, cwd=cwd, env={}, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
    try:
        return process.wait(timeout=left)
    except subprocess.TimeoutExpired:
        stop_all(log, strict=False)  # a process that survives fails the run at the step's own stop_all
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return None
