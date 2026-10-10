"""PIT features at/before whale_buy_t + real V3 tick/liquidity depth (v2).

Lookahead rules:
  - relative size, depth, pool structure: data with ts < whale_buy_t
  - absorption: uses [whale_buy_t, whale_buy_t+W] — known only at signal_t
  - NEVER used for entry before signal_t
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import load_freeze

LN1P = float(np.log(1.0001))


def within_tick_impact_frac(liquidity: float, price_weth: float, clip_weth: float) -> float:
    L = max(float(liquidity), 1.0)
    px = max(float(price_weth), 1e-30)
    sqrtP = np.sqrt(px)
    d_sqrt = clip_weth / L
    if sqrtP <= 0:
        return 1.0
    frac = abs(2.0 * d_sqrt / sqrtP)
    return float(min(frac, 5.0))


def weth_to_move_price(liquidity: float, price_weth: float, move_frac: float = 0.01) -> float:
    L = max(float(liquidity), 1.0)
    px = max(float(price_weth), 1e-30)
    sqrtP = np.sqrt(px)
    d_sqrt = move_frac * sqrtP / 2.0
    return float(max(d_sqrt * L, 0.0))


def attach_depth_features(buys: pd.DataFrame, eth_usd: float) -> pd.DataFrame:
    d = buys.copy()
    liq = pd.to_numeric(d.get("liquidity", 0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
    # liquidity may be hex string
    if liq.dtype == object or (hasattr(d["liquidity"], "dtype") and str(d["liquidity"].dtype) == "object"):
        def _liq(x):
            try:
                if isinstance(x, str) and x.startswith("0x"):
                    return float(int(x, 16))
                return float(x)
            except Exception:
                return 0.0
        liq = d["liquidity"].map(_liq).to_numpy(dtype=float)
    px = pd.to_numeric(d.get("price_weth", 0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
    freeze = load_freeze()
    clips = freeze["depth"]["clips_usd"]
    move = float(freeze["depth"]["price_move_for_depth_weth"])
    impact_25 = np.zeros(len(d))
    impact_100 = np.zeros(len(d))
    weth_1pct = np.zeros(len(d))
    c25 = clips[0] / eth_usd
    c100 = clips[1] / eth_usd if len(clips) > 1 else 100.0 / eth_usd
    for i in range(len(d)):
        impact_25[i] = within_tick_impact_frac(liq[i], px[i], c25)
        impact_100[i] = within_tick_impact_frac(liq[i], px[i], c100)
        weth_1pct[i] = weth_to_move_price(liq[i], px[i], move)
    d["depth_impact_25usd"] = impact_25
    d["depth_impact_100usd"] = impact_100
    d["depth_weth_to_move_1pct"] = weth_1pct
    d["depth_score"] = (
        np.log1p(pd.Series(weth_1pct).clip(lower=0)).rank(pct=True).to_numpy()
        * (1.0 - pd.Series(impact_100).clip(0, 1).rank(pct=True).to_numpy())
    )
    d["depth_proxy_log_liq"] = np.log1p(np.clip(liq, 0, None))
    return d


def _absorption_for_window(ts: np.ndarray, px: np.ndarray, absorb_w: int) -> np.ndarray:
    n = len(ts)
    nr = np.zeros(n)
    for i in range(n):
        t_end = ts[i] + absorb_w
        j = i
        while j < n and ts[j] < t_end:
            j += 1
        if j < n and px[i] > 0:
            end_px = px[j]
            nr[i] = 1.0 if end_px >= px[i] * 0.99 else 0.0
        elif j > i and px[i] > 0:
            end_px = px[min(j - 1, n - 1)]
            nr[i] = 1.0 if end_px >= px[i] * 0.99 else 0.0
        else:
            nr[i] = 0.0
    return nr


def attach_relative_size_and_absorption(
    buys: pd.DataFrame,
    *,
    extra_absorb_windows: list[int] | None = None,
) -> pd.DataFrame:
    """PIT relative size (prior 30m) + absorption over primary + optional extra windows."""
    freeze = load_freeze()
    lookback = int(freeze["identity"]["whale_volume_lookback_seconds"])
    primary_w = int(freeze["signal_timing"]["absorption_window_seconds"])
    windows = sorted({primary_w, *(extra_absorb_windows or [])})
    d = buys.sort_values(["pool", "ts"]).copy()
    rel_parts, prior_parts = [], []
    abs_parts = {w: [] for w in windows}

    for _, g in d.groupby("pool", sort=False):
        ts = g["ts"].to_numpy(dtype=np.int64)
        qw = g["quote_weth"].to_numpy(dtype=float)
        px = g["price_weth"].to_numpy(dtype=float)
        n = len(g)
        left = 0
        ri = np.zeros(n)
        pv = np.zeros(n)
        for i in range(n):
            while ts[i] - ts[left] > lookback:
                left += 1
            window_vol = qw[left:i].sum()
            pv[i] = window_vol
            ri[i] = qw[i] / (window_vol + 1e-9)
        rel_parts.append(pd.Series(ri, index=g.index))
        prior_parts.append(pd.Series(pv, index=g.index))
        for w in windows:
            abs_parts[w].append(pd.Series(_absorption_for_window(ts, px, w), index=g.index))

    d["rel_size"] = pd.concat(rel_parts).sort_index()
    d["prior_30m_volume_weth"] = pd.concat(prior_parts).sort_index()
    for w in windows:
        col = f"absorbed_{w}s"
        d[col] = pd.concat(abs_parts[w]).sort_index()
    d["absorbed"] = d[f"absorbed_{primary_w}s"]
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
    """Full scoped buy universe — NO downsample. Primary absorb from freeze; extras for sensitivity."""
    freeze = load_freeze()
    u = freeze["universe"]
    extras = list(freeze.get("sensitivity_controls_diagnostic_only", {}).get("absorption_windows_seconds", []))
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
    buys = attach_relative_size_and_absorption(buys, extra_absorb_windows=extras)
    buys = attach_pool_only(buys)
    buys["whale_buy_t"] = buys["ts"].astype(np.int64)
    buys["actor_fields_used"] = 0
    return buys
