# Clanker v0.2.1 Actual Sell Route Capture Result

Status: **COMPLETE — ROUTE EVIDENCE READY, QUOTE REPLAY STILL INVALID**

Seven unique transactions covered the eight corrected sell events. Seven public
RPC calls brought the complete calibration total to 198/200, with no transport
failure or budget exhaustion.

- six transactions target `0xd8ba9d1a99fc21f0eca24e9b85737c28a194a4e2`
  with selector `0xeffbec13`;
- one targets Multicall3 and wraps a call to `0x62bff5...b088` with selector
  `0xc3c16ee4`;
- three simple custom-router calldata payloads visibly encode token-to-WETH
  ordering, zero native value, the verified dynamic-fee PoolKey, tick spacing
  200, and the Clanker hook;
- other captured transactions contain multi-leg or opposite-order paths, so a
  Swap event cannot be attributed to the top-level route merely by inspecting
  one path array.

This strengthens two limitations. First, actual execution is wrapper/router
based rather than the direct top-level Quoter call used by v0.2. Second,
preceding-block `eth_call` is not transaction-index replay: it misses earlier
state changes and multi-leg effects within the event's block/transaction.

The next validation must decode event-to-action correspondence and replay at
transaction-index state (or use a trace-capable archive source). Until then,
the quote path, executable exits, PnL labels, and alpha remain unvalidated.

The public-RPC calibration budget is closed at 198/200; the remaining two calls
must not be used as a new sample. Database and backfill gates remain false.
