#!/usr/bin/env bash
set -euo pipefail

bash scripts/apply-clickhouse-init.sh
bash scripts/verify-inference-inputs.sh "${1:-3}"
