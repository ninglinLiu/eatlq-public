# A5 / A3 re-run at the account's real trade size — pre-registration

Written before any $50 result was computed. Round 38 ran these families at a
$5,000 notional and both came back UNDERPOWERED. That notional was wrong: the
account's real per-trade size is about $50. This document fixes what may and may
not change in the re-run, so that the re-run is a test and not a search.

## 1. Why the size makes this a different question, not a re-roll

Execution cost has two parts that behave oppositely under size:

- slippage scales with size — a $50 trade moves the pool 1/100 as far as $5,000
- the fee tier does not scale at all — it is a rate
- gas does not scale either, and it becomes *worse* as size falls

Round 38's finding was specific: the A5 signal was real (+8 to +24 bps
conditional edge, several cells with lower bounds above zero on the raw edge) but
was buried under a 67–95 bps round-trip toll. The toll was almost entirely
slippage. At $50 the measured toll on the same pools is a few bps of fee tier
plus gas, so this re-run asks a question round 38 could not answer: **is the
surviving edge larger than the toll that this account actually pays?**

The same logic applies to A3 and it is re-run unchanged alongside, because they
share the harness and because A3's negative edges are the control that tells us
whether a positive A5 result is a flow effect or a generic small-cap artefact.

## 2. What is allowed to change

Exactly three things:

1. **Panel** — 366 days (2025-08-17 → 2026-08-17) instead of the 3-month window.
2. **Cost** — point-in-time per (pool, date) from `census.pit_cost_50` sized at
   $50, requiring `feasible = 1` on the entry date, instead of one static
   per-symbol number sized at $5,000.
3. **Gas** — charged explicitly, which round 38 did not do because at $5,000 it
   was 0.04 bps. Measured from 286 real single-swap Base receipts: mean
   $0.0359 per swap, so **14.4 bps per round trip at $50**. The mean is used
   rather than the median ($0.0038, 1.5 bps) because one 10 gwei congestion
   spike holds 47% of the sampled spend, and a strategy that trades continuously
   should be charged for spikes. This is the conservative choice by a factor of
   about 9.

## 3. What is not allowed to change

- **Signal definitions** — `sig_a5` = net WETH flow / gross WETH flow over W
  hours, requiring ≥5 swaps in the window; `sig_a3` = L-hour log return scaled by
  sigma_168·√L. Unchanged from GOAL.md.
- **Grids** — A5: W ∈ {1, 4, 24} × q ∈ {0.3, 0.6} × h ∈ {4, 24}. A3: L ∈ {4, 24,
  168} × k ∈ {1.5, 2.5} × h ∈ {4, 24}. Twelve cells per family, no additions.
- **Statistic** — net per-trade edge = conditional return − the same symbol's
  unconditional return over the same split and horizon − round-trip cost − gas.
- **Inference** — day-blocked bootstrap, 10,000 resamples, Bonferroni α = 0.05/12.
- **Trade construction** — long-only, non-overlapping, enter at the open of the
  bar after the signal, hold h hours.
- **One pool per symbol**, the cheapest, so that correlated pools on the same
  token do not enter as independent evidence.

## 4. Split

IS = 2025-08-17 → 2026-05-17 (9 months). OOS = 2026-05-17 → 2026-08-17 (3
months). Declared here before running. A cell counts only if it clears zero in
IS **and** keeps a positive point estimate in OOS.

## 5. Verdicts, fixed now

- `FLOW_EDGE_PAYS_AT_50` — at least one A5 cell has a net lower bound above zero
  in IS and a positive OOS point estimate, **and** it survives the two audits in
  section 6.
- `FLOW_EDGE_REAL_BUT_STILL_SHORT` — the raw edge clears zero but the net does
  not. This is round 38's outcome and would mean the size change was not enough.
- `UNDERPOWERED` — CIs still span zero and the width exceeds the point estimate.
- `FALSIFIED` — point estimates negative across the grid.

## 6. Mandatory audits before any positive claim

Both were forced by round 40, where a positive result turned out to be one asset.

1. **Concentration guard** — recompute the winning cell with the single
   largest-contributing symbol removed. If the result flips sign or loses
   significance, it is a single-asset finding and must be reported as such, not
   as a strategy.
2. **Capacity honesty** — confirm the position is small against *instantaneous*
   pool liquidity at entry, not against a trailing median. Round 40's positive
   result came from positions that passed a trailing-median gate while being
   large against the liquidity actually present.

Additionally, because gas is now a first-order term, results are reported at both
the conservative mean-gas charge and the 99%-trimmed charge, and a cell that only
works under trimmed gas is reported as gas-regime-dependent rather than as a win.
