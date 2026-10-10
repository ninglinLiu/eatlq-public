"""Task 4: robust resampling battery for WHALE_EVENT absolute + paired incremental."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..arms import prepare_splits
from ..common import load_freeze
from .bootstrap_utils import block_keys_from_day, cluster_bootstrap_sum, leave_one_unit_out
from .data import load_audit_cfg, save_audit_json, selected


def _pnl_frame(sel: pd.DataFrame) -> pd.DataFrame:
    d = sel.copy()
    d["_pnl"] = pd.to_numeric(d["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    if "day" not in d.columns:
        d["day"] = pd.to_datetime(d["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    d["day"] = d["day"].astype(str)
    if "entity_cluster" not in d.columns:
        actor = d["economic_actor"] if "economic_actor" in d.columns else d.get("actor")
        d["entity_cluster"] = actor.astype(str) if actor is not None else "unknown"
    return d


def absolute_resampling(sel: pd.DataFrame, *, n_boot: int, seed: int) -> dict[str, Any]:
    d = _pnl_frame(sel)
    pnl = d["_pnl"].to_numpy()
    out = {
        "n": int(len(d)),
        "total_pnl": float(pnl.sum()),
        "token_cluster": cluster_bootstrap_sum(pnl, d["token"].astype(str).to_numpy(), n_boot=n_boot, seed=seed),
        "day_block": cluster_bootstrap_sum(
            pnl, block_keys_from_day(d["day"], "day"), n_boot=n_boot, seed=seed + 1
        ),
        "week_block": cluster_bootstrap_sum(
            pnl, block_keys_from_day(d["day"], "week"), n_boot=n_boot, seed=seed + 2
        ),
        "actor_cluster": cluster_bootstrap_sum(
            pnl, d["entity_cluster"].astype(str).to_numpy(), n_boot=n_boot, seed=seed + 3
        ),
        "loo_token": leave_one_unit_out(d["_pnl"], d["token"]),
        "loo_actor": leave_one_unit_out(d["_pnl"], d["entity_cluster"]),
    }
    # Fold / era LOO using prepare_splits fold labels if present, else recompute
    if "fold" not in d.columns or d["fold"].isna().all():
        freeze = load_freeze()
        split = prepare_splits(d, freeze)
        d = d.copy()
        d["fold"] = split["fold"].values
    fold_pnl = d.groupby("fold")["_pnl"].sum()
    fold_pnl = fold_pnl[fold_pnl.index.notna()]
    loo_fold_flips = 0
    total = float(d["_pnl"].sum())
    for f, fp in fold_pnl.items():
        sub = total - float(fp)
        if total != 0 and (total > 0) != (sub > 0):
            loo_fold_flips += 1
    out["fold_pnl"] = {str(k): float(v) for k, v in fold_pnl.items()}
    out["loo_fold"] = {
        "n_folds": int(len(fold_pnl)),
        "n_sign_flips": int(loo_fold_flips),
        "flip_rate": float(loo_fold_flips / len(fold_pnl)) if len(fold_pnl) else 0.0,
        "n_positive_folds": int((fold_pnl > 0).sum()),
    }
    # Pass / fail flags (absolute positivity robustness — not incremental)
    out["passes"] = {
        "token_bootstrap_p05_positive": bool(out["token_cluster"]["p05"] > 0),
        "day_bootstrap_p05_positive": bool(out["day_block"]["p05"] > 0),
        "week_bootstrap_p05_positive": bool(out["week_block"]["p05"] > 0),
        "actor_bootstrap_p05_positive": bool(out["actor_cluster"]["p05"] > 0),
        "loo_token_stable": bool(out["loo_token"]["flip_rate"] < 0.5),
        "loo_actor_stable": bool(out["loo_actor"]["flip_rate"] < 0.75),
        "majority_folds_positive": bool(
            out["loo_fold"]["n_folds"] >= 2
            and out["loo_fold"]["n_positive_folds"] >= (out["loo_fold"]["n_folds"] + 1) // 2
        ),
    }
    out["all_absolute_robustness_pass"] = bool(all(out["passes"].values()))
    return out


def run_resampling(oof: dict[str, pd.DataFrame], incremental_report: dict | None = None) -> dict[str, Any]:
    cfg = load_audit_cfg()
    n_boot = int(cfg.get("bootstrap", {}).get("n_boot", 200))
    seed = int(cfg.get("bootstrap", {}).get("seed", 7))

    whale_sel = selected(oof["WHALE_EVENT"])
    pool_sel = selected(oof["POOL_ONLY"])
    print(f"[audit:resampling] WHALE n={len(whale_sel)} POOL n={len(pool_sel)}", flush=True)

    whale_abs = absolute_resampling(whale_sel, n_boot=n_boot, seed=seed)
    pool_abs = absolute_resampling(pool_sel, n_boot=n_boot, seed=seed + 10)

    paired = {}
    if incremental_report:
        ov = incremental_report.get("within_opportunity_overlay", {})
        mt = incremental_report.get("covariate_matched", {})
        paired = {
            "overlay_token_p05": ov.get("paired_delta_p05_token"),
            "overlay_day_p05": ov.get("paired_delta_p05_day"),
            "overlay_passes": ov.get("passes_paired_p05"),
            "matched_token_p05": mt.get("paired_delta_p05_token"),
            "matched_day_p05": mt.get("paired_delta_p05_day"),
            "matched_passes": mt.get("passes_paired_p05"),
        }

    # Primary information verdict remains prospective unless paired incremental clears
    if paired.get("overlay_passes") and paired.get("matched_passes") and whale_abs["all_absolute_robustness_pass"]:
        info_verdict = "INFORMATION_EDGE_GO"
    else:
        info_verdict = "PROSPECTIVE_CONFIRMATION_REQUIRED"

    report = {
        "task": "robust_resampling_battery",
        "whale_absolute": whale_abs,
        "pool_absolute": pool_abs,
        "paired_incremental_summary": paired,
        "primary_information_verdict": info_verdict,
        "note": (
            "Absolute token-bootstrap p05>0 only shows the whale trade set's total is stable; "
            "it does not prove incremental edge vs POOL_ONLY. Paired Δ bootstraps are decisive."
        ),
    }
    save_audit_json("04_resampling.json", report)
    return report
