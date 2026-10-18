import os
import re
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


class DispatchChoicesTest(unittest.TestCase):
    def test_the_workflows_flows_choices_are_exactly_the_flows_admission_knows(self):
        # A choice admission doesn't know is refused only after a dispatch; a flow without a choice can't be run.
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/emulator-flows.yml").read_text()
        block = re.search(r"(?m)^      flows:\n((?:        .*\n)+)", workflow)
        self.assertIsNotNone(block, "emulator-flows.yml has no flows input")
        options = re.search(r"(?m)^        options: \[(.*)\]$", block.group(1))
        self.assertIsNotNone(options, "the flows input lists its options on one line")
        self.assertEqual([o.strip() for o in options.group(1).split(",")], list(flows.FLOWS))


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


class ManifestTest(unittest.TestCase):
    def manifest(self, text):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.txt"
            if text is not None:
                path.write_text(text)
            return flows.read_manifest(path)

    def test_reads_class_counts_and_skips_comments(self):
        self.assertEqual(self.manifest("# expected\nChatReplyFlow 3\n\nSignInFlow 2\n"), {"ChatReplyFlow": 3, "SignInFlow": 2})

    def test_reads_may_apps_current_manifest_classes(self):
        # may-app android/flows/manifest.txt with #180 (build 10's PlanYourDayFlow), ApprovalsFlow (build 9's crash),
        # OnboardingBFlow (#239, build 11's onboarding B), build 12's seven (may-app docs/evidence/build-12) and build 13's
        # four (may-app android/flows README, flows 18-21).
        text = "".join(f"{name} 1\n" for name in ("AnyTimeEventFlow", "ApprovalsFlow", "ChatReplyFlow", "ComputerRestingFlow", "ConnectionsFlow",
                                                  "DraftKillFlow", "EmptyStatesFlow", "MessageMenuFlow", "NewAccountConfirmFlow", "OnboardingBFlow",
                                                  "PinFlow", "PlanYourDayFlow", "ReportBugFlow", "SendingStatusFlow", "SignInFlow",
                                                  "SignOutQueuedFlow", "TakeNotesFlow", "TaskDetailFlow", "TaskFilesOffFlow",
                                                  "ThreadReplyReconnectFlow", "VoiceNotesOffFlow"))
        try:
            read = self.manifest(text)
        except FlowsError as refused:
            read = refused
        self.assertEqual(sorted(read) if isinstance(read, dict) else read, sorted(name for name, _ in
                         (line.split() for line in text.splitlines())))

    def test_reads_a_manifest_with_a_byte_order_mark(self):
        try:
            read = self.manifest("\ufeffChatReplyFlow 3\n")
        except FlowsError as refused:
            read = refused
        self.assertEqual(read, {"ChatReplyFlow": 3})

    def test_refuses_a_manifest_over_64_kib(self):
        with self.assertRaises(FlowsError) as refused:
            self.manifest("ChatReplyFlow 3\n" + "#" * (64 * 1024))
        self.assertIn("larger than 64 KiB", str(refused.exception))

    def test_refuses_a_missing_empty_or_malformed_manifest(self):
        for text in (None, "", "# only a comment\n", "ChatReplyFlow\n", "ChatReplyFlow 0\n", "ChatReplyFlow -1\n",
                     "ChatReplyFlow 3 extra\n", "NewFlow 1\n", "ChatReplyFlow 3\nChatReplyFlow 3\n"):
            with self.subTest(text=text), self.assertRaises(FlowsError):
                self.manifest(text)

    EXPECTED = {"ChatReplyFlow": 2, "SignInFlow": 1}

    def results(self, *labels):
        return [(label, "pass") for label in labels]

    def test_exactly_the_manifest_matches(self):
        self.assertIsNone(flows.manifest_difference(
            self.results("ChatReplyFlow test 1", "ChatReplyFlow test 2", "SignInFlow test 1"), self.EXPECTED, "all"))
        self.assertIsNone(flows.manifest_difference(self.results("SignInFlow test 1"), self.EXPECTED, "sign-in"))

    def test_a_missing_or_an_extra_test_says_which(self):
        for labels, selection, difference in (
                (("ChatReplyFlow test 1", "SignInFlow test 1"), "all", "missing"),                      # a test missing
                (("ChatReplyFlow test 1", "ChatReplyFlow test 2"), "all", "missing"),                   # a class missing
                ((), "sign-in", "missing"),
                (("ConnectionsFlow test 1",), "connections", "missing"),                                # class not listed
                (("ChatReplyFlow test 1", "ChatReplyFlow test 2", "ChatReplyFlow test 3", "SignInFlow test 1"), "all", "extra"),
                (("ChatReplyFlow test 1", "ChatReplyFlow test 2", "SignInFlow test 1", "an unlisted flow test 1"), "all", "extra"),
                (("an unlisted flow test 1",), "sign-in", "extra"),
                (("SignInFlow test 1", "SignInFlow test 2"), "sign-in", "extra")):
            with self.subTest(labels=labels, selection=selection):
                self.assertEqual(flows.manifest_difference(self.results(*labels), self.EXPECTED, selection), difference)


class ReportTest(unittest.TestCase):
    EXPECTED = {"ChatReplyFlow": 1}

    BRANCH = "May flows: flows taken from a branch; this run is not release evidence"

    def printed(self, from_branch):
        with mock.patch("builtins.print") as printed:
            flows.report([("ChatReplyFlow test 1", "pass")], True, self.EXPECTED, "all", from_branch)
        return [c.args[0] for c in printed.call_args_list]

    def test_a_run_matching_the_manifest_passes(self):
        self.assertEqual(self.printed("false"), ["May flow ChatReplyFlow test 1: pass", "Every selected May flow passed"])

    def test_flows_from_a_branch_say_so_first(self):
        self.assertEqual(self.printed("true")[0], self.BRANCH)
        self.assertNotIn(self.BRANCH, self.printed("false"))

    def test_all_passing_but_not_the_manifest_fails_saying_which(self):
        two = {"ChatReplyFlow": 1, "SignInFlow": 1}
        for results, expected, says in (
                ([("ChatReplyFlow test 1", "pass"), ("an unlisted flow test 1", "pass")], self.EXPECTED, "not in android/flows/manifest.txt"),
                ([], self.EXPECTED, "No flow test reported a result"),
                ([("ChatReplyFlow test 1", "pass")], two, "missing from the run")):
            with self.subTest(results=results), mock.patch("builtins.print"), self.assertRaises(FlowsError) as failed:
                flows.report(results, True, expected, "all", "false")
            self.assertIn(says, str(failed.exception))


class NavigationTest(unittest.TestCase):
    def navigate(self, modes, overlay=0):
        calls = []

        def fake_run(argv, log, *, env=None, check=True, **kwargs):
            calls.append(argv[-1])
            return overlay

        answers = iter(modes)
        clock = iter(range(0, 1000, 10))
        with mock.patch.object(flows, "run_private", side_effect=fake_run), \
                mock.patch.object(flows, "sleep"), \
                mock.patch.object(flows.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(flows.subprocess, "run",
                                  side_effect=lambda *a, **k: mock.Mock(stdout=next(answers).encode())):
            flows.three_button_navigation("adb", Path("log"), {})
        return calls

    def test_switches_and_waits_for_three_button(self):
        self.assertEqual(self.navigate(["2", "0"]), ["com.android.internal.systemui.navbar.threebutton"])

    def test_a_failed_switch_or_a_mode_that_never_changes_fails_saying_so(self):
        for modes, overlay in ((["0"], 1), (["2"] * 10, 0)):
            with self.subTest(overlay=overlay), self.assertRaises(FlowsError) as failed:
                self.navigate(modes, overlay)
            self.assertIn("did not switch to three-button navigation", str(failed.exception))


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

    def test_a_failed_assertion_says_its_line_in_the_flows_own_file(self):
        frames = "\n".join(["\tat org.junit.Assert.fail(Assert.java:89)",
                            "\tat org.junit.Assert.assertTrue(Assert.java:42)",
                            "\tat " + self.PKG + "FlowSupportKt.within(FlowSupport.kt:82)",
                            "\tat " + self.PKG + "DraftKillFlow.draftSurvives(DraftKillFlow.kt:52)",
                            "\tat " + self.PKG + "DraftKillFlow.c_sending_clears_it_for_good(DraftKillFlow.kt:63)"])
        text = "\n".join([
            status(self.PKG + "DraftKillFlow", "a", -2, "java.lang.AssertionError: the draft is back\n" + frames),
            # A message that looks like a frame is never read; with no frame in the flow's file, no line.
            status(self.PKG + "DraftKillFlow", "b", -2, "java.lang.AssertionError: x (DraftKillFlow.kt:99)\n\tat org.junit.Assert.fail(Assert.java:89)"),
            # An exception says no line, nor does a flow that isn't listed.
            status(self.PKG + "DraftKillFlow", "c", -2, "java.lang.IllegalStateException: y\n" + frames),
            status("com.example.Leak", "d", -2, "java.lang.AssertionError: z\n\tat com.example.Leak.d(Leak.kt:7)"),
            "INSTRUMENTATION_CODE: -1",
        ])
        results, _ = parse_instrumentation(text)
        self.assertEqual([outcome for _, outcome in results], [
            "fail (assertion, line 52)", "fail (assertion)", "fail (an exception, not an assertion)", "fail (assertion)"])
        public = "\n".join("May flow " + label + ": " + outcome for label, outcome in results)
        for word in ("the draft is back", "draftSurvives", "FlowSupport", "Assert.java", "99", "Leak.kt", "IllegalState"):
            self.assertNotIn(word, public)

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
