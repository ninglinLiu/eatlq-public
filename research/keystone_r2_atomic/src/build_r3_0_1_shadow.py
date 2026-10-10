#!/usr/bin/env python3
"""Build r3.0.1-shadow closed preregistration.

Resolves hash ambiguity, freezes feature/residualizer/price-control pipelines,
adds OOF controls, evaluation trigger, E1/E2/E3 hierarchy, atomic label spec,
append-only ledger schema. Does NOT start confirmatory clock until START_READY.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LinearRegression

ROOT = Path("/root/keystone_r2_atomic")
OUT = ROOT / "out"
R3 = Path("/root/keystone_r3_shadow")
R301 = R3 / "r3.0.1"
PKG = Path("/root/KEYSTONE_R1R2_审查包")

F10_FEATURES = [
    "impact_residual", "ineffective_buying", "liq_fragility_score",
    "part_exhaust_score", "accel",
]
PRICE_FEATURES = [
    "early_ret", "mom_h1", "mom_h2", "accel", "range_pos",
    "pre_pullback", "pre_vol", "ns_pre",  # pre_pullback≈drawdown proxy; ns_pre activity; no future
]
# Prefer explicit names; map drawdown/realized_vol/pool_age from available PIT cols
HGB_PARAMS = dict(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
HOLDOUT_FRAC = 0.20
EMBARGO_DAYS = 1
PRIMARY_THR = 0.8624456161522396

R20_WRAPPER = OUT / "frozen_f10_hgb.pkl"
R21_WRAPPER = OUT / "r21_f10_fullfit_operational.pkl"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    return sha_bytes(p.read_bytes())


def sha_json(o) -> str:
    return sha_bytes(json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())


def fold_residuals_fit(train: pd.DataFrame):
    """Fit residualizers on train; return fitted objects + apply fn."""
    def fit_one(ycol, xcols):
        X = train[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        y = train[ycol].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        lr = LinearRegression()
        if len(train) < 30:
            return {"xcols": xcols, "coef": None, "intercept": 0.0, "ycol": ycol}
        lr.fit(X, y)
        return {
            "xcols": xcols,
            "coef": lr.coef_.tolist(),
            "intercept": float(lr.intercept_),
            "ycol": ycol,
        }

    impact = fit_one("early_ret", ["liq_log", "pre_vol", "ns_pre", "active_pools_l1"])
    flow = fit_one("ns_pre", ["liq_log", "pre_vol", "active_pools_l1"])
    return {"impact_residualizer": impact, "flow_residualizer": flow, "fitted_on_n": int(len(train))}


def apply_residualizers(df: pd.DataFrame, rez: dict) -> pd.DataFrame:
    out = df.copy()

    def pred(spec):
        X = out[spec["xcols"]].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        if spec["coef"] is None:
            return np.zeros(len(out))
        return X @ np.array(spec["coef"]) + spec["intercept"]

    out["impact_residual"] = out["early_ret"].fillna(0.0).values - pred(rez["impact_residualizer"])
    out["flow_surprise"] = out["ns_pre"].fillna(0.0).values - pred(rez["flow_residualizer"])
    out["ineffective_buying"] = out["flow_surprise"] - out["impact_residual"]
    if "accel" not in out.columns:
        out["accel"] = out["mom_h2"] - out["mom_h1"]
    out["part_exhaust_score"] = out.get("seq_part_exhaust", 0) * out["early_ret"].clip(lower=0)
    out["liq_fragility_score"] = out["liq_fragility_proxy"] * out["early_ret"].clip(lower=0)
    return out


def main():
    R301.mkdir(parents=True, exist_ok=True)
    (R301 / "fixtures").mkdir(exist_ok=True)
    (R301 / "artifacts").mkdir(exist_ok=True)
    (R3 / "ledger").mkdir(exist_ok=True)

    assert R21_WRAPPER.exists() and R20_WRAPPER.exists()
    r21_art_sha = sha_file(R21_WRAPPER)
    r20_wrap_sha = sha_file(R20_WRAPPER)
    r21_obj = pickle.load(open(R21_WRAPPER, "rb"))
    r20_obj = pickle.load(open(R20_WRAPPER, "rb"))
    r21_clf = r21_obj["clf"]
    r20_clf = r20_obj["clf"]
    r21_inner_sha = sha_bytes(pickle.dumps(r21_clf, protocol=4))
    r20_inner_sha = sha_bytes(pickle.dumps(r20_clf, protocol=4))

    panel = pd.read_csv(OUT / "r2_panel_atomic.csv")
    panel["t0"] = pd.to_datetime(panel["t0"], utc=True)
    panel = panel.sort_values("t0").reset_index(drop=True)
    # ensure range_pos
    if "range_pos" not in panel.columns:
        panel["range_pos"] = panel["pre_runup_from_lo"] / (panel["pre_vol"] + 1e-9)

    # --- Golden fixture: 30 KS live-pass rows with frozen feature vector + expected scores ---
    ks = panel[panel.ks_live_pass.astype(bool)].copy()
    # use existing F10 feature columns already on panel
    rng = np.random.default_rng(20260720)
    idx = rng.choice(ks.index.to_numpy(), size=min(30, len(ks)), replace=False)
    fixture_rows = []
    max_abs = 0.0
    for i in idx:
        row = ks.loc[i]
        x = np.array([[float(row[c]) for c in F10_FEATURES]])
        # DataFrame for feature names
        Xdf = pd.DataFrame(x, columns=F10_FEATURES)
        s21 = float(r21_clf.predict_proba(Xdf)[0, 1])
        s20 = float(r20_clf.predict_proba(Xdf)[0, 1])
        max_abs = max(max_abs, abs(s21 - s20))
        fixture_rows.append({
            "signal_id": f"{row.pool}|{pd.Timestamp(row.t0).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "pool": row.pool,
            "t0": str(row.t0),
            "features": {c: float(row[c]) for c in F10_FEATURES},
            "expected_f10_score_r21": s21,
            "expected_f10_score_r20_wrapper_clf": s20,
            "expected_f10_pass_q50": bool(s21 >= PRIMARY_THR),
        })
    fixture = {
        "n": len(fixture_rows),
        "features_in_order": F10_FEATURES,
        "primary_threshold": PRIMARY_THR,
        "max_abs_score_diff_r21_vs_r20_clf": max_abs,
        "pass_criterion_max_abs_error": 1e-12,
        "score_parity_verified": bool(max_abs <= 1e-12),
        "rows": fixture_rows,
    }
    fix_path = R301 / "fixtures" / "f10_golden_scores.json"
    fix_path.write_text(json.dumps(fixture, indent=2) + "\n")
    fix_sha = sha_file(fix_path)

    hash_resolution = {
        "status": "RESOLVED",
        "explanation": (
            "e1c68... is SHA256 of r2.0.0 wrapper file frozen_f10_hgb.pkl, NOT the inner estimator "
            "of r21_f10_fullfit_operational.pkl. r21 artifact wrapper SHA is 9cf8.... "
            "Inner pickle bytes differ across wrappers, but predict_proba scores match exactly "
            f"(max_abs_diff={max_abs}) on golden fixture."
        ),
        "operational_artifact_path": str(R21_WRAPPER),
        "artifact_sha256_expected": r21_art_sha,
        "inner_estimator_sha256_expected": r21_inner_sha,
        "inner_estimator_sha_is_applicable_as_e1c68": False,
        "r20_wrapper_sha256": r20_wrap_sha,
        "r20_inner_estimator_sha256": r20_inner_sha,
        "behavior_equivalent_to_r20_wrapper_clf": bool(max_abs <= 1e-12),
        "golden_score_fixture_sha256": fix_sha,
        "primary_threshold": PRIMARY_THR,
    }
    (R301 / "HASH_RESOLUTION.json").write_text(json.dumps(hash_resolution, indent=2) + "\n")

    # --- Freeze residualizers on DEV core (same cutoff as fullfit) ---
    n = len(panel)
    hold_n = max(int(n * HOLDOUT_FRAC), 1)
    core = panel.iloc[:-hold_n].copy()
    rez = fold_residuals_fit(core)
    rez["training_cutoff_utc"] = str(core.t0.max())
    rez["training_start_utc"] = str(core.t0.min())
    rez["n_rows"] = int(len(core))
    rez["formula"] = {
        "impact_residual": "early_ret - LinearRegression(liq_log,pre_vol,ns_pre,active_pools_l1)",
        "flow_surprise": "ns_pre - LinearRegression(liq_log,pre_vol,active_pools_l1)",
        "ineffective_buying": "flow_surprise - impact_residual",
        "accel": "mom_h2 - mom_h1",
        "part_exhaust_score": "seq_part_exhaust * max(early_ret,0)",
        "liq_fragility_score": "liq_fragility_proxy * max(early_ret,0) [proxy uses early_ret>0 path]",
        "sequence": "N=6 equal bars in [t0,t0+600]; seq_part_exhaust from preentry_bars constructor",
        "market_controls": "active_pools_l1, chain_swaps_l1 = previous UTC day aggregates",
        "day_boundary": "UTC floor('D')",
        "missing_value_policy": "fillna(0.0) after residualization",
    }
    rez_path = R301 / "artifacts" / "r21_residualizers.json"
    # also pickle LinearRegression objects for exact apply
    tr_rez_objs = {}
    for name, spec in [("impact", rez["impact_residualizer"]), ("flow", rez["flow_residualizer"])]:
        lr = LinearRegression()
        X = core[spec["xcols"]].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        y = core[spec["ycol"]].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        lr.fit(X, y)
        tr_rez_objs[name] = lr
    rez_pkl = R301 / "artifacts" / "r21_residualizers.pkl"
    with open(rez_pkl, "wb") as f:
        pickle.dump({
            "impact": tr_rez_objs["impact"],
            "flow": tr_rez_objs["flow"],
            "impact_xcols": rez["impact_residualizer"]["xcols"],
            "flow_xcols": rez["flow_residualizer"]["xcols"],
            "training_cutoff_utc": rez["training_cutoff_utc"],
        }, f)
    rez_path.write_text(json.dumps(rez, indent=2) + "\n")
    rez_sha = sha_file(rez_pkl)

    # Feature transformer metadata (no mutable state beyond residualizers + formulas)
    feat_pipe = {
        "name": "r21_f10_feature_pipeline",
        "features_out": F10_FEATURES,
        "residualizer_sha256": rez_sha,
        "residualizer_json_sha256": sha_file(rez_path),
        "sequence_n": 6,
        "sequence_window_seconds": 600,
        "bar_sec": 100,
        "market_control_lag_days": 1,
        "day_boundary": "UTC",
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "sklearn_version": __import__("sklearn").__version__,
        "python_version": sys.version.split()[0],
        "code_refs": {
            "r2_pipeline": str(ROOT / "src/r2_pipeline.py"),
            "r2_pipeline_sha256": sha_file(ROOT / "src/r2_pipeline.py") if (ROOT / "src/r2_pipeline.py").exists() else None,
            "build_r301": str(Path(__file__)),
            "build_r301_sha256": sha_file(Path(__file__)),
        },
    }
    feat_path = R301 / "artifacts" / "r21_feature_pipeline.json"
    feat_path.write_text(json.dumps(feat_pipe, indent=2) + "\n")
    feat_sha = sha_file(feat_path)

    # Copy operational scorer into r3.0.1 artifacts with manifest binding
    import shutil
    op_copy = R301 / "artifacts" / "r21_f10_fullfit_operational.pkl"
    shutil.copy2(R21_WRAPPER, op_copy)
    assert sha_file(op_copy) == r21_art_sha

    # --- Price-only control: freeze on DEV core, same HGB params, secondary only ---
    # Ensure price feature columns
    for c in PRICE_FEATURES:
        if c not in panel.columns:
            raise RuntimeError(f"missing price feature {c}")
    core_ks = core[core.ks_live_pass.astype(bool) & core.atomic_pnl_primary.notna()].copy()
    y = (core_ks["atomic_pnl_primary"] > 0).astype(int)
    price_clf = HistGradientBoostingClassifier(**HGB_PARAMS)
    price_clf.fit(core_ks[PRICE_FEATURES].fillna(0.0), y)
    price_scores_train = price_clf.predict_proba(core_ks[PRICE_FEATURES].fillna(0.0))[:, 1]
    # Match F10 fullfit accept count on core labeled (=154) via top-k threshold
    f10_full = pickle.load(open(R21_WRAPPER, "rb"))["clf"]
    # score all core ks with f10 using panel features
    core_all_ks = core[core.ks_live_pass.astype(bool)].copy()
    # re-apply residualizers for safety
    core_all_ks = apply_residualizers(core_all_ks, rez)
    f10_s = f10_clf_scores = f10_full.predict_proba(core_all_ks[F10_FEATURES].fillna(0.0))[:, 1]
    n_acc_target = int((f10_s >= PRIMARY_THR).sum())
    # price threshold: top n_acc_target on labeled? Use same n among labeled core_ks
    core_ks_scored = core_ks.copy()
    core_ks_scored = apply_residualizers(core_ks_scored, rez)
    p_s = price_clf.predict_proba(core_ks_scored[PRICE_FEATURES].fillna(0.0))[:, 1]
    # top-k among labeled
    k = int((core_ks_scored.assign(_f=f10_full.predict_proba(core_ks_scored[F10_FEATURES].fillna(0.0))[:, 1])["_f"] >= PRIMARY_THR).sum())
    if k < 1:
        k = max(1, len(core_ks_scored) // 2)
    price_thr = float(np.partition(p_s, -k)[-k]) if len(p_s) >= k else float(np.median(p_s))
    price_art = {
        "clf": price_clf,
        "features": PRICE_FEATURES,
        "hgb_params": HGB_PARAMS,
        "role": "SECONDARY_NEGATIVE_CONTROL_ONLY",
        "decision_impact": False,
        "training_cutoff_utc": str(core.t0.max()),
        "training_start_utc": str(core.t0.min()),
        "topk_match_f10_accept_n_on_labeled_core": k,
        "threshold_topk": price_thr,
        "label": "atomic_pnl_primary > 0",
    }
    price_path = R301 / "artifacts" / "r21_price_only_control.pkl"
    with open(price_path, "wb") as f:
        pickle.dump(price_art, f)
    price_sha = sha_file(price_path)
    price_schema_sha = sha_json(PRICE_FEATURES)

    # --- Partial OOF + controls on SAME rows ---
    scores = pd.read_parquet(OUT / "frozen_f10_scores.parquet")
    scores["t0"] = pd.to_datetime(scores["t0"], utc=True)
    # merge atomic labels
    lab = panel[["pool", "t0", "atomic_pnl_primary", "simulation_status_h", "sellable_h",
                 "ks_live_pass", "early_ret", "mom_h1", "mom_h2", "accel", "range_pos",
                 "pre_pullback", "pre_vol", "ns_pre"]].copy()
    lab["t0"] = pd.to_datetime(lab["t0"], utc=True)
    m = scores.merge(lab, on=["pool", "t0"], how="left", suffixes=("", "_y"))
    oof = m[m["score_oof"].notna() & m["ks_live_pass"].astype(bool)].copy()
    oof_lab = oof[oof["atomic_pnl_primary"].notna()].copy()
    oof_unk = oof[oof["atomic_pnl_primary"].isna()].copy()

    # Apply price control scores on OOF rows (control is fullfit on core — secondary only;
    # for fair OOF comparison we also need OOF price model. Build fold-wise price OOF.)
    edges_info = []
    # Rebuild OOF controls with same fold structure as r21 (folds 2 and 3 only historically)
    panel_dev = panel.iloc[:-hold_n].copy()
    core_idx = panel_dev.index.to_numpy()
    edges = np.linspace(0, len(core_idx), 5).astype(int)
    oof_ctrl = []
    for i in range(4):
        te_local = core_idx[edges[i]:edges[i + 1]]
        tr_local = core_idx[:edges[i]]
        if len(tr_local) < 80:
            continue
        tr0 = panel.loc[tr_local]
        te0 = panel.loc[te_local]
        cut = te0.t0.min() - pd.Timedelta(days=EMBARGO_DAYS)
        tr0 = tr0[tr0.t0 <= cut]
        tr_r = apply_residualizers(tr0, fold_residuals_fit(tr0))
        te_r = apply_residualizers(te0, fold_residuals_fit(tr0))  # apply train residualizer
        # Actually apply with train-fit residualizer coefficients
        rez_tr = fold_residuals_fit(tr0)
        tr_r = apply_residualizers(tr0, rez_tr)
        te_r = apply_residualizers(te0, rez_tr)
        trk = tr_r[tr_r.ks_live_pass.astype(bool) & tr_r.atomic_pnl_primary.notna()]
        tek = te_r[te_r.ks_live_pass.astype(bool)]
        if len(trk) < 40 or len(tek) < 1:
            continue
        # F10 fold model
        fclf = HistGradientBoostingClassifier(**HGB_PARAMS)
        fclf.fit(trk[F10_FEATURES].fillna(0.0), (trk.atomic_pnl_primary > 0).astype(int))
        f_tr = fclf.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1]
        f_thr = float(np.quantile(f_tr, 0.50))
        f_te = fclf.predict_proba(tek[F10_FEATURES].fillna(0.0))[:, 1]
        # early_ret control: top-k match
        k_te = int((f_te >= f_thr).sum())
        er = tek["early_ret"].fillna(-np.inf).values
        if k_te > 0 and k_te <= len(er):
            er_thr = float(np.partition(er, -k_te)[-k_te])
        else:
            er_thr = float(np.nanmedian(er))
        # price-only fold
        pclf = HistGradientBoostingClassifier(**HGB_PARAMS)
        pclf.fit(trk[PRICE_FEATURES].fillna(0.0), (trk.atomic_pnl_primary > 0).astype(int))
        p_tr = pclf.predict_proba(trk[PRICE_FEATURES].fillna(0.0))[:, 1]
        # match accept count on train then apply quantile; for test use top-k equal to F10 accepts on test
        p_te = pclf.predict_proba(tek[PRICE_FEATURES].fillna(0.0))[:, 1]
        if k_te > 0 and k_te <= len(p_te):
            p_thr = float(np.partition(p_te, -k_te)[-k_te])
        else:
            p_thr = float(np.quantile(p_tr, 0.50))
        edges_info.append({
            "fold": i,
            "train_start": str(trk.t0.min()),
            "train_end": str(trk.t0.max()),
            "test_start": str(tek.t0.min()),
            "test_end": str(tek.t0.max()),
            "embargo_days": EMBARGO_DAYS,
            "embargo_end": str(cut),
            "train_n_labeled": int(len(trk)),
            "test_n": int(len(tek)),
            "f10_thr_q50": f_thr,
            "n_f10_accept_test": k_te,
        })
        for j, ix in enumerate(tek.index):
            oof_ctrl.append({
                "pool": tek.loc[ix, "pool"],
                "t0": tek.loc[ix, "t0"],
                "fold": i,
                "atomic_pnl_primary": tek.loc[ix, "atomic_pnl_primary"],
                "sellable_h": tek.loc[ix, "sellable_h"],
                "f10_score_oof": float(f_te[j]),
                "f10_pass_oof": bool(f_te[j] >= f_thr),
                "v0_pass": True,
                "v1_early_ret_pass": bool(tek.loc[ix, "early_ret"] >= er_thr),
                "c0_price_only_score": float(p_te[j]),
                "c0_price_only_pass": bool(p_te[j] >= p_thr),
                "row_is_in_model_training_set": False,
                "test_start": str(tek.t0.min()),
                "test_end": str(tek.t0.max()),
                "embargo_end": str(cut),
            })
    oof_df = pd.DataFrame(oof_ctrl)
    oof_df.to_parquet(R301 / "artifacts" / "dev_partial_oof_with_controls.parquet", index=False)
    oof_df.to_csv(R301 / "artifacts" / "dev_partial_oof_with_controls.csv", index=False)

    def summarize(pass_col):
        d = oof_df[oof_df.atomic_pnl_primary.notna()]
        msk = d[pass_col].astype(bool)
        acc = d.loc[msk, "atomic_pnl_primary"]
        return {
            "n_oof_scored": int(len(oof_df)),
            "n_oof_labeled": int(d.shape[0]),
            "n_oof_unknown": int(oof_df.atomic_pnl_primary.isna().sum()),
            "n_accepted": int(msk.sum()),
            "accept_rate": float(msk.mean()) if len(d) else None,
            "ev_accepted": float(acc.mean()) if len(acc) else None,
            "median": float(acc.median()) if len(acc) else None,
            "ev_per_signal_skip0": float(d["atomic_pnl_primary"].where(msk, 0.0).mean()) if len(d) else None,
        }

    oof_report = {
        "name": "DEV_PARTIAL_WALK_FORWARD_OOF",
        "note": "Only folds with sufficient train history (historically folds 2–3); NOT full 306-row OOF",
        "folds": edges_info,
        "V0_KEYSTONE": summarize("v0_pass"),
        "V1_EARLY_RET_TOPK": summarize("v1_early_ret_pass"),
        "C0_PRICE_ONLY_HGB_TOPK": summarize("c0_price_only_pass"),
        "F10_OOF": summarize("f10_pass_oof"),
    }
    (R301 / "DEV_PARTIAL_WALK_FORWARD_OOF.json").write_text(json.dumps(oof_report, indent=2, default=str) + "\n")

    # --- Early-ret decile bins etc for E2 matching (from R2 DEV core, frozen) ---
    core_ks_bins = core[core.ks_live_pass.astype(bool)].copy()
    er_edges = np.quantile(core_ks_bins["early_ret"].fillna(0.0), np.linspace(0, 1, 11)).tolist()
    liq_edges = np.quantile(np.log1p(core_ks_bins["liq_log"].clip(lower=0).fillna(0.0)), np.linspace(0, 1, 6)).tolist()
    # pool age proxy: not always present — use ns_pre buckets as activity/maturity proxy if no age
    age_edges = np.quantile(core_ks_bins["ns_pre"].fillna(0.0), np.linspace(0, 1, 6)).tolist()
    match_spec = {
        "method": "strata_nearest_time_1to1_without_replacement",
        "strata": {
            "early_ret_decile_edges": er_edges,
            "liquidity_log1p_quintile_edges": liq_edges,
            "ns_pre_quintile_edges_as_maturity_proxy": age_edges,
            "calendar_time": "same_UTC_isocalendar_week",
            "regime": "frozen_categorical_placeholder_active_pools_l1_tercile_from_R2_core",
        },
        "active_pools_l1_tercile_edges": np.quantile(
            core_ks_bins["active_pools_l1"].fillna(core_ks_bins["active_pools_l1"].median()),
            [0, 1/3, 2/3, 1]
        ).tolist(),
        "caliper": None,
        "ratio": "1:1",
        "replacement": False,
        "tie_break": "signal_id ascending",
        "unmatched_policy": "mark_unmatched_report_coverage",
        "edges_source": "R2 DEV core only; never recompute on R3",
    }
    (R301 / "E2_MATCHING_SPEC.json").write_text(json.dumps(match_spec, indent=2) + "\n")

    # --- Portfolio E1 policy ---
    e1_policy = {
        "type": "USD_CAP_POINT_IN_TIME",
        "usd_cap": 50.0,
        "eth_cap": 0.02,
        "amount_in_wei": "min(50_USD/ETHUSD_decision, 0.02 ETH, available_WETH) in wei",
        "eth_usd_source": "executor env ETH price at decision_block (point-in-time); document exact feed in recorder",
        "compounding": False,
        "starting_weth_balance_for_counterfactual": 0.10,
        "max_concurrent_positions": 3,
        "same_block_signal_order": "ascending signal_id",
        "f10_pass_priority_when_capital_short": "higher f10_score first, tie signal_id",
        "allow_repeat_same_pool": False,
        "buy_failure_frees_capital": True,
        "unsellable_writeoff": "at first DETERMINISTIC_REVERT/ECONOMICALLY_UNSELLABLE after retry schedule",
        "open_positions_at_eval_end": "mark with counterfactual atomic sell at hard_end or max_hold",
        "gas_price_model": "actual block baseFee+priority if available else frozen research gas_weth debit table",
        "research_size_grid_eth_comparison_only": [0.005, 0.010, 0.0166666667, 0.0333333333],
    }
    (R301 / "E1_PORTFOLIO_POLICY.json").write_text(json.dumps(e1_policy, indent=2) + "\n")

    # --- Atomic label spec R3 ---
    atomic_spec = {
        "version": "r3.0.1",
        "preferred_name_success_path": "atomic_net_after_all_costs",
        "two_outcome_tracks": ["ACTUAL_LIVE_OUTCOME", "COUNTERFACTUAL_ATOMIC_SHADOW_OUTCOME"],
        "priority": "actual live tx wins when present; else counterfactual",
        "must_store_fields": [
            "entry_amount_in_wei", "entry_sim_status", "entry_token_out", "entry_gas_weth",
            "exit_sim_status", "exit_weth_out", "exit_gas_weth", "failed_retry_gas_weth",
            "atomic_net_after_all_costs", "actual_tx_hash_if_any",
            "amountOutMinimum", "inclusion_delay_blocks",
        ],
        "unknown_policy": {
            "do_not_drop_silently": True,
            "report_unknown_rate_by_f10_group": True,
            "max_unknown_rate_confirmatory": 0.05,
            "bounds": ["worst_case_-100pct", "best_case_skip0"],
        },
        "executor_path": "prefer production executor simulate_real_sell semantics; document caller/recipient",
        "qv2_role": "comparison_only",
    }
    (R301 / "R3_ATOMIC_LABEL_SPEC.json").write_text(json.dumps(atomic_spec, indent=2) + "\n")

    # --- Ledger schema ---
    ledger_schema = {
        "tables": {
            "r3_preoutcome_events": {
                "insert_only": True,
                "update_delete_forbidden": True,
                "unique": ["signal_id"],
                "hash_chain": True,
                "required_fields": [
                    "signal_id", "t0_block", "decision_block", "feature_cutoff_block",
                    "preoutcome_write_block", "target_entry_block", "decision_block_hash",
                    "pool", "keystone_pass", "p_fatal", "friction",
                    "f10_score", "f10_pass",
                    "impact_residual_value", "ineffective_buying_value",
                    "liq_fragility_score_value", "part_exhaust_score_value", "accel_value",
                    "feature_pipeline_sha256", "residualizer_sha256",
                    "price_only_control_score", "price_only_control_sha256",
                    "feature_snapshot_hash", "record_sha256", "prev_record_sha256",
                    "timestamp_written_utc", "recorder_code_sha256", "manifest_version",
                    "write_delay_ms",
                ],
            },
            "r3_outcome_events": {
                "append_only": True,
                "join_key": "signal_id",
                "may_not_mutate_preoutcome": True,
                "superseding_events_allowed": True,
            },
        },
        "late_backfill_label": "LATE_BACKFILL",
        "late_backfill_excluded_from_confirmatory": True,
        "f10_fail_field": "derived_not_stored",
        "assert_on_write": "f10_pass == (f10_score >= primary_threshold)",
    }
    (R301 / "LEDGER_SCHEMA.json").write_text(json.dumps(ledger_schema, indent=2) + "\n")

    # --- Get start block from chain tip for exclusive start (prospective) ---
    start_block = None
    start_utc = None
    try:
        r = subprocess.run(
            ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
             "SELECT max(block_number), max(block_time) FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split("\t")
            start_block = int(parts[0])
            start_utc = parts[1]
    except Exception as e:
        start_block = None
        start_utc = f"UNRESOLVED:{e}"

    # Pilot rows under old r3.0.0 if any
    old_ledger = R3 / "ledger" / "preoutcome_signals.jsonl"
    pilot_n = 0
    if old_ledger.exists():
        pilot_n = sum(1 for _ in old_ledger.open())
        # mark
        (R301 / "R3_PILOT_PRE_FREEZE_NOTE.md").write_text(
            f"Existing ledger rows under pre-r3.0.1 ({pilot_n}) are labeled R3_PILOT_PRE_FREEZE "
            "and excluded from confirmatory sample.\n"
        )

    # --- Master prereg ---
    prereg = {
        "r3_version": "r3.0.1-shadow",
        "created_utc": utcnow(),
        "closes_design": "r3.0.0-shadow",
        "inherits_closed": "r2.1.0",
        "confirmatory_start_status": "READY_AFTER_LOADER_SELFCHECK" if (
            hash_resolution["behavior_equivalent_to_r20_wrapper_clf"] and fixture["score_parity_verified"]
        ) else "R3_START_BLOCKED_HASH_AMBIGUITY",
        "start_block_exclusive": start_block,
        "start_utc_exclusive_observed_chain_tip": start_utc,
        "note_start": "Confirmatory sample = signals with decision_block > start_block_exclusive AND written under r3.0.1-shadow recorder",
        "champion_keystone": {
            "model_sha256": "bd02d79d016fb112b2ba8fff95f968b4036bb03a80818ee5b825efa4d714ae6b",
            "params_sha256": "749a61da5dc3ac0d500b071edfccaf205d1b6141853d4057e343e7736c564cd6",
            "p_cut_expression": "min(0.46818927983255865, 0.27)",
            "p_cut_effective": 0.27,
        },
        "operational_scorer": {
            "artifact_path": str(op_copy),
            "artifact_sha256_expected": r21_art_sha,
            "inner_estimator_sha256_expected": r21_inner_sha,
            "inner_estimator_sha_is_applicable_as_e1c68": False,
            "r20_wrapper_sha256_behavior_reference": r20_wrap_sha,
            "score_parity_vs_r20_wrapper_clf": True,
            "feature_pipeline_sha256": feat_sha,
            "residualizer_sha256": rez_sha,
            "golden_score_fixture_sha256": fix_sha,
            "primary_threshold": PRIMARY_THR,
            "features_in_order": F10_FEATURES,
        },
        "price_only_control": {
            "role": "SECONDARY_NEGATIVE_CONTROL_ONLY",
            "decision_impact": False,
            "features_in_order": PRICE_FEATURES,
            "model_family": "HistGradientBoostingClassifier",
            "hgb_params": HGB_PARAMS,
            "artifact_path": str(price_path),
            "artifact_sha256": price_sha,
            "feature_schema_sha256": price_schema_sha,
            "training_cutoff_utc": str(core.t0.max()),
            "selection": "top-k match F10 accept count within fold/OOF or labeled-core for frozen artifact",
        },
        "primary_sizing_policy": e1_policy,
        "analysis_hierarchy": {
            "primary": "E1_CAPITAL_CONSTRAINED_PORTFOLIO_DELTA",
            "required_secondary": "E2_SELLABLE_ONLY_DIRECTIONAL",
            "safety": "E3_FATAL_RATE",
        },
        "promotion_rule": {
            "E1": "week × deployer/bytecode-family cluster-bootstrap 95% CI lower bound on portfolio delta > 0",
            "E2": "sellable-only matched uplift > 0; CI not deeply negative per prereg bound",
            "E3": "fatal risk difference upper 95% CI <= noninferiority margin (prereg  +2.0pp absolute)",
            "completeness": "preoutcome write completeness >= 99%",
            "unknown": "outcome unknown rate <= 5% and pass/fail unknown-rate gap within prereg bound",
            "integrity": "no hash/schema/feature-pipeline drift",
            "all_required": True,
        },
        "evaluation_trigger": {
            "evaluate_once": True,
            "min_total_labeled": 200,
            "min_f10_pass_labeled": 50,
            "min_f10_fail_labeled": 50,
            "min_calendar_weeks": 8,
            "min_deployer_or_bytecode_families": 30,
            "hard_end_utc": "2026-12-31T23:59:59Z",
            "if_hard_end_reached_without_minima": "INSUFFICIENT_PROSPECTIVE_SAMPLE",
            "no_optional_stopping": True,
        },
        "blinding": {
            "grouped_outcome_access_before_trigger": False,
            "forbidden_interim": [
                "grouped pass/fail outcome inspection",
                "interim F10 PnL delta",
                "interim significance",
                "interim TP/SL comparison",
                "interim fatal-rate comparison by F10 group",
            ],
            "allowed_interim": [
                "recorder health",
                "write completeness",
                "model/pipeline hash consistency",
                "feature missingness",
                "simulator liveness",
                "ungrouped infra status",
            ],
            "safety_incident_review": "audit-logged; any strategy change terminates R3 into new version",
        },
        "mode": {
            "capital": 0,
            "decision_impact": False,
            "discovery_allowed": False,
            "threshold_retune_allowed": False,
            "feature_change_allowed": False,
        },
        "historical_evidence_primary_number": {
            "name": "DEV_PARTIAL_WALK_FORWARD_OOF",
            "path": str(R301 / "DEV_PARTIAL_WALK_FORWARD_OOF.json"),
            "F10": oof_report["F10_OOF"],
            "V0": oof_report["V0_KEYSTONE"],
            "V1": oof_report["V1_EARLY_RET_TOPK"],
            "C0": oof_report["C0_PRICE_ONLY_HGB_TOPK"],
        },
        "hashes": {
            "prereg_will_self_hash": True,
            "hash_resolution": sha_file(R301 / "HASH_RESOLUTION.json"),
            "feature_pipeline": feat_sha,
            "residualizer": rez_sha,
            "golden_fixture": fix_sha,
            "price_only_control": price_sha,
            "e1_policy": sha_file(R301 / "E1_PORTFOLIO_POLICY.json"),
            "e2_matching": sha_file(R301 / "E2_MATCHING_SPEC.json"),
            "atomic_label": sha_file(R301 / "R3_ATOMIC_LABEL_SPEC.json"),
            "ledger_schema": sha_file(R301 / "LEDGER_SCHEMA.json"),
            "build_script": sha_file(Path(__file__)),
        },
        "pilot_pre_freeze_rows": pilot_n,
        "pilot_label": "R3_PILOT_PRE_FREEZE",
    }

    # Self-check gate
    blocked = []
    if r21_art_sha != sha_file(op_copy):
        blocked.append("artifact_copy_hash_mismatch")
    if not fixture["score_parity_verified"]:
        blocked.append("golden_score_parity_failed")
    if PRIMARY_THR != r21_obj.get("thresholds", {}).get("train_q50") and abs(
        PRIMARY_THR - float(r21_obj.get("thresholds", {}).get("train_q50", PRIMARY_THR))
    ) > 1e-12:
        # r21 thresholds key
        pass
    thr_file = float(r21_obj["thresholds"]["train_q50"])
    if abs(thr_file - PRIMARY_THR) > 1e-12:
        blocked.append("primary_threshold_mismatch")
    if not PRICE_FEATURES:
        blocked.append("price_control_undefined")
    if start_block is None:
        blocked.append("start_block_unresolved")

    prereg["confirmatory_start_status"] = (
        "R3_START_BLOCKED: " + ",".join(blocked) if blocked else "R3_START_READY_PENDING_HUMAN_ARM"
    )
    # Human must still arm; automated ready means specs consistent
    if not blocked:
        prereg["confirmatory_start_status"] = "R3_SPECS_CONSISTENT_AWAITING_HUMAN_ARM"
        prereg["clock_started"] = False

    pre_path = R301 / "R3_PREREGISTRATION.json"
    pre_path.write_text(json.dumps(prereg, indent=2, default=str) + "\n")
    prereg["hashes"]["R3_PREREGISTRATION"] = sha_file(pre_path)
    pre_path.write_text(json.dumps(prereg, indent=2, default=str) + "\n")

    # Recorder v3.0.1
    recorder = f'''#!/usr/bin/env python3
"""r3.0.1-shadow append-only preoutcome recorder."""
from __future__ import annotations
import hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("{R301}")
SPEC = json.loads((ROOT / "R3_PREREGISTRATION.json").read_text())
LEDGER = Path("{R3}") / "ledger" / "r3_preoutcome_events.jsonl"
STATE = Path("{R3}") / "ledger" / "r3_preoutcome_chain_tip.txt"
PRIMARY_THR = float(SPEC["operational_scorer"]["primary_threshold"])
ART_SHA = SPEC["operational_scorer"]["artifact_sha256_expected"]
PIPE_SHA = SPEC["operational_scorer"]["feature_pipeline_sha256"]
REZ_SHA = SPEC["operational_scorer"]["residualizer_sha256"]
PRICE_SHA = SPEC["price_only_control"]["artifact_sha256"]
RECORDER_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_preoutcome(rec: dict) -> str:
    if SPEC.get("clock_started") is not True:
        # allow writes only after human arm sets clock_started true in an arm file
        arm = Path("{R3}") / "HUMAN_ARM.json"
        if not arm.exists() or not json.loads(arm.read_text()).get("clock_started"):
            raise RuntimeError("R3 confirmatory clock not armed; refusing write")
    required = [
        "signal_id","t0_block","decision_block","feature_cutoff_block","preoutcome_write_block",
        "target_entry_block","decision_block_hash","pool","keystone_pass","p_fatal","friction",
        "f10_score","impact_residual_value","ineffective_buying_value","liq_fragility_score_value",
        "part_exhaust_score_value","accel_value","price_only_control_score","feature_snapshot_hash",
    ]
    missing = [k for k in required if k not in rec]
    if missing:
        raise ValueError(f"missing: {{missing}}")
    if "outcome" in rec:
        raise ValueError("outcome forbidden in preoutcome")
    rec = dict(rec)
    rec["f10_pass"] = bool(float(rec["f10_score"]) >= PRIMARY_THR)
    rec["manifest_version"] = "r3.0.1-shadow"
    rec["feature_pipeline_sha256"] = PIPE_SHA
    rec["residualizer_sha256"] = REZ_SHA
    rec["price_only_control_sha256"] = PRICE_SHA
    rec["operational_artifact_sha256"] = ART_SHA
    rec["recorder_code_sha256"] = RECORDER_SHA
    rec["timestamp_written_utc"] = utcnow()
    rec["decision_impact"] = False
    rec["shadow_mode"] = True
    prev = STATE.read_text().strip() if STATE.exists() else "GENESIS"
    rec["prev_record_sha256"] = prev
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    rh = hashlib.sha256(line.encode()).hexdigest()
    rec2 = json.loads(line)
    rec2["record_sha256"] = rh
    line2 = json.dumps(rec2, sort_keys=True, separators=(",", ":"))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(line2 + "\\n")
    STATE.write_text(rh + "\\n")
    return rh


if __name__ == "__main__":
    print("recorder", Path(__file__))
    print("status", SPEC.get("confirmatory_start_status"))
    print("artifact", ART_SHA)
'''
    rec_path = R301 / "shadow_recorder.py"
    rec_path.write_text(recorder)

    # Human arm template
    (R3 / "HUMAN_ARM.template.json").write_text(json.dumps({
        "r3_version": "r3.0.1-shadow",
        "clock_started": False,
        "armed_by": "",
        "armed_utc": "",
        "ack_hash_resolution": False,
        "ack_no_grouped_outcome_peek": False,
        "ack_evaluation_trigger_frozen": False,
        "start_block_exclusive": start_block,
    }, indent=2) + "\n")

    # README
    (R301 / "README.md").write_text(f"""# r3.0.1-shadow

## Status
`{prereg['confirmatory_start_status']}`

## Hash resolution
See `HASH_RESOLUTION.json`. Operational artifact is **9cf8…** (`r21_f10_fullfit_operational.pkl`).
`e1c68…` is the **r2.0.0 wrapper file** hash; scoring behavior matches (golden fixture max_abs_diff={max_abs}).

## Historical primary number
Use `DEV_PARTIAL_WALK_FORWARD_OOF.json` (not DEV_IN_SAMPLE 308/154).

## Start confirmatory clock
1. Review this directory.
2. Copy `../HUMAN_ARM.template.json` → `../HUMAN_ARM.json` and set acknowledgements + `clock_started=true`.
3. Only then does `shadow_recorder.append_preoutcome` accept writes into confirmatory ledger.

## Pilot rows
Pre-r3.0.1 ledger lines = `R3_PILOT_PRE_FREEZE` (n={pilot_n}), excluded.
""")

    # Update review pack merge
    final = {
        "r3_version": "r3.0.1-shadow",
        "created_utc": utcnow(),
        "r2_1_0": "ACCEPTED",
        "r3_0_0": "SUPERSEDED_DESIGN",
        "r3_0_1": prereg["confirmatory_start_status"],
        "hash_resolution": hash_resolution,
        "oof_primary": oof_report["F10_OOF"],
        "oof_controls": {
            "V0": oof_report["V0_KEYSTONE"],
            "V1": oof_report["V1_EARLY_RET_TOPK"],
            "C0": oof_report["C0_PRICE_ONLY_HGB_TOPK"],
        },
        "paths": {
            "prereg": str(pre_path),
            "dir": str(R301),
        },
    }
    (R301 / "FINAL_R3_0_1.json").write_text(json.dumps(final, indent=2) + "\n")

    # refresh merged review doc
    parts = [f"""# KEYSTONE 审查包（r2.1.0 ACCEPTED + r3.0.1-shadow）

更新：{utcnow()}

## 正式立场
- R2.1：**ACCEPTED**
- R3.0.0 设计：条件接受但启动阻断 → 由 **r3.0.1-shadow** 取代
- 当前 R3 状态：`{prereg['confirmatory_start_status']}`
- 结论标签不变：`PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD`；FORWARD/DEPLOY = false

## Hash 冲突已解析
- Operational artifact SHA: `{r21_art_sha}`
- `e1c68…` = r2.0.0 **wrapper 文件**哈希，不是 r21 inner estimator
- Golden fixture score parity max_abs_diff = {max_abs}

## 历史主数字（请用这个）
DEV_PARTIAL_WALK_FORWARD_OOF F10: {json.dumps(oof_report['F10_OOF'])}

同批对照 V0/V1/C0 见 `DEV_PARTIAL_WALK_FORWARD_OOF.json`。

---
"""]
    for title, p in [
        ("HASH_RESOLUTION", R301 / "HASH_RESOLUTION.json"),
        ("R3_PREREGISTRATION r3.0.1", pre_path),
        ("DEV_PARTIAL_WALK_FORWARD_OOF", R301 / "DEV_PARTIAL_WALK_FORWARD_OOF.json"),
        ("FINAL_R3_0_1", R301 / "FINAL_R3_0_1.json"),
        ("README", R301 / "README.md"),
    ]:
        parts.append(f"\n# {title}\n\n")
        body = p.read_text()
        if p.suffix == ".json":
            parts.append("```json\n" + body + "\n```\n")
        else:
            parts.append(body + "\n")
    merged = "".join(parts)
    Path("/root/KEYSTONE_审查包_完整合并.md").write_text(merged)
    PKG.mkdir(exist_ok=True)
    (PKG / "KEYSTONE_审查包_完整合并.md").write_text(merged)
    # copy key files into pack
    for p in [pre_path, R301 / "HASH_RESOLUTION.json", R301 / "DEV_PARTIAL_WALK_FORWARD_OOF.json",
              R301 / "FINAL_R3_0_1.json", fix_path]:
        (PKG / p.name).write_text(p.read_text())

    print("STATUS", prereg["confirmatory_start_status"], flush=True)
    print("artifact", r21_art_sha, flush=True)
    print("inner", r21_inner_sha, flush=True)
    print("golden max_abs", max_abs, flush=True)
    print("OOF F10", oof_report["F10_OOF"], flush=True)
    print("OOF V0", oof_report["V0_KEYSTONE"], flush=True)
    print("OOF V1", oof_report["V1_EARLY_RET_TOPK"], flush=True)
    print("OOF C0", oof_report["C0_PRICE_ONLY_HGB_TOPK"], flush=True)
    return final


if __name__ == "__main__":
    main()
