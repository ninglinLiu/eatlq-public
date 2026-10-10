"""Shared cluster / block / paired bootstrap helpers for the diagnostic audit."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def cluster_bootstrap_sum(
    values: np.ndarray,
    clusters: np.ndarray,
    *,
    n_boot: int = 200,
    seed: int = 0,
) -> dict[str, float]:
    """Bootstrap the sum of `values` by resampling clusters with replacement."""
    if len(values) == 0:
        return {"mean": 0.0, "p05": 0.0, "p50": 0.0, "p95": 0.0, "frac_positive": 0.0, "n_clusters": 0}
    rng = np.random.default_rng(seed)
    # Aggregate to cluster sums
    s = pd.Series(values).groupby(clusters, sort=False).sum()
    group_sums = s.to_numpy(dtype=float)
    n = len(group_sums)
    idx = rng.integers(0, n, size=(n_boot, n))
    boots = group_sums[idx].sum(axis=1)
    return {
        "mean": float(boots.mean()),
        "p05": float(np.quantile(boots, 0.05)),
        "p50": float(np.quantile(boots, 0.50)),
        "p95": float(np.quantile(boots, 0.95)),
        "frac_positive": float((boots > 0).mean()),
        "n_clusters": int(n),
    }


def paired_delta_bootstrap(
    delta: np.ndarray,
    clusters: np.ndarray,
    *,
    n_boot: int = 200,
    seed: int = 0,
) -> dict[str, float]:
    """Cluster-bootstrap the sum of paired Δ = treat - control."""
    out = cluster_bootstrap_sum(delta, clusters, n_boot=n_boot, seed=seed)
    out["n_pairs"] = int(len(delta))
    out["mean_delta_per_pair"] = float(np.mean(delta)) if len(delta) else 0.0
    out["sum_delta"] = float(np.sum(delta)) if len(delta) else 0.0
    return out


def leave_one_unit_out(
    pnl: pd.Series,
    unit: pd.Series,
    *,
    max_units: int = 100,
) -> dict[str, Any]:
    total = float(pnl.sum())
    by = pnl.groupby(unit).sum().sort_values(key=lambda x: x.abs(), ascending=False)
    probe = by.head(max_units)
    flips = 0
    worst_drop = 0.0
    for u_pnl in probe.values:
        sub = total - float(u_pnl)
        worst_drop = min(worst_drop, sub - total)
        if total != 0 and (total > 0) != (sub > 0):
            flips += 1
    return {
        "n_units": int(len(by)),
        "n_probed": int(len(probe)),
        "n_sign_flips": int(flips),
        "flip_rate": float(flips / len(probe)) if len(probe) else 0.0,
        "total_pnl": total,
        "worst_loo_total": float(total - float(probe.iloc[0])) if len(probe) else total,
    }


def block_keys_from_day(day: pd.Series, grain: str = "day") -> np.ndarray:
    d = day.astype(str)
    if grain == "week":
        ts = pd.to_datetime(d, errors="coerce", utc=True)
        return (ts.dt.isocalendar().year.astype(str) + "-W" + ts.dt.isocalendar().week.astype(str)).to_numpy()
    return d.to_numpy()
