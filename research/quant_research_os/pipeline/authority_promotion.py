#!/usr/bin/env python3
"""Authority Promotion Pass — authority_level + May-Jul canonical enrichment."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

OS = Path("/root/quant_research_os")
CATEGORIES = [
    "user_theses", "research_principles", "hypotheses", "experiments",
    "conclusions", "failures", "methods", "decisions",
]

VALIDATED_WINDOWS = {
    "W-2026-06-12-2026-06-18": "VALIDATED",
    "W-2026-06-19-2026-06-25": "VALIDATED",
}

# May-Jul auto weeks for enrichment
AUTO_DENSE = [
    "W-2026-05-11-2026-05-17", "W-2026-05-18-2026-05-24", "W-2026-05-25-2026-05-31",
    "W-2026-06-08-2026-06-14", "W-2026-06-15-2026-06-21", "W-2026-06-22-2026-06-28",
    "W-2026-06-29-2026-07-05", "W-2026-07-06-2026-07-12",
]

HY_PAT = re.compile(
    r"假设|hypothesis|HYPOTHESIS|证伪|falsif|右尾|左尾|forced.?seller|KEYSTONE|"
    r"operator|deployer|honeypot|deep.?edge|零 onset|fee.?100",
    re.I,
)
EX_PAT = re.compile(
    r"sprint\d+|backtest|holdout|walk.?forward|audit|实验|EX-|verdict|stress\d+",
    re.I,
)


def load_jsonl(p: Path) -> list:
    if not p.exists() or p.stat().st_size == 0:
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def write_jsonl(p: Path, rows: list) -> None:
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def infer_authority(window_id: str, w: dict) -> str:
    if window_id in VALIDATED_WINDOWS:
        return "VALIDATED"
    st = w.get("status", "")
    if st == "ARTIFACTS_ONLY":
        return "ARTIFACT_ONLY"
    if st in ("EMPTY_CONFIRMED", "NO_TRANSCRIPT_ACTIVITY"):
        return "UNVERIFIED"
    if window_id in AUTO_DENSE or st == "QA_PASSED":
        return "AUTO_CURATED"
    if st == "NEEDS_REVIEW":
        return "UNVERIFIED"
    return "UNVERIFIED"


def enrich_week(wdir: Path, window_id: str) -> dict:
    """Add hypotheses/experiments from selected lineage + upgrade conclusions."""
    selected = load_jsonl(wdir / "selected_lineage.jsonl")
    existing_hy = {o["id"] for o in load_jsonl(wdir / "hypotheses.jsonl")}
    existing_ex = {o["id"] for o in load_jsonl(wdir / "experiments.jsonl")}
    added = {"hypotheses": 0, "experiments": 0, "conclusions_upgraded": 0}

    new_hy, new_ex = [], []
    prefix = window_id.replace("W-", "").replace("-", "")[:12]

    for i, evt in enumerate(selected):
        prev = evt.get("content_preview", "")
        sp = [{"source_file": evt["source_file"], "jsonl_index": evt.get("jsonl_index", -1)}]
        if evt.get("event_type") == "USER_MESSAGE" and HY_PAT.search(prev):
            hid = f"HY-{prefix}-{len(existing_hy)+len(new_hy)+1:02d}"
            if hid not in existing_hy:
                new_hy.append({
                    "id": hid, "object_type": "HYPOTHESIS",
                    "hypothesis_id": hid,
                    "statement": prev[:400],
                    "operational_definition": "unknown",
                    "origin": evt.get("user_event_type", "USER_PROMPT"),
                    "status": "UNKNOWN",
                    "related_hypotheses": [],
                    "valid_from": evt.get("timestamp", "unknown")[:10],
                    "valid_until": "unknown",
                    "regime_dependency": "unknown",
                    "authorship": "USER_EXPLICIT" if evt.get("authorship_confidence") == "LIKELY_USER_ORIGINAL" else "COMPILER_INFERRED",
                    "persistence_scope": "REGIME_DEPENDENT",
                    "reuse_policy": "TOPIC_MATCH_ONLY",
                    "epistemic_role": "HYPOTHESIS",
                    "authority_level": "AUTO_CURATED",
                    "source_pointers": sp,
                    "inferred": True,
                    "inference_reason": "Promoted from user prompt during authority pass",
                })
                added["hypotheses"] += 1
        if EX_PAT.search(prev) and evt.get("event_type") in ("USER_MESSAGE", "ASSISTANT_MESSAGE", "REPORT_CREATED"):
            eid = f"EX-{prefix}-{len(existing_ex)+len(new_ex)+1:02d}"
            if eid not in existing_ex:
                new_ex.append({
                    "id": eid, "object_type": "EXPERIMENT",
                    "experiment_id": eid,
                    "question": prev[:300],
                    "hypothesis_id": "unknown",
                    "design": "unknown",
                    "universe": "unknown",
                    "sample_size": "unknown",
                    "time_range": window_id,
                    "features": "unknown",
                    "entry": "unknown",
                    "exit": "unknown",
                    "cost_model": "unknown",
                    "train_test_protocol": "unknown",
                    "result": "unknown",
                    "limitations": "Auto-promoted; design fields need manual fill",
                    "execution_mode": "UNKNOWN",
                    "authorship": "COMPILER_INFERRED",
                    "persistence_scope": "PROJECT_SPECIFIC",
                    "reuse_policy": "AUDIT_REFERENCE_ONLY",
                    "epistemic_role": "EMPIRICAL_RESULT",
                    "authority_level": "AUTO_CURATED",
                    "source_pointers": sp,
                    "inferred": True,
                    "inference_reason": "Promoted from high-signal event during authority pass",
                })
                added["experiments"] += 1

    if new_hy:
        write_jsonl(wdir / "hypotheses.jsonl", load_jsonl(wdir / "hypotheses.jsonl") + new_hy)
    if new_ex:
        write_jsonl(wdir / "experiments.jsonl", load_jsonl(wdir / "experiments.jsonl") + new_ex)

    # Upgrade conclusions with can_prove/cannot_prove + execution_mode
    cons = load_jsonl(wdir / "conclusions.jsonl")
    for c in cons:
        stmt = c.get("statement", "")
        if "live" in stmt.lower() or "ABANDON" in stmt or "fill" in stmt.lower():
            c["execution_mode"] = "LIVE"
            c["live_proven"] = "fill" in stmt.lower() or "ABANDON" in stmt
        elif "backtest" in stmt.lower() or "holdout" in stmt.lower():
            c["execution_mode"] = "BACKTEST"
            c["live_proven"] = False
        else:
            c["execution_mode"] = c.get("execution_mode", "UNKNOWN")
            c["live_proven"] = c.get("live_proven", False)
        if "can_prove" not in c or c.get("can_prove") == "unknown":
            c["can_prove"] = "See source_pointers; authority pass partial"
            c["cannot_prove"] = "Future regime; live execution not confirmed unless stated"
            added["conclusions_upgraded"] += 1
        c["authority_level"] = infer_authority(window_id, {"status": "QA_PASSED"})
    if cons:
        write_jsonl(wdir / "conclusions.jsonl", cons)

    # Failures → guardrail linkage
    fails = load_jsonl(wdir / "failures.jsonl")
    for f in fails:
        f["authority_level"] = infer_authority(window_id, {"status": "QA_PASSED"})
        if f.get("future_guardrail") and f.get("reuse_policy") != "ALWAYS_LOAD":
            f["reuse_policy"] = "ALWAYS_LOAD"
            f["persistence_scope"] = "PERMANENT"
    if fails:
        write_jsonl(wdir / "failures.jsonl", fails)

    return added


def stamp_authority(wdir: Path, level: str) -> int:
    n = 0
    for cat in CATEGORIES:
        objs = load_jsonl(wdir / f"{cat}.jsonl")
        for o in objs:
            if "authority_level" not in o:
                o["authority_level"] = level
                n += 1
            # Validated weeks: preserve execution metadata
            if level == "VALIDATED" and cat == "conclusions":
                stmt = o.get("statement", "")
                if "+59.7" in stmt or "holdout" in stmt.lower():
                    o.setdefault("execution_mode", "BACKTEST")
                    o.setdefault("live_proven", False)
        if objs:
            write_jsonl(wdir / f"{cat}.jsonl", objs)
    return n


def main():
    windows = load_jsonl(OS / "state/windows.jsonl")
    report = {"validated_stamped": 0, "auto_enriched": {}, "total_authority_stamped": 0}

    for w in windows:
        wid = w["window_id"]
        wdir = Path(w.get("output_dir", OS / "weekly" / w["dir_name"]))
        if not wdir.exists():
            continue
        level = infer_authority(wid, w)

        if wid in VALIDATED_WINDOWS:
            n = stamp_authority(wdir, "VALIDATED")
            report["validated_stamped"] += n
            report["total_authority_stamped"] += n
        elif wid in AUTO_DENSE and w.get("status") == "QA_PASSED":
            stamp_authority(wdir, "AUTO_CURATED")
            added = enrich_week(wdir, wid)
            report["auto_enriched"][wid] = added
            report["total_authority_stamped"] += stamp_authority(wdir, "AUTO_CURATED")
        elif w.get("status") == "ARTIFACTS_ONLY":
            report["total_authority_stamped"] += stamp_authority(wdir, "ARTIFACT_ONLY")

    (OS / "state/authority_promotion_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
