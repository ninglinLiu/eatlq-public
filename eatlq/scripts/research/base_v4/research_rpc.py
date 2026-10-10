"""Research-only RPC client: separate from live executor, counted + rate-limited."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

from web3 import Web3
from web3.providers.rpc import HTTPProvider

# Independent research endpoint — never read the live executor .env, and never
# hardcode the key here: provider URLs carry the API token in the path, so a
# literal default would ship the credential to anyone who clones the repo.
# Export RESEARCH_RPC_URL (and RESEARCH_ARCHIVE_RPC_URL for fork-replay) instead.
def require_rpc_url(var: str, *, fallback_var: Optional[str] = None) -> str:
    url = os.environ.get(var)
    if not url and fallback_var:
        url = os.environ.get(fallback_var)
    if not url:
        wanted = var if not fallback_var else f"{var} (or {fallback_var})"
        raise RuntimeError(
            f"{wanted} is not set. Export a research RPC endpoint before running; "
            "the token must never be committed."
        )
    return url


RPC_COUNTER_PATH = "/tmp/v4_research_rpc_counter.json"
RPC_HARD_LIMIT = 20_000
DEFAULT_RATE_LIMIT_RPS = 8.0


class RpcBudgetExceeded(RuntimeError):
    pass


class ResearchRpc:
    """Thread-safe JSON-RPC wrapper with global eth_* call budget."""

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        counter_path: str = RPC_COUNTER_PATH,
        hard_limit: int = RPC_HARD_LIMIT,
        rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS,
    ):
        self.url = url or require_rpc_url("RESEARCH_RPC_URL")
        self.counter_path = counter_path
        self.hard_limit = hard_limit
        self.min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._w3 = Web3(HTTPProvider(self.url, request_kwargs={"timeout": 90}))

    @property
    def w3(self) -> Web3:
        return self._w3

    def _load_counter(self) -> dict:
        if os.path.exists(self.counter_path):
            try:
                return json.load(open(self.counter_path))
            except Exception:
                pass
        return {"eth_calls": 0, "stopped": False}

    def _save_counter(self, data: dict) -> None:
        tmp = self.counter_path + ".tmp"
        json.dump(data, open(tmp, "w"))
        os.replace(tmp, self.counter_path)

    def count(self) -> int:
        return self._load_counter().get("eth_calls", 0)

    def is_stopped(self) -> bool:
        return bool(self._load_counter().get("stopped"))

    def _tick(self, method: str) -> None:
        with self._lock:
            state = self._load_counter()
            if state.get("stopped"):
                raise RpcBudgetExceeded(
                    f"Research RPC budget exhausted ({state['eth_calls']}/{self.hard_limit}); "
                    "counter marked stopped — manual reset required"
                )
            state["eth_calls"] = state.get("eth_calls", 0) + 1
            state["last_method"] = method
            state["last_ts"] = time.time()
            if state["eth_calls"] > self.hard_limit:
                state["stopped"] = True
                self._save_counter(state)
                raise RpcBudgetExceeded(
                    f"Research RPC hard limit exceeded: {state['eth_calls']} > {self.hard_limit}"
                )
            self._save_counter(state)
            if self.min_interval > 0:
                gap = self.min_interval - (time.time() - self._last_call)
                if gap > 0:
                    time.sleep(gap)
                self._last_call = time.time()

    def call(self, method: str, params: list[Any]) -> Any:
        self._tick(method)
        return self._w3.provider.make_request(method, params)["result"]

    def eth_block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def eth_get_logs(self, flt: dict) -> list:
        return self.call("eth_getLogs", [flt])

    def eth_get_code(self, address: str, block: str = "latest") -> bytes:
        raw = self.call("eth_getCode", [address, block])
        return bytes.fromhex(raw[2:]) if raw and raw != "0x" else b""

    def eth_get_block_by_number(self, block: int, full: bool = False) -> dict:
        tag = hex(block) if isinstance(block, int) else block
        return self.call("eth_getBlockByNumber", [tag, full])

    def eth_get_transaction_by_hash(self, tx_hash: str) -> Optional[dict]:
        return self.call("eth_getTransactionByHash", [tx_hash])


def get_research_rpc(*, archive: bool = False) -> ResearchRpc:
    if archive:
        url = require_rpc_url("RESEARCH_ARCHIVE_RPC_URL", fallback_var="RESEARCH_RPC_URL")
    else:
        url = require_rpc_url("RESEARCH_RPC_URL")
    return ResearchRpc(url)
