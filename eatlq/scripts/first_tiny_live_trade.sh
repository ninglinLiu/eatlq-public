#!/usr/bin/env bash
# First live trade — 1 shot, session-only private key.
set -euo pipefail
cd /opt/EATLQ

ENV_FILE="${TINY_LIVE_ENV:-infra/compose/.env.tiny_live}"
set -a && source "$ENV_FILE" && set +a

export EXECUTOR_DRY_RUN=0
export LIVE_TRADING_MODE=true
export LIVE_AUTHORIZED_BY=HUMAN

echo "=== First tiny live trade ==="
echo "Wallet (expected): ${EXECUTOR_WALLET_ADDRESS:-not set}"
echo "Max 1 trade, max loss 2U. You will be prompted for private key."
echo ""

python3 scripts/tiny_live_runner.py \
  --max-trades 1 \
  --max-loss-u 2 \
  --max-loss-per-trade-u 2 \
  --max-concurrent 1
