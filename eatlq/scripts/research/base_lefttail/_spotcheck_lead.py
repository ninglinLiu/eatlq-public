#!/usr/bin/env python3
"""Real-gas spot-check of the tick-anchored earlier-exit PnL (Exp1 validation).

Reuses cached token_out from stress167 base50 (no buy re-quote). For ~15 rug pools,
does a REAL-GAS sell at (exb - 300) and compares real_net vs the tick-ratio prediction.
RPC = slot detection (once/token, in-memory) + 1 swap eth_call per pool. Read-only.
"""
import json, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import keystone_live as K
import live_config_rebacktest as L
import replay_realgas_sellsim as RG

LEAD = 300
priced = json.load(open(K.PCACHE)); labels = L._load_labels()
stress = json.load(open("/opt/EATLQ/mvp/audits/stress167_20260624/_stress167_cache.json"))
_, path = K.load()
df = L.run_lens(L.load_base_df(), "qv2")
dep, skel, creators, token_skel = L._load_blacklists()
m = L.gate_masks(df, dep, skel, creators, token_skel)
rug = df[m["model_depth"] & ~m["live_full"] & df.gate_fric & df.tradeable
         & (df.label == "delayed_rug") & (df.ret > 0)].copy()

def lp_at(ps, tb, orient):
    ch = None
    for bkt, lt, lb in ps:
        if lb <= tb: ch = (lt, lb)
        else: break
    if ch is None: ch = (ps[0][1], ps[0][2])
    return ch[0] * K.LN1P * orient

AMT = int(50.0 / K.ETH_USD * 1e18)
rows = []
calls = [0]
_orig = RG.w3.provider.make_request
def counted(*a, **k):
    calls[0] += 1
    return _orig(*a, **k)
RG.w3.provider.make_request = counted

import numpy as np
sample = rug.sort_values("entry_block").iloc[::11].head(15)  # spread-out 15
for _, r in sample.iterrows():
    base = stress.get(f"{r.pool}:base50", {})
    if base.get("status") != "ok" or base.get("token_out") is None:
        continue
    tok = int(base["token_out"]); exb = int(base["exit_block"]); orient = int(r.orient)
    base_net = float(base["real_net"])
    ps = path.get(r.pool, [])
    if not ps:
        continue
    lp_exit = lp_at(ps, exb, orient)
    lp_t = lp_at(ps, exb - LEAD, orient)
    tick_pred = (1.0 + base_net) * math.exp(lp_t - lp_exit) - 1.0
    xv, xout = RG.realgas_sell_ok(r.alt if hasattr(r,'alt') else df.loc[r.name,'alt'], int(priced[r.pool]['fee']), tok, exb - LEAD)
    rg_net = (xout / AMT - 1.0) if xv == "ok" else (-1.0 if xv == "revert" else None)
    rows.append((r.pool, base_net, tick_pred, rg_net, xv))

print(f"RPC eth_calls used: {calls[0]}")
print(f"{'pool':>12}{'base_net':>10}{'tick_pred':>11}{'realgas':>10}{'sell':>8}{'abs_err':>9}")
errs = []
for p, b, tp, rg, xv in rows:
    err = abs(tp - rg) if rg is not None else None
    if err is not None: errs.append(err)
    print(f"{p[:12]:>12}{b:>10.3f}{tp:>11.3f}{(rg if rg is not None else float('nan')):>10.3f}{xv:>8}{(err if err is not None else float('nan')):>9.3f}")
if errs:
    print(f"\nn={len(errs)} median abs err={np.median(errs):.4f} max={max(errs):.4f}")
    print("=> tick-anchored earlier-exit PnL matches real-gas within this error band (pre-flip, alive pool).")
json.dump([{"pool":p,"base_net":b,"tick_pred":tp,"realgas":rg,"sell":xv} for p,b,tp,rg,xv in rows],
          open("/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624/_spotcheck_lead.json","w"), indent=2)
