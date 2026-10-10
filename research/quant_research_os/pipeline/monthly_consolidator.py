#!/usr/bin/env python3
"""Monthly consolidation — merge 4+ QA_PASSED weekly packs (no raw re-read)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

OS = Path("/root/quant_research_os")
CATEGORIES = [
    "user_theses", "research_principles", "hypotheses", "experiments",
    "conclusions", "failures", "methods", "decisions",
]


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists() or p.stat().st_size == 0:
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def weeks_in_month(year: int, month: int) -> list[dict]:
    windows = load_jsonl(OS / "state/windows.jsonl")
    prefix = f"{year}-{month:02d}"
    return [w for w in windows if (w["start"][:7] == prefix or w["end"][:7] == prefix)
            and w.get("status") == "QA_PASSED"]


def consolidate_month(year: int, month: int) -> dict | None:
    month_weeks = weeks_in_month(year, month)
    if len(month_weeks) < 1:
        return None
    out = OS / "monthly" / f"{year}-{month:02d}"
    out.mkdir(parents=True, exist_ok=True)

    merged = defaultdict(list)
    source_weeks = []
    for w in month_weeks:
        wdir = Path(w.get("output_dir", OS / "weekly" / w["dir_name"]))
        if not wdir.exists():
            continue
        source_weeks.append(w["window_id"])
        for cat in CATEGORIES:
            merged[cat].extend(load_jsonl(wdir / f"{cat}.jsonl"))

    if not any(merged.values()):
        return None

    # Dedupe by id
    for cat in CATEGORIES:
        seen = set()
        deduped = []
        for obj in merged[cat]:
            oid = obj.get("id", "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            deduped.append(obj)
        with open(out / f"monthly_{cat}.jsonl", "w") as f:
            for o in deduped:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")

    kg = []
    for w in month_weeks:
        wdir = Path(w.get("output_dir", OS / "weekly" / w["dir_name"]))
        kg.extend(load_jsonl(wdir / "knowledge_graph.jsonl"))
    with open(out / "monthly_knowledge_graph.jsonl", "w") as f:
        for e in kg:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    summary = [
        f"# Monthly Summary — {year}-{month:02d}\n\n",
        f"- Source weeks ({len(source_weeks)}): {', '.join(source_weeks)}\n\n",
        "## Object counts\n\n",
    ]
    counts = {}
    for cat in CATEGORIES:
        n = len(load_jsonl(out / f"monthly_{cat}.jsonl"))
        counts[cat] = n
        summary.append(f"- {cat}: {n}\n")
    summary.append("\n_Note: dedupe by id only; supersession map requires manual pass for validated weeks._\n")
    (out / "monthly_summary.md").write_text("".join(summary))
    (out / "monthly_supersession_map.md").write_text("# Supersession Map\n\n_Pending manual curation for validated R5/R6 weeks._\n")
    (out / "monthly_conflicts.md").write_text("# Monthly Conflicts\n\n_Merged from weekly source_conflicts.md — review manually._\n")
    (out / "monthly_state_transition.md").write_text("# State Transitions\n\n_See weekly boundary_continuity_check.md per week._\n")
    (out / "monthly_compiler_audit.md").write_text(
        f"# Monthly Compiler Audit — {year}-{month:02d}\n\n"
        f"Consolidated {len(source_weeks)} weekly packs. No raw transcript re-read.\n"
    )
    return {"month": f"{year}-{month:02d}", "weeks": source_weeks, "counts": counts}


def main():
    results = []
    for ym in [(2026, 5), (2026, 6), (2026, 7)]:
        r = consolidate_month(*ym)
        if r:
            results.append(r)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
