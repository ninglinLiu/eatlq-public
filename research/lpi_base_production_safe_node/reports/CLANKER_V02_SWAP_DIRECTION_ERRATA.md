# Clanker v0.2 Swap Direction Errata

Status: **CRITICAL LABEL CORRECTION**

The v0.2 `swap_fill_proxy` and both v0.2.1 real-Swap selectors interpreted V4
Swap event signs backwards. The event amounts follow the swap caller's balance
delta: positive currency is received by the caller and negative currency is
paid by the caller.

Evidence is not based on a label guess. Three captured Base transactions decode
through EntryPoint and Universal Router to V4 action `0x06`. Official Uniswap
v4-periphery defines `0x06` as `SWAP_EXACT_IN_SINGLE`. The decoded parameters
are USDC-to-token exact-input buys with empty hookData, and their matching Swap
events record token positive and USDC negative.

Corrections:

- token0 buy: `amount0 > 0`, `amount1 < 0`;
- token1 buy: `amount1 > 0`, `amount0 < 0`;
- token0 sell: `amount0 < 0`, `amount1 > 0`;
- token1 sell: `amount1 < 0`, `amount0 > 0`.

Affected conclusions:

- v0.2 real-Swap fill proxies and direction-dependent features are invalid;
- v0.2.1's 8/8 “buy” cross-check is withdrawn;
- v0.2.1's 2/8 “sell” result and `SELL_PATH_NOT_REPRODUCED` are withdrawn;
- route-capture bytes, PoolKey verification, RPC error taxonomy, and direct
  Quoter entry/reverse outcomes remain valid;
- `QUOTE_PATH_NOT_VALIDATED` and `INSUFFICIENT_MARKET_DATA` remain valid.

All prior evidence stays immutable. Corrected code is offline-only until a new
non-overlapping validation run is authorized; only nine calls remain in the
current 200-call calibration budget.
