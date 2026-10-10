# Base Data Source Contract

Chain: Base (`chain_id = 8453`)

## Envelope contract

All normalized records use the shared envelopes:

* `EventEnvelope`
* `TransactionEnvelope`
* `LogEnvelope`
* `TransferEnvelope`
* `SwapEnvelope`
* `PoolEnvelope`
* `LiquidityEnvelope`

Required common fields on event/log-shaped records:

```text
chain_id
block_number
block_hash
block_timestamp
transaction_hash
transaction_index
log_index
contract_address
topic0
raw_data
source_name
source_table
observed_at
ingested_at
point_in_time_available_at
provenance_hash
```

Rules:

* missing values use `UNKNOWN`, never blank/`0` coercion;
* failed transactions are retained;
* routers are not treated as economic principals by default;
* no write path to production storage.

## BaseSource interface

```text
capabilities()
explain(bounds) -> plan dict (always offline-safe)
iter_logs / iter_transactions / iter_events(bounds)
```

`QueryBounds` requires:

```text
start_block, end_block, limit, explicit_columns
max_threads = 1
statement_timeout_sec
max_bytes_to_read
dry_run / explain_only / checkpoint_path
```

## Adapter scaffolding (this round)

| adapter | status | connects? |
|---|---|---|
| `NullBaseSource` | implemented | no |
| `ClickHouseReadonlyAdapter` | empty scaffold | no — blocked by `database_access_enabled=false` and unimplemented transport |

No production endpoint, username, database name, or table name is hardcoded.

## Registry scaffold

Launchpads registered as candidates/planned:

* `clanker` (`v4`, `v4.1`) — factory/start_block UNKNOWN until verified
* `doppler` — planned
* `flaunch` — planned
