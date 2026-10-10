#!/usr/bin/env bash
# Paper-forward: 零真钱跑完整 KEYSTONE 链路
# 用法: cd /opt/EATLQ/infra/compose && bash ../../scripts/paper_forward_keystone.sh
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "$0")/../infra/compose" && pwd)"
cd "$COMPOSE_DIR"

echo "=== KEYSTONE paper-forward 启动 ==="
echo "工作目录: $COMPOSE_DIR"

# 1. 检查 .env
if [[ ! -f .env ]]; then
  echo "ERROR: 缺少 .env — 先 cp .env.example .env 并填入 RPC URL"
  exit 1
fi

# 2. 强制 paper 模式
grep -q '^EXECUTOR_DRY_RUN=' .env && sed -i 's/^EXECUTOR_DRY_RUN=.*/EXECUTOR_DRY_RUN=1/' .env || echo 'EXECUTOR_DRY_RUN=1' >> .env
grep -q '^LIVE_TRADING_MODE=' .env && sed -i 's/^LIVE_TRADING_MODE=.*/LIVE_TRADING_MODE=/' .env || echo 'LIVE_TRADING_MODE=' >> .env

echo "EXECUTOR_DRY_RUN=1, LIVE_TRADING_MODE=空 (paper 模式)"

# 3. 模型检查。默认复用已验证模型；只有 FORCE_TRAIN=1 才重训。
if [[ ! -s /opt/EATLQ/services/signal_bridge/_model.pkl ]]; then
  echo "ERROR: 缺少 /opt/EATLQ/services/signal_bridge/_model.pkl — 先运行 train_model.py"
  exit 1
fi
if [[ "${FORCE_TRAIN:-0}" == "1" ]]; then
  echo "=== 训练 keystone 模型 ==="
  docker compose run --rm --no-deps \
    -v /opt/EATLQ/services/signal_bridge:/app \
    -e CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://clickhouse:8123}" \
    -e CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}" \
    signal-bridge python train_model.py || echo "WARN: train_model 失败，使用已有 _model.pkl"
else
  echo "使用已有 keystone 模型: /opt/EATLQ/services/signal_bridge/_model.pkl"
fi

# 4. 启动核心 + 数据 + 执行链路
echo "=== 启动服务 ==="
docker compose --profile ingestion --profile strategy --profile executor up -d \
  redis clickhouse postgres archiver ingestor pool-discovery swap-poller \
  executor signal-bridge

echo ""
echo "=== 启动完成。观察命令 ==="
echo "  docker compose logs -f signal-bridge executor"
echo "  docker exec eatlq-redis-1 redis-cli XLEN signals"
echo "  docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools"
echo ""
echo "48h 后检查:"
echo "  - signal-bridge 有 REJECT/PASS 日志"
echo "  - executor 有 DRY BUY / DRY SELL"
echo "  - signals_dead_letter 为空或极少"
