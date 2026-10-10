# Shared Panel Build Plan (DRAFT ONLY — NOT AUTHORIZED TO RUN)

Status: `BLOCKED_BY_SAFE_TO_BEGIN_GATE`

## Target artifact

```text
shared_panel/base_alpha_atlas_shared_panel.parquet
```

Build once; all 30 strategies consume it. No per-strategy full rescans.

## Data priority when gate opens

1. Copy existing offline diagnostics / CSV into `normalized/` (read-only source → project-local sink)  
2. Prefer already-decoded swap / pool snapshots under `mvp/diagnostics`  
3. Production ClickHouse only via separate Gate: readonly, max_threads=1, timeouts, bounded block range, EXPLAIN first — **never** full-table `GROUP BY pool_address` like live bridge  
4. Independent public RPC last, concurrency 1, cached

## Required columns (aspirational; fill by availability)

event_time, block_number, log_index, bar_close_time, known_at, feature_available_at,  
1m/5m/15m OHLCV, buy/sell volume, trade counts, size distribution, trade-sign runs,  
inter-arrival, pool age, LP events, wallet cohort summaries, cross-sectional ranks,  
Base-wide regime proxies, gas, slippage / impact proxies, sell_success.

## Resource envelope

```text
nice 19, ionice idle, 1 thread, ≤384 MiB, timeout ≤20 m, max_heavy_workers=2
```

## Do not run this plan until

`docs/gates/SAFE_TO_BEGIN_OFFLINE_RESEARCH.md` shows `true`.
