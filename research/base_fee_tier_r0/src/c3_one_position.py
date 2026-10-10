#!/usr/bin/env python3
"""Recompute one cbBTC position from scratch and see whether 1157 bps survives.

Everything about C3's cbBTC result rests on a single pool,
0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1, and a single number: 1157 bps of
weekly fees on a $50 position whose liquidity share is order 1e-5. Three artifact
attacks have already failed -- the share is not inflated at the moments the fees
arrive, the pool has no duplicate rows, and the liquidity field reproduces the
observed price impact of individual swaps to six digits, so decimals and units are
right.

What has not been checked is the arithmetic chain itself: notional to liquidity,
liquidity to share, share to fees. A rough hand calculation lands about an order of
magnitude below what the pipeline reports, and an order of magnitude is not
something to leave as a rounding difference. This prints every intermediate for one
position so the disagreement has to resolve one way or the other.
"""
from __future__ import annotations

import math
import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"
POOL = "0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1"
FEE = 2500
CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800", "--max_memory_usage=12000000000"]


def ch(q: str, fmt: str = "TSV") -> str:
    p = subprocess.run(CH + (["--format", fmt] if fmt != "TSV" else []) +
                       ["--query", q], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout


row = ch(f"""SELECT pos_id, toString(entry), tick_lower, tick_upper, toString(L)
             FROM census.c3x_pos
             WHERE pool = '{POOL}' AND T = 7 ORDER BY entry LIMIT 1""")
pos_id, entry, tl, tu, L = row.strip().split("\t")
pos_id, tl, tu, L = int(pos_id), int(tl), int(tu), float(L)
print(f"position {pos_id}, pool {POOL}, entry {entry}, T=7")
print(f"  ticks [{tl}, {tu}]  width {(1.0001 ** (tu - tl) - 1) * 100:.3f}%"
      f"  ({(tu - tl)} ticks, spacing 100)")
print(f"  L = {L:.6e}")

pa, pb = 1.0001 ** tl, 1.0001 ** tu
p_raw = float(ch(f"""
  SELECT pow(argMax(toFloat64(sqrt_price_x96), (block_number, log_index))
             / pow(2, 96), 2)
  FROM census.cl_swaps
  WHERE pool = '{POOL}' AND block_time < toDateTime('{entry} 00:00:00')
        AND sqrt_price_x96 > 0""").strip())
print(f"  entry raw price p = {p_raw:.6e}   range [{pa:.6e}, {pb:.6e}]")
print(f"  in range at entry: {pa <= p_raw <= pb}")


def amounts(L_, p, pa_, pb_):
    sp, spa, spb = math.sqrt(p), math.sqrt(pa_), math.sqrt(pb_)
    if sp <= spa:
        return L_ * (1.0 / spa - 1.0 / spb), 0.0
    if sp >= spb:
        return 0.0, L_ * (spb - spa)
    return L_ * (1.0 / sp - 1.0 / spb), L_ * (sp - spa)


a0, a1 = amounts(L, p_raw, pa, pb)
# token0 is WETH here, so token1 must be converted at the raw price
v_weth_raw = a0 + a1 / p_raw
eth_usd = float(ch(f"""SELECT quantile(0.5)(eth_usd) FROM edge.eth_usd_1h
                       WHERE toDate(bar) = toDate('{entry}')""").strip())
print(f"\n  token0 (WETH) at entry: {a0 / 1e18:.8f} WETH")
print(f"  token1 (cbBTC) at entry: {a1 / 1e8:.8f} cbBTC")
print(f"  total value: {v_weth_raw / 1e18:.8f} WETH")
print(f"  ETH/USD on {entry}: {eth_usd:,.2f}")
print(f"  ==> position notional = ${v_weth_raw / 1e18 * eth_usd:,.4f}"
      f"   (target $50)")

print("\nWhat the pool did during the 7 days, and what the position collected:")
out = ch(f"""
SELECT count() AS swaps,
       countIf(tick >= {tl} AND tick < {tu}) AS swaps_in_range,
       sum(abs(toFloat64(amount0))) AS vol_all,
       sumIf(abs(toFloat64(amount0)), tick >= {tl} AND tick < {tu}) AS vol_in,
       sumIf(abs(toFloat64(amount0)) * ({FEE} / 1e6) * {L!r}
             / (toFloat64(liquidity) + {L!r}),
             tick >= {tl} AND tick < {tu}) AS my_fees,
       sumIf(abs(toFloat64(amount0)) * ({FEE} / 1e6),
             tick >= {tl} AND tick < {tu}) AS pool_fees_in,
       quantileIf(0.5)(toFloat64(liquidity), tick >= {tl} AND tick < {tu}) AS Lact_p50,
       quantileIf(0.5)({L!r} / (toFloat64(liquidity) + {L!r}),
                       tick >= {tl} AND tick < {tu}) AS share_p50
FROM census.cl_swaps
WHERE pool = '{POOL}' AND sqrt_price_x96 > 0
      AND block_time >= toDateTime('{entry} 00:00:00')
      AND block_time < toDateTime('{entry} 00:00:00') + INTERVAL 7 DAY
""")
(sw, swin, vol_all, vol_in, myf, poolf, lact, share) = \
    [float(x) for x in out.strip().split("\t")]
v_ref_weth = v_weth_raw / 1e18
print(f"  swaps {sw:,.0f}, of which in my range {swin:,.0f} "
      f"({100 * swin / sw:.1f}%)")
print(f"  pool WETH volume {vol_all / 1e18:,.1f} WETH, in my range "
      f"{vol_in / 1e18:,.1f} WETH")
print(f"  pool fees earned in my range: {poolf / 1e18:,.4f} WETH "
      f"(${poolf / 1e18 * eth_usd:,.0f})")
print(f"  median active liquidity beside me: {lact:.4e}   my L: {L:.4e}")
print(f"  ==> median share {share:.3e}  (1 in {1 / share:,.0f})")
print(f"  my attributed fees: {myf / 1e18:.8f} WETH "
      f"= ${myf / 1e18 * eth_usd:.4f}")
print(f"  ==> {1e4 * myf / v_weth_raw:,.1f} bps of the position over 7 days")

print("\nCross-check by two routes that must agree:")
r1 = poolf * share
print(f"  pool fees in range x median share = {r1 / 1e18:.8f} WETH")
print(f"  per-swap attribution              = {myf / 1e18:.8f} WETH")
print(f"  ratio = {myf / r1 if r1 else float('nan'):.3f}  "
      f"(far from 1 means the share moves with swap size)")

print("\nIs the pool's own return plausible? A pool cannot pay its LPs 200%/yr "
      "for a year.")
lp_value_weth = (lact * (1.0 / math.sqrt(p_raw) - 1.0 / math.sqrt(pb))
                 + lact * (math.sqrt(p_raw) - math.sqrt(pa)) / p_raw) / 1e18
print(f"  liquidity {lact:.4e} spread over my range is worth "
      f"{lp_value_weth:,.1f} WETH (${lp_value_weth * eth_usd:,.0f})")
print(f"  it collected ${poolf / 1e18 * eth_usd:,.0f} of fees in 7 days")
if lp_value_weth > 0:
    wk = poolf / 1e18 / lp_value_weth
    print(f"  ==> {1e4 * wk:,.1f} bps per week = {100 * wk * 52:,.1f}% per year "
          f"on the liquidity actually sitting in that range")
    print("      (this is the pool's own number and involves no position of mine)")
