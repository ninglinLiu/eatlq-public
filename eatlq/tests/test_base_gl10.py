from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path("/opt/EATLQ")
import sys
sys.path.insert(0, str(ROOT / "scripts/research/base_gl10"))
from reactive_exit_core import PESS, first_onset, vector_metrics, verdict  # noqa:E402

OUT = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl10_reactive_exit_per_leg_20260716.json"
GOLDEN = ROOT / "mvp/diagnostics/base_gl10_golden_reconciliation_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl10_rpc_full_ledger_20260716.jsonl"
SUMMARY = ROOT / "mvp/diagnostics/base_gl10_rpc_ledger_20260716.json"


def load(path: Path):
    return json.loads(path.read_text())


def test_preregistration_chain_exact():
    expected = {
        ROOT / "reports/GL10_preregistration_20260716.md": "1c2eff5173dda874eb32ffe360abdfada4922fcb30655a021eb2b6168e4bd62b",
        ROOT / "reports/GL10_preregistration_erratum_time_order_20260716.md": "6600d839f1b63e9f870be08cd96af24122f20f9fa7032e7879f29b83ace71257",
    }
    for path, digest in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_onset_first_hit_and_no_future_access():
    single = first_onset(100, 1000, {101: -100, 102: -300, 103: 900})
    assert single["status"] == "ONSET_DETECTED"
    assert single["onset_block"] == 102
    assert single["evidence_max_block"] == 102
    assert single == first_onset(100, 1000, {101: -100, 102: -300})
    rolling = first_onset(100, 1000, {100: -200, 101: -200, 102: -150})
    assert rolling["onset_block"] == 102
    assert rolling["single_hit"] is False
    assert rolling["rolling_hit"] is True
    assert first_onset(100, 1000, {100: -100, 101: 20})["status"] == "ONSET_NOT_DETECTED"


def test_population_status_qty_and_lag_counts_exact():
    out = load(OUT)
    assert out["step0"] == {"L0_n": 339, "L0_rug_n": 219, "decision_then_economics_join": True,
                            "labeled_rug_n": 262, "model_fit_executed": False}
    assert out["onset_status_counts"] == {"ONSET_DETECTED": 260, "ONSET_NOT_DETECTED": 2}
    assert out["qty_source_counts"] == {"GL5V2_FROZEN_POSITION": 18, "GL5V3_EXACT_QUOTE": 236,
                                        "UNRESOLVED": 8}
    observed = [(x["lag_blocks"], x["escapable_n"], x["no_exit_n"], x["unresolved_n"])
                for x in out["lags"]]
    assert observed == [(1, 46, 205, 11), (2, 46, 205, 11), (3, 46, 205, 11),
                        (5, 46, 205, 11), (15, 44, 206, 12)]
    assert [x["revert_n"] for x in out["lags"]] == [205, 205, 205, 205, 206]


def test_all_evidence_is_causal_and_revert_is_not_success():
    rows = load(PER)["rows"]
    assert len(rows) == 262
    for row in rows:
        onset = row["onset"]
        assert onset["log_to_block_max"] <= row["flip_block"]
        if onset["onset_block"] is not None:
            assert onset["evidence_max_block"] == onset["onset_block"]
        for lag in (1, 2, 3, 5, 15):
            r = row["reactions"][str(lag)]
            if r["reaction_block"] is not None:
                assert r["reaction_block"] == onset["onset_block"] + lag
                assert r["evidence_max_block"] == r["reaction_block"]
            if r["status"] == "ESCAPABLE":
                probe = r["attempts"][-1]["probe"]
                assert probe["buy_ok"] is True
                assert probe["sell_ok"] is True
                assert int(r["recovered_weth_raw"]) > 0
                assert 0.98 <= r["qty_ratio"] <= 1.02
            else:
                assert r.get("reactive_ret") is None


def test_double_bounds_and_overlay_economics_exact():
    out = load(OUT)
    b = out["baseline_economics"]
    assert b["1"]["pooled_mean_interval"] == [0.9599071830294859, 0.9907082064906041]
    assert b["5"]["pooled_mean_interval"] == [0.8747963822103998, 0.8963896540397844]
    assert b["15"]["pooled_mean_interval"] == [0.6567766824320225, 0.6662377865635439]
    one = out["lags"][0]
    assert one["rug_baseline_median_lower"] == 0.4549185327736302
    assert one["rug_reactive_median_lower"] == 0.3921222902191012
    assert one["rug_median_improvement_lower"] == -0.06279624255452898
    assert one["overlay_counts"]["1:REACTIVE_EXIT_EARLIER"] == 33
    assert one["economics"]["1"]["lower_vector_sha256"] == "636a9ce9a332ce151abfa1831e1305594c237c753566352c2562b4194ac69be7"
    toy = vector_metrics([{"resolved": True, "ret": 1.0},
                          {"resolved": False, "optimistic_qv2_ret": 2.0}])
    assert toy["pooled_mean_interval"] == [(1.0 + PESS) / 2, 1.5]


def test_frozen_verdict_mapping_all_three_labels():
    base = {"pooled_mean_interval": [0.1, 0.1], "ex_top1_lower_mean": 0.05}
    good = {"lag_blocks": 1, "evaluable_n": 30, "rug_median_improvement_lower": .20,
            "economics": {"1": {"pooled_mean_interval": [.1, .1], "ex_top1_lower_mean": .05}}}
    bad = {**good, "rug_median_improvement_lower": .199999}
    assert verdict([good], base) == ("REACTIVE_EXIT_MATERIALLY_HELPS", [1])
    assert verdict([bad], base) == ("REACTIVE_EXIT_INEFFECTIVE", [])
    assert verdict([{**good, "evaluable_n": 29}], base) == ("INSUFFICIENT_N", [])
    out = load(OUT)
    assert out["verdict_candidate"] == "REACTIVE_EXIT_INEFFECTIVE"
    assert out["eligible_lags"] == []


def test_golden_selection_and_objects_exact():
    per = {x["pool"]: x for x in load(PER)["rows"]}
    golden = load(GOLDEN)["rows"]
    expected = [p for _, p in sorted((hashlib.sha256(f"GL10-golden|{p}".encode()).hexdigest(), p)
                                     for p in per)[:5]]
    assert [x["pool"] for x in golden] == expected
    for g in golden:
        src = per[g["pool"]]
        assert g["onset"] == src["onset"]
        assert g["reactions"] == src["reactions"]


def test_rpc_ledger_allowlist_pairing_hash_and_no_secret():
    summary = load(SUMMARY)
    assert summary["ledger_complete"] is True
    assert summary["attempt_started_records"] == summary["attempt_finished_records"] == 3479
    assert summary["logical_calls"] == 3350
    assert summary["methods"] == {"eth_call": 2052, "eth_getLogs": 1298}
    assert set(summary["methods"]) <= {"eth_call", "eth_getBlockByNumber", "eth_getBalance", "eth_getLogs"}
    assert summary["send_sign_broadcast_calls"] == 0
    assert summary["public_fallback_used"] is False
    assert hashlib.sha256(LEDGER.read_bytes()).hexdigest() == summary["ledger_sha256"]
    records = [json.loads(x) for x in LEDGER.read_text().splitlines()]
    assert len(records) == 2 * 3479
    starts = {(x["call_id"], x["attempt_global"], x["method"], x["params_sha256"])
              for x in records if x["event"] == "attempt_started"}
    finishes = {(x["call_id"], x["attempt_global"], x["method"], x["params_sha256"])
                for x in records if x["event"] == "attempt_finished"}
    assert starts == finishes
    raw = LEDGER.read_text().lower()
    assert "http://" not in raw and "https://" not in raw and "wss://" not in raw
    for forbidden in ("eth_send", "eth_sign", "sendrawtransaction", "sendtransaction"):
        assert forbidden not in raw


def test_go_not_made_everywhere():
    assert load(OUT)["go_decision"] == "NOT_MADE"
    assert load(PER)["go_decision"] == "NOT_MADE"
    assert load(GOLDEN)["go_decision"] == "NOT_MADE"
