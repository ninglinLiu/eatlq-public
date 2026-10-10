---
sop_applied: true
alpha_id: AR-GL2-C-EXOGENOUS-WEATHER-OOS
family: F6/F7
next_gate: HISTORICAL_TRAIN_DERIVE_THEN_FROZEN_OOS_REPLAY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-C preregistration — exogenous weather basket, train-derived thresholds, frozen OOS

Frozen on 2026-07-15 before opening or computing the GL2-C KEYSTONE return rows. This is a read-only historical falsification of GL1-18. It neither trains a model nor authorizes a signal, order, signature, broadcast, Redis write, executor start, promotion, or GO decision. Final evidence grading belongs to the review agent.

## 1. Population, clock and outcomes

- Chain and quote unit: Base, WETH. Every return must reuse the GL1 seconds-execution evidence hierarchy and 0.001 WETH convention; no USD conversion.
- Calendar envelope: `2026-02-27T05:41:15Z` through `2026-06-08T18:29:40Z`.
- Training: signal timestamp strictly before `2026-04-15T00:00:00Z`.
- OOS: signal timestamp at or after `2026-04-15T00:00:00Z` and no later than the envelope end. OOS is never used to choose a metric, direction, threshold, or missing policy.
- Decision time for an opportunity at UTC day D: `D 00:00:00Z`. Every weather observation must have timestamp `< D 00:00:00Z`; no current-day close, current-day final count, or future transaction is allowed.
- Primary population: the GL1 KEYSTONE-selected subset, with the exact GL1 selection flag and no weather input in selection.
- Secondary population: all 431 same-age candidates in the GL1 universe. This is reported separately and never substituted for the KEYSTONE population.
- Frozen exit fronts: entry `+1`, exits `+1/+5/+15`; use real subsequent sell first, otherwise explicitly labelled guarded atomic quote; confirmed five-evidence rug is `-100%`; unresolved stays unresolved and is excluded from a finite-return mean with numerator/denominator reported. Missing is never silently changed to full loss or success.
- Primary endpoint: OOS KEYSTONE mean at `entry +1 / exit +1`.
- Stability endpoints: the same OOS population at exits `+5` and `+15`, plus the two fixed OOS calendar subwindows below. Full-universe EV is secondary.

## 2. Frozen exogenous indicator basket

Exactly these five single-indicator gates are tested. No composite, interaction, KEYSTONE score, W1/W2/W3 label, pool/token/address/hash, or outcome-derived extra indicator may be added after this file is hashed.

1. `pool_creation_rate_7d`: mean daily count in `[D-7d,D)` of WETH-legged pools emitted by the four approved Base factories already frozen by the B6 repair census. Both high and low directions are candidate gates.
2. `aggregate_weth_net_inflow_7d`: sum of signed WETH pool deltas from all locally indexed Base WETH-pool swaps in `[D-7d,D)`, divided by seven. Positive means WETH entered pools (net demand for the non-WETH leg). Use decoded integer WETH amount with token0/token1 orientation; never use target-token units or price proxies. Both directions are candidates. If the local indexed swap source does not cover the whole calendar envelope with an auditable timestamp and WETH-leg mapping, this indicator is `UNAVAILABLE_NOT_TESTED`, not imputed from KEYSTONE trades.
3. `eth_realized_volatility_7d`: population standard deviation of the seven lagged close-to-close log returns ending at D-1, using closes D-8 through D-1. Current-day close is forbidden. Both high and low directions are candidates.
4. `base_gas_regime_7d`: median daily Base block gas-fill ratio (`gasUsed/gasLimit`) over `[D-7d,D)`, from historical block headers or an already-hashed local block-header census. Both directions are candidates. A sparse proxy, present-day gas, or receipt gas from the candidate trade is forbidden. If full lagged coverage cannot be reproduced read-only, mark `UNAVAILABLE_NOT_TESTED`.
5. `whole_universe_rug_rate_7d`: among the independent same-age full-universe candidates whose observation/cutoff is completely before D, the share adjudicated as confirmed non-recovering rug by the GL1 five-evidence rule. Rows still unresolved at D are not rugs and stay out of numerator and denominator. Require at least 10 matured candidates in `[D-7d,D)`; otherwise missing. This indicator may use past full-universe outcomes but never the focal opportunity or anything settled at/after D. Both directions are candidates.

All indicator values are keyed only by UTC decision day. A missing value closes that indicator's gate for that day. Availability and excluded days must be reported per indicator.

## 3. Frozen threshold derivation on training only

For each available indicator independently:

1. Compute the indicator distribution across training-eligible UTC days only.
2. Candidate numerical thresholds are training empirical quantiles `q={0.20,0.35,0.50,0.65,0.80}` using deterministic linear interpolation.
3. Candidate directions are `value <= threshold` and `value >= threshold`.
4. Reject a candidate unless its training open-day share is between 20% and 60% inclusive, it opens at least five training days, and it contains at least 10 finite KEYSTONE `+1/+1` outcomes.
5. Score each surviving candidate by the minimum of its training KEYSTONE mean returns across the three frozen exits `+1/+5/+15`. A window with zero finite outcomes scores `-infinity`.
6. Select the largest score. Deterministic ties: more finite `+1/+1` observations, then smaller open-day share, then lower quantile, then direction `low` before `high`.
7. The chosen direction and numerical threshold are frozen before any OOS return is read. If no candidate survives, the indicator is `TRAIN_UNPOWERED` and is not tested OOS.

Threshold search is intentionally tiny and declared. Training performance is descriptive only; it is not OOS evidence.

## 4. Frozen OOS random comparator

- RNG seed: `20260715`.
- Draws: `10,000` per indicator.
- For an indicator, take its OOS eligible UTC days and the exact number of days its frozen gate opens. Each draw samples that many eligible days without replacement. One sampled day set is applied to all three exit fronts and both populations.
- Thus each placebo preserves calendar eligibility and exact work/open-day share. It does not preserve outcome counts; those are reported.
- For each endpoint report the true-gate mean, random mean distribution median/p95, true-minus-random-median, and whether true is strictly greater than random p95. Undefined/zero-exposure never counts as a win.

## 5. Frozen OOS stability rule

Fixed OOS subwindows, end exclusive:

- `O1 = [2026-04-15T00:00:00Z, 2026-05-15T00:00:00Z)`
- `O2 = [2026-05-15T00:00:00Z, 2026-06-09T00:00:00Z)` intersected with the calendar envelope.

A preregistered indicator is labelled `OOS_STABLE_WIN` by the machine script only when all are true:

1. OOS opens at least five eligible days and has at least eight finite KEYSTONE `+1/+1` outcomes.
2. KEYSTONE true mean is strictly above same-share random p95 at `+1/+1`.
3. At least two of the three exits `+1/+5/+15` are strictly above their same-share random p95, and all three true-minus-random-medians are positive.
4. Each fixed subwindow with at least four finite KEYSTONE `+1/+1` outcomes has true mean above its matched random median; at least two subwindows must be powered. Empty or underpowered windows remain visible and cannot satisfy this clause.
5. Removing the largest positive 1% of OOS KEYSTONE `+1/+1` returns (at least one row when non-empty) leaves a positive finite mean.

Otherwise the script emits `NO_STABLE_OOS_WIN`, `OOS_UNPOWERED`, or `UNAVAILABLE_NOT_TESTED` with reasons. The script does not assign GL1 grade and does not decide GO.

## 6. Required outputs and audit constraints

- Report every metric, selected training threshold, eligibility/open share, all three exit distributions, O1/O2 rows, full-universe secondary rows, random comparator, top-1%-removed result, and missing/unresolved counts. Do not report a single pooled mean without these distributions.
- Machine output: `mvp/diagnostics/base_gl2_c_weather_oos_20260715.json`.
- Human report: `reports/GL2_C_exogenous_weather_oos_result_20260715.md`.
- Script: `scripts/research/base_gl2/gl2_c_weather_oos.py`.
- Regression: `tests/test_base_gl2_c.py` with exact assertions.
- Hash ledger must include this preregistration and its standalone SHA256, all input artifacts, script, machine output, report, and test evidence.
- Pure read-only recheck. RPC, if needed only for missing historical gas headers, must use the authorized `prettiest-blue-aura` endpoint without revealing its URL and only `eth_getBlockByNumber`/`eth_blockNumber`; otherwise zero RPC. No `eth_send*`, signing, broadcasting, executor, live/signal/Redis/config mutation, purchase, or fabricated external result.
- No single unanchored source may be promoted to IRON. Final line-by-line grade is reserved for the review agent.
