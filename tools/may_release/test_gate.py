import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import gate
import play_upload
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
        # may-native-compile-pr (compile of a may-app pull request head) is proposed, not yet created:
        # admission denies on its 404 until Samuel creates it. may-ds-renders (the design-system Swift compile,
        # ds-swift-compile.yml) is named by Samuel; admission likewise denies until it exists with its policy.
        self.assertEqual(gate.ENVIRONMENTS, {
            "compile": "may-native-compile",
            "compile-pr": "may-native-compile-pr",
            "ds-compile": "may-ds-renders",
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

    def test_committed_artifact_keys_are_the_four_distinct_recipients_and_the_console_placeholder(self):
        # Provided by Dots from Samuel's Mac on 2026-10-13; private halves never left it.
        expected = {
            "may-ios-internal": "age1rxygmrkqmgaweevxdwpgpwwlgvf9haeulg4qwvv03mv7ml4rhqxsjsxfwl",
            "may-ios-playground-internal": "age1q8mp85cgc9w5uyvjqy57y0r8dm4d5s88x7q24fmpel8vsw777gtsa8gxrf",
            "may-android-internal": "age1kpcl62lf4hssansdlcu5zwde0esfwazvg3uuwddn4rru2zlrcceskyd7vg",
            "may-android-playground-internal": "age1cv26vadk855hhzs6fljc6wvh3t96r2kklkg8uul62hwfjj5u8v9sex4n5j",
        }
        self.assertEqual({e: gate.artifact_recipient(e) for e in expected}, expected)
        # Samuel's sign-only Console-upload key: a placeholder until he commits its public half.
        self.assertEqual(gate.SIGN_ONLY_RECIPIENT, "may-android-console-upload")
        self.assertIsNone(gate.artifact_recipient(gate.SIGN_ONLY_RECIPIENT))
        self.assertEqual(sorted(p.name for p in gate.ARTIFACT_KEYS.iterdir()),
                         sorted(e + ".age.pub" for e in list(expected) + [gate.SIGN_ONLY_RECIPIENT]))

    def test_placeholder_artifact_key_denies_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory) / "keys"
            keys.mkdir()
            (keys / "may-ios-internal.age.pub").write_text(
                "PLACEHOLDER: the setup lane replaces this whole file with the age public key.\n")
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                   "GH_TOKEN": "fixture-token", "GITHUB_OUTPUT": os.path.join(directory, "output")}
            argv = ["gate.py", "--action", "ios", "--target", "production", "--source-sha", self.sha,
                    "--build-number", "9"]
            with patch.dict(os.environ, env), patch("sys.argv", argv), patch("gate.ARTIFACT_KEYS", keys), \
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


    def run_admission(self, dry_run, action="android", target="playground", sign_only="false", build="3",
                      console_key=True, console_value=None):
        """gate.main() as admission runs it, with fixture metadata. Returns (denial, output, requests)."""
        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory)
            for environment in gate.ENVIRONMENTS.values():
                (keys / (environment + ".age.pub")).write_text("age1" + "q" * 58 + "\n")
            (keys / (gate.SIGN_ONLY_RECIPIENT + ".age.pub")).write_text(
                (console_value or "age1" + "p" * 58) + "\n" if console_key else "PLACEHOLDER: Samuel's key\n")
            output = os.path.join(directory, "output")
            env = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH,
                   "GH_TOKEN": "fixture-token", "GITHUB_OUTPUT": output}
            argv = ["gate.py", "--action", action, "--target", target, "--source-sha", self.sha,
                    "--build-number", build, "--dry-run", dry_run, "--sign-only", sign_only]
            requests = []

            def fake_json(path, token):
                requests.append(path)
                return self.policies if path.endswith("per_page=100") else self.environment

            denial = None
            with patch.dict(os.environ, env), patch("sys.argv", argv), patch("gate.ARTIFACT_KEYS", keys), \
                    patch("gate.get_json", side_effect=fake_json):
                try:
                    gate.main()
                except GateDenial as error:
                    denial = error.args[0]
            written = Path(output).read_text() if os.path.exists(output) else None
            return denial, written, requests

    HELD = ("Uploads are held for this platform; this run did not upload anything. "
            "dry_run: false fails here by design; dispatch with dry_run: true")
    NOT_ADMITTED = ("Stage 4 admits only a May or May Playground upload to the Play internal track or to internal "
                    "TestFlight; this run did not upload anything. Dispatch with dry_run: true")
    FLOOR = ("Store build number is below this app's floor (May production Android starts at 3: its first "
             "release, versionCode 2, was uploaded by hand on 14 Oct)")

    def test_stage_4_admits_android_internal_and_ios_testflight_uploads(self):
        self.assertEqual(gate.UPLOADS_HELD, set())
        self.assertEqual(gate.TESTFLIGHT_TARGETS, {"playground", "production"})
        denial, written, requests = self.run_admission("false")
        self.assertIsNone(denial)
        self.assertEqual(written, "environment=may-android-playground-internal\nbuild_environment=may-native-compile\n")
        self.assertTrue(requests)
        for target, environment in (("playground", "may-ios-playground-internal"), ("production", "may-ios-internal")):
            with self.subTest(target=target):
                denial, written, requests = self.run_admission("false", "ios", target)
                self.assertIsNone(denial)
                self.assertEqual(written, "environment=" + environment + "\nbuild_environment=may-native-compile\n")
                self.assertTrue(requests)

    def test_stage_3_admits_a_may_production_android_upload(self):
        denial, written, requests = self.run_admission("false", "android", "production")
        self.assertIsNone(denial)
        self.assertEqual(written, "environment=may-android-internal\nbuild_environment=may-native-compile\n")
        self.assertEqual(sorted({path.split("/")[1] for path in requests}), ["may-android-internal", "may-native-compile"])

    def test_may_production_android_build_number_floor_is_3_dry_runs_included(self):
        for dry_run in ("true", "false"):
            for build in ("1", "2"):
                with self.subTest(dry_run=dry_run, build=build):
                    denial, written, requests = self.run_admission(dry_run, "android", "production", build=build)
                    self.assertEqual(denial, self.FLOOR)
                    self.assertEqual((written, requests), (None, []))
            denial, _, _ = self.run_admission(dry_run, "android", "production", build="3")
            self.assertIsNone(denial)
        # The rechecks inside protected jobs apply it too; Playground and iOS keep their old rule.
        with self.assertRaises(GateDenial):
            validate_run(REPO, MAIN, DISPATCH, self.sha, "android", "2", "production")
        for action, target in (("android", "playground"), ("ios", "production"), ("ios", "playground")):
            self.assertTrue(validate_run(REPO, MAIN, DISPATCH, self.sha, action, "2", target))

    def test_stage_4_refuses_every_other_upload_before_anything_else(self):
        # A held platform, or an iOS target outside TESTFLIGHT_TARGETS, is refused before anything else.
        for held, targets, expected in (({"ios"}, {"playground", "production"}, self.HELD),
                                        (set(), set(), self.NOT_ADMITTED), (set(), {"production"}, self.NOT_ADMITTED)):
            with self.subTest(held=held, targets=targets), patch("gate.UPLOADS_HELD", held), \
                    patch("gate.TESTFLIGHT_TARGETS", targets):
                denial, written, requests = self.run_admission("false", "ios", "playground")
                self.assertEqual(denial, expected)
                self.assertIsNone(written)  # no environment output, so no later job can start
                self.assertEqual(requests, [])  # refused before any metadata request
        for value in ("False", "FALSE", "", "0", "yes", "true "):
            with self.subTest(value=value):
                denial, written, _ = self.run_admission(value)
                self.assertIsNotNone(denial)
                self.assertIsNone(written)

    def test_admission_refuses_any_track_but_internal(self):
        for target in ("playground", "production"):
            for track in ("production", "beta", "alpha", "qa", None):
                with self.subTest(target=target, track=track), \
                        patch("gate.PLAY_UPLOAD_TRACKS", {"playground": track, "production": track}):
                    denial, written, requests = self.run_admission("false", "android", target)
                    self.assertEqual(denial, self.NOT_ADMITTED)
                    self.assertEqual((written, requests), (None, []))
        # The table admission reads is the one play_upload.py writes from.
        self.assertIs(gate.PLAY_UPLOAD_TRACKS, play_upload.PLAY_UPLOAD_TRACKS)

    def test_admission_passes_a_dry_run_for_every_target(self):
        for action in ("android", "ios"):
            for target in ("playground", "production"):
                with self.subTest(action=action, target=target):
                    denial, written, requests = self.run_admission("true", action, target)
                    self.assertIsNone(denial)
                    self.assertTrue(written)
                    self.assertTrue(requests)

    def test_sign_only_is_admitted_for_may_production_android_with_dry_run_true(self):
        denial, written, requests = self.run_admission("true", "android", "production", sign_only="true")
        self.assertIsNone(denial)
        self.assertEqual(written, "environment=may-android-internal\nbuild_environment=may-native-compile\n")
        self.assertTrue(requests)

    def test_sign_only_is_refused_everywhere_else_before_anything(self):
        cases = (
            (("true", "android", "playground"), "sign_only is only for the May production Android app (target "
                                                "production); nothing was signed. Dispatch without sign_only"),
            (("true", "ios", "production"), "sign_only is only for the May production Android app (target "
                                            "production); nothing was signed. Dispatch without sign_only"),
            (("false", "android", "production"), "sign_only never uploads: dispatch it with dry_run: true; "
                                                 "nothing was signed"),
        )
        for (dry_run, action, target), expected in cases:
            with self.subTest(dry_run=dry_run, action=action, target=target):
                denial, written, requests = self.run_admission(dry_run, action, target, sign_only="true")
                self.assertEqual(denial, expected)
                self.assertEqual((written, requests), (None, []))
        for value in ("True ", "yes", "1", ""):
            with self.subTest(value=value):
                denial, written, _ = self.run_admission("true", "android", "production", sign_only=value)
                self.assertEqual(denial, "sign_only must be true or false")
                self.assertIsNone(written)
        # dry_run missing while sign_only is true fails closed.
        with self.assertRaises(GateDenial):
            gate.validate_sign_only("android", "true", None, "production")

    def test_sign_only_waits_for_samuels_console_upload_key(self):
        denial, written, requests = self.run_admission("true", "android", "production", sign_only="true",
                                                       console_key=False)
        self.assertEqual(denial, "The Console-upload public key (artifact_keys/may-android-console-upload.age.pub) is "
                                 "still a placeholder; Samuel's public key must be committed before sign_only can run")
        self.assertEqual((written, requests), (None, []))
        # Nor a Console key that is one of the environments' handoff keys, whose private halves are CI secrets.
        denial, written, requests = self.run_admission("true", "android", "production", sign_only="true",
                                                       console_value="age1" + "q" * 58)
        self.assertEqual(denial, "The Console-upload key must not be any environment's handoff key, whose private "
                                 "half is a CI secret; commit a key only Samuel holds")
        self.assertEqual((written, requests), (None, []))
        # A plain dry run of the same target does not need it.
        denial, _, _ = self.run_admission("true", "android", "production", console_key=False)
        self.assertIsNone(denial)

    def test_rechecks_pass_no_dry_run(self):
        gate.validate_dry_run("android", None)  # the in-job rechecks
        gate.validate_dry_run("compile", None)
        gate.validate_sign_only("android", None, None, "playground")
        with self.assertRaises(GateDenial):
            gate.validate_dry_run("compile", "false")
        gate.validate_dry_run("ios", "false", "playground")
        with patch("gate.TESTFLIGHT_TARGETS", set()), self.assertRaises(GateDenial):
            gate.validate_dry_run("ios", "false", "playground")  # an unheld platform still needs its own rule
        with self.assertRaises(GateDenial):
            gate.validate_dry_run("ios", "false", "staging")


if __name__ == "__main__":
    unittest.main()
