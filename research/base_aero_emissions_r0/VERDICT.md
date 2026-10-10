# Round 45 — emissions overlay + epoch calendar

Not a preregistered C-family PASS/FAIL. These are the two remaining "maybe C still
pays" and "maybe Thursday is special" claims, killed at the cheapest layer.

## C + AERO emissions

Whose money: AERO holders (inflation), not swap counterparties. Overlay assumes the
LP NFT is staked for free — optimistic for a profit claim.

Unconditional aero_cl book, fee-corrected `net_strict`:

| T | n | net | em median | net+em | p95-capped net+em |
|---|---|---|---|---|---|
| 1 | 60984 | −81.9 | +1.4 | −79.2 | −79.8 |
| 7 | 9486 | −72.3 | +10.5 | −52.8 | −56.4 |
| 30 | 2320 | −123.3 | +47.3 | −14.9 | −57.1 |

High-confidence stake denominator does not help (T=7 net+em −23.3). The hole is
50–70 bps/week; the typical subsidy is 7–10 bps/week.

Ex-ante rule "top quartile of previous-epoch emission yield, T=7":

| | mean | 95% day-block CI |
|---|---|---|
| selected, no emissions | −44.1 | [−133.1, +31.9] |
| selected, +emissions | **+4.7** | **[−84.3, +79.5]** |
| h1 / h2 | +13.5 / −4.7 | both cross zero |
| drop PING | −5.9 | [−96.6, +71.7] |

PING carries 223% of selected net+em; dropping any of PING/CLANKER/GAME/noice/POKT
flips the mean negative. Fee 456 vs rest 211, IL 399 vs 193, emissions 49 vs 12:
**the same C4 mechanism.** Subsidy does not outrun the extra IL.

Verdict: `EMISSIONS_DO_NOT_SAVE_LP`. Retire C+emissions. Do not open C5.

## D1 calendar

Thursday 00:00 UTC is epoch open. Panel-wide volume is not Thursday-specific
(9.29M WETH vs Monday 9.74M). Weekend drop is a weekend. Deepest AERO CL pool
Thursday mean +4 bps, median indistinguishable from other days; both halves same
sign, magnitude below round-trip cost. The all-AERO Wednesday −80 bps is two
outlier hours on a thin pool (`0x4e5066…`), deepest pool Wednesday −2.1.

Verdict: `NO_TRADEABLE_EPOCH_CALENDAR`. Do not reopen on a finer clock.

## What this does not kill

Bribes paid to veAERO voters (Q3 in `HYPOTHESIS_QUEUE.md`) are a different
counterparty — protocols buying votes, not LPs harvesting inflation. That is the
next measurement.
