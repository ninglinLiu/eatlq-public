#!/usr/bin/env python3
"""GL5 Step-0-only forensic anchor.

This program is deliberately offline.  It re-aggregates frozen rows, checks the
source identity of the 431/116 selector, and enforces the preregistered stop
gate.  It has no RPC client and no live/executor imports.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL5_preregistration_20260716.md"
PREREG_SHA = "909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9"
OUT = DIAG / "base_gl5_step0_anchor_20260716.json"
PER_LEG = DIAG / "base_gl5_per_leg_20260716.json"

CORE = DIAG / "base_reaudit10_seconds_rpc_20260715.json"
PER = DIAG / "base_reaudit10_per_trade_20260715.json"
FINAL = DIAG / "base_reaudit10_final_20260715.json"
GL3 = DIAG / "base_gl3_gl12_alpha_beta_20260715.json"
GL1 = DIAG / "base_gl1_golden_truth_ledger_gl4_20260716.json"
B8 = ROOT / "scripts/research/base_b8/b8_rpc_evidence.py"
B5 = ROOT / "scripts/research/base_b5/b5_offline_reverify.py"
LEGACY = ROOT / "scripts/research/base_lefttail/keystone_live.py"
PRICED = ROOT / "scripts/research/base_lefttail/_priced_live.json"
REAUDIT_PREREG = ROOT / "configs/research/base_reaudit10_seconds_preregistration_20260715.json"
LIVE_F = ROOT / "mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json"
LIVE_F_REPORT = ROOT / "mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest_cn.md"
AGGRESSIVE = ROOT / "mvp/audits/aggressive_live_rebacktest_20260625/aggressive_live_rebacktest.json"
AGGRESSIVE_REPORT = ROOT / "mvp/audits/aggressive_live_rebacktest_20260625/aggressive_live_rebacktest_cn.md"

OFFSETS = (1, 3, 5, 10, 15)
EXPECTED = {
    1: {"n": 113, "unresolved": 3, "mean": 0.5663701436516096,
        "median": 0.2597229826746521, "top1_removed_mean": 0.48288116031956935,
        "evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 107, "CONFIRMED_LP_RUG": 2,
                     "REAL_SELL": 4, "UNRESOLVED_EXIT": 3}},
    3: {"n": 113, "unresolved": 3, "mean": 0.5716764163421629,
        "median": 0.2597229826746521, "top1_removed_mean": 0.4882830415270697,
        "evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 100, "CONFIRMED_LP_RUG": 2,
                     "REAL_SELL": 11, "UNRESOLVED_EXIT": 3}},
    5: {"n": 112, "unresolved": 4, "mean": 0.5658600160864686,
        "median": 0.2586536605039931, "top1_removed_mean": 0.48133795277529695,
        "evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 101, "CONFIRMED_LP_RUG": 3,
                     "REAL_SELL": 8, "UNRESOLVED_EXIT": 4}},
    10: {"n": 111, "unresolved": 5, "mean": 0.5817845065592507,
         "median": 0.28751008126389255, "top1_removed_mean": 0.4967792039603212,
         "evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 100, "CONFIRMED_LP_RUG": 3,
                      "REAL_SELL": 8, "UNRESOLVED_EXIT": 5}},
    15: {"n": 111, "unresolved": 5, "mean": 0.5768599229957799,
         "median": 0.2731717488361512, "top1_removed_mean": 0.49397325802362163,
         "evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 101, "CONFIRMED_LP_RUG": 3,
                      "REAL_SELL": 7, "UNRESOLVED_EXIT": 5}},
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def function_source(path: Path, name: str) -> str:
    text = path.read_text()
    tree = ast.parse(text)
    node = next(x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    return ast.get_source_segment(text, node) or ""


def summary(values: list[float]) -> dict:
    a = np.asarray(values, dtype=float)
    order = np.argsort(a)[::-1]
    k = max(1, math.ceil(0.01 * len(a)))
    rem = np.delete(a, order[:k])
    return {
        "n": int(len(a)),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "top1_removed_mean": float(rem.mean()),
    }


def exact_or_tolerance(actual: float, expected: float) -> dict:
    return {
        "actual": actual,
        "expected": expected,
        "python_equal": actual == expected,
        "absolute_error": abs(actual - expected),
        "within_preregistered_tolerance": abs(actual - expected) <= 1e-15,
    }


def main() -> None:
    assert sha(PREREG) == PREREG_SHA
    for path in (CORE, PER, FINAL, GL3, GL1, B8, B5, LEGACY, PRICED,
                 REAUDIT_PREREG, LIVE_F, LIVE_F_REPORT, AGGRESSIVE, AGGRESSIVE_REPORT):
        assert path.exists(), path

    core = json.loads(CORE.read_text())
    per = json.loads(PER.read_text())
    final = json.loads(FINAL.read_text())
    gl3 = json.loads(GL3.read_text())
    gl1 = json.loads(GL1.read_text())
    assert len(core["rows"]) == 431
    assert sum(bool(x["selected"]) for x in core["rows"]) == 116
    assert len(per["rows"]) == 116

    final_by_offset = {int(x["exit_offset_blocks"]): x for x in final["exit_latency_budget_frontier"]}
    reproduced = {}
    all_exact = True
    for offset in OFFSETS:
        key = f"1:{offset}"
        cells = [row["matrix"][key] for row in per["rows"]]
        vals = [cell["ret"] for cell in cells if cell["resolved"]]
        got = summary(vals)
        got["unresolved"] = sum(not cell["resolved"] for cell in cells)
        got["evidence"] = dict(sorted(Counter(cell["evidence"] for cell in cells).items()))
        exp = EXPECTED[offset]
        numeric = {k: exact_or_tolerance(got[k], exp[k]) for k in ("mean", "median", "top1_removed_mean")}
        exact = (got["n"] == exp["n"] and got["unresolved"] == exp["unresolved"]
                 and got["evidence"] == exp["evidence"]
                 and all(x["python_equal"] for x in numeric.values()))
        final_exact = (
            got["n"] == final_by_offset[offset]["pooled"]["n"]
            and got["unresolved"] == final_by_offset[offset]["unresolved"]
            and got["evidence"] == final_by_offset[offset]["evidence"]
            and got["mean"] == final_by_offset[offset]["pooled"]["mean"]
            and got["median"] == final_by_offset[offset]["pooled"]["median"]
            and got["top1_removed_mean"] == final_by_offset[offset]["pooled"]["top1_removed_mean"]
        )
        all_exact &= exact and final_exact
        reproduced[str(offset)] = {
            **got,
            "numeric_assertions": numeric,
            "hardcoded_anchor_exact": exact,
            "reaudit10_final_exact": final_exact,
        }

    selector_source = function_source(B8, "prepare_rows")
    transitive_source = "\n".join(function_source(B5, name) for name in
                                  ("load_base", "features", "regime", "fit_forward"))
    f_identifiers = ("blacklist", "deployer", "skeleton", "safe_rug", "cluster_blacklist")
    selector_hits = {term: term in selector_source.lower() for term in f_identifiers}
    transitive_hits = {term: term in transitive_source.lower() for term in f_identifiers}
    explicit_f_filter = any(selector_hits.values()) or any(transitive_hits.values())
    # The exact selected expression is also pinned separately so a prose mention
    # of a blacklist could never satisfy the identity gate.
    selected_line = next(line.strip() for line in selector_source.splitlines() if 'df["selected"]' in line)
    selector_expression_has_f = any(term in selected_line.lower() for term in f_identifiers)
    f_identity_pass = explicit_f_filter and selector_expression_has_f

    live_f = json.loads(LIVE_F.read_text())
    aggressive = json.loads(AGGRESSIVE.read_text())
    # These are external contrast objects, never inputs to the 431/116 replay.
    separate_f_objects = {
        "conservative_644_pool_rebacktest": {
            "same_estimand": False,
            "source_sha256": sha(LIVE_F),
            "report_sha256": sha(LIVE_F_REPORT),
            "universe_n": 644,
            "deployer_blacklist_n": live_f.get("meta", {}).get("config", {}).get("deployer_blacklist_n"),
            "skeleton_blacklist_n": live_f.get("meta", {}).get("config", {}).get("skeleton_blacklist_n"),
            "selected_after_F_n": 120,
            "reported_version_note": "2026-06-24 artifact reports 247 deployer including two PARADAENGI plus 33 skeleton; its code header says 245+2 and is not in the B8 selector path",
        },
        "aggressive_644_pool_rebacktest": {
            "same_estimand": False,
            "source_sha256": sha(AGGRESSIVE),
            "report_sha256": sha(AGGRESSIVE_REPORT),
            "F_lp_pull_blacklist": aggressive.get("meta", {}).get("config", {}).get("F_lp_pull_blacklist"),
            "selected_no_F_n": aggressive.get("gate_breakdown", {}).get("aggressive_live_no_F"),
            "reported_rug_entries": 207,
        },
    }

    claims = {x["id"]: x for x in gl1["claims"]}
    gl101 = claims["GL1-01"]
    gl112 = claims["GL1-12"]
    gl101_exact = all(
        gl101["answer"]["frontier"][f"1:{o}"]["mean"] == EXPECTED[o]["mean"]
        for o in (1, 5, 15)
    )
    gl112_exact = (
        gl112["answer"]["keystone_interval"] == {
            "1": gl3["summaries"]["1"]["alpha_beta"]["keystone_interval"],
            "5": gl3["summaries"]["5"]["alpha_beta"]["keystone_interval"],
            "15": gl3["summaries"]["15"]["alpha_beta"]["keystone_interval"],
        }
        and gl112["answer"]["full_universe_beta_interval"] == {
            "1": gl3["summaries"]["1"]["alpha_beta"]["beta_full_universe_interval"],
            "5": gl3["summaries"]["5"]["alpha_beta"]["beta_full_universe_interval"],
            "15": gl3["summaries"]["15"]["alpha_beta"]["beta_full_universe_interval"],
        }
        and gl112["answer"]["selection_alpha_interval"] == {
            "1": gl3["summaries"]["1"]["alpha_beta"]["selection_alpha_interval"],
            "5": gl3["summaries"]["5"]["alpha_beta"]["selection_alpha_interval"],
            "15": gl3["summaries"]["15"]["alpha_beta"]["selection_alpha_interval"],
        }
    )

    arm0_pass = bool(all_exact and gl101_exact and gl112_exact)
    if not arm0_pass:
        stop_reason = "STOP_ARM0_REPRODUCTION_DRIFT"
    elif not f_identity_pass:
        stop_reason = "STOP_OBJECT_IDENTITY_MISMATCH"
    else:
        stop_reason = None
    stop = stop_reason is not None

    source_hashes = {str(p.relative_to(ROOT)): sha(p) for p in
                     (PREREG, CORE, PER, FINAL, GL3, GL1, B8, B5, LEGACY, PRICED,
                      REAUDIT_PREREG, LIVE_F, LIVE_F_REPORT, AGGRESSIVE, AGGRESSIVE_REPORT)}
    result = {
        "schema_version": 1,
        "task_id": "GL5",
        "research_only": True,
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False,
                           "broadcast_allowed": False},
        "preregistration_sha256": PREREG_SHA,
        "step0": {
            "status": "STOPPED" if stop else "PASS",
            "stop_gate_triggered": stop,
            "stop_reason": stop_reason,
            "arm0_reproduction_pass": arm0_pass,
            "object_identity_pass": f_identity_pass,
        },
        "pinned_estimand": {
            "population": {"same_age_universe_n": 431, "selected_n": 116,
                           "regimes": {
                               "W1": ["2026-02-27T05:41:15Z", "2026-04-01T00:00:00Z"],
                               "W2": ["2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z"],
                               "W3": ["2026-05-01T00:00:00Z", "2026-06-08T18:29:40Z"],
                           }},
            "selection_expression": selected_line,
            "selection_model": {
                "class": "HistGradientBoostingClassifier",
                "max_iter": 300, "max_depth": 4, "learning_rate": 0.05,
                "l2_regularization": 1.0, "min_samples_leaf": 20, "random_state": 0,
                "feature_set": "all features returned by B5 features()",
                "p_cut": 0.2808669872465047,
                "friction_gate": -0.05,
            },
            "position": {"notional_weth_raw": "1000000000000000", "notional_weth": 0.001,
                         "gas_usd_per_attempted_leg": 0.075},
            "legacy_trigger": {"primary": "legacy_exb", "fallback": "entry_block+7200"},
            "legacy_exit": {"hard_stop": -0.5, "trail_drawdown": 0.35,
                            "maximum_hold_blocks": 7200, "take_profit_multiple": None,
                            "tp_ladder": None,
                            "note": "keystone_live.exit_block has no take-profit branch; the frozen object has neither a single TP nor a TP ladder"},
            "execution_offsets_blocks": {"entry": [1, 3, 5, 10, 15],
                                         "exit": [1, 3, 5, 10, 15],
                                         "gl1_gl3_primary_exit": [1, 5, 15]},
            "old_exit_priority": ["REAL_SELL", "ATOMIC_GUARDED_EXACT_QUOTE",
                                  "TEMPORARY_RECOVERY", "CONFIRMED_NONRECOVERING_RUG=-1",
                                  "UNRESOLVED"],
            "F_filter": None,
        },
        "f_identity": {
            "required_by_task_question": True,
            "pass": f_identity_pass,
            "selected_expression_has_explicit_f": selector_expression_has_f,
            "selector_scope_identifier_hits": selector_hits,
            "transitive_selection_scope_identifier_hits": transitive_hits,
            "blacklist_version": None,
            "blacklist_sha256": None,
            "finding": "The 431/116 +56% source estimand has no explicit F filter. The historical F-on audits are separate 644-pool estimands and cannot be grafted without changing config.",
            "separate_external_contrast_objects": separate_f_objects,
        },
        "arm0_reproduction": {
            "status": "PASS" if arm0_pass else "FAIL",
            "aggregation_source": str(PER.relative_to(ROOT)),
            "frontier": reproduced,
            "gl1_taxonomy": {
                "GL1-01_grade": gl101["grade"],
                "GL1-01_plus56_anchor_exact": gl101_exact,
                "GL1-12_grade": gl112["grade"],
                "GL1-12_interval_anchor_exact": gl112_exact,
                "correction": "+56% point estimate is GL1-01 PINNED; GL1-12 IRON is the bounded 431/116 sign-separation result.",
            },
        },
        "post_step0": {
            "arm1": "NOT_RUN_STOP_GATE" if stop else "ELIGIBLE_NOT_YET_RUN",
            "same_age_baseline_repricing": "NOT_RUN_STOP_GATE" if stop else "ELIGIBLE_NOT_YET_RUN",
            "decomposition": "NOT_RUN_STOP_GATE" if stop else "ELIGIBLE_NOT_YET_RUN",
            "golden_rpc": "NOT_RUN_STOP_GATE" if stop else "ELIGIBLE_NOT_YET_RUN",
            "new_rpc_calls": 0,
            "rpc_url_accessed": False,
        },
        "conclusion_candidate": (
            "INCONCLUSIVE_OBJECT_NOT_DEFINED: the requested F-on +56% estimand does not exist in the frozen source chain; neither edge-survives nor edge-collapses may be adjudicated without defining a new estimand."
            if stop_reason == "STOP_OBJECT_IDENTITY_MISMATCH" else
            "STOPPED_ARM0_DRIFT" if stop_reason else "STEP0_PASS"
        ),
        "source_hashes": source_hashes,
    }

    per_leg_rows = []
    core_by_pool = {r["pool"]: r for r in core["rows"]}
    for row in per["rows"]:
        c = core_by_pool[row["pool"]]
        per_leg_rows.append({
            "pool": row["pool"], "token": row["token"], "regime": row["regime"],
            "signal_time": row["signal_time"], "entry_block": row["entry_block"],
            "trigger_block": row["trigger_block"], "selected": row["selected"],
            "F_status": "NOT_DEFINED_IN_SOURCE_ESTIMAND",
            "arm0": {f"1:{o}": row["matrix"][f"1:{o}"] for o in OFFSETS},
            "arm1": "NOT_RUN_STOP_GATE",
            "same_age_baseline_repricing": "NOT_RUN_STOP_GATE",
            "source_core_selected": c["selected"],
        })
    assert len(per_leg_rows) == 116
    assert all(x["selected"] and x["source_core_selected"] for x in per_leg_rows)

    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    PER_LEG.write_text(json.dumps({
        "schema_version": 1, "task_id": "GL5", "research_only": True,
        "go_decision": "NOT_MADE", "preregistration_sha256": PREREG_SHA,
        "step0_status": result["step0"]["status"], "stop_reason": stop_reason,
        "rows": per_leg_rows,
    }, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "out": str(OUT), "per_leg": str(PER_LEG), "arm0_pass": arm0_pass,
        "f_identity_pass": f_identity_pass, "stop_reason": stop_reason,
        "new_rpc_calls": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
