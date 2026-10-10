# Round C1b — verdict

Preregistration: `GOAL.md`. Outcome column `net_strict_bps`. Seed 20260819. Zero live.

## Primary result

Preregistered cell (predictor FV, L=168h, top quartile, T=7, pooled over c):

| pricing | n | blocks | mean | 95% CI | verdict |
|---|---|---|---|---|---|
| stored fee field | 4642 | 47 | −37.80 | [−64.59, −13.53] | NO_CONDITIONAL_MM_EDGE |
| true on-chain fee | 4642 | 47 | −55.91 | [−81.52, −32.79] | NO_CONDITIONAL_MM_EDGE |

The conditioning mechanism worked as designed: quiet fraction rose from 29.3% (baseline)
to 52.1% (selected). It was not enough. Break-even needed 62% at T=7. Powered null.

## The instrument error this round found

`s06_onchain_fee_audit.py` read `fee()` from all 411 panel pools on-chain, at head and at
the panel's first block:

- univ3: 0/209 pools disagree with the stored fee field. UniV3 fees are immutable, so
  this is the control, and it passes.
- aero_cl: 16/175 disagree at head, and 25/175 changed fee during the panel year.

`s07_fee_curve.py` then recovered the per-day fee by bisection (10,742 eth_calls instead
of the 73,566 a dense daily scan needs). The result is not a step function. **Aerodrome
Slipstream fees are recomputed continuously**, so a single static field per pool is the
wrong shape of instrument, not merely a stale value.

Example, `0x70acdf…` (cbBTC, stored field 2500):

```
2025-08 .. 2026-02   ~300–500   (3–5 bps)
2026-03 .. 2026-04   ~200–350
2026-04 .. 2026-06    50–175   (0.5–1.75 bps)
2026-06-18 onward      2500    (25 bps)  <- only the final 60 of 366 days
```

Volume-weighted true rate is 27.1% of the stored field.

## What that invalidated

`0x70acdf…` was the only positive cell in the round, and in round 40 before it:

| | mean net | median net |
|---|---|---|
| stored fee | **+242.8** | +456.7 (top FV decile) |
| true fee | **−48.4** | −44.3 |

Corrected, all 13 cbBTC pools sit in −39 to −83. There is no cbBTC outlier and there
never was one. Consequences:

- Round 40's positive result is void, same cell, same cause.
- **Round C3 must not be run as designed**; its entire premise is this cell.
- `s05_fee_bound_cbbtc.py` passed (0/375 violations, ratio/share ≈ 0.81) and could never
  have caught this: it used `fee=2500` in both numerator and denominator, so it is blind
  to a global fee-scale error. Bound checks cannot substitute for reading the instrument.

## What it did not invalidate

Aggregate net barely moves, which is itself the evidence that the correction is sane:

| cell | stored | true | delta |
|---|---|---|---|
| all positions (183,925) | −69.14 | −69.86 | −0.72 |
| T=1 | −72.95 | −73.60 | −0.65 |
| T=7 | −47.00 | −48.15 | −1.15 |
| T=30 | −63.81 | −64.55 | −0.74 |

Errors run both ways (corr p05 = 0.165, p95 = 7.50) and cancel across pools. **Every
prior kill stands and gets slightly more negative.** This bug only ever manufactured
false positives; it never hid a real one.

## Sweep for any survivor, corrected

446 pool × T cells, day-block bootstrap:

- 8 cells with lower bound > 0. Sign-flip null median is **16**; p = 0.996. The count of
  positive cells is *below* chance.
- Best cell (POKT aero_cl T=7, lo = 125.2) against the null distribution of the max
  lower bound: **p = 0.0715**. Does not clear multiplicity.
- 6 cells pass half-year stability and top-3-day < 50%, but they are 4 symbols (POKT,
  doginme, TOSHI, PRIME) and appear as venue pairs on the same symbol. Two pools on one
  token share the price path, so the pairing is not independent confirmation.

The ex-ante version of that selection — "high fee tier pays at T=7", which unlike a
symbol list could actually have been written down on 2025-08-17 — is dead:

| tier, T=7 | n | syms | mean | 95% CI | ex-survivor mean |
|---|---|---|---|---|---|
| 10–25 bp | 897 | 8 | +12.2 | [−32.2, 53.2] | **−49.1** |
| 25–50 bp | 9680 | 76 | −62.2 | [−96.6, −32.2] | −73.2 |
| ≥50 bp | 1096 | 17 | −31.7 | [−141.8, 80.3] | −31.7 |

26/92 symbols positive in the ≥25bp tier; median symbol mean −52.4.

## Family-level kill

The reason is mechanical, not statistical. Across fee-tier buckets at T=7:

```
tier        fee_bps    il_bps   cost_bps    net
1–5 bp         61.3     122.2       65.2   −98.5
5–10 bp       116.5     146.8       67.0   −97.3
25–50 bp      285.5     247.9       92.5   −62.2
≥50 bp        496.4     392.8      135.4   −31.7
```

**Fee revenue and impermanent loss scale together**, because a pool carries a high fee
tier precisely when its token is volatile. Cost rises on top. There is no tier and no
horizon (T=1/7/30 all tested) where fee revenue outruns IL plus cost.

This is a kill on the C family, not on one hypothesis. Passive symmetric market making
on Base does not pay, and no further variation of tier, horizon, width, or conditioning
signal changes the sign. Retire the family; do not spend rounds on C4+.

Emissions remain the one untested revenue term and are the only thing that could move
this. That is the concurrent round, and it must be recomputed against the corrected fee
curve in `out/fee_curve.json`, not the static field.

## Artifacts

```
src/s06_onchain_fee_audit.py   on-chain fee() vs stored field, 411 pools
src/s07_fee_curve.py           per-day true fee by bisection, 10,742 calls
src/s08_fee_damage.py          volume-weighted correction, damage report
src/s09_rerun_corrected.py     C1b re-run + 446-cell sweep
src/s10_survivor_kill.py       multiplicity, sign-flip null, stability
src/s11_fee_tier_exante.py     ex-ante fee-tier cross-section
out/fee_curve.json             reusable: true daily fee, 201 non-univ3 pools
out/pos_fee_corrected.json     positions with corrected fee and net
```
