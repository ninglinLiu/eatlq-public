-- 040_tx_from_by_hash.sql
-- Maps tx_hash -> tx.from (EOA buyer). Populated by build_tx_from_lookup.py.
-- Apply: docker exec -i eatlq-clickhouse-1 clickhouse-client --multiquery < infra/clickhouse/init/040_tx_from_by_hash.sql

CREATE TABLE IF NOT EXISTS eatlq.tx_from_by_hash
(
    chain_id UInt64,
    tx_hash String,
    tx_from String,
    block_time Nullable(DateTime64(3, 'UTC')),
    fetched_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY toYYYYMM(coalesce(block_time, fetched_at))
ORDER BY (chain_id, tx_hash)
SETTINGS index_granularity = 8192;

DROP VIEW IF EXISTS eatlq.uniswap_v3_swaps_with_buyer;

CREATE VIEW eatlq.uniswap_v3_swaps_with_buyer AS
SELECT
    s.*,
    lower(
        if(
            length(tf.tx_from) = 42 AND startsWith(tf.tx_from, '0x'),
            tf.tx_from,
            s.sender
        )
    ) AS buyer_address,
    if(length(tf.tx_from) = 42, 1, 0) AS has_tx_from
FROM eatlq.uniswap_v3_swaps_decoded AS s
LEFT JOIN (
    SELECT chain_id, tx_hash, anyLast(tx_from) AS tx_from
    FROM eatlq.tx_from_by_hash
    GROUP BY chain_id, tx_hash
) AS tf ON s.chain_id = tf.chain_id AND lower(s.tx_hash) = lower(tf.tx_hash);
