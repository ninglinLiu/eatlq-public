#!/usr/bin/env python3
"""GL12 EV stack: does gating (funding + safe-skeleton) improve net EV on the
strategy's actual executable set (GL11 339 legs), after fees and after the cost
of killing clean winners?

Return convention (GL11 BASELINE_LOWER): NET return, 0=breakeven, -1=total loss.
Gates are PIT-honest. LP-lock gate not yet wired (no per-pool data) -> noted.
READ-ONLY.
"""
import json, os, statistics as st

ROOT = "/opt/EATLQ"
GL12 = os.path.join(ROOT, "scripts/research/base_gl12")
LT = os.path.join(ROOT, "scripts/research/base_lefttail")

legs = json.load(open(os.path.join(ROOT, "mvp/diagnostics/base_gl11_bankroll_variance_capacity_per_leg_20260716.json")))["rows"]
pools = {p["pool"].lower(): p for p in json.load(open(os.path.join(GL12, "_gl12_pools.json")))}
dep2funder = {(r.get("deployer") or "").lower(): (r.get("first_funder") or "").lower()
              for r in json.load(open(os.path.join(GL12, "_gl12_funding_raw.json"))) if r.get("first_funder")}
fanout = {(r.get("funder") or "").lower(): int(r.get("global_fanout") or 0)
          for r in json.load(open(os.path.join(GL12, "_gl12_funder_fanout.json")))}
tok_skel = {k.lower(): v for k, v in json.load(open(os.path.join(LT, "_token_skeleton_644.json"))).items()}
safe_skel = set(json.load(open(os.path.join(LT, "_rug_seed_validated.json")))["safe_skeletons"])

# pool_share per token (from concentration reconstructions)
pool_share = {}
for fn in ("_gl12_concentration.json", "_gl12_concentration2.json"):
    try:
        for r in json.load(open(os.path.join(GL12, fn))):
            sup = r.get("supply") or 0
            if sup > 0:
                pool_share[(r.get("token") or "").lower()] = (r.get("pool_bal") or 0) / sup
    except Exception:
        pass
POOLSHARE_T = 0.4

# funder -> rug flip blocks (from 644 labeled rug pools with resolved funder)
funder_rug_flips = {}
for pl in pools.values():
    if not pl["is_rug"]:
        continue
    f = dep2funder.get((pl["deployer"] or ""))
    if f and pl.get("flip_block"):
        funder_rug_flips.setdefault(f, []).append(pl["flip_block"])

FANOUT_INFRA = 1000  # funders above this are CEX/bridge/launchpad infra -> not signal

def funding_reject(pool, entry_block):
    pl = pools.get(pool)
    if not pl:
        return False
    f = dep2funder.get((pl["deployer"] or ""))
    if not f or fanout.get(f, 10**9) > FANOUT_INFRA:
        return False
    prior = [fb for fb in funder_rug_flips.get(f, []) if fb < entry_block and fb != (pl.get("flip_block") or -1)]
    return len(prior) > 0

def skeleton_reject(pool):
    pl = pools.get(pool)
    if not pl:
        return False
    return tok_skel.get(pl["token"]) in safe_skel

def poolshare_reject(pool):
    pl = pools.get(pool)
    if not pl:
        return False
    ps = pool_share.get(pl["token"])
    return ps is not None and ps < POOLSHARE_T

# assemble legs with return + gate flags
data = []
missing = 0
for lg in legs:
    pool = lg["pool"].lower()
    r = lg["returns"]["BASELINE_LOWER"]  # net return
    eb = lg["entry_block"]
    in_uni = pool in pools
    if not in_uni:
        missing += 1
    data.append({
        "pool": pool, "r": r, "entry_block": eb,
        "rug_outcome": lg.get("rug_outcome"),
        "f_rej": funding_reject(pool, eb),
        "s_rej": skeleton_reject(pool),
        "p_rej": poolshare_reject(pool),
    })

def stats(subset, cost):
    rs = [d["r"] - cost for d in subset]
    if not rs:
        return None
    losers = [d for d in subset if d["r"] < -0.5]
    winners = [d for d in subset if d["r"] > 0.1]
    return {
        "n": len(subset), "ev": st.mean(rs), "median": st.median([d["r"] for d in subset]),
        "loss_legs": len(losers), "loss_rate": len(losers) / len(subset),
        "win_legs": len(winners),
    }

def report(name, kept, removed, cost):
    k = stats(kept, cost)
    rem_losers = sum(1 for d in removed if d["r"] < -0.5)
    rem_winners = sum(1 for d in removed if d["r"] > 0.1)
    rem_ret = sum(d["r"] for d in removed)
    total_profit = sum(d["r"] - cost for d in kept)
    print(f"\n[{name}]  (cost/trade={cost:.1%})")
    print(f"  kept n={k['n']}  EV/trade={k['ev']:+.3f}  TOTAL_PROFIT(sum ret)={total_profit:+.1f}  loss_legs={k['loss_legs']} ({k['loss_rate']:.1%})")
    print(f"  removed n={len(removed)}  losers(<-50%)={rem_losers}  winners(>+10%)={rem_winners}  sum_removed_ret={rem_ret:+.2f}")

print("=== GL12 EV stack on GL11 executable set ===")
print(f"legs={len(data)}  legs not in labeled universe (no gate)={missing}")
print(f"funding-rej={sum(d['f_rej'] for d in data)}  skeleton-rej={sum(d['s_rej'] for d in data)}  poolshare<{POOLSHARE_T}-rej={sum(d['p_rej'] for d in data)}")
print(f"rug_outcome=True legs={sum(1 for d in data if d['rug_outcome'])}  loss legs(<-50%)={sum(1 for d in data if d['r']<-0.5)}")

cost = 0.01
print(f"\n================= COST = {cost:.0%} =================")
report("BASELINE no gate", data, [], cost)
for name, key in [("FUNDING", "f_rej"), ("SKELETON", "s_rej"), ("POOLSHARE<0.4", "p_rej")]:
    kept = [d for d in data if not d[key]]; rem = [d for d in data if d[key]]
    report(name, kept, rem, cost)
kept = [d for d in data if not (d["s_rej"] or d["p_rej"])]; rem = [d for d in data if (d["s_rej"] or d["p_rej"])]
report("SKELETON+POOLSHARE", kept, rem, cost)
kept = [d for d in data if not (d["f_rej"] or d["s_rej"] or d["p_rej"])]; rem = [d for d in data if (d["f_rej"] or d["s_rej"] or d["p_rej"])]
report("FULL STACK f+s+p", kept, rem, cost)

# save
out = {"legs": len(data), "missing_from_universe": missing,
       "funding_rejected": sum(d['f_rej'] for d in data),
       "skeleton_rejected": sum(d['s_rej'] for d in data),
       "baseline": stats(data, 0.0),
       "funding": stats([d for d in data if not d['f_rej']], 0.0),
       "skeleton": stats([d for d in data if not d['s_rej']], 0.0),
       "stack": stats([d for d in data if not (d['f_rej'] or d['s_rej'])], 0.0)}
json.dump(out, open(os.path.join(GL12, "_gl12_ev_stack.json"), "w"), indent=1)
