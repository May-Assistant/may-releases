"""The May flow board, written by the publish job into may-renders as one self-contained folder, device-tests/board/
(index.html and media/, every path relative and below it), so the coordinator can upload it as-is.
board-notes.json may also name each test's row (scenario_titles).

Verdicts come from the device reports publish.py has already checked, never from hand-curated data. may-app supplies
the flows (docs/evidence/flows.json) and the lanes' curated text (docs/evidence/board-notes.json: a banner, per flow
what it means, what comes before the next run, an open issue and the pull requests changed since; per result a
headline, a note and links), sealed into the encrypted report by the device jobs. Every value is checked against a
fixed shape here and escaped by the page, which builds itself from the JSON it carries (board_template.html).

A flow with several tests gets one nested row per test (<flow>-<scenario>, titled from this repository's scenario
list), each with its own state; the flow's own page lists them.

The run history (device-tests/history.json) keeps HISTORY runs, newest first, each with its verdicts and its end
screenshots copied to device-tests/board/media/<folder>/, so they outlive the run folder (publish.KEEP). Previews
(270px, silent) are release assets, never in git: the page expects each at media/<folder>/<device>-<scenario>-preview.mp4
and media/previews.json says which release asset holds it; a preview that is absent falls back to the end screen. A run older than
the KEEP newest has lost its release, so the board shows it as Archived: verdict and end screen, no recording.
"""
import datetime
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "board_template.html"
HISTORY = 60
BOARD, MEDIA = "board", "media"
DEVICES = ("android", "ios")
FOLDER = re.compile(r"[0-9]{8}-[0-9a-f]{12}")
RUN = re.compile(r"[1-9][0-9]{0,19}")
FLOW_ID = re.compile(r"[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2})?")
SCENARIO = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
COMPONENT = re.compile(r"[A-Z][A-Za-z0-9]{0,63}")
SHOT = re.compile(r"[a-z0-9][a-z0-9-]{0,47}-end\.png")
VIDEO = re.compile(r"[a-z0-9][a-z0-9-]{0,47}(?:-part[0-9]{1,2})?\.mp4")
# Links only to May-Assistant's own pull requests and issues.
LINK = re.compile(r"https://github\.com/May-Assistant/[a-z0-9-]{1,40}/(?:pull|issues)/[1-9][0-9]{0,6}")
VERDICTS = {"pass": "pass", "ui-pass": "pass", "fail": "fail", "skipped": "skipped"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
MAX_TEXT, MAX_LINE, MAX_ITEMS, MAX_INPUT = 600, 160, 12, 256 * 1024


class BoardError(Exception):
    pass


def text(value, limit=MAX_TEXT):
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


def read_json(path):
    """A may-app input as JSON, or None when it is absent, a link, too large or not JSON."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_INPUT:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def checked_flows(data):
    """may-app's flows.json: its flows, each a fixed shape, or BoardError."""
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, list) or not flows:
        raise BoardError("may-app's flows.json has no flows")
    checked, ids = [], set()
    for flow in flows:
        if not (isinstance(flow, dict) and isinstance(flow.get("id"), str) and FLOW_ID.fullmatch(flow["id"]) and
                flow["id"] not in ids and text(flow.get("title"), MAX_LINE) and
                isinstance(flow.get("scenarios"), list) and len(flow["scenarios"]) <= MAX_ITEMS and
                all(isinstance(s, str) and SCENARIO.fullmatch(s) for s in flow["scenarios"]) and
                isinstance(flow.get("components"), list) and len(flow["components"]) <= 40 and
                all(isinstance(c, str) and COMPONENT.fullmatch(c) for c in flow["components"])):
            raise BoardError("may-app's flows.json has a malformed flow")
        ids.add(flow["id"])
        checked.append({key: flow[key] for key in ("id", "title", "scenarios", "components")})
    return checked


def links(value):
    return isinstance(value, list) and len(value) <= MAX_ITEMS and all(
        isinstance(link, dict) and set(link) == {"text", "url"} and text(link["text"], 40) and
        isinstance(link["url"], str) and LINK.fullmatch(link["url"]) for link in value)


def checked_notes(data):
    """may-app's board-notes.json: the lanes' curated text, each field a fixed shape, or BoardError."""
    if not isinstance(data, dict) or not set(data) <= {"banner", "flows", "results", "scenario_titles"}:
        raise BoardError("may-app's board-notes.json is malformed")
    banner = data.get("banner")
    if not (banner is None or isinstance(banner, dict) and set(banner) == {"title", "body"} and
            text(banner["title"], MAX_LINE) and text(banner["body"])):
        raise BoardError("may-app's board-notes.json has a malformed banner")
    flows = data.get("flows", {})
    if not (isinstance(flows, dict) and all(
            isinstance(key, str) and FLOW_ID.fullmatch(key) and isinstance(flow, dict) and
            set(flow) <= {"means", "next", "issue", "changed"} and
            ("means" not in flow or text(flow["means"])) and
            ("next" not in flow or isinstance(flow["next"], list) and len(flow["next"]) <= MAX_ITEMS and
             all(text(item, MAX_TEXT) for item in flow["next"])) and
            ("issue" not in flow or type(flow["issue"]) is int and 0 < flow["issue"] < 10 ** 7) and
            ("changed" not in flow or isinstance(flow["changed"], list) and len(flow["changed"]) <= MAX_ITEMS and
             all(type(n) is int and 0 < n < 10 ** 7 for n in flow["changed"]))
            for key, flow in flows.items())):
        raise BoardError("may-app's board-notes.json has a malformed flow note")
    results = data.get("results", {})
    if not (isinstance(results, dict) and all(
            isinstance(key, str) and key.count("/") == 1 and key.split("/")[0] in DEVICES and
            SCENARIO.fullmatch(key.split("/")[1]) and isinstance(note, dict) and
            set(note) <= {"headline", "note", "links"} and
            ("headline" not in note or text(note["headline"], MAX_LINE)) and
            ("note" not in note or text(note["note"])) and ("links" not in note or links(note["links"]))
            for key, note in results.items())):
        raise BoardError("may-app's board-notes.json has a malformed result note")
    titles = data.get("scenario_titles", {})
    if not (isinstance(titles, dict) and all(isinstance(key, str) and SCENARIO.fullmatch(key) and text(title, MAX_LINE)
                                             for key, title in titles.items())):
        raise BoardError("may-app's board-notes.json has a malformed scenario title")
    return {"banner": banner, "flows": flows, "results": results, "scenario_titles": titles}


def entry(folder, run, reports, now, previews=frozenset()):
    """This run's history entry from the checked device reports ({device: rows}); previews holds the
    "<device>/<scenario>" whose preview was made."""
    results = {}
    for device, rows in reports.items():
        for row in rows:
            if row["verdict"] not in VERDICTS:
                continue  # the smoke scenario is not a flow's test
            shots = [s for s in row["screenshots"] if SHOT.fullmatch(s)]
            results[f"{device}/{row['id']}"] = {
                "verdict": VERDICTS[row["verdict"]], "reason": row["reason"] or None,
                "shot": shots[0] if shots else None,
                "videos": [v for v in row["videos"] if VIDEO.fullmatch(v)],
                "preview": f"{device}/{row['id']}" in previews}
    return {"folder": folder, "run": run, "when": f"{now.day} {MONTHS[now.month - 1]}, {now:%H:%M} UTC",
            "results": results}


def history_entries(existing):
    """history.json's entries that still have the fixed shape; anything else is dropped."""
    kept = []
    for item in existing if isinstance(existing, list) else []:
        if not (isinstance(item, dict) and isinstance(item.get("folder"), str) and FOLDER.fullmatch(item["folder"]) and
                (item.get("run") is None or isinstance(item["run"], str) and RUN.fullmatch(item["run"])) and
                text(item.get("when"), 40) and isinstance(item.get("results"), dict)):
            continue
        results = {}
        for key, result in item["results"].items():
            if isinstance(key, str) and key.count("/") == 1 and key.split("/")[0] in DEVICES and \
                    SCENARIO.fullmatch(key.split("/")[1]) and isinstance(result, dict) and \
                    result.get("verdict") in VERDICTS.values() and \
                    (result.get("reason") is None or text(result["reason"], MAX_LINE)) and \
                    (result.get("shot") is None or isinstance(result["shot"], str) and SHOT.fullmatch(result["shot"])) and \
                    isinstance(result.get("videos"), list) and \
                    all(isinstance(v, str) and VIDEO.fullmatch(v) for v in result["videos"]):
                results[key] = dict({k: result.get(k) for k in ("verdict", "reason", "shot", "videos")},
                                    preview=result.get("preview") is True)
        kept.append({"folder": item["folder"], "run": item.get("run"), "when": item["when"], "results": results})
    return kept


def history_after(existing, new):
    """history.json after adding new first: (the entries kept, the folders whose screenshots go)."""
    entries = [item for item in history_entries(existing) if item["folder"] != new["folder"]]
    entries.insert(0, new)
    return entries[:HISTORY], [item["folder"] for item in entries[HISTORY:]]


def flow_state(verdicts):
    """pass: every test passed on both apps; partial: some passed; fail: something failed and none passed."""
    if not verdicts or all(v == "none" for v in verdicts):
        return "none"
    if all(v == "skipped" for v in verdicts):
        return "skipped"
    if all(v == "pass" for v in verdicts):
        return "pass"
    if "pass" in verdicts:
        return "partial"
    return "fail" if "fail" in verdicts else "none"


def platform_state(verdicts):
    verdicts = [v for v in verdicts if v != "none"]
    return "none" if not verdicts else "fail" if "fail" in verdicts else \
        "skipped" if all(v == "skipped" for v in verdicts) else "pass"


def run_date(folder):
    return f"{int(folder[6:8])} {MONTHS[int(folder[4:6]) - 1]}"


def payload(flows, notes, entries, asset_url, keep, titles):
    """The page's data: flows with their state in the newest run, every kept run oldest first, and each run's results
    (the curated text on the newest run's alone)."""
    newest = entries[0]
    runs, results = [], {}
    for index, item in reversed(list(enumerate(entries))):
        archived = index >= keep
        runs.append({"folder": item["folder"], "run": item["run"], "date": run_date(item["folder"]),
                     "when": item["when"], "archived": archived})
        shown = {}
        for key, result in item["results"].items():
            device, scenario = key.split("/")
            row = {"verdict": result["verdict"], "run": item["run"], "run_date": run_date(item["folder"])}
            if result["shot"]:
                row["thumb"] = f"{MEDIA}/{item['folder']}/{device}/{result['shot']}"
            if result["videos"] and not archived:
                row["video"] = asset_url(item["folder"], device, result["videos"][0])
            if result["preview"] and not archived:
                row["preview"] = preview_path(item["folder"], key)
            if result["reason"]:
                row["note"] = result["reason"]
            if item is newest:
                row.update(notes["results"].get(key, {}))
            shown[key] = row
        results[item["folder"]] = shown
    # A nested row's title: the lanes' own (board-notes.json), else this repository's scenario list, else its id.
    titles = {**titles, **notes["scenario_titles"]}
    out = []
    for flow in flows:
        out.append(flow_row(flow, notes["flows"].get(flow["id"], {}), newest, entries))
        if len(flow["scenarios"]) > 1:
            for scenario in flow["scenarios"]:
                row = flow_row({"id": f"{flow['id']}-{scenario}", "title": titles.get(scenario, scenario),
                                "scenarios": [scenario], "components": flow["components"]}, {}, newest, entries)
                out.append(dict(row, parent=flow["id"], scenario_row=True))
    return {"flows": out, "runs": runs, "current": newest["folder"], "results": results}


def flow_row(flow, extra, newest, entries):
    """A sidebar row: the flow, its state in the newest run and when it last passed, and its curated text."""
    verdicts = {device: [newest["results"].get(f"{device}/{s}", {}).get("verdict", "none") for s in flow["scenarios"]]
                for device in DEVICES}
    passed = next((item for item in entries if flow["scenarios"] and all(
        item["results"].get(f"{device}/{s}", {}).get("verdict") == "pass"
        for s in flow["scenarios"] for device in DEVICES)), None)
    return {**flow, "parent": flow["id"].split(".")[0] if "." in flow["id"] else None,
            "state": flow_state(verdicts["android"] + verdicts["ios"]),
            "android": platform_state(verdicts["android"]), "ios": platform_state(verdicts["ios"]),
            "means": extra.get("means"), "next": extra.get("next", []), "issue": extra.get("issue"),
            "changed": extra.get("changed", []),
            "last_pass": f"{run_date(passed['folder'])} {passed['folder'][:4]}" +
            (f", run {passed['run']}" if passed["run"] else "") if passed else None}


def preview_path(folder, key):
    """Where the page expects a preview: fixed, relative, below the board; the file itself is a release asset."""
    return f"{MEDIA}/{folder}/{preview_asset(key)}"


def preview_asset(key):
    device, scenario = key.split("/")
    return f"{device}-{scenario}-preview.mp4"


def previews_list(entries, keep):
    """media/previews.json: for each preview the page expects, the release (tag) and asset that hold it, so the
    coordinator can download them into the folder before uploading it. The newest keep runs alone have releases."""
    return {preview_path(item["folder"], key): {"tag": f"device-tests-{item['folder']}", "asset": preview_asset(key)}
            for item in entries[:keep] for key, result in sorted(item["results"].items()) if result["preview"]}


def page(data, banner, when):
    """board.html: the template with its data as JSON, every "<" escaped so no value can close the script."""
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e") \
        .replace("&", "\\u0026")
    flash = json.dumps(banner).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = TEMPLATE.read_text()
    return template.replace("{{RUN_WHEN}}", when.replace("&", "&amp;").replace("<", "&lt;")) \
        .replace("{{BANNER}}", flash).replace("{{DATA}}", blob)


def update(base, folder, run, reports, flows_data, notes_data, asset_url, keep, titles, now=None, previews=frozenset()):
    """Record this run in base/history.json, copy its end screenshots from base/folder to base/BOARD/MEDIA/folder,
    drop those beyond HISTORY and write base/BOARD/index.html. Returns "written", "no notes" (written without the
    malformed board-notes.json) or "no flows" (not written: flows.json missing or malformed)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    index = base / "history.json"
    try:
        existing = json.loads(index.read_text()) if index.is_file() and not index.is_symlink() else []
    except (OSError, ValueError):
        existing = []
    new = entry(folder, run, reports, now, previews)
    entries, removed = history_after(existing, new)
    for device_key, result in new["results"].items():
        device = device_key.split("/")[0]
        source = base / folder / device / result["shot"] if result["shot"] else None
        if source and source.is_file() and not source.is_symlink():
            target = base / BOARD / MEDIA / folder / device
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target / result["shot"])
        else:
            result["shot"] = None
    for old in removed:
        shutil.rmtree(base / BOARD / MEDIA / old, ignore_errors=True)
    index.write_text(json.dumps(entries, indent=2) + "\n")
    try:
        flows = checked_flows(flows_data)
    except BoardError:
        return "no flows"
    try:
        notes, word = checked_notes(notes_data if notes_data is not None else {}), "written"
    except BoardError:
        notes, word = checked_notes({}), "no notes"
    (base / BOARD / MEDIA).mkdir(parents=True, exist_ok=True)
    (base / BOARD / MEDIA / "previews.json").write_text(json.dumps(previews_list(entries, keep), indent=2) + "\n")
    (base / BOARD / "index.html").write_text(page(payload(flows, notes, entries, asset_url, keep, titles),
                                                  notes["banner"], new["when"]))
    return word
