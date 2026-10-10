# Clanker v0.2.1 Sell Route Capture Method

Status: **PREREGISTERED BEFORE NETWORK EXECUTION**

Fetch the eight public transactions used by the observed-sell cross-check and
preserve destination, sender, value, calldata, selector, and calldata SHA-256.
If the top-level selector is Universal Router `execute(bytes,bytes[],uint256)`,
decode only that outer envelope and retain each raw command input with its hash.
Do not guess nested command or hook signatures.

The capture is read-only, uses at most 17 remaining public-RPC attempts, and
keeps all quote calibration and route capture calls combined at no more than
200. It has no transaction-submission, wallet, production database, cache,
Docker, order, or backfill path. Capturing calldata is not proof of profitability.
