"""iOS renders of May Playground in the simulator, on a may-app commit (ios-renders.yml, the render job).

This repository is public and may-app is private, so nothing derived from may-app is printed, summarised or
uploaded in the clear. The public log carries counts and fixed words only. The renders, may-app's manifest of
screens, the contact sheet built from them and the build log leave this runner only as one age-encrypted file, to
the environment's public key (the variable MAY_RENDERS_RECIPIENT). The publish job decrypts it on a fresh runner that
has run no may-app code, and pushes it to the private repository May-Assistant/may-renders (publish.py).

Commands, each a step of the render job, none holding a secret (the build and the snapshot tests run may-app's own
code, so this runner never holds a write key or a decrypt key):

- run: XcodeGen (the pinned release, hash-checked, as compile.py) and `xcodebuild test` of MayPlaygroundSnapshots on
  an iPhone 16 Pro simulator (found, or created on the newest iOS runtime), with TEST_RUNNER_MAY_SNAPSHOT_DIR (which
  xcodebuild passes to the test process as MAY_SNAPSHOT_DIR) set to a private folder. The test outcome is recorded
  privately; a failed build or test does not fail this step, so its log and the renders that were made are still
  published, and `verdict` fails the job afterwards.
- check: reads may-app's manifest (MANIFEST: rows of {id, title, spec, android: {light, dark}, ios: {light, dark}},
  paths relative to EVIDENCE), checks it and every PNG against fixed shapes, and prints rendered against expected
  (every row in light and dark), per mode, and how many are not the 1179 x 2556 frame. Without a usable manifest
  (not yet in the commit, or malformed) the renders in the snapshot folder named <id>-<mode>.png stand in for its
  rows, so the run still publishes them and the build log, and `verdict` fails the job.
- sheet: the run folder: the contact sheet (one HTML page, Android | iOS per row and mode), one stitched PNG per row
  and mode where both renders exist (stitch.swift, CoreGraphics, compiled here with the pinned Xcode), the manifest,
  and the tail of the build log.
- seal: one tar of the run folder (<may-app short sha>/), encrypted to MAY_RENDERS_RECIPIENT. Without that variable it
  seals nothing, says so, and the publish job has nothing to publish.
- verdict: fails the job if the build or the snapshot tests failed, the manifest was not usable, or any expected
  render is missing.

Interface with may-app (N's snapshot target, M's manifest): each expected render is <snapshot folder>/<id>-<mode>.png;
each row's Android render is EVIDENCE/<android[mode]>. Both are checked; nothing else in the snapshot folder is
published.
"""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import compile as native
from gate import AGE_RECIPIENT
from store import StoreError, child_env, require_step_secrets, write_private
import toolchain

RECIPIENT = "MAY_RENDERS_RECIPIENT"
SCHEME = "MayPlaygroundSnapshots"
DEVICE = "iPhone 16 Pro"
DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro"
IOS_RUNTIME = re.compile(r"com\.apple\.CoreSimulator\.SimRuntime\.iOS-(\d{1,3})-(\d{1,3})")
UDID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
XCODE = Path("/Applications/Xcode_26.3.app/Contents/Developer")
EVIDENCE = "docs/evidence/parity"
MANIFEST = EVIDENCE + "/screens.json"
FRAME = (1179, 2556)
MODES = ("light", "dark")
SCREEN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Relative to EVIDENCE; one leading "../" reaches a sibling evidence folder (docs/evidence/<other>/), never further.
EVIDENCE_PATH = re.compile(r"(?:\.\./[A-Za-z0-9_][A-Za-z0-9_-]{0,63}/)?[A-Za-z0-9_][A-Za-z0-9_./-]{0,239}\.png")
MAX_LOG = 8 * 1024 * 1024
MAX_SCREENS = 300
MAX_MANIFEST = 256 * 1024
MAX_PNG = 20 * 1024 * 1024
MAX_SIDE = 12000
PNG = b"\x89PNG\r\n\x1a\n"
# Private folders under RUNNER_TEMP: the snapshot output, the run folder that is sealed, and the sealed file.
SNAPSHOTS, OUT, SEALED = "may-renders-snapshots", "may-renders-out", "may-renders-sealed"
OUTCOME, CHECKED = "renders-outcome.json", "renders-check.json"


class RendersError(ValueError):
    pass


def root_dirs():
    raw = os.environ.get("RUNNER_TEMP")
    if not raw or not Path(raw).is_dir():
        raise RendersError("Runner temporary directory is unavailable")
    root = Path(raw).resolve()
    source, private = root / "may-source", root / "may-private"
    if not source.is_dir() or not private.is_dir():
        raise RendersError("Exact private source is not prepared")
    return root, source, private


def short(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise RendersError("Source SHA is not an exact lowercase commit SHA")
    return sha[:12]


def quiet(argv, log, cwd=None, env=None, capture=False):
    """Run a tool with its output in the private log (or its stdout captured, for JSON the caller parses, and its
    stderr in the log); nothing is printed."""
    if capture:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, stdin=subprocess.DEVNULL)
        with log.open("ab") as output:
            output.write(result.stderr)
        return result
    with log.open("ab") as output:
        return subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=output, stdin=subprocess.DEVNULL)


def build(argv, log, cwd=None, env=None):
    """quiet, failing on a non-zero exit."""
    if quiet(argv, log, cwd=cwd, env=env).returncode:
        raise RendersError("A build tool failed; diagnostics remain on the render runner")


def simulator(log, env, tool=quiet):
    """The UDID of an available iPhone 16 Pro on the newest iOS runtime, created there if there is none."""
    listed = tool(["xcrun", "simctl", "list", "devices", "available", "--json"], log, env=env, capture=True)
    try:
        devices = json.loads(listed.stdout)["devices"] if not listed.returncode else {}
    except (ValueError, KeyError, TypeError):
        devices = {}
    found = []
    for runtime, entries in (devices.items() if isinstance(devices, dict) else ()):
        match = IOS_RUNTIME.fullmatch(runtime) if isinstance(runtime, str) else None
        for entry in entries if match and isinstance(entries, list) else ():
            if isinstance(entry, dict) and entry.get("name") == DEVICE and UDID.fullmatch(str(entry.get("udid"))):
                found.append(((int(match.group(1)), int(match.group(2))), entry["udid"]))
    if found:
        return max(found)[1]
    listed = tool(["xcrun", "simctl", "list", "runtimes", "--json"], log, env=env, capture=True)
    try:
        runtimes = json.loads(listed.stdout)["runtimes"] if not listed.returncode else []
    except (ValueError, KeyError, TypeError):
        runtimes = []
    available = []
    for entry in runtimes if isinstance(runtimes, list) else ():
        match = IOS_RUNTIME.fullmatch(str(entry.get("identifier"))) if isinstance(entry, dict) else None
        if match and entry.get("isAvailable") is True:
            available.append(((int(match.group(1)), int(match.group(2))), entry["identifier"]))
    if not available:
        raise RendersError("No available iOS simulator runtime")
    created = tool(["xcrun", "simctl", "create", "May " + DEVICE, DEVICE_TYPE, max(available)[1]], log, env=env,
                   capture=True)
    udid = created.stdout.decode(errors="replace").strip() if not created.returncode else ""
    if not UDID.fullmatch(udid):
        raise RendersError("The iPhone 16 Pro simulator could not be created")
    return udid


def run_snapshots(sha):
    """XcodeGen, then the snapshot tests on the simulator; the outcome is recorded privately, never printed."""
    require_step_secrets(())
    short(sha)
    root, source, private = root_dirs()
    if native.credentials_left(private):
        raise RendersError("A read key or host file is still on disk; nothing was rendered")
    if not XCODE.is_dir():
        raise RendersError("Pinned Xcode 26.3 is unavailable")
    log = private / "ios-renders.log"
    env = os.environ.copy()
    env["DEVELOPER_DIR"] = str(XCODE)
    version = quiet(["xcodebuild", "-version"], log, env=env, capture=True)
    if version.returncode or version.stdout.splitlines()[:1] != [b"Xcode 26.3"]:
        raise RendersError("Pinned Xcode version differs")
    archive = private / "xcodegen.zip"
    build(["curl", "-fsSL", "--retry", "2", "-o", str(archive), native.XCODEGEN_URL], log)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != native.XCODEGEN_SHA256:
        raise RendersError("Pinned XcodeGen download hash differs")
    binary = native.extract_generator(archive, private / "xcodegen")
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
    snapshots = root / SNAPSHOTS
    snapshots.mkdir(mode=0o700)
    env["MAY_SNAPSHOT_DIR"] = env["TEST_RUNNER_MAY_SNAPSHOT_DIR"] = str(snapshots)
    passed = False
    try:
        build([str(binary), "generate"], log, cwd=source / "ios", env=env)
        udid = simulator(log, env)
        build(["xcodebuild", "-project", str(source / "ios/May.xcodeproj"), "-scheme", SCHEME,
               "-destination", "id=" + udid, "-derivedDataPath", str(root / "may-derived"),
               "-clonedSourcePackagesDirPath", str(root / "may-packages"), "CODE_SIGNING_ALLOWED=NO", "test"],
              log, env=env)
        passed = True
    except (RendersError, OSError, subprocess.SubprocessError):
        passed = False
    write_private(private / OUTCOME, json.dumps({"tests": "passed" if passed else "failed"}).encode())
    if passed:
        print("May iOS renders: snapshot tests passed; their output and log stay in this runner's private files")
    else:
        print("May iOS renders: snapshot tests failed; their output and log stay in this runner's private files")


def png_size(path):
    """(width, height) of a PNG no larger than MAX_PNG and MAX_SIDE a side, read from its header; else None."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_PNG:
            return None
        with path.open("rb") as stream:
            head = stream.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != PNG or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return (width, height) if 0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE else None


def evidence_paths(value, what):
    """A row's {light, dark} paths (each a relative PNG path inside EVIDENCE or a sibling evidence folder, or null);
    null for the whole platform means it has no render of that screen yet."""
    if value is None:
        return {mode: None for mode in MODES}
    if not isinstance(value, dict) or set(value) - set(MODES):
        raise RendersError("A screens manifest row's " + what + " paths are malformed")
    paths = {}
    for mode in MODES:
        path = value.get(mode)
        if path is not None and not (isinstance(path, str) and EVIDENCE_PATH.fullmatch(path) and
                                     ".." not in path.removeprefix("../").split("/")):
            raise RendersError("A screens manifest row's " + what + " paths are malformed")
        paths[mode] = path
    return paths


def manifest_rows(source):
    """may-app's manifest of screens, every field checked against a fixed shape; refuses anything else."""
    path = source / MANIFEST
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST:
            raise RendersError("The screens manifest is missing or too large")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RendersError("The screens manifest is missing or not JSON") from None
    rows = data.get("screens") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_SCREENS:
        raise RendersError("The screens manifest has no screen list")
    checked, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise RendersError("A screens manifest row is not an object")
        screen, title, spec = row.get("id"), row.get("title"), row.get("spec")
        if not (isinstance(screen, str) and SCREEN_ID.fullmatch(screen)) or screen in ids:
            raise RendersError("A screens manifest id is malformed or repeated")
        if not (isinstance(title, str) and 0 < len(title) <= 200) or \
                not (spec is None or isinstance(spec, str) and len(spec) <= 300):
            raise RendersError("A screens manifest title or spec is malformed")
        ids.add(screen)
        checked.append({"id": screen, "title": title, "spec": spec or "",
                        "android": evidence_paths(row.get("android"), "Android"),
                        "ios": evidence_paths(row.get("ios", {}), "iOS")})
    return checked


def android_render(source, row, mode):
    """The row's Android render for a mode, inside the checkout's docs/evidence/ and a valid PNG; else None."""
    if not row["android"][mode]:
        return None
    path = source / EVIDENCE / PurePosixPath(row["android"][mode])
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to((source / EVIDENCE).parent.resolve()) or png_size(resolved) is None:
        return None
    return resolved


def snapshot_rows(snapshots):
    """Rows for the renders in the snapshot folder named <id>-<mode>.png, when may-app's manifest is not usable."""
    try:
        names = sorted(path.name for path in snapshots.iterdir()) if snapshots.is_dir() else []
    except OSError:
        names = []
    pattern = re.compile(f"({SCREEN_ID.pattern})-(?:{'|'.join(MODES)})\\.png")
    ids = list(dict.fromkeys(m.group(1) for m in map(pattern.fullmatch, names) if m))[:MAX_SCREENS]
    return [{"id": screen, "title": screen, "spec": "", "android": {mode: None for mode in MODES},
             "ios": {mode: None for mode in MODES}} for screen in ids]


def check(sha):
    require_step_secrets(())
    short(sha)
    root, source, private = root_dirs()
    snapshots = root / SNAPSHOTS
    try:
        rows, manifest = manifest_rows(source), True
    except RendersError:
        rows, manifest = snapshot_rows(snapshots), False
        print("May iOS renders: may-app's screens manifest is missing or malformed; the renders in the snapshot "
              "folder are published without it, and the verdict fails")
    expected, rendered, android, wrong, per_mode = [], [], 0, 0, {mode: [0, 0] for mode in MODES}
    for row in rows:
        for mode in MODES:
            name = f"{row['id']}-{mode}.png"
            size = png_size(snapshots / name)
            entry = {"id": row["id"], "mode": mode, "ios": name, "rendered": size is not None, "android": None}
            found = android_render(source, row, mode)
            if found is not None:
                entry["android"] = str(found)
                android += 1
            expected.append(entry)
            per_mode[mode][1] += 1
            if size is not None:
                rendered.append(entry)
                per_mode[mode][0] += 1
                wrong += size != FRAME
    names = {entry["ios"] for entry in expected}
    try:
        others = sum(1 for path in snapshots.iterdir() if path.name not in names) if snapshots.is_dir() else 0
    except OSError:
        others = 0
    write_private(private / CHECKED, json.dumps({"rows": rows, "expected": expected, "manifest": manifest}).encode())
    modes = ", ".join(f"{mode} {per_mode[mode][0]} of {per_mode[mode][1]}" for mode in MODES)
    print(f"May iOS renders: {len(rendered)} of {len(expected)} expected renders made ({modes}); "
          f"{wrong} not the 1179 x 2556 frame; {others} other files in the snapshot folder, not published; "
          f"Android renders found for {android} of {len(expected)}")


def checked_record(private):
    try:
        record = json.loads((private / CHECKED).read_text())
    except (OSError, ValueError):
        raise RendersError("No renders check is recorded") from None
    if not isinstance(record, dict) or not isinstance(record.get("expected"), list):
        raise RendersError("No renders check is recorded")
    return record


def page(sha, ds_sha, rows, expected, stitched, manifest=True):
    """The contact sheet's HTML: every value from may-app escaped, every image a relative path in the run folder."""
    by_row = {}
    for entry in expected:
        by_row.setdefault(entry["id"], []).append(entry)
    parts = ["<!doctype html>", '<meta charset="utf-8">', "<title>May parity renders</title>",
             "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}"
             "td,th{border:1px solid #ccc;padding:8px;vertical-align:top}img{max-width:360px}</style>",
             f"<h1>May parity renders, may-app {html.escape(sha)}</h1>",
             f"<p>Design system: {html.escape(ds_sha) if ds_sha else 'may-app pin'}. "
             f"Device: {DEVICE}, light and dark.</p>",
             '<p><a href="build.log">Build and test log</a> (its last 8 MB).</p>',
             "" if manifest else "<p><b>may-app's screens manifest was missing or malformed in this commit</b>: "
                                 "these are the renders the snapshot tests made, with no Android column.</p>",
             "<table><tr><th>Screen</th><th>Mode</th><th>Android</th><th>iOS</th></tr>"]
    for row in rows:
        for entry in by_row.get(row["id"], []):
            key = f"{entry['id']}-{entry['mode']}"
            android = f'<img src="android/{key}.png" alt="Android">' if entry["android"] else "missing"
            ios = f'<img src="ios/{key}.png" alt="iOS">' if entry["rendered"] else "missing"
            sheet = f'<br><a href="sheet/{key}.png">side by side</a>' if key in stitched else ""
            parts.append(f"<tr><td>{html.escape(row['title'])}<br><small>{html.escape(row['id'])} "
                         f"{html.escape(row['spec'])}</small>{sheet}</td>"
                         f"<td>{html.escape(entry['mode'])}</td><td>{android}</td><td>{ios}</td></tr>")
    parts.append("</table>")
    return "\n".join(parts) + "\n"


def stitcher(private, log, tool=build):
    """stitch.swift, compiled with the pinned Xcode's swiftc into the private folder; the binary's path."""
    binary = private / "stitch"
    env = os.environ.copy()
    env["DEVELOPER_DIR"] = str(XCODE)
    tool(["xcrun", "swiftc", "-O", "-o", str(binary), str(Path(__file__).resolve().parent / "stitch.swift")], log,
         env=env)
    return binary


def sheet(sha, ds_sha=None, build_stitcher=stitcher, tool=quiet):
    require_step_secrets(())
    folder = short(sha)
    if ds_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", ds_sha):
        raise RendersError("Design-system SHA is not an exact lowercase commit SHA")
    root, source, private = root_dirs()
    record = checked_record(private)
    out = root / OUT / folder
    out.mkdir(parents=True, mode=0o700)
    for name in ("ios", "android", "sheet"):
        (out / name).mkdir(mode=0o700)
    log = private / "ios-renders-sheet.log"
    snapshots = root / SNAPSHOTS
    try:
        binary = build_stitcher(private, log)
    except (RendersError, OSError, subprocess.SubprocessError):
        binary = None
    stitched = set()
    for entry in record["expected"]:
        key = f"{entry['id']}-{entry['mode']}"
        ios, android = out / "ios" / (key + ".png"), out / "android" / (key + ".png")
        # Each file is checked again where it lies now (the tests ran may-app code), and copied as a plain file.
        if entry["rendered"] and png_size(snapshots / entry["ios"]) is not None:
            shutil.copyfile(snapshots / entry["ios"], ios, follow_symlinks=False)
        else:
            entry["rendered"] = False
        if entry["android"] and png_size(Path(entry["android"])) is not None:
            shutil.copyfile(entry["android"], android, follow_symlinks=False)
        else:
            entry["android"] = None
        if binary is not None and entry["rendered"] and entry["android"]:
            result = tool([str(binary), str(android), str(ios), str(out / "sheet" / (key + ".png"))], log)
            if not result.returncode and png_size(out / "sheet" / (key + ".png")) is not None:
                stitched.add(key)
            else:
                (out / "sheet" / (key + ".png")).unlink(missing_ok=True)
    manifest = record.get("manifest") is not False
    if manifest:
        (out / "screens.json").write_text(json.dumps(record["rows"], indent=2) + "\n")
    # The build and test log's tail, for the may-app lane (the first runs of a new target are read there).
    build_log = private / "ios-renders.log"
    try:
        size = build_log.stat().st_size if build_log.is_file() and not build_log.is_symlink() else 0
        with build_log.open("rb") as stream:
            stream.seek(max(0, size - MAX_LOG))
            (out / "build.log").write_bytes(stream.read(MAX_LOG))
    except OSError:
        (out / "build.log").write_bytes(b"")
    (out / "index.html").write_text(page(sha, ds_sha, record["rows"], record["expected"], stitched, manifest))
    rows = len(record["expected"])
    if binary is None:
        print(f"May iOS renders: contact sheet of {rows} rows; the stitcher did not build, so none were stitched")
    else:
        print(f"May iOS renders: contact sheet of {rows} rows, {len(stitched)} stitched side by side")


def seal(sha):
    """One tar of the run folder, encrypted to MAY_RENDERS_RECIPIENT; nothing in the clear leaves the runner."""
    require_step_secrets(())
    folder = short(sha)
    root, _, _ = root_dirs()
    out = root / OUT
    if not (out / folder / "index.html").is_file():
        raise RendersError("The contact sheet is not built; nothing was sealed")
    recipient = os.environ.get(RECIPIENT, "").strip()
    if not recipient:
        print("May iOS renders: MAY_RENDERS_RECIPIENT is not set in may-ios-renders; nothing was sealed, so nothing "
              "will be published")
        return
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise RendersError("MAY_RENDERS_RECIPIENT is not an age public key")
    sealed = root / SEALED
    sealed.mkdir(mode=0o700)
    plain = sealed / "renders.tar"
    try:
        with tarfile.open(plain, "w") as archive:
            for path in sorted(out.rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise RendersError("The run folder holds something other than files and folders")
                archive.add(path, arcname=path.relative_to(out).as_posix(), recursive=False)
        result = subprocess.run([toolchain.tool_path("age", "age"), "-r", recipient, "-o",
                                 str(sealed / "renders.age"), str(plain)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=child_env())
        if result.returncode:
            raise RendersError("The renders could not be encrypted")
    finally:
        plain.unlink(missing_ok=True)
    print("May iOS renders: run folder encrypted to the renders public key for the publish job")


def verdict(sha):
    require_step_secrets(())
    short(sha)
    _, _, private = root_dirs()
    try:
        outcome = json.loads((private / OUTCOME).read_text())
    except (OSError, ValueError):
        raise RendersError("No snapshot outcome is recorded") from None
    record = checked_record(private)
    missing = sum(1 for entry in record["expected"] if not entry.get("rendered"))
    if not isinstance(outcome, dict) or outcome.get("tests") != "passed":
        raise RendersError("The snapshot tests failed")
    if record.get("manifest") is False:
        raise RendersError("may-app's screens manifest is missing or malformed")
    if missing:
        raise RendersError("Some expected renders are missing")
    print("May iOS renders: snapshot tests passed and every expected render was made")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "check", "sheet", "seal", "verdict"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ds-sha")
    args = parser.parse_args()
    try:
        if args.command == "sheet":
            sheet(args.source_sha, args.ds_sha or None)
        else:
            {"run": run_snapshots, "check": check, "seal": seal, "verdict": verdict}[args.command](args.source_sha)
    except (RendersError, StoreError) as error:
        raise SystemExit("May iOS renders stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("May iOS renders failed; private logs and renders were not published") from None
