"""Auditable, rate-limited historical quote calibration primitives.

This module is deliberately separate from the v0.2 alpha runner.  It makes a
failed RPC attempt a first-class result so transport failure cannot silently
become ``NO_QUOTE`` market data.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from eth_abi import decode, encode
from eth_hash.auto import keccak

from .v02_quote import (
    DYNAMIC_FEE_FLAG,
    QUOTE_EXACT_INPUT_SINGLE_SEL,
    V4_QUOTER,
    PoolKey,
)

TRANSPORT_ERRORS = {
    "HTTP_429",
    "HTTP_5XX",
    "TIMEOUT",
    "CONNECTION_ERROR",
    "INVALID_JSON",
}


def strict_json_dumps(value: Any, *, indent: int = 2) -> str:
    """Serialize evidence as RFC-compatible JSON; NaN/Infinity are forbidden."""
    return json.dumps(value, indent=indent, sort_keys=True, allow_nan=False) + "\n"


def calldata_sha256(calldata: Optional[str]) -> Optional[str]:
    if calldata is None:
        return None
    return hashlib.sha256(bytes.fromhex(calldata.removeprefix("0x"))).hexdigest()


def extract_revert_data(error: Any) -> Optional[str]:
    """Find the longest hex payload in common JSON-RPC error wrappers."""
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str) and value.startswith("0x"):
            try:
                bytes.fromhex(value[2:])
            except ValueError:
                return
            candidates.append(value.lower())
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(error)
    return max(candidates, key=len) if candidates else None


def classify_jsonrpc_error(error: Any) -> tuple[str, Optional[str], list[str]]:
    """Return error class, raw revert payload, and visible 4-byte selectors."""
    data = extract_revert_data(error)
    if not data or len(data) < 10:
        return "JSON_RPC_ERROR", data, []
    raw = data[2:]
    # Preserve the outer selector and selectors at ABI-word boundaries after
    # it. This exposes nested custom errors such as the v0.2 0x7a5ed73
    # payload without guessing an unverified signature for 0x6190b2b0.
    visible = [raw[:8]] + [raw[i : i + 8] for i in range(8, len(raw) - 7, 64)]
    selectors = ["0x" + value for value in dict.fromkeys(visible) if value != "00000000"]
    return "UNKNOWN_REVERT", data, selectors


@dataclass
class RpcAttempt:
    attempt_id: int
    request_id: int
    purpose: str
    method: str
    endpoint_class: str
    endpoint: str
    attempt_number: int
    pool_id: Optional[str]
    token: Optional[str]
    quote_side: Optional[str]
    historical_block: Optional[int]
    to_address: Optional[str]
    calldata_sha256: Optional[str]
    started_at_utc: str
    elapsed_ms: float
    response_bytes: int
    ok: bool
    http_status: Optional[int]
    jsonrpc_code: Optional[int]
    error_class: Optional[str]
    error_message: Optional[str]
    revert_data: Optional[str]
    revert_selectors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RpcOutcome:
    ok: bool
    result: Any = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    revert_data: Optional[str] = None
    revert_selectors: list[str] = field(default_factory=list)
    final_attempt_id: Optional[int] = None

    @property
    def transport_failed(self) -> bool:
        return self.error_class in TRANSPORT_ERRORS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RpcBudgetExhausted(RuntimeError):
    pass


@dataclass
class CalibrationRpcClient:
    """Single-threaded public RPC with an auditable retry and rate policy."""

    endpoint: str = "https://mainnet.base.org"
    endpoint_class: str = "public_base_http"
    timeout_sec: float = 30.0
    max_calls: int = 200
    max_response_bytes: int = 50 * 1024 * 1024
    minimum_interval_sec: float = 2.0
    retry_backoff_sec: tuple[float, ...] = (2.0, 5.0, 15.0)
    max_consecutive_rate_limits: int = 6
    user_agent: str = "lpi-clanker-quote-calibration-v021/0.2.1"

    calls: int = 0
    response_bytes_total: int = 0
    journal: list[RpcAttempt] = field(default_factory=list)
    _request_id: int = 0
    _last_attempt_start: Optional[float] = None
    _consecutive_rate_limits: int = 0

    def _wait_for_rate_limit(self) -> None:
        if self._last_attempt_start is None:
            return
        remaining = self.minimum_interval_sec - (time.monotonic() - self._last_attempt_start)
        if remaining > 0:
            time.sleep(remaining)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "User-Agent": self.user_agent}

    def _append(
        self,
        *,
        request_id: int,
        attempt_number: int,
        purpose: str,
        method: str,
        pool_id: Optional[str],
        token: Optional[str],
        quote_side: Optional[str],
        historical_block: Optional[int],
        to_address: Optional[str],
        calldata: Optional[str],
        started_at_utc: str,
        elapsed_ms: float,
        response_bytes: int,
        ok: bool,
        http_status: Optional[int],
        jsonrpc_code: Optional[int],
        error_class: Optional[str],
        error_message: Optional[str],
        revert_data: Optional[str],
        revert_selectors: list[str],
    ) -> RpcAttempt:
        row = RpcAttempt(
            attempt_id=len(self.journal) + 1,
            request_id=request_id,
            purpose=purpose,
            method=method,
            endpoint_class=self.endpoint_class,
            endpoint=self.endpoint,
            attempt_number=attempt_number,
            pool_id=pool_id,
            token=token,
            quote_side=quote_side,
            historical_block=historical_block,
            to_address=to_address.lower() if to_address else None,
            calldata_sha256=calldata_sha256(calldata),
            started_at_utc=started_at_utc,
            elapsed_ms=round(elapsed_ms, 3),
            response_bytes=response_bytes,
            ok=ok,
            http_status=http_status,
            jsonrpc_code=jsonrpc_code,
            error_class=error_class,
            error_message=error_message[:500] if error_message else None,
            revert_data=revert_data,
            revert_selectors=revert_selectors,
        )
        self.journal.append(row)
        return row

    def call(
        self,
        method: str,
        params: list[Any],
        *,
        purpose: str,
        pool_id: Optional[str] = None,
        token: Optional[str] = None,
        quote_side: Optional[str] = None,
        historical_block: Optional[int] = None,
        to_address: Optional[str] = None,
        calldata: Optional[str] = None,
    ) -> RpcOutcome:
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

        for attempt_number in range(1, len(self.retry_backoff_sec) + 2):
            if self.calls >= self.max_calls:
                raise RpcBudgetExhausted(f"rpc_call_budget_exhausted:{self.calls}")
            if self.response_bytes_total >= self.max_response_bytes:
                raise RpcBudgetExhausted("rpc_response_budget_exhausted")
            self._wait_for_rate_limit()
            self._last_attempt_start = time.monotonic()
            started_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            t0 = time.perf_counter()
            body = b""
            try:
                request = urllib.request.Request(self.endpoint, data=payload, headers=self._headers())
                with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                    http_status = response.status
                    body = response.read(self.max_response_bytes + 1)
                self.calls += 1
                self.response_bytes_total += len(body)
                if len(body) > self.max_response_bytes or self.response_bytes_total > self.max_response_bytes:
                    raise RpcBudgetExhausted("rpc_response_budget_exhausted")
                try:
                    decoded = json.loads(body)
                except json.JSONDecodeError as exc:
                    row = self._append(
                        request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                        method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                        historical_block=historical_block, to_address=to_address, calldata=calldata,
                        started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                        response_bytes=len(body), ok=False, http_status=http_status,
                        jsonrpc_code=None, error_class="INVALID_JSON", error_message=str(exc),
                        revert_data=None, revert_selectors=[],
                    )
                    outcome = RpcOutcome(False, error_class="INVALID_JSON", error_message=str(exc), final_attempt_id=row.attempt_id)
                else:
                    if "error" in decoded:
                        error = decoded["error"]
                        cls, revert_data, selectors = classify_jsonrpc_error(error)
                        row = self._append(
                            request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                            method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                            historical_block=historical_block, to_address=to_address, calldata=calldata,
                            started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                            response_bytes=len(body), ok=False, http_status=http_status,
                            jsonrpc_code=error.get("code") if isinstance(error, dict) else None,
                            error_class=cls, error_message=str(error), revert_data=revert_data,
                            revert_selectors=selectors,
                        )
                        self._consecutive_rate_limits = 0
                        return RpcOutcome(
                            False, error_class=cls, error_message=str(error), revert_data=revert_data,
                            revert_selectors=selectors, final_attempt_id=row.attempt_id,
                        )
                    row = self._append(
                        request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                        method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                        historical_block=historical_block, to_address=to_address, calldata=calldata,
                        started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                        response_bytes=len(body), ok=True, http_status=http_status,
                        jsonrpc_code=None, error_class=None, error_message=None, revert_data=None,
                        revert_selectors=[],
                    )
                    self._consecutive_rate_limits = 0
                    return RpcOutcome(True, result=decoded.get("result"), final_attempt_id=row.attempt_id)
            except urllib.error.HTTPError as exc:
                self.calls += 1
                try:
                    body = exc.read(8192)
                except Exception:  # noqa: BLE001
                    body = b""
                self.response_bytes_total += len(body)
                status = exc.code
                cls = "HTTP_429" if status == 429 else ("HTTP_5XX" if status >= 500 else "HTTP_ERROR")
                row = self._append(
                    request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                    method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                    historical_block=historical_block, to_address=to_address, calldata=calldata,
                    started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                    response_bytes=len(body), ok=False, http_status=status, jsonrpc_code=None,
                    error_class=cls, error_message=f"HTTPError:{status}", revert_data=None,
                    revert_selectors=[],
                )
                outcome = RpcOutcome(False, error_class=cls, error_message=f"HTTPError:{status}", final_attempt_id=row.attempt_id)
            except (TimeoutError, socket.timeout) as exc:
                self.calls += 1
                row = self._append(
                    request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                    method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                    historical_block=historical_block, to_address=to_address, calldata=calldata,
                    started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                    response_bytes=0, ok=False, http_status=None, jsonrpc_code=None,
                    error_class="TIMEOUT", error_message=f"{type(exc).__name__}:{exc}",
                    revert_data=None, revert_selectors=[],
                )
                outcome = RpcOutcome(False, error_class="TIMEOUT", error_message=str(exc), final_attempt_id=row.attempt_id)
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionResetError,
                ConnectionError,
            ) as exc:
                self.calls += 1
                row = self._append(
                    request_id=request_id, attempt_number=attempt_number, purpose=purpose,
                    method=method, pool_id=pool_id, token=token, quote_side=quote_side,
                    historical_block=historical_block, to_address=to_address, calldata=calldata,
                    started_at_utc=started_at_utc, elapsed_ms=(time.perf_counter() - t0) * 1000,
                    response_bytes=0, ok=False, http_status=None, jsonrpc_code=None,
                    error_class="CONNECTION_ERROR", error_message=f"{type(exc).__name__}:{exc}",
                    revert_data=None, revert_selectors=[],
                )
                outcome = RpcOutcome(False, error_class="CONNECTION_ERROR", error_message=str(exc), final_attempt_id=row.attempt_id)

            if outcome.error_class == "HTTP_429":
                self._consecutive_rate_limits += 1
            else:
                self._consecutive_rate_limits = 0
            if self._consecutive_rate_limits >= self.max_consecutive_rate_limits:
                return outcome
            if attempt_number <= len(self.retry_backoff_sec) and outcome.error_class in TRANSPORT_ERRORS:
                time.sleep(self.retry_backoff_sec[attempt_number - 1])
                continue
            return outcome
        raise AssertionError("unreachable")

    def journal_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "response_bytes_total": self.response_bytes_total,
            "minimum_interval_sec": self.minimum_interval_sec,
            "concurrency": 1,
            "journal": [row.to_dict() for row in self.journal],
        }


def compute_pool_id(pool: PoolKey) -> str:
    encoded = encode(["(address,address,uint24,int24,address)"], [pool.as_tuple()])
    return "0x" + keccak(encoded).hex()


def build_quote_calldata(pool: PoolKey, *, zero_for_one: bool, amount_in: int) -> str:
    params = (pool.as_tuple(), zero_for_one, amount_in, b"")
    encoded = encode(["((address,address,uint24,int24,address),bool,uint128,bytes)"], [params])
    return "0x" + (QUOTE_EXACT_INPUT_SINGLE_SEL + encoded).hex()


def audited_quote(
    rpc: CalibrationRpcClient,
    pool: PoolKey,
    *,
    pool_id: str,
    token: str,
    side: str,
    zero_for_one: bool,
    amount_in: int,
    block: int,
) -> dict[str, Any]:
    calldata = build_quote_calldata(pool, zero_for_one=zero_for_one, amount_in=amount_in)
    outcome = rpc.call(
        "eth_call",
        [{"to": V4_QUOTER, "data": calldata}, hex(block)],
        purpose="historical_quote",
        pool_id=pool_id,
        token=token,
        quote_side=side,
        historical_block=block,
        to_address=V4_QUOTER,
        calldata=calldata,
    )
    row: dict[str, Any] = {
        "ok": False,
        "pool_id": pool_id,
        "token": token,
        "side": side,
        "zero_for_one": zero_for_one,
        "amount_in": amount_in,
        "amount_out": None,
        "gas_estimate": None,
        "block": block,
        "fee_raw": pool.fee,
        "fee_mode": "DYNAMIC_FLAG" if pool.fee & DYNAMIC_FEE_FLAG else "STATIC",
        "calldata_sha256": calldata_sha256(calldata),
        "rpc": outcome.to_dict(),
    }
    if not outcome.ok:
        return row
    raw = outcome.result
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) <= 2:
        row["rpc"]["error_class"] = "EMPTY_RETURN"
        row["rpc"]["error_message"] = "eth_call returned empty or non-hex result"
        return row
    try:
        amount_out, gas_estimate = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    except Exception as exc:  # noqa: BLE001
        row["rpc"]["error_class"] = "ABI_DECODE_ERROR"
        row["rpc"]["error_message"] = f"{type(exc).__name__}:{exc}"[:500]
        return row
    row.update(ok=True, amount_out=int(amount_out), gas_estimate=int(gas_estimate))
    return row


def adjudicate(
    *,
    entry_successes: int,
    immediate_reverse_successes: int,
    crosscheck_passes: int,
    transport_failures: int,
    total_network_attempts: int,
    budget_exhausted: bool,
) -> str:
    transport_ratio = transport_failures / total_network_attempts if total_network_attempts else 1.0
    if budget_exhausted or transport_ratio > 0.20:
        return "TRANSPORT_INSUFFICIENT"
    if entry_successes >= 8 and immediate_reverse_successes >= 8 and crosscheck_passes >= 5:
        return "QUOTE_PATH_VALIDATED"
    return "QUOTE_PATH_NOT_VALIDATED"
