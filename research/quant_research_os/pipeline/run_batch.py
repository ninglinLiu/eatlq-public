#!/usr/bin/env python3
"""Process pending weekly windows with checkpoint resume."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

OS_ROOT = Path("/root/quant_research_os")
COMPILER = OS_ROOT / "pipeline/weekly_compiler.py"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def update_window_status(windows_path: Path, window_id: str, status: str, **extra) -> None:
    rows = load_jsonl(windows_path)
    for w in rows:
        if w["window_id"] == window_id:
            w["status"] = status
            w.update(extra)
    with open(windows_path, "w") as f:
        for w in rows:
            f.write(json.dumps(w, ensure_ascii=False) + "\n")


def main():
    max_weeks = int(sys.argv[1]) if len(sys.argv) > 1 else 35
    windows_path = OS_ROOT / "state/windows.jsonl"
    windows = load_jsonl(windows_path)
    pending = [w for w in windows if w["status"] in ("PENDING", "FAILED")]
    pending.sort(key=lambda x: x["start"])

    processed = 0
    results = []

    for w in pending[:max_weeks]:
        if w.get("status") in ("QA_PASSED", "EXISTING_VALIDATED"):
            continue
        wid = w["window_id"]
        out_dir = OS_ROOT / "weekly" / w["dir_name"]
        start = w["start"][:10]
        end = w["end"][:10]

        update_window_status(windows_path, wid, "RUNNING")

        try:
            proc = subprocess.run(
                [sys.executable, str(COMPILER),
                 "--start", start, "--end", end,
                 "--out", str(out_dir), "--window-id", wid],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr or proc.stdout)

            result = json.loads(proc.stdout)

            qa = result.get("qa_status", "NEEDS_REVIEW")
            final_status = qa if qa in ("QA_PASSED", "SKIPPED_EMPTY") else "NEEDS_REVIEW"

            append_jsonl(OS_ROOT / "state/checkpoints.jsonl", {
                "window_id": wid,
                "stage": "CANONICAL_DONE" if final_status != "SKIPPED_EMPTY" else "SKIPPED_EMPTY",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "output_dir": str(out_dir),
                "object_counts": result.get("object_counts", {}),
                "raw_events": result.get("raw_events", 0),
                "selected": result.get("selected", 0),
                "qa_status": qa,
            })
            update_window_status(windows_path, wid, final_status, output_dir=str(out_dir))
            results.append({"window_id": wid, "status": final_status, **result})
            processed += 1

        except Exception as e:
            append_jsonl(OS_ROOT / "state/failures.jsonl", {
                "window_id": wid,
                "stage": "WEEKLY_COMPILE",
                "error": str(e)[:500],
                "last_checkpoint": "RUNNING",
                "retry_recommendation": f"python3 {COMPILER} --start {start} --end {end} --out {out_dir} --window-id {wid}",
            })
            update_window_status(windows_path, wid, "FAILED")
            results.append({"window_id": wid, "status": "FAILED", "error": str(e)[:200]})

    # Update pipeline state
    windows = load_jsonl(windows_path)
    state = json.loads((OS_ROOT / "state/pipeline_state.json").read_text())
    state["last_batch"] = datetime.utcnow().isoformat() + "Z"
    state["qa_passed"] = sum(1 for w in windows if w["status"] == "QA_PASSED")
    state["skipped_empty"] = sum(1 for w in windows if w["status"] == "SKIPPED_EMPTY")
    state["needs_review"] = sum(1 for w in windows if w["status"] == "NEEDS_REVIEW")
    state["failed"] = sum(1 for w in windows if w["status"] == "FAILED")
    state["pending"] = sum(1 for w in windows if w["status"] == "PENDING")
    (OS_ROOT / "state/pipeline_state.json").write_text(json.dumps(state, indent=2))

    print(json.dumps({"processed": processed, "results": results}, indent=2))


if __name__ == "__main__":
    main()
