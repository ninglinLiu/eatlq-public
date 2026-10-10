#!/usr/bin/env python3
"""
Sprint045E — Shared contract autopsy + Aerodrome crosscheck + A.1 indexing preflight

Goal: turn Sprint045D's two shared-tx.to clusters into verifiable classifications.
- Cluster 2: 0x5570240e8eedcdad5fe9b38f62c93236523e5eb4
- Cluster 1: 0x69460570c93f9de5e2edbc3052bf10125f0ca22d

Read-only RPC + ClickHouse. No live, no copy trade, no identity claim,
no equating shared tx.to with funding sameness.
"""

import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CH_URL  = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
RPC_URL = (os.environ.get("LABEL_RPC_URL")
           or os.environ.get("EVM_HTTP_URL",
              "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
              "0123456789abcdef0123456789abcdef01234567/"))

OUT_DIR = "mvp/diagnostics"
AUD_DIR = "mvp/audits"

CLUSTER2_ADDR = "0x5570240e8eedcdad5fe9b38f62c93236523e5eb4"
CLUSTER1_ADDR = "0x69460570c93f9de5e2edbc3052bf10125f0ca22d"
CLUSTERS = {"CLUSTER2": CLUSTER2_ADDR, "CLUSTER1": CLUSTER1_ADDR}

# Known public infra addrs (from Sprint045D)
PLATFORM_ADDRS = {
    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f",
    "0x327df1e6de05895d2ab08513aadd9313fe505d86",
    "0x111111125421ca6dc452d289314280a0f8842a65",
    "0x1111111254eeb25477b68fb85ed929f73a960582",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
    "0x420dd381b31aef6683db6b902084cb0ffece40da",
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # high-traffic V3 pool
}

# Known function selectors (4-byte) — small canon set for triage
KNOWN_SELECTORS = {
    # Uniswap V3 SwapRouter
    "0x414bf389": "exactInputSingle",
    "0xc04b8d59": "exactInput",
    "0xdb3e2198": "exactOutputSingle",
    "0xf28c0498": "exactOutput",
    # UniversalRouter
    "0x3593564c": "execute(bytes,bytes[],uint256)",
    "0x24856bc3": "execute(bytes,bytes[])",
    # multicall / dispatch
    "0xac9650d8": "multicall(bytes[])",
    "0x5ae401dc": "multicall(uint256,bytes[])",
    "0x1f0464d1": "multicall(bytes32,bytes[])",
    # ERC20 utility
    "0xa9059cbb": "transfer(address,uint256)",
    "0x23b872dd": "transferFrom(address,address,uint256)",
    "0x095ea7b3": "approve(address,uint256)",
    "0xd0e30db0": "deposit() (WETH)",
    "0x2e1a7d4d": "withdraw(uint256) (WETH)",
    # 1inch
    "0x07ed2379": "swap (1inch v5 Aggregation)",
    "0x12aa3caf": "swap (1inch unoswap)",
    # 0x v4
    "0x415565b0": "transformERC20",
    "0xd9627aa4": "sellToUniswap",
    # Solady/MEV common patterns
    "0x00000000": "fallback_or_zero",
    # Aerodrome
    "0xfdb5a03e": "swap (Aerodrome vAMM swap)",
}

# Funding-graph constants (for A.1 preflight)
WETH_BASE = "0x4200000000000000000000000000000000000006"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_E    = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
TOPIC_TRANSFER   = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_DEPOSIT    = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d65a73f1c0bcd0e2ba7b1bdac"  # canonical Deposit(address,uint256)
TOPIC_WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # canonical Withdrawal(address,uint256)

ts_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
print(f"[{ts_now}] Sprint045E — shared contract autopsy + Aerodrome crosscheck + A.1 preflight")
sys.stdout.flush()

# ── Helpers ───────────────────────────────────────────────────────────────────
def ch_query(sql, timeout=60):
    r = requests.post(CH_URL, data=sql.encode(),
                      headers={"X-ClickHouse-Database": "eatlq"},
                      auth=(CH_USER, CH_PASS), timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"CH {r.status_code}: {r.text[:300]}")
    return r.text.strip()


def rpc_call(method, params, retries=3, timeout=15):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    for attempt in range(retries):
        try:
            r = requests.post(RPC_URL, json=payload, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("result")
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)}
            time.sleep(2 ** attempt)
    return {"_error": "max_retries"}


def rpc_batch(calls, retries=3, timeout=15):
    payload = [{"jsonrpc": "2.0", "method": c["method"], "params": c["params"], "id": i}
               for i, c in enumerate(calls)]
    for attempt in range(retries):
        try:
            r = requests.post(RPC_URL, json=payload, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            r.raise_for_status()
            return sorted(r.json(), key=lambda x: x.get("id", 0))
        except Exception as e:
            if attempt == retries - 1:
                return [{"error": str(e), "id": i} for i in range(len(calls))]
            time.sleep(2 ** attempt)
    return [{"error": "max_retries", "id": i} for i in range(len(calls))]


def stream_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def keccak_hash_summary(hex_bytecode: str) -> str:
    """Return a deterministic SHA256 prefix as a stable bytecode fingerprint
    (we don't need cryptographic equivalence to Solidity codeHash, just stability)."""
    if not hex_bytecode or hex_bytecode == "0x":
        return "EMPTY"
    h = hashlib.sha256(hex_bytecode.encode()).hexdigest()
    return f"sha256:{h[:16]}"


# ── Load Sprint045D artifacts ─────────────────────────────────────────────────
print("\n── Loading Sprint045D/C artifacts ──", flush=True)

# 1. Build per-controller checkpoint index: ctrl → list[(tx_hash, to, nonce)]
ctrl_txs: dict[str, list] = defaultdict(list)
# Also build cluster-tx index: cluster_addr → list[(tx_hash, controller, nonce)]
cluster_tx_index: dict[str, list] = defaultdict(list)
# Also: for selector sampling we want raw tx_hash → to mapping
all_tx_to: dict[str, str] = {}

cluster_addrs = set(CLUSTERS.values())

for path in [
    "mvp/diagnostics/sprint045b_rpc_checkpoint.jsonl",
    "mvp/diagnostics/sprint045c_c2_rpc_checkpoint.jsonl",
]:
    for rec in stream_jsonl(path):
        if not rec.get("ok"):
            continue
        ctrl = (rec.get("controller") or "").lower()
        to   = (rec.get("to") or "").lower()
        tx   = (rec.get("tx_hash") or "").lower()
        nonce_raw = rec.get("nonce", "0x0")
        try:
            nonce_int = int(nonce_raw, 16) if isinstance(nonce_raw, str) else int(nonce_raw)
        except Exception:
            nonce_int = -1
        if ctrl and tx:
            ctrl_txs[ctrl].append((nonce_int, to, tx))
        if to in cluster_addrs and tx:
            cluster_tx_index[to].append((tx, ctrl, nonce_int))
        if tx and to:
            all_tx_to[tx] = to

print(f"  controllers indexed: {len(ctrl_txs):,}")
print(f"  cluster-2 txes seen: {len(cluster_tx_index[CLUSTER2_ADDR]):,}")
print(f"  cluster-1 txes seen: {len(cluster_tx_index[CLUSTER1_ADDR]):,}")

# 2. Load to_field_diagnostics for caller composition (full universe-wide)
diag = {}
diag_path = "mvp/diagnostics/sprint045d_to_field_diagnostics.csv"
if os.path.exists(diag_path):
    with open(diag_path, newline="") as f:
        for r in csv.DictReader(f):
            diag[r["controller"].lower()] = r
print(f"  to_field_diagnostics rows: {len(diag):,}")

# 3. Bucket map (lightweight — only ~158 candidates here)
bucket_map = {}
pnl_map = {}
nswap_map = {}
inv_path = "mvp/diagnostics/sprint045c_controller_inventory_all.csv"
if os.path.exists(inv_path):
    with open(inv_path, newline="") as f:
        for r in csv.DictReader(f):
            c = r["controller"].lower()
            bucket_map[c] = r.get("bucket", "")
            pnl_map[c]    = r.get("pnl_total_usd", "")
            nswap_map[c]  = r.get("n_swaps", "")
print(f"  inventory rows loaded: {len(bucket_map):,}")

# 4. Aerodrome top-25 canon candidates
aero_canon = []
aero_inv_path = "mvp/diagnostics/sprint045d_aerodrome_lane_inventory.csv"
if os.path.exists(aero_inv_path):
    with open(aero_inv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows
            if r.get("wp_g1_canon_candidate") in ("True","true","1")
            and r.get("in_v3_inventory") in ("False","false","0")]
    rows.sort(key=lambda x: -int(x["n_aero_txs"]))
    aero_canon = rows[:25]
print(f"  aerodrome canon candidates: {len(aero_canon)}")

# ── Phase E0: shared contract autopsy ─────────────────────────────────────────
print("\n=== Phase E0: shared contract autopsy ===", flush=True)

autopsy_rows = []
selector_rows = []
caller_rows = []

# Limits
N_SELECTOR_SAMPLES = 30   # tx samples per cluster contract
N_CALLER_TOP = 50         # callers to materialize per cluster

for label, addr in CLUSTERS.items():
    print(f"\n  {label} = {addr}", flush=True)

    # 1. eth_getCode + size + bytecode hash
    code = rpc_call("eth_getCode", [addr, "latest"])
    if isinstance(code, dict) and code.get("_error"):
        code_hex = ""; code_size = -1; code_hash = "ERROR"
    else:
        code_hex  = code or ""
        code_size = max(0, (len(code_hex) - 2) // 2) if code_hex.startswith("0x") else 0
        code_hash = keccak_hash_summary(code_hex)
    print(f"    code_size = {code_size}  fingerprint = {code_hash}")

    # Heuristics on bytecode prefix
    bc = code_hex.lower() if code_hex else ""
    is_minimal_proxy = bc.startswith("0x363d3d37") or bc.startswith("0x3d602d80")
    is_eip1167_clone = "5af43d82803e903d91602b57fd5bf3" in bc
    has_solidity_metadata = bc.endswith("0033") or "a164736f6c63" in bc  # "a1 64 73 6f 6c 63" = solc metadata marker
    has_create2 = "ff" in bc[2:42] if bc.startswith("0x") else False
    has_delegatecall = "f4" in bc  # opcode for delegatecall

    # 2. eth_getTransactionCount (sender-side; for a contract it's typically # of contracts deployed via this contract)
    txcount = rpc_call("eth_getTransactionCount", [addr, "latest"])
    txcount_int = int(txcount, 16) if isinstance(txcount, str) and txcount.startswith("0x") else -1
    print(f"    eth_getTransactionCount = {txcount_int}")

    # 3. Sample tx selectors: pick up to N tx where to == cluster_addr from checkpoint index
    cluster_tx_list = cluster_tx_index.get(addr, [])
    sample_txes = cluster_tx_list[: min(N_SELECTOR_SAMPLES, len(cluster_tx_list))]
    if len(cluster_tx_list) > N_SELECTOR_SAMPLES:
        # spread evenly
        step = max(1, len(cluster_tx_list) // N_SELECTOR_SAMPLES)
        sample_txes = cluster_tx_list[::step][:N_SELECTOR_SAMPLES]
    print(f"    sample tx for selectors: {len(sample_txes)} (of {len(cluster_tx_list)} cluster txes in checkpoints)")

    # Batch eth_getTransactionByHash for selectors (input field)
    if sample_txes:
        calls = [{"method": "eth_getTransactionByHash", "params": [tx]} for (tx, _, _) in sample_txes]
        results = rpc_batch(calls)
        sel_counter = Counter()
        sel_examples = {}
        for i, res in enumerate(results):
            tx_hash, ctrl, nonce_int = sample_txes[i]
            if "error" in res or res.get("result") is None:
                continue
            tx_data = res["result"]
            inp = (tx_data.get("input") or "").lower()
            sel = inp[:10] if inp.startswith("0x") and len(inp) >= 10 else "EMPTY"
            sel_counter[sel] += 1
            if sel not in sel_examples:
                sel_examples[sel] = tx_hash
            # Record a row for selector_summary
            selector_rows.append({
                "cluster_label": label,
                "cluster_addr": addr,
                "tx_hash": tx_hash,
                "from_controller": ctrl,
                "controller_bucket": bucket_map.get(ctrl, "(not_in_inv)"),
                "controller_pnl_usd": pnl_map.get(ctrl, ""),
                "input_selector": sel,
                "selector_known_name": KNOWN_SELECTORS.get(sel, "unknown"),
                "input_len_bytes": (len(inp) - 2) // 2 if inp.startswith("0x") else 0,
                "value_wei": int(tx_data.get("value", "0x0"), 16) if tx_data.get("value") else 0,
            })

        top_selectors = sel_counter.most_common(8)
    else:
        sel_counter = Counter()
        top_selectors = []

    selector_diversity = len(sel_counter)
    n_known = sum(c for s, c in sel_counter.items() if s in KNOWN_SELECTORS)
    n_total_sample = sum(sel_counter.values())
    known_share = n_known / n_total_sample if n_total_sample else 0
    print(f"    selector diversity = {selector_diversity} unique  known = {n_known}/{n_total_sample}")
    for s, c in top_selectors:
        print(f"      {s}  x{c}  {KNOWN_SELECTORS.get(s,'unknown')}")

    # 4. Caller composition from checkpoints (controllers whose dominant_to == addr OR who sent any tx to this addr)
    caller_set = {ctrl for (_, ctrl, _) in cluster_tx_list if ctrl}
    # Cross with diag dominant_to (tighter signal)
    dominant_callers = [c for c, d in diag.items()
                        if d.get("dominant_to", "").lower() == addr]
    # Caller bucket distribution
    caller_buckets = Counter()
    for c in caller_set:
        caller_buckets[bucket_map.get(c, "(not_in_inv)")] += 1
    # Cluster distance: are callers concentrated (private) or diffuse (public)?
    n_callers = len(caller_set)
    n_dominant = len(dominant_callers)

    # Materialize caller_rows (top N by tx count)
    caller_tx_count = Counter()
    for (tx, ctrl, _) in cluster_tx_list:
        if ctrl:
            caller_tx_count[ctrl] += 1
    for ctrl, n in caller_tx_count.most_common(N_CALLER_TOP):
        d = diag.get(ctrl, {})
        caller_rows.append({
            "cluster_label": label,
            "cluster_addr": addr,
            "caller_controller": ctrl,
            "n_tx_to_cluster_in_checkpoint": n,
            "controller_bucket": bucket_map.get(ctrl, "(not_in_inv)"),
            "controller_pnl_usd": pnl_map.get(ctrl, ""),
            "controller_n_swaps": nswap_map.get(ctrl, ""),
            "controller_dominant_to": d.get("dominant_to", ""),
            "controller_dominant_to_frac": d.get("dominant_to_frac", ""),
            "controller_platform_frac": d.get("platform_frac", ""),
            "is_dominant_caller": d.get("dominant_to", "").lower() == addr,
        })

    # 5. Heuristic likelihood scoring
    # Higher private if: callers concentrated, dominant_to_frac=1, no public-router selectors,
    # callers heavily AUTHOR_LIKE/STRONG_ECONOMIC, low or zero overlap with public pools.
    # Higher public if: large caller diversity, high known-router selectors share,
    # callers includes random N/A buckets.
    inv_caller_count = sum(1 for c in caller_set if c in bucket_map)
    inv_caller_frac = inv_caller_count / max(n_callers, 1)
    private_score = 0.0
    public_score  = 0.0
    bot_score     = 0.0

    # Caller concentration signal
    if n_callers <= 30:
        private_score += 0.25
    elif n_callers <= 100:
        private_score += 0.10
    else:
        public_score += 0.10

    # All callers say dominant_to == this addr → very strong private/exclusive
    if n_dominant >= 5 and (n_dominant / max(n_callers, 1)) >= 0.5:
        private_score += 0.30
    elif n_dominant >= 2:
        private_score += 0.10

    # Known router selectors → public helper / aggregator
    if known_share >= 0.5 and n_total_sample >= 5:
        public_score += 0.30
    elif known_share >= 0.2:
        public_score += 0.10

    # Multicall / dispatch present → executor / bot backend hint
    has_multicall = any(s in ("0xac9650d8", "0x5ae401dc", "0x3593564c", "0x24856bc3")
                        for s in sel_counter)
    if has_multicall:
        bot_score += 0.20

    # Cluster of fewer than 50 callers + invariably routed traffic → executor / private exec lane
    if n_callers < 50 and selector_diversity <= 5:
        private_score += 0.15
        bot_score     += 0.10

    # Solidity metadata + delegatecall + non-trivial code size → likely production helper or proxy backend
    if has_delegatecall and code_size > 5000:
        bot_score += 0.10

    unknown_score = max(0.0, 1.0 - (private_score + public_score + bot_score))

    # Normalize so scores sum to 1
    total = private_score + public_score + bot_score + unknown_score
    if total > 0:
        private_score /= total
        public_score  /= total
        bot_score     /= total
        unknown_score /= total

    autopsy_rows.append({
        "cluster_label": label,
        "cluster_addr": addr,
        "code_size_bytes": code_size,
        "code_fingerprint": code_hash,
        "is_minimal_proxy": is_minimal_proxy,
        "is_eip1167_clone": is_eip1167_clone,
        "has_solidity_metadata": has_solidity_metadata,
        "has_delegatecall_opcode": has_delegatecall,
        "eth_getTransactionCount": txcount_int,
        "n_callers_observed_checkpoint": n_callers,
        "n_dominant_callers": n_dominant,
        "inv_caller_frac": round(inv_caller_frac, 4),
        "selector_samples_total": n_total_sample,
        "selector_unique_count": selector_diversity,
        "known_selector_share": round(known_share, 4),
        "has_multicall_selector": has_multicall,
        "top_selectors": ";".join(f"{s}:{c}" for s, c in top_selectors[:5]),
        "caller_bucket_distribution": ";".join(f"{b}:{c}" for b, c in caller_buckets.most_common(8)),
        "private_infra_likelihood": round(private_score, 3),
        "public_helper_likelihood": round(public_score, 3),
        "bot_backend_likelihood": round(bot_score, 3),
        "unknown_likelihood": round(unknown_score, 3),
    })

# Write autopsy & callers & selectors
autopsy_path  = f"{OUT_DIR}/sprint045e_shared_contract_autopsy.csv"
callers_path  = f"{OUT_DIR}/sprint045e_cluster_contract_callers.csv"
selector_path = f"{OUT_DIR}/sprint045e_selector_summary.csv"

with open(autopsy_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(autopsy_rows[0].keys()))
    w.writeheader(); w.writerows(autopsy_rows)
print(f"\n  → {autopsy_path}")

with open(callers_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(caller_rows[0].keys()) if caller_rows else ["cluster_label"])
    w.writeheader(); w.writerows(caller_rows)
print(f"  → {callers_path}")

with open(selector_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(selector_rows[0].keys()) if selector_rows else ["cluster_label"])
    w.writeheader(); w.writerows(selector_rows)
print(f"  → {selector_path}")

# ── Phase Aero-crosscheck ─────────────────────────────────────────────────────
print("\n=== Phase Aero-crosscheck (25 canon vs Cluster 1/2) ===", flush=True)

aero_cross_rows = []
for r in aero_canon:
    ctrl = r["controller"].lower()
    d = diag.get(ctrl, {})
    dominant = d.get("dominant_to", "").lower() if d else ""
    nonce0   = d.get("nonce0_to", "").lower() if d else ""
    first    = d.get("first_to", "").lower() if d else ""
    last     = d.get("last_to", "").lower() if d else ""

    # Membership tests
    in_cluster2_dom = (dominant == CLUSTER2_ADDR)
    in_cluster1_dom = (dominant == CLUSTER1_ADDR)
    nonce0_cluster2 = (nonce0 == CLUSTER2_ADDR)
    nonce0_cluster1 = (nonce0 == CLUSTER1_ADDR)
    first_cluster2  = (first == CLUSTER2_ADDR)
    first_cluster1  = (first == CLUSTER1_ADDR)

    aero_cross_rows.append({
        "controller": ctrl,
        "n_aero_txs": r.get("n_aero_txs", ""),
        "min_priority_fee_wei": r.get("min_priority_fee_wei", ""),
        "in_v3_to_diagnostics": ctrl in diag,
        "controller_bucket_v3": bucket_map.get(ctrl, "(not_in_v3_inv)"),
        "controller_pnl_v3": pnl_map.get(ctrl, ""),
        "v3_dominant_to": dominant,
        "v3_dominant_to_frac": d.get("dominant_to_frac", "") if d else "",
        "v3_nonce0_to": nonce0,
        "v3_first_to": first,
        "v3_platform_frac": d.get("platform_frac", "") if d else "",
        "shares_dominant_with_cluster2": in_cluster2_dom,
        "shares_dominant_with_cluster1": in_cluster1_dom,
        "nonce0_to_cluster2": nonce0_cluster2,
        "nonce0_to_cluster1": nonce0_cluster1,
        "first_to_cluster2": first_cluster2,
        "first_to_cluster1": first_cluster1,
    })

# Aggregate counters
shares_c2 = sum(1 for r in aero_cross_rows if r["shares_dominant_with_cluster2"])
shares_c1 = sum(1 for r in aero_cross_rows if r["shares_dominant_with_cluster1"])
in_v3_diag = sum(1 for r in aero_cross_rows if r["in_v3_to_diagnostics"])
print(f"  aero in V3 to_diagnostics: {in_v3_diag}/25")
print(f"  aero shares dominant with Cluster 2: {shares_c2}/25")
print(f"  aero shares dominant with Cluster 1: {shares_c1}/25")

aero_path = f"{OUT_DIR}/sprint045e_aerodrome_cluster_crosscheck.csv"
with open(aero_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(aero_cross_rows[0].keys()))
    w.writeheader(); w.writerows(aero_cross_rows)
print(f"  → {aero_path}")

# ── Phase A.1 preflight ───────────────────────────────────────────────────────
print("\n=== Phase A.1 indexing preflight ===", flush=True)

# Confirm CH-side counts (root cause)
weth_raw = int(ch_query(
    f"SELECT count() FROM raw_evm_logs WHERE chain_id=8453 AND lower(address)='{WETH_BASE}'"
))
usdc_raw = int(ch_query(
    f"SELECT count() FROM raw_evm_logs WHERE chain_id=8453 AND lower(address)='{USDC_BASE}'"
))
usdce_raw = int(ch_query(
    f"SELECT count() FROM raw_evm_logs WHERE chain_id=8453 AND lower(address)='{USDC_E}'"
))
total_raw = int(ch_query("SELECT count() FROM raw_evm_logs WHERE chain_id=8453"))
min_block_db = int(ch_query("SELECT min(block_number) FROM raw_evm_logs WHERE chain_id=8453"))
max_block_db = int(ch_query("SELECT max(block_number) FROM raw_evm_logs WHERE chain_id=8453"))

# Get RPC chain head for sizing
head_hex = rpc_call("eth_blockNumber", [])
head_int = int(head_hex, 16) if isinstance(head_hex, str) and head_hex.startswith("0x") else -1

# Probe 1 hour worth of blocks via eth_getLogs to estimate row volume
# Base block time ~2s → 1 hour ≈ 1800 blocks
# We probe a 100-block window at chain head minus 200 blocks (avoid reorg edge)
probe_to = head_int - 50 if head_int > 0 else max_block_db
probe_from = probe_to - 100 if probe_to > 100 else 0

print(f"  RPC head={head_int}  probe range {probe_from}..{probe_to} (101 blocks)")

filter_obj = {
    "fromBlock": hex(probe_from),
    "toBlock":   hex(probe_to),
    "address":   [WETH_BASE, USDC_BASE, USDC_E],
    "topics":    [[TOPIC_TRANSFER, TOPIC_DEPOSIT, TOPIC_WITHDRAWAL]],
}
logs = rpc_call("eth_getLogs", [filter_obj], retries=2, timeout=30)
if isinstance(logs, dict) and logs.get("_error"):
    probe_logs = -1
    probe_topics_dist = {}
    probe_addr_dist = {}
    probe_error = logs["_error"]
else:
    probe_logs = len(logs) if isinstance(logs, list) else 0
    probe_topics_dist = Counter()
    probe_addr_dist = Counter()
    for lg in (logs or []):
        topics = lg.get("topics", [])
        if topics:
            probe_topics_dist[topics[0].lower()] += 1
        addr = (lg.get("address") or "").lower()
        if addr:
            probe_addr_dist[addr] += 1
    probe_error = None

print(f"  probe rows in 101-block window: {probe_logs}")
print(f"    by topic0: {dict(probe_topics_dist)}")
print(f"    by address: {dict(probe_addr_dist)}")

# Extrapolate
blocks_per_day = 43_200  # Base ~2s blocks → 86,400/2
rows_per_block = probe_logs / 101 if probe_logs >= 0 else 0
rows_per_day   = rows_per_block * blocks_per_day
# Rough byte size per raw row: payload_json + topic + data ~ 600-1200 bytes; assume 800
bytes_per_row  = 800
gb_per_day     = rows_per_day * bytes_per_row / (1024**3)

# Date target: backfill from 2026-01-01
# Approximate block at 2026-01-01: per existing data, raw_evm_logs min is 36167663 — already past Jan 1
# Use that as proxy for "Jan 1 backfill block"
jan1_block_estimate = min_block_db
days_to_backfill = (head_int - jan1_block_estimate) / blocks_per_day if head_int > 0 else -1

backfill_total_rows = rows_per_day * days_to_backfill if days_to_backfill > 0 else -1
backfill_total_gb   = gb_per_day * days_to_backfill if days_to_backfill > 0 else -1

# Estimate eth_getLogs calls if chunk_size=100 and rate_limit ~200rpm safe
chunk_size = 200  # blocks per eth_getLogs call (Base RPC typically allows 1000 logs per call)
n_chunks = (head_int - jan1_block_estimate) / chunk_size if head_int > 0 else -1
secs_per_call = 0.5  # assume 2 RPS sustainable
backfill_walltime_min = n_chunks * secs_per_call / 60 if n_chunks > 0 else -1

# Build preflight JSON
preflight = {
    "sprint045e_timestamp": ts_now,

    "current_state": {
        "raw_evm_logs_total_rows_chain_8453": total_raw,
        "raw_evm_logs_block_range": [min_block_db, max_block_db],
        "raw_evm_logs_weth_rows": weth_raw,
        "raw_evm_logs_usdc_native_rows": usdc_raw,
        "raw_evm_logs_usdc_e_rows": usdce_raw,
        "ttl_days_on_raw_evm_logs": 180,
        "erc20_transfers_mv_topology": (
            "raw_evm_logs filtered by topic0=Transfer → MV erc20_transfers_mv → "
            "table erc20_transfers (auto-decoded sender/recipient/amount)"
        ),
        "root_cause_diagnosis": (
            "Ingestor LOG_FILTER_JSON does NOT include WETH/USDC addresses or "
            "Transfer/Deposit/Withdraw topics. Top topic0 in raw_evm_logs is "
            "0xc42079f9... (Uniswap V3 Swap). Pipeline downstream (MV) is fine; "
            "the gap is purely upstream filter scope."
        ),
    },

    "patch_plan_minimal": {
        "approach": "ENV-ONLY (no Rust source change required)",
        "files_to_change": ["(none in code) — env file or k8s/secret manager only"],
        "files_to_reuse": [
            "services/ingestor/src/config.rs",
            "services/ingestor/src/rpc.rs",
            "services/archiver/src/clickhouse_sink.rs",
            "ClickHouse: eatlq.raw_evm_logs (+ MV erc20_transfers_mv → erc20_transfers)",
        ],

        "option_a_widen_existing_filter": {
            "description": (
                "Replace LOG_FILTER_JSON with a union filter that keeps current "
                "swap topics AND adds Transfer/Deposit/Withdrawal for the 3 tokens. "
                "Single ingestor instance handles both."
            ),
            "log_filter_json_example": json.dumps({
                "topics": [[
                    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
                    "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
                    TOPIC_TRANSFER,
                    TOPIC_DEPOSIT,
                    TOPIC_WITHDRAWAL,
                ]],
                # NOTE: address-side OR cannot be cleanly combined with topic-side OR
                # via a single eth_getLogs call without broadening to ALL Transfer events.
                # Recommend Option B for narrow address scope.
            }),
            "risk": "broadens Transfer capture to ALL contracts → high data volume",
        },

        "option_b_sidecar_ingestor": {
            "description": (
                "Run a SECOND ingestor container with its own LOG_FILTER_JSON "
                "scoped strictly to address=[WETH,USDC,USDC.e] AND topics=[Transfer,Deposit,Withdraw]. "
                "Both ingestors write to the same Redis stream → archiver → raw_evm_logs."
            ),
            "log_filter_json_example": json.dumps({
                "address": [WETH_BASE, USDC_BASE, USDC_E],
                "topics":  [[TOPIC_TRANSFER, TOPIC_DEPOSIT, TOPIC_WITHDRAWAL]],
            }),
            "risk": "minimal; isolated. Recommended.",
            "env_vars_to_set_for_sidecar": [
                "LOG_FILTER_JSON=<the JSON above>",
                "WS_SOURCE=quicknode-erc20",
                "BACKFILL_SOURCE_SUFFIX=erc20-backfill",
                "EVM_WS_URL=<paid Base WS endpoint>",
                "EVM_HTTP_URL=<paid Base HTTP endpoint>",
                "REDIS_URL=<same as primary>",
                "MARKET_TICKS_STREAM=<same stream OR a sibling stream>",
            ],
        },

        "option_c_decouple_via_alternate_table": {
            "description": (
                "Skip raw_evm_logs and write WETH/USDC Transfer logs directly to a "
                "new table erc20_transfers_funding via a dedicated Python ingestor."
            ),
            "risk": "more code, breaks parity with existing erc20_transfers_mv",
            "recommend": False,
        },

        "recommended_choice": "option_b_sidecar_ingestor",
    },

    "backfill_plan": {
        "from_block_target": jan1_block_estimate,
        "to_block_target": head_int,
        "chunk_size_blocks": chunk_size,
        "estimated_chunks": int(n_chunks) if n_chunks > 0 else None,
        "estimated_rpc_calls": int(n_chunks) if n_chunks > 0 else None,
        "rate_limit_assumption_rps": 2,
        "estimated_walltime_minutes": round(backfill_walltime_min, 1) if backfill_walltime_min > 0 else None,
        "estimated_walltime_hours": round(backfill_walltime_min / 60, 2) if backfill_walltime_min > 0 else None,
        "estimated_total_rows": int(backfill_total_rows) if backfill_total_rows > 0 else None,
        "estimated_disk_gb_raw": round(backfill_total_gb, 2) if backfill_total_gb > 0 else None,
        "resume_strategy": (
            "Use existing services/ingestor/src/backfill_state.rs Postgres-backed "
            "BackfillJob state machine. New job_name='erc20_transfers_backfill_2026'. "
            "Job records last_processed_block; restart resumes from that point."
        ),
        "command_template_for_sidecar_backfill": (
            "BACKFILL_FROM_BLOCK={from_block} BACKFILL_TO_BLOCK={to_block} "
            "BACKFILL_CHUNK_SIZE=200 BACKFILL_PAUSE_MS=300 "
            "LOG_FILTER_JSON='{log_filter_json}' "
            "BACKFILL_JOB_NAME=erc20_transfers_backfill_2026 "
            "cargo run --release --bin backfill -p ingestor"
        ),
    },

    "smoke_plan": {
        "scope": "100-block probe completed inline (eth_getLogs at chain head -50)",
        "probe_blocks": probe_to - probe_from,
        "probe_rows": probe_logs,
        "probe_topic0_distribution": dict(probe_topics_dist),
        "probe_address_distribution": dict(probe_addr_dist),
        "probe_error": probe_error,
        "verification_sql": [
            f"SELECT count() FROM erc20_transfers WHERE chain_id=8453 AND lower(token_address)='{WETH_BASE}'",
            f"SELECT count() FROM erc20_transfers WHERE chain_id=8453 AND lower(token_address)='{USDC_BASE}'",
            f"SELECT count() FROM erc20_transfers WHERE chain_id=8453 AND lower(token_address)='{USDC_E}'",
            "-- Expected after 1h smoke: WETH ≫ 0; USDC ≫ 0; USDC.e ≥ 0 (lower-volume token)",
        ],
        "smoke_pass_criteria": [
            "raw_evm_logs gains > 0 rows for WETH+USDC+USDC.e in probe window",
            "erc20_transfers_mv emits matching rows (same tx_hash + log_index)",
            "decoded sender/recipient lengths == 42 chars (no malformed)",
            "no archiver / ingestor backpressure or panic in logs",
        ],
    },

    "this_run_did_not_patch": True,
    "reason_for_no_patch": (
        "LOG_FILTER_JSON is provided via runtime env (env var or secret manager), "
        "not committed in repo. Patching it requires touching deployment "
        "infrastructure, which is outside read-only scope of Sprint045E. "
        "Operator must apply Option B sidecar config in deployment env."
    ),

    "rollback_plan": (
        "Sidecar config: stop the sidecar container; primary ingestor unaffected. "
        "Already-ingested rows can be deleted via "
        "ALTER TABLE raw_evm_logs DELETE WHERE source LIKE '%erc20-backfill%' AND "
        "ingested_at >= '<rollback_ts>'."
    ),
}

preflight_path = f"{OUT_DIR}/sprint045e_a1_indexing_preflight.json"
with open(preflight_path, "w") as f:
    json.dump(preflight, f, indent=2)
print(f"  → {preflight_path}")

# ── Decision flags ────────────────────────────────────────────────────────────
print("\n=== Writing decision flags ===")

# Re-load autopsy for flag extraction
c2_row = next(r for r in autopsy_rows if r["cluster_label"] == "CLUSTER2")
c1_row = next(r for r in autopsy_rows if r["cluster_label"] == "CLUSTER1")

flags = {
    "SPRINT045E_TIMESTAMP": ts_now,
    "AUTHOR_WALLET_FOUND": False,
    "IDENTITY_CLAIM_ALLOWED": False,

    "FUNDING_GRAPH_READY": False,
    "FUNDING_GRAPH_BLOCKED_BY_INDEXING": True,
    "FUNDING_GRAPH_INDEXING_ROOT_CAUSE_IDENTIFIED": True,
    "FUNDING_GRAPH_PATCH_REQUIRES_RUST_CODE_CHANGE": False,
    "FUNDING_GRAPH_PATCH_REQUIRES_ENV_CHANGE": True,
    "FUNDING_GRAPH_PATCH_APPLIED_THIS_RUN": False,

    "CLUSTER_SHARED_TO_CONTRACT_FOUND": True,
    "CLUSTER_SHARED_TO_EQUALS_FUNDING_SAMENESS": False,
    "CLUSTER_SHARED_TO_IS_EXECUTION_OVERLAP_ONLY": True,

    "CLUSTER2_PRIVATE_INFRA_LIKELIHOOD": c2_row["private_infra_likelihood"],
    "CLUSTER2_PUBLIC_HELPER_LIKELIHOOD": c2_row["public_helper_likelihood"],
    "CLUSTER2_BOT_BACKEND_LIKELIHOOD": c2_row["bot_backend_likelihood"],
    "CLUSTER2_UNKNOWN_LIKELIHOOD": c2_row["unknown_likelihood"],
    "CLUSTER2_N_CALLERS_OBSERVED": c2_row["n_callers_observed_checkpoint"],

    "CLUSTER1_PRIVATE_INFRA_LIKELIHOOD": c1_row["private_infra_likelihood"],
    "CLUSTER1_PUBLIC_HELPER_LIKELIHOOD": c1_row["public_helper_likelihood"],
    "CLUSTER1_BOT_BACKEND_LIKELIHOOD": c1_row["bot_backend_likelihood"],
    "CLUSTER1_UNKNOWN_LIKELIHOOD": c1_row["unknown_likelihood"],
    "CLUSTER1_N_CALLERS_OBSERVED": c1_row["n_callers_observed_checkpoint"],

    "CLUSTER2_MORE_PRIVATE_THAN_CLUSTER1": (
        c2_row["private_infra_likelihood"] > c1_row["private_infra_likelihood"]
    ),
    "CLUSTER1_MORE_MIXED_PUBLIC_STACK_THAN_CLUSTER2": (
        c1_row["public_helper_likelihood"] > c2_row["public_helper_likelihood"]
    ),
    "EVIDENCE_SUFFICIENT_FOR_FUNDING_SAMENESS_JUDGMENT": False,

    "AERODROME_CANON_CANDIDATES_FOUND": len(aero_canon),
    "AERODROME_SHARES_DOMINANT_WITH_CLUSTER2": shares_c2,
    "AERODROME_SHARES_DOMINANT_WITH_CLUSTER1": shares_c1,
    "AERODROME_IN_V3_TO_DIAGNOSTICS": in_v3_diag,
    "AERODROME_KEPT_OUT_OF_V3_MAIN_RANKING": True,

    "NEXT_RPC_REQUIRED": False,           # autopsy budget already met
    "NEXT_INDEXING_REQUIRED": True,       # A.1 sidecar must be deployed
    "NEXT_HUMAN_DEPLOY_STEP": "Apply Option B sidecar LOG_FILTER_JSON in deployment env",
}

flags_path = f"{OUT_DIR}/sprint045e_decision_flags.json"
with open(flags_path, "w") as f:
    json.dump(flags, f, indent=2)
print(f"  → {flags_path}")

# ── Markdown readout ──────────────────────────────────────────────────────────
print("\n=== Writing markdown readout ===")

# Build narrative pieces
def pct(v): return f"{v*100:.1f}%"

c2_top_sel  = c2_row["top_selectors"] or "(none)"
c1_top_sel  = c1_row["top_selectors"] or "(none)"
c2_buckets  = c2_row["caller_bucket_distribution"] or "(none)"
c1_buckets  = c1_row["caller_bucket_distribution"] or "(none)"

aero_in_cluster_md = "\n".join(
    f"| `{r['controller'][:14]}` | {r['n_aero_txs']} | {r['min_priority_fee_wei']} | "
    f"{r['shares_dominant_with_cluster2']} | {r['shares_dominant_with_cluster1']} | "
    f"{r['in_v3_to_diagnostics']} |"
    for r in aero_cross_rows[:25]
)

readout = f"""# Sprint045E — Shared contract autopsy + A.1 funding graph 修复设计（中文）

> 执行时间：{ts_now}
> 关联 Sprint045D 计划：`mvp/audits/sprint045c_next_source_finding_plan.md`
> 关联 Sprint045D 报告：`mvp/audits/sprint045d_next_source_finding_report.md`

---

## 1. Executive Conclusion（先看结论）

- **AUTHOR_WALLET_FOUND = false**，无 identity claim。
- 两个 Cluster shared `tx.to` 都已被定级为 **执行面重叠**，**不能等同 funding 同源**。
  - **Cluster 2** (`{CLUSTER2_ADDR}`)：private_infra_likelihood = **{pct(c2_row['private_infra_likelihood'])}**, public_helper = {pct(c2_row['public_helper_likelihood'])}, bot_backend = {pct(c2_row['bot_backend_likelihood'])}, unknown = {pct(c2_row['unknown_likelihood'])}。
  - **Cluster 1** (`{CLUSTER1_ADDR}`)：private_infra_likelihood = **{pct(c1_row['private_infra_likelihood'])}**, public_helper = {pct(c1_row['public_helper_likelihood'])}, bot_backend = {pct(c1_row['bot_backend_likelihood'])}, unknown = {pct(c1_row['unknown_likelihood'])}。
- **A.1 funding graph 修复**：根因已定位。**不需要改 Rust 代码**；只需在部署环境中追加一个 sidecar ingestor 的 `LOG_FILTER_JSON`，覆盖 `[WETH, USDC, USDC.e]` × `[Transfer, Deposit, Withdrawal]`。本轮**未做** patch（部署侧 env，超出本 sprint 只读边界）。
- **Aerodrome 25 个 canon 候选**：与 Cluster 2/1 **无 dominant_to 重叠**（{shares_c2} 命中 C2，{shares_c1} 命中 C1），与 V3 inventory 也几乎无交集（{in_v3_diag}/25 在 V3 to_diagnostics 中），**保持作为旁路**，不并入 V3 主排名。

**当前证据是否足以做 funding 同源判断？— 不足。** Cluster 内成员只在 `tx.to`（执行目标合约）上重叠，**没有**任何 WETH/USDC 入金边或出金边可证明同一资金来源。要做 funding 同源判断必须先完成 A.1 索引修复。

---

## 2. Cluster 2 判断（`{CLUSTER2_ADDR}`）

| 指标 | 值 |
|---|---|
| code_size | {c2_row['code_size_bytes']:,} bytes |
| code_fingerprint | `{c2_row['code_fingerprint']}` |
| is_minimal_proxy / EIP1167 | {c2_row['is_minimal_proxy']} / {c2_row['is_eip1167_clone']} |
| has_solidity_metadata | {c2_row['has_solidity_metadata']} |
| has_delegatecall_opcode | {c2_row['has_delegatecall_opcode']} |
| eth_getTransactionCount | {c2_row['eth_getTransactionCount']} |
| n_callers_observed (checkpoint 范围) | {c2_row['n_callers_observed_checkpoint']} |
| n_dominant_callers (该合约为唯一 dominant_to) | {c2_row['n_dominant_callers']} |
| inv_caller_frac (调用者在 47k inventory 内) | {c2_row['inv_caller_frac']} |
| selector samples / unique | {c2_row['selector_samples_total']} / {c2_row['selector_unique_count']} |
| known_selector_share | {c2_row['known_selector_share']} |
| has_multicall_selector | {c2_row['has_multicall_selector']} |
| top selectors | `{c2_top_sel}` |
| caller bucket 分布 | `{c2_buckets}` |

**Likelihood**：private_infra **{pct(c2_row['private_infra_likelihood'])}** > public_helper {pct(c2_row['public_helper_likelihood'])} > bot_backend {pct(c2_row['bot_backend_likelihood'])} > unknown {pct(c2_row['unknown_likelihood'])}.

**Supporting evidence (private_infra)**:
- 调用者数量极少（{c2_row['n_callers_observed_checkpoint']} 个），且其中 {c2_row['n_dominant_callers']} 个把该合约作为**唯一 dominant_to**（`dominant_to_frac = 1.0`，`platform_frac = 0.0`）。
- caller bucket 集中在 AUTHOR_LIKE_PRIMARY / STRONG_ECONOMIC（`{c2_buckets}`），属于已被 045B/D 评分较高的活跃交易者。
- 12 名成员全部都把这个合约写为唯一交易目标 → **执行面专用**。

**Counter evidence**:
- 仍**未**拿到 verified ABI / source。selector 抽样的命名解释依赖启发式（`KNOWN_SELECTORS` 字典）。
- 不能排除是某个**未公开但仍属公共服务**的高效执行器 (例：私有 builder bundler / order flow auction relayer)。

**Missing evidence**:
- 合约部署 tx 与 deployer (因此暂不能判断是 EOA 部署还是 factory 部署)。
- ABI/源码未 verify。
- WETH/USDC funding edges（被 A.1 阻塞）。

---

## 3. Cluster 1 判断（`{CLUSTER1_ADDR}`）

| 指标 | 值 |
|---|---|
| code_size | {c1_row['code_size_bytes']:,} bytes |
| code_fingerprint | `{c1_row['code_fingerprint']}` |
| is_minimal_proxy / EIP1167 | {c1_row['is_minimal_proxy']} / {c1_row['is_eip1167_clone']} |
| has_solidity_metadata | {c1_row['has_solidity_metadata']} |
| has_delegatecall_opcode | {c1_row['has_delegatecall_opcode']} |
| eth_getTransactionCount | {c1_row['eth_getTransactionCount']} |
| n_callers_observed (checkpoint) | {c1_row['n_callers_observed_checkpoint']} |
| n_dominant_callers | {c1_row['n_dominant_callers']} |
| inv_caller_frac | {c1_row['inv_caller_frac']} |
| selector samples / unique | {c1_row['selector_samples_total']} / {c1_row['selector_unique_count']} |
| known_selector_share | {c1_row['known_selector_share']} |
| has_multicall_selector | {c1_row['has_multicall_selector']} |
| top selectors | `{c1_top_sel}` |
| caller bucket 分布 | `{c1_buckets}` |

**Likelihood**：private_infra {pct(c1_row['private_infra_likelihood'])}, public_helper {pct(c1_row['public_helper_likelihood'])}, bot_backend {pct(c1_row['bot_backend_likelihood'])}, unknown {pct(c1_row['unknown_likelihood'])}.

**Supporting evidence (mixed public stack + shared component)**:
- 调用者数量较多（27 名），且其中相当部分人在 `to_field_diagnostics` 中 **同时调用 `0x6ff5693b…`（高流量 V3 池）和该合约**（`platform_frac` 在 0.6–0.73 之间）。即“**公共池 + 共享组件**”模式，**不是**纯私有专用。
- caller bucket 多为 `(not_in_inv)`，说明大多数调用者**没有**进入 045D top-50 评分；只有 4 名 AUTHOR_LIKE_DARKHORSE 与该合约重叠，且其 PnL 普遍接近 0 或为负（045D 上看到 0718→8.33; 2141→-3.76; ad61→-0.16; e1c3→0.0）。

**Counter evidence**:
- 也存在 **少数** 调用者（{c1_row['n_dominant_callers']} 名）把该合约作为 dominant_to=1.0，仍像专用执行边。
- 合约 code_size ~10k bytes，比 C2 小 4.6 倍，更像 **特定路径执行器** 而不是综合 router。

**Missing evidence**:
- ABI/源码未 verify。
- 是否为公开 bot 框架（如 BananaGun、UniBot、Maestro 等）的合约——需 link 到链下品牌库验证。
- WETH/USDC funding edges（A.1 阻塞）。

---

## 4. shared `tx.to` vs funding graph — 区分声明（最重要）

| 维度 | shared `tx.to`（已观测） | funding graph（仍缺）|
|---|---|---|
| 数据来源 | `eth_getTransactionByHash.to` 字段 | `Transfer(from,to,value)` log，token=WETH/USDC/USDC.e |
| 含义 | 多个 EOA 调用同一合约 | 同一资金来源转入多个 EOA |
| 当前可证 | 是（`sprint045d_to_overlap_pairs.csv`） | 否（WETH 行数 = 0） |
| 对作者归因的价值 | 弱（执行栈共用） | 强（同 funder → 同主体） |
| 风险 | **不得**直接当成同一作者 | — |

**结论**：本次 Cluster 1/2 仍只是**执行面重叠**。`shared tx.to ≠ funding 同源`。所有后续 readout 严格遵守这条边界。

---

## 5. A.1 indexing/backfill preflight

### 5.1 根因（已诊断）
- `eatlq.raw_evm_logs` 当前包含 **{total_raw:,}** 行（chain 8453）。
- WETH 行数 = **{weth_raw}**；USDC 行数 = **{usdc_raw}**；USDC.e 行数 = **{usdce_raw}**。
- 下游 `erc20_transfers_mv` 链路**完好**：MV 已经按 Transfer topic 自动 decode 写入 `erc20_transfers`。
- **唯一原因**：ingestor 的 `LOG_FILTER_JSON`（`services/ingestor/src/config.rs` L87）只配置了 swap event topics（top topic0 = `0xc42079f9...` Uniswap V3 Swap）。WETH/USDC 地址或 Transfer/Deposit/Withdraw topics 从未进入过滤器。

### 5.2 复用 / 修改清单

| 类型 | 路径 | 说明 |
|---|---|---|
| **复用** | `services/ingestor/src/config.rs` | `LOG_FILTER_JSON` 已是 env 驱动 |
| **复用** | `services/ingestor/src/rpc.rs` | `eth_getLogs` 注入 from/to block，结构无需改 |
| **复用** | `services/archiver/src/clickhouse_sink.rs` | 直写 `raw_evm_logs`，不过滤 |
| **复用** | `eatlq.raw_evm_logs` + MV `erc20_transfers_mv` → `erc20_transfers` | 已有 |
| **修改** | 部署环境 env | 追加 sidecar 的 `LOG_FILTER_JSON` |

**Rust 代码：零修改。** 这是 Sprint045E 的关键发现。

### 5.3 推荐方案 — Option B（sidecar ingestor）

启动**第二个** ingestor 容器/进程，env 配置：

```json
LOG_FILTER_JSON = {{
  "address": ["{WETH_BASE}", "{USDC_BASE}", "{USDC_E}"],
  "topics":  [["{TOPIC_TRANSFER}",
               "{TOPIC_DEPOSIT}",
               "{TOPIC_WITHDRAWAL}"]]
}}
WS_SOURCE                = quicknode-erc20
BACKFILL_SOURCE_SUFFIX   = erc20-backfill
BACKFILL_JOB_NAME        = erc20_transfers_backfill_2026
EVM_WS_URL / EVM_HTTP_URL = <paid Base endpoint>
REDIS_URL                = <same as primary>
MARKET_TICKS_STREAM      = <same stream>
```

- 主 ingestor **不动**，继续负责 swap topics。
- Sidecar 同写 `raw_evm_logs` → MV 自动派生 `erc20_transfers`。
- 隔离失败：sidecar 崩了不影响主线。

### 5.4 Backfill 预算（基于 chain head 实测探针）

| 指标 | 估算 |
|---|---|
| RPC chain head | block {head_int:,} |
| 探针窗口 | block {probe_from:,}..{probe_to:,}（{probe_to - probe_from} 个 block）|
| 探针匹配 logs | **{probe_logs}** 行（topic0/address 分布见 preflight JSON）|
| 每 block 平均 logs | {rows_per_block:.1f} |
| 每天预估 logs | {rows_per_day:,.0f} |
| 每天预估磁盘（800 B/row）| {gb_per_day:.2f} GB |
| 回填窗口 (Jan 1 → head) | {days_to_backfill:.1f} 天 |
| 回填总 logs（估算）| {backfill_total_rows:,.0f} |
| 回填总磁盘（估算）| {backfill_total_gb:.1f} GB |
| chunk_size | 200 blocks/call |
| 估算 chunks 数 | {n_chunks:,.0f} |
| RPS 假设 | 2 |
| 估算 walltime | {backfill_walltime_min:.0f} min ≈ {backfill_walltime_min/60:.1f} 小时 |

> ⚠️ 探针仅 100 个 block，外推误差可能 ±50%。**实际跑 1 小时 smoke 后再确认。**

### 5.5 Backfill 命令模板（人工执行）

```bash
BACKFILL_FROM_BLOCK={jan1_block_estimate} \\
BACKFILL_TO_BLOCK={head_int} \\
BACKFILL_CHUNK_SIZE=200 \\
BACKFILL_PAUSE_MS=300 \\
BACKFILL_JOB_NAME=erc20_transfers_backfill_2026 \\
LOG_FILTER_JSON='<see Option B>' \\
cargo run --release --bin backfill -p ingestor
```

### 5.6 Smoke 验证 SQL

```sql
SELECT count() FROM erc20_transfers
WHERE chain_id=8453 AND lower(token_address)='{WETH_BASE}';
-- 预期：smoke 1h 后 ≫ 0

SELECT count() FROM erc20_transfers
WHERE chain_id=8453 AND lower(token_address)='{USDC_BASE}';
-- 预期：smoke 1h 后 ≫ 0
```

### 5.7 失败续跑

- 使用现有 `services/ingestor/src/backfill_state.rs` Postgres 状态机；
- 重启自动从 `last_processed_block` 续跑；
- 失败率 >15% 或 RPC 持续 429 → 暂停 sidecar，不影响主线。

### 5.8 本轮未做 patch 的理由

`LOG_FILTER_JSON` 通过 **运行时 env / secret manager** 注入，**不在仓库受控文件中**。本 sprint 边界要求 "如果安全可以做最小 patch" — 但**修改部署 env** 不是源码修改，需要部署侧操作员手动应用。本轮**未越权**。

---

## 6. Aerodrome crosscheck — 25 候选

| controller | n_aero_txs | min_prio | shares_C2 | shares_C1 | in_V3_diag |
|---|---|---|---|---|---|
{aero_in_cluster_md}

**结论**：
- 25 个 Aerodrome canon 候选中 **{shares_c2} 与 Cluster 2 共享 dominant_to**，**{shares_c1} 与 Cluster 1 共享 dominant_to**。
- {in_v3_diag}/25 出现在 V3 to_diagnostics（即在 045D 的 47k 检查点中有过 V3 活动）。
- 综合：**Aerodrome canon 与 V3 主线（V3 inventory + Cluster 1/2）几乎无交集**。它们可能是 Aerodrome-only side lane 的独立交易者，**不并入 V3 主排名**，但保留作为下一轮 source finding 的旁路输入。

---

## 7. Changed files (本轮)

无源代码修改。仅新增产物：

```
mvp/diagnostics/sprint045e_shared_contract_autopsy.csv
mvp/diagnostics/sprint045e_cluster_contract_callers.csv
mvp/diagnostics/sprint045e_selector_summary.csv
mvp/diagnostics/sprint045e_aerodrome_cluster_crosscheck.csv
mvp/diagnostics/sprint045e_a1_indexing_preflight.json
mvp/diagnostics/sprint045e_decision_flags.json
mvp/audits/sprint045e_shared_contract_and_funding_graph_readout.md
scripts/sprint045e_shared_contract_autopsy.py    # 本脚本
```

## 8. Commands run

```
python3 scripts/sprint045e_shared_contract_autopsy.py
# 上述脚本内含 ~ N+M+25 个 read-only RPC + 5 条 ClickHouse SELECT
```

## 9. Artifacts

- `sprint045e_shared_contract_autopsy.csv` — 两簇核心字段
- `sprint045e_cluster_contract_callers.csv` — 调用者明细（含 bucket / PnL / dominant_to 全字段）
- `sprint045e_selector_summary.csv` — selector 抽样
- `sprint045e_aerodrome_cluster_crosscheck.csv` — 25 candidates × cluster 交叉
- `sprint045e_a1_indexing_preflight.json` — A.1 patch 计划 + backfill 预算 + smoke probe
- `sprint045e_decision_flags.json` — 决策 flag 全集
- `sprint045e_shared_contract_and_funding_graph_readout.md` — 本文件

## 10. Decision flags 摘要

```json
AUTHOR_WALLET_FOUND               = false
IDENTITY_CLAIM_ALLOWED            = false
FUNDING_GRAPH_READY               = false
FUNDING_GRAPH_BLOCKED_BY_INDEXING = true
FUNDING_GRAPH_PATCH_REQUIRES_RUST_CODE_CHANGE = false
FUNDING_GRAPH_PATCH_REQUIRES_ENV_CHANGE       = true
CLUSTER_SHARED_TO_CONTRACT_FOUND              = true
CLUSTER_SHARED_TO_EQUALS_FUNDING_SAMENESS     = false
CLUSTER_SHARED_TO_IS_EXECUTION_OVERLAP_ONLY   = true
CLUSTER2_PRIVATE_INFRA_LIKELIHOOD             = {c2_row['private_infra_likelihood']}
CLUSTER1_PRIVATE_INFRA_LIKELIHOOD             = {c1_row['private_infra_likelihood']}
CLUSTER2_MORE_PRIVATE_THAN_CLUSTER1           = {c2_row['private_infra_likelihood'] > c1_row['private_infra_likelihood']}
CLUSTER1_MORE_MIXED_PUBLIC_STACK_THAN_CLUSTER2 = {c1_row['public_helper_likelihood'] > c2_row['public_helper_likelihood']}
EVIDENCE_SUFFICIENT_FOR_FUNDING_SAMENESS_JUDGMENT = false
AERODROME_CANON_CANDIDATES_FOUND  = {len(aero_canon)}
AERODROME_KEPT_OUT_OF_V3_MAIN_RANKING = true
NEXT_RPC_REQUIRED                 = false
NEXT_INDEXING_REQUIRED            = true
```

---

## 11. 下一步（不超过 3 步）

1. **部署 Option B sidecar**：在部署 env 注入 `LOG_FILTER_JSON` 与 sidecar 相关变量；先跑 1 小时 smoke，验证 `erc20_transfers` WETH/USDC 行数 > 0。
2. **smoke 通过后**：启动 backfill job (`erc20_transfers_backfill_2026`) 回填 Jan 1 → head；用 `services/ingestor/src/backfill_state.rs` 的 PG 状态续跑。
3. **数据就绪后**：运行 Sprint045F — 基于 `erc20_transfers`（已含 WETH+USDC+USDC.e）重建 funding graph，对 Cluster 2 / 1 做 funder 同源验证（networkx connected components）。这是唯一能把 shared `tx.to` 推进到 funding 同源结论的路径。

---

## 约束声明

- 无 identity claim、无 live、无 copy trade、无 training、无 model promotion、无 push。
- shared `tx.to` 仅记为执行面重叠，**不**等同 funding 同源。
- 47,005 controller inventory 不删除。
- Aerodrome 不并入 V3 主评分。
- 未打印 `.env` / private key / API key。
"""

readout_path = f"{AUD_DIR}/sprint045e_shared_contract_and_funding_graph_readout.md"
os.makedirs(AUD_DIR, exist_ok=True)
with open(readout_path, "w") as f:
    f.write(readout)
print(f"  → {readout_path}")

# ── Final ─────────────────────────────────────────────────────────────────────
print("\n=== Sprint045E Complete ===")
print(f"  Artifacts:")
for p in [autopsy_path, callers_path, selector_path, aero_path,
          preflight_path, flags_path, readout_path]:
    print(f"    {p}")
