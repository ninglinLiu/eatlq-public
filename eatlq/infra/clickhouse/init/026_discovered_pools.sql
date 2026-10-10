CREATE TABLE IF NOT EXISTS eatlq.discovered_pools (
    chain_id UInt64,
    pool_address String,
    factory_address String,
    pool_type String,
    token0_address String,
    token0_symbol String,
    token0_decimals UInt8,
    token1_address String,
    token1_symbol String,
    token1_decimals UInt8,
    fee_bps UInt32,
    has_weth_leg UInt8,
    created_block UInt64,
    created_tx_hash String,
    discovered_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(discovered_at)
ORDER BY (chain_id, pool_address);
