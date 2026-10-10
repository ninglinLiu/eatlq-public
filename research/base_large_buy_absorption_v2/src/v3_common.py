"""Shared helpers for LBA v3 research layer (does not mutate v2 freeze)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, ROOT, save_json, utcnow

V3_OUT = OUT / "v3"
V3_CFG = ROOT / "configs" / "freeze_v3.json"


def load_v3() -> dict:
    return json.loads(V3_CFG.read_text())


def bh_correct(pvals: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg: return reject flags for each p-value (same order)."""
    n = len(pvals)
    if n == 0:
        return []
    order = np.argsort(pvals)
    ranked = np.asarray(pvals, dtype=float)[order]
    thresh = alpha * (np.arange(1, n + 1) / n)
    below = ranked <= thresh
    if not below.any():
        return [False] * n
    max_k = int(np.where(below)[0].max())
    reject = np.zeros(n, dtype=bool)
    reject[order[: max_k + 1]] = True
    return reject.tolist()


def p_from_boot_frac_positive(frac_positive: float, n_boot: int) -> float:
    """One-sided p ≈ 1 - frac_positive, floored by 1/(n_boot+1)."""
    return float(max(1.0 / (n_boot + 1), 1.0 - frac_positive))


def load_oof_arms() -> dict[str, pd.DataFrame]:
    arms = ["POOL_ONLY", "LARGE_BUY_ONLY", "ABSORPTION_ONLY", "LARGE_BUY_ABSORPTION"]
    out = {}
    for a in arms:
        p = OUT / "oos" / f"oof_{a}.parquet"
        df = pd.read_parquet(p)
        if "day" not in df.columns:
            df["day"] = pd.to_datetime(df["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
        out[a] = df
    return out


def ensure_v3_out() -> Path:
    V3_OUT.mkdir(parents=True, exist_ok=True)
    return V3_OUT
