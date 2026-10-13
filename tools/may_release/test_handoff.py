import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import handoff
from store import StoreError

SHA = "a" * 40
HAS_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))
RUN = {"GITHUB_REPOSITORY": "May-Assistant/may-releases", "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1"}


def system_tool(name, relative):
    return shutil.which(Path(relative).name)


def expected(build="9", run="123456", platform="android", attempt="1"):
    with patch.dict(os.environ, dict(RUN, GITHUB_RUN_ID=run, GITHUB_RUN_ATTEMPT=attempt)):
        return handoff.expected_manifest(platform, "playground", SHA, build)


def entry(name, kind="file", data=b"x", link="", mode=0o644):
    return (name, kind, data, link, mode)


class TarRulesTests(unittest.TestCase):
    """The plaintext rules, without encryption. Nothing may land outside its bundle."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.count = 0

    def tearDown(self):
        self.directory.cleanup()

    def tar(self, manifest, members):
        self.count += 1
        path = self.root / ("handoff-%d.tar" % self.count)
        types = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                 "hard": tarfile.LNKTYPE, "chr": tarfile.CHRTYPE, "blk": tarfile.BLKTYPE,
                 "fifo": tarfile.FIFOTYPE}
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            for name, kind, payload, link, mode in members:
                info = tarfile.TarInfo(name)
                info.type, info.linkname, info.mode = types[kind], link, mode
                if kind == "file":
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    archive.addfile(info)
        return path

    def destination(self):
        self.count += 1
        path = self.root / ("out-%d" % self.count)
        path.mkdir()
        return path

    def outside_files(self, destination):
        return [p for p in self.root.rglob("*") if p.is_file() and not p.name.startswith("handoff-")
                and destination not in p.parents]

    def test_matching_android_manifest_yields_product(self):
        out = self.destination()
        path = self.tar(expected(), [entry("product/unsigned.aab", data=b"bundle")])
        product = handoff.read_tar(path, out, expected(), allow_symlinks=False)
        self.assertEqual(product.read_bytes(), b"bundle")
        self.assertEqual(oct(product.stat().st_mode & 0o777), oct(0o600))

    def test_ios_archive_with_internal_bundle_symlinks_is_extracted(self):
        app = "product/May.xcarchive/Products/Applications/May.app"
        fw = app + "/Frameworks/Kit.framework"
        members = [entry("product/May.xcarchive", "dir"), entry("product/May.xcarchive/Products", "dir"),
                   entry("product/May.xcarchive/Products/Applications", "dir"), entry(app, "dir"),
                   entry(app + "/May", data=b"binary", mode=0o755), entry(app + "/Frameworks", "dir"),
                   entry(fw, "dir"), entry(fw + "/Versions", "dir"), entry(fw + "/Versions/A", "dir"),
                   entry(fw + "/Versions/A/Kit", data=b"lib"), entry(fw + "/Versions/A/Headers", "dir"),
                   entry(fw + "/Versions/A/Headers/Kit.h", data=b"h"), entry(fw + "/Versions/A/Resources", "dir"),
                   entry(fw + "/Versions/Current", "sym", link="A"),
                   entry(fw + "/Kit", "sym", link="Versions/Current/Kit"),
                   entry(fw + "/Headers", "sym", link="Versions/Current/Headers"),
                   entry(fw + "/Resources", "sym", link="Versions/Current/Resources")]
        out = self.destination()
        product = handoff.read_tar(self.tar(expected(platform="ios"), members), out, expected(platform="ios"),
                                   allow_symlinks=True)
        binary = product / "Products/Applications/May.app/May"
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        kit = product / "Products/Applications/May.app/Frameworks/Kit.framework"
        self.assertEqual((kit / "Kit").read_bytes(), b"lib")
        self.assertEqual((kit / "Headers/Kit.h").read_bytes(), b"h")
        self.assertTrue((kit / "Versions/Current").is_symlink())

    def test_mismatched_run_target_source_or_build_is_refused(self):
        for name, manifest in (("build", expected(build="10")), ("run", expected(run="999")),
                               ("earlier-attempt", expected(attempt="2")),
                               ("platform", dict(expected(), platform="ios")),
                               ("source", dict(expected(), source_sha="b" * 40)),
                               ("extra-field", dict(expected(), note="x"))):
            with self.subTest(name=name):
                path = self.tar(manifest, [entry("product/unsigned.aab")])
                with self.assertRaises(StoreError):
                    handoff.read_tar(path, self.destination(), expected(), allow_symlinks=False)

    def test_unexpected_members_are_refused(self):
        ios = expected(platform="ios")
        root = "product/May.xcarchive"
        app = root + "/Products/Applications/May.app"
        parents = [entry(root, "dir"), entry(root + "/Products", "dir"),
                   entry(root + "/Products/Applications", "dir"), entry(app, "dir")]
        cases = {
            # The reviewer's reproduction: resolves to $RUNNER_TEMP/may-tools/SECRET after the move.
            "escape-symlink": (ios, parents + [entry(root + "/a", "dir"),
                                               entry(root + "/a/esc", "sym", link="../../../may-tools/SECRET")]),
            # Samuel's reproduction: lands on the signing .p12 once moved into may-release.
            "leak-to-signing-p12": (ios, parents + [entry(app + "/leak", "sym",
                                                          link="../../../../../may-private/ios-signing/distribution.p12")]),
            "chain-escape": (ios, parents + [entry(app + "/a", "sym", link="b"),
                                             entry(app + "/b", "sym", link="../../escape")]),
            "chain-through-dir-link": (ios, parents + [entry(app + "/up", "sym", link=".."),
                                                       entry(app + "/x", "sym", link="up/../../../../x")]),
            "dangling": (ios, parents + [entry(app + "/gone", "sym", link="Missing/Thing")]),
            "loop": (ios, parents + [entry(app + "/p", "sym", link="q"), entry(app + "/q", "sym", link="p")]),
            "absolute-symlink": (ios, parents + [entry(app + "/x", "sym", link="/etc/passwd")]),
            "symlink-out-of-own-bundle": (ios, parents + [entry(app + "/x", "sym", link="../../Other")]),
            "write-through-symlink": (ios, parents + [entry(app + "/d", "sym", link="."),
                                                      entry(app + "/d/f", data=b"x")]),
            "hard-link": (ios, parents + [entry(app + "/h", "hard", link=app + "/May")]),
            "absolute-path": (expected(), [entry("/product/unsigned.aab")]),
            "dot-dot": (expected(), [entry("product/unsigned.aab"), entry("product/unsigned.aab/../../escape")]),
            "not-normal": (expected(), [entry("product//unsigned.aab")]),
            "char-device": (ios, parents + [entry(app + "/dev", "chr")]),
            "block-device": (ios, parents + [entry(app + "/dev", "blk")]),
            "fifo": (ios, parents + [entry(app + "/pipe", "fifo")]),
            "android-symlink": (expected(), [entry("product/unsigned.aab", "sym", link="unsigned.aab")]),
            "other-product": (expected(), [entry("product/unsigned.aab"), entry("product/other.aab")]),
            "outside-product": (expected(), [entry("product/unsigned.aab"), entry("evil.sh")]),
            "duplicate": (expected(), [entry("product/unsigned.aab"), entry("product/unsigned.aab")]),
            "missing-parent": (ios, [entry(root, "dir"), entry(app + "/May")]),
            "empty": (expected(), []),
            "android-dir": (expected(), [entry("product/unsigned.aab", "dir")]),
        }
        for name, (manifest, members) in cases.items():
            with self.subTest(name=name):
                out = self.destination()
                with self.assertRaises(StoreError):
                    handoff.read_tar(self.tar(manifest, members), out, manifest,
                                     allow_symlinks=manifest["platform"] == "ios")
                self.assertEqual(self.outside_files(out), [])
                self.assertFalse((self.root / "may-tools").exists())
                self.assertFalse((self.root / "escape").exists())

    def test_validation_alone_refuses_bad_links_before_writing(self):
        root = "product/May.xcarchive"
        app = root + "/Products/Applications/May.app"
        parents = [entry(root, "dir"), entry(root + "/Products", "dir"),
                   entry(root + "/Products/Applications", "dir"), entry(app, "dir")]
        for name, links in (("leak", [entry(app + "/leak", "sym", link="../../../../../may-private/ios-signing/distribution.p12")]),
                            ("chain", [entry(app + "/a", "sym", link="b"), entry(app + "/b", "sym", link="../../escape")]),
                            ("dangling", [entry(app + "/gone", "sym", link="Missing")]),
                            ("other-bundle", [entry(app + "/x", "sym", link="..")]),
                            ("through-link", [entry(app + "/d", "sym", link="."), entry(app + "/d/f")]),
                            ("hard-link", [entry(app + "/h", "hard", link=root)])):
            with self.subTest(name=name), tarfile.open(self.tar(expected(platform="ios"), parents + links)) as archive:
                with self.assertRaises(StoreError):
                    handoff.checked_members(archive, root, True)
        # Even a valid internal link is refused when symlinks are not allowed (Android).
        valid = parents + [entry(app + "/May", data=b"b"), entry(app + "/alias", "sym", link="May")]
        with tarfile.open(self.tar(expected(platform="ios"), valid)) as archive:
            handoff.checked_members(archive, root, True)
            with self.assertRaises(StoreError):
                handoff.checked_members(archive, root, False)

    def test_names_that_alias_on_a_case_insensitive_filesystem_are_refused(self):
        root = "product/May.xcarchive"
        app = root + "/Products/Applications/May.app"
        parents = [entry(root, "dir"), entry(root + "/Products", "dir"),
                   entry(root + "/Products/Applications", "dir"), entry(app, "dir")]
        cases = {
            "case-link-vs-dir": [entry(app + "/Real", "dir"), entry(app + "/link", "sym", link="Real"),
                                 entry(app + "/LINK", "dir")],
            "case-write-under-link": [entry(app + "/Real", "dir"), entry(app + "/link", "sym", link="Real"),
                                      entry(app + "/LINK/x", data=b"x")],
            "nfc-vs-nfd": [entry(app + "/caf\u00e9", data=b"a"), entry(app + "/cafe\u0301", data=b"b")],
            "case-files": [entry(app + "/Info.plist"), entry(app + "/info.plist")],
            "link-target-aliases-member": [entry(app + "/Real", "dir"), entry(app + "/Real/f"),
                                           entry(app + "/l", "sym", link="REAL/f")],
        }
        for name, members in cases.items():
            with self.subTest(name=name), tarfile.open(self.tar(expected(platform="ios"), parents + members)) as archive:
                with self.assertRaises(StoreError) as refused:
                    handoff.checked_members(archive, root, True)
                self.assertIn("alias", refused.exception.args[0])

    def test_missing_handoff_says_to_rerun_all_jobs(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, dict(RUN, RUNNER_TEMP=directory, MAY_ARTIFACT_DECRYPT_KEY="AGE-SECRET-KEY-1" + "Q" * 58)), \
                patch("handoff.artifact_recipient", return_value="age1" + "q" * 58):
            with self.assertRaises(StoreError) as refused:
                handoff.open_handoff("android", "playground", SHA, "9")
        self.assertEqual(refused.exception.args[0],
                         "Re-run all jobs: this release only accepts a build from the same run attempt")

    def test_parent_chain_is_resolved_before_every_write(self):
        """Validation bypassed: extract alone must not write through a link in any component."""
        root = "product/May.xcarchive"
        cases = {
            "file-under-escaping-link": [entry(root, "dir"), entry(root + "/a", "dir"),
                                         entry(root + "/a/l", "sym", link="../../.."),
                                         entry(root + "/a/l/planted.py", data=b"evil")],
            "dir-under-escaping-link": [entry(root, "dir"), entry(root + "/a", "dir"),
                                        entry(root + "/a/l", "sym", link="../../.."),
                                        entry(root + "/a/l/planted", "dir")],
        }
        for name, members in cases.items():
            with self.subTest(name=name), tarfile.open(self.tar(expected(platform="ios"), members)) as archive:
                out = self.destination()
                with self.assertRaises(StoreError):
                    handoff.extract(archive, archive.getmembers()[1:], root, out)
                self.assertFalse((self.root / "planted.py").exists())
                self.assertFalse((self.root / "planted").exists())

    def test_case_insensitive_resolution_through_an_aliased_link_is_refused(self):
        """Model APFS on Linux: a resolver that matches names case- and normalization-insensitively."""
        def folding_resolve(path):
            current, pending, hops = Path("/"), list(Path(path).parts[1:]), 0
            while pending:
                part = pending.pop(0)
                names = {handoff.fold(n): n for n in os.listdir(current)} if current.is_dir() else {}
                actual = part if (current / part).exists() or (current / part).is_symlink() else names.get(handoff.fold(part))
                if actual is None:
                    raise FileNotFoundError(part)
                candidate = current / actual
                if candidate.is_symlink():
                    hops += 1
                    if hops > 40:
                        raise RuntimeError("loop")
                    pending = list(Path(os.readlink(candidate)).parts) + pending
                    continue
                current = Path(os.path.normpath(candidate))
            return current

        root = "product/May.xcarchive"
        for name, last in (("file", entry(root + "/a/LINK/x", data=b"x")), ("mkdir", entry(root + "/a/LINK/sub", "dir"))):
            members = [entry(root, "dir"), entry(root + "/a", "dir"), entry(root + "/a/link", "sym", link="../../.."),
                       last]
            with self.subTest(name=name), tarfile.open(self.tar(expected(platform="ios"), members)) as archive:
                with self.assertRaises(StoreError) as refused:
                    handoff.extract(archive, archive.getmembers()[1:], root, self.destination(), folding_resolve)
                self.assertIn("outside its own folder", refused.exception.args[0])

    def test_symlink_is_rechecked_where_it_finally_lives(self):
        # If validation were bypassed, the post-extraction realpath check still refuses.
        ios = expected(platform="ios")
        root = "product/May.xcarchive"
        members = [entry(root, "dir"), entry(root + "/a", "dir"),
                   entry(root + "/a/esc", "sym", link="../../../may-tools/SECRET")]
        (self.root / "may-tools").mkdir()
        (self.root / "may-tools/SECRET").write_text("mock secret")  # exists, so strict resolve succeeds
        for link in ("../../../may-tools/SECRET", "../missing"):  # escapes to a real file; dangling in-bundle
            with self.subTest(link=link), tarfile.open(self.tar(ios, members[:2] + [
                    entry(root + "/a/esc", "sym", link=link)])) as archive:
                with self.assertRaises(StoreError):
                    handoff.extract(archive, archive.getmembers()[1:], root, self.destination())


# Skipped only off CI; in CI a missing age makes these fail rather than vanish.
@unittest.skipUnless(HAS_AGE or os.environ.get("CI"), "age and age-keygen are required for the round trip")
class AgeRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        keys = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        self.keyfile = keys  # what the setup lane pastes: the whole age-keygen output
        self.secret = next(l for l in keys.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
        self.public = subprocess.run(["age-keygen", "-y"], input=self.secret, capture_output=True,
                                     text=True, check=True).stdout.strip()
        (self.root / "may-release").mkdir()
        (self.root / "may-release/unsigned.aab").write_bytes(b"unsigned bundle")
        self.patches = [patch.dict(os.environ, dict(RUN, RUNNER_TEMP=self.directory.name, PATH=os.environ["PATH"]),
                                   clear=True),
                        patch("handoff.toolchain.tool_path", side_effect=system_tool),
                        patch("handoff.artifact_recipient", return_value=self.public)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def carry(self):
        """Simulate the artifact hop to a fresh release VM."""
        download = self.root / "may-handoff-download"
        download.mkdir()
        shutil.move(str(self.root / "may-handoff/handoff.age"), str(download / "handoff.age"))
        shutil.rmtree(self.root / "may-release")
        shutil.rmtree(self.root / "may-handoff")

    def test_round_trip_restores_exact_product_and_leaves_no_key(self):
        handoff.seal("android", "playground", SHA, "9")
        sealed = (self.root / "may-handoff/handoff.age").read_bytes()
        self.assertNotIn(b"unsigned bundle", sealed)
        self.assertEqual(sorted(p.name for p in (self.root / "may-handoff").iterdir()), ["handoff.age"])
        self.carry()
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.keyfile):
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertEqual((self.root / "may-release/unsigned.aab").read_bytes(), b"unsigned bundle")
        self.assertEqual(sorted(p.name for p in (self.root / "may-private").iterdir()), [])

    def test_ios_archive_round_trip_keeps_internal_symlinks(self):
        shutil.rmtree(self.root / "may-release")
        fw = self.root / "may-release/May.xcarchive/Products/Applications/May.app/Frameworks/Kit.framework"
        (fw / "Versions/A").mkdir(parents=True)
        (fw / "Versions/A/Kit").write_bytes(b"lib")
        os.symlink("A", fw / "Versions/Current")
        os.symlink("Versions/Current/Kit", fw / "Kit")
        handoff.seal("ios", "playground", SHA, "9")
        download = self.root / "may-handoff-download"
        download.mkdir()
        shutil.move(str(self.root / "may-handoff/handoff.age"), str(download / "handoff.age"))
        shutil.rmtree(self.root / "may-release")
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.keyfile):
            handoff.open_handoff("ios", "playground", SHA, "9")
        kit = self.root / "may-release/May.xcarchive/Products/Applications/May.app/Frameworks/Kit.framework/Kit"
        self.assertTrue(kit.is_symlink())
        self.assertEqual(kit.read_bytes(), b"lib")

    def test_plaintext_file_is_private_before_age_writes_it(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        modes, real_run = [], subprocess.run

        def watch(argv, **kwargs):
            if "-d" in argv:
                plain = Path(argv[argv.index("-o") + 1])
                modes.append(plain.stat().st_mode & 0o777 if plain.exists() else None)
            return real_run(argv, **kwargs)

        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.keyfile), \
                patch("handoff.subprocess.run", side_effect=watch):
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertEqual(modes, [0o600])

    def test_other_build_number_or_run_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        for changes, build in (({}, "10"), ({"GITHUB_RUN_ID": "654321"}, "9")):
            with self.subTest(changes=changes, build=build), \
                    patch.dict(os.environ, dict(changes, MAY_ARTIFACT_DECRYPT_KEY=self.secret)):
                shutil.rmtree(self.root / "may-release", ignore_errors=True)
                with self.assertRaises(StoreError):
                    handoff.open_handoff("android", "playground", SHA, build)
                self.assertFalse((self.root / "may-release/unsigned.aab").exists())

    def test_wrong_key_or_tampered_ciphertext_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        other = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
        other_secret = next(l for l in other.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=other_secret), self.assertRaises(StoreError) as refused:
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertIn("does not match this target's committed public key", refused.exception.args[0])
        sealed = self.root / "may-handoff-download/handoff.age"
        data = bytearray(sealed.read_bytes())
        data[-20] ^= 0x01
        sealed.write_bytes(bytes(data))
        shutil.rmtree(self.root / "may-release", ignore_errors=True)
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.secret), self.assertRaises(StoreError):
            handoff.open_handoff("android", "playground", SHA, "9")
        self.assertFalse((self.root / "may-release/unsigned.aab").exists())

    def test_malformed_decrypt_key_is_refused(self):
        handoff.seal("android", "playground", SHA, "9")
        self.carry()
        for value in ("", self.secret + "\n" + self.secret, self.public, "AGE-SECRET-KEY-1short"):
            with self.subTest(value=value[:20]), patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=value):
                shutil.rmtree(self.root / "may-release", ignore_errors=True)
                with self.assertRaises(StoreError):
                    handoff.open_handoff("android", "playground", SHA, "9")

    def test_steps_refuse_other_secrets_and_placeholders(self):
        with patch.dict(os.environ, MAY_PLAY_JSON="x"), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")
        with patch.dict(os.environ, MAY_ARTIFACT_DECRYPT_KEY=self.secret), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")  # the build job never holds the key
        with patch("handoff.artifact_recipient", return_value=None), self.assertRaises(StoreError):
            handoff.seal("android", "playground", SHA, "9")


if __name__ == "__main__":
    unittest.main()
