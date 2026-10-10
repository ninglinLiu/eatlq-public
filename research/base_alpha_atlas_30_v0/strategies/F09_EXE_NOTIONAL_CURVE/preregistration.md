# F09_EXE_NOTIONAL_CURVE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F09` Quote, routing & execution anomalies
- name: Nonlinear notional slippage curve
- economic_mechanism: Fit impact ≈ a*n^b on historical swaps; enter only notionals where modeled net EV>0 under stress.
- counterparty: size_takers_ignoring_convexity

## Universe / Timing
- universe: dense pools
- observation_time: offline fit windows
- feature_available_at: fit on past only

## Feature formulas
- `model` = impact_hat(n)=a*n^b
- `edge_hat` = expected_move_hat - impact_hat(n) - costs

## Parameter search space (not selected)
```json
{
  "fit_lookback_h": [
    6,
    24
  ],
  "n_usd": [
    50,
    100
  ],
  "hold": [
    5,
    15
  ],
  "min_edge_hat": [
    0.0,
    0.01
  ],
  "dom_S": [
    5,
    10
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: enter long only if edge_hat(n_usd)>=min_edge_hat and separate alpha direction from F01 sign run (uses dom_sign)
- execution_delay: 1 bar
- position_size: n_usd
- exit: hold
- stop: 0.1
- take_profit: 0.1
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: 10

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: scramble sizes vs returns in fit
- difference_from_prior_failed_strategies: Position sizing economics; depends on external direction signal defined as dom_sign from last S swaps.

## Live conflict notes
Sizing overlay risk if combined with live.
