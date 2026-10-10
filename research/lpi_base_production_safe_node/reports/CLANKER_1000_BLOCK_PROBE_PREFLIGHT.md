# Clanker 1000-Block Probe Preflight

```text
status = PREFLIGHT_READY_NOT_EXECUTED
execution_authorized = false
business_table_queried = false
```

## Purpose

Plan the first Clanker-oriented 1000-block readonly probe without binding or querying production business tables.

## Candidate (logical, unbound)

```text
candidate_table = logical.base_logs_candidate
```

This name is intentionally logical. It is **not** mapped to a ClickHouse production table in this round.

## Required fields

```text
block_number
block_hash
block_timestamp
transaction_hash
transaction_index
log_index
address
topic0
topic1
topic2
topic3
data
```

## Partition key

```text
partition_key = block_number
```

## Block range

```text
start_block = 20000000
end_block   = 20000999
span        = 1000 blocks
```

Note: `20000000` is a planning placeholder, not a verified Clanker activity window. Real start/end must be chosen from a future approved data inventory.

## Limits and budgets

```text
LIMIT = 1000
max_threads = 1
statement_timeout_sec = 10
max_bytes_to_read = 8388608
estimated_read_bytes = 4096000
readonly = true
dry_run = true
explain_only = true
checkpoint = /root/lpi_base_production_safe_node/checkpoints/clanker_1000_probe.ckpt.json
```

## Stop conditions

* statement_timeout
* max_bytes_to_read
* limit_reached
* health_guard_trip
* access_defaults_closed

## How full-table scan is prevented

* explicit column projection (no `SELECT *`)
* `block_number` lower/upper predicate
* bounded 1000-block window
* `LIMIT 1000`
* `max_threads=1`
* `max_bytes_to_read=8MiB`
* 10s statement timeout
* offline preflight rejects mutating SQL
* access defaults keep `database_access_enabled=false`

## SQL skeleton (not executed)

```sql
SELECT block_number, block_hash, block_timestamp, transaction_hash, transaction_index,
       log_index, address, topic0, topic1, topic2, topic3, data
FROM logical.base_logs_candidate
WHERE block_number >= 20000000
  AND block_number <= 20000999
LIMIT 1000
```

## Machine-readable artifact

```text
reports/raw/clanker_1000_block_probe_preflight.json
plan_hash = 9b6d19e1c43548d28953107dfe4b38c1366c47ae4f458c92fd60baf5a8f448db
```

## Execute denial proof

`--execute` returned exit code `3` with `AccessDeniedError` while defaults remain closed.

## Next approval needed before probe

1. bind logical candidate to a verified readonly table/view;
2. confirm partition key and columns via system metadata only if approved;
3. choose a real Clanker-relevant block window;
4. pass live health preflight;
5. set `ready_for_bounded_database_probe=true` only after Codex/owner review.
