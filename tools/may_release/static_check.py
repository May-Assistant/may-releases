"""No-secret structural guard for this draft public control repository."""

from pathlib import Path

root = Path(__file__).resolve().parents[2]
check = (root / ".github/workflows/control-check.yml").read_text()
compile_workflow = (root / ".github/workflows/native-compile-draft.yml").read_text()
assert "pull_request:" in check and "permissions:\n  contents: read" in check
assert "secrets." not in check and "pull_request_target" not in check
assert compile_workflow.count("if: ${{ github.event_name == 'may-compile-disabled-pending-review' }}") == 2
assert "environment: ${{ needs.admission.outputs.environment }}" in compile_workflow
assert "actions: read" in compile_workflow and "deployments: read" in compile_workflow
assert "Recheck protection after reviewer approval" in compile_workflow
assert "secrets.MAY_APP_READ_KEY" in compile_workflow and "secrets.MAY_DS_READ_KEY" in compile_workflow
assert "upload-artifact" not in compile_workflow and "pull_request_target" not in compile_workflow
assert "--source-sha '${{ inputs.source_sha }}'" not in compile_workflow
assert "--platform '${{ inputs.platform }}'" not in compile_workflow
print("May public draft is inert and its no-secret controls are structurally present")
