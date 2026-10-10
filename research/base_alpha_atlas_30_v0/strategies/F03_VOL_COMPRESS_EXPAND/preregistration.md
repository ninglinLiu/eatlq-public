# F03_VOL_COMPRESS_EXPAND — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F03` Volatility regime transitions
- name: Volatility compression then expansion
- economic_mechanism: Low realized vol regimes store energy; breakout expansion continues briefly.
- counterparty: option-like_speculators

## Universe / Timing
- universe: pools with continuous 1m prices >= H hours/day
- observation_time: 1m close
- feature_available_at: vol from past returns only

## Feature formulas
- `r_t` = log px_t - log px_{t-1}
- `rv_s` = sqrt(sum r^2 over s bars)
- `rv_l` = sqrt(sum r^2 over l bars)
- `compress` = rv_s/rv_l
- `expand_flag` = rv_s_next_window / rv_s >= e_min computed without peek: use causal trigger rv_s_t / rv_s_{t-s} 

## Parameter search space (not selected)
```json
{
  "s": [
    15,
    30
  ],
  "l": [
    120,
    240
  ],
  "compress_max": [
    0.35,
    0.5
  ],
  "expand_min": [
    1.5,
    2.0
  ],
  "hold": [
    10,
    20
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: enter long in breakout direction sign(sum r over last s) when compress_max met then expand_min met
- execution_delay: 1 bar after expand trigger
- position_size: notional {50,100}
- exit: hold/stop/tp
- stop: stop_ret {0.1,0.2}
- take_profit: tp {0.1,0.2}
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: 20 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: vol block bootstrap
- difference_from_prior_failed_strategies: Explicit compress→expand state machine vs raw momentum.

## Live conflict notes
Classic vol; not KEYSTONE meme fatal model.
