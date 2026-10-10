#!/usr/bin/env python3
"""READ-ONLY: real QuoterV2 multi-size round-trip quotes at each pool's entry &
exit block, to model slippage saturation (more $ in -> worse net %). No state
change, no broadcast. Caches to _capacity_size.json keyed pool:size.
"""
from __future__ import annotations
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/opt/EATLQ/scripts/lib")
import keystone_live as K  # noqa
import gate_sweep as G  # noqa
from v3_quoterv2_struct import JsonRpcClient, WETH_BASE, quote_exact_input_single_struct  # noqa

ARCHIVE = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
ETH_USD = 3000.0
SIZES = [100, 300, 500, 1000, 2000, 3000, 5000, 10000]
CACHE = os.path.join(HERE, "_capacity_size.json")
_tl = threading.local()


def rpc():
    if not hasattr(_tl, "c"):
        _tl.c = JsonRpcClient(ARCHIVE, delay_s=0.0)
    return _tl.c


def q(tin, tout, amt, fee, blk):
    try:
        r = quote_exact_input_single_struct(rpc(), tin, tout, amt, fee, block_identifier=blk)
        return r["amount_out"] if r["ok"] else None
    except Exception:
        return None


def main():
    df = G.load_all()
    priced = json.load(open(K.PCACHE))
    cfg = (df.ns_pre >= 5) & (df.p <= 0.27) & (df.entry_weth >= 3) & df.tradeable
    sub = df[cfg].copy()
    meta = []
    for r in sub.itertuples(index=False):
        pr = priced.get(r.pool) or {}
        fee = pr.get("fee")
        exb = pr.get("exb")
        if fee in (100, 500, 3000, 10000) and exb:
            meta.append((r.pool, str(r.alt), int(r.entry_block), int(exb), int(fee)))
    print(f"config pools to quote: {len(meta)}")
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

    def work(m):
        pool, alt, eb, xb, fee = m
        out = {}
        for s in SIZES:
            key = f"{pool}:{s}"
            if key in cache:
                continue
            amt = int(s / ETH_USD * 1e18)
            buy = q(WETH_BASE, alt, amt, fee, eb)
            if not buy or buy <= 0:
                out[key] = {"fric": -1.0, "net": None, "why": "entry_fail"}
                continue
            back = q(alt, WETH_BASE, buy, fee, eb)
            fric = -1.0 if back is None else back / amt - 1.0
            sell = q(alt, WETH_BASE, buy, fee, xb)
            net = -1.0 if sell is None else sell / amt - 1.0
            out[key] = {"fric": fric, "net": net, "why": "ok"}
        return out

    done = 0
    with ThreadPoolExecutor(max_workers=20) as ex:
        for res in ex.map(work, meta):
            cache.update(res)
            done += 1
            if done % 40 == 0:
                json.dump(cache, open(CACHE, "w"))
                print(f"  {done}/{len(meta)}", flush=True)
    json.dump(cache, open(CACHE, "w"))
    print(f"done. cache size {len(cache)} ({len(cache)//len(SIZES)} pools x {len(SIZES)} sizes)")


if __name__ == "__main__":
    main()
