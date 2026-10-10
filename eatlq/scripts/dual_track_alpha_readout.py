#!/usr/bin/env python3
"""Dual-track alpha readout — fee100 SOP mainline vs engineering shadow validation.

Read-only aggregation. Writes:
  mvp/audits/sprint078_pipeline_batch25/dual_track_readout.md
  mvp/audits/sprint078_pipeline_batch25/dual_track_flags.json

Does NOT promote models, write signals, or touch private keys.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_MD = REPO / "mvp/audits/sprint078_pipeline_batch25/dual_track_readout.md"
OUT_JSON = REPO / "mvp/audits/sprint078_pipeline_batch25/dual_track_flags.json"
REDIS_URL = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
CUTOVER_TS = int(os.environ.get("ACCUMULATION_CUTOVER_TS", "1779005000"))


def jload(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def csv_last_signal(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        rows = list(csv.DictReader(path.open()))
        return max((r.get("signal_time", "") for r in rows), default="")
    except Exception:
        return ""


def fee100_onset_stats() -> dict:
    csv_path = REPO / "logs/sprint055_paper_shadow.csv"
    rows = list(csv.DictReader(csv_path.open())) if csv_path.exists() else []
    fee100 = [r for r in rows if str(r.get("fee_bps")) == "100"]
    eligible = [r for r in fee100 if r.get("entry_quote_ok") == "True" and r.get("is_0x6ff5") == "True"]
    return {
        "csv_rows_total": len(rows),
        "fee100_rows": len(fee100),
        "fee100_eligible_sender_gated": len(eligible),
        "last_signal_time": csv_last_signal(csv_path),
        "eligible_pools": len({r.get("pool") for r in eligible if r.get("pool")}),
    }


def sprint069_stats() -> dict:
    st = jload(REPO / "mvp/audits/sprint069_forward_dryrun/forward_dryrun_state.json")
    if not st:
        return {"status": "NOT_INITIALIZED"}
    inc = st.get("incremental_metrics") or {}
    d = st.get("accounts", {}).get("D_entry_anchor_30m_fixed", {})
    fwd = [t for t in d.get("closed_trades", []) if t.get("data_source") == "forward"]
    wins = [t for t in fwd if t.get("win")]
    losses = [t for t in fwd if not t.get("win")]
    gw = sum(float(t.get("pnl") or 0) for t in wins)
    gl = abs(sum(float(t.get("pnl") or 0) for t in losses))
    return {
        "status": st.get("status"),
        "run_count": st.get("run_count"),
        "last_run": st.get("last_run"),
        "forward_n_total": len(fwd),
        "forward_n_since_repair": inc.get("forward_n_since_repair", "unknown"),
        "run_count_since_repair": inc.get("run_count_since_repair", "unknown"),
        "last_processed": (st.get("last_processed_by_line") or {}).get("alpha_001_baseline"),
        "wr": round(len(wins) / len(fwd), 4) if fwd else None,
        "pf": round(gw / gl, 4) if gl > 0 else None,
        "roi_pct": round(100 * sum(float(t.get("pnl") or 0) for t in fwd) / 100.0, 2) if fwd else 0,
        "days": len({(t.get("entry_time") or "")[:10] for t in fwd}),
        "repair_baseline": st.get("repair_baseline"),
    }


def executor_stats() -> dict:
    out = {
        "track_a_true_live_forward_n": 0,
        "track_b_engineering_executor_n": 0,
        "exec_sells": 0,
        "signal_stale": True,
        "last_trade_time": None,
        "error": None,
    }
    try:
        import redis
        r = redis.from_url(REDIS_URL, decode_responses=True)
        for raw in r.lrange("exec:trades", 0, 999):
            d = json.loads(raw)
            if not d.get("dry_run") or d.get("action") != "buy" or not d.get("success"):
                continue
            ts = int(d.get("ts") or 0)
            src = d.get("signal_source", "")
            if ts >= CUTOVER_TS and "S074" in src:
                if "_B" in src:
                    out["track_b_engineering_executor_n"] += 1
                else:
                    out["track_a_true_live_forward_n"] += 1
                    if ts and (out.get("_last_ts") is None or ts > out["_last_ts"]):
                        out["_last_ts"] = ts
        for raw in r.lrange("exec:trades", 0, 999):
            d = json.loads(raw)
            if d.get("dry_run") and d.get("action") == "sell" and d.get("success"):
                out["exec_sells"] += 1
        if out.get("_last_ts"):
            out["last_trade_time"] = datetime.fromtimestamp(out["_last_ts"], tz=timezone.utc).isoformat()
        out.pop("_last_ts", None)
    except Exception as exc:
        out["error"] = str(exc)
    n_doc = jload(REPO / "mvp/audits/execution_readiness/n_accounting.json")
    if isinstance(n_doc, dict) and not out["track_a_true_live_forward_n"]:
        out["track_a_true_live_forward_n"] = int(n_doc.get("true_live_forward_executor_n") or 0)
    last_sig = csv_last_signal(REPO / "logs/sprint055_paper_shadow.csv")
    if last_sig:
        try:
            dt = datetime.fromisoformat(last_sig.replace("Z", "+00:00"))
            out["signal_stale"] = (datetime.now(tz=timezone.utc) - dt).total_seconds() > 7200
        except Exception:
            pass
    return out


def engineering_dryrun() -> list[dict]:
    sb = jload(REPO / "mvp/audits/sprint086_engineering_shadow/scoreboard.json")
    return sb if isinstance(sb, list) else []


def variant_stats() -> dict:
    sb = jload(REPO / "mvp/audits/sprint087_alpha_variants/scoreboard.json")
    fb = jload(REPO / "mvp/audits/sprint087_alpha_variants/family_best.json")
    if not isinstance(sb, list):
        return {"variants": 0}
    return {
        "variants": len(sb),
        "max_fwd_n": max(int(r.get("fwd_n") or 0) for r in sb),
        "families": len(fb) if isinstance(fb, dict) else 0,
        "family_best": fb if isinstance(fb, dict) else {},
    }


def batch25_stats() -> dict:
    sb = jload(REPO / "mvp/audits/sprint078_pipeline_batch25/scoreboard.json")
    if not isinstance(sb, list) or not sb:
        return {"strategies": 0}
    best = max(sb, key=lambda r: int(r.get("fwd_n") or 0))
    return {
        "strategies": len(sb),
        "max_fwd_n": max(int(r.get("fwd_n") or 0) for r in sb),
        "best_strategy": best.get("strategy_id"),
        "best_fwd_n": best.get("fwd_n"),
        "best_pf": best.get("fwd_pf"),
        "best_wr": best.get("fwd_wr"),
    }


def sop_verdict() -> dict:
    return jload(REPO / "mvp/audits/sop_promotion_inventory/sop_promotion_flags.json") or {}


def fmt(v) -> str:
    if v is None:
        return "—"
    return str(v)


def build_readout() -> tuple[str, dict]:
    now = datetime.now(tz=timezone.utc)
    fee100 = fee100_onset_stats()
    s069 = sprint069_stats()
    exe = executor_stats()
    eng = engineering_dryrun()
    variants = variant_stats()
    b25 = batch25_stats()
    sop = sop_verdict()

    eng_lines = "\n".join(
        f"| {r.get('track_key')} | {r.get('signal_rows_total')} | {r.get('forward_n')} | "
        f"{r.get('forward_n_incremental')} | {fmt(r.get('forward_wr'))} | {fmt(r.get('forward_pf'))} | "
        f"{fmt(r.get('forward_roi_pct'))}% | {fmt(r.get('forward_days'))} | 工程-only |"
        for r in eng
    ) or "| — | — | — | — | — | — | — | — | — |"

    md = f"""# 双轨验证 Readout（Alpha 主线 vs 工程验证线）

生成时间：`{now.isoformat()}`  
安全边界：`LIVE_ALLOWED=false` · 无 private key · 无真实交易 · 无 model promotion

---

## 1. fee100 onset 主线（SOP 晋升口径）

| 指标 | 值 | 证据类型 |
|------|-----|----------|
| Paper CSV 总行 | {fee100['csv_rows_total']} | 信号供给 |
| fee100 行数 | {fee100['fee100_rows']} | Alpha 主线 |
| sender-gated 合格行 | {fee100['fee100_eligible_sender_gated']} | Alpha 主线 |
| 合格池数 | {fee100['eligible_pools']} | Alpha 主线 |
| 最近信号时间 | {fee100['last_signal_time'] or '无'} | 供给状态 |
| Sprint069 forward N（总量） | {s069.get('forward_n_total', '—')} | Dry-run 虚拟账本 |
| **修复后增量 forward N** | **{s069.get('forward_n_since_repair', '—')}** | **增量健康度** |
| 修复后 cron 成功次数 | {s069.get('run_count_since_repair', '—')} | 管道健康 |
| WR / PF / ROI / 天数 | {fmt(s069.get('wr'))} / {fmt(s069.get('pf'))} / {fmt(s069.get('roi_pct'))}% / {fmt(s069.get('days'))} | Alpha 证据（historical+forward 混合） |
| last_processed | {s069.get('last_processed') or '—'} | 增量 cursor |
| SOP 裁决 | {((sop.get('decision') or {}).get('verdict')) or '—'} | 晋升门禁 |

**解读：** fee100 主线自 `{fee100['last_signal_time'] or 'N/A'}` 起无新 onset 信号；069 cron 在修复后 **增量 N={s069.get('forward_n_since_repair', 0)}**（replay 137 笔不计入增量）。SOP 晋升仍只看 fee100 forward-only 路径。

---

## 2. Executor 可执行验证（Track A vs Track B）

| 轨道 | true_live_forward N | 用途 | 是否计入 SOP |
|------|---------------------|------|--------------|
| Track A（fee100 bridge） | {exe['track_a_true_live_forward_n']} | 可执行 / sell-exit / 稳定性 | 是（canary 前置） |
| Track B（fee500 bridge `_B`） | {exe['track_b_engineering_executor_n']} | 工程验证-only | **否** |
| Sell 成功次数 | {exe['exec_sells']} | 卖出环路 | 工程证据 |
| signal_stale | {exe['signal_stale']} | 供给告警 | — |
| 最近 executor 成交 | {exe.get('last_trade_time') or '—'} | — | — |

**N 增长路径：** fee100 paper → sprint082 bridge → Redis signals → executor dry-run（Track A）。fee500 走独立 CSV/state/dedup（Track B `_B`），**不得与 Track A 混算 canary N**。

---

## 3. Dry-run 虚拟账本

### 3a. Sprint069 legacy（fee100 主线，2 策略）
- D_entry_anchor_30m_fixed：forward N={s069.get('forward_n_total')}，修复后增量={s069.get('forward_n_since_repair')}
- 状态：{s069.get('status')}，run_count={s069.get('run_count')}

### 3b. Sprint078 batch25（25 策略，fee100 CSV）
- 策略数：{b25.get('strategies', 0)}，max fwd_n={b25.get('max_fwd_n', '—')}
- 最佳：{b25.get('best_strategy', '—')}（n={b25.get('best_fwd_n')}, PF={b25.get('best_pf')}, WR={b25.get('best_wr')}）

### 3c. Sprint086 工程 shadow（fee500 / fee3000 / old_pool）
| 轨道 | 信号行 | forward N | 增量 N | WR | PF | ROI | 天 | 证据 |
|------|--------|-----------|--------|----|----|-----|-----|------|
{eng_lines}

### 3d. Sprint087 alpha 变体对比（{variants.get('variants', 0)} 策略）
- 家族数：{variants.get('families', 0)}，max fwd_n={variants.get('max_fwd_n', '—')}
- 用途：**dry-run 候选对比**，全部 `DRYRUN_COMPARE_ONLY`，不进 SOP 晋升

---

| 类别 | 内容 | 可用于 SOP 晋升 |
|------|------|-----------------|
| **Alpha 证据** | fee100 onset paper、069/078 forward dry-run（fee100 CSV）、Track A executor N | 是（须 concentration + n 门槛） |
| **工程证据** | Track B executor N、086 非 fee100 dry-run、sprint073 fee-expanded 研究、historical replay | **否** |
| **Replay 膨胀** | 069 修复时 replay 的 137 forward 笔 | **否**（看 incremental 字段） |

---

## 5. 当前动作项

1. 等待 fee100 新池 onset 或放宽 batch25 paper 过滤器以增加 alpha 主线供给。
2. Track B bridge cron 已接入；fee500 热池出现时 executor N_B 应独立增长。
3. 069 已加文件锁；监控 `forward_n_since_repair` 而非总量。
4. SOP 维持 `{((sop.get('decision') or {}).get('verdict')) or 'NO_PROMOTION_NO_CANARY'}`。

---
*Artifact: `{OUT_MD.relative_to(REPO)}`*
"""
    flags = {
        "generated_at_utc": now.isoformat(),
        "live_allowed": False,
        "sop_promotion_eligible_tracks": ["fee100_onset_mainline"],
        "engineering_only_tracks": ["fee500", "fee3000", "old_pool", "track_b_executor"],
        "fee100_onset": fee100,
        "sprint069": s069,
        "executor": exe,
        "engineering_dryrun": eng,
        "alpha_variants": variants,
        "batch25": b25,
        "sop_verdict": (sop.get("decision") or {}).get("verdict"),
    }
    return md, flags


def main() -> None:
    md, flags = build_readout()
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(flags, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": True, "readout": str(OUT_MD), "flags": str(OUT_JSON)}, indent=2))


if __name__ == "__main__":
    main()
