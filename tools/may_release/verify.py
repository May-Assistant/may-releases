"""Dependency-free checks of signed May release artifacts.

Everything here is pure Python so the public, no-secret control check can test
it. Nothing is printed: failures raise VerifyError with a fixed message, and no
certificate subject, profile field, or file content ever reaches a public log.

The CMS/PKCS#7 reader accepts BER indefinite lengths because Apple provisioning
profiles use them; jarsigner signature blocks are plain DER.
"""

import hashlib
import plistlib
import re
import zipfile

SIGNED_DATA_OID = bytes.fromhex("06092a864886f70d010702")
SIGNATURE_BLOCK = re.compile(r"META-INF/[^/]+\.(RSA|DSA|EC)", re.IGNORECASE)
SIGNATURE_FILE = re.compile(r"META-INF/[^/]+\.SF", re.IGNORECASE)
SIGNATURE_EXTRA = re.compile(r"META-INF/SIG-[^/]+", re.IGNORECASE)
SIGNATURE_RELATED = re.compile(r"META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|DSA|EC)|SIG-[^/]+)", re.IGNORECASE)


class VerifyError(ValueError):
    pass


def _element(data, offset, end):
    """Read one BER element: (tag, content_start, content_end, next_offset)."""
    if offset + 2 > end:
        raise VerifyError("Signature structure is truncated")
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise VerifyError("Signature structure uses an unsupported tag")
    first, pos = data[offset + 1], offset + 2
    if first == 0x80:
        if not tag & 0x20:
            raise VerifyError("Signature structure is malformed")
        start = pos
        while True:
            if pos + 2 > end:
                raise VerifyError("Signature structure is truncated")
            if data[pos:pos + 2] == b"\x00\x00":
                return tag, start, pos, pos + 2
            pos = _element(data, pos, end)[3]
    if first & 0x80:
        count = first & 0x7F
        if not 1 <= count <= 4 or pos + count > end:
            raise VerifyError("Signature structure is malformed")
        length, pos = int.from_bytes(data[pos:pos + count], "big"), pos + count
    else:
        length = first
    if pos + length > end:
        raise VerifyError("Signature structure is truncated")
    return tag, pos, pos + length, pos + length


def _children(data, start, end):
    items, offset = [], start
    while offset < end:
        tag, content_start, content_end, following = _element(data, offset, end)
        items.append((tag, offset, content_start, content_end, following))
        offset = following
    return items


def _octets(data, tag, start, end):
    if tag == 0x04:
        return data[start:end]
    if tag == 0x24:  # BER constructed OCTET STRING: concatenate its segments.
        return b"".join(_octets(data, t, s, e) for t, _, s, e, _ in _children(data, start, end))
    raise VerifyError("Signed content is not an octet string")


def signed_data(blob):
    """Return certificates (raw DER), signer IDs and content of a CMS SignedData."""
    try:
        data = bytes(blob)
        top = _children(data, 0, len(data))
        if len(top) != 1 or top[0][0] != 0x30:
            raise VerifyError("Signature block is not one CMS structure")
        info = _children(data, top[0][2], top[0][3])
        if len(info) != 2 or data[info[0][1]:info[0][4]] != SIGNED_DATA_OID or info[1][0] != 0xA0:
            raise VerifyError("Signature block is not CMS SignedData")
        wrapper = _children(data, info[1][2], info[1][3])
        if len(wrapper) != 1 or wrapper[0][0] != 0x30:
            raise VerifyError("Signature block is not CMS SignedData")
        fields = _children(data, wrapper[0][2], wrapper[0][3])
        if len(fields) < 4 or fields[0][0] != 0x02 or fields[1][0] != 0x31 or fields[2][0] != 0x30:
            raise VerifyError("Signature block is not CMS SignedData")
        content = None
        encapsulated = _children(data, fields[2][2], fields[2][3])
        if len(encapsulated) == 2 and encapsulated[1][0] == 0xA0:
            inner = _children(data, encapsulated[1][2], encapsulated[1][3])
            if len(inner) != 1:
                raise VerifyError("Signed content is malformed")
            content = _octets(data, inner[0][0], inner[0][2], inner[0][3])
        certificates, signers = [], []
        for tag, _, start, end, _ in fields[3:-1]:
            if tag == 0xA0:
                certificates += [data[o:f] for t, o, _, _, f in _children(data, start, end) if t == 0x30]
            elif tag != 0xA1:
                raise VerifyError("Signature block is not CMS SignedData")
        if fields[-1][0] != 0x31:
            raise VerifyError("Signature block has no signer set")
        for tag, _, start, end, _ in _children(data, fields[-1][2], fields[-1][3]):
            parts = _children(data, start, end)
            if tag != 0x30 or len(parts) < 2 or parts[1][0] != 0x30:
                raise VerifyError("Signer identifier is unsupported")
            sid = _children(data, parts[1][2], parts[1][3])
            if len(sid) != 2 or sid[0][0] != 0x30 or sid[1][0] != 0x02:
                raise VerifyError("Signer identifier is unsupported")
            signers.append((data[sid[0][1]:sid[0][4]], data[sid[1][1]:sid[1][4]]))
        return {"certificates": certificates, "signers": signers, "content": content}
    except (IndexError, RecursionError, TypeError):
        raise VerifyError("Signature block is malformed") from None


def issuer_and_serial(certificate):
    """Return the raw issuer Name and serial INTEGER of an X.509 certificate."""
    outer = _children(certificate, 0, len(certificate))
    if len(outer) != 1 or outer[0][0] != 0x30:
        raise VerifyError("Certificate is malformed")
    parts = _children(certificate, outer[0][2], outer[0][3])
    if not parts or parts[0][0] != 0x30:
        raise VerifyError("Certificate is malformed")
    tbs = _children(certificate, parts[0][2], parts[0][3])
    if tbs and tbs[0][0] == 0xA0:
        tbs = tbs[1:]
    if len(tbs) < 3 or tbs[0][0] != 0x02 or tbs[2][0] != 0x30:
        raise VerifyError("Certificate is malformed")
    return certificate[tbs[2][1]:tbs[2][4]], certificate[tbs[0][1]:tbs[0][4]]


def sole_signer_certificate(block):
    parsed = signed_data(block)
    if len(parsed["signers"]) != 1 or len(parsed["certificates"]) != 1:
        raise VerifyError("Signature must have exactly one signer and one certificate")
    certificate = parsed["certificates"][0]
    if issuer_and_serial(certificate) != parsed["signers"][0]:
        raise VerifyError("Signer does not match the embedded certificate")
    return certificate


def normalize_fingerprint(value, digits=64):
    cleaned = re.sub(r"[\s:]", "", value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{%d}" % digits, cleaned):
        raise VerifyError("Expected certificate fingerprint is malformed")
    return cleaned


def manifest_names(raw):
    """Names of jar-manifest sections that carry an entry digest."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise VerifyError("Signed manifest is not UTF-8") from None
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith(" ") and lines and lines[-1]:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    names, sections, current = set(), [], []
    for line in lines + [""]:
        if line:
            current.append(line)
        elif current:
            sections.append(current)
            current = []
    for section in sections[1:]:
        attributes = dict(line.split(": ", 1) for line in section if ": " in line)
        if "Name" in attributes and any(key.endswith("-Digest") for key in attributes):
            names.add(attributes["Name"])
    return names


def signature_entries(path):
    with zipfile.ZipFile(path) as archive:
        return [n for n in archive.namelist() if SIGNATURE_BLOCK.fullmatch(n) or SIGNATURE_FILE.fullmatch(n)
                or SIGNATURE_EXTRA.fullmatch(n)]


def aab_signer_sha256(path):
    """SHA-256 of the sole signer certificate, after checking every entry is signed.

    jarsigner -verify checks each listed digest; this checks that the manifest
    lists every entry, so an added unsigned entry cannot ride along.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise VerifyError("Bundle has duplicate entries")
            blocks = [n for n in names if SIGNATURE_BLOCK.fullmatch(n)]
            files = [n for n in names if SIGNATURE_FILE.fullmatch(n)]
            if len(blocks) != 1 or len(files) != 1 or any(SIGNATURE_EXTRA.fullmatch(n) for n in names):
                raise VerifyError("Bundle must carry exactly one signature")
            signed = manifest_names(archive.read("META-INF/MANIFEST.MF"))
            for name in names:
                if not name.endswith("/") and not SIGNATURE_RELATED.fullmatch(name) and name not in signed:
                    raise VerifyError("Bundle has an entry outside its signature")
            block = archive.read(blocks[0])
    except (zipfile.BadZipFile, KeyError):
        raise VerifyError("Bundle is not a signed zip archive") from None
    return hashlib.sha256(sole_signer_certificate(block)).hexdigest()


def profile_plist(blob):
    content = signed_data(blob)["content"]
    if not content:
        raise VerifyError("Provisioning profile has no content")
    try:
        profile = plistlib.loads(content)
    except (plistlib.InvalidFileException, ValueError):
        raise VerifyError("Provisioning profile content is invalid") from None
    if not isinstance(profile, dict):
        raise VerifyError("Provisioning profile content is invalid")
    return profile


def check_profile(profile, team, bundle_id, certificate_sha1=None):
    """Require an App Store profile for exactly this team and bundle; return its UUID."""
    entitlements = profile.get("Entitlements")
    if profile.get("TeamIdentifier") != [team] or not isinstance(entitlements, dict):
        raise VerifyError("Provisioning profile team differs")
    if entitlements.get("application-identifier") != team + "." + bundle_id:
        raise VerifyError("Provisioning profile bundle ID differs")
    if entitlements.get("com.apple.developer.team-identifier") != team:
        raise VerifyError("Provisioning profile team differs")
    if "ProvisionedDevices" in profile or profile.get("ProvisionsAllDevices"):
        raise VerifyError("Provisioning profile is not an App Store profile")
    if entitlements.get("get-task-allow") is not False:
        raise VerifyError("Provisioning profile is not an App Store profile")
    if certificate_sha1 is not None:
        expected = normalize_fingerprint(certificate_sha1, 40)
        certificates = profile.get("DeveloperCertificates")
        if not isinstance(certificates, list) or expected not in {
                hashlib.sha1(c).hexdigest() for c in certificates if isinstance(c, bytes)}:
            raise VerifyError("Provisioning profile does not include the approved certificate")
    uuid = profile.get("UUID")
    if not isinstance(uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{36}", uuid):
        raise VerifyError("Provisioning profile UUID is invalid")
    return uuid


def ipa_identity(path, team, bundle_id, build_number):
    """Check the IPA's embedded profile team and bundle, and its bundle and build."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise VerifyError("IPA has duplicate entries")
            apps = {n.split("/")[1] for n in names
                    if n.startswith("Payload/") and n.count("/") >= 2 and n.split("/")[1].endswith(".app")}
            if len(apps) != 1:
                raise VerifyError("IPA must contain exactly one app")
            app = "Payload/" + apps.pop() + "/"
            profile = profile_plist(archive.read(app + "embedded.mobileprovision"))
            info = plistlib.loads(archive.read(app + "Info.plist"))
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException):
        raise VerifyError("IPA is not a complete app archive") from None
    check_profile(profile, team, bundle_id)
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != bundle_id:
        raise VerifyError("IPA bundle ID differs")
    if info.get("CFBundleVersion") != build_number:
        raise VerifyError("IPA build number differs")
