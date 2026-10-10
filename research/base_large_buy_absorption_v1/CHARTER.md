# LARGE_BUY_ABSORPTION_v1 — Charter

## Status
`FORWARD_LOCKED_RESPEC` — isolated from `base_actor_flow_oos_v0`.

## Hypothesis
On Base Uniswap V3 new WETH pools, a **relative large buy whose 5-minute price impact is absorbed (non-reversal)** carries incremental, post-latency, capturable information after controlling for pool structure and PIT depth.

This is an **order-flow** candidate, not a smart-money / actor-identity strategy.
Legacy alias: `WHALE_EVENT` (v0). Official name: `LARGE_BUY_ABSORPTION_v1`.

## Inheritance from v0
- v0 sealed as `ACTOR_FLOW_V0_DIAGNOSTIC_CONSUMED` / `HISTORICAL_CANDIDATE_POSITIVE` / `NOT_FORWARD_SPEC`.
- v1 historical rerun is labeled **`DIAGNOSTIC_REPLAY` only**.
- v1 inherits **no** sealed/OOS certification from v0.
- After gate pass: freeze config + code hash + schema, stamp new prospective T0, start observer-only shadow.

## Objective fixes only (no retuning)
1. Signal timing: wait full 5m absorption window before `signal_t`; entry after measured latency.
2. Loss truth: recovery floor, infra exclusion, censored no future-path PnL.
3. Primary estimand: fixed-$1000 scheduled wallet (not unconstrained sum).
4. Same-token one position; skip while holding.
5. Real PIT V3 depth control.
6. Honest naming (order-flow, not smart money).

Frozen unchanged from v0 intent:
- absorption window = 300s
- relative-size lookback = 1800s
- primary size = $50
- max_concurrent = 3
- TP ladder / primary horizon
- measured latency = 30s
- score quantile / top_k (selection mass)

## Hard bans
- Live trading before prospective confirmation
- Inheriting v0 OOS as forward evidence
- Retuning thresholds after seeing DIAGNOSTIC_REPLAY results
- Calling this "smart money" / actor identity
- Using unconstrained opportunity PnL as primary scorecard
- Lookahead: features after `signal_t`, or entry before `signal_t + latency`

## Ablations (frozen)
- `LARGE_BUY_ONLY`
- `ABSORPTION_ONLY`
- `LARGE_BUY_ABSORPTION` (primary)
- Controls: `POOL_ONLY`, capacity-matched random, wallet-id permutation (N/A for pure flow — size permutation within strata)

## Primary verdict vocabulary
- `DIAGNOSTIC_REPLAY_POSITIVE` / `DIAGNOSTIC_REPLAY_NEGATIVE`
- `SCHEDULED_EDGE_POSITIVE` / `SCHEDULED_EDGE_UNCONFIRMED`
- `LOOKAHEAD_FREE_PASS` / `LOOKAHEAD_FAIL`
- `LOSS_TRUTH_OK` / `LOSS_TRUTH_FAIL`
- `FREEZE_READY` / `NOT_READY`
- `TARGET_NO_GO` (monthly $1000) separate from scheduled edge
- Live: always `NO_GO` until prospective shadow clears
