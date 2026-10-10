#!/usr/bin/env python3
"""Turn "not significant" into "here is what the sample excludes".

A p-value alone cannot separate "no edge" from "no power". The decision that
matters is whether the sample could have detected an edge as large as the cost
hurdle, because an edge smaller than the hurdle is untradeable regardless of
significance. So for every cell we report the day-blocked bootstrap CI on the
edge next to the hurdle it would have to clear.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict

import numpy as np

import run_mr2 as M

BOOT = 10_000
RNG = np.random.default_rng(20260818)


def ci(by_day: dict[str, list[float]]) -> tuple[float, float, float, float]:
    days = list(by_day.values())
    obs = float(np.mean([x for d in days for x in d]))
    nd = len(days)
    means = np.empty(BOOT)
    for i in range(BOOT):
        pick = RNG.integers(0, nd, nd)
        means[i] = np.mean([x for j in pick for x in days[j]])
    return obs, float(means.std(ddof=1)), float(np.percentile(means, 2.5)), \
        float(np.percentile(means, 97.5))


if __name__ == "__main__":
    raw = M.load()
    grids = {s: M.to_grid(d) for s, d in raw.items()}
    nm = {s: g for s, g in grids.items() if g["is_major"] == 0}
    z = {s: M.signals(g) for s, g in nm.items()}
    mean_cost = float(np.mean([g["cost_bps"] for g in nm.values()]))
    print(f"[pw] non_major symbols={len(nm)}  mean one-way cost={mean_cost:.1f} bps  "
          f"round-trip hurdle={2*mean_cost:.1f} bps\n")

    print(f"{'cell':<16}{'n':>6}{'edge':>8}{'se':>7}{'ci95_low':>10}{'ci95_high':>11}"
          f"{'hurdle':>8}{'excl?':>7}")
    rows = []
    for side in ("long", "short"):
        for k in M.KS:
            for h in M.HS:
                r = {s: M.fwd(g, h) for s, g in nm.items()}
                edge_day: dict[str, list[float]] = defaultdict(list)
                hurdles = []
                for s, g in nm.items():
                    sel = M.picks(z[s], r[s], k, h, side)
                    if not len(sel):
                        continue
                    bars = np.array(g["bar"])
                    sel = sel[np.array([bars[t + 1] < M.OOS_END for t in sel])]
                    if not len(sel):
                        continue
                    m = np.isfinite(r[s])
                    base = r[s][m]
                    u = float(base.mean()) * (1 if side == "long" else -1) * 1e4
                    for t in sel:
                        c = r[s][t] * (1 if side == "long" else -1) * 1e4
                        edge_day[g["bar"][t + 1][:10]].append(c - u)
                        hurdles.append(2 * g["cost_bps"])
                if not hurdles:
                    continue
                obs, se, lo, hi = ci(edge_day)
                n = len(hurdles)
                hur = float(np.mean(hurdles))
                excl = "YES" if hi < hur else "no"
                rows.append({"cell": f"{side}_k{k:g}_h{h}", "n": n, "edge_bps": obs,
                             "se_bps": se, "ci95": [lo, hi], "hurdle_bps": hur,
                             "hurdle_excluded": excl == "YES"})
                print(f"{rows[-1]['cell']:<16}{n:>6}{obs:>8.1f}{se:>7.1f}{lo:>10.1f}"
                      f"{hi:>11.1f}{hur:>8.1f}{excl:>7}")

    n_excl = sum(1 for r in rows if r["hurdle_excluded"])
    print(f"\n[pw] cells where the 95% upper bound is below the cost hurdle: "
          f"{n_excl}/{len(rows)}")
    smallest = min(rows, key=lambda r: r["hurdle_bps"] - r["ci95"][1])
    print(f"[pw] tightest case: {smallest['cell']} upper bound {smallest['ci95'][1]:.1f} "
          f"vs hurdle {smallest['hurdle_bps']:.1f} bps")
    json.dump({"mean_one_way_cost_bps": mean_cost, "cells": rows},
              open("out/power_check.json", "w"), indent=1)
    print("[pw] wrote out/power_check.json")
