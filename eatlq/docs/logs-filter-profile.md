# EATLQ Logs Filter Profile

## Goal

Keep the current `newHeads + eth_getLogs` runtime stable by narrowing the HTTP log filter to a practical subset that still captures real trading activity:

- Uniswap V2 `Swap`
- Uniswap V2 `Mint`
- Uniswap V2 `Burn`
- Uniswap V3 `Swap`
- Uniswap V3 `Mint`
- Uniswap V3 `Burn`
- ERC-20 `Transfer`

This profile is intentionally biased toward:

- low enough volume for a single QuickNode setup
- high signal-to-noise ratio for Phase 1
- immediate usefulness for ClickHouse archival and later strategy work

## Official References

- QuickNode `eth_subscribe`: [https://www.quicknode.com/docs/flow/eth_subscribe](https://www.quicknode.com/docs/flow/eth_subscribe)
- Ethereum log filter object (`address`, `topics`): [https://github.com/tunnckocore/ethereum-jsonrpc-api-docs/blob/master/ethereum-jsonrpc-api.md](https://github.com/tunnckocore/ethereum-jsonrpc-api-docs/blob/master/ethereum-jsonrpc-api.md)
- OpenZeppelin ERC-20 `Transfer` event definition: [IERC20.sol](https://github.com/OpenZeppelin/openzeppelin-contracts/blob/master/contracts/token/ERC20/IERC20.sol)
- Uniswap V2 `Swap` event definition: [IUniswapV2Pair.sol](https://github.com/Uniswap/v2-core/blob/master/contracts/interfaces/IUniswapV2Pair.sol)
- Uniswap V2 pair contract reference: [Pair](https://docs.uniswap.org/contracts/v2/reference/smart-contracts/pair)
- Uniswap V3 `Swap` event definition: [IUniswapV3PoolEvents.sol](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)
- Uniswap V3 pool events reference: [IUniswapV3PoolEvents](https://docs.uniswap.org/contracts/v3/reference/core/interfaces/pool/IUniswapV3PoolEvents)

## Event Topics

These are the keccak256 event signature hashes used in `topics[0]`.

- ERC-20 `Transfer(address,address,uint256)`
- `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`
- Uniswap V2 `Swap(address,uint256,uint256,uint256,uint256,address)`
  - `0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822`
- Uniswap V2 `Mint(address,uint256,uint256)`
  - `0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f`
- Uniswap V2 `Burn(address,uint256,uint256,address)`
  - `0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496`
- Uniswap V3 `Swap(address,address,int256,int256,uint160,uint128,int24)`
  - `0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67`
- Uniswap V3 `Mint(address,address,int24,int24,uint128,uint256,uint256)`
  - `0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde`
- Uniswap V3 `Burn(address,int24,int24,uint128,uint256,uint256)`
  - `0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c`

## Starter Mainnet Address Set

### Core ERC-20 tokens

- WETH: `0xC02aaA39B223FE8D0A0E5C4F27eAD9083C756Cc2`
- USDC: `0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48`
- USDT: `0xdAC17F958D2ee523a2206206994597C13D831ec7`
- WBTC: `0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599`
- DAI: `0x6B175474E89094C44Da98b954EedeAC495271d0F`

### Uniswap V2 pairs

- USDC/WETH: `0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc`
- USDT/WETH: `0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852`
- WBTC/WETH: `0xBb2b8038a1640196FbE3e38816F3e67Cba72D940`
- DAI/WETH: `0xA478c2975Ab1Ea89e8196811F51A7B7Ade33eB11`

### Uniswap V3 pools

- USDC/WETH 0.05%: `0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640`
- USDC/WETH 0.30%: `0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8`
- USDT/WETH 0.30%: `0x11b815efB8f581194ae79006d24E0d814B7697F6`
- WBTC/WETH 0.30%: `0xCBCdF9626bc03E24f779434178A73a0B4Bad62eD`
- DAI/WETH 0.30%: `0xC2e9F25be6257c210d7ADf0d4cd6e3e881ba25f8`

## Recommended Phase 1 Filter

Keep the websocket side on heads only, and put this exact filter line into `infra/compose/.env`:

```dotenv
WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]
LOG_FILTER_JSON={"address":["0xC02aaA39B223FE8D0A0E5C4F27eAD9083C756Cc2","0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48","0xdAC17F958D2ee523a2206206994597C13D831ec7","0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599","0x6B175474E89094C44Da98b954EedeAC495271d0F","0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc","0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852","0xBb2b8038a1640196FbE3e38816F3e67Cba72D940","0xA478c2975Ab1Ea89e8196811F51A7B7Ade33eB11","0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640","0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8","0x11b815efB8f581194ae79006d24E0d814B7697F6","0xCBCdF9626bc03E24f779434178A73a0B4Bad62eD","0xC2e9F25be6257c210d7ADf0d4cd6e3e881ba25f8"],"topics":[["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef","0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822","0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67","0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f","0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496","0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde","0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"]]}
```

## Why This Profile Works

- It keeps the HTTP pull filter on a bounded set of high-liquidity contracts instead of the entire chain.
- It still captures the most useful real-time actions for Phase 1:
  - token transfers of core assets
- V2 pair swaps plus liquidity adds/removes
- V3 pool swaps plus liquidity adds/removes
- It aligns with the whitepaper's "signal over noise" principle better than `LOG_FILTER_JSON={}`.
- The address allowlist stays unchanged, so adding mint/burn widens event families without turning the filter into a chain-wide firehose.

## Important Caveat

This is not a full "all Uniswap swaps on mainnet" feed.

It is a curated starter profile.

Why:

- A full-chain `logs` firehose is too heavy for a single normal websocket endpoint.
- Even "all Uniswap pools" is too large unless you maintain a dynamic pool registry.

For Phase 1, the right trade-off is:

- start with a narrow, stable, high-value contract set
- confirm end-to-end stability
- expand gradually

## Expected Runtime Behavior

After switching to this filter and rebuilding `ingestor`, you should see:

- `ingestor` logs stop showing frequent `upstream went away`
- `Redis XINFO STREAM market_ticks` starts increasing again
- `archiver` resumes writing to ClickHouse
- `SELECT count() FROM eatlq.raw_evm_logs` starts increasing again

## Suggested Rollout Command

```bash
cd /opt/EATLQ
```

Edit `infra/compose/.env`, replace `LOG_FILTER_JSON`, and confirm `WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]`, then:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml up -d --build ingestor
```

Then verify:

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=60s ingestor
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse \
  clickhouse-client --query "SELECT count(), max(received_at_ms) FROM eatlq.raw_evm_logs"
```

## Future Expansion Options

When Phase 1 is stable, expand in this order:

1. Add more core token contracts
2. Add more known high-liquidity Uniswap pools
3. Split subscriptions by domain:
   - one ingestor for ERC-20 transfers
   - one ingestor for V2 swaps
   - one ingestor for V3 swaps
4. Build a pool registry and dynamically refresh the address allowlist

## Non-Goals For Now

These are intentionally deferred:

- full-chain `logs {}` firehose
- all DEX protocols
- all token transfers on Ethereum
- all Uniswap pools discovered dynamically
- pending message recovery and `XAUTOCLAIM`
