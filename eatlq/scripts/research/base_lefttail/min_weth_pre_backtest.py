#!/usr/bin/env python3
"""Backtest impact: MIN_WETH_PRE from first swap (W=600s), on 644 labeled universe.

Compares adding pre-window WETH swap-in floor on top of aggressive live gates:
  MIN_PRE>=5, p_fatal<=0.27, entry_weth>=3, friction>=-5%, F-off + PARADAENGI deployers.

weth_pre_600 = sum of WETH-side inbound swaps in [0,600s] from t0 (first swap).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aggressive_live_rebacktest as A  # noqa
import live_config_rebacktest as L  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/min_weth_pre_backtest_20260701"
W = 600
THRESHOLDS = [0.0, 0.1, 0.5, 1.0, 3.0]
XYZ = "0x2c4f71b3c9db0ca96ea072d8ac2da95f8ec35743"


def fetch_weth_pre(pools_orient: dict[str, int]) -> dict[str, float]:
    """Batch CH: WETH inbound volume in first W seconds from t0."""
    import clickhouse_connect

    client = clickhouse_connect.get_client(host="localhost", port=8123, username="default", password="")
    addrs = list(pools_orient.keys())
    # query in one shot with pool list
    in_list = ",".join(f"'{p}'" for p in addrs)
    sql = f"""
    WITH fs AS (
      SELECT pool_address, toUnixTimestamp(min(block_time)) u0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND pool_address IN ({in_list}) AND block_time IS NOT NULL
      GROUP BY pool_address
    )
    SELECT s.pool_address,
      sumIf(
        if(lower(fs.pool_address)=lower(s.pool_address) AND
           (CASE WHEN {{orient_map}} END),
           abs(toFloat64(s.amount0))/1e18,
           abs(toFloat64(s.amount1))/1e18
        ),
        toUnixTimestamp(s.block_time)-fs.u0 BETWEEN 0 AND {W}
          AND (
            (CASE WHEN {{orient_map}} END AND s.amount0 > 0) OR
            (NOT (CASE WHEN {{orient_map}} END) AND s.amount1 > 0)
          )
      ) AS weth_pre
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN fs ON lower(fs.pool_address)=lower(s.pool_address)
    WHERE s.chain_id=8453 AND s.pool_address IN ({in_list})
    GROUP BY s.pool_address
    """
    # clickhouse CASE per pool is awkward — query raw sums and pick in Python
    sql = f"""
    WITH fs AS (
      SELECT pool_address, toUnixTimestamp(min(block_time)) u0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND pool_address IN ({in_list}) AND block_time IS NOT NULL
      GROUP BY pool_address
    )
    SELECT s.pool_address,
      sumIf(if(s.amount0>0, abs(toFloat64(s.amount0))/1e18, 0),
            toUnixTimestamp(s.block_time)-fs.u0 BETWEEN 0 AND {W}) s0_in,
      sumIf(if(s.amount1>0, abs(toFloat64(s.amount1))/1e18, 0),
            toUnixTimestamp(s.block_time)-fs.u0 BETWEEN 0 AND {W}) s1_in
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN fs ON lower(fs.pool_address)=lower(s.pool_address)
    WHERE s.chain_id=8453 AND s.pool_address IN ({in_list})
    GROUP BY s.pool_address
    """
    rows = client.query(sql).result_rows
    out = {}
    for pool, s0, s1 in rows:
        o = pools_orient.get(pool.lower()) or pools_orient.get(pool)
        if o is None:
            weth = min(float(s0 or 0), float(s1 or 0))
        elif o == -1:  # token0 = WETH
            weth = float(s0 or 0)
        else:
            weth = float(s1 or 0)
        out[pool.lower()] = weth
    return out


def fetch_weth_pre_docker(pools_orient: dict[str, int]) -> dict[str, float]:
    import subprocess

    addrs = list(pools_orient.keys())
    in_list = ",".join(f"'{p}'" for p in addrs)
    sql = f"""
    WITH fs AS (
      SELECT pool_address, toUnixTimestamp(min(block_time)) u0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND lower(pool_address) IN ({','.join("'"+p.lower()+"'" for p in addrs)})
        AND block_time IS NOT NULL
      GROUP BY pool_address
    )
    SELECT lower(s.pool_address) AS pool,
      sumIf(if(s.amount0>0, abs(toFloat64(s.amount0))/1e18, 0),
            toUnixTimestamp(s.block_time)-fs.u0 BETWEEN 0 AND {W}) s0_in,
      sumIf(if(s.amount1>0, abs(toFloat64(s.amount1))/1e18, 0),
            toUnixTimestamp(s.block_time)-fs.u0 BETWEEN 0 AND {W}) s1_in
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN fs ON lower(fs.pool_address)=lower(s.pool_address)
    WHERE s.chain_id=8453
    GROUP BY pool
    FORMAT JSON
    """
    raw = subprocess.check_output(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "-q", sql],
        text=True,
    )
    out = {}
    for row in json.loads(raw)["data"]:
        pool = row["pool"]
        s0, s1 = float(row["s0_in"] or 0), float(row["s1_in"] or 0)
        o = pools_orient.get(pool)
        if o == -1:
            weth = s0
        elif o == 1:
            weth = s1
        else:
            weth = min(s0, s1)
        out[pool] = weth
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = L.run_lens(L.load_base_df(), "qv2")
    _, path = __import__("keystone_live", fromlist=["load"]).load()
    priced = json.load(open(os.path.join(HERE, "_priced_live.json")))
    creators = json.load(open(
        "/opt/EATLQ/mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json"
    )).get("token_creators", {})

    pools_orient = {r.pool.lower(): int(r.orient) for r in df.itertuples()}
    cache_path = os.path.join(OUT_DIR, "weth_pre_600_cache.json")
    if os.path.exists(cache_path):
        weth_pre = json.load(open(cache_path))
    else:
        weth_pre = fetch_weth_pre_docker(pools_orient)
        json.dump(weth_pre, open(cache_path, "w"), indent=2)

    df["weth_pre_600"] = df.pool.map(lambda p: weth_pre.get(p.lower(), 0.0))
    base_gate = A.aggressive_gate(df, creators)

    # pools passing minpre+p but failing various weth_pre (before depth)
    pre_depth = (df.ns_pre >= A.MIN_PRE) & (df.p <= A.P_CUT) & df.gate_fric

    results = []
    baseline_trades = A.build_trades(df, path, priced, base_gate)
    baseline_pf = A.portfolio_sim(baseline_trades)

    for thr in THRESHOLDS:
        weth_gate = df.weth_pre_600 >= thr
        gate = base_gate & weth_gate
        trades = A.build_trades(df, path, priced, gate)
        pf = A.portfolio_sim(trades)

        blocked = base_gate & ~weth_gate
        blocked_df = df[blocked]
        blocked_pre = pre_depth & ~weth_gate

        # PnL of blocked winners (would-have-been trades under baseline)
        base_trade_pools = {t["pool"] for t in baseline_trades}
        blocked_trade_pools = base_trade_pools - {t["pool"] for t in trades}
        missed_pnl = sum(t["ret"] for t in baseline_trades if t["pool"] in blocked_trade_pools)

        results.append({
            "min_weth_pre_eth": thr,
            "gate_pass_n": int(gate.sum()),
            "blocked_vs_baseline": int(blocked.sum()),
            "blocked_pre_depth_n": int(blocked_pre.sum()),
            "portfolio": pf,
            "missed_trade_n": len(blocked_trade_pools),
            "missed_pnl_usd_sum": float(missed_pnl * A.STAKE) if missed_pnl else 0.0,
            "blocked_labels": dict(Counter(blocked_df.label)),
            "blocked_missed_winners": int(sum(
                1 for t in baseline_trades
                if t["pool"] in blocked_trade_pools and t["ret"] > 0
            )),
            "blocked_missed_fatals": int(sum(
                1 for t in baseline_trades
                if t["pool"] in blocked_trade_pools and t["ret"] <= -0.5
            )),
        })

    # distribution stats on full 644
    dist = {
        "n": len(df),
        "ns_pre_ge5": int((df.ns_pre >= 5).sum()),
        "weth_pre_p25": float(df.weth_pre_600.quantile(0.25)),
        "weth_pre_p50": float(df.weth_pre_600.median()),
        "weth_pre_p75": float(df.weth_pre_600.quantile(0.75)),
        "weth_pre_ge3": int((df.weth_pre_600 >= 3).sum()),
        "weth_pre_ge05": int((df.weth_pre_600 >= 0.5).sum()),
        "minpre_ge5_weth_lt01": int(((df.ns_pre >= 5) & (df.weth_pre_600 < 0.1)).sum()),
        "minpre_ge5_weth_lt05": int(((df.ns_pre >= 5) & (df.weth_pre_600 < 0.5)).sum()),
        "corr_weth_pre_vs_entry_weth": float(df.weth_pre_600.corr(df.entry_weth)),
    }

    # XYZ not in 644 — query if present
    xyz_note = None
    if XYZ.lower() in weth_pre:
        xyz_note = {"pool": XYZ, "weth_pre_600": weth_pre[XYZ.lower()]}

    report = {
        "meta": {
            "W_sec": W,
            "from": "first_swap_t0",
            "baseline": "aggressive_F_off TRAIL0.25",
            "thresholds_eth": THRESHOLDS,
        },
        "distribution_644": dist,
        "xyz_live_pool": xyz_note,
        "baseline_portfolio": baseline_pf,
        "scenarios": results,
    }
    out_json = os.path.join(OUT_DIR, "min_weth_pre_backtest.json")
    json.dump(report, open(out_json, "w"), indent=2)

    print("=" * 72)
    print(f"644 pools | weth_pre from t0+[0,{W}s] WETH-side swap-in")
    print(f"median weth_pre={dist['weth_pre_p50']:.3f} | corr w/ entry_weth={dist['corr_weth_pre_vs_entry_weth']:.3f}")
    print(f"minpre>=5 but weth_pre<0.1: {dist['minpre_ge5_weth_lt01']} pools (dust wash class)")
    print("=" * 72)
    print(f"{'MIN_WETH':>8} {'pass':>5} {'blk':>5} {'$/mo':>8} {'tr/mo':>6} {'fatal%':>7} {'miss$':>8} {'missW':>6}")
    bmo = baseline_pf["monthly_usd"]
    for r in results:
        pf = r["portfolio"]
        print(
            f"{r['min_weth_pre_eth']:>8.1f} {r['gate_pass_n']:>5} {r['blocked_vs_baseline']:>5} "
            f"{pf['monthly_usd']:>8.0f} {pf['trades_per_month']:>6.1f} "
            f"{pf['fatal_rate']*100:>6.1f}% {r['missed_pnl_usd_sum']:>8.0f} {r['blocked_missed_winners']:>6}"
        )
    print(f"\nBaseline: ${bmo:.0f}/mo  |  Wrote {out_json}")


if __name__ == "__main__":
    main()
