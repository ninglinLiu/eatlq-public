#!/usr/bin/env bash
# Weekly Phase 6 retraining cycle (LASZLO / EATLQ convention).
#
# Runs INSIDE the strategy container so it picks up:
#   - numpy / xgboost / web3 / clickhouse_connect already baked in the image
#   - docker-network DNS for http://clickhouse:8123
#   - the same EVM_HTTP_URL the live strategy uses (archive RPC if available)
#
# Pipeline (author's Phase 2 mainline):
#   1. label_dashcam_executable.py  -- archive-RPC replay quoter labels
#      (firewall-safe; approximate/velocity fallback is DISABLED by default
#      because models trained on it cannot be deployed)
#   2. retrain_model.py             -- 28d train window + 7d holdout +
#                                      absolute gate accuracy >= 0.55 +
#                                      PR-AUC uplift vs prev model
#
# Exit codes bubble up from retrain_model.py (standardized):
#   0 = model promoted
#   1 = generic error (container not up, image missing, etc.)
#   2 = gate failed (accuracy < 0.55 or pr_auc uplift not met)
#   3 = not enough data after filtering / labeling
#
# Env knobs (read from the host shell; exported into the container):
#   LABEL_WORKERS           default 5  (RPC fan-out; keep low to share quota
#                                        with the live strategy service)
#   LABEL_LIMIT             default 0  (0=unlimited; set to e.g. 1000 for
#                                        a smoke test before full run)
#   LABEL_BUCKET_SEC        default 300
#   LABEL_EXIT_HORIZON_SEC  default 1800
#   LABEL_NOTIONAL_WEI      default 500000000000000  (0.0005 ETH)
#   LABEL_MAX_AGE_DAYS      default 30
#   RETRAIN_WINDOW_DAYS     default 28
#   RETRAIN_HOLDOUT_DAYS    default 7
#   RETRAIN_MIN_ACCURACY    default 0.55
#   LABELER                 default executable  (set to "approximate" only
#                                                  for smoke/debug; resulting
#                                                  model will be FIREWALLED)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

STRATEGY_SERVICE="${STRATEGY_SERVICE:-strategy}"
STRATEGY_CONTAINER="${STRATEGY_CONTAINER:-eatlq-strategy-1}"

: "${LABEL_WORKERS:=5}"
: "${LABEL_LIMIT:=0}"
: "${LABEL_BUCKET_SEC:=300}"
: "${LABEL_EXIT_HORIZON_SEC:=1800}"
: "${LABEL_NOTIONAL_WEI:=500000000000000}"
: "${LABEL_MAX_AGE_DAYS:=30}"
: "${RETRAIN_WINDOW_DAYS:=28}"
: "${RETRAIN_HOLDOUT_DAYS:=7}"
: "${RETRAIN_MIN_ACCURACY:=0.55}"
: "${LABELER:=executable}"

if ! docker inspect -f '{{.State.Running}}' "${STRATEGY_CONTAINER}" >/dev/null 2>&1; then
  echo "ERROR: container ${STRATEGY_CONTAINER} is not running." >&2
  echo "Start the stack first with: scripts/laszlo.sh up" >&2
  exit 1
fi

run_in_strategy() {
  local extra_env="${1}"; shift
  docker exec "${STRATEGY_CONTAINER}" env \
    LABEL_WORKERS="${LABEL_WORKERS}" \
    LABEL_LIMIT="${LABEL_LIMIT}" \
    LABEL_BUCKET_SEC="${LABEL_BUCKET_SEC}" \
    LABEL_EXIT_HORIZON_SEC="${LABEL_EXIT_HORIZON_SEC}" \
    LABEL_NOTIONAL_WEI="${LABEL_NOTIONAL_WEI}" \
    LABEL_MAX_AGE_DAYS="${LABEL_MAX_AGE_DAYS}" \
    RETRAIN_WINDOW_DAYS="${RETRAIN_WINDOW_DAYS}" \
    RETRAIN_HOLDOUT_DAYS="${RETRAIN_HOLDOUT_DAYS}" \
    RETRAIN_MIN_ACCURACY="${RETRAIN_MIN_ACCURACY}" \
    ${extra_env} "$@"
}

case "${LABELER}" in
  executable)
    LABELER_SCRIPT="scripts/label_dashcam_executable.py"
    LABELED_OUTPUT="${LABELED_OUTPUT:-logs/labeled_dataset_v2_executable.csv}"
    ;;
  approximate)
    LABELER_SCRIPT="scripts/label_dashcam.py"
    LABELED_OUTPUT="${LABELED_OUTPUT:-logs/labeled_dataset.csv}"
    echo "WARNING: LABELER=approximate. Model trained on these labels will be"
    echo "         FIREWALLED (not deployable). Use only for smoke/debug."
    ;;
  *)
    echo "ERROR: unknown LABELER=${LABELER} (valid: executable, approximate)" >&2
    exit 1
    ;;
esac

echo "== ${LABELER_SCRIPT} (workers=${LABEL_WORKERS} limit=${LABEL_LIMIT} max_age=${LABEL_MAX_AGE_DAYS}d) =="
run_in_strategy "LABELED_OUTPUT=${LABELED_OUTPUT}" python -u "${LABELER_SCRIPT}"

echo
echo "== retrain_model.py (window=${RETRAIN_WINDOW_DAYS}d holdout=${RETRAIN_HOLDOUT_DAYS}d min_acc=${RETRAIN_MIN_ACCURACY}) =="
set +e
run_in_strategy "LABELED_OUTPUT=${LABELED_OUTPUT}" python -u scripts/retrain_model.py
rc=$?
set -e

case ${rc} in
  0) echo "phase6: OK, model promoted" ;;
  2) echo "phase6: gate_failed (accuracy/pr_auc); previous model retained" ;;
  3) echo "phase6: insufficient_data; cycle deferred" ;;
  *) echo "phase6: error rc=${rc}" ;;
esac
exit ${rc}
