#!/usr/bin/env python3
"""Pinned read-only RPC evidence for the frozen GL2-A b0 timeline.

Phase one measures every frozen row at b0+2/b0+6/b0+16, using the true
b0+1 integer holding for the exact-size quote.  Only rows still unresolved are
eligible for the later five-evidence phase.  No send/sign/broadcast method is
implemented.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL2_A_prereg_five_evidence_20260715.md"
CORE = DIAG / "base_reaudit10_seconds_rpc_20260715.json"
OUT = DIAG / "base_gl2_a_five_evidence_rpc_20260715.json"
LEDGER = DIAG / "base_gl2_a_rpc_full_ledger_20260715.jsonl"
SUMMARY = DIAG / "base_gl2_a_rpc_ledger_20260715.json"
PREREG_SHA256 = "331009ad147eedc5189fa9f7ab5651b479d5065ec6cd29532816ecbf6e17ed07"
WINDOWS = (1, 5, 15)
ALLOWED = {"eth_call", "eth_getLogs", "eth_getTransactionReceipt", "debug_traceTransaction"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def env_value(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    path = ROOT / "infra/compose/.env"
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def error_class(exc: Exception) -> str:
    text = str(exc).lower()
    if "execution reverted" in text:
        return "execution_reverted"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "rpc_other"


class Rpc:
    def __init__(self, url: str, *, reset_ledger: bool):
        if "prettiest-blue-aura" not in url:
            raise RuntimeError("GL2-A is frozen to endpoint alias prettiest-blue-aura")
        self.url = url
        self.lock = threading.Lock()
        self.logical = 0
        self.attempts = 0
        self.methods = Counter()
        self.errors = Counter()
        self.last_started = 0.0
        if reset_ledger:
            LEDGER.write_text("")
        elif not LEDGER.exists():
            raise RuntimeError("append requested but full ledger does not exist")
        else:
            for line in LEDGER.read_text().splitlines():
                if not line:
                    continue
                row = json.loads(line)
                self.attempts = max(self.attempts, int(row["attempt_id"]))
                self.logical = max(self.logical, int(row["logical_id"]))
                self.methods[row["method"]] += 1
                if row["status"] == "error":
                    self.errors[row.get("error_class", row.get("error_type", "unknown"))] += 1

    def _append(self, row: dict) -> None:
        with self.lock:
            with LEDGER.open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def call(self, method: str, params: list, timeout: int = 150):
        if method not in ALLOWED:
            raise RuntimeError(f"FORBIDDEN_RPC_METHOD:{method}")
        with self.lock:
            self.logical += 1
            logical_id = self.logical
        request_hash = canonical_hash({"method": method, "params": params})
        last = None
        for retry in range(1, 5):
            # Keep the aggregate rate below the provider's 50 req/s limit.
            with self.lock:
                wait = max(0.0, self.last_started + 0.025 - time.monotonic())
                if wait:
                    time.sleep(wait)
                self.last_started = time.monotonic()
                self.attempts += 1
                attempt_id = self.attempts
                self.methods[method] += 1
            started = time.time()
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": logical_id, "method": method, "params": params}).encode()
                request = Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": "EATLQ-GL2A-readonly/1"})
                with urlopen(request, timeout=timeout) as response:
                    obj = json.load(response)
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"]))
                result = obj.get("result")
                meta = {"request_sha256": request_hash, "response_sha256": canonical_hash(result)}
                self._append({"attempt_id": attempt_id, "logical_id": logical_id, "method": method, "retry": retry, "status": "ok", "elapsed_ms": round((time.time() - started) * 1000, 3), **meta})
                return result, meta
            except Exception as exc:
                last = exc
                category = error_class(exc)
                with self.lock:
                    self.errors[category] += 1
                self._append({"attempt_id": attempt_id, "logical_id": logical_id, "method": method, "retry": retry, "status": "error", "error_type": type(exc).__name__, "error_class": category, "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(), "elapsed_ms": round((time.time() - started) * 1000, 3), "request_sha256": request_hash})
                if category == "execution_reverted":
                    break
                time.sleep(0.5 * retry)
        raise RuntimeError(f"{type(last).__name__}:{str(last)[:180]}")

    def summary(self) -> dict:
        return {
            "endpoint_alias": "prettiest-blue-aura",
            "posture": "read-only; no send/sign/broadcast method implemented",
            "logical_calls": self.logical,
            "http_attempts": self.attempts,
            "methods_attempted": dict(sorted(self.methods.items())),
            "errors_by_type": dict(sorted(self.errors.items())),
            "full_ledger_path": str(LEDGER.relative_to(ROOT)),
            "full_ledger_sha256": sha(LEDGER),
        }


def safe(fn, where: str) -> dict:
    try:
        return fn()
    except Exception as exc:
        return {"status": "unknown", "where": where, "error_type": type(exc).__name__, "error_class": error_class(exc), "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}


def row_probe(rpc: Rpc, old, code: str, row: dict) -> dict:
    entry = next(p for p in row["entry_points"] if p["offset_blocks"] == 1)
    entry_atomic = entry["atomic"]
    qty = int(entry_atomic.get("received_target", 0) or 0)
    fee = int(row["fee_evidence"]["fee"])
    points = []
    for window in WINDOWS:
        # Frozen preregistration: b0+1 entry; +1/+5/+15 after entry.
        block = int(row["entry_block"]) + 1 + window
        atomic = safe(lambda b=block: old.atomic_at(rpc, code, row["token"], fee, b), "target_atomic")
        quote = {"status": "not_applicable", "reason": "no_true_entry_holding"}
        if qty > 0:
            quote = safe(lambda b=block: old.quote_at(rpc, row["token"], fee, qty, b), "target_exact_size_quote")
        points.append({"window_blocks_after_entry": window, "scheduled_block": block, "atomic_guard": atomic, "exact_size_quote": quote})
    return {
        "stable_key": hashlib.sha256("|".join((row["pool"].lower(), row["token"].lower(), str(row["entry_block"]), str(row["trigger_block"]))).encode()).hexdigest(),
        "pool": row["pool"],
        "token": row["token"],
        "orient": int(row["orient"]),
        "regime": row["regime"],
        "selected": bool(row["selected"]),
        "signal_time": row["signal_time"],
        "b0": int(row["entry_block"]),
        "entry_block": int(entry["scheduled_block"]),
        "true_position_token_raw": str(qty) if qty > 0 else None,
        "entry_atomic": entry_atomic,
        "fee": fee,
        "target_points": points,
        "five_evidence": {},
    }


def main() -> None:
    if sha(PREREG) != PREREG_SHA256:
        raise RuntimeError("frozen GL2-A preregistration hash mismatch")
    url = env_value("EXECUTOR_READ_RPC_URL")
    if not url:
        raise RuntimeError("EXECUTOR_READ_RPC_URL missing")
    rpc = Rpc(url, reset_ledger=True)
    old = load_module(ROOT / "scripts/research/base_reaudit10/reaudit10_rpc.py", "gl2a_rpc_helpers")
    code = old.compile_bytecode()
    core = json.loads(CORE.read_text())
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(row_probe, rpc, old, code, row) for row in core["rows"]]
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if i % 50 == 0 or i == len(futures):
                print(f"[GL2-A target ladder] {i}/{len(futures)} logical={rpc.logical} attempts={rpc.attempts}", flush=True)
    rows.sort(key=lambda r: r["stable_key"])
    payload = {
        "schema_version": 2,
        "research_only": True,
        "preregistration_sha256": PREREG_SHA256,
        "timeline": {"b0_field": "base_reaudit10_seconds_rpc.rows[].entry_block", "entry": "b0+1", "exits": {"1": "b0+2", "5": "b0+6", "15": "b0+16"}},
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "selected": sum(r["selected"] for r in rows),
            "target_points": sum(len(r["target_points"]) for r in rows),
            "fully_priced_points": sum(
                p["atomic_guard"].get("status") == "measured"
                and p["atomic_guard"].get("sell_ok")
                and p["atomic_guard"].get("recovery_ratio", 0) > 0.1
                and p["exact_size_quote"].get("status") == "measured"
                and int(p["exact_size_quote"].get("amount_out", 0) or 0) > 0
                for r in rows for p in r["target_points"]
            ),
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    ledger = {
        "schema_version": 2,
        "research_only": True,
        "preregistration_sha256": PREREG_SHA256,
        "phase": "all_431_frozen_b0_target_ladder",
        "rpc": rpc.summary(),
        "output": {"path": str(OUT.relative_to(ROOT)), "sha256": sha(OUT)},
    }
    SUMMARY.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"summary": payload["summary"], "rpc": ledger["rpc"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
