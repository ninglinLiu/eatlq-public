# F08_EVT_MINT_BURST — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F08` Protocol & event driven
- name: Clustered mint burst event
- economic_mechanism: >=k mints from distinct owners within dt after pool birth → structural attention event.
- counterparty: multi_LP_bootstrappers

## Universe / Timing
- universe: new pools
- observation_time: event
- feature_available_at: mints <=t

## Feature formulas
- `mint_burst` = count distinct owner mints in dt after first_mint

## Parameter search space (not selected)
```json
{
  "dt_sec": [
    300,
    900,
    1800
  ],
  "k_min": [
    3,
    5
  ],
  "age_max_m": [
    30,
    60
  ],
  "hold": [
    15,
    45
  ],
  "stop_ret": [
    0.2,
    0.3
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long if mint_burst>=k_min and age_m<=age_max
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold
- stop: stop_ret
- take_profit: 0.15
- maximum_hold: hold
- repeat_entry: once
- cooldown: age_max

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: owner ids permute
- difference_from_prior_failed_strategies: Multi-LP burst ≠ single mint drift F02.

## Live conflict notes
Young pool overlap medium-high.
