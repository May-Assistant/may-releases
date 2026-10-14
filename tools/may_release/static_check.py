"""No-secret structural guard for this public control repository.

problems(root) returns fixed descriptions of every violated rule; the CLI exits
non-zero if there is any. Dependency-free on purpose: it runs in the PR check.

It also pins the store workflow's activation stage (STAGE, STORE_JOB_IF,
UPLOAD_IF): exactly which jobs and steps carry the never-matching event guard.
Moving to another stage, or back to fully inert, means changing those constants
in the same reviewed change as the workflow, never the workflow alone.
"""

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
PINNED_USES = re.compile(r"(\./\S+|[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}|docker://\S+@sha256:[0-9a-f]{64})")
# Messages these exceptions (and any subclass) carry may be printed by a CLI, so
# each must be one string literal: never formatted, never from provider or file data.
PRINTED_ERRORS = {"StoreError", "PlayRefusal", "GateDenial", "VerifyError"}
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
UPLOAD_ARTIFACT = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
DOWNLOAD_ARTIFACT = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
FETCH = "Fetch exact private source and pinned design system"
DECRYPT = "Decrypt and check the build handoff"
REPORT = "Report what the dry run would have uploaded"
UPLOADS = {"android-release": "Upload update to the internal track only",
           "ios-release": "Upload verified IPA for internal TestFlight"}
# A job or step whose condition requires this event never runs: the workflow
# registers only workflow_dispatch. That is how a job or step is held inert.
INERT = "github.event_name == 'may-store-disabled-pending-review'"
STAGE = "stage 1"
# Activation stage 1 (Android dry runs only). Each store job's exact job-level
# `if:` (None: no condition). Admission and the Android jobs are active and keep
# their platform condition; the iOS jobs stay inert (tools/release/ios.sh does not
# exist yet). Every store upload step is inert too (UPLOAD_IF). The guard appears
# nowhere else in the workflow's code.
STORE_JOB_IF = {
    "admission": None,
    "android-build": "${{ inputs.platform == 'android' }}",
    "android-release": "${{ inputs.platform == 'android' }}",
    "ios-build": "${{ " + INERT + " && inputs.platform == 'ios' }}",
    "ios-release": "${{ " + INERT + " && inputs.platform == 'ios' }}",
}
# A store upload runs only when dry_run is exactly 'false' (github.event.inputs
# holds booleans as strings, so a missing value never uploads), and the upload
# script receives the same value to recheck. The report runs in every other case.
# In stage 1 each upload is also held inert (UPLOAD_IF), so no upload runs at all;
# stage 2 removes the guard from the Android upload only, leaving DRY_RUN_UPLOAD_IF.
DRY_RUN_UPLOAD_IF = "${{ github.event.inputs.dry_run == 'false' }}"
UPLOAD_IF = "${{ " + INERT + " && github.event.inputs.dry_run == 'false' }}"
REPORT_IF = "${{ github.event.inputs.dry_run != 'false' }}"
DRY_RUN_ENV = "${{ github.event.inputs.dry_run }}"
DRY_RUN_INPUT = ("      dry_run:\n        description: [^\n]+\n        required: true\n"
                 "        type: boolean\n        default: true\n")
READ_KEYS = {"MAY_APP_READ_KEY", "MAY_DS_READ_KEY"}
# Store workflow: job -> (ordered named steps, {step: exact secrets}, allowed actions).
STORE_JOBS = {
    "android-build": (
        ["Install pinned JDK and age", FETCH, "Build unsigned Android App Bundle without secrets",
         "Encrypt unsigned bundle to the target's public key"],
        {FETCH: READ_KEYS}, {CHECKOUT, UPLOAD_ARTIFACT}),
    "android-release": (
        ["Install pinned JDK and age", DECRYPT, "Sign and verify Android App Bundle",
         "Upload update to the internal track only", REPORT],
        {DECRYPT: {"MAY_ARTIFACT_DECRYPT_KEY"},
         "Sign and verify Android App Bundle": {"MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                                                "MAY_ANDROID_KEY_PASSWORD"},
         "Upload update to the internal track only": {"MAY_PLAY_JSON"}},
        {CHECKOUT, DOWNLOAD_ARTIFACT}),
    "ios-build": (
        ["Install pinned age", FETCH, "Build unsigned iOS archive without secrets",
         "Encrypt unsigned archive to the target's public key"],
        {FETCH: READ_KEYS}, {CHECKOUT, UPLOAD_ARTIFACT}),
    "ios-release": (
        ["Install pinned age", DECRYPT, "Sign and verify internal-TestFlight IPA",
         "Upload verified IPA for internal TestFlight", REPORT],
        {DECRYPT: {"MAY_ARTIFACT_DECRYPT_KEY"},
         "Sign and verify internal-TestFlight IPA": {"MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD",
                                                     "MAY_IOS_PROFILE_BASE64"},
         "Upload verified IPA for internal TestFlight": {"MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID"}},
        {CHECKOUT, DOWNLOAD_ARTIFACT}),
}
# In a release job every command is this repository's own Python, with arguments
# from a safe character set: no ; | & ` ( ) < > { } or newline, so no $( or chaining.
RELEASE_COMMAND = re.compile(
    r"python3 -I -S -B tools/may_release/(gate|toolchain|handoff|sign|play_upload|ios_upload|dry_run|cleanup|integrity)\.py"
    r"( [A-Za-z0-9_./\"$=-]+)*")
ADMISSION_GATE = ('python3 -I -S -B tools/may_release/gate.py --action "$MAY_PLATFORM" --target "$MAY_TARGET" '
                  '--source-sha "$MAY_SOURCE_SHA" --build-number "$MAY_BUILD_NUMBER"')
CONTINUE_ON_ERROR = re.compile(r"(?m)^\s*(-\s+)?continue-on-error:")
VARS = {"MAY_ANDROID_KEY_ALIAS", "MAY_ANDROID_CERT_SHA256", "MAY_PLAY_ACCOUNT", "MAY_ASC_KEY_ID"}
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
    held = sum(1 for c in STORE_JOB_IF.values() if c and INERT in c) + len(UPLOADS)
    if code_lines(store).count(INERT) != held:
        found.append(f"store-release-draft.yml: the inert guard appears or is missing somewhere {STAGE} does not expect")
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


def release_env_allowed(name, value, secrets):
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
    if store.count("Recheck protected destination after Samuel's review") != 2 or \
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
    if not re.search(r"(?m)^" + DRY_RUN_INPUT, store.partition("\njobs:\n")[0]):
        found.append("store-release-draft.yml: dry_run is not a required boolean input defaulting to true")
    for job, (order, expected, actions) in STORE_JOBS.items():
        header, steps = job_steps(store, job)
        release = job.endswith("-release")
        build = job.replace("-release", "-build")
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
        # The dry run must still build, seal, open, sign and verify: only the
        # upload and the report are conditional (cleanup always runs).
        for name, text in steps:
            condition = step_condition(text)
            if step_condition_count(text) > 1:
                found.append(f"store {job}: a step has more than one if:")
            if release and name == UPLOADS[job]:
                # Together these require exactly UPLOAD_IF: the guard first, then
                # exactly the dry-run condition stage 2 will leave in place.
                if condition is None or not condition.startswith("${{ " + INERT + " && "):
                    found.append(f"store {job}: the upload is not held inert ({STAGE})")
                if (condition or "").replace(INERT + " && ", "", 1) != DRY_RUN_UPLOAD_IF or \
                        ("          MAY_DRY_RUN: " + DRY_RUN_ENV + "\n") not in text:
                    found.append(f"store {job}: a dry run would run the store upload")
            elif release and name == REPORT:
                if condition != REPORT_IF or not run_commands(text) or not run_commands(text)[0].startswith(
                        "python3 -I -S -B tools/may_release/dry_run.py --platform " + platform + " "):
                    found.append(f"store {job}: the dry run does not report what it would have uploaded")
            elif condition not in (None, "always()") or (condition == "always()" and "cleanup.py" not in text):
                found.append(f"store {job}: a step other than the upload or dry-run report is conditional")
        for _, text in steps:
            if UPLOAD_ARTIFACT in text and "path: ${{ runner.temp }}/may-handoff/handoff.age\n" not in text:
                found.append(f"store {job}: uploads something other than the encrypted handoff")
        for _, text in steps:
            if ("upload-artifact@" in text or "download-artifact@" in text) and \
                    not re.search(r"(?m)^          name: may-handoff-(android|ios)-\$\{\{ github\.run_attempt \}\}$", text):
                found.append(f"store {job}: handoff artifact is not bound to this run attempt")
        if release:
            all_secrets = set().union(*expected.values())
            body = job_text(store, job)
            if re.search(r"(?m)^\s+(shell|defaults|working-directory):", body):
                found.append(f"store {job}: overrides the shell, defaults or working directory")
            if not all(release_env_allowed(n, v, all_secrets) for n, v in env_entries(body)):
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


def control_problems(root):
    found = []
    flows = root / ".github/workflows"
    check = (flows / "control-check.yml").read_text()
    compile_workflow = (flows / "native-compile-draft.yml").read_text()
    store = (flows / "store-release-draft.yml").read_text()
    if not ("pull_request:" in check and "permissions:\n  contents: read" in check and "secrets." not in check):
        found.append("control-check.yml is not a read-only, no-secret PR check")
    if compile_workflow.count("if: ${{ github.event_name == 'may-compile-disabled-pending-review' }}") != 2:
        found.append("native-compile-draft.yml is not inert")
    for name, text in (("native-compile-draft.yml", compile_workflow), ("store-release-draft.yml", store)):
        if "environment: ${{ needs.admission.outputs." not in text:
            found.append(f"{name}: protected environment is not chosen by admission")
    if "upload-artifact" in compile_workflow:
        found.append("native-compile-draft.yml: publishes an artifact")
    if "Recheck protection after reviewer approval" not in compile_workflow:
        found.append("native-compile-draft.yml does not recheck after review")
    if "--source-sha '${{ inputs.source_sha }}'" in compile_workflow or "--platform '${{ inputs.platform }}'" in compile_workflow:
        found.append("native-compile-draft.yml interpolates inputs into shell")
    return found + store_problems(store)


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


def message_problems(root):
    found = []
    trees = {path: ast.parse(path.read_text(), str(path))
             for path in sorted((root / "tools/may_release").glob("*.py")) if not path.name.startswith("test_")}
    printed = printed_error_classes(trees)
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node.func) in printed:
                if node.keywords or len(node.args) != 1 or not (
                        isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    found.append(f"{path.name}:{node.lineno}: printed error message is not one string literal")
            if isinstance(node, ast.Raise) and node.exc is not None and not isinstance(node.exc, ast.Call) \
                    and called_name(node.exc) in printed:
                found.append(f"{path.name}:{node.lineno}: printed error raised without a message")
    return found


def problems(root=ROOT):
    return workflow_problems(root) + control_problems(root) + message_problems(root)


if __name__ == "__main__":
    failures = problems()
    for failure in failures:
        print("Static check failed: " + failure)
    if failures:
        sys.exit(1)
    print("May store workflow is at activation stage 1 (Android dry runs only; uploads and iOS inert), "
          "the native compile workflow is inert, and the no-secret controls are structurally present")
