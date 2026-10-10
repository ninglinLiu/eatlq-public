# Alpha Leaderboard

All values are research estimates. “Net” uses a fixed **60 bps per leg** sensitivity, not observed fills. Pool-hour signals overlap and cannot be added as independent trades.

## Strongest gross structure

| Rank | Candidate | Deep status | Deep result | Main caveat |
|---:|---|---|---|---|
| 1 | F03 directional flow persistence | REPLAY_SURVIVOR historically; promoted only to zero-order SHADOW_CANDIDATE | 4h gross 238.9 bps; proxy net 118.9 bps | aggregated proxy partly resembles momentum; no executable quotes/fills |
| 2 | S02 participant-breadth disaster veto | MECHANISM_PLAUSIBLE | future-worst-move separation 267.0 bps | BH q=0.168; no sellability label |
| 3 | C03 post-dormancy reactivation | MECHANISM_PLAUSIBLE | 4h reactivation-probability separation 8.9% | non-monetized and zero-hour grid incomplete |

## Strongest net-return candidate

F03 only. It survives 30/60/100 bps-per-leg stress at approximately 179/119/39 bps, but fails at 200 bps per leg. One-hour delayed gross spread is about 194.9 bps. This remains a quote-free upper-bound replay, not realized P&L.

## Strongest disaster-avoidance signal

S02: high within-hour unique-sender breadth is associated with a less adverse future 24h path. The effect is positive in 75% of replay months and remains positive after removing the largest 1% absolute outcomes, but does not survive the 48-test correction and lacks failed-sale observations.

## Strongest Meta-Alpha

No Meta-Alpha survived. R02 (dispersion-conditioned reversal) is **KILLED** at the stated 60 bps-per-leg stress and changes sign between replay halves.

## Strongest anomaly finding

A01 (wallet-breadth/activity mismatch) is DISCOVERY_ONLY: the apparent volatility separation falls from 0.420 to 0.033 after removing the largest 1% outcomes, so the “effect” is mostly a rare-tail locator.

## Forward Shadow

F03 is the sole SHADOW_CANDIDATE for **zero-order data collection**. The initialized ledger contains 4 signals at 2026-08-14T13:00:00+00:00, zero matured forward outcomes, zero orders, and no executor. Its goal is to collect unseen outcomes and executable read-only quote/depth/failure facts.

## Explicitly killed in Deep Dive

| Candidate | Kill |
|---|---|
| F01 raw flow imbalance | -5.0 bps after cost proxy |
| H01 close-at-high continuation | -24.7 bps after cost proxy; day-cluster CI crosses zero |
| RV01 same-pair convergence | only 4.00 gross bps, 116.0 bps short of break-even, top-pool concentration 43.0% |
| R02 dispersion reversal | -18.8 bps after cost proxy and temporal sign instability |

Rapid screening additionally marks 5 predefined hypotheses KILLED, 6 DATA_INSUFFICIENT, 29 DISCOVERY_ONLY and 8 GROSS_STRUCTURE before Deep Dive overrides.
