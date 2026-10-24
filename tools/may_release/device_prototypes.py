"""The flow board's expected side: may-app's flow prototypes (docs/evidence/prototypes, review pages 1.1), rendered on
the Android device runner into the sealed report, beside the recordings they are compared with.

- paths: each docs/evidence/prototypes/paths/<flow>.json whose top-level "board" names a flows.json id is copied, as
  path-<board>.json, into the run folder (board.py checks its shape; the publish job runs no may-app code).
- render: its own step, with no secret, after the previews and before the seal. may-app's stdlib build
  (src/build.py) writes the pages from the checked-out commit (main history, by admission); the runner image's
  headless Chrome then opens each main-path step's deep link (pages/<flow>.html#n=<n>&platform=<p>&shot=1: the
  device screen alone at its logical size, autoplay off, every timer stopped), with no network (every host name
  resolves to nothing) and file:// alone. A shot is kept only when the same page, under the same virtual-time
  budget, reports <html data-ready="1">; it is written as proto-<board>-<platform>-<n>.png. A page that is not ready,
  a shot that is not a small PNG, or one past the run's SHOT_TOTAL is left out: that step shows its caption alone.

Nothing here ever fails the job, and the public log gives counts alone.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

PROTOTYPES = "docs/evidence/prototypes"
BUILD = PROTOTYPES + "/src/build.py"
BOARD_ID = re.compile(r"[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2})?")  # board.FLOW_ID
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
MAX_PATH = 256 * 1024  # board.MAX_INPUT
PATH_FILE = re.compile(r"path-(" + BOARD_ID.pattern + r")\.json")
SHOT_FILE = re.compile(r"proto-(" + BOARD_ID.pattern + r")-(android|ios)-([1-9][0-9]{0,2})\.png")
PLATFORMS = ("android", "ios")
# Each platform's screen at its logical size (the prototypes' own), rendered at half scale.
SCREENS = {"android": (412, 915), "ios": (393, 852)}
SCALE = 0.5
# Per page: the virtual time it may run before it must be ready, and the wall time Chrome gets for it.
BUDGET_MS, CALL_SECONDS = 5000, 30
BUILD_SECONDS = 120
# The step's own timeout is 8 minutes (device-tests.yml): no new shot starts after STEP_SECONDS.
STEP_SECONDS, WORKERS = 6 * 60, 4
MAX_SHOT, SHOT_TOTAL = 512 * 1024, 8 * 1024 * 1024
MAX_STEPS = 200
PNG = b"\x89PNG\r\n\x1a\n"
READY = re.compile(rb"<html\b[^>]*\bdata-ready=\"1\"")
CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def path_files(source):
    """(board id, path JSON text) for each prototype path that names its board flow, small and not a link."""
    folder = source / PROTOTYPES / "paths"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() and not folder.is_symlink() else []:
        if not SLUG.fullmatch(path.stem) or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PATH:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        board = data.get("board") if isinstance(data, dict) else None
        if isinstance(board, str) and BOARD_ID.fullmatch(board) and data.get("flow") == path.stem:
            yield board, data


def copy_paths(source, folder):
    """Each named path into the run folder as path-<board>.json; the count copied."""
    copied = 0
    for board, data in path_files(source):
        (folder / f"path-{board}.json").write_text(json.dumps(data))
        copied += 1
    return copied


def main_steps(data):
    """{platform: [n, …]} for a path's main case: the steps with no edge, each n once per platform that shows it."""
    steps = data.get("steps") if isinstance(data, dict) else None
    shown = {platform: set() for platform in PLATFORMS}
    for step in steps[:MAX_STEPS] if isinstance(steps, list) else []:
        if not isinstance(step, dict) or step.get("edge") or type(step.get("n")) is not int or not 1 <= step["n"] <= 999:
            continue
        for platform in PLATFORMS if step.get("platform") == "both" else (step.get("platform"),):
            if platform in shown:
                shown[platform].add(step["n"])
    return {platform: sorted(numbers) for platform, numbers in shown.items()}


def chrome():
    """The runner image's own Chrome, or None."""
    return next((found for name in CHROMES if (found := shutil.which(name))), None)


def chrome_argv(browser, profile, platform):
    width, height = SCREENS[platform]
    return [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", "--mute-audio", f"--user-data-dir={profile}",
            '--host-resolver-rules=MAP * ~NOTFOUND', f"--virtual-time-budget={BUDGET_MS}",
            f"--window-size={width},{height}", f"--force-device-scale-factor={SCALE}"]


def shoot(browser, pages, slug, platform, n, work, run=subprocess.run):
    """The PNG of one step, or None: its page must report ready, and the shot must be a small PNG."""
    url = (pages / f"{slug}.html").resolve().as_uri() + f"#n={n}&platform={platform}&shot=1"
    with tempfile.TemporaryDirectory(dir=work) as profile:
        argv = chrome_argv(browser, profile, platform)
        try:
            dom = run(argv + ["--dump-dom", url], capture_output=True, stdin=subprocess.DEVNULL,
                      timeout=CALL_SECONDS, env=chrome_env(profile))
            if dom.returncode or not READY.search(dom.stdout[:4096]):
                return None
            shot = Path(profile) / "shot.png"
            done = run(argv + [f"--screenshot={shot}", url], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=CALL_SECONDS, env=chrome_env(profile))
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode or not shot.is_file() or shot.is_symlink() or shot.stat().st_size > MAX_SHOT:
            return None
        data = shot.read_bytes()
    return data if data.startswith(PNG) else None


def chrome_env(profile):
    """Chrome's environment: a path, a home of its own and nothing of the runner's."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": profile, "LANG": "C.UTF-8"}


def render(source, folder, work, log, browser=None, run=subprocess.run, clock=time.monotonic):
    """(made, failed): every main-path step's screen, per platform, for each path copied into folder. (None, None)
    when there is no path, or the build or Chrome is unavailable."""
    paths = [(PATH_FILE.fullmatch(p.name).group(1), p) for p in sorted(folder.glob("path-*.json"))
             if PATH_FILE.fullmatch(p.name)]
    browser = browser or chrome()
    build = source / BUILD
    if not paths or browser is None or not build.is_file() or build.is_symlink():
        return None, None
    with log.open("ab") as output:
        try:
            built = run(["python3", "-I", "-S", "-B", str(build)], cwd=source, stdout=output, stderr=output,
                        stdin=subprocess.DEVNULL, timeout=BUILD_SECONDS, env=chrome_env(str(work)))
        except (OSError, subprocess.SubprocessError):
            return None, None
    pages = source / PROTOTYPES / "pages"
    if built.returncode or not pages.is_dir() or pages.is_symlink():
        return None, None
    jobs = []
    for board, path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            continue
        slug = data.get("flow") if isinstance(data, dict) else None
        if not (isinstance(slug, str) and SLUG.fullmatch(slug)):
            continue
        jobs += [(board, slug, platform, n) for platform, numbers in main_steps(data).items() for n in numbers]
    started, total, made, failed = clock(), 0, 0, 0
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        futures = {}
        for job in jobs:
            futures[pool.submit(lambda j: None if clock() - started > STEP_SECONDS else
                                shoot(browser, pages, j[1], j[2], j[3], work, run), job)] = job
        for future in concurrent.futures.as_completed(futures):
            board, _, platform, n = futures[future]
            data = future.result()
            if data is None or total + len(data) > SHOT_TOTAL:
                failed += 1
                continue
            total += len(data)
            (folder / f"proto-{board}-{platform}-{n}.png").write_bytes(data)
            made += 1
    return made, failed
