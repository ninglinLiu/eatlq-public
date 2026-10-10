#!/usr/bin/env python3
"""Build or backfill model_manifest sidecars for XGBoost model JSON files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_FEATURE_ORDER: List[str] = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]


def manifest_path_for(model_path: Path) -> Path:
    return model_path.with_name(f"{model_path.stem}.manifest.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_feature_order(model_path: Path, metrics_path: Optional[Path]) -> List[str]:
    if metrics_path and metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            cols = metrics.get("feature_columns") or metrics.get("features")
            if isinstance(cols, list) and cols:
                return [str(c) for c in cols]
        except Exception:
            pass
    name = model_path.name.lower()
    if "realtime" in name or "base_v" in name or "v2" in name or "v3" in name:
        return list(DEFAULT_FEATURE_ORDER[:4])
    return list(DEFAULT_FEATURE_ORDER)


def build_manifest(
    model_path: Path,
    feature_order: Optional[Iterable[str]] = None,
    trained_at: Optional[str] = None,
    include_hash: bool = True,
) -> dict:
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    metrics_path = model_path.with_name(model_path.stem + "_metrics.json")
    order = list(feature_order) if feature_order else infer_feature_order(model_path, metrics_path)
    payload = {
        "model_file": model_path.name,
        "feature_order": order,
        "num_features": len(order),
        "trained_at": trained_at
        or datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
    }
    if include_hash:
        payload["sha256"] = sha256_file(model_path)
    return payload


def write_manifest(model_path: Path, payload: dict, overwrite: bool = True) -> Path:
    out = manifest_path_for(model_path)
    if out.exists() and not overwrite:
        return out
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="*", type=Path, help="Model JSON path(s)")
    parser.add_argument(
        "--glob",
        default="models/signal_model*.json",
        help="Backfill glob when no explicit model paths are given",
    )
    parser.add_argument(
        "--features",
        default="",
        help="Comma-separated feature order override",
    )
    parser.add_argument("--no-hash", action="store_true")
    parser.add_argument("--no-overwrite", action="store_true")
    args = parser.parse_args(argv)

    paths: List[Path] = list(args.model)
    if not paths:
        paths = sorted(Path(".").glob(args.glob))

    if not paths:
        print("ERROR: no model files matched", file=sys.stderr)
        return 1

    feature_order = None
    if args.features.strip():
        feature_order = [c.strip() for c in args.features.split(",") if c.strip()]

    written = 0
    for model_path in paths:
        name = model_path.name.lower()
        if "_metrics" in name or "_threshold" in name:
            continue
        if not model_path.is_file():
            print(f"skip missing: {model_path}")
            continue
        payload = build_manifest(
            model_path,
            feature_order=feature_order,
            include_hash=not args.no_hash,
        )
        out = write_manifest(model_path, payload, overwrite=not args.no_overwrite)
        print(f"wrote {out}")
        written += 1

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
