#!/usr/bin/env python3
"""Mine rug deployers + funding-source clusters; FP-validate vs 378 clean winners.

Uses Blockscout (Base) for getcontractcreation + txlist. Basescan V2 is tried if
BASESCAN_API_KEY is set but Base requires a paid Etherscan plan on V2.

Outputs under mvp/audits/funding_cluster_poc_20260624/:
  - deployer_funding_graph.json
  - deployer_blacklist_validated.json  (safe deployers for live seed)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"
SHARDS = [ROOT / f"scripts/research/base_lefttail/_uni_shards/_uni_s{i}.json" for i in range(4)]
OUT_DIR = ROOT / "mvp/audits/funding_cluster_poc_20260624"

BLOCKSCOUT = "https://base.blockscout.com/api"
BASESCAN_V2 = "https://api.etherscan.io/v2/api"
CHAIN_ID = 8453
BATCH = 5
SLEEP = 0.25


def _get(url: str, tries: int = 4) -> dict:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EATLQ-research/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and k + 1 < tries:
                time.sleep(2 ** k)
                continue
            raise
        except Exception:
            if k + 1 >= tries:
                raise
            time.sleep(1 + k)
    return {}


def load_pools() -> Tuple[List[dict], List[dict]]:
    """Return (rug_pools, clean_pools) with token addresses."""
    rugs, cleans = [], []
    seen_rug, seen_clean = set(), set()

    def add(pool: dict):
        tok = (pool.get("token") or "").lower()
        if not tok:
            return
        lab = pool.get("label")
        if lab == "delayed_rug" and tok not in seen_rug:
            seen_rug.add(tok)
            rugs.append(pool)
        elif lab == "clean" and tok not in seen_clean:
            seen_clean.add(tok)
            cleans.append(pool)

    data = json.loads(LAB.read_text())
    for p in data.get("rug_pools", []):
        add(p)
    for shard in SHARDS:
        if not shard.exists():
            continue
        sd = json.loads(shard.read_text())
        for p in sd.get("pools", []):
            add(p)
    return rugs, cleans


def api_getcontractcreation(addrs: List[str], api_key: Optional[str]) -> List[dict]:
    joined = ",".join(a for a in addrs)
    if api_key:
        q = urllib.parse.urlencode({
            "chainid": CHAIN_ID,
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": joined,
            "apikey": api_key,
        })
        j = _get(f"{BASESCAN_V2}?{q}")
        if j.get("status") == "1" and isinstance(j.get("result"), list):
            return j["result"]
    q = urllib.parse.urlencode({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": joined,
    })
    j = _get(f"{BLOCKSCOUT}?{q}")
    if j.get("status") == "1" and isinstance(j.get("result"), list):
        return j["result"]
    raise RuntimeError(f"getcontractcreation failed addrs={addrs[:2]}… msg={j.get('message')} status={j.get('status')}")


def fetch_all_creators(tokens: List[str], api_key: Optional[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    todo = [t for t in tokens if t not in out]
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        for row in api_getcontractcreation(batch, api_key):
            ca = (row.get("contractAddress") or "").lower()
            creator = (row.get("contractCreator") or "").lower()
            if ca:
                out[ca] = {
                    "creator": creator,
                    "tx_hash": row.get("txHash") or "",
                    "block": row.get("blockNumber") or "",
                    "factory": (row.get("contractFactory") or "").lower(),
                }
        time.sleep(SLEEP)
        if (i // BATCH) % 20 == 0 and i:
            print(f"  creators {min(i + BATCH, len(todo))}/{len(todo)}", flush=True)
    return out


def first_eth_funder(deployer: str, api_key: Optional[str]) -> Optional[dict]:
    """First successful incoming native ETH transfer to deployer (txlist asc)."""
    if api_key:
        q = urllib.parse.urlencode({
            "chainid": CHAIN_ID,
            "module": "account",
            "action": "txlist",
            "address": deployer,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": 100,
            "sort": "asc",
            "apikey": api_key,
        })
        j = _get(f"{BASESCAN_V2}?{q}")
        if j.get("status") == "1" and isinstance(j.get("result"), list):
            rows = j["result"]
        else:
            rows = []
    else:
        rows = []
    if not rows:
        q = urllib.parse.urlencode({
            "module": "account",
            "action": "txlist",
            "address": deployer,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": 100,
            "sort": "asc",
        })
        j = _get(f"{BLOCKSCOUT}?{q}")
        if j.get("status") != "1" or not isinstance(j.get("result"), list):
            return None
        rows = j["result"]
    dep = deployer.lower()
    for tx in rows:
        if tx.get("isError") not in ("0", 0):
            continue
        to = (tx.get("to") or "").lower()
        if to != dep:
            continue
        try:
            val = int(tx.get("value") or "0")
        except ValueError:
            continue
        if val <= 0:
            continue
        frm = (tx.get("from") or "").lower()
        if not frm or frm == dep:
            continue
        return {
            "funder": frm,
            "value_wei": val,
            "tx_hash": tx.get("hash") or "",
            "block": tx.get("blockNumber") or "",
        }
    return None


def main() -> int:
    api_key = os.environ.get("BASESCAN_API_KEY") or os.environ.get("ETHERSCAN_API_KEY")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rugs, cleans = load_pools()
    rug_tokens = [p["token"].lower() for p in rugs]
    clean_tokens = [p["token"].lower() for p in cleans]
    all_tokens = sorted(set(rug_tokens + clean_tokens))
    print(f"tokens rug={len(rug_tokens)} clean={len(clean_tokens)} total={len(all_tokens)}", flush=True)

    print("fetching contract creators…", flush=True)
    creators = fetch_all_creators(all_tokens, api_key)
    missing = [t for t in all_tokens if t not in creators]
    if missing:
        print(f"WARN missing creators {len(missing)}", flush=True)

    rug_deployers: Dict[str, List[str]] = defaultdict(list)
    clean_deployers: Dict[str, List[str]] = defaultdict(list)
    for p in rugs:
        c = creators.get(p["token"].lower(), {}).get("creator")
        if c:
            rug_deployers[c].append(p["token"].lower())
    for p in cleans:
        c = creators.get(p["token"].lower(), {}).get("creator")
        if c:
            clean_deployers[c].append(p["token"].lower())

    overlap = set(rug_deployers) & set(clean_deployers)
    safe_deployers = sorted(set(rug_deployers) - overlap)
    unsafe_deployers = sorted(overlap)

    print(f"deployers rug={len(rug_deployers)} clean={len(clean_deployers)} "
          f"overlap={len(overlap)} safe={len(safe_deployers)}", flush=True)

    # Funding graph for all rug deployers (including overlap for analysis)
    funder_to_deployers: Dict[str, Set[str]] = defaultdict(set)
    deployer_funding: Dict[str, dict] = {}
    unique_rug_deployers = sorted(set(rug_deployers))
    print(f"fetching first ETH funder for {len(unique_rug_deployers)} rug deployers…", flush=True)
    for i, dep in enumerate(unique_rug_deployers):
        ff = first_eth_funder(dep, api_key)
        if ff:
            deployer_funding[dep] = ff
            funder_to_deployers[ff["funder"]].add(dep)
        if (i + 1) % 25 == 0:
            print(f"  funders {i + 1}/{len(unique_rug_deployers)}", flush=True)
        time.sleep(SLEEP)

    # Funding clusters: funder linked to 2+ rug deployers
    clusters = []
    for funder, deps in sorted(funder_to_deployers.items(), key=lambda x: -len(x[1])):
        if len(deps) < 2:
            continue
        safe_in_cluster = sorted(d for d in deps if d in safe_deployers)
        clusters.append({
            "funder": funder,
            "n_rug_deployers": len(deps),
            "n_safe_deployers": len(safe_in_cluster),
            "deployers": sorted(deps),
            "safe_deployers": safe_in_cluster,
        })

    graph = {
        "meta": {
            "source": "blockscout" if not api_key else "basescan_v2_or_blockscout",
            "n_rug_tokens": len(rug_tokens),
            "n_clean_tokens": len(clean_tokens),
            "n_rug_deployers": len(rug_deployers),
            "n_clean_deployers": len(clean_deployers),
            "n_overlap_deployers": len(overlap),
            "n_safe_deployers": len(safe_deployers),
            "n_funding_clusters_ge2": len(clusters),
            "n_deployers_with_funder": len(deployer_funding),
        },
        "token_creators": creators,
        "rug_deployers": {k: v for k, v in rug_deployers.items()},
        "clean_deployers": {k: v for k, v in clean_deployers.items()},
        "unsafe_deployers": {
            d: {"rug_tokens": rug_deployers[d], "clean_tokens": clean_deployers[d]}
            for d in unsafe_deployers
        },
        "deployer_funding": deployer_funding,
        "funding_clusters": clusters,
    }
    graph_path = OUT_DIR / "deployer_funding_graph.json"
    graph_path.write_text(json.dumps(graph, indent=2))
    print(f"wrote {graph_path}", flush=True)

    validated = {
        "safe_deployers": safe_deployers,
        "unsafe_deployers": unsafe_deployers,
        "n_safe": len(safe_deployers),
        "n_unsafe": len(unsafe_deployers),
        "n_clean_deployers_total": len(clean_deployers),
        "fp_validation": "rug_deployers minus clean_deployers in 644 labeled set",
        "coverage": {
            "rug_tokens_with_safe_deployer": sum(
                1 for p in rugs
                if creators.get(p["token"].lower(), {}).get("creator") in safe_deployers
            ),
            "rug_tokens_total": len(rugs),
        },
    }
    val_path = OUT_DIR / "deployer_blacklist_validated.json"
    val_path.write_text(json.dumps(validated, indent=2))
    print(f"wrote {val_path} safe={len(safe_deployers)}", flush=True)

    # Also copy to research dir for executor seed script consumption
    seed_path = ROOT / "scripts/research/base_lefttail/_deployer_seed_validated.json"
    seed_path.write_text(json.dumps(validated, indent=2))

    cov = validated["coverage"]["rug_tokens_with_safe_deployer"]
    print(f"rug token coverage via safe deployers: {cov}/{len(rugs)} "
          f"({100 * cov / len(rugs):.1f}%)", flush=True)
    print(f"funding clusters (≥2 rug deployers): {len(clusters)}", flush=True)
    if clusters[:3]:
        for c in clusters[:3]:
            print(f"  top cluster funder={c['funder'][:12]}… n={c['n_rug_deployers']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
