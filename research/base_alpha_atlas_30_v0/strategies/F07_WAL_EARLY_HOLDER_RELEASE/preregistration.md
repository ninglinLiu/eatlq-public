# F07_WAL_EARLY_HOLDER_RELEASE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F07` Wallet cohort & inventory migration
- name: Early-holder inventory release
- economic_mechanism: Wallets that bought in first A minutes net sell above threshold → bearish / avoid long.
- counterparty: early_insiders_distributing

## Universe / Timing
- universe: young pools
- observation_time: event/1m
- feature_available_at: cohort defined by buys with time<=first_swap+A

## Feature formulas
- `early_set` = tx_from with buy in [0,A]
- `net_sell_eth` = sell_weth - buy_weth of early_set in window W after A

## Parameter search space (not selected)
```json
{
  "A_min": [
    5,
    10,
    15
  ],
  "W_min": [
    10,
    30
  ],
  "sell_eth_min": [
    1.0,
    3.0
  ],
  "hold": [
    10,
    30
  ],
  "mode": [
    0,
    1
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: mode0: BEARISH no-long; mode1: long if net_sell below low threshold (holders retaining)
- execution_delay: 1 bar after W
- position_size: {0 or 50,100}
- exit: hold
- stop: 0.2
- take_profit: 0.15
- maximum_hold: hold
- repeat_entry: once
- cooldown: until age>A+W

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: cohort membership scramble
- difference_from_prior_failed_strategies: Inventory release ≠ copy-trade.

## Live conflict notes
Young-pool overlap with live.
