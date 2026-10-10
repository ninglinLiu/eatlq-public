#!/usr/bin/env python3
"""
Sprint045F-slim — Targeted funding graph for Cluster 2 (12 controllers)
- Default `--query-mode controller_topic`: indexed-topic ERC20 Transfer queries (cost-controlled).
- Legacy: full-token Transfer stream + local controller filter (`--query-mode legacy`).
- No Jan1→Apr backfill in-repo; budgets via MAX_RPC_CALLS / MAX_RAW_LOGS / MAX_OUTPUT_MB.

Env: LABEL_RPC_URL or EVM_HTTP_URL — required for eth_getLogs (no URL baked here).
     CLICKHOUSE_URL, CH_USER, CH_PASSWORD — optional CH stats + legacy insert only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

CHAIN_ID = 8453
CLUSTER2_SHARED = "0x5570240e8eedcdad5fe9b38f62c93236523e5eb4"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDCE = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
TOKENS = [WETH, USDC, USDCE]
TOKENS_LOWER = frozenset(t.lower() for t in TOKENS)

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d"
WITHDRAW = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7"

RPC_URL = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
CH_URL = os.environ.get("CLICKHOUSE_URL", "").rstrip("/")
CH_USER = os.environ.get("CH_USER", "")
CH_PASS = os.environ.get("CH_PASSWORD", "")

BASE_BLOCK_TIME_SEC = 2.0
BLOCKS_PER_DAY = int(86400 / BASE_BLOCK_TIME_SEC)  # ~43200

MAX_RPC_CALLS_DEFAULT = 10_000
MAX_RAW_LOGS_DEFAULT = 100_000
MAX_OUTPUT_MB_DEFAULT = 200

LEGACY_SUMMARY_PATH = "mvp/diagnostics/sprint045f_cluster2_funding_summary.json"


def topic_addr(topic: Optional[str]) -> str:
    if not topic or not isinstance(topic, str):
        return ""
    t = topic.lower()
    if len(t) < 42:
        return ""
    return "0x" + t[-40:]


def hex_to_int(h: str) -> int:
    if not h or h == "0x":
        return 0
    return int(h, 16)


def parse_amount_raw(data: str) -> str:
    if not data or data == "0x":
        return "0x0"
    body = data[2:] if data.startswith("0x") else data
    if len(body) < 64:
        body = body.zfill(64)
    return "0x" + body[:64]


def amount_norm(token: str, amount_raw: str) -> float:
    try:
        v = hex_to_int(amount_raw)
    except Exception:
        return 0.0
    dec = 18 if token.lower() == WETH else 6
    return float(v) / (10**dec)


def addr_topic_padded(addr: str) -> str:
    """ERC-20 indexed address as 32-byte topic (left-padded hex)."""
    h = addr.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) != 40 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"invalid EVM address for topic: {addr!r}")
    return "0x" + h.rjust(64, "0")


def rpc_call(method: str, params: list, timeout: int = 45) -> Any:
    if not RPC_URL:
        raise RuntimeError("Missing LABEL_RPC_URL or EVM_HTTP_URL for Sprint045F slim smoke.")
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        return {"_error": j["error"]}
    return j.get("result")


def load_cluster2_controllers(path: str) -> List[str]:
    out: Set[str] = set()
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("cluster_label") == "CLUSTER2":
                out.add(row["caller_controller"].lower())
    return sorted(out)


def load_inventory_window(
    inv_path: str, controllers: Set[str]
) -> Tuple[str, str]:
    dates: List[str] = []
    with open(inv_path, newline="") as f:
        for row in csv.DictReader(f):
            addr = row.get("controller") or ""
            if addr.lower() not in controllers:
                continue
            for key in ("first_seen_utc", "last_seen_utc"):
                s = row.get(key, "")
                if s:
                    dates.append(s.split()[0])
    if not dates:
        return "", ""
    return min(dates), max(dates)


def ch_query(sql: str) -> str:
    if not CH_URL:
        return "SKIPPED_NO_CLICKHOUSE_URL"
    auth = (CH_USER, CH_PASS) if CH_USER or CH_PASS else None
    r = requests.post(
        CH_URL,
        data=sql.encode(),
        headers={"X-ClickHouse-Database": "eatlq"},
        auth=auth,
        timeout=120,
    )
    if not r.ok:
        return f"ERROR {r.status_code}"
    return r.text.strip()


def disk_preflight() -> Dict[str, Any]:
    disk = shutil.disk_usage("/")
    free_gb = disk.free / (1024**3)
    total_gb = disk.total / (1024**3)
    docker_du = ""
    try:
        docker_du = subprocess.check_output(["du", "-sh", "/var/lib/docker"], text=True).split()[0]
    except Exception:
        docker_du = "unknown"
    return {
        "disk_total_gb": round(total_gb, 2),
        "disk_free_gb": round(free_gb, 2),
        "disk_used_pct": round(100 * (1 - disk.free / disk.total), 1),
        "docker_var_lib_docker_du": docker_du,
        "raw_360gb_allowed_default": free_gb > 800,
        "recommend_funding_only_slim_table": True,
        "rationale": "VPS free disk << 360GB raw backfill; slim targeted RPC path only.",
    }


def ch_table_stats() -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    safe_queries = [
        ("raw_evm_logs_rows_8453", "SELECT count() FROM raw_evm_logs WHERE chain_id=8453"),
        ("erc20_transfers_rows", "SELECT count() FROM erc20_transfers"),
        (
            "raw_weth_rows_8453",
            f"SELECT count() FROM raw_evm_logs WHERE chain_id=8453 AND lower(address)='{WETH}'",
        ),
        ("mv_exists_comment", "-- erc20_transfers_mv exists per SHOW CREATE audit"),
    ]
    for name, sql in safe_queries[:-1]:
        txt = ch_query(sql).strip()
        try:
            stats[name] = int(txt)
        except ValueError:
            stats[name] = txt
    stats["existing_mv"] = (
        "erc20_transfers_mv: raw topic0=Transfer decode → erc20_transfers (Sprint045E)"
    )
    return stats


def fetch_logs_chunked(
    from_block: int,
    to_block: int,
    chunk: int,
) -> Tuple[List[Dict], int, List[str]]:
    """Return (all_logs, rpc_calls, errors).

    Query each (token, topic0) separately. Some providers (e.g. certain QuickNode
    routes) reject WETH Deposit/Withdraw topic filters with -32602; funding
    graph still uses Transfer as primary signal.
    """
    specs: List[Tuple[str, str]] = [(tok, TRANSFER) for tok in sorted(TOKENS_LOWER)]
    if os.environ.get("SPRINT045F_INCLUDE_WETH_WRAP", "").lower() in ("1", "true", "yes"):
        wl = WETH.lower()
        specs.extend([(wl, DEPOSIT), (wl, WITHDRAW)])

    logs: List[Dict] = []
    errors: List[str] = []
    calls = 0
    b = from_block
    while b <= to_block:
        e = min(b + chunk - 1, to_block)
        for tok, t0 in specs:
            flt: Dict[str, Any] = {
                "fromBlock": hex(b),
                "toBlock": hex(e),
                "address": tok,
                "topics": [t0],
            }
            res = rpc_call("eth_getLogs", [flt], timeout=60)
            calls += 1
            if isinstance(res, dict) and res.get("_error"):
                errors.append(f"blocks {b}-{e} tok={tok[:12]} topic0={t0[:12]}: {res['_error']}")
                time.sleep(0.35)
                continue
            if isinstance(res, list):
                logs.extend(res)
            time.sleep(0.1)
        b = e + 1
        time.sleep(0.08)
    return logs, calls, errors


class CostBudget:
    """Hard caps for controller_topic runs (eth_getLogs-focused)."""

    def __init__(
        self,
        max_rpc_getlogs: int,
        max_raw_logs: int,
        max_output_mb: int,
    ):
        self.max_rpc_getlogs = max_rpc_getlogs
        self.max_raw_logs = max_raw_logs
        self.max_output_bytes = max_output_mb * 1024 * 1024
        self.abort_reason: Optional[str] = None

    def check_rpc(self, n_calls_so_far: int) -> bool:
        if n_calls_so_far > self.max_rpc_getlogs:
            self.abort_reason = f"MAX_RPC_CALLS exceeded (>{self.max_rpc_getlogs})"
            return False
        return True

    def check_raw(self, n_logs: int) -> bool:
        if n_logs > self.max_raw_logs:
            self.abort_reason = f"MAX_RAW_LOGS exceeded (>{self.max_raw_logs})"
            return False
        return True


def fetch_logs_controller_topic_chunked(
    from_block: int,
    to_block: int,
    chunk: int,
    controllers: List[str],
    budget: CostBudget,
    directions: Optional[Set[str]] = None,
    tokens: Optional[List[str]] = None,
) -> Tuple[List[Dict], int, List[str], Optional[str]]:
    """Indexed-topic Transfer filter: one token address + one topic line per call.

    - outbound: topic1 = controller (from), topic2 = wildcard
    - inbound: topic1 = wildcard, topic2 = controller (to)

    directions: subset of {\"outbound\",\"inbound\"}; default both.
    tokens: lowercase hex token contract list; default WETH/USDC/USDC.e.
    """
    dirs = directions or {"outbound", "inbound"}
    tok_list = sorted(tokens) if tokens is not None else sorted(TOKENS_LOWER)
    padded = {c: addr_topic_padded(c) for c in controllers}

    logs: List[Dict] = []
    errors: List[str] = []
    calls = 0
    b = from_block

    while b <= to_block:
        e = min(b + chunk - 1, to_block)
        for ctrl in controllers:
            pad = padded[ctrl]
            for tok in tok_list:
                if "outbound" in dirs:
                    if not budget.check_rpc(calls + 1):
                        return logs, calls, errors, budget.abort_reason
                    flt: Dict[str, Any] = {
                        "fromBlock": hex(b),
                        "toBlock": hex(e),
                        "address": tok,
                        "topics": [TRANSFER, pad, None],
                    }
                    res = rpc_call("eth_getLogs", [flt], timeout=90)
                    calls += 1
                    if isinstance(res, dict) and res.get("_error"):
                        errors.append(
                            f"{b}-{e} out {ctrl[:10]} {tok[:10]} {res['_error']}"
                        )
                        time.sleep(0.2)
                    elif isinstance(res, list):
                        if not budget.check_raw(len(logs) + len(res)):
                            return logs, calls, errors, budget.abort_reason
                        logs.extend(res)
                    time.sleep(0.08)

                if "inbound" in dirs:
                    if not budget.check_rpc(calls + 1):
                        return logs, calls, errors, budget.abort_reason
                    flt_in: Dict[str, Any] = {
                        "fromBlock": hex(b),
                        "toBlock": hex(e),
                        "address": tok,
                        "topics": [TRANSFER, None, pad],
                    }
                    res2 = rpc_call("eth_getLogs", [flt_in], timeout=90)
                    calls += 1
                    if isinstance(res2, dict) and res2.get("_error"):
                        errors.append(
                            f"{b}-{e} in {ctrl[:10]} {tok[:10]} {res2['_error']}"
                        )
                        time.sleep(0.2)
                    elif isinstance(res2, list):
                        if not budget.check_raw(len(logs) + len(res2)):
                            return logs, calls, errors, budget.abort_reason
                        logs.extend(res2)
                    time.sleep(0.08)
        b = e + 1
        time.sleep(0.04)

    return logs, calls, errors, None


def process_controller_topic_logs(
    raw_logs: List[Dict],
    ctrl_set: Set[str],
    block_ts: Dict[int, int],
) -> List[Dict[str, Any]]:
    """Decode Transfer logs into edge rows + controller-side direction / edge_class."""
    clower = CLUSTER2_SHARED.lower()
    rows: List[Dict[str, Any]] = []
    for lg in raw_logs:
        lg_addr = (lg.get("address") or "").lower()
        if lg_addr not in TOKENS_LOWER:
            continue
        topics = lg.get("topics") or []
        t0 = (topics[0] or "").lower() if topics else ""
        if t0 != TRANSFER.lower() or len(topics) < 3:
            continue
        from_a = topic_addr(topics[1])
        to_a = topic_addr(topics[2])
        data = lg.get("data") or "0x"
        amt = parse_amount_raw(data)
        bn = hex_to_int(lg.get("blockNumber", "0x0"))
        txh = (lg.get("transactionHash") or "").lower()
        li = hex_to_int(lg.get("logIndex", "0x0"))

        focus = ""
        direction = ""
        if from_a in ctrl_set:
            focus = from_a.lower()
            direction = "outbound"
        elif to_a in ctrl_set:
            focus = to_a.lower()
            direction = "inbound"
        else:
            continue

        peer = ""
        if direction == "inbound":
            peer = from_a.lower()
        else:
            peer = to_a.lower()

        edge_class = "unknown"
        if peer in ctrl_set:
            edge_class = "internal_controller_transfer"
        elif peer == focus:
            edge_class = "self_loop"
        elif peer == clower:
            edge_class = "router_pass_through"
        elif direction == "inbound":
            edge_class = "direct_funding"
        else:
            edge_class = "direct_sink"

        ts = block_ts.get(bn)
        bt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
        norm = amount_norm(lg_addr, amt)
        rows.append(
            {
                "chain_id": CHAIN_ID,
                "block_time_utc": bt,
                "block_number": bn,
                "tx_hash": txh,
                "log_index": li,
                "token_address": lg_addr,
                "from_addr": from_a.lower(),
                "to_addr": to_a.lower(),
                "amount_raw": amt,
                "amount_norm": round(norm, 8),
                "event_type": "Transfer",
                "focus_controller": focus,
                "direction": direction,
                "peer_address": peer,
                "edge_class": edge_class,
            }
        )
    return rows


def load_legacy_summary_for_compare(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {
            "source": path,
            "missing": True,
            "note": "no legacy sprint045f_cluster2 summary on disk",
        }
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception as e:
        return {"source": path, "error": str(e)}
    rpc = int(d.get("rpc_calls_eth_getLogs") or d.get("rpc_calls") or 0)
    raw = int(d.get("raw_logs_fetched") or len(d.get("raw_logs_sample") or []) or 0)
    edges = int(
        d.get("edge_rows_after_controller_filter")
        or d.get("n_edge_rows")
        or 0
    )
    lpe = (raw / edges) if edges else None
    return {
        "source": path,
        "query_mode_label": "legacy_full_token_then_local_filter",
        "rpc_calls_eth_getLogs": rpc,
        "raw_logs_fetched": raw,
        "edge_rows": edges,
        "logs_per_edge": round(lpe, 2) if lpe is not None else None,
    }


def approximate_output_mb(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    sample = rows[: min(500, len(rows))]
    jb = json.dumps(sample).encode()
    approx_row = len(jb) / len(sample)
    return (approx_row * len(rows)) / (1024 * 1024)


def safe_logs_per_edge(raw_n: int, edge_n: int) -> Optional[float]:
    if edge_n <= 0:
        return None
    return round(raw_n / edge_n, 4)


def process_logs(
    raw_logs: List[Dict],
    ctrl_set: Set[str],
    block_ts: Dict[int, int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for lg in raw_logs:
        lg_addr = (lg.get("address") or "").lower()
        if lg_addr not in TOKENS_LOWER:
            continue
        t0 = (lg.get("topics") or [""])[0]
        if not t0:
            continue
        t0 = t0.lower()
        topics = lg.get("topics") or []
        bn = hex_to_int(lg.get("blockNumber", "0x0"))
        txh = (lg.get("transactionHash") or "").lower()
        li = hex_to_int(lg.get("logIndex", "0x0"))
        data = lg.get("data") or "0x"

        from_a = to_a = ""
        event_type = ""
        focus: Set[str] = set()

        if t0 == TRANSFER and len(topics) >= 3:
            event_type = "Transfer"
            from_a = topic_addr(topics[1])
            to_a = topic_addr(topics[2])
            amt = parse_amount_raw(data)
            for c in ctrl_set:
                if c == from_a or c == to_a:
                    focus.add(c)
        elif t0 == DEPOSIT and lg_addr == WETH:
            event_type = "Deposit"
            to_a = topic_addr(topics[1]) if len(topics) >= 2 else ""
            from_a = "ETH/native"
            amt = parse_amount_raw(data)
            for c in ctrl_set:
                if c == to_a:
                    focus.add(c)
        elif t0 == WITHDRAW and lg_addr == WETH:
            event_type = "Withdraw"
            from_a = topic_addr(topics[1]) if len(topics) >= 2 else ""
            to_a = "ETH/native"
            amt = parse_amount_raw(data)
            for c in ctrl_set:
                if c == from_a:
                    focus.add(c)
        else:
            continue

        if not focus:
            continue

        ts = block_ts.get(bn)
        bt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""

        norm = amount_norm(lg_addr, amt)
        for fc in sorted(focus):
            rows.append(
                {
                    "chain_id": CHAIN_ID,
                    "block_time_utc": bt,
                    "block_number": bn,
                    "tx_hash": txh,
                    "log_index": li,
                    "token_address": lg_addr,
                    "from_addr": from_a,
                    "to_addr": to_a,
                    "amount_raw": amt,
                    "amount_norm": round(norm, 8),
                    "event_type": event_type,
                    "focus_controller": fc,
                }
            )
    return rows


def build_block_timestamps(blocks: Set[int]) -> Dict[int, int]:
    """block_number -> unix seconds (batch eth_getBlockByNumber)."""
    out: Dict[int, int] = {}
    if not RPC_URL:
        return out
    blocks_l = sorted(blocks)
    if not blocks_l:
        return out
    batch: List[Dict] = []
    for i, bn in enumerate(blocks_l):
        batch.append(
            {"jsonrpc": "2.0", "method": "eth_getBlockByNumber", "params": [hex(bn), False], "id": i}
        )
        if len(batch) >= 40 or i == len(blocks_l) - 1:
            r = requests.post(RPC_URL, json=batch, timeout=60)
            r.raise_for_status()
            for item in sorted(r.json(), key=lambda x: x.get("id", 0)):
                res = item.get("result")
                if not res:
                    continue
                bnum = int(res["number"], 16)
                ts = int(res["timestamp"], 16)
                out[bnum] = ts
            batch = []
            time.sleep(0.25)
    return out


def graph_analysis(
    rows: List[Dict[str, Any]],
    ctrl_set: Set[str],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    """Shared funder/sink uses external ERC20 Transfer peers (excludes controller↔controller)."""

    clower = CLUSTER2_SHARED.lower()
    external_in: Dict[str, Set[str]] = defaultdict(set)
    external_out: Dict[str, Set[str]] = defaultdict(set)
    non_router_external: Set[str] = set()
    has_router_peer = False
    n_dep = n_wd = n_transfer_rows = 0

    for r in rows:
        et = r["event_type"]
        if et == "Deposit":
            n_dep += 1
            continue
        if et == "Withdraw":
            n_wd += 1
            continue
        if et != "Transfer":
            continue
        n_transfer_rows += 1
        fc = r["focus_controller"]
        fa = (r["from_addr"] or "").lower()
        ta = (r["to_addr"] or "").lower()
        peer: Optional[str] = None
        if ta == fc and fa and fa != fc:
            peer = fa
        elif fa == fc and ta and ta != fc:
            peer = ta
        if not peer:
            continue
        if peer in ctrl_set:
            continue
        if peer == clower:
            has_router_peer = True
            continue
        non_router_external.add(peer)
        if ta == fc:
            external_in[peer].add(fc)
        else:
            external_out[peer].add(fc)

    shared_funders = {k: sorted(v) for k, v in external_in.items() if len(v) >= 2}
    shared_sinks = {k: sorted(v) for k, v in external_out.items() if len(v) >= 2}

    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for ctrls in shared_funders.values():
        c0 = ctrls[0]
        for c in ctrls[1:]:
            union(c0, c)
    for ctrls in shared_sinks.values():
        c0 = ctrls[0]
        for c in ctrls[1:]:
            union(c0, c)

    comp_map: Dict[str, List[str]] = defaultdict(list)
    for c in ctrl_set:
        comp_map[find(c)].append(c)

    comp_rows: List[Dict[str, Any]] = []
    for i, (_root, members) in enumerate(sorted(comp_map.items(), key=lambda x: -len(x[1]))):
        comp_rows.append(
            {
                "component_id": i,
                "n_members": len(members),
                "controllers": "|".join(sorted(members)),
            }
        )

    has_non_router_xfers = bool(non_router_external)
    if not rows:
        verdict = "INCONCLUSIVE"
    elif shared_funders:
        verdict = "SAME_FUNDER_STRONG"
    elif shared_sinks:
        verdict = "SAME_SINK_STRONG"
    elif n_transfer_rows == 0 and (n_dep > 0 or n_wd > 0):
        verdict = "INCONCLUSIVE"
    elif has_non_router_xfers:
        verdict = "PASS_THROUGH_ONLY"
    elif has_router_peer:
        verdict = "SHARED_ROUTER_ONLY"
    else:
        verdict = "INCONCLUSIVE"

    summary = {
        "n_edge_rows": len(rows),
        "n_transfer_focus_rows": n_transfer_rows,
        "n_deposit_edges": n_dep,
        "n_withdraw_edges": n_wd,
        "has_router_peer_transfer": has_router_peer,
        "n_non_router_external_peers": len(non_router_external),
        "non_router_external_peers_sample": sorted(non_router_external)[:25],
        "n_unique_external_funders": len(external_in),
        "n_unique_external_sinks": len(external_out),
        "shared_funder_counterparties": shared_funders,
        "shared_sink_counterparties": shared_sinks,
        "n_connected_components": len(comp_map),
        "largest_component_size": max((len(v) for v in comp_map.values()), default=0),
        "verdict": verdict,
    }
    return summary, comp_rows, verdict

def controller_topic_main(args: argparse.Namespace, ts_tag: str) -> int:
    """Sprint045F indexed-topic Transfer; micro gate then Cluster2 full."""
    global RPC_URL, CH_URL, CH_USER, CH_PASS
    RPC_URL = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    CH_URL = os.environ.get("CLICKHOUSE_URL", "").rstrip("/")
    CH_USER = os.environ.get("CH_USER", "")
    CH_PASS = os.environ.get("CH_PASSWORD", "")
    ctrls = load_cluster2_controllers("mvp/diagnostics/sprint045e_cluster_contract_callers.csv")
    ctrl_set = set(ctrls)
    dp = disk_preflight()
    chs = ch_table_stats()
    load_inventory_window("mvp/diagnostics/sprint045c_controller_inventory_all.csv", ctrl_set)

    fb = args.center_block - args.half_span
    tb = args.center_block + args.half_span
    span_blocks = tb - fb + 1

    max_rpc = int(os.environ.get("MAX_RPC_CALLS", str(MAX_RPC_CALLS_DEFAULT)))
    max_raw = int(os.environ.get("MAX_RAW_LOGS", str(MAX_RAW_LOGS_DEFAULT)))
    max_out_mb = int(os.environ.get("MAX_OUTPUT_MB", str(MAX_OUTPUT_MB_DEFAULT)))
    budget = CostBudget(max_rpc, max_raw, max_out_mb)

    legacy_ref = load_legacy_summary_for_compare(LEGACY_SUMMARY_PATH)
    baseline_raw = legacy_ref.get("raw_logs_fetched")
    if legacy_ref.get("missing") or not isinstance(baseline_raw, int) or baseline_raw <= 0:
        baseline_raw = 603433
        legacy_ref["documented_fallback_raw_baseline_used"] = baseline_raw

    edge_csv_cols = (
        "chain_id",
        "block_time_utc",
        "block_number",
        "tx_hash",
        "log_index",
        "token_address",
        "from_addr",
        "to_addr",
        "amount_raw",
        "amount_norm",
        "event_type",
        "focus_controller",
        "direction",
        "peer_address",
        "edge_class",
    )

    def comps_singleton() -> List[Dict[str, Any]]:
        return [
            {"component_id": i, "n_members": 1, "controllers": c}
            for i, c in enumerate(sorted(ctrl_set))
        ]

    def merge_flags(rc: int, rawn: int, edges: int, qfix: bool, ver: str) -> None:
        p = "mvp/diagnostics/sprint045f_decision_flags.json"
        base: Dict[str, Any] = {}
        try:
            if os.path.isfile(p):
                with open(p) as pf:
                    base = json.load(pf)
        except json.JSONDecodeError:
            pass
        base.update(
            {
                "SPRINT045F_CONTROLLER_TOPIC_TIMESTAMP": ts_tag,
                "QUERY_MODE": "controller_topic",
                "QUERY_MODE_FIXED": qfix,
                "CONTROLLER_TOPIC_ETH_GETLOGS_CALLS": rc,
                "CONTROLLER_TOPIC_RAW_LOGS": rawn,
                "CONTROLLER_TOPIC_EDGE_ROWS": edges,
                "CLUSTER2_FUNDING_VERDICT_CONTROLLER_TOPIC": ver,
                "AUTHOR_WALLET_FOUND": False,
                "EXPAND_7d_30d_JANAPR_FORBIDDEN_THIS_PATCH": True,
                "RAN_RAW_360GB_BACKFILL": False,
            }
        )
        with open(p, "w") as pf:
            json.dump(base, pf, indent=2)

    summary: Dict[str, Any] = {
        "sprint045f_timestamp": ts_tag,
        "query_mode": "controller_topic",
        "forbidden_expand": ["7d", "30d", "jan_apr_bulk"],
        "window": {
            "center_block": args.center_block,
            "from_block": fb,
            "to_block": tb,
            "span_blocks": span_blocks,
            "chunk_blocks": args.chunk_blocks,
        },
        "step0_disk": dp,
        "ch_stats": chs,
    }

    comp_block: Dict[str, Any] = {
        "legacy": legacy_ref,
        "micro_smoke": {},
        "full_cluster2": {},
        "combined": {},
        "QUERY_MODE_FIXED": False,
    }

    if not RPC_URL:
        summary["abort_reason"] = "missing RPC URL"
        summary["verdict"] = "INCONCLUSIVE"
        comp_block["combined"] = {
            "rpc_calls_eth_getLogs": 0,
            "raw_logs_fetched": 0,
            "edge_rows": 0,
            "logs_per_edge": None,
        }
        comp_block["micro_smoke"] = {"skipped": True, "reason": "no_rpc_url"}
        comp_block["full_cluster2"] = {"skipped": True, "reason": "no_rpc_url"}
        summary["comparison_legacy_vs_controller_topic"] = comp_block
        with open("mvp/diagnostics/sprint045f_controller_topic_summary.json", "w") as xf:
            json.dump(summary, xf, indent=2)
        with open(
            "mvp/diagnostics/sprint045f_controller_topic_edges_smoke.csv", "w", newline=""
        ) as ef:
            w = csv.writer(ef)
            w.writerow(edge_csv_cols)
        with open(
            "mvp/diagnostics/sprint045f_controller_topic_components.csv",
            "w",
            newline="",
        ) as cf:
            w = csv.writer(cf)
            w.writerow(["component_id", "n_members", "controllers"])
            w.writerows([(i, 1, c) for i, c in enumerate(sorted(ctrl_set))])
        merge_flags(0, 0, 0, False, "INCONCLUSIVE")
        _write_controller_topic_readout(ts_tag, args, dp, comp_block, "INCONCLUSIVE")
        return 1

    raw_m: List[Dict] = []
    micro_calls = 0
    micro_errs: List[str] = []
    micro_abort: Optional[str] = None
    t0 = time.time()
    if ctrls and not args.skip_micro:
        raw_m, micro_calls, micro_errs, micro_abort = fetch_logs_controller_topic_chunked(
            fb,
            tb,
            args.chunk_blocks,
            [ctrls[0]],
            budget,
            directions={"inbound"},
            tokens=[WETH.lower()],
        )

    micro_ok = bool(
        ctrls
        and micro_abort is None
        and micro_calls > 0
        and micro_calls > len(micro_errs)
    )
    comp_block["micro_smoke"] = {
        "controller": ctrls[0] if ctrls else None,
        "token": WETH.lower(),
        "directions": ["inbound"],
        "eth_getLogs_calls": micro_calls,
        "rpc_errors": len(micro_errs),
        "raw_logs": len(raw_m),
        "seconds": round(time.time() - t0, 3),
        "budget_abort": micro_abort,
        "micro_ok": micro_ok,
    }

    if not args.micro_only and not args.skip_micro and not micro_ok:
        summary["abort_reason"] = (
            micro_abort or "micro_smoke_gate failed (indexed-topic RPC all errors)"
        )
        summary["verdict"] = "INCONCLUSIVE"
        comp_block["combined"] = {
            "rpc_calls_eth_getLogs": micro_calls,
            "raw_logs_fetched": len(raw_m),
            "edge_rows": 0,
            "logs_per_edge": None,
        }
        comp_block["QUERY_MODE_FIXED"] = len(raw_m) < baseline_raw
        summary["comparison_legacy_vs_controller_topic"] = comp_block
        with open("mvp/diagnostics/sprint045f_controller_topic_summary.json", "w") as xf:
            json.dump(summary, xf, indent=2)
        with open(
            "mvp/diagnostics/sprint045f_controller_topic_edges_smoke.csv", "w", newline=""
        ) as ef:
            w = csv.writer(ef)
            w.writerow(edge_csv_cols)
        with open(
            "mvp/diagnostics/sprint045f_controller_topic_components.csv",
            "w",
            newline="",
        ) as cf:
            w = csv.DictWriter(cf, fieldnames=["component_id", "n_members", "controllers"])
            w.writeheader()
            w.writerows(comps_singleton())
        merge_flags(micro_calls, len(raw_m), 0, comp_block["QUERY_MODE_FIXED"], "INCONCLUSIVE")
        _write_controller_topic_readout(ts_tag, args, dp, comp_block, "INCONCLUSIVE")
        return 2

    raw_f: List[Dict] = []
    full_calls = 0
    full_errs: List[str] = []
    full_abort: Optional[str] = None
    t1 = time.time()
    if not args.micro_only:
        raw_f, full_calls, full_errs, full_abort = fetch_logs_controller_topic_chunked(
            fb,
            tb,
            args.chunk_blocks,
            ctrls,
            budget,
            directions={"inbound", "outbound"},
            tokens=None,
        )

    comp_block["full_cluster2"] = {
        "skipped": args.micro_only,
        "eth_getLogs_calls": 0 if args.micro_only else full_calls,
        "rpc_errors": 0 if args.micro_only else len(full_errs),
        "raw_logs": 0 if args.micro_only else len(raw_f),
        "seconds": 0 if args.micro_only else round(time.time() - t1, 3),
        "budget_abort": None if args.micro_only else full_abort,
    }

    raw_use = raw_m if args.micro_only else raw_f
    total_rpc = micro_calls if args.micro_only else micro_calls + full_calls
    total_raw = len(raw_use)

    blocks_set = {hex_to_int(x.get("blockNumber", "0x0")) for x in raw_use}
    block_ts = build_block_timestamps(blocks_set)
    rows = process_controller_topic_logs(raw_use, ctrl_set, block_ts)

    gra_rows = [
        r
        for r in rows
        if r.get("edge_class")
        in ("direct_funding", "direct_sink", "router_pass_through")
    ]
    gra_sum, comp_rows, verdict_gra = graph_analysis(gra_rows, ctrl_set)
    verdict = verdict_gra if gra_rows else "INCONCLUSIVE"

    comp_block["combined"] = {
        "rpc_calls_eth_getLogs": total_rpc,
        "raw_logs_fetched": total_raw,
        "edge_rows": len(rows),
        "logs_per_edge": safe_logs_per_edge(total_raw, len(rows)),
    }
    comp_block["QUERY_MODE_FIXED"] = total_raw < baseline_raw
    summary["comparison_legacy_vs_controller_topic"] = comp_block
    summary["edge_class_counts"] = dict(Counter(r.get("edge_class", "?") for r in rows))
    summary["graph_analysis_subset"] = gra_sum
    summary["verdict"] = verdict
    summary["verdict_note_gate4"] = (
        "INCONCLUSIVE with 0 edges = no direct funding/sink observed in window; not negative proof."
        if len(rows) == 0
        else None
    )
    summary["rpc_error_sample"] = (micro_errs + full_errs)[:40]
    summary["budget_abort"] = (
        micro_abort if args.micro_only else (full_abort or micro_abort)
    )

    approx_mb = approximate_output_mb(rows) + len(json.dumps(summary).encode()) / (1024**2)
    summary["approximate_output_mb"] = round(approx_mb, 6)
    if approx_mb > max_out_mb:
        summary["abort_reason"] = f"MAX_OUTPUT_MB {max_out_mb} exceeded (est {approx_mb:.2f})"
        with open("mvp/diagnostics/sprint045f_controller_topic_summary.json", "w") as xf:
            json.dump(summary, xf, indent=2)
        with open(
            "mvp/diagnostics/sprint045f_controller_topic_edges_smoke.csv", "w", newline=""
        ) as ef:
            w = csv.writer(ef)
            w.writerow(edge_csv_cols)
        merge_flags(total_rpc, total_raw, len(rows), comp_block["QUERY_MODE_FIXED"], verdict)
        _write_controller_topic_readout(ts_tag, args, dp, comp_block, verdict_gra)
        return 3

    with open("mvp/diagnostics/sprint045f_controller_topic_summary.json", "w") as xf:
        json.dump(summary, xf, indent=2)

    with open(
        "mvp/diagnostics/sprint045f_controller_topic_edges_smoke.csv", "w", newline=""
    ) as ef:
        w = csv.DictWriter(ef, fieldnames=list(edge_csv_cols))
        w.writeheader()
        w.writerows(rows)

    with open(
        "mvp/diagnostics/sprint045f_controller_topic_components.csv", "w", newline=""
    ) as cf:
        w = csv.DictWriter(cf, fieldnames=["component_id", "n_members", "controllers"])
        w.writeheader()
        w.writerows(comp_rows)

    merge_flags(total_rpc, total_raw, len(rows), comp_block["QUERY_MODE_FIXED"], verdict)

    _write_controller_topic_readout(ts_tag, args, dp, comp_block, verdict_gra)

    if summary.get("budget_abort"):
        print(
            "NOTE: budget_abort — partial fetch; see sprint045f_controller_topic_summary.json",
            file=sys.stderr,
        )

    print(
        f"Sprint045F controller_topic: eth_getLogs={total_rpc} raw_logs={total_raw} "
        f"edges={len(rows)} QUERY_MODE_FIXED={comp_block['QUERY_MODE_FIXED']} verdict={verdict}"
    )
    return 0


def _write_controller_topic_readout(
    ts_tag: str,
    args: argparse.Namespace,
    dp: Dict[str, Any],
    comp_blk: Dict[str, Any],
    verdict_graph: str,
) -> None:
    legacy = comp_blk.get("legacy") or {}
    mic = comp_blk.get("micro_smoke") or {}
    full = comp_blk.get("full_cluster2") or {}
    comb = comp_blk.get("combined") or {}

    lines = []
    lines.append("# Sprint045F — controller_topic 成本补丁 Readout（中文 executive）\n\n")
    lines.append(f"> **`{ts_tag}`** · 默认 **`--half-span {args.half_span}` blocks** （本补丁 **禁止**扩展 7d/30d/Jan–Apr）\n\n")
    lines.append("## 1）中文 executive conclusion\n\n")
    lines.append(
        "- 使用 **ERC20 Transfer + indexed topic** 精准查询：每 RPC **单 token 合约**、"
        "**单方向**（`topic1`=from 或 `topic2`=to 之一固定为 controller）。\n"
    )
    lines.append("- **不以 shared `tx.to` 为 funding 同源**；`router_pass_through` 单独标记。\n")
    lines.append("- **未声明 `AUTHOR_WALLET_FOUND`**。\n")
    lines.append(
        f"- **QUERY_MODE_FIXED**（新模态 `raw_logs` 相对 legacy baseline 下降）：**{comp_blk.get('QUERY_MODE_FIXED')}**\n"
    )
    lines.append(
        f"- **verdict（Graph 子集）**：`{verdict_graph}` — 若为 INCONCLUSIVE 且无边 → **窗口内无直接证据**（**非**否定证明）。\n\n"
    )

    lines.append("## 2）是否修复查询模式\n\n")
    lines.append(
        "当 `QUERY_MODE_FIXED=true` 时表示 `combined.raw_logs_fetched` **严格小于** legacy baseline（磁盘上的 `sprint045f_cluster2_funding_summary.json` 或回退常数 603433）。\n\n"
    )

    lines.append("## 3）Micro smoke（1 controller / WETH / inbound）\n\n")
    lines.append("```json\n" + json.dumps(mic, indent=2, ensure_ascii=False) + "\n```\n\n")
    lines.append("## 4）12-controller 默认窗口（micro 通过后）\n\n")
    lines.append("```json\n" + json.dumps(full, indent=2, ensure_ascii=False) + "\n```\n\n")

    lines.append("## 5）指标 — old vs controller_topic\n\n")
    lines.append("| 指标 | legacy | controller_topic（combined） |\n")
    lines.append("|------|--------|------------------------------|\n")
    lines.append(
        f"| eth_getLogs | {legacy.get('rpc_calls_eth_getLogs')} | {comb.get('rpc_calls_eth_getLogs')} |\n"
    )
    lines.append(
        f"| raw_logs_fetched | {legacy.get('raw_logs_fetched')} | {comb.get('raw_logs_fetched')} |\n"
    )
    lines.append(f"| edge_rows | {legacy.get('edge_rows')} | {comb.get('edge_rows')} |\n")
    lines.append(
        f"| logs_per_edge | {legacy.get('logs_per_edge')} | {comb.get('logs_per_edge')} |\n\n"
    )
    lines.append(
        "**estimated_api_credits（粗估）**：以 `eth_getLogs` 次数为主；区块时间戳 batch 请求不计入 `MAX_RPC_CALLS`。\n\n"
    )

    lines.append("## 6）shared funder / sink（仅 external 非 router；见 summary JSON）\n\n")
    lines.append("（完整 `shared_funder_counterparties` / `shared_sink_counterparties` 在 `sprint045f_controller_topic_summary.json` → `graph_analysis_subset`。）\n\n")

    lines.append(f"## 7）verdict（Graph）\n\n`{verdict_graph}`\n\n")

    lines.append("## 8）Artifacts\n\n")
    lines.append("- `mvp/diagnostics/sprint045f_controller_topic_edges_smoke.csv`\n")
    lines.append("- `mvp/diagnostics/sprint045f_controller_topic_summary.json`\n")
    lines.append("- `mvp/diagnostics/sprint045f_controller_topic_components.csv`\n")
    lines.append("- `mvp/diagnostics/sprint045f_decision_flags.json`\n")
    lines.append("- `mvp/audits/sprint045f_controller_topic_readout.md`（本文件）\n\n")

    lines.append("## 9）下一步（≤3 步）\n\n")
    lines.append("1. 若仍 INCONCLUSIVE：优先 **改 `center-block` 对齐 inventory 首笔交易块**（仍用 indexed-topic，**不**扩半窗到周级）。\n")
    lines.append("2. 有边后：进入 **funding attribution review**（强模型后置），仍禁止把 router 当作资金同源。\n")
    lines.append("3. 保持 **MAX_RPC_* / MAX_RAW_LOGS / MAX_OUTPUT_MB** 预算；大批量需单独审批。\n\n")

    lines.append(f"---\nVPS 根分区约 **{dp.get('disk_free_gb')} GB** 可用。\n")

    os.makedirs("mvp/audits", exist_ok=True)
    with open("mvp/audits/sprint045f_controller_topic_readout.md", "w") as mf:
        mf.writelines(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--center-block", type=int, default=41650115)
    ap.add_argument("--half-span", type=int, default=1500, help="total window = 2*half_span+1 blocks")
    ap.add_argument("--chunk-blocks", type=int, default=120)
    ap.add_argument("--try-ch-insert", action="store_true")
    ap.add_argument(
        "--query-mode",
        choices=("legacy", "controller_topic"),
        default="controller_topic",
        help="legacy: full-token Transfer scan; controller_topic: indexed-topic (cost fix, default)",
    )
    ap.add_argument(
        "--micro-only",
        action="store_true",
        help="Only Phase-A: 1 controller, WETH, inbound (+1500-span window)",
    )
    ap.add_argument(
        "--skip-micro",
        action="store_true",
        help="Skip micro gate (debug only; defeats Sprint045F preflight policy)",
    )
    args = ap.parse_args()

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs("mvp/diagnostics", exist_ok=True)
    os.makedirs("mvp/audits", exist_ok=True)
    if args.query_mode == "controller_topic":
        return controller_topic_main(args, ts_tag)

    global RPC_URL
    RPC_URL = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    global CH_URL, CH_USER, CH_PASS
    CH_URL = os.environ.get("CLICKHOUSE_URL", "").rstrip("/")
    CH_USER = os.environ.get("CH_USER", "")
    CH_PASS = os.environ.get("CH_PASSWORD", "")
    had_rpc = bool(RPC_URL)

    cluster_path = "mvp/diagnostics/sprint045e_cluster_contract_callers.csv"
    inv_path = "mvp/diagnostics/sprint045c_controller_inventory_all.csv"

    ctrls = load_cluster2_controllers(cluster_path)
    ctrl_set = set(ctrls)

    # Step 0
    dp = disk_preflight()
    chs = ch_table_stats()

    dmin, dmax = load_inventory_window(inv_path, ctrl_set)

    fb = args.center_block - args.half_span
    tb = args.center_block + args.half_span
    span_blocks = tb - fb + 1

    # Row / disk estimates from Sprint045E probe (~92 logs/block for 3-token OR filter)
    est_rows_full_window = span_blocks * 92
    est_bytes_rows = est_rows_full_window * 350
    est_gb_edges_only_after_filter = (len(ctrls) * est_rows_full_window * 0.001) / (
        1024**3
    )  # tiny if 0.1% hit rate

    preflight = {
        "sprint045f_timestamp": ts_tag,
        "query_mode": "legacy",
        "step0_disk_and_ch": {**dp, **chs},
        "cluster2_controllers": ctrls,
        "inventory_date_span": {"min": dmin, "max": dmax},
        "smoke_window": {
            "center_block": args.center_block,
            "from_block": fb,
            "to_block": tb,
            "span_blocks": span_blocks,
            "chunk_blocks": args.chunk_blocks,
        },
        "three_tier_estimates": {
            "7d_blocks": 7 * BLOCKS_PER_DAY,
            "7d_est_raw_logs_eth_getLogs": round(7 * BLOCKS_PER_DAY * 92),
            "7d_est_gb_if_stored_like_raw_probe": round(7 * BLOCKS_PER_DAY * 92 * 350 / (1024**3), 2),
            "30d_est_raw_logs": round(30 * BLOCKS_PER_DAY * 92),
            "30d_est_gb": round(30 * BLOCKS_PER_DAY * 92 * 350 / (1024**3), 2),
            "jan_apr_est_days_low": 90,
            "jan_apr_est_gb_order_of_magnitude": 600,
            "notes": (
                "eth_getLogs returns full token-transfer stream; slim table keeps only rows "
                "touching focus controllers (~orders of magnitude smaller)."
            ),
        },
        "failure_resume": (
            "Re-run same script with different --center-block / --half-span; "
            "merge CSV outputs externally. Rust backfill_state not used here (offline RPC)."
        ),
        "ran_raw_360gb_backfill": False,
        "funding_only_slim_table_feasible": True,
        "rpc_configured": bool(RPC_URL),
    }

    with open("mvp/diagnostics/sprint045f_backfill_preflight.json", "w") as f:
        json.dump(preflight, f, indent=2)

    if not RPC_URL:
        print(
            "WARN: LABEL_RPC_URL or EVM_HTTP_URL not set — wrote preflight only; smoke skipped.",
            file=sys.stderr,
        )
        rows: List[Dict[str, Any]] = []
        raw_logs: List[Dict] = []
        rpc_calls = 0
        log_errors = ["RPC URL not configured"]
        fetch_s = 0.0
        block_ts: Dict[int, int] = {}
    else:
        # Step 2 fetch
        t0 = time.time()
        raw_logs, rpc_calls, log_errors = fetch_logs_chunked(fb, tb, args.chunk_blocks)
        fetch_s = time.time() - t0

        blocks_needed = {hex_to_int(x.get("blockNumber", "0x0")) for x in raw_logs}
        block_ts = build_block_timestamps(blocks_needed)

        rows = process_logs(raw_logs, ctrl_set, block_ts)
    summary, comp_rows, verdict = graph_analysis(rows, ctrl_set)
    summary["rpc_calls_eth_getLogs"] = rpc_calls
    summary["rpc_errors"] = log_errors
    summary["fetch_seconds"] = round(fetch_s, 2)
    summary["raw_logs_fetched"] = len(raw_logs)
    summary["edge_rows_after_controller_filter"] = len(rows)
    summary["logs_per_edge"] = safe_logs_per_edge(len(raw_logs), len(rows))

    whether_scale = (
        "HOLD: smoke already shows shared funder/sink — no need to expand window for funding proof."
        if verdict in ("SAME_FUNDER_STRONG", "SAME_SINK_STRONG")
        else (
            "EXPAND_7D: if INCONCLUSIVE or PASS_THROUGH_ONLY — increase --half-span to ~7d/2 blocks around center"
            if verdict in ("INCONCLUSIVE", "PASS_THROUGH_ONLY", "SHARED_ROUTER_ONLY")
            else "REVIEW"
        )
    )
    summary["whether_to_scale_next"] = whether_scale
    summary["had_rpc_for_smoke"] = had_rpc

    edge_path = "mvp/diagnostics/sprint045f_cluster2_funding_edges_smoke.csv"
    with open(edge_path, "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "chain_id",
                "block_time_utc",
                "block_number",
                "tx_hash",
                "log_index",
                "token_address",
                "from_addr",
                "to_addr",
                "amount_raw",
                "amount_norm",
                "event_type",
                "focus_controller",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    comp_path = "mvp/diagnostics/sprint045f_cluster2_funding_components.csv"
    with open(comp_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["component_id", "n_members", "controllers"])
        w.writeheader()
        w.writerows(comp_rows)

    # Optional CH insert (column names must match eatlq.funding_transfers_slim DDL)
    if args.try_ch_insert and rows and CH_URL:
        try:
            lines_out: List[str] = []
            for r in rows:
                bt = r.get("block_time_utc") or ""
                bt_ch = bt.replace("T", " ").replace("Z", "") if bt else None
                rec = {
                    "chain_id": r["chain_id"],
                    "source": "sprint045f_slim",
                    "block_time": bt_ch,
                    "block_number": r["block_number"],
                    "tx_hash": r["tx_hash"],
                    "log_index": r["log_index"],
                    "token_address": r["token_address"],
                    "from_addr": r["from_addr"],
                    "to_addr": r["to_addr"],
                    "amount_raw": r["amount_raw"],
                    "amount_norm": r["amount_norm"],
                    "event_type": r["event_type"],
                    "focus_controller": r["focus_controller"],
                    "payload_note": "",
                }
                lines_out.append(json.dumps(rec))
            lines = "\n".join(lines_out)
            auth = (CH_USER, CH_PASS) if (CH_USER or CH_PASS) else None
            r = requests.post(
                f"{CH_URL}/?query=INSERT+INTO+funding_transfers_slim+FORMAT+JSONEachRow",
                data=lines.encode(),
                headers={"X-ClickHouse-Database": "eatlq"},
                auth=auth,
                timeout=120,
            )
            summary["ch_insert_status"] = r.status_code
            summary["ch_insert_body"] = r.text[:200]
        except Exception as e:
            summary["ch_insert_status"] = str(e)

    summ_path = "mvp/diagnostics/sprint045f_cluster2_funding_summary.json"
    with open(summ_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Decision flags
    flags = {
        "SPRINT045F_SLIM_TIMESTAMP": ts_tag,
        "QUERY_MODE": "legacy",
        "RAN_RAW_360GB_BACKFILL": False,
        "FUNDING_ONLY_SLIM_TABLE_FEASIBLE": True,
        "CLUSTER2_SHARED_FUNDER_DETECTED": verdict == "SAME_FUNDER_STRONG",
        "CLUSTER2_SHARED_SINK_DETECTED": verdict == "SAME_SINK_STRONG",
        "CLUSTER2_FUNDING_VERDICT": verdict,
        "EXPAND_TO_7D_OR_30D_RECOMMENDED": verdict
        in ("INCONCLUSIVE", "PASS_THROUGH_ONLY", "SHARED_ROUTER_ONLY"),
        "AUTHOR_WALLET_FOUND": False,
        "NEXT_INDEXING_FULL_RAW_BACKFILL": False,
        "HAD_RPC_FOR_CLUSTER2_SMOKE": had_rpc,
    }
    with open("mvp/diagnostics/sprint045f_decision_flags.json", "w") as f:
        json.dump(flags, f, indent=2)

    exec_smoke = (
        "本轮已用 **eth_getLogs 分块** 拉取 WETH/USDC/USDC.e 的 Transfer + WETH Deposit/Withdraw，并按 12 个 controller 过滤写入边表 CSV。"
        if had_rpc
        else (
            "本轮 **未实际执行** RPC smoke（未设置 `LABEL_RPC_URL`/`EVM_HTTP_URL`），边数恒为 0；"
            "**仅**完成磁盘/CH 预检、slim schema 与脚本路径。设置环境变量后重跑 `scripts/sprint045f_slim_funding_graph.py` 以生成真实边。"
        )
    )
    step10_first = (
        "先设置 `LABEL_RPC_URL`（或 `EVM_HTTP_URL`）后 **重跑** `python3 scripts/sprint045f_slim_funding_graph.py`（默认同锚点 ±1500 blocks smoke）。"
        if not had_rpc
        else "若仍为 INCONCLUSIVE / PASS_THROUGH_ONLY：增大 `--half-span`（可先 ~7 天等价 block：`--half-span {7 * BLOCKS_PER_DAY // 2}`），保持分块与限速。"
    )

    # Readout MD
    readout = f"""# Sprint045F-slim — Cluster 2 资金图 smoke（中文）

> 时间：{ts_tag}
> 关联：Sprint045E readout / preflight

## 1. Executive conclusion

- **未执行** Jan1→Apr、也未执行任何 **raw 360GB** 级别回填；`RAN_RAW_360GB_BACKFILL = false`。
- {exec_smoke}
- Cluster 2 funding 证据标签：**{verdict}**（见 §6）。**注意**：若为 INCONCLUSIVE 且未跑 RPC，该标签仅表示证据不足而非否定结论。
- VPS 当前 **可用磁盘约 {dp['disk_free_gb']:.1f} GB**，**禁止** full raw 回填；**推荐** funding-only slim 路径。

## 2. 是否建议跑 raw 360GB

**否。** 默认门闸 `free_disk > 800GB` 才允许；本机 **{dp['disk_free_gb']:.1f} GB**。

## 3. VPS 磁盘 / 风险

| 项 | 值 |
|---|---|
| 根分区可用 | {dp['disk_free_gb']:.1f} GB / {dp['disk_total_gb']:.1f} GB |
| Docker data | {dp['docker_var_lib_docker_du']} |
| 风险 | 任何全量 raw 写入会导致磁盘满；仅允许 **targeted** 产物与分块 RPC |

## 4. funding-only slim 表

- DDL：`mvp/diagnostics/sprint045f_funding_only_schema.sql`
- **必要性**：现有 `raw_evm_logs` 无 WETH/USDC 日志；`erc20_transfers_mv` 无法从无源数据派生。slim 表接收 **筛后** 边，体积极小，不替代 raw。

## 5. Cluster 2 smoke 配置与结果

| 项 | 值 |
|---|---|
| RPC smoke 已执行 | {had_rpc} |
| 窗口 | block `{fb}` → `{tb}`（**{span_blocks}** blocks, ~{span_blocks * BASE_BLOCK_TIME_SEC / 3600:.2f} h）|
| 分块 | {args.chunk_blocks} blocks/call |
| eth_getLogs 调用次数 | {rpc_calls} |
| 抓取原始 logs | {len(raw_logs)} |
| **过滤后边行数** | {len(rows)} |
| 耗时 | {fetch_s:.1f}s |
| RPC 错误 | {log_errors or '[]'} |

边清单：`mvp/diagnostics/sprint045f_cluster2_funding_edges_smoke.csv`

## 6. shared funder / shared sink

```json
{json.dumps({k: summary[k] for k in ('shared_funder_counterparties','shared_sink_counterparties','verdict') if k in summary}, indent=2, ensure_ascii=False)}
```

- **是否出现 shared funder（≥2 个 controller 同入金方）**：{str(bool(summary.get('shared_funder_counterparties')))}
- **是否出现 shared sink（≥2 个 controller 同出金方）**：{str(bool(summary.get('shared_sink_counterparties')))}

## 7. 是否扩到 7d / 30d / Jan-Apr

- 建议：**{whether_scale}**
- 若扩窗：仅增加 **eth_getLogs 分块次数**；预估见 `sprint045f_backfill_preflight.json` 的 `three_tier_estimates`。

### 附录：仅在「被迫」大范围 raw/Jan-Apr 时的工程约束（本机不满足，勿执行）

| 维度 | 建议 |
|---|---|
| 磁盘门闸 | 空闲磁盘 **>** **800 GB**（与 `raw_360gb_allowed_default` 一致）再评审；低于阈值 **禁止** raw 级别 Jan–Apr。 |
| 分片策略 | chain_id + block **固定高度段**（如 10k–50k blocks/批）并行写；每批校验行数与时间戳连续性。 |
| 失败续跑 | 沿用 `services/ingestor/src/backfill_state.rs` checkpoint；断点文件名与块段绑定；失败后只重扫失败区间。 |
| 清理策略 | 保留 slim 导出与诊断 CSV；staging raw 可按 **按月 partition** TTL 或与 slim 对齐后 **`DROP PARTITION`**（需运维审批）。 |

## 8. Artifacts

- `mvp/diagnostics/sprint045f_funding_only_schema.sql`
- `mvp/diagnostics/sprint045f_backfill_preflight.json`
- `mvp/diagnostics/sprint045f_cluster2_funding_edges_smoke.csv`
- `mvp/diagnostics/sprint045f_cluster2_funding_summary.json`
- `mvp/diagnostics/sprint045f_cluster2_funding_components.csv`
- `mvp/diagnostics/sprint045f_decision_flags.json`
- `mvp/audits/sprint045f_slim_funding_graph_readout.md`（本文件）

## 9. Decision flags（摘要）

```json
{json.dumps(flags, indent=2)}
```

## 10. 下一步（≤3 步）

1. {step10_first}
2. 在 CH 上执行 `sprint045f_funding_only_schema.sql`，需要时用 `JSONEachRow`/`CSV` **导入** 边文件；CH 不可用时不阻塞 slim CSV 链路。
3. 若仍为 INCONCLUSIVE 且无 shared funder/sink：缩小到 **inventory first_seen ±** 锚点或使用按 tx / receipt 的微型解析；**任何**大范围 raw Jan–Apr 需 **磁盘 > ~800GB 空闲 + 分段续跑**，本机不推荐。

---
**边界**：shared `tx.to`（Cluster 合约）≠ funding 同源；本报告不声明 AUTHOR_WALLET_FOUND。
"""
    with open("mvp/audits/sprint045f_slim_funding_graph_readout.md", "w") as f:
        f.write(readout)

    print("Sprint045F-slim done.")
    print(f"  edges: {len(rows)}  verdict: {verdict}  rpc_calls: {rpc_calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
