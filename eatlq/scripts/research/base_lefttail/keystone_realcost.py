#!/usr/bin/env python3
"""KEYSTONE reality accounting — net the left-tail predictor's P&L through REAL
QuoterV2 round-trips: buy WETH->alt at entry block, sell alt->WETH at entry+~4h
block, at $NOTIONAL, real LP fee + both-side slippage. sell quote reverts =>
honeypot/rug => -100%. This is the TRUE money: it captures slippage, rugs and
honeypots that the tick proxy could not, and CORRECTS the over-harsh tick rule
that booked every silent pool at -100%.

Model is unchanged (trained on cheap tick features, all TRAIN pools). We only
re-measure realized P&L on the archive-reachable TEST subset and redo the convex
prune check on REAL net returns.  Base ~2s blocks => 4h ~= 7200 blocks.
"""
from __future__ import annotations
import subprocess, math, sys, os, json, time
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
sys.path.insert(0, "/opt/EATLQ/scripts/lib")
from v3_quoterv2_struct import JsonRpcClient, WETH_BASE, quote_exact_input_single_struct  # noqa

WETH = WETH_BASE.lower()
LN1P = math.log(1.0001)
W, H = 600, 14400
MIN_PRE = 5
DATE_LO, DATE_HI = "2025-10-01", "2026-06-01"
STOP = math.log(0.5)
ARCH_MIN = 45_000_000
HOLD_BLOCKS = 7200                       # ~4h on Base (2s blocks)
NOTIONAL_USD, ETH_USD = 100.0, 3000.0
FEE_SELECTOR = "0xddca3f43"
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
CACHE = "/opt/EATLQ/scripts/research/base_lefttail/_pools.tsv"
PRICE_CACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced.json"
RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
rpc = JsonRpcClient(RPC_URL, delay_s=0.03)
_fee_cache: dict[str, int] = {}

SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a, lower(token1_address) t1a
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL GROUP BY pool_address
  HAVING t0 >= '{DATE_LO} 00:00:00' AND t0 <= '{DATE_HI} 23:59:59'
),
sw AS (
  SELECT s.pool_address pool, s.tick tick, s.block_time bt, s.block_number bn,
         s.recipient rcp, s.sender snd, toFloat64(s.liquidity) liq,
         dateDiff('second', fs.t0, s.block_time) sec, fs.t0 t0
  FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON s.pool_address=fs.pool_address
  WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
    AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W+H}
)
SELECT pool, if(c.t0a='{WETH}', -1, 1) orient,
  if(c.t0a='{WETH}', c.t1a, c.t0a) alt, any(t0) t0,
  countIf(sec<= {W}) ns_pre, uniqExactIf(rcp, sec<= {W}) buyers_pre,
  uniqExactIf(snd, sec<= {W}) senders_pre,
  argMinIf(tick, bt, sec<= {W}) first_tick, argMaxIf(tick, bt, sec<= {W}) last_tick_pre,
  minIf(tick, sec<= {W}) min_tick_pre, maxIf(tick, sec<= {W}) max_tick_pre,
  argMinIf(liq, bt, sec<= {W}) liq_first, argMaxIf(liq, bt, sec<= {W}) liq_last_pre,
  minIf(liq, sec<= {W}) liq_min_pre,
  countIf(sec<= {W//2}) ns_h1, countIf(sec> {W//2} AND sec<= {W}) ns_h2,
  argMaxIf(tick, bt, sec<= {W//2}) last_tick_h1,
  argMaxIf(bn, bt, sec<= {W}) entry_block,
  countIf(sec> {W} AND sec<= {W+H}) ns_fwd,
  minIf(tick, sec> {W} AND sec<= {W+H}) min_tick_fwd,
  maxIf(tick, sec> {W} AND sec<= {W+H}) max_tick_fwd,
  argMaxIf(tick, bt, sec> {W} AND sec<= {W+H}) last_tick_fwd
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient, alt HAVING ns_pre >= {MIN_PRE}
FORMAT TabSeparated
"""
COLS = ["pool","orient","alt","t0","ns_pre","buyers_pre","senders_pre","first_tick",
        "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre",
        "liq_min_pre","ns_h1","ns_h2","last_tick_h1","entry_block","ns_fwd",
        "min_tick_fwd","max_tick_fwd","last_tick_fwd"]


def load_df():
    if not os.path.exists(CACHE):
        r = subprocess.run(CH + [SQL], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print("SQL ERR", r.stderr[:1500]); sys.exit(1)
        open(CACHE, "w").write(r.stdout)
    df = pd.read_csv(CACHE, sep="\t", names=COLS)
    df["t0"] = pd.to_datetime(df["t0"])
    for c in COLS:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


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
    fwd_last = np.where(has, lp(df.last_tick_fwd), -np.inf)
    fmin = fwd_lo - lp_last
    death = (df.ns_fwd <= 2).values
    fatal = death | (fmin <= STOP)
    return f.replace([np.inf, -np.inf], np.nan).fillna(0.0), fatal.astype(int)


def pool_fee(pool):
    if pool in _fee_cache:
        return _fee_cache[pool]
    try:
        res = rpc.call("eth_call", [{"to": pool, "data": FEE_SELECTOR}, "latest"], timeout=15)
        v = int(res, 16) if res and res != "0x" else None
    except Exception:
        v = None
    _fee_cache[pool] = v
    return v


def roundtrip(pool, alt, fee, eb, xb):
    """Return (ret_4h, ret_friction, why). friction = buy then sell SAME entry
    block (pure fee+slippage+honeypot). ret_4h = sell xb blocks later."""
    amt = int(NOTIONAL_USD / ETH_USD * 1e18)
    qi = quote_exact_input_single_struct(rpc, WETH_BASE, alt, amt, fee, block_identifier=eb)
    if not qi["ok"]:
        return None, None, "entry_fail"
    out = qi["amount_out"]
    qf = quote_exact_input_single_struct(rpc, alt, WETH_BASE, out, fee, block_identifier=eb)
    ret_f = -1.0 if not qf["ok"] else qf["amount_out"] / amt - 1.0
    qo = quote_exact_input_single_struct(rpc, alt, WETH_BASE, out, fee, block_identifier=xb)
    if not qo["ok"]:
        return -1.0, ret_f, "sell_fail"     # honeypot / rug at 4h
    return qo["amount_out"] / amt - 1.0, ret_f, "ok"


def price_test(df, te_mask, head):
    cache = json.load(open(PRICE_CACHE)) if os.path.exists(PRICE_CACHE) else {}
    sub = df[te_mask & (df.entry_block >= ARCH_MIN)]
    print(f"  archive-reachable TEST pools (entry_block>={ARCH_MIN}): {len(sub)}")
    out = {}
    for i, (_, r) in enumerate(sub.iterrows()):
        pool = r.pool
        if pool in cache:
            out[pool] = cache[pool]; continue
        fee = pool_fee(pool)
        if fee not in (100, 500, 3000, 10000):
            out[pool] = {"ret": None, "fric": None, "why": "bad_fee"}; continue
        xb = min(int(r.entry_block) + HOLD_BLOCKS, head)
        ret, fric, why = roundtrip(pool, r.alt, fee, int(r.entry_block), xb)
        out[pool] = {"ret": ret, "fric": fric, "why": why}
        if (i + 1) % 25 == 0:
            print(f"    priced {i+1}/{len(sub)} rpc={rpc.calls}", flush=True)
            cache.update(out); json.dump(cache, open(PRICE_CACHE, "w"))
    cache.update(out); json.dump(cache, open(PRICE_CACHE, "w"))
    return out


def desc(tag, ret, why):
    ret = np.array([x for x in ret if x is not None])
    if len(ret) == 0:
        print(f"  {tag:18s} n=0"); return
    sf = np.mean([w == "sell_fail" for w in why])
    fatal = np.mean(ret <= -0.5)
    win_thr = 0.30
    nwin = int((ret >= win_thr).sum())
    print(f"  {tag:18s} n={len(ret):4d} fatal={fatal:.3f} sell_fail={sf:.3f} "
          f"mean={ret.mean():+.3f} med={np.median(ret):+.3f} winrate={(ret>0).mean():.2f} "
          f"best={ret.max():+.2f} winners(>30%)={nwin}")


def main():
    df = load_df()
    X, y = build(df)
    cut = df.t0.quantile(0.6); tr = df.t0 <= cut; te = ~tr
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                         l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    clf.fit(X[tr], y[tr])
    p_te = clf.predict_proba(X[te])[:, 1]
    print(f"#### KEYSTONE REAL-COST audit | ${NOTIONAL_USD:.0f} QuoterV2 round-trip, hold ~4h ####")
    print(f"pools={len(df)} TRAIN={tr.sum()} TEST={te.sum()}  holdout AUC(tick-fatal)={roc_auc_score(y[te],p_te):.3f}")
    head = int(rpc.call("eth_blockNumber", [], timeout=15), 16)
    print(f"chain head block={head}")
    priced = price_test(df, te, head)
    # align scores to reachable pools
    te_df = df[te].copy(); te_df["p"] = p_te
    te_df = te_df[te_df.pool.isin(priced)]
    te_df["ret"] = te_df.pool.map(lambda x: priced[x]["ret"])
    te_df["fric"] = te_df.pool.map(lambda x: priced[x].get("fric"))
    te_df["why"] = te_df.pool.map(lambda x: priced[x]["why"])
    te_df = te_df[te_df.ret.notna()]
    span_days = (te_df.t0.max() - te_df.t0.min()).days or 1
    fr = te_df.fric.dropna().values
    print(f"\n--- FRICTION decomposition (buy+sell SAME entry block, ${NOTIONAL_USD:.0f}) ---")
    print(f"  pure friction: mean={fr.mean():+.3f} med={np.median(fr):+.3f} "
          f"sell_fail@entry={np.mean(fr<=-0.999):.3f}  (this is cost to get IN and immediately OUT)")
    print(f"  => of the 4h loss, friction explains the part above; rest is 4h price move")
    # depth is the real lever: bucket by pre-entry liquidity, show friction + 4h net
    print(f"\n--- REAL net by PRE-ENTRY LIQUIDITY decile (the friction lever) ---")
    te_df = te_df.copy()
    te_df["liqr"] = te_df.liq_last_pre.rank(pct=True)
    for lo, hi, tag in [(0.0,0.5,"shallow 50%"),(0.5,0.8,"mid 30%"),(0.8,0.95,"deep 15%"),(0.95,1.01,"deepest 5%")]:
        b = te_df[(te_df.liqr>=lo)&(te_df.liqr<hi)]
        if len(b)==0: continue
        fr=b.fric.dropna().values; rt=b.ret.values
        print(f"  {tag:13s} n={len(b):3d}  friction med={np.median(fr):+.3f} mean={fr.mean():+.3f} | "
              f"4h net mean={rt.mean():+.3f} med={np.median(rt):+.3f} winrate={(rt>0).mean():.2f} best={rt.max():+.2f}")
    # the executable gate: a live bot QUOTES the round-trip before entering and
    # only buys if friction is acceptable. friction is observable pre-trade -> not look-ahead.
    print(f"\n--- FRICTION-GATED universe (only enter if live round-trip quote is cheap) ---")
    g = te_df[te_df.fric.notna()].copy()
    for thr in (-0.02, -0.05, -0.10):
        s = g[g.fric >= thr]
        if len(s)==0: print(f"  friction>= {thr:+.2f}: n=0"); continue
        rt=s.ret.values
        nmo = len(s)/span_days*30
        print(f"  friction>= {thr:+.2f}: n={len(s):3d} ({100*len(s)/len(g):.0f}% of universe, ~{nmo:.0f}/mo) | "
              f"4h net mean={rt.mean():+.3f} med={np.median(rt):+.3f} winrate={(rt>0).mean():.2f} "
              f"best={rt.max():+.2f} fatal={np.mean(rt<=-0.5):.3f}")
    print(f"\n--- REAL net returns (priced n={len(te_df)}, span {span_days}d) ---")
    print("--- CONVEX CHECK on REAL money (prune predicted-fatal, keep winners?) ---")
    desc("ALL reachable", te_df.ret.values, te_df.why.values)
    for q in (0.30, 0.50, 0.70):
        keep = te_df[te_df.p <= te_df.p.quantile(q)]
        desc(f"keep safest {int(q*100)}%", keep.ret.values, keep.why.values)
        # monthly estimate for this prune level
        n_keep_month = len(keep) / span_days * 30
        mean = np.mean([x for x in keep.ret.values if x is not None])
        for fpos in (0.10, 0.20):
            print(f"      -> ~{n_keep_month:.0f} trades/mo  x size {int(fpos*100)}% x mean {mean:+.3f}"
                  f"  => ~{n_keep_month*fpos*mean*100:+.0f}%/mo (gross-of-funding, real-cost)")


if __name__ == "__main__":
    main()
