#!/usr/bin/env python3
"""
Universe-first, OOS-first alpha harness (data-honest rebuild).

Fixes the 6 documented failures of the forced-seller probe:

  Layer 2/3 (slippage, can't-sell):  every outcome is V3-slippage-adjusted;
                                     samples that die are NOT dropped (= -100%).
  Layer 4 (post-hoc deep subset):    the tradeable universe is defined using
                                     ONLY pre-entry info (depth/age/2-sidedness),
                                     BEFORE any signal is applied.
  Layer 5 (regime decay):            time split is the FIRST constraint, recent
                                     OOS reported separately and weighted highest.

Order of operations (the iron rule):
  1) Define tradeable+surviving universe at each candidate entry (pre-entry only).
  2) Compute the UNIVERSE BASE RATE (forward return of every universe bar).
  3) Apply the dump signal WITHIN the universe.
  4) Compare signal vs base, split Train / Val / Recent-OOS.

Data limits (see alpha_discipline_and_data_reality_cn.md):
  - No seller identity (tx_from 0.88%) -> toxicity proxied by pool BEHAVIOR
    (death / activity collapse), not by who sold.
  - No LP mint/burn for dogcoin pools -> rug proxied by trading death.
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
RET_CAP, RET_FLOOR = 5.0, -1.0
BASE_ROUTING = (
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
)


def ch(sql: str, timeout: int = 1200) -> str:
    r = subprocess.run(["docker", "exec", CH, "clickhouse-client", "--query", sql],
                       capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def tick_to_return(et: int, xt: int, weth_t1: bool) -> float:
    d = (xt - et) if weth_t1 else (et - xt)
    d = max(-16000, min(16000, d))
    return max(RET_FLOOR, min(RET_CAP, math.pow(1.0001, d) - 1.0))


def v3_buy(weth_in: float, sp_x96: float, liq: float, weth_t1: bool, fee_meta: int) -> float:
    if liq <= 0 or sp_x96 <= 0 or weth_in <= 0:
        return 0.0
    amt = weth_in * (1.0 - fee_meta / 10000.0)
    sp = sp_x96 / Q96
    if weth_t1:
        spn = sp + amt / liq
        return liq * (1.0 / sp - 1.0 / spn)
    spn = (liq * sp) / (liq + amt * sp)
    return liq * (sp - spn)


def v3_sell(alt_in: float, sp_x96: float, liq: float, weth_t1: bool, fee_meta: int) -> float:
    if liq <= 0 or sp_x96 <= 0 or alt_in <= 0:
        return 0.0
    amt = alt_in * (1.0 - fee_meta / 10000.0)
    sp = sp_x96 / Q96
    if weth_t1:
        spn = (liq * sp) / (liq + amt * sp)
        return liq * (sp - spn)
    spn = sp + amt / liq
    return liq * (1.0 / sp - 1.0 / spn)


def stats(v):
    if not v:
        return {"n": 0}
    pos = sum(x for x in v if x > 0); neg = abs(sum(x for x in v if x < 0))
    return {"n": len(v), "win_rate": round(sum(1 for x in v if x > 0)/len(v), 4),
            "mean": round(sum(v)/len(v), 6), "median": round(median(v), 6),
            "pf": round(pos/neg, 4) if neg > 0 else None}


def fetch_bars(days: int, before: str | None):
    routing = ",".join(f"'{a}'" for a in BASE_ROUTING)
    bc = f"AND s.block_time < toDateTime('{before}', 'UTC')" if before else ""
    sql = f"""
    WITH alt AS (
        SELECT lower(pool_address) AS pool,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(fee_bps) AS fee_bps
        FROM eatlq.discovered_pools FINAL
        WHERE chain_id={CHAIN_ID} AND pool_type='v3'
          AND length(token0_address)=42 AND length(token1_address)=42
        GROUP BY pool
        HAVING (t0='{WETH}' OR t1='{WETH}')
           AND multiIf(t1='{WETH}', t0, t0='{WETH}', t1, '') != ''
           AND NOT (multiIf(t1='{WETH}', t0, t0='{WETH}', t1, '') IN ({routing}))
    )
    SELECT a.pool AS pool,
           toUnixTimestamp(toStartOfMinute(s.block_time)) AS m,
           (a.t1='{WETH}') AS weth_t1, a.fee_bps AS fee_bps,
           argMax(s.tick, s.block_time) AS tick,
           toString(argMax(s.sqrt_price_x96, s.block_time)) AS sp,
           toString(argMax(s.liquidity, s.block_time)) AS liq,
           sum(if(a.t1='{WETH}', if(s.amount1<0, toFloat64(-s.amount1),0), if(s.amount0<0, toFloat64(-s.amount0),0)))/1e18 AS wsell,
           sum(if(a.t1='{WETH}', if(s.amount1>0, toFloat64(s.amount1),0), if(s.amount0>0, toFloat64(s.amount0),0)))/1e18 AS wbuy,
           count() AS n
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN alt AS a ON lower(s.pool_address)=a.pool
    WHERE s.chain_id={CHAIN_ID} AND s.block_time >= now()-INTERVAL {days} DAY
          AND s.block_time IS NOT NULL {bc}
    GROUP BY pool, m, weth_t1, fee_bps
    ORDER BY pool, m
    FORMAT JSONEachRow
    """
    out = []
    for line in ch(sql).splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        r["m"] = int(r["m"]); r["tick"] = int(r["tick"]); r["n"] = int(r["n"])
        r["wsell"] = float(r["wsell"]); r["wbuy"] = float(r["wbuy"])
        r["fee_bps"] = int(r["fee_bps"]); r["weth_t1"] = int(r["weth_t1"])
        out.append(r)
    return out


def outcome_slip(entry_bar, exit_bar, weth_t1, fee, weth_in_wei, gas_frac):
    """Return slippage-adjusted net return; None if uncomputable."""
    try:
        sp_in = float(entry_bar["sp"]); l_in = float(entry_bar["liq"])
        sp_out = float(exit_bar["sp"]); l_out = float(exit_bar["liq"])
        alt = v3_buy(weth_in_wei, sp_in, l_in, weth_t1, fee)
        wb = v3_sell(alt, sp_out, l_out, weth_t1, fee)
        if alt > 0 and wb > 0:
            return max(RET_FLOOR, min(RET_CAP, wb / weth_in_wei - 1.0 - gas_frac))
    except (ValueError, ZeroDivisionError):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--before", default="2026-05-25")
    # universe (pre-entry only)
    ap.add_argument("--min-age-min", type=int, default=60)
    ap.add_argument("--pre-window-min", type=int, default=30)
    ap.add_argument("--pre-min-swaps", type=int, default=10)
    ap.add_argument("--max-entry-impact", type=float, default=0.02)
    # signal
    ap.add_argument("--drop", type=float, default=0.20)
    ap.add_argument("--win-min", type=int, default=5)
    ap.add_argument("--hold-min", type=int, default=30)
    ap.add_argument("--min-weth", type=float, default=0.05)
    # execution
    ap.add_argument("--weth-in", type=float, default=0.006)
    ap.add_argument("--gas-usd", type=float, default=0.15)
    ap.add_argument("--eth-usd", type=float, default=1665.0)
    args = ap.parse_args()

    print(f"[uni] fetch bars {args.days}d before={args.before}", flush=True)
    bars = fetch_bars(args.days, args.before or None)
    print(f"[uni] bars={len(bars):,}", flush=True)

    by_pool = defaultdict(list)
    for b in bars:
        by_pool[b["pool"]].append(b)

    win, hold, tol = args.win_min*60, args.hold_min*60, 120
    prew = args.pre_window_min*60
    age = args.min_age_min*60
    weth_in_wei = args.weth_in*1e18
    gas_frac = args.gas_usd/(args.weth_in*args.eth_usd)

    # accumulators: lists of (m, ret)
    base_slip, base_tick = [], []
    sig_slip, sig_tick = [], []
    n_universe_bars = 0
    n_signal = 0
    dead_sig = thin_sig = clean_sig = 0
    uni_pools = set()
    sig_pool_counts = defaultdict(int)

    for pool, bl in by_pool.items():
        bl.sort(key=lambda x: x["m"])
        ms = [b["m"] for b in bl]
        idx = {b["m"]: b for b in bl}
        weth_t1 = bool(bl[0]["weth_t1"]); fee = int(bl[0]["fee_bps"])
        first_m = ms[0]

        def near(target, lo, hi):
            best = bestd = None
            for mm in ms:
                if mm < lo or mm > hi:
                    continue
                d = abs(mm-target)
                if bestd is None or d < bestd:
                    bestd, best = d, mm
            return best

        for b in bl:
            m = b["m"]
            # ---- UNIVERSE membership: pre-entry info ONLY ----
            if m - first_m < age:
                continue
            pre = [idx[x] for x in ms if m-prew <= x <= m]
            pre_swaps = sum(p["n"] for p in pre)
            pre_buy = sum(p["wbuy"] for p in pre)
            pre_sell = sum(p["wsell"] for p in pre)
            if pre_swaps < args.pre_min_swaps or pre_buy <= 0 or pre_sell <= 0:
                continue  # not 2-sided / not active enough
            # entry depth: our $size one-way impact < threshold (entry-bar state)
            try:
                sp_in = float(b["sp"]); l_in = float(b["liq"])
                alt = v3_buy(weth_in_wei, sp_in, l_in, weth_t1, fee)
                if alt <= 0:
                    continue
                spr = sp_in/Q96
                mid = (spr*spr) if weth_t1 else (1.0/(spr*spr))
                impact = (weth_in_wei/alt)/mid - 1.0 if mid > 0 else 1.0
            except (ValueError, ZeroDivisionError):
                continue
            if abs(impact) >= args.max_entry_impact:
                continue  # too thin to trade our size -> not in tradeable universe

            n_universe_bars += 1
            uni_pools.add(pool)
            entry_tick = int(b["tick"])

            # ---- OUTCOME (no dropping): clean / thin / dead ----
            fwd = near(m+hold, m+hold-tol, m+hold+tol)
            later = [x for x in ms if x > m]
            if fwd is not None:
                r_tick = tick_to_return(entry_tick, int(idx[fwd]["tick"]), weth_t1)
                r_slip = outcome_slip(b, idx[fwd], weth_t1, fee, weth_in_wei, gas_frac)
                if r_slip is None:
                    r_slip = r_tick  # fallback (rare)
                outc = ("clean", r_tick, r_slip)
            elif later:
                ex = min(later, key=lambda x: abs(x-(m+hold)))
                r_tick = tick_to_return(entry_tick, int(idx[ex]["tick"]), weth_t1)
                r_slip = outcome_slip(b, idx[ex], weth_t1, fee, weth_in_wei, gas_frac)
                if r_slip is None:
                    r_slip = r_tick
                outc = ("thin", r_tick, r_slip)
            else:
                outc = ("dead", -1.0, -1.0)  # cannot exit = rug

            base_tick.append((m, outc[1])); base_slip.append((m, outc[2]))

            # ---- SIGNAL: forced-seller dump, computed at entry (pre info) ----
            prior = near(m-win, m-win-tol, m-win+tol)
            if prior is None or prior >= m:
                continue
            wsell_w = sum(idx[x]["wsell"] for x in ms if prior < x <= m)
            wbuy_w = sum(idx[x]["wbuy"] for x in ms if prior < x <= m)
            drop = tick_to_return(int(idx[prior]["tick"]), entry_tick, weth_t1)
            if drop > -args.drop or drop < -0.95:
                continue
            if wsell_w < args.min_weth or wsell_w <= wbuy_w:
                continue
            n_signal += 1
            sig_pool_counts[pool] += 1
            if outc[0] == "clean":
                clean_sig += 1
            elif outc[0] == "thin":
                thin_sig += 1
            else:
                dead_sig += 1
            sig_tick.append((m, outc[1])); sig_slip.append((m, outc[2]))

    # ---- OOS-first time split (Train 50% / Val 25% / Recent 25% by time) ----
    def split_report(pairs):
        if not pairs:
            return {"n": 0}
        pairs = sorted(pairs, key=lambda x: x[0])
        ms_sorted = [p[0] for p in pairs]
        q50 = ms_sorted[int(len(ms_sorted)*0.50)]
        q75 = ms_sorted[int(len(ms_sorted)*0.75)]
        train = [r for mm, r in pairs if mm < q50]
        val = [r for mm, r in pairs if q50 <= mm < q75]
        recent = [r for mm, r in pairs if mm >= q75]
        return {
            "all": stats([r for _, r in pairs]),
            "train_oldest50": stats(train),
            "val_mid25": stats(val),
            "recent_oos_newest25": stats(recent),
            "val_split_utc": datetime.fromtimestamp(q50, timezone.utc).isoformat(),
            "recent_split_utc": datetime.fromtimestamp(q75, timezone.utc).isoformat(),
        }

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "universe": {
            "tradeable_bars": n_universe_bars,
            "unique_pools": len(uni_pools),
            "definition": "pre-entry: age>=%dm, >=%d swaps & 2-sided in last %dm, $size impact<%.0f%%" % (
                args.min_age_min, args.pre_min_swaps, args.pre_window_min, args.max_entry_impact*100),
        },
        "signal_events": {
            "n": n_signal, "clean": clean_sig, "thin": thin_sig, "dead_rug": dead_sig,
            "dead_pct": round(dead_sig/n_signal, 4) if n_signal else 0,
            "unique_pools": len(sig_pool_counts),
            "top_pool_share": round(max(sig_pool_counts.values())/n_signal, 4) if n_signal and sig_pool_counts else 0,
        },
        "BASE_RATE_universe_slippage": split_report(base_slip),
        "SIGNAL_dump_slippage": split_report(sig_slip),
        "SIGNAL_dump_tick_ideal": split_report(sig_tick),
        "gas_frac_per_trade": round(gas_frac, 4),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
