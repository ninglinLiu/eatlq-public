#!/usr/bin/env python3
"""Froth LONG, net of REAL 8h funding (Coinglass). A long PAYS funding when the
rate is positive -- worst exactly during froth. This subtracts the realized
funding over each trade's actual holding window from the gross price return.

Restricts to trades whose entry is within funding coverage (>= 2025-06-23).
Reports GROSS vs NET-of-funding, by froth bin, TRAIN/TEST, with robustness.
"""
from __future__ import annotations
import json, glob, os, random, statistics as st
from froth_long import (load, features, atr, sim_long_full, is_breakout,
                        is_rollover, bin_of)

FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
SPLIT = 0.6
CLIP = float(os.environ.get("FUND_CLIP", "0.02"))   # clip |8h funding| (data has -200% glitches)


def load_funding():
    f = {}
    for fn in glob.glob(f"{FUND}/*.json"):
        sym = os.path.basename(fn)[:-5]
        d = json.load(open(fn))
        f[sym] = sorted(d["rows"])      # [[ts_s, rate], ...]
    return f


def funding_paid(frows, entry_ts, exit_ts):
    """sum of realized 8h funding rates settled within (entry, exit] -> long PAYS this.
    Clip non-physical glitches (data has -200%/8h rows)."""
    return sum(max(-CLIP, min(CLIP, r)) for ts, r in frows if entry_ts < ts <= exit_ts)


def collect_funded(data, funding, funnel):
    sigs, per = [], {}
    cov_min = min((rows[0][0] for rows in funding.values()), default=0)
    for sym, series in data.items():
        feats = features(series); at_series = atr(series)
        per[sym] = (series, feats, at_series)
        tmax, tmin = series[-1][0], series[0][0]
        cut = tmin + SPLIT * (tmax - tmin)
        for f in feats:
            if not funnel(f):
                continue
            entry_i = f["t"] + 1
            if entry_i >= len(series):
                continue
            seg = "train" if series[f["t"]][0] <= cut else "test"
            sigs.append({"sym": sym, "f": f, "entry_i": entry_i, "seg": seg})
    FK = ["ext20", "ext60", "ret24", "rsi", "volc"]
    tr = [s for s in sigs if s["seg"] == "train"]
    thr = {k: (sorted(s["f"][k] for s in tr)[int(2/3*len(tr))] if tr else 0) for k in FK}
    out, last_exit = [], {}
    for s in sorted(sigs, key=lambda x: (x["sym"], x["entry_i"])):
        sym = s["sym"]
        if s["entry_i"] <= last_exit.get(sym, -1):
            continue
        series, feats, at_series = per[sym]
        r = sim_long_full(series, at_series, s["entry_i"])
        if not r:
            continue
        last_exit[sym] = s["entry_i"] + r[3]
        entry_ts = series[s["entry_i"]][0]
        if sym not in funding or entry_ts < funding[sym][0][0]:
            continue                              # no funding coverage -> drop
        exit_ts = entry_ts + r[3] * 3600
        fpaid = funding_paid(funding[sym], entry_ts, exit_ts)
        s["score"] = sum(1 for k in FK if s["f"][k] >= thr[k])
        s["gross"] = r[0]
        s["fund"] = fpaid
        s["net"] = r[0] - fpaid                   # long pays positive funding
        s["cat"] = r[2]
        out.append(s)
    return out


def grp(sigs, key):
    rg, rn = [s["gross"] for s in sigs], [s["net"] for s in sigs]
    fp = [s["fund"] for s in sigs]
    cat = sum(s["cat"] for s in sigs) / len(sigs)
    win_n = sum(1 for s in sigs if s["net"] > 0) / len(sigs)
    return (len(sigs), 100*sum(rg)/len(rg), 100*sum(rn)/len(rn),
            100*sum(fp)/len(fp), 100*win_n, 100*cat)


def report(sigs, name):
    print(f"\n--- {name}: GROSS vs NET-of-funding by froth bin "
          f"(n | gross | net | fund_drag | net_win% | cat%) ---")
    bins = {}
    for s in sigs:
        bins.setdefault(bin_of(s["score"]), []).append(s)
    for b in ["0-1", "2-3", "4-5"]:
        g = bins.get(b)
        if not g:
            print(f"  bin {b}: (none)"); continue
        n, gr, nt, fd, w, c = grp(g, b)
        print(f"  bin {b}: n={n:4d} gross={gr:+6.2f}% net={nt:+6.2f}% "
              f"fund_drag={fd:+5.2f}% net_win={w:3.0f}% cat={c:4.1f}%")


def robust(sigs, name):
    rets = sorted((s["net"] for s in sigs), reverse=True)
    if not rets:
        print(f"  [{name}] (none)"); return
    pos = sum(r for r in rets if r > 0); neg = -sum(r for r in rets if r < 0)
    pf = pos/neg if neg else 999
    pf1 = (pos - max(rets, default=0))/neg if neg else 999
    rng = random.Random(7); means = []
    for _ in range(2000):
        means.append(sum(rets[rng.randrange(len(rets))] for _ in range(len(rets)))/len(rets))
    means.sort()
    print(f"  [{name}] n={len(rets)} NET_EV={100*sum(rets)/len(rets):+.2f}% "
          f"PF={pf:.2f} PF_top1rm={pf1:.2f} boot_p5={100*means[int(0.05*len(means))]:+.2f}%")


def run(data, funding, funnel, label):
    print(f"\n================= {label} (NET of real 8h funding) =================")
    sigs = collect_funded(data, funding, funnel)
    if not sigs:
        print("  no covered signals."); return
    g = grp(sigs, "ALL")
    print(f"ALL covered n={g[0]} gross={g[1]:+.2f}% NET={g[2]:+.2f}% "
          f"avg_fund_drag={g[3]:+.2f}% net_win={g[4]:.0f}% cat={g[5]:.1f}%")
    report(sigs, "ALL")
    report([s for s in sigs if s["seg"] == "train"], "TRAIN")
    report([s for s in sigs if s["seg"] == "test"], "TEST (OOS)")
    print("\n  robustness (NET):")
    robust(sigs, "ALL covered")
    robust([s for s in sigs if s["score"] in (2, 3)], "mid-froth(2-3)")
    robust([s for s in sigs if s["score"] >= 4], "froth>=4")


def main():
    data = load(); funding = load_funding()
    print(f"#### FROTH LONG net of REAL funding. coins={len(data)} funded={len(funding)} ####")
    print(f"funding coverage starts ~2025-06-23; trades before that are dropped.")
    run(data, funding, is_breakout, "BREAKOUT LONG")
    run(data, funding, is_rollover, "ROLLOVER LONG")


if __name__ == "__main__":
    main()
