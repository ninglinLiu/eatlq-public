# F01_OFS_SIZE_SEQUENCE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F01` Order-flow sequence & microstructure
- name: Large-then-small size sequence toxicity
- economic_mechanism: After a large signed trade, a burst of tiny same-sign trades indicates informed follow-through vs spoof-like fade.
- counterparty: uninformed_retail_and_LP

## Universe / Timing
- universe: V3 WETH pools with trade count>=M in 15m
- observation_time: t = time of first small trade after large seed trade
- feature_available_at: all trades in windows ending at t only

## Feature formulas
- `size_eth_i` = |weth_amount_i|
- `large_flag_i` = size_eth_i >= Q_p(size_eth, lookback) with p in search
- `seq_score` = 1 if exists large_+ then >=k small_+ within dt_sec else 0
- `opp_fade` = count opposite-sign within dt_sec after large

## Parameter search space (not selected)
```json
{
  "p_large": [
    0.9,
    0.95
  ],
  "k_small": [
    3,
    5,
    8
  ],
  "dt_sec": [
    30,
    60,
    120
  ],
  "max_opp_fade": [
    0,
    1
  ],
  "hold_sec": [
    120,
    300
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: Enter long token if seq_score=1 and opp_fade<=max_opp_fade and large sign = +1
- execution_delay: delay_sec in {2,5} mapped to next bar
- position_size: notional_usd grid {50,100}
- exit: exit at hold_sec or stop/tp
- stop: ret<=-stop_ret stop_ret in {0.08,0.12}
- take_profit: ret>=tp_ret tp_ret in {0.12,0.20}
- maximum_hold: hold_sec
- repeat_entry: one entry per large-seed event
- cooldown: cooldown_sec in {300,600}

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: permute trade sizes within window keeping signs; edge should vanish
- difference_from_prior_failed_strategies: Uses ordered size transition, not aggregate buy_pressure_ratio used in older LASZLO features.

## Live conflict notes
Mechanism ≠ KEYSTONE features; execution conflict if co-deployed.
