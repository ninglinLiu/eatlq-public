#!/usr/bin/env python3
"""One-way execution slippage vs pool mid — the depth metric R1's round trip was not.

An atomic round trip through a single pool retraces the same liquidity curve, so
price impact cancels by construction and the result is always ~2x fee no matter
how thin the pool is. That measures the fee tier, not depth. Here we compare the
buy's executed rate against the pool's own pre-trade mid from slot0, which is
what actually grows with size.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

import depth_631 as D
from slot_detect import SlotDetector

_SLOT0 = Web3.keccak(text="slot0()")[:4]
WETH = D.WETH
SIZES_USD = [50, 500, 5_000, 25_000, 100_000]
_lock = threading.Lock()
_prog = {"done": 0, "t0": time.time()}


def mid_raw(pool: str, block) -> float | None:
    """token1_raw per token0_raw, from sqrtPriceX96."""
    r = D.rpc("eth_call", [{"to": Web3.to_checksum_address(pool), "data": "0x" + _SLOT0.hex()},
                           hex(int(block))])
    res = r.get("result") or ""
    if len(res) < 66:
        return None
    sqrt_p = int(res[2:66], 16)
    if not sqrt_p:
        return None
    return (sqrt_p / 2**96) ** 2


def probe(row: dict, det: SlotDetector, block) -> list[dict]:
    alt = Web3.to_checksum_address(row["alt"])
    base = {k: row[k] for k in ("pool", "venue", "factory", "alt", "n_swaps", "n_days")}
    fail = lambda k: [{**base, "usd": u, "klass": k, "slip_bps": None} for u in SIZES_USD]  # noqa: E731

    router = D.ROUTERS.get(row["factory"])
    if not router:
        return fail("NO_ROUTER")
    ws = det.detect(WETH, D.MULTICALL3, router, block)
    als = det.detect(alt, D.MULTICALL3, router, block)
    if not (ws and als):
        return fail("UNRESOLVED_" + det.reason.get(alt, "SLOT_NOT_CONFIRMED"))
    mid = mid_raw(row["pool"], block)
    if not mid:
        return fail("NO_MID")

    weth_is_t0 = WETH.lower() < alt.lower()
    big = 1 << 160
    ov = {WETH: {"stateDiff": {ws["bal"]: D._w(big), ws["allow"]: D._w(big)}},
          alt: {"stateDiff": {als["bal"]: D._w(big), als["allow"]: D._w(big)}}}

    known = D._dialect_cache.get(row["factory"])
    tries = [(known, D.key_for(row, known))] if known else D.dialects_for(row)
    probe_amt = int(SIZES_USD[0] / D.ETH_USD * 1e18)
    for d, key in tries:
        if key < 0:
            continue
        r = D.aggregate3([(router, D.swap_call(d, WETH, alt, key, probe_amt))], ov, block)
        if r and D.amt_out(r[0]):
            D._dialect_cache.setdefault(row["factory"], d)
            break
    else:
        return fail("BUY_REVERT")

    out = []
    for usd in SIZES_USD:
        amt_in = int(usd / D.ETH_USD * 1e18)
        r = D.aggregate3([(router, D.swap_call(d, WETH, alt, key, amt_in))], ov, block)
        got = D.amt_out(r[0]) if r else None
        if not got:
            out.append({**base, "usd": usd, "klass": "BUY_REVERT", "slip_bps": None})
            continue
        want = amt_in * mid if weth_is_t0 else amt_in / mid
        out.append({**base, "usd": usd, "klass": "OK", "alt_out": got,
                    "slip_bps": (1.0 - got / want) * 10000.0 if want > 0 else None})
    return out


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "out/targets_631.tsv"
    dest = sys.argv[2] if len(sys.argv) > 2 else "out/slippage_631.json"
    hdr, *lines = open(src).read().strip().split("\n")
    cols = hdr.split("\t")
    rows = [dict(zip(cols, ln.split("\t"))) for ln in lines]
    block = int(D.rpc("eth_blockNumber", [])["result"], 16) - 20
    print(f"[slip] pools={len(rows)} block={block} sizes={SIZES_USD}", flush=True)

    det = SlotDetector(D.RPC_URL)
    res: list[dict] = []

    def work(r):
        try:
            got = probe(r, det, block)
        except Exception as e:  # noqa: BLE001
            got = [{**{k: r[k] for k in ("pool", "venue", "factory", "alt", "n_swaps", "n_days")},
                    "usd": u, "klass": f"ERROR_{type(e).__name__}", "slip_bps": None}
                   for u in SIZES_USD]
        with _lock:
            res.extend(got)
            _prog["done"] += 1
            if _prog["done"] % 50 == 0 or _prog["done"] == len(rows):
                el = time.time() - _prog["t0"]
                print(f"[slip] {_prog['done']}/{len(rows)} el={el/60:.1f}m "
                      f"eta={(el/_prog['done'])*(len(rows)-_prog['done'])/60:.1f}m", flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, rows))

    json.dump({"block": block, "sizes_usd": SIZES_USD, "eth_usd": D.ETH_USD, "rows": res},
              open(dest, "w"), indent=1)
    print(f"[slip] DONE rows={len(res)} min={(time.time()-_prog['t0'])/60:.1f}", flush=True)
