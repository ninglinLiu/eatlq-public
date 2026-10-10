# EXIT_ALPHA_v0_REVIEW

Generated: `2026-07-21T12:13:02Z`

## Framing

Entry is frozen capacity-constrained **random spray** (no stock-picking).
Question: can a path-causal exit beat `FIXED_TP_4H` on net-of-cost scheduled-wallet PnL?

## Live: **NO_GO** | Observer shadow proposed: **False**

## Baseline scheduled PnL (net-of-cost)

- TEST scheduled PnL: `-0.1537554085358491` WETH (fills=620)
- SEALED scheduled PnL: `-0.3192778667457569` WETH (fills=214)

## Per-exit verdicts (paired day Δ vs FIXED_TP_4H)

| Exit | Verdict | TEST ΣΔ | TEST day-p05 | SEALED ΣΔ | SEALED day-p05 | BH |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| FIXED_TP_4H | BASELINE | — | — | — | — | — |
| TIME_30M | **UNCONFIRMED** | 0.326388 | -0.035360 | 0.001313 | -0.281212 | False |
| TIME_4H | **UNCONFIRMED** | 1.497438 | 0.445336 | 0.000393 | -0.178695 | False |
| TRAIL_20_FROM_PEAK | **REFUTED** | -0.163514 | -0.669560 | 0.001232 | -0.248853 | False |
| MOMENTUM_HOLD | **REFUTED** | -0.163296 | -0.940219 | 0.000172 | -0.193172 | False |

## Success bar

net-of-real-cost scheduled incremental PnL of exit X over FIXED_TP_4H on same random entry priority, TEST day-p05>0, BH-corrected, replicated on sealed

## Stop rule

If TRAIL_20_FROM_PEAK and MOMENTUM_HOLD both fail vs FIXED_TP_4H under sealed+net-of-cost, seal exit-alpha line closed for this universe — do not keep adding exit variants.

Dynamic exits both REFUTED → line closed: **True**

## BH (TEST)

```json
{'method': 'benjamini_hochberg', 'alpha': 0.05, 'pvals': {'TIME_30M': 0.06999999999999995, 'TIME_4H': 0.015000000000000013, 'TRAIL_20_FROM_PEAK': 0.6975, 'MOMENTUM_HOLD': 0.63}, 'reject': {'TIME_30M': False, 'TIME_4H': False, 'TRAIL_20_FROM_PEAK': False, 'MOMENTUM_HOLD': False}}
```

## Disposition

No exit cleared PROVEN under TEST day-p05>0 + BH + SEALED replication on the same random-entry scheduled stream. `live = NO_GO`.

Stop rule fired: both `TRAIL_20_FROM_PEAK` and `MOMENTUM_HOLD` are REFUTED. Exit-alpha line closed for this universe — do not keep adding exit variants.
