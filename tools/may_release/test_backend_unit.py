"""May's backend checks at a may-app pull request head (backend-unit-pr.yml, backend_unit.py)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backend_unit
from backend_unit import BackendTestsError, failing_tests, test_counts, type_errors
import static_check

WORKFLOW = ".github/workflows/backend-unit-pr.yml"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": os.environ.get("PATH", "")}
# Private words the backend's output carries that must never reach the public log.
PRIVATE = "PRIVATE-BACKEND-TEST-NAME-4b1d"
MESSAGE = "PRIVATE-ASSERTION-TEXT-77e0"
TYPE_ERROR = "PRIVATE-TYPE-ERROR-9c3a"


def tap(root, passed=3, failed=0, skipped=0, todo=0, cancelled=0, summary=True):
    """node --test's TAP output (Node 22, not on a terminal): one file whose failing tests sit in a suite, so the suite
    and the file fail only because a test inside them did (subtestsFailed)."""
    source = f"{root}/may-source/backend/test/private.test.ts"
    lines = ["TAP version 13", "# \\# tests 999", f"# Subtest: {source}"]
    for n in range(passed):
        lines += [f"    ok {n + 1} - passes {n}", "      ---", "      duration_ms: 1", "      ..."]
    for n in range(failed):
        lines += [f"    # Subtest: {PRIVATE} {n}", f"    not ok {passed + n + 1} - {PRIVATE} {n}", "      ---",
                  "      duration_ms: 2", f"      location: '{source}:{12 + n}:3'", "      failureType: 'testCodeFailure'",
                  "      error: |-", f"        {MESSAGE} at {root}/may-source/backend/src/x.ts", "        ",
                  "        1 !== 2", "      code: 'ERR_ASSERTION'", "      stack: |-",
                  f"        TestContext.<anonymous> (file://{source}:12:9)", "      ..."]
    if failed:
        lines += [f"not ok 1 - {source}", "  ---", "  type: 'test'", f"  location: '{source}:1:1'",
                  "  failureType: 'subtestsFailed'", f"  error: '{failed} subtests failed'", "  ..."]
    else:
        lines += [f"ok 1 - {source}", "  ---", "  ..."]
    if summary:
        total = passed + failed + skipped + todo + cancelled
        lines += ["1..1", f"# tests {total}", "# suites 0", f"# pass {passed}", f"# fail {failed}",
                  f"# cancelled {cancelled}", f"# skipped {skipped}", f"# todo {todo}", "# duration_ms 1234.5"]
    return "\n".join(lines) + "\n"


class Fixture(unittest.TestCase):
    """A prepared runner: may-source (a git checkout with backend/ and the placed design system), may-private, and the
    pinned Node's bin folder, under a temporary RUNNER_TEMP. Every tool is replaced by a fake `quiet` and `value`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        source = self.root / "may-source"
        (source / "backend/migrations").mkdir(parents=True)
        (source / "backend/package.json").write_text("{}\n")
        (source / "backend/package-lock.json").write_text("{}\n")
        for name in ("001_init.sql", "002_more_things.sql", "README.md", "3_bad.sql"):
            (source / "backend/migrations" / name).write_text("-- x\n")
        (source / "android/build-deps/may-design-system").mkdir(parents=True)
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(source)] + argv, check=True, env=GIT_ENV, capture_output=True)
        self.sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, env=GIT_ENV,
                                  capture_output=True).stdout.decode().strip()
        (self.root / "may-private").mkdir()
        self.node = self.root / "may-tools/node/node-v22.22.0-linux-x64/bin"
        self.environ = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "HOME": str(self.root)}
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def fake(self, install=0, typecheck=0, migrate=("applied 2 migration(s)", "schema up to date"), migrate_status=0,
             test_status=0, test_output=None, owner=0, tsc_output="", test_timeout=False):
        runs = list(migrate)

        def quiet(argv, log, *, cwd=None, env=None, timeout=None):
            self.calls.append((list(argv), cwd, dict(env or {})))

            def write(text):
                with log.open("a") as stream:
                    stream.write(text)
            if argv[:2] == ["npm", "--version"]:
                return 0
            if argv[:2] == ["npm", "ci"]:
                write("npm ERR! private dependency trouble\n" if install else "added 12 packages\n")
                return install
            if argv[:3] == ["npm", "run", "typecheck"]:
                write(tsc_output)
                return typecheck
            if argv[:4] == ["npm", "run", "-s", "migrate"]:
                write(runs.pop(0) + "\n" if runs else "")
                return migrate_status
            if argv[:2] == ["npm", "test"]:
                write(test_output if test_output is not None else tap(self.root))
                return None if test_timeout else test_status
            if argv[0] == "psql":
                return owner
            raise AssertionError("unexpected tool " + argv[0])
        return quiet

    def value(self, node="v22.22.0", server="16.15 (Debian 16.15-1.pgdg120+1)", applied="2"):
        def answer(argv, env):
            self.calls.append((list(argv), None, dict(env or {})))
            if argv == ["node", "--version"]:
                return node
            if "SHOW server_version" in argv:
                return server
            if "SELECT count(*) FROM schema_migrations" in argv:
                return applied
            raise AssertionError("unexpected value " + argv[0])
        return answer

    def run_checks(self, quiet, value=None, environ=None, record=False):
        out = io.StringIO()
        with patch.dict(os.environ, environ or self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(backend_unit, "quiet", quiet), patch.object(backend_unit, "value", value or self.value()), \
                patch.object(backend_unit, "pinned_node_bin", lambda: self.node):
            try:
                backend_unit.run_checks(self.sha, record)
            except BackendTestsError as error:
                return out.getvalue(), error.args[0]
        return out.getvalue(), None

    def record(self, quiet, value=None):
        out, error = self.run_checks(quiet, value, record=True)
        self.assertIsNone(error)
        private = self.root / "may-private"
        return (out, json.loads((private / backend_unit.OUTCOME).read_text()),
                json.loads((private / backend_unit.FAILURES).read_text()))

    def tools(self):
        return [call[0][:3] for call in self.calls]


class RunTests(Fixture):
    def test_every_stage_passing_prints_only_fixed_lines_and_counts(self):
        out, error = self.run_checks(self.fake(test_output=tap(self.root, passed=5, skipped=1, todo=2)))
        self.assertIsNone(error)
        self.assertEqual(out.splitlines(), [
            "May backend toolchain: passed", "May backend install: passed", "May backend typecheck: passed",
            "May backend migrations: passed", "May backend tests: passed",
            "May backend tests: 8 tests, 5 passed, 0 failed, 3 skipped", "Every May backend check passed"])
        for private in (PRIVATE, MESSAGE, "private.test.ts", str(self.root)):
            self.assertNotIn(private, out)

    def test_the_stages_run_in_backend_ci_order_in_backend(self):
        self.run_checks(self.fake())
        self.assertEqual(self.tools(), [
            ["node", "--version"], ["npm", "--version"], ["psql", backend_unit.ADMIN_URL, "-Atc"],
            ["npm", "ci", "--no-audit"], ["npm", "run", "typecheck"],
            ["psql", backend_unit.ADMIN_URL, "-v"], ["psql", backend_unit.ADMIN_URL, "-v"],
            ["npm", "run", "-s"], ["npm", "run", "-s"], ["psql", backend_unit.MIGRATE_URL, "-Atc"], ["npm", "test"]])
        backend = self.root / "may-source/backend"
        self.assertTrue(all(cwd == backend for argv, cwd, _ in self.calls if argv[0] == "npm" and argv[1] != "--version"))
        owner = [argv[-1] for argv, _, _ in self.calls if "-qc" in argv]
        self.assertEqual(owner, ["CREATE ROLE may_owner LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE",
                                 "CREATE DATABASE may_ci_migrate OWNER may_owner"])

    def test_the_tools_get_the_pinned_node_the_test_database_and_the_store_allowlist_only(self):
        environ = dict(self.environ, SOMETHING_ELSE="x", GH_TOKEN="t", GITHUB_TOKEN="t")
        self.run_checks(self.fake(), environ=environ)
        for argv, _, env in self.calls:
            with self.subTest(tool=argv[:2]):
                self.assertTrue(env["PATH"].startswith(str(self.node) + os.pathsep))
                self.assertEqual(env["MAY_TEST_ADMIN_URL"], "postgres://may_admin@127.0.0.1:5432/postgres")
                self.assertEqual(env["npm_config_cache"], str(self.root / "may-npm"))
                for name in ("SOMETHING_ELSE", "GH_TOKEN", "GITHUB_TOKEN"):
                    self.assertNotIn(name, env)
        migrate = [env for argv, _, env in self.calls if argv[:4] == ["npm", "run", "-s", "migrate"]]
        self.assertEqual({env["MAY_DATABASE_URL_MIGRATE"] for env in migrate},
                         {"postgres://may_owner@127.0.0.1:5432/may_ci_migrate"})
        self.assertNotIn("MAY_DATABASE_URL_MIGRATE", [n for argv, _, env in self.calls if argv[:2] == ["npm", "test"] for n in env])

    def test_a_failing_test_fails_the_tests_stage_and_the_job(self):
        out, error = self.run_checks(self.fake(test_status=1, test_output=tap(self.root, passed=4, failed=2)))
        self.assertEqual(error, "At least one May backend check did not pass")
        self.assertIn("May backend tests: failed", out)
        self.assertIn("May backend tests: 6 tests, 4 passed, 2 failed, 0 skipped", out)
        self.assertNotIn(PRIVATE, out)
        self.assertNotIn(MESSAGE, out)

    def test_a_cancelled_test_counts_as_failed_and_no_test_at_all_fails(self):
        out, error = self.run_checks(self.fake(test_status=1, test_output=tap(self.root, passed=2, cancelled=1)))
        self.assertIn("May backend tests: 3 tests, 2 passed, 1 failed, 0 skipped", out)
        self.calls = []
        out, error = self.run_checks(self.fake(test_output=tap(self.root, passed=0)))
        self.assertEqual(error, "At least one May backend check did not pass")
        self.assertIn("May backend tests: failed", out)

    def test_npm_test_exiting_non_zero_fails_even_when_its_summary_shows_no_failure(self):
        out, error = self.run_checks(self.fake(test_status=1))
        self.assertEqual(error, "At least one May backend check did not pass")
        self.assertIn("May backend tests: failed", out)

    def test_no_summary_is_said_in_a_fixed_line(self):
        out, error = self.run_checks(self.fake(test_status=1, test_output=tap(self.root, summary=False)))
        self.assertIn("May backend tests: no test summary (npm test stopped before node --test reported)", out)
        self.assertEqual(error, "At least one May backend check did not pass")

    def test_npm_test_that_does_not_finish_fails(self):
        out, error = self.run_checks(self.fake(test_timeout=True, test_output=tap(self.root, summary=False)))
        self.assertEqual(error, "npm test did not finish within 15 minutes")
        self.assertIn("May backend tests: did not finish in time", out)
        self.assertEqual(backend_unit.TEST_SECONDS, 15 * 60)

    def test_a_failed_install_stops_every_later_stage(self):
        out, error = self.run_checks(self.fake(install=1))
        self.assertEqual(error, "At least one May backend check did not pass")
        self.assertEqual([t for t in self.tools() if t[0] == "npm" and t[1] != "--version"], [["npm", "ci", "--no-audit"]])
        for stage in ("typecheck", "migrations", "tests"):
            self.assertIn(f"May backend {stage}: did not run (an earlier stage stopped it)", out)
        self.assertNotIn("private dependency trouble", out)

    def test_a_failed_typecheck_or_migration_still_runs_the_tests(self):
        out, error = self.run_checks(self.fake(typecheck=2, migrate_status=1))
        self.assertEqual(error, "At least one May backend check did not pass")
        self.assertIn("May backend typecheck: failed", out)
        self.assertIn("May backend migrations: failed", out)
        self.assertIn("May backend tests: passed", out)
        self.assertIn(["npm", "test"], self.tools())

    def test_migrations_need_every_file_applied_once_and_a_quiet_second_run(self):
        cases = {
            "the ledger short of the files": (dict(), dict(applied="1")),
            "the ledger unreadable": (dict(), dict(applied=None)),
            "the first run's count differs": (dict(migrate=("applied 1 migration(s)", "schema up to date")), dict()),
            "the second run applied something": (dict(migrate=("applied 2 migration(s)", "applied 1 migration(s)")), dict()),
            "the owner role refused": (dict(owner=1), dict()),
            "the owner role timed out": (dict(owner=None), dict()),
        }
        for case, (fake, value) in cases.items():
            with self.subTest(case=case):
                self.calls = []
                out, error = self.run_checks(self.fake(**fake), self.value(**value))
                self.assertIn("May backend migrations: failed", out)
                self.assertEqual(error, "At least one May backend check did not pass")

    def test_only_numbered_migration_files_count(self):
        self.assertEqual(backend_unit.migration_files(self.root / "may-source/backend"), 2)

    def test_the_wrong_node_or_postgres_stops_before_anything_installs(self):
        for case, value, message in (
                ("node", self.value(node="v22.21.0"), "Node is not the pinned v22.22.0; nothing ran"),
                ("postgres", self.value(server="16.14"), "The Postgres service is not 16.15, or not reachable; nothing ran"),
                ("postgres 16.150", self.value(server="16.150"), "The Postgres service is not 16.15, or not reachable; nothing ran"),
                ("no postgres", self.value(server=None), "The Postgres service is not 16.15, or not reachable; nothing ran")):
            with self.subTest(case=case):
                self.calls = []
                out, error = self.run_checks(self.fake(), value)
                self.assertEqual(error, message)
                self.assertNotIn(["npm", "ci", "--no-audit"], self.tools())

    def test_a_pull_request_without_the_backend_lockfile_runs_nothing(self):
        (self.root / "may-source/backend/package-lock.json").unlink()
        out, error = self.run_checks(self.fake())
        self.assertEqual(error, "The pull request has no backend/package-lock.json; nothing ran")
        self.assertEqual(self.calls, [])

    def test_a_secret_in_the_step_refuses_before_any_tool_runs(self):
        for secret in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN", "MAY_APP_PR_COMMENT_TOKEN"):
            with self.subTest(secret=secret):
                self.calls = []
                with self.assertRaises(backend_unit.StoreError):
                    self.run_checks(self.fake(), environ=dict(self.environ, **{secret: "x"}))
                self.assertEqual(self.calls, [])

    def test_a_source_other_than_the_prepared_sha_is_refused(self):
        with patch.dict(os.environ, self.environ, clear=True), self.assertRaises(backend_unit.StoreError), \
                patch.object(backend_unit, "quiet", self.fake()):
            backend_unit.run_checks("b" * 40)
        self.assertEqual(self.calls, [])


class RecordTests(Fixture):
    def test_failing_tests_are_recorded_privately_and_do_not_fail_the_step(self):
        out, outcome, failed = self.record(self.fake(test_status=1, test_output=tap(self.root, passed=1, failed=2)))
        self.assertEqual(out.splitlines()[-1],
                         "May backend checks finished; any failures stay in this runner's private files for the next step")
        for private in (PRIVATE, MESSAGE, str(self.root)):
            self.assertNotIn(private, out)
        self.assertEqual(outcome, {"result": "failed", "tests": [3, 1, 2, 0], "migrations": [2, 2], "stages": {
            "toolchain": "passed", "install": "passed", "typecheck": "passed", "migrations": "passed", "tests": "failed"}})
        self.assertEqual(failed["total"], 2, "the file and suite that failed only through their tests are left out")
        self.assertEqual(failed["failures"][0], {
            "test": PRIVATE + " 0", "message": MESSAGE + " at backend/src/x.ts", "at": "backend/test/private.test.ts:12:3"})
        self.assertEqual(failed["tails"], {})

    def test_a_passing_run_is_recorded_as_passed(self):
        _, outcome, failed = self.record(self.fake())
        self.assertEqual(outcome["result"], "passed")
        self.assertEqual(failed, {"failures": [], "total": 0, "typecheck": [], "typecheck_total": 0, "tails": {}})

    def test_type_errors_are_recorded_under_backend(self):
        tsc = (f"src/private/x.ts(12,5): error TS2322: {TYPE_ERROR}\nsrc/y.ts(1,1): error TS1005: ';' expected.\n"
               "Found 2 errors.\n")
        _, outcome, failed = self.record(self.fake(typecheck=2, tsc_output=tsc))
        self.assertEqual(outcome["stages"]["typecheck"], "failed")
        self.assertEqual(failed["typecheck"][0], {"at": "backend/src/private/x.ts:12:5", "code": "TS2322", "message": TYPE_ERROR})
        self.assertEqual(failed["typecheck_total"], 2)
        self.assertNotIn("typecheck", failed["tails"], "parsed errors stand in for the log")

    def test_a_stage_with_nothing_more_specific_keeps_its_cleaned_last_lines(self):
        _, outcome, failed = self.record(self.fake(install=1))
        self.assertEqual(failed["tails"], {"install": ["npm ERR! private dependency trouble"]})
        self.calls = []
        for name in (backend_unit.OUTCOME, backend_unit.FAILURES):
            (self.root / "may-private" / name).unlink()
        noisy = "\n".join(f"line {n} {self.root}/may-source/backend/x ghp_{'A' * 36}" for n in range(60))
        _, _, failed = self.record(self.fake(test_status=1, test_output=noisy))
        self.assertEqual(len(failed["tails"]["tests"]), backend_unit.TAIL_LINES)
        self.assertEqual(failed["tails"]["tests"][-1], "line 59 backend/x [redacted]")

    def test_a_check_before_the_stages_still_fails_the_step_and_records_nothing(self):
        out, error = self.run_checks(self.fake(), self.value(node="v20.0.0"), record=True)
        self.assertEqual(error, "Node is not the pinned v22.22.0; nothing ran")
        self.assertFalse((self.root / "may-private" / backend_unit.OUTCOME).exists())

    def test_at_most_max_failures_are_kept_and_all_counted(self):
        with patch.object(backend_unit, "MAX_FAILURES", 3):
            _, _, failed = self.record(self.fake(test_status=1, test_output=tap(self.root, failed=5)))
        self.assertEqual((len(failed["failures"]), failed["total"]), (3, 5))


class ParseTests(unittest.TestCase):
    def test_counts_come_from_the_last_summary_lines(self):
        text = "# tests 99\n# pass 99\n# fail 0\n" + tap("/r", passed=2, failed=1, skipped=1)
        self.assertEqual(test_counts(text), (4, 2, 1, 1))
        spec = "ℹ tests 5\nℹ suites 1\nℹ pass 4\nℹ fail 0\nℹ cancelled 1\nℹ skipped 0\nℹ todo 0\nℹ duration_ms 12.5\n"
        self.assertEqual(test_counts(spec), (5, 4, 1, 0))

    def test_a_summary_without_tests_pass_and_fail_is_none(self):
        for text in ("", "# tests 3\n# pass 3\n", "# \\# tests 3\n# pass 3\n# fail 0\n", "# tests three\n# pass 3\n# fail 0\n"):
            with self.subTest(text=text):
                self.assertIsNone(test_counts(text))

    def test_the_spec_reporters_failing_list_is_read_when_there_is_no_tap(self):
        spec = ("✔ passes (0.3ms)\nℹ tests 2\nℹ pass 1\nℹ fail 1\n\n✖ failing tests:\n\n"
                f"test at backend/test/x.test.ts:4:1\n✖ {PRIVATE} (0.46ms)\n  AssertionError: {MESSAGE}\n")
        self.assertEqual(failing_tests(spec, []), ([{"test": PRIVATE, "message": "AssertionError: " + MESSAGE,
                                                     "at": "backend/test/x.test.ts:4:1"}], 1))

    def test_skipped_and_todo_results_are_not_failures(self):
        text = "ok 1 - a # SKIP\nnot ok 2 - b # TODO\n  ---\n  failureType: 'testCodeFailure'\n  ...\n"
        failures, total = failing_tests(text, [])
        self.assertEqual(total, 1)
        self.assertEqual(failures[0]["test"], "b")

    def test_type_errors_outside_the_tsc_shape_are_ignored(self):
        self.assertEqual(type_errors("error: something\nsrc/x.ts:1:1 - error TS1: y\n", []), ([], 0))


class CliTests(unittest.TestCase):
    def test_an_unprepared_runner_prints_only_a_fixed_line(self):
        tool = Path(backend_unit.__file__)
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", str(tool), "--source-sha", "a" * 40],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(), "May backend checks: Exact private checkout is not prepared")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def flagged(self, relative, old, new, message, case):
        self.tearDown()
        self.setUp()
        path = self.root / relative
        text = path.read_text()
        self.assertIn(old, text, "mutation anchor not found: " + case)
        path.write_text(text.replace(old, new, 1))
        found = static_check.problems(self.root)
        self.assertTrue(any(message in problem for problem in found), (case, found))

    def test_the_committed_workflow_and_tools_pass(self):
        self.assertEqual(static_check.backend_unit_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.backend_unit_print_problems(static_check.ROOT), [])

    def test_each_widening_is_flagged(self):
        name = "backend-unit-pr.yml: "
        secret = name + "a secret or env entry other than the check's PR token, the fetch's two source-read keys, the post's comment token and the dispatch's inputs"
        runs = name + "runs something other than the gate"
        shape = name + "is not the Linux backend run"
        service = name + "the Postgres service is not exactly backend-ci.yml's 16.15, pinned by digest"
        checks_step = "      - name: Run the backend checks without any credential, printing only pass or fail and counts\n        env:\n"
        node_step = "      - name: Install the pinned, checksum-verified Node\n"
        fetch_env = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        cases = {
            "a read key on the checks": (checks_step, checks_step + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the PR token on the checks": (checks_step, checks_step + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "GH_TOKEN on the checks": (checks_step, checks_step + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a read key on the Node install": (node_step, node_step + "        env:\n          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n", secret),
            "a comment token in the fetch": (fetch_env, fetch_env + "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n", secret),
            "a job-level env": ("    timeout-minutes: 20\n", "    timeout-minutes: 20\n    env:\n      CI: '1'\n", secret),
            "a secret in the service": ("          POSTGRES_HOST_AUTH_METHOD: trust\n",
                                        "          POSTGRES_HOST_AUTH_METHOD: trust\n          POSTGRES_PASSWORD: ${{ secrets.MAY_APP_READ_KEY }}\n", service),
            "a Postgres tag without its digest": ("postgres:16.15@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d",
                                                  "postgres:16.15", service),
            "another Postgres": ("postgres:16.15@", "postgres:17.2@", service),
            "a second service": ("      postgres:\n", "      redis:\n        image: redis:7\n      postgres:\n", service),
            "the log uploaded": ("        if: always()\n", "        if: always()\n      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n"
                                 "        with:\n          name: backend-ci-logs\n          path: ${{ runner.temp }}/may-private/\n", shape),
            "a step summary": ('--record-outcome\n', '--record-outcome >> "$GITHUB_STEP_SUMMARY"\n', shape),
            "the Node exported": ("tools/may_release/toolchain.py node\n", "tools/may_release/toolchain.py node --export\n", shape),
            "setup-node with an npm cache": ("      - name: Install the pinned, checksum-verified Node\n",
                                             "      - uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0\n"
                                             "        with:\n          node-version: '22.22.0'\n          cache: npm\n"
                                             "      - name: Install the pinned, checksum-verified Node\n", "uses an Actions cache"),
            "the log printed": ("      - name: Remove private source",
                                "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/backend-tests.log\"\n"
                                "      - name: Remove private source", runs),
            "the check without --check-pull": ('--pr-number "$MAY_PR_NUMBER" --check-pull\n', '--pr-number "$MAY_PR_NUMBER"\n', runs),
            "the checks failing the step": ("backend_unit.py --source-sha \"$MAY_SOURCE_SHA\" --record-outcome\n",
                                            "backend_unit.py --source-sha \"$MAY_SOURCE_SHA\"\n", runs),
            "an input interpolated into shell": ('backend_unit.py --source-sha "$MAY_SOURCE_SHA"',
                                                 "backend_unit.py --source-sha '${{ inputs.source_sha }}'", runs),
            "checks that may fail quietly": ("      - name: Run the backend checks without any credential, printing only pass or fail and counts\n",
                                             "      - name: Run the backend checks without any credential, printing only pass or fail and counts\n"
                                             "        continue-on-error: true\n", name + "sets a shell, defaults, a condition"),
            "a cleanup that is not always run": ("        if: always()\n", "", name + "backend's steps are not exactly"),
            "a store environment": ("    environment: ${{ needs.admission.outputs.environment }}\n",
                                    "    environment: may-android-playground-internal\n", name + "backend's environment is not the one admission chose"),
            "a pull_request trigger": ("on:\n  workflow_dispatch:\n", "on:\n  pull_request:\n  workflow_dispatch:\n",
                                       name + "is not dispatched with exactly pr_number and source_sha"),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: write\nconcurrency:",
                                 name + "permissions are not exactly read-only"),
            "the Android lane's concurrency group": ("  group: may-backend-unit-pr\n", "  group: may-android-unit-pr\n",
                                                     name + "does not run one at a time in its own concurrency group"),
            "a longer timeout": ("    timeout-minutes: 20\n", "    timeout-minutes: 360\n", name + "sets a shell, defaults"),
            "a macOS runner": ("  backend:\n    needs: admission\n    runs-on: ubuntu-24.04\n",
                               "  backend:\n    needs: admission\n    runs-on: macos-15\n", shape),
            "another run name": ("run-name: 'May backend checks for may-app #", "run-name: 'May backend for may-app #",
                                 name + "run-name is not exactly the pull request number and head SHA"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_tool_prints_only_fixed_lines_and_counts(self):
        tool = "tools/may_release/backend_unit.py"
        done = 'print("Every May backend check passed")'
        cases = {
            "a printed log": (done, done + "\n    print(log_text(Path('x')))", "prints something other than a fixed line"),
            "printed counts dict": (done, 'print(f"counts {counts}")', "prints something other than a fixed line"),
            "npm writing to this log": ("stdout=output, stderr=subprocess.STDOUT,\n                                    stdin",
                                        "stdin", "runs a subprocess that may inherit this log"),
            "sys.stdout": (done, done + "\n    sys.stdout.write(str(stages))", "writes to stdout or stderr other than"),
            "a formatted refusal": ('raise BackendTestsError("At least one May backend check did not pass")',
                                    'raise BackendTestsError("did not pass: " + str(stages))', "printed error message is not one string literal"),
            "an exit built from data": ('raise SystemExit("May backend checks failed; no private detail was published") from None',
                                        'raise SystemExit("failed: " + str(sys.exc_info()))', "exits with a message that is not a fixed literal"),
            "a step summary": ('NODE_VERSION = "v22.22.0"', 'NODE_VERSION = "v22.22.0"\nSUMMARY_FILE = "GITHUB_STEP_SUMMARY"',
                               "writes a GitHub Actions file or workflow command"),
            "the record printed": ('print("May backend checks finished; any failures stay in this runner\'s private files for the next step")',
                                   'print(json.dumps(stages))', "prints something other than a fixed line"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(tool, old, new, message, case)


if __name__ == "__main__":
    unittest.main()
