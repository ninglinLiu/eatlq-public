"""Task 5: is WHALE_EVENT just LARGE_BUY_IMPACT (size/depth proxy)?"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..arms import prepare_splits, select_arm
from ..common import load_freeze
from .bootstrap_utils import paired_delta_bootstrap
from .data import add_covariate_bins, load_audit_cfg, save_audit_json, selected, whale_flag


def select_large_buy_impact(
    frame: pd.DataFrame,
    *,
    quantile: float,
    top_k: int,
    train_threshold: float | None = None,
) -> tuple[pd.Series, float]:
    """Size/impact only — ignore whale_absorbed and any actor identity."""
    s = pd.to_numeric(frame.get("whale_impact", 0), errors="coerce").fillna(0.0)
    eligible = s > 0
    if "buy_status" in frame.columns:
        eligible &= frame["buy_status"].eq("SUCCESS")
    thr = train_threshold
    if thr is None:
        base = s[eligible]
        thr = float(base.quantile(quantile)) if len(base) else 1e9
    selected_m = eligible & (s >= thr)
    if selected_m.any() and top_k > 0:
        tmp = frame.loc[selected_m, ["decision_time"]].copy()
        tmp["_score"] = s[selected_m]
        tmp = tmp.sort_values(["decision_time", "_score"], ascending=[True, False])
        tmp["_rank"] = tmp.groupby("decision_time").cumcount()
        keep = tmp.index[tmp["_rank"] < top_k]
        selected_m = pd.Series(False, index=frame.index)
        selected_m.loc[keep] = True
    return selected_m, thr


def oof_large_buy(panel: pd.DataFrame) -> pd.DataFrame:
    freeze = load_freeze()
    d = prepare_splits(panel, freeze)
    q = float(freeze["selection"]["score_quantile"])
    top_k = int(freeze["selection"]["top_k_per_decision_window"])
    parts = []
    folds = sorted([f for f in d["fold"].dropna().unique() if int(f) >= 0])
    for fold in folds:
        train = d[(d["fold"] == fold) & (d["split"] == "TRAIN")]
        test = d[(d["fold"] == fold) & (d["split"] == "TEST")]
        if test.empty:
            sub = d[d["fold"] == fold]
            if sub.empty:
                continue
            cut = sub["decision_time"].quantile(0.7)
            train = sub[sub["decision_time"] <= cut]
            test = sub[sub["decision_time"] > cut]
        if test.empty:
            continue
        _, thr = select_large_buy_impact(train if len(train) else test, quantile=q, top_k=top_k)
        sel, _ = select_large_buy_impact(test, quantile=q, top_k=top_k, train_threshold=thr)
        chunk = test.copy()
        chunk["policy_selected"] = sel.astype(bool)
        chunk["arm"] = "LARGE_BUY_IMPACT"
        parts.append(chunk)
    if not parts:
        sel, thr = select_large_buy_impact(d, quantile=q, top_k=top_k)
        d = d.copy()
        d["policy_selected"] = sel
        return d
    return pd.concat(parts, ignore_index=True)


def stratified_hit_ratio(panel: pd.DataFrame, whale_sel_ids: set[str]) -> list[dict]:
    d = add_covariate_bins(panel)
    if "buy_status" in d.columns:
        d = d[d["buy_status"].eq("SUCCESS")]
    d = d.copy()
    d["_whale_sel"] = d["signal_id"].astype(str).isin(whale_sel_ids) if "signal_id" in d.columns else whale_flag(d)
    rows = []
    for (age, depth), g in d.groupby(["age_bucket", "depth_decile"], dropna=False):
        rows.append({
            "age_bucket": str(age),
            "depth_decile": str(depth),
            "n": int(len(g)),
            "whale_hit_rate": float(g["_whale_sel"].mean()) if len(g) else 0.0,
        })
    return rows


def run_proxy_ablation(oof: dict[str, pd.DataFrame], panel: pd.DataFrame) -> dict[str, Any]:
    cfg = load_audit_cfg()
    n_boot = int(cfg.get("bootstrap", {}).get("n_boot", 200))
    seed = int(cfg.get("bootstrap", {}).get("seed", 7))

    whale = selected(oof["WHALE_EVENT"])
    print("[audit:proxy] building LARGE_BUY_IMPACT OOF ...", flush=True)
    lbi = oof_large_buy(panel)
    lbi_sel = selected(lbi)

    whale_ids = set(whale["signal_id"].astype(str)) if "signal_id" in whale.columns else set()
    lbi_ids = set(lbi_sel["signal_id"].astype(str)) if "signal_id" in lbi_sel.columns else set()
    inter = whale_ids & lbi_ids
    union = whale_ids | lbi_ids
    jaccard = len(inter) / len(union) if union else 0.0
    overlap_whale = len(inter) / len(whale_ids) if whale_ids else 0.0
    overlap_lbi = len(inter) / len(lbi_ids) if lbi_ids else 0.0

    w_pnl = float(pd.to_numeric(whale["wallet_pnl_weth"], errors="coerce").fillna(0).sum())
    l_pnl = float(pd.to_numeric(lbi_sel["wallet_pnl_weth"], errors="coerce").fillna(0).sum())
    w_mean = float(pd.to_numeric(whale["wallet_pnl_weth"], errors="coerce").fillna(0).mean()) if len(whale) else 0.0
    l_mean = float(pd.to_numeric(lbi_sel["wallet_pnl_weth"], errors="coerce").fillna(0).mean()) if len(lbi_sel) else 0.0

    # Matched Δ: whale vs LBI on intersection vs whale-only
    # Compare mean pnl of whale-only vs LBI-only with token bootstrap of difference of totals capacity-matched
    only_w = whale[~whale["signal_id"].astype(str).isin(lbi_ids)] if "signal_id" in whale.columns else whale.iloc[0:0]
    only_l = lbi_sel[~lbi_sel["signal_id"].astype(str).isin(whale_ids)] if "signal_id" in lbi_sel.columns else lbi_sel.iloc[0:0]

    # Correlation: whale_score vs whale_impact among pool buys
    pool = oof["POOL_ONLY"]
    corr = None
    if len(pool):
        a = pd.to_numeric(pool.get("whale_score"), errors="coerce")
        b = pd.to_numeric(pool.get("whale_impact"), errors="coerce")
        m = a.notna() & b.notna()
        if m.sum() > 10:
            corr = float(np.corrcoef(a[m], b[m])[0, 1])

    hit_rows = stratified_hit_ratio(panel, whale_ids)

    # Decision rule: if overlap high and PnL/trade similar → rename recommendation
    size_explains = (
        overlap_whale >= 0.85
        and abs(w_mean - l_mean) / max(abs(w_mean), 1e-9) < 0.15
    ) or (corr is not None and corr >= 0.9 and overlap_whale >= 0.7)

    # Note: whale_absorbed filter is the only non-size component; measure its bite
    absorbed_filter_drop = None
    if len(panel):
        impact_pos = pd.to_numeric(panel.get("whale_impact", 0), errors="coerce").fillna(0) > 0
        absorbed = pd.to_numeric(panel.get("whale_absorbed", 0), errors="coerce").fillna(0) > 0
        if "buy_status" in panel.columns:
            ok = panel["buy_status"].eq("SUCCESS")
        else:
            ok = pd.Series(True, index=panel.index)
        n_impact = int((ok & impact_pos).sum())
        n_both = int((ok & impact_pos & absorbed).sum())
        absorbed_filter_drop = 1.0 - (n_both / n_impact) if n_impact else None

    rename = "LARGE_BUY_IMPACT" if size_explains else "KEEP_WHALE_EVENT_LABEL_PENDING"
    recommendation = {
        "size_explains_effect": bool(size_explains),
        "recommended_label": rename,
        "rationale": (
            "WHALE_EVENT features contain no actor identity; whale_impact is relative size vs "
            "prior-30m volume and whale_absorbed is short-horizon non-reversal. High overlap with "
            "LARGE_BUY_IMPACT implies pool-level order-flow, not smart-money identity."
            if size_explains
            else "Absorbed/non-reversal filter or selection mass differs enough that a pure size "
            "label is not yet forced; still not an identity trait."
        ),
        "identity_component_present": False,
        "feature_decomposition": {
            "whale_impact": "quote_weth / prior_30m_pool_volume (PIT)",
            "whale_absorbed": "5m price non-reversal after buy",
            "whale_score": "clip(impact,0,5) * (0.5 + 0.5*absorbed)",
            "actor_fields_used": [],
        },
    }

    report = {
        "task": "whale_proxy_ablation",
        "whale_n": int(len(whale)),
        "large_buy_n": int(len(lbi_sel)),
        "jaccard": jaccard,
        "overlap_fraction_of_whale": overlap_whale,
        "overlap_fraction_of_lbi": overlap_lbi,
        "whale_total_pnl_weth": w_pnl,
        "lbi_total_pnl_weth": l_pnl,
        "whale_mean_pnl": w_mean,
        "lbi_mean_pnl": l_mean,
        "whale_impact_score_corr": corr,
        "n_whale_only": int(len(only_w)),
        "n_lbi_only": int(len(only_l)),
        "absorbed_filter_drop_rate": absorbed_filter_drop,
        "stratified_hit_ratio_age_depth": hit_rows[:80],  # cap size
        "recommendation": recommendation,
        "bootstrap_note": "Absolute PnLs are unconstrained sums; use incremental module for paired Δ.",
        "n_boot_config": n_boot,
        "seed": seed,
    }
    save_audit_json("05_proxy_ablation.json", report)
    print(
        f"[audit:proxy] jaccard={jaccard:.3f} overlap_whale={overlap_whale:.3f} "
        f"corr={corr} rename={rename}",
        flush=True,
    )
    return report
