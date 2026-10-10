#!/usr/bin/env python3
"""Kill test for the 8 pool x T cells that showed a positive bootstrap lower bound
after the fee correction.

Three cheap checks, in cost order, any one of which is disqualifying:

1. Multiplicity. lo>0 is a one-sided 2.5% test, so 446 cells yield ~11 positives under
   the null. Observing 8 is fewer than chance. Also run a sign-flip null to get the
   distribution of the max lower bound, which the per-cell CI cannot see.
2. Half-year stability, split fixed in GOAL.md at 2026-02-16.
3. Single-day concentration: what fraction of the cell's total net comes from its single
   best entry day. A cell carried by one day is one draw, not an edge.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_condmm_r0/src")
import s02_test as m

OUT = Path("/root/base_condmm_r0/out")
NSIM = 2000


def main():
    txt = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                          "--query", "SELECT pool, venue FROM edge.bars_universe_50 "
                                     "FORMAT TabSeparated"],
                         capture_output=True, text=True, timeout=300).stdout
    m.VENUE = dict(l.split("\t") for l in txt.rstrip("\n").split("\n") if l)

    pos, pan, meta = m.load()
    prev = json.loads((OUT / "rerun_corrected.json").read_text())
    sweep = pd.DataFrame(prev["sweep"])

    corr = pd.Series(
        {(r["pool"], r["T"]): r["corr"] for _, r in sweep.iterrows()})
    pos["corr"] = [corr.get((p, t), 1.0) for p, t in zip(pos["pool"], pos["T"])]
    pos["net_strict_bps_true"] = pos["net_strict_bps"] + pos["fee_bps"] * (pos["corr"] - 1)
    m.OUTCOME = "net_strict_bps_true"

    surv = sweep[sweep["lo"] > 0].sort_values("lo", ascending=False)
    print(f"cells tested = {len(sweep)}   cells with lo>0 = {len(surv)}")
    print(f"expected under null at one-sided 2.5% = {0.025 * len(sweep):.1f}")

    # ---- 1. sign-flip null for the MAX lower bound across all cells
    rng = np.random.default_rng(7)
    cells = []
    for r in sweep.itertuples(index=False):
        g = pos[(pos["pool"] == r.pool) & (pos["T"] == r.T)]
        s, n = m.day_blocks(g)
        cells.append((s, n))

    print(f"\nsign-flip null over {len(cells)} cells x {NSIM} sims ...")
    max_lo = np.empty(NSIM)
    n_pos = np.empty(NSIM, int)
    for i in range(NSIM):
        los = []
        for s, n in cells:
            k = len(s)
            sgn = rng.choice((-1.0, 1.0), size=k)
            sf = s * sgn
            idx = rng.integers(0, k, size=(200, k))
            means = sf[idx].sum(1) / np.maximum(n[idx].sum(1), 1e-12)
            los.append(np.percentile(means, 2.5))
        los = np.array(los)
        max_lo[i] = los.max()
        n_pos[i] = int((los > 0).sum())

    obs_max = float(surv["lo"].max())
    obs_npos = int(len(surv))
    p_max = float((max_lo >= obs_max).mean())
    p_npos = float((n_pos >= obs_npos).mean())
    print(f"  observed max lower bound = {obs_max:.1f}   "
          f"null p05/p50/p95 = {np.percentile(max_lo, 5):.1f}/"
          f"{np.percentile(max_lo, 50):.1f}/{np.percentile(max_lo, 95):.1f}   "
          f"p = {p_max:.4f}")
    print(f"  observed count lo>0 = {obs_npos}   "
          f"null p50 = {np.percentile(n_pos, 50):.1f}   p = {p_npos:.4f}")

    # ---- 2 and 3. per-survivor stability and concentration
    print("\n" + "=" * 112)
    print("PER-SURVIVOR KILL CHECKS")
    print("=" * 112)
    print(f"{'symbol':9s} {'ven':8s} {'T':>2s} {'n':>5s} {'mean':>8s} {'lo':>7s} "
          f"{'h1':>8s} {'h2':>8s} {'same':>5s} {'top1day%':>9s} {'top3day%':>9s} {'days':>5s}")
    rows = []
    for r in surv.itertuples(index=False):
        g = pos[(pos["pool"] == r.pool) & (pos["T"] == r.T)]
        by_day = g.groupby("entry")[m.OUTCOME].sum()
        tot = by_day.sum()
        srt = by_day.sort_values(ascending=False)
        top1 = float(srt.iloc[0] / tot) if tot else np.nan
        top3 = float(srt.iloc[:3].sum() / tot) if tot else np.nan
        h1 = g[g["entry"] < m.SPLIT]
        h2 = g[g["entry"] >= m.SPLIT]
        m1 = h1[m.OUTCOME].mean() if len(h1) else np.nan
        m2 = h2[m.OUTCOME].mean() if len(h2) else np.nan
        same = bool(np.isfinite(m1) and np.isfinite(m2) and np.sign(m1) == np.sign(m2))
        print(f"{r.symbol[:9]:9s} {r.venue:8s} {r.T:2d} {r.n:5d} {r.mean:8.1f} "
              f"{r.lo:7.1f} {m1:8.1f} {m2:8.1f} {str(same):>5s} "
              f"{100 * top1:9.1f} {100 * top3:9.1f} {len(by_day):5d}")
        rows.append({"pool": r.pool, "symbol": r.symbol, "venue": r.venue, "T": int(r.T),
                     "n": int(r.n), "mean": r.mean, "lo": r.lo, "h1": m1, "h2": m2,
                     "same_sign": same, "top1_day_share": top1, "top3_day_share": top3,
                     "entry_days": int(len(by_day))})

    surviving = [r for r in rows if r["same_sign"] and r["top3_day_share"] < 0.5]
    print(f"\nsurvivors passing BOTH half-year stability and top-3-day < 50%: "
          f"{len(surviving)}")
    for r in surviving:
        print(f"  {r['symbol']} {r['venue']} T={r['T']} mean={r['mean']:.1f} "
              f"lo={r['lo']:.1f}")

    json.dump({"multiplicity": {"cells": len(sweep), "obs_lo_pos": obs_npos,
                                "expected_null": 0.025 * len(sweep),
                                "obs_max_lo": obs_max, "p_max": p_max,
                                "p_count": p_npos},
               "survivors": rows, "passing": surviving},
              open(OUT / "survivor_kill.json", "w"), indent=1, default=str)
    print(f"\nwrote {OUT / 'survivor_kill.json'}")


if __name__ == "__main__":
    main()
