# F09_EXE_QUOTE_STALE — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F09` Quote, routing & execution anomalies
- name: Stale quote recovery
- economic_mechanism: If mid moves >=x without swaps for g seconds (stale), trade mean-reversion toward last trade price when swaps resume.
- counterparty: stale_arbitrageurs_slow

## Universe / Timing
- universe: pools with external mid OR tick-derived mid
- observation_time: event
- feature_available_at: stale detection causal

## Feature formulas
- `stale` = no swaps for g sec AND abs(mid_now/last_trade_px-1)>=x
- `resume` = first swap after stale

## Parameter search space (not selected)
```json
{
  "g_sec": [
    15,
    30,
    60
  ],
  "x": [
    0.01,
    0.02,
    0.05
  ],
  "hold_sec": [
    30,
    120
  ],
  "stop_ret": [
    0.05,
    0.1
  ],
  "tp_ret": [
    0.005,
    0.02
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: on resume, long if mid>last_trade (expect down? → fade: side = -sign(mid/last_trade-1))
- execution_delay: delay_sec {1,2}
- position_size: {50,100}
- exit: hold_sec
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold_sec
- repeat_entry: one per stale episode
- cooldown: g_sec

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: inject fake stale flags
- difference_from_prior_failed_strategies: Latency/staleness microstructure; not trend.

## Live conflict notes
No arb executor; research only; data missing.
