#!/usr/bin/env python3
"""Bootstrap Quant Research OS: dirs, windows, register validated weeks."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

OS_ROOT = Path("/root/quant_research_os")

VALIDATED = [
    {
        "window_id": "W-2026-06-12-2026-06-18",
        "start": "2026-06-12T00:00:00",
        "end": "2026-06-18T23:59:59",
        "dir": "2026-06-12_2026-06-18",
        "source": "/root/research_inventory_round5_weekly_20260612_0618",
        "note": "Round 5 weekly merge",
    },
    {
        "window_id": "W-2026-06-19-2026-06-25",
        "start": "2026-06-19T00:00:00",
        "end": "2026-06-25T23:59:59",
        "dir": "2026-06-19_2026-06-25",
        "source": "/root/research_inventory_round6_weekly_20260619_0625_blind",
        "note": "Round 6 blind test",
    },
]

EARLIEST = datetime(2025, 10, 27)
LATEST = datetime(2026, 7, 11)


def monday_of(d: datetime) -> datetime:
    return d - timedelta(days=d.weekday())


def week_windows() -> list[dict]:
    cur = monday_of(EARLIEST)
    end_limit = monday_of(LATEST)
    windows = []
    validated_ids = {v["window_id"] for v in VALIDATED}
    while cur <= end_limit:
        wend = cur + timedelta(days=6, hours=23, minutes=59, seconds=59)
        wid = f"W-{cur.strftime('%Y-%m-%d')}-{wend.strftime('%Y-%m-%d')}"
        dname = f"{cur.strftime('%Y-%m-%d')}_{wend.strftime('%Y-%m-%d')}"
        if wid in validated_ids:
            status = "EXISTING_VALIDATED"
        else:
            status = "PENDING"
        windows.append({
            "window_id": wid,
            "start": cur.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": wend.strftime("%Y-%m-%dT%H:%M:%S"),
            "dir_name": dname,
            "status": status,
        })
        cur += timedelta(days=7)
    return windows


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    # Directory structure
    for sub in ["state", "weekly", "monthly", "global", "pipeline"]:
        (OS_ROOT / sub).mkdir(parents=True, exist_ok=True)

    windows = week_windows()

    # Register validated weeks (copy, don't symlink — preserve immutability)
    checkpoints = []
    for v in VALIDATED:
        dst = OS_ROOT / "weekly" / v["dir"]
        src = Path(v["source"])
        if src.exists() and not (dst / "weekly_raw_index.jsonl").exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("_build*.py", "__pycache__"))
        checkpoints.append({
            "window_id": v["window_id"],
            "stage": "QA_PASSED",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "output_dir": str(dst),
            "source": "EXISTING_VALIDATED",
            "note": v["note"],
        })
        for w in windows:
            if w["window_id"] == v["window_id"]:
                w["status"] = "QA_PASSED"
                w["output_dir"] = str(dst)

    write_jsonl(OS_ROOT / "state/windows.jsonl", windows)
    write_jsonl(OS_ROOT / "state/checkpoints.jsonl", checkpoints)
    write_jsonl(OS_ROOT / "state/failures.jsonl", [])

    pipeline_state = {
        "initialized_at": datetime.utcnow().isoformat() + "Z",
        "earliest_research_date": EARLIEST.strftime("%Y-%m-%d"),
        "latest_research_date": LATEST.strftime("%Y-%m-%d"),
        "total_windows": len(windows),
        "qa_passed": sum(1 for w in windows if w["status"] == "QA_PASSED"),
        "pending": sum(1 for w in windows if w["status"] == "PENDING"),
        "schema_version": "round3-6",
    }
    (OS_ROOT / "state/pipeline_state.json").write_text(json.dumps(pipeline_state, indent=2))

    # Global placeholders
    global_dir = OS_ROOT / "global"
    for name in [
        "user_theses", "research_principles", "hypotheses", "experiments",
        "conclusions", "failures", "methods", "decisions", "open_questions", "knowledge_graph",
    ]:
        p = global_dir / f"{name}.jsonl"
        if not p.exists():
            p.write_text("")
    (global_dir / "current_state.md").write_text(
        "# Current State\n\n_Pending global consolidation after all weekly windows complete._\n"
    )
    (global_dir / "global_summary.md").write_text(
        "# Global Summary\n\n_Pending._\n"
    )

    print(json.dumps(pipeline_state, indent=2))
    print(f"windows written: {len(windows)}")


if __name__ == "__main__":
    main()
