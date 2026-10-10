#!/usr/bin/env python3
"""Offline-only lineage correction for the 20260717T141325Z metadata inventory.

Does NOT modify original raw query results, health JSON, command log, or original bundles.
Does NOT access ClickHouse/Redis/Docker.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/lpi_base_production_safe_node")
sys.path.insert(0, str(ROOT))

from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.verify import verify_bundle_archive
from launchpad_intelligence.query.metadata_ranking import rank_tables
from launchpad_intelligence.query.runner_status import historical_141325_correction

EV = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z"
CORR_DIR = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z_lineage_correction"
ORIG_BUNDLE = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.tar.gz"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_tsv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        rows.append({headers[i]: (parts[i] if i < len(parts) else "") for i in range(len(headers))})
    return rows


def main() -> int:
    if CORR_DIR.exists():
        raise SystemExit(f"refuse overwrite {CORR_DIR}")
    CORR_DIR.mkdir(parents=True)

    # Snapshot original raw hashes (must remain unchanged after this script).
    raw_paths = sorted((EV / "query_results").iterdir())
    original_raw_hashes = {p.name: sha256(p) for p in raw_paths if p.is_file() and not p.is_symlink()}
    tracked = {
        "query_plans.json": sha256(EV / "query_plans.json"),
        "command_log_sanitized.json": sha256(EV / "command_log_sanitized.json"),
        "health_before.json": sha256(EV / "health_before.json"),
        "health_after.json": sha256(EV / "health_after.json"),
        "candidates_ranked.json": sha256(EV / "candidates_ranked.json"),
        "run_metadata.json": sha256(EV / "run_metadata.json"),
        "q_app_tables.tsv": original_raw_hashes["q_app_tables.tsv"],
        "q_app_columns.tsv": original_raw_hashes["q_app_columns.tsv"],
        "original_bundle.tar.gz": sha256(ORIG_BUNDLE),
        "original_bundle.tar.gz.sha256": sha256(Path(str(ORIG_BUNDLE) + ".sha256")),
        "original_bundle.tar.gz.manifest.sha256": sha256(Path(str(ORIG_BUNDLE) + ".manifest.sha256")),
    }

    log = json.loads((EV / "command_log_sanitized.json").read_text(encoding="utf-8"))
    entries = log["entries"]
    all_hashes = [e["query_hash"] for e in entries]
    # 0,1 health before; 2-6 schema; 7,8 health after
    health_hashes = [all_hashes[i] for i in (0, 1, 7, 8)]
    metadata_hashes = [all_hashes[i] for i in (2, 3, 4, 5, 6)]
    source_tables = sorted(
        {
            "system.processes",
            "system.merges",
            "system.columns",
            "system.databases",
            "system.tables",
        }
    )
    window_start = entries[0]["started_utc"]
    window_end = entries[-1]["ended_utc"]

    hist = historical_141325_correction()

    provenance = {
        "correction_type": "SUPERSEDING_EXECUTION_PROVENANCE",
        "correction_created_at_utc": utc_now(),
        "does_not_modify_original_raw_bytes": True,
        "does_not_requery_database": True,
        "supersedes_bundle_sha256": tracked["original_bundle.tar.gz"],
        "original_manifest_sha256_sidecar": tracked["original_bundle.tar.gz.manifest.sha256"],
        "original_archive_detached_sha256_sidecar": tracked["original_bundle.tar.gz.sha256"],
        "execution_source_state": "UNSEALED_WORKTREE",
        "execution_source_commit": "UNKNOWN",
        "execution_source_tree_hash": "UNKNOWN",
        "recorded_head_during_execution": "ae207835ea704ec3fe70f0229b464db9b07f0e91",
        "recorded_head_did_not_contain_runner": True,
        "execution_source_commit_recorded": "ae207835ea704ec3fe70f0229b464db9b07f0e91",
        "execution_source_commit_contains_runner": False,
        "post_execution_sealed_commit": "bbc1a8c8f540f8911312f6879c23c788a07c501b",
        "post_execution_sealed_tree": "edfb2df59c311ed2b0f2948fb0e0dfa73dfc5558",
        "code_equivalence_to_post_execution_commit": "UNPROVEN",
        "equivalence_to_post_execution_commit": "UNPROVEN",
        "inference_note": (
            "Working-tree contents were likely later sealed as bbc1a8c, "
            "but this is inference only and must not be treated as verified execution lineage."
        ),
        "raw_query_results_unchanged": True,
        "raw_query_results_modified": False,
        "original_bundle_modified": False,
        "query_texts_and_settings_recoverable": True,
        "business_table_access": False,
        "explain_executed": False,
        "rpc_http": False,
        "source_tables": source_tables,
        "source_query_hashes": all_hashes,
        "metadata_query_hashes": metadata_hashes,
        "health_query_hashes": health_hashes,
        "query_window_start_utc": window_start,
        "query_window_end_utc": window_end,
        "original_artifact_sha256": tracked,
        "original_query_result_sha256": original_raw_hashes,
        "historical_runner_exit_correction": hist,
        "health_after_note": (
            "health_after.json records merge=1; query_results/health_merges.tsv retains only "
            "pre-health stdout (post-health stdout was hashed in command log only)."
        ),
        "post_health_status": "BLOCKED_LIVE_PRIORITY",
        "consumers_must_prefer_this_provenance_over_original_bundle_source_commit": True,
    }

    prov_path = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.superseding_provenance.json"
    if prov_path.exists():
        raise SystemExit(f"refuse overwrite {prov_path}")
    prov_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Offline re-rank with fingerprint v0.2 (does not touch original candidates_ranked.json)
    tables = parse_tsv(EV / "query_results" / "q_app_tables.tsv")
    columns = parse_tsv(EV / "query_results" / "q_app_columns.tsv")
    ranked = [c.to_dict() for c in rank_tables(tables_rows=tables, columns_rows=columns, evidence_refs=[str(EV.relative_to(ROOT))])]
    ranked_path = CORR_DIR / "candidates_ranked_v0_2.json"
    ranked_path.write_text(json.dumps({"candidates": ranked, "schema_fingerprint_version": "lpi.schema_fingerprint.v0.2.0"}, indent=2, sort_keys=True) + "\n")

    # Update candidate binding offline (still disabled)
    logs = [c for c in ranked if c.get("candidate_role") == "logs"]
    elig = [c for c in ranked if c.get("bounded_probe_safety_status") in {"CANDIDATE_UNVERIFIED_DATA", "WEAK_CANDIDATE_UNVERIFIED_DATA"}]
    best_logs = logs[0] if logs else None
    best_elig = elig[0] if elig else None
    cand = {
        "status": best_elig.get("bounded_probe_safety_status") if best_elig else "NO_SAFE_CANDIDATE",
        "runtime_enabled": False,
        "business_query_authorized": False,
        "source": "clickhouse_system_metadata",
        "review_required": True,
        "primary_logs_candidate": best_logs,
        "best_eligible_candidate": best_elig,
        "data_content_status": "UNKNOWN",
        "clanker_presence_status": "UNKNOWN",
        "factory_address_status": "UNKNOWN",
        "activity_window_status": "UNKNOWN",
        "schema_fingerprint_version": "lpi.schema_fingerprint.v0.2.0",
        "evidence_dir": str(EV.relative_to(ROOT)),
        "lineage_correction_dir": str(CORR_DIR.relative_to(ROOT)),
        "recommended_business_probe": "NONE",
    }
    if best_elig:
        cand.update(
            {
                "database": best_elig["database"],
                "table": best_elig["table"],
                "engine": best_elig["engine"],
                "partition_key": best_elig["partition_key"],
                "sorting_key": best_elig["sorting_key"],
                "primary_key": best_elig["primary_key"],
                "candidate_role": best_elig["candidate_role"],
                "block_pruning_status": best_elig["block_pruning_status"],
                "schema_fingerprint": best_elig.get("schema_fingerprint_v0_2") or best_elig.get("schema_fingerprint"),
                "legacy_schema_fingerprint_v0_1": best_elig.get("legacy_schema_fingerprint_v0_1"),
            }
        )
    (ROOT / "config" / "base_table_bindings.candidate.json").write_text(
        json.dumps(cand, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Alternatives decision (sealed metadata only)
    alts = {
        "recommended_business_probe": "NONE",
        "ready_for_codex_business_probe_review": False,
        "strategies": {
            "A_direct_block_filter_raw_evm_logs": {
                "status": "REJECT",
                "reasons": [
                    "block_number not in partition/sort/primary key",
                    "partition_key=toYYYYMM(received_at_ms)",
                    "LIMIT cannot prevent large reads",
                    "max_bytes_to_read is last-resort insurance only",
                ],
            },
            "B_received_at_ms_plus_block_predicate": {
                "status": "CONDITIONAL_DESIGN",
                "requires": ["trusted_block_to_time_window_evidence"],
                "current_block_window": "UNKNOWN",
                "clanker_activity": "UNKNOWN",
                "executable_query_generated": False,
            },
            "C_erc20_transfers": {
                "status": "NOT_A_LOG_SOURCE",
                "reasons": ["missing topic0..3/data", "cannot close Clanker event identification loop"],
            },
            "D_views_mv_dictionary": {
                "status": "NAME_LIST_ONLY_UNVERIFIED",
                "note": "names from sealed system.tables only; view SQL/underlying pruning UNKNOWN",
                "names": sorted(
                    r["name"]
                    for r in tables
                    if r.get("engine") in {"View", "MaterializedView", "Dictionary"}
                ),
            },
            "E_create_projection_index_or_table": {
                "status": "PRODUCTION_MUTATION_FORBIDDEN",
                "executable": False,
            },
            "F_rpc_backfill": {
                "status": "OUT_OF_SCOPE_NOT_AUTHORIZED",
                "executable": False,
            },
        },
        "required_new_evidence_before_reconsideration": [
            "verified_clanker_factory_and_version_ranges",
            "verified_activity_block_window",
            "safe_block_to_time_mapping_or_block_prunable_log_source",
            "fresh_live_health_safe_window",
            "sealed_commit_before_query",
        ],
    }
    (CORR_DIR / "clanker_safe_path_alternatives.json").write_text(
        json.dumps(alts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Copy provenance into correction dir and build correction bundle
    shutil.copy2(prov_path, CORR_DIR / "superseding_provenance.json")
    (CORR_DIR / "original_raw_hashes.json").write_text(
        json.dumps({"query_results": original_raw_hashes, "tracked": tracked}, indent=2, sort_keys=True) + "\n"
    )
    (CORR_DIR / "run_metadata.json").write_text(
        json.dumps(
            {
                "round_id": "metadata_lineage_correction",
                "artifact_class": "BASE_METADATA_LINEAGE_CORRECTION",
                "source_commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                "created_at": utc_now(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    corr_sha = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.correction.sha256"
    lines = []
    for p in sorted(CORR_DIR.rglob("*")):
        if p.is_file() and not p.is_symlink():
            lines.append(f"{sha256(p)}  {p.relative_to(ROOT)}")
    lines.append(f"{sha256(prov_path)}  {prov_path.relative_to(ROOT)}")
    corr_sha.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stamp = "20260717T141325Z_lineage_correction"
    staging = ROOT / "var" / "tmp" / f"bundle_stage_{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for p in CORR_DIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            dest = staging / p.relative_to(CORR_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    # also include superseding provenance at top level
    (staging / "superseding_provenance.json").write_bytes(prov_path.read_bytes())
    bundle = ROOT / "evidence" / f"base_metadata_inventory_{stamp}.tar.gz"
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"], text=True).strip()
    RemoteEvidenceBundleExporter(ROOT).export(
        staging_dir=staging,
        output_tar=bundle,
        bundle_id=f"base_metadata_inventory_{stamp}",
        source_code_commit=head,
        source_tree_hash=tree,
        unknown_count=1,
        golden_results={"on_chain_golden": "NONE", "correction_only": True},
    )
    verify_bundle_archive(archive_path=bundle, workspace_root=ROOT)

    # Verify originals untouched
    for name, digest in original_raw_hashes.items():
        assert sha256(EV / "query_results" / name) == digest, name
    assert sha256(ORIG_BUNDLE) == tracked["original_bundle.tar.gz"]

    print(f"corr_dir={CORR_DIR}")
    print(f"provenance={prov_path}")
    print(f"correction_sha256={corr_sha}")
    print(f"bundle={bundle}")
    print(f"bundle_sha256={sha256(bundle)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
