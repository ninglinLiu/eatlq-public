"""GL8 frozen-result regression and safety assertions.

These tests are deliberately offline: they only reconcile committed/local artifacts and
never instantiate the archive RPC client.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np


ROOT = Path("/opt/EATLQ")
RESULT = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_20260716.json"
PER_LEG = ROOT / "mvp/diagnostics/base_gl8_pit_three_arm_per_leg_20260716.json"
KNOWN = ROOT / "mvp/diagnostics/base_gl8_known_bad_at_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl8_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl8_rpc_full_ledger_20260716.jsonl"
LEDGER_SUM = ROOT / "mvp/diagnostics/base_gl8_rpc_ledger_20260716.json"
V3 = ROOT / "mvp/diagnostics/base_gl5v3_Fon644_archive_per_leg_20260716.json"
V2 = ROOT / "mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json"
PREREG = ROOT / "reports/GL8_preregistration_20260716.md"
FOLD_ERRATUM = ROOT / "reports/GL8_preregistration_erratum_fold_coverage_20260716.md"
CHAIN_ERRATUM = ROOT / "reports/GL8_preregistration_erratum_chain_order_20260716.md"

PREREG_SHA = "70aca8dfe74e803efdfa928106a51c3cf09292932423a09823b4269243298b5b"
FOLD_ERRATUM_SHA = "a6f38c6319d768f6cf42061368fab1a7df6de2435f3a71550cf8aa15b2bfa6b3"
CHAIN_ERRATUM_SHA = "8595938efe32aabeeaa68482980ce10e2202b251669deb6dd86e5451c1b42de0"
PESS = -1.0229943556396193


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verdict(powered_n: int, pit_lower: float, floors: list[float], top1_removed: float,
            oracle_lower: float, hindsight_share: float) -> str:
    if powered_n < 30:
        return "INSUFFICIENT_N"
    supported = (
        pit_lower > 0
        and all(x >= -0.10 for x in floors)
        and top1_removed > 0
        and oracle_lower > 0
        and hindsight_share < 0.70
    )
    return "FORWARD_EDGE_SUPPORTED" if supported else "MOSTLY_HINDSIGHT"


def test_preregistration_chain_is_exact_and_immutable() -> None:
    result = load(RESULT)
    assert sha(PREREG) == PREREG_SHA
    assert sha(FOLD_ERRATUM) == FOLD_ERRATUM_SHA
    assert sha(CHAIN_ERRATUM) == CHAIN_ERRATUM_SHA
    assert result["preregistration_sha256"] == PREREG_SHA
    assert result["fold_coverage_erratum_sha256"] == FOLD_ERRATUM_SHA
    assert result["chain_order_erratum_sha256"] == CHAIN_ERRATUM_SHA


def test_step0_and_oracle_set_anchor_are_exact() -> None:
    result, per, known, v3 = load(RESULT), load(PER_LEG), load(KNOWN), load(V3)
    assert result["step0"] == {
        "labeled_n": 644,
        "common_NoF_n": 339,
        "OracleF_n": 120,
        "oracle_deployer_n": 247,
        "oracle_skeleton_n": 33,
        "outside_fold_n": 0,
        "fold_n": 9,
    }
    assert known["oracle_identity_counts"] == {"deployer": 247, "skeleton": 33}
    got = {r["pool"].lower() for r in per["rows"] if r["selections"]["OracleF"]}
    frozen = {r["pool"].lower() for r in v3["rows"] if r["selected_Fon"]}
    assert got == frozen
    assert len(got) == 120


def test_no_backfill_fold_snapshot_and_family_provenance() -> None:
    result, per = load(RESULT), load(PER_LEG)
    fold_start = {x["id"]: x["fold_start_block"] for x in result["folds"]}
    for fold, block in fold_start.items():
        all_fold_blocks = [int(r["entry_block"]) for r in per["rows"] if r["fold"]["id"] == fold]
        assert block == min(all_fold_blocks)
    active_hit_n = 0
    for row in per["rows"]:
        block = fold_start[row["fold"]["id"]]
        assert block <= int(row["entry_block"])
        for hit in row["pit_active_hits"]:
            active_hit_n += 1
            assert int(hit["known_bad_block"]) < int(hit["fold_start_block"])
            assert int(hit["fold_start_block"]) == block
            assert int(hit["fold_start_block"]) <= int(row["entry_block"])
            assert hit["strict_before"] is True
            typ = hit["identity_type"]
            assert row["identity"][typ] == hit["identity"]
        assert row["selections"]["PITF"] == (
            row["common_gate_pass"] and len(row["pit_active_hits"]) == 0
        )
    # The fold snapshot cannot learn from an identity first exposed inside the fold.
    assert active_hit_n == 1
    assert sum(r["common_gate_pass"] for r in per["rows"]) == 339
    assert sum(r["selections"]["PITF"] for r in per["rows"]) == 338


def test_qty_lineage_is_exact_for_every_common_leg() -> None:
    per, v3, v2 = load(PER_LEG), load(V3), load(V2)
    v3_by = {r["pool"].lower(): r for r in v3["rows"]}
    v2_by = {r["pool"].lower(): r for r in v2["rows"]}
    for row in per["rows"]:
        if not row["common_gate_pass"]:
            continue
        pool = row["pool"].lower()
        for off in ("1", "5", "15"):
            q3 = (((v3_by[pool]["arm1"][off].get("archive") or {}).get("exact_size_quote") or {})
                  .get("amount_in_token_raw"))
            q2 = (v2_by[pool]["arm1"][off] or {}).get("position_qty_raw")
            expected = (int(q3), "GL5V3_EXACT_QUOTE") if q3 and int(q3) > 0 else (
                (int(q2), "GL5V2_FROZEN_POSITION") if q2 and int(q2) > 0 else (0, "UNRESOLVED")
            )
            cell = row["economics"][off]
            assert (int(cell["qty_raw"]), cell["qty_source"]) == expected


def test_collapse_precedence_bounds_and_pooled_formulas() -> None:
    result, per = load(RESULT), load(PER_LEG)
    for arm in ("NoF", "PITF", "OracleF"):
        selected = [r for r in per["rows"] if r["selections"][arm]]
        for off in ("1", "5", "15"):
            cells = [r["economics"][off] for r in selected]
            for cell in cells:
                if "CONFIRMED_COLLAPSE" in cell["branch"]:
                    assert cell["resolved"] is True
                    assert cell["ret"] == PESS
            lower = [c["ret"] if c["resolved"] else PESS for c in cells]
            upper = [c["ret"] if c["resolved"] else (c.get("optimistic_qv2_ret") or 0.0) for c in cells]
            metric = result["metrics"]["pooled"][arm][off]
            assert metric["selected_n"] == len(cells)
            assert metric["resolved_n"] == sum(c["resolved"] for c in cells)
            assert metric["unresolved_n"] == sum(not c["resolved"] for c in cells)
            assert metric["population_mean_interval"][0] == float(np.mean(lower))
            assert metric["population_mean_interval"][1] == float(np.mean(upper))


def test_alpha_hindsight_power_and_frozen_verdict_mapping() -> None:
    r = load(RESULT)
    nof = r["metrics"]["pooled"]["NoF"]["1"]["population_mean_interval"]
    pit = r["metrics"]["pooled"]["PITF"]["1"]["population_mean_interval"]
    oracle = r["metrics"]["pooled"]["OracleF"]["1"]["population_mean_interval"]
    assert r["metrics"]["pooled"]["PITF"]["1"]["conditional_alpha_vs_NoF"] == [
        pit[0] - nof[1], pit[1] - nof[0]
    ]
    share = (oracle[0] - pit[0]) / oracle[0]
    assert r["hindsight_share"]["1"]["hindsight_share"] == share
    assert r["powered"] == {"threshold": 30, "PITF_clean_resolved_plus1_n": 333, "pass": True}
    floors = [x["lower_mean"] for x in r["fold_floor_plus1"]]
    actual = verdict(333, pit[0], floors,
                     r["metrics"]["pooled"]["PITF"]["1"]["top1_removed_lower_mean"],
                     oracle[0], share)
    assert actual == "FORWARD_EDGE_SUPPORTED"
    assert r["verdict_candidate"] == actual
    # Exercise all three frozen labels and the strict 0.70 boundary.
    assert verdict(29, 1.0, [0.0], 0.5, 1.2, 0.1) == "INSUFFICIENT_N"
    assert verdict(30, 0.1, [-0.10], 0.1, 1.0, 0.699999) == "FORWARD_EDGE_SUPPORTED"
    assert verdict(30, 0.1, [-0.10], 0.1, 1.0, 0.70) == "MOSTLY_HINDSIGHT"
    assert verdict(30, 0.0, [0.0], 0.1, 1.0, 0.1) == "MOSTLY_HINDSIGHT"


def test_golden_selection_and_known_bad_evidence_are_exact() -> None:
    golden, known = load(GOLDEN), load(KNOWN)
    certified = [x for x in known["identities"] if x["status"] == "CERTIFIED"]
    ranked = sorted(
        (hashlib.sha256(f"GL8-golden|{x['identity_type']}|{x['identity']}".encode()).hexdigest(), x)
        for x in certified
    )[:5]
    assert golden["expected_n"] == 5
    assert golden["actual_n"] == 5
    assert golden["category_shortfall"] is False
    assert [x["rank_sha256"] for x in golden["rows"]] == [x[0] for x in ranked]
    known_by = {(x["identity_type"], x["identity"]): x for x in certified}
    for row in golden["rows"]:
        source = known_by[(row["identity_type"], row["identity"])]
        ev = source["evidence"]
        assert source["known_bad_block"] == row["known_bad_block"]
        assert source["evidence_pool"] == row["evidence_pool"]
        assert ev["reserve_rule_pass"] is True
        assert ev["no_positive_recovery"] is True
        assert ev["block_evidence"]["ok"] is True
        assert ev["qty_source"] in {"GL5V3_EXACT_QUOTE", "GL5V2_FROZEN_POSITION"}
        assert row["evidence_tx"] is None


def test_rpc_ledger_allowlist_completeness_hash_and_no_secret() -> None:
    summary = load(LEDGER_SUM)
    raw = LEDGER.read_bytes()
    rows = [json.loads(x) for x in raw.splitlines()]
    allowed = {"eth_call", "eth_getBlockByNumber", "eth_getBalance", "eth_getLogs"}
    started = [x for x in rows if x["event"] == "attempt_started"]
    finished = [x for x in rows if x["event"] == "attempt_finished"]
    assert summary["ledger_complete"] is True
    assert len(started) == len(finished) == summary["http_attempts"] == 3728
    assert summary["attempt_started_records"] == summary["attempt_finished_records"] == 3728
    assert {x["method"] for x in rows} <= allowed
    assert summary["ledger_sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["ledger_sha256"] == "c0597b4718bb8308a749fbf38f9c7a9ced2cc83f2df998fe38de83995932f5c9"
    assert summary["send_sign_broadcast_calls"] == 0
    assert summary["public_fallback_used"] is False
    scan_paths = [RESULT, PER_LEG, KNOWN, GOLDEN, LEDGER, LEDGER_SUM]
    text = "\n".join(p.read_text(errors="replace") for p in scan_paths)
    assert re.search(r"https?://", text, re.I) is None
    assert re.search(r"(?:PRIVATE_KEY|MNEMONIC|SEED_PHRASE|ARCHIVE_RPC)\s*[:=]\s*[^\s\"']+", text, re.I) is None


def test_research_boundary_and_go_are_exact() -> None:
    for path in (RESULT, PER_LEG, KNOWN, GOLDEN):
        assert load(path)["go_decision"] == "NOT_MADE"
    result = load(RESULT)
    assert result["research_only"] is True
    assert result["wallet_level_boundary"] == "GL7v2 exact strategy-wallet sellability remains unresolved"
