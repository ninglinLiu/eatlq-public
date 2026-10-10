#!/usr/bin/env python3
"""Is the $5k depth of the survivor pools a property, or an artifact of one block?

R1 measured every pool at a single snapshot block. If depth swings hard across
blocks then the capacity ceiling it reported is fiction. Here the same one-way
slippage is re-measured on a grid of historical blocks (archive RPC), so each
pool gets a distribution instead of a point.

Storage slot keys do not depend on block, so slot detection is resolved once and
reused across the whole grid.
"""
from __future__ import annotations

import json
import statistics as st
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

import depth_631 as D
import slippage_631 as SL
from slot_detect import SlotDetector

SIZE_USD = 5_000
BLOCKS_PER_12H = 21_600          # Base is a constant 2s per block
N_POINTS = 60
_lock = threading.Lock()
_prog = {"done": 0, "t0": time.time(), "calls": 0}


def block_grid(head: int, n: int = N_POINTS, step: int = BLOCKS_PER_12H) -> list[int]:
    return sorted(head - i * step for i in range(n))


def measure(pool_row: dict, slots: dict, router: str, dialect: str, key: int,
            block: int) -> dict:
    alt = Web3.to_checksum_address(pool_row["alt"])
    mid = SL.mid_raw(pool_row["pool"], block)
    if not mid:
        return {"block": block, "klass": "NO_MID", "slip_bps": None}
    big = 1 << 160
    ov = {D.WETH: {"stateDiff": {slots["weth"]["bal"]: D._w(big),
                                 slots["weth"]["allow"]: D._w(big)}},
          alt: {"stateDiff": {slots["alt"]["bal"]: D._w(big),
                              slots["alt"]["allow"]: D._w(big)}}}
    amt_in = int(SIZE_USD / D.ETH_USD * 1e18)
    r = D.aggregate3([(router, D.swap_call(dialect, D.WETH, alt, key, amt_in))], ov, block)
    got = D.amt_out(r[0]) if r else None
    if not got:
        return {"block": block, "klass": "BUY_REVERT", "slip_bps": None}
    weth_is_t0 = D.WETH.lower() < alt.lower()
    want = amt_in * mid if weth_is_t0 else amt_in / mid
    return {"block": block, "klass": "OK",
            "slip_bps": (1.0 - got / want) * 10000.0 if want > 0 else None}


def run_pool(row: dict, det: SlotDetector, grid: list[int], head: int) -> dict:
    alt = Web3.to_checksum_address(row["alt"])
    out = {"pool": row["pool"], "venue": row["venue"], "alt": row["alt"],
           "symbol": row.get("symbol", "?"), "is_major": row.get("is_major", "0"),
           "n_swaps": row["n_swaps"], "n_days": row["n_days"], "points": []}
    router = D.ROUTERS.get(row["factory"])
    if not router:
        out["fatal"] = "NO_ROUTER"
        return out
    ws = det.detect(D.WETH, D.MULTICALL3, router, head)
    als = det.detect(alt, D.MULTICALL3, router, head)
    if not (ws and als):
        out["fatal"] = "UNRESOLVED"
        return out
    slots = {"weth": ws, "alt": als}

    # Settle the dialect at head, where the pool certainly exists.
    chosen = None
    for d, key in D.dialects_for(row):
        if key < 0:
            continue
        probe = measure(row, slots, router, d, key, head)
        if probe["klass"] == "OK":
            chosen = (d, key)
            break
    if not chosen:
        out["fatal"] = "BUY_REVERT_AT_HEAD"
        return out
    d, key = chosen
    out["dialect"] = d
    for b in grid:
        # Per-point isolation: one throttled call must not discard the other 59.
        try:
            out["points"].append(measure(row, slots, router, d, key, b))
        except Exception as e:  # noqa: BLE001
            out["points"].append({"block": b, "klass": f"ERROR_{type(e).__name__}",
                                  "slip_bps": None})
    return out


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "out/stability_r2.json"
    tgt = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "out/stability_pools.json"))
    head = int(D.rpc("eth_blockNumber", [])["result"], 16) - 20
    grid = block_grid(head)
    span_d = (head - grid[0]) * 2 / 86400
    print(f"[stab] pools={len(tgt)} points={len(grid)} size=${SIZE_USD} "
          f"span={span_d:.1f}d blocks={grid[0]}..{head}", flush=True)

    det = SlotDetector(D.RPC_URL)
    res = []

    def work(r):
        try:
            got = run_pool(r, det, grid, head)
        except Exception as e:  # noqa: BLE001
            got = {"pool": r["pool"], "symbol": r.get("symbol", "?"),
                   "fatal": f"ERROR_{type(e).__name__}", "points": []}
        with _lock:
            res.append(got)
            _prog["done"] += 1
            if _prog["done"] % 10 == 0 or _prog["done"] == len(tgt):
                el = time.time() - _prog["t0"]
                print(f"[stab] {_prog['done']}/{len(tgt)} el={el/60:.1f}m "
                      f"eta={(el/_prog['done'])*(len(tgt)-_prog['done'])/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, tgt))

    json.dump({"size_usd": SIZE_USD, "head": head, "grid": grid, "span_days": span_d,
               "pools": res}, open(dest, "w"), indent=1)

    ok = [p for p in res if p["points"]]
    print(f"\n[stab] DONE pools={len(res)} measurable={len(ok)} "
          f"min={(time.time()-_prog['t0'])/60:.1f}")
    stable = 0
    for p in ok:
        v = [x["slip_bps"] for x in p["points"] if x["slip_bps"] is not None]
        if v and st.median(v) <= 30:
            stable += 1
    print(f"[stab] pools with median slippage <=30bps across the grid: {stable}")
