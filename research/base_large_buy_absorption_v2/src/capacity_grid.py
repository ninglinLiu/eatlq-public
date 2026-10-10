"""Track A: preregistered capacity grid with fixed exits. Do NOT pick best cell."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .arms_v1 import ARM_SCORE_COLS, capacity_matched_random
from .scheduler import simulate_scheduled
from .scorecard_v1 import paired_scheduled_delta
from .common import save_json, utcnow
from .v3_common import (
    V3_OUT,
    bh_correct,
    ensure_v3_out,
    load_oof_arms,
    load_v3,
    p_from_boot_frac_positive,
)


def _score_frame(frame: pd.DataFrame, arm: str) -> pd.DataFrame:
    d = frame.copy()
    col = ARM_SCORE_COLS.get(arm, "pool_only_score")
    d["_sched_score"] = pd.to_numeric(d.get(col, 0), errors="coerce").fillna(0.0)
    return d


def run_capacity_grid(oof: dict[str, pd.DataFrame] | None = None) -> dict[str, Any]:
    ensure_v3_out()
    cfg = load_v3()
    grid_cfg = cfg["capacity_grid"]
    oof = oof or load_oof_arms()
    n_boot = 200
    seed = 17

    # Capacity-matched random on POOL spine (same selection count as POOL_ONLY)
    spine = oof["POOL_ONLY"].copy()
    n_sel = int(spine["policy_selected"].sum())
    rand = spine.copy()
    rand["policy_selected"] = capacity_matched_random(rand, n_sel, seed=42)
    rand_arm = "CAPACITY_MATCHED_RANDOM"

    arms_run = {
        "POOL_ONLY": oof["POOL_ONLY"],
        "LARGE_BUY_ONLY": oof["LARGE_BUY_ONLY"],
        "ABSORPTION_ONLY": oof["ABSORPTION_ONLY"],
        "LARGE_BUY_ABSORPTION": oof["LARGE_BUY_ABSORPTION"],
        rand_arm: rand,
    }

    rows = []
    pvals_lba_pool = []
    pvals_lba_rand = []
    cell_keys = []

    for max_c in grid_cfg["max_concurrent"]:
        for mode in grid_cfg["multi_token_modes"]:
            one_tok = bool(mode["one_position_per_token"])
            mpt = int(mode["max_per_token"])
            label = mode["label"]
            print(f"[v3/A] grid max_c={max_c} mode={label} …", flush=True)
            fills_by_arm: dict[str, pd.DataFrame] = {}
            summaries: dict[str, dict] = {}
            for arm, fr in arms_run.items():
                scored = _score_frame(fr, arm if arm != rand_arm else "POOL_ONLY")
                fills, _monthly, summary = simulate_scheduled(
                    scored,
                    score_col="_sched_score",
                    max_concurrent=max_c,
                    one_position_per_token=one_tok,
                    max_per_token=mpt,
                )
                fills_by_arm[arm] = fills
                summaries[arm] = summary

            pair_pool = paired_scheduled_delta(
                fills_by_arm["LARGE_BUY_ABSORPTION"],
                fills_by_arm["POOL_ONLY"],
                n_boot=n_boot,
                seed=seed,
            )
            pair_rand = paired_scheduled_delta(
                fills_by_arm["LARGE_BUY_ABSORPTION"],
                fills_by_arm[rand_arm],
                n_boot=n_boot,
                seed=seed + 1,
            )
            # also token p05 as secondary
            p_day = p_from_boot_frac_positive(
                pair_pool.get("bootstrap_day", {}).get("frac_positive", 0.0), n_boot
            )
            p_rand = p_from_boot_frac_positive(
                pair_rand.get("bootstrap_day", {}).get("frac_positive", 0.0), n_boot
            )
            pvals_lba_pool.append(p_day)
            pvals_lba_rand.append(p_rand)
            cell_keys.append(f"c{max_c}_{label}")

            row = {
                "max_concurrent": max_c,
                "mode": label,
                "one_position_per_token": one_tok,
                "max_per_token": mpt,
                "lba_n_fills": summaries["LARGE_BUY_ABSORPTION"]["n_fills"],
                "pool_n_fills": summaries["POOL_ONLY"]["n_fills"],
                "rand_n_fills": summaries[rand_arm]["n_fills"],
                "lba_skip_rate": summaries["LARGE_BUY_ABSORPTION"]["skip_rate"],
                "pool_skip_rate": summaries["POOL_ONLY"]["skip_rate"],
                "lba_cum_usd": summaries["LARGE_BUY_ABSORPTION"]["cum_withdrawn_usd"],
                "pool_cum_usd": summaries["POOL_ONLY"]["cum_withdrawn_usd"],
                "rand_cum_usd": summaries[rand_arm]["cum_withdrawn_usd"],
                "lbo_cum_usd": summaries["LARGE_BUY_ONLY"]["cum_withdrawn_usd"],
                "abs_cum_usd": summaries["ABSORPTION_ONLY"]["cum_withdrawn_usd"],
                "lba_sched_weth": summaries["LARGE_BUY_ABSORPTION"]["scheduled_trading_pnl_weth"],
                "pool_sched_weth": summaries["POOL_ONLY"]["scheduled_trading_pnl_weth"],
                "lba_vs_pool_sum_delta": pair_pool["sum_delta"],
                "lba_vs_pool_p05_day": pair_pool["p05_day"],
                "lba_vs_pool_p05_token": pair_pool.get("p05_token"),
                "lba_vs_pool_passes_raw": bool(pair_pool["passes"]),
                "lba_vs_rand_sum_delta": pair_rand["sum_delta"],
                "lba_vs_rand_p05_day": pair_rand["p05_day"],
                "lba_vs_rand_passes_raw": bool(pair_rand["passes"]),
                "p_lba_vs_pool": p_day,
                "p_lba_vs_rand": p_rand,
            }
            rows.append(row)

    # BH across grid cells for LBA vs POOL
    alpha = float(cfg["multiple_testing"]["alpha"])
    reject_pool = bh_correct(pvals_lba_pool, alpha=alpha)
    reject_rand = bh_correct(pvals_lba_rand, alpha=alpha)
    for i, row in enumerate(rows):
        row["lba_vs_pool_bh_reject"] = bool(reject_pool[i])
        row["lba_vs_rand_bh_reject"] = bool(reject_rand[i])

    df = pd.DataFrame(rows)
    df.to_csv(V3_OUT / "capacity_grid.csv", index=False)

    n_cells = len(df)
    n_pass_raw = int(df["lba_vs_pool_passes_raw"].sum())
    n_pass_bh = int(df["lba_vs_pool_bh_reject"].sum())
    frac_bh = n_pass_bh / max(1, n_cells)
    # Robust = majority of cells BH-significant for LBA vs POOL
    if frac_bh >= 0.5 and n_pass_bh >= 1:
        verdict = "PROVEN"
    elif n_pass_raw >= max(1, n_cells // 2) and (df["lba_vs_pool_sum_delta"] > 0).mean() >= 0.5:
        verdict = "UNCONFIRMED"
    elif (df["lba_vs_pool_sum_delta"] <= 0).mean() >= 0.75 and n_pass_raw == 0:
        verdict = "REFUTED"
    else:
        verdict = "UNCONFIRMED"

    fills_baseline = int(df.loc[df["max_concurrent"] == 3, "lba_n_fills"].iloc[0]) if len(df) else 0
    fills_max = int(df["lba_n_fills"].max()) if len(df) else 0

    report = {
        "task": "CAPACITY_GRID",
        "generated_at": utcnow(),
        "n_cells": n_cells,
        "n_lba_vs_pool_raw_pass": n_pass_raw,
        "n_lba_vs_pool_bh_reject": n_pass_bh,
        "frac_bh_reject": frac_bh,
        "fills_at_c3": fills_baseline,
        "fills_max_across_grid": fills_max,
        "fills_lift": fills_max / max(1, fills_baseline),
        "verdict_SELECTION_EDGE_ROBUST_ACROSS_CAPACITY": verdict,
        "rule": grid_cfg["edge_rule"],
        "note": "Do NOT select parameters from best cell. Exit rules frozen.",
        "rows": rows,
    }
    save_json(V3_OUT / "capacity_grid.json", report)
    print(
        f"[v3/A] fills {fills_baseline}→{fills_max} (x{report['fills_lift']:.1f}); "
        f"BH pass {n_pass_bh}/{n_cells}; verdict={verdict}",
        flush=True,
    )
    return report


if __name__ == "__main__":
    run_capacity_grid()
