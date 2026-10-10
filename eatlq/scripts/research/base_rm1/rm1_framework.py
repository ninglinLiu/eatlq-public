#!/usr/bin/env python3
"""RM1 v2 one-arm/one-roundtrip guardrail state machine.

This module deliberately has no signer and no JSON-RPC send method.  It can
validate simulations, record evidence, and produce a flatten intent, but the
only broadcaster shipped in RM1 v2 is fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path("/opt/EATLQ")
PREREG = ROOT / "configs/research/base_rm1_v2_preregistration_20260715.json"
ALLOWED_READONLY_RPC_METHODS = frozenset(
    {"eth_blockNumber", "eth_getBlockByNumber", "eth_getCode", "eth_call"}
)
FORBIDDEN_SEND_RPC_METHODS = frozenset(
    {"eth_sendTransaction", "eth_sendRawTransaction", "personal_sendTransaction"}
)


class GuardViolation(RuntimeError):
    """A runtime hard guard rejected the requested transition."""


class BroadcastDisabled(GuardViolation):
    """The research delivery has no enabled broadcaster."""


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_validate_prereg(path: Path = PREREG) -> dict[str, Any]:
    cfg = json.loads(path.read_text())
    posture = cfg["posture"]
    if posture["live_broadcast"] != "DISABLED":
        raise GuardViolation("LIVE_BROADCAST_NOT_DISABLED")
    if posture["live_allowed"] is not False:
        raise GuardViolation("LIVE_ALLOWED_MUST_EQUAL_FALSE")
    if posture["self_go_allowed"] is not False:
        raise GuardViolation("SELF_GO_ALLOWED_MUST_EQUAL_FALSE")
    if posture["signing_allowed"] is not False:
        raise GuardViolation("SIGNING_MUST_EQUAL_FALSE")
    if posture["send_rpc_allowed"] is not False:
        raise GuardViolation("SEND_RPC_MUST_EQUAL_FALSE")
    if posture["per_arm_roundtrips"] != 1:
        raise GuardViolation("PER_ARM_MUST_EQUAL_ONE")
    if cfg["chain"]["notional_weth_wei"] != "1000000000000000":
        raise GuardViolation("NOTIONAL_MUST_EQUAL_0_001_WETH")

    for source in cfg["source_anchors"].values():
        source_path = ROOT / source["path"]
        if file_sha256(source_path) != source["sha256"]:
            raise GuardViolation(f"SOURCE_HASH_MISMATCH:{source['path']}")
    return cfg


def d5_passed(evidence: dict[str, Any], cfg: dict[str, Any]) -> bool:
    gate = cfg["execution"]["d5"]
    return bool(
        evidence.get("status") == "measured"
        and evidence.get("buy_ok") is gate["buy_ok_required"]
        and evidence.get("sell_ok") is gate["sell_ok_required"]
        and evidence.get("buy_integer_equal") is gate["router_integer_equality_required"]
        and evidence.get("sell_integer_equal") is gate["router_integer_equality_required"]
        and float(evidence.get("recovery_ratio", 0.0))
        > float(gate["minimum_recovery_ratio_exclusive"])
    )


def keystone_exit_reason(
    *, entry_price: float, peak_price: float, current_price: float, blocks_held: int
) -> str | None:
    if min(entry_price, peak_price, current_price) <= 0:
        raise ValueError("prices must be positive")
    if blocks_held < 0:
        raise ValueError("blocks_held must be non-negative")
    if current_price / entry_price - 1.0 <= -0.5:
        return "HARD_STOP_MINUS_50_PERCENT"
    if current_price / peak_price - 1.0 <= -0.35:
        return "TRAIL_35_PERCENT_FROM_PEAK"
    if blocks_held >= 7200:
        return "MAX_HOLD_7200_BLOCKS"
    return None


def shakedown_exit_due(*, blocks_held: int, hold_blocks: int) -> bool:
    if hold_blocks < 1 or hold_blocks > 7200:
        raise ValueError("SHAKEDOWN hold_blocks must be in [1, 7200]")
    return blocks_held >= hold_blocks


def entry_slippage_pp(*, quoted_token_out: int, actual_token_out: int) -> float:
    if quoted_token_out <= 0 or actual_token_out <= 0:
        raise ValueError("amounts must be positive")
    return (quoted_token_out / actual_token_out - 1.0) * 100.0


def exit_slippage_pp(*, quoted_weth_out: int, actual_weth_out: int) -> float:
    if quoted_weth_out <= 0 or actual_weth_out <= 0:
        raise ValueError("amounts must be positive")
    return (actual_weth_out / quoted_weth_out - 1.0) * 100.0


def latency_ms(*, broadcast_monotonic_ns: int, inclusion_monotonic_ns: int) -> int:
    if inclusion_monotonic_ns < broadcast_monotonic_ns:
        raise ValueError("inclusion precedes broadcast")
    return (inclusion_monotonic_ns - broadcast_monotonic_ns) // 1_000_000


def observation_flags(
    *,
    entry_slippage: float | None,
    exit_slippage: float | None,
    broadcast_to_inclusion_ms: int | None,
    gap_percentage_points: float | None,
    unresolved: bool,
    cfg: dict[str, Any],
) -> list[str]:
    t = cfg["per_trade_observation_flags"]
    flags: list[str] = []
    if any(
        value is not None
        and abs(value) > float(t["absolute_entry_or_exit_slippage_gt_percent"])
        for value in (entry_slippage, exit_slippage)
    ):
        flags.append("QUOTE_UNRELIABLE_OVER_3_PERCENT")
    if (
        broadcast_to_inclusion_ms is not None
        and broadcast_to_inclusion_ms
        > int(t["broadcast_to_inclusion_latency_gt_seconds"]) * 1000
    ):
        flags.append("INCLUSION_OUTSIDE_30_SECOND_FRONTIER")
    if gap_percentage_points is not None and gap_percentage_points < float(
        t["gap_lt_percentage_points"]
    ):
        flags.append("EXECUTION_GAP_BELOW_MINUS_5PP")
    if unresolved:
        flags.append("SHALLOW_POOL_UNRESOLVED_PIPELINE_BUG")
    return flags


@dataclass
class DisabledBroadcaster:
    attempted_calls: int = 0

    def submit(self, unsigned_transaction: dict[str, Any]) -> str:
        del unsigned_transaction
        self.attempted_calls += 1
        raise BroadcastDisabled("LIVE_BROADCAST_DISABLED_NO_SEND_PATH")


@dataclass
class ArmRuntime:
    cfg: dict[str, Any]
    arm_id: str
    roundtrips_started: int = 0
    roundtrips_completed: int = 0
    open_position_token_wei: int = 0
    killed: bool = False
    kill_reason: str | None = None
    stopped_for_human_review: bool = False
    flatten_required: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def _event(self, name: str, **details: Any) -> None:
        self.events.append({"index": len(self.events) + 1, "event": name, **details})

    def kill(self, reason: str) -> None:
        self.killed = True
        self.kill_reason = reason
        self.flatten_required = self.open_position_token_wei > 0
        self._event("KILL_SWITCH", reason=reason, flatten_required=self.flatten_required)

    def _reject(self, reason: str) -> None:
        self.kill(reason)
        raise GuardViolation(reason)

    def begin_entry(
        self,
        *,
        d5_evidence: dict[str, Any],
        prebroadcast_simulation_ok: bool,
        intended_weth_in_wei: int,
        existing_onchain_token_balance_wei: int,
        simulated_token_out: int,
    ) -> None:
        if self.killed:
            raise GuardViolation(f"ARM_ALREADY_KILLED:{self.kill_reason}")
        if self.open_position_token_wei != 0:
            self._reject("OPEN_POSITION_EXISTS")
        if existing_onchain_token_balance_wei != 0:
            self._reject("EXTERNAL_ONCHAIN_POSITION_EXISTS")
        if self.roundtrips_started != 0:
            self._reject("PER_ARM_ONE_ROUNDTRIP_EXHAUSTED")
        if intended_weth_in_wei != int(self.cfg["chain"]["notional_weth_wei"]):
            self._reject("NOTIONAL_MUST_EQUAL_0_001_WETH")
        if not d5_passed(d5_evidence, self.cfg):
            self._reject("SKIP_UNSELLABLE_D5_FAILED")
        if prebroadcast_simulation_ok is not True:
            self._reject("PREBROADCAST_ETH_CALL_FAILED")
        if simulated_token_out <= 0:
            self._reject("SIMULATED_ENTRY_OUTPUT_NOT_POSITIVE")
        self.roundtrips_started = 1
        self.open_position_token_wei = simulated_token_out
        self._event("VIRTUAL_ENTRY_OPENED", simulated_token_out=str(simulated_token_out))

    def check_inclusion_timeout(self, elapsed_ms: int) -> None:
        threshold = int(
            self.cfg["runtime_kill_switches"]["broadcast_to_inclusion_timeout_seconds"]
        ) * 1000
        if elapsed_ms > threshold:
            self._reject("BROADCAST_TO_INCLUSION_TIMEOUT")
        self._event("INCLUSION_WITHIN_TIMEOUT", elapsed_ms=elapsed_ms)

    def complete_exit(self, *, remaining_token_wei: int) -> None:
        if self.open_position_token_wei <= 0:
            self._reject("NO_OPEN_POSITION_TO_EXIT")
        if remaining_token_wei != 0:
            self.open_position_token_wei = remaining_token_wei
            self._reject("EXIT_POSITION_NOT_ZERO")
        self.open_position_token_wei = 0
        self.roundtrips_completed = 1
        self.stopped_for_human_review = True
        self._event("ROUNDTRIP_CLOSED_STOP_FOR_HUMAN")

    def flatten_intent(self) -> dict[str, Any]:
        intent = {
            "arm_id": self.arm_id,
            "action": "FLATTEN_ALL_TARGET_TO_WETH",
            "amount_token_wei": str(self.open_position_token_wei),
            "requires_exact_eth_call_before_human_broadcast": True,
            "broadcast_in_this_delivery": False,
        }
        self._event("FLATTEN_INTENT_CREATED", amount_token_wei=intent["amount_token_wei"])
        return intent

    def snapshot(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "roundtrips_started": self.roundtrips_started,
            "roundtrips_completed": self.roundtrips_completed,
            "open_position_token_wei": str(self.open_position_token_wei),
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "flatten_required": self.flatten_required,
            "stopped_for_human_review": self.stopped_for_human_review,
            "events": self.events,
        }


def source_constant_proof(cfg: dict[str, Any]) -> dict[str, Any]:
    source = cfg["source_anchors"]["keystone_exit_source"]
    return {
        "source_path": source["path"],
        "source_sha256": source["sha256"],
        "source_lines": source["lines"],
        "hard_stop_log": math.log(0.5),
        "hard_stop_fraction": -0.5,
        "trailing_fraction": source["trailing_drawdown_fraction"],
        "maximum_hold_blocks": source["maximum_hold_blocks"],
    }
