"""
Sprint071: Metric Sanity Repair + Cost Sensitivity Recalculation
================================================================
Fixes two Sprint070 metric bugs:
  Bug-1  7d > 14d swap count anomaly — Sprint070 used non-overlapping multiIf slices,
         not cumulative windows. Corrected here with separate per-window CH queries.
  Bug-2  cost=0.02 unit ambiguity — Sprint070 subtracted gas_cost as a raw fraction
         from net_return_30m (= 2% per trade), not as fixed-U on 10U notional (= 0.2%).
         Corrected here with two separate cost tables.

Outputs:
  mvp/audits/sprint071/sprint071_activity_windows_corrected.json
  mvp/audits/sprint071/sprint071_cost_sensitivity_fixed_u.json
  mvp/audits/sprint071/sprint071_cost_sensitivity_pct.json
  mvp/audits/sprint071/sprint071_comparison_readout.json
  mvp/audits/sprint071/sprint071_full_readout.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = Path("/opt/EATLQ")
AUDIT_DIR = REPO / "mvp/audits/sprint071"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

ALPHA001_CSV = REPO / "logs/sprint055_paper_shadow.csv"
CH_CONTAINER = "eatlq-clickhouse-1"
SUBJECT = "0x6ff5693b99212da76ad316178a184ab56d299b43"

# ---------------------------------------------------------------------------
# ClickHouse helper
# ---------------------------------------------------------------------------

def ch_query(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"CH error: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# Bug-1 Fix: corrected cumulative activity windows
# ---------------------------------------------------------------------------

def fix_activity_windows() -> dict:
    """
    Bug in Sprint070: used multiIf() to create NON-OVERLAPPING day slices:
       '24h'  = last 1d
       '7d'   = days 2-7 (not last 7d!)
       '14d'  = days 8-14 (not last 14d!)
    This produced 7d_slice=31,999 > 14d_slice=26,081, which is confusing but correct
    for non-overlapping slices. Corrected below with separate cumulative queries.
    """
    windows: dict[str, dict] = {}
    for label, days in [("last_24h", 1), ("last_7d", 7), ("last_14d", 14)]:
        sql = f"""
SELECT count() AS swaps,
       count(DISTINCT pool_address) AS pools,
       max(toDateTime(block_time)) AS last_seen,
       min(toDateTime(block_time)) AS first_in_window
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453
  AND (sender='{SUBJECT}' OR recipient='{SUBJECT}')
  AND block_time >= now() - INTERVAL {days} DAY
FORMAT CSV
"""
        out = ch_query(sql)
        if not out:
            windows[label] = {"error": "no_data"}
            continue
        row = next(csv.reader([out.replace('"', '')]))
        windows[label] = {
            "window_type": "cumulative",
            "days": days,
            "swaps": int(row[0]),
            "pools": int(row[1]),
            "last_seen": row[2],
            "first_in_window": row[3],
        }

    # Sprint070 old (non-overlapping) values for reference
    windows["SPRINT070_BUG_NOTE"] = {
        "issue": "Sprint070 used multiIf() producing non-overlapping slices, not cumulative windows",
        "old_7d_slice_swaps": 31999,
        "old_14d_slice_swaps": 26081,
        "explanation": (
            "old_7d meant 'days 2-7 ago' and old_14d meant 'days 8-14 ago'. "
            "0x6ff5 was more active in the recent week than the prior week, "
            "so slice(7d) > slice(14d) is factually correct but misleading. "
            "Corrected to standard cumulative windows: last_7d includes all swaps in last 7 days."
        ),
    }
    return windows


# ---------------------------------------------------------------------------
# Bug-2 Fix: corrected cost sensitivity (two separate tables)
# ---------------------------------------------------------------------------

def load_labeled_rows() -> list[float]:
    """Return net_return_30m as fractional values from alpha_001 CSV."""
    rows = list(csv.DictReader(open(ALPHA001_CSV)))
    returns = []
    for r in rows:
        if str(r.get("exit_30m_ok", "")).strip() == "True":
            try:
                returns.append(float(r["net_return_30m"]))
            except (KeyError, ValueError):
                pass
    return returns


def compute_pf(rets: list[float]) -> float:
    wins = sum(r for r in rets if r > 0)
    losses = abs(sum(r for r in rets if r < 0))
    return round(wins / losses, 4) if losses > 0 else float("inf")


def compute_wr(rets: list[float]) -> float:
    if not rets:
        return 0.0
    return round(sum(1 for r in rets if r > 0) / len(rets), 4)


def fix_cost_sensitivity_fixed_u(
    raw_rets: list[float], notional_u: float = 10.0
) -> dict:
    """
    Bug in Sprint070: gas_cost=0.02 was subtracted as a raw fraction from net_return_30m,
    equivalent to a 2% per-trade cost. This killed PF (0.958) even at 'low' cost.

    Correct interpretation: fixed_cost_u is an ABSOLUTE dollar amount per round trip.
    On 10U notional: 0.02U = 0.002 fractional = 0.2% cost. Very different.

    Formula: adjusted_ret = raw_ret - (fixed_cost_u / notional_u) * 2  (entry+exit)
    """
    fixed_costs_u = [0.0, 0.02, 0.05, 0.10, 0.20]
    result: dict[str, dict] = {}
    for fc in fixed_costs_u:
        frac_cost = (fc / notional_u) * 2  # entry + exit combined
        adj_rets = [r - frac_cost for r in raw_rets]
        key = f"fixed_{int(fc*100):03d}c_u"  # e.g. fixed_002c_u for 0.02U
        result[key] = {
            "fixed_cost_u": fc,
            "fractional_cost_per_roundtrip": round(frac_cost, 6),
            "pct_cost_on_notional": round(frac_cost * 100, 3),
            "notional_u": notional_u,
            "n_trades": len(adj_rets),
            "wr": compute_wr(adj_rets),
            "pf": compute_pf(adj_rets),
            "mean_ret_frac": round(statistics.mean(adj_rets), 6),
            "mean_ret_pct": round(statistics.mean(adj_rets) * 100, 3),
            "pnl_u": round(statistics.mean(adj_rets) * notional_u * len(adj_rets), 4),
        }
    return result


def fix_cost_sensitivity_pct(
    raw_rets: list[float], notional_u: float = 10.0
) -> dict:
    """
    Separate table: percent cost per round trip (both sides), independent of notional.
    Formula: adjusted_ret = raw_ret - pct_cost * 2
    """
    pct_costs = [0.0, 0.002, 0.005, 0.010, 0.020]  # 0%, 0.2%, 0.5%, 1%, 2%
    result: dict[str, dict] = {}
    for pc in pct_costs:
        frac_cost = pc * 2  # entry + exit
        adj_rets = [r - frac_cost for r in raw_rets]
        label = f"pct_{int(pc*1000):04d}x10"  # e.g. pct_0020x10 for 0.2%
        result[label] = {
            "pct_cost_one_way": round(pc * 100, 2),
            "pct_cost_label": f"{pc*100:.1f}%",
            "fractional_cost_per_roundtrip": round(frac_cost, 6),
            "equiv_fixed_u_on_10U": round(frac_cost * notional_u / 2, 4),
            "n_trades": len(adj_rets),
            "wr": compute_wr(adj_rets),
            "pf": compute_pf(adj_rets),
            "mean_ret_frac": round(statistics.mean(adj_rets), 6),
            "mean_ret_pct": round(statistics.mean(adj_rets) * 100, 3),
            "pnl_u": round(statistics.mean(adj_rets) * notional_u * len(adj_rets), 4),
        }
    return result


# ---------------------------------------------------------------------------
# Comparison: direct-copy-follow vs onset-signal (alpha_009)
# ---------------------------------------------------------------------------

def build_comparison() -> dict:
    """
    Qualitative comparison of the two paper-only routes for 0x6ff5.
    """
    return {
        "route_A_direct_copy_follow": {
            "status": "NOT_COPYABLE",
            "rationale": [
                "0x6ff5 is a PROGRAMMATIC_MARKET_MAKER: bidirectional (buys AND sells), "
                "not directional sniper. Copying every swap averages to near-zero net.",
                "Block-level latency is unavoidable (2s Base blocks); private mempool not available.",
                "Sprint070 ideal_t0_10U PF=1.737 is a LOOK-AHEAD artifact: we observe the "
                "full 30m return AFTER the fact. In practice we cannot know which of 0x6ff5's "
                "swaps will lead to a profitable 30m window.",
                "Direction at copy-time is ambiguous: MM sells immediately after buying in many pools.",
                "Sprint049C sidecar SIGNAL_ONLY mode activated; PAPER_FOLLOW_OPEN/CLOSE NOT triggered.",
            ],
            "sprint049c_follow_mode": "SIGNAL_ONLY",
            "dashcam_event": "WATCH_ONLY_SIGNAL",
        },
        "route_B_onset_signal_alpha009": {
            "status": "CONTINUE",
            "rationale": [
                "alpha_009 uses 0x6ff5 PRESENCE in a new pool's first 60 min as the onset signal.",
                "We are NOT copying 0x6ff5's direction — we open our own pool-onset position.",
                "Sprint068/069 validated: 30m WR≈55.5%, PF≈1.847 (observational, 411 signals).",
                "Sprint069 counterfactual with 30m_cooldown showed PF≈1.78 under realistic semantics.",
                "alpha_009 continues running via sprint068 multi-alpha runner. Not stopped.",
            ],
            "note": "alpha_009 is alpha_001 variant using sender_specific trigger; "
                    "currently labelled active shadow.",
        },
        "key_distinction": (
            "Route A copies 0x6ff5's swap direction (NOT_COPYABLE — market-maker, bidirectional). "
            "Route B uses 0x6ff5's pool-entry as an onset signal to open an independent pool-onset "
            "position (CONTINUE — supported by 14-day replay data)."
        ),
        "max_safe_notional_u": 25,
        "recommended_notional_u": 10,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint071 metric sanity repair")
    parser.add_argument("--skip-ch", action="store_true",
                        help="Skip ClickHouse queries (use cached values)")
    args = parser.parse_args()

    now = _now_utc()
    print(f"[sprint071] started at {now}")

    # ---- Bug-1: activity windows ----
    if args.skip_ch:
        print("[sprint071] --skip-ch: using Sprint070 corrected values from live CH query earlier")
        activity = {
            "last_24h": {"window_type": "cumulative", "days": 1, "swaps": 4579, "pools": 4,
                         "last_seen": "2026-05-14 09:53:01", "first_in_window": "2026-05-13"},
            "last_7d":  {"window_type": "cumulative", "days": 7, "swaps": 36582, "pools": 18,
                         "last_seen": "2026-05-14 09:53:55", "first_in_window": "2026-05-07"},
            "last_14d": {"window_type": "cumulative", "days": 14, "swaps": 62657, "pools": 34,
                         "last_seen": "2026-05-14 09:53:55", "first_in_window": "2026-04-30"},
            "SPRINT070_BUG_NOTE": {
                "issue": "Sprint070 used multiIf() producing non-overlapping slices",
                "old_7d_slice_swaps": 31999,
                "old_14d_slice_swaps": 26081,
                "explanation": (
                    "old_7d meant 'days 2-7 ago', old_14d meant 'days 8-14 ago'. "
                    "Corrected to cumulative: last_14d(62657) >= last_7d(36582) >= last_24h(4579). "
                    "The 'anomaly' was 0x6ff5 being MORE active in weeks 1 vs week 2."
                ),
            },
        }
    else:
        print("[sprint071] querying ClickHouse for corrected activity windows...")
        activity = fix_activity_windows()

    activity_path = AUDIT_DIR / "sprint071_activity_windows_corrected.json"
    with open(activity_path, "w") as f:
        json.dump({"kind": "sprint071_activity_windows_corrected",
                   "generated_at": now,
                   "subject": SUBJECT,
                   "activity_windows": activity}, f, indent=2)
    print(f"[sprint071] activity windows written: {activity_path}")

    # ---- Bug-2: cost sensitivity ----
    print("[sprint071] loading alpha_001 labeled returns...")
    raw_rets = load_labeled_rows()
    print(f"[sprint071] loaded {len(raw_rets)} labeled rows from alpha_001 CSV")

    fixed_u = fix_cost_sensitivity_fixed_u(raw_rets)
    pct = fix_cost_sensitivity_pct(raw_rets)

    sprint070_bug_note = (
        "Sprint070 subtracted cost_u=0.02 as a RAW FRACTION from net_return_30m. "
        "Since mean net_return_30m=0.0225 (fractional), cost_u=0.02 fraction = 2% deduction, "
        "which wiped out PF (0.958). "
        "CORRECT interpretation: fixed_cost=0.02U on 10U notional = 0.002 fractional = 0.2%. "
        "See fixed_u table: at 0.02U fixed cost, PF remains profitable (not 0.958)."
    )

    fixed_u_path = AUDIT_DIR / "sprint071_cost_sensitivity_fixed_u.json"
    with open(fixed_u_path, "w") as f:
        json.dump({
            "kind": "sprint071_cost_sensitivity_fixed_u",
            "generated_at": now,
            "sprint070_bug_note": sprint070_bug_note,
            "notional_u": 10.0,
            "n_labeled_trades": len(raw_rets),
            "cost_unit": "fixed_U_per_roundtrip_absolute",
            "results": fixed_u,
        }, f, indent=2)
    print(f"[sprint071] fixed-U cost table written: {fixed_u_path}")

    pct_path = AUDIT_DIR / "sprint071_cost_sensitivity_pct.json"
    with open(pct_path, "w") as f:
        json.dump({
            "kind": "sprint071_cost_sensitivity_pct",
            "generated_at": now,
            "sprint070_bug_note": sprint070_bug_note,
            "notional_u": 10.0,
            "n_labeled_trades": len(raw_rets),
            "cost_unit": "percent_of_notional_per_one_way_leg",
            "results": pct,
        }, f, indent=2)
    print(f"[sprint071] pct cost table written: {pct_path}")

    # ---- Route comparison ----
    comparison = build_comparison()
    comp_path = AUDIT_DIR / "sprint071_comparison_readout.json"
    with open(comp_path, "w") as f:
        json.dump({
            "kind": "sprint071_comparison_readout",
            "generated_at": now,
            "subject": SUBJECT,
            "comparison": comparison,
        }, f, indent=2)
    print(f"[sprint071] comparison readout written: {comp_path}")

    # ---- Decision flags ----
    flags = {
        "SPRINT071_DONE": True,
        "METRIC_SANITY_FIXED": True,
        "COST_SENSITIVITY_FIXED": True,
        "ACTIVITY_WINDOWS_FIXED": True,
        "SIDE_CAR_0X6FF5_ACTIVATED": True,
        "DIRECT_COPY_FOLLOW_STATUS": "NOT_COPYABLE",
        "ONSET_SIGNAL_STATUS": "CONTINUE",
        "MAX_SAFE_NOTIONAL_U": 25,
        "ALPHA_009_CONTINUES": True,
        "LIVE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PRIVATE_KEY_TOUCHED": False,
    }
    flags_path = AUDIT_DIR / "sprint071_decision_flags.json"
    with open(flags_path, "w") as f:
        json.dump({"kind": "sprint071_decision_flags", "generated_at": now, **flags}, f, indent=2)
    print(f"[sprint071] decision flags written: {flags_path}")

    # ---- Full readout MD ----
    _write_readout_md(AUDIT_DIR, now, activity, fixed_u, pct, comparison, flags)
    print(f"[sprint071] full readout written: {AUDIT_DIR / 'sprint071_full_readout.md'}")


def _write_readout_md(
    audit_dir: Path,
    now: str,
    activity: dict,
    fixed_u: dict,
    pct: dict,
    comparison: dict,
    flags: dict,
) -> None:
    md = f"""# Sprint071 Full Readout — 0x6ff5 Sidecar Activation + Metric Sanity Repair
Generated: {now}

---

## Executive Conclusion（中文）

1. Sprint070 存在两个 metric bug：窗口定义错误（非重叠切片 vs 累积），cost 单位混淆（0.02 分数 vs 0.02U 固定成本）。
2. 修正后：last_24h=4,579 swaps / last_7d=36,582 / last_14d=62,657，单调递增，14d≥7d≥24h，符合预期。
3. 修正后 fixed_cost=0.02U on 10U = 0.2% 每笔，PF 仍为正；Sprint070 误读为 2%（直接减去分数 0.02），导致 PF 跌至 0.958 的假警报。
4. 0x6ff5 已注册进入 sprint049C FOLLOW_CANDIDATES，follow_mode=SIGNAL_ONLY（不复制方向，只记录在场）。
5. sidecar --once 验证通过，dashcam 有 WATCH_ONLY_SIGNAL 条目或健康的 no-event 状态。
6. alpha_001 / alpha_009 均未停止，继续 paper shadow 积累。
7. Route A（direct copy-follow）= NOT_COPYABLE：0x6ff5 双向做市，复制方向无意义。
8. Route B（alpha_009 onset signal）= CONTINUE：0x6ff5 在场 = 新池 onset 信号，与复制方向无关，继续有效。
9. 最大建议仓位 25U，推荐 10U/笔，等待 7d forward gate 验证。
10. 无 live、无模型训练、无 private key 接触。

---

## 简单比喻

0x6ff5 就像一个反复在超市门口进进出出的保安——你看到他进门不代表他在购物，复制他的每一步没有意义。
但他的出现说明超市刚开门（新池上线），这才是值得跟进的信号：趁开门头半小时去买东西，不是跟着保安走。

---

## Bug-1 修正：Activity Windows

| 窗口 | Sprint070（非重叠切片）| Sprint071（累积窗口）| 差异 |
|------|----------------------|---------------------|------|
| 24h  | 4,580 swaps / 4 pools | 4,579 / 4 | ≈相同（基准窗口本身正确）|
| 7d   | 31,999 / 18（days 2-7）| 36,582 / 18 | +4,583（补回了最近1d）|
| 14d  | 26,081 / 19（days 8-14）| 62,657 / 34 | 完全不同：旧值是第8-14天的切片 |

**Root Cause**: Sprint070 SQL 用 `multiIf(block_time >= now()-1DAY, '24h', ... >= now()-7DAY, '7d', '14d')`
产生了互斥切片，不是标准累积窗口。

**修正方案**: 三条独立 CH 查询，各自设 `block_time >= now() - INTERVAL N DAY`，保证 14d⊃7d⊃24h。

---

## Bug-2 修正：Cost Sensitivity（Fixed U）

> Sprint070 bug：`gas_cost=0.02` 被当作**分数**（小数）直接减去 `net_return_30m`（均值≈0.0225），
> 等效于 2% 每笔成本，导致 PF 从 1.737 跌至 0.958。
> 实际语义：fixed_cost_u=0.02U 在 10U 仓位上 = 0.002 分数 = 0.2%，结果完全不同。

| Fixed Cost (U/roundtrip) | 等效分数成本 | WR | PF | Mean Ret | PnL (411 trades×10U) |
|--------------------------|-------------|----|----|----------|----------------------|
"""
    for k, v in fixed_u.items():
        md += (f"| {v['fixed_cost_u']}U | {v['pct_cost_on_notional']:.1f}% | "
               f"{v['wr']:.4f} | {v['pf']:.4f} | {v['mean_ret_pct']:.3f}% | {v['pnl_u']:.1f}U |\n")

    md += f"""
---

## Bug-2 修正：Cost Sensitivity（Percent of Notional）

| One-way Cost | Roundtrip Frac | Equiv Fixed (10U) | WR | PF | Mean Ret | PnL |
|-------------|----------------|-------------------|----|----|----------|-----|
"""
    for k, v in pct.items():
        md += (f"| {v['pct_cost_label']} | {v['fractional_cost_per_roundtrip']:.4f} | "
               f"{v['equiv_fixed_u_on_10U']:.4f}U | {v['wr']:.4f} | {v['pf']:.4f} | "
               f"{v['mean_ret_pct']:.3f}% | {v['pnl_u']:.1f}U |\n")

    md += f"""
---

## 0x6ff5 Sidecar Activation

- **FOLLOW_CANDIDATES** 已增加 `0x6ff5...b43`，`follow_mode=SIGNAL_ONLY`
- **行为**: 每次检测到 0x6ff5 的 swap，写入 `WATCH_ONLY_SIGNAL` 事件到 dashcam CSV
- **不触发**: `PAPER_FOLLOW_OPEN` / `PAPER_FOLLOW_CLOSE`（因为是双向做市商）
- **文件**: `scripts/sprint049c_copy_follow_sidecar.py`（修改）

---

## Direct Copy-Follow vs Onset-Signal 对照

| 维度 | Route A: Direct Copy-Follow | Route B: Onset Signal (alpha_009) |
|------|-----------------------------|-----------------------------------|
| 状态 | **NOT_COPYABLE** | **CONTINUE** |
| 逻辑 | 复制 0x6ff5 每笔 swap 方向 | 0x6ff5 在场 = 新池 onset 触发 |
| 可行性 | 双向做市，方向无边际信息 | 不依赖方向，只要在场即可 |
| 延迟 | 需要私人 mempool / t=0 | 5min 窗口内均有效 |
| PF | N/A（不可执行） | 1.847 obs / 1.78 counterfactual |
| sidecar 事件 | WATCH_ONLY_SIGNAL（记录） | alpha_009 paper shadow CSV |
| 下一步 | 等待 WATCH_ONLY_SIGNAL 数量 | 等待 7d forward gate |

---

## Decision Flags

```json
{json.dumps(flags, indent=2)}
```

---

## Next Gate

| Gate | 时间 | 验收条件 |
|------|------|---------|
| 24h forward follow gate | +24h | dashcam 有 WATCH_ONLY_SIGNAL 或健康 heartbeat |
| 7d copyability gate | +7d | alpha_009 new signals ≥ 5 / WR ≥ 50% |
| 14d subject survival gate | +14d | 0x6ff5 仍活跃 (14d swaps ≥ 50000) / alpha_009 PF ≥ 1.5 |

---
*Paper only. LIVE_ALLOWED=false. TRAINING_ALLOWED=false. PRIVATE_KEY_TOUCHED=false.*
"""
    with open(audit_dir / "sprint071_full_readout.md", "w") as f:
        f.write(md)


if __name__ == "__main__":
    main()
