#!/usr/bin/env bash
# Seal EATLQ ingestion + alpha cron to stop RPC burn while QuickNode is off.
set -euo pipefail

REPO="/opt/EATLQ"
AUDIT_DIR="$REPO/mvp/audits/p0_fix_20260612"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
COMPOSE_DIR="$REPO/infra/compose"

mkdir -p "$AUDIT_DIR" "$REPO/mvp/diagnostics"

echo "[$STAMP] P0 sealed mode — stopping RPC-burning containers"
cd "$COMPOSE_DIR"

# Stop strategy/ingestion path (profile strategy + core ingestor)
for svc in ingestor swap-poller pool-discovery strategy executor shadow-closer wallet-scorer; do
  if docker compose ps -q "$svc" 2>/dev/null | grep -q .; then
    echo "  stopping $svc"
    docker compose stop "$svc" || true
  fi
done

# Prevent restart-unless-stopped from bringing ingestor back until unsealed
docker update --restart=no eatlq-ingestor-1 2>/dev/null || true
docker update --restart=no eatlq-swap-poller-1 2>/dev/null || true
docker update --restart=no eatlq-pool-discovery-1 2>/dev/null || true
docker update --restart=no eatlq-strategy-1 2>/dev/null || true
docker update --restart=no eatlq-executor-1 2>/dev/null || true

# Write sealed marker
cat > "$REPO/mvp/diagnostics/INGESTION_MODE" <<EOF
mode=sealed
applied_at_utc=$STAMP
reason=P0_fix: QuickNode off, stop dual-ingestion RPC burn + autoheal loop
rpc_default=0
EOF

# Backup + install sealed crontab
if crontab -l >"$AUDIT_DIR/crontab_pre_seal_${STAMP}.bak" 2>/dev/null; then
  echo "  crontab backed up to $AUDIT_DIR/crontab_pre_seal_${STAMP}.bak"
else
  echo "  (no existing crontab)"
fi

if [[ -f "$REPO/infra/cron/crontab.sealed" ]]; then
  crontab "$REPO/infra/cron/crontab.sealed"
  echo "  installed infra/cron/crontab.sealed"
fi

echo "[$STAMP] sealed mode applied"
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'eatlq-|NAMES' || true
