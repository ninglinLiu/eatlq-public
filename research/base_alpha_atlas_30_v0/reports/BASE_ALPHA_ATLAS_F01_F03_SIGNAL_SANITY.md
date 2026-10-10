# BASE_ALPHA_ATLAS — F01/F03 Local Signal Sanity

UTC: 2026-07-18  
Scope: signal generation only — **no returns, PnL, MFE/MAE, win_rate, profit_factor, or best_parameter**.

## Inputs (allowed only)

| Strategy | Source |
|---|---|
| F01 | `normalized/wave1_probe/run004_v3r6_candidate_pool_head20k.csv` (swap-level) |
| F03 | `shared_panel/tiny_wave1/base_alpha_atlas_tiny_wave1_panel.csv` (closed 1m bars) |

No ClickHouse, Redis business content, RPC/WS, or `/opt/EATLQ` production files.

## Resource envelope

- single process / single thread
- `nice=19`, `ionice=idle`
- timeout ≤ 120s
- observed peak RSS: F01 ≈ 20 MiB; F03 ≈ 115 MiB (≤ 256 MiB)

## Outputs

| Artifact | Format |
|---|---|
| `normalized/stage1a/f01_signals.parquet` | parquet |
| `normalized/stage1a/f03_signals.parquet` | parquet |
| `normalized/stage1a/sanity_summary.json` | json |

## F01 metrics

| Metric | Value |
|---|---|
| signal_count | 217 |
| pools_covered | 11 |
| signals_per_pool (mean) | 19.73 |
| signals_per_hour (mean) | 6.38 |
| feature_coverage.n_swaps | 7000 |
| UNKNOWN_rate | 0.0 |
| duplicate_signal_rate | 0.0 |
| transaction_index | UNKNOWN (sort = block_number, log_index) |
| observation_timestamp / feature_available_at | equal to trigger swap time (ms) |
| trigger_coordinates | block_number + log_index (+ null transaction_index) |
| runtime_s | 0.381 |
| peak_memory_mb | 19.9 |

Status: causal swap-run logic exercised on local probe — **not** parameter-selected alpha.

## F03 metrics

| Metric | Value |
|---|---|
| signal_count | 2 |
| pools_covered | 2 |
| signals_per_pool (mean) | 1.0 |
| signals_per_hour (mean) | 1.0 |
| feature_coverage.n_bars | 8419 (30 pools) |
| UNKNOWN_rate | 0.0 |
| duplicate_signal_rate | 0.0 |
| status_label | `FIXTURE_VALIDATED_SIGNAL_LOGIC` |
| l_used | 30 (tiny depth; search space still {60,120}) |
| observation_timestamp / feature_available_at | bar_close_time |
| trigger_coordinates | bar_index + bar_close_time + jump_ratio |
| runtime_s | 0.131 |
| peak_memory_mb | 114.8 |

## Explicitly absent fields

`return`, `PnL`, `MFE`, `MAE`, `win_rate`, `profit_factor`, `best_parameter` — not computed.

## Production posture (sanity window)

Checked after run (read-only):

- `eatlq-executor-1` / `eatlq-signal-bridge-1` / `eatlq-ingestor-1`: **running**, RestartCount **0**
- Redis stream `signals` consumer group `execution_group` **pending=0**, lag=0
- Swap used bytes ≈ 2173988864 (no growth attributed to this task)
- This task did not open production DB or RPC connections
