#!/usr/bin/env python3
"""
Sprint092 — whitepaper-faithful sweep with all 4 corrections applied:

  1. Pool-age constraint REMOVED (whitepaper "not pinned" per design.md §3).
  2. Base-routing target exclusion ENFORCED (target ∉ {WETH, USDC, USDbC, cbBTC}).
  3. Honeypot check: historical sell-proxy by default (clearly labelled proxy),
     OR Quoter V2 eth_call if --quoter-honeypot flag is set.
  4. Whale_score remains 0/1 binarisation (no XGBoost model trained); explicitly
     documented as implementation simplification — whitepaper formal Whale =
     XGBoost confidence × 1.3 bonus.

Runs single discovery pass at lowest threshold ($10k) then in-memory filters
the full Mode-A backtest at each higher threshold.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from sprint092_universe_expander import (  # noqa: E402
    fee_tier_sql_in,
    discover_whale_buy_events,
    check_quoter_v2_sell_path,
)
from sprint092_null_overlay_strategy import (  # noqa: E402
    HOLDOUT_DAYS,
    MAIN_DAYS,
    OUT_DIR,
    run_backtest_from_candidates,
)
import sprint092_null_overlay_strategy as strat


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Sprint092 whitepaper-faithful sweep (post-review)")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--lowest-thresh", type=int, default=10_000)
    p.add_argument("--thresholds", type=int, nargs="+", default=[100_000, 50_000, 25_000, 10_000])
    p.add_argument("--max-pool-age-hours", type=int, default=8760,
                   help="default 1y (whitepaper not pinned per design.md §3 line 65)")
    p.add_argument("--quoter-honeypot", action="store_true",
                   help="use Quoter V2 eth_call honeypot (whitepaper-exact); "
                        "default is historical sell-proxy")
    p.add_argument("--no-honeypot", action="store_true")
    args = p.parse_args()

    base_thresh = args.lowest_thresh
    logging.info(
        "base discovery: %dd × thresh=$%d × max_pool_age=%dh × base-routing-excluded",
        args.days, base_thresh, args.max_pool_age_hours,
    )
    base_candidates = discover_whale_buy_events(
        days=args.days,
        max_pool_age_hours=args.max_pool_age_hours,
        whale_usd_thresh=base_thresh,
    )
    logging.info("base discovery: %d candidates", len(base_candidates))

    rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL", "")
    if args.quoter_honeypot and not rpc_url:
        raise RuntimeError("--quoter-honeypot requires EVM_HTTP_URL or LABEL_RPC_URL")

    quoter_cache: dict[str, bool] = {}

    def _quoter_filter(cands: list[dict]) -> list[dict]:
        """Apply Quoter V2 honeypot per unique target token."""
        out = []
        for c in cands:
            tgt = c.get("target_address", "")
            tgt_dec = int(c.get("target_decimals", 18))
            if not tgt:
                continue
            if tgt in quoter_cache:
                if quoter_cache[tgt]:
                    out.append(c)
                continue
            res = check_quoter_v2_sell_path(tgt, tgt_dec, rpc_url)
            ok = bool(res.get("ok"))
            quoter_cache[tgt] = ok
            logging.info("quoter %s dec=%d → ok=%s (out=%d)", tgt[:10], tgt_dec, ok, res.get("amount_out"))
            if ok:
                out.append(c)
        return out

    if args.quoter_honeypot:
        logging.info("applying Quoter V2 honeypot pre-filter to %d candidates", len(base_candidates))
        base_candidates = _quoter_filter(base_candidates)
        logging.info("after Quoter V2: %d candidates", len(base_candidates))

    sweep_dir = OUT_DIR / "whitepaper_faithful_v3"
    sweep_dir.mkdir(parents=True, exist_ok=True)

    sweep: list[dict] = []
    for thresh in args.thresholds:
        logging.info("=== tier $%d ===", thresh)
        tier_dir = sweep_dir / f"thresh_{thresh}"
        tier_dir.mkdir(parents=True, exist_ok=True)
        strat.POSITIONS_CSV = tier_dir / "positions.csv"
        strat.DAILY_ROLLUP_CSV = tier_dir / "daily_rollup.csv"
        strat.FEE_TIER_CSV = tier_dir / "fee_tier_breakdown.csv"
        strat.GATE_EVAL_JSON = tier_dir / "forward_gate_eval.json"
        strat.READOUT_MD = tier_dir / "readout.md"

        tier_cands = [c for c in base_candidates
                      if float(c.get("notional_usd", 0)) >= thresh]
        logging.info("tier $%d: %d candidates", thresh, len(tier_cands))

        result = run_backtest_from_candidates(
            candidates=tier_cands,
            days=args.days,
            main_days=MAIN_DAYS,
            max_pool_age_hours=args.max_pool_age_hours,
            check_honeypot=not args.no_honeypot,
            whale_usd_thresh=thresh,
        )
        sweep.append({
            "whale_usd_thresh": thresh,
            "candidates_in": len(tier_cands),
            "candidates_scanned": result.get("candidates_scanned"),
            "n_signals": result.get("n_signals"),
            "skipped": result.get("skipped"),
            "pf_all": result.get("pf_all"),
            "win_rate_all": result.get("win_rate_all"),
            "top_pool_share": result.get("top_pool_share"),
            "unique_pools": result.get("unique_pools"),
            "verdict": result.get("verdict"),
        })

    out_path = sweep_dir / "sweep_summary.json"
    out_path.write_text(json.dumps({
        "sprint": 92,
        "version": "whitepaper_faithful_v3_post_review",
        "corrections": [
            "drop_72h_pool_age_constraint (whitepaper not pinned)",
            "enforce_base_routing_target_exclusion",
            "honeypot_quoter_v2" if args.quoter_honeypot else "honeypot_historical_sell_proxy",
            "whale_score_remains_binary_no_xgboost_model",
        ],
        "params": vars(args),
        "base_candidates": len(base_candidates),
        "sweep": sweep,
    }, indent=2))
    print(json.dumps({"summary_path": str(out_path), "sweep": sweep}, indent=2))


if __name__ == "__main__":
    main()
