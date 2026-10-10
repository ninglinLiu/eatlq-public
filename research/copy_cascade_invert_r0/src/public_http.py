"""Public HTTPS + JSON-RPC. No EATLQ archive, no private keys."""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from src.common import UA

_DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
}


def http_json(
    url: str,
    *,
    timeout: float = 25.0,
    sleep_s: float = 0.06,
    retries: int = 5,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    payload: Any = None,
) -> tuple[int, Any, bytes]:
    last_status = 0
    last_body = b""
    for attempt in range(retries):
        if sleep_s:
            time.sleep(sleep_s + random.random() * sleep_s * 0.4)
        hdrs = dict(_DEFAULT_HEADERS)
        if headers:
            hdrs.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            status = int(exc.code)
        except Exception:  # noqa: BLE001 — recon must continue on DNS/TLS/WAF
            last_status = 0
            last_body = b""
            time.sleep(0.4 * (attempt + 1))
            continue
        last_status, last_body = status, body
        if status in {429, 403, 502, 503, 504}:
            time.sleep(0.6 * (attempt + 1))
            continue
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed = None
        return status, parsed, body
    parsed = None
    if last_body:
        try:
            parsed = json.loads(last_body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            parsed = None
    return last_status, parsed, last_body


def rpc_call(rpc_urls: list[str], method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for url in rpc_urls:
        status, parsed, _ = http_json(url, method="POST", payload=payload, sleep_s=0.02, timeout=20.0)
        if status == 200 and isinstance(parsed, dict) and parsed.get("result") is not None:
            return parsed["result"]
        last_err = (url, status, (parsed or {}) if isinstance(parsed, dict) else None)
    raise RuntimeError(f"public rpc failed {method}: {last_err}")


def rpc_batch(rpc_urls: list[str], calls: list[tuple[str, list[Any]]]) -> list[Any]:
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    last_err = None
    for url in rpc_urls:
        status, parsed, _ = http_json(url, method="POST", payload=payload, sleep_s=0.02, timeout=30.0)
        if status == 200 and isinstance(parsed, list):
            by_id = {row.get("id"): row.get("result") for row in parsed if isinstance(row, dict)}
            return [by_id.get(i) for i in range(len(calls))]
        last_err = (url, status)
    raise RuntimeError(f"public rpc batch failed: {last_err}")
