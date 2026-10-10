#!/usr/bin/env python3
"""
XGBoost research with live feature snapshots (B2).

Joins dashcam behavior labels with feature_snapshots.csv at (timestamp, pool).
KS test validates train feature distribution vs prod Redis samples.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0"
BEHAVIOR = AUDIT / "labels" / "dashcam_behavior_labeled.csv"
SNAPSHOTS = REPO / "logs" / "feature_snapshots.csv"
OUT = AUDIT / "xgb_research_v2"

log = logging.getLogger("train_xgb_live_features")

FEATURE_COLS = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
    "is_smart_money",
    "is_sniper",
    "is_diamond_hand",
    "is_paper_hand",
    "is_whale",
]


def _float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def load_snapshots(path: Path) -> dict[tuple[int, str], dict[str, float]]:
    out: dict[tuple[int, str], dict[str, float]] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pool = (row.get("pool") or "").lower()
            ts = int(_float(row.get("timestamp")))
            if not pool or ts <= 0:
                continue
            out[(ts, pool)] = {c: _float(row.get(c)) for c in FEATURE_COLS[:5]}
    return out


def join_rows(behavior_path: Path, snapshots: dict) -> list[dict]:
    rows: list[dict] = []
    if not behavior_path.exists():
        return rows
    with behavior_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pool = (row.get("pool") or "").lower()
            ts_raw = row.get("timestamp") or ""
            try:
                ts = int(float(ts_raw))
            except (TypeError, ValueError):
                continue
            snap = snapshots.get((ts, pool))
            if not snap:
                # nearest snapshot within 60s same pool
                for delta in range(-60000, 60001, 1000):
                    snap = snapshots.get((ts + delta, pool))
                    if snap:
                        break
            if not snap:
                continue
            merged = dict(snap)
            merged.update({
                "is_smart_money": 1.0 if row.get("is_smart_money") in ("True", "true", "1") else 0.0,
                "is_sniper": 1.0 if row.get("is_sniper") in ("True", "true", "1") else 0.0,
                "is_diamond_hand": 1.0 if row.get("is_diamond_hand") in ("True", "true", "1") else 0.0,
                "is_paper_hand": 1.0 if row.get("is_paper_hand") in ("True", "true", "1") else 0.0,
                "is_whale": 1.0 if row.get("is_whale") in ("True", "true", "1") else 0.0,
                "pool": pool,
                "timestamp": ts,
            })
            rows.append(merged)
    return rows


def ks_test(sample_a: np.ndarray, sample_b: np.ndarray) -> dict[str, float]:
    """Two-sample KS; uses scipy if available else manual approximation."""
    try:
        from scipy.stats import ks_2samp

        stat, p = ks_2samp(sample_a, sample_b)
        return {"ks_statistic": float(stat), "p_value": float(p)}
    except Exception:
        # Manual ECDF max diff
        a = np.sort(sample_a)
        b = np.sort(sample_b)
        grid = np.sort(np.unique(np.concatenate([a, b])))
        cdf_a = np.searchsorted(a, grid, side="right") / max(len(a), 1)
        cdf_b = np.searchsorted(b, grid, side="right") / max(len(b), 1)
        d = float(np.max(np.abs(cdf_a - cdf_b)))
        n = min(len(a), len(b))
        p_approx = float(max(0.0, 1.0 - 2.0 * d * (n ** 0.5)))
        return {"ks_statistic": d, "p_value": p_approx}


def prod_feature_sample(limit: int = 500) -> dict[str, list[float]]:
    """Sample live Redis feat:* keys for KS comparison."""
    import os

    try:
        import redis
    except ImportError:
        return {c: [] for c in FEATURE_COLS[:5]}

    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.from_url(url, decode_responses=True)
    out: dict[str, list[float]] = {c: [] for c in FEATURE_COLS[:5]}
    keys = list(r.scan_iter("feat:*:price_velocity_5m", count=200))[:limit]
    for k in keys:
        parts = k.split(":")
        if len(parts) < 2:
            continue
        pool = parts[1]
        for feat, col in [
            ("price_velocity_5m", "price_velocity_5m"),
            ("buy_pressure_ratio", "buy_pressure_ratio"),
            ("token_volatility_5m", "token_volatility_5m"),
            ("activity_heat", "activity_heat"),
            ("unique_buyer_entropy_5m", "unique_buyer_entropy_5m"),
        ]:
            v = r.get(f"feat:{pool}:{feat}")
            if v is not None:
                out[col].append(_float(v))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Train XGB on live feature snapshots")
    p.add_argument("--behavior-csv", type=str, default=str(BEHAVIOR))
    p.add_argument("--snapshots-csv", type=str, default=str(SNAPSHOTS))
    args = p.parse_args()

    snapshots = load_snapshots(Path(args.snapshots_csv))
    rows = join_rows(Path(args.behavior_csv), snapshots)
    OUT.mkdir(parents=True, exist_ok=True)

    ks_report: dict[str, Any] = {}
    prod = prod_feature_sample()
    for col in FEATURE_COLS[:5]:
        train_vals = np.array([r[col] for r in rows if col in r], dtype=float)
        prod_vals = np.array(prod.get(col) or [], dtype=float)
        if len(train_vals) >= 10 and len(prod_vals) >= 10:
            ks_report[col] = ks_test(train_vals, prod_vals)
        else:
            ks_report[col] = {"ks_statistic": None, "p_value": None, "note": "insufficient samples"}

    ks_pass = all(
        (v.get("p_value") or 0) > 0.05
        for v in ks_report.values()
        if v.get("p_value") is not None
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "joined_rows": len(rows),
        "snapshots_keys": len(snapshots),
        "ks_report": ks_report,
        "ks_acceptance_pass": ks_pass if any(v.get("p_value") for v in ks_report.values()) else None,
        "RESEARCH_ONLY": True,
        "NO_ALPHA_CLAIM": True,
    }
    (OUT / "ks_acceptance.json").write_text(json.dumps(report, indent=2))

    if rows:
        with (OUT / "joined_training_rows.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "pool"] + FEATURE_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, 0.0) for k in ["timestamp", "pool"] + FEATURE_COLS})

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
