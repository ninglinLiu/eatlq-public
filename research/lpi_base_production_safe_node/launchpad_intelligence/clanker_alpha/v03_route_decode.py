"""Offline decoders for captured Clanker-related swap routes."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from eth_abi import decode
from eth_hash.auto import keccak

from .v03_semantics import (
    ACTION_SETTLE_ALL,
    ACTION_SWAP_EXACT_IN_SINGLE,
    ACTION_TAKE_ALL,
    DYNAMIC_FEE_FLAG,
    UR_COMMAND_V4_SWAP,
)

UNIVERSAL_ROUTER_EXECUTE = "0x" + keccak(b"execute(bytes,bytes[],uint256)")[:4].hex()
ENTRYPOINT_HANDLE_OPS = "0x765e827f"
CLANKER_STYLE_ROUTER = "0xeffbec13"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


@dataclass
class MappedSwapAction:
    status: str
    top_level_selector: str
    to_address: str
    action: Optional[int]
    action_name: Optional[str]
    action_order: Optional[int]
    pool_key: Optional[dict[str, Any]]
    zero_for_one: Optional[bool]
    amount_in: Optional[int]
    amount_out_minimum: Optional[int]
    hook_data_hex: Optional[str]
    actions_bytes_hex: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    ambiguity: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_execute_slice(input_hex: str) -> Optional[str]:
    sel = UNIVERSAL_ROUTER_EXECUTE[2:]
    pos = input_hex.lower().find(sel)
    if pos < 0:
        return None
    return "0x" + input_hex[pos:]


def decode_v4_swap_command_input(raw: bytes) -> dict[str, Any]:
    actions, params = decode(["bytes", "bytes[]"], raw)
    rows = []
    for idx, action in enumerate(actions):
        item: dict[str, Any] = {
            "action_order": idx,
            "action": action,
            "action_name": {
                ACTION_SWAP_EXACT_IN_SINGLE: "SWAP_EXACT_IN_SINGLE",
                ACTION_SETTLE_ALL: "SETTLE_ALL",
                ACTION_TAKE_ALL: "TAKE_ALL",
            }.get(action, f"UNKNOWN_0x{action:02x}"),
            "param_sha256": _sha(params[idx]) if idx < len(params) else None,
        }
        if action == ACTION_SWAP_EXACT_IN_SINGLE and idx < len(params):
            try:
                (pool_key, zero_for_one, amount_in, amount_out_min, hook_data) = decode(
                    ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
                    params[idx],
                )[0]
                item["pool_key"] = {
                    "currency0": pool_key[0].lower(),
                    "currency1": pool_key[1].lower(),
                    "fee": int(pool_key[2]),
                    "tick_spacing": int(pool_key[3]),
                    "hooks": pool_key[4].lower(),
                    "fee_mode": "DYNAMIC_FLAG" if int(pool_key[2]) & DYNAMIC_FEE_FLAG else "STATIC",
                }
                item["zero_for_one"] = bool(zero_for_one)
                item["amount_in"] = int(amount_in)
                item["amount_out_minimum"] = int(amount_out_min)
                item["hook_data_hex"] = "0x" + hook_data.hex()
            except Exception as exc:  # noqa: BLE001
                item["decode_error"] = f"{type(exc).__name__}:{exc}"[:300]
        rows.append(item)
    return {"actions_hex": "0x" + actions.hex(), "rows": rows}


def decode_universal_router_execute(input_hex: str) -> dict[str, Any]:
    if not input_hex.lower().startswith(UNIVERSAL_ROUTER_EXECUTE):
        raise ValueError("not_universal_router_execute")
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(input_hex[10:]))
    command_rows = []
    for command, command_input in zip(commands, inputs):
        low6 = command & 0x3F
        row: dict[str, Any] = {
            "raw_command": command,
            "command_type_low6": low6,
            "input_sha256": _sha(command_input),
        }
        if low6 == UR_COMMAND_V4_SWAP:
            row["v4_swap"] = decode_v4_swap_command_input(command_input)
        command_rows.append(row)
    return {
        "commands_hex": "0x" + commands.hex(),
        "deadline": int(deadline),
        "command_rows": command_rows,
    }


def decode_effbec13_single_hop(input_hex: str) -> dict[str, Any]:
    """Best-effort parse of the observed 0xeffbec13 Clanker-style router template."""
    if not input_hex.lower().startswith(CLANKER_STYLE_ROUTER):
        raise ValueError("not_effbec13")
    raw = input_hex.lower()
    words = [raw[10 + i * 64 : 10 + (i + 1) * 64] for i in range((len(raw) - 10) // 64)]
    amount_in = int(words[2], 16) if len(words) > 2 else None
    amount_out_min = int(words[3], 16) if len(words) > 3 else None
    # Find path length word == 2 followed by two addresses, fee flag, spacing, hooks
    path = None
    pool = None
    for i in range(len(words) - 6):
        if int(words[i], 16) != 2:
            continue
        a0 = "0x" + words[i + 1][-40:]
        a1 = "0x" + words[i + 2][-40:]
        fee_word = int(words[i + 4], 16) if int(words[i + 3], 16) == 0 else int(words[i + 3], 16)
        # template: word after currencies is 0, then 0x800000, then 0xc8, then hooks
        if int(words[i + 3], 16) == 0 and int(words[i + 4], 16) in (DYNAMIC_FEE_FLAG, 0x800000):
            fee = DYNAMIC_FEE_FLAG
            spacing = int(words[i + 5], 16)
            hooks = "0x" + words[i + 6][-40:]
            path = [a0, a1]
            # canonical pool key sorts currencies
            c0, c1 = (a0, a1) if int(a0, 16) < int(a1, 16) else (a1, a0)
            pool = {
                "currency0": c0,
                "currency1": c1,
                "fee": fee,
                "tick_spacing": spacing,
                "hooks": hooks,
                "fee_mode": "DYNAMIC_FLAG",
                "path_order": path,
            }
            break
    if pool is None:
        return {
            "status": "AMBIGUOUS_OR_UNKNOWN_LAYOUT",
            "amount_in": amount_in,
            "amount_out_minimum": amount_out_min,
        }
    # Path tokenIn = path[0]; zeroForOne vs sorted key
    token_in = path[0]
    zero_for_one = token_in == pool["currency0"]
    return {
        "status": "DECODED_SINGLE_HOP",
        "amount_in": amount_in,
        "amount_out_minimum": amount_out_min,
        "pool_key": pool,
        "zero_for_one": zero_for_one,
        "hook_data_hex": None,
        "notes": ["effbec13_template_parse", "hookData_not_fully_typed"],
    }


def map_captured_transaction(tx: dict[str, Any]) -> MappedSwapAction:
    """Map one sealed captured transaction to a V4 swap action if unambiguous."""
    selector = (tx.get("top_level") or {}).get("selector") or (tx.get("input") or "")[:10]
    selector = selector.lower()
    to_address = (tx.get("to_address") or "").lower()
    input_hex = tx.get("input") or "0x"
    notes: list[str] = []

    # Path A: direct or nested Universal Router execute
    execute_slice = None
    if selector == UNIVERSAL_ROUTER_EXECUTE:
        execute_slice = input_hex
    else:
        execute_slice = _find_execute_slice(input_hex)
        if execute_slice:
            notes.append("execute_embedded_in_wrapper")

    if execute_slice is not None:
        try:
            ur = decode_universal_router_execute(execute_slice)
        except Exception as exc:  # noqa: BLE001
            return MappedSwapAction(
                "DECODE_FAIL", selector, to_address, None, None, None, None, None, None, None, None,
                notes=notes + [f"ur_decode:{exc}"], ambiguity="UR_DECODE_FAIL",
            )
        swaps = []
        for command in ur["command_rows"]:
            if command.get("command_type_low6") != UR_COMMAND_V4_SWAP:
                continue
            for row in command["v4_swap"]["rows"]:
                if row["action"] == ACTION_SWAP_EXACT_IN_SINGLE and "pool_key" in row:
                    swaps.append(row)
        if len(swaps) == 0:
            return MappedSwapAction(
                "NO_SWAP_EXACT_IN_SINGLE", selector, to_address, None, None, None, None, None, None, None,
                None, actions_bytes_hex=None, notes=notes + ["no_0x06_action"],
            )
        if len(swaps) > 1:
            return MappedSwapAction(
                "AMBIGUOUS_MULTI_LEG", selector, to_address, ACTION_SWAP_EXACT_IN_SINGLE,
                "SWAP_EXACT_IN_SINGLE", None, None, None, None, None, None,
                notes=notes + [f"swap_exact_in_single_count={len(swaps)}"],
                ambiguity="MULTIPLE_SWAP_EXACT_IN_SINGLE",
            )
        s0 = swaps[0]
        return MappedSwapAction(
            "MAPPED",
            selector,
            to_address,
            ACTION_SWAP_EXACT_IN_SINGLE,
            "SWAP_EXACT_IN_SINGLE",
            s0["action_order"],
            s0["pool_key"],
            s0["zero_for_one"],
            s0["amount_in"],
            s0["amount_out_minimum"],
            s0.get("hook_data_hex"),
            actions_bytes_hex=next(
                c["v4_swap"]["actions_hex"]
                for c in ur["command_rows"]
                if c.get("command_type_low6") == UR_COMMAND_V4_SWAP
            ),
            notes=notes + ["entrypoint_or_ur_path"],
        )

    if selector == CLANKER_STYLE_ROUTER:
        parsed = decode_effbec13_single_hop(input_hex)
        if parsed.get("status") != "DECODED_SINGLE_HOP":
            return MappedSwapAction(
                "AMBIGUOUS_OR_UNKNOWN_LAYOUT", selector, to_address, None, None, None, None, None,
                parsed.get("amount_in"), parsed.get("amount_out_minimum"), None,
                notes=notes + parsed.get("notes", []), ambiguity=parsed.get("status"),
            )
        return MappedSwapAction(
            "MAPPED_CUSTOM_ROUTER",
            selector,
            to_address,
            None,
            "CUSTOM_EFFBEC13_SINGLE_HOP",
            0,
            parsed["pool_key"],
            parsed["zero_for_one"],
            parsed["amount_in"],
            parsed["amount_out_minimum"],
            parsed.get("hook_data_hex"),
            notes=notes + parsed.get("notes", []),
        )

    if to_address == MULTICALL3 or selector == "0x174dea71":
        return MappedSwapAction(
            "AMBIGUOUS_MULTI_LEG", selector, to_address, None, None, None, None, None, None, None, None,
            notes=notes + ["multicall_or_aggregator_not_fully_expanded"],
            ambiguity="MULTICALL_WRAPPER",
        )

    return MappedSwapAction(
        "UNKNOWN_TOP_LEVEL", selector, to_address, None, None, None, None, None, None, None, None,
        notes=notes, ambiguity="UNKNOWN_SELECTOR",
    )
