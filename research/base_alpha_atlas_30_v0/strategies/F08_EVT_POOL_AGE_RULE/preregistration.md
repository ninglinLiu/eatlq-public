# F08_EVT_POOL_AGE_RULE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F08` Protocol & event driven
- name: Deterministic pool-age liquidity rule
- economic_mechanism: Pure age-window + min pre-volume + max p_impact_proxy rule without ML fatal model — structural contrast to KEYSTONE.
- counterparty: early_LP_and_snipers

## Universe / Timing
- universe: new V3 WETH pools
- observation_time: age minutes since first_swap
- feature_available_at: first_swap known; features from [0,age_entry]

## Feature formulas
- `age_m` = (t-first_swap)/60
- `pre_vol_eth` = sum |weth| in [0, age_m]
- `impact_proxy` = abs(ret) / (pre_vol_eth+eps)

## Parameter search space (not selected)
```json
{
  "age_enter_lo": [
    8,
    10
  ],
  "age_enter_hi": [
    12,
    15,
    20
  ],
  "min_pre_vol": [
    0.5,
    1.0,
    2.0
  ],
  "max_impact_proxy": [
    0.05,
    0.1
  ],
  "hold_min": [
    20,
    60,
    120
  ]
}
```
combo_count = 108 (hard cap 512)

## Execution skeleton
- entry: long if age in [lo,hi] and pre_vol>=min and impact_proxy<=max
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold_min
- stop: 0.5 hard stop like live optional stress
- take_profit: tp {0.2,0.5}
- maximum_hold: hold_min
- repeat_entry: once per pool
- cooldown: until age>hi

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: compare to KEYSTONE signals overlap; must show incremental EV after excluding overlapping entries
- difference_from_prior_failed_strategies: Explicitly non-ML and different feature set than p_fatal; still HIGH live universe overlap → research contrast only.

## Live conflict notes
REJECT_LIVE_CONFLICT risk if deployed; kept as contrast sleeve for orthogonality/falsification of KEYSTONE.
