# Clanker v0.2.1 Final Closeout

Primary adjudication: **PASS_WITH_CORRECTIONS**

Infrastructure verdict: **QUOTE_PATH_NOT_VALIDATED**

Economic verdict: **INSUFFICIENT_MARKET_DATA**

## Completed and verified

- Added an auditable public-RPC client with per-attempt pool/side/block,
  calldata hash, HTTP/JSON-RPC/revert taxonomy, response bytes, and latency.
- Verified 12/12 canonical PoolKeys against matching launch-receipt Initialize
  events and confirmed historical bytecode at V4Quoter and StateView.
- Executed and retained two calibration runs, an observed-event direction
  diagnostic, and route captures under a combined 198/200 public-RPC calls.
- Observed zero transport failures in the completed bounded runs.
- Preserved both failed and superseded evidence; no sealed artifact was
  rewritten.
- Captured the actual wrapper/router calldata for both corrected buy and sell
  event sets.
- Corrected the V4 Swap event caller-delta sign convention in authoritative
  code and added a regression test.
- Final offline test result: 138/138 OK, maximum RSS 46,156 KiB, socket messages
  sent/received 0/0.

## Material corrections

The original v0.2 direction interpretation was wrong. Positive V4 Swap event
amounts are received by the swap caller and negative amounts are paid by the
caller. Three captured transactions decode through EntryPoint and Universal
Router to action `0x06`, which official Uniswap v4-periphery defines as
`SWAP_EXACT_IN_SINGLE`; their decoded USDC-to-token parameters and matching
event signs provide a concrete golden counterexample.

As a result:

- v0.2 direction-dependent features and real-Swap proxies are invalid;
- v0.2.1 “8/8 buy” and “2/8 sell” comparisons are withdrawn;
- `SELL_PATH_NOT_REPRODUCED` is withdrawn;
- PoolKey verification, RPC transport findings, raw route captures, and direct
  Quoter success/revert outcomes remain valid;
- no PnL, alpha, or edge conclusion is supported.

Official code references:

- Uniswap v4 [Actions.sol](https://github.com/Uniswap/v4-periphery/blob/main/src/libraries/Actions.sol)
- Uniswap v4 [V4Router.sol](https://github.com/Uniswap/v4-periphery/blob/main/src/V4Router.sol)
- Uniswap v4 [IPoolManager.sol](https://github.com/Uniswap/v4-core/blob/main/src/interfaces/IPoolManager.sol)

## Strongest remaining counterexamples

1. A preceding-block `eth_call` is not transaction-index replay and misses
   earlier in-block and multi-leg state changes.
2. Seven corrected sell events map to only seven unique transactions, several
   with multi-leg or opposite-order top-level paths; event-to-action mapping is
   not yet deterministic.
3. Direct Quoter entry success plus reverse failure does not prove a sell was
   impossible through the actual router at the actual transaction state.
4. The prior 200-launch cohort and H1/H2/H3 results depend on invalid direction
   labels and cannot be repaired by relabeling only the final report.

## Safety and gates

No production ClickHouse, Redis, Docker, service, wallet, key, signer, order,
transaction submission, or backfill path was accessed. The two remaining calls
in the 200-call budget are deliberately unused.

```text
ready_for_authoritative_local_baseline = true
ready_for_offline_tests = true
ready_for_base_adapter_scaffolding = true
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
```

## Next single task: corrected execution semantics v0.3

Stay offline. Decode the 15 captured buy/sell route transactions deeply enough
to map each PoolManager Swap log to one exact V4 action, PoolKey, direction,
amount-in, amount-out, hookData, and action order. Create goldens that fail on
the old sign convention and reject multi-leg ambiguity. Then relabel the sealed
200-launch swap evidence and report the number of independent launches with an
observed external buy and later observed sell inside 30 minutes.

Do not calculate strategy PnL unless at least 30 independent launches have an
unambiguous observed round-trip sequence. Do not use prior-block Quoter output
as transaction-index truth. If the count is below 30, return
`INSUFFICIENT_ROUND_TRIP_MARKET_DATA` and specify the smallest trace-capable,
public-data-only acquisition needed next. No database, wallet, order, or
production service access is authorized.
