# Q4 — paper short of A3

A3 long (buy after z_L > k) is a confirmed loss. The money, if any, is on the
short. This file is the cheapest kill of that claim: reuse `a5_year_50.json`,
do not build Morpho.

## Arithmetic

From `run_a5_year.py`: `long_net = edge − hurdle` with `edge = cond − uncond`.
A short on the same trigger pays the round-trip instead of receiving it:

```
short_net = −edge − hurdle − borrow
```

`−long_net` is a trap: it credits you the 110 bps hurdle. 50% APR borrow is
2.3 bps / 4h — not the binding term.

## Result

24 split-cells (12 pre-registered × IS/OOS). **0 with short lower bound > 0.**

Headline cell (4h 1.5σ up, hold 4h), the one RESEARCH_PLAN quotes:

| split | n | edge [lo, hi] | hurdle | long net | paper short |
|---|---|---|---|---|---|
| IS | 22127 | −142 [−385, **+6**] | 116.5 | −259 [−523, −110] | **+23 [−125, +267]** |
| OOS | 6919 | −568 [−1349, −20] | 107.4 | −676 [−1500, −126] | **+459 [−90, +1240]** |

OOS point estimate of the short looks large because the long was a disaster.
The lower bound of −edge is 20 bps; the hurdle is 107 bps. CI still crosses zero.

Two OOS cells have the short *entirely* below zero (`24_2.5_4`, `168_1.5_4`):
the long lost to costs, not to a large negative edge, so reversing still loses.

## Inventory

Morpho Blue on Base (recon sample) lets you borrow **USDC and WETH**. A3's 179
names are micro-caps (top contributors YGG / ATTN / AUDD). The names that make
1.5σ 4-hour moves are the names you cannot borrow. The names you can borrow do
not make those moves.

## Verdict

`SHORT_SIDE_DOES_NOT_CLEAR`. Do not build the lending stack to harvest A3.
A4 (cross-sectional long-short) is the same inventory constraint and stays blocked.
