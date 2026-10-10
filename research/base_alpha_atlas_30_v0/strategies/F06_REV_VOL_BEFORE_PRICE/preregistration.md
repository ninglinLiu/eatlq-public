# F06_REV_VOL_BEFORE_PRICE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F06` Silence & revival
- name: Volume revival before price
- economic_mechanism: Swap count recovers to >c*baseline while |ret| still <r_max → long anticipating price catch-up.
- counterparty: latent_demand

## Universe / Timing
- universe: previously active pools silent then reviving
- observation_time: 1m
- feature_available_at: vol and ret causal

## Feature formulas
- `vol_base` = mean swap_count over baseline window B prior to silence
- `vol_now` = swap_count over last s bars
- `ret_abs` = abs(px_t/px_{t-s}-1)

## Parameter search space (not selected)
```json
{
  "B": [
    60,
    120
  ],
  "s": [
    5,
    10
  ],
  "c": [
    0.5,
    1.0
  ],
  "r_max": [
    0.02,
    0.05
  ],
  "hold": [
    10,
    20
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: long if vol_now>=c*vol_base and ret_abs<=r_max and prior silence>=G (G in {1800,3600})
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold
- stop: 0.12
- take_profit: 0.12
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: 30 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: shuffle volume path vs price
- difference_from_prior_failed_strategies: Requires volume-price divergence, not raw revival chase.

## Live conflict notes
Orthogonal mechanism to KEYSTONE.
