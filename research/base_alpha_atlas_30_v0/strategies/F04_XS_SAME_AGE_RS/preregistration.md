# F04_XS_SAME_AGE_RS — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F04` Cross-sectional capital rotation
- name: Same-age cohort relative strength
- economic_mechanism: Among pools with age in [a,b] minutes, long top RS, avoid bottom (long-only: long top).
- counterparty: weak_same_age_pools

## Universe / Timing
- universe: new pools age 5-120m
- observation_time: minute bars since pool first swap
- feature_available_at: age from first_swap_time known

## Feature formulas
- `age_m` = (t-first_swap)/60
- `rs` = ret_{w} zscore within age cohort bucket

## Parameter search space (not selected)
```json
{
  "age_lo": [
    5,
    10
  ],
  "age_hi": [
    30,
    60,
    120
  ],
  "w": [
    5,
    10
  ],
  "rs_min": [
    0.5,
    1.0
  ],
  "hold": [
    5,
    15
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long if age in [age_lo,age_hi] and rs>=rs_min
- execution_delay: 1 bar
- position_size: notional {50,100}
- exit: hold/stop/tp
- stop: 0.15/0.25
- take_profit: 0.1/0.2
- maximum_hold: hold
- repeat_entry: one per pool lifetime window
- cooldown: until age_hi

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: cohort label shuffle
- difference_from_prior_failed_strategies: Age-bucket RS; KEYSTONE uses ML p_fatal not RS zscore.

## Live conflict notes
HIGH live overlap risk: same young-pool universe as KEYSTONE → Wave parking bias.
