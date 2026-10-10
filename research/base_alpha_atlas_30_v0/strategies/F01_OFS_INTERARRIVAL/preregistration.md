# F01_OFS_INTERARRIVAL — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F01` Order-flow sequence & microstructure
- name: Inter-arrival compression continuation
- economic_mechanism: Sudden compression of trade inter-arrival times predicts short-horizon continuation of the dominant sign in the compressed window.
- counterparty: slower_market_makers

## Universe / Timing
- universe: pools with >=20 swaps in 1h
- observation_time: bar close each 1m
- feature_available_at: inter-arrival stats from swaps with time<=bar_close

## Feature formulas
- `ia_i` = t_i - t_{i-1}
- `ia_med_L` = median(ia) over last L swaps
- `ia_med_S` = median(ia) over last S swaps, S<L
- `compress_ratio` = ia_med_L / (ia_med_S+eps)
- `dom_sign` = sign of sum(sign_i) over last S swaps

## Parameter search space (not selected)
```json
{
  "L": [
    50,
    100
  ],
  "S": [
    5,
    10
  ],
  "compress_min": [
    2.0,
    3.0,
    4.0
  ],
  "hold_bars": [
    2,
    5,
    10
  ],
  "stop_ret": [
    0.07,
    0.12
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: long if compress_ratio>=compress_min and dom_sign=+1
- execution_delay: 1 bar
- position_size: notional_usd {50,100}
- exit: time/stop/tp
- stop: ret<=-stop_ret
- take_profit: tp_ret in {0.08,0.15}
- maximum_hold: hold_bars
- repeat_entry: no overlap positions per pool
- cooldown: cooldown_bars {5,10}

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: time-warp inter-arrivals (shuffle gaps); edge must die
- difference_from_prior_failed_strategies: Not activity_heat count; uses gap compression ratio.

## Live conflict notes
Orthogonal to KEYSTONE fatal classifier.
