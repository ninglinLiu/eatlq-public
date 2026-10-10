#!/usr/bin/env python3
"""KEYSTONE go-live candidate: depth-gate (live round-trip quote) + EXIT MANAGEMENT
(trailing take-profit on the tick path, real QuoterV2 sell at the chosen exit block),
priced over the WHOLE universe (archive is full), TRAIN -> holdout.

Causality / no look-ahead:
  - features: only pre-entry [0,600s]
  - friction gate: a quote a live bot can run BEFORE buying (buy + immediate sell
    at entry block) -> decides whether to enter at all.
  - exit timing: trailing stop walked causally over the forward tick path; we sell
    at the block of the bucket where the rule fires. No future info beyond the bar.
  - model trained on TRAIN pools only.
Base ~2s blocks. forward buckets = 10 min.
"""
from __future__ import annotations
import subprocess, math, sys, os, json
import numpy as np, pandas as pd
from collections import defaultdict
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
sys.path.insert(0, "/opt/EATLQ/scripts/lib")
from v3_quoterv2_struct import JsonRpcClient, WETH_BASE, quote_exact_input_single_struct  # noqa

WETH = WETH_BASE.lower()
LN1P = math.log(1.0001)
W, H = 600, 14400
STOP = math.log(0.5)
HARD_STOP = math.log(0.5)         # -50% hard floor from entry
TRAIL = 0.35                      # exit if 35% below running peak
HOLD_BLOCKS = 7200                # ~4h fallback
NOTIONAL_USD, ETH_USD = 100.0, 3000.0
FEE_SELECTOR = "0xddca3f43"
FRIC_PRICE_FLOOR = -0.15          # only bother pricing exits for pools this tradeable
POOLS = "/opt/EATLQ/scripts/research/base_lefttail/_pools.tsv"
FWD = "/opt/EATLQ/scripts/research/base_lefttail/_fwdpath.tsv"
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
rpc = JsonRpcClient(RPC_URL, delay_s=0.02)
_fee: dict[str, int] = {}
COLS = ["pool","orient","alt","t0","ns_pre","buyers_pre","senders_pre","first_tick",
        "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre",
        "liq_min_pre","ns_h1","ns_h2","last_tick_h1","entry_block","ns_fwd",
        "min_tick_fwd","max_tick_fwd","last_tick_fwd"]


def load():
    df = pd.read_csv(POOLS, sep="\t", names=COLS)
    df["t0"] = pd.to_datetime(df["t0"])
    for c in COLS:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    path = defaultdict(list)
    pf = pd.read_csv(FWD, sep="\t", names=["pool","bkt","ltick","lblock"])
    for pool, bkt, lt, lb in pf.itertuples(index=False):
        path[pool].append((int(bkt), int(lt), int(lb)))
    for p in path:
        path[p].sort()
    return df, path


def build(df):
    o = df.orient.values; lp = lambda t: t * LN1P * o
    lp_first, lp_last = lp(df.first_tick), lp(df.last_tick_pre)
    lp_lo = np.minimum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    lp_hi = np.maximum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    f = pd.DataFrame(index=df.index)
    f["ns_pre"], f["buyers_pre"], f["senders_pre"] = df.ns_pre, df.buyers_pre, df.senders_pre
    f["swaps_per_buyer"] = df.ns_pre / df.buyers_pre.clip(lower=1)
    f["buyer_sender_ratio"] = df.buyers_pre / df.senders_pre.clip(lower=1)
    f["early_ret"] = lp_last - lp_first
    f["pre_vol"] = lp_hi - lp_lo
    f["pre_pullback"] = lp_hi - lp_last
    f["pre_runup_from_lo"] = lp_last - lp_lo
    f["liq_log"] = np.log1p(df.liq_last_pre.clip(lower=0))
    f["liq_trend"] = df.liq_last_pre / df.liq_first.clip(lower=1)
    f["liq_drawdown"] = df.liq_min_pre / df.liq_first.clip(lower=1)
    f["heat_accel"] = df.ns_h2 / df.ns_h1.clip(lower=1)
    lp_h1 = lp(df.last_tick_h1)
    f["mom_h1"], f["mom_h2"] = lp_h1 - lp_first, lp_last - lp_h1
    has = df.ns_fwd > 0
    fwd_lo = np.where(has, np.minimum(lp(df.min_tick_fwd), lp(df.max_tick_fwd)), -np.inf)
    fatal = (df.ns_fwd <= 2).values | ((fwd_lo - lp_last) <= STOP)
    return f.replace([np.inf, -np.inf], np.nan).fillna(0.0), fatal.astype(int)


def exit_block(path_steps, entry_lp, orient, entry_block, head):
    """Causal trailing stop on tick path. Returns (block, reason)."""
    if not path_steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    peak = entry_lp
    for bkt, ltick, lblock in path_steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        if (lp - peak) <= math.log(1 - TRAIL):
            return lblock, "trail"
    return path_steps[-1][2], "hold_end"


def pool_fee(pool):
    if pool in _fee:
        return _fee[pool]
    try:
        res = rpc.call("eth_call", [{"to": pool, "data": FEE_SELECTOR}, "latest"], timeout=15)
        v = int(res, 16) if res and res != "0x" else None
    except Exception:
        v = None
    _fee[pool] = v
    return v


def quote(tin, tout, amt, fee, blk):
    q = quote_exact_input_single_struct(rpc, tin, tout, amt, fee, block_identifier=blk)
    return q["amount_out"] if q["ok"] else None


def price_all(df, path, head):
    cache = json.load(open(PCACHE)) if os.path.exists(PCACHE) else {}
    amt = int(NOTIONAL_USD / ETH_USD * 1e18)
    todo = [r for _, r in df.iterrows() if r.pool not in cache]
    print(f"  pricing {len(todo)} new pools (cached {len(cache)}) ...")
    for i, r in enumerate(todo):
        pool = r.pool
        fee = pool_fee(pool)
        rec = {"fee": fee, "fric": None, "net": None, "why": "bad_fee", "exb": None, "exwhy": None}
        if fee in (100, 500, 3000, 10000):
            out = quote(WETH_BASE, r.alt, amt, fee, int(r.entry_block))
            if out is None or out <= 0:
                rec["why"] = "entry_fail"
            else:
                back = quote(r.alt, WETH_BASE, out, fee, int(r.entry_block))
                rec["fric"] = -1.0 if back is None else back / amt - 1.0
                rec["why"] = "ok"
                if rec["fric"] is not None and rec["fric"] >= FRIC_PRICE_FLOOR:
                    entry_lp = r.last_tick_pre * LN1P * r.orient
                    exb, exwhy = exit_block(path.get(pool, []), entry_lp, r.orient, int(r.entry_block), head)
                    sell = quote(r.alt, WETH_BASE, out, fee, exb)
                    rec["net"] = -1.0 if sell is None else sell / amt - 1.0
                    rec["exb"], rec["exwhy"] = exb, exwhy
        cache[pool] = rec
        if (i + 1) % 50 == 0:
            print(f"    {i+1}/{len(todo)} rpc={rpc.calls}", flush=True)
            json.dump(cache, open(PCACHE, "w"))
    json.dump(cache, open(PCACHE, "w"))
    return cache


def stat(tag, r):
    r = np.array([x for x in r if x is not None])
    if len(r) == 0:
        print(f"  {tag:26s} n=0"); return
    print(f"  {tag:26s} n={len(r):4d} mean={r.mean():+.3f} med={np.median(r):+.3f} "
          f"WR={(r>0).mean():.2f} fatal={(r<=-0.5).mean():.3f} best={r.max():+.2f} "
          f"p90={np.quantile(r,0.9):+.2f}")


def main():
    df, path = load()
    X, y = build(df)
    cut = df.t0.quantile(0.6); tr = (df.t0 <= cut).values; te = ~tr
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                         l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    clf.fit(X[tr], y[tr])
    df = df.copy(); df["p"] = clf.predict_proba(X)[:, 1]
    print(f"#### KEYSTONE LIVE candidate: depth-gate + trailing-exit, ${NOTIONAL_USD:.0f}, real QuoterV2 ####")
    print(f"pools={len(df)} TRAIN={tr.sum()} (<= {cut.date()}) TEST={te.sum()}  "
          f"tick-fatal holdout AUC={roc_auc_score(y[te], df.p[te]):.3f}  TRAIL={TRAIL} hardstop={1-math.exp(HARD_STOP):.0%}")
    head = int(rpc.call("eth_blockNumber", [], timeout=15), 16)
    cache = price_all(df, path, head)
    df["fric"] = df.pool.map(lambda x: cache[x]["fric"])
    df["net"] = df.pool.map(lambda x: cache[x]["net"])
    df["why"] = df.pool.map(lambda x: cache[x]["why"])
    df["exwhy"] = df.pool.map(lambda x: cache[x].get("exwhy"))
    ok = df[df.why == "ok"]
    print(f"\npriced ok={len(ok)}  entry_fail={ (df.why=='entry_fail').sum() }  bad_fee={ (df.why=='bad_fee').sum() }")
    trd, ted = df[tr & (df.why == "ok")], df[te & (df.why == "ok")]
    te_days = (ted.t0.max() - ted.t0.min()).days or 1
    print("\n[HONEST PROTOCOL] p-threshold and friction gate fixed on TRAIN, applied verbatim to TEST holdout.")
    for thr in (-0.02, -0.05):
        gtr = trd[trd.fric >= thr]
        gte = ted[ted.fric >= thr]
        if len(gtr) == 0 or len(gte) == 0:
            continue
        p_cut = gtr.p.quantile(0.6)            # learned on TRAIN only
        print(f"\n=== friction>= {thr:+.2f}  | p_cut(TRAIN q60)={p_cut:.3f} ===")
        stat("TRAIN gate-only", gtr.net.values)
        stat("TRAIN gate+model", gtr[gtr.p <= p_cut].net.values)
        stat("TEST  gate-only", gte.net.values)
        kept = gte[gte.p <= p_cut]
        stat("TEST  gate+model (HOLDOUT)", kept.net.values)
        nmo = len(kept) / te_days * 30
        r = kept.net.dropna().values
        if len(r):
            for fpos in (0.05, 0.10):
                print(f"      -> ~{nmo:.0f} trades/mo x size {int(fpos*100)}% x mean {r.mean():+.3f}"
                      f"  => ~{nmo*fpos*r.mean()*100:+.0f}%/mo (real-cost, gross-of-MEV/latency)")


if __name__ == "__main__":
    main()
