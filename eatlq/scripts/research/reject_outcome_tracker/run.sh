#!/usr/bin/env bash
# Reject-outcome tracker — cron entrypoint.
#
# Pure observational tool: reads executor container logs (docker logs) and
# ClickHouse's already-collected swap archive; writes only to the isolated
# app.reject_outcome_tracking Postgres table. Zero RPC calls, zero writes to
# any live trading table/config, zero interaction with the executor's
# decision path. See README.md for the safety model.
set -euo pipefail
cd /opt/EATLQ
mkdir -p /tmp/eatlq_locks logs
LOCK=/tmp/eatlq_locks/reject_outcome_tracker.lock
exec flock -n "${LOCK}" -c '
  python3 scripts/research/reject_outcome_tracker/ingest.py &&
  python3 scripts/research/reject_outcome_tracker/snapshot.py
' >> /opt/EATLQ/logs/reject_outcome_tracker.log 2>&1
