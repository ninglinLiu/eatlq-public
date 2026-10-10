"""
Aero vAMM Adapter Pilot -- research-only bounded adapter validation.

This script validates whether confirmed Aerodrome vAMM / volatile rows can be
quoted through Aero vAMM semantics without entering any training path.

Hard boundaries:
  - max 100 rows by default
  - only aero_vamm_volatile rows from logs/aero_vamm_small_sample_candidates.csv
  - no Aero CL / Slipstream, stable pools, unknown pools, full labels, training,
    promote, or LIVE behavior
  - no RPC URL/API key is written to outputs

Env:
  LABEL_RPC_URL or EVM_HTTP_URL        archive RPC; if unset, local
                                      infra/compose/.env is read silently
  AERO_VAMM_PILOT_MAX_ROWS            default 100, hard-capped at 100
  LABEL_WORKERS                       default 1, hard-capped at 2
  LABEL_EXIT_HORIZON_SEC              default 1800
  LABEL_NOTIONAL_WEI                  default 500000000000000
  AERO_VAMM_TOKEN0_NOTIONAL_RAW       default 1000000000000000000
  AERO_VAMM_RPC_MAX_CALLS             default 850
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent

INPUT_CSV = REPO / "logs" / "aero_vamm_small_sample_candidates.csv"
RESEARCH_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_small_sample_research_report.json"
OUT_CSV = REPO / "logs" / "aero_vamm_adapter_pilot_output.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_adapter_pilot_report.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-adapter-pilot.md"

WETH_BASE = "0x4200000000000000000000000000000000000006"
AERO_VAMM_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
AERO_V2_FACTORY = "0x420dd381b31aef6683db6b902084cb0ffece40da"
KNOWN_TOKEN_DECIMALS = {
    WETH_BASE.lower(): 18,
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631": 18,  # AERO
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,   # USDC
}

FAIL_REASONS = (
    "ok",
    "fail_not_aero_vamm",
    "fail_stable_pool_excluded",
    "fail_cl_slipstream_excluded",
    "fail_unknown_pool_type",
    "fail_abi_mismatch",
    "fail_reserve_unavailable",
    "fail_token_metadata_missing",
    "fail_quote_unavailable",
    "fail_quote_mismatch",
    "fail_before_creation",
    "fail_rpc_error",
    "fail_sanity_clip",
)

OUT_FIELDS = [
    "tx_hash",
    "log_index",
    "pool_address",
    "token_address",
    "pool_type",
    "stable_flag",
    "adapter_status",
    "failure_reason",
    "reserves_status",
    "quote_status",
    "estimated_entry_quote",
    "estimated_exit_quote",
    "sanity_check_status",
    "entry_block",
    "exit_block",
    "token0",
    "token1",
    "quote_token_in",
    "quote_token_out",
    "quote_amount_in",
    "roundtrip_net_estimate",
    "rpc_calls_this_row",
]

# Minimal selectors used by Aerodrome IPool. These are selectors only, not ABI
# assumptions about pool math; quote math comes from getAmountOut().
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_STABLE = "0x22be3de1"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_GET_AMOUNT_OUT = "0xf140a35a"  # getAmountOut(uint256,address)


class PilotAbort(RuntimeError):
    pass


class RPC:
    """Small JSON-RPC client with per-method counting and 429 hard abort."""

    def __init__(self, url: str, timeout: int = 30, max_calls: int = 850) -> None:
        self._url = url
        self._timeout = timeout
        self._max_calls = max_calls
        self._id = 0
        self._lock = threading.Lock()
        self._aborted = threading.Event()
        self.method_counts: Counter[str] = Counter()

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(self.method_counts.values())

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: list[Any]) -> Any:
        if self._aborted.is_set():
            raise PilotAbort("RPC_ABORTED")
        with self._lock:
            if sum(self.method_counts.values()) >= self._max_calls:
                self._aborted.set()
                raise PilotAbort("RPC_BUDGET_EXHAUSTED")
            self.method_counts[method] += 1
        req = Request(
            self._url,
            data=json.dumps({
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._next_id(),
            }).encode("utf-8"),
            method="POST",
        )
        req.add_header("content-type", "application/json")
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except HTTPError as exc:
            if exc.code == 429:
                self._aborted.set()
                raise PilotAbort("HTTP_429_ABORT") from exc
            raise
        if isinstance(payload, dict) and payload.get("error"):
            message = str(payload["error"].get("message", "rpc_error"))
            raise RuntimeError(message)
        return payload.get("result") if isinstance(payload, dict) else None


@dataclass(frozen=True)
class PoolMeta:
    pool: str
    token0: str
    token1: str
    stable: bool
    token0_symbol: str = ""
    token1_symbol: str = ""


def _load_local_env_if_needed() -> None:
    if os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL"):
        return
    env_path = REPO / "infra" / "compose" / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key in {"LABEL_RPC_URL", "EVM_HTTP_URL"} and not os.environ.get(key):
            os.environ[key] = val.strip().strip('"').strip("'")


def _norm_addr(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value.startswith("0x") else f"0x{value}"


def _valid_addr(value: str) -> bool:
    value = _norm_addr(value)
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _pad32(hex_value: str) -> str:
    return hex_value.lower().replace("0x", "").rjust(64, "0")


def _addr_arg(addr: str) -> str:
    return _pad32(_norm_addr(addr)[2:])


def _uint_arg(value: int) -> str:
    return _pad32(hex(max(0, int(value)))[2:])


def _decode_addr(result: str) -> str:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short address return")
    return "0x" + raw[-40:].lower()


def _decode_bool(result: str) -> bool:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short bool return")
    return int(raw[-64:], 16) != 0


def _decode_uint(result: str) -> int:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short uint return")
    return int(raw[0:64], 16)


def _eth_call(rpc: RPC, to_addr: str, data: str, block: int | str) -> str:
    block_tag = hex(block) if isinstance(block, int) else block
    result = rpc.call("eth_call", [{"to": _norm_addr(to_addr), "data": data}, block_tag])
    if not isinstance(result, str) or result in {"", "0x"}:
        raise ValueError("empty eth_call result")
    return result


def call_token0(rpc: RPC, pool: str, block: int | str = "latest") -> str:
    return _decode_addr(_eth_call(rpc, pool, SEL_TOKEN0, block))


def call_token1(rpc: RPC, pool: str, block: int | str = "latest") -> str:
    return _decode_addr(_eth_call(rpc, pool, SEL_TOKEN1, block))


def call_stable(rpc: RPC, pool: str, block: int | str = "latest") -> bool:
    return _decode_bool(_eth_call(rpc, pool, SEL_STABLE, block))


def call_get_reserves(rpc: RPC, pool: str, block: int) -> tuple[int, int]:
    raw = _eth_call(rpc, pool, SEL_GET_RESERVES, block).replace("0x", "")
    if len(raw) < 128:
        raise ValueError("short reserves return")
    return int(raw[0:64], 16), int(raw[64:128], 16)


def call_get_amount_out(rpc: RPC, pool: str, amount_in: int, token_in: str, block: int) -> int:
    data = SEL_GET_AMOUNT_OUT + _uint_arg(amount_in) + _addr_arg(token_in)
    return _decode_uint(_eth_call(rpc, pool, data, block))


def _load_rows(max_rows: int) -> list[dict[str, str]]:
    if not INPUT_CSV.is_file():
        raise FileNotFoundError(str(INPUT_CSV))
    rows: list[dict[str, str]] = []
    with INPUT_CSV.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
            if len(rows) >= max_rows:
                break
    return rows


def _classify_static(row: dict[str, str]) -> str:
    pt = (row.get("pool_type_class") or row.get("pool_type") or "").lower()
    topic0 = (row.get("topic0") or "").lower()
    event_kind = (row.get("event_kind") or "").lower()
    stable = (row.get("stable") or "").lower()
    if "cl" in pt or "slipstream" in pt or "cl" in event_kind:
        return "fail_cl_slipstream_excluded"
    if not pt or "unknown" in pt:
        return "fail_unknown_pool_type"
    if "aero_vamm" not in pt:
        return "fail_not_aero_vamm"
    if stable in {"true", "1", "yes"}:
        return "fail_stable_pool_excluded"
    if topic0 != AERO_VAMM_SWAP_TOPIC:
        return "fail_abi_mismatch"
    return "ok"


def _pool_meta_from_row(row: dict[str, str]) -> PoolMeta | None:
    pool = _norm_addr(row.get("pool_address", ""))
    t0 = _norm_addr(row.get("token0", ""))
    t1 = _norm_addr(row.get("token1", ""))
    if not _valid_addr(pool):
        return None
    stable = (row.get("stable") or "").strip().lower() in {"1", "true", "yes"}
    return PoolMeta(
        pool=pool,
        token0=t0 if _valid_addr(t0) else "",
        token1=t1 if _valid_addr(t1) else "",
        stable=stable,
        token0_symbol=row.get("token0_symbol", ""),
        token1_symbol=row.get("token1_symbol", ""),
    )


def _base_out(row: dict[str, str], reason: str, before_calls: int, after_calls: int) -> dict[str, Any]:
    return {
        "tx_hash": row.get("tx_hash", ""),
        "log_index": row.get("log_index", ""),
        "pool_address": _norm_addr(row.get("pool_address", "")),
        "token_address": _norm_addr(row.get("token_address_from_candidate", "")) if row.get("token_address_from_candidate") else "",
        "pool_type": "aero_vamm" if reason != "fail_not_aero_vamm" else row.get("pool_type_class", ""),
        "stable_flag": row.get("stable", ""),
        "adapter_status": "ok" if reason == "ok" else "failed",
        "failure_reason": reason,
        "reserves_status": "not_attempted",
        "quote_status": "not_attempted",
        "estimated_entry_quote": "",
        "estimated_exit_quote": "",
        "sanity_check_status": "not_attempted",
        "entry_block": "",
        "exit_block": "",
        "token0": row.get("token0", ""),
        "token1": row.get("token1", ""),
        "quote_token_in": "",
        "quote_token_out": "",
        "quote_amount_in": "",
        "roundtrip_net_estimate": "",
        "rpc_calls_this_row": max(0, after_calls - before_calls),
    }


def process_row(
    row: dict[str, str],
    rpc: RPC,
    latest_block: int,
    pool_cache: dict[str, PoolMeta],
    pool_lock: threading.Lock,
    notional_wei: int,
    token0_notional: int,
    exit_horizon_sec: int,
) -> dict[str, Any]:
    before_calls = rpc.total_calls
    static_reason = _classify_static(row)
    if static_reason != "ok":
        return _base_out(row, static_reason, before_calls, rpc.total_calls)

    meta = _pool_meta_from_row(row)
    if meta is None:
        return _base_out(row, "fail_token_metadata_missing", before_calls, rpc.total_calls)

    try:
        receipt = rpc.call("eth_getTransactionReceipt", [row.get("tx_hash", "")])
        if not receipt or not receipt.get("blockNumber"):
            return _base_out(row, "fail_before_creation", before_calls, rpc.total_calls)
        entry_block = int(receipt["blockNumber"], 16)
        if entry_block <= 0 or entry_block > latest_block:
            return _base_out(row, "fail_before_creation", before_calls, rpc.total_calls)
        # Base is close to 2s/block. This pilot avoids rebuilding a broad ts grid.
        exit_block = min(latest_block, entry_block + max(1, exit_horizon_sec // 2))

        with pool_lock:
            cached = pool_cache.get(meta.pool)
        if cached is None:
            chain_t0 = call_token0(rpc, meta.pool, "latest")
            chain_t1 = call_token1(rpc, meta.pool, "latest")
            chain_stable = call_stable(rpc, meta.pool, "latest")
            cached = PoolMeta(meta.pool, chain_t0, chain_t1, chain_stable, meta.token0_symbol, meta.token1_symbol)
            with pool_lock:
                pool_cache[meta.pool] = cached

        if cached.stable:
            out = _base_out(row, "fail_stable_pool_excluded", before_calls, rpc.total_calls)
            out["stable_flag"] = "true"
            return out
        meta = cached

        try:
            r0, r1 = call_get_reserves(rpc, meta.pool, entry_block)
        except Exception:
            return _base_out(row, "fail_reserve_unavailable", before_calls, rpc.total_calls)
        if r0 <= 0 or r1 <= 0:
            return _base_out(row, "fail_reserve_unavailable", before_calls, rpc.total_calls)

        weth = WETH_BASE.lower()
        if meta.token0 == weth:
            token_in, token_out, amount_in, token_address = meta.token0, meta.token1, notional_wei, meta.token1
        elif meta.token1 == weth:
            token_in, token_out, amount_in, token_address = meta.token1, meta.token0, notional_wei, meta.token0
        else:
            decimals = KNOWN_TOKEN_DECIMALS.get(meta.token0, 18)
            amount_in = 10 ** decimals if meta.token0 in KNOWN_TOKEN_DECIMALS else token0_notional
            token_in, token_out, token_address = meta.token0, meta.token1, meta.token0

        try:
            entry_quote = call_get_amount_out(rpc, meta.pool, amount_in, token_in, entry_block)
            exit_quote = call_get_amount_out(rpc, meta.pool, entry_quote, token_out, exit_block) if entry_quote > 0 else 0
        except Exception:
            out = _base_out(row, "fail_quote_unavailable", before_calls, rpc.total_calls)
            out["reserves_status"] = "ok"
            return out
        if entry_quote <= 0 or exit_quote <= 0:
            out = _base_out(row, "fail_quote_unavailable", before_calls, rpc.total_calls)
            out["reserves_status"] = "ok"
            out["quote_status"] = "failed"
            return out

        net = (exit_quote / amount_in) - 1.0
        if net < -1.0 or net > 10.0:
            reason = "fail_sanity_clip"
            status = "failed"
            sanity = "failed"
        else:
            reason = "ok"
            status = "ok"
            sanity = "ok"

        return {
            "tx_hash": row.get("tx_hash", ""),
            "log_index": row.get("log_index", ""),
            "pool_address": meta.pool,
            "token_address": token_address,
            "pool_type": "aero_vamm",
            "stable_flag": "false",
            "adapter_status": status,
            "failure_reason": reason,
            "reserves_status": "ok",
            "quote_status": "ok",
            "estimated_entry_quote": str(entry_quote),
            "estimated_exit_quote": str(exit_quote),
            "sanity_check_status": sanity,
            "entry_block": str(entry_block),
            "exit_block": str(exit_block),
            "token0": meta.token0,
            "token1": meta.token1,
            "quote_token_in": token_in,
            "quote_token_out": token_out,
            "quote_amount_in": str(amount_in),
            "roundtrip_net_estimate": f"{net:.12f}",
            "rpc_calls_this_row": max(0, rpc.total_calls - before_calls),
        }
    except PilotAbort:
        raise
    except Exception:
        return _base_out(row, "fail_rpc_error", before_calls, rpc.total_calls)


def _pct(n: int, d: int) -> float:
    return round((float(n) / float(d)) if d else 0.0, 6)


def _conclusion(report: dict[str, Any]) -> str:
    attempted = int(report["attempted_rows"])
    if attempted <= 0 or report.get("script_error"):
        return "HOLD_DUE_TO_SCRIPT_FAILURE"
    if report["stable_accidentally_accepted"] or report["cl_slipstream_accidentally_accepted"]:
        return "HOLD_DUE_TO_POOL_TYPE_AMBIGUITY"
    if report["unknown_accidentally_accepted"]:
        return "HOLD_DUE_TO_POOL_TYPE_AMBIGUITY"
    if report["reserve_success_rate"] < 0.95:
        return "HOLD_DUE_TO_RESERVE_FAILURE"
    if report["quote_success_rate"] < 0.90:
        return "HOLD_DUE_TO_QUOTE_UNAVAILABLE"
    if report["sanity_clip_count"] > attempted * 0.05:
        return "HOLD_DUE_TO_SANITY_FAILURE"
    cps = report["estimated_rpc_calls_per_success"]
    if cps is None or cps > 8:
        return "HOLD_DUE_TO_RPC_COST"
    if report["success_rate"] >= 0.90:
        return "AERO_VAMM_ADAPTER_PILOT_GO_TO_1000_ROW_DRYRUN"
    if report["failure_reason_distribution"].get("fail_abi_mismatch", 0):
        return "HOLD_DUE_TO_ABI_MISMATCH"
    return "HOLD_DUE_TO_SCRIPT_FAILURE"


def _write_outputs(rows_out: list[dict[str, Any]], report: dict[str, Any]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for row in rows_out:
            writer.writerow({k: row.get(k, "") for k in OUT_FIELDS})

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dist = report["failure_reason_distribution"]
    md = f"""# Aero vAMM Adapter Pilot

Status: research-only. No full Aero label, no V2 canonical/training merge, no strategy/executor production change, no promote, no LIVE.

## Gate Metrics

- attempted_rows: {report['attempted_rows']}
- ok_rows: {report['ok_rows']}
- success_rate: {report['success_rate']}
- reserve_success_rate: {report['reserve_success_rate']}
- quote_success_rate: {report['quote_success_rate']}
- estimated_rpc_calls_per_success: {report['estimated_rpc_calls_per_success']}
- sanity_clip_count: {report['sanity_clip_count']}

## Pool Type Validation

- stable_excluded_count: {report['stable_excluded_count']}
- cl_slipstream_excluded_count: {report['cl_slipstream_excluded_count']}
- unknown_excluded_count: {report['unknown_excluded_count']}
- stable_accidentally_accepted: {report['stable_accidentally_accepted']}
- cl_slipstream_accidentally_accepted: {report['cl_slipstream_accidentally_accepted']}
- unknown_accidentally_accepted: {report['unknown_accidentally_accepted']}

## Failure Taxonomy

{json.dumps(dist, indent=2, sort_keys=True)}

## RPC Budget

- rpc_call_count_by_method: {json.dumps(report['rpc_call_count_by_method'], sort_keys=True)}
- rpc_total_calls: {report['rpc_total_calls']}
- rpc_url_written_to_artifacts: false

## Conclusion

`{report['conclusion_enum']}`

Next step: {'1000-row Aero vAMM dry-run may proceed behind the same research-only gates.' if report['conclusion_enum'] == 'AERO_VAMM_ADAPTER_PILOT_GO_TO_1000_ROW_DRYRUN' else 'Do not proceed to 1000-row dry-run until the hold reason is resolved.'}
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")


def main() -> int:
    _load_local_env_if_needed()
    max_rows = min(100, max(1, int(os.environ.get("AERO_VAMM_PILOT_MAX_ROWS", "100"))))
    workers = min(2, max(1, int(os.environ.get("LABEL_WORKERS", "1"))))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", "500000000000000"))
    token0_notional = int(os.environ.get("AERO_VAMM_TOKEN0_NOTIONAL_RAW", "1000000000000000000"))
    exit_horizon_sec = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    max_rpc_calls = int(os.environ.get("AERO_VAMM_RPC_MAX_CALLS", "850"))
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""

    rows_out: list[dict[str, Any]] = []
    script_error = ""
    rpc = RPC(rpc_url, max_calls=max_rpc_calls) if rpc_url else None

    try:
        rows = _load_rows(max_rows)
        if rpc is None:
            script_error = "missing_rpc_url"
            for row in rows:
                rows_out.append(_base_out(row, "fail_rpc_error", 0, 0))
        else:
            chain_id = int(str(rpc.call("eth_chainId", "[]".replace("[]", "")) if False else rpc.call("eth_chainId", [])), 16)
            if chain_id != 8453:
                raise RuntimeError("unexpected_chain_id")
            latest_block = int(rpc.call("eth_blockNumber", []), 16)
            pool_cache: dict[str, PoolMeta] = {}
            pool_lock = threading.Lock()
            if workers == 1:
                for row in rows:
                    rows_out.append(
                        process_row(row, rpc, latest_block, pool_cache, pool_lock, notional_wei, token0_notional, exit_horizon_sec)
                    )
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = [
                        ex.submit(process_row, row, rpc, latest_block, pool_cache, pool_lock, notional_wei, token0_notional, exit_horizon_sec)
                        for row in rows
                    ]
                    for fut in as_completed(futures):
                        rows_out.append(fut.result())
                order = {(r.get("tx_hash", ""), r.get("log_index", "")): i for i, r in enumerate(rows)}
                rows_out.sort(key=lambda r: order.get((r.get("tx_hash", ""), r.get("log_index", "")), 10**9))
    except PilotAbort as exc:
        script_error = str(exc)
    except Exception as exc:
        script_error = type(exc).__name__

    attempted = len(rows_out)
    fail_dist: Counter[str] = Counter(r.get("failure_reason", "fail_script_failure") for r in rows_out)
    for reason in FAIL_REASONS:
        fail_dist.setdefault(reason, 0)
    ok_rows = fail_dist["ok"]
    reserve_ok = sum(1 for r in rows_out if r.get("reserves_status") == "ok")
    quote_ok = sum(1 for r in rows_out if r.get("quote_status") == "ok")
    sanity_clip = fail_dist["fail_sanity_clip"]
    rpc_total = rpc.total_calls if rpc is not None else 0
    calls_per_success = round(rpc_total / ok_rows, 6) if ok_rows else None

    stable_acc = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and r.get("stable_flag") == "true")
    cl_acc = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and "cl" in (r.get("pool_type") or "").lower())
    unk_acc = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and "unknown" in (r.get("pool_type") or "").lower())

    report: dict[str, Any] = {
        "title": "Aero vAMM Adapter Pilot",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "research_only": True,
        "input_csv": str(INPUT_CSV.relative_to(REPO)),
        "research_report": str(RESEARCH_JSON.relative_to(REPO)),
        "output_csv": str(OUT_CSV.relative_to(REPO)),
        "attempted_rows": attempted,
        "ok_rows": int(ok_rows),
        "success_rate": _pct(ok_rows, attempted),
        "failure_reason_distribution": dict(sorted((k, int(v)) for k, v in fail_dist.items())),
        "rpc_call_count_by_method": dict(sorted((rpc.method_counts if rpc is not None else Counter()).items())),
        "rpc_total_calls": int(rpc_total),
        "estimated_rpc_calls_per_success": calls_per_success,
        "stable_excluded_count": int(fail_dist["fail_stable_pool_excluded"]),
        "cl_slipstream_excluded_count": int(fail_dist["fail_cl_slipstream_excluded"]),
        "unknown_excluded_count": int(fail_dist["fail_unknown_pool_type"]),
        "quote_success_rate": _pct(quote_ok, attempted),
        "reserve_success_rate": _pct(reserve_ok, attempted),
        "sanity_clip_count": int(sanity_clip),
        "stable_accidentally_accepted": int(stable_acc),
        "cl_slipstream_accidentally_accepted": int(cl_acc),
        "unknown_accidentally_accepted": int(unk_acc),
        "max_rows": max_rows,
        "label_workers": workers,
        "rpc_budget_max_calls": max_rpc_calls,
        "rpc_url_written_to_artifacts": False,
        "script_error": script_error,
        "gate_thresholds": {
            "success_rate_min": 0.90,
            "quote_success_rate_min": 0.90,
            "reserve_success_rate_min": 0.95,
            "sanity_clip_max_fraction": 0.05,
            "rpc_calls_per_success_max": 8,
            "stable_accidentally_accepted_max": 0,
            "cl_slipstream_accidentally_accepted_max": 0,
        },
    }
    report["conclusion_enum"] = _conclusion(report)

    _write_outputs(rows_out, report)
    print(json.dumps({
        "attempted_rows": attempted,
        "ok_rows": ok_rows,
        "success_rate": report["success_rate"],
        "rpc_total_calls": rpc_total,
        "conclusion_enum": report["conclusion_enum"],
        "output_csv": str(OUT_CSV.relative_to(REPO)),
        "output_json": str(OUT_JSON.relative_to(REPO)),
        "output_md": str(OUT_MD.relative_to(REPO)),
    }, indent=2))
    return 0 if not script_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
