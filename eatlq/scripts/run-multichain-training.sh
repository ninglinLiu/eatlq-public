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

CHAIN_IDS="${1:-1,8453}"
TAG="${2:-multichain}"
DATASET="models/labeled_dataset_${TAG}.csv"
MODEL="models/signal_model_${TAG}_v1.json"
METRICS="models/signal_model_${TAG}_v1_metrics.json"

echo "=== Multi-chain training: chain_ids=${CHAIN_IDS} tag=${TAG} ==="

echo ""
echo "=== Phase 3A: Extract training data per chain, then merge ==="

IFS=',' read -ra CID_ARRAY <<< "${CHAIN_IDS}"
PART_FILES=()
for CID in "${CID_ARRAY[@]}"; do
  CID="$(echo "${CID}" | tr -d ' ')"
  PART="models/labeled_dataset_${TAG}_part_${CID}.csv"
  echo "Extracting chain_id=${CID} -> ${PART}"
  $DC run --rm strategy \
    python scripts/extract_training_data.py \
      --chain-id "${CID}" \
      --output "${PART}"
  if [[ ! -f "${REPO_ROOT}/${PART}" ]]; then
    echo "FAIL: ${PART} not created"
    exit 1
  fi
  PART_FILES+=("${REPO_ROOT}/${PART}")
done

echo "Merging ${#PART_FILES[@]} partitions -> ${DATASET}"
head -1 "${PART_FILES[0]}" > "${REPO_ROOT}/${DATASET}"
for PF in "${PART_FILES[@]}"; do
  tail -n +2 "${PF}" >> "${REPO_ROOT}/${DATASET}"
done
echo "3A PASS: $(wc -l < "${REPO_ROOT}/${DATASET}") lines"

echo ""
echo "=== Phase 3B: Train merged model ==="
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

for PF in "${PART_FILES[@]}"; do
  rm -f "${PF}"
done
