#!/usr/bin/env python3
"""Cluster gate grid: (binary|continuous) × (≥1|≥2) dual-metric (B3)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

import sprint091_sender_gated_paper as s91  # noqa: E402

log = logging.getLogger("sprint091_cluster_gated")
OUT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated" / "cluster_gate_grid.json"


def run_grid(days: int = 14, horizon_min: int = 30) -> dict:
    configs = [
        {"scoring": "binary", "min_distinct_senders": 1},
        {"scoring": "binary", "min_distinct_senders": 2},
        {"scoring": "continuous", "min_distinct_senders": 1},
        {"scoring": "continuous", "min_distinct_senders": 2},
    ]
    results = []
    for cfg in configs:
        s91._MIN_DISTINCT_SENDERS = cfg["min_distinct_senders"]
        s91._SCORING_MODE = cfg["scoring"]
        s91._SENDER_SCORES = s91.load_sender_scores() if cfg["scoring"] == "continuous" else {}
        s91._USE_SINGLE_TICK = False
        robust = s91.run_backtest(
            days=days, horizon_sec=horizon_min * 60, write_readout_file=False, write_csv_file=False,
        )
        s91._USE_SINGLE_TICK = True
        single = s91.run_backtest(
            days=days, horizon_sec=horizon_min * 60, write_readout_file=False, write_csv_file=False,
        )
        results.append({
            **cfg,
            "robust_pf": robust.get("pf_gated_30m_tick"),
            "robust_n": robust.get("gated_signals"),
            "single_tick_pf": single.get("pf_gated_30m_tick"),
            "single_tick_n": single.get("gated_signals"),
            "dual_pass": (
                (robust.get("pf_gated_30m_tick") or 0) > 1.5
                and (robust.get("gated_signals") or 0) >= 15
                and (single.get("pf_gated_30m_tick") or 0) > 1.5
                and (single.get("gated_signals") or 0) >= 15
            ),
        })
        log.info(
            "config scoring=%s min=%d robust_pf=%s single_pf=%s",
            cfg["scoring"], cfg["min_distinct_senders"],
            robust.get("pf_gated_30m_tick"), single.get("pf_gated_30m_tick"),
        )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backtest_days": days,
        "horizon_min": horizon_min,
        "configs": results,
        "RESEARCH_ONLY": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Sprint091 cluster gate grid")
    p.add_argument("--backtest-days", type=int, default=14)
    p.add_argument("--horizon-min", type=int, default=30)
    args = p.parse_args()
    print(json.dumps(run_grid(days=args.backtest_days, horizon_min=args.horizon_min), indent=2))


if __name__ == "__main__":
    main()
