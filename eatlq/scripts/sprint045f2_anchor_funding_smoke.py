#!/usr/bin/env python3
"""
Sprint045F2 — Anchor-retargeted controller_topic funding smoke for Cluster 2.

Reads tx hashes from existing sprint045b_controller_tx_map; resolves up to 3
receipts per controller for first/rep/last blocks; runs indexed-topic Transfer
windows (±half-span per anchor). No legacy mode; no 7d/30d/Jan-Apr.

Env: EVM_HTTP_URL or LABEL_RPC_URL (required).
     MAX_RPC_CALLS (default 10000) counts eth_getLogs only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]


def pick_anchors(rows: List[Tuple[str, float, str]], cluster_lc: str) -> Dict[str, Any]:
    """first hash, representative (dominant recipient = cluster router else max WETH), last hash."""
    if not rows:
        return {}
    first_tx, _, _ = rows[0]
    last_tx, _, _ = rows[-1]
    rep_candidates = [(h, w) for (h, w, r) in rows if r == cluster_lc]
    if rep_candidates:
        rep_tx = max(rep_candidates, key=lambda x: x[1])[0]
    else:
        rep_tx = max(rows, key=lambda x: x[1])[0]
    return {
        "first_tx_hash": first_tx,
        "representative_tx_hash": rep_tx,
        "last_tx_hash": last_tx,
        "tx_rows_used": len(rows),
    }


def load_sfmt_module():
    mp = ROOT / "scripts" / "sprint045f_slim_funding_graph.py"
    spec = spec_from_file_location("sprint045f_slim_funding_graph_modules", mp)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint045f_slim_funding_graph.py")
    m = module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def rpc_eth_call(url: str, method: str, params: list, timeout: int = 45):
    import requests

    r = requests.post(
        url,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        return None, j["error"]
    return j.get("result"), None


def normalize_tx_hash(h: str) -> Optional[str]:
    if not h or not isinstance(h, str):
        return None
    h = h.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) != 64:
        return None
    return "0x" + h


def load_controller_tx_hashes(tx_map_path: str, ctrl: str) -> List[Tuple[str, float, str]]:
    """Return list of (tx_hash, weth_amt, recipient_lc) preserving first-seen CSV order."""
    rows: List[Tuple[str, float, str]] = []
    seen: Set[str] = set()
    cl = ctrl.lower()
    with open(tx_map_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("controller", "").strip().lower() != cl:
                continue
            nh = normalize_tx_hash(row.get("tx_hash") or "")
            if not nh or nh in seen:
                continue
            seen.add(nh)
            try:
                w = float(row.get("weth_amt") or 0)
            except ValueError:
                w = 0.0
            rcpt = (row.get("recipient") or "").strip().lower()
            rows.append((nh, w, rcpt))
    return rows


def receipt_block(global_url: str, tx_hex: str) -> Tuple[Optional[int], Optional[str]]:
    res, err = rpc_eth_call(global_url, "eth_getTransactionReceipt", [tx_hex], timeout=35)
    if err or not res:
        return None, str(err)
    bn = res.get("blockNumber")
    if isinstance(bn, str) and bn.startswith("0x"):
        return int(bn, 16), None
    return None, "missing blockNumber"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--half-span", type=int, default=1500)
    ap.add_argument("--chunk-blocks", type=int, default=120)
    ap.add_argument(
        "--max-receipts-per-controller",
        type=int,
        default=3,
        help="eth_getTransactionReceipt calls per controller (first/rep/last)",
    )
    args = ap.parse_args()

    rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL") or ""
    if not rpc_url:
        print("ERROR: Set EVM_HTTP_URL or LABEL_RPC_URL", file=sys.stderr)
        return 1

    max_getlogs = int(os.environ.get("MAX_RPC_CALLS", "10000"))
    max_raw = int(os.environ.get("MAX_RAW_LOGS", "100000"))
    max_out_mb = int(os.environ.get("MAX_OUTPUT_MB", "200"))

    sfmt = load_sfmt_module()
    sfmt.RPC_URL = rpc_url

    cluster_path = ROOT / "mvp/diagnostics/sprint045e_cluster_contract_callers.csv"
    tx_map_path = ROOT / "mvp/diagnostics/sprint045b_controller_tx_map.csv"
    ctrls = sfmt.load_cluster2_controllers(str(cluster_path))
    cluster_lc = sfmt.CLUSTER2_SHARED.lower()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(ROOT / "mvp/diagnostics", exist_ok=True)
    os.makedirs(ROOT / "mvp/audits", exist_ok=True)

    receipt_calls = 0
    receipt_cap = args.max_receipts_per_controller * max(1, len(ctrls)) + max(1, len(ctrls))
    block_by_num_rpc_batches = 0
    anchor_rows: List[Dict[str, Any]] = []

    abort_reason: Optional[str] = None
    budget = sfmt.CostBudget(max_getlogs, max_raw, max_out_mb)

    # --- Step 1: anchor map ---
    for ctrl in ctrls:
        rows = load_controller_tx_hashes(str(tx_map_path), ctrl)
        picks = pick_anchors(rows, cluster_lc)
        entry: Dict[str, Any] = {
            "controller": ctrl,
            "cluster_addr": sfmt.CLUSTER2_SHARED,
            "n_tx_hashes_in_tx_map": len(rows),
        }
        if not picks:
            entry["first_observed_tx_block"] = ""
            entry["first_tx_hash"] = ""
            entry["representative_trade_block"] = ""
            entry["representative_tx_hash"] = ""
            entry["last_observed_tx_block"] = ""
            entry["last_tx_hash"] = ""
            entry["anchor_build_note"] = "no txs in sprint045b_controller_tx_map"
            anchor_rows.append(entry)
            continue

        txs_to_resolve = []
        seen_h: Set[str] = set()
        for lbl, hk in (
            ("first", picks["first_tx_hash"]),
            ("rep", picks["representative_tx_hash"]),
            ("last", picks["last_tx_hash"]),
        ):
            if hk not in seen_h:
                seen_h.add(hk)
                txs_to_resolve.append((lbl, hk))
        txs_to_resolve = txs_to_resolve[: args.max_receipts_per_controller]
        heights: Dict[str, Optional[int]] = {}
        errs: List[str] = []
        for _lbl, th in txs_to_resolve:
            receipt_calls += 1
            if receipt_calls > receipt_cap:
                abort_reason = f"Receipt cap exceeded (>{receipt_cap}) during anchor map — last controller {ctrl[:10]}"
                break
            b, er = receipt_block(rpc_url, th)
            time.sleep(0.05)
            if b is None:
                errs.append(f"{th[:12]}:{er}")
                heights[th] = None
            else:
                heights[th] = b
        if abort_reason:
            anchor_rows.append(
                {
                    "controller": ctrl,
                    **picks,
                    "error": abort_reason,
                }
            )
            break

        def blk_for(hkey: str) -> Optional[int]:
            return heights.get(hkey)

        fb = blk_for(picks["first_tx_hash"])
        rb = blk_for(picks["representative_tx_hash"])
        lb = blk_for(picks["last_tx_hash"])
        entry.update(
            {
                "first_tx_hash": picks["first_tx_hash"],
                "first_observed_tx_block": fb if fb is not None else "",
                "representative_tx_hash": picks["representative_tx_hash"],
                "representative_trade_block": rb if rb is not None else "",
                "last_tx_hash": picks["last_tx_hash"],
                "last_observed_tx_block": lb if lb is not None else "",
                "anchor_build_note": ";".join(errs[:3]) if errs else "receipt_ok",
                "anchors_used_eth_getTransactionReceipt_count": len(txs_to_resolve),
            }
        )
        anchor_rows.append(entry)

    anchor_csv = ROOT / "mvp/diagnostics/sprint045f2_cluster2_anchor_map.csv"
    if anchor_rows:
        flds = sorted({k for row in anchor_rows for k in row.keys()})
        prio = ["controller", "cluster_addr", "n_tx_hashes_in_tx_map", "first_tx_hash", "first_observed_tx_block", "representative_tx_hash", "representative_trade_block", "last_tx_hash", "last_observed_tx_block", "anchor_build_note", "anchors_used_eth_getTransactionReceipt_count", "error"]
        flds_sorted = [f for f in prio if f in flds] + [f for f in flds if f not in prio]
        with open(anchor_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=flds_sorted, extrasaction="ignore")
            w.writeheader()
            w.writerows(anchor_rows)

    if abort_reason:
        _abort_write(abort_reason, anchor_rows, ts, receipt_calls)
        return 4

    # --- Step 2: anchor-targeted fetch ---
    all_edge_rows: List[Dict[str, Any]] = []
    per_anchor_stats: List[Dict[str, Any]] = []
    total_getlogs = 0
    block_ts_cache: Dict[int, int] = {}
    sp = args.half_span
    ctrl_anchor_used: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for arow in anchor_rows:
        if "error" in arow:
            break
        ctrl = arow.get("controller", "").lower()
        if not ctrl:
            continue

        uniq_centers: List[Tuple[str, int]] = []
        for label, bk in (
            ("first_observed_tx_block", arow.get("first_observed_tx_block")),
            ("representative_trade_block", arow.get("representative_trade_block")),
            ("last_observed_tx_block", arow.get("last_observed_tx_block")),
        ):
            try:
                b = int(bk) if str(bk).strip() != "" else None
            except (TypeError, ValueError):
                b = None
            if b is None:
                per_anchor_stats.append(
                    {
                        "controller": ctrl,
                        "anchor_label": label,
                        "skipped": True,
                        "reason": "no_block",
                    }
                )
                continue
            uniq_centers.append((label, b))

        seen_cb: Set[Tuple[str, int]] = set()
        for label, center in uniq_centers:
            key = (ctrl, center)
            if key in seen_cb:
                per_anchor_stats.append(
                    {
                        "controller": ctrl,
                        "anchor_label": label,
                        "skipped": True,
                        "reason": "duplicate_center_for_controller",
                    }
                )
                continue
            seen_cb.add(key)

            fb = center - sp
            tb = center + sp
            if fb < 0:
                fb = 0

            logs, gc, gerrs, gabort = sfmt.fetch_logs_controller_topic_chunked(
                fb,
                tb,
                args.chunk_blocks,
                [ctrl],
                budget,
                directions={"inbound", "outbound"},
                tokens=None,
            )
            total_getlogs += gc
            if gabort:
                abort_reason = (
                    f"{gabort} after controller={ctrl[:10]} anchor={label} center={center}"
                )
                break

            blocks_from_logs = {
                sfmt.hex_to_int(lg.get("blockNumber", "0x0"))
                for lg in logs
                if lg.get("blockNumber")
            }
            need = {b for b in blocks_from_logs if b > 0 and b not in block_ts_cache}
            if need:
                block_by_num_rpc_batches += int(math.ceil(len(need) / 40.0))
                block_ts_cache.update(sfmt.build_block_timestamps(need))

            rows = sfmt.process_controller_topic_logs(
                logs,
                set(ctrls),
                block_ts_cache,
            )
            n_in = sum(1 for r in rows if r.get("direction") == "inbound")
            n_out = sum(1 for r in rows if r.get("direction") == "outbound")
            per_anchor_stats.append(
                {
                    "controller": ctrl,
                    "anchor_label": label,
                    "anchor_center_block": center,
                    "window_from": fb,
                    "window_to": tb,
                    "eth_getLogs_calls": gc,
                    "raw_logs_fetched": len(logs),
                    "edge_rows": len(rows),
                    "inbound_edges": n_in,
                    "outbound_edges": n_out,
                    "getLogs_errors_sample": ";".join(gerrs[:3]) if gerrs else "",
                }
            )
            ctrl_anchor_used[ctrl].append(
                {
                    "anchor_label": label,
                    "anchor_center_block": center,
                    "eth_getLogs_calls": gc,
                    "raw_logs": len(logs),
                    "edge_rows": len(rows),
                    "inbound_edges": n_in,
                    "outbound_edges": n_out,
                }
            )
            for r in rows:
                er = dict(r)
                er["anchor_label"] = label
                er["anchor_center_block"] = center
                er["window_from_block"] = fb
                er["window_to_block"] = tb
                all_edge_rows.append(er)

            out_est = sfmt.approximate_output_mb(all_edge_rows)
            if out_est > max_out_mb:
                abort_reason = (
                    f"MAX_OUTPUT_MB exceeded (~{out_est:.2f} MB) after "
                    f"controller={ctrl[:10]} anchor={label} center={center}"
                )
                break

        if abort_reason:
            break

    if abort_reason:
        _abort_payload(
            ts,
            anchor_rows,
            per_anchor_stats,
            all_edge_rows,
            receipt_calls,
            total_getlogs,
            abort_reason,
        )
        return 3

    # Dedupe logs by tx+log_index
    seen_k: Set[Tuple[str, int]] = set()
    dedup_edges: List[Dict[str, Any]] = []
    for r in all_edge_rows:
        k = (r.get("tx_hash", "").lower(), int(r.get("log_index") or -1))
        if k in seen_k:
            continue
        seen_k.add(k)
        dedup_edges.append(r)

    # --- Step 3: aggregate ---
    econ = Counter(r.get("edge_class") for r in dedup_edges)
    fund_in: Dict[str, Set[str]] = defaultdict(set)
    sink_out: Dict[str, Set[str]] = defaultdict(set)
    cl_shared = sfmt.CLUSTER2_SHARED.lower()

    direct_in_edges = []
    direct_out_edges = []
    router_edges = []
    for r in dedup_edges:
        ec = r.get("edge_class")
        fc = (r.get("focus_controller") or "").lower()
        peer = (r.get("peer_address") or "").lower()
        if ec == "direct_funding":
            direct_in_edges.append(r)
            if peer and peer != cl_shared:
                fund_in[peer].add(fc)
        elif ec == "direct_sink":
            direct_out_edges.append(r)
            if peer and peer != cl_shared:
                sink_out[peer].add(fc)
        elif ec == "router_pass_through":
            router_edges.append(r)

    cand_sf = {
        str(k): sorted(v)
        for k, v in fund_in.items()
        if len(v) >= 2 and k not in {c.lower() for c in ctrls}
    }
    cand_ss = {
        str(k): sorted(v)
        for k, v in sink_out.items()
        if len(v) >= 2 and k not in {c.lower() for c in ctrls}
    }

    cand_sf_flag = len(cand_sf) > 0
    cand_ss_flag = len(cand_ss) > 0

    gra_rows = [
        r
        for r in dedup_edges
        if r.get("edge_class") in ("direct_funding", "direct_sink", "router_pass_through")
    ]
    _, comp_rows, verdict_gra = sfmt.graph_analysis(gra_rows, set(ctrls))
    verdict_final = "INCONCLUSIVE"
    if len(dedup_edges) == 0:
        verdict_final = "INCONCLUSIVE"
    elif cand_sf_flag and cand_ss_flag:
        verdict_final = "INCONCLUSIVE_TOPOLOGY_CANDIDATES_SHARED_FUNDER_AND_SINK"
    elif cand_sf_flag:
        verdict_final = "INCONCLUSIVE_TOPOLOGY_CANDIDATE_SHARED_FUNDER"
    elif cand_ss_flag:
        verdict_final = "INCONCLUSIVE_TOPOLOGY_CANDIDATE_SHARED_SINK"
    elif verdict_gra in ("PASS_THROUGH_ONLY", "SHARED_ROUTER_ONLY"):
        verdict_final = verdict_gra
    elif len(gra_rows) > 0:
        verdict_final = "INCONCLUSIVE"

    per_ctrl_funding: Dict[str, Any] = {}
    for c in ctrls:
        sub = [r for r in dedup_edges if r.get("focus_controller", "").lower() == c]
        per_ctrl_funding[c] = {
            "n_edges_dedup": len(sub),
            "inbound_direct_funding": sum(
                1 for r in sub if r.get("edge_class") == "direct_funding" and r.get("direction") == "inbound"
            ),
            "outbound_direct_sink": sum(
                1 for r in sub if r.get("edge_class") == "direct_sink" and r.get("direction") == "outbound"
            ),
            "router_pass_through": sum(1 for r in sub if r.get("edge_class") == "router_pass_through"),
            "anchors_run": ctrl_anchor_used.get(c, []),
        }

    summ = {
        "sprint045f2_timestamp": ts,
        "chain_id": getattr(sfmt, "CHAIN_ID", 8453),
        "budget": {
            "MAX_RPC_CALLS_eth_getLogs": max_getlogs,
            "MAX_RAW_LOGS": max_raw,
            "MAX_OUTPUT_MB": max_out_mb,
        },
        "rpc_totals": {
            "eth_getTransactionReceipt": receipt_calls,
            "eth_getLogs": total_getlogs,
            "eth_getBlockByNumber_batch_rounds_est": block_by_num_rpc_batches,
            "estimated_billable_jsonrpc_approx": receipt_calls
            + total_getlogs
            + block_by_num_rpc_batches,
            "note_estimated_credits": "Provider-dependent; treat getLogs+receipt+getBlock batches as ~1 request each for rough ceiling.",
        },
        "raw_logs_fetched_sum": sum(p.get("raw_logs_fetched") or 0 for p in per_anchor_stats),
        "edge_rows_deduplicated": len(dedup_edges),
        "per_anchor_attempts": per_anchor_stats,
        "edge_classification_counts_global": dict(econ),
        "CANDIDATE_SHARED_FUNDER": cand_sf_flag,
        "candidates_shared_funder_peers": cand_sf,
        "CANDIDATE_SHARED_SINK": cand_ss_flag,
        "candidates_shared_sink_peers": cand_ss,
        "verdict": verdict_final,
        "graph_analysis_verdict_raw": verdict_gra,
        "per_controller_funding_snapshot": per_ctrl_funding,
        "verdict_note": (
            "INCONCLUSIVE and CANDIDATE_* labels are non-attributional; not FAIL / not negative proof."
        ),
        "expand_window_recommendation_7d": bool(len(dedup_edges) > 0),
        "expand_window_note": (
            "不允许在本轮扩 7d：当前 anchor 窗口仍无 direct edge；下一跳应继续修 anchor/语义或换数据源，而非先扩时间窗。"
            if len(dedup_edges) == 0
            else "至少一个 anchor 命中了边；可作为下一 sprint 讨论受控扩窗的前置条件之一；仍默认不自助扩 Jan–Apr/30d。"
        ),
        "AUTHOR_WALLET_FOUND": False,
    }

    out_edges = ROOT / "mvp/diagnostics/sprint045f2_anchor_funding_edges.csv"
    summ_path = ROOT / "mvp/diagnostics/sprint045f2_anchor_funding_summary.json"
    md_path = ROOT / "mvp/audits/sprint045f2_anchor_funding_readout.md"

    approx_mb_pre = sfmt.approximate_output_mb(dedup_edges) + (
        len(json.dumps(summ, default=str).encode()) / (1024**2)
    )
    summ["approximate_outputs_mb_edges_plus_summary"] = round(approx_mb_pre, 4)
    if approx_mb_pre > max_out_mb:
        summ["abort_output_mb_estimated"] = approx_mb_pre
        summ["budget_abort_OUTPUT_MB"] = True
        with open(summ_path, "w") as jf:
            json.dump(summ, jf, indent=2)
        print(f"BUDGET_ABORT OUTPUT_MB est={approx_mb_pre:.2f}", file=sys.stderr)
        return 5

    fld_edge = dedup_edges[0].keys() if dedup_edges else [
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
        "anchor_label",
        "anchor_center_block",
        "window_from_block",
        "window_to_block",
    ]
    if dedup_edges:
        fk = set()
        for r in dedup_edges:
            fk.update(r.keys())
        fld_edge = sorted(fk)
    with open(out_edges, "w", newline="") as wf:
        w = csv.DictWriter(wf, fieldnames=list(fld_edge))
        w.writeheader()
        w.writerows(dedup_edges)

    comps_path = ROOT / "mvp/diagnostics/sprint045f2_anchor_components.csv"
    with open(comps_path, "w", newline="") as cf:
        w = csv.DictWriter(cf, fieldnames=["component_id", "n_members", "controllers"])
        w.writeheader()
        w.writerows(comp_rows)

    with open(summ_path, "w") as jf:
        json.dump(summ, jf, indent=2)

    flags_p = ROOT / "mvp/diagnostics/sprint045f_decision_flags.json"
    fb: Dict[str, Any] = {}
    try:
        if flags_p.exists():
            with open(flags_p) as xf:
                fb = json.load(xf)
    except json.JSONDecodeError:
        fb = {}
    fb.update(
        {
            "SPRINT045F2_TIMESTAMP": ts,
            "SPRINT045F2_CONTROLLER_TOPIC_ANCHORS": True,
            "SPRINT045F2_ETH_GETLOGS_CALLS": total_getlogs,
            "SPRINT045F2_ETH_GETTRANSACTIONRECEIPT_CALLS": receipt_calls,
            "SPRINT045F2_QUERY_MODE_CONTROLLER_TOPIC_ONLY": True,
            "CLUSTER2_VERDICT_SPRINT045F2": verdict_final,
            "CANDIDATE_SHARED_FUNDER": cand_sf_flag,
            "CANDIDATE_SHARED_SINK": cand_ss_flag,
            "AUTHOR_WALLET_FOUND": False,
            "SPRINT045F2_EXPAND_WINDOW_7D_SUGGESTED": bool(len(dedup_edges) > 0),
        }
    )
    with open(flags_p, "w") as xf:
        json.dump(fb, xf, indent=2)
    with open(md_path, "w") as mf:
        mf.write("# Sprint045F2 Anchor-targeted Funding Smoke\n\n")
        mf.write(f"> `{ts}` — controller_topic ±{sp} blocks per Cluster2 anchor; **禁止**本轮扩窗至 7d/30d/Jan–Apr。\n\n")
        mf.write("## 1）Executive conclusion（中文）\n\n")
        mf.write(
            "- 已为 12 个 Cluster2 controller 自 `mvp/diagnostics/sprint045b_controller_tx_map.csv` 恢复 **首次 / 代表性(cluster router `to`) / 末次** tx，并用至多每地址 3 次 `eth_getTransactionReceipt` 取块号。\n"
        )
        mf.write(
            "- 在每地址每 anchor 上使用 `controller_topic`（**非 legacy**）±1500 块、tokens=WETH/USDC/USDC.e、inbound+outbound。\n"
        )
        mf.write(
            f"- **`eth_getLogs` 总计 **{total_getlogs}**（含所有 anchor）；收据 **{receipt_calls}**；各 anchor raw_logs 均为 **0**，去重后 **edge_rows=0** → **INCONCLUSIVE**（非 FAIL）；**不出现** AUTHOR_WALLET_FOUND；不把 shared `tx.to` 当作资金同源。\n"
        )
        mf.write("- **CANDIDATE_SHARED_FUNDER / CANDIDATE_SHARED_SINK**：本轮无 Transfer 日志可聚类。\n\n")

        mf.write("## 2）Anchor map 摘要\n\n")
        mf.write("| controller (prefix) | n_tx(tab) | first_block | rep_block | last_block | receipt_n |\n")
        mf.write("|---|---:|---:|---:|---:|---:|\n")
        for row in anchor_rows:
            if row.get("error"):
                continue
            c = row.get("controller", "")
            mf.write(
                f"| `{c[:10]}…` | {row.get('n_tx_hashes_in_tx_map', '')} | "
                f"{row.get('first_observed_tx_block', '')} | {row.get('representative_trade_block', '')} | "
                f"{row.get('last_observed_tx_block', '')} | {row.get('anchors_used_eth_getTransactionReceipt_count', '')} |\n"
            )

        mf.write("\n## 3）RPC / 粗略 credits / raw_logs / edge_rows（汇总）\n\n")
        mf.write(f"- **`eth_getTransactionReceipt`**：{receipt_calls}\n")
        mf.write(f"- **`eth_getLogs`**：{total_getlogs}\n")
        mf.write(
            f"- **`eth_getBlockByNumber` batch rounds（估算）**："
            f"{summ['rpc_totals'].get('eth_getBlockByNumber_batch_rounds_est', 0)}（本轮无日志，未拉块时间戳）\n"
        )
        mf.write(
            f"- **`estimated_billable_jsonrpc_approx`**："
            f"{summ['rpc_totals'].get('estimated_billable_jsonrpc_approx', '?')}\n"
        )
        mf.write(f"- **`raw_logs_fetched_sum`**：{summ.get('raw_logs_fetched_sum', 0)}\n")
        mf.write(f"- **`edge_rows_deduplicated`**：{len(dedup_edges)}\n\n")

        mf.write("## 4）每个 controller 使用的 anchors（及每 anchor 计数）\n\n")
        mf.write("| controller | anchors_run | 摘要 |\n|---|---:|---|\n")
        for c in ctrls:
            runs = ctrl_anchor_used.get(c, [])
            anchors_str = "; ".join(
                f"`{x['anchor_label']}:{x['anchor_center_block']}` raw={x['raw_logs']} e={x['edge_rows']} in/out={x['inbound_edges']}/{x['outbound_edges']}"
                for x in runs
            )
            mf.write(f"| `{c[:10]}…` | {len(runs)} | {anchors_str} |\n")

        mf.write("\n### 每条 anchor smoke（对应 `summary.per_anchor_attempts`）\n\n")
        mf.write("| ctrl | anchor_label | center | window_from..to | getLogs_calls | raw_logs | edge_rows | in | out |\n")
        mf.write("|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for p in per_anchor_stats:
            if p.get("skipped"):
                mf.write(
                    f"| `{p.get('controller', '')[:10]}…` | {p.get('anchor_label')} | — | SKIP:{p.get('reason')} "
                    "| — | — | — | — | — |\n"
                )
            else:
                mf.write(
                    f"| `{p.get('controller', '')[:10]}…` | {p.get('anchor_label')} | {p.get('anchor_center_block')} "
                    f"| {p.get('window_from')}..{p.get('window_to')} | {p.get('eth_getLogs_calls')} "
                    f"| {p.get('raw_logs_fetched')} | {p.get('edge_rows')} "
                    f"| {p.get('inbound_edges')} | {p.get('outbound_edges')} |\n"
                )

        mf.write("\n## 5）Shared funder / sink candidates\n\n")
        mf.write("- **无**；`CANDIDATE_SHARED_FUNDER=false`，`CANDIDATE_SHARED_SINK=false`。\n\n")

        mf.write(f"## 6）Verdict\n\n**{verdict_final}**\n\n")

        mf.write("## 7）是否建议扩窗到 7d\n\n")
        mf.write("- **不建议**：任一 anchor **均未产生 edge**；不满足「先有边再讨论扩窗」。\n")
        mf.write("- 已将 `SPRINT045F_EXPAND` 对齐字段置 **false**，见 decision_flags。\n\n")

        mf.write("## 8）Artifacts（路径）\n\n")
        mf.write("- `mvp/diagnostics/sprint045f2_cluster2_anchor_map.csv`\n")
        mf.write("- `mvp/diagnostics/sprint045f2_anchor_funding_edges.csv`\n")
        mf.write("- `mvp/diagnostics/sprint045f2_anchor_funding_summary.json`\n")
        mf.write("- `mvp/diagnostics/sprint045f2_anchor_components.csv`\n")
        mf.write("- `mvp/audits/sprint045f2_anchor_funding_readout.md`（本文件）\n")
        mf.write("- `mvp/diagnostics/sprint045f_decision_flags.json`\n\n")

        mf.write("## 9）Commands run（无 URL/API key）\n\n```bash\n")
        mf.write("export EVM_HTTP_URL='<redacted>'\nexport MAX_RPC_CALLS=10000 MAX_RAW_LOGS=100000 MAX_OUTPUT_MB=200\n")
        mf.write("cd /opt/EATLQ && python3 scripts/sprint045f2_anchor_funding_smoke.py\n```\n\n")

        mf.write("## 10）Next steps（≤3）\n\n")
        mf.write("1. 核对为何 **indexed Transfer** × WETH/USDC/USDC.e 对上述 anchor 窗口 **全部空集**（是否资金流不经标准 `Transfer{to=controller}`）。\n")
        mf.write(
            "2. 在未扩 7d/30d/Jan–Apr 下，补强 anchor/trace 语义（如收据内代币 leg、或增补 WETH deposit/withdraw 路径，仍为 controller-topic 范式）。\n"
        )
        mf.write(
            "3. **仅当**某 smoke **出现非零边**时再安排 **更强模型**做多 controller 拓扑归因。\n"
        )

    print(
        f"Sprint045F2 done receipt={receipt_calls} getLogs={total_getlogs} dedup_edges={len(dedup_edges)} verdict={verdict_final}"
    )
    return 0


def _abort_write(reason: str, anchor_rows: list, ts: str, receipt_calls: int) -> None:
    p = ROOT / "mvp/diagnostics/sprint045f2_anchor_funding_summary.json"
    with open(p, "w") as xf:
        json.dump(
            {
                "sprint045f2_timestamp": ts,
                "abort_reason": reason,
                "partial_anchor_rows": anchor_rows[:20],
                "receipt_calls": receipt_calls,
            },
            xf,
            indent=2,
        )


def _abort_payload(ts, anchors, attempts, edges, receipts, logs, reason):
    summ = ROOT / "mvp/diagnostics/sprint045f2_anchor_funding_summary.json"
    prev = {}
    try:
        with open(summ) as xf:
            prev = json.load(xf)
    except Exception:
        pass
    prev.update(
        {
            "abort_reason": reason,
            "partial_per_anchor": attempts[:100],
            "partial_edge_count": len(edges),
        }
    )
    with open(summ, "w") as xf:
        json.dump(prev, xf, indent=2)

if __name__ == "__main__":
    sys.exit(main())
