# F04_XS_LEADER_TO_SECOND — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F04` Cross-sectional capital rotation
- name: Leader to second-tier rotation
- economic_mechanism: Within a 1h cohort, capital rotates from top return leader to 2nd-4th rank names with rising relative volume.
- counterparty: late_chasers_on_leader

## Universe / Timing
- universe: cross-section of WETH V3 pools active in hour
- observation_time: hour close
- feature_available_at: ranks from past hour only

## Feature formulas
- `ret_1h` = px_h/px_{h-1}-1
- `rank_ret` = rank(ret_1h)
- `rel_vol` = vol_1h / mean(vol_1h of cohort)
- `leader` = rank_ret==1
- `second_ok` = rank_ret in {2,3,4} and rel_vol rising vs prior hour

## Parameter search space (not selected)
```json
{
  "cohort_min_pools": [
    10,
    20
  ],
  "hold_h": [
    1,
    2,
    4
  ],
  "rel_vol_min": [
    1.0,
    1.5
  ],
  "stop_ret": [
    0.15,
    0.25
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```
combo_count = 48 (hard cap 512)

## Execution skeleton
- entry: long second_ok names when leader ret_1h > leader_min (leader_min in {0.05,0.10})
- execution_delay: next hour open proxy = +1 bar
- position_size: equal weight among signals capped notional_total {100,200}
- exit: hold_h
- stop: stop_ret
- take_profit: tp_ret
- maximum_hold: hold_h
- repeat_entry: rebalance hourly
- cooldown: 0

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: rank shuffle within hour
- difference_from_prior_failed_strategies: Cross-sectional relative, not single-pool momentum.

## Live conflict notes
XS ≠ KEYSTONE single-pool age entry.
