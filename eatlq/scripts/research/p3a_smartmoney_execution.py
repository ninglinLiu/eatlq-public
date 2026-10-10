#!/usr/bin/env python3
"""
P3-A (zero RPC): does CONCENTRATING the P2 execution sim on smart-money-touched
OOS pools finally produce +EV vs spraying the whole OOS universe?

Uses cached wallet data (p3_txfrom_cache, p3_pooltxs_cache). Smart set built from
TRAIN winners only (born < T); execution simulated on OOS pools (born >= T).
Real slippage, runner, honeypot=total loss, no sample dropping (same P2 model).
Small sample -> indicative only.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime

from righttail_label_substrate import fetch
from righttail_p2_execution_sim import simulate_pool, portfolio_stats

CSV = "/opt/EATLQ/mvp/diagnostics/righttail_labels.csv"
TXFROM = "/opt/EATLQ/mvp/diagnostics/p3_txfrom_cache.json"
POOLTX = "/opt/EATLQ/mvp/diagnostics/p3_pooltxs_cache.json"
TRAIN_Q = 0.60
MIN_ACTIVE = 3
ETH_USD = 1665.0
SIZE_USD = 10.0
GAS_USD = 0.05


def f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def main():
    txfrom = json.load(open(TXFROM))
    pooltx = json.load(open(POOLTX))
    rows = list(csv.DictReader(open(CSV)))
    for r in rows:
        r["birth_ts"] = int(datetime.fromisoformat(r["birth_utc"]).timestamp())
        r["is_winner"] = 1 if (f(r["mfe_72h"]) >= 5 and r["sellable_after_entry"] == "1") else 0
        r["is_death"] = 1 if (r["dead_24h"] == "1" and r["sellable_after_entry"] == "0") else 0
        r["active"] = int(r["early_active_min"])
    rows.sort(key=lambda r: r["birth_ts"])
    T = rows[int(len(rows) * TRAIN_Q)]["birth_ts"]

    def wallets(pool):
        return {txfrom.get(h) for h in pooltx.get(pool.lower(), []) if txfrom.get(h)}

    tr_win = [r for r in rows if r["birth_ts"] < T and r["is_winner"]]
    tr_die = [r for r in rows if r["birth_ts"] < T and r["is_death"]]
    w_in_win = defaultdict(int); w_in_die = defaultdict(int)
    for r in tr_win:
        for w in wallets(r["pool"]):
            w_in_win[w] += 1
    for r in tr_die:
        for w in wallets(r["pool"]):
            w_in_die[w] += 1

    oos_rows = [r for r in rows if r["birth_ts"] >= T and r["active"] >= MIN_ACTIVE]
    oos_pools = {r["pool"].lower() for r in oos_rows}

    # forward paths
    print("fetch forward paths ...", flush=True)
    pools, meta = fetch(240, "2026-05-22", 72)
    obs = 30 * 60
    size_wei = (SIZE_USD / ETH_USD) * 1e18
    gas_wei = (GAS_USD / ETH_USD) * 1e18
    gap_sec = 90 * 60
    targets = [(2.0, 0.5), (5.0, 0.25)]

    def run_subset(pool_set):
        recs = []
        for p in pool_set:
            if p not in pools:
                continue
            bl = sorted(pools[p], key=lambda x: x["m"])
            birth = meta[p]["birth"]; dp = birth + obs
            r = simulate_pool(bl, birth, bool(meta[p]["weth_t1"]), meta[p]["fee_bps"],
                              dp, size_wei, gas_wei, gap_sec, targets, 0.25, 0.5)
            if r is not None:
                recs.append(r)
        return portfolio_stats(recs)

    out = {"split_T_utc": datetime.utcfromtimestamp(T).isoformat(),
           "size_usd": SIZE_USD, "results": {}}

    # baseline: spray whole OOS tradeable universe
    out["results"]["OOS_all_spray"] = run_subset(oos_pools)

    # smart-touched subsets for several S definitions (a priori, report all)
    for mw, dt in ((1, 0), (1, 1), (2, 0), (2, 1)):
        S = {w for w, c in w_in_win.items() if c >= mw and w_in_die.get(w, 0) <= dt}
        touched = {r["pool"].lower() for r in oos_rows if wallets(r["pool"]) & S}
        out["results"][f"OOS_smart_min_win{mw}_death_tol{dt}"] = {
            "smart_set": len(S), "touched_pools": len(touched),
            **run_subset(touched),
        }

    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
