# Clanker Contract Source Dossier (Base v4)

```text
study = CLANKER_ALPHA_FEASIBILITY_STUDY_V01
chain_id = 8453
factory = 0xE85A59c628F7d27878ACeB4bf3b35733630083a9
verification_status = VERIFIED_MULTI_SOURCE
```

## Identity

| Field | Value |
|---|---|
| Name | Clanker |
| Version | `4` (`version` constant in `Clanker.sol`; README label v4.0) |
| Address | `0xE85A59c628F7d27878ACeB4bf3b35733630083a9` |
| Deployment tx | `UNKNOWN` |
| Deployment block (first code) | `31526699` via `eth_getCode` binary search |
| Runtime bytecode SHA256 | `365456b7fae5f3d0f95eb1500426505f8a8a4a412fce23008792e1b7ff8b0b5f` |
| Runtime bytecode keccak256 | `43acb1f309cc223c71169825b9ab9c0f703669c25156e2999102dcfbb1826bee` |
| Bytecode length | 12375 bytes |

## Evidence layers (independent)

1. **Official repo deployed-contracts list** — [clanker-devco/v4-contracts README](https://raw.githubusercontent.com/clanker-devco/v4-contracts/main/README.md) lists this exact Base mainnet v4.0 Clanker address.
2. **Explorer verified source** — Blockscout reports verified `Clanker` at the same address (`compiler 0.8.28+commit.7893614a`, source path `src/Clanker.sol`).
3. **On-chain receipt/log consistency** — Live `TokenCreated` logs from the factory decode cleanly with the official `IClanker` event ABI; `topic0` matches keccak of the canonical signature; indexed `tokenAddress` / `tokenAdmin` match topic slots.

Machine registry: `config/clanker_verified_registry.v0.1.json`.

## Launch event

```text
event = TokenCreated
signature = TokenCreated(address,address,address,string,string,string,string,string,int24,address,bytes32,address,address,address,uint256,address[])
topic0 = 0x9299d1d1a88d8e1abdc591ae7a167a6bc63a8f17d695804e9091ee33aa89fb67
```

ABI source commit: `e0ed708395fe31d10c427890c4c55d6752bfa302` (`src/interfaces/IClanker.sol`).

Indexed: `tokenAddress`, `tokenAdmin`.  
Non-indexed: `msgSender`, image/name/symbol/metadata/context, `startingTick`, `poolHook`, `poolId`, `pairedToken`, `locker`, `mevModule`, `extensionsSupply`, `extensions`.

## Applicable range

```text
from_block = 31526699
to_block = open (while code remains)
```

## Related contracts used for pricing (not launch emitters)

| Role | Address |
|---|---|
| Uniswap v4 PoolManager | `0x498581ff718922c3f8e6A244956aF099B2652b2b` |
| WETH | `0x4200000000000000000000000000000000000006` |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |

PoolManager `Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)` topic0:
`0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f`.

## UNKNOWN (kept closed)

- Factory deployment transaction hash
- All pre-v4 Clanker factories / event layouts on Base
- Any third-party “Clanker” addresses not in official README + verified explorer match

## Study universe decision

Alpha sample uses **only** this verified v4 factory + `TokenCreated` topic0. Pre-v4 volume is excluded rather than guessed.
