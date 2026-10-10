# Base Market Map

As-of: 2026-08-14 14:00 UTC. “Observed” means present in the local read-only event store; it does not mean the entire Base market is covered.

## Market structure

Base Mainnet is chain 8453 and exposes standard EVM read methods. Base currently documents 2-second blocks and 200ms Flashblocks; incremental preconfirmation ordering is therefore a distinct research clock that the local hourly panel does **not** capture ([Base connection parameters](https://docs.base.org/base-chain/quickstart/connecting-to-base), [Flashblocks FAQ](https://docs.base.org/base-chain/flashblocks/faq)). Base is an Ethereum rollup with sequencer, batcher and bridge-specific state transitions ([protocol overview](https://docs.base.org/base-chain/specs/protocol/overview), [standard bridges](https://docs.base.org/base-chain/specs/protocol/bridging/bridges)).

| Surface | What exists / can be observed | Local evidence | Research readiness |
|---|---|---:|---|
| V2-style AMMs | constant-product/stable-style pools, swaps, mint/burn | 59,295 registry rows; 24,750 observed pairs | swaps usable; reserves/quotes not reconstructed |
| V3/CL AMMs | ticks, active liquidity, fee tiers, LP ranges | 20,702 registry rows; 9,279 observed pools | swap state usable; LP event coverage severely narrow |
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
| V3 swaps | 13,098,470 | 10,256,628 unique events / 9,279 pools | 2025-10-01 00:16:01.000 → 2026-08-14 14:59:53.000 |
| V2 swaps | 1,728,962 | 1,715,843 unique events / 24,750 pairs | 2026-02-23 09:59:39.000 → 2026-08-14 14:59:49.000 |
| Raw EVM logs | 18,956,417 | 34,063 addresses / 12 topics | 2025-10-01 00:16:01.000 → 2026-08-14 14:59:53.000 |
| V3 mint / burn / collect | 1,133,303 / 2,909,485 / 7,719,230 | only 7 / 7 / 3 pools | ends 2026-05-20 |

Rapid screening used all 7,451 V3 pools observed from 2026-04-01 through the cutoff, producing 33,599 pool-hours. There is no outcome-selected or full-period top-N universe filter.

## Fact, proxy, and missingness boundary

- Facts: chain, event address, block/transaction/log identity, event timestamp, pool, ABI words, tick, active-liquidity word, sender/recipient fields as emitted, and removed flag.
- Validated decoding: signed tick uses the low 32 bits of the sign-extended ABI word; liquidity uses the low unsigned 128 bits; amount0 direction uses two’s-complement sign extension.
- Proxies: sender breadth is not unique human breadth; active liquidity is not USD depth; event row count is not volume; tick close is not an executable quote; block/log index is not Flashblock arrival time.
- Major gaps: USD reserve/depth snapshots, gas and actual route quotes, failed calls/fills, sellability, token tax/restrictions, traces, complete LP coverage, bridge/lending/perp/CEX/social data, and future zero-activity rows.
- Source duplication: V3 has 2,841,842 excess rows. Unique event keys are used for activity; duplication-pressure sensitivity is part of the Kill Panel.

## Cheapest high-information additions

1. Read-only `eth_call` quote/depth snapshots for F03 signals, including deliberate sell-quote and failure recording.
2. Flashblock payload/index capture to replace hourly block-order proxies.
3. Decode bridge plus lending liquidation/rate events into a second, genuinely orthogonal market panel.
