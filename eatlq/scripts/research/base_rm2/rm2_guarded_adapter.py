#!/usr/bin/env python3
"""Human-only RM2 one-shot broadcast adapter.

The module contains a real send implementation, but it is unreachable unless
every runtime guard passes with an external arm file, an external secret
source, a matching ARM_TRADE_ID, and the exact human confirmation arguments.
RM2 itself only runs ``disabled-proof`` and never calls execute/flatten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


ROOT = Path("/opt/EATLQ").resolve()
RM1_PREREG = ROOT / "configs/research/base_rm1_v2_preregistration_20260715.json"
RM1_PREREG_SHA256 = "8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0"
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ROUTER = "0x2626664c2603336e57b271c5c0b26f421741e481"
NOTIONAL = 10**15
HUMAN_CONFIRMATION = "HUMAN_EXPLICIT"
REVIEW_APPROVAL = "HUMAN_REVIEW_AGENT_APPROVED"
ALLOWED_RPC = frozenset({"eth_call", "eth_getTransactionReceipt", "eth_blockNumber"})
SEND_RPC = "eth_sendRawTransaction"
EXACT_INPUT_SINGLE_SELECTOR = "0x04e45aaf"
APPROVE_SELECTOR = "0x095ea7b3"
D5_CREATION_BYTECODE_LEN = 4276
D5_CREATION_BYTECODE_SHA256 = "1bd35ad11b3059683d111cd23aa9d89f91d544d3344e5978783e626b09639d37"
MAX_SIMULATION_SLIPPAGE_PERCENT = 3
RPC_HOST = "prettiest-blue-aura.base-mainnet.quiknode.pro"


class GuardRejected(RuntimeError):
    """A required runtime equality or safety condition failed."""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reject(reason: str) -> None:
    raise GuardRejected(reason)


def outside_repo(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        reject(f"{label}_MUST_BE_ABSOLUTE")
    resolved = expanded.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        reject(f"{label}_MUST_BE_OUTSIDE_REPOSITORY")
    return resolved


def secure_external_file(path: Path, label: str) -> Path:
    resolved = outside_repo(path, label)
    if not resolved.is_file():
        reject(f"{label}_MISSING")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        reject(f"{label}_MUST_NOT_BE_GROUP_OR_WORLD_ACCESSIBLE")
    return resolved


def load_rm1_contract() -> dict[str, Any]:
    if sha(RM1_PREREG) != RM1_PREREG_SHA256:
        reject("RM1_PREREGISTRATION_HASH_MISMATCH")
    cfg = json.loads(RM1_PREREG.read_text())
    posture = cfg["posture"]
    if not (
        posture["live_broadcast"] == "DISABLED"
        and posture["live_allowed"] is False
        and posture["self_go_allowed"] is False
        and posture["per_arm_roundtrips"] == 1
    ):
        reject("RM1_FAIL_CLOSED_POSTURE_DRIFT")
    k = cfg["runtime_kill_switches"]
    expected = {
        "d5_failed": True,
        "prebroadcast_simulation_failed": True,
        "broadcast_to_inclusion_timeout_seconds": 30,
        "remaining_position_after_exit_must_equal_wei": "0",
        "reject_entry_when_open_position_exists": True,
        "reject_second_roundtrip_per_arm": True,
        "each_trigger_stops_new_entries": True,
        "flatten_intent_on_trigger_when_position_open": True,
    }
    if k != expected:
        reject("RM1_KILL_SWITCH_CONTRACT_DRIFT")
    if cfg["chain"]["chain_id"] != CHAIN_ID or cfg["chain"]["weth_address"].lower() != WETH:
        reject("RM1_CHAIN_CONTRACT_DRIFT")
    if int(cfg["chain"]["notional_weth_wei"]) != NOTIONAL:
        reject("RM1_NOTIONAL_CONTRACT_DRIFT")
    if [x.lower() for x in cfg["execution"]["router_whitelist"]] != [ROUTER]:
        reject("RM1_ROUTER_CONTRACT_DRIFT")
    for source in cfg["source_anchors"].values():
        path = ROOT / source["path"]
        if not path.is_file() or sha(path) != source["sha256"]:
            reject(f"RM1_SOURCE_ANCHOR_MISMATCH:{source['path']}")
    return cfg


def decode_d5(raw: str) -> dict[str, Any]:
    if not raw or not raw.startswith("0x") or len(raw) < 2 + 16 * 64:
        reject("D5_RESPONSE_MALFORMED")
    words = [int(raw[2+i*64:2+(i+1)*64], 16) for i in range(16)]
    flags = words[0]
    received = words[3] - words[2]
    buy_ok = bool(flags & 2) and received > 0
    sell_ok = bool(flags & 8) and words[8] > words[7]
    recovered = words[8] - words[7] if sell_ok else 0
    return {
        "status": "measured", "flags": flags, "buy_ok": buy_ok, "sell_ok": sell_ok,
        "buy_integer_equal": buy_ok and words[1] == received,
        "sell_integer_equal": sell_ok and words[5] == recovered,
        "received_target": received, "recovered_weth": recovered,
        "recovery_ratio": recovered / NOTIONAL if sell_ok else 0.0,
    }


def d5_green(evidence: dict[str, Any], cfg: dict[str, Any]) -> bool:
    gate = cfg["execution"]["d5"]
    return bool(
        evidence.get("status") == "measured"
        and evidence.get("buy_ok") is True
        and evidence.get("sell_ok") is True
        and evidence.get("buy_integer_equal") is True
        and evidence.get("sell_integer_equal") is True
        and float(evidence.get("recovery_ratio", 0)) > float(gate["minimum_recovery_ratio_exclusive"])
    )


def validate_tx(tx: dict[str, Any], *, wallet: str, require_value: int, expected_to: str = ROUTER) -> None:
    required = {"from", "to", "data", "value", "gas", "nonce", "chainId"}
    if not required.issubset(tx):
        reject("UNSIGNED_TRANSACTION_FIELDS_MISSING")
    if str(tx["from"]).lower() != wallet.lower():
        reject("UNSIGNED_TRANSACTION_FROM_MISMATCH")
    if str(tx["to"]).lower() != expected_to.lower():
        reject("ROUTER_NOT_WHITELISTED")
    if int(tx["value"]) != require_value:
        reject("TRANSACTION_VALUE_MISMATCH")
    if int(tx["chainId"]) != CHAIN_ID:
        reject("TRANSACTION_CHAIN_ID_MISMATCH")
    if not isinstance(tx["data"], str) or not tx["data"].startswith("0x"):
        reject("TRANSACTION_CALLDATA_INVALID")


def decode_exact_input_single(data: str) -> dict[str, Any]:
    if not data.startswith(EXACT_INPUT_SINGLE_SELECTOR):
        reject("ONLY_SWAPROUTER02_EXACT_INPUT_SINGLE_ALLOWED")
    body = data[10:]
    words = [body[i:i+64] for i in range(0, len(body), 64) if len(body[i:i+64]) == 64]
    if len(words) != 7:
        reject("EXACT_INPUT_SINGLE_CALLDATA_LENGTH_INVALID")
    return {
        "token_in": "0x" + words[0][-40:], "token_out": "0x" + words[1][-40:],
        "fee": int(words[2], 16), "recipient": "0x" + words[3][-40:],
        "amount_in": int(words[4], 16), "amount_out_minimum": int(words[5], 16),
        "sqrt_price_limit_x96": int(words[6], 16),
    }


def validate_swap_calldata(tx: dict[str, Any], *, wallet: str, token_in: str, token_out: str, amount_in: int) -> dict[str, Any]:
    decoded = decode_exact_input_single(str(tx["data"]))
    if decoded["token_in"].lower() != token_in.lower() or decoded["token_out"].lower() != token_out.lower():
        reject("SWAP_TOKEN_DIRECTION_MISMATCH")
    if decoded["recipient"].lower() != wallet.lower():
        reject("SWAP_RECIPIENT_MUST_EQUAL_ARM_WALLET")
    if decoded["amount_in"] != amount_in:
        reject("SWAP_CALLDATA_AMOUNT_IN_MISMATCH")
    if decoded["amount_out_minimum"] <= 0:
        reject("SWAP_MINIMUM_OUTPUT_MUST_BE_POSITIVE")
    if decoded["fee"] not in {100, 500, 3000, 10000}:
        reject("UNISWAP_V3_FEE_NOT_ALLOWED")
    return decoded


def validate_d5_call(call: dict[str, Any], *, block: Any, wallet: str, target: str, fee: int, amount_in: int) -> None:
    if block != "latest":
        reject("D5_BLOCK_MUST_EQUAL_LATEST")
    if "to" in call and call["to"] not in (None, "", "0x"):
        reject("D5_MUST_BE_DEPLOYLESS_ATOMIC_CALL")
    if str(call.get("from", "")).lower() != wallet.lower():
        reject("D5_FROM_MUST_EQUAL_ARM_WALLET")
    if int(call.get("value", -1)) != NOTIONAL:
        reject("D5_VALUE_MUST_EQUAL_0_001_WETH")
    data = str(call.get("data", ""))
    if not data.startswith("0x"):
        reject("D5_CALLDATA_INVALID")
    try:
        raw = bytes.fromhex(data[2:])
    except ValueError:
        reject("D5_CALLDATA_INVALID")
    if len(raw) != D5_CREATION_BYTECODE_LEN + 96:
        reject("D5_INITCODE_OR_ARGUMENT_LENGTH_MISMATCH")
    creation, arguments = raw[:D5_CREATION_BYTECODE_LEN], raw[D5_CREATION_BYTECODE_LEN:]
    if hashlib.sha256(creation).hexdigest() != D5_CREATION_BYTECODE_SHA256:
        reject("D5_ATOMIC_ROUNDTRIP_INITCODE_HASH_MISMATCH")
    target_word, fee_word, amount_word = arguments[:32], arguments[32:64], arguments[64:96]
    if int.from_bytes(target_word, "big") >> 160:
        reject("D5_TARGET_ARGUMENT_NONCANONICAL")
    if ("0x" + target_word[-20:].hex()).lower() != target.lower():
        reject("D5_TARGET_ARGUMENT_MISMATCH")
    if int.from_bytes(fee_word, "big") != fee:
        reject("D5_FEE_ARGUMENT_MISMATCH")
    if int.from_bytes(amount_word, "big") != amount_in:
        reject("D5_AMOUNT_ARGUMENT_MISMATCH")


def enforce_simulated_slippage_floor(*, simulated_out: int, minimum_out: int, label: str) -> None:
    if simulated_out <= 0 or minimum_out <= 0:
        reject(f"{label}_SIMULATED_OR_MINIMUM_OUTPUT_NONPOSITIVE")
    if minimum_out * 100 < simulated_out * (100 - MAX_SIMULATION_SLIPPAGE_PERCENT):
        reject(f"{label}_MINIMUM_OUTPUT_BELOW_3_PERCENT_SIMULATION_FLOOR")


def build_exact_approval_tx(template: dict[str, Any], *, wallet: str, target: str, amount: int) -> dict[str, Any]:
    validate_tx(template, wallet=wallet, require_value=0, expected_to=target)
    zero_template = APPROVE_SELECTOR + ROUTER.removeprefix("0x").rjust(64, "0") + f"{0:064x}"
    if str(template["data"]).lower() != zero_template.lower():
        reject("APPROVAL_TEMPLATE_MUST_HAVE_ROUTER_AND_ZERO_AMOUNT")
    exact = dict(template)
    exact["data"] = APPROVE_SELECTOR + ROUTER.removeprefix("0x").rjust(64, "0") + f"{amount:064x}"
    return exact


def build_exact_flatten_tx(
    template: dict[str, Any], *, wallet: str, target: str, amount_in: int, minimum_out: int,
) -> dict[str, Any]:
    validate_tx(template, wallet=wallet, require_value=0)
    decoded = decode_exact_input_single(str(template["data"]))
    if decoded["token_in"].lower() != target.lower() or decoded["token_out"].lower() != WETH:
        reject("FLATTEN_TEMPLATE_TOKEN_DIRECTION_MISMATCH")
    if decoded["recipient"].lower() != wallet.lower():
        reject("SWAP_RECIPIENT_MUST_EQUAL_ARM_WALLET")
    if decoded["fee"] not in {100, 500, 3000, 10000}:
        reject("UNISWAP_V3_FEE_NOT_ALLOWED")
    if decoded["amount_in"] != 0 or decoded["amount_out_minimum"] != 0:
        reject("FLATTEN_TEMPLATE_AMOUNTS_MUST_EQUAL_ZERO")
    body = str(template["data"])[10:]
    words = [body[index:index+64] for index in range(0, len(body), 64)]
    words[4] = f"{amount_in:064x}"
    words[5] = f"{minimum_out:064x}"
    exact = dict(template)
    exact["data"] = EXACT_INPUT_SINGLE_SELECTOR + "".join(words)
    return exact


def positive_uint_result(raw: str) -> bool:
    return bool(raw and raw.startswith("0x") and len(raw) >= 66 and int(raw[2:66], 16) > 0)


def _forbidden_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if any(part in str(key).lower() for part in ("private_key", "secret", "mnemonic", "seed_phrase")):
                return True
            if _forbidden_secret_key(child):
                return True
    if isinstance(value, list):
        return any(_forbidden_secret_key(x) for x in value)
    return False


def load_external_arm(path: Path, required_adapter_sha256: str) -> dict[str, Any]:
    arm_path = secure_external_file(path, "ARM_FILE")
    arm = json.loads(arm_path.read_text())
    if _forbidden_secret_key(arm):
        reject("ARM_FILE_MUST_NOT_CONTAIN_SECRET_MATERIAL")
    required = {
        "schema_version", "trade_id", "preregistration_sha256", "adapter_sha256",
        "review_approval", "review_ticket", "wallet", "target_token", "notional_weth_wei",
        "max_roundtrips", "mode", "shakedown_n_blocks", "rpc_endpoint_alias", "state_file", "unsigned_entry_tx",
        "d5_eth_call", "unsigned_approval_tx_template", "unsigned_flatten_tx",
    }
    if not required.issubset(arm):
        reject("ARM_FILE_FIELDS_MISSING")
    if arm["schema_version"] != 1:
        reject("ARM_SCHEMA_VERSION_MISMATCH")
    if arm["preregistration_sha256"] != RM1_PREREG_SHA256:
        reject("ARM_PREREGISTRATION_HASH_MISMATCH")
    actual_adapter_sha = sha(Path(__file__).resolve())
    if not (arm["adapter_sha256"] == required_adapter_sha256 == actual_adapter_sha):
        reject("ADAPTER_HASH_NOT_EXACTLY_REVIEWED")
    if arm["review_approval"] != REVIEW_APPROVAL or not str(arm["review_ticket"]).strip():
        reject("INDEPENDENT_REVIEW_APPROVAL_MISSING")
    if arm["rpc_endpoint_alias"] != "prettiest-blue-aura":
        reject("RPC_ENDPOINT_ALIAS_MISMATCH")
    if int(arm["notional_weth_wei"]) != NOTIONAL:
        reject("ARM_NOTIONAL_MUST_EQUAL_0_001_WETH")
    if int(arm["max_roundtrips"]) != 1:
        reject("ARM_MAX_ROUNDTRIPS_MUST_EQUAL_ONE")
    if arm["mode"] not in {"KEYSTONE", "SHAKEDOWN"}:
        reject("ARM_MODE_INVALID")
    if arm["mode"] == "SHAKEDOWN":
        n = arm["shakedown_n_blocks"]
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 7200:
            reject("SHAKEDOWN_N_BLOCKS_MUST_BE_INTEGER_1_TO_7200")
    elif arm["shakedown_n_blocks"] is not None:
        reject("KEYSTONE_MUST_NOT_SET_SHAKEDOWN_N_BLOCKS")
    try:
        entry_nonce = int(arm["unsigned_entry_tx"]["nonce"])
        approval_nonce = int(arm["unsigned_approval_tx_template"]["nonce"])
        flatten_nonce = int(arm["unsigned_flatten_tx"]["transaction"]["nonce"])
    except (KeyError, TypeError, ValueError):
        reject("ARM_TRANSACTION_NONCE_FIELDS_INVALID")
    if not (approval_nonce == entry_nonce + 1 and flatten_nonce == approval_nonce + 1):
        reject("ARM_ENTRY_APPROVAL_FLATTEN_NONCES_MUST_BE_CONSECUTIVE")
    for key in ("wallet", "target_token"):
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(arm[key])):
            reject(f"ARM_{key.upper()}_INVALID")
    if str(arm["target_token"]).lower() == WETH:
        reject("TARGET_TOKEN_MUST_NOT_EQUAL_WETH")
    outside_repo(Path(arm["state_file"]), "ARM_STATE_FILE")
    return arm


@dataclass(frozen=True)
class EntryGuardInput:
    secret_available: bool
    environment_trade_id: str | None
    arm_trade_id: str
    live_confirmation: str | None
    max_roundtrips_argument: str
    notional_argument: str
    arm_notional: int
    state_entry_reserved_or_sent: bool
    state_open_position: int
    existing_onchain_position: int
    existing_target_allowance: int
    prebroadcast_simulation_ok: bool
    d5_evidence: dict[str, Any]
    simulated_token_out: int
    minimum_token_out: int


def enforce_entry_guards(g: EntryGuardInput, cfg: dict[str, Any]) -> None:
    if g.secret_available is not True:
        reject("EXTERNAL_PRIVATE_KEY_SOURCE_MISSING")
    if not g.environment_trade_id:
        reject("ARM_TRADE_ID_UNSET")
    if g.environment_trade_id != g.arm_trade_id:
        reject("ARM_TRADE_ID_MISMATCH")
    if g.live_confirmation != HUMAN_CONFIRMATION:
        reject("HUMAN_EXPLICIT_BROADCAST_CONFIRMATION_MISSING")
    if g.prebroadcast_simulation_ok is not True:
        reject("PREBROADCAST_ETH_CALL_FAILED")
    if not d5_green(g.d5_evidence, cfg):
        reject("SKIP_UNSELLABLE_D5_FAILED")
    if not (g.max_roundtrips_argument == "==1" and cfg["posture"]["per_arm_roundtrips"] == 1):
        reject("PER_ARM_MUST_EQUAL_ONE")
    if not (g.notional_argument == f"=={NOTIONAL}" and g.arm_notional == NOTIONAL):
        reject("NOTIONAL_MUST_EQUAL_0_001_WETH")
    if g.state_open_position != 0:
        reject("OPEN_POSITION_EXISTS")
    if g.existing_onchain_position != 0:
        reject("EXTERNAL_ONCHAIN_POSITION_EXISTS")
    if g.existing_target_allowance != 0:
        reject("STALE_TARGET_ALLOWANCE_EXISTS")
    if g.state_entry_reserved_or_sent:
        reject("PER_ARM_ONE_ROUNDTRIP_EXHAUSTED")
    if g.simulated_token_out <= 0:
        reject("SIMULATED_ENTRY_OUTPUT_NOT_POSITIVE")
    enforce_simulated_slippage_floor(
        simulated_out=g.simulated_token_out, minimum_out=g.minimum_token_out, label="ENTRY",
    )
    load_rm1_contract()  # repeat the immutable runtime contract at the last pre-send boundary


def enforce_inclusion_timeout(elapsed_ms: int) -> None:
    if elapsed_ms > 30_000:
        reject("BROADCAST_TO_INCLUSION_TIMEOUT")


def enforce_exit_zero(remaining_token_wei: int) -> None:
    if remaining_token_wei != 0:
        reject("EXIT_POSITION_NOT_ZERO")


def receipt_succeeded(receipt: Any) -> bool:
    try:
        status = receipt["status"]
    except (KeyError, TypeError):
        status = getattr(receipt, "status", None)
    return int(status) == 1 if status is not None else False


def enforce_flatten_guards(
    *, secret_available: bool, environment_trade_id: str | None, arm_trade_id: str,
    reject_new_entry_argument: str, simulate_exact_argument: str, target_after_argument: str,
    open_position: int, actual_balance: int, flatten_amount: int,
    exact_simulation_ok: bool, flatten_already_sent: bool,
    simulated_weth_out: int = 1, minimum_weth_out: int = 1,
) -> None:
    if secret_available is not True:
        reject("EXTERNAL_PRIVATE_KEY_SOURCE_MISSING")
    if not environment_trade_id:
        reject("ARM_TRADE_ID_UNSET")
    if environment_trade_id != arm_trade_id:
        reject("ARM_TRADE_ID_MISMATCH")
    if reject_new_entry_argument != "==true":
        reject("EMERGENCY_REJECT_NEW_ENTRY_MUST_EQUAL_TRUE")
    if simulate_exact_argument != "==true":
        reject("EMERGENCY_EXACT_SIMULATION_MUST_EQUAL_TRUE")
    if target_after_argument != "==0":
        reject("EMERGENCY_TARGET_BALANCE_AFTER_MUST_EQUAL_ZERO")
    if open_position <= 0 or actual_balance <= 0:
        reject("NO_OPEN_POSITION_TO_FLATTEN")
    if flatten_amount != actual_balance:
        reject("FLATTEN_AMOUNT_MUST_EQUAL_ACTUAL_BALANCE")
    if exact_simulation_ok is not True:
        reject("FLATTEN_PREBROADCAST_ETH_CALL_FAILED")
    enforce_simulated_slippage_floor(
        simulated_out=simulated_weth_out, minimum_out=minimum_weth_out, label="FLATTEN",
    )
    if flatten_already_sent:
        reject("FLATTEN_ALREADY_SENT")
    load_rm1_contract()


class SecretSource(Protocol):
    def available(self) -> bool: ...
    def load(self) -> bytearray: ...


class ExternalSecretSource:
    """Reads only a human-selected external file or indirect environment name."""

    def __init__(self) -> None:
        self.file_name = os.environ.get("RM2_PRIVATE_KEY_FILE", "").strip()
        self.env_name = os.environ.get("RM2_PRIVATE_KEY_ENV_NAME", "").strip()

    def available(self) -> bool:
        if bool(self.file_name) == bool(self.env_name):
            return False
        if self.file_name:
            try:
                secure_external_file(Path(self.file_name), "PRIVATE_KEY_FILE")
                return True
            except GuardRejected:
                return False
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{5,63}", self.env_name):
            return False
        if "EXECUTOR" in self.env_name or self.env_name in {"ARM_TRADE_ID", "RM2_PRIVATE_KEY_ENV_NAME"}:
            return False
        return bool(os.environ.get(self.env_name, "").strip())

    def load(self) -> bytearray:
        if not self.available():
            reject("EXTERNAL_PRIVATE_KEY_SOURCE_MISSING")
        if self.file_name:
            raw = secure_external_file(Path(self.file_name), "PRIVATE_KEY_FILE").read_bytes().strip()
        else:
            raw = os.environ[self.env_name].encode().strip()
        if len(raw) == 66 and raw[:2].lower() == b"0x":
            raw = raw[2:]
        if len(raw) != 64 or re.fullmatch(rb"[0-9a-fA-F]{64}", raw) is None:
            reject("PRIVATE_KEY_FORMAT_INVALID")
        decoded = bytearray.fromhex(raw.decode("ascii"))
        if len(decoded) != 32 or not any(decoded):
            wipe(decoded)
            reject("PRIVATE_KEY_FORMAT_INVALID")
        return decoded


class ArmStateStore:
    """Durable outside-repo one-entry reservation and flatten state."""

    def __init__(self, path: str) -> None:
        self.path = outside_repo(Path(path), "ARM_STATE_FILE")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"entry_reserved": False, "entry_tx_hash": None, "open_position_token_wei": 0, "flatten_tx_hash": None, "killed": False, "kill_reason": None}
        return json.loads(self.path.read_text())

    def load_bound(self, trade_id: str, *, allow_missing: bool) -> dict[str, Any]:
        if not self.path.exists():
            if not allow_missing:
                reject("ARM_STATE_FILE_MISSING")
            return self.load()
        state = self.load()
        if str(state.get("trade_id", "")) != trade_id:
            reject("ARM_STATE_TRADE_ID_MISMATCH")
        # The O_EXCL marker is the crash-safe authority. If the process died
        # after fsync(marker) but before state.update(), synthesize and persist
        # the fail-closed flags on the next bound read.
        if not bool(state.get("consumed")):
            pending_reason = None
            for suffix, reason in (
                (".approval-reserved", "APPROVAL_RESERVED_PENDING"),
                (".flatten-reserved", "FLATTEN_RESERVED_PENDING"),
            ):
                marker = Path(str(self.path) + suffix)
                if marker.exists():
                    row = json.loads(marker.read_text())
                    if str(row.get("trade_id", "")) != trade_id:
                        reject("RESERVATION_TRADE_ID_MISMATCH")
                    pending_reason = reason
            if pending_reason is not None and not (
                state.get("killed") is True
                and state.get("flatten_required") is True
                and state.get("reject_new_entry") is True
            ):
                self.update(
                    killed=True, flatten_required=True, reject_new_entry=True,
                    kill_reason=pending_reason,
                )
                state = self.load()
        return state

    def reserve_entry_once(self, trade_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"trade_id": trade_id, "entry_reserved": True, "entry_tx_hash": None, "open_position_token_wei": 0, "flatten_tx_hash": None, "killed": False, "kill_reason": None}
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            reject("PER_ARM_ONE_ROUNDTRIP_EXHAUSTED")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())

    def reserve_flatten_once(self, trade_id: str) -> None:
        """Fail closed before signing so a crash cannot replay a flatten."""
        reservation = Path(str(self.path) + ".flatten-reserved")
        payload = {"trade_id": trade_id, "flatten_reserved": True}
        try:
            fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            reject("FLATTEN_ALREADY_SENT_OR_RESERVED")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        self.update(
            flatten_reserved=True, reject_new_entry=True, killed=True,
            flatten_required=True, kill_reason="FLATTEN_RESERVED_PENDING",
        )

    def approval_reservation_exists(self, trade_id: str) -> bool:
        reservation = Path(str(self.path) + ".approval-reserved")
        if not reservation.exists():
            return False
        row = json.loads(reservation.read_text())
        if str(row.get("trade_id", "")) != trade_id:
            reject("APPROVAL_RESERVATION_TRADE_ID_MISMATCH")
        return True

    def reserve_approval_once(self, trade_id: str) -> None:
        reservation = Path(str(self.path) + ".approval-reserved")
        payload = {"trade_id": trade_id, "approval_reserved": True}
        try:
            fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            reject("APPROVAL_ALREADY_SENT_OR_RESERVED")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        self.update(
            approval_reserved=True, reject_new_entry=True, killed=True,
            flatten_required=True, kill_reason="APPROVAL_RESERVED_PENDING",
        )

    def update(self, **changes: Any) -> None:
        state = self.load(); state.update(changes)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class ChainIO:
    """The sole production signer/send boundary."""

    def __init__(self) -> None:
        url = os.environ.get("RM2_RPC_URL", "").strip()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host != RPC_HOST or "orbital" in url.lower():
            reject("RM2_RPC_MUST_BE_PRETTIEST_BLUE_AURA")
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 180}))
        if int(self.w3.eth.chain_id) != CHAIN_ID:
            reject("RPC_CHAIN_ID_MISMATCH")
        self.send_calls = 0
        self.real_tx_hashes: list[str] = []

    def eth_call(self, tx: dict[str, Any], block: str | int = "latest") -> str:
        result = self.w3.eth.call(tx, block_identifier=block)
        return "0x" + bytes(result).hex()

    def balance_of(self, token: str, wallet: str) -> int:
        data = "0x70a08231" + wallet.removeprefix("0x").rjust(64, "0")
        return int.from_bytes(self.w3.eth.call({"to": token, "data": data}), "big")

    def allowance(self, token: str, wallet: str, spender: str = ROUTER) -> int:
        data = "0xdd62ed3e" + wallet.removeprefix("0x").rjust(64, "0") + spender.removeprefix("0x").rjust(64, "0")
        return int.from_bytes(self.w3.eth.call({"to": token, "data": data}), "big")

    def block_number(self) -> int:
        return int(self.w3.eth.block_number)

    def address_from_secret(self, secret: bytearray) -> str:
        return self.w3.eth.account.from_key(bytes(secret)).address

    def sign_and_send(self, tx: dict[str, Any], secret: bytearray) -> str:
        unsigned = {k: v for k, v in tx.items() if k != "from"}
        signed = self.w3.eth.account.sign_transaction(unsigned, bytes(secret))
        self.send_calls += 1
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        self.real_tx_hashes.append(tx_hash)
        return tx_hash

    def wait_receipt(self, tx_hash: str, timeout: int = 30) -> Any:
        return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=0.5)


def wipe(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0


def enforce_human_authority(*, secret_available: bool, arm_trade_id: str, live_confirmation: str | None = None) -> None:
    if secret_available is not True:
        reject("EXTERNAL_PRIVATE_KEY_SOURCE_MISSING")
    environment_trade_id = os.environ.get("ARM_TRADE_ID")
    if not environment_trade_id:
        reject("ARM_TRADE_ID_UNSET")
    if environment_trade_id != arm_trade_id:
        reject("ARM_TRADE_ID_MISMATCH")
    if live_confirmation is not None and live_confirmation != HUMAN_CONFIRMATION:
        reject("HUMAN_EXPLICIT_BROADCAST_CONFIRMATION_MISSING")


def execute_entry(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_rm1_contract()
    arm = load_external_arm(Path(args.arm_file), args.require_adapter_sha256)
    secret_source = ExternalSecretSource()
    state = ArmStateStore(arm["state_file"])
    before = state.load_bound(str(arm["trade_id"]), allow_missing=True)
    # Authority checks run before constructing an RPC client or touching any
    # signer.  Missing human material therefore fails before simulation.
    enforce_human_authority(secret_available=secret_source.available(), arm_trade_id=str(arm["trade_id"]), live_confirmation=args.live_broadcast)
    if args.max_roundtrips != "==1": reject("PER_ARM_MUST_EQUAL_ONE")
    if args.notional_weth_wei != f"=={NOTIONAL}": reject("NOTIONAL_MUST_EQUAL_0_001_WETH")
    if bool(before.get("entry_reserved")): reject("PER_ARM_ONE_ROUNDTRIP_EXHAUSTED")
    if int(before.get("open_position_token_wei", 0)) != 0: reject("OPEN_POSITION_EXISTS")
    io = ChainIO()
    wallet = str(arm["wallet"])
    target = str(arm["target_token"])
    # SwapRouter02's WETH payment path wraps this exact native value; requiring
    # equality avoids a separate standing WETH allowance on the isolated arm.
    validate_tx(arm["unsigned_entry_tx"], wallet=wallet, require_value=NOTIONAL)
    entry_decoded = validate_swap_calldata(
        arm["unsigned_entry_tx"], wallet=wallet, token_in=WETH, token_out=target, amount_in=NOTIONAL,
    )
    d5_block = arm["d5_eth_call"].get("block")
    validate_d5_call(
        arm["d5_eth_call"]["transaction"], block=d5_block, wallet=wallet,
        target=target, fee=int(entry_decoded["fee"]), amount_in=NOTIONAL,
    )
    start_block = io.block_number()
    d5_raw = io.eth_call(arm["d5_eth_call"]["transaction"], "latest")
    evidence = decode_d5(d5_raw)
    try:
        simulation = io.eth_call(arm["unsigned_entry_tx"], "latest")
        simulation_ok = positive_uint_result(simulation)
        simulated_token_out = int(simulation[2:66], 16) if simulation_ok else 0
    except Exception:
        simulation_ok = False
        simulated_token_out = 0
    end_block = io.block_number()
    if end_block < start_block or end_block - start_block > 1:
        reject("D5_TO_ENTRY_SIMULATION_BLOCK_DRIFT_GT_ONE")
    existing = io.balance_of(target, wallet)
    existing_allowance = io.allowance(target, wallet)
    enforce_entry_guards(EntryGuardInput(
        secret_available=secret_source.available(), environment_trade_id=os.environ.get("ARM_TRADE_ID"),
        arm_trade_id=str(arm["trade_id"]), live_confirmation=args.live_broadcast,
        max_roundtrips_argument=args.max_roundtrips, notional_argument=args.notional_weth_wei,
        arm_notional=int(arm["notional_weth_wei"]), state_entry_reserved_or_sent=bool(before.get("entry_reserved")),
        state_open_position=int(before.get("open_position_token_wei", 0)), existing_onchain_position=existing,
        existing_target_allowance=existing_allowance,
        prebroadcast_simulation_ok=simulation_ok, d5_evidence=evidence,
        simulated_token_out=simulated_token_out, minimum_token_out=int(entry_decoded["amount_out_minimum"]),
    ), cfg)
    state.reserve_entry_once(str(arm["trade_id"]))
    secret = secret_source.load()
    try:
        if io.address_from_secret(secret).lower() != wallet.lower():
            state.update(killed=True, kill_reason="PRIVATE_KEY_WALLET_MISMATCH")
            reject("PRIVATE_KEY_WALLET_MISMATCH")
        started = time.monotonic_ns()
        tx_hash = io.sign_and_send(arm["unsigned_entry_tx"], secret)
    finally:
        wipe(secret)
    state.update(entry_tx_hash=tx_hash)
    try:
        receipt = io.wait_receipt(tx_hash, timeout=30)
    except Exception:
        state.update(killed=True, kill_reason="BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
        reject("BROADCAST_TO_INCLUSION_TIMEOUT")
    if not receipt_succeeded(receipt):
        state.update(killed=True, kill_reason="ENTRY_RECEIPT_STATUS_NOT_ONE")
        reject("ENTRY_RECEIPT_STATUS_NOT_ONE")
    elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
    try:
        enforce_inclusion_timeout(elapsed_ms)
    except GuardRejected:
        state.update(killed=True, kill_reason="BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
        raise
    actual = io.balance_of(target, wallet)
    if actual <= 0:
        state.update(killed=True, kill_reason="ENTRY_POSITION_NOT_OBSERVED")
        reject("ENTRY_POSITION_NOT_OBSERVED")
    state.update(open_position_token_wei=actual, inclusion_elapsed_ms=elapsed_ms)
    return {"trade_id": arm["trade_id"], "entry_tx_hash": tx_hash, "send_calls": io.send_calls, "status": "ENTRY_SENT_STOP_FOR_HUMAN_EXIT_RULE"}


def emergency_flatten(args: argparse.Namespace) -> dict[str, Any]:
    load_rm1_contract()
    arm = load_external_arm(Path(args.arm_file), args.require_adapter_sha256)
    secret_source = ExternalSecretSource(); state = ArmStateStore(arm["state_file"])
    before = state.load_bound(str(arm["trade_id"]), allow_missing=False)
    enforce_human_authority(secret_available=secret_source.available(), arm_trade_id=str(arm["trade_id"]))
    if args.reject_new_entry != "==true": reject("EMERGENCY_REJECT_NEW_ENTRY_MUST_EQUAL_TRUE")
    if args.simulate_exact_tx != "==true": reject("EMERGENCY_EXACT_SIMULATION_MUST_EQUAL_TRUE")
    if args.target_balance_after != "==0": reject("EMERGENCY_TARGET_BALANCE_AFTER_MUST_EQUAL_ZERO")
    if not bool(before.get("entry_reserved")): reject("ARM_ENTRY_NOT_RESERVED")
    if bool(before.get("flatten_tx_hash")): reject("FLATTEN_ALREADY_SENT")
    io = ChainIO(); wallet = str(arm["wallet"]); target = str(arm["target_token"])
    actual = io.balance_of(target, wallet)
    if actual <= 0: reject("NO_OPEN_POSITION_TO_FLATTEN")

    approval_template = arm["unsigned_approval_tx_template"]
    approval_tx = build_exact_approval_tx(approval_template, wallet=wallet, target=target, amount=actual)
    allowance = io.allowance(target, wallet)
    approval_tx_hash = before.get("approval_tx_hash")
    if state.approval_reservation_exists(str(arm["trade_id"])):
        if allowance < actual:
            reject("APPROVAL_RESERVED_BUT_ALLOWANCE_INSUFFICIENT_HUMAN_INTERVENTION")
        state.update(
            approval_recovered_from_chain=True, reject_new_entry=True, killed=True,
            flatten_required=True, kill_reason="APPROVAL_RECOVERED_FLATTEN_REQUIRED",
        )
    else:
        if allowance != 0:
            reject("STALE_TARGET_ALLOWANCE_EXISTS")
        try:
            io.eth_call(approval_tx, "latest")
        except Exception:
            reject("APPROVAL_PREBROADCAST_ETH_CALL_FAILED")
        state.reserve_approval_once(str(arm["trade_id"]))
        secret = secret_source.load()
        try:
            if io.address_from_secret(secret).lower() != wallet.lower():
                state.update(killed=True, kill_reason="PRIVATE_KEY_WALLET_MISMATCH")
                reject("PRIVATE_KEY_WALLET_MISMATCH")
            approval_started = time.monotonic_ns()
            approval_tx_hash = io.sign_and_send(approval_tx, secret)
        finally:
            wipe(secret)
        state.update(approval_tx_hash=approval_tx_hash)
        try:
            approval_receipt = io.wait_receipt(approval_tx_hash, timeout=30)
        except Exception:
            state.update(killed=True, kill_reason="APPROVAL_BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
            reject("APPROVAL_BROADCAST_TO_INCLUSION_TIMEOUT")
        if not receipt_succeeded(approval_receipt):
            state.update(killed=True, kill_reason="APPROVAL_RECEIPT_STATUS_NOT_ONE", flatten_required=True)
            reject("APPROVAL_RECEIPT_STATUS_NOT_ONE")
        try:
            enforce_inclusion_timeout((time.monotonic_ns() - approval_started) // 1_000_000)
        except GuardRejected:
            state.update(killed=True, kill_reason="APPROVAL_BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
            raise
        allowance = io.allowance(target, wallet)
        if allowance < actual:
            state.update(killed=True, kill_reason="APPROVAL_ALLOWANCE_INSUFFICIENT", flatten_required=True)
            reject("APPROVAL_ALLOWANCE_INSUFFICIENT")
        state.update(
            approval_confirmed=True, killed=True, flatten_required=True,
            kill_reason="APPROVAL_CONFIRMED_FLATTEN_REQUIRED",
        )

    flatten = arm["unsigned_flatten_tx"]
    if int(flatten.get("amount_token_wei", -1)) != 0:
        reject("FLATTEN_TEMPLATE_AMOUNT_FIELD_MUST_EQUAL_ZERO")
    probe_tx = build_exact_flatten_tx(
        flatten["transaction"], wallet=wallet, target=target, amount_in=actual, minimum_out=1,
    )
    try:
        probe = io.eth_call(probe_tx, "latest")
        probe_ok = positive_uint_result(probe)
        simulated_weth_out = int(probe[2:66], 16) if probe_ok else 0
    except Exception:
        probe_ok = False
        simulated_weth_out = 0
    minimum_weth_out = (simulated_weth_out * (100 - MAX_SIMULATION_SLIPPAGE_PERCENT) + 99) // 100
    exact_flatten_tx = build_exact_flatten_tx(
        flatten["transaction"], wallet=wallet, target=target,
        amount_in=actual, minimum_out=minimum_weth_out,
    )
    try:
        simulation = io.eth_call(exact_flatten_tx, "latest")
        simulation_ok = probe_ok and positive_uint_result(simulation)
    except Exception:
        simulation_ok = False
    enforce_flatten_guards(
        secret_available=secret_source.available(), environment_trade_id=os.environ.get("ARM_TRADE_ID"),
        arm_trade_id=str(arm["trade_id"]), reject_new_entry_argument=args.reject_new_entry,
        simulate_exact_argument=args.simulate_exact_tx, target_after_argument=args.target_balance_after,
        open_position=actual, actual_balance=actual,
        flatten_amount=actual, exact_simulation_ok=simulation_ok,
        flatten_already_sent=bool(before.get("flatten_tx_hash")),
        simulated_weth_out=simulated_weth_out, minimum_weth_out=minimum_weth_out,
    )
    state.reserve_flatten_once(str(arm["trade_id"]))
    secret = secret_source.load()
    try:
        if io.address_from_secret(secret).lower() != wallet.lower():
            state.update(killed=True, kill_reason="PRIVATE_KEY_WALLET_MISMATCH")
            reject("PRIVATE_KEY_WALLET_MISMATCH")
        started = time.monotonic_ns(); tx_hash = io.sign_and_send(exact_flatten_tx, secret)
    finally:
        wipe(secret)
    state.update(flatten_tx_hash=tx_hash, reject_new_entry=True)
    try:
        receipt = io.wait_receipt(tx_hash, timeout=30)
    except Exception:
        state.update(killed=True, kill_reason="BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
        reject("BROADCAST_TO_INCLUSION_TIMEOUT")
    if not receipt_succeeded(receipt):
        state.update(killed=True, kill_reason="FLATTEN_RECEIPT_STATUS_NOT_ONE", flatten_required=True)
        reject("FLATTEN_RECEIPT_STATUS_NOT_ONE")
    try:
        enforce_inclusion_timeout((time.monotonic_ns() - started) // 1_000_000)
    except GuardRejected:
        state.update(killed=True, kill_reason="BROADCAST_TO_INCLUSION_TIMEOUT", flatten_required=True)
        raise
    remaining = io.balance_of(target, wallet)
    try:
        enforce_exit_zero(remaining)
    except GuardRejected:
        state.update(killed=True, kill_reason="EXIT_POSITION_NOT_ZERO", open_position_token_wei=remaining, flatten_required=True)
        raise
    state.update(
        open_position_token_wei=0, roundtrip_completed=True, consumed=True,
        flatten_required=False, killed=False, kill_reason=None,
    )
    return {"trade_id": arm["trade_id"], "approval_tx_hash": approval_tx_hash, "flatten_tx_hash": tx_hash, "send_calls": io.send_calls, "status": "FLATTENED_ZERO_STOP_FOR_HUMAN_REVIEW"}


def good_d5_fixture() -> dict[str, Any]:
    return {"status": "measured", "buy_ok": True, "sell_ok": True, "buy_integer_equal": True, "sell_integer_equal": True, "recovery_ratio": .99, "received_target": 123}


def disabled_proof() -> dict[str, Any]:
    if os.environ.get("ARM_TRADE_ID"):
        reject("DISABLED_PROOF_REQUIRES_ARM_TRADE_ID_UNSET")
    if ExternalSecretSource().available():
        reject("DISABLED_PROOF_REQUIRES_NO_PRIVATE_KEY_SOURCE")
    cfg = load_rm1_contract(); base = dict(
        secret_available=True, environment_trade_id="RM2-PROOF", arm_trade_id="RM2-PROOF",
        live_confirmation=HUMAN_CONFIRMATION, max_roundtrips_argument="==1",
        notional_argument=f"=={NOTIONAL}", arm_notional=NOTIONAL,
        state_entry_reserved_or_sent=False, state_open_position=0, existing_onchain_position=0,
        existing_target_allowance=0,
        prebroadcast_simulation_ok=True, d5_evidence=good_d5_fixture(), simulated_token_out=123,
        minimum_token_out=120,
    )
    cases: list[tuple[str, str]] = []

    def entry_case(name: str, expected: str, **changes: Any) -> None:
        values = dict(base); values.update(changes)
        try:
            enforce_entry_guards(EntryGuardInput(**values), cfg)
            actual = "NO_REJECTION"
        except GuardRejected as exc:
            actual = str(exc)
        if actual != expected:
            raise AssertionError((name, expected, actual))
        cases.append((name, actual))

    entry_case("missing_private_key", "EXTERNAL_PRIVATE_KEY_SOURCE_MISSING", secret_available=False)
    entry_case("arm_trade_id_unset", "ARM_TRADE_ID_UNSET", environment_trade_id=None)
    entry_case("arm_trade_id_mismatch", "ARM_TRADE_ID_MISMATCH", environment_trade_id="OTHER")
    entry_case("human_confirmation_missing", "HUMAN_EXPLICIT_BROADCAST_CONFIRMATION_MISSING", live_confirmation=None)
    entry_case("prebroadcast_revert", "PREBROADCAST_ETH_CALL_FAILED", prebroadcast_simulation_ok=False)
    bad = good_d5_fixture(); bad["sell_ok"] = False
    entry_case("d5_failure", "SKIP_UNSELLABLE_D5_FAILED", d5_evidence=bad)
    entry_case("max_roundtrips_not_exact", "PER_ARM_MUST_EQUAL_ONE", max_roundtrips_argument="==2")
    entry_case("notional_cli_not_exact", "NOTIONAL_MUST_EQUAL_0_001_WETH", notional_argument=str(NOTIONAL))
    entry_case("wrong_notional", "NOTIONAL_MUST_EQUAL_0_001_WETH", arm_notional=NOTIONAL+1)
    entry_case("existing_open_position", "OPEN_POSITION_EXISTS", state_open_position=1)
    entry_case("external_onchain_position", "EXTERNAL_ONCHAIN_POSITION_EXISTS", existing_onchain_position=1)
    entry_case("stale_target_allowance", "STALE_TARGET_ALLOWANCE_EXISTS", existing_target_allowance=1)
    entry_case("arm_already_sent", "PER_ARM_ONE_ROUNDTRIP_EXHAUSTED", state_entry_reserved_or_sent=True)
    entry_case("simulated_output_nonpositive", "SIMULATED_ENTRY_OUTPUT_NOT_POSITIVE", simulated_token_out=0)
    entry_case("entry_minimum_below_slippage_floor", "ENTRY_MINIMUM_OUTPUT_BELOW_3_PERCENT_SIMULATION_FLOOR", minimum_token_out=119)
    for name, action, expected in (
        ("inclusion_timeout", lambda: enforce_inclusion_timeout(30_001), "BROADCAST_TO_INCLUSION_TIMEOUT"),
        ("nonzero_exit_balance", lambda: enforce_exit_zero(1), "EXIT_POSITION_NOT_ZERO"),
    ):
        try: action(); actual = "NO_REJECTION"
        except GuardRejected as exc: actual = str(exc)
        if actual != expected: raise AssertionError((name, expected, actual))
        cases.append((name, actual))
    flatten_base = dict(secret_available=True, environment_trade_id="RM2-PROOF", arm_trade_id="RM2-PROOF", reject_new_entry_argument="==true", simulate_exact_argument="==true", target_after_argument="==0", open_position=123, actual_balance=123, flatten_amount=123, exact_simulation_ok=True, flatten_already_sent=False)
    for name, expected, change in (
        ("flatten_missing_private_key", "EXTERNAL_PRIVATE_KEY_SOURCE_MISSING", {"secret_available": False}),
        ("flatten_arm_trade_id_unset", "ARM_TRADE_ID_UNSET", {"environment_trade_id": None}),
        ("flatten_arm_trade_id_mismatch", "ARM_TRADE_ID_MISMATCH", {"environment_trade_id": "OTHER"}),
        ("flatten_reject_new_entry_not_exact", "EMERGENCY_REJECT_NEW_ENTRY_MUST_EQUAL_TRUE", {"reject_new_entry_argument": "true"}),
        ("flatten_simulate_argument_not_exact", "EMERGENCY_EXACT_SIMULATION_MUST_EQUAL_TRUE", {"simulate_exact_argument": "true"}),
        ("flatten_zero_target_not_exact", "EMERGENCY_TARGET_BALANCE_AFTER_MUST_EQUAL_ZERO", {"target_after_argument": "0"}),
        ("flatten_requires_open_position", "NO_OPEN_POSITION_TO_FLATTEN", {"open_position": 0, "actual_balance": 0}),
        ("flatten_exact_simulation", "FLATTEN_PREBROADCAST_ETH_CALL_FAILED", {"exact_simulation_ok": False}),
        ("flatten_amount_exact", "FLATTEN_AMOUNT_MUST_EQUAL_ACTUAL_BALANCE", {"flatten_amount": 122}),
        ("flatten_minimum_below_slippage_floor", "FLATTEN_MINIMUM_OUTPUT_BELOW_3_PERCENT_SIMULATION_FLOOR", {"simulated_weth_out": 100, "minimum_weth_out": 96}),
        ("flatten_already_sent", "FLATTEN_ALREADY_SENT", {"flatten_already_sent": True}),
    ):
        values = dict(flatten_base); values.update(change)
        try: enforce_flatten_guards(**values); actual = "NO_REJECTION"
        except GuardRejected as exc: actual = str(exc)
        if actual != expected: raise AssertionError((name, expected, actual))
        cases.append((name, actual))
    return {
        "schema_version": 1, "alpha_id": "AR-RM2-CAPSTONE-ADAPTER", "research_only": True,
        "posture": {"ARM_TRADE_ID_present": False, "private_key_source_present": False, "sign_calls": 0, "send_calls": 0, "real_tx_hashes": [], "live_allowed": False, "self_go_allowed": False},
        "guard_rejections": [{"case": name, "rejected": True, "reason": reason} for name, reason in cases],
        "guard_n": len(cases), "all_rejected": True,
        "note": "Boolean fixtures exercise downstream guards without creating or loading secret material; no ChainIO instance is constructed.",
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="human-arm-adapter")
    p.add_argument("command", choices=("execute", "emergency-flatten", "disabled-proof"))
    p.add_argument("--arm-file")
    p.add_argument("--require-prereg-sha256")
    p.add_argument("--require-adapter-sha256")
    p.add_argument("--max-roundtrips")
    p.add_argument("--notional-weth-wei")
    p.add_argument("--live-broadcast")
    p.add_argument("--reject-new-entry")
    p.add_argument("--simulate-exact-tx")
    p.add_argument("--target-balance-after")
    p.add_argument("--output")
    return p


def require_cli(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    for name in names:
        if getattr(args, name) is None:
            reject(f"CLI_ARGUMENT_REQUIRED:{name}")
    if args.require_prereg_sha256 != RM1_PREREG_SHA256:
        reject("CLI_PREREGISTRATION_HASH_MISMATCH")


def main() -> None:
    args = parser().parse_args()
    if args.command == "disabled-proof":
        proof = disabled_proof()
        if not args.output:
            reject("DISABLED_PROOF_OUTPUT_REQUIRED")
        output = (ROOT / args.output).resolve()
        if ROOT not in output.parents:
            reject("DISABLED_PROOF_OUTPUT_MUST_BE_IN_REPOSITORY")
        output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"guard_n": proof["guard_n"], "all_rejected": True, "send_calls": 0, "real_tx_hashes": []}, sort_keys=True))
        return
    common = ("arm_file", "require_prereg_sha256", "require_adapter_sha256")
    if args.command == "emergency-flatten":
        require_cli(args, common + ("reject_new_entry", "simulate_exact_tx", "target_balance_after"))
        result = emergency_flatten(args)
    else:
        require_cli(args, common + ("max_roundtrips", "notional_weth_wei", "live_broadcast"))
        result = execute_entry(args)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
