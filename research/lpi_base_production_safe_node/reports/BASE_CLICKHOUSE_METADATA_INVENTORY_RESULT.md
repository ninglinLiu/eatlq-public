# Base ClickHouse Metadata Inventory Result

```text
baseline_unchanged = a492897c409773bf23b4c8f33bdd2ccd4fff39cc
offline_closeout_unchanged = 344f3aef62cac0204aa25123a0f44933674a5839
implementation_commit = bbc1a8c8f540f8911312f6879c23c788a07c501b
implementation_tree = edfb2df59c311ed2b0f2948fb0e0dfa73dfc5558
implementation_parent_commit = ae207835ea704ec3fe70f0229b464db9b07f0e91
evidence_dir = evidence/base_metadata_inventory_20260717T141325Z
blocked_attempt_evidence = evidence/base_metadata_inventory_20260717T141129Z
execution_performed = true
business_table_access = false
explain_executed = false
rpc_http = false
metadata_inventory_completed = true
candidate_binding_available = true
ready_for_codex_business_probe_review = false
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
```

## Health

- before: `SAFE_TO_PROCEED` triggers=[]
- after: `BLOCKED_LIVE_PRIORITY` triggers=['clickhouse_active_merges=1']
- Post-inventory merge activity observed; no business probe started.

## Schema inventory summary

- app database confirmed: `eatlq`
- tables returned: 123
- columns returned: 2095
- candidates ranked: 123

## Primary logs candidate (schema only; data UNKNOWN)

`eatlq.raw_evm_logs` has the strongest Clanker-oriented field coverage (exact block_number/log_index/address/topics/data) but partition/sort keys use `received_at_ms`, not `block_number`.

```text
block_pruning_status = NOT_PRUNABLE
bounded_probe_safety_status = REJECT_OR_HOLD
```

Therefore the next Clanker 1000-block business probe must remain refused (or require a different, explicitly reviewed smaller-budget strategy). Metadata cannot prove Clanker content.

## Best eligible weak candidate

```json
{
  "activity_window_status": "UNKNOWN",
  "alias_match_fields": [
    "block_timestamp",
    "transaction_hash",
    "address"
  ],
  "block_pruning_status": "PARTIALLY_PRUNABLE",
  "bounded_probe_safety_status": "WEAK_CANDIDATE_UNVERIFIED_DATA",
  "candidate_role": "transfers",
  "clanker_presence_status": "UNKNOWN",
  "data_content_status": "UNKNOWN",
  "database": "eatlq",
  "engine": "MergeTree",
  "evidence_refs": [
    "evidence/base_metadata_inventory_20260717T141325Z"
  ],
  "exact_match_fields": [
    "block_number",
    "log_index"
  ],
  "factory_address_status": "UNKNOWN",
  "field_mappings": [
    {
      "matched_column": "block_number",
      "matched_type": "UInt64",
      "notes": "",
      "required": "block_number",
      "status": "exact"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "block_hash",
      "status": "missing"
    },
    {
      "matched_column": "block_time",
      "matched_type": "Nullable(DateTime64(3, \\'UTC\\'))",
      "notes": "name_alias_unverified_semantics",
      "required": "block_timestamp",
      "status": "alias_candidate"
    },
    {
      "matched_column": "tx_hash",
      "matched_type": "String",
      "notes": "name_alias_unverified_semantics",
      "required": "transaction_hash",
      "status": "alias_candidate"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "transaction_index",
      "status": "missing"
    },
    {
      "matched_column": "log_index",
      "matched_type": "UInt32",
      "notes": "",
      "required": "log_index",
      "status": "exact"
    },
    {
      "matched_column": "token_address",
      "matched_type": "LowCardinality(String)",
      "notes": "name_alias_unverified_semantics",
      "required": "address",
      "status": "alias_candidate"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "topic0",
      "status": "missing"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "topic1",
      "status": "missing"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "topic2",
      "status": "missing"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "topic3",
      "status": "missing"
    },
    {
      "matched_column": null,
      "matched_type": null,
      "notes": "",
      "required": "data",
      "status": "missing"
    }
  ],
  "missing_fields": [
    "block_hash",
    "transaction_index",
    "topic0",
    "topic1",
    "topic2",
    "topic3",
    "data"
  ],
  "partition_key": "toYYYYMM(received_at_ms)",
  "primary_key": "chain_id, token_address, block_number, tx_hash, log_index",
  "rejection_reasons": [
    "critical_fields_missing:topic0,data",
    "data_content_unverified",
    "adapter_runtime_transport_unimplemented"
  ],
  "sampling_key": "",
  "schema_fingerprint": "88d19908849d02dc44322b26c4815cc3b8ed01acfddd03e2045d8ca404585705",
  "score": 44,
  "sorting_key": "chain_id, token_address, block_number, tx_hash, log_index",
  "storage_policy": "default",
  "table": "erc20_transfers",
  "unknown_fields": []
}
```

## Queries executed (metadata/health only)

```json
[
  {
    "command_sanitized": {
      "argv_sanitized": [
        "timeout",
        "5",
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--readonly=2",
        "--max_threads=1",
        "--max_execution_time=3",
        "--max_result_rows=5000",
        "--max_result_bytes=2097152",
        "--max_bytes_to_read=4194304",
        "--format=TSVWithNames",
        "--query_id=lpi_meta_q_sys_tables_columns_1784297607",
        "--query=<hash:576bb12f3b3ccefe1411690655f7363eae8bd4da91e517267dac4c6cd0e9a095>"
      ],
      "query_hash": "576bb12f3b3ccefe1411690655f7363eae8bd4da91e517267dac4c6cd0e9a095",
      "query_id": "lpi_meta_q_sys_tables_columns_1784297607",
      "self_query_marker": "q_sys_tables_columns",
      "settings": {
        "max_bytes_to_read": 4194304,
        "max_execution_time": 3,
        "max_result_bytes": 2097152,
        "max_result_rows": 5000,
        "max_threads": 1,
        "readonly": 2
      },
      "sql": "SELECT database, table, name, type, position FROM system.columns WHERE database = 'system' AND table = 'tables' LIMIT 200"
    },
    "elapsed_sec": 0.2777,
    "ended_utc": "2026-07-17T14:13:27Z",
    "exit_code": 0,
    "hit_byte_cap": false,
    "hit_row_cap": false,
    "output_bytes": 1466,
    "query_hash": "576bb12f3b3ccefe1411690655f7363eae8bd4da91e517267dac4c6cd0e9a095",
    "query_id_runtime": "lpi_meta_q_sys_tables_columns_1784297607",
    "row_count": 34,
    "started_utc": "2026-07-17T14:13:27Z",
    "stderr": "",
    "stdout_sha256": "d02979d4d058ba6607ba9259dfec44fce05042a9aad694af6fe5a822ad848559",
    "timed_out": false
  },
  {
    "command_sanitized": {
      "argv_sanitized": [
        "timeout",
        "5",
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--readonly=2",
        "--max_threads=1",
        "--max_execution_time=3",
        "--max_result_rows=5000",
        "--max_result_bytes=2097152",
        "--max_bytes_to_read=4194304",
        "--format=TSVWithNames",
        "--query_id=lpi_meta_q_sys_columns_columns_1784297607",
        "--query=<hash:2a940f9a883315e89d5095cf7cba4bae4cbc5217fea885b681ac9011ca282b06>"
      ],
      "query_hash": "2a940f9a883315e89d5095cf7cba4bae4cbc5217fea885b681ac9011ca282b06",
      "query_id": "lpi_meta_q_sys_columns_columns_1784297607",
      "self_query_marker": "q_sys_columns_columns",
      "settings": {
        "max_bytes_to_read": 4194304,
        "max_execution_time": 3,
        "max_result_bytes": 2097152,
        "max_result_rows": 5000,
        "max_threads": 1,
        "readonly": 2
      },
      "sql": "SELECT database, table, name, type, position FROM system.columns WHERE database = 'system' AND table = 'columns' LIMIT 200"
    },
    "elapsed_sec": 0.2705,
    "ended_utc": "2026-07-17T14:13:27Z",
    "exit_code": 0,
    "hit_byte_cap": false,
    "hit_row_cap": false,
    "output_bytes": 919,
    "query_hash": "2a940f9a883315e89d5095cf7cba4bae4cbc5217fea885b681ac9011ca282b06",
    "query_id_runtime": "lpi_meta_q_sys_columns_columns_1784297607",
    "row_count": 21,
    "started_utc": "2026-07-17T14:13:27Z",
    "stderr": "",
    "stdout_sha256": "2e3be68d9e45c0317d6ae2c6c8aca67a62f9fb19b8e8ec5ca0e96417c7142dc7",
    "timed_out": false
  },
  {
    "command_sanitized": {
      "argv_sanitized": [
        "timeout",
        "5",
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--readonly=2",
        "--max_threads=1",
        "--max_execution_time=3",
        "--max_result_rows=5000",
        "--max_result_bytes=2097152",
        "--max_bytes_to_read=4194304",
        "--format=TSVWithNames",
        "--query_id=lpi_meta_q_app_databases_1784297607",
        "--query=<hash:ef8ebf0f06afe43a3e0bc700e1c898f2650a69c063cbf9e55648fd9c67848ac0>"
      ],
      "query_hash": "ef8ebf0f06afe43a3e0bc700e1c898f2650a69c063cbf9e55648fd9c67848ac0",
      "query_id": "lpi_meta_q_app_databases_1784297607",
      "self_query_marker": "q_app_databases",
      "settings": {
        "max_bytes_to_read": 4194304,
        "max_execution_time": 3,
        "max_result_bytes": 2097152,
        "max_result_rows": 5000,
        "max_threads": 1,
        "readonly": 2
      },
      "sql": "SELECT name, engine FROM system.databases WHERE name = 'eatlq' LIMIT 10"
    },
    "elapsed_sec": 0.4221,
    "ended_utc": "2026-07-17T14:13:27Z",
    "exit_code": 0,
    "hit_byte_cap": false,
    "hit_row_cap": false,
    "output_bytes": 25,
    "query_hash": "ef8ebf0f06afe43a3e0bc700e1c898f2650a69c063cbf9e55648fd9c67848ac0",
    "query_id_runtime": "lpi_meta_q_app_databases_1784297607",
    "row_count": 1,
    "started_utc": "2026-07-17T14:13:27Z",
    "stderr": "",
    "stdout_sha256": "14cc0bf85a8d7ec20c0c326d7e7743cb9e15bf344546179592c4507517bae261",
    "timed_out": false
  },
  {
    "command_sanitized": {
      "argv_sanitized": [
        "timeout",
        "5",
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--readonly=2",
        "--max_threads=1",
        "--max_execution_time=3",
        "--max_result_rows=5000",
        "--max_result_bytes=2097152",
        "--max_bytes_to_read=4194304",
        "--format=TSVWithNames",
        "--query_id=lpi_meta_q_app_tables_1784297607",
        "--query=<hash:d1bb5be32b694ff1118a605fb176cfa4904776e4d9bd4418ad9983f2597f793b>"
      ],
      "query_hash": "d1bb5be32b694ff1118a605fb176cfa4904776e4d9bd4418ad9983f2597f793b",
      "query_id": "lpi_meta_q_app_tables_1784297607",
      "self_query_marker": "q_app_tables",
      "settings": {
        "max_bytes_to_read": 4194304,
        "max_execution_time": 3,
        "max_result_bytes": 2097152,
        "max_result_rows": 5000,
        "max_threads": 1,
        "readonly": 2
      },
      "sql": "SELECT database, name, engine, partition_key, sorting_key, primary_key, sampling_key, storage_policy FROM system.tables WHERE database = 'eatlq' LIMIT 5000"
    },
    "elapsed_sec": 0.4411,
    "ended_utc": "2026-07-17T14:13:28Z",
    "exit_code": 0,
    "hit_byte_cap": false,
    "hit_row_cap": false,
    "output_bytes": 8147,
    "query_hash": "d1bb5be32b694ff1118a605fb176cfa4904776e4d9bd4418ad9983f2597f793b",
    "query_id_runtime": "lpi_meta_q_app_tables_1784297607",
    "row_count": 123,
    "started_utc": "2026-07-17T14:13:27Z",
    "stderr": "",
    "stdout_sha256": "70b921369c674f54fd06cfeb83f0ec37fd961303580296e82b4e687e6829c03c",
    "timed_out": false
  },
  {
    "command_sanitized": {
      "argv_sanitized": [
        "timeout",
        "5",
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--readonly=2",
        "--max_threads=1",
        "--max_execution_time=3",
        "--max_result_rows=5000",
        "--max_result_bytes=2097152",
        "--max_bytes_to_read=4194304",
        "--format=TSVWithNames",
        "--query_id=lpi_meta_q_app_columns_1784297608",
        "--query=<hash:298793590c964ebb527813382a60ba6ff275ec20747fd6df3dd812fb1fa6394b>"
      ],
      "query_hash": "298793590c964ebb527813382a60ba6ff275ec20747fd6df3dd812fb1fa6394b",
      "query_id": "lpi_meta_q_app_columns_1784297608",
      "self_query_marker": "q_app_columns",
      "settings": {
        "max_bytes_to_read": 4194304,
        "max_execution_time": 3,
        "max_result_bytes": 2097152,
        "max_result_rows": 5000,
        "max_threads": 1,
        "readonly": 2
      },
      "sql": "SELECT database, table, name, type, position, default_kind, default_expression, is_in_partition_key, is_in_sorting_key, is_in_primary_key, is_in_sampling_key FROM system.columns WHERE database = 'eatlq' LIMIT 5000"
    },
    "elapsed_sec": 0.3775,
    "ended_utc": "2026-07-17T14:13:28Z",
    "exit_code": 0,
    "hit_byte_cap": false,
    "hit_row_cap": false,
    "output_bytes": 145564,
    "query_hash": "298793590c964ebb527813382a60ba6ff275ec20747fd6df3dd812fb1fa6394b",
    "query_id_runtime": "lpi_meta_q_app_columns_1784297608",
    "row_count": 2095,
    "started_utc": "2026-07-17T14:13:28Z",
    "stderr": "",
    "stdout_sha256": "860a94d7cc04587bdff2d7d71cf7f389a56287324ca436eea76007160e9d1b4d",
    "timed_out": false
  }
]
```

## Golden / UNKNOWN

```text
on_chain_golden = NONE
clanker_presence = UNKNOWN
factory_address = UNKNOWN
activity_block_window = UNKNOWN
data_content = UNKNOWN
```
