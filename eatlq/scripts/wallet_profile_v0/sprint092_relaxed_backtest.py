#!/usr/bin/env python3
"""
Sprint092 — RELAXED-AGE alpha verification.

Runs the whitepaper-faithful $100k single-buy backtest with pool_age <= 1 year,
to test whether the alpha exists at all on Base (the strict 72h whitepaper
window yields 0 candidates).

Targets only the days that contain $100k+ buys, skipping the slow 21-day full
discovery sweep.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from sprint092_universe_expander import _discover_day, fee_tier_sql_in  # noqa: E402
from sprint092_null_overlay_strategy import (  # noqa: E402
    HOLDOUT_DAYS,
    MAIN_DAYS,
    OUT_DIR,
    run_backtest_from_candidates,
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fee_in = fee_tier_sql_in()

    days_with_100k = [3, 8]  # 21-day window: May 17 (-8) + May 23 (-3) have all 6 $100k buys
    all_candidates = []
    for d in days_with_100k:
        logging.info("discovering day -%d ...", d)
        rows = _discover_day(d, max_pool_age_hours=8760, whale_usd_thresh=100_000, fee_in=fee_in)
        logging.info("day -%d: %d candidates", d, len(rows))
        all_candidates.extend(rows)
    logging.info("total candidates: %d", len(all_candidates))

    out_dir = OUT_DIR / "relaxed_age"
    out_dir.mkdir(parents=True, exist_ok=True)

    import sprint092_null_overlay_strategy as strat
    strat.POSITIONS_CSV = out_dir / "positions.csv"
    strat.DAILY_ROLLUP_CSV = out_dir / "daily_rollup.csv"
    strat.FEE_TIER_CSV = out_dir / "fee_tier_breakdown.csv"
    strat.GATE_EVAL_JSON = out_dir / "forward_gate_eval.json"
    strat.READOUT_MD = out_dir / "readout.md"

    result = run_backtest_from_candidates(
        candidates=all_candidates,
        days=MAIN_DAYS + HOLDOUT_DAYS,
        main_days=MAIN_DAYS,
        max_pool_age_hours=8760,
        check_honeypot=True,
        whale_usd_thresh=100_000,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
