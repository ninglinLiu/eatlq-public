#!/usr/bin/env python3
"""GL12 prep: assemble the pool-level table for the PIT funder-gate experiment.

Reuses the 2026-06-24 funding graph (token->creator) and the 644 labeled
candidate universe. Produces:
  _gl12_pools.json  : per-pool {pool, token, deployer, entry_block, is_rug, flip_block}
  _gl12_deployers.json : {deployer: earliest_creation_block} across rug+clean
READ-ONLY over source artifacts.
"""
import json, os

ROOT = "/opt/EATLQ"
LT = os.path.join(ROOT, "scripts/research/base_lefttail")
POC = os.path.join(ROOT, "mvp/audits/funding_cluster_poc_20260624")
OUTDIR = os.path.join(ROOT, "scripts/research/base_gl12")
os.makedirs(OUTDIR, exist_ok=True)

graph = json.load(open(os.path.join(POC, "deployer_funding_graph.json")))
token_creators = {k.lower(): v for k, v in graph["token_creators"].items()}

flat = json.load(open(os.path.join(LT, "_universe_blocks_flat.json")))
rug_pools = json.load(open(os.path.join(LT, "_labeled_universe.json")))["rug_pools"]
rug_by_pool = {r["pool"].lower(): r for r in rug_pools}

pools = []
missing_creator = 0
for row in flat:
    pool = row["pool"].lower()
    token = row["token"].lower()
    tc = token_creators.get(token)
    deployer = (tc or {}).get("creator", "").lower() or None
    if deployer is None:
        missing_creator += 1
    rug = rug_by_pool.get(pool)
    pools.append({
        "pool": pool,
        "token": token,
        "deployer": deployer,
        "entry_block": row.get("entry_block"),
        "fee": row.get("fee"),
        "is_rug": rug is not None,
        "flip_block": (rug or {}).get("flip_block"),
        "mechanism": (rug or {}).get("mechanism"),
    })

# earliest creation block per deployer (from token_creators block field)
dep_earliest = {}
for token, tc in token_creators.items():
    dep = (tc.get("creator") or "").lower()
    blk = tc.get("block")
    if not dep or blk in (None, ""):
        continue
    blk = int(blk)
    if dep not in dep_earliest or blk < dep_earliest[dep]:
        dep_earliest[dep] = blk

# restrict to deployers that appear in our labeled universe
uni_deployers = {p["deployer"] for p in pools if p["deployer"]}
dep_out = {d: dep_earliest.get(d) for d in uni_deployers}

json.dump(pools, open(os.path.join(OUTDIR, "_gl12_pools.json"), "w"), indent=1)
json.dump(dep_out, open(os.path.join(OUTDIR, "_gl12_deployers.json"), "w"), indent=1)

n_rug = sum(1 for p in pools if p["is_rug"])
n_rug_with_dep = sum(1 for p in pools if p["is_rug"] and p["deployer"])
n_clean_with_dep = sum(1 for p in pools if not p["is_rug"] and p["deployer"])
print(f"pools={len(pools)} rug={n_rug} clean={len(pools)-n_rug}")
print(f"pools missing creator={missing_creator}")
print(f"rug pools with deployer={n_rug_with_dep}  clean pools with deployer={n_clean_with_dep}")
print(f"unique deployers in universe={len(uni_deployers)}  with creation block={sum(1 for v in dep_out.values() if v)}")
