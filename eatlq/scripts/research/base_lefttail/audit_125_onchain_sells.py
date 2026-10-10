#!/usr/bin/env python3
"""Ground-truth honeypot audit: did OTHER real wallets actually sell each token
for WETH during our holding window? Real on-chain sells = demonstrably not a
honeypot for those sellers in that window. This is stronger than any simulation.

Sell direction (trader sends alt token IN, gets WETH OUT):
  orient=1  (alt=token0): amount0 > 0
  orient=-1 (alt=token1): amount1 > 0
"""
from __future__ import annotations
import json, os, subprocess, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K

FRIC = -0.05
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
OUT = "/opt/EATLQ/scripts/research/base_lefttail/_audit_125_onchain.json"


def build_set():
    df, _ = K.load(); X, y = K.build(df)
    cut = df.t0.quantile(.6); tr = (df.t0 <= cut).values; te = ~tr
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0).fit(X[tr], y[tr])
    df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]
    pl = json.load(open(K.PCACHE)); real = json.load(open(os.path.join(os.path.dirname(K.PCACHE), "_realgas_replay.json")))
    df["fric"] = df.pool.map(lambda p: pl.get(p, {}).get("fric"))
    df["why"] = df.pool.map(lambda p: pl.get(p, {}).get("why"))
    df["net"] = df.pool.map(lambda p: real.get(p, {}).get("real_net", pl.get(p, {}).get("net")))
    pc = df[tr & (df.why == "ok") & df.fric.notna() & (df.fric >= FRIC)].p.quantile(.6)
    old = df[te & (df.why == "ok") & df.fric.notna() & (df.fric >= FRIC) & (df.p <= pc)].copy()
    return old, real


def query_sells(pool, orient, b0, b1):
    cond = "amount0 > 0" if orient == 1 else "amount1 > 0"
    # within holding window; also count buys for context
    sql = f"""
    SELECT
      countIf({cond}) AS sells,
      uniqExactIf(recipient, {cond}) AS sell_recipients,
      uniqExactIf(sender, {cond}) AS sell_senders,
      uniqExactIf(tx_hash, {cond}) AS sell_txs,
      count() AS total_swaps
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id=8453 AND pool_address='{pool}'
      AND block_number > {int(b0)} AND block_number <= {int(b1)}
    FORMAT TabSeparated
    """
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    vals = r.stdout.strip().split("\t")
    return dict(zip(["sells", "sell_recipients", "sell_senders", "sell_txs", "total_swaps"], map(int, vals)))


def main():
    old, real = build_set()
    rows = []
    for i, (_, rrow) in enumerate(old.iterrows(), 1):
        pool = rrow.pool
        rr = real.get(pool, {})
        b0 = rr.get("entry_block"); b1 = rr.get("exit_block")
        orient = int(rrow.orient)
        rec = {"pool": pool, "orient": orient, "net": rrow.net,
               "entry_sell_sim": rr.get("entry_sell"), "exit_sell_sim": rr.get("exit_sell"),
               "entry_block": b0, "exit_block": b1}
        if b0 and b1 and b1 > b0:
            q = query_sells(pool, orient, b0, b1)
            rec.update(q or {})
        rows.append(rec)
        if i % 25 == 0:
            print(f"  audited {i}/{len(old)}", flush=True)
    R = pd.DataFrame(rows)
    json.dump(rows, open(OUT, "w"), indent=2)

    R["fatal"] = R.net.astype(float) <= -0.5
    R["proven_sellable"] = R.get("sells", pd.Series([0]*len(R))).fillna(0) >= 3  # >=3 real sells by market
    R["multi_seller"] = R.get("sell_recipients", pd.Series([0]*len(R))).fillna(0) >= 2

    print("\n================ ON-CHAIN GROUND TRUTH AUDIT (131 set) ================")
    print(f"total={len(R)}  fatal={int(R.fatal.sum())}  nonfatal={int((~R.fatal).sum())}")
    nf = R[~R.fatal]
    print(f"\nNON-FATAL {len(nf)} laps:")
    print(f"  proven sellable (>=3 real market sells in window): {int(nf.proven_sellable.sum())}")
    print(f"  multi-seller (>=2 distinct recipients sold):        {int(nf.multi_seller.sum())}")
    print(f"  zero real sells in window (UNPROVEN):               {int((nf.get('sells',0).fillna(0)==0).sum())}")
    print(f"  sim said exit_sell=ok:                              {int((nf.exit_sell_sim=='ok').sum())}")
    fa = R[R.fatal]
    print(f"\nFATAL {len(fa)} laps:")
    for _, x in fa.iterrows():
        print(f"  {x.pool[:12]} net={float(x.net):+.2f} sim entry/exit={x.entry_sell_sim}/{x.exit_sell_sim} "
              f"real_sells={x.get('sells')} distinct_sellers={x.get('sell_recipients')}")
    print("\nNON-FATAL but ZERO real sells (need scrutiny):")
    z = nf[nf.get('sells',0).fillna(0)==0]
    for _, x in z.iterrows():
        print(f"  {x.pool[:12]} net={float(x.net):+.2f} sim exit={x.exit_sell_sim} total_swaps={x.get('total_swaps')}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
