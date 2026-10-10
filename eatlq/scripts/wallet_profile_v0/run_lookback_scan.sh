#!/usr/bin/env bash
# R4: label freshness scan — smart_money lookback 14/30/60/90d × 14d backtest
set -euo pipefail
cd /opt/EATLQ
OUT_DIR=mvp/audits/wallet_profile_v0/sender_gated
mkdir -p "$OUT_DIR"
RESULTS="$OUT_DIR/lookback_scan.json"
echo '{"lookbacks":[' > "$RESULTS"

first=1
for lb in 14 30 60 90; do
  echo "=== compute smart_money lookback ${lb}d ==="
  python3 scripts/wallet_profile_v0/compute_labels.py \
    --only smart_money --lookback-days "$lb" \
    --output-suffix "_lb${lb}d" --redis-suffix "_lb${lb}d" --redis-out || true

  echo "=== backtest 14d with labels _lb${lb}d ==="
  python3 scripts/sprint091_sender_gated_paper.py \
    --backtest-days 14 --no-readout \
    --labels-csv "mvp/audits/wallet_profile_v0/labels/all_labels_lb${lb}d.csv" \
    --output-json "$OUT_DIR/backtest_lb${lb}d_14d.json" 2>/dev/null | tail -1

  if [ "$first" -eq 0 ]; then echo ',' >> "$RESULTS"; fi
  first=0
  python3 -c "
import json
d=json.load(open('$OUT_DIR/backtest_lb${lb}d_14d.json'))
print(json.dumps({'lookback_days': $lb, **{k:d[k] for k in ['gated_signals','pf_gated_30m_tick','mean_return_gated','top_pool_share']}}))
" >> "$RESULTS"
done
echo ']}' >> "$RESULTS"
python3 -m json.tool "$RESULTS" > "$OUT_DIR/lookback_scan_formatted.json"
mv "$OUT_DIR/lookback_scan_formatted.json" "$RESULTS"
echo "Wrote $RESULTS"
