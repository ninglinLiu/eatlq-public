# BASE_ALPHA_ATLAS — F01/F03 Signal Orthogonality (Structure Only)

UTC: 2026-07-18  
**No PnL comparison.**

## Inputs compared

Stage1A signal sets from local probe / tiny panel (see sanity report).

## Structural overlap

| Metric | Value |
|---|---|
| same_pool_hit Jaccard | **0.0** |
| same_pool_hour Jaccard | **0.0** |
| F01 pools | 11 |
| F03 pools | 2 |
| intersection pools | ∅ |
| hourly trigger-frequency correlation | −0.514 |

Negative hourly correlation on this tiny sample is **not** evidence of diversification alpha — samples are sparse and universes differ (swap probe vs bar panel pools).

## Primary features

| Strategy | Inputs |
|---|---|
| F01 | `trade_sign`, `run_length`, `run_notional_eth` (swap sequence) |
| F03 | `close_to_close_log_return`, `realized_vol_baseline`, `jump_ratio` (closed bars) |

No shared primary feature vector.

## Mechanism assessment

F01 and F03 are **different mechanisms** on this freeze:

1. **Grain**: swap-level runs vs closed-bar volatility jumps.
2. **Trigger**: consecutive same-sign flow vs `|r|/rv` threshold.
3. **Pool overlap on Stage1A outputs**: none.

Therefore Stage1A does **not** flag REPLACE_REQUIRED for mechanism duplication. Future full-panel runs may still show incidental co-firing when both fire in liquid regimes; that would be temporal coincidence, not identical feature plumbing.

## Forbidden

Any PnL / return / win-rate overlap tables — omitted by design.
