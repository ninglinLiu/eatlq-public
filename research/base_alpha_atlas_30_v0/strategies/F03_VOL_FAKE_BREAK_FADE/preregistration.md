# F03_VOL_FAKE_BREAK_FADE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F03` Volatility regime transitions
- name: Failed breakout fade
- economic_mechanism: Price closes beyond Donchian high then reverses within m bars back inside → fade.
- counterparty: breakout_chasers

## Universe / Timing
- universe: OHLCV pools
- observation_time: 1m
- feature_available_at: Donchian from past only

## Feature formulas
- `don_hi` = max(high)_{t-n..t-1}
- `don_lo` = min(low)_{t-n..t-1}
- `break_up` = close_t > don_hi
- `fail` = break_up and min(close)_{t+1..t+m} < don_hi (evaluated at t+m without using >t+m)

## Parameter search space (not selected)
```json
{
  "n": [
    30,
    60,
    120
  ],
  "m": [
    3,
    5,
    10
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.08,
    0.12
  ],
  "tp_ret": [
    0.06,
    0.12
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: At confirmation time t+m when fail=True, enter short-token bearish mark / no long; long-only atlas records BEARISH_FADE signal quality. Optional: long if failed break_down symmetric.
- execution_delay: confirm at t+m, delay 1
- position_size: 0 long or {50,100} on upside failed breakdown arm only
- exit: hold/stop/tp
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: n bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: path scramble inside window
- difference_from_prior_failed_strategies: Requires failure confirmation; not plain breakout.

## Live conflict notes
Mostly bearish fade; low buy conflict with KEYSTONE.
