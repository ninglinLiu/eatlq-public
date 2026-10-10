# Debot Discover vs Hyperliquid own ledger

status: `RECONCILED_WITH_GAPS`
generated_at: 2026-09-17T08:18:20+00:00

- Debot board venue: `hyperliquid_perp` (NOT Base/BSC spot)
- counter-ledger: `https://api.hyperliquid.xyz/info userFillsByTime.closedPnl`
- rows / ok / fail: 996 / 994 / 2
- fail split: no HL account 0, still throttled 2
- ranked wallets that actually closed a trade in the labeled window: 987
- **ranked but inactive in window**: 7
- our 2000-fill cap hit (excluded from magnitude): 306
- clean rows judged (active, uncapped): 683
- sign agree (all / active / clean): 0.9637826961770624 / 0.9706180344478217 / 0.986822840409956
- magnitude within ±10% / ±25% / 2x: 0.7635829662261381 / 0.869309838472834 / 0.947136563876652 (n=681)
- median |Debot/HL| ratio: 0.9999999999999993
- median |Debot-HL| USD: 8.150234999999839

Debot realized_pnl (USD) vs Hyperliquid closedPnl summed over the same labeled window, gross of fees. Debot rows labeled 'all' are excluded: the window is not reproducible. Magnitude stats use only active rows below our 2000-fill page cap; capped rows undercount OUR side, not Debot's.

Gaps: OUR_FILLS_CAP_TRUNCATION, HL_THROTTLED_COVERAGE_HOLE

Debot's Base/BSC spot smart-money board is `SOURCE_BLOCKED_AUTH`.
GO remains false.

