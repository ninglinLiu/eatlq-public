#!/usr/bin/env python3
"""GL12: evaluate a POINT-IN-TIME funder-level gate.

Gate rule (PIT-honest): when considering entry to pool P at block E, look at
P's deployer's first funder F. Reject P iff F had ALREADY funded some deployer
whose token rugged at flip_block < E (a rug that was observable before E).

Measures, on the labeled universe:
  - rug catch rate  = rugs rejected / rugs total (with deployer+funder resolved)
  - clean kill rate = cleans rejected / cleans total (the false-positive cost)
  - also a naive (non-PIT) funder-overlap view for context.
Inputs:
  _gl12_pools.json, _gl12_funding_raw.json (deployer -> first_funder)
READ-ONLY.
"""
import json, os

OUTDIR = "/opt/EATLQ/scripts/research/base_gl12"
pools = json.load(open(os.path.join(OUTDIR, "_gl12_pools.json")))
funding_rows = json.load(open(os.path.join(OUTDIR, "_gl12_funding_raw.json")))
if isinstance(funding_rows, dict) and "error" in funding_rows:
    raise SystemExit(f"funding query errored: {funding_rows}")

dep2funder = {}
dep2fundblk = {}
for r in funding_rows:
    d = (r.get("deployer") or "").lower()
    f = (r.get("first_funder") or "").lower()
    if d and f:
        dep2funder[d] = f
        dep2fundblk[d] = r.get("first_fund_blk")

# funder global fanout (distinct addresses funded across all Base) -> infra vs bespoke
fanout = {}
try:
    for r in json.load(open(os.path.join(OUTDIR, "_gl12_funder_fanout.json"))):
        fanout[(r.get("funder") or "").lower()] = int(r.get("global_fanout") or 0)
except Exception:
    pass

# attach funder to each pool
for p in pools:
    p["funder"] = dep2funder.get(p["deployer"] or "")

resolved = [p for p in pools if p["deployer"] and p["funder"]]
rugs = [p for p in resolved if p["is_rug"]]
cleans = [p for p in resolved if not p["is_rug"]]

# funder -> list of (flip_block) for rug pools it is linked to (via deployer)
funder_rug_flips = {}
for p in rugs:
    funder_rug_flips.setdefault(p["funder"], []).append(p.get("flip_block") or 0)

def gate_rejects(p):
    """PIT: reject if this funder previously funded a rug that flipped before E."""
    E = p.get("entry_block")
    flips = funder_rug_flips.get(p["funder"], [])
    prior = [fb for fb in flips if fb and E and fb < E and fb != (p.get("flip_block") or -1)]
    # also count self-linkage only via OTHER pools: if same funder had a distinct earlier rug
    return len(prior) > 0

rug_caught = sum(1 for p in rugs if gate_rejects(p))
clean_killed = sum(1 for p in cleans if gate_rejects(p))

# Naive (non-PIT, upper bound): funder ever linked to any rug
funders_with_rug = set(funder_rug_flips.keys())
rug_naive = sum(1 for p in rugs if p["funder"] in funders_with_rug)
clean_naive = sum(1 for p in cleans if p["funder"] in funders_with_rug)

# funder-level overlap: funders that fund BOTH rug and clean deployers
funder_side = {}
for p in resolved:
    s = funder_side.setdefault(p["funder"], {"rug": 0, "clean": 0})
    s["rug" if p["is_rug"] else "clean"] += 1
both = {f: s for f, s in funder_side.items() if s["rug"] and s["clean"]}
rug_only = {f: s for f, s in funder_side.items() if s["rug"] and not s["clean"]}

print("=== GL12 funder-gate evaluation ===")
print(f"resolved pools (deployer+funder): {len(resolved)}  rug={len(rugs)} clean={len(cleans)}")
print(f"distinct funders: {len(funder_side)}")
print()
print("--- PIT funder gate (reject if funder had a rug flip before entry) ---")
print(f"  rug caught   : {rug_caught}/{len(rugs)} = {rug_caught/max(1,len(rugs)):.1%}")
print(f"  clean killed : {clean_killed}/{len(cleans)} = {clean_killed/max(1,len(cleans)):.1%}")
print()
print("--- Naive (non-PIT upper bound: funder EVER linked to a rug) ---")
print(f"  rug caught   : {rug_naive}/{len(rugs)} = {rug_naive/max(1,len(rugs)):.1%}")
print(f"  clean killed : {clean_naive}/{len(cleans)} = {clean_naive/max(1,len(cleans)):.1%}")
print()
print("--- funder-level FP structure ---")
print(f"  funders funding BOTH rug & clean: {len(both)}")
print(f"  funders funding ONLY rug        : {len(rug_only)}")
print(f"  (top mixed funders):")
for f, s in sorted(both.items(), key=lambda kv: -(kv[1]['rug']+kv[1]['clean']))[:8]:
    print(f"    {f[:14]} rug={s['rug']} clean={s['clean']}")

print()
print("--- REFINED PIT gate: exclude high-fanout infra funders (CEX/bridge/launchpad) ---")
print("  (fire only if funder had prior rug flip AND funder global_fanout <= T)")
sweep = {}
for T in [10, 25, 50, 100, 250, 500, 1000]:
    def refined_reject(p):
        fo = fanout.get(p["funder"], 10**9)
        if fo > T:
            return False
        E = p.get("entry_block")
        flips = funder_rug_flips.get(p["funder"], [])
        prior = [fb for fb in flips if fb and E and fb < E and fb != (p.get("flip_block") or -1)]
        return len(prior) > 0
    rc = sum(1 for p in rugs if refined_reject(p))
    ck = sum(1 for p in cleans if refined_reject(p))
    prec = rc / max(1, rc + ck)
    sweep[T] = {"rug_caught": rc, "clean_killed": ck}
    print(f"  T<={T:>4}: rug {rc:>3}/{len(rugs)}={rc/len(rugs):>5.1%}  clean {ck:>3}/{len(cleans)}={ck/len(cleans):>5.1%}  precision={prec:.1%}")

json.dump({
    "resolved": len(resolved), "rug": len(rugs), "clean": len(cleans),
    "pit_gate": {"rug_caught": rug_caught, "rug_total": len(rugs),
                 "clean_killed": clean_killed, "clean_total": len(cleans)},
    "naive_gate": {"rug_caught": rug_naive, "clean_killed": clean_naive},
    "funders_both": len(both), "funders_rug_only": len(rug_only),
    "refined_sweep": sweep,
}, open(os.path.join(OUTDIR, "_gl12_gate_result.json"), "w"), indent=1)
