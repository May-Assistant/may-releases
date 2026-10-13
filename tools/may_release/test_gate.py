import copy
import unittest

from gate import validate_environment, validate_run


class GateTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.environment = {
            "can_admins_bypass": False,
            "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False},
            "protection_rules": [{"type": "required_reviewers", "reviewers": [
                {"type": "User", "reviewer": {"id": 58840187}}]}],
        }
        self.policies = {"branch_policies": [{"name": "main"}]}

    def test_exact_main_manual_only(self):
        self.assertEqual(validate_run("May-Assistant/may-releases", "refs/heads/main",
                                      "workflow_dispatch", self.sha, "ios", "9"), "may-ios-internal")
        for repo, ref, event in (("May-Assistant/may-app", "refs/heads/main", "workflow_dispatch"),
                                 ("May-Assistant/may-releases", "refs/heads/feature", "workflow_dispatch"),
                                 ("May-Assistant/may-releases", "refs/heads/main", "push")):
            with self.assertRaises(ValueError):
                validate_run(repo, ref, event, self.sha, "ios", "9")

    def test_source_and_number_validation(self):
        for sha, build in (("abc", "9"), (self.sha, "0"), (self.sha, "09")):
            with self.assertRaises(ValueError):
                validate_run("May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch",
                             sha, "ios", build)
        self.assertEqual(validate_run("May-Assistant/may-releases", "refs/heads/main",
                                      "workflow_dispatch", self.sha, "compile"), "may-native-compile")

    def test_each_store_target_has_distinct_protected_environment(self):
        expected = {
            ("ios", "production"): "may-ios-internal",
            ("ios", "playground"): "may-ios-playground-internal",
            ("android", "production"): "may-android-internal",
            ("android", "playground"): "may-android-playground-internal",
        }
        for (platform, target), environment in expected.items():
            with self.subTest(platform=platform, target=target):
                self.assertEqual(validate_run("May-Assistant/may-releases", "refs/heads/main",
                                              "workflow_dispatch", self.sha, platform, "9", target), environment)
        for target in ("rem", "", "production/../playground"):
            with self.assertRaises(ValueError):
                validate_run("May-Assistant/may-releases", "refs/heads/main",
                             "workflow_dispatch", self.sha, "ios", "9", target)

    def test_protected_owner_gate(self):
        validate_environment(self.environment, self.policies)
        for field, value in (("can_admins_bypass", True),
                             ("deployment_branch_policy", {"protected_branches": True}),
                             ("protection_rules", [])):
            candidate = copy.deepcopy(self.environment)
            candidate[field] = value
            with self.assertRaises(ValueError):
                validate_environment(candidate, self.policies)
        with self.assertRaises(ValueError):
            validate_environment(self.environment, {"branch_policies": [{"name": "*"}]})


if __name__ == "__main__":
    unittest.main()
