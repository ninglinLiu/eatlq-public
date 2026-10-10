#!/usr/bin/env python3
"""Bridge the CSV-based canary state into Redis so the (Docker) Telegram bot and
dashboard can surface it. The canary itself stays decoupled (writes CSV ledgers);
this publisher reads those + the sprint097 verdict and writes compact Redis keys.

Keys written (read by bot + dashboard):
  canary:state     JSON  live caps/progress/open positions + loop heartbeat
  canary:detector  JSON  last detector scan funnel (pools/fee100/clean)
  canary:events    LIST  recent BUY/SELL rows (LPUSH, capped 50) for proactive alerts
  eatlq:overall    JSON  the overall situation narrative (sprint097 conclusion)
"""
from __future__ import annotations
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import redis

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from canary_ledger import read_rows as _ledger_read_rows  # noqa: E402

REDIS_URL = os.environ.get("CANARY_REDIS_URL", "redis://172.18.0.2:6379/0")
LIVE_DIR = os.environ.get("CANARY_LIVE_DIR", "/opt/EATLQ/mvp/diagnostics/live")
STATE = os.path.join(LIVE_DIR, "canary_state.json")
LIVE_LEDGER = os.path.join(LIVE_DIR, "live_ledger.csv")
CAND_LEDGER = os.path.join(LIVE_DIR, "candidates_ledger.csv")

CAPS = {"total_usd": 50, "per_trade_usd": "5-10", "max_trades": 10,
        "max_loss_usd": -20, "hold_h": 2, "max_concurrent": 2}

OVERALL = {
    "updated": datetime.now(timezone.utc).isoformat(),
    "headline": "Base 上唯一通过反自欺门的结构：fee=100(0.01%) 池；其余猎场系统性负期望",
    "regime": "当前 regime (30天 fresh, 326 onset 真实成本回测)",
    "bullets": [
        "全市场 taker onset: EV -23%, 胜率 15% — 系统性负期望",
        "fee=100(0.01%): EV +236%, 胜率 71%, 中位 +199%, 去顶池PF 21 (非单池), 近期变强",
        "0x6ff5 翻转为信号: 它在 fee=100 池 94% 拉、在其它费率层 0% 拉(净砸)",
        "右尾几乎只在控盘池 → 外部 taker 多为出场流动性, 故只押 fee=100",
        "残余风险: 逆向选择/操盘手变招/样本小, 只能用 live canary 前向证伪",
        "供给真实: Base 30天 1952 个 WETH 新池(约65/天); 之前'零onset'是我们管道bug",
    ],
    "strategy": "fee=100 + onset(heat>=5) + 双边可卖(QuoterV2) + 真实成本; 规则A(0x6ff5在场,要求外部买家) / 规则B(无0x6ff5,要求organic); $8买/hold 2h",
}


def _read_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def _read_csv(p):
    try:
        with open(p) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def main():
    r = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=4)
    now = datetime.now(timezone.utc)
    st = _read_json(STATE, {"trades_done": 0, "deployed_usd": 0.0,
                            "realized_pnl_usd": 0.0, "open": [], "traded_pools": []})

    halted = float(st.get("realized_pnl_usd", 0)) <= CAPS["max_loss_usd"]
    state = {
        "ts": now.isoformat(),
        "mode": "LIVE",
        "loop_alive_ts": int(time.time()),
        "trades_done": st.get("trades_done", 0),
        "deployed_usd": round(float(st.get("deployed_usd", 0)), 2),
        "realized_pnl_usd": round(float(st.get("realized_pnl_usd", 0)), 4),
        "open_count": len(st.get("open", [])),
        "open": [{"pool": o.get("pool"), "alt": o.get("alt"),
                  "size_usd": o.get("size_usd"),
                  "hold_left_min": max(0, round((o.get("sell_at", 0) - time.time()) / 60))}
                 for o in st.get("open", [])],
        "halted": halted,
        "caps": CAPS,
    }
    r.set("canary:state", json.dumps(state, ensure_ascii=False))

    # detector funnel from the candidates ledger (recent window).
    # Use the schema-safe reader so CLEAN rows are never misparsed (see canary_ledger).
    cands = _ledger_read_rows(CAND_LEDGER)
    recent = cands[-300:]
    fee100 = [c for c in recent if str(c.get("fee")) == "100"]
    clean = [c for c in cands if c.get("verdict") == "CLEAN"]
    last_ts = recent[-1]["ts"] if recent else ""
    rej = {}
    for c in recent:
        if c.get("verdict") == "REJECT":
            key = (c.get("reason", "") or "").split("(")[0].split("=")[0][:40]
            rej[key] = rej.get(key, 0) + 1
    detector = {
        "ts": now.isoformat(),
        "last_scan": last_ts,
        "rows_recent": len(recent),
        "fee100_seen": len(fee100),
        "clean_total": len(clean),
        "top_rejects": dict(sorted(rej.items(), key=lambda x: -x[1])[:5]),
    }
    r.set("canary:detector", json.dumps(detector, ensure_ascii=False))

    # events: push new BUY/SELL rows from live ledger (dedup via seen-set)
    for row in _read_csv(LIVE_LEDGER):
        key = f"{row.get('ts')}|{row.get('action')}|{row.get('pool')}"
        if r.sismember("canary:events:seen", key):
            continue
        r.sadd("canary:events:seen", key)
        r.lpush("canary:events", json.dumps(row, ensure_ascii=False))
        r.ltrim("canary:events", 0, 49)

    r.set("eatlq:overall", json.dumps(OVERALL, ensure_ascii=False))
    print(f"[publish] canary:state trades={state['trades_done']} "
          f"pnl=${state['realized_pnl_usd']} open={state['open_count']} "
          f"fee100_seen={detector['fee100_seen']} clean={detector['clean_total']}", flush=True)


if __name__ == "__main__":
    main()
