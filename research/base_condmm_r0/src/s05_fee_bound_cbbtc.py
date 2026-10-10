#!/usr/bin/env python3
"""Absolute upper-bound check on the one cell that produced every positive number.

The cell aero_cl x cbBTC x 25bps earns a mean 625.0 bps of fees over a 7-day
hold, while the same asset on univ3 at a HIGHER fee tier (30bps) earns 70.7 bps.
Same asset, higher tier, nine times less fee income. One of the two is wrong.

The test needs no model. My synthetic position cannot receive more fees than the
pool itself collected over the same window, and the pool's take is an identity:

    pool_fees_weth = sum(|WETH leg of every swap|) * fee_rate

So compute ratio = my_fee_weth / pool_fees_weth for every position in the cell.
ratio > 1 is impossible and proves the attribution is broken. ratio should track
the position's liquidity share, which the panel records as `share`.

This is round 40's absolute-bound audit re-run on a single cell. Read-only.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_condmm_r0")
POSITIONS = Path("/root/base_fee_tier_r0/out/stage3_pnl_50.json")
WETH = "0x4200000000000000000000000000000000000006"
NOTIONAL_USD = 50.0


def ch(q: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage", "12000000000", "--query", q],
        capture_output=True, text=True, timeout=1800)
    if r.returncode:
        raise RuntimeError(r.stderr[:3000])
    return r.stdout


def main():
    meta = pd.DataFrame(
        [l.split("\t") for l in ch(
            "SELECT pool, venue, symbol, fee FROM edge.bars_universe_50 "
            "FORMAT TabSeparated").rstrip("\n").split("\n") if l],
        columns=["pool", "venue", "sym", "fee_tier"])
    cb = meta.loc[(meta.sym == "cbBTC")]
    print("cbBTC pools in the $50 universe:")
    print(cb.to_string(index=False))

    pos = pd.DataFrame(json.loads(POSITIONS.read_text())).merge(
        meta, on="pool", how="left")

    cell = pos.loc[(pos.sym == "cbBTC") & (pos.venue == "aero_cl") & (pos["T"] == 7)]
    ctrl = pos.loc[(pos.sym == "cbBTC") & (pos.venue == "univ3") & (pos["T"] == 7)]
    print(f"\ncell aero_cl cbBTC T=7 rows={len(cell)} pools={cell.pool.nunique()}")
    print(f"ctrl univ3   cbBTC T=7 rows={len(ctrl)} pools={ctrl.pool.nunique()}")
    for nm, d in (("aero_cl", cell), ("univ3", ctrl)):
        print(f"  {nm}: fee_bps mean={d.fee_bps.mean():.1f} "
              f"p50={d.fee_bps.median():.1f} p99={d.fee_bps.quantile(.99):.1f} "
              f"share p50={d.share.median():.3e} p99={d.share.quantile(.99):.3e} "
              f"in_range p50={d.in_range.median():.3f}")

    # per-pool: mean vs median net is the right-tail test the ledger keeps needing
    print(f"\n{'='*104}\nper-pool, cbBTC T=7 (mean vs median separates a real edge "
          "from a right tail)")
    print(f"{'pool':>44}{'venue':>9}{'tier':>6}{'rows':>6}"
          f"{'net_mean':>10}{'net_p50':>9}{'fee_mean':>10}{'fee_p50':>9}"
          f"{'IL_mean':>9}{'shr_p50':>10}{'shr_p99':>10}")
    per_pool = []
    for p, g in pos.loc[(pos.sym == "cbBTC") & (pos["T"] == 7)].groupby("pool"):
        rec = {
            "pool": p, "venue": g.venue.iloc[0], "fee_tier": g.fee_tier.iloc[0],
            "rows": len(g),
            "net_mean": float(g.net_strict_bps.mean()),
            "net_p50": float(g.net_strict_bps.median()),
            "fee_mean": float(g.fee_bps.mean()),
            "fee_p50": float(g.fee_bps.median()),
            "il_mean": float(g.il_bps.mean()),
            "share_p50": float(g.share.median()),
            "share_p99": float(g.share.quantile(0.99)),
        }
        per_pool.append(rec)
        print(f"{p:>44}{rec['venue']:>9}{rec['fee_tier']:>6}{rec['rows']:>6}"
              f"{rec['net_mean']:>10.1f}{rec['net_p50']:>9.1f}"
              f"{rec['fee_mean']:>10.1f}{rec['fee_p50']:>9.1f}"
              f"{rec['il_mean']:>9.1f}{rec['share_p50']:>10.2e}"
              f"{rec['share_p99']:>10.2e}")

    # pool-level fee identity per (pool, 7-day window)
    pools = sorted(set(cell.pool) | set(ctrl.pool))
    plist = ",".join(f"'{p}'" for p in pools)
    print(f"\npulling pool-level swap volume for {len(pools)} pools ...")
    rows = ch(f"""
        SELECT s.pool, toDate(s.block_time) AS d,
               sum(abs(toFloat64(if(p.token0='{WETH}', s.amount0, s.amount1))))/1e18
                   AS weth_vol,
               count() AS n_swaps,
               quantile(0.5)(toFloat64(s.liquidity)) AS L_med,
               quantile(0.1)(toFloat64(s.liquidity)) AS L_q10
        FROM census.cl_swaps s
        INNER JOIN census.pools p ON p.pool = s.pool
        WHERE s.pool IN ({plist}) AND s.block_time >= '2025-08-17'
        GROUP BY s.pool, d ORDER BY s.pool, d FORMAT TabSeparated""")
    daily = pd.DataFrame(
        [l.split("\t") for l in rows.rstrip("\n").split("\n") if l],
        columns=["pool", "d", "weth_vol", "n_swaps", "L_med", "L_q10"])
    for c in ("weth_vol", "L_med", "L_q10"):
        daily[c] = daily[c].astype(float)
    daily["n_swaps"] = daily.n_swaps.astype(int)
    daily["d"] = pd.to_datetime(daily.d)

    out = {"cbBTC_pools": cb.to_dict("records"), "per_pool_T7": per_pool,
           "checks": []}

    for nm, d in (("aero_cl", cell), ("univ3", ctrl)):
        recs = []
        for _, r in d.iterrows():
            t0 = pd.Timestamp(r.entry)
            t1 = t0 + pd.Timedelta(days=7)
            w = daily.loc[(daily.pool == r.pool) & (daily.d >= t0) & (daily.d < t1)]
            if w.empty:
                continue
            fee_rate = int(r.fee_tier) / 1e6
            pool_fees_weth = float(w.weth_vol.sum()) * fee_rate
            # my fee in WETH: fee_bps is against the $50 notional
            my_fee_usd = r.fee_bps / 1e4 * NOTIONAL_USD
            recs.append({
                "pool": r.pool, "entry": r.entry,
                "pool_fees_weth": pool_fees_weth,
                "pool_weth_vol": float(w.weth_vol.sum()),
                "n_swaps": int(w.n_swaps.sum()),
                "L_med": float(w.L_med.median()),
                "L_q10": float(w.L_q10.median()),
                "my_fee_usd": my_fee_usd,
                "fee_bps": float(r.fee_bps),
                "share": float(r.share),
            })
        df = pd.DataFrame(recs)
        if df.empty:
            continue
        # convert pool fees to USD with the panel's own ETH price
        eth = pd.DataFrame(
            [l.split("\t") for l in ch(
                "SELECT toDate(bar) AS d, quantile(0.5)(eth_usd) FROM edge.eth_usd_1h "
                "GROUP BY d ORDER BY d FORMAT TabSeparated").rstrip("\n").split("\n") if l],
            columns=["d", "eth_usd"])
        eth["d"] = pd.to_datetime(eth.d)
        eth["eth_usd"] = eth.eth_usd.astype(float)
        df["entry_d"] = pd.to_datetime(df.entry)
        df = df.merge(eth, left_on="entry_d", right_on="d", how="left")
        df["pool_fees_usd"] = df.pool_fees_weth * df.eth_usd
        df["ratio"] = df.my_fee_usd / df.pool_fees_usd

        print(f"\n{'='*88}\n{nm} cbBTC T=7  absolute fee bound")
        print(f"  positions checked        {len(df)}")
        print(f"  pool weth vol / 7d  p50 {df.pool_weth_vol.median():,.1f} WETH")
        print(f"  pool fees / 7d      p50 ${df.pool_fees_usd.median():,.2f}")
        print(f"  my fee / 7d         p50 ${df.my_fee_usd.median():,.4f}")
        print(f"  ratio my/pool       p50 {df.ratio.median():.4f}  "
              f"p90 {df.ratio.quantile(.9):.4f}  max {df.ratio.max():.4f}")
        print(f"  VIOLATIONS ratio>1       {int((df.ratio > 1).sum())} / {len(df)}")
        print(f"  recorded share      p50 {df.share.median():.3e}")
        print(f"  ratio / share       p50 {(df.ratio/df.share).median():.2f}"
              "   (should be ~1 if attribution is consistent)")
        print(f"  L_med               p50 {df.L_med.median():.4e}")
        print(f"  L_q10               p50 {df.L_q10.median():.4e}")
        print(f"  n_swaps / 7d        p50 {df.n_swaps.median():,.0f}")
        out["checks"].append({
            "venue": nm, "n": len(df),
            "ratio_p50": float(df.ratio.median()),
            "ratio_p90": float(df.ratio.quantile(.9)),
            "ratio_max": float(df.ratio.max()),
            "violations": int((df.ratio > 1).sum()),
            "share_p50": float(df.share.median()),
            "ratio_over_share_p50": float((df.ratio / df.share).median()),
            "pool_weth_vol_p50": float(df.pool_weth_vol.median()),
            "pool_fees_usd_p50": float(df.pool_fees_usd.median()),
            "my_fee_usd_p50": float(df.my_fee_usd.median()),
            "L_med_p50": float(df.L_med.median()),
            "L_q10_p50": float(df.L_q10.median()),
            "n_swaps_p50": float(df.n_swaps.median()),
        })

    with open(ROOT / "out" / "fee_bound_cbbtc.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("\n[s05] wrote out/fee_bound_cbbtc.json")


if __name__ == "__main__":
    main()
