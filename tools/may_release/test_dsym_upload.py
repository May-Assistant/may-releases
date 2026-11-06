"""An iOS release's dSYMs, uploaded to PostHog after a successful TestFlight upload (dsym_upload.py,
store-release-draft.yml's ios-release)."""

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dsym_upload
import static_check
from store import StoreError

KEY = "phx_" + "K" * 40
WORKFLOW = ".github/workflows/store-release-draft.yml"


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.dsyms = self.root / dsym_upload.DSYMS
        for name in ("MayPlayground.app.dSYM", "MayWidgets.appex.dSYM"):
            (self.dsyms / name / "Contents").mkdir(parents=True)
        self.cli = self.root / "may-tools/posthog-cli/posthog-cli-aarch64-apple-darwin/posthog-cli"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("#!/bin/sh\n")
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def environ(self, **extra):
        env = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin", "HOME": str(self.root),
               dsym_upload.SYMBOLS_KEY: KEY, dsym_upload.PROJECT: "12345"}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def upload(self, status=0, **extra):
        def run(argv, env, stdout, stderr, stdin, timeout):
            self.calls.append((argv, env))
            return subprocess.CompletedProcess(argv, status)
        out = io.StringIO()
        with patch.dict(os.environ, self.environ(**extra), clear=True), patch("sys.stdout", out), \
                patch.object(dsym_upload, "install", lambda: self.cli.parent):
            result = dsym_upload.upload(run)
        return result, out.getvalue()

    def test_the_dsyms_go_up_with_the_key_in_the_clis_environment_alone(self):
        result, out = self.upload()
        self.assertEqual(result, 0)
        self.assertEqual(out, "May iOS dSYMs: 2 dSYM bundles uploaded to PostHog\n")
        (argv, env), = self.calls
        self.assertEqual(argv, [str(self.cli), "--host", "https://us.posthog.com", "dsym", "upload", "--directory",
                                str(self.dsyms)])
        self.assertEqual((env["POSTHOG_CLI_API_KEY"], env["POSTHOG_CLI_PROJECT_ID"]), (KEY, "12345"))
        self.assertEqual(env["HOME"], str(self.root / "may-private" / dsym_upload.HOME))
        self.assertNotIn(dsym_upload.SYMBOLS_KEY, env)
        self.assertNotIn(KEY, " ".join(argv))
        self.assertNotIn(KEY, out)
        self.assertNotIn("--include-source", argv)

    def test_the_host_follows_the_crash_report_cloud(self):
        for given, expected in (("https://eu.i.posthog.com/", "https://eu.posthog.com"),
                                ("https://us.i.posthog.com", "https://us.posthog.com"), ("", "https://us.posthog.com")):
            with self.subTest(given=given):
                self.calls = []
                self.upload(**{dsym_upload.HOST: given})
                self.assertEqual(self.calls[0][0][2], expected)
        self.calls = []
        result, out = self.upload(**{dsym_upload.HOST: "https://example.com"})
        self.assertEqual((result, self.calls), (None, []))
        self.assertIn("is not PostHog's US or EU cloud", out)

    def test_an_unset_key_or_project_skips_cleanly(self):
        result, out = self.upload(**{dsym_upload.SYMBOLS_KEY: None})
        self.assertEqual((result, out, self.calls),
                         (None, "May iOS dSYMs: MAY_POSTHOG_IOS_SYMBOLS_KEY is not set; nothing uploaded\n", []))
        for project in (None, "", "abc", "0", "1" * 13):
            with self.subTest(project=project):
                result, out = self.upload(**{dsym_upload.PROJECT: project})
                self.assertEqual((result, self.calls), (None, []))
                self.assertIn("MAY_POSTHOG_IOS_PROJECT_ID is not set or not a project number", out)

    def test_no_dsym_bundle_or_a_linked_folder_uploads_nothing(self):
        for path in list(self.dsyms.iterdir()):
            (path / "Contents").rmdir()
            path.rmdir()
        result, out = self.upload()
        self.assertEqual((result, self.calls), (None, []))
        self.assertIn("the archive holds no dSYM bundle", out)
        self.dsyms.rmdir()
        elsewhere = self.root / "elsewhere"
        (elsewhere / "X.dSYM").mkdir(parents=True)
        self.dsyms.symlink_to(elsewhere)
        result, _ = self.upload()
        self.assertEqual((result, self.calls), (None, []))

    def test_a_failed_upload_is_reported_and_never_fails_the_release(self):
        result, out = self.upload(status=3)
        self.assertEqual(result, 3)
        self.assertEqual(out, "May iOS dSYMs: the upload of 2 dSYM bundles did not succeed (exit status 3); the "
                              "release is not failed for it\n")

    def test_another_steps_secret_refuses_before_anything(self):
        with self.assertRaises(StoreError):
            self.upload(MAY_ASC_KEY_BASE64="x")
        self.assertEqual(self.calls, [])


class CliTests(unittest.TestCase):
    def run_tool(self, env, *args):
        return subprocess.run([sys.executable, "-I", "-S", "-B", dsym_upload.__file__, *args], capture_output=True,
                              env=dict(env, PATH=os.environ.get("PATH", "")))

    def test_every_way_out_exits_zero_with_a_fixed_line(self):
        with tempfile.TemporaryDirectory() as raw:
            good = ("--target", "playground", "--source-sha", "a" * 40, "--build-number", "14")
            for env, args, line in (
                    ({"RUNNER_TEMP": raw}, good, "May iOS dSYMs: MAY_POSTHOG_IOS_SYMBOLS_KEY is not set; nothing uploaded"),
                    ({"RUNNER_TEMP": raw, "MAY_ASC_KEY_BASE64": "x", dsym_upload.SYMBOLS_KEY: KEY}, good,
                     "May iOS dSYMs stopped: another step's secret or a malformed input; nothing uploaded"),
                    ({"RUNNER_TEMP": raw}, ("--target", "playground", "--source-sha", "x", "--build-number", "14"),
                     "May iOS dSYMs stopped: another step's secret or a malformed input; nothing uploaded")):
                with self.subTest(line=line):
                    done = self.run_tool(env, *args)
                    self.assertEqual(done.returncode, 0)
                    self.assertEqual(done.stdout.decode().strip(), line)
                    self.assertNotIn(KEY, done.stdout.decode() + done.stderr.decode())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.text = (self.root / WORKFLOW).read_text()

    def problems(self, text):
        return static_check.store_problems(text)

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.store_problems(self.text), [])
        self.assertEqual(static_check.dsym_upload_print_problems(self.root), [])
        self.assertIn(static_check.DSYM_COMMAND, self.text)

    def test_each_widening_is_flagged(self):
        step = self.text[self.text.index("      - name: " + static_check.DSYM_SEND):]
        step = step[:step.index("\n      - name:", 1) + 1]
        condition = "        if: ${{ !cancelled() && steps.testflight_upload.outcome == 'success' }}\n"
        key = "          MAY_POSTHOG_IOS_SYMBOLS_KEY: ${{ secrets.MAY_POSTHOG_IOS_SYMBOLS_KEY }}\n"
        for what, changed in (
                ("unconditional", step.replace(condition, "")),
                ("another secret", step.replace(key, key + "          MAY_ASC_ISSUER_ID: ${{ secrets.MAY_ASC_ISSUER_ID }}\n")),
                ("no integrity check before", None),
                ("may fail the job quietly", step.replace(condition, condition + "        continue-on-error: true\n"))):
            with self.subTest(what):
                if changed is None:
                    integrity = "      - name: Check the checkout is exactly its commit before uploading the dSYMs\n" \
                                "        run: python3 -I -S -B tools/may_release/integrity.py\n"
                    text = self.text.replace(integrity, "")
                else:
                    text = self.text.replace(step, changed)
                self.assertNotEqual(text, self.text)
                self.assertTrue(self.problems(text))


if __name__ == "__main__":
    unittest.main()
