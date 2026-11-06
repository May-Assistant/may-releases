"""Upload an iOS release's dSYMs to PostHog, so its crash reports are symbolicated (store-release-draft.yml).

It runs in ios-release after the TestFlight upload succeeded, on the opened handoff's May.xcarchive (handoff.py open
places it under may-release; the release's cleanup removes it), with PostHog's own CLI, pinned and checksum-verified
(toolchain.py's posthog-cli, which this step installs itself after it has taken the key out of its environment): `posthog-cli --host <host> dsym upload
--directory May.xcarchive/dSYMs`. No source is included. The step holds MAY_POSTHOG_IOS_SYMBOLS_KEY alone (a PostHog
personal API key with the error tracking write and organization read scopes; coordinator, 6 Nov), taken out of the
environment at once and given to the CLI's own environment alone, with the project id (the variable
MAY_POSTHOG_IOS_PROJECT_ID) and a HOME of its own under may-private, so nothing the CLI keeps outlives the job.

The host is PostHog's app host for the cloud the iOS app reports to (MAY_POSTHOG_IOS_HOST, the ingestion host the
crash-report key uses: us.i or eu.i, US when unset). The CLI's output goes to a private log alone; this log has fixed
lines, the number of dSYM bundles and the CLI's integer exit status. **It never fails the release**: an unset key or
project id skips with a fixed line, and a failed upload is reported and exits 0, since the build is already in
TestFlight by then.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it after the standard library.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import StoreError, child_env, require_step_secrets, runner_root
import toolchain

SYMBOLS_KEY, PROJECT, HOST = "MAY_POSTHOG_IOS_SYMBOLS_KEY", "MAY_POSTHOG_IOS_PROJECT_ID", "MAY_POSTHOG_IOS_HOST"
# The crash-report key's ingestion host (store.POSTHOG_HOSTS) -> the same cloud's app host, which the CLI talks to.
APP_HOSTS = {"https://us.i.posthog.com": "https://us.posthog.com", "https://eu.i.posthog.com": "https://eu.posthog.com"}
PROJECT_ID = re.compile(r"[1-9][0-9]{0,11}")
DSYMS = Path("may-release/May.xcarchive/dSYMs")
LOG, HOME = "dsym-upload.log", "dsym-home"
UPLOAD_SECONDS, MAX_BUNDLES = 15 * 60, 200


class DsymError(ValueError):
    pass


def app_host(value):
    """PostHog's app host for the ingestion host value (one trailing slash tolerated; US when unset), or None."""
    return APP_HOSTS.get((value or "").strip().rstrip("/") or "https://us.i.posthog.com")


def bundles(folder):
    """The archive's dSYM bundles: real folders named *.dSYM directly in folder, never a link; [] when it has none."""
    if folder.is_symlink() or not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir()
                  if path.name.endswith(".dSYM") and path.is_dir() and not path.is_symlink())[:MAX_BUNDLES + 1]


def install():
    """The pinned, checksum-verified CLI's folder (toolchain.py), installed now unless this job already has it."""
    try:
        return Path(toolchain.tool_path("posthog-cli", "posthog-cli")).parent
    except StoreError:
        return toolchain.install("posthog-cli")


def upload(run=subprocess.run):
    """Uploads the dSYMs; returns the CLI's exit status, or None when it was skipped. Never raises for a failed upload."""
    require_step_secrets((SYMBOLS_KEY,))
    # Held in memory alone from here: only the CLI's own environment gets it, never an argument, a file or a line.
    key = os.environ.pop(SYMBOLS_KEY, "")
    if not key:
        print("May iOS dSYMs: MAY_POSTHOG_IOS_SYMBOLS_KEY is not set; nothing uploaded")
        return None
    project = os.environ.get(PROJECT, "").strip()
    if not PROJECT_ID.fullmatch(project):
        print("May iOS dSYMs: MAY_POSTHOG_IOS_PROJECT_ID is not set or not a project number; nothing uploaded")
        return None
    host = app_host(os.environ.get(HOST))
    if host is None:
        print("May iOS dSYMs: MAY_POSTHOG_IOS_HOST is not PostHog's US or EU cloud; nothing uploaded")
        return None
    root = runner_root()
    folder = root / DSYMS
    found = bundles(folder)
    if not found:
        print("May iOS dSYMs: the archive holds no dSYM bundle; nothing uploaded")
        return None
    if len(found) > MAX_BUNDLES:
        print("May iOS dSYMs: the archive holds more dSYM bundles than expected; nothing uploaded")
        return None
    try:
        cli = install() / "posthog-cli"
    except (StoreError, OSError, ValueError):
        print("May iOS dSYMs: the pinned PostHog CLI could not be installed; nothing uploaded")
        return None
    if cli.is_symlink() or not cli.is_file():
        print("May iOS dSYMs: the pinned PostHog CLI could not be installed; nothing uploaded")
        return None
    private = root / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    home = private / HOME
    home.mkdir(mode=0o700, exist_ok=True)
    env = child_env(HOME=str(home), POSTHOG_CLI_API_KEY=key, POSTHOG_CLI_PROJECT_ID=project)
    try:
        with (private / LOG).open("ab") as output:
            done = run([str(cli), "--host", host, "dsym", "upload", "--directory", str(folder)], env=env, stdout=output,
                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=UPLOAD_SECONDS)
        status = done.returncode
    except subprocess.TimeoutExpired:
        print("May iOS dSYMs: the upload did not finish in time; the release is not failed for it")
        return 1
    except OSError:
        print("May iOS dSYMs: the PostHog CLI could not start; the release is not failed for it")
        return 1
    count = len(found)
    if status == 0:
        print(f"May iOS dSYMs: {int(count)} dSYM bundles uploaded to PostHog")
    else:
        print(f"May iOS dSYMs: the upload of {int(count)} dSYM bundles did not succeed (exit status {int(status)}); "
              "the release is not failed for it")
    return status


def main():
    parser = argparse.ArgumentParser()
    # The release's own inputs, as its other steps take them; nothing here depends on them but the check below.
    parser.add_argument("--target", choices=("production", "playground"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha) or not re.fullmatch(r"[1-9][0-9]{0,8}", args.build_number):
        raise DsymError("The release's inputs are malformed; nothing uploaded")
    upload()


if __name__ == "__main__":
    try:
        main()
    except (DsymError, StoreError):
        # Reported, never failing the release: the build is already in TestFlight.
        print("May iOS dSYMs stopped: another step's secret or a malformed input; nothing uploaded")
    except Exception:
        print("May iOS dSYMs stopped; no key, path or CLI output was printed")
