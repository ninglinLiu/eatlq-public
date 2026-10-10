"""Read-only copy of Base offline tapes and goldens into this project. No CH, no RPC."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.common import PATHS, ROOT, sha256_file, utc_now_iso, write_json

DEST_OFFLINE = ROOT / "data/raw/base_offline"
DEST_GOLDEN = ROOT / "data/raw/golden"
DEST_DUNE = ROOT / "data/raw/dune_overlay"


COPIES = [
    ("b5_swaps", PATHS["eatlq_b5"], DEST_OFFLINE / "base_b5_real_swaps_20260714.tsv", "tape"),
    ("b8_swaps", PATHS["eatlq_b8"], DEST_OFFLINE / "base_b8_future_swaps_20260714.tsv", "tape"),
    ("sprint083_safe", PATHS["eatlq_sprint083_safe"], DEST_OFFLINE / "copyability_safe_dataset_sample.csv", "wallet_tape"),
    ("sprint083_dataset", PATHS["eatlq_sprint083_dataset"], DEST_OFFLINE / "dataset_sample.csv", "wallet_tape"),
    ("sprint083_closed", PATHS["eatlq_sprint083_closed"], DEST_OFFLINE / "closed_trades_sample.csv", "fifo_closed"),
    ("run004", PATHS["eatlq_run004"], DEST_OFFLINE / "run004_v3r6_candidate_pool.csv", "pool_swaps"),
    ("exclusions", PATHS["actor_flow_exclusions"], DEST_OFFLINE / "base_attribution_exclusions.json", "exclusions"),
    ("b3_entity", PATHS["eatlq_b3_entity"], DEST_GOLDEN / "base_b3_entity_attribution_sample_v2_20260714.json", "golden"),
    ("b3_inventory", PATHS["eatlq_b3_inventory"], DEST_GOLDEN / "base_b3_inventory_golden_20260714.json", "golden"),
    ("b3_trace", PATHS["eatlq_b3_trace"], DEST_GOLDEN / "base_b3_trace_golden_20260714.json", "golden"),
    ("gl2b_fifo", PATHS["eatlq_gl2b_fifo"], DEST_GOLDEN / "base_gl2_b_fifo_cashleg_golden_20260715.json", "golden"),
    ("gl2b_final", PATHS["eatlq_gl2b_final"], DEST_GOLDEN / "base_gl2_b_final_20260715.json", "golden"),
    ("gl2b_report", PATHS["eatlq_gl2b_report"], DEST_GOLDEN / "GL2_B_smartmoney_copyability_result_20260715.md", "golden"),
    ("actor_flow_golden", PATHS["actor_flow_golden"], DEST_GOLDEN / "golden_audit_rows.csv", "golden"),
    ("dune_overlay", PATHS["eatlq_dune"], DEST_DUNE / "dune_smart_base_20260504_q1_wallet_summary_v2.csv", "overlay_not_vendor"),
]


def copy_all() -> dict:
    DEST_OFFLINE.mkdir(parents=True, exist_ok=True)
    DEST_GOLDEN.mkdir(parents=True, exist_ok=True)
    DEST_DUNE.mkdir(parents=True, exist_ok=True)
    items = []
    for key, src, dest, kind in COPIES:
        src_path = Path(src)
        rec = {
            "key": key,
            "kind": kind,
            "src": str(src_path),
            "dest": str(dest),
            "copied": False,
        }
        if not src_path.exists():
            rec["error"] = "SRC_MISSING"
            items.append(rec)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src_path.resolve() != dest.resolve():
            shutil.copy2(src_path, dest)
        rec["copied"] = dest.exists()
        rec["nbytes"] = dest.stat().st_size if dest.exists() else 0
        rec["sha256"] = sha256_file(dest) if dest.exists() else None
        items.append(rec)
    payload = {
        "generated_at": utc_now_iso(),
        "rpc_used": False,
        "clickhouse_used": False,
        "n_copied": sum(1 for i in items if i.get("copied")),
        "n_missing": sum(1 for i in items if not i.get("copied")),
        "items": items,
        "bsc_tape": "INCONCLUSIVE_BSC_NO_LEDGER",
    }
    write_json(ROOT / "data/raw/base_offline/copy_manifest.json", payload)
    return payload


if __name__ == "__main__":
    p = copy_all()
    print(json.dumps({k: p[k] for k in ("n_copied", "n_missing", "bsc_tape")}, indent=2))
