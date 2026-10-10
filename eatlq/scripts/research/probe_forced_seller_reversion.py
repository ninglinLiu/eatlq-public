#!/usr/bin/env python3
"""
PROBE: forced-seller mean reversion (exit-liquidity provision) on Base alt pools.

THEORY OF EDGE (human-owned, not data-mined):
  On Base alt pools there are PRICE-INSENSITIVE forced sellers — snipers/bots/
  early farmers whose cost basis is ~0 (first-block) and who dump the whole bag
  in a tight window to rotate capital or escape a feared rug. They optimize for
  CERTAINTY OF EXIT, not price. That forced selling overshoots to the downside.
  Patient capital that provides exit liquidity into the dump captures the bounce
  as price normalizes.

  WHO PAYS US, AND WHY:
    The forced seller pays us by dumping below fair value because they are
    price-insensitive. This is the OPPOSITE side from the onset/momentum game
    (where WE were the slow late FOMO buyer). Here our slowness is fine: we
    react AFTER the dump completes and hold minutes.

METHOD (lookahead-safe):
  - Universe: WETH-paired alt pools (target NOT base-routing), with metadata.
  - Minute bars per pool: last tick, WETH sell vol, WETH buy vol, n swaps.
  - Dump event at minute m: alt price dropped >= DROP over trailing WIN minutes,
    driven by sells (weth_sell > weth_buy) with weth_sell >= MIN_WETH.
  - Entry uses ONLY info up to minute m. Forward return at m+HOLD via tick.
  - Sellability: a forward bar must exist (pool keeps trading) else EXCLUDE
    (never fabricate a -100%).
  - Costs deducted. OOS split by date. Concentration + base-rate comparison.

This is a FIRST probe, not a verdict. Rigorous OOS + ablation + capacity come
before any live capital.
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
BASE_ROUTING = (
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
)


RET_CAP = 5.0     # +500% cap: beyond this a real fill is implausible (bad tick / no liquidity)
RET_FLOOR = -1.0  # -100% floor: cannot lose more than the position

def tick_to_return(entry_tick: int, exit_tick: int, weth_is_t1: bool) -> float:
    delta = (exit_tick - entry_tick) if weth_is_t1 else (entry_tick - exit_tick)
    # clamp delta so 1.0001^delta cannot overflow into nonsense (bad/extreme ticks)
    if delta > 16000:   # 1.0001^16000 ~ +396% ; above is implausible single move
        delta = 16000
    if delta < -16000:
        delta = -16000
    r = math.pow(1.0001, delta) - 1.0
    return max(RET_FLOOR, min(RET_CAP, r))


def ch(sql: str, timeout: int = 600) -> str:
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
               any(lower(token0_address)) AS t0,
               any(lower(token1_address)) AS t1
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
        argMax(s.tick, s.block_time) AS last_tick,
        sum(if(a.t1='{WETH}', if(s.amount1<0, toFloat64(-s.amount1), 0), if(s.amount0<0, toFloat64(-s.amount0), 0)))/1e18 AS weth_sell,
        sum(if(a.t1='{WETH}', if(s.amount1>0, toFloat64(s.amount1), 0), if(s.amount0>0, toFloat64(s.amount0), 0)))/1e18 AS weth_buy,
        count() AS n
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN alt AS a ON lower(s.pool_address)=a.pool
    WHERE s.chain_id={CHAIN_ID} AND s.block_time >= now() - INTERVAL {days} DAY
          AND s.block_time IS NOT NULL
          {before_clause}
    GROUP BY pool, m, weth_is_t1
    ORDER BY pool, m
    FORMAT JSONEachRow
    """
    rows = []
    for line in ch(sql).splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--drop", type=float, default=0.20, help="min alt price drop over window")
    ap.add_argument("--win-min", type=int, default=5, help="dump detection window (min)")
    ap.add_argument("--hold-min", type=int, default=30, help="forward hold (min)")
    ap.add_argument("--min-weth", type=float, default=0.05, help="min WETH sell volume in window")
    ap.add_argument("--cost", type=float, default=0.04, help="round-trip cost (fee+slip+gas)")
    ap.add_argument("--before", default="", help="exclude data on/after this UTC date (drop corrupted tail)")
    args = ap.parse_args()

    before = args.before or None
    print(f"[probe] fetching bars: alt pools, {args.days}d, before={before} ...", flush=True)
    bars = fetch_bars(args.days, before)
    print(f"[probe] bars={len(bars):,}", flush=True)

    # group by pool
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_pool[b["pool"]].append(b)

    win = args.win_min * 60
    hold = args.hold_min * 60
    tol = 120  # allow forward/back bar within +-2 min of target

    events = []      # forced-seller reversion entries
    base_rate = []   # unconditional forward returns (every Nth bar)
    base_stride = 7

    for pool, blist in by_pool.items():
        blist.sort(key=lambda x: x["m"])
        ms = [b["m"] for b in blist]
        tick = {b["m"]: int(b["last_tick"]) for b in blist}
        wsell = {b["m"]: float(b["weth_sell"]) for b in blist}
        wbuy = {b["m"]: float(b["weth_buy"]) for b in blist}
        weth_is_t1 = bool(blist[0]["weth_is_t1"])

        def bar_at(target, lo, hi):
            # nearest bar with timestamp in [target-? ] — pick closest within tol of target
            best = None; bestd = None
            for mm in ms:
                if mm < lo or mm > hi:
                    continue
                d = abs(mm - target)
                if bestd is None or d < bestd:
                    bestd = d; best = mm
            return best

        for i, b in enumerate(blist):
            m = b["m"]
            # base rate sample
            if i % base_stride == 0:
                fwd_m = bar_at(m + hold, m + hold - tol, m + hold + tol)
                if fwd_m is not None:
                    r = tick_to_return(tick[m], tick[fwd_m], weth_is_t1) - args.cost
                    base_rate.append(r)
            # dump detection: prior tick ~win ago
            prior_m = bar_at(m - win, m - win - tol, m - win + tol)
            if prior_m is None or prior_m >= m:
                continue
            # trailing window sell volume
            wsell_win = sum(wsell[x] for x in ms if prior_m < x <= m)
            wbuy_win = sum(wbuy[x] for x in ms if prior_m < x <= m)
            drop = tick_to_return(tick[prior_m], tick[m], weth_is_t1)  # alt return over window (negative = dump)
            # plausible dump only: between -95% and -DROP. <-95% in 5min ≈ rug/bad tick.
            if drop > -args.drop or drop < -0.95:
                continue
            if wsell_win < args.min_weth or wsell_win <= wbuy_win:
                continue
            # forward bar (sellability: must exist)
            fwd_m = bar_at(m + hold, m + hold - tol, m + hold + tol)
            if fwd_m is None:
                continue  # pool stopped trading -> exclude, do not fabricate
            fwd = tick_to_return(tick[m], tick[fwd_m], weth_is_t1) - args.cost
            events.append({
                "pool": pool, "m": m, "drop": round(drop, 4),
                "weth_sell": round(wsell_win, 4), "fwd_net": round(fwd, 6),
                "date": datetime.fromtimestamp(m, tz=timezone.utc).strftime("%Y-%m-%d"),
            })

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

    nets = [e["fwd_net"] for e in events]
    # OOS split by date
    dates = sorted({e["date"] for e in events})
    if dates:
        split = dates[int(len(dates)*0.67)]
        train = [e["fwd_net"] for e in events if e["date"] < split]
        test = [e["fwd_net"] for e in events if e["date"] >= split]
    else:
        split, train, test = None, [], []

    # concentration
    pool_counts = defaultdict(int)
    for e in events:
        pool_counts[e["pool"]] += 1
    top_pool_share = (max(pool_counts.values())/len(events)) if events else 0

    # PF after removing top-1 trade (echo-alpha guard)
    pf_top1 = None
    if len(nets) > 1:
        trimmed = sorted(nets)[:-1]
        pos = sum(v for v in trimmed if v > 0); neg = abs(sum(v for v in trimmed if v < 0))
        pf_top1 = round(pos/neg, 4) if neg > 0 else None

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "thesis": "forced-seller mean reversion / exit-liquidity provision",
        "params": vars(args),
        "universe_pools_with_bars": len(by_pool),
        "base_rate_unconditional": stats(base_rate),
        "event_all": stats(nets),
        "event_pf_top1_removed": pf_top1,
        "event_oos_train": stats(train),
        "event_oos_test": stats(test),
        "oos_split_date": split,
        "concentration": {
            "unique_pools": len(pool_counts),
            "top_pool_share": round(top_pool_share, 4),
        },
        "edge_vs_base_mean": (round(stats(nets).get("mean", 0) - stats(base_rate).get("mean", 0), 6)
                              if nets and base_rate else None),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
