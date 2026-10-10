#!/usr/bin/env python3
"""Sprint037A-BF — ClickHouse inventory + historical V3 swap backfill planning (read-only).

- No training, labeling, live execution, or RPC backfill in this module.
- Emits JSON + Markdown for cost / path decisions. Optional CH via HTTP or docker exec.

Env:
  CLICKHOUSE_URL / CLICKHOUSE_USER / CLICKHOUSE_PASSWORD  — HTTP (optional)
  SPRINT037A_BF_CH_CONTAINER — default eatlq-clickhouse-1 for --docker-exec
  SPRINT037A_BF_OUT_JSON / _OUT_MD — output paths
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]

OUT_JSON = REPO / "mvp/diagnostics/sprint037a_bf_backfill_inventory.json"
OUT_MD = REPO / "mvp/audits/sprint037a_bf_historical_backfill_plan.md"

CHAIN_BASE = 8453
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

DECISION_FLAGS: dict[str, Any] = {
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "HISTORICAL_BACKFILL_PLANNING_ALLOWED": True,
    "FULL_BACKFILL_ALLOWED": False,
    "FULL_BACKFILL_GATE": "false_until_cost_preflight",
    "SPRINT037B_REPLAY_ALLOWED": False,
    "SPRINT037B_REPLAY_GATE": "false_until_history_depth_pass",
    "TRAINING_ALLOWED": False,
    "H12_ABLATION_ALLOWED": False,
    "PHASE6_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "MODEL_PROMOTION_ALLOWED": False,
    "NO_ALPHA_CLAIM": True,
}

# Sprint037a meme filter — keep in sync with scripts/sprint037a_historical_regime_mining.py
_STABLES = tuple(
    sorted(
        {
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
            "0x4a3a6dd60a34bb2aba60d73b4c88d1e535f79e18",
            "0x368181499736d0c0cc614dbb145e2ec1ac86b8c6",
            "0x04d5ddf5f3a8939889f11e97f8c4bb48317f1938",
            "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
        }
    )
)
_EXCL = (
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0x6c561b446416e1a00e8e93e221854d6ea4171372",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
    "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",
    "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
)


def _meme_cte() -> str:
    st = ", ".join(f"'{a.lower()}'" for a in _STABLES)
    ex = ", ".join(f"'{a.lower()}'" for a in _EXCL)
    return f"""meme AS (
  SELECT lower(pool_address) AS pa FROM eatlq.discovered_pools
  WHERE chain_id = {CHAIN_BASE} AND lower(pool_type) = 'v3' AND has_weth_leg = 1
    AND fee_bps IN (100, 500, 3000, 10000)
    AND NOT (lower(token0_address) IN ({st}) AND lower(token1_address) IN ({st}))
    AND lower(pool_address) NOT IN ({ex})
)"""


def _ch_guard(sql: str) -> None:
    t = sql.strip().lower()
    if not (t.startswith("select") or t.startswith("with")):
        raise ValueError("read-only SELECT/WITH only")


def _ch_http_post(url: str, user: str, password: str, sql: str, timeout: float = 120.0) -> str:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";") + " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if user:
        tok = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {tok}")
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _ch_docker_exec(container: str, sql: str, timeout: int = 180) -> str:
    _ch_guard(sql)
    return subprocess.check_output(
        ["docker", "exec", container, "clickhouse-client", "--query", sql],
        text=True,
        timeout=timeout,
    )


def _run_query(mode: str, ch_url: str, user: str, pw: str, container: str, sql: str) -> str:
    if mode == "docker-exec":
        return _ch_docker_exec(container, sql)
    return _ch_http_post(ch_url, user, pw, sql)


def _parse_json_each_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _offline_fixture() -> dict[str, Any]:
    return {
        "kind": "sprint037a_bf_backfill_inventory",
        "version": "1.0",
        "offline_fixture": True,
        "decision_flags": {**DECISION_FLAGS},
        "coverage_root_cause_notes": ["Fixture: run with `python3 scripts/sprint037a_bf_backfill_inventory.py --run --docker-exec`."],
        "clickhouse_inventory": {},
        "backfill_sources_ranked": [],
        "shortest_path_summary": "",
        "cost_estimates": {},
        "smoke_pipeline": {"status": "skipped_offline"},
        "next_move": "",
    }


def _estimate_rpc(rows_per_day_logs: float, days: int) -> dict[str, Any]:
    """Order-of-magnitude only; actual Base volume varies."""
    total_logs = rows_per_day_logs * days
    # Optimistic: 5k logs per eth_getLogs response → lower bound requests
    req_lo = int(total_logs / 5000) + 1
    # Conservative: 2k logs per response + chunking overhead
    req_hi = int(total_logs / 2000) + 5000
    return {
        "assumed_mean_logs_per_day_base_v3_topic_all_pools": rows_per_day_logs,
        "days": days,
        "estimated_total_logs_order_of_magnitude": int(total_logs),
        "estimated_eth_getLogs_calls_range": [req_lo, req_hi],
        "notes": (
            "Full-chain Uniswap V3 Swap topic scan is the expensive upper bound. "
            "Prefer: (a) warehouse export, (b) address-batched getLogs over discovered_pools only, "
            "(c) archival node with higher quotas."
        ),
    }


def _write_md(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sprint037A-BF — Base Uniswap V3 historical swap backfill plan",
        "",
        "Research and inventory only. **No alpha claim.** Does not authorize full backfill.",
        "",
        "## Coverage root cause (summary)",
        "",
    ]
    for x in payload.get("coverage_root_cause_notes") or []:
        lines.append(f"- {x}")
    lines.extend(
        [
            "",
            "## Inventory snapshot",
            "",
            f"```json\n{json.dumps(payload.get('clickhouse_inventory'), indent=2, sort_keys=True)}\n```",
            "",
            "## Available historical sources (ranked)",
            "",
        ]
    )
    for i, s in enumerate(payload.get("backfill_sources_ranked") or [], 1):
        lines.append(f"{i}. {s}")
    lines.extend(
        [
            "",
            "## Shortest path",
            "",
            payload.get("shortest_path_summary", ""),
            "",
            "## Cost / time / RPC (order-of-magnitude)",
            "",
            f"```json\n{json.dumps(payload.get('cost_estimates'), indent=2, sort_keys=True)}\n```",
            "",
            "## Smoke (pipeline / inventory)",
            "",
            f"```json\n{json.dumps(payload.get('smoke_pipeline'), indent=2, sort_keys=True)}\n```",
            "",
            "## Decision flags",
            "",
            f"```json\n{json.dumps(payload.get('decision_flags'), indent=2, sort_keys=True)}\n```",
            "",
            "## Next move",
            "",
            (payload.get("next_move") or ""),
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--offline-fixture", action="store_true")
    ap.add_argument("--docker-exec", action="store_true", help="Use docker exec to clickhouse-client (no HTTP URL)")
    args = ap.parse_args()

    out_j = Path(os.environ.get("SPRINT037A_BF_OUT_JSON", str(OUT_JSON)))
    out_md = Path(os.environ.get("SPRINT037A_BF_OUT_MD", str(OUT_MD)))
    container = os.environ.get("SPRINT037A_BF_CH_CONTAINER", "eatlq-clickhouse-1")

    if args.offline_fixture:
        fix = _offline_fixture()
        out_j.parent.mkdir(parents=True, exist_ok=True)
        out_j.write_text(json.dumps(fix, indent=2, sort_keys=True), encoding="utf-8")
        _write_md(out_md, fix)
        print(json.dumps({"status": "ok", "fixture": True}))
        return 0

    if not args.run:
        print("use --run or --offline-fixture", file=sys.stderr)
        return 2

    mode = "docker-exec" if args.docker_exec else "http"
    ch_url = (os.environ.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_HTTP_URL") or "").strip()
    user = os.environ.get("CLICKHOUSE_USER", "default")
    pw = os.environ.get("CLICKHOUSE_PASSWORD") or ""
    if mode == "http" and not ch_url:
        print("set CLICKHOUSE_URL or pass --docker-exec", file=sys.stderr)
        return 5

    def qry(sql: str) -> list[dict[str, Any]]:
        raw = _run_query(mode, ch_url, user, pw, container, sql)
        return _parse_json_each_rows(raw)

    inv: dict[str, Any] = {}

    inv["uniswap_v3_swaps_chain"] = qry(
        f"""SELECT count() AS rows,
       uniqExact((tx_hash, log_index, lower(pool_address))) AS uniq_events,
       min(coalesce(block_time, received_at_ms)) AS min_ts,
       max(coalesce(block_time, received_at_ms)) AS max_ts
FROM eatlq.uniswap_v3_swaps WHERE chain_id = {CHAIN_BASE} FORMAT JSONEachRow"""
    )
    inv["raw_evm_logs_v3_topic_filtered"] = qry(
        f"""SELECT count() AS rows,
       min(coalesce(block_time, received_at_ms)) AS min_ts,
       max(coalesce(block_time, received_at_ms)) AS max_ts
FROM eatlq.raw_evm_logs
WHERE chain_id = {CHAIN_BASE}
  AND topic0 = '{V3_SWAP_TOPIC}'
  AND length(topic1) = 66 AND length(topic2) = 66 AND length(data) >= 322
FORMAT JSONEachRow"""
    )
    meme_count_sql = f"""
SELECT toUInt64(count()) AS meme_pool_rows
FROM eatlq.discovered_pools
WHERE chain_id = {CHAIN_BASE} AND lower(pool_type) = 'v3' AND has_weth_leg = 1
  AND fee_bps IN (100, 500, 3000, 10000)
  AND NOT (
    lower(token0_address) IN ({', '.join(repr(s) for s in _STABLES)})
    AND lower(token1_address) IN ({', '.join(repr(s) for s in _STABLES)})
  )
  AND lower(pool_address) NOT IN ({', '.join(repr(e.lower()) for e in _EXCL)})
FORMAT JSONEachRow"""
    inv["discovered_pools_meme_universe"] = qry(meme_count_sql)

    meme_join_sql = f"""
WITH {_meme_cte().replace('meme AS', 'meme AS', 1)}
SELECT
  count() AS swap_rows,
  toUInt64(uniqExact(lower(s.pool_address))) AS pools_with_swap,
  min(coalesce(s.block_time, s.received_at_ms)) AS min_ts,
  max(coalesce(s.block_time, s.received_at_ms)) AS max_ts
FROM eatlq.uniswap_v3_swaps_deduped AS s
INNER JOIN meme m ON lower(s.pool_address) = m.pa
WHERE s.chain_id = {CHAIN_BASE} AND s.removed = 0
FORMAT JSONEachRow"""
    inv["meme_cohort_on_deduped"] = qry(meme_join_sql)

    global_dup = qry(
        f"""SELECT
  count() AS rows,
  uniqExact((tx_hash, log_index, lower(pool_address))) AS uniq_events
FROM eatlq.uniswap_v3_swaps WHERE chain_id = {CHAIN_BASE} FORMAT JSONEachRow"""
    )[0]
    rows = float(global_dup.get("rows") or 0)
    uq = float(global_dup.get("uniq_events") or 1)
    dup_ratio = round(rows / uq, 4) if uq else None

    recent_day = qry(
        f"""SELECT toDate(coalesce(block_time, received_at_ms)) AS d, count() AS c
FROM eatlq.raw_evm_logs
WHERE chain_id = {CHAIN_BASE} AND topic0 = '{V3_SWAP_TOPIC}'
  AND length(topic1)=66 AND length(topic2)=66 AND length(data) >= 322
GROUP BY d ORDER BY d DESC LIMIT 1 FORMAT JSONEachRow"""
    )

    mean_daily_logs: Optional[float] = None
    if recent_day:
        n7 = qry(
            f"""SELECT avg(cnt) AS mean_daily FROM (
  SELECT toDate(coalesce(block_time, received_at_ms)) AS d, count() AS cnt
  FROM eatlq.raw_evm_logs
  WHERE chain_id = {CHAIN_BASE} AND topic0 = '{V3_SWAP_TOPIC}'
    AND length(topic1)=66 AND length(topic2)=66 AND length(data) >= 322
    AND coalesce(block_time, received_at_ms) >= now() - INTERVAL 14 DAY
  GROUP BY d
) AS daily FORMAT JSONEachRow"""
        )
        if n7:
            mean_daily_logs = float(n7[0].get("mean_daily") or 0)

    last_7d_raw = qry(
        f"""SELECT count() AS c
FROM eatlq.raw_evm_logs
WHERE chain_id = {CHAIN_BASE} AND topic0 = '{V3_SWAP_TOPIC}'
  AND length(topic1)=66 AND length(topic2)=66 AND length(data) >= 322
  AND coalesce(block_time, received_at_ms) >= now() - INTERVAL 7 DAY
FORMAT JSONEachRow"""
    )

    smoke = {
        "status": "inventory_only",
        "global_table_duplicate_ratio_rows_per_uniq_event": dup_ratio,
        "recent_window_mean_daily_raw_logs_estimate": mean_daily_logs,
        "last_7d_projectable_raw_v3_logs": int(last_7d_raw[0].get("c") or 0) if last_7d_raw else None,
        "note": (
            "Raw vs normalized row counts can differ by calendar day; use deduped views for analytics. "
            "This run did not execute RPC or insert backfill. "
            "last_7d_* is a cheap ingest-path smoke only."
        ),
    }

    meme_d = inv["meme_cohort_on_deduped"][0]
    raw_t = inv["raw_evm_logs_v3_topic_filtered"][0]
    raw_rows = float(raw_t.get("rows") or 0)
    # Span in days from min/max (block_time clock)
    ts0 = str(raw_t.get("min_ts") or "")
    ts1 = str(raw_t.get("max_ts") or "")

    def _parse_ts_simple(s: str) -> Optional[float]:
        if not s or len(s) < 10:
            return None
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            try:
                return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None

    tmn = _parse_ts_simple(ts0)
    tmx = _parse_ts_simple(ts1)
    span_ch_days = (tmx - tmn) / 86400.0 if tmn and tmx and tmx > tmn else None
    mean_daily_over_ch_retained = round(raw_rows / span_ch_days, 2) if span_ch_days and span_ch_days > 0.5 else None

    cost_basis_logs = max(mean_daily_logs or 0.0, mean_daily_over_ch_retained or 0.0)
    if cost_basis_logs < 1:
        cost_basis_logs = mean_daily_logs or 50_000.0

    inv["raw_v3_rate_hints"] = [
        {
            "metric": "mean_daily_last_14d_calendar",
            "value": mean_daily_logs,
            "note": "Can be depressed if ingest/filter/throttle changed; not-alone predictor for pre-retained-window history.",
        },
        {
            "metric": "mean_daily_over_clickhouse_retained_window",
            "value": mean_daily_over_ch_retained,
            "retained_span_days_approx": round(span_ch_days, 4) if span_ch_days else None,
        },
        {
            "metric": "rpc_cost_model_logs_per_day_used",
            "value": round(cost_basis_logs, 2),
            "rule": "max(last_14d, retained_window_mean); use warehouse quote for pre-CH history.",
        },
    ]
    raw_min = str(raw_t.get("min_ts") or "")
    raw_max = str(raw_t.get("max_ts") or "")

    root_notes = [
        "eatlq.uniswap_v3_swaps / raw_evm_logs (V3 Swap topic) share the same earliest timestamps in CH — "
        "this deployment’s ingestion retained Base logs starting at that clock, not ‘full chain history’. "
        "Earlier on-chain history is absent from local CH until backfilled.",
        "Sprint037A’s ~23d meme-cohort window is tighter than global V3 (~76d) because only a subset of "
        "meme-filtered discovered_pools (WETH-leg, fee tiers, stable-stable excluded) show swap rows in CH "
        f"({meme_d.get('pools_with_swap')} / {inv['discovered_pools_meme_universe'][0].get('meme_pool_rows')} pools; "
        f"{meme_d.get('swap_rows')} swap rows on uniswap_v3_swaps_deduped).",
        "uniswap_v3_swaps has material duplicate keys (rows / uniq_event). Prefer uniswap_v3_swaps_deduped / "
        "uniswap_v3_swaps_enriched for regime mining and backfill validation.",
    ]

    sources = [
        "**Warehouse / batch export** (Dune, Flipside, BigQuery public crypto): lowest incremental RPC once exported; "
        "map schema → `raw_evm_logs` or directly to `uniswap_v3_swaps` columns with dedupe keys.",
        "**Batched `eth_getLogs`**: topic `Swap` (V3) with `address` in batches of discovered pool addresses + block ranges; "
        "scales with (pools × time chunks), not infinite if pool list is capped.",
        "**Archive node replay**: same as (2) with higher throughput; still requires chunking and cost preflight.",
    ]

    shortest = (
        "Shortest engineering path: **bulk load historical decoded logs via warehouse export** into staging table, "
        "then `INSERT` into `eatlq.raw_evm_logs` preserving (chain_id, block_number, tx_hash, log_index, topic0..) "
        "so existing materialized views can populate `uniswap_v3_swaps`. "
        "If export maps 1:1 to normalized swap table, skip raw insert with an explicit deduped `INSERT SELECT` "
        "guarded by uniqueness constraint tests."
    )

    est180 = _estimate_rpc(cost_basis_logs, 180)
    est365 = _estimate_rpc(cost_basis_logs, 365)

    next_move = (
        "1) Run commercial / internal cost preflight on chosen source (export quote + CH storage). "
        "2) Pilot 1–7d block window via **address-batched** `getLogs` into staging **or** import Parquet slice. "
        "3) Validate with `uniqExact(tx_hash,log_index,pool)` parity vs deduped view. "
        "4) Re-run `scripts/sprint037a_historical_regime_mining.py` after span ≥ 180d. "
        "**Sprint037B remains closed** until `SPRINT037A_MIN_HISTORY_SPAN_DAYS` passes."
    )

    payload: dict[str, Any] = {
        "kind": "sprint037a_bf_backfill_inventory",
        "version": "1.0",
        "offline_fixture": False,
        "decision_flags": {**DECISION_FLAGS},
        "clickhouse_inventory": inv,
        "coverage_root_cause_notes": root_notes,
        "backfill_sources_ranked": sources,
        "shortest_path_summary": shortest,
        "cost_estimates": {
            "basis_rpc_model_logs_per_day": round(cost_basis_logs, 4),
            "basis_breakdown": inv["raw_v3_rate_hints"],
            "rpc_full_chain_topic_scan_180d": est180,
            "rpc_full_chain_topic_scan_365d": est365,
            "human_time": {
                "schema_mapping": "1–3 engineer-days",
                "pilot_load_and_validation": "0.5–2 days",
                "full_180d_load": "2–5 days wall-clock (I/O bound) typical",
            },
        },
        "smoke_pipeline": smoke,
        "next_move": next_move,
        "provenance": {
            "raw_table_min_ts": raw_min,
            "raw_table_max_ts": raw_max,
            "inventory_mode": mode,
        },
    }

    out_j.parent.mkdir(parents=True, exist_ok=True)
    out_j.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    _write_md(out_md, payload)
    print(json.dumps({"status": "ok", "out_json": str(out_j)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
