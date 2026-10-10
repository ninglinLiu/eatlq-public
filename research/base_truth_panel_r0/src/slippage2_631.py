#!/usr/bin/env python3
"""Two-sided one-way slippage: entry AND exit, each against pool mid.

R1/R2 measured only the buy leg. That silently passes one-sided pools: a pool
holding almost no WETH quotes a ~fee buy (it receives WETH, pays out abundant
alt) while the exit is a total loss. BGPT and QWLA both looked like 1.2 bps
depth on the buy and turn out to be 10,000 bps on the sell.

Exit is measured by granting the holder the alt-equivalent of the same notional
and selling it, so both legs price the same dollar amount at the same block.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

import depth_631 as D
from slippage_631 import mid_raw
from slot_detect import SlotDetector

SIZES_USD = [50, 500, 5_000, 25_000]
_lock = threading.Lock()
_prog = {"done": 0, "t0": time.time()}


def probe(row: dict, det: SlotDetector, block) -> list[dict]:
    alt = Web3.to_checksum_address(row["alt"])
    base = {k: row[k] for k in ("pool", "venue", "factory", "alt", "n_swaps", "n_days")}
    fail = lambda k: [{**base, "usd": u, "klass": k, "buy_bps": None, "sell_bps": None}  # noqa: E731
                      for u in SIZES_USD]

    router = D.ROUTERS.get(row["factory"])
    if not router:
        return fail("NO_ROUTER")
    ws = det.detect(D.WETH, D.MULTICALL3, router, block)
    als = det.detect(alt, D.MULTICALL3, router, block)
    if not (ws and als):
        return fail("UNRESOLVED_" + det.reason.get(alt, "SLOT_NOT_CONFIRMED"))
    mid = mid_raw(row["pool"], block)
    if not mid:
        return fail("NO_MID")

    weth_is_t0 = D.WETH.lower() < alt.lower()
    big = 1 << 160
    ov = {D.WETH: {"stateDiff": {ws["bal"]: D._w(big), ws["allow"]: D._w(big)}},
          alt: {"stateDiff": {als["bal"]: D._w(big), als["allow"]: D._w(big)}}}

    known = D._dialect_cache.get(row["factory"])
    tries = [(known, D.key_for(row, known))] if known else D.dialects_for(row)
    for d, key in tries:
        if key < 0:
            continue
        r = D.aggregate3([(router, D.swap_call(d, D.WETH, alt, key, 10**16))], ov, block)
        if r and D.amt_out(r[0]):
            D._dialect_cache.setdefault(row["factory"], d)
            break
    else:
        return fail("BUY_REVERT")

    out = []
    for usd in SIZES_USD:
        weth_in = int(usd / D.ETH_USD * 1e18)
        alt_at_mid = weth_in * mid if weth_is_t0 else weth_in / mid

        rb = D.aggregate3([(router, D.swap_call(d, D.WETH, alt, key, weth_in))], ov, block)
        got = D.amt_out(rb[0]) if rb else None
        buy = (1.0 - got / alt_at_mid) * 10000.0 if got and alt_at_mid > 0 else None

        alt_in = int(alt_at_mid)
        rs = D.aggregate3([(router, D.swap_call(d, alt, D.WETH, key, alt_in))], ov, block)
        back = D.amt_out(rs[0]) if rs else None
        weth_at_mid = alt_in / mid if weth_is_t0 else alt_in * mid
        sell = (1.0 - back / weth_at_mid) * 10000.0 if back and weth_at_mid > 0 else None

        klass = ("OK" if buy is not None and sell is not None else
                 "SELL_REVERT" if buy is not None else
                 "BUY_REVERT" if sell is not None else "BOTH_REVERT")
        out.append({**base, "usd": usd, "klass": klass, "buy_bps": buy, "sell_bps": sell,
                    "worst_bps": max(x for x in (buy, sell) if x is not None)
                    if (buy is not None or sell is not None) else None})
    return out


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "out/targets_631.tsv"
    dest = sys.argv[2] if len(sys.argv) > 2 else "out/slippage2_631.json"
    hdr, *lines = open(src).read().strip().split("\n")
    cols = hdr.split("\t")
    rows = [dict(zip(cols, ln.split("\t"))) for ln in lines]
    block = int(D.rpc("eth_blockNumber", [])["result"], 16) - 20
    print(f"[s2] pools={len(rows)} block={block} sizes={SIZES_USD} both_directions", flush=True)

    det = SlotDetector(D.RPC_URL)
    res: list[dict] = []

    def work(r):
        try:
            got = probe(r, det, block)
        except Exception as e:  # noqa: BLE001
            got = [{**{k: r[k] for k in ("pool", "venue", "factory", "alt", "n_swaps", "n_days")},
                    "usd": u, "klass": f"ERROR_{type(e).__name__}",
                    "buy_bps": None, "sell_bps": None} for u in SIZES_USD]
        with _lock:
            res.extend(got)
            _prog["done"] += 1
            if _prog["done"] % 50 == 0 or _prog["done"] == len(rows):
                el = time.time() - _prog["t0"]
                print(f"[s2] {_prog['done']}/{len(rows)} el={el/60:.1f}m "
                      f"eta={(el/_prog['done'])*(len(rows)-_prog['done'])/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, rows))

    json.dump({"block": block, "sizes_usd": SIZES_USD, "eth_usd": D.ETH_USD, "rows": res},
              open(dest, "w"), indent=1)
    print(f"[s2] DONE rows={len(res)} min={(time.time()-_prog['t0'])/60:.1f}", flush=True)
