# Clanker v4 Historical Quote-Path Calibration v0.2.1

Status: **PREREGISTERED BEFORE NETWORK EXECUTION**

## Question

Can the configured Uniswap v4 Quoter reproduce paired-token entry quotes,
same-block reverse quotes, and real swap magnitudes for the Clanker v4 pool
keys in the sealed v0.2 evidence?

This is infrastructure calibration. It cannot establish alpha, expected PnL,
or permission to trade.

## Fixed sample

The runner selects exactly 12 positions from the sealed 200-launch v0.2 sample:

- prior entry success with observed swaps: indices 21, 75, 86, 180;
- prior entry success without observed swaps: indices 0, 1, 7, 10;
- prior entry failure with observed swaps: indices 16, 17, 18, 19.

Selection is not changed after observing v0.2.1 RPC results. Pool keys are
recovered from each launch transaction's `Initialize` event and accepted only
if a canonical PoolKey hash exactly reproduces the sealed pool id.

## Calls and directions

For each verified pool:

1. query StateView slot0 and liquidity at the intended entry block;
2. quote the sealed $250 sizing amount from paired currency to token;
3. if successful, immediately quote the entire token output back to paired
   currency at the same block;
4. if successful, query StateView and quote that token output back to paired
   currency at the sealed 30-minute exit block.

Up to eight real buy-side Swap events strictly after their launch block are
cross-checked by quoting the event's actual input at the preceding block. A
cross-check passes when relative token-output error is at most 25%. This is a
pre-block approximation, not transaction-index replay.

## Transport and evidence policy

- endpoint: `https://mainnet.base.org`;
- public JSON-RPC only; concurrency 1;
- minimum 2 seconds between network-attempt starts;
- at most 200 network attempts and 50 MiB response bytes;
- transport retry delays: 2, 5, and 15 seconds;
- stop after six consecutive HTTP 429 attempts;
- record every attempt, including HTTP status, JSON-RPC code, raw revert data,
  visible selectors, destination, historical block, calldata SHA-256, response
  bytes, and latency;
- strict JSON (`NaN` and infinity forbidden);
- implementation and tests must be committed before network execution;
- existing v0.2 evidence is read-only and never rewritten.

Production ClickHouse, Redis, Docker, wallets, signing, transaction submission,
database gates, and backfill gates are outside the runner's capabilities.

## Preregistered verdicts

`TRANSPORT_INSUFFICIENT` has priority if the call budget is exhausted or more
than 20% of network attempts fail for transport reasons.

Otherwise, `QUOTE_PATH_VALIDATED` requires all of:

- at least 8 successful entry quotes;
- at least 8 successful same-block reverse quotes;
- at least 5 passing real-Swap cross-checks.

All other completed outcomes are `QUOTE_PATH_NOT_VALIDATED`.
