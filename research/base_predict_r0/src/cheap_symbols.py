#!/usr/bin/env python3
"""Post-hoc: does the A5 edge survive on the symbols whose toll is small?

A5 found a real signal -- +8 to +24 bps, dose-responsive in both the threshold
and the window, and stable across IS/OOS -- that is 3 to 8 times smaller than
the round-trip cost of the median symbol. But the hurdle is not uniform: it runs
from 2.0 bps (bsdETH) to 181.6 (RSC). If a +20 bps edge exists on a 2 bps
symbol, that clears.

This is explicitly post-hoc. Round 35 ran the same cut and got non-exclusions
that were really n=76-184 / se=21-41 underpowering, not leads, so nothing here
counts as a lead unless the interval clears zero at the corrected alpha AND the
sample is large enough to say so.

The second half asks the question that decides whether symbol selection can ever
work: is the hurdle actually cheap relative to how much the symbol moves? Cost
and volatility may simply scale together, in which case no selection helps and
the constraint is structural rather than a matter of picking better names.
"""
from __future__ import annotations

import json
import sys

import numpy as np

sys.path.insert(0, "src")
from run_predict import (ALPHA, BOOT, N_CELLS, OOS_END, RNG, fwd,  # noqa: E402
                        load, picks, sig_a5, sigma_168, to_grid)

CHEAP_MAX_HURDLE = 40.0      # covers bsdETH, VIRTUAL, HYPE, ZEN, CMC20, ZRO
BEST_CELLS = [(24, 0.6, 4), (4, 0.6, 4), (24, 0.6, 24), (1, 0.6, 4)]


def dayboot(byday: dict, alpha: float) -> dict:
    days = list(byday.values())
    flat = [x for d in days for x in d]
    out = {"n": len(flat), "n_days": len(days), "mean": float(np.mean(flat))}
    if len(days) < 5:
        return out | {"lo": None, "hi": None, "se": None}
    nd = len(days)
    ms = np.empty(BOOT)
    for i in range(BOOT):
        p = RNG.integers(0, nd, nd)
        ms[i] = np.mean([x for j in p for x in days[j]])
    return out | {"lo": float(np.quantile(ms, alpha / 2)),
                  "hi": float(np.quantile(ms, 1 - alpha / 2)),
                  "se": float(ms.std(ddof=1))}


def main() -> None:
    grids = {s: to_grid(d) for s, d in load().items()}

    # ---- part 1: is the toll cheap relative to how much the symbol moves? ----
    print("=" * 104)
    print("Is a cheap symbol cheap in the way that matters? hurdle vs its own hourly")
    print("volatility. sigma_1h is the median trailing-168h stdev of hourly log returns.")
    print("=" * 104)
    print(f"{'symbol':<10}{'hurdle_bps':>11}{'sigma_1h_bps':>14}{'sigma_4h_bps':>14}"
          f"{'hurdle/sigma_4h':>17}")
    rows = []
    for s, g in grids.items():
        sg = sigma_168(g["close"])
        sg = sg[np.isfinite(sg)]
        if not len(sg):
            continue
        s1 = float(np.median(sg)) * 1e4
        s4 = s1 * 2.0                        # sqrt(4)
        h = 2.0 * g["cost_bps"]
        rows.append((s, h, s1, s4, h / s4 if s4 > 0 else float("nan")))
    for s, h, s1, s4, ratio in sorted(rows, key=lambda x: x[1]):
        print(f"{s:<10}{h:>11.1f}{s1:>14.1f}{s4:>14.1f}{ratio:>17.2f}")
    rr = np.array([r[4] for r in rows if np.isfinite(r[4])])
    print(f"\nhurdle / sigma_4h across {len(rr)} symbols: "
          f"p10={np.quantile(rr, .1):.2f} p50={np.median(rr):.2f} "
          f"p90={np.quantile(rr, .9):.2f}  (ratio<1 means a 4h move can cover the toll)")
    cheap = [r for r in rows if r[1] <= CHEAP_MAX_HURDLE]
    print(f"symbols with hurdle <= {CHEAP_MAX_HURDLE:.0f} bps: "
          f"{', '.join(r[0] for r in sorted(cheap, key=lambda x: x[1]))}")
    print("their hurdle/sigma_4h: " + ", ".join(
        f"{r[0]}={r[4]:.2f}" for r in sorted(cheap, key=lambda x: x[1])))

    # ---- part 2: A5 restricted to the cheap symbols ----
    keep = {r[0] for r in cheap}
    sub = {s: g for s, g in grids.items() if s in keep}
    print("\n" + "=" * 104)
    print(f"A5 on the {len(sub)} cheap symbols only (POST-HOC, alpha=0.05/{N_CELLS} kept)")
    print("=" * 104)
    print(f"{'cell':<16}{'n':>6}{'days':>5}{'uncond':>8}{'cond':>8}{'edge':>8}"
          f"{'hurdle':>8}{'net':>9}{'net_lo':>9}{'net_hi':>9}{'se':>7}")
    out = {}
    a = ALPHA / N_CELLS
    for W, q, h in BEST_CELLS:
        sig = {s: sig_a5(g, W) for s, g in sub.items()}
        byday: dict[str, list[float]] = {}
        eday: dict[str, list[float]] = {}
        conds, unconds, hurs = [], [], []
        for s, g in sub.items():
            r = fwd(g, h)
            sel = picks(sig[s], r, q, h)
            if not len(sel):
                continue
            bars = np.array(g["bar"])
            sel = sel[[b < OOS_END for b in bars[sel + 1]]]
            if not len(sel):
                continue
            m = np.isfinite(r)
            u = float(r[m].mean()) * 1e4
            hd = 2.0 * g["cost_bps"]
            for t in sel:
                c = r[t] * 1e4
                d = g["bar"][t + 1][:10]
                byday.setdefault(d, []).append(c - u - hd)
                eday.setdefault(d, []).append(c - u)
                conds.append(c)
                unconds.append(u)
                hurs.append(hd)
        if not conds:
            continue
        net = dayboot(byday, a)
        edge = dayboot(eday, a)
        key = f"W{W}_q{q:g}_h{h}"
        out[key] = {"net": net, "edge": edge, "hurdle_bps": float(np.mean(hurs))}
        print(f"{key:<16}{net['n']:>6}{net['n_days']:>5}{np.mean(unconds):>8.1f}"
              f"{np.mean(conds):>8.1f}{edge['mean']:>8.1f}{np.mean(hurs):>8.1f}"
              f"{net['mean']:>9.1f}"
              f"{(net.get('lo') if net.get('lo') is not None else float('nan')):>9.1f}"
              f"{(net.get('hi') if net.get('hi') is not None else float('nan')):>9.1f}"
              f"{(net.get('se') if net.get('se') is not None else float('nan')):>7.1f}")

    pos = [k for k, v in out.items()
           if v["net"].get("lo") is not None and v["net"]["lo"] > 0]
    neg = [v["net"]["hi"] for v in out.values() if v["net"].get("hi") is not None]
    print()
    if pos:
        print(f"POST_HOC_LEAD on {', '.join(pos)} -- not a result. Needs the hurdle "
              "recomputed at true $5,000 and a fresh out-of-sample window.")
    elif neg and all(x < 0 for x in neg):
        print(f"NO_EDGE_EVEN_ON_CHEAP_SYMBOLS: every cell's upper bound < 0 "
              f"(loosest {max(neg):.1f} bps). Symbol selection does not rescue A5.")
    else:
        print("UNDERPOWERED_ON_CHEAP_SUBSET: intervals straddle zero. Same outcome as "
              "round 35's post-hoc cut -- records nothing.")
    json.dump(out, open("out/cheap_symbols.json", "w"), indent=1)


if __name__ == "__main__":
    main()
