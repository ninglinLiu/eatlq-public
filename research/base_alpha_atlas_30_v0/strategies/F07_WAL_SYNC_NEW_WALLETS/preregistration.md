# F07_WAL_SYNC_NEW_WALLETS — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F07` Wallet cohort & inventory migration
- name: Synchronized new-wallet entry
- economic_mechanism: k distinct tx_from with first-seen label NEW enter same pool within dt, excluding routers.
- counterparty: dispersed_coordinated_demand

## Universe / Timing
- universe: pools with tx_from on swaps
- observation_time: event
- feature_available_at: wallet age labels known_at<=t

## Feature formulas
- `new_wallet` = first_seen_tx_time >= t - W_new
- `sync_count` = count distinct new_wallet buyers in dt
- `router_exclude` = tx_from not in static router set

## Parameter search space (not selected)
```json
{
  "W_new": [
    3600,
    86400
  ],
  "dt": [
    60,
    300
  ],
  "k_min": [
    3,
    5,
    8
  ],
  "hold": [
    10,
    30
  ],
  "stop_ret": [
    0.15,
    0.25
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long if sync_count>=k_min
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold
- stop: stop_ret
- take_profit: tp {0.1,0.2}
- maximum_hold: hold
- repeat_entry: one per cluster
- cooldown: dt

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: wallet id permute
- difference_from_prior_failed_strategies: Cohort sync ≠ single smart-money follow.

## Live conflict notes
Heavy; not Wave1.
