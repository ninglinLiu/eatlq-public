#!/usr/bin/env python3
"""REGIME PROBE — daily GREEN/RED light for memecoin right-tail regime (READ-ONLY).

The single highest-value tool: it answers "should we trade AT ALL today?".
Definition (causal, no look-ahead): among WETH pools whose first swap was 4-10h
ago (so their first-4h window has already fully closed), take the MEDIAN meme-side
return from pool age 10min -> 4h. GREEN if > 0 (right-tail regime present), RED
otherwise (stand down / stay in cash).

Run daily / hourly. Point at Base now; same logic ports to any chain's swaps +
discovered-pool registry (just change CH/Redis env + WETH address + chain_id).

Usage:
  python3 regime_probe.py                 # live reading now
  python3 regime_probe.py --lookback 24   # widen maturity window to 24h
"""
from __future__ import annotations
import argparse, math, subprocess, json
import numpy as np

LN1P = math.log(1.0001)
WETH = "0x4200000000000000000000000000000000000006"
CH = "eatlq-clickhouse-1"
REDIS = "eatlq-redis-1"
CHAIN = 8453
GREEN_MIN_POOLS = 5   # need at least this many matured pools to call regime


def ch(q):
    r = subprocess.run(["docker", "exec", CH, "clickhouse-client", "--query", q],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()


def redis_orient(pool):
    """orient = -1 if token0 is WETH else +1 (meme is the non-WETH leg)."""
    r = subprocess.run(["docker", "exec", REDIS, "redis-cli", "hget",
                        f"pool:discovered:{pool}", "token0_address"],
                       capture_output=True, text=True)
    t0 = (r.stdout or "").strip().strip('"').lower()
    if not t0:
        return None
    return -1 if t0 == WETH else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=10, help="maturity window upper bound (hours ago)")
    args = ap.parse_args()

    # pools whose first swap matured 4h..lookback-h ago -> their 4h window has closed
    q = f"""
    WITH recent AS (
      SELECT pool_address, min(block_time) t0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN} AND block_time >= now() - INTERVAL {args.lookback+5} HOUR
      GROUP BY pool_address
      HAVING t0 >= now() - INTERVAL {args.lookback} HOUR AND t0 <= now() - INTERVAL 5 HOUR
    )
    SELECT s.pool_address,
      argMinIf(s.tick, s.block_time, dateDiff('second', r.t0, s.block_time) >= 600)  AS tick_entry,
      argMaxIf(s.tick, s.block_time, dateDiff('second', r.t0, s.block_time) <= 14400) AS tick_4h,
      countIf(dateDiff('second', r.t0, s.block_time) <= 14400) AS ns_4h
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN recent r ON s.pool_address = r.pool_address
    WHERE s.chain_id={CHAIN} AND s.block_time >= now() - INTERVAL {args.lookback+5} HOUR
    GROUP BY s.pool_address
    FORMAT TabSeparated
    """
    all_rows = [l.split("\t") for l in ch(q).splitlines() if l]
    matured = len(all_rows)
    survived = sum(1 for _, te, _, _ in all_rows if te not in ("\\N", ""))
    survival_rate = survived / matured if matured else 0.0
    rows = [r for r in all_rows if r[3] not in ("\\N", "") and int(r[3]) >= 5]
    rets = []
    n_died_early = 0
    for pool, te, t4, ns in rows:
        if te in ("\\N", "") or t4 in ("\\N", ""):
            n_died_early += 1        # no swap survived to the 10min entry point
            continue
        o = redis_orient(pool)
        if o is None:
            continue
        r = math.exp((int(t4) - int(te)) * LN1P * o) - 1.0
        # cap crazy tick-glitch outliers at +/-100x so median/mean stay meaningful
        rets.append(max(min(r, 100.0), -1.0))

    n = len(rets)
    print(f"[REGIME PROBE] chain={CHAIN}  window=4h..{args.lookback}h ago")
    print(f"  new pools matured={matured}  survived-to-10min={survived} ({survival_rate:.0%})  "
          f"usable(>=5 swaps + orient)={n}")

    # Signal 1 (orient-free, always available): survival rate. A dead tape where
    # almost no new pool survives to the entry window is a RED regime by itself.
    if survival_rate < 0.10 or survived < GREEN_MIN_POOLS:
        print(f"  => 🔴 RED (dead tape: only {survival_rate:.0%} of new pools survive to entry) "
              f"— 空仓/持稳定币,不要方向性交易")
        print(json.dumps({"light": "red", "reason": "dead_tape",
                          "survival_rate": survival_rate, "survived": survived}))
        return
    if n < GREEN_MIN_POOLS:
        print("  => ⚪ INSUFFICIENT return data (survivors alive but orient coverage low) — 保守视为 RED")
        print(json.dumps({"light": "red", "reason": "low_orient_coverage", "n": n}))
        return

    # Signal 2 (magnitude): median meme 4h return among survivors.
    a = np.array(rets)
    med, mean = float(np.median(a)), float(a.mean())
    wr, p2 = float((a > 0).mean()), float((a >= 1).mean())
    light = "🟢 GREEN" if med > 0 else "🔴 RED"
    verdict = "可以按存活者篮子出手" if med > 0 else "空仓/持稳定币,不要方向性交易"
    print(f"  median 4h meme return = {med:+.1%}   mean = {mean:+.1%}   WR(>0) = {wr:.0%}   share>2x = {p2:.0%}")
    print(f"  => {light}   {verdict}")
    print(json.dumps({"light": "green" if med > 0 else "red", "median": med,
                      "mean": mean, "wr": wr, "n": n, "survival_rate": survival_rate}))


if __name__ == "__main__":
    main()
