DROP VIEW IF EXISTS eatlq.pool_registry;
DROP VIEW IF EXISTS eatlq.uniswap_v3_pool_registry;
DROP VIEW IF EXISTS eatlq.uniswap_v2_pair_registry;
DROP VIEW IF EXISTS eatlq.token_registry;

CREATE VIEW eatlq.token_registry
AS
SELECT
    toUInt64(1) AS chain_id,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token_address,
    'WETH' AS symbol,
    'Wrapped Ether' AS name,
    toUInt8(18) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token_address,
    'USDC' AS symbol,
    'USD Coin' AS name,
    toUInt8(6) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token_address,
    'USDT' AS symbol,
    'Tether USD' AS name,
    toUInt8(6) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token_address,
    'WBTC' AS symbol,
    'Wrapped BTC' AS name,
    toUInt8(8) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x6b175474e89094c44da98b954eedeac495271d0f' AS token_address,
    'DAI' AS symbol,
    'Dai Stablecoin' AS name,
    toUInt8(18) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xb68d578815f29e9ac074154274866f08038e995d' AS token_address,
    'DOGGO' AS symbol,
    'Ethereum Dog Mascot' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8ce4e0080f540944df4fcdb42d40c4e2860955fa' AS token_address,
    'SHISHI' AS symbol,
    'SHISHI' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x45e02bc2875a2914c4f585bbf92a6f28bc07cb70' AS token_address,
    'BMBG' AS symbol,
    'BMBG Token' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8b1484d57abbe239bb280661377363b03c89caea' AS token_address,
    'ADI' AS symbol,
    'ADI' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xf944e35f95e819e752f3ccb5faf40957d311e8c5' AS token_address,
    'MOCA' AS symbol,
    'Moca' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8616c8b6ced1c846d050b0c9a8e1e8965b893a8a' AS token_address,
    '01001000' AS symbol,
    'Binary Code' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xaefb54306240502c5421be478fa16aacfa9698a2' AS token_address,
    'BLIN' AS symbol,
    'BLIN' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xcf0c122c6b73ff809c693db761e7baebe62b6a2e' AS token_address,
    'FLOKI' AS symbol,
    'FLOKI' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8908e07701ca7d2d8fbf4d95706b484464f01959' AS token_address,
    'pALVA' AS symbol,
    'Alvara LVF Pod' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8f135bcfc4084ce6c8620e87b12c4ee82d165276' AS token_address,
    'pfUSDC-22' AS symbol,
    'Peapods Interest Bearing USDC - 22' AS name,
    toUInt8(6) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x4c9edd5852cd905f086c759e8383e09bff1e68b3' AS token_address,
    'USDe' AS symbol,
    'USDe' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x90a1717e0dabe37693f79afe43ae236dc3b65957' AS token_address,
    'USDM1' AS symbol,
    'USDM1' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xf91384484f4717314798e8975bcd904a35fc2bf1' AS token_address,
    'SMEL' AS symbol,
    'Imagine The SMEL' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x5702a4487da07c827cde512e2d5969cb430cd839' AS token_address,
    'ERBB' AS symbol,
    'Exchange Request for Bitbon' AS name,
    toUInt8(27) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xec53bf9167f50cdeb3ae105f56099aaab9061f83' AS token_address,
    'EIGEN' AS symbol,
    'Eigen' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xc2eb40516ecaac04ae9964934983d1e9ebdf51fd' AS token_address,
    '99BTC' AS symbol,
    '99 Bitcoins' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x320623b8e4ff03373931769a31fc52a4e78b5d70' AS token_address,
    'RSR' AS symbol,
    'Reserve Rights' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x6ad12e761b438bea3ea09f6c6266556bb24c2181' AS token_address,
    'BDX' AS symbol,
    'BELDEX' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x1abaea1f7c830bd89acc67ec4af516284b1bc33c' AS token_address,
    'EURC' AS symbol,
    'Euro Coin' AS name,
    toUInt8(6) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x77e06c9eccf2e797fd462a92b6d7642ef85b0a44' AS token_address,
    'wTAO' AS symbol,
    'Wrapped TAO' AS name,
    toUInt8(9) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x72e4f9f808c49a2a61de9c5896298920dc4eeea9' AS token_address,
    'BITCOIN' AS symbol,
    'HarryPotterObamaSonic10Inu' AS name,
    toUInt8(8) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x40fd72257597aa14c7231a7b1aaa29fce868f677' AS token_address,
    'XOR' AS symbol,
    'Sora Token' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x5870700f1272a1adbb87c3140bd770880a95e55d' AS token_address,
    'BIFI' AS symbol,
    'beefy.finance' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xaca40632c51c2a03209d2714b88aa0f1456a2101' AS token_address,
    'Q' AS symbol,
    'Q Token' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x817162975186d4d53dbf5a7377dd45376e2d2fc5' AS token_address,
    'REACT' AS symbol,
    'Wrapped REACT' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x68749665ff8d2d112fa859aa293f07a622782f38' AS token_address,
    'XAUt' AS symbol,
    'Tether Gold' AS name,
    toUInt8(6) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
-- ── Base L2 (chain_id = 8453) ─────────────────────────────────────────────
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x4200000000000000000000000000000000000006' AS token_address,
    'WETH' AS symbol,
    'Wrapped Ether' AS name,
    toUInt8(18) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' AS token_address,
    'USDC' AS symbol,
    'USD Coin' AS name,
    toUInt8(6) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca' AS token_address,
    'USDbC' AS symbol,
    'USD Base Coin' AS name,
    toUInt8(6) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x2ae3f1ec7f1f5012cfeab0185bfcb5aa7ac6e90f' AS token_address,
    'cbETH' AS symbol,
    'Coinbase Wrapped Staked ETH' AS name,
    toUInt8(18) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token_address,
    'AERO' AS symbol,
    'Aerodrome Finance' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x4ed4e862860be2ec1f4896764335d5942baadf07' AS token_address,
    'DEGEN' AS symbol,
    'Degen' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x50c5725949a6f0c72e6c4a641f24049a917db0cb' AS token_address,
    'DAI' AS symbol,
    'Dai Stablecoin' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452' AS token_address,
    'wstETH' AS symbol,
    'Wrapped liquid staked Ether 2.0' AS name,
    toUInt8(18) AS decimals,
    toUInt8(0) AS is_core,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf' AS token_address,
    'cbBTC' AS symbol,
    'Coinbase Wrapped BTC' AS name,
    toUInt8(8) AS decimals,
    toUInt8(1) AS is_core,
    'static_curated_allowlist' AS registry_source;

CREATE VIEW eatlq.uniswap_v2_pair_registry
AS
SELECT
    toUInt64(1) AS chain_id,
    '0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc' AS pair_address,
    'USDC/WETH' AS pair_label,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'USDC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852' AS pair_address,
    'USDT/WETH' AS pair_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'WETH' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xbb2b8038a1640196fbe3e38816f3e67cba72d940' AS pair_address,
    'WBTC/WETH' AS pair_label,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'WBTC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xa478c2975ab1ea89e8196811f51a7b7ade33eb11' AS pair_address,
    'DAI/WETH' AS pair_label,
    '0x6b175474e89094c44da98b954eedeac495271d0f' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'DAI' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xa40907f1a6949c29996a09cc1ac1ac0872070c32' AS pair_address,
    'DOGGO/WETH' AS pair_label,
    '0xb68d578815f29e9ac074154274866f08038e995d' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'DOGGO' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x09718a7260b1a8d694f381dcf908890ecb318f2d' AS pair_address,
    'SHISHI/WETH' AS pair_label,
    '0x8ce4e0080f540944df4fcdb42d40c4e2860955fa' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'SHISHI' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x878723b7e12183853c8d41ab9f728c277226bb06' AS pair_address,
    '01001000/WETH' AS pair_label,
    '0x8616c8b6ced1c846d050b0c9a8e1e8965b893a8a' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    '01001000' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x0b07188b12e3bba6a680e553e23c4079e98a034b' AS pair_address,
    'MOCA/USDT' AS pair_label,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token0_address,
    '0xf944e35f95e819e752f3ccb5faf40957d311e8c5' AS token1_address,
    'USDT' AS token0_symbol,
    'MOCA' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x5cac7cb341b089e15abcfc17bba5dff50f37ae12' AS pair_address,
    'BLIN/WETH' AS pair_label,
    '0xaefb54306240502c5421be478fa16aacfa9698a2' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'BLIN' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xca7c2771d248dcbe09eabe0ce57a62e18da178c0' AS pair_address,
    'FLOKI/WETH' AS pair_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xcf0c122c6b73ff809c693db761e7baebe62b6a2e' AS token1_address,
    'WETH' AS token0_symbol,
    'FLOKI' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8acc975dc4e9f3fc989cd2f10ef15e8caaa7e97e' AS pair_address,
    'pALVA/pfUSDC-22' AS pair_label,
    '0x8908e07701ca7d2d8fbf4d95706b484464f01959' AS token0_address,
    '0x8f135bcfc4084ce6c8620e87b12c4ee82d165276' AS token1_address,
    'pALVA' AS token0_symbol,
    'pfUSDC-22' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
-- ── Base L2 (chain_id = 8453) Aerodrome vAMM pools ───────────────────────────
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d' AS pair_address,
    'AERO/USDC [Aerodrome vAMM]' AS pair_label,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token0_address,
    '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' AS token1_address,
    'AERO' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6' AS pair_address,
    'AERO/WETH [Aerodrome vAMM]' AS pair_label,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token0_address,
    '0x4200000000000000000000000000000000000006' AS token1_address,
    'AERO' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source;

CREATE VIEW eatlq.uniswap_v3_pool_registry
AS
SELECT
    toUInt64(1) AS chain_id,
    '0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640' AS pool_address,
    'USDC/WETH 0.05%' AS pool_label,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'USDC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8' AS pool_address,
    'USDC/WETH 0.30%' AS pool_label,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'USDC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x11b815efb8f581194ae79006d24e0d814b7697f6' AS pool_address,
    'USDT/WETH 0.30%' AS pool_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'WETH' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xcbcdf9626bc03e24f779434178a73a0b4bad62ed' AS pool_address,
    'WBTC/WETH 0.30%' AS pool_label,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'WBTC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xc2e9f25be6257c210d7adf0d4cd6e3e881ba25f8' AS pool_address,
    'DAI/WETH 0.30%' AS pool_label,
    '0x6b175474e89094c44da98b954eedeac495271d0f' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'DAI' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xe0554a476a092703abdb3ef35c80e0d76d32939f' AS pool_address,
    'USDC/WETH 1.00%' AS pool_label,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'USDC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xc7bbec68d12a0d1830360f8ec58fa599ba1b0e9b' AS pool_address,
    'USDT/WETH 1.00%' AS pool_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'WETH' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x53ead11073fc0651dce70572666f0ed0752abfea' AS pool_address,
    'BMBG/USDT 0.30%' AS pool_label,
    '0x45e02bc2875a2914c4f585bbf92a6f28bc07cb70' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'BMBG' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x41a561b972039a9ccb975a4c17b84c9f95099e15' AS pool_address,
    'ADI/USDC 0.05%' AS pool_label,
    '0x8b1484d57abbe239bb280661377363b03c89caea' AS token0_address,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token1_address,
    'ADI' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xe6d7ebb9f1a9519dc06d557e03c522d53520e76a' AS pool_address,
    'USDe/USDC 1.00%' AS pool_label,
    '0x4c9edd5852cd905f086c759e8383e09bff1e68b3' AS token0_address,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token1_address,
    'USDe' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x6f161ad0e297ecb9d1b33c048272ccc964cb4b6a' AS pool_address,
    'USDM1/USDC 1.00%' AS pool_label,
    '0x90a1717e0dabe37693f79afe43ae236dc3b65957' AS token0_address,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token1_address,
    'USDM1' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xb76726b4befe761a1859c1c02e7d157142e077c0' AS pool_address,
    'WETH/SMEL 0.30%' AS pool_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xf91384484f4717314798e8975bcd904a35fc2bf1' AS token1_address,
    'WETH' AS token0_symbol,
    'SMEL' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x8d7625fcf354c0961ac51fc121b9dd1708d6b226' AS pool_address,
    'ERBB/USDT 1.00%' AS pool_label,
    '0x5702a4487da07c827cde512e2d5969cb430cd839' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'ERBB' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0xd640333b71b015092d9b3afcff3e427036304370' AS pool_address,
    'USDC/EIGEN 1.00%' AS pool_label,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token0_address,
    '0xec53bf9167f50cdeb3ae105f56099aaab9061f83' AS token1_address,
    'USDC' AS token0_symbol,
    'EIGEN' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x4585fe77225b41b697c938b018e2ac67ac5a20c0' AS pool_address,
    'WBTC/WETH 0.05%' AS pool_label,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'WBTC' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x40ab23e8f571bf19a85605b9638e50cc25a256ec' AS pool_address,
    'WBTC/USDT 0.05%' AS pool_label,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'WBTC' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x9d52d8a9f2fbf430fd634a21267b4414049c0064' AS pool_address,
    'WETH/99BTC 0.30%' AS pool_label,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token0_address,
    '0xc2eb40516ecaac04ae9964934983d1e9ebdf51fd' AS token1_address,
    'WETH' AS token0_symbol,
    '99BTC' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x32d9259e6792b2150fd50395d971864647fa27b2' AS pool_address,
    'RSR/WETH 0.30%' AS pool_label,
    '0x320623b8e4ff03373931769a31fc52a4e78b5d70' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'RSR' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x41e9f7cf65fe0c3c71a077e49023ba3c4f4f65b3' AS pool_address,
    'BDX/WETH 0.30%' AS pool_label,
    '0x6ad12e761b438bea3ea09f6c6266556bb24c2181' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'BDX' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x95dbb3c7546f22bce375900abfdd64a4e5bd73d6' AS pool_address,
    'EURC/USDC 0.05%' AS pool_label,
    '0x1abaea1f7c830bd89acc67ec4af516284b1bc33c' AS token0_address,
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48' AS token1_address,
    'EURC' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x433a00819c771b33fa7223a5b3499b24fbcd1bbc' AS pool_address,
    'wTAO/WETH 1.00%' AS pool_label,
    '0x77e06c9eccf2e797fd462a92b6d7642ef85b0a44' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'wTAO' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x25392d7129040710f152174af5019004a6f9b18d' AS pool_address,
    'BITCOIN/WETH 0.30%' AS pool_label,
    '0x72e4f9f808c49a2a61de9c5896298920dc4eeea9' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'BITCOIN' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x7f63306a62c345365881e0fff85cb2c8baaa13d5' AS pool_address,
    'XOR/WETH 1.00% @0x7f63' AS pool_label,
    '0x40fd72257597aa14c7231a7b1aaa29fce868f677' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'XOR' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x9a99748cf3a6b21207bcc9aeebafbd734bfdaba8' AS pool_address,
    'BIFI/WETH 1.00%' AS pool_label,
    '0x5870700f1272a1adbb87c3140bd770880a95e55d' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'BIFI' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x1a5a974874ba51a7b38e30f63449495c58b243e7' AS pool_address,
    'Q/WETH 1.00%' AS pool_label,
    '0xaca40632c51c2a03209d2714b88aa0f1456a2101' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'Q' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x56534741cd8b152df6d48adf7ac51f75169a83b2' AS pool_address,
    'WBTC/USDT 0.05% @0x5653' AS pool_label,
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'WBTC' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x46b63703fa267bfcdb9ce66a1c5216e94d840fbf' AS pool_address,
    'REACT/WETH 1.00%' AS pool_label,
    '0x817162975186d4d53dbf5a7377dd45376e2d2fc5' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'REACT' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x15483ab0d7351509ff5236c617004903e67a386d' AS pool_address,
    'XOR/WETH 1.00% @0x1548' AS pool_label,
    '0x40fd72257597aa14c7231a7b1aaa29fce868f677' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'XOR' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x9ebb79b1759eaa95ad9c132c3a394e2dd792120d' AS pool_address,
    'XOR/WETH 1.00% @0x9ebb' AS pool_label,
    '0x40fd72257597aa14c7231a7b1aaa29fce868f677' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'XOR' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x94391eecd76d9cb35013d7f263b64921e31842c8' AS pool_address,
    'XOR/WETH 0.30%' AS pool_label,
    '0x40fd72257597aa14c7231a7b1aaa29fce868f677' AS token0_address,
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2' AS token1_address,
    'XOR' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(1) AS chain_id,
    '0x6546055f46e866a4b9a4a13e81273e3152bae5da' AS pool_address,
    'XAUt/USDT 0.05%' AS pool_label,
    '0x68749665ff8d2d112fa859aa293f07a622782f38' AS token0_address,
    '0xdac17f958d2ee523a2206206994597c13d831ec7' AS token1_address,
    'XAUt' AS token0_symbol,
    'USDT' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
-- ── Base L2 (chain_id = 8453) Uniswap V3 pools ───────────────────────────
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xd0b53d9277642d899df5c87a3966a349a798f224' AS pool_address,
    'WETH/USDC 0.05%' AS pool_label,
    '0x4200000000000000000000000000000000000006' AS token0_address,
    '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' AS token1_address,
    'WETH' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x4c36388be6f416a29c8d8eee81c771ce6be14b18' AS pool_address,
    'WETH/USDbC 0.05%' AS pool_label,
    '0x4200000000000000000000000000000000000006' AS token0_address,
    '0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca' AS token1_address,
    'WETH' AS token0_symbol,
    'USDbC' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x10648ba41b8565907cfa1496765fa4d95390aa0d' AS pool_address,
    'cbETH/WETH 0.05%' AS pool_label,
    '0x2ae3f1ec7f1f5012cfeab0185bfcb5aa7ac6e90f' AS token0_address,
    '0x4200000000000000000000000000000000000006' AS token1_address,
    'cbETH' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xb4cb800910b228ed3d0834cf79d697127bbb00e5' AS pool_address,
    'WETH/USDC 0.30%' AS pool_label,
    '0x4200000000000000000000000000000000000006' AS token0_address,
    '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' AS token1_address,
    'WETH' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(30) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xfcd08d68b2313cd5e1d9e0e3f3183ad7cfdbf66f' AS pool_address,
    'WETH/DAI 0.05%' AS pool_label,
    '0x4200000000000000000000000000000000000006' AS token0_address,
    '0x50c5725949a6f0c72e6c4a641f24049a917db0cb' AS token1_address,
    'WETH' AS token0_symbol,
    'DAI' AS token1_symbol,
    toUInt16(5) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
-- ── Base L2 (chain_id = 8453) Aerodrome CL pools ───────────────────────────
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0x82321f3beb69f503380d6b233857d5c43562e2d0' AS pool_address,
    'AERO/WETH [Aerodrome CL]' AS pool_label,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token0_address,
    '0x4200000000000000000000000000000000000006' AS token1_address,
    'AERO' AS token0_symbol,
    'WETH' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xdfe5f275020def30993f042174fc2d335678b626' AS pool_address,
    'AERO/cbBTC [Aerodrome CL]' AS pool_label,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token0_address,
    '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf' AS token1_address,
    'AERO' AS token0_symbol,
    'cbBTC' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source
UNION ALL
SELECT
    toUInt64(8453) AS chain_id,
    '0xbe00ff35af70e8415d0eb605a286d8a45466a4c1' AS pool_address,
    'AERO/USDC [Aerodrome CL]' AS pool_label,
    '0x940181a94a35a4569e4529a3cdfb74e38fd98631' AS token0_address,
    '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' AS token1_address,
    'AERO' AS token0_symbol,
    'USDC' AS token1_symbol,
    toUInt16(100) AS fee_bps,
    toUInt8(1) AS is_curated,
    'static_curated_allowlist' AS registry_source;

CREATE VIEW eatlq.pool_registry
AS
SELECT
    chain_id,
    'uniswap_v2' AS protocol_family,
    'pair' AS venue_type,
    pair_address AS venue_address,
    pair_label AS venue_label,
    token0_address,
    token1_address,
    token0_symbol,
    token1_symbol,
    fee_bps,
    is_curated,
    registry_source
FROM eatlq.uniswap_v2_pair_registry
UNION ALL
SELECT
    chain_id,
    'uniswap_v3' AS protocol_family,
    'pool' AS venue_type,
    pool_address AS venue_address,
    pool_label AS venue_label,
    token0_address,
    token1_address,
    token0_symbol,
    token1_symbol,
    fee_bps,
    is_curated,
    registry_source
FROM eatlq.uniswap_v3_pool_registry;
