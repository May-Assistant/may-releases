"""The flow prototypes' expected side: paths carried into the Android report by their own step, and main-path screens
shot from may-app's own pages as a user of their own, with no network, inside the step's time, never failing the
job (the coordinator's security review of #104)."""
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

import device_prototypes
import device_tests
import publish
from test_device_tests import network_tripwire

TRIPWIRE = network_tripwire()
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
# Literals, not the module's own names, so these tests fail (never error) against code without the sandbox.
RENDER_USER = "may-render"
SANDBOX = ["sudo", "-n", "unshare", "--net", "--", "sudo", "-n", "-u", RENDER_USER, "--", "env", "-i"]


def setUpModule():
    TRIPWIRE.start()


def tearDownModule():
    TRIPWIRE.stop()


def path(board_id="2", flow="sign-in-out", steps=None):
    return {"flow": flow, "board": board_id, "pattern": "review-pages 1.2",
            "cases": [{"id": "main", "name": "Sign in, then sign out", "goal": "Sign in; sign out."},
                      {"id": "unsent", "name": "Unsent", "goal": "The alert counts unsent messages."}],
            "steps": steps if steps is not None else [
                {"n": 1, "platform": "both", "action": "assert", "target": {"id": "demo-signin"},
                 "expect": "The sign-in screen."},
                {"n": 2, "platform": "android", "action": "tap", "target": {"id": "demo-primary"},
                 "expect": "The picker opens."},
                {"n": 2, "platform": "ios", "action": "tap", "target": {"text": "Continue"}, "expect": "The sheet."},
                {"n": 3, "platform": "ios", "action": "wait", "target": {"id": "demo-chats"}, "expect": "Chats."},
                {"n": 4, "platform": "android", "action": "tap", "target": {"id": "demo-out"}, "expect": "Out.",
                 "edge": "unsent", "given": {"code": "unsent", "text": "One message waits."}}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "may-source"
        for folder in ("paths", "src"):
            (self.source / device_prototypes.PROTOTYPES / folder).mkdir(parents=True)
        (self.source / device_prototypes.PROTOTYPES / "src" / "build.py").write_text("print('built')\n")
        self.folder = self.root / "out" / "android"
        self.folder.mkdir(parents=True)
        self.stage = self.root / "stage" / "prototypes"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def write_path(self, data, name=None):
        target = self.source / device_prototypes.PROTOTYPES / "paths" / f"{name or data['flow']}.json"
        target.write_text(json.dumps(data))
        return target

    def copy(self):
        result = device_prototypes.copy_paths(self.source, self.folder)
        return result[0] if isinstance(result, tuple) else result


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
        self.assertEqual(self.copy(), 1)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["path-2.json"])

    def test_two_paths_naming_one_board_are_both_dropped_and_counted(self):
        self.write_path(path())
        self.write_path(path(flow="sign-in-again"), "sign-in-again")
        self.write_path(path(flow="chat", board_id="4"), "chat")
        self.assertEqual(device_prototypes.copy_paths(self.source, self.folder), (1, 2))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["path-4.json"])

    def test_at_most_max_paths_are_read(self):
        for i in range(45):
            self.write_path(path(flow=f"flow-{i:02d}", board_id=str(i + 1)), f"flow-{i:02d}")
        self.assertEqual(self.copy(), 40)

    def test_the_paths_are_copied_by_the_prototypes_step_not_with_the_secret(self):
        """The run step holds the sign-in secret; may-app's paths are copied by the step that holds none."""
        self.write_path(path())
        device_tests.board_inputs(self.source, self.folder)
        self.assertEqual(list(self.folder.glob("path-*.json")), [], "board_inputs (the run step) copies no path")

    def test_the_main_path_gives_each_platforms_steps_once(self):
        self.assertEqual(device_prototypes.main_steps(path()), {"android": [1, 2], "ios": [1, 2, 3]},
                         "a both step counts for each platform; an edge step (android n=4) is left out")

    def test_a_malformed_path_is_dropped_alone(self):
        bad = path(flow="chat", board_id="4", steps=[{"n": 1, "platform": ["android"], "action": "tap",
                                                      "target": {}, "expect": "x"}])
        for data in (bad, path(steps="x"), path(steps=[{"n": "1", "platform": "both"}])):
            with self.subTest(data=data["steps"]):
                try:
                    with self.assertRaises(ValueError):
                        device_prototypes.main_steps(data)
                except TypeError as error:
                    self.fail(f"a malformed path raised {error!r}, which would stop every render")


class FakeHost:
    """Stands in for sudo, unshare, the build and Chrome: records every argv and its keyword arguments; dump-dom
    answers with the page's ready state, a screenshot writes a PNG; it never reaches anything."""

    def __init__(self, ready=True, shot=PNG, fail_build=False, build_seconds=0, clock=None):
        self.calls, self.ready, self.shot, self.fail_build = [], ready, shot, fail_build
        self.build_seconds, self.clock = build_seconds, clock

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if "python3" in argv:
            if self.clock:
                self.clock[0] += self.build_seconds
            if not self.fail_build:
                (Path(argv[-1]).parent.parent / "pages").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(argv, 1 if self.fail_build else 0, stdout=b"", stderr=b"")
        if "--dump-dom" in argv:
            ready = self.ready(argv[-1]) if callable(self.ready) else self.ready
            html = b'<!DOCTYPE html>\n<html lang="en" class="still"' + (b' data-ready="1"' if ready else b"") + b">"
            return subprocess.CompletedProcess(argv, 0, stdout=html, stderr=b"")
        shots = [a for a in argv if a.startswith("--screenshot=")]
        if shots:
            Path(shots[0].split("=", 1)[1]).write_bytes(self.shot(argv[-1]) if callable(self.shot) else self.shot)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


class RenderTests(Fixture):
    def render(self, host, clock=None):
        device_prototypes.copy_paths(self.source, self.folder)
        kwargs = {"browser": "/usr/bin/google-chrome", "run": host}
        if clock is not None:
            kwargs["clock"] = lambda: clock[0]
        try:
            return device_prototypes.render(self.source, self.folder, self.stage, self.scratch, **kwargs)
        except (TypeError, OSError) as error:
            self.fail(f"render takes no stage of its own and no scratch for partial shots: {error!r}")

    def processes(self, host):
        return [argv for argv, _ in host.calls if "python3" in argv or "/usr/bin/google-chrome" in argv]

    def test_may_app_code_runs_as_its_own_user_with_no_network(self):
        self.write_path(path())
        host = FakeHost()
        self.assertEqual(self.render(host), (5, 0))
        processes = self.processes(host)
        self.assertEqual(len(processes), 11, "the build and two calls per shot")
        for argv in processes:
            self.assertEqual(argv[:len(SANDBOX)], SANDBOX, "the render user, a network namespace, a clean environment")
        build = next(argv for argv in processes if "python3" in argv)
        self.assertTrue(build[-1].startswith(str(self.stage)), "the build runs from the stage, never the checkout")
        for argv in processes[1:]:
            for flag in ("--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=socks5://127.0.0.1:9",
                         "--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                         "--disable-background-networking"):
                self.assertIn(flag, argv)
            self.assertTrue(argv[-1].startswith("file://" + str(self.stage)) and "&shot=1" in argv[-1])

    def test_the_stage_holds_the_prototypes_alone_handed_to_the_render_user_and_is_removed(self):
        self.write_path(path())
        (self.source / "secret.txt").write_text("not for the render user")
        host = FakeHost()
        self.render(host)
        commands = [argv for argv, _ in host.calls]
        self.assertIn(["sudo", "-n", "chown", "-R", f"{RENDER_USER}:", str(self.stage)], commands)
        self.assertEqual(commands[-1], ["sudo", "-n", "rm", "-rf", "--", str(self.stage)], "removed at the end")

    def test_the_render_user_can_reach_the_stage(self):
        """A fresh temporary folder is 0700, the runner's alone: the render user could not open build.py in it."""
        self.write_path(path())
        self.stage.parent.mkdir(mode=0o700)
        seen = []
        host = FakeHost()

        def run(argv, **kwargs):
            if "chown" in argv:
                seen.append(self.stage.parent.stat().st_mode & 0o777)
            return host(argv, **kwargs)
        self.render(run)
        self.assertEqual(seen, [0o755])

    def test_each_call_is_bounded_and_no_shot_starts_after_the_steps_time(self):
        self.write_path(path())
        host = FakeHost()
        self.render(host)
        for argv, kwargs in host.calls:
            if "/usr/bin/google-chrome" in argv:
                self.assertEqual(kwargs.get("timeout"), device_prototypes.CALL_SECONDS)
        clock = [0.0]
        late = FakeHost(build_seconds=device_prototypes.STEP_SECONDS + 1, clock=clock)
        self.assertEqual(self.render(late, clock), (0, 5), "the clock starts before the build")
        self.assertFalse([a for a, _ in late.calls if "/usr/bin/google-chrome" in a], "no shot after the deadline")
        self.assertLessEqual(next(k["timeout"] for a, k in late.calls if "python3" in a), device_prototypes.STEP_SECONDS)

    def test_the_build_gets_no_more_than_the_steps_time_left(self):
        self.write_path(path())
        host = FakeHost()
        with patch("device_prototypes.STEP_SECONDS", 45):
            self.render(host, [0.0])
        self.assertEqual(next(k["timeout"] for a, k in host.calls if "python3" in a), 45)

    def test_a_page_that_is_not_ready_or_a_shot_that_is_not_a_small_png_is_left_out(self):
        self.write_path(path())
        host = FakeHost(ready=lambda url: "n=3" not in url,
                        shot=lambda url: b"GIF89a" if "n=1&platform=ios" in url else
                        PNG + b"\0" * device_prototypes.MAX_SHOT if "n=2&platform=ios" in url else PNG)
        self.assertEqual(self.render(host), (2, 3))
        self.assertEqual(sorted(p.name for p in self.folder.glob("proto-*")),
                         ["proto-2-android-1.png", "proto-2-android-2.png"])

    def test_a_shot_is_whole_or_absent(self):
        self.write_path(path())
        moves = []
        real = os.replace
        with patch("device_prototypes.os.replace", side_effect=lambda a, b: moves.append((Path(a), Path(b))) or real(a, b)):
            self.render(FakeHost())
        self.assertEqual(len(moves), 5, "each shot is written aside and renamed into place")
        for partial, target in moves:
            self.assertEqual(partial.parent, self.scratch, "never a half-written file in the sealed folder")
            self.assertEqual(target.parent, self.folder)
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".part")], [])

    def test_a_malformed_path_costs_only_itself(self):
        self.write_path(path())
        self.write_path(path(flow="chat", board_id="4", steps=[{"n": 1, "platform": ["android"], "action": "tap",
                                                                 "target": {}, "expect": "x"}]), "chat")
        self.assertEqual(self.render(FakeHost()), (5, 0))

    def test_the_runs_screens_stop_at_their_total(self):
        self.write_path(path())
        with patch("device_prototypes.SHOT_TOTAL", 2 * len(PNG)):
            self.assertEqual(self.render(FakeHost()), (2, 3))

    def test_no_path_no_build_or_no_chrome_renders_nothing(self):
        self.assertEqual(self.render(FakeHost()), (None, None), "no path")
        self.write_path(path())
        self.assertEqual(self.render(FakeHost(fail_build=True)), (None, None), "the build failed")
        with patch("device_prototypes.chrome", return_value=None):
            self.assertEqual(device_prototypes.render(self.source, self.folder, self.stage, self.scratch,
                                                      run=FakeHost()), (None, None))

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
                try:
                    device_tests.render_prototypes(copied=lambda *a: (0, 0), rendered=rendered)
                except TypeError as error:
                    self.fail(f"the prototypes step does not copy the paths itself: {error}")
                self.assertEqual(printed.getvalue(), line)


class PublishTests(unittest.TestCase):
    def test_the_report_carries_paths_and_screens(self):
        for name in ("path-2.json", "path-6.2.json", "proto-2-android-12.png", "proto-15-ios-3.png"):
            self.assertTrue(publish.DEVICE_FILE.fullmatch(name), name)
        for name in ("path-x.json", "proto-2-web-1.png", "proto-2-ios-0.png", "path-2.json.png",
                     ".proto-2-ios-1.png.part"):
            self.assertFalse(publish.DEVICE_FILE.fullmatch(name), name)


if __name__ == "__main__":
    unittest.main()
