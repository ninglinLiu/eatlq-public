# F09_EXE_IMPACT_ASYMM — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F09` Quote, routing & execution anomalies
- name: Buy/sell impact asymmetry
- economic_mechanism: Estimate impact_buy and impact_sell on matched notionals; trade direction with lower adverse impact persistence.
- counterparty: asymmetric_LP_curves

## Universe / Timing
- universe: pools with dense swaps
- observation_time: 5m estimation windows
- feature_available_at: impacts from past windows only

## Feature formulas
- `impact(side,n)` = median |ret| over dt after swaps of side with size~n
- `asym` = impact_buy - impact_sell

## Parameter search space (not selected)
```json
{
  "n_eth": [
    0.3,
    1.0
  ],
  "dt_sec": [
    30,
    60
  ],
  "asym_min": [
    0.002,
    0.005
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.1,
    0.15
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: long if asym<=-asym_min (buys cheaper impact than sells) else flat/bearish mark
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold
- stop: stop_ret
- take_profit: 0.1
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: 15 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: sign flip of sides
- difference_from_prior_failed_strategies: Microstructure impact geometry ≠ signal ML.

## Live conflict notes
Distinct; medium compute.
