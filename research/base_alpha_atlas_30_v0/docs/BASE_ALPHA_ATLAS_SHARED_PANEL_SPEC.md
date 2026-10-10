# BASE_ALPHA_ATLAS_SHARED_PANEL_SPEC

Status: `DESIGN_ONLY` — build forbidden until `ready_for_shared_panel_build=true`.

## Goal

One immutable panel consumed read-only by all strategies:

```text
shared_panel/base_alpha_atlas_shared_panel.parquet
```

## Minimal necessary fields

### Keys / PIT
- `pool_id`
- `event_time` (unix s)
- `block_number` (nullable)
- `log_index` (nullable)
- `bar_close_time`
- `known_at`
- `feature_available_at`  # must be >= all input event times used

### Market
- `open, high, low, close` at 1m (and optional 5m/15m columns prefixed)
- `volume_eth`, `buy_volume_eth`, `sell_volume_eth`
- `trade_count`, `buy_count`, `sell_count`
- `vwap_eth`

### Microstructure (nullable if sparse)
- `mean_interarrival_s`, `median_interarrival_s`
- `sign_run_len`, `large_trade_flag`
- `size_p90_eth`

### LP / events (nullable)
- `mint_eth_proxy`, `burn_eth_proxy`
- `event_mint_burst_k`
- `pool_age_minutes`
- `first_swap_time`

### Cross-section / regime (nullable, join-as-of)
- `xs_ret_1h_rank`
- `breadth_active_pools_1h`
- `new_pools_1h`
- `base_fee_z` (if/when available)

### Execution proxies (nullable)
- `impact_buy_proxy`, `impact_sell_proxy`
- `depth_proxy`

### Cost placeholders (not filled with 0)
- columns may be absent; evaluation must keep UNKNOWN

## Sources (design mapping)

| Field group | Likely offline diagnostics | Needs bounded export | Needs heavy prod DB |
|---|---|---|---|
| OHLCV 1m | sprint035e/f ohlcv* names | maybe rebuild from swaps | if coverage holes |
| swaps microstructure | base_b5_real_swaps*.tsv name | yes bounded | full history CH |
| mints/burns | docs mention decoded tables | yes | yes if not copied |
| wallet/HHI | controller/transfer audits (HUGE) | no for Wave1 | yes |
| base_fee | not found by name | external archive | maybe |

## Partitioning
- Hive-style: `panel_date=YYYY-MM-DD/pool_shard=00..0F`
- Sorted by `(pool_id, bar_close_time)`
- Row group target ~64-128MB

## Expected size (order-of-magnitude, not measured)
- 30d × 500 pools × 1m bars ≈ 2.16e7 rows
- ~50-120 bytes/row compressed → **~1-3 GB** parquet (estimate only)
- Wave1 may start with **subset panel** 7-14d × top-N pools to stay <<1GB

## One-build / thirty-read pattern
1. Builder job (future, gated) writes panel + `manifest.json` (sha256, row counts, column dict, PIT checks)
2. Strategies receive `panel_path` + column projection list
3. Runners must `read_parquet(columns=needed)` only
4. Forbidden: per-strategy raw CH scan or re-deriving OHLCV from raw logs

## PIT rules
- `feature_available_at >= max(known_at of inputs)`
- as-of joins for XS/regime columns
- no `FINAL` full-table semantics in builder queries when gate opens; prefer bounded incremental

## Explicit non-goals now
- no build
- no 325MB diagnostics ingestion
- no production `GROUP BY pool_address` full scans
