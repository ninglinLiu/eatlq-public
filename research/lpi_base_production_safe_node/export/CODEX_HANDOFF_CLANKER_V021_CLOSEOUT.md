# Codex Handoff — Clanker v0.2.1 Closeout

Adjudication: **PASS_WITH_CORRECTIONS**

The bounded public-RPC work is complete and closed at 198/200 calls. Final code
corrects the V4 Swap caller-delta direction; final offline tests are 138/138 OK.

Do not use the v0.2 direction features, 8/8 “buy” comparison, 2/8 “sell” result,
or any earlier PnL conclusion. They are superseded by
`reports/CLANKER_V02_SWAP_DIRECTION_ERRATA.md` and
`reports/CLANKER_V021_FINAL_CLOSEOUT.md`.

Valid retained facts are 12/12 verified PoolKeys, zero observed transport
failures in v0.2.1 bounded runs, direct Quoter entry/reverse raw outcomes, and
captured raw buy/sell transaction routes. Quote path and economic edge are not
validated.

Implementation correction commit: `e883ba031edf47dfdb7cd414ed22dd9d7a490922`

Final actual-sell route evidence commit: `c492e85e400d83e7a74ac122cf60bdcd8bb5224c`

All database/backfill gates remain false. Next work is the fully offline v0.3
event-to-action decoder and corrected round-trip coverage census described in
the closeout report.
