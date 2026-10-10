# CHARTER — base_large_buy_absorption_v2

## Role
Forward-locked order-flow research on Base Uniswap V3 **new WETH pools** (first 48h).
Not smart money. Not live trading.

## Why v2
1. Correct the over-kill narrative from v1: prove/refute **per hypothesis**, never global-kill.
2. Preregister user timing hypothesis: **5s absorption / 0s latency** (0s = optimistic upper bound).
3. Phase 0 showed intended-universe data was already complete vs ClickHouse; sparse calendar is **birth clustering**, not silent drop.
4. Add REGIME/SELECTION/EXECUTION PnL attribution and PIT intraday ranking under 3-slot capital.

## Hard gates
- Lookahead-free signal timing (`decision_time = signal_t`, entry after absorb known).
- Loss-truth conservative floors.
- Scheduled wallet is primary estimand.
- Sensitivity curves are diagnostic only — **forbidden** as selection.
- No inherit of v0/v1 OOS certification.

## Universe (frozen design)
`discovered_pools` v3 + `has_weth_leg=1`, first swap in calendar window, swaps in first 48h only.
Do not confuse with full Base V3 warehouse day coverage.

## Deliverables
- `out/diag/DATA_COVERAGE_DIAGNOSTIC.md`
- `out/LARGE_BUY_ABSORPTION_v2_REPLAY.json`
- `out/LARGE_BUY_ABSORPTION_v2_REVIEW.md`
- Observer-only shadow (edge claim only if selection+ranking PROVEN)
