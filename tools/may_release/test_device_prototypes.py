"""The flow prototypes' expected side: paths carried into the Android report, and main-path screens shot from may-app's
own pages by the image's Chrome with no network, never failing the job."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board
import device_prototypes
import device_tests
import publish
from test_device_tests import network_tripwire

TRIPWIRE = network_tripwire()
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()


def path(board_id="2", flow="sign-in-out", steps=None):
    return {"flow": flow, "board": board_id, "pattern": "review-pages 1.1",
            "cases": [{"id": "main", "name": "Sign in, then sign out", "goal": "Sign in; sign out.", "start": None,
                       "given": None},
                      {"id": "unsent", "name": "Unsent", "goal": "The alert counts unsent messages.", "start": "x",
                       "given": "One message waits."}],
            "steps": steps if steps is not None else [
                {"n": 1, "platform": "both", "action": "assert", "target": {"id": "demo-signin"},
                 "expect": "The sign-in screen.", "step": "welcome"},
                {"n": 2, "platform": "android", "action": "tap", "target": {"id": "demo-primary", "text": "Continue"},
                 "expect": "The picker opens.", "step": "picker"},
                {"n": 2, "platform": "ios", "action": "tap", "target": {"text": "Continue"},
                 "expect": "The sheet opens.", "step": "picker"},
                {"n": 3, "platform": "ios", "action": "wait", "target": {"id": "demo-chats"},
                 "expect": "Chats.", "step": "chats"},
                {"n": 1, "platform": "both", "action": "tap", "target": {"id": "demo-out"}, "expect": "Signed out.",
                 "step": "x", "edge": "unsent", "given": {"code": "unsent", "text": "One message waits."}}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "may-source"
        (self.source / device_prototypes.PROTOTYPES / "paths").mkdir(parents=True)
        (self.source / device_prototypes.PROTOTYPES / "src").mkdir(parents=True)
        (self.source / device_prototypes.BUILD).write_text("print('built')\n")
        self.folder = self.root / "out" / "android"
        self.folder.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def write_path(self, data, name=None):
        target = self.source / device_prototypes.PROTOTYPES / "paths" / f"{name or data['flow']}.json"
        target.write_text(json.dumps(data))
        return target


class PathTests(Fixture):
    def test_only_a_path_naming_its_board_flow_is_carried(self):
        self.write_path(path())
        self.write_path(dict(path(flow="chat"), board="4.x"), "chat")        # not a flows.json id
        self.write_path(dict(path(flow="take-notes"), board=None), "take-notes")
        self.write_path(path(flow="other", board_id="3"), "renamed")         # flow is not the file's own name
        (self.source / device_prototypes.PROTOTYPES / "paths" / "linked.json").symlink_to(
            self.source / device_prototypes.PROTOTYPES / "paths" / "sign-in-out.json")
        (self.source / device_prototypes.PROTOTYPES / "paths" / "big.json").write_text(
            json.dumps(dict(path(flow="big"), pad="x" * device_prototypes.MAX_PATH)))
        self.assertEqual(device_prototypes.copy_paths(self.source, self.folder), 1)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["path-2.json"])
        self.assertEqual(json.loads((self.folder / "path-2.json").read_text())["board"], "2")

    def test_the_android_board_inputs_carry_the_paths_and_the_ios_ones_do_not(self):
        self.write_path(path())
        ios = self.root / "out" / "ios"
        ios.mkdir()
        device_tests.board_inputs(self.source, self.folder)
        device_tests.board_inputs(self.source, ios)
        self.assertIn("path-2.json", [p.name for p in self.folder.iterdir()])
        self.assertEqual(list(ios.iterdir()), [])

    def test_the_main_path_gives_each_platforms_steps_once(self):
        self.assertEqual(device_prototypes.main_steps(path()), {"android": [1, 2], "ios": [1, 2, 3]},
                         "a both step counts for each platform; an edge step and its n are left out")
        self.assertEqual(device_prototypes.main_steps({"steps": "x"}), {"android": [], "ios": []})


class FakeChrome:
    """A stand-in for Chrome: dump-dom answers with the page's ready state, a screenshot writes a PNG; it records every
    argv, and never reaches anything."""

    def __init__(self, ready=True, shot=PNG, fail_build=False):
        self.calls, self.ready, self.shot, self.fail_build = [], ready, shot, fail_build

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == "python3":
            return subprocess.CompletedProcess(argv, 1 if self.fail_build else 0)
        if "--dump-dom" in argv:
            ready = self.ready(argv[-1]) if callable(self.ready) else self.ready
            html = b'<!DOCTYPE html>\n<html lang="en" class="still"' + (b' data-ready="1"' if ready else b"") + b">"
            return subprocess.CompletedProcess(argv, 0, stdout=html, stderr=b"")
        shot = next(a for a in argv if a.startswith("--screenshot=")).split("=", 1)[1]
        Path(shot).write_bytes(self.shot(argv[-1]) if callable(self.shot) else self.shot)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


class RenderTests(Fixture):
    def render(self, chrome):
        def build(argv, **kwargs):
            if argv[0] == "python3":
                (self.source / device_prototypes.PROTOTYPES / "pages").mkdir(exist_ok=True)
            return chrome(argv, **kwargs)
        return device_prototypes.render(self.source, self.folder, self.work, self.work / "build.log",
                                        browser="/usr/bin/google-chrome", run=build)

    def test_each_main_path_step_is_shot_per_platform_with_no_network(self):
        self.write_path(path())
        device_prototypes.copy_paths(self.source, self.folder)
        chrome = FakeChrome()
        self.assertEqual(self.render(chrome), (5, 0))
        self.assertEqual(sorted(p.name for p in self.folder.glob("proto-*")), [
            "proto-2-android-1.png", "proto-2-android-2.png", "proto-2-ios-1.png", "proto-2-ios-2.png",
            "proto-2-ios-3.png"])
        build, *shots = chrome.calls
        self.assertEqual(build[0][:5], ["python3", "-I", "-S", "-B", str(self.source / device_prototypes.BUILD)])
        for argv, kwargs in shots:
            self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND", argv, "no host name resolves")
            self.assertTrue(argv[-1].startswith("file://") and "&shot=1" in argv[-1], "file:// alone, the shot mode")
            self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "LANG"}, "nothing of the runner's environment")
        dumps = [argv for argv, _ in shots if "--dump-dom" in argv]
        self.assertEqual(len(dumps), 5, "each shot is kept only after its page reports ready")
        self.assertIn("--window-size=393,852", next(a for a, _ in shots if "platform=ios" in a[-1]))

    def test_a_page_that_is_not_ready_or_a_shot_that_is_not_a_small_png_is_left_out(self):
        self.write_path(path())
        device_prototypes.copy_paths(self.source, self.folder)
        chrome = FakeChrome(ready=lambda url: "n=3" not in url,
                            shot=lambda url: b"GIF89a" if "n=1&platform=ios" in url else
                            PNG + b"\0" * device_prototypes.MAX_SHOT if "n=2&platform=ios" in url else PNG)
        self.assertEqual(self.render(chrome), (2, 3))
        self.assertEqual(sorted(p.name for p in self.folder.glob("proto-*")),
                         ["proto-2-android-1.png", "proto-2-android-2.png"])
        self.assertFalse([a for a, _ in chrome.calls if "--screenshot" in " ".join(a) and "n=3" in a[-1]],
                         "a page that is not ready is never shot")

    def test_the_runs_screens_stop_at_their_total(self):
        self.write_path(path())
        device_prototypes.copy_paths(self.source, self.folder)
        with patch("device_prototypes.SHOT_TOTAL", 2 * len(PNG)):
            made, failed = self.render(FakeChrome())
        self.assertEqual((made, failed), (2, 3))

    def test_no_path_no_build_or_no_chrome_renders_nothing(self):
        chrome = FakeChrome()
        self.assertEqual(self.render(chrome), (None, None), "no path")
        self.write_path(path())
        device_prototypes.copy_paths(self.source, self.folder)
        self.assertEqual(self.render(FakeChrome(fail_build=True)), (None, None), "the build failed")
        with patch("device_prototypes.chrome", return_value=None):
            self.assertEqual(device_prototypes.render(self.source, self.folder, self.work, self.work / "log",
                                                      run=FakeChrome()), (None, None))

    def test_the_step_never_fails_and_logs_counts_alone(self):
        out = self.root / "runner"
        (out / device_tests.OUT / "20261024-aaaaaaaaaaaa" / "android").mkdir(parents=True)
        cases = (((3, 1), "May device tests (android): 3 prototype screens rendered, 1 failed\n"),
                 ((None, None), "May device tests (android): no prototype screens (no named path, or the build or "
                                "Chrome is unavailable)\n"),
                 (OSError("boom"), "May device tests (android): no prototype screens (an error stopped them; the seal "
                                   "goes on)\n"))
        for result, line in cases:
            def rendered(*args, result=result):
                if isinstance(result, Exception):
                    raise result
                return result
            with self.subTest(line=line), patch.dict(os.environ, {"RUNNER_TEMP": str(out)}, clear=True), \
                    patch("sys.stdout", new_callable=io.StringIO) as printed:
                device_tests.render_prototypes(rendered)
                self.assertEqual(printed.getvalue(), line)


class PublishTests(unittest.TestCase):
    def test_the_report_carries_paths_and_screens_and_drops_bad_ones(self):
        for name in ("path-2.json", "path-6.2.json", "proto-2-android-12.png", "proto-15-ios-3.png"):
            self.assertTrue(publish.DEVICE_FILE.fullmatch(name), name)
        for name in ("path-x.json", "proto-2-web-1.png", "proto-2-ios-0.png", "path-2.json.png"):
            self.assertFalse(publish.DEVICE_FILE.fullmatch(name), name)


if __name__ == "__main__":
    unittest.main()
