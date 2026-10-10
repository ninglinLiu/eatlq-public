#!/usr/bin/env bash
set -euo pipefail
# Pull archive URL from live compose the same way prior KEYSTONE fork jobs did.
# Do not echo the URL.
export FORK_ARCHIVE_RPC
FORK_ARCHIVE_RPC="$(grep -E '^POOL_DISCOVERY_RPC_URL=' /opt/EATLQ/infra/compose/.env | cut -d= -f2-)"
if [[ -z "${FORK_ARCHIVE_RPC}" ]]; then
  echo "POOL_DISCOVERY_RPC_URL missing" >&2
  exit 1
fi
cd "$(dirname "$0")"
mkdir -p out
python3 src/ch_signals.py
python3 src/fork_bt.py
python3 src/report.py
