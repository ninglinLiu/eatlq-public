# Codex Review Request — ClickHouse Metadata Inventory

Cursor completed the authorized metadata-only schema inventory round.

## Identity

```text
baseline_unchanged = a492897c409773bf23b4c8f33bdd2ccd4fff39cc
offline_closeout_unchanged = 344f3aef62cac0204aa25123a0f44933674a5839
implementation_commit = bbc1a8c8f540f8911312f6879c23c788a07c501b
implementation_tree = edfb2df59c311ed2b0f2948fb0e0dfa73dfc5558
handoff_parent = bbc1a8c8f540f8911312f6879c23c788a07c501b
docs_commit = 69e80af0e0cd940d299d6aea034d19064b7cf106
```

## Outcome

```text
metadata_inventory_completed = true
candidate_binding_available = true
ready_for_codex_business_probe_review = false
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
business_table_access = false
explain_executed = false
rpc_http = false
```

## Evidence

```text
primary = evidence/base_metadata_inventory_20260717T141325Z
blocked_pre_gate_attempt = evidence/base_metadata_inventory_20260717T141129Z
bundle = evidence/base_metadata_inventory_20260717T141325Z_postprocess.tar.gz
offline_tests = reports/raw/offline_tests_20260717T141452Z
```

## Key finding

`eatlq.raw_evm_logs` is the strongest logs-shaped schema match but
`block_pruning_status = NOT_PRUNABLE` (partition/sort on `received_at_ms`).
Recommend holding Clanker 1000-block business probe.

## Reports

- reports/BASE_CLICKHOUSE_METADATA_INVENTORY_RESULT.md
- reports/BASE_METADATA_QUERY_HEALTH_COMPARISON.md
- reports/BASE_TABLE_BINDING_CANDIDATES.md
- reports/CLANKER_FIRST_BOUNDED_PROBE_AUTHORIZATION_DOSSIER.md
- config/base_table_bindings.candidate.json (runtime_enabled=false)

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL.
