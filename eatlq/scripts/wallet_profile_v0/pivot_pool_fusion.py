#!/usr/bin/env python3
"""
Pivot route — pool-centric onset + 4-dim fusion (no binary smart_money gate).

Uses buyer (tx.from) semantics via uniswap_v3_swaps_with_buyer.
Wallet tags contribute as weak prior only (continuous w_score).

Output:
  mvp/audits/wallet_profile_v0/pivot/pool_fusion_paper_shadow.csv
  mvp/audits/wallet_profile_v0/pivot/pool_fusion_readout.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

import sprint091_sender_gated_paper as s91  # noqa: E402
from ch_helper import CHAIN_ID, ch_query, write_csv  # noqa: E402
from fusion_shadow import load_wallet_tags, w_score  # noqa: E402

log = logging.getLogger("pivot_pool_fusion")
OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "pivot"
OUT_CSV = OUT_DIR / "pool_fusion_paper_shadow.csv"
OUT_JSON = OUT_DIR / "pool_fusion_readout.json"


def pool_onset_features(pool: str, onset_wts: int) -> dict[str, float]:
    """CH proxy for 4-dim fusion features at onset (5m window)."""
    dt = datetime.fromtimestamp(onset_wts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(onset_wts + 300, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT
        count() AS heat,
        uniqExact(buyer_address) AS uniq_buyers,
        avgIf(tick, amount0 < 0) AS avg_buy_tick,
        stddevPop(tick) AS tick_vol
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >= toDateTime('{dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
        if not rows:
            return {"activity_heat": 0.0, "unique_buyer_entropy_5m": 0.0, "price_velocity_5m": 0.0, "token_volatility_5m": 0.0}
        r = rows[0]
        heat = float(r.get("heat") or 0)
        ub = float(r.get("uniq_buyers") or 0)
        vol = float(r.get("tick_vol") or 0)
        return {
            "activity_heat": min(heat / 10.0, 100.0),
            "unique_buyer_entropy_5m": min(ub / 10.0, 1.0),
            "price_velocity_5m": 0.0,
            "token_volatility_5m": max(vol / max(abs(float(r.get("avg_buy_tick") or 1)), 1), 0.0),
            "buy_pressure_ratio": 0.5,
        }
    except Exception:
        return {"activity_heat": 0.0, "unique_buyer_entropy_5m": 0.0, "price_velocity_5m": 0.0, "token_volatility_5m": 0.0, "buy_pressure_ratio": 0.5}


def fusion_score(pool: str, buyers: list[str], features: dict[str, float], tags: dict) -> dict[str, Any]:
    """4-dim fusion + weak wallet prior (max buyer w_score)."""
    wallet_prior = max((w_score(b, tags) for b in buyers), default=0.0)
    momentum = float(features.get("price_velocity_5m", 0.0))
    activity = float(features.get("activity_heat", 0.0)) / 100.0
    risk = max(float(features.get("token_volatility_5m", 0.0)) - 1.0, 0.0)
    entropy = float(features.get("unique_buyer_entropy_5m", 0.0))
    # Pool-centric: wallet prior weight reduced vs sender-gated binary gate
    final = 0.25 * wallet_prior + 0.30 * momentum + 0.25 * activity + 0.10 * entropy - 0.10 * risk
    return {
        "wallet_prior": round(wallet_prior, 4),
        "momentum": round(momentum, 4),
        "activity": round(activity, 4),
        "risk": round(risk, 4),
        "entropy": round(entropy, 4),
        "fusion_score": round(final, 6),
        "distinct_buyers": len(set(buyers)),
    }


def run_backtest(
    *,
    days: int = 14,
    horizon_sec: int = 1800,
    min_heat: int = 5,
    min_distinct_buyers: int = 2,
    min_fusion_score: float = 0.15,
    single_tick: bool = False,
) -> dict[str, Any]:
    tags = load_wallet_tags()
    prev = s91._USE_SINGLE_TICK
    s91._USE_SINGLE_TICK = single_tick

    onsets = s91.discover_historical_onsets(days, min_heat=min_heat)
    rows_out: list[dict] = []
    skipped = Counter()

    for o in onsets:
        pool = o["pool"]
        wts = int(o["onset_wts"])
        heat = int(o.get("onset_heat") or 0)
        if heat < min_heat:
            skipped["heat_low"] += 1
            continue
        if int(o.get("onset_lag", 999999)) > s91.ONSET_MAX:
            skipped["onset_lag"] += 1
            continue
        buyers = s91.onset_buyers(pool, wts)
        if len(set(buyers)) < min_distinct_buyers:
            skipped["cluster_gate"] += 1
            continue
        feats = pool_onset_features(pool, wts)
        scored = fusion_score(pool, buyers, feats, tags)
        if scored["fusion_score"] < min_fusion_score:
            skipped["fusion_low"] += 1
            continue
        ret = s91.forward_tick_return(pool, wts, horizon_sec=horizon_sec)
        if ret is None:
            skipped["no_forward"] += 1
            continue
        rows_out.append({
            "signal_time": datetime.fromtimestamp(wts, tz=timezone.utc).isoformat(),
            "pool": pool,
            "onset_heat": heat,
            "distinct_buyers": scored["distinct_buyers"],
            "wallet_prior": scored["wallet_prior"],
            "fusion_score": scored["fusion_score"],
            "net_return_30m_tick": round(ret, 6),
            "gate_mode": "pool_fusion_pivot",
        })

    s91._USE_SINGLE_TICK = prev
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(
        OUT_CSV,
        rows_out,
        ["signal_time", "pool", "onset_heat", "distinct_buyers", "wallet_prior", "fusion_score", "net_return_30m_tick", "gate_mode"],
    )

    rets = [float(r["net_return_30m_tick"]) for r in rows_out]
    pf = s91._pf(rets)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "route": "pool_centric_fusion_pivot",
        "backtest_days": days,
        "horizon_min": horizon_sec // 60,
        "min_heat": min_heat,
        "min_distinct_buyers": min_distinct_buyers,
        "min_fusion_score": min_fusion_score,
        "single_tick": single_tick,
        "onsets_scanned": len(onsets),
        "signals": len(rows_out),
        "skipped": dict(skipped),
        "pf_30m_tick": pf,
        "mean_return": sum(rets) / len(rets) if rets else None,
        "unique_pools": len(set(r["pool"] for r in rows_out)),
        "RESEARCH_ONLY": True,
        "guard_lifted": False,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Pivot pool-centric fusion paper shadow")
    p.add_argument("--backtest-days", type=int, default=14)
    p.add_argument("--horizon-min", type=int, default=30)
    p.add_argument("--min-heat", type=int, default=5)
    p.add_argument("--min-distinct-buyers", type=int, default=2)
    p.add_argument("--min-fusion-score", type=float, default=0.15)
    p.add_argument("--single-tick", action="store_true")
    args = p.parse_args()
    summary = run_backtest(
        days=args.backtest_days,
        horizon_sec=args.horizon_min * 60,
        min_heat=args.min_heat,
        min_distinct_buyers=args.min_distinct_buyers,
        min_fusion_score=args.min_fusion_score,
        single_tick=args.single_tick,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
