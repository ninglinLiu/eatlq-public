"""Run-004 V2-only live-equivalent historical simulation.

This script is intentionally invariant-first. It is allowed to produce a
FAILED_* result; it is not allowed to silently continue after semantic gates
fail. The first implemented execution path is `positive_controls`, which
validates V2 Swap decoding, WETH-leg direction, timestamp horizon resolution,
duplicate conflicts, and constant-product quote sanity before any formal
walk-forward backtest is allowed.

Formal model research / walk-forward is blocked until:
  1. positive controls pass 100%;
  2. point-in-time PairCreated universe scan is available;
  3. feature timestamp audit is available for generated candidates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - config can fall back to JSON-like defaults
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

DEFAULT_CONFIG = ROOT / "configs/run004/v2_live_equivalent_backtest.yaml"
OUT_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_backtest_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v2-live-equivalent-backtest.md"
OUT_FACTORY_CSV = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe.csv"
OUT_FACTORY_SUMMARY = ROOT / "mvp/diagnostics/run004_v2_factory_paircreated_universe_summary.json"
OUT_FACTORY_MD = ROOT / "mvp/runs/run-004-v2-factory-paircreated-universe-scan.md"

V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
PAIR_CREATED_TOPIC0 = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
UNISWAP_V2_FACTORY_BASE = "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"
WETH_BASE = "0x4200000000000000000000000000000000000006"

SEL_GET_RESERVES = "0x0902f1ac"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_FACTORY = "0xc45a0155"


def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _intish(v: Any) -> int:
    """Parse CSV integer fields, including pandas scientific notation strings."""
    s = str(v or "0").strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(Decimal(s))
        except (InvalidOperation, ValueError):
            return 0


def _decode_addr_word(res: str) -> str:
    raw = (res or "0x")[2:] if str(res).startswith("0x") else str(res or "")
    if len(raw) < 64:
        return ""
    return "0x" + raw[-40:].lower()


def _decode_uint_words(data: str, n: int) -> list[int]:
    raw = data[2:] if data.startswith("0x") else data
    raw = raw.rjust(n * 64, "0")
    return [int(raw[i * 64 : (i + 1) * 64] or "0", 16) for i in range(n)]


def _pad_topic_addr(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().replace("0x", "").rjust(40, "0")


def _sha_key(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to read v2_live_equivalent_backtest.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 4, backoff: float = 0.5, sleep: float = 0.03):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep
        self._id = 0
        self.calls = Counter()
        self.errors = Counter()

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}).encode()
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self.calls[method] += 1
            time.sleep(self.sleep)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                if data.get("error"):
                    self.errors[method] += 1
                    raise RuntimeError(str(data["error"])[:240])
                return data.get("result")
            except Exception as exc:  # noqa: BLE001 - record and retry all RPC/network failures
                last_err = exc
                self.errors[method] += 1
                time.sleep(min(self.backoff * (2**attempt), 8.0))
        raise RuntimeError(f"RPC {method} failed after {self.max_retries} retries: {last_err}")

    def receipt(self, tx_hash: str) -> dict[str, Any]:
        return self.call("eth_getTransactionReceipt", [tx_hash]) or {}

    def block(self, block_num: int) -> dict[str, Any]:
        return self.call("eth_getBlockByNumber", [hex(block_num), False]) or {}

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str, block_num: int) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, hex(block_num)]) or "0x"

    def get_logs(self, address: str, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        return self.call(
            "eth_getLogs",
            [{
                "address": address,
                "topics": topics,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }],
        ) or []


def rpc_from_config(cfg: dict[str, Any]) -> RPC:
    _load_env_file()
    url = ""
    for env_name in cfg["rpc"].get("env_vars", ["LABEL_RPC_URL", "EVM_HTTP_URL"]):
        url = os.environ.get(env_name, "").strip()
        if url:
            break
    if not url:
        raise RuntimeError("LABEL_RPC_URL or EVM_HTTP_URL is required")
    return RPC(
        url,
        timeout=int(cfg["rpc"].get("timeout_sec", 60)),
        max_retries=int(cfg["rpc"].get("max_retries", 4)),
        backoff=float(cfg["rpc"].get("retry_backoff_sec", 0.5)),
        sleep=float(cfg["rpc"].get("rate_limit_sleep_sec", 0.03)),
    )


@dataclass
class DecodedSwap:
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int
    direction: str
    token_amount: int
    weth_amount: int
    implied_price_weth_per_token: float | None
    certainty: str
    reason: str


def decode_v2_swap_log(log: dict[str, Any], token0: str, token1: str, weth: str = WETH_BASE) -> DecodedSwap:
    a0i, a1i, a0o, a1o = _decode_uint_words(str(log.get("data", "0x")), 4)
    t0 = _lower(token0)
    t1 = _lower(token1)
    weth = _lower(weth)

    if weth not in (t0, t1):
        return DecodedSwap(a0i, a1i, a0o, a1o, "UNKNOWN", 0, 0, None, "uncertain", "non_weth_pair")

    if t0 == weth:
        if a0i > 0 and a1o > 0 and a1i == 0 and a0o == 0:
            price = (a0i / a1o) if a1o else None
            return DecodedSwap(a0i, a1i, a0o, a1o, "BUY_WETH_TO_TOKEN", a1o, a0i, price, "certain", "")
        if a1i > 0 and a0o > 0 and a0i == 0 and a1o == 0:
            price = (a0o / a1i) if a1i else None
            return DecodedSwap(a0i, a1i, a0o, a1o, "SELL_TOKEN_TO_WETH", a1i, a0o, price, "certain", "")
    if t1 == weth:
        if a1i > 0 and a0o > 0 and a0i == 0 and a1o == 0:
            price = (a1i / a0o) if a0o else None
            return DecodedSwap(a0i, a1i, a0o, a1o, "BUY_WETH_TO_TOKEN", a0o, a1i, price, "certain", "")
        if a0i > 0 and a1o > 0 and a1i == 0 and a0o == 0:
            price = (a1o / a0i) if a0i else None
            return DecodedSwap(a0i, a1i, a0o, a1o, "SELL_TOKEN_TO_WETH", a0i, a1o, price, "certain", "")

    return DecodedSwap(a0i, a1i, a0o, a1o, "UNKNOWN", 0, 0, None, "uncertain", "ambiguous_amount_pattern")


def quote_v2_out(reserve_in: int, reserve_out: int, amount_in: int) -> int:
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0
    amount_in_with_fee = amount_in * 997
    return (amount_in_with_fee * reserve_out) // (reserve_in * 1000 + amount_in_with_fee)


def get_reserves(rpc: RPC, pool: str, block_num: int) -> tuple[int, int]:
    raw = rpc.eth_call(pool, SEL_GET_RESERVES, block_num)
    vals = _decode_uint_words(raw, 2)
    return vals[0], vals[1]


def token0_token1_factory(rpc: RPC, pool: str, block_num: int) -> dict[str, str]:
    return {
        "token0": _decode_addr_word(rpc.eth_call(pool, SEL_TOKEN0, block_num)),
        "token1": _decode_addr_word(rpc.eth_call(pool, SEL_TOKEN1, block_num)),
        "factory": _decode_addr_word(rpc.eth_call(pool, SEL_FACTORY, block_num)),
    }


def block_timestamp(rpc: RPC, block_num: int) -> int:
    block = rpc.block(block_num)
    if not block:
        raise RuntimeError(f"missing block {block_num}")
    return int(block["timestamp"], 16)


def first_block_at_or_after_timestamp(rpc: RPC, target_ts: int, lo: int, hi: int) -> tuple[int, int, int]:
    """Return (block, timestamp, error_seconds) for first block with ts >= target_ts."""
    lo0, hi0 = lo, hi
    answer = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        ts = block_timestamp(rpc, mid)
        if ts >= target_ts:
            answer = mid
            hi = mid - 1
        else:
            lo = mid + 1
    ans_ts = block_timestamp(rpc, answer)
    if ans_ts < target_ts:
        raise RuntimeError(f"exit block resolution failed in range {lo0}-{hi0}")
    return answer, ans_ts, ans_ts - target_ts


def load_h1611_rows(limit_pool_type: str = "v2") -> list[dict[str, Any]]:
    path = ROOT / "mvp/diagnostics/run004_h1611_all_v2_decoded_backed_10k_dataset.csv"
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if _lower(row.get("pool_type")) == limit_pool_type and row.get("source_tx_hash"):
                rows.append(row)
    return rows


def deterministic_sample(rows: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    keyed = []
    seen = set()
    for row in rows:
        key = (row.get("source_tx_hash"), row.get("source_log_index"), row.get("pool_address"))
        if key in seen:
            continue
        seen.add(key)
        keyed.append(row)
    if len(keyed) <= n:
        return keyed
    return rng.sample(keyed, n)


def find_matching_swap_log(receipt: dict[str, Any], row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    pool = _lower(row.get("pool_address"))
    expected_log_index = _intish(row.get("source_log_index"))
    candidates = []
    for log in receipt.get("logs", []) or []:
        topics = [str(t).lower() for t in log.get("topics", [])]
        if _lower(log.get("address")) == pool and topics and topics[0] == V2_SWAP_TOPIC:
            candidates.append(log)
    for log in candidates:
        if int(log.get("logIndex", "0x0"), 16) == expected_log_index:
            return log, "exact_log_index"
    if len(candidates) == 1:
        return candidates[0], "single_pool_swap_fallback"
    return None, f"no_exact_match_candidates={len(candidates)}"


def csv_direction(row: dict[str, Any]) -> str:
    d = _lower(row.get("swap_direction") or row.get("direction"))
    if d == "buy_weth_to_token":
        return "BUY_WETH_TO_TOKEN"
    if d == "sell_token_to_weth":
        return "SELL_TOKEN_TO_WETH"
    return "UNKNOWN"


def duplicate_conflict_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    key_specs = {
        "source_tx_hash+source_log_index": ["source_tx_hash", "source_log_index"],
        "pool+source_tx_hash+source_log_index": ["pool_address", "source_tx_hash", "source_log_index"],
        "candidate_id+row_id": ["candidate_id", "row_id"],
    }
    for name, cols in key_specs.items():
        counter = Counter(tuple(row.get(c, "") for c in cols) for row in rows)
        dup_keys = [k for k, count in counter.items() if count > 1]
        conflict_count = 0
        for key in dup_keys:
            vals = {
                (
                    row.get("final_return_after_cost"),
                    row.get("alpha_top20_after_gas"),
                    row.get("risk_severity"),
                )
                for row in rows
                if tuple(row.get(c, "") for c in cols) == key
            }
            if len(vals) > 1:
                conflict_count += 1
        result[name] = {
            "duplicate_key_count": len(dup_keys),
            "duplicate_rows_extra": sum(counter[k] - 1 for k in dup_keys),
            "duplicate_conflict_count": conflict_count,
        }
    return result


def quote_sanity_same_reserves(rpc: RPC, row: dict[str, Any], notional_wei: int, round_trip_cost: float) -> dict[str, Any]:
    pool = _lower(row["pool_address"])
    entry_block = _intish(row.get("source_block_number"))
    token0 = _lower(row.get("token0_address") or row.get("token0"))
    token1 = _lower(row.get("token1_address") or row.get("token1"))
    reserves = get_reserves(rpc, pool, max(entry_block - 1, 1))
    r0, r1 = reserves
    if token0 == WETH_BASE:
        rw, rt = r0, r1
    elif token1 == WETH_BASE:
        rw, rt = r1, r0
    else:
        return {"passed": False, "reason": "non_weth_pair", "net_return": None}
    tokens = quote_v2_out(rw, rt, notional_wei)
    weth_back = quote_v2_out(rt, rw, tokens)
    gross_roundtrip = weth_back / notional_wei - 1.0 if notional_wei else 0.0
    net = gross_roundtrip - round_trip_cost
    return {
        "passed": net < 0,
        "gross_roundtrip_return": gross_roundtrip,
        "net_return_after_round_trip_cost": net,
        "tokens_out": tokens,
        "weth_back": weth_back,
        "reason": "" if net < 0 else "same_reserves_roundtrip_non_negative",
    }


def decode_paircreated_log(log: dict[str, Any]) -> dict[str, Any]:
    topics = [str(t).lower() for t in log.get("topics", [])]
    data = str(log.get("data", "0x"))
    if len(topics) < 3 or topics[0] != PAIR_CREATED_TOPIC0:
        raise ValueError("not_paircreated_log")
    token0 = "0x" + topics[1][-40:]
    token1 = "0x" + topics[2][-40:]
    words = _decode_uint_words(data, 2)
    raw = data[2:] if data.startswith("0x") else data
    pair = "0x" + raw[:64][-40:].lower() if len(raw) >= 64 else ""
    if not pair or pair == "0x0000000000000000000000000000000000000000":
        raise ValueError("missing_pair_address")
    return {
        "token0": token0.lower(),
        "token1": token1.lower(),
        "pair_address": pair,
        "pair_index": words[1] if len(words) > 1 else None,
        "creation_block": int(str(log.get("blockNumber", "0x0")), 16),
        "creation_tx_hash": str(log.get("transactionHash", "")),
        "log_index": int(str(log.get("logIndex", "0x0")), 16),
    }


def _read_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _resolve_block_bound(rpc: RPC, value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if s == "latest":
        return rpc.block_number()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def _pair_conflict(existing: dict[str, Any], new: dict[str, Any]) -> bool:
    keys = ("token0", "token1", "creation_block", "creation_tx_hash", "log_index")
    return any(existing.get(k) != new.get(k) for k in keys)


def _scan_factory_paircreated(cfg: dict[str, Any], rpc: RPC) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scan_cfg = cfg.get("factory_scan", {})
    factory = _lower(cfg["universe"].get("factory", UNISWAP_V2_FACTORY_BASE))
    from_block = _resolve_block_bound(rpc, scan_cfg.get("from_block", 0), 0)
    to_block = _resolve_block_bound(rpc, scan_cfg.get("to_block", "latest"), rpc.block_number())
    chunk_size = int(scan_cfg.get("chunk_size", 9999))
    checkpoint_path = ROOT / str(scan_cfg.get("checkpoint_path", "mvp/diagnostics/run004_v2_factory_paircreated_universe_checkpoint.json"))
    resume = bool(scan_cfg.get("resume", True))
    rpc.max_retries = int(scan_cfg.get("max_rpc_retries", rpc.max_retries))
    rpc.sleep = float(scan_cfg.get("rate_limit_sleep_sec", rpc.sleep))

    checkpoint = _read_checkpoint(checkpoint_path) if resume else None
    checkpoint_resume_used = False
    completed_to_block = from_block - 1
    pair_by_addr: dict[str, dict[str, Any]] = {}
    duplicate_logs = 0
    semantic_conflicts: list[dict[str, Any]] = []

    if checkpoint:
        meta = checkpoint.get("meta", {})
        compatible = (
            _lower(meta.get("factory")) == factory
            and int(meta.get("from_block", -1)) == from_block
            and int(meta.get("to_block", -1)) <= to_block
            and int(meta.get("chunk_size", -1)) == chunk_size
        )
        if compatible:
            checkpoint_resume_used = True
            completed_to_block = int(checkpoint.get("completed_to_block", from_block - 1))
            # Backward-compatible migration from the first heavy checkpoint
            # format. New checkpoints no longer store all pairs; they rely on
            # the universe CSV as the resumable pair store.
            if checkpoint.get("pairs"):
                for row in checkpoint.get("pairs", []):
                    pair_by_addr[_lower(row["pair_address"])] = row
            elif OUT_FACTORY_CSV.exists():
                with OUT_FACTORY_CSV.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        pair_by_addr[_lower(row["pair_address"])] = {
                            "pair_address": _lower(row["pair_address"]),
                            "token0": _lower(row["token0"]),
                            "token1": _lower(row["token1"]),
                            "creation_block": int(row["creation_block"]),
                            "creation_tx_hash": row["creation_tx_hash"],
                            "log_index": int(row["log_index"]),
                            "pair_index": int(row["pair_index"]) if str(row.get("pair_index") or "").isdigit() else None,
                        }
            duplicate_logs = int(checkpoint.get("duplicate_pair_logs", 0))
            semantic_conflicts = list(checkpoint.get("semantic_conflicts", []))
        else:
            checkpoint = None

    start_block = max(from_block, completed_to_block + 1)
    chunks_done = 0
    total_logs_seen = len(pair_by_addr) + duplicate_logs
    decode_failures: list[dict[str, Any]] = []

    cur = start_block
    while cur <= to_block:
        end = min(cur + chunk_size - 1, to_block)
        logs = rpc.get_logs(factory, [PAIR_CREATED_TOPIC0], cur, end)
        chunks_done += 1
        total_logs_seen += len(logs)
        for log in logs:
            try:
                row = decode_paircreated_log(log)
            except Exception as exc:  # noqa: BLE001 - recorded as scan failure
                decode_failures.append({
                    "block": log.get("blockNumber"),
                    "tx_hash": log.get("transactionHash"),
                    "log_index": log.get("logIndex"),
                    "error": str(exc),
                })
                continue
            pair = _lower(row["pair_address"])
            if pair in pair_by_addr:
                duplicate_logs += 1
                if _pair_conflict(pair_by_addr[pair], row):
                    semantic_conflicts.append({"existing": pair_by_addr[pair], "new": row})
                continue
            pair_by_addr[pair] = row

        completed_to_block = end
        if chunks_done % 100 == 0 or end == to_block:
            _write_factory_csv(sorted(pair_by_addr.values(), key=lambda r: (r["creation_block"], r["log_index"])))
        if chunks_done % 10 == 0 or end == to_block:
            _write_checkpoint(checkpoint_path, {
                "meta": {
                    "factory": factory,
                    "from_block": from_block,
                    "to_block": to_block,
                    "chunk_size": chunk_size,
                    "universe_construction_method": "uniswap_v2_factory_paircreated",
                },
                "completed_to_block": completed_to_block,
                "pair_store_csv": str(OUT_FACTORY_CSV.relative_to(ROOT)),
                "unique_pairs": len(pair_by_addr),
                "duplicate_pair_logs": duplicate_logs,
                "semantic_conflicts": semantic_conflicts,
                "decode_failures": decode_failures,
                "updated_at": _utc_now(),
            })
        print(f"[factory_scan] {cur:,}-{end:,}: logs={len(logs)} unique_pairs={len(pair_by_addr)}")
        cur = end + 1

    pairs = sorted(pair_by_addr.values(), key=lambda r: (r["creation_block"], r["log_index"]))
    scan_meta = {
        "from_block": from_block,
        "to_block": to_block,
        "scanned_blocks": max(0, to_block - from_block + 1),
        "chunk_size": chunk_size,
        "total_paircreated_logs": total_logs_seen,
        "unique_pairs": len(pairs),
        "duplicate_pair_logs": duplicate_logs,
        "semantic_conflicts": semantic_conflicts,
        "decode_failures": decode_failures,
        "checkpoint_path": str(checkpoint_path.relative_to(ROOT)),
        "checkpoint_resume_used": checkpoint_resume_used,
        "factory": factory,
    }
    return pairs, scan_meta


def _validate_factory_pairs(
    cfg: dict[str, Any],
    rpc: RPC,
    pairs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scan_cfg = cfg.get("factory_scan", {})
    sample_n = min(int(scan_cfg.get("validation_sample_n", 50)), len(pairs))
    seed = int(scan_cfg.get("deterministic_seed", 42))
    rng = random.Random(seed)
    sample = rng.sample(pairs, sample_n) if len(pairs) > sample_n else pairs[:]
    factory = _lower(cfg["universe"].get("factory", UNISWAP_V2_FACTORY_BASE))
    validations: list[dict[str, Any]] = []

    for row in sample:
        pair = _lower(row["pair_address"])
        creation_block = int(row["creation_block"])
        checked_block = creation_block
        errors: list[str] = []
        meta = {"token0": "", "token1": "", "factory": ""}
        for block in (creation_block, creation_block + 1):
            try:
                meta = token0_token1_factory(rpc, pair, block)
                checked_block = block
                if meta["token0"] and meta["token1"] and meta["factory"]:
                    break
            except Exception as exc:  # noqa: BLE001 - try next block, record if final fail
                errors.append(str(exc)[:160])
        token_match = meta["token0"] == _lower(row["token0"]) and meta["token1"] == _lower(row["token1"])
        factory_match = meta["factory"] == factory
        passed = token_match and factory_match
        validations.append({
            "pair_address": pair,
            "creation_block": creation_block,
            "checked_block": checked_block,
            "log_token0": _lower(row["token0"]),
            "log_token1": _lower(row["token1"]),
            "onchain_token0": meta["token0"],
            "onchain_token1": meta["token1"],
            "onchain_factory": meta["factory"],
            "token_match": token_match,
            "factory_match": factory_match,
            "passed": passed,
            "errors": errors,
        })

    stats = {
        "validation_sample_n": sample_n,
        "validation_passed_n": sum(1 for v in validations if v["passed"]),
        "validation_failed_n": sum(1 for v in validations if not v["passed"]),
        "validation_all_passed": all(v["passed"] for v in validations) if validations else False,
    }
    return validations, stats


def _write_factory_csv(pairs: list[dict[str, Any]]) -> None:
    OUT_FACTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "pair_address",
        "token0",
        "token1",
        "contains_weth",
        "creation_block",
        "creation_tx_hash",
        "log_index",
        "pair_index",
    ]
    with OUT_FACTORY_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in pairs:
            writer.writerow({
                "pair_address": _lower(row["pair_address"]),
                "token0": _lower(row["token0"]),
                "token1": _lower(row["token1"]),
                "contains_weth": WETH_BASE in {_lower(row["token0"]), _lower(row["token1"])},
                "creation_block": int(row["creation_block"]),
                "creation_tx_hash": row["creation_tx_hash"],
                "log_index": int(row["log_index"]),
                "pair_index": row.get("pair_index"),
            })


def run_factory_scan(cfg: dict[str, Any]) -> dict[str, Any]:
    rpc = rpc_from_config(cfg)
    pairs, scan_meta = _scan_factory_paircreated(cfg, rpc)
    validations, validation_stats = _validate_factory_pairs(cfg, rpc, pairs)

    weth_pairs = [p for p in pairs if WETH_BASE in {_lower(p["token0"]), _lower(p["token1"])}]
    non_weth_pairs = len(pairs) - len(weth_pairs)
    has_core_fields = all(
        p.get("pair_address") and p.get("token0") and p.get("token1")
        and p.get("creation_block") is not None and p.get("creation_tx_hash")
        for p in pairs
    )
    point_in_time_pass = (
        len(pairs) > 0
        and validation_stats["validation_all_passed"]
        and not scan_meta["semantic_conflicts"]
        and not scan_meta["decode_failures"]
        and has_core_fields
    )
    decision = "READY_FOR_1D_PILOT" if point_in_time_pass else "FAILED_FACTORY_UNIVERSE_VALIDATION"

    _write_factory_csv(pairs)
    summary = {
        "generated_at": _utc_now(),
        "patch": "run-004-v2-factory-paircreated-universe-scan",
        "sanity_only_not_deploy_evidence": True,
        "constraints": {
            "chain_id": int(cfg["rpc"].get("chain_id", 8453)),
            "diagnostics_only": True,
            "no_training": True,
            "no_live": True,
            "no_shadow": True,
            "no_promote": True,
            "no_production_model_write": True,
            "no_active_model_overwrite": True,
            "v2_factory_only": True,
            "no_v3_aero_unknown": True,
        },
        "universe_construction_method": "uniswap_v2_factory_paircreated",
        "factory": scan_meta["factory"],
        "pair_created_topic0": PAIR_CREATED_TOPIC0,
        "from_block": scan_meta["from_block"],
        "to_block": scan_meta["to_block"],
        "scanned_blocks": scan_meta["scanned_blocks"],
        "chunk_size": scan_meta["chunk_size"],
        "total_paircreated_logs": scan_meta["total_paircreated_logs"],
        "unique_pairs": scan_meta["unique_pairs"],
        "duplicate_pair_logs": scan_meta["duplicate_pair_logs"],
        "semantic_conflict_count": len(scan_meta["semantic_conflicts"]),
        "decode_failure_count": len(scan_meta["decode_failures"]),
        "weth_pairs": len(weth_pairs),
        "non_weth_pairs": non_weth_pairs,
        "first_pair_block": min((int(p["creation_block"]) for p in pairs), default=None),
        "last_pair_block": max((int(p["creation_block"]) for p in pairs), default=None),
        **validation_stats,
        "validation_failures": [v for v in validations if not v["passed"]],
        "validation_sample": validations[:10],
        "rpc_calls": dict(rpc.calls),
        "rpc_errors": dict(rpc.errors),
        "checkpoint_resume_used": scan_meta["checkpoint_resume_used"],
        "checkpoint_path": scan_meta["checkpoint_path"],
        "point_in_time_universe_pass": point_in_time_pass,
        "decision": decision,
        "universe_bias_warning": (
            "Factory PairCreated universe removes H1611-known-pool lookahead bias for Uniswap V2 pairs. "
            "Residual bias remains if later backtest candidates are generated from a filtered subset "
            "instead of all active factory pairs by test window."
        ),
        "allow_1d_pilot": point_in_time_pass,
        "outputs": {
            "universe_csv": str(OUT_FACTORY_CSV.relative_to(ROOT)),
            "summary_json": str(OUT_FACTORY_SUMMARY.relative_to(ROOT)),
            "markdown": str(OUT_FACTORY_MD.relative_to(ROOT)),
        },
    }
    OUT_FACTORY_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_FACTORY_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_factory_markdown(summary)
    return summary


def _write_factory_markdown(summary: dict[str, Any]) -> None:
    md = f"""# Run-004 V2 Factory PairCreated Universe Scan

**Status:** {summary['decision']}  
**Generated:** {summary['generated_at']}  
**Scope:** diagnostics-only · Uniswap V2 factory only · no training · no live · no shadow · no production model writes

## Scan Parameters

| Field | Value |
|-------|-------|
| chain_id | {summary['constraints']['chain_id']} |
| factory | `{summary['factory']}` |
| PairCreated topic0 | `{summary['pair_created_topic0']}` |
| from_block | {summary['from_block']:,} |
| to_block | {summary['to_block']:,} |
| scanned_blocks | {summary['scanned_blocks']:,} |
| chunk_size | {summary['chunk_size']:,} |
| checkpoint_resume_used | {summary['checkpoint_resume_used']} |

## Universe Summary

| Metric | Value |
|--------|-------|
| total_paircreated_logs | {summary['total_paircreated_logs']:,} |
| unique_pairs | **{summary['unique_pairs']:,}** |
| duplicate_pair_logs | {summary['duplicate_pair_logs']:,} |
| semantic_conflict_count | {summary['semantic_conflict_count']} |
| decode_failure_count | {summary['decode_failure_count']} |
| weth_pairs | **{summary['weth_pairs']:,}** |
| non_weth_pairs | {summary['non_weth_pairs']:,} |
| first_pair_block | {summary['first_pair_block']} |
| last_pair_block | {summary['last_pair_block']} |

## Validation

| Metric | Value |
|--------|-------|
| validation_sample_n | {summary['validation_sample_n']} |
| validation_passed_n | {summary['validation_passed_n']} |
| validation_failed_n | {summary['validation_failed_n']} |
| point_in_time_universe_pass | **{summary['point_in_time_universe_pass']}** |

Validation checks token0(), token1(), and factory() on each sampled pair at
`creation_block` or `creation_block + 1`. factory() must equal the Base
Uniswap V2 factory.

## RPC Accounting

| Metric | Value |
|--------|-------|
| rpc_calls | `{summary['rpc_calls']}` |
| rpc_errors | `{summary['rpc_errors']}` |

## Required Answers

1. Factory universe 是否已完成：**{summary['point_in_time_universe_pass']}**.
2. 扫到了多少 V2 pair：**{summary['unique_pairs']:,}**.
3. 其中多少是 WETH pair：**{summary['weth_pairs']:,}**.
4. 是否能支持 point-in-time backtest：**{summary['point_in_time_universe_pass']}**.
5. 是否仍然存在 universe bias：{summary['universe_bias_warning']}
6. 是否允许进入 1d pilot：**{summary['allow_1d_pilot']}**. This does not start pilot automatically.

## Artifacts

- `mvp/diagnostics/run004_v2_factory_paircreated_universe.csv`
- `mvp/diagnostics/run004_v2_factory_paircreated_universe_summary.json`
- `mvp/runs/run-004-v2-factory-paircreated-universe-scan.md`
"""
    OUT_FACTORY_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_FACTORY_MD.write_text(md, encoding="utf-8")


def run_positive_controls(cfg: dict[str, Any]) -> dict[str, Any]:
    rpc = rpc_from_config(cfg)
    rows = load_h1611_rows("v2")
    sample_n = int(cfg["swap_semantics"].get("positive_control_n", 20))
    seed = int(cfg["swap_semantics"].get("deterministic_seed", 42))
    sample = deterministic_sample(rows, sample_n, seed)

    controls: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    notional = int(cfg["execution"].get("notional_wei", 500000000000000))
    round_trip_cost = float(cfg["execution"].get("round_trip_cost", 0.006))
    horizon_sec = int(cfg["horizon"].get("exit_horizon_sec", 1800))

    for row in sample:
        tx = row["source_tx_hash"]
        pool = _lower(row["pool_address"])
        entry_block = _intish(row["source_block_number"])
        receipt = rpc.receipt(tx)
        log, match_method = find_matching_swap_log(receipt, row)
        if not log:
            item = {
                "tx_hash": tx,
                "pool": pool,
                "entry_block": entry_block,
                "passed": False,
                "failure_reason": f"swap_log_not_found:{match_method}",
            }
            controls.append(item)
            failures.append(item)
            continue

        token0 = _lower(row.get("token0_address") or row.get("token0"))
        token1 = _lower(row.get("token1_address") or row.get("token1"))
        decoded = decode_v2_swap_log(log, token0, token1, WETH_BASE)
        csv_amt = {
            "amount0_in": _intish(row.get("amount0_in")),
            "amount1_in": _intish(row.get("amount1_in")),
            "amount0_out": _intish(row.get("amount0_out")),
            "amount1_out": _intish(row.get("amount1_out")),
        }
        amount_match = (
            decoded.amount0_in == csv_amt["amount0_in"]
            and decoded.amount1_in == csv_amt["amount1_in"]
            and decoded.amount0_out == csv_amt["amount0_out"]
            and decoded.amount1_out == csv_amt["amount1_out"]
        )
        direction_match = decoded.direction == csv_direction(row)

        # Confirm token metadata at entry block from chain, not only CSV.
        onchain_meta = token0_token1_factory(rpc, pool, entry_block)
        token_meta_match = (
            onchain_meta["token0"] == token0
            and onchain_meta["token1"] == token1
            and onchain_meta["factory"] == UNISWAP_V2_FACTORY_BASE
        )

        entry_ts = block_timestamp(rpc, entry_block)
        target_ts = entry_ts + horizon_sec
        exit_block, exit_ts, exit_err = first_block_at_or_after_timestamp(
            rpc,
            target_ts,
            entry_block,
            entry_block + max(3600, horizon_sec * 2),
        )

        sanity = quote_sanity_same_reserves(rpc, row, notional, round_trip_cost)
        passed = (
            decoded.certainty == "certain"
            and amount_match
            and direction_match
            and token_meta_match
            and exit_ts >= target_ts
            and sanity["passed"]
        )
        item = {
            "tx_hash": tx,
            "pool": pool,
            "log_index": int(log.get("logIndex", "0x0"), 16),
            "match_method": match_method,
            "entry_block": entry_block,
            "entry_timestamp": entry_ts,
            "exit_target_timestamp": target_ts,
            "exit_block": exit_block,
            "exit_timestamp": exit_ts,
            "exit_block_resolution_method": "binary_search_first_block_ts_gte_target",
            "exit_block_resolution_error_seconds": exit_err,
            "token0_csv": token0,
            "token1_csv": token1,
            "token0_onchain": onchain_meta["token0"],
            "token1_onchain": onchain_meta["token1"],
            "factory_onchain": onchain_meta["factory"],
            "decoded_direction": decoded.direction,
            "csv_direction": csv_direction(row),
            "amounts_decoded": {
                "amount0_in": decoded.amount0_in,
                "amount1_in": decoded.amount1_in,
                "amount0_out": decoded.amount0_out,
                "amount1_out": decoded.amount1_out,
            },
            "amounts_csv": csv_amt,
            "implied_price_weth_per_token": decoded.implied_price_weth_per_token,
            "weth_amount": decoded.weth_amount,
            "token_amount": decoded.token_amount,
            "amount_match": amount_match,
            "direction_match": direction_match,
            "token_meta_match": token_meta_match,
            "same_reserves_roundtrip_sanity": sanity,
            "passed": passed,
            "failure_reason": "" if passed else {
                "decode_certainty": decoded.certainty,
                "decode_reason": decoded.reason,
                "amount_match": amount_match,
                "direction_match": direction_match,
                "token_meta_match": token_meta_match,
                "quote_sanity_passed": sanity["passed"],
            },
        }
        controls.append(item)
        if not passed:
            failures.append(item)

    dup = duplicate_conflict_audit(rows)
    duplicate_pass = all(v["duplicate_conflict_count"] == 0 for v in dup.values())
    controls_pass = len(failures) == 0 and len(controls) == sample_n

    # Full factory PairCreated universe is required for formal backtest but is
    # intentionally not run inside positive_controls.
    universe = {
        "universe_construction_method": "not_yet_scanned_factory_paircreated",
        "scanned_factory_blocks": 0,
        "discovered_v2_pairs": 0,
        "active_pairs_by_window": {},
        "pair_created_coverage": 0.0,
        "universe_bias_warning": (
            "Formal walk-forward backtest remains blocked until a full UniswapV2Factory "
            "PairCreated scan reconstructs the point-in-time pool universe. Existing "
            "H1611 CSV pool list is allowed only for biased positive controls/baseline."
        ),
    }

    formal_allowed = controls_pass and duplicate_pass and False
    decision = "FAILED_SEMANTICS_UNCERTAIN" if not formal_allowed else "READY_FOR_PILOT"
    blockers = []
    if not controls_pass:
        blockers.append("v2_swap_decode_positive_controls_failed")
    if not duplicate_pass:
        blockers.append("duplicate_conflict_audit_failed")
    blockers.append("point_in_time_factory_universe_not_scanned")

    return {
        "generated_at": _utc_now(),
        "patch": "run-004-v2-live-equivalent-backtest",
        "mode": "positive_controls",
        "sanity_only_not_deploy_evidence": True,
        "constraints": {
            "v2_only": True,
            "no_v3": True,
            "no_aero": True,
            "no_unknown_pool": True,
            "no_live": True,
            "no_shadow": True,
            "no_production_model_write": True,
            "no_active_model_overwrite": True,
            "no_training": True,
        },
        "horizon_policy": {
            "policy": cfg["horizon"]["policy"],
            "entry_quote_block_policy": cfg["horizon"]["entry_quote_block_policy"],
            "exit_quote_block_policy": cfg["horizon"]["exit_quote_block_policy"],
            "exit_horizon_sec": horizon_sec,
            "resolution_method": "binary_search_first_block_timestamp_gte_exit_target_timestamp",
        },
        "quote_policy": {
            "formula": "UniswapV2 constant product: amount_in*997*rout/(rin*1000 + amount_in*997)",
            "lp_fee_bps": cfg["execution"]["lp_fee_bps"],
            "round_trip_cost": round_trip_cost,
            "notional_wei": notional,
            "entry_quote_success_required": True,
            "exit_quote_success_required": True,
            "silent_clip_allowed": False,
        },
        "positive_controls": {
            "requested_n": sample_n,
            "executed_n": len(controls),
            "passed_n": sum(1 for c in controls if c["passed"]),
            "failed_n": len(failures),
            "all_passed": controls_pass,
            "failure_reasons": [f["failure_reason"] for f in failures],
            "controls": controls,
        },
        "duplicate_key_conflict_audit": dup,
        "universe": universe,
        "leakage_guard": {
            "feature_timestamp_audit": "not_run_no_feature_generation_yet",
            "shuffled_vs_chronological": "not_run_no_training_yet",
            "label_permutation_test": "not_run_no_training_yet",
            "train_test_overlap_audit": "not_run_no_training_yet",
            "formal_backtest_blocked_until_all_are_run": True,
        },
        "gate": {
            "v2_swap_decode_positive_controls_pass": controls_pass,
            "weth_leg_classification_100pct": controls_pass,
            "exit_quote_not_same_block_label": controls_pass,
            "duplicate_conflict_zero": duplicate_pass,
            "quote_sanity_pass": all(c.get("same_reserves_roundtrip_sanity", {}).get("passed", False) for c in controls),
            "point_in_time_universe_pass": False,
            "formal_backtest_allowed": formal_allowed,
            "decision": decision,
            "blockers": blockers,
        },
        "outputs": {
            "summary_json": str(OUT_SUMMARY.relative_to(ROOT)),
            "markdown": str(OUT_MD.relative_to(ROOT)),
        },
        "rpc_stats": {
            "calls": dict(rpc.calls),
            "errors": dict(rpc.errors),
        },
    }


def write_report(summary: dict[str, Any]) -> None:
    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    pc = summary["positive_controls"]
    gate = summary["gate"]
    universe = summary["universe"]
    md = f"""# Run-004 V2-only Live-Equivalent Historical Simulation

**Status:** {gate['decision']}  
**Mode executed:** `{summary['mode']}`  
**Generated:** {summary['generated_at']}  

This report is intentionally honest-first. A formal walk-forward backtest is
**not** allowed until all invariant gates pass.

## Design Choices Fixed So Far

- Universe method: UniswapV2Factory `PairCreated` is required for the formal run.
- Current universe status: `{universe['universe_construction_method']}`.
- Existing H1611 pool list may only be used as a biased baseline / controls seed.
- Swap topic: `{V2_SWAP_TOPIC}`.
- Horizon policy: `{summary['horizon_policy']['policy']}`.
- Exit block resolution: `{summary['horizon_policy']['resolution_method']}`.
- Entry quote block: `{summary['horizon_policy']['entry_quote_block_policy']}`.
- Exit quote block: `{summary['horizon_policy']['exit_quote_block_policy']}`.
- Quote formula: `{summary['quote_policy']['formula']}`.
- Silent clipping: forbidden.

## Invariant Gate Results

| Gate | Result |
|------|--------|
| V2 Swap decode positive controls | {gate['v2_swap_decode_positive_controls_pass']} |
| WETH leg classification 100% | {gate['weth_leg_classification_100pct']} |
| Exit quote uses future horizon | {gate['exit_quote_not_same_block_label']} |
| Duplicate conflict = 0 | {gate['duplicate_conflict_zero']} |
| Same-reserves roundtrip sanity | {gate['quote_sanity_pass']} |
| Point-in-time factory universe | {gate['point_in_time_universe_pass']} |
| formal_backtest_allowed | **{gate['formal_backtest_allowed']}** |

Blockers:
"""
    for b in gate["blockers"]:
        md += f"- `{b}`\n"

    md += f"""
## Positive Controls

| Metric | Value |
|--------|-------|
| requested_n | {pc['requested_n']} |
| executed_n | {pc['executed_n']} |
| passed_n | {pc['passed_n']} |
| failed_n | {pc['failed_n']} |
| all_passed | **{pc['all_passed']}** |

Each control fetched the transaction receipt, decoded the V2 Swap log, compared
`amount0In/amount1In/amount0Out/amount1Out` to H1611 CSV fields, classified
BUY/SELL from token0/token1/WETH leg, resolved `exit_time = entry_time + 1800s`
to the first block with timestamp >= target, and ran same-reserves roundtrip
sanity. Full per-control records are in the JSON summary.

## Universe Construction Status

| Field | Value |
|-------|-------|
| scanned_factory_blocks | {universe['scanned_factory_blocks']} |
| discovered_v2_pairs | {universe['discovered_v2_pairs']} |
| pair_created_coverage | {universe['pair_created_coverage']} |

Warning: {universe['universe_bias_warning']}

## Current Decision

**{gate['decision']}** for formal backtest.

This is not a claim that V2 has or lacks alpha. It means the engine has passed
or failed only the invariants run so far. Since the point-in-time PairCreated
universe has not yet been scanned, any full historical result would still be
semantically biased.

## Final Questions (Current Stage Only)

1. V2 historical可信 alpha: **not evaluated yet**.
2. Cost 后是否成立: **not evaluated yet**.
3. 跨 OOT windows 是否成立: **not evaluated yet**.
4. 是否被 top1/top5 pool 主导: **not evaluated yet in formal backtest**; H18/fresh evidence says this is the primary risk.
5. 当前 fresh V2 集中度是否意味着 live 机会不足: **yes, current evidence says live V2 is highly concentrated and unsuitable for promotion**.
6. V2 下一步: **baseline / research benchmark only until factory-universe + walk-forward pass**.
7. 当前失败原因: **semantic completeness / universe construction not finished**, not alpha failure.

## Artifacts

- `mvp/diagnostics/run004_v2_live_equivalent_backtest_summary.json`
- `mvp/runs/run-004-v2-live-equivalent-backtest.md`
- `scripts/run004_v2_live_equivalent_backtest.py`
- `configs/run004/v2_live_equivalent_backtest.yaml`
"""
    OUT_MD.write_text(md, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--mode", choices=["positive_controls", "factory_scan"], default="positive_controls")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.mode == "positive_controls":
        summary = run_positive_controls(cfg)
        write_report(summary)
        print(json.dumps({
            "mode": args.mode,
            "decision": summary["gate"]["decision"],
            "formal_backtest_allowed": summary["gate"]["formal_backtest_allowed"],
            "positive_controls": summary["positive_controls"]["all_passed"],
            "summary_json": str(OUT_SUMMARY),
            "markdown": str(OUT_MD),
        }, indent=2))
        return
    if args.mode == "factory_scan":
        summary = run_factory_scan(cfg)
        print(json.dumps({
            "mode": args.mode,
            "decision": summary["decision"],
            "point_in_time_universe_pass": summary["point_in_time_universe_pass"],
            "unique_pairs": summary["unique_pairs"],
            "weth_pairs": summary["weth_pairs"],
            "summary_json": str(OUT_FACTORY_SUMMARY),
            "markdown": str(OUT_FACTORY_MD),
            "universe_csv": str(OUT_FACTORY_CSV),
        }, indent=2))
        return
    else:  # pragma: no cover
        raise ValueError(args.mode)


if __name__ == "__main__":
    main()

