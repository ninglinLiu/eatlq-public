# Clanker First Bounded Probe Authorization Dossier

This dossier does **not** authorize execution.

```text
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
execution_performed_metadata_only = true
recommend_next_codex_action = hold_business_probe_best_logs_not_block_prunable
clanker_data_presence = UNKNOWN
factory_address = UNKNOWN
activity_block_window = UNKNOWN
golden_on_chain = NONE
```

## Why business probe stays closed

1. Best logs-shaped table `eatlq.raw_evm_logs` is **NOT_PRUNABLE** by `block_number` (partitioned by `received_at_ms`).
2. Post-inventory health observed an active ClickHouse merge.
3. No on-chain Golden; content/factory/window remain UNKNOWN.
4. Adapter runtime transport remains unimplemented; `config/defaults.json` access switches remain false.

## Primary logs candidate (reject/hold for block-bounded probe)

```json
{
  "activity_window_status": "UNKNOWN",
  "alias_match_fields": [
    "block_timestamp",
    "transaction_hash"
  ],
  "block_pruning_status": "NOT_PRUNABLE",
  "bounded_probe_safety_status": "REJECT_OR_HOLD",
  "candidate_role": "logs",
  "clanker_presence_status": "UNKNOWN",
  "data_content_status": "UNKNOWN",
  "database": "eatlq",
  "engine": "MergeTree",
  "evidence_refs": [
    "evidence/base_metadata_inventory_20260717T141325Z"
  ],
  "exact_match_fields": [
    "block_number",
    "log_index",
    "address",
    "topic0",
    "topic1",
    "topic2",
    "topic3",
    "data"
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
      "matched_column": "address",
      "matched_type": "LowCardinality(String)",
      "notes": "",
      "required": "address",
      "status": "exact"
    },
    {
      "matched_column": "topic0",
      "matched_type": "String",
      "notes": "",
      "required": "topic0",
      "status": "exact"
    },
    {
      "matched_column": "topic1",
      "matched_type": "String",
      "notes": "",
      "required": "topic1",
      "status": "exact"
    },
    {
      "matched_column": "topic2",
      "matched_type": "String",
      "notes": "",
      "required": "topic2",
      "status": "exact"
    },
    {
      "matched_column": "topic3",
      "matched_type": "String",
      "notes": "",
      "required": "topic3",
      "status": "exact"
    },
    {
      "matched_column": "data",
      "matched_type": "String",
      "notes": "",
      "required": "data",
      "status": "exact"
    }
  ],
  "missing_fields": [
    "block_hash",
    "transaction_index"
  ],
  "partition_key": "toYYYYMM(received_at_ms)",
  "primary_key": "chain_id, received_at_ms, tx_hash, log_index",
  "rejection_reasons": [
    "block_not_prunable",
    "data_content_unverified",
    "adapter_runtime_transport_unimplemented"
  ],
  "sampling_key": "",
  "schema_fingerprint": "dd5d0ef9b530892b0a3e031c05b4c11c07df2b1c0aacc6bb0d4ec5e926e202cb",
  "score": 111,
  "sorting_key": "chain_id, received_at_ms, tx_hash, log_index",
  "storage_policy": "default",
  "table": "raw_evm_logs",
  "unknown_fields": []
}
```

## Weak eligible non-logs candidate (not sufficient for Clanker log probe)

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

## Counterexamples (sample REJECT_OR_HOLD)

```json
[
  {
    "activity_window_status": "UNKNOWN",
    "alias_match_fields": [
      "block_timestamp",
      "transaction_hash"
    ],
    "block_pruning_status": "NOT_PRUNABLE",
    "bounded_probe_safety_status": "REJECT_OR_HOLD",
    "candidate_role": "logs",
    "clanker_presence_status": "UNKNOWN",
    "data_content_status": "UNKNOWN",
    "database": "eatlq",
    "engine": "MergeTree",
    "evidence_refs": [
      "evidence/base_metadata_inventory_20260717T141325Z"
    ],
    "exact_match_fields": [
      "block_number",
      "log_index",
      "address",
      "topic0",
      "topic1",
      "topic2",
      "topic3",
      "data"
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
        "matched_column": "address",
        "matched_type": "LowCardinality(String)",
        "notes": "",
        "required": "address",
        "status": "exact"
      },
      {
        "matched_column": "topic0",
        "matched_type": "String",
        "notes": "",
        "required": "topic0",
        "status": "exact"
      },
      {
        "matched_column": "topic1",
        "matched_type": "String",
        "notes": "",
        "required": "topic1",
        "status": "exact"
      },
      {
        "matched_column": "topic2",
        "matched_type": "String",
        "notes": "",
        "required": "topic2",
        "status": "exact"
      },
      {
        "matched_column": "topic3",
        "matched_type": "String",
        "notes": "",
        "required": "topic3",
        "status": "exact"
      },
      {
        "matched_column": "data",
        "matched_type": "String",
        "notes": "",
        "required": "data",
        "status": "exact"
      }
    ],
    "missing_fields": [
      "block_hash",
      "transaction_index"
    ],
    "partition_key": "toYYYYMM(received_at_ms)",
    "primary_key": "chain_id, received_at_ms, tx_hash, log_index",
    "rejection_reasons": [
      "block_not_prunable",
      "data_content_unverified",
      "adapter_runtime_transport_unimplemented"
    ],
    "sampling_key": "",
    "schema_fingerprint": "dd5d0ef9b530892b0a3e031c05b4c11c07df2b1c0aacc6bb0d4ec5e926e202cb",
    "score": 111,
    "sorting_key": "chain_id, received_at_ms, tx_hash, log_index",
    "storage_policy": "default",
    "table": "raw_evm_logs",
    "unknown_fields": []
  },
  {
    "activity_window_status": "UNKNOWN",
    "alias_match_fields": [
      "block_timestamp",
      "transaction_hash"
    ],
    "block_pruning_status": "VERIFIED_METADATA_PRUNABLE",
    "bounded_probe_safety_status": "REJECT_OR_HOLD",
    "candidate_role": "unknown",
    "clanker_presence_status": "UNKNOWN",
    "data_content_status": "UNKNOWN",
    "database": "eatlq",
    "engine": "ReplacingMergeTree",
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
        "matched_column": null,
        "matched_type": null,
        "notes": "",
        "required": "address",
        "status": "missing"
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
      "address",
      "topic0",
      "topic1",
      "topic2",
      "topic3",
      "data"
    ],
    "partition_key": "intDiv(block_number, 1000000)",
    "primary_key": "chain_id, protocol, block_number, tx_hash, log_index",
    "rejection_reasons": [
      "candidate_role_unknown",
      "critical_fields_missing:address,topic0,data",
      "data_content_unverified",
      "adapter_runtime_transport_unimplemented"
    ],
    "sampling_key": "",
    "schema_fingerprint": "abd23a53f62d747c93be3744537d17ca634d73884fd1f5f7badafe126eec2e4c",
    "score": 61,
    "sorting_key": "chain_id, protocol, block_number, tx_hash, log_index",
    "storage_policy": "default",
    "table": "base_cha_wallet_swaps",
    "unknown_fields": []
  },
  {
    "activity_window_status": "UNKNOWN",
    "alias_match_fields": [
      "block_timestamp"
    ],
    "block_pruning_status": "VERIFIED_METADATA_PRUNABLE",
    "bounded_probe_safety_status": "REJECT_OR_HOLD",
    "candidate_role": "unknown",
    "clanker_presence_status": "UNKNOWN",
    "data_content_status": "UNKNOWN",
    "database": "eatlq",
    "engine": "ReplacingMergeTree",
    "evidence_refs": [
      "evidence/base_metadata_inventory_20260717T141325Z"
    ],
    "exact_match_fields": [
      "block_number"
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
        "matched_colu
```
