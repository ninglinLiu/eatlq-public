# F02_LP_POST_MINT_DRIFT — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F02` LP & liquidity events
- name: Post-mint short-horizon drift
- economic_mechanism: Large liquidity mints temporarily improve executable depth; subsequent informed flow may drift price before inventory adjusts.
- counterparty: LPs_providing_inventory

## Universe / Timing
- universe: V3 pools with decoded mints
- observation_time: mint event time
- feature_available_at: mint row known_at; price path only after mint

## Feature formulas
- `mint_eth_proxy` = abs(amount0/1) mapped to WETH side when possible else UNKNOWN
- `mint_z` = zscore(mint_eth_proxy vs prior 24h mints)
- `fwd_ret_h` = px(t+h)/px(t)-1 using mid from ticks/swaps

## Parameter search space (not selected)
```json
{
  "mint_z_min": [
    1.0,
    1.5,
    2.0
  ],
  "h_bars": [
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
  "min_mint_eth": [
    0.5,
    2.0
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: long if mint_z>=mint_z_min and mint_eth_proxy>=min and subsequent 1st swap sign not forced (no lookahead)
- execution_delay: delay 1 bar after mint confirmation
- position_size: notional_usd {50,100}
- exit: exit h_bars/stop/tp
- stop: ret<=-stop_ret
- take_profit: ret>=tp_ret
- maximum_hold: h_bars
- repeat_entry: max 1 open per pool
- cooldown: cooldown 30 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: randomize mint timestamps within day; edge gone
- difference_from_prior_failed_strategies: Event-study on mint, not unsigned 'liquidity improved' narrative.

## Live conflict notes
Not KEYSTONE entry window logic.
