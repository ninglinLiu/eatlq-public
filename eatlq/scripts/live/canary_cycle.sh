#!/usr/bin/env bash
# One canary cycle, cron-driven (*/5). flock prevents overlap. Durable: no daemon to die.
# Detect fee=100 CLEAN onset -> execute live canary within HARD caps -> publish state to Redis.
set -u
cd /opt/EATLQ || exit 1
LOCK=/tmp/canary_cycle.lock
exec 9>"$LOCK"
flock -n 9 || { echo "$(date -u +%FT%TZ) busy, skip"; exit 0; }

export EXECUTOR_PRIVATE_KEY="$(grep -E '^EXECUTOR_PRIVATE_KEY=' infra/compose/.env | cut -d= -f2-)"
export CANARY_HOLD_SEC=7200
ETH_USD=3000
LOG=/opt/EATLQ/mvp/diagnostics/live/loop.log

# Within a single cron tick we run an internal ~60s loop. This gives:
#   - entry latency ~60s (catch a fresh fee=100 onset early inside its 15min window,
#     so we enter near onset price instead of up to 5min late), and
#   - exit responsiveness ~60s (re-price every open position for the trailing/stop
#     ladder each minute, not once per 5min — critical for the -10% hard stop).
# flock(9) still guarantees no overlap with the next tick.
ITERS="${CANARY_ITERS:-5}"
SUBSLEEP="${CANARY_SUBSLEEP:-60}"
{
  for i in $(seq 1 "$ITERS"); do
    echo "=== cycle $(date -u +%FT%TZ) (sub $i/$ITERS) ==="
    # onset may occur <=30min after birth; we only enter <=15min after onset (faithful to backtest).
    python3 scripts/live/corrected_method_detector.py --lookback-min 90 --max-age-min 90 \
        --onset-window-min 30 --entry-fresh-min 15 --eth-usd "$ETH_USD" 2>&1
    python3 scripts/live/canary_executor.py --live --size-usd 8 --eth-usd "$ETH_USD" 2>&1
    python3 scripts/live/canary_redis_publish.py 2>&1 || echo "[cycle] publish failed (non-fatal)"
    [ "$i" -lt "$ITERS" ] && sleep "$SUBSLEEP"
  done
} >>"$LOG" 2>&1

# keep log bounded (raised: each tick now emits ~5 sub-cycles)
tail -n 6000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
