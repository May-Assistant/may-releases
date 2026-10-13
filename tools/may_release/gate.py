"""No-secret admission for May's public control repository.

Only fixed validation messages are emitted; GitHub response bodies are never printed.
Every GateDenial message is a fixed literal (static_check.py enforces this), so the
CLI prints it.

A protected job's `environment:` would silently auto-create a missing environment
with no protection. This gate runs first and denies on a 404, so that never
happens: the environments must be created, by hand, before any dispatch.
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
    ("ios", "production"): "may-ios-internal",  # CONFIRM with setup lane
    ("ios", "playground"): "may-ios-playground-internal",  # CONFIRM with setup lane
    ("android", "production"): "android-production",  # created by the setup lane (May)
    ("android", "playground"): "android-playground",  # created by the setup lane (May Playground)
}
MAX_BUILD_NUMBER = 2_000_000_000


class GateDenial(ValueError):
    pass


def validate_run(repo, ref, event, sha, action, build_number=None, target="production"):
    if (repo, ref, event) != (CONTROL_REPO, "refs/heads/main", "workflow_dispatch"):
        raise GateDenial("May control must run by manual dispatch from its main branch")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateDenial("Private source must be an exact lowercase commit SHA")
    key = "compile" if action == "compile" else (action, target)
    if key not in ENVIRONMENTS:
        raise GateDenial("Unknown May control action")
    if action != "compile" and not (re.fullmatch(r"[1-9][0-9]{0,9}", build_number or "")
                                    and int(build_number) <= MAX_BUILD_NUMBER):
        raise GateDenial("Store build number must be a positive integer no greater than 2000000000")
    return ENVIRONMENTS[key]


def validate_environment(data, policies, dispatcher_id=None, dispatcher_login=None):
    if data.get("can_admins_bypass") is not False:
        raise GateDenial("Administrator bypass must be disabled")
    branch = data.get("deployment_branch_policy") or {}
    if branch.get("custom_branch_policies") is not True or branch.get("protected_branches") is not False:
        raise GateDenial("Environment must use explicit branch policy")
    if sorted(p.get("name") for p in policies.get("branch_policies", [])) != ["main"]:
        raise GateDenial("Environment must allow only main")
    rules = [r for r in data.get("protection_rules", []) if r.get("type") == "required_reviewers"]
    if len(rules) != 1:
        raise GateDenial("One required-reviewer rule is required")
    reviewers = rules[0].get("reviewers", [])
    if len(reviewers) != 1 or reviewers[0].get("type") != "User" or reviewers[0].get("reviewer", {}).get("id") != OWNER_ID:
        raise GateDenial("Samuel must be the sole required reviewer")
    prevent_self_review = rules[0].get("prevent_self_review")
    if not isinstance(prevent_self_review, bool):
        raise GateDenial("Environment self-review setting is unreadable")
    if prevent_self_review:
        # With self-review prevented, the sole reviewer can never approve a run
        # they dispatched, so it would wait until it times out. Fail closed when
        # the dispatcher cannot be identified.
        reviewer = reviewers[0].get("reviewer", {})
        login = reviewer.get("login")
        if not dispatcher_id and not dispatcher_login:
            raise GateDenial("Dispatcher identity is unavailable while self-review is prevented")
        if str(dispatcher_id) == str(OWNER_ID) or (
                isinstance(login, str) and (dispatcher_login or "").lower() == login.lower()):
            raise GateDenial("Self-review is prevented and the sole required reviewer dispatched this run, "
                             "so it can never be approved")


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
        if error.code == 404:
            raise GateDenial("Protected environment does not exist; it must never be auto-created") from None
        raise GateDenial("Protected environment metadata unavailable") from None
    except (urllib.error.URLError, TimeoutError):
        raise GateDenial("Protected environment metadata unavailable") from None


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
        raise GateDenial("GitHub metadata token unavailable")
    base = f"environments/{env}"
    validate_environment(get_json(base, token),
                         get_json(base + "/deployment-branch-policies?per_page=100", token),
                         os.environ.get("GITHUB_ACTOR_ID"), os.environ.get("GITHUB_TRIGGERING_ACTOR"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"environment={env}\n")
    print(f"May {args.action} admission passed for {args.source_sha}")


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May control admission denied: " + denial.args[0]) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit("May control admission denied; inspect protected environment and inputs") from None
