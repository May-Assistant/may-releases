"""May's web checks at a may-app pull request head (web-tests-pr.yml, web_tests.py)."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import static_check
import web_tests
from web_tests import WebTestsError, failing_tests, test_counts, type_errors

WORKFLOW = ".github/workflows/web-tests-pr.yml"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": os.environ.get("PATH", "")}
# Private words the web app's output carries that must never reach the public log.
PRIVATE = "PRIVATE-WEB-TEST-NAME-5e2a"
MESSAGE = "PRIVATE-WEB-ASSERTION-31c7"
TYPE_ERROR = "PRIVATE-WEB-TYPE-ERROR-8d04"


def vitest(root, passed=3, failed=0, skipped=0, todo=0, broken_file=False):
    """vitest's json reporter output: one file with the given results, and optionally one file that did not load."""
    name = f"{root}/may-source/web/src/private.test.tsx"
    results = [{"status": "passed", "fullName": f"passes {n}", "failureMessages": []} for n in range(passed)]
    results += [{"status": "failed", "fullName": f"{PRIVATE} {n}",
                 "failureMessages": [f"AssertionError: {MESSAGE} at {root}/may-source/web/src/x.ts\n    at stack"]}
                for n in range(failed)]
    results += [{"status": "skipped", "fullName": "skipped", "failureMessages": []} for _ in range(skipped)]
    results += [{"status": "todo", "fullName": "todo", "failureMessages": []} for _ in range(todo)]
    files = [{"name": name, "status": "failed" if failed else "passed", "message": "", "assertionResults": results}]
    if broken_file:
        files.append({"name": f"{root}/may-source/web/src/broken.test.ts", "status": "failed",
                      "message": f"Failed to load {MESSAGE}", "assertionResults": []})
    return {"numTotalTests": passed + failed + skipped + todo, "numPassedTests": passed, "numFailedTests": failed,
            "numPendingTests": skipped, "numTodoTests": todo, "success": not failed and not broken_file,
            "testResults": files}


class Fixture(unittest.TestCase):
    """A prepared runner: may-source (a git checkout with web/ and both placed design systems), may-private, and the
    pinned Node's bin folder, under a temporary RUNNER_TEMP. Every tool is replaced by a fake `quiet` and `value`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        source = self.root / "may-source"
        (source / "web").mkdir(parents=True)
        (source / "web/package.json").write_text("{}\n")
        (source / "web/package-lock.json").write_text("{}\n")
        (source / "android/build-deps/may-design-system").mkdir(parents=True)
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(source)] + argv, check=True, env=GIT_ENV, capture_output=True)
        self.sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, env=GIT_ENV,
                                  capture_output=True).stdout.decode().strip()
        (source / "web/build-deps/may-design-system/.git").mkdir(parents=True)
        (self.root / "may-private").mkdir()
        self.node = self.root / "may-tools/node/node-v22.22.0-linux-x64/bin"
        self.environ = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "HOME": str(self.root)}
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def fake(self, ds=0, install=0, test_status=0, report=None, build=0, build_output="", test_timeout=False,
             write_report=True):
        def quiet(argv, log, *, cwd=None, env=None, timeout=None):
            self.calls.append((list(argv), cwd, dict(env or {})))

            def write(text):
                with log.open("a") as stream:
                    stream.write(text)
            if argv[:2] == ["npm", "--version"]:
                return 0
            if argv[:3] == ["npm", "run", "fetch-ds"]:
                write("private design system trouble\n" if ds else "built\n")
                return ds
            if argv[:2] == ["npm", "ci"]:
                write("npm ERR! private dependency trouble\n" if install else "added 12 packages\n")
                return install
            if argv[:4] == ["npx", "--no-install", "vitest", "run"]:
                write(f"{PRIVATE} output\n")
                out = next(a for a in argv if a.startswith("--outputFile.json="))[len("--outputFile.json="):]
                if write_report:
                    Path(out).write_text(json.dumps(report if report is not None else vitest(self.root)))
                return None if test_timeout else test_status
            if argv[:3] == ["npm", "run", "build"]:
                write(build_output)
                return build
            raise AssertionError("unexpected tool " + argv[0])
        return quiet

    def value(self, node="v22.22.0"):
        def answer(argv, env):
            self.calls.append((list(argv), None, dict(env or {})))
            if argv == ["node", "--version"]:
                return node
            raise AssertionError("unexpected value " + argv[0])
        return answer

    def run_checks(self, quiet, value=None, environ=None, record=False):
        out = io.StringIO()
        with patch.dict(os.environ, environ or self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(web_tests, "quiet", quiet), patch.object(web_tests, "value", value or self.value()), \
                patch.object(web_tests, "pinned_node_bin", lambda: self.node):
            try:
                web_tests.run_checks(self.sha, record)
            except WebTestsError as error:
                return out.getvalue(), error.args[0]
        return out.getvalue(), None

    def record(self, quiet, value=None):
        out, error = self.run_checks(quiet, value, record=True)
        self.assertIsNone(error)
        private = self.root / "may-private"
        return (out, json.loads((private / web_tests.OUTCOME).read_text()),
                json.loads((private / web_tests.FAILURES).read_text()))

    def tools(self):
        return [call[0][:3] for call in self.calls]


class RunTests(Fixture):
    def test_every_stage_passing_prints_only_fixed_lines_and_counts(self):
        out, error = self.run_checks(self.fake(report=vitest(self.root, passed=5, skipped=1, todo=2)))
        self.assertIsNone(error)
        self.assertEqual(out.splitlines(), [
            "May web toolchain: passed", "May web design-system: passed", "May web install: passed",
            "May web tests: passed", "May web build: passed",
            "May web tests: 8 tests, 5 passed, 0 failed, 3 skipped", "Every May web check passed"])
        for private in (PRIVATE, MESSAGE, "private.test.tsx", str(self.root)):
            self.assertNotIn(private, out)

    def test_the_stages_run_in_order_in_web(self):
        self.run_checks(self.fake())
        web = self.root / "may-source/web"
        ran = [(call[0][:3], call[1]) for call in self.calls if call[0][0] in ("npm", "npx")]
        self.assertEqual(ran, [(["npm", "--version"], None), (["npm", "run", "fetch-ds"], web),
                               (["npm", "ci", "--no-audit"], web), (["npx", "--no-install", "vitest"], web),
                               (["npm", "run", "build"], web)])

    def test_the_tools_get_the_pinned_node_and_no_secret(self):
        environ = dict(self.environ, SOMETHING_ELSE="x")
        self.run_checks(self.fake(), environ=environ)
        for argv, _, env in self.calls:
            self.assertTrue(env["PATH"].startswith(str(self.node) + os.pathsep))
            self.assertNotIn("SOMETHING_ELSE", env)
            self.assertEqual(env["npm_config_cache"], str(self.root / "may-npm"))

    def test_vitests_report_goes_to_a_private_file(self):
        self.run_checks(self.fake())
        argv = next(call[0] for call in self.calls if call[0][0] == "npx")
        self.assertIn("--outputFile.json=" + str(self.root / "may-private" / web_tests.REPORT), argv)

    def test_a_failing_test_fails_the_tests_stage_and_the_job_and_still_builds(self):
        out, error = self.run_checks(self.fake(test_status=1, report=vitest(self.root, passed=2, failed=1)))
        self.assertEqual(error, "At least one May web check did not pass")
        self.assertIn("May web tests: failed", out)
        self.assertIn("May web build: passed", out)
        self.assertIn("May web tests: 3 tests, 2 passed, 1 failed, 0 skipped", out)
        self.assertNotIn(PRIVATE, out)

    def test_a_file_that_did_not_load_fails_even_with_every_test_passing(self):
        out, error = self.run_checks(self.fake(report=vitest(self.root, broken_file=True)))
        self.assertIsNotNone(error)
        self.assertIn("May web tests: failed", out)

    def test_no_tests_at_all_fails(self):
        out, error = self.run_checks(self.fake(report=vitest(self.root, passed=0)))
        self.assertIsNotNone(error)
        self.assertIn("May web tests: failed", out)

    def test_vitest_exiting_non_zero_fails_even_when_its_report_shows_no_failure(self):
        out, error = self.run_checks(self.fake(test_status=1))
        self.assertIsNotNone(error)
        self.assertIn("May web tests: failed", out)

    def test_no_report_is_said_in_a_fixed_line(self):
        out, error = self.run_checks(self.fake(test_status=1, write_report=False))
        self.assertIsNotNone(error)
        self.assertIn("May web tests: no test summary (vitest stopped before it reported)", out)

    def test_vitest_that_does_not_finish_fails(self):
        out, error = self.run_checks(self.fake(test_timeout=True))
        self.assertIsNotNone(error)
        self.assertIn("May web tests: did not finish in time", out)

    def test_a_failed_design_system_stops_every_later_stage(self):
        out, error = self.run_checks(self.fake(ds=1))
        self.assertIsNotNone(error)
        self.assertNotIn(["npm", "ci", "--no-audit"], self.tools())
        for stage in ("install", "tests", "build"):
            self.assertIn(f"May web {stage}: did not run (an earlier stage stopped it)", out)

    def test_a_failed_install_stops_the_tests_and_the_build(self):
        out, error = self.run_checks(self.fake(install=1))
        self.assertIsNotNone(error)
        self.assertNotIn(["npx", "--no-install", "vitest"], self.tools())
        self.assertIn("May web build: did not run (an earlier stage stopped it)", out)

    def test_a_failed_build_fails_the_job(self):
        out, error = self.run_checks(self.fake(build=2))
        self.assertIsNotNone(error)
        self.assertIn("May web build: failed", out)

    def test_the_wrong_node_stops_before_anything_installs(self):
        out, error = self.run_checks(self.fake(), value=self.value(node="v24.0.0"))
        self.assertEqual(error, "Node is not the pinned v22.22.0; nothing ran")
        self.assertEqual([c for c in self.tools() if c[0] in ("npm", "npx")], [])

    def test_a_pull_request_without_a_prepared_web_design_system_runs_nothing(self):
        (self.root / "may-source/web/build-deps/may-design-system/.git").rmdir()
        out, error = self.run_checks(self.fake())
        self.assertEqual(error, "The pull request has no prepared web/ and design system; nothing ran")
        self.assertEqual(self.calls, [])

    def test_a_secret_in_the_step_refuses_before_any_tool_runs(self):
        with self.assertRaises(Exception):
            self.run_checks(self.fake(), environ=dict(self.environ, MAY_APP_READ_KEY="k"))
        self.assertEqual(self.calls, [])


class RecordTests(Fixture):
    def test_failing_tests_are_recorded_privately_and_do_not_fail_the_step(self):
        out, outcome, failed = self.record(self.fake(test_status=1, report=vitest(self.root, passed=1, failed=2,
                                                                                  broken_file=True)))
        self.assertEqual(outcome["result"], "failed")
        self.assertEqual(outcome["stages"]["tests"], "failed")
        self.assertEqual(outcome["tests"], [3, 1, 2, 0])
        self.assertEqual(failed["total"], 3)
        self.assertEqual([f["test"] for f in failed["failures"]], [f"{PRIVATE} 0", f"{PRIVATE} 1", "(the file did not run)"])
        self.assertEqual(failed["failures"][0]["at"], "web/src/private.test.tsx")
        self.assertTrue(failed["failures"][0]["message"].startswith("AssertionError: " + MESSAGE))
        self.assertNotIn(str(self.root), json.dumps(failed))
        self.assertNotIn(PRIVATE, out)

    def test_a_passing_run_is_recorded_as_passed(self):
        _, outcome, failed = self.record(self.fake())
        self.assertEqual(outcome, {"result": "passed", "stages": dict.fromkeys(web_tests.STAGES, "passed"),
                                   "tests": [3, 3, 0, 0]})
        self.assertEqual(failed["tails"], {})

    def test_type_errors_from_the_build_are_recorded_under_web(self):
        _, outcome, failed = self.record(self.fake(build=2, build_output=f"src/a.tsx(3,7): error TS2322: {TYPE_ERROR}\n"))
        self.assertEqual(failed["typecheck"], [{"at": "web/src/a.tsx:3:7", "code": "TS2322", "message": TYPE_ERROR}])
        self.assertNotIn("build", failed["tails"])

    def test_a_stage_with_nothing_more_specific_keeps_its_cleaned_last_lines(self):
        _, _, failed = self.record(self.fake(install=1))
        self.assertEqual(failed["tails"], {"install": ["npm ERR! private dependency trouble"]})


class ParseTests(unittest.TestCase):
    def test_counts_and_skips(self):
        self.assertEqual(test_counts(vitest("/r", passed=2, failed=1, skipped=3, todo=1)), (7, 2, 1, 4))
        self.assertIsNone(test_counts(None))

    def test_a_report_without_its_counts_is_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "r.json"
            for content in ("not json", "[]", json.dumps({"numTotalTests": "3"})):
                path.write_text(content)
                self.assertIsNone(web_tests.vitest_report(path))
            path.write_text(json.dumps(vitest("/r")))
            self.assertIsNotNone(web_tests.vitest_report(path))

    def test_at_most_max_failures_are_kept_and_all_counted(self):
        failures, total = failing_tests(vitest("/r", passed=0, failed=web_tests.MAX_FAILURES + 5), Path("/r/may-source"),
                                        ["/r/may-source"])
        self.assertEqual((len(failures), total), (web_tests.MAX_FAILURES, web_tests.MAX_FAILURES + 5))

    def test_type_errors_outside_the_tsc_shape_are_ignored(self):
        self.assertEqual(type_errors("vite v8 building\nerror during build\n", []), ([], 0))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.text = (self.root / WORKFLOW).read_text()

    def test_the_committed_workflow_and_tools_pass(self):
        self.assertEqual(static_check.web_tests_workflow_problems(self.text), [])
        self.assertEqual(static_check.web_tests_print_problems(self.root), [])

    def test_each_widening_is_flagged(self):
        checks = 'MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n        run: python3 -I -S -B tools/may_release/web_tests.py'
        widened = {
            "a secret on the checks": self.text.replace(
                checks, 'MAY_SOURCE_SHA: ${{ inputs.source_sha }}\n          X: ${{ secrets.MAY_APP_READ_KEY }}\n'
                        '        run: python3 -I -S -B tools/may_release/web_tests.py'),
            "an artifact": self.text + "      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02\n",
            "the native fetch": self.text.replace(" --web\n", "\n"),
            "a longer timeout": self.text.replace("timeout-minutes: 50", "timeout-minutes: 90"),
            "another concurrency group": self.text.replace("group: may-web-tests", "group: may-backend-unit-pr"),
            "a step summary": self.text.replace('--record-outcome\n', '--record-outcome >> "$GITHUB_STEP_SUMMARY"\n'),
            "inputs in shell": self.text.replace('--source-sha "$MAY_SOURCE_SHA" --record-outcome',
                                                 '--source-sha "${{ inputs.source_sha }}" --record-outcome'),
            "no cleanup": self.text.replace("        if: always()\n", ""),
        }
        for what, text in widened.items():
            with self.subTest(what):
                self.assertNotEqual(text, self.text)
                self.assertTrue(static_check.web_tests_workflow_problems(text))


if __name__ == "__main__":
    unittest.main()
