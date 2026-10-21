"""May Playground's iOS renders (ios-renders.yml): gate.py --action renders, prepare.py --ds-sha, renders.py and
publish.py. Everything Apple or GitHub is faked; nothing here calls a network service."""

import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import gate
from gate import GateDenial
import prepare
from prepare import PrepareError, fetch
import publish
from publish import PublishError
import renders
from renders import RendersError
import static_check
import store
from test_compile_pr import ENVIRONMENT, POLICIES, FakeResponse, pull

REPO, MAIN, DISPATCH = "May-Assistant/may-releases", "refs/heads/main", "workflow_dispatch"
SHA, DS = "a" * 40, "c" * 40
FOLDER = SHA[:12]
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/ios-renders.yml"
RECIPIENT = "age1" + "q" * 58
FRAME = renders.FRAME


def png(width=4, height=8):
    return renders.PNG + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\0\0\0" + b"rest"


class Workspace:
    """RUNNER_TEMP with a prepared may-source (its manifest and Android renders) and may-private."""

    def __init__(self, rows=None):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, self.private = self.root / "may-source", self.root / "may-private"
        self.source.mkdir()
        self.private.mkdir()
        manifest = self.source / renders.MANIFEST
        manifest.parent.mkdir(parents=True)
        self.rows = rows if rows is not None else [
            {"id": "chat-reply", "title": "Chat reply <b>", "spec": "docs/design/chat.md",
             "android": {"light": "android/chat-reply-light.png", "dark": "android/chat-reply-dark.png"},
             "ios": {"light": "ios/chat-reply-light.png", "dark": None}},
            {"id": "sign-in", "title": "Sign in", "spec": "docs/design/sign-in.md",
             "android": {"light": None, "dark": None}, "ios": {"light": None, "dark": None}}]
        manifest.write_text(json.dumps(self.rows))
        android = self.source / renders.EVIDENCE / "android"
        android.mkdir(parents=True)
        for mode in ("light", "dark"):
            (android / f"chat-reply-{mode}.png").write_bytes(png(3, 3))
        self.snapshots = self.root / renders.SNAPSHOTS
        self.env = {"RUNNER_TEMP": str(self.root), "PATH": "/usr/bin:/bin"}

    def snapshot(self, *names):
        self.snapshots.mkdir(exist_ok=True)
        for name in names:
            (self.snapshots / name).write_bytes(png())


class GateTests(unittest.TestCase):
    def run_gate(self, args, pr=None, token="p" * 20):
        answers = {"May-Assistant/may-releases/environments/may-ios-renders": ENVIRONMENT,
                   "May-Assistant/may-releases/environments/may-ios-renders/deployment-branch-policies?per_page=100": POLICIES,
                   "May-Assistant/may-app/pulls/42": pr or pull()}
        seen = []

        def urlopen(request, timeout):
            seen.append(request.full_url)
            answer = answers.get(request.full_url.split("api.github.com/repos/", 1)[1])
            if answer is None:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return FakeResponse(json.dumps(answer).encode())

        environ = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": MAIN, "GITHUB_EVENT_NAME": DISPATCH, "GH_TOKEN": "t" * 20,
                   "MAY_APP_PR_READ_TOKEN": token, "GITHUB_TRIGGERING_ACTOR": "samuelalake"}
        environ = {k: v for k, v in environ.items() if v is not None}
        with patch.dict(os.environ, environ, clear=True), patch("urllib.request.urlopen", urlopen), \
                patch.object(sys, "argv", ["gate.py"] + args), patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                gate.main()
                denial = None
            except GateDenial as error:
                denial = error.args[0]
        return denial, seen, out.getvalue()

    def test_a_main_commit_or_a_pull_request_head_is_admitted_in_its_own_environment(self):
        self.assertEqual(gate.validate_run(REPO, MAIN, DISPATCH, SHA, "renders"), "may-ios-renders")
        denial, seen, out = self.run_gate(["--action", "renders", "--source-sha", SHA])
        self.assertIsNone(denial)
        self.assertEqual(out.strip(), f"May renders admission passed for {SHA} (a may-app main commit; dispatched by "
                                      "samuelalake)")
        denial, seen, out = self.run_gate(["--action", "renders", "--source-sha", SHA, "--pr-number", "42",
                                           "--ds-sha", DS, "--check-pull"])
        self.assertIsNone(denial)
        self.assertIn("https://api.github.com/repos/May-Assistant/may-app/pulls/42", seen)
        self.assertEqual(out.strip(), f"May renders pull request check passed for {SHA} (may-app #42; design system "
                                      f"{DS}; dispatched by samuelalake)")

    def test_without_the_pull_request_token_the_check_is_skipped_and_the_fetch_still_pins_the_head(self):
        denial, seen, out = self.run_gate(["--action", "renders", "--source-sha", SHA, "--pr-number", "42",
                                           "--check-pull"], token=None)
        self.assertIsNone(denial)
        self.assertNotIn("https://api.github.com/repos/May-Assistant/may-app/pulls/42", seen)
        self.assertEqual(out.strip(), "May renders pull request check skipped: no MAY_APP_PR_READ_TOKEN in "
                                      f"may-ios-renders; the fetch still requires the head of may-app #42 to be {SHA}")

    def test_refusals(self):
        cases = ((["--check-pull"], "Only a pull request's renders read a pull request"),
                 (["--ds-sha", "main"], "A design-system commit is given only with renders"),
                 (["--pr-number", "42", "--check-pull"], "Source SHA is not the pull request's current head"))
        for extra, message in cases:
            with self.subTest(extra=extra):
                denial, _, _ = self.run_gate(["--action", "renders", "--source-sha", SHA] + extra,
                                             pr=pull(head__sha="b" * 40))
                self.assertIn(message, denial or "")
        denial, _, _ = self.run_gate(["--action", "compile", "--source-sha", SHA, "--ds-sha", DS])
        self.assertIn("A design-system commit is given only with renders", denial or "")

    def test_a_reviewer_on_the_renders_environment_is_refused(self):
        with self.assertRaises(GateDenial):
            gate.validate_no_reviewer_environment(dict(ENVIRONMENT, protection_rules=[{"type": "required_reviewers"}]),
                                                  POLICIES)


class DesignSystemOverrideTests(unittest.TestCase):
    def fetch_bases(self, ancestor):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "may-private"
            private.mkdir()
            calls = []

            def status(argv, env=None):
                return 0 if "--is-ancestor" not in argv else (0 if argv[-1] == ancestor else 1)

            with patch("prepare.quiet", side_effect=lambda argv, log, env: calls.append(argv)), \
                    patch("prepare.git_status", side_effect=status):
                try:
                    fetch(Path(directory) / "ds", "git@example.invalid:x.git", DS, "fixture read key", private, "ds",
                          bases=prepare.DS_BASES)
                    error = None
                except PrepareError as raised:
                    error = raised.args[0]
        return error, calls

    def test_the_override_must_be_in_main_or_claude_may_integration(self):
        error, calls = self.fetch_bases("refs/remotes/origin/main")
        self.assertIsNone(error)
        fetched = [argv for argv in calls if "fetch" in argv][0]
        self.assertEqual(fetched[-2:], ["+refs/heads/claude/may-integration:refs/remotes/origin/claude/may-integration",
                                        "+refs/heads/main:refs/remotes/origin/main"])
        error, calls = self.fetch_bases("refs/remotes/origin/elsewhere")
        self.assertEqual(error, "Design-system SHA is not in main or claude/may-integration history")
        self.assertFalse(any("checkout" in argv for argv in calls))

    def test_the_override_is_for_the_playground_renders_only(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(PrepareError) as refused:
            prepare.prepare(SHA, "ios", "production", ds_sha=DS)
        self.assertEqual(refused.exception.args[0], "A design-system override is for the iOS Playground renders only")


class SimulatorTests(unittest.TestCase):
    UDID_A, UDID_B = "11111111-2222-3333-4444-555555555555", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

    def tool(self, answers):
        calls = []

        def run(argv, log, env=None, capture=False):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, answers[argv[3]].encode() if argv[2] == "list" else
                                               answers["create"].encode(), b"")
        return run, calls

    def test_the_newest_ios_iphone_16_pro_is_used(self):
        devices = {"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-18-6": [{"name": "iPhone 16 Pro", "udid": self.UDID_A}],
            "com.apple.CoreSimulator.SimRuntime.iOS-26-2": [{"name": "iPhone 16 Pro", "udid": self.UDID_B},
                                                            {"name": "iPhone 17", "udid": self.UDID_A}],
            "com.apple.CoreSimulator.SimRuntime.watchOS-26-0": [{"name": "iPhone 16 Pro", "udid": self.UDID_A}]}}
        run, calls = self.tool({"devices": json.dumps(devices)})
        self.assertEqual(renders.simulator(Path(os.devnull), {}, run), self.UDID_B)
        self.assertEqual(len(calls), 1)

    def test_one_is_created_on_the_newest_ios_runtime_when_there_is_none(self):
        runtimes = {"runtimes": [
            {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-2", "isAvailable": True},
            {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-3", "isAvailable": False},
            {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-6", "isAvailable": True}]}
        run, calls = self.tool({"devices": json.dumps({"devices": {}}), "runtimes": json.dumps(runtimes),
                                "create": self.UDID_A + "\n"})
        self.assertEqual(renders.simulator(Path(os.devnull), {}, run), self.UDID_A)
        self.assertEqual(calls[-1], ["xcrun", "simctl", "create", "May iPhone 16 Pro", renders.DEVICE_TYPE,
                                     "com.apple.CoreSimulator.SimRuntime.iOS-26-2"])
        run, _ = self.tool({"devices": "{}", "runtimes": json.dumps(runtimes), "create": "not a udid"})
        with self.assertRaises(RendersError):
            renders.simulator(Path(os.devnull), {}, run)


class ManifestAndCountTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()

    def tearDown(self):
        self.work.directory.cleanup()

    def test_rows_are_checked_against_fixed_shapes(self):
        self.assertEqual([row["id"] for row in renders.manifest_rows(self.work.source)], ["chat-reply", "sign-in"])
        good = dict(self.work.rows[0])
        for change in ({"id": "Chat Reply"}, {"ios": {"light": "../x.png"}}, {"android": {"light": "../../etc/passwd.png"}},
                       {"android": {"light": "/abs/path.png"}}, {"android": {"light": "x.jpg"}},
                       {"android": {"sepia": "a.png"}}, {"android": "a.png"}, {"title": ""}, {"spec": 7}):
            with self.subTest(change=change):
                (self.work.source / renders.MANIFEST).write_text(json.dumps([dict(good, **change)]))
                with self.assertRaises(RendersError):
                    renders.manifest_rows(self.work.source)
        (self.work.source / renders.MANIFEST).write_text(json.dumps([good, good]))
        with self.assertRaises(RendersError):
            renders.manifest_rows(self.work.source)

    def test_counts_are_printed_and_nothing_from_may_app(self):
        self.work.snapshot("chat-reply-light.png", "sign-in-light.png", "stray.png")
        (self.work.snapshots / "sign-in-light.png").write_bytes(png(*FRAME))
        (self.work.snapshots / "chat-reply-dark.png").write_bytes(b"not a png")
        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            renders.check(SHA)
        self.assertEqual(out.getvalue().strip(), "May iOS renders: 2 of 4 expected renders made (light 2 of 2, dark 0 "
                                                 "of 2); 1 not the 1179 x 2556 frame; 1 other files in the snapshot "
                                                 "folder, not published; Android renders found for 2 of 4")
        for leak in ("chat", "Chat", "sign", "docs/design", "android/"):
            self.assertNotIn(leak, out.getvalue())

    def test_an_android_path_out_of_the_checkout_is_not_used(self):
        outside = self.work.root / "outside.png"
        outside.write_bytes(png())
        link = self.work.source / renders.EVIDENCE / "android/chat-reply-light.png"
        link.unlink()
        link.symlink_to(outside)
        self.assertIsNone(renders.android_render(self.work.source, renders.manifest_rows(self.work.source)[0], "light"))
        self.assertIsNotNone(renders.android_render(self.work.source, renders.manifest_rows(self.work.source)[0], "dark"))


class SheetSealVerdictTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()
        self.work.snapshot("chat-reply-light.png", "chat-reply-dark.png", "sign-in-light.png", "sign-in-dark.png")
        (self.work.private / "ios-renders.log").write_text("Compiling MayPlaygroundSnapshots\n" * 3)
        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO):
            renders.check(SHA)

    def tearDown(self):
        self.work.directory.cleanup()

    def sheet(self, stitch_ok=True):
        def stitch(argv, log):
            if stitch_ok:
                Path(argv[3]).write_bytes(png(10, 8))
            return subprocess.CompletedProcess(argv, 0 if stitch_ok else 1, b"", b"")

        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            renders.sheet(SHA, DS, build_stitcher=lambda private, log: Path("/fake/stitch"), tool=stitch)
        return self.work.root / renders.OUT / FOLDER, out.getvalue()

    def test_the_contact_sheet_holds_every_row_and_escapes_may_app_text(self):
        out, printed = self.sheet()
        self.assertEqual(printed.strip(), "May iOS renders: contact sheet of 4 rows, 2 stitched side by side")
        self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob("*.png")), [
            "android/chat-reply-dark.png", "android/chat-reply-light.png", "ios/chat-reply-dark.png",
            "ios/chat-reply-light.png", "ios/sign-in-dark.png", "ios/sign-in-light.png", "sheet/chat-reply-dark.png",
            "sheet/chat-reply-light.png"])
        page = (out / "index.html").read_text()
        self.assertIn("Chat reply &lt;b&gt;", page)
        self.assertNotIn("<b>", page)
        self.assertIn('<img src="ios/sign-in-light.png" alt="iOS">', page)
        self.assertEqual(json.loads((out / "screens.json").read_text())[0]["id"], "chat-reply")
        self.assertTrue((out / "build.log").is_file(), "the build log is in the run folder")
        self.assertEqual((out / "build.log").read_text(), "Compiling MayPlaygroundSnapshots\n" * 3)
        self.assertNotIn("chat", printed)

    def test_a_failed_stitch_or_a_linked_render_is_left_out(self):
        outside = self.work.root / "outside.png"
        outside.write_bytes(png())
        (self.work.snapshots / "sign-in-light.png").unlink()
        (self.work.snapshots / "sign-in-light.png").symlink_to(outside)
        out, printed = self.sheet(stitch_ok=False)
        self.assertEqual(printed.strip(), "May iOS renders: contact sheet of 4 rows, 0 stitched side by side")
        self.assertFalse((out / "ios/sign-in-light.png").exists())
        self.assertEqual(list((out / "sheet").iterdir()), [])

    def test_seal_with_a_placeholder_key_seals_nothing(self):
        self.sheet()
        def age(argv, **kwargs):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"sealed to nobody")
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, self.work.env, clear=True), \
                patch("renders.toolchain.tool_path", return_value="/fake/age"), \
                patch("renders.subprocess.run", side_effect=age), patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                renders.seal(SHA)
            except RendersError as error:
                self.fail("an unset recipient is reported, not refused: " + error.args[0])
        self.assertIn("MAY_RENDERS_RECIPIENT is not set in may-ios-renders; nothing was sealed", out.getvalue())
        self.assertFalse((self.work.root / renders.SEALED).exists())

    def test_seal_encrypts_one_tar_of_the_run_folder_and_keeps_no_plaintext(self):
        self.sheet()
        seen = {}

        def age(argv, **kwargs):
            with tarfile.open(argv[-1]) as archive:
                seen["names"] = archive.getnames()
            seen["argv"] = argv
            Path(argv[argv.index("-o") + 1]).write_bytes(b"age-encryption.org/v1 fixture")
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, dict(self.work.env, MAY_RENDERS_RECIPIENT=RECIPIENT), clear=True), \
                patch("renders.toolchain.tool_path", return_value="/fake/age"), \
                patch("renders.subprocess.run", side_effect=age), patch("sys.stdout", new_callable=io.StringIO):
            renders.seal(SHA)
        self.assertEqual(seen["argv"][:3], ["/fake/age", "-r", RECIPIENT])
        self.assertIn(f"{FOLDER}/index.html", seen["names"])
        self.assertIn(f"{FOLDER}/build.log", seen["names"])
        self.assertTrue(all(n == FOLDER or n.startswith(FOLDER + "/") for n in seen["names"]))
        self.assertEqual(sorted(p.name for p in (self.work.root / renders.SEALED).iterdir()), ["renders.age"])

    def test_verdict(self):
        def outcome(tests):
            (self.work.private / renders.OUTCOME).unlink(missing_ok=True)
            (self.work.private / renders.OUTCOME).write_text(json.dumps({"tests": tests}))

        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO):
            outcome("passed")
            renders.verdict(SHA)
            outcome("failed")
            with self.assertRaises(RendersError) as refused:
                renders.verdict(SHA)
            self.assertEqual(refused.exception.args[0], "The snapshot tests failed")
            outcome("passed")
            record = json.loads((self.work.private / renders.CHECKED).read_text())
            record["expected"][0]["rendered"] = False
            (self.work.private / renders.CHECKED).write_text(json.dumps(record))
            with self.assertRaises(RendersError) as refused:
                renders.verdict(SHA)
            self.assertEqual(refused.exception.args[0], "Some expected renders are missing")


class NoManifestTests(unittest.TestCase):
    """A commit without a usable manifest still publishes its renders and build log, and the verdict fails."""

    def setUp(self):
        self.work = Workspace()
        self.work.snapshot("chat-reply-light.png", "chat-reply-dark.png", "sign-in-light.png", "stray.png",
                           "Upper-light.png")
        (self.work.private / "ios-renders.log").write_text("Compiling MayPlaygroundSnapshots\n")
        (self.work.private / renders.OUTCOME).write_text(json.dumps({"tests": "passed"}))

    def tearDown(self):
        self.work.directory.cleanup()

    def run_steps(self, manifest_text):
        path = self.work.source / renders.MANIFEST
        if manifest_text is None:
            path.unlink()
        else:
            path.write_text(manifest_text)
        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            renders.check(SHA)
            renders.sheet(SHA, None, build_stitcher=lambda private, log: None)
            with self.assertRaises(RendersError) as refused:
                renders.verdict(SHA)
        return self.work.root / renders.OUT / FOLDER, out.getvalue(), refused.exception.args[0]

    def test_missing_or_malformed_manifest(self):
        for label, text in (("missing", None), ("not json", "{"), ("bad row", json.dumps([{"id": "Bad Id"}]))):
            with self.subTest(label=label):
                shutil.rmtree(self.work.root / renders.OUT, ignore_errors=True)
                (self.work.private / renders.CHECKED).unlink(missing_ok=True)
                out, printed, refusal = self.run_steps(text)
                self.assertEqual(printed.splitlines()[:2], [
                    "May iOS renders: may-app's screens manifest is missing or malformed; the renders in the snapshot "
                    "folder are published without it, and the verdict fails",
                    "May iOS renders: 3 of 4 expected renders made (light 2 of 2, dark 1 of 2); 3 not the 1179 x 2556 "
                    "frame; 2 other files in the snapshot folder, not published; Android renders found for 0 of 4"])
                self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob("*.png")), [
                    "ios/chat-reply-dark.png", "ios/chat-reply-light.png", "ios/sign-in-light.png"])
                self.assertEqual((out / "build.log").read_text(), "Compiling MayPlaygroundSnapshots\n")
                self.assertFalse((out / "screens.json").exists(), "no stand-in manifest is published as may-app's")
                self.assertIn("screens manifest was missing or malformed", (out / "index.html").read_text())
                self.assertEqual(refusal, "may-app's screens manifest is missing or malformed")

    def test_a_usable_manifest_is_not_flagged(self):
        with patch.dict(os.environ, self.work.env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as out:
            renders.check(SHA)
        self.assertNotIn("manifest", out.getvalue())
        self.assertIs(json.loads((self.work.private / renders.CHECKED).read_text())["manifest"], True)


def tar_of(entries):
    """A tar of (name, data or None for a folder, type) entries, as bytes."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, "../../../etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


GOOD = [(FOLDER, None, "dir"), (f"{FOLDER}/ios", None, "dir"), (f"{FOLDER}/ios/chat-reply-light.png", png(), "file"),
        (f"{FOLDER}/screens.json", b"[]", "file"), (f"{FOLDER}/build.log", b"log", "file"),
        (f"{FOLDER}/index.html", b"<!doctype html><img src='ios/chat-reply-light.png'>", "file")]


class PublishOpenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def extract(self, entries):
        path = self.root / "renders.tar"
        path.write_bytes(tar_of(entries))
        tree = self.root / "tree"
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir()
        with tarfile.open(path) as archive:
            publish.extract(archive, publish.checked_members(archive, FOLDER), tree)
        return tree

    def test_only_the_run_folder_layout_is_extracted(self):
        tree = self.extract(GOOD)
        self.assertEqual((tree / f"{FOLDER}/ios/chat-reply-light.png").read_bytes(), png())
        self.assertEqual((tree / f"{FOLDER}/build.log").read_bytes(), b"log")
        bad = {
            "other folder": GOOD + [("bbbbbbbbbbbb", None, "dir")],
            "escape": GOOD + [(f"{FOLDER}/../../x.png", png(), "file")],
            "unknown kind": GOOD + [(f"{FOLDER}/logs", None, "dir")],
            "not a png name": GOOD + [(f"{FOLDER}/ios/run.sh", b"x", "file")],
            "symlink": GOOD + [(f"{FOLDER}/ios/link.png", None, "sym")],
            "no sheet": [e for e in GOOD if not e[0].endswith("index.html")],
            "orphan file": GOOD + [(f"{FOLDER}/sheet/a.png", png(), "file")],
            "not a png": GOOD + [(f"{FOLDER}/ios/fake.png", b"GIF89a", "file")],
            "script": [e for e in GOOD if not e[0].endswith("index.html")] +
                      [(f"{FOLDER}/index.html", b"<script>alert(1)</script>", "file")],
        }
        for label, entries in bad.items():
            with self.subTest(label=label):
                try:
                    self.extract(entries)
                except PublishError:
                    continue
                except Exception as error:
                    self.fail(f"{label}: refused only by accident ({type(error).__name__})")
                self.fail(f"{label}: extracted")

    def test_nothing_sealed_publishes_nothing(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_DECRYPT_KEY": "x"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.open_renders(SHA)
            self.assertNotIn("MAY_RENDERS_DECRYPT_KEY", os.environ)
        self.assertEqual(out.getvalue().strip(), "May iOS renders: no sealed renders from this run attempt; nothing will "
                                                 "be published")

    def test_a_key_that_is_not_the_committed_one_is_refused_before_decrypting(self):
        (self.root / "may-renders-download").mkdir()
        (self.root / publish.DOWNLOAD).write_bytes(b"sealed")
        calls = []

        def age(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"age1" + b"p" * 58 + b"\n", b"")

        key = "AGE-SECRET-KEY-1" + "Q" * 58
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_RENDERS_DECRYPT_KEY": key,
                                     "MAY_RENDERS_RECIPIENT": RECIPIENT}, clear=True), \
                patch("publish.toolchain.tool_path", side_effect=lambda tool, name: "/fake/" + name), \
                patch("publish.subprocess.run", side_effect=age), self.assertRaises(PublishError) as refused:
            publish.open_renders(SHA)
        self.assertEqual(refused.exception.args[0], "The renders decryption key does not match MAY_RENDERS_RECIPIENT")
        self.assertEqual([argv[0] for argv in calls], ["/fake/age-keygen"])
        self.assertFalse(any(p.name.endswith(".key") for p in self.root.rglob("*")))


class PublishPushTests(unittest.TestCase):
    KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 120 + "\n-----END OPENSSH PRIVATE KEY-----"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        tree = self.root / publish.TREE / FOLDER
        (tree / "ios").mkdir(parents=True)
        (tree / "index.html").write_text("<!doctype html>")
        (tree / "ios" / "a.png").write_bytes(png())

    def tearDown(self):
        self.directory.cleanup()

    def push(self, existing_runs=None, branch_exists=True, env=None):
        calls, work = [], self.root / "may-publish-git"

        def git(argv, log, env, cwd, capture=False):
            calls.append(argv)
            if argv[0] == "init":
                Path(argv[-1]).mkdir()
            if argv[0] == "checkout" and existing_runs is not None:
                (work / "runs.json").write_text(json.dumps(existing_runs))
                for run in existing_runs:
                    (work / run["folder"]).mkdir(exist_ok=True)
            return b"abc\trefs/heads/main\n" if branch_exists and argv[0] == "ls-remote" else b""

        environ = {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": self.KEY, "GITHUB_RUN_ID": "123"}
        environ.update(env or {})
        with patch.dict(os.environ, {k: v for k, v in environ.items() if v is not None}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            publish.push(SHA, "42", run_git=git)
            self.assertNotIn("MAY_APP_RENDERS_WRITE_KEY", os.environ)
        return calls, work, out.getvalue()

    def test_pushes_only_may_renders_main_and_never_by_force(self):
        runs = [{"folder": "%012x" % n} for n in range(1, 11)]
        calls, work, out = self.push(existing_runs=runs)
        pushes = [argv for argv in calls if argv[0] == "push"]
        self.assertEqual(pushes, [["push", "--quiet", "origin", "HEAD:refs/heads/main"]])
        self.assertIn(["remote", "add", "origin", "git@github.com:May-Assistant/may-renders.git"], calls)
        self.assertFalse(any("--force" in argv or "-f" in argv or any(a.startswith("+") for a in argv[1:])
                             for argv in calls if argv[0] == "push"))
        kept = json.loads((work / "runs.json").read_text())
        self.assertEqual([run["folder"] for run in kept], [FOLDER] + ["%012x" % n for n in range(1, 10)])
        self.assertEqual(kept[0], {"folder": FOLDER, "source_sha": SHA, "pull": 42, "run": "123"})
        self.assertIn(["rm", "-r", "-q", "--ignore-unmatch", "--", "%012x" % 10], calls)
        self.assertFalse((work / ("%012x" % 10)).exists())
        self.assertTrue((work / FOLDER / "ios" / "a.png").is_file())
        self.assertEqual(out.strip(), f"May iOS renders published to may-renders as {FOLDER}/ (10 run folders kept, "
                                      "1 removed)")
        self.assertFalse(any(p.name.endswith(".key") or p.name == "known_hosts" for p in self.root.rglob("*")))

    def test_main_is_created_when_may_renders_is_empty(self):
        calls, work, _ = self.push(branch_exists=False)
        self.assertIn(["checkout", "-q", "--orphan", "renders"], calls)
        self.assertFalse(any(argv[0] == "fetch" for argv in calls))
        self.assertIn("never by hand", (work / "README.md").read_text())

    def test_without_the_key_nothing_is_pushed(self):
        calls, _, out = self.push(env={"MAY_APP_RENDERS_WRITE_KEY": None})
        self.assertEqual(calls, [])
        self.assertIn("MAY_APP_RENDERS_WRITE_KEY is not set in may-ios-renders; nothing was pushed", out)
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": "not a key"}, clear=True), \
                self.assertRaises(PublishError):
            publish.push(SHA, None, run_git=lambda *a, **k: b"")

    def test_another_steps_secret_is_refused(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_RENDERS_WRITE_KEY": self.KEY,
                                     "MAY_APP_READ_KEY": "x"}, clear=True), self.assertRaises(store.StoreError):
            publish.push(SHA, None, run_git=lambda *a, **k: b"")


class PublishCommentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        tree = self.root / publish.TREE / FOLDER / "ios"
        tree.mkdir(parents=True)
        (tree / "a.png").write_bytes(png())

    def tearDown(self):
        self.directory.cleanup()

    def comment(self, pr="42", head=SHA, comments=()):
        calls = []

        def api(method, path, payload, token):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/pulls/42"):
                return 200, pull(head__sha=head)
            if method == "GET":
                return 200, list(comments)
            return 201, {}

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "MAY_APP_PR_COMMENT_TOKEN": "c" * 30,
                                     "GITHUB_RUN_ID": "77"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                publish.comment(SHA, pr, call_api=api)
                error = None
            except PublishError as raised:
                error = raised.args[0]
        return calls, out.getvalue(), error

    def test_one_comment_links_the_run_folder(self):
        calls, out, error = self.comment()
        self.assertIsNone(error)
        body = [payload for method, _, payload in calls if method == "POST"][0]["body"]
        self.assertIn(f"https://github.com/May-Assistant/may-renders/tree/main/{FOLDER}", body)
        self.assertIn(f"https://github.com/May-Assistant/may-renders/blob/main/{FOLDER}/build.log", body)
        self.assertIn(f"<!-- may-ios-renders head={SHA} -->", body)
        self.assertIn("1 images", body)
        self.assertEqual(out.strip(), "May iOS renders linked on may-app #42")

    def test_no_pull_request_or_a_moved_head_posts_nothing(self):
        calls, out, _ = self.comment(pr=None)
        self.assertEqual(calls, [])
        self.assertIn("not a pull request's renders; no comment posted", out)
        calls, _, error = self.comment(head="b" * 40)
        self.assertEqual(error, "The pull request is no longer open at the rendered head; nothing was posted")
        self.assertFalse(any(method == "POST" for method, _, _ in calls))
        calls, out, _ = self.comment(comments=[{"body": f"<!-- may-ios-renders head={SHA} -->"}])
        self.assertFalse(any(method == "POST" for method, _, _ in calls))
        self.assertIn("already linked", out)

    def test_without_the_comment_token_the_comment_is_skipped_and_nothing_fails(self):
        calls = []
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                publish.comment(SHA, "42", call_api=lambda *a: calls.append(a))
            except PublishError as error:
                self.fail("a missing comment token failed the step: " + error.args[0])
        self.assertEqual(calls, [])
        self.assertEqual(out.getvalue().strip(), "May iOS renders: comment skipped: no token "
                                                 "(MAY_APP_PR_COMMENT_TOKEN is not set in may-ios-renders)")

    def test_only_the_pull_request_and_its_comments_are_reachable(self):
        with self.assertRaises(PublishError):
            publish.github("GET", "/repos/May-Assistant/may-app/contents/x", None, "t")


class WorkflowPinTests(unittest.TestCase):
    """ios-renders.yml is pinned whole by static_check.renders_workflow_problems."""

    def setUp(self):
        self.text = (ROOT / WORKFLOW).read_text()

    def flagged(self, old, new, message):
        self.assertEqual(self.text.count(old), 1, old)
        problems = static_check.renders_workflow_problems(self.text.replace(old, new))
        self.assertTrue(any(message in p for p in problems), problems)

    def test_the_committed_workflow_passes(self):
        self.assertEqual(static_check.renders_workflow_problems(self.text), [])
        self.assertEqual(static_check.renders_print_problems(ROOT), [])

    def test_secrets_stay_in_their_one_step(self):
        self.flagged("          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}\n", "",
                     "a secret or env entry other than")
        self.flagged('''      - name: Run the snapshot tests in the simulator without any credential
        env:
''', '''      - name: Run the snapshot tests in the simulator without any credential
        env:
          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}
''', "a secret or env entry other than")

    def test_each_publish_secret_is_in_its_own_step(self):
        write = "          MAY_APP_RENDERS_WRITE_KEY: ${{ secrets.MAY_APP_RENDERS_WRITE_KEY }}\n"
        decrypt = "          MAY_RENDERS_DECRYPT_KEY: ${{ secrets.MAY_RENDERS_DECRYPT_KEY }}\n"
        moved = self.text.replace(write, "").replace(decrypt, decrypt + write)
        self.assertTrue(any("a secret or env entry other than" in p
                            for p in static_check.renders_workflow_problems(moved)))

    def test_only_the_encrypted_file_is_handed_over(self):
        self.flagged("${{ runner.temp }}/may-renders-sealed/renders.age", "${{ runner.temp }}/may-renders-out",
                     "hands over something other than exactly the one encrypted renders file")
        self.flagged("          retention-days: 1\n", "          retention-days: 90\n",
                     "hands over something other than exactly the one encrypted renders file")

    def test_runs_and_conditions_are_pinned(self):
        self.flagged('renders.py check --source-sha "$MAY_SOURCE_SHA"', 'renders.py check --source-sha "$MAY_SOURCE_SHA"; env',
                     "runs something other than")
        self.flagged("        if: inputs.pr_number != ''\n", "", "a condition or a timeout other than its own")
        self.flagged("    runs-on: macos-15\n", "    runs-on: self-hosted\n", "render on macOS")

    def test_static_check_runs_it(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            shutil.copytree(ROOT / ".github", copy / ".github")
            shutil.copytree(ROOT / "tools", copy / "tools", ignore=shutil.ignore_patterns("__pycache__"))
            path = copy / WORKFLOW
            path.write_text(path.read_text().replace("retention-days: 1\n", "retention-days: 5\n"))
            self.assertTrue(any(p.startswith("ios-renders.yml: hands over") for p in static_check.problems(copy)))


class SecretNameTests(unittest.TestCase):
    def test_the_write_key_is_refused_by_every_other_step(self):
        self.assertIn("MAY_APP_RENDERS_WRITE_KEY", store.SECRET_NAMES)
        with patch.dict(os.environ, {"MAY_APP_RENDERS_WRITE_KEY": "x"}, clear=True), self.assertRaises(store.StoreError):
            store.require_step_secrets(store.SOURCE_READ_SECRETS)

    def test_the_decrypt_key_is_refused_by_every_other_step(self):
        self.assertIn("MAY_RENDERS_DECRYPT_KEY", store.SECRET_NAMES)
        self.assertNotIn("may-ios-renders.age.pub", [p.name for p in gate.ARTIFACT_KEYS.iterdir()])

    def test_the_recipient_reaches_only_the_seal_and_the_decrypt(self):
        text = (ROOT / WORKFLOW).read_text()
        line = "          MAY_RENDERS_RECIPIENT: ${{ vars.MAY_RENDERS_RECIPIENT }}\n"
        step = "      - name: Run the snapshot tests in the simulator without any credential\n        env:\n"
        self.assertEqual(text.count(step), 1)
        moved = text.replace(line, "", 1).replace(step, step + line)
        self.assertTrue(any("a secret or env entry other than" in p
                            for p in static_check.renders_workflow_problems(moved)))


if __name__ == "__main__":
    unittest.main()
