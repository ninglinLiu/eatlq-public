#!/usr/bin/env python3
"""GL3 full-interval recovery scan for the frozen GL1-12 residual cells."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import threading
import time
import math
from copy import deepcopy
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
PREREG = ROOT / "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md"
PREREG_SHA = "fdbeccb0a4a1b17ccda0bb3e8779608804d051a8e61f89d4021cd5fc5b88a0bf"
GL2 = D / "base_gl2_a_universe_ev_20260715.json"
CORE = D / "base_reaudit10_seconds_rpc_20260715.json"
ETH = D / "base_2min_accel_b3_eth_daily_20260714.json"
INVENTORY = D / "base_gl3_event_inventory_20260715.json"
POOL_EVIDENCE = D / "base_gl3_pool_interval_evidence_20260715.json"
RESULT = D / "base_gl3_gl12_alpha_beta_20260715.json"
REPORT = ROOT / "reports/GL3_gl12_alpha_beta_result_20260715.md"
LEDGER = D / "base_gl3_rpc_full_ledger_20260715.jsonl"
RPC_SUMMARY = D / "base_gl3_rpc_ledger_20260715.json"
CUTOFF = 46_865_124
WETH = "0x4200000000000000000000000000000000000006"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WINDOWS = (1, 5, 15)
CHUNK = 5_000
ADDRESS_BATCH = 40
ALLOWED = {"eth_call", "eth_getLogs", "eth_getTransactionReceipt"}
INPUT_HASHES = {
    str(GL2.relative_to(ROOT)): "966cf597c837070a40a3cce9c2defcdf11f25230c97adb93c69892bb62c5f197",
    str(CORE.relative_to(ROOT)): "e62e0f37bb6a2756256795bfaa0d5f9677b73e5e8d0836370ebf1afbdbbd5e2d",
    "mvp/diagnostics/base_gl2_a_trigger_evidence_reviewed_20260715.json": "827889eab4248980ec874fa030205e431090d56831703fbc09c6c03aba840cc3",
    str(ETH.relative_to(ROOT)): "728fc565687dddf9792553fd5ffc31072cd260b00dbe451bea54c9cad2f8e129",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def env_value(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    for line in (ROOT / "infra/compose/.env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def error_class(exc: Exception) -> str:
    text = str(exc).lower()
    if "execution reverted" in text:
        return "execution_reverted"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "rpc_other"


class Rpc:
    def __init__(self, *, reset: bool):
        url = env_value("EXECUTOR_READ_RPC_URL") or ""
        if "prettiest-blue-aura" not in url or "orbital" in url:
            raise RuntimeError("GL3 requires prettiest-blue-aura and forbids orbital")
        self.url = url
        self.lock = threading.Lock()
        self.logical = 0
        self.attempts = 0
        self.last_started = 0.0
        self.methods = Counter()
        self.errors = Counter()
        if reset:
            LEDGER.write_text("")
        elif LEDGER.exists():
            for line in LEDGER.read_text().splitlines():
                row = json.loads(line)
                self.logical = max(self.logical, int(row["logical_id"]))
                self.attempts = max(self.attempts, int(row["attempt_id"]))
                self.methods[row["method"]] += 1
                if row["status"] == "error":
                    self.errors[row["error_class"]] += 1

    def _write(self, row: dict) -> None:
        with self.lock:
            with LEDGER.open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def call(self, method: str, params: list, timeout: int = 180):
        if method not in ALLOWED:
            raise RuntimeError(f"FORBIDDEN_RPC_METHOD:{method}")
        with self.lock:
            self.logical += 1
            logical = self.logical
        request_hash = canonical_hash({"method": method, "params": params})
        last = None
        for retry in range(1, 5):
            with self.lock:
                wait = max(0.0, self.last_started + 0.025 - time.monotonic())
                if wait:
                    time.sleep(wait)
                self.last_started = time.monotonic()
                self.attempts += 1
                attempt = self.attempts
                self.methods[method] += 1
            started = time.time()
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": logical, "method": method, "params": params}).encode()
                req = Request(self.url, data=body, headers={"content-type": "application/json", "user-agent": "EATLQ-GL3-readonly/1"})
                with urlopen(req, timeout=timeout) as response:
                    obj = json.load(response)
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"]))
                result = obj.get("result")
                meta = {"request_sha256": request_hash, "response_sha256": canonical_hash(result)}
                self._write({"attempt_id": attempt, "logical_id": logical, "method": method, "retry": retry, "status": "ok", "elapsed_ms": round((time.time() - started) * 1000, 3), **meta})
                return result, meta
            except Exception as exc:
                last = exc
                category = error_class(exc)
                self.errors[category] += 1
                self._write({"attempt_id": attempt, "logical_id": logical, "method": method, "retry": retry, "status": "error", "error_class": category, "error_type": type(exc).__name__, "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(), "request_sha256": request_hash, "elapsed_ms": round((time.time() - started) * 1000, 3)})
                if category == "execution_reverted":
                    break
                time.sleep(0.5 * retry)
        raise RuntimeError(f"{type(last).__name__}:{str(last)[:180]}")

    def summary(self) -> dict:
        return {"endpoint_alias": "prettiest-blue-aura", "posture": "read-only; no send/sign/broadcast method implemented", "logical_calls": self.logical, "http_attempts": self.attempts, "methods_attempted": dict(sorted(self.methods.items())), "errors_by_type": dict(sorted(self.errors.items())), "full_ledger_path": str(LEDGER.relative_to(ROOT)), "full_ledger_sha256": sha(LEDGER)}


def verify_frozen() -> None:
    if sha(PREREG) != PREREG_SHA:
        raise RuntimeError("GL3 preregistration hash mismatch")
    for name, expected in INPUT_HASHES.items():
        if sha(ROOT / name) != expected:
            raise RuntimeError(f"frozen input hash mismatch: {name}")
    data = json.loads(GL2.read_text())
    counts = {w: sum(not r["windows"][w]["resolved"] for r in data["rows"]) for w in ("1", "5", "15")}
    if counts != {"1": 149, "5": 152, "15": 153}:
        raise RuntimeError(f"residual population drift: {counts}")


def ch_rows(sql: str, timeout: int = 900) -> list[dict]:
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query", sql], text=True, capture_output=True, timeout=timeout, check=True)
    return [json.loads(line) for line in p.stdout.splitlines() if line]


def residual_scope() -> tuple[dict[str, dict], dict[str, int], int]:
    data = json.loads(GL2.read_text())
    core = {row["pool"].lower(): row for row in json.loads(CORE.read_text())["rows"]}
    pools: dict[str, dict] = {}
    starts: dict[str, int] = {}
    for row in data["rows"]:
        unresolved = [int(w) for w, cell in row["windows"].items() if not cell["resolved"]]
        if not unresolved:
            continue
        pool = row["pool"].lower()
        frozen = core[pool]
        pools[pool] = {
            "pool": pool,
            "token": row["token"].lower(),
            "regime": row["regime"],
            "cohort": row["cohort"],
            "orient": int(frozen["orient"]),
            "fee": int(frozen["fee_evidence"]["fee"]),
        }
        starts[pool] = min(int(row["windows"][str(w)]["target_block"]) for w in unresolved)
    return pools, starts, min(starts.values())


def query_ch_events(pools: list[str], lower: int) -> tuple[list[dict], dict[str, str]]:
    quoted = ",".join("'" + p + "'" for p in pools)
    specs = {
        "mint": ("uniswap_v3_mints_decoded", "toString(liquidity) liquidity_raw,toString(amount0) amount0,toString(amount1) amount1"),
        "burn": ("uniswap_v3_burns_decoded", "toString(liquidity) liquidity_raw,toString(amount0) amount0,toString(amount1) amount1"),
        "swap": ("uniswap_v3_swaps_decoded", "toString(liquidity) liquidity_raw,toString(amount0) amount0,toString(amount1) amount1"),
    }
    out, hashes = [], {}
    for kind, (table, extra) in specs.items():
        sql = f"SELECT '{kind}' kind,lower(pool_address) pool,block_number block,lower(tx_hash) tx,log_index,{extra} FROM eatlq.{table} FINAL WHERE chain_id=8453 AND removed=0 AND block_number BETWEEN {lower} AND {CUTOFF} AND lower(pool_address) IN ({quoted}) ORDER BY pool,block,log_index FORMAT JSONEachRow"
        hashes[kind] = hashlib.sha256(sql.encode()).hexdigest()
        out.extend(ch_rows(sql))
    out.sort(key=lambda x: (x["pool"], int(x["block"]), int(x["log_index"]), x["kind"]))
    return out, hashes


def event_key(kind: str, pool: str, block: int, tx: str, log_index: int) -> str:
    return "|".join((kind, pool.lower(), str(block), tx.lower(), str(log_index)))


def signed_word(word: str) -> int:
    value = int(word, 16)
    return value - (1 << 256) if value >= (1 << 255) else value


def decode_event_amounts(kind: str, data: str) -> dict:
    body = data.removeprefix("0x")
    words = [body[i:i+64] for i in range(0, len(body), 64) if len(body[i:i+64]) == 64]
    if kind == "swap" and len(words) >= 2:
        return {"amount0": str(signed_word(words[0])), "amount1": str(signed_word(words[1]))}
    if kind == "mint" and len(words) >= 4:
        return {"liquidity_raw": str(int(words[1], 16)), "amount0": str(int(words[2], 16)), "amount1": str(int(words[3], 16))}
    if kind == "burn" and len(words) >= 3:
        return {"liquidity_raw": str(int(words[0], 16)), "amount0": str(int(words[1], 16)), "amount1": str(int(words[2], 16))}
    return {"decode_error": f"unexpected_{kind}_word_count:{len(words)}"}


def scan_event_logs(rpc: Rpc, pools: list[str], lower: int) -> tuple[list[dict], list[dict]]:
    tasks = []
    for offset in range(0, len(pools), ADDRESS_BATCH):
        addresses = pools[offset:offset + ADDRESS_BATCH]
        for begin in range(lower, CUTOFF + 1, CHUNK):
            end = min(CUTOFF, begin + CHUNK - 1)
            tasks.append((addresses, begin, end))
    logs, chunks = [], []

    def one(task):
        addresses, begin, end = task
        filt = {"fromBlock": hex(begin), "toBlock": hex(end), "address": addresses, "topics": [[MINT, BURN, SWAP]]}
        raw, meta = rpc.call("eth_getLogs", [filt])
        return raw, {"from_block": begin, "to_block": end, "address_count": len(addresses), "status": "ok", **meta}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(one, task) for task in tasks]
        for i, future in enumerate(as_completed(futures), 1):
            raw, chunk = future.result()
            logs.extend(raw)
            chunks.append(chunk)
            if i % 250 == 0 or i == len(futures):
                print(f"[GL3 event chunks] {i}/{len(futures)} logs={len(logs)}", flush=True)
    return logs, sorted(chunks, key=lambda x: (x["from_block"], x["address_count"], x["request_sha256"]))


def inventory_mode() -> None:
    verify_frozen()
    pools, starts, lower = residual_scope()
    ch, queries = query_ch_events(sorted(pools), lower)
    rpc = Rpc(reset=True)
    raw, chunks = scan_event_logs(rpc, sorted(pools), lower)
    topic_kind = {MINT: "mint", BURN: "burn", SWAP: "swap"}
    rpc_events = []
    for log in raw:
        topic = str(log.get("topics", [""])[0]).lower()
        if topic not in topic_kind:
            continue
        kind = topic_kind[topic]
        data = str(log.get("data", ""))
        rpc_events.append({"kind": kind, "pool": str(log["address"]).lower(), "block": int(log["blockNumber"], 16), "tx": str(log["transactionHash"]).lower(), "log_index": int(log["logIndex"], 16), "data_sha256": hashlib.sha256(data.encode()).hexdigest(), **decode_event_amounts(kind, data)})
    ck = {event_key(x["kind"], x["pool"], int(x["block"]), x["tx"], int(x["log_index"])) for x in ch}
    rk = {event_key(x["kind"], x["pool"], int(x["block"]), x["tx"], int(x["log_index"])) for x in rpc_events}
    per_pool = {}
    for pool in sorted(pools):
        start = starts[pool]
        pck = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in ch if x["pool"] == pool and int(x["block"]) >= start}
        prk = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in rpc_events if x["pool"] == pool and int(x["block"]) >= start}
        per_pool[pool] = {
            "ch_event_keys_n": len(pck),
            "rpc_event_keys_n": len(prk),
            "ch_minus_rpc": sorted(pck-prk),
            "rpc_minus_ch": sorted(prk-pck),
            "event_keys_equal": pck == prk,
        }
    payload = {"schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "research_only": True, "preregistration_sha256": PREREG_SHA, "scope": {"pool_n": len(pools), "lower_block": lower, "cutoff_block": CUTOFF, "per_pool_start": starts}, "ch_query_sha256": queries, "ch_events": ch, "rpc_events": sorted(rpc_events, key=lambda x: (x["pool"], x["block"], x["log_index"])), "coverage": {"chunk_size_blocks": CHUNK, "chunks": chunks, "all_chunks_ok": len(chunks) > 0 and all(x["status"] == "ok" for x in chunks), "ch_event_keys_n": len(ck), "rpc_event_keys_n": len(rk), "ch_minus_rpc": sorted(ck-rk), "rpc_minus_ch": sorted(rk-ck), "event_keys_equal": ck == rk, "per_pool": per_pool}, "rpc_ledger": rpc.summary(), "source_hashes": INPUT_HASHES}
    INVENTORY.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_rpc_summary(rpc, {str(INVENTORY.relative_to(ROOT)): sha(INVENTORY)})
    print(json.dumps({"scope": payload["scope"], "coverage": {k:v for k,v in payload["coverage"].items() if k != "chunks"}, "event_counts": dict(Counter(x["kind"] for x in ch))}, indent=2))


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


def compile_dual_balance() -> str:
    out = Path("/tmp/gl3_forgeout")
    art = out / "DualBalanceCreate.sol/DualBalanceCreate.json"
    if not art.exists():
        subprocess.run([str(Path.home()/".foundry/bin/forge"), "build", "--contracts", "scripts/research/base_gl3/DualBalanceCreate.sol", "--out", str(out), "--cache-path", "/tmp/gl3_forgecache"], cwd=ROOT, check=True)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def dual_balance(rpc: Rpc, code: str, token: str, pool: str, block: int) -> dict:
    data = "0x" + code + token.removeprefix("0x").rjust(64, "0") + WETH.removeprefix("0x").rjust(64, "0") + pool.removeprefix("0x").rjust(64, "0")
    raw, meta = rpc.call("eth_call", [{"data": data, "gas": hex(3_000_000)}, hex(block)])
    if not raw or len(raw) < 130:
        raise RuntimeError("short dual balance response")
    return {"token_raw": str(int(raw[2:66], 16)), "weth_raw": str(int(raw[66:130], 16)), "status": "measured", **meta}


def transfer_net_at(rpc: Rpc, pool: str, token: str, block: int) -> dict:
    raw, meta = rpc.call("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(block), "address": [token, WETH], "topics": [TRANSFER]}])
    nets = {token: 0, WETH: 0}
    incoming = {token: 0, WETH: 0}
    outgoing = {token: 0, WETH: 0}
    for log in raw:
        topics = [str(x).lower() for x in log.get("topics", [])]
        if len(topics) < 3:
            continue
        address = str(log["address"]).lower()
        sender, recipient = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
        amount = int(str(log.get("data", "0x0")), 16)
        if recipient == pool:
            nets[address] += amount; incoming[address] += amount
        if sender == pool:
            nets[address] -= amount; outgoing[address] += amount
    return {"status": "measured", "log_n": len(raw), "token_net_raw": str(nets[token]), "weth_net_raw": str(nets[WETH]), "token_in_raw": str(incoming[token]), "weth_in_raw": str(incoming[WETH]), "token_out_raw": str(outgoing[token]), "weth_out_raw": str(outgoing[WETH]), **meta}


def receipt_mint_net(rpc: Rpc, tx: str, pool: str, token: str) -> dict:
    raw, meta = rpc.call("eth_getTransactionReceipt", [tx])
    if not raw or int(raw.get("status", "0x0"), 16) != 1:
        return {"status": "unknown", "reason": "receipt_missing_or_failed", **meta}
    tin = win = 0
    for log in raw.get("logs", []):
        topics = [str(x).lower() for x in log.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER:
            continue
        address = str(log["address"]).lower()
        recipient = "0x" + topics[2][-40:]
        if recipient != pool:
            continue
        amount = int(str(log.get("data", "0x0")), 16)
        if address == token: tin += amount
        if address == WETH: win += amount
    return {"status": "measured", "token_in_raw": str(tin), "weth_in_raw": str(win), "block": int(raw["blockNumber"], 16), **meta}


def pool_settle(rpc: Rpc, code: str, atomic_code: str, old, pool: str, info: dict, events: list[dict], checkpoint_starts: list[int], coverage_ok: bool) -> dict:
    token = info["token"]
    event_blocks = sorted({int(e["block"]) for e in events})
    checkpoint_blocks = sorted(set(event_blocks) | set(checkpoint_starts) | {CUTOFF})
    checks = []
    data_gap = []
    for i, block in enumerate(checkpoint_blocks):
        try:
            before = dual_balance(rpc, code, token, pool, block-1)
            after = dual_balance(rpc, code, token, pool, block)
            transfers = transfer_net_at(rpc, pool, token, block)
            td = int(after["token_raw"])-int(before["token_raw"])
            wd = int(after["weth_raw"])-int(before["weth_raw"])
            equal = td == int(transfers["token_net_raw"]) and wd == int(transfers["weth_net_raw"])
            checks.append({"block": block, "before": before, "after": after, "transfers": transfers, "token_delta_raw": str(td), "weth_delta_raw": str(wd), "integer_equal": equal})
            if not equal: data_gap.append(f"reserve_transfer_mismatch:{block}")
        except Exception as exc:
            data_gap.append(f"reserve_rpc:{block}:{error_class(exc)}")
        if (i+1) % 100 == 0:
            print(f"[GL3 reserves {pool[:10]}] {i+1}/{len(checkpoint_blocks)}", flush=True)
    mints = [e for e in events if e["kind"] == "mint"]
    mint_receipts = []
    for event in mints:
        try:
            receipt = receipt_mint_net(rpc, event["tx"], pool, token)
            mint_receipts.append({"event": event, "receipt": receipt})
            if receipt.get("status") != "measured": data_gap.append(f"mint_receipt:{event['tx']}")
        except Exception as exc:
            data_gap.append(f"mint_receipt:{event['tx']}:{error_class(exc)}")
    return {"pool": pool, "token": token, "event_n": len(events), "event_counts": dict(Counter(e["kind"] for e in events)), "event_blocks_n": len(event_blocks), "checkpoint_blocks_n": len(checkpoint_blocks), "reserve_checks": checks, "reserve_all_integer_equal": len(checks) == len(checkpoint_blocks) and all(x["integer_equal"] for x in checks), "mint_receipts": mint_receipts, "data_gaps": data_gap, "coverage_ok": coverage_ok}


def settle_mode() -> None:
    verify_frozen()
    inv = json.loads(INVENTORY.read_text())
    if inv["preregistration_sha256"] != PREREG_SHA:
        raise RuntimeError("inventory prereg mismatch")
    pools, starts, _ = residual_scope()
    gl2 = json.loads(GL2.read_text())
    checkpoints = defaultdict(set)
    for row in gl2["rows"]:
        pool = row["pool"].lower()
        for cell in row["windows"].values():
            if not cell["resolved"]:
                checkpoints[pool].add(int(cell["target_block"]))
    by_pool = defaultdict(list)
    for event in inv["rpc_events"]:
        if int(event["block"]) >= starts[event["pool"]]:
            by_pool[event["pool"]].append(event)
    rpc = Rpc(reset=False)
    dual_code = compile_dual_balance()
    old = module(ROOT / "scripts/research/base_reaudit10/reaudit10_rpc.py", "gl3_old")
    atomic_code = old.compile_bytecode()
    evidence = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(pool_settle, rpc, dual_code, atomic_code, old, pool, info, by_pool[pool], sorted(checkpoints[pool]), bool(inv["coverage"]["per_pool"][pool]["event_keys_equal"] and inv["coverage"]["all_chunks_ok"])): pool for pool, info in pools.items()}
        for i, future in enumerate(as_completed(futures), 1):
            evidence.append(future.result())
            print(f"[GL3 pools] {i}/{len(futures)}", flush=True)
    evidence.sort(key=lambda x: x["pool"])
    payload = {"schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "research_only": True, "preregistration_sha256": PREREG_SHA, "inventory_sha256": sha(INVENTORY), "pools": evidence, "summary": {"pool_n": len(evidence), "event_n": sum(x["event_n"] for x in evidence), "event_blocks_n": sum(x["event_blocks_n"] for x in evidence), "reserve_checks_n": sum(len(x["reserve_checks"]) for x in evidence), "reserve_integer_equal_n": sum(sum(y["integer_equal"] for y in x["reserve_checks"]) for x in evidence), "pools_all_green": sum(x["coverage_ok"] and x["reserve_all_integer_equal"] and not x["data_gaps"] for x in evidence), "data_gap_pools": sum(bool(x["data_gaps"]) for x in evidence)}, "rpc_ledger": rpc.summary()}
    POOL_EVIDENCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_rpc_summary(rpc, {str(INVENTORY.relative_to(ROOT)): sha(INVENTORY), str(POOL_EVIDENCE.relative_to(ROOT)): sha(POOL_EVIDENCE)})
    print(json.dumps(payload["summary"], indent=2))


def reindex_mode() -> None:
    """Mechanically refresh per-pool post-exit coverage from a completed inventory."""
    verify_frozen()
    inv = json.loads(INVENTORY.read_text())
    starts = {k: int(v) for k, v in inv["scope"]["per_pool_start"].items()}
    per_pool = {}
    for pool, start in sorted(starts.items()):
        ck = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in inv["ch_events"] if x["pool"] == pool and int(x["block"]) >= start}
        rk = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in inv["rpc_events"] if x["pool"] == pool and int(x["block"]) >= start}
        per_pool[pool] = {"ch_event_keys_n": len(ck), "rpc_event_keys_n": len(rk), "ch_minus_rpc": sorted(ck-rk), "rpc_minus_ch": sorted(rk-ck), "event_keys_equal": ck == rk}
    inv["coverage"]["per_pool"] = per_pool
    inv["coverage"]["post_exit_per_pool_equal_n"] = sum(x["event_keys_equal"] for x in per_pool.values())
    INVENTORY.write_text(json.dumps(inv, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"post_exit_per_pool_equal_n": inv["coverage"]["post_exit_per_pool_equal_n"], "pool_n": len(per_pool)}, indent=2))


def write_rpc_summary(rpc: Rpc, outputs: dict[str, str]) -> None:
    payload = {"schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "research_only": True, "preregistration_sha256": PREREG_SHA, "rpc": rpc.summary(), "outputs": outputs}
    RPC_SUMMARY.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def strict_exit_failure(cell: dict) -> tuple[bool, list[str]]:
    reasons = []
    atomic = cell.get("atomic_guard") or {}
    if atomic.get("status") != "measured" or atomic.get("sell_ok"):
        reasons.append("exit_atomic_not_strict_failure")
    quote = cell.get("exact_quote") or {}
    qtext = str(quote.get("error", "")).lower()
    if quote.get("status") == "measured" or "execution reverted" not in qtext:
        reasons.append("exit_quote_not_explicit_execution_revert")
    return not reasons, reasons


def true_sell(event: dict, orient: int) -> bool:
    a0, a1 = int(event.get("amount0", 0)), int(event.get("amount1", 0))
    weth, token = (a0, a1) if orient == -1 else (a1, a0)
    return event.get("kind") == "swap" and weth < 0 and token > 0


def percentile(values: list[float], q: float) -> float | None:
    return None if not values else float(np.quantile(values, q, method="linear"))


def bounded_stats(rows: list[dict], window: str) -> dict:
    cells = [row["windows"][window] for row in rows]
    resolved = [float(cell["net_return"]) for cell in cells if cell["resolved"]]
    missing = len(cells) - len(resolved)
    median = percentile(resolved, .5)
    p75 = percentile(resolved, .75)
    pessimistic = (sum(resolved) - missing) / len(cells)
    optimistic = (sum(resolved) + missing * median) / len(cells) if median is not None else None
    optimistic_p75 = (sum(resolved) + missing * p75) / len(cells) if p75 is not None else None
    evidence = Counter(cell["evidence"] for cell in cells)
    return {
        "n_total": len(cells),
        "n_resolved": len(resolved),
        "n_unresolved": missing,
        "n_confirmed_continuously_unsellable": evidence["CONFIRMED_CONTINUOUSLY_UNSELLABLE"],
        "resolved_coverage": len(resolved) / len(cells),
        "evidence_counts": dict(sorted(evidence.items())),
        "resolved_distribution": {
            "mean": float(np.mean(resolved)) if resolved else None,
            "p05": percentile(resolved, .05), "p25": percentile(resolved, .25),
            "p50": median, "p75": p75, "p95": percentile(resolved, .95),
            "win_rate": sum(x > 0 for x in resolved) / len(resolved) if resolved else None,
        },
        "full_population_ev": {
            "identified_point": float(np.mean(resolved)) if not missing else None,
            "pessimistic": pessimistic,
            "median_optimistic": optimistic,
            "p75_optimistic_sensitivity": optimistic_p75,
            "assignment": "residual=-1 / cohort-window resolved median / cohort-window resolved p75",
        },
    }


def share_intervals(beta: tuple[float, float], keystone: tuple[float, float]) -> dict:
    if keystone[0] <= 0 <= keystone[1]:
        return {"beta_signed_share": "UNDEFINED", "selection_alpha_signed_share": "UNDEFINED", "reason": "keystone_interval_crosses_zero"}
    ratios = [b/k for b in beta for k in keystone]
    blo, bhi = min(ratios), max(ratios)
    return {"beta_signed_share": [blo, bhi], "selection_alpha_signed_share": [1-bhi, 1-blo]}


def adjudicate_mode() -> None:
    verify_frozen()
    gl2 = json.loads(GL2.read_text())
    inv = json.loads(INVENTORY.read_text())
    pe = json.loads(POOL_EVIDENCE.read_text())
    core = {row["pool"].lower(): row for row in json.loads(CORE.read_text())["rows"]}
    evidence = {row["pool"]: row for row in pe["pools"]}
    events = defaultdict(list)
    for event in inv["rpc_events"]:
        events[event["pool"]].append(event)
    rows = deepcopy(gl2["rows"])
    adjudications = []
    for row in rows:
        pool = row["pool"].lower()
        frozen = core[pool]
        ev = evidence.get(pool)
        for window, cell in row["windows"].items():
            if cell["resolved"]:
                continue
            target = int(cell["target_block"])
            after = [event for event in events[pool] if int(event["block"]) >= target]
            ch_after = [event for event in inv["ch_events"] if event["pool"] == pool and int(event["block"]) >= target]
            ch_keys = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in ch_after}
            rpc_keys = {event_key(x["kind"], pool, int(x["block"]), x["tx"], int(x["log_index"])) for x in after}
            cell_event_equal = ch_keys == rpc_keys
            sells = [event for event in after if true_sell(event, int(frozen["orient"]))]
            mints = [event for event in after if event["kind"] == "mint" and int(event.get("liquidity_raw", 0)) > 0]
            exit_failed, failure_reasons = strict_exit_failure(cell)
            gaps = []
            if ev is None:
                gaps.append("missing_pool_evidence")
            else:
                if not cell_event_equal: gaps.append("post_exit_ch_rpc_event_key_mismatch")
                if not ev["reserve_all_integer_equal"]: gaps.append("reserve_transfer_integer_mismatch")
                gaps.extend(ev.get("data_gaps", []))
            if failure_reasons: gaps.extend(failure_reasons)
            mint_receipts = {x["event"]["tx"]: x["receipt"] for x in (ev or {}).get("mint_receipts", [])}
            qualified_mints = []
            for mint in mints:
                receipt = mint_receipts.get(mint["tx"])
                if not receipt or receipt.get("status") != "measured":
                    gaps.append(f"mint_receipt_missing:{mint['tx']}")
                elif int(receipt["token_in_raw"]) + int(receipt["weth_in_raw"]) > 0:
                    qualified_mints.append(mint)
            # Every covered pool has no post-exit candidate in this run.  Keep the
            # generic fields explicit so a future rerun cannot silently invent one.
            candidate_blocks = sorted({int(x["block"]) for x in qualified_mints + sells})
            delayed_tests = []
            if candidate_blocks and not gaps:
                gaps.append("candidate_retest_required_but_not_executed")
            if not gaps and exit_failed and not sells and not qualified_mints:
                cell.update({
                    "resolved": True,
                    "evidence": "CONFIRMED_CONTINUOUSLY_UNSELLABLE",
                    "exit_weth_raw": "0", "frozen_cost_weth_raw": "0",
                    "net_weth_raw": str(-10**15), "net_return": -1.0,
                    "gl3_full_interval": {
                        "adjudication": "CONFIRMED_CONTINUOUSLY_UNSELLABLE",
                        "scan_from_block": target, "scan_through_block": CUTOFF,
                        "post_exit_ch_rpc_event_keys_equal": cell_event_equal,
                        "reserve_transfer_integer_equal": True,
                        "true_sell_n": 0, "qualified_mint_n": 0,
                        "candidate_retests": [],
                        "mechanism": "exit atomic/quote strict failure; no Mint or true sell recovery boundary through cutoff",
                    },
                })
                outcome = "CONFIRMED_CONTINUOUSLY_UNSELLABLE"
            else:
                cell["gl3_full_interval"] = {
                    "adjudication": "UNRESOLVED_DATA_GAP_OR_RECOVERY_RETEST",
                    "scan_from_block": target, "scan_through_block": CUTOFF,
                    "post_exit_ch_rpc_event_keys_equal": cell_event_equal,
                    "reserve_transfer_integer_equal": bool(ev and ev["reserve_all_integer_equal"]),
                    "true_sell_n": len(sells), "qualified_mint_n": len(qualified_mints),
                    "candidate_blocks": candidate_blocks, "candidate_retests": delayed_tests,
                    "data_gaps": sorted(set(gaps)),
                }
                outcome = "UNRESOLVED"
            adjudications.append({"pool": pool, "window": int(window), "outcome": outcome, "target_block": target, "event_n": len(after), "true_sell_n": len(sells), "qualified_mint_n": len(qualified_mints), "data_gaps": sorted(set(gaps))})
    selected = [row for row in rows if row["cohort"]["keystone_selected"]]
    summaries = {}
    all_alpha_lower_green = True
    for window in map(str, WINDOWS):
        full = bounded_stats(rows, window)
        key = bounded_stats(selected, window)
        fb = full["full_population_ev"]
        kb = key["full_population_ev"]
        alpha_lower = kb["pessimistic"] - fb["median_optimistic"]
        alpha_upper = kb["median_optimistic"] - fb["pessimistic"]
        alpha_p75_upper = key["full_population_ev"]["p75_optimistic_sensitivity"] - fb["pessimistic"]
        all_alpha_lower_green &= alpha_lower >= .10
        summaries[window] = {
            "full_universe": full, "keystone_subset": key,
            "alpha_beta": {
                "beta_full_universe_interval": [fb["pessimistic"], fb["median_optimistic"]],
                "keystone_interval": [kb["pessimistic"], kb["median_optimistic"]],
                "selection_alpha_interval": [alpha_lower, alpha_upper],
                "selection_alpha_p75_sensitivity_upper": alpha_p75_upper,
                **share_intervals((fb["pessimistic"], fb["median_optimistic"]), (kb["pessimistic"], kb["median_optimistic"])),
            },
        }
    payload = {
        "schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "family": "F1/F4/F7", "sop_applied": True,
        "research_only": True,
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "next_gate": "INDEPENDENT_AGENT_LINE_REVIEW",
        "preregistration": {"path": str(PREREG.relative_to(ROOT)), "sha256": PREREG_SHA, "modified": False},
        "authoritative_estimand": {"entry": "entry_block+1", "exit": "trigger_block+1/+5/+15", "notional_weth_raw": str(10**15), "pricing": "WETH"},
        "population": {"full_universe_n": len(rows), "keystone_subset_n": len(selected), "frozen_residual_cells": {"1": 149, "5": 152, "15": 153}},
        "event_reserve_summary": {**pe["summary"], "post_exit_event_key_equal_pools": inv["coverage"]["post_exit_per_pool_equal_n"], "post_exit_event_key_gap_pools": len(evidence)-inv["coverage"]["post_exit_per_pool_equal_n"]},
        "adjudication_counts": dict(sorted(Counter(x["outcome"] for x in adjudications).items())),
        "adjudications": adjudications, "summaries": summaries, "rows": rows,
        "review_candidate": {"grade": "IRON" if all_alpha_lower_green else "PINNED", "is_final_grade": False, "rule": "all three alpha_lower >= +0.10 plus independent anchor review", "all_three_alpha_lower_ge_10pp": bool(all_alpha_lower_green)},
        "source_hashes": {str(path.relative_to(ROOT)): sha(path) for path in (GL2, CORE, ETH, INVENTORY, POOL_EVIDENCE, LEDGER)},
        "posture": "Codex does not assign final grade or GO; independent reviewer must line-review anchors",
    }
    RESULT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_result_report(payload)
    rpc = Rpc(reset=False)
    write_rpc_summary(rpc, {str(INVENTORY.relative_to(ROOT)): sha(INVENTORY), str(POOL_EVIDENCE.relative_to(ROOT)): sha(POOL_EVIDENCE), str(RESULT.relative_to(ROOT)): sha(RESULT), str(REPORT.relative_to(ROOT)): sha(REPORT)})
    print(json.dumps({"adjudication_counts": payload["adjudication_counts"], "summaries": payload["summaries"], "review_candidate": payload["review_candidate"]}, indent=2))


def fmt_pct(value) -> str:
    return "NA" if value is None else f"{100*value:+.2f}%"


def write_result_report(payload: dict) -> None:
    lines = [
        "---", "sop_applied: true", "alpha_id: AR-GL3-CLOSE-GL12", "family: F1/F4/F7",
        "next_gate: INDEPENDENT_AGENT_LINE_REVIEW", "decision_flags:", "  training_allowed: false",
        "  live_allowed: false", "  self_go_allowed: false", "---", "",
        "# GL3 · GL1-12 全区间恢复与 alpha/beta 收口", "",
        "本报告不自定最终分级、不判 GO。唯一口径是 entry_block+1 入场、trigger_block+1/+5/+15 出场；全宇宙 431 个同龄候选，KEYSTONE 为其中冻结 116 个，WETH 计价。", "",
        "## 全区间证据", "",
        f"- 连续 RPC 事件分片全部成功；出场后 CH/RPC 事件键一致池 {payload['event_reserve_summary']['post_exit_event_key_equal_pools']}/153，缺口池 {payload['event_reserve_summary']['post_exit_event_key_gap_pools']}/153。",
        f"- 真储备核验 {payload['event_reserve_summary']['reserve_integer_equal_n']}/{payload['event_reserve_summary']['reserve_checks_n']} 个检查点原始整数相等；数据缺口池 {payload['event_reserve_summary']['data_gap_pools']}。",
        f"- residual cell 裁定：持续不可卖 {payload['adjudication_counts'].get('CONFIRMED_CONTINUOUSLY_UNSELLABLE',0)}，仍 unresolved {payload['adjudication_counts'].get('UNRESOLVED',0)}；只有全区间双源与储备证据全绿者才使用 -100%。", "",
        "## 逐窗分布与有界 EV", "",
        "|窗|人群|resolved/total|unresolved|持续不可卖|resolved p05/p25/p50/p75/p95|全宇宙 EV 点/区间|", "|---|---|---:|---:|---:|---|---|",
    ]
    for window in map(str, WINDOWS):
        for key, label in (("full_universe", "全宇宙"), ("keystone_subset", "KEYSTONE")):
            s = payload["summaries"][window][key]; d = s["resolved_distribution"]; b = s["full_population_ev"]
            dist = "/".join(fmt_pct(d[x]) for x in ("p05", "p25", "p50", "p75", "p95"))
            ev = fmt_pct(b["identified_point"]) if b["identified_point"] is not None else f"[{fmt_pct(b['pessimistic'])}, {fmt_pct(b['median_optimistic'])}]"
            lines.append(f"|+{window}|{label}|{s['n_resolved']}/{s['n_total']}|{s['n_unresolved']}|{s['n_confirmed_continuously_unsellable']}|{dist}|{ev}|")
    lines += ["", "## alpha / beta", "", "|窗|beta（全宇宙）|KEYSTONE|条件 alpha 保守区间|alpha 下沿|", "|---|---|---|---|---:|"]
    for window in map(str, WINDOWS):
        a = payload["summaries"][window]["alpha_beta"]
        lines.append(f"|+{window}|[{fmt_pct(a['beta_full_universe_interval'][0])}, {fmt_pct(a['beta_full_universe_interval'][1])}]|[{fmt_pct(a['keystone_interval'][0])}, {fmt_pct(a['keystone_interval'][1])}]|[{fmt_pct(a['selection_alpha_interval'][0])}, {fmt_pct(a['selection_alpha_interval'][1])}]|{fmt_pct(a['selection_alpha_interval'][0])}|")
    lines += ["", f"供独立审查的机械候选为 `{payload['review_candidate']['grade']}`，但这不是最终分级。9 个池因冻结指定的 ClickHouse 解码表与原始 RPC 事件键不等而保留 unresolved；未把缺失当全损，也未用报价冒充成交。", "", "纯只读重核；executor 保持关闭；未下单、签名、广播、训练或修改 live/signals/Redis。", ""]
    REPORT.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inventory", "reindex", "settle", "adjudicate"))
    args = parser.parse_args()
    if args.mode == "inventory": inventory_mode()
    elif args.mode == "reindex": reindex_mode()
    elif args.mode == "settle": settle_mode()
    else: adjudicate_mode()


if __name__ == "__main__":
    main()
