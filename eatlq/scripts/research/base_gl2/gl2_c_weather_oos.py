#!/usr/bin/env python3
"""GL2-C frozen exogenous-weather train/OOS falsification.

This is an offline, read-only research script.  It implements the rules frozen
in reports/GL2_C_prereg_exogenous_weather_20260715.md without RPC, training a
model, writing signals, or touching any executor/live path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL2_C_prereg_exogenous_weather_20260715.md"
PREREG_SHA = "2ee3684425e66a42366b5f291c6fd016304d3c295bf263cdf8daecb1e0f662ea"
KEYSTONE = DIAG / "base_reaudit10_per_trade_20260715.json"
CORE = DIAG / "base_reaudit10_seconds_rpc_20260715.json"
RUG = DIAG / "base_reaudit10_rug_five_evidence_rpc_20260715.json"
B6 = DIAG / "base_b6_weather_repair_20260714.json"
ETH = DIAG / "base_2min_accel_b3_eth_daily_20260714.json"
OUT = DIAG / "base_gl2_c_weather_oos_20260715.json"
REPORT = ROOT / "reports/GL2_C_exogenous_weather_oos_result_20260715.md"

START = date(2026, 2, 27)
END = date(2026, 6, 8)
TRAIN_END = date(2026, 4, 15)
O1 = (date(2026, 4, 15), date(2026, 5, 15))
O2 = (date(2026, 5, 15), date(2026, 6, 9))
EXITS = (1, 5, 15)
QUANTILES = (0.20, 0.35, 0.50, 0.65, 0.80)
SEED = 20260715
DRAWS = 10_000


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def days(lo: date, hi: date):
    d = lo
    while d <= hi:
        yield d
        d += timedelta(days=1)


def finite(x) -> bool:
    return x is not None and math.isfinite(float(x))


def summarize(values, total_rows: int | None = None) -> dict:
    vals = np.asarray([float(x) for x in values if finite(x)], dtype=float)
    unresolved = (total_rows - len(vals)) if total_rows is not None else None
    if not len(vals):
        return {
            "n": 0, "unresolved": unresolved, "mean": None, "median": None,
            "p10": None, "p90": None, "win_rate": None, "fatal_rate": None,
            "top1_removed_mean": None,
        }
    k = max(1, math.ceil(0.01 * len(vals)))
    keep = np.sort(vals)[:-k]
    return {
        "n": int(len(vals)),
        "unresolved": unresolved,
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "p10": float(np.quantile(vals, 0.10, method="linear")),
        "p90": float(np.quantile(vals, 0.90, method="linear")),
        "win_rate": float(np.mean(vals > 0)),
        "fatal_rate": float(np.mean(vals <= -1.0)),
        "top1_removed_mean": float(np.mean(keep)) if len(keep) else None,
    }


def import_reaudit():
    path = ROOT / "scripts/research/base_reaudit10/reaudit10_analyze.py"
    spec = importlib.util.spec_from_file_location("gl2c_reaudit", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def keystone_rows() -> list[dict]:
    rows = []
    for src in load_json(KEYSTONE)["rows"]:
        outcomes = {}
        evidence = {}
        for xo in EXITS:
            cell = src["matrix"][f"1:{xo}"]
            outcomes[str(xo)] = float(cell["ret"]) if cell.get("resolved") else None
            evidence[str(xo)] = cell.get("evidence") or "UNRESOLVED"
        rows.append({
            "pool": src["pool"], "day": src["signal_time"][:10],
            "signal_time": src["signal_time"], "outcomes": outcomes,
            "evidence": evidence,
        })
    assert len(rows) == 116
    return rows


def full_universe_rows() -> list[dict]:
    """Rebuild the 431-row same-age secondary cohort from frozen GL1 inputs."""
    an = import_reaudit()
    core = load_json(CORE)
    rug = load_json(RUG)
    swaps = an.extend_rug_swaps(an.load_swaps(), rug)
    b5 = an.mod(ROOT / "scripts/research/base_b5/b5_offline_reverify.py", "gl2c_b5")
    frame, _, _ = b5.load_base()
    orient = {r.pool: int(r.orient) for r in frame.itertuples()}
    rugs, _ = an.rug_classes(rug, swaps, orient)
    closes = load_json(ETH)["closes"]
    out = []
    for src in core["rows"]:
        eth = float(closes.get(src["signal_time"][:10]) or min(closes.values()))
        outcomes = {}
        evidence = {}
        for xo in EXITS:
            cell = an.classify_cell(src, 1, xo, swaps, rugs, eth)
            outcomes[str(xo)] = float(cell["ret"]) if cell.get("resolved") else None
            evidence[str(xo)] = cell.get("evidence") or "UNRESOLVED"
        out.append({
            "pool": src["pool"], "day": src["signal_time"][:10],
            "signal_time": src["signal_time"], "outcomes": outcomes,
            "evidence": evidence,
        })
    assert len(out) == 431
    return out


def indicator_series(universe: list[dict]) -> tuple[dict, dict]:
    b6 = load_json(B6)
    counts = {k: int(v) for k, v in b6["summary"]["per_day"].items()}
    closes = {k: float(v) for k, v in load_json(ETH)["closes"].items()}
    series = {name: {} for name in (
        "pool_creation_rate_7d", "aggregate_weth_net_inflow_7d",
        "eth_realized_volatility_7d", "base_gas_regime_7d",
        "whole_universe_rug_rate_7d",
    )}
    unavailable = {
        "aggregate_weth_net_inflow_7d": (
            "UNAVAILABLE_NOT_TESTED: no local all-WETH-pool decoded swap source covers "
            "the full frozen calendar with auditable timestamps and WETH orientation"
        ),
        "base_gas_regime_7d": (
            "UNAVAILABLE_NOT_TESTED: no already-hashed full historical Base block-header "
            "gas-fill census; sparse candidate receipts are forbidden"
        ),
        "whole_universe_rug_rate_7d": (
            "UNAVAILABLE_NOT_TESTED: GL1 final five-evidence labels lack historical "
            "settlement timestamps needed to prove each rug was known strictly before D"
        ),
    }
    for d in days(START, END):
        key = str(d)
        lag = [str(d - timedelta(days=i)) for i in range(7, 0, -1)]
        if all(x in counts for x in lag):
            series["pool_creation_rate_7d"][key] = sum(counts[x] for x in lag) / 7.0
        close_days = [str(d - timedelta(days=i)) for i in range(8, 0, -1)]
        if all(x in closes and closes[x] > 0 for x in close_days):
            prices = np.asarray([closes[x] for x in close_days], dtype=float)
            rets = np.diff(np.log(prices))
            series["eth_realized_volatility_7d"][key] = float(np.std(rets, ddof=0))
    coverage = {}
    for name, vals in series.items():
        coverage[name] = {
            "status": "UNAVAILABLE_NOT_TESTED" if name in unavailable else "AVAILABLE",
            "available_days": len(vals),
            "missing_days": (END - START).days + 1 - len(vals),
            "reason": unavailable.get(name),
        }
    return series, coverage


def rows_on(rows: list[dict], open_days: set[str], exit_front: int,
            window: tuple[date, date] | None = None) -> tuple[list, int]:
    chosen = []
    total = 0
    for row in rows:
        d = date.fromisoformat(row["day"])
        if row["day"] not in open_days:
            continue
        if window is not None and not (window[0] <= d < window[1]):
            continue
        total += 1
        val = row["outcomes"][str(exit_front)]
        if finite(val):
            chosen.append(float(val))
    return chosen, total


def derive_threshold(name: str, values: dict, keystone: list[dict]) -> dict:
    train_days = sorted(d for d in values if date.fromisoformat(d) < TRAIN_END)
    if not train_days:
        return {"status": "TRAIN_UNPOWERED", "reason": "zero training eligible days"}
    arr = np.asarray([values[d] for d in train_days], dtype=float)
    candidates = []
    for q in QUANTILES:
        threshold = float(np.quantile(arr, q, method="linear"))
        for direction in ("low", "high"):
            opened = {d for d in train_days if (
                values[d] <= threshold if direction == "low" else values[d] >= threshold
            )}
            share = len(opened) / len(train_days)
            primary, _ = rows_on(keystone, opened, 1)
            means = []
            finite_by_exit = {}
            for xo in EXITS:
                vals, total = rows_on(keystone, opened, xo)
                finite_by_exit[str(xo)] = {"finite_n": len(vals), "total_rows": total}
                means.append(float(np.mean(vals)) if vals else None)
            survives = (
                0.20 <= share <= 0.60 and len(opened) >= 5 and len(primary) >= 10
                and all(x is not None for x in means)
            )
            candidates.append({
                "quantile": q, "threshold": threshold, "direction": direction,
                "open_days": len(opened), "eligible_days": len(train_days),
                "open_share": share, "primary_finite_n": len(primary),
                "finite_by_exit": finite_by_exit,
                "means_by_exit": {str(x): means[i] for i, x in enumerate(EXITS)},
                "score_min_exit_mean": min(means) if survives else None,
                "survives": survives,
            })
    good = [x for x in candidates if x["survives"]]
    if not good:
        return {
            "status": "TRAIN_UNPOWERED", "reason": "no candidate passed frozen power/share rules",
            "training_eligible_days": len(train_days), "candidates": candidates,
        }
    good.sort(key=lambda x: (
        -x["score_min_exit_mean"], -x["primary_finite_n"], x["open_share"],
        x["quantile"], 0 if x["direction"] == "low" else 1,
    ))
    chosen = good[0]
    return {
        "status": "THRESHOLD_FROZEN_FROM_TRAIN", "indicator": name,
        "training_eligible_days": len(train_days), "selected": chosen,
        "candidates": candidates,
    }


def random_distribution(rows: list[dict], eligible: list[str], n_open: int,
                        random_sets: list[set[str]], xo: int,
                        window: tuple[date, date] | None = None) -> dict:
    means = []
    outcome_ns = []
    for opened in random_sets:
        vals, _ = rows_on(rows, opened, xo, window)
        outcome_ns.append(len(vals))
        if vals:
            means.append(float(np.mean(vals)))
    if not means:
        return {
            "draws": DRAWS, "finite_draws": 0, "median": None, "p95": None,
            "finite_outcome_n_min": 0, "finite_outcome_n_max": 0,
        }
    return {
        "draws": DRAWS, "finite_draws": len(means),
        "median": float(np.quantile(means, 0.50, method="linear")),
        "p95": float(np.quantile(means, 0.95, method="higher")),
        "finite_outcome_n_min": min(outcome_ns),
        "finite_outcome_n_max": max(outcome_ns),
    }


def endpoint(rows: list[dict], opened: set[str], eligible: list[str], xo: int,
             random_sets: list[set[str]], window=None) -> dict:
    vals, total = rows_on(rows, opened, xo, window)
    true = summarize(vals, total)
    rnd = random_distribution(rows, eligible, len(opened), random_sets, xo, window)
    return {
        "true_gate": true,
        "random_same_open_share": rnd,
        "true_minus_random_median": (
            true["mean"] - rnd["median"]
            if true["mean"] is not None and rnd["median"] is not None else None
        ),
        "true_strictly_gt_random_p95": bool(
            true["mean"] is not None and rnd["p95"] is not None and true["mean"] > rnd["p95"]
        ),
    }


def evaluate_indicator(name: str, values: dict, coverage: dict,
                       keystone: list[dict], universe: list[dict]) -> dict:
    if coverage["status"] != "AVAILABLE":
        return {"status": "UNAVAILABLE_NOT_TESTED", "coverage": coverage}
    train = derive_threshold(name, values, keystone)
    if train["status"] != "THRESHOLD_FROZEN_FROM_TRAIN":
        return {"status": "TRAIN_UNPOWERED", "coverage": coverage, "training": train}
    selected = train["selected"]
    eligible = sorted(d for d in values if date.fromisoformat(d) >= TRAIN_END)
    opened = {d for d in eligible if (
        values[d] <= selected["threshold"] if selected["direction"] == "low"
        else values[d] >= selected["threshold"]
    )}
    rng = random.Random(f"{SEED}:{name}")
    random_sets = [set(rng.sample(eligible, len(opened))) for _ in range(DRAWS)]
    endpoints = {"KEYSTONE": {}, "FULL_UNIVERSE_431": {}}
    for cohort, rows in (("KEYSTONE", keystone), ("FULL_UNIVERSE_431", universe)):
        for xo in EXITS:
            endpoints[cohort][str(xo)] = endpoint(rows, opened, eligible, xo, random_sets)
    subwindows = {}
    for wid, window in (("O1", O1), ("O2", O2)):
        subwindows[wid] = {"start": str(window[0]), "end_exclusive": str(window[1]),
                           "KEYSTONE": {}, "FULL_UNIVERSE_431": {}}
        for cohort, rows in (("KEYSTONE", keystone), ("FULL_UNIVERSE_431", universe)):
            for xo in EXITS:
                subwindows[wid][cohort][str(xo)] = endpoint(
                    rows, opened, eligible, xo, random_sets, window
                )
    primary_vals, _ = rows_on(keystone, opened, 1)
    positives = sorted((x for x in primary_vals if x > 0), reverse=True)
    remove_n = max(1, math.ceil(0.01 * len(primary_vals))) if primary_vals else 0
    removed = positives[:remove_n]
    trimmed = list(primary_vals)
    for value in removed:
        trimmed.remove(value)
    trim_summary = summarize(trimmed)
    primary = endpoints["KEYSTONE"]
    powered_subwindows = []
    subwindow_passes = []
    for wid in ("O1", "O2"):
        ep = subwindows[wid]["KEYSTONE"]["1"]
        if ep["true_gate"]["n"] >= 4:
            powered_subwindows.append(wid)
            subwindow_passes.append(
                ep["true_gate"]["mean"] is not None
                and ep["random_same_open_share"]["median"] is not None
                and ep["true_gate"]["mean"] > ep["random_same_open_share"]["median"]
            )
    rules = {
        "open_days_at_least_5": len(opened) >= 5,
        "primary_finite_n_at_least_8": primary["1"]["true_gate"]["n"] >= 8,
        "primary_plus1_gt_random_p95": primary["1"]["true_strictly_gt_random_p95"],
        "at_least_2_exits_gt_random_p95": sum(
            primary[str(x)]["true_strictly_gt_random_p95"] for x in EXITS
        ) >= 2,
        "all_exit_deltas_positive": all(
            primary[str(x)]["true_minus_random_median"] is not None
            and primary[str(x)]["true_minus_random_median"] > 0 for x in EXITS
        ),
        "at_least_2_powered_subwindows": len(powered_subwindows) >= 2,
        "all_powered_subwindows_gt_random_median": bool(powered_subwindows)
        and all(subwindow_passes),
        "top1pct_removed_mean_positive": trim_summary["mean"] is not None
        and trim_summary["mean"] > 0,
    }
    status = "OOS_STABLE_WIN" if all(rules.values()) else (
        "OOS_UNPOWERED" if len(opened) < 5 or primary["1"]["true_gate"]["n"] < 8
        else "NO_STABLE_OOS_WIN"
    )
    return {
        "status": status, "coverage": coverage, "training": train,
        "oos": {
            "eligible_days": len(eligible), "open_days": len(opened),
            "open_share": len(opened) / len(eligible) if eligible else None,
            "open_day_list": sorted(opened), "endpoints": endpoints,
            "subwindows": subwindows,
            "top_positive_1pct_removed": {
                "source_n": len(primary_vals), "requested_remove_n": remove_n,
                "actually_removed_positive_n": len(removed), "removed_values": removed,
                "remaining": trim_summary,
            },
            "stability_rules": rules,
        },
    }


def pct(x):
    return "—" if x is None else f"{x:+.2%}"


def render(result: dict) -> str:
    lines = [
        "---", "sop_applied: true", "alpha_id: AR-GL2-C-EXOGENOUS-WEATHER-OOS",
        "family: F6/F7", "next_gate: HUMAN_LINE_REVIEW_ONLY", "decision_flags:",
        "  training_allowed: false", "  live_allowed: false", "  self_go_allowed: false",
        "---", "", "# GL2-C 外生天气门：冻结训练派生与严格 OOS 结果", "",
        "> 纯历史只读重核；没有训练模型、下单、签名、广播、信号/Redis/live 修改或 GO。最终分级由审查 agent 逐行决定。",
        "", f"预注册 SHA256：`{result['preregistration_sha256']}`。脚本确认该哈希与冻结锚完全一致后才计算结果。",
        "", "## 人群与时间口径", "",
        "- 主人群：GL1 冻结 KEYSTONE 选中 116 条；天气不参与选择。",
        "- 次人群：GL1 同龄全宇宙 431 条；只作 secondary，不替代 KEYSTONE。",
        "- 入场固定 +1 块，出场 +1/+5/+15 块；真成交优先，否则原子守卫精确报价；confirmed rug=-100%，unresolved 保留并逐格报分母。",
        "- 每日天气只读 D 00:00 UTC 之前的值；训练 `<2026-04-15`，OOS `>=2026-04-15`。",
        "", "## 五指标可用性与机器状态", "",
        "| 指标 | 可用日 | 缺失日 | 状态 | 原因 |", "|---|---:|---:|---|---|",
    ]
    for name, item in result["indicators"].items():
        cov = item["coverage"]
        lines.append(f"| `{name}` | {cov['available_days']} | {cov['missing_days']} | `{item['status']}` | {cov.get('reason') or '本地冻结源覆盖'} |")
    lines += ["", "## 训练阈值与 OOS 开工占比", "",
              "| 指标 | 训练方向 | 阈值 | 训练开日/占比 | OOS开日/占比 | 机器标签 |",
              "|---|---|---:|---:|---:|---|"]
    for name, item in result["indicators"].items():
        if "oos" not in item:
            lines.append(f"| `{name}` | — | — | — | — | `{item['status']}` |")
            continue
        s = item["training"]["selected"]
        lines.append(
            f"| `{name}` | {s['direction']} q={s['quantile']:.2f} | {s['threshold']:.10g} | "
            f"{s['open_days']}/{s['eligible_days']} ({s['open_share']:.1%}) | "
            f"{item['oos']['open_days']}/{item['oos']['eligible_days']} ({item['oos']['open_share']:.1%}) | `{item['status']}` |"
        )
    for name, item in result["indicators"].items():
        if "oos" not in item:
            continue
        lines += ["", f"## `{name}`：逐窗分布", "",
                  "| 窗口/人群/出场 | true n/unresolved | mean | median | p10/p90 | win/fatal | random median/p95 | Δmedian | >p95 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|:---:|"]
        scopes = [("OOS", item["oos"]["endpoints"])]
        for wid in ("O1", "O2"):
            scopes.append((wid, item["oos"]["subwindows"][wid]))
        for scope, data in scopes:
            for cohort in ("KEYSTONE", "FULL_UNIVERSE_431"):
                eps = data[cohort]
                for xo in EXITS:
                    ep = eps[str(xo)]; t = ep["true_gate"]; r = ep["random_same_open_share"]
                    lines.append(
                        f"| {scope}/{cohort}/+{xo} | {t['n']}/{t['unresolved']} | {pct(t['mean'])} | {pct(t['median'])} | "
                        f"{pct(t['p10'])}/{pct(t['p90'])} | {pct(t['win_rate'])}/{pct(t['fatal_rate'])} | "
                        f"{pct(r['median'])}/{pct(r['p95'])} | {pct(ep['true_minus_random_median'])} | "
                        f"{'是' if ep['true_strictly_gt_random_p95'] else '否'} |"
                    )
        trim = item["oos"]["top_positive_1pct_removed"]
        lines += ["", f"KEYSTONE +1/+1 去最大正收益 1%：原 n={trim['source_n']}，实际移除 {trim['actually_removed_positive_n']} 条，剩余 mean={pct(trim['remaining']['mean'])}。",
                  "", "机器稳定规则：`" + item["status"] + "`；" + ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in item["oos"]["stability_rules"].items()) + "。"]
    lines += ["", "## 限制与审查边界", "",
              "- 聚合 WETH 净流入、Base gas regime 和全宇宙历史 rug rate 没有满足冻结的无前视全覆盖源，故不是零、不是失败，而是 `UNAVAILABLE_NOT_TESTED`。",
              "- ETH daily 源是既有本地冻结文件；早期不足八个 close 的日期保持 missing。",
              "- 全宇宙 431 行锚定本次运行前已有的 reaudit10 core/five-evidence 输入，没有等待或吸收 GL2-A 后续结果，避免看结果后改 C。",
              "- 机器只发 `OOS_STABLE_WIN/NO_STABLE_OOS_WIN/OOS_UNPOWERED/UNAVAILABLE_NOT_TESTED`，不定 GL1 grade，不作 GO。",
              "", "## 可复现", "", "运行：`python3 scripts/research/base_gl2/gl2_c_weather_oos.py`。随机比较器每指标 10,000 次，seed 基础值 `20260715`，严格保持同 eligible 日与同开工天数。", ""]
    return "\n".join(lines)


def main():
    if sha(PREREG) != PREREG_SHA:
        raise RuntimeError("GL2-C preregistration hash drift; results are invalid")
    keystone = keystone_rows()
    universe = full_universe_rows()
    series, coverage = indicator_series(universe)
    indicators = {
        name: evaluate_indicator(name, series[name], coverage[name], keystone, universe)
        for name in series
    }
    input_paths = [PREREG, KEYSTONE, CORE, RUG, B6, ETH,
                   ROOT / "mvp/diagnostics/base_b5_real_swaps_20260714.tsv",
                   ROOT / "mvp/diagnostics/base_b8_future_swaps_20260714.tsv",
                   ROOT / "scripts/research/base_reaudit10/reaudit10_analyze.py"]
    result = {
        "schema_version": 1, "sop_applied": True,
        "alpha_id": "AR-GL2-C-EXOGENOUS-WEATHER-OOS", "family": "F6/F7",
        "next_gate": "HUMAN_LINE_REVIEW_ONLY",
        "decision_flags": {"training_allowed": False, "live_allowed": False,
                           "self_go_allowed": False},
        "research_only": True, "go_decision": "NOT_MADE", "rpc_calls": 0,
        "preregistration_sha256": sha(PREREG),
        "chronology": {
            "training": "signal day < 2026-04-15", "oos": "signal day >= 2026-04-15",
            "weather_cutoff": "all inputs strictly before D 00:00:00Z",
            "result_computed_after_preregistration": True,
        },
        "cohorts": {"KEYSTONE": len(keystone), "FULL_UNIVERSE_431": len(universe)},
        "random_comparator": {"seed_base": SEED, "draws_per_indicator": DRAWS,
                              "same_eligible_days": True, "same_open_day_count": True},
        "indicators": indicators,
        "basket_summary": {
            "preregistered_indicators": len(indicators),
            "available_and_oos_tested": sum("oos" in x for x in indicators.values()),
            "stable_win_count": sum(x["status"] == "OOS_STABLE_WIN" for x in indicators.values()),
            "statuses": dict(Counter(x["status"] for x in indicators.values())),
            "grade_assigned": False, "go_decision": "NOT_MADE",
        },
        "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in input_paths},
        "limitations": [
            "three indicators are unavailable under their frozen no-imputation source rules",
            "GL2-A post-preregistration outputs are intentionally not incorporated",
            "historical counterfactual evidence is not our realized fill",
        ],
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    REPORT.write_text(render(result))
    print(json.dumps({"out": str(OUT), "report": str(REPORT),
                      "basket_summary": result["basket_summary"],
                      "statuses": {k: v["status"] for k, v in indicators.items()}},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
