"""Pinned, checksum-verified toolchain for May release jobs.

actions/setup-java does not verify the JDK archive it downloads, so jobs
install their JDK and age from here instead. Each archive is pinned by URL and
SHA-256 and refused on any mismatch. Provenance of each pin, checked when it
was recorded on 2026-10-13:

- Temurin JDK 21.0.12.1+1 (linux x64): SHA-256 equals Adoptium's published
  checksum file (OpenJDK21U-jdk_x64_linux_hotspot_21.0.12.1_1.tar.gz.sha256.txt)
  and the Adoptium API's `checksum` field.
- age v1.3.1 (linux amd64, darwin arm64): each archive passed
  `sigsum-verify -k age-sigsum-key.pub -P sigsum-generic-2025-1` against its
  published .proof, using the key in age's SIGSUM.md.
- ffmpeg 7:6.1.1-3ubuntu5 (Ubuntu 24.04, recorded 2026-10-24): no static build
  has a stable URL with a published checksum, so the device-tests publish job
  installs Ubuntu's own package at this exact version with apt, which checks the
  archive's signature, and refuses any other version afterwards (APT_PINS). It
  makes the flow board's recording previews.

Tools land under $RUNNER_TEMP/may-tools. Scripts call them by absolute path.
"""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import StoreError

TOOLS = {
    ("jdk", "Linux", "x86_64"): (
        "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/"
        "OpenJDK21U-jdk_x64_linux_hotspot_21.0.12.1_1.tar.gz",
        "ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94", "jdk-21.0.12.1+1"),
    ("age", "Linux", "x86_64"): (
        "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-linux-amd64.tar.gz",
        "bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377", "age"),
    ("age", "Darwin", "arm64"): (
        "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-darwin-arm64.tar.gz",
        "01120ea2cbf0463d4c6bd767f99f3271bbed1cdc8a9aa718a76ba1fe4f01998b", "age"),
}


# Ubuntu packages installed at an exact version from the runner's signed archive (Linux only).
APT_PINS = {"ffmpeg": "7:6.1.1-3ubuntu5"}


def apt_install(name, run=subprocess.run):
    """APT_PINS[name] from Ubuntu's archive (apt checks its signature), quietly, then its version checked."""
    if name not in APT_PINS or platform.system() != "Linux":
        raise StoreError("No pinned package for this runner")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"}
    for argv in (["sudo", "apt-get", "update", "-qq"],
                 ["sudo", "apt-get", "install", "-y", "-qq", "--no-install-recommends", f"{name}={APT_PINS[name]}"]):
        if run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
               timeout=600).returncode:
            raise StoreError("The pinned package did not install")
    found = run(["dpkg-query", "-W", "-f=${Version}", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, env=env, timeout=60)
    if found.returncode or found.stdout.decode(errors="replace") != APT_PINS[name]:
        raise StoreError("The installed package is not the pinned version")


def tools_root():
    temp = os.environ.get("RUNNER_TEMP")
    if not temp:
        raise StoreError("Protected runner temp is unavailable")
    return Path(temp).resolve() / "may-tools"


def tool_path(name, relative):
    """Absolute path of a file inside an installed pinned tool; refuses if absent."""
    top = TOOLS.get((name, platform.system(), platform.machine()))
    if not top:
        raise StoreError("No pinned tool for this runner")
    path = tools_root() / name / top[2] / relative
    if not path.is_file() or path.is_symlink():
        raise StoreError("Pinned tool is not installed")
    return str(path)


def install(name, system=None, machine=None):
    key = (name, system or platform.system(), machine or platform.machine())
    if key not in TOOLS:
        raise StoreError("No pinned tool for this runner")
    url, expected, top = TOOLS[key]
    root = tools_root()
    root.mkdir(mode=0o700, exist_ok=True)
    archive, target = root / (name + ".tar.gz"), root / name
    if target.exists():
        raise StoreError("Pinned tool directory already exists")
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise StoreError("Pinned tool download checksum differs")
        if not hasattr(tarfile, "data_filter"):
            raise StoreError("Python lacks safe tar extraction")
        target.mkdir(mode=0o700)
        with tarfile.open(archive) as package:
            package.extractall(target, filter="data")
        if not (target / top).is_dir():
            raise StoreError("Pinned tool archive layout differs")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        archive.unlink(missing_ok=True)
    return target / top


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=("jdk", "age", "ffmpeg"))
    parser.add_argument("--export", action="store_true",
                        help="add to GITHUB_PATH (and JAVA_HOME to GITHUB_ENV); for no-secret jobs only")
    args = parser.parse_args()
    if args.tool == "ffmpeg":
        try:
            apt_install("ffmpeg")
        except StoreError as error:
            raise SystemExit("Pinned package install refused: " + error.args[0]) from None
        except Exception:
            raise SystemExit("Pinned package install failed") from None
        print("Pinned ffmpeg installed from Ubuntu's signed archive")
        raise SystemExit(0)
    try:
        home = install(args.tool)
        if args.export:
            bin_dir = home / "bin" if args.tool == "jdk" else home
            with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as stream:
                stream.write(str(bin_dir) + "\n")
            if args.tool == "jdk":
                with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
                    stream.write("JAVA_HOME=" + str(home) + "\n")
    except StoreError as error:
        raise SystemExit("Pinned toolchain install refused: " + error.args[0]) from None
    except Exception:
        raise SystemExit("Pinned toolchain install failed") from None
    print("Pinned, checksum-verified " + args.tool + " installed")
