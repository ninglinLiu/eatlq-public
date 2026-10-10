#!/usr/bin/env python3
"""Repair expanded_v2 dataset_sample in-place (no ClickHouse, no training).

- Normalize amount_proxy from wei to human WETH
- Add amount_weth, amount_usd_notional, trade_size_bucket
- Add closed-loop eligibility / T+N horizon flags
- Refresh feature_manifest.json
- Write training_prep_expanded_v2_flags.json with copyability prep gates
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from v3_smart_wallet_roundtrip_dataset_builder import (  # noqa: E402
    ROUTER_TX_HASH_PREFIX,
    STABLECOIN_DECIMALS,
    WETH_WEI,
    build_manifest,
    closed_loop_horizon_flags,
    trade_size_bucket,
    usd_notional_from_buy,
    weth_from_raw,
)

DEFAULT_DIR = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Repair expanded_v2 dataset CSV (offline)")
    p.add_argument("--dataset-dir", default=DEFAULT_DIR)
    p.add_argument("--no-backup", action="store_true")
    return p.parse_args()


def as_float(v) -> float | None:
    if v in (None, "", "\\N"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _looks_like_wei(amount: float | None) -> bool:
    """Values above 1000 are wei-scale WETH raw, not human ether."""
    return amount is not None and amount > 1000.0


def repair_row(r: dict) -> dict:
    out = dict(r)
    weth_raw = as_float(r.get("amount_proxy"))
    if weth_raw is None:
        weth_raw = as_float(r.get("amount_weth"))
    token = str(r.get("token", "")).lower()
    if _looks_like_wei(weth_raw):
        weth_h = weth_from_raw(weth_raw)
    else:
        weth_h = weth_raw
    amount_outlier = False
    if weth_h is not None and weth_h > 500.0:
        amount_outlier = True
        weth_h = None
        usd_n = None
    usd_existing = as_float(r.get("amount_usd_notional"))
    if usd_existing is not None and usd_existing < 1_000_000:
        usd_n = usd_existing
    elif weth_h is not None:
        # Legacy CSV lacks token raw amount; size USD from WETH leg proxy only.
        usd_n = weth_h * float(os.environ.get("ETH_USD_NOTIONAL_PROXY", "3000"))
    else:
        usd_n = None

    hold = as_float(r.get("hold_time_sec"))
    closed = hold is not None or as_float(r.get("realized_wallet_pnl")) is not None
    pnl_raw = as_float(r.get("realized_wallet_pnl"))
    if pnl_raw is not None and _looks_like_wei(abs(pnl_raw)):
        pnl_h = weth_from_raw(pnl_raw)
    else:
        pnl_h = pnl_raw

    out["amount_proxy"] = weth_h if weth_h is not None else ""
    out["amount_weth"] = weth_h if weth_h is not None else ""
    out["amount_usd_notional"] = usd_n if usd_n is not None else ""
    out["amount_proxy_outlier"] = amount_outlier
    out["trade_size_bucket"] = trade_size_bucket(usd_n) or ""
    out.update(closed_loop_horizon_flags(hold if closed else None))
    out["label_family_short_window"] = "future_return_30m"
    out["label_family_closed_loop"] = "realized_wallet_pnl_weth" if closed else ""
    out["realized_wallet_pnl_weth"] = pnl_h if closed else ""
    out["realized_wallet_pnl"] = pnl_h if closed else ""
    tx = str(r.get("tx_hash") or r.get("entry_id", "").split(":")[0])
    out["router_tx_artifact_prefix"] = tx.lower().startswith(ROUTER_TX_HASH_PREFIX)
    return out


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.dataset_dir
    csv_path = out_dir / "dataset_sample.csv"
    if not csv_path.exists():
        print(f"Missing {csv_path}", file=sys.stderr)
        return 2

    rows = list(csv.DictReader(csv_path.open()))
    if not args.no_backup:
        bak = out_dir / "dataset_sample.csv.bak_pre_unit_repair"
        if not bak.exists():
            shutil.copy2(csv_path, bak)

    repaired = [repair_row(r) for r in rows]
    fieldnames: list[str] = []
    for row in repaired:
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(repaired)

    manifest = build_manifest(out_dir / "feature_manifest.json")

    # training prep gates
    n = len(repaired)
    closed_n = sum(1 for r in repaired if str(r.get("closed_loop_eligible")).lower() in ("true", "1"))
    pnl_n = sum(1 for r in repaired if as_float(r.get("realized_wallet_pnl_weth")) is not None)
    hold_n = sum(1 for r in repaired if as_float(r.get("hold_time_sec")) is not None)
    amounts = [as_float(r.get("amount_weth")) for r in repaired]
    amounts_ok = [a for a in amounts if a is not None]
    outlier_n = sum(1 for r in repaired if str(r.get("amount_proxy_outlier")).lower() == "true")
    unit_ok = bool(amounts_ok) and max(amounts_ok) < 500.0 and min(amounts_ok) >= 0.0
    usd_vals = [as_float(r.get("amount_usd_notional")) for r in repaired]
    usd_ok = [u for u in usd_vals if u is not None]
    usd_unit_ok = bool(usd_ok) and max(usd_ok) < 5_000_000.0
    unit_ok = unit_ok and usd_unit_ok
    pool_counts = Counter(str(r.get("pool", "")).lower() for r in repaired if r.get("pool"))
    top_pool = max(pool_counts.values()) / n if pool_counts else 0.0

    prep = {
        "DATA_REPAIR_RAN": True,
        "FULL_TRAINING_READY": False,
        "BOUNDED_XGB_SMOKE_TRAINING_REVIEW_READY": False,
        "TRAINING_PREP_PASS": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "DATA_REPAIR_BEFORE_RETRAIN": True,
        "row_count": n,
        "closed_loop_eligible_share": round(closed_n / n, 4) if n else 0,
        "realized_wallet_pnl_weth_coverage": round(pnl_n / n, 4) if n else 0,
        "hold_time_sec_coverage": round(hold_n / n, 4) if n else 0,
        "amount_proxy_human_units_pass": unit_ok,
        "amount_proxy_outlier_rows": outlier_n,
        "top_pool_share": round(top_pool, 4),
        "gates": {
            "amount_proxy_human_units": unit_ok,
            "closed_loop_eligible_share_gte_0_45": (closed_n / n) >= 0.45 if n else False,
            "realized_pnl_coverage_gte_0_45": (pnl_n / n) >= 0.45 if n else False,
            "hold_time_coverage_gte_0_45": (hold_n / n) >= 0.45 if n else False,
            "top_pool_share_lt_0_20": top_pool < 0.20,
        },
        "failed_gates": [],
        "NEXT_STEP": "DATA_REPAIR_BEFORE_RETRAIN",
        "note": "Unit repair applied offline. Full builder rebuild still recommended before smoke retrain.",
    }
    for gname, passed in prep["gates"].items():
        if not passed:
            prep["failed_gates"].append(gname)
    if not prep["failed_gates"]:
        prep["TRAINING_PREP_PASS"] = True
        prep["BOUNDED_XGB_SMOKE_TRAINING_REVIEW_READY"] = True
    prep["NEXT_STEP"] = "DATA_REPAIR_BEFORE_RETRAIN"
    prep["OFFLINE_UNIT_REPAIR_PASS"] = unit_ok
    prep["SMOKE_RETRAIN_ALLOWED"] = False

    (out_dir / "training_prep_expanded_v2_flags.json").write_text(
        json.dumps(prep, indent=2) + "\n", encoding="utf-8"
    )

    repair_flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_repaired": True,
        "rows": n,
        "manifest_fields": len(manifest),
        "amount_proxy_human_units_pass": unit_ok,
        "closed_loop_eligible_share": prep["closed_loop_eligible_share"],
        "training_prep": prep,
    }
    (out_dir / "dataset_repair_flags.json").write_text(
        json.dumps(repair_flags, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(repair_flags, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
