"""Remove task-owned private material from RUNNER_TEMP at the end of a store job.

A Python command (not shell) so that release jobs run nothing but this
repository's Python. Fixed paths only; never fails the job.
"""

import os
from pathlib import Path
import shutil
import sys

PATHS = {
    "build": ("may-source", "may-private", "may-design-system", "may-release", "may-handoff"),
    "release": ("may-private", "may-release", "may-handoff-download"),
}

if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) == 2 else ""
    temp = os.environ.get("RUNNER_TEMP")
    if job in PATHS and temp and Path(temp).is_dir():
        for name in PATHS[job]:
            shutil.rmtree(Path(temp) / name, ignore_errors=True)
    print("Task-owned private material removed")
