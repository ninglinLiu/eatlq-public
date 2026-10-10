#!/usr/bin/env bash
# Install Sprint078 Batch-25 pipeline cron entries (paper shadow + dry-run).
set -euo pipefail
ROOT=/opt/EATLQ
MARKER="# sprint078-batch25-pipeline"
TMP=$(mktemp)

# Strip old batch25 lines and legacy 8-alpha sprint068 runner (superseded by batch25).
crontab -l 2>/dev/null \
  | grep -v "$MARKER" \
  | grep -v "sprint078_batch25_harness" \
  | grep -v "sprint068_runner.lock" \
  | grep -v "sprint068_batch25.lock" \
  > "$TMP" || true

{
  cat "$TMP"
  # RPC reduction 2026-05-26: */10 (was */5), cap Quoter labels per run
  echo "*/10 * * * * flock -n /tmp/sprint068_batch25.lock -c 'cd $ROOT && python3 scripts/sprint068_multi_alpha_runner.py --once --batch25-manifest mvp/audits/sprint078_pipeline_batch25/batch25_manifest.json --max-rpc-calls 80 --bounded-label 40 >> logs/sprint068/batch25_runner_cron.log 2>&1' $MARKER"
  echo "*/15 * * * * cd $ROOT && python3 scripts/sprint078_batch25_harness.py --once >> logs/sprint078/harness_cron.log 2>&1 $MARKER"
} | sort -u > "${TMP}.new"

crontab "${TMP}.new"
rm -f "$TMP" "${TMP}.new"
mkdir -p "$ROOT/logs/sprint078" "$ROOT/logs/sprint068"
echo "Installed batch25 cron entries:"
crontab -l | grep "$MARKER"
