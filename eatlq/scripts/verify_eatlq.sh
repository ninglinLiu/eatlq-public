#!/usr/bin/env bash
# EATLQ 一键验证（适合 VPS / 本地，仓库根目录执行）
# 用法:
#   cd /opt/EATLQ && bash scripts/verify_eatlq.sh
# 可选环境变量:
#   EATLQ_ROOT   仓库根目录（默认: 脚本所在目录的上一级）
#   COMPOSE_FILE 覆盖 compose 路径（默认: infra/compose/compose.yaml）

set -euo pipefail

ROOT="${EATLQ_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/compose.yaml}"
DC=(docker compose -f "$COMPOSE_FILE")

echo "=========================================="
echo " EATLQ 验证  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " ROOT: $ROOT"
echo "=========================================="
echo ""

echo "=== Git ==="
git rev-parse HEAD 2>/dev/null || echo "(not a git repo)"
echo ""

echo "=== Docker Compose 服务状态 ==="
"${DC[@]}" ps 2>/dev/null || true
echo ""

echo "=== Redis: 聪明钱钱包数量 ==="
"${DC[@]}" exec redis redis-cli ZCARD wallet:smart_money 2>/dev/null || echo "N/A"
"${DC[@]}" exec redis redis-cli ZCARD wallet:sniper 2>/dev/null || echo "N/A"
echo ""

echo "=== Redis: shadow:signals ==="
SHADOW_LEN=$("${DC[@]}" exec redis redis-cli --raw LLEN shadow:signals 2>/dev/null | tr -d '\r\n' || echo 0)
echo "LLEN shadow:signals = $SHADOW_LEN"
if [ "${SHADOW_LEN:-0}" -gt 0 ] 2>/dev/null; then
  echo "--- 最新 1 条（LPUSH 在 index 0）---"
  "${DC[@]}" exec redis redis-cli LRANGE shadow:signals 0 0 2>/dev/null || true
  echo "--- status 统计（LPUSH：index 0 最新；大列表抽样或全量）---"
  count_json() {
    echo "$1" | grep -c "$2" || true
  }
  if [ "${SHADOW_LEN:-0}" -le 5000 ] 2>/dev/null; then
    RAW=$("${DC[@]}" exec redis redis-cli --raw LRANGE shadow:signals 0 -1 2>/dev/null || true)
    CLOSED=$(count_json "$RAW" '"status": "closed"')
    OPEN=$(count_json "$RAW" '"status": "open"')
    CLOSED=${CLOSED:-0}
    OPEN=${OPEN:-0}
    echo "全量: closed=$CLOSED open=$OPEN"
  else
    # 列表上限 50000：全量 grep 太重；抽「最新 2000」+「最旧 2000」（最旧更应已 closed）
    HEAD=$("${DC[@]}" exec redis redis-cli --raw LRANGE shadow:signals 0 1999 2>/dev/null || true)
    TAIL=$("${DC[@]}" exec redis redis-cli --raw LRANGE shadow:signals -2000 -1 2>/dev/null || true)
    H_C=$(count_json "$HEAD" '"status": "closed"'); H_O=$(count_json "$HEAD" '"status": "open"')
    T_C=$(count_json "$TAIL" '"status": "closed"'); T_O=$(count_json "$TAIL" '"status": "open"')
    echo "抽样 newest 2000: closed=${H_C:-0} open=${H_O:-0}"
    echo "抽样 oldest 2000:  closed=${T_C:-0} open=${T_O:-0}  (shadow-closer 正常时此处 closed 应较多)"
  fi
fi
echo ""

echo "=== Redis: 已发现池数量（KEYS 数量，生产环境大 keyspace 慎用 SCAN）==="
POOL_N=$("${DC[@]}" exec redis redis-cli --scan --pattern 'pool:discovered:*' 2>/dev/null | wc -l | tr -d ' ')
echo "pool:discovered:* count ≈ $POOL_N"
if [ "${POOL_N:-0}" -gt 0 ] 2>/dev/null; then
  K=$("${DC[@]}" exec redis redis-cli --scan --pattern 'pool:discovered:*' 2>/dev/null | head -1)
  if [ -n "$K" ]; then
    echo "示例 key: $K"
    echo "--- HGETALL (检查 created_at_sec) ---"
    "${DC[@]}" exec redis redis-cli HGET "$K" created_at_sec 2>/dev/null || true
    "${DC[@]}" exec redis redis-cli HGET "$K" discovered_at_ms 2>/dev/null || true
  fi
fi
echo ""

echo "=== Redis: exec:cap_hit_count（若存在）==="
"${DC[@]}" exec redis redis-cli GET exec:cap_hit_count 2>/dev/null || echo "(nil)"
echo ""

echo "=== 最近日志: executor / strategy / shadow-closer（各 25 行）==="
"${DC[@]}" logs executor --tail=25 2>/dev/null || true
echo "--- strategy ---"
"${DC[@]}" logs strategy --tail=25 2>/dev/null || true
echo "--- shadow-closer ---"
"${DC[@]}" logs shadow-closer --tail=25 2>/dev/null || true
echo ""

echo "=== Python: validate_sm_pipeline.py ==="
"${DC[@]}" exec -e REDIS_URL=redis://redis:6379/0 strategy \
  python3 /app/scripts/validate_sm_pipeline.py 2>/dev/null || echo "(strategy container missing or script failed)"
echo ""

echo "=== Python: shadow_report.py（需 numpy）==="
if "${DC[@]}" exec strategy python3 -c "import numpy" 2>/dev/null; then
  "${DC[@]}" exec -e REDIS_URL=redis://redis:6379/0 strategy \
    python3 /app/scripts/shadow_report.py 2>/dev/null || true
else
  echo "提示: strategy 容器内无 numpy，可执行: docker compose -f $COMPOSE_FILE exec strategy pip install numpy -q"
fi
echo ""

echo "=========================================="
echo " 验证结束。若 shadow 长期无 closed，检查:"
echo " 1) shadow-closer 日志是否有 RPC/Redis 错误"
echo " 2) SHADOW_HOLD_MIN 与 signal_ts_ms 是否已满 30min+"
echo " 3) EVM_HTTP_URL / EXECUTOR_RPC_URL 是否可达"
echo "=========================================="
