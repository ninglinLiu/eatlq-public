"""Phase 2: OOF provenance audit — fail closed on in-sample leakage."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out"
OUT_V1 = ROOT / "out_v1"

REQUIRED = [
    "signal_id", "decision_time", "pool", "fold",
    "model_train_start", "model_train_end", "score", "selected", "label_end_time",
    "row_in_training_set",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_one(path: Path, model_id: str) -> tuple[pd.DataFrame, dict]:
    df = pd.read_parquet(path)
    issues = []
    for c in REQUIRED:
        if c not in df.columns:
            issues.append(f"missing_column:{c}")
    if issues:
        return df, {"model_id": model_id, "ok": False, "issues": issues}

    if bool(df["row_in_training_set"].astype(bool).any()):
        issues.append("row_in_training_set_true_present")

    # decision must be after model_train_end (embargo already applied in training cut)
    bad_time = df["decision_time"] < df["model_train_end"]
    n_bad = int(bad_time.sum())
    if n_bad:
        issues.append(f"decision_time_before_train_end:{n_bad}")

    # fold must be present and finite
    if df["fold"].isna().any():
        issues.append("fold_na")

    out = df[[
        "signal_id", "decision_time", "pool", "fold",
        "model_train_start", "model_train_end", "score", "selected", "label_end_time",
        "row_in_training_set",
    ]].copy()
    out["model_id"] = model_id
    out = out.rename(columns={"selected": "selection"})
    return out, {
        "model_id": model_id,
        "ok": len(issues) == 0,
        "issues": issues,
        "n_rows": int(len(df)),
        "n_selected": int(df["selected"].sum()),
        "all_row_in_training_set_false": bool((~df["row_in_training_set"].astype(bool)).all()),
    }


def run_audit() -> dict:
    OUT_V1.mkdir(parents=True, exist_ok=True)
    parts = []
    reports = []
    targets = {
        "C3_SIMPLE_MOMENTUM": OUT / "predictions" / "oof_C3_SIMPLE_MOMENTUM.parquet",
        "G2_HGB_COMPETING": OUT / "predictions" / "oof_G2_HGB_COMPETING.parquet",
        "C1_PRICE_ONLY": OUT / "predictions" / "oof_C1_PRICE_ONLY.parquet",
    }
    for mid, path in targets.items():
        tab, rep = audit_one(path, mid)
        reports.append(rep)
        if rep["ok"]:
            parts.append(tab)

    failed = [r for r in reports if not r["ok"]]
    if failed:
        result = {
            "created_utc": utcnow(),
            "status": "OOF_PROVENANCE_FAILURE",
            "reports": reports,
        }
        (OUT_V1 / "OOF_PROVENANCE_FAILURE.json").write_text(json.dumps(result, indent=2) + "\n")
        print("OOF_PROVENANCE_FAILURE", flush=True)
        return result

    prov = pd.concat(parts, ignore_index=True)
    prov.to_csv(OUT_V1 / "OOF_PROVENANCE.csv", index=False)
    result = {
        "created_utc": utcnow(),
        "status": "OOF_PROVENANCE_OK",
        "reports": reports,
        "path": str(OUT_V1 / "OOF_PROVENANCE.csv"),
    }
    (OUT_V1 / "OOF_PROVENANCE_AUDIT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == "__main__":
    r = run_audit()
    if r.get("status") == "OOF_PROVENANCE_FAILURE":
        raise SystemExit(2)
