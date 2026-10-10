-- 036_block_ts_dict.sql
-- In-memory dictionary over block_ts_map so mutations can look up real
-- on-chain block_time by (chain_id, block_number) in O(1) without
-- correlated-subquery support (which ClickHouse ALTER UPDATE lacks).
--
-- Used by scripts/finalize_block_time_backfill.sh:
--   ALTER TABLE eatlq.uniswap_v{2,3}_swaps
--   UPDATE block_time = dictGet('eatlq.block_ts_dict', 'block_time',
--                                (chain_id, block_number))
--   WHERE block_time IS NULL AND dictHas('eatlq.block_ts_dict', ...);
--
-- LIFETIME(MIN 0 MAX 0) disables auto-refresh; callers invoke
-- SYSTEM RELOAD DICTIONARY eatlq.block_ts_dict after new inserts into
-- block_ts_map.  COMPLEX_KEY_HASHED keeps the ~1M entries in ~100MB RAM.
--
-- Note: the SOURCE clause references a password in plaintext.  For this
-- repository it matches infra/compose/.env (CLICKHOUSE_PASSWORD).  In
-- other deployments a named_collection should be preferred.

DROP DICTIONARY IF EXISTS eatlq.block_ts_dict;

CREATE DICTIONARY eatlq.block_ts_dict
(
    chain_id     UInt64,
    block_number UInt64,
    block_time   DateTime64(3, 'UTC')
)
PRIMARY KEY chain_id, block_number
SOURCE(CLICKHOUSE(
    HOST 'localhost' PORT 9000
    USER 'default' PASSWORD 'Ch_7Z3mN2rQ8vP52026sT9xW4y'
    DB 'eatlq' TABLE 'block_ts_map'
))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(MIN 0 MAX 0);
