"""Task 1: matched / paired incremental estimands for WHALE_EVENT vs POOL_ONLY.

Replaces the invalid arithmetic (sum_whale - sum_pool_only) with:
  1) within-opportunity overlay on the POOL_ONLY spine
  2) covariate-matched non-whale control
  3) token/day-clustered paired bootstrap of ΔPnL
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .bootstrap_utils import paired_delta_bootstrap
from .data import (
    add_covariate_bins,
    load_audit_cfg,
    save_audit_json,
    selected,
    whale_flag,
)


def _invalid_legacy_incremental(whale_sel: pd.DataFrame, pool_sel: pd.DataFrame) -> dict[str, Any]:
    w = float(pd.to_numeric(whale_sel["wallet_pnl_weth"], errors="coerce").fillna(0).sum())
    p = float(pd.to_numeric(pool_sel["wallet_pnl_weth"], errors="coerce").fillna(0).sum())
    return {
        "name": "LEGACY_ARITHMETIC_DIFF",
        "valid": False,
        "whale_n": int(len(whale_sel)),
        "pool_n": int(len(pool_sel)),
        "whale_total_pnl": w,
        "pool_total_pnl": p,
        "arithmetic_diff": w - p,
        "note": "Invalid estimand: subtracts totals of two different selection sets.",
    }


def within_opportunity_overlay(
    pool_oof: pd.DataFrame,
    *,
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    """On POOL_ONLY-selected rows, compare whale-flagged vs non-whale subsets."""
    pool_sel = selected(pool_oof)
    if pool_sel.empty:
        return {"name": "WITHIN_OPPORTUNITY_OVERLAY", "n": 0}
    flag = whale_flag(pool_sel)
    treat = pool_sel.loc[flag]
    ctrl = pool_sel.loc[~flag]
    t_pnl = pd.to_numeric(treat["wallet_pnl_weth"], errors="coerce").fillna(0.0)
    c_pnl = pd.to_numeric(ctrl["wallet_pnl_weth"], errors="coerce").fillna(0.0)

    # Capacity-matched: sample |treat| from control (or all if smaller) and pair by rank of pnl? 
    # Better: compare means with cluster bootstrap of (mean_t - mean_c) via resampling tokens.
    # Paired capacity match: randomly match without replacement within token when possible,
    # else global random capacity match, then bootstrap sum of pair deltas.
    rng = np.random.default_rng(seed)
    n_pair = min(len(treat), len(ctrl))
    if n_pair == 0:
        return {
            "name": "WITHIN_OPPORTUNITY_OVERLAY",
            "n_pool_selected": int(len(pool_sel)),
            "n_whale_within_pool": int(len(treat)),
            "n_nonwhale_within_pool": int(len(ctrl)),
            "whale_share_of_pool": float(flag.mean()) if len(pool_sel) else 0.0,
            "mean_pnl_whale": float(t_pnl.mean()) if len(t_pnl) else None,
            "mean_pnl_nonwhale": float(c_pnl.mean()) if len(c_pnl) else None,
            "n_pairs": 0,
        }

    t_idx = treat.index.to_numpy()
    c_idx = ctrl.index.to_numpy()
    # Prefer same-token matches
    t_tok = treat["token"].astype(str).to_numpy()
    c_tok = ctrl["token"].astype(str).to_numpy()
    c_by_tok: dict[str, list[int]] = {}
    for i, tok in enumerate(c_tok):
        c_by_tok.setdefault(tok, []).append(i)

    used_c = set()
    pairs_t, pairs_c = [], []
    for ti, tok in enumerate(t_tok):
        cands = [j for j in c_by_tok.get(tok, []) if j not in used_c]
        if cands:
            j = int(rng.choice(cands))
            used_c.add(j)
            pairs_t.append(ti)
            pairs_c.append(j)
        if len(pairs_t) >= n_pair:
            break
    # Fill remaining with global unused control
    remain_t = [i for i in range(len(t_idx)) if i not in set(pairs_t)]
    remain_c = [j for j in range(len(c_idx)) if j not in used_c]
    rng.shuffle(remain_t)
    rng.shuffle(remain_c)
    for ti, cj in zip(remain_t, remain_c):
        pairs_t.append(ti)
        pairs_c.append(cj)
        if len(pairs_t) >= n_pair:
            break

    t_vals = t_pnl.to_numpy()[np.array(pairs_t, dtype=int)]
    c_vals = c_pnl.to_numpy()[np.array(pairs_c, dtype=int)]
    delta = t_vals - c_vals
    clusters = t_tok[np.array(pairs_t, dtype=int)]
    day = treat["day"].astype(str).to_numpy()[np.array(pairs_t, dtype=int)]
    boot_tok = paired_delta_bootstrap(delta, clusters, n_boot=n_boot, seed=seed)
    boot_day = paired_delta_bootstrap(delta, day, n_boot=n_boot, seed=seed + 1)

    return {
        "name": "WITHIN_OPPORTUNITY_OVERLAY",
        "n_pool_selected": int(len(pool_sel)),
        "n_whale_within_pool": int(len(treat)),
        "n_nonwhale_within_pool": int(len(ctrl)),
        "whale_share_of_pool": float(flag.mean()),
        "mean_pnl_whale": float(t_pnl.mean()),
        "mean_pnl_nonwhale": float(c_pnl.mean()) if len(c_pnl) else None,
        "mean_pnl_ratio": float(t_pnl.mean() / c_pnl.mean()) if len(c_pnl) and c_pnl.mean() != 0 else None,
        "n_pairs": int(len(delta)),
        "sum_paired_delta": float(delta.sum()),
        "mean_paired_delta": float(delta.mean()),
        "bootstrap_token_cluster_delta": boot_tok,
        "bootstrap_day_cluster_delta": boot_day,
        "paired_delta_p05_token": boot_tok["p05"],
        "paired_delta_p05_day": boot_day["p05"],
        "passes_paired_p05": bool(boot_tok["p05"] > 0 and boot_day["p05"] > 0),
    }


def covariate_matched_control(
    whale_oof: pd.DataFrame,
    pool_oof: pd.DataFrame,
    *,
    n_boot: int,
    seed: int,
    max_pairs: int,
) -> dict[str, Any]:
    """1:1 match each whale-selected signal to a non-whale buy on age/depth/mom/size strata."""
    whale_sel = add_covariate_bins(selected(whale_oof))
    # Candidate controls: successful buys that are NOT whale-flagged
    universe = add_covariate_bins(pool_oof.copy())
    if "buy_status" in universe.columns:
        universe = universe[universe["buy_status"].eq("SUCCESS")]
    non_whale = universe.loc[~whale_flag(universe)].copy()
    if whale_sel.empty or non_whale.empty:
        return {"name": "COVARIATE_MATCHED", "n_pairs": 0}

    rng = np.random.default_rng(seed)
    # Index controls by stratum
    ctrl_pos: dict[str, list[int]] = {}
    for i, st in enumerate(non_whale["match_stratum"].astype(str).to_numpy()):
        ctrl_pos.setdefault(st, []).append(i)
    for st in ctrl_pos:
        rng.shuffle(ctrl_pos[st])
    ptr = {st: 0 for st in ctrl_pos}

    t_pnl = pd.to_numeric(whale_sel["wallet_pnl_weth"], errors="coerce").fillna(0.0).to_numpy()
    c_pnl_all = pd.to_numeric(non_whale["wallet_pnl_weth"], errors="coerce").fillna(0.0).to_numpy()
    t_stratum = whale_sel["match_stratum"].astype(str).to_numpy()
    t_token = whale_sel["token"].astype(str).to_numpy()
    t_day = whale_sel["day"].astype(str).to_numpy()
    # Covariates for balance
    t_size = whale_sel["_size"].to_numpy()
    t_depth = whale_sel["_depth"].to_numpy()
    t_mom = whale_sel["_mom"].to_numpy()
    c_size = non_whale["_size"].to_numpy()
    c_depth = non_whale["_depth"].to_numpy()
    c_mom = non_whale["_mom"].to_numpy()

    deltas, clusters, days = [], [], []
    bal_t_size, bal_c_size = [], []
    bal_t_depth, bal_c_depth = [], []
    bal_t_mom, bal_c_mom = [], []
    n_unmatched = 0
    order = np.arange(len(whale_sel))
    rng.shuffle(order)
    for ti in order:
        if len(deltas) >= max_pairs:
            break
        st = t_stratum[ti]
        bucket = ctrl_pos.get(st)
        if not bucket or ptr[st] >= len(bucket):
            n_unmatched += 1
            continue
        cj = bucket[ptr[st]]
        ptr[st] += 1
        deltas.append(float(t_pnl[ti] - c_pnl_all[cj]))
        clusters.append(t_token[ti])
        days.append(t_day[ti])
        bal_t_size.append(float(t_size[ti]))
        bal_c_size.append(float(c_size[cj]))
        bal_t_depth.append(float(t_depth[ti]))
        bal_c_depth.append(float(c_depth[cj]))
        bal_t_mom.append(float(t_mom[ti]))
        bal_c_mom.append(float(c_mom[cj]))

    delta = np.asarray(deltas, dtype=float)
    boot_tok = paired_delta_bootstrap(delta, np.asarray(clusters), n_boot=n_boot, seed=seed)
    boot_day = paired_delta_bootstrap(delta, np.asarray(days), n_boot=n_boot, seed=seed + 3)

    def _mean(a: list[float]) -> float | None:
        return float(np.mean(a)) if a else None

    return {
        "name": "COVARIATE_MATCHED",
        "n_whale_selected": int(len(whale_sel)),
        "n_control_pool": int(len(non_whale)),
        "n_pairs": int(len(delta)),
        "n_unmatched": int(n_unmatched),
        "match_rate": float(len(delta) / len(whale_sel)) if len(whale_sel) else 0.0,
        "sum_paired_delta": float(delta.sum()) if len(delta) else 0.0,
        "mean_paired_delta": float(delta.mean()) if len(delta) else 0.0,
        "bootstrap_token_cluster_delta": boot_tok,
        "bootstrap_day_cluster_delta": boot_day,
        "paired_delta_p05_token": boot_tok["p05"],
        "paired_delta_p05_day": boot_day["p05"],
        "passes_paired_p05": bool(len(delta) and boot_tok["p05"] > 0 and boot_day["p05"] > 0),
        "covariate_balance": {
            "treat_mean_size": _mean(bal_t_size),
            "ctrl_mean_size": _mean(bal_c_size),
            "treat_mean_depth": _mean(bal_t_depth),
            "ctrl_mean_depth": _mean(bal_c_depth),
            "treat_mean_mom": _mean(bal_t_mom),
            "ctrl_mean_mom": _mean(bal_c_mom),
            "size_abs_rel_diff": (
                abs(_mean(bal_t_size) - _mean(bal_c_size)) / max(abs(_mean(bal_t_size) or 0), 1e-12)
                if bal_t_size else None
            ),
        },
    }


def decide_incremental_verdict(overlay: dict, matched: dict, legacy: dict) -> dict[str, Any]:
    """Corrected incremental vocabulary."""
    overlay_pass = bool(overlay.get("passes_paired_p05"))
    matched_pass = bool(matched.get("passes_paired_p05"))
    if overlay_pass and matched_pass:
        verdict = "INCREMENTAL_EDGE_CONFIRMED_PAIRED"
    elif overlay_pass or matched_pass:
        verdict = "INCREMENTAL_EDGE_MIXED_PAIRED_EVIDENCE"
    else:
        # Default honest state from the review: invalid legacy + unconfirmed paired
        verdict = "UNCONFIRMED_INVALID_INCREMENTAL_ESTIMAND"

    return {
        "incremental_verdict": verdict,
        "legacy_arithmetic_diff_weth": legacy.get("arithmetic_diff"),
        "legacy_valid": False,
        "overlay_paired_delta_p05_token": overlay.get("paired_delta_p05_token"),
        "matched_paired_delta_p05_token": matched.get("paired_delta_p05_token"),
        "overlay_passes": overlay_pass,
        "matched_passes": matched_pass,
        "note": (
            "Legacy scorecard incremental_vs_pool_only_weth is an invalid estimand. "
            "Decision uses token/day-clustered paired ΔPnL p05."
        ),
    }


def run_incremental(oof: dict[str, pd.DataFrame]) -> dict[str, Any]:
    cfg = load_audit_cfg()
    n_boot = int(cfg.get("bootstrap", {}).get("n_boot", 200))
    seed = int(cfg.get("bootstrap", {}).get("seed", 7))
    max_pairs = int(cfg.get("matching", {}).get("max_pairs", 20000))

    whale = oof["WHALE_EVENT"]
    pool = oof["POOL_ONLY"]
    whale_sel = selected(whale)
    pool_sel = selected(pool)

    legacy = _invalid_legacy_incremental(whale_sel, pool_sel)
    print(
        f"[audit:incremental] legacy invalid diff={legacy['arithmetic_diff']:.4f} "
        f"(whale_n={legacy['whale_n']}, pool_n={legacy['pool_n']})",
        flush=True,
    )
    overlay = within_opportunity_overlay(pool, n_boot=n_boot, seed=seed)
    print(
        f"[audit:incremental] overlay whale_share={overlay.get('whale_share_of_pool')} "
        f"mean_ratio={overlay.get('mean_pnl_ratio')} "
        f"paired_p05_tok={overlay.get('paired_delta_p05_token')}",
        flush=True,
    )
    matched = covariate_matched_control(
        whale, pool, n_boot=n_boot, seed=seed, max_pairs=max_pairs
    )
    print(
        f"[audit:incremental] matched n_pairs={matched.get('n_pairs')} "
        f"match_rate={matched.get('match_rate')} "
        f"paired_p05_tok={matched.get('paired_delta_p05_token')}",
        flush=True,
    )
    decision = decide_incremental_verdict(overlay, matched, legacy)
    report = {
        "task": "incremental_estimand_repair",
        "legacy": legacy,
        "within_opportunity_overlay": overlay,
        "covariate_matched": matched,
        "decision": decision,
    }
    save_audit_json("01_incremental.json", report)
    return report
