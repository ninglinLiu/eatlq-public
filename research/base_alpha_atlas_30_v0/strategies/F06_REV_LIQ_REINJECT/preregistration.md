# F06_REV_LIQ_REINJECT — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F06` Silence & revival
- name: Liquidity re-injection revival
- economic_mechanism: After silence, a mint above threshold then swaps resume → long.
- counterparty: reactivated_LP

## Universe / Timing
- universe: pools with mint after silence
- observation_time: mint time
- feature_available_at: mint+prior gap known

## Feature formulas
- `gap_before_mint` = mint_time - last_swap_before
- `mint_eth` = WETH proxy
- `swaps_after` = count swaps in (mint, mint+dt]

## Parameter search space (not selected)
```json
{
  "G_min": [
    3600,
    7200
  ],
  "mint_min_eth": [
    0.5,
    2.0
  ],
  "dt_sec": [
    300,
    900
  ],
  "min_swaps_after": [
    2,
    5
  ],
  "hold": [
    10,
    30
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: long when gap_before_mint>=G_min and mint_eth>=min and swaps_after>=min (confirm at mint+dt)
- execution_delay: 1 bar after confirm
- position_size: {50,100}
- exit: hold
- stop: 0.15
- take_profit: 0.15
- maximum_hold: hold
- repeat_entry: one per episode
- cooldown: G_min

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: mint/swap order scramble
- difference_from_prior_failed_strategies: Joint mint+silence+resume; not mint-only F02.

## Live conflict notes
Event combo ≠ KEYSTONE.
