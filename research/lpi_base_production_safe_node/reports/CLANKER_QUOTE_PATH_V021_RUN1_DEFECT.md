# Clanker Quote-Path v0.2.1 Run 1 — Cross-check Selection Defect

Status: **EVIDENCE RETAINED; CROSS-CHECK ACCEPTANCE NOT MET**

Source commit: `f32fe5cdf796868b45eb1c153837a760d1a110dc`

The bounded public-RPC run completed with 85 network attempts, zero transport
failures, 12/12 canonical pool-id matches, 8 successful entry quotes, one
successful same-block reverse quote, and zero successful 30-minute exit quotes.
Its provisional infrastructure verdict was `QUOTE_PATH_NOT_VALIDATED`.

The run is not the final calibration because the implementation truncated the
candidate Swap list to eight before filtering for buy direction. Five of those
eight were then rejected as non-buy events, leaving only three real-Swap
cross-checks. Although all three passed, this does not satisfy the preregistered
minimum of five.

The evidence directory is preserved byte-for-byte as
`evidence/clanker_quote_calibration_v021/`. It will not be overwritten or
presented as meeting the cross-check acceptance criterion.

A correction must select verified buy-side events before applying the eight-
attempt cap, commit that correction, and run to a new evidence directory. The
combined first and corrected runs must remain within 200 public-RPC network
attempts. Production databases, Redis, Docker, wallets, orders, and gates remain
untouched.
