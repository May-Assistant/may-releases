"""Rehearsal of may-app's ios.sh archive-unsigned at a pull request head (ios-archive-rehearsal.yml, ios_rehearsal.py)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ios_rehearsal
from ios_rehearsal import RehearsalError
import static_check
from store import StoreError

WORKFLOW = ".github/workflows/ios-archive-rehearsal.yml"
PIN, ANDROID_PIN = "d" * 40, "e" * 40
APP_ID = "io.github.mayassistant.may.playground"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": os.environ.get("PATH", "")}


class Result:
    def __init__(self, returncode):
        self.returncode = returncode


class RehearsalFixture(unittest.TestCase):
    """A prepared runner: may-source (a git checkout with tools/release/ios.sh) and may-private with prepare.py's
    design-system record, under a temporary RUNNER_TEMP. ios.sh itself is replaced by a fake `run`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        source = self.root / "may-source"
        (source / "tools/release").mkdir(parents=True)
        (source / "tools/release/ios.sh").write_text("#!/usr/bin/env bash\nexit 99\n")
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(source)] + argv, check=True, env=GIT_ENV, capture_output=True)
        self.sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, env=GIT_ENV,
                                  capture_output=True).stdout.decode().strip()
        private = self.root / "may-private"
        private.mkdir()
        (private / "design-system-pins.json").write_text(json.dumps(
            {"platform": "ios", "target": "playground", "source_sha": self.sha,
             "design_system": {"ios": PIN, "android": ANDROID_PIN}}))
        self.environ = {"RUNNER_TEMP": str(self.root), "PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
                        "GITHUB_TOKEN_LIKE": "x", "GITHUB_WORKSPACE": "/w"}
        self.seen = []

    def tearDown(self):
        self.directory.cleanup()

    def ios_record(self, **changes):
        record = {"platform": "ios", "target": "playground", "app_id": APP_ID, "source_sha": self.sha,
                  "build_number": "7", "version": "0.1.0", "team_id_unverified": "R6A892D599", "design_system": PIN,
                  "xcode": "26.3 (17C529)", "minimum_ios": "17.0", "package_resolved_sha256": "c" * 64,
                  "product": "May.xcarchive", "sha256": None, "rehearsal": True}
        record.update(changes)
        return record

    def fake_ios_sh(self, status=0, record_changes=None, extra=None, record=True, wrong_sha=False):
        def run(argv, cwd, env, stdout, stderr, stdin):
            self.seen.append((argv, cwd, dict(env)))
            out = Path(env["MAY_ARTIFACT_DIR"])
            archive = out / "May.xcarchive"
            (archive / "Products/Applications/MayPlayground.app").mkdir(parents=True)
            (archive / "Products/Applications/MayPlayground.app/MayPlayground").write_bytes(b"\xcf\xfa\xed\xfe binary")
            (archive / "Info.plist").write_text("plist")
            os.symlink("Products", archive / "link")
            if extra:
                (out / extra).write_text("x")
            digest = ios_rehearsal.tree_sha256(archive)
            stdout.write(b"ios.sh: checks passed: design system " + PIN.encode() + b"\n")
            stdout.write(b"ios.sh: resolved package private-package 1.2.3 " + b"f" * 40 + b"\n")
            if record:
                value = self.ios_record(sha256="0" * 64 if wrong_sha else digest, **(record_changes or {}))
                stdout.write(b"may-release-record: " + json.dumps(value, sort_keys=True).encode() + b"\n")
            return Result(status)
        return run

    def archive(self, run, pinned=True, build_number="7", environ=None):
        out = io.StringIO()
        with patch.dict(os.environ, environ or self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(ios_rehearsal, "MIN_FREE_BYTES", 0):
            ios_rehearsal.archive("playground", self.sha, build_number, "116", run=run, pinned=lambda: pinned)
        return out.getvalue()

    def summary(self):
        return json.loads((self.root / "may-rehearsal/ios-archive-rehearsal.json").read_text())


class ArchiveTests(RehearsalFixture):
    def test_a_good_archive_publishes_only_the_fixed_summary(self):
        printed = self.archive(self.fake_ios_sh())
        self.assertEqual(printed, "May iOS archive rehearsal finished; ios.sh exit status 0; its log stays on this runner\n")
        summary = self.summary()
        self.assertEqual(summary["result"], "archived")
        self.assertEqual(summary["reason"], "archived and checked")
        self.assertEqual(summary["archive_tree_sha256"],
                         ios_rehearsal.tree_sha256(self.root / "may-release/May.xcarchive"))
        self.assertEqual((summary["pr_number"], summary["source_sha"], summary["target"], summary["app_id"],
                          summary["build_number"], summary["xcode"], summary["archive_uploaded"]),
                         (116, self.sha, "playground", APP_ID, "7", "26.3 (17C529)", False))
        self.assertEqual(set(summary), ios_rehearsal.SUMMARY_KEYS)
        # Nothing ios.sh printed or recorded about private content reaches the summary.
        text = (self.root / "may-rehearsal/ios-archive-rehearsal.json").read_text()
        for private in (PIN, ANDROID_PIN, "c" * 64, "0.1.0", "17.0", "private-package", "MayPlayground.app", "f" * 40):
            self.assertNotIn(private, text)
        self.assertEqual(sorted(p.name for p in (self.root / "may-rehearsal").iterdir()), ["ios-archive-rehearsal.json"])

    def test_ios_sh_gets_store_pys_environment_and_the_rehearsal_mark_only(self):
        self.archive(self.fake_ios_sh())
        (argv, cwd, env), = self.seen
        self.assertEqual(argv, ["bash", str(self.root / "may-source/tools/release/ios.sh"), "archive-unsigned"])
        self.assertEqual(cwd, self.root / "may-source")
        self.assertEqual(env, {"PATH": self.environ["PATH"], "HOME": str(self.root), "RUNNER_TEMP": str(self.root),
                               "MAY_TARGET": "playground", "MAY_SOURCE_SHA": self.sha, "MAY_BUILD_NUMBER": "7",
                               "MAY_APP_ID": APP_ID, "MAY_IOS_APP_ID": APP_ID, "MAY_TEAM_ID": "R6A892D599",
                               "MAY_ARTIFACT_DIR": str(self.root / "may-release"),
                               "DEVELOPER_DIR": "/Applications/Xcode_26.3.app/Contents/Developer",
                               "GIT_TERMINAL_PROMPT": "0", "MAY_DS_READ_TOKEN": "prepared-local-design-system-only",
                               "MAY_REHEARSAL": "1"})

    def test_origin_main_is_pointed_at_the_head_on_this_runner_only(self):
        self.archive(self.fake_ios_sh())
        head = subprocess.run(["git", "-C", str(self.root / "may-source"), "rev-parse", "refs/remotes/origin/main"],
                              env=GIT_ENV, capture_output=True).stdout.decode().strip()
        self.assertEqual(head, self.sha)
        self.assertIn("nothing was pushed", self.summary()["origin_main"])

    def test_an_existing_origin_main_is_refused_before_ios_sh_runs(self):
        subprocess.run(["git", "-C", str(self.root / "may-source"), "update-ref", "refs/remotes/origin/main", self.sha],
                       env=GIT_ENV, check=True)
        with self.assertRaises(RehearsalError) as caught:
            self.archive(self.fake_ios_sh())
        self.assertEqual(caught.exception.args[0], "Could not point this runner's origin/main at the pull request head")
        self.assertEqual(self.seen, [])

    def test_every_failure_is_published_as_a_fixed_reason(self):
        cases = {
            "ios.sh exited non-zero": dict(status=7),
            "ios.sh's record is missing or malformed": dict(record=False),
            "ios.sh's record differs from the request": dict(record_changes={"design_system": ANDROID_PIN}),
            "the product is not exactly May.xcarchive": dict(extra="May.xcarchive.partial"),
            "the archive differs from ios.sh's record": dict(wrong_sha=True),
        }
        for changes in ({"build_number": "8"}, {"rehearsal": False}, {"xcode": "26.4 (17D1)"}, {"target": "production"},
                        {"team_id_unverified": "AAAAAAAAAA"}, {"product": "Other.xcarchive"}):
            cases["ios.sh's record differs from the request " + str(changes)] = dict(record_changes=changes)
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                self.tearDown()
                self.setUp()
                self.archive(self.fake_ios_sh(**kwargs))
                summary = self.summary()
                self.assertEqual(summary["result"], "failed")
                self.assertEqual(summary["reason"], reason.split(" {")[0])
                self.assertIsNone(summary["archive_tree_sha256"])
                self.assertIsNone(summary["product"])
                self.assertEqual(summary["ios_sh_exit"], kwargs.get("status", 0))

    def test_ios_sh_that_cannot_start_is_recorded_as_failed(self):
        def run(*args, **kwargs):
            raise OSError("no bash")
        self.archive(run)
        self.assertEqual((self.summary()["reason"], self.summary()["ios_sh_exit"]), ("ios.sh did not start", -1))

    def test_refused_before_ios_sh_runs(self):
        cases = {
            "A read key or host file is still on disk; nothing was archived": lambda: (self.root / "may-private/app.key").write_text("k"),
            "Pinned Xcode 26.3 (17C529) is unavailable": lambda: None,
            "The prepared checkout is not this rehearsal's (platform, target or source SHA)":
                lambda: (self.root / "may-private/design-system-pins.json").write_text(json.dumps(
                    {"platform": "ios", "target": "production", "source_sha": self.sha, "design_system": {"ios": PIN}})),
            "tools/release/ios.sh is not in this may-app commit": lambda: (self.root / "may-source/tools/release/ios.sh").unlink(),
        }
        for message, arrange in cases.items():
            with self.subTest(message=message):
                self.tearDown()
                self.setUp()
                arrange()
                with self.assertRaises(RehearsalError) as caught:
                    self.archive(self.fake_ios_sh(), pinned="Xcode" not in message)
                self.assertEqual(caught.exception.args[0], message)
                self.assertEqual(self.seen, [])
                self.assertFalse((self.root / "may-rehearsal").exists())

    def test_too_little_free_space_is_refused_before_ios_sh_runs(self):
        out = io.StringIO()
        with patch.dict(os.environ, self.environ, clear=True), patch("sys.stdout", out), \
                patch.object(ios_rehearsal, "MIN_FREE_BYTES", 1 << 62), self.assertRaises(RehearsalError) as caught:
            ios_rehearsal.archive("playground", self.sha, "7", "116", run=self.fake_ios_sh(), pinned=lambda: True)
        self.assertEqual(caught.exception.args[0], "Runner has insufficient free space for an archive")
        self.assertEqual(self.seen, [])

    def test_a_record_with_crash_reports_off_archives_as_one_without_the_flag(self):
        # ios.sh with PostHog crash reports records posthog_key; a rehearsal never holds the key, so it is false.
        self.archive(self.fake_ios_sh(record_changes={"posthog_key": False}))
        self.assertEqual((self.summary()["result"], self.summary()["reason"]), ("archived", "archived and checked"))
        self.assertNotIn("posthog", (self.root / "may-rehearsal/ios-archive-rehearsal.json").read_text())

    def test_a_record_claiming_a_posthog_key_or_naming_one_is_refused(self):
        for changes, reason in (({"posthog_key": True}, "ios.sh's record differs from the request"),
                                ({"posthog_key": "false"}, "ios.sh's record differs from the request"),
                                ({"posthog_key": 0}, "ios.sh's record differs from the request"),
                                ({"posthog_key": False, "posthog": "phc_testLeak"}, "ios.sh's record is missing or malformed"),
                                ({"posthog_sha256": "a" * 64}, "ios.sh's record is missing or malformed")):
            with self.subTest(changes=changes):
                self.tearDown()
                self.setUp()
                self.archive(self.fake_ios_sh(record_changes=changes))
                self.assertEqual((self.summary()["result"], self.summary()["reason"]), ("failed", reason))
                self.assertNotIn("phc_testLeak", (self.root / "may-rehearsal/ios-archive-rehearsal.json").read_text())

    def test_no_secret_may_be_in_the_archive_step(self):
        for name in ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY", "MAY_APP_PR_READ_TOKEN", "MAY_ASC_KEY_BASE64",
                     "MAY_POSTHOG_IOS_KEY", "MAY_POSTHOG_ANDROID_KEY"):
            with self.subTest(secret=name), self.assertRaises(StoreError):
                self.archive(self.fake_ios_sh(), environ=dict(self.environ, **{name: "secret"}))
        self.assertEqual(self.seen, [])

    def test_inputs_are_exact(self):
        self.assertEqual(ios_rehearsal.inputs("production", "a" * 40, "2", "116"), "io.github.mayassistant.may")
        for args in (("other", "a" * 40, "2", "116"), ("playground", "A" * 40, "2", "116"),
                     ("playground", "a" * 39, "2", "116"), ("playground", "a" * 40, "0", "116"),
                     ("playground", "a" * 40, "2000000001", "116"), ("playground", "a" * 40, "02", "116"),
                     ("playground", "a" * 40, "2", "0"), ("playground", "a" * 40, "2", "1/2")):
            with self.subTest(args=args), self.assertRaises(RehearsalError):
                ios_rehearsal.inputs(*args)


class SummaryCheckTests(RehearsalFixture):
    def run_step(self, step, environ=None):
        out = io.StringIO()
        with patch.dict(os.environ, environ or self.environ, clear=True), patch("sys.stdout", out):
            step("playground", self.sha, "7", "116")
        return out.getvalue()

    def write(self, value):
        (self.root / "may-rehearsal/ios-archive-rehearsal.json").write_text(json.dumps(value))

    def test_a_good_summary_is_rewritten_and_passes_the_verdict(self):
        self.archive(self.fake_ios_sh())
        good = self.summary()
        self.write(good)
        self.assertIn("summary checked", self.run_step(ios_rehearsal.record))
        self.assertEqual(self.summary(), good)
        self.assertIn("rehearsal passed", self.run_step(ios_rehearsal.verdict))

    def test_a_failed_rehearsal_publishes_its_summary_and_then_fails_the_job(self):
        self.archive(self.fake_ios_sh(status=6))
        self.run_step(ios_rehearsal.record)
        with self.assertRaises(RehearsalError):
            self.run_step(ios_rehearsal.verdict)

    def test_a_summary_that_is_not_exactly_its_shape_is_never_published(self):
        self.archive(self.fake_ios_sh())
        good = self.summary()
        tampered = {
            "an extra key": dict(good, log="ios.sh: refused: private detail"),
            "a missing key": {k: v for k, v in good.items() if k != "signing"},
            "a reason of its own": dict(good, reason="App/Shared/Secret.swift:1: error"),
            "another commit": dict(good, source_sha="b" * 40),
            "another build number": dict(good, build_number="8"),
            "another target's app": dict(good, app_id="io.github.mayassistant.may"),
            "an archived result without a digest": dict(good, archive_tree_sha256=None),
            "a digest that is not one": dict(good, archive_tree_sha256="private text"),
            "a failed result holding a digest": dict(good, reason="ios.sh exited non-zero", ios_sh_exit=7, result="failed"),
            "an exit status that is text": dict(good, ios_sh_exit="7 private"),
            "an archive said to be uploaded": dict(good, archive_uploaded=True),
            "not an object": [good],
        }
        for case, value in tampered.items():
            with self.subTest(case=case):
                self.write(value)
                with self.assertRaises(RehearsalError) as caught:
                    self.run_step(ios_rehearsal.record)
                self.assertIn("not exactly its fixed shape", caught.exception.args[0])
                with self.assertRaises(RehearsalError):
                    self.run_step(ios_rehearsal.verdict)
        self.write(good)
        (self.root / "may-rehearsal/extra.txt").write_text("private")
        with self.assertRaises(RehearsalError) as caught:
            self.run_step(ios_rehearsal.record)
        self.assertIn("folder holds anything else", caught.exception.args[0])

    def test_a_missing_summary_is_refused(self):
        with self.assertRaises(RehearsalError):
            self.run_step(ios_rehearsal.record)
        with self.assertRaises(RehearsalError):
            self.run_step(ios_rehearsal.verdict)


class TreeDigestTests(unittest.TestCase):
    def test_every_path_type_mode_content_and_link_target_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory) / "May.xcarchive"
            (top / "a").mkdir(parents=True)
            (top / "a/file").write_text("one")
            os.symlink("a", top / "link")
            digests = {ios_rehearsal.tree_sha256(top)}
            (top / "a/file").write_text("two")
            digests.add(ios_rehearsal.tree_sha256(top))
            (top / "a/file").chmod(0o755)
            digests.add(ios_rehearsal.tree_sha256(top))
            (top / "link").unlink()
            os.symlink("a/file", top / "link")
            digests.add(ios_rehearsal.tree_sha256(top))
            (top / "b").mkdir()
            digests.add(ios_rehearsal.tree_sha256(top))
            self.assertEqual(len(digests), 5)
            self.assertEqual(ios_rehearsal.tree_sha256(top), ios_rehearsal.tree_sha256(top))
            os.mkfifo(top / "fifo")
            with self.assertRaises(RehearsalError):
                ios_rehearsal.tree_sha256(top)

    def test_xcode_must_be_exactly_the_pinned_version_and_build(self):
        class Done:
            def __init__(self, stdout, returncode=0):
                self.stdout, self.returncode = stdout, returncode
        for stdout, returncode, expected in ((b"Xcode 26.3\nBuild version 17C529\n", 0, True),
                                             (b"Xcode 26.3\nBuild version 17C530\n", 0, False),
                                             (b"Xcode 26.4\nBuild version 17C529\n", 0, False),
                                             (b"Xcode 26.3\nBuild version 17C529\n", 1, False), (b"", 0, False)):
            with self.subTest(stdout=stdout, returncode=returncode), \
                    patch("subprocess.run", return_value=Done(stdout, returncode)) as run:
                self.assertIs(ios_rehearsal.xcode_is_pinned(), expected)
                self.assertEqual(run.call_args.kwargs["env"]["DEVELOPER_DIR"],
                                 "/Applications/Xcode_26.3.app/Contents/Developer")


class RehearsalWorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(static_check.ROOT / ".github", self.root / ".github")
        shutil.copytree(static_check.ROOT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.directory.cleanup()

    def test_the_committed_workflow_and_tool_pass(self):
        self.assertEqual(static_check.ios_rehearsal_workflow_problems((static_check.ROOT / WORKFLOW).read_text()), [])
        self.assertEqual(static_check.ios_rehearsal_print_problems(static_check.ROOT), [])

    def test_each_widening_is_flagged(self):
        name = "ios-archive-rehearsal.yml: "
        secret = name + "a secret or env entry other than the check's PR token, the fetch's two source-read keys and the dispatch's inputs"
        runs = name + "runs something other than the gate"
        publish = name + "publishes something other than exactly the rehearsal summary file"
        archive_step = "      - name: Archive with ios.sh, unsigned, without any credential\n        env:\n"
        record_step = "      - name: Check the rehearsal summary before publishing it\n        env:\n"
        fetch_env = "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n"
        summary_path = "          path: ${{ runner.temp }}/may-rehearsal/ios-archive-rehearsal.json\n"
        cases = {
            "a read key in the archive step": (archive_step, archive_step + "          MAY_APP_READ_KEY: ${{ secrets.MAY_APP_READ_KEY }}\n", secret),
            "the PR token in the archive step": (archive_step, archive_step + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "GH_TOKEN in the archive step": (archive_step, archive_step + "          GH_TOKEN: ${{ github.token }}\n", secret),
            "a read key in the summary check": (record_step, record_step + "          MAY_DS_READ_KEY: ${{ secrets.MAY_DS_READ_KEY }}\n", secret),
            "the PR token in the fetch step": (fetch_env, fetch_env + "          MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n", secret),
            "a signing secret anywhere": (fetch_env, fetch_env + "          MAY_IOS_CERT_BASE64: ${{ secrets.MAY_IOS_CERT_BASE64 }}\n", secret),
            "a job-level env": ("    timeout-minutes: 90\n", "    timeout-minutes: 90\n    env:\n      CI: '1'\n", secret),
            "the archive uploaded": (summary_path, "          path: ${{ runner.temp }}/may-release/May.xcarchive\n", publish),
            "the private folder uploaded": (summary_path, "          path: ${{ runner.temp }}/may-private/\n", publish),
            "a glob uploaded": (summary_path, "          path: ${{ runner.temp }}/may-rehearsal/*\n", publish),
            "a missing summary tolerated": ("          if-no-files-found: error\n", "          if-no-files-found: ignore\n", publish),
            "a second upload": ("      - name: Fail unless ios.sh archived",
                "      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n        with:\n"
                "          name: log\n          path: ${{ runner.temp }}/may-private/ios-rehearsal.log\n"
                "      - name: Fail unless ios.sh archived", publish),
            "no summary check before the upload": ("python3 -I -S -B tools/may_release/ios_rehearsal.py record",
                "python3 -I -S -B tools/may_release/ios_rehearsal.py verdict", runs),
            "the log printed": ("      - name: Fail unless ios.sh archived",
                "      - name: Show\n        run: cat \"$RUNNER_TEMP/may-private/ios-rehearsal.log\"\n"
                "      - name: Fail unless ios.sh archived", runs),
            "the main-history fetch instead": ('--source-sha "$MAY_SOURCE_SHA" --pr-number "$MAY_PR_NUMBER"\n      - name: Archive',
                '--source-sha "$MAY_SOURCE_SHA"\n      - name: Archive', runs),
            "the check without --check-pull": ('--pr-number "$MAY_PR_NUMBER" --check-pull\n', '--pr-number "$MAY_PR_NUMBER"\n', runs),
            "an input interpolated into shell": ('ios_rehearsal.py archive --target "$MAY_TARGET"',
                "ios_rehearsal.py archive --target '${{ inputs.target }}'", runs),
            "a step summary": ("          retention-days: 7\n", "          retention-days: 7\n"
                "      - name: Summary\n        run: echo done >> \"$GITHUB_STEP_SUMMARY\"\n",
                name + "is not the macOS iOS rehearsal with no step summary"),
            "a verdict that may be skipped": ("      - name: Fail unless ios.sh archived and every check passed\n",
                "      - name: Fail unless ios.sh archived and every check passed\n        continue-on-error: true\n",
                name + "sets a shell, defaults, a condition"),
            "a cleanup that is not always run": ("        if: always()\n", "", name + "rehearsal's steps are not exactly"),
            "a store environment": ("    environment: ${{ needs.admission.outputs.environment }}\n",
                "    environment: may-ios-playground-internal\n", name + "rehearsal's environment is not the one admission chose"),
            "a push trigger": ("on:\n  workflow_dispatch:\n", "on:\n  push:\n  workflow_dispatch:\n",
                name + "is not dispatched with exactly pr_number, source_sha, target and build_number"),
            "write permission": ("permissions:\n  contents: read\nconcurrency:", "permissions:\n  contents: write\nconcurrency:",
                name + "permissions are not exactly read-only"),
            "a linux runner": ("    runs-on: macos-15\n", "    runs-on: ubuntu-24.04\n", name + "is not the macOS iOS rehearsal"),
            "a shared concurrency group": ("  group: may-ios-archive-rehearsal\n", "  group: may-native-compile-pr\n",
                name + "does not run one at a time in its own concurrency group"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                path = self.root / WORKFLOW
                text = path.read_text()
                self.assertIn(old, text, "mutation anchor not found")
                path.write_text(text.replace(old, new, 1))
                found = static_check.problems(self.root)
                self.assertTrue(any(message in problem for problem in found), (case, found))

    def test_the_tool_prints_only_fixed_lines_and_the_exit_status(self):
        tool = "tools/may_release/ios_rehearsal.py"
        finished = 'print(f"May iOS archive rehearsal finished; ios.sh exit status {int(status)}; its log stays on this runner")'
        cases = {
            "a printed log": (finished, finished + "\n    print(log.read_text())", "prints something other than a fixed line"),
            "a printed record": (finished, 'print(f"record {record}")', "prints something other than a fixed line"),
            "ios.sh writing to this log": ("stdout=output,\n                         stderr=subprocess.STDOUT, ",
                                           "", "runs a subprocess that may inherit this log"),
            "sys.stdout": (finished, finished + "\n    sys.stdout.write(str(record))", "writes to stdout or stderr other than"),
            "a formatted refusal": ('raise RehearsalError("tools/release/ios.sh is not in this may-app commit")',
                                    'raise RehearsalError("missing: " + str(script))', "printed error message is not one string literal"),
            "an exit built from data": ('raise SystemExit("May iOS archive rehearsal failed; no private detail was published") from None',
                                        'raise SystemExit("failed: " + str(sys.exc_info()))', "exits with a message that is not a fixed literal"),
            "a step summary": ('RECORD_DIR, RECORD = "may-rehearsal", "ios-archive-rehearsal.json"',
                               'RECORD_DIR, RECORD = "may-rehearsal", "ios-archive-rehearsal.json"\nSUMMARY = "GITHUB_STEP_SUMMARY"',
                               "writes a GitHub Actions file or workflow command"),
        }
        for case, (old, new, message) in cases.items():
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                path = self.root / tool
                text = path.read_text()
                self.assertIn(old, text, "mutation anchor not found")
                path.write_text(text.replace(old, new, 1))
                found = static_check.problems(self.root)
                self.assertTrue(any(message in problem for problem in found), (case, found))


if __name__ == "__main__":
    unittest.main()
