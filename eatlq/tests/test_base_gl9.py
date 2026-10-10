"""Offline exact-equality regression tests for GL9."""
from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
sys.path[:0] = [str(ROOT / "scripts/research"), str(ROOT / "scripts/research/base_gl9")]
import structural_sellability_gate as G  # noqa: E402
import gl9_structural_sellability as R  # noqa: E402

RESULT = ROOT / "mvp/diagnostics/base_gl9_structural_sellability_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl9_structural_sellability_per_leg_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl9_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl9_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl9_rpc_ledger_20260716.json"
GL8 = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
LABELS = ROOT / "scripts/research/base_lefttail/_labeled_universe.json"

FROZEN = {
    "reports/GL9_preregistration_20260716.md": "4b12ea4dc8e5aee2808b59d5602339465e665b54d7817027727aa01145ea09a5",
    "reports/GL9_preregistration_erratum_runtime_selector_20260716.md": "748acbd312ead9c64680d1552dc0c5b7c7cd3b7e2231a0e9397448af6d26cee6",
    "reports/GL9_preregistration_erratum_empty_combination_20260716.md": "46f745e7c3f42afd6f120a14b3a831593ef22844241368fcc0620ad385126c85",
    "reports/GL9_preregistration_erratum_log_pagination_20260716.md": "ef18cad0999d9ec808328c0911175d7083a3887a8f41bcae0ffa4fce86ed4ef4",
    "reports/GL9_preregistration_erratum_initialize_search_20260716.md": "031346b9a1c6be882b25d9fc77965ace932d36afa4ff2c6a8263e71a6b04de38",
    "reports/GL9_preregistration_erratum_bytecode_metadata_20260716.md": "6932790455dc036304e1dc3545448893c02a093248170f7235da7cf70faac38a",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_chain_and_step0_are_exact() -> None:
    result = load(RESULT)
    for rel, expected in FROZEN.items():
        assert sha(ROOT / rel) == expected
    assert result["step0"] == {
        "labeled_n": 644, "L0_n": 339, "combination_n": 16,
        "outcome_join_after_all_gate_rows": True, "model_fit_executed": False,
    }
    assert result["preregistration_sha256"] == FROZEN["reports/GL9_preregistration_20260716.md"]
    assert result["bytecode_metadata_erratum_sha256"] == FROZEN["reports/GL9_preregistration_erratum_bytecode_metadata_20260716.md"]


def test_l0_anchor_and_gate_apis_have_no_outcome_inputs() -> None:
    per, gl8, v3 = load(PER), load(GL8), load(V3)
    expected = {r["pool"].lower() for r in gl8["rows"] if r["common_gate_pass"]}
    got = {r["pool"] for r in per["rows"]}
    assert got == expected
    assert len(got) == 339
    assert {r["pool"].lower() for r in v3["rows"]} == {r["pool"].lower() for r in gl8["rows"]}
    forbidden = {"label", "rug", "flip", "ret", "branch", "outcome"}
    for fn in (G.converge_historical_qty, G.evaluate_l2, G.evaluate_l3, G.evaluate_l4):
        assert forbidden.isdisjoint(inspect.signature(fn).parameters)
    assert per["decision_then_outcome_join"] is True


def test_every_decision_evidence_is_at_or_before_entry() -> None:
    per = load(PER)
    for row in per["rows"]:
        entry = int(row["entry_block"])
        for layer in ("L1", "L2", "L3", "L4"):
            assert int(row["layers"][layer]["evidence_block"]) <= entry
        reserve = row["layers"]["L3"]["reserve"]
        assert reserve["lookback_block"] == entry - 15
        assert reserve["entry_block"] == entry
        for page in row["layers"]["L3"]["lp"]["initialize_evidence"]["pages"]:
            assert page["to_block"] <= entry
            assert page["to_block"] - page["from_block"] + 1 <= 2000
        for page in row["layers"]["L3"]["lp"]["mint_log_evidence"]["pages"]:
            assert page["to_block"] <= entry
            assert page["to_block"] - page["from_block"] + 1 <= 2000


def test_l1_qty_lineage_threshold_and_fail_closed_counts() -> None:
    per, v3, v2 = load(PER), load(V3), load(V2)
    v3by = {r["pool"].lower(): r for r in v3["rows"]}
    v2by = {r["pool"].lower(): r for r in v2["rows"]}
    reasons = Counter()
    for row in per["rows"]:
        pool = row["pool"]
        q3 = (((v3by[pool]["arm1"]["1"].get("archive") or {}).get("exact_size_quote") or {}).get("amount_in_token_raw"))
        q2 = (v2by[pool]["arm1"]["1"] or {}).get("position_qty_raw")
        expected = (int(q3), "GL5V3_EXACT_QUOTE") if q3 and int(q3) > 0 else (
            (int(q2), "GL5V2_FROZEN_POSITION") if q2 and int(q2) > 0 else (0, "UNRESOLVED")
        )
        assert (int(row["historical_qty_raw"]), row["qty_source"]) == expected
        l1 = row["layers"]["L1"]
        reasons[l1["reason"]] += 1
        if l1["pass"]:
            assert G.QTY_LO <= l1["qty_ratio"] <= G.QTY_HI
            assert l1["recovery_ratio"] >= G.RECOVERY_MIN
    assert reasons == Counter({"ok": 333, "fee_or_qty_missing": 6})


def test_l2_static_analysis_metadata_and_owner_are_exact() -> None:
    per = load(PER)
    assert G.pushed_selectors(bytes.fromhex("63f9f92be400")) == {"f9f92be4"}
    assert G.pushed_selectors(bytes.fromhex("65f9f92be4000000")) == set()
    fake = bytes.fromhex("00a1f4ff0003")
    region, meta = G.executable_region(fake)
    assert region == b"\x00"
    assert meta == {"metadata_stripped": True, "metadata_size": 5}
    reasons = Counter(x for r in per["rows"] for x in r["layers"]["L2"]["reasons"])
    assert reasons == Counter({"owner_not_renounced": 2})
    assert sum(r["layers"]["L2"]["pass"] for r in per["rows"]) == 337
    assert sum(r["layers"]["L2"]["metadata_stripped"] for r in per["rows"]) == 339
    for row in per["rows"]:
        l2 = row["layers"]["L2"]
        assert l2["executable_code_size"] + l2["metadata_size"] == l2["runtime_code_size"]


def test_l3_lp_and_reserve_fail_closed_adjudication_is_exact() -> None:
    per = load(PER)
    reasons = Counter(x for r in per["rows"] for x in r["layers"]["L3"]["reasons"])
    assert reasons == Counter({"active_lp_not_burn_custodied": 333,
                               "reserve_unresolved_or_declining": 47,
                               "no_active_position": 4, "lp_mapping_unresolved": 2})
    assert sum(r["layers"]["L3"]["pass"] for r in per["rows"]) == 0
    assert Counter(r["layers"]["L3"]["lp"]["mapping_complete"] for r in per["rows"]) == Counter({True: 337, False: 2})
    for row in per["rows"]:
        lp = row["layers"]["L3"]["lp"]
        if lp["pass"]:
            assert lp["active_position_n"] > 0
            active = [p for p in lp["positions"] if p.get("matches_pool") and int(p.get("liquidity_raw") or 0) > 0]
            assert all(p["owner"] == G.DEAD for p in active)


def test_l4_age_repeat_and_external_timestamp_policy_are_exact() -> None:
    per, result = load(PER), load(RESULT)
    assert sum(r["layers"]["L4"]["age_pass"] for r in per["rows"]) == 339
    assert [r["layers"]["L4"]["repeat_l1"]["pass"] for r in per["rows"]] == [r["layers"]["L1"]["pass"] for r in per["rows"]]
    assert result["external_overlay"] == {"eligible_n": 0, "unavailable_n": 339, "critical_n": 0}
    assert G.evaluate_external_snapshot({"published_at": "2025-01-01", "snapshot_provenance": "x", "critical": ["x"]}, "2025-01-02") == {
        "status": "EXTERNAL_ELIGIBLE_PIT", "eligible": True, "critical": ["x"],
        "published_at": "2025-01-01", "snapshot_provenance": "x",
    }
    assert G.evaluate_external_snapshot({"published_at": "2025-01-02", "snapshot_provenance": "x"}, "2025-01-02")["eligible"] is False


def test_powerset_metrics_bounds_rugs_and_winners_are_exact() -> None:
    result, per, labels = load(RESULT), load(PER), load(LABELS)
    expected_ids = {"L0" if not s else "L0+" + "+".join(s)
                    for n in range(5) for s in itertools.combinations(("L1", "L2", "L3", "L4"), n)}
    assert {x["id"] for x in result["combinations"]} == expected_ids
    assert len(result["combinations"]) == 16
    rugs = {x["pool"].lower() for x in labels["rug_pools"]}
    winners = {r["pool"] for r in per["rows"] if r["economics"]["1"]["resolved"] and r["economics"]["1"]["ret"] >= 2.0}
    assert len(winners) == 50
    for combo in result["combinations"]:
        selected = [r for r in per["rows"] if all(r["layers"][x]["pass"] for x in combo["layers"])]
        for off in ("1", "5", "15"):
            cells = [r["economics"][off] for r in selected]
            lo = [c["ret"] if c["resolved"] else R.PESS for c in cells]
            hi = [c["ret"] if c["resolved"] else (c.get("optimistic_qv2_ret") or 0.0) for c in cells]
            m = combo["metrics"][off]
            assert m["passed_n"] == len(selected)
            assert m["rug_admitted_n"] == sum(r["pool"] in rugs for r in selected)
            assert m["winners_retained_n"] == sum(r["pool"] in winners for r in selected)
            assert m["pooled_mean_interval"] == ([float(np.mean(lo)), float(np.mean(hi))] if lo else [None, None])
            assert m["bootstrap_lower"] == R.bootstrap(lo, combo["index"], int(off))


def _verdict(non_l0: list[dict]) -> str:
    powered = [x for x in non_l0 if x["powered"]]
    if not powered:
        return "INSUFFICIENT_N"
    return "CLEAN_EDGE_SURVIVES" if any(x["eligible"] for x in powered) else "CLEAN_FILTER_KILLS_EDGE"


def test_pareto_sweet_spot_and_three_label_mapping_are_exact() -> None:
    result = load(RESULT)
    assert [x["id"] for x in result["pareto_frontier"]] == ["L0+L1", "L0+L2", "L0+L4", "L0+L1+L4"]
    assert result["sweet_spot"]["id"] == "L0+L1"
    assert result["sweet_spot"]["sweet_score"] == 0.5984604558492302
    states = [x["primary_gates"] for x in result["combinations"] if x["id"] != "L0"]
    assert _verdict(states) == result["verdict_candidate"] == "CLEAN_FILTER_KILLS_EDGE"
    assert _verdict([{"powered": False, "eligible": False}]) == "INSUFFICIENT_N"
    assert _verdict([{"powered": True, "eligible": True}]) == "CLEAN_EDGE_SURVIVES"
    assert _verdict([{"powered": True, "eligible": False}]) == "CLEAN_FILTER_KILLS_EDGE"


def test_golden_rpc_ledger_secret_and_go_are_exact() -> None:
    result, per, golden, summary = load(RESULT), load(PER), load(GOLDEN), load(LEDGER_SUM)
    ranked = sorted((hashlib.sha256(f"GL9-golden|{r['pool']}".encode()).hexdigest(), r["pool"])
                    for r in per["rows"])[:5]
    assert golden["expected_n"] == golden["actual_n"] == 5
    assert [(x["rank_sha256"], x["pool"]) for x in golden["rows"]] == ranked
    raw = LEDGER.read_bytes()
    rows = [json.loads(x) for x in raw.splitlines()]
    started = [x for x in rows if x["event"] == "attempt_started"]
    finished = [x for x in rows if x["event"] == "attempt_finished"]
    key = lambda x: (x["call_id"], x["attempt_global"], x["attempt_for_call"], x["method"], x["params_sha256"])
    assert Counter(map(key, started)) == Counter(map(key, finished))
    assert len(started) == len(finished) == 5126
    assert summary["ledger_complete"] is True
    assert summary["ledger_sha256"] == hashlib.sha256(raw).hexdigest() == "3b4927e1870bc0aed8284dbf836aebba8e8139fcaa8674f5621f3ea190746bcd"
    assert set(summary["methods"]) <= {"eth_call", "eth_getBlockByNumber", "eth_getBalance", "eth_getLogs"}
    assert summary["send_sign_broadcast_calls"] == 0
    assert summary["public_fallback_used"] is False
    text = "\n".join(p.read_text(errors="replace") for p in (RESULT, PER, GOLDEN, LEDGER, LEDGER_SUM))
    assert re.search(r"https?://", text, re.I) is None
    assert re.search(r"(?:PRIVATE_KEY|MNEMONIC|SEED_PHRASE)\s*[:=]", text, re.I) is None
    for artifact in (result, per, golden):
        assert artifact["go_decision"] == "NOT_MADE"
