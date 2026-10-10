"""Run-004 Patch V3R-28 — timestamp-horizon leakage / holdout audit.

Research-only/read-only static audit of V3R-27 timestamp-horizon labels.
No RPC, source expansion, training, production labeler, mainline, executor,
promotion, or live behavior changes.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float, as_int, distribution, top_shares  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

V3R27_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r27_bounded_timestamp_horizon_validation_summary.json"
V3R27_ROWS = ROOT / "mvp/diagnostics/run004_v3r27_bounded_timestamp_horizon_validation_rows.csv"
V3R26_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r26_timestamp_block_mapping_summary.json"
V3R24R_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_summary.json"
SCHEMA_JSON = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_schema.json"
OPERATIONS_MD = ROOT / "Operations.md"
ARCHITECTURE_MD = ROOT / "Architecture.md"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r28-timestamp-horizon-leakage-holdout-audit.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r28_timestamp_horizon_leakage_holdout_audit_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r28_timestamp_horizon_leakage_holdout_audit_rows.csv"

NEAR_ZERO_ABS = 0.05
TEMPORAL_ADJACENT_BLOCKS = 900


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def duplicate_count(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        dup += int(key in seen)
        seen.add(key)
    return dup


def positive_rate(rows: list[dict[str, Any]], field: str) -> float:
    vals = [as_float(r.get(field)) for r in rows if as_float(r.get(field)) is not None]
    if not vals:
        return 0.0
    return round(sum(1 for v in vals if v is not None and v > 0) / len(vals), 4)


def split_metrics(train: list[dict[str, Any]], holdout: list[dict[str, Any]], mode: str, failure_reason: str = "") -> dict[str, Any]:
    train_pools = {r["pool_address"] for r in train}
    holdout_pools = {r["pool_address"] for r in holdout}
    train_tokens = {r["token_address"] for r in train}
    holdout_tokens = {r["token_address"] for r in holdout}
    pool_overlap = len(train_pools & holdout_pools)
    token_overlap = len(train_tokens & holdout_tokens)
    feasible = bool(train and holdout and not failure_reason)
    if mode == "group_by_pool" and pool_overlap:
        feasible = False
        failure_reason = failure_reason or "pool_overlap"
    if mode == "group_by_token" and token_overlap:
        feasible = False
        failure_reason = failure_reason or "token_overlap"
    return {
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "train_positive_rate_5m": positive_rate(train, "label_return_5m"),
        "holdout_positive_rate_5m": positive_rate(holdout, "label_return_5m"),
        "train_positive_rate_30m": positive_rate(train, "label_return_30m"),
        "holdout_positive_rate_30m": positive_rate(holdout, "label_return_30m"),
        "train_pool_count": len(train_pools),
        "holdout_pool_count": len(holdout_pools),
        "train_token_count": len(train_tokens),
        "holdout_token_count": len(holdout_tokens),
        "pool_overlap": pool_overlap,
        "token_overlap": token_overlap,
        "split_feasible": feasible,
        "failure_reason": failure_reason,
    }


def chronological_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: (as_int(r["entry_timestamp"]), as_int(r["entry_block"])))
    holdout_n = max(15, math.ceil(len(rows) * 0.30))
    return split_metrics(ordered[:-holdout_n], ordered[-holdout_n:], "chronological")


def group_split(rows: list[dict[str, Any]], group_field: str, mode: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[group_field]].append(row)
    ordered_groups = sorted(groups.items(), key=lambda kv: max(as_int(r["entry_timestamp"]) for r in kv[1]), reverse=True)
    target = max(15, math.ceil(len(rows) * 0.30))
    holdout_groups = set()
    holdout: list[dict[str, Any]] = []
    for group, group_rows in ordered_groups:
        holdout_groups.add(group)
        holdout.extend(group_rows)
        if len(holdout) >= target:
            break
    train = [r for r in rows if r[group_field] not in holdout_groups]
    reason = ""
    if not train or not holdout:
        reason = "insufficient_sample"
    if len(holdout) < 15:
        reason = "holdout_too_small"
    metrics = split_metrics(train, holdout, mode, reason)
    metrics["holdout_groups"] = sorted(holdout_groups)
    return metrics


def temporal_adjacency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"same_pool_adjacent_pairs": 0, "same_token_adjacent_pairs": 0}
    examples = []
    for field, key in [("pool_address", "same_pool_adjacent_pairs"), ("token_address", "same_token_adjacent_pairs")]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        for value, group in grouped.items():
            ordered = sorted(group, key=lambda r: as_int(r["entry_block"]))
            for prev, cur in zip(ordered, ordered[1:]):
                gap = as_int(cur["entry_block"]) - as_int(prev["entry_block"])
                if 0 <= gap <= TEMPORAL_ADJACENT_BLOCKS:
                    counts[key] += 1
                    if len(examples) < 8:
                        examples.append({"field": field, "value": value, "block_gap": gap, "tx_a": prev["tx_hash"], "tx_b": cur["tx_hash"]})
    return {**counts, "examples": examples, "risk": counts["same_pool_adjacent_pairs"] > 0 or counts["same_token_adjacent_pairs"] > 0}


def reconstruct(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = {(r["tx_hash"], r["log_index"]) for r in rows}
    pools = [r["pool_address"] for r in rows]
    tokens = [r["token_address"] for r in rows]
    return {
        "total_rows": len(rows),
        "valid_5m_label_count": sum(1 for r in rows if as_bool(r.get("timestamp_quote_5m_ok")) and as_float(r.get("label_return_5m")) is not None),
        "valid_30m_label_count": sum(1 for r in rows if as_bool(r.get("timestamp_quote_30m_ok")) and as_float(r.get("label_return_30m")) is not None),
        "pool_count": len(set(pools)),
        "token_count": len(set(tokens)),
        "fee_distribution": dict(Counter(str(as_int(r["fee"])) for r in rows)),
        "source_file_distribution": dict(Counter(r["source_path"] for r in rows)),
        "tx_hash_log_index_unique_count": len(keys),
        "duplicate_key_count": duplicate_count(rows),
        "duplicate_conflict_count": duplicate_count(rows),
    }


def leakage_audit(rows: list[dict[str, Any]], v3r27_summary: dict[str, Any]) -> dict[str, Any]:
    source_dist = Counter(r["source_path"] for r in rows)
    fee_dist = Counter(str(as_int(r["fee"])) for r in rows)
    top_source_share = max(source_dist.values()) / len(rows)
    top_fee_share = max(fee_dist.values()) / len(rows)
    adjacency = temporal_adjacency(rows)
    horizon_order_ok = all(as_int(r["entry_block"]) < as_int(r["exit_block_5m"]) < as_int(r["exit_block_30m"]) for r in rows)
    timestamp_fields_ok = all(as_int(r["entry_timestamp"]) > 0 and as_int(r["exit_timestamp_5m"]) > 0 and as_int(r["exit_timestamp_30m"]) > 0 for r in rows)
    audit = {
        "same_block_sanity_not_label": bool(v3r27_summary["decision_flags"]["same_block_sanity_not_label"]),
        "fixed_block_not_primary_label": bool(v3r27_summary["fixed_block_return_control_only"]),
        "timestamp_horizon_primary_label": bool(v3r27_summary["timestamp_horizon_primary_label"]),
        "labels_from_timestamp_horizon": all(as_bool(r.get("timestamp_quote_5m_ok")) and as_bool(r.get("timestamp_quote_30m_ok")) for r in rows),
        "entry_before_5m_before_30m": horizon_order_ok,
        "duplicate_key_count": duplicate_count(rows),
        "identity_leakage_risk": "handled_by_group_splits",
        "temporal_adjacency": adjacency,
        "source_top_share": round(top_source_share, 4),
        "source_concentration_severe": top_source_share > 0.75 or len(source_dist) == 1,
        "fee_top_share": round(top_fee_share, 4),
        "fee_concentration_severe": top_fee_share > 0.75 or len(fee_dist) == 1,
        "timestamp_missing_or_mapping_fallback": not timestamp_fields_ok,
    }
    audit["leakage_audit_passed"] = (
        audit["same_block_sanity_not_label"]
        and audit["fixed_block_not_primary_label"]
        and audit["timestamp_horizon_primary_label"]
        and audit["labels_from_timestamp_horizon"]
        and audit["entry_before_5m_before_30m"]
        and audit["duplicate_key_count"] == 0
        and not audit["source_concentration_severe"]
        and not audit["fee_concentration_severe"]
        and not audit["timestamp_missing_or_mapping_fallback"]
    )
    return audit


def sign_flip_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    flips = []
    for row in rows:
        r5 = as_float(row.get("label_return_5m"))
        r30 = as_float(row.get("label_return_30m"))
        if r5 is None or r30 is None or r5 == 0 or r30 == 0:
            continue
        if r5 * r30 < 0:
            near_zero = abs(r5) <= NEAR_ZERO_ABS or abs(r30) <= NEAR_ZERO_ABS
            flips.append({**row, "near_zero_flip": near_zero})
    pools = [r["pool_address"] for r in flips]
    tokens = [r["token_address"] for r in flips]
    magnitudes = [min(abs(float(r["label_return_5m"])), abs(float(r["label_return_30m"]))) for r in flips]
    examples = [
        {
            "tx_hash": r["tx_hash"],
            "log_index": r["log_index"],
            "pool_address": r["pool_address"],
            "token_address": r["token_address"],
            "fee": as_int(r["fee"]),
            "label_return_5m": as_float(r["label_return_5m"]),
            "label_return_30m": as_float(r["label_return_30m"]),
            "near_zero_flip": r["near_zero_flip"],
        }
        for r in flips[:8]
    ]
    share = round(len(flips) / max(1, len(rows)), 4)
    near_zero_share = round(sum(1 for r in flips if r["near_zero_flip"]) / max(1, len(flips)), 4)
    concentrated = bool(flips) and (Counter(pools).most_common(1)[0][1] / len(flips) > 0.5 or Counter(tokens).most_common(1)[0][1] / len(flips) > 0.5)
    passed = share <= 0.20 or near_zero_share >= 0.75
    recommended = "dual_horizon" if passed and len(flips) > 0 else "30m" if passed else "not_ready"
    return {
        "sign_flip_count": len(flips),
        "sign_flip_share": share,
        "sign_flip_fee_distribution": dict(Counter(str(as_int(r["fee"])) for r in flips)),
        "sign_flip_pool_top_share": round(Counter(pools).most_common(1)[0][1] / len(flips), 4) if flips else 0.0,
        "sign_flip_token_top_share": round(Counter(tokens).most_common(1)[0][1] / len(flips), 4) if flips else 0.0,
        "sign_flip_examples": examples,
        "small_magnitude_min_abs_distribution": distribution(magnitudes),
        "near_zero_flip_share": near_zero_share,
        "sign_flips_concentrated": concentrated,
        "dual_horizon_label_unstable": not passed,
        "label_stability_passed": passed,
        "recommended_primary_horizon": recommended,
        "reason": "sign_flip_share <= 0.20; keep dual horizons through holdout audit" if passed else "5m/30m disagreement too high",
    }


def assign_rows(rows: list[dict[str, Any]], chronological: dict[str, Any], pool_split: dict[str, Any], token_split: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda r: (as_int(r["entry_timestamp"]), as_int(r["entry_block"])))
    holdout_n = chronological["holdout_rows"]
    chronological_holdout = {(r["tx_hash"], r["log_index"]) for r in ordered[-holdout_n:]}
    pool_holdout_groups = set(pool_split.get("holdout_groups", []))
    token_holdout_groups = set(token_split.get("holdout_groups", []))
    out = []
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        r5 = as_float(row.get("label_return_5m")) or 0.0
        r30 = as_float(row.get("label_return_30m")) or 0.0
        out.append(
            {
                **row,
                "chronological_split": "holdout" if key in chronological_holdout else "train",
                "pool_split": "holdout" if row["pool_address"] in pool_holdout_groups else "train",
                "token_split": "holdout" if row["token_address"] in token_holdout_groups else "train",
                "sign_flip_5m_30m": bool(r5 and r30 and r5 * r30 < 0),
            }
        )
    return out


def write_rows(rows: list[dict[str, Any]]) -> None:
    fields = [
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "entry_timestamp",
        "exit_block_5m",
        "exit_block_30m",
        "fixed_block_return_control",
        "label_return_5m",
        "label_return_30m",
        "chronological_split",
        "pool_split",
        "token_split",
        "sign_flip_5m_30m",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:100]:
            writer.writerow({k: row.get(k, "") for k in fields})


def render_md(summary: dict[str, Any]) -> str:
    flags = summary["decision_flags"]
    return f"""# Run-004 Patch V3R-28 — Timestamp-Horizon Leakage / Holdout Audit

Status: **{'PASSED' if flags['holdout_audit_passed'] else 'FAILED'}**. Research-only/read-only. No training, no model promotion, no mainline/live.

## Leakage Audit

- duplicate conflicts: **{summary['dataset_reconstruction']['duplicate_conflict_count']}**
- same-block sanity not label: **{flags['same_block_sanity_not_label']}**
- fixed-block not primary label: **{flags['fixed_block_not_primary_label']}**
- timestamp horizon primary label: **{flags['timestamp_horizon_primary_label']}**
- source top share / fee top share: **{summary['leakage_audit']['source_top_share']} / {summary['leakage_audit']['fee_top_share']}**
- temporal adjacency risk: **{summary['leakage_audit']['temporal_adjacency']['risk']}**

## Holdout / Split Feasibility

- chronological: `{summary['holdout_splits']['chronological']}`
- group-by-pool: `{summary['holdout_splits']['group_by_pool']}`
- group-by-token: `{summary['holdout_splits']['group_by_token']}`

## Sign Flip / Label Stability

- sign flips 5m/30m: **{summary['label_stability']['sign_flip_count']} ({summary['label_stability']['sign_flip_share']})**
- near-zero flip share: **{summary['label_stability']['near_zero_flip_share']}**
- recommended primary horizon: **{summary['label_stability']['recommended_primary_horizon']}**
- label stability passed: **{summary['label_stability']['label_stability_passed']}**

## Decision Flags

- `v3r28_completed`: `{str(flags['v3r28_completed']).lower()}`
- `dataset_reconstructed`: `{str(flags['dataset_reconstructed']).lower()}`
- `duplicate_conflict_count`: `{flags['duplicate_conflict_count']}`
- `same_block_sanity_not_label`: `{str(flags['same_block_sanity_not_label']).lower()}`
- `timestamp_horizon_primary_label`: `{str(flags['timestamp_horizon_primary_label']).lower()}`
- `fixed_block_not_primary_label`: `{str(flags['fixed_block_not_primary_label']).lower()}`
- `leakage_audit_passed`: `{str(flags['leakage_audit_passed']).lower()}`
- `chronological_split_feasible`: `{str(flags['chronological_split_feasible']).lower()}`
- `pool_disjoint_split_feasible`: `{str(flags['pool_disjoint_split_feasible']).lower()}`
- `token_disjoint_split_feasible`: `{str(flags['token_disjoint_split_feasible']).lower()}`
- `label_stability_passed`: `{str(flags['label_stability_passed']).lower()}`
- `sign_flip_count_5m_30m`: `{flags['sign_flip_count_5m_30m']}`
- `sign_flip_share_5m_30m`: `{flags['sign_flip_share_5m_30m']}`
- `recommended_primary_horizon`: `{flags['recommended_primary_horizon']}`
- `holdout_audit_passed`: `{str(flags['holdout_audit_passed']).lower()}`
- `allow_v3r29_small_timestamp_dataset_expansion`: `{str(flags['allow_v3r29_small_timestamp_dataset_expansion']).lower()}`
- `allow_model_research`: `{str(flags['allow_model_research']).lower()}`
- `allow_larger_expansion`: `{str(flags['allow_larger_expansion']).lower()}`
- `allow_mainline`: `{str(flags['allow_mainline']).lower()}`
- `allow_live`: `{str(flags['allow_live']).lower()}`
"""


def main() -> None:
    v3r27_summary = json.loads(V3R27_SUMMARY.read_text(encoding="utf-8"))
    v3r26_summary = json.loads(V3R26_SUMMARY.read_text(encoding="utf-8"))
    v3r24r_summary = json.loads(V3R24R_SUMMARY.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    operations = OPERATIONS_MD.read_text(encoding="utf-8")
    architecture = ARCHITECTURE_MD.read_text(encoding="utf-8")
    rows = load_csv(V3R27_ROWS)
    reconstruction = reconstruct(rows)
    leakage = leakage_audit(rows, v3r27_summary)
    chronological = chronological_split(rows)
    pool_split = group_split(rows, "pool_address", "group_by_pool")
    token_split = group_split(rows, "token_address", "group_by_token")
    stability = sign_flip_audit(rows)
    chosen_split_no_severe_overlap = pool_split["split_feasible"] or token_split["split_feasible"]
    holdout_audit_passed = (
        leakage["leakage_audit_passed"]
        and chronological["split_feasible"]
        and chronological["holdout_rows"] >= 15
        and (pool_split["split_feasible"] or pool_split["failure_reason"] == "insufficient_sample")
        and (token_split["split_feasible"] or token_split["failure_reason"] == "insufficient_sample")
        and chosen_split_no_severe_overlap
        and stability["label_stability_passed"]
    )
    flags = {
        "v3r28_completed": True,
        "dataset_reconstructed": True,
        "duplicate_conflict_count": reconstruction["duplicate_conflict_count"],
        "same_block_sanity_not_label": leakage["same_block_sanity_not_label"],
        "timestamp_horizon_primary_label": leakage["timestamp_horizon_primary_label"],
        "fixed_block_not_primary_label": leakage["fixed_block_not_primary_label"],
        "leakage_audit_passed": leakage["leakage_audit_passed"],
        "chronological_split_feasible": chronological["split_feasible"],
        "pool_disjoint_split_feasible": pool_split["split_feasible"],
        "token_disjoint_split_feasible": token_split["split_feasible"],
        "label_stability_passed": stability["label_stability_passed"],
        "sign_flip_count_5m_30m": stability["sign_flip_count"],
        "sign_flip_share_5m_30m": stability["sign_flip_share"],
        "recommended_primary_horizon": stability["recommended_primary_horizon"],
        "holdout_audit_passed": holdout_audit_passed,
        "allow_v3r29_small_timestamp_dataset_expansion": holdout_audit_passed,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    annotated = assign_rows(rows, chronological, pool_split, token_split)
    summary = {
        "run": "run004_v3r28_timestamp_horizon_leakage_holdout_audit",
        "research_only": True,
        "inputs": {
            "v3r27_summary": str(V3R27_SUMMARY.relative_to(ROOT)),
            "v3r27_rows": str(V3R27_ROWS.relative_to(ROOT)),
            "v3r26_summary": str(V3R26_SUMMARY.relative_to(ROOT)),
            "v3r24r_summary": str(V3R24R_SUMMARY.relative_to(ROOT)),
            "schema": str(SCHEMA_JSON.relative_to(ROOT)),
            "operations": str(OPERATIONS_MD.relative_to(ROOT)),
            "architecture": str(ARCHITECTURE_MD.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "project_contract_alignment": {
            "offline_training_chain_observed": "scripts/label_dashcam.py -> scripts/retrain_model.py" in operations
            or "scripts/label_dashcam.py" in architecture,
            "architecture_holdout_contract": "holdout 固定最新 7 天" in architecture,
            "no_training_authorization": True,
            "no_mainline_or_live_changes": True,
            "schema_version": schema.get("version"),
        },
        "prior_flags": {
            "v3r27": v3r27_summary.get("decision_flags", {}),
            "v3r26": v3r26_summary.get("decision_flags", {}),
            "v3r24r": v3r24r_summary.get("decision_flags", {}),
        },
        "dataset_reconstruction": reconstruction,
        "leakage_audit": leakage,
        "holdout_splits": {
            "chronological": chronological,
            "group_by_pool": pool_split,
            "group_by_token": token_split,
        },
        "label_stability": stability,
        "output_row_cap": {
            "output_rows": len(annotated),
            "row_cap": 100,
            "output_row_cap_respected": len(annotated) <= 100,
        },
        "decision_flags": flags,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_rows(annotated)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(
        {
            "rows": reconstruction["total_rows"],
            "chronological": chronological["split_feasible"],
            "pool_split": pool_split["split_feasible"],
            "token_split": token_split["split_feasible"],
            "sign_flips": stability["sign_flip_count"],
            "passed": holdout_audit_passed,
        }
    )


if __name__ == "__main__":
    main()
