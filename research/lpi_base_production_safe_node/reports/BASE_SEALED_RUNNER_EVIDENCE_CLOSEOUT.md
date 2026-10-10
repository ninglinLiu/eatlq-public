# Base Sealed Runner and Evidence Provenance Closeout

Codex verdict on `4d55d80`: `PASS_WITH_CORRECTIONS`.

This round is offline only. No Docker/Redis/ClickHouse/RPC/HTTP/business-table access.

## Identity

```text
baseline_unchanged = a492897c409773bf23b4c8f33bdd2ccd4fff39cc
lineage_correction_commit = 4d55d807f9880dbc065707b76beb44afaaccc38b
implementation_commit = 79a70607bb183d902170a9dcb24744ea805c353f
implementation_tree = 7c6f925bbe21bb0b3772adf37c3dcc8db87c0176
evidence_handoff_commit = 77bfbaefe78ad38d828c82b4058e317b5d3a25e0
```

## Fixes

1. Run-owned output allowlist after initial clean-worktree seal (no self-block on evidence dir).
2. Docker inspect, Redis XINFO, and ClickHouse system queries all use `ExternalCommandExecutor`.
3. Critical-path digest includes `command_executor.py` and `runner_status.py`.
4. Placeholder blob-mismatch test replaced with real coverage.
5. Stub integration coverage for executor routing and exit codes 10/30/31.
6. Provenance Bundle v2 with non-empty `source_tables` / `source_query_hashes` / supersedes.

## Bundle hashes

```text
original_bundle_sha256 = 77366faf4955f53adb85c21b81169d100a38529552a31540428be37d23cf232d
v1_correction_bundle_sha256 = 155d7a9dea3bc80d977b9d74ba71649e9bc26c6811d3f5febf18d6c11ff67a49
v2_correction_bundle_sha256 = ae5125e0cc28c71ebce57ff18da4454c6555f415146092955261d4719048e60d
```

## Gates

```text
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
ready_for_codex_business_probe_review = false
recommended_business_probe = NONE
metadata_only_schema_inventory_authorized = false
production_adjacent_commands_this_round = false
original_raw_modified = false
```
