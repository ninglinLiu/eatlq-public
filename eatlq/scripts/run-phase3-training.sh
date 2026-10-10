#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

if [[ ! -f "${COMPOSE_DIR}/.env" ]]; then
  echo "Missing ${COMPOSE_DIR}/.env"
  exit 1
fi

DC="docker compose --env-file ${COMPOSE_DIR}/.env -f ${COMPOSE_DIR}/compose.yaml"

CHAIN_ID="${1:-}"
TAG=""
EXTRACT_EXTRA_ARGS=""
DATASET="models/labeled_dataset.csv"
MODEL="models/signal_model_v1.json"
METRICS="models/signal_model_v1_metrics.json"

if [[ -n "${CHAIN_ID}" ]]; then
  EXTRACT_EXTRA_ARGS="--chain-id ${CHAIN_ID}"
  case "${CHAIN_ID}" in
    8453)  TAG="base" ;;
    1)     TAG="mainnet" ;;
    *)     TAG="chain${CHAIN_ID}" ;;
  esac
  DATASET="models/labeled_dataset_${TAG}.csv"
  MODEL="models/signal_model_${TAG}_v1.json"
  METRICS="models/signal_model_${TAG}_v1_metrics.json"
fi

echo "=== Phase 3A: Extract training data ==="
echo "chain_id=${CHAIN_ID:-auto} dataset=${DATASET}"
$DC run --rm strategy \
  python scripts/extract_training_data.py \
    --output "${DATASET}" ${EXTRACT_EXTRA_ARGS}

if [[ ! -f "${REPO_ROOT}/${DATASET}" ]]; then
  echo "FAIL: ${DATASET} not created"
  exit 1
fi
echo "3A PASS: $(wc -l < "${REPO_ROOT}/${DATASET}") lines"

echo ""
echo "=== Phase 3B: Train model ==="
echo "model=${MODEL} metrics=${METRICS}"
$DC run --rm strategy \
  python scripts/train_model.py \
    --input "${DATASET}" \
    --model-output "${MODEL}" \
    --metrics-output "${METRICS}" \
    --accuracy-gate 0.55

if [[ -f "${REPO_ROOT}/${METRICS}" ]]; then
  echo "3B PASS"
  cat "${REPO_ROOT}/${METRICS}"
else
  echo "3B: model gate did not pass or training failed (check output above)"
fi
