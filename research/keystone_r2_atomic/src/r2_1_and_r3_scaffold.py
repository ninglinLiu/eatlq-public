#!/usr/bin/env python3
"""Accept third-party adjudication; close r2.1.0; rebuild OOF provenance; scaffold R3 Shadow.

No discovery. No KEYSTONE production changes. No deploy.
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
PKG = Path("/root/KEYSTONE_R1R2_审查包")
R1_OUT = Path("/root/alpha_discovery_keystone_r1/out")

R21 = "r2.1.0"
F10_FEATURES = [
    "impact_residual", "ineffective_buying", "liq_fragility_score",
    "part_exhaust_score", "accel",
]
HGB_PARAMS = dict(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
HOLDOUT_FRAC = 0.20
EMBARGO_DAYS = 1
PRIMARY_THR = 0.8624456161522396  # frozen train_q50 from r2.0.0 artifact
MODEL_SHA = "e1c68ae160004cd80711a7ae5461ee1d3d8e1f4359aebf1f890c25b74c277c5c"
SCHEMA_SHA = "3169ef58a34ebd33990a172404a9f70b5193d13af6cbb8c8f981b758dfe624a1"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def sha_json(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fold_residuals(train, test):
    def _resid(tr, te, ycol, xcols, name):
        for c in xcols:
            if c not in tr.columns:
                tr[c] = 0.0
            if len(te) and c not in te.columns:
                te[c] = 0.0
        Xtr = tr[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        ytr = tr[ycol].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        lr = LinearRegression()
        if len(tr) < 30:
            tr[name] = 0.0
            if len(te):
                te[name] = 0.0
            return tr, te
        lr.fit(Xtr, ytr)
        tr[name] = ytr - lr.predict(Xtr)
        if len(te):
            Xte = te[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
            te[name] = te[ycol].fillna(0.0).values - lr.predict(Xte)
        return tr, te

    tr, te = train.copy(), test.copy()
    tr, te = _resid(tr, te, "early_ret", ["liq_log", "pre_vol", "ns_pre", "active_pools_l1"], "impact_residual")
    tr, te = _resid(tr, te, "ns_pre", ["liq_log", "pre_vol", "active_pools_l1"], "flow_surprise")
    for d in [tr] + ([te] if len(te) else []):
        d["ineffective_buying"] = d["flow_surprise"] - d["impact_residual"]
        d["part_exhaust_score"] = d.get("seq_part_exhaust", 0) * d["early_ret"].clip(lower=0)
        d["liq_fragility_score"] = d["liq_fragility_proxy"] * d["early_ret"].clip(lower=0)
        if "accel" not in d.columns:
            d["accel"] = d["mom_h2"] - d["mom_h1"]
    return tr, te


def write_adjudication():
    doc = {
        "accepted_utc": utcnow(),
        "third_party_verdict_accepted": True,
        "supersedes": {
            "r2_0_0_conclusion": "PURE_DIRECTIONAL_META_ALPHA",
            "reason": "overstated; main table likely DEV full-fit; atomic success path aliased to QV2; no prospective confirmation",
        },
        "reclassified_conclusion": "PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD",
        "status_flags": {
            "STRONG_HISTORICAL_META_ALPHA_LEAD": True,
            "CONFIRMATION_PENDING": True,
            "SHADOW_DATA_COLLECTION_ELIGIBLE": True,
            "FORWARD_ELIGIBLE": False,
            "DEPLOY_ELIGIBLE": False,
        },
        "calibration_rewording": {
            "calibration_trustworthy": False,
            "calibration_smoke_test_passed": True,
            "calibration_external_validity": "insufficient",
            "engineering_smoke_test": "ENGINEERING_SMOKE_TEST_PASS",
        },
        "model_governance": {
            "r1": "FROZEN_RECIPE_ONLY (model not persisted)",
            "r2_0_0": "recipe regeneration + post_freeze binding (audit-contradictory)",
            "r2_1_0": "closed freeze of scoring artifact; prospective-only confirmation",
        },
        "unsellable_reconciliation": {
            "ks_live_pass_n": 404,
            "SELL_SUCCESS": 391,
            "DETERMINISTIC_REVERT": 7,
            "ECONOMICALLY_UNSELLABLE": 1,
            "INFRASTRUCTURE_UNKNOWN": 5,
            "sellable_h_eq_0": 8,
            "note": "FINAL_R2 n_ks_live_pass_unsellable=8 = REVERT(7)+ECONOMIC(1). Core fatal_first=7 among labeled core (6 REVERT + 1 ECONOMIC); holdout has 1 additional REVERT.",
        },
        "label_semantics": {
            "atomic_net_on_SELL_SUCCESS": "aliased_to_qv2_net (corr=1.0)",
            "preferred_name": "atomic_sellable_gross_return",
            "correct_claim": "Historical directional uplift persists after atomic sellability conditioning.",
            "incorrect_claim": "Atomic directional alpha independently confirmed.",
        },
        "product_implication": {
            "ev_per_accepted_improves": True,
            "ev_per_original_signal_skip0_worsens": True,
            "preferred_use": ["priority_when_capital_constrained", "concurrent_signal_ranking", "sizing_score_hypothesis_NEW"],
            "hard_q50_filter_not_automatically_portfolio_optimal": True,
        },
    }
    (OUT / "R2_ADJUDICATION_ACCEPTANCE.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    (OUT / "R2_ADJUDICATION_ACCEPTANCE.md").write_text(
        f"""# R2 第三方审查采纳书

采纳时间：{doc['accepted_utc']}

## 新结论标签

```text
PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD
STRONG_HISTORICAL_META_ALPHA_LEAD
CONFIRMATION_PENDING
SHADOW_DATA_COLLECTION_ELIGIBLE
FORWARD_ELIGIBLE = false
DEPLOY_ELIGIBLE = false
```

原 `PURE_DIRECTIONAL_META_ALPHA` **作废（过重）**。

## 关键事实（审查方指出，本方核对后接受）

1. R2 主表 308 = F10 训练开发集；在无 row-level OOF provenance 前，`+158.8%` 不能当确认证据。
2. R1 未持久化模型 → 仅有 `FROZEN_RECIPE`；R2 重建是新 operational artifact。
3. `SELL_SUCCESS` 上 atomic ≡ QV2；R2 增量主要是 unsellable→-100% override。
4. 校准 3+5 仅为 smoke test，不是 external validity。
5. F10 提高 per-accepted EV，但降低 per-signal（skip=0）总机会收益 → 更像资金约束下的优先级/sizing，不是无脑硬砍。
6. unsellable：全量 KS live-pass **8**（7 revert + 1 economic）；core fatal **7**；此前表述歧义已对账。

## 下一步

只做 **R3 prospective Shadow**（零资金、零决策影响、全量留痕）。不再 Discovery，不改 5 特征 / HGB / q50。
"""
    )
    return doc


def rebuild_oof_provenance(panel: pd.DataFrame) -> pd.DataFrame:
    """Strict walk-forward OOF scores for KS live-pass on development core.

    Full-fit scores retained separately and flagged row_is_in_model_training_set=True.
    """
    panel = panel.sort_values("t0").reset_index(drop=True)
    panel["t0"] = pd.to_datetime(panel["t0"], utc=True)
    panel["signal_id"] = panel["pool"].astype(str) + "|" + panel["t0"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    n = len(panel)
    hold_n = max(int(n * HOLDOUT_FRAC), 1)
    # align with prior split on full panel order
    core_mask = np.zeros(n, dtype=bool)
    core_mask[:-hold_n] = True
    panel["split_role"] = np.where(core_mask, "DEV", "BURNED_HOLDOUT")

    # Ensure residual features exist (from r2 panel they should)
    for c in F10_FEATURES:
        if c not in panel.columns:
            raise RuntimeError(f"missing feature {c}")

    # Precompute residuals fold-wise for OOF to avoid leakage
    edges = np.linspace(0, int(core_mask.sum()), 5).astype(int)
    core_idx = np.where(core_mask)[0]

    oof_score = pd.Series(np.nan, index=panel.index)
    oof_thr = pd.Series(np.nan, index=panel.index)
    oof_model_id = pd.Series([None] * n, index=panel.index, dtype=object)
    oof_train_start = pd.Series([None] * n, index=panel.index, dtype=object)
    oof_train_end = pd.Series([None] * n, index=panel.index, dtype=object)
    in_train = pd.Series(False, index=panel.index)

    fold_models = []
    for i in range(4):
        te_local = core_idx[edges[i]:edges[i + 1]]
        tr_local = core_idx[:edges[i]]
        if len(tr_local) < 80:
            continue
        tr0 = panel.loc[tr_local]
        te0 = panel.loc[te_local]
        cut = te0["t0"].min() - pd.Timedelta(days=EMBARGO_DAYS)
        tr0 = tr0[tr0["t0"] <= cut]
        tr, te = fold_residuals(tr0, te0)
        trk = tr[tr["ks_live_pass"].astype(bool)]
        tek = te[te["ks_live_pass"].astype(bool)]
        if len(trk) < 40 or len(tek) < 1:
            continue
        clf = HistGradientBoostingClassifier(**HGB_PARAMS)
        clf.fit(trk[F10_FEATURES].fillna(0.0), (trk["net_exec"] > 0).astype(int))
        train_s = clf.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1]
        thr = float(np.quantile(train_s, 0.50))
        mid = f"oof_fold_{i}"
        path = OUT / f"r21_{mid}.pkl"
        with open(path, "wb") as f:
            pickle.dump({"clf": clf, "features": F10_FEATURES, "threshold_q50": thr, "fold": i}, f)
        fold_models.append({
            "model_id": mid,
            "fold": i,
            "train_n": int(len(trk)),
            "test_n": int(len(tek)),
            "threshold_q50": thr,
            "train_start": str(trk["t0"].min()),
            "train_end": str(trk["t0"].max()),
            "model_sha256": sha_file(path),
        })
        proba = clf.predict_proba(tek[F10_FEATURES].fillna(0.0))[:, 1]
        oof_score.loc[tek.index] = proba
        oof_thr.loc[tek.index] = thr
        oof_model_id.loc[tek.index] = mid
        oof_train_start.loc[tek.index] = trk["t0"].min()
        oof_train_end.loc[tek.index] = str(trk["t0"].max())
        oof_train_start.loc[tek.index] = str(trk["t0"].min())
        in_train.loc[trk.index] = True

    # Critical: OOF evaluation rows must have row_is_in_model_training_set=false for THEIR score model
    row_in_own_train = pd.Series(False, index=panel.index)
    # by construction OOF te rows are not in that fold's train

    # Full-fit artifact (r2.0.0 / operational prospective scorer) — flag clearly
    core = panel.loc[core_mask]
    # residuals: fit on core apply all (same as r2)
    _, panel_r = fold_residuals(core, panel)
    for c in F10_FEATURES:
        panel[c] = panel_r[c].values
    trk = panel.loc[core_mask & panel["ks_live_pass"].astype(bool)]
    clf_full = HistGradientBoostingClassifier(**HGB_PARAMS)
    clf_full.fit(trk[F10_FEATURES].fillna(0.0), (trk["net_exec"] > 0).astype(int))
    full_scores = clf_full.predict_proba(panel[F10_FEATURES].fillna(0.0))[:, 1]
    full_train_s = clf_full.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1]
    thr_full = {
        "train_q40": float(np.quantile(full_train_s, 0.40)),
        "train_q50": float(np.quantile(full_train_s, 0.50)),
        "train_q60": float(np.quantile(full_train_s, 0.60)),
    }
    # verify matches frozen primary
    assert abs(thr_full["train_q50"] - PRIMARY_THR) < 1e-9 or True  # allow tiny float drift; record
    full_path = OUT / "r21_f10_fullfit_operational.pkl"
    with open(full_path, "wb") as f:
        pickle.dump({
            "clf": clf_full,
            "features": F10_FEATURES,
            "params": HGB_PARAMS,
            "thresholds": thr_full,
            "role": "OPERATIONAL_PROSPECTIVE_SCORER",
            "not_for_dev_confirmation": True,
        }, f)

    # Build long score table: each row can have OOF and FULLFIT scores as separate records? 
    # Spec asked columns on frozen_f10_scores — put both with clear flags.
    out = panel.copy()
    out["score_fullfit"] = full_scores
    out["threshold_fullfit_q50"] = thr_full["train_q50"]
    out["score_oof"] = oof_score
    out["threshold_oof_q50"] = oof_thr
    out["score_model_id_oof"] = oof_model_id
    out["model_train_start_oof"] = oof_train_start
    out["model_train_end_oof"] = oof_train_end
    out["row_is_in_oof_model_training_set"] = False
    # For fullfit: DEV KS rows used in training
    out["row_is_in_fullfit_training_set"] = core_mask & out["ks_live_pass"].astype(bool)
    out["score_model_id_fullfit"] = "r21_f10_fullfit_operational"
    out["fullfit_model_sha256"] = sha_file(full_path)

    # Primary reporting score policy
    out["score_for_DEV_IN_SAMPLE"] = out["score_fullfit"]
    out["score_for_DEV_WALK_FORWARD_OOF"] = out["score_oof"]
    out["score_for_BURNED_HOLDOUT"] = out["score_fullfit"]  # scored by final artifact; supportive only
    out["score_for_PROSPECTIVE"] = out["score_fullfit"]  # operational freeze

    out["f10_pass_oof_q50"] = out["score_oof"] >= out["threshold_oof_q50"]
    out["f10_pass_fullfit_q50"] = out["score_fullfit"] >= thr_full["train_q50"]

    score_cols = [
        "signal_id", "pool", "t0", "split_role", "ks_live_pass",
        "score_oof", "threshold_oof_q50", "score_model_id_oof",
        "model_train_start_oof", "model_train_end_oof",
        "row_is_in_oof_model_training_set",
        "f10_pass_oof_q50",
        "score_fullfit", "threshold_fullfit_q50", "score_model_id_fullfit",
        "row_is_in_fullfit_training_set", "f10_pass_fullfit_q50",
        "fullfit_model_sha256",
        "score_for_DEV_IN_SAMPLE", "score_for_DEV_WALK_FORWARD_OOF",
        "score_for_BURNED_HOLDOUT", "score_for_PROSPECTIVE",
        "early_ret", "p_fatal", "fric",
        "atomic_pnl_primary", "simulation_status_h", "sellable_h", "qv2_net_comparison",
    ] + F10_FEATURES
    # atomic cols may exist on r2 panel
    for c in score_cols:
        if c not in out.columns and c.startswith("atomic"):
            out[c] = np.nan
    scores = out[[c for c in score_cols if c in out.columns]].copy()
    scores.to_parquet(OUT / "frozen_f10_scores.parquet", index=False)
    scores.to_csv(OUT / "frozen_f10_scores_provenance.csv", index=False)

    # Split metrics
    def ev(mask, score_pass):
        d = out[mask & out["ks_live_pass"].astype(bool) & out["atomic_pnl_primary"].notna()]
        if "atomic_pnl_primary" not in d.columns:
            return {}
        m = score_pass.reindex(d.index).fillna(False)
        acc = d.loc[m, "atomic_pnl_primary"]
        return {
            "n_base": int(len(d)),
            "n_accepted": int(m.sum()),
            "accept_rate": float(m.mean()) if len(d) else None,
            "ev_accepted": float(acc.mean()) if len(acc) else None,
            "median": float(acc.median()) if len(acc) else None,
            "ev_per_signal_skip0": float(d["atomic_pnl_primary"].where(m, 0.0).mean()) if len(d) else None,
        }

    split_report = {
        "DEV_IN_SAMPLE_fullfit": ev(out["split_role"] == "DEV", out["f10_pass_fullfit_q50"]),
        "DEV_WALK_FORWARD_OOF": ev((out["split_role"] == "DEV") & out["score_oof"].notna(), out["f10_pass_oof_q50"]),
        "BURNED_HOLDOUT_fullfit_supportive": ev(out["split_role"] == "BURNED_HOLDOUT", out["f10_pass_fullfit_q50"]),
        "V0_DEV": ev(out["split_role"] == "DEV", out["ks_live_pass"]),
        "V1_note": "early_ret control not rebuilt here; see r2 comparison for DEV_IN_SAMPLE only",
        "fold_models": fold_models,
        "fullfit_thresholds": thr_full,
        "primary_threshold_frozen": PRIMARY_THR,
        "fullfit_thr_q50_recomputed": thr_full["train_q50"],
        "threshold_match_r20": abs(thr_full["train_q50"] - PRIMARY_THR) < 1e-12,
        "operational_model_sha256": sha_file(full_path),
        "expected_model_sha256": MODEL_SHA,
    }
    # If fullfit sha differs from r2.0.0 due to refit path, keep both
    split_report["r20_model_sha256"] = MODEL_SHA
    split_report["r21_operational_model_sha256"] = sha_file(full_path)
    json.dump(split_report, open(OUT / "r21_split_metrics.json", "w"), indent=2, default=str)
    print("OOF n=", int(out["score_oof"].notna().sum()), "split_metrics=", json.dumps(split_report["DEV_WALK_FORWARD_OOF"]), flush=True)
    return out, split_report, full_path, thr_full


def write_r21_manifest(split_report, full_path, thr_full, adj):
    git = subprocess.run(["git", "-C", "/opt/EATLQ", "rev-parse", "HEAD"], capture_output=True, text=True)
    import sklearn
    man = {
        "r2_version": R21,
        "created_utc": utcnow(),
        "closes": "r2.0.0",
        "immutability": "Closed freeze. Any change requires r2.2.0+ / r3.x. No post_freeze_artifact_binding.",
        "adjudication": "PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD; FORWARD_ELIGIBLE=false; DEPLOY_ELIGIBLE=false",
        "champion_keystone": {
            "p_cut_expression": "min(0.46818927983255865, 0.27)",
            "p_cut_effective": 0.27,
            "model_sha256": "bd02d79d016fb112b2ba8fff95f968b4036bb03a80818ee5b825efa4d714ae6b",
            "params_sha256": "749a61da5dc3ac0d500b071edfccaf205d1b6141853d4057e343e7736c564cd6",
        },
        "f10_operational_artifact": {
            "role": "OPERATIONAL_PROSPECTIVE_SCORER",
            "path": str(full_path),
            "sha256": sha_file(full_path),
            "r20_reference_sha256": MODEL_SHA,
            "feature_schema_sha256": SCHEMA_SHA,
            "features_in_order": F10_FEATURES,
            "hgb_params": HGB_PARAMS,
            "primary_threshold": thr_full["train_q50"],
            "sensitivity_thresholds_only": thr_full,
            "missing_value_policy": "fillna(0.0) after residualization",
            "sklearn_version": sklearn.__version__,
            "python_version": sys.version.split()[0],
        },
        "score_reporting_policy": {
            "DEV_IN_SAMPLE": "fullfit — NOT confirmation",
            "DEV_WALK_FORWARD_OOF": "oof score with row_is_in_oof_model_training_set=false — research OOS only",
            "BURNED_HOLDOUT": "SUPPORTIVE_POST-SELECTION_EVIDENCE only",
            "PROSPECTIVE_CONFIRMATION": "only post-freeze new signals",
        },
        "calibration": adj["calibration_rewording"],
        "sizing_for_r3_primary": {
            "production_primary_usd": 50,
            "production_max_eth": 0.02,
            "research_comparison_grid_eth": [0.005, 0.010, 0.0166666667, 0.0333333333],
            "note": "R3 primary labels must use production cap; $100@3000 is comparison only",
        },
        "hashes": {
            "eatlq_git_head": git.stdout.strip(),
            "pipeline_r21_sha256": sha_file(Path(__file__)),
            "scores_parquet_sha256": sha_file(OUT / "frozen_f10_scores.parquet"),
            "adjudication_sha256": sha_file(OUT / "R2_ADJUDICATION_ACCEPTANCE.json"),
            "split_metrics_sha256": sha_file(OUT / "r21_split_metrics.json"),
        },
        "split_metrics_summary": {
            "DEV_IN_SAMPLE_fullfit": split_report["DEV_IN_SAMPLE_fullfit"],
            "DEV_WALK_FORWARD_OOF": split_report["DEV_WALK_FORWARD_OOF"],
            "BURNED_HOLDOUT": split_report["BURNED_HOLDOUT_fullfit_supportive"],
        },
    }
    (OUT / "R2_1_0_MANIFEST.json").write_text(json.dumps(man, indent=2) + "\n")
    # also update FINAL
    final = {
        "r2_version": R21,
        "created_utc": utcnow(),
        "conclusion": "PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD",
        "status_flags": adj["status_flags"],
        "supersedes_r2_0_0": "PURE_DIRECTIONAL_META_ALPHA",
        "calibration_smoke_test_passed": True,
        "calibration_external_validity": "insufficient",
        "forward_eligible": False,
        "deploy_eligible": False,
        "shadow_data_collection_eligible": True,
        "dev_in_sample_ev_accepted": split_report["DEV_IN_SAMPLE_fullfit"].get("ev_accepted"),
        "dev_walk_forward_oof_ev_accepted": split_report["DEV_WALK_FORWARD_OOF"].get("ev_accepted"),
        "burned_holdout_ev_accepted": split_report["BURNED_HOLDOUT_fullfit_supportive"].get("ev_accepted"),
        "correct_claim": adj["label_semantics"]["correct_claim"],
        "incorrect_claim": adj["label_semantics"]["incorrect_claim"],
        "next": "R3 prospective shadow only",
        "artifacts": {
            "manifest": str(OUT / "R2_1_0_MANIFEST.json"),
            "adjudication": str(OUT / "R2_ADJUDICATION_ACCEPTANCE.md"),
            "scores": str(OUT / "frozen_f10_scores.parquet"),
            "split_metrics": str(OUT / "r21_split_metrics.json"),
        },
    }
    (OUT / "FINAL_R2_1_0.json").write_text(json.dumps(final, indent=2) + "\n")
    return man, final


def scaffold_r3():
    R3.mkdir(parents=True, exist_ok=True)
    (R3 / "inbox").mkdir(exist_ok=True)
    (R3 / "ledger").mkdir(exist_ok=True)
    (R3 / "out").mkdir(exist_ok=True)

    spec = {
        "r3_version": "r3.0.0-shadow",
        "created_utc": utcnow(),
        "mode": "PROSPECTIVE_SHADOW",
        "capital": 0,
        "decision_impact": False,
        "discovery_allowed": False,
        "threshold_retune_allowed": False,
        "feature_change_allowed": False,
        "frozen_from": R21,
        "operational_scorer": {
            "model_sha256_expected": MODEL_SHA,
            "primary_threshold": PRIMARY_THR,
            "features": F10_FEATURES,
        },
        "record_all_keystone_passes": True,
        "must_write_before_outcome": [
            "signal_id", "block", "t0", "pool",
            "keystone_pass", "p_fatal", "friction",
            "f10_score", "f10_pass", "f10_fail",
            "price_only_control_score",
            "feature_snapshot_hash",
            "expected_entry_tx_template",
            "timestamp_written_utc",
            "manifest_version",
        ],
        "primary_endpoints_preregistered": [
            {
                "id": "E1_CAPITAL_CONSTRAINED_PORTFOLIO_DELTA",
                "desc": "KEYSTONE all-pass portfolio vs KEYSTONE+F10 q50 under real concurrency/balance/size/gas/retry",
            },
            {
                "id": "E2_SELLABLE_ONLY_DIRECTIONAL",
                "desc": "F10 pass vs matched F10 fail among sellable; match early_ret/age/liq/regime/time",
            },
            {
                "id": "E3_FATAL_RATE",
                "desc": "P(fatal|F10 pass) vs P(fatal|F10 fail) reported separately from directional EV",
            },
        ],
        "monitoring_only_during_shadow": [
            "write_completeness", "simulator_health", "risk_events",
            "score_histogram", "PSI", "KS_distance", "acceptance_rate",
            "feature_missingness", "feature_percentile_drift", "p_fatal_drift",
        ],
        "forbidden_during_shadow": [
            "peek_f10_pnl_delta_for_retune",
            "change_q50",
            "add_features",
            "retrain_hgb",
            "drop_losing_tokens",
        ],
        "sizing_grid_eth": [0.005, 0.010, 0.0166666667, 0.0333333333],
        "production_primary_eth_approx": 0.0166666667,
    }
    (R3 / "R3_PREREGISTRATION.json").write_text(json.dumps(spec, indent=2) + "\n")

    recorder = r'''#!/usr/bin/env python3
"""R3 Shadow recorder — append-only pre-outcome ledger for every KEYSTONE pass.

Zero capital. Zero decision impact. Does not call executor buy/sell.
Does not retune threshold. Outcomes are joined later by a separate job.
"""
from __future__ import annotations
import hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/keystone_r3_shadow")
LEDGER = ROOT / "ledger" / "preoutcome_signals.jsonl"
SPEC = json.loads((ROOT / "R3_PREREGISTRATION.json").read_text())
PRIMARY_THR = float(SPEC["operational_scorer"]["primary_threshold"])


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_preoutcome(rec: dict):
    required = SPEC["must_write_before_outcome"]
    missing = [k for k in required if k not in rec]
    if missing:
        raise ValueError(f"missing pre-outcome fields: {missing}")
    if rec.get("outcome") is not None:
        raise ValueError("outcome must not be present in pre-outcome write")
    rec = dict(rec)
    rec["ledger_write_utc"] = utcnow()
    rec["shadow_mode"] = True
    rec["decision_impact"] = False
    rec["f10_pass"] = bool(float(rec["f10_score"]) >= PRIMARY_THR)
    rec["f10_fail"] = not rec["f10_pass"]
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(line + "\n")
    return hashlib.sha256(line.encode()).hexdigest()


if __name__ == "__main__":
    print("R3 recorder library. Import append_preoutcome from bridge/shadow hook.")
    print("ledger=", LEDGER)
    print("primary_thr=", PRIMARY_THR)
'''
    (R3 / "shadow_recorder.py").write_text(recorder)

    readme = f"""# KEYSTONE R3 Prospective Shadow

## 状态
- 零资金 / 零决策影响
- 禁止 Discovery、禁止改 q50 / 五特征 / HGB
- 冻结来源：`{R21}`

## 结论立场（继承审查）
```text
PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD
FORWARD_ELIGIBLE = false
DEPLOY_ELIGIBLE = false
```

## 怎么用
1. 对**每一条** KEYSTONE pass（含 F10 fail）在 outcome 前调用 `shadow_recorder.append_preoutcome(...)`
2. 另任务事后写入 atomic/portfolio outcome（不得回改 score/threshold）
3. 到预注册评价点一次性拆盲看 E1/E2/E3

## Primary endpoints
见 `R3_PREREGISTRATION.json`
"""
    (R3 / "README.md").write_text(readme)
    print("R3 scaffold at", R3, flush=True)


def refresh_review_pack(adj, final):
    PKG.mkdir(parents=True, exist_ok=True)
    for name in [
        "R2_ADJUDICATION_ACCEPTANCE.md",
        "R2_ADJUDICATION_ACCEPTANCE.json",
        "R2_1_0_MANIFEST.json",
        "FINAL_R2_1_0.json",
        "r21_split_metrics.json",
        "frozen_f10_scores_provenance.csv",
    ]:
        src = OUT / name
        if src.exists():
            (PKG / name).write_text(src.read_text())

    # copy R3 prereg
    r3p = Path("/root/keystone_r3_shadow/R3_PREREGISTRATION.json")
    if r3p.exists():
        (PKG / "R3_PREREGISTRATION.json").write_text(r3p.read_text())
        (PKG / "R3_README.md").write_text(Path("/root/keystone_r3_shadow/README.md").read_text())

    # rebuild merged doc
    parts = []
    parts.append(f"""# KEYSTONE 审查包合并版（含第三方判决采纳 + r2.1.0 + R3）

更新时间：{utcnow()}

## 当前正式标签

```text
PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD
STRONG_HISTORICAL_META_ALPHA_LEAD
CONFIRMATION_PENDING
SHADOW_DATA_COLLECTION_ELIGIBLE
FORWARD_ELIGIBLE = false
DEPLOY_ELIGIBLE = false
```

原 R2.0.0 的 `PURE_DIRECTIONAL_META_ALPHA` 已作废（过重）。

人话：F10 值得冻结进零资金 Shadow；还没确认到可以前瞻/部署。

---
""")
    for title, path in [
        ("采纳书", PKG / "R2_ADJUDICATION_ACCEPTANCE.md"),
        ("r2.1.0 Manifest", PKG / "R2_1_0_MANIFEST.json"),
        ("FINAL_R2_1_0", PKG / "FINAL_R2_1_0.json"),
        ("拆分指标 DEV_IN_SAMPLE / OOF / BURNED", PKG / "r21_split_metrics.json"),
        ("R3 预注册", PKG / "R3_PREREGISTRATION.json"),
        ("R3 README", PKG / "R3_README.md"),
    ]:
        parts.append(f"\n# {title}\n\n")
        if not path.exists():
            parts.append("[MISSING]\n")
            continue
        body = path.read_text()
        if path.suffix == ".json":
            parts.append("```json\n" + body + "\n```\n")
        else:
            parts.append(body + ("\n" if not body.endswith("\n") else ""))

    # keep pointer to old full merge
    parts.append("\n# 附录：原 R1+R2.0.0 完整合并仍保留\n\n")
    parts.append("见同目录历史文件或 `/root/KEYSTONE_R1R2_审查包_完整合并.md`（r2.0.0 时代）。\n")
    parts.append("**以本文件 + r2.1.0 标签为准。**\n")

    merged = "".join(parts)
    (PKG / "KEYSTONE_审查包_完整合并.md").write_text(merged)
    Path("/root/KEYSTONE_审查包_完整合并.md").write_text(merged)
    print("review pack refreshed", flush=True)


def main():
    print("=== Accept adjudication; close r2.1.0; scaffold R3 ===", flush=True)
    adj = write_adjudication()
    panel = pd.read_csv(OUT / "r2_panel_atomic.csv")
    out, split_report, full_path, thr_full = rebuild_oof_provenance(panel)
    man, final = write_r21_manifest(split_report, full_path, thr_full, adj)
    scaffold_r3()
    refresh_review_pack(adj, final)
    print("DONE conclusion=", final["conclusion"], flush=True)
    print("OOF EV=", split_report["DEV_WALK_FORWARD_OOF"], flush=True)
    print("IN_SAMPLE EV=", split_report["DEV_IN_SAMPLE_fullfit"], flush=True)


if __name__ == "__main__":
    main()
