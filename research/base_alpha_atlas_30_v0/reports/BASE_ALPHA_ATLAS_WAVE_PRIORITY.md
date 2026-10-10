# BASE_ALPHA_ATLAS_WAVE_PRIORITY

Compute order only — **not** promotion to Paper/Live.

## Scoring dimensions (1-5)
`economic_plausibility, novelty, data_readiness, expected_gross_edge_required, execution_feasibility, capacity, lookahead_risk, overfitting_risk, live_conflict_risk, estimated_compute_cost`

Composite used for ranking favors readiness & feasibility; penalizes live conflict, compute, lookahead, overfitting.

## Wave 1 (max 6)

1. `F03_VOL_JUMP_CONTINUE` — class `A_OFFLINE_DATA_LIKELY_AVAILABLE` — score 29 — Jump continuation
2. `F06_REV_VOL_BEFORE_PRICE` — class `A_OFFLINE_DATA_LIKELY_AVAILABLE` — score 29 — Volume revival before price
3. `F05_REG_NEWPOOL_DILUTION` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 33 — New-pool dilution avoidance
4. `F01_OFS_SIGN_RUNLEN` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 27 — Signed-trade run-length persistence
5. `F10_PLP_HIGHVOL_LOWTREND` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 26 — High volume low trend LP
6. `F02_LP_POST_MINT_DRIFT` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 25 — Post-mint short-horizon drift

## Wave 2 (max 9)

1. `F03_VOL_COMPRESS_EXPAND` — class `A_OFFLINE_DATA_LIKELY_AVAILABLE` — score 28 — Volatility compression then expansion
2. `F03_VOL_FAKE_BREAK_FADE` — class `A_OFFLINE_DATA_LIKELY_AVAILABLE` — score 28 — Failed breakout fade
3. `F06_REV_SILENCE_FIRST_TRADE` — class `A_OFFLINE_DATA_LIKELY_AVAILABLE` — score 28 — First trade after silence
4. `F01_OFS_SIZE_SEQUENCE` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 27 — Large-then-small size sequence toxicity
5. `F01_OFS_INTERARRIVAL` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 27 — Inter-arrival compression continuation
6. `F02_LP_POST_BURN_PRESSURE` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 25 — Post-burn sell pressure
7. `F04_XS_LEADER_TO_SECOND` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 25 — Leader to second-tier rotation
8. `F05_REG_BREADTH_EXPAND` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 25 — Active breadth expansion gate
9. `F06_REV_LIQ_REINJECT` — class `B_SMALL_BOUNDED_EXPORT_REQUIRED` — score 25 — Liquidity re-injection revival

## Parking

- `F02_LP_DEPTH_LEAD` — `C_PRODUCTION_DB_HEAVY` — live_conflict=3 — Depth thinning leads returns
- `F04_XS_SAME_AGE_RS` — `B_SMALL_BOUNDED_EXPORT_REQUIRED` — live_conflict=5 — Same-age cohort relative strength
- `F04_XS_OLD_TO_NEW_MIGRATE` — `C_PRODUCTION_DB_HEAVY` — live_conflict=4 — Old-to-new capital migration
- `F05_REG_GAS_CONGESTION` — `D_DATA_UNAVAILABLE` — live_conflict=2 — Gas congestion entry filter
- `F07_WAL_SYNC_NEW_WALLETS` — `C_PRODUCTION_DB_HEAVY` — live_conflict=3 — Synchronized new-wallet entry
- `F07_WAL_EARLY_HOLDER_RELEASE` — `C_PRODUCTION_DB_HEAVY` — live_conflict=4 — Early-holder inventory release
- `F07_WAL_CONC_DECLINE` — `C_PRODUCTION_DB_HEAVY` — live_conflict=3 — Holder concentration decline
- `F08_EVT_POOL_AGE_RULE` — `B_SMALL_BOUNDED_EXPORT_REQUIRED` — live_conflict=5 — Deterministic pool-age liquidity rule
- `F08_EVT_MINT_BURST` — `B_SMALL_BOUNDED_EXPORT_REQUIRED` — live_conflict=4 — Clustered mint burst event
- `F08_EVT_FEE_TIER_SHIFT` — `D_DATA_UNAVAILABLE` — live_conflict=2 — Fee-tier / pool migration proxy
- `F09_EXE_IMPACT_ASYMM` — `B_SMALL_BOUNDED_EXPORT_REQUIRED` — live_conflict=3 — Buy/sell impact asymmetry
- `F09_EXE_NOTIONAL_CURVE` — `B_SMALL_BOUNDED_EXPORT_REQUIRED` — live_conflict=3 — Nonlinear notional slippage curve
- `F09_EXE_QUOTE_STALE` — `D_DATA_UNAVAILABLE` — live_conflict=2 — Stale quote recovery
- `F10_PLP_FEE_VS_IL` — `C_PRODUCTION_DB_HEAVY` — live_conflict=2 — Short-horizon fee vs IL
- `F10_PLP_EVENT_WINDOW` — `C_PRODUCTION_DB_HEAVY` — live_conflict=2 — Event-window LP

## Wave 1 selection rationale

- Prefer `A_OFFLINE_DATA_LIKELY_AVAILABLE` then light `B_`
- Exclude `C_PRODUCTION_DB_HEAVY` and `D_DATA_UNAVAILABLE`
- Exclude live_conflict_risk=5 and KEYSTONE-contrast age rule from Wave 1 execution candidates
- Favor quick falsifiability and lower compute

```json
{
  "wave1": [
    "F03_VOL_JUMP_CONTINUE",
    "F06_REV_VOL_BEFORE_PRICE",
    "F05_REG_NEWPOOL_DILUTION",
    "F01_OFS_SIGN_RUNLEN",
    "F10_PLP_HIGHVOL_LOWTREND",
    "F02_LP_POST_MINT_DRIFT"
  ],
  "wave2": [
    "F03_VOL_COMPRESS_EXPAND",
    "F03_VOL_FAKE_BREAK_FADE",
    "F06_REV_SILENCE_FIRST_TRADE",
    "F01_OFS_SIZE_SEQUENCE",
    "F01_OFS_INTERARRIVAL",
    "F02_LP_POST_BURN_PRESSURE",
    "F04_XS_LEADER_TO_SECOND",
    "F05_REG_BREADTH_EXPAND",
    "F06_REV_LIQ_REINJECT"
  ],
  "parking": [
    "F02_LP_DEPTH_LEAD",
    "F04_XS_SAME_AGE_RS",
    "F04_XS_OLD_TO_NEW_MIGRATE",
    "F05_REG_GAS_CONGESTION",
    "F07_WAL_SYNC_NEW_WALLETS",
    "F07_WAL_EARLY_HOLDER_RELEASE",
    "F07_WAL_CONC_DECLINE",
    "F08_EVT_POOL_AGE_RULE",
    "F08_EVT_MINT_BURST",
    "F08_EVT_FEE_TIER_SHIFT",
    "F09_EXE_IMPACT_ASYMM",
    "F09_EXE_NOTIONAL_CURVE",
    "F09_EXE_QUOTE_STALE",
    "F10_PLP_FEE_VS_IL",
    "F10_PLP_EVENT_WINDOW"
  ]
}
```

