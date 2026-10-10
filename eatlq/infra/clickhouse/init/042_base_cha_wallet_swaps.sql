-- B2 research-only transfer-net attribution tables.
-- Populated only by scripts/wallet_profile_v0/base_transfer_net_attribution.py.

CREATE TABLE IF NOT EXISTS eatlq.base_target_transfers_b2
(
    chain_id UInt64,
    block_number UInt64,
    block_time Nullable(DateTime64(3, 'UTC')),
    tx_hash String,
    log_index UInt32,
    token_address String,
    sender String,
    recipient String,
    amount UInt256,
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (chain_id, tx_hash, log_index, token_address);

CREATE TABLE IF NOT EXISTS eatlq.base_account_code_b2
(
    chain_id UInt64,
    address String,
    at_block UInt64,
    account_code_class LowCardinality(String),
    code_hash String,
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (chain_id, address, at_block);

CREATE TABLE IF NOT EXISTS eatlq.base_cha_wallet_swaps
(
    chain_id UInt64,
    protocol LowCardinality(String),
    venue_address String,
    block_number UInt64,
    block_time Nullable(DateTime64(3, 'UTC')),
    block_time_source LowCardinality(String),
    tx_hash String,
    log_index UInt32,
    swap_sender String,
    swap_recipient String,
    token0_address String,
    token1_address String,
    quote_token_address String,
    target_token_address String,
    side LowCardinality(String),
    resolved_account String,
    attribution_source LowCardinality(String),
    net_delta_raw Int256,
    transfer_log_count UInt16,
    same_direction_candidate_count UInt16,
    is_infra_only UInt8,
    account_code_class LowCardinality(String),
    residual_bucket LowCardinality(String),
    tx_from String,
    tx_from_match UInt8,
    built_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(built_at)
PARTITION BY intDiv(block_number, 1000000)
ORDER BY (chain_id, protocol, block_number, tx_hash, log_index);
