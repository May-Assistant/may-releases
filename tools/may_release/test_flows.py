import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flows
from flows import FlowsError, admit, parse_instrumentation

SHA = "a" * 40
OTHER = "b" * 40


def status(cls, test, code, stack=None):
    lines = ["INSTRUMENTATION_STATUS: class=" + cls, "INSTRUMENTATION_STATUS: test=" + test]
    if stack:
        lines.append("INSTRUMENTATION_STATUS: stack=" + stack)
    lines.append("INSTRUMENTATION_STATUS_CODE: " + str(code))
    return "\n".join(lines)


class AdmitTest(unittest.TestCase):
    def test_admits_a_dispatch_from_each_allowed_branch(self):
        for ref in flows.ALLOWED_REFS:
            admit("May-Assistant/may-releases", ref, "workflow_dispatch", SHA, OTHER, "all")

    def test_refuses_another_branch_event_or_repository(self):
        for repo, ref, event in (("May-Assistant/may-releases", "refs/heads/feature", "workflow_dispatch"),
                                 ("May-Assistant/may-releases", "refs/heads/main", "push"),
                                 ("someone/may-releases", "refs/heads/main", "workflow_dispatch")):
            with self.assertRaises(FlowsError):
                admit(repo, ref, event, SHA, OTHER, "all")

    def test_refuses_inexact_shas_and_unknown_flows(self):
        for source, flows_sha, selection in ((SHA[:7], OTHER, "all"), (SHA, "main", "all"),
                                             (SHA.upper(), OTHER, "all"), (SHA, OTHER, "everything")):
            with self.assertRaises(FlowsError):
                admit("May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch", source, flows_sha, selection)


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
