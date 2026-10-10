#!/usr/bin/env bash
# Start tiny live runner in tmux (session-only PK — you type key after attach).
set -euo pipefail
cd /opt/EATLQ

SESSION="${TINY_LIVE_SESSION:-tiny_live}"
ENV_FILE="${TINY_LIVE_ENV:-infra/compose/.env.tiny_live}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a && source "$ENV_FILE" && set +a

MAX_TRADES="${MAX_TRADES:-5}"
MAX_LOSS_U="${MAX_LOSS_U:-5}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already exists — attach: tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s "$SESSION" \
  "cd /opt/EATLQ && set -a && source $ENV_FILE && set +a && \
   python3 scripts/tiny_live_runner.py --max-trades $MAX_TRADES --max-loss-u $MAX_LOSS_U; \
   echo 'runner exited'; bash"

echo "Started tmux session: $SESSION"
echo "  attach:  tmux attach -t $SESSION"
echo "  logs:    tail -f logs/tiny_live_runner.csv"
echo "  monitor: python3 scripts/tiny_live_monitor.py --interval 300"
