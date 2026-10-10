#!/usr/bin/env python3
"""Quantify how badly the static fee field mispriced fee revenue, and re-run the C1b
headline with the true per-day fee.

s07 recovered the true fee() step function per pool per panel day. Fee revenue is linear
in the fee rate holding volume fixed, and the realised volume already happened under the
true fee, so the correct historical fee revenue is just the volume-weighted true rate
instead of the static field:

    corr(pool, entry, T) = sum_d(vol_d * f_true_d) / (f_stored * sum_d vol_d)

applied over the position's hold window. net_true = net + fee_bps * (corr - 1).
"""

import json
import subprocess
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

OUT = "/root/base_condmm_r0/out"
POS = "/root/base_fee_tier_r0/out/stage3_pnl_50.json"


def ch(q, timeout=900):
    r = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--query", q], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[:3000])
    return r.stdout


def report_curve(curve, stored, ven, sym):
    print("=" * 104)
    print("POOLS WHOSE TRUE FEE DIFFERED FROM THE STORED FIELD ON >0 PANEL DAYS")
    print("=" * 104)
    print(f"{'pool':44s} {'venue':8s} {'sym':9s} {'stored':>7s} {'wrong_d':>8s} "
          f"{'mean_true':>9s} {'stored/true':>11s}")
    rows = []
    for pl, series in curve.items():
        vals = [v for v in series.values() if v is not None]
        if not vals:
            continue
        f0 = stored[pl]
        rows.append((pl, sum(1 for v in vals if v != f0), sum(vals) / len(vals), f0, len(vals)))
    rows.sort(key=lambda r: -r[1])
    n_wrong = 0
    for pl, wrong, mt, f0, n in rows:
        if wrong == 0:
            continue
        n_wrong += 1
        print(f"{pl:44s} {ven[pl]:8s} {sym[pl][:9]:9s} {f0:7d} {wrong:8d} "
              f"{mt:9.1f} {f0 / mt if mt else 0:11.2f}")
    tot_d = sum(r[4] for r in rows)
    bad_d = sum(r[1] for r in rows)
    print(f"\n{n_wrong}/{len(rows)} non-univ3 pools mispriced on >=1 day")
    print(f"pool-days mispriced: {bad_d}/{tot_d} ({100.0 * bad_d / tot_d:.1f}%)")
    return rows


def daily_vol():
    print("\npulling per-pool daily notional from edge.bars_1h_year_50 ...")
    q = """
SELECT pool, toDate(bar) AS d, sum(toFloat64(vol_weth)) AS v
FROM edge.bars_1h_year_50 GROUP BY pool, d FORMAT TabSeparated"""
    try:
        txt = ch(q)
    except RuntimeError as e:
        print(f"  vol_weth unavailable ({str(e)[:120]}); falling back to swap counts")
        q = """
SELECT pool, toDate(bar) AS d, sum(toFloat64(n_swaps)) AS v
FROM edge.bars_1h_year_50 GROUP BY pool, d FORMAT TabSeparated"""
        txt = ch(q)
    vol = defaultdict(dict)
    for line in txt.strip().split("\n"):
        if not line:
            continue
        p, d, v = line.split("\t")
        vol[p][d] = float(v)
    print(f"  pools with volume: {len(vol)}")
    return vol


def main():
    fc = json.load(open(f"{OUT}/fee_curve.json"))
    curve = fc["curve"]
    uni = [json.loads(l) for l in open(f"{OUT}/universe50.jsonl") if l.strip()]
    stored = {p["pool"]: int(p["fee"]) for p in uni}
    ven = {p["pool"]: p["venue"] for p in uni}
    sym = {p["pool"]: p["symbol"] for p in uni}

    report_curve(curve, stored, ven, sym)
    vol = daily_vol()

    pos = pd.DataFrame(json.load(open(POS)))
    pos["entry_d"] = pd.to_datetime(pos["entry"]).dt.date
    print(f"\npositions: {len(pos)}  pools: {pos['pool'].nunique()}")

    # correction factor per (pool, entry, T)
    keys = pos[["pool", "entry_d", "T"]].drop_duplicates()
    print(f"distinct (pool, entry, T) windows: {len(keys)}")

    corr = {}
    for pl, ed, T in keys.itertuples(index=False):
        ser = curve.get(pl)
        if ser is None:
            corr[(pl, ed, T)] = 1.0
            continue
        f0 = stored.get(pl)
        if not f0:
            corr[(pl, ed, T)] = 1.0
            continue
        pv = vol.get(pl, {})
        num = den = 0.0
        for k in range(int(T)):
            d = (ed + timedelta(days=k)).isoformat()
            ft = ser.get(d)
            if ft is None:
                continue
            w = pv.get(d, 0.0)
            if w <= 0:
                w = 1.0  # unweighted fallback so a zero-volume day still counts
            num += w * ft
            den += w
        corr[(pl, ed, T)] = (num / den) / f0 if den > 0 else 1.0

    pos["corr"] = [corr[(p, e, t)] for p, e, t in
                   zip(pos["pool"], pos["entry_d"], pos["T"])]
    pos["fee_bps_true"] = pos["fee_bps"] * pos["corr"]
    for col in ("net_cheap_bps", "net_strict_bps"):
        pos[col + "_true"] = pos[col] + pos["fee_bps"] * (pos["corr"] - 1.0)

    off = pos[np.abs(pos["corr"] - 1.0) > 0.01]
    print(f"\npositions with >1% fee mispricing: {len(off)}/{len(pos)} "
          f"({100.0 * len(off) / len(pos):.1f}%)")
    if len(off):
        print(f"  corr distribution: p05={off['corr'].quantile(.05):.3f} "
              f"med={off['corr'].median():.3f} p95={off['corr'].quantile(.95):.3f}")
        print(f"  fee_bps overstated by (median): "
              f"{(off['fee_bps'] - off['fee_bps_true']).median():.2f} bps")

    print("\n" + "=" * 104)
    print("HEADLINE IMPACT: mean net_cheap_bps, stored fee vs true fee")
    print("=" * 104)
    print(f"{'cell':38s} {'n':>7s} {'stored':>10s} {'true':>10s} {'delta':>9s}")
    for lbl, m in [("all positions", pos)] + \
                  [(f"T={t}", pos[pos["T"] == t]) for t in sorted(pos["T"].unique())]:
        if not len(m):
            continue
        a, b = m["net_cheap_bps"].mean(), m["net_cheap_bps_true"].mean()
        print(f"{lbl:38s} {len(m):7d} {a:10.2f} {b:10.2f} {b - a:9.2f}")

    print("\n" + "=" * 104)
    print("THE cbBTC CELL THAT CARRIED THE ONLY POSITIVE C1b RESULT")
    print("=" * 104)
    cb = pos[pos["symbol"] == "cbBTC"]
    print(f"{'pool':44s} {'ven':8s} {'n':>6s} {'stored_net':>11s} {'true_net':>10s} "
          f"{'med_true':>9s} {'corr':>6s}")
    for pl, g in cb.groupby("pool"):
        print(f"{pl:44s} {ven.get(pl, '?'):8s} {len(g):6d} "
              f"{g['net_cheap_bps'].mean():11.1f} {g['net_cheap_bps_true'].mean():10.1f} "
              f"{g['net_cheap_bps_true'].median():9.1f} {g['corr'].mean():6.3f}")

    keep = ["pool", "symbol", "entry", "T", "c", "fee_bps", "fee_bps_true", "corr",
            "il_bps", "net_cheap_bps", "net_cheap_bps_true", "net_strict_bps",
            "net_strict_bps_true"]
    pos[keep].to_json(f"{OUT}/pos_fee_corrected.json", orient="records")
    print(f"\nwrote {OUT}/pos_fee_corrected.json")


if __name__ == "__main__":
    main()
