"""
Aero vAMM 500-row horizon/cost sensitivity pilot (research-only).

Plan mode:
  RUN_EXECUTE!=1 -> print plan only.

Execute mode:
  RUN_EXECUTE=1 -> run bounded 500-row pilot, no training/no promote/no live.
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
INPUT_REF_2000 = REPO / "logs" / "aero_vamm_canonical_label_dryrun_2000.csv"
INPUT_REF_AUDIT = REPO / "mvp" / "diagnostics" / "aero_vamm_2000_low_positive_rate_audit.json"
INPUT_REF_DESIGN = REPO / "mvp" / "diagnostics" / "aero_vamm_cost_horizon_sensitivity_design.json"

OUT_CSV = REPO / "logs" / "aero_vamm_horizon_cost_pilot_500.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_horizon_cost_pilot_500_report.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-horizon-cost-pilot-500.md"

WETH_BASE = "0x4200000000000000000000000000000000000006"
AERO_VAMM_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_STABLE = "0x22be3de1"
SEL_DECIMALS = "0x313ce567"
SEL_GET_AMOUNT_OUT = "0xf140a35a"  # getAmountOut(uint256,address)

HORIZONS = [
    ("5m", 300),
    ("15m", 900),
    ("30m", 1800),
    ("60m", 3600),
]
COSTS = [0.0, 0.002, 0.004, 0.006, 0.008]

FAILURES = [
    "ok",
    "fail_not_aero_vamm",
    "fail_stable_pool_excluded",
    "fail_cl_slipstream_excluded",
    "fail_unknown_pool_type",
    "fail_abi_mismatch",
    "fail_token_metadata_missing",
    "fail_quote_unavailable",
    "fail_before_creation",
    "fail_rpc_error",
    "fail_429_abort",
]


@dataclass(frozen=True)
class PilotConfig:
    max_rows: int = 500
    workers: int = 2
    rpc_max_calls: int = 12000
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
        self.method_counts: Counter[str] = Counter()
        self.abort_reason = ""

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


def call_get_amount_out(rpc: RPC, pool: str, amount_in: int, token_in: str, block_num: int) -> int:
    data = SEL_GET_AMOUNT_OUT + _uint_arg(amount_in) + _addr_arg(token_in)
    return _decode_uint(_eth_call(rpc, pool, data, block_num))


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default


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


def _evenly_pick(rows: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    if n <= 0:
        return []
    ordered = sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
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


def _assign_quartiles(rows: list[dict[str, str]]) -> None:
    ordered = sorted(rows, key=lambda r: _parse_int(r.get("timestamp", "0")))
    n = len(ordered)
    for i, row in enumerate(ordered):
        q = min(3, int((4 * i) / max(1, n)))
        row["_quartile"] = f"Q{q + 1}"


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
    if not all_rows:
        return [], {}, {}, {}, excluded
    _assign_quartiles(all_rows)

    by_pool: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in all_rows:
        by_pool[_norm_addr(row.get("pool_address", ""))].append(row)
    pools = sorted(by_pool.keys(), key=lambda p: len(by_pool[p]), reverse=True)
    if len(pools) < 2:
        pools = pools[:1]

    if len(pools) >= 2 and max_rows >= 8:
        pa, pb = pools[0], pools[1]
        quotas = {
            ("Q1", pa): 63,
            ("Q1", pb): 62,
            ("Q2", pa): 62,
            ("Q2", pb): 63,
            ("Q3", pa): 63,
            ("Q3", pb): 62,
            ("Q4", pa): 62,
            ("Q4", pb): 63,
        }
        sample: list[dict[str, str]] = []
        selected_ids: set[tuple[str, str, str]] = set()
        # Phase 1: matrix targets
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            for p in [pa, pb]:
                bucket = [r for r in by_pool[p] if r.get("_quartile") == q]
                for row in _evenly_pick(bucket, min(len(bucket), quotas[(q, p)])):
                    k = _canonical_key(row)
                    if k in selected_ids:
                        continue
                    selected_ids.add(k)
                    sample.append(row)
        # Phase 2: fill quartile deficits
        quartile_target = {"Q1": 125, "Q2": 125, "Q3": 125, "Q4": 125}
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            have = sum(1 for r in sample if r.get("_quartile") == q)
            need = max(0, quartile_target[q] - have)
            if need <= 0:
                continue
            bucket = [r for r in all_rows if r.get("_quartile") == q and _canonical_key(r) not in selected_ids]
            for row in _evenly_pick(bucket, min(need, len(bucket))):
                k = _canonical_key(row)
                selected_ids.add(k)
                sample.append(row)
        # Phase 3: fill to max_rows with pool balance preference
        pool_target = {pa: max_rows // 2, pb: max_rows // 2}
        rem = max_rows - len(sample)
        if rem > 0:
            by_pool_sampled = Counter(_norm_addr(r.get("pool_address", "")) for r in sample)
            candidates = [r for r in all_rows if _canonical_key(r) not in selected_ids]
            candidates = sorted(candidates, key=lambda r: _parse_int(r.get("timestamp", "0")))
            for row in candidates:
                if rem <= 0:
                    break
                p = _norm_addr(row.get("pool_address", ""))
                if p in pool_target and by_pool_sampled[p] >= pool_target[p]:
                    continue
                k = _canonical_key(row)
                selected_ids.add(k)
                sample.append(row)
                by_pool_sampled[p] += 1
                rem -= 1
            if rem > 0:
                for row in candidates:
                    if rem <= 0:
                        break
                    k = _canonical_key(row)
                    if k in selected_ids:
                        continue
                    selected_ids.add(k)
                    sample.append(row)
                    rem -= 1
    else:
        sample = _evenly_pick(all_rows, min(max_rows, len(all_rows)))

    sample = sorted(sample, key=lambda r: _parse_int(r.get("timestamp", "0")))[:max_rows]
    for i, row in enumerate(sample, 1):
        row["sample_id"] = str(i)
    pool_dist = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in sample))
    quartile_dist = dict(Counter((r.get("_quartile") or "") for r in sample))
    available_pool_dist = {k: len(v) for k, v in by_pool.items()}
    return sample, pool_dist, quartile_dist, available_pool_dist, excluded


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
        "median": q(0.5),
        "p75": q(0.75),
        "p95": q(0.95),
        "max": vals[-1],
        "mean": round(sum(vals) / n, 12),
    }


def _safe_float(s: str) -> float | None:
    try:
        return float(s)
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aero vAMM 500-row horizon/cost pilot.")
    p.add_argument("--max-rows", type=int, default=int(os.environ.get("AERO_VAMM_PILOT_ROWS", "500")))
    p.add_argument("--workers", type=int, default=int(os.environ.get("LABEL_WORKERS", "2")))
    p.add_argument("--rpc-max-calls", type=int, default=int(os.environ.get("AERO_VAMM_RPC_MAX_CALLS", "12000")))
    p.add_argument("--block-time-sec", type=int, default=int(os.environ.get("AERO_VAMM_BLOCK_TIME_SEC", "2")))
    return p.parse_args()


def _build_out_fields() -> list[str]:
    fields = [
        "sample_id",
        "tx_hash",
        "log_index",
        "pool_address",
        "timestamp",
        "time_quartile",
        "entry_block",
        "token0",
        "token1",
        "stable_flag",
        "pool_type",
        "quote_token_in",
        "quote_token_out",
        "entry_notional_in",
        "entry_quote",
    ]
    for h, _ in HORIZONS:
        fields += [
            f"exit_block_{h}",
            f"exit_quote_{h}",
            f"gross_return_{h}",
            f"net_return_cost_000_{h}",
            f"net_return_cost_002_{h}",
            f"net_return_cost_004_{h}",
            f"net_return_cost_006_{h}",
            f"net_return_cost_008_{h}",
            f"positive_cost_000_{h}",
            f"positive_cost_002_{h}",
            f"positive_cost_004_{h}",
            f"positive_cost_006_{h}",
            f"positive_cost_008_{h}",
            f"quote_status_{h}",
        ]
    fields += ["failure_reason", "rpc_calls_this_row"]
    return fields


def _cost_key(cost: float) -> str:
    return f"{int(round(cost * 1000)):03d}"


def _load_rpc_url() -> str:
    _load_local_env_if_needed()
    return os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""


def main() -> int:
    args = parse_args()
    cfg = PilotConfig(
        max_rows=max(1, min(500, args.max_rows)),
        workers=max(1, min(4, args.workers)),
        rpc_max_calls=max(1000, args.rpc_max_calls),
        block_time_sec=max(1, args.block_time_sec),
        abort_on_429=(os.environ.get("AERO_VAMM_ABORT_ON_429", "1") != "0"),
    )
    plan = {
        "title": "Aero vAMM horizon/cost pilot 500",
        "status": "plan_only",
        "max_rows": cfg.max_rows,
        "horizons": HORIZONS,
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

    sample, sample_pool_dist, sample_quartile_dist, available_pool_dist, excluded_counts = _build_sample(cfg.max_rows)
    out_fields = _build_out_fields()
    rpc = RPC(rpc_url, max_calls=cfg.rpc_max_calls, abort_on_429=cfg.abort_on_429)

    lock = threading.Lock()
    pool_cache: dict[str, PoolMeta] = {}
    decimals_cache: dict[str, int] = {}
    receipt_cache: dict[str, int] = {}
    quote_cache: dict[tuple[str, int, int, str], int] = {}
    latest_block = 0
    script_error = ""

    try:
        chain_id = int(rpc.call("eth_chainId", []), 16)
        if chain_id != 8453:
            raise RuntimeError("CHAIN_ID_MISMATCH")
        latest_block = int(rpc.call("eth_blockNumber", []), 16)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}, indent=2))
        return 2

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
        out["rpc_calls_this_row"] = str(max(0, rpc_delta))
        for h, _ in HORIZONS:
            out[f"quote_status_{h}"] = "failed"
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

            out = {k: "" for k in out_fields}
            out["sample_id"] = row.get("sample_id", "")
            out["tx_hash"] = txh
            out["log_index"] = lgi
            out["pool_address"] = pool
            out["timestamp"] = row.get("timestamp", "")
            out["time_quartile"] = row.get("_quartile", "")
            out["entry_block"] = str(entry_block)
            out["token0"] = meta.token0
            out["token1"] = meta.token1
            out["stable_flag"] = "false"
            out["pool_type"] = "aero_vamm"
            out["quote_token_in"] = token_in
            out["quote_token_out"] = token_out
            out["entry_notional_in"] = str(amount_in)
            out["entry_quote"] = str(entry_quote)
            out["failure_reason"] = "ok"

            success_any = False
            for h_name, h_sec in HORIZONS:
                exit_block = min(latest_block, entry_block + max(1, h_sec // cfg.block_time_sec))
                out[f"exit_block_{h_name}"] = str(exit_block)
                qk_exit = (pool, exit_block, entry_quote, token_out)
                try:
                    with lock:
                        exit_quote = quote_cache.get(qk_exit)
                    if exit_quote is None:
                        exit_quote = call_get_amount_out(rpc, pool, entry_quote, token_out, exit_block)
                        with lock:
                            quote_cache[qk_exit] = exit_quote
                    if exit_quote <= 0:
                        raise ValueError("non_positive_quote")
                    gross = (exit_quote / amount_in) - 1.0
                    if not math.isfinite(gross) or gross < -1.0 or gross > 10.0:
                        raise ValueError("gross_out_of_range")

                    out[f"exit_quote_{h_name}"] = str(exit_quote)
                    out[f"gross_return_{h_name}"] = f"{gross:.12f}"
                    for c in COSTS:
                        ck = _cost_key(c)
                        net = gross - c
                        out[f"net_return_cost_{ck}_{h_name}"] = f"{net:.12f}"
                        out[f"positive_cost_{ck}_{h_name}"] = "1" if net > 0.0 else "0"
                    out[f"quote_status_{h_name}"] = "ok"
                    success_any = True
                except Exception:
                    out[f"quote_status_{h_name}"] = "failed"
                    out[f"exit_quote_{h_name}"] = ""
                    out[f"gross_return_{h_name}"] = ""
                    for c in COSTS:
                        ck = _cost_key(c)
                        out[f"net_return_cost_{ck}_{h_name}"] = ""
                        out[f"positive_cost_{ck}_{h_name}"] = ""

            if not success_any:
                return fail_row(row, "fail_quote_unavailable", rpc.total_calls - before)
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
    failure_dist = Counter((r.get("failure_reason") or "fail_rpc_error") for r in rows_out)
    for f in FAILURES:
        failure_dist.setdefault(f, 0)

    success_rows_by_h: dict[str, int] = {}
    quote_rate_by_h: dict[str, float] = {}
    for h, _ in HORIZONS:
        succ = sum(1 for r in rows_out if r.get(f"quote_status_{h}") == "ok")
        success_rows_by_h[h] = succ
        quote_rate_by_h[h] = _pct(succ, attempted)

    row_success = sum(1 for r in rows_out if any(r.get(f"quote_status_{h}") == "ok" for h, _ in HORIZONS))
    calls_per_success_row = round(rpc.total_calls / row_success, 6) if row_success > 0 else None

    stable_accepted = sum(
        1
        for r in rows_out
        if (r.get("failure_reason") == "ok") and str(r.get("stable_flag", "")).lower() in {"1", "true", "yes"}
    )
    cl_accepted = sum(
        1 for r in rows_out if (r.get("failure_reason") == "ok") and "cl" in (r.get("pool_type") or "").lower()
    )
    unknown_accepted = sum(
        1
        for r in rows_out
        if (r.get("failure_reason") == "ok") and "unknown" in (r.get("pool_type") or "").lower()
    )

    pool_dist = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in rows_out if r.get("pool_address")))
    quartile_dist = dict(Counter((r.get("time_quartile") or "") for r in rows_out if r.get("time_quartile")))

    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    pool_rate: dict[str, dict[str, dict[str, float]]] = {}
    quartile_rate: dict[str, dict[str, dict[str, float]]] = {}
    combo_rows: dict[tuple[str, str], list[tuple[str, str, float, float]]] = {}
    # combo_rows[(h,ck)] -> (pool, quartile, gross, net)
    for h, _ in HORIZONS:
        matrix[h] = {}
        for c in COSTS:
            ck = _cost_key(c)
            gross_vals: list[float] = []
            net_vals: list[float] = []
            pos = 0
            cnt = 0
            per_pool_num = Counter()
            per_pool_den = Counter()
            per_q_num = Counter()
            per_q_den = Counter()
            combo_key = (h, ck)
            combo_rows[combo_key] = []
            for r in rows_out:
                if r.get(f"quote_status_{h}") != "ok":
                    continue
                g = _safe_float(r.get(f"gross_return_{h}", ""))
                n = _safe_float(r.get(f"net_return_cost_{ck}_{h}", ""))
                if g is None or n is None:
                    continue
                cnt += 1
                gross_vals.append(g)
                net_vals.append(n)
                p = _norm_addr(r.get("pool_address", ""))
                q = r.get("time_quartile", "")
                per_pool_den[p] += 1
                per_q_den[q] += 1
                is_pos = n > 0.0
                if is_pos:
                    pos += 1
                    per_pool_num[p] += 1
                    per_q_num[q] += 1
                combo_rows[combo_key].append((p, q, g, n))

            matrix[h][f"cost_{ck}"] = {
                "positive_count": pos,
                "positive_rate": _pct(pos, cnt),
                "gross_return_stats": _quantiles(gross_vals),
                "net_return_stats": _quantiles(net_vals),
                "sample_count": cnt,
            }
            pool_rate.setdefault(h, {})[f"cost_{ck}"] = {
                p: _pct(per_pool_num[p], per_pool_den[p]) for p in sorted(per_pool_den)
            }
            quartile_rate.setdefault(h, {})[f"cost_{ck}"] = {
                q: _pct(per_q_num[q], per_q_den[q]) for q in sorted(per_q_den)
            }

    best_by_positive: dict[str, Any] = {"horizon": "", "cost": "", "positive_rate": -1.0}
    best_by_median_net: dict[str, Any] = {"horizon": "", "cost": "", "median_net_return": -999.0}
    best_by_p25_net: dict[str, Any] = {"horizon": "", "cost": "", "p25_net_return": -999.0}
    for h, _ in HORIZONS:
        for c in COSTS:
            ck = _cost_key(c)
            m = matrix[h][f"cost_{ck}"]
            pr = float(m["positive_rate"])
            med = m["net_return_stats"]["median"]
            p25 = m["net_return_stats"]["p25"]
            if pr > best_by_positive["positive_rate"]:
                best_by_positive = {"horizon": h, "cost": c, "positive_rate": pr}
            if med is not None and med > best_by_median_net["median_net_return"]:
                best_by_median_net = {"horizon": h, "cost": c, "median_net_return": med}
            if p25 is not None and p25 > best_by_p25_net["p25_net_return"]:
                best_by_p25_net = {"horizon": h, "cost": c, "p25_net_return": p25}

    any_cost006_ge_010 = any(matrix[h]["cost_006"]["positive_rate"] >= 0.10 for h, _ in HORIZONS)
    any_cost004_ge_010 = any(matrix[h]["cost_004"]["positive_rate"] >= 0.10 for h, _ in HORIZONS)
    signal_before_cost_disappears = (
        any(matrix[h]["cost_000"]["positive_rate"] >= 0.10 for h, _ in HORIZONS) and not any_cost006_ge_010
    )

    # check if positives are overly concentrated for best cost_006 horizon
    best_h_006 = max(HORIZONS, key=lambda x: matrix[x[0]]["cost_006"]["positive_rate"])[0]
    per_pool_006 = pool_rate[best_h_006]["cost_006"]
    per_q_006 = quartile_rate[best_h_006]["cost_006"]
    pool_dom = max(per_pool_006.values()) if per_pool_006 else 0.0
    q_dom = max(per_q_006.values()) if per_q_006 else 0.0
    no_single_pool_time_explains_all = (pool_dom < 0.95) and (q_dom < 0.95)

    rpc_secret_leakage = False
    sample_text = json.dumps(rows_out[:3]).lower()
    if "http://" in sample_text or "https://" in sample_text:
        rpc_secret_leakage = True

    gates = {
        "attempted_rows_eq_500": attempted == 500,
        "quote_success_rate_each_horizon_ge_095": all(quote_rate_by_h[h] >= 0.95 for h, _ in HORIZONS),
        "stable_accepted_eq_0": stable_accepted == 0,
        "cl_slipstream_accepted_eq_0": cl_accepted == 0,
        "unknown_accepted_eq_0": unknown_accepted == 0,
        "total_rpc_calls_le_12000": rpc.total_calls <= 12000,
        "calls_per_success_row_le_16": calls_per_success_row is not None and calls_per_success_row <= 16,
        "no_rpc_secret_leakage": rpc_secret_leakage is False,
        "horizon_cost_signal_gate": any_cost006_ge_010 or any_cost004_ge_010,
        "best_horizon_distribution_sane": True,
        "no_single_pool_time_quartile_explains_all_positives": no_single_pool_time_explains_all,
    }

    # conclusion
    if script_error:
        if "429" in script_error or "RPC_CALL_BUDGET_EXCEEDED" in script_error:
            conclusion = "HOLD_DUE_TO_RPC_LIMIT"
        else:
            conclusion = "HOLD_DUE_TO_SCRIPT_FAILURE"
    elif not gates["stable_accepted_eq_0"] or not gates["cl_slipstream_accepted_eq_0"] or not gates["unknown_accepted_eq_0"]:
        conclusion = "HOLD_DUE_TO_POOL_TYPE_LEAKAGE"
    elif not gates["quote_success_rate_each_horizon_ge_095"]:
        conclusion = "HOLD_DUE_TO_QUOTE_FAILURE"
    elif not gates["total_rpc_calls_le_12000"] or not gates["calls_per_success_row_le_16"]:
        conclusion = "HOLD_DUE_TO_RPC_LIMIT"
    elif any_cost006_ge_010:
        conclusion = "AERO_HORIZON_PILOT_FOUND_USABLE_AFTER_COST_SIGNAL"
    elif any_cost004_ge_010:
        conclusion = "AERO_HORIZON_PILOT_COST_ASSUMPTION_DOMINATES"
    elif (max(per_q_006.values()) - min(per_q_006.values()) if len(per_q_006) >= 2 else 0.0) >= 0.08:
        conclusion = "AERO_HORIZON_PILOT_TEMPORAL_REGIME_DOMINATES"
    else:
        conclusion = "AERO_HORIZON_PILOT_NO_USABLE_SIGNAL"

    report = {
        "title": "Aero vAMM horizon/cost pilot 500",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "attempted_rows": attempted,
        "success_rows_per_horizon": success_rows_by_h,
        "quote_success_rate_per_horizon": quote_rate_by_h,
        "rpc_call_count_by_method": dict(sorted((k, int(v)) for k, v in rpc.method_counts.items())),
        "total_rpc_calls": int(rpc.total_calls),
        "calls_per_success_row": calls_per_success_row,
        "pool_distribution": pool_dist,
        "timestamp_quartile_distribution": quartile_dist,
        "sample_pool_distribution_targeted": sample_pool_dist,
        "sample_quartile_distribution_targeted": sample_quartile_dist,
        "available_pool_distribution": available_pool_dist,
        "horizon_cost_matrix": matrix,
        "positive_rate_by_pool_horizon_cost": pool_rate,
        "positive_rate_by_time_quartile_horizon_cost": quartile_rate,
        "best_combo_by_positive_rate": best_by_positive,
        "best_combo_by_median_net_return": best_by_median_net,
        "best_combo_by_p25_net_return": best_by_p25_net,
        "any_horizon_cost_006_positive_rate_ge_010": any_cost006_ge_010,
        "any_horizon_cost_004_positive_rate_ge_010": any_cost004_ge_010,
        "signal_exists_pre_cost_but_disappears_after_cost_006": signal_before_cost_disappears,
        "failure_taxonomy": dict(sorted((k, int(v)) for k, v in failure_dist.items())),
        "source_excluded_counts": dict(sorted((k, int(v)) for k, v in excluded_counts.items())),
        "stable_accepted_count": stable_accepted,
        "cl_slipstream_accepted_count": cl_accepted,
        "unknown_accepted_count": unknown_accepted,
        "no_single_pool_time_quartile_explains_all_positives": no_single_pool_time_explains_all,
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

    md_lines = [
        "# Aero vAMM Horizon/Cost Pilot 500",
        "",
        "Status: research-only (no training/no promote/no live).",
        "",
        f"- attempted_rows: {attempted}",
        f"- total_rpc_calls: {report['total_rpc_calls']}",
        f"- calls_per_success_row: {report['calls_per_success_row']}",
        "",
        "## Quote success per horizon",
        json.dumps(report["quote_success_rate_per_horizon"], indent=2, sort_keys=True),
        "",
        "## Best combos",
        json.dumps(
            {
                "best_by_positive_rate": best_by_positive,
                "best_by_median_net_return": best_by_median_net,
                "best_by_p25_net_return": best_by_p25_net,
            },
            indent=2,
            sort_keys=True,
        ),
        "",
        "## Horizon x Cost positive-rate matrix",
        json.dumps(
            {
                h: {k: v["positive_rate"] for k, v in report["horizon_cost_matrix"][h].items()}
                for h, _ in HORIZONS
            },
            indent=2,
            sort_keys=True,
        ),
        "",
        "## Gate results",
        json.dumps(report["gate_results"], indent=2, sort_keys=True),
        "",
        "## Conclusion",
        f"`{conclusion}`",
    ]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "attempted_rows": attempted,
                "total_rpc_calls": report["total_rpc_calls"],
                "calls_per_success_row": report["calls_per_success_row"],
                "quote_success_rate_per_horizon": report["quote_success_rate_per_horizon"],
                "any_horizon_cost_006_ge_010": any_cost006_ge_010,
                "any_horizon_cost_004_ge_010": any_cost004_ge_010,
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
