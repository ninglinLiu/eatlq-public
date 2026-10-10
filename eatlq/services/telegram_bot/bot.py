"""
EATLQ Telegram Bot
──────────────────
• 轮询 Telegram 接收 /命令（/status /positions /daily /alpha /shadow /help）
• 监控 Redis 实时推送：PANIC 变化、新成交、每日日结（UTC 08:00）、Alpha 工厂小时报
• 去重：同内容在 TTL 内不重复发送
• 完全独立于 executor，不修改任何执行逻辑
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tgbot")

# ── Config ───────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SYSTEM_PANIC_KEY = os.environ.get("SYSTEM_PANIC_KEY", "SYSTEM_PANIC").strip() or "SYSTEM_PANIC"
ALPHA_ARTIFACT_ROOT = Path(os.environ.get("ALPHA_ARTIFACT_ROOT", "/artifacts/audits"))
SPRINT075_DIR = ALPHA_ARTIFACT_ROOT.parent / "logs" / "sprint075"
SPRINT077_DIR = ALPHA_ARTIFACT_ROOT.parent / "logs" / "sprint077"

POLL_INTERVAL_SEC = int(os.environ.get("TG_POLL_INTERVAL_SEC", "10"))
TRADE_COOLDOWN_SEC = int(os.environ.get("TG_TRADE_COOLDOWN_SEC", "120"))
ALPHA_INTERVAL_SEC = int(os.environ.get("TG_ALPHA_INTERVAL_SEC", "3600"))
DAILY_HOUR_UTC = int(os.environ.get("TG_DAILY_HOUR_UTC", "8"))
ENABLE_LEGACY_EXEC_TRADE_PUSH = os.environ.get(
    "TG_ENABLE_LEGACY_EXEC_TRADE_PUSH", "0"
).strip().lower() in ("1", "true", "yes", "on")
ENABLE_LEGACY_CANARY_PUSH = os.environ.get(
    "TG_ENABLE_LEGACY_CANARY_PUSH", "0"
).strip().lower() in ("1", "true", "yes", "on")

r = redis.from_url(REDIS_URL, decode_responses=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sf(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _si(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _jl(raw: Any) -> Optional[Dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _short(addr: str, n: int = 16) -> str:
    s = (addr or "").strip()
    return s[:n] + "…" if len(s) > n else s


# ── Telegram API ──────────────────────────────────────────────────────────────
import urllib.error
import urllib.request


def _tg(method: str, payload: Dict[str, Any]) -> Optional[Dict]:
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("Telegram %s HTTP %s: %s", method, e.code, e.read().decode()[:200])
    except Exception as e:
        log.warning("Telegram %s failed: %s", method, e)
    return None


def send(text: str, chat_id: str = "", disable_preview: bool = True) -> None:
    cid = chat_id or CHAT_ID
    if not cid or not text:
        return
    # Telegram message limit: 4096 chars
    if len(text) > 4000:
        text = text[:3990] + "\n…(截断)"
    _tg(
        "sendMessage",
        {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        },
    )


def _authorized(chat_id: str) -> bool:
    return bool(CHAT_ID) and str(chat_id).strip() == str(CHAT_ID).strip()


def _deny_if_unauthorized(chat_id: str) -> bool:
    """Defense-in-depth for control cmds. Prefer silent drop in main() for privacy."""
    if _authorized(chat_id):
        return False
    # Do not reply — avoid confirming the bot works to strangers.
    log.warning("Unauthorized control command from chat %s", chat_id)
    return True


def get_updates(offset: int = 0) -> List[Dict]:
    result = _tg("getUpdates", {"offset": offset, "timeout": 8, "limit": 20})
    if result and result.get("ok"):
        return result.get("result", [])
    return []


# ── Deduplication (Redis-backed) ──────────────────────────────────────────────
def _already_sent(key: str, content: str, ttl: int) -> bool:
    h = hashlib.sha256(content.encode()).hexdigest()[:20]
    rk = f"tgbot:dedup:{key}"
    if r.get(rk) == h:
        return True
    r.set(rk, h, ex=ttl)
    return False


# ── Data readers ──────────────────────────────────────────────────────────────
def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def read_executor_status() -> Dict:
    panic = r.get(SYSTEM_PANIC_KEY) or ""
    pools = r.smembers("positions:open_pools") or r.smembers("exec:open_pools") or set()
    day = _day_key()
    daily = _jl(r.get(f"exec:daily_eval:{day}")) or {}
    dry_run_flag = daily.get("dry_run", True)
    trades_raw = r.lrange("exec:trades", 0, 4) or []
    recent_trades = [_jl(t) for t in trades_raw if _jl(t)]
    eth_price = _sf(r.get("exec:eth_price_usd"), 2000.0)
    return {
        "panic": panic,
        "open_count": len(pools),
        "daily": daily,
        "recent_trades": recent_trades,
        "day": day,
        "dry_run": dry_run_flag,
        "eth_price_usd": eth_price,
    }


def read_control_snapshot() -> Dict[str, Any]:
    try:
        pending = r.xpending("signals", "execution_group")
    except Exception:
        pending = {}
    try:
        sig_len = r.xlen("signals")
    except Exception:
        sig_len = -1
    return {
        "panic": r.get(SYSTEM_PANIC_KEY) or "",
        "open_positions": len(r.smembers("positions:open_pools") or set()),
        "pending_buys": len(r.smembers("positions:pending_buy_set") or set()),
        "signals_len": sig_len,
        "signals_pending": pending,
        "eth_price_usd": _sf(r.get("exec:eth_price_usd"), 0.0),
    }


def read_open_positions() -> List[Dict]:
    pools = r.smembers("positions:open_pools") or r.smembers("exec:open_pools") or set()
    now_ms = int(time.time() * 1000)
    positions = []
    for pool in sorted(pools):
        pos = r.hgetall(f"positions:{pool}") or _jl(r.get(f"exec:pos:{pool}")) or {}
        if not pos:
            continue
        opened_ms = _si(pos.get("opened_at_ms"))
        age_min = (now_ms - opened_ms) / 60000 if opened_ms else 0
        entry_wei = _si(pos.get("entry_eth_wei"))
        eth_price = _sf(r.get("exec:eth_price_usd"), 2000.0)
        entry_usd = entry_wei / 1e18 * eth_price
        symbol = pos.get("non_weth_symbol") or pos.get("token_symbol") or "?"
        positions.append(
            {
                "pool": str(pool),
                "symbol": symbol,
                "entry_usd": round(entry_usd, 2),
                "age_min": round(age_min, 0),
            }
        )
    return positions


def read_shadow_funnel() -> Dict:
    rows = r.lrange("shadow:signals", 0, 199) or []
    total = accepted = rejected = closed_n = 0
    by_reject: Dict[str, int] = {}
    for raw in rows:
        rec = _jl(raw)
        if not rec:
            continue
        total += 1
        rej = (rec.get("reject_reason") or "").strip()
        if rej:
            rejected += 1
            by_reject[rej] = by_reject.get(rej, 0) + 1
        else:
            accepted += 1
        if rec.get("status") == "closed":
            closed_n += 1
    return {
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "closed": closed_n,
        "by_reject": by_reject,
    }


def read_alpha_summary() -> Dict:
    rows = []
    for pattern in [
        str(SPRINT075_DIR / "*_state.json"),
        str(SPRINT077_DIR / "*_state.json"),
    ]:
        for fp in sorted(glob.glob(pattern)):
            try:
                d = json.loads(Path(fp).read_text())
                stats = d.get("stats") or {}
                eq = _sf(d.get("equity"), 100.0)
                n = _si(stats.get("forward_n"))
                wins = _si(stats.get("wins"))
                losses = _si(stats.get("losses"))
                gain = _sf(stats.get("total_gain"))
                loss = _sf(stats.get("total_loss_abs"))
                wr = wins / n if n > 0 else 0.0
                pf = min(gain / loss, 99.0) if loss > 0 else (99.0 if gain > 0 else 0.0)
                harness = "top10" if "sprint075" in fp else "exp"
                rows.append(
                    {
                        "sid": d.get("strategy_id", ""),
                        "harness": harness,
                        "equity": eq,
                        "roi_pct": round(eq - 100, 3),
                        "n": n,
                        "wins": wins,
                        "losses": losses,
                        "wr": round(wr, 3),
                        "pf": round(pf, 2),
                        "gain": round(gain, 3),
                        "loss_abs": round(loss, 3),
                        "family": d.get("family", ""),
                        "exit_horizon": d.get("exit_horizon", ""),
                        "open_count": len(d.get("open_positions") or {}),
                        "last_run": (d.get("last_run") or "")[:16],
                    }
                )
            except Exception as e:
                log.debug("Failed reading %s: %s", fp, e)
    if not rows:
        return {}
    n_pos = sum(1 for row in rows if row["equity"] > 100)
    avg_eq = sum(row["equity"] for row in rows) / len(rows)
    total_n = sum(row["n"] for row in rows)
    top3 = sorted(rows, key=lambda row: row["equity"], reverse=True)[:3]
    worst3 = sorted(rows, key=lambda row: row["equity"])[:3]
    return {
        "total": len(rows),
        "n_positive": n_pos,
        "avg_equity": round(avg_eq, 4),
        "total_fwd_trades": total_n,
        "top3": top3,
        "worst3": worst3,
        "all_rows": rows,
    }


# ── Canary (sprint097 fee=100 live forward test) ─────────────────────────────
def read_canary() -> Dict:
    return _jl(r.get("canary:state")) or {}


def read_canary_detector() -> Dict:
    return _jl(r.get("canary:detector")) or {}


def read_overall() -> Dict:
    return _jl(r.get("eatlq:overall")) or {}


def _canary_loop_fresh(state: Dict) -> bool:
    # loop publishes a heartbeat every cycle (~5min). Stale > 12min => loop down.
    hb = _si(state.get("loop_alive_ts"))
    return hb > 0 and (time.time() - hb) < 720


# ── Command handlers ──────────────────────────────────────────────────────────
def cmd_status(chat_id: str) -> None:
    st = read_executor_status()
    panic = st["panic"]
    daily = st["daily"]
    mode = "试运行 🔍" if st["dry_run"] else "🔴 实盘 💰"
    net = _sf(daily.get("net_pnl_usd"))
    gross = _sf(daily.get("gross_pnl_usd"))
    n_trades = _si(daily.get("trade_count"))
    wr = _sf(daily.get("win_rate"))
    wins = _si(daily.get("win_count"))
    losses = _si(daily.get("loss_count"))
    pnl_emoji = "🟢" if net >= 0 else "🔴"
    panic_line = f"🚨 <b>PANIC:</b> {panic[:80]}" if panic else "✅ 系统正常，无熔断"
    signal_age = ""
    try:
        latest_sig = r.xrevrange("signals", count=1)
        if latest_sig:
            sid, fields = latest_sig[0]
            ts_ms = int(sid.split("-")[0])
            age_s = (int(time.time() * 1000) - ts_ms) // 1000
            signal_age = f"最近信号：{age_s // 60} 分钟前"
    except Exception:
        pass
    # canary (fee=100 live forward test) — the only currently-armed real-money line
    cs = read_canary()
    canary_line = ""
    if cs:
        loop_tag = "🟢运行中" if _canary_loop_fresh(cs) else "🔴循环已停"
        halt_tag = " ⛔已熔断" if cs.get("halted") else ""
        cpnl = _sf(cs.get("realized_pnl_usd"))
        cpe = "🟢" if cpnl >= 0 else "🔴"
        canary_line = (
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>🎣 Canary (fee=100 实盘前向)</b> {loop_tag}{halt_tag}\n"
            f"成交 {_si(cs.get('trades_done'))}/{cs.get('caps',{}).get('max_trades','?')} | "
            f"投入 ${_sf(cs.get('deployed_usd')):.0f}/${cs.get('caps',{}).get('total_usd','?')} | "
            f"持仓 {_si(cs.get('open_count'))}\n"
            f"{cpe} 已实现：{cpnl:+.4f} USD\n"
        )
    text = (
        f"📟 <b>系统状态</b> [{st['day']}]\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{panic_line}\n"
        f"模式：{mode}\n"
        f"当前持仓：{st['open_count']} 个\n"
        f"{canary_line}"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>今日成绩（旧 executor）</b>\n"
        f"成交：{n_trades} 笔 | 盈 {wins} / 亏 {losses} | 胜率 {wr:.1f}%\n"
        f"{pnl_emoji} 净盈亏：{net:+.4f} USD（毛 {gross:+.4f}）\n"
        f"ETH 参考价：${st['eth_price_usd']:.0f}\n"
        f"{'━━━━━━━━━━━━━━━━' if signal_age else ''}\n"
        f"{signal_age}\n"
        f"<i>详情：/canary（当下）·/overall（总体）</i>"
    ).rstrip("\n")
    send(text, chat_id)


def cmd_canary(chat_id: str) -> None:
    cs = read_canary()
    if not cs:
        send("📭 <b>Canary 未上报状态</b>\n循环可能未启动或 Redis 桥接未运行", chat_id)
        return
    det = read_canary_detector()
    loop_tag = "🟢 运行中" if _canary_loop_fresh(cs) else "🔴 循环已停（需重启）"
    halt_tag = "⛔ 已熔断（累计亏损触顶）" if cs.get("halted") else "✅ 未熔断"
    caps = cs.get("caps", {})
    cpnl = _sf(cs.get("realized_pnl_usd"))
    cpe = "🟢" if cpnl >= 0 else "🔴"
    open_lines = ""
    for o in (cs.get("open") or []):
        open_lines += (f"  • <code>{_short(o.get('pool',''),18)}</code> "
                       f"${o.get('size_usd')} | 还剩 {o.get('hold_left_min')}min\n")
    open_block = f"<b>持仓：</b>\n{open_lines}" if open_lines else "持仓：无\n"
    rej = det.get("top_rejects", {})
    rej_str = " | ".join(f"{k}:{v}" for k, v in list(rej.items())[:4]) or "—"
    text = (
        f"🎣 <b>Canary — fee=100 实盘前向验证</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"循环：{loop_tag}\n"
        f"风控：{halt_tag}\n"
        f"模式：🔴 真钱 LIVE\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"成交：{_si(cs.get('trades_done'))} / {caps.get('max_trades','?')} 笔\n"
        f"投入：${_sf(cs.get('deployed_usd')):.1f} / ${caps.get('total_usd','?')}\n"
        f"{cpe} 已实现盈亏：{cpnl:+.4f} USD（熔断线 ${caps.get('max_loss_usd','?')}）\n"
        f"单笔 ${caps.get('per_trade_usd','?')} · 持有 {caps.get('hold_h','?')}h · "
        f"最多 {caps.get('max_concurrent','?')} 并发\n"
        f"{open_block}"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>探测器漏斗</b>（最近 {_si(det.get('rows_recent'))} 池）\n"
        f"fee=100 出现：{_si(det.get('fee100_seen'))} | CLEAN：{_si(det.get('clean_total'))}\n"
        f"主要拒绝：{rej_str}\n"
        f"最后扫描：{(det.get('last_scan') or '—')[:16]}\n"
        f"<i>fee=100 onset 稀有(~1/天)，长时间无信号属正常，不降标准。/overall 看总体</i>"
    )
    send(text, chat_id)


def cmd_overall(chat_id: str) -> None:
    o = read_overall()
    if not o:
        send("📭 <b>总体结论未上报</b>", chat_id)
        return
    bullets = "\n".join(f"• {b}" for b in (o.get("bullets") or []))
    text = (
        f"🧭 <b>总体情况（不要忽视）</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>{o.get('headline','')}</b>\n"
        f"<i>{o.get('regime','')}</i>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{bullets}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>当前策略</b>\n{o.get('strategy','')}\n"
        f"<i>更新：{(o.get('updated') or '')[:16]}</i>"
    )
    send(text, chat_id)


def cmd_positions(chat_id: str) -> None:
    positions = read_open_positions()
    if not positions:
        send("📭 <b>当前没有持仓</b>", chat_id)
        return
    lines = [f"📌 <b>当前持仓 ({len(positions)} 个)</b>", "━━━━━━━━━━━━━━━━"]
    for pos in positions:
        lines.append(
            f"• <code>{_short(pos['pool'])}</code>\n"
            f"  {pos['symbol']} | ${pos['entry_usd']:.2f} | 持仓 {pos['age_min']:.0f} 分钟"
        )
    send("\n".join(lines), chat_id)


def cmd_daily(chat_id: str) -> None:
    st = read_executor_status()
    daily = st["daily"]
    if not daily:
        send("📭 今日暂无日结数据（executor 可能尚未生成）", chat_id)
        return
    mode = "试运行 🔍" if daily.get("dry_run", True) else "实盘 💰"
    net = _sf(daily.get("net_pnl_usd"))
    gross = _sf(daily.get("gross_pnl_usd"))
    fee = _sf(daily.get("fee_cost_usd"))
    slip = _sf(daily.get("slippage_cost_usd"))
    n = _si(daily.get("trade_count"))
    wins = _si(daily.get("win_count"))
    losses = _si(daily.get("loss_count"))
    wr = _sf(daily.get("win_rate"))
    avg_win = _sf(daily.get("avg_win_usd"))
    avg_loss = _sf(daily.get("avg_loss_usd"))
    pnl_emoji = "🟢" if net >= 0 else "🔴"
    verdict = "净利润为正，继续观察" if net > 0 else "净利润为负，需要关注"
    text = (
        f"📊 <b>今日交易日结</b> [{st['day']}] ({mode})\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"毛利润：{gross:+.4f} USD\n"
        f"估算成本：-{fee + slip:.4f} USD\n"
        f"  (手续费 {fee:.4f} + 滑点 {slip:.4f})\n"
        f"{pnl_emoji} <b>净利润：{net:+.4f} USD</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"成交：{n} 笔 | 盈 {wins} / 亏 {losses} | 胜率 {wr:.1f}%\n"
        f"平均盈：{avg_win:+.4f} | 平均亏：{avg_loss:+.4f} USD\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{'🟢' if net > 0 else '🔴'} {verdict}\n"
        f"<i>⚠️ 成本为估算值，非真实链上数据</i>"
    )
    send(text, chat_id)


def cmd_alpha(chat_id: str) -> None:
    a = read_alpha_summary()
    if not a:
        send("⚠️ <b>Alpha 数据不可用</b>\nstate files 未挂载或为空", chat_id)
        return
    top3_lines = []
    for i, row in enumerate(a["top3"]):
        wr_pct = row["wr"] * 100
        tag = "[top10]" if row["harness"] == "top10" else "[exp]"
        top3_lines.append(
            f"  {i+1}. {row['sid']} {tag}\n"
            f"     equity={row['equity']:.3f} n={row['n']} "
            f"wr={wr_pct:.0f}% pf={row['pf']:.2f}"
        )
    worst_line = ""
    if a.get("worst3"):
        w = a["worst3"][0]
        worst_line = f"\n最差：{w['sid']} equity={w['equity']:.3f}"
    all_n = a["total_fwd_trades"]
    all_wins = sum(row["wins"] for row in a.get("all_rows", []))
    overall_wr = (all_wins / all_n * 100) if all_n > 0 else 0
    text = (
        f"🏭 <b>Alpha 工厂快报</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"策略：{a['total']} 条 | 盈利：{a['n_positive']}/{a['total']}\n"
        f"平均权益：{a['avg_equity']:.4f}（起始 100）\n"
        f"Forward 成交：{all_n} 笔 | 综合胜率：{overall_wr:.1f}%\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>Top 3（按权益）：</b>\n"
        + "\n".join(top3_lines)
        + worst_line
        + "\n━━━━━━━━━━━━━━━━\n"
        f"<i>完整排行见 Dashboard → 阿尔法工厂\n"
        f"http://127.0.0.1:8090 (需 SSH 转发)</i>"
    )
    send(text, chat_id)


def cmd_shadow(chat_id: str) -> None:
    f = read_shadow_funnel()
    reject_parts = " | ".join(
        f"{k}:{v}"
        for k, v in sorted(f["by_reject"].items(), key=lambda x: -x[1])[:5]
    )
    accept_pct = (
        f"{f['accepted']/f['total']*100:.0f}%" if f["total"] > 0 else "—"
    )
    text = (
        f"👁 <b>Shadow 信号漏斗（最近 200 条）</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"总计：{f['total']}\n"
        f"接受：{f['accepted']} ({accept_pct}) | 拒绝：{f['rejected']}\n"
        f"已关闭：{f['closed']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"主要拒绝原因：\n{reject_parts or '无'}"
    )
    send(text, chat_id)


def cmd_console(chat_id: str, text: str = "") -> None:
    if _deny_if_unauthorized(chat_id):
        return
    s = read_control_snapshot()
    pending = s.get("signals_pending") or {}
    if isinstance(pending, dict):
        pending_n = pending.get("pending", pending.get("count", 0))
    elif isinstance(pending, (list, tuple)) and pending:
        pending_n = pending[0]
    else:
        pending_n = 0
    panic = s["panic"]
    state = "⛔ 已暂停(PANIC)" if panic else "✅ 可接收买入"
    send(
        f"🎛 <b>Keystone 控制台</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"状态：{state}\n"
        f"原因：{panic[:180] if panic else '—'}\n"
        f"持仓：{s['open_positions']} | pending_buy：{s['pending_buys']}\n"
        f"signals：len={s['signals_len']} pending={pending_n}\n"
        f"ETH参考价：${s['eth_price_usd']:.0f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>控制:</b>\n"
        f"/pause [原因] — 紧急暂停新买入\n"
        f"/resume confirm — 清除 PANIC,恢复买入闸门\n"
        f"/status /positions — 查看状态",
        chat_id,
    )


def cmd_pause(chat_id: str, text: str = "") -> None:
    if _deny_if_unauthorized(chat_id):
        return
    reason = text.partition(" ")[2].strip() or "telegram_operator_pause"
    value = f"PAUSED_BY_TELEGRAM:{reason[:180]}"
    r.set(SYSTEM_PANIC_KEY, value)
    send(
        f"⛔ <b>已紧急暂停 Keystone 买入</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{value}\n"
        f"监控/卖出仍会继续运行。\n"
        f"恢复需发送：<code>/resume confirm</code>",
        chat_id,
    )


def cmd_resume(chat_id: str, text: str = "") -> None:
    if _deny_if_unauthorized(chat_id):
        return
    parts = text.split()
    if len(parts) < 2 or parts[1].lower() != "confirm":
        send(
            "⚠️ 恢复买入需要显式确认:\n"
            "<code>/resume confirm</code>\n"
            "这只会清除 PANIC,不会绕过 executor 的任何风控/蜜罐门。",
            chat_id,
        )
        return
    old = r.get(SYSTEM_PANIC_KEY) or ""
    r.delete(SYSTEM_PANIC_KEY)
    send(
        f"✅ <b>已清除 PANIC,买入闸门恢复</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"previous={old[:180] or 'none'}\n"
        f"executor 仍会执行 external risk / friction / real-gas sell sim / 动态仓位约束。",
        chat_id,
    )


def cmd_help(chat_id: str) -> None:
    text = (
        "🤖 <b>EATLQ Bot 指令</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "/status — 系统整体状态（含 Canary 摘要）\n"
        "/canary — 🎣 fee=100 实盘前向（当下情况）\n"
        "  循环/风控/成交/投入/持仓/探测漏斗\n"
        "/overall — 🧭 总体结论（不要忽视）\n"
        "  当前 regime 的真实成本回测裁决\n"
        "/positions — 旧 executor 开仓明细\n"
        "/daily — 今日交易日结\n"
        "/alpha — Alpha 工厂排行快报\n"
        "/shadow — Shadow 信号漏斗统计\n"
        "/console — 🎛 控制台/闸门状态\n"
        "/pause [原因] — 紧急暂停新买入\n"
        "/resume confirm — 恢复买入闸门\n"
        "/help — 本帮助\n"
        "━━━━━━━━━━━━━━━━\n"
        "<b>自动推送（无需指令）：</b>\n"
        "• 🎣 Canary 真钱成交（BUY/SELL，立即）\n"
        "• ⚠️ Canary 循环心跳超时\n"
        "• SYSTEM_PANIC 触发/清除（立即）\n"
        "• Alpha 工厂小时更新 / 每日日结"
    )
    send(text, chat_id)


COMMANDS: Dict[str, Any] = {
    "/status": cmd_status,
    "/canary": cmd_canary,
    "/overall": cmd_overall,
    "/positions": cmd_positions,
    "/daily": cmd_daily,
    "/alpha": cmd_alpha,
    "/shadow": cmd_shadow,
    "/console": cmd_console,
    "/pause": cmd_pause,
    "/panic": cmd_pause,
    "/resume": cmd_resume,
    "/help": cmd_help,
    "/start": cmd_help,
}


# ── Event monitor (proactive push) ───────────────────────────────────────────
_state: Dict[str, Any] = {
    "last_trade_ts": 0.0,
    "last_alpha_ts": 0.0,
    "last_daily_day": "",
    "last_panic": None,  # None = first run
}


def monitor_events() -> None:
    now = time.time()
    utc = datetime.now(timezone.utc)
    day = utc.strftime("%Y-%m-%d")

    # 1. PANIC state change (immediate, no dedup needed – only on change)
    panic = r.get(SYSTEM_PANIC_KEY) or ""
    if _state["last_panic"] is None:
        _state["last_panic"] = panic  # init, don't fire on first poll
    elif panic != _state["last_panic"]:
        if panic:
            send(
                f"🚨 <b>SYSTEM_PANIC 已触发!</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{panic[:200]}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"请立即检查 Dashboard 并手动介入！"
            )
        else:
            send("✅ <b>SYSTEM_PANIC 已清除</b>\n系统恢复正常运行")
        _state["last_panic"] = panic

    # 2. Legacy dry-run executor trade list.
    #
    # Disabled by default: exec:trades is a historical dry-run list and is no
    # longer written by the live Keystone executor. Re-reading its head caused
    # the bot to resend the same stale "BUY [试运行]" every time the short dedup
    # TTL expired. Live Keystone BUY/SELL alerts are sent directly by executor.
    if ENABLE_LEGACY_EXEC_TRADE_PUSH and now - _state["last_trade_ts"] > TRADE_COOLDOWN_SEC:
        trades = r.lrange("exec:trades", 0, 0)
        if trades:
            t = _jl(trades[0])
            if t:
                ts = _si(t.get("ts"))
                pool_short = _short(t.get("pool") or "", 12)
                dedup_key = f"trade:{ts}:{pool_short}"
                if not _already_sent(dedup_key, dedup_key, ttl=TRADE_COOLDOWN_SEC * 3):
                    action = (t.get("action") or "").upper()
                    pnl = _sf(t.get("pnl_usd"))
                    reason = t.get("reason") or "—"
                    pool_type = t.get("pool_type") or ""
                    dry = bool(t.get("dry_run", True))
                    mode_tag = "[试运行]" if dry else "[实盘]"
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    reason_zh = {
                        "stop_loss": "止损",
                        "take_profit": "止盈",
                        "trailing_stop": "移动止盈",
                        "time_decay": "超时",
                    }.get(reason, reason)
                    text = (
                        f"{emoji} <b>{action}</b> {mode_tag} {pool_type}\n"
                        f"池：<code>{_short(t.get('pool') or '', 20)}</code>\n"
                        f"PnL：{pnl:+.4f} USD\n"
                        f"原因：{reason_zh}"
                    )
                    send(text)
                    _state["last_trade_ts"] = now

    # 2b/2c. Legacy fee=100 canary event/watchdog.
    #
    # Disabled by default: current production path is Keystone executor. The old
    # canary:state heartbeat is stale and caused false "armed_live_loop stopped"
    # alerts even though Keystone live execution is healthy.
    if ENABLE_LEGACY_CANARY_PUSH:
        try:
            ev_raw = r.lrange("canary:events", 0, 0)
            if ev_raw:
                ev = _jl(ev_raw[0]) or {}
                ev_key = f"{ev.get('ts')}|{ev.get('action')}|{ev.get('pool')}"
                if ev_key.strip("|") and not _already_sent("canary_evt", ev_key, ttl=86400):
                    action = (ev.get("action") or "").upper()
                    pnl_raw = ev.get("realized_pnl_usd")
                    emoji = "🟢" if action == "BUY" else ("🟢" if _sf(pnl_raw) >= 0 else "🔴")
                    pnl_line = ""
                    if action == "SELL":
                        pnl_line = f"PnL：{_sf(pnl_raw):+.4f} USD\n"
                    send(
                        f"{emoji} <b>Canary {action}</b> 🔴真钱 [fee=100]\n"
                        f"池：<code>{_short(ev.get('pool') or '', 22)}</code>\n"
                        f"金额：${ev.get('size_usd','?')}\n"
                        f"{pnl_line}"
                        f"<i>{ev.get('note','')}</i>\n"
                        f"详情 /canary"
                    )
        except Exception as exc:
            log.debug("canary event push failed: %s", exc)

        try:
            cs = read_canary()
            if cs and not _canary_loop_fresh(cs):
                if not _already_sent("canary_loopdown", str(int(time.time() // 1800)), ttl=1800):
                    send("⚠️ <b>Canary 循环心跳超时</b>\n探测/执行循环可能已停止，请检查并重启 armed_live_loop。")
        except Exception:
            pass

    # 3. Daily summary at DAILY_HOUR_UTC
    if day != _state["last_daily_day"] and utc.hour >= DAILY_HOUR_UTC:
        _state["last_daily_day"] = day
        log.info("Sending daily summary for %s", day)
        cmd_daily(CHAT_ID)

    # 4. Alpha factory hourly update (content-deduped)
    if now - _state["last_alpha_ts"] > ALPHA_INTERVAL_SEC:
        _state["last_alpha_ts"] = now
        a = read_alpha_summary()
        if a:
            content = f"{a['n_positive']}/{a['total']}/{a['avg_equity']:.3f}"
            if not _already_sent("alpha_hourly", content, ttl=ALPHA_INTERVAL_SEC):
                top1 = a["top3"][0] if a["top3"] else {}
                all_n = a["total_fwd_trades"]
                all_wins = sum(row["wins"] for row in a.get("all_rows", []))
                wr = (all_wins / all_n * 100) if all_n > 0 else 0
                text = (
                    f"🏭 <b>Alpha 工厂小时更新</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"盈利策略：{a['n_positive']}/{a['total']}\n"
                    f"平均权益：{a['avg_equity']:.4f} | "
                    f"综合胜率：{wr:.1f}%\n"
                    f"Forward 成交：{all_n} 笔\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"领跑：{top1.get('sid','—')} "
                    f"equity={top1.get('equity',100):.3f} "
                    f"n={top1.get('n',0)}"
                )
                send(text)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing — bot cannot start. "
            "Set both env vars to enable."
        )
        sys.exit(1)

    log.info("Bot starting (chat_id=%s)", CHAT_ID)
    send(
        "🤖 <b>EATLQ Bot 已启动</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "发送 /help 查看所有指令\n"
        "已激活自动推送：成交通知 | PANIC | Alpha 小时报 | 每日日结"
    )

    offset = 0
    while True:
        try:
            updates = get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = (
                    upd.get("message")
                    or upd.get("edited_message")
                    or {}
                )
                text = (msg.get("text") or "").strip()
                cid = str((msg.get("chat") or {}).get("id", ""))
                if not text or not cid:
                    continue
                # Global allowlist: only TELEGRAM_CHAT_ID may use any command.
                # Unauthorized chats are dropped silently (no reply).
                if not _authorized(cid):
                    log.warning(
                        "Dropping message from unauthorized chat %s: %s",
                        cid,
                        text[:80],
                    )
                    continue
                cmd = text.split()[0].split("@")[0].lower()
                handler = COMMANDS.get(cmd)
                if handler:
                    log.info("Command %s from chat %s", cmd, cid)
                    try:
                        if getattr(handler, "__code__", None) and handler.__code__.co_argcount >= 2:
                            handler(cid, text)
                        else:
                            handler(cid)
                    except Exception as exc:
                        log.warning("Handler %s failed: %s", cmd, exc)
                        send(f"⚠️ 指令执行出错：{exc}", cid)
                else:
                    send(
                        f"❓ 未知指令：<code>{cmd}</code>\n发送 /help 查看可用指令",
                        cid,
                    )

            monitor_events()

        except redis.exceptions.ConnectionError as e:
            log.warning("Redis disconnected: %s — retrying in 5s", e)
            time.sleep(5)
        except Exception as exc:
            log.warning("Main loop error: %s", exc)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
