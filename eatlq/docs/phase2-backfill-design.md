# EATLQ Phase 2 Backfill Design

## Goal

Add a dedicated backfill path for missed block ranges without changing the stable Phase 1 runtime:

`newHeads websocket -> eth_getLogs HTTP pull -> ingestor -> Redis Stream market_ticks -> archiver -> ClickHouse raw_evm_logs`

The backfill worker must stay separate from the realtime `ingestor` loop so that missed-range recovery does not destabilize the `newHeads` path that is already verified on the VPS.

## Constraints

- Do not switch back to `eth_subscribe logs`
- Keep `market_ticks` as the durable handoff into `archiver`
- Keep the existing `archiver` rule: write ClickHouse first, then `XACK`
- Keep `received_at_ms` as the guaranteed archival clock
- Avoid broad refactors to services already running on the VPS

## First Implemented Shape

The first Phase 2 increment is a dedicated one-shot worker named `backfill`.

It reuses the same HTTP `eth_getLogs` filter and the same Redis stream writer as `ingestor`, but it does not connect to the websocket and it does not change the realtime pipeline.

Runtime shape:

`backfill range scan -> eth_getLogs HTTP pull -> Redis Stream market_ticks -> archiver -> ClickHouse raw_evm_logs`

## Operator Model

The initial worker is intentionally operator-driven.

Required input:

- `BACKFILL_FROM_BLOCK`

Optional input:

- `BACKFILL_TO_BLOCK`
- `BACKFILL_CHUNK_SIZE`
- `BACKFILL_CONFIRMATIONS`
- `BACKFILL_PAUSE_MS`
- `BACKFILL_SOURCE_SUFFIX`

Resolution rules:

- If `BACKFILL_TO_BLOCK` is set, the worker scans exactly `from..=to`
- If `BACKFILL_TO_BLOCK` is not set, the worker resolves `eth_blockNumber` at startup and uses `latest - BACKFILL_CONFIRMATIONS`
- The worker scans in fixed-size inclusive chunks and writes decoded `IngestRecordV1` records into `market_ticks`
- The worker exits when the requested range is finished

## Why Manual Range Input First

This step solves the most urgent gap, which is the lack of any independent missed-range recovery path.

It does not yet attempt automatic gap discovery from Redis, Postgres, or ClickHouse because those choices affect state ownership and dedupe strategy. The manual range model gives operations a safe recovery lever now while preserving the current stable service boundaries.

## Data Semantics

- Backfilled records reuse the existing `IngestRecordV1` contract
- `received_at_ms` is assigned when the backfill worker emits the record
- `source` is tagged with a suffix such as `quicknode-backfill` so downstream analysis can distinguish replayed data from realtime data
- Records still flow through Redis and `archiver`, so the durability boundary remains unchanged

## Dedupe And Overlap

This first worker does not add global dedupe.

Operational rule for now:

- run backfill only for known missed ranges
- avoid overlapping previously archived ranges unless duplicate raw rows are acceptable

The recommended future dedupe key remains:

`(chain_id, tx_hash, log_index, address, topic0)`

That key should be enforced when we introduce either:

- a normalized deduped table in ClickHouse
- or a stateful checkpoint and replay ledger in Postgres

## Failure Model

- If the worker crashes mid-range, already flushed records stay durable in Redis and can still be archived by the existing pipeline
- Unsent later chunks can be retried by rerunning the worker with a narrower range
- Because the worker is separate from `ingestor`, failures here do not interrupt the realtime `newHeads` loop

## Compose Entry Point

A profile-gated `backfill` service is available in `infra/compose/compose.yaml`.

It is not started by normal `docker compose up`.

Example:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=22000000 \
  -e BACKFILL_TO_BLOCK=22000100 \
  backfill
```

Or let the worker resolve the upper bound from the current tip:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=22000000 \
  -e BACKFILL_CONFIRMATIONS=12 \
  backfill
```

## Acceptance

The implementation is acceptable when:

- `cargo check` passes
- `cargo test -p ingestor` passes
- the regular `ingestor` service still builds and runs unchanged
- running the `backfill` binary writes records into `market_ticks`
- `archiver` continues inserting those records into `raw_evm_logs`

## Checkpoint And Gap State

The worker now supports an optional Postgres-backed state layer through `BACKFILL_STATE_POSTGRES_URL`.

When enabled, the worker bootstraps and uses three tables:

- `app.backfill_jobs`
- `app.backfill_gap_ranges`
- `app.backfill_chunk_progress`
- `app.ingestor_head_state`

Current behavior:

- every run is attached to a logical job key derived from `CHAIN_ID` and `BACKFILL_JOB_NAME`
- operators can enqueue a gap explicitly through the `backfill_enqueue_gap` binary or the `backfill-enqueue-gap` compose service
- if a pending or failed gap exists for that job and `BACKFILL_AUTO_CLAIM_PENDING_GAP=true`, the worker claims that gap first
- if no explicit `BACKFILL_FROM_BLOCK` is provided, the worker can resume from `last_completed_to_block + 1`
- each completed chunk records progress and advances the checkpoint
- each run finishes by marking the gap `completed` or `failed`
- the realtime `ingestor` can also persist `last_seen_head` and `last_fetched_head` as best-effort state without affecting the live pipeline on failure

This gives Phase 2 a durable replay ledger without changing the realtime ingestion path.

## Pending Gap Scheduler

A dedicated `backfill_scheduler` binary is now available for long-running pending-gap processing.

Its role is intentionally narrow:

- poll Postgres for the oldest `pending` or `failed` gap for the configured `BACKFILL_JOB_NAME`
- claim exactly one gap at a time
- run the existing backfill flow for that explicit range
- sleep when no work is available

Important behavior:

- it does not invent new ranges
- it does not auto-resume from checkpoint when no pending gap exists
- it is safe to keep separate from the realtime `ingestor`

Key config:

- `BACKFILL_STATE_POSTGRES_URL`
- `BACKFILL_JOB_NAME`
- `BACKFILL_SCHEDULER_POLL_INTERVAL_MS`

This path has now been validated on the VPS with a real enqueue -> claim -> complete flow.

## Current Limits

- gap rows can now be discovered from ClickHouse and enqueued, but this is still heuristic
- no global raw-log dedupe is enforced yet
- checkpoint state is per logical backfill job, not yet per topic or address family

Why heuristic:

- `raw_evm_logs` only contains blocks where the current filter matched at least one log
- a missing block number in ClickHouse can mean either "ingestion gap" or "the filter matched nothing in that block"
- the discovery step therefore only enqueues candidate gaps and should be used with dense allowlists or narrow operator-selected windows
- when `app.ingestor_head_state.last_fetched_head` exists, discovery uses it as the default upper bound so it does not scan beyond known realtime coverage

## Candidate Gap Discovery

A dedicated `backfill_discover_gaps` binary can inspect archived `raw_evm_logs` block numbers and enqueue candidate gaps into `app.backfill_gap_ranges`.

Inputs:

- `BACKFILL_DISCOVERY_CLICKHOUSE_URL`
- `BACKFILL_DISCOVERY_CLICKHOUSE_DB`
- `BACKFILL_DISCOVERY_CLICKHOUSE_USER`
- `BACKFILL_DISCOVERY_CLICKHOUSE_PASSWORD`
- `BACKFILL_DISCOVERY_CLICKHOUSE_TABLE`
- `BACKFILL_DISCOVERY_FROM_BLOCK`
- `BACKFILL_DISCOVERY_TO_BLOCK`
- `BACKFILL_DISCOVERY_MIN_GAP_SIZE`
- `BACKFILL_DISCOVERY_MAX_GAP_SIZE`
- `BACKFILL_DISCOVERY_INCLUDE_EDGE_GAPS`

Behavior:

- reads distinct archived `block_number` values from ClickHouse for the requested range
- identifies missing internal ranges between adjacent archived block numbers
- optionally includes leading and trailing edge gaps
- enqueues those ranges into Postgres as `pending` gaps
- leaves already `completed` ranges intact if the same exact range is rediscovered

This is designed as an operator aid, not as a final fully automated reliability guarantee.

## Next Increment

The next step should build on the state layer rather than replace it:

1. feed candidate gaps from monitoring or operator tooling with better expected-coverage hints
2. project raw logs into downstream normalized event tables for strategy-friendly reads
3. persist a dedicated high-water mark for realtime heads so discovery is not inferred only from event-bearing blocks
4. introduce stronger dedupe in downstream normalized tables

See `docs/phase2-normalized-events-design.md` for the first normalized-table rollout.

That normalized rollout has now been validated on the VPS for:

- historical projection from `raw_evm_logs`
- live recent-window parity between raw rows and normalized rows
