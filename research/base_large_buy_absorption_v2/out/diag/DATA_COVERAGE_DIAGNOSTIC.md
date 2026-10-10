# DATA_COVERAGE_DIAGNOSTIC — Phase 0 HARD GATE

Generated: `2026-07-21T07:05:20Z`

## Headline

- Intended-universe completeness: **PROVEN**
- Pipeline silent-drop (H2): **REFUTED**
- Phase 1 full rebuild: **SKIP_FULL_REBUILD**

## Why sparse calendar is NOT a bug

`load_swap_universe` only keeps **new WETH v3 pools** (first swap inside freeze window) and only their **first 48 hours**. Full Base V3 has ~every calendar day; new-pool births cluster (e.g. Oct ~4 birth days). Resolved days ≈ birth/activity days of that design.

## Totals

- Resolved swaps: `1,818,168`
- CH new-pool 48h swaps: `1,819,988`
- CH full warehouse swaps: `7,580,450`
- Resolved unique days: `136` / span `274`
- Scoped buys (age/size filter): `524,257` on `105` days (mean `4993` / day, max `39797`)

## Hypotheses

### H1_intentional_new_pool_48h_filter

- Supported: `True`
- resolved=1,818,168 vs CH new-pool-48h=1,819,988 (ratio=0.9990); vs full warehouse=7,580,450 (ratio=0.2398). Sparse calendar matches new-pool birth-day clustering, not missing warehouse days.

### H2_silent_chunk_drop_artifact

- Supported: `False`
- REFUTED: monthly resolved swap counts match CH new-pool-48h within ~0.5% across all months. No material silent drop.

### H3_freeze_window_too_narrow

- Supported: `False`
- Freeze ends 2026-07-01 00:00:00; warehouse max=2026-07-21. A few trailing days cannot explain 169/274 'missing' calendar days.

## Monthly reconciliation (resolved vs CH 48h vs all)

| ym | resolved | ch_48h | cov | pools_r | pools_ch | birth_days | ch_all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025-10 | 116063 | 116095 | 0.9997 | 219 | 220 | 4 | 212321 |
| 2025-11 | 160791 | 160954 | 0.9990 | 290 | 290 | 6 | 226177 |
| 2025-12 | 276593 | 276630 | 0.9999 | 218 | 218 | 7 | 279617 |
| 2026-01 | 271429 | 272388 | 0.9965 | 376 | 376 | 9 | 397473 |
| 2026-02 | 377248 | 377415 | 0.9996 | 388 | 388 | 7 | 1161017 |
| 2026-03 | 236703 | 237014 | 0.9987 | 337 | 337 | 7 | 2573362 |
| 2026-04 | 106489 | 106493 | 1.0000 | 265 | 265 | 23 | 711832 |
| 2026-05 | 199104 | 199249 | 0.9993 | 575 | 578 | 31 | 1118767 |
| 2026-06 | 73727 | 73729 | 1.0000 | 1678 | 1678 | 22 | 899884 |
| 2026-07 | 21 | 21 | 1.0000 | 9 | 9 | 0 | 0 |

## Rebuild recommendation

- Keep resolved_swaps as resolved_swaps_full (already matches CH ground truth).
- Harden load_swap_universe with daily-chunk asserts for future rebuilds.
- Proceed to v2 timing/ranking research on this complete intended universe.
- Do NOT claim 260+ calendar days of new-pool signals — births cluster; evaluate on covered days only.
