#!/usr/bin/env python3
"""Artifact-first fallback for SKIPPED_EMPTY windows — no full transcript re-read."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

OS = Path("/root/quant_research_os")
EATLQ = Path("/opt/EATLQ")
ROOT = Path("/root")

# New status taxonomy (replaces bare SKIPPED_EMPTY where applicable)
EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
NO_TRANSCRIPT = "NO_TRANSCRIPT_ACTIVITY"
ARTIFACTS_ONLY = "ARTIFACTS_ONLY"
SOURCE_INSUFFICIENT = "SOURCE_COVERAGE_INSUFFICIENT"


def load_jsonl(p: Path) -> list:
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []


def write_jsonl(p: Path, rows: list) -> None:
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def parse_window_times(w: dict) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(w["start"])
    end = datetime.fromisoformat(w["end"])
    return start, end


def scan_git(start: datetime, end: datetime) -> list[dict]:
    hits = []
    try:
        log = subprocess.check_output(
            ["git", "-C", str(EATLQ), "log",
             f"--since={start.isoformat()}", f"--until={end.isoformat()}",
             "--format=%H|%ai|%s"],
            text=True, errors="replace",
        )
        for line in log.strip().splitlines():
            if line.strip():
                h, ai, subj = line.split("|", 2)
                hits.append({"type": "GIT_COMMIT", "path": str(EATLQ), "preview": subj[:200], "ts": ai})
    except Exception:
        pass
    return hits


def scan_files(base: Path, start: datetime, end: datetime, patterns=("*.md", "*.py")) -> list[dict]:
    hits = []
    if not base.exists():
        return hits
    t0, t1 = start.timestamp(), end.timestamp() + 86400
    for pat in patterns:
        for p in base.rglob(pat):
            if any(x in str(p) for x in ["node_modules", ".git", "forge-std", "openzeppelin", "v4-core/lib"]):
                continue
            try:
                st = p.stat()
                if t0 <= st.st_mtime <= t1:
                    hits.append({"type": "FILE_MODIFIED", "path": str(p), "preview": p.name, "ts": datetime.fromtimestamp(st.st_mtime).isoformat()})
            except OSError:
                pass
    return hits


def scan_audits(start: datetime, end: datetime) -> list[dict]:
    hits = []
    audit = EATLQ / "mvp/audits"
    if not audit.exists():
        return hits
    t0, t1 = start.timestamp(), end.timestamp() + 86400
    for p in audit.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
            if t0 <= st.st_mtime <= t1:
                hits.append({"type": "REPORT_CREATED", "path": str(p), "preview": p.name, "ts": datetime.fromtimestamp(st.st_mtime).isoformat()})
        except OSError:
            pass
    return hits


def scan_root_md(start: datetime, end: datetime) -> list[dict]:
    hits = []
    t0, t1 = start.timestamp(), end.timestamp() + 86400
    for p in list(ROOT.glob("*.md")) + list(ROOT.glob("research_inventory*/*.md")):
        try:
            st = p.stat()
            if t0 <= st.st_mtime <= t1:
                hits.append({"type": "FILE_MODIFIED", "path": str(p), "preview": p.name, "ts": datetime.fromtimestamp(st.st_mtime).isoformat()})
        except OSError:
            pass
    return hits


def classify_window(w: dict) -> dict:
    start, end = parse_window_times(w)
    wdir = Path(w.get("output_dir", OS / "weekly" / w["dir_name"]))
    raw = load_jsonl(wdir / "weekly_raw_index.jsonl")
    selected = load_jsonl(wdir / "selected_lineage.jsonl")

    transcript_events = [e for e in raw if e.get("event_type") in ("USER_MESSAGE", "ASSISTANT_MESSAGE")]
    user_msgs = [e for e in transcript_events if e.get("event_type") == "USER_MESSAGE"]

    git_h = scan_git(start, end)
    eatlq_h = scan_files(EATLQ, start, end)
    audit_h = scan_audits(start, end)
    root_h = scan_root_md(start, end)
    artifact_hits = git_h + eatlq_h + audit_h + root_h

    # Dedupe artifact by path
    seen = set()
    artifacts = []
    for h in artifact_hits:
        key = h["path"]
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(h)

    report = {
        "window_id": w["window_id"],
        "transcript_user_msgs": len(user_msgs),
        "transcript_total": len(transcript_events),
        "raw_index_total": len(raw),
        "artifact_count": len(artifacts),
        "artifact_types": list({a["type"] for a in artifacts}),
        "artifact_sample": artifacts[:15],
    }

    if len(transcript_events) == 0 and len(artifacts) == 0:
        new_status = EMPTY_CONFIRMED
        report["classification"] = "No transcript and no artifacts in window"
    elif len(user_msgs) == 0 and len(transcript_events) > 0:
        new_status = NO_TRANSCRIPT  # assistant-only carried timestamps
        report["classification"] = "Transcript lines indexed but zero user prompts"
    elif len(user_msgs) == 0 and len(artifacts) > 0:
        new_status = ARTIFACTS_ONLY
        report["classification"] = "No user transcript; artifacts present — NEEDS_REVIEW"
    elif len(user_msgs) > 0 and len(artifacts) == 0:
        new_status = SOURCE_INSUFFICIENT
        report["classification"] = "User prompts exist but no git/audit/file artifacts found"
    elif len(user_msgs) > 0:
        new_status = "NEEDS_REVIEW"  # had been wrongly SKIPPED_EMPTY
        report["classification"] = "User + artifacts present; requires curation pass"
    else:
        new_status = ARTIFACTS_ONLY if artifacts else EMPTY_CONFIRMED
        report["classification"] = "Fallback"

    report["new_status"] = new_status
    return report


def main():
    windows = load_jsonl(OS / "state/windows.jsonl")
    results = []
    for w in windows:
        if w.get("status") not in ("SKIPPED_EMPTY", "EMPTY_CONFIRMED"):
            continue
        r = classify_window(w)
        w["status"] = r["new_status"]
        w["empty_classification"] = r["classification"]
        w["artifact_fallback"] = {
            "transcript_user_msgs": r["transcript_user_msgs"],
            "artifact_count": r["artifact_count"],
            "artifact_types": r["artifact_types"],
        }
        if r["new_status"] in (ARTIFACTS_ONLY, NO_TRANSCRIPT, SOURCE_INSUFFICIENT, "NEEDS_REVIEW"):
            w["authority_hint"] = "ARTIFACT_ONLY" if r["new_status"] == ARTIFACTS_ONLY else "UNVERIFIED"
        results.append(r)

    write_jsonl(OS / "state/windows.jsonl", windows)
    out = OS / "state/artifact_fallback_report.jsonl"
    write_jsonl(out, results)

    from collections import Counter
    c = Counter(w["status"] for w in windows)
    summary = {
        "reclassified": len(results),
        "status_counts": dict(c),
        "artifact_only": sum(1 for r in results if r["new_status"] == ARTIFACTS_ONLY),
        "empty_confirmed": sum(1 for r in results if r["new_status"] == EMPTY_CONFIRMED),
        "no_transcript_activity": sum(1 for r in results if r["new_status"] == NO_TRANSCRIPT),
        "needs_review_from_fallback": sum(1 for r in results if r["new_status"] == "NEEDS_REVIEW"),
    }
    (OS / "state/artifact_fallback_summary.json").write_text(json.dumps(summary, indent=2))

    # Update pipeline metrics
    state = json.loads((OS / "state/pipeline_state.json").read_text())
    processed_statuses = {"QA_PASSED", "NEEDS_REVIEW", "ARTIFACTS_ONLY", "NO_TRANSCRIPT_ACTIVITY",
                          "SOURCE_COVERAGE_INSUFFICIENT", "EXISTING_VALIDATED"}
    non_empty = [w for w in windows if w["status"] in processed_statuses or w["status"] == "QA_PASSED"]
    qa_passed = sum(1 for w in windows if w["status"] == "QA_PASSED")
    state.update({
        "TOTAL_REGISTERED_WINDOWS": len(windows),
        "NON_EMPTY_PROCESSED_WINDOWS": qa_passed + sum(1 for w in windows if w["status"] == "NEEDS_REVIEW"),
        "QA_PASSED_WINDOWS": qa_passed,
        "SKIPPED_EMPTY_WINDOWS": 0,  # deprecated bare SKIPPED_EMPTY
        "EMPTY_CONFIRMED": c.get(EMPTY_CONFIRMED, 0),
        "NO_TRANSCRIPT_ACTIVITY": c.get(NO_TRANSCRIPT, 0),
        "ARTIFACTS_ONLY": c.get(ARTIFACTS_ONLY, 0),
        "SOURCE_COVERAGE_INSUFFICIENT": c.get(SOURCE_INSUFFICIENT, 0),
        "NEEDS_REVIEW": c.get("NEEDS_REVIEW", 0),
        "QA_PASS_RATE_AMONG_PROCESSED": f"{100 * qa_passed / max(qa_passed + c.get('NEEDS_REVIEW', 0), 1):.0f}%",
        "QA_PASSED_SHARE_OF_ALL_WINDOWS": f"{100 * qa_passed / len(windows):.0f}%",
    })
    (OS / "state/pipeline_state.json").write_text(json.dumps(state, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
