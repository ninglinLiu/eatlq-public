"""Append-only live feature snapshot log (B2 — train/infer parity)."""

from __future__ import annotations

import csv
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_LOG_DIR = Path(os.environ.get("FEATURE_SNAPSHOT_LOG_DIR", "/app/logs"))
_DEFAULT_FILE = _DEFAULT_LOG_DIR / "feature_snapshots.csv"

_FIELDS = (
    "timestamp",
    "pool",
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
)

_lock = threading.Lock()


class FeatureSnapshotLogger:
    """Log Redis feat:* values at strategy tick time for offline training."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path or _DEFAULT_FILE
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_header()

    def _ensure_header(self) -> None:
        if self.log_path.exists() and self.log_path.stat().st_size > 0:
            return
        with self.log_path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_FIELDS).writeheader()

    def log_tick(self, pool: str, features: Any, *, ts_ms: int | None = None) -> None:
        if not pool:
            return
        ts = ts_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        row = {
            "timestamp": ts,
            "pool": pool.lower(),
            "price_velocity_5m": getattr(features, "price_velocity_5m", 0.0),
            "buy_pressure_ratio": getattr(features, "buy_pressure_ratio", 0.0),
            "token_volatility_5m": getattr(features, "token_volatility_5m", 0.0),
            "activity_heat": getattr(features, "activity_heat", 0.0),
            "unique_buyer_entropy_5m": getattr(features, "unique_buyer_entropy_5m", 0.0),
        }
        with _lock:
            with self.log_path.open("a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=_FIELDS).writerow(row)
