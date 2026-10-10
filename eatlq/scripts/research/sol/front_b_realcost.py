#!/usr/bin/env python3
"""FRONT B — REALIZED-COST GUILLOTINE for the 'down-wick shakeout' signal.

Front B's tick price-path proxy showed 'down-wick alone' lift 1.38 (p5 1.11, n42).
EVERY prior ember with that profile died once REAL execution cost was applied.
This applies it: QuoterV2 round-trip (WETH->alt at entry block, alt->WETH at exit
block) at archive state, $NOTIONAL, real LP fee + both-side slippage; sell-fail/
honeypot => -100%. Same gates as sprint097 (PF_top1_removed, bootstrap_p5, canary).

Fee is read on-chain per pool via pool.fee() (selector 0xddca3f43) because
discovered_pools.fee_bps mixes meta- and protocol- encodings (ambiguous).

CASE-CONTROL on REALIZED return: down-wick pools vs a random non-signal sample.
Restricted to entry_block >= ARCH_MIN (confirmed archive depth).
"""
from __future__ import annotations
import math
import os
import random
import statistics
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/EATLQ/scripts/lib")
from v3_quoterv2_struct import JsonRpcClient, WETH_BASE, quote_exact_input_single_struct  # noqa: E402

WETH = WETH_BASE.lower()
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
LN1P = math.log(1.0001)
NBUCK, SIG_BUCKETS, ENTRY_B, EXIT_B = 30, 6, 6, 29
WICK_DROP, WICK_REC = 0.08, 0.04
DATE_LO, DATE_HI, MIN_SWAPS = "2026-03-01", "2026-06-06", 40
ARCH_MIN = 45_000_000          # confirmed archive depth
NOTIONAL_USD = float(os.environ.get("NOTIONAL_USD", "100"))
ETH_USD = 3000.0
FEE_SELECTOR = "0xddca3f43"
RPC_URL = os.environ.get("RPC_URL") or __import__("re").search(
    r'RPC_URL = os.environ.get\("RPC_URL",\s*\n?\s*"([^"]+)"',
    open("/opt/EATLQ/scripts/research/expanded_realcost_backtest.py").read()).group(1)
rpc = JsonRpcClient(RPC_URL, delay_s=0.04)

SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0addr, lower(token1_address) t1addr
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0, count() ns
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL
  GROUP BY pool_address
  HAVING ns >= {MIN_SWAPS} AND t0 >= '{DATE_LO} 00:00:00' AND t0 <= '{DATE_HI} 23:59:59'
)
SELECT s.pool_address AS pool,
       if(c.t0addr='{WETH}', -1, 1) AS orient,
       if(c.t0addr='{WETH}', c.t1addr, c.t0addr) AS alt,
       toUInt32(floor(dateDiff('second', fs.t0, s.block_time)/300)) AS b,
       argMin(s.tick, s.block_time) AS first_tick,
       argMax(s.tick, s.block_time) AS last_tick,
       min(s.tick) AS min_tick, max(s.tick) AS max_tick,
       uniqExact(s.recipient) AS buyers,
       argMin(s.block_number, s.block_time) AS first_block,
       argMax(s.block_number, s.block_time) AS last_block
FROM eatlq.uniswap_v3_swaps_decoded s
INNER JOIN fs ON s.pool_address=fs.pool_address
INNER JOIN cand c ON s.pool_address=c.pool_address
WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
  AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {NBUCK*300}
GROUP BY pool, orient, alt, b, fs.t0
ORDER BY pool, b
FORMAT TabSeparated
"""


def logp(tick, orient):
    return tick * LN1P * orient


def pool_fee(pool):
    try:
        res = rpc.call("eth_call", [{"to": pool, "data": FEE_SELECTOR}, "latest"], timeout=15)
        return int(res, 16) if res and res != "0x" else None
    except Exception:
        return None


def roundtrip(pool, alt, fee, entry_block, exit_block):
    amt_in = int(NOTIONAL_USD / ETH_USD * 1e18)
    qi = quote_exact_input_single_struct(rpc, WETH_BASE, alt, amt_in, fee, block_identifier=entry_block)
    if not qi["ok"]:
        return None, "entry_fail:" + qi["reason"]
    qo = quote_exact_input_single_struct(rpc, alt, WETH_BASE, qi["amount_out"], fee, block_identifier=exit_block)
    if not qo["ok"]:
        return -1.0, "sell_fail:" + qo["reason"]            # honeypot => -100%
    return qo["amount_out"] / amt_in - 1.0, "ok"


def pf(rets):
    g = sum(x for x in rets if x > 0); l = -sum(x for x in rets if x < 0)
    return g / l if l > 0 else (float("inf") if g > 0 else 0.0)


def pf_top1_removed(rets):
    if len(rets) < 3:
        return 0.0
    srt = sorted(rets, reverse=True)
    g = sum(x for x in srt[1:] if x > 0); l = -sum(x for x in srt if x < 0)
    return g / l if l > 0 else 0.0


def boot_p5(rets, n=2000):
    if len(rets) < 5:
        return -1.0
    evs = sorted(statistics.mean([random.choice(rets) for _ in rets]) for _ in range(n))
    return evs[int(0.05 * n)]


def summarize(label, rets, whys=None):
    if not rets:
        print(f"  {label}: n=0"); return
    wr = sum(1 for x in rets if x > 0) / len(rets)
    sf = (sum(1 for w in whys if w.startswith("sell_fail")) / len(whys)) if whys else 0
    print(f"  {label}: n={len(rets)} EV={100*statistics.mean(rets):+.1f}% med={100*statistics.median(rets):+.1f}% "
          f"WR={100*wr:.0f}% PF={pf(rets):.2f} PF_top1rm={pf_top1_removed(rets):.2f} "
          f"boot_p5={100*boot_p5(rets):+.1f}% sell_fail={100*sf:.0f}% "
          f"best={100*max(rets):+.0f}% worst={100*min(rets):+.0f}%")


def main():
    print(f"#### FRONT B GUILLOTINE: down-wick signal @ REALIZED QuoterV2 round-trip (${NOTIONAL_USD:.0f}) ####")
    raw = subprocess.run(CH + [SQL], capture_output=True, text=True, timeout=600)
    if raw.returncode != 0:
        print("CH error:", raw.stderr[:300]); return
    pools = defaultdict(dict); meta = {}
    for line in raw.stdout.strip().split("\n"):
        if not line:
            continue
        pool, ori, alt, b, ft, lt, mn, mx, buyers, fb, lb = line.split("\t")
        meta[pool] = {"orient": int(ori), "alt": alt}
        pools[pool][int(b)] = {"first": int(ft), "last": int(lt), "min": int(mn), "max": int(mx),
                               "buyers": int(buyers), "fb": int(fb), "lb": int(lb)}
    # detect signal + collect entry/exit blocks
    sig_pools, ctrl_pools = [], []
    for pool, bk in pools.items():
        if ENTRY_B not in bk or max(bk) < EXIT_B - 5:
            continue
        ori = meta[pool]["orient"]
        wick = False
        for b in range(1, SIG_BUCKETS):
            if b not in bk or (b - 1) not in bk:
                continue
            prev_close = logp(bk[b - 1]["last"], ori)
            low_tick = bk[b]["min"] if ori == 1 else bk[b]["max"]
            bar_low = logp(low_tick, ori)
            bar_close = logp(bk[b]["last"], ori)
            if (bar_low - prev_close) <= math.log(1 - WICK_DROP) and (bar_close - bar_low) >= math.log(1 + WICK_REC):
                wick = True
                break
        entry_block = bk[ENTRY_B]["fb"]
        ex_b = max(b for b in bk if b <= EXIT_B)
        exit_block = bk[ex_b]["lb"]
        if entry_block < ARCH_MIN:
            continue
        rec = {"pool": pool, "alt": meta[pool]["alt"], "entry_block": entry_block, "exit_block": exit_block}
        (sig_pools if wick else ctrl_pools).append(rec)
    random.seed(1); random.shuffle(ctrl_pools)
    ctrl_pools = ctrl_pools[:max(40, len(sig_pools))]
    print(f"down-wick pools (entry_block>={ARCH_MIN}): {len(sig_pools)} | control sample: {len(ctrl_pools)}\n")

    def price_group(group, name):
        rets, whys = [], []
        for i, r in enumerate(group):
            fee = pool_fee(r["pool"])
            if fee is None or fee not in (100, 500, 3000, 10000):
                continue
            ret, why = roundtrip(r["pool"], r["alt"], fee, r["entry_block"], r["exit_block"])
            if ret is None:
                continue
            rets.append(ret); whys.append(why)
            if (i + 1) % 15 == 0:
                print(f"    [{name}] {i+1}/{len(group)} priced={len(rets)} rpc={rpc.calls}", flush=True)
        return rets, whys

    print("[pricing down-wick group]")
    sret, swhy = price_group(sig_pools, "wick")
    print("[pricing control group]")
    cret, cwhy = price_group(ctrl_pools, "ctrl")

    print("\n=== REALIZED-COST VERDICT (QuoterV2, real fee + both-side slippage, honeypot=-100%) ===")
    summarize("DOWN-WICK", sret, swhy)
    summarize("CONTROL  ", cret, cwhy)
    if sret and cret:
        lift = (statistics.mean(sret) - statistics.mean(cret))
        survive = statistics.mean(sret) > 0 and boot_p5(sret) > 0 and pf_top1_removed(sret) >= 1.3
        print(f"\n  down-wick realized EV {100*statistics.mean(sret):+.1f}% vs control {100*statistics.mean(cret):+.1f}% "
              f"(gap {100*lift:+.1f}pp)")
        print(f"  >>> {'SURVIVES the guillotine (first survivor — needs forward canary)' if survive else 'DIES at realized cost (joins every prior ember)'}")
        print(f"      [survive test: EV>0 & bootstrap_p5>0 & PF_top1_removed>=1.3]")
    print(f"\nrpc_calls={rpc.calls}")


if __name__ == "__main__":
    main()
