# Clanker Alpha v0.2 Quote-Path Errata

Status: **CORRECTED INTERPRETATION; ECONOMIC VERDICT UNCHANGED AS INSUFFICIENT**

The v0.2 artifact correctly withdrew the v0.1 negative-edge claim and reported
`INSUFFICIENT_MARKET_DATA`. Its finer status labels are not reliable evidence
that a pool had no executable quote.

## Defect

The v0.2 RPC journal contains 426 failed `eth_call` attempts, including 364
HTTP 429 responses. The runner caught these failures and then emitted
`NO_TRADE_NO_QUOTE` or `EXIT_QUOTE_UNAVAILABLE` without preserving the failed
attempt on the corresponding pool/side/block label. Consequently:

- `NO_TRADE_NO_QUOTE` does not prove absence of a market;
- `EXIT_QUOTE_UNAVAILABLE` does not distinguish transport failure from contract
  revert or genuine missing liquidity;
- 0 executable holdout entries cannot be used for an alpha conclusion;
- the 200-pool result is transport-contaminated, not a market census.

The sealed v0.2 evidence is not modified. This erratum changes interpretation,
not source bytes.

## v0.2.1 correction contract

The calibration runner records every network attempt with pool, token, quote
side, historical block, destination, calldata hash, HTTP status, JSON-RPC code,
revert data/selectors, response size, latency, and an explicit error class.
Application reverts are never retried as transport errors and transport errors
are never converted to market labels.

The bounded run uses 12 preregistered pools, one public Base RPC endpoint,
concurrency 1, at most 200 network attempts, and a minimum two seconds between
attempt starts. It has no production database, cache, Docker, wallet, signing,
transaction submission, or backfill capability.

Possible results are limited to:

- `QUOTE_PATH_VALIDATED`;
- `QUOTE_PATH_NOT_VALIDATED`;
- `TRANSPORT_INSUFFICIENT`.

None is an economic-edge or trading authorization.
