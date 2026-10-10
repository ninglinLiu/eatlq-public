"""PIT features at/before whale_buy_t + real V3 tick/liquidity depth.

Lookahead rules:
  - relative size, depth, pool structure: data with ts < whale_buy_t (strictly prior window)
  - absorption: uses [whale_buy_t, whale_buy_t+300] prices — known only at signal_t
  - NEVER used for entry before signal_t
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import load_freeze

LN1P = float(np.log(1.0001))


def within_tick_impact_frac(liquidity: float, price_weth: float, clip_weth: float) -> float:
    """Approximate within-tick Uniswap V3 price impact for a buy of clip_weth.

    Using ΔsqrtP ≈ Δx / L  (token0~quote approximation when quote is WETH on one side).
    Returns fractional price move estimate, capped.
    """
    L = max(float(liquidity), 1.0)
    px = max(float(price_weth), 1e-30)
    # sqrtP from price (token1/token0); we only need relative move
    sqrtP = np.sqrt(px)
    d_sqrt = clip_weth / L
    # rough: dP/P ≈ 2 * d_sqrt / sqrtP for small moves when price ~ token ratio
    if sqrtP <= 0:
        return 1.0
    frac = abs(2.0 * d_sqrt / sqrtP)
    return float(min(frac, 5.0))


def weth_to_move_price(liquidity: float, price_weth: float, move_frac: float = 0.01) -> float:
    """WETH needed to move price by move_frac under constant within-tick L."""
    L = max(float(liquidity), 1.0)
    px = max(float(price_weth), 1e-30)
    sqrtP = np.sqrt(px)
    # target: 2*d_sqrt/sqrtP ≈ move_frac => d_sqrt ≈ move_frac*sqrtP/2
    d_sqrt = move_frac * sqrtP / 2.0
    return float(max(d_sqrt * L, 0.0))


def attach_depth_features(buys: pd.DataFrame, eth_usd: float) -> pd.DataFrame:
    d = buys.copy()
    liq = pd.to_numeric(d.get("liquidity", 0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
    px = pd.to_numeric(d.get("price_weth", 0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
    freeze = load_freeze()
    clips = freeze["depth"]["clips_usd"]
    move = float(freeze["depth"]["price_move_for_depth_weth"])
    impact_25 = np.zeros(len(d))
    impact_100 = np.zeros(len(d))
    weth_1pct = np.zeros(len(d))
    for i in range(len(d)):
        c25 = clips[0] / eth_usd
        c100 = clips[1] / eth_usd if len(clips) > 1 else 100.0 / eth_usd
        impact_25[i] = within_tick_impact_frac(liq[i], px[i], c25)
        impact_100[i] = within_tick_impact_frac(liq[i], px[i], c100)
        weth_1pct[i] = weth_to_move_price(liq[i], px[i], move)
    d["depth_impact_25usd"] = impact_25
    d["depth_impact_100usd"] = impact_100
    d["depth_weth_to_move_1pct"] = weth_1pct
    # Real depth score: deeper = more WETH to move 1%, lower impact
    d["depth_score"] = (
        np.log1p(d["depth_weth_to_move_1pct"].clip(lower=0)).rank(pct=True)
        * (1.0 - d["depth_impact_100usd"].clip(0, 1).rank(pct=True))
    )
    # Legacy log-liquidity kept as diagnostic only — NOT used as depth control
    d["depth_proxy_log_liq"] = np.log1p(pd.to_numeric(d["liquidity"], errors="coerce").fillna(0).clip(lower=0))
    return d


def attach_relative_size_and_absorption(buys: pd.DataFrame) -> pd.DataFrame:
    """PIT relative size (prior 30m) + absorption over [t, t+300].

    Absorption is a *label of the buy event*, knowable only at t+300.
    """
    freeze = load_freeze()
    lookback = int(freeze["identity"]["whale_volume_lookback_seconds"])
    absorb_w = int(freeze["signal_timing"]["absorption_window_seconds"])
    d = buys.sort_values(["pool", "ts"]).copy()
    rel, nonrev, prior_vol = [], [], []
    for _, g in d.groupby("pool", sort=False):
        ts = g["ts"].to_numpy(dtype=np.int64)
        qw = g["quote_weth"].to_numpy(dtype=float)
        px = g["price_weth"].to_numpy(dtype=float)
        n = len(g)
        left = 0
        ri = np.zeros(n)
        nr = np.zeros(n)
        pv = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > lookback:
                left += 1
            # strictly prior volume: exclude current buy
            window_vol = qw[left:i].sum()
            pv[i] = window_vol
            ri[i] = qw[i] / (window_vol + 1e-9)
            # absorption: first price at/after t+absorb_w vs buy price
            t_end = ts[i] + absorb_w
            j = i
            while j < n and ts[j] < t_end:
                j += 1
            # need a print at or after absorb window end; if only intra-window, use last in window
            if j < n and px[i] > 0:
                # prefer first bar with ts >= t_end
                end_px = px[j]
                nr[i] = 1.0 if end_px >= px[i] * 0.99 else 0.0
            elif j > i and px[i] > 0:
                end_px = px[min(j - 1, n - 1)]
                nr[i] = 1.0 if end_px >= px[i] * 0.99 else 0.0
            else:
                nr[i] = 0.0  # unknown / no path → not absorbed
        rel.append(pd.Series(ri, index=g.index))
        nonrev.append(pd.Series(nr, index=g.index))
        prior_vol.append(pd.Series(pv, index=g.index))
    d["rel_size"] = pd.concat(rel).sort_index()
    d["absorbed"] = pd.concat(nonrev).sort_index()
    d["prior_30m_volume_weth"] = pd.concat(prior_vol).sort_index()
    d["large_buy_score"] = d["rel_size"].clip(0, 5)
    d["absorption_score"] = d["absorbed"]
    d["lba_score"] = d["large_buy_score"] * (0.5 + 0.5 * d["absorbed"])
    return d


def attach_pool_only(buys: pd.DataFrame) -> pd.DataFrame:
    d = buys.copy()
    age = pd.to_numeric(d["pool_age_s"], errors="coerce").fillna(0)
    d["age_bucket"] = pd.cut(
        age,
        bins=[-1, 900, 3600, 21600, 86400, 604800],
        labels=["0-15m", "15-60m", "1-6h", "6-24h", "1-7d"],
    )
    intens = []
    for _, g in d.groupby("pool", sort=False):
        ts = g["ts"].to_numpy()
        n = len(ts)
        left = 0
        vals = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > 1800:
                left += 1
            vals[i] = i - left
        intens.append(pd.Series(vals, index=g.index))
    d["buy_intensity_30m"] = pd.concat(intens).sort_index() if intens else 0.0
    first_px = d.groupby("pool")["price_weth"].transform("first")
    d["mom_from_open"] = np.where(first_px > 0, d["price_weth"] / first_px - 1.0, 0.0)
    d["pool_only_score"] = (
        0.25 * d["depth_score"].rank(pct=True)
        + 0.25 * d["buy_intensity_30m"].rank(pct=True)
        + 0.25 * (1.0 - d["pool_age_s"].rank(pct=True))
        + 0.25 * d["mom_from_open"].clip(-1, 5).rank(pct=True)
    )
    return d


def build_buy_universe(resolved: pd.DataFrame) -> pd.DataFrame:
    """Full scoped buy universe — NO downsample."""
    freeze = load_freeze()
    u = freeze["universe"]
    buys = resolved[
        (resolved["side"] == "buy")
        & (resolved["pool_age_s"] >= int(u["pool_age_min_s"]))
        & (resolved["pool_age_s"] < int(u["pool_age_max_s"]))
        & (resolved["quote_weth"] >= float(u["min_quote_weth"]))
    ].copy()
    buys = buys.sort_values(["pool", "ts"]).reset_index(drop=True)
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])
    print(f"[features] scoped buys (full): {len(buys):,}", flush=True)
    buys = attach_depth_features(buys, eth_usd)
    buys = attach_relative_size_and_absorption(buys)
    buys = attach_pool_only(buys)
    buys["whale_buy_t"] = buys["ts"].astype(np.int64)
    buys["actor_fields_used"] = 0  # order-flow, not identity
    return buys
