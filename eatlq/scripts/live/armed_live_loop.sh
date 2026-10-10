#!/usr/bin/env bash
# Armed live loop (sprint097 fee=100 signal): detect fee==100 CLEAN onsets (Rule A 0x6ff5 /
# Rule B organic), then auto-execute the canary in --live mode within HARD caps
# (50U total / 5-10U each / <=10 trades / -20U halt / 2 concurrent / hold 2h).
# Safe no-op while no CLEAN fee=100 candidate exists. fee=100 onsets are rare (~1/day),
# so this is expected to idle for long stretches and fire only on a genuine clean signal.
set -u
cd /opt/EATLQ
export EXECUTOR_PRIVATE_KEY="$(grep -E '^EXECUTOR_PRIVATE_KEY=' infra/compose/.env | cut -d= -f2-)"
export CANARY_HOLD_SEC=7200   # 2h validated horizon
ETH_USD=3000
# 60s cadence: catch a fresh fee=100 onset early inside its 15min entry window AND
# re-price open positions for the trailing/-10% exit ladder every minute.
while true; do
  echo "=== cycle $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  python3 scripts/live/corrected_method_detector.py --lookback-min 90 --max-age-min 90 \
      --onset-window-min 30 --entry-fresh-min 15 --eth-usd "$ETH_USD" 2>&1
  python3 scripts/live/canary_executor.py --live --size-usd 8 --eth-usd "$ETH_USD" 2>&1
  python3 scripts/live/canary_redis_publish.py 2>&1 || echo "[loop] publish failed (non-fatal)"
  sleep 60
done
