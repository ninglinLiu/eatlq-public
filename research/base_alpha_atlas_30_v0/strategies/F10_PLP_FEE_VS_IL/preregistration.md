# F10_PLP_FEE_VS_IL — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F10` Passive LP & fee capture
- name: Short-horizon fee vs IL
- economic_mechanism: Provide liquidity for H minutes; net = fee_income - IL - gas - rebalance - withdraw; enter only when predicted net>0.
- counterparty: takers_paying_fees

## Universe / Timing
- universe: V3 pools with fee + price path
- observation_time: LP open time
- feature_available_at: prediction features <= open

## Feature formulas
- `fee_income` = fee_tier * expected_volume * liq_share
- `IL` = IL(price_ratio) standard V3 formula for range
- `net` = fee_income - IL - gas - rebalance_cost - withdrawal_cost

## Parameter search space (not selected)
```json
{
  "H_min": [
    15,
    60,
    240
  ],
  "range_width_ticks": [
    10,
    50,
    200
  ],
  "liq_usd": [
    50,
    100
  ],
  "min_pred_net": [
    0.0,
    0.01
  ],
  "vol_forecast_w": [
    30,
    120
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: open LP if pred_net>=min_pred_net using vol_forecast from past w
- execution_delay: 1 bar to mint
- position_size: liq_usd
- exit: close after H_min
- stop: emergency withdraw if IL mid-path exceeds stop_il {0.02,0.05}
- take_profit: n/a take-profit beyond H; optional early exit if fee_captured>=tp_fee
- maximum_hold: H_min
- repeat_entry: no overlapping LP on same pool
- cooldown: H_min

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: fee path scramble vs price path
- difference_from_prior_failed_strategies: Full net components mandatory; APR-only forbidden.

## Live conflict notes
Different product (LP not swap); still not Wave1.
