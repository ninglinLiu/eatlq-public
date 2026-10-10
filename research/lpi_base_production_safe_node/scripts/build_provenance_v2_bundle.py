#!/usr/bin/env python3
"""Build v2 correction Evidence Bundle from the CURRENT sealed HEAD.

Must be run only after the sealed-runner implementation commit is created.
Does not modify original inventory evidence or v1 correction bundle.
Offline only — no Docker/Redis/ClickHouse.
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

ORIG_BUNDLE = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.tar.gz"
V1_CORR_BUNDLE = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z_lineage_correction.tar.gz"
SUPERSEDING = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z.superseding_provenance.json"
EV = ROOT / "evidence" / "base_metadata_inventory_20260717T141325Z"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"], text=True).strip()
    porcelain = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True)
    if porcelain.strip():
        raise SystemExit("refuse: worktree dirty; commit implementation first")

    # Ensure generator itself is in HEAD
    ls = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-tree", "HEAD", "--", "scripts/build_provenance_v2_bundle.py"],
        text=True,
    )
    if not ls.strip():
        raise SystemExit("refuse: generator not in HEAD tree")

    prov = json.loads(SUPERSEDING.read_text(encoding="utf-8"))
    stamp = "20260717T141325Z_lineage_correction_v2"
    out_dir = ROOT / "evidence" / f"base_metadata_inventory_{stamp}"
    if out_dir.exists():
        raise SystemExit(f"refuse overwrite {out_dir}")
    out_dir.mkdir(parents=True)

    # Copy superseding provenance and annotate with v2 generator identity
    v2_prov = dict(prov)
    v2_prov.update(
        {
            "correction_bundle_version": "v2",
            "generator_script": "scripts/build_provenance_v2_bundle.py",
            "generator_source_commit": head,
            "generator_source_tree_hash": tree,
            "superseded_correction_bundle_sha256": sha256(V1_CORR_BUNDLE),
            "upstream_original_bundle_sha256": sha256(ORIG_BUNDLE),
            "v2_created_at_utc": utc_now(),
            "recommended_business_probe": "NONE",
            "on_chain_golden": "NONE",
            "database_access": False,
            "rpc_access": False,
            "http_access": False,
            "business_table_access": False,
        }
    )
    (out_dir / "superseding_provenance_v2.json").write_text(
        json.dumps(v2_prov, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "round_id": "metadata_lineage_correction_v2",
                "artifact_class": "BASE_METADATA_LINEAGE_CORRECTION_V2",
                "source_commit": head,
                "source_tree_hash": tree,
                "created_at": utc_now(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Pin unchanged raw hashes
    raw = {p.name: sha256(p) for p in sorted((EV / "query_results").iterdir()) if p.is_file()}
    (out_dir / "upstream_raw_query_result_sha256.json").write_text(
        json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    staging = ROOT / "var" / "tmp" / f"bundle_stage_{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for p in out_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            dest = staging / p.relative_to(out_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

    bundle = ROOT / "evidence" / f"base_metadata_inventory_{stamp}.tar.gz"
    quality = {
        "correction_type": "SUPERSEDING_EXECUTION_PROVENANCE_V2",
        "upstream_original_bundle_sha256": sha256(ORIG_BUNDLE),
        "superseded_correction_bundle_sha256": sha256(V1_CORR_BUNDLE),
        "upstream_execution_source_state": "UNSEALED_WORKTREE",
        "upstream_execution_source_commit": "UNKNOWN",
        "upstream_execution_source_tree_hash": "UNKNOWN",
        "post_execution_sealed_commit": "bbc1a8c8f540f8911312f6879c23c788a07c501b",
        "post_execution_sealed_tree": "edfb2df59c311ed2b0f2948fb0e0dfa73dfc5558",
        "code_equivalence_to_post_execution_commit": "UNPROVEN",
        "raw_query_results_unchanged": True,
        "original_bundle_unchanged": True,
        "production_state_mutated": False,
        "database_access": False,
        "rpc_access": False,
        "http_access": False,
        "business_table_access": False,
        "explain_executed": False,
        "recommended_business_probe": "NONE",
        "on_chain_golden": "NONE",
        "supersedes": {
            "original_bundle_sha256": sha256(ORIG_BUNDLE),
            "v1_correction_bundle_sha256": sha256(V1_CORR_BUNDLE),
        },
    }
    manifest = RemoteEvidenceBundleExporter(ROOT).export(
        staging_dir=staging,
        output_tar=bundle,
        bundle_id=f"base_metadata_inventory_{stamp}",
        source_code_commit=head,
        source_tree_hash=tree,
        unknown_count=1,
        golden_results={"on_chain_golden": "NONE", "status": "no_chain_golden"},
        quality_results=quality,
        source_tables=list(prov["source_tables"]),
        source_query_hashes=list(prov["source_query_hashes"]),
        health_before={"note": "see upstream health_before.json; not re-queried"},
        health_after={"status": "BLOCKED_LIVE_PRIORITY", "note": "upstream post-health; not re-queried"},
    )
    verify_bundle_archive(archive_path=bundle, workspace_root=ROOT)

    # Verify original artifacts unchanged
    assert sha256(ORIG_BUNDLE) == "77366faf4955f53adb85c21b81169d100a38529552a31540428be37d23cf232d"
    assert sha256(V1_CORR_BUNDLE) == "155d7a9dea3bc80d977b9d74ba71649e9bc26c6811d3f5febf18d6c11ff67a49"
    assert manifest.source_tables
    assert len(manifest.source_query_hashes) == 9
    assert manifest.source_code_commit == head

    print(f"out_dir={out_dir}")
    print(f"bundle={bundle}")
    print(f"bundle_sha256={sha256(bundle)}")
    print(f"source_code_commit={head}")
    print(f"source_tree_hash={tree}")
    print(f"source_tables={manifest.source_tables}")
    print(f"source_query_hashes_n={len(manifest.source_query_hashes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
