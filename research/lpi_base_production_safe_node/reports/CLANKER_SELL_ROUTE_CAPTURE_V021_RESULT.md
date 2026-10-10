# Clanker v0.2.1 Route Capture Result and Direction Discovery

Status: **CAPTURE COMPLETE; PRIOR BUY/SELL LABELS INVALIDATED**

Eight public transactions were captured with eight calls, bringing the full
calibration total to 191/200. The top-level transactions used three wrapper
destinations and selectors rather than directly targeting Universal Router.

Offline decoding established:

- three `0x765e827f` EntryPoint envelopes each contain one packed UserOperation;
- their account calldata contains Universal Router `execute`;
- the Universal Router command is `0x10` (V4 swap command envelope);
- the nested V4 actions are `0x06, 0x0c, 0x0f`;
- official v4-periphery assigns `0x06` to `SWAP_EXACT_IN_SINGLE`;
- the decoded action-0x06 pool key, direction, amount, and empty hookData show
  USDC-to-token exact-input buys for pool `0xb57755...71def`;
- the corresponding Swap events have token amount positive and USDC amount
  negative.

Therefore the v0.2 code's sign convention was reversed. For these events,
positive is the swap caller's received currency and negative is the caller's
paid currency. The previous `swap_fill_proxy`, buy-side cross-check selector,
and sell-side cross-check selector used the opposite interpretation.

Consequences:

- the reported 8/8 “buy” cross-checks were actually sell events compared
  against buy quotes and are invalid as direction validation;
- the reported 2/8 “sell” passes were actually buy events compared against
  sell quotes and are invalid as direction validation;
- `QUOTE_PATH_NOT_VALIDATED` remains correct;
- `SELL_PATH_NOT_REPRODUCED` is withdrawn because its sample was not sells;
- all economic conclusions remain `INSUFFICIENT_MARKET_DATA`.

The captured raw calldata remains valid evidence. Code and labels must be
corrected before any new alpha or PnL statement.
