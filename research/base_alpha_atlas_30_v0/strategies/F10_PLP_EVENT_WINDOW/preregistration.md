# F10_PLP_EVENT_WINDOW — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F10` Passive LP & fee capture
- name: Event-window LP
- economic_mechanism: Mint LP for short window around scheduled/detected volume events (e.g., post-mint burst) then withdraw.
- counterparty: event_takers

## Universe / Timing
- universe: pools with detectable events from F02/F08 definitions
- observation_time: event time
- feature_available_at: event features <=t

## Feature formulas
- `event_flag` = reuse F08 mint_burst or volume z event without future
- `net` = fee_income - IL - gas - withdraw

## Parameter search space (not selected)
```json
{
  "pre_min": [
    0,
    5
  ],
  "post_min": [
    10,
    30,
    60
  ],
  "range_width_ticks": [
    20,
    100
  ],
  "liq_usd": [
    50,
    100
  ],
  "min_event_z": [
    1.0,
    2.0
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: on event_flag, mint at t+pre_min, burn at t+post_min
- execution_delay: 1 bar
- position_size: liq_usd
- exit: fixed schedule
- stop: stop_il
- take_profit: n/a
- maximum_hold: post_min
- repeat_entry: one per event
- cooldown: 60m

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: placebo events
- difference_from_prior_failed_strategies: Scheduled LP around events; distinct from continuous highvol-lowtrend screen.

## Live conflict notes
Heavy LP sim.
