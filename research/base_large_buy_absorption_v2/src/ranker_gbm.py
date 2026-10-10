"""Track C: HistGradientBoosting intraday ranker vs size/random/ridge/lba_score.

TEST paired significance + SEALED replication required for PROVEN.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .arms_v1 import prepare_splits
from .common import OUT, load_freeze, save_json, utcnow
from .ranking import _feat_matrix, _fit_ridge, _predict, _select_topk_per_day, _day_key
from .scheduler import simulate_scheduled
from .scorecard_v1 import paired_scheduled_delta
from .v3_common import V3_OUT, ensure_v3_out, load_v3


def _eval_policy(frame: pd.DataFrame, score_col: str, cap: dict) -> dict:
    d = frame.copy()
    d["_sched_score"] = pd.to_numeric(d.get(score_col, 0), errors="coerce").fillna(0.0)
    fills, monthly, summary = simulate_scheduled(
        d,
        score_col="_sched_score",
        max_concurrent=int(cap["max_concurrent"]),
        one_position_per_token=bool(cap["one_position_per_token"]),
        max_per_token=int(cap["max_per_token"]),
    )
    return {"summary": summary, "fills": fills, "n_selected": int(d["policy_selected"].sum())}


def _walk_forward_scores(
    d: pd.DataFrame,
    elig: pd.Series,
    feat_cols: list[str],
    *,
    model: str,
) -> tuple[pd.DataFrame, list[str], Any]:
    folds = sorted([int(f) for f in d["fold"].dropna().unique() if int(f) >= 0])
    parts = []
    used = feat_cols
    pack: Any = None
    for fold in folds:
        train = d[(d["fold"] == fold) & (d["split"] == "TRAIN") & elig]
        test = d[(d["fold"] == fold) & (d["split"] == "TEST") & elig]
        if test.empty:
            continue
        if train.empty or len(train) < 50:
            sub = d[(d["fold"] == fold) & elig]
            if sub.empty:
                continue
            cut = sub["decision_time"].quantile(0.7)
            train = sub[sub["decision_time"] <= cut]
            test = sub[sub["decision_time"] > cut]
        if train.empty or test.empty:
            continue
        Xtr, used = _feat_matrix(train, feat_cols)
        ytr = pd.to_numeric(train["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
        Xte, _ = _feat_matrix(test, used)
        if model == "gbm":
            est = HistGradientBoostingRegressor(
                max_depth=4,
                learning_rate=0.05,
                max_iter=120,
                min_samples_leaf=40,
                l2_regularization=1.0,
                random_state=42,
            )
            est.fit(Xtr, ytr)
            pred = est.predict(Xte)
            pack = est
        else:
            pack = _fit_ridge(Xtr, ytr, lam=1.0)
            pred = _predict(pack, Xte)
        chunk = test.copy()
        chunk["rank_score"] = pred
        parts.append(chunk)
    if not parts:
        sub = d[elig].copy()
        cut = sub["decision_time"].quantile(0.7)
        train, test = sub[sub["decision_time"] <= cut], sub[sub["decision_time"] > cut]
        Xtr, used = _feat_matrix(train, feat_cols)
        ytr = train["wallet_pnl_weth"].fillna(0).to_numpy()
        Xte, _ = _feat_matrix(test, used)
        if model == "gbm":
            est = HistGradientBoostingRegressor(
                max_depth=4, learning_rate=0.05, max_iter=120,
                min_samples_leaf=40, l2_regularization=1.0, random_state=42,
            )
            est.fit(Xtr, ytr)
            test = test.copy()
            test["rank_score"] = est.predict(Xte)
            pack = est
        else:
            pack = _fit_ridge(Xtr, ytr)
            test = test.copy()
            test["rank_score"] = _predict(pack, Xte)
        parts = [test]
    out = pd.concat(parts, ignore_index=True)
    return out, used, pack


def _importance(est, feat_names: list[str]) -> dict[str, float]:
    if est is None or not hasattr(est, "feature_importances_"):
        # HistGBM has no feature_importances_ in older sklearn; use permutation-free proxy
        if hasattr(est, "train_score_"):
            return {f: 0.0 for f in feat_names}
        return {}
    return {f: float(v) for f, v in zip(feat_names, est.feature_importances_)}


def _gbm_importance_proxy(est: HistGradientBoostingRegressor, feat_names: list[str]) -> dict[str, float]:
    """Approximate importance via number of splits per feature from sklearn trees_."""
    try:
        # sklearn >=1.2 exposes _predictors
        counts = np.zeros(len(feat_names))
        predictors = getattr(est, "_predictors", None)
        if predictors is None:
            return {f: 0.0 for f in feat_names}
        for stage in predictors:
            for tree in stage:
                nodes = getattr(tree, "nodes", None)
                if nodes is None:
                    continue
                for node in nodes:
                    # node is structured array with feature_idx
                    fi = int(node["feature_idx"]) if isinstance(node, np.void) or hasattr(node, "__getitem__") else -1
                    if 0 <= fi < len(counts):
                        counts[fi] += 1
        tot = counts.sum() or 1.0
        return {f: float(c / tot) for f, c in zip(feat_names, counts)}
    except Exception:
        return {f: 0.0 for f in feat_names}


def run_ranker_gbm(lba_oof: pd.DataFrame | None = None) -> dict[str, Any]:
    ensure_v3_out()
    v3 = load_v3()
    rcfg = v3["ranker"]
    freeze = load_freeze()
    feat_cols = list(rcfg["features_pit_only"])
    top_k = int(rcfg["top_k_intraday"])
    cap = dict(rcfg["primary_capacity"])

    if lba_oof is None:
        lba_oof = pd.read_parquet(OUT / "oos" / "oof_LARGE_BUY_ABSORPTION.parquet")

    # Full labeled wait panel for sealed + richer features if needed
    # Use OOF frame which already has features + labels; attach sealed from prepare_splits
    d = prepare_splits(lba_oof.copy(), freeze)
    elig = pd.Series(True, index=d.index)
    if "scorable_primary" in d.columns:
        elig &= d["scorable_primary"].astype(bool)
    if "buy_status" in d.columns:
        elig &= d["buy_status"].eq("SUCCESS")
    if "absorbed" in d.columns:
        elig &= pd.to_numeric(d["absorbed"], errors="coerce").fillna(0) > 0
    if "large_buy_score" in d.columns:
        elig &= pd.to_numeric(d["large_buy_score"], errors="coerce").fillna(0) > 0

    print("[v3/C] fitting GBM walk-forward …", flush=True)
    ranked, used, est = _walk_forward_scores(d, elig, feat_cols, model="gbm")
    ranked["policy_selected"] = _select_topk_per_day(ranked, ranked["rank_score"], top_k)
    ranked["rank_policy"] = "HISTGBM_TOPK"
    ranked.to_parquet(V3_OUT / "oof_RANK_GBM.parquet", index=False)

    # Ridge baseline on same splits
    ridge_df, _, _ = _walk_forward_scores(d, elig, feat_cols, model="ridge")
    ridge_df["policy_selected"] = _select_topk_per_day(ridge_df, ridge_df["rank_score"], top_k)

    base = ranked.copy()
    rng = np.random.default_rng(42)

    rand = base.copy()
    days = _day_key(rand["decision_time"])
    sel = pd.Series(False, index=rand.index)
    for _, idx in rand.groupby(days).groups.items():
        idx = list(idx)
        pick = rng.choice(idx, size=min(top_k, len(idx)), replace=False)
        sel.loc[pick] = True
    rand["policy_selected"] = sel
    rand["rank_score"] = rng.random(len(rand))

    by_size = base.copy()
    by_size["rank_score"] = pd.to_numeric(by_size.get("rel_size", 0), errors="coerce").fillna(0)
    by_size["policy_selected"] = _select_topk_per_day(by_size, by_size["rank_score"], top_k)

    by_lba = base.copy()
    by_lba["rank_score"] = pd.to_numeric(by_lba.get("lba_score", 0), errors="coerce").fillna(0)
    by_lba["policy_selected"] = _select_topk_per_day(by_lba, by_lba["rank_score"], top_k)

    m_gbm = _eval_policy(ranked, "rank_score", cap)
    m_ridge = _eval_policy(ridge_df, "rank_score", cap)
    m_rand = _eval_policy(rand, "rank_score", cap)
    m_size = _eval_policy(by_size, "rank_score", cap)
    m_lba = _eval_policy(by_lba, "rank_score", cap)

    pair_vs_rand = paired_scheduled_delta(m_gbm["fills"], m_rand["fills"])
    pair_vs_size = paired_scheduled_delta(m_gbm["fills"], m_size["fills"])
    pair_vs_ridge = paired_scheduled_delta(m_gbm["fills"], m_ridge["fills"])
    pair_vs_lba = paired_scheduled_delta(m_gbm["fills"], m_lba["fills"])

    # SEALED replication: train on all unsealed eligible, score sealed
    if "is_sealed" in d.columns:
        sealed_mask = d["is_sealed"].astype(bool)
    else:
        sealed_mask = d["split"].astype(str).eq("SEALED") if "split" in d.columns else pd.Series(False, index=d.index)
    sealed = d[sealed_mask & elig].copy()
    unsealed = d[(~sealed_mask) & elig].copy()
    sealed_report = {"n_sealed": int(len(sealed)), "available": bool(len(sealed) > 0)}
    sealed_beats_size = False
    if len(sealed) and len(unsealed):
        Xtr, used2 = _feat_matrix(unsealed, feat_cols)
        ytr = pd.to_numeric(unsealed["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
        est_s = HistGradientBoostingRegressor(
            max_depth=4, learning_rate=0.05, max_iter=120,
            min_samples_leaf=40, l2_regularization=1.0, random_state=7,
        )
        est_s.fit(Xtr, ytr)
        Xse, _ = _feat_matrix(sealed, used2)
        sealed = sealed.copy()
        sealed["rank_score"] = est_s.predict(Xse)
        sealed["policy_selected"] = _select_topk_per_day(sealed, sealed["rank_score"], top_k)
        size_s = sealed.copy()
        size_s["rank_score"] = pd.to_numeric(size_s.get("rel_size", 0), errors="coerce").fillna(0)
        size_s["policy_selected"] = _select_topk_per_day(size_s, size_s["rank_score"], top_k)
        m_s = _eval_policy(sealed, "rank_score", cap)
        m_ss = _eval_policy(size_s, "rank_score", cap)
        pair_s = paired_scheduled_delta(m_s["fills"], m_ss["fills"])
        sealed_beats_size = bool(
            pair_s["passes"]
            and m_s["summary"]["scheduled_trading_pnl_weth"] > m_ss["summary"]["scheduled_trading_pnl_weth"]
        )
        sealed_report.update({
            "gbm_sched_weth": m_s["summary"]["scheduled_trading_pnl_weth"],
            "gbm_cum_usd": m_s["summary"]["cum_withdrawn_usd"],
            "size_sched_weth": m_ss["summary"]["scheduled_trading_pnl_weth"],
            "size_cum_usd": m_ss["summary"]["cum_withdrawn_usd"],
            "paired_vs_size": {k: v for k, v in pair_s.items() if k != "bootstrap_day"},
            "beats_size": sealed_beats_size,
        })

    test_beats_size = bool(
        pair_vs_size["passes"]
        and m_gbm["summary"]["scheduled_trading_pnl_weth"] > m_size["summary"]["scheduled_trading_pnl_weth"]
    )
    if test_beats_size and sealed_beats_size:
        verdict = "PROVEN_ON_SEALED"
    elif test_beats_size and not sealed_report.get("available"):
        verdict = "UNCONFIRMED"  # no sealed to replicate
    elif test_beats_size and not sealed_beats_size:
        verdict = "REFUTED"  # failed sealed replication
    elif m_gbm["summary"]["scheduled_trading_pnl_weth"] > m_size["summary"]["scheduled_trading_pnl_weth"]:
        verdict = "UNCONFIRMED"
    else:
        verdict = "REFUTED"

    imp = _gbm_importance_proxy(est, used) if isinstance(est, HistGradientBoostingRegressor) else {}

    def _brief(m):
        return {
            "n_selected": m["n_selected"],
            "n_fills": m["summary"]["n_fills"],
            "skip_rate": m["summary"]["skip_rate"],
            "scheduled_trading_pnl_weth": m["summary"]["scheduled_trading_pnl_weth"],
            "cum_withdrawn_usd": m["summary"]["cum_withdrawn_usd"],
        }

    report = {
        "task": "RANKER_GBM",
        "generated_at": utcnow(),
        "features_used": used,
        "feature_importance_proxy": imp,
        "top_k_intraday": top_k,
        "primary_capacity": cap,
        "n_test_rows": int(len(ranked)),
        "policies": {
            "HISTGBM_TOPK": _brief(m_gbm),
            "RIDGE_TOPK": _brief(m_ridge),
            "RANK_RANDOM": _brief(m_rand),
            "RANK_BY_SIZE": _brief(m_size),
            "RANK_BY_LBA_SCORE": _brief(m_lba),
        },
        "paired_test": {
            "vs_random": {k: v for k, v in pair_vs_rand.items() if "bootstrap" not in k or k == "bootstrap_day"},
            "vs_size": {k: v for k, v in pair_vs_size.items() if "bootstrap" not in k or k == "bootstrap_day"},
            "vs_ridge": {k: v for k, v in pair_vs_ridge.items() if "bootstrap" not in k or k == "bootstrap_day"},
            "vs_lba_score": {k: v for k, v in pair_vs_lba.items() if "bootstrap" not in k or k == "bootstrap_day"},
        },
        "sealed": sealed_report,
        "verdicts": {
            "RANKER_BEATS_SIZE": verdict,
            "RANKER_BEATS_RANDOM": (
                "PROVEN" if pair_vs_rand["passes"] and m_gbm["summary"]["scheduled_trading_pnl_weth"] > m_rand["summary"]["scheduled_trading_pnl_weth"]
                else (
                    "UNCONFIRMED"
                    if m_gbm["summary"]["scheduled_trading_pnl_weth"] > m_rand["summary"]["scheduled_trading_pnl_weth"]
                    else "REFUTED"
                )
            ),
        },
        "note": "PIT features only. SEALED looked only after TEST fit for replication check.",
    }
    # Clean nested bootstrap for JSON
    def _clean(x):
        if isinstance(x, dict):
            return {k: _clean(v) for k, v in x.items()}
        if isinstance(x, (np.floating, np.integer)):
            return float(x)
        return x
    save_json(V3_OUT / "ranking_gbm.json", _clean(report))
    print(f"[v3/C] RANKER_BEATS_SIZE={verdict}", flush=True)
    return report


if __name__ == "__main__":
    run_ranker_gbm()
