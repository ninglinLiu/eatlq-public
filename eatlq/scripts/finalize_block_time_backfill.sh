#!/usr/bin/env bash
# Run AFTER scripts/backfill_block_timestamps.py has finished populating
# eatlq.block_ts_map.  Fires the ALTER TABLE UPDATE mutations on the swap
# tables, waits for them to complete, then verifies downstream coverage.
#
# Safe to run multiple times: mutations that don't change rows are no-ops,
# and ClickHouse de-dupes identical mutations.
#
# Usage:
#   scripts/finalize_block_time_backfill.sh            # mutate + wait + verify
#   scripts/finalize_block_time_backfill.sh --dry-run  # show counts only
#   SKIP_MUTATION=1 scripts/finalize_block_time_backfill.sh  # just verify
set -euo pipefail

CH=(docker exec -i eatlq-clickhouse-1 clickhouse-client
    --query_plan_max_optimizations_to_apply=0)
CHAIN_ID="${CHAIN_ID:-8453}"
TABLES=(uniswap_v2_swaps uniswap_v3_swaps)

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then dry_run=1; fi

log "=== Pre-flight: block_ts_map status ==="
"${CH[@]}" -q "
  SELECT count() AS rows,
         countDistinct(block_number) AS unique_blocks,
         min(block_time) AS min_t,
         max(block_time) AS max_t
  FROM eatlq.block_ts_map WHERE chain_id=${CHAIN_ID}
  FORMAT PrettyCompact"

log ""
log "=== NULL block_time counts before mutation ==="
for t in "${TABLES[@]}"; do
    "${CH[@]}" -q "
      SELECT '${t}' AS table,
             count() AS null_rows,
             countDistinct(block_number) AS null_unique_blocks
      FROM eatlq.${t} WHERE chain_id=${CHAIN_ID} AND block_time IS NULL
      FORMAT PrettyCompact"
done

if [[ "$dry_run" == "1" ]]; then
    log "dry-run; exiting"; exit 0
fi

if [[ "${SKIP_MUTATION:-0}" != "1" ]]; then
    log ""
    log "=== Reloading eatlq.block_ts_dict (to pick up any new rows) ==="
    "${CH[@]}" -q "SYSTEM RELOAD DICTIONARY eatlq.block_ts_dict"
    "${CH[@]}" -q "
      SELECT name, status, element_count
      FROM system.dictionaries
      WHERE database='eatlq' AND name='block_ts_dict'
      FORMAT PrettyCompact"

    for t in "${TABLES[@]}"; do
        log ""
        log "=== Launching ALTER TABLE UPDATE on eatlq.${t} (dictGet-based) ==="
        # Use dictGet against block_ts_dict (COMPLEX_KEY_HASHED in-memory dict
        # backed by block_ts_map).  dictHas gates rows so only blocks we
        # actually have a timestamp for are rewritten; the rest stay NULL.
        "${CH[@]}" -q "
          ALTER TABLE eatlq.${t}
          UPDATE block_time = dictGet(
              'eatlq.block_ts_dict', 'block_time',
              (toUInt64(chain_id), toUInt64(block_number))
          )
          WHERE chain_id = ${CHAIN_ID}
            AND block_time IS NULL
            AND dictHas('eatlq.block_ts_dict', (toUInt64(chain_id), toUInt64(block_number)))
          SETTINGS mutations_sync = 0
        "
    done

    log ""
    log "=== Waiting for mutations to complete ==="
    while true; do
        pending=$("${CH[@]}" -q "
          SELECT sum(parts_to_do) FROM system.mutations
          WHERE database='eatlq' AND table IN ('uniswap_v2_swaps','uniswap_v3_swaps')
            AND is_done = 0
          FORMAT TSVRaw" | tr -d '\n' | tr -d ' ')
        [[ -z "$pending" || "$pending" == "0" ]] && break
        log "  parts_to_do=${pending}; sleeping 30s"
        sleep 30
    done
    log "  all mutations complete"
fi

log ""
log "=== NULL block_time counts AFTER mutation ==="
for t in "${TABLES[@]}"; do
    "${CH[@]}" -q "
      SELECT '${t}' AS table,
             count() AS null_rows,
             countDistinct(block_number) AS null_unique_blocks
      FROM eatlq.${t} WHERE chain_id=${CHAIN_ID} AND block_time IS NULL
      FORMAT PrettyCompact"
done

log ""
log "=== Downstream visibility: uniswap_v*_swaps_decoded coverage ==="
for t in uniswap_v2_swaps_decoded uniswap_v3_swaps_decoded; do
    "${CH[@]}" -q "
      SELECT
        '${t}' AS view,
        count() AS events_30d,
        min(block_time) AS min_t,
        max(block_time) AS max_t
      FROM eatlq.${t}
      WHERE block_time >= now() - INTERVAL 30 DAY
      SETTINGS max_memory_usage = 5500000000,
               max_bytes_before_external_group_by = 1500000000
      FORMAT PrettyCompact" || log "  (${t} query may have hit memory limits; this is OK)"
done

log ""
log "All done.  Next:"
log "  docker exec eatlq-wallet-scorer-1 python scripts/compute_wallet_scores.py"
log "  (wallet-scorer auto-loops every \${SCORING_INTERVAL_SEC:-21600}s)"
