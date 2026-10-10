# F05_REG_NEWPOOL_DILUTION — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F05` Base-wide regime
- name: New-pool dilution avoidance
- economic_mechanism: When new pools created/hour exceed threshold, expected edge per pool dilutes; strategy flats risk-on basket (signal=FLAT).
- counterparty: attenion_fragmented_traders

## Universe / Timing
- universe: Base-wide
- observation_time: hour
- feature_available_at: new pool counts from first_swap or pool_created events <=hour

## Feature formulas
- `new_pools_h` = count first_swap in hour
- `dilution` = new_pools_h / median_7d

## Parameter search space (not selected)
```json
{
  "dilution_max": [
    1.5,
    2.0,
    3.0
  ],
  "lookback_d": [
    7,
    14
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ],
  "z_volume": [
    0,
    1
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: Trade long top-k ONLY if dilution<=dilution_max; else no entry (edge from avoidance).
- execution_delay: +1h
- position_size: {100,200} when allowed
- exit: hold_h
- stop: 0.2
- take_profit: 0.15
- maximum_hold: hold_h
- repeat_entry: hourly
- cooldown: 0

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: shuffle new_pools_h series
- difference_from_prior_failed_strategies: Avoidance regime ≠ always-in KEYSTONE.

## Live conflict notes
Could later risk-reduce live; research only now.
