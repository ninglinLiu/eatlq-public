import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "reports/GL2_C_prereg_exogenous_weather_20260715.md"
RESULT = ROOT / "mvp/diagnostics/base_gl2_c_weather_oos_20260715.json"
REPORT = ROOT / "reports/GL2_C_exogenous_weather_oos_result_20260715.md"
PREREG_SHA = "2ee3684425e66a42366b5f291c6fd016304d3c295bf263cdf8daecb1e0f662ea"


def load():
    return json.loads(RESULT.read_text())


def test_frozen_preregistration_and_safety_posture_are_exact():
    data = load()
    assert hashlib.sha256(PREREG.read_bytes()).hexdigest() == PREREG_SHA
    assert data["preregistration_sha256"] == PREREG_SHA
    assert data["chronology"]["result_computed_after_preregistration"] == True
    assert data["rpc_calls"] == 0
    assert data["research_only"] == True
    assert data["go_decision"] == "NOT_MADE"
    assert data["decision_flags"] == {
        "live_allowed": False,
        "self_go_allowed": False,
        "training_allowed": False,
    }


def test_cohorts_and_five_indicator_statuses_are_exact():
    data = load()
    assert data["cohorts"] == {"FULL_UNIVERSE_431": 431, "KEYSTONE": 116}
    assert data["basket_summary"]["preregistered_indicators"] == 5
    assert data["basket_summary"]["available_and_oos_tested"] == 2
    assert data["basket_summary"]["stable_win_count"] == 0
    assert {k: v["status"] for k, v in data["indicators"].items()} == {
        "aggregate_weth_net_inflow_7d": "UNAVAILABLE_NOT_TESTED",
        "base_gas_regime_7d": "UNAVAILABLE_NOT_TESTED",
        "eth_realized_volatility_7d": "NO_STABLE_OOS_WIN",
        "pool_creation_rate_7d": "OOS_UNPOWERED",
        "whole_universe_rug_rate_7d": "UNAVAILABLE_NOT_TESTED",
    }


def test_pool_creation_train_threshold_and_zero_oos_opens_are_exact():
    item = load()["indicators"]["pool_creation_rate_7d"]
    chosen = item["training"]["selected"]
    assert chosen["direction"] == "high"
    assert chosen["quantile"] == 0.8
    assert chosen["threshold"] == 2830.6857142857148
    assert chosen["open_days"] == 10
    assert chosen["eligible_days"] == 47
    assert chosen["primary_finite_n"] == 37
    assert item["oos"]["eligible_days"] == 55
    assert item["oos"]["open_days"] == 0
    assert item["oos"]["endpoints"]["KEYSTONE"]["1"]["true_gate"]["n"] == 0


def test_eth_volatility_threshold_primary_and_random_comparator_are_exact():
    item = load()["indicators"]["eth_realized_volatility_7d"]
    chosen = item["training"]["selected"]
    assert chosen["direction"] == "low"
    assert chosen["quantile"] == 0.5
    assert chosen["threshold"] == 0.02870375381542059
    assert chosen["open_days"] == 20
    assert chosen["primary_finite_n"] == 38
    assert item["oos"]["eligible_days"] == 55
    assert item["oos"]["open_days"] == 46
    primary = item["oos"]["endpoints"]["KEYSTONE"]["1"]
    assert primary["true_gate"]["n"] == 22
    assert primary["true_gate"]["unresolved"] == 2
    assert primary["true_gate"]["mean"] == 0.15116430880698284
    assert primary["random_same_open_share"]["draws"] == 10000
    assert primary["random_same_open_share"]["median"] == 0.19143161798206065
    assert primary["random_same_open_share"]["p95"] == 0.26772749099292215
    assert primary["true_minus_random_median"] == -0.040267309175077814
    assert primary["true_strictly_gt_random_p95"] == False


def test_subwindows_secondary_and_top_tail_control_are_exact():
    oos = load()["indicators"]["eth_realized_volatility_7d"]["oos"]
    assert oos["subwindows"]["O1"]["KEYSTONE"]["1"]["true_gate"]["n"] == 18
    assert oos["subwindows"]["O2"]["KEYSTONE"]["1"]["true_gate"]["n"] == 4
    assert oos["subwindows"]["O2"]["KEYSTONE"]["1"]["true_minus_random_median"] == 0.0
    secondary = oos["endpoints"]["FULL_UNIVERSE_431"]["1"]
    assert secondary["true_gate"]["n"] == 132
    assert secondary["true_gate"]["unresolved"] == 24
    assert secondary["true_gate"]["mean"] == -0.3035771546678729
    trim = oos["top_positive_1pct_removed"]
    assert trim["source_n"] == 22
    assert trim["actually_removed_positive_n"] == 1
    assert trim["removed_values"] == [2.9937921752208028]
    assert trim["remaining"]["mean"] == 0.015801077072991433


def test_report_keeps_cohorts_unavailable_sources_and_review_boundary_visible():
    text = REPORT.read_text()
    assert "KEYSTONE 选中 116 条" in text
    assert "同龄全宇宙 431 条" in text
    assert text.count("UNAVAILABLE_NOT_TESTED") >= 4
    assert "最终分级由审查 agent 逐行决定" in text
    assert "不定 GL1 grade，不作 GO" in text


if __name__ == "__main__":
    tests = [
        test_frozen_preregistration_and_safety_posture_are_exact,
        test_cohorts_and_five_indicator_statuses_are_exact,
        test_pool_creation_train_threshold_and_zero_oos_opens_are_exact,
        test_eth_volatility_threshold_primary_and_random_comparator_are_exact,
        test_subwindows_secondary_and_top_tail_control_are_exact,
        test_report_keeps_cohorts_unavailable_sources_and_review_boundary_visible,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
