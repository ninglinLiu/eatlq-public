# EATLQ Phase 2 Registry Design

## Goal

Add a minimal static registry layer on top of the current curated allowlist so normalized event tables can be joined with stable token and pool metadata.

This is intentionally a Phase 2 data-availability primitive, not a dynamic discovery system.

## Why This Comes Now

The pipeline now has verified:

- stable realtime ingestion
- raw archival
- replay and gap scheduling
- normalized transfer, swap, mint, and burn tables

After those event families are trustworthy, the next low-risk improvement is to add stable metadata anchors for the same curated contracts.

## Scope

The first registry cut stays deliberately small:

- one static token registry
- one static Uniswap V2 pair registry
- one static Uniswap V3 pool registry
- one unified pool registry view
- one enriched query-view layer built by joining deduped normalized rows to the registry
- one candidate-ranking layer that summarizes uncovered addresses across event families

The source of truth is the same curated contract set already present in `docs/logs-filter-profile.md`.

## ClickHouse Objects

Registry views:

- `eatlq.token_registry`
- `eatlq.uniswap_v2_pair_registry`
- `eatlq.uniswap_v3_pool_registry`
- `eatlq.pool_registry`

Enriched query views:

- `eatlq.erc20_transfers_enriched`
- `eatlq.uniswap_v2_swaps_enriched`
- `eatlq.uniswap_v2_mints_enriched`
- `eatlq.uniswap_v2_burns_enriched`
- `eatlq.uniswap_v3_swaps_enriched`
- `eatlq.uniswap_v3_mints_enriched`
- `eatlq.uniswap_v3_burns_enriched`

Gap and candidate views:

- `eatlq.registry_gap_summary`
- `eatlq.registry_candidate_summary`

These are implemented as static ClickHouse views rather than mutable tables.

Why:

- they are tiny
- they are version-controlled in SQL
- reapplying init SQL on an existing VPS stays simple
- they avoid duplicate inserts from repeated `apply-clickhouse-init.sh` runs

## Current Metadata

### Token Registry

Current columns:

- `chain_id`
- `token_address`
- `symbol`
- `name`
- `decimals`
- `is_core`
- `registry_source`

Current token set:

- WETH
- USDC
- USDT
- WBTC
- DAI
- DOGGO
- SHISHI
- BMBG
- ADI
- MOCA
- 01001000
- BLIN
- FLOKI
- pALVA
- pfUSDC-22
- USDe
- USDM1
- SMEL
- ERBB
- EIGEN
- 99BTC
- RSR
- BDX
- EURC
- wTAO
- BITCOIN
- XOR
- BIFI
- Q
- REACT
- XAUt

### Uniswap V2 Pair Registry

Current columns:

- `chain_id`
- `pair_address`
- `pair_label`
- `token0_address`
- `token1_address`
- `token0_symbol`
- `token1_symbol`
- `fee_bps`
- `is_curated`
- `registry_source`

Current pair set:

- USDC/WETH
- USDT/WETH
- WBTC/WETH
- DAI/WETH
- DOGGO/WETH
- SHISHI/WETH
- 01001000/WETH
- MOCA/USDT
- BLIN/WETH
- FLOKI/WETH
- pALVA/pfUSDC-22

### Uniswap V3 Pool Registry

Current columns:

- `chain_id`
- `pool_address`
- `pool_label`
- `token0_address`
- `token1_address`
- `token0_symbol`
- `token1_symbol`
- `fee_bps`
- `is_curated`
- `registry_source`

Current pool set:

- USDC/WETH 0.05%
- USDC/WETH 0.30%
- USDT/WETH 0.30%
- WBTC/WETH 0.30%
- DAI/WETH 0.30%
- USDC/WETH 1.00%
- USDT/WETH 1.00%
- BMBG/USDT 0.30%
- ADI/USDC 0.05%
- USDe/USDC 1.00%
- USDM1/USDC 1.00%
- WETH/SMEL 0.30%
- ERBB/USDT 1.00%
- USDC/EIGEN 1.00%
- WBTC/WETH 0.05%
- WBTC/USDT 0.05%
- WETH/99BTC 0.30%
- RSR/WETH 0.30%
- BDX/WETH 0.30%
- EURC/USDC 0.05%
- wTAO/WETH 1.00%
- BITCOIN/WETH 0.30%
- XOR/WETH 1.00% @0x7f63
- BIFI/WETH 1.00%
- Q/WETH 1.00%
- WBTC/USDT 0.05% @0x5653
- REACT/WETH 1.00%
- XOR/WETH 1.00% @0x1548
- XOR/WETH 1.00% @0x9ebb
- XOR/WETH 0.30%
- XAUt/USDT 0.05%

## Operational Note

For an already-initialized VPS volume, apply the new registry DDL with:

```bash
bash scripts/apply-clickhouse-init.sh
```

Inspect the registry contents with:

```bash
bash scripts/inspect-registry.sh
```

Check whether the current normalized tables are fully covered by the static registry with:

```bash
bash scripts/verify-registry-coverage.sh
```

Inspect uncovered addresses and venues with:

```bash
bash scripts/inspect-registry-gaps.sh
```

Inspect ranked registry-expansion candidates with:

```bash
bash scripts/inspect-registry-candidates.sh
```

Inspect the enriched query surface with:

```bash
bash scripts/inspect-enriched-events.sh
```

## Current Limits

- this is a static curated registry, not onchain discovery
- registry coverage only matches the current allowlist, not the full historical chain
- enriched query views add metadata joins, but the underlying normalized tables remain raw-address first
- no automatic refresh exists when the allowlist expands
- no token logo, protocol metadata, or semantic asset class layer exists yet
- candidate ranking is still frequency-first, so low-quality meme assets can surface near the top and require manual curation
- promotion is intentionally conservative: only venues whose token metadata can be read cleanly onchain are added batch by batch
- the registry now has a separate lifecycle-focused expansion path, where venues are promoted specifically because they appear in `mint` / `burn` gaps, not only because they rank highly in swaps
- pools that appear in both `v3_mints` and `v3_burns` are especially valuable because one promotion can improve two lifecycle surfaces at once
- after the obvious mint/burn overlap pools are promoted, the next batches should continue from the remaining overlap set before falling back to burn-only or mint-only tails
- when multiple V3-compatible pools share the same token pair and fee, labels may carry a short address suffix so operators can distinguish distinct venues safely

## Next Increment

After candidate ranking and the first manual promotions are in place, the next practical improvements are:

1. apply and verify the single-surface lifecycle tail batch on the VPS
2. confirm coverage improvement for the newly promoted venues and tokens
3. confirm whether lifecycle coverage is effectively complete before returning to broader ranked candidates
4. only then consider semi-static refresh jobs or dynamic onchain discovery
