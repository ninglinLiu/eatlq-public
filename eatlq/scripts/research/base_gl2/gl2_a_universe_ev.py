#!/usr/bin/env python3
"""GL2-A: frozen five-evidence universe EV re-adjudication.

This is an offline/read-only reducer over the evidence already collected by the
seconds re-audit.  It deliberately has no transaction, signer, Redis, live or
signal-writing path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL2_A_prereg_five_evidence_20260715.md"
CORE = DIAG / "base_reaudit10_seconds_rpc_20260715.json"
RUG = DIAG / "base_reaudit10_rug_five_evidence_rpc_20260715.json"
ETH = DIAG / "base_2min_accel_b3_eth_daily_20260714.json"
OUT = DIAG / "base_gl2_a_universe_ev_20260715.json"
REPORT = ROOT / "reports/GL2_A_universe_ev_result_20260715.md"
RPC_LEDGER = DIAG / "base_gl2_a_rpc_ledger_20260715.json"
PREREG_SHA256 = "331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07"
SPEND = 10**15
WINDOWS = (1, 5, 15)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def stable_key(row: dict) -> str:
    raw = "|".join(
        (row["pool"].lower(), row["token"].lower(), str(row["entry_block"]), str(row["trigger_block"]))
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def percentile(values: list[float], q: float) -> float | None:
    return None if not values else float(np.quantile(np.asarray(values), q, method="linear"))


def stats(rows: list[dict], window: int, exclude_rug: bool) -> dict:
    cells = [r["windows"][str(window)] for r in rows]
    priced = [c for c in cells if c["resolved"]]
    if exclude_rug:
        priced = [c for c in priced if c["evidence"] != "CONFIRMED_NONRECOVERING_RUG"]
    values = [float(c["net_return"]) for c in priced]
    rugs = sum(c["evidence"] == "CONFIRMED_NONRECOVERING_RUG" for c in cells)
    unresolved = sum(not c["resolved"] for c in cells)
    total_net_raw = sum(int(c["net_weth_raw"]) for c in priced)
    return {
        "n_total": len(cells),
        "n_resolved": len(priced),
        "n_confirmed_rug": rugs,
        "n_unresolved": unresolved,
        "resolved_coverage": len(priced) / len(cells) if cells else None,
        "evidence_counts": dict(sorted(Counter(c["evidence"] for c in cells).items())),
        "mean": float(np.mean(values)) if values else None,
        "median": percentile(values, 0.50),
        "p05": percentile(values, 0.05),
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p95": percentile(values, 0.95),
        "win_rate": sum(v > 0 for v in values) / len(values) if values else None,
        "total_net_weth_raw": str(total_net_raw),
        "total_net_weth": total_net_raw / 1e18,
        "partial_identification": unresolved > 0,
        "unresolved_minus_100pct_lower_bound_mean": (
            (sum(values) - unresolved) / len(cells) if cells else None
        ),
        "upper_bound": "UNBOUNDED_WITHOUT_AN_UPSIDE_CAP" if unresolved else (max(values) if values else None),
    }


def strict_rug_evidence(detail: dict | None, target_block: int, cutoff: int) -> dict:
    if not detail:
        return {
            "E1_mechanism": None,
            "E2_pre_atomic_sellable": None,
            "E2_post_failure": None,
            "E3_no_later_real_sell_through_cutoff": None,
            "E4_no_recovery_through_cutoff": None,
            "E5_observation_complete": target_block <= cutoff,
            "confirmed_nonrecovering_rug": False,
            "source": None,
        }
    depth = detail.get("depth_ratio")
    e1 = bool(detail.get("mechanics") and depth is not None and depth <= 0.05)
    e2pre = detail.get("pre_atomic_sell_ok") is True
    e2post = detail.get("post_atomic_bad") is True
    e3 = detail.get("later_actual_sell") is False
    e4 = bool(detail.get("no_recovery_through_cutoff") and not detail.get("atomic_recovery"))
    e5 = target_block <= cutoff
    confirmed = bool(e1 and e2pre and e2post and e3 and e4 and e5)
    return {
        "E1_mechanism": e1,
        "E1_depth_ratio": depth,
        "E1_candidate_tx": detail.get("candidate_tx"),
        "E2_pre_atomic_sellable": e2pre,
        "E2_post_failure": e2post,
        "E3_no_later_real_sell_through_cutoff": e3,
        "E4_no_recovery_through_cutoff": e4,
        "E5_observation_complete": e5,
        "confirmed_nonrecovering_rug": confirmed,
        "source": "mvp/diagnostics/base_reaudit10_rug_five_evidence_rpc_20260715.json",
    }


def normalize_cell(src: dict, old: dict, window: int, rug_detail: dict | None, eth_usd: float, cutoff: int) -> dict:
    ep = next(p for p in src["entry_points"] if p["offset_blocks"] == 1)
    xp = next(p for p in src["exit_points"] if p["offset_blocks"] == window)
    qty = int(ep.get("atomic", {}).get("received_target", 0) or 0)
    quote = xp.get("exact_size_quotes_by_entry_offset", {}).get("1", {})
    five = strict_rug_evidence(rug_detail, int(xp["scheduled_block"]), cutoff)
    base = {
        "entry_offset_blocks": 1,
        "exit_offset_blocks": window,
        "entry_block": int(ep["scheduled_block"]),
        "exit_block": int(xp["scheduled_block"]),
        "entry_weth_raw": str(SPEND),
        "true_position_token_raw": str(qty) if qty > 0 else None,
        "atomic_quote_amount_out_raw": str(quote.get("amount_out")) if quote.get("status") == "measured" else None,
        "atomic_quote_request_sha256": quote.get("request_sha256"),
        "real_sell_tx": old.get("exit_tx") if old.get("evidence") == "REAL_SELL" else None,
        "real_sell_log_index": old.get("exit_log_index") if old.get("evidence") == "REAL_SELL" else None,
        "five_evidence": five,
        "rpc_errors": [
            x.get("error")
            for x in (ep.get("atomic", {}), xp.get("atomic_guard", {}), quote)
            if x.get("status") == "unknown" and x.get("error")
        ],
    }

    # A pool sell cannot be asserted to be this synthetic account's fill.  If
    # the exact-size same-block quote exists, the sell only confirms sellability
    # and the monetary amount remains the quote, as frozen in the preregistration.
    if old.get("evidence") == "REAL_SELL":
        amount = int(quote.get("amount_out", 0) or 0)
        if quote.get("status") == "measured" and amount > 0:
            attempts = int(old.get("attempts", 1))
            cost_raw = int(round((1 + attempts) * 0.075 / eth_usd * 1e18))
            net_raw = amount - SPEND - cost_raw
            return {
                **base,
                "resolved": True,
                "evidence": "ATOMIC_GUARDED_EXACT_QUOTE_REAL_SELL_CONFIRMED",
                "exit_weth_raw": str(amount),
                "frozen_cost_weth_raw": str(cost_raw),
                "net_weth_raw": str(net_raw),
                "net_return": net_raw / SPEND,
                "unresolved_reason": None,
            }
        old = {**old, "resolved": False, "evidence": "UNRESOLVED_REAL_SELL_NOT_POSITION_FILL_AND_NO_EXACT_QUOTE"}

    if old.get("resolved") and old.get("evidence") == "ATOMIC_GUARDED_EXACT_QUOTE":
        amount = int(old["proceeds"])
        attempts = int(old.get("attempts", 1))
        cost_raw = int(round((1 + attempts) * 0.075 / eth_usd * 1e18))
        net_raw = amount - SPEND - cost_raw
        return {
            **base,
            "resolved": True,
            "evidence": "ATOMIC_GUARDED_EXACT_QUOTE",
            "exit_weth_raw": str(amount),
            "frozen_cost_weth_raw": str(cost_raw),
            "net_weth_raw": str(net_raw),
            "net_return": net_raw / SPEND,
            "unresolved_reason": None,
        }

    if old.get("resolved") and old.get("evidence") == "NO_ENTRY_FLAT":
        return {
            **base,
            "resolved": True,
            "evidence": "NO_ENTRY_FLAT",
            "exit_weth_raw": str(SPEND),
            "frozen_cost_weth_raw": "0",
            "net_weth_raw": "0",
            "net_return": 0.0,
            "unresolved_reason": None,
        }

    if five["confirmed_nonrecovering_rug"]:
        return {
            **base,
            "resolved": True,
            "evidence": "CONFIRMED_NONRECOVERING_RUG",
            "exit_weth_raw": "0",
            "frozen_cost_weth_raw": "0",
            "net_weth_raw": str(-SPEND),
            "net_return": -1.0,
            "unresolved_reason": None,
        }

    reason = old.get("evidence", "UNRESOLVED_EXIT")
    if five.get("E4_no_recovery_through_cutoff") is False:
        reason = "UNRESOLVED_TARGET_WINDOW_RECOVERED_LATER"
    elif not five.get("E5_observation_complete"):
        reason = "UNRESOLVED_RIGHT_CENSORED"
    return {
        **base,
        "resolved": False,
        "evidence": reason if str(reason).startswith("UNRESOLVED") else "UNRESOLVED_FIVE_EVIDENCE_INCOMPLETE",
        "exit_weth_raw": None,
        "frozen_cost_weth_raw": None,
        "net_weth_raw": "0",
        "net_return": None,
        "unresolved_reason": reason,
    }


def comparison(full: dict, selected: dict) -> dict:
    if full["mean"] is None or selected["mean"] is None:
        alpha = None
    else:
        alpha = selected["mean"] - full["mean"]
    kmean = selected["mean"]
    return {
        "beta_component_full_universe_mean": full["mean"],
        "selection_alpha_component": alpha,
        "keystone_mean_identity_rhs": None if alpha is None else full["mean"] + alpha,
        "keystone_mean": kmean,
        "beta_signed_share_of_keystone_mean": None if kmean is None or math.isclose(kmean, 0.0, abs_tol=1e-12) else full["mean"] / kmean,
        "selection_alpha_signed_share_of_keystone_mean": None if kmean is None or math.isclose(kmean, 0.0, abs_tol=1e-12) else alpha / kmean,
        "resolved_coverage_gap_keystone_minus_universe": selected["resolved_coverage"] - full["resolved_coverage"],
        "partial_identification": bool(full["n_unresolved"] or selected["n_unresolved"]),
    }


def main() -> None:
    if sha(PREREG) != PREREG_SHA256:
        raise RuntimeError("frozen GL2-A preregistration hash mismatch")
    ana = load_module(ROOT / "scripts/research/base_reaudit10/reaudit10_analyze.py", "reaudit10_gl2a")
    core = json.loads(CORE.read_text())
    rug_raw = json.loads(RUG.read_text())
    swaps = ana.extend_rug_swaps(ana.load_swaps(), rug_raw)
    orient_by_pool = {r["pool"]: int(r["orient"]) for r in core["rows"]}
    rug_classes, rug_summary = ana.rug_classes(rug_raw, swaps, orient_by_pool)
    rug_detail = {r["pool"]: r for r in rug_summary["rows"]}
    eth_closes = json.loads(ETH.read_text())["closes"]
    cutoff = int(ana.CUTOFF)

    rows = []
    for src in sorted(core["rows"], key=lambda r: (r["pool"], r["entry_block"], r["trigger_block"])):
        eth_usd = float(eth_closes.get(src["signal_time"][:10]) or min(eth_closes.values()))
        windows = {}
        for window in WINDOWS:
            old = ana.classify_cell(src, 1, window, swaps, rug_classes, eth_usd)
            windows[str(window)] = normalize_cell(src, old, window, rug_detail.get(src["pool"]), eth_usd, cutoff)
        rows.append(
            {
                "stable_key": stable_key(src),
                "pool": src["pool"],
                "token": src["token"],
                "orient": int(src["orient"]),
                "regime": src["regime"],
                "signal_time": src["signal_time"],
                "candidate_entry_base_block": int(src["entry_block"]),
                "candidate_exit_trigger_block": int(src["trigger_block"]),
                "cohort": {"full_universe": True, "keystone_selected": bool(src["selected"])},
                "windows": windows,
            }
        )

    if len(rows) != 431 or len({r["stable_key"] for r in rows}) != 431:
        raise RuntimeError("frozen universe identity mismatch")
    selected = [r for r in rows if r["cohort"]["keystone_selected"]]
    summaries = {}
    for w in WINDOWS:
        summaries[str(w)] = {}
        for policy, exclude in (("including_confirmed_rugs", False), ("excluding_confirmed_rugs_survivor_conditioned", True)):
            fu = stats(rows, w, exclude)
            ks = stats(selected, w, exclude)
            summaries[str(w)][policy] = {
                "full_universe": fu,
                "keystone_subset": ks,
                "alpha_beta_decomposition": comparison(fu, ks),
            }

    payload = {
        "schema_version": 1,
        "alpha_id": "AR-GL2-A-UNIVERSE-EV",
        "sop_applied": True,
        "research_only": True,
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "next_gate": "HUMAN_LINE_REVIEW_ONLY",
        "preregistration": {"path": str(PREREG.relative_to(ROOT)), "sha256": PREREG_SHA256, "modified": False},
        "population": {"full_universe_n": len(rows), "keystone_subset_n": len(selected), "pricing": "WETH", "entry_notional_weth_raw": str(SPEND)},
        "cutoff": {"timestamp": "2026-06-08T18:29:40Z", "block": cutoff},
        "windows": {"entry_offset_blocks": 1, "exit_offset_blocks": list(WINDOWS)},
        "summaries": summaries,
        "rows": rows,
        "rpc_ledger": {"endpoint_alias": "prettiest-blue-aura", "new_logical_calls": 0, "methods": {}, "posture": "existing pinned evidence only; no send/sign/broadcast"},
        "source_hashes": {
            str(CORE.relative_to(ROOT)): sha(CORE),
            str(RUG.relative_to(ROOT)): sha(RUG),
            str(ETH.relative_to(ROOT)): sha(ETH),
            "mvp/diagnostics/base_b5_real_swaps_20260714.tsv": sha(DIAG / "base_b5_real_swaps_20260714.tsv"),
            "mvp/diagnostics/base_b8_future_swaps_20260714.tsv": sha(DIAG / "base_b8_future_swaps_20260714.tsv"),
            "scripts/research/base_reaudit10/reaudit10_analyze.py": sha(ROOT / "scripts/research/base_reaudit10/reaudit10_analyze.py"),
        },
        "review_posture": "candidate facts only; final grade and GO are not made by this script",
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    RPC_LEDGER.write_text(json.dumps(payload["rpc_ledger"], indent=2, sort_keys=True) + "\n")
    write_report(payload)


def fmt(v: float | None) -> str:
    return "NA" if v is None else f"{v:+.4%}"


def write_report(p: dict) -> None:
    lines = [
        "---",
        "sop_applied: true",
        "alpha_id: AR-GL2-A-UNIVERSE-EV",
        "family: F1/F4/F7",
        "next_gate: HUMAN_LINE_REVIEW_ONLY",
        "decision_flags:",
        "  training_allowed: false",
        "  live_allowed: false",
        "  self_go_allowed: false",
        "---",
        "",
        "# GL2-A：431 个同龄候选五证据宇宙 EV",
        "",
        "本报告严格使用冻结预注册；它只给审查 agent 候选事实，不自行给 GL1-12 定级，不作 GO。完整宇宙是同一批 431 个同龄候选，KEYSTONE 是其中冻结的 116 个子集。入场为 +1 块，退出分别为 +1/+5/+15 块；原生 WETH、真实整数持仓、真实卖单优先证明可卖，不能证明是策略持仓的成交则金额仍取同块精确原子报价。",
        "",
        f"预注册 SHA256：`{PREREG_SHA256}`。本次新增 RPC 调用为 0；只重算已固定的 prettiest-blue-aura/链上证据。",
        "",
        "## 逐窗结果",
        "",
        "仍有未决时，下表均值只是已定价点估计，不称为 431 行完整经济 EV；排 rug 版是 survivor-conditioned 对照。",
        "",
        "| 窗口 | 口径 | 人群 | resolved/total | unresolved | 确认 rug | mean | p05 / p25 / p50 / p75 / p95 | win rate |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for w in WINDOWS:
        for policy, label in (("including_confirmed_rugs", "含确认 rug"), ("excluding_confirmed_rugs_survivor_conditioned", "排确认 rug")):
            d = p["summaries"][str(w)][policy]
            for cohort, name in (("full_universe", "全宇宙"), ("keystone_subset", "KEYSTONE")):
                s = d[cohort]
                qs = " / ".join(fmt(s[k]) for k in ("p05", "p25", "p50", "p75", "p95"))
                lines.append(f"| +{w} | {label} | {name} | {s['n_resolved']}/{s['n_total']} | {s['n_unresolved']} | {s['n_confirmed_rug']} | {fmt(s['mean'])} | {qs} | {fmt(s['win_rate'])} |")
    lines += [
        "",
        "## 同窗 alpha / beta 分解",
        "",
        "`beta = 全宇宙已定价均值`；`条件 alpha = KEYSTONE 已定价均值 − 同窗全宇宙已定价均值`。两边有残余未知时仅为部分识别。",
        "",
        "| 窗口 | 口径 | beta | 条件 alpha | KEYSTONE 均值 | beta 占比 | alpha 占比 | 部分识别 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for w in WINDOWS:
        for policy, label in (("including_confirmed_rugs", "含确认 rug"), ("excluding_confirmed_rugs_survivor_conditioned", "排确认 rug")):
            a = p["summaries"][str(w)][policy]["alpha_beta_decomposition"]
            lines.append(f"| +{w} | {label} | {fmt(a['beta_component_full_universe_mean'])} | {fmt(a['selection_alpha_component'])} | {fmt(a['keystone_mean'])} | {fmt(a['beta_signed_share_of_keystone_mean'])} | {fmt(a['selection_alpha_signed_share_of_keystone_mean'])} | {'是' if a['partial_identification'] else '否'} |")
    lines += [
        "",
        "## 五证据裁定与残余",
        "",
        "每行、每窗均写入 E1 机制、E2 前后原子可卖、E3 截止前后续真卖单、E4 恢复、E5 截止完整性。只有五项合取成立才写 `CONFIRMED_NONRECOVERING_RUG=-100%`；恢复或证据不齐的目标窗继续 unresolved。真实池内卖单若不能证明是本策略账户的真实持仓成交，只用于确认可卖，金额仍为精确整数持仓的同块原子报价。",
        "",
        "未决的最坏情形展示为全部按 −100% 的识别下界；没有冻结收益上限，因此上界明确为 unbounded，未用 0、首个 recipient 或最新报价补齐。",
        "",
        "## 审查边界",
        "",
        "本结果不训练、不下单、不签名、不广播、不写 live/signals/Redis，executor 未启动。最终 GL1-12 分级与逐行事实是否足以升级，由审查 agent 决定。",
        "",
    ]
    REPORT.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
