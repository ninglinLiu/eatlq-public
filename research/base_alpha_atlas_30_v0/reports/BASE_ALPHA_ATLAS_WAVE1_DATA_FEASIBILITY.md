# BASE_ALPHA_ATLAS_WAVE1_DATA_FEASIBILITY

UTC: 2026-07-18T14:13Z  
Task: ATLAS-W1-A1  
Constraints honored: no CH business queries, no file copy ≥50MB, no backtest, no Redis writes.

## Probe corpus (project-local, 16MB total)

`normalized/wave1_probe/`

| file | size | role |
|---|---|---|
| `base_b5_real_swaps_head50k.tsv` | 9.0M | head extract of 121MB TSV (not full copy) |
| `base_b8_future_swaps_20260714.tsv` | 1.3M | full small swap TSV |
| `run004_v3r6_candidate_pool_head20k.csv` | 5.6M | rich decoded swap sample + WETH side flags |
| `sprint035e/f ohlcv*` | ~29K | smoke OHLCV only (tiny) |
| `schema_sniff_013.txt` | tiny | pointer into decoded view SQL |

## Per-strategy feasibility

| strategy_id | prior class | feasibility | evidence | unblock if PARTIAL/BLOCKED |
|---|---|---|---|---|
| F03_VOL_JUMP_CONTINUE | A | **PARTIAL** | OHLCV smoke exists but n≈100 rows; not enough history. Can build 1m close from swap ticks (`tick` col in b5/v3r6). | Tiny panel: derive 1m bars from `head50k`/`v3r6` offline (no CH). |
| F06_REV_VOL_BEFORE_PRICE | A | **PARTIAL** | Needs trade_count + close; smoke OHLCV lacks trade_count. Swaps can synthesize counts. | Same derived 1m panel with `trade_count`. |
| F05_REG_NEWPOOL_DILUTION | B | **PARTIAL** | Need hourly new_pool_count. Can approximate from first-seen pool in swap extracts; coverage incomplete. | Bounded export: `pool, min(block_time)` for 14–30d, LIMIT pools, OR build from discovered_pools offline dump if appears &lt;50MB. |
| F01_OFS_SIGN_RUNLEN | B | **READY** | `v3r6` has `amount0/1`, `is_buy_weth_to_token`, `timestamp_ms`, `pool_address`, `log_index`. `b5` has pool/block/ts/amount0/1/tick. | Optional: larger bounded swap slice &lt;50MB. |
| F10_PLP_HIGHVOL_LOWTREND | B | **BLOCKED** | No fee-income / LP position realization series in probe. OHLCV volume/trend only → insufficient for mandatory fee−IL−gas net. | Needs fee growth or LP simulator inputs; do not fake APR. |
| F02_LP_POST_MINT_DRIFT | B | **BLOCKED** | No `uniswap_v3_mints_decoded` offline extract found under probe rules. Schema exists in `013_decoded_views.sql`. | Bounded mint export: pool, block_time, amount0/1, tick_lower/upper, owner; WHERE block_time range; max_threads=1 — **not run this task**. |

## Class revisions

- F03, F06: keep computational path offline-capable via **derived bars**; demote pure “A OHLCV file ready” narrative → `PARTIAL` until panel built.
- F10, F02: remain out of first compute until mint/fee data appears.
- F01: upgrade practical readiness to READY on local extracts.

## Acceptance vs spec

- ≥2 Wave1 strategies READY or PARTIAL with concrete next step: **PASS** (F01 READY; F03/F06/F05 PARTIAL).
- No strategy called alpha.

## Live safety

Executor/bridge/ingestor remained `running` rc=0 at probe start. No production writes.
