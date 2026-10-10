#!/usr/bin/env bash
set -euo pipefail

# Phase 3.5: Extract realtime training data and train 4-feature model.
# Usage: bash scripts/run-realtime-training.sh [CHAIN_ID]
#
# Requires: ClickHouse with 025_realtime_training_views.sql applied.

CHAIN_ID="${1:-}"
DATASET="models/realtime_dataset.csv"
MODEL="models/signal_model_realtime_v1.json"
METRICS="models/signal_model_realtime_v1_metrics.json"

CHAIN_ARG=""
if [ -n "$CHAIN_ID" ]; then
    CHAIN_ARG="--chain-id $CHAIN_ID"
fi

echo "=== Step 1: Extract realtime training data ==="
python scripts/extract_realtime_training_data.py \
    --output "$DATASET" \
    $CHAIN_ARG

echo ""
echo "=== Step 2: Train 4-feature realtime model ==="
python scripts/train_model.py \
    --input "$DATASET" \
    --model-output "$MODEL" \
    --metrics-output "$METRICS" \
    --features realtime \
    --label label \
    --accuracy-gate 0.52

echo ""
echo "=== Done ==="
echo "Model: $MODEL"
echo "Metrics: $METRICS"
