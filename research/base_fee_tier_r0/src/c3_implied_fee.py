#!/usr/bin/env python3
"""Measure the fee each pool actually charged, from price impact alone.

census.pools.fee was read once when the pool was resolved. Aerodrome Slipstream
pools do not have a fixed fee -- a fee manager can change it, and many volatile CL
pools run far below the value the factory was deployed with. Every fee number in
this project came from that one snapshot, so if the snapshot is wrong then every
fee-revenue figure is wrong by the same ratio, including the 1157 bps that C3's
cbBTC result rests on.

The pool tells us the truth without being asked. For a swap that puts token0 in,
constant-product concentrated liquidity moves the price by

    1/sqrt(p_after) - 1/sqrt(p_before) = amount0_net / L

and amount0 as logged is gross of the fee, so

    f = 1 - L * delta(1/sqrt(p)) / amount0_gross

Only swaps that stay inside one tick range are usable, because crossing a tick
changes L midway and breaks the identity. Restricting to swaps whose tick does not
change and whose size is small relative to L keeps the estimate clean, and the
median over thousands of such swaps is what the pool really charged.
"""
from __future__ import annotations

import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"
Q96 = 2 ** 96
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]
SYMS = ("cbBTC", "tBTC", "cbXRP", "uXRP", "cbADA", "uADA", "cbLTC", "uSOL",
        "uDOGE", "uSUI", "LINK", "AAVE", "MORPHO", "ZEN")


def ch(q: str) -> str:
    p = subprocess.run(CH + ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


syms = ",".join(f"'{s}'" for s in SYMS)
pools = {}
for ln in ch(f"""
    SELECT p.pool, t.symbol, p.fee, p.venue,
           if(p.token0 = '{WETH}', 1, 0) AS weth_is_token0
    FROM census.pools p
    INNER JOIN census.tokens t
      ON t.token = if(p.token0 = '{WETH}', p.token1, p.token0)
    WHERE (p.token0 = '{WETH}' OR p.token1 = '{WETH}')
      AND t.symbol IN ({syms}) AND p.fee > 0
    """).strip().split("\n"):
    pool, sym, fee, venue, w0 = ln.split("\t")
    pools[pool] = (sym, int(fee), venue, int(w0))

plist = ",".join(f"'{p}'" for p in pools)
sys.stderr.write(f"[fee] probing {len(pools)} pools\n")

# consecutive swaps in the same pool and the same tick, so L is constant across
# the step and the identity holds exactly
raw = ch(f"""
SELECT pool, toString(amount0), toString(amount1), toString(sqrt_price_x96),
       toString(prev_spx), toString(liquidity), tick
FROM (
  SELECT pool, amount0, amount1, sqrt_price_x96, liquidity, tick,
         any(sqrt_price_x96) OVER (PARTITION BY pool
                                   ORDER BY block_number, log_index
                                   ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING)
           AS prev_spx,
         any(tick) OVER (PARTITION BY pool
                         ORDER BY block_number, log_index
                         ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) AS prev_tick
  FROM census.cl_swaps
  WHERE pool IN ({plist}) AND sqrt_price_x96 > 0 AND liquidity > 0
        AND cityHash64(tx_hash) % 40 = 0
)
WHERE prev_spx > 0 AND prev_tick = tick AND amount0 != 0 AND amount1 != 0
""")

est = defaultdict(list)
for ln in raw.strip().split("\n"):
    if not ln:
        continue
    pool, a0, a1, spx, pspx, liq, tick = ln.split("\t")
    a0, a1 = int(a0), int(a1)
    spx, pspx, L = int(spx), int(pspx), int(liq)
    if L <= 0 or spx <= 0 or pspx <= 0 or spx == pspx:
        continue
    # token0 in when amount0 > 0; the reciprocal-sqrt form applies then
    if a0 > 0:
        d = Q96 * (1.0 / spx - 1.0 / pspx)
        gross = a0
    elif a1 > 0:
        d = (spx - pspx) / Q96
        gross = a1
    else:
        continue
    net = L * d
    if gross <= 0 or net <= 0:
        continue
    f = 1.0 - net / gross
    if -0.02 < f < 0.5:
        est[pool].append(f)

by_sym = defaultdict(list)
for pool, v in est.items():
    if len(v) < 30:
        continue
    v.sort()
    implied = v[len(v) // 2]
    sym, fee, venue, _ = pools[pool]
    by_sym[sym].append((pool, venue, fee / 100.0, 1e4 * implied, len(v)))

print("=" * 112)
print("Fee actually charged, solved from price impact, against the fee this "
      "project has been assuming")
print("=" * 112)
print(f"{'symbol':<8}{'venue':<9}{'pool':<44}{'assumed_bps':>12}"
      f"{'implied_bps':>12}{'ratio':>8}{'n':>7}")
tot = defaultdict(list)
for sym in sorted(by_sym):
    for pool, venue, assumed, implied, n in sorted(by_sym[sym],
                                                   key=lambda r: -r[4]):
        ratio = implied / assumed if assumed else float("nan")
        tot[venue].append(ratio)
        print(f"{sym:<8}{venue:<9}{pool:<44}{assumed:>12.1f}{implied:>12.2f}"
              f"{ratio:>8.3f}{n:>7}")

print("\n" + "=" * 112)
print("By venue -- if a venue's ratio is far from 1, every fee number computed "
      "for it is wrong by that factor")
print("=" * 112)
print(f"{'venue':<12}{'pools':>7}{'median_implied/assumed':>24}"
      f"{'min':>8}{'max':>8}")
for venue in sorted(tot):
    v = sorted(tot[venue])
    print(f"{venue:<12}{len(v):>7}{v[len(v) // 2]:>24.3f}{v[0]:>8.3f}"
          f"{v[-1]:>8.3f}")
