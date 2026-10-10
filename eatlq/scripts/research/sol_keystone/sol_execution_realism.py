#!/usr/bin/env python3
"""Task 5 — execution realism: offline vs executable edge (zero RPC)."""
from __future__ import annotations

from common import (
    COST_RT,
    ENTRY_S,
    OUT,
    TEST_DAYS,
    TRAIN_DAYS,
    load_grids,
    load_json,
    roundtrip_ret,
    summarize_rets,
    write_json,
)

# Solana solo-player friction model (conservative upper bound)
SANDWICH_BPS_PER_SIDE = 150       # 1.5% per side on thin bonding curve
PRIORITY_FEE_RT = 0.005            # ~0.5% round-trip priority + Jito tip amortized
DELAY_SLOTS = [0, 1, 2, 5]         # slot delay from signal; ~400ms/slot => use seconds proxy
SLOT_S = 0.4


def apply_execution(ret: float | None, delay_s: float, n_trades_180s: float) -> float | None:
    if ret is None:
        return None
    # deeper early activity => more sandwich competition
    depth_factor = min(1.5, 1.0 + max(0, n_trades_180s - 40) / 200)
    sandwich = 2 * (SANDWICH_BPS_PER_SIDE / 10000) * depth_factor
    delay_penalty = delay_s * 0.002  # 0.2% per second of signal decay on pump curve
    return ret - sandwich - PRIORITY_FEE_RT - delay_penalty


def main():
    raw_rc = load_json(OUT / "_sol_realcost_labels.json")
    raw_lt = load_json(OUT / "_sol_lefttail_model.json")
    raw_uni = load_json(OUT / "_sol_universe.json")
    uni = {r["mint"]: r for r in raw_uni["rows"]}
    labels = {x["mint"]: x for x in raw_rc["labels"]}
    pruned = {x["mint"] for x in raw_lt["predictions"] if x.get("keep_pruned")}
    grids = load_grids()

    offline, executable = [], []
    by_delay = {d: [] for d in DELAY_SLOTS}

    for mint in pruned:
        if mint not in labels:
            continue
        lab = labels[mint]
        r = lab["returns"].get("30m") or lab["returns"].get("5m")
        if r is None:
            continue
        n180 = float(uni.get(mint, {}).get("n_trades_180s") or 0)
        offline.append(r)
        # executable at delay=2 slots (~0.8s) as primary solo-player estimate
        ex = apply_execution(r, delay_s=2 * SLOT_S, n_trades_180s=n180)
        if ex is not None:
            executable.append(ex)
        for ds in DELAY_SLOTS:
            er = apply_execution(r, delay_s=ds * SLOT_S, n_trades_180s=n180)
            if er is not None and lab.get("day") in TEST_DAYS:
                by_delay[ds].append(er)

    off_sum = summarize_rets(offline)
    exe_sum = summarize_rets(executable)
    exec_discount = (off_sum.get("mean", 0) - exe_sum.get("mean", 0)) if off_sum.get("n") else None

    # signal decay: first delay where TEST mean crosses 0
    decay_slot = None
    for ds in DELAY_SLOTS:
        s = summarize_rets(by_delay[ds])
        if s.get("n", 0) >= 30 and s.get("mean", -1) <= 0:
            decay_slot = ds
            break

    split_stats = {}
    for name, days in [("TRAIN", TRAIN_DAYS), ("TEST", TEST_DAYS)]:
        o, e = [], []
        for mint in pruned:
            lab = labels.get(mint, {})
            if lab.get("day") not in days:
                continue
            r = lab["returns"].get("30m") or lab["returns"].get("5m")
            if r is None:
                continue
            n180 = float(uni.get(mint, {}).get("n_trades_180s") or 0)
            o.append(r)
            er = apply_execution(r, 2 * SLOT_S, n180)
            if er is not None:
                e.append(er)
        split_stats[name] = {
            "offline_edge": summarize_rets(o),
            "executable_edge": summarize_rets(e),
            "execution_discount": (
                summarize_rets(o).get("mean", 0) - summarize_rets(e).get("mean", 0)
                if summarize_rets(o).get("n") else None
            ),
        }

    out = {
        "model": {
            "sandwich_bps_per_side": SANDWICH_BPS_PER_SIDE,
            "priority_fee_rt": PRIORITY_FEE_RT,
            "delay_slots_tested": DELAY_SLOTS,
            "slot_seconds": SLOT_S,
            "primary_delay_slots": 2,
        },
        "pruned_basket_30m_hold": {
            "offline_edge": off_sum,
            "executable_edge": exe_sum,
            "execution_discount_mean": exec_discount,
            "signal_decay_to_zero_slot": decay_slot,
            "by_delay_TEST": {str(d): summarize_rets(by_delay[d]) for d in DELAY_SLOTS},
            "by_split": split_stats,
        },
        "baseline_cost_rt": COST_RT,
        "note": "executable = offline minus sandwich(1.5%/side scaled) minus priority/Jito minus delay decay",
        "rpc_calls": 0,
    }
    write_json("_sol_execution_realism.json", out)
    print(f"execution offline={off_sum.get('mean')} executable={exe_sum.get('mean')} discount={exec_discount}")


if __name__ == "__main__":
    main()
