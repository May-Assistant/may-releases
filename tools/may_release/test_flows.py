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
    def test_reports_pass_fail_and_error_with_only_the_exception_class(self):
        text = "\n".join([
            status("io.github.mayassistant.may.flows.ChatReplyFlow", "reply", 1),
            status("io.github.mayassistant.may.flows.ChatReplyFlow", "reply", 0),
            status("io.github.mayassistant.may.flows.TaskDetailFlow", "menu", 1),
            status("io.github.mayassistant.may.flows.TaskDetailFlow", "menu", -2,
                   "java.lang.AssertionError: the project menu (secret detail) is inside"),
            "\tat org.junit.Assert.fail(Assert.java:89)",
            status("io.github.mayassistant.may.flows.EmptyStatesFlow", "chats", -1, "weird text: here"),
            "INSTRUMENTATION_CODE: -1",
        ])
        results, completed = parse_instrumentation(text)
        self.assertTrue(completed)
        self.assertEqual(results, [
            ("io.github.mayassistant.may.flows.ChatReplyFlow#reply", "pass"),
            ("io.github.mayassistant.may.flows.TaskDetailFlow#menu", "fail (java.lang.AssertionError)"),
            ("io.github.mayassistant.may.flows.EmptyStatesFlow#chats", "error (unknown)"),
        ])
        self.assertNotIn("secret", repr(results))

    def test_a_crash_is_not_complete_and_odd_names_are_dropped(self):
        text = status("x y", "t", 0) + "\nINSTRUMENTATION_RESULT: shortMsg=Process crashed.\nINSTRUMENTATION_CODE: 0"
        self.assertEqual(parse_instrumentation(text), ([], False))


if __name__ == "__main__":
    unittest.main()
