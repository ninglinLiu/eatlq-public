#!/usr/bin/env bash
# P1–P4 one-shot ops (read-only / dry-run / infra restart). No live trading.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
export LIVE_TRADING_MODE=false

echo "=== P1: ensure forward watch cron ==="
if ! crontab -l 2>/dev/null | grep -qF 'sprint_forward_accumulation_watch'; then
  (crontab -l 2>/dev/null; echo "*/30 * * * * cd $REPO && python3 scripts/sprint_forward_accumulation_watch.py --once >> logs/forward_accumulation_watch_cron.log 2>&1") | crontab -
  echo "Installed */30 forward accumulation watch cron"
else
  echo "Forward watch cron already present"
fi

echo "=== P1: harness + leaderboard (already on crontab; refresh now) ==="
python3 scripts/sprint075_forward_harness.py --once
python3 scripts/sprint077_forward_harness.py --once
python3 scripts/sprint076_forward_leaderboard.py

echo "=== P2: S074_023 watch snapshot ==="
python3 scripts/sprint_forward_accumulation_watch.py --once
head -35 mvp/audits/forward_accumulation_watch/forward_accumulation_readout.md

echo "=== P3: restart stale ingestor / pool-discovery (signal supply) ==="
docker restart eatlq-ingestor-1 eatlq-pool-discovery-1 || true
python3 scripts/sprint055_fee100_paper_shadow.py --once || true
python3 scripts/sprint083_accumulation_monitor.py --once

echo "=== P4: human closure preflight + panic drill (no key, no live) ==="
python3 scripts/sprint061_human_closure_preflight.py --once --panic-drill
cat mvp/audits/sprint061_human_closure/sprint061_human_closure_flags.json

echo "=== Done ==="
