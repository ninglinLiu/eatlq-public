# Clanker Alpha Hypotheses v0.1 (Pre-Registered)

```text
status = PRE_REGISTERED
frozen_before_pnl = true
max_hypotheses = 3
split_rule = time_ordered_60pct_discovery_40pct_holdout
```

These hypotheses are frozen before any net-PnL labeling or cohort threshold search.
After freeze, thresholds may be chosen only on the discovery split; holdout is evaluated once.

## Common execution assumptions (all hypotheses)

| Field | Value |
|---|---|
| Universe | Verified Clanker v4 factory launches on Base (`chain_id=8453`) only |
| Signal cutoff | End of entry-delay block / timestamp (hypothesis-specific) |
| Earliest entry | After signal cutoff; never same log as launch without delay |
| Exit horizons evaluated | 5m / 30m / 2h / 24h (primary report: 30m) |
| Entry delays evaluated | 1 / 3 / 5 blocks and 30s / 60s (primary report: 3 blocks) |
| Notional sizes | 100 / 250 / 500 USD (primary: 250 USD) |
| Cost scenarios | optimistic / base / stressed (primary verdict uses **base**) |
| Features allowed | On-chain only, available at or before signal cutoff |
| Leakage ban | No future ATH, future volume, ex-post fame, post-exit holders |

## H1 — Early buyer breadth vs concentration

**Claim:** Launches with higher unique buyer count and lower single-address buy concentration in the pre-entry window have higher risk-adjusted net returns than the unconditional launch set.

| Item | Definition |
|---|---|
| Signal cutoff | End of the block immediately before earliest entry (for block-delay entries); or `launch_ts + delay_sec` for time-delay entries |
| Earliest entry | Primary: launch + 3 blocks |
| Exit | Primary: entry + 30 minutes |
| Expected direction | Selected cohort mean/median net PnL **>** buy-all baseline on holdout (base cost) |
| Features at cutoff | Unique buyers in `[launch_block, entry_block)`; max single-buyer ETH share; top-3 buy share; buy/sell count imbalance |
| Kill condition | Holdout base-cost expected net PnL ≤ 0, or median net return clearly negative, or edge driven only by top-1/top-3 winners |
| Anti-leakage | Buyer set and concentration use only swaps/transfers with `block_number < entry_block` (or `timestamp ≤ signal_cutoff` for time delays) |

## H2 — Repeat creator / deployer

**Claim:** Tokens whose `tokenAdmin` (or msgSender deployer) has ≥1 prior verified Clanker v4 launch in-sample before this launch earn higher subsequent net returns than first-time creators.

| Item | Definition |
|---|---|
| Signal cutoff | Launch inclusion time (prior-count uses only launches with `block_number < this.launch_block`) |
| Earliest entry | Primary: launch + 3 blocks |
| Exit | Primary: entry + 30 minutes |
| Expected direction | Repeat-creator cohort outperforms first-time cohort and beat-buy-all on holdout (base cost) |
| Features at cutoff | Prior launch count for admin/deployer; optional trailing prior-launch net return using only exits completed before this launch’s signal cutoff |
| Kill condition | Same as H1; also fail if “prior count” uses any launch at/after current block |
| Anti-leakage | Creator history is strictly causal in block order; no use of this launch’s future path |

## H3 — Early sustained net-buy momentum without extreme single print

**Claim:** Launches showing sustained net buy imbalance in the first N blocks after launch, without an extreme single-swap size spike, have positive short-horizon net expectancy after costs.

| Item | Definition |
|---|---|
| N | 5 blocks after launch (fixed pre-registration) |
| Signal cutoff | End of `launch_block + N` |
| Earliest entry | `launch_block + N + 1` (so all N blocks are observed) |
| Exit | Primary: entry + 30 minutes |
| Expected direction | Cohort net expectancy > 0 and > buy-all on holdout (base cost) |
| Features at cutoff | Net buy volume / gross volume over N blocks; unique buyers; max single swap / gross volume; whether max single swap exceeds pre-set discovery-only percentile threshold |
| Extreme print rule | Flag if max single buy notional / window gross buy notional > threshold chosen on discovery only |
| Kill condition | Same as H1; also fail if entry uses any post-cutoff swap |
| Anti-leakage | Momentum window ends at signal cutoff; entry is strictly after |

## Pre-registered evaluation protocol

1. Sort launches by `(block_number, log_index)`.
2. First 60% = discovery (explore thresholds / cohort cuts).
3. Last 40% = holdout (single evaluation; no retuning).
4. Compare: buy-all · no-trade · H1 · H2 · H3.
5. Report discovery and holdout separately with cost scenarios.
6. If holdout does not support a hypothesis under base cost, that hypothesis is rejected.
7. Study-level verdict rules follow the task gate (`EDGE_SUPPORTED` / `EDGE_NOT_SUPPORTED` / `INSUFFICIENT_MARKET_DATA`).

## Explicit non-claims

- Passing decoder/golden tests is not economic evidence.
- Optimistic-only PnL is not sufficient for `EDGE_SUPPORTED`.
- Social/Farcaster popularity is out of scope for v0.1.
