"""No-secret admission for May's public control repository.

Only fixed validation messages are emitted; GitHub response bodies are never printed.
"""

import argparse
import json
import os
import re
import urllib.error
import urllib.request

CONTROL_REPO = "May-Assistant/may-releases"
OWNER_ID = 58840187
ENVIRONMENTS = {
    "compile": "may-native-compile",
    ("ios", "production"): "may-ios-internal",
    ("ios", "playground"): "may-ios-playground-internal",
    ("android", "production"): "may-android-internal",
    ("android", "playground"): "may-android-playground-internal",
}


def validate_run(repo, ref, event, sha, action, build_number=None, target="production"):
    if (repo, ref, event) != (CONTROL_REPO, "refs/heads/main", "workflow_dispatch"):
        raise ValueError("May control must run by manual dispatch from its main branch")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Private source must be an exact lowercase commit SHA")
    key = "compile" if action == "compile" else (action, target)
    if key not in ENVIRONMENTS:
        raise ValueError("Unknown May control action")
    if action != "compile" and not re.fullmatch(r"[1-9][0-9]{0,8}", build_number or ""):
        raise ValueError("Store build number must be a positive decimal integer")
    return ENVIRONMENTS[key]


def validate_environment(data, policies):
    if data.get("can_admins_bypass") is not False:
        raise ValueError("Administrator bypass must be disabled")
    branch = data.get("deployment_branch_policy") or {}
    if branch.get("custom_branch_policies") is not True or branch.get("protected_branches") is not False:
        raise ValueError("Environment must use explicit branch policy")
    if sorted(p.get("name") for p in policies.get("branch_policies", [])) != ["main"]:
        raise ValueError("Environment must allow only main")
    rules = [r for r in data.get("protection_rules", []) if r.get("type") == "required_reviewers"]
    if len(rules) != 1:
        raise ValueError("One required-reviewer rule is required")
    reviewers = rules[0].get("reviewers", [])
    if len(reviewers) != 1 or reviewers[0].get("type") != "User" or reviewers[0].get("reviewer", {}).get("id") != OWNER_ID:
        raise ValueError("Samuel must be the sole required reviewer")


def get_json(path, token):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{CONTROL_REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Protected environment metadata unavailable (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("Protected environment metadata unavailable") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("compile", "ios", "android"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number")
    parser.add_argument("--target", choices=("production", "playground"), default="production")
    args = parser.parse_args()
    env = validate_run(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
                       os.environ.get("GITHUB_EVENT_NAME"), args.source_sha, args.action,
                       args.build_number, args.target)
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GitHub metadata token unavailable")
    base = f"environments/{env}"
    validate_environment(get_json(base, token),
                         get_json(base + "/deployment-branch-policies?per_page=100", token))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"environment={env}\n")
    print(f"May {args.action} admission passed for {args.source_sha}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TypeError, KeyError):
        raise SystemExit("May control admission denied; inspect protected environment and inputs") from None
