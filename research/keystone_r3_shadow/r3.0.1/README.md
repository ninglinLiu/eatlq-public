# r3.0.1-shadow

## Status
`CLOCK_STARTED` — `HUMAN_ARM.json` written 2026-08-21. Observer running. Confirmatory n starts at 0; no July–August backfill.

## Hash resolution
See `HASH_RESOLUTION.json`. Operational artifact is **9cf8…** (`r21_f10_fullfit_operational.pkl`).
`e1c68…` is the **r2.0.0 wrapper file** hash; scoring behavior matches (golden fixture max_abs_diff=0.0).

## Historical primary number
Use `DEV_PARTIAL_WALK_FORWARD_OOF.json` (not DEV_IN_SAMPLE 308/154).

| Gate | n_acc | EV accepted |
|------|------:|------------:|
| V0 KEYSTONE | 147 | +124.2% |
| V1 early_ret top-k | 61 | +74.3% |
| C0 price-only HGB top-k | 61 | +146.7% |
| F10 OOF | 61 | +146.7% |

**Important:** on this partial OOF, frozen C0 ≈ F10 in EV. Historical “beats strong price control” is therefore **not** established on OOF; prospective R3 must keep C0 as a required secondary negative control (no decision impact).

## Start confirmatory clock
1. Review this directory.
2. Copy `../HUMAN_ARM.template.json` → `../HUMAN_ARM.json` and set acknowledgements + `clock_started=true`.
3. Only then does `shadow_recorder.append_preoutcome` accept writes into confirmatory ledger.

## Pilot rows
Pre-r3.0.1 ledger lines = `R3_PILOT_PRE_FREEZE` (n=0), excluded.
