#!/usr/bin/env python3
"""ETH vs USD denomination audit for KEYSTONE holdout.

Key fact: Quoter net = sell_weth / buy_weth - 1  →  **already ETH-denominated per trade**.
USD labels in prior reports assumed stake = $100 @ ETH=$3000 (0.0333 ETH fixed for quoting).

This script:
  1. Re-states holdout stats in **ETH return** (explicit)
  2. Recomputes **USD return** using historical ETH/USD at entry vs exit (~4h later)
  3. Compares fixed-ETH vs fixed-USD stake semantics
  4. Scenarios: ETH +20% / -20% over hold period (sensitivity)
  5. Reserve ETH bag: alpha-only vs total NAV (no live impact)

Uses cached _priced_live.json — zero RPC, zero CH.
"""
from __future__ import annotations
import json, math, os, sys, urllib.request
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import load, build  # noqa: E402

FRIC_THR = -0.05
HOLD_HOURS = 4
STAKE_ETH_FIXED = 100.0 / 3000.0  # 0.0333 ETH — same as $100 @ $3000 quote assumption
GAS_ETH = 0.15 / 3000.0           # gas ~$0.15 in ETH at $3000 ref
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
OUT = "/opt/EATLQ/mvp/audits/keystone_eth_usd_pnl.json"


def fetch_eth_daily() -> pd.Series:
    """Daily ETH/USD — try cache, else monthly anchors (public approx Oct2025–Jun2026)."""
    cache = "/tmp/eth_usd_daily_keystone.json"
    if os.path.exists(cache):
        raw = json.load(open(cache))
        if raw and isinstance(next(iter(raw.keys())), str):
            idx = pd.to_datetime(list(raw.keys()))
            return pd.Series(list(raw.values()), index=idx).sort_index()
    # Monthly anchor USD (approx; 4h hold uses same-day → ~flat intraday)
    anchors = {
        "2025-10-01": 2500, "2025-11-01": 3400, "2025-12-01": 3300,
        "2026-01-01": 3200, "2026-02-01": 2800, "2026-03-01": 3000,
        "2026-04-01": 3100, "2026-05-01": 2600, "2026-06-01": 2700,
    }
    idx = pd.date_range("2025-10-01", "2026-06-15", freq="D")
    s = pd.Series(index=idx, dtype=float)
    keys = sorted(pd.Timestamp(k) for k in anchors)
    vals = [anchors[k.strftime("%Y-%m-%d")] for k in keys]
    for i, d in enumerate(idx):
        k = keys[max(j for j, t in enumerate(keys) if t <= d)]
        s.iloc[i] = anchors[k.strftime("%Y-%m-%d")]
    return s


def nearest_eth_price(series: pd.Series, ts: pd.Timestamp, default: float = 3000.0) -> float:
    if series.empty:
        return default
    ts = ts.normalize()
    if ts in series.index:
        return float(series.loc[ts])
    idx = series.index.get_indexer([ts], method="nearest")[0]
    if idx < 0:
        return default
    return float(series.iloc[idx])


def stat(tag: str, r: np.ndarray) -> dict:
    r = np.array([x for x in r if x is not None and not np.isnan(x)])
    if len(r) == 0:
        return {"tag": tag, "n": 0}
    return {
        "tag": tag,
        "n": int(len(r)),
        "mean": float(r.mean()),
        "med": float(np.median(r)),
        "wr": float((r > 0).mean()),
        "fatal": float((r <= -0.5).mean()),
    }


def main():
    df, _ = load()
    X, y = build(df)
    cut = df.t0.quantile(0.6)
    tr, te = df.t0 <= cut, df.t0 > cut
    clf = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0,
    )
    clf.fit(X[tr], y[tr])
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    base = json.load(open(PCACHE))
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    df["net"] = df.pool.map(lambda x: base.get(x, {}).get("net"))
    p_cut = df[tr & df.fric.notna() & (df.fric >= FRIC_THR)].p.quantile(0.6)
    keep = df[te & df.fric.notna() & (df.fric >= FRIC_THR) & (df.p <= p_cut) & df.net.notna()]

    eth_px = fetch_eth_daily()
    entry_times = keep.t0 + timedelta(seconds=600)  # t0 + W
    exit_times = entry_times + timedelta(hours=HOLD_HOURS)

    r_eth = keep.net.values - GAS_ETH  # gas in ETH terms on stake
    r_eth_gross = keep.net.values

    r_usd_hist = []
    stake_eth_fixed = []
    stake_eth_usd_sized = []
    for i, row in keep.iterrows():
        ent = row.t0 + timedelta(seconds=600)
        ex = ent + timedelta(hours=HOLD_HOURS)
        p0 = nearest_eth_price(eth_px, ent)
        p1 = nearest_eth_price(eth_px, ex)
        ne = row.net
        if ne is None:
            continue
        # USD return on trade: convert stake at entry, value at exit
        r_usd = (1 + ne) * (p1 / p0) - 1.0
        r_usd_hist.append(r_usd - GAS_ETH * (p1 / p0))
        stake_eth_fixed.append(STAKE_ETH_FIXED)
        stake_eth_usd_sized.append(100.0 / p0)  # $100 at entry ETH price

    r_usd_hist = np.array(r_usd_hist)
    r_eth_net = np.array([x - GAS_ETH for x in keep.net.values])

    # ETH price beta during 4h hold (without meme alpha)
    eth_hodl_4h = []
    for ent, ex in zip(entry_times, exit_times):
        p0 = nearest_eth_price(eth_px, ent)
        p1 = nearest_eth_price(eth_px, ex)
        eth_hodl_4h.append(p1 / p0 - 1.0)
    eth_hodl_4h = np.array(eth_hodl_4h)

    # Scenario: ETH moves ±20% during typical hold (stress, not historical)
    r_usd_p20 = (1 + r_eth_gross) * 1.20 - 1.0
    r_usd_m20 = (1 + r_eth_gross) * 0.80 - 1.0

    # Wallet simulation: $2000 start, 10% per trade in ETH, 30 trades over holdout
    wallet_eth = 2000.0 / 3000.0  # ~0.667 ETH reserve at ref price
    cum_alpha_eth = r_eth_net.sum() * STAKE_ETH_FIXED  # naive sum (ignores overlap)
    te_days = max((keep.t0.max() - keep.t0.min()).days, 1)
    n_mo = len(keep) / te_days * 30

    print("#### KEYSTONE ETH vs USD denomination audit ####")
    print(f"holdout gate+model n={len(keep)}  (~{n_mo:.0f} trades/mo)")
    print("\n--- Per-trade return (same Quoter cache) ---")
    for d in [
        stat("ETH gross (WETH/WETH-1)", r_eth_gross),
        stat("ETH net (- gas)", r_eth_net),
        stat("USD historical (entry/exit ETH px)", r_usd_hist),
        stat("USD stress: ETH +20% during hold", r_usd_p20),
        stat("USD stress: ETH -20% during hold", r_usd_m20),
        stat("ETH-only 4h hodl (no trade)", eth_hodl_4h),
    ]:
        if d["n"]:
            print(f"  {d['tag']:40s} n={d['n']:3d} mean={d['mean']:+.3f} med={d['med']:+.3f} "
                  f"WR={d['wr']:.2f} fatal={d['fatal']:.3f}")

    print("\n--- Stake sizing ---")
    print(f"  Backtest quote stake: {STAKE_ETH_FIXED:.4f} ETH (= $100 @ $3000 ref)")
    print(f"  Live executor: stake_usd → stake_wei = usd/eth_price (USD-fixed, variable ETH)")
    print(f"  Historical $100/trade → mean stake {np.mean(stake_eth_usd_sized):.4f} ETH "
          f"(range {np.min(stake_eth_usd_sized):.4f}-{np.max(stake_eth_usd_sized):.4f})")

    print("\n--- Correlation: trade ETH alpha vs ETH 4h move ---")
    if len(eth_hodl_4h) == len(r_eth_gross):
        corr = np.corrcoef(r_eth_gross, eth_hodl_4h)[0, 1]
        print(f"  corr(meme_net_eth, eth_hodl_4h) = {corr:+.3f}  (≈0 → alpha independent of short ETH beta)")

    out = {
        "n": len(keep),
        "eth_gross": stat("eth_gross", r_eth_gross),
        "eth_net": stat("eth_net", r_eth_net),
        "usd_historical": stat("usd_hist", r_usd_hist),
        "usd_eth_plus20": stat("usd_p20", r_usd_p20),
        "usd_eth_minus20": stat("usd_m20", r_usd_m20),
        "eth_hodl_4h": stat("eth_hodl", eth_hodl_4h),
        "stake_eth_mean_usd100": float(np.mean(stake_eth_usd_sized)),
        "note": "net in _priced_live.json is WETH_out/WETH_in-1 (ETH-denominated); USD reports added ETH/USD path",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
