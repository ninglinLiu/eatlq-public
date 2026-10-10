# F10_PLP_HIGHVOL_LOWTREND — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F10` Passive LP & fee capture
- name: High volume low trend LP
- economic_mechanism: Select pools with high vol_volume_z and low |trend|; earn fees with limited IL.
- counterparty: noisy_takers

## Universe / Timing
- universe: V3 pools
- observation_time: 1h decision
- feature_available_at: features past only

## Feature formulas
- `vol_z` = zscore(volume_eth_1h)
- `trend_abs` = abs(px_h/px_{h-L}-1)

## Parameter search space (not selected)
```json
{
  "vol_z_min": [
    1.0,
    1.5
  ],
  "trend_max": [
    0.02,
    0.05
  ],
  "L": [
    6,
    12
  ],
  "H_h": [
    2,
    6
  ],
  "range_width_ticks": [
    50,
    200
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: open LP if vol_z>=min and trend_abs<=max
- execution_delay: +1h mint
- position_size: liq_usd {50,100}
- exit: H_h hours
- stop: stop_il {0.02,0.05}
- take_profit: early exit if fee_captured>=tp
- maximum_hold: H_h
- repeat_entry: no overlap
- cooldown: H_h

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: shuffle volume vs trend labels
- difference_from_prior_failed_strategies: Screen on volume/trend geometry for LP, not directional buy.

## Live conflict notes
LP sleeve; low swap-executor conflict.
