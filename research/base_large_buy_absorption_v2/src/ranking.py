"""Phase 4: PIT-only intraday ranking under 3-slot scheduler.

Walk-forward: fit linear ranker on TRAIN (ridge on z-scored PIT features
predicting wallet_pnl_weth), apply on TEST, take top-K per calendar day,
compare vs random / size / score baselines with paired dPnL.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .arms_v1 import prepare_splits
from .common import OUT, load_freeze, save_json, utcnow
from .scheduler import simulate_scheduled
from .scorecard_v1 import paired_scheduled_delta


def _zmat(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    sd = np.where(sd < 1e-12, 1.0, sd)
    Z = (X - mu) / sd
    Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
    return Z, mu, sd


def _fit_ridge(X: np.ndarray, y: np.ndarray, lam: float = 1.0) -> np.ndarray:
    Z, mu, sd = _zmat(X)
    y = np.nan_to_num(y, nan=0.0)
    # add bias
    A = np.concatenate([Z, np.ones((len(Z), 1))], axis=1)
    xtx = A.T @ A
    xtx.flat[:: xtx.shape[0] + 1] += lam
    w = np.linalg.solve(xtx, A.T @ y)
    return np.concatenate([mu, sd, w])  # pack


def _predict(pack: np.ndarray, X: np.ndarray) -> np.ndarray:
    n_feat = X.shape[1]
    mu = pack[:n_feat]
    sd = pack[n_feat : 2 * n_feat]
    w = pack[2 * n_feat :]
    Z = (X - mu) / sd
    Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
    A = np.concatenate([Z, np.ones((len(Z), 1))], axis=1)
    return A @ w


def _feat_matrix(df: pd.DataFrame, cols: list[str]) -> tuple[np.ndarray, list[str]]:
    cols = [c for c in cols if c in df.columns]
    X = df.loc[:, cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    return X, cols


def _day_key(decision_time: pd.Series) -> pd.Series:
    return pd.to_datetime(decision_time.astype(int), unit="s", utc=True).dt.strftime("%Y-%m-%d")


def _select_topk_per_day(df: pd.DataFrame, score: pd.Series, k: int) -> pd.Series:
    tmp = df[["decision_time"]].copy()
    tmp["_s"] = score
    tmp["_day"] = _day_key(tmp["decision_time"])
    tmp = tmp.sort_values(["_day", "_s"], ascending=[True, False])
    tmp["_r"] = tmp.groupby("_day").cumcount()
    keep = tmp.index[tmp["_r"] < k]
    out = pd.Series(False, index=df.index)
    out.loc[keep] = True
    return out


def run_intraday_ranking(lba_oof: pd.DataFrame) -> dict[str, Any]:
    freeze = load_freeze()
    feat_cols = list(freeze["ranking"]["features_pit_only"])
    top_k = int(freeze["ranking"]["top_k_intraday"])
    # Use full labeled LBA panel with splits
    d = prepare_splits(lba_oof.copy(), freeze)
    # Eligible base
    elig = pd.Series(True, index=d.index)
    if "scorable_primary" in d.columns:
        elig &= d["scorable_primary"].astype(bool)
    if "buy_status" in d.columns:
        elig &= d["buy_status"].eq("SUCCESS")
    if "absorbed" in d.columns:
        elig &= pd.to_numeric(d["absorbed"], errors="coerce").fillna(0) > 0
    if "large_buy_score" in d.columns:
        elig &= pd.to_numeric(d["large_buy_score"], errors="coerce").fillna(0) > 0

    folds = sorted([int(f) for f in d["fold"].dropna().unique() if int(f) >= 0])
    parts = []
    used_feats = feat_cols
    for fold in folds:
        train = d[(d["fold"] == fold) & (d["split"] == "TRAIN") & elig]
        test = d[(d["fold"] == fold) & (d["split"] == "TEST") & elig]
        if test.empty:
            continue
        if train.empty:
            train = d[(d["fold"] == fold) & elig]
            cut = train["decision_time"].quantile(0.7)
            test = train[train["decision_time"] > cut]
            train = train[train["decision_time"] <= cut]
        if train.empty or test.empty:
            continue
        Xtr, used_feats = _feat_matrix(train, feat_cols)
        ytr = pd.to_numeric(train["wallet_pnl_weth"], errors="coerce").fillna(0).to_numpy()
        pack = _fit_ridge(Xtr, ytr, lam=1.0)
        Xte, _ = _feat_matrix(test, used_feats)
        pred = _predict(pack, Xte)
        chunk = test.copy()
        chunk["rank_score"] = pred
        chunk["policy_selected"] = _select_topk_per_day(chunk, chunk["rank_score"], top_k)
        chunk["rank_policy"] = "PIT_RIDGE_TOPK"
        parts.append(chunk)

    if not parts:
        # fallback: single split on all eligible
        sub = d[elig].copy()
        cut = sub["decision_time"].quantile(0.7)
        train, test = sub[sub["decision_time"] <= cut], sub[sub["decision_time"] > cut]
        Xtr, used_feats = _feat_matrix(train, feat_cols)
        pack = _fit_ridge(Xtr, train["wallet_pnl_weth"].fillna(0).to_numpy())
        Xte, _ = _feat_matrix(test, used_feats)
        test = test.copy()
        test["rank_score"] = _predict(pack, Xte)
        test["policy_selected"] = _select_topk_per_day(test, test["rank_score"], top_k)
        test["rank_policy"] = "PIT_RIDGE_TOPK"
        parts = [test]

    ranked = pd.concat(parts, ignore_index=True)
    ranked.to_parquet(OUT / "oos" / "oof_RANK_PIT.parquet", index=False)

    # Baselines on same TEST rows
    base = ranked.copy()
    n_sel = int(base["policy_selected"].sum())
    rng = np.random.default_rng(42)

    def eval_policy(frame: pd.DataFrame, name: str) -> dict:
        fills, monthly, summary = simulate_scheduled(frame, score_col="rank_score" if "rank_score" in frame.columns else "lba_score")
        return {"name": name, "summary": summary, "fills": fills, "n_selected": int(frame["policy_selected"].sum())}

    # random among eligible test rows, capacity-matched total selections roughly by day topk
    rand = base.copy()
    days = _day_key(rand["decision_time"])
    sel = pd.Series(False, index=rand.index)
    for day, idx in rand.groupby(days).groups.items():
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

    m_rank = eval_policy(ranked, "PIT_RIDGE_TOPK")
    m_rand = eval_policy(rand, "RANK_RANDOM")
    m_size = eval_policy(by_size, "RANK_BY_SIZE")
    m_lba = eval_policy(by_lba, "RANK_BY_LBA_SCORE")

    pair_vs_rand = paired_scheduled_delta(m_rank["fills"], m_rand["fills"])
    pair_vs_size = paired_scheduled_delta(m_rank["fills"], m_size["fills"])
    pair_vs_lba = paired_scheduled_delta(m_rank["fills"], m_lba["fills"])

    report = {
        "task": "INTRADAY_RANKING",
        "generated_at": utcnow(),
        "features_used": used_feats,
        "top_k_intraday": top_k,
        "n_test_rows": int(len(ranked)),
        "policies": {
            k: {
                "n_selected": v["n_selected"],
                "n_fills": v["summary"]["n_fills"],
                "skip_rate": v["summary"]["skip_rate"],
                "scheduled_trading_pnl_weth": v["summary"]["scheduled_trading_pnl_weth"],
                "cum_withdrawn_usd": v["summary"]["cum_withdrawn_usd"],
            }
            for k, v in {
                "PIT_RIDGE_TOPK": m_rank,
                "RANK_RANDOM": m_rand,
                "RANK_BY_SIZE": m_size,
                "RANK_BY_LBA_SCORE": m_lba,
            }.items()
        },
        "paired": {
            "vs_random": pair_vs_rand,
            "vs_size": pair_vs_size,
            "vs_lba_score": pair_vs_lba,
        },
        "verdicts": {
            "STRUCTURED_RANKING_BEATS_RANDOM": (
                "PROVEN" if pair_vs_rand["passes"] and m_rank["summary"]["scheduled_trading_pnl_weth"] > m_rand["summary"]["scheduled_trading_pnl_weth"]
                else (
                    "UNCONFIRMED"
                    if m_rank["summary"]["scheduled_trading_pnl_weth"] > m_rand["summary"]["scheduled_trading_pnl_weth"]
                    else "REFUTED"
                )
            ),
            "STRUCTURED_RANKING_BEATS_SIZE": (
                "PROVEN" if pair_vs_size["passes"] else (
                    "UNCONFIRMED"
                    if m_rank["summary"]["scheduled_trading_pnl_weth"] > m_size["summary"]["scheduled_trading_pnl_weth"]
                    else "REFUTED"
                )
            ),
        },
        "note": "Features are PIT-only; absorbed is an event label known at signal_t (eligibility), not a future path feature.",
    }
    # strip non-json from nested
    def _clean(x):
        if isinstance(x, dict):
            return {k: _clean(v) for k, v in x.items() if k != "fills"}
        return x
    save_json(OUT / "ranking.json", _clean(report))
    return report
