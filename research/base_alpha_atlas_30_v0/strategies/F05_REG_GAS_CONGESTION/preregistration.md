# F05_REG_GAS_CONGESTION — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F05` Base-wide regime
- name: Gas congestion entry filter
- economic_mechanism: High base-fee regimes worsen net EV; long micro-alpha basket only when base_fee_z below threshold.
- counterparty: congested_priority_fees_eat_edge

## Universe / Timing
- universe: needs base fee series + basket
- observation_time: hour/ L2 block
- feature_available_at: fee from blocks <=t

## Feature formulas
- `base_fee_z` = zscore(base_fee_per_gas)

## Parameter search space (not selected)
```json
{
  "z_max": [
    -0.5,
    0.0,
    0.5
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ],
  "stop_ret": [
    0.2,
    0.3
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: enter basket if base_fee_z<=z_max
- execution_delay: +1h
- position_size: {100,200}
- exit: hold_h
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold_h
- repeat_entry: hourly
- cooldown: 0

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: permute fees vs returns
- difference_from_prior_failed_strategies: Cost-regime timing; not flow toxicity.

## Live conflict notes
Low conflict; data missing.
