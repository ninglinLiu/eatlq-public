#!/usr/bin/env python3
"""READ-ONLY: measure the RAW fast-rug rate over the creation-complete set of
WETH pools (from factory PoolCreated, May/June 2026), bypassing MIN_PRE / p_fatal
and the swap-capture subscription bias.

For each created pool we read WETH.balanceOf(pool) at the archive across offsets
from the creation block. A pool that has liquidity then loses it within <=30
blocks of first having it is a fast LP-pull rug. No anvil, no broadcast.
"""
from __future__ import annotations
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ARCHIVE = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
WETH = "0x4200000000000000000000000000000000000006"
THR = int(0.05 * 1e18)     # "has liquidity" threshold
GONE = int(0.02 * 1e18)    # "liquidity removed" threshold
OFFSETS = [0, 2, 5, 10, 20, 30, 60, 150, 450, 1800, 7200]
HERE = os.path.dirname(os.path.abspath(__file__))


def weth_bal(pool: str, block: int) -> int | None:
    data = "0x70a08231" + "0" * 24 + pool[2:].lower()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": WETH, "data": data}, hex(block)]}
    for _ in range(3):
        try:
            r = requests.post(ARCHIVE, json=payload, timeout=20)
            res = r.json().get("result")
            if res and res != "0x":
                return int(res, 16)
            if res == "0x":
                return 0
        except Exception:
            time.sleep(0.5)
    return None


def classify(pool: str, cblk: int) -> dict:
    bals = {}
    for off in OFFSETS:
        bals[off] = weth_bal(pool, cblk + off)
    # first offset with liquidity
    first_lp = next((o for o in OFFSETS if bals[o] and bals[o] >= THR), None)
    if first_lp is None:
        return {"label": "no_lp", "bals": bals}
    maxb = max(b for b in bals.values() if b is not None)
    # first offset AFTER first_lp where liquidity is gone
    rug_off = next((o for o in OFFSETS if o > first_lp and bals[o] is not None
                    and bals[o] < GONE and bals[o] < 0.1 * maxb), None)
    if rug_off is None:
        return {"label": "survived", "first_lp": first_lp, "bals": bals}
    gap = rug_off - first_lp
    return {"label": "fast_rug" if gap <= 30 else "rug", "first_lp": first_lp,
            "rug_off": rug_off, "gap_blocks": gap, "bals": bals}


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
    rows = [json.loads(l) for l in open("/tmp/created_pools.jsonl")][:limit]
    out = []

    def work(row):
        c = classify(row["pool"], int(row["cblk"]))
        return {"pool": row["pool"], "cblk": int(row["cblk"]), "m": row["m"],
                "ns_captured": int(row["ns_captured"]), "buyers_captured": int(row["buyers_captured"]),
                **{k: v for k, v in c.items() if k != "bals"}}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=24) as ex:
        for i, rec in enumerate(ex.map(work, rows)):
            out.append(rec)
            if (i + 1) % 100 == 0:
                print(f"  {i+1}/{len(rows)} ({time.time()-t0:.0f}s)", flush=True)
    json.dump(out, open(os.path.join(HERE, "_raw_fastrug.json"), "w"), indent=1)
    print(f"done {len(out)} pools in {time.time()-t0:.0f}s -> _raw_fastrug.json")


if __name__ == "__main__":
    main()
