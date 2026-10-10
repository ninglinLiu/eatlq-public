#!/usr/bin/env python3
"""JUNE 2026 out-of-sample backtest for KEYSTONE.

- Model trained on the existing Oct-2025 .. Jun-1 dataset (keystone_lefttail).
- p_cut frozen from TRAIN gated pools (friction>=-0.05), q60 (same protocol).
- June pools (Jun-1 .. Jun-21, covered days only; 9-17 ingestion gap excluded)
  are a PURE forward holdout: never seen by the model.
- Real-cost: QuoterV2 buy at entry block, sell at entry+~4h (keystone_realcost
  fixed-hold methodology), unsellable => -100%. PLUS a real-gas executable-sell
  check (state-override, 3M gas) at the exit block to flag gas-bomb honeypots.
"""
from __future__ import annotations
import subprocess, math, os, sys, json, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sklearn.ensemble import HistGradientBoostingClassifier
import keystone_lefttail as KL
import replay_realgas_sellsim as RG
from web3 import Web3
from eth_abi import encode

WETH = RG.WETH
QUOTER = RG.QUOTER
NOTIONAL_USD, ETH_USD = 100.0, 3000.0
AMT_IN = int(NOTIONAL_USD / ETH_USD * 1e18)
HOLD_BLOCKS = 7200            # ~4h on Base 2s blocks
FRIC_THR = -0.05
JUNE_LO, JUNE_HI = "2026-06-01", "2026-06-21"
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
w3 = RG.w3
_QSIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]

JUNE_SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a, lower(token1_address) t1a
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL GROUP BY pool_address
  HAVING t0 >= '{JUNE_LO} 00:00:00' AND t0 <= '{JUNE_HI} 23:59:59'
),
sw AS (
  SELECT s.pool_address pool, s.tick tick, s.block_time bt, s.block_number bn,
         s.recipient rcp, s.sender snd, toFloat64(s.liquidity) liq,
         dateDiff('second', fs.t0, s.block_time) sec, fs.t0 t0
  FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON s.pool_address=fs.pool_address
  WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
    AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND 15000
)
SELECT pool, if(c.t0a='{WETH.lower()}', -1, 1) orient,
  if(c.t0a='{WETH.lower()}', c.t1a, c.t0a) alt,
  any(t0) t0,
  countIf(sec<=600) ns_pre, uniqExactIf(rcp, sec<=600) buyers_pre,
  uniqExactIf(snd, sec<=600) senders_pre,
  argMinIf(tick, bt, sec<=600) first_tick, argMaxIf(tick, bt, sec<=600) last_tick_pre,
  minIf(tick, sec<=600) min_tick_pre, maxIf(tick, sec<=600) max_tick_pre,
  argMinIf(liq, bt, sec<=600) liq_first, argMaxIf(liq, bt, sec<=600) liq_last_pre,
  minIf(liq, sec<=600) liq_min_pre,
  countIf(sec<=300) ns_h1, countIf(sec>300 AND sec<=600) ns_h2,
  argMaxIf(tick, bt, sec<=300) last_tick_h1,
  countIf(sec>600 AND sec<=15000) ns_fwd, minIf(tick, sec>600 AND sec<=15000) min_tick_fwd,
  maxIf(tick, sec>600 AND sec<=15000) max_tick_fwd, argMaxIf(tick, bt, sec>600 AND sec<=15000) last_tick_fwd,
  argMaxIf(liq, bt, sec>600 AND sec<=15000) liq_last_fwd,
  minIf(bn, sec>600) entry_block
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient, alt
HAVING ns_pre>=5 AND entry_block>0
FORMAT TabSeparated
"""
JCOLS = KL.COLS + ["alt", "entry_block"]
# reorder to match select
JCOLS = ["pool","orient","alt","t0","ns_pre","buyers_pre","senders_pre","first_tick",
         "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre","liq_min_pre",
         "ns_h1","ns_h2","last_tick_h1","ns_fwd","min_tick_fwd","max_tick_fwd","last_tick_fwd",
         "liq_last_fwd","entry_block"]


def pool_fee(pool, block):
    try:
        r = w3.provider.make_request("eth_call", [{"to": Web3.to_checksum_address(pool), "data": "0xddca3f43"}, hex(int(block))])
        v = int(r["result"], 16) if r.get("result") and r["result"] != "0x" else None
        return v
    except Exception:
        return None


def quote(tin, tout, amt, fee, block):
    params = encode(["(address,address,uint256,uint24,uint160)"],
                    [(Web3.to_checksum_address(tin), Web3.to_checksum_address(tout), int(amt), int(fee), 0)])
    try:
        r = w3.provider.make_request("eth_call", [{"to": QUOTER, "data": "0x" + (_QSIG + params).hex()}, hex(int(block))])
    except Exception:
        return None
    res = r.get("result")
    if not res or res == "0x":
        return None
    return RG._parse_uint(res[:66])


def main():
    print("#### JUNE 2026 OOS BACKTEST ####", flush=True)
    # 1) train model on existing dataset
    print("training model on Oct2025..Jun1 ...", flush=True)
    dtr = KL.run_sql()
    Xtr, Ytr = KL.build(dtr)
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                         l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    clf.fit(Xtr, Ytr.fatal)
    # p_cut from TRAIN gated pools (use cached friction)
    cache = json.load(open(KL_PCACHE := os.path.join(os.path.dirname(__file__), "_priced_live.json")))
    dtr = dtr.copy(); dtr["p"] = clf.predict_proba(Xtr)[:, 1]
    dtr["fric"] = dtr.pool.map(lambda x: cache.get(x, {}).get("fric"))
    gated = dtr[dtr.fric.notna() & (dtr.fric >= FRIC_THR)]
    p_cut = gated.p.quantile(0.6)
    print(f"  p_cut(TRAIN gated q60) = {p_cut:.3f}  (train gated n={len(gated)})", flush=True)

    # 2) June pools
    print("extracting June pools ...", flush=True)
    r = subprocess.run(CH + [JUNE_SQL], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print("SQL ERR", r.stderr[:1500]); sys.exit(1)
    rows = [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]
    dj = pd.DataFrame(rows, columns=JCOLS)
    for c in JCOLS:
        if c not in ("pool", "alt", "t0"):
            dj[c] = pd.to_numeric(dj[c], errors="coerce")
    dj["t0"] = pd.to_datetime(dj["t0"])
    Xj, Yj = KL.build(dj)
    dj["p"] = clf.predict_proba(Xj)[:, 1]
    dj["fatal_label"] = Yj.fatal.values
    print(f"June onset pools: {len(dj)}  (model-fatal-label rate={Yj.fatal.mean():.3f})", flush=True)

    # 3) friction gate + price each candidate (RPC)
    head = int(w3.eth.block_number)
    recs = []
    for i, row in dj.iterrows():
        pool, alt, eb = row.pool, Web3.to_checksum_address(row.alt), int(row.entry_block)
        xb = min(eb + HOLD_BLOCKS, head)
        fee = pool_fee(pool, eb)
        rec = {"pool": pool, "p": float(row.p), "fatal_label": int(row.fatal_label),
               "fee": fee, "entry_block": eb, "exit_block": xb,
               "fric": None, "net": None, "real_net": None, "exit_sell": None}
        if fee in (100, 500, 3000, 10000):
            tok = quote(WETH, alt, AMT_IN, fee, eb)
            if tok and tok > 0:
                back = quote(alt, WETH, tok, fee, eb)
                rec["fric"] = (back / AMT_IN - 1.0) if back else -1.0
                sell = quote(alt, WETH, tok, fee, xb)
                rec["net"] = (sell / AMT_IN - 1.0) if sell else -1.0
                # real-gas executable sell at exit (state-override, 3M gas)
                xv, xout = RG.realgas_sell_ok(alt, fee, tok, xb)
                rec["exit_sell"] = xv
                rec["real_net"] = (xout / AMT_IN - 1.0) if xv == "ok" else (-1.0 if xv == "revert" else rec["net"])
            else:
                rec["fric"] = -1.0; rec["net"] = -1.0; rec["real_net"] = -1.0; rec["exit_sell"] = "entry_fail"
        recs.append(rec)
        if (i + 1) % 10 == 0:
            print(f"  priced {i+1}/{len(dj)}", flush=True)
    R = pd.DataFrame(recs)
    json.dump(recs, open(os.path.join(os.path.dirname(__file__), "_june_backtest.json"), "w"))

    # 4) apply gate + ML
    gate = R[R.fric.notna() & (R.fric >= FRIC_THR)]
    kept = gate[gate.p <= p_cut]
    days = 21  # June 1-21 nominal (covered days only; 9-17 gap noted)

    def rep(tag, sub, col):
        a = sub[col].dropna().values.astype(float)
        if len(a) == 0:
            print(f"  {tag:30s} n=0"); return
        nmo = len(sub) / days * 30
        print(f"  {tag:30s} n={len(a):3d} mean={a.mean():+.3f} med={np.median(a):+.3f} "
              f"WR={(a>0).mean():.2f} FATAL={ (a<=-0.5).mean():.3f} ~{nmo:.0f} tr/mo")

    print("\n" + "=" * 74)
    print("JUNE 2026 OOS RESULTS (covered days: Jun1-8,18-21; 9-17 ingestion gap)")
    print("=" * 74)
    print(f"onset pools={len(R)}  friction-gated={len(gate)}  gate+ML kept={len(kept)}")
    rep("gate-only  CACHED", gate, "net")
    rep("gate-only  REAL-GAS", gate, "real_net")
    rep("gate+ML    CACHED  (HOLDOUT)", kept, "net")
    rep("gate+ML    REAL-GAS(HOLDOUT)", kept, "real_net")
    if len(kept):
        r = kept.real_net.dropna().values
        # drawdown on equal-weight sequential book
        eq = np.cumprod(1 + 0.10 * r)  # 10% position each
        peak = np.maximum.accumulate(eq); dd = (eq / peak - 1).min()
        print(f"\n  equity (10% size, sequential): x{eq[-1]:.2f}  maxDD={dd:.1%}")
        for fpos in (0.05, 0.10):
            nmo = len(kept) / days * 30
            print(f"  -> ~{nmo:.0f} trades/mo x {int(fpos*100)}% x mean {r.mean():+.3f} "
                  f"=> ~{nmo*fpos*r.mean()*100:+.0f}%/mo (real-cost, gross MEV/latency)")


if __name__ == "__main__":
    main()
