# BASE_ALPHA_ATLAS — Stage 1A Result

UTC: 2026-07-18  
Title: F01/F03 Causal Signal Plumbing and Backtest Preflight

## Access flags (end state)

```text
ready_for_f01_signal_logic = true
ready_for_f03_signal_logic = true
ready_for_local_signal_sanity = true
ready_for_tiny_backtest_preflight = true

ready_for_tiny_backtest = false
ready_for_parameter_search = false
ready_for_full_panel = false
ready_for_production_database_access = false
```

Also retained: `ready_for_strategy_implementation` / prior design flags; `backtest_enabled=false`.

## Task completion

| Task | Result |
|---|---|
| 1 Tiny Panel exception note | `reports/BASE_ALPHA_ATLAS_TINY_PANEL_EXCEPTION_NOTE.md` (panel not deleted/rebuilt) |
| 2 F01 freeze | `strategies/F01_OFS_SIGN_RUNLEN/{preregistration,mechanism}.md` |
| 3 F01 causal runner + fixtures | `runner.py` + `tests/test_f01_signal.py` — **OK** |
| 4 F03 PIT audit | `preregistration.md` + `reports/F03_PIT_AND_FORMULA_AUDIT.md`; runner NaN-safe; fixture label **FIXTURE_VALIDATED_SIGNAL_LOGIC** |
| 5 Local signal sanity | parquet + `reports/BASE_ALPHA_ATLAS_F01_F03_SIGNAL_SANITY.md` (F01 n=217 / F03 n=2) |
| 6 Orthogonality (structure) | `reports/BASE_ALPHA_ATLAS_F01_F03_ORTHOGONALITY.md` — Jaccard 0 |
| 7 Tiny BT preflight | `reports/BASE_ALPHA_ATLAS_TINY_BACKTEST_PREFLIGHT.md` — **not executed** |

## Unit tests

`python3 -m unittest` F01 + F03 + phase05 catalog — **OK** (19+ tests; F03 includes missing-close regression).

## Production safety (post Stage1A)

| Check | Result |
|---|---|
| executor / signal-bridge / ingestor | running |
| RestartCount | 0 / 0 / 0 |
| Redis `signals` / `execution_group` pending | 0 |
| Swap used | ≈ 2173988864 B (stable vs stage start) |
| Prod DB / RPC from this task | none |

## Explicit stop

Stage1A complete. **No tiny/full backtest started. No parameter search. No PnL computed.**
