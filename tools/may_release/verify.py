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


# The bundle's own manifest, which bundletool turns into every APK's: aapt2's
# protobuf XmlNode (frameworks/base/tools/aapt2/Resources.proto). Only what the
# release side must verify is decoded: the root <manifest> element's `package`
# and android:versionCode. Field numbers used:
#   XmlNode:      element = 1
#   XmlElement:   name = 3, attribute = 4 (repeated)
#   XmlAttribute: namespace_uri = 1, name = 2, value = 3, source = 4,
#                 resource_id = 5, compiled_item = 6
#   Item:         prim = 7, str = 2;  Primitive: int_decimal_value = 6
# bundletool reads the version code as the first attribute whose resource ID is
# android:versionCode's (0x0101021b), whatever its name, from its compiled
# primitive, and the package from the compiled string when there is one. So the
# identity read here must be the only reading possible: exactly one attribute
# carries that ID, and it is android:versionCode with exactly one compiled
# primitive; nothing carries versionCodeMajor's ID or name; `package` has no
# compiled item or resource ID (aapt2 writes it as a plain string). A uint32 or
# int32 field wider than its width is refused, never truncated: protobuf parsers
# keep only the low bits, so a wider value would read differently there.
MANIFEST_ENTRY = "base/manifest/AndroidManifest.xml"
ANDROID_NS = "http://schemas.android.com/apk/res/android"
MAX_MANIFEST_BYTES = 4 << 20
# A real manifest's message has tens of fields; a pathological one is refused at once.
MAX_PROTO_FIELDS = 10_000
VERSION_CODE_ID = 0x0101021B
VERSION_CODE_MAJOR_ID = 0x01010576


def _varint(data, offset):
    value = shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, offset
    raise VerifyError("Bundle manifest is not a readable aapt2 manifest")


def _fields(data):
    """[(field number, wire type, value)] of one protobuf message; groups and bad lengths refused."""
    found, offset = [], 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            size, offset = _varint(data, offset)
            if offset + size > len(data):
                raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
            value, offset = data[offset:offset + size], offset + size
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if offset + width > len(data):
                raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
            value, offset = data[offset:offset + width], offset + width
        else:
            raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
        found.append((number, wire, value))
        if len(found) > MAX_PROTO_FIELDS:
            raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
    return found


def _one(fields, number, wire, required=True):
    """The single value of a non-repeated field; a repeated or mistyped one is refused, never last-wins."""
    values = [v for n, w, v in fields if n == number]
    if len(values) > 1 or any(w != wire for n, w, _ in fields if n == number) or (required and not values):
        raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
    return values[0] if values else None


def _text(fields, number):
    value = _one(fields, number, 2, required=False)
    try:
        return "" if value is None else value.decode("utf-8")
    except UnicodeDecodeError:
        raise VerifyError("Bundle manifest is not a readable aapt2 manifest") from None


def manifest_identity(data):
    """(package, versionCode as a decimal string) from a bundle's proto AndroidManifest.xml.

    Refused unless the root is one <manifest> element with exactly one `package`
    and exactly one android:versionCode whose string value and compiled integer
    agree, and no android:versionCodeMajor (which would change the effective
    version code).
    """
    element = _fields(_one(_fields(data), 1, 2))
    if _text(element, 3) != "manifest":
        raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
    packages, versions = [], []
    for number, wire, raw in element:
        if number != 4:
            continue
        if wire != 2:
            raise VerifyError("Bundle manifest is not a readable aapt2 manifest")
        attribute = _fields(raw)
        namespace, name = _text(attribute, 1), _text(attribute, 2)
        resource_id = _one(attribute, 5, 0, required=False)
        if resource_id is not None and resource_id > 0xFFFFFFFF:
            # A uint32 field: protobuf parsers keep only its low 32 bits, and aapt2 never writes wider.
            raise VerifyError("Bundle manifest resource ID is wider than 32 bits")
        if resource_id == VERSION_CODE_MAJOR_ID or name == "versionCodeMajor":
            raise VerifyError("Bundle manifest sets versionCodeMajor")
        if (resource_id == VERSION_CODE_ID) != ((namespace, name) == (ANDROID_NS, "versionCode")):
            raise VerifyError("Bundle manifest versionCode is not exactly android:versionCode")
        if (namespace, name) == ("", "package"):
            if resource_id is not None or _one(attribute, 6, 2, required=False) is not None:
                raise VerifyError("Bundle manifest package is not a plain string")
            packages.append(_text(attribute, 3))
        elif resource_id == VERSION_CODE_ID:
            text = _text(attribute, 3)
            item = _fields(_one(attribute, 6, 2))
            primitive = _fields(_one(item, 7, 2))
            compiled = _one(primitive, 6, 0)
            if compiled > 0x7FFFFFFF:
                # An int32 field: a wider varint (or a negative value) would not read back as this number.
                raise VerifyError("Bundle manifest versionCode is malformed")
            if len(item) != 1 or len(primitive) != 1 or not re.fullmatch(r"[1-9][0-9]{0,9}", text) or \
                    int(text) != compiled:
                raise VerifyError("Bundle manifest versionCode is malformed")
            versions.append(text)
    if len(packages) != 1 or len(versions) != 1 or not re.fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z][\w]*)+", packages[0]):
        raise VerifyError("Bundle manifest must name exactly one package and one versionCode")
    return packages[0], versions[0]


def aab_identity(path):
    """(package, versionCode) of an Android App Bundle, read from its base module manifest."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names.count(MANIFEST_ENTRY) != 1:
                raise VerifyError("Bundle must carry exactly one base manifest")
            info = archive.getinfo(MANIFEST_ENTRY)
            if info.file_size > MAX_MANIFEST_BYTES:
                raise VerifyError("Bundle manifest is too large")
            data = archive.read(MANIFEST_ENTRY)
    except (zipfile.BadZipFile, KeyError, OSError):
        raise VerifyError("Bundle is not a readable zip archive") from None
    return manifest_identity(data)


def check_aab_identity(path, package, build_number):
    """Refuse a bundle whose own manifest is not exactly this app (package) at this build (versionCode)."""
    found_package, found_version = aab_identity(path)
    if found_package != package:
        raise VerifyError("Bundle package is not this target's app")
    if found_version != build_number:
        raise VerifyError("Bundle versionCode is not the requested build number")
    return found_package, found_version


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



# Coordinator, 30 Oct (F4): May's iOS push needs the signed app to carry aps-environment=production, which the export
# takes from the app's own entitlements and its App Store profile together. The check reads what codesign says the
# exported app was signed with (codesign -d --entitlements - --xml), for both targets, and says pass or fail and
# whether the key is there, never a value or anything of the profile.
PUSH_KEY, PUSH_PRODUCTION = "aps-environment", "production"


def check_push(xml):
    """Require codesign's entitlements XML to set aps-environment to production; fixed messages, no values."""
    starts = [at for at in (xml.find(b"<?xml"), xml.find(b"<plist")) if at >= 0]
    try:
        entitlements = plistlib.loads(xml[min(starts):]) if starts else None
    except (plistlib.InvalidFileException, ValueError):
        entitlements = None
    if not isinstance(entitlements, dict):
        raise VerifyError("IPA push check failed: the app's signed entitlements could not be read")
    if PUSH_KEY not in entitlements:
        raise VerifyError("IPA push check failed: the app's signed entitlements have no aps-environment")
    if entitlements[PUSH_KEY] != PUSH_PRODUCTION:
        raise VerifyError("IPA push check failed: the app's signed entitlements have aps-environment, not production")


APP_ICON = "AppIcon"


def ipa_icon(path):
    """Check the IPA's app has its icon: Info.plist names CFBundleIconName AppIcon (at the top level, as actool's
    partial plist can write it, or in CFBundleIcons' primary icon), and the app's compiled asset catalog
    (Assets.car) exists and names AppIcon. TestFlight build 7 showed no icon in App Store Connect; this makes a
    missing one fail the job before the upload, with a fixed message saying which part is missing."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            apps = {n.split("/")[1] for n in names
                    if n.startswith("Payload/") and n.count("/") >= 2 and n.split("/")[1].endswith(".app")}
            if len(apps) != 1:
                raise VerifyError("IPA must contain exactly one app")
            app = "Payload/" + apps.pop() + "/"
            info = plistlib.loads(archive.read(app + "Info.plist"))
            catalog = archive.read(app + "Assets.car") if app + "Assets.car" in names else None
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException):
        raise VerifyError("IPA is not a complete app archive") from None
    icons = info.get("CFBundleIcons") if isinstance(info, dict) else None
    primary = icons.get("CFBundlePrimaryIcon") if isinstance(icons, dict) else None
    named = (isinstance(info, dict) and info.get("CFBundleIconName") == APP_ICON) or \
        (isinstance(primary, dict) and primary.get("CFBundleIconName") == APP_ICON)
    if not named:
        raise VerifyError("IPA Info.plist names no CFBundleIconName AppIcon (App Store Connect shows no icon)")
    if catalog is None:
        raise VerifyError("IPA app has no compiled asset catalog (Assets.car), so no icon")
    if APP_ICON.encode() not in catalog:
        raise VerifyError("IPA asset catalog (Assets.car) names no AppIcon")
