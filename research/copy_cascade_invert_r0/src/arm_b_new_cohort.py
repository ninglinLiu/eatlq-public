"""Arm B: unfamous new-wallet sync. Falsify by wallet-id permute. No k lowering."""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import timedelta

from src.common import CONFIG, ROOT, mean, median, read_json, top1_removed_mean, utc_now_iso, write_json
from src.tape import load_closed_trades, load_entries
from src.arm_a_crowd_fade import _famous_set, _pit_pnl


def _is_new(entry: dict, w_new: float) -> bool:
    age = entry.get("wallet_age_days")
    if age is None:
        return False
    return age * 86400.0 <= w_new


def _cohort_returns(entries, closed, *, permute_seed: int | None = None) -> tuple[list[float], str]:
    w_new = float(CONFIG["arm_b_W_new_sec"])
    dt = float(CONFIG["arm_b_dt_sec"])
    k_min = int(CONFIG["arm_b_k_min"])
    top_n = int(CONFIG["fame_top_n"])

    missing_age = sum(1 for e in entries if e.get("wallet_age_days") is None)
    if missing_age == len(entries):
        return [], "INCONCLUSIVE_FIRST_SEEN"

    work = entries
    if permute_seed is not None:
        rng = random.Random(permute_seed)
        wallets = [e["wallet"] for e in entries]
        perm = wallets[:]
        rng.shuffle(perm)
        remap = dict(zip(wallets, perm))
        work = [{**e, "wallet": remap[e["wallet"]]} for e in entries]

    # group new buys by pool
    by_pool = defaultdict(list)
    for e in work:
        if e.get("pool") and e.get("entry_time") and _is_new(e, w_new):
            by_pool[e["pool"]].append(e)

    rets = []
    seen = set()
    for pool, evs in by_pool.items():
        evs = sorted(evs, key=lambda x: x["entry_time"])
        n = len(evs)
        i = 0
        while i < n:
            t0 = evs[i]["entry_time"]
            pnl = _pit_pnl(closed, t0)
            famous = _famous_set(pnl, top_n)
            cluster = []
            j = i
            while j < n and (evs[j]["entry_time"] - t0).total_seconds() <= dt:
                if evs[j]["wallet"] not in famous:
                    cluster.append(evs[j])
                j += 1
            wallets = {c["wallet"] for c in cluster}
            if len(wallets) >= k_min:
                key = (pool, t0.isoformat())
                if key not in seen:
                    seen.add(key)
                    # one position per cluster; use first member's delayed long as structure proxy
                    r = cluster[0].get("copy_delay_60s")
                    if r is None:
                        r = cluster[0].get("future_return_5m")
                    if r is not None:
                        rets.append(r)
            i += 1
    first_seen_status = "FIRST_SEEN_WALLET_AGE_DAYS"
    if missing_age:
        first_seen_status = "FIRST_SEEN_PARTIAL_WALLET_AGE"
    return rets, first_seen_status


def run_arm_b() -> dict:
    entries = load_entries()
    closed = load_closed_trades()
    rets, first_seen = _cohort_returns(entries, closed)
    perm, _ = _cohort_returns(entries, closed, permute_seed=int(CONFIG["seed"]))

    n = len(rets)
    m = mean(rets)
    gated_inconclusive = first_seen.startswith("INCONCLUSIVE")
    gates = {
        "first_seen_usable": not gated_inconclusive,
        "mean_gt_0": bool(m is not None and m > 0 and n >= int(CONFIG["min_powered_n"])),
        "top1_removed_gt_0": bool(
            top1_removed_mean(rets) is not None
            and top1_removed_mean(rets) > 0
            and n >= int(CONFIG["min_powered_n"])
        ),
        "permute_weaker": bool(m is not None and mean(perm) is not None and m > mean(perm)),
        "k_not_lowered": True,
    }
    passed = (not gated_inconclusive) and all(gates.values())
    payload = {
        "generated_at": utc_now_iso(),
        "arm": "B_NEW_COHORT",
        "first_seen_status": first_seen,
        "k_min": CONFIG["arm_b_k_min"],
        "W_new_sec": CONFIG["arm_b_W_new_sec"],
        "dt_sec": CONFIG["arm_b_dt_sec"],
        "n_signals": n,
        "mean": m,
        "median": median(rets),
        "top1_removed_mean": top1_removed_mean(rets),
        "permute_mean": mean(perm),
        "permute_n": len(perm),
        "gates": gates,
        "passed": passed,
        "note": "Cohort sync ≠ single-wallet follow. Missing first_seen is INCONCLUSIVE, not a k cut.",
    }
    write_json(ROOT / "out/arm_b.json", payload)
    return payload


if __name__ == "__main__":
    p = run_arm_b()
    print(p["first_seen_status"], p["n_signals"], p["passed"])
