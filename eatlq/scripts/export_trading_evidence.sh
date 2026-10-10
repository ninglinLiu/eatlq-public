#!/usr/bin/env bash
# 导出「交易逻辑分析」用证据包（VPS 上运行，打包发给我或贴关键片段）
#
# 用法:
#   cd /opt/EATLQ && bash scripts/export_trading_evidence.sh
#
# 生成目录: evidence/YYYYMMDD_HHMMSS/
# 可将整个目录 zip 后上传，或只把 *.txt / *.jsonl 内容复制到对话里
#
# 可选环境变量:
#   EVIDENCE_DIR   输出根目录（默认: evidence）
#   LOG_LINES      各服务日志 tail 行数（默认: 3000）
#   SIGNALS_COUNT  signals 流拉取条数（默认: 150）
#   SHADOW_SAMPLE  shadow 每条抽样条数（默认: 400）

set -euo pipefail

ROOT="${EATLQ_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/compose.yaml}"
DC=(docker compose -f "$COMPOSE_FILE")

LOG_LINES="${LOG_LINES:-3000}"
SIGNALS_COUNT="${SIGNALS_COUNT:-150}"
SHADOW_SAMPLE="${SHADOW_SAMPLE:-400}"
EVIDENCE_ROOT="${EVIDENCE_DIR:-evidence}"
STAMP=$(date -u +%Y%m%d_%H%M%S)
OUT="${EVIDENCE_ROOT}/${STAMP}"
mkdir -p "$OUT"

echo "输出目录: $OUT"

# --- Redis：需能 exec redis ---
redis_exec() {
  "${DC[@]}" exec redis redis-cli "$@" 2>/dev/null
}

{
  echo "=== 时间(UTC) ==="
  date -u
  echo ""
  echo "=== Git HEAD ==="
  git rev-parse HEAD 2>/dev/null || true
  echo ""
  echo "=== Redis: 关键长度 ==="
  echo -n "exec:trades LLEN: "
  redis_exec LLEN exec:trades || true
  echo -n "shadow:signals LLEN: "
  redis_exec LLEN shadow:signals || true
  echo -n "wallet:smart_money ZCARD: "
  redis_exec ZCARD wallet:smart_money || true
  echo ""
  echo "=== Redis: SYSTEM_PANIC ==="
  redis_exec GET SYSTEM_PANIC || echo "(nil)"
  echo ""
  echo "=== Redis: exec:cap_hit_count ==="
  redis_exec GET exec:cap_hit_count || echo "(nil)"
} > "$OUT/redis_summary.txt"

# exec:trades 全量（JSON 每行一条，便于分析）
redis_exec --raw LRANGE exec:trades 0 -1 > "$OUT/exec_trades.jsonl" 2>/dev/null || true

# shadow：最新 + 最旧各一段（列表过长时避免整表）
redis_exec --raw LRANGE shadow:signals 0 $((SHADOW_SAMPLE - 1)) > "$OUT/shadow_signals_newest.jsonl" 2>/dev/null || true
SHADOW_LEN=$(redis_exec --raw LLEN shadow:signals | tr -d '\r\n' || echo 0)
if [ "${SHADOW_LEN:-0}" -gt "$SHADOW_SAMPLE" ] 2>/dev/null; then
  START=$((SHADOW_LEN - SHADOW_SAMPLE))
  redis_exec --raw LRANGE shadow:signals "$START" -1 > "$OUT/shadow_signals_oldest_sample.jsonl" 2>/dev/null || true
fi

# signals 流：最近 N 条（需 stream 存在）
redis_exec XREVRANGE signals + - COUNT "$SIGNALS_COUNT" > "$OUT/signals_stream_sample.txt" 2>/dev/null || echo "(no signals or error)" > "$OUT/signals_stream_sample.txt"

# market_ticks：最近少量（可能很大，只取 80 条）
redis_exec XREVRANGE market_ticks + - COUNT 80 > "$OUT/market_ticks_tail.txt" 2>/dev/null || true

# 每日评估键
{
  echo "=== exec:daily_eval:* keys ==="
  redis_exec --scan --pattern 'exec:daily_eval:*' 2>/dev/null | head -20
} > "$OUT/redis_daily_eval_keys.txt" 2>/dev/null || true

# Docker 日志（不改容器，只读）
for svc in strategy executor shadow-closer swap-poller pool-discovery; do
  "${DC[@]}" logs "$svc" --tail="$LOG_LINES" > "$OUT/log_${svc}.txt" 2>/dev/null || echo "(no $svc)" > "$OUT/log_${svc}.txt"
done

# .env 中与交易相关的键（脱敏：私钥、API key 打码）
if [ -f infra/compose/.env ]; then
  grep -E '^(EXECUTOR_|STRATEGY_|SMART_|SHADOW_|CHAIN_ID|REDIS_URL|BASE_CAPITAL|EVM_|SIGNALS_|MARKET_|MIN_CONFIDENCE|MAX_POSITION|DRY_RUN|TELEGRAM_)' infra/compose/.env 2>/dev/null | \
    sed -E 's/(PRIVATE_KEY|API_KEY|BOT_TOKEN|PASSWORD)=.*/\1=***REDACTED***/g' > "$OUT/env_trading_redacted.txt" || true
else
  echo "(no infra/compose/.env)" > "$OUT/env_trading_redacted.txt"
fi

# 说明文件
cat > "$OUT/README_ANALYSIS.txt" << 'EOF'
本目录为「交易逻辑分析」证据包。

建议发给我的内容（按优先级）:
1) redis_summary.txt
2) exec_trades.jsonl（若过大可只发最后 200 行）
3) shadow_signals_newest.jsonl + shadow_signals_oldest_sample.jsonl（若有）
4) signals_stream_sample.txt（前 80 行即可）
5) log_strategy.txt / log_executor.txt 中与 BUY、reject、monitor_check、dry_run 相关的片段
6) Telegram：最近 1～2 天的买卖/止损/日报（可复制文字）
7) env_trading_redacted.txt

不要发送: 私钥、完整 .env、含密钥的 URL。
EOF

echo ""
echo "完成: $OUT"
echo "打包(tar，一般系统自带):"
echo "  cd $ROOT && tar -czvf ${EVIDENCE_ROOT}/eatlq_evidence_${STAMP}.tar.gz -C ${EVIDENCE_ROOT} ${STAMP}"
echo "打包(zip，需 apt install -y zip):"
echo "  cd $ROOT && zip -r ${EVIDENCE_ROOT}/eatlq_evidence_${STAMP}.zip ${EVIDENCE_ROOT}/${STAMP}"
ls -la "$OUT"
