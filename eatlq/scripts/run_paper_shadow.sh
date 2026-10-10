#!/usr/bin/env bash
# ============================================================
# run_paper_shadow.sh — Start paper shadow dry-run pipeline
# ============================================================
# What this does:
#   1. Seeds pool:discovered:* in Redis for 49 sender-gated pools
#   2. Publishes pending S074_022_6ff5/S074_032_6ff5 signals from sprint078 CSV
#   3. Watches sprint055 live shadow CSV every 60s for new signals
#
# Expected: EXECUTOR_DRY_RUN=1 executor container is already running.
#           Redis is reachable at $REDIS_URL.
#
# Accumulation target: run for 2+ weeks to reach N>=100 executor-confirmed
#   dry-run trades before READY_FOR_HUMAN_LIVE_REVIEW gate check.
#
# Safety:
#   - LIVE_TRADING_MODE is never set here
#   - Bridge only writes to Redis signals stream; no chain transactions
#
# Usage:
#   bash scripts/run_paper_shadow.sh           # watch sprint055 live CSV
#   bash scripts/run_paper_shadow.sh --once    # process once and exit
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

export REDIS_URL="${REDIS_URL:-redis://172.18.0.10:6379/0}"
export SPRINT055_CSV="${SPRINT055_CSV:-$REPO_ROOT/logs/sprint055_paper_shadow.csv}"

# Safety gate
if [[ "${LIVE_TRADING_MODE:-}" =~ ^(1|true|yes|on)$ ]]; then
    echo "ERROR: LIVE_TRADING_MODE is set. This script runs paper shadow only. Exiting." >&2
    exit 1
fi

# Check if sprint055 has is_0x6ff5 column
has_sender_field=$(python3 -c "
import csv
with open('$SPRINT055_CSV') as f:
    cols = next(csv.reader(f))
    print('YES' if 'is_0x6ff5' in cols else 'NO')
" 2>/dev/null || echo "NO")

if [[ "$has_sender_field" == "NO" ]]; then
    echo "WARNING: sprint055_paper_shadow.csv has no is_0x6ff5 field."
    echo "Using sprint078_shadow.csv as fallback (OOS historical replay, not live forward)."
    export SPRINT055_CSV="$REPO_ROOT/logs/sprint078_shadow.csv"
fi

echo "=== EATLQ Paper Shadow Dry-Run Bridge ==="
echo "CSV:       $SPRINT055_CSV"
echo "Redis:     $REDIS_URL"
echo "Mode:      DRY_RUN (LIVE_TRADING_MODE=${LIVE_TRADING_MODE:-false})"
echo "Target:    N>=100 executor-confirmed dry-run trades"
echo "==========================================="

# Run bridge
if [[ "${1:-}" == "--once" ]]; then
    python3 "$SCRIPT_DIR/sprint082_signal_bridge.py" --once --csv "$SPRINT055_CSV"
else
    python3 "$SCRIPT_DIR/sprint082_signal_bridge.py" --watch --csv "$SPRINT055_CSV"
fi
