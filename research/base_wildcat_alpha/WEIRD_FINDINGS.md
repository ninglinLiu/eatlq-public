# Weird Findings

1. **The observed market is extremely sparse outside a small core.** 7,451 pools yield only 33,599 active pool-hours in 4.5 months. “No row” cannot be treated as a zero-return or sellable state.
2. **ABI decoding can silently erase the market.** Decimal parsing of the hex ABI words produced an empty tick panel; correct signed/unsigned decoding restored 33,599 observations. Any old or future result using naive conversion is DATA_INVALID.
3. **Seventy-eight pool-hours imply >100x tick movement inside one hour.** They are quarantined, not celebrated as alpha. Manual trace/quote validation is required.
4. **A highly significant effect can be economically dead.** RV01 has q≈3.5e-23 but only 4.00 gross bps versus a 120 bps two-leg cost proxy.
5. **Directional flow persistence decays non-monotonically.** F03 delayed gross spreads are 1h=194.9, 2h=210.2, 4h=99.1 bps. This may be discrete event-time selection, not a smooth causal decay.
6. **Broad participation may be more valuable as a veto than a long signal.** S02 separates adverse future paths even though identity-level “smart wallet” information was never used.
7. **Post-dormancy state has repeatable behavior but no direct price value yet.** C03 separates the probability of another event within four hours, suggesting episode-state modeling rather than token-direction prediction.
8. **The breadth/activity anomaly is mostly tail dependence.** A01 loses over 90% of its spread after removing the top 1% outcomes; retain it as an extreme-event locator, not a strategy.
9. **LP tables look huge but cover almost no universe.** Millions of rows collapse to 7 mint/burn pools and 3 collect pools. Row count alone would badly overstate research breadth.
10. **Source duplication is itself material.** V3 raw rows exceed unique event keys by roughly 2.84 million; duplicate-pressure is preserved as a diagnostic instead of silently discarded.

Future validation priority: inspect the 78 quarantined tick episodes; collect F03 read-only buy and sell quotes; build a complete LP/bridge/lending event panel.
