# F04_XS_OLD_TO_NEW_MIGRATE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F04` Cross-sectional capital rotation
- name: Old-to-new capital migration
- economic_mechanism: When aggregate notional in pools age>24h falls while age<2h rises, long a basket of newest qualifying pools.
- counterparty: migrating_speculators

## Universe / Timing
- universe: Base V3 WETH universe hourly
- observation_time: hour close
- feature_available_at: age from first_swap

## Feature formulas
- `flow_old` = sum buy_weth in age>24h
- `flow_new` = sum buy_weth in age<2h
- `migrate_ratio` = flow_new/(flow_old+eps)

## Parameter search space (not selected)
```json
{
  "ratio_min": [
    1.0,
    1.5,
    2.0
  ],
  "basket_k": [
    3,
    5
  ],
  "hold_h": [
    1,
    2
  ],
  "stop_ret": [
    0.2,
    0.3
  ],
  "min_pool_vol_eth": [
    1.0,
    3.0
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: if migrate_ratio>=ratio_min long top-k newest pools by flow_new with vol filter
- execution_delay: +1h
- position_size: total notional {100,200} split k ways
- exit: hold_h
- stop: stop_ret
- take_profit: tp {0.15,0.25}
- maximum_hold: hold_h
- repeat_entry: hourly
- cooldown: 0

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: permute pool ages
- difference_from_prior_failed_strategies: Macro rotation aggregate, not single-pool micro.

## Live conflict notes
Heavy; also universe overlap.
