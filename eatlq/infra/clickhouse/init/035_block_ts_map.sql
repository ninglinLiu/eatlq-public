-- 035_block_ts_map.sql
-- Auxiliary map of (chain_id, block_number) -> real on-chain block_time.
--
-- Populated by scripts/backfill_block_timestamps.py against an archive RPC
-- (QuickNode) to recover block_time for historical blocks that were ingested
-- before block_time was consistently captured (e.g. discovery_poller writes,
-- early backfill shards, eth_getLogs backfills that did not carry
-- blockTimestamp).  The base tables uniswap_v2_swaps / uniswap_v3_swaps are
-- then mutated via ALTER TABLE UPDATE using this map so all downstream views
-- (uniswap_v*_swaps_decoded, swap_trade_events_all, sender_pnl_summary_v1,
-- sender_feature_store_v1) observe correct
-- timestamps without per-view rewrites.
--
-- ReplacingMergeTree with fetched_at as version makes re-running the
-- backfill script idempotent; FINAL selects return the most-recent fetch.

CREATE TABLE IF NOT EXISTS eatlq.block_ts_map
(
    chain_id     UInt64,
    block_number UInt64,
    block_time   DateTime64(3, 'UTC'),
    fetched_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY intDiv(block_number, 1000000)
ORDER BY (chain_id, block_number)
SETTINGS index_granularity = 8192;
