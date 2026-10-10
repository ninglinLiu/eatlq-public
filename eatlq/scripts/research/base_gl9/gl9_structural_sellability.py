#!/usr/bin/env python3
"""GL9 PIT structural-sellability gate stack and edge-cost accounting."""
from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research"))
sys.path.insert(0, str(ROOT / "scripts/research/base_gl9"))
from gl_readonly_rpc import Rpc, compile_atomic_bytecode  # noqa: E402
import structural_sellability_gate as G  # noqa: E402


PREREG = ROOT / "reports/GL9_preregistration_20260716.md"
PREREG_SHA = "4b12ea4dc8e5aee2808b59d5602339465e665b54d7817027727aa01145ea09a5"
ERRATUM = ROOT / "reports/GL9_preregistration_erratum_runtime_selector_20260716.md"
ERRATUM_SHA = "748acbd312ead9c64680d1552dc0c5b7c7cd3b7e2231a0e9397448af6d26cee6"
EMPTY_ERRATUM = ROOT / "reports/GL9_preregistration_erratum_empty_combination_20260716.md"
EMPTY_ERRATUM_SHA = "46f745e7c3f42afd6f120a14b3a831593ef22844241368fcc0620ad385126c85"
LOG_ERRATUM = ROOT / "reports/GL9_preregistration_erratum_log_pagination_20260716.md"
LOG_ERRATUM_SHA = "ef18cad0999d9ec808328c0911175d7083a3887a8f41bcae0ffa4fce86ed4ef4"
INIT_ERRATUM = ROOT / "reports/GL9_preregistration_erratum_initialize_search_20260716.md"
INIT_ERRATUM_SHA = "031346b9a1c6be882b25d9fc77965ace932d36afa4ff2c6a8263e71a6b04de38"
METADATA_ERRATUM = ROOT / "reports/GL9_preregistration_erratum_bytecode_metadata_20260716.md"
METADATA_ERRATUM_SHA = "6932790455dc036304e1dc3545448893c02a093248170f7235da7cf70faac38a"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
GL8 = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
LABELS = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"
EXTERNAL = ROOT / "scripts/research/base_lefttail/_external_risk_current.json"
OUT = ROOT / "mvp/diagnostics/base_gl9_structural_sellability_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl9_structural_sellability_per_leg_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl9_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl9_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl9_rpc_ledger_20260716.json"

PESS = -1.0229943556396193
OFFSETS = (1, 5, 15)
LAYERS = ("L1", "L2", "L3", "L4")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qty_for(v3row: dict, v2row: dict) -> tuple[int, str]:
    q = (((v3row["arm1"]["1"].get("archive") or {}).get("exact_size_quote") or {})
         .get("amount_in_token_raw"))
    if q and int(q) > 0:
        return int(q), "GL5V3_EXACT_QUOTE"
    q = (v2row["arm1"]["1"] or {}).get("position_qty_raw")
    if q and int(q) > 0:
        return int(q), "GL5V2_FROZEN_POSITION"
    return 0, "UNRESOLVED"


def gate_one(rpc: Rpc, atomic_bytecode: str, helper_runtime: str,
             item: G.DecisionInput, signal_time: str, external: dict | None) -> dict:
    """Compute all decision layers before outcome labels are available."""
    l1 = G.converge_historical_qty(rpc, atomic_bytecode, item)
    l2 = G.evaluate_l2(rpc, helper_runtime, item)
    l3 = G.evaluate_l3(rpc, item)
    l4 = G.evaluate_l4(rpc, atomic_bytecode, item)
    l5 = G.evaluate_external_snapshot(external, signal_time)
    assert all(int(x["evidence_block"]) <= item.entry_block for x in (l1, l2, l3, l4))
    return {"pool": item.pool, "token": item.token, "entry_block": item.entry_block,
            "signal_time": signal_time, "fee": item.fee,
            "historical_qty_raw": str(item.historical_qty_raw), "qty_source": item.qty_source,
            "layers": {"L1": l1, "L2": l2, "L3": l3, "L4": l4, "L5": l5}}


def combos() -> list[dict]:
    rows = []
    for n in range(5):
        for subset in itertools.combinations(LAYERS, n):
            ident = "L0" if not subset else "L0+" + "+".join(subset)
            rows.append({"id": ident, "layers": list(subset)})
    return rows


def bootstrap(lower: list[float], combo_index: int, off: int) -> dict:
    if not lower:
        return {"paths": 0, "terminal_p5": None, "mdd_p95": None}
    rng = np.random.default_rng(20260716 + combo_index * 100 + off)
    arr = np.asarray(lower, dtype=float)
    sample = rng.choice(arr, size=(10_000, len(arr)), replace=True)
    eq = 100.0 + np.cumsum(sample, axis=1)
    peaks = np.maximum.accumulate(np.column_stack([np.full(10_000, 100.0), eq]), axis=1)[:, 1:]
    dd = np.clip((peaks - eq) / np.maximum(peaks, 1e-12), 0, 1)
    return {"paths": 10_000, "terminal_p5": float(np.quantile(eq[:, -1], .05)),
            "mdd_p95": float(np.quantile(dd.max(axis=1), .95))}


def metrics(selected: list[dict], all_n: int, rug_set: set[str], winners: set[str],
            base_winner_n: int, combo_index: int, off: int) -> dict:
    cells = [r["economics"][str(off)] for r in selected]
    lower = [float(x["ret"]) if x["resolved"] else PESS for x in cells]
    upper = [float(x["ret"]) if x["resolved"] else float(x.get("optimistic_qv2_ret") or 0.0) for x in cells]
    resolved = sum(x["resolved"] for x in cells)
    rugs = sum(r["pool"] in rug_set for r in selected)
    retained = sum(r["pool"] in winners for r in selected)
    pos = np.maximum(np.asarray(lower), 0) if lower else np.asarray([])
    top1_share = float(pos.max() / pos.sum()) if pos.size and pos.sum() > 0 else 0.0
    if len(lower) > 1:
        temp = list(lower)
        temp.pop(int(np.argmax(temp)))
        ex_top1 = float(np.mean(temp))
    else:
        ex_top1 = None
    return {
        "passed_n": len(selected), "rejected_n": all_n - len(selected),
        "resolved_n": resolved, "clean_resolved_n": resolved, "unresolved_n": len(cells) - resolved,
        "rug_admitted_n": rugs, "rug_admitted_rate": rugs / len(selected) if selected else None,
        "confirmed_collapse_at_offset_n": sum("CONFIRMED_COLLAPSE" in x["branch"] for x in cells),
        "actual_real_sell_n": sum(x["branch"].startswith("REAL_SELL") for x in cells),
        "pooled_mean_interval": [float(np.mean(lower)) if lower else None,
                                 float(np.mean(upper)) if upper else None],
        "median_interval": [float(np.median(lower)) if lower else None,
                            float(np.median(upper)) if upper else None],
        "win_rate_interval": [float(np.mean(np.asarray(lower) > 0)) if lower else None,
                              float(np.mean(np.asarray(upper) > 0)) if upper else None],
        "ex_top1_lower_mean": ex_top1, "top1_positive_contribution": top1_share,
        "winners_retained_n": retained,
        "winners_retained_rate": retained / base_winner_n if base_winner_n else None,
        "branches": dict(sorted(Counter(x["branch"] for x in cells).items())),
        "bootstrap_lower": bootstrap(lower, combo_index, off),
        "lower_vector_sha256": hashlib.sha256(json.dumps(lower, separators=(",", ":")).encode()).hexdigest(),
    }


def dominated(a: dict, b: dict) -> bool:
    """Return True when candidate a is dominated by candidate b."""
    av, bv = a["metrics"]["1"], b["metrics"]["1"]
    weak = (
        bv["rug_admitted_rate"] <= av["rug_admitted_rate"]
        and bv["pooled_mean_interval"][0] >= av["pooled_mean_interval"][0]
        and bv["winners_retained_rate"] >= av["winners_retained_rate"]
        and bv["passed_n"] >= av["passed_n"]
    )
    strict = (
        bv["rug_admitted_rate"] < av["rug_admitted_rate"]
        or bv["pooled_mean_interval"][0] > av["pooled_mean_interval"][0]
        or bv["winners_retained_rate"] > av["winners_retained_rate"]
        or bv["passed_n"] > av["passed_n"]
    )
    return weak and strict


def main() -> None:
    assert sha(PREREG) == PREREG_SHA
    assert sha(ERRATUM) == ERRATUM_SHA
    assert sha(EMPTY_ERRATUM) == EMPTY_ERRATUM_SHA
    assert sha(LOG_ERRATUM) == LOG_ERRATUM_SHA
    assert sha(INIT_ERRATUM) == INIT_ERRATUM_SHA
    assert sha(METADATA_ERRATUM) == METADATA_ERRATUM_SHA
    # Assert gate APIs cannot receive forbidden outcome fields.
    forbidden = {"label", "rug", "flip", "ret", "branch", "outcome"}
    for fn in (G.converge_historical_qty, G.evaluate_l2, G.evaluate_l3, G.evaluate_l4):
        assert forbidden.isdisjoint(inspect.signature(fn).parameters)

    v3 = json.loads(V3.read_text())
    v2 = json.loads(V2.read_text())
    gl8 = json.loads(GL8.read_text())
    assert len(v3["rows"]) == len(v2["rows"]) == len(gl8["rows"]) == 644
    v3by = {r["pool"].lower(): r for r in v3["rows"]}
    v2by = {r["pool"].lower(): r for r in v2["rows"]}
    gl8by = {r["pool"].lower(): r for r in gl8["rows"]}
    assert set(v3by) == set(v2by) == set(gl8by)
    l0_pools = sorted(p for p, r in gl8by.items() if r["common_gate_pass"])

    # Decision-only extraction occurs before outcome/return labels are joined.
    external_raw = json.loads(EXTERNAL.read_text()) if EXTERNAL.exists() else {}
    decision_inputs = []
    for pool in l0_pools:
        r3, r2 = v3by[pool], v2by[pool]
        qty, source = qty_for(r3, r2)
        decision_inputs.append((
            G.DecisionInput(pool=pool, token=r3["token"].lower(), entry_block=int(r3["entry_block"]),
                            fee=int((r3.get("fee_evidence") or {}).get("fee") or 0),
                            historical_qty_raw=qty, qty_source=source),
            r3["signal_time"], external_raw.get(r3["token"].lower()),
        ))

    rpc = Rpc(LEDGER, attempt_cap=60000, user_agent="EATLQ-GL9-readonly/1")
    atomic_bytecode = compile_atomic_bytecode()
    helper_runtime = G.compile_code_reader_runtime()
    gate_rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(gate_one, rpc, atomic_bytecode, helper_runtime, item, signal, ext)
                   for item, signal, ext in decision_inputs]
        for future in as_completed(futures):
            gate_rows.append(future.result())
    gate_rows.sort(key=lambda x: x["pool"])
    assert {r["pool"] for r in gate_rows} == set(l0_pools)

    # Outcome join starts here, after every decision layer has been frozen per pool.
    rug_set = {r["pool"].lower() for r in json.loads(LABELS.read_text())["rug_pools"]}
    enriched = []
    for gate in gate_rows:
        source = gl8by[gate["pool"]]
        enriched.append({**gate, "economics": source["economics"],
                         "evaluation_only": {"rug_outcome": gate["pool"] in rug_set}})
    winners = {r["pool"] for r in enriched
               if r["economics"]["1"]["resolved"] and float(r["economics"]["1"]["ret"]) >= 2.0}

    definitions = combos()
    combo_rows = []
    for index, definition in enumerate(definitions):
        selected = [r for r in enriched if all(r["layers"][layer]["pass"] for layer in definition["layers"])]
        combo_rows.append({"id": definition["id"], "layers": definition["layers"], "index": index,
                           "selected_pool_sha256": hashlib.sha256(json.dumps(sorted(r["pool"] for r in selected), separators=(",", ":")).encode()).hexdigest(),
                           "metrics": {str(off): metrics(selected, len(enriched), rug_set, winners,
                                                               len(winners), index, off) for off in OFFSETS}})
    assert len(combo_rows) == 16
    base = next(x for x in combo_rows if x["id"] == "L0")["metrics"]["1"]

    powered = [x for x in combo_rows if x["id"] != "L0" and x["metrics"]["1"]["clean_resolved_n"] >= 30]
    frontier = [x for x in powered if not any(dominated(x, y) for y in powered if y is not x)]

    for row in combo_rows:
        m = row["metrics"]["1"]
        if row["id"] == "L0":
            row["primary_gates"] = {"powered": m["clean_resolved_n"] >= 30, "eligible": False}
            continue
        powered_pass = m["clean_resolved_n"] >= 30
        if not powered_pass or m["passed_n"] == 0:
            rug_pass = False
        elif base["rug_admitted_n"] == 0:
            rug_pass = m["rug_admitted_n"] == 0
        else:
            rug_pass = (m["rug_admitted_n"] * 2 <= base["rug_admitted_n"]
                        and m["rug_admitted_rate"] <= .5 * base["rug_admitted_rate"])
        winner_pass = base["winners_retained_n"] > 0 and m["winners_retained_rate"] >= .5
        edge_pass = (m["pooled_mean_interval"][0] is not None and m["pooled_mean_interval"][0] > 0
                     and m["ex_top1_lower_mean"] is not None and m["ex_top1_lower_mean"] > 0)
        bt = m["bootstrap_lower"]
        right_tail_pass = bt["terminal_p5"] is not None and bt["terminal_p5"] > 100 and bt["mdd_p95"] <= .25
        row["primary_gates"] = {"powered": powered_pass, "rug_reduction": rug_pass,
                                "winner_retention": winner_pass, "edge": edge_pass,
                                "right_tail": right_tail_pass,
                                "eligible": powered_pass and rug_pass and winner_pass and edge_pass and right_tail_pass}

    for row in frontier:
        m = row["metrics"]["1"]
        rug_score = 0.0 if not base["rug_admitted_rate"] else float(np.clip(1 - m["rug_admitted_rate"] / base["rug_admitted_rate"], 0, 1))
        winner_score = 0.0 if not base["winners_retained_n"] else float(np.clip(m["winners_retained_rate"], 0, 1))
        edge_score = float(np.clip((m["pooled_mean_interval"][0] - PESS) /
                                   (base["pooled_mean_interval"][0] - PESS), 0, 1))
        coverage_score = m["passed_n"] / base["passed_n"]
        row["sweet_score"] = .40 * rug_score + .30 * winner_score + .20 * edge_score + .10 * coverage_score
    sweet = sorted(frontier, key=lambda x: (-x["sweet_score"], -x["metrics"]["1"]["pooled_mean_interval"][0],
                                             -x["metrics"]["1"]["passed_n"], x["id"]))[0] if frontier else None

    if not powered:
        verdict = "INSUFFICIENT_N"
    elif any(x["primary_gates"]["eligible"] for x in combo_rows if x["id"] != "L0"):
        verdict = "CLEAN_EDGE_SURVIVES"
    else:
        verdict = "CLEAN_FILTER_KILLS_EDGE"

    golden_options = sorted((hashlib.sha256(f"GL9-golden|{p}".encode()).hexdigest(), p) for p in l0_pools)[:5]
    gate_by = {r["pool"]: r for r in enriched}
    golden = []
    for rank, pool in golden_options:
        r = gate_by[pool]
        golden.append({"rank_sha256": rank, "pool": pool, "token": r["token"],
                       "entry_block": r["entry_block"], "layers": r["layers"]})
    GOLDEN.write_text(json.dumps({"schema_version": 1, "task_id": "GL9-golden",
                                  "selection_rule": "sha256(GL9-golden|pool)", "expected_n": 5,
                                  "actual_n": len(golden), "rows": golden,
                                  "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    rpc_summary = rpc.summary()
    LEDGER_SUM.write_text(json.dumps(rpc_summary, indent=2, sort_keys=True) + "\n")
    PER.write_text(json.dumps({"schema_version": 1, "task_id": "GL9-per-leg",
                               "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
                               "empty_combination_erratum_sha256": EMPTY_ERRATUM_SHA,
                               "log_pagination_erratum_sha256": LOG_ERRATUM_SHA,
                               "initialize_search_erratum_sha256": INIT_ERRATUM_SHA,
                               "bytecode_metadata_erratum_sha256": METADATA_ERRATUM_SHA,
                               "decision_then_outcome_join": True, "rows": enriched,
                               "go_decision": "NOT_MADE"}, indent=2, sort_keys=True) + "\n")

    layer_summary = {}
    for layer in LAYERS:
        row = next(x for x in combo_rows if x["id"] == f"L0+{layer}")
        layer_summary[layer] = row["metrics"]
    cumulative_ids = ["L0", "L0+L1", "L0+L1+L2", "L0+L1+L2+L3", "L0+L1+L2+L3+L4"]
    result = {
        "schema_version": 1, "task_id": "GL9", "research_only": True,
        "preregistration_sha256": PREREG_SHA, "erratum_sha256": ERRATUM_SHA,
        "empty_combination_erratum_sha256": EMPTY_ERRATUM_SHA,
        "log_pagination_erratum_sha256": LOG_ERRATUM_SHA,
        "initialize_search_erratum_sha256": INIT_ERRATUM_SHA,
        "bytecode_metadata_erratum_sha256": METADATA_ERRATUM_SHA,
        "step0": {"labeled_n": len(v3by), "L0_n": len(l0_pools), "combination_n": len(combo_rows),
                  "outcome_join_after_all_gate_rows": True, "model_fit_executed": False},
        "constants": {"SPEND": str(G.SPEND), "SPEND_CAP": str(G.SPEND_CAP),
                      "RECOVERY_MIN": G.RECOVERY_MIN, "K_RESERVE": G.K_RESERVE,
                      "K_OPEN": G.K_OPEN, "PESS": PESS, "WINNER_R": 2.0},
        "layer_pass_counts": {layer: sum(r["layers"][layer]["pass"] for r in enriched) for layer in LAYERS},
        "layer_status_counts": {layer: dict(sorted(Counter(r["layers"][layer]["status"] for r in enriched).items())) for layer in LAYERS},
        "external_overlay": {"eligible_n": sum(r["layers"]["L5"]["eligible"] for r in enriched),
                             "unavailable_n": sum(not r["layers"]["L5"]["eligible"] for r in enriched),
                             "critical_n": sum(bool(r["layers"]["L5"]["critical"]) for r in enriched)},
        "evaluation_label_counts": {"L0_rug_outcome_n": base["rug_admitted_n"], "L0_winner_n": len(winners)},
        "combinations": combo_rows, "standalone_layers": layer_summary,
        "cumulative_path": {ident: next(x for x in combo_rows if x["id"] == ident)["metrics"] for ident in cumulative_ids},
        "pareto_frontier": [{"id": x["id"], "sweet_score": x.get("sweet_score"),
                             "primary": x["metrics"]["1"], "primary_gates": x["primary_gates"]} for x in frontier],
        "sweet_spot": ({"id": sweet["id"], "sweet_score": sweet["sweet_score"],
                        "primary": sweet["metrics"]["1"], "primary_gates": sweet["primary_gates"]} if sweet else None),
        "powered_non_L0_n": len(powered),
        "eligible_combination_ids": [x["id"] for x in combo_rows if x.get("primary_gates", {}).get("eligible")],
        "verdict_candidate": verdict,
        "golden": {"path": str(GOLDEN.relative_to(ROOT)), "n": len(golden)},
        "rpc": rpc_summary,
        "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in (PREREG, ERRATUM, EMPTY_ERRATUM, LOG_ERRATUM, INIT_ERRATUM, METADATA_ERRATUM, V3, V2, GL8, LABELS, EXTERNAL)},
        "wallet_level_boundary": "generic entry-state atomic simulation does not prove exact strategy-wallet real execution",
        "go_decision": "NOT_MADE",
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"step0": result["step0"], "layer_pass_counts": result["layer_pass_counts"],
                      "external_overlay": result["external_overlay"], "labels": result["evaluation_label_counts"],
                      "pareto": [x["id"] for x in frontier], "sweet": result["sweet_spot"],
                      "eligible": result["eligible_combination_ids"], "verdict": verdict,
                      "rpc": rpc_summary}, indent=2))


if __name__ == "__main__":
    main()
