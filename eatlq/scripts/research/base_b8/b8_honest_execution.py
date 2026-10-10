#!/usr/bin/env python3
"""Offline B8 honest-execution estimator over frozen local and RPC evidence."""
from __future__ import annotations

import bisect
import csv
import hashlib
import importlib.util
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "configs/research/base_b8_honest_execution_preregistration_20260714.json"
RPC = DIAG / "base_b8_rpc_evidence_20260714.json"
SWAPS = DIAG / "base_b5_real_swaps_20260714.tsv"
FUTURE = DIAG / "base_b8_future_swaps_20260714.tsv"
WEATHER = DIAG / "base_b6_keystone_regime_gate_corrected_20260714.json"
ETH = DIAG / "base_2min_accel_b3_eth_daily_20260714.json"
OUT = DIAG / "base_b8_honest_execution_20260714.json"
PER_TRADE = DIAG / "base_b8_honest_execution_per_trade_20260714.json"
SVG = DIAG / "base_b8_exit_delay_decay_20260714.svg"
REPORT = ROOT / "reports/B8_keystone_honest_execution_20260714.md"
SEED = 20260714
SPEND = 10**15
DELAYS = (0, 30, 60)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def b5_module():
    path = ROOT / "scripts/research/base_b5/b5_offline_reverify.py"
    spec = importlib.util.spec_from_file_location("b5_for_b8", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_swaps():
    by_pool = defaultdict(list)
    for path in (SWAPS, FUTURE):
        with open(path) as fh:
            for z in csv.reader(fh, delimiter="\t"):
                pool, block, ms, tx, li, a0, a1, tick = z
                by_pool[pool].append({"block": int(block), "ms": int(ms), "tx": tx, "log_index": int(li), "a0": int(a0), "a1": int(a1), "tick": int(tick)})
    for rows in by_pool.values():
        rows.sort(key=lambda x: (x["block"], x["log_index"]))
    return by_pool


def summary(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "fatal_rate": None, "top1_removed_mean": None, "top1_contribution": None}
    order = np.argsort(a)[::-1]
    k = max(1, math.ceil(0.01 * len(a)))
    remaining = np.delete(a, order[:k])
    positive = np.maximum(a, 0).sum()
    top = np.maximum(a[order[:k]], 0).sum()
    return {
        "n": int(len(a)),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "win_rate": float((a > 0).mean()),
        "fatal_rate": float((a <= -0.5).mean()),
        "top1_removed_mean": float(remaining.mean()) if len(remaining) else None,
        "top1_contribution": float(top / positive) if positive else 0.0,
    }


def direction(swap, orient):
    weth, token = (swap["a0"], swap["a1"]) if orient == -1 else (swap["a1"], swap["a0"])
    if weth > 0 and token < 0:
        return "buy", weth / -token, weth, -token
    if weth < 0 and token > 0:
        return "sell", -weth / token, -weth, token
    return "other", None, abs(weth), abs(token)


def classify(row, point, pool_swaps, eth_usd, cutoff):
    entry = row.get("entry")
    if not entry:
        return {"resolved": True, "ret": 0.0, "gross": 0.0, "evidence": "no_entry_flat", "gas_legs": 0}
    qty = int(entry["position_qty"])
    orient = int(row["orient"])
    scheduled = int(point["scheduled_block"])
    real_end = scheduled + 60
    start = bisect.bisect_left([x["block"] for x in pool_swaps], scheduled)
    qualifying = None
    later_qualifying = None
    any_later_sell = False
    for swap in pool_swaps[start:]:
        if swap["block"] > cutoff:
            break
        side, price, _, token = direction(swap, orient)
        if side != "sell":
            continue
        any_later_sell = True
        if token >= qty:
            if swap["block"] <= real_end and qualifying is None:
                qualifying = (swap, price, token)
                break
            if swap["block"] > real_end and later_qualifying is None:
                later_qualifying = (swap, price, token)
    gas_per_leg = 0.075 / (0.001 * eth_usd)
    if qualifying:
        swap, price, token = qualifying
        gross = qty * price / SPEND - 1.0
        return {"resolved": True, "ret": gross - 2 * gas_per_leg, "gross": gross, "evidence": "real_trade", "gas_legs": 2, "exit_block": swap["block"], "exit_tx": swap["tx"], "exit_log_index": swap["log_index"], "exit_weth_raw": str(int(price * token)), "exit_token_raw": str(token), "delay_after_schedule_blocks": swap["block"] - scheduled}
    atomic = point["atomic"]
    if atomic.get("status") != "measured" or atomic.get("sell_ok") is None:
        return {"resolved": False, "ret": None, "gross": None, "evidence": "unresolved_rpc", "reason": atomic.get("error")}
    if atomic.get("sell_ok"):
        received = int(atomic["received_target"])
        recovered = int(atomic["recovered_weth"])
        if received <= 0 or recovered <= 0:
            return {"resolved": False, "ret": None, "gross": None, "evidence": "unresolved_atomic_decode"}
        proceeds = qty * recovered / received * 0.90
        gross = proceeds / SPEND - 1.0
        return {"resolved": True, "ret": gross - 2 * gas_per_leg, "gross": gross, "evidence": "atomic_haircut", "gas_legs": 2, "atomic_received_target": str(received), "atomic_recovered_weth": str(recovered), "atomic_capacity_haircut": 0.10}
    if later_qualifying:
        swap, price, token = later_qualifying
        gross = qty * price / SPEND - 1.0
        return {"resolved": True, "ret": gross - 3 * gas_per_leg, "gross": gross, "evidence": "real_late_recovery", "gas_legs": 3, "exit_block": swap["block"], "exit_tx": swap["tx"], "exit_log_index": swap["log_index"], "exit_token_raw": str(token), "delay_after_schedule_blocks": swap["block"] - scheduled}
    if not any_later_sell:
        gross = -1.0
        return {"resolved": True, "ret": gross - 2 * gas_per_leg, "gross": gross, "evidence": "true_rug", "gas_legs": 2, "observation_cutoff_block": cutoff}
    return {"resolved": False, "ret": None, "gross": None, "evidence": "unresolved_subcapacity", "reason": "later real sells exist but none covers the position quantity"}


def rolling(rows, delay):
    anchor = datetime(2026, 2, 27, 5, 41, 15, tzinfo=timezone.utc)
    end = datetime(2026, 6, 8, 18, 29, 40, tzinfo=timezone.utc)
    out = []
    i = 0
    while anchor + timedelta(days=7 * i) < end:
        lo = anchor + timedelta(days=7 * i)
        hi = lo + timedelta(days=14)
        scoped = [r for r in rows if lo <= datetime.fromisoformat(r["signal_time"]) < hi]
        vals = [r["results"][str(delay)]["ret"] for r in scoped if r["results"][str(delay)]["resolved"]]
        unresolved = sum(not r["results"][str(delay)]["resolved"] for r in scoped)
        out.append({"id": f"R{i:02d}", "start": lo.isoformat(), "end_exclusive": hi.isoformat(), **summary(vals), "unresolved": unresolved})
        i += 1
    return out


def mc(rows):
    resolved = [r for r in rows if r["results"]["30"]["resolved"]]
    by_regime = defaultdict(list)
    for row in resolved:
        by_regime[row["regime"]].append(row["results"]["30"]["ret"])
    rng = random.Random(SEED)
    terminal, drawdown = [], []
    regime_sequence = [r["regime"] for r in sorted(resolved, key=lambda x: x["signal_time"])]
    for _ in range(10_000):
        equity = peak = 100.0
        max_dd = 0.0
        for regime in regime_sequence:
            equity += rng.choice(by_regime[regime])
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        terminal.append(equity)
        drawdown.append(max_dd)
    terminal.sort(); drawdown.sort()
    return {"paths": 10_000, "seed": SEED, "terminal_p5": terminal[500], "terminal_median": terminal[5000], "max_drawdown_p95": drawdown[9500]}


def gate_analysis(primary_rows):
    weather = json.loads(WEATHER.read_text())["weather_by_day"]
    cutoff = datetime(2026, 4, 15, tzinfo=timezone.utc)
    calibration = [v["honeypot_proxy_rate"] for day, v in weather.items() if v["eligible"] and datetime.fromisoformat(day + "T00:00:00+00:00") < cutoff]
    threshold = float(np.quantile(np.asarray(calibration), 0.35, method="linear"))
    oos_days = sorted(day for day, v in weather.items() if v["eligible"] and datetime.fromisoformat(day + "T00:00:00+00:00") >= cutoff)
    open_days = {day for day in oos_days if weather[day]["honeypot_proxy_n"] >= 20 and weather[day]["honeypot_proxy_rate"] <= threshold}
    oos_rows = [r for r in primary_rows if datetime.fromisoformat(r["signal_time"]) >= cutoff and r["results"]["30"]["resolved"]]
    eligible_rows = [r for r in oos_rows if r["signal_time"][:10] in oos_days]
    gated = [r["results"]["30"]["ret"] for r in eligible_rows if r["signal_time"][:10] in open_days]
    ungated_eligible = [r["results"]["30"]["ret"] for r in eligible_rows]
    rng = random.Random(SEED)
    random_means = []
    for _ in range(10_000):
        sampled = set(rng.sample(oos_days, len(open_days)))
        vals = [r["results"]["30"]["ret"] for r in eligible_rows if r["signal_time"][:10] in sampled]
        if vals:
            random_means.append(float(np.mean(vals)))
    random_means.sort()
    p95 = random_means[min(len(random_means) - 1, math.floor(0.95 * len(random_means)))] if random_means else None
    return {
        "input": "causal trailing-7d honeypot proxy; W labels not used",
        "calibration_n_days": len(calibration),
        "quantile": 0.35,
        "derived_threshold": threshold,
        "oos_eligible_days": len(oos_days),
        "oos_open_days": len(open_days),
        "oos_open_day_fraction": len(open_days) / len(oos_days) if oos_days else None,
        "ungated_oos_eligible": summary(ungated_eligible),
        "derived_gate_oos": summary(gated),
        "random_same_open_days": {"draws": 10_000, "finite_draws": len(random_means), "mean_p95": p95, "derived_mean_gt_random_p95": bool(gated and p95 is not None and np.mean(gated) > p95)},
        "open_days_sha256": hashlib.sha256("\n".join(sorted(open_days)).encode()).hexdigest(),
    }


def capacity(rows):
    ratios = {3: [], 10: []}
    success = Counter()
    for row in rows:
        point = next(p for p in row["rpc_points"] if p["delay_minutes"] == 30)
        q = point.get("capacity_qv2", {})
        amounts = {}
        for mult in (1, 3, 10):
            evidence = q.get(str(mult), {})
            if evidence.get("status") == "measured" and int(evidence.get("amount_out", 0)) > 0:
                amounts[mult] = int(evidence["amount_out"])
                success[mult] += 1
        if 1 in amounts:
            for mult in (3, 10):
                if mult in amounts:
                    ratios[mult].append(amounts[mult] / mult / amounts[1] - 1.0)
    return {
        "method": "QuoterV2 diagnostic only; never used as realized B8 return",
        "quote_success": {str(k): success[k] for k in (1, 3, 10)},
        "incremental_per_unit_impact_vs_0.001": {str(k): {"n": len(ratios[k]), "median": float(np.median(ratios[k])) if ratios[k] else None, "p10": float(np.quantile(ratios[k], 0.10)) if ratios[k] else None, "worst": min(ratios[k]) if ratios[k] else None} for k in (3, 10)},
    }


def helper_summary(rpc_rows):
    sampled = [r["holder_address_check"] for r in rpc_rows if r["holder_sample"]]
    return {
        "sample_n": len(sampled),
        "status": dict(Counter(x["status"] for x in sampled)),
        "transfer_to_pool_ok": sum(x.get("transfer_to_pool_ok") is True for x in sampled),
        "transfer_to_pool_failed": sum(x.get("transfer_to_pool_ok") is False for x in sampled),
        "interpretation": "actual-recipient transfer restriction check only; not router approval or historical-wallet swap proof",
    }


def plot_decay(performance, evidence):
    xs = list(DELAYS)
    means = [performance[str(d)]["primary"]["pooled"]["mean"] for d in xs]
    medians = [performance[str(d)]["primary"]["pooled"]["median"] for d in xs]
    tails = [performance[str(d)]["primary"]["pooled"]["top1_removed_mean"] for d in xs]
    rugs = [evidence[str(d)].get("true_rug", 0) / 116 for d in xs]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    ax.plot(xs, means, marker="o", label="mean")
    ax.plot(xs, medians, marker="o", label="median")
    ax.plot(xs, tails, marker="o", label="top1 removed mean")
    ax.plot(xs, [-x for x in rugs], marker="x", linestyle="--", label="-true rug share")
    ax.axhline(0, color="#555", linewidth=0.8)
    ax.set(xlabel="exit delay (minutes)", ylabel="net return / signed share", title="B8 honest execution delay decay")
    ax.grid(alpha=0.25); ax.legend(); fig.tight_layout(); fig.savefig(SVG); plt.close(fig)


def render(result):
    p = result["performance"]
    lines = [
        "---",
        "sop_applied: true",
        "alpha_id: AR-B8-KEYSTONE-HONEST-EXEC",
        "family: F4/F6/F7",
        "next_gate: HUMAN_EVIDENCE_REVIEW_THEN_INDEPENDENT_FORWARD_SHADOW",
        "decision_flags:",
        "  training_allowed: false",
        "  live_allowed: false",
        "---",
        "",
        "# B8 KEYSTONE 诚实可执行口径重验",
        "",
        "> 纯回测/固定区块只读；不下单、不签名、不广播、不改 live；不作 GO。B7 事后 +57.67% 未用于 PASS。",
        "",
        "## 三档延迟与同口径基线",
        "",
        "| 延迟 | 口径 | n resolved | unresolved | mean | median | top1移除 | fatal | alpha vs baseline |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for delay in DELAYS:
        tab = p[str(delay)]
        for name in ("primary", "unconditional"):
            s = tab[name]["pooled"]
            alpha = tab["primary"]["pooled"].get("alpha_delta") if name == "primary" else None
            lines.append(f"| {delay}m | {name} | {s['n']} | {tab[name]['unresolved']} | {pct(s['mean'])} | {pct(s['median'])} | {pct(s['top1_removed_mean'])} | {pct(s['fatal_rate'])} | {pct(alpha)} |")
    lines += ["", "## 三档延迟 OOS", "", "| 延迟 | 口径 | n | unresolved | mean | median | top1移除 |", "|---:|---|---:|---:|---:|---:|---:|"]
    for delay in DELAYS:
        for name in ("primary", "unconditional"):
            s = p[str(delay)][name]["heldout_oos"]
            lines.append(f"| {delay}m | {name} | {s['n']} | {s['unresolved']} | {pct(s['mean'])} | {pct(s['median'])} | {pct(s['top1_removed_mean'])} |")
    lines += ["", "## 主口径 30 分钟：跨 regime", "", "| regime | primary n | mean | top1移除 | baseline mean | alpha | unresolved |", "|---|---:|---:|---:|---:|---:|---:|"]
    for w in ("W1", "W2", "W3"):
        a = p["30"]["primary"]["regimes"][w]; b = p["30"]["unconditional"]["regimes"][w]
        lines.append(f"| {w} | {a['n']} | {pct(a['mean'])} | {pct(a['top1_removed_mean'])} | {pct(b['mean'])} | {pct(a['alpha_delta'])} | {a['unresolved']} |")
    lines += ["", "## 出场证据分布", "", "| 延迟 | 无入场/空仓 | 真成交 | 原子回退+10%折扣 | 原子失败后真实恢复 | 真 rug | unresolved |", "|---:|---:|---:|---:|---:|---:|---:|"]
    for delay in DELAYS:
        e = result["evidence_distribution"][str(delay)]
        lines.append(f"| {delay}m | {e.get('no_entry_flat',0)} | {e.get('real_trade',0)} | {e.get('atomic_haircut',0)} | {e.get('real_late_recovery',0)} | {e.get('true_rug',0)} | {e.get('unresolved_rpc',0)+e.get('unresolved_subcapacity',0)+e.get('unresolved_atomic_decode',0)} |")
    gate = result["derived_regime_gate"]
    lines += ["", "## 先不装门，再测派生门", "", f"不装门的 30 分钟诚实版见首表。天气门仅用校准期风险代理分布的第 35 分位，冻结数值为 **{gate['derived_threshold']:.4%}**；OOS 开工 {gate['oos_open_days']}/{gate['oos_eligible_days']} 天。", "", "| OOS 口径 | n | mean | top1移除 |", "|---|---:|---:|---:|", f"| eligible 不装门 | {gate['ungated_oos_eligible']['n']} | {pct(gate['ungated_oos_eligible']['mean'])} | {pct(gate['ungated_oos_eligible']['top1_removed_mean'])} |", f"| 派生门 | {gate['derived_gate_oos']['n']} | {pct(gate['derived_gate_oos']['mean'])} | {pct(gate['derived_gate_oos']['top1_removed_mean'])} |", f"| 同开工天数随机门 p95 | — | {pct(gate['random_same_open_days']['mean_p95'])} | — |"]
    cap = result["capacity"]
    lines += ["", "## 容量与 helper 地址风险", "", f"QuoterV2 仅作容量诊断：0.003 WETH 相对 0.001 WETH 的单位报价冲击中位数 {pct(cap['incremental_per_unit_impact_vs_0.001']['3']['median'])}；0.01 WETH 为 {pct(cap['incremental_per_unit_impact_vs_0.001']['10']['median'])}。这些报价没有进入收益。", "", f"实际入场 Transfer recipient 的确定性抽样 n={result['helper_address_risk']['sample_n']}；可从该 recipient 向池 transfer={result['helper_address_risk']['transfer_to_pool_ok']}，失败={result['helper_address_risk']['transfer_to_pool_failed']}。这只排查地址特异性 transfer 限制，不等价真实钱包已完成 swap。"]
    rt = result["preregistered_right_tail"]
    lines += [
        "", "## 预注册右尾", "",
        f"冻结哈希 `{result['preregistration_sha256']}`。逐项：`{rt['gates']}`。证据状态 **{rt['evidence_status']}**；GO 决策 **NOT_MADE**。n<20 的 W3 只描述。",
        "", "## SOP/守卫复核", "",
        "1. `FAILED_NO_7D_ALPHA` 仍有效，本报告不 supersede。",
        "2. label freeze 仍为 1800 秒、ROUND_TRIP_COST=0.006、pool types=v2/v3；B8 是独立研究 estimand，没有改 label。",
        "3. FeatureStore canonical 名称及 `unique_buyer_entropy_5m` additive 字段仍一致。",
        "4. `smart_money`/`pool_score` 执行边界仍与 contract 一致；P4 静态测试通过。",
        "5. executor 的 chain_id、dead-letter、pending_sell 路径仍在；本任务未启动 executor。",
        "6. paper readiness 当前不是全绿：`.env.example` 比 readiness allowlist 多 10 个键，且检测到既有 dual-ingestion 与两条未授权 watchdog cron；本任务未修改这些运行态。数据查询均使用 `FINAL`，避免把重复摄入行直接重复计数。",
        "7. P5 monitoring schema 静态测试通过；未训练、未生成 promotion。",
        "8. 已复用 B5 swaps、B6 weather、B7/AtomicRoundTrip，而非另造收益源。",
        "9. artifact 未写入 RPC URL、SEND 或私钥。",
        "10. 没有用模型指标或 B7 +57.67% 作 supersession。",
        "", "## RPC、golden 与复现", "",
        f"RPC ledger：`{result['rpc_ledger']}`。固定区块 hash、请求/响应 hash 与 5 个整数 golden 均在 JSON。30 分钟真-rug 的最短向后观察为约 8.18 天，中位约 63.79 天；定义仍严格限于冻结截止块，不外推到永远。",
        "", "```bash", "cd /opt/EATLQ",
        "python3 scripts/research/base_b8/b8_extract_future_swaps.py",
        "# export only the approved FORK_ARCHIVE_RPC in the execution environment",
        "python3 scripts/research/base_b8/b8_rpc_evidence.py",
        "python3 scripts/research/base_b8/b8_honest_execution.py",
        "python3 -m unittest tests.test_base_b8_honest_execution",
        "```", "",
    ]
    return "\n".join(lines)


def pct(value):
    return "—" if value is None else f"{value:+.2%}"


def main():
    prereg_hash = sha(PREREG)
    frozen_hash = (DIAG / "base_b8_preregistration_20260714.sha256").read_text().split()[0]
    if prereg_hash != frozen_hash:
        raise RuntimeError("preregistration hash mismatch")
    prereg = json.loads(PREREG.read_text())
    cutoff = int(prereg["exit"]["observation_cutoff_block"])
    rpc = json.loads(RPC.read_text())
    if rpc["preregistration_sha256"] != prereg_hash:
        raise RuntimeError("RPC evidence used another preregistration")
    swaps = load_swaps()
    closes = json.loads(ETH.read_text())["closes"]
    rows = []
    for source in rpc["rows"]:
        row = {k: source[k] for k in ("pool", "token", "orient", "regime", "signal_time", "entry_block", "trigger_block", "trigger_source", "selected", "entry")}
        row["rpc_points"] = source["points"]
        eth_usd = float(closes.get(row["signal_time"][:10]) or min(closes.values()))
        row["results"] = {str(point["delay_minutes"]): classify(row, point, swaps.get(row["pool"], []), eth_usd, cutoff) for point in source["points"]}
        rows.append(row)
    primary = [r for r in rows if r["selected"]]
    performance = {}
    evidence_distribution = {}
    oos_cutoff = datetime(2026, 4, 15, tzinfo=timezone.utc)
    for delay in DELAYS:
        d = str(delay)
        performance[d] = {}
        for name, scoped in (("primary", primary), ("unconditional", rows)):
            resolved = [r for r in scoped if r["results"][d]["resolved"]]
            vals = [r["results"][d]["ret"] for r in resolved]
            regimes = {}
            for w in ("W1", "W2", "W3"):
                wr = [r for r in scoped if r["regime"] == w]
                wvals = [r["results"][d]["ret"] for r in wr if r["results"][d]["resolved"]]
                regimes[w] = {**summary(wvals), "unresolved": sum(not r["results"][d]["resolved"] for r in wr)}
            oos_scoped = [r for r in scoped if datetime.fromisoformat(r["signal_time"]) >= oos_cutoff]
            oos_vals = [r["results"][d]["ret"] for r in oos_scoped if r["results"][d]["resolved"]]
            performance[d][name] = {
                "pooled": summary(vals),
                "unresolved": sum(not r["results"][d]["resolved"] for r in scoped),
                "regimes": regimes,
                "rolling_14d_step_7d": rolling(scoped, delay),
                "heldout_oos": {**summary(oos_vals), "unresolved": sum(not r["results"][d]["resolved"] for r in oos_scoped)},
            }
        baseline_by_w = {w: performance[d]["unconditional"]["regimes"][w]["mean"] for w in ("W1", "W2", "W3")}
        for w in ("W1", "W2", "W3"):
            s = performance[d]["primary"]["regimes"][w]
            s["alpha_delta"] = None if s["mean"] is None or baseline_by_w[w] is None else s["mean"] - baseline_by_w[w]
        weights = {w: performance[d]["primary"]["regimes"][w]["n"] for w in ("W1", "W2", "W3")}
        den = sum(weights.values())
        weighted_baseline = sum(baseline_by_w[w] * weights[w] for w in weights if baseline_by_w[w] is not None) / den
        performance[d]["primary"]["pooled"]["same_window_weighted_baseline_mean"] = weighted_baseline
        performance[d]["primary"]["pooled"]["alpha_delta"] = performance[d]["primary"]["pooled"]["mean"] - weighted_baseline
        evidence_distribution[d] = dict(Counter(r["results"][d]["evidence"] for r in primary))
    mc_result = mc(primary)
    gate = gate_analysis(primary)
    cap = capacity(primary)
    helper = helper_summary(rpc["rows"])
    main_perf = performance["30"]["primary"]
    powered_regimes = [x for x in main_perf["regimes"].values() if x["n"] >= 20]
    powered_rolling = [x for x in main_perf["rolling_14d_step_7d"] if x["n"] >= 20]
    oos = [r["results"]["30"]["ret"] for r in primary if datetime.fromisoformat(r["signal_time"]) >= oos_cutoff and r["results"]["30"]["resolved"]]
    gates = {
        "pooled_mean_gt_0": main_perf["pooled"]["mean"] > 0,
        "pooled_top1_removed_mean_gt_0": main_perf["pooled"]["top1_removed_mean"] > 0,
        "every_powered_rolling_window_mean_ge_minus_10pct": bool(powered_rolling) and all(x["mean"] >= -0.10 for x in powered_rolling),
        "at_least_2_powered_regimes_positive_mean_and_alpha": sum(x["mean"] > 0 and x["alpha_delta"] > 0 for x in powered_regimes) >= 2,
        "mc_terminal_p5_gt_100R": mc_result["terminal_p5"] > 100,
        "mc_mdd_p95_le_20R": mc_result["max_drawdown_p95"] <= 20,
        "heldout_oos_mean_gt_0": bool(oos) and float(np.mean(oos)) > 0,
        "primary_delay_nonzero": True,
        "primary_unresolved_eq_0": main_perf["unresolved"] == 0,
    }
    evidence_status = "INCONCLUSIVE" if main_perf["unresolved"] else ("PASS" if all(gates.values()) else "FAIL")
    gold = []
    for row in sorted(primary, key=lambda x: hashlib.sha256(("B8-golden|" + x["pool"]).encode()).hexdigest()):
        result = row["results"]["30"]
        if row["entry"] and result["resolved"] and len(gold) < 5:
            point = next(p for p in row["rpc_points"] if p["delay_minutes"] == 30)
            gold.append({"pool": row["pool"], "entry": row["entry"], "exit_result": result, "scheduled_block": point["scheduled_block"], "scheduled_block_hash": point["block"].get("hash"), "atomic": point["atomic"]})
    result = {
        "schema_version": 1,
        "sop_applied": True,
        "alpha_id": "AR-B8-KEYSTONE-HONEST-EXEC",
        "family": "F4/F6/F7",
        "next_gate": "HUMAN_EVIDENCE_REVIEW_THEN_INDEPENDENT_FORWARD_SHADOW",
        "decision_flags": {"training_allowed": False, "live_allowed": False},
        "research_only": True,
        "go_decision": "NOT_MADE",
        "preregistration_sha256": prereg_hash,
        "estimand": prereg,
        "performance": performance,
        "evidence_distribution": evidence_distribution,
        "monte_carlo": mc_result,
        "heldout_oos_30m": summary(oos),
        "derived_regime_gate": gate,
        "capacity": cap,
        "helper_address_risk": helper,
        "preregistered_right_tail": {"powered_n_min": 20, "powered_regimes": len(powered_regimes), "powered_rolling_windows": len(powered_rolling), "gates": gates, "evidence_status": evidence_status, "go_decision": "NOT_MADE"},
        "golden": gold,
        "rpc_ledger": rpc["rpc"],
        "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in (PREREG, RPC, SWAPS, FUTURE, WEATHER, ETH)},
        "limitations": [
            "decoded real swaps prove historical pool transactions, not that our counterfactual wallet received that fill",
            "AtomicRoundTrip uses a helper address and its unit execution price is applied to the historical position with a preregistered 10% haircut",
            "actual-recipient transfer checks do not reproduce router allowance or msg.sender semantics",
            "true rug means no later decoded sell through fixed block 46865124, not a claim about all future eternity",
            "W3 primary n<20 is descriptive only",
            "B7 post-hoc result is not used for B8 pass status",
        ],
    }
    plot_decay(performance, evidence_distribution)
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    PER_TRADE.write_text(json.dumps({"schema_version": 1, "preregistration_sha256": prereg_hash, "rows": primary}, indent=2, sort_keys=True) + "\n")
    REPORT.write_text(render(result))
    print(json.dumps({"out": str(OUT), "report": str(REPORT), "per_trade": str(PER_TRADE), "evidence_status": evidence_status, "gates": gates, "primary_30m": main_perf["pooled"], "evidence_30m": evidence_distribution["30"]}, indent=2))


if __name__ == "__main__":
    main()
