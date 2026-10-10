# EATLQ Phase 2 Normalized Events Design

## Goal

Add strategy-friendlier ClickHouse tables on top of `eatlq.raw_evm_logs` without changing the stable ingestion pipeline.

The runtime handoff stays:

`newHeads websocket -> eth_getLogs HTTP pull -> ingestor -> Redis Stream market_ticks -> archiver -> ClickHouse raw_evm_logs`

The normalized layer is a downstream projection, not a replacement for the raw table.

## Why This Comes Now

Phase 2 already has:

- stable realtime ingestion
- durable raw archival
- backfill state
- pending-gap scheduling

The next useful step is to make the archived data easier to query for strategy work, operations, and debugging.

## First Implemented Scope

The first normalized cut only covers event families that are already part of the current practical filter profile:

- ERC-20 `Transfer`
- Uniswap V2 `Swap`
- Uniswap V2 `Mint`
- Uniswap V2 `Burn`
- Uniswap V3 `Swap`
- Uniswap V3 `Mint`
- Uniswap V3 `Burn`

This is deliberate.

We are not pretending the system currently captures all possible DEX or token events. We are projecting the events we already ingest reliably.

## Tables

New ClickHouse tables:

- `eatlq.erc20_transfers`
- `eatlq.uniswap_v2_swaps`
- `eatlq.uniswap_v2_mints`
- `eatlq.uniswap_v2_burns`
- `eatlq.uniswap_v3_swaps`
- `eatlq.uniswap_v3_mints`
- `eatlq.uniswap_v3_burns`

Each table is fed by a materialized view from `eatlq.raw_evm_logs`.

New safer query views:

- `eatlq.erc20_transfers_deduped`
- `eatlq.uniswap_v2_swaps_deduped`
- `eatlq.uniswap_v2_mints_deduped`
- `eatlq.uniswap_v2_burns_deduped`
- `eatlq.uniswap_v3_swaps_deduped`
- `eatlq.uniswap_v3_mints_deduped`
- `eatlq.uniswap_v3_burns_deduped`

These views group on the recommended normalized key and keep the latest row by `ingested_at`, expose that timestamp as `latest_ingested_at`, and also expose `duplicate_rows` so operators can measure duplicate pressure.

New derived hourly activity views:

- `eatlq.erc20_transfers_hourly`
- `eatlq.uniswap_v2_swaps_hourly`
- `eatlq.uniswap_v2_mints_hourly`
- `eatlq.uniswap_v2_burns_hourly`
- `eatlq.uniswap_v3_swaps_hourly`
- `eatlq.uniswap_v3_mints_hourly`
- `eatlq.uniswap_v3_burns_hourly`

These aggregate deduped rows into per-hour activity counts so operators can inspect recent active tokens and pools without rewriting the same aggregation each time.

## Normalization Rules

Shared rules:

- keep `received_at_ms` as the archival clock
- keep `block_time` nullable
- keep `payload_json` for traceability
- normalize addresses to lowercase hex strings
- keep the raw event key shape available through `chain_id`, `tx_hash`, `log_index`, and contract address

Current field decoding choice:

- indexed address fields are decoded from topics into plain address columns
- non-indexed numeric payload fields are stored as raw 32-byte hex strings

Why raw hex for numeric fields in v1:

- it avoids lossy or fragile SQL-time decoding on first rollout
- it keeps the initial DDL simple and production-safe
- it still makes downstream queries much easier than re-parsing `payload_json`

Future upgrades can add:

- decimal or signed numeric helper columns
- price and size enrichment
- stronger dedupe semantics
- token and pool registry enrichment

The first static registry foundation now lives in `docs/phase2-registry-design.md`.

## Event Mapping

### ERC-20 Transfer

Source:

- `topic0 = Transfer(address,address,uint256)`

Projected columns:

- `token_address`
- `sender`
- `recipient`
- `amount_raw`

### Uniswap V2 Swap

Source:

- `topic0 = Swap(address,uint256,uint256,uint256,uint256,address)`

Projected columns:

- `pair_address`
- `sender`
- `recipient`
- `amount0_in_raw`
- `amount1_in_raw`
- `amount0_out_raw`
- `amount1_out_raw`

### Uniswap V2 Mint

Source:

- `topic0 = Mint(address,uint256,uint256)`

Projected columns:

- `pair_address`
- `sender`
- `amount0_raw`
- `amount1_raw`

### Uniswap V2 Burn

Source:

- `topic0 = Burn(address,uint256,uint256,address)`

Projected columns:

- `pair_address`
- `sender`
- `recipient`
- `amount0_raw`
- `amount1_raw`

### Uniswap V3 Swap

Source:

- `topic0 = Swap(address,address,int256,int256,uint160,uint128,int24)`

Projected columns:

- `pool_address`
- `sender`
- `recipient`
- `amount0_raw`
- `amount1_raw`
- `sqrt_price_x96_raw`
- `liquidity_raw`
- `tick_raw`

### Uniswap V3 Mint

Source:

- `topic0 = Mint(address,address,int24,int24,uint128,uint256,uint256)`

Projected columns:

- `pool_address`
- `sender`
- `owner`
- `tick_lower_raw`
- `tick_upper_raw`
- `liquidity_raw`
- `amount0_raw`
- `amount1_raw`

### Uniswap V3 Burn

Source:

- `topic0 = Burn(address,int24,int24,uint128,uint256,uint256)`

Projected columns:

- `pool_address`
- `owner`
- `tick_lower_raw`
- `tick_upper_raw`
- `liquidity_raw`
- `amount0_raw`
- `amount1_raw`

## Operational Note

For brand-new ClickHouse volumes, Docker init will create these tables automatically.

For an already-initialized VPS volume, new init SQL files are not replayed automatically. Use:

```bash
bash scripts/apply-clickhouse-init.sh
```

If you also want historical `raw_evm_logs` rows projected into the new tables, run:

```bash
bash scripts/backfill-normalized-events.sh
```

For a queryable operational snapshot across all normalized surfaces, use:

```bash
bash scripts/inspect-phase2-status.sh
```

This reads `eatlq.phase2_raw_status` and `eatlq.phase2_surface_status`, which expose raw archive freshness plus per-surface raw-total, raw-projectable, raw-projectable-unique, raw-projectable-duplicate, raw-unprojectable, normalized, recent parity, duplicate surplus, registry gap, and latest timestamps.

For a queryable alert layer on top of those status views, use:

```bash
bash scripts/inspect-phase2-alerts.sh
```

This reads `eatlq.phase2_alert_summary` and `eatlq.phase2_alert_events`, which separate active `critical` issues from `warning` conditions such as historical unique projectable gaps or historical raw rows that fail the current projection filter, and attach suggested remediation commands to each alert row.

For rebuild-oriented warning remediation driven by the alert layer, use:

```bash
bash scripts/remediate-phase2-alerts.sh
```

This defaults to dry-run mode and only executes targeted rebuilds when `--apply` is explicitly provided. Alerts caused only by unprojectable historical raw rows are intentionally excluded from rebuild automation.

For the minimal cron / monitoring export on top of status and alerts, use:

```bash
bash scripts/phase2-monitor-cron.sh 30 300 1
```

This emits one machine-friendly summary line, then the strict healthcheck body, and returns the strict healthcheck exit code unchanged.

For root-cause analysis of the current ERC20 historical unprojectable raw backlog, use:

```bash
bash scripts/inspect-erc20-unprojectable-raw.sh 20
```

This groups those rows by filter-failure reason, source, token, and recent samples.

Current behavior of that script:

- it truncates the normalized target tables first
- it then repopulates them from `eatlq.raw_evm_logs`
- it groups on the normalized event key during rebuild so overlapping realtime and backfill raw rows collapse into one normalized row
- this makes reruns operationally simpler on an existing VPS

Then verify:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%swaps%'"

docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%transfers%'"

docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%mints%'"

docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%burns%'"

docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count() FROM eatlq.erc20_transfers"
```

To check that live raw ingestion and normalized projections stay aligned over a recent window:

```bash
bash scripts/verify-normalized-events.sh
```

Optional custom window:

```bash
bash scripts/verify-normalized-events.sh 30
```

To inspect duplicate pressure and deduped row counts:

```bash
bash scripts/verify-normalized-dedupe.sh
```

To inspect concrete duplicate samples:

```bash
bash scripts/inspect-normalized-duplicates.sh
```

Examples:

```bash
bash scripts/inspect-normalized-duplicates.sh v3 20
bash scripts/inspect-normalized-duplicates.sh v3-mint 20
bash scripts/inspect-normalized-duplicates.sh v3-burn 20
bash scripts/inspect-normalized-duplicates.sh erc20 20
```

To inspect whether duplicates already exist in `raw_evm_logs`, including which `source` values contributed to them:

```bash
bash scripts/inspect-raw-duplicate-origins.sh v3 20
```

To inspect recent top activity from the derived hourly views:

```bash
bash scripts/inspect-normalized-activity.sh
```

Examples:

```bash
bash scripts/inspect-normalized-activity.sh v3 24 20
bash scripts/inspect-normalized-activity.sh v3-mint 24 20
bash scripts/inspect-normalized-activity.sh v3-burn 24 20
bash scripts/inspect-normalized-activity.sh erc20 6 20
```

## Current Limits

- if `erc20_transfers` stays at `0` while swap tables have rows, first verify the active `LOG_FILTER_JSON` really includes the ERC-20 `Transfer` topic
- existing historical rows in `raw_evm_logs` are not backfilled into the new tables automatically by table creation alone
- dedupe currently exists as a safer query surface through views, not yet as a physically deduplicated storage engine
- raw numeric hex still needs downstream decoding for some analytics
- indexed signed tick fields are still stored as raw 32-byte hex and need helper decoding later
- the current verification model checks recent-window parity by counts, not full row-for-row reconciliation
- duplicate sample inspection now also has dedicated audit views for cross-surface summary, but physical dedupe is still not enforced at storage level
- live materialized views can still append overlapping rows in the future; the rebuild script is currently the safest way to physically re-collapse those overlaps
- targeted rebuilds are now available per normalized surface, so operators do not always need a full cross-surface rebuild to re-collapse one anomalous table
- the new activity layer is still address-level counting, not yet semantic trade sizing or wallet/entity intelligence

## Next Increment

After this first normalized layer is verified, the next practical improvements are:

1. verify the expanded mint / burn layer on the VPS after DDL apply and rebuild
2. decide whether dedupe should remain query-time or move into a physical deduplicated table
3. add token and pool registry enrichment on top of the normalized layer
4. add higher-level derived analytics tables on top
