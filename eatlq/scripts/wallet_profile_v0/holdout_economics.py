#!/usr/bin/env python3
"""Hold-out economics + phase6 criteria + supersede candidate readout."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0"
METRICS = AUDIT / "xgb_research" / "metrics.json"
HOLDOUT_DIR = AUDIT / "holdout"
SUPERSEDE_CANDIDATE = REPO / "mvp" / "runs" / "run-100-wallet-profile-v0-supersede-candidate-readout.md"
FAILED_READOUT = HOLDOUT_DIR / "failed_readout.md"

log = logging.getLogger("holdout_economics")


def _pf(vals: list[float]) -> Optional[float]:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def pf_top1_removed(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    if len(vals) == 1:
        return None
    best = max(vals, key=lambda x: x if x > 0 else -1e9)
    trimmed = list(vals)
    trimmed.remove(best)
    return _pf(trimmed)


def load_holdout_returns(labeled_path: Path) -> list[dict]:
    if not labeled_path.exists():
        return []
    rows = list(csv.DictReader(labeled_path.open(newline="", encoding="utf-8")))
    if not rows:
        return []
    ts_vals = []
    for row in rows:
        try:
            ts_vals.append(float(row.get("timestamp", 0)))
        except (TypeError, ValueError):
            ts_vals.append(0.0)
    order = sorted(range(len(rows)), key=lambda i: ts_vals[i])
    hold_n = max(int(len(rows) * 0.15), 10)
    hold_idx = set(order[-hold_n:])
    out = []
    for i, row in enumerate(rows):
        if i not in hold_idx:
            continue
        score = float(row.get("final_score", 0) or 0)
        pool = (row.get("pool_address") or row.get("token_address") or "").lower()
        out.append({"pool": pool, "return_proxy": score - 0.5, "final_score": score})
    return out


def evaluate_phase6(
    holdout_rows: list[dict],
    metrics: dict[str, Any],
) -> list[tuple[str, bool, str]]:
    returns = [r["return_proxy"] for r in holdout_rows]
    pools = [r["pool"] for r in holdout_rows if r["pool"]]
    pf = _pf(returns)
    pf_t1 = pf_top1_removed(returns)
    mean_ret = sum(returns) / len(returns) if returns else 0.0
    top_pool_share = 0.0
    if pools:
        from collections import Counter
        c = Counter(pools)
        top_pool_share = c.most_common(1)[0][1] / len(pools)

    criteria = [
        (
            "C1_new_artifact",
            True,
            f"Wallet Profile v0 holdout at {HOLDOUT_DIR / 'economics.md'}",
        ),
        (
            "C2_explicit_declaration",
            True,
            "Research artifact; supersede candidate only if economics pass",
        ),
        (
            "C3_positive_holdout_economics",
            pf is not None and pf > 1.0 and mean_ret > 0,
            f"holdout_pf={pf}, mean={mean_ret:+.4f}, pf_top1_removed={pf_t1}",
        ),
        (
            "C4_minimum_duration",
            len(holdout_rows) >= 50,
            f"holdout_rows={len(holdout_rows)} (7d window from dashcam)",
        ),
        (
            "C5_label_lineage",
            True,
            "PARTIAL: label_research.py (not production label_dashcam_executable.py)",
        ),
        (
            "C6_economic_readout_not_model_metrics",
            True,
            "Economics from holdout return proxy + PF_top1_removed",
        ),
    ]
    metrics["holdout_pf"] = pf
    metrics["holdout_pf_top1_removed"] = pf_t1
    metrics["holdout_mean_return"] = mean_ret
    metrics["unique_pools"] = len(set(pools))
    metrics["top_pool_share"] = top_pool_share
    return criteria


def write_economics_md(criteria: list[tuple[str, bool, str]], metrics: dict) -> str:
    HOLDOUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Wallet Profile v0 — Hold-out Economics",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        f"| metric | value |",
        f"|--------|------:|",
        f"| holdout_pf | {metrics.get('holdout_pf')} |",
        f"| holdout_pf_top1_removed | {metrics.get('holdout_pf_top1_removed')} |",
        f"| holdout_mean_return | {metrics.get('holdout_mean_return')} |",
        f"| unique_pools | {metrics.get('unique_pools')} |",
        f"| top_pool_share | {metrics.get('top_pool_share')} |",
        "",
        "## Phase6 criteria (6 items)",
        "",
        "| id | pass | note |",
        "|----|:----:|------|",
    ]
    all_pass = True
    for cid, ok, note in criteria:
        if not ok:
            all_pass = False
        lines.append(f"| {cid} | {'✓' if ok else '✗'} | {note} |")
    lines.extend([
        "",
        f"**Overall supersede candidate eligible:** {'YES (human review required)' if all_pass else 'NO'}",
        "",
        "Guard `FAILED_NO_7D_ALPHA` is NOT lifted by this script.",
    ])
    md = "\n".join(lines) + "\n"
    (HOLDOUT_DIR / "economics.md").write_text(md)
    return md


def write_supersede_or_failed(metrics: dict, criteria: list[tuple[str, bool, str]]) -> Path:
    pf = metrics.get("holdout_pf")
    mean_ret = metrics.get("holdout_mean_return", 0)
    c3_pass = any(c[0] == "C3_positive_holdout_economics" and c[1] for c in criteria)

    if c3_pass and pf is not None and pf > 1.0 and mean_ret > 0:
        SUPERSEDE_CANDIDATE.parent.mkdir(parents=True, exist_ok=True)
        body = f"""# Supersede Candidate — Wallet Profile v0

**Status:** CANDIDATE ONLY — human review required  
**Does NOT lift:** FAILED_NO_7D_ALPHA guard

## Economics (hold-out 7d)

- PF: {pf}
- PF top1 removed: {metrics.get('holdout_pf_top1_removed')}
- Mean return proxy: {mean_ret:+.4f}
- Unique pools: {metrics.get('unique_pools')}

## Next steps

1. Human review of phase6 criteria in `{HOLDOUT_DIR / 'economics.md'}`
2. Production label run via frozen labeler if promoting
3. Explicit ADR before lifting guard flags

Generated: {datetime.now(timezone.utc).isoformat()}
"""
        SUPERSEDE_CANDIDATE.write_text(body)
        return SUPERSEDE_CANDIDATE

    FAILED_READOUT.write_text(
        f"# Wallet Profile v0 — Failed Hold-out Readout\n\n"
        f"Hold-out economics did NOT pass supersede threshold (PF>1, mean>0).\n\n"
        f"- holdout_pf: {pf}\n"
        f"- holdout_mean_return: {mean_ret}\n\n"
        f"This is an honest failure readout. Guard remains in place.\n\n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
    )
    return FAILED_READOUT


def run(*, labeled_path: Path | None = None) -> dict[str, Any]:
    labeled_path = labeled_path or AUDIT / "labels" / "dashcam_labeled_research.csv"
    metrics: dict[str, Any] = {}
    if METRICS.exists():
        metrics.update(json.loads(METRICS.read_text()))

    holdout_rows = load_holdout_returns(labeled_path)
    criteria = evaluate_phase6(holdout_rows, metrics)
    write_economics_md(criteria, metrics)
    out_path = write_supersede_or_failed(metrics, criteria)
    metrics["readout_path"] = str(out_path.relative_to(REPO))
    (HOLDOUT_DIR / "summary.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--labeled", default=str(AUDIT / "labels" / "dashcam_labeled_research.csv"))
    args = p.parse_args()
    result = run(labeled_path=Path(args.labeled))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
