import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tempfile
from pathlib import Path
from unittest import mock

import flows
from flows import FlowsError, admit, environment_exists, overlay, parse_instrumentation
from gate import GateDenial
from store import StoreError

SHA = "a" * 40
OTHER = "b" * 40


def status(cls, test, code, stack=None):
    lines = ["INSTRUMENTATION_STATUS: class=" + cls, "INSTRUMENTATION_STATUS: test=" + test]
    if stack:
        lines.append("INSTRUMENTATION_STATUS: stack=" + stack)
    lines.append("INSTRUMENTATION_STATUS_CODE: " + str(code))
    return "\n".join(lines)


class AdmitTest(unittest.TestCase):
    def test_admits_a_dispatch_from_main_with_flows_from_main_or_a_branch(self):
        for from_branch in ("false", "true"):
            admit("May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch", SHA, OTHER, "all", from_branch)

    def test_refuses_flows_from_branch_other_than_true_or_false(self):
        for value in ("", "yes", "True", None):
            with self.assertRaises(FlowsError):
                admit("May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch", SHA, OTHER, "all", value)

    def test_refuses_another_branch_event_or_repository(self):
        for repo, ref, event in (("May-Assistant/may-releases", "refs/heads/claude/emulator-flows", "workflow_dispatch"),
                                 ("May-Assistant/may-releases", "refs/heads/feature", "workflow_dispatch"),
                                 ("May-Assistant/may-releases", "refs/heads/main", "push"),
                                 ("someone/may-releases", "refs/heads/main", "workflow_dispatch")):
            with self.assertRaises(FlowsError):
                admit(repo, ref, event, SHA, OTHER, "all", "false")

    def test_refuses_inexact_shas_and_unknown_flows(self):
        for source, flows_sha, selection in ((SHA[:7], OTHER, "all"), (SHA, "main", "all"),
                                             (SHA.upper(), OTHER, "all"), (SHA, OTHER, "everything")):
            with self.assertRaises(FlowsError):
                admit("May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch", source, flows_sha, selection, "false")


class EnvironmentTest(unittest.TestCase):
    def test_passes_when_the_environment_exists(self):
        asked = []
        environment_exists("token", get=lambda path, token: asked.append(path) or {"name": "may-flows"})
        self.assertEqual(asked, ["environments/may-flows"])

    def test_refuses_a_missing_environment_or_token(self):
        def missing(path, token):
            raise GateDenial("Protected environment does not exist; it must never be auto-created")
        with self.assertRaises(FlowsError):
            environment_exists("token", get=missing)
        with self.assertRaises(FlowsError):
            environment_exists("", get=lambda path, token: {})


class OverlayTest(unittest.TestCase):
    def run_overlay(self, from_branch, environ):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "may-source/android").mkdir(parents=True)
            (root / "may-private").mkdir()

            def fake_fetch(dest, remote, sha, key, private, label, main_history=False):
                calls.append((sha, label, main_history))
                (dest / "android/flows").mkdir(parents=True)
                (dest / "android/flows/flows.init.gradle").write_text("// flows")

            with mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch.object(flows, "runner_root", return_value=root), \
                    mock.patch.object(flows, "prepared", return_value=(root / "may-source", root / "may-private")), \
                    mock.patch.object(flows, "fetch", side_effect=fake_fetch):
                overlay(SHA, OTHER, from_branch)
            self.assertTrue((root / "may-source/android/flows/flows.init.gradle").is_file())
        return calls

    def test_flows_come_from_main_history_unless_from_branch(self):
        self.assertEqual(self.run_overlay("false", {"MAY_APP_READ_KEY": "k"}), [(OTHER, "flows", True)])
        self.assertEqual(self.run_overlay("true", {"MAY_APP_READ_KEY": "k"}), [(OTHER, "flows", False)])

    def test_the_overlay_step_refuses_any_other_secret(self):
        with self.assertRaises(StoreError):
            self.run_overlay("false", {"MAY_APP_READ_KEY": "k", "MAY_DS_READ_KEY": "d"})


class InstallSdkTest(unittest.TestCase):
    def install(self, outcomes):
        calls, waits = [], []

        def fake_run(argv, log, *, env=None, check=True, timeout=None, **kwargs):
            if "--install" in argv:
                calls.append(timeout)
                outcome = outcomes[len(calls) - 1]
                if outcome == "timeout":
                    raise flows.subprocess.TimeoutExpired(argv, timeout)
                return outcome
            return 0

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "cmdline-tools/latest/bin").mkdir(parents=True)
            (home / "cmdline-tools/latest/bin/sdkmanager").write_text("")
            with mock.patch.object(flows, "run_private", side_effect=fake_run), \
                    mock.patch.object(flows, "sleep", side_effect=waits.append), \
                    mock.patch.object(flows.subprocess, "run"):
                try:
                    flows.install_sdk(home, home / "log", {})
                    return len(calls), waits, None
                except FlowsError as error:
                    return len(calls), waits, error

    def test_a_first_install_failure_is_retried_once(self):
        self.assertEqual(self.install([1, 0]), (2, [flows.SDK_RETRY_SECONDS], None))

    def test_a_first_success_does_not_retry(self):
        self.assertEqual(self.install([0]), (1, [], None))

    def test_each_attempt_has_a_timeout_and_a_timed_out_attempt_is_retried(self):
        self.assertEqual(self.install(["timeout", 0]), (2, [flows.SDK_RETRY_SECONDS], None))
        calls = []
        with mock.patch.object(flows, "run_private", side_effect=lambda argv, log, **kw: calls.append(kw.get("timeout")) or 0), \
                tempfile.TemporaryDirectory() as temp, mock.patch.object(flows.subprocess, "run"):
            home = Path(temp)
            (home / "cmdline-tools/latest/bin").mkdir(parents=True)
            (home / "cmdline-tools/latest/bin/sdkmanager").write_text("")
            flows.install_sdk(home, home / "log", {})
        self.assertEqual(calls, [flows.SDK_INSTALL_SECONDS])

    def test_two_timeouts_fail_the_run(self):
        count, waits, error = self.install(["timeout", "timeout"])
        self.assertEqual(count, 2)
        self.assertIsInstance(error, FlowsError)

    def test_a_second_failure_fails_the_run(self):
        count, waits, error = self.install([1, 1])
        self.assertEqual((count, waits), (2, [flows.SDK_RETRY_SECONDS]))
        self.assertIsInstance(error, FlowsError)


class ParseTest(unittest.TestCase):
    PKG = "io.github.mayassistant.may.flows."

    def test_reports_each_test_in_fixed_words_only(self):
        text = "\n".join([
            status(self.PKG + "ChatReplyFlow", "maysReplyComesIntoView", 1),
            status(self.PKG + "ChatReplyFlow", "maysReplyComesIntoView", 0),
            status(self.PKG + "TaskDetailFlow", "theProjectMenuStaysClear", 1),
            status(self.PKG + "TaskDetailFlow", "theProjectMenuStaysClear", -2,
                   "java.lang.AssertionError: the project menu (Book the meeting room) is inside"),
            "\tat org.junit.Assert.fail(Assert.java:89)",
            status(self.PKG + "TaskDetailFlow", "theDayPicker", -1,
                   "io.github.mayassistant.may.core.MayApiException: model_unavailable"),
            status(self.PKG + "EmptyStatesFlow", "chats", -3),
            "INSTRUMENTATION_CODE: -1",
        ])
        results, completed = parse_instrumentation(text)
        self.assertTrue(completed)
        self.assertEqual(results, [
            ("ChatReplyFlow test 1", "pass"),
            ("TaskDetailFlow test 1", "fail (assertion)"),
            ("TaskDetailFlow test 2", "fail (an exception, not an assertion)"),
            ("EmptyStatesFlow test 1", "ignored"),
        ])

    def test_a_malformed_status_code_is_skipped_not_a_crash(self):
        text = "\n".join([status(self.PKG + "ChatReplyFlow", "a", "--1"), status(self.PKG + "ChatReplyFlow", "a", "- 2"),
                          status(self.PKG + "ChatReplyFlow", "a", 0), "INSTRUMENTATION_CODE: -1"])
        self.assertEqual(parse_instrumentation(text), ([("ChatReplyFlow test 1", "pass")], True))

    def test_nothing_reported_by_the_device_reaches_the_public_lines(self):
        private = ["maysReplyComesIntoView", "Book the meeting room", "MayApiException", "mayassistant.may.core",
                   "secretTestName", "com.example.Leak", "Process crashed"]
        text = "\n".join([
            status(self.PKG + "ChatReplyFlow", "maysReplyComesIntoView", -2, "java.lang.AssertionError: Book the meeting room"),
            status(self.PKG + "ChatReplyFlow", "secretTestName", -2, "io.github.mayassistant.may.core.MayApiException: x"),
            status("com.example.Leak", "secretTestName", 0),
            "INSTRUMENTATION_STATUS: stream=Book the meeting room",
            "INSTRUMENTATION_RESULT: shortMsg=Process crashed.",
            "INSTRUMENTATION_CODE: 0",
        ])
        results, completed = parse_instrumentation(text)
        self.assertFalse(completed)
        public = "\n".join("May flow " + label + ": " + outcome for label, outcome in results)
        for word in private:
            self.assertNotIn(word, public)
        self.assertEqual([label for label, _ in results],
                         ["ChatReplyFlow test 1", "ChatReplyFlow test 2", "an unlisted flow test 1"])


if __name__ == "__main__":
    unittest.main()
