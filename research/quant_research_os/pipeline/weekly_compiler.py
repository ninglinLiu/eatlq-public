#!/usr/bin/env python3
"""Quant Research OS — parameterized weekly compiler (Round 3–6 schema)."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

OS_ROOT = Path("/root/quant_research_os")
TRANSCRIPT_BASE = Path("/root/.cursor/projects/root/agent-transcripts")
EATLQ = Path("/opt/EATLQ")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
TS_RE = re.compile(r"<timestamp>([^<]+)</timestamp>")
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(\w+)\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s+(AM|PM)\s+\((UTC[^)]+)\)"
)
HIGH_ASSIST = re.compile(
    r"结论|verdict|PASS|FAIL|WEAK_PASS|INVALIDATED|SUPERSEDED|PIVOT|"
    r"honeypot|−100%|-100%|ABANDON|SYSTEM_PANIC|go.?live|REJECT|SUPPORTED|"
    r"holdout|falsif|guardrail|execution_confirmed|月化|毁灭",
    re.I,
)
USER_TYPE_RULES = [
    (re.compile(r"GO-LIVE|解除 panic|开始 execution|上实盘|paper-forward", re.I), "REQUEST_FOR_EXECUTION"),
    (re.compile(r"暂停|SYSTEM_PANIC|stop|回收", re.I), "STOP_INSTRUCTION"),
    (re.compile(r"批准|拍板|放行|签字", re.I), "APPROVAL"),
    (re.compile(r"质疑|为什么|完蛋|怀疑|质问", re.I), "CHALLENGE"),
    (re.compile(r"纠正|不是|误|错了", re.I), "CORRECTION"),
    (re.compile(r"拒绝|REJECT", re.I), "REJECTION"),
    (re.compile(r"只读|核实|审计|验证", re.I), "REQUEST_FOR_VERIFICATION"),
    (re.compile(r"通用前缀|任务目标|# |hypothesis|假设", re.I), "ORIGINAL_RESEARCH_PROMPT"),
    (re.compile(r"决策|拍板", re.I), "DECISION"),
]


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def parse_ts(text: str) -> datetime | None:
    m = TS_RE.search(text or "")
    if not m:
        return None
    dm = DATE_RE.search(m.group(1))
    if not dm:
        return None
    _, mon, day, year, hr, mi, ap, _ = dm.groups()
    h = int(hr) % 12 + (12 if ap == "PM" and int(hr) != 12 else 0)
    if ap == "AM" and int(hr) == 12:
        h = 0
    return datetime(int(year), MONTHS[mon], int(day), h, int(mi))


def get_text(rec: dict) -> str:
    msg = rec.get("message") or {}
    content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") if c.get("type") == "text" else f"[tool:{c.get('name')}]"
            for c in content if isinstance(c, dict)
        )
    return str(content)


def classify_user(text: str) -> str:
    if len(text.strip()) < 20 and re.match(r"^(继续|开始|好的?)$", text.strip(), re.I):
        return "SYSTEM_GENERATED_FOLLOWUP"
    for pat, typ in USER_TYPE_RULES:
        if pat.search(text):
            return typ
    return "META_RESEARCH_REFLECTION" if len(text) > 200 else "UNKNOWN"


def authorship_confidence(text: str) -> str:
    if len(text) > 3000 or "【通用前缀】" in text:
        return "MIXED_CONTENT"
    if re.search(r"^.{0,300}(Agent|assistant|报告|Executive Conclusion)", text, re.I | re.M):
        return "MIXED_CONTENT"
    return "LIKELY_USER_ORIGINAL"


def write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── Stage 1: Raw Index ─────────────────────────────────────────────

def stage1_raw_index(start: datetime, end: datetime, out: Path) -> list[dict]:
    raw, eid = [], 0
    active: dict[str, list] = {}

    for jf in sorted(TRANSCRIPT_BASE.rglob("*.jsonl")):
        if "/subagents/" in str(jf):
            continue
        tid = jf.parent.name
        rows, last_ts, hit = [], None, False
        for idx, line in enumerate(open(jf, errors="ignore")):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = rec.get("role", "")
            if role not in ("user", "assistant"):
                continue
            text = get_text(rec)
            ts = parse_ts(text) if role == "user" else last_ts
            if role == "user" and parse_ts(text):
                last_ts = parse_ts(text)
                if start <= last_ts <= end:
                    hit = True
            rows.append((idx, rec, role, text, ts or last_ts))
        if hit:
            active[str(jf)] = rows

    for jf_str, rows in active.items():
        jf = Path(jf_str)
        tid = jf.parent.name
        for idx, rec, role, text, ts in rows:
            if not ts or ts < start or ts > end:
                continue
            eid += 1
            prec = "EMBEDDED" if role == "user" and parse_ts(text) else "CARRIED"
            evt = {
                "event_id": f"E-{eid:06d}",
                "timestamp": ts.isoformat(),
                "timestamp_precision": prec,
                "event_type": "USER_MESSAGE" if role == "user" else "ASSISTANT_MESSAGE",
                "source_file": str(jf),
                "transcript_id": tid,
                "jsonl_index": idx,
                "content_preview": text[:320].replace("\n", " "),
                "content_sha256": sha256(text),
            }
            if role == "user":
                evt["user_event_type"] = classify_user(text)
                evt["authorship_confidence"] = authorship_confidence(text)
            raw.append(evt)

    # git commits
    try:
        log = subprocess.check_output(
            ["git", "-C", str(EATLQ), "log",
             f"--since={start.isoformat()}", f"--until={(end + timedelta(seconds=1)).isoformat()}",
             "--format=%H|%ai|%s"],
            text=True, errors="replace",
        )
        for line in log.strip().splitlines():
            if not line.strip():
                continue
            h, ai, subj = line.split("|", 2)
            eid += 1
            raw.append({
                "event_id": f"E-{eid:06d}",
                "timestamp": ai.strip(),
                "timestamp_precision": "GIT",
                "event_type": "GIT_COMMIT",
                "source_file": str(EATLQ / ".git"),
                "transcript_id": h[:12],
                "jsonl_index": -1,
                "content_preview": subj[:320],
                "content_sha256": sha256(line),
            })
    except Exception:
        pass

    # audit reports
    audit_dir = EATLQ / "mvp/audits"
    if audit_dir.exists():
        for p in sorted(audit_dir.rglob("*")):
            if not p.is_file() or p.suffix not in (".md", ".json"):
                continue
            if not re.search(r"202[56]", p.name + str(p.parent.name)):
                continue
            try:
                st = p.stat()
                mtime = datetime.fromtimestamp(st.st_mtime)
                if mtime < start or mtime > end + timedelta(days=1):
                    continue
            except OSError:
                continue
            eid += 1
            prev = p.read_text(errors="replace")[:320].replace("\n", " ")
            raw.append({
                "event_id": f"E-{eid:06d}",
                "timestamp": mtime.isoformat(),
                "timestamp_precision": "MTIME",
                "event_type": "REPORT_CREATED",
                "source_file": str(p),
                "transcript_id": p.parent.name,
                "jsonl_index": -1,
                "content_preview": prev,
                "content_sha256": sha256(prev),
            })

    write_jsonl(out / "weekly_raw_index.jsonl", raw)
    return raw


# ── Stage 2: Selected Lineage ────────────────────────────────────

def stage2_lineage(raw: list[dict], out: Path) -> list[dict]:
    selected = []
    for evt in raw:
        et = evt["event_type"]
        if et == "USER_MESSAGE":
            ut = evt.get("user_event_type", "")
            if ut in ("SYSTEM_GENERATED_FOLLOWUP",) and len(evt["content_preview"]) < 30:
                continue
            selected.append({**evt, "selection_reason": ut or "USER_HIGH_VALUE"})
        elif et == "ASSISTANT_MESSAGE":
            if len(evt["content_preview"]) > 150 and HIGH_ASSIST.search(evt["content_preview"]):
                selected.append({**evt, "selection_reason": "HIGH_SIGNAL_ASSISTANT"})
        elif et in ("GIT_COMMIT", "REPORT_CREATED"):
            selected.append({**evt, "selection_reason": et})

    write_jsonl(out / "selected_lineage.jsonl", selected)

    users = [s for s in selected if s["event_type"] == "USER_MESSAGE"]
    lines = [f"# Selected Timeline — {out.name}\n\n"]
    for u in users[:50]:
        lines.append(
            f"- **{u['timestamp'][:16]}** [{u.get('user_event_type','?')}] "
            f"`{u['transcript_id'][:8]}` L{u['jsonl_index']}: {u['content_preview'][:90]}...\n"
        )
    if len(users) > 50:
        lines.append(f"\n_+{len(users)-50} more in selected_lineage.jsonl_\n")
    (out / "selected_timeline.md").write_text("".join(lines))
    return selected


# ── Stage 3: Canonical (automated extraction) ────────────────────

def mk(oid, otype, authorship="COMPILER_INFERRED", **kw):
    base = {
        "id": oid,
        "object_type": otype,
        "authorship": authorship,
        "persistence_scope": kw.pop("persistence_scope", "REGIME_DEPENDENT"),
        "reuse_policy": kw.pop("reuse_policy", "TOPIC_MATCH_ONLY"),
        "epistemic_role": kw.pop("epistemic_role", "EMPIRICAL_RESULT"),
        "source_pointers": kw.pop("source_pointers", []),
    }
    if authorship == "COMPILER_INFERRED":
        base["inferred"] = True
        base["inference_reason"] = kw.pop("inference_reason", "Auto-extracted from weekly selected lineage")
    base.update(kw)
    return base


def stage3_canonical(selected: list[dict], raw: list[dict], out: Path, window_id: str) -> dict:
    """Automated canonical extraction — sparse weeks get minimal objects."""
    counts = {k: 0 for k in ["user_theses", "research_principles", "hypotheses",
                              "experiments", "conclusions", "failures", "methods", "decisions"]}
    canon = {k: [] for k in counts}

    user_msgs = [s for s in selected if s.get("user_event_type") in (
        "ORIGINAL_RESEARCH_PROMPT", "CHALLENGE", "CORRECTION", "DECISION",
        "REQUEST_FOR_EXECUTION", "META_RESEARCH_REFLECTION", "REQUEST_FOR_VERIFICATION",
    ) and s.get("authorship_confidence") != "LIKELY_PASTED"]

    for i, u in enumerate(user_msgs[:8]):
        counts["user_theses"] += 1
        sp = {"source_file": u["source_file"], "jsonl_index": u["jsonl_index"],
              "transcript_id": u["transcript_id"]}
        canon["user_theses"].append(mk(
            f"UT-{window_id[-10:].replace('-','')}-{i+1:02d}", "USER_THESIS",
            statement=u["content_preview"][:500],
            epistemic_role="USER_BELIEF" if "CHALLENGE" not in u.get("user_event_type", "") else "USER_OBJECTION",
            authorship="USER_EXPLICIT" if u.get("authorship_confidence") == "LIKELY_USER_ORIGINAL" else "COMPILER_INFERRED",
            source_pointers=[sp],
        ))

    # failures from audit previews mentioning guardrail patterns
    fail_pat = re.compile(
        r"lookahead|honeypot|state.?override|pipeline|archiver|fee.?encod|"
        r"授权|成交|backtest.*live|Quoter|survivor",
        re.I,
    )
    reports = [r for r in raw if r["event_type"] == "REPORT_CREATED" and fail_pat.search(r["content_preview"])]
    for i, r in enumerate(reports[:5]):
        counts["failures"] += 1
        canon["failures"].append(mk(
            f"FL-{window_id[-10:].replace('-','')}-{i+1:02d}", "FAILURE",
            failure_id=f"FL-{window_id[-10:].replace('-','')}-{i+1:02d}",
            name=r["content_preview"][:80],
            failure_type="auto_detected",
            what_happened=r["content_preview"][:300],
            why_it_happened="unknown",
            impact="unknown",
            how_detected="audit report in window",
            future_guardrail="See source audit report",
            source_pointers=[{"source_file": r["source_file"], "jsonl_index": -1}],
            reuse_policy="AUDIT_REFERENCE_ONLY",
        ))

    # decisions from execution-related user msgs
    for i, u in enumerate([s for s in selected if s.get("user_event_type") == "REQUEST_FOR_EXECUTION"][:3]):
        counts["decisions"] += 1
        canon["decisions"].append(mk(
            f"DC-{window_id[-10:].replace('-','')}-{i+1:02d}", "DECISION",
            decision=u["content_preview"][:400],
            execution_status="AUTHORIZED",
            execution_confirmed=False,
            execution_mode="UNKNOWN",
            authorship="USER_EXPLICIT",
            epistemic_role="DECISION",
            source_pointers=[{"source_file": u["source_file"], "jsonl_index": u["jsonl_index"]}],
        ))

    # conclusions from high-signal assistant
    assists = [s for s in selected if s["event_type"] == "ASSISTANT_MESSAGE"][:5]
    for i, a in enumerate(assists):
        counts["conclusions"] += 1
        canon["conclusions"].append(mk(
            f"CN-{window_id[-10:].replace('-','')}-{i+1:02d}", "CONCLUSION",
            statement=a["content_preview"][:400],
            status="unknown",
            evidence_level="unknown",
            can_prove="unknown",
            cannot_prove="unknown",
            valid_from="unknown",
            valid_until="unknown",
            regime_dependency="unknown",
            project_scope="unknown",
            superseded_by="unknown",
            contradictions=[],
            authorship="AGENT_EXPLICIT",
            source_pointers=[{"source_file": a["source_file"], "jsonl_index": a["jsonl_index"]}],
        ))

    for name in counts:
        write_jsonl(out / f"{name}.jsonl", canon[name])

    write_jsonl(out / "knowledge_graph.jsonl", [])

    summary = [
        f"# Weekly Canonical Summary — {window_id}\n\n",
        f"- Raw events: **{len(raw)}**\n",
        f"- Selected: **{len(selected)}**\n",
        f"- Canonical objects: **{sum(counts.values())}**\n\n",
        "## Counts\n\n",
    ]
    for k, v in counts.items():
        summary.append(f"- {k}: {v}\n")
    summary.append("\n_Auto-compiled; review flagged weeks for NEEDS_REVIEW._\n")
    (out / "weekly_canonical_summary.md").write_text("".join(summary))
    (out / "open_questions.md").write_text(f"# Open Questions — {window_id}\n\n_Auto-generated placeholder; requires manual review if canonical sparse._\n")
    (out / "source_conflicts.md").write_text(f"# Source Conflicts — {window_id}\n\n_None auto-detected._\n")

    return {"counts": counts, "total_objects": sum(counts.values())}


# ── Stage 4: QA template ─────────────────────────────────────────

def stage4_qa(selected: list[dict], out: Path, window_id: str) -> str:
    users = [s for s in selected if s["event_type"] == "USER_MESSAGE"]
    assists = [s for s in selected if s["event_type"] == "ASSISTANT_MESSAGE"]
    reports = [s for s in selected if s["event_type"] == "REPORT_CREATED"]

    notes = []
    if len(selected) < 3:
        status = "SKIPPED_EMPTY"
    elif len(users) == 0:
        status = "NEEDS_REVIEW"
        notes.append("Activity without indexed user prompts")
    elif len(users) >= 2 and len(selected) >= 10:
        status = "QA_PASSED"
        notes.append("Auto QA — manual 25-sample review recommended before monthly merge")
    else:
        status = "NEEDS_REVIEW"
        notes.append("Sparse week")

    body = [
        f"# Quality Sample Audit — {window_id}\n\n",
        f"**Status: {status}**\n\n",
        f"- User messages selected: {len(users)}\n",
        f"- Assistant high-signal: {len(assists)}\n",
        f"- Reports: {len(reports)}\n\n",
        "## Automated checks\n\n",
        "| Check | Result |\n|-------|--------|\n",
        f"| Raw events exist | {'✅' if selected else '❌'} |\n",
        f"| User prompts present | {'✅' if users else '❌'} |\n",
        f"| Critical lineage (manual) | PENDING — auto pipeline |\n\n",
    ]
    if notes:
        body.append("## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n")
    (out / "quality_sample_audit.md").write_text("".join(body))
    return status


# ── Stage 5: Boundary placeholder ────────────────────────────────

def stage5_boundary(out: Path, window_id: str) -> None:
    (out / "boundary_continuity_check.md").write_text(
        f"# Boundary Continuity — {window_id}\n\n"
        "_Compare with prior week after global consolidation._\n\n"
        "**Verdict: UNKNOWN** (auto-generated)\n"
    )


def run_week(start: datetime, end: datetime, out_dir: Path, window_id: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = stage1_raw_index(start, end, out_dir)
    selected = stage2_lineage(raw, out_dir)
    meta = stage3_canonical(selected, raw, out_dir, window_id)
    qa_status = stage4_qa(selected, out_dir, window_id)
    stage5_boundary(out_dir, window_id)

    (out_dir / "compiler_audit.md").write_text(
        f"# Compiler Audit — {window_id}\n\n"
        f"- Pipeline: Quant Research OS weekly_compiler\n"
        f"- Raw: {len(raw)} | Selected: {len(selected)} | Canonical: {meta['total_objects']}\n"
        f"- QA status: {qa_status}\n"
    )
    return {
        "window_id": window_id,
        "raw_events": len(raw),
        "selected": len(selected),
        "object_counts": meta["counts"],
        "qa_status": qa_status,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window-id", required=True)
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    result = run_week(start, end, Path(args.out), args.window_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
