#!/usr/bin/env python3
"""Recover the true per-day fee() step function for every Aerodrome-family pool in the
panel, over the panel window 2025-08-17 .. 2026-08-17.

s06 established that Slipstream fees are factory-settable and that 25 of 175 aero_cl
pools changed fee during the panel year, while 0 of 209 univ3 pools did. The panel
prices fee revenue with a single static fee field per pool, so every aero_cl pool that
moved has mispriced fee revenue for part of the year.

Strategy: fees are step functions with very few steps, so a dense daily scan would waste
~99% of its calls. Probe a 14-day grid first, then bisect only the intervals whose
endpoints disagree, down to 1-day resolution. Read-only eth_call.
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "/root/base_aero_emissions_r0/src")
import rpc

OUT = "/root/base_condmm_r0/out"
SEL_FEE = "0xddca3f43"

# census.cl_swaps materialises block_time with this affine map; reuse it so the fee
# curve lands on the same day boundaries the panel uses for `entry`.
B_REF, T_REF, SEC_PER_BLOCK = 46_000_000, 1_778_789_347, 2

DAY0 = date(2025, 8, 17)
DAY1 = date(2026, 8, 17)
GRID_DAYS = 14
BLOCKS_PER_DAY = 86400 // SEC_PER_BLOCK


def blk_of_day(d):
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return B_REF + (ts - T_REF) // SEC_PER_BLOCK


def main():
    pools = [json.loads(l) for l in open(f"{OUT}/universe50.jsonl") if l.strip()]
    targets = [p for p in pools if p["venue"] != "univ3"]
    print(f"pools total={len(pools)}  non-univ3 targets={len(targets)}")

    ndays = (DAY1 - DAY0).days
    days = [DAY0 + timedelta(days=i) for i in range(ndays + 1)]
    b_lo, b_hi = blk_of_day(DAY0), blk_of_day(DAY1)
    print(f"window: {DAY0} .. {DAY1}  blocks {b_lo} .. {b_hi}  ({ndays} days)")

    # probes[pool] = {block: fee or None}
    probes = {p["pool"]: {} for p in targets}

    def run(pending):
        """pending: list of (pool, block). Fills probes; returns number resolved."""
        if not pending:
            return 0
        reqs = [("eth_call", [{"to": pl, "data": SEL_FEE}, hex(b)]) for pl, b in pending]
        res = rpc.rpc_batch(reqs)
        for (pl, b), (out, err) in zip(pending, res):
            probes[pl][b] = None if err else rpc.dec_uint(out)
        return len(pending)

    grid = list(range(0, ndays + 1, GRID_DAYS))
    if grid[-1] != ndays:
        grid.append(ndays)
    grid_blocks = [blk_of_day(days[i]) for i in grid]
    pending = [(p["pool"], b) for p in targets for b in grid_blocks]
    print(f"coarse grid: {len(grid_blocks)} points/pool -> {len(pending)} calls")
    run(pending)
    print(f"  done. rpc={rpc.STATS['calls']}")

    for it in range(20):
        pending = []
        for pl, pr in probes.items():
            bs = sorted(pr)
            for a, b in zip(bs, bs[1:]):
                if pr[a] != pr[b] and (b - a) > BLOCKS_PER_DAY:
                    mid = (a + b) // 2
                    if mid not in pr:
                        pending.append((pl, mid))
        if not pending:
            print(f"bisection converged after {it} rounds")
            break
        print(f"refine round {it}: {len(pending)} calls  (rpc so far {rpc.STATS['calls']})")
        run(pending)

    curve = {}
    for pl, pr in probes.items():
        bs = sorted(pr)
        series = {}
        for d in days:
            bd = blk_of_day(d)
            lo = [b for b in bs if b <= bd]
            series[d.isoformat()] = pr[lo[-1]] if lo else pr[bs[0]]
        curve[pl] = series

    meta = {"day0": DAY0.isoformat(), "day1": DAY1.isoformat(),
            "grid_days": GRID_DAYS, "rpc": dict(rpc.STATS),
            "pools": len(curve)}
    json.dump({"meta": meta, "curve": curve}, open(f"{OUT}/fee_curve.json", "w"))

    stored = {p["pool"]: int(p["fee"]) for p in pools}
    ven = {p["pool"]: p["venue"] for p in pools}
    sym = {p["pool"]: p["symbol"] for p in pools}

    print("\n" + "=" * 104)
    print("POOLS WHOSE TRUE FEE DIFFERED FROM THE STORED FIELD ON >0 PANEL DAYS")
    print("=" * 104)
    print(f"{'pool':44s} {'venue':8s} {'sym':9s} {'stored':>7s} {'days_wrong':>10s} "
          f"{'mean_true':>9s} {'ratio':>6s}")
    rows = []
    for pl, series in curve.items():
        vals = [v for v in series.values() if v is not None]
        if not vals:
            continue
        f0 = stored[pl]
        wrong = sum(1 for v in vals if v != f0)
        mean_true = sum(vals) / len(vals)
        rows.append((pl, wrong, mean_true, f0, len(vals)))
    rows.sort(key=lambda r: -r[1])
    nwrong = 0
    for pl, wrong, mt, f0, n in rows:
        if wrong == 0:
            continue
        nwrong += 1
        print(f"{pl:44s} {ven[pl]:8s} {sym[pl][:9]:9s} {f0:7d} {wrong:10d} "
              f"{mt:9.1f} {f0 / mt if mt else 0:6.2f}")
    print(f"\n{nwrong}/{len(rows)} non-univ3 pools have >=1 day priced at the wrong fee")

    tot_days = sum(r[4] for r in rows)
    bad_days = sum(r[1] for r in rows)
    print(f"pool-days priced at the wrong fee: {bad_days}/{tot_days} "
          f"({100.0 * bad_days / tot_days:.1f}%)")
    print(f"\nrpc stats: {rpc.STATS}")


if __name__ == "__main__":
    main()
