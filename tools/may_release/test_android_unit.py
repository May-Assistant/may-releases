"""May's Android unit suites at a may-app pull request head (android-unit-pr.yml, android_unit.py)."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import android_unit
from android_unit import UnitTestsError, report_counts, suite_result
import static_check

WORKFLOW = ".github/workflows/android-unit-pr.yml"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": os.environ.get("PATH", "")}
# Private words a report carries that must never reach the public log.
PRIVATE = "io.github.mayassistant.may.PrivateScreenTest"
MESSAGE = "expected private text but was other private text"


def junit(tests, failures=0, errors=0, skipped=0, name=PRIVATE):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="{name}" tests="{tests}" skipped="{skipped}" '
            f'failures="{failures}" errors="{errors}" timestamp="2026-10-18T00:00:00" hostname="runner" time="1.2">\n'
            f'  <testcase name="a private test" classname="{name}"><failure message="{MESSAGE}">{MESSAGE}</failure>'
            f'</testcase>\n</testsuite>\n')


class Fixture(unittest.TestCase):
    """A prepared runner: may-source (a git checkout with fetch-design-system.sh and the placed design system),
    may-private, and an SDK folder, under a temporary RUNNER_TEMP. Every tool is replaced by a fake `quiet`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        source = self.root / "may-source"
        (source / "android/scripts").mkdir(parents=True)
        (source / "android/scripts/fetch-design-system.sh").write_text("#!/usr/bin/env bash\nexit 99\n")
        (source / "android/build-deps/may-design-system").mkdir(parents=True)
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(source)] + argv, check=True, env=GIT_ENV, capture_output=True)
        self.sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, env=GIT_ENV,
                                  capture_output=True).stdout.decode().strip()
        (self.root / "may-private").mkdir()
        self.sdk = self.root / "sdk"
        (self.sdk / "cmdline-tools/latest/bin").mkdir(parents=True)
        (self.sdk / "cmdline-tools/latest/bin/sdkmanager").write_text("")
        self.jdk = self.root / "may-tools/jdk/jdk-21.0.12.1+1"
        self.environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
                        "ANDROID_HOME": str(self.sdk)}
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def reports(self, **suites):
        """Write each named suite's reports: suite=[junit text, ...]."""
        for name, texts in suites.items():
            folder = self.root / "may-source/android" / android_unit.SUITES[name][1]
            folder.mkdir(parents=True, exist_ok=True)
            for number, text in enumerate(texts):
                (folder / f"TEST-{PRIVATE}{number}.xml").write_text(text)

    def fake(self, gradle=0, design_system=0, suites=None, timeout=False):
        def quiet(argv, log, *, cwd=None, env=None, timeout_seconds=None, **_):
            self.calls.append((list(argv), cwd, dict(env or {})))
            if argv[0] == "bash":
                return design_system
            if argv[0] == "./gradlew":
                if timeout:
                    raise subprocess.TimeoutExpired(argv, 1)
                self.reports(**(suites if suites is not None else
                                {name: [junit(3)] for name in android_unit.SUITES}))
                return gradle
            raise AssertionError("unexpected tool " + argv[0])
        return quiet

    def run_suites(self, quiet, environ=None):
        out = io.StringIO()
        with patch.dict(os.environ, environ or self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(android_unit, "quiet", quiet), patch.object(android_unit, "install_sdk", lambda *a: None), \
                patch.object(android_unit, "pinned_java_home", lambda: self.jdk):
            try:
                android_unit.run_suites(self.sha)
            except UnitTestsError as error:
                return out.getvalue(), error.args[0]
        return out.getvalue(), None

    def gradle_calls(self):
        return [call for call in self.calls if call[0][0] == "./gradlew"]


class RunTests(Fixture):
    def test_every_suite_passing_prints_only_fixed_lines_and_counts(self):
        quiet = self.fake(suites={"core": [junit(3), junit(4, skipped=1)], "playground": [junit(5)],
                                  "production": [junit(2)]})
        out, error = self.run_suites(quiet)
        self.assertIsNone(error)
        self.assertEqual(out.splitlines(), [
            "May Android unit suite core: passed, 7 tests, 0 failed, 1 skipped",
            "May Android unit suite playground: passed, 5 tests, 0 failed, 0 skipped",
            "May Android unit suite production: passed, 2 tests, 0 failed, 0 skipped",
            "Every May Android unit suite passed"])
        for private in (PRIVATE, MESSAGE, "a private test", str(self.root)):
            self.assertNotIn(private, out)

    def test_the_design_system_check_runs_before_gradle_which_runs_all_three_suites_on_with_continue(self):
        self.run_suites(self.fake())
        self.assertEqual([call[0] for call in self.calls], [
            ["bash", "android/scripts/fetch-design-system.sh"],
            ["./gradlew", "--no-daemon", "--continue", ":core:test", ":app:testPlaygroundDebugUnitTest",
             ":app:testProductionDebugUnitTest"]])
        self.assertEqual(self.calls[0][1], self.root / "may-source")
        self.assertEqual(self.calls[1][1], self.root / "may-source/android")

    def test_gradle_gets_the_pinned_jdk_and_the_store_allowlist_only(self):
        environ = dict(self.environ, SOMETHING_ELSE="x", GH_TOKEN="t")
        self.run_suites(self.fake(), environ)
        env = self.gradle_calls()[0][2]
        self.assertEqual(env.get("JAVA_HOME"), str(self.jdk))
        self.assertTrue(env["PATH"].startswith(str(self.jdk / "bin") + os.pathsep))
        self.assertEqual(env["GRADLE_USER_HOME"], str(self.root / "may-gradle"))
        self.assertEqual(env["ANDROID_HOME"], str(self.sdk))
        self.assertNotIn("SOMETHING_ELSE", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_a_failure_or_an_error_fails_its_suite_and_the_job(self):
        for case, text in (("a failure", junit(4, failures=1)), ("an error", junit(4, errors=2))):
            with self.subTest(case=case):
                self.calls = []
                quiet = self.fake(suites={"core": [junit(3)], "playground": [text], "production": [junit(2)]}, gradle=1)
                shutil.rmtree(self.root / "may-source/android/app", ignore_errors=True)
                shutil.rmtree(self.root / "may-source/android/core", ignore_errors=True)
                out, error = self.run_suites(quiet)
                self.assertEqual(error, "At least one May Android unit suite did not pass")
                failed = 1 if case == "a failure" else 2
                self.assertIn(f"May Android unit suite playground: failed, 4 tests, {failed} failed, 0 skipped", out)
                self.assertNotIn("Every May Android unit suite passed", out)

    def test_a_suite_with_no_tests_fails(self):
        out, error = self.run_suites(self.fake(suites={"core": [junit(0)], "playground": [junit(5)],
                                                       "production": [junit(2)]}))
        self.assertEqual(error, "At least one May Android unit suite did not pass")
        self.assertIn("May Android unit suite core: failed, 0 tests, 0 failed, 0 skipped", out)

    def test_a_suite_that_left_no_report_did_not_run(self):
        out, error = self.run_suites(self.fake(gradle=1, suites={"core": [junit(3)], "playground": [junit(5)]}))
        self.assertEqual(error, "At least one May Android unit suite did not run")
        self.assertIn("May Android unit suite production: did not run (its build or Gradle stopped before a report)", out)

    def test_gradle_failing_after_every_suite_passed_still_fails(self):
        out, error = self.run_suites(self.fake(gradle=1))
        self.assertEqual(error, "Gradle failed although every suite reported; its log stays on this runner")
        self.assertNotIn("Every May Android unit suite passed", out)

    def test_gradle_that_does_not_finish_fails(self):
        out, error = self.run_suites(self.fake(timeout=True))
        self.assertEqual(error, "Gradle did not finish within 50 minutes")
        self.assertIn("May Android unit suite core: did not run", out)

    def test_committed_test_results_are_refused_before_gradle_runs(self):
        for name, (_, folder) in android_unit.SUITES.items():
            with self.subTest(suite=name):
                self.calls = []
                path = self.root / "may-source/android" / folder
                path.mkdir(parents=True)
                (path / "TEST-x.xml").write_text(junit(9))
                out, error = self.run_suites(self.fake())
                self.assertEqual(error, "The pull request carries test results of its own; no suite ran")
                self.assertEqual(self.gradle_calls(), [])
                shutil.rmtree(path)

    def test_a_refused_or_missing_design_system_check_runs_no_suite(self):
        out, error = self.run_suites(self.fake(design_system=1))
        self.assertEqual(error, "fetch-design-system.sh refused the pinned design system; no suite ran")
        self.assertEqual(self.gradle_calls(), [])
        (self.root / "may-source/android/scripts/fetch-design-system.sh").unlink()
        self.calls = []
        out, error = self.run_suites(self.fake())
        self.assertEqual(error, "The pull request has no android/scripts/fetch-design-system.sh; no suite ran")
        self.assertEqual(self.calls, [])

    def test_a_secret_in_the_step_refuses_before_any_tool_runs(self):
        for secret in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN", "MAY_APP_PR_COMMENT_TOKEN"):
            with self.subTest(secret=secret):
                self.calls = []
                with self.assertRaises(android_unit.StoreError):
                    self.run_suites(self.fake(), dict(self.environ, **{secret: "x"}))
                self.assertEqual(self.calls, [])

    def test_a_source_other_than_the_prepared_sha_is_refused(self):
        with patch.dict(os.environ, self.environ, clear=True), self.assertRaises(android_unit.StoreError), \
                patch.object(android_unit, "quiet", self.fake()):
            android_unit.run_suites("b" * 40)
        self.assertEqual(self.calls, [])


class ReportTests(unittest.TestCase):
    def test_counts_come_from_the_testsuite_tag_alone(self):
        self.assertEqual(report_counts(junit(7, failures=1, errors=2, skipped=3)),
                         {"tests": 7, "failures": 1, "errors": 2, "skipped": 3})

    def test_a_tag_without_exactly_each_plain_count_is_unreadable(self):
        good = junit(3)
        cases = {
            "no testsuite": "<?xml version='1.0'?><testsuites/>",
            "a count missing": good.replace(' errors="0"', ""),
            "a count twice": good.replace(' errors="0"', ' errors="0" errors="0"'),
            "a negative count": good.replace('tests="3"', 'tests="-3"'),
            "a word count": good.replace('tests="3"', 'tests="three"'),
            "a huge count": good.replace('tests="3"', 'tests="10000000"'),
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                self.assertIsNone(report_counts(text))

    def test_a_name_carrying_count_like_text_is_not_a_count(self):
        text = junit(3, name="x tests=&quot;99&quot; failures=&quot;0&quot;")
        self.assertEqual(report_counts(text)["tests"], 3)

    def test_a_linked_or_absent_report_folder_or_file_is_unreadable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertIsNone(suite_result(root / "absent"))
            (root / "empty").mkdir()
            self.assertIsNone(suite_result(root / "empty"))
            (root / "real").mkdir()
            (root / "real/TEST-a.xml").write_text(junit(2))
            self.assertEqual(suite_result(root / "real"), (2, 0, 0))
            os.symlink(root / "real", root / "linked")
            self.assertIsNone(suite_result(root / "linked"))
            (root / "file").mkdir()
            os.symlink(root / "real/TEST-a.xml", root / "file/TEST-b.xml")
            self.assertIsNone(suite_result(root / "file"))
            (root / "real/TEST-c.xml").write_text("not a report")
            self.assertIsNone(suite_result(root / "real"))


class SdkTests(unittest.TestCase):
    def test_installs_only_the_pinned_packages_and_retries_once(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / "cmdline-tools/latest/bin").mkdir(parents=True)
            (home / "cmdline-tools/latest/bin/sdkmanager").write_text("")
            installs, statuses = [], [1, 1]

            def quiet(argv, log, **_):
                installs.append(argv[1:])
                return statuses.pop(0)
            with patch.object(android_unit, "quiet", quiet), patch.object(android_unit, "sleep", lambda s: None), \
                    patch.object(android_unit.subprocess, "run"):
                with self.assertRaises(UnitTestsError) as caught:
                    android_unit.install_sdk(home, home / "log", {})
            self.assertEqual(caught.exception.args[0], "The Android SDK did not install after one retry; no suite ran")
            self.assertEqual(installs, [["--install", "platform-tools", "platforms;android-37.0", "build-tools;36.0.0"]] * 2)
            self.assertNotIn("emulator", str(android_unit.SDK_PACKAGES))

    def test_no_sdk_manager_is_refused(self):
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(UnitTestsError):
            android_unit.install_sdk(Path(raw), Path(raw) / "log", {})


class CliTests(unittest.TestCase):
    def test_an_unprepared_runner_prints_only_a_fixed_line(self):
        tool = Path(android_unit.__file__)
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run([sys.executable, "-I", "-S", "-B", str(tool), "--source-sha", "a" * 40],
                                    capture_output=True, env={"PATH": os.environ.get("PATH", ""), "RUNNER_TEMP": raw})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(), "May Android unit tests: Exact private checkout is not prepared")


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

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.android_unit_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.android_unit_print_problems(static_check.ROOT), [])

    def test_each_widening_is_flagged(self):
        name = "android-unit-pr.yml: "
        secret = name + "a secret or env entry other than the check's PR token, the fetch's two source-read keys, the post's comment token and the dispatch's inputs"
        runs = name + "runs something other than the gate"
        shape = name + "is not the Linux unit-test run"
        suites_step = "      - name: Run the unit suites without any credential, printing only pass or fail and counts\n        env:\n"
        jdk_step = "      - name: Install the pinned, checksum-verified JDK\n"
        fetch_env = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        cases = {
            "a read key on the suites": (suites_step, suites_step + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the PR token on the suites": (suites_step, suites_step + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "GH_TOKEN on the suites": (suites_step, suites_step + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "the PR number on the suites": (suites_step, suites_step + "          MAY_PR_NUMBER: ${{ inputs.pr_number }}\n", secret),
            "a read key on the JDK install": (jdk_step, jdk_step + "        env:\n          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n", secret),
            "the PR token in the fetch": (fetch_env, fetch_env + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "a comment token anywhere": (fetch_env, fetch_env + "          MAY_APP_PR_COMMENT_TOKEN: ${{ secrets.MAY_APP_PR_COMMENT_TOKEN }}\n", secret),
            "a job-level env": ("    timeout-minutes: 75\n", "    timeout-minutes: 75\n    env:\n      CI: '1'\n", secret),
            "the JDK exported": ("tools/may_release/toolchain.py jdk\n", "tools/may_release/toolchain.py jdk --export\n", shape),
            "an artifact": ("        if: always()\n", "        if: always()\n      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n"
                            "        with:\n          name: log\n          path: ${{ runner.temp }}/may-private/\n", shape),
            "a step summary": ('--source-sha "$MAY_SOURCE_SHA" --record-outcome\n',
                               '--source-sha "$MAY_SOURCE_SHA" --record-outcome >> "$GITHUB_STEP_SUMMARY"\n', shape),
            "the log printed": ("      - name: Remove private source",
                                "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/android-unit.log\"\n"
                                "      - name: Remove private source", runs),
            "the main-history fetch instead": ('--source-sha "$MAY_SOURCE_SHA" --pr-number "$MAY_PR_NUMBER"\n      - name: Run',
                                               '--source-sha "$MAY_SOURCE_SHA"\n      - name: Run', runs),
            "the iOS fetch": ("prepare.py --platform android", "prepare.py --platform ios", shape),
            "the check without --check-pull": ('--pr-number "$MAY_PR_NUMBER" --check-pull\n', '--pr-number "$MAY_PR_NUMBER"\n', runs),
            "an input interpolated into shell": ('android_unit.py --source-sha "$MAY_SOURCE_SHA"',
                                                 "android_unit.py --source-sha '${{ inputs.source_sha }}'", runs),
            "suites that may fail quietly": ("      - name: Run the unit suites without any credential, printing only pass or fail and counts\n",
                                             "      - name: Run the unit suites without any credential, printing only pass or fail and counts\n"
                                             "        continue-on-error: true\n", name + "sets a shell, defaults, a condition"),
            "a cleanup that is not always run": ("        if: always()\n", "", name + "unit's steps are not exactly"),
            "a store environment": ("    environment: ${{ needs.admission.outputs.environment }}\n",
                                    "    environment: may-android-playground-internal\n", name + "unit's environment is not the one admission chose"),
            "a push trigger": ("on:\n  workflow_dispatch:\n", "on:\n  push:\n  workflow_dispatch:\n",
                               name + "is not dispatched with exactly pr_number and source_sha"),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: write\nconcurrency:",
                                 name + "permissions are not exactly read-only"),
            "a shared concurrency group": ("  group: may-android-unit-pr\n", "  group: may-native-compile-pr\n",
                                           name + "does not run one at a time in its own concurrency group"),
            "a longer timeout": ("    timeout-minutes: 75\n", "    timeout-minutes: 360\n", name + "sets a shell, defaults"),
            "a macOS runner": ("  unit:\n    needs: admission\n    runs-on: ubuntu-24.04\n",
                               "  unit:\n    needs: admission\n    runs-on: macos-15\n", shape),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(WORKFLOW, old, new, message, case)

    def test_the_tool_prints_only_fixed_lines_and_counts(self):
        tool = "tools/may_release/android_unit.py"
        done = 'print("Every May Android unit suite passed")'
        cases = {
            "a printed log": (done, done + "\n    print(log.read_text())", "prints something other than a fixed line"),
            "a printed report": (done, 'print(f"report {results}")', "prints something other than a fixed line"),
            "Gradle writing to this log": ("stdout=output, stderr=subprocess.STDOUT,\n                                stdin",
                                           "stdin", "runs a subprocess that may inherit this log"),
            "sys.stdout": (done, done + "\n    sys.stdout.write(str(results))", "writes to stdout or stderr other than"),
            "a formatted refusal": ('raise UnitTestsError("At least one May Android unit suite did not run")',
                                    'raise UnitTestsError("did not run: " + str(results))', "printed error message is not one string literal"),
            "an exit built from data": ('raise SystemExit("May Android unit tests failed; no private detail was published") from None',
                                        'raise SystemExit("failed: " + str(sys.exc_info()))', "exits with a message that is not a fixed literal"),
            "a step summary": ('FETCH_DESIGN_SYSTEM = "android/scripts/fetch-design-system.sh"',
                               'FETCH_DESIGN_SYSTEM = "android/scripts/fetch-design-system.sh"\nSUMMARY = "GITHUB_STEP_SUMMARY"',
                               "writes a GitHub Actions file or workflow command"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.flagged(tool, old, new, message, case)


if __name__ == "__main__":
    unittest.main()
