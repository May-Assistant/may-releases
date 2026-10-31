"""A throwaway unprivileged macOS user for a may-app pull request's own build code (native-compile-pr.yml's native
job, compile.py --sandbox), on a macos-15 runner whose user has passwordless sudo.

sandbox.py does this on Linux for ios_core.py and swift_checks.py. On macOS the pull request's XcodeGen spec, Swift
packages, macros, plugins and build phases ran as the runner user: able to use sudo, read the runner's own files
(its _diag logs and the job's ACTIONS_RUNTIME_TOKEN among them), rewrite tools/may_release or the checked head, or
reach the sealed record's upload (coordinator, 31 Oct, #65). So they run as USER, made for the job here:

- a hidden local user and group of their own (dscl, UID and GID FIXED_ID, refused when either is taken), shell
  /usr/bin/false, no admin or any other group; denied cron and at (DENY_FILES);
- in its own copy of the prepared checkout under ROOT (src, its own; its project.yml pointed at the copy's own design
  system), the pinned XcodeGen (readable, not its own), its own HOME, TMPDIR, derived data and packages; its output
  on the private log's descriptor alone;
- unable to enter the runner's home (set to 0700 here) or runner temp, checked as that user before anything runs;
- killed after each stage and on a timeout, as itself (kill -KILL -1), round after round until pgrep finds none, a
  survivor failing the job (STOP_ROUNDS);
- removed at the end with its files in the world-writable places and its per-user caches (LEFTOVER_DIRS), its copies,
  the user and the group.

It fails closed (coordinator, 31 Oct): any setup or check that does not succeed raises before any of the pull
request's code runs, and the job ends red; nothing falls back to the runner user. Every root command here is the
sandbox's own setup or teardown, never the pull request's code. Nothing is printed; errors are fixed literals.
"""

import os
from pathlib import Path
import re
import subprocess
import time

USER = "may-sandbox"
FIXED_ID = 497
ROOT = Path("/private/var/may-sandbox")
ADMIN_SECONDS = 10 * 60
DENY_FILES = ("/usr/lib/cron/cron.deny", "/usr/lib/cron/at.deny")
STOP_ROUNDS = 20
# The world-writable places and the per-user cache roots macOS makes on first use (/private/var/folders), and the
# sandbox itself; never the whole disk.
LEFTOVER_DIRS = ("/private/tmp", "/private/var/tmp", "/private/var/folders", str(ROOT))
# The folders the sandbox user owns under ROOT, besides its copy of the checkout.
OWN_DIRS = ("home", "tmp", "derived", "packages")


class SandboxError(ValueError):
    pass


def admin(argv, log, data=None):
    """One root command of the sandbox's own setup or teardown, with data on its stdin when given; its status, or
    None."""
    return admin_said(argv, log, data)[0]


# Run 41170609342: the runner's home could not be closed, and nothing said why. What a failed setup command wrote is
# sorted into ERROR_CLASSES' fixed words (first match, else "other"; "did not start" when it never ran) for the
# caller's fixed line; the words themselves go to the private log alone.
ERROR_CLASSES = (("no such file", re.compile(rb"No such file", re.IGNORECASE)),
                 ("not permitted", re.compile(rb"Operation not permitted", re.IGNORECASE)),
                 ("permission denied", re.compile(rb"Permission denied", re.IGNORECASE)),
                 ("sudo refused", re.compile(rb"password is required|sudo: ", re.IGNORECASE)),
                 ("usage", re.compile(rb"usage:|illegal option|invalid", re.IGNORECASE)),
                 ("exists", re.compile(rb"already exists|eDSRecordAlreadyExists", re.IGNORECASE)),
                 ("read-only", re.compile(rb"Read-only file system", re.IGNORECASE)))


ERROR_WORDS = (*(name for name, _ in ERROR_CLASSES), "other", "did not start")
TEARDOWN_NAMES = (*LEFTOVER_DIRS, "the sandbox", "the user", "the group")


def error_class(said):
    return next((name for name, pattern in ERROR_CLASSES if pattern.search(said or b"")), "other")


def admin_said(argv, log, data=None):
    """admin's (status, class): what the command wrote goes to the private log, and is sorted into ERROR_CLASSES'
    words when it failed (None when it succeeded)."""
    try:
        with log.open("ab") as output:
            start = output.tell()
            status = subprocess.run(["sudo", "-n", *argv], stdout=output, stderr=subprocess.STDOUT, input=data,
                                    stdin=None if data is not None else subprocess.DEVNULL,
                                    timeout=ADMIN_SECONDS).returncode
    except (OSError, subprocess.SubprocessError):
        return None, "did not start"
    if not status:
        return status, None
    try:
        with log.open("rb") as written:
            written.seek(start)
            said = written.read(64 * 1024)
    except OSError:
        said = b""
    return status, error_class(said)


def sandboxed(argv, path=(), env=()):
    """argv as USER, with nothing of the runner's environment: the given folders (under ROOT) and the system on PATH,
    its own HOME and TMPDIR, and the given KEY=value pairs."""
    return ["sudo", "-n", "-u", USER, "--", "/usr/bin/env", "-i",
            "PATH=" + ":".join([*(str(p) for p in path), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            f"HOME={ROOT / 'home'}", f"TMPDIR={ROOT / 'tmp'}/", *env, *argv]


def as_sandbox(argv, log):
    """A check run as USER (never the pull request's code); its status, or None."""
    try:
        with log.open("ab") as output:
            return subprocess.run(sandboxed(argv), stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  cwd="/", timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def ids_taken(run=subprocess.run):
    """Whether USER, or FIXED_ID as a user's or a group's id, already exists on this runner (read without root)."""
    try:
        for kind, key in (("/Users", "UniqueID"), ("/Groups", "PrimaryGroupID")):
            listed = run(["dscl", ".", "-list", kind, key], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
            if listed.returncode:
                return True
            for line in listed.stdout.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) == 2 and (parts[0] == USER or parts[1] == str(FIXED_ID)):
                    return True
    except (OSError, subprocess.SubprocessError):
        return True
    return False


def setup_commands(copies):
    """The root commands that make the sandbox: a hidden user and group of its own, ROOT (root's), each copy at
    ROOT/<name> (src its own, the rest readable, not its own) and its own folders."""
    user, group, ident = f"/Users/{USER}", f"/Groups/{USER}", str(FIXED_ID)
    commands = [["rm", "-rf", "--", str(ROOT)],
                ["dscl", ".", "-create", group],
                ["dscl", ".", "-create", group, "PrimaryGroupID", ident],
                ["dscl", ".", "-create", user],
                ["dscl", ".", "-create", user, "UniqueID", ident],
                ["dscl", ".", "-create", user, "PrimaryGroupID", ident],
                ["dscl", ".", "-create", user, "UserShell", "/usr/bin/false"],
                ["dscl", ".", "-create", user, "NFSHomeDirectory", str(ROOT / "home")],
                ["dscl", ".", "-create", user, "IsHidden", "1"],
                ["install", "-d", "-o", "root", "-g", "wheel", "-m", "0755", str(ROOT)]]
    for name, path in copies.items():
        commands.append(["cp", "-R", "-p", "--", str(path), str(ROOT / name)])
        # macOS's getopt stops at the first operand, so "--" goes before the owner or mode, never after it (run
        # 41170609342: "chmod 0700 -- home" took "--" for a file).
        commands.append(["chown", "-R", "--", "root:wheel" if name != "src" else f"{USER}:{USER}", str(ROOT / name)])
        if name != "src":
            # A copy of a private folder keeps its 0700: readable by all, written by none but root.
            commands.append(["chmod", "-R", "--", "u=rwX,go=rX", str(ROOT / name)])
    commands.append(["install", "-d", "-o", USER, "-g", USER, "-m", "0700", *(str(ROOT / d) for d in OWN_DIRS)])
    return commands


def setup(log, copies, home, runner_temp, project=None, ids=ids_taken, say=lambda stage, status, cause=None: None):
    """The sandbox made (setup_commands), denied cron and at, and checked as USER unable to enter the runner's home
    or runner temp; refuses otherwise, before any of the pull request's code runs. project, when given, is the
    copy's own project.yml (prepared by the caller), installed over the copied one as USER's. say(stage, status) hears
    each command's exit status as it ends, and a failed one's ERROR_CLASSES word (the caller prints fixed lines)."""
    def check(stage, result, refusal):
        status, cause = result if isinstance(result, tuple) else (result, None if result == 0 else "other")
        say(stage, status, cause)
        if status != 0:
            raise SandboxError(refusal + "; nothing ran")

    if "src" not in copies:
        raise SandboxError("The unprivileged user has no checkout to build; nothing ran")
    check("ids", 1 if ids() else 0, "The unprivileged user's name or id is already taken")
    check("home", admin_said(["chmod", "--", "0700", str(home)], log),
          "The runner's home could not be closed to the unprivileged user")
    for index, argv in enumerate(setup_commands(copies), 1):
        check(f"setup {index} {argv[0]}", admin_said(argv, log), "The unprivileged user could not be set up")
    if project is not None:
        check("project", admin_said(["install", "-o", USER, "-g", USER, "-m", "0644", str(project),
                                str(ROOT / "src/ios/project.yml")], log),
              "The unprivileged user's project could not be pointed at its own copy")
    for path in DENY_FILES:
        check("deny", admin_said(["tee", "-a", "--", path], log, data=(USER + "\n").encode()),
              "The unprivileged user could not be denied cron and at")
    for path in (home, runner_temp):
        check("unreachable", as_sandbox(["/bin/test", "!", "-x", str(path)], log),
              "The unprivileged user can reach the runner's files")
    check("writable", as_sandbox(["/bin/test", "-w", str(ROOT / "src")], log),
          "The unprivileged user cannot write its own copy")


def stop_all(log, strict=True):
    """Every process of USER killed, as that user (kill -1: all it may signal, forks during a scan included), round
    after round until pgrep finds none; strict, refuses if any is left."""
    for _ in range(STOP_ROUNDS):
        admin(["-u", USER, "/bin/kill", "-KILL", "-1"], log)
        if admin(["pgrep", "-u", USER], log) == 1:
            return
        time.sleep(0.25)
    if strict:
        raise SandboxError("A process of the unprivileged user could not be stopped")


def leftovers(folder):
    """The root command that deletes USER's files in folder, one of LEFTOVER_DIRS."""
    return ["find", folder, "-xdev", "-user", USER, "-delete"]


def known(kind, run=subprocess.run):
    """Whether USER exists as a user (kind "/Users") or a group ("/Groups") here, read without root."""
    try:
        return run(["dscl", ".", "-read", f"{kind}/{USER}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def teardown(log, exists=known):
    """The sandbox gone: its user's processes, its files in the world-writable places and caches, its copies, the
    user and the group. What could not be removed, as fixed names (a LEFTOVER_DIRS folder, "the sandbox", "the user",
    "the group"); empty when all of it went. Run 41170609342: a setup refused before the user was made has no user, so
    its files, processes and records are not looked for, and nothing is said to be left."""
    user = exists("/Users")
    if user:
        stop_all(log, strict=False)
    left = [folder for folder in LEFTOVER_DIRS
            if user and os.path.isdir(folder) and admin(leftovers(folder), log) != 0]
    if admin(["rm", "-rf", "--", str(ROOT)], log) != 0:
        left.append("the sandbox")
    if user and admin(["dscl", ".", "-delete", f"/Users/{USER}"], log) != 0:
        left.append("the user")
    if exists("/Groups") and admin(["dscl", ".", "-delete", f"/Groups/{USER}"], log) != 0:
        left.append("the group")
    return left


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
        stop_all(log, strict=False)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
        return None
