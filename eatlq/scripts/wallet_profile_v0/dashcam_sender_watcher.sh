#!/usr/bin/env bash
# R6: monitor dashcam sender_address fill rate (background watcher)
set -euo pipefail
LOG=/opt/EATLQ/mvp/audits/wallet_profile_v0/sender_gated/dashcam_sender_fill.log
DASH=/opt/EATLQ/logs/dashcam.csv
mkdir -p "$(dirname "$LOG")"
while true; do
  n=$(awk -F',' 'NR>1 && $22 ~ /^0x/ {c++} END{print c+0}' "$DASH" 2>/dev/null || echo 0)
  total=$(awk 'END{print NR-1}' "$DASH" 2>/dev/null || echo 0)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) dashcam_with_sender=$n total_rows=$total" >> "$LOG"
  sleep 1800
done
