#!/usr/bin/env bash
# P0-1: Backfill NULL block_time rows using free Base RPC + budget guard.
set -euo pipefail

REPO="/opt/EATLQ"
cd "$REPO"

export LABEL_RPC_URL="${LABEL_RPC_URL:-https://mainnet.base.org}"
export FREE_RPC_URL="${FREE_RPC_URL:-https://mainnet.base.org}"
export CHAIN_ID="${CHAIN_ID:-8453}"
export TARGET_TABLES="${TARGET_TABLES:-uniswap_v3_swaps}"
# mainnet.base.org rejects JSON-RPC batch arrays (HTTP 403); use single calls.
export MAX_CPS="${MAX_CPS:-2}"
export CONCURRENCY="${CONCURRENCY:-1}"
export BATCH_SIZE="${BATCH_SIZE:-1}"
export EATLQ_RPC_DAILY_CALL_LIMIT="${EATLQ_RPC_DAILY_CALL_LIMIT:-60000}"
export EATLQ_RPC_BUDGET_ENABLED="${EATLQ_RPC_BUDGET_ENABLED:-1}"

python3 scripts/lib/rpc_budget_guard.py --require 100 || {
  echo "RPC budget too low for backfill run; abort."
  exit 1
}

exec python3 scripts/backfill_block_timestamps.py
