#!/usr/bin/env python3
"""
Q3 (zero RPC): does the wallet signal predict REALIZED P2 return, not just paper MFE?

Rebuild the smart-money set using REAL P2 execution outcome as the training target
(instead of mfe_72h>=5x), then OOS-test whether smart-touched pools have higher
realized P2 return. If not -> P3 is economically limited (confirms the wall).
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from statistics import mean, median

from righttail_label_substrate import fetch
from righttail_p2_execution_sim import simulate_pool

CSV = "/opt/EATLQ/mvp/diagnostics/righttail_labels.csv"
TXFROM = "/opt/EATLQ/mvp/diagnostics/p3_txfrom_cache.json"
POOLTX = "/opt/EATLQ/mvp/diagnostics/p3_pooltxs_cache.json"
TRAIN_Q = 0.60
MIN_ACTIVE = 3
ETH_USD = 1665.0
SIZE_USD = 10.0
GAS_USD = 0.05
WIN_RET = 1.0     # realized "win" = net >= 2x (ret>=1.0)


def f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def main():
    txfrom = json.load(open(TXFROM)); pooltx = json.load(open(POOLTX))
    rows = list(csv.DictReader(open(CSV)))
    for r in rows:
        r["birth_ts"] = int(datetime.fromisoformat(r["birth_utc"]).timestamp())
        r["active"] = int(r["early_active_min"])
    rows.sort(key=lambda r: r["birth_ts"])
    T = rows[int(len(rows) * TRAIN_Q)]["birth_ts"]

    def wallets(pool):
        return {txfrom.get(h) for h in pooltx.get(pool.lower(), []) if txfrom.get(h)}

    print("fetch forward paths + simulate every pool ...", flush=True)
    pools, meta = fetch(240, "2026-05-22", 72)
    obs = 30 * 60
    size_wei = (SIZE_USD / ETH_USD) * 1e18
    gas_wei = (GAS_USD / ETH_USD) * 1e18
    targets = [(2.0, 0.5), (5.0, 0.25)]

    realret = {}
    for r in rows:
        p = r["pool"].lower()
        if p not in pools:
            continue
        bl = sorted(pools[p], key=lambda x: x["m"])
        birth = meta[p]["birth"]
        res = simulate_pool(bl, birth, bool(meta[p]["weth_t1"]), meta[p]["fee_bps"],
                            birth + obs, size_wei, gas_wei, 90 * 60, targets, 0.25, 0.5)
        if res is not None:
            realret[p] = res[0]

    for r in rows:
        r["realret"] = realret.get(r["pool"].lower())
        r["paper_win"] = 1 if (f(r["mfe_72h"]) >= 5 and r["sellable_after_entry"] == "1") else 0
        r["real_win"] = 1 if (r["realret"] is not None and r["realret"] >= WIN_RET) else 0

    train = [r for r in rows if r["birth_ts"] < T and r["realret"] is not None]
    oos = [r for r in rows if r["birth_ts"] >= T and r["active"] >= MIN_ACTIVE and r["realret"] is not None]

    # build smart sets two ways: target=paper MFE vs target=REAL return
    def build_S(target_key, loser_pred, min_win=2):
        w_win = defaultdict(int); w_lose = defaultdict(int)
        for r in train:
            ws = wallets(r["pool"])
            if r[target_key]:
                for w in ws:
                    w_win[w] += 1
            elif loser_pred(r):
                for w in ws:
                    w_lose[w] += 1
        return {w for w, c in w_win.items() if c >= min_win and w_lose.get(w, 0) == 0}

    S_paper = build_S("paper_win", lambda r: r["realret"] is not None and r["realret"] <= -0.5)
    S_real = build_S("real_win", lambda r: r["realret"] is not None and r["realret"] <= -0.5)

    def oos_eval(S):
        tch = [r for r in oos if wallets(r["pool"]) & S]
        un = [r for r in oos if not (wallets(r["pool"]) & S)]
        base_real = mean([r["realret"] for r in oos])
        return {
            "smart_set": len(S), "touched": len(tch),
            "mean_realret_touched": round(mean([r["realret"] for r in tch]), 4) if tch else None,
            "mean_realret_untouched": round(mean([r["realret"] for r in un]), 4) if un else None,
            "mean_realret_base": round(base_real, 4),
            "P(real_win|touched)": round(mean([r["real_win"] for r in tch]), 4) if tch else None,
            "P(real_win|base)": round(mean([r["real_win"] for r in oos]), 4),
            "P(paper_win|touched)": round(mean([r["paper_win"] for r in tch]), 4) if tch else None,
            "P(paper_win|base)": round(mean([r["paper_win"] for r in oos]), 4),
        }

    out = {
        "split_T_utc": datetime.utcfromtimestamp(T).isoformat(),
        "train_n": len(train), "train_real_wins(>=2x net)": sum(r["real_win"] for r in train),
        "train_paper_wins": sum(r["paper_win"] for r in train),
        "oos_n": len(oos), "oos_real_wins": sum(r["real_win"] for r in oos),
        "S_target=PAPER_MFE": oos_eval(S_paper),
        "S_target=REAL_P2_return": oos_eval(S_real),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
