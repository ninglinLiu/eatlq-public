#!/usr/bin/env python3
"""READ-ONLY: profit-capture rate by max_concurrent (fixed $100, $1000 start)."""
import _recompute_live_config as M

tr = M.build_walkforward(0.02)


def stats(cap):
    s = M.portfolio_sim(tr, start=1000.0, stake=100.0, max_concurrent=cap)
    return s["n"], s["skipped_concurrency"], s["end"] - 1000.0, s["peak_concurrent"]


base_n, _, base_pnl, base_peak = stats(10**9)
print(f"UNLIMITED: n={base_n} pnl=${base_pnl:,.0f} peak_concurrent={base_peak}")
for cap in (3, 6, 10):
    n, skip, pnl, peak = stats(cap)
    print(f"cap={cap:2d}: taken={n:3d} skipped={skip:3d} pnl=${pnl:9,.0f} "
          f"capture={100*pnl/base_pnl:5.1f}% peak={peak}")
