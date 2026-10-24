"""The flow board's expected side: may-app's flow prototypes (docs/evidence/prototypes, review pages 1.2: the
review-pages skill in samuelalake/agent-skills), rendered on the Android device runner into the sealed report, beside
the recordings they are compared with. Its own step, with no secret, after the previews and before the seal; nothing
here ever fails the job, and the public log gives counts alone.

- paths: each docs/evidence/prototypes/paths/<flow>.json whose top-level "board" names a flows.json id (and whose
  "flow" is its own file name) is copied, as path-<board>.json, into the run folder; board.py checks its shape (the
  publish job runs no may-app code). Two paths naming one board are both dropped.
- render: may-app's code never runs as the runner's user, and never with a network. The prototypes folder alone is
  copied to a folder owned by a user of its own (may-render), which cannot read the runner's home or the private
  checkout; may-app's stdlib build (src/build.py) and the image's headless Chrome both run as that user, inside a
  network namespace of their own with no interface (sudo unshare --net). Chrome also proxies everything to a closed
  port, resolves no host name and sends no WebRTC outside the proxy. Each main-path step's deep link
  (pages/<flow>.html#n=<n>&platform=<p>&shot=1: the device screen alone at its logical size, autoplay off, every
  timer stopped) is kept as proto-<board>-<platform>-<n>.png only when the same page, under the same virtual-time
  budget, reports <html data-ready="1"> and the shot is a small PNG within the run's SHOT_TOTAL; each is written to a
  temporary name and renamed into place, so a step stopped part-way never leaves half a picture.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

PROTOTYPES = "docs/evidence/prototypes"
BOARD_ID = re.compile(r"[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2})?")  # board.FLOW_ID
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
MAX_PATH, MAX_PATHS = 256 * 1024, 40  # board.MAX_INPUT; paths copied at most
PATH_FILE = re.compile(r"path-(" + BOARD_ID.pattern + r")\.json")
PLATFORMS = ("android", "ios")
# Each platform's screen at its logical size (the prototypes' own), rendered at half scale.
SCREENS = {"android": (412, 915), "ios": (393, 852)}
SCALE = 0.5
# Per page: the virtual time it may run before it must be ready, and the wall time Chrome gets for it.
BUDGET_MS, CALL_SECONDS = 5000, 30
BUILD_SECONDS = 120
# The step's own timeout is 8 minutes (device-tests.yml): from the build's start, no new shot starts after
# STEP_SECONDS, so the build, the shots and those still running (WORKERS at CALL_SECONDS each) end well inside it.
STEP_SECONDS, WORKERS = 6 * 60, 4
MAX_SHOT, SHOT_TOTAL = 512 * 1024, 8 * 1024 * 1024
MAX_STEPS = 200
PNG = b"\x89PNG\r\n\x1a\n"
READY = re.compile(rb"<html\b[^>]*\bdata-ready=\"1\"")
CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
# The user may-app's code runs as, and the clean environment it gets.
RENDER_USER = "may-render"
RENDER_PATH = "/usr/local/bin:/usr/bin:/bin"


def path_files(source):
    """{board id: path JSON} for each prototype path that names its board flow, small and not a link; a board named by
    two paths is dropped. Returns (paths, dropped)."""
    folder = source / PROTOTYPES / "paths"
    found, dropped = {}, 0
    for path in sorted(folder.glob("*.json"))[:MAX_PATHS] if folder.is_dir() and not folder.is_symlink() else []:
        data = None
        if SLUG.fullmatch(path.stem) and not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_PATH:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError, RecursionError):
                data = None
        board = data.get("board") if isinstance(data, dict) else None
        if not (isinstance(board, str) and BOARD_ID.fullmatch(board) and data.get("flow") == path.stem):
            dropped += 1
        elif board in found:
            found[board] = None
            dropped += 1
        else:
            found[board] = data
    twice = [board for board, data in found.items() if data is None]
    return {board: data for board, data in found.items() if data is not None}, dropped + len(twice)


def copy_paths(source, folder):
    """Each named path into the run folder as path-<board>.json: (copied, dropped)."""
    paths, dropped = path_files(source)
    for board, data in paths.items():
        (folder / f"path-{board}.json").write_text(json.dumps(data))
    return len(paths), dropped


def main_steps(data):
    """{platform: [n, …]} for a path's main case: the steps with no edge, each n once per platform that shows it. A
    path whose steps are not the expected types raises ValueError, so only that path is dropped."""
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        raise ValueError("A path has no steps")
    shown = {platform: set() for platform in PLATFORMS}
    for step in steps[:MAX_STEPS]:
        if not (isinstance(step, dict) and type(step.get("n")) is int and 1 <= step["n"] <= 999 and
                step.get("platform") in ("both",) + PLATFORMS and isinstance(step.get("edge", ""), (str, type(None)))):
            raise ValueError("A path has a malformed step")
        if step.get("edge"):
            continue
        for platform in PLATFORMS if step["platform"] == "both" else (step["platform"],):
            shown[platform].add(step["n"])
    return {platform: sorted(numbers) for platform, numbers in shown.items()}


def chrome():
    """The runner image's own Chrome, or None."""
    return next((found for name in CHROMES if (found := shutil.which(name))), None)


def sandboxed(argv, home):
    """argv as the render user, with a clean environment, in a network namespace of its own with no interface."""
    return ["sudo", "-n", "unshare", "--net", "--", "sudo", "-n", "-u", RENDER_USER, "--", "env", "-i",
            f"HOME={home}", f"PATH={RENDER_PATH}", "LANG=C.UTF-8", *argv]


def chrome_argv(browser, profile, platform):
    width, height = SCREENS[platform]
    return [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", "--mute-audio", "--disable-background-networking", f"--user-data-dir={profile}",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=socks5://127.0.0.1:9",
            "--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            f"--virtual-time-budget={BUDGET_MS}", f"--window-size={width},{height}",
            f"--force-device-scale-factor={SCALE}"]


def shoot(browser, stage, slug, platform, n, run=subprocess.run):
    """The PNG of one step, or None: its page must report ready, and the shot must be a small PNG. Each call has its
    own profile in the stage, made beforehand and owned by the render user."""
    profile = stage / "profiles" / f"{slug}-{platform}-{n}"
    url = (stage / "pages" / f"{slug}.html").as_uri() + f"#n={n}&platform={platform}&shot=1"
    argv = chrome_argv(browser, profile, platform)
    try:
        dom = run(sandboxed(argv + ["--dump-dom", url], profile), capture_output=True, stdin=subprocess.DEVNULL,
                  timeout=CALL_SECONDS)
        if dom.returncode or not READY.search(dom.stdout[:4096]):
            return None
        shot = profile / "shot.png"
        done = run(sandboxed(argv + [f"--screenshot={shot}", url], profile), capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=CALL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode or not shot.is_file() or shot.is_symlink() or shot.stat().st_size > MAX_SHOT:
        return None
    data = shot.read_bytes()
    return data if data.startswith(PNG) else None


def stage_for(source, stage, jobs, run):
    """The prototypes folder alone, copied into stage with a profile folder per shot, then handed to the render user;
    False when that cannot be done."""
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(source / PROTOTYPES, stage, symlinks=False, ignore=shutil.ignore_patterns("pages"))
    stage.parent.chmod(0o755)  # a fresh temporary folder is the runner's alone: the render user must pass through it
    for _, slug, platform, n in jobs:
        (stage / "profiles" / f"{slug}-{platform}-{n}").mkdir(parents=True)
    # Handed to the render user; readable by all, so the runner reads the shots back (prototypes hold no secret).
    for argv in (["sudo", "-n", "useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", RENDER_USER],
                 ["sudo", "-n", "chown", "-R", f"{RENDER_USER}:", str(stage)],
                 ["sudo", "-n", "chmod", "-R", "a+rX", str(stage)]):
        try:
            done = run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
        # useradd exits 9 when the user is already there (a re-run on the same runner).
        if done.returncode and not (argv[2] == "useradd" and done.returncode == 9):
            return False
    return True


def render(source, folder, stage, scratch, browser=None, run=subprocess.run, clock=time.monotonic):
    """(made, failed): every main-path step's screen, per platform, for each path copied into folder. (None, None)
    when there is no path, or the stage, the build or Chrome is unavailable. stage is a fresh folder outside the
    runner's home (removed here at the end); scratch is the runner's own, for shots not yet whole."""
    try:
        return render_in(source, folder, stage, scratch, browser, run, clock)
    finally:
        try:
            run(["sudo", "-n", "rm", "-rf", "--", str(stage)], capture_output=True, stdin=subprocess.DEVNULL,
                timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass


def render_in(source, folder, stage, scratch, browser, run, clock):
    started = clock()
    jobs = []
    for path in sorted(folder.glob("path-*.json")):
        match = PATH_FILE.fullmatch(path.name)
        try:
            data = json.loads(path.read_text()) if match else None
            slug = data.get("flow") if isinstance(data, dict) else None
            if not (isinstance(slug, str) and SLUG.fullmatch(slug)):
                continue
            jobs += [(match.group(1), slug, platform, n) for platform, numbers in main_steps(data).items()
                     for n in numbers]
        except (OSError, ValueError, RecursionError):
            continue  # only this path is left out
    browser = browser or chrome()
    if not jobs or browser is None or not (source / PROTOTYPES / "src" / "build.py").is_file():
        return None, None
    try:
        if not stage_for(source, stage, jobs, run):
            return None, None
        built = run(sandboxed(["python3", "-I", "-S", "-B", str(stage / "src" / "build.py")], stage), cwd=stage,
                    capture_output=True, stdin=subprocess.DEVNULL,
                    timeout=max(1, min(BUILD_SECONDS, STEP_SECONDS - (clock() - started))))
    except (OSError, subprocess.SubprocessError):
        return None, None
    if built.returncode or not (stage / "pages").is_dir():
        return None, None
    total, made, failed = 0, 0, 0

    def one(job):
        return None if clock() - started > STEP_SECONDS else shoot(browser, stage, job[1], job[2], job[3], run)

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(one, job): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            board, _, platform, n = futures[future]
            data = future.result()
            if data is None or total + len(data) > SHOT_TOTAL:
                failed += 1
                continue
            total += len(data)
            target = folder / f"proto-{board}-{platform}-{n}.png"
            partial = scratch / f"{target.name}.part"
            partial.write_bytes(data)
            os.replace(partial, target)
            made += 1
    return made, failed
