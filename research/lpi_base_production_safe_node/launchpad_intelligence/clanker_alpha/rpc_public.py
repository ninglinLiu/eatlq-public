"""Budgeted public JSON-RPC client (no secrets; concurrency=1)."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RpcCallRecord:
    method: str
    endpoint_class: str
    block_range: Optional[list[int]]
    address: Optional[str]
    topic0: Optional[str]
    response_bytes: int
    elapsed_ms: float
    ok: bool
    error: Optional[str] = None


@dataclass
class PublicRpcClient:
    endpoint: str
    endpoint_class: str = "public_base_http"
    timeout_sec: float = 30.0
    retry_limit: int = 1
    max_calls: int = 1000
    max_response_bytes: int = 250 * 1024 * 1024
    user_agent: str = "lpi-clanker-alpha-v01/0.1 (+local-research)"

    calls: int = 0
    response_bytes_total: int = 0
    journal: list[RpcCallRecord] = field(default_factory=list)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def call(
        self,
        method: str,
        params: list[Any],
        *,
        block_range: Optional[list[int]] = None,
        address: Optional[str] = None,
        topic0: Optional[str] = None,
    ) -> Any:
        if self.calls >= self.max_calls:
            raise RuntimeError(f"rpc_budget_exhausted calls={self.calls}")
        if self.response_bytes_total >= self.max_response_bytes:
            raise RuntimeError("rpc_response_budget_exhausted")

        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        last_err: Optional[str] = None
        attempts = 1 + max(0, self.retry_limit)
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                req = urllib.request.Request(self.endpoint, data=payload, headers=self._headers())
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    body = resp.read()
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self.calls += 1
                self.response_bytes_total += len(body)
                if self.response_bytes_total > self.max_response_bytes:
                    raise RuntimeError("rpc_response_budget_exhausted")
                out = json.loads(body)
                if "error" in out:
                    last_err = str(out["error"])
                    self.journal.append(
                        RpcCallRecord(
                            method=method,
                            endpoint_class=self.endpoint_class,
                            block_range=block_range,
                            address=address,
                            topic0=topic0,
                            response_bytes=len(body),
                            elapsed_ms=elapsed_ms,
                            ok=False,
                            error=last_err,
                        )
                    )
                    if attempt + 1 < attempts:
                        time.sleep(0.25)
                        continue
                    raise RuntimeError(last_err)
                self.journal.append(
                    RpcCallRecord(
                        method=method,
                        endpoint_class=self.endpoint_class,
                        block_range=block_range,
                        address=address,
                        topic0=topic0,
                        response_bytes=len(body),
                        elapsed_ms=elapsed_ms,
                        ok=True,
                    )
                )
                return out["result"]
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                ConnectionResetError,
                ConnectionError,
            ) as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_err = f"{type(exc).__name__}:{exc}"
                self.calls += 1
                self.journal.append(
                    RpcCallRecord(
                        method=method,
                        endpoint_class=self.endpoint_class,
                        block_range=block_range,
                        address=address,
                        topic0=topic0,
                        response_bytes=0,
                        elapsed_ms=elapsed_ms,
                        ok=False,
                        error=last_err,
                    )
                )
                if attempt + 1 < attempts:
                    time.sleep(0.6)
                    continue
                raise RuntimeError(last_err) from exc
        raise RuntimeError(last_err or "rpc_failed")
