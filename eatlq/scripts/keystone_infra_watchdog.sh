#!/usr/bin/env bash
# Keystone infra watchdog — cron entrypoint.
set -euo pipefail
cd /opt/EATLQ
exec python3 /opt/EATLQ/scripts/keystone_infra_watchdog.py >> /opt/EATLQ/logs/keystone_infra_watchdog.log 2>&1
