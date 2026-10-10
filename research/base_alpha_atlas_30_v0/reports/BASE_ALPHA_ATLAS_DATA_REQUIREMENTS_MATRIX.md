# BASE_ALPHA_ATLAS_DATA_REQUIREMENTS_MATRIX

Filename/schema-level only. No large diagnostics content read. No production business queries.

| strategy_id | class | tables/files | columns | resolution | PIT | min_history | est_rows | est_bytes | diagnostics_maybe | prod_db_required | missing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| F01_OFS_SIGN_RUNLEN | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | uniswap_v3_swaps_decoded or offline swap TSV; pool metadata | pool_address; block_time; amount0; amount1; tick; tx_from | swap-event or 1m bars built from swaps | features use only swaps with block_time< | >=14d active pools | ~1e6-1e7 swap rows bounded export | 200MB-2GB bounded | maybe: base_b5_real_swaps_*.tsv / gl* sw | True | signed WETH orientation table; calibrated cost dis |
| F01_OFS_SIZE_SEQUENCE | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | uniswap_v3_swaps_decoded | pool_address; block_time; amount0; amount1; log_index | event-time swaps | strict event order by (block_time, log_i | >=14d | ~1e6-1e7 | 200MB-2GB | maybe via existing swap TSV names | True | reliable WETH side mapping |
| F01_OFS_INTERARRIVAL | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | uniswap_v3_swaps_decoded | pool_address; block_time; amount0; amount1 | 1m from swaps | PIT bar_close | >=14d | ~1e6+ | 200MB+ | maybe | True | stable 1m builder |
| F02_LP_POST_MINT_DRIFT | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | uniswap_v3_mints_decoded; uniswap_v3_swaps_decoded | pool_address; block_time; amount0; amount1; tick_lower; tick | event + 1m | mint before entry; no future mints | >=30d | mints ~1e5-1e6; swaps larger | 100MB-1GB | uncertain; mint tables named in docs | True | WETH-normalized mint size |
| F02_LP_POST_BURN_PRESSURE | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | uniswap_v3_burns_decoded; swaps | pool_address; block_time; amount0; amount1 | event+1m | PIT | >=30d | ~1e5-1e6 | 100MB-1GB | uncertain | True | short execution path not in live executor buy-focu |
| F02_LP_DEPTH_LEAD | `C_PRODUCTION_DB_HEAVY` | mints; burns; swaps; optional ticks | pool_address; block_time; amounts; liquidity if present | 1m | PIT | >=30d | multi-table join heavy | >2GB likely | unlikely complete offline | True | true depth/tick bitmap |
| F03_VOL_COMPRESS_EXPAND | `A_OFFLINE_DATA_LIKELY_AVAILABLE` | OHLCV diagnostics or swap-derived 1m | pool; bar_time; close; volume | 1m | PIT | >=14d | bars ~1e6 | 50-500MB | yes: sprint035e/f ohlcv* filenames | False | uniform OHLCV coverage across pools |
| F03_VOL_JUMP_CONTINUE | `A_OFFLINE_DATA_LIKELY_AVAILABLE` | OHLCV/swaps | close; time; pool | 1m | PIT | >=14d | ~1e6 | 50-500MB | yes ohlcv diagnostics names | False |  |
| F03_VOL_FAKE_BREAK_FADE | `A_OFFLINE_DATA_LIKELY_AVAILABLE` | OHLCV | open; high; low; close; time | 1m | confirm uses only <=t+m | >=14d | ~1e6 | 50-500MB | yes | False |  |
| F04_XS_LEADER_TO_SECOND | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | multi-pool OHLCV panel | pool; hour; close; volume | 1h | PIT ranks | >=30d | panel hours * pools | 100MB-1GB | partial if OHLCV multihorizon exists | True | aligned multi-pool clock |
| F04_XS_SAME_AGE_RS | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | swaps/OHLCV; first_swap times | pool; time; close; first_swap_ts | 1m | first_swap<=t | >=30d new pools | many short series | 200MB-2GB | partial | True | clean first_swap catalog without full CH group by |
| F04_XS_OLD_TO_NEW_MIGRATE | `C_PRODUCTION_DB_HEAVY` | all-pool swaps hourly aggregates | pool; hour; buy_weth; age | 1h | PIT | >=30d | wide universe | >2GB | no | True | full-universe hourly flows |
| F05_REG_BREADTH_EXPAND | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | hourly unique pool counts; volumes | hour; pool; swap_count; volume_eth | 1h | PIT | >=30d | hours*pools | 50-500MB if aggregated offline | partial from diagnostics aggregates mayb | True | preaggregated breadth table |
| F05_REG_NEWPOOL_DILUTION | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | new pool hourly counts | hour; new_pool_count | 1h | PIT | >=30d | small if aggregated | <100MB aggregated | maybe | True | pool creation feed completeness |
| F05_REG_GAS_CONGESTION | `D_DATA_UNAVAILABLE` | base fee L2 series; basket prices | block_time; base_fee; pool prices | 1h or block | PIT | >=30d | fee series small; basket larger | UNKNOWN | no dedicated fee parquet found by name | True | Base L2 base_fee historical archive in-project |
| F06_REV_SILENCE_FIRST_TRADE | `A_OFFLINE_DATA_LIKELY_AVAILABLE` | swap times series | pool; block_time; amounts | event | PIT | >=30d | swaps medium | 100MB-1GB | maybe from swap TSV | False |  |
| F06_REV_VOL_BEFORE_PRICE | `A_OFFLINE_DATA_LIKELY_AVAILABLE` | 1m ohlcv+counts | pool; time; close; trade_count | 1m | PIT | >=30d | ~1e6 | 50-500MB | yes if ohlcv+counts | False | trade_count column may be missing in some OHLCV fi |
| F06_REV_LIQ_REINJECT | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | mints; swaps | pool; times; amounts | event | confirm at mint+dt only with data<=that  | >=30d | medium | 100MB-1GB | uncertain | True | mint decode coverage |
| F07_WAL_SYNC_NEW_WALLETS | `C_PRODUCTION_DB_HEAVY` | swaps with tx_from; wallet first_seen registry | pool; tx_from; block_time; amounts | event | first_seen PIT | >=60d for wallet age | large | >2GB | controller maps exist by name but heavy | True | router/bundler exclusion list completeness; first_ |
| F07_WAL_EARLY_HOLDER_RELEASE | `C_PRODUCTION_DB_HEAVY` | swaps+tx_from | tx_from; pool; time; amounts | event | PIT cohort | >=30d | large | >2GB | partial controller inventories by name | True | accurate buy/sell attribution |
| F07_WAL_CONC_DECLINE | `C_PRODUCTION_DB_HEAVY` | erc20_transfers_decoded; balance snapshots | token; from; to; value; time | 5m-1h | PIT balances | >=60d | very large transfers | >>2GB | transfer net audits exist by name but hu | True | point-in-time balance engine |
| F08_EVT_POOL_AGE_RULE | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | swaps; first_swap | pool; time; amounts | 1m/event | PIT | >=30d | medium | 200MB-2GB | partial | True | non-overlapping evaluation protocol vs live |
| F08_EVT_MINT_BURST | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | mints_decoded | pool; owner; time; amounts | event | PIT | >=30d | mints medium | 50-500MB | uncertain | True | owner field presence |
| F08_EVT_FEE_TIER_SHIFT | `D_DATA_UNAVAILABLE` | pair↔fee tier registry; per-tier volumes | token0; token1; fee; hour; volume | 1h | PIT | >=60d | UNKNOWN | UNKNOWN | no clean registry file found by name in  | True | canonical pair registry |
| F09_EXE_IMPACT_ASYMM | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | swaps | pool; time; amounts; tick | event→5m | PIT | >=30d | medium-large | 200MB-2GB | maybe | True | clean mid price |
| F09_EXE_NOTIONAL_CURVE | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | swaps | size; impact proxies | event | PIT fit | >=30d | medium | 200MB+ | maybe | True | stable impact measurement |
| F09_EXE_QUOTE_STALE | `D_DATA_UNAVAILABLE` | high-res mid quotes or tick stream + swaps | mid; last_trade; times | sub-second to 1s ideal | PIT | UNKNOWN | UNKNOWN | UNKNOWN | no quote tape in offline inventory names | True | quote tape |
| F10_PLP_FEE_VS_IL | `C_PRODUCTION_DB_HEAVY` | swaps; mints; burns; fee tier meta | price; volume; liquidity; fees | 1m | PIT | >=60d | large | >2GB for accurate fees | no complete fee capture dataset by name | True | actual fee growth series per position |
| F10_PLP_HIGHVOL_LOWTREND | `B_SMALL_BOUNDED_EXPORT_REQUIRED` | OHLCV+volume; fee tier | pool; hour; close; volume; fee | 1h | PIT | >=30d | medium | 50-500MB | partial ohlcv | True | fee income realization |
| F10_PLP_EVENT_WINDOW | `C_PRODUCTION_DB_HEAVY` | events+swaps+LP sim | event markers; prices; volumes; fees | 1m | PIT | >=30d | large | >1GB | no | True | LP simulator fidelity |

## Class counts

- `A_OFFLINE_DATA_LIKELY_AVAILABLE`: 5
- `B_SMALL_BOUNDED_EXPORT_REQUIRED`: 15
- `C_PRODUCTION_DB_HEAVY`: 7
- `D_DATA_UNAVAILABLE`: 3

## Rule

Class `C_PRODUCTION_DB_HEAVY` MUST NOT enter first compute wave.
Class `D_DATA_UNAVAILABLE` parked until data appears offline.

