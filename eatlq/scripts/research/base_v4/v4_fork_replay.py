#!/usr/bin/env python3
"""Phase 3: V4 fork-replay ground-truth labeler (hook-aware, Universal Router)."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from live_gate import executor_healthy, wait_for_live_clear
from research_rpc import require_rpc_url
from v4_pool_swap import V4PoolSwapExecutor, weth_zero_for_one
from v4_tx_replay import build_swap_index, estimate_index_rpc, load_swap_index, simulate_tx_call

POOLS_PATH = "/tmp/v4_created_pools.jsonl"
OUT_PATH = os.path.join(HERE, "_fork_replay_labels.json")
OUT_V2_PATH = os.path.join(HERE, "_fork_replay_labels_v2.json")
WEAK_MERGED_PATH = os.path.join(HERE, "_fork_replay_labels_merged.json")
ROUNDTRIP_PATH = os.path.join(HERE, "_fork_replay_labels_roundtrip.json")
MERGED_STRONG_PATH = os.path.join(HERE, "_fork_replay_labels_merged_strong.json")
BATCH_CHECKPOINT = os.path.join(HERE, "_fork_replay_batch_checkpoint.json")
SELFTEST_PATH = os.path.join(HERE, "_fork_replay_selftest.json")
MAX_POOLS_PER_ROUND = 120
BATCH_TIME_LIMIT_S = 90 * 60
TIER_QUOTAS = {"vanilla": 25, "returns_delta_high": 25, "other_hooked": 25}
RPC_WARN_TOTAL = 18_000
MAX_RETRIES = 2
MAX_ANVIL_PARALLEL = 1  # self-test sequential; batch uses 1 to protect VPS
HOLD_OFFSETS = [0, 30, 300, 1800, 7200]
LIQUIDITY_BLOCK_OFFSET = 50

ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
ARCHIVE_RPC = require_rpc_url("RESEARCH_ARCHIVE_RPC_URL", fallback_var="RESEARCH_RPC_URL")
ACCT = Web3.to_checksum_address("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

TOP_RD_HOOKS = [
    "0xb429d62f8f3bFFb98CdB9569533eA23bF0Ba28CC",
    "0xBDF938149ac6a781F94FAa0ed45E6A0e984c6544",
    "0xd60D6B218116cFd801E28F78d011a203D2b068Cc",
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_anvil(block: int, port: int, wait_s: int = 120) -> subprocess.Popen:
    cmd = [
        "nice", "-n", "19",
        ANVIL, "--fork-url", ARCHIVE_RPC,
        "--fork-block-number", str(block),
        "--port", str(port), "--silent", "--no-rate-limit",
    ]
    if os.path.exists("/usr/bin/cpulimit"):
        cmd = ["cpulimit", "-l", "200", "--", *cmd]  # ~25% of 8 cores
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 90}))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            if w3.is_connected() and w3.eth.block_number > 0:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("anvil failed to start")


def replay_at_block(block: int, pool_row: dict, spend_wei: int, retries: int = MAX_RETRIES) -> dict:
    last_err = None
    fork_block = block + LIQUIDITY_BLOCK_OFFSET
    for attempt in range(retries + 1):
        port = _free_port()
        proc = None
        try:
            proc = _start_anvil(fork_block, port)
            w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 90}))
            ex = V4PoolSwapExecutor(w3, ACCT, PK)
            res = ex.roundtrip(pool_row, spend_wei)
            res["block"] = fork_block
            res["entry_block"] = block
            res["zero_for_one"] = weth_zero_for_one(pool_row)
            return res
        except Exception as e:
            last_err = str(e)[:120]
            time.sleep(2 + attempt * 2)
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
    return {"buy_ok": False, "sell_ok": False, "error": last_err, "block": fork_block, "entry_block": block}


def _tradeable(rep: dict) -> bool:
    return bool(rep.get("buy_ok") and rep.get("sell_ok"))


def label_pool_tx(pool_row: dict, index: dict) -> dict:
    """Cheap ground truth: replay a cached on-chain swap tx (0 RPC after index built)."""
    rep = _tx_replay_for_pool(pool_row, index)
    entry = int(pool_row["blockNumber"])
    if rep.get("ok"):
        return {"label": "onchain_swap_ok", "entry_block": entry, "tx_replay": rep}
    err = (rep.get("error") or "").lower()
    if "allowance" in err or "0xf4059071" in err:
        return {"label": "replay_blocked", "entry_block": entry, "tx_replay": rep, "note": rep.get("error")}
    return {"label": "dead", "entry_block": entry, "tx_replay": rep, "note": rep.get("error")}


def label_pool(pool_row: dict, *, spend_eth: float = 0.005, offsets=None) -> dict:
    offsets = offsets or HOLD_OFFSETS
    entry = int(pool_row["blockNumber"])
    scans = []
    e = replay_at_block(entry, pool_row, int(spend_eth * 1e18))
    scans.append({"offset": 0, "block": entry, **e})
    if not e.get("buy_ok"):
        return {"label": "dead", "entry_block": entry, "scans": scans, "note": e.get("error")}
    if not e.get("sell_ok"):
        err = (e.get("error") or "").lower()
        if "returns_delta" in err or "hook" in err:
            return {"label": "hook_block", "entry_block": entry, "scans": scans, "note": e.get("error")}
        return {"label": "instant_hp", "entry_block": entry, "scans": scans, "note": e.get("error")}
    last_clean = entry
    for off in offsets:
        if off == 0:
            continue
        blk = entry + off
        r = replay_at_block(blk, pool_row, int(spend_eth * 1e18))
        scans.append({"offset": off, "block": blk, **r})
        if not _tradeable(r):
            if r.get("buy_ok") and not r.get("sell_ok"):
                return {"label": "hook_block", "flip_block": blk, "entry_block": entry, "scans": scans}
            return {"label": "delayed_rug", "mechanism": "unknown", "flip_block": blk,
                    "entry_block": entry, "scans": scans, "note": r.get("error")}
        last_clean = blk
    return {"label": "clean", "entry_block": entry, "scans": scans}


def load_pools(path: str = POOLS_PATH) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()]


def _is_technical_error(err: Optional[str]) -> bool:
    if not err:
        return False
    e = err.lower()
    return any(x in e for x in (
        "anvil", "failed to start", "helper deploy", "timeout", "connection",
        "runtimeerror", "contractlogicerror:('execution reverted', '0x')",
    )) and "hook" not in e


def _classify_strong_label(pool_row: dict, lab: dict) -> str:
    """Map scan results to Phase 4 strong label enum."""
    label = lab.get("label", "dead")
    scans = lab.get("scans") or []
    entry = scans[0] if scans else {}
    note = (lab.get("note") or entry.get("error") or "").lower()
    prof = pool_row.get("hook_profile") or {}

    if _is_technical_error(lab.get("note") or entry.get("error")):
        return "roundtrip_error"
    if label == "clean":
        return "clean"
    if label == "delayed_rug":
        return "delayed_rug"
    if label == "instant_hp":
        if prof.get("has_returns_delta"):
            slip = entry.get("slippage")
            if slip is not None and slip < -0.3:
                return "delta_tax"
            return "delta_tax" if "delta" in note or "returns" in note else "instant_hp"
        return "instant_hp"
    if label == "hook_block":
        return "delta_tax" if prof.get("has_returns_delta") else "hook_block"
    if label == "dead":
        if "fee" in note or "dynamic" in note:
            return "dynamic_fee_spike"
        return "dead"
    return label


def label_pool_strong(pool_row: dict, *, spend_eth: float = 0.003) -> dict:
    entry = int(pool_row["blockNumber"])
    try:
        lab = label_pool(pool_row, spend_eth=spend_eth, offsets=HOLD_OFFSETS)
        strong = _classify_strong_label(pool_row, lab)
        return {
            **lab,
            "label": strong,
            "label_source": "roundtrip_strong",
            "entry_block": entry,
        }
    except Exception as e:
        return {
            "label": "roundtrip_error",
            "label_source": "roundtrip_strong",
            "entry_block": entry,
            "scans": [],
            "note": str(e)[:120],
        }


def get_universe_pools(tier: Optional[str] = None) -> list[dict]:
    """211 WETH pools from weak merged labels."""
    if not os.path.exists(WEAK_MERGED_PATH):
        raise FileNotFoundError(WEAK_MERGED_PATH)
    pids = {p["poolId"] for p in json.load(open(WEAK_MERGED_PATH)).get("pools", [])}
    rows = [r for r in load_pools() if r["poolId"] in pids and r.get("has_weth")]
    if tier == "vanilla":
        return [r for r in rows if r.get("hook_profile", {}).get("risk_tier") == "vanilla"]
    if tier == "returns_delta_high":
        return [r for r in rows if r.get("hook_profile", {}).get("risk_tier") == "returns_delta_high"]
    if tier == "other":
        return [r for r in rows if r.get("hook_profile", {}).get("risk_tier") in ("standard", "unknown")]
    return rows


def merge_strong_labels(
    weak_path: str = WEAK_MERGED_PATH,
    strong_path: str = ROUNDTRIP_PATH,
    out_path: str = MERGED_STRONG_PATH,
) -> dict:
    weak = _load_label_file(weak_path)
    strong = _load_label_file(strong_path)
    merged = {}
    for pid in set(weak) | set(strong):
        w, s = weak.get(pid, {}), strong.get(pid, {})
        rec = {**w, **s}
        rec["poolId"] = pid
        rec["weak_label"] = w.get("label")
        rec["strong_label"] = s.get("label")
        rec["label_weak"] = w.get("label")
        rec["label_strong"] = s.get("label")
        merged[pid] = rec
    out = {"n": len(merged), "pools": list(merged.values())}
    json.dump(out, open(out_path, "w"), indent=2)
    return out


def _load_label_file(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    data = json.load(open(path))
    return {p["poolId"]: p for p in data.get("pools", [])}


def selftest_roundtrip(limit: int = 2, spend_eth: float = 0.003) -> int:
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        print("STOP: live gate", flush=True)
        return 2
    rows = load_pools()
    vanilla = [r for r in rows if r.get("has_weth") and r.get("hook_profile", {}).get("is_vanilla")][:1]
    rd = [r for r in rows if r.get("has_weth") and r.get("hook_profile", {}).get("risk_tier") == "returns_delta_high"]
    rd = sorted(rd, key=lambda x: x["blockNumber"], reverse=True)[: max(0, limit - len(vanilla))]
    cases = (vanilla + rd)[:limit]
    print(f"#### ROUNDTRIP SELFTEST (n={len(cases)}) ####", flush=True)
    ok = 0
    results = []
    for i, row in enumerate(cases):
        t0 = time.time()
        lab = label_pool_strong(row, spend_eth=spend_eth)
        entry = (lab.get("scans") or [{}])[0]
        passed = lab["label"] in ("clean", "instant_hp", "hook_block", "delta_tax", "delayed_rug", "dead")
        if entry.get("buy_ok"):
            ok += 1
        results.append({
            "poolId": row["poolId"],
            "tier": row.get("hook_profile", {}).get("risk_tier"),
            "label": lab["label"],
            "buy_ok": entry.get("buy_ok"),
            "sell_ok": entry.get("sell_ok"),
            "elapsed_s": round(time.time() - t0, 1),
        })
        print(f"  [{i+1}] {lab['label']} buy={entry.get('buy_ok')} sell={entry.get('sell_ok')}", flush=True)
    json.dump({"cases": results}, open(os.path.join(HERE, "_roundtrip_selftest.json"), "w"), indent=2)
    if ok < 1:
        print("SELFTEST FAIL: no buy_ok on any case", flush=True)
        return 1
    print(f"SELFTEST PASS: buy_ok on {ok}/{len(cases)}", flush=True)
    return 0


def run_roundtrip_batch(*, tier: Optional[str] = None) -> dict:
    if not executor_healthy():
        return {"stopped": True, "reason": "executor unhealthy"}
    if not wait_for_live_clear(max_wait_s=300):
        return {"stopped": True, "reason": "open positions timeout"}

    sample = get_universe_pools(tier)
    tier_counts = Counter(r.get("hook_profile", {}).get("risk_tier") for r in sample)
    print(f"[roundtrip batch tier={tier or 'all'}] n={len(sample)} tier_n={dict(tier_counts)}", flush=True)

    done = _load_label_file(ROUNDTRIP_PATH)
    batch_t0 = time.time()
    new_this_run = 0
    stopped_reason = None

    for i, row in enumerate(sample):
        if time.time() - batch_t0 > BATCH_TIME_LIMIT_S:
            stopped_reason = "batch_time_limit_90min"
            break
        if not wait_for_live_clear(max_wait_s=60):
            stopped_reason = "open_positions"
            break
        pid = row["poolId"]
        if pid in done and done[pid].get("label_source") == "roundtrip_strong":
            continue
        lab = label_pool_strong(row, spend_eth=0.003)
        rec = {
            "poolId": pid,
            "hooks": row["hooks"],
            "hook_tier": row.get("hook_profile", {}).get("risk_tier"),
            "hook_template": (row.get("hook_template") or {}).get("template_name"),
            **lab,
        }
        done[pid] = rec
        new_this_run += 1
        pools = list(done.values())
        json.dump({"summary": {"n": len(pools), "new_this_run": new_this_run}, "pools": pools},
                  open(ROUNDTRIP_PATH, "w"), indent=2)
        if new_this_run % 3 == 0 or (i + 1) == len(sample):
            c = Counter(p.get("label") for p in pools)
            print(f"  [{i+1}/{len(sample)}] {pid[:16]} -> {lab['label']} | {dict(c)}", flush=True)

    pools = list(done.values())
    summary = {
        "n": len(pools),
        "expected": len(sample),
        "new_this_run": new_this_run,
        "labels": dict(Counter(p.get("label") for p in pools)),
        "tier": tier,
        "stopped_reason": stopped_reason,
        "elapsed_s": round(time.time() - batch_t0, 1),
    }
    json.dump({"summary": summary, "pools": pools}, open(ROUNDTRIP_PATH, "w"), indent=2)
    return summary


def pick_selftest_pools(n_per_hook: int = 2, index: Optional[dict] = None) -> list[dict]:
    """Pick WETH pools per RD hook from jsonl; prefer pools with cached swap txs."""
    indexed = set((index or {}).get("pools", {}).keys())
    by: defaultdict[str, list] = defaultdict(list)
    for line in open(POOLS_PATH):
        r = json.loads(line)
        if not r.get("has_weth"):
            continue
        if r["hooks"] in TOP_RD_HOOKS:
            by[r["hooks"]].append(r)

    def _pid_key(row: dict) -> str:
        pid = row["poolId"]
        return pid if pid.startswith("0x") else "0x" + pid

    picked = []
    for h in TOP_RD_HOOKS:
        pools = by.get(h, [])
        if indexed:
            hit = [p for p in pools if _pid_key(p) in indexed]
            pools = hit or pools
        pools = sorted(pools, key=lambda x: x["blockNumber"], reverse=True)
        picked.extend(pools[:n_per_hook])
    return picked


def _tx_replay_for_pool(pool_row: dict, index: dict) -> dict:
    pid = pool_row["poolId"]
    if not pid.startswith("0x"):
        pid = "0x" + pid
    meta = index.get("pools", {}).get(pid)
    if not meta:
        return {"ok": False, "error": "no_cached_swap_tx", "method": "tx_replay"}
    swap_block = int(meta["block"])
    for fork_block in (max(0, swap_block - 1), swap_block):
        res = simulate_tx_call(meta["tx_hash"], fork_block)
        if res.get("ok"):
            return {
                **res,
                "method": "tx_replay",
                "cached_block": swap_block,
                "fork_block": fork_block,
            }
    return {
        **res,
        "method": "tx_replay",
        "cached_block": swap_block,
        "fork_block": max(0, swap_block - 1),
    }


def selftest(n_per_hook: int = 2, spend_eth: float = 0.003) -> int:
    if not executor_healthy():
        print("STOP: executor unhealthy", flush=True)
        return 2
    if not wait_for_live_clear(max_wait_s=120):
        print("STOP: open positions — live takes priority", flush=True)
        return 2

    index = load_swap_index()
    if not index.get("pools"):
        print("[selftest] building swap index (1x eth_getLogs, 500 blocks)…", flush=True)
        index = build_swap_index(lookback_blocks=500)

    cases = pick_selftest_pools(n_per_hook, index)

    print(f"#### V4 FORK-REPLAY SELF-TEST (n={len(cases)}, swap_index={len(index.get('pools', {}))}) ####", flush=True)
    print(f"archive={ARCHIVE_RPC[:52]}…", flush=True)
    results = []
    ok_tx = 0
    for i, row in enumerate(cases):
        print(f"\n[{i+1}/{len(cases)}] hook={row['hooks'][:12]}… pool={row['poolId'][:16]}…", flush=True)
        t0 = time.time()
        tx_rep = _tx_replay_for_pool(row, index)
        rt_rep = replay_at_block(row["blockNumber"], row, int(spend_eth * 1e18))
        rec = {
            "poolId": row["poolId"],
            "hooks": row["hooks"],
            "entry_block": row["blockNumber"],
            "hook_tier": row.get("hook_profile", {}).get("risk_tier"),
            "tx_replay": tx_rep,
            "roundtrip": rt_rep,
            "elapsed_s": round(time.time() - t0, 1),
        }
        if tx_rep.get("ok"):
            ok_tx += 1
            rec["label_entry"] = "onchain_swap_ok"
        elif rt_rep.get("buy_ok") and rt_rep.get("sell_ok"):
            rec["label_entry"] = "clean"
        elif rt_rep.get("buy_ok"):
            rec["label_entry"] = "instant_hp"
        else:
            rec["label_entry"] = "dead"
        results.append(rec)
        print(f"  tx_replay={tx_rep.get('ok')} roundtrip buy={rt_rep.get('buy_ok')} sell={rt_rep.get('sell_ok')}", flush=True)
        if tx_rep.get("error"):
            print(f"  tx_err={tx_rep.get('error')[:80]}", flush=True)
        if rt_rep.get("error"):
            print(f"  rt_err={rt_rep.get('error')[:80]}", flush=True)
        json.dump({"cases": results}, open(SELFTEST_PATH, "w"), indent=2)

    passed = ok_tx >= max(1, len(cases) // 2)
    print(f"\nSELF-TEST {'PASS' if passed else 'FAIL'}: "
          f"tx_replay_ok={ok_tx}/{len(cases)} (roundtrip={sum(1 for r in results if r.get('roundtrip',{}).get('sell_ok'))})", flush=True)
    return 0 if passed else 1


def stratified_sample(rows: list[dict], n: int = MAX_POOLS_PER_ROUND) -> list[dict]:
    buckets: defaultdict[str, list] = defaultdict(list)
    for r in rows:
        buckets[r.get("hook_profile", {}).get("risk_tier", "unknown")].append(r)
    order = ["returns_delta_high", "standard", "vanilla", "unknown"]
    picked: list[dict] = []
    per = max(1, n // max(len(buckets), 1))
    for b in order:
        picked.extend(buckets.get(b, [])[:per])
    return picked[:n]


def _pid_key(row: dict) -> str:
    pid = row["poolId"]
    return pid if pid.startswith("0x") else "0x" + pid


def _tier_bucket(row: dict) -> Optional[str]:
    tier = row.get("hook_profile", {}).get("risk_tier", "unknown")
    if tier == "vanilla":
        return "vanilla"
    if tier == "returns_delta_high":
        return "returns_delta_high"
    if tier in ("standard", "unknown"):
        return "other_hooked"
    return "other_hooked"


def stratified_sample_quotas(
    rows: list[dict], index: dict, quotas: dict[str, int], *, max_total: int = 120
) -> tuple[list[dict], dict]:
    """Quota stratified sample from indexed WETH pools."""
    keys = set(index.get("pools", {}).keys())
    indexed = [r for r in rows if r.get("has_weth") and _pid_key(r) in keys]
    buckets: defaultdict[str, list] = defaultdict(list)
    for r in indexed:
        buckets[_tier_bucket(r)].append(r)
    for b in buckets:
        buckets[b].sort(key=lambda x: x["blockNumber"], reverse=True)

    picked: list[dict] = []
    seen = set()
    tier_counts: dict[str, int] = {}
    for tier, q in quotas.items():
        n = 0
        for r in buckets.get(tier, []):
            pid = r["poolId"]
            if pid in seen:
                continue
            picked.append(r)
            seen.add(pid)
            n += 1
            if n >= q:
                break
        tier_counts[tier] = n

    for tier in ("returns_delta_high", "other_hooked", "vanilla"):
        for r in buckets.get(tier, []):
            if len(picked) >= max_total:
                break
            if r["poolId"] not in seen:
                picked.append(r)
                seen.add(r["poolId"])
                tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return picked[:max_total], tier_counts


def load_done_labels(*paths) -> dict:
    done = {}
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            for r in json.load(open(path)).get("pools", []):
                done[r["poolId"]] = r
        except Exception:
            pass
    return done


def merge_label_files() -> dict:
    """Merge v1 + v2 labels by poolId (v2 wins)."""
    merged = {}
    for path in (OUT_PATH, OUT_V2_PATH):
        if not os.path.exists(path):
            continue
        for r in json.load(open(path)).get("pools", []):
            merged[r["poolId"]] = r
    out = {"n": len(merged), "pools": list(merged.values())}
    merge_path = os.path.join(HERE, "_fork_replay_labels_merged.json")
    json.dump(out, open(merge_path, "w"), indent=2)
    return out



def run_batch(
    *,
    limit: int = MAX_POOLS_PER_ROUND,
    tx_only: bool = False,
    out_path: Optional[str] = None,
    min_per_tier: int = 0,
    resume_unlabeled: bool = False,
) -> dict:
    if not executor_healthy():
        return {"stopped": True, "reason": "executor unhealthy"}
    if not wait_for_live_clear(max_wait_s=300):
        return {"stopped": True, "reason": "open positions timeout"}

    out_path = out_path or (OUT_V2_PATH if tx_only else OUT_PATH)
    batch_t0 = time.time()
    rows = load_pools()
    index = load_swap_index()
    if tx_only and not index.get("pools"):
        index = build_swap_index(lookback_blocks=5000)

    if tx_only and resume_unlabeled:
        merged = load_done_labels(OUT_V2_PATH, OUT_PATH, os.path.join(HERE, "_fork_replay_labels_merged.json"))
        merged_ids = {_pid_key({"poolId": k}) for k in merged}
        keys = set(index.get("pools", {}).keys())
        sample = []
        for r in rows:
            if not r.get("has_weth"):
                continue
            pid = _pid_key(r)
            if pid in keys and pid not in merged_ids:
                sample.append(r)
        sample = sample[:limit]
        tier_counts = Counter(_tier_bucket(r) for r in sample)
        print(f"[batch resume] unlabeled WETH in index: {len(sample)}", flush=True)
    elif tx_only and min_per_tier > 0:
        quotas = {k: min_per_tier for k in TIER_QUOTAS}
        sample, tier_counts = stratified_sample_quotas(rows, index, quotas, max_total=limit)
        print(
            f"[batch tx-only] sample={len(sample)} index={len(index.get('pools', {}))} "
            f"tier_n={tier_counts}",
            flush=True,
        )
    elif tx_only:
        keys = set(index.get("pools", {}).keys())
        hit = [r for r in rows if r.get("has_weth") and _pid_key(r) in keys]
        sample = stratified_sample(hit, limit)
        tier_counts = Counter(_tier_bucket(r) for r in sample)
        print(f"[batch tx-only] sample={len(sample)} indexed_pools={len(keys)}", flush=True)
    else:
        sample = stratified_sample([r for r in rows if r.get("has_weth")], limit)
        tier_counts = {}

    done = load_done_labels(out_path, OUT_PATH, os.path.join(HERE, "_fork_replay_labels_merged.json"))
    new_this_run = 0
    stopped_reason = None
    for i, row in enumerate(sample):
        if time.time() - batch_t0 > BATCH_TIME_LIMIT_S:
            stopped_reason = "batch_time_limit_90min"
            break
        if not wait_for_live_clear(max_wait_s=60):
            stopped_reason = "open_positions"
            break
        pid = row["poolId"]
        if pid in done:
            continue
        lab = label_pool_tx(row, index) if tx_only else label_pool(row)
        rec = {
            "poolId": pid,
            "hooks": row["hooks"],
            "hook_tier": row.get("hook_profile", {}).get("risk_tier"),
            "label_source": "tx_replay_weak" if tx_only else "roundtrip",
            **lab,
        }
        done[pid] = rec
        new_this_run += 1
        all_pools = list(done.values())
        payload = {
            "summary": {"n": len(all_pools), "new_this_run": new_this_run, "tier_counts": dict(tier_counts)},
            "pools": all_pools,
        }
        json.dump(payload, open(out_path, "w"), indent=2)
        json.dump({"at": i + 1, "total": len(sample), "poolId": pid, "label": lab["label"]},
                  open(BATCH_CHECKPOINT, "w"))
        if new_this_run % 5 == 0 or (i + 1) == len(sample):
            c = Counter(r["label"] for r in all_pools)
            print(f"  [{i+1}/{len(sample)}] {pid[:16]} -> {lab['label']} | total={len(all_pools)} {dict(c)}", flush=True)

    all_pools = list(done.values())
    counts = Counter(r["label"] for r in all_pools)
    summary = {
        "n": len(all_pools),
        "new_this_run": new_this_run,
        "labels": dict(counts),
        "tier_counts": dict(tier_counts) if tier_counts else {},
        "stopped_reason": stopped_reason,
        "elapsed_s": round(time.time() - batch_t0, 1),
        "out_path": out_path,
    }
    json.dump({"summary": summary, "pools": all_pools}, open(out_path, "w"), indent=2)
    if tx_only:
        merge_label_files()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--tx-only", action="store_true", help="label via cached tx replay (no roundtrip anvil)")
    ap.add_argument("--limit", type=int, default=MAX_POOLS_PER_ROUND)
    ap.add_argument("--min-per-tier", type=int, default=0, help="quota per tier for tx-only batch")
    ap.add_argument("--resume-unlabeled", action="store_true", help="label WETH pools in index not yet merged")
    ap.add_argument("--per-hook", type=int, default=2)
    ap.add_argument("--build-index", action="store_true", help="incremental swap index build")
    ap.add_argument("--lookback", type=int, default=5000, help="swap index lookback blocks")
    ap.add_argument("--dry-run-index", action="store_true", help="estimate index RPC only")
    ap.add_argument("--merge-labels", action="store_true", help="merge weak+strong or v1+v2 label files")
    ap.add_argument("--weak", default=WEAK_MERGED_PATH, help="weak labels path for --merge-labels")
    ap.add_argument("--strong", default=ROUNDTRIP_PATH, help="strong labels path for --merge-labels")
    ap.add_argument("--out", default=MERGED_STRONG_PATH, help="merged output path")
    ap.add_argument("--selftest-roundtrip", action="store_true", help="roundtrip smoke (vanilla + RD)")
    ap.add_argument("--roundtrip", action="store_true", help="with --batch: full buy→sell strong labels")
    ap.add_argument("--tier", choices=("vanilla", "returns_delta_high", "other"), default=None)
    args = ap.parse_args()
    if args.merge_labels:
        if os.path.exists(args.weak) and os.path.exists(args.strong):
            out = merge_strong_labels(args.weak, args.strong, args.out)
        else:
            out = merge_label_files()
        print(json.dumps({"n": out.get("n"), "out": args.out if os.path.exists(args.weak) else MERGED_STRONG_PATH}, indent=2))
        return 0
    if args.dry_run_index:
        est = build_swap_index(lookback_blocks=args.lookback, dry_run=True)
        print(json.dumps(est, indent=2))
        return 0
    if args.build_index:
        idx = build_swap_index(lookback_blocks=args.lookback)
        meta = idx.get("meta", idx)
        print(
            f"swap index: pools={len(idx.get('pools', {}))} "
            f"earliest={meta.get('earliest_block')} rpc_added={meta.get('rpc_added')} "
            f"total_rpc={meta.get('rpc_total')}",
            flush=True,
        )
        return 0
    if args.selftest_roundtrip:
        return selftest_roundtrip(limit=args.limit if args.limit != MAX_POOLS_PER_ROUND else 2)
    if args.selftest:
        return selftest(n_per_hook=args.per_hook)
    if args.batch:
        if args.roundtrip:
            print(json.dumps(run_roundtrip_batch(tier=args.tier), indent=2))
        else:
            print(json.dumps(run_batch(
                limit=args.limit, tx_only=args.tx_only, min_per_tier=args.min_per_tier,
                resume_unlabeled=args.resume_unlabeled,
            ), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
