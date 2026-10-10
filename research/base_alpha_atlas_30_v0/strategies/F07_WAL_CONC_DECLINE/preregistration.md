# F07_WAL_CONC_DECLINE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F07` Wallet cohort & inventory migration
- name: Holder concentration decline
- economic_mechanism: HHI of token balances among top wallets declines at rate d → healthier distribution long.
- counterparty: concentrated_whales_distributing_to_broader_set

## Universe / Timing
- universe: tokens with transfer data
- observation_time: 5m/1h snapshots
- feature_available_at: balances known_at from transfers <=t

## Feature formulas
- `HHI_t` = sum share_i^2 top K holders
- `dHHI` = HHI_t - HHI_{t-w}

## Parameter search space (not selected)
```json
{
  "K": [
    10,
    20
  ],
  "w": [
    12,
    24
  ],
  "dHHI_max": [
    -0.02,
    -0.05
  ],
  "hold": [
    12,
    24
  ],
  "stop_ret": [
    0.2,
    0.3
  ]
}
```
combo_count = 32 (hard cap 512)

## Execution skeleton
- entry: long if dHHI<=dHHI_max and liquidity filter
- execution_delay: 1 bar
- position_size: {50,100}
- exit: hold
- stop: stop_ret
- take_profit: 0.15
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: w

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: holder permute
- difference_from_prior_failed_strategies: Concentration path ≠ flow run-length.

## Live conflict notes
Heavy.
