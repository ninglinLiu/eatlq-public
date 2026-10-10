#!/usr/bin/env python3
"""Where does the A5 edge live -- and can it ever be somewhere tradeable?

The pooled A5 result was +8 to +24 bps of real, dose-responsive, IS/OOS-stable
edge. Restricting to the six cheapest symbols made it vanish or go negative.
That points at one specific structure: the edge is concentrated in the names
whose toll is largest.

If so, the constraint is not "cost is too high" and not "there is no signal".
It is that signal strength and tradability are anti-correlated across the
universe, so every cheap name is also an unpredictable one. That is a claim
about the market, testable directly: rank the symbols by their own toll and
look at where the edge sits.

Two normalisations are reported because they answer different questions:
  edge_bps        -- can this pay the toll (absolute, what a trader cares about)
  edge / sigma_4h -- how much of the symbol's own motion is being predicted
                     (scale-free, comparable across a 0.4 bps and a 118 bps name)
"""
from __future__ import annotations

import json
import sys

import numpy as np

sys.path.insert(0, "src")
from run_predict import OOS_END, fwd, load, picks, sig_a5, sigma_168, to_grid  # noqa: E402

W, Q, H = 4, 0.6, 4       # the cell with the best pooled combination of n and edge


def main() -> None:
    grids = {s: to_grid(d) for s, d in load().items()}
    rows = []
    for s, g in grids.items():
        sg = sigma_168(g["close"])
        sg = sg[np.isfinite(sg)]
        if not len(sg):
            continue
        s4 = float(np.median(sg)) * 1e4 * 2.0
        r = fwd(g, H)
        sel = picks(sig_a5(g, W), r, Q, H)
        bars = np.array(g["bar"])
        if len(sel):
            sel = sel[[b < OOS_END for b in bars[sel + 1]]]
        if len(sel) < 15:
            continue
        m = np.isfinite(r)
        u = float(r[m].mean()) * 1e4
        c = r[sel] * 1e4
        edge = float(c.mean()) - u
        se = float(c.std(ddof=1) / np.sqrt(len(c)))
        hur = 2.0 * g["cost_bps"]
        rows.append(dict(sym=s, n=len(sel), hurdle=hur, sigma4=s4, edge=edge,
                         se=se, net=edge - hur,
                         edge_sig=edge / s4 if s4 > 0 else float("nan"),
                         hur_sig=hur / s4 if s4 > 0 else float("nan")))

    rows.sort(key=lambda x: x["hurdle"])
    print("=" * 110)
    print(f"A5 cell W={W} q={Q} h={H}, per symbol, sorted by its own round-trip toll")
    print("=" * 110)
    print(f"{'symbol':<10}{'n':>5}{'hurdle':>8}{'sigma_4h':>10}{'edge':>9}{'se':>7}"
          f"{'net':>9}{'edge/sig':>10}{'hurdle/sig':>12}")
    for r in rows:
        print(f"{r['sym']:<10}{r['n']:>5}{r['hurdle']:>8.1f}{r['sigma4']:>10.1f}"
              f"{r['edge']:>9.1f}{r['se']:>7.1f}{r['net']:>9.1f}"
              f"{r['edge_sig']:>10.3f}{r['hur_sig']:>12.2f}")

    h = np.array([r["hurdle"] for r in rows])
    e = np.array([r["edge"] for r in rows])
    es = np.array([r["edge_sig"] for r in rows])
    n = np.array([r["n"] for r in rows], dtype=float)
    print()
    print(f"symbols with n>=15: {len(rows)}")
    print(f"corr(hurdle, edge_bps)      = {np.corrcoef(h, e)[0, 1]:+.3f}"
          "   positive => the edge lives in the expensive names")
    print(f"corr(hurdle, edge/sigma_4h) = {np.corrcoef(h, es)[0, 1]:+.3f}"
          "   scale-free version of the same question")
    print(f"weighted mean edge/sigma_4h = {float(np.average(es, weights=n)):+.4f}"
          "   (how much of a symbol's own 4h motion the signal captures)")
    print(f"median hurdle/sigma_4h      = {np.median([r['hur_sig'] for r in rows]):.2f}"
          "   (how much of that motion the toll costs)")

    lo = [r for r in rows if r["hurdle"] <= 40]
    hi = [r for r in rows if r["hurdle"] > 40]
    for lbl, grp in (("toll <= 40 bps", lo), ("toll >  40 bps", hi)):
        if not grp:
            continue
        wn = sum(g["n"] for g in grp)
        me = sum(g["edge"] * g["n"] for g in grp) / wn
        ms = sum(g["edge_sig"] * g["n"] for g in grp) / wn
        print(f"  {lbl}: {len(grp)} symbols, n={wn}, "
              f"trade-weighted edge={me:+.1f} bps, edge/sigma_4h={ms:+.4f}")

    best = max(rows, key=lambda r: r["net"])
    print(f"\nbest single symbol by net: {best['sym']} net={best['net']:+.1f} bps "
          f"(edge {best['edge']:+.1f} +/- {best['se']:.1f}, toll {best['hurdle']:.1f}, "
          f"n={best['n']})")
    pos = [r for r in rows if r["net"] > 0]
    print(f"symbols with positive net: {len(pos)}/{len(rows)}"
          + ("" if not pos else "  -> " + ", ".join(
              f"{r['sym']} {r['net']:+.1f} (n={r['n']}, se={r['se']:.1f})" for r in pos)))
    json.dump(rows, open("out/where_is_the_edge.json", "w"), indent=1)


if __name__ == "__main__":
    main()
