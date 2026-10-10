#!/usr/bin/env python3
"""FRONT B — Base early-ramp WICK-SHAKEOUT microstructure (the user's DEX idea).

HYPOTHESIS: a sharp down-wick early in a new pool (operator shakes out paper
hands / clears panic snipers) WHILE the distinct-buyer count keeps RISING =
accumulation, not death -> positive forward return.

DATA: ClickHouse eatlq.uniswap_v3_swaps_decoded (Base, to 2026-06-08), joined to
discovered_pools for WETH orientation. Price path reconstructed from `tick`
(decimals & orientation cancel in returns). Distinct buyers = distinct recipient
on memecoin-out swaps.

CAVEAT (honest): outcome is a PRICE-PATH proxy (tick close), NOT a QuoterV2
realized sellable round-trip (RPC is sealed). True executable EV would be WORSE
(slippage in thin pools, sell-fail). So a NEGATIVE verdict here is conservative-safe;
a POSITIVE verdict would still need QuoterV2 confirmation before any canary.

SIGNAL (first 30m, no look-ahead): exists a 5-min bar whose intrabar low is
>= WICK_DROP below the prior bar's close AND recovers >= WICK_REC off that low,
AND distinct buyers in min[15,30) > min[0,15).
ENTRY at +30m (signal fully formed), EXIT at +150m (2h hold). cost = COST proxy.
GATES: lift>=1.3, bootstrap p5>1, n>=30, placebo, OOS by pool-creation date.
"""
from __future__ import annotations
import json
import math
import random
import statistics as st
import subprocess
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
LN1P = math.log(1.0001)
NBUCK = 30                 # 30 x 5min = 150 min
SIG_BUCKETS = 6            # first 30 min = signal window
ENTRY_B = 6                # enter at +30m (bucket 6 first tick)
EXIT_B = 29                # exit ~+150m
WICK_DROP = 0.08           # intrabar low >=8% below prior close
WICK_REC = 0.04            # recover >=4% off the low (close)
COST = 0.02                # round-trip cost proxy (V3 alt; real is worse)
DATE_LO = "2026-03-01"
DATE_HI = "2026-06-06"     # leave +2.5h forward room before 06-08 cutoff
MIN_SWAPS = 40             # tradeable pools only


SQL = f"""
WITH cand AS (
  SELECT pool_address,
         lower(token0_address) t0addr, lower(token1_address) t1addr
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0, count() ns
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL
  GROUP BY pool_address
  HAVING ns >= {MIN_SWAPS}
         AND t0 >= '{DATE_LO} 00:00:00' AND t0 <= '{DATE_HI} 23:59:59'
)
SELECT s.pool_address AS pool,
       if(c.t0addr = '{WETH}', -1, 1) AS orient,   -- +1 if memecoin=token0 else -1
       toUInt32(floor(dateDiff('second', fs.t0, s.block_time) / 300)) AS b,
       argMin(s.tick, s.block_time) AS first_tick,
       argMax(s.tick, s.block_time) AS last_tick,
       min(s.tick) AS min_tick,
       max(s.tick) AS max_tick,
       uniqExact(s.recipient) AS buyers,
       count() AS n
FROM eatlq.uniswap_v3_swaps_decoded s
INNER JOIN fs ON s.pool_address = fs.pool_address
INNER JOIN cand c ON s.pool_address = c.pool_address
WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
  AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {NBUCK*300}
GROUP BY pool, orient, b, fs.t0
ORDER BY pool, b
FORMAT TabSeparated
"""


def run_ch(sql):
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:500])
    return r.stdout


def logp(tick, orient):
    return tick * LN1P * orient   # relative log-price of the memecoin


def main():
    print("#### FRONT B: Base early down-wick shakeout + buyers rising -> forward 2h return ####")
    print("#### outcome = tick price-path proxy (NOT QuoterV2 realized); negative = conservative ####\n")
    raw = run_ch(SQL)
    pools = defaultdict(dict)
    orient = {}
    t0day = {}
    for line in raw.strip().split("\n"):
        if not line:
            continue
        pool, ori, b, ft, lt, mn, mx, buyers, n = line.split("\t")
        orient[pool] = int(ori)
        pools[pool][int(b)] = {
            "first": int(ft), "last": int(lt), "min": int(mn), "max": int(mx),
            "buyers": int(buyers), "n": int(n)}
    print(f"candidate pools with early buckets: {len(pools)}")

    rows = []
    for pool, buckets in pools.items():
        ori = orient[pool]
        if ENTRY_B not in buckets or max(buckets) < EXIT_B - 5:
            continue
        # signal: down-wick in first SIG_BUCKETS with recovery + buyers rising
        wick = False
        for b in range(1, SIG_BUCKETS):
            if b not in buckets or (b - 1) not in buckets:
                continue
            prev_close = logp(buckets[b - 1]["last"], ori)
            bar_low = logp(min(buckets[b]["min"], buckets[b]["max"])  # low price = min memecoin price
                           if ori == 1 else max(buckets[b]["min"], buckets[b]["max"]), ori)
            bar_close = logp(buckets[b]["last"], ori)
            drop = bar_low - prev_close          # log return (negative = down)
            rec = bar_close - bar_low
            if drop <= math.log(1 - WICK_DROP) and rec >= math.log(1 + WICK_REC):
                wick = True
                break
        early_buyers = sum(buckets[b]["buyers"] for b in range(0, 3) if b in buckets)
        late_buyers = sum(buckets[b]["buyers"] for b in range(3, 6) if b in buckets)
        buyers_rising = late_buyers > early_buyers
        signal = 1 if (wick and buyers_rising) else 0
        # outcome: entry +30m, exit ~+150m (last available bucket >= EXIT_B-5)
        ex_b = max(b for b in buckets if b <= EXIT_B)
        p_entry = logp(buckets[ENTRY_B]["first"], ori)
        p_exit = logp(buckets[ex_b]["last"], ori)
        ret = math.exp(p_exit - p_entry) * (1 - COST) - 1
        rows.append({"pool": pool, "signal": signal, "wick": int(wick),
                     "rising": int(buyers_rising), "ret": ret, "win": 1 if ret > 0 else 0,
                     "t0b": min(buckets)})

    n = len(rows)
    if n == 0:
        print("no priceable candidate pools in window."); return
    base = sum(r["win"] for r in rows) / n
    allret = [r["ret"] for r in rows]
    print(f"priceable candidates: {n}  forward-win base rate: {100*base:.1f}%  "
          f"all EV={100*sum(allret)/n:+.1f}% median={100*st.median(allret):+.1f}%")

    def lift_ci(sub_rows, pred, n_boot=1500, seed=1):
        wins = [r["win"] for r in sub_rows]
        flags = [1 if pred(r) else 0 for r in sub_rows]
        m = len(sub_rows); nsel = sum(flags)
        b = sum(wins) / m if m else 0
        if nsel == 0 or b == 0:
            return None
        nws = sum(w for w, f in zip(wins, flags) if f)
        lift = (nws / nsel) / b
        rng = random.Random(seed); lifts = []
        for _ in range(n_boot):
            sw = ss = sws = 0
            for _ in range(m):
                i = rng.randrange(m); sw += wins[i]
                if flags[i]:
                    ss += 1; sws += wins[i]
            if ss and sw:
                lifts.append((sws / ss) / (sw / m))
        lifts.sort()
        return dict(n_sel=nsel, n_win=nws, lift=lift, p5=lifts[int(0.05 * len(lifts))] if lifts else 0,
                    ev=sum(r["ret"] for r, f in zip(sub_rows, flags) if f) / nsel)

    def placebo(sub_rows, pred, n=200, seed=5):
        rng = random.Random(seed)
        labels = [r["win"] for r in sub_rows]
        flags = [1 if pred(r) else 0 for r in sub_rows]
        nsel = sum(flags)
        if not nsel:
            return 0
        out = []
        for _ in range(n):
            rng.shuffle(labels)
            bb = sum(labels) / len(labels)
            if bb:
                out.append((sum(l for l, f in zip(labels, flags) if f) / nsel) / bb)
        return st.mean(out) if out else 0

    # OOS by pool-creation bucket-time proxy: split by pool order (created order ~ t0)
    rows.sort(key=lambda r: r["pool"])  # stable; real split below by half
    half = n // 2
    train, test = rows[:half], rows[half:]

    print("\n=== CASE-CONTROL: signal -> P(forward win) (gates: lift>=1.3 & p5>1 & n>=30 & placebo~1) ===")
    for nm, fn in [("down-wick only", lambda r: r["wick"] == 1),
                   ("buyers rising only", lambda r: r["rising"] == 1),
                   ("WICK + buyers rising", lambda r: r["signal"] == 1)]:
        full = lift_ci(rows, fn)
        if not full:
            print(f"  {nm:22s} (insufficient)"); continue
        pl = placebo(rows, fn)
        ok = full["lift"] >= 1.3 and full["p5"] > 1.0 and full["n_sel"] >= 30 and abs(pl - 1) < 0.2
        print(f"  {nm:22s} lift={full['lift']:.2f}(p5={full['p5']:.2f}) n={full['n_sel']} "
              f"selEV={100*full['ev']:+.1f}% placebo={pl:.2f} -> {'PASS' if ok else 'fail'}")

    print("\n=== OOS (split halves) — signal must hold in BOTH ===")
    for nm, fn in [("WICK + buyers rising", lambda r: r["signal"] == 1)]:
        a = lift_ci(train, fn); b = lift_ci(test, fn)
        if a and b:
            print(f"  {nm:22s} half1 lift={a['lift']:.2f}(selEV {100*a['ev']:+.1f}%,n{a['n_sel']}) | "
                  f"half2 lift={b['lift']:.2f}(selEV {100*b['ev']:+.1f}%,n{b['n_sel']}) "
                  f"-> {'consistent' if a['lift']>=1.3 and b['lift']>=1.3 else 'FLIPS/weak'}")


if __name__ == "__main__":
    main()
