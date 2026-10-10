# F02_LP_POST_BURN_PRESSURE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F02` LP & liquidity events
- name: Post-burn sell pressure
- economic_mechanism: Large burns reduce depth; residual inventory seekers sell into thinner book → negative drift.
- counterparty: remaining_LPs_and_holders

## Universe / Timing
- universe: V3 pools with burns
- observation_time: burn event time
- feature_available_at: burn known_at

## Feature formulas
- `burn_eth_proxy` = WETH-normalized burned liquidity proxy
- `burn_z` = zscore vs 24h burns
- `depth_proxy_drop` = 1 - depth_after/depth_before if available else UNKNOWN

## Parameter search space (not selected)
```json
{
  "burn_z_min": [
    1.0,
    2.0
  ],
  "hold_bars": [
    5,
    15,
    30
  ],
  "stop_ret": [
    0.1,
    0.2
  ],
  "tp_ret": [
    0.08,
    0.15
  ],
  "min_burn_eth": [
    0.5,
    2.0
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long-only atlas: enter SHORT-TOKEN via avoiding; design records SELL-token / flat-bias: open short-token synthetic = do not long; optional future short. For long-only: enter opposite pool? → specify: signal is BEARISH; strategy takes no long; paper metric tracks short mid. Implementation: side=SELL_SIGNAL_ONLY (no buy).
- execution_delay: 1 bar
- position_size: 0 USD long; tracks bearish mark-to-market for falsification only OR future short sleeve
- exit: signal horizon hold_bars
- stop: n/a long
- take_profit: n/a long
- maximum_hold: hold_bars
- repeat_entry: n/a
- cooldown: cooldown 30 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: burn size shuffle
- difference_from_prior_failed_strategies: Opposite of mint-drift; burn-specific.

## Live conflict notes
Bearish signal sleeve; low live buy conflict but still research-only.
