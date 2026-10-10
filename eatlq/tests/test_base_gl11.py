from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path("/opt/EATLQ")
import sys
sys.path.insert(0, str(ROOT / "scripts/research/base_gl11"))
from bankroll_core import (bootstrap_indices, concurrency_filter, kelly_grid, n8_interpretation,
                           simulate, viability_label)  # noqa:E402

OUT = ROOT / "mvp/diagnostics/base_gl11_bankroll_variance_capacity_20260716.json"
PER = ROOT / "mvp/diagnostics/base_gl11_bankroll_variance_capacity_per_leg_20260716.json"
LEDGER = ROOT / "mvp/diagnostics/base_gl11_rpc_full_ledger_20260716.jsonl"
SUMMARY = ROOT / "mvp/diagnostics/base_gl11_rpc_ledger_20260716.json"


def load(path: Path):
    return json.loads(path.read_text())


def test_preregistration_and_all_errata_hashes_exact():
    expected = {
        "reports/GL11_preregistration_20260716.md": "0ae0a33443c8ac66a36504ded6d4920a1fe5e42f737cd18d55eb5b7d6273e8b7",
        "reports/GL11_preregistration_erratum_capacity_unresolved_20260716.md": "73da6b55a69e1f370892f1f4e2a23e39f7874db527f68e2a42d6ff959aede2a4",
        "reports/GL11_preregistration_erratum_month_block_phase_20260716.md": "0f323200ba0ea9e1b83e6e2b3769e66b38bb34b4ecbae187debf35e64a43ee8a",
        "reports/GL11_preregistration_erratum_entry_notional_scale_20260716.md": "9c1f1e454c034b1ac5c6388b2d98d9d402658129793a8b9570a87286a3dca4b5",
    }
    for rel, digest in expected.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest


def test_four_empirical_vectors_and_gl10_lineage_exact():
    out = load(OUT)
    assert out["step0"] == {"L0_n": 339, "capacity_resolved_n": 330, "capacity_unresolved_n": 9,
                            "gl10_dependency_verdict": "REACTIVE_EXIT_INEFFECTIVE",
                            "model_fit_executed": False}
    d = out["empirical_distributions"]
    assert d["BASELINE_LOWER"]["vector_sha256"] == "91ff6ee150fd836b7169b8e7bdd326489eb051c3fee7e7efb3b8574f4ff93bb8"
    assert d["BASELINE_UPPER"]["vector_sha256"] == "29abf56926c97c6baa8578c710155717a7f578dc92e30ef3330e0409c345b2c2"
    assert d["GL10_L1_LOWER"]["vector_sha256"] == "636a9ce9a332ce151abfa1831e1305594c237c753566352c2562b4194ac69be7"
    assert d["GL10_L1_UPPER"]["vector_sha256"] == "32b1b8c1df9146a6f071999a9cae209ea51014c66462d6c9c109c3c9ac372164"
    assert d["BASELINE_LOWER"]["rug_outcome_rate"] == 0.6460176991150443
    assert d["BASELINE_LOWER"]["loss_floor_rate"] == 0.02654867256637168


def test_capacity_scale_formula_and_fail_closed_exact():
    rows = load(PER)["rows"]
    assert len(rows) == 339
    resolved = [x for x in rows if x["capacity_status"] == "RESOLVED"]
    assert len(resolved) == 330
    for row in resolved:
        assert row["strategy_entry_weth_raw"] > 0
        assert row["implied_ethusd"] == 50.0 / (row["strategy_entry_weth_raw"] / 1e18)
        assert np.isfinite(row["capacity_usd"]) and row["capacity_usd"] > 0
    assert all(x["capacity_usd"] is None for x in rows if x["capacity_status"] != "RESOLVED")


def test_concurrency_is_time_only_and_counts_exact():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"pool": "a", "entry_time": t, "exit_time": t + timedelta(seconds=10), "entry_block": 1, "returns": 999},
        {"pool": "b", "entry_time": t + timedelta(seconds=1), "exit_time": t + timedelta(seconds=2), "entry_block": 2, "returns": -999},
        {"pool": "c", "entry_time": t + timedelta(seconds=11), "exit_time": t + timedelta(seconds=12), "entry_block": 3, "returns": 0},
    ]
    assert [x["pool"] for x in concurrency_filter(rows, 1)] == ["a", "c"]
    changed = [{**x, "returns": -x["returns"]} for x in rows]
    assert [x["pool"] for x in concurrency_filter(changed, 1)] == ["a", "c"]
    assert load(OUT)["concurrency_admission_n"] == {"1": 179, "3": 305, "5": 326}


def test_month_block_random_phase_and_order():
    groups = {"2026-01": [0, 1, 2, 3], "2026-02": [4, 5, 6]}
    idx = bootstrap_indices(groups, 100, 8, 2026071611)
    assert idx.shape == (100, 8)
    assert len(set(idx[:, 0].tolist())) > 2
    # Whenever two adjacent observations remain inside a source month, order is preserved.
    for row in idx:
        for a, b in zip(row, row[1:]):
            if (a <= 3 and b <= 3) or (a >= 4 and b >= 4):
                assert b == a + 1 or b <= a  # b<=a marks a newly sampled month block


def test_grid_ruin_mdd_required_n_and_selected_point_exact():
    out = load(OUT)
    assert len(out["grid"]) == 4 * 5 * 3 * 5 == 300
    assert all(x["paths"] == 10_000 and x["horizon_trades"] == 1024 for x in out["grid"])
    selected = out["selected_operating_point"]
    assert selected["fraction"] == .05
    assert out["selected_initial_effective_fraction_cap"] == .005
    assert selected["ruin_probability"] == 0.0
    assert selected["required_n_95"] == 8
    toy = simulate(np.asarray([-1.0]), np.asarray([1e9]), np.zeros((10, 3), dtype=int), .5, 1e9,
                   candidates=(3,))
    assert toy["ruin_probability"] == 1.0
    assert toy["terminal_p50"] == 1250.0
    assert toy["mdd_p50"] == .875


def test_kelly_capacity_monthly_and_gl10_parallel_exact():
    out = load(OUT)
    assert out["kelly"]["BASELINE_LOWER"] == {
        "full_kelly": .2, "half_kelly": .1, "quarter_kelly": .05,
        "mean_log_growth": 0.14916841291537103, "grid_step": .0001, "grid_max": .2}
    assert kelly_grid(np.asarray([1.0, -.5]))["full_kelly"] == .2
    assert out["signal_frequency_monthly"]["1"]["p50"] == 19.0
    assert out["capacity_ceiling_monthly_usd"]["1"]["p50"] == 4278.183889653645
    assert out["turnover_at_50_cap_monthly_usd"]["1"]["p50"] == 950.0
    assert out["selected_monthly_pnl_by_arm"]["BASELINE_LOWER"]["pnl_p50"] == 982.8439219418815
    assert out["selected_monthly_pnl_by_arm"]["GL10_L1_LOWER"]["pnl_p50"] == 914.0471258722519


def test_p8_and_viability_mappings_exact():
    out = load(OUT)
    assert out["p_8_cumulative_loss"] == 0.024375
    assert out["n8_interpretation"] == "POSSIBLE_BUT_TAIL_EVENT"
    assert out["p_8_cumulative_loss_by_arm"]["GL10_L1_LOWER"]["p_loss"] == .03186
    assert n8_interpretation(.1) == "READILY_COMPATIBLE_WITH_SAMPLING_VARIANCE"
    assert n8_interpretation(.01) == "POSSIBLE_BUT_TAIL_EVENT"
    assert n8_interpretation(.009999) == "UNLIKELY_PURE_VARIANCE_UNDER_HISTORICAL_DISTRIBUTION"
    assert viability_label(.005, 8, 1, 100) == "SURVIVABLE_OPERATING_POINT_DESCRIPTIVE"
    assert viability_label(.005, None, None, 1000) == "NOT_VIABLE_UNDER_SURVIVABLE_SIZING"
    assert out["viability_label"] == "SURVIVABLE_OPERATING_POINT_DESCRIPTIVE"


def test_zero_rpc_ledger_allowlist_secret_and_go_exact():
    out, per, summary = load(OUT), load(PER), load(SUMMARY)
    assert LEDGER.read_bytes() == b""
    assert hashlib.sha256(LEDGER.read_bytes()).hexdigest() == summary["ledger_sha256"]
    assert summary["ledger_complete"] is True
    assert summary["logical_calls"] == summary["http_attempts"] == 0
    assert summary["attempt_started_records"] == summary["attempt_finished_records"] == 0
    assert summary["methods"] == {}
    assert summary["send_sign_broadcast_calls"] == 0
    assert summary["endpoint_alias"] == "OFFLINE_NO_RPC"
    assert out["go_decision"] == per["go_decision"] == "NOT_MADE"
