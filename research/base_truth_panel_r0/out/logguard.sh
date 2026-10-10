#!/bin/bash
# ClickHouse system logs grew to 29 GiB during the backfill -- nearly twice the size
# of the data being backfilled -- because millions of inserts each emit log rows.
# TTL is set, but TTL only applies on merge, so a hard cap is kept here as well.
while kill -0 3502776 2>/dev/null; do
  sz=$(docker exec eatlq-clickhouse-1 clickhouse-client --query "
    SELECT sum(bytes_on_disk) FROM system.parts
    WHERE active AND database='system' AND table LIKE '%_log'" 2>/dev/null)
  free=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  if [ -n "$sz" ] && [ "$sz" -gt 5000000000 ]; then
    for t in text_log trace_log processors_profile_log metric_log asynchronous_metric_log query_log part_log; do
      docker exec eatlq-clickhouse-1 clickhouse-client --query "TRUNCATE TABLE IF EXISTS system.$t" 2>/dev/null
    done
    echo "[guard] $(date +%H:%M:%S) truncated system logs at $((sz/1000000000))GB, free=${free}G"
  else
    echo "[guard] $(date +%H:%M:%S) syslogs=$((${sz:-0}/1000000))MB free=${free}G"
  fi
  sleep 300
done
echo "[guard] backfill pid gone, exiting"
