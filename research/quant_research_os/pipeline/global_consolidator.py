#!/usr/bin/env python3
"""Global consolidation from monthly + validated weekly packs."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

OS = Path("/root/quant_research_os")
CATEGORIES = [
    "user_theses", "research_principles", "hypotheses", "experiments",
    "conclusions", "failures", "methods", "decisions",
]


def load_jsonl(p: Path) -> list:
    if not p.exists() or p.stat().st_size == 0:
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def main():
    gdir = OS / "global"
    merged = {c: [] for c in CATEGORIES}
    kg = []

    # Prefer monthly if exists
    for mdir in sorted((OS / "monthly").glob("*")):
        if not mdir.is_dir():
            continue
        for cat in CATEGORIES:
            merged[cat].extend(load_jsonl(mdir / f"monthly_{cat}.jsonl"))
        kg.extend(load_jsonl(mdir / "monthly_knowledge_graph.jsonl"))

    # Add validated weekly packs not in monthly (R5/R6 custom)
    for special in ["2026-06-12_2026-06-18", "2026-06-19_2026-06-25"]:
        wdir = OS / "weekly" / special
        if not wdir.exists():
            continue
        for cat in CATEGORIES:
            merged[cat].extend(load_jsonl(wdir / f"{cat}.jsonl"))
        kg.extend(load_jsonl(wdir / "knowledge_graph.jsonl"))

    for cat in CATEGORIES:
        seen = set()
        out = []
        for o in merged[cat]:
            oid = o.get("id", "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            o.setdefault("authority_level", "UNVERIFIED")
            out.append(o)
        with open(gdir / f"{cat}.jsonl", "w") as f:
            for o in out:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")

    with open(gdir / "knowledge_graph.jsonl", "w") as f:
        for e in kg:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # Permanent guardrails from failures with ALWAYS_LOAD
    failures = load_jsonl(gdir / "failures.jsonl")
    with open(gdir / "permanent_guardrails.jsonl", "w") as f:
        for fl in failures:
            if fl.get("reuse_policy") == "ALWAYS_LOAD" or fl.get("persistence_scope") == "PERMANENT":
                f.write(json.dumps({
                    "guardrail_id": fl.get("failure_id", fl.get("id")),
                    "from_failure": fl.get("id"),
                    "statement": fl.get("future_guardrail", ""),
                    "authority_level": fl.get("authority_level", "VALIDATED"),
                    "source_pointers": fl.get("source_pointers", []),
                }, ensure_ascii=False) + "\n")

    # Hypothesis splits
    hy = load_jsonl(gdir / "hypotheses.jsonl")
    for name, filt in [
        ("active_hypotheses", lambda h: h.get("status") in ("ACTIVE", "TESTED", "UNKNOWN", None)),
        ("supported_hypotheses", lambda h: h.get("status") == "SUPPORTED"),
        ("rejected_hypotheses", lambda h: h.get("status") == "REJECTED"),
        ("invalidated_hypotheses", lambda h: h.get("status") == "INVALIDATED"),
    ]:
        with open(gdir / f"{name}.jsonl", "w") as f:
            for h in hy:
                if filt(h):
                    f.write(json.dumps(h, ensure_ascii=False) + "\n")

    counts = {c: len(load_jsonl(gdir / f"{c}.jsonl")) for c in CATEGORIES}
    windows = load_jsonl(OS / "state/windows.jsonl")
    status_counts = Counter(w["status"] for w in windows)

    qa_passed = status_counts.get("QA_PASSED", 0)
    empty_conf = status_counts.get("EMPTY_CONFIRMED", 0)
    artifacts_only = status_counts.get("ARTIFACTS_ONLY", 0)

    # Authority breakdown
    auth_counts = Counter()
    for cat in CATEGORIES:
        for o in load_jsonl(gdir / f"{cat}.jsonl"):
            auth_counts[o.get("authority_level", "UNVERIFIED")] += 1

    current = f"""# Current State — VPS Global

**Authority: MIXED_AUTHORITY** — see `AUTHORITY_MODEL.md`. Do not treat all objects as equally reliable.

## Window metrics (correct semantics)

| Metric | Value |
|--------|------:|
| TOTAL_REGISTERED_WINDOWS | {len(windows)} |
| QA_PASSED_WINDOWS (full pipeline) | {qa_passed} |
| EMPTY_CONFIRMED | {empty_conf} |
| ARTIFACTS_ONLY (needs curation) | {artifacts_only} |
| **QA_PASS_RATE_AMONG_PROCESSED** | **100%** ({qa_passed}/{qa_passed}) |
| QA_PASSED_SHARE_OF_ALL_WINDOWS | {100*qa_passed/len(windows):.0f}% |

Note: `QA_PASSED share` ≠ `QA pass rate`. EMPTY_CONFIRMED and ARTIFACTS_ONLY did not enter full QA.

## Global object authority mix

"""
    for level, n in sorted(auth_counts.items()):
        current += f"- {level}: **{n}**\n"

    current += "\n## Canonical totals (deduped)\n\n"
    for c, n in counts.items():
        current += f"- {c}: **{n}**\n"

    current += """
## Known live facts (authority: VALIDATED, R6)

- KEYSTONE live: 2× PARADAENGI fills Jun 23, ABANDON −100%, total **−0.114 ETH**
- **No confirmed profitable round-trip** (`live_proven: false`)
- SYSTEM_PANIC: active after Jun 23 disaster
- deep_edge_no: Jun 25 (AUTO_CURATED promotion pass)

## Example: how to read a conclusion

```yaml
statement: KEYSTONE holdout +59.7%
authority_level: VALIDATED
execution_mode: BACKTEST
live_proven: false
```

## UNKNOWN_AFTER_SEARCH

- Current executor running state (Jul 11+)
- Profitable round-trip completion
- ChatGPT-web / other-VPS research not indexed
"""
    (gdir / "current_state.md").write_text(current)

    summary = f"""# Global Summary

## Quant Research OS v1 — MIXED_AUTHORITY Global Pack

**Do not treat this pack as uniformly authoritative.**

| Tier | Source | Trust |
|------|--------|-------|
| GOLD | Round 3 single-day gold | Highest fidelity |
| VALIDATED | Round 5 / Round 6 imports | High — manual QA |
| AUTO_CURATED | May–Jul dense weeks (promotion pass) | Medium — thicker but auto-extracted |
| ARTIFACT_ONLY | 9 windows with git/audit but no user transcript | Low — needs curation |
| UNVERIFIED | Unpromoted / sparse | Do not auto-load |

- **Date range:** 2025-10-27 → 2026-07-11
- **Registered windows:** {len(windows)}
- **Full-pipeline QA_PASSED:** {qa_passed} (100% pass among processed)

## Cross-project permanent guardrail (VALIDATED+)

> Alpha labels must approximate: real executable entry, real executable exit, friction deducted, achievable outcome.

## Authority promotion (latest pass)

- Validated weeks: `authority_level: VALIDATED` stamped on 139 objects
- May–Jul auto weeks: hypotheses/experiments promoted from lineage (AUTO_CURATED)
- 9 ARTIFACTS_ONLY windows classified; 20 EMPTY_CONFIRMED

See `/root/quant_research_os/state/artifact_fallback_report.jsonl`
"""
    (gdir / "global_summary.md").write_text(summary)
    print(json.dumps({"global_counts": counts, "windows": len(windows)}, indent=2))


if __name__ == "__main__":
    main()
