# Clanker v4 Historical Quote-Path Calibration v0.2.1 Result

Verdict: **QUOTE_PATH_NOT_VALIDATED**

Economic conclusion: **NOT AUTHORIZED — INSUFFICIENT MARKET DATA remains**

Corrected source commit: `cd4c31a52c2b0ac07895e6643f5f0d8f28fa2644`

## What is established

- 12/12 launch receipts contained exactly one matching `Initialize` event and
  the decoded canonical PoolKey reproduced the sealed pool id.
- Historical bytecode was present at both configured V4Quoter and StateView
  addresses.
- The corrected run made 90 public Base RPC attempts; together with the
  retained incomplete run, total attempts were 175/200.
- Concurrency was one, minimum observed attempt-start separation was two
  seconds, response bytes were 350,576, and transport failures were 0/90.
- 8/12 paired-to-token entry quotes succeeded.
- 8/8 selected real buy-side Swap cross-checks passed the preregistered 25%
  output-error tolerance. Relative error ranged from 2.18% to 20.96%, with a
  median of 2.91%.
- All JSON artifacts parse under strict JSON and all seven payload hashes match
  `SHA256SUMS.json`.

## Why the quote path is not validated

Of the eight successful entry quotes, only one could quote the full hypothetical
token output back to the paired currency at the same block. None could quote
that same token amount at the 30-minute exit block. The 19 failures were
application-level `UNKNOWN_REVERT` results, not HTTP or network failures.

The repeated outer selector `0x6190b2b0` and nested values are retained raw.
Their signatures have not been verified, so this report does not name or infer
the errors. It also does not equate a Quoter revert with an impossible on-chain
sell.

The eight passing event cross-checks cover only three pools, with six events
from one pool. They validate the paired-to-token quote magnitude on those
events, not broad pool independence and not the exit direction.

The strongest unresolved alternatives are:

1. the $250 entry output is too large for the available reverse-side range at
   the queried state;
2. reverse quotes require different hook data or a different quote path;
3. the pool permits smaller real sells even though the full hypothetical
   round-trip quote reverts;
4. some pools are initialized at a boundary or with zero active liquidity and
   are genuinely non-executable in one direction.

## Safety and gates

No ClickHouse, Redis, Docker, production service, wallet, private key, signing,
transaction submission, order, or backfill path was accessed. Database probe
and micro-batch backfill gates remain false. No trading decision may use this
calibration as proof of edge.

## Next economically relevant test

Use the remaining 25-call combined budget to quote observed real sell-side Swap
inputs at their preceding blocks. If small real sells reproduce while the full
$250 round-trip amount reverts, the next alpha study must size by executable
depth rather than fixed notional. If observed sells also fail, identify verified
hook data/router semantics before any PnL replay.
