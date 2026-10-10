# F01_OFS_SIGN_RUNLEN — Mechanism

## Hypothesis

Same-sign swap **runs** that are long relative to the pool’s recent completed-run distribution, and that carry enough WETH notional, indicate persistent aggressive flow. Fade traders who lean against the run are the economic counterparties.

## Why swap-level (not bars)

Bar aggregation destroys run boundaries (multiple signs inside one minute). The edge claim is about **sequence structure**, so the atomic observation is the swap.

## Distinction from failed priors

- Not unsigned “volume spike”
- Not KEYSTONE `p_fatal` age-window ML
- Uses explicit run length + optional z vs **completed** prior runs only

## Known limitations on this VPS extract

- `transaction_index` UNKNOWN → same-block order may be wrong
- b5 TSV lacks explicit buy/sell flags → Stage1A sanity prefers v3r6 rows with `is_buy_weth_to_token`
