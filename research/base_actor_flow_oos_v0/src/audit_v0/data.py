"""Load labeled panel and rebuild OOF arm selections without mutating scored outputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..arms import run_arms_oos, select_arm
from ..common import OUT, load_freeze

AUDIT_OUT = OUT / "audit"
AUDIT_CFG = Path("/root/base_actor_flow_oos_v0/configs/audit_v0.json")


def load_audit_cfg() -> dict:
    if AUDIT_CFG.exists():
        return json.loads(AUDIT_CFG.read_text())
    return {
        "rpc": {"skip_by_default": True, "max_traces": 160},
        "bootstrap": {"n_boot": 200, "seed": 7},
        "matching": {"max_pairs": 20000},
    }


def load_labeled_panel() -> pd.DataFrame:
    path = OUT / "panel" / "events_labeled.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing labeled panel: {path}")
    df = pd.read_parquet(path)
    # Normalize dtypes used by audit
    for c in ("decision_time", "label_end_time", "ts", "day"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["wallet_pnl_weth"] = pd.to_numeric(df.get("wallet_pnl_weth"), errors="coerce")
    if "day" not in df.columns or df["day"].isna().all():
        df["day"] = pd.to_datetime(df["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    else:
        # day may already be int yyyymmdd or string
        if pd.api.types.is_numeric_dtype(df["day"]):
            df["day"] = df["day"].astype("Int64").astype(str)
    df["month"] = pd.to_datetime(df["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m")
    return df


def load_or_build_oof(panel: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Rebuild walk-forward OOF selections; cache under out/audit/ for reuse."""
    cache = AUDIT_OUT / "oof_cache"
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / "_ok"
    arms = list(load_freeze()["arms"])
    if marker.exists() and all((cache / f"{a}.parquet").exists() for a in arms):
        return {a: pd.read_parquet(cache / f"{a}.parquet") for a in arms}
    panel = panel if panel is not None else load_labeled_panel()
    print("[audit] rebuilding OOF arm selections via arms.run_arms_oos ...", flush=True)
    oof = run_arms_oos(panel)
    for a, frame in oof.items():
        frame.to_parquet(cache / f"{a}.parquet", index=False)
    marker.write_text("ok\n")
    return oof


def selected(frame: pd.DataFrame) -> pd.DataFrame:
    m = frame["policy_selected"].astype(bool) if "policy_selected" in frame.columns else pd.Series(False, index=frame.index)
    return frame.loc[m].copy()


def whale_flag(frame: pd.DataFrame) -> pd.Series:
    s = pd.to_numeric(frame.get("whale_score", 0), errors="coerce").fillna(0.0)
    absb = pd.to_numeric(frame.get("whale_absorbed", 0), errors="coerce").fillna(0.0)
    return (s > 0) & (absb > 0)


def add_covariate_bins(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if "age_bucket" not in d.columns or d["age_bucket"].isna().all():
        age = pd.to_numeric(d.get("pool_age_s", 0), errors="coerce").fillna(0)
        d["age_bucket"] = pd.cut(
            age,
            bins=[-1, 3600, 86400, 7 * 86400, 30 * 86400, 1e18],
            labels=["<1h", "1h-1d", "1d-7d", "7d-30d", ">30d"],
        ).astype(str)
    depth = pd.to_numeric(d.get("depth_proxy", np.nan), errors="coerce")
    if depth.notna().sum() < max(10, len(d) // 20):
        depth = pd.to_numeric(d.get("liquidity", 0), errors="coerce").fillna(0.0)
    d["_depth"] = depth.fillna(depth.median() if depth.notna().any() else 0.0)
    d["_mom"] = pd.to_numeric(d.get("mom_from_open", 0), errors="coerce").fillna(0.0)
    size = pd.to_numeric(d.get("quote_weth", np.nan), errors="coerce")
    if size.isna().all():
        size = pd.to_numeric(d.get("log_size_weth", 0), errors="coerce").fillna(0.0)
    d["_size"] = size.fillna(0.0)

    def _decile(s: pd.Series) -> pd.Series:
        try:
            return pd.qcut(s.rank(method="first"), 10, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series(0, index=s.index)

    d["depth_decile"] = _decile(d["_depth"])
    d["mom_decile"] = _decile(d["_mom"])
    d["size_decile"] = _decile(d["_size"])
    d["match_stratum"] = (
        d["age_bucket"].astype(str) + "|"
        + d["depth_decile"].astype(str) + "|"
        + d["mom_decile"].astype(str) + "|"
        + d["size_decile"].astype(str)
    )
    return d


def save_audit_json(name: str, obj: Any) -> Path:
    AUDIT_OUT.mkdir(parents=True, exist_ok=True)
    path = AUDIT_OUT / name
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")
    return path
