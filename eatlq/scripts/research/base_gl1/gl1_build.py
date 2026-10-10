#!/usr/bin/env python3
"""GL1 deterministic golden-truth-ledger builder (research-only, zero RPC)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median


ROOT = Path(__file__).resolve().parents[3]
DATE = "20260715"
REPORTS = ROOT / "reports"
DIAG = ROOT / "mvp" / "diagnostics"


def load(rel: str):
    return json.loads((ROOT / rel).read_text())


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def pct(x) -> str:
    return "未知" if x is None else f"{100 * x:.2f}%"


def inventory() -> list[dict]:
    roots = [
        "reports", "docs", "mvp/audits", "mvp/diagnostics", "mvp/runs",
        "configs/research", "scripts/research/base_lefttail", "scripts/research/base_b4",
        "scripts/research/base_b5", "scripts/research/base_b6", "scripts/research/base_b8",
        "scripts/research/base_reaudit10", "scripts/research/base_gl1", "scripts/research/base_rh1_smartmoney.py",
        "scripts/research/base_rh1_smartmoney_core.py", "scripts/research/base_rm1", "tests",
    ]
    allowed = {".md", ".json", ".jsonl", ".py", ".csv", ".tsv", ".sha256", ".yml", ".yaml", ".sol"}
    strong = re.compile(
        r"keystone|quoterv2|atomic sell|sellability|honeypot|left.?tail|delayed.?rug|"
        r"p_fatal|weather.?gate|smart.?money|聪明钱|天气门|左尾|五证据|honest.?execution|"
        r"reaudit.?10|golden.?truth|2min.?accel|transfer.?restriction|rm1",
        re.I,
    )
    forced_path = re.compile(
        r"(?:base_(?:b[3-8]|reaudit10|rh1|rm1)|keystone|lefttail|honeypot|fastrug|rug_|"
        r"sellability|quoterv2|capacity|gate_sweep|risk_rebacktest|live_config_rebacktest)", re.I
    )
    excluded = ("/.venv/", "/node_modules/", "/__pycache__/", "/sol_grad_audit/", "/uni_shards/")
    found: dict[str, Path] = {}
    for root in roots:
        p = ROOT / root
        candidates = [p] if p.is_file() else (p.rglob("*") if p.exists() else [])
        for q in candidates:
            if not q.is_file() or q.suffix.lower() not in allowed:
                continue
            rel = q.relative_to(ROOT).as_posix()
            if (rel.startswith("reports/GL1_") and rel != "reports/GL1_task_spec_golden_truth_ledger_20260715.md") or rel.startswith("mvp/diagnostics/base_gl1_"):
                continue
            if any(x in f"/{rel}" for x in excluded) or q.stat().st_size > 25_000_000:
                continue
            path_hit = bool(forced_path.search(rel))
            content_hit = False
            if not path_hit and q.stat().st_size <= 2_000_000:
                try:
                    content_hit = bool(strong.search(q.read_text(errors="replace")[:250_000]))
                except OSError:
                    pass
            if path_hit or content_hit:
                found[rel] = q

    rows = []
    for rel, q in sorted(found.items()):
        text = ""
        if q.stat().st_size <= 2_000_000:
            try:
                text = q.read_text(errors="replace")[:100_000]
            except OSError:
                pass
        date = None
        m = re.search(r"20\d{6}", rel)
        if m:
            date = m.group(0)
        elif text:
            m = re.search(r"20\d{2}-\d{2}-\d{2}", text[:4000])
            date = m.group(0) if m else None
        claim = "结构化数据/代码锚点"
        if q.suffix.lower() == ".md":
            headings = [x.lstrip("# ").strip() for x in text.splitlines() if x.startswith("#")]
            claim = headings[0][:180] if headings else "研究/审计文档"
        elif q.suffix.lower() == ".json":
            try:
                obj = json.loads(text if q.stat().st_size <= 2_000_000 else q.read_text())
                claim = "结构化字段: " + ", ".join(list(obj)[:10]) if isinstance(obj, dict) else f"结构化数组 n={len(obj)}"
            except Exception:
                claim = "结构化结果（大文件，仅哈希索引）"
        elif q.suffix.lower() in {".py", ".sol"}:
            for line in text.splitlines()[:60]:
                s = line.strip(" #/*\t")
                if len(s) > 20 and not s.startswith(("import ", "from ", "pragma ")):
                    claim = s[:180]
                    break
        rows.append({
            "path": rel,
            "date": date,
            "kind": q.suffix.lower().lstrip("."),
            "bytes": q.stat().st_size,
            "sha256": sha(q),
            "core_claim": claim,
            "reproducible_script": q.suffix.lower() in {".py", ".sol"} or bool(re.search(r"scripts?/|python3|pytest", text, re.I)),
            "embedded_hash": bool(re.search(r"sha256|0x[a-f0-9]{64}", text, re.I)),
        })
    return rows


def frequency(rows: list[dict]) -> dict:
    ts = sorted(datetime.fromisoformat(r["signal_time"]) for r in rows if r["selected"])
    daily = Counter(t.date().isoformat() for t in ts)
    monthly = Counter(t.strftime("%Y-%m") for t in ts)
    week_counts = Counter((t.date() - timedelta(days=t.weekday())).isoformat() for t in ts)
    start = ts[0].date() - timedelta(days=ts[0].weekday())
    end = ts[-1].date() - timedelta(days=ts[-1].weekday())
    weekly = []
    d = start
    while d <= end:
        weekly.append({"week_start": d.isoformat(), "n": week_counts[d.isoformat()]})
        d += timedelta(days=7)
    return {
        "selected_n": len(ts),
        "first_signal_utc": ts[0].isoformat(),
        "last_signal_utc": ts[-1].isoformat(),
        "signal_span_days": (ts[-1] - ts[0]).total_seconds() / 86400,
        "active_days": len(daily),
        "daily_timeline_nonzero": [{"date": k, "n": v} for k, v in sorted(daily.items())],
        "daily_count_histogram_on_active_days": dict(sorted(Counter(daily.values()).items())),
        "weekly_timeline_including_zero_weeks": weekly,
        "weekly_count_histogram": dict(sorted(Counter(x["n"] for x in weekly).items())),
        "monthly_timeline": [{"month": k, "n": v} for k, v in sorted(monthly.items())],
        "warning": "这是历史 116 个 KEYSTONE 选中信号的分布，不是未来到达率；禁止压成单一周均值。",
    }


def cell_from_rows(rows: list[dict], key: str) -> dict:
    cells = [r["matrix"][key] for r in rows]
    resolved = [c for c in cells if c["resolved"]]
    rets = [c["ret"] for c in resolved]
    ordered = sorted(rets, reverse=True)
    top1_n = max(1, math.ceil(len(ordered) * .01))
    return {
        "n_total": len(cells), "n_resolved": len(resolved), "n_unresolved": len(cells) - len(resolved),
        "mean": mean(rets), "median": median(rets), "top1_removed_mean": mean(ordered[top1_n:]),
        "evidence": dict(sorted(Counter(c["evidence"] for c in cells).items())),
    }


def anchor(rel: str, note: str = "") -> dict:
    p = ROOT / rel
    return {"path": rel, "sha256": sha(p), "note": note}


def build_claim_history() -> list[dict]:
    # These are historical claims, not current truth. Their scopes are deliberately explicit.
    return [
        {"claim": "KEYSTONE edge", "date": "2026-06", "value": "n=131, mean +59.7%, median +30.5%, win 68%, fatal 3.8%", "scope": "single holdout; old sell simulator/exit assumptions", "source": "docs/keystone-go-live.md"},
        {"claim": "KEYSTONE edge", "date": "2026-06", "value": "n=131, hold4h mean +65.2%", "scope": "same holdout; exit-upgrade replay", "source": "mvp/audits/keystone_exit_upgrade/"},
        {"claim": "KEYSTONE edge", "date": "2026-06", "value": "n=217, mean +71.6%, median +37.5%", "scope": "monthly walk-forward; old proxy execution", "source": "scripts/research/base_lefttail/_set217_forkreplay.json"},
        {"claim": "KEYSTONE edge", "date": "2026-06", "value": "n=131, 2% haircut mean +54.2%, median +27.2%", "scope": "old holdout; fixed 2% haircut", "source": "scripts/research/base_lefttail/_risk_rebacktest_report.json"},
        {"claim": "KEYSTONE edge", "date": "2026-07-14", "value": "KEYSTONE full -4.61%; W1 +16.06%, W2 -18.48%, W3 about -100%", "scope": "30-minute exit; missing/adverse treated conservatively, B5", "source": "mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json"},
        {"claim": "KEYSTONE edge", "date": "2026-07-15", "value": "+1/+5/+15 exit mean +56.64%/+56.59%/+57.69%; top1-removed +48.29%/+48.13%/+49.40%", "scope": "entry +1 block; seconds; real sell first then guarded atomic quote; confirmed rug only; unresolved 3/4/5 retained", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "blind-buy baseline", "date": "2026-07-14", "value": "mean -68.02%", "scope": "B5 30-minute/missing-as-adverse convention", "source": "mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json"},
        {"claim": "blind-buy baseline", "date": "2026-07-15", "value": "resolved 285/431, mean +1.37%, unresolved 146", "scope": "reaudit +1 block; unresolved excluded from point estimate", "source": "reports/reaudit_10_suspect_conclusions_result_20260715.md"},
        {"claim": "QuoterV2 disagreement", "date": "2026-07-14", "value": "historical exit snapshots: 1/224 unique pools", "scope": "survivor/known-exit sets; not unbiased instant-hp prevalence", "source": "mvp/diagnostics/base_b4_arm_a_20260714.json"},
        {"claim": "QuoterV2 disagreement", "date": "2026-07-15", "value": "0/306 measured 2m disease cases; Wilson 95% upper 1.24%; unknown disease 2", "scope": "unbiased fresh probe; QV2-reasonable vs atomic severe disagreement", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "W2 toxicity", "date": "2026-07-15", "value": "2m 304/408=74.51%; 10m 311/408=76.23%", "scope": "full W2 cohort; atomic roundtrip sell failure", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "KEYSTONE W2", "date": "2026-07-15", "value": "resolved n=60 mean +54.08%; conditional alpha +30.52pp; unresolved 1", "scope": "KEYSTONE-selected W2; +1/+1 block seconds", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "rug prevalence", "date": "2026-07-14", "value": "delayed rug 262: LP-pull 260, transfer-toggle 2", "scope": "B4 candidate label; coarse historical class", "source": "mvp/diagnostics/base_b4_arm_a_20260714.json"},
        {"claim": "rug adjudication", "date": "2026-07-15", "value": "confirmed LP rug 235; temporary unsellable 1; unresolved 26", "scope": "same 262 candidates; five-evidence/no-recovery adjudication", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "capacity", "date": "2026-06", "value": "fixed $100 about $4,330/month; dynamic ceiling about $28k/month", "scope": "old survivor proxy, USD/gas/frequency assumptions", "source": "scripts/research/base_lefttail/capacity_curve_report_20260623.md"},
        {"claim": "capacity", "date": "2026-07-14", "value": "0.003/0.010 WETH median incremental impact -0.0218pp/-0.0979pp; n=76 each", "scope": "QuoterV2 diagnostic only; not realized fills", "source": "mvp/diagnostics/base_b8_honest_execution_20260714.json"},
        {"claim": "weather gate", "date": "2026-07-14", "value": "derived gate appeared to revive/kill depending data coverage", "scope": "102-day envelope but only 40 eligible days in early repair", "source": "reports/keystone_revival_professional_20260714.md"},
        {"claim": "weather gate", "date": "2026-07-15", "value": "opens 2/55 OOS days; KEYSTONE n=0; does not beat same-work random gate", "scope": "frozen causal exogenous gate; seconds exit fronts", "source": "mvp/diagnostics/base_reaudit10_final_20260715.json"},
        {"claim": "B3 acceleration", "date": "2026-07-14", "value": "true signals W1=17, W2=0, W3=0", "scope": "3,331-pool frozen universe; fresh tokens; true reserve/holder", "source": "mvp/diagnostics/base_2min_accel_b3_v1_20260714.json"},
        {"claim": "RH smart-money", "date": "2026-07-14", "value": "UNTESTED", "scope": "EOA attribution mismatch and FIFO/on-chain golden blocked", "source": "mvp/diagnostics/base_rh1_smartmoney_20260714.json"},
    ]


def make_ledger() -> dict:
    per = load("mvp/diagnostics/base_reaudit10_per_trade_20260715.json")
    final = load("mvp/diagnostics/base_reaudit10_final_20260715.json")
    b4 = load("mvp/diagnostics/base_b4_arm_b_20260714.json")
    b8 = load("mvp/diagnostics/base_b8_honest_execution_20260714.json")
    rm1 = load("mvp/diagnostics/base_rm1_v2_dryrun_20260715.json")
    rows = per["rows"]
    freq = frequency(rows)
    reverified = {key: cell_from_rows(rows, key) for key in ("1:1", "1:5", "1:15", "5:1", "15:1")}
    # Strict equality to the independently aggregated final artifact prevents silent denominator drift.
    for key in ("1:1", "1:5", "1:15", "5:1", "15:1"):
        summary = final["performance_matrix"][key]["pooled"]
        assert reverified[key]["n_resolved"] == summary["n"]
        assert reverified[key]["mean"] == summary["mean"]
        assert round(reverified[key]["top1_removed_mean"], 15) == round(summary["top1_removed_mean"], 15)
        # Preserve the canonical serialized float after the independent equality check.
        reverified[key]["top1_removed_mean"] = summary["top1_removed_mean"]

    base = reverified["1:1"]["mean"]
    impacts = b8["capacity"]["incremental_per_unit_impact_vs_0.001"]
    cap = [
        {"size_weth": .001, "quote_n": 76, "median_incremental_return_impact": 0.0, "worst_incremental_return_impact": 0.0, "projected_seconds_net_return": base, "evidence": "base mixed real-sell/guarded-quote + quote-only size delta"},
        {"size_weth": .003, "quote_n": impacts["3"]["n"], "median_incremental_return_impact": impacts["3"]["median"], "worst_incremental_return_impact": impacts["3"]["worst"], "projected_seconds_net_return": base + impacts["3"]["median"], "evidence": "quote-only size delta; not a realized 0.003 WETH fill"},
        {"size_weth": .010, "quote_n": impacts["10"]["n"], "median_incremental_return_impact": impacts["10"]["median"], "worst_incremental_return_impact": impacts["10"]["worst"], "projected_seconds_net_return": base + impacts["10"]["median"], "evidence": "quote-only size delta; not a realized 0.010 WETH fill"},
    ]
    scenarios = []
    for size in (.001, .003, .010):
        projected = next(x["projected_seconds_net_return"] for x in cap if x["size_weth"] == size)
        for n in (1, 5, 10, 20, 38, 61):
            scenarios.append({"notional_weth": size, "signals_per_month_assumption": n, "historical_model_net_weth": size * n * projected, "warning": "机械情景，不是未来月盈利预测；未含真钱执行误差。"})

    A = lambda rel, note="": [anchor(rel, note)]
    ledger = [
        {"id":"GL1-01","claim":"历史秒级 KEYSTONE 选中样本有正的回放 edge","scope":"KEYSTONE selected; 0.001 WETH; frozen WETH gas/cost model; entry +1; exit +1/+5/+15 blocks; real sell first, else guarded atomic quote; confirmed rug=-100%; unresolved retained","grade":"PINNED","answer":{"frontier":{k:reverified[k] for k in ("1:1","1:5","1:15")},"right_tail_gates_plus_1":final["performance_matrix"]["1:1"]["gates"],"formal_status":"INCONCLUSIVE because unresolved != 0"},"anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","raw row recompute")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","independent aggregate")],"flip_root":"30-minute vs seconds; missing-as-loss vs unresolved; proxy vs evidence hierarchy","open_resolution":None},
        {"id":"GL1-02","claim":"KEYSTONE 真钱净 edge","scope":"same strategy and 0.001 WETH, but actual entry+exit fills, gas and broadcast latency","grade":"OPEN","answer":"没有真钱往返，报价不能单独证明成交利润","anchors":A("mvp/diagnostics/base_rm1_v2_dryrun_20260715.json","broadcast disabled; real tx hashes empty"),"flip_root":"historical replay was repeatedly phrased as realized tradability","open_resolution":"RM1 one-arm one-roundtrip in comparable shallow pool; human arm only"},
        {"id":"GL1-03","claim":"历史出场延迟前沿","scope":"entry +1 fixed; exit +1/+5/+15 blocks; same evidence hierarchy","grade":"PINNED","answer":{"plus_1":reverified["1:1"],"plus_5":reverified["1:5"],"plus_15":reverified["1:15"],"interpretation":"2–30 seconds range did not decay monotonically; this does not prove future latency tolerance"},"anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","per-trade matrix")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","frontier")],"flip_root":"t0+600/1800s feature or exit horizon was mislabeled as execution latency","open_resolution":None},
        {"id":"GL1-04","claim":"历史入场延迟前沿","scope":"entry +1/+5/+15 blocks; exit +1 fixed","grade":"PINNED","answer":{"plus_1":reverified["1:1"],"plus_5":reverified["5:1"],"plus_15":reverified["15:1"]},"anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json"),"flip_root":"entry feature window and transaction inclusion delay were conflated","open_resolution":None},
        {"id":"GL1-05","claim":"历史 KEYSTONE 信号频率是爆发型而非稳定均匀","scope":"116 selected signals only; natural UTC calendar weeks/months; zero weeks included","grade":"PINNED","answer":freq,"anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","signal_time raw rows"),"flip_root":"131/217 trade counts from different samples were divided into a misleading average","open_resolution":None},
        {"id":"GL1-06","claim":"未来信号频率","scope":"future unseen Base market under frozen selector","grade":"OPEN","answer":"历史 distribution cannot identify future arrival rate","anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","only historical through 2026-05-26"),"flip_root":"backtest counts were presented as a monthly forecast","open_resolution":"more forward time; report weekly/monthly distribution and zero intervals, never only mean"},
        {"id":"GL1-07","claim":"容量/尺寸对历史报价 edge 的侵蚀","scope":"0.001/0.003/0.010 WETH; n=76 quote-success cohort; QuoterV2 incremental impact only","grade":"PINNED","answer":{"curve":cap,"restriction":"only the size delta is quote-measured; projected return is diagnostic, not realized"},"anchors":A("mvp/diagnostics/base_b8_honest_execution_20260714.json","capacity object")+[anchor("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","0.001 base")],"flip_root":"old USD capacity extrapolated survivor quotes into fills and stable frequency","open_resolution":None},
        {"id":"GL1-08","claim":"现实每月盈利","scope":"future count × size × actual fills; WETH native","grade":"OPEN","answer":{"mechanical_scenarios":scenarios,"usd":"not estimated; requires contemporaneous on-chain ETH/USD and actual fills"},"anchors":A("mvp/diagnostics/base_reaudit10_per_trade_20260715.json")+[anchor("mvp/diagnostics/base_b8_honest_execution_20260714.json")],"flip_root":"$4.3k/$28k combined optimistic frequency, USD, quote, survivor and size assumptions","open_resolution":"RM1 realized roundtrips plus forward frequency; then multiply distributions, not point means"},
        {"id":"GL1-09","claim":"QuoterV2 reasonable quote掩盖原子严重不可卖的假阳性率","scope":"fresh 1,265-pool probe; 2-minute measured disease cases n=306; exact atomic roundtrip comparator","grade":"PINNED","answer":{"disagreement":0,"n":306,"wilson95":[0.0,0.012398593345761188],"unknown_disease":2,"identification_warning":"unknown prevents claiming universal zero"},"anchors":A("mvp/diagnostics/base_b4_arm_b_20260714.json","raw arm-B rows")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","age cross-check")],"flip_root":"survivor exit snapshot 1/224 and unbiased entry disease question were different estimands","open_resolution":None},
        {"id":"GL1-10","claim":"W2 全人群高度有毒","scope":"full W2 n=408; atomic sell failure at 2m/10m","grade":"IRON","answer":{"two_minute":[304,408,304/408],"ten_minute":[311,408,311/408]},"anchors":A("mvp/diagnostics/base_b4_arm_b_20260714.json","2m atomic census")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","independent 2m/10m age reconstruction")],"flip_root":"full W2 toxicity was confused with selected KEYSTONE W2 returns","open_resolution":None},
        {"id":"GL1-11","claim":"KEYSTONE 选中的 W2 历史秒级表现为正","scope":"selected W2; +1/+1 blocks; resolved n=60; unresolved 1","grade":"PINNED","answer":{"mean":0.5407906579197636,"top1_removed_mean":0.46901919800344766,"alpha_delta":0.30517340036285995,"unresolved":1},"anchors":A("mvp/diagnostics/base_reaudit10_final_20260715.json")+[anchor("mvp/diagnostics/base_reaudit10_per_trade_20260715.json")],"flip_root":"ecological fallacy: full W2 was substituted for selected subset","open_resolution":None},
        {"id":"GL1-12","claim":"无条件瞎买的完整经济 EV","scope":"all 431 candidates; +1/+1; same evidence hierarchy","grade":"OPEN","answer":{"resolved_only_n":285,"resolved_only_mean":0.013701194529929463,"unresolved":146,"warning":"resolved-only mean is not the full-universe EV"},"anchors":A("mvp/diagnostics/base_reaudit10_final_20260715.json"),"flip_root":"-68% imputed missing as loss; +1.37% conditions on resolved; neither identifies full EV","open_resolution":"historically settle 146 unresolved using five-evidence hierarchy without default loss"},
        {"id":"GL1-13","claim":"五证据 rug 裁定","scope":"262 delayed-rug candidates; mechanics + pre/post atomic + real sell + later recovery + cutoff","grade":"IRON","answer":{"CONFIRMED_LP_RUG":235,"TEMPORARY_UNSELLABLE":1,"UNRESOLVED":26,"confirmed_share":235/262,"unresolved_not_loss":True},"anchors":A("mvp/diagnostics/base_reaudit10_rug_five_evidence_rpc_20260715.json","multi-source raw evidence")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","independent classification output")],"flip_root":"quote failure/label was previously treated as rug without recovery and mechanics proof","open_resolution":None},
        {"id":"GL1-14","claim":"B3 2-minute acceleration signal density","scope":"frozen 3,331 pools; fresh token; true pool-era transfers/reserves/holders","grade":"IRON","answer":{"W1":17,"W2":0,"W3":0,"seconds_reprice_measured_W1":15,"seconds_unresolved_W1":2},"anchors":A("mvp/diagnostics/base_2min_accel_b3_v1_20260714.json","B3 result")+[anchor("mvp/diagnostics/base_reaudit10_b3_seconds_rpc_20260715.json","separate seconds replay")],"flip_root":"proxy 64 signals and true 17 signals were mixed; V2 rows were coerced into V3","open_resolution":None},
        {"id":"GL1-15","claim":"D5 对历史 KEYSTONE 选中样本的收益边际","scope":"all +1/+3/+5/+10/+15 entry-exit matrix cells","grade":"IRON","answer":{"blocked":0,"mean_delta":0.0,"safety_role":"still mandatory pre-entry guard; zero historical marginal != useless in future"},"anchors":A("mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json","first method")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json","seconds matrix cross-check")],"flip_root":"a mandatory safety gate's role was confused with observed incremental alpha","open_resolution":None},
        {"id":"GL1-16","claim":"forward LP monitor historically rescued losses","scope":"24 measured forward cases; block-level archive monitor","grade":"PINNED","answer":{"real_loss":4,"prewarning_among_real_loss":0,"rescue_proven":0,"prewarning_total":2},"anchors":A("mvp/diagnostics/base_reaudit10_final_20260715.json")+[anchor("mvp/diagnostics/base_b5_lp_monitor_20260714.json")],"flip_root":"theoretical pending-mempool possibility was stated as archive-proven rescue","open_resolution":None},
        {"id":"GL1-17","claim":"当前冻结天气门能挑对时机","scope":"causal exogenous derived gate; 55 OOS eligible days; compare same-open-share random gates","grade":"PINNED","answer":{"open_days":2,"selected_trades_on_open_days":0,"beats_random_p95":False},"anchors":A("mvp/diagnostics/base_b6_keystone_regime_gate_corrected_20260714.json")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json")],"flip_root":"102 calendar envelope, 40 eligible repair days, regime labels and post-hoc W1/W2/W3 were mixed","open_resolution":None},
        {"id":"GL1-18","claim":"任意可能的天气门都无效","scope":"all future causal exogenous gates","grade":"OPEN","answer":"one frozen gate failed; universal claim is not identified","anchors":A("mvp/diagnostics/base_reaudit10_final_20260715.json"),"flip_root":"failure of one threshold was over-generalized","open_resolution":"new preregistered external metric with rolling/OOS and same-work random comparator"},
        {"id":"GL1-19","claim":"RH 聪明钱可复制 alpha","scope":"point-in-time attributed EOAs, true holdings, FIFO PnL, costed exits","grade":"OPEN","answer":"UNTESTED; attribution and independent FIFO/on-chain golden blocked","anchors":A("mvp/diagnostics/base_rh1_smartmoney_20260714.json")+[anchor("reports/base_rh1_smartmoney_falsification_20260714.md")],"flip_root":"recipient/transfer proxy and repeated wallet activity were treated as true copyable holdings","open_resolution":"historical: close B2 EOA mismatch <=2% and independent FIFO golden"},
        {"id":"GL1-20","claim":"RM1 禁广播框架与硬停在干跑中生效","scope":"fixed block 42,702,284; 0.001 WETH shallow pool; live_broadcast disabled","grade":"IRON","answer":{"signing":rm1["signing_performed"],"broadcast":rm1["broadcast_performed"],"send_calls":0,"real_tx_hashes":rm1["real_transaction_hashes"],"read_only_rpc_attempts":rm1["rpc"]["http_attempts"]},"anchors":A("mvp/diagnostics/base_rm1_v2_dryrun_20260715.json","runtime evidence")+[anchor("tests/test_base_rm1.py","12 regression assertions")],"flip_root":"dry-run pipeline proof and realized execution evidence were not separated","open_resolution":None},
        {"id":"GL1-21","claim":"当前可以作 GO","scope":"all GL1 evidence","grade":"IRON","answer":{"go":"NOT_MADE","live_allowed":False,"self_go_allowed":False},"anchors":A("reports/GL1_task_spec_golden_truth_ledger_20260715.md")+[anchor("mvp/diagnostics/base_reaudit10_final_20260715.json")],"flip_root":"positive historical edge was mistaken for authorization","open_resolution":None},
        {"id":"GL1-22","claim":"102 天、40 天和 55 天分别是什么","scope":"UTC calendar envelope vs original weather eligibility intersection vs corrected OOS eligibility","grade":"PINNED","answer":{"calendar_dates_inclusive":102,"exact_elapsed_days":101.53362268518518,"requested_start":"2026-02-27T05:41:15Z","requested_end":"2026-06-08T18:29:40Z","original_eligible_days":40,"original_segments":[["2026-04-17","2026-05-13",27],["2026-05-27","2026-06-08",13]],"corrected_oos_eligible_days":55,"meaning":"102 is an envelope, 40 was a rule intersection, neither means 102 fully observed trading days"},"anchors":A("mvp/diagnostics/base_b6_keystone_regime_gate_corrected_20260714.json","requested envelope and corrected coverage")+[anchor("reports/keystone_history_and_alpha_claims_audit_20260714.md","independent date/intersection audit")],"flip_root":"calendar dates, active pool dates, feature-complete dates and OOS dates were all called 'days'","open_resolution":None},
        {"id":"GL1-23","claim":"长期日历时间上的 alpha 衰减速度","scope":"future performance decay under a frozen selector, distinct from 2–30 second execution latency","grade":"OPEN","answer":{"historical_observation":"later rolling windows became sparse and weaker; overlapping windows and selection history do not identify a stable decay law","not_same_as":"seconds latency frontier"},"anchors":A("mvp/diagnostics/base_reaudit10_final_20260715.json","14d rolling history")+[anchor("mvp/diagnostics/base_reaudit10_per_trade_20260715.json","signal-time distribution")],"flip_root":"seconds-scale execution decay and months-scale market drift were treated as one decay question","open_resolution":"more forward/OOS calendar time under frozen selector; report non-overlapping windows and arrival distribution"},
    ]
    return {
        "schema_version":"gl1_golden_truth_ledger_v1",
        "generated_utc":"2026-07-15T00:00:00Z",
        "research_only":True,"new_rpc_calls":0,"go_decision":"NOT_MADE",
        "grading_notice":"Codex proposes grades; independent review agent retains final line-by-line grading authority.",
        "authoritative_scope":{
            "execution":"automated seconds, entry/exit block offsets explicit (+1/+5/+15 primary)",
            "missing":"never default to -100%; only confirmed non-recovering rug is -100%",
            "evidence_priority":["REAL_SELL","ATOMIC_GUARDED_EXACT_QUOTE","CONFIRMED_NONRECOVERING_RUG","UNRESOLVED"],
            "pricing":"WETH native; true reserves/holdings; quote explicitly not a fill",
            "cohort":"always explicit: full universe vs KEYSTONE-selected subset",
        },
        "source_preregistration_sha256":final["preregistration_sha256"],
        "reverified_cells":reverified,"frequency_distribution":freq,"capacity_curve":cap,
        "historical_claims":build_claim_history(),"claims":ledger,
    }


def frontmatter(title: str) -> str:
    return f"""---
sop_applied: true
alpha_id: AR-GOLDEN-TRUTH-LEDGER
family: F1/F4/F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# {title}

"""


def render_doc_index(rows: list[dict]) -> str:
    counts = Counter(r["kind"] for r in rows)
    out = frontmatter("GL1 文档普查索引")
    out += f"共收录 **{len(rows)}** 个语义相关工件；规则排除了依赖包、缓存、大于 25MB 的原始分片和仅因哈希偶然包含 `B4/B5` 的误报。类型分布：`{dict(sorted(counts.items()))}`。每一行的 SHA256 都由本次脚本直接计算。\n\n"
    out += "| 路径 | 日期 | 类型 | 核心主张/角色 | 可重跑 | 有 hash | SHA256 |\n|---|---|---:|---|:---:|:---:|---|\n"
    for r in rows:
        claim = r["core_claim"].replace("|", "\\|").replace("\n", " ")
        out += f"| `{r['path']}` | {r['date'] or '未标'} | {r['kind']} | {claim} | {'是' if r['reproducible_script'] else '否'} | {'是' if r['embedded_hash'] else '否'} | `{r['sha256']}` |\n"
    return out


def render_ledger(d: dict) -> str:
    out = frontmatter("GL1 项目终局真相账本（候选分级，待审查 agent 逐行终审）")
    out += "权威口径：秒级区块偏移；缺失保留未知；真成交优先，否则明确写原子守卫报价；只有五证据确认且不恢复的 rug 才记全损；真储备/真持仓；WETH 计价；全宇宙和 KEYSTONE 子集绝不混写。**本轮新增 RPC=0，GO=NOT_MADE。**\n\n"
    out += "## 总账\n\n| ID | 主张 | 分级 | 权威口径下答案 | 历史为何翻转 | OPEN 怎么定 |\n|---|---|:---:|---|---|---|\n"
    for c in d["claims"]:
        ans = json.dumps(c["answer"], ensure_ascii=False, sort_keys=True).replace("|","\\|")
        if len(ans)>420: ans=ans[:417]+"…"
        out += f"| {c['id']} | {c['claim']}<br><small>{c['scope']}</small> | **{c['grade']}** | {ans} | {c['flip_root']} | {c['open_resolution'] or '—'} |\n"
    out += "\n## 历次量化主张（只记录当时怎么说，不拿旧文档给当前答案背书）\n\n| 主张 | 日期 | 当时数值 | 当时口径 | 出处 |\n|---|---|---|---|---|\n"
    for h in d["historical_claims"]:
        out += f"| {h['claim']} | {h['date']} | {h['value']} | {h['scope']} | `{h['source']}` |\n"
    f=d["frequency_distribution"]
    out += "\n## 频率时间分布（禁止用单一均值代替）\n\n"
    out += "自然周（含零周）：`"+", ".join(f"{x['week_start']}={x['n']}" for x in f["weekly_timeline_including_zero_weeks"])+"`。\n\n"
    out += f"周计数直方图（信号数→周数）：`{f['weekly_count_histogram']}`；月份：`{f['monthly_timeline']}`；有信号日计数直方图：`{f['daily_count_histogram_on_active_days']}`。\n\n"
    out += "## 容量与现实月收益\n\n| 尺寸 WETH | 报价 n | 相对 0.001 的中位收益侵蚀 | 最坏侵蚀 | 诊断性历史净收益 | 证据限制 |\n|---:|---:|---:|---:|---:|---|\n"
    for x in d["capacity_curve"]:
        out += f"| {x['size_weth']:.3f} | {x['quote_n']} | {pct(x['median_incremental_return_impact'])} | {pct(x['worst_incremental_return_impact'])} | {pct(x['projected_seconds_net_return'])} | {x['evidence']} |\n"
    out += "\n旧的 `$4,330/月`、`$28k/月` 不是权威答案。现实月收益仍为 OPEN；机器 JSON 给出 1/5/10/20/38/61 笔与三种尺寸的机械 WETH 情景，明确不是预测，也不做无依据 USD 换算。\n\n"
    out += "## 锚点\n\n每条 claim 的完整文件 SHA256、说明和 OPEN 解决方法均在机器 JSON；本 Markdown 不重复隐藏长 hash。分级为 Codex 候选，终审权保留给审查 agent。\n"
    return out


def render_conflicts(d: dict) -> str:
    rows = [
        ("KEYSTONE 活/死","+59.7/+71.6 → -4.61 → 秒级 +56.64","执行延迟；缺失处理；证据优先级；样本配置","这些是不同估计量；当前历史秒级=PINNED，真钱=OPEN"),
        ("瞎买 -68%/+1.37%","-68.02 → resolved-only +1.37","缺失=-100% vs unresolved 排除","两者都不是 431 全体 EV；完整 EV=OPEN"),
        ("QuoterV2 诚实/不诚实","1/224 vs 0/306","幸存者退出集 vs 无偏入场疾病集","不同人群/问题，不是直接矛盾"),
        ("W2 有毒/KEYSTONE W2 赚钱","304/408 不可卖 vs selected n60 +54.08%","全 W2 vs KEYSTONE 子集","全域毒性和选择器避毒可同时成立"),
        ("rug 262/235","粗标签 262 vs confirmed 235+temporary1+unknown26","标签 vs 五证据分类/恢复检查","只有 235 可确认全损"),
        ("天气门有效/无效","102天/40天/55 OOS天","日历外壳、资料齐备交集、修复后 OOS eligibility","当前冻结门未胜随机；不能外推所有门"),
        ("容量 $28k/未知","旧 USD 动态上限 vs 0.001–0.010 WETH quote curve","频率、USD、gas、幸存者、报价=成交","报价尺寸侵蚀已钉；实现容量和月钱 OPEN"),
        ("速度能否救 rug","理论抢跑 vs archive rescue=0/4","pending 可观测性 vs canonical block hindsight","历史没有救援证据；mempool 可能性仍 OPEN"),
        ("D5 有用/零边际","安全必需 vs blocked=0","防护职责 vs 已选样本增量收益","两句可同时成立"),
        ("alpha 衰减/未衰减","2–30秒不衰减 vs 后期窗口稀疏/弱","毫秒延迟曲线 vs 日历时间漂移","短延迟历史稳定；未来/长期衰减 OPEN"),
        ("RH 聪明钱 alpha","早期转账/钱包代理正面叙事 vs UNTESTED","接收地址 vs EOA/真持仓/FIFO","未闭合归因前不得作 edge 结论"),
    ]
    out=frontmatter("GL1 冲突根因诊断表")
    out += "| 冲突主张 | 历次表面翻转 | 真正变动的口径 | 统一裁定 |\n|---|---|---|---|\n"
    for r in rows: out += "| " + " | ".join(r) + " |\n"
    out += "\n结论：大多数所谓‘翻烧饼’不是同口径复算不稳定，而是问题、分母、时间、缺失处理或成交证据换了却没写。今后任何脱离 cohort、block offset、evidence class、missing policy、size/gas 的数字，视为没有结论。\n"
    return out


def render_rules(d: dict) -> str:
    out=frontmatter("GL1 黄金铁律清单（IRON + PINNED，候选终审版）")
    out += "下列事实仅在各自行内口径成立；不允许删掉口径后泛化。\n\n"
    for c in d["claims"]:
        if c["grade"] in {"IRON","PINNED"}:
            out += f"- **{c['id']} · {c['grade']} · {c['claim']}**：{c['scope']}。答案见机器账本；主要翻转根因：{c['flip_root']}\n\n"
    out += "审查 agent 可降级或改级；Codex 不自我背书、不作 GO。\n"
    return out


def render_open(d: dict) -> str:
    priorities={"GL1-02":1,"GL1-08":2,"GL1-06":3,"GL1-23":4,"GL1-12":5,"GL1-18":6,"GL1-19":7}
    mode={"GL1-02":"需极小额真钱（RM1）","GL1-08":"需极小额真钱 + 更多时间","GL1-06":"需更多时间数据","GL1-23":"需更多时间数据","GL1-12":"历史可解","GL1-18":"历史可解 + OOS","GL1-19":"历史可解"}
    opens=sorted((c for c in d["claims"] if c["grade"]=="OPEN"),key=lambda c:priorities[c["id"]])
    out=frontmatter("GL1 未验证清单（按对挣钱的重要性排序）")
    out += "| 优先级 | ID | 还不知道什么 | 定案方式 | 类型 |\n|---:|---|---|---|---|\n"
    for i,c in enumerate(opens,1): out += f"| {i} | {c['id']} | {c['claim']} | {c['open_resolution']} | {mode[c['id']]} |\n"
    out += "\n任何 OPEN 都不得用最坏值偷偷填成亏光，也不得用最好报价偷偷填成成交。\n"
    return out


def render_plain(d: dict) -> str:
    f=d["frequency_distribution"]
    return frontmatter("GL1 人话版：我们现在到底知道什么") + f"""## 哪些是铁的事实

历史里那套挑币办法，在同一套秒级算法重放中，确实挑到过一批明显好于同期乱买的币。以入场后约一块、出场再晚一块为例，116 个信号里 113 个能按严格证据定价，平均约赚 56.64%，去掉最强的约 1% 后约赚 48.29%；另外 3 个就是不知道，没把它们硬说成赚，也没说成亏光。把出场推迟到约 10 秒或 30 秒，历史曲线没有垮。W2 像一片很毒的沼泽：全体里 304/408 在两分钟不能完成原子卖出，但筛选器挑中的 W2 小岛，已定价 60 个平均仍约 +54.08%。这两件事不冲突。

信号不是水龙头匀速流。{f['selected_n']} 个历史信号只落在 {f['active_days']} 个有信号日；自然周是 13、25、0、0、0、50、0、3、5、6、6、5、2、1 笔，月份是 38、61、17 笔。容量方面，76 个能报价的样本显示，把单笔从 0.001 WETH 放大到 0.003/0.010 WETH，中位报价收益只少约 0.022/0.098 个百分点；但这是报价压力测试，不是真钱成交。QuoterV2 在 306 个已知原子不可卖病例里没有出现“它说合理、原子测试却严重失败”，95% 上界 1.24%，另有 2 个未知。262 个可疑 rug 经过五路证据后，235 个确认抽池、1 个后来恢复、26 个仍未知；只有确认的 235 个能按亏光处理。

## 哪些还不知道

最重要的未知是：历史报价和少量链上别人的成交，换成我们自己的真钱买入再卖出后，究竟会少多少；所以不能把历史 +56.64% 直接当成银行账上的利润。未来一个月会来几笔信号也不知道，历史分布太爆发，不能拿平均数冒充预测。真实月赚多少钱、实际可放大到多大、431 个乱买候选里 146 个未知最终怎样、有没有别的真正有效天气开关、聪明钱能否复制，都还是未验证。RM1 已证明禁广播管道和八类硬停能工作，但还没有真钱交易。

## 为什么以前反复得到不同答案

以前常把不同尺子量出的数写成同一个问题：有时等半小时才卖，有时两秒就卖；有时资料缺了就算亏光，有时保留“不知道”；有时看全部新池，有时只看筛选器挑中的币；有时把报价当成交，有时看真实成交；还有时把 102 个日历日说成 102 个资料齐全天。尺子一换，答案当然换。这本账本强制每个数都带上“看谁、何时买卖、缺失怎么办、用成交还是报价、下多大、扣什么成本”。不同尺子的数不再互相打架，同一尺子下才允许比较。
"""


def main() -> None:
    docs=inventory()
    ledger=make_ledger()
    doc_json=DIAG/f"base_gl1_doc_index_{DATE}.json"
    led_json=DIAG/f"base_gl1_golden_truth_ledger_{DATE}.json"
    dump(doc_json,{"schema_version":"gl1_doc_index_v1","count":len(docs),"selection_rule":"semantic/path relevance; excludes dependencies/cache/large raw shards","documents":docs})
    dump(led_json,ledger)
    outputs={
        REPORTS/f"GL1_doc_index_{DATE}.md":render_doc_index(docs),
        REPORTS/f"GL1_golden_truth_ledger_{DATE}.md":render_ledger(ledger),
        REPORTS/f"GL1_conflict_root_causes_{DATE}.md":render_conflicts(ledger),
        REPORTS/f"GL1_golden_rules_{DATE}.md":render_rules(ledger),
        REPORTS/f"GL1_open_questions_{DATE}.md":render_open(ledger),
        REPORTS/f"GL1_plain_language_summary_{DATE}.md":render_plain(ledger),
    }
    for path,text in outputs.items(): path.write_text(text)
    hashes={str(p.relative_to(ROOT)):sha(p) for p in [doc_json,led_json,*outputs]}
    hashes["scripts/research/base_gl1/gl1_build.py"]=sha(Path(__file__))
    dump(DIAG/f"base_gl1_artifact_hashes_{DATE}.json",{
        "schema_version":"gl1_hash_ledger_v1","new_rpc_calls":0,"go_decision":"NOT_MADE",
        "source_preregistration_sha256":ledger["source_preregistration_sha256"],"artifacts":hashes})
    print(json.dumps({"doc_count":len(docs),"claim_count":len(ledger["claims"]),"grades":dict(Counter(c["grade"] for c in ledger["claims"])),"hashes":hashes},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
