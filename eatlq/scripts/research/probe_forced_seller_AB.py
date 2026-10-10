#!/usr/bin/env python3
"""
A + B rigorous stress tests for the forced-seller mean-reversion edge.

A. RUG RATE + -100% STRESS:
   Every detected dump event is classified by what happens AFTER entry:
     - clean : a forward bar exists near entry+HOLD       -> real tick return
     - thin  : pool has later swaps but none near +HOLD   -> exit at nearest later bar
     - dead  : NO swaps at all after entry                -> -100% (cannot exit = rug)
   PF/WR reported under optimistic / realistic / pessimistic scenarios.

B. REAL SLIPPAGE via on-chain V3 math (NO archive node needed):
   Uses the sqrt_price_x96 + liquidity recorded on the entry/exit swaps to
   simulate buying $X of the alt with WETH (price impact + LP fee) and selling
   it back at exit. This replaces the idealized "tick = fill price" assumption.

Lookahead-safe: entry uses only info up to minute m. Clean-data window only.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

CH = "eatlq-clickhouse-1"
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
Q96 = 2 ** 96
BASE_ROUTING = (
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
)

RET_CAP, RET_FLOOR = 5.0, -1.0


def tick_to_return(entry_tick: int, exit_tick: int, weth_is_t1: bool) -> float:
    delta = (exit_tick - entry_tick) if weth_is_t1 else (entry_tick - exit_tick)
    delta = max(-16000, min(16000, delta))
    return max(RET_FLOOR, min(RET_CAP, math.pow(1.0001, delta) - 1.0))


def ch(sql: str, timeout: int = 900) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def fetch_bars(days: int, before: str | None) -> list[dict]:
    routing = ",".join(f"'{a}'" for a in BASE_ROUTING)
    before_clause = f"AND s.block_time < toDateTime('{before}', 'UTC')" if before else ""
    sql = f"""
    WITH alt AS (
        SELECT lower(pool_address) AS pool,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(token0_decimals) AS d0, any(token1_decimals) AS d1,
               any(fee_bps) AS fee_bps
        FROM eatlq.discovered_pools FINAL
        WHERE chain_id={CHAIN_ID} AND pool_type='v3'
          AND length(token0_address)=42 AND length(token1_address)=42
        GROUP BY pool
        HAVING (t0='{WETH}' OR t1='{WETH}')
           AND NOT (multiIf(t1='{WETH}', t0, t0='{WETH}', t1, '') IN ({routing}))
           AND multiIf(t1='{WETH}', t0, t0='{WETH}', t1, '') != ''
    )
    SELECT
        a.pool AS pool,
        toUnixTimestamp(toStartOfMinute(s.block_time)) AS m,
        (a.t1='{WETH}') AS weth_is_t1,
        a.fee_bps AS fee_bps,
        if(a.t1='{WETH}', a.d0, a.d1) AS alt_dec,
        argMax(s.tick, s.block_time) AS last_tick,
        toString(argMax(s.sqrt_price_x96, s.block_time)) AS last_sqrtp,
        toString(argMax(s.liquidity, s.block_time)) AS last_liq,
        sum(if(a.t1='{WETH}', if(s.amount1<0, toFloat64(-s.amount1), 0), if(s.amount0<0, toFloat64(-s.amount0), 0)))/1e18 AS weth_sell,
        sum(if(a.t1='{WETH}', if(s.amount1>0, toFloat64(s.amount1), 0), if(s.amount0>0, toFloat64(s.amount0), 0)))/1e18 AS weth_buy,
        count() AS n
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN alt AS a ON lower(s.pool_address)=a.pool
    WHERE s.chain_id={CHAIN_ID} AND s.block_time >= now() - INTERVAL {days} DAY
          AND s.block_time IS NOT NULL {before_clause}
    GROUP BY pool, m, weth_is_t1, fee_bps, alt_dec
    ORDER BY pool, m
    FORMAT JSONEachRow
    """
    rows = []
    for line in ch(sql).splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# --- V3 single-range price-impact math (archive-free, uses recorded state) ---

def v3_buy_alt_with_weth(weth_in_wei: float, sqrtp_x96: float, liq: float,
                         weth_is_t1: bool, fee_bps_meta: int) -> float:
    """Return alt tokens out (raw) for weth_in (wei) given recorded sqrtP, L.
    LP fee deducted on input. Single active-range approximation."""
    if liq <= 0 or sqrtp_x96 <= 0 or weth_in_wei <= 0:
        return 0.0
    fee = fee_bps_meta / 10000.0  # meta 1=0.01%
    amt_in = weth_in_wei * (1.0 - fee)
    sp = sqrtp_x96 / Q96  # real sqrt price = sqrt(token1/token0)
    if weth_is_t1:
        # WETH=token1 in -> price up -> receive token0 (alt)
        sp_new = sp + amt_in / liq
        # amountOut token0 = L*(1/sp - 1/sp_new)
        return liq * (1.0 / sp - 1.0 / sp_new)
    else:
        # WETH=token0 in -> price down -> receive token1 (alt)
        sp_new = (liq * sp) / (liq + amt_in * sp)
        return liq * (sp - sp_new)


def v3_sell_alt_for_weth(alt_in_raw: float, sqrtp_x96: float, liq: float,
                         weth_is_t1: bool, fee_bps_meta: int) -> float:
    """Return weth out (wei) for selling alt_in (raw) given recorded sqrtP, L."""
    if liq <= 0 or sqrtp_x96 <= 0 or alt_in_raw <= 0:
        return 0.0
    fee = fee_bps_meta / 10000.0
    amt_in = alt_in_raw * (1.0 - fee)
    sp = sqrtp_x96 / Q96
    if weth_is_t1:
        # sell token0 (alt) in -> price down -> receive token1 (WETH)
        sp_new = (liq * sp) / (liq + amt_in * sp)
        return liq * (sp - sp_new)
    else:
        # sell token1 (alt) in -> price up -> receive token0 (WETH)
        sp_new = sp + amt_in / liq
        return liq * (1.0 / sp - 1.0 / sp_new)


def stats(vals):
    if not vals:
        return {"n": 0}
    pos = sum(v for v in vals if v > 0); neg = abs(sum(v for v in vals if v < 0))
    return {
        "n": len(vals),
        "win_rate": round(sum(1 for v in vals if v > 0)/len(vals), 4),
        "mean": round(sum(vals)/len(vals), 6),
        "median": round(median(vals), 6),
        "pf": round(pos/neg, 4) if neg > 0 else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--before", default="2026-05-25")
    ap.add_argument("--drop", type=float, default=0.20)
    ap.add_argument("--win-min", type=int, default=5)
    ap.add_argument("--hold-min", type=int, default=30)
    ap.add_argument("--min-weth", type=float, default=0.05)
    ap.add_argument("--weth-in", type=float, default=0.006, help="WETH spent per trade (~$10)")
    ap.add_argument("--gas-usd", type=float, default=0.15, help="total gas for approve+buy+sell")
    ap.add_argument("--eth-usd", type=float, default=1665.0)
    args = ap.parse_args()

    print(f"[AB] fetch bars {args.days}d before={args.before} ...", flush=True)
    bars = fetch_bars(args.days, args.before or None)
    print(f"[AB] bars={len(bars):,}", flush=True)

    by_pool = defaultdict(list)
    for b in bars:
        by_pool[b["pool"]].append(b)

    win, hold, tol = args.win_min*60, args.hold_min*60, 120
    weth_in_wei = args.weth_in * 1e18
    gas_frac = (args.gas_usd / (args.weth_in * args.eth_usd))  # gas as fraction of trade

    clean, thin, dead = [], [], []      # tick-based scenario A
    slip_clean = []                      # B: real-slippage net returns (clean events only)
    # B-depth: (one_way_buy_impact, net_slippage_return, oos_is_test_bool, m)
    slip_detail = []
    pool_counts = defaultdict(int)
    test_cut = None

    for pool, blist in by_pool.items():
        blist.sort(key=lambda x: x["m"])
        ms = [b["m"] for b in blist]
        idx = {b["m"]: b for b in blist}
        weth_is_t1 = bool(blist[0]["weth_is_t1"])
        fee_bps = int(blist[0]["fee_bps"])

        def bar_near(target, lo, hi):
            best, bestd = None, None
            for mm in ms:
                if mm < lo or mm > hi:
                    continue
                d = abs(mm - target)
                if bestd is None or d < bestd:
                    bestd, best = d, mm
            return best

        for b in blist:
            m = b["m"]
            prior_m = bar_near(m - win, m - win - tol, m - win + tol)
            if prior_m is None or prior_m >= m:
                continue
            wsell_win = sum(idx[x]["weth_sell"] for x in ms if prior_m < x <= m)
            wbuy_win = sum(idx[x]["weth_buy"] for x in ms if prior_m < x <= m)
            drop = tick_to_return(int(idx[prior_m]["last_tick"]), int(b["last_tick"]), weth_is_t1)
            if drop > -args.drop or drop < -0.95:
                continue
            if wsell_win < args.min_weth or wsell_win <= wbuy_win:
                continue
            pool_counts[pool] += 1

            entry_tick = int(b["last_tick"])
            fwd_m = bar_near(m + hold, m + hold - tol, m + hold + tol)
            later = [x for x in ms if x > m]

            if fwd_m is not None:
                # clean: tick-based + real-slippage
                r_tick = tick_to_return(entry_tick, int(idx[fwd_m]["last_tick"]), weth_is_t1)
                clean.append(r_tick)
                cur_rtick = r_tick
                # B: real slippage using recorded sqrtP/L at entry and exit bars
                try:
                    sp_in = float(b["last_sqrtp"]); l_in = float(b["last_liq"])
                    fb = idx[fwd_m]
                    sp_out = float(fb["last_sqrtp"]); l_out = float(fb["last_liq"])
                    alt_out = v3_buy_alt_with_weth(weth_in_wei, sp_in, l_in, weth_is_t1, fee_bps)
                    weth_back = v3_sell_alt_for_weth(alt_out, sp_out, l_out, weth_is_t1, fee_bps)
                    if alt_out > 0 and weth_back > 0:
                        net = max(RET_FLOOR, min(RET_CAP, weth_back / weth_in_wei - 1.0 - gas_frac))
                        slip_clean.append(net)
                        sp_real = sp_in / Q96
                        mid = (sp_real * sp_real) if weth_is_t1 else (1.0 / (sp_real * sp_real))
                        eff = weth_in_wei / alt_out
                        impact = eff / mid - 1.0 if mid > 0 else 1.0
                        slip_detail.append((impact, net, m, cur_rtick))
                except (ValueError, ZeroDivisionError):
                    pass
            elif later:
                # thin: alive but no bar near horizon -> exit at nearest later bar
                ex = min(later, key=lambda x: abs(x - (m + hold)))
                thin.append(tick_to_return(entry_tick, int(idx[ex]["last_tick"]), weth_is_t1))
            else:
                # dead: no swaps at all after entry -> cannot exit = rug
                dead.append(-1.0)

    n_clean, n_thin, n_dead = len(clean), len(thin), len(dead)
    total = n_clean + n_thin + n_dead

    optimistic = clean
    realistic = clean + thin + dead
    pessimistic = clean + [-1.0]*n_thin + dead

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "event_classification": {
            "total": total,
            "clean": n_clean, "clean_pct": round(n_clean/total, 4) if total else 0,
            "thin": n_thin, "thin_pct": round(n_thin/total, 4) if total else 0,
            "dead_rug": n_dead, "dead_pct": round(n_dead/total, 4) if total else 0,
        },
        "concentration": {
            "unique_pools": len(pool_counts),
            "top_pool_share": round(max(pool_counts.values())/total, 4) if total and pool_counts else 0,
        },
        "A_scenario_optimistic_cleanonly": stats(optimistic),
        "A_scenario_realistic_dead_neg100": stats(realistic),
        "A_scenario_pessimistic_thin_and_dead_neg100": stats(pessimistic),
        "B_real_slippage_clean_events": stats(slip_clean),
        "B_vs_tick_mean_delta": (round(stats(slip_clean).get("mean",0) - stats(clean).get("mean",0), 6)
                                 if slip_clean and clean else None),
        "gas_frac_per_trade": round(gas_frac, 4),
    }
    # B-depth: edge survival in pools deep enough for our $size
    depth = {}
    for thr in (0.005, 0.01, 0.02, 0.05):
        sub = [net for imp, net, _, _ in slip_detail if abs(imp) < thr]
        depth[f"buy_impact_lt_{thr}"] = {**stats(sub),
                                         "share_of_clean": round(len(sub)/len(slip_detail), 4) if slip_detail else 0}
    out["B_depth_filtered_net_returns"] = depth

    # Anti-overfit: OOS time split on the deep (<2%) tradeable subset
    deep = sorted([(m, net, rt) for imp, net, m, rt in slip_detail if abs(imp) < 0.02],
                  key=lambda x: x[0])
    if deep:
        cut_idx = int(len(deep) * 0.8)
        cut_m = deep[cut_idx][0]
        out["B_deep_oos"] = {
            "split_date_utc": datetime.fromtimestamp(cut_m, timezone.utc).isoformat(),
            "slippage_train": stats([net for m, net, rt in deep if m < cut_m]),
            "slippage_test": stats([net for m, net, rt in deep if m >= cut_m]),
            "tick_ideal_train": stats([rt for m, net, rt in deep if m < cut_m]),
            "tick_ideal_test": stats([rt for m, net, rt in deep if m >= cut_m]),
        }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
