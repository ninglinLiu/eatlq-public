"""Leg A2: cross-pool PIT actor skill.

Pulls from eatlq.uniswap_v3_swaps_decoded (not the heavy resolved VIEW),
joins WETH pool meta in pandas, builds FIFO trips, asof to signals.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..common import OUT, ROOT, WETH, ch_query
from ..alpha.pit_actor_features import (
    build_closed_round_trips,
    _expanding_actor_stats,
    _asof_actor,
    _pool_buyers_agg,
)
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out

Q96 = 2.0 ** 96


def _load_pool_meta() -> pd.DataFrame:
    ensure_alpha_v2_out()
    cache = ALPHA_V2_OUT / "pool_meta_weth.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    meta = ch_query("""
    SELECT lower(pool_address) AS pool,
           lower(token0_address) AS token0,
           lower(token1_address) AS token1,
           token0_decimals AS d0,
           token1_decimals AS d1
    FROM eatlq.discovered_pools
    WHERE pool_type = 'v3' AND has_weth_leg = 1
    SETTINGS max_memory_usage = '4000000000'
    """, timeout=120)
    meta.to_parquet(cache, index=False)
    return meta


def _price_weth_from_sqrt(sp, token0, token1, d0, d1, target) -> np.ndarray:
    sp = np.asarray(sp, dtype=float)
    d0 = np.asarray(d0, dtype=float)
    d1 = np.asarray(d1, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        p1_per_0 = (sp / Q96) ** 2
        p1h_per_0h = p1_per_0 * np.power(10.0, d0 - d1)
    weth0 = token0 == WETH
    weth1 = token1 == WETH
    price_for_t0 = np.where(weth1, p1h_per_0h, np.where(weth0, 1.0 / np.maximum(p1h_per_0h, 1e-300), np.nan))
    price_for_t1 = np.where(weth0, 1.0 / np.maximum(p1h_per_0h, 1e-300), np.where(weth1, p1h_per_0h, np.nan))
    return np.where(target == token0, price_for_t0, np.where(target == token1, price_for_t1, np.nan))


def pull_xpool_swaps(*, cache: Path | None = None) -> pd.DataFrame:
    """Weekly pull of V3 swaps → WETH-pair buy/sell with price/quote."""
    ensure_alpha_v2_out()
    cache = cache or (ALPHA_V2_OUT / "xpool_swaps.parquet")
    if cache.exists():
        print(f"[alpha_v2/A2] loading cached xpool swaps {cache}", flush=True)
        return pd.read_parquet(cache)

    meta = _load_pool_meta()
    weth_pools = set(meta["pool"].astype(str))
    print(f"[alpha_v2/A2] WETH pools in meta={len(weth_pools):,}", flush=True)

    weeks = list(pd.date_range("2025-10-01", "2026-07-22", freq="W-MON", tz="UTC"))
    if not weeks or weeks[0] > pd.Timestamp("2025-10-01", tz="UTC"):
        weeks = [pd.Timestamp("2025-10-01", tz="UTC")] + weeks
    frames = []
    for i, start in enumerate(weeks):
        end = weeks[i + 1] if i + 1 < len(weeks) else pd.Timestamp("2026-07-22", tz="UTC")
        start_s = start.strftime("%Y-%m-%d %H:%M:%S")
        end_s = end.strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
        SELECT
          lower(recipient) AS economic_actor,
          lower(pool_address) AS pool,
          toString(amount0) AS a0,
          toString(amount1) AS a1,
          toString(sqrt_price_x96) AS sqrt_price_x96,
          toUnixTimestamp(block_time) AS ts
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE block_time >= toDateTime('{start_s}')
          AND block_time < toDateTime('{end_s}')
          AND recipient != ''
        SETTINGS max_memory_usage = '18000000000', max_threads = 4
        """
        try:
            chunk = ch_query(sql, timeout=600)
        except Exception as e:
            print(f"[alpha_v2/A2] week {start_s[:10]} fail: {str(e)[:160]}", flush=True)
            continue
        if chunk.empty:
            print(f"[alpha_v2/A2] week {start_s[:10]}: 0", flush=True)
            continue
        chunk["pool"] = chunk["pool"].astype(str).str.lower()
        chunk = chunk[chunk["pool"].isin(weth_pools)]
        print(f"[alpha_v2/A2] week {start_s[:10]}: raw→weth {len(chunk):,}", flush=True)
        if not chunk.empty:
            frames.append(chunk)

    if not frames:
        raise RuntimeError("No xpool swaps pulled")
    raw = pd.concat(frames, ignore_index=True)
    raw = raw.merge(meta, on="pool", how="inner")
    raw["economic_actor"] = raw["economic_actor"].astype(str).str.lower()
    raw["token0"] = raw["token0"].astype(str).str.lower()
    raw["token1"] = raw["token1"].astype(str).str.lower()
    raw["ts"] = pd.to_numeric(raw["ts"], errors="coerce").fillna(0).astype(np.int64)
    a0 = pd.to_numeric(raw["a0"], errors="coerce").fillna(0.0).to_numpy()
    a1 = pd.to_numeric(raw["a1"], errors="coerce").fillna(0.0).to_numpy()
    d0 = pd.to_numeric(raw["d0"], errors="coerce").fillna(18).to_numpy()
    d1 = pd.to_numeric(raw["d1"], errors="coerce").fillna(18).to_numpy()
    sp = pd.to_numeric(raw["sqrt_price_x96"], errors="coerce").fillna(0.0).to_numpy()
    t0 = raw["token0"].to_numpy()
    t1 = raw["token1"].to_numpy()

    # target = non-WETH token
    target = np.where(t0 == WETH, t1, np.where(t1 == WETH, t0, ""))
    raw["token"] = target
    # pool received WETH (>0 on WETH leg) ⇒ user bought target
    weth_delta = np.where(t0 == WETH, a0, np.where(t1 == WETH, a1, 0.0))
    raw["side"] = np.where(weth_delta > 0, "buy", np.where(weth_delta < 0, "sell", "unknown"))
    raw = raw[raw["side"].isin(["buy", "sell"]) & (raw["token"] != "") & (raw["token"] != WETH)].copy()

    t0 = raw["token0"].to_numpy()
    t1 = raw["token1"].to_numpy()
    tgt = raw["token"].to_numpy()
    a0 = pd.to_numeric(raw["a0"], errors="coerce").fillna(0.0).to_numpy()
    a1 = pd.to_numeric(raw["a1"], errors="coerce").fillna(0.0).to_numpy()
    d0 = pd.to_numeric(raw["d0"], errors="coerce").fillna(18).to_numpy()
    d1 = pd.to_numeric(raw["d1"], errors="coerce").fillna(18).to_numpy()
    sp = pd.to_numeric(raw["sqrt_price_x96"], errors="coerce").fillna(0.0).to_numpy()

    raw["price_weth"] = _price_weth_from_sqrt(sp, t0, t1, d0, d1, tgt)
    raw["quote_weth"] = np.where(
        t0 == WETH, np.abs(a0) / np.power(10.0, d0.astype(float)),
        np.abs(a1) / np.power(10.0, d1.astype(float)),
    )
    raw["pool_age_s"] = 0.0
    raw["entity_cluster"] = raw["economic_actor"]
    raw = raw[
        (raw["price_weth"] > 0) & np.isfinite(raw["price_weth"])
        & (raw["quote_weth"] > 0) & np.isfinite(raw["quote_weth"])
        & raw["economic_actor"].ne("") & raw["economic_actor"].ne("nan")
    ]
    out = raw[[
        "economic_actor", "token", "pool", "ts", "side",
        "quote_weth", "price_weth", "pool_age_s", "entity_cluster",
    ]].reset_index(drop=True)
    out.to_parquet(cache, index=False)
    print(f"[alpha_v2/A2] cached {len(out):,} xpool swaps → {cache}", flush=True)
    return out


def attach_actor_xpool_features(
    panel: pd.DataFrame,
    *,
    xpool: pd.DataFrame | None = None,
) -> pd.DataFrame:
    ensure_alpha_v2_out()
    d = panel.copy()
    d["_row"] = np.arange(len(d))
    d["economic_actor"] = d["economic_actor"].astype(str).str.lower()
    d["decision_time"] = d["decision_time"].astype(np.int64)
    d["pool"] = d["pool"].astype(str).str.lower()
    if "entity_cluster" not in d.columns:
        d["entity_cluster"] = d["economic_actor"]
    else:
        d["entity_cluster"] = d["entity_cluster"].astype(str).str.lower()

    if xpool is None:
        xpool = pull_xpool_swaps()

    trips_cache = ALPHA_V2_OUT / "actor_xpool_closed_trips.parquet"
    trips = build_closed_round_trips(xpool, cache_path=trips_cache, actors_filter=None)
    actor_stats = _expanding_actor_stats(trips)

    print(f"[alpha_v2/A2] asof-joining xpool stats onto {len(d):,} signals…", flush=True)
    a = _asof_actor(d, actor_stats)
    feat = d[["_row"]].merge(
        a[["_row", "n_trips", "win_rate", "avg_ret", "median_hold_s", "early_entry_skill"]],
        on="_row", how="left",
    )
    feat = feat.rename(columns={
        "n_trips": "actor_xpool_n_prior_trips",
        "win_rate": "actor_xpool_prior_win_rate",
        "avg_ret": "actor_xpool_prior_avg_ret",
        "median_hold_s": "actor_xpool_prior_median_hold_s",
        "early_entry_skill": "actor_xpool_early_entry_skill",
    })
    feat["actor_xpool_cold_start"] = feat["actor_xpool_n_prior_trips"].isna().astype(float)
    for c in [
        "actor_xpool_n_prior_trips", "actor_xpool_prior_win_rate", "actor_xpool_prior_avg_ret",
        "actor_xpool_prior_median_hold_s", "actor_xpool_early_entry_skill",
    ]:
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    print("[alpha_v2/A2] aggregating pool buyers (xpool)…", flush=True)
    pba = _pool_buyers_agg(d, xpool, actor_stats)
    pba = pba.rename(columns={
        "pool_buyers_mean_win_rate": "pool_buyers_xpool_mean_win_rate",
        "pool_buyers_mean_n_trips": "pool_buyers_xpool_mean_n_trips",
        "pool_buyers_n_with_history": "pool_buyers_xpool_n_with_history",
    })
    feat = feat.merge(pba, on="_row", how="left")
    for c in [
        "pool_buyers_xpool_mean_win_rate", "pool_buyers_xpool_mean_n_trips",
        "pool_buyers_xpool_n_with_history",
    ]:
        feat[c] = pd.to_numeric(feat.get(c, 0), errors="coerce").fillna(0.0)

    feat["actor_xpool_score"] = (
        (1.0 - feat["actor_xpool_cold_start"])
        * (
            feat["actor_xpool_prior_win_rate"]
            + 0.3 * feat["actor_xpool_prior_avg_ret"].clip(-1, 5)
            + 0.2 * feat["actor_xpool_early_entry_skill"]
            + 0.1 * np.log1p(feat["actor_xpool_n_prior_trips"])
        )
        + 0.5 * feat["pool_buyers_xpool_mean_win_rate"]
        + 0.1 * np.log1p(feat["pool_buyers_xpool_n_with_history"])
    )

    out_cols = [
        "actor_xpool_n_prior_trips", "actor_xpool_prior_win_rate", "actor_xpool_prior_avg_ret",
        "actor_xpool_prior_median_hold_s", "actor_xpool_early_entry_skill",
        "actor_xpool_cold_start",
        "pool_buyers_xpool_mean_win_rate", "pool_buyers_xpool_mean_n_trips",
        "pool_buyers_xpool_n_with_history", "actor_xpool_score",
    ]
    result = panel.copy()
    for c in out_cols:
        result[c] = feat[c].to_numpy()
    print(
        f"[alpha_v2/A2] cold_start={result['actor_xpool_cold_start'].mean():.1%} "
        f"mean_win={result.loc[result['actor_xpool_cold_start']==0, 'actor_xpool_prior_win_rate'].mean():.3f} "
        f"score>0={(result['actor_xpool_score']>0).mean():.1%}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].head(2000)  # noqa: E712
    attach_actor_xpool_features(elig)
