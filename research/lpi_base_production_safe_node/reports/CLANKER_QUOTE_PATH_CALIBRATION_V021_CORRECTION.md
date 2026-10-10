# Clanker Quote-Path v0.2.1 Corrected Run Addendum

Status: **PREREGISTERED BEFORE CORRECTED NETWORK EXECUTION**

Run 1 used 85 public-RPC attempts and retained only three cross-checks because
it applied the eight-candidate cap before buy-direction filtering. Its evidence
is retained and does not meet acceptance.

The corrected runner changes only two execution controls:

1. it verifies Swap direction first and then selects the earliest eight genuine
   buy-side events;
2. it writes a new, non-overwriting evidence directory and caps the corrected
   run at 115 attempts, keeping both runs combined at no more than 200.

Pool selection, addresses, calldata, quote blocks, sizing, StateView checks,
25% real-Swap tolerance, verdict thresholds, rate limit, concurrency, and all
production-safety boundaries remain unchanged. The corrected result supersedes
Run 1 only for the cross-check completeness decision; Run 1 remains part of the
audit trail.
