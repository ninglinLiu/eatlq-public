# LARGE_BUY_ABSORPTION_v2 — DIAGNOSTIC_REPLAY Review

Generated: `2026-07-21T07:29:29Z`

> **Not a global kill.** Per-hypothesis PROVEN / REFUTED / UNCONFIRMED only.
> Primary timing preregistered: **5s absorb / 0s latency** (0s = optimistic upper bound).
> Sensitivity curves are diagnostic — not used to pick parameters.

## Per-hypothesis verdicts

| Hypothesis | Verdict |
| --- | --- |
| `DATA_COMPLETE_FOR_INTENDED_UNIVERSE` | **PROVEN** |
| `PIPELINE_ARTIFACT_DROP` | **REFUTED** |
| `ABSORB_5S_LATENCY_0S_PRIMARY` | **SCHEDULED_EDGE_UNCONFIRMED** |
| `REGIME_POSITIVE_USD` | **PROVEN** |
| `SELECTION_EDGE_LBA_VS_POOL` | **UNCONFIRMED** |
| `SELECTION_EDGE_LBA_VS_RANDOM` | **REFUTED** |
| `STRUCTURED_RANKING_BEATS_RANDOM` | **UNCONFIRMED** |
| `STRUCTURED_RANKING_BEATS_SIZE` | **REFUTED** |
| `SENSITIVITY_USED_FOR_SELECTION` | **REFUTED** |

- Freeze gate: `FREEZE_READY`
- Shadow upgrade: `False`
- Live: `NO_GO`

## Scorecard (scheduled primary)

| Arm | Verdict | Selected | Fills | Skip | Sched WETH | Cum USD | Paired p05 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| POOL_ONLY | BASELINE | 74406 | 153 | 0.9979437142165954 | 0.1349275192441711 | 404.782557732519 | 0.0 |
| LARGE_BUY_ONLY | SCHEDULED_EDGE_UNCONFIRMED | 76922 | 385 | 0.9949949299290192 | 0.1457765982267878 | 437.3297946803752 | -0.1994361311851846 |
| ABSORPTION_ONLY | SCHEDULED_EDGE_NEGATIVE | 277601 | 386 | 0.9986095150954069 | 0.1237241547726641 | 371.1724643180041 | -0.2549137220930293 |
| LARGE_BUY_ABSORPTION | SCHEDULED_EDGE_UNCONFIRMED | 58947 | 384 | 0.993485673571174 | 0.2466888753181055 | 740.0666259543277 | -0.1282233743407428 |
| CAPACITY_MATCHED_RANDOM | CONTROL | 74406 | 363 | 0.99512136118055 | 0.284009739183885 | 852.0292175516673 | nan |

## Data coverage (Phase 0)

- Phase1 trigger: `SKIP_FULL_REBUILD`
- Intended-universe complete: `PROVEN`
- Pipeline artifact drop: `REFUTED`

Sparse calendar vs full Base V3 is **by design** (new-pool first-48h), not a silent data drop.

## Open items (not a global kill)

- `ABSORB_5S_LATENCY_0S_PRIMARY`
- `SELECTION_EDGE_LBA_VS_POOL`
- `SELECTION_EDGE_LBA_VS_RANDOM`
- `STRUCTURED_RANKING_BEATS_RANDOM`
- `STRUCTURED_RANKING_BEATS_SIZE`

## Governance

- Code hash: `41fd647e7f5f68dda6bf1f831544112eb1e24a632a2f0c0d385ea31361e61c16`
- Config hash: `bc21de6467be4ad65235e87e0e04b5f878431d028db793ad15cb0984919e93c7`
- Data hash: `075b801d092368034462d4027b765e87e59c31002f31e3d07caa11836e1ef6f9`
- Prospective T0: `2026-07-21T07:29:29Z`

