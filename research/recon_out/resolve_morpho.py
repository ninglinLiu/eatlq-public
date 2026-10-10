#!/usr/bin/env python3
"""Resolve Morpho Blue market ids -> MarketParams via eth_call idToMarketParams(bytes32).

Read-only eth_call. One call per distinct market id.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

sys.path.insert(0, "/root/recon_out")
from probe_rpc import CALLS, rpc  # noqa: E402

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
SEL = "0x2c3c9157"  # idToMarketParams(bytes32)

A = json.load(open("/root/recon_out/assets_raw.json"))
mids = A["mids"]

out = {}
for mid, cnt in sorted(mids.items(), key=lambda x: -x[1]):
    data = SEL + mid[2:]
    r = rpc("eth_call", [{"to": MORPHO, "data": data}, "latest"])
    if "error" in r or not r.get("result") or r["result"] == "0x":
        out[mid] = {"count": cnt, "error": r.get("error", {}).get("message", "empty")}
        print(f"{mid} ERR")
        continue
    w = r["result"][2:]
    words = [w[i * 64:(i + 1) * 64] for i in range(5)]
    out[mid] = {
        "count": cnt,
        "loanToken": "0x" + words[0][-40:],
        "collateralToken": "0x" + words[1][-40:],
        "oracle": "0x" + words[2][-40:],
        "irm": "0x" + words[3][-40:],
        "lltv": int(words[4], 16),
    }
    print(f"{mid} n={cnt:<5} coll={out[mid]['collateralToken']} "
          f"loan={out[mid]['loanToken']} lltv={out[mid]['lltv']/1e16:.1f}%")

json.dump(out, open("/root/recon_out/morpho_markets.json", "w"), indent=1)
coll = Counter()
for v in out.values():
    if "collateralToken" in v:
        coll[v["collateralToken"]] += v["count"]
print("\n=== Morpho collateral token weighted by liquidation count ===")
for a, c in coll.most_common():
    print(f"  {a}  {c}")
print("rpc_calls:", CALLS)
