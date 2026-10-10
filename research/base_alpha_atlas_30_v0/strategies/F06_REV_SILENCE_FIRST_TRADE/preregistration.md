# F06_REV_SILENCE_FIRST_TRADE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F06` Silence & revival
- name: First trade after silence
- economic_mechanism: After silence_gap_sec with zero swaps, the first swap's sign and size predict short continuation.
- counterparty: sleeping_LP_inventory

## Universe / Timing
- universe: pools with at least one silence >=G historically
- observation_time: event time of revival swap
- feature_available_at: gap measured with prior swap time known

## Feature formulas
- `gap_sec` = t_i - t_{i-1}
- `revive_sign` = sign_i
- `revive_eth` = size_eth_i

## Parameter search space (not selected)
```json
{
  "G_min": [
    1800,
    3600,
    7200
  ],
  "min_eth": [
    0.2,
    0.5,
    1.0
  ],
  "hold_sec": [
    300,
    900
  ],
  "stop_ret": [
    0.1,
    0.2
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: long if gap_sec>=G_min and revive_sign=+1 and revive_eth>=min_eth
- execution_delay: delay_sec {2,5}
- position_size: {50,100}
- exit: hold_sec
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold_sec
- repeat_entry: one per silence episode
- cooldown: G_min

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: shuffle gaps
- difference_from_prior_failed_strategies: Silence definition hard; ≠ continuous momentum.

## Live conflict notes
Different horizon than 10-15m KEYSTONE window.
