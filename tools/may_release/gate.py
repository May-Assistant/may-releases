"""No-secret admission for May's public control repository.

Only fixed validation messages are emitted; GitHub response bodies are never printed.
Every GateDenial message is a fixed literal (static_check.py enforces this), so the
CLI prints it.

A protected job's `environment:` would silently auto-create a missing environment
with no protection. This gate runs first and denies on a 404, so that never
happens: the environments must be created, by hand, before any dispatch.

Every store run builds in `may-native-compile` (read-only source keys only) and
signs and uploads in the target's store environment, so admission checks both.
It also denies while the target's committed artifact public key is still a
placeholder.

Self-review: the gate denies only when self-review is prevented and the sole
reviewer dispatched (the run could never be approved). With it off, Samuel may
dispatch and approve his own run. That is acceptable only because every path in
this repository targets internal testing alone (the Play `internal` track and
internal-only TestFlight); there is no track input to choose anything else.
Before any production or public-beta release, Prevent self-review must be on and
a second reviewer added (README, "Before any non-internal release").

Activation stage 3: admission refuses any dispatch whose dry_run is not true unless
it asks for an upload this stage allows: a platform whose upload is not held
(UPLOADS_HELD) and, for Android, a target store.PLAY_UPLOAD_TRACKS maps to the
Play `internal` track (stage 3: May and May Playground). Any other dry_run: false
dispatch fails at once, before any approval, build or secret, rather than
finishing green without having uploaded anything. (A dispatch that is admitted
and then does not upload still fails: confirm_upload.py, after the upload step.)

Every store dispatch, dry runs included, must also use a build number at or above
its app's floor (store.MIN_BUILD_NUMBERS: May production Android starts at 3).

Sign-only (sign_only: true, never the default) is admitted only for May production
Android (store.SIGN_ONLY_TARGETS), only with dry_run: true (it never uploads), and
only once the Console-upload public key (store.SIGN_ONLY_RECIPIENT) is committed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

# Run as `python3 -I -S -B`: the script folder is not on sys.path, so append it
# after the standard library. A file planted here cannot shadow a stdlib import.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from store import (PLAY_UPLOAD_TRACKS, SIGN_ONLY_RECIPIENT, SIGN_ONLY_TARGETS, build_number_floor_ok)

CONTROL_REPO = "May-Assistant/may-releases"
OWNER_ID = 58840187
# Exact names confirmed from the repository's Actions secrets page, 2026-10-13.
ENVIRONMENTS = {
    "compile": "may-native-compile",
    ("ios", "production"): "may-ios-internal",
    ("ios", "playground"): "may-ios-playground-internal",
    ("android", "production"): "may-android-internal",
    ("android", "playground"): "may-android-playground-internal",
}
MAX_BUILD_NUMBER = 2_000_000_000
# Stage 3: the store upload step of each platform listed here carries the inert
# guard, so it can never run; static_check.py requires this set to be exactly the
# platforms whose upload step is held. Stage 1 held both; stage 2 lifted Android.
UPLOADS_HELD = {"ios"}
ARTIFACT_KEYS = Path(__file__).resolve().parent / "artifact_keys"
AGE_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]{58}")


class GateDenial(ValueError):
    pass


def artifact_recipient(environment):
    """The committed age public key for a store environment, or None if a placeholder."""
    try:
        text = (ARTIFACT_KEYS / (environment + ".age.pub")).read_text().strip()
    except OSError:
        return None
    return text if AGE_RECIPIENT.fullmatch(text) else None


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
    if action != "compile" and not build_number_floor_ok(action, target, build_number):
        raise GateDenial("Store build number is below this app's floor (May production Android starts at 3: "
                         "versionCode 1 was a manual upload and 2 was built for Dots)")
    return ENVIRONMENTS[key]


def validate_dry_run(action, dry_run, target="production"):
    """Admission passes the dispatch's dry_run (github.event.inputs.dry_run, a string).
    The rechecks inside protected jobs pass none, so None is not checked."""
    if dry_run is None:
        return
    if dry_run not in ("true", "false"):
        raise GateDenial("dry_run must be true or false")
    if dry_run == "true":
        return
    if action in UPLOADS_HELD:
        raise GateDenial("Uploads are held for this platform (stage 3 holds iOS); this run did not upload "
                         "anything. dry_run: false fails here by design; dispatch with dry_run: true")
    if action != "android" or PLAY_UPLOAD_TRACKS.get(target) != "internal":
        raise GateDenial("Stage 3 admits only an Android May or May Playground upload to the Play internal "
                         "track; this run did not upload anything. Dispatch with dry_run: true")


def validate_sign_only(action, sign_only, dry_run, target="production"):
    """Admission passes the dispatch's sign_only (a string). The rechecks pass none."""
    if sign_only is None:
        return
    if sign_only not in ("true", "false"):
        raise GateDenial("sign_only must be true or false")
    if sign_only == "false":
        return
    if (action, target) not in SIGN_ONLY_TARGETS:
        raise GateDenial("sign_only is only for the May production Android app (target production); "
                         "nothing was signed. Dispatch without sign_only")
    if dry_run != "true":
        raise GateDenial("sign_only never uploads: dispatch it with dry_run: true; nothing was signed")
    if artifact_recipient(SIGN_ONLY_RECIPIENT) is None:
        raise GateDenial("The Console-upload public key (artifact_keys/may-android-console-upload.age.pub) is "
                         "still a placeholder; Samuel's public key must be committed before sign_only can run")


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
    parser.add_argument("--dry-run")
    parser.add_argument("--sign-only")
    args = parser.parse_args()
    env = validate_run(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
                       os.environ.get("GITHUB_EVENT_NAME"), args.source_sha, args.action,
                       args.build_number, args.target)
    validate_dry_run(args.action, args.dry_run, args.target)
    validate_sign_only(args.action, args.sign_only, args.dry_run, args.target)
    build_env = ENVIRONMENTS["compile"]
    if args.action != "compile" and artifact_recipient(env) is None:
        raise GateDenial("Artifact encryption public key for this target is still a placeholder; "
                         "the setup lane must commit it first")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    for name in sorted({env, build_env}):
        base = f"environments/{name}"
        validate_environment(get_json(base, token),
                             get_json(base + "/deployment-branch-policies?per_page=100", token),
                             os.environ.get("GITHUB_ACTOR_ID"), os.environ.get("GITHUB_TRIGGERING_ACTOR"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"environment={env}\nbuild_environment={build_env}\n")
    print(f"May {args.action} admission passed for {args.source_sha}")


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May control admission denied: " + denial.args[0]) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit("May control admission denied; inspect protected environment and inputs") from None
