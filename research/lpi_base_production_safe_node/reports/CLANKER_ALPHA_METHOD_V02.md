# Clanker Alpha Method v0.2 (Pre-Registered)

```text
status = PRE_REGISTERED
study = CLANKER_ALPHA_EXECUTION_VALIDITY_V02
v01_window_excluded_to_block = 48712326
```

## Quote hierarchy

1. **Preferred:** Uniswap v4 `V4Quoter.quoteExactInputSingle` via `eth_call` at `quote_block`  
   - Quoter: `0x0d5e0F971ED27FBfF6c2837bf31316121532048d`  
   - StateView: `0xA3c0c9b65baD0b08107Aa264b0f3dB444B867A71`
2. **If historical quoter reverts (observed with Clanker hooks):** use a **qualifying real Swap print** in `[intended_entry, intended_entry+max_wait]` as `REAL_SWAP_FILL_PROXY`, recording amount0/amount1, fee, tx, log index, and size vs target notional.
3. **Else:** `QUOTE_UNAVAILABLE` / `NO_TRADE_NO_QUOTE` (PnL=0).

Fixed constant impact (e.g. 0.003) is **not** an acceptable “real quote”.

## Cost scenarios

| scenario | slippage allowance | size-mismatch penalty | gas |
|---|---|---|---|
| optimistic | 0.001 | 0 | observed baseFee × 180k × 1.0 |
| base | 0.005 | max(0, 1 - swap_notional/target) × 0.5 of edge | baseFee × 220k × 1.15; LP fee from Swap.fee or DYNAMIC_UNKNOWN |
| stressed | 0.02 | same × 1.0 | baseFee × 300k × 1.5 |

L1 data fee: `UNKNOWN` (recorded, not invented).

## USD / ETH

- USDC pairs: USD notionals native.
- WETH pairs: report **ETH-denominated** primary PnL; USD only if a timestamped ETH/USD source is attached for that run.
- No frozen constant ETHUSD mixed across windows without source.

## RPC coverage

- Batch `eth_getLogs` with `topics[1]` OR-lists of poolIds.
- Per-pool coverage matrix: requested / successful / failed / ratio / primary-horizon complete.
- Limits: concurrency=1, calls≤1500, raw≤250MiB, retry≤1.

## Confirmation window

- Strictly after v0.1 `to_block=48712326`.
- ≥200 continuous launches target.
- Holdout evaluated **once**.
- Executable entry definition: timeline-valid entry quote within max wait + timeline-valid exit quote (or explicit `EXIT_QUOTE_UNAVAILABLE` without forced zero unless liquidity removal proven).
