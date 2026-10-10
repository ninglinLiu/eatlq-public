# Base V3 Actor-Flow OOS Harness — CHARTER

Status: ACTIVE RESEARCH — DEPLOY FORBIDDEN  
Project: `/root/base_actor_flow_oos_v0`  
Role: **Base Uniswap V3 Actor-Flow Research Agent**  
Phase: `ACTOR_FLOW_OOS_V0`

## Core hypothesis (rescoped, not a WONTFIX reversal)

The old bans stay permanently closed:

- absolute PnL ranking / winrate top-N
- winner-only wallets
- EOA-only identity (forcing everything to EOA)
- trigger-price trailing as primary exit proof
- MFE / paper-peak targets as economic targets
- `top1_profit_share < 50%` as a hard gate on tiny windows
- router / `tx.from` / transfer recipient as subject without resolution
- N addresses = N independent people (Sybil inflation)

This project answers only the *narrow, historically untested* question:

> Can resolvable Base V3 economic-actor behavior, after controlling for pool structure,
> add incremental, post-latency, capturable information?

## Fixed venue

- Base chain
- Uniswap V3
- WETH new pools / new tokens
- Existing ClickHouse warehouse + archive RPC for sampled golden audit

**Forbidden:** venue rejection (suggest CEX/perps/other chains), unsealing sealed holdout for tuning, expand-search-after-failure.

## Capital constraint

- Initial capital ≈ 1,000 USD (frozen WETH at `eth_usd_freeze`)
- No compounding; monthly profits withdrawn
- Business target ≈ 1,000 USD/month withdrawable — does **not** relax statistical standards
- Position sizes: $10 / $25 / $50 / $100
- No leverage

## Four-layer actor model

1. Stable observable economic actor
2. Behavior type (diamond / whale / bot / … — non-exclusive traits)
3. PIT/OOS predictive value ("smart")
4. Post-latency, post-exit capturable ("copyable")

Labels must never skip layers.

## Actor resolution tiers

| Tier | Meaning |
|------|---------|
| A | Direct EOA; pay / receive / hold consistent |
| B | ERC-4337 `userOp.sender` smart account |
| C | Stable personal execution contract / proxy |
| D | Shared router / vault / CEX-hot / ambiguous → EXCLUDED |

Gate metric (replaces old `EOA mismatch ≤ 2%`):

> Sampled `resolved_economic_actor` error rate ≤ 2%, and separately report `unresolved_coverage`.

## Six arms

- `POOL_ONLY` — pool age / depth / price / flow structure only
- `DIAMOND` — matured retention + positive executable markout trait
- `WHALE_EVENT` — relative impact + absorption + independent continuation
- `SMART_ALPHA` — history decision alpha (matched, shrunk, clustered)
- `BOT_REGIME` — bot toxicity / sniper density / organic share filters
- `INDEPENDENT_CONSENSUS` — independent entity-cluster count (post-Sybil)

## Dual verdict vocabulary (only)

| Verdict | Meaning |
|---------|---------|
| `INFORMATION_EDGE_GO` | Identity adds incremental lift over `POOL_ONLY` under walk-forward + cluster bootstrap + LOO |
| `INFORMATION_EDGE_NO_GO` | No incremental identity information after pool controls |
| `COPY_EDGE_GO` | Also survives capacity, sellability, concurrency, paper-forward, and $1k fixed-capital monthly cashflow |
| `COPY_EDGE_NO_GO` | Information may exist but is not copyable under capital/exit constraints |
| `ACTOR_RESOLUTION_INSUFFICIENT` | Foundation gate failed — stop before scoring arms |
| `POOL_STRUCTURE_SUFFICES` | Best identity arm ≈ `POOL_ONLY` → drop identity engineering |
| `INSUFFICIENT_INDEPENDENT_EVENTS` | Sample / breadth too thin for powered claim |
| `PROSPECTIVE_CONFIRMATION_REQUIRED` | Positive OOF — enter shadow only |
| `DATA_LINEAGE_INSUFFICIENT` | Required data missing |

`deploy_allowed` is always `false`.

## Hard bans (inputs)

- KEYSTONE / F10 / G2 / C3 / P2 / H1–H9 scores, passes, thresholds, ensembles
- Old abs-PnL / winrate wallet rankings as features
- Trigger-price trailing as primary economic proof
- MFE≥5x as primary target
- Expanding search space after failure to force a winner

## Allowed reuse (adapted copy only — no cross-project imports)

See `REUSE_MANIFEST.json`.

## Walk-forward

- 42–60d train / one full outcome-horizon embargo / 7–14d test / rolling folds
- Final 20% calendar time sealed once
- Entry = `signal_observed_at + measured_latency`, replayed at own size via atomic sim
- Target = net executable return (30m/4h/24h + frozen strong-TP ladder), all costs charged

## Honest closeout

If foundation gate fails or all arms fail: emit the honest verdict and stop.
Do not launch further rounds solely to fish a positive OOF.
