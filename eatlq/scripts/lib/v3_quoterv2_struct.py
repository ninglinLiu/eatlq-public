"""Research-only Uniswap V3 QuoterV2 struct ABI helper.

This module intentionally has no production execution hooks. It provides the
known-good QuoterV2 struct ABI path validated in V3R-13C.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Protocol

import requests

WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
STRUCT_SELECTOR = "c6a5026a"


class RpcLike(Protocol):
    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        ...


class JsonRpcClient:
    """Minimal JSON-RPC client for diagnostics scripts."""

    def __init__(self, url: str, delay_s: float = 0.04) -> None:
        self.url = url
        self.delay_s = delay_s
        self.session = requests.Session()
        self._id = 0
        self._calls = 0
        self._lock = Lock()

    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        last_error: Exception | None = None
        for attempt in range(6):
            with self._lock:
                self._id += 1
                req_id = self._id
                self._calls += 1
            try:
                if self.delay_s > 0:
                    time.sleep(self.delay_s)
                resp = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if "error" in payload:
                    raise RuntimeError(f"rpc[{method}]: {payload['error']}")
                return payload.get("result")
            except Exception as exc:
                last_error = exc
                if "429" in str(exc) or "Too Many Requests" in str(exc):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(str(last_error) if last_error else "rpc_failed")

    @property
    def calls(self) -> int:
        return self._calls


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _pad_addr(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(64, "0")


def _pad_uint(n: int) -> str:
    return hex(int(n))[2:].rjust(64, "0")


def _decode_uint256_first_word(res: str | None) -> int:
    if not res or res == "0x":
        return 0
    raw = res[2:] if res.startswith("0x") else res
    return int(raw[:64], 16) if len(raw) >= 64 else 0


def _block_tag(block_identifier: int | str | None) -> str:
    if block_identifier is None:
        return "latest"
    if isinstance(block_identifier, int):
        return hex(max(0, block_identifier))
    block = block_identifier.strip().lower()
    if block == "head":
        return "latest"
    if block.startswith("0x") or block in {"latest", "pending", "earliest", "safe", "finalized"}:
        return block
    return block


def _resolve_block_used(rpc: RpcLike, block_identifier: int | str | None) -> int | str:
    if isinstance(block_identifier, int):
        return max(0, block_identifier)
    if block_identifier is None or str(block_identifier).strip().lower() in {"", "latest", "head"}:
        try:
            return int(str(rpc.call("eth_blockNumber", [], timeout=15)), 16)
        except Exception:
            return "latest"
    return str(block_identifier)


def build_quote_exact_input_single_calldata(
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    sqrt_price_limit_x96: int = 0,
) -> str:
    return (
        "0x"
        + STRUCT_SELECTOR
        + _pad_addr(token_in)
        + _pad_addr(token_out)
        + _pad_uint(amount_in)
        + _pad_uint(fee)
        + _pad_uint(sqrt_price_limit_x96)
    )


def quote_exact_input_single_struct(
    rpc: RpcLike,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    block_identifier: int | str | None = None,
    sqrt_price_limit_x96: int = 0,
    quoter: str = QUOTER_V2_BASE,
    timeout: int = 25,
) -> dict[str, Any]:
    """Call QuoterV2.quoteExactInputSingle using the validated struct ABI."""
    token_in_l = _lower(token_in)
    token_out_l = _lower(token_out)
    block_used = _resolve_block_used(rpc, block_identifier)
    out: dict[str, Any] = {
        "ok": False,
        "failed": True,
        "amount_out": 0,
        "reason": "",
        "selector": STRUCT_SELECTOR,
        "fee": int(fee),
        "block_used": block_used,
        "token_in": token_in_l,
        "token_out": token_out_l,
        "amount_in": int(amount_in),
    }
    if int(amount_in) <= 0:
        out["reason"] = "zero_amount_in"
        return out

    data = build_quote_exact_input_single_calldata(
        token_in_l, token_out_l, int(amount_in), int(fee), int(sqrt_price_limit_x96)
    )
    try:
        res = rpc.call("eth_call", [{"to": quoter, "data": data}, _block_tag(block_identifier)], timeout=timeout)
    except Exception as exc:
        msg = str(exc)[:240]
        out["reason"] = "quoter_revert" if "revert" in msg.lower() else "rpc_error"
        out["error"] = msg
        return out

    amount_out = _decode_uint256_first_word(res)
    out["amount_out"] = amount_out
    if amount_out <= 0:
        out["reason"] = "zero_amount_out" if res and res != "0x" else "empty_result"
        return out
    out["ok"] = True
    out["failed"] = False
    out["reason"] = ""
    return out
