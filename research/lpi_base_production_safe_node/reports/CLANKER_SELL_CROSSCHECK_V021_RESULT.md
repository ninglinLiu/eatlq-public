# Clanker v0.2.1 Observed Sell Cross-check Result

Verdict: **SELL_PATH_NOT_REPRODUCED**

Eight real post-launch token-to-paired Swap events from four pools were quoted
at their preceding blocks. The run used eight public-RPC calls, had zero
transport failures, observed a minimum two-second call-start interval, and
brought the combined calibration total to 183/200.

Only two events passed the 25% output-error tolerance, both from pool
`0x275b12c8374bde2ee088703ca1418c3a9183662051d1e30ff6389e1853885bf6`.
Their relative errors were 9.31% and 16.35%. The other six events across all
four selected pools returned the same unverified application-revert shape seen
in the full hypothetical reverse quotes.

This falsifies the narrow explanation that only the fixed $250 hypothetical
round-trip size caused every reverse failure. It does not prove the historical
sells were impossible: those sells are chain facts. Instead, the configured
direct V4Quoter call with empty hook data is not a reliable general model of the
actual sell execution path.

The next blocker is contract semantics, not market statistics. Capture and
decode the public transaction calldata for these successful on-chain sells,
identify the actual router/actions/hookData, and reproduce that path before any
new PnL or alpha study.

Economic edge remains `INSUFFICIENT_MARKET_DATA`; trading is not authorized.
No production system, database, cache, Docker service, wallet, signing path,
order path, or gate was accessed or changed.
