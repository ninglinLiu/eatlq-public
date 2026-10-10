#!/usr/bin/env python3
"""Size-specific QuoterV2 + real-gas replay for Keystone holdout candidates."""
from __future__ import annotations

import json
import os
import sys

import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import keystone_live as K  # noqa: E402
import replay_realgas_sellsim as RG  # noqa: E402

SIZES = [100, 300, 1000]
FRIC_THR = -0.05
CACHE = os.path.join(HERE, "_dynamic_size_realgas.json")


def candidate_frame() -> tuple[pd.DataFrame, dict]:
    df, path = K.load()
    X, y = K.build(df)
    cut = df.t0.quantile(0.6)
    tr = (df.t0 <= cut).values
    te = ~tr
    clf = HistGradientBoostingClassifier(
        max_iter=300,
        max_depth=4,
        learning_rate=0.05,
        l2_regularization=1.0,
        min_samples_leaf=20,
        random_state=0,
    )
    clf.fit(X[tr], y[tr])
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    base = json.load(open(K.PCACHE))
    df["fric100"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    df["why"] = df.pool.map(lambda x: base.get(x, {}).get("why"))
    gtr = df[tr & (df.why == "ok") & df.fric100.notna() & (df.fric100 >= FRIC_THR)]
    p_cut = gtr.p.quantile(0.6)
    cand = df[te & (df.why == "ok") & (df.p <= p_cut)].copy()
    return cand, path


def main() -> None:
    cand, path = candidate_frame()
    head = int(K.rpc.call("eth_blockNumber", [], timeout=15), 16)
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    total = len(cand) * len(SIZES)
    done = 0
    print(f"dynamic size real-gas candidates={len(cand)} sizes={SIZES} cached={len(cache)}")
    for _, row in cand.iterrows():
        pool = row.pool
        fee = K.pool_fee(pool)
        entry_lp = row.last_tick_pre * K.LN1P * row.orient
        exb, exwhy = K.exit_block(path.get(pool, []), entry_lp, row.orient, int(row.entry_block), head)
        for size in SIZES:
            key = f"{pool}:{size}"
            if key in cache:
                done += 1
                continue
            amt = int(size / K.ETH_USD * 1e18)
            rec = {
                "size_usd": size,
                "fee": fee,
                "entry_block": int(row.entry_block),
                "exit_block": int(exb),
                "exwhy": exwhy,
                "fric": None,
                "net": None,
                "real_net": None,
                "entry_sell": None,
                "exit_sell": None,
                "token_out": None,
                "why": "bad_fee",
            }
            if fee in (100, 500, 3000, 10000):
                token_out = K.quote(K.WETH_BASE, row.alt, amt, fee, int(row.entry_block))
                rec["why"] = "entry_fail"
                if token_out and token_out > 0:
                    rec["token_out"] = int(token_out)
                    back = K.quote(row.alt, K.WETH_BASE, int(token_out), fee, int(row.entry_block))
                    rec["fric"] = -1.0 if back is None else back / amt - 1.0
                    rec["why"] = "ok"
                    if rec["fric"] >= FRIC_THR:
                        sell = K.quote(row.alt, K.WETH_BASE, int(token_out), fee, int(exb))
                        rec["net"] = -1.0 if sell is None else sell / amt - 1.0
                        ev, _ = RG.realgas_sell_ok(row.alt, fee, int(token_out), int(row.entry_block))
                        xv, xout = RG.realgas_sell_ok(row.alt, fee, int(token_out), int(exb))
                        rec["entry_sell"] = ev
                        rec["exit_sell"] = xv
                        rec["real_net"] = (xout / amt - 1.0) if xv == "ok" else (-1.0 if xv == "revert" else rec["net"])
            cache[key] = rec
            done += 1
            if done % 20 == 0:
                json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
                print(f"  {done}/{total} saved")
    json.dump(cache, open(CACHE, "w"), indent=2, sort_keys=True)
    print(f"wrote {len(cache)} records -> {CACHE}")


if __name__ == "__main__":
    main()
