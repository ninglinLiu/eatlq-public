"""
Aero vAMM canonical label dry-run (2000 rows, research-only).

Default behavior:
  - PLAN mode (RUN_EXECUTE!=1): print execution plan only.
  - EXEC mode (RUN_EXECUTE=1): run bounded dry-run with strict gates.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent

INPUT_CANDIDATES = REPO / "logs" / "run004_candidates_swaponly_full.csv"
INPUT_REFERENCE_SMALL_SAMPLE = REPO / "logs" / "aero_vamm_adapter_1000_dryrun_output.csv"
INPUT_REFERENCE_DRYRUN_REPORT = REPO / "mvp" / "diagnostics" / "aero_vamm_adapter_1000_dryrun_report.json"

OUT_CSV = REPO / "logs" / "aero_vamm_canonical_label_dryrun_2000.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_canonical_label_dryrun_2000_report.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-canonical-label-dryrun-2000.md"

WETH_BASE = "0x4200000000000000000000000000000000000006"
AERO_VAMM_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_STABLE = "0x22be3de1"
SEL_DECIMALS = "0x313ce567"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_GET_AMOUNT_OUT = "0xf140a35a"  # getAmountOut(uint256,address)

FAILURE_TAXONOMY = [
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
    "fail_429_abort",
    "fail_duplicate_conflict",
]

OUT_FIELDS = [
    "sample_id",
    "tx_hash",
    "log_index",
    "pool_address",
    "pool_type",
    "stable_flag",
    "timestamp",
    "entry_block",
    "exit_block",
    "token0",
    "token1",
    "quote_token_in",
    "quote_token_out",
    "quote_amount_in",
    "estimated_entry_quote",
    "estimated_exit_quote",
    "net_return",
    "label",
    "adapter_status",
    "failure_reason",
    "reserves_status",
    "quote_status",
    "sanity_check_status",
    "rpc_calls_this_row",
    "reserve_weth_at_entry",
    "reserve_token_at_entry",
    "reserve_ratio",
    "log_reserve_weth",
    "liquidity_to_trade_ratio",
    "swap_size_vs_reserve",
    "price_impact_estimate_at_notional",
    "block_gas_fill_ratio",
    "block_tx_count",
    "is_new_pool_7d",
    "is_new_pool_30d",
]


@dataclass(frozen=True)
class DryrunConfig:
    max_rows: int = 2000
    label_workers: int = 2
    rpc_max_calls: int = 10000
    label_exit_horizon_sec: int = 1800
    label_notional_wei: int = 500000000000000
    round_trip_cost: float = 0.006
    abort_on_429: bool = True
    allow_infinite_retry: bool = False
    resume: bool = True


class RpcAbort(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolMeta:
    token0: str
    token1: str
    stable: bool
    token0_decimals: int
    token1_decimals: int


class RPC:
    def __init__(self, url: str, max_calls: int, abort_on_429: bool = True):
        self.url = url
        self.max_calls = max_calls
        self.abort_on_429 = abort_on_429
        self._id = 0
        self._lock = threading.Lock()
        self._aborted = threading.Event()
        self.method_counts: Counter[str] = Counter()
        self.abort_reason = ""

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(self.method_counts.values())

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: list[Any]) -> Any:
        if self._aborted.is_set():
            raise RpcAbort(self.abort_reason or "RPC_ABORTED")
        with self._lock:
            if sum(self.method_counts.values()) >= self.max_calls:
                self.abort_reason = "RPC_CALL_BUDGET_EXCEEDED"
                self._aborted.set()
                raise RpcAbort(self.abort_reason)
            self.method_counts[method] += 1
        req = Request(
            self.url,
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
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
        except HTTPError as exc:
            if exc.code == 429 and self.abort_on_429:
                self.abort_reason = "HTTP_429_ABORT"
                self._aborted.set()
                raise RpcAbort(self.abort_reason) from exc
            raise
        if isinstance(payload, dict) and payload.get("error"):
            msg = str(payload["error"].get("message", "rpc_error"))
            if "429" in msg and self.abort_on_429:
                self.abort_reason = "HTTP_429_ABORT"
                self._aborted.set()
                raise RpcAbort(self.abort_reason)
            raise RuntimeError(msg)
        return payload.get("result") if isinstance(payload, dict) else None


def _norm_addr(value: str) -> str:
    t = (value or "").strip().lower()
    if not t:
        return ""
    return t if t.startswith("0x") else f"0x{t}"


def _valid_addr(value: str) -> bool:
    t = _norm_addr(value)
    if len(t) != 42 or not t.startswith("0x"):
        return False
    try:
        int(t[2:], 16)
    except ValueError:
        return False
    return True


def _pct(a: int, b: int) -> float:
    return round(float(a) / float(b), 6) if b else 0.0


def _pad32(x: str) -> str:
    return x.lower().replace("0x", "").rjust(64, "0")


def _addr_arg(a: str) -> str:
    return _pad32(_norm_addr(a)[2:])


def _uint_arg(v: int) -> str:
    return _pad32(hex(max(0, int(v)))[2:])


def _decode_uint(result: str) -> int:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short_uint")
    return int(raw[0:64], 16)


def _decode_addr(result: str) -> str:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short_addr")
    return "0x" + raw[-40:].lower()


def _decode_bool(result: str) -> bool:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short_bool")
    return int(raw[-64:], 16) != 0


def _eth_call(rpc: RPC, to_addr: str, data: str, block_tag: int | str) -> str:
    bt = hex(block_tag) if isinstance(block_tag, int) else block_tag
    res = rpc.call("eth_call", [{"to": _norm_addr(to_addr), "data": data}, bt])
    if not isinstance(res, str) or res in {"", "0x"}:
        raise ValueError("empty_eth_call")
    return res


def call_pool_meta(rpc: RPC, pool: str) -> tuple[str, str, bool]:
    t0 = _decode_addr(_eth_call(rpc, pool, SEL_TOKEN0, "latest"))
    t1 = _decode_addr(_eth_call(rpc, pool, SEL_TOKEN1, "latest"))
    st = _decode_bool(_eth_call(rpc, pool, SEL_STABLE, "latest"))
    return t0, t1, st


def call_decimals(rpc: RPC, token: str) -> int:
    return max(0, min(_decode_uint(_eth_call(rpc, token, SEL_DECIMALS, "latest")), 36))


def call_reserves(rpc: RPC, pool: str, block: int) -> tuple[int, int]:
    raw = _eth_call(rpc, pool, SEL_GET_RESERVES, block).replace("0x", "")
    if len(raw) < 128:
        raise ValueError("short_reserves")
    return int(raw[0:64], 16), int(raw[64:128], 16)


def call_get_amount_out(rpc: RPC, pool: str, amount_in: int, token_in: str, block: int) -> int:
    data = SEL_GET_AMOUNT_OUT + _uint_arg(amount_in) + _addr_arg(token_in)
    return _decode_uint(_eth_call(rpc, pool, data, block))


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


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default


def _classify_candidate(row: dict[str, str]) -> tuple[bool, str]:
    ptype = (row.get("pool_type") or "").lower().strip()
    topic0 = (row.get("topic0") or "").lower().strip()
    ek = (row.get("event_kind") or "").lower().strip()
    reject = (row.get("reject_reason") or "").lower().strip()
    if "slipstream" in ptype or "slipstream" in ek:
        return False, "fail_cl_slipstream_excluded"
    if ptype == "aero_cl" or "aero_cl" in ptype:
        return False, "fail_cl_slipstream_excluded"
    if not ptype or ptype in {"unknown", "none"} or "unknown_pool_type" in reject or "unresolved" in reject:
        return False, "fail_unknown_pool_type"
    if ptype != "aero_vamm":
        return False, "fail_not_aero_vamm"
    if topic0 != AERO_VAMM_SWAP_TOPIC:
        return False, "fail_not_aero_vamm"
    if ek and "swap" not in ek:
        return False, "fail_not_aero_vamm"
    return True, "ok"


def _canonical_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        (row.get("tx_hash") or "").lower().strip(),
        (row.get("log_index") or "").strip(),
        _norm_addr(row.get("pool_address", "")),
    )


def _conflicts(a: dict[str, str], b: dict[str, str]) -> bool:
    keys = ("timestamp", "pool_type", "topic0", "event_kind", "candidate_status", "reject_reason")
    return any((a.get(k) or "").strip() != (b.get(k) or "").strip() for k in keys)


def _evenly_pick(rows: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    if n <= 0:
        return []
    if n >= len(rows):
        return sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
    ordered = sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
    out: list[dict[str, str]] = []
    last = len(ordered) - 1
    used: set[int] = set()
    for i in range(n):
        idx = round(i * last / max(1, n - 1))
        while idx in used and idx < last:
            idx += 1
        while idx in used and idx > 0:
            idx -= 1
        used.add(idx)
        out.append(ordered[idx])
    return sorted(out, key=lambda r: _parse_int(r.get("timestamp", "0")))


def _build_sample(max_rows: int) -> tuple[list[dict[str, str]], dict[str, int], dict[str, int], int, int, Counter[str]]:
    if not INPUT_CANDIDATES.is_file():
        raise FileNotFoundError(str(INPUT_CANDIDATES))
    source_total = 0
    excluded = Counter()
    dedup: dict[tuple[str, str, str], dict[str, str]] = {}
    dup_count = 0
    dup_conflict = 0

    with INPUT_CANDIDATES.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            source_total += 1
            ok, reason = _classify_candidate(row)
            if not ok:
                excluded[reason] += 1
                continue
            key = _canonical_key(row)
            if not all(key) or not _valid_addr(key[2]):
                excluded["fail_token_metadata_missing"] += 1
                continue
            if key in dedup:
                dup_count += 1
                if _conflicts(dedup[key], row):
                    dup_conflict += 1
                continue
            dedup[key] = row

    by_pool: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in dedup.values():
        by_pool[_norm_addr(row.get("pool_address", ""))].append(row)
    available = {k: len(v) for k, v in by_pool.items()}

    pools = sorted(by_pool.keys(), key=lambda p: len(by_pool[p]), reverse=True)
    sample: list[dict[str, str]] = []
    selected: dict[str, int] = {}
    if len(pools) >= 2:
        p0, p1 = pools[0], pools[1]
        t0 = min(max_rows // 2, len(by_pool[p0]))
        t1 = min(max_rows // 2, len(by_pool[p1]))
        sample += _evenly_pick(by_pool[p0], t0)
        sample += _evenly_pick(by_pool[p1], t1)
        selected[p0], selected[p1] = t0, t1
        rem = max_rows - len(sample)
        if rem > 0:
            for p in pools:
                if rem <= 0:
                    break
                already = selected.get(p, 0)
                left = len(by_pool[p]) - already
                if left <= 0:
                    continue
                take = min(left, rem)
                sample += _evenly_pick(by_pool[p][already:], take)
                selected[p] = already + take
                rem -= take
    elif len(pools) == 1:
        p = pools[0]
        take = min(max_rows, len(by_pool[p]))
        sample += _evenly_pick(by_pool[p], take)
        selected[p] = take

    sample = sorted(sample, key=lambda r: _parse_int(r.get("timestamp", "0")))[:max_rows]
    for i, row in enumerate(sample, 1):
        row["sample_id"] = str(i)
    selected = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in sample))
    return sample, selected, available, dup_count, dup_conflict, excluded


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aero vAMM canonical label dry-run.")
    p.add_argument("--max-rows", type=int, default=int(os.environ.get("AERO_VAMM_CANONICAL_MAX_ROWS", "2000")))
    p.add_argument("--workers", type=int, default=int(os.environ.get("LABEL_WORKERS", "2")))
    p.add_argument("--rpc-max-calls", type=int, default=int(os.environ.get("AERO_VAMM_RPC_MAX_CALLS", "10000")))
    p.add_argument("--emit-plan-json", type=str, default="")
    return p.parse_args()


def build_plan(cfg: DryrunConfig) -> dict[str, Any]:
    return {
        "title": "Aero vAMM canonical label dry-run plan",
        "status": "plan_only",
        "inputs": {
            "candidates": str(INPUT_CANDIDATES.relative_to(REPO)),
            "ref_1000_csv": str(INPUT_REFERENCE_SMALL_SAMPLE.relative_to(REPO)),
            "ref_1000_report": str(INPUT_REFERENCE_DRYRUN_REPORT.relative_to(REPO)),
        },
        "outputs": {
            "csv": str(OUT_CSV.relative_to(REPO)),
            "json": str(OUT_JSON.relative_to(REPO)),
            "md": str(OUT_MD.relative_to(REPO)),
        },
        "config": {
            "max_rows": cfg.max_rows,
            "label_workers": cfg.label_workers,
            "rpc_max_calls": cfg.rpc_max_calls,
            "abort_on_429": cfg.abort_on_429,
            "resume": cfg.resume,
        },
        "taxonomies": FAILURE_TAXONOMY,
    }


def _base_failed(row: dict[str, str], reason: str, rpc_delta: int) -> dict[str, Any]:
    out = {k: "" for k in OUT_FIELDS}
    out["sample_id"] = row.get("sample_id", "")
    out["tx_hash"] = (row.get("tx_hash") or "").lower()
    out["log_index"] = row.get("log_index", "")
    out["pool_address"] = _norm_addr(row.get("pool_address", ""))
    out["pool_type"] = (row.get("pool_type") or "")
    out["timestamp"] = row.get("timestamp", "")
    out["adapter_status"] = "failed"
    out["failure_reason"] = reason
    out["rpc_calls_this_row"] = str(max(0, rpc_delta))
    out["reserves_status"] = "not_attempted"
    out["quote_status"] = "not_attempted"
    out["sanity_check_status"] = "not_attempted"
    return out


def _quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p05": None, "p25": None, "median": None, "p75": None, "p95": None, "max": None, "mean": None}
    vals = sorted(values)
    n = len(vals)

    def q(p: float) -> float:
        idx = min(n - 1, max(0, int(round((n - 1) * p))))
        return vals[idx]

    return {
        "min": vals[0],
        "p05": q(0.05),
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "p95": q(0.95),
        "max": vals[-1],
        "mean": round(sum(vals) / n, 12),
    }


def _gate_table(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    attempted = report["attempted_rows"]
    g = {
        "attempted_rows_eq_2000": attempted == 2000,
        "success_rate_ge_095": report["success_rate"] >= 0.95,
        "positive_rate_between_010_090": 0.10 <= report["positive_rate"] <= 0.90,
        "duplicate_conflict_count_eq_0": report["duplicate_conflict_count"] == 0,
        "reserve_success_rate_ge_095": report["reserve_success_rate"] >= 0.95,
        "quote_success_rate_ge_095": report["quote_success_rate"] >= 0.95,
        "sanity_clip_count_le_5pct": report["sanity_clip_count"] <= int(round(max(1, attempted) * 0.05)),
        "calls_per_success_le_8": (report["calls_per_success"] is not None and report["calls_per_success"] <= 8),
        "total_rpc_calls_le_10000": report["total_rpc_calls"] <= 10000,
        "stable_accepted_eq_0": report["stable_accepted_count"] == 0,
        "cl_slipstream_accepted_eq_0": report["cl_slipstream_accepted_count"] == 0,
        "unknown_accepted_eq_0": report["unknown_accepted_count"] == 0,
        "net_return_distribution_sane": report["net_return_distribution_sane"],
        "no_rpc_secret_leakage": report["rpc_secret_leakage_detected"] is False,
    }
    return {k: {"pass": bool(v)} for k, v in g.items()}


def _conclusion(report: dict[str, Any]) -> str:
    if report["script_error"]:
        if "429" in report["script_error"] or "RPC_CALL_BUDGET_EXCEEDED" in report["script_error"]:
            return "HOLD_DUE_TO_RPC_LIMIT"
        return "HOLD_DUE_TO_SCRIPT_FAILURE"
    if report["duplicate_conflict_count"] > 0:
        return "HOLD_DUE_TO_DUPLICATE_CONFLICT"
    if report["stable_accepted_count"] or report["cl_slipstream_accepted_count"] or report["unknown_accepted_count"]:
        return "HOLD_DUE_TO_POOL_TYPE_LEAKAGE"
    if report["success_rate"] < 0.95:
        return "HOLD_DUE_TO_SUCCESS_RATE"
    if not (0.10 <= report["positive_rate"] <= 0.90):
        return "HOLD_DUE_TO_POSITIVE_RATE_DISTRIBUTION"
    if report["reserve_success_rate"] < 0.95:
        return "HOLD_DUE_TO_RESERVE_FAILURE"
    if report["quote_success_rate"] < 0.95:
        return "HOLD_DUE_TO_QUOTE_FAILURE"
    if report["sanity_clip_count"] > int(round(max(1, report["attempted_rows"]) * 0.05)):
        return "HOLD_DUE_TO_SANITY_FAILURE"
    if report["calls_per_success"] is None or report["calls_per_success"] > 8 or report["total_rpc_calls"] > 10000:
        return "HOLD_DUE_TO_RPC_COST"
    # Conservative progression: 2000-row pass should go to 5000-row first.
    return "AERO_VAMM_2000_DRYRUN_GO_TO_5000_DRYRUN"


def main() -> int:
    args = parse_args()
    cfg = DryrunConfig(
        max_rows=max(1, min(2000, args.max_rows)),
        label_workers=max(1, min(2, args.workers)),
        rpc_max_calls=max(1000, args.rpc_max_calls),
        abort_on_429=(os.environ.get("AERO_VAMM_ABORT_ON_429", "1") != "0"),
        resume=(os.environ.get("AERO_VAMM_RESUME", "1") != "0"),
    )
    plan = build_plan(cfg)
    if os.environ.get("RUN_EXECUTE", "0") != "1":
        print(json.dumps(plan, indent=2))
        if args.emit_plan_json:
            Path(args.emit_plan_json).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        return 0

    _load_local_env_if_needed()
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    if not rpc_url:
        print("missing RPC URL (LABEL_RPC_URL/EVM_HTTP_URL)")
        return 2

    sample_rows, selected_dist, available_dist, duplicate_key_count, duplicate_conflict_count, source_excluded = _build_sample(cfg.max_rows)
    resume_rows: list[dict[str, Any]] = []
    done_keys: set[tuple[str, str, str]] = set()
    if cfg.resume and OUT_CSV.is_file():
        with OUT_CSV.open("r", encoding="utf-8") as rf:
            for row in csv.DictReader(rf):
                resume_rows.append(row)
                done_keys.add((row.get("tx_hash", ""), row.get("log_index", ""), _norm_addr(row.get("pool_address", ""))))
    pending_rows = [r for r in sample_rows if _canonical_key(r) not in done_keys]

    rpc = RPC(rpc_url, max_calls=cfg.rpc_max_calls, abort_on_429=cfg.abort_on_429)
    rows_out: list[dict[str, Any]] = list(resume_rows)
    script_error = ""

    lock = threading.Lock()
    pool_cache: dict[str, PoolMeta] = {}
    token_decimals_cache: dict[str, int] = {}
    receipt_cache: dict[str, int] = {}
    reserve_cache: dict[tuple[str, int], tuple[int, int]] = {}
    quote_cache: dict[tuple[str, int, int, str], int] = {}

    try:
        chain_id = int(rpc.call("eth_chainId", []), 16)
        if chain_id != 8453:
            script_error = "CHAIN_ID_MISMATCH"
            raise RuntimeError(script_error)
        latest_block = int(rpc.call("eth_blockNumber", []), 16)

        def run_one(row: dict[str, str]) -> dict[str, Any]:
            before = rpc.total_calls
            try:
                key = _canonical_key(row)
                if not all(key):
                    return _base_failed(row, "fail_token_metadata_missing", rpc.total_calls - before)
                receipt_block = None
                with lock:
                    receipt_block = receipt_cache.get(key[0])
                if receipt_block is None:
                    receipt = rpc.call("eth_getTransactionReceipt", [key[0]])
                    if not receipt or not receipt.get("blockNumber"):
                        return _base_failed(row, "fail_before_creation", rpc.total_calls - before)
                    receipt_block = int(receipt["blockNumber"], 16)
                    with lock:
                        receipt_cache[key[0]] = receipt_block
                if receipt_block <= 0 or receipt_block > latest_block:
                    return _base_failed(row, "fail_before_creation", rpc.total_calls - before)
                exit_block = min(latest_block, receipt_block + max(1, cfg.label_exit_horizon_sec // 2))

                pool = key[2]
                with lock:
                    pm = pool_cache.get(pool)
                if pm is None:
                    t0, t1, stable = call_pool_meta(rpc, pool)
                    if stable:
                        return _base_failed(row, "fail_stable_pool_excluded", rpc.total_calls - before)
                    d0 = token_decimals_cache.get(t0)
                    if d0 is None:
                        d0 = call_decimals(rpc, t0)
                        with lock:
                            token_decimals_cache[t0] = d0
                    d1 = token_decimals_cache.get(t1)
                    if d1 is None:
                        d1 = call_decimals(rpc, t1)
                        with lock:
                            token_decimals_cache[t1] = d1
                    pm = PoolMeta(token0=t0, token1=t1, stable=False, token0_decimals=d0, token1_decimals=d1)
                    with lock:
                        pool_cache[pool] = pm
                if pm.stable:
                    return _base_failed(row, "fail_stable_pool_excluded", rpc.total_calls - before)

                rk = (pool, receipt_block)
                with lock:
                    reserves = reserve_cache.get(rk)
                if reserves is None:
                    reserves = call_reserves(rpc, pool, receipt_block)
                    with lock:
                        reserve_cache[rk] = reserves
                r0, r1 = reserves
                if r0 <= 0 or r1 <= 0:
                    return _base_failed(row, "fail_reserve_unavailable", rpc.total_calls - before)

                if pm.token0 == WETH_BASE:
                    token_in, token_out, amount_in = pm.token0, pm.token1, cfg.label_notional_wei
                    reserve_weth, reserve_token = r0, r1
                elif pm.token1 == WETH_BASE:
                    token_in, token_out, amount_in = pm.token1, pm.token0, cfg.label_notional_wei
                    reserve_weth, reserve_token = r1, r0
                else:
                    token_in, token_out = pm.token0, pm.token1
                    amount_in = 10 ** pm.token0_decimals if pm.token0_decimals > 0 else cfg.label_notional_wei
                    reserve_weth, reserve_token = 0, r0

                qk1 = (pool, receipt_block, amount_in, token_in)
                with lock:
                    q1 = quote_cache.get(qk1)
                if q1 is None:
                    q1 = call_get_amount_out(rpc, pool, amount_in, token_in, receipt_block)
                    with lock:
                        quote_cache[qk1] = q1
                if q1 <= 0:
                    o = _base_failed(row, "fail_quote_unavailable", rpc.total_calls - before)
                    o["reserves_status"] = "ok"
                    o["quote_status"] = "failed"
                    return o

                qk2 = (pool, exit_block, q1, token_out)
                with lock:
                    q2 = quote_cache.get(qk2)
                if q2 is None:
                    q2 = call_get_amount_out(rpc, pool, q1, token_out, exit_block)
                    with lock:
                        quote_cache[qk2] = q2
                if q2 <= 0:
                    o = _base_failed(row, "fail_quote_unavailable", rpc.total_calls - before)
                    o["reserves_status"] = "ok"
                    o["quote_status"] = "failed"
                    return o

                net = (q2 / amount_in) - 1.0 - cfg.round_trip_cost
                if not math.isfinite(net) or net < -1.0 or net > 10.0:
                    o = _base_failed(row, "fail_sanity_clip", rpc.total_calls - before)
                    o["reserves_status"] = "ok"
                    o["quote_status"] = "ok"
                    o["sanity_check_status"] = "failed"
                    return o
                label = 1 if net > 0.0 else 0

                reserve_ratio = (reserve_weth / reserve_token) if reserve_token > 0 else 0.0
                log_reserve_weth = math.log10(reserve_weth) if reserve_weth > 0 else None
                liquidity_to_trade_ratio = (reserve_weth / amount_in) if amount_in > 0 and reserve_weth > 0 else 0.0
                swap_size_vs_reserve = (amount_in / reserve_weth) if reserve_weth > 0 else 0.0
                spot_out = (amount_in * reserve_token / reserve_weth) if reserve_weth > 0 else 0.0
                price_impact = (1.0 - (q1 / spot_out)) if spot_out > 0 else 0.0

                return {
                    "sample_id": row.get("sample_id", ""),
                    "tx_hash": key[0],
                    "log_index": key[1],
                    "pool_address": key[2],
                    "pool_type": "aero_vamm",
                    "stable_flag": "false",
                    "timestamp": row.get("timestamp", ""),
                    "entry_block": str(receipt_block),
                    "exit_block": str(exit_block),
                    "token0": pm.token0,
                    "token1": pm.token1,
                    "quote_token_in": token_in,
                    "quote_token_out": token_out,
                    "quote_amount_in": str(amount_in),
                    "estimated_entry_quote": str(q1),
                    "estimated_exit_quote": str(q2),
                    "net_return": f"{net:.12f}",
                    "label": str(label),
                    "adapter_status": "ok",
                    "failure_reason": "ok",
                    "reserves_status": "ok",
                    "quote_status": "ok",
                    "sanity_check_status": "ok",
                    "rpc_calls_this_row": str(max(0, rpc.total_calls - before)),
                    "reserve_weth_at_entry": str(reserve_weth),
                    "reserve_token_at_entry": str(reserve_token),
                    "reserve_ratio": f"{reserve_ratio:.12f}",
                    "log_reserve_weth": f"{log_reserve_weth:.12f}" if log_reserve_weth is not None else "",
                    "liquidity_to_trade_ratio": f"{liquidity_to_trade_ratio:.12f}",
                    "swap_size_vs_reserve": f"{swap_size_vs_reserve:.12f}",
                    "price_impact_estimate_at_notional": f"{price_impact:.12f}",
                    "block_gas_fill_ratio": "",
                    "block_tx_count": "",
                    "is_new_pool_7d": "",
                    "is_new_pool_30d": "",
                }
            except RpcAbort as exc:
                raise exc
            except Exception:
                return _base_failed(row, "fail_rpc_error", rpc.total_calls - before)

        if cfg.label_workers == 1:
            for row in pending_rows:
                try:
                    rows_out.append(run_one(row))
                except RpcAbort as exc:
                    script_error = str(exc)
                    rows_out.append(_base_failed(row, "fail_429_abort", 0))
                    for rem in pending_rows[pending_rows.index(row) + 1:]:
                        rows_out.append(_base_failed(rem, "fail_429_abort", 0))
                    break
        else:
            idx_map = {id(r): i for i, r in enumerate(pending_rows)}
            with ThreadPoolExecutor(max_workers=cfg.label_workers) as ex:
                futs = {ex.submit(run_one, row): row for row in pending_rows}
                aborted = False
                tmp: dict[int, dict[str, Any]] = {}
                for fut in as_completed(futs):
                    row = futs[fut]
                    idx = idx_map[id(row)]
                    try:
                        tmp[idx] = fut.result()
                    except RpcAbort as exc:
                        aborted = True
                        script_error = str(exc)
                        tmp[idx] = _base_failed(row, "fail_429_abort", 0)
                    except Exception:
                        tmp[idx] = _base_failed(row, "fail_rpc_error", 0)
                if aborted:
                    for i, row in enumerate(pending_rows):
                        if i not in tmp:
                            tmp[i] = _base_failed(row, "fail_429_abort", 0)
                for i in range(len(pending_rows)):
                    rows_out.append(tmp[i])

    except Exception as exc:
        if not script_error:
            script_error = type(exc).__name__

    rows_out = sorted(rows_out, key=lambda r: _parse_int(r.get("sample_id", "0")))
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as wf:
        writer = csv.DictWriter(wf, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for row in rows_out:
            writer.writerow({k: row.get(k, "") for k in OUT_FIELDS})

    attempted = len(rows_out)
    success_rows = sum(1 for r in rows_out if r.get("failure_reason") == "ok")
    failed_rows = attempted - success_rows
    fail_dist = Counter(r.get("failure_reason", "fail_rpc_error") for r in rows_out)
    for reason in FAILURE_TAXONOMY:
        fail_dist.setdefault(reason, 0)

    labels = [int(r.get("label", "0")) for r in rows_out if r.get("failure_reason") == "ok" and (r.get("label") or "").isdigit()]
    positive_count = sum(1 for x in labels if x == 1)
    negative_count = sum(1 for x in labels if x == 0)
    positive_rate = _pct(positive_count, len(labels))

    net_vals = []
    for r in rows_out:
        try:
            if r.get("failure_reason") == "ok":
                net_vals.append(float(r.get("net_return", "")))
        except Exception:
            pass
    net_stats = _quantiles(net_vals)
    net_sane = all((-1.0 <= x <= 10.0) for x in net_vals) if net_vals else False

    reserve_success = sum(1 for r in rows_out if r.get("reserves_status") == "ok")
    quote_success = sum(1 for r in rows_out if r.get("quote_status") == "ok")
    sanity_clip_count = int(fail_dist["fail_sanity_clip"])

    pool_dist = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in rows_out if r.get("pool_address")))
    vals = sorted(pool_dist.values(), reverse=True)
    top1 = _pct(vals[0], attempted) if vals else 0.0
    top2 = _pct(sum(vals[:2]), attempted) if vals else 0.0

    stable_accepted = sum(1 for r in rows_out if r.get("failure_reason") == "ok" and str(r.get("stable_flag", "")).lower() in {"true", "1", "yes"})
    cl_accepted = sum(1 for r in rows_out if r.get("failure_reason") == "ok" and "cl" in (r.get("pool_type") or "").lower())
    unknown_accepted = sum(1 for r in rows_out if r.get("failure_reason") == "ok" and "unknown" in (r.get("pool_type") or "").lower())

    calls_per_success = round(rpc.total_calls / success_rows, 6) if success_rows > 0 else None
    est_5000_calls = int(round((calls_per_success or 0.0) * 5000)) if calls_per_success is not None else None
    est_full_calls = int(round((calls_per_success or 0.0) * 22024)) if calls_per_success is not None else None

    rpc_secret_leakage = False
    for candidate in (json.dumps(rows_out[:5]),):
        if "http://" in candidate.lower() or "https://" in candidate.lower():
            rpc_secret_leakage = True

    report = {
        "title": "Aero vAMM canonical label dry-run 2000",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "attempted_rows": attempted,
        "success_rows": success_rows,
        "failed_rows": failed_rows,
        "success_rate": _pct(success_rows, attempted),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "positive_rate": positive_rate,
        "net_return_stats": net_stats,
        "net_return_distribution_sane": net_sane,
        "duplicate_key_count": duplicate_key_count,
        "duplicate_conflict_count": duplicate_conflict_count,
        "pool_distribution": pool_dist,
        "top1_pool_share": top1,
        "top2_pool_share": top2,
        "stable_accepted_count": stable_accepted,
        "cl_slipstream_accepted_count": cl_accepted,
        "unknown_accepted_count": unknown_accepted,
        "reserve_success_rate": _pct(reserve_success, attempted),
        "quote_success_rate": _pct(quote_success, attempted),
        "sanity_clip_count": sanity_clip_count,
        "failure_reason_distribution": dict(sorted((k, int(v)) for k, v in fail_dist.items())),
        "rpc_call_count_by_method": dict(sorted((k, int(v)) for k, v in rpc.method_counts.items())),
        "total_rpc_calls": int(rpc.total_calls),
        "calls_per_success": calls_per_success,
        "estimated_rpc_calls_5000": est_5000_calls,
        "estimated_rpc_calls_22024": est_full_calls,
        "csv_canonical_trainable_later": False,  # set below by gates
        "selected_distribution_targeted": selected_dist,
        "available_distribution": available_dist,
        "source_excluded_counts": dict(sorted((k, int(v)) for k, v in source_excluded.items())),
        "script_error": script_error,
        "rpc_secret_leakage_detected": rpc_secret_leakage,
        "artifacts": {
            "csv": str(OUT_CSV.relative_to(REPO)),
            "json": str(OUT_JSON.relative_to(REPO)),
            "md": str(OUT_MD.relative_to(REPO)),
        },
    }
    report["gate_results"] = _gate_table(report)
    report["conclusion_enum"] = _conclusion(report)
    report["csv_canonical_trainable_later"] = all(v["pass"] for v in report["gate_results"].values())

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    md = f"""# Aero vAMM Canonical Label Dry-run 2000

Status: research-only.

## Core
- attempted/success/failed/success_rate: {attempted}/{success_rows}/{failed_rows}/{report['success_rate']}
- positive_count/negative_count/positive_rate: {positive_count}/{negative_count}/{positive_rate}
- reserve_success_rate: {report['reserve_success_rate']}
- quote_success_rate: {report['quote_success_rate']}
- sanity_clip_count: {sanity_clip_count}

## Distribution
- pool_distribution: {json.dumps(pool_dist, sort_keys=True)}
- top1_pool_share/top2_pool_share: {top1}/{top2}

## RPC
- rpc_call_count_by_method: {json.dumps(report['rpc_call_count_by_method'], sort_keys=True)}
- total_rpc_calls: {report['total_rpc_calls']}
- calls_per_success: {report['calls_per_success']}
- estimated_rpc_calls_5000: {report['estimated_rpc_calls_5000']}
- estimated_rpc_calls_22024: {report['estimated_rpc_calls_22024']}

## Failure Taxonomy
{json.dumps(report['failure_reason_distribution'], indent=2, sort_keys=True)}

## Gates
{json.dumps(report['gate_results'], indent=2, sort_keys=True)}

## Conclusion
`{report['conclusion_enum']}`
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")

    print(json.dumps({
        "attempted_rows": attempted,
        "success_rows": success_rows,
        "failed_rows": failed_rows,
        "success_rate": report["success_rate"],
        "positive_rate": positive_rate,
        "total_rpc_calls": report["total_rpc_calls"],
        "calls_per_success": report["calls_per_success"],
        "conclusion_enum": report["conclusion_enum"],
        "output_csv": report["artifacts"]["csv"],
        "output_json": report["artifacts"]["json"],
        "output_md": report["artifacts"]["md"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
