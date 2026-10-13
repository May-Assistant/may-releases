"""No-secret structural guard for this draft public control repository.

problems(root) returns fixed descriptions of every violated rule; the CLI exits
non-zero if there is any. Dependency-free on purpose: it runs in the PR check.
"""

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
PINNED_USES = re.compile(r"(\./\S+|[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}|docker://\S+@sha256:[0-9a-f]{64})")
# Messages these exceptions carry may be printed by a CLI, so each must be one
# string literal: never formatted, never derived from provider or file data.
PRINTED_ERRORS = {"StoreError", "PlayRefusal", "GateDenial", "VerifyError"}
STEP_SECRETS = {
    "android": {
        "Fetch exact private source and pinned design system": {"MAY_APP_READ_KEY", "MAY_DS_READ_KEY"},
        "Sign and verify Android App Bundle": {"MAY_ANDROID_KEYSTORE_BASE64", "MAY_ANDROID_STORE_PASSWORD",
                                               "MAY_ANDROID_KEY_PASSWORD"},
        "Upload update to the internal track only": {"MAY_PLAY_JSON"},
    },
    "ios": {
        "Fetch exact private source and pinned design system": {"MAY_APP_READ_KEY", "MAY_DS_READ_KEY"},
        "Sign and verify internal-TestFlight IPA": {"MAY_IOS_CERT_BASE64", "MAY_IOS_CERT_PASSWORD",
                                                    "MAY_IOS_PROFILE_BASE64"},
        "Upload verified IPA for internal TestFlight": {"MAY_ASC_KEY_BASE64", "MAY_ASC_ISSUER_ID"},
    },
}
STEP_ORDER = {
    "android": ["Install hash-locked Play client without secrets",
                "Fetch exact private source and pinned design system",
                "Build unsigned Android App Bundle without secrets",
                "Sign and verify Android App Bundle",
                "Upload update to the internal track only"],
    "ios": ["Fetch exact private source and pinned design system",
            "Build unsigned iOS archive without secrets",
            "Sign and verify internal-TestFlight IPA",
            "Upload verified IPA for internal TestFlight"],
}


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


def secrets_in(text):
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", text))


def workflow_problems(root):
    found = []
    workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
    if not workflows:
        found.append("No workflows found")
    for path in workflows:
        text = path.read_text()
        for ref in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)", text):
            if not PINNED_USES.fullmatch(ref):
                found.append(f"{path.name}: action reference is not pinned to a 40-hex commit SHA")
        if re.search(r"(?m)^\s*deployments:", text):
            found.append(f"{path.name}: requests deployments permission")
        if "pull_request_target" in text:
            found.append(f"{path.name}: uses pull_request_target")
        for job in re.findall(r"(?m)^  ([\w-]+):\n", text.partition("\njobs:\n")[2]):
            header = job_text(text, job).partition("    steps:\n")[0]
            if re.search(r"(?m)^    environment:", header) and not re.search(r"(?m)^    needs: admission$", header):
                found.append(f"{path.name}: protected job {job} does not wait for admission")
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
    if store.count("github.event_name == 'may-store-disabled-pending-review'") != 3:
        found.append("store-release-draft.yml is not inert")
    for name, text in (("native-compile-draft.yml", compile_workflow), ("store-release-draft.yml", store)):
        if "environment: ${{ needs.admission.outputs.environment }}" not in text:
            found.append(f"{name}: protected environment is not chosen by admission")
        if "upload-artifact" in text:
            found.append(f"{name}: publishes an artifact")
    if "Recheck protection after reviewer approval" not in compile_workflow:
        found.append("native-compile-draft.yml does not recheck after review")
    if "--source-sha '${{ inputs.source_sha }}'" in compile_workflow or "--platform '${{ inputs.platform }}'" in compile_workflow:
        found.append("native-compile-draft.yml interpolates inputs into shell")
    if store.count("Recheck protected destination after Samuel's review") != 2:
        found.append("store-release-draft.yml does not recheck after review")
    if "track: production" in store or "--track production" in store:
        found.append("store-release-draft.yml names the production track")
    for line in store.splitlines():
        if "pip" in line and "install" in line and not all(
                flag in line for flag in ("--require-hashes", "--only-binary=:all:",
                                          "-r tools/may_release/requirements-play.txt")):
            found.append("store-release-draft.yml installs packages without hash-locked wheels")
    for job, expected in STEP_SECRETS.items():
        header, steps = job_steps(store, job)
        if any("${{" in text.partition("run:")[2] for _, text in steps):
            found.append(f"store {job}: a run script interpolates an expression into shell")
        if secrets_in(header):
            found.append(f"store {job}: job-level environment holds a secret")
        names = [name for name, _ in steps]
        if [n for n in names if n in STEP_ORDER[job]] != STEP_ORDER[job]:
            found.append(f"store {job}: build, sign and upload steps are missing or out of order")
        actual = {name: secrets_in(text) for name, text in steps if secrets_in(text)}
        if actual != expected:
            found.append(f"store {job}: a step holds secrets it does not need")
    return found


def requirements_problems(root):
    found = []
    text = (root / "tools/may_release/requirements-play.txt").read_text()
    entries = re.findall(r"(?m)^([A-Za-z0-9._-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text)
    requirement_lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith(("#", "--hash"))]
    if len(entries) != len(requirement_lines) or not entries:
        found.append("requirements-play.txt has a requirement without a pinned version and hash")
    if ("google-auth", "2.40.3") not in {(name, version) for name, version, _ in entries}:
        found.append("requirements-play.txt does not pin google-auth 2.40.3")
    return found


def message_problems(root):
    found = []
    for path in sorted((root / "tools/may_release").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in PRINTED_ERRORS:
                if node.keywords or len(node.args) != 1 or not (
                        isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    found.append(f"{path.name}:{node.lineno}: printed error message is not one string literal")
    return found


def problems(root=ROOT):
    return (workflow_problems(root) + control_problems(root) + requirements_problems(root) +
            message_problems(root))


if __name__ == "__main__":
    failures = problems()
    for failure in failures:
        print("Static check failed: " + failure)
    if failures:
        sys.exit(1)
    print("May public draft is inert and its no-secret controls are structurally present")
