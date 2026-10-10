# PnL Decomposition (Phase 2)

Generated: `2026-07-21T07:22:48Z`

## POOL_ONLY = 池子结构基线

POOL_ONLY = buy opportunities that pass only pool-structure filters (age 15m–24h, min size, depth/momentum/intensity score quantile), WITHOUT requiring a 'large buy' or 'absorbed' flag. Same $1000 wallet, 3 concurrent slots, one position per token, TP ladder exits.

CAPACITY_MATCHED_RANDOM picks the same count of eligible buys as POOL_ONLY from the same spine. If the regime itself has positive expected USD after costs, random can withdraw ~$500–$600 without any sieve edge.

## REGIME

- POOL_ONLY withdrawn: `$404.78`
- Random withdrawn: `$852.03`
- Verdict: `PROVEN`

## SELECTION

- LBA vs POOL paired Δ WETH: `0.1118` (p05 day `-0.1282`, pass `False`)
- LBA vs Random paired Δ WETH: `-0.0373` (p05 day `-0.2177`)
- LBA vs POOL: `UNCONFIRMED`
- LBA vs Random: `REFUTED`

## EXECUTION

- Skip rate: `99.35%`
- Unconst diag WETH: `65.8853` vs scheduled `0.2467`
