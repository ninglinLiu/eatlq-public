#!/usr/bin/env python3
"""C0–C3 controls + G1/G2/G3 formal routes — OOF only on main table."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.tree import DecisionTreeRegressor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import OUT, load_freeze, utcnow  # noqa: E402
from src.features import (  # noqa: E402
    FEATURE_COLS,
    PRICE_ONLY,
    SELLABILITY_ONLY,
    fold_fit_residuals,
)
from src.trial_ledger import Ledger, Trial  # noqa: E402

FEAT = OUT / "features" / "feature_matrix.parquet"
PRED = OUT / "predictions"
METRICS = OUT / "metrics"

HGB_REG = dict(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
HGB_CLF = dict(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
TOP_K = 3
EMBARGO_S = 86400


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d = d[d["split_role"].isin(["LABEL_DESIGN", "DEV", "SEALED_HOLDOUT"])]
    d = d.dropna(subset=["atomic_net_return", "xs_return_pctile"])
    d = d.sort_values("decision_time").reset_index(drop=True)
    return d


def walk_forward_folds(dev: pd.DataFrame, n_folds: int = 5) -> list[tuple[pd.DataFrame, pd.DataFrame, dict]]:
    if dev is None or len(dev) == 0 or "decision_time" not in dev.columns:
        return []
    times = np.sort(dev["decision_time"].unique())
    if len(times) < 4:
        return []
    if len(times) < n_folds + 1:
        n_folds = max(2, min(n_folds, len(times) // 2))
    cuts = np.linspace(0, len(times), n_folds + 1).astype(int)
    folds = []
    for i in range(1, n_folds):
        if cuts[i] <= 0:
            continue
        train_end_t = times[cuts[i] - 1]
        test_start_idx = cuts[i]
        test_end_idx = cuts[i + 1] if i + 1 < len(cuts) else len(times)
        if test_start_idx >= len(times):
            break
        test_start_t = times[test_start_idx]
        test_end_t = times[min(test_end_idx, len(times)) - 1]
        # embargo
        train = dev[dev["decision_time"] <= train_end_t - EMBARGO_S]
        test = dev[(dev["decision_time"] >= test_start_t) & (dev["decision_time"] <= test_end_t)]
        if len(train) < 80 or len(test) < 20:
            continue
        meta = {
            "fold": i,
            "model_train_start": int(train["decision_time"].min()),
            "model_train_end": int(train["decision_time"].max()),
            "test_start": int(test["decision_time"].min()),
            "test_end": int(test["decision_time"].max()),
        }
        folds.append((train, test, meta))
    return folds


def _X(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    return df.reindex(columns=cols).replace([np.inf, -np.inf], np.nan).fillna(0.0).values


def select_topk(scores: pd.Series, decision_time: pd.Series, k: int = TOP_K, thr: float | None = None) -> pd.Series:
    """Top-k within decision_time; if singleton slice, apply score threshold (NO_TRADE capable)."""
    tmp = pd.DataFrame({"s": scores.to_numpy(), "t": decision_time.to_numpy()})
    selected = np.zeros(len(tmp), dtype=bool)
    for _, g in tmp.groupby("t"):
        if len(g) >= 2:
            take = g["s"].nlargest(min(k, len(g))).index.to_numpy()
            selected[take] = True
        else:
            # singleton: require thr if provided else take (legacy)
            i = g.index[0]
            if thr is None:
                selected[i] = True
            else:
                selected[i] = bool(g.loc[i, "s"] >= thr)
    return pd.Series(selected, index=scores.index)


def non_overlapping(df: pd.DataFrame) -> pd.Series:
    """Keep first selected per pool until label_end_time."""
    keep = pd.Series(False, index=df.index)
    last_end: dict[str, int] = {}
    for i, r in df.sort_values("decision_time").iterrows():
        if not r.get("selected", False):
            continue
        pool = r["pool"]
        if pool in last_end and r["decision_time"] < last_end[pool]:
            continue
        keep.loc[i] = True
        last_end[pool] = int(r["label_end_time"])
    return keep


def eval_block(df: pd.DataFrame, H: int) -> dict:
    if df.empty or "selected" not in df.columns:
        return {"n": 0, "ev_accepted": None, "ev_per_opp": None}
    acc = df[df["selected"] & df["atomic_net_return"].notna()]
    n_opp = len(df)
    ev_acc = float(acc["atomic_net_return"].mean()) if len(acc) else None
    # skip=0 EV per original opportunity
    ev_opp = float(df["atomic_net_return"].where(df["selected"], 0.0).mean()) if n_opp else None
    hours = H / 3600.0
    cap_hour = float(acc["atomic_net_return"].mean() / hours) if len(acc) and hours > 0 else None
    # IC
    ics = []
    for _, g in df.groupby("decision_time"):
        if len(g) < 5 or g["score"].isna().all():
            continue
        ics.append(float(g["score"].corr(g["xs_return_pctile"])))
    fatal = float((acc["barrier_event"] == "FATAL_FIRST").mean()) if len(acc) else None
    fail = float(acc["atomic_sell_failure"].mean()) if len(acc) else None
    # top5 concentration
    if len(acc):
        pnl = acc["atomic_net_return"].sort_values(ascending=False)
        tot = pnl.sum()
        top5 = float(pnl.head(5).sum() / tot) if tot != 0 else None
    else:
        top5 = None
    return {
        "n": int(len(df)),
        "n_accepted": int(len(acc)),
        "acceptance_rate": float(len(acc) / n_opp) if n_opp else 0.0,
        "ev_accepted": ev_acc,
        "ev_per_opp": ev_opp,
        "capital_hour_return": cap_hour,
        "rank_ic": float(np.nanmean(ics)) if ics else None,
        "tp_first_rate": float((acc["barrier_event"] == "TP_FIRST").mean()) if len(acc) else None,
        "sl_first_rate": float((acc["barrier_event"] == "SL_FIRST").mean()) if len(acc) else None,
        "fatal_first_rate": fatal,
        "atomic_failure_rate": fail,
        "median_return": float(acc["atomic_net_return"].median()) if len(acc) else None,
        "trimmed_mean": float(acc["atomic_net_return"].sort_values().iloc[int(0.05*len(acc)):int(0.95*len(acc))].mean()) if len(acc) >= 20 else ev_acc,
        "expected_shortfall": float(acc["expected_shortfall"].mean()) if len(acc) and "expected_shortfall" in acc else None,
        "top5_dependency": top5,
    }


def oof_train_predict(dev: pd.DataFrame, cols: list[str], target: str, kind: str, model_id: str) -> pd.DataFrame:
    folds = walk_forward_folds(dev)
    parts = []
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        use_cols = [c for c in cols if c in tr.columns]
        Xtr, Xte = _X(tr, use_cols), _X(te, use_cols)
        ytr = tr[target].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        train_score = np.zeros(len(tr))
        if kind == "reg":
            model = HistGradientBoostingRegressor(**HGB_REG)
            model.fit(Xtr, ytr)
            score = model.predict(Xte)
            train_score = model.predict(Xtr)
        elif kind == "clf":
            model = HistGradientBoostingClassifier(**HGB_CLF)
            yb = (ytr > np.median(ytr)).astype(int)
            if len(np.unique(yb)) < 2:
                score = np.zeros(len(te))
                train_score = np.zeros(len(tr))
            else:
                model.fit(Xtr, yb)
                score = model.predict_proba(Xte)[:, 1]
                train_score = model.predict_proba(Xtr)[:, 1]
        else:
            raise ValueError(kind)
        chunk = te.copy()
        chunk["score"] = score
        parts.append(_finalize_chunk(chunk, train_score, meta, model_id))
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _finalize_chunk(chunk: pd.DataFrame, train_scores: np.ndarray, meta: dict, model_id: str) -> pd.DataFrame:
    thr = float(np.quantile(train_scores, 0.60)) if len(train_scores) else 0.0
    chunk = chunk.copy()
    chunk["fold"] = meta["fold"]
    chunk["model_id"] = model_id
    chunk["model_train_start"] = meta["model_train_start"]
    chunk["model_train_end"] = meta["model_train_end"]
    chunk["row_in_training_set"] = False
    chunk["rank"] = chunk.groupby("decision_time")["score"].rank(ascending=False, method="first")
    chunk["selected"] = select_topk(chunk["score"], chunk["decision_time"], TOP_K, thr=thr).values
    chunk["selected"] = non_overlapping(chunk).values & chunk["selected"].values
    return chunk


def run_c0_random(dev: pd.DataFrame) -> pd.DataFrame:
    folds = walk_forward_folds(dev)
    parts = []
    rng = np.random.default_rng(0)
    for train, test, meta in folds:
        chunk = test.copy()
        train_scores = rng.random(len(train))
        chunk["score"] = rng.random(len(chunk))
        parts.append(_finalize_chunk(chunk, train_scores, meta, "C0_RANDOM"))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def run_c3_momentum(dev: pd.DataFrame) -> pd.DataFrame:
    folds = walk_forward_folds(dev)
    parts = []
    for train, test, meta in folds:
        chunk = test.copy()
        train_scores = train["early_ret"].fillna(0.0).to_numpy()
        chunk["score"] = chunk["early_ret"].fillna(0.0)
        parts.append(_finalize_chunk(chunk, train_scores, meta, "C3_SIMPLE_MOMENTUM"))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def run_g1(dev: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    # baseline linear
    folds = walk_forward_folds(dev)
    parts = []
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        cols = [c for c in FEATURE_COLS if c in tr.columns]
        lr = LinearRegression()
        lr.fit(_X(tr, cols), tr["xs_return_pctile"].fillna(0.5).values)
        chunk = te.copy()
        chunk["score"] = lr.predict(_X(te, cols))
        parts.append(_finalize_chunk(chunk, lr.predict(_X(tr, cols)), meta, "G1_LINEAR_RANK"))
    out["G1_LINEAR_RANK"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    out["G1_HGB_RANK"] = oof_train_predict(dev, FEATURE_COLS, "xs_return_pctile", "reg", "G1_HGB_RANK")
    # sensitivity: tree
    parts = []
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        cols = [c for c in FEATURE_COLS if c in tr.columns]
        tree = DecisionTreeRegressor(max_depth=3, random_state=0)
        tree.fit(_X(tr, cols), tr["xs_return_pctile"].fillna(0.5).values)
        chunk = te.copy()
        chunk["score"] = tree.predict(_X(te, cols))
        parts.append(_finalize_chunk(chunk, tree.predict(_X(tr, cols)), meta, "G1_TREE_SENS"))
    out["G1_TREE_SENS"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return out


def run_g2(dev: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    # baseline discrete hazard: logistic on TP_FIRST
    folds = walk_forward_folds(dev)
    parts = []
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        cols = [c for c in FEATURE_COLS if c in tr.columns]
        y = (tr["barrier_event"] == "TP_FIRST").astype(int).values
        if y.sum() < 5 or y.sum() > len(y) - 5:
            score = np.zeros(len(te))
            p_sl = np.zeros(len(te))
            p_fatal = np.zeros(len(te))
        else:
            clf = LogisticRegression(max_iter=200, random_state=0)
            clf.fit(_X(tr, cols), y)
            score = clf.predict_proba(_X(te, cols))[:, 1]
            # competing proxies
            y_sl = (tr["barrier_event"] == "SL_FIRST").astype(int).values
            y_fa = (tr["barrier_event"] == "FATAL_FIRST").astype(int).values
            p_sl = LogisticRegression(max_iter=200, random_state=1).fit(_X(tr, cols), y_sl).predict_proba(_X(te, cols))[:, 1] if 0 < y_sl.sum() < len(y_sl) else np.zeros(len(te))
            p_fatal = LogisticRegression(max_iter=200, random_state=2).fit(_X(tr, cols), y_fa).predict_proba(_X(te, cols))[:, 1] if 0 < y_fa.sum() < len(y_fa) else np.zeros(len(te))
        chunk = te.copy()
        chunk["p_tp"] = score
        chunk["p_sl"] = p_sl
        chunk["p_fatal"] = p_fatal
        chunk["score"] = score - 0.7 * p_sl - 1.2 * p_fatal  # expected utility proxy
        tr_score = (tr["barrier_event"] == "TP_FIRST").astype(float).values
        parts.append(_finalize_chunk(chunk, tr_score, meta, "G2_HAZARD_BASE"))
    out["G2_HAZARD_BASE"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    # primary boosted competing risk
    parts = []
    freeze = load_freeze()
    tp = float(freeze["primary_barrier_params"]["tp"])
    sl = float(freeze["primary_barrier_params"]["sl"])
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        cols = [c for c in FEATURE_COLS if c in tr.columns]
        Xtr, Xte = _X(tr, cols), _X(te, cols)
        probs = {}
        probs_tr = {}
        for label, key in [("TP_FIRST", "p_tp"), ("SL_FIRST", "p_sl"), ("FATAL_FIRST", "p_fatal"), ("TIMEOUT", "p_timeout")]:
            y = (tr["barrier_event"] == label).astype(int).values
            if 0 < y.sum() < len(y):
                m = HistGradientBoostingClassifier(**HGB_CLF)
                m.fit(Xtr, y)
                probs[key] = m.predict_proba(Xte)[:, 1]
                probs_tr[key] = m.predict_proba(Xtr)[:, 1]
            else:
                probs[key] = np.zeros(len(te))
                probs_tr[key] = np.zeros(len(tr))
        chunk = te.copy()
        for k, v in probs.items():
            chunk[k] = v
        chunk["score"] = chunk["p_tp"] * tp + chunk["p_sl"] * (-sl) + chunk["p_fatal"] * (-1.0)
        tr_eu = probs_tr["p_tp"] * tp + probs_tr["p_sl"] * (-sl) + probs_tr["p_fatal"] * (-1.0)
        parts.append(_finalize_chunk(chunk, tr_eu, meta, "G2_HGB_COMPETING"))
    out["G2_HGB_COMPETING"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    # sensitivity: deeper HGB
    sens_params = dict(max_depth=4, max_iter=100, learning_rate=0.05, random_state=1)
    parts = []
    for train, test, meta in folds:
        tr, te = fold_fit_residuals(train, test)
        cols = [c for c in FEATURE_COLS if c in tr.columns]
        y = (tr["barrier_event"] == "TP_FIRST").astype(int).values
        m = HistGradientBoostingClassifier(**sens_params)
        if 0 < y.sum() < len(y):
            m.fit(_X(tr, cols), y)
            score = m.predict_proba(_X(te, cols))[:, 1]
            tr_score = m.predict_proba(_X(tr, cols))[:, 1]
        else:
            score = np.zeros(len(te))
            tr_score = np.zeros(len(tr))
        chunk = te.copy()
        chunk["score"] = score
        parts.append(_finalize_chunk(chunk, tr_score, meta, "G2_HGB_SENS"))
    out["G2_HGB_SENS"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return out


def _lag_matrix(df: pd.DataFrame, cols: list[str], n: int) -> pd.DataFrame:
    d = df.sort_values(["pool", "decision_time"]).copy()
    for c in cols:
        for lag in range(1, n + 1):
            d[f"{c}_lag{lag}"] = d.groupby("pool")[c].shift(lag)
    return d


def run_g3(dev: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    base_cols = ["early_ret", "pre_vol", "net_flow", "liq_trend", "accel"]
    # lagged HGB for N in {3,6,12} — pick best on first fold train only internally? No — freeze N=6 as primary, N=3 sensitivity
    for n, mid in [(6, "G3_LAGGED_HGB"), (3, "G3_LAGGED_HGB_SENS")]:
        d = _lag_matrix(dev, base_cols, n)
        lag_cols = [f"{c}_lag{i}" for c in base_cols for i in range(1, n + 1)]
        d = d.dropna(subset=lag_cols + ["xs_return_pctile"])
        # many pools have only one decision → lag features empty; require minimum supply
        if len(d) < 200 or d["pool"].nunique() < 30:
            out[mid] = pd.DataFrame()
            continue
        out[mid] = oof_train_predict(d, lag_cols, "xs_return_pctile", "reg", mid)

    # HMM on state snippets N=6
    try:
        from hmmlearn.hmm import GaussianHMM
    except Exception:
        out["G3_HMM"] = pd.DataFrame()
        return out

    folds = walk_forward_folds(dev)
    parts = []
    for train, test, meta in folds:
        tr = _lag_matrix(train, base_cols, 6).dropna(subset=[f"{c}_lag1" for c in base_cols])
        te = _lag_matrix(test, base_cols, 6).dropna(subset=[f"{c}_lag1" for c in base_cols])
        if len(tr) < 100 or len(te) < 10:
            continue
        Xtr = _X(tr, base_cols)
        Xte = _X(te, base_cols)
        hmm = GaussianHMM(n_components=3, covariance_type="diag", n_iter=50, random_state=0)
        try:
            hmm.fit(Xtr)
            states_tr = hmm.predict(Xtr)
            state_ret = {s: float(tr.loc[states_tr == s, "atomic_net_return"].mean()) for s in range(3)}
            states_te = hmm.predict(Xte)
            score = np.array([state_ret.get(s, 0.0) for s in states_te])
            tr_scores = np.array([state_ret.get(s, 0.0) for s in states_tr])
        except Exception:
            score = np.zeros(len(te))
            tr_scores = np.zeros(len(tr))
        chunk = te.copy()
        chunk["score"] = score
        parts.append(_finalize_chunk(chunk, tr_scores, meta, "G3_HMM"))
    out["G3_HMM"] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return out


def stack_layers(rank_df: pd.DataFrame, risk_df: pd.DataFrame | None, model_id: str) -> pd.DataFrame:
    """L1 market gate + L2 rank + L3 sellability/competing risk."""
    d = rank_df.copy()
    # L1: market opportunity gate from train-fold opportunity_supply & breadth
    gates = []
    for fold, g in d.groupby("fold"):
        # use within-fold prior rows? approximate: threshold = train-like median of score>0 days
        # Here: open if opportunity_supply >= fold median
        thr_mg = g["opportunity_supply"].median()
        thr_score = g["score"].quantile(0.50)
        g = g.copy()
        g["market_gate"] = (g["opportunity_supply"] >= thr_mg).astype(float)
        g["market_gate_thr"] = thr_mg
        g["asset_ok"] = g["selected"].astype(bool)
        # sellability from liquidity features
        g["sellability_probability"] = 1.0 / (1.0 + np.exp(-g["liq_trend"].fillna(0)))
        thr_sell = 0.45
        g["sell_ok"] = g["sellability_probability"] > thr_sell
        # expected utility margin
        g["expected_utility"] = g["score"]
        safety = 0.0
        g["eu_ok"] = g["expected_utility"] > safety
        g["selected_final"] = (
            (g["market_gate"] > 0)
            & g["sell_ok"]
            & g["asset_ok"]
            & g["eu_ok"]
        )
        if risk_df is not None and len(risk_df):
            r = risk_df[risk_df["fold"] == fold][["signal_id", "p_fatal", "score"]].rename(columns={"score": "eu_risk"})
            g = g.merge(r, on="signal_id", how="left")
            g["selected_final"] = g["selected_final"] & (g["p_fatal"].fillna(1.0) < 0.35)
        g["selected"] = g["selected_final"]
        g["model_id"] = model_id
        gates.append(g)
    return pd.concat(gates, ignore_index=True) if gates else d


def main():
    PRED.mkdir(parents=True, exist_ok=True)
    METRICS.mkdir(parents=True, exist_ok=True)
    freeze = load_freeze()
    H = int(freeze["primary_horizon_seconds"])
    df = _prep(pd.read_parquet(FEAT))
    # Models train/eval on DEV only for OOF main table; LABEL_DESIGN already consumed
    dev = df[df["split_role"] == "DEV"].copy()
    print(f"[models] DEV rows={len(dev)} pools={dev['pool'].nunique()}", flush=True)

    results = {}
    results["C0_RANDOM"] = run_c0_random(dev)
    results["C1_PRICE_ONLY"] = oof_train_predict(dev, PRICE_ONLY, "xs_return_pctile", "reg", "C1_PRICE_ONLY")
    results["C2_SELLABILITY_ONLY"] = oof_train_predict(dev, SELLABILITY_ONLY, "xs_return_pctile", "reg", "C2_SELLABILITY_ONLY")
    results["C3_SIMPLE_MOMENTUM"] = run_c3_momentum(dev)
    results.update(run_g1(dev))
    g2 = run_g2(dev)
    results.update(g2)
    results.update(run_g3(dev))

    # stacked candidates
    if len(results.get("G1_HGB_RANK", pd.DataFrame())) and len(results.get("G2_HGB_COMPETING", pd.DataFrame())):
        results["STACK_G1G2"] = stack_layers(results["G1_HGB_RANK"], results["G2_HGB_COMPETING"], "STACK_G1G2")
    if len(results.get("G3_HMM", pd.DataFrame())) and len(results.get("G2_HGB_COMPETING", pd.DataFrame())):
        results["STACK_G3G2"] = stack_layers(results["G3_HMM"], results["G2_HGB_COMPETING"], "STACK_G3G2")

    scorecard = []
    ledger = Ledger()
    c1_ev = None
    for mid, pred in results.items():
        if pred is None or pred.empty:
            scorecard.append({"model_id": mid, "n": 0, "ev_accepted": None})
            continue
        # provenance columns
        req = [
            "signal_id", "decision_time", "label_end_time", "fold", "model_id",
            "model_train_start", "model_train_end", "row_in_training_set", "score", "rank", "selected",
        ]
        for c in req:
            if c not in pred.columns:
                pred[c] = np.nan if c != "row_in_training_set" else False
        assert bool((~pred["row_in_training_set"]).all())
        path = PRED / f"oof_{mid}.parquet"
        pred.to_parquet(path, index=False)
        m = eval_block(pred, H)
        m["model_id"] = mid
        m["oof_predictions_path"] = str(path)
        # worst fold
        fold_evs = []
        for f, g in pred.groupby("fold"):
            ev = eval_block(g, H).get("ev_accepted")
            if ev is not None:
                fold_evs.append(ev)
        m["worst_fold"] = float(min(fold_evs)) if fold_evs else None
        # remove top1/top5 stress
        acc = pred[pred["selected"] & pred["atomic_net_return"].notna()].copy()
        if len(acc) > 5:
            drop5 = acc.sort_values("atomic_net_return", ascending=False).iloc[5:]
            m["ev_remove_top5"] = float(drop5["atomic_net_return"].mean())
        else:
            m["ev_remove_top5"] = None
        scorecard.append(m)
        if mid == "C1_PRICE_ONLY":
            c1_ev = m.get("ev_accepted")

    sc = pd.DataFrame(scorecard)
    if c1_ev is not None and "ev_accepted" in sc.columns:
        sc["incremental_ev_vs_c1"] = sc["ev_accepted"] - c1_ev
    sc.to_csv(METRICS / "oof_scorecard.csv", index=False)

    for _, r in sc.iterrows():
        inc = r.get("incremental_ev_vs_c1")
        verdict = "REJECT"
        if r.get("ev_accepted") is not None and inc is not None and inc > 0 and (r.get("worst_fold") or -1) > -0.5:
            verdict = "DISCOVERY_LEAD"
        ledger.add(
            Trial(
                trial_id=str(r["model_id"]),
                family=str(r["model_id"]).split("_")[0],
                stage=1,
                description="OOF walk-forward",
                n_oos=int(r.get("n_accepted")) if pd.notna(r.get("n_accepted")) else 0,
                oof_ev=r.get("ev_accepted"),
                price_only_oof_ev=c1_ev,
                incremental_ev=inc if pd.notna(inc) else None,
                rank_ic=r.get("rank_ic"),
                fatal_rate=r.get("fatal_first_rate"),
                atomic_failure_rate=r.get("atomic_failure_rate"),
                capital_hour_return=r.get("capital_hour_return"),
                worst_fold=r.get("worst_fold"),
                top5_dependency=r.get("top5_dependency"),
                verdict=verdict,
            )
        )
    ledger.save(METRICS / "trial_ledger.csv")
    summary = {"created_utc": utcnow(), "n_models": len(results), "c1_ev": c1_ev, "scorecard_path": str(METRICS / "oof_scorecard.csv")}
    (METRICS / "models_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(sc.to_string(index=False), flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
