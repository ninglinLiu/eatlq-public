import json
import sys
import unittest
from pathlib import Path

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/research/base_rm1"))

from rm1_framework import (  # noqa: E402
    ALLOWED_READONLY_RPC_METHODS,
    FORBIDDEN_SEND_RPC_METHODS,
    ArmRuntime,
    BroadcastDisabled,
    DisabledBroadcaster,
    GuardViolation,
    d5_passed,
    entry_slippage_pp,
    exit_slippage_pp,
    file_sha256,
    keystone_exit_reason,
    latency_ms,
    load_and_validate_prereg,
    observation_flags,
    shakedown_exit_due,
    source_constant_proof,
)


GOLDEN = json.loads(
    (ROOT / "tests/fixtures/base_rm1_v2_golden_20260715.json").read_text()
)


def good_d5():
    return {
        "status": "measured",
        "buy_ok": True,
        "sell_ok": True,
        "buy_integer_equal": True,
        "sell_integer_equal": True,
        "recovery_ratio": 0.994,
        "received_target": "123",
    }


class TestBaseRm1(unittest.TestCase):
    def test_prereg_hash_and_fail_closed_posture(self):
        cfg = load_and_validate_prereg()
        path = ROOT / "configs/research/base_rm1_v2_preregistration_20260715.json"
        assert file_sha256(path) == GOLDEN["preregistration_sha256"]
        assert cfg["posture"]["live_broadcast"] == "DISABLED"
        assert cfg["posture"]["live_allowed"] == False
        assert cfg["posture"]["self_go_allowed"] == False
        assert cfg["posture"]["signing_allowed"] == False
        assert cfg["posture"]["send_rpc_allowed"] == False
        assert cfg["chain"]["notional_weth_wei"] == GOLDEN["notional_weth_wei"]

    def test_source_anchored_keystone_constants(self):
        proof = source_constant_proof(load_and_validate_prereg())
        expected = GOLDEN["keystone_exit"]
        assert proof["hard_stop_fraction"] == expected["hard_stop_fraction"]
        assert proof["trailing_fraction"] == expected["trailing_drawdown_fraction"]
        assert proof["maximum_hold_blocks"] == expected["maximum_hold_blocks"]

    def test_keystone_exit_rules_use_exact_boundaries(self):
        assert keystone_exit_reason(
            entry_price=100.0, peak_price=100.0, current_price=50.0, blocks_held=1
        ) == "HARD_STOP_MINUS_50_PERCENT"
        assert keystone_exit_reason(
            entry_price=100.0, peak_price=200.0, current_price=130.0, blocks_held=1
        ) == "TRAIL_35_PERCENT_FROM_PEAK"
        assert keystone_exit_reason(
            entry_price=100.0, peak_price=100.0, current_price=100.0, blocks_held=7200
        ) == "MAX_HOLD_7200_BLOCKS"
        assert keystone_exit_reason(
            entry_price=100.0, peak_price=120.0, current_price=100.0, blocks_held=7199
        ) == None

    def test_shakedown_requires_human_positive_N(self):
        assert shakedown_exit_due(blocks_held=14, hold_blocks=15) == False
        assert shakedown_exit_due(blocks_held=15, hold_blocks=15) == True
        for invalid in (0, 7201):
            try:
                shakedown_exit_due(blocks_held=1, hold_blocks=invalid)
                raised = False
            except ValueError:
                raised = True
            assert raised == True

    def test_d5_requires_all_integer_and_recovery_guards(self):
        cfg = load_and_validate_prereg()
        evidence = good_d5()
        assert d5_passed(evidence, cfg) == True
        evidence["sell_integer_equal"] = False
        assert d5_passed(evidence, cfg) == False
        evidence = good_d5()
        evidence["recovery_ratio"] = 0.1
        assert d5_passed(evidence, cfg) == False

    def test_broadcaster_is_unconditionally_disabled(self):
        broadcaster = DisabledBroadcaster()
        try:
            broadcaster.submit({"to": "0x0", "data": "0x"})
            raised = False
        except BroadcastDisabled as exc:
            raised = str(exc) == "LIVE_BROADCAST_DISABLED_NO_SEND_PATH"
        assert raised == True
        assert broadcaster.attempted_calls == 1
        assert ALLOWED_READONLY_RPC_METHODS == frozenset(
            {"eth_blockNumber", "eth_getBlockByNumber", "eth_getCode", "eth_call"}
        )
        assert ALLOWED_READONLY_RPC_METHODS.isdisjoint(FORBIDDEN_SEND_RPC_METHODS) == True

    def test_one_arm_happy_path_closes_exactly_zero(self):
        cfg = load_and_validate_prereg()
        runtime = ArmRuntime(cfg, "test-arm")
        runtime.begin_entry(
            d5_evidence=good_d5(),
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=10**15,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=123,
        )
        runtime.check_inclusion_timeout(30_000)
        runtime.complete_exit(remaining_token_wei=0)
        snapshot = runtime.snapshot()
        assert snapshot["roundtrips_started"] == 1
        assert snapshot["roundtrips_completed"] == 1
        assert snapshot["open_position_token_wei"] == "0"
        assert snapshot["stopped_for_human_review"] == True
        assert snapshot["killed"] == False

    def test_existing_position_and_second_roundtrip_are_rejected(self):
        cfg = load_and_validate_prereg()
        runtime = ArmRuntime(cfg, "test-open")
        runtime.begin_entry(
            d5_evidence=good_d5(),
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=10**15,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=123,
        )
        try:
            runtime.begin_entry(
                d5_evidence=good_d5(),
                prebroadcast_simulation_ok=True,
                intended_weth_in_wei=10**15,
                existing_onchain_token_balance_wei=0,
                simulated_token_out=123,
            )
            reason = None
        except GuardViolation as exc:
            reason = str(exc)
        assert reason == "OPEN_POSITION_EXISTS"

        closed = ArmRuntime(cfg, "test-closed")
        closed.begin_entry(
            d5_evidence=good_d5(),
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=10**15,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=123,
        )
        closed.complete_exit(remaining_token_wei=0)
        try:
            closed.begin_entry(
                d5_evidence=good_d5(),
                prebroadcast_simulation_ok=True,
                intended_weth_in_wei=10**15,
                existing_onchain_token_balance_wei=0,
                simulated_token_out=123,
            )
            second_reason = None
        except GuardViolation as exc:
            second_reason = str(exc)
        assert second_reason == "PER_ARM_ONE_ROUNDTRIP_EXHAUSTED"

    def test_every_kill_condition_is_runtime_enforced(self):
        cfg = load_and_validate_prereg()
        cases = []

        bad = good_d5()
        bad["sell_ok"] = False
        runtime = ArmRuntime(cfg, "d5")
        try:
            runtime.begin_entry(
                d5_evidence=bad,
                prebroadcast_simulation_ok=True,
                intended_weth_in_wei=10**15,
                existing_onchain_token_balance_wei=0,
                simulated_token_out=123,
            )
        except GuardViolation as exc:
            cases.append(str(exc))

        runtime = ArmRuntime(cfg, "preflight")
        try:
            runtime.begin_entry(
                d5_evidence=good_d5(),
                prebroadcast_simulation_ok=False,
                intended_weth_in_wei=10**15,
                existing_onchain_token_balance_wei=0,
                simulated_token_out=123,
            )
        except GuardViolation as exc:
            cases.append(str(exc))

        runtime = ArmRuntime(cfg, "wrong-notional")
        try:
            runtime.begin_entry(
                d5_evidence=good_d5(),
                prebroadcast_simulation_ok=True,
                intended_weth_in_wei=10**15 + 1,
                existing_onchain_token_balance_wei=0,
                simulated_token_out=123,
            )
        except GuardViolation as exc:
            cases.append(str(exc))

        runtime = ArmRuntime(cfg, "external-position")
        try:
            runtime.begin_entry(
                d5_evidence=good_d5(),
                prebroadcast_simulation_ok=True,
                intended_weth_in_wei=10**15,
                existing_onchain_token_balance_wei=1,
                simulated_token_out=123,
            )
        except GuardViolation as exc:
            cases.append(str(exc))

        runtime = ArmRuntime(cfg, "timeout")
        runtime.begin_entry(
            d5_evidence=good_d5(),
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=10**15,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=123,
        )
        try:
            runtime.check_inclusion_timeout(30_001)
        except GuardViolation as exc:
            cases.append(str(exc))

        runtime = ArmRuntime(cfg, "residual")
        runtime.begin_entry(
            d5_evidence=good_d5(),
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=10**15,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=123,
        )
        try:
            runtime.complete_exit(remaining_token_wei=1)
        except GuardViolation as exc:
            cases.append(str(exc))

        assert cases == [
            "SKIP_UNSELLABLE_D5_FAILED",
            "PREBROADCAST_ETH_CALL_FAILED",
            "NOTIONAL_MUST_EQUAL_0_001_WETH",
            "EXTERNAL_ONCHAIN_POSITION_EXISTS",
            "BROADCAST_TO_INCLUSION_TIMEOUT",
            "EXIT_POSITION_NOT_ZERO",
        ]

    def test_quote_pair_math_latency_and_frozen_flag_boundaries(self):
        cfg = load_and_validate_prereg()
        assert entry_slippage_pp(quoted_token_out=100, actual_token_out=100) == 0.0
        assert exit_slippage_pp(quoted_weth_out=100, actual_weth_out=100) == 0.0
        assert latency_ms(
            broadcast_monotonic_ns=1_000_000_000,
            inclusion_monotonic_ns=31_000_000_000,
        ) == 30_000
        assert observation_flags(
            entry_slippage=3.0,
            exit_slippage=-3.0,
            broadcast_to_inclusion_ms=30_000,
            gap_percentage_points=-5.0,
            unresolved=False,
            cfg=cfg,
        ) == []
        assert observation_flags(
            entry_slippage=3.0001,
            exit_slippage=None,
            broadcast_to_inclusion_ms=30_001,
            gap_percentage_points=-5.0001,
            unresolved=True,
            cfg=cfg,
        ) == [
            "QUOTE_UNRELIABLE_OVER_3_PERCENT",
            "INCLUSION_OUTSIDE_30_SECOND_FRONTIER",
            "EXECUTION_GAP_BELOW_MINUS_5PP",
            "SHALLOW_POOL_UNRESOLVED_PIPELINE_BUG",
        ]

    def test_flatten_is_intent_only_and_requires_presimulation(self):
        runtime = ArmRuntime(load_and_validate_prereg(), "flatten")
        runtime.open_position_token_wei = 123
        intent = runtime.flatten_intent()
        assert intent["action"] == "FLATTEN_ALL_TARGET_TO_WETH"
        assert intent["amount_token_wei"] == "123"
        assert intent["requires_exact_eth_call_before_human_broadcast"] == True
        assert intent["broadcast_in_this_delivery"] == False

    def test_pinned_archive_dryrun_golden_and_zero_send(self):
        result = json.loads(
            (ROOT / "mvp/diagnostics/base_rm1_v2_dryrun_20260715.json").read_text()
        )
        fixture = GOLDEN["shallow_archive_fixture"]
        chain = result["chain_evidence"]
        assert result["preregistration_sha256"] == GOLDEN["preregistration_sha256"]
        assert result["broadcast_performed"] == False
        assert result["signing_performed"] == False
        assert result["real_transaction_hashes"] == []
        assert chain["entry_block"]["number"] == fixture["entry_block"]
        assert chain["entry_block"]["hash"] == fixture["entry_block_hash"]
        assert chain["pool_weth_at_entry_wei"] == fixture["pool_weth_at_entry_wei"]
        assert chain["d5"]["received_target"] == fixture["expected_d5_received_target"]
        assert chain["d5"]["recovered_weth"] == fixture["expected_d5_recovered_weth"]
        assert chain["d5"]["flags"] == 15
        assert chain["d5"]["buy_integer_equal"] == True
        assert chain["d5"]["sell_integer_equal"] == True
        assert result["pipeline_proof"]["all_kill_switches_triggered"] == True
        assert result["pipeline_proof"]["broadcast_rejection"]["rpc_send_calls"] == 0
        assert result["rpc"]["all_methods_read_only"] == True
