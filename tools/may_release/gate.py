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

Activation stage 4: admission refuses any dispatch whose dry_run is not true unless
it asks for an upload this stage allows: a platform whose upload is not held
(UPLOADS_HELD) and, for Android, a target store.PLAY_UPLOAD_TRACKS maps to the
Play `internal` track (May and May Playground), or, for iOS, a target in
TESTFLIGHT_TARGETS (May and May Playground; ios_upload.py exports for internal
TestFlight only). Any other dry_run: false
dispatch fails at once, before any approval, build or secret, rather than
finishing green without having uploaded anything. (A dispatch that is admitted
and then does not upload still fails: confirm_upload.py, after the upload step.)

Every store dispatch, dry runs included, must also use a build number at or above
its app's floor (store.MIN_BUILD_NUMBERS: May production Android starts at 3).

Sign-only (sign_only: true, never the default) is admitted only for May production
Android (store.SIGN_ONLY_TARGETS), only with dry_run: true (it never uploads), and
only once the Console-upload public key (store.SIGN_ONLY_RECIPIENT) is committed.

Compile of a pull request head (action compile-pr, native-compile-pr.yml): only a
may-app commit that is, right now, the head of an open pull request against may-app's
main, from may-app itself (never a fork). It builds in its own environment,
may-native-compile-pr, never a store environment.
- Samuel's decision (decisions.md, Build 11, 17 Oct): compile-only runs need no approval.
  So that environment has its own policy (NO_REVIEWER_ACTIONS): administrator bypass off,
  branch policy exactly main, and no required-reviewer rule at all (one is denied, so a
  stale rule can never turn the lane into a wait until timeout).
- Admission (no environment, so a missing one is denied on its 404, never auto-created)
  checks the inputs and that policy. The pull request itself is read with --check-pull,
  as the environment job's first step, with MAY_APP_PR_READ_TOKEN held in that
  environment: main-dispatched runs only, never a repository secret any branch can read.

Swift compile of a may-design-system commit (action ds-compile, ds-swift-compile.yml): only a
may-design-system commit that is, right now, the head of an open pull request in may-design-system
itself (never a fork) against claude/may-integration or main (DS_BASES). It builds in its own
environment, may-ds-renders, which also has no required reviewer (Samuel's decision: the compile
errors go to the lane as a comment on that private pull request; it signs, uploads and releases
nothing). Admission checks the inputs and that policy; --check-pull, the environment job's first
step, reads the pull request with MAY_DS_PR_READ_TOKEN, held in that environment.

iOS renders of May Playground (action renders, ios-renders.yml): a may-app main commit, or the current head of an
open may-app pull request against main from may-app itself (--pr-number, optional for this action alone), with an
optional may-design-system commit to render with (--ds-sha). They run in their own environment, may-ios-renders,
which has no required reviewer either (NO_REVIEWER_ACTIONS): the renders sign, upload and release nothing, and are
published only to the private repository may-renders (renders.py, publish.py). That environment holds a write key
for may-renders alone, so this is Samuel's decision to confirm; until then the policy is checked as for the compile
lanes.

Device tests of May Playground against staging (action device-tests, device-tests.yml): a may-app main commit, or
"main" for main's head; the one action a schedule (its nightly run) may start. They run in their own environment,
may-device-tests, with no required reviewer (NO_REVIEWER_ACTIONS), which Samuel is asked to confirm.
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

from store import PLAY_UPLOAD_TRACKS, SIGN_ONLY_RECIPIENT, SIGN_ONLY_TARGETS, build_number_floor_ok

CONTROL_REPO = "May-Assistant/may-releases"
APP_REPO = "May-Assistant/may-app"
DS_REPO = "May-Assistant/may-design-system"
# The may-design-system branches a ds-compile pull request may target.
DS_BASES = ("claude/may-integration", "main")
OWNER_ID = 58840187
# Exact names confirmed from the repository's Actions secrets page, 2026-10-13.
ENVIRONMENTS = {
    "compile": "may-native-compile",
    "compile-pr": "may-native-compile-pr",
    "ds-compile": "may-ds-renders",
    "renders": "may-ios-renders",
    "device-tests": "may-device-tests",
    ("ios", "production"): "may-ios-internal",
    ("ios", "playground"): "may-ios-playground-internal",
    ("android", "production"): "may-android-internal",
    ("android", "playground"): "may-android-playground-internal",
}
MAX_BUILD_NUMBER = 2_000_000_000
# The store upload step of each platform listed here carries the inert guard, so
# it can never run; static_check.py requires this set to be exactly the platforms
# whose upload step is held. Stage 1 held both; stage 2 lifted Android; stage 4
# lifted iOS (Samuel's go, 19 Oct).
UPLOADS_HELD = set()
# Stage 4: the iOS targets admitted to upload, each to internal TestFlight only.
TESTFLIGHT_TARGETS = {"playground", "production"}
ARTIFACT_KEYS = Path(__file__).resolve().parent / "artifact_keys"
# Actions whose environment has no required reviewer (Samuel, 17 Oct: compile-only pull-request runs need no
# approval), the design-system Swift compile (Samuel's decision for ds-swift-compile.yml: it compiles and comments
# on a may-design-system pull request, nothing else), and the iOS renders (ios-renders.yml: simulator snapshots of
# May Playground, published only to the private repository may-renders), and the device tests (device-tests.yml:
# Playground on an emulator against staging, nightly, so nothing can wait for a review). static_check.py pins this to
# exactly these four: no signing, store or release action may join it.
NO_REVIEWER_ACTIONS = {"compile-pr", "ds-compile", "renders", "device-tests"}
# Actions that take a pull request number (and, with --check-pull, read that pull request).
PULL_ACTIONS = ("compile-pr", "ds-compile")
# Actions that may name a pull request (the renders of a may-app pull request head, or of a main commit without one).
OPTIONAL_PULL_ACTIONS = ("renders",)
# Actions that build in their own environment alone, with no store or may-native-compile environment.
OWN_ENVIRONMENT_ACTIONS = ("compile", "compile-pr", "ds-compile", "renders", "device-tests")
# The one action a schedule may start (device-tests.yml's nightly run), and then on may-app main's head ("main").
SCHEDULED_ACTIONS = ("device-tests",)
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?")
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
    events = ("workflow_dispatch", "schedule") if action in SCHEDULED_ACTIONS else ("workflow_dispatch",)
    if (repo, ref) != (CONTROL_REPO, "refs/heads/main") or event not in events:
        raise GateDenial("May control must run by manual dispatch from its main branch")
    if not (re.fullmatch(r"[0-9a-f]{40}", sha) or action in SCHEDULED_ACTIONS and sha == "main"):
        raise GateDenial("Private source must be an exact lowercase commit SHA")
    key = action if action in OWN_ENVIRONMENT_ACTIONS else (action, target)
    if key not in ENVIRONMENTS:
        raise GateDenial("Unknown May control action")
    if action in OWN_ENVIRONMENT_ACTIONS:
        return ENVIRONMENTS[key]
    if not (re.fullmatch(r"[1-9][0-9]{0,9}", build_number or "")
                                    and int(build_number) <= MAX_BUILD_NUMBER):
        raise GateDenial("Store build number must be a positive integer no greater than 2000000000")
    if not build_number_floor_ok(action, target, build_number):
        raise GateDenial("Store build number is below this app's floor (May production Android starts at 3: "
                         "its first release, versionCode 2, was uploaded by hand on 14 Oct)")
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
        raise GateDenial("Uploads are held for this platform; this run did not upload anything. "
                         "dry_run: false fails here by design; dispatch with dry_run: true")
    if action == "android" and PLAY_UPLOAD_TRACKS.get(target) == "internal":
        return
    if action == "ios" and target in TESTFLIGHT_TARGETS:
        return
    raise GateDenial("Stage 4 admits only a May or May Playground upload to the Play internal track or to "
                     "internal TestFlight; this run did not upload anything. Dispatch with dry_run: true")


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
    console = artifact_recipient(SIGN_ONLY_RECIPIENT)
    if console is None:
        raise GateDenial("The Console-upload public key (artifact_keys/may-android-console-upload.age.pub) is "
                         "still a placeholder; Samuel's public key must be committed before sign_only can run")
    if console in {artifact_recipient(environment) for environment in ENVIRONMENTS.values()}:
        raise GateDenial("The Console-upload key must not be any environment's handoff key, whose private half "
                         "is a CI secret; commit a key only Samuel holds")


def pull_number(value):
    if not re.fullmatch(r"[1-9][0-9]{0,6}", value or ""):
        raise GateDenial("Pull request number must be a positive integer")
    return int(value)


def validate_pull_request(pull, number, sha):
    """The may-app pull request (GET /repos/May-Assistant/may-app/pulls/{number}) is open, against
    may-app's main, from may-app itself, and its head is exactly sha. Anything unreadable denies."""
    if not isinstance(pull, dict) or pull.get("number") != number:
        raise GateDenial("Pull request metadata is unreadable")
    if pull.get("state") != "open":
        raise GateDenial("Pull request is not open")
    base, head = pull.get("base") or {}, pull.get("head") or {}
    if base.get("ref") != "main" or (base.get("repo") or {}).get("full_name") != APP_REPO:
        raise GateDenial("Pull request is not against may-app main")
    if (head.get("repo") or {}).get("full_name") != APP_REPO:
        raise GateDenial("Pull request head is not in may-app itself (forks are refused)")
    if head.get("sha") != sha:
        raise GateDenial("Source SHA is not the pull request's current head")


def validate_ds_pull_request(pull, number, sha):
    """The may-design-system pull request (GET /repos/May-Assistant/may-design-system/pulls/{number}) is open, against
    claude/may-integration or main of may-design-system, from may-design-system itself, and its head is exactly sha."""
    if not isinstance(pull, dict) or pull.get("number") != number:
        raise GateDenial("Pull request metadata is unreadable")
    if pull.get("state") != "open":
        raise GateDenial("Pull request is not open")
    base, head = pull.get("base") or {}, pull.get("head") or {}
    if base.get("ref") not in DS_BASES or (base.get("repo") or {}).get("full_name") != DS_REPO:
        raise GateDenial("Pull request is not against may-design-system claude/may-integration or main")
    if (head.get("repo") or {}).get("full_name") != DS_REPO:
        raise GateDenial("Pull request head is not in may-design-system itself (forks are refused)")
    if head.get("sha") != sha:
        raise GateDenial("Design-system SHA is not the pull request's current head")


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


def validate_no_reviewer_environment(data, policies):
    """An environment with no required reviewer (NO_REVIEWER_ACTIONS): administrator bypass off, branch policy
    exactly main, and no required-reviewer rule (deny one: a reviewer here would only hang the run)."""
    if data.get("can_admins_bypass") is not False:
        raise GateDenial("Administrator bypass must be disabled")
    branch = data.get("deployment_branch_policy") or {}
    if branch.get("custom_branch_policies") is not True or branch.get("protected_branches") is not False:
        raise GateDenial("Environment must use explicit branch policy")
    if sorted(p.get("name") for p in policies.get("branch_policies", [])) != ["main"]:
        raise GateDenial("Environment must allow only main")
    if any(r.get("type") == "required_reviewers" for r in data.get("protection_rules", [])):
        raise GateDenial("A compile-only environment must have no required reviewer (Samuel's decision)")


def login_or_unknown(value):
    """A GitHub login for the public admission line, or "unknown": never any other text."""
    return value if isinstance(value, str) and LOGIN.fullmatch(value) else "unknown"


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


def get_app_pull(number, token, repo=APP_REPO):
    """One may-app (or, for ds-compile, may-design-system) pull request, read with a pull-request token. Bodies are
    never printed."""
    if repo not in (APP_REPO, DS_REPO):
        raise GateDenial("Pull requests are read only from may-app or may-design-system")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise GateDenial("Pull request does not exist or is not readable with the pull-request token") from None
        raise GateDenial("Pull request metadata unavailable") from None
    except (urllib.error.URLError, TimeoutError):
        raise GateDenial("Pull request metadata unavailable") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("compile", "compile-pr", "ds-compile", "renders", "device-tests", "ios",
                                             "android"), required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-number")
    parser.add_argument("--target", choices=("production", "playground"), default="production")
    parser.add_argument("--dry-run")
    parser.add_argument("--sign-only")
    parser.add_argument("--pr-number")
    # renders alone: a may-design-system commit to render with instead of may-app's pin.
    parser.add_argument("--ds-sha")
    # compile-pr, ds-compile or renders (with a pull request) on its environment's runner, before the fetch: read the
    # pull request itself.
    parser.add_argument("--check-pull", action="store_true")
    args = parser.parse_args()
    if args.action in OPTIONAL_PULL_ACTIONS:
        if args.check_pull and args.pr_number is None:
            raise GateDenial("Only a pull request's renders read a pull request")
    elif (args.action in PULL_ACTIONS) != (args.pr_number is not None):
        raise GateDenial("A pull request number is given with compile-pr or ds-compile and only then")
    if args.check_pull and args.action not in PULL_ACTIONS + OPTIONAL_PULL_ACTIONS:
        raise GateDenial("Only compile-pr, ds-compile and renders read a pull request")
    if args.ds_sha is not None and (args.action != "renders" or not re.fullmatch(r"[0-9a-f]{40}", args.ds_sha)):
        raise GateDenial("A design-system commit is given only with renders, as an exact lowercase commit SHA")
    env = validate_run(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
                       os.environ.get("GITHUB_EVENT_NAME"), args.source_sha, args.action,
                       args.build_number, args.target)
    validate_dry_run(args.action, args.dry_run, args.target)
    validate_sign_only(args.action, args.sign_only, args.dry_run, args.target)
    # A pull-request compile builds in its own environment alone, never may-native-compile or a store one.
    build_env = env if args.action in PULL_ACTIONS + OPTIONAL_PULL_ACTIONS else ENVIRONMENTS["compile"]
    if args.action not in OWN_ENVIRONMENT_ACTIONS and artifact_recipient(env) is None:
        raise GateDenial("Artifact encryption public key for this target is still a placeholder; "
                         "the setup lane must commit it first")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise GateDenial("GitHub metadata token unavailable")
    author, pull_read = None, True
    if args.action == "renders" and args.pr_number is not None:
        number = pull_number(args.pr_number)
        if args.check_pull:
            # Without the token (not yet created for may-ios-renders) the pull request is not read: prepare.py's fetch
            # of refs/pull/<number>/head still requires that head to be exactly the source SHA.
            pull_token = os.environ.get("MAY_APP_PR_READ_TOKEN")
            if pull_token:
                validate_pull_request(get_app_pull(number, pull_token), number, args.source_sha)
            else:
                pull_read = False
    if args.action == "compile-pr":
        number = pull_number(args.pr_number)
        if args.check_pull:
            pull_token = os.environ.get("MAY_APP_PR_READ_TOKEN")
            if not pull_token:
                raise GateDenial("Pull-request read token unavailable")
            pull = get_app_pull(number, pull_token)
            validate_pull_request(pull, number, args.source_sha)
            author = login_or_unknown((pull.get("user") or {}).get("login"))
    elif args.action == "ds-compile":
        number = pull_number(args.pr_number)
        if args.check_pull:
            pull_token = os.environ.get("MAY_DS_PR_READ_TOKEN")
            if not pull_token:
                raise GateDenial("Design-system pull-request read token unavailable")
            validate_ds_pull_request(get_app_pull(number, pull_token, DS_REPO), number, args.source_sha)
    for name in sorted({env, build_env}):
        base = f"environments/{name}"
        data, policies = get_json(base, token), get_json(base + "/deployment-branch-policies?per_page=100", token)
        if args.action in NO_REVIEWER_ACTIONS:
            validate_no_reviewer_environment(data, policies)
        else:
            validate_environment(data, policies, os.environ.get("GITHUB_ACTOR_ID"), os.environ.get("GITHUB_TRIGGERING_ACTOR"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"environment={env}\nbuild_environment={build_env}\n")
    if args.action == "compile-pr":
        # Who wrote and who dispatched it, so a run is attributable from the public log (logins only).
        dispatcher = login_or_unknown(os.environ.get("GITHUB_TRIGGERING_ACTOR"))
        checked = f"; pull request by {author}" if author else ""
        print(f"May compile-pr {'pull request check' if args.check_pull else 'admission'} passed for {args.source_sha} "
              f"(may-app #{int(args.pr_number)}{checked}; dispatched by {dispatcher})")
        return
    if args.action == "renders":
        # Only the dispatch's own inputs (commits and number) and the dispatcher, never anything read from may-app.
        dispatcher = login_or_unknown(os.environ.get("GITHUB_TRIGGERING_ACTOR"))
        subject = f"may-app #{int(args.pr_number)}" if args.pr_number is not None else "a may-app main commit"
        design = "; design system " + args.ds_sha if args.ds_sha else ""
        if not pull_read:
            print(f"May renders pull request check skipped: no MAY_APP_PR_READ_TOKEN in may-ios-renders; the fetch "
                  f"still requires the head of may-app #{int(args.pr_number)} to be {args.source_sha}")
            return
        print(f"May renders {'pull request check' if args.check_pull else 'admission'} passed for {args.source_sha} "
              f"({subject}{design}; dispatched by {dispatcher})")
        return
    if args.action == "device-tests":
        dispatcher = login_or_unknown(os.environ.get("GITHUB_TRIGGERING_ACTOR"))
        subject = "may-app main's head" if args.source_sha == "main" else args.source_sha
        print(f"May device-tests admission passed for {subject} (a may-app main commit; dispatched by {dispatcher})")
        return
    if args.action == "ds-compile":
        # may-design-system is private: only the dispatch's own inputs (commit and number) and the dispatcher are
        # printed, never the pull request's author, title or anything else read from it.
        dispatcher = login_or_unknown(os.environ.get("GITHUB_TRIGGERING_ACTOR"))
        print(f"May ds-compile {'pull request check' if args.check_pull else 'admission'} passed for {args.source_sha} "
              f"(may-design-system #{int(args.pr_number)}; dispatched by {dispatcher})")
        return
    print(f"May {args.action} admission passed for {args.source_sha}")


if __name__ == "__main__":
    try:
        main()
    except GateDenial as denial:
        raise SystemExit("May control admission denied: " + denial.args[0]) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit("May control admission denied; inspect protected environment and inputs") from None
