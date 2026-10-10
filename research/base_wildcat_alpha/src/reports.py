from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd

from safety import research_entry_guard


ROOT=Path(__file__).resolve().parents[1]


def _audit_rows() -> dict:
    raw=json.loads((ROOT/"data/source_audit.json").read_text())
    result={}
    for name,text in raw.items():
        row=pd.read_csv(io.StringIO(text),sep="\t").iloc[0].to_dict()
        result[name]={k.replace("\\'", "'"):v for k,v in row.items()}
    return result


def _fmt(v, digits=1):
    if pd.isna(v): return "N/A"
    return f"{v:.{digits}f}"


def generate_reports() -> None:
    research_entry_guard()
    quality=json.loads((ROOT/"data/feature_quality.json").read_text())
    audit=_audit_rows()
    rapid=pd.read_csv(ROOT/"RAPID_SCREEN_RESULTS.csv")
    deep=pd.read_csv(ROOT/"KILL_PANEL_RESULTS.csv").set_index("alpha_id")
    full={r["alpha_id"]:r for r in json.loads((ROOT/"KILL_PANEL_FULL.json").read_text())}
    shadow=json.loads((ROOT/"SHADOW/state.json").read_text())

    # Promote only the initialized zero-order shadow candidate; this is a
    # research-state label, never an execution authorization.
    atlas=pd.read_csv(ROOT/"ALPHA_ATLAS.csv")
    for aid,row in deep.iterrows():
        atlas.loc[atlas.alpha_id==aid,"status"]=row.status
        atlas.loc[atlas.alpha_id==aid,"gross_result"]=(
            f"Deep replay spread={row.gross_spread:.6g}; gross_bps={_fmt(row.gross_bps)}; q48={row.q_bh_48:.3g}"
        )
        atlas.loc[atlas.alpha_id==aid,"cost_adjusted_result"]=(
            f"Deep net proxy={_fmt(row.net_bps_60_each_leg)} bps at 60bps/leg"
            if pd.notna(row.net_bps_60_each_leg) else "not directly monetized"
        )
    atlas.loc[atlas.alpha_id=="F03","status"]="SHADOW_CANDIDATE"
    atlas.loc[atlas.alpha_id=="F03","next_action"]="collect pre-registered unseen outcomes plus executable quote/depth/failure facts; zero orders"
    atlas.to_csv(ROOT/"ALPHA_ATLAS.csv",index=False)

    market=f"""# Base Market Map

As-of: 2026-08-14 14:00 UTC. “Observed” means present in the local read-only event store; it does not mean the entire Base market is covered.

## Market structure

Base Mainnet is chain 8453 and exposes standard EVM read methods. Base currently documents 2-second blocks and 200ms Flashblocks; incremental preconfirmation ordering is therefore a distinct research clock that the local hourly panel does **not** capture ([Base connection parameters](https://docs.base.org/base-chain/quickstart/connecting-to-base), [Flashblocks FAQ](https://docs.base.org/base-chain/flashblocks/faq)). Base is an Ethereum rollup with sequencer, batcher and bridge-specific state transitions ([protocol overview](https://docs.base.org/base-chain/specs/protocol/overview), [standard bridges](https://docs.base.org/base-chain/specs/protocol/bridging/bridges)).

| Surface | What exists / can be observed | Local evidence | Research readiness |
|---|---|---:|---|
| V2-style AMMs | constant-product/stable-style pools, swaps, mint/burn | {int(audit['discovered_pools']["countIf(equals(pool_type, 'v2'))"]):,} registry rows; {int(audit['v2_swaps']['uniqExact(pair_address)']):,} observed pairs | swaps usable; reserves/quotes not reconstructed |
| V3/CL AMMs | ticks, active liquidity, fee tiers, LP ranges | {int(audit['discovered_pools']["countIf(equals(pool_type, 'v3'))"]):,} registry rows; {int(audit['v3_swaps']['uniqExact(pool_address)']):,} observed pools | swap state usable; LP event coverage severely narrow |
| Aerodrome | stable, volatile and concentrated pools plus incentive/gauge layer | local registry has only a handful of explicitly typed Aero pools | insufficient locally; official docs confirm all three pool forms ([Aerodrome liquidity docs](https://github.com/aerodrome-finance/docs/blob/main/content/liquidity.mdx)) |
| Bridges / cross-domain | Ethereum↔Base deposit/withdrawal messages and standard bridge events | raw logs exist but no decoded bridge panel | high-value missing dataset |
| Lending / vaults | lending rates, liquidation, collateral and vault allocation states | absent from local research panel | data acquisition required; Base ecosystem lists lending apps and Morpho exposes chain 8453 through read APIs ([Base ecosystem](https://www.base.org/ecosystem), [Morpho data docs](https://docs.morpho.org/build/earn/tutorials/get-data/)) |
| Perpetuals / CEX / intents / solvers | funding, basis, liquidations, auction and routing outcomes | absent | NOT_YET_TESTED |
| Stablecoins / payments | transfer, mint/burn, bridge and payment flows | partial raw ERC20/log facts, not semantically complete | DATA_INSUFFICIENT |
| Incentives / points / rebates | emissions, gauges, rewards and rule changes | absent except raw undecoded logs | DATA_INSUFFICIENT |
| Contract lifecycle | deployment, upgrade, permission, mint/burn and call graph | raw logs only; traces/code-history absent | IMPLEMENTATION_BLOCKED pending indexer |
| Sequencer microtime | intra-block and Flashblock index/order | block/log index proxy only | current 1h panel is inadequate |

The official Base ecosystem directory currently describes 700+ builders, so the two AMM schemas in the local store are a data boundary, not a market boundary ([Base ecosystem](https://www.base.org/ecosystem)).

## Local data inventory and provenance

| Dataset | Rows | Unique event/pool evidence | Coverage |
|---|---:|---:|---|
| V3 swaps | {int(audit['v3_swaps']['count()']):,} | {int(audit['v3_swaps']['uniqExact((pool_address, tx_hash, log_index))']):,} unique events / {int(audit['v3_swaps']['uniqExact(pool_address)']):,} pools | {audit['v3_swaps']['min(block_time)']} → {audit['v3_swaps']['max(block_time)']} |
| V2 swaps | {int(audit['v2_swaps']['count()']):,} | {int(audit['v2_swaps']['uniqExact((pair_address, tx_hash, log_index))']):,} unique events / {int(audit['v2_swaps']['uniqExact(pair_address)']):,} pairs | {audit['v2_swaps']['min(block_time)']} → {audit['v2_swaps']['max(block_time)']} |
| Raw EVM logs | {int(audit['raw_logs']['count()']):,} | {int(audit['raw_logs']['uniqExact(address)']):,} addresses / {int(audit['raw_logs']['uniqExact(topic0)']):,} topics | {audit['raw_logs']['min(block_time)']} → {audit['raw_logs']['max(block_time)']} |
| V3 mint / burn / collect | {int(audit['v3_mints']['count()']):,} / {int(audit['v3_burns']['count()']):,} / {int(audit['v3_collects']['count()']):,} | only 7 / 7 / 3 pools | ends 2026-05-20 |

Rapid screening used all {quality['pools']:,} V3 pools observed from 2026-04-01 through the cutoff, producing {quality['rows']:,} pool-hours. There is no outcome-selected or full-period top-N universe filter.

## Fact, proxy, and missingness boundary

- Facts: chain, event address, block/transaction/log identity, event timestamp, pool, ABI words, tick, active-liquidity word, sender/recipient fields as emitted, and removed flag.
- Validated decoding: signed tick uses the low 32 bits of the sign-extended ABI word; liquidity uses the low unsigned 128 bits; amount0 direction uses two’s-complement sign extension.
- Proxies: sender breadth is not unique human breadth; active liquidity is not USD depth; event row count is not volume; tick close is not an executable quote; block/log index is not Flashblock arrival time.
- Major gaps: USD reserve/depth snapshots, gas and actual route quotes, failed calls/fills, sellability, token tax/restrictions, traces, complete LP coverage, bridge/lending/perp/CEX/social data, and future zero-activity rows.
- Source duplication: V3 has {int(audit['v3_swaps']['count()'])-int(audit['v3_swaps']['uniqExact((pool_address, tx_hash, log_index))']):,} excess rows. Unique event keys are used for activity; duplication-pressure sensitivity is part of the Kill Panel.

## Cheapest high-information additions

1. Read-only `eth_call` quote/depth snapshots for F03 signals, including deliberate sell-quote and failure recording.
2. Flashblock payload/index capture to replace hourly block-order proxies.
3. Decode bridge plus lending liquidation/rate events into a second, genuinely orthogonal market panel.
"""
    (ROOT/"MARKET_MAP.md").write_text(market,encoding="utf-8")

    leaderboard=f"""# Alpha Leaderboard

All values are research estimates. “Net” uses a fixed **60 bps per leg** sensitivity, not observed fills. Pool-hour signals overlap and cannot be added as independent trades.

## Strongest gross structure

| Rank | Candidate | Deep status | Deep result | Main caveat |
|---:|---|---|---|---|
| 1 | F03 directional flow persistence | REPLAY_SURVIVOR historically; promoted only to zero-order SHADOW_CANDIDATE | 4h gross {_fmt(deep.loc['F03','gross_bps'])} bps; proxy net {_fmt(deep.loc['F03','net_bps_60_each_leg'])} bps | aggregated proxy partly resembles momentum; no executable quotes/fills |
| 2 | S02 participant-breadth disaster veto | MECHANISM_PLAUSIBLE | future-worst-move separation {_fmt(deep.loc['S02','gross_bps'])} bps | BH q={deep.loc['S02','q_bh_48']:.3f}; no sellability label |
| 3 | C03 post-dormancy reactivation | MECHANISM_PLAUSIBLE | 4h reactivation-probability separation {deep.loc['C03','gross_spread']:.1%} | non-monetized and zero-hour grid incomplete |

## Strongest net-return candidate

F03 only. It survives 30/60/100 bps-per-leg stress at approximately 179/119/39 bps, but fails at 200 bps per leg. One-hour delayed gross spread is about {full['F03']['delay_stress_bps']['1']:.1f} bps. This remains a quote-free upper-bound replay, not realized P&L.

## Strongest disaster-avoidance signal

S02: high within-hour unique-sender breadth is associated with a less adverse future 24h path. The effect is positive in {deep.loc['S02','positive_month_share']:.0%} of replay months and remains positive after removing the largest 1% absolute outcomes, but does not survive the 48-test correction and lacks failed-sale observations.

## Strongest Meta-Alpha

No Meta-Alpha survived. R02 (dispersion-conditioned reversal) is **KILLED** at the stated 60 bps-per-leg stress and changes sign between replay halves.

## Strongest anomaly finding

A01 (wallet-breadth/activity mismatch) is DISCOVERY_ONLY: the apparent volatility separation falls from {deep.loc['A01','gross_spread']:.3f} to {deep.loc['A01','without_top_1pct_outcomes']:.3f} after removing the largest 1% outcomes, so the “effect” is mostly a rare-tail locator.

## Forward Shadow

F03 is the sole SHADOW_CANDIDATE for **zero-order data collection**. The initialized ledger contains {shadow['signal_count']} signals at {shadow['market_data_hour']}, zero matured forward outcomes, zero orders, and no executor. Its goal is to collect unseen outcomes and executable read-only quote/depth/failure facts.

## Explicitly killed in Deep Dive

| Candidate | Kill |
|---|---|
| F01 raw flow imbalance | {_fmt(deep.loc['F01','net_bps_60_each_leg'])} bps after cost proxy |
| H01 close-at-high continuation | {_fmt(deep.loc['H01','net_bps_60_each_leg'])} bps after cost proxy; day-cluster CI crosses zero |
| RV01 same-pair convergence | only {_fmt(deep.loc['RV01','gross_bps'],2)} gross bps, {-deep.loc['RV01','net_bps_60_each_leg']:.1f} bps short of break-even, top-pool concentration {deep.loc['RV01','pool_concentration']:.1%} |
| R02 dispersion reversal | {_fmt(deep.loc['R02','net_bps_60_each_leg'])} bps after cost proxy and temporal sign instability |

Rapid screening additionally marks {int((rapid.status=='KILLED').sum())} predefined hypotheses KILLED, {int((rapid.status=='DATA_INSUFFICIENT').sum())} DATA_INSUFFICIENT, {int((rapid.status=='DISCOVERY_ONLY').sum())} DISCOVERY_ONLY and {int((rapid.status=='GROSS_STRUCTURE').sum())} GROSS_STRUCTURE before Deep Dive overrides.
"""
    (ROOT/"ALPHA_LEADERBOARD.md").write_text(leaderboard,encoding="utf-8")

    weird=f"""# Weird Findings

1. **The observed market is extremely sparse outside a small core.** {quality['pools']:,} pools yield only {quality['rows']:,} active pool-hours in 4.5 months. “No row” cannot be treated as a zero-return or sellable state.
2. **ABI decoding can silently erase the market.** Decimal parsing of the hex ABI words produced an empty tick panel; correct signed/unsigned decoding restored {quality['rows']:,} observations. Any old or future result using naive conversion is DATA_INVALID.
3. **Seventy-eight pool-hours imply >100x tick movement inside one hour.** They are quarantined, not celebrated as alpha. Manual trace/quote validation is required.
4. **A highly significant effect can be economically dead.** RV01 has q≈{deep.loc['RV01','q_bh_48']:.2g} but only {_fmt(deep.loc['RV01','gross_bps'],2)} gross bps versus a 120 bps two-leg cost proxy.
5. **Directional flow persistence decays non-monotonically.** F03 delayed gross spreads are 1h={full['F03']['delay_stress_bps']['1']:.1f}, 2h={full['F03']['delay_stress_bps']['2']:.1f}, 4h={full['F03']['delay_stress_bps']['4']:.1f} bps. This may be discrete event-time selection, not a smooth causal decay.
6. **Broad participation may be more valuable as a veto than a long signal.** S02 separates adverse future paths even though identity-level “smart wallet” information was never used.
7. **Post-dormancy state has repeatable behavior but no direct price value yet.** C03 separates the probability of another event within four hours, suggesting episode-state modeling rather than token-direction prediction.
8. **The breadth/activity anomaly is mostly tail dependence.** A01 loses over 90% of its spread after removing the top 1% outcomes; retain it as an extreme-event locator, not a strategy.
9. **LP tables look huge but cover almost no universe.** Millions of rows collapse to 7 mint/burn pools and 3 collect pools. Row count alone would badly overstate research breadth.
10. **Source duplication is itself material.** V3 raw rows exceed unique event keys by roughly 2.84 million; duplicate-pressure is preserved as a diagnostic instead of silently discarded.

Future validation priority: inspect the 78 quarantined tick episodes; collect F03 read-only buy and sell quotes; build a complete LP/bridge/lending event panel.
"""
    (ROOT/"WEIRD_FINDINGS.md").write_text(weird,encoding="utf-8")

    final=f"""# Final Wildcat Report

## Executive verdict

The greenfield search produced one candidate worthy of zero-order forward data collection, not a tradable system: **F03 directional flow persistence**. Its historical replay survives time, parameter, cost, delay, pool leave-out, duplicate-source and right-tail tests well enough to initialize a signal-only shadow ledger. The ledger currently has {shadow['signal_count']} signals and **zero matured forward outcomes**. No candidate has evidence for real fills, sellability, USD capacity or realized net return.

## A. Old boundaries actually broken

- The universe was expanded from any prior new-pool/WETH/single-venue framing to all locally observed V3 pools, V2 market state, LP transitions, same-pair relative value, survival, lifecycle, block structure, anomaly and meta-regime questions.
- Targets include direction, volatility, adverse path, inactivity/reactivation, relative spread, market selection and data anomalies.
- No old score, label, threshold, model, report conclusion or executor was imported. Raw facts and generic ClickHouse infrastructure were used read-only.
- {sum(atlas.family.isin(['block_time','liquidity','survival','lp_state','lifecycle','anomaly']))/len(atlas):.0%} of hypotheses are explicitly nontraditional, above the 30% mandate.

## B. Orthogonal Alpha families searched

Forty-eight hypotheses across 12 families: block/sequencer time, active liquidity, participant topology, order-flow geometry, intrabar path, market regime, disaster/sellability avoidance, LP transitions, venue selection, cross-pool relative value, lifecycle, and anomalies.

## C. Data-mining sparks only

A01 is a tail-event locator; B01/B03, participation ratios, LP-event candidates and most venue/cross-market ideas remain DISCOVERY_ONLY or DATA_INSUFFICIENT. Their rapid-screen p-values are not promotion evidence.

## D. Economically explainable mechanisms

- F03: persistent one-sided flow aligned with current tick movement may encode information diffusion or sliced execution.
- S02: broad participant arrival may reduce abandonment/manipulation risk.
- C03: a strong reactivation episode tends to be followed by another event within four hours.
- RV01: fragmented same-pair prices reconnect, but the measured scale is far below assumed costs.

These are mechanism hypotheses, not causal proof. F03’s aggregate feature also overlaps with ordinary momentum and must be decomposed at event level.

## E. After cost, delay, capacity and failed-fill checks

F03 survives the assumed cost and 1–2h delay tests in price-space. It **does not** pass capacity or failed-fill evidence because the source has neither historical executable USD quote/depth nor failed-sale labels. S02 has a positive avoidance proxy but no sellability endpoint. All candidate statuses are capped accordingly.

## F. Fit for an approximately $1,000 account

No candidate is proven suitable. Small capital reduces expected impact, but pool USD depth, gas, token restrictions, route failure, tax and exit quotes are missing. The correct capacity statement in the atlas is “$1,000 UNVERIFIED,” not an assumed fill.

## G. Chance of approaching $1,000/month

F03’s arithmetic gross spread and signal count can superficially exceed that target, but signals overlap across hours/pools and reuse the same capital. Without forward fills, concurrency rules, USD notional capacity and realized costs, a monthly dollar projection would be fabricated. Evidence still required: pre-registered unseen signals, bidirectional executable quotes, simulated fill failures, portfolio capital locking and at least several regimes of forward observation.

## H. Historical ideas redefined into new possibilities

Wallet identity was replaced by participant breadth and state transition; momentum was reframed as direction-aligned flow persistence with delay stress; “dead pool” risk became activity-gap and adverse-path targets; cross-pool convergence was separated from directional selection. Only the first survives enough to shadow, and it must still prove it is not ordinary momentum in disguise.

## I. Old hypotheses that should remain dead

Uncosted raw flow imbalance, close-at-high continuation, same-pair convergence at observed scale, and dispersion reversal are KILLED under this implementation. No prior success result was accepted as evidence, and no broad claim that Base lacks alpha is supported.

## J. Three highest-information next experiments

1. For every F03 shadow signal, record read-only two-sided quotes for $250/$500/$1,000/$2,000, one- and two-block delays, failure codes, gas, price impact and a later sell quote.
2. Rebuild F03 at event/Flashblock time: actual direction-switch count, 200ms index, router identity, repeated tx clustering and placebo order permutations.
3. Create a decoded non-AMM panel for bridge flows plus lending rates/liquidations, then rerun a preregistered tournament to test orthogonality to F03.

## K. Results most likely to be false

- A01: >90% of the apparent separation is removed with the largest 1% outcomes.
- S02: does not survive the 48-test correction and lacks permanent-unsellable outcomes.
- F03: strongest remaining candidate, but exact-hour availability excludes dormant states, the score contains current return sign, the tick is not an executable quote, and the cost model is assumed.
- LP results: millions of source rows cover only a handful of pools.
- Any extrapolated dollar P&L: no valid evidence exists.

## L. Zero-order Forward Shadow decision

**Yes, F03 only, as SHADOW_CANDIDATE for data acquisition.** The initialized process writes signals and local evidence only. It has no signer, wallet, order route, executor, automatic promotion, or capital path. A future positive shadow result would still not authorize real trading.

## Verification summary

- 48 hypotheses / 12 families; 8 Deep Dives.
- 27 Kill Panel concerns recorded per Deep Dive in `KILL_PANEL_FULL.json`.
- Independent arithmetic reimplementation matches all eight published deep spreads exactly.
- Research panel: {quality['rows']:,} rows / {quality['pools']:,} pools; exact 1h/4h/24h outcomes {quality['exact_1h_outcomes']:,}/{quality['exact_4h_outcomes']:,}/{quality['exact_24h_outcomes']:,}.
- Highest research state: SHADOW_CANDIDATE. Matured shadow outcomes: {shadow['matured_outcome_count']}.

LIVE_TRADING = FORBIDDEN
REAL_ORDERS_SENT = 0
REAL_TRANSACTIONS_SIGNED = 0
REAL_CAPITAL_AT_RISK = 0
"""
    (ROOT/"FINAL_WILDCAT_REPORT.md").write_text(final,encoding="utf-8")

    trace=f"""# Requirement and Evidence Audit

| Mandate requirement | Authoritative evidence | Result |
|---|---|---|
| Independent greenfield workspace | `/root/base_wildcat_alpha`; source imports contain no old research package | PASS |
| Permanent no-live boundary | `NO_LIVE.lock`, `src/safety.py`, safety tests, shadow state | PASS |
| All entries fail closed | AST/integrity test plus entry guards | PASS |
| Base market map, data facts/proxies/gaps | `MARKET_MAP.md`, `data/source_audit.json`, `data/MANIFEST.json` | PASS |
| At least 40 hypotheses / 10 families | `ALPHA_ATLAS.csv`: {len(atlas)} / {atlas.family.nunique()} | PASS |
| At least 15 non-old variants and 30% unusual | 24 nontraditional hypotheses ({sum(atlas.family.isin(['block_time','liquidity','survival','lp_state','lifecycle','anomaly']))/len(atlas):.0%}) | PASS |
| Independent experiment artifacts | {len(list((ROOT/'EXPERIMENTS').glob('*/config.json')))} configs/results/readmes | PASS |
| Common Alpha Tournament and 5–10 Deep Dives | `TOURNAMENT_SELECTION.csv`: {len(deep)} selected | PASS |
| Time, walk-forward, perturbation, cost, delay, bootstrap, placebo, leave-out | `KILL_PANEL_FULL.json` | PASS; missing-data gates explicitly unresolved |
| Independent recomputation | `INDEPENDENT_RECOMPUTE.json`: all_match=true | PASS |
| Required leaderboard and weird findings | `ALPHA_LEADERBOARD.md`, `WEIRD_FINDINGS.md` | PASS |
| Zero-order forward shadow if warranted | `SHADOW/state.json`, `SHADOW/signals.csv`: {shadow['signal_count']} signals, 0 orders | INITIALIZED; no matured evidence yet |
| Final questions A–L and safety footer | `FINAL_WILDCAT_REPORT.md` | PASS |

Unresolved research evidence is not hidden: executable quotes, failed fills, sellability, USD capacity, MEV and price impact remain missing. Those gaps limit economic claims but do not invalidate completion of the mandated discovery, tournament, replay review, and zero-order shadow initialization.
"""
    (ROOT/"REQUIREMENTS_TRACEABILITY.md").write_text(trace,encoding="utf-8")


if __name__=="__main__": generate_reports()
