# Clanker Safe Data Path Alternatives

Offline decision package from sealed system metadata only.
No new database/RPC access. No executable business probe generated.

## Decision

```text
recommended_business_probe = NONE
ready_for_codex_business_probe_review = false
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
```

## Accepted facts (not reopened)

- `eatlq.raw_evm_logs` is the strongest logs-shaped schema match
- partition/sort on `received_at_ms`; `block_number` not a pruning key
- `block_pruning_status = NOT_PRUNABLE` for pure block windows
- `eatlq.erc20_transfers` is a weak transfers candidate only
- Clanker presence / factory / version ranges / activity window = UNKNOWN

## Strategy evaluations

### A. Direct `block_number` filter on `raw_evm_logs`

`REJECT` — not in partition/sort/primary key; LIMIT/`max_bytes_to_read` do not make it safe.

### B. `received_at_ms` partition + `block_number` predicate

`CONDITIONAL_DESIGN` only — requires verified block→time window evidence first.
Current activity window UNKNOWN. No executable query generated.

### C. `erc20_transfers`

`NOT_A_LOG_SOURCE` — missing topics/data; cannot close Clanker event identification.

### D. Existing View / MaterializedView / Dictionary

Name list from sealed `system.tables` only. Underlying tables, refresh, and pruning remain `UNKNOWN`.
Name similarity is not evidence.

### E. Create projection / index / new table

`PRODUCTION_MUTATION_FORBIDDEN` — not suggested for execution in this phase.

### F. RPC backfill

`OUT_OF_SCOPE_NOT_AUTHORIZED`.

## Required before reconsideration

```text
verified_clanker_factory_and_version_ranges
verified_activity_block_window
safe_block_to_time_mapping_or_block_prunable_log_source
fresh_live_health_safe_window
sealed_commit_before_query
```

Machine-readable copy: `evidence/base_metadata_inventory_20260717T141325Z_lineage_correction/clanker_safe_path_alternatives.json`
