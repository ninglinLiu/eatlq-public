#!/usr/bin/env python3
"""Sprint084 expansion before/after summary (read-only)."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TOP10_MAX = 0.25
TARGET_REGIME = ("by_price_velocity_5m", "[-0.3,0)")


def load_cell(path: Path, kind: str, bucket: str) -> dict | None:
    if not path.is_file():
        return None
    for r in csv.DictReader(path.open()):
        if r.get("regime_kind") == kind and r.get("bucket") == bucket:
            return r
    return None


def review_gate_failures(cell: dict) -> list[str]:
    fails = []
    if int(float(cell.get("unique_pools") or 0)) < 20:
        fails.append("LOW_POOL_DIVERSITY")
    t10 = float(cell.get("top10_pool_share") or 1)
    if t10 >= TOP10_MAX:
        fails.append("TOP10_CONCENTRATION")
    if float(cell.get("T30_proxy_share") or 0) > 0.5:
        fails.append("PROXY_T30")
    if float(cell.get("source_top_share") or 0) > 0.7:
        fails.append("SINGLE_SOURCE")
    return fails


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--before-dir", default="mvp/audits/sprint084_pool_entry_quality/regime_review_v3")
    p.add_argument("--after-dir", default="mvp/audits/sprint084_pool_entry_quality/regime_review_v4")
    p.add_argument("--before-backfill", default="mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v2/economic_label_backfill_flags.json")
    p.add_argument("--after-backfill", default="mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v3/economic_label_backfill_flags.json")
    p.add_argument("--out-dir", default="mvp/audits/sprint084_pool_entry_quality/paper_quoter_expansion_v1")
    args = p.parse_args()

    before_cell = load_cell(
        REPO / args.before_dir / "positive_regime_candidates.csv",
        TARGET_REGIME[0],
        TARGET_REGIME[1],
    )
    after_cell = load_cell(
        REPO / args.after_dir / "positive_regime_candidates.csv",
        TARGET_REGIME[0],
        TARGET_REGIME[1],
    )
    bf = json.loads((REPO / args.before_backfill).read_text())
    af = json.loads((REPO / args.after_backfill).read_text())
    b_flags = json.loads((REPO / args.before_dir / "pool_entry_quality_regime_review_flags.json").read_text())
    a_flags = json.loads((REPO / args.after_dir / "pool_entry_quality_regime_review_flags.json").read_text())

    target_before = review_gate_failures(before_cell) if before_cell else []
    target_after = review_gate_failures(after_cell) if after_cell else []

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": a_flags.get("PAPER_FORWARD_ALLOWED", "false"),
        "MODEL_PROMOTION_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "coverage_before_after": {
            "paper_quoter_pools_before": bf["metrics_before_after"]["unique_pools_paper_quoter"],
            "paper_quoter_pools_after": af["metrics_before_after"]["unique_pools_paper_quoter"],
            "global_top10_before": b_flags["global_stats"]["top10_pool_share"],
            "global_top10_after": a_flags["global_stats"]["top10_pool_share"],
            "review_candidates_before": b_flags.get("review_candidates_count", 0),
            "review_candidates_after": a_flags.get("review_candidates_count", 0),
        },
        "target_regime_price_velocity_near_miss": {
            "regime": TARGET_REGIME,
            "before": {
                "top10_pool_share": before_cell.get("top10_pool_share") if before_cell else None,
                "source_top_share": before_cell.get("source_top_share") if before_cell else None,
                "unique_pools": before_cell.get("unique_pools") if before_cell else None,
                "n": before_cell.get("n") if before_cell else None,
                "gate_failures": target_before,
                "is_review_candidate": len(target_before) == 0,
            },
            "after": {
                "top10_pool_share": after_cell.get("top10_pool_share") if after_cell else None,
                "source_top_share": after_cell.get("source_top_share") if after_cell else None,
                "unique_pools": after_cell.get("unique_pools") if after_cell else None,
                "n": after_cell.get("n") if after_cell else None,
                "gate_failures": target_after,
                "is_review_candidate": len(target_after) == 0,
            },
        },
        "blockers": af.get("blockers", []),
        "RECOMMENDED_NEXT_ACTION": (
            "PAPER_FORWARD_REVIEW_CANDIDATE"
            if a_flags.get("PAPER_FORWARD_ALLOWED") == "REVIEW_CANDIDATE"
            else "continue_live_paper_shadow_and_quoter_expansion"
        ),
    }
    out = REPO / args.out_dir
    (out / "expansion_before_after_flags.json").write_text(json.dumps(flags, indent=2) + "\n")
    print(json.dumps(flags, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
