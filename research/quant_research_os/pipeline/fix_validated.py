#!/usr/bin/env python3
"""Fix validated custom windows (R5/R6) + re-register QA_PASSED."""
import json
import shutil
from datetime import datetime
from pathlib import Path

OS = Path("/root/quant_research_os")
CUSTOM = [
    {
        "window_id": "W-2026-06-12-2026-06-18",
        "start": "2026-06-12T00:00:00",
        "end": "2026-06-18T23:59:59",
        "dir_name": "2026-06-12_2026-06-18",
        "status": "QA_PASSED",
        "source": "EXISTING_VALIDATED",
        "note": "Round 5 — custom window (not Mon-Sun aligned)",
        "import_from": "/root/research_inventory_round5_weekly_20260612_0618",
    },
    {
        "window_id": "W-2026-06-19-2026-06-25",
        "start": "2026-06-19T00:00:00",
        "end": "2026-06-25T23:59:59",
        "dir_name": "2026-06-19_2026-06-25",
        "status": "QA_PASSED",
        "source": "EXISTING_VALIDATED",
        "note": "Round 6 blind — custom window",
        "import_from": "/root/research_inventory_round6_weekly_20260619_0625_blind",
    },
]

def load_jsonl(p):
    return [json.loads(l) for l in open(p) if l.strip()]

def write_jsonl(p, rows):
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

windows = load_jsonl(OS / "state/windows.jsonl")
existing_ids = {w["window_id"] for w in windows}

for c in CUSTOM:
    dst = OS / "weekly" / c["dir_name"]
    src = Path(c["import_from"])
    if src.exists():
        marker = dst / ".validated_import"
        if not marker.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("_build*.py"))
            marker.write_text(c["note"])
    entry = {k: v for k, v in c.items() if k != "import_from"}
    entry["output_dir"] = str(dst)
    if c["window_id"] not in existing_ids:
        windows.append(entry)
    else:
        for w in windows:
            if w["window_id"] == c["window_id"]:
                w.update(entry)

# Annotate overlapping calendar weeks
overlap_notes = {
    "W-2026-06-08-2026-06-14": "Partial overlap; see W-2026-06-12-2026-06-18 (R5 validated)",
    "W-2026-06-15-2026-06-21": "Partial overlap; see R5/R6 validated custom windows",
    "W-2026-06-22-2026-06-28": "Partial overlap; see W-2026-06-19-2026-06-25 (R6) for Jun 22-25",
}
for w in windows:
    if w["window_id"] in overlap_notes:
        w["overlap_note"] = overlap_notes[w["window_id"]]
        w["status_note"] = "AUTO_COMPILED — prefer validated custom window for canonical"

windows.sort(key=lambda x: x["start"])
write_jsonl(OS / "state/windows.jsonl", windows)

# Append checkpoints if missing
cps = load_jsonl(OS / "state/checkpoints.jsonl")
cp_ids = {c["window_id"] for c in cps}
for c in CUSTOM:
    if c["window_id"] not in cp_ids:
        cps.append({
            "window_id": c["window_id"],
            "stage": "QA_PASSED",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "output_dir": str(OS / "weekly" / c["dir_name"]),
            "source": "EXISTING_VALIDATED",
        })
write_jsonl(OS / "state/checkpoints.jsonl", cps)

state = json.loads((OS / "state/pipeline_state.json").read_text())
state["qa_passed"] = sum(1 for w in windows if w["status"] == "QA_PASSED")
state["custom_validated_windows"] = 2
state["total_windows"] = len(windows)
(OS / "state/pipeline_state.json").write_text(json.dumps(state, indent=2))
print(f"windows: {len(windows)} qa_passed: {state['qa_passed']}")
