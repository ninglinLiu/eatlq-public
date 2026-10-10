"""Re-seal splits with salted hash; burn V3 confirmation rows from V4 confirmation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")


def load_v4_freeze() -> dict:
    return json.loads((ROOT / "configs" / "v4_orthogonal_freeze.json").read_text())


def load_v4_registry() -> dict:
    return json.loads((ROOT / "configs" / "v4_hypothesis_registry.json").read_text())


def _u01(signal_id: str, salt: str) -> float:
    h = hashlib.sha256(f"{salt}|{signal_id}".encode()).hexdigest()
    # 8 hex chars -> 32-bit fraction
    return int(h[:8], 16) / 0xFFFFFFFF


def assign_salted_splits(signal_ids: pd.Series, freeze: dict) -> pd.Series:
    salt = freeze["splits"]["salt"]
    ld = freeze["splits"]["label_design_frac"]
    dev = freeze["splits"]["dev_frac"]
    ih = freeze["splits"]["internal_holdout_frac"]
    cut1, cut2, cut3 = ld, ld + dev, ld + dev + ih
    roles = []
    for sid in signal_ids.astype(str):
        u = _u01(sid, salt)
        if u <= cut1:
            roles.append("LABEL_DESIGN")
        elif u <= cut2:
            roles.append("DEV")
        elif u <= cut3:
            roles.append("INTERNAL_HOLDOUT")
        else:
            roles.append("UNTOUCHED")
    return pd.Series(roles, index=signal_ids.index)


def v3_burned_confirmation_ids(freeze: dict) -> set[str]:
    """Rows that were in V3 confirmation splits (peeked) — cannot be V4 confirmation."""
    if not freeze["splits"].get("burn_v3_confirmation_from_v4_confirmation", True):
        return set()
    path = ROOT / freeze["splits"]["v3_panel_path"]
    if not path.exists():
        # fall back: v3 features
        path = ROOT / "out_v3" / "features" / "feature_matrix.parquet"
    if not path.exists():
        return set()
    v3 = pd.read_parquet(path, columns=["signal_id", "split_role"])
    burned = v3[v3["split_role"].isin(["INTERNAL_HOLDOUT", "UNTOUCHED"])]["signal_id"].astype(str)
    return set(burned.tolist())


def apply_burn_to_confirmation(df: pd.DataFrame, burned: set[str]) -> pd.DataFrame:
    """If a burned id landed in V4 holdout/untouched, move it to DEV."""
    out = df.copy()
    mask = out["signal_id"].astype(str).isin(burned) & out["split_role"].isin(["INTERNAL_HOLDOUT", "UNTOUCHED"])
    n = int(mask.sum())
    out.loc[mask, "split_role"] = "DEV"
    out.attrs["n_burned_moved_to_dev"] = n
    return out


def build_v4_frame(freeze: dict | None = None) -> pd.DataFrame:
    freeze = freeze or load_v4_freeze()
    feat_path = ROOT / freeze["source_feature_matrix"]
    df = pd.read_parquet(feat_path)
    df = df.copy()
    df["split_role_v3"] = df["split_role"]
    df["split_role"] = assign_salted_splits(df["signal_id"], freeze)
    burned = v3_burned_confirmation_ids(freeze)
    df = apply_burn_to_confirmation(df, burned)
    df["v3_confirmation_burned"] = df["signal_id"].astype(str).isin(burned)
    return df
