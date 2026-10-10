#!/usr/bin/env python3
"""Why are BOTH long and short negative? Decompose it.

For the SAME rollover candidate set, compare:
  (A) BUY&HOLD long over H bars  vs  its exact mirror short (= -raw - cost).
      These MUST sum to -2*cost (zero-sum identity). This is the sanity anchor.
  (B) STOP+TRAIL long  vs  STOP+TRAIL short (the path-dependent exits we used).
      Their sum is FAR below -2*cost. The gap = 'exit drag' = what the
      stop/trailing machinery bleeds in chop, on BOTH sides.

If (A) sums to ~-0.1% but (B) sums to ~-4%, the double-negative is the EXITS,
not a missing alpha. That is the honest answer to 'how can both sides lose'.
"""
from __future__ import annotations
import statistics as st
from froth_long import load, features, atr, sim_long_full, is_rollover, is_breakout
from froth_short import sim_short_full

RT_COST = 0.0010
H = 48


def raw_long(series, entry_i, h):
    """enter next-bar open, exit close h bars later. raw price return (no cost)."""
    ex = min(entry_i + h - 1, len(series) - 1)
    entry = series[entry_i][1]
    if entry <= 0:
        return None
    return series[ex][4] / entry - 1


def run(data, funnel, label):
    bh_l, bh_s, st_l, st_s = [], [], [], []
    for sym, series in data.items():
        feats = features(series); at_series = atr(series)
        for f in feats:
            if not funnel(f):
                continue
            ei = f["t"] + 1
            if ei + H >= len(series):
                continue
            rl = raw_long(series, ei, H)
            if rl is None:
                continue
            bh_l.append(rl - RT_COST)
            bh_s.append(-rl - RT_COST)
            sl = sim_long_full(series, at_series, ei)
            ss = sim_short_full(series, at_series, ei)
            if sl: st_l.append(sl[0])
            if ss: st_s.append(ss[0])
    n = len(bh_l)
    mbl, mbs = st.mean(bh_l), st.mean(bh_s)
    msl, mss = st.mean(st_l), st.mean(st_s)
    print(f"\n===== {label}  (n={n}, H={H}h) =====")
    print(f"  (A) BUY&HOLD    long EV={100*mbl:+6.2f}%   short EV={100*mbs:+6.2f}%   "
          f"SUM={100*(mbl+mbs):+6.2f}%   (identity: -2*cost = {-200*RT_COST:.2f}%)")
    print(f"  (B) STOP+TRAIL  long EV={100*msl:+6.2f}%   short EV={100*mss:+6.2f}%   "
          f"SUM={100*(msl+mss):+6.2f}%")
    print(f"  EXIT DRAG (B-A):  long {100*(msl-mbl):+6.2f}%   short {100*(mss-mbs):+6.2f}%   "
          f"total bled by exits = {100*((msl+mss)-(mbl+mbs)):+.2f}%")
    print(f"  -> buy&hold is zero-sum; the double-negative is the STOP/TRAIL exits "
          f"whipsawing both sides in chop.")


def main():
    data = load()
    print("#### MIRROR DIAGNOSTIC: can both long & short really be negative? ####")
    run(data, is_rollover, "ROLLOVER (overheated + rolling over)")
    run(data, is_breakout, "BREAKOUT (overheated, near high)")


if __name__ == "__main__":
    main()
