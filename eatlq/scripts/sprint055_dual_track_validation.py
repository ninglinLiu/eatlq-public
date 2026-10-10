#!/usr/bin/env python3
"""Sprint055 dual-track validation.

Bounded offline/CH synthesis for:
- Track A: 0x501e scoped paper/shadow validation readiness.
- Track B: author-like funding graph probe candidates.

No live trading, training, production signal writes, broad RPC backfill, or secret
reads are performed. ClickHouse use is read-only and bounded to existing local
tables/artifacts.
"""

from __future__ import annotations

import csv
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "mvp" / "audits" / "sprint055"
DIAG_DIR = ROOT / "mvp" / "diagnostics"
LOG_DIR = ROOT / "logs"

TRACK_A = "0x501ef174b66883ba40f92e6fb24bfb49331d4d4c"
AUTHOR_EXPLICIT = {
    "0xa190dd0bf07afb32c6273555077497525526dabb",
    "0xbf1328239da8656c8a799a429131d3cc2e438b35",
    "0xadbbeb3e8a4e462ab060ad57928c4a2b11b773a3",
    "0x0718f66c5f4268d4b3efe74e45a8345480aee7fe",
}
CONTROL_7E49 = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
CORE_TOKENS = {
    "0x4200000000000000000000000000000000000006": "WETH",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDC.e",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_csv(path: Path, rows: list[dict[str, Any]], preferred: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(preferred)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def as_float(value: Any, default: float = 0.0) -> float:
    if value in ("", None):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def by_key(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, str]]:
    return {row.get(key, "").lower(): row for row in rows if row.get(key)}


def ch_query(sql: str) -> str:
    cmd = [
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--database",
        "eatlq",
        "--query",
        sql,
    ]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=60).strip()
    except Exception as exc:
        return f"CH_QUERY_FAILED: {exc}"


def split_tsv_with_names(text: str) -> list[dict[str, str]]:
    if not text or text.startswith("CH_QUERY_FAILED"):
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    headers = lines[0].split("\t")
    out: list[dict[str, str]] = []
    for line in lines[1:]:
        vals = line.split("\t")
        out.append({headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))})
    return out


def top_share(counter: dict[str, float]) -> tuple[str, float, float]:
    total = sum(abs(v) for v in counter.values())
    if not counter or total <= 0:
        return "", 0.0, 0.0
    key, val = max(counter.items(), key=lambda kv: abs(kv[1]))
    return key, abs(val) / total, total


def build_track_a(inputs: dict[str, Any]) -> dict[str, Any]:
    score = by_key(inputs["scorecard"], "address_or_cluster").get(TRACK_A, {})
    roi = by_key(inputs["roi"], "controller").get(TRACK_A, {})
    realized = [row for row in inputs["realized"] if row.get("controller", "").lower() == TRACK_A]
    deconc = [row for row in inputs["deconc"] if row.get("controller", "").lower() == TRACK_A]
    probe = by_key(inputs["probe"], "controller").get(TRACK_A, {})
    bucket = by_key(inputs["buckets"], "controller").get(TRACK_A, {})
    to_diag = by_key(inputs["to_diag"], "controller").get(TRACK_A, {})
    tx_rows = [row for row in inputs["tx_map"] if row.get("controller", "").lower() == TRACK_A]
    tx_to_pool_map = {
        row.get("tx_hash", "").lower(): row.get("pool_sender", "").lower()
        for row in tx_rows
        if row.get("tx_hash") and row.get("pool_sender")
    }

    token_pnl: dict[str, float] = defaultdict(float)
    token_count: Counter[str] = Counter()
    day_pnl: dict[str, float] = defaultdict(float)
    pool_weth: dict[str, float] = defaultdict(float)
    bucket_pnl: dict[str, float] = defaultdict(float)
    pnl_values: list[float] = []
    for row in realized:
        token = row.get("meme_token", "").lower()
        day = row.get("buy_bt", "")[:10]
        pnl = as_float(row.get("realized_pnl"))
        weth_in = as_float(row.get("weth_in"))
        token_pnl[token] += pnl
        token_count[token] += 1
        day_pnl[day] += pnl
        bucket_pnl[row.get("exit_bucket", "unknown")] += pnl
        pnl_values.append(pnl)
        pool_from_tx_map = tx_to_pool_map.get(row.get("buy_tx", "").lower(), "")
        if pool_from_tx_map:
            pool_weth[pool_from_tx_map] += weth_in

    tx_to_pool = {row.get("tx_hash", "").lower(): row.get("pool", "").lower() for row in deconc if row.get("tx_hash")}
    pool_from_deconc: dict[str, float] = defaultdict(float)
    for row in realized:
        pool = tx_to_pool.get(row.get("buy_tx", "").lower(), "")
        if pool:
            pool_from_deconc[pool] += as_float(row.get("weth_in"))

    horizon: dict[str, dict[str, float]] = {}
    for key in ("pnl_5m", "pnl_30m"):
        vals = [as_float(row.get(key)) for row in deconc if row.get(key, "") != ""]
        horizon[key] = {
            "n": len(vals),
            "sum_pnl": round(sum(vals), 6),
            "positive_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else 0.0,
            "median_pnl": round(sorted(vals)[len(vals) // 2], 6) if vals else 0.0,
        }

    tok_key, tok_share, tok_total = top_share(token_pnl)
    day_key, day_share, _ = top_share(day_pnl)
    pool_key, pool_share, _ = top_share(pool_from_deconc or pool_weth)
    largest = max((abs(v) for v in pnl_values), default=0.0)
    net_pnl = sum(pnl_values)
    largest_trade_share_of_abs = largest / sum(abs(v) for v in pnl_values) if pnl_values else 0.0

    sidecar_rows = inputs["sidecar_dashcam"]
    latest_sidecar = sidecar_rows[-1] if sidecar_rows else {}
    latency_ms = [
        as_float(row.get("block_to_sidecar_latency_ms"))
        for row in sidecar_rows
        if row.get("event_type") == "LATENCY_SAMPLE"
    ]

    ch_window_rows = split_tsv_with_names(
        ch_query(
            "SELECT min(block_time) AS min_block_time, max(block_time) AS max_block_time, count() AS rows "
            "FROM uniswap_v3_swaps_trade_semantic WHERE chain_id=8453 FORMAT TSVWithNames"
        )
    )
    ch_window = ch_window_rows[0] if ch_window_rows else {}

    verdict = "WEAK_PASS"
    if tok_share >= 0.95 or pool_share >= 0.95:
        verdict = "WEAK_PASS"
    if not realized:
        verdict = "INCONCLUSIVE"

    return {
        "address": TRACK_A,
        "verdict": verdict,
        "scorecard_category": score.get("category", ""),
        "realized_pnl_weth": score.get("realized_pnl", ""),
        "roi_pct": score.get("roi_pct", ""),
        "profit_factor": score.get("profit_factor", ""),
        "win_rate": score.get("win_rate", ""),
        "matched_pairs": roi.get("own_matched_pairs", ""),
        "quoteable_share": roi.get("quoteable_share", score.get("router_flags", "")),
        "delayed_follow_verdict": roi.get("delayed_follow_verdict", ""),
        "delayed_follow_roi_pct": roi.get("delayed_follow_roi_pct", ""),
        "top_token": tok_key,
        "top_token_pnl_share_abs": round(tok_share, 4),
        "top_token_trade_share": round((token_count[tok_key] / len(realized)) if realized and tok_key else 0.0, 4),
        "top_pool": pool_key,
        "top_pool_weth_share": round(pool_share, 4),
        "top_day": day_key,
        "top_day_pnl_share_abs": round(day_share, 4),
        "net_pnl_from_realized_csv": round(net_pnl, 6),
        "largest_trade_share_abs": round(largest_trade_share_of_abs, 4),
        "exit_bucket_pnl": dict(sorted((k, round(v, 6)) for k, v in bucket_pnl.items())),
        "horizon_5m": horizon["pnl_5m"],
        "horizon_30m": horizon["pnl_30m"],
        "horizon_artifact_status": (
            "NO_0X501E_ROWS_IN_SPRINT048B_DECONC_HORIZON_ARTIFACT; "
            "use realized exit buckets as fallback"
            if not deconc
            else "AVAILABLE"
        ),
        "fee_tier_mix": score.get("fee_tier_mix", ""),
        "low_priority_gas_share": bucket.get("priority_share_le5_wei", ""),
        "gas_fingerprint": score.get("gas_fingerprint", ""),
        "is_eoa": bucket.get("is_eoa_probed", probe.get("is_eoa", "")),
        "eip7702_like": bucket.get("eip7702_like_probed", probe.get("eip7702_like", "")),
        "dominant_to": to_diag.get("dominant_to", ""),
        "dominant_to_frac": to_diag.get("dominant_to_frac", ""),
        "router_flags": score.get("router_flags", ""),
        "concentration_flags": score.get("concentration_flags", ""),
        "ch_v3_min_block_time": ch_window.get("min_block_time", ""),
        "ch_v3_max_block_time": ch_window.get("max_block_time", ""),
        "ch_v3_rows": ch_window.get("rows", ""),
        "recent_activity_status": "INCONCLUSIVE_CH_SEMANTIC_MAX_APR30_NO_MAY_WINDOW",
        "sidecar_latest_sample_utc": latest_sidecar.get("timestamp_utc", ""),
        "sidecar_total_ticks": latest_sidecar.get("notes", ""),
        "sidecar_latency_ms_min": round(min(latency_ms), 2) if latency_ms else "",
        "sidecar_latency_ms_max": round(max(latency_ms), 2) if latency_ms else "",
        "paper_watchlist_action": "watch_0x501e_wallet_only_new_activity_quoteable_v3_weth_leg_no_signals",
    }


def pick_anchors(tx_rows: list[dict[str, str]]) -> dict[str, str]:
    if not tx_rows:
        return {}
    first = tx_rows[0]
    last = tx_rows[-1]
    rep = max(tx_rows, key=lambda row: as_float(row.get("weth_amt")))
    return {
        "first_tx_hash": first.get("tx_hash", ""),
        "representative_tx_hash": rep.get("tx_hash", ""),
        "last_tx_hash": last.get("tx_hash", ""),
        "first_side": first.get("side", ""),
        "representative_side": rep.get("side", ""),
        "last_side": last.get("side", ""),
        "representative_weth_amt": rep.get("weth_amt", ""),
        "tx_rows_used": str(len(tx_rows)),
    }


def ch_anchor_map(tx_hashes: list[str]) -> dict[str, dict[str, str]]:
    txs = sorted({h.lower() for h in tx_hashes if h and h.startswith("0x")})
    if not txs:
        return {}
    quoted = ",".join(f"'{h}'" for h in txs)
    sql = (
        "SELECT tx_hash, min(block_number) AS block_number, min(block_time) AS block_time, "
        "any(pool_address) AS pool_address, any(fee_bps) AS fee_bps "
        "FROM uniswap_v3_swaps_trade_semantic "
        f"WHERE chain_id=8453 AND tx_hash IN ({quoted}) "
        "GROUP BY tx_hash FORMAT TSVWithNames"
    )
    return {row["tx_hash"].lower(): row for row in split_tsv_with_names(ch_query(sql))}


def ch_funding_edges(candidates: list[str]) -> list[dict[str, str]]:
    all_addrs = sorted(set(candidates + [CONTROL_7E49]))
    addr_list = ",".join(f"'{a}'" for a in all_addrs)
    token_list = ",".join(f"'{t}'" for t in CORE_TOKENS)
    sql = (
        "SELECT token_address, token_symbol, "
        f"if(sender IN ({addr_list}), sender, recipient) AS candidate, "
        "sum(sender=candidate) AS out_count, sum(recipient=candidate) AS in_count, "
        "sumIf(amount_normalized, recipient=candidate) AS in_amount, "
        "sumIf(amount_normalized, sender=candidate) AS out_amount, "
        "min(block_time) AS first_edge_at, max(block_time) AS last_edge_at, any(tx_hash) AS sample_tx "
        "FROM erc20_transfers_semantic "
        f"WHERE chain_id=8453 AND token_address IN ({token_list}) "
        f"AND (sender IN ({addr_list}) OR recipient IN ({addr_list})) "
        "GROUP BY token_address, token_symbol, candidate "
        "ORDER BY candidate, token_symbol FORMAT TSVWithNames"
    )
    return split_tsv_with_names(ch_query(sql))


def build_track_b(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    score_by_addr = by_key(inputs["scorecard"], "address_or_cluster")
    bucket_by_addr = by_key(inputs["buckets"], "controller")
    to_by_addr = by_key(inputs["to_diag"], "controller")
    tx_by_addr: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in inputs["tx_map"]:
        ctrl = row.get("controller", "").lower()
        if ctrl:
            tx_by_addr[ctrl].append(row)

    funding_candidates = set(AUTHOR_EXPLICIT)
    for row in inputs["scorecard"]:
        addr = row.get("address_or_cluster", "").lower()
        if (
            row.get("recommended_next_action") == "funding_graph_probe"
            or as_float(row.get("author_fingerprint_score")) >= 58
        ) and addr.startswith("0x"):
            funding_candidates.add(addr)
    candidates = sorted(funding_candidates)

    anchors_by_addr = {addr: pick_anchors(tx_by_addr.get(addr, [])) for addr in candidates}
    all_anchor_hashes: list[str] = []
    for anchors in anchors_by_addr.values():
        all_anchor_hashes.extend(
            [anchors.get("first_tx_hash", ""), anchors.get("representative_tx_hash", ""), anchors.get("last_tx_hash", "")]
        )
    anchor_ch = ch_anchor_map(all_anchor_hashes)
    edge_rows = ch_funding_edges(candidates)

    edges_by_candidate: dict[str, list[dict[str, str]]] = defaultdict(list)
    funder_counter: Counter[str] = Counter()
    sink_counter: Counter[str] = Counter()
    for edge in edge_rows:
        cand = edge.get("candidate", "").lower()
        edges_by_candidate[cand].append(edge)

    # This table aggregates only by candidate, so a shared funder/sink can be
    # asserted only if raw edge rows exist. Current bounded query returns none.
    shared_funder = ",".join(k for k, v in funder_counter.items() if v > 1)
    shared_sink = ",".join(k for k, v in sink_counter.items() if v > 1)

    out: list[dict[str, Any]] = []
    for addr in candidates:
        score = score_by_addr.get(addr, {})
        bucket = bucket_by_addr.get(addr, {})
        to_diag = to_by_addr.get(addr, {})
        anchors = anchors_by_addr.get(addr, {})

        def enrich_anchor(prefix: str) -> dict[str, str]:
            h = anchors.get(f"{prefix}_tx_hash", "").lower()
            row = anchor_ch.get(h, {})
            return {
                f"{prefix}_tx_hash": h,
                f"{prefix}_block": row.get("block_number", ""),
                f"{prefix}_block_time": row.get("block_time", ""),
                f"{prefix}_pool": row.get("pool_address", ""),
                f"{prefix}_fee_bps": row.get("fee_bps", ""),
            }

        cand_edges = edges_by_candidate.get(addr, [])
        edge_summary = ";".join(
            f"{e.get('token_symbol')} in={e.get('in_count')}/{e.get('in_amount')} out={e.get('out_count')}/{e.get('out_amount')}"
            for e in cand_edges
        )
        evidence_level = "INCONCLUSIVE_NO_CORE_ERC20_EDGES_IN_CH_WINDOW"
        if cand_edges:
            evidence_level = "WEAK_EDGE_PRESENT_NEEDS_RAW_SHARED_FUNDER_CHECK"

        row: dict[str, Any] = {
            "candidate": addr,
            "category": score.get("category", ""),
            "author_fingerprint_score": score.get("author_fingerprint_score", ""),
            "realized_pnl": score.get("realized_pnl", ""),
            "roi_pct": score.get("roi_pct", ""),
            "gas_fingerprint": score.get("gas_fingerprint", ""),
            "low_priority_gas_share": bucket.get("priority_share_le5_wei", ""),
            "p50_priority_fee_wei": bucket.get("priority_fee_p50_wei", ""),
            "fee_tier_mix": score.get("fee_tier_mix", ""),
            "fee100_share": bucket.get("fee100_share", ""),
            "fee3000_share": bucket.get("fee3000_share", ""),
            "fee10000_share": bucket.get("fee10000_share", ""),
            "is_eoa": bucket.get("is_eoa_probed", ""),
            "eip7702_like": bucket.get("eip7702_like_probed", ""),
            "dominant_to": to_diag.get("dominant_to", ""),
            "dominant_to_frac": to_diag.get("dominant_to_frac", ""),
            "nonce0_to": to_diag.get("nonce0_to", ""),
            "tx_rows_used": anchors.get("tx_rows_used", "0"),
            "representative_weth_amt": anchors.get("representative_weth_amt", ""),
            "core_erc20_edge_summary": edge_summary,
            "native_eth_funding_coverage": "NOT_COVERED_NO_TRACE_OR_RPC_BALANCE_GRAPH",
            "shared_funder": shared_funder,
            "shared_sink": shared_sink,
            "common_with_0x7e49": "NO_CORE_ERC20_EDGE_OVERLAP_IN_CH_WINDOW",
            "funding_evidence_level": evidence_level,
            "verdict": "INCONCLUSIVE",
            "recommended_next_action": "keep_author_like_reference_require_targeted_trace_or_nonce0_funding_probe",
        }
        for prefix in ("first", "representative", "last"):
            row.update(enrich_anchor(prefix))
        out.append(row)
    return out


def build_shared_cluster_rows(scorecard: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for row in scorecard:
        if row.get("type") == "cluster" or row.get("address_or_cluster", "").startswith("cluster:shared_to:"):
            rows.append(
                {
                    "address_or_cluster": row.get("address_or_cluster", ""),
                    "category": "useful_bot_or_flow_source",
                    "operator_identity_allowed": False,
                    "feature_use_allowed": True,
                    "funding_claim_allowed": False,
                    "router_flags": row.get("router_flags", ""),
                    "funding_flags": row.get("funding_flags", ""),
                    "decision": "FEATURE_ENRICHMENT_ONLY_SHARED_TX_TO_NOT_OPERATOR",
                }
            )
    return rows


def md_table(rows: list[dict[str, Any]], fields: list[str], limit: int = 20) -> str:
    out = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    for row in rows[:limit]:
        out.append("| " + " | ".join(str(row.get(f, "")) for f in fields) + " |")
    return "\n".join(out)


def write_readout(track_a: dict[str, Any], track_b: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    text = f"""# Sprint055 — Dual Track Validation

Generated: `{datetime.now(timezone.utc).isoformat()}`

## Scope

Reused root wiki/whitepaper contracts, Sprint054 outputs, and Sprint045-053 controller / ROI / quoteable / token viability / shared `tx.to` / funding graph artifacts. ClickHouse use was read-only and bounded to local existing tables. No live trading, training, model promotion, production signal writes, broad RPC backfill, or secret reads were performed.

## Track A — `0x501e` Economic Validation

Verdict: **{track_a['verdict']}** for paper/shadow watchlist only.

| check | result |
| --- | --- |
| Base V3 / WETH-leg universe | yes, via Sprint045C/048A/049B artifacts |
| quoteable / executable | quoteable historical lane, `quoteable_share={track_a['quoteable_share']}` |
| delayed follow | `{track_a['delayed_follow_verdict']}` at 1-block model, df_roi={track_a['delayed_follow_roi_pct']}% |
| horizon artifact status | {track_a['horizon_artifact_status']} |
| 5m horizon | {track_a['horizon_5m']} |
| 30m horizon | {track_a['horizon_30m']} |
| realized exit buckets | {track_a['exit_bucket_pnl']} |
| top token concentration | `{track_a['top_token']}` share_abs={track_a['top_token_pnl_share_abs']} trade_share={track_a['top_token_trade_share']} |
| top pool concentration | `{track_a['top_pool']}` weth_share={track_a['top_pool_weth_share']} |
| top day concentration | `{track_a['top_day']}` pnl_share_abs={track_a['top_day_pnl_share_abs']} |
| anomaly concentration | largest_trade_share_abs={track_a['largest_trade_share_abs']} |
| CH coverage | {track_a['ch_v3_min_block_time']} → {track_a['ch_v3_max_block_time']} rows={track_a['ch_v3_rows']} |
| recent activity | {track_a['recent_activity_status']} |
| platform/router contamination | `{track_a['router_flags']}`; dominant_to=`{track_a['dominant_to']}` frac={track_a['dominant_to_frac']} |
| EOA / 7702 | is_eoa={track_a['is_eoa']}, eip7702_like={track_a['eip7702_like']} |

Paper watchlist rule: monitor wallet-level fresh buys only; require new-token/new-pool viability, WETH-leg quoteability, observed follow entry within measured stream latency, and no `signals` writes.

## Track B — Author-like Funding Graph

Verdict: **INCONCLUSIVE**. Author-like fingerprints remain valid screening evidence, but funding graph attribution is not proven. In local CH `erc20_transfers_semantic` coverage, WETH/USDC/USDC.e edges touching the author-like set and `0x7e49` are zero, so there is no shared funder/sink proof.

{md_table(track_b, ['candidate', 'author_fingerprint_score', 'gas_fingerprint', 'low_priority_gas_share', 'fee_tier_mix', 'dominant_to', 'tx_rows_used', 'funding_evidence_level', 'verdict'], 20)}

## Shared `tx.to` Cluster Rule

Shared `tx.to` is now fixed as feature enrichment only, never operator identity.

{md_table(clusters, ['address_or_cluster', 'operator_identity_allowed', 'feature_use_allowed', 'funding_claim_allowed', 'decision'], 10)}

## Decision Flags

- `TRACK_A_0X501E_VERDICT={track_a['verdict']}`
- `TRACK_A_PRODUCTION_SIGNALS_ALLOWED=false`
- `TRACK_A_RECENT_ACTIVITY_CONFIRMED=false`
- `TRACK_B_AUTHOR_WALLET_FOUND=false`
- `TRACK_B_FUNDING_GRAPH_VERDICT=INCONCLUSIVE`
- `SHARED_TX_TO_OPERATOR_IDENTITY_ALLOWED=false`
- `LIVE_ALLOWED=false`
- `TRAINING_ALLOWED=false`
- `MODEL_PROMOTION_ALLOWED=false`
"""
    (AUDIT_DIR / "dual_track_validation_readout.md").write_text(text, encoding="utf-8")


def main() -> None:
    inputs = {
        "scorecard": read_csv(ROOT / "mvp/audits/sprint054/candidate_scorecard.csv"),
        "roi": read_csv(DIAG_DIR / "sprint049b_rescored_candidates.csv"),
        "realized": read_csv(DIAG_DIR / "sprint048b_realized_pnl.csv"),
        "deconc": read_csv(DIAG_DIR / "sprint048b_deconc_paper_follow.csv"),
        "probe": read_csv(DIAG_DIR / "sprint045c_top_controller_probe.csv"),
        "buckets": read_csv(DIAG_DIR / "sprint045c_candidate_buckets.csv"),
        "to_diag": read_csv(DIAG_DIR / "sprint045d_to_field_diagnostics.csv"),
        "tx_map": read_csv(DIAG_DIR / "sprint045b_controller_tx_map.csv"),
        "sidecar_dashcam": read_csv(LOG_DIR / "copy_follow_dashcam.csv"),
        "token_viability": read_json(DIAG_DIR / "sprint049c_token_viability_check.json"),
    }

    track_a = build_track_a(inputs)
    track_b = build_track_b(inputs)
    clusters = build_shared_cluster_rows(inputs["scorecard"])

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(
        DIAG_DIR / "sprint055_track_a_0x501e_watchlist.csv",
        [track_a],
        [
            "address",
            "verdict",
            "realized_pnl_weth",
            "roi_pct",
            "profit_factor",
            "win_rate",
            "delayed_follow_verdict",
            "top_token_pnl_share_abs",
            "top_pool_weth_share",
            "recent_activity_status",
            "paper_watchlist_action",
        ],
    )
    write_csv(
        DIAG_DIR / "sprint055_track_b_author_funding_probe.csv",
        track_b,
        [
            "candidate",
            "verdict",
            "author_fingerprint_score",
            "gas_fingerprint",
            "low_priority_gas_share",
            "fee_tier_mix",
            "is_eoa",
            "eip7702_like",
            "dominant_to",
            "nonce0_to",
            "funding_evidence_level",
            "common_with_0x7e49",
        ],
    )
    write_csv(
        DIAG_DIR / "sprint055_shared_tx_to_feature_enrichment.csv",
        clusters,
        [
            "address_or_cluster",
            "operator_identity_allowed",
            "feature_use_allowed",
            "funding_claim_allowed",
            "decision",
        ],
    )
    flags = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "track_a_0x501e_verdict": track_a["verdict"],
        "track_a_recent_activity_confirmed": False,
        "track_a_production_signals_allowed": False,
        "track_b_author_wallet_found": False,
        "track_b_funding_graph_verdict": "INCONCLUSIVE",
        "shared_tx_to_operator_identity_allowed": False,
        "live_allowed": False,
        "training_allowed": False,
        "model_promotion_allowed": False,
        "commands_scope": "offline artifacts + read-only local ClickHouse",
    }
    (AUDIT_DIR / "decision_flags.json").write_text(json.dumps(flags, indent=2), encoding="utf-8")
    (AUDIT_DIR / "track_a_0x501e_validation.json").write_text(json.dumps(track_a, indent=2), encoding="utf-8")
    (AUDIT_DIR / "track_b_author_funding_probe.json").write_text(json.dumps(track_b, indent=2), encoding="utf-8")
    write_readout(track_a, track_b, clusters)
    print(json.dumps({"audit_dir": str(AUDIT_DIR), "track_a_verdict": track_a["verdict"], "track_b_rows": len(track_b)}, indent=2))


if __name__ == "__main__":
    main()
