"""No-secret structural guard for this public control repository.

problems(root) returns fixed descriptions of every violated rule; the CLI exits
non-zero if there is any. Dependency-free on purpose: it runs in the PR check.

It also pins the store workflow's activation stage (STAGE, STORE_JOB_IF,
UPLOAD_IF, PLAY_UPLOAD_TRACKS, and the sign-only steps): exactly which jobs and
steps carry the never-matching event guard, which uploads may happen, and the one
sign-only publication of May production's encrypted signed bundle. Moving to another
stage, or back to an earlier one, means changing those constants in the same
reviewed change as the workflow, never the workflow alone.
"""

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
PINNED_USES = re.compile(r"(\./\S+|[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}|docker://\S+@sha256:[0-9a-f]{64})")
# Messages these exceptions (and any subclass) carry may be printed by a CLI, so
# each must be one string literal: never formatted, never from provider or file data.
PRINTED_ERRORS = {"StoreError", "PlayRefusal", "GateDenial", "VerifyError", "FlowsError"}
# The one printed error whose message is not a single literal: it adds Play's
# numeric HTTP status, so that a 403 can be told from a 401 or a 404 in the public
# log. What is enforced (message_problems):
# - exactly one class of this name exists, at the top level of play_upload.py,
#   and it parses to exactly STATUS_ERROR_SOURCE's syntax tree (comments and
#   formatting aside). Any edit to the class fails this check until this copy is
#   updated in the same reviewed change;
# - it is called only in play_upload.py, with exactly one argument of the form
#   `<name>.status_code`;
# - play_upload.py never rebinds the names the class relies on (PROTECTED_NAMES,
#   in any binding form) and never refers to FORBIDDEN_NAMES (builtins,
#   globals(), vars(), setattr, exec, eval, __import__);
# - no module assigns an exception's `.args`, or any attribute of this class.
STATUS_ERROR = "PlayStatusError"
STATUS_ERROR_SOURCE = '''
class PlayStatusError(StoreError):
    """A non-2xx Play reply; the message names its numeric HTTP status and nothing else."""

    def __init__(self, status):
        if type(status) is int and 100 <= status <= 999:
            super().__init__("Google Play request was rejected or uncertain (HTTP " + str(status) + ")")
        else:
            super().__init__("Google Play request was rejected or uncertain (no HTTP status)")
'''
# Names play_upload.py may never rebind: the builtins the pinned class calls, the
# class itself, and its base (bound only by `from store import ... StoreError`).
PROTECTED_NAMES = {"str", "type", "int", "super", STATUS_ERROR, "StoreError"}
# Ways round a name check: play_upload.py may not refer to any of these at all.
FORBIDDEN_NAMES = {"builtins", "__builtins__", "globals", "vars", "setattr", "exec", "eval", "__import__"}
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
UPLOAD_ARTIFACT = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
DOWNLOAD_ARTIFACT = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
FETCH = "Fetch exact private source and pinned design system"
DECRYPT = "Decrypt and check the build handoff"
REPORT = "Report what the dry run would have uploaded"
CONFIRM = "Fail unless the requested store upload succeeded"
SEAL_SIGNED = "Seal the signed bundle for one Play Console upload"
PUBLISH_SIGNED = "Publish the sealed signed bundle for 3 days"
CONFIRM_SIGNED = "Fail unless the signed bundle was published, not uploaded"
UPLOADS = {"android-release": "Upload update to the internal track only",
           "ios-release": "Upload verified IPA for internal TestFlight"}
# Each upload step's id, which the loud-failure step reads (steps.<id>.outcome).
UPLOAD_IDS = {"android-release": "play_upload", "ios-release": "testflight_upload"}
# A job or step whose condition requires this event never runs: the workflow
# registers only workflow_dispatch. That is how a job or step is held inert.
INERT = "github.event_name == 'may-store-disabled-pending-review'"
STAGE = "stage 3"
# Activation stage 3 (Android uploads to the Play internal track, May and May
# Playground; sign-only for May production). Each store
# job's exact job-level `if:` (None: no condition). Admission and the Android jobs
# are active and keep their platform condition; the iOS jobs stay inert
# (tools/release/ios.sh does not exist yet). The Android upload step is active and
# the iOS upload step is still held (UPLOAD_IF). The guard appears nowhere else in
# the workflow's code.
STORE_JOB_IF = {
    "admission": None,
    "android-build": "${{ inputs.platform == 'android' }}",
    "android-release": "${{ inputs.platform == 'android' && github.event.inputs.sign_only != 'true' }}",
    "android-sign-only": "${{ inputs.platform == 'android' && github.event.inputs.sign_only == 'true' }}",
    "ios-build": "${{ " + INERT + " && inputs.platform == 'ios' }}",
    "ios-release": "${{ " + INERT + " && inputs.platform == 'ios' }}",
}
# A store upload runs only when dry_run is exactly 'false' (github.event.inputs
# holds booleans as strings, so a missing value never uploads), and the upload
# script receives the same value to recheck. The report runs in every other case.
# A held upload also carries the guard first (HELD_UPLOAD_IF). Stage 3: the
# Android upload is active (DRY_RUN_UPLOAD_IF), the iOS upload is still held.
DRY_RUN_UPLOAD_IF = "${{ github.event.inputs.dry_run == 'false' }}"
HELD_UPLOAD_IF = "${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}"
UPLOAD_IF = {"android-release": DRY_RUN_UPLOAD_IF, "ios-release": HELD_UPLOAD_IF}
REPORT_IF = "${{ github.event.inputs.dry_run != 'false' }}"
# Loud failure: right after each upload step, whenever dry_run is false (unless the
# run was cancelled, and even after an earlier failure), confirm_upload.py fails the
# job unless that upload step's outcome is success. It never carries the guard, so
# it is already in place when an upload's guard comes off.
CONFIRM_IF = "${{ !cancelled() && github.event.inputs.dry_run == 'false' }}"
# Stage 3: store.py's PLAY_UPLOAD_TRACKS must be exactly this (May and May
# Playground to the Play internal track only), and play_upload.py's TRACK must be
# "internal".
PLAY_UPLOAD_TRACKS = {"playground": "internal", "production": "internal"}
DRY_RUN_ENV = "${{ github.event.inputs.dry_run }}"
DRY_RUN_INPUT = ("      dry_run:\n        description: [^\n]+\n        required: true\n"
                 "        type: boolean\n        default: true\n")
# Sign-only (stage 3, May production Android): a required boolean input that
# defaults to false, so it is always an explicit choice. When it is 'true',
# android-release does not run and SIGN_ONLY_JOB does (STORE_JOB_IF): the same
# environment, decrypt, sign and verify, then its three sign-only steps, the
# check also after an earlier failure unless the run was cancelled. That job
# references no Play secret or variable, so MAY_PLAY_JSON never reaches its
# runner (a job's referenced secrets reach the runner even when the step that
# uses them is skipped).
SIGN_ONLY_JOB = "android-sign-only"
SIGN_ONLY_ENV = "${{ github.event.inputs.sign_only }}"
SIGN_ONLY_INPUT = ("      sign_only:\n        description: [^\n]+\n        required: true\n"
                   "        type: boolean\n        default: false\n")
CONFIRM_SIGNED_IF = "${{ !cancelled() }}"
SIGNED_ID = "signed_artifact"
# The publish step, exactly: only the may-signed folder (the encrypted bundle, its
# SHA-256 and its record), bound to the run attempt, kept 3 days.
PUBLISH_SIGNED_STEP = (
    "name: " + PUBLISH_SIGNED + "\n"
    "        id: " + SIGNED_ID + "\n"
    "        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n"
    "        with:\n"
    "          name: may-signed-android-${{ github.run_attempt }}\n"
    "          path: ${{ runner.temp }}/may-signed/\n"
    "          if-no-files-found: error\n"
    "          retention-days: 3\n"
    "          compression-level: 0\n")
READ_KEYS = {"MAY_APP_READ_KEY", "MAY_DS_READ_KEY"}
# The Android build step holds the PostHog crash-report key and nothing else (store.POSTHOG_SECRETS).
ANDROID_BUILD = "Build unsigned Android App Bundle with only the crash-report key"
# That step runs exactly this and nothing else (it is the one build step holding a secret past the fetch), and only it
# may read the PostHog host variable.
ANDROID_BUILD_COMMAND = ('python3 -I -S -B tools/may_release/store.py --platform android --target "$MAY_TARGET" '
                         '--source-sha "$MAY_SOURCE_SHA" --build-number "$MAY_BUILD_NUMBER"')
POSTHOG_HOST_ENV = "MAY_POSTHOG_HOST: ${{ vars.MAY_POSTHOG_HOST }}"
# Store workflow: job -> (ordered named steps, {step: exact secrets}, allowed actions).
STORE_JOBS = {
    "android-build": (
        ["Install pinned JDK and age", FETCH, ANDROID_BUILD,
         "Encrypt unsigned bundle to the target's public key"],
        {FETCH: READ_KEYS, ANDROID_BUILD: {"MAY_POSTHOG_ANDROID_KEY"}}, {CHECKOUT, UPLOAD_ARTIFACT}),
    "android-release": (
        ["Install pinned JDK and age", DECRYPT, "Sign and verify Android App Bundle",
         "Upload update to the internal track only", CONFIRM, REPORT],
        {DECRYPT: {"MAY_ARTIFACT_DECRYPT_KEY"},
         "Sign and verify Android App Bundle": {"MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                                                "MAY_ANDROID_KEY_PASSWORD"},
         "Upload update to the internal track only": {"MAY_PLAY_JSON"}},
        {CHECKOUT, DOWNLOAD_ARTIFACT}),
    SIGN_ONLY_JOB: (
        ["Install pinned JDK and age", DECRYPT, "Sign and verify Android App Bundle",
         SEAL_SIGNED, PUBLISH_SIGNED, CONFIRM_SIGNED],
        {DECRYPT: {"MAY_ARTIFACT_DECRYPT_KEY"},
         "Sign and verify Android App Bundle": {"MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                                                "MAY_ANDROID_KEY_PASSWORD"}},
        {CHECKOUT, DOWNLOAD_ARTIFACT, UPLOAD_ARTIFACT}),
    "ios-build": (
        ["Install pinned age", FETCH, "Build unsigned iOS archive without secrets",
         "Encrypt unsigned archive to the target's public key"],
        {FETCH: READ_KEYS}, {CHECKOUT, UPLOAD_ARTIFACT}),
    "ios-release": (
        ["Install pinned age", DECRYPT, "Sign and verify internal-TestFlight IPA",
         "Upload verified IPA for internal TestFlight", CONFIRM, REPORT],
        {DECRYPT: {"MAY_ARTIFACT_DECRYPT_KEY"},
         "Sign and verify internal-TestFlight IPA": {"MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD",
                                                     "MAY_IOS_PROFILE_BASE64"},
         "Upload verified IPA for internal TestFlight": {"MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID"}},
        {CHECKOUT, DOWNLOAD_ARTIFACT}),
}
# In a release job every command is this repository's own Python, with arguments
# from a safe character set: no ; | & ` ( ) < > { } or newline, so no $( or chaining.
RELEASE_COMMAND = re.compile(
    r"python3 -I -S -B tools/may_release/(gate|toolchain|handoff|sign|sign_only|play_upload|ios_upload|confirm_upload|dry_run|cleanup|integrity)\.py"
    r"( [A-Za-z0-9_./\"$=-]+)*")
ADMISSION_GATE = ('python3 -I -S -B tools/may_release/gate.py --action "$MAY_PLATFORM" --target "$MAY_TARGET" '
                  '--source-sha "$MAY_SOURCE_SHA" --build-number "$MAY_BUILD_NUMBER" --dry-run "$MAY_DRY_RUN" '
                  '--sign-only "$MAY_SIGN_ONLY"')
CONTINUE_ON_ERROR = re.compile(r"(?m)^\s*(-\s+)?continue-on-error:")
VARS = {"MAY_ANDROID_KEY_ALIAS", "MAY_ANDROID_CERT_SHA256", "MAY_PLAY_ACCOUNT", "MAY_ASC_KEY_ID", "MAY_POSTHOG_HOST"}
INPUTS = {"MAY_TARGET": "target", "MAY_SOURCE_SHA": "source_sha", "MAY_BUILD_NUMBER": "build_number"}


def job_text(workflow, job):
    jobs = workflow.partition("\njobs:\n")[2]
    match = re.search(r"(?ms)^  " + re.escape(job) + r":\n(.*?)(?=^  [\w-]+:\n|\Z)", jobs)
    return match.group(1) if match else ""


def job_steps(workflow, job):
    """(header, [(name, text)]) for one job of a workflow in this repository's layout."""
    text = job_text(workflow, job)
    header, _, body = text.partition("    steps:\n")
    steps = []
    for chunk in re.split(r"(?m)^      - ", body)[1:]:
        name = re.search(r"(?m)^(?:        )?name: (.+)$", chunk)
        steps.append((name.group(1).strip() if name else "", chunk))
    return header, steps


def step_condition(step):
    """The step's `if:` expression, or None. It may be on the step's dash line, like name:."""
    match = re.search(r"(?m)^(?:        )?if: (.+)$", step)
    return match.group(1).strip() if match else None


def step_condition_count(step):
    return len(re.findall(r"(?m)^(?:        )?if: ", step))


def code_lines(text):
    """text without YAML comments, so a guard named in a comment is not counted."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def stage_problems(store):
    """Where the inert guard must, and must not, be in this activation stage."""
    found = []
    for job, expected in STORE_JOB_IF.items():
        header = job_text(store, job).partition("    steps:\n")[0]
        if not header:
            found.append(f"store {job}: job is missing")
            continue
        conditions = [c.strip() for c in re.findall(r"(?m)^    if: (.+)$", header)]
        if len(conditions) > 1:
            found.append(f"store {job}: has more than one job-level if:")
        condition = conditions[0] if conditions else None
        if expected is not None and INERT in expected:
            if condition is None or INERT not in condition:
                found.append(f"store {job}: the job is not held inert ({STAGE})")
            elif condition != expected:
                found.append(f"store {job}: the inert job does not keep its exact platform condition")
        elif condition is not None and INERT in condition:
            found.append(f"store {job}: the job is held inert, but {STAGE} activates it "
                         "(stage mismatch: change STORE_JOB_IF in static_check.py on purpose)")
        elif condition != expected:
            found.append(f"store {job}: does not keep its platform condition" if expected else
                         f"store {job}: its job-level condition is not the one {STAGE} expects")
    held = sum(1 for c in STORE_JOB_IF.values() if c and INERT in c) + \
        sum(1 for c in UPLOAD_IF.values() if INERT in c)
    if code_lines(store).count(INERT) != held:
        found.append(f"store-release-draft.yml: the inert guard appears or is missing somewhere {STAGE} does not expect")
    return found


# Every code line of the store workflow is in this repository's plain block layout,
# so the line-based rules here read what GitHub's YAML parser reads: no quoted,
# spaced or explicit (`?`) keys, no anchors, aliases, tags or merge keys, no tabs,
# no multi-line quoted, plain or flow values (block scalars only after `key: |`).
LAYOUT_LINE = re.compile(r"( *)(- )?([A-Za-z][\w-]*):(?: (.*))?")
LAYOUT_VALUE = re.compile(r"""[^\s&*!{}?|>%@`'"\[\]#,][^#]*|'(?:[^']|'')*'|\[[^\[\]{}'"#]*\]|[|>]-?""")
ADMISSION_ENV = [("GH_TOKEN", "${{ github.token }}"), ("MAY_PLATFORM", "${{ inputs.platform }}"),
                 ("MAY_TARGET", "${{ inputs.target }}"), ("MAY_SOURCE_SHA", "${{ inputs.source_sha }}"),
                 ("MAY_BUILD_NUMBER", "${{ inputs.build_number }}"), ("MAY_DRY_RUN", DRY_RUN_ENV),
                 ("MAY_SIGN_ONLY", SIGN_ONLY_ENV)]
ADMISSION_OUTPUTS = ("    outputs:\n      environment: ${{ steps.gate.outputs.environment }}\n"
                     "      build_environment: ${{ steps.gate.outputs.build_environment }}\n")


def layout_problems(store):
    found, block = [], None
    for number, line in enumerate(store.splitlines(), 1):
        indent = len(line) - len(line.lstrip(" "))
        if block is not None and (not line.strip() or indent > block):
            continue
        block = None
        if not line.strip() or line.strip().startswith("#"):
            continue
        match = LAYOUT_LINE.fullmatch(line)
        value = re.sub(r"(^|\s)#.*$", "", (match.group(4) or "") if match else "").strip()
        if "\t" in line or not match or (value and not LAYOUT_VALUE.fullmatch(value)):
            found.append(f"store-release-draft.yml:{number}: is not a plain one-line `key: value` in this repository's layout")
            continue
        if value[:1] in ("|", ">"):
            block = len(match.group(1)) + (2 if match.group(2) else 0)
    if re.findall(r"(?m)^([\w-]+):", store) != ["name", "on", "permissions", "concurrency", "jobs"]:
        found.append("store-release-draft.yml: top-level keys are not exactly name, on, permissions, concurrency, jobs")
    if re.findall(r"(?m)^  ([\w-]+):", store.partition("\non:\n")[2].partition("\npermissions:")[0]) != ["workflow_dispatch"]:
        found.append("store-release-draft.yml: is triggered by something other than workflow_dispatch")
    if re.findall(r"(?m)^  ([\w-]+):", store.partition("\njobs:\n")[2]) != list(STORE_JOB_IF):
        found.append(f"store-release-draft.yml: jobs are not exactly the ones {STAGE} pins")
    admission = job_text(store, "admission")
    if uses_in(admission) != [CHECKOUT] or env_entries(admission) != ADMISSION_ENV or \
            ADMISSION_OUTPUTS not in admission or admission.count("      - id: gate\n") != 1:
        found.append("store admission: its actions, env or outputs are not exactly the gate's")
    return found


def secrets_in(text):
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", text))


def uses_in(text):
    return re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)", text)


def run_commands(step):
    """Command lines of a step's run: (single line or block)."""
    match = re.search(r"(?ms)^        run: (\|\n(.*)|(.*?)$)", step)
    if not match:
        return []
    if match.group(3) is not None:
        return [match.group(3).strip()]
    return [line.strip() for line in match.group(2).splitlines() if line.strip()]


def env_entries(text):
    """(name, value) for every entry of every env: block in text."""
    found, env_indent = [], None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if env_indent is not None and line.strip() and indent > env_indent:
            name, _, value = line.strip().partition(":")
            found.append((name, value.strip()))
            continue
        env_indent = indent if re.fullmatch(r"\s*(-\s+)?env:", line) else None
    return found


def release_env_allowed(name, value, secrets, job):
    if name == "MAY_UPLOAD_OUTCOME":
        return job in UPLOAD_IDS and value == "${{ steps." + UPLOAD_IDS[job] + ".outcome }}"
    if name == "MAY_PUBLISH_OUTCOME":
        return job == SIGN_ONLY_JOB and value == "${{ steps." + SIGNED_ID + ".outcome }}"
    if name == "MAY_SIGN_ONLY":
        return job == SIGN_ONLY_JOB and value == SIGN_ONLY_ENV
    if name in secrets:
        return value == "${{ secrets." + name + " }}"
    if name in VARS:
        return value == "${{ vars." + name + " }}"
    if name in INPUTS:
        return value == "${{ inputs." + INPUTS[name] + " }}"
    return (name, value) in {("PYTHONDONTWRITEBYTECODE", "'1'"), ("GH_TOKEN", "${{ github.token }}"),
                             ("MAY_DRY_RUN", DRY_RUN_ENV)}


def bare_secret_references(text):
    """Any use of the secrets context other than secrets.NAME (e.g. secrets[...],
    toJSON(secrets), `secrets: inherit`). Comments and name/description values are prose."""
    found = 0
    for line in text.splitlines():
        code = re.sub(r"(^|\s)#.*$", "", line)
        if re.match(r"\s*(-\s+)?(name|description):", code):
            continue
        found += len(re.findall(r"\bsecrets\b(?!\.[A-Za-z_][A-Za-z0-9_]*)", code))
    return found


def workflow_problems(root):
    found = []
    workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
    if not workflows:
        found.append("No workflows found")
    for path in workflows:
        text = path.read_text()
        for ref in uses_in(text):
            if not PINNED_USES.fullmatch(ref):
                found.append(f"{path.name}: action reference is not pinned to a 40-hex commit SHA")
            if ref.startswith("actions/setup-java@"):
                found.append(f"{path.name}: setup-java does not verify its JDK; use toolchain.py")
        if bare_secret_references(text):
            found.append(f"{path.name}: secrets are referenced other than as secrets.NAME")
        if re.search(r"(?m)^\s*(-\s+)?env:[ \t]*[^\s#]", text):
            found.append(f"{path.name}: uses a flow-style env mapping")
        if re.search(r"(?m)^\s*deployments:", text):
            found.append(f"{path.name}: requests deployments permission")
        if "pull_request_target" in text:
            found.append(f"{path.name}: uses pull_request_target")
        if re.search(r"pip3? install|python3? -m pip", text):
            found.append(f"{path.name}: installs Python packages")
        for step in re.split(r"(?m)^      - ", text)[1:]:
            if re.search(r"uses: actions/checkout@", step) and "persist-credentials: false" not in step:
                found.append(f"{path.name}: a checkout persists its credentials")
        if re.search(r"plugin_marketplaces:.*https?://", text):
            found.append(f"{path.name}: plugin marketplace is a moving URL, not a pinned checkout")
        for job in re.findall(r"(?m)^  ([\w-]+):\n", text.partition("\njobs:\n")[2]):
            header = job_text(text, job).partition("    steps:\n")[0]
            runs_on = re.search(r"(?m)^    runs-on: (.+)$", header)
            if "toolchain.py jdk" in job_text(text, job) and runs_on and "macos" in runs_on.group(1) and \
                    "inputs.platform == 'android' && 'ubuntu-24.04'" not in runs_on.group(1):
                found.append(f"{path.name}: job {job} installs the linux-only pinned JDK on macOS")
            if re.search(r"(?m)^    environment:", header) and not re.search(r"(?m)^    needs: (admission|\[admission, [\w-]+\])$", header):
                found.append(f"{path.name}: protected job {job} does not wait for admission")
    return found


def store_problems(store):
    found = []
    found += stage_problems(store)
    found += layout_problems(store)
    if store.count("Recheck protected destination after Samuel's review") != 3 or \
            store.count("Recheck build environment after Samuel's review") != 2:
        found.append("store-release-draft.yml does not recheck after review")
    if "track: production" in store or "--track production" in store:
        found.append("store-release-draft.yml names the production track")
    if re.search(r"(?m)^(env|defaults):", store):
        found.append("store-release-draft.yml sets workflow-level env or defaults")
    if secrets_in(job_text(store, "admission")):
        found.append("store admission: holds a secret")
    # Admission is active: its gate chooses and checks the environments every later
    # job uses, so it runs exactly the gate, and no admission step may be skipped
    # or allowed to fail.
    _, admission_steps = job_steps(store, "admission")
    if [run_commands(text) for _, text in admission_steps if run_commands(text)] != [[ADMISSION_GATE]] or \
            any(step_condition(text) is not None for _, text in admission_steps) or \
            CONTINUE_ON_ERROR.search(job_text(store, "admission")):
        found.append("store admission: does not run exactly the gate, unconditionally")
    # The gate refuses dry_run: false for a held upload or an upload this stage does
    # not allow, so such a dispatch fails at admission instead of finishing green.
    if "          MAY_DRY_RUN: " + DRY_RUN_ENV + "\n" not in job_text(store, "admission"):
        found.append("store admission: does not pass the dispatch's dry_run to the gate")
    if not re.search(r"(?m)^" + DRY_RUN_INPUT, store.partition("\njobs:\n")[0]):
        found.append("store-release-draft.yml: dry_run is not a required boolean input defaulting to true")
    if not re.search(r"(?m)^" + SIGN_ONLY_INPUT, store.partition("\njobs:\n")[0]):
        found.append("store-release-draft.yml: sign_only is not a required boolean input defaulting to false")
    if "          MAY_SIGN_ONLY: " + SIGN_ONLY_ENV + "\n" not in job_text(store, "admission"):
        found.append("store admission: does not pass the dispatch's sign_only to the gate")
    for job, (order, expected, actions) in STORE_JOBS.items():
        header, steps = job_steps(store, job)
        release = job.endswith("-release") or job == SIGN_ONLY_JOB
        build = job.split("-")[0] + "-build"
        environment = "needs.admission.outputs.environment" if release else "needs.admission.outputs.build_environment"
        if not header:
            found.append(f"store {job}: job is missing")
            continue
        if "    environment: ${{ " + environment + " }}" not in header:
            found.append(f"store {job}: environment is not the one admission checked")
        if release and f"    needs: [admission, {build}]" not in header:
            found.append(f"store {job}: does not run after its own build job")
        if secrets_in(header):
            found.append(f"store {job}: job-level environment holds a secret")
        # Only the handoff download may fail without failing the job: a missing
        # artifact is reported by the decrypt step.
        if CONTINUE_ON_ERROR.search(header) or any(
                CONTINUE_ON_ERROR.search(text) and DOWNLOAD_ARTIFACT not in text for _, text in steps):
            found.append(f"store {job}: a step other than the handoff download may fail without failing the job")
        if any("${{" in text.partition("run:")[2] for _, text in steps):
            found.append(f"store {job}: a run script interpolates an expression into shell")
        names = [name for name, _ in steps]
        if [n for n in names if n in order] != order:
            found.append(f"store {job}: its steps are missing or out of order")
        if {name: secrets_in(text) for name, text in steps if secrets_in(text)} != expected:
            found.append(f"store {job}: a step holds secrets it does not need")
        if set(uses_in(job_text(store, job))) - actions:
            found.append(f"store {job}: uses an action it does not need")
        platform = job.split("-")[0]
        prepare = ('python3 -I -S -B tools/may_release/prepare.py --platform ' + platform +
                   ' --target "$MAY_TARGET" --source-sha "$MAY_SOURCE_SHA"')
        if not release and [run_commands(text) for name, text in steps if name == FETCH] != [[prepare]]:
            found.append(f"store {job}: does not prepare the design system for its own platform and target")
        if job == "android-build" and [run_commands(text) for name, text in steps if name == ANDROID_BUILD] != \
                [[ANDROID_BUILD_COMMAND]]:
            found.append(f"store {job}: the crash-report key's build step runs something other than the build")
        # The PostHog host variable: only on android-build's build step (never at job level or on another step).
        allowed_host = job == "android-build" and [n for n, t in steps if "MAY_POSTHOG_HOST" in t] in ([], [ANDROID_BUILD])
        if "MAY_POSTHOG_HOST" in job_text(store, job) and not (allowed_host and "MAY_POSTHOG_HOST" not in header):
            found.append(f"store {job}: reads the PostHog host outside the crash-report key's build step")
        # The dry run must still build, seal, open, sign and verify: only the
        # upload, the loud-failure check and the report are conditional (cleanup
        # always runs).
        for name, text in steps:
            condition = step_condition(text)
            if step_condition_count(text) > 1:
                found.append(f"store {job}: a step has more than one if:")
            if release and name == UPLOADS.get(job):
                # Together these require exactly UPLOAD_IF[job]: for a held upload
                # the guard first, then exactly the dry-run condition; for an
                # active one, exactly the dry-run condition with no guard.
                if INERT in UPLOAD_IF[job]:
                    if condition is None or not condition.startswith("${{ " + INERT + " && "):
                        found.append(f"store {job}: the upload is not held inert ({STAGE})")
                elif INERT in (condition or ""):
                    found.append(f"store {job}: the upload is held inert, but {STAGE} activates it "
                                 "(stage mismatch: change UPLOAD_IF in static_check.py on purpose)")
                if (condition or "").replace(INERT + " && ", "", 1) != DRY_RUN_UPLOAD_IF or \
                        ("          MAY_DRY_RUN: " + DRY_RUN_ENV + "\n") not in text:
                    found.append(f"store {job}: a dry run would run the store upload")
            elif release and name == CONFIRM:
                pass  # loud_failure_problems pins it exactly
            elif job == SIGN_ONLY_JOB and name in (SEAL_SIGNED, PUBLISH_SIGNED, CONFIRM_SIGNED):
                pass  # sign_only_problems pins them exactly
            elif release and name == REPORT:
                if condition != REPORT_IF or not run_commands(text) or not run_commands(text)[0].startswith(
                        "python3 -I -S -B tools/may_release/dry_run.py --platform " + platform + " "):
                    found.append(f"store {job}: the dry run does not report what it would have uploaded")
            elif condition not in (None, "always()") or (condition == "always()" and "cleanup.py" not in text):
                found.append(f"store {job}: a step other than the upload or dry-run report is conditional")
        for name, text in steps:
            if UPLOAD_ARTIFACT in text and "path: ${{ runner.temp }}/may-handoff/handoff.age\n" not in text and \
                    not (job == SIGN_ONLY_JOB and text == PUBLISH_SIGNED_STEP):
                found.append(f"store {job}: uploads something other than the encrypted handoff "
                             "(or, for sign-only, exactly the sealed signed bundle)")
        for name, text in steps:
            if job == SIGN_ONLY_JOB and text == PUBLISH_SIGNED_STEP:
                continue  # pinned exactly, with its own run-attempt-bound name
            if ("upload-artifact@" in text or "download-artifact@" in text) and \
                    not re.search(r"(?m)^          name: may-handoff-(android|ios)-\$\{\{ github\.run_attempt \}\}$", text):
                found.append(f"store {job}: handoff artifact is not bound to this run attempt")
        # Every job, build and release alike: each secrets.X is bound as X, each vars.X as X (X in VARS),
        # and nothing else but the declared inputs and constants.
        if not release:
            all_secrets = set().union(*expected.values())
            if not all(release_env_allowed(n, v, all_secrets, job) for n, v in env_entries(job_text(store, job))):
                found.append(f"store {job}: sets an environment variable it does not declare")
        if release:
            if job in UPLOADS:
                found += loud_failure_problems(store, job, steps)
            found += sign_only_problems(store, job, steps)
            all_secrets = set().union(*expected.values())
            body = job_text(store, job)
            if re.search(r"(?m)^\s+(shell|defaults|working-directory):", body):
                found.append(f"store {job}: overrides the shell, defaults or working directory")
            if not all(release_env_allowed(n, v, all_secrets, job) for n, v in env_entries(body)):
                found.append(f"store {job}: sets an environment variable it does not declare")
            for name, text in steps:
                for command in run_commands(text):
                    if not RELEASE_COMMAND.fullmatch(command):
                        found.append(f"store {job}: runs a command that is not this repository's Python")
            for previous, current in zip(steps, steps[1:]):
                if secrets_in(current[1]) and run_commands(previous[1]) != ["python3 -I -S -B tools/may_release/integrity.py"]:
                    found.append(f"store {job}: a secret step is not preceded by the checkout integrity check")
                if secrets_in(current[1]) and step_condition(previous[1]) is not None:
                    found.append(f"store {job}: the integrity check before a secret step is conditional")
    return found


def loud_failure_problems(store, job, steps):
    """A dispatch with dry_run: false must never end green without uploading.

    Right after the upload step, exactly one CONFIRM step runs confirm_upload.py
    for this platform under exactly CONFIRM_IF (never the guard), reading only the
    upload step's outcome, and the upload step carries the id it reads.
    """
    platform, upload_id = job.split("-")[0], UPLOAD_IDS[job]
    names = [name for name, _ in steps]
    confirms = [text for name, text in steps if name == CONFIRM]
    command = ("python3 -I -S -B tools/may_release/confirm_upload.py --platform " + platform +
               ' --target "$MAY_TARGET" --source-sha "$MAY_SOURCE_SHA" --build-number "$MAY_BUILD_NUMBER"')
    uploads = [text for name, text in steps if name == UPLOADS[job]]
    if len(confirms) != 1 or UPLOADS[job] not in names or names.index(CONFIRM) != names.index(UPLOADS[job]) + 1 or \
            step_condition(confirms[0]) != CONFIRM_IF or run_commands(confirms[0]) != [command] or \
            env_entries(confirms[0]) != [("MAY_UPLOAD_OUTCOME", "${{ steps." + upload_id + ".outcome }}")] or \
            len(uploads) != 1 or not re.search(r"(?m)^(?:        )?id: " + upload_id + "$", uploads[0]) or \
            len(re.findall(r"(?m)^\s*(?:-\s+)?id: " + upload_id + "$", job_text(store, job))) != 1:
        return [f"store {job}: a dispatch with dry_run: false could end green without uploading "
                "(the loud-failure check after the upload is missing or changed)"]
    return []


def sign_only_problems(store, job, steps):
    """Sign-only is SIGN_ONLY_JOB alone, which never references a Play secret or variable.

    In that job the three sign-only steps come right after signing, in order: the
    seal step runs sign_only.py with only MAY_SIGN_ONLY and MAY_DRY_RUN; the
    publish step is exactly PUBLISH_SIGNED_STEP (the may-signed folder, 3 days);
    the check runs confirm_upload.py --sign-only under exactly CONFIRM_SIGNED_IF,
    reading only the publish step's outcome; then only the cleanup. No other job
    has a sign-only step.
    """
    names = [name for name, _ in steps]
    texts = dict(steps)
    ours = [n for n in names if n in (SEAL_SIGNED, PUBLISH_SIGNED, CONFIRM_SIGNED)]
    ids = len(re.findall(r"(?m)^\s*(?:-\s+)?id: " + SIGNED_ID + "$", job_text(store, job)))
    if job != SIGN_ONLY_JOB:
        if ours or ids or any("sign_only" in code_lines(text) for _, text in steps):
            return [f"store {job}: has a sign-only step; sign-only runs only in {SIGN_ONLY_JOB}"]
        return []
    found = []
    if re.search(r"MAY_PLAY_|secrets\.MAY_PLAY", code_lines(job_text(store, job))):
        found.append(f"store {job}: references a Play secret or variable; its runner must never receive "
                     "MAY_PLAY_JSON")
    sign = "Sign and verify Android App Bundle"
    tail = ("python3 -I -S -B tools/may_release/{} --target \"$MAY_TARGET\" --source-sha \"$MAY_SOURCE_SHA\" "
            "--build-number \"$MAY_BUILD_NUMBER\"")
    if ours != [SEAL_SIGNED, PUBLISH_SIGNED, CONFIRM_SIGNED] or sign not in names or \
            names[names.index(sign) + 1:] != ours + ["Remove task-owned bundle, keys and logs"] or ids != 1 or \
            step_condition(texts[SEAL_SIGNED]) is not None or \
            run_commands(texts[SEAL_SIGNED]) != [tail.format("sign_only.py")] or \
            env_entries(texts[SEAL_SIGNED]) != [("MAY_SIGN_ONLY", SIGN_ONLY_ENV), ("MAY_DRY_RUN", DRY_RUN_ENV)] or \
            texts[PUBLISH_SIGNED] != PUBLISH_SIGNED_STEP or \
            step_condition(texts[CONFIRM_SIGNED]) != CONFIRM_SIGNED_IF or \
            run_commands(texts[CONFIRM_SIGNED]) != [tail.format("confirm_upload.py --platform android") + " --sign-only"] or \
            env_entries(texts[CONFIRM_SIGNED]) != [("MAY_PUBLISH_OUTCOME", "${{ steps." + SIGNED_ID + ".outcome }}")]:
        found.append(f"store {job}: the sign-only steps are missing or changed (seal, publish exactly the sealed "
                     "bundle for 3 days, then fail unless it was published)")
    return found


def assigned_literal(path, name):
    """The literal value of a module-level `name = ...` in path, or None."""
    value = None
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [name]:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                value = None
    return value


def held_uploads_problems(root, store):
    """gate.py's UPLOADS_HELD must name exactly the platforms whose upload step is held."""
    held = set()
    for job, name in UPLOADS.items():
        for step_name, text in job_steps(store, job)[1]:
            if step_name == name and INERT in (step_condition(text) or ""):
                held.add(job.split("-")[0])
    declared = assigned_literal(root / "tools/may_release/gate.py", "UPLOADS_HELD")
    if not isinstance(declared, set) or declared != held:
        return [f"gate.py: UPLOADS_HELD is not exactly the platforms whose upload is held ({STAGE}), so "
                "admission would admit dry_run: false for a held upload or refuse it for an active one"]
    return []


def upload_track_problems(root):
    """Only the Play internal track is reachable (stage 3: for May and May Playground)."""
    found = []
    tracks = assigned_literal(root / "tools/may_release/store.py", "PLAY_UPLOAD_TRACKS")
    if not isinstance(tracks, dict) or tracks != PLAY_UPLOAD_TRACKS or set(tracks.values()) - {"internal"}:
        found.append(f"store.py: PLAY_UPLOAD_TRACKS is not exactly {STAGE}'s "
                     "(May and May Playground to the Play internal track only)")
    uploader = (root / "tools/may_release/play_upload.py").read_text()
    if assigned_literal(root / "tools/may_release/play_upload.py", "TRACK") != "internal" or \
            uploader.count("/tracks/") != 1 or '/tracks/{TRACK}"' not in uploader:
        found.append("play_upload.py: writes a track other than the Play internal track (TRACK)")
    return found


# The native compile workflow is active for iOS alone (each build): May Playground, the internal target, so its
# design-system pin may differ from Android's (store.design_system_pins). These are the lines that make it so.
COMPILE_IOS_LINES = (
    "        options: [ios]\n",
    "    if: ${{ inputs.platform == 'ios' }}\n    runs-on: macos-15\n",
)
GATE_COMPILE = 'python3 -I -S -B tools/may_release/gate.py --action compile --source-sha "$MAY_SOURCE_SHA"'
PREPARE_IOS = 'python3 -I -S -B tools/may_release/prepare.py --platform ios --target playground --source-sha "$MAY_SOURCE_SHA"'
COMPILE_IOS = "python3 -I -S -B tools/may_release/compile.py --platform ios --target playground"
COMPILE_CLEANUP = ('rm -rf -- "$RUNNER_TEMP/may-source" "$RUNNER_TEMP/may-design-system" "$RUNNER_TEMP/may-private" '
                   '"$RUNNER_TEMP/may-derived" "$RUNNER_TEMP/may-packages" "$RUNNER_TEMP/may-gradle"')
# Every command the compile workflow may run, each the number of times it does (security review of #16, F1/F2):
# no other step, so nothing prints the private log or runs anything else.
COMPILE_RUNS = [GATE_COMPILE, GATE_COMPILE, PREPARE_IOS, COMPILE_IOS, COMPILE_CLEANUP]
COMPILE_STEP_NAMES = ["Recheck protection after reviewer approval", "Fetch exact private app and pinned design system quietly",
                      "Compile without signing or store access", "Remove private source, logs and products"]
COMPILE_SECRETS = ("MAY_APP_READ_KEY", "MAY_DS_READ_KEY")
COMPILE_ENV = {("GH_TOKEN", "${{ github.token }}"), ("MAY_SOURCE_SHA", "${{ inputs.source_sha }}")} | \
    {(name, "${{ secrets." + name + " }}") for name in COMPILE_SECRETS}
JOB_PERMISSIONS = "    permissions:\n      contents: read\n      actions: read\n"


def compile_workflow_problems(text):
    """The native compile is exactly the active iOS-only May Playground compile: dispatch only, read-only, the two
    source-read keys in the fetch step alone, and nothing run but its four commands."""
    found = []
    if "may-compile-disabled-pending-review" in text or not all(text.count(line) == 1 for line in COMPILE_IOS_LINES) \
            or "toolchain.py jdk" in text or "--platform android" in text:
        found.append("native-compile-draft.yml is not the active iOS-only May Playground compile")
    if re.findall(r"(?m)^([\w-]+):", text) != ["name", "on", "permissions", "concurrency", "jobs"]:
        found.append("native-compile-draft.yml: top-level keys are not exactly name, on, permissions, concurrency, jobs")
    if re.findall(r"(?m)^  ([\w-]+):", text.partition("\non:\n")[2].partition("\npermissions:")[0]) != ["workflow_dispatch"]:
        found.append("native-compile-draft.yml: is triggered by something other than workflow_dispatch")
    if "\npermissions:\n  contents: read\nconcurrency:" not in text or re.search(r":\s*write\b", text) or \
            [job_text(text, job).count(JOB_PERMISSIONS) for job in ("admission", "native")] != [1, 1] or \
            text.count("permissions:") != 3:
        found.append("native-compile-draft.yml: permissions are not exactly read-only (contents, and actions in each job)")
    if re.findall(r"(?m)^  ([\w-]+):", text.partition("\njobs:\n")[2]) != ["admission", "native"]:
        found.append("native-compile-draft.yml: jobs are not exactly admission and native")
    # run: may also sit on a step's dash line (`- run: ...`), unnamed (security review of #16, R1).
    runs = [re.sub(r"\s+", " ", r).strip() for r in re.findall(r"(?m)^\s+(?:-\s+)?run:[ \t]*(.*)$", text)]
    if runs != COMPILE_RUNS or re.search(r"(?m)^\s+(?:-\s+)?run: *[|>]", text):
        found.append("native-compile-draft.yml: runs something other than the gate, the fetch, the compile and the cleanup")
    # Every step, named or not, by the key it starts with: admission is the checkout then the gate; native is the
    # checkout then its four named steps, the last run always.
    steps = {job: job_steps(text, job)[1] for job in ("admission", "native")}
    leading = {job: [chunk.split(":", 1)[0] for _, chunk in found_steps] for job, found_steps in steps.items()}
    names = [name for name, _ in steps["native"]]
    cleanup = steps["native"][-1][1] if steps["native"] else ""
    if leading != {"admission": ["uses", "id"], "native": ["uses", "name", "name", "name", "name"]} or \
            names != [""] + COMPILE_STEP_NAMES or "\n        if: always()\n" not in cleanup or \
            [uses_in(chunk) for _, chunk in steps["admission"] + steps["native"]].count([CHECKOUT]) != 2 or \
            uses_in(text) != [CHECKOUT, CHECKOUT]:
        found.append("native-compile-draft.yml: native's steps are not exactly the recheck, fetch, compile and an always-run cleanup")
    if re.search(r"(?m)^\s*(defaults|shell):", text) or text.count("timeout-minutes:") != 1 or \
            "    timeout-minutes: 90\n" not in job_text(text, "native"):
        found.append("native-compile-draft.yml: sets a shell, defaults or a timeout other than native's 90 minutes")
    fetch = job_text(text, "native").partition("      - name: Fetch exact private app and pinned design system quietly\n")[2] \
        .partition("      - name: ")[0]
    if secrets_in(text) != set(COMPILE_SECRETS) or any(text.count("secrets." + n) != 1 for n in COMPILE_SECRETS) or \
            secrets_in(fetch) != set(COMPILE_SECRETS) or any(e not in COMPILE_ENV for e in env_entries(text)) or \
            re.search(r"(?m)^(    )?env:", text):
        found.append("native-compile-draft.yml: a secret or env entry other than the fetch step's two source-read keys")
    return found


def control_problems(root):
    found = []
    flows = root / ".github/workflows"
    check = (flows / "control-check.yml").read_text()
    compile_workflow = (flows / "native-compile-draft.yml").read_text()
    store = (flows / "store-release-draft.yml").read_text()
    if not ("pull_request:" in check and "permissions:\n  contents: read" in check and "secrets." not in check):
        found.append("control-check.yml is not a read-only, no-secret PR check")
    found += compile_workflow_problems(compile_workflow)
    for name, text in (("native-compile-draft.yml", compile_workflow), ("store-release-draft.yml", store)):
        if "environment: ${{ needs.admission.outputs." not in text:
            found.append(f"{name}: protected environment is not chosen by admission")
    if "upload-artifact" in compile_workflow:
        found.append("native-compile-draft.yml: publishes an artifact")
    if "Recheck protection after reviewer approval" not in compile_workflow:
        found.append("native-compile-draft.yml does not recheck after review")
    if "--source-sha '${{ inputs.source_sha }}'" in compile_workflow or "--platform '${{ inputs.platform }}'" in compile_workflow:
        found.append("native-compile-draft.yml interpolates inputs into shell")
    return found + store_problems(store) + held_uploads_problems(root, store) + upload_track_problems(root)


def printed_error_classes(trees):
    """PRINTED_ERRORS plus every class, in any module, that derives from one."""
    names, changed = set(PRINTED_ERRORS), True
    while changed:
        changed = False
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name not in names and any(
                        (isinstance(b, ast.Name) and b.id in names) or
                        (isinstance(b, ast.Attribute) and b.attr in names) for b in node.bases):
                    names.add(node.name)
                    changed = True
    return names


def called_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def one_literal(call):
    return not call.keywords and len(call.args) == 1 and \
        isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)


def bound_names(node):
    """The names a node binds, in every binding form Python has."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.alias):
        return [node.asname or node.name.split(".")[0]]
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    return []


def status_error_problems(trees):
    """The pinned PlayStatusError: one class, exactly STATUS_ERROR_SOURCE, nothing rebound under it."""
    found = []
    pinned = ast.dump(ast.parse(STATUS_ERROR_SOURCE).body[0])
    classes = [(path, node) for path, tree in trees.items() for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef) and node.name == STATUS_ERROR]
    if len(classes) != 1 or classes[0][0].name != "play_upload.py" or \
            classes[0][1] not in trees[classes[0][0]].body:
        found.append(f"play_upload.py: {STATUS_ERROR} must be defined exactly once, at its top level")
    for path, node in classes:
        if ast.dump(node) != pinned:
            found.append(f"{path.name}:{node.lineno}: {STATUS_ERROR} differs from STATUS_ERROR_SOURCE "
                         "(printed error message is not pinned)")
    for path, tree in trees.items():
        if path.name != "play_upload.py":
            continue
        for node in ast.walk(tree):
            for name in bound_names(node):
                if name in PROTECTED_NAMES and not (
                        (name == STATUS_ERROR and (path, node) in classes) or
                        (name == "StoreError" and isinstance(node, ast.alias) and node.asname is None)):
                    found.append(f"{path.name}:{getattr(node, 'lineno', 0)}: rebinds {name}, "
                                 f"which the pinned {STATUS_ERROR} relies on")
            if isinstance(node, ast.ImportFrom) and any(alias.name == "StoreError" for alias in node.names) \
                    and (node.module != "store" or node.level):
                found.append(f"{path.name}:{node.lineno}: StoreError must come from store")
            used = [node.id] if isinstance(node, ast.Name) else \
                [alias.name.split(".")[0] for alias in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) \
                else [node.attr] if isinstance(node, ast.Attribute) else []
            for name in used:
                if name in FORBIDDEN_NAMES:
                    found.append(f"{path.name}:{node.lineno}: uses {name}, a way round the {STATUS_ERROR} pin")
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and (
                    node.attr == "args" or (isinstance(node.value, ast.Name) and node.value.id == STATUS_ERROR)):
                found.append(f"{path.name}:{node.lineno}: printed error message is assigned directly (.args or a class attribute)")
    return found


def message_problems(root):
    found = []
    trees = {path: ast.parse(path.read_text(), str(path))
             for path in sorted((root / "tools/may_release").glob("*.py")) if not path.name.startswith("test_")}
    printed = printed_error_classes(trees)
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node.func) in printed and not one_literal(node):
                if not (path.name == "play_upload.py" and isinstance(node.func, ast.Name) and
                        node.func.id == STATUS_ERROR and not node.keywords and len(node.args) == 1 and
                        isinstance(node.args[0], ast.Attribute) and node.args[0].attr == "status_code" and
                        isinstance(node.args[0].value, ast.Name)):
                    found.append(f"{path.name}:{node.lineno}: printed error message is not one string literal")
            if isinstance(node, ast.ClassDef) and node.name in printed and node.name != STATUS_ERROR:
                # A printed error's own constructor sets its message too. (The status
                # error's constructor is pinned whole by status_error_problems.)
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and called_name(call.func) == "__init__" and not one_literal(call):
                        found.append(f"{path.name}:{call.lineno}: printed error message is not one string literal")
            if isinstance(node, ast.Raise) and node.exc is not None and not isinstance(node.exc, ast.Call) \
                    and called_name(node.exc) in printed:
                found.append(f"{path.name}:{node.lineno}: printed error raised without a message")
    return found + status_error_problems(trees)


def problems(root=ROOT):
    return workflow_problems(root) + control_problems(root) + message_problems(root)


if __name__ == "__main__":
    failures = problems()
    for failure in failures:
        print("Static check failed: " + failure)
    if failures:
        sys.exit(1)
    print("May store workflow is at activation stage 3 (Android uploads to the Play internal track for May "
          "and May Playground, May production sign-only in its own job with no Play secret, each checked "
          "loudly; iOS inert), the native compile workflow compiles iOS May Playground only, and the no-secret controls are "
          "structurally present")
