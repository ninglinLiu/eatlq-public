# Base Evidence Provenance V2 Result

v2 correction Bundle is generated only from a clean committed HEAD that contains
the generator. It does not rewrite original inventory bytes or the v1 correction Bundle.

## Upstream lineage (unchanged facts)

```text
upstream_execution_source_state = UNSEALED_WORKTREE
upstream_execution_source_commit = UNKNOWN
upstream_execution_source_tree_hash = UNKNOWN
post_execution_sealed_commit = bbc1a8c8f540f8911312f6879c23c788a07c501b
equivalence = UNPROVEN
original_bundle_sha256 = 77366faf4955f53adb85c21b81169d100a38529552a31540428be37d23cf232d
v1_correction_bundle_sha256 = 155d7a9dea3bc80d977b9d74ba71649e9bc26c6811d3f5febf18d6c11ff67a49
```

## v2 artifacts

```text
v2_evidence_dir = evidence/base_metadata_inventory_20260717T141325Z_lineage_correction_v2/
v2_bundle = evidence/base_metadata_inventory_20260717T141325Z_lineage_correction_v2.tar.gz
v2_bundle_sha256 = ae5125e0cc28c71ebce57ff18da4454c6555f415146092955261d4719048e60d
v2_source_code_commit = 79a70607bb183d902170a9dcb24744ea805c353f
v2_source_tree_hash = 7c6f925bbe21bb0b3772adf37c3dcc8db87c0176
```

Manifest includes non-empty `source_tables` (5 system tables) and 9 `source_query_hashes`,
plus `quality_results.supersedes` linking original and v1 correction hashes.
