"""No-signing, no-store native compile on a protected cloud runner.

Every tool writes to a private runner log. Public output is a fixed pass/fail line.
"""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import zipfile

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import StoreError, require_step_secrets

XCODEGEN_URL = "https://github.com/yonaskolb/XcodeGen/releases/download/2.46.0/xcodegen.zip"
XCODEGEN_SHA256 = "4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"


class CompileError(ValueError):
    pass


def run(argv, log, cwd=None, env=None):
    with log.open("ab") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise CompileError("Native compile failed; diagnostics remain on protected runner")


def extract_generator(archive, generator):
    generator.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as package:
        package.extractall(generator)
    binary = generator / "xcodegen/bin/xcodegen"
    if not binary.is_file():
        raise CompileError("Pinned XcodeGen binary is unavailable")
    # zipfile.extractall discards Unix executable bits even when the archive has 0755.
    binary.chmod(0o755)
    return binary


# The iOS schemes each target builds. Playground is internal, so it may build at iOS's own design-system pin
# (prepare.py --platform ios --target playground); May (production) needs the pins to match.
IOS_SCHEMES = {None: ("May", "MayPlayground"), "playground": ("MayPlayground",), "production": ("May",)}


def compile_native(platform, target=None):
    # The compile runs third-party build code (XcodeGen, SwiftPM, Xcode) and holds no secret at all.
    require_step_secrets(())
    root = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
    source, private = root / "may-source", root / "may-private"
    if not os.environ.get("RUNNER_TEMP") or not source.is_dir() or not private.is_dir():
        raise CompileError("Exact private source is not prepared")
    stats = os.statvfs(root)
    if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
        raise CompileError("Protected runner has insufficient free space")
    log = private / (platform + "-compile.log")
    if platform == "ios":
        developer = Path("/Applications/Xcode_26.3.app/Contents/Developer")
        if not developer.is_dir():
            raise CompileError("Pinned Xcode 26.3 is unavailable")
        env = os.environ.copy()
        env["DEVELOPER_DIR"] = str(developer)
        version = subprocess.run(["xcodebuild", "-version"], capture_output=True,
                                 stdin=subprocess.DEVNULL, env=env)
        if version.returncode or version.stdout.splitlines()[:1] != [b"Xcode 26.3"]:
            raise CompileError("Pinned Xcode version differs")
        archive = private / "xcodegen.zip"
        run(["curl", "-fsSL", "--retry", "2", "-o", str(archive), XCODEGEN_URL], log)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != XCODEGEN_SHA256:
            raise CompileError("Pinned XcodeGen download hash differs")
        binary = extract_generator(archive, private / "xcodegen")
        env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
        run([str(binary), "generate"], log, cwd=source / "ios", env=env)
        for scheme in IOS_SCHEMES[target]:
            run(["xcodebuild", "-project", str(source / "ios/May.xcodeproj"), "-scheme", scheme,
                 "-configuration", "Release", "-destination", "generic/platform=iOS Simulator",
                 "-derivedDataPath", str(root / "may-derived"),
                 "-clonedSourcePackagesDirPath", str(root / "may-packages"),
                 "CODE_SIGNING_ALLOWED=NO", "build"], log, env=env)
    elif platform == "android":
        env = os.environ.copy()
        env["GRADLE_USER_HOME"] = str(root / "may-gradle")
        env["JAVA_TOOL_OPTIONS"] = env.get("JAVA_TOOL_OPTIONS", "") + " -Djava.io.tmpdir=" + str(root)
        run(["./gradlew", "--no-daemon", ":core:test", ":app:testProductionDebugUnitTest",
             ":app:assembleProductionDebug", ":app:bundleProductionRelease"], log,
            cwd=source / "android", env=env)
    else:
        raise CompileError("Unknown native platform")
    print("Protected May native compile passed; no signing, store call, or public artifact")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("ios", "android"), required=True)
    parser.add_argument("--target", choices=("production", "playground"))
    args = parser.parse_args()
    try:
        compile_native(args.platform, args.target)
    except (CompileError, StoreError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        raise SystemExit("Protected May native compile failed; private logs were not published") from None
