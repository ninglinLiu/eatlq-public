# F05_REG_BREADTH_EXPAND — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F05` Base-wide regime
- name: Active breadth expansion gate
- economic_mechanism: Trade any subordinate alpha only when unique active pools/hour rises above quantile; this strategy itself goes long a breadth ETF-like top-volume basket.
- counterparty: narrow_market_makers

## Universe / Timing
- universe: Base-wide hourly
- observation_time: hour
- feature_available_at: breadth from past hour counts

## Feature formulas
- `breadth_h` = count distinct pools with >=q swaps in hour h
- `breadth_z` = zscore vs trailing D days

## Parameter search space (not selected)
```json
{
  "q": [
    5,
    10
  ],
  "D": [
    7,
    14
  ],
  "z_min": [
    0.5,
    1.0,
    1.5
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: if breadth_z>=z_min long top-k pools by volume that hour
- execution_delay: +1h
- position_size: total {100,200}
- exit: hold_h
- stop: 0.2
- take_profit: 0.15
- maximum_hold: hold_h
- repeat_entry: hourly
- cooldown: 0

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: time permute breadth series
- difference_from_prior_failed_strategies: Timing gate family: when to trade, not toxic-flow ML.

## Live conflict notes
Regime sleeve; can gate others later.
