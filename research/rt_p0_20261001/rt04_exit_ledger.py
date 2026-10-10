#!/usr/bin/env python3
"""RT04 bounds from already published samples. No new chain calls."""

from __future__ import annotations

import json
from pathlib import Path

GL2 = Path("/root/copy_cascade_invert_r0/data/raw/golden/base_gl2_b_final_20260715.json")
LAT = Path("/root/copy_cascade_invert_r0/out/latency_tax.json")
OUT = Path("/root/rt_p0_20261001/rt04_exit_ledger.json")


def pair_from_summary(block: dict) -> tuple[float, float]:
    """n_resolved == 2. max is one value; the other follows from the mean."""
    if block["resolved"] != 2:
        raise SystemExit("expected two resolved baseline trades")
    hi = block["max"]
    lo = 2 * block["mean"] - hi
    return lo, hi


def main() -> None:
    gl2 = json.loads(GL2.read_text())
    lat = json.loads(LAT.read_text())
    base = gl2["forward"]["descriptive_contaminated_group_by_delay"]["1"]["baseline"]
    smart = gl2["forward"]["descriptive_contaminated_group_by_delay"]["1"]["smart"]
    lo, hi = pair_from_summary(base)
    point_resolved = base["mean"]
    # Full denominator of 4. Unknowns are not facts.
    as_zero = (lo + hi + 0 + 0) / 4
    as_full_loss = (lo + hi - 1 - 1) / 4
    delays = {}
    for sec, block in gl2["forward"]["descriptive_contaminated_group_by_delay"].items():
        delays[sec] = {
            "smart_mean": block["smart"]["mean"],
            "smart_resolved": block["smart"]["resolved"],
            "smart_unresolved": block["smart"]["unresolved"],
            "baseline_mean_resolved_only": block["baseline"]["mean"],
            "baseline_unresolved": block["baseline"]["unresolved"],
            "baseline_unresolved_reason": block["baseline"]["unresolved_reasons"],
        }
    ledger = {
        "new_chain_calls": False,
        "merit_claimed": False,
        "samples": [
            {
                "id": "GL2B-baseline-forward",
                "source": str(GL2),
                "what_it_is": "market reference after a delay plus 30 bps, not our fill",
                "n": 4,
                "confirmed_resolved": 2,
                "unknown": 2,
                "unknown_class": "UNRESOLVED_NO_TRUE_EXIT",
                "confirmed_fail": 0,
                "resolved_pair": [lo, hi],
                "point_if_unknowns_dropped": point_resolved,
                "bound_if_unknowns_marked_zero": as_zero,
                "bound_if_unknowns_marked_minus_100pct": as_full_loss,
                "note": "Published mean uses the 2 resolved rows only. Dropping unknowns and calling them total losses are different numbers.",
            },
            {
                "id": "GL2B-smart-forward",
                "source": str(GL2),
                "what_it_is": "same reference price, contaminated wallets only",
                "n": smart["n_signals"],
                "confirmed_resolved": smart["resolved"],
                "unknown": smart["unresolved"],
                "confirmed_fail_or_negative": smart["n_signals"],
                "mean": smart["mean"],
                "note": "All 4 are resolved and negative, and all 4 come from the 2 left-truncated wallets. This is not an eligibility result.",
            },
            {
                "id": "latency-tax-vendor-overlap",
                "source": str(LAT),
                "n": lat["n_entries_considered"],
                "atomic_sell_sim": "INCONCLUSIVE_NO_RPC",
                "delays_sec": {
                    k: {"mean": v["mean_delayed_return"], "unrealizable": v["unrealizable"]}
                    for k, v in lat["curves"].items()
                },
                "note": "n=3 is below the project's powered gate of 20. No tax verdict.",
            },
            {
                "id": "live-receipts-2026-06",
                "source": "LEGACY/03_SCOPED_VERDICTS.md section 6 receipt table",
                "re_fetched_this_round": False,
                "buys": 8,
                "sells_landed": 3,
                "bot_sell_attempts_status_0": 48,
                "status_0_class": "confirmed_fail",
                "unsold_positions": 5,
                "unsold_class": "confirmed_unfilled_then_pool_collapsed",
                "mark_weth": 0.001363,
                "cost_weth": 0.16142,
                "economic_net_weth": -0.160235862229,
                "after_2026_06_29": "UNKNOWN",
                "note": "Mark near zero is not the economic result. The 48 failed sends are confirmed failures, not unknown exits.",
            },
        ],
        "delay_on_gl2_published_means": delays,
        "size_sweep_this_round": None,
        "gap": "Entry size, pool state, tax and impact were not re-read from receipts or traces in this round. Bounds above only restate published aggregates.",
    }
    OUT.write_text(json.dumps(ledger, indent=2) + "\n")
    print(json.dumps({
        "baseline_pair": [lo, hi],
        "dropped": point_resolved,
        "unknown_as_zero": as_zero,
        "unknown_as_full_loss": as_full_loss,
        "smart_mean": smart["mean"],
        "latency_n": lat["n_entries_considered"],
    }, indent=2))


if __name__ == "__main__":
    main()
