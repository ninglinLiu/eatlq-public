# Round 41 — the trade size was wrong by 100x, and fixing it killed the remaining leads

## What prompted this

Every round from 35 to 40 sized trades at a notional $5,000. The account's real
per-trade size is about $50, and its total capital is under $5,000. The original
greenfield report (`base_wildcat_alpha/FINAL_WILDCAT_REPORT.md`, section F) had it
right — "fit for an approximately $1,000 account", capacity stated as "$1,000
UNVERIFIED". The $5,000 figure entered later, in the cost-panel work, and was
never questioned again.

This matters because the two components of execution cost behave oppositely
under size:

| component | scales with size? | at $5,000 | at $50 |
|---|---|---|---|
| slippage | yes, linearly | dominates | negligible |
| fee tier | no, it is a rate | small | dominates |
| gas | no, it is a fixed fee | 0.14 bps | 14.4 bps |

So the correction is not a rescaling of prior results. It changes their signs in
both directions.

## What was measured, not assumed

**Gas.** 286 `eth_getTransactionReceipt` calls on real Base transactions that
carried exactly one swap log, 22 per month across 13 months, reading `gasUsed`,
`effectiveGasPrice` and `l1Fee`. A swap costs a median of **$0.0038** and a mean
of **$0.0359**; the L1 data fee is 0.6% of the total. One 10 gwei congestion spike
on 2025-10-26 holds 46.9% of the entire sampled spend, so the mean is unstable but
conservative, and it is what is charged everywhere below: **14.4 bps per round
trip at $50** against 1.5 bps for the median regime. A mint plus burn plus two
inventory swaps is 1.62M gas, **52 bps per position cycle at $50**.

The worry that gas would make small size unviable was unfounded for swapping and
correct for market making.

**Cost.** `census.pit_cost_50` rebuilt at $50 over the same 3,029-pool universe:

| | $5,000 | $50 |
|---|---:|---:|
| median round-trip cost | 5,924 bps | **143 bps** |
| inventory feasibility pass rate | 11.8% | **62.5%** |
| pools cheap + feasible ≥300 days | 17 | **64** |
| pools cheap + feasible ≥90 days | 43 | **138** |
| hourly bars in reach | 656,945 | **2,071,182** |
| usable flow hours | 149,770 | **1,414,253** |

Cross-checked against the 2,524 atomic round trips already measured at four sizes:
at the $50 bucket the median one-way cost is 105 bps against 2,072 bps at $5,000,
and 47 pools cost under 10 bps one-way against 19.

## The finding that reframes six rounds

Round 38 found an anti-correlation between edge and tradability and attributed it
to depth. It is not depth. At $50 slippage is negligible so **cost is the fee
tier**, and fee tiers are chosen to match the volatility they must compensate
liquidity providers for. The ratio that decides whether a trade can win is
therefore nearly flat across the whole universe:

| fee tier (bps) | toll / hourly sigma at $50 | at $5,000 |
|---:|---:|---:|
| 0.8 | 0.32 | 0.20 |
| 1 | 0.55 | 0.05 |
| 5 | 0.53 | 0.86 |
| 15 | 0.68 | 1.37 |
| 25 | 0.67 | 1.99 |
| 30 | 1.01 | 1.32 |
| 70 | 1.23 | 2.49 |
| 95 | 0.75 | 1.39 |

A 120x range of fee tiers, and the ratio barely moves. Two consequences:

1. **The unwinnable regime is gone.** At $5,000 every tier at or above 15 bps had
   a toll exceeding a full standard deviation of the hourly move. No skill wins
   that. At $50 the worst is 1.23.
2. **There is a floor near 0.5 that pool selection cannot cross**, because
   choosing a cheaper tier also chooses a quieter asset. Trading smaller cannot
   escape a rate.

The only remaining lever is holding period, because sigma grows as √t while the
toll is fixed. Toll as a fraction of the move: 0.42 at 4 hours, 0.17 at a day,
0.065 at a week, 0.031 at 30 days.

## What doubling requires

Annual return = (trades per capital slot per year) × (net edge per trade), so
capital and trade size cancel. Requirements, in units of one standard deviation of
the move being predicted:

| hold | trades/slot/yr | net bps to double | gross needed, median pool | best decile | toll paid per year |
|---|---:|---:|---:|---:|---:|
| 4 hours | 2,190 | 4.6 | 0.446 | 0.189 | 1,713% |
| 24 hours | 365 | 27.4 | 0.232 | 0.127 | 285% |
| **3 days** | **122** | **82.2** | **0.204** | **0.143** | **95%** |
| 1 week | 52 | 191.8 | 0.225 | 0.185 | 41% |
| 30 days | 12 | 821.9 | 0.362 | 0.343 | 10% |

An interior optimum at about three days: predict 20.4% of a sigma per trade on a
median pool, or 14.3% on the cheapest decile. That is a **net Sharpe of 1.15**,
which is an ordinary target for a real edge. **Doubling is not ruled out by the
account size, the cost structure or the venue.**

## Verdicts

Pre-registrations written before the corresponding numbers existed:
`base_predict_r0/GOAL_R1_SIZE50.md`, `base_deep_edge_r0/GOAL_R1.md`.

| family | tested on | cells clearing zero | verdict |
|---|---|---:|---|
| A5 order flow imbalance | 366 d, $50, 179 symbols | 0 of 12 | **FALSIFIED** |
| A3 multi-bar momentum | 366 d, $50, 179 symbols | 0 of 12 | **FALSIFIED, wrong sign** |
| A2 multi-day mean reversion | 366 d, $50, 270 symbols | 0 of 12 | UNDERPOWERED |
| C1 plain market making | 366 d, $50, 842 pools | 0 of 9 | **CONFIRMED LOSS** |
| C2 fee tier selection | 366 d, $50, 842 pools | 8 of 9 | REAL BUT UNBANKABLE |

**A5 is the important one.** Round 38 measured +8 to +24 bps on a 3-month panel
and called it "real but smaller than the toll". On 366 days and 9.4x more flow
hours, at a size where the toll is a third of what it was, not one of the twelve
pre-registered cells clears zero. The round 38 result was a short-panel artefact.

**A3 is the strongest result in the project and it has the wrong sign.** Three
cells are significantly negative in both splits after Bonferroni correction:

| rule | IS net, 95% CI | n | OOS net, 95% CI | n |
|---|---:|---:|---:|---:|
| buy after 1.5σ 4h up-move, hold 4h | −523 to −110 | 22,127 | −1,500 to −125 | 6,915 |
| buy after 2.5σ 24h up-move, hold 4h | −1,594 to −143 | 4,773 | −237 to −62 | 1,519 |
| buy after strong 1h buy-flow, hold 4h | −181 to −28 | 54,796 | −488 to −74 | 15,605 |

Chasing strength on Base micro-caps loses reliably and out of sample. This
retires **F03 directional flow persistence**, the single candidate the original
48-hypothesis greenfield search promoted, with its sign reversed. The profitable
side is selling into strength, which a long-only on-chain account cannot reach
directly.

**C1 is now a confirmed loss rather than an unproven one.** At $50 the position is
always a negligible share of the pool, which removes the capacity-gate flaw that
invalidated round 40's positive. With it removed, every cell's whole interval sits
below zero — including the 30-day cells, where round 40 could only say
"underpowered".

## Why market making loses, decomposed

Splitting C1 positions by the direction the price actually moved (net bps vs
holding the tokens, pooled over range widths):

| hold | price rose >2% | barely moved | price fell >2% |
|---|---:|---:|---:|
| 1 day | −164.1 | −42.7 | −161.4 |
| 7 days | −109.1 | **+80.8** | −164.6 |
| 30 days | −130.0 | **+214.4** | −212.4 |

Market making is a short-volatility position. It wins only in the quiet regime,
and the quiet regime is not frequent enough to pay for the other two. The
asymmetry between up and down (+55 bps at 7 days, +83 at 30) is consistent with
the A3 finding — selling into strength is the better half — but it is second
order next to the quiet/moved split.

## Where this leaves the search

The diagnosis changed; the prognosis did not. For six rounds the story was "there
may be an edge but costs eat it". That story rested on a trade size 100x too
large. At the real size the costs are survivable and the edges are absent. The
bottleneck has moved from cost to signal, and no further cost work will help.

The evidence now converges on one already-pre-registered and never-run hypothesis:
**C3, market making on slow cross-rates** (`base_fee_tier_r0/GOAL_R3_CBBTC.md`).
If liquidity provision only pays in the quiet regime, then select assets that are
structurally quiet against WETH — a wrapped major against WETH is a cross-rate
between two things that move together — rather than trying to time the regime. It
has two independent supports: round 40's discarded positive was entirely cbBTC, a
wrapped major, and the quiet-regime column above is the only positive number in
the whole market-making table. cbBTC is held out of the confirmation set.

## Method debts incurred

1. **The glitch filter does not transfer to the expanded universe.** `MAX_ABS_LR
   = 2.3` was pre-registered on the deep universe, where 108 of 656,852 bars
   exceeded it. On the $50 universe 223,044 of 2,071,182 bars do — 10.8%. In a
   micro-cap pool a 10x hourly move can be real, so neutralising them may be
   discarding signal rather than noise. The A2 verdict at $50 is contaminated by
   this and should not be read as a clean measurement.
2. **The bootstrap had to be rewritten to run at all.** Resampling day blocks and
   re-averaging the flat trade list is O(BOOT × trades); on a 366-day panel with
   179 symbols it does not finish. Day-level sums and counts give an identical
   estimator in O(BOOT × days).
3. **The `$5,000` string is still in output headers** of `stage3.py` even though
   the notional is now an argument. Cosmetic, but it is exactly the class of stale
   label that produced this whole round.
