"""Android renders of May Playground on a may-app commit (android-renders.yml, the render job).

may-app's Android unit tests write their renders, PNGs, when the JVM property may.renders.dir names a folder (may-app's
build hands that property to the test JVM). This job runs `./gradlew :app:testPlaygroundDebugUnitTest
--tests '*Renders*' -Dmay.renders.dir=<private folder>` with the pinned JDK 21 (toolchain.py), at the fetched commit
(prepare.py: may-app's main, or the head of an open pull request). The test filter is fixed here, never a dispatch
input: an input is public, and a test's name is may-app's.

This repository is public and may-app is private, so nothing derived from may-app is printed, summarised or uploaded
in the clear. The public log has fixed words and counts alone. The renders, a page listing them and the tail of the
build log leave this runner only as one age-encrypted file, to the environment's public key (MAY_RENDERS_RECIPIENT).
The publish job decrypts it on a fresh runner that has run no may-app code and pushes it to the private repository
May-Assistant/may-renders as android-renders/<may-app short sha>/ (publish.py android-open, android-push).

Commands, each a step of the render job, none holding a secret:
- run: the Gradle test task above. Its outcome is recorded privately and written as the step's output `tests`
  (passed, failed or timeout); a failed run does not fail this step, so the renders that were made are still published.
  Each PNG the tests wrote (a real file, never a link, at most MAX_PNG bytes, a PNG) goes to the run folder's android/,
  its path below the renders folder joined with "__" into one name of FILE's shape; anything else is left out.
- seal: one tar of the run folder (<may-app short sha>/), encrypted to MAY_RENDERS_RECIPIENT; without it, nothing.
- verdict: fails the job unless the tests passed and made at least one render.
"""

import argparse
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gate import AGE_RECIPIENT
from store import StoreError, child_env, require_step_secrets
import toolchain

RECIPIENT = "MAY_RENDERS_RECIPIENT"
TASK, FILTER = ":app:testPlaygroundDebugUnitTest", "*Renders*"
SNAPSHOTS, OUT, SEALED = "may-android-renders-snapshots", "may-android-renders-out", "may-renders-sealed"
OUTCOME, LOG = "android-renders-result.json", "android-renders.log"
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,140}\.png")
PNG = b"\x89PNG\r\n\x1a\n"
MAX_PNG, MAX_PNGS, MAX_LOG_TAIL = 20 * 1024 * 1024, 500, 2 * 1024 * 1024
GRADLE_SECONDS = 50 * 60
WORDS = ("passed", "failed", "timeout")


class AndroidRendersError(ValueError):
    pass


def root_dirs():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise AndroidRendersError("Runner temporary directory is unavailable")
    root = Path(raw).resolve()
    source, private = root / "may-source", root / "may-private"
    if not (source / "android").is_dir() or not private.is_dir():
        raise AndroidRendersError("Exact private source is not prepared")
    return root, source, private


def short(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise AndroidRendersError("Source SHA is not an exact lowercase commit SHA")
    return sha[:12]


def published_name(relative):
    """One name of FILE's shape for a render's path below the renders folder, or None when it has no such name."""
    name = "__".join(relative.parts)
    return name if FILE.fullmatch(name) else None


def collect(snapshots, target):
    """The tests' PNGs, copied into target under published_name; returns how many."""
    target.mkdir(parents=True, mode=0o700)
    count, seen = 0, set()
    for path in sorted(snapshots.rglob("*.png")) if snapshots.is_dir() else []:
        if count >= MAX_PNGS:
            break
        name = published_name(path.relative_to(snapshots))
        try:
            if name is None or name in seen or path.is_symlink() or not path.is_file() or \
                    path.stat().st_size > MAX_PNG:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if not data.startswith(PNG):
            continue
        (target / name).write_bytes(data)
        seen.add(name)
        count += 1
    return sorted(seen)


def pages(folder_path, sha, names, word):
    """index.html and README.md listing the renders (private: may-renders alone)."""
    title = f"May Android renders of {sha}: tests {word}, {len(names)} images"
    (folder_path / "README.md").write_text(f"# {title}\n\n" + "".join(
        f"### {name}\n\n![{name}](android/{name})\n\n" for name in names))
    (folder_path / "index.html").write_text(
        "<!doctype html>\n<meta charset=\"utf-8\">\n<title>May Android renders</title>\n"
        f"<h1>{html.escape(title)}</h1>\n" + "".join(
            f"<figure><img src=\"android/{html.escape(name)}\" width=\"360\"><figcaption>{html.escape(name)}"
            "</figcaption></figure>\n" for name in names))


def write_output(word):
    path = os.environ.get("GITHUB_OUTPUT")
    if path and word in WORDS:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"tests={word}\n")


def run_tests(sha, run=subprocess.run):
    require_step_secrets(())
    folder = short(sha)
    root, source, private = root_dirs()
    snapshots, out = root / SNAPSHOTS, root / OUT / folder
    snapshots.mkdir(mode=0o700)
    try:
        java = Path(toolchain.tool_path("jdk", "bin/java"))
    except StoreError:
        raise AndroidRendersError("The pinned JDK is not installed; nothing ran") from None
    env = child_env(JAVA_HOME=str(java.parents[1]), GRADLE_USER_HOME=str(root / "may-gradle"),
                    PATH=str(java.parent) + os.pathsep + os.environ.get("PATH", ""))
    log = private / LOG
    try:
        with log.open("ab") as output:
            status = run(["./gradlew", "--no-daemon", TASK, "--tests", FILTER, "-Dmay.renders.dir=" + str(snapshots)],
                            cwd=source / "android", env=env, stdout=output, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, timeout=GRADLE_SECONDS).returncode
    except subprocess.TimeoutExpired:
        status = None
    word = "timeout" if status is None else "failed" if status else "passed"
    names = collect(snapshots, out / "android")
    pages(out, sha, names, word)
    data = log.read_bytes() if log.is_file() and not log.is_symlink() else b""
    (out / "build.log").write_bytes(data[-MAX_LOG_TAIL:])
    (private / OUTCOME).write_text(json.dumps({"tests": word, "images": len(names)}))
    write_output(word)
    count = len(names)
    print(f"May Android renders: tests {word}; {int(count)} images")


def seal(sha):
    """One tar of the run folder, encrypted to MAY_RENDERS_RECIPIENT; nothing in the clear leaves the runner."""
    require_step_secrets(())
    folder = short(sha)
    root, _, _ = root_dirs()
    out = root / OUT
    if not (out / folder / "index.html").is_file():
        raise AndroidRendersError("The run folder is not built; nothing was sealed")
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("May Android renders: MAY_RENDERS_RECIPIENT is not set; nothing was sealed, so nothing will be published")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise AndroidRendersError("MAY_RENDERS_RECIPIENT is not an age public key")
    sealed = root / SEALED
    sealed.mkdir(mode=0o700)
    plain = sealed / "renders.tar"
    try:
        with tarfile.open(plain, "w") as archive:
            for path in sorted(out.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise AndroidRendersError("The run folder holds something other than files and folders")
                archive.add(path, arcname=path.relative_to(out).as_posix(), recursive=False)
        result = subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o",
                                 str(sealed / "renders.age"), str(plain)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=child_env())
        if result.returncode:
            raise AndroidRendersError("The renders could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print("May Android renders: run folder encrypted to the renders public key for the publish job")


def verdict(sha):
    require_step_secrets(())
    short(sha)
    _, _, private = root_dirs()
    try:
        outcome = json.loads((private / OUTCOME).read_text())
    except (OSError, ValueError):
        raise AndroidRendersError("No render outcome is recorded") from None
    if not isinstance(outcome, dict) or outcome.get("tests") != "passed":
        raise AndroidRendersError("The render tests did not pass")
    if not isinstance(outcome.get("images"), int) or outcome["images"] < 1:
        raise AndroidRendersError("The render tests made no render")
    print("May Android renders: the render tests passed and made renders")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "seal", "verdict"))
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        {"run": run_tests, "seal": seal, "verdict": verdict}[args.command](args.source_sha)
    except (AndroidRendersError, StoreError) as error:
        raise SystemExit("May Android renders stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May Android renders failed; private logs and renders were not published") from None
