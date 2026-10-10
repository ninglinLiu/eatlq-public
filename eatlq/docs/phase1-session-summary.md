# EATLQ Phase 1 Session Summary

## Final Status

Phase 1 is now running as a stable single-node ingestion and archival stack on the VPS.

The live path is:

`newHeads websocket -> eth_getLogs HTTP pull -> ingestor -> Redis Stream market_ticks -> archiver -> ClickHouse raw_evm_logs`

Verified outcomes:

- `ingestor` stays connected and processes each new block
- Redis `market_ticks` grows again
- `archiver` writes to ClickHouse again
- historical Redis pending entries were reduced from `771` to `2` after startup recovery

## What Was Delivered

### Infrastructure

- `redis`, `clickhouse`, `postgres`, `ingestor`, and `archiver` are defined in `infra/compose/compose.yaml`
- Redis uses AOF persistence
- ClickHouse initializes the raw log table and hourly materialized view
- Postgres is present as the future OLTP and strategy state store

### Ingestor

- Connects to the EVM websocket with `tokio-tungstenite`
- Subscribes to `eth_subscribe` with `["newHeads"]`
- Uses HTTP JSON-RPC `eth_getLogs` per block to fetch filtered logs
- Normalizes records into `IngestRecordV1`
- Encodes payloads with `rmp-serde`
- Writes MessagePack payloads into Redis `market_ticks`

### Archiver

- Reads `market_ticks` through Redis consumer group `archiver_group`
- Decodes `payload_msgpack` into `IngestRecordV1`
- Maps records into ClickHouse rows
- Inserts into `raw_evm_logs`
- Only `XACK`s after ClickHouse persistence succeeds
- Reclaims idle pending entries on startup with `XAUTOCLAIM`

### ClickHouse

- `raw_evm_logs`
- `raw_evm_logs_hourly`
- `raw_evm_logs_hourly_mv`
- first normalized Phase 2 tables can now be layered on top of `raw_evm_logs`:
  - `erc20_transfers`
  - `uniswap_v2_swaps`
  - `uniswap_v3_swaps`

The schema now treats `received_at_ms` as the guaranteed archival clock for Phase 1, with `block_time` remaining nullable for later enrichment.

## Major Problems And Fixes

### 1. `rustls` crypto provider panic

Symptom:

- `ingestor` crashed at startup with a `CryptoProvider` panic from `rustls 0.23`

Resolution:

- Added `rustls = { version = "0.23", features = ["ring"] }`
- Installed the `ring` provider during process startup
- Logged duplicate provider installation as a warning instead of failing

### 2. ClickHouse TTL and archive schema mismatch

Symptom:

- `raw_evm_logs` creation failed because TTL was applied directly to `DateTime64(3)`
- the original schema assumed non-null `block_time`, which is not guaranteed at ingest time

Resolution:

- fixed the TTL expression
- changed the schema to archive against `received_at_ms`
- made `block_time` nullable
- kept `block_time` available for later RPC enrichment

### 3. Websocket instability with `eth_subscribe logs`

Symptom:

- QuickNode repeatedly closed the websocket with `upstream went away`
- full-chain or broad logs subscriptions were not sustainable on the current endpoint tier

Resolution:

- moved the runtime entrypoint to `newHeads + eth_getLogs`
- kept the websocket stream lightweight
- moved log filtering onto HTTP block pulls

This was the key architecture adjustment that made the VPS deployment stable.

### 4. Review-driven websocket robustness issues

Symptom:

- the reader loop could exit on subscribe send failure
- internal backpressure could stall websocket polling
- subscription confirmation payloads could become junk downstream records

Resolution:

- subscribe send failures now fall back into reconnect logic
- `tx.send(...).await` became `tx.try_send(...)`
- full raw channels drop frames with warnings instead of stalling
- non-subscription payloads are filtered out before decoding
- expected confirmation payloads are now logged at low noise level

### 5. `archiver` startup failure due to missing env defaults

Symptom:

- `archiver` started with blank env values and crashed with `InvalidNumber`

Resolution:

- added safe defaults for all `ARCHIVER_*` values directly in `compose.yaml`
- kept `.env.example` aligned with those defaults

### 6. Redis pending entries accumulated after restarts

Symptom:

- Redis showed `lag=0` but `pending=771`
- new data still flowed, but old delivered messages remained stuck in the PEL

Resolution:

- added startup-time `XAUTOCLAIM` recovery in `archiver`
- reclaimed idle pending entries
- reinserted them into ClickHouse
- acknowledged them after persistence

Verified result:

- `pending` dropped from `771` to `2` while `lag` stayed at `0`

## Key Design Decisions Beyond The Whitepaper

### 1. `received_at_ms` is the Phase 1 archival clock

- `IngestRecordV1.received_at_ms` is guaranteed for every record
- ClickHouse partitioning, ordering, and TTL use `received_at_ms`
- `block_time` stays nullable until richer enrichment exists

This lets Phase 1 run now instead of waiting for a full timestamp backfill subsystem.

### 2. `newHeads + eth_getLogs` is the practical Phase 1 ingestion mode

- websocket only carries block notifications
- HTTP carries filtered log retrieval
- filtering is configured through `LOG_FILTER_JSON`

This is the main operational compromise that made the pipeline reliable on constrained RPC plans.

### 3. Deliberate frame loss is safer than websocket stall

- `ws.rs` now drops frames when the raw channel is full
- this avoids blocking websocket polling and triggering provider disconnects

For this latency-sensitive stack, graceful degradation is preferable to reader deadlock.

### 4. Redis consumer-group semantics are explicit

- `market_ticks` is the durable handoff between ingestion and archival
- ClickHouse insert happens before `XACK`
- old pending entries are reclaimed on startup if they are idle long enough

This provides a simple but durable handoff model without Kafka or a more complex broker.

### 5. `blockTimestamp` is used opportunistically

- some live log payloads include `blockTimestamp`
- `archiver` extracts it from `payload_json` when present
- extracted values are stored into nullable `block_time`

This reduces Phase 2 enrichment pressure.

## Current Known Gaps

These are acceptable for the current phase:

- no dead-letter queue yet
- no dedicated replay or backfill worker yet
- no metrics stack yet
- `value_usd` remains `NULL`
- filtering is curated and manual, not dynamic
- no pool registry refresh job yet
- no richer strategy-facing feature store yet beyond the first normalized event tables

## Current Recommended Mainnet Runtime Shape

- websocket subscription: `WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]`
- HTTP log pull filter: `LOG_FILTER_JSON={...}`
- use a narrow allowlist of high-liquidity Uniswap V2 and V3 pools first
- only add broader transfer coverage after stability is confirmed

## Recommended Next Steps

### Immediate

- rotate the exposed QuickNode endpoint if the current one was shared in logs
- clean up the VPS `.env` so it contains the latest `EVM_HTTP_URL`, `LOG_FILTER_JSON`, and `ARCHIVER_PENDING_*` keys explicitly
- keep observing ClickHouse row growth over longer windows such as 10 to 30 minutes

### Phase 2 Candidate Work

- add a dedicated backfill worker for missed block ranges using `eth_getLogs`
- expand topic and address allowlists more systematically
- enrich and harden the normalized strategy-friendly event tables
- add a metrics stack and alerting
- begin the feature-engine or signal-generation layer

## Recommended VPS Verification Commands

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m ingestor
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m archiver
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO GROUPS market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XPENDING market_ticks archiver_group
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count(), max(received_at_ms) FROM eatlq.raw_evm_logs"
```
