"""TestFlight "What to test" notes for an iOS store build (coordinator, 30 Oct): what changed in may-app since the
previous build, as testers read it in TestFlight.

may-app squash-merges its pull requests, so each first-parent commit subject on main is a pull request's title
("Brief: needs_you_count, everything waiting on the person (173) (#1503)"). The notes are those titles since the
previous iOS build of the same app, grouped by their area (the text before the first colon) and written as
"Try:" lines, at most NOTES_LIMIT characters (App Store Connect's whatsNew limit).

may-app is private and this repository public: the notes are private. This module only composes them; nothing here
prints a title, and the steps that use it print counts alone.
"""

import re

NOTES_LIMIT = 4000
# With no previous build to compare against (the first build, or one whose commit is not an ancestor of this one),
# the notes are the newest FALLBACK_COUNT titles.
FALLBACK_COUNT = 20
NOTHING_NEW = "No new changes since the previous build."
PULL_NUMBER = re.compile(r"\s*\(#\d+\)\s*$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# store-release-draft.yml's run-name, as static_check.STORE_RUN_NAME renders it.
RUN_NAME = re.compile(r"(May Playground|May) · (iOS|Android) · build ([1-9][0-9]{0,8}) at ([0-9a-f]{40})"
                      r"( \(dry run\))?( \(sign only\))?")
APPS = {"playground": "May Playground", "production": "May"}


def previous_build(runs, target, build_number, source_sha):
    """The commit of the newest successful iOS upload of target before build_number, from store-release-draft.yml's
    runs ({display_title, status, conclusion}), or None. Dry runs, sign-only runs, other targets and this commit are
    not previous builds."""
    best = None
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        match = RUN_NAME.fullmatch(str(run.get("display_title") or ""))
        if not match or match.group(1) != APPS[target] or match.group(2) != "iOS" or match.group(5) or match.group(6):
            continue
        number, sha = int(match.group(3)), match.group(4)
        if number < build_number and sha != source_sha and (best is None or number > best[0]):
            best = (number, sha)
    return best[1] if best else None


def area_of(title):
    """(area, rest): the text before the first colon, when it is short enough to be an area, and the rest."""
    head, colon, rest = title.partition(":")
    if colon and 0 < len(head.strip()) <= 40 and rest.strip():
        return head.strip(), rest.strip()
    return "Other", title.strip()


def compose(titles):
    """The notes for these pull-request titles (newest first): grouped by area in the order each area first
    appears, one "Try:" line each, cut whole lines short of NOTES_LIMIT with a count of what was left out."""
    cleaned = []
    for title in titles:
        title = PULL_NUMBER.sub("", CONTROL.sub(" ", str(title))).strip()
        if title:
            cleaned.append(title)
    if not cleaned:
        return NOTHING_NEW
    groups = {}
    for title in cleaned:
        area, rest = area_of(title)
        groups.setdefault(area, []).append(rest)
    lines = []
    for area, items in groups.items():
        lines += [f"Try: {area}: {item}" for item in items]
    text = "\n".join(lines)
    if len(text) <= NOTES_LIMIT:
        return text
    # Room for the closing "…and N more" line, then whole lines only.
    kept, size = [], len(f"\n…and {len(lines)} more")
    for line in lines:
        if size + len(line) + 1 > NOTES_LIMIT:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept + [f"…and {len(lines) - len(kept)} more"])
