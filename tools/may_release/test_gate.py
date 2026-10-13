import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import gate
from gate import GateDenial, validate_environment, validate_run

REPO, MAIN, DISPATCH = "May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch"


class GateTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.environment = {
            "can_admins_bypass": False,
            "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False},
            "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False, "reviewers": [
                {"type": "User", "reviewer": {"id": 58840187, "login": "samuelalake"}}]}],
        }
        self.policies = {"branch_policies": [{"name": "main"}]}

    def test_exact_main_manual_only(self):
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, self.sha, "ios", "9"), "may-ios-internal")
        for repo, ref, event in (("May-Assistant/may-app", MAIN, DISPATCH),
                                 (REPO, "refs/heads/feature", DISPATCH),
                                 (REPO, MAIN, "push")):
            with self.assertRaises(ValueError):
                validate_run(repo, ref, event, self.sha, "ios", "9")

    def test_source_and_number_validation(self):
        for sha, build in (("abc", "9"), (self.sha, "0"), (self.sha, "09")):
            with self.assertRaises(ValueError):
                validate_run(REPO, MAIN, DISPATCH, sha, "ios", build)
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, self.sha, "compile"), "may-native-compile")

    def test_build_number_ceiling(self):
        self.assertEqual(validate_run(REPO, MAIN, DISPATCH, self.sha, "android", "2000000000"),
                         "may-android-internal")
        for build in ("2000000001", "2100000000", "99999999999"):
            with self.subTest(build=build), self.assertRaises(GateDenial):
                validate_run(REPO, MAIN, DISPATCH, self.sha, "android", build)

    def test_each_store_target_has_distinct_protected_environment(self):
        expected = {
            ("ios", "production"): "may-ios-internal",
            ("ios", "playground"): "may-ios-playground-internal",
            ("android", "production"): "may-android-internal",
            ("android", "playground"): "may-android-playground-internal",
        }
        for (platform, target), environment in expected.items():
            with self.subTest(platform=platform, target=target):
                self.assertEqual(validate_run(REPO, MAIN, DISPATCH, self.sha, platform, "9", target),
                                 environment)
        self.assertEqual(len(set(gate.ENVIRONMENTS.values())), len(gate.ENVIRONMENTS))
        for target in ("rem", "", "production/../playground"):
            with self.assertRaises(ValueError):
                validate_run(REPO, MAIN, DISPATCH, self.sha, "ios", "9", target)

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

    def test_exact_environment_names_confirmed_by_setup_lane(self):
        # Confirmed from the repository's Actions secrets page on 2026-10-13. Any
        # rename must be made in the repository settings and here together.
        self.assertEqual(gate.ENVIRONMENTS, {
            "compile": "may-native-compile",
            ("ios", "production"): "may-ios-internal",
            ("ios", "playground"): "may-ios-playground-internal",
            ("android", "production"): "may-android-internal",
            ("android", "playground"): "may-android-playground-internal",
        })

    def test_prevented_self_review_by_sole_reviewer_is_denied(self):
        prevented = copy.deepcopy(self.environment)
        prevented["protection_rules"][0]["prevent_self_review"] = True
        for actor_id, login in (("58840187", None), (None, "SamuelAlake"), ("58840187", "samuelalake"),
                                (None, None)):
            with self.subTest(actor_id=actor_id, login=login), self.assertRaises(GateDenial):
                validate_environment(prevented, self.policies, actor_id, login)
        # Another dispatcher can be approved by Samuel.
        validate_environment(prevented, self.policies, "12345", "another-maintainer")
        # Self-review allowed (Samuel's current policy for internal-only
        # releases): Samuel may dispatch and then approve.
        validate_environment(self.environment, self.policies, "58840187", "samuelalake")

    def test_unreadable_self_review_setting_is_denied(self):
        for value in (None, "true", 1):
            candidate = copy.deepcopy(self.environment)
            if value is None:
                del candidate["protection_rules"][0]["prevent_self_review"]
            else:
                candidate["protection_rules"][0]["prevent_self_review"] = value
            with self.subTest(value=value), self.assertRaises(GateDenial):
                validate_environment(candidate, self.policies, "12345", "another-maintainer")

    def test_missing_environment_404_denies_and_writes_no_output(self):
        def not_found(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "output")
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                   "GH_TOKEN": "fixture-token", "GITHUB_OUTPUT": output}
            argv = ["gate.py", "--action", "android", "--target", "playground",
                    "--source-sha", self.sha, "--build-number", "9"]
            with patch.dict(os.environ, env), patch("sys.argv", argv), \
                    patch("gate.artifact_recipient", return_value="age1" + "q" * 58), \
                    patch("urllib.request.urlopen", side_effect=not_found):
                with self.assertRaises(GateDenial) as denied:
                    gate.main()
            self.assertIn("does not exist", denied.exception.args[0])
            self.assertFalse(os.path.exists(output))

    def test_placeholder_artifact_key_denies_before_any_request(self):
        for environment in ("may-ios-internal", "may-ios-playground-internal", "may-android-internal",
                            "may-android-playground-internal"):
            self.assertIsNone(gate.artifact_recipient(environment))  # committed placeholders
        with tempfile.TemporaryDirectory() as directory:
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                   "GH_TOKEN": "fixture-token", "GITHUB_OUTPUT": os.path.join(directory, "output")}
            argv = ["gate.py", "--action", "ios", "--target", "production", "--source-sha", self.sha,
                    "--build-number", "9"]
            with patch.dict(os.environ, env), patch("sys.argv", argv), \
                    patch("urllib.request.urlopen") as request:
                with self.assertRaises(GateDenial) as denied:
                    gate.main()
                request.assert_not_called()
            self.assertIn("placeholder", denied.exception.args[0])

    def test_real_public_key_is_accepted_and_both_environments_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory)
            (keys / "may-android-playground-internal.age.pub").write_text("age1" + "q" * 58 + "\n")
            output = os.path.join(directory, "output")
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                   "GH_TOKEN": "fixture-token", "GITHUB_OUTPUT": output}
            argv = ["gate.py", "--action", "android", "--target", "playground", "--source-sha", self.sha,
                    "--build-number", "9"]
            checked = []

            def fake_json(path, token):
                checked.append(path.split("/")[1])
                return self.policies if path.endswith("per_page=100") else self.environment

            with patch.dict(os.environ, env), patch("sys.argv", argv), patch("gate.ARTIFACT_KEYS", keys), \
                    patch("gate.get_json", side_effect=fake_json):
                gate.main()
            self.assertEqual(sorted(set(checked)), ["may-android-playground-internal", "may-native-compile"])
            with open(output) as stream:
                self.assertEqual(stream.read(), "environment=may-android-playground-internal\n"
                                                "build_environment=may-native-compile\n")
        for bad in ("age1short", "AGE-SECRET-KEY-1" + "Q" * 58, "age1" + "b" * 58):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "x.age.pub").write_text(bad)
                with patch("gate.ARTIFACT_KEYS", Path(directory)):
                    self.assertIsNone(gate.artifact_recipient("x"))


if __name__ == "__main__":
    unittest.main()
