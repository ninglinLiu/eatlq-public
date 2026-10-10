# EATLQ Phase 1 Runbook

## 1. Fill secrets

```bash
cp infra/compose/.env.example infra/compose/.env
```

Required edits in `infra/compose/.env`:

- `EVM_WS_URL`: your QuickNode or Infura websocket endpoint
- `EVM_HTTP_URL`: your HTTPS JSON-RPC endpoint, usually the HTTP form of the same provider URL
- `LOG_FILTER_JSON`: the filter object passed to `eth_getLogs`
- `CLICKHOUSE_PASSWORD`
- `POSTGRES_PASSWORD`

Optional `archiver` recovery tuning:

- `ARCHIVER_PENDING_MIN_IDLE_MS`: minimum Redis pending idle time before startup reclaim, default `60000`
- `ARCHIVER_PENDING_RECLAIM_COUNT`: maximum pending messages reclaimed per `XAUTOCLAIM` pass, default `250`

Phase 1 now uses `newHeads + eth_getLogs` by default:

```dotenv
WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]
LOG_FILTER_JSON={"address":[],"topics":[]}
```

For QuickNode/Infura, keep the websocket side on `newHeads` and put all event filtering on the HTTP side:

```dotenv
LOG_FILTER_JSON={"topics":[["0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67","0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822","0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef","0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f","0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496","0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde","0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"]]}
```

## 2. Start the stack

```bash
./scripts/dev-up.sh
```

## 3. Initialize Redis consumer groups

```bash
./scripts/init-streams.sh
```

## 4. Verify services

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs ingestor
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs archiver
```

## 5. Verify Redis stream growth

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
```

You should see `length` increase after new block headers arrive and `ingestor` starts fetching logs for those blocks.

## 6. Verify ClickHouse schema

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq"
```

Expected tables:

- `raw_evm_logs`
- `raw_evm_logs_hourly`
- `raw_evm_logs_hourly_mv`

## 7. Verify archiver writes

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --tail=100 archiver
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count() FROM eatlq.raw_evm_logs"
```

You should see `archiver flushed records to clickhouse` in the logs and the row count should keep increasing.

To inspect consumer-group recovery health:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO GROUPS market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XPENDING market_ticks archiver_group
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO CONSUMERS market_ticks archiver_group
```

After `archiver` restarts, you should eventually see old pending messages reclaimed and `pending` trend down as recovered entries are inserted and acked.

## 8. Local Rust check

```bash
cargo check
cargo test -p ingestor
cargo test -p archiver
```

## 9. Manual backfill smoke test

Use this only after the normal stack is already healthy. The goal is to prove that a narrow range can be replayed without disturbing realtime `ingestor`.

Minimal run command:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=22000000 \
  -e BACKFILL_TO_BLOCK=22000002 \
  -e BACKFILL_CHUNK_SIZE=1 \
  backfill
```

If you want the worker to reuse checkpoint state in Postgres, keep `BACKFILL_STATE_POSTGRES_URL` configured in `infra/compose/.env`. If that variable is blank, the worker still runs but behaves statelessly and requires `BACKFILL_FROM_BLOCK`.

Recommended acceptance flow:

Step 1: Capture a baseline before the run:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count(), max(received_at_ms) FROM eatlq.raw_evm_logs"
```

Step 2: Run the narrow backfill command above.

Step 3: Inspect backfill logs:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=22000000 \
  -e BACKFILL_TO_BLOCK=22000002 \
  -e BACKFILL_CHUNK_SIZE=1 \
  backfill
```

Expected:

- logs include `starting backfill range scan`
- logs include one or more `backfill chunk fetched via eth_getLogs`
- the command exits on its own after the requested range completes

Step 4: Verify Redis and archiver after the run:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m archiver
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count(), max(received_at_ms) FROM eatlq.raw_evm_logs"
```

Expected:

- `market_ticks` `entries-added` is higher than the baseline if the range matched the filter
- `archiver` still shows `archiver flushed records to clickhouse`
- ClickHouse `count()` and `max(received_at_ms)` move forward if matching logs were emitted

Step 5: If stateful backfill is enabled, inspect checkpoint and gap state:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "TABLE app.backfill_jobs"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, job_key, from_block, to_block, status, last_completed_to_block FROM app.backfill_gap_ranges ORDER BY id DESC LIMIT 10"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT gap_id, chunk_from_block, chunk_to_block, fetched_logs_count FROM app.backfill_chunk_progress ORDER BY id DESC LIMIT 20"
```

Expected:

- `app.backfill_jobs.last_run_status` becomes `completed`
- `app.backfill_jobs.last_completed_to_block` advances to the last completed chunk
- `app.backfill_gap_ranges` records the requested range and ends in `completed`
- `app.backfill_chunk_progress` contains one row per completed chunk
- `app.ingestor_head_state` should gradually show `last_seen_head` and `last_fetched_head` after `ingestor` has been running with Postgres-backed state enabled

To enqueue a gap first and let a later worker claim it:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=22000000 \
  -e BACKFILL_TO_BLOCK=22000010 \
  backfill-enqueue-gap
```

Then run `backfill` without an explicit range. If `BACKFILL_AUTO_CLAIM_PENDING_GAP=true`, it will claim the oldest pending or failed gap for the same `BACKFILL_JOB_NAME`.

To discover candidate gaps from ClickHouse archived block numbers and enqueue them into Postgres:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_DISCOVERY_FROM_BLOCK=22000000 \
  -e BACKFILL_DISCOVERY_TO_BLOCK=22001000 \
  -e BACKFILL_DISCOVERY_MIN_GAP_SIZE=1 \
  -e BACKFILL_DISCOVERY_MAX_GAP_SIZE=5 \
  backfill-discover-gaps
```

Expected:

- logs include `completed ClickHouse-backed candidate gap discovery`
- new `pending` rows may appear in `app.backfill_gap_ranges`
- later `backfill` runs can claim those pending gaps automatically

Important limitation:

- `raw_evm_logs` only stores blocks where the current filter matched at least one log
- a missing block number in ClickHouse is only a candidate gap, not proof that ingestion missed data
- keep discovery ranges narrow and prefer dense pool/topic allowlists when using this workflow
- if `app.ingestor_head_state.last_fetched_head` exists, discovery uses that as its default upper bound to stay inside known realtime coverage

To run a background worker that continuously claims `pending` or `failed` gaps:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill up -d backfill-scheduler
```

If you just changed Rust code or the Dockerfile, add `--build` explicitly for that run.

To inspect it:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m backfill-scheduler
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --tail=50 backfill-scheduler
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps backfill-scheduler
```

Expected:

- logs include `backfill scheduler started`
- when work exists, logs include `scheduler claimed pending gap`
- each claimed gap is processed through the normal `backfill` flow
- when no work exists, the scheduler stays idle and polls again later

Minimal VPS acceptance:

1. pick a recent `ingestor` log line where `from_block == to_block` and `fetched_logs=0`
1. enqueue exactly that one block:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml \
  --profile backfill run --rm \
  -e BACKFILL_FROM_BLOCK=24662931 \
  -e BACKFILL_TO_BLOCK=24662931 \
  backfill-enqueue-gap
```

1. wait a little longer than `BACKFILL_SCHEDULER_POLL_INTERVAL_MS`, then inspect:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --tail=50 backfill-scheduler
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, job_key, from_block, to_block, status, claimed_at, run_started_at, run_finished_at, last_completed_to_block, last_error FROM app.backfill_gap_ranges ORDER BY id DESC LIMIT 5;"'
```

Acceptance result:

- the newest gap row moves from `pending` to `completed`
- `claimed_at`, `run_started_at`, and `run_finished_at` are filled
- `last_completed_to_block` equals the enqueued block
- `last_error` stays empty

## 10. Apply New ClickHouse DDL On Existing Volumes

If your ClickHouse container already has a long-lived volume, newly added files under `infra/clickhouse/init` do not apply automatically on restart.

Apply them manually:

```bash
bash scripts/apply-clickhouse-init.sh
```

If the ClickHouse volume already contains historical `raw_evm_logs`, project that history into the new normalized tables:

```bash
bash scripts/backfill-normalized-events.sh
```

That script now rebuilds each normalized target into a temporary table first, validates the row count, then atomically swaps it into place with `RENAME TABLE`. If the rebuild insert fails, the original table stays untouched.

Verify the normalized tables exist:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%transfers%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%swaps%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%mints%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%burns%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count() AS transfers, (SELECT count() FROM eatlq.uniswap_v2_swaps) AS v2_swaps, (SELECT count() FROM eatlq.uniswap_v3_swaps) AS v3_swaps, (SELECT count() FROM eatlq.uniswap_v2_mints) AS v2_mints, (SELECT count() FROM eatlq.uniswap_v2_burns) AS v2_burns, (SELECT count() FROM eatlq.uniswap_v3_mints) AS v3_mints, (SELECT count() FROM eatlq.uniswap_v3_burns) AS v3_burns FROM eatlq.erc20_transfers"
```

Verify the static registry views exist:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%registry%'"
```

Verify recent live parity between raw and normalized rows:

```bash
bash scripts/verify-normalized-events.sh
```

Inspect duplicate pressure and deduped row counts:

```bash
bash scripts/verify-normalized-dedupe.sh
```

Inspect concrete duplicate samples:

```bash
bash scripts/inspect-normalized-duplicates.sh
```

Inspect duplicate anomalies across all normalized surfaces from the audit views:

```bash
bash scripts/inspect-dedupe-anomalies.sh all 20
```

If one normalized surface shows duplicate drift and you want a narrower physical re-collapse than the full rebuild, rebuild only that surface:

```bash
bash scripts/rebuild-normalized-surface.sh v3-burn
```

Supported targets are `all`, `erc20`, `v2`, `v3`, `v2-mint`, `v2-burn`, `v3-mint`, and `v3-burn`.

Each target rebuild now uses the same temp-table + validation + atomic swap pattern, so operators no longer have to accept `TRUNCATE + INSERT` data-loss risk during manual remediation.

Inspect whether those duplicates already exist in `raw_evm_logs` and which `source` values contributed to them:

```bash
bash scripts/inspect-raw-duplicate-origins.sh v3 20
```

Inspect recent top token and pool activity from the derived hourly views:

```bash
bash scripts/inspect-normalized-activity.sh
bash scripts/inspect-normalized-activity.sh v3-mint 24 20
bash scripts/inspect-normalized-activity.sh v3-burn 24 20
```

Inspect the static registry contents:

```bash
bash scripts/inspect-registry.sh
```

Verify current normalized coverage against the static registry:

```bash
bash scripts/verify-registry-coverage.sh
```

Inspect uncovered addresses and venues outside the current curated registry:

```bash
bash scripts/inspect-registry-gaps.sh
```

Inspect ranked registry-expansion candidates aggregated across event families:

```bash
bash scripts/inspect-registry-candidates.sh
```

Inspect the enriched query views that join normalized rows to registry metadata:

```bash
bash scripts/inspect-enriched-events.sh
```

Run the consolidated Phase 2 healthcheck:

```bash
bash scripts/phase2-healthcheck.sh 30 24 10
```

This wraps the current raw heartbeat, recent raw-vs-normalized parity, dedupe summary, registry coverage, and top v2/v3 activity checks into one operator command.

Run the strict Phase 2 healthcheck:

```bash
bash scripts/phase2-healthcheck-strict.sh 30 300 1
```

This exits non-zero if raw archival is stale, recent raw-vs-normalized parity diverges, duplicate surplus appears, or lifecycle registry coverage regresses beyond the allowed threshold.

Inspect the queryable Phase 2 operational status views:

```bash
bash scripts/inspect-phase2-status.sh
```

This prints the raw archive heartbeat plus one row per normalized surface with raw-total, raw-projectable, raw-projectable-unique, raw-projectable-duplicate, raw-unprojectable, normalized, recent parity, duplicate surplus, registry gap, and latest timestamps.

Inspect the queryable Phase 2 alert views:

```bash
bash scripts/inspect-phase2-alerts.sh
bash scripts/inspect-phase2-alerts.sh critical
```

This prints active `critical` and `warning` alerts from `eatlq.phase2_alert_summary` and `eatlq.phase2_alert_events`, keeping historical unique projectable gaps separate from raw duplicate backlog and from historical raw rows that fail the current projection filter. Each alert row also includes a suggested remediation target and command.

Dry-run the rebuild-oriented remediations implied by current alerts:

```bash
bash scripts/remediate-phase2-alerts.sh
```

Apply those rebuild-oriented remediations and re-run validation:

```bash
bash scripts/remediate-phase2-alerts.sh warning --apply
```

This defaults to dry-run mode and only executes targeted rebuilds when `--apply` is explicitly provided. Alerts caused only by unprojectable historical raw rows are intentionally excluded from rebuild automation.

Run the minimal cron / monitoring entrypoint:

```bash
bash scripts/phase2-monitor-cron.sh 30 300 1
bash scripts/phase2-monitor-cron.sh 30 300 1 /opt/EATLQ/monitor/phase2-summary.txt
```

This prints a single-line summary for machine consumption, then prints the strict healthcheck output, and exits with the same code as `phase2-healthcheck-strict.sh`. The optional fourth argument writes the summary line to a file. The script only depends on standard shell utilities and does not require `python`.

Example crontab entry:

```bash
*/5 * * * * cd /opt/EATLQ && bash scripts/phase2-monitor-cron.sh 30 300 1 /opt/EATLQ/monitor/phase2-summary.txt >> /var/log/eatlq-phase2-monitor.log 2>&1
```

Inspect the ERC20 historical unprojectable raw backlog:

```bash
bash scripts/inspect-erc20-unprojectable-raw.sh 20
```

This breaks the backlog down by filter-failure reason, source, token, and recent samples so you can explain why those rows never entered `eatlq.erc20_transfers`. The current verified backlog is entirely `data_len` failures with `data = 0x`, sourced from `quicknode`, which strongly suggests non-ERC20 `Transfer`-signature collisions such as NFT-style logs rather than missed ERC20 value payloads.

## 11. Troubleshooting

- If `ingestor` stays unhealthy, check the websocket URL first. The healthcheck file is only written after a successful Redis flush.
- If Redis has no data, first verify that `WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]` is active, then verify `LOG_FILTER_JSON` is not over-restrictive.
- If `archiver` has no output, make sure `LOG_FILTER_JSON` matches active contracts/topics and that `raw_evm_logs` exists in ClickHouse.
- If ClickHouse init scripts do not appear, recreate the container and volume after checking the mounted init directory path.
- If `backfill` exits with `missing required environment variable: BACKFILL_FROM_BLOCK`, either set `BACKFILL_FROM_BLOCK` explicitly or enable Postgres-backed state with `BACKFILL_STATE_POSTGRES_URL` and a prior checkpoint or pending gap.
- If the Postgres state tables do not exist on an already-initialized volume, rerun the `backfill` worker once. It bootstraps the schema at runtime even if Docker init scripts were not re-applied.
