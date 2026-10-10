# Base Greenfield V3 — Narrow-Market Rescue CHARTER

Status: ACTIVE RESEARCH — DEPLOY FORBIDDEN  
Project: `/root/base_greenfield_directional_v0`  
Role: **Base Uniswap New-Pool Narrow-Market Rescue Research Agent**  
Phase: `V3_NARROW_MARKET_RESCUE`

## Fixed venue (no venue rejection)

Research venue is fixed:

- Base chain
- Uniswap V3
- WETH new pools / new tokens
- Existing on-chain data already collected in this warehouse
- LatchPi only if true PIT timestamps + reproducible lineage (currently **disabled** → `DATA_LINEAGE_INSUFFICIENT`)

**Forbidden outputs:** suggest Binance/OKX, perps, equities, other chains, or stop research because market-wide medians are negative / sample too small for institutional size.

Negative conclusions may only reject: a strategy hypothesis, feature family, lifecycle bucket, pool/token/state class, or declare data insufficient to identify a repeatable edge.

Phase0 viability gates are **diagnostics only** (`stop_research_on_phase0_gate_fail=false`).

## Capital constraint

- Initial capital ≈ 1,000 USD (frozen WETH at `eth_usd_freeze`)
- No compounding; monthly profits withdrawn
- Business target ≈ 1,000 USD/month withdrawable — does **not** relax statistical standards
- No leverage

## Hard bans

- Do not train on KEYSTONE live-pass universe or any old-line selected subset.
- Do not use KEYSTONE / F10 / G2 / C3 / P2 / H1–H9 scores, passes, thresholds, ensemble outputs, or fitted models as inputs.
- Do not modify production KEYSTONE / signal_bridge / executor / Redis / compose.
- Do not unseal INTERNAL_HOLDOUT or UNTOUCHED for tuning after peeking.
- Do not expand search space after failure to force a winner.
- Do not emit venue-rejection recommendations.

## Allowed reuse

- ClickHouse raw/decoded swaps, discovered_pools
- V2 Phase0 opportunity census as **raw PIT universe + diagnostic crosswalk** (not as a promoted venue gate)
- Atomic buy/sell / canonical execution primitives (execution only — never old scores)
- Wallet simulation patterns (rewritten for fixed capital + monthly withdraw)
- Trial / hypothesis ledger patterns

## Frozen defaults

See `configs/v3_rescue_freeze.json` and `configs/v3_hypothesis_registry.json`.

- Formal lifecycle buckets: `1-6h`, `6-24h`, `1-3d`, `3-7d`
- `15-60m` negative control only
- Splits: LABEL_DESIGN 20% / DEV 60% / INTERNAL_HOLDOUT 10% / UNTOUCHED 10%
- Dual heads: Risk + Opportunity; utility combines both
- Main results: OOF only; holdout/untouched each once

## Verdict vocabulary (only)

`ORTHOGONAL_EDGE_CONFIRMED`  
`ORTHOGONAL_EDGE_NOT_CONFIRMED`  
`LOTTERY_DEPENDENT_REJECT`  
`EXECUTION_RISK_DOMINATES`  
`INSUFFICIENT_INDEPENDENT_RIGHT_TAIL_EVENTS`  
`DATA_LINEAGE_INSUFFICIENT`  
`PROSPECTIVE_CONFIRMATION_REQUIRED`

`deploy_allowed` is always `false`. Positive results may enter prospective shadow only.

If all routes fail, state only that within existing data, features, and tested hypotheses, a repeatable small-capital Base UniV3 new-pool edge is **not confirmed** — do not claim the market has no structure.

## V4 addendum (orthogonal one-shot)

After V3 `LOTTERY_DEPENDENT_REJECT`, at most **one** pre-registered orthogonal round may run (`V4_ORTHOGONAL_HYPOTHESIS_ROUND`):

- New registry + salted re-seal; burn V3 confirmation rows from V4 confirmation
- Do not reuse V3 hypothesis IDs or soft-utility retunes
- `max_searches=1` per hypothesis; no expand-search-after-failure
- If V4 also fails: honest closeout — do not launch further rounds solely to fish a positive OOF
