"""
Aero vAMM 60m-only 2000-row cost sensitivity rerun (research-only).

Plan mode:
  RUN_EXECUTE!=1 -> print plan only.

Execute mode:
  RUN_EXECUTE=1 -> run bounded experiment, no training/no promote/no live.
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
INPUT_PILOT_500_REPORT = REPO / "mvp" / "diagnostics" / "aero_vamm_horizon_cost_pilot_500_report.json"
INPUT_RERUN_2000_REPORT = REPO / "mvp" / "diagnostics" / "aero_vamm_60m_cost_sensitivity_2000_report.json"

OUT_ROWS_TAG = os.environ.get("AERO_VAMM_OUTPUT_ROWS", "2000")
OUT_CSV = REPO / "logs" / f"aero_vamm_60m_cost_sensitivity_{OUT_ROWS_TAG}.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / f"aero_vamm_60m_cost_sensitivity_{OUT_ROWS_TAG}_report.json"
OUT_MD = REPO / "mvp" / "runs" / f"aero-vamm-60m-cost-sensitivity-{OUT_ROWS_TAG}.md"

WETH_BASE = "0x4200000000000000000000000000000000000006"
AERO_VAMM_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_STABLE = "0x22be3de1"
SEL_DECIMALS = "0x313ce567"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_GET_AMOUNT_OUT = "0xf140a35a"  # getAmountOut(uint256,address)

HORIZON_NAME = "60m"
HORIZON_SEC = 3600
COSTS = [0.0, 0.002, 0.004, 0.006, 0.008]

FAILURES = [
    "ok",
    "fail_not_aero_vamm",
    "fail_stable_pool_excluded",
    "fail_cl_slipstream_excluded",
    "fail_unknown_pool_type",
    "fail_token_metadata_missing",
    "fail_before_creation",
    "fail_quote_unavailable",
    "fail_rpc_error",
    "fail_429_abort",
]


@dataclass(frozen=True)
class Config:
    max_rows: int = 5000
    workers: int = 2
    rpc_max_calls: int = 9000
    block_time_sec: int = 2
    abort_on_429: bool = True


@dataclass(frozen=True)
class PoolMeta:
    token0: str
    token1: str
    stable: bool
    token0_decimals: int
    token1_decimals: int


class RpcAbort(RuntimeError):
    pass


class RPC:
    def __init__(self, url: str, max_calls: int, abort_on_429: bool = True):
        self.url = url
        self.max_calls = max_calls
        self.abort_on_429 = abort_on_429
        self._id = 0
        self._lock = threading.Lock()
        self._aborted = threading.Event()
        self.abort_reason = ""
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
            raise RpcAbort(self.abort_reason or "RPC_ABORTED")
        with self._lock:
            if sum(self.method_counts.values()) >= self.max_calls:
                self.abort_reason = "RPC_CALL_BUDGET_EXCEEDED"
                self._aborted.set()
                raise RpcAbort(self.abort_reason)
            self.method_counts[method] += 1
        req = Request(
            self.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params, "id": self._next_id()}
            ).encode("utf-8"),
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


def _norm_addr(v: str) -> str:
    t = (v or "").strip().lower()
    if not t:
        return ""
    return t if t.startswith("0x") else f"0x{t}"


def _valid_addr(v: str) -> bool:
    t = _norm_addr(v)
    if len(t) != 42 or not t.startswith("0x"):
        return False
    try:
        int(t[2:], 16)
    except ValueError:
        return False
    return True


def _pct(a: int, b: int) -> float:
    return round(float(a) / float(b), 6) if b else 0.0


def _parse_int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: str) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _pad32(x: str) -> str:
    return x.lower().replace("0x", "").rjust(64, "0")


def _uint_arg(v: int) -> str:
    return _pad32(hex(max(0, int(v)))[2:])


def _addr_arg(a: str) -> str:
    return _pad32(_norm_addr(a)[2:])


def _decode_uint(result: str) -> int:
    raw = (result or "").replace("0x", "")
    if len(raw) < 64:
        raise ValueError("short_uint")
    return int(raw[:64], 16)


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


def call_reserves(rpc: RPC, pool: str, block_num: int) -> tuple[int, int]:
    raw = _eth_call(rpc, pool, SEL_GET_RESERVES, block_num).replace("0x", "")
    if len(raw) < 128:
        raise ValueError("short_reserves")
    return int(raw[0:64], 16), int(raw[64:128], 16)


def call_get_amount_out(rpc: RPC, pool: str, amount_in: int, token_in: str, block_num: int) -> int:
    data = SEL_GET_AMOUNT_OUT + _uint_arg(amount_in) + _addr_arg(token_in)
    return _decode_uint(_eth_call(rpc, pool, data, block_num))


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


def _load_rpc_url() -> str:
    _load_local_env_if_needed()
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""


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


def _assign_quartiles(rows: list[dict[str, str]]) -> None:
    ordered = sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
    n = len(ordered)
    for i, row in enumerate(ordered):
        q = min(3, int((4 * i) / max(1, n)))
        row["_quartile"] = f"Q{q + 1}"


def _evenly_pick(rows: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    ordered = sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
    if n <= 0:
        return []
    if n >= len(ordered):
        return ordered
    out: list[dict[str, str]] = []
    used: set[int] = set()
    last = len(ordered) - 1
    for i in range(n):
        idx = round(i * last / max(1, n - 1))
        while idx in used and idx < last:
            idx += 1
        while idx in used and idx > 0:
            idx -= 1
        used.add(idx)
        out.append(ordered[idx])
    return sorted(out, key=lambda r: _parse_int(r.get("timestamp", "0")))


def _build_sample(max_rows: int) -> tuple[list[dict[str, str]], dict[str, int], dict[str, int], dict[str, int], Counter[str]]:
    if not INPUT_CANDIDATES.is_file():
        raise FileNotFoundError(str(INPUT_CANDIDATES))
    dedup: dict[tuple[str, str, str], dict[str, str]] = {}
    excluded = Counter()
    with INPUT_CANDIDATES.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ok, reason = _classify_candidate(row)
            if not ok:
                excluded[reason] += 1
                continue
            key = _canonical_key(row)
            if not all(key) or not _valid_addr(key[2]):
                excluded["fail_token_metadata_missing"] += 1
                continue
            if key in dedup:
                continue
            dedup[key] = row

    all_rows = list(dedup.values())
    _assign_quartiles(all_rows)
    by_pool: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in all_rows:
        by_pool[_norm_addr(row.get("pool_address", ""))].append(row)
    pools = sorted(by_pool.keys(), key=lambda p: len(by_pool[p]), reverse=True)
    if len(pools) < 2:
        raise RuntimeError("INSUFFICIENT_POOLS_FOR_BALANCED_SAMPLE")
    pa, pb = pools[0], pools[1]

    per_quartile_target = max_rows // 4
    a_major = (per_quartile_target + 1) // 2
    b_major = per_quartile_target - a_major
    quotas = {
        ("Q1", pa): a_major,
        ("Q1", pb): b_major,
        ("Q2", pa): b_major,
        ("Q2", pb): a_major,
        ("Q3", pa): a_major,
        ("Q3", pb): b_major,
        ("Q4", pa): b_major,
        ("Q4", pb): a_major,
    }
    sample: list[dict[str, str]] = []
    selected: set[tuple[str, str, str]] = set()
    # phase 1: strict matrix
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        for p in [pa, pb]:
            bucket = [r for r in by_pool[p] if r.get("_quartile") == q]
            for row in _evenly_pick(bucket, min(quotas[(q, p)], len(bucket))):
                k = _canonical_key(row)
                if k in selected:
                    continue
                selected.add(k)
                sample.append(row)
    # phase 2: quartile top-up
    quartile_target = {
        "Q1": per_quartile_target,
        "Q2": per_quartile_target,
        "Q3": per_quartile_target,
        "Q4": per_quartile_target,
    }
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        have = sum(1 for r in sample if r.get("_quartile") == q)
        need = max(0, quartile_target[q] - have)
        if need <= 0:
            continue
        bucket = [r for r in all_rows if r.get("_quartile") == q and _canonical_key(r) not in selected]
        for row in _evenly_pick(bucket, min(need, len(bucket))):
            k = _canonical_key(row)
            selected.add(k)
            sample.append(row)
    # phase 3: pool top-up
    per_pool_target = max_rows // 2
    pool_target = {pa: per_pool_target, pb: per_pool_target}
    by_pool_now = Counter(_norm_addr(r.get("pool_address", "")) for r in sample)
    rem = max_rows - len(sample)
    if rem > 0:
        candidates = [r for r in all_rows if _canonical_key(r) not in selected]
        candidates = sorted(candidates, key=lambda r: _parse_int(r.get("timestamp", "0")))
        for row in candidates:
            if rem <= 0:
                break
            p = _norm_addr(row.get("pool_address", ""))
            if p in pool_target and by_pool_now[p] >= pool_target[p]:
                continue
            k = _canonical_key(row)
            selected.add(k)
            sample.append(row)
            by_pool_now[p] += 1
            rem -= 1
        if rem > 0:
            for row in candidates:
                if rem <= 0:
                    break
                k = _canonical_key(row)
                if k in selected:
                    continue
                selected.add(k)
                sample.append(row)
                rem -= 1

    sample = sorted(sample, key=lambda r: _parse_int(r.get("timestamp", "0")))[:max_rows]
    for i, row in enumerate(sample, 1):
        row["sample_id"] = str(i)
    sample_pool = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in sample))
    sample_q = dict(Counter((r.get("_quartile") or "") for r in sample))
    avail_pool = {k: len(v) for k, v in by_pool.items()}
    return sample, sample_pool, sample_q, avail_pool, excluded


def _build_out_fields() -> list[str]:
    fields = [
        "sample_id",
        "tx_hash",
        "log_index",
        "pool_address",
        "timestamp",
        "time_quartile",
        "entry_block",
        "exit_block_60m",
        "token0",
        "token1",
        "stable_flag",
        "pool_type",
        "quote_token_in",
        "quote_token_out",
        "entry_notional_in",
        "entry_quote",
        "exit_quote_60m",
        "gross_return_60m",
        "quote_status_60m",
        "reserve_status",
        "token_metadata_status",
    ]
    for c in COSTS:
        ck = _cost_key(c)
        fields += [f"net_return_cost_{ck}_60m", f"positive_cost_{ck}_60m"]
    fields += ["failure_reason", "rpc_calls_this_row"]
    return fields


def _cost_key(cost: float) -> str:
    return f"{int(round(cost * 1000)):03d}"


def _quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "min": None,
            "p05": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p95": None,
            "max": None,
            "mean": None,
        }
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


def _duplicate_counts_output(rows: list[dict[str, str]]) -> tuple[int, int]:
    by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = ((row.get("tx_hash") or "").lower(), row.get("log_index") or "", _norm_addr(row.get("pool_address", "")))
        by_key[key].append(row)
    dup_key = 0
    dup_conflict = 0
    for _, group in by_key.items():
        if len(group) <= 1:
            continue
        dup_key += len(group) - 1
        anchor = group[0]
        conflict_fields = [
            "entry_block",
            "exit_block_60m",
            "entry_quote",
            "exit_quote_60m",
            "gross_return_60m",
        ]
        for g in group[1:]:
            if any((g.get(f) or "") != (anchor.get(f) or "") for f in conflict_fields):
                dup_conflict += 1
    return dup_key, dup_conflict


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aero 60m cost sensitivity 2000 rerun.")
    p.add_argument(
        "--max-rows",
        type=int,
        default=int(os.environ.get("AERO_VAMM_MAX_ROWS", os.environ.get("AERO_VAMM_PILOT_ROWS", "2000"))),
    )
    p.add_argument("--workers", type=int, default=int(os.environ.get("LABEL_WORKERS", "2")))
    p.add_argument("--rpc-max-calls", type=int, default=int(os.environ.get("AERO_VAMM_RPC_MAX_CALLS", "9000")))
    p.add_argument("--block-time-sec", type=int, default=int(os.environ.get("AERO_VAMM_BLOCK_TIME_SEC", "2")))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config(
        max_rows=max(1, min(5000, args.max_rows)),
        workers=max(1, min(2, args.workers)),
        rpc_max_calls=max(1000, args.rpc_max_calls),
        block_time_sec=max(1, args.block_time_sec),
        abort_on_429=(os.environ.get("AERO_VAMM_ABORT_ON_429", "1") != "0"),
    )
    plan = {
        "title": f"Aero vAMM 60m cost sensitivity {cfg.max_rows}",
        "status": "plan_only",
        "max_rows": cfg.max_rows,
        "horizon": HORIZON_NAME,
        "costs": COSTS,
        "rpc_max_calls": cfg.rpc_max_calls,
        "outputs": {
            "csv": str(OUT_CSV.relative_to(REPO)),
            "json": str(OUT_JSON.relative_to(REPO)),
            "md": str(OUT_MD.relative_to(REPO)),
        },
    }
    if os.environ.get("RUN_EXECUTE", "0") != "1":
        print(json.dumps(plan, indent=2))
        return 0

    rpc_url = _load_rpc_url()
    if not rpc_url:
        print("missing RPC URL (LABEL_RPC_URL/EVM_HTTP_URL)")
        return 2

    sample, sample_pool_dist, sample_q_dist, avail_pool_dist, excluded_counts = _build_sample(cfg.max_rows)
    out_fields = _build_out_fields()
    rpc = RPC(rpc_url, max_calls=cfg.rpc_max_calls, abort_on_429=cfg.abort_on_429)

    lock = threading.Lock()
    pool_cache: dict[str, PoolMeta] = {}
    decimals_cache: dict[str, int] = {}
    receipt_cache: dict[str, int] = {}
    quote_cache: dict[tuple[str, int, int, str], int] = {}
    reserve_cache: dict[tuple[str, int], tuple[int, int]] = {}
    latest_block = 0
    script_error = ""

    chain_id = int(rpc.call("eth_chainId", []), 16)
    if chain_id != 8453:
        print(json.dumps({"error": "CHAIN_ID_MISMATCH"}, indent=2))
        return 2
    latest_block = int(rpc.call("eth_blockNumber", []), 16)

    def fail_row(row: dict[str, str], reason: str, rpc_delta: int) -> dict[str, str]:
        out = {k: "" for k in out_fields}
        out["sample_id"] = row.get("sample_id", "")
        out["tx_hash"] = (row.get("tx_hash") or "").lower()
        out["log_index"] = row.get("log_index", "")
        out["pool_address"] = _norm_addr(row.get("pool_address", ""))
        out["timestamp"] = row.get("timestamp", "")
        out["time_quartile"] = row.get("_quartile", "")
        out["pool_type"] = row.get("pool_type", "")
        out["failure_reason"] = reason
        out["quote_status_60m"] = "failed"
        out["rpc_calls_this_row"] = str(max(0, rpc_delta))
        return out

    def run_one(row: dict[str, str]) -> dict[str, str]:
        before = rpc.total_calls
        try:
            key = _canonical_key(row)
            if not all(key):
                return fail_row(row, "fail_token_metadata_missing", rpc.total_calls - before)
            txh, lgi, pool = key
            if not _valid_addr(pool):
                return fail_row(row, "fail_token_metadata_missing", rpc.total_calls - before)

            with lock:
                entry_block = receipt_cache.get(txh)
            if entry_block is None:
                receipt = rpc.call("eth_getTransactionReceipt", [txh])
                if not receipt or not receipt.get("blockNumber"):
                    return fail_row(row, "fail_before_creation", rpc.total_calls - before)
                entry_block = int(receipt["blockNumber"], 16)
                with lock:
                    receipt_cache[txh] = entry_block
            if entry_block <= 0 or entry_block > latest_block:
                return fail_row(row, "fail_before_creation", rpc.total_calls - before)

            with lock:
                meta = pool_cache.get(pool)
            if meta is None:
                t0, t1, stable = call_pool_meta(rpc, pool)
                if stable:
                    return fail_row(row, "fail_stable_pool_excluded", rpc.total_calls - before)
                d0 = decimals_cache.get(t0)
                if d0 is None:
                    d0 = call_decimals(rpc, t0)
                    with lock:
                        decimals_cache[t0] = d0
                d1 = decimals_cache.get(t1)
                if d1 is None:
                    d1 = call_decimals(rpc, t1)
                    with lock:
                        decimals_cache[t1] = d1
                meta = PoolMeta(token0=t0, token1=t1, stable=False, token0_decimals=d0, token1_decimals=d1)
                with lock:
                    pool_cache[pool] = meta
            if meta.stable:
                return fail_row(row, "fail_stable_pool_excluded", rpc.total_calls - before)

            rk = (pool, entry_block)
            with lock:
                reserves = reserve_cache.get(rk)
            if reserves is None:
                reserves = call_reserves(rpc, pool, entry_block)
                with lock:
                    reserve_cache[rk] = reserves
            reserve_status = "ok" if reserves[0] > 0 and reserves[1] > 0 else "failed"

            if meta.token0 == WETH_BASE:
                token_in, token_out, amount_in = meta.token0, meta.token1, 500000000000000
            elif meta.token1 == WETH_BASE:
                token_in, token_out, amount_in = meta.token1, meta.token0, 500000000000000
            else:
                token_in, token_out = meta.token0, meta.token1
                amount_in = 10 ** meta.token0_decimals if meta.token0_decimals > 0 else 500000000000000

            qk_entry = (pool, entry_block, amount_in, token_in)
            with lock:
                entry_quote = quote_cache.get(qk_entry)
            if entry_quote is None:
                entry_quote = call_get_amount_out(rpc, pool, amount_in, token_in, entry_block)
                with lock:
                    quote_cache[qk_entry] = entry_quote
            if entry_quote <= 0:
                return fail_row(row, "fail_quote_unavailable", rpc.total_calls - before)

            exit_block = min(latest_block, entry_block + max(1, HORIZON_SEC // cfg.block_time_sec))
            qk_exit = (pool, exit_block, entry_quote, token_out)
            with lock:
                exit_quote = quote_cache.get(qk_exit)
            if exit_quote is None:
                exit_quote = call_get_amount_out(rpc, pool, entry_quote, token_out, exit_block)
                with lock:
                    quote_cache[qk_exit] = exit_quote
            if exit_quote <= 0:
                return fail_row(row, "fail_quote_unavailable", rpc.total_calls - before)

            gross = (exit_quote / amount_in) - 1.0
            if not math.isfinite(gross) or gross < -1.0 or gross > 10.0:
                return fail_row(row, "fail_rpc_error", rpc.total_calls - before)

            out = {k: "" for k in out_fields}
            out["sample_id"] = row.get("sample_id", "")
            out["tx_hash"] = txh
            out["log_index"] = lgi
            out["pool_address"] = pool
            out["timestamp"] = row.get("timestamp", "")
            out["time_quartile"] = row.get("_quartile", "")
            out["entry_block"] = str(entry_block)
            out["exit_block_60m"] = str(exit_block)
            out["token0"] = meta.token0
            out["token1"] = meta.token1
            out["stable_flag"] = "false"
            out["pool_type"] = "aero_vamm"
            out["quote_token_in"] = token_in
            out["quote_token_out"] = token_out
            out["entry_notional_in"] = str(amount_in)
            out["entry_quote"] = str(entry_quote)
            out["exit_quote_60m"] = str(exit_quote)
            out["gross_return_60m"] = f"{gross:.12f}"
            out["quote_status_60m"] = "ok"
            out["reserve_status"] = reserve_status
            out["token_metadata_status"] = "ok"
            for c in COSTS:
                ck = _cost_key(c)
                net = gross - c
                out[f"net_return_cost_{ck}_60m"] = f"{net:.12f}"
                out[f"positive_cost_{ck}_60m"] = "1" if net > 0.0 else "0"
            out["failure_reason"] = "ok"
            out["rpc_calls_this_row"] = str(max(0, rpc.total_calls - before))
            return out
        except RpcAbort:
            raise
        except Exception:
            return fail_row(row, "fail_rpc_error", rpc.total_calls - before)

    rows_out: list[dict[str, str]] = []
    if cfg.workers == 1:
        for row in sample:
            try:
                rows_out.append(run_one(row))
            except RpcAbort as exc:
                script_error = str(exc)
                rows_out.append(fail_row(row, "fail_429_abort", 0))
                for rem in sample[len(rows_out):]:
                    rows_out.append(fail_row(rem, "fail_429_abort", 0))
                break
    else:
        idx_map = {id(r): i for i, r in enumerate(sample)}
        tmp: dict[int, dict[str, str]] = {}
        aborted = False
        with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
            futures = {ex.submit(run_one, row): row for row in sample}
            for fut in as_completed(futures):
                row = futures[fut]
                idx = idx_map[id(row)]
                try:
                    tmp[idx] = fut.result()
                except RpcAbort as exc:
                    aborted = True
                    script_error = str(exc)
                    tmp[idx] = fail_row(row, "fail_429_abort", 0)
                except Exception:
                    tmp[idx] = fail_row(row, "fail_rpc_error", 0)
        if aborted:
            for i, row in enumerate(sample):
                if i not in tmp:
                    tmp[i] = fail_row(row, "fail_429_abort", 0)
        rows_out = [tmp[i] for i in range(len(sample))]

    rows_out = sorted(rows_out, key=lambda r: _parse_int(r.get("sample_id", "0")))
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for row in rows_out:
            w.writerow({k: row.get(k, "") for k in out_fields})

    attempted = len(rows_out)
    success_rows = sum(1 for r in rows_out if r.get("quote_status_60m") == "ok")
    quote_success_rate = _pct(success_rows, attempted)
    failure_dist = Counter((r.get("failure_reason") or "fail_rpc_error") for r in rows_out)
    for f in FAILURES:
        failure_dist.setdefault(f, 0)

    token_meta_ok = sum(1 for r in rows_out if r.get("token_metadata_status") == "ok")
    reserve_ok = sum(1 for r in rows_out if r.get("reserve_status") == "ok")
    token_meta_rate = _pct(token_meta_ok, attempted)
    reserve_rate = _pct(reserve_ok, attempted)

    dup_key_count, dup_conflict_count = _duplicate_counts_output(rows_out)
    pool_dist = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in rows_out if r.get("pool_address")))
    quartile_dist = dict(Counter((r.get("time_quartile") or "") for r in rows_out if r.get("time_quartile")))
    pool_quartile_dist = dict(
        Counter(
            f"{_norm_addr(r.get('pool_address', ''))}|{r.get('time_quartile', '')}"
            for r in rows_out
            if r.get("pool_address") and r.get("time_quartile")
        )
    )

    gross_vals = [_safe_float(r.get("gross_return_60m", "")) for r in rows_out if r.get("quote_status_60m") == "ok"]
    gross_vals = [x for x in gross_vals if x is not None]
    gross_stats = _quantiles(gross_vals)

    by_cost: dict[str, dict[str, Any]] = {}
    by_pool_cost: dict[str, dict[str, float]] = {}
    by_quartile_cost: dict[str, dict[str, float]] = {}
    by_pool_quartile_cost: dict[str, dict[str, float]] = {}
    positive_concentration: dict[str, dict[str, Any]] = {}
    for c in COSTS:
        ck = _cost_key(c)
        net_vals = []
        pos_count = 0
        denom = 0
        pool_num = Counter()
        pool_den = Counter()
        q_num = Counter()
        q_den = Counter()
        pq_num = Counter()
        pq_den = Counter()
        for r in rows_out:
            if r.get("quote_status_60m") != "ok":
                continue
            n = _safe_float(r.get(f"net_return_cost_{ck}_60m", ""))
            if n is None:
                continue
            denom += 1
            net_vals.append(n)
            p = _norm_addr(r.get("pool_address", ""))
            q = r.get("time_quartile", "")
            pool_den[p] += 1
            q_den[q] += 1
            pq_den[(p, q)] += 1
            if n > 0:
                pos_count += 1
                pool_num[p] += 1
                q_num[q] += 1
                pq_num[(p, q)] += 1
        by_cost[f"cost_{ck}"] = {
            "positive_count": pos_count,
            "positive_rate": _pct(pos_count, denom),
            "net_return_stats": _quantiles(net_vals),
        }
        by_pool_cost[f"cost_{ck}"] = {k: _pct(pool_num[k], pool_den[k]) for k in sorted(pool_den)}
        by_quartile_cost[f"cost_{ck}"] = {k: _pct(q_num[k], q_den[k]) for k in sorted(q_den)}
        by_pool_quartile_cost[f"cost_{ck}"] = {
            f"{k[0]}|{k[1]}": _pct(pq_num[k], pq_den[k]) for k in sorted(pq_den)
        }
        pos_pool_share = 0.0
        pos_q_share = 0.0
        if pos_count > 0:
            pos_pool_share = round(max(pool_num.values()) / pos_count, 6) if pool_num else 0.0
            pos_q_share = round(max(q_num.values()) / pos_count, 6) if q_num else 0.0
        positive_concentration[f"cost_{ck}"] = {
            "max_positive_share_single_pool": pos_pool_share,
            "max_positive_share_single_time_quartile": pos_q_share,
        }

    cost004_pass = by_cost["cost_004"]["positive_rate"] >= 0.10
    cost006_pass = by_cost["cost_006"]["positive_rate"] >= 0.10
    net_sane = all(math.isfinite(x) and -1.0 <= x <= 10.0 for x in gross_vals)
    rpc_secret_leakage = "http://" in json.dumps(rows_out[:3]).lower() or "https://" in json.dumps(rows_out[:3]).lower()

    calls_per_success_row = round(rpc.total_calls / success_rows, 6) if success_rows else None

    stable_accepted = sum(
        1 for r in rows_out if r.get("failure_reason") == "ok" and (r.get("stable_flag") or "").lower() in {"1", "true", "yes"}
    )
    cl_accepted = sum(
        1 for r in rows_out if r.get("failure_reason") == "ok" and "cl" in (r.get("pool_type") or "").lower()
    )
    unknown_accepted = sum(
        1 for r in rows_out if r.get("failure_reason") == "ok" and "unknown" in (r.get("pool_type") or "").lower()
    )

    # compare with pilot and 2000 baseline
    consistency_vs_500 = {"available": False}
    if INPUT_PILOT_500_REPORT.is_file():
        try:
            pilot = json.loads(INPUT_PILOT_500_REPORT.read_text(encoding="utf-8"))
            p004 = pilot["horizon_cost_matrix"]["60m"]["cost_004"]["positive_rate"]
            p006 = pilot["horizon_cost_matrix"]["60m"]["cost_006"]["positive_rate"]
            consistency_vs_500 = {
                "available": True,
                "pilot_500_cost004_positive_rate": p004,
                "pilot_500_cost006_positive_rate": p006,
                "rerun_2000_cost004_positive_rate": by_cost["cost_004"]["positive_rate"],
                "rerun_2000_cost006_positive_rate": by_cost["cost_006"]["positive_rate"],
                "cost004_abs_diff": round(abs(by_cost["cost_004"]["positive_rate"] - p004), 6),
                "cost006_abs_diff": round(abs(by_cost["cost_006"]["positive_rate"] - p006), 6),
                "direction_consistent": (
                    by_cost["cost_004"]["positive_rate"] >= by_cost["cost_006"]["positive_rate"]
                    and p004 >= p006
                ),
            }
        except Exception:
            consistency_vs_500 = {"available": False}
    consistency_vs_2000 = {"available": False}
    if INPUT_RERUN_2000_REPORT.is_file():
        try:
            base_2000 = json.loads(INPUT_RERUN_2000_REPORT.read_text(encoding="utf-8"))
            b004 = base_2000["cost_metrics"]["cost_004"]["positive_rate"]
            b006 = base_2000["cost_metrics"]["cost_006"]["positive_rate"]
            consistency_vs_2000 = {
                "available": True,
                "base_2000_cost004_positive_rate": b004,
                "base_2000_cost006_positive_rate": b006,
                "current_cost004_positive_rate": by_cost["cost_004"]["positive_rate"],
                "current_cost006_positive_rate": by_cost["cost_006"]["positive_rate"],
                "cost004_abs_diff": round(abs(by_cost["cost_004"]["positive_rate"] - b004), 6),
                "cost006_abs_diff": round(abs(by_cost["cost_006"]["positive_rate"] - b006), 6),
                "direction_consistent": (
                    by_cost["cost_004"]["positive_rate"] >= by_cost["cost_006"]["positive_rate"]
                    and b004 >= b006
                ),
            }
        except Exception:
            consistency_vs_2000 = {"available": False}

    max_pool_share_004 = positive_concentration["cost_004"]["max_positive_share_single_pool"]
    max_q_share_004 = positive_concentration["cost_004"]["max_positive_share_single_time_quartile"]

    gates = {
        "attempted_rows_eq_target": attempted == cfg.max_rows,
        "quote_success_rate_ge_095": quote_success_rate >= 0.95,
        "stable_accepted_eq_0": stable_accepted == 0,
        "cl_slipstream_accepted_eq_0": cl_accepted == 0,
        "unknown_accepted_eq_0": unknown_accepted == 0,
        "total_rpc_calls_le_budget": rpc.total_calls <= cfg.rpc_max_calls,
        "calls_per_success_row_le_6": calls_per_success_row is not None and calls_per_success_row <= 6,
        "output_duplicate_conflict_eq_0": dup_conflict_count == 0,
        "no_rpc_secret_leakage": not rpc_secret_leakage,
        "cost_004_positive_rate_ge_010": cost004_pass,
        "result_not_entirely_single_pool": max_pool_share_004 < 0.95 if by_cost["cost_004"]["positive_count"] > 0 else True,
        "result_not_entirely_single_time_quartile": max_q_share_004 < 0.95 if by_cost["cost_004"]["positive_count"] > 0 else True,
        "net_return_distribution_sane": net_sane,
    }

    if script_error:
        if "429" in script_error or "RPC_CALL_BUDGET_EXCEEDED" in script_error:
            conclusion = "HOLD_DUE_TO_RPC_LIMIT"
        else:
            conclusion = "HOLD_DUE_TO_SCRIPT_FAILURE"
    elif not gates["quote_success_rate_ge_095"]:
        conclusion = "HOLD_DUE_TO_QUOTE_FAILURE"
    elif not (gates["stable_accepted_eq_0"] and gates["cl_slipstream_accepted_eq_0"] and gates["unknown_accepted_eq_0"]):
        conclusion = "HOLD_DUE_TO_POOL_TYPE_LEAKAGE"
    elif not gates["output_duplicate_conflict_eq_0"]:
        conclusion = "HOLD_DUE_TO_DUPLICATE_CONFLICT"
    elif not gates["result_not_entirely_single_pool"]:
        conclusion = "HOLD_DUE_TO_POOL_CONCENTRATION"
    elif not gates["result_not_entirely_single_time_quartile"]:
        conclusion = "HOLD_DUE_TO_TEMPORAL_CONCENTRATION"
    elif not gates["total_rpc_calls_le_budget"] or not gates["calls_per_success_row_le_6"]:
        conclusion = "HOLD_DUE_TO_RPC_LIMIT"
    elif cost006_pass:
        conclusion = (
            "AERO_5000_60M_COST006_SIGNAL_CONFIRMED"
            if cfg.max_rows >= 5000
            else "AERO_60M_2000_COST006_SIGNAL_CONFIRMED"
        )
    elif cost004_pass:
        conclusion = (
            "AERO_5000_60M_COST004_SIGNAL_CONFIRMED"
            if cfg.max_rows >= 5000
            else "AERO_60M_2000_COST004_SIGNAL_CONFIRMED"
        )
    else:
        conclusion = (
            "AERO_5000_60M_COST004_SIGNAL_WEAK_OR_UNSTABLE"
            if cfg.max_rows >= 5000
            else "AERO_60M_2000_COST004_SIGNAL_WEAK_OR_UNSTABLE"
        )

    report = {
        "title": f"Aero vAMM 60m cost sensitivity {cfg.max_rows}",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "attempted_rows": attempted,
        "success_rows": success_rows,
        "quote_success_rate": quote_success_rate,
        "token_metadata_success_rate": token_meta_rate,
        "reserve_success_rate": reserve_rate,
        "output_only_duplicate_key_count": dup_key_count,
        "output_only_duplicate_conflict_count": dup_conflict_count,
        "pool_distribution": pool_dist,
        "time_quartile_distribution": quartile_dist,
        "pool_quartile_distribution": pool_quartile_dist,
        "sample_pool_distribution_targeted": sample_pool_dist,
        "sample_quartile_distribution_targeted": sample_q_dist,
        "available_pool_distribution": avail_pool_dist,
        "rpc_call_count_by_method": dict(sorted((k, int(v)) for k, v in rpc.method_counts.items())),
        "total_rpc_calls": int(rpc.total_calls),
        "calls_per_success_row": calls_per_success_row,
        "gross_return_stats": gross_stats,
        "cost_metrics": by_cost,
        "positive_rate_by_pool_for_each_cost": by_pool_cost,
        "positive_rate_by_time_quartile_for_each_cost": by_quartile_cost,
        "positive_rate_by_pool_quartile_for_each_cost": by_pool_quartile_cost,
        "positive_concentration_by_cost": positive_concentration,
        "cost_004_positive_rate_ge_010": cost004_pass,
        "cost_006_positive_rate_ge_010": cost006_pass,
        "consistency_vs_500_row_pilot": consistency_vs_500,
        "consistency_vs_2000_row_baseline": consistency_vs_2000,
        "failure_taxonomy": dict(sorted((k, int(v)) for k, v in failure_dist.items())),
        "source_excluded_counts": dict(sorted((k, int(v)) for k, v in excluded_counts.items())),
        "stable_accepted_count": stable_accepted,
        "cl_slipstream_accepted_count": cl_accepted,
        "unknown_accepted_count": unknown_accepted,
        "rpc_secret_leakage_detected": rpc_secret_leakage,
        "gate_results": {k: {"pass": bool(v)} for k, v in gates.items()},
        "conclusion_enum": conclusion,
        "script_error": script_error,
        "artifacts": {
            "csv": str(OUT_CSV.relative_to(REPO)),
            "json": str(OUT_JSON.relative_to(REPO)),
            "md": str(OUT_MD.relative_to(REPO)),
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    matrix = {k: v["positive_rate"] for k, v in by_cost.items()}
    md = [
        f"# Aero vAMM 60m Cost Sensitivity {cfg.max_rows}",
        "",
        "Status: research-only (no training/no scale/no live).",
        "",
        f"- attempted/success/quote_success_rate: {attempted}/{success_rows}/{quote_success_rate}",
        f"- total_rpc_calls: {report['total_rpc_calls']}",
        f"- calls_per_success_row: {report['calls_per_success_row']}",
        "",
        "## Positive rate by cost",
        json.dumps(matrix, indent=2, sort_keys=True),
        "",
        "## Gates",
        json.dumps(report["gate_results"], indent=2, sort_keys=True),
        "",
        "## Conclusion",
        f"`{conclusion}`",
    ]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "attempted_rows": attempted,
                "success_rows": success_rows,
                "quote_success_rate": quote_success_rate,
                "total_rpc_calls": report["total_rpc_calls"],
                "calls_per_success_row": report["calls_per_success_row"],
                "cost_004_positive_rate": by_cost["cost_004"]["positive_rate"],
                "cost_006_positive_rate": by_cost["cost_006"]["positive_rate"],
                "conclusion_enum": conclusion,
                "output_csv": report["artifacts"]["csv"],
                "output_json": report["artifacts"]["json"],
                "output_md": report["artifacts"]["md"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

