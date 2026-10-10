# F02_LP_DEPTH_LEAD — Preregistration (Phase 0.5)

## Status
```text
STAGE = DESIGN_ONLY
parameter_selection = FORBIDDEN
historical_pnl_inspection = FORBIDDEN
```

## Identity
- family: `F02` LP & liquidity events
- name: Depth thinning leads returns
- economic_mechanism: Measurable depth proxy declines before adverse selection; trade mean-reversion or momentum per sign of thinning.
- counterparty: toxic_takers

## Universe / Timing
- universe: pools with mint/burn/swap to proxy depth
- observation_time: 1m bars
- feature_available_at: depth_proxy from events <= bar_close

## Feature formulas
- `depth_proxy_t` = cum_mints_eth - cum_burns_eth + baseline (UNKNOWN if incomplete)
- `thin_rate` = (depth_{t-w}-depth_t)/(abs(depth_{t-w})+eps)
- `ret_1m` = px_t/px_{t-1}-1

## Parameter search space (not selected)
```json
{
  "w": [
    5,
    15,
    30
  ],
  "thin_min": [
    0.1,
    0.2,
    0.35
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.1,
    0.15
  ],
  "mode": [
    0,
    1
  ]
}
```
combo_count = 72 (hard cap 512)

## Execution skeleton
- entry: if mode=0 fade thinning (long after thin if ret_1m<0); if mode=1 momentum (long if thin and ret_1m>0). mode is structural arm, not free continuous param spam.
- execution_delay: 1 bar
- position_size: notional {50,100}
- exit: hold/stop/tp
- stop: ret<=-stop_ret
- take_profit: tp in {0.08,0.15}
- maximum_hold: hold
- repeat_entry: no overlap
- cooldown: 10 bars

## Cost / failure / falsification
- cost_model: see catalog COST_MODEL (UNKNOWN ≠ 0)
- failure_handling: {"missing_feature": "skip_signal", "unknown_cost_field": "do_not_treat_as_zero; mark INCONCLUSIVE_COST", "no_sell_quote": "force flat mark with failed_sell stress; no phantom fill", "partial_history": "exclude pool from fold"}
- main_falsification: placebo depth from shuffled mint/burn
- difference_from_prior_failed_strategies: Lead-lag depth→price, not 'liquidity improved' slogan.

## Live conflict notes
Heavy data; parked from Wave1.
