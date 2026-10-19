import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import plistlib

import ios_upload
from store import StoreError

SHA = "a" * 40
STARTED = 4102444800.0  # 2100-01-01T00:00:00Z
FRESH = "2100-01-01T00:01:00.000+00:00"


def item(upload_id="upload-1", build="9", state="PROCESSING", created=FRESH, nested=True, platform="IOS",
         short="1.0"):
    return {"type": "buildUploads", "id": upload_id,
            "attributes": {"cfBundleVersion": build, "cfBundleShortVersionString": short, "platform": platform,
                           "createdDate": created,
                           "state": {"state": state, "errors": [], "warnings": []} if nested else state}}


def listing(*items):
    return 200, {"data": list(items)}


class Replies:
    """get(): the replies in turn, the last repeated; records each call."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, token, url):
        self.calls.append(url)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def poll(get, build="9"):
    clock = Clock()
    return ios_upload.await_build_upload("token", "6822072535", build, "1.0", STARTED, get, clock.sleep, clock), clock


class BuildUploadReadbackTests(unittest.TestCase):
    def test_returns_an_accepted_build_upload_for_this_build_number(self):
        for state in ("PROCESSING", "COMPLETE"):
            for nested in (True, False):
                with self.subTest(state=state, nested=nested):
                    get = Replies(listing(item(build="8", state="COMPLETE"), item(state=state, nested=nested)))
                    (found, clock) = poll(get)
                    self.assertEqual(found, ("upload-1", state))
                    self.assertEqual(clock.sleeps, [])
        self.assertEqual(get.calls, ["https://api.appstoreconnect.apple.com/v1/apps/6822072535/buildUploads?"
                                     "filter%5BcfBundleVersion%5D=9&limit=200"])

    def test_waits_while_absent_awaiting_or_transient_then_accepts(self):
        get = Replies(listing(), (None, None), (503, None), (429, None), listing(item(state="AWAITING_UPLOAD")),
                      listing(item(state="PROCESSING")))
        (found, clock) = poll(get)
        self.assertEqual(found, ("upload-1", "PROCESSING"))
        self.assertEqual(clock.sleeps, [30] * 5)

    def test_absent_until_the_timeout_fails(self):
        """Run 39192306300: altool exited 0 and no build upload ever appeared."""
        with self.assertRaises(StoreError) as refused:
            poll(Replies(listing()))
        self.assertEqual(refused.exception.args[0], "No accepted App Store Connect build upload for this build "
                         "number appeared before the timeout; altool's exit status is not an upload")
        with self.assertRaises(StoreError):
            poll(Replies(listing(item(state="AWAITING_UPLOAD"))))

    def test_failed_build_upload_fails(self):
        with self.assertRaises(StoreError) as refused:
            poll(Replies(listing(item(state="FAILED"))))
        self.assertEqual(refused.exception.args[0], "App Store Connect build upload FAILED; the build was not accepted")

    def test_the_newest_upload_in_the_window_decides(self):
        older = item("upload-0", state="FAILED", created="2100-01-01T00:00:30Z")
        (found, _) = poll(Replies(listing(older, item(state="COMPLETE"))))
        self.assertEqual(found, ("upload-1", "COMPLETE"))
        newer_failed = item("upload-2", state="FAILED", created="2100-01-01T00:02:00Z")
        with self.assertRaises(StoreError) as refused:
            poll(Replies(listing(item(state="PROCESSING"), newer_failed)))
        self.assertEqual(refused.exception.args[0], "App Store Connect build upload FAILED; the build was not accepted")

    def test_other_build_numbers_and_stale_uploads_do_not_count(self):
        stale = item(created="2099-12-31T23:54:59Z")  # before the step started, beyond the clock-skew margin
        for items in ([item(build="10")], [item(build="19")], [stale], [item(created=None)],
                      [item(created="2100-01-01T00:01:00")], [item(created="yesterday")], [item(platform="MAC_OS")],
                      [item(platform=None)], [item(short="1.1")], [item(short=None)]):
            with self.subTest(items=items):
                with self.assertRaises(StoreError) as refused:
                    poll(Replies(listing(*items)))
                self.assertIn("before the timeout", refused.exception.args[0])
        (found, _) = poll(Replies(listing(item(created="2099-12-31T23:56:00Z"))))  # within the margin
        self.assertEqual(found[0], "upload-1")

    def test_refusals_and_invalid_replies_fail_at_once(self):
        for reply, message in (((401, None), "refused the build upload readback (HTTP 401 or 403)"),
                               ((403, None), "refused the build upload readback (HTTP 401 or 403)"),
                               ((404, None), "found no such app or build upload list (HTTP 404)"),
                               ((400, None), "build upload readback was rejected"),
                               ((200, None), "build upload list was invalid"),
                               ((200, {"data": {}}), "build upload list was invalid")):
            with self.subTest(reply=reply):
                with self.assertRaises(StoreError) as refused:
                    poll(Replies(reply))
                self.assertIn(message, refused.exception.args[0])

    def test_unreadable_uploads_are_skipped_and_their_field_names_named_at_the_timeout(self):
        unreadable = (["x"], [item(upload_id="bad id")], [item(state="UNKNOWN")], [item(created=None)],
                      [item(platform=None, short=7)])
        names = ("attributes", "id", "state", "createdDate", "cfBundleShortVersionString, platform")
        for items, fields in zip(unreadable, names):
            with self.subTest(fields=fields), patch("sys.stdout", new_callable=io.StringIO) as output:
                with self.assertRaises(StoreError) as refused:
                    poll(Replies(listing(*items)))
                self.assertIn("before the timeout", refused.exception.args[0])
                self.assertEqual(output.getvalue(), "App Store Connect build upload fields that could not be read: "
                                 + fields + "\n")
        with patch("sys.stdout", new_callable=io.StringIO) as output, self.assertRaises(StoreError):
            poll(Replies(listing(item(build="8", short=None))))  # another build's fields are not this upload's
        self.assertEqual(output.getvalue(), "")

    def test_requests_go_only_to_the_app_store_connect_api(self):
        for url in ("https://api.appstoreconnect.apple.com.evil.example/v1/apps", "http://api.appstoreconnect.apple.com/v1/",
                    "https://example.com/"):
            with self.subTest(url=url), self.assertRaises(StoreError):
                ios_upload.asc_get("token", url)


class TokenTests(unittest.TestCase):
    def test_der_to_jose_pads_and_strips_to_32_byte_halves(self):
        r, s = b"\x00" + b"\x80" + b"\x01" * 31, b"\x7f" * 31  # r has a sign byte; s is short
        der = b"\x30" + bytes([4 + len(r) + len(s)]) + b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
        self.assertEqual(ios_upload.der_to_jose(der), b"\x80" + b"\x01" * 31 + b"\x00" + b"\x7f" * 31)

    def test_der_to_jose_refuses_malformed_signatures(self):
        good = b"\x30\x06\x02\x01\x01\x02\x01\x02"
        self.assertEqual(ios_upload.der_to_jose(good), b"\x00" * 31 + b"\x01" + b"\x00" * 31 + b"\x02")
        for bad in (b"", good[:-1], good + b"\x00", b"\x31" + good[1:], b"\x30\x06\x03\x01\x01\x02\x01\x02",
                    b"\x30\x26\x02\x22" + b"\x01" * 34 + b"\x02\x00"):
            with self.subTest(bad=bad), self.assertRaises(StoreError):
                ios_upload.der_to_jose(bad)

    @unittest.skipUnless(Path(ios_upload.OPENSSL).is_file(), "the system openssl signs the token")
    def test_token_is_a_verifiable_es256_jwt_for_app_store_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            key, public = work / "AuthKey.p8", work / "public.pem"
            subprocess.run([ios_upload.OPENSSL, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-out", str(key)], check=True, capture_output=True)
            subprocess.run([ios_upload.OPENSSL, "pkey", "-in", str(key), "-pubout", "-out", str(public)],
                           check=True, capture_output=True)
            token = ios_upload.asc_token(key, "00000000-1111-2222-3333-444444444444", now=1000)
            header, claims, signature = token.split(".")
            decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            self.assertEqual(json.loads(decode(header)), {"alg": "ES256", "kid": "23VX669FGY", "typ": "JWT"})
            self.assertEqual(json.loads(decode(claims)), {"iss": "00000000-1111-2222-3333-444444444444", "iat": 1000,
                                                          "exp": 2140, "aud": "appstoreconnect-v1"})
            raw = decode(signature)
            self.assertEqual(len(raw), 64)

            def der_int(half):
                half = half.lstrip(b"\x00") or b"\x00"
                half = b"\x00" + half if half[0] & 0x80 else half
                return b"\x02" + bytes([len(half)]) + half

            body = der_int(raw[:32]) + der_int(raw[32:])
            (work / "sig.der").write_bytes(b"\x30" + bytes([len(body)]) + body)
            (work / "input").write_bytes((header + "." + claims).encode())
            verified = subprocess.run([ios_upload.OPENSSL, "dgst", "-sha256", "-verify", str(public), "-signature",
                                       str(work / "sig.der"), str(work / "input")], capture_output=True)
            self.assertEqual(verified.returncode, 0)

    def test_unsigned_token_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(StoreError) as refused:
            ios_upload.asc_token(Path(directory) / "missing.p8", "issuer")
        self.assertEqual(refused.exception.args[0], "App Store Connect token could not be signed")


class ShortVersionTests(unittest.TestCase):
    def ipa(self, info):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "May.ipa"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Payload/May.app/Info.plist", plistlib.dumps(info))
        return path

    def test_reads_a_valid_short_version_only(self):
        self.assertEqual(ios_upload.ipa_short_version(self.ipa({"CFBundleShortVersionString": "1.2.3"})), "1.2.3")
        for info in ({}, {"CFBundleShortVersionString": 1}, {"CFBundleShortVersionString": "1.0 beta"},
                     {"CFBundleShortVersionString": "1.2.3.4"}):
            with self.subTest(info=info), self.assertRaises(StoreError):
                ios_upload.ipa_short_version(self.ipa(info))


class AltoolExcerptTests(unittest.TestCase):
    def excerpt(self, text):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "ios-upload.log"
            if text is not None:
                log.write_text(text)
            return ios_upload.altool_excerpt(log)

    def test_prints_only_the_uuid_counts_and_code_tokens(self):
        secret_path = "/Users/runner/work/_temp/may-private/asc-keys/AuthKey_23VX669FGY.p8"
        log = "\n".join([
            "Running altool at path '/Applications/Xcode_26.3.app/Contents/SharedFrameworks/altool'...",
            "2026-10-19 10:00:00.000 *** Error: Error uploading '" + secret_path + "'.",
            "*** Error: Unable to upload: ENTITY_ERROR.ATTRIBUTE.INVALID (-19232) ITMS-90062 issuer "
            "00000000-1111-2222-3333-444444444444 key 23VX669FGY",
            "WARNING: something about Private Repo May ABCDEF DEADBEEF",
            "Delivery UUID: 0a1b2c3d-1111-2222-3333-444455556666",
            "No errors uploading."])
        lines = self.excerpt(log)
        self.assertEqual(lines, [
            "altool output (scrubbed; the full log stays private and is deleted at cleanup):",
            "  delivery UUID: 0a1b2c3d-1111-2222-3333-444455556666",
            "  lines naming an error: 2; naming a warning: 1",
            "  codes: -19232, ENTITY_ERROR.ATTRIBUTE.INVALID, ITMS-90062"])
        printed = "\n".join(lines)
        for leak in ("runner", "AuthKey", "23VX669FGY", "VX", "00000000-1111", "Private Repo", "ABCDEF", "DEADBEEF",
                     "Xcode_26.3", "/"):
            self.assertNotIn(leak, printed.replace("delivery UUID: 0a1b2c3d", ""))

    def test_missing_or_malformed_values_print_nothing_from_the_log(self):
        self.assertEqual(self.excerpt(None), ["altool output: none recorded"])
        lines = self.excerpt("Delivery UUID: /Users/runner/secret-path-that-is-36-chars\n")
        self.assertEqual(lines[1:], ["  delivery UUID: none", "  lines naming an error: 0; naming a warning: 0",
                                     "  codes: none"])

    def test_code_list_is_capped(self):
        log = "\n".join("ERROR CODE_" + chr(65 + i // 26) + chr(65 + i % 26) for i in range(30))
        self.assertTrue(self.excerpt(log)[3].endswith("(10 more)"))


if __name__ == "__main__":
    unittest.main()
