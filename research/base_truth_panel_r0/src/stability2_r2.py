#!/usr/bin/env python3
"""Two-sided slippage across a 30-day block grid — the final gate.

Combines the two corrections this round produced: exits must be priced (a
one-sided pool passes any buy-only test) and depth must hold across blocks (a
single snapshot can be off by 15x on unstable pools). A pool only counts if its
worse direction stays under the ceiling at most points in the window.
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
from slippage_631 import mid_raw
from slot_detect import SlotDetector

SIZE_USD = 5_000
BLOCKS_PER_12H = 21_600
N_POINTS = 60
_lock = threading.Lock()
_prog = {"done": 0, "t0": time.time()}


def point(row: dict, slots: dict, router: str, d: str, key: int, block: int) -> dict:
    alt = Web3.to_checksum_address(row["alt"])
    mid = mid_raw(row["pool"], block)
    if not mid:
        return {"block": block, "klass": "NO_MID", "buy_bps": None, "sell_bps": None}
    big = 1 << 160
    ov = {D.WETH: {"stateDiff": {slots["weth"]["bal"]: D._w(big),
                                 slots["weth"]["allow"]: D._w(big)}},
          alt: {"stateDiff": {slots["alt"]["bal"]: D._w(big),
                              slots["alt"]["allow"]: D._w(big)}}}
    weth_is_t0 = D.WETH.lower() < alt.lower()
    weth_in = int(SIZE_USD / D.ETH_USD * 1e18)
    alt_at_mid = weth_in * mid if weth_is_t0 else weth_in / mid

    rb = D.aggregate3([(router, D.swap_call(d, D.WETH, alt, key, weth_in))], ov, block)
    got = D.amt_out(rb[0]) if rb else None
    buy = (1.0 - got / alt_at_mid) * 10000.0 if got and alt_at_mid > 0 else None

    alt_in = int(alt_at_mid)
    rs = D.aggregate3([(router, D.swap_call(d, alt, D.WETH, key, alt_in))], ov, block)
    back = D.amt_out(rs[0]) if rs else None
    weth_at_mid = alt_in / mid if weth_is_t0 else alt_in * mid
    sell = (1.0 - back / weth_at_mid) * 10000.0 if back and weth_at_mid > 0 else None

    return {"block": block, "buy_bps": buy, "sell_bps": sell,
            "klass": "OK" if buy is not None and sell is not None else "PARTIAL",
            "worst_bps": max([x for x in (buy, sell) if x is not None], default=None)}


def run_pool(row: dict, det: SlotDetector, grid: list[int], head: int) -> dict:
    alt = Web3.to_checksum_address(row["alt"])
    out = {k: row[k] for k in ("pool", "venue", "alt", "symbol", "is_major", "n_swaps", "n_days")}
    out["points"] = []
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
    chosen = None
    for d, key in D.dialects_for(row):
        if key < 0:
            continue
        if point(row, slots, router, d, key, head)["buy_bps"] is not None:
            chosen = (d, key)
            break
    if not chosen:
        out["fatal"] = "BUY_REVERT_AT_HEAD"
        return out
    d, key = chosen
    out["dialect"] = d
    for b in grid:
        try:
            out["points"].append(point(row, slots, router, d, key, b))
        except Exception as e:  # noqa: BLE001
            out["points"].append({"block": b, "klass": f"ERROR_{type(e).__name__}",
                                  "buy_bps": None, "sell_bps": None, "worst_bps": None})
    return out


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "out/stability2_r2.json"
    tgt = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "out/stability_pools.json"))
    head = int(D.rpc("eth_blockNumber", [])["result"], 16) - 20
    grid = sorted(head - i * BLOCKS_PER_12H for i in range(N_POINTS))
    print(f"[s2stab] pools={len(tgt)} points={len(grid)} size=${SIZE_USD} "
          f"span={(head-grid[0])*2/86400:.1f}d both_directions", flush=True)

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
                print(f"[s2stab] {_prog['done']}/{len(tgt)} el={el/60:.1f}m "
                      f"eta={(el/_prog['done'])*(len(tgt)-_prog['done'])/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, tgt))

    json.dump({"size_usd": SIZE_USD, "head": head, "grid": grid,
               "span_days": (head - grid[0]) * 2 / 86400, "pools": res},
              open(dest, "w"), indent=1)

    hard = 0
    for p in res:
        v = [x["worst_bps"] for x in p["points"] if x.get("worst_bps") is not None]
        if v and len(v) >= 0.9 * len(grid) and st.median(v) <= 30:
            hard += 1
    print(f"\n[s2stab] DONE pools={len(res)} min={(time.time()-_prog['t0'])/60:.1f}")
    print(f"[s2stab] median worst-direction <=30bps with >=90% coverage: {hard}")
