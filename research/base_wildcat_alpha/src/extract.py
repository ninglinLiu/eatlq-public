from __future__ import annotations

import csv
import json
from pathlib import Path

from ch_readonly import query_to_file, query_tsv
from safety import research_entry_guard


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CUTOFF = "2026-08-14 14:00:00"
START = "2026-04-01 00:00:00"


HOURLY_V3_SQL = f"""
SELECT
    pool_address,
    toStartOfHour(block_time) AS block_hour,
    uniqExact(tuple(tx_hash, log_index)) AS swap_count,
    uniqExact(tx_hash) AS tx_count,
    uniqExact(sender) AS unique_senders,
    uniqExact(recipient) AS unique_recipients,
    uniqExact(block_number) AS unique_blocks,
    argMin(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))), tuple(block_number, log_index)) AS open_tick,
    argMax(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))), tuple(block_number, log_index)) AS close_tick,
    min(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8))))) AS low_tick,
    max(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8))))) AS high_tick,
    stddevPop(toFloat64(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))))) AS tick_std,
    avg(toFloat64(log_index)) AS mean_log_index,
    uniqExactIf(tuple(tx_hash, log_index), startsWith(substring(amount0_raw, 3), 'f')) / uniqExact(tuple(tx_hash, log_index)) AS amount0_negative_share,
    avg(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32))))))) AS mean_log_liquidity,
    stddevPop(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32))))))) AS sd_log_liquidity,
    argMin(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32)))))), tuple(block_number, log_index)) AS open_log_liquidity,
    argMax(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32)))))), tuple(block_number, log_index)) AS close_log_liquidity,
    count() / uniqExact(tuple(tx_hash, log_index)) AS duplicate_pressure
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0 AND block_time IS NOT NULL AND length(tick_raw) = 66 AND length(liquidity_raw) = 66
  AND block_time >= toDateTime64('{START}', 3, 'UTC')
  AND block_time <= toDateTime64('{CUTOFF}', 3, 'UTC')
GROUP BY pool_address, block_hour
ORDER BY pool_address, block_hour
"""


POOL_META_SQL = """
SELECT
    pool_address,
    argMax(factory_address, discovered_at) AS factory_address,
    argMax(pool_type, discovered_at) AS pool_type,
    argMax(token0_address, discovered_at) AS token0_address,
    argMax(token0_symbol, discovered_at) AS token0_symbol,
    argMax(token0_decimals, discovered_at) AS token0_decimals,
    argMax(token1_address, discovered_at) AS token1_address,
    argMax(token1_symbol, discovered_at) AS token1_symbol,
    argMax(token1_decimals, discovered_at) AS token1_decimals,
    argMax(fee_bps, discovered_at) AS fee_bps,
    argMax(has_weth_leg, discovered_at) AS has_weth_leg,
    minIf(created_block, created_block > 0) AS created_block
FROM eatlq.discovered_pools
WHERE chain_id = 8453
GROUP BY pool_address
"""


V2_MARKET_SQL = f"""
SELECT
    toStartOfHour(block_time) AS block_hour,
    count() AS v2_swap_rows,
    uniqExact(pair_address) AS v2_active_pools,
    uniqExact(tx_hash) AS v2_transactions
FROM eatlq.uniswap_v2_swaps
WHERE chain_id = 8453 AND removed = 0
  AND block_time >= toDateTime64('{START}', 3, 'UTC')
  AND block_time <= toDateTime64('{CUTOFF}', 3, 'UTC')
GROUP BY block_hour
ORDER BY block_hour
"""


LP_HOURLY_SQL = f"""
SELECT
    pool_address,
    block_hour,
    sum(mints) AS mint_count,
    sum(burns) AS burn_count,
    sum(collects) AS collect_count,
    sum(mint_log_amount) AS mint_log_amount,
    sum(burn_log_amount) AS burn_log_amount
FROM
(
    SELECT pool_address, toStartOfHour(block_time) AS block_hour,
           count() AS mints, 0 AS burns, 0 AS collects,
           sum(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32))))))) AS mint_log_amount,
           0.0 AS burn_log_amount
    FROM eatlq.uniswap_v3_mints
    WHERE chain_id=8453 AND removed=0
      AND block_time >= toDateTime64('{START}',3,'UTC')
      AND block_time <= toDateTime64('{CUTOFF}',3,'UTC')
    GROUP BY pool_address, block_hour
    UNION ALL
    SELECT pool_address, toStartOfHour(block_time) AS block_hour,
           0 AS mints, count() AS burns, 0 AS collects,
           0.0 AS mint_log_amount,
           sum(log1p(toFloat64(reinterpretAsUInt128(reverse(unhex(right(liquidity_raw, 32))))))) AS burn_log_amount
    FROM eatlq.uniswap_v3_burns
    WHERE chain_id=8453 AND removed=0
      AND block_time >= toDateTime64('{START}',3,'UTC')
      AND block_time <= toDateTime64('{CUTOFF}',3,'UTC')
    GROUP BY pool_address, block_hour
    UNION ALL
    SELECT pool_address, toStartOfHour(block_time) AS block_hour,
           0 AS mints, 0 AS burns, count() AS collects,
           0.0 AS mint_log_amount, 0.0 AS burn_log_amount
    FROM eatlq.uniswap_v3_collects
    WHERE chain_id=8453 AND removed=0
      AND block_time >= toDateTime64('{START}',3,'UTC')
      AND block_time <= toDateTime64('{CUTOFF}',3,'UTC')
    GROUP BY pool_address, block_hour
)
GROUP BY pool_address, block_hour
ORDER BY pool_address, block_hour
"""


AUDIT_QUERIES = {
    "v3_swaps": "SELECT count(), uniqExact(tuple(pool_address,tx_hash,log_index)), uniqExact(pool_address), min(block_time), max(block_time), countIf(block_time IS NULL), countIf(removed=1) FROM eatlq.uniswap_v3_swaps WHERE chain_id=8453",
    "v2_swaps": "SELECT count(), uniqExact(tuple(pair_address,tx_hash,log_index)), uniqExact(pair_address), min(block_time), max(block_time), countIf(block_time IS NULL), countIf(removed=1) FROM eatlq.uniswap_v2_swaps WHERE chain_id=8453",
    "v3_mints": "SELECT count(), uniqExact(pool_address), min(block_time), max(block_time), countIf(block_time IS NULL), countIf(removed=1) FROM eatlq.uniswap_v3_mints WHERE chain_id=8453",
    "v3_burns": "SELECT count(), uniqExact(pool_address), min(block_time), max(block_time), countIf(block_time IS NULL), countIf(removed=1) FROM eatlq.uniswap_v3_burns WHERE chain_id=8453",
    "v3_collects": "SELECT count(), uniqExact(pool_address), min(block_time), max(block_time), countIf(removed=1) FROM eatlq.uniswap_v3_collects WHERE chain_id=8453",
    "discovered_pools": "SELECT count(), uniqExact(pool_address), countIf(pool_type='v2'), countIf(pool_type='v3'), countIf(has_weth_leg=1), min(created_block), max(created_block) FROM eatlq.discovered_pools WHERE chain_id=8453",
    "raw_logs": "SELECT count(), uniqExact(address), uniqExact(topic0), min(block_time), max(block_time), countIf(block_time IS NULL), countIf(removed=1) FROM eatlq.raw_evm_logs WHERE chain_id=8453",
}


def extract() -> None:
    research_entry_guard()
    DATA.mkdir(parents=True, exist_ok=True)
    query_to_file(HOURLY_V3_SQL, DATA / "hourly_v3.tsv")
    query_to_file(POOL_META_SQL, DATA / "pool_meta.tsv")
    query_to_file(V2_MARKET_SQL, DATA / "hourly_v2_market.tsv")
    query_to_file(LP_HOURLY_SQL, DATA / "hourly_lp.tsv")
    audit = {name: query_tsv(sql) for name, sql in AUDIT_QUERIES.items()}
    (DATA / "source_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    manifest = {
        "chain_id": 8453,
        "cutoff_utc": CUTOFF,
        "rapid_panel_start_utc": START,
        "selection": "all observed V3 pools in the time window; no ex-post activity filter or prior labels",
        "dedup_key": ["pool_address", "tx_hash", "log_index"],
        "source_mode": "ClickHouse --readonly=1, SELECT only",
        "files": sorted(p.name for p in DATA.iterdir()),
    }
    (DATA / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    extract()
