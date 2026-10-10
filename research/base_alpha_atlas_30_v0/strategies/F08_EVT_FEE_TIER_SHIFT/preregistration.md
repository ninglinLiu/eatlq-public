# F08_EVT_FEE_TIER_SHIFT — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F08` Protocol & event driven
- name: Fee-tier / pool migration proxy
- economic_mechanism: When same token pair appears with new fee tier pool and volume migrates, long migrating pool / avoid dying pool.
- counterparty: migrating_flow_takers

## Universe / Timing
- universe: token pairs with >=2 fee tiers
- observation_time: 1h
- feature_available_at: pair mapping known

## Feature formulas
- `share_new` = vol_new_tier / (vol_all_tiers+eps)
- `share_delta` = share_new_t - share_new_{t-1}

## Parameter search space (not selected)
```json
{
  "share_delta_min": [
    0.1,
    0.2
  ],
  "hold_h": [
    2,
    6,
    12
  ],
  "min_vol_eth": [
    5,
    20
  ],
  "stop_ret": [
    0.15,
    0.25
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long new-tier pool if share_delta>=min and vol filters
- execution_delay: +1h
- position_size: {50,100}
- exit: hold_h
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold_h
- repeat_entry: one per migration episode
- cooldown: 24h

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: tier labels shuffle
- difference_from_prior_failed_strategies: Protocol structure change; not micro runs.

## Live conflict notes
Low live conflict; data missing.
