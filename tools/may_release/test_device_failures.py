"""Smoke run 39671911872's two lessons (device_agent.py, device_tests.py): the iOS job's macOS runner needs the
darwin-arm64 Claude Code package, and an unexpected failure prints where it happened (a fixed stage, the exception's
class and an errno), never its text. Everything is faked; nothing here reaches the network."""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import device_agent
import device_tests
from device_tests import DeviceTestsError
from test_device_agent import OAUTH, fake_cli_package
from test_device_smoke import NO_STAGING, Workspace

TOOL = Path(device_tests.__file__)


def digest(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class PackageTests(unittest.TestCase):
    def test_macos_takes_the_darwin_arm64_package_of_the_same_release(self):
        self.assertEqual(device_agent.package("darwin"), (
            "https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.296.tgz",
            "sha512-vqtx3kS3r1hNLMk+N8JMG5J4kcLHIbI3t/NSIdcFQZrRVCFA6NtcxJyn/B+Sf3GxJaFJOCEfoBQXsFo10+Oj9A=="))
        self.assertEqual(device_agent.package("linux"), (device_agent.CLI_URL, device_agent.CLI_SHA512))
        self.assertIn("linux-x64", device_agent.CLI_URL)

    def test_install_on_macos_fetches_and_checks_the_darwin_package(self):
        package = fake_cli_package()
        fetched = []
        with tempfile.TemporaryDirectory() as directory, patch("device_agent.DARWIN_SHA512", digest(package)):
            try:
                binary = device_agent.install(Path(directory), fetch=lambda url: fetched.append(url) or package,
                                              system="darwin")
            except device_agent.AgentError:
                self.fail("the darwin package was not checked against its own pin")
            self.assertTrue(binary.is_file())
        self.assertEqual(fetched, [device_agent.DARWIN_URL])
        with tempfile.TemporaryDirectory() as directory, patch("device_agent.CLI_SHA512", digest(package)), \
                self.assertRaises(device_agent.AgentError):
            device_agent.install(Path(directory), fetch=lambda url: package, system="darwin")


class FailureLineTests(Workspace):
    def test_an_unexpected_failure_names_its_stage_class_and_errno_alone(self):
        def broken(*args, **kwargs):
            raise OSError(8, "Exec format error: /private/runner/may-claude/claude")

        class Emulator:
            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        try:
            with self.environ(NO_STAGING), patch("device_tests.boot", return_value=("/sdk/adb", Emulator(), {})), \
                    patch("device_tests.maestro", return_value=Path("/fake/maestro")), \
                    patch("device_tests.flows.run_private", return_value=0), \
                    patch("device_tests.flows.sdk", return_value=Path("/sdk")), \
                    patch("device_tests.Recorder"), patch("device_tests.head_revision", return_value="a" * 40):
                device_tests.run_scenarios("smoke", agent=broken, install=lambda root: root / "claude")
        except OSError as error:
            failure = error
        except (AttributeError, DeviceTestsError) as error:
            self.fail("the run stopped before the agent: " + type(error).__name__)
        else:
            self.fail("the agent's failure did not stop the run")
        with patch("sys.stdout") as out:
            device_tests.failure_line(failure)
        line = "".join(call.args[0] for call in out.write.call_args_list if call.args)
        self.assertEqual(line.strip(), "May device tests failed at smoke: the agent (OSError, errno 8)")
        self.assertNotIn("private", line)

    def test_the_command_line_prints_the_line_then_its_fixed_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "may-private").mkdir()
            report = root / device_tests.OUT / "20261021-aaaaaaaaaaaa" / "android"
            report.mkdir(parents=True)
            (report / "report.json").write_text("{not json /private/path")
            done = subprocess.run([sys.executable, "-I", "-S", "-B", str(TOOL), "judge"], capture_output=True,
                                  text=True, env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": directory})
        self.assertEqual(done.stdout.strip(), "May device tests failed at starting (JSONDecodeError)")
        self.assertEqual(done.stderr.strip(), "May device tests failed; private logs and evidence were not published")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("private/path", done.stdout + done.stderr)



class AndroidLaunchTests(unittest.TestCase):
    """Smoke run 39677407348 failed at "smoke: launching the app": the start now names MainActivity, retries once."""

    def launch(self, token, codes):
        calls, slept = [], []

        def private(argv, log, *, cwd=None, env=None, check=True, timeout=None):
            calls.append(list(argv))
            if check:
                self.fail("a start that fails must not raise a tool error: " + " ".join(argv[2:4]))
            return codes.pop(0) if "start" in argv else 0

        with patch("device_tests.flows.run_private", side_effect=private), \
                patch("device_tests.flows.sleep", side_effect=slept.append):
            try:
                device_tests.launch("/sdk/adb", {}, token, Path("/dev/null"))
                raised = None
            except DeviceTestsError as error:
                raised = error.args[0]
        return [argv for argv in calls if "start" in argv], slept, raised

    def test_the_start_names_main_activity_with_or_without_a_session(self):
        starts, slept, raised = self.launch("token-" + "x" * 30, [0])
        self.assertEqual(starts, [["/sdk/adb", "shell", "am", "start", "-W", "-n",
                                   "io.github.mayassistant.may.playground/io.github.mayassistant.may.MainActivity",
                                   "--es", "may_test_session", "token-" + "x" * 30]])
        self.assertEqual((slept, raised), ([], None))
        starts, _, _ = self.launch("", [0])
        self.assertEqual(starts[0][-2:], ["-n", device_tests.APP_ID + "/" + device_tests.ACTIVITY])

    def test_one_retry_then_a_readable_stop(self):
        starts, slept, raised = self.launch("", [1, 0])
        self.assertEqual((len(starts), slept, raised), (2, [10], None))
        starts, slept, raised = self.launch("", [1, 1])
        self.assertEqual((len(starts), slept), (2, [10]))
        self.assertEqual(raised, "May Playground did not start on the emulator after one retry")

if __name__ == "__main__":
    unittest.main()
