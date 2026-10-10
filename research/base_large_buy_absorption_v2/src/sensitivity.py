"""Diagnostic-only timing sensitivity. NEVER used to pick parameters.

One-at-a-time sweeps (not full factorial) to bound compute:
  - absorb ∈ {5,30,60,300} at latency=0
  - latency ∈ {0,5,15,30} at absorb=5
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .arms_v1 import select_arm
from .common import OUT, load_freeze, save_json, utcnow
from .labels_truth import label_events_truth
from .scheduler import simulate_scheduled
from .signals import assert_lookahead_free, build_arm_panel


def _eval_lba_config(
    buys: pd.DataFrame,
    resolved: pd.DataFrame,
    aw: int,
    lat: int,
    *,
    train_frac: float = 0.7,
) -> dict[str, Any]:
    panel = build_arm_panel(
        buys,
        "LARGE_BUY_ABSORPTION",
        absorption_window_seconds=aw,
        measured_latency_seconds=lat,
    )
    la = assert_lookahead_free(panel, absorption_window_seconds=aw, measured_latency_seconds=lat)
    if not la["pass"]:
        return {"absorption_window_s": aw, "latency_s": lat, "error": "LOOKAHEAD_FAIL", "lookahead": la}
    labeled, _ = label_events_truth(panel, resolved)
    # Simple chronological train/test — diagnostic only (not the primary WF scorecard)
    cut = labeled["decision_time"].quantile(train_frac)
    train = labeled[labeled["decision_time"] <= cut]
    test = labeled[labeled["decision_time"] > cut]
    freeze = load_freeze()
    q = float(freeze["selection"]["score_quantile"])
    top_k = int(freeze["selection"]["top_k_per_decision_window"])
    _, thr = select_arm(train if len(train) else test, "LARGE_BUY_ABSORPTION", quantile=q, top_k=top_k)
    sel, _ = select_arm(test, "LARGE_BUY_ABSORPTION", quantile=q, top_k=top_k, train_threshold=thr)
    test = test.copy()
    test["policy_selected"] = sel
    test["_sched_score"] = pd.to_numeric(test.get("lba_score", 0), errors="coerce").fillna(0)
    fills, monthly, summary = simulate_scheduled(test, score_col="_sched_score")
    return {
        "absorption_window_s": aw,
        "latency_s": lat,
        "lookahead": la["verdict"],
        "n_test": int(len(test)),
        "n_selected": int(sel.sum()),
        "n_fills": summary["n_fills"],
        "skip_rate": summary["skip_rate"],
        "scheduled_trading_pnl_weth": summary["scheduled_trading_pnl_weth"],
        "cum_withdrawn_usd": summary["cum_withdrawn_usd"],
        "unconstrained_diag_weth": summary["unconstrained_opportunity_pnl_weth"],
        "method": "diagnostic_chrono_split_not_primary_wf",
    }


def run_sensitivity(buys: pd.DataFrame, resolved: pd.DataFrame) -> dict[str, Any]:
    freeze = load_freeze()
    primary_a = int(freeze["signal_timing"]["absorption_window_seconds"])
    primary_l = int(freeze["signal_timing"]["measured_latency_seconds"])
    abs_grid = list(dict.fromkeys([primary_a] + list(
        freeze["sensitivity_controls_diagnostic_only"]["absorption_windows_seconds"]
    )))
    lat_grid = list(dict.fromkeys([primary_l] + list(
        freeze["sensitivity_controls_diagnostic_only"]["latencies_seconds"]
    )))

    # Diagnostic subsample (~20% by pool) to keep compute bounded; primary WF uses full data.
    rng = np.random.default_rng(7)
    pools = buys["pool"].astype(str).unique()
    keep_pools = set(rng.choice(pools, size=max(1, int(0.2 * len(pools))), replace=False))
    buys_s = buys[buys["pool"].astype(str).isin(keep_pools)].copy()
    resolved_s = resolved[resolved["pool"].astype(str).isin(keep_pools)].copy()
    print(f"[sens] subsample pools={len(keep_pools)}/{len(pools)} buys={len(buys_s):,}", flush=True)

    rows = []
    for aw in abs_grid:
        print(f"[sens] absorb={aw}s latency={primary_l}s …", flush=True)
        r = _eval_lba_config(buys_s, resolved_s, aw, primary_l)
        r["sweep"] = "absorb_at_primary_latency"
        rows.append(r)
    for lat in lat_grid:
        if lat == primary_l:
            continue
        print(f"[sens] absorb={primary_a}s latency={lat}s …", flush=True)
        r = _eval_lba_config(buys_s, resolved_s, primary_a, lat)
        r["sweep"] = "latency_at_primary_absorb"
        rows.append(r)

    for r in rows:
        r["is_primary_preregistered"] = bool(
            r.get("absorption_window_s") == primary_a and r.get("latency_s") == primary_l
        )
        r["subsample_frac_pools"] = 0.2

    report = {
        "task": "SENSITIVITY_DIAGNOSTIC_ONLY",
        "generated_at": utcnow(),
        "warning": "DO NOT select parameters from these results. Primary is preregistered 5s/0s. Curves use 20% pool subsample.",
        "primary": {"absorption_window_s": primary_a, "latency_s": primary_l},
        "rows": rows,
    }
    save_json(OUT / "sensitivity.json", report)
    pd.DataFrame(rows).to_csv(OUT / "sensitivity.csv", index=False)
    return report
