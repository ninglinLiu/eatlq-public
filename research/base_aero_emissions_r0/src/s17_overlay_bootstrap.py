#!/usr/bin/env python3
"""Day-block bootstrap + leave-one-symbol-out on the trailing-emission selected T=7 book.

s16 showed mean net+em = +4.7, median +91, h1/h2 opposite signs, PING carrying 223% of
the book. This file only asks: does the CI clear zero, and does any symbol-out book stay
positive. Same seed and bootstrap as C1b (s02_test).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_condmm_r0/src")
import s02_test as m

sys.path.insert(0, "/root/base_aero_emissions_r0/src")
import s16_overlay_and_calendar as s16

OUT = Path("/root/base_aero_emissions_r0/out")


def main():
    apr = s16.load_apr()
    pos = pd.DataFrame(json.loads(s16.POS.read_text()))
    uni = [json.loads(l) for l in s16.UNI.read_text().splitlines() if l.strip()]
    venue = {p["pool"]: p["venue"] for p in uni}
    pos["venue"] = pos["pool"].map(venue).fillna("unknown")
    pos["entry_d"] = pd.to_datetime(pos["entry"]).dt.date

    corr = pd.DataFrame(json.loads(s16.CORR.read_text()))
    pos = pos.merge(
        corr[["pool", "entry", "T", "c", "net_strict_bps_true"]],
        on=["pool", "entry", "T", "c"], how="left",
    )
    pos["net"] = pos["net_strict_bps_true"].fillna(pos["net_strict_bps"])

    prev = []
    wr = []
    for pl, ed in zip(pos["pool"], pos["entry_d"]):
        ep = s16.epoch_of(ed)
        rec = apr.get(pl, {}).get(str(ep))
        wr.append(rec["wr"] if rec else np.nan)
        prec = apr.get(pl, {}).get(str(ep - timedelta(days=7)))
        prev.append(prec["wr"] if prec else np.nan)
    pos["wr"] = wr
    pos["wr_lag"] = prev
    pos["em_bps"] = pos["wr"] * 1e4 * (pos["T"] / 7.0)
    pos["net_em"] = pos["net"] + pos["em_bps"].fillna(0.0)

    t7 = pos[(pos["venue"] == "aero_cl") & (pos["T"] == 7)].copy()
    keys = t7[["entry", "pool", "wr_lag"]].drop_duplicates()
    keys = keys[keys["wr_lag"].notna()]
    keys["pct"] = keys.groupby("entry")["wr_lag"].rank(pct=True, method="average")
    sel = t7.merge(keys.loc[keys["pct"] >= 0.75, ["entry", "pool"]], on=["entry", "pool"])
    print(f"selected {len(sel)} rows, {sel['entry'].nunique()} days, {sel['symbol'].nunique()} symbols")

    m.OUTCOME = "net_em"
    m.RNG = np.random.default_rng(20260819)
    s, n = m.day_blocks(sel)
    r = m.boot_ci(s, n)
    print(f"day-block mean={r['mean']:.2f}  95% [{r['lo']:.2f}, {r['hi']:.2f}]  "
          f"blocks={r['blocks']}")

    m.RNG = np.random.default_rng(20260819)
    s0, n0 = m.day_blocks(sel.assign(_x=sel["net"]).rename(columns={"_x": "net"}))
    # bootstrap without emissions for contrast
    m.OUTCOME = "net"
    m.RNG = np.random.default_rng(20260819)
    s1, n1 = m.day_blocks(sel)
    r1 = m.boot_ci(s1, n1)
    print(f"same book, no emissions  mean={r1['mean']:.2f}  95% [{r1['lo']:.2f}, {r1['hi']:.2f}]")

    m.OUTCOME = "net_em"
    print("\nleave-one-symbol-out:")
    worst = None
    for sym in sorted(sel["symbol"].unique()):
        g = sel[sel["symbol"] != sym]
        m.RNG = np.random.default_rng(20260819)
        s2, n2 = m.day_blocks(g)
        r2 = m.boot_ci(s2, n2)
        if worst is None or r2["lo"] < worst[0]:
            worst = (r2["lo"], r2["mean"], r2["hi"], sym)
        if r2["lo"] > 0 or r2["hi"] < 0 or sym in {"PING", "CLANKER", "GAME", "noice", "POKT"}:
            print(f"  drop {sym:10s}  n={len(g):5d}  mean={r2['mean']:8.1f}  "
                  f"[{r2['lo']:8.1f}, {r2['hi']:8.1f}]")
    print(f"worst LOO: drop {worst[3]}  mean={worst[1]:.1f}  lo={worst[0]:.1f}")

    tot = sel.groupby("symbol")["net_em"].sum()
    share = (tot / tot.sum()).sort_values(ascending=False)
    print(f"\ntop symbol {share.index[0]} share of book net+em = {share.iloc[0]:.1%}")
    print(f"top 3 share = {share.iloc[:3].sum():.1%}")

    for name, mask in (("h1", sel["entry"] < m.SPLIT), ("h2", sel["entry"] >= m.SPLIT)):
        g = sel[mask]
        m.RNG = np.random.default_rng(20260819)
        s3, n3 = m.day_blocks(g)
        r3 = m.boot_ci(s3, n3)
        print(f"{name}: mean={r3['mean']:.1f} [{r3['lo']:.1f}, {r3['hi']:.1f}]  blocks={r3['blocks']}")

    json.dump({
        "n": int(len(sel)),
        "with_em": r,
        "without_em": r1,
        "worst_loo": {"symbol": worst[3], "lo": worst[0], "mean": worst[1], "hi": worst[2]},
        "top_share": float(share.iloc[0]),
        "top_symbol": str(share.index[0]),
    }, open(OUT / "overlay_bootstrap.json", "w"), indent=1, default=str)
    print(f"\nwrote {OUT / 'overlay_bootstrap.json'}")


if __name__ == "__main__":
    main()
