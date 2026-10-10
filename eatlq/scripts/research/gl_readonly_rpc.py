#!/usr/bin/env python3
"""Shared archive-only JSON-RPC primitives for GL6/GL7.

The endpoint is held only in memory.  The append-only ledger records a start
and finish record for every HTTP attempt, including transport exceptions.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path("/opt/EATLQ")
ALLOWED = {"eth_call", "eth_getBalance", "eth_getBlockByNumber", "eth_getLogs"}
WETH = "0x4200000000000000000000000000000000000006"
QUOTER = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
FROM = "0x000000000000000000000000000000000000b607"


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def archive_url() -> str:
    text = (ROOT / "reports/GL5v3_task_spec_Fon644_archive_resolve_20260716.md").read_text()
    m = re.search(r'export GL5V3_ARCHIVE_RPC="([^"]+)"', text)
    if not m:
        raise RuntimeError("archive endpoint absent from GL5v3 spec")
    return m.group(1)


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, ledger: Path, attempt_cap: int = 60000, user_agent: str = "EATLQ-GL-readonly/1"):
        self.url = archive_url()
        self.ledger = ledger
        self.cap = attempt_cap
        self.ua = user_agent
        self.lock = threading.Lock()
        self.next_call = 0
        self.attempts = 0
        self.logical_calls = 0
        self.methods = Counter()
        self.started = 0
        self.finished = 0
        ledger.unlink(missing_ok=True)

    def _append(self, rec: dict):
        with self.lock:
            with self.ledger.open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

    def call(self, method: str, params: list, timeout: int = 120):
        if method not in ALLOWED:
            raise RuntimeError(f"forbidden RPC method {method}")
        ph = canonical_hash(params)
        with self.lock:
            self.next_call += 1
            cid = self.next_call
            self.logical_calls += 1
            self.methods[method] += 1
        last = "unknown"
        for local_attempt in range(1, 4):
            with self.lock:
                self.attempts += 1
                aid = self.attempts
                if aid > self.cap:
                    raise RuntimeError("RPC attempt cap exceeded")
            base = {"attempt_global": aid, "attempt_for_call": local_attempt, "call_id": cid,
                    "method": method, "params_sha256": ph, "utc": datetime.now(timezone.utc).isoformat()}
            self._append({**base, "event": "attempt_started"})
            with self.lock:
                self.started += 1
            success = False
            rh = None
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": cid, "method": method, "params": params}).encode()
                req = Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": self.ua})
                with urlopen(req, timeout=timeout) as response:
                    payload = json.load(response)
                if payload.get("error") is not None:
                    msg = str(payload["error"]).lower()
                    last = "archive_unavailable" if ("archive" in msg or "missing trie" in msg or "historical state" in msg) else (
                        "rate_limited" if ("rate" in msg or "limit" in msg or "429" in msg) else (
                            "rpc_revert" if "revert" in msg else "rpc_error"))
                    rh = canonical_hash(payload["error"])
                else:
                    result = payload.get("result")
                    rh = canonical_hash(result)
                    success = True
                    return_value = result
            except Exception as exc:
                name = type(exc).__name__.lower()
                last = "timeout" if "timeout" in name else "transport_error"
            finally:
                self._append({**base, "event": "attempt_finished", "success": success,
                              "error_class": None if success else last, "response_sha256": rh})
                with self.lock:
                    self.finished += 1
            if success:
                return return_value, ph, rh
            if last == "rpc_revert":
                raise RpcError(last)
            if local_attempt < 3:
                time.sleep(0.25 * local_attempt)
        raise RpcError(last)

    def summary(self) -> dict:
        return {"endpoint_alias": "GL5V3_ARCHIVE_RPC_secret_omitted", "logical_calls": self.logical_calls,
                "http_attempts": self.attempts, "attempt_started_records": self.started,
                "attempt_finished_records": self.finished, "ledger_complete": self.attempts == self.started == self.finished,
                "methods": dict(self.methods), "allowed_methods": sorted(ALLOWED), "public_fallback_used": False,
                "send_sign_broadcast_calls": 0,
                "ledger_sha256": hashlib.sha256(self.ledger.read_bytes()).hexdigest() if self.ledger.exists() else None}


def compile_atomic_bytecode() -> str:
    out = Path("/tmp/gl_atomic_forgeout")
    art = out / "AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run([str(Path("/root/.foundry/bin/forge")), "build", "--contracts",
                        "scripts/research/base_b4/AtomicRoundTrip.sol", "--out", str(out),
                        "--cache-path", "/tmp/gl_atomic_forgecache"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_calldata(bytecode: str, token: str, fee: int, spend: int) -> str:
    return "0x" + bytecode + token.lower().removeprefix("0x").rjust(64, "0") + f"{fee:064x}" + f"{spend:064x}"


def decode_atomic(raw: str) -> dict:
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("short atomic return")
    vals = [int(raw[2+i*64:2+(i+1)*64], 16) for i in range(16)]
    keys = ["flags", "router_buy_out", "target_before", "target_after_buy", "target_after_sell",
            "router_sell_out", "weth_after_deposit", "weth_after_buy", "weth_after_sell", "pin_block",
            "chain_id", "parent_block_hash", "weth_approve_error_hash", "buy_error_hash",
            "target_approve_error_hash", "sell_error_hash"]
    return dict(zip(keys, vals))


def atomic_probe(rpc: Rpc, bytecode: str, token: str, fee: int, spend: int, block: int) -> dict:
    tx = {"from": FROM, "data": atomic_calldata(bytecode, token, fee, spend), "value": hex(spend), "gas": hex(25_000_000)}
    try:
        raw, qh, rh = rpc.call("eth_call", [tx, hex(block), {FROM: {"balance": hex(10**21)}}])
        a = decode_atomic(raw)
        got = max(0, a["target_after_buy"] - a["target_before"])
        recovered = max(0, a["weth_after_sell"] - a["weth_after_buy"])
        return {"ok": True, "buy_ok": bool(a["flags"] & 2) and got > 0,
                "sell_ok": bool(a["flags"] & 8) and recovered > 0,
                "received_target_raw": str(got), "recovered_weth_raw": str(recovered),
                "recovery_ratio": recovered / spend if spend else None, "flags": a["flags"],
                "request_sha256": qh, "response_sha256": rh,
                "buy_error_hash": f"0x{a['buy_error_hash']:064x}", "sell_error_hash": f"0x{a['sell_error_hash']:064x}"}
    except Exception as exc:
        return {"ok": False, "buy_ok": False, "sell_ok": False, "error_class": str(exc)}


def call_uint(rpc: Rpc, to: str, data: str, block: int, from_addr: str | None = None) -> dict:
    tx = {"to": to, "data": data}
    if from_addr:
        tx["from"] = from_addr
    try:
        raw, qh, rh = rpc.call("eth_call", [tx, hex(block)])
        return {"ok": bool(raw and raw != "0x"), "value_raw": str(int(raw, 16)) if raw and raw != "0x" else None,
                "request_sha256": qh, "response_sha256": rh}
    except Exception as exc:
        return {"ok": False, "value_raw": None, "error_class": str(exc)}


def balance_of(rpc: Rpc, token: str, owner: str, block: int) -> dict:
    data = "0x70a08231" + owner.lower().removeprefix("0x").rjust(64, "0")
    return call_uint(rpc, token, data, block)


def pool_fee(rpc: Rpc, pool: str, block: int) -> dict:
    x = call_uint(rpc, pool, "0xddca3f43", block)
    x["fee"] = int(x["value_raw"]) if x.get("value_raw") else None
    return x

