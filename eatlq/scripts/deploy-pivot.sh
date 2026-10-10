#!/usr/bin/env bash
set -euo pipefail

# EATLQ Strategic Pivot — Deployment Script
# Run on VPS after git pull to deploy the full pivoted stack in DRY_RUN mode.

cd /opt/EATLQ 2>/dev/null || cd "$(dirname "$0")/.."

echo "=== EATLQ Strategic Pivot Deployment ==="
echo ""

# 1. Clean Redis state
echo "[1/5] Cleaning old Redis state..."
docker compose -f infra/compose/compose.yaml exec -T redis redis-cli EVAL "
local keys = redis.call('keys', 'exec:*')
for _, k in ipairs(keys) do redis.call('del', k) end
local panic = redis.call('del', 'SYSTEM_PANIC')
return #keys + panic
" 0 2>/dev/null || echo "  (redis not running yet, skip cleanup)"

# 2. Verify .env has required vars
echo "[2/5] Checking .env configuration..."
ENV_FILE="infra/compose/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "  ERROR: $ENV_FILE not found. Copy from .env.example and configure."
    exit 1
fi

REQUIRED_VARS=("EVM_WS_URL" "EVM_HTTP_URL" "CHAIN_ID")
for var in "${REQUIRED_VARS[@]}"; do
    if ! grep -q "^${var}=" "$ENV_FILE"; then
        echo "  WARNING: $var not set in .env"
    fi
done

# Ensure DRY_RUN is on for initial deployment
if ! grep -q "^EXECUTOR_DRY_RUN=1" "$ENV_FILE"; then
    echo "  NOTE: Setting EXECUTOR_DRY_RUN=1 for safe initial deployment"
    if grep -q "^EXECUTOR_DRY_RUN=" "$ENV_FILE"; then
        sed -i 's/^EXECUTOR_DRY_RUN=.*/EXECUTOR_DRY_RUN=1/' "$ENV_FILE"
    else
        echo "EXECUTOR_DRY_RUN=1" >> "$ENV_FILE"
    fi
fi

echo "  .env OK"

# 3. Build all services
echo "[3/5] Building Docker images..."
docker compose --env-file "$ENV_FILE" -f infra/compose/compose.yaml build \
    --parallel \
    strategy executor dashboard pool-discovery swap-poller wallet-scorer

# 4. Start core infrastructure
echo "[4/5] Starting core infrastructure (redis, clickhouse, postgres)..."
docker compose --env-file "$ENV_FILE" -f infra/compose/compose.yaml up -d \
    redis clickhouse postgres ingestor archiver

echo "  Waiting for services to be healthy..."
sleep 10

# 5. Start application services
echo "[5/5] Starting application services..."
docker compose --env-file "$ENV_FILE" -f infra/compose/compose.yaml \
    --profile strategy --profile executor --profile dashboard \
    up -d --force-recreate

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Services running:"
docker compose --env-file "$ENV_FILE" -f infra/compose/compose.yaml ps --format "table {{.Name}}\t{{.Status}}"
echo ""
echo "Next steps:"
echo "  1. Monitor logs: docker compose -f infra/compose/compose.yaml logs -f pool-discovery strategy executor"
echo "  2. Check pool discovery: docker compose -f infra/compose/compose.yaml exec redis redis-cli KEYS 'pool:discovered:*'"
echo "  3. Check wallet scores: docker compose -f infra/compose/compose.yaml exec redis redis-cli ZCARD wallet:smart_money"
echo "  4. After 2 weeks of DRY_RUN data accumulation:"
echo "     - Run: python scripts/label_dashcam.py"
echo "     - Run: python scripts/retrain_model.py"
echo "     - Set EXECUTOR_DRY_RUN=0 in .env"
echo "     - Restart: docker compose up -d --force-recreate executor"
