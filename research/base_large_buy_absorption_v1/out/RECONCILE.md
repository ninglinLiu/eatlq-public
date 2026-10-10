# Denominator reconciliation (v0 → v1)

## Why 67.4% and 61.99% both appeared

| Metric | Formula | Value |
| --- | --- | --- |
| Arm-vs-arm | WHALE selected / POOL selected | 19684/29194 = **67.4%** |
| Within-pool overlay | whale-flagged inside POOL selected | 18096/29194 = **61.99%** |

Different questions, both true.

## Why skip ~97.5% vs ~99%

- **97.5%**: WHALE arm under $1000 / max_concurrent=3
- **~99%**: POOL_ONLY arm under the same scheduler (more candidates → more skips)

## Why 43 unique days across 6 months

v0 whale-selected 43 unique days arose from: (1) underlying resolved data covering only 131 of 274 calendar days (true discontinuity), (2) walk-forward TEST windows only, (3) v0 200k random downsample of scoped buys (removed in v1), (4) absorbed+quantile selection. Not a date-field bug alone.

## v1 current figures

- POOL_ONLY selected: 75797
- LARGE_BUY_ABSORPTION selected: 48148
- arm share LBA/POOL: 0.6352230299352217
- LBA skip_rate: 0.9915468970673756
- calendar scoped coverage: 0.38321167883211676

