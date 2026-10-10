"""
Aero vAMM Adapter 1000-row Dry-run (research only).

Purpose:
  - Validate Aero vAMM volatile adapter behavior on a bounded 1000-row sample.
  - Do not enter any training/canonical/promote/live path.

Hard boundaries:
  - max processed rows: 1000
  - only aero_vamm + stable=false + vAMM swap topic
  - exclude CL/Slipstream/stable/unknown/non-swap/unresolved
  - abort on HTTP 429 (no infinite retry)
  - do not write RPC secrets to artifacts
"""

from __future__ import annotations

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
from statistics import median
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent

INPUT_CANDIDATES = REPO / "logs" / "run004_candidates_swaponly_full.csv"
INPUT_SMALL_SAMPLE = REPO / "logs" / "aero_vamm_small_sample_candidates.csv"
INPUT_PILOT_REPORT = REPO / "mvp" / "diagnostics" / "aero_vamm_adapter_pilot_report.json"
OUT_CSV = REPO / "logs" / "aero_vamm_adapter_1000_dryrun_output.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_adapter_1000_dryrun_report.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-adapter-1000-dryrun.md"

WETH_BASE = "0x4200000000000000000000000000000000000006"
AERO_VAMM_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_STABLE = "0x22be3de1"
SEL_DECIMALS = "0x313ce567"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_GET_AMOUNT_OUT = "0xf140a35a"  # getAmountOut(uint256,address)

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
    "fail_429_abort",
)

OUT_FIELDS = [
    "sample_id",
    "timestamp",
    "tx_hash",
    "log_index",
    "pool_address",
    "token_address",
    "pool_type",
    "stable_flag",
    "adapter_status",
    "failure_reason",
    "reserves_status",
    "entry_quote_status",
    "exit_quote_status",
    "quote_status",
    "estimated_entry_quote",
    "estimated_exit_quote",
    "estimated_net_return",
    "sanity_clip_flag",
    "sanity_check_status",
    "entry_block",
    "exit_block",
    "token0",
    "token1",
    "token0_decimals",
    "token1_decimals",
    "quote_token_in",
    "quote_token_out",
    "quote_amount_in",
    "rpc_calls_this_row",
]


class DryrunAbort(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolMeta:
    token0: str
    token1: str
    stable: bool
    token0_decimals: int
    token1_decimals: int


class RPC:
    """JSON-RPC client with call counting and strict 429 abort."""

    def __init__(self, url: str, timeout: int = 30, max_calls: int = 8000) -> None:
        self._url = url
        self._timeout = timeout
        self._max_calls = max_calls
        self._id = 0
        self._lock = threading.Lock()
        self._aborted = threading.Event()
        self.method_counts: Counter[str] = Counter()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(self.method_counts.values())

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    def call(self, method: str, params: list[Any]) -> Any:
        if self._aborted.is_set():
            raise DryrunAbort("RPC_ABORTED")
        with self._lock:
            if sum(self.method_counts.values()) >= self._max_calls:
                self._aborted.set()
                raise DryrunAbort("RPC_BUDGET_EXHAUSTED")
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
                raise DryrunAbort("HTTP_429_ABORT") from exc
            raise
        if isinstance(payload, dict) and payload.get("error"):
            msg = str(payload["error"].get("message", "rpc_error"))
            if "429" in msg:
                self._aborted.set()
                raise DryrunAbort("HTTP_429_ABORT")
            raise RuntimeError(msg)
        return payload.get("result") if isinstance(payload, dict) else None


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


def _pad32(hex_value: str) -> str:
    return hex_value.lower().replace("0x", "").rjust(64, "0")


def _addr_arg(addr: str) -> str:
    return _pad32(_norm_addr(addr)[2:])


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


def _eth_call(rpc: RPC, to_addr: str, data: str, block: int | str) -> str:
    block_tag = hex(block) if isinstance(block, int) else block
    out = rpc.call("eth_call", [{"to": _norm_addr(to_addr), "data": data}, block_tag])
    if not isinstance(out, str) or out in {"", "0x"}:
        raise ValueError("empty_eth_call")
    return out


def call_token0(rpc: RPC, pool: str) -> str:
    return _decode_addr(_eth_call(rpc, pool, SEL_TOKEN0, "latest"))


def call_token1(rpc: RPC, pool: str) -> str:
    return _decode_addr(_eth_call(rpc, pool, SEL_TOKEN1, "latest"))


def call_stable(rpc: RPC, pool: str) -> bool:
    return _decode_bool(_eth_call(rpc, pool, SEL_STABLE, "latest"))


def call_decimals(rpc: RPC, token: str) -> int:
    v = _decode_uint(_eth_call(rpc, token, SEL_DECIMALS, "latest"))
    return max(0, min(int(v), 36))


def call_get_reserves(rpc: RPC, pool: str, block_num: int) -> tuple[int, int]:
    raw = _eth_call(rpc, pool, SEL_GET_RESERVES, block_num).replace("0x", "")
    if len(raw) < 128:
        raise ValueError("short_reserves")
    return int(raw[0:64], 16), int(raw[64:128], 16)


def call_get_amount_out(rpc: RPC, pool: str, amount_in: int, token_in: str, block_num: int) -> int:
    data = SEL_GET_AMOUNT_OUT + _uint_arg(amount_in) + _addr_arg(token_in)
    return _decode_uint(_eth_call(rpc, pool, data, block_num))


def _classify_candidate_row(row: dict[str, str]) -> tuple[bool, str]:
    pool_type = (row.get("pool_type") or "").strip().lower()
    topic0 = (row.get("topic0") or "").strip().lower()
    event_kind = (row.get("event_kind") or "").strip().lower()
    reject_reason = (row.get("reject_reason") or "").strip().lower()

    if "slipstream" in pool_type or "slipstream" in event_kind:
        return False, "fail_cl_slipstream_excluded"
    if pool_type in {"aero_cl"} or "aero_cl" in pool_type or "or_cl" in event_kind:
        return False, "fail_cl_slipstream_excluded"
    if pool_type in {"", "unknown", "none"}:
        return False, "fail_unknown_pool_type"
    if "unknown_pool_type" in reject_reason or "unresolved" in reject_reason:
        return False, "fail_unknown_pool_type"
    if pool_type != "aero_vamm":
        return False, "fail_not_aero_vamm"
    if topic0 != AERO_VAMM_SWAP_TOPIC:
        return False, "fail_not_aero_vamm"
    if "swap" not in event_kind and event_kind:
        return False, "fail_not_aero_vamm"
    return True, "ok"


def _scan_candidates() -> tuple[dict[str, list[dict[str, str]]], Counter[str], int]:
    if not INPUT_CANDIDATES.is_file():
        raise FileNotFoundError(str(INPUT_CANDIDATES))
    eligible: dict[str, list[dict[str, str]]] = defaultdict(list)
    excluded: Counter[str] = Counter()
    total = 0
    seen_keys: set[tuple[str, str, str]] = set()
    with INPUT_CANDIDATES.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            ok, reason = _classify_candidate_row(row)
            if not ok:
                excluded[reason] += 1
                continue
            pool = _norm_addr(row.get("pool_address", ""))
            tx = (row.get("tx_hash") or "").strip().lower()
            li = (row.get("log_index") or "").strip()
            ts = (row.get("timestamp") or "").strip()
            if not (_valid_addr(pool) and tx and li and ts.isdigit()):
                excluded["fail_token_metadata_missing"] += 1
                continue
            key = (tx, li, pool)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            eligible[pool].append(row)
    return eligible, excluded, total


def _evenly_pick(rows: list[dict[str, str]], n: int) -> list[dict[str, str]]:
    if n <= 0 or not rows:
        return []
    if n >= len(rows):
        return list(rows)
    rows_sorted = sorted(rows, key=lambda r: int(r.get("timestamp", "0")))
    out: list[dict[str, str]] = []
    last_idx = len(rows_sorted) - 1
    used: set[int] = set()
    for i in range(n):
        idx = round(i * last_idx / max(1, n - 1))
        while idx in used and idx < last_idx:
            idx += 1
        while idx in used and idx > 0:
            idx -= 1
        used.add(idx)
        out.append(rows_sorted[idx])
    out.sort(key=lambda r: int(r.get("timestamp", "0")))
    return out


def _build_1000_sample(eligible_by_pool: dict[str, list[dict[str, str]]]) -> tuple[list[dict[str, str]], dict[str, int], dict[str, int]]:
    pools = sorted(eligible_by_pool.keys(), key=lambda p: len(eligible_by_pool[p]), reverse=True)
    available = {p: len(eligible_by_pool[p]) for p in pools}
    sample: list[dict[str, str]] = []
    selected_distribution: dict[str, int] = {}

    if len(pools) == 0:
        return sample, selected_distribution, available
    if len(pools) == 1:
        p = pools[0]
        take = min(1000, available[p])
        sample.extend(_evenly_pick(eligible_by_pool[p], take))
        selected_distribution[p] = take
        return sample, selected_distribution, available

    # If two pools exist, target 500/500 where possible.
    p0, p1 = pools[0], pools[1]
    t0 = min(500, available[p0])
    t1 = min(500, available[p1])
    sample.extend(_evenly_pick(eligible_by_pool[p0], t0))
    sample.extend(_evenly_pick(eligible_by_pool[p1], t1))
    selected_distribution[p0] = t0
    selected_distribution[p1] = t1

    remaining = 1000 - len(sample)
    if remaining > 0:
        for p in pools:
            if remaining <= 0:
                break
            already = selected_distribution.get(p, 0)
            left = max(0, available[p] - already)
            if left <= 0:
                continue
            take = min(left, remaining)
            extra_rows = _evenly_pick(eligible_by_pool[p][already:], take)
            sample.extend(extra_rows)
            selected_distribution[p] = already + len(extra_rows)
            remaining -= len(extra_rows)

    sample = sorted(sample, key=lambda r: int(r.get("timestamp", "0")))[:1000]
    if len(sample) < 1000 and INPUT_SMALL_SAMPLE.is_file():
        # Fallback top-up from the existing small-sample file only if still short.
        topup: list[dict[str, str]] = []
        seen = {(r.get("tx_hash", ""), r.get("log_index", ""), _norm_addr(r.get("pool_address", ""))) for r in sample}
        with INPUT_SMALL_SAMPLE.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = (row.get("tx_hash", ""), row.get("log_index", ""), _norm_addr(row.get("pool_address", "")))
                if k in seen:
                    continue
                seen.add(k)
                topup.append(row)
                if len(sample) + len(topup) >= 1000:
                    break
        sample.extend(topup)
    sample = sample[:1000]
    # Recompute selected distribution from final sample.
    selected_distribution = dict(Counter(_norm_addr(r.get("pool_address", "")) for r in sample))
    return sample, selected_distribution, available


def _base_out(row: dict[str, str], reason: str, rpc_delta: int) -> dict[str, Any]:
    return {
        "sample_id": row.get("sample_id", ""),
        "timestamp": row.get("timestamp", ""),
        "tx_hash": row.get("tx_hash", ""),
        "log_index": row.get("log_index", ""),
        "pool_address": _norm_addr(row.get("pool_address", "")),
        "token_address": "",
        "pool_type": row.get("pool_type", ""),
        "stable_flag": "",
        "adapter_status": "ok" if reason == "ok" else "failed",
        "failure_reason": reason,
        "reserves_status": "not_attempted",
        "entry_quote_status": "not_attempted",
        "exit_quote_status": "not_attempted",
        "quote_status": "not_attempted",
        "estimated_entry_quote": "",
        "estimated_exit_quote": "",
        "estimated_net_return": "",
        "sanity_clip_flag": "",
        "sanity_check_status": "not_attempted",
        "entry_block": "",
        "exit_block": "",
        "token0": "",
        "token1": "",
        "token0_decimals": "",
        "token1_decimals": "",
        "quote_token_in": "",
        "quote_token_out": "",
        "quote_amount_in": "",
        "rpc_calls_this_row": max(0, rpc_delta),
    }


def process_row(
    row: dict[str, str],
    rpc: RPC,
    latest_block: int,
    notional_wei: int,
    token0_notional_default: int,
    exit_horizon_sec: int,
    lock: threading.Lock,
    pool_cache: dict[str, PoolMeta],
    token_decimals_cache: dict[str, int],
    receipt_cache: dict[str, int],
    reserve_cache: dict[tuple[str, int], tuple[int, int]],
    quote_cache: dict[tuple[str, int, int, str], int],
) -> dict[str, Any]:
    before = rpc.total_calls
    try:
        ok, static_reason = _classify_candidate_row(row)
        if not ok:
            return _base_out(row, static_reason, rpc.total_calls - before)

        pool = _norm_addr(row.get("pool_address", ""))
        tx_hash = (row.get("tx_hash") or "").strip()
        log_index = (row.get("log_index") or "").strip()
        if not (_valid_addr(pool) and tx_hash and log_index):
            return _base_out(row, "fail_token_metadata_missing", rpc.total_calls - before)

        with lock:
            entry_block = receipt_cache.get(tx_hash)
        if entry_block is None:
            receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
            if not receipt or not receipt.get("blockNumber"):
                return _base_out(row, "fail_before_creation", rpc.total_calls - before)
            entry_block = int(receipt["blockNumber"], 16)
            with lock:
                receipt_cache[tx_hash] = entry_block
        if entry_block <= 0 or entry_block > latest_block:
            return _base_out(row, "fail_before_creation", rpc.total_calls - before)
        exit_block = min(latest_block, entry_block + max(1, exit_horizon_sec // 2))

        with lock:
            pmeta = pool_cache.get(pool)
        if pmeta is None:
            t0 = call_token0(rpc, pool)
            t1 = call_token1(rpc, pool)
            stable = call_stable(rpc, pool)
            if stable:
                out = _base_out(row, "fail_stable_pool_excluded", rpc.total_calls - before)
                out["stable_flag"] = "true"
                return out
            with lock:
                d0 = token_decimals_cache.get(t0)
            if d0 is None:
                d0 = call_decimals(rpc, t0)
                with lock:
                    token_decimals_cache[t0] = d0
            with lock:
                d1 = token_decimals_cache.get(t1)
            if d1 is None:
                d1 = call_decimals(rpc, t1)
                with lock:
                    token_decimals_cache[t1] = d1
            pmeta = PoolMeta(token0=t0, token1=t1, stable=stable, token0_decimals=d0, token1_decimals=d1)
            with lock:
                pool_cache[pool] = pmeta
        if pmeta.stable:
            out = _base_out(row, "fail_stable_pool_excluded", rpc.total_calls - before)
            out["stable_flag"] = "true"
            return out

        with lock:
            reserves = reserve_cache.get((pool, entry_block))
        if reserves is None:
            reserves = call_get_reserves(rpc, pool, entry_block)
            with lock:
                reserve_cache[(pool, entry_block)] = reserves
        r0, r1 = reserves
        if r0 <= 0 or r1 <= 0:
            return _base_out(row, "fail_reserve_unavailable", rpc.total_calls - before)

        if pmeta.token0 == WETH_BASE:
            token_in = pmeta.token0
            token_out = pmeta.token1
            amount_in = notional_wei
        elif pmeta.token1 == WETH_BASE:
            token_in = pmeta.token1
            token_out = pmeta.token0
            amount_in = notional_wei
        else:
            token_in = pmeta.token0
            token_out = pmeta.token1
            amount_in = 10 ** pmeta.token0_decimals if pmeta.token0_decimals > 0 else token0_notional_default

        qk_entry = (pool, entry_block, amount_in, token_in)
        with lock:
            entry_quote = quote_cache.get(qk_entry)
        if entry_quote is None:
            entry_quote = call_get_amount_out(rpc, pool, amount_in, token_in, entry_block)
            with lock:
                quote_cache[qk_entry] = entry_quote
        if entry_quote <= 0:
            out = _base_out(row, "fail_quote_unavailable", rpc.total_calls - before)
            out["reserves_status"] = "ok"
            out["entry_quote_status"] = "failed"
            return out

        qk_exit = (pool, exit_block, entry_quote, token_out)
        with lock:
            exit_quote = quote_cache.get(qk_exit)
        if exit_quote is None:
            exit_quote = call_get_amount_out(rpc, pool, entry_quote, token_out, exit_block)
            with lock:
                quote_cache[qk_exit] = exit_quote
        if exit_quote <= 0:
            out = _base_out(row, "fail_quote_unavailable", rpc.total_calls - before)
            out["reserves_status"] = "ok"
            out["entry_quote_status"] = "ok"
            out["exit_quote_status"] = "failed"
            return out

        net = (exit_quote / amount_in) - 1.0
        if not math.isfinite(net) or net < -1.0 or net > 10.0:
            return {
                **_base_out(row, "fail_sanity_clip", rpc.total_calls - before),
                "pool_type": "aero_vamm",
                "stable_flag": "false",
                "reserves_status": "ok",
                "entry_quote_status": "ok",
                "exit_quote_status": "ok",
                "quote_status": "ok",
                "estimated_entry_quote": str(entry_quote),
                "estimated_exit_quote": str(exit_quote),
                "estimated_net_return": "",
                "sanity_clip_flag": "true",
                "sanity_check_status": "failed",
                "entry_block": str(entry_block),
                "exit_block": str(exit_block),
                "token0": pmeta.token0,
                "token1": pmeta.token1,
                "token0_decimals": str(pmeta.token0_decimals),
                "token1_decimals": str(pmeta.token1_decimals),
                "quote_token_in": token_in,
                "quote_token_out": token_out,
                "quote_amount_in": str(amount_in),
                "token_address": token_out,
            }

        return {
            "sample_id": row.get("sample_id", ""),
            "timestamp": row.get("timestamp", ""),
            "tx_hash": tx_hash,
            "log_index": log_index,
            "pool_address": pool,
            "token_address": token_out,
            "pool_type": "aero_vamm",
            "stable_flag": "false",
            "adapter_status": "ok",
            "failure_reason": "ok",
            "reserves_status": "ok",
            "entry_quote_status": "ok",
            "exit_quote_status": "ok",
            "quote_status": "ok",
            "estimated_entry_quote": str(entry_quote),
            "estimated_exit_quote": str(exit_quote),
            "estimated_net_return": f"{net:.12f}",
            "sanity_clip_flag": "false",
            "sanity_check_status": "ok",
            "entry_block": str(entry_block),
            "exit_block": str(exit_block),
            "token0": pmeta.token0,
            "token1": pmeta.token1,
            "token0_decimals": str(pmeta.token0_decimals),
            "token1_decimals": str(pmeta.token1_decimals),
            "quote_token_in": token_in,
            "quote_token_out": token_out,
            "quote_amount_in": str(amount_in),
            "rpc_calls_this_row": max(0, rpc.total_calls - before),
        }
    except DryrunAbort:
        raise
    except Exception:
        return _base_out(row, "fail_rpc_error", rpc.total_calls - before)


def _pct(n: int, d: int) -> float:
    return round((float(n) / float(d)) if d else 0.0, 6)


def _concentration(pool_counts: Counter[str], total: int) -> dict[str, float]:
    if total <= 0:
        return {"top1_share": 0.0, "top2_share": 0.0, "top5_share": 0.0}
    vals = [v for _, v in pool_counts.most_common()]
    top1 = vals[0] if vals else 0
    top2 = sum(vals[:2]) if vals else 0
    top5 = sum(vals[:5]) if vals else 0
    return {
        "top1_share": round(top1 / total, 6),
        "top2_share": round(top2 / total, 6),
        "top5_share": round(top5 / total, 6),
    }


def _timestamp_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ts = [int(r.get("timestamp", "0") or 0) for r in rows if str(r.get("timestamp", "")).isdigit()]
    if not ts:
        return {
            "min_timestamp_ms": None,
            "max_timestamp_ms": None,
            "train_rows": 0,
            "holdout_rows": 0,
            "train_range_ms": [None, None],
            "holdout_range_ms": [None, None],
        }
    rows_sorted = sorted(rows, key=lambda r: int(r.get("timestamp", "0") or 0))
    split = int(len(rows_sorted) * 0.8)
    train = rows_sorted[:split]
    holdout = rows_sorted[split:]
    train_ts = [int(r["timestamp"]) for r in train] if train else []
    hold_ts = [int(r["timestamp"]) for r in holdout] if holdout else []
    return {
        "min_timestamp_ms": min(ts),
        "max_timestamp_ms": max(ts),
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "train_range_ms": [min(train_ts) if train_ts else None, max(train_ts) if train_ts else None],
        "holdout_range_ms": [min(hold_ts) if hold_ts else None, max(hold_ts) if hold_ts else None],
    }


def _net_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals: list[float] = []
    for r in rows:
        s = r.get("estimated_net_return", "")
        if s in {"", None}:
            continue
        try:
            v = float(s)
        except ValueError:
            continue
        if math.isfinite(v):
            vals.append(v)
    if not vals:
        return {"computed": False, "sane": False, "count": 0}
    vals_s = sorted(vals)
    p05 = vals_s[max(0, int(0.05 * (len(vals_s) - 1)))]
    p95 = vals_s[min(len(vals_s) - 1, int(0.95 * (len(vals_s) - 1)))]
    sane = all(-1.0 <= x <= 10.0 for x in vals_s)
    return {
        "computed": True,
        "sane": sane,
        "count": len(vals_s),
        "min": vals_s[0],
        "median": median(vals_s),
        "max": vals_s[-1],
        "p05": p05,
        "p95": p95,
    }


def _conclusion(report: dict[str, Any]) -> str:
    if report.get("script_error"):
        if "429" in report["script_error"] or "RPC_BUDGET_EXHAUSTED" in report["script_error"]:
            return "HOLD_DUE_TO_RPC_LIMIT"
        return "HOLD_DUE_TO_SCRIPT_FAILURE"
    if report["attempted_rows"] != 1000:
        return "HOLD_DUE_TO_SCRIPT_FAILURE"
    if report["stable_rows_accepted"] > 0 or report["cl_slipstream_rows_accepted"] > 0 or report["unknown_rows_accepted"] > 0:
        return "HOLD_DUE_TO_POOL_TYPE_LEAKAGE"
    if report["rpc_total_calls"] > 8000:
        return "HOLD_DUE_TO_RPC_COST"
    if report["calls_per_success"] is None or report["calls_per_success"] > 8:
        return "HOLD_DUE_TO_RPC_COST"
    if report["success_rate"] < 0.95:
        return "HOLD_DUE_TO_AERO_VAMM_SUCCESS_RATE"
    if report["reserve_success_rate"] < 0.95:
        return "HOLD_DUE_TO_RESERVE_FAILURE"
    if report["quote_success_rate"] < 0.95:
        return "HOLD_DUE_TO_QUOTE_FAILURE"
    if report["sanity_clip_count"] > int(round(report["attempted_rows"] * 0.05)):
        return "HOLD_DUE_TO_SANITY_FAILURE"
    if report["failure_reason_distribution"].get("fail_429_abort", 0) > 0:
        return "HOLD_DUE_TO_RPC_LIMIT"
    return "AERO_VAMM_1000_DRYRUN_GO_TO_CANONICAL_LABEL_DESIGN"


def _write_outputs(rows_out: list[dict[str, Any]], report: dict[str, Any]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        for r in rows_out:
            w.writerow({k: r.get(k, "") for k in OUT_FIELDS})

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    md = f"""# Aero vAMM Adapter 1000-row Dry-run

Status: research-only (no train/retrain/canonical merge/promote/live).

## Core Metrics

- attempted_rows: {report['attempted_rows']}
- ok_rows: {report['ok_rows']}
- success_rate: {report['success_rate']}
- reserve_success_rate: {report['reserve_success_rate']}
- quote_success_rate: {report['quote_success_rate']}
- sanity_clip_count: {report['sanity_clip_count']}

## Pool Validation

- pool_distribution: {json.dumps(report['pool_distribution_selected'], sort_keys=True)}
- stable_rows_accepted: {report['stable_rows_accepted']}
- cl_slipstream_rows_accepted: {report['cl_slipstream_rows_accepted']}
- unknown_rows_accepted: {report['unknown_rows_accepted']}

## Failure Taxonomy

{json.dumps(report['failure_reason_distribution'], indent=2, sort_keys=True)}

## RPC Budget

- rpc_call_count_by_method: {json.dumps(report['rpc_call_count_by_method'], sort_keys=True)}
- rpc_total_calls: {report['rpc_total_calls']}
- calls_per_success: {report['calls_per_success']}
- estimated_full_aero_vamm_calls_for_22024_rows: {report['estimated_full_aero_vamm_calls_for_22024_rows']}
- rpc_url_written_to_artifacts: false

## Net Return Sanity

{json.dumps(report['estimated_net_return_summary'], indent=2, sort_keys=True)}

## Timestamp Coverage

{json.dumps(report['timestamp_coverage'], indent=2, sort_keys=True)}

## Conclusion

`{report['conclusion_enum']}`
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")


def main() -> int:
    _load_local_env_if_needed()
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    workers = min(2, max(1, int(os.environ.get("LABEL_WORKERS", "2"))))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", "500000000000000"))
    token0_notional_default = int(os.environ.get("AERO_VAMM_TOKEN0_NOTIONAL_RAW", "1000000000000000000"))
    exit_horizon_sec = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    max_calls = int(os.environ.get("AERO_VAMM_DRYRUN_RPC_MAX_CALLS", "8000"))
    target_rows = 1000

    rows_out: list[dict[str, Any]] = []
    script_error = ""
    source_total = 0
    source_excluded = Counter()
    pool_distribution_available: dict[str, int] = {}
    pool_distribution_selected: dict[str, int] = {}

    try:
        eligible_by_pool, source_excluded, source_total = _scan_candidates()
        sample, pool_distribution_selected, pool_distribution_available = _build_1000_sample(eligible_by_pool)
        for i, row in enumerate(sample, 1):
            row["sample_id"] = str(i)
        if len(sample) < target_rows:
            script_error = f"INSUFFICIENT_SAMPLE_{len(sample)}"
            # still continue with available rows, and report HOLD

        if not rpc_url:
            script_error = script_error or "missing_rpc_url"
            for row in sample:
                rows_out.append(_base_out(row, "fail_rpc_error", 0))
        else:
            rpc = RPC(rpc_url, max_calls=max_calls)
            chain_id = int(rpc.call("eth_chainId", []), 16)
            if chain_id != 8453:
                raise RuntimeError("unexpected_chain_id")
            latest_block = int(rpc.call("eth_blockNumber", []), 16)

            lock = threading.Lock()
            pool_cache: dict[str, PoolMeta] = {}
            token_decimals_cache: dict[str, int] = {}
            receipt_cache: dict[str, int] = {}
            reserve_cache: dict[tuple[str, int], tuple[int, int]] = {}
            quote_cache: dict[tuple[str, int, int, str], int] = {}

            indexed_rows = list(enumerate(sample))
            results: dict[int, dict[str, Any]] = {}

            if workers == 1:
                for idx, row in indexed_rows:
                    try:
                        results[idx] = process_row(
                            row=row,
                            rpc=rpc,
                            latest_block=latest_block,
                            notional_wei=notional_wei,
                            token0_notional_default=token0_notional_default,
                            exit_horizon_sec=exit_horizon_sec,
                            lock=lock,
                            pool_cache=pool_cache,
                            token_decimals_cache=token_decimals_cache,
                            receipt_cache=receipt_cache,
                            reserve_cache=reserve_cache,
                            quote_cache=quote_cache,
                        )
                    except DryrunAbort as exc:
                        script_error = str(exc)
                        for j in range(idx, len(sample)):
                            results[j] = _base_out(sample[j], "fail_429_abort", 0)
                        break
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {
                        ex.submit(
                            process_row,
                            row=row,
                            rpc=rpc,
                            latest_block=latest_block,
                            notional_wei=notional_wei,
                            token0_notional_default=token0_notional_default,
                            exit_horizon_sec=exit_horizon_sec,
                            lock=lock,
                            pool_cache=pool_cache,
                            token_decimals_cache=token_decimals_cache,
                            receipt_cache=receipt_cache,
                            reserve_cache=reserve_cache,
                            quote_cache=quote_cache,
                        ): idx
                        for idx, row in indexed_rows
                    }
                    aborted = False
                    for fut in as_completed(futs):
                        idx = futs[fut]
                        try:
                            results[idx] = fut.result()
                        except DryrunAbort as exc:
                            aborted = True
                            script_error = str(exc)
                            results[idx] = _base_out(sample[idx], "fail_429_abort", 0)
                        except Exception:
                            results[idx] = _base_out(sample[idx], "fail_rpc_error", 0)
                    if aborted:
                        for idx in range(len(sample)):
                            if idx not in results:
                                results[idx] = _base_out(sample[idx], "fail_429_abort", 0)

            for idx in range(len(sample)):
                rows_out.append(results.get(idx, _base_out(sample[idx], "fail_rpc_error", 0)))

            rpc_method_counts = dict(sorted(rpc.method_counts.items()))
            rpc_total_calls = int(rpc.total_calls)
    except Exception as exc:
        script_error = script_error or type(exc).__name__
        rpc_method_counts = {}
        rpc_total_calls = 0

    attempted = len(rows_out)
    fail_dist: Counter[str] = Counter(r.get("failure_reason", "fail_rpc_error") for r in rows_out)
    for reason in FAIL_REASONS:
        fail_dist.setdefault(reason, 0)
    ok_rows = int(fail_dist["ok"])
    reserve_ok = sum(1 for r in rows_out if r.get("reserves_status") == "ok")
    quote_ok = sum(1 for r in rows_out if r.get("entry_quote_status") == "ok" and r.get("exit_quote_status") == "ok")
    sanity_clip_count = int(fail_dist["fail_sanity_clip"])
    calls_per_success = round(rpc_total_calls / ok_rows, 6) if ok_rows > 0 else None
    pool_counts_attempted = Counter(_norm_addr(r.get("pool_address", "")) for r in rows_out if r.get("pool_address"))
    stable_rows_accepted = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and str(r.get("stable_flag", "")).lower() in {"1", "true", "yes"})
    cl_rows_accepted = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and "cl" in (r.get("pool_type") or "").lower())
    unknown_rows_accepted = sum(1 for r in rows_out if r.get("adapter_status") == "ok" and "unknown" in (r.get("pool_type") or "").lower())

    if calls_per_success is not None:
        est_full_calls = int(round(calls_per_success * 22024))
    elif attempted > 0:
        est_full_calls = int(round((rpc_total_calls / max(1, attempted)) * 22024))
    else:
        est_full_calls = None

    report: dict[str, Any] = {
        "title": "Aero vAMM Adapter 1000-row Dry-run",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "research_only": True,
        "input_candidates_csv": str(INPUT_CANDIDATES.relative_to(REPO)),
        "input_small_sample_csv": str(INPUT_SMALL_SAMPLE.relative_to(REPO)),
        "input_pilot_report": str(INPUT_PILOT_REPORT.relative_to(REPO)),
        "attempted_rows": attempted,
        "ok_rows": ok_rows,
        "success_rate": _pct(ok_rows, attempted),
        "pool_distribution_available": pool_distribution_available,
        "pool_distribution_selected": pool_distribution_selected,
        "pool_distribution_attempted": dict(pool_counts_attempted),
        "stable_excluded_count": int(fail_dist["fail_stable_pool_excluded"]),
        "cl_slipstream_excluded_count": int(fail_dist["fail_cl_slipstream_excluded"]),
        "unknown_excluded_count": int(fail_dist["fail_unknown_pool_type"]),
        "reserve_success_rate": _pct(reserve_ok, attempted),
        "quote_success_rate": _pct(quote_ok, attempted),
        "sanity_clip_count": sanity_clip_count,
        "failure_reason_distribution": dict(sorted((k, int(v)) for k, v in fail_dist.items())),
        "rpc_call_count_by_method": rpc_method_counts,
        "rpc_total_calls": rpc_total_calls,
        "calls_per_success": calls_per_success,
        "estimated_full_aero_vamm_calls_for_22024_rows": est_full_calls,
        "estimated_net_return_summary": _net_stats(rows_out),
        "top_pool_concentration": _concentration(pool_counts_attempted, attempted),
        "timestamp_coverage": _timestamp_coverage(rows_out),
        "source_scan_total_rows": source_total,
        "source_scan_excluded_counts": dict(sorted((k, int(v)) for k, v in source_excluded.items())),
        "stable_rows_accepted": int(stable_rows_accepted),
        "cl_slipstream_rows_accepted": int(cl_rows_accepted),
        "unknown_rows_accepted": int(unknown_rows_accepted),
        "script_error": script_error,
        "gates": {
            "attempted_rows_eq": 1000,
            "success_rate_min": 0.95,
            "reserve_success_rate_min": 0.95,
            "quote_success_rate_min": 0.95,
            "sanity_clip_max_fraction": 0.05,
            "calls_per_success_max": 8,
            "total_rpc_calls_max": 8000,
            "stable_rows_accepted_max": 0,
            "cl_slipstream_rows_accepted_max": 0,
            "unknown_rows_accepted_max": 0,
        },
        "artifacts": {
            "output_csv": str(OUT_CSV.relative_to(REPO)),
            "output_json": str(OUT_JSON.relative_to(REPO)),
            "output_md": str(OUT_MD.relative_to(REPO)),
        },
        "rpc_url_written_to_artifacts": False,
    }
    report["conclusion_enum"] = _conclusion(report)

    _write_outputs(rows_out, report)

    print(json.dumps({
        "attempted_rows": report["attempted_rows"],
        "ok_rows": report["ok_rows"],
        "success_rate": report["success_rate"],
        "reserve_success_rate": report["reserve_success_rate"],
        "quote_success_rate": report["quote_success_rate"],
        "rpc_total_calls": report["rpc_total_calls"],
        "calls_per_success": report["calls_per_success"],
        "conclusion_enum": report["conclusion_enum"],
        "output_csv": report["artifacts"]["output_csv"],
        "output_json": report["artifacts"]["output_json"],
        "output_md": report["artifacts"]["output_md"],
    }, indent=2))

    return 0 if not script_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
