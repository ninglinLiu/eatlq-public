# LARGE_BUY_ABSORPTION_v1 — DIAGNOSTIC_REPLAY Review

Generated: `2026-07-21T06:24:51Z`

> Historical label: **DIAGNOSTIC_REPLAY**. Does **not** inherit v0 OOS certification.
> Narrative: **order-flow**, not smart money. Legacy alias: WHALE_EVENT.

## Freeze-readiness gate

- Lookahead: `LOOKAHEAD_FREE_PASS`
- Loss truth: `LOSS_TRUTH_OK`
- Primary scheduled verdict: `SCHEDULED_EDGE_UNCONFIRMED`
- Gate: `FREEZE_READY`
- Live: `NO_GO`
- Monthly $1000 target: `TARGET_NO_GO`

## Scorecard (scheduled primary)

| Arm | Scheduled verdict | Selected | Fills | Skip | Sched PnL WETH | Cum withdrawn USD | Unconst diag WETH | Paired Δ p05 day |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| POOL_ONLY | BASELINE | 75797 | 162 | 0.9978627122445479 | 0.15242701479834248 | 457.28104439503323 | 51.89391258303924 | 0.0 |
| LARGE_BUY_ONLY | SCHEDULED_EDGE_UNCONFIRMED | 78329 | 422 | 0.9946124679237575 | 0.21193917829458972 | 635.8175348837821 | 88.676514909058 | -0.2042368828065957 |
| ABSORPTION_ONLY | SCHEDULED_EDGE_NEGATIVE | 229080 | 415 | 0.9981884057971014 | 0.14310711247328042 | 429.32133741985285 | 129.14052971666518 | -0.2563777933516451 |
| LARGE_BUY_ABSORPTION | SCHEDULED_EDGE_UNCONFIRMED | 48148 | 407 | 0.9915468970673756 | 0.16875765392590655 | 506.27296177773206 | 48.56828441621684 | -0.2042467604487689 |
| CAPACITY_MATCHED_RANDOM | CONTROL | 75797 | 405 | 0.9946567806113699 | 0.1967243567085286 | 590.1730701255987 | 66.78960405069934 | nan |

## Governance

- Code hash: `0822f5e82542cf1b8cdc079735458be528968241439ede0e7c8f36a5242c971f`
- Config hash: `25cde3abc215cdb12645e9d8db80fd7689f287b529080e2059e180bb734c47b0`
- Data hash: `075b801d092368034462d4027b765e87e59c31002f31e3d07caa11836e1ef6f9`
- Prospective T0: `2026-07-21T06:24:51Z`
- Shadow: observer-only (`python3 -m src.shadow_recorder`)

## Hard bans still in force

- No live trading
- No retune after seeing this replay
- No smart-money narrative
- Unconstrained PnL is diagnostic only

