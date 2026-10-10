# Clanker v0.2.1 Observed Sell Cross-check Method

Status: **PREREGISTERED BEFORE NETWORK EXECUTION**

Purpose: distinguish a broken sell quote path from a fixed-$250 reverse amount
that exceeds executable depth.

From the sealed v0.2 Swap evidence and corrected PoolKeys, select up to eight
post-launch real token-to-paired Swap events. Apply a round-robin pool policy so
pool diversity is exhausted before adding another event from the same pool.
Quote each event's actual token input at the preceding block using the same
V4Quoter and empty hook data as v0.2.1.

A row passes when quoted paired output is within 25% of the event output.
`SELL_PATH_OBSERVED` requires at least five passes across at least three pools.
Transport contamination over 20% or budget exhaustion produces
`TRANSPORT_INSUFFICIENT`; all other completed outcomes are
`SELL_PATH_NOT_REPRODUCED`.

The run is capped at 25 attempts, concurrency one, and a minimum two-second
interval. Combined with the prior 175 attempts, the complete calibration cannot
exceed 200. No result establishes profitability or authorizes trading.
