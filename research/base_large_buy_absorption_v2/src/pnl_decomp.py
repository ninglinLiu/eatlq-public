"""Phase 2: REGIME / SELECTION / EXECUTION PnL attribution.

POOL_ONLY = pool-structure baseline (no large-buy / absorption sieve),
same scheduler. Random control ≈ regime + random selection.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, load_freeze, save_json, utcnow
from .scorecard_v1 import eval_arm, paired_scheduled_delta
from .arms_v1 import capacity_matched_random


def run_pnl_decomp(oof: dict[str, pd.DataFrame], scorecard_details: dict) -> dict[str, Any]:
    freeze = load_freeze()
    eth_usd = float(freeze["capital"]["eth_usd_freeze"])

    def sched_usd(arm_key: str) -> float:
        arm = scorecard_details.get("arms", {}).get(arm_key) or scorecard_details.get("controls", {}).get(arm_key)
        if not arm:
            return 0.0
        return float(arm["scheduled"]["cum_withdrawn_usd"])

    def sched_weth(arm_key: str) -> float:
        arm = scorecard_details.get("arms", {}).get(arm_key) or scorecard_details.get("controls", {}).get(arm_key)
        if not arm:
            return 0.0
        return float(arm["scheduled"]["scheduled_trading_pnl_weth"])

    pool_usd = sched_usd("POOL_ONLY")
    rand_usd = sched_usd("CAPACITY_MATCHED_RANDOM")
    lba_usd = sched_usd("LARGE_BUY_ABSORPTION")
    lbo_usd = sched_usd("LARGE_BUY_ONLY")
    abs_usd = sched_usd("ABSORPTION_ONLY")

    pool_w = sched_weth("POOL_ONLY")
    rand_w = sched_weth("CAPACITY_MATCHED_RANDOM")
    lba_w = sched_weth("LARGE_BUY_ABSORPTION")

    # Re-eval fills for paired deltas if present in details — rebuild lightly
    pool_m = eval_arm(oof["POOL_ONLY"], "POOL_ONLY")
    lba_m = eval_arm(oof["LARGE_BUY_ABSORPTION"], "LARGE_BUY_ABSORPTION")
    spine = oof["POOL_ONLY"].copy()
    n_sel = int(spine["policy_selected"].sum()) if "policy_selected" in spine.columns else 0
    rand = spine.copy()
    rand["policy_selected"] = capacity_matched_random(rand, n_sel, seed=42)
    rand_m = eval_arm(rand, "CAPACITY_MATCHED_RANDOM")

    pair_lba_pool = paired_scheduled_delta(lba_m["fills"], pool_m["fills"])
    pair_rand_pool = paired_scheduled_delta(rand_m["fills"], pool_m["fills"])
    pair_lba_rand = paired_scheduled_delta(lba_m["fills"], rand_m["fills"])

    # EXECUTION loss proxy: compare unconstrained opportunity vs scheduled for LBA
    lba_unconst = float(lba_m["UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth"])
    lba_sched = float(lba_m["scheduled"]["scheduled_trading_pnl_weth"])
    skip_rate = float(lba_m["scheduled"]["skip_rate"])

    regime = {
        "name": "REGIME",
        "definition": (
            "Entering new-pool WETH V3 structure (POOL_ONLY / random on same spine) "
            "with TP ladder + scheduled wallet. Money from 'being in the game', not from sieves."
        ),
        "pool_only_cum_withdrawn_usd": pool_usd,
        "random_cum_withdrawn_usd": rand_usd,
        "pool_only_sched_weth": pool_w,
        "random_sched_weth": rand_w,
        "interpretation": (
            f"Random ~${rand_usd:.0f} and POOL_ONLY ~${pool_usd:.0f} over the covered window "
            "show the regime (new pools + exits + capital rules) can print positive USD. "
            "That is NOT proof of a selectable edge over the baseline."
        ),
    }
    selection = {
        "name": "SELECTION",
        "definition": (
            "Incremental scheduled PnL of large-buy / absorption sieves vs POOL_ONLY "
            "under identical scheduler (paired day bootstrap)."
        ),
        "lba_vs_pool_sum_delta_weth": pair_lba_pool["sum_delta"],
        "lba_vs_pool_p05_day": pair_lba_pool["p05_day"],
        "lba_vs_pool_passes": pair_lba_pool["passes"],
        "rand_vs_pool_sum_delta_weth": pair_rand_pool["sum_delta"],
        "rand_vs_pool_p05_day": pair_rand_pool["p05_day"],
        "lba_vs_rand_sum_delta_weth": pair_lba_rand["sum_delta"],
        "lba_vs_rand_p05_day": pair_lba_rand["p05_day"],
        "lba_vs_rand_passes": pair_lba_rand["passes"],
        "arm_cum_withdrawn_usd": {
            "POOL_ONLY": pool_usd,
            "CAPACITY_MATCHED_RANDOM": rand_usd,
            "LARGE_BUY_ONLY": lbo_usd,
            "ABSORPTION_ONLY": abs_usd,
            "LARGE_BUY_ABSORPTION": lba_usd,
        },
        "interpretation": (
            "SELECTION edge requires LBA paired Δ vs POOL_ONLY with day-cluster bootstrap p05 > 0. "
            "Beating random is a secondary check (does the sieve beat luck under same capacity)."
        ),
    }
    execution = {
        "name": "EXECUTION",
        "definition": (
            "Drag from latency, capacity skips, failed fills, conservative loss floors."
        ),
        "lba_unconstrained_opportunity_weth": lba_unconst,
        "lba_scheduled_weth": lba_sched,
        "capacity_skip_rate": skip_rate,
        "latency_seconds_primary": int(freeze["signal_timing"]["measured_latency_seconds"]),
        "absorption_window_seconds_primary": int(freeze["signal_timing"]["absorption_window_seconds"]),
        "drag_unconst_minus_sched_weth": lba_unconst - lba_sched,
        "interpretation": (
            f"Skip rate {skip_rate:.1%} under 3-slot / one-token scheduler dominates. "
            "Unconstrained opportunity is diagnostic only — not tradable capital."
        ),
    }

    # Plain-language POOL_ONLY
    pool_only_explainer = {
        "what_it_is": (
            "POOL_ONLY = buy opportunities that pass only pool-structure filters "
            "(age 15m–24h, min size, depth/momentum/intensity score quantile), "
            "WITHOUT requiring a 'large buy' or 'absorbed' flag. Same $1000 wallet, "
            "3 concurrent slots, one position per token, TP ladder exits."
        ),
        "what_it_is_not": (
            "Not 'all Base swaps'. Not smart-money. Not proof you should trade randomly."
        ),
        "why_random_can_look_good": (
            "CAPACITY_MATCHED_RANDOM picks the same count of eligible buys as POOL_ONLY "
            "from the same spine. If the regime itself has positive expected USD after costs, "
            "random can withdraw ~$500–$600 without any sieve edge."
        ),
    }

    report = {
        "task": "PNL_DECOMPOSITION",
        "generated_at": utcnow(),
        "eth_usd_freeze": eth_usd,
        "REGIME": regime,
        "SELECTION": selection,
        "EXECUTION": execution,
        "POOL_ONLY_explainer": pool_only_explainer,
        "verdicts": {
            "REGIME_POSITIVE_USD": "PROVEN" if max(pool_usd, rand_usd) > 0 else "REFUTED",
            "SELECTION_EDGE_LBA_VS_POOL": (
                "PROVEN" if pair_lba_pool["passes"] and (lba_w - pool_w) > 0
                else ("UNCONFIRMED" if (lba_w - pool_w) > 0 else "REFUTED")
            ),
            "SELECTION_EDGE_LBA_VS_RANDOM": (
                "PROVEN" if pair_lba_rand["passes"] and (lba_w - rand_w) > 0
                else ("UNCONFIRMED" if (lba_w - rand_w) > 0 else "REFUTED")
            ),
        },
    }
    save_json(OUT / "pnl_decomp.json", report)
    md = [
        "# PnL Decomposition (Phase 2)",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## POOL_ONLY = 池子结构基线",
        "",
        pool_only_explainer["what_it_is"],
        "",
        pool_only_explainer["why_random_can_look_good"],
        "",
        "## REGIME",
        "",
        f"- POOL_ONLY withdrawn: `${pool_usd:.2f}`",
        f"- Random withdrawn: `${rand_usd:.2f}`",
        f"- Verdict: `{report['verdicts']['REGIME_POSITIVE_USD']}`",
        "",
        "## SELECTION",
        "",
        f"- LBA vs POOL paired Δ WETH: `{pair_lba_pool['sum_delta']:.4f}` (p05 day `{pair_lba_pool['p05_day']:.4f}`, pass `{pair_lba_pool['passes']}`)",
        f"- LBA vs Random paired Δ WETH: `{pair_lba_rand['sum_delta']:.4f}` (p05 day `{pair_lba_rand['p05_day']:.4f}`)",
        f"- LBA vs POOL: `{report['verdicts']['SELECTION_EDGE_LBA_VS_POOL']}`",
        f"- LBA vs Random: `{report['verdicts']['SELECTION_EDGE_LBA_VS_RANDOM']}`",
        "",
        "## EXECUTION",
        "",
        f"- Skip rate: `{skip_rate:.2%}`",
        f"- Unconst diag WETH: `{lba_unconst:.4f}` vs scheduled `{lba_sched:.4f}`",
        "",
    ]
    (OUT / "PNL_DECOMP.md").write_text("\n".join(md))
    return report
