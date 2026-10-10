# Clanker Alpha Hypotheses v0.2 (Pre-Registered)

```text
status = PRE_REGISTERED
frozen_before_new_window_pnl = true
supersedes = CLANKER_ALPHA_HYPOTHESES_V01.md
max_entry_wait_blocks = 5
primary_entry_delay_blocks = 3
primary_exit_minutes = 30
primary_notional = 250_USD_OR_EQUIV
split = time_ordered_60_discovery_40_holdout
holdout_eval = ONCE
```

v0.1 hypotheses remain historically frozen but are **economically void** under the v0.1 errata. v0.2 re-states testable claims with corrected execution semantics.

## Hard execution rules (all hypotheses)

| Rule | Value |
|---|---|
| Intended entry | `launch_block + 3` |
| Max quote wait | **5 blocks** after intended entry |
| Actual entry | block of accepted entry quote / qualifying swap print |
| Reject if | `actual_entry > intended_exit` OR entry quote after max wait |
| NO_TRADE_NO_QUOTE PnL | `0` (not gas loss) |
| Exit quote | must be at block `>= actual_entry` |
| Buyer feature name | `unique_transaction_actors` unless beneficiary recoverable |

## H1 — Early breadth vs concentration (actors)

**Claim:** Launches with higher pre-entry `unique_transaction_actors` (tx.from of buy-side Swaps) and lower top-actor paired-notional share have higher conditional-on-executable net returns than the executable baseline.

Kill if holdout conditional expectancy ≤ 0 under base cost, or cohort empty, or driven only by top-1/top-3.

## H2 — Repeat non-infrastructure creator

**Claim:** Launches whose `tokenAdmin` is classified as `USER_OR_UNKNOWN_EOA` / non-infrastructure and has ≥1 prior verified v4 launch before this block outperform first-time such admins on conditional executable PnL.

Kill if admin is router/relayer/factory-helper/infrastructure, or prior history incomplete, or holdout expectancy ≤ 0.

## H3 — Early net-buy momentum without extreme print

**Claim:** Among executable entries, launches with positive net paired buy imbalance over launch..launch+5 blocks, without extreme single-tx share above discovery percentile, have positive 30m conditional expectancy after base costs.

Kill if features use post-cutoff data, or holdout expectancy ≤ 0.

## Confirmation gate

```text
EDGE_NOT_SUPPORTED requires confirmation_holdout_executable_entries >= 30
else INSUFFICIENT_MARKET_DATA
```
