"""May-app's iOS gating tests (ios_gating.py): the list, the run on the simulator, its record. Xcode is faked."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import ios_gating


class Done:
    def __init__(self, code=0):
        self.returncode = code


# xcresulttool's `get test-results tests` shape: a plan, its bundles, their suites and cases.
RESULTS = {"testNodes": [{"nodeType": "Test Plan", "name": "MayPlaygroundSnapshots", "children": [
    {"nodeType": "Unit test bundle", "name": "MayPlaygroundSnapshots", "children": [
        {"nodeType": "Test Suite", "name": "PushDelegateThreadTests", "children": [
            {"nodeType": "Test Case", "name": "testTapRoutesOnTheMainActor()", "result": "Failed"},
            {"nodeType": "Test Case", "name": "testBannerShows()", "result": "Passed"}]},
        {"nodeType": "Test Suite", "name": "Odd Suite", "children": [
            {"nodeType": "Test Case", "name": "test with spaces", "result": "Failed"}]}]}]}]}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        (self.source / "ios").mkdir(parents=True)
        self.private.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def listed(self, text):
        (self.source / ios_gating.LIST).write_text(text)


class SelectorTests(Workspace):
    def test_no_list_or_one_with_no_selector_is_none(self):
        self.assertEqual(ios_gating.selectors(self.source), ("none", []))
        self.listed("# N's gating tests\n\n   \n")
        self.assertEqual(ios_gating.selectors(self.source), ("none", []))

    def test_each_selector_is_a_bundle_and_class_or_method(self):
        self.listed("# push\nMayPlaygroundSnapshots/PushDelegateThreadTests\n  MayPlaygroundSnapshots/A/testB  \n")
        self.assertEqual(ios_gating.selectors(self.source), ("listed", [
            "MayPlaygroundSnapshots/PushDelegateThreadTests", "MayPlaygroundSnapshots/A/testB"]))

    def test_anything_else_is_malformed_and_runs_nothing(self):
        for case, text in (("a bundle alone", "MayPlaygroundSnapshots\n"), ("four parts", "A/B/C/D\n"),
                           ("a space", "A/B C\n"), ("an option", "-only-testing:A/B\n"), ("a path", "../A/B\n"),
                           ("parentheses", "A/B/testC()\n"), ("twice", "A/B\nA/B\n"),
                           ("too many", "".join(f"A/B{n}\n" for n in range(ios_gating.MAX_SELECTORS + 1))),
                           ("too long", "A/B\n" + "#" * ios_gating.MAX_LIST + "\n"), ("not ASCII", "A/Bé\n"),
                           ("a leading digit", "A/1B\n")):
            with self.subTest(case=case):
                (self.source / ios_gating.LIST).write_bytes(text.encode("utf-8"))
                self.assertEqual(ios_gating.selectors(self.source), ("malformed", []))
        (self.source / ios_gating.LIST).unlink()
        (self.root / "elsewhere.txt").write_text("A/B\n")
        (self.source / ios_gating.LIST).symlink_to(self.root / "elsewhere.txt")
        self.assertEqual(ios_gating.selectors(self.source), ("malformed", []), "a link is never read")
        (self.source / ios_gating.LIST).unlink()
        (self.source / ios_gating.LIST).mkdir()
        self.assertEqual(ios_gating.selectors(self.source), ("malformed", []))
        self.assertTrue(len("".join(f"A/B{n}\n" for n in range(ios_gating.MAX_SELECTORS))) < ios_gating.MAX_LIST)


class RunTests(Workspace):
    def run_tests(self, code=0, results=None, simulator=None, raises=None):
        self.calls = []

        def tool(argv, log, env=None, timeout=None):
            self.calls.append((argv, env, timeout))
            if raises:
                raise raises
            return Done(code)

        return ios_gating.run(self.source, self.root, self.private, {"DEVELOPER_DIR": "/X"}, self.private / "log",
                              tool=tool, simulator=simulator or (lambda log, env: "UDID-1"),
                              read_results=lambda bundle, env: results if results is not None else (2, [], 0))

    def test_no_list_runs_nothing_and_records_none(self):
        self.assertEqual(self.run_tests(), ios_gating.record("none"))
        self.assertEqual(self.calls, [])
        self.assertEqual(ios_gating.read(self.private), ios_gating.record("none"))

    def test_a_malformed_list_runs_nothing_and_fails(self):
        self.listed("A/B C\n")
        self.assertEqual(self.run_tests()["state"], "malformed")
        self.assertEqual(self.calls, [])
        self.assertIn("malformed", ios_gating.FAILING)

    def test_the_listed_tests_alone_run_on_the_simulator_unsigned(self):
        self.listed("MayPlaygroundSnapshots/PushDelegateThreadTests\nMayPlaygroundSnapshots/A/testB\n")
        result = self.run_tests()
        self.assertEqual(result, {"state": "passed", "listed": 2, "ran": 2, "failed": [], "total": 0})
        argv, env, timeout = self.calls[0]
        self.assertEqual(argv[:2], ["xcodebuild", "test"])
        for flag, value in (("-project", str(self.source / "ios/May.xcodeproj")), ("-scheme", "MayPlaygroundSnapshots"),
                            ("-destination", "id=UDID-1"), ("-derivedDataPath", str(self.root / "may-derived")),
                            ("-resultBundlePath", str(self.private / ios_gating.BUNDLE))):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual([a for a in argv if a.startswith("-only-testing:")], [
            "-only-testing:MayPlaygroundSnapshots/PushDelegateThreadTests", "-only-testing:MayPlaygroundSnapshots/A/testB"])
        self.assertEqual(argv[-1], "CODE_SIGNING_ALLOWED=NO")
        self.assertEqual(timeout, ios_gating.TEST_SECONDS)
        snapshots = str(self.private / ios_gating.SNAPSHOTS)
        self.assertEqual((env.get("MAY_SNAPSHOT_DIR"), env.get("TEST_RUNNER_MAY_SNAPSHOT_DIR")), (snapshots, snapshots),
                         "snapshot images stay in the private folder")
        self.assertEqual(ios_gating.read(self.private), result)

    def test_a_failing_test_fails_with_its_name(self):
        self.listed("MayPlaygroundSnapshots/PushDelegateThreadTests\n")
        result = self.run_tests(code=65, results=(2, ["MayPlaygroundSnapshots/PushDelegateThreadTests/testTap"], 1))
        self.assertEqual(result, {"state": "failed", "listed": 1, "ran": 2,
                                  "failed": ["MayPlaygroundSnapshots/PushDelegateThreadTests/testTap"], "total": 1})

    def test_no_test_run_a_nonzero_exit_or_a_tool_that_did_not_finish_is_failed(self):
        self.listed("MayPlaygroundSnapshots/Typo\n")
        self.assertEqual(self.run_tests(code=0, results=(0, [], 0)),
                         {"state": "failed", "listed": 1, "ran": 0, "failed": [], "total": 0},
                         "a selector matching nothing never passes")
        self.assertEqual(self.run_tests(code=65, results=(0, [], 0))["state"], "failed", "the build for testing failed")
        self.assertEqual(self.run_tests(code=65, results=(3, [], 0))["state"], "failed", "the exit decides too")
        self.assertEqual(self.run_tests(raises=subprocess.TimeoutExpired("xcodebuild", 1))["state"], "failed")

        def no_simulator(log, env):
            import renders
            raise renders.RendersError("No iOS simulator runtime is available")
        self.assertEqual(self.run_tests(simulator=no_simulator)["state"], "failed")


class RecordTests(Workspace):
    def test_outcomes_name_each_failing_case_in_a_selectors_shape_and_count_the_rest(self):
        self.assertEqual(ios_gating.outcomes(RESULTS), (3, [
            "MayPlaygroundSnapshots/PushDelegateThreadTests/testTapRoutesOnTheMainActor"], 2))
        self.assertEqual(ios_gating.outcomes({}), (0, [], 0))
        self.assertEqual(ios_gating.outcomes({"testNodes": "x"}), (0, [], 0))
        deep = {"nodeType": "Test Case", "name": "t", "result": "Failed"}
        for _ in range(40):
            deep = {"nodeType": "Group", "children": [deep]}
        self.assertEqual(ios_gating.outcomes({"testNodes": [deep]}), (0, [], 0), "bounded depth")

    def test_a_record_is_checked_whole(self):
        good = {"state": "failed", "listed": 1, "ran": 2, "failed": ["A/B/testC"], "total": 1}
        self.assertEqual(ios_gating.checked(dict(good)), good)
        for change in ({"state": "maybe"}, {"listed": -1}, {"listed": ios_gating.MAX_SELECTORS + 1}, {"ran": True},
                       {"failed": ["A/B C"]}, {"failed": "A/B"}, {"failed": ["A/B"] * 2, "total": 1},
                       {"total": "1"}, {"extra": 1}, {"state": "passed"}, {"state": "none"}):
            with self.subTest(change=change):
                self.assertIsNone(ios_gating.checked(dict(good, **change)))
        self.assertEqual(ios_gating.checked(ios_gating.record("not run")), ios_gating.record("not run"))
        self.assertIsNone(ios_gating.checked(dict(ios_gating.record("none"), listed=2)))

    def test_a_missing_or_bad_record_reads_as_not_run(self):
        self.assertEqual(ios_gating.read(self.private), ios_gating.record("not run"))
        (self.private / ios_gating.RESULT).write_text("{")
        self.assertEqual(ios_gating.read(self.private), ios_gating.record("not run"))
        (self.private / ios_gating.RESULT).write_text(json.dumps({"state": "passed"}))
        self.assertEqual(ios_gating.read(self.private), ios_gating.record("not run"))
        (self.private / ios_gating.RESULT).unlink()
        (self.root / "planted.json").write_text(json.dumps(ios_gating.record("passed", 1, 1)))
        (self.private / ios_gating.RESULT).symlink_to(self.root / "planted.json")
        self.assertEqual(ios_gating.read(self.private), ios_gating.record("not run"), "a link is never read")

    def test_results_reads_xcresulttool_or_nothing(self):
        self.assertEqual(ios_gating.results(self.private / "absent.xcresult", dict(os.environ)), (0, [], 0))


if __name__ == "__main__":
    unittest.main()
