#!/usr/bin/env python3
"""Honest EV re-backtest: fork-replay sellability truth + rug haircut (READ-ONLY).

The standing keystone_live EV uses QuoterV2 for the exit price, which is
structurally blind to LP-pull / honeypot rugs (QuoterV2 quotes a healthy sell
even when the real sell would revert). This script overrides that blind spot:
for every pool the strategy would ACTUALLY trade (p_fatal-gated + friction-ok),
we take the fork-replay ground-truth label. If the pool rugs (LP pulled / becomes
untradeable) at or before OUR causal exit block, we book -100% (not the QuoterV2
number). Otherwise the pool was genuinely sellable at exit and the QuoterV2
round-trip net stands. All entered pools stay in the denominator (no survivorship).

Data (all pre-computed, zero RPC):
  _uni_shards/_uni_s*.json  fork-replay labels (label/flip_block/flip_offset)
  _priced_live.json         QuoterV2 net + causal exit block (exb) per pool
  _universe_blocks.json     p-gated universe + in_traded (friction-ok) flag
"""
import json, glob, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# 1) fork-replay labels
labels = {}
for f in glob.glob(os.path.join(HERE, "_uni_shards/_uni_s*.json")):
    d = json.load(open(f))
    for r in (d["pools"] if isinstance(d, dict) else d):
        labels[r["pool"]] = r

# 2) QuoterV2 priced cache (net + exit block)
priced = json.load(open(os.path.join(HERE, "_priced_live.json")))

# 3) p-gated universe + friction-ok flag
ub = json.load(open(os.path.join(HERE, "_universe_blocks.json")))
rows = ub["rows"]
meta = ub["meta"]

GAS_RT_USD = 0.30        # fixed round-trip gas on Base
NOTIONAL = 100.0
gas_frac = GAS_RT_USD / NOTIONAL   # gas as fraction of notional


def classify(pool, exb):
    """Return ('rug'|'dead'|'hp'|'sellable', flip_block_or_None)."""
    lab = labels.get(pool)
    if lab is None:
        return "unlabeled", None
    L = lab.get("label")
    if L == "dead":
        return "dead", None
    if L == "instant_hp":
        return "hp", None
    if L == "delayed_rug":
        fb = lab.get("flip_block")
        if exb is not None and fb is not None and fb <= exb:
            return "rug", fb          # rugged at/before our exit -> real loss
        return "sellable", fb          # rugged AFTER we exited -> we got out
    return "sellable", None            # clean


def run(universe_rows, haircut, tag):
    nets_baseline, nets_honest = [], []
    n_rug = n_dead = n_hp = n_sellable = n_unlabeled = 0
    for r in universe_rows:
        pool = r["pool"]
        pc = priced.get(pool)
        if not pc or pc.get("net") is None or pc.get("exb") is None:
            continue                    # not priceable -> not entered live
        net = float(pc["net"]) - gas_frac
        exb = pc["exb"]
        kind, _fb = classify(pool, exb)
        nets_baseline.append(net)       # what the QuoterV2 backtest believed
        if kind in ("rug", "dead", "hp"):
            nets_honest.append(haircut)
            if kind == "rug": n_rug += 1
            elif kind == "dead": n_dead += 1
            else: n_hp += 1
        else:
            nets_honest.append(net)
            n_sellable += 1
            if kind == "unlabeled":
                n_unlabeled += 1
    b, h = np.array(nets_baseline), np.array(nets_honest)
    print(f"\n===== {tag}  (haircut={haircut:+.0%}) =====")
    print(f"  entered n={len(h)}  | rug_before_exit={n_rug} dead={n_dead} instant_hp={n_hp} sellable={n_sellable}")
    print(f"  [baseline QuoterV2]  mean={b.mean():+.3f}  median={np.median(b):+.3f}  "
          f"WR={(b>0).mean():.2f}  fatal(<=-50%)={(b<=-0.5).mean():.3f}")
    print(f"  [HONEST sellability] mean={h.mean():+.3f}  median={np.median(h):+.3f}  "
          f"WR={(h>0).mean():.2f}  fatal(<=-50%)={(h<=-0.5).mean():.3f}")
    # PnL contribution concentration: drop top-3 winners
    order = np.argsort(h)[::-1]
    if len(h) > 3:
        h_no_top3 = np.delete(h, order[:3])
        print(f"  [HONEST, minus top-3 winners] mean={h_no_top3.mean():+.3f}  n={len(h_no_top3)}")
    return h


print("#### HONEST SELLABILITY-AWARE EV RE-BACKTEST ####")
print(f"universe meta: {json.dumps(meta)}")
print(f"fork-replay labels loaded: {len(labels)}")

# Universe A: what we ACTUALLY trade live = p-gated + friction-ok
traded = [r for r in rows if r.get("in_traded_131_217")]
# Universe B: full p-gated candidate intent (anti-survivorship: include friction-hidden)
full = rows

for hc in (-1.0, -0.9):
    run(traded, hc, "A) LIVE-TRADED (p-gate + friction-ok)")
run(full, -1.0, "B) FULL p-gated candidate universe (anti-survivorship)")
