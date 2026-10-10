#!/usr/bin/env python3
"""
Right-tail label substrate (foundation for both P0 toxicity filter & P1 right-tail hunter).

Reframes the target away from "30min median PnL" toward what actually matters for a
small-position right-tail hunter:

  OUTCOME LABELS (measured forward from a realistic decision point = birth + 30min):
    mfe_6h / mfe_24h / mfe_72h   : max favorable alt/WETH price multiple (1.0 = flat)
    dead_6h / dead_24h / dead_72h: trading ceased before the horizon (rug/death proxy)
    sellable_after_entry         : any WETH-out (sell) swap exists after entry (CH proxy)
    time_to_5x_min / time_to_10x_min : minutes from entry to first 5x/10x (None if never)
    max_dd_before_5x             : worst drawdown from entry before first 5x (or end)

  EARLY FEATURES (observed in [birth, birth+30min] only -> no lookahead):
    early_swaps, early_wbuy, early_wsell, early_buysell_ratio, early_active_min,
    entry_impact_10usd, fee_bps, early_tick_range, weth_t1, birth_utc

Iron rules kept: no survivorship (dead pools INCLUDED), pre-entry-only features,
OOS by birth time. Data caveat: only pools EATLQ ingested (~9/day), not full Base flow.
"""
from __future__ import annotations

import argparse
import csv
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
MULT_CAP = 1000.0  # guard against bad-tick infinities
BASE_ROUTING = (
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
)


def ch(sql: str, timeout: int = 1800) -> str:
    r = subprocess.run(["docker", "exec", CH, "clickhouse-client", "--query", sql],
                       capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def mult(signed_tick: int, entry_signed: int) -> float:
    d = signed_tick - entry_signed
    d = max(-70000, min(70000, d))
    return min(MULT_CAP, math.pow(1.0001, d))


def v3_buy_impact(weth_in_wei: float, sp_x96: float, liq: float, weth_t1: bool, fee_meta: int):
    if liq <= 0 or sp_x96 <= 0:
        return None
    amt = weth_in_wei * (1.0 - fee_meta / 10000.0)
    sp = sp_x96 / Q96
    if weth_t1:
        spn = sp + amt / liq
        alt = liq * (1.0 / sp - 1.0 / spn)
        mid = sp * sp
    else:
        spn = (liq * sp) / (liq + amt * sp)
        alt = liq * (sp - spn)
        mid = 1.0 / (sp * sp)
    if alt <= 0 or mid <= 0:
        return None
    return (weth_in_wei / alt) / mid - 1.0


def fetch(days: int, born_before: str, horizon_h: int):
    routing = ",".join(f"'{a}'" for a in BASE_ROUTING)
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
    ),
    births AS (
        SELECT lower(s.pool_address) AS pool, min(s.block_time) AS birth
        FROM eatlq.uniswap_v3_swaps_decoded s
        INNER JOIN alt a ON lower(s.pool_address)=a.pool
        WHERE s.chain_id={CHAIN_ID} AND s.block_time IS NOT NULL
              AND s.block_time >= now()-INTERVAL {days} DAY
        GROUP BY pool
        HAVING birth < toDateTime('{born_before}','UTC')
    )
    SELECT a.pool AS pool,
           toUnixTimestamp(b.birth) AS birth,
           (a.t1='{WETH}') AS weth_t1, a.fee_bps AS fee_bps,
           toUnixTimestamp(toStartOfMinute(s.block_time)) AS m,
           max(s.tick) AS max_tick, min(s.tick) AS min_tick,
           argMax(s.tick, s.block_time) AS last_tick,
           toString(argMax(s.sqrt_price_x96, s.block_time)) AS sp,
           toString(argMax(s.liquidity, s.block_time)) AS liq,
           sum(if(a.t1='{WETH}', if(s.amount1<0, toFloat64(-s.amount1),0), if(s.amount0<0, toFloat64(-s.amount0),0)))/1e18 AS wsell,
           sum(if(a.t1='{WETH}', if(s.amount1>0, toFloat64(s.amount1),0), if(s.amount0>0, toFloat64(s.amount0),0)))/1e18 AS wbuy,
           count() AS n
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN alt AS a ON lower(s.pool_address)=a.pool
    INNER JOIN births AS b ON lower(s.pool_address)=b.pool
    WHERE s.chain_id={CHAIN_ID} AND s.block_time IS NOT NULL
          AND s.block_time >= b.birth
          AND s.block_time < b.birth + INTERVAL {horizon_h + 1} HOUR
    GROUP BY pool, birth, weth_t1, fee_bps, m
    ORDER BY pool, m
    FORMAT JSONEachRow
    """
    pools = defaultdict(list)
    meta = {}
    for line in ch(sql).splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        p = r["pool"]
        meta[p] = {"birth": int(r["birth"]), "weth_t1": int(r["weth_t1"]), "fee_bps": int(r["fee_bps"])}
        pools[p].append({
            "m": int(r["m"]), "max_tick": int(r["max_tick"]), "min_tick": int(r["min_tick"]),
            "last_tick": int(r["last_tick"]), "sp": r["sp"], "liq": r["liq"],
            "wsell": float(r["wsell"]), "wbuy": float(r["wbuy"]), "n": int(r["n"]),
        })
    return pools, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--born-before", default="2026-05-22")
    ap.add_argument("--obs-min", type=int, default=30, help="observation window = birth..birth+obs")
    ap.add_argument("--weth-in", type=float, default=0.006)
    ap.add_argument("--out-csv", default="/opt/EATLQ/mvp/diagnostics/righttail_labels.csv")
    args = ap.parse_args()

    print(f"[rt] fetch new pools born<{args.born_before}, {args.days}d ...", flush=True)
    pools, meta = fetch(args.days, args.born_before, 72)
    print(f"[rt] pools={len(pools):,}", flush=True)

    obs = args.obs_min * 60
    weth_in_wei = args.weth_in * 1e18
    rows = []

    for p, bl in pools.items():
        bl.sort(key=lambda x: x["m"])
        birth = meta[p]["birth"]; weth_t1 = bool(meta[p]["weth_t1"]); fee = meta[p]["fee_bps"]
        dp = birth + obs  # decision point

        pre = [b for b in bl if b["m"] <= dp]
        if not pre:
            continue
        entry_bar = pre[-1]
        # signed tick: higher = alt more expensive in WETH
        def sgn(t):
            return t if weth_t1 else -t
        entry_signed = sgn(entry_bar["last_tick"])

        # early features [birth, dp]
        e_swaps = sum(b["n"] for b in pre)
        e_wbuy = sum(b["wbuy"] for b in pre)
        e_wsell = sum(b["wsell"] for b in pre)
        e_active = len(pre)
        e_ticks = [b["last_tick"] for b in pre]
        e_tick_range = (max(e_ticks) - min(e_ticks)) if e_ticks else 0
        try:
            impact = v3_buy_impact(weth_in_wei, float(entry_bar["sp"]), float(entry_bar["liq"]), weth_t1, fee)
        except (ValueError, ZeroDivisionError):
            impact = None

        fwd = [b for b in bl if b["m"] > dp]
        last_m = bl[-1]["m"]

        def mfe(h):
            cap = dp + h * 3600
            best = 1.0
            for b in fwd:
                if b["m"] > cap:
                    break
                # favorable extreme within bar
                fav_tick = b["max_tick"] if weth_t1 else b["min_tick"]
                m_ = mult(sgn(fav_tick), entry_signed)
                if m_ > best:
                    best = m_
            return round(best, 4)

        def dead(h):
            return 1 if last_m < dp + h * 3600 else 0

        sellable = 1 if any(b["wsell"] > 0 for b in fwd) else 0

        # time-to-Nx and max drawdown before 5x (using last_tick path)
        t5 = t10 = None
        dd_before_5x = 1.0
        hit5 = False
        for b in fwd:
            cur = mult(sgn(b["last_tick"]), entry_signed)
            if not hit5 and cur < dd_before_5x:
                dd_before_5x = cur
            if t5 is None and cur >= 5.0:
                t5 = (b["m"] - dp) // 60; hit5 = True
            if t10 is None and cur >= 10.0:
                t10 = (b["m"] - dp) // 60

        rows.append({
            "pool": p, "birth_utc": datetime.fromtimestamp(birth, timezone.utc).isoformat(),
            "weth_t1": int(weth_t1), "fee_bps": fee,
            "early_swaps": e_swaps, "early_wbuy": round(e_wbuy, 6), "early_wsell": round(e_wsell, 6),
            "early_buysell_ratio": round(e_wbuy / e_wsell, 4) if e_wsell > 0 else None,
            "early_active_min": e_active, "early_tick_range": e_tick_range,
            "entry_impact_10usd": round(impact, 6) if impact is not None else None,
            "mfe_6h": mfe(6), "mfe_24h": mfe(24), "mfe_72h": mfe(72),
            "dead_6h": dead(6), "dead_24h": dead(24), "dead_72h": dead(72),
            "sellable_after_entry": sellable,
            "time_to_5x_min": t5, "time_to_10x_min": t10,
            "max_dd_before_5x": round(dd_before_5x, 4),
        })

    # write CSV
    if rows:
        with open(args.out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)

    # base-rate summary, OOS by birth time (oldest50 / mid25 / recent25)
    def seg_summary(sub):
        nn = len(sub)
        if nn == 0:
            return {"n": 0}
        def rate(key, thr):
            return round(sum(1 for r in sub if (r[key] or 0) >= thr) / nn, 4)
        return {
            "n": nn,
            "p_2x_24h": rate("mfe_24h", 2), "p_5x_24h": rate("mfe_24h", 5),
            "p_10x_24h": rate("mfe_24h", 10), "p_50x_24h": rate("mfe_24h", 50),
            "p_2x_72h": rate("mfe_72h", 2), "p_5x_72h": rate("mfe_72h", 5),
            "p_10x_72h": rate("mfe_72h", 10), "p_50x_72h": rate("mfe_72h", 50),
            "dead_24h_rate": round(sum(r["dead_24h"] for r in sub) / nn, 4),
            "dead_72h_rate": round(sum(r["dead_72h"] for r in sub) / nn, 4),
            "not_sellable_rate": round(sum(1 for r in sub if r["sellable_after_entry"] == 0) / nn, 4),
            "median_mfe_24h": round(median([r["mfe_24h"] for r in sub]), 4),
            "median_mfe_72h": round(median([r["mfe_72h"] for r in sub]), 4),
        }

    rows_sorted = sorted(rows, key=lambda r: r["birth_utc"])
    nseg = len(rows_sorted)
    seg = {
        "ALL": seg_summary(rows_sorted),
        "train_oldest50": seg_summary(rows_sorted[:nseg // 2]),
        "val_mid25": seg_summary(rows_sorted[nseg // 2:nseg * 3 // 4]),
        "recent_oos_newest25": seg_summary(rows_sorted[nseg * 3 // 4:]),
    }
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "n_pools_labeled": len(rows),
        "csv": args.out_csv,
        "caveat": "only EATLQ-ingested pools (~9/day), not full Base launch flow; right-censoring minimized by born_before cutoff",
        "right_tail_base_rates_oos": seg,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
