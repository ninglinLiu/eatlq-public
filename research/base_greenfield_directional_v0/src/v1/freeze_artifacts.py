"""Phase 1: freeze existing discovery artifacts by SHA256 (read-only)."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/base_greenfield_directional_v0")
OUT_V1 = ROOT / "out_v1"
OUT = ROOT / "out"


def sha256_file(p: Path) -> str | None:
    if not p.exists():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freeze() -> dict:
    OUT_V1.mkdir(parents=True, exist_ok=True)
    files = {
        "final_greenfield": OUT / "FINAL_GREENFIELD.json",
        "oof_scorecard": OUT / "metrics" / "oof_scorecard.csv",
        "design_freeze": ROOT / "configs" / "design_freeze.json",
        "hard_bans": ROOT / "configs" / "hard_bans.json",
        "oof_c3": OUT / "predictions" / "oof_C3_SIMPLE_MOMENTUM.parquet",
        "oof_g2": OUT / "predictions" / "oof_G2_HGB_COMPETING.parquet",
        "oof_c1": OUT / "predictions" / "oof_C1_PRICE_ONLY.parquet",
        "feature_matrix": OUT / "features" / "feature_matrix.parquet",
        "feature_meta": OUT / "features" / "feature_meta.json",
        "labels_full": OUT / "labels" / "labels_full.parquet",
        "sealed_holdout_labels": OUT / "labels" / "sealed_holdout_labels.parquet",
        "panel_15_60m": OUT / "panel" / "greenfield_panel_15-60m.parquet",
        "panel_manifest": OUT / "panel" / "manifest.json",
        "src_models_routes": ROOT / "src" / "models_routes.py",
        "src_features": ROOT / "src" / "features.py",
        "src_build_labels": ROOT / "src" / "build_labels.py",
        "src_atomic_sim": ROOT / "src" / "atomic_sim.py",
        "src_common": ROOT / "src" / "common.py",
        "charter": ROOT / "CHARTER.md",
    }
    hashes = {k: sha256_file(v) for k, v in files.items()}

    # Exact C3 formula freeze (from discovery code — do not modify)
    c3_formula = {
        "name": "C3_SIMPLE_MOMENTUM",
        "feature": "early_ret",
        "score_formula": "score = early_ret",
        "selection_policy": "top-k=3 within decision_time if n>=2; else score >= train_fold_q60(early_ret); then non-overlapping per pool until label_end_time",
        "threshold_rule": "train_fold quantile 0.60 of early_ret",
        "source_file": "src/models_routes.py::run_c3_momentum",
        "source_sha256": hashes["src_models_routes"],
    }
    g2_spec = {
        "name": "G2_HGB_COMPETING",
        "features": json.loads((OUT / "features" / "feature_meta.json").read_text())["feature_cols"],
        "preprocessing": "fold_fit_residuals on prior train fold only (market_adj_ret, flow_surprise, path_curvature, vol_depth, part_concentration, flow_persistence)",
        "model": "HistGradientBoostingClassifier one-vs-rest for TP_FIRST/SL_FIRST/FATAL_FIRST/TIMEOUT; EU = p_tp*0.5 + p_sl*(-0.35) + p_fatal*(-1.0)",
        "hgb_params": {"max_depth": 3, "max_iter": 80, "learning_rate": 0.05, "random_state": 0},
        "selection_policy": "top-k=3 or score>=train_fold_q60(EU); non-overlapping; original stack also used p_fatal<0.35 for STACK only — P1 uses G2 selected as frozen in oof parquet",
        "threshold_rule": "train_fold quantile 0.60 of EU score",
        "horizon_s": 3600,
        "barrier": "TB_A",
        "barrier_params": {"tp": 0.5, "sl": 0.35},
        "source_file": "src/models_routes.py::run_g2",
        "source_sha256": hashes["src_models_routes"],
        "oof_predictions_sha256": hashes["oof_g2"],
    }

    freeze_doc = load_design()
    holdout_bounds = holdout_time_bounds()

    py_versions = {
        "python": sys.version,
        "platform": platform.platform(),
        "pandas": _ver("pandas"),
        "numpy": _ver("numpy"),
        "sklearn": _ver("sklearn"),
        "web3": _ver("web3"),
    }

    doc = {
        "created_utc": utcnow(),
        "role": "Base Greenfield V1 Profit Validation — artifact freeze",
        "immutability": "Discovery artifacts under out/ and configs/ must not be modified by V1",
        "file_sha256": hashes,
        "c3_exact": c3_formula,
        "g2_exact": g2_spec,
        "label_policy": {
            "H": freeze_doc.get("primary_horizon_seconds"),
            "barrier": freeze_doc.get("primary_barrier"),
            "barrier_params": freeze_doc.get("primary_barrier_params"),
        },
        "universe": freeze_doc.get("universe"),
        "splits": freeze_doc.get("splits"),
        "sealed_holdout_bounds": holdout_bounds,
        "python_sklearn_versions": py_versions,
        "c3_formula_sha256": sha256_text(json.dumps(c3_formula, sort_keys=True)),
        "g2_spec_sha256": sha256_text(json.dumps(g2_spec, sort_keys=True)),
    }
    path = OUT_V1 / "ARTIFACT_FREEZE.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {path}", flush=True)
    return doc


def load_design() -> dict:
    return json.loads((ROOT / "configs" / "design_freeze.json").read_text())


def holdout_time_bounds() -> dict:
    import pandas as pd

    lab = pd.read_parquet(OUT / "labels" / "labels_full.parquet")
    h = lab[lab["split_role"] == "SEALED_HOLDOUT"]
    return {
        "n_rows": int(len(h)),
        "decision_time_min": int(h["decision_time"].min()) if len(h) else None,
        "decision_time_max": int(h["decision_time"].max()) if len(h) else None,
        "n_pools": int(h["pool"].nunique()) if len(h) else 0,
    }


def _ver(mod: str) -> str:
    try:
        m = __import__(mod)
        return getattr(m, "__version__", "unknown")
    except Exception as e:
        return f"ERR:{e}"


if __name__ == "__main__":
    freeze()
