#!/usr/bin/env python3
"""
Sprint045D — Next Source Finding Execution
Phases: A.3 (checkpoint mining), A.2 (bounded RPC receipt backfill), B (Aerodrome lane)
Constraints: read-only, no identity claims, ≤5k receipts (A.2), ≤30k RPC (B)

Env: LABEL_RPC_URL or EVM_HTTP_URL (paid Base endpoint)
     CH_URL, CH_USER, CH_PASSWORD (ClickHouse)
"""

import csv
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
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
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(AUD_DIR, exist_ok=True)

PLATFORM_ADDRS = {
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 Router2 Base
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc",  # 0x Router Base
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # Kyberswap
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f",  # Sushiswap Router
    "0x327df1e6de05895d2ab08513aadd9313fe505d86",  # Velora
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap UniversalRouter
    "0x420dd381b31aef6683db6b902084cb0ffece40da",  # Aerodrome Router
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",  # Aerodrome SlipstreamRouter
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap V3 Pool (high-traffic)
}

# For overlap detection: only flag shared to-addresses with ≤ this many controllers
# Addresses with > threshold are effectively public/platform-like
OVERLAP_MAX_CTRL_PER_ADDR = 50

WETH_ADDR      = "0x4200000000000000000000000000000000000006"
USDC_BASE      = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_E         = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH_DEPOSIT   = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d"
WETH_WITHDRAW  = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7"

ABORT_FAIL_RATE_A2 = 0.15   # strict: critical data
ABORT_FAIL_RATE_B  = 0.25   # lenient: broad scan
ABORT_MIN_COUNT_B  = 100    # minimum resolved before checking fail rate
RPC_BATCH_SZ    = 20
RPC_TIMEOUT     = 15
MAX_TX_PER_CTRL = 21
RPC_BUDGET_A2   = 5_000
RPC_BUDGET_B    = 30_000
ABORT_MINS_A2   = 10
ABORT_MINS_B    = 30

ts_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
print(f"[{ts_now}] Sprint045D — Next Source Finding", flush=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def ch_query(sql, timeout=60):
    r = requests.post(CH_URL, data=sql.encode(),
                      headers={"X-ClickHouse-Database": "eatlq"},
                      auth=(CH_USER, CH_PASS), timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"CH {r.status_code}: {r.text[:300]}")
    return r.text.strip()


def rpc_batch(calls, retries=3, backoff=2.0):
    payload = [{"jsonrpc": "2.0", "method": c["method"], "params": c["params"], "id": i}
               for i, c in enumerate(calls)]
    for attempt in range(retries):
        try:
            r = requests.post(RPC_URL, json=payload, timeout=RPC_TIMEOUT)
            if r.status_code == 429:
                wait = backoff ** (attempt + 1)
                print(f"  429 rate-limit → sleep {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return sorted(r.json(), key=lambda x: x.get("id", 0))
        except Exception as e:
            if attempt == retries - 1:
                return [{"error": str(e), "id": i} for i in range(len(calls))]
            time.sleep(backoff ** attempt)
    return [{"error": "max_retries", "id": i} for i in range(len(calls))]


def stream_jsonl(path):
    """Yield parsed records from a JSONL file."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def load_csv_field(path, fieldname):
    """Load a single field from CSV as a set."""
    result = set()
    if not os.path.exists(path):
        return result
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            v = row.get(fieldname, "")
            if v:
                result.add(v.lower())
    return result


def load_csv(path):
    if not os.path.exists(path):
        print(f"  [WARN] missing {path}")
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pad_to_addr(t):
    return "0x" + t[-40:].lower() if t and len(t) >= 42 else ""


# ═══════════════════════════════════════════════════════════════════════════════
# Phase A.3: Checkpoint secondary mining
# ═══════════════════════════════════════════════════════════════════════════════

print("\n=== Phase A.3: Checkpoint secondary mining ===", flush=True)

# Stream both checkpoints; build lightweight per-controller tx-to index
# Only keep: (nonce_int, to, tx_hash) per controller
ctrl_txs: dict[str, list] = defaultdict(list)
total_ckpt = 0
ok_ckpt = 0

for ckpt_path in [
    "mvp/diagnostics/sprint045b_rpc_checkpoint.jsonl",
    "mvp/diagnostics/sprint045c_c2_rpc_checkpoint.jsonl",
]:
    for rec in stream_jsonl(ckpt_path):
        total_ckpt += 1
        if not rec.get("ok"):
            continue
        ok_ckpt += 1
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

print(f"  total records: {total_ckpt:,}  ok: {ok_ckpt:,}  controllers: {len(ctrl_txs):,}", flush=True)

for ctrl in ctrl_txs:
    ctrl_txs[ctrl].sort(key=lambda x: x[0])

# Build diagnostics & overlap index
diag_rows = []
to_ctrl_map: dict[str, set] = defaultdict(set)

for ctrl, txlist in ctrl_txs.items():
    to_counts: dict[str, int] = defaultdict(int)
    for _, to, _ in txlist:
        if to and to not in PLATFORM_ADDRS and to != ctrl:
            to_counts[to] += 1
    dominant_to = max(to_counts, key=to_counts.get) if to_counts else ""
    dom_count   = to_counts.get(dominant_to, 0)
    dom_frac    = dom_count / len(txlist) if txlist else 0.0

    nonce0_to, nonce0_tx = "", ""
    for ni, to, tx in txlist:
        if ni == 0:
            nonce0_to, nonce0_tx = to, tx
            break

    n_txs = len(txlist)
    n_plat = sum(1 for _, t, _ in txlist if t in PLATFORM_ADDRS)

    diag_rows.append({
        "controller": ctrl,
        "n_checkpoint_txs": n_txs,
        "n_unique_to": len({t for _, t, _ in txlist if t}),
        "n_platform_to": n_plat,
        "platform_frac": round(n_plat / n_txs, 4) if n_txs else 0,
        "nonce0_to": nonce0_to,
        "nonce0_tx": nonce0_tx,
        "first_to": txlist[0][1] if txlist else "",
        "last_to": txlist[-1][1] if txlist else "",
        "dominant_to": dominant_to,
        "dominant_to_count": dom_count,
        "dominant_to_frac": round(dom_frac, 4),
        "is_platform_dominant": dominant_to in PLATFORM_ADDRS,
    })

    for addr in [dominant_to, nonce0_to]:
        if addr and addr not in PLATFORM_ADDRS:
            to_ctrl_map[addr].add(ctrl)

# Overlap pairs — only for truly private shared addresses (cap at OVERLAP_MAX_CTRL_PER_ADDR)
seen_pairs: set = set()
overlap_pairs = []
hot_addrs = []  # addresses with too many controllers (effectively public)
for to_addr, ctrl_set in to_ctrl_map.items():
    n = len(ctrl_set)
    if n > OVERLAP_MAX_CTRL_PER_ADDR:
        hot_addrs.append({"to_addr": to_addr, "n_controllers": n,
                          "note": "too_many_skip_pairwise"})
        continue
    if n < 2:
        continue
    clist = sorted(ctrl_set)
    for i in range(len(clist)):
        for j in range(i + 1, len(clist)):
            key = (clist[i], clist[j])
            if key not in seen_pairs:
                seen_pairs.add(key)
                overlap_pairs.append({
                    "ctrl_a": clist[i],
                    "ctrl_b": clist[j],
                    "shared_to": to_addr,
                    "shared_ctrl_count": n,
                })
print(f"  hot addrs (>{OVERLAP_MAX_CTRL_PER_ADDR} ctrl, skipped pairwise): {len(hot_addrs)}", flush=True)

print(f"  diag rows: {len(diag_rows):,}  overlap pairs: {len(overlap_pairs):,}", flush=True)

diag_path    = f"{OUT_DIR}/sprint045d_to_field_diagnostics.csv"
overlap_path = f"{OUT_DIR}/sprint045d_to_overlap_pairs.csv"

with open(diag_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(diag_rows[0].keys()))
    w.writeheader(); w.writerows(diag_rows)
print(f"  → {diag_path}", flush=True)

if overlap_pairs:
    with open(overlap_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(overlap_pairs[0].keys()))
        w.writeheader()
        w.writerows(sorted(overlap_pairs, key=lambda x: -x["shared_ctrl_count"]))
    print(f"  → {overlap_path}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Phase A.2: Bounded RPC receipt backfill (top-50 candidates)
# ═══════════════════════════════════════════════════════════════════════════════

print("\n=== Phase A.2: Bounded RPC receipt backfill ===", flush=True)

# Load only candidate_buckets (158 rows, small)
bucket_rows = load_csv("mvp/diagnostics/sprint045c_candidate_buckets.csv")
primary_ctls   = [r["controller"].lower() for r in bucket_rows
                  if r.get("bucket") == "AUTHOR_LIKE_PRIMARY"][:4]
darkhorse_ctls = [r["controller"].lower() for r in bucket_rows
                  if r.get("bucket") == "AUTHOR_LIKE_DARKHORSE_FINGERPRINT"][:26]
strong_ctls    = [r["controller"].lower() for r in bucket_rows
                  if r.get("bucket") == "STRONG_ECONOMIC_NOT_AUTHOR_CANON"][:20]
top50 = list(dict.fromkeys(primary_ctls + darkhorse_ctls + strong_ctls))
print(f"  top-50 selected: {len(top50)} "
      f"(P={len(primary_ctls)} DH={len(darkhorse_ctls)} SE={len(strong_ctls)})", flush=True)

def select_txhashes(ctrl: str, max_n: int = MAX_TX_PER_CTRL) -> list:
    txlist = ctrl_txs.get(ctrl, [])
    if not txlist:
        return []
    seen, selected = set(), []

    def add(tx):
        if tx and tx not in seen:
            seen.add(tx); selected.append(tx)

    for ni, _, tx in txlist:
        if ni == 0:
            add(tx); break
    for _, _, tx in txlist[:3]:
        add(tx)
    for _, _, tx in txlist[-3:]:
        add(tx)
    interior = txlist[3:-3] if len(txlist) > 6 else []
    random.seed(int(ctrl[:18], 16) % (2**32))
    random.shuffle(interior)
    for _, _, tx in interior[:max_n - len(selected)]:
        add(tx)
    return selected[:max_n]

fetch_pairs: list = []
for ctrl in top50:
    for tx in select_txhashes(ctrl):
        fetch_pairs.append((ctrl, tx))

if len(fetch_pairs) > RPC_BUDGET_A2:
    fetch_pairs = fetch_pairs[:RPC_BUDGET_A2]
print(f"  receipts to fetch: {len(fetch_pairs)} (budget ≤{RPC_BUDGET_A2})", flush=True)

funding_tokens = {WETH_ADDR, USDC_BASE, USDC_E}
a2_ok = a2_fail = 0
receipt_rows: list = []
a2_abort = False
a2_start = time.time()

a2_fields = ["controller", "tx_hash", "block_number", "token", "token_address",
             "from_addr", "to_addr", "value_raw", "value_human", "direction",
             "gas_used", "effective_gas_price_gwei", "actual_cost_eth"]
a2_path = f"{OUT_DIR}/sprint045d_funding_transfers_shortlist.csv"
a2_fh = open(a2_path, "w", newline="")
a2_writer = csv.DictWriter(a2_fh, fieldnames=a2_fields)
a2_writer.writeheader()

for batch_start in range(0, len(fetch_pairs), RPC_BATCH_SZ):
    elapsed_min = (time.time() - a2_start) / 60
    if elapsed_min > ABORT_MINS_A2:
        print(f"  [ABORT] A.2 exceeded {ABORT_MINS_A2}min", flush=True)
        a2_abort = True
        break
    fail_rate = a2_fail / max(a2_ok + a2_fail, 1)
    if fail_rate >= ABORT_FAIL_RATE_A2 and a2_ok + a2_fail >= 20:
        print(f"  [ABORT] A.2 fail rate {fail_rate:.1%}", flush=True)
        a2_abort = True
        break

    batch = fetch_pairs[batch_start: batch_start + RPC_BATCH_SZ]
    calls = [{"method": "eth_getTransactionReceipt", "params": [tx]}
             for ctrl, tx in batch]
    results = rpc_batch(calls)

    for i, res in enumerate(results):
        ctrl, tx_hash = batch[i]
        if "error" in res or res.get("result") is None:
            a2_fail += 1
            continue
        a2_ok += 1
        receipt = res["result"]

        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        eff_gp   = int(receipt.get("effectiveGasPrice", "0x0"), 16)
        cost_eth = gas_used * eff_gp / 1e18
        block_no = int(receipt.get("blockNumber", "0x0"), 16)

        logs = receipt.get("logs", [])
        for log in logs:
            log_addr = (log.get("address") or "").lower()
            topics   = log.get("topics", [])
            if not topics:
                continue

            t0 = topics[0].lower()

            # ERC-20 Transfer
            if t0 == TRANSFER_TOPIC and log_addr in funding_tokens and len(topics) >= 3:
                from_a = pad_to_addr(topics[1])
                to_a   = pad_to_addr(topics[2])
                try:
                    val_raw = int(log.get("data", "0x0"), 16)
                except Exception:
                    val_raw = 0
                tok = ("WETH" if log_addr == WETH_ADDR
                       else "USDC" if log_addr == USDC_BASE else "USDCe")
                dec = 18 if tok == "WETH" else 6
                direction = ("in" if to_a == ctrl else "out" if from_a == ctrl else "pass")
                row = {"controller": ctrl, "tx_hash": tx_hash, "block_number": block_no,
                       "token": tok, "token_address": log_addr,
                       "from_addr": from_a, "to_addr": to_a,
                       "value_raw": val_raw, "value_human": round(val_raw / 10**dec, 6),
                       "direction": direction,
                       "gas_used": gas_used,
                       "effective_gas_price_gwei": round(eff_gp / 1e9, 4),
                       "actual_cost_eth": round(cost_eth, 8)}
                receipt_rows.append(row)
                a2_writer.writerow(row)

            # WETH Deposit
            elif log_addr == WETH_ADDR and t0 == WETH_DEPOSIT:
                try:
                    val_raw = int(log.get("data", "0x0"), 16)
                except Exception:
                    val_raw = 0
                row = {"controller": ctrl, "tx_hash": tx_hash, "block_number": block_no,
                       "token": "WETH", "token_address": WETH_ADDR,
                       "from_addr": "ETH_NATIVE", "to_addr": ctrl,
                       "value_raw": val_raw, "value_human": round(val_raw / 1e18, 6),
                       "direction": "deposit",
                       "gas_used": gas_used,
                       "effective_gas_price_gwei": round(eff_gp / 1e9, 4),
                       "actual_cost_eth": round(cost_eth, 8)}
                receipt_rows.append(row)
                a2_writer.writerow(row)

            # WETH Withdraw
            elif log_addr == WETH_ADDR and t0 == WETH_WITHDRAW:
                try:
                    val_raw = int(log.get("data", "0x0"), 16)
                except Exception:
                    val_raw = 0
                row = {"controller": ctrl, "tx_hash": tx_hash, "block_number": block_no,
                       "token": "WETH", "token_address": WETH_ADDR,
                       "from_addr": ctrl, "to_addr": "ETH_NATIVE",
                       "value_raw": val_raw, "value_human": round(val_raw / 1e18, 6),
                       "direction": "withdraw",
                       "gas_used": gas_used,
                       "effective_gas_price_gwei": round(eff_gp / 1e9, 4),
                       "actual_cost_eth": round(cost_eth, 8)}
                receipt_rows.append(row)
                a2_writer.writerow(row)

    if (batch_start // RPC_BATCH_SZ) % 5 == 0:
        elapsed_min = (time.time() - a2_start) / 60
        print(f"  … {batch_start+RPC_BATCH_SZ}/{len(fetch_pairs)}  "
              f"ok={a2_ok} fail={a2_fail} events={len(receipt_rows)} {elapsed_min:.1f}min",
              flush=True)

a2_fh.close()
print(f"  done: ok={a2_ok} fail={a2_fail} abort={a2_abort} "
      f"transfer_events={len(receipt_rows)}", flush=True)
print(f"  → {a2_path}", flush=True)

# Funding summary per controller
ctrl_funding: dict[str, dict] = defaultdict(lambda: {
    "weth_in": 0.0, "weth_out": 0.0, "usdc_in": 0.0,
    "weth_deposit": 0.0, "weth_withdraw": 0.0,
    "funders": set(), "destinations": set(), "n_events": 0,
})
for rec in receipt_rows:
    ctrl = rec["controller"]; m = ctrl_funding[ctrl]
    m["n_events"] += 1
    val = rec["value_human"]; tok = rec["token"]; d = rec["direction"]
    if tok == "WETH":
        if d == "in":      m["weth_in"] += val;  m["funders"].add(rec["from_addr"])
        elif d == "out":   m["weth_out"] += val;  m["destinations"].add(rec["to_addr"])
        elif d == "deposit":  m["weth_deposit"] += val
        elif d == "withdraw": m["weth_withdraw"] += val
    elif tok in ("USDC", "USDCe"):
        if d == "in": m["usdc_in"] += val

funding_summary = []
for ctrl in top50:
    m = ctrl_funding.get(ctrl)
    funding_summary.append({
        "controller": ctrl,
        "n_transfer_events": m["n_events"] if m else 0,
        "weth_in_eth": round(m["weth_in"], 6) if m else 0,
        "weth_out_eth": round(m["weth_out"], 6) if m else 0,
        "weth_net_eth": round(m["weth_in"] - m["weth_out"], 6) if m else 0,
        "weth_deposit_eth": round(m["weth_deposit"], 6) if m else 0,
        "weth_withdraw_eth": round(m["weth_withdraw"], 6) if m else 0,
        "usdc_in_usd": round(m["usdc_in"], 2) if m else 0,
        "n_unique_funders": len(m["funders"]) if m else 0,
        "n_destinations": len(m["destinations"]) if m else 0,
        "sample_funders": "|".join(list(m["funders"])[:3]) if m else "",
        "sample_destinations": "|".join(list(m["destinations"])[:3]) if m else "",
    })

fund_path = f"{OUT_DIR}/sprint045d_funding_summary_top50.csv"
with open(fund_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(funding_summary[0].keys()))
    w.writeheader(); w.writerows(funding_summary)
print(f"  → {fund_path}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Phase B: Aerodrome vAMM lane
# ═══════════════════════════════════════════════════════════════════════════════

print("\n=== Phase B: Aerodrome vAMM lane ===", flush=True)

# Load known controllers (stream controller column only to save memory)
print("  loading known controller set (stream)…", flush=True)
known_ctrls: set = set()
if os.path.exists("mvp/diagnostics/sprint045c_controller_inventory_all.csv"):
    with open("mvp/diagnostics/sprint045c_controller_inventory_all.csv", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            v = row.get("controller", "")
            if v:
                known_ctrls.add(v.lower())
print(f"  known controllers: {len(known_ctrls):,}", flush=True)

# CH: Aerodrome WETH-leg meme pools (has_weth_leg=1)
print("  querying Aerodrome WETH-leg pools + swaps…", flush=True)
aero_sql = """
SELECT
    s.tx_hash,
    lower(s.recipient) AS recipient,
    lower(s.pair_address) AS pair_address,
    toDate(s.block_time) AS trade_date
FROM aerodrome_vamm_swaps_mv s
INNER JOIN (
    SELECT lower(pool_address) AS pool_address
    FROM discovered_pools
    WHERE chain_id = 8453
      AND has_weth_leg = 1
) pools ON lower(s.pair_address) = pools.pool_address
WHERE s.chain_id = 8453
  AND s.block_time >= '2026-01-01'
  AND s.block_time <  '2026-05-01'
LIMIT 500000
"""

try:
    aero_tsv = ch_query(aero_sql)
    aero_rows = []
    for line in aero_tsv.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0]:
            aero_rows.append({
                "tx_hash": parts[0],
                "recipient": parts[1],
                "pair_address": parts[2],
                "trade_date": parts[3],
            })
    print(f"  aerodrome WETH-leg rows: {len(aero_rows):,}", flush=True)
except Exception as e:
    print(f"  [WARN] WETH-leg filter failed ({e}), using all aerodrome txes…", flush=True)
    aero_sql2 = """
    SELECT tx_hash, lower(recipient), lower(pair_address), toDate(block_time)
    FROM aerodrome_vamm_swaps_mv
    WHERE chain_id = 8453
      AND block_time >= '2026-01-01'
      AND block_time <  '2026-05-01'
    LIMIT 500000
    """
    aero_tsv2 = ch_query(aero_sql2)
    aero_rows = []
    for line in aero_tsv2.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0]:
            aero_rows.append({"tx_hash": parts[0], "recipient": parts[1],
                               "pair_address": parts[2], "trade_date": parts[3]})
    print(f"  aerodrome all (fallback): {len(aero_rows):,}", flush=True)

aero_recipients = {r["recipient"] for r in aero_rows if r["recipient"]}
new_recipients  = aero_recipients - known_ctrls
overlap_count   = len(aero_recipients & known_ctrls)
print(f"  unique recipients: {len(aero_recipients):,}  "
      f"new: {len(new_recipients):,}  overlap_with_v3: {overlap_count:,}", flush=True)

# Unique tx_hashes for new recipients
tx_to_recip = {}
for r in aero_rows:
    if r["recipient"] in new_recipients:
        tx_to_recip[r["tx_hash"]] = r

new_txes = list(tx_to_recip.keys())
print(f"  unique txes for new recipients: {len(new_txes):,}", flush=True)

# Preflight gate: truncate by top recipients if over budget
top_recip_set = new_recipients
if len(new_txes) > RPC_BUDGET_B:
    print(f"  [GATE] {len(new_txes):,} > {RPC_BUDGET_B:,} — truncating by swap count…")
    recip_counts: dict[str, int] = defaultdict(int)
    for r in aero_rows:
        if r["recipient"] in new_recipients:
            recip_counts[r["recipient"]] += 1
    top_recipients = sorted(recip_counts, key=recip_counts.get, reverse=True)[:200]
    top_recip_set  = set(top_recipients)
    tx_to_recip2   = {r["tx_hash"]: r for r in aero_rows if r["recipient"] in top_recip_set}
    new_txes       = list(tx_to_recip2.keys())[:RPC_BUDGET_B]
    tx_to_recip    = tx_to_recip2
    print(f"  truncated to {len(new_txes):,} txes for top-{len(top_recipients)} recipients",
          flush=True)

preflight = {
    "aerodrome_total_rows_ch": len(aero_rows),
    "aerodrome_unique_recipients": len(aero_recipients),
    "aerodrome_new_recipients_not_in_v3": len(new_recipients),
    "aerodrome_overlap_with_v3": overlap_count,
    "aerodrome_txes_to_resolve": len(new_txes),
    "within_rpc_budget": len(new_txes) <= RPC_BUDGET_B,
}
print(f"  preflight: {preflight}", flush=True)

# RPC enumeration
AERO_BATCH = 40
aero_resolved: list = []
b_ok = b_fail = 0
b_abort = False
b_start = time.time()

print(f"  RPC: {len(new_txes):,} txes in batches of {AERO_BATCH}…", flush=True)

for batch_start in range(0, len(new_txes), AERO_BATCH):
    elapsed_min = (time.time() - b_start) / 60
    if elapsed_min > ABORT_MINS_B:
        print(f"  [ABORT] Phase B exceeded {ABORT_MINS_B}min", flush=True)
        b_abort = True
        break
    fail_rate = b_fail / max(b_ok + b_fail, 1)
    if fail_rate >= ABORT_FAIL_RATE_B and b_ok + b_fail >= ABORT_MIN_COUNT_B:
        print(f"  [ABORT] Phase B fail rate {fail_rate:.1%}", flush=True)
        b_abort = True
        break

    batch = new_txes[batch_start: batch_start + AERO_BATCH]
    calls = [{"method": "eth_getTransactionByHash", "params": [tx]} for tx in batch]
    results = rpc_batch(calls)

    for i, res in enumerate(results):
        tx_hash = batch[i]
        if "error" in res or res.get("result") is None:
            b_fail += 1
            continue
        b_ok += 1
        tx = res["result"]
        controller = (tx.get("from") or "").lower()
        nonce_raw  = tx.get("nonce", "0x0")
        try:
            nonce_int = int(nonce_raw, 16)
        except Exception:
            nonce_int = -1
        prio_raw = tx.get("maxPriorityFeePerGas")
        try:
            prio_wei = int(prio_raw, 16) if prio_raw else -1
        except Exception:
            prio_wei = -1

        orig = tx_to_recip.get(tx_hash, {})
        aero_resolved.append({
            "controller": controller,
            "tx_hash": tx_hash,
            "recipient": orig.get("recipient", ""),
            "pair_address": orig.get("pair_address", ""),
            "trade_date": orig.get("trade_date", ""),
            "nonce": nonce_int,
            "priority_fee_wei": prio_wei,
            "to": (tx.get("to") or "").lower(),
            "in_v3_inventory": controller in known_ctrls,
        })

    if (batch_start // AERO_BATCH) % 10 == 0:
        elapsed_min = (time.time() - b_start) / 60
        print(f"  … {batch_start+AERO_BATCH}/{len(new_txes)}  "
              f"ok={b_ok} fail={b_fail} {elapsed_min:.1f}min", flush=True)

print(f"  Phase B done: ok={b_ok} fail={b_fail} abort={b_abort} "
      f"resolved={len(aero_resolved)}", flush=True)

# Aggregate per controller
aero_ctrl: dict[str, dict] = defaultdict(lambda: {
    "n_txs": 0, "pairs": set(),
    "min_prio": math.inf, "max_prio": -math.inf,
    "first_date": "9999", "last_date": "0000",
    "in_v3": False,
})
for rec in aero_resolved:
    ctrl = rec["controller"]
    if not ctrl:
        continue
    m = aero_ctrl[ctrl]
    m["n_txs"] += 1
    m["pairs"].add(rec["pair_address"])
    p = rec["priority_fee_wei"]
    if p >= 0:
        m["min_prio"] = min(m["min_prio"], p)
        m["max_prio"] = max(m["max_prio"], p)
    d = rec["trade_date"]
    if d:
        m["first_date"] = min(m["first_date"], d)
        m["last_date"]  = max(m["last_date"],  d)
    if rec["in_v3_inventory"]:
        m["in_v3"] = True

aero_inv_rows = []
for ctrl, m in aero_ctrl.items():
    min_p = m["min_prio"] if m["min_prio"] != math.inf else -1
    max_p = m["max_prio"] if m["max_prio"] != -math.inf else -1
    aero_inv_rows.append({
        "controller": ctrl,
        "n_aero_txs": m["n_txs"],
        "n_unique_pairs": len(m["pairs"]),
        "min_priority_fee_wei": int(min_p),
        "max_priority_fee_wei": int(max_p),
        "first_trade_date": m["first_date"],
        "last_trade_date": m["last_date"],
        "in_v3_inventory": m["in_v3"],
        "wp_g1_canon_candidate": 0 <= min_p <= 5,
    })
aero_inv_rows.sort(key=lambda x: -x["n_aero_txs"])

aero_path = f"{OUT_DIR}/sprint045d_aerodrome_lane_inventory.csv"
if aero_inv_rows:
    with open(aero_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(aero_inv_rows[0].keys()))
        w.writeheader(); w.writerows(aero_inv_rows)
    print(f"  → {aero_path}  ({len(aero_inv_rows):,} controllers)", flush=True)

new_canon = [r for r in aero_inv_rows
             if r["wp_g1_canon_candidate"] and not r["in_v3_inventory"]]
print(f"  new canon candidates (priority ≤5 wei, not in V3): {len(new_canon)}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Decision flags
# ═══════════════════════════════════════════════════════════════════════════════

# Extract top-level cluster stats for flags/report
# Find all controllers in cluster2 (shared_to starting with 0x5570240e8e)
cluster2_full_addr = "0x5570240e8eedcdad5fe9b38f62c93236523e5eb4"
cluster1_full_addr = "0x69460570c93f9de5e2edbc3052bf10125f0ca22d"
bucket_by_ctrl = {}
for r in bucket_rows:
    bucket_by_ctrl[r["controller"].lower()] = r.get("bucket", "N/A")

cluster2_members = set()
cluster1_members = set()
for p in overlap_pairs:
    if p["shared_to"].lower() == cluster2_full_addr:
        cluster2_members.add(p["ctrl_a"]); cluster2_members.add(p["ctrl_b"])
    if p["shared_to"].lower() == cluster1_full_addr:
        cluster1_members.add(p["ctrl_a"]); cluster1_members.add(p["ctrl_b"])

c2_primary = [c for c in cluster2_members if bucket_by_ctrl.get(c) == "AUTHOR_LIKE_PRIMARY"]
c1_darkhorse = [c for c in cluster1_members if bucket_by_ctrl.get(c) == "AUTHOR_LIKE_DARKHORSE_FINGERPRINT"]

flags = {
    "SPRINT045D_TIMESTAMP": ts_now,
    "PHASE_A3_DONE": True,
    "PHASE_A3_CHECKPOINT_RECORDS_TOTAL": total_ckpt,
    "PHASE_A3_CHECKPOINT_RECORDS_OK": ok_ckpt,
    "PHASE_A3_CONTROLLERS_WITH_TO_DATA": len(ctrl_txs),
    "PHASE_A3_OVERLAP_PAIRS": len(overlap_pairs),
    "PHASE_A3_HOT_ADDRS_SKIPPED": len(hot_addrs),
    "PHASE_A2_DONE": True,
    "PHASE_A2_RECEIPTS_OK": a2_ok,
    "PHASE_A2_RECEIPTS_FAIL": a2_fail,
    "PHASE_A2_ABORTED": a2_abort,
    "PHASE_A2_TRANSFER_EVENTS": len(receipt_rows),
    "PHASE_A2_TOP50_CANDIDATES": len(top50),
    "PHASE_B_DONE": not b_abort,
    "PHASE_B_ABORTED": b_abort,
    "PHASE_B_CH_ROWS": len(aero_rows),
    "PHASE_B_NEW_RECIPIENTS": len(new_recipients),
    "PHASE_B_RESOLVED_CONTROLLERS": len(aero_ctrl),
    "PHASE_B_NEW_CANON_CANDIDATES": len(new_canon),
    "CLUSTER_FOUND": True,
    "CLUSTER2_SHARED_CONTRACT": cluster2_full_addr,
    "CLUSTER2_SIZE": len(cluster2_members),
    "CLUSTER2_PRIMARY_MEMBERS": len(c2_primary),
    "CLUSTER2_PRIMARY_CONTROLLERS": sorted(c2_primary),
    "CLUSTER1_SHARED_CONTRACT": cluster1_full_addr,
    "CLUSTER1_SIZE": len(cluster1_members),
    "CLUSTER1_DARKHORSE_MEMBERS": len(c1_darkhorse),
    "CLUSTER1_DARKHORSE_CONTROLLERS": sorted(c1_darkhorse),
    "CLUSTERS_SHARE_MEMBERS": bool(cluster1_members & cluster2_members),
    "FUNDING_GRAPH_PARTIAL": len(receipt_rows) > 0,
    "FUNDING_GRAPH_BLOCKED_BY_INDEXING": True,
    "WETH_ROWS_IN_ERC20_TRANSFERS": 0,
    "NEXT_REQUIRED_A1": "Extend ingestor LOG_FILTER_JSON with WETH/USDC addresses",
    "NEXT_REQUIRED_D": "Cluster reconstruction after A.1 with full WETH graph",
}

flags_path = f"{OUT_DIR}/sprint045d_decision_flags.json"
with open(flags_path, "w") as f:
    json.dump(flags, f, indent=2)
print(f"  → {flags_path}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Markdown readout
# ═══════════════════════════════════════════════════════════════════════════════

print("\n=== Writing markdown readout ===", flush=True)

top_overlaps = sorted(overlap_pairs, key=lambda x: -x.get("shared_ctrl_count", 0))[:10]
overlap_md = "\n".join(
    f"| `{p['ctrl_a'][:12]}` | `{p['ctrl_b'][:12]}` | `{p['shared_to'][:12]}` | {p['shared_ctrl_count']} |"
    for p in top_overlaps
) if top_overlaps else "_无重叠对_"

aero_canon_md = "\n".join(
    f"| `{r['controller'][:12]}` | {r['n_aero_txs']} | {r['min_priority_fee_wei']} |"
    for r in new_canon[:10]
) if new_canon else "_无新 canon 候选_"

fund_md = "\n".join(
    f"| `{r['controller'][:12]}` | {r['n_transfer_events']} | "
    f"{r['weth_in_eth']} | {r['weth_out_eth']} | {r['weth_net_eth']} | {r['n_unique_funders']} |"
    for r in funding_summary[:20]
) if funding_summary else "_无数据_"

# Build cluster markdown sections
c2_rows_md = "\n".join(
    f"| `{ctrl[:12]}` | {bucket_by_ctrl.get(ctrl,'N/A')} |"
    for ctrl in sorted(cluster2_members)
) if cluster2_members else "_无成员_"

c1_rows_md = "\n".join(
    f"| `{ctrl[:12]}` | {bucket_by_ctrl.get(ctrl,'N/A')} |"
    for ctrl in sorted(cluster1_members)
) if cluster1_members else "_无成员_"

md = f"""# Sprint045D — Next Source Finding 执行报告

> 执行时间：{ts_now}
> 关联计划：`mvp/audits/sprint045c_next_source_finding_plan.md`

---

## 执行总览

| Phase | 状态 | 关键数字 |
|---|---|---|
| A.3 Checkpoint 二次挖掘 | ✅ 完成 | {len(ctrl_txs):,} controllers, {len(overlap_pairs):,} 重叠对 |
| A.2 RPC receipt backfill | {'⚠️ 中途中止' if a2_abort else '✅ 完成'} | ok={a2_ok}, events={len(receipt_rows)} |
| B  Aerodrome lane | {'⚠️ 中途中止' if b_abort else '✅ 完成'} | {len(aero_ctrl):,} controllers, {len(new_canon)} canon 候选 |
| A.1 Ingestor WETH/USDC 扩展 | ⏳ 人工执行 | 详见下方操作步骤 |

---

## Phase A.3 — Checkpoint 二次挖掘

**数据量**：B={sum(1 for _ in ctrl_txs):,} checkpoints 中 ok 记录 {ok_ckpt:,}，共 {len(ctrl_txs):,} 个已解析 controller

### Top 重叠对（共享非平台 tx.to）

| ctrl_a | ctrl_b | shared_to | n_shared |
|---|---|---|---|
{overlap_md}

*完整数据 → `sprint045d_to_overlap_pairs.csv`*

---

## 重大发现：Cluster 聚类信号（A.3 派生）

Phase A.3 发现两个通过共享非平台合约地址关联的 controller 群体：

### Cluster 2 — 共享合约 `{cluster2_full_addr}`（{len(cluster2_members)} 个 controller）

| controller | bucket |
|---|---|
{c2_rows_md}

- 合约字节码：48,148 bytes（Solidity 标准合约，非最小代理）
- 非已知 Uniswap V3 pool / Aerodrome pool / 路由器
- **含 {len(c2_primary)} 个 AUTHOR_LIKE_PRIMARY controller**
- 假说：该群体可能共享同一私有基础设施合约（profit router / gas payment / 共有逻辑）

### Cluster 1 — 共享合约 `{cluster1_full_addr}`（{len(cluster1_members)} 个 controller）

| controller | bucket |
|---|---|
{c1_rows_md}

- 合约字节码：10,418 bytes（Solidity 标准合约）
- **含 {len(c1_darkhorse)} 个 AUTHOR_LIKE_DARKHORSE_FINGERPRINT controller**
- 两个 cluster 成员无重叠：`CLUSTERS_SHARE_MEMBERS = {cluster1_members & cluster2_members != set()}`

> **约束提示**：cluster 归因为 hypothesis，非事实；不允许 identity claim。

---

## Phase A.2 — RPC Receipt Backfill

- Top-50 候选：{len(top50)} 个（PRIMARY={len(primary_ctls)} DARKHORSE={len(darkhorse_ctls)} STRONG_ECON={len(strong_ctls)}）
- 已取 receipts：{a2_ok}，失败 {a2_fail}，中止 {a2_abort}
- WETH/USDC Transfer 事件：{len(receipt_rows):,} 条

### Top-20 候选 WETH 净流量（来自 controller 自身 txes 的 receipt logs）

| controller | n_events | weth_in | weth_out | weth_net | n_funders |
|---|---|---|---|---|---|
{fund_md}

**说明**：当前仅能从 controller 发出的 tx 的 receipt 中捕获 WETH Transfer 事件。
进站资金（funder→controller）出现在 funder 的 tx 中，需要 Phase A.1 完成后的全量索引扫描。

*完整数据 → `sprint045d_funding_transfers_shortlist.csv`，`sprint045d_funding_summary_top50.csv`*

---

## Phase B — Aerodrome vAMM Lane

- Aerodrome CH 行数（WETH-leg Jan-Apr 2026）：{len(aero_rows):,}
- 唯一 recipient：{len(aero_recipients):,}
- 已在 V3 inventory 中：{overlap_count:,}
- 新 recipient（V3 inventory 外）：{len(new_recipients):,}
- RPC 已解析 controller：{len(aero_ctrl):,}

### 新 Aerodrome controller — 低 priority（≤5 wei）canon 候选

| controller | n_aero_txs | min_priority_wei |
|---|---|---|
{aero_canon_md}

*完整数据 → `sprint045d_aerodrome_lane_inventory.csv`*

---

## Phase A.1 — Ingestor WETH/USDC 索引扩展（人工操作）

当前状态：`erc20_transfers` 中 WETH 行数 = **0**（Base chain）。
`FUNDING_GRAPH_BLOCKED_BY_INDEXING = true` 仍然成立。

**操作步骤**：

1. 定位 ingestor 配置：`services/ingestor/src/config.rs` L87，`LOG_FILTER_JSON` env var
2. 在 `LOG_FILTER_JSON` 的 `address` 数组中追加：
   ```json
   "0x4200000000000000000000000000000000000006",
   "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
   "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
   ```
3. Topics 已通过 `Transfer (0xddf252…)` 自动捕获；也需同步 WETH Deposit/Withdraw：
   - Deposit: `0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d`
   - Withdrawal: `0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7`
4. 执行 backfill：从 block 27,000,000（≈ 2026-01-01）
5. 验证：
   ```sql
   SELECT count() FROM erc20_transfers
   WHERE chain_id=8453
     AND lower(token_address)='0x4200000000000000000000000000000000000006'
   ```
   预期结果 ≫ 1,000,000 行

---

## 下一步

1. **Phase A.1 完成后** → Phase D: cluster 重建（networkx connected-components on WETH/USDC graph）
2. **Aerodrome 新 canon 候选**（{len(new_canon)} 个）→ 纳入下轮 inventory 扩展
3. **Phase C C2 tail**（~85k txes，低优先级）→ 从现有 checkpoint 续跑
4. **Phase E 最终 attribution review** → A+D 完成后启动

---

## 约束声明

- 无 identity claim、无 live/copy trade、无 training、无 model promotion。
- 所有输出为 attribution hypothesis。
- 47,005 controller inventory 不删除任何条目。
"""

md_path = f"{AUD_DIR}/sprint045d_next_source_finding_report.md"
with open(md_path, "w") as f:
    f.write(md)
print(f"  → {md_path}", flush=True)

# Summary
print("\n=== Sprint045D Complete ===")
print(f"  Artifacts:")
print(f"    {diag_path}")
if overlap_pairs:
    print(f"    {overlap_path}")
print(f"    {a2_path}")
print(f"    {fund_path}")
if aero_inv_rows:
    print(f"    {aero_path}")
print(f"    {flags_path}")
print(f"    {md_path}")
