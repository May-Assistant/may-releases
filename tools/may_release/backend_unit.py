"""May's backend checks at an open may-app pull request head: pass or fail per stage, and test counts.

may-app's own backend-ci.yml is switched off (private Actions are blocked by billing), so this runs the same job here,
on a free public Linux runner, at the head prepare.py fetched (--pr-number): the pinned Node 22.22.0 (toolchain.py),
against the job's Postgres 16.15 service container (pinned by digest in backend-unit-pr.yml), in STAGES order:
- toolchain: Node is exactly v22.22.0 and the server exactly Postgres 16.15 (a check of this runner: it fails the
  step, like android_unit.py's pinned JDK);
- install: `npm ci --no-audit --no-fund` in backend/, with npm's cache under $RUNNER_TEMP/may-npm;
- typecheck: `npm run typecheck`;
- migrations: as backend-ci.yml's "Migrations from scratch": create the may_owner role (NOSUPERUSER NOBYPASSRLS
  CREATEROLE) and the may_ci_migrate database it owns, run `npm run -s migrate` twice as may_owner, then require
  every backend/migrations/NNN_name.sql to be applied exactly once (schema_migrations), the first run to say
  `applied <n> migration(s)` and the second to end with `schema up to date`;
- tests: `npm test` (node --test, which bounds each file itself) with the job's Postgres as MAY_TEST_ADMIN_URL (as
  TEST_ADMIN, never the superuser),
  stopped after 25 minutes (the suite outgrew backend-ci.yml's 15 by 25 Oct).
An install that fails stops the stages after it; a failed typecheck or migration run still runs the stages after it,
so one red stage does not hide another (as Gradle's --continue in android_unit.py).

This repository is public, so nothing the backend, its dependencies or its tests print reaches this log: every tool
writes to a private log on the runner. Public output is one fixed line per stage (its name from STAGES and a fixed
verdict) and one line of test counts summed by node --test itself (its closing `# tests N`, `# pass N`, ... lines,
read from the private log): tests, passed, failed (fail and cancelled) and skipped (skipped and todo). Never a test
name, message or path.

With --record-outcome (backend-unit-pr.yml), a red stage does not fail this step. The outcome and the detail (each
failing test's name, the first line of its error and its location, repo-relative; tsc's errors; and, for a stage
with neither, the last lines of its log, cleaned) go to private files (OUTCOME, FAILURES), and backend_unit_post.py,
the next step, posts them on the may-app pull request and then fails the job. Every check before the stages (secrets,
the prepared checkout, the pinned Node, Postgres) still fails here. The detail is never printed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from android_unit import ANSI, clean
from store import StoreError, child_env, prepared, require_step_secrets, runner_root
import toolchain

# Stage name -> its private log, under may-private. Order is the run and printed order.
STAGES = {"toolchain": "backend-toolchain.log", "install": "backend-install.log", "typecheck": "backend-typecheck.log",
          "migrations": "backend-migrations.log", "tests": "backend-tests.log"}
# may-app backend-ci.yml's pins: NODE_EXPECTED_VERSION and PG_EXPECTED_VERSION.
NODE_VERSION = "v22.22.0"
POSTGRES_VERSION = "16.15"
# The service container's superuser, with trust auth on the runner's loopback, and the migration owner's URL
# backend-ci.yml builds (user may_owner, no password, database may_ci_migrate).
ADMIN_URL = "postgres://may_admin@127.0.0.1:5432/postgres"
MIGRATE_URL = "postgres://may_owner@127.0.0.1:5432/may_ci_migrate"
# The tests' admin, MAY_TEST_ADMIN_URL (coordinator, 26 Oct): the pull request's tests and migrations hold this URL, so it
# is no superuser, and may set no logging parameter or reach the superuser; everything else the suite does (databases,
# roles, BYPASSRLS roles, terminating its own backends, replica mode) it may. Its name has no may_ prefix: the suite
# lists may_* roles. Made by the superuser (LOCK_SQL); may_owner is then made by it (TEST_OWNER_SQL), so it holds
# may_owner's rights (createrole_self_grant), and the superuser can no longer log in (CLOSE_SQL): with trust auth on
# the loopback, any process here could otherwise connect as may_admin. The superuser URL never leaves this script.
TEST_ADMIN = "ci_admin"
TEST_ADMIN_URL = "postgres://ci_admin@127.0.0.1:5432/postgres"
LOCK_SQL = ("CREATE ROLE ci_admin LOGIN NOSUPERUSER CREATEDB CREATEROLE BYPASSRLS",
            "GRANT pg_signal_backend, pg_read_all_stats, pg_read_all_data, pg_write_all_data TO ci_admin",
            "GRANT SET ON PARAMETER session_replication_role TO ci_admin WITH GRANT OPTION",
            "ALTER ROLE ci_admin SET createrole_self_grant = 'set, inherit'")
TEST_OWNER_SQL = ("CREATE ROLE may_owner LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE",
                  "GRANT SET ON PARAMETER session_replication_role TO may_owner")
CLOSE_SQL = "ALTER ROLE may_admin NOLOGIN"
# Read as the tests' admin once the superuser is closed: itself no superuser, may_admin unable to log in, and what the
# suite's shared setup needs on Postgres 16 (K, may-app #1352): SET ROLE may_owner, and createrole_self_grant in a new
# session, without which that setup fails ("must be able to SET ROLE") and the run hangs instead of exiting.
LOCKED_SQL = ("SELECT r.rolsuper::text || ',' || a.rolcanlogin::text || ',' || "
              "pg_has_role(current_user, 'may_owner', 'SET')::text || ',' || current_setting('createrole_self_grant') "
              "FROM pg_roles r, pg_roles a WHERE r.rolname = current_user AND a.rolname = 'may_admin'")
LOCKED = "false,false,true,set, inherit"
OWNER_SQL = ("CREATE DATABASE may_ci_migrate OWNER may_owner",)
# The same filename rule as may-app's src/db/migrate.ts (and backend-ci.yml's find).
MIGRATION_FILE = re.compile(r"[0-9]{3}_[a-z0-9_]+\.sql")
INSTALL_SECONDS = 10 * 60
TYPECHECK_SECONDS = 10 * 60
MIGRATE_SECONDS = 5 * 60
# Run 9 (40239424875) stopped at 15 minutes with no summary; the suite (2,562 tests on 24 Oct, its whole stage list in
# 14:41) now needs about 17-18. Headroom inside the job's 30 minutes and the one-hour app token.
TEST_SECONDS = 25 * 60
# The service container's own log goes into this public job log at teardown (docker logs --details), and Postgres
# logs each failing statement's text with it: runs 1-9 published may-app's SQL that way. Before anything else, the
# cluster logs nothing but a panic. ALTER SYSTEM runs outside a transaction, so each is its own psql call.
SILENCE_SQL = ("ALTER SYSTEM SET log_min_messages = 'panic'", "ALTER SYSTEM SET log_min_error_statement = 'panic'",
               "ALTER SYSTEM SET log_checkpoints = off", "SELECT pg_reload_conf()")
SILENT = {"log_min_messages": "panic", "log_min_error_statement": "panic", "log_checkpoints": "off"}
# The second guard: the service log's lines that carry a statement, its detail or its context fail the job, counted
# alone; the log itself goes to a private file and is never printed.
LEAKY = re.compile(rb"\b(?:ERROR|STATEMENT|DETAIL|CONTEXT|HINT):")
SERVICE_LOG = "backend-service.log"
# node --test's closing summary, TAP (`# tests 12`, Node 22 when not on a terminal) or spec (`ℹ tests 12`).
SUMMARY = re.compile(r"(?:# |ℹ )(tests|suites|pass|fail|cancelled|skipped|todo|duration_ms) ([0-9]{1,7})(?:\.[0-9]+)?")
# tsc's error lines (no colour when not on a terminal): `src/x.ts(12,5): error TS2322: message`.
TSC_ERROR = re.compile(r"(.+?)\(([0-9]{1,7}),([0-9]{1,7})\): error (TS[0-9]{1,6}): (.*)")
# A TAP result line, with its indent.
TAP_RESULT = re.compile(r"( *)not ok [0-9]+ - (.*?)(?: # (?:SKIP|TODO).*)?")
# --record-outcome's private files, under may-private, for backend_unit_post.py.
OUTCOME, FAILURES = "backend-unit-result.json", "backend-unit-failures.json"
# Failing tests and type errors recorded (the rest are counted), the longest name, message and location kept, and how
# many of a stage's last log lines stand in when it left nothing more specific.
MAX_FAILURES = 100
MAX_NAME, MAX_MESSAGE = 200, 240
TAIL_LINES = 40
# A private log is read for the summary and the detail up to this size (its end, where the summary is).
MAX_LOG_BYTES = 16 * 1024 * 1024
VERDICTS = ("passed", "failed", "timeout", "not run")


class BackendTestsError(ValueError):
    pass


def quiet(argv, log, *, cwd=None, env=None, timeout=None):
    """The tool's exit status, or None when it ran past timeout; its output goes to the private log alone."""
    try:
        with log.open("ab") as output:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return result.returncode


def value(argv, env):
    """One short answer from a tool (its stripped stdout), or None; never printed."""
    try:
        result = subprocess.run(argv, env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() if not result.returncode else None


def pinned_node_bin():
    """The bin folder of the pinned Node toolchain.py installed (never the runner's own), by absolute path."""
    try:
        return Path(toolchain.tool_path("node", "bin/node")).parent
    except StoreError:
        raise BackendTestsError("The pinned Node is not installed; nothing ran") from None


def log_text(path):
    """The end of a private log, as text without ANSI codes; empty when it is missing or linked."""
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - MAX_LOG_BYTES))
        return ANSI.sub("", stream.read().decode("utf-8", errors="replace"))


def test_counts(text):
    """(tests, passed, failed, skipped) from node --test's last summary lines, or None without tests, pass and fail.
    failed counts fail and cancelled; skipped counts skipped and todo. The last of each line counts, so a test's own
    output earlier in the log cannot stand in for the summary."""
    found = {}
    for line in text.splitlines():
        match = SUMMARY.fullmatch(line.strip())
        if match:
            found[match.group(1)] = int(match.group(2))
    if not {"tests", "pass", "fail"} <= set(found):
        return None
    return (found["tests"], found["pass"], found["fail"] + found.get("cancelled", 0),
            found.get("skipped", 0) + found.get("todo", 0))


def unquoted(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].replace("''", "'") if text[0] == "'" else text[1:-1]
    return text


def yaml_fields(lines, start, indent):
    """{field: first line of its value} of the YAML block after a TAP result at lines[start], indented past indent."""
    fields, index = {}, start + 1
    while index < len(lines):
        line = lines[index]
        depth = len(line) - len(line.lstrip(" "))
        if line.strip() and depth <= indent:
            break
        if line.strip() == "..." and depth == indent + 2:
            break
        match = re.fullmatch(r" *([A-Za-z_]+): ?(.*)", line)
        if match and depth == indent + 2 and match.group(1) not in fields:
            raw = match.group(2).strip()
            if raw in ("|", "|-", ">", ">-", "|+", ">+"):
                following = next((n.strip() for n in lines[index + 1:]
                                  if n.strip() and len(n) - len(n.lstrip(" ")) > depth), "")
                fields[match.group(1)] = following
            else:
                fields[match.group(1)] = unquoted(raw)
        index += 1
    return fields


def failing_tests(text, roots):
    """(failures, total): each failing test of node --test's output, in order, at most MAX_FAILURES of them, as
    {test, message, at}. TAP's `not ok` results, leaving out suites and files that only failed because a test inside
    them did (failureType 'subtestsFailed'); otherwise the spec reporter's closing `failing tests:` list."""
    lines = text.splitlines()
    failures, total = [], 0
    for index, line in enumerate(lines):
        result = TAP_RESULT.fullmatch(line)
        if not result:
            continue
        fields = yaml_fields(lines, index, len(result.group(1)))
        if fields.get("failureType") == "subtestsFailed":
            continue
        total += 1
        if len(failures) < MAX_FAILURES:
            failures.append({"test": clean(result.group(2).replace("\\#", "#"), roots, MAX_NAME),
                             "message": clean(fields.get("error", ""), roots, MAX_MESSAGE),
                             "at": clean(fields["location"], roots, MAX_NAME) if fields.get("location") else None})
    if total:
        return failures, total
    marker = next((i for i, line in enumerate(lines) if line.strip().endswith("failing tests:")), None)
    if marker is None:
        return [], 0
    at = None
    for index in range(marker + 1, len(lines)):
        line = lines[index].strip()
        if line.startswith("test at "):
            at = line[len("test at "):]
        elif line.startswith("✖ "):
            total += 1
            message = next((n.strip() for n in lines[index + 1:index + 4] if n.strip()), "")
            if len(failures) < MAX_FAILURES:
                failures.append({"test": clean(re.sub(r" \([0-9.]+m?s\)$", "", line[2:]), roots, MAX_NAME),
                                 "message": clean(message, roots, MAX_MESSAGE),
                                 "at": clean(at, roots, MAX_NAME) if at else None})
            at = None
    return failures, total


def type_errors(text, roots):
    """(errors, total): tsc's error lines, at most MAX_FAILURES of them, as {at, code, message}; paths under backend/."""
    errors, total = [], 0
    for line in text.splitlines():
        match = TSC_ERROR.fullmatch(line.strip())
        if not match:
            continue
        total += 1
        if len(errors) < MAX_FAILURES:
            path = match.group(1) if match.group(1).startswith("/") else "backend/" + match.group(1)
            errors.append({"at": clean(f"{path}:{int(match.group(2))}:{int(match.group(3))}", roots, MAX_NAME),
                           "code": match.group(4), "message": clean(match.group(5), roots, MAX_MESSAGE)})
    return errors, total


def tail(text, roots):
    """A stage log's last TAIL_LINES non-empty lines, each cleaned as clean() does."""
    lines = [line for line in text.splitlines() if line.strip()]
    return [clean(line, roots, MAX_MESSAGE) for line in lines[-TAIL_LINES:]]


def migration_files(backend):
    folder = backend / "migrations"
    if folder.is_symlink() or not folder.is_dir():
        return 0
    return sum(1 for path in folder.iterdir()
               if MIGRATION_FILE.fullmatch(path.name) and path.is_file() and not path.is_symlink())


def migrate(backend, log, env):
    """(verdict, applied, files) of backend-ci.yml's "Migrations from scratch"."""
    for sql in OWNER_SQL:
        if quiet(["psql", TEST_ADMIN_URL, "-v", "ON_ERROR_STOP=1", "-qc", sql], log, env=env, timeout=60) != 0:
            return "failed", None, None
    env = dict(env, MAY_DATABASE_URL_MIGRATE=MIGRATE_URL)
    # Each run's own log, read alone below, then appended to the stage's log.
    first, second = log.parent / "backend-migrate-1.log", log.parent / "backend-migrate-2.log"
    for run in (first, second):
        run.unlink(missing_ok=True)
    for run in (first, second):
        status = quiet(["npm", "run", "-s", "migrate"], run, cwd=backend, env=env, timeout=MIGRATE_SECONDS)
        with log.open("ab") as output:
            output.write(run.read_bytes() if run.is_file() else b"")
        if status is None:
            return "timeout", None, None
        if status:
            return "failed", None, None
    files = migration_files(backend)
    count = value(["psql", MIGRATE_URL, "-Atc", "SELECT count(*) FROM schema_migrations"], env)
    applied = int(count) if count and re.fullmatch(r"[0-9]{1,6}", count) else None
    said = [line.strip() for line in log_text(first).splitlines() if line.strip()]
    last = [line.strip() for line in log_text(second).splitlines() if line.strip()]
    passed = files > 0 and applied == files and f"applied {files} migration(s)" in said and \
        bool(last) and last[-1] == "schema up to date"
    return ("passed" if passed else "failed"), applied, files


def check_toolchain(env, log):
    """Node is exactly NODE_VERSION, npm runs, and the service is exactly Postgres POSTGRES_VERSION; refuses otherwise."""
    if value(["node", "--version"], env) != NODE_VERSION:
        raise BackendTestsError("Node is not the pinned v22.22.0; nothing ran")
    if quiet(["npm", "--version"], log, env=env, timeout=60) != 0:
        raise BackendTestsError("The pinned Node's npm does not run; nothing ran")
    server = value(["psql", ADMIN_URL, "-Atc", "SHOW server_version"], env) or ""
    if server != POSTGRES_VERSION and not server.startswith(POSTGRES_VERSION + " "):
        raise BackendTestsError("The Postgres service is not 16.15, or not reachable; nothing ran")
    silence(env, log)
    lock(env, log)


def silence(env, log):
    """The service logs nothing a statement, test or migration could put in it (SILENCE_SQL), checked; refuses
    otherwise, before any of the pull request's code runs."""
    for sql in SILENCE_SQL:
        if quiet(["psql", ADMIN_URL, "-v", "ON_ERROR_STOP=1", "-qc", sql], log, env=env, timeout=60) != 0:
            raise BackendTestsError("The Postgres service's log could not be silenced; nothing ran")
    for setting, wanted in SILENT.items():
        if value(["psql", ADMIN_URL, "-Atc", "SHOW " + setting], env) != wanted:
            raise BackendTestsError("The Postgres service's log could not be silenced; nothing ran")


def lock(env, log):
    """The tests' admin made (LOCK_SQL, TEST_OWNER_SQL) and the superuser closed (CLOSE_SQL), checked as that admin
    (LOCKED_SQL) and by the superuser's refused login; refuses otherwise, before any of the pull request's code runs."""
    steps = [(ADMIN_URL, sql) for sql in LOCK_SQL] + [(TEST_ADMIN_URL, sql) for sql in TEST_OWNER_SQL] + \
        [(ADMIN_URL, CLOSE_SQL)]
    for url, sql in steps:
        if quiet(["psql", url, "-v", "ON_ERROR_STOP=1", "-qc", sql], log, env=env, timeout=60) != 0:
            raise BackendTestsError("The Postgres service's superuser could not be closed to the tests; nothing ran")
    if value(["psql", TEST_ADMIN_URL, "-Atc", LOCKED_SQL], env) != LOCKED or \
            value(["psql", ADMIN_URL, "-Atc", "SELECT 1"], env) is not None:
        raise BackendTestsError("The Postgres service's superuser could not be closed to the tests; nothing ran")


def run_checks(source_sha, record=False):
    require_step_secrets(())
    root = runner_root()
    source, private = prepared(root, source_sha)
    backend = source / "backend"
    if backend.is_symlink() or not backend.is_dir() or not (backend / "package-lock.json").is_file() or \
            (backend / "package-lock.json").is_symlink():
        raise BackendTestsError("The pull request has no backend/package-lock.json; nothing ran")
    if record:
        for name in (OUTCOME, FAILURES):
            (private / name).unlink(missing_ok=True)
    logs = {stage: private / name for stage, name in STAGES.items()}
    node_bin = pinned_node_bin()
    env = child_env(PATH=str(node_bin) + os.pathsep + os.environ.get("PATH", ""),
                    MAY_TEST_ADMIN_URL=TEST_ADMIN_URL, npm_config_cache=str(root / "may-npm"), npm_config_update_notifier="false",
                    npm_config_fund="false", npm_config_audit="false", GIT_TERMINAL_PROMPT="0")
    check_toolchain(env, logs["toolchain"])
    stages = dict.fromkeys(STAGES, "not run")
    stages["toolchain"] = "passed"
    counts = migrations = None
    status = quiet(["npm", "ci", "--no-audit", "--no-fund"], logs["install"], cwd=backend, env=env,
                   timeout=INSTALL_SECONDS)
    stages["install"] = "timeout" if status is None else "failed" if status else "passed"
    if stages["install"] == "passed":
        status = quiet(["npm", "run", "typecheck"], logs["typecheck"], cwd=backend, env=env, timeout=TYPECHECK_SECONDS)
        stages["typecheck"] = "timeout" if status is None else "failed" if status else "passed"
        stages["migrations"], applied, files = migrate(backend, logs["migrations"], env)
        migrations = [applied, files] if applied is not None and files is not None else None
        status = quiet(["npm", "test"], logs["tests"], cwd=backend, env=env, timeout=TEST_SECONDS)
        counts = test_counts(log_text(logs["tests"]))
        stages["tests"] = "timeout" if status is None else \
            "passed" if not status and counts and counts[0] and not counts[2] else "failed"
    if not record:
        report(stages, counts)
        return
    try:
        report(stages, counts)
        passed = True
    except BackendTestsError:
        passed = False
    record_outcome(private, source, logs, stages, counts, migrations, passed)
    print("May backend checks finished; any failures stay in this runner's private files for the next step")


def record_outcome(private, source, logs, stages, counts, migrations, passed):
    roots = [str(source), str(source.resolve())]
    failures, total = failing_tests(log_text(logs["tests"]), roots)
    errors, error_total = type_errors(log_text(logs["typecheck"]), roots)
    tails = {}
    for stage in ("install", "typecheck", "migrations", "tests"):
        if stages[stage] in ("failed", "timeout") and not (stage == "typecheck" and errors) and \
                not (stage == "tests" and failures):
            tails[stage] = tail(log_text(logs[stage]), roots)
    (private / FAILURES).write_text(json.dumps({"failures": failures, "total": total, "typecheck": errors,
                                                "typecheck_total": error_total, "tails": tails}))
    (private / OUTCOME).write_text(json.dumps({
        "result": "passed" if passed else "failed", "stages": stages,
        "tests": list(counts) if counts is not None else None, "migrations": migrations}))


def report(stages, counts):
    """One fixed line per stage and one of test counts, then pass only if every stage passed with at least one test."""
    for name in STAGES:
        verdict = stages[name]
        if verdict == "timeout":
            print(f"May backend {name}: did not finish in time")
        elif verdict == "not run":
            print(f"May backend {name}: did not run (an earlier stage stopped it)")
        else:
            print(f"May backend {name}: {verdict}")
    if counts is None:
        print("May backend tests: no test summary (npm test stopped before node --test reported)")
    else:
        tests, passed, failed, skipped = (int(n) for n in counts)
        print(f"May backend tests: {int(tests)} tests, {int(passed)} passed, {int(failed)} failed, {int(skipped)} skipped")
    if stages["tests"] == "timeout":
        raise BackendTestsError("npm test did not finish within 25 minutes")
    if any(stages[name] != "passed" for name in STAGES):
        raise BackendTestsError("At least one May backend check did not pass")
    print("Every May backend check passed")


def check_service_log(container):
    """The guard after the checks: the service container's log, read into a private file, holds no line that could
    carry an error, a statement, its detail, context or hint; fails the step with their count otherwise. Never prints the log."""
    require_step_secrets(())
    if not re.fullmatch(r"[0-9a-f]{12,64}", container or ""):
        raise BackendTestsError("The Postgres service's container is unknown, so its log was not checked")
    private = runner_root() / "may-private"
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / SERVICE_LOG
    with path.open("wb") as output:
        try:
            done = subprocess.run(["docker", "logs", "--details", container], stdout=output, stderr=output,
                                  stdin=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError):
            done = None
    if done is None or done.returncode:
        raise BackendTestsError("The Postgres service's log could not be read, so it was not checked")
    leaked = sum(1 for line in path.read_bytes().splitlines() if LEAKY.search(line))
    if leaked:
        print(f"May backend database log: {int(leaked)} lines that could carry may-app's SQL or data")
        raise BackendTestsError("The Postgres service's log is not silent")
    print("May backend database log: no error, statement, detail, context or hint lines")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha")
    parser.add_argument("--record-outcome", action="store_true")
    parser.add_argument("--check-service-log", metavar="CONTAINER")
    args = parser.parse_args()
    try:
        if args.check_service_log is not None:
            check_service_log(args.check_service_log)
        elif args.source_sha:
            run_checks(args.source_sha, args.record_outcome)
        else:
            raise BackendTestsError("--source-sha or --check-service-log is required")
    except (BackendTestsError, StoreError) as error:
        raise SystemExit("May backend checks: " + error.args[0]) from None
    except Exception:
        # Any other failure, whatever its type or message, prints only this.
        raise SystemExit("May backend checks failed; no private detail was published") from None
