#!/usr/bin/env python3
"""
Recompute duplicate metrics from output CSV only; preserve source-stage counts as diagnostics.

Does not modify the original output CSV, label/net_return, or original report file.
Reads original JSON + CSV; writes *_corrected* artifacts only.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DEFAULT_CSV = REPO / "logs" / "aero_vamm_canonical_label_dryrun_2000.csv"
DEFAULT_REPORT = REPO / "mvp" / "diagnostics" / "aero_vamm_canonical_label_dryrun_2000_report.json"
DEFAULT_MD = REPO / "mvp" / "runs" / "aero-vamm-canonical-label-dryrun-2000.md"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_canonical_label_dryrun_2000_report_corrected.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-canonical-label-dryrun-2000-corrected.md"


def norm_addr(v: str) -> str:
    t = (v or "").strip().lower()
    if not t:
        return ""
    return t if t.startswith("0x") else f"0x{t}"


def canonical_key(r: dict[str, str]) -> tuple[str, str, str]:
    return (
        (r.get("tx_hash") or "").strip().lower(),
        (r.get("log_index") or "").strip(),
        norm_addr(r.get("pool_address") or ""),
    )


def compute_output_duplicates(rows: list[dict[str, str]]) -> tuple[int, int, int]:
    """Returns duplicate_key_count, duplicate_conflict_count, unique_keys."""
    by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_key[canonical_key(r)].append(r)
    unique = len(by_key)
    dup_key_count = sum(len(g) - 1 for g in by_key.values() if len(g) > 1)
    conflict_fields = (
        "net_return",
        "label",
        "entry_block",
        "exit_block",
        "estimated_entry_quote",
        "estimated_exit_quote",
    )
    conflict_rows = 0
    for g in by_key.values():
        if len(g) < 2:
            continue
        any_conflict = False
        for fld in conflict_fields:
            vals = {(x.get(fld) or "").strip() for x in g}
            if len(vals) > 1:
                any_conflict = True
                break
        if any_conflict:
            conflict_rows += len(g) - 1
    return dup_key_count, conflict_rows, unique


def recompute_gate_duplicate(gates: dict, dup_conflict: int) -> dict:
    out = deepcopy(gates)
    if "duplicate_conflict_count_eq_0" in out:
        out["duplicate_conflict_count_eq_0"] = {"pass": dup_conflict == 0}
    return out


def all_gates_pass(gates: dict) -> bool:
    return all(v.get("pass") is True for v in gates.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--source-md", type=Path, default=DEFAULT_MD)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-md", type=Path, default=OUT_MD)
    args = ap.parse_args()

    with args.csv.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    dup_keys, dup_conflicts, unique_keys = compute_output_duplicates(rows)

    with args.report.open("r", encoding="utf-8") as f:
        report = json.load(f)

    source_dup_keys = report.get("duplicate_key_count")
    source_dup_conflicts = report.get("duplicate_conflict_count")

    corrected = deepcopy(report)
    corrected["duplicate_key_count"] = dup_keys
    corrected["duplicate_conflict_count"] = dup_conflicts
    corrected["source_candidate_duplicate_key_count"] = source_dup_keys
    corrected["source_candidate_duplicate_conflict_count"] = source_dup_conflicts
    corrected["source_candidate_duplicate_stats_diagnostics_only"] = True
    corrected["output_csv_duplicate_audit"] = {
        "row_count": len(rows),
        "unique_canonical_keys": unique_keys,
        "duplicate_key_count": dup_keys,
        "duplicate_conflict_count": dup_conflicts,
        "canonical_key": "tx_hash + log_index + pool_address",
    }

    corrected["gate_results"] = recompute_gate_duplicate(corrected["gate_results"], dup_conflicts)
    corrected["csv_canonical_trainable_later"] = all_gates_pass(corrected["gate_results"])
    corrected["conclusion_enum"] = "AERO_2000_REPORT_FIXED_BUT_HOLD_LOW_POSITIVE_RATE"
    corrected["report_correction"] = {
        "corrected_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "fix_description": (
            "duplicate_key_count and duplicate_conflict_count recomputed from output CSV only; "
            "former report values preserved as source_candidate_* (diagnostics_only, not gates)."
        ),
        "original_report_path": str(args.report.relative_to(REPO)),
        "original_conclusion_enum": report.get("conclusion_enum"),
    }
    corrected["artifacts"]["json_corrected"] = str(args.out_json.relative_to(REPO))
    corrected["artifacts"]["md_corrected"] = str(args.out_md.relative_to(REPO))

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(corrected, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Markdown: mirror original structure with corrected gates / duplicates
    md_lines = [
        "# Aero vAMM Canonical Label Dry-run 2000 (corrected report metrics)",
        "",
        "Status: research-only. Duplicate gate metrics recomputed from output CSV only.",
        "",
        "## Correction note",
        f"- Output CSV duplicate_key_count: **{dup_keys}**",
        f"- Output CSV duplicate_conflict_count: **{dup_conflicts}**",
        f"- Source-stage counts (diagnostics only, not gates): "
        f"source_candidate_duplicate_key_count={source_dup_keys}, "
        f"source_candidate_duplicate_conflict_count={source_dup_conflicts}",
        "",
        "## Core",
        f"- attempted/success/failed/success_rate: {corrected['attempted_rows']}/{corrected['success_rows']}/{corrected['failed_rows']}/{corrected['success_rate']}",
        f"- positive_count/negative_count/positive_rate: {corrected['positive_count']}/{corrected['negative_count']}/{corrected['positive_rate']}",
        f"- reserve_success_rate: {corrected['reserve_success_rate']}",
        f"- quote_success_rate: {corrected['quote_success_rate']}",
        f"- sanity_clip_count: {corrected['sanity_clip_count']}",
        "",
        "## Gates (corrected duplicate scope)",
        "```json",
        json.dumps(corrected["gate_results"], indent=2, sort_keys=True),
        "```",
        "",
        "## Conclusion",
        f"`{corrected['conclusion_enum']}`",
        "",
        f"- csv_canonical_trainable_later (all gates): **{corrected['csv_canonical_trainable_later']}**",
        "",
        "## Artifacts",
        f"- Corrected JSON: `{args.out_json.relative_to(REPO)}`",
        f"- This file: `{args.out_md.relative_to(REPO)}`",
        f"- Unchanged CSV: `{args.csv.relative_to(REPO)}`",
        f"- Original report (unchanged): `{args.report.relative_to(REPO)}`",
    ]
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(json.dumps({"wrote_json": str(args.out_json), "wrote_md": str(args.out_md)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
