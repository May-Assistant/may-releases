#!/usr/bin/env python3
"""Builds the May flow board (board.html): list-detail, data embedded as JSON for the page's script."""
import html, json
from pathlib import Path

HERE = Path(__file__).parent
data = json.loads((HERE / "data.json").read_text())
thumbs = json.loads(Path(data["thumbs"]).read_text())
flows = json.loads(Path(data["flows"]).read_text())["flows"]
run = data["run"]
E = html.escape
MEANS = data.get("means", {})

def state(flow):
    """pass: every test passed on both apps; partial: some passed; fail: something failed and nothing passed."""
    vs = [run["results"].get(f"{p}/{s}", {}).get("verdict", "none") for s in flow["scenarios"] for p in ("android", "ios")]
    if not vs or all(v == "none" for v in vs):
        return "none"
    if all(v == "skipped" for v in vs):
        return "skipped"
    if all(v == "pass" for v in vs):
        return "pass"
    if "pass" in vs:
        return "partial"
    return "fail" if "fail" in vs else "none"

def platform(flow, p):
    vs = [run["results"].get(f"{p}/{s}", {}).get("verdict") for s in flow["scenarios"]]
    vs = [v for v in vs if v]
    return "none" if not vs else "fail" if "fail" in vs else "skipped" if all(v == "skipped" for v in vs) else "pass"

out_flows = []
for f in flows:
    a, i = platform(f, "android"), platform(f, "ios")
    fid = f["id"]
    out_flows.append({
        "id": fid, "title": f["title"], "parent": fid.split(".")[0] if "." in fid else None,
        "scenarios": f["scenarios"], "components": f["components"], "means": MEANS.get(fid),
        "android": a, "ios": i, "state": state(f),
        "last_pass": data.get("last_pass", {}).get(fid), "changed": data.get("changed", {}).get(fid, []),
        "issue": data.get("issues", {}).get(fid),
        **data.get("flow_extra", {}).get(fid, {}),
    })

RUNS = {x["folder"]: x["run"] for x in json.loads((HERE / "runs.json").read_text())}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
results = {}
for k, r in run["results"].items():
    r = dict(r)
    folder = r.get("video", "").split("/device-tests-")[-1].split("/")[0] if "/device-tests-" in r.get("video", "") else None
    if folder in RUNS:
        r["run"] = RUNS[folder]
        r["run_date"] = f"{int(folder[6:8])} {MONTHS[int(folder[4:6]) - 1]}"
    if k in thumbs:
        r["thumb"] = thumbs[k]
    results[k] = r

payload = json.dumps({"flows": out_flows, "results": results}).replace("</", "<\\/")
banner = data.get("banner")
banner_html = f'<div class="flash"><strong>{E(banner["title"])}</strong><p>{banner["body"]}</p></div>' if banner else ""
page = (HERE / "template.html").read_text()
page = (page.replace("{{RUN_ID}}", E(run["id"])).replace("{{RUN_URL}}", E(run["url"]))
        .replace("{{RUN_WHEN}}", E(run["when"])).replace("{{RUN_SHA}}", E(run["sha"]))
        .replace("{{RELEASE_URL}}", E(run["release"])).replace("{{REPORT_URL}}", E(run["report"]))
        .replace("{{BANNER}}", banner_html).replace("{{DATA}}", payload))
(HERE / "board.html").write_text(page)
print("board.html", len(page))
