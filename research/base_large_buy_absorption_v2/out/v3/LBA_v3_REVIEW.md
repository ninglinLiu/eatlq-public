# LARGE_BUY_ABSORPTION_v3 — Research Layer Review

Generated: `2026-07-21T08:20:56Z`

> Per-hypothesis only. Does **not** mutate frozen v2 shadow. `live = NO_GO`.
> Capacity grid: robustness across cells (BH-corrected), **not** best-cell selection.
> Primary power test: signal-level covariate-matched ATT (Track B).

## Per-hypothesis verdicts

| Hypothesis | Verdict |
| --- | --- |
| `SIGNAL_LEVEL_INCREMENTAL_RETURN` | **REFUTED** |
| `WHALE_NOT_JUST_SIZE_DEPTH_PROXY` | **REFUTED** |
| `SELECTION_EDGE_ROBUST_ACROSS_CAPACITY` | **PROVEN** |
| `RANKER_BEATS_SIZE` | **UNCONFIRMED** |
| `RANKER_BEATS_RANDOM` | **UNCONFIRMED** |
| `V2_SHADOW_MUTATED` | **REFUTED** |
| `BEST_CELL_SELECTED` | **REFUTED** |

- Propose v3 shadow upgrade: `False`
- Live: `NO_GO`

## Track A — Capacity grid

- Fills at 3-slot: `384` → max `1875` (x`4.8828125`)
- BH-significant LBA-vs-POOL cells: `4` / `8`
- Verdict: `PROVEN`

See `out/v3/capacity_grid.csv`.

## Track B — Matched incremental (primary)

- ATT mean Δ WETH: `-0.00012172877283833392`
- Pairs: `50000`
- Day-cluster p05: `-0.0006004696657249251`
- Signal incremental: `REFUTED`
- Not just size/depth proxy: `REFUTED`

## Track C — HistGBM ranker

- RANKER_BEATS_SIZE: `UNCONFIRMED`
- Sealed: `{"n_sealed": 52791, "available": true, "gbm_sched_weth": 0.024312978968796528, "gbm_cum_usd": 72.93893690639025, "size_sched_weth": 0.04381455310555579, "size_cum_usd": 131.4436593166688, "paired_vs_size": {"n_days": 18, "sum_delta": -0.019501574136759257, "mean_daily_delta": -0.0010834207853755143, "bootstrap_token": {"mean": -0.019321286737126818, "p05": -0.07686388413172952, "frac_positive": 0.`

## Open items

- `SIGNAL_LEVEL_INCREMENTAL_RETURN`
- `WHALE_NOT_JUST_SIZE_DEPTH_PROXY`
- `RANKER_BEATS_SIZE`
- `RANKER_BEATS_RANDOM`

## Governance

- Code hash: `ba6357593489b3fd70e0448d7cad83ed78d43520bf878eba4f53fce17bd588e7`
- Config hash: `1505a37e2e31ad9627b486f3934ad5fcc378068939c26d220c4b97c0d02a8481`
- v2 freeze / shadow: untouched
