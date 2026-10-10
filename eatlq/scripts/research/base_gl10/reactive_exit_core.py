#!/usr/bin/env python3
"""Pure, offline GL10 reactive-exit calculations.

The functions in this module do not know rug labels and do not perform RPC.
They consume only a causal reserve history or frozen return cells.
"""
from __future__ import annotations

import hashlib
import json
from statistics import median

PESS = -1.0229943556396193
COST = 0.0229943556396193
LAGS = (1, 2, 3, 5, 15)
OFFSETS = (1, 5, 15)


def first_onset(entry_block: int, anchor_balance: int, block_net: dict[int, int]) -> dict:
    """Return the first causal 30%-one-block or 50%-three-block reserve drop.

    ``block_net`` is positive for WETH flowing into the pool.  At candidate b,
    only observations through b are accessed.  The outcome/flip label is not an
    input to this function.
    """
    balances = {entry_block - 1: int(anchor_balance)}
    current = int(anchor_balance)
    for block in sorted(block_net):
        if block < entry_block:
            raise ValueError("pre-entry flow supplied")
        # Fill only the three causal lookback points needed by the rule.
        for k in range(max(entry_block, block - 3), block):
            balances.setdefault(k, current)
        prev = balances.get(block - 1, current)
        current += int(block_net[block])
        if current < 0:
            return {"status": "INVALID_NEGATIVE_BALANCE", "onset_block": None}
        balances[block] = current
        net_out_1 = -int(block_net[block])
        one_ratio = (1.0 - current / prev) if prev > 0 else None
        lo = max(entry_block - 1, block - 3)
        prior = [balances.get(k, prev) for k in range(lo, block)]
        peak3 = max(prior) if prior else prev
        net_out_3 = -sum(int(block_net.get(k, 0)) for k in range(block - 2, block + 1))
        three_ratio = (1.0 - current / peak3) if peak3 > 0 else None
        hit_one = prev > 0 and net_out_1 > 0 and one_ratio is not None and one_ratio >= 0.30
        hit_three = peak3 > 0 and net_out_3 > 0 and three_ratio is not None and three_ratio >= 0.50
        if hit_one or hit_three:
            return {
                "status": "ONSET_DETECTED",
                "onset_block": block,
                "balance_before_raw": str(prev),
                "balance_after_raw": str(current),
                "single_block_drop": one_ratio,
                "rolling_3_block_drop": three_ratio,
                "single_hit": hit_one,
                "rolling_hit": hit_three,
                "evidence_max_block": block,
            }
    return {"status": "ONSET_NOT_DETECTED", "onset_block": None}


def return_cell(cell: dict) -> tuple[float, float]:
    """Convert one frozen GL5v3/GL8 cell into its honest lower/upper bounds."""
    if cell.get("resolved") and cell.get("ret") is not None:
        x = float(cell["ret"])
        return x, x
    return PESS, float(cell.get("optimistic_qv2_ret", PESS))


def vector_metrics(cells: list[dict]) -> dict:
    """Compute exact pooled double-bound metrics for frozen/effective cells."""
    lower, upper = zip(*(return_cell(x) for x in cells)) if cells else ((), ())
    lower, upper = list(lower), list(upper)
    if lower:
        top = max(range(len(lower)), key=lambda i: lower[i])
        ex = [x for i, x in enumerate(lower) if i != top]
        pos = sum(max(0.0, x) for x in lower)
        top_share = max(0.0, lower[top]) / pos if pos else 0.0
    else:
        ex, top_share = [], None
    payload = json.dumps(lower, separators=(",", ":"), allow_nan=False).encode()
    return {
        "n": len(cells),
        "resolved_n": sum(bool(x.get("resolved")) for x in cells),
        "unresolved_n": sum(not bool(x.get("resolved")) for x in cells),
        "pooled_mean_interval": [sum(lower) / len(lower) if lower else None,
                                 sum(upper) / len(upper) if upper else None],
        "median_interval": [median(lower) if lower else None, median(upper) if upper else None],
        "win_rate_interval": [sum(x > 0 for x in lower) / len(lower) if lower else None,
                              sum(x > 0 for x in upper) / len(upper) if upper else None],
        "ex_top1_lower_mean": sum(ex) / len(ex) if ex else None,
        "top1_positive_contribution": top_share,
        "lower_vector_sha256": hashlib.sha256(payload).hexdigest(),
    }


def verdict(rows: list[dict], baseline_primary: dict) -> tuple[str, list[int]]:
    """Apply the frozen powered/material-help mapping exactly."""
    powered = [r for r in rows if r["evaluable_n"] >= 30]
    eligible = []
    for row in powered:
        p = row["economics"]["1"]
        if (row["rug_median_improvement_lower"] >= 0.20
                and p["pooled_mean_interval"][0] + 1e-12 >= baseline_primary["pooled_mean_interval"][0]
                and p["ex_top1_lower_mean"] + 1e-12 >= baseline_primary["ex_top1_lower_mean"]):
            eligible.append(int(row["lag_blocks"]))
    if not powered:
        return "INSUFFICIENT_N", eligible
    if eligible:
        return "REACTIVE_EXIT_MATERIALLY_HELPS", eligible
    return "REACTIVE_EXIT_INEFFECTIVE", eligible
