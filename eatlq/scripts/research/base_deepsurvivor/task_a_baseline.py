#!/usr/bin/env python3
"""Task A: Random deep-pool baseline — fixed hold windows, real QuoterV2 PnL."""
from __future__ import annotations

import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (  # noqa: E402
    HERE,
    HOLD_BLOCKS,
    BudgetRpc,
    load_json,
    pnl_stats,
    rpc_ledger,
    save_json,
    time_split,
)

UNIVERSE = os.path.join(HERE, "_deep_universe.json")
OUT = os.path.join(HERE, "_deep_baseline.json")
PRICE_CACHE = os.path.join(HERE, "_baseline_pricing.json")
CHECKPOINT = os.path.join(HERE, "_baseline_checkpoint.json")
MAX_PRICE_RPC = 500  # subset pricing within round budget


def price_universe(rows: list, rpc: BudgetRpc, head: int) -> dict:
    cache = load_json(PRICE_CACHE, {})
    train, test, _ = time_split(rows)
    # Stratified sample: ensure TEST gets priced pools (min 40 each split)
    per_split = MAX_PRICE_RPC // (2 * len(HOLD_BLOCKS))
    random.seed(42)
    tr_sample = train if len(train) <= per_split else random.sample(train, per_split)
    te_sample = test if len(test) <= per_split else random.sample(test, per_split)
    todo = tr_sample + te_sample
    todo = [r for r in todo if f"{r['pool']}:{r['entry_block']}" not in cache]
    print(f"  pricing {len(todo)} pools x {len(HOLD_BLOCKS)} holds (cached={len(cache)})", flush=True)
    for i, r in enumerate(todo):
        pool, alt, eb = r["pool"], r["alt"], r["entry_block"]
        fee = rpc.pool_fee(pool, eb)
        key = f"{pool}:{eb}"
        rec = {"fee": fee, "holds": {}}
        if fee not in (100, 500, 3000, 10000):
            rec["why"] = "bad_fee"
            cache[key] = rec
            continue
        for hold_name, hold_blk in HOLD_BLOCKS.items():
            if rpc.budget_remaining() <= 0:
                break
            xb = min(eb + hold_blk, head)
            try:
                rt = rpc.roundtrip(pool, alt, fee, eb, xb)
                rec["holds"][hold_name] = rt
            except Exception:
                rec["holds"][hold_name] = {"net": None, "why": "rpc_stop"}
        rec["why"] = "ok"
        cache[key] = rec
        if (i + 1) % 25 == 0:
            save_json(PRICE_CACHE, cache)
            print(f"    {i+1}/{len(todo)} rpc_added={rpc.session_added()}", flush=True)
    save_json(PRICE_CACHE, cache)
    return cache


def baseline_for_split(split_rows: list, cache: dict, hold: str) -> dict:
    nets = []
    for r in split_rows:
        key = f"{r['pool']}:{r['entry_block']}"
        rec = cache.get(key, {})
        if rec.get("why") != "ok":
            continue
        h = rec.get("holds", {}).get(hold, {})
        if h.get("net") is not None:
            nets.append(h["net"])
    return pnl_stats(nets)


def main():
    print("#### Task A: Deep pool random baseline ####", flush=True)
    ckpt = load_json(CHECKPOINT, {})
    if ckpt.get("done") and os.path.exists(OUT):
        b = load_json(OUT)
        print(f"  checkpoint done: {b['conclusion']}")
        return

    uni = load_json(UNIVERSE)
    rows = uni.get("rows", [])
    if not rows:
        print("  ERROR: empty universe — run task0 first")
        sys.exit(1)

    train, test, cut = time_split(rows)
    print(f"  universe={len(rows)} TRAIN={len(train)} TEST={len(test)} cut_ts={cut}", flush=True)

    rpc = BudgetRpc()
    head = rpc.block_number()
    cache = price_universe(rows, rpc, head)

    results = {"holds": {}, "train": {}, "test": {}, "random_sample_n": len(cache)}
    for hold in HOLD_BLOCKS:
        tr = baseline_for_split(train, cache, hold)
        te = baseline_for_split(test, cache, hold)
        results["holds"][hold] = {"train": tr, "test": te}

    # Primary hold = 4h (matches KEYSTONE convention)
    primary = results["holds"].get("4h", {})
    te4 = primary.get("test", {})
    tr4 = primary.get("train", {})

    if te4.get("n", 0) >= 30:
        ev = "positive" if te4["mean"] > 0 else "negative"
        tail = f"right_50={te4.get('right_50',0):.1%} fatal_50={te4.get('fatal_50',0):.1%}"
        conclusion = f"Deep pool random 4h hold TEST is {ev} EV (mean={te4['mean']:+.3f}, WR={te4['wr']:.1%}). {tail}"
    else:
        conclusion = f"Insufficient TEST n={te4.get('n',0)} for 4h baseline conclusion (need >=30)"

    results["meta"] = {
        "train_cut_ts": cut,
        "n_universe": len(rows),
        "n_train": len(train),
        "n_test": len(test),
        "rpc": rpc_ledger(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    results["conclusion"] = conclusion
    results["conclusion_lines"] = [
        f"1. Deep pool random entry 4h TEST: mean={te4.get('mean','NA')} WR={te4.get('wr','NA')} n={te4.get('n',0)}",
        f"2. Right tail (>=+50%): {te4.get('right_50','NA')}  fatal (<=-50%): {te4.get('fatal_50','NA')}",
        f"3. TRAIN vs TEST gap 4h: mean {tr4.get('mean','NA')} vs {te4.get('mean','NA')}",
    ]
    save_json(OUT, results)
    save_json(CHECKPOINT, {"done": True})
    print(f"  {conclusion}")
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
