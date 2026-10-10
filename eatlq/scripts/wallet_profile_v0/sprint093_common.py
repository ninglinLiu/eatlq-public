#!/usr/bin/env python3
"""
Sprint093 shared helpers.

Both Sprint093-A (full Mode-A restoration with XGB Whale) and Sprint093-B
(independent Phase 6 label loop) consume the same candidate stream — the
sprint092 v3 alt-pool universe filtered by:
  - base-routing target exclusion (whitepaper-faithful)
  - Quoter V2 honeypot eth_call

This module owns:
  1. Discovery from sprint092 universe expander (delegated).
  2. Quoter V2 honeypot pre-filter (delegated).
  3. 5-minute forward tick / slot0 replay for Phase 6 label.
  4. Whitepaper Phase 1 wallet features (per author's own write-up):
       win_rate_30d (default 0.5), avg_roi (default 0.0),
       price_velocity_5m, buy_pressure_ratio, is_whale.
  5. Pre-signal 5m feature window (USD-normalized, target-aware).

DOES NOT lift FAILED_NO_7D_ALPHA. RESEARCH_ONLY for both A and B.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402
from sprint092_universe_expander import (  # noqa: E402
    ETH_USD,
    WETH, USDC, USDBC, CBBTC, BASE_ROUTING_TOKENS,
    WHALE_USD_THRESH,
    check_quoter_v2_sell_path,
    discover_whale_buy_events,
    fetch_5m_feature_window,
    tick_delta_to_return,
)
from sprint092_null_overlay_strategy import (  # noqa: E402
    compute_features_usd_normalized,
)

log = logging.getLogger("sprint093_common")

PHASE1_DEFAULT_WIN_RATE_30D = 0.5
PHASE1_DEFAULT_AVG_ROI = 0.0


def fetch_candidates(
    *,
    days: int,
    max_pool_age_hours: int,
    lowest_thresh: int,
    rpc_url: str | None = None,
    apply_quoter_v2: bool = True,
) -> list[dict[str, Any]]:
    """Discover candidates and optionally apply Quoter V2 honeypot pre-filter."""
    log.info(
        "discover: days=%d max_pool_age_h=%d lowest_thresh=$%d",
        days, max_pool_age_hours, lowest_thresh,
    )
    cands = discover_whale_buy_events(
        days=days,
        max_pool_age_hours=max_pool_age_hours,
        whale_usd_thresh=lowest_thresh,
    )
    log.info("base discovery: %d candidates", len(cands))

    if apply_quoter_v2:
        if not rpc_url:
            rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL", "")
        if not rpc_url:
            raise RuntimeError("Quoter V2 requested but no RPC URL available")
        quoter_cache: dict[str, dict[str, Any]] = {}
        kept: list[dict[str, Any]] = []
        for c in cands:
            tgt = c.get("target_address", "")
            tgt_dec = int(c.get("target_decimals", 18))
            if not tgt:
                continue
            cache_key = f"{tgt}|{tgt_dec}"
            if cache_key in quoter_cache:
                hp = quoter_cache[cache_key]
            else:
                hp = check_quoter_v2_sell_path(tgt, tgt_dec, rpc_url)
                quoter_cache[cache_key] = hp
                log.info(
                    "quoter %s dec=%d → ok=%s out=%d reason=%s",
                    tgt[:10], tgt_dec, hp.get("ok"), hp.get("amount_out"), hp.get("reason"),
                )
            c["quoter_v2_ok"] = bool(hp.get("ok"))
            c["quoter_v2_fee_tier"] = int(hp.get("fee_tier_used") or -1)
            c["quoter_v2_amount_out"] = int(hp.get("amount_out") or 0)
            if hp.get("ok"):
                kept.append(c)
        log.info("after Quoter V2: %d candidates (kept) / %d total", len(kept), len(cands))
        return kept
    return cands


def fetch_5m_forward_tick(pool: str, signal_ts: int, *, lookahead_sec: int = 300) -> dict[str, Any]:
    """Phase 6 forward replay: tick of the last swap at/before signal_ts+lookahead_sec.

    Returns: {forward_tick: int|None, forward_ts: int|None, n_swaps_in_window: int}
    """
    start_dt = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(signal_ts + lookahead_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT
        tick,
        toUnixTimestamp(block_time) AS ts,
        count() OVER () AS n_swaps_in_window
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >  toDateTime('{start_dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
    ORDER BY block_number DESC, log_index DESC
    LIMIT 1
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
    except Exception as exc:
        log.debug("forward_tick fail %s: %s", pool[:10], exc)
        return {"forward_tick": None, "forward_ts": None, "n_swaps_in_window": 0}
    if not rows:
        return {"forward_tick": None, "forward_ts": None, "n_swaps_in_window": 0}
    r = rows[0]
    return {
        "forward_tick": int(r["tick"]),
        "forward_ts": int(r["ts"]),
        "n_swaps_in_window": int(r.get("n_swaps_in_window") or 0),
    }


def fetch_wallet_phase1_features(
    sender: str,
    signal_ts: int,
    *,
    lookback_days: int = 30,
) -> dict[str, float]:
    """Phase 1 wallet history features per whitepaper §Phase 1.

    Whitepaper says: win_rate_30d & avg_roi were fixed at defaults (0.5, 0.0)
    during the Phase 1 synthetic bootstrap because real labels did not exist.
    We provide BOTH the whitepaper-strict defaults AND a "live" version computed
    from actual ClickHouse data so the readout can compare.

    Live version is NOT used for the v1_strict score path (which uses defaults),
    only for the v1_reconstruction_live variant.
    """
    start_dt = datetime.fromtimestamp(signal_ts - lookback_days * 86400, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT
        count() AS n_buys,
        uniqExact(pool_address) AS uniq_pools,
        avg(abs(amount0) + abs(amount1)) AS avg_raw
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {CHAIN_ID}
      AND lower(buyer_address) = '{sender.lower()}'
      AND block_time >  toDateTime('{start_dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
    except Exception:
        return {
            "win_rate_30d_default": PHASE1_DEFAULT_WIN_RATE_30D,
            "avg_roi_default": PHASE1_DEFAULT_AVG_ROI,
            "n_buys_30d": 0,
            "uniq_pools_30d": 0,
        }
    if not rows:
        return {
            "win_rate_30d_default": PHASE1_DEFAULT_WIN_RATE_30D,
            "avg_roi_default": PHASE1_DEFAULT_AVG_ROI,
            "n_buys_30d": 0,
            "uniq_pools_30d": 0,
        }
    r = rows[0]
    return {
        "win_rate_30d_default": PHASE1_DEFAULT_WIN_RATE_30D,
        "avg_roi_default": PHASE1_DEFAULT_AVG_ROI,
        "n_buys_30d": int(r.get("n_buys") or 0),
        "uniq_pools_30d": int(r.get("uniq_pools") or 0),
    }


def attach_5m_pre_features(candidate: dict[str, Any]) -> dict[str, Any]:
    """Attach pre-signal 5m USD-normalized features to a candidate."""
    rows = fetch_5m_feature_window(
        pool=candidate["pool"],
        signal_ts=int(candidate["signal_ts"]),
        token0=candidate["token0"],
        token1=candidate["token1"],
        token0_dec=int(candidate["token0_decimals"]),
        token1_dec=int(candidate["token1_decimals"]),
        quote_side=int(candidate.get("quote_side", -1)),
    )
    features = compute_features_usd_normalized(rows)
    candidate["features_5m"] = features
    return candidate


def attach_5m_forward_label(
    candidate: dict[str, Any],
    *,
    threshold_pct: float = 0.0,
) -> dict[str, Any]:
    """Attach 5m forward label using tick delta.

    label = 1 if 5m forward return > threshold_pct, else 0.
    threshold_pct = 0.0 → simple up/down direction label.
    """
    fwd = fetch_5m_forward_tick(candidate["pool"], int(candidate["signal_ts"]))
    entry_tick = float(candidate.get("tick") or 0)
    direction = int(candidate.get("direction", 0))
    if fwd["forward_tick"] is None or direction == 0:
        candidate["label_5m"] = {
            "forward_tick": None,
            "forward_return_pct": None,
            "label": None,
            "n_swaps_in_window": fwd.get("n_swaps_in_window", 0),
            "skip_reason": "no_forward_swap" if fwd["forward_tick"] is None else "no_direction",
        }
        return candidate
    delta_tick = float(fwd["forward_tick"]) - entry_tick
    raw_ret = tick_delta_to_return(delta_tick, direction)
    label = 1 if raw_ret > threshold_pct else 0
    candidate["label_5m"] = {
        "forward_tick": fwd["forward_tick"],
        "forward_ts": fwd["forward_ts"],
        "delta_tick": delta_tick,
        "forward_return_pct": raw_ret,
        "label": label,
        "n_swaps_in_window": fwd.get("n_swaps_in_window", 0),
        "threshold_pct": threshold_pct,
        "skip_reason": None,
    }
    return candidate


__all__ = [
    "fetch_candidates",
    "fetch_5m_forward_tick",
    "fetch_wallet_phase1_features",
    "attach_5m_pre_features",
    "attach_5m_forward_label",
    "PHASE1_DEFAULT_WIN_RATE_30D",
    "PHASE1_DEFAULT_AVG_ROI",
]
