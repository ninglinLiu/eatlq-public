# Clanker v0.2.1 Actual Sell Route Capture Method

Status: **PREREGISTERED AFTER DIRECTION ERRATUM, BEFORE NETWORK EXECUTION**

The eight events called “buy cross-checks” in the sealed corrected calibration
are, under the corrected caller-delta sign convention, actual token-to-paired
sells. Fetch those eight public transaction inputs and retain the same raw route
envelope fields used in the earlier capture.

At most nine calls remain; the run must keep all calibration calls combined at
or below 200. No quote, new market sample, database, wallet, order, or production
service is accessed. The purpose is only to identify the execution envelopes of
real sells so a later clean-window quote replay can use the correct route.
