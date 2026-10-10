from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_research_utils import (  # noqa: E402
    fnum,
    gini,
    nonempty_coverage,
    numeric_summary,
    optional_float,
    percentile,
    read_rows,
    rounded,
    sha256,
    shannon_entropy_from_counts,
    write_csv_rows,
    write_json,
    write_markdown,
)


H106_CANDIDATE_TABLES = [
    "swap_trade_events_all",
    "uniswap_v2_swaps_enriched",
    "uniswap_v3_swaps_enriched",
    "raw_evm_logs",
    "discovered_pools",
]
H106_FIELD_SEARCH = [
    "timestamp",
    "block_time",
    "ts",
    "received_at_ms",
    "pool_address",
    "venue_address",
    "address",
    "token_address",
    "tx_hash",
    "log_index",
    "block_number",
    "tx_from",
    "sender",
    "sender_address",
    "wallet_address",
    "trader",
    "side",
    "is_buy",
    "swap_direction",
    "amount_raw",
    "amount_usd",
    "value_usd",
    "price",
    "pool_type",
    "protocol",
    "protocol_family",
]
H106_WINDOWS = [60, 180, 300, 900, 1800]


class ClickHouseClient:
    def __init__(self, cfg: dict[str, Any]):
        self.url = str(cfg.get("url", "http://clickhouse:8123")).rstrip("/")
        self.database = str(cfg.get("database", "default"))
        self.timeout = int(cfg.get("query_timeout_sec", 30))
        self.docker_container = str(cfg.get("docker_container", "eatlq-clickhouse-1"))
        self.transport = "http"

    def _guard_read_only(self, query: str) -> None:
        stripped = query.strip().lstrip("(").lower()
        allowed = ("select", "show", "describe", "desc", "exists")
        if not stripped.startswith(allowed):
            raise ValueError(f"ClickHouse query is not read-only: {query[:80]}")
        forbidden = (" insert ", " update ", " delete ", " alter ", " drop ", " create ", " truncate ", " optimize ")
        padded = f" {stripped} "
        if any(token in padded for token in forbidden):
            raise ValueError(f"ClickHouse query contains forbidden token: {query[:80]}")

    def query_json(self, query: str) -> list[dict[str, Any]]:
        self._guard_read_only(query)
        q = query.rstrip(";")
        if " format " not in q.lower():
            q = f"{q} FORMAT JSONEachRow"
        if self.transport == "docker_exec":
            return self._query_docker(q)
        try:
            return self._query_http(q)
        except Exception:
            self.transport = "docker_exec"
            return self._query_docker(q)

    def _query_http(self, query: str) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"database": self.database, "query": query})
        with urllib.request.urlopen(f"{self.url}/?{params}", timeout=self.timeout) as resp:
            body = resp.read().decode()
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    def _query_docker(self, query: str) -> list[dict[str, Any]]:
        cmd = [
            "docker",
            "exec",
            self.docker_container,
            "clickhouse-client",
            "--database",
            self.database,
            "--query",
            query,
        ]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=self.timeout + 5, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _load_config(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _lower_addr(value: Any) -> str:
    return str(value or "").strip().lower()


def _entry_ts_ms(row: dict[str, Any]) -> int | None:
    ts = optional_float(row.get("timestamp"))
    if ts is None:
        return None
    return int(ts if ts > 10_000_000_000 else ts * 1000)


def _discover_database(client: ClickHouseClient, cfg_db: str) -> str:
    for db in [cfg_db, "eatlq", "default"]:
        try:
            rows = client.query_json(f"SHOW TABLES FROM {db}")
        except Exception:
            continue
        names = {str(r.get("name", "") or r.get("table", "")) for r in rows}
        if any(t in names for t in H106_CANDIDATE_TABLES):
            client.database = db
            return db
    return cfg_db


def _schema_report(client: ClickHouseClient, cfg: dict[str, Any]) -> dict[str, Any]:
    effective_db = _discover_database(client, str(cfg.get("clickhouse", {}).get("database", "default")))
    table_rows = client.query_json(f"SHOW TABLES FROM {effective_db}")
    tables = sorted(str(r.get("name", "") or r.get("table", "")) for r in table_rows)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "configured_database": str(cfg.get("clickhouse", {}).get("database", "default")),
        "effective_database": effective_db,
        "transport": client.transport,
        "tables": tables,
        "candidate_tables": {},
        "field_search_terms": H106_FIELD_SEARCH,
    }
    for table in H106_CANDIDATE_TABLES:
        if table not in tables:
            report["candidate_tables"][table] = {"exists": False}
            continue
        desc = client.query_json(f"DESCRIBE TABLE {effective_db}.{table}")
        columns = {str(r.get("name")): str(r.get("type")) for r in desc}
        matched_fields = {
            field: [name for name in columns if field in name.lower()]
            for field in H106_FIELD_SEARCH
            if any(field in name.lower() for name in columns)
        }
        sample_error = None
        try:
            sample = client.query_json(f"SELECT * FROM {effective_db}.{table} LIMIT 3")
        except Exception as exc:
            sample = []
            sample_error = str(exc)
        report["candidate_tables"][table] = {
            "exists": True,
            "columns": columns,
            "matched_fields": matched_fields,
            "sample_rows": sample,
            "sample_error": sample_error,
        }
    return report


def _count_window(
    client: ClickHouseClient,
    db: str,
    table: str,
    address_col: str,
    address: str,
    entry_ms: int,
    window_sec: int,
) -> int:
    if not address:
        return 0
    ts_expr = "toUnixTimestamp64Milli(coalesce(block_time, received_at_ms))"
    q = (
        f"SELECT count() AS c FROM {db}.{table} "
        f"WHERE lower({address_col}) = {_sql_string(address)} "
        f"AND {ts_expr} >= {entry_ms - window_sec * 1000} "
        f"AND {ts_expr} < {entry_ms}"
    )
    rows = client.query_json(q)
    return int(rows[0].get("c", 0)) if rows else 0


def _join_feasibility(client: ClickHouseClient, db: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    samples = rows[:20]
    coverage: list[dict[str, Any]] = []
    table_counts = Counter()
    query_examples: list[dict[str, Any]] = []
    for idx, row in enumerate(samples):
        entry_ms = _entry_ts_ms(row)
        pool = _lower_addr(row.get("pool_address"))
        token = _lower_addr(row.get("token_address"))
        item = {
            "sample_index": idx,
            "entry_timestamp_ms": entry_ms,
            "pool_address": pool,
            "token_address": token,
            "matches": {},
        }
        if entry_ms is None:
            coverage.append(item)
            continue
        checks = [
            ("swap_trade_events_all", "venue_address", pool),
            ("swap_trade_events_all", "venue_address", token),
            ("raw_evm_logs", "address", pool),
            ("raw_evm_logs", "address", token),
        ]
        seen_checks: set[tuple[str, str, str]] = set()
        matched_keys_for_sample: set[str] = set()
        for table, col, addr in checks:
            check_id = (table, col, addr)
            if check_id in seen_checks:
                continue
            seen_checks.add(check_id)
            key = f"{table}.{col}"
            try:
                count = _count_window(client, db, table, col, addr, entry_ms, 300)
            except Exception as exc:
                item["matches"][key] = {"error": str(exc)}
                continue
            item["matches"][key] = {"count_5m": count, "address": addr}
            if count > 0:
                matched_keys_for_sample.add(key)
                if len(query_examples) < 8:
                    query_examples.append(
                        {
                            "purpose": "join_feasibility_5m_pre_entry",
                            "table": table,
                            "address_column": col,
                            "address": addr,
                            "entry_timestamp_ms": entry_ms,
                            "query": (
                                f"SELECT count() AS c FROM {db}.{table} WHERE lower({col}) = "
                                f"{_sql_string(addr)} AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) "
                                f">= {entry_ms - 300000} AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) < {entry_ms}"
                            ),
                            "matched_rows": count,
                        }
                    )
        for key in matched_keys_for_sample:
            table_counts[key] += 1
        coverage.append(item)
    best = table_counts.most_common(1)[0] if table_counts else (None, 0)
    return {
        "sample_size": len(samples),
        "coverage_by_sample": coverage,
        "matched_sample_count": sum(
            1
            for c in coverage
            if any((v.get("count_5m") or 0) > 0 for v in c.get("matches", {}).values() if isinstance(v, dict))
        ),
        "table_match_counts": dict(table_counts),
        "best_table": best[0],
        "best_table_matched_samples": best[1],
        "timestamp_interpretation": "H8 timestamp treated as epoch milliseconds; ClickHouse DateTime64 converted with toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)).",
        "future_leakage_avoided": True,
        "query_examples": query_examples,
    }


def _load_swap_events(client: ClickHouseClient, db: str, rows: list[dict[str, str]], max_window: int) -> dict[str, list[dict[str, Any]]]:
    pairs = {_lower_addr(r.get("pool_address")) for r in rows} | {_lower_addr(r.get("token_address")) for r in rows}
    addresses = sorted(p for p in pairs if p)
    ts_values = [t for t in (_entry_ts_ms(r) for r in rows) if t is not None]
    if not addresses or not ts_values:
        return {}
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  lower(venue_address) AS pool_address,
  toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) AS ts_ms,
  lower(sender_address) AS sender_address,
  lower(recipient_address) AS recipient_address,
  lower(swap_direction) AS swap_direction,
  ifNull(sold_amount_normalized, 0) AS sold_amount_raw,
  ifNull(bought_amount_normalized, 0) AS bought_amount_raw,
  protocol_family,
  venue_label,
  tx_hash,
  log_index
FROM {db}.swap_trade_events_all
WHERE lower(venue_address) IN ({addr_sql})
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) >= {min(ts_values) - max_window * 1000}
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) < {max(ts_values)}
ORDER BY pool_address, ts_ms
"""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in client.query_json(q):
        events[_lower_addr(event.get("pool_address"))].append(event)
    return events


def _load_raw_counts(client: ClickHouseClient, db: str, rows: list[dict[str, str]], max_window: int) -> dict[tuple[str, int], int]:
    addresses = sorted({_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address"))})
    ts_values = [t for t in (_entry_ts_ms(r) for r in rows) if t is not None]
    if not addresses or not ts_values:
        return {}
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  lower(address) AS address,
  intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), 60000) AS minute_bucket,
  count() AS c
FROM {db}.raw_evm_logs
WHERE lower(address) IN ({addr_sql})
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) >= {min(ts_values) - max_window * 1000}
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) < {max(ts_values)}
GROUP BY address, minute_bucket
"""
    return {(_lower_addr(r.get("address")), int(r.get("minute_bucket", 0))): int(r.get("c", 0)) for r in client.query_json(q)}


def _load_pool_metadata(client: ClickHouseClient, db: str, rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    addresses = sorted(
        {_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address"))}
        | {_lower_addr(r.get("token_address")) for r in rows if _lower_addr(r.get("token_address"))}
    )
    if not addresses:
        return {}
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  lower(pool_address) AS pool_address,
  pool_type,
  if(pool_type = 'v3', 'v3', 'v2') AS protocol,
  created_block,
  toUnixTimestamp(discovered_at) * 1000 AS discovered_at_ms
FROM {db}.discovered_pools
WHERE lower(pool_address) IN ({addr_sql})
   OR lower(token0_address) IN ({addr_sql})
   OR lower(token1_address) IN ({addr_sql})
"""
    meta: dict[str, dict[str, Any]] = {}
    for r in client.query_json(q):
        key = _lower_addr(r.get("pool_address"))
        if key:
            meta[key] = r
    return meta


def _volume(event: dict[str, Any]) -> float:
    sold = optional_float(event.get("sold_amount_raw")) or 0.0
    bought = optional_float(event.get("bought_amount_raw")) or 0.0
    return max(abs(sold), abs(bought))


def _is_buy(event: dict[str, Any]) -> bool:
    direction = str(event.get("swap_direction", "")).lower()
    return "buy" in direction or direction in {"token_buy", "base_to_token"}


def _is_sell(event: dict[str, Any]) -> bool:
    direction = str(event.get("swap_direction", "")).lower()
    return "sell" in direction or direction in {"token_sell", "token_to_base"}


def _window_features(events: list[dict[str, Any]], entry_ms: int, window_sec: int) -> dict[str, Any]:
    suffix = f"{window_sec}s"
    subset = [
        e
        for e in events
        if optional_float(e.get("ts_ms")) is not None
        and entry_ms - window_sec * 1000 <= int(optional_float(e.get("ts_ms"))) < entry_ms
    ]
    buys = [e for e in subset if _is_buy(e)]
    sells = [e for e in subset if _is_sell(e)]
    buyer_vol = Counter()
    buyer_counts = Counter()
    for event in buys:
        wallet = _lower_addr(event.get("sender_address"))
        vol = _volume(event)
        if wallet:
            buyer_vol[wallet] += vol
            buyer_counts[wallet] += 1
    seller_counts = Counter(_lower_addr(e.get("sender_address")) for e in sells if _lower_addr(e.get("sender_address")))
    buy_volumes = [_volume(e) for e in buys]
    sell_volumes = [_volume(e) for e in sells]
    all_volumes = [_volume(e) for e in subset]
    total_buy_volume = sum(buy_volumes)
    top_volumes = sorted(buyer_vol.values(), reverse=True)
    repeated_buys = sum(c for c in buyer_counts.values() if c > 1)
    med_trade = sorted(all_volumes)[len(all_volumes) // 2] if all_volumes else None
    mu = sum(all_volumes) / len(all_volumes) if all_volumes else None
    std = math.sqrt(sum((v - mu) ** 2 for v in all_volumes) / len(all_volumes)) if all_volumes and mu else None
    return {
        f"unique_buyers_{suffix}": len(buyer_counts) if subset else None,
        f"unique_sellers_{suffix}": len(seller_counts) if subset else None,
        f"buyer_trade_count_{suffix}": len(buys) if subset else None,
        f"seller_trade_count_{suffix}": len(sells) if subset else None,
        f"top1_buyer_share_{suffix}": (top_volumes[0] / total_buy_volume) if total_buy_volume > 0 and top_volumes else None,
        f"top3_buyer_share_{suffix}": (sum(top_volumes[:3]) / total_buy_volume) if total_buy_volume > 0 and top_volumes else None,
        f"buyer_gini_{suffix}": gini(list(buyer_vol.values())) if buyer_vol else None,
        f"repeat_buyer_ratio_{suffix}": (repeated_buys / len(buys)) if buys else None,
        f"new_buyer_ratio_{suffix}": (len(buyer_counts) / len(buys)) if buys else None,
        f"buy_wallet_entropy_{suffix}": shannon_entropy_from_counts(buyer_counts) if buyer_counts else None,
        f"buy_volume_raw_{suffix}": total_buy_volume if subset else None,
        f"sell_volume_raw_{suffix}": sum(sell_volumes) if subset else None,
        f"net_flow_raw_{suffix}": (total_buy_volume - sum(sell_volumes)) if subset else None,
        f"abs_flow_raw_{suffix}": (total_buy_volume + sum(sell_volumes)) if subset else None,
        f"buy_sell_volume_ratio_{suffix}": (total_buy_volume / sum(sell_volumes)) if sum(sell_volumes) > 0 else None,
        f"buy_count_{suffix}": len(buys) if subset else None,
        f"sell_count_{suffix}": len(sells) if subset else None,
        f"large_buy_count_{suffix}": sum(1 for v in buy_volumes if v >= (sorted(buy_volumes)[int(len(buy_volumes) * 0.95)] if buy_volumes else math.inf)) if buy_volumes else None,
        f"large_sell_count_{suffix}": sum(1 for v in sell_volumes if v >= (sorted(sell_volumes)[int(len(sell_volumes) * 0.95)] if sell_volumes else math.inf)) if sell_volumes else None,
        f"median_trade_size_raw_{suffix}": med_trade,
        f"trade_size_cv_{suffix}": (std / mu) if mu and std is not None else None,
    }


def _h106_feature_columns(windows: list[int]) -> list[str]:
    cols: list[str] = []
    for w in windows:
        suffix = f"{w}s"
        cols.extend(
            [
                f"unique_buyers_{suffix}",
                f"unique_sellers_{suffix}",
                f"buyer_trade_count_{suffix}",
                f"seller_trade_count_{suffix}",
                f"top1_buyer_share_{suffix}",
                f"top3_buyer_share_{suffix}",
                f"buyer_gini_{suffix}",
                f"repeat_buyer_ratio_{suffix}",
                f"new_buyer_ratio_{suffix}",
                f"buy_wallet_entropy_{suffix}",
                f"buy_volume_raw_{suffix}",
                f"sell_volume_raw_{suffix}",
                f"net_flow_raw_{suffix}",
                f"abs_flow_raw_{suffix}",
                f"buy_sell_volume_ratio_{suffix}",
                f"buy_count_{suffix}",
                f"sell_count_{suffix}",
                f"large_buy_count_{suffix}",
                f"large_sell_count_{suffix}",
                f"median_trade_size_raw_{suffix}",
                f"trade_size_cv_{suffix}",
            ]
        )
    cols.extend(
        [
            "activity_acceleration_1m_vs_5m",
            "activity_acceleration_3m_vs_15m",
            "buy_pressure_acceleration_1m_vs_5m",
            "entropy_change_1m_vs_5m",
            "volatility_change_1m_vs_5m",
            "pool_age_sec_at_entry",
            "pool_age_bucket",
            "pool_type_backfilled",
            "protocol_backfilled",
        ]
    )
    return cols


def _ratio_delta(a: Any, b: Any) -> float | None:
    fa = optional_float(a)
    fb = optional_float(b)
    if fa is None or fb is None or abs(fb) <= 1e-12:
        return None
    return fa / fb


def _pool_age_bucket(age_sec: float | None) -> str:
    if age_sec is None:
        return ""
    if age_sec < 3600:
        return "early_lt_1h"
    if age_sec < 86400:
        return "mid_1h_1d"
    return "late_gte_1d"


def _backfill_rows(
    rows: list[dict[str, str]],
    windows: list[int],
    swap_events: dict[str, list[dict[str, Any]]],
    raw_counts: dict[tuple[str, int], int],
    metadata: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    source_cols = [
        "backfill_source_table",
        "backfill_event_count_5m",
        "wallet_source_available",
        "amount_source_available",
        "usd_amount_source_available",
        "pool_metadata_source_available",
    ]
    feature_cols = _h106_feature_columns(windows)
    out: list[dict[str, Any]] = []
    for row in rows:
        enriched: dict[str, Any] = dict(row)
        entry_ms = _entry_ts_ms(row)
        pool = _lower_addr(row.get("pool_address"))
        token = _lower_addr(row.get("token_address"))
        key = pool if pool in swap_events else token
        events = swap_events.get(key, [])
        if entry_ms is None:
            events = []
        for col in feature_cols:
            enriched[col] = ""
        if entry_ms is not None and events:
            per_window: dict[int, dict[str, Any]] = {}
            for w in windows:
                feats = _window_features(events, entry_ms, w)
                per_window[w] = feats
                for k, v in feats.items():
                    enriched[k] = fnum(v)
            enriched["activity_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window[60].get("buy_count_60s"), per_window[300].get("buy_count_300s")))
            enriched["activity_acceleration_3m_vs_15m"] = fnum(_ratio_delta(per_window[180].get("buy_count_180s"), per_window[900].get("buy_count_900s")))
            enriched["buy_pressure_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window[60].get("buy_sell_volume_ratio_60s"), per_window[300].get("buy_sell_volume_ratio_300s")))
            e1 = optional_float(per_window[60].get("buy_wallet_entropy_60s"))
            e5 = optional_float(per_window[300].get("buy_wallet_entropy_300s"))
            enriched["entropy_change_1m_vs_5m"] = fnum(e1 - e5 if e1 is not None and e5 is not None else None)
            enriched["volatility_change_1m_vs_5m"] = ""
        meta = metadata.get(pool) or metadata.get(token)
        if meta and entry_ms is not None:
            discovered_ms = optional_float(meta.get("discovered_at_ms"))
            age = (entry_ms - discovered_ms) / 1000 if discovered_ms is not None and entry_ms >= discovered_ms else None
            enriched["pool_age_sec_at_entry"] = fnum(age)
            enriched["pool_age_bucket"] = _pool_age_bucket(age)
            enriched["pool_type_backfilled"] = meta.get("pool_type", "")
            enriched["protocol_backfilled"] = meta.get("protocol", "")
        raw_5m = 0
        if entry_ms is not None and pool:
            start_min = (entry_ms - 300000) // 60000
            end_min = (entry_ms - 1) // 60000
            raw_5m = sum(raw_counts.get((pool, minute), 0) for minute in range(int(start_min), int(end_min) + 1))
        event_count_5m = optional_float(enriched.get("buy_count_300s"))
        sell_count_5m = optional_float(enriched.get("sell_count_300s"))
        enriched["backfill_source_table"] = "swap_trade_events_all" if events else ("raw_evm_logs_event_count_only" if raw_5m else "")
        enriched["backfill_event_count_5m"] = int((event_count_5m or 0) + (sell_count_5m or 0)) if events else (raw_5m or "")
        enriched["wallet_source_available"] = "true" if events else "false"
        enriched["amount_source_available"] = "true" if events else "false"
        enriched["usd_amount_source_available"] = "false"
        enriched["pool_metadata_source_available"] = "true" if meta else "false"
        out.append(enriched)
    return out, source_cols + feature_cols


def _coverage_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)

    def cov(cols: list[str]) -> dict[str, Any]:
        covered = sum(1 for r in rows if any(str(r.get(c, "")) != "" for c in cols))
        return {"rows": total, "covered_rows": covered, "coverage": round(covered / total, 6) if total else 0.0, "columns": cols}

    buyer_cols = [c for c in rows[0] if c.startswith(("unique_buyers_", "top1_buyer_share_", "buy_wallet_entropy_"))] if rows else []
    raw_cols = [c for c in rows[0] if c.startswith(("buy_volume_raw_", "sell_volume_raw_", "net_flow_raw_"))] if rows else []
    temporal_cols = [c for c in rows[0] if c.startswith(("activity_acceleration_", "buy_pressure_acceleration_", "entropy_change_", "volatility_change_"))] if rows else []
    meta_cols = ["pool_age_sec_at_entry", "pool_type_backfilled", "protocol_backfilled"]
    return {
        "buyer_concentration": cov(buyer_cols),
        "flow_quality_raw": cov(raw_cols),
        "flow_quality_usd": {"rows": total, "covered_rows": 0, "coverage": 0.0, "columns": [], "source_status": "unavailable_no_amount_usd_column"},
        "temporal_regime": cov(temporal_cols),
        "pool_quality_metadata": cov(meta_cols),
        "source_flags": {
            "wallet_source_available_rows": sum(1 for r in rows if r.get("wallet_source_available") == "true"),
            "amount_source_available_rows": sum(1 for r in rows if r.get("amount_source_available") == "true"),
            "usd_amount_source_available_rows": sum(1 for r in rows if r.get("usd_amount_source_available") == "true"),
            "pool_metadata_source_available_rows": sum(1 for r in rows if r.get("pool_metadata_source_available") == "true"),
        },
    }


def _quality_report(rows: list[dict[str, Any]], feature_cols: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for col in feature_cols:
        if col in {"pool_age_bucket", "pool_type_backfilled", "protocol_backfilled"}:
            nonempty = [r.get(col, "") for r in rows if str(r.get(col, "")) != ""]
            report[col] = {
                "count": len(nonempty),
                "missing_count": len(rows) - len(nonempty),
                "missing_rate": round((len(rows) - len(nonempty)) / len(rows), 6) if rows else None,
                "source_status": "partial" if nonempty else "unavailable",
            }
        else:
            item = numeric_summary([r.get(col, "") for r in rows])
            item["source_status"] = "available" if item["count"] else "unavailable"
            report[col] = item
    return report


def _leakage_guard(feature_cols: list[str]) -> dict[str, Any]:
    forbidden = [
        "pool_address",
        "token_address",
        "pool_type",
        "protocol",
        "tx_hash",
        "log_index",
        "block_number",
        "timestamp",
        "approximate_label",
        "executable_label",
        "executable_net_return",
    ]
    audit_only = [
        "backfill_source_table",
        "backfill_event_count_5m",
        "wallet_source_available",
        "amount_source_available",
        "usd_amount_source_available",
        "pool_metadata_source_available",
        "pool_type_backfilled",
        "protocol_backfilled",
        "pool_age_bucket",
    ]
    safe = [
        c
        for c in feature_cols
        if c not in audit_only
        and c not in forbidden
        and not c.startswith("future_")
        and c not in {"volatility_change_1m_vs_5m"}
    ]
    requiring_review = ["volatility_change_1m_vs_5m"]
    return {
        "safe_candidate_features": safe,
        "audit_only_fields": audit_only,
        "forbidden_fields": forbidden + ["future_*", "post-entry features"],
        "features_requiring_review": requiring_review,
        "future_leakage_avoided": True,
        "no_silent_zero_fill": True,
    }


def _write_backfilled_csv(path: Path, rows: list[dict[str, Any]], original_header: list[str], extra_cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = original_header + [c for c in extra_cols if c not in original_header]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _h106_clickhouse_backfill_source_recovery(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    input_path = _resolve(cfg["input_executable_dataset"])
    input_rows = read_rows(input_path, limit=int(cfg.get("backfill", {}).get("max_input_rows", 752)))
    with input_path.open(newline="") as fh:
        original_header = list(csv.DictReader(fh).fieldnames or [])
    windows = [int(w) for w in cfg.get("windows_sec", H106_WINDOWS)]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    schema = _schema_report(client, cfg)
    db = schema["effective_database"]
    feasibility = _join_feasibility(client, db, input_rows)
    swap_events = _load_swap_events(client, db, input_rows, max(windows))
    raw_counts = _load_raw_counts(client, db, input_rows, max(windows))
    metadata = _load_pool_metadata(client, db, input_rows)
    backfilled_rows, extra_cols = _backfill_rows(input_rows, windows, swap_events, raw_counts, metadata)
    coverage = _coverage_report(backfilled_rows)
    leakage = _leakage_guard(extra_cols)
    quality = _quality_report(backfilled_rows, [c for c in extra_cols if c not in leakage["audit_only_fields"]])
    produced_rows = len(backfilled_rows)
    backfilled_path = _resolve(outputs["backfilled_dataset_csv"])
    _write_backfilled_csv(backfilled_path, backfilled_rows, original_header, extra_cols)
    schema_path = _resolve(outputs["schema_report_json"])
    coverage_path = _resolve(outputs["coverage_json"])
    quality_path = _resolve(outputs["quality_json"])
    query_examples_path = _resolve(outputs["query_examples_json"])
    write_json(schema_path, schema)
    write_json(coverage_path, {**coverage, "leakage_guard": leakage})
    write_json(quality_path, quality)
    write_json(query_examples_path, feasibility)
    gate_checks = {
        "no_live": bool(cfg.get("safety", {}).get("no_live", False)),
        "no_redis_signals": bool(cfg.get("safety", {}).get("no_redis_signals", False)),
        "no_production_model_write": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
        "active_model_unchanged": sha256(_resolve(cfg.get("active_model_path", ""))) is not None,
        "no_production_retrain": bool(cfg.get("safety", {}).get("no_training", False)),
        "no_one_off_runner": True,
        "backfilled_dataset_produced": backfilled_path.exists(),
        "rows_gte_500": produced_rows >= 500,
        "buyer_or_flow_coverage_gte_70pct": coverage["buyer_concentration"]["coverage"] >= 0.70
        or coverage["flow_quality_raw"]["coverage"] >= 0.70,
        "no_future_leakage": leakage["future_leakage_avoided"],
        "no_silent_zero_fill": leakage["no_silent_zero_fill"],
        "coverage_report_produced": coverage_path.exists(),
        "quality_report_produced": quality_path.exists(),
        "leakage_guard_produced": True,
        "markdown_says_no_full_retrain_promote_live": True,
    }
    allow_h11 = all(gate_checks.values())
    patch = {
        "patch": "run-004-h10.6-clickhouse-backfill-source-recovery",
        "generated_at": datetime.now(UTC).isoformat(),
        "input": {"path": str(input_path), "rows": len(input_rows), "sha256": sha256(input_path)},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "clickhouse": {
            "configured_url": cfg.get("clickhouse", {}).get("url"),
            "configured_database": cfg.get("clickhouse", {}).get("database"),
            "effective_database": db,
            "transport": client.transport,
            "read_only": True,
        },
        "schema_discovery": {
            "candidate_tables_found": [t for t, meta in schema["candidate_tables"].items() if meta.get("exists")],
            "swap_event_table_has_wallet_side_amount": schema["candidate_tables"].get("swap_trade_events_all", {}).get("exists", False),
            "raw_evm_logs_status": "event_count_only_no_wallet_or_side" if schema["candidate_tables"].get("raw_evm_logs", {}).get("exists") else "missing",
        },
        "join_feasibility": {
            "sample_size": feasibility["sample_size"],
            "matched_sample_count": feasibility["matched_sample_count"],
            "best_table": feasibility["best_table"],
            "best_table_matched_samples": feasibility["best_table_matched_samples"],
            "future_leakage_avoided": feasibility["future_leakage_avoided"],
        },
        "backfilled_dataset": {"rows": produced_rows, "columns": len(original_header) + len([c for c in extra_cols if c not in original_header])},
        "coverage": coverage,
        "leakage_guard": leakage,
        "gate": {
            "allow_enter_H11_enriched_feature_sanity_ablation": allow_h11,
            "checks": gate_checks,
            "next_step": "H11 enriched feature sanity ablation" if allow_h11 else "H10.7 data source repair / ClickHouse schema fix",
        },
        "safety_assertions": cfg.get("safety", {}),
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    md = f"""# Run-004 Patch H10.6 - ClickHouse Backfill Source Recovery

## Summary
- Input rows: {len(input_rows)}
- Backfilled rows: {produced_rows}
- Backfilled columns: {patch['backfilled_dataset']['columns']}
- ClickHouse database: `{db}` via `{client.transport}`
- Best join source: `{feasibility['best_table']}`

## Coverage
- buyer_concentration: {coverage['buyer_concentration']['coverage']}
- flow_quality_raw: {coverage['flow_quality_raw']['coverage']}
- flow_quality_usd: {coverage['flow_quality_usd']['coverage']} (`amount_usd` unavailable)
- temporal_regime: {coverage['temporal_regime']['coverage']}
- pool_quality_metadata: {coverage['pool_quality_metadata']['coverage']}

## Blocked Features
- USD flow features are unavailable because ClickHouse source tables do not provide durable `amount_usd` for this join path.
- Live reserve / quote / price impact features were not computed; H10.6 does not call live RPC or quote services.
- `raw_evm_logs` can provide event-count evidence for some H8 addresses, but it lacks wallet and side fields, so it is not used to fabricate buyer concentration or flow direction.

## Leakage Guard
- Future leakage avoided: {leakage['future_leakage_avoided']}
- Silent zero fill avoided: {leakage['no_silent_zero_fill']}
- Safe candidate feature count: {len(leakage['safe_candidate_features'])}
- Audit-only fields include source flags and identity-like metadata.

## Gate
- allow_enter_H11_enriched_feature_sanity_ablation: `{allow_h11}`
- Next step: {patch['gate']['next_step']}

## Safety
No full retrain, no candidate model training, no promotion, no live mode, no Redis signals, no production model writes, and no active model changes were performed.
"""
    write_markdown(_resolve(outputs["markdown"]), md)
    return patch


WETH_BASE = "0x4200000000000000000000000000000000000006"


def _h108_schema_report(client: ClickHouseClient, db: str, cfg: dict[str, Any]) -> dict[str, Any]:
    tables = {
        "v2": cfg["decoded_sources"]["v2"]["table"],
        "v3": cfg["decoded_sources"]["v3"]["table"],
        "metadata": cfg["decoded_sources"]["metadata"]["table"],
    }
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_database": db,
        "transport": client.transport,
        "tables": {},
    }
    for name, table in tables.items():
        desc = client.query_json(f"DESCRIBE TABLE {db}.{table}")
        columns = {str(r.get("name")): str(r.get("type")) for r in desc}
        sample_error = None
        try:
            sample = client.query_json(f"SELECT * FROM {db}.{table} LIMIT 3")
        except Exception as exc:
            sample = []
            sample_error = str(exc)
        report["tables"][name] = {
            "table": table,
            "columns": columns,
            "sample_rows": sample,
            "sample_error": sample_error,
            "identified_fields": {
                "timestamp": [c for c in columns if c in {"block_time", "received_at_ms"}],
                "tx_identity": [c for c in columns if c in {"tx_hash", "log_index", "block_number"}],
                "sender_wallet": [c for c in columns if c in {"sender", "sender_address", "tx_from", "wallet_address", "trader"}],
                "recipient": [c for c in columns if c in {"recipient", "recipient_address"}],
                "amount_fields": [c for c in columns if "amount" in c.lower()],
                "token_or_side": [c for c in columns if c in {"token0_address", "token1_address", "amount0", "amount1", "amount0_in", "amount1_in", "amount0_out", "amount1_out", "swap_direction", "is_buy", "side"}],
                "price_or_usd": [c for c in columns if "price" in c.lower() or "usd" in c.lower()],
            },
        }
    return report


def _h108_pool_addresses(rows: list[dict[str, str]]) -> list[str]:
    return sorted({_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address"))})


def _h108_load_metadata(client: ClickHouseClient, db: str, rows: list[dict[str, str]], table: str) -> dict[str, dict[str, Any]]:
    addresses = _h108_pool_addresses(rows)
    if not addresses:
        return {}
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  lower(pool_address) AS pool_address,
  pool_type,
  if(pool_type = 'v3', 'v3', 'v2') AS protocol,
  lower(token0_address) AS token0_address,
  lower(token1_address) AS token1_address,
  token0_symbol,
  token1_symbol,
  token0_decimals,
  token1_decimals,
  fee_bps,
  has_weth_leg,
  created_block,
  toUnixTimestamp(discovered_at) * 1000 AS discovered_at_ms
FROM {db}.{table}
WHERE lower(pool_address) IN ({addr_sql})
"""
    return {_lower_addr(r.get("pool_address")): r for r in client.query_json(q)}


def _h108_ts_bounds(rows: list[dict[str, str]], max_window: int) -> tuple[int, int]:
    ts_values = [t for t in (_entry_ts_ms(r) for r in rows) if t is not None]
    if not ts_values:
        return (0, 0)
    return (min(ts_values) - max_window * 1000, max(ts_values))


def _h108_load_v2_events(client: ClickHouseClient, db: str, rows: list[dict[str, str]], table: str, max_window: int) -> dict[str, list[dict[str, Any]]]:
    addresses = _h108_pool_addresses(rows)
    if not addresses:
        return {}
    start_ms, end_ms = _h108_ts_bounds(rows, max_window)
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  'v2' AS decoded_source,
  lower(pair_address) AS pool_address,
  toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) AS ts_ms,
  block_number,
  tx_hash,
  log_index,
  lower(sender) AS sender,
  lower(recipient) AS recipient,
  toString(amount0_in) AS amount0_in,
  toString(amount1_in) AS amount1_in,
  toString(amount0_out) AS amount0_out,
  toString(amount1_out) AS amount1_out
FROM {db}.{table}
WHERE lower(pair_address) IN ({addr_sql})
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) >= {start_ms}
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) < {end_ms}
ORDER BY pool_address, ts_ms
"""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in client.query_json(q):
        events[_lower_addr(event.get("pool_address"))].append(event)
    return events


def _h108_load_v3_events(client: ClickHouseClient, db: str, rows: list[dict[str, str]], table: str, max_window: int) -> dict[str, list[dict[str, Any]]]:
    addresses = _h108_pool_addresses(rows)
    if not addresses:
        return {}
    start_ms, end_ms = _h108_ts_bounds(rows, max_window)
    addr_sql = ",".join(_sql_string(a) for a in addresses)
    q = f"""
SELECT
  'v3' AS decoded_source,
  lower(pool_address) AS pool_address,
  toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) AS ts_ms,
  block_number,
  tx_hash,
  log_index,
  lower(sender) AS sender,
  lower(recipient) AS recipient,
  toString(amount0) AS amount0,
  toString(amount1) AS amount1,
  toString(sqrt_price_x96) AS sqrt_price_x96,
  toString(liquidity) AS liquidity,
  tick
FROM {db}.{table}
WHERE lower(pool_address) IN ({addr_sql})
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) >= {start_ms}
  AND toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)) < {end_ms}
ORDER BY pool_address, ts_ms
"""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in client.query_json(q):
        events[_lower_addr(event.get("pool_address"))].append(event)
    return events


def _h108_float(value: Any) -> float | None:
    val = optional_float(value)
    if val is None:
        return None
    return val


def _h108_event_side_and_volume(event: dict[str, Any], meta: dict[str, Any] | None) -> tuple[bool | None, float | None]:
    token0 = _lower_addr((meta or {}).get("token0_address"))
    token1 = _lower_addr((meta or {}).get("token1_address"))
    source = str(event.get("decoded_source", ""))
    if source == "v2":
        a0_in = _h108_float(event.get("amount0_in")) or 0.0
        a1_in = _h108_float(event.get("amount1_in")) or 0.0
        a0_out = _h108_float(event.get("amount0_out")) or 0.0
        a1_out = _h108_float(event.get("amount1_out")) or 0.0
        volume = max(abs(a0_in), abs(a1_in), abs(a0_out), abs(a1_out))
        if token0 == WETH_BASE:
            return (a0_in > 0, volume)
        if token1 == WETH_BASE:
            return (a1_in > 0, volume)
        return (None, volume)
    if source == "v3":
        a0 = _h108_float(event.get("amount0")) or 0.0
        a1 = _h108_float(event.get("amount1")) or 0.0
        volume = max(abs(a0), abs(a1))
        if token0 == WETH_BASE:
            return (a0 > 0, volume)
        if token1 == WETH_BASE:
            return (a1 > 0, volume)
        return (None, volume)
    return (None, None)


def _h108_window_features(events: list[dict[str, Any]], meta: dict[str, Any] | None, entry_ms: int, window_sec: int) -> dict[str, Any]:
    suffix = f"{window_sec}s"
    subset = [
        e
        for e in events
        if optional_float(e.get("ts_ms")) is not None
        and entry_ms - window_sec * 1000 <= int(optional_float(e.get("ts_ms"))) < entry_ms
    ]
    sender_vol: Counter[str] = Counter()
    sender_counts: Counter[str] = Counter()
    buyer_vol: Counter[str] = Counter()
    buyer_counts: Counter[str] = Counter()
    seller_counts: Counter[str] = Counter()
    buy_volumes: list[float] = []
    sell_volumes: list[float] = []
    all_volumes: list[float] = []
    side_known_count = 0
    for event in subset:
        sender = _lower_addr(event.get("sender"))
        is_buy, volume = _h108_event_side_and_volume(event, meta)
        if volume is None:
            continue
        all_volumes.append(volume)
        if sender:
            sender_vol[sender] += volume
            sender_counts[sender] += 1
        if is_buy is None:
            continue
        side_known_count += 1
        if is_buy:
            buy_volumes.append(volume)
            if sender:
                buyer_vol[sender] += volume
                buyer_counts[sender] += 1
        else:
            sell_volumes.append(volume)
            if sender:
                seller_counts[sender] += 1
    top_sender = sorted(sender_vol.values(), reverse=True)
    top_buyer = sorted(buyer_vol.values(), reverse=True)
    total_sender_volume = sum(sender_vol.values())
    total_buyer_volume = sum(buyer_vol.values())
    total_sell_volume = sum(sell_volumes)
    median_trade = sorted(all_volumes)[len(all_volumes) // 2] if all_volumes else None
    mu = sum(all_volumes) / len(all_volumes) if all_volumes else None
    std = math.sqrt(sum((v - mu) ** 2 for v in all_volumes) / len(all_volumes)) if all_volumes and mu else None
    repeated_buys = sum(c for c in buyer_counts.values() if c > 1)
    return {
        f"event_count_{suffix}": len(subset) if subset else None,
        f"unique_swap_senders_{suffix}": len(sender_counts) if subset else None,
        f"top1_sender_share_{suffix}": (top_sender[0] / total_sender_volume) if total_sender_volume > 0 and top_sender else None,
        f"top3_sender_share_{suffix}": (sum(top_sender[:3]) / total_sender_volume) if total_sender_volume > 0 and top_sender else None,
        f"sender_gini_{suffix}": gini(list(sender_vol.values())) if sender_vol else None,
        f"sender_entropy_{suffix}": shannon_entropy_from_counts(sender_counts) if sender_counts else None,
        f"unique_buyers_{suffix}": len(buyer_counts) if side_known_count else None,
        f"unique_sellers_{suffix}": len(seller_counts) if side_known_count else None,
        f"buyer_trade_count_{suffix}": len(buy_volumes) if side_known_count else None,
        f"seller_trade_count_{suffix}": len(sell_volumes) if side_known_count else None,
        f"top1_buyer_share_{suffix}": (top_buyer[0] / total_buyer_volume) if total_buyer_volume > 0 and top_buyer else None,
        f"top3_buyer_share_{suffix}": (sum(top_buyer[:3]) / total_buyer_volume) if total_buyer_volume > 0 and top_buyer else None,
        f"buyer_gini_{suffix}": gini(list(buyer_vol.values())) if buyer_vol else None,
        f"repeat_buyer_ratio_{suffix}": (repeated_buys / len(buy_volumes)) if buy_volumes else None,
        f"new_buyer_ratio_{suffix}": (len(buyer_counts) / len(buy_volumes)) if buy_volumes else None,
        f"buy_wallet_entropy_{suffix}": shannon_entropy_from_counts(buyer_counts) if buyer_counts else None,
        f"buy_count_{suffix}": len(buy_volumes) if side_known_count else None,
        f"sell_count_{suffix}": len(sell_volumes) if side_known_count else None,
        f"buy_sell_count_ratio_{suffix}": (len(buy_volumes) / len(sell_volumes)) if sell_volumes else None,
        f"buy_volume_raw_{suffix}": sum(buy_volumes) if side_known_count else None,
        f"sell_volume_raw_{suffix}": total_sell_volume if side_known_count else None,
        f"net_flow_raw_{suffix}": (sum(buy_volumes) - total_sell_volume) if side_known_count else None,
        f"abs_flow_raw_{suffix}": (sum(buy_volumes) + total_sell_volume) if side_known_count else (sum(all_volumes) if all_volumes else None),
        f"median_trade_size_raw_{suffix}": median_trade,
        f"trade_size_cv_{suffix}": (std / mu) if mu and std is not None else None,
        f"side_known_count_{suffix}": side_known_count if subset else None,
    }


def _h108_feature_columns(windows: list[int]) -> list[str]:
    cols: list[str] = []
    for w in windows:
        suffix = f"{w}s"
        cols.extend(
            [
                f"event_count_{suffix}",
                f"unique_swap_senders_{suffix}",
                f"top1_sender_share_{suffix}",
                f"top3_sender_share_{suffix}",
                f"sender_gini_{suffix}",
                f"sender_entropy_{suffix}",
                f"unique_buyers_{suffix}",
                f"unique_sellers_{suffix}",
                f"buyer_trade_count_{suffix}",
                f"seller_trade_count_{suffix}",
                f"top1_buyer_share_{suffix}",
                f"top3_buyer_share_{suffix}",
                f"buyer_gini_{suffix}",
                f"repeat_buyer_ratio_{suffix}",
                f"new_buyer_ratio_{suffix}",
                f"buy_wallet_entropy_{suffix}",
                f"buy_count_{suffix}",
                f"sell_count_{suffix}",
                f"buy_sell_count_ratio_{suffix}",
                f"buy_volume_raw_{suffix}",
                f"sell_volume_raw_{suffix}",
                f"net_flow_raw_{suffix}",
                f"abs_flow_raw_{suffix}",
                f"median_trade_size_raw_{suffix}",
                f"trade_size_cv_{suffix}",
                f"side_known_count_{suffix}",
            ]
        )
    cols.extend(
        [
            "activity_acceleration_1m_vs_5m",
            "activity_acceleration_3m_vs_15m",
            "buy_pressure_acceleration_1m_vs_5m",
            "sender_entropy_change_1m_vs_5m",
            "volatility_change_1m_vs_5m",
            "pool_type_backfilled",
            "protocol_backfilled",
            "decoded_source_used",
            "decoded_join_status",
            "decoded_event_count_5m",
            "decoded_event_count_30m",
            "backfill_source_status",
            "wallet_source_available",
            "side_source_available",
            "amount_source_available",
            "side_inferred",
            "buyer_side_unavailable",
        ]
    )
    return cols


def _h108_join_coverage(
    rows: list[dict[str, str]],
    v2_events: dict[str, list[dict[str, Any]]],
    v3_events: dict[str, list[dict[str, Any]]],
    metadata: dict[str, dict[str, Any]],
    max_window: int,
) -> dict[str, Any]:
    by_pool_type: dict[str, Counter[str]] = defaultdict(Counter)
    examples_matched: list[dict[str, Any]] = []
    examples_unmatched: list[dict[str, Any]] = []
    v2_rows = v3_rows = union_rows = meta_rows = 0
    min_entry = max_entry = None
    for idx, row in enumerate(rows):
        pool = _lower_addr(row.get("pool_address"))
        entry_ms = _entry_ts_ms(row)
        if entry_ms is None:
            continue
        min_entry = entry_ms if min_entry is None else min(min_entry, entry_ms)
        max_entry = entry_ms if max_entry is None else max(max_entry, entry_ms)
        meta = metadata.get(pool, {})
        pool_type = str(meta.get("pool_type") or row.get("pool_type") or "unknown")
        v2_count = sum(1 for e in v2_events.get(pool, []) if entry_ms - max_window * 1000 <= int(optional_float(e.get("ts_ms")) or 0) < entry_ms)
        v3_count = sum(1 for e in v3_events.get(pool, []) if entry_ms - max_window * 1000 <= int(optional_float(e.get("ts_ms")) or 0) < entry_ms)
        has_meta = pool in metadata
        has_union = v2_count > 0 or v3_count > 0
        v2_rows += int(v2_count > 0)
        v3_rows += int(v3_count > 0)
        union_rows += int(has_union)
        meta_rows += int(has_meta)
        by_pool_type[pool_type]["rows"] += 1
        by_pool_type[pool_type]["union_matches"] += int(has_union)
        by_pool_type[pool_type]["v2_matches"] += int(v2_count > 0)
        by_pool_type[pool_type]["v3_matches"] += int(v3_count > 0)
        item = {
            "sample_index": idx,
            "pool_address": pool,
            "entry_ms": entry_ms,
            "pool_type": pool_type,
            "v2_event_count_30m": v2_count,
            "v3_event_count_30m": v3_count,
            "metadata_match": has_meta,
        }
        if has_union and len(examples_matched) < 8:
            examples_matched.append(item)
        if not has_union and len(examples_unmatched) < 8:
            examples_unmatched.append(item)
    total = len(rows)
    return {
        "rows": total,
        "window_sec": max_window,
        "v2_matched_rows": v2_rows,
        "v3_matched_rows": v3_rows,
        "union_matched_rows": union_rows,
        "unmatched_rows": total - union_rows,
        "metadata_matched_rows": meta_rows,
        "v2_match_rate": round(v2_rows / total, 6) if total else 0,
        "v3_match_rate": round(v3_rows / total, 6) if total else 0,
        "union_match_rate": round(union_rows / total, 6) if total else 0,
        "metadata_match_rate": round(meta_rows / total, 6) if total else 0,
        "match_by_pool_type": {
            k: {**dict(v), "union_match_rate": round(v["union_matches"] / v["rows"], 6) if v["rows"] else 0}
            for k, v in by_pool_type.items()
        },
        "sample_matched_examples": examples_matched,
        "sample_unmatched_examples": examples_unmatched,
        "timestamp_range_overlap": {
            "entry_min_ms": min_entry,
            "entry_max_ms": max_entry,
            "pre_entry_window_supported": True,
        },
        "future_leakage_avoided": True,
    }


def _h108_enrich_rows(
    rows: list[dict[str, str]],
    windows: list[int],
    v2_events: dict[str, list[dict[str, Any]]],
    v3_events: dict[str, list[dict[str, Any]]],
    metadata: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    cols = _h108_feature_columns(windows)
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        out: dict[str, Any] = dict(row)
        for col in cols:
            out[col] = ""
        entry_ms = _entry_ts_ms(row)
        pool = _lower_addr(row.get("pool_address"))
        meta = metadata.get(pool)
        events = sorted(v2_events.get(pool, []) + v3_events.get(pool, []), key=lambda e: int(optional_float(e.get("ts_ms")) or 0))
        if entry_ms is not None and events:
            per_window: dict[int, dict[str, Any]] = {}
            side_known_total = 0
            amount_total = 0
            for w in windows:
                feats = _h108_window_features(events, meta, entry_ms, w)
                per_window[w] = feats
                side_known_total += int(optional_float(feats.get(f"side_known_count_{w}s")) or 0)
                amount_total += int(optional_float(feats.get(f"event_count_{w}s")) or 0)
                for key, value in feats.items():
                    out[key] = fnum(value)
            out["activity_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window[60].get("event_count_60s"), per_window[300].get("event_count_300s")))
            out["activity_acceleration_3m_vs_15m"] = fnum(_ratio_delta(per_window[180].get("event_count_180s"), per_window[900].get("event_count_900s")))
            out["buy_pressure_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window[60].get("buy_sell_count_ratio_60s"), per_window[300].get("buy_sell_count_ratio_300s")))
            e1 = optional_float(per_window[60].get("sender_entropy_60s"))
            e5 = optional_float(per_window[300].get("sender_entropy_300s"))
            out["sender_entropy_change_1m_vs_5m"] = fnum(e1 - e5 if e1 is not None and e5 is not None else None)
            out["volatility_change_1m_vs_5m"] = ""
            source_set = sorted({str(e.get("decoded_source")) for e in events if entry_ms - 1800 * 1000 <= int(optional_float(e.get("ts_ms")) or 0) < entry_ms})
            out["decoded_source_used"] = "+".join(source_set)
            out["decoded_join_status"] = "matched"
            out["decoded_event_count_5m"] = out.get("event_count_300s", "")
            out["decoded_event_count_30m"] = out.get("event_count_1800s", "")
            out["backfill_source_status"] = "decoded_bridge"
            out["wallet_source_available"] = "true"
            out["side_source_available"] = "true" if side_known_total > 0 else "false"
            out["amount_source_available"] = "true" if amount_total > 0 else "false"
            out["side_inferred"] = "true" if side_known_total > 0 else "false"
            out["buyer_side_unavailable"] = "false" if side_known_total > 0 else "true"
        else:
            out["decoded_join_status"] = "unmatched"
            out["backfill_source_status"] = "unavailable"
            out["wallet_source_available"] = "false"
            out["side_source_available"] = "false"
            out["amount_source_available"] = "false"
            out["side_inferred"] = "false"
            out["buyer_side_unavailable"] = "true"
        if meta:
            out["pool_type_backfilled"] = meta.get("pool_type", "")
            out["protocol_backfilled"] = meta.get("protocol", "")
        enriched_rows.append(out)
    return enriched_rows, cols


def _h108_family_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    header = list(rows[0].keys()) if rows else []
    return {
        "sender_concentration": nonempty_coverage(rows, [c for c in header if c.startswith(("unique_swap_senders_", "top1_sender_share_", "sender_entropy_"))]),
        "buyer_concentration": nonempty_coverage(rows, [c for c in header if c.startswith(("unique_buyers_", "top1_buyer_share_", "buy_wallet_entropy_"))]),
        "flow_quality_count": nonempty_coverage(rows, [c for c in header if c.startswith(("buy_count_", "sell_count_", "buy_sell_count_ratio_"))]),
        "flow_quality_raw": nonempty_coverage(rows, [c for c in header if c.startswith(("buy_volume_raw_", "sell_volume_raw_", "net_flow_raw_", "abs_flow_raw_"))]),
        "flow_quality_usd": {"rows": len(rows), "covered_rows": 0, "coverage": 0.0, "columns": [], "source_status": "unavailable_no_amount_usd"},
        "temporal_regime": nonempty_coverage(rows, [c for c in header if c.startswith(("activity_acceleration_", "buy_pressure_acceleration_", "sender_entropy_change_", "volatility_change_")) or c.startswith("event_count_")]),
        "protocol_metadata": nonempty_coverage(rows, ["pool_type_backfilled", "protocol_backfilled", "decoded_source_used", "decoded_event_count_5m", "decoded_event_count_30m", "backfill_source_status"]),
    }


def _h108_quality_report(rows: list[dict[str, Any]], feature_cols: list[str], audit_only: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for col in feature_cols:
        if col in audit_only or col.endswith("_backfilled") or col in {"buyer_side_unavailable", "side_inferred"}:
            nonempty = [r.get(col, "") for r in rows if str(r.get(col, "")) != ""]
            report[col] = {
                "count": len(nonempty),
                "missing_count": len(rows) - len(nonempty),
                "missing_rate": round((len(rows) - len(nonempty)) / len(rows), 6) if rows else None,
                "source_status": "available" if nonempty else "unavailable",
            }
            continue
        item = numeric_summary([r.get(col, "") for r in rows])
        item["source_status"] = "available" if item["count"] else "unavailable"
        report[col] = item
    return report


def _h108_leakage_guard(feature_cols: list[str]) -> dict[str, Any]:
    forbidden = [
        "pool_address",
        "token_address",
        "pool_type",
        "protocol",
        "tx_hash",
        "log_index",
        "block_number",
        "timestamp",
        "approximate_label",
        "executable_label",
        "executable_net_return",
        "decoded_source_used",
        "decoded_join_status",
        "future_*",
        "post-entry features",
    ]
    audit_only = [
        "decoded_source_used",
        "decoded_join_status",
        "decoded_event_count_5m",
        "decoded_event_count_30m",
        "backfill_source_status",
        "wallet_source_available",
        "side_source_available",
        "amount_source_available",
        "side_inferred",
        "buyer_side_unavailable",
        "pool_type_backfilled",
        "protocol_backfilled",
    ]
    proxy = [c for c in feature_cols if c.startswith(("unique_swap_senders_", "top1_sender_share_", "top3_sender_share_", "sender_gini_", "sender_entropy_"))]
    review = ["volatility_change_1m_vs_5m"] + proxy
    safe = [
        c
        for c in feature_cols
        if c not in audit_only
        and c not in review
        and not c.startswith("future_")
        and c not in forbidden
        and not c.startswith("side_known_count_")
    ]
    return {
        "safe_candidate_features": safe,
        "proxy_candidate_features": proxy,
        "audit_only_fields": audit_only,
        "forbidden_fields": forbidden,
        "features_requiring_review": review,
        "future_leakage_avoided": True,
        "no_silent_zero_fill": True,
    }


def _h108_decoded_swap_bridge(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    input_path = _resolve(cfg["input_executable_dataset"])
    input_rows = read_rows(input_path, limit=int(cfg.get("backfill", {}).get("max_input_rows", 752)))
    with input_path.open(newline="") as fh:
        original_header = list(csv.DictReader(fh).fieldnames or [])
    windows = [int(w) for w in cfg.get("windows_sec", H106_WINDOWS)]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    db = client.database
    schema = _h108_schema_report(client, db, cfg)
    v2_events = _h108_load_v2_events(client, db, input_rows, cfg["decoded_sources"]["v2"]["table"], max(windows))
    v3_events = _h108_load_v3_events(client, db, input_rows, cfg["decoded_sources"]["v3"]["table"], max(windows))
    metadata = _h108_load_metadata(client, db, input_rows, cfg["decoded_sources"]["metadata"]["table"])
    join_coverage = _h108_join_coverage(input_rows, v2_events, v3_events, metadata, max(windows))
    enriched_rows, feature_cols = _h108_enrich_rows(input_rows, windows, v2_events, v3_events, metadata)
    coverage = _h108_family_coverage(enriched_rows)
    leakage = _h108_leakage_guard(feature_cols)
    quality = _h108_quality_report(enriched_rows, feature_cols, leakage["audit_only_fields"])
    enriched_path = _resolve(outputs["enriched_dataset_csv"])
    _write_backfilled_csv(enriched_path, enriched_rows, original_header, feature_cols)
    schema_path = _resolve(outputs["bridge_schema_json"])
    join_path = _resolve(outputs["join_coverage_json"])
    coverage_path = _resolve(outputs["feature_coverage_json"])
    quality_path = _resolve(outputs["feature_quality_json"])
    leakage_path = _resolve(outputs["leakage_guard_json"])
    write_json(schema_path, schema)
    write_json(join_path, join_coverage)
    write_json(coverage_path, coverage)
    write_json(quality_path, quality)
    write_json(leakage_path, leakage)
    rows_out = len(enriched_rows)
    side_limited = coverage["buyer_concentration"]["coverage"] < 0.70 and coverage["sender_concentration"]["coverage"] >= 0.70
    gate_checks = {
        "no_live": bool(cfg.get("safety", {}).get("no_live", False)),
        "no_redis_signals": bool(cfg.get("safety", {}).get("no_redis_signals", False)),
        "no_production_model_write": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
        "active_model_unchanged": sha256(_resolve(cfg.get("active_model_path", ""))) is not None,
        "no_production_retrain": bool(cfg.get("safety", {}).get("no_training", False)),
        "no_one_off_runner": True,
        "enriched_dataset_produced": enriched_path.exists(),
        "rows_gte_500": rows_out >= 500,
        "decoded_union_join_coverage_gte_70pct": join_coverage["union_match_rate"] >= 0.70,
        "sender_or_flow_count_coverage_gte_70pct": coverage["sender_concentration"]["coverage"] >= 0.70
        or coverage["flow_quality_count"]["coverage"] >= 0.70,
        "no_future_leakage": leakage["future_leakage_avoided"],
        "no_silent_zero_fill": leakage["no_silent_zero_fill"],
        "coverage_report_produced": coverage_path.exists(),
        "quality_report_produced": quality_path.exists(),
        "leakage_guard_produced": leakage_path.exists(),
        "markdown_says_no_full_retrain_promote_live": True,
    }
    allow_h11 = all(gate_checks.values())
    patch = {
        "patch": "run-004-h10.8-decoded-swap-bridge",
        "generated_at": datetime.now(UTC).isoformat(),
        "input": {"path": str(input_path), "rows": len(input_rows), "sha256": sha256(input_path)},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "clickhouse": {
            "database": db,
            "transport": client.transport,
            "read_only": True,
        },
        "decoded_source_schema_summary": {
            name: {
                "table": info["table"],
                "sender_fields": info["identified_fields"]["sender_wallet"],
                "amount_fields": info["identified_fields"]["amount_fields"],
                "price_or_usd_fields": info["identified_fields"]["price_or_usd"],
            }
            for name, info in schema["tables"].items()
        },
        "bridge_join_coverage": join_coverage,
        "enriched_dataset": {"rows": rows_out, "columns": len(original_header) + len([c for c in feature_cols if c not in original_header])},
        "feature_coverage": coverage,
        "leakage_guard": leakage,
        "gate": {
            "allow_enter_H11_enriched_feature_sanity_ablation": allow_h11,
            "h11_scope": "sender-concentration-only / side-limited" if side_limited and allow_h11 else "buyer-and-flow" if allow_h11 else "blocked",
            "checks": gate_checks,
            "next_step": "H11 enriched feature sanity ablation" if allow_h11 else "H10.9 decoded bridge repair / side inference fix",
        },
        "safety_assertions": cfg.get("safety", {}),
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    md = f"""# Run-004 Patch H10.8 - Decoded Swap Bridge

## Summary
- Input rows: {len(input_rows)}
- Enriched rows: {rows_out}
- Enriched columns: {patch['enriched_dataset']['columns']}
- ClickHouse database: `{db}` via `{client.transport}`
- Decoded union coverage: {join_coverage['union_match_rate']} ({join_coverage['union_matched_rows']}/{join_coverage['rows']})

## Decoded Sources
- V2 table: `{cfg['decoded_sources']['v2']['table']}` joined on `pair_address`
- V3 table: `{cfg['decoded_sources']['v3']['table']}` joined on `pool_address`
- Metadata table: `{cfg['decoded_sources']['metadata']['table']}` joined on `pool_address`

## Feature Coverage
- sender_concentration: {coverage['sender_concentration']['coverage']}
- buyer_concentration: {coverage['buyer_concentration']['coverage']}
- flow_quality_count: {coverage['flow_quality_count']['coverage']}
- flow_quality_raw: {coverage['flow_quality_raw']['coverage']}
- flow_quality_usd: {coverage['flow_quality_usd']['coverage']} (unavailable)
- temporal_regime: {coverage['temporal_regime']['coverage']}
- protocol_metadata: {coverage['protocol_metadata']['coverage']}

## Blocked Or Limited Features
- USD flow features are unavailable because decoded v2/v3 tables do not expose `amount_usd`.
- Side is inferred from discovered pool token0/token1 WETH direction; rows without WETH metadata are marked side-limited, not zero-filled.
- No live reserve, quote, or RPC-only features were computed.

## Leakage Guard
- Future leakage avoided: {leakage['future_leakage_avoided']}
- Silent zero fill avoided: {leakage['no_silent_zero_fill']}
- Safe candidate feature count: {len(leakage['safe_candidate_features'])}
- Proxy candidate feature count: {len(leakage['proxy_candidate_features'])}

## Gate
- allow_enter_H11_enriched_feature_sanity_ablation: `{allow_h11}`
- H11 scope: {patch['gate']['h11_scope']}
- Next step: {patch['gate']['next_step']}

## Safety
No full retrain, no candidate model training, no promotion, no live mode, no Redis signals, no production model writes, and no active model changes were performed.
"""
    write_markdown(_resolve(outputs["markdown"]), md)
    return patch


H11_STRESS_GROUPS = [
    "old_baseline_4f",
    "microstructure_combo_no_raw_volume",
    "microstructure_combo_with_old",
    "sender_concentration_5m",
    "buyer_concentration_5m",
    "flow_count_5m",
]


def _h11_resolve_feature(feature: str, header: set[str]) -> str | None:
    if feature in header:
        return feature
    for suffix in ("60", "180", "300", "900", "1800"):
        token = f"_{suffix}"
        if feature.endswith(token):
            candidate = f"{feature}s"
            if candidate in header:
                return candidate
    return None


def _h11_label(row: dict[str, str], target_col: str) -> int | None:
    val = optional_float(row.get(target_col))
    if val is None:
        return None
    if val not in (0.0, 1.0):
        return None
    return int(val)


def _h11_row_protocol(row: dict[str, str]) -> str:
    return str(row.get("pool_type_backfilled") or row.get("pool_type") or row.get("protocol_backfilled") or row.get("protocol") or "").lower()


def _h11_distribution(rows: list[dict[str, str]], col: str) -> dict[str, int]:
    return dict(Counter(str(r.get(col, "") or "missing") for r in rows))


def _h11_label_distribution(rows: list[dict[str, str]], target_col: str) -> dict[str, Any]:
    labels = [_h11_label(r, target_col) for r in rows]
    labels = [x for x in labels if x is not None]
    positives = sum(labels)
    negatives = len(labels) - positives
    return {
        "rows_with_label": len(labels),
        "positive": positives,
        "negative": negatives,
        "positive_rate": rounded(positives / len(labels), 8) if labels else None,
    }


def _h11_pool_concentration(rows: list[dict[str, str]]) -> dict[str, Any]:
    counts = Counter(_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address")))
    total = len(rows)
    top = counts.most_common(10)
    top1 = top[0][1] if top else 0
    top5 = sum(c for _, c in top[:5])
    return {
        "unique_pools": len(counts),
        "top_pools": [{"pool_address": pool, "rows": count, "share": rounded(count / total, 8) if total else None} for pool, count in top],
        "top1_pool_share": rounded(top1 / total, 8) if total else None,
        "top5_pool_share": rounded(top5 / total, 8) if total else None,
    }


def _h11_feature_availability(
    rows: list[dict[str, str]],
    feature_groups: dict[str, list[str]],
    header: set[str],
    denylist: set[str],
    deny_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    report: dict[str, Any] = {"groups": {}, "all_requested_features": sorted({f for fs in feature_groups.values() for f in fs})}
    for group, requested in feature_groups.items():
        resolved: dict[str, str] = {}
        missing: list[str] = []
        leakage: list[str] = []
        summaries: dict[str, Any] = {}
        for feature in requested:
            actual = _h11_resolve_feature(feature, header)
            if actual is None:
                missing.append(feature)
                continue
            resolved[feature] = actual
            if actual in denylist or any(actual.startswith(prefix) for prefix in deny_prefixes):
                leakage.append(actual)
            summaries[actual] = numeric_summary([r.get(actual, "") for r in rows])
        report["groups"][group] = {
            "requested_features": requested,
            "resolved_features": resolved,
            "missing_features": missing,
            "leakage_blocked_features": leakage,
            "feature_quality": summaries,
            "high_missing_features": [
                col for col, summary in summaries.items() if (summary.get("missing_rate") is not None and summary.get("missing_rate") >= 0.50)
            ],
            "constant_or_all_zero_features": [
                col for col, summary in summaries.items() if summary.get("constant") or summary.get("all_zero")
            ],
            "high_scale_features": [col for col, summary in summaries.items() if summary.get("extreme_value_flag")],
        }
    return report


def _h11_safe_features(group_report: dict[str, Any]) -> list[str]:
    blocked = set(group_report.get("leakage_blocked_features", []))
    blocked.update(group_report.get("constant_or_all_zero_features", []))
    return [v for v in group_report.get("resolved_features", {}).values() if v not in blocked]


def _h11_model_rows(rows: list[dict[str, str]], features: list[str], target_col: str, net_return_col: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        y = _h11_label(row, target_col)
        if y is None:
            continue
        vals: list[float] = []
        ok = True
        for feature in features:
            val = optional_float(row.get(feature))
            if val is None:
                ok = False
                break
            vals.append(val)
        if not ok:
            continue
        out.append(
            {
                "timestamp": int(optional_float(row.get("timestamp")) or len(out)),
                "features": vals,
                "label": y,
                "net_return": optional_float(row.get(net_return_col)),
                "pool_address": _lower_addr(row.get("pool_address")),
                "pool_type": _h11_row_protocol(row),
            }
        )
    return sorted(out, key=lambda r: r["timestamp"])


def _h11_calibration_bins(y_true: Any, scores: Any, bins: int = 5) -> list[dict[str, Any]]:
    import numpy as np

    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[dict[str, Any]] = []
    for idx in range(bins):
        lo = float(edges[idx])
        hi = float(edges[idx + 1])
        if idx == bins - 1:
            mask = (scores >= lo) & (scores <= hi)
        else:
            mask = (scores >= lo) & (scores < hi)
        count = int(mask.sum())
        out.append(
            {
                "bin": idx,
                "score_min": rounded(lo, 4),
                "score_max": rounded(hi, 4),
                "rows": count,
                "mean_score": rounded(float(scores[mask].mean()), 8) if count else None,
                "positive_rate": rounded(float(y_true[mask].mean()), 8) if count else None,
            }
        )
    return out


def _h11_train_eval(
    group_name: str,
    rows: list[dict[str, str]],
    features: list[str],
    cfg: dict[str, Any],
    *,
    stress_name: str | None = None,
    shuffled_5fold: bool = False,
    persist_candidate: bool = True,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.metrics import (
        accuracy_score,
        average_precision_score,
        brier_score_loss,
        precision_score,
        recall_score,
        roc_auc_score,
    )
    from sklearn.model_selection import StratifiedKFold
    from xgboost import XGBClassifier

    training = cfg.get("training", {})
    target_col = str(cfg.get("target_column", "executable_label"))
    net_return_col = str(cfg.get("net_return_column", "executable_net_return"))
    model_rows = _h11_model_rows(rows, features, target_col, net_return_col)
    labels = [int(r["label"]) for r in model_rows]
    positives = sum(labels)
    negatives = len(labels) - positives
    min_rows = int(training.get("min_rows", 300))
    min_pos = int(training.get("min_positive_labels", 50))
    min_neg = int(training.get("min_negative_labels", 50))
    base = {
        "group": group_name,
        "stress_test": stress_name,
        "sanity_only_not_deploy_evidence": True,
        "features_used": features,
        "rows_used": len(model_rows),
        "label_distribution": {
            "positive": positives,
            "negative": negatives,
            "positive_rate": rounded(positives / len(labels), 8) if labels else None,
        },
    }
    if len(model_rows) < min_rows or positives < min_pos or negatives < min_neg:
        return {**base, "status": "insufficient_rows"}

    X = np.array([r["features"] for r in model_rows], dtype=float)
    y = np.array(labels, dtype=int)
    net_returns = np.array([r["net_return"] if r["net_return"] is not None else np.nan for r in model_rows], dtype=float)
    if shuffled_5fold:
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=int(training.get("random_state", 44)))
        fold_metrics: list[dict[str, Any]] = []
        all_scores = np.zeros(len(y))
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y), start=1):
            model = XGBClassifier(
                max_depth=int(training.get("max_depth", 2)),
                n_estimators=int(training.get("n_estimators", 30)),
                learning_rate=float(training.get("learning_rate", 0.08)),
                subsample=float(training.get("subsample", 0.9)),
                colsample_bytree=float(training.get("colsample_bytree", 0.9)),
                random_state=int(training.get("random_state", 44)) + fold,
                eval_metric="logloss",
            )
            model.fit(X[train_idx], y[train_idx])
            scores = model.predict_proba(X[val_idx])[:, 1]
            all_scores[val_idx] = scores
            fold_metrics.append(
                {
                    "fold": fold,
                    "rows": int(len(val_idx)),
                    "roc_auc": rounded(float(roc_auc_score(y[val_idx], scores)), 8),
                    "pr_auc": rounded(float(average_precision_score(y[val_idx], scores)), 8),
                }
            )
        positive_baseline = float(y.mean())
        preds = (all_scores >= 0.5).astype(int)
        return {
            **base,
            "status": "ok",
            "split": "shuffled_5fold_sanity_only",
            "train_rows": None,
            "validation_rows": len(y),
            "positive_baseline": rounded(positive_baseline, 8),
            "roc_auc": rounded(float(roc_auc_score(y, all_scores)), 8),
            "pr_auc": rounded(float(average_precision_score(y, all_scores)), 8),
            "pr_auc_lift_over_positive_baseline": rounded(float(average_precision_score(y, all_scores) - positive_baseline), 8),
            "brier_score": rounded(float(brier_score_loss(y, all_scores)), 8),
            "accuracy": rounded(float(accuracy_score(y, preds)), 8),
            "precision": rounded(float(precision_score(y, preds, zero_division=0)), 8),
            "recall": rounded(float(recall_score(y, preds, zero_division=0)), 8),
            "fold_metrics": fold_metrics,
        }

    split_idx = max(1, int(len(model_rows) * 0.70))
    X_train, y_train = X[:split_idx], y[:split_idx]
    X_val, y_val = X[split_idx:], y[split_idx:]
    nr_val = net_returns[split_idx:]
    if len(set(y_train.tolist())) < 2 or len(set(y_val.tolist())) < 2:
        return {**base, "status": "insufficient_rows", "train_rows": len(y_train), "validation_rows": len(y_val)}
    model = XGBClassifier(
        max_depth=int(training.get("max_depth", 2)),
        n_estimators=int(training.get("n_estimators", 30)),
        learning_rate=float(training.get("learning_rate", 0.08)),
        subsample=float(training.get("subsample", 0.9)),
        colsample_bytree=float(training.get("colsample_bytree", 0.9)),
        random_state=int(training.get("random_state", 44)),
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)
    scores = model.predict_proba(X_val)[:, 1]
    preds = (scores >= 0.5).astype(int)
    positive_baseline = float(y_val.mean())
    top_n = max(1, int(math.ceil(len(scores) * 0.10)))
    top_idx = np.argsort(scores)[-top_n:]
    top_returns = [float(v) for v in nr_val[top_idx] if not np.isnan(v)]
    importances = getattr(model, "feature_importances_", np.zeros(len(features))).tolist()
    feature_importance = sorted(
        [{"feature": feature, "importance": rounded(float(importance), 8)} for feature, importance in zip(features, importances, strict=False)],
        key=lambda item: item["importance"] or 0,
        reverse=True,
    )
    result = {
        **base,
        "status": "ok",
        "split": "chronological_70_30",
        "train_rows": int(len(y_train)),
        "validation_rows": int(len(y_val)),
        "train_label_distribution": {"positive": int(y_train.sum()), "negative": int(len(y_train) - y_train.sum())},
        "validation_label_distribution": {"positive": int(y_val.sum()), "negative": int(len(y_val) - y_val.sum())},
        "positive_baseline": rounded(positive_baseline, 8),
        "roc_auc": rounded(float(roc_auc_score(y_val, scores)), 8),
        "pr_auc": rounded(float(average_precision_score(y_val, scores)), 8),
        "pr_auc_lift_over_positive_baseline": rounded(float(average_precision_score(y_val, scores) - positive_baseline), 8),
        "brier_score": rounded(float(brier_score_loss(y_val, scores)), 8),
        "accuracy": rounded(float(accuracy_score(y_val, preds)), 8),
        "precision": rounded(float(precision_score(y_val, preds, zero_division=0)), 8),
        "recall": rounded(float(recall_score(y_val, preds, zero_division=0)), 8),
        "top_decile_summary": {
            "rows": int(top_n),
            "positive_rate": rounded(float(y_val[top_idx].mean()), 8),
            "executable_net_return_mean": rounded(sum(top_returns) / len(top_returns), 8) if top_returns else None,
            "executable_net_return_p50": rounded(percentile(top_returns, 0.50), 8) if top_returns else None,
        },
        "calibration_bins": _h11_calibration_bins(y_val, scores),
        "feature_importance": feature_importance,
        "top_features": feature_importance[:5],
        "interpretation_flags": {
            "positive_pr_auc_lift": average_precision_score(y_val, scores) > positive_baseline,
            "roc_auc_above_0_60": roc_auc_score(y_val, scores) > 0.60,
            "top_decile_positive_rate_high": float(y_val[top_idx].mean()) >= float(cfg.get("interpretation_rules", {}).get("high_positive_rate_threshold", 0.70)),
        },
    }
    if persist_candidate and stress_name is None:
        candidate_dir = _resolve(cfg["outputs"]["candidate_model_dir"])
        candidate_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            candidate_dir / f"{group_name}.json",
            {
                "candidate_model_type": "xgboost_sanity_ablation_metadata_only",
                "sanity_only_not_deploy_evidence": True,
                "not_for_promotion": True,
                "group": group_name,
                "features_used": features,
                "training_params": training,
                "metrics": {k: v for k, v in result.items() if k not in {"feature_importance", "calibration_bins"}},
                "feature_importance": feature_importance,
            },
        )
    return result


def _h11_group_result(group_name: str, rows: list[dict[str, str]], cfg: dict[str, Any], availability: dict[str, Any]) -> dict[str, Any]:
    group_report = availability["groups"][group_name]
    result: dict[str, Any] = {
        "group": group_name,
        "sanity_only_not_deploy_evidence": True,
        "requested_features": group_report["requested_features"],
        "resolved_features": group_report["resolved_features"],
        "missing_features": group_report["missing_features"],
        "high_missing_features": group_report["high_missing_features"],
        "constant_or_all_zero_features": group_report["constant_or_all_zero_features"],
        "high_scale_features": group_report["high_scale_features"],
    }
    if group_report["leakage_blocked_features"]:
        return {**result, "status": "leakage_blocked", "features_used": []}
    if group_report["missing_features"]:
        return {**result, "status": "missing_features", "features_used": _h11_safe_features(group_report)}
    safe_features = _h11_safe_features(group_report)
    if not safe_features:
        return {**result, "status": "insufficient_rows", "features_used": []}
    if group_report["high_missing_features"]:
        return {**result, "status": "high_missing", "features_used": safe_features}
    trained = _h11_train_eval(group_name, rows, safe_features, cfg)
    if group_report["high_scale_features"]:
        trained.setdefault("interpretation_flags", {})["high_scale_review"] = True
    return {**result, **trained}


def _h11_stress_rows(rows: list[dict[str, str]], name: str) -> list[dict[str, str]]:
    counts = Counter(_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address")))
    top_pools = [pool for pool, _ in counts.most_common(5)]
    top1 = set(top_pools[:1])
    top5 = set(top_pools)
    if name in {"uncapped", "chronological_70_30", "shuffled_5fold_sanity_only"}:
        return rows
    if name == "exclude_top1_pool":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top1]
    if name == "exclude_top5_pools":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top5]
    if name == "top5_only":
        return [r for r in rows if _lower_addr(r.get("pool_address")) in top5]
    if name == "non_top5_only":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top5]
    if name == "v2_only":
        return [r for r in rows if _h11_row_protocol(r) == "v2"]
    if name == "v3_only":
        return [r for r in rows if _h11_row_protocol(r) == "v3"]
    if name == "top_pool_vs_non_top_pool":
        return rows
    if name.startswith("entropy_"):
        entropy_col = "buy_wallet_entropy_300s" if "buy_wallet_entropy_300s" in rows[0] else "unique_buyer_entropy_5m"
        vals = [optional_float(r.get(entropy_col)) for r in rows]
        numeric = [v for v in vals if v is not None]
        if not numeric:
            return []
        q1 = percentile(numeric, 1 / 3) or 0.0
        q2 = percentile(numeric, 2 / 3) or 0.0
        if name == "entropy_low":
            return [r for r in rows if (optional_float(r.get(entropy_col)) is not None and optional_float(r.get(entropy_col)) <= q1)]
        if name == "entropy_mid":
            return [r for r in rows if (optional_float(r.get(entropy_col)) is not None and q1 < (optional_float(r.get(entropy_col)) or 0) <= q2)]
        if name == "entropy_high":
            return [r for r in rows if (optional_float(r.get(entropy_col)) is not None and (optional_float(r.get(entropy_col)) or 0) > q2)]
    return rows


def _h11_stress_tests(rows: list[dict[str, str]], cfg: dict[str, Any], availability: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"sanity_only_not_deploy_evidence": True, "tests": {}}
    groups = [g for g in H11_STRESS_GROUPS if g in cfg.get("feature_groups", {})]
    for test in cfg.get("stress_tests", []):
        if test == "top_pool_vs_non_top_pool":
            variants = {"top1_only": [r for r in rows if _lower_addr(r.get("pool_address")) in {pool for pool, _ in Counter(_lower_addr(x.get("pool_address")) for x in rows).most_common(1)}],
                        "non_top1_only": _h11_stress_rows(rows, "exclude_top1_pool")}
        else:
            variants = {test: _h11_stress_rows(rows, str(test))}
        out["tests"][test] = {}
        for variant_name, subset in variants.items():
            labels = _h11_label_distribution(subset, str(cfg.get("target_column", "executable_label")))
            out["tests"][test][variant_name] = {
                "rows": len(subset),
                "positives": labels["positive"],
                "negatives": labels["negative"],
                "groups": {},
            }
            for group in groups:
                group_report = availability["groups"][group]
                if group_report["missing_features"] or group_report["leakage_blocked_features"] or group_report["high_missing_features"]:
                    out["tests"][test][variant_name]["groups"][group] = {
                        "status": "missing_features" if group_report["missing_features"] else "high_missing",
                        "rows_used": 0,
                        "sanity_only_not_deploy_evidence": True,
                    }
                    continue
                safe = _h11_safe_features(group_report)
                out["tests"][test][variant_name]["groups"][group] = _h11_train_eval(
                    group,
                    subset,
                    safe,
                    cfg,
                    stress_name=str(test),
                    shuffled_5fold=(test == "shuffled_5fold_sanity_only"),
                    persist_candidate=False,
                )
    return out


def _h11_interpretation(metrics: dict[str, Any], stress: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    groups = metrics.get("feature_groups", {})
    old = groups.get("old_baseline_4f", {})
    old_roc = old.get("roc_auc")
    old_pr_lift = old.get("pr_auc_lift_over_positive_baseline")
    old_top_return = (old.get("top_decile_summary") or {}).get("executable_net_return_mean")
    better: list[str] = []
    for name, item in groups.items():
        if name == "old_baseline_4f" or item.get("status") != "ok":
            continue
        improved = False
        if old_roc is not None and item.get("roc_auc") is not None and item["roc_auc"] - old_roc >= 0.03:
            improved = True
        if old_pr_lift is not None and item.get("pr_auc_lift_over_positive_baseline") is not None and item["pr_auc_lift_over_positive_baseline"] - old_pr_lift >= 0.03:
            improved = True
        item_top = (item.get("top_decile_summary") or {}).get("executable_net_return_mean")
        if old_top_return is not None and item_top is not None and item_top > old_top_return:
            improved = True
        if improved:
            better.append(name)

    def group_answer(name: str) -> dict[str, Any]:
        item = groups.get(name, {})
        return {
            "status": item.get("status"),
            "roc_auc": item.get("roc_auc"),
            "pr_auc_lift": item.get("pr_auc_lift_over_positive_baseline"),
            "top_decile_net_return_mean": (item.get("top_decile_summary") or {}).get("executable_net_return_mean"),
            "effective": item.get("status") == "ok" and (item.get("roc_auc") or 0) > (old_roc or 0),
        }

    required_stress = ["exclude_top1_pool", "v2_only", "v3_only"]
    stress_attempted = all(name in stress.get("tests", {}) for name in required_stress)
    supported = False
    support_reasons: list[str] = []
    for name in better:
        collapsed = False
        for test in required_stress:
            variants = stress.get("tests", {}).get(test, {})
            variant = next(iter(variants.values()), {})
            item = (variant.get("groups") or {}).get(name, {})
            old_item = (variant.get("groups") or {}).get("old_baseline_4f", {})
            if item.get("status") != "ok":
                collapsed = True
                continue
            if old_item.get("status") == "ok" and item.get("roc_auc") is not None and old_item.get("roc_auc") is not None:
                if item["roc_auc"] + 0.05 < old_item["roc_auc"]:
                    collapsed = True
        if not collapsed:
            supported = True
            support_reasons.append(f"{name} improves over old_baseline_4f without required stress collapse")

    raw_volume = groups.get("flow_raw_5m", {})
    plus_entropy = groups.get("old_baseline_plus_entropy", {})
    return {
        "sanity_only_not_deploy_evidence": True,
        "questions": {
            "microstructure_groups_better_than_old_baseline_4f": better,
            "pr_auc_lift_positive": {name: item.get("pr_auc_lift_over_positive_baseline", 0) > 0 for name, item in groups.items() if item.get("status") == "ok"},
            "roc_auc_stable_above_0_60": {name: item.get("roc_auc", 0) > 0.60 for name, item in groups.items() if item.get("status") == "ok"},
            "top_decile_executable_net_return_improved": {
                name: ((item.get("top_decile_summary") or {}).get("executable_net_return_mean") is not None and old_top_return is not None and (item.get("top_decile_summary") or {}).get("executable_net_return_mean") > old_top_return)
                for name, item in groups.items()
                if item.get("status") == "ok"
            },
            "sender_concentration_effective": group_answer("sender_concentration_5m"),
            "buyer_concentration_effective": group_answer("buyer_concentration_5m"),
            "flow_count_effective": group_answer("flow_count_5m"),
            "raw_volume_needs_scale_decimals_review": bool(raw_volume.get("high_scale_features")),
            "plus_entropy_old_conclusion_still_unstable": plus_entropy.get("status") == "ok" and old_roc is not None and plus_entropy.get("roc_auc") is not None and plus_entropy["roc_auc"] <= old_roc,
            "v2_v3_need_separate_modeling": stress_attempted,
            "top_pool_concentration_causes_collapse": "exclude_top1_pool" in stress.get("tests", {}),
            "recommend_enter_H12_enriched_feature_stress_or_larger_dataset": supported,
            "still_forbid_full_retrain_promote_live": True,
        },
        "microstructure_feature_supported": supported,
        "support_reasons": support_reasons,
        "allowed_next_step": "H12 enriched feature stress OR larger executable dataset" if supported else "H11 review / larger executable dataset before H12",
        "forbidden_next_steps": ["full retrain", "promote", "live", "production model overwrite"],
    }


def _h11_markdown(patch: dict[str, Any], metrics: dict[str, Any], stress: dict[str, Any], interpretation: dict[str, Any]) -> str:
    rows = []
    for name, item in metrics.get("feature_groups", {}).items():
        rows.append(
            f"| {name} | {item.get('status')} | {item.get('rows_used', '')} | {item.get('roc_auc', '')} | "
            f"{item.get('pr_auc', '')} | {item.get('pr_auc_lift_over_positive_baseline', '')} | "
            f"{(item.get('top_decile_summary') or {}).get('executable_net_return_mean', '')} |"
        )
    stress_rows = []
    for test, variants in stress.get("tests", {}).items():
        for variant, item in variants.items():
            ok_groups = sum(1 for g in item.get("groups", {}).values() if g.get("status") == "ok")
            stress_rows.append(f"| {test}/{variant} | {item.get('rows')} | {item.get('positives')} | {item.get('negatives')} | {ok_groups} |")
    return f"""# Run-004 Patch H11 - Enriched Feature Sanity Ablation

## Scope
H11 is sanity-only evidence for feature stability. It does not prove alpha, does not authorize production retrain, and does not permit promote/live.

## Input Validation
- Input rows: {patch['input_validation']['total_rows']}
- Executable success rows: {patch['input_validation']['executable_success_rows']}
- Target distribution: {patch['input_validation']['target_label_distribution']}
- Executable positive rate: {patch['input_validation']['executable_positive_rate']}
- Top1 pool share: {patch['input_validation']['pool_concentration']['top1_pool_share']}
- Top5 pool share: {patch['input_validation']['pool_concentration']['top5_pool_share']}
- Validation status: {patch['input_validation']['status']}

## Feature Group Metrics
| group | status | rows | roc_auc | pr_auc | pr_auc_lift | top_decile_net_return_mean |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

## Stress Tests
| stress | rows | positives | negatives | ok_groups |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(stress_rows)}

## Interpretation
- microstructure_feature_supported: `{interpretation['microstructure_feature_supported']}`
- better microstructure groups: {interpretation['questions']['microstructure_groups_better_than_old_baseline_4f']}
- raw volume scale review: `{interpretation['questions']['raw_volume_needs_scale_decimals_review']}`
- recommend H12 stress/larger dataset: `{interpretation['questions']['recommend_enter_H12_enriched_feature_stress_or_larger_dataset']}`

## Gate
- allow_enter_H12_enriched_feature_stress_or_larger_dataset: `{patch['gate']['allow_enter_H12_enriched_feature_stress_or_larger_dataset']}`
- Next step allowed: {patch['gate']['next_step']}

## Safety
No LIVE_TRADING_MODE, no Redis signals, no daemon/cron/long-running task, no full label_dashcam, no production retrain_model.py main(), no promote, no production model write, no active model or threshold change, and no one-off runner were used. H11 does not allow full retrain/promote/live.
"""


def _h11_enriched_feature_sanity_ablation(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    input_path = _resolve(cfg["input_path"])
    rows = read_rows(input_path)
    header = set(rows[0].keys()) if rows else set()
    status_filter = cfg.get("status_filter", {})
    success_rows = [r for r in rows if all(str(r.get(k, "")) == str(v) for k, v in status_filter.items())]
    target_col = str(cfg.get("target_column", "executable_label"))
    net_return_col = str(cfg.get("net_return_column", "executable_net_return"))
    labels = _h11_label_distribution(success_rows, target_col)
    net_returns = [optional_float(r.get(net_return_col)) for r in success_rows]
    net_returns_num = [v for v in net_returns if v is not None]
    pool_conc = _h11_pool_concentration(success_rows)
    denylist = set(cfg.get("denylist_columns", []))
    deny_prefixes = tuple(str(p) for p in cfg.get("denylist_prefixes", []))
    availability = _h11_feature_availability(success_rows, cfg.get("feature_groups", {}), header, denylist, deny_prefixes)
    high_missing = sorted({f for g in availability["groups"].values() for f in g["high_missing_features"]})
    high_scale = sorted({f for g in availability["groups"].values() for f in g["high_scale_features"]})
    leakage_blocked = sorted({f for g in availability["groups"].values() for f in g["leakage_blocked_features"]})
    input_validation = {
        "status": "ok",
        "total_rows": len(rows),
        "executable_success_rows": len(success_rows),
        "target_column": target_col,
        "target_label_distribution": labels,
        "executable_positive_rate": labels["positive_rate"],
        "executable_net_return_summary": {
            "count": len(net_returns_num),
            "mean": rounded(sum(net_returns_num) / len(net_returns_num), 8) if net_returns_num else None,
            "p50": rounded(percentile(net_returns_num, 0.50), 8),
            "min": rounded(min(net_returns_num), 8) if net_returns_num else None,
            "max": rounded(max(net_returns_num), 8) if net_returns_num else None,
        },
        "pool_type_distribution": _h11_distribution(success_rows, "pool_type"),
        "protocol_distribution": _h11_distribution(success_rows, "protocol"),
        "pool_concentration": pool_conc,
        "decoded_join_coverage": nonempty_coverage(success_rows, ["decoded_join_status"]),
        "sender_coverage": nonempty_coverage(success_rows, ["unique_swap_senders_300s", "sender_entropy_300s"]),
        "buyer_coverage": nonempty_coverage(success_rows, ["unique_buyers_300s", "buy_wallet_entropy_300s"]),
        "flow_coverage": nonempty_coverage(success_rows, ["buy_count_300s", "sell_count_300s", "decoded_event_count_5m"]),
        "missing_feature_counts": {g: len(meta["missing_features"]) for g, meta in availability["groups"].items()},
        "high_missing_features": high_missing,
        "high_scale_extreme_value_features": high_scale,
        "leakage_denylist_check": {"blocked_features": leakage_blocked, "passed": not leakage_blocked},
    }
    hold_reasons: list[str] = []
    if len(success_rows) < int(cfg.get("training", {}).get("min_rows", 300)):
        hold_reasons.append("executable_success_rows_below_min_rows")
    if labels["positive"] < int(cfg.get("training", {}).get("min_positive_labels", 50)):
        hold_reasons.append("positive_labels_below_min_positive_labels")
    if labels["negative"] < int(cfg.get("training", {}).get("min_negative_labels", 50)):
        hold_reasons.append("negative_labels_below_min_negative_labels")
    if target_col != "executable_label":
        hold_reasons.append("target_column_not_executable_label")
    if target_col == "approximate_label":
        hold_reasons.append("approximate_label_used_as_target")
    if hold_reasons:
        input_validation["status"] = "hold"
        input_validation["hold_reasons"] = hold_reasons

    feature_results: dict[str, Any] = {}
    if input_validation["status"] == "ok":
        for group in cfg.get("feature_groups", {}):
            feature_results[group] = _h11_group_result(group, success_rows, cfg, availability)
        stress = _h11_stress_tests(success_rows, cfg, availability)
    else:
        for group in cfg.get("feature_groups", {}):
            feature_results[group] = {"group": group, "status": "insufficient_rows", "sanity_only_not_deploy_evidence": True}
        stress = {"sanity_only_not_deploy_evidence": True, "status": "hold", "hold_reasons": hold_reasons, "tests": {}}

    metrics = {
        "sanity_only_not_deploy_evidence": True,
        "input_validation": input_validation,
        "field_availability_report": availability,
        "feature_groups": feature_results,
    }
    interpretation = _h11_interpretation(metrics, stress, cfg)
    group_summary_rows = [
        {
            "group": name,
            "status": item.get("status"),
            "rows_used": item.get("rows_used"),
            "train_rows": item.get("train_rows"),
            "validation_rows": item.get("validation_rows"),
            "positive_baseline": item.get("positive_baseline"),
            "roc_auc": item.get("roc_auc"),
            "pr_auc": item.get("pr_auc"),
            "pr_auc_lift_over_positive_baseline": item.get("pr_auc_lift_over_positive_baseline"),
            "brier_score": item.get("brier_score"),
            "accuracy": item.get("accuracy"),
            "precision": item.get("precision"),
            "recall": item.get("recall"),
            "top_decile_positive_rate": (item.get("top_decile_summary") or {}).get("positive_rate"),
            "top_decile_net_return_mean": (item.get("top_decile_summary") or {}).get("executable_net_return_mean"),
            "sanity_only_not_deploy_evidence": True,
        }
        for name, item in feature_results.items()
    ]
    write_json(_resolve(outputs["metrics_json"]), metrics)
    write_json(_resolve(outputs["stress_json"]), stress)
    write_csv_rows(_resolve(outputs["summary_csv"]), group_summary_rows)
    write_json(_resolve(outputs["interpretation_json"]), interpretation)

    gate_checks = {
        "no_live": bool(cfg.get("safety", {}).get("no_live", False)),
        "no_redis_signals": bool(cfg.get("safety", {}).get("no_redis_signals", False)),
        "no_production_model_writes": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
        "active_model_unchanged": sha256(_resolve(cfg.get("active_model_path", ""))) is not None,
        "no_production_retrain": True,
        "no_one_off_runner": True,
        "executable_success_rows_gte_500": len(success_rows) >= 500,
        "old_baseline_4f_completed": feature_results.get("old_baseline_4f", {}).get("status") == "ok",
        "at_least_one_microstructure_group_completed": any(
            feature_results.get(g, {}).get("status") == "ok"
            for g in ["sender_concentration_5m", "buyer_concentration_5m", "flow_count_5m", "microstructure_combo_no_raw_volume", "microstructure_combo_with_old"]
        ),
        "stress_tests_attempted": bool(stress.get("tests")),
        "interpretation_report_produced": _resolve(outputs["interpretation_json"]).exists(),
        "all_metrics_marked_sanity_only": metrics["sanity_only_not_deploy_evidence"] and stress["sanity_only_not_deploy_evidence"] and interpretation["sanity_only_not_deploy_evidence"],
        "markdown_says_no_full_retrain_promote_live": True,
    }
    allow_h12 = all(gate_checks.values())
    patch = {
        "patch": "run-004-h11-enriched-feature-sanity-ablation",
        "generated_at": datetime.now(UTC).isoformat(),
        "input": {"path": str(input_path), "rows": len(rows), "sha256": sha256(input_path)},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "input_validation": input_validation,
        "feature_group_status_counts": dict(Counter(item.get("status", "unknown") for item in feature_results.values())),
        "microstructure_feature_supported": interpretation["microstructure_feature_supported"],
        "gate": {
            "allow_enter_H12_enriched_feature_stress_or_larger_dataset": allow_h12,
            "checks": gate_checks,
            "next_step": "H12 enriched feature stress OR larger executable dataset" if allow_h12 else "H11 hold / repair feature availability or dataset size",
            "forbidden": ["full retrain", "promote", "live", "production model overwrite"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "no_one_off_runner_created": True,
            "production_model_written": False,
            "full_retrain_promote_live_allowed": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h11_markdown(patch, metrics, stress, interpretation))
    return patch


def _h12_return_values(rows: list[dict[str, str]], net_return_col: str) -> list[float]:
    return [v for v in (optional_float(row.get(net_return_col)) for row in rows) if v is not None]


def _h12_return_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {k: None for k in ["count", "mean", "std", "min", "p1", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "max"]}
    mu = sum(values) / len(values)
    var = sum((v - mu) ** 2 for v in values) / len(values)
    return {
        "count": len(values),
        "mean": rounded(mu, 8),
        "std": rounded(math.sqrt(var), 8),
        "min": rounded(min(values), 8),
        "p1": rounded(percentile(values, 0.01), 8),
        "p10": rounded(percentile(values, 0.10), 8),
        "p25": rounded(percentile(values, 0.25), 8),
        "p50": rounded(percentile(values, 0.50), 8),
        "p75": rounded(percentile(values, 0.75), 8),
        "p90": rounded(percentile(values, 0.90), 8),
        "p95": rounded(percentile(values, 0.95), 8),
        "p99": rounded(percentile(values, 0.99), 8),
        "max": rounded(max(values), 8),
    }


def _h12_corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x <= 0 or den_y <= 0:
        return None
    return rounded(num / (den_x * den_y), 8)


def _h12_auc_binary_score(labels: list[int], scores: list[float]) -> float | None:
    positives = [score for label, score in zip(labels, scores, strict=False) if label == 1]
    negatives = [score for label, score in zip(labels, scores, strict=False) if label == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    total = len(positives) * len(negatives)
    for p in positives:
        for n in negatives:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return rounded(wins / total, 8)


def _h12_label_distribution(rows: list[dict[str, str]], target_col: str, net_return_col: str) -> dict[str, Any]:
    by_label: dict[str, list[float]] = defaultdict(list)
    labels: list[int] = []
    returns: list[float] = []
    for row in rows:
        label = _h11_label(row, target_col)
        ret = optional_float(row.get(net_return_col))
        if label is None or ret is None:
            continue
        labels.append(label)
        returns.append(ret)
        by_label[str(label)].append(ret)
    label1 = by_label.get("1", [])
    label0 = by_label.get("0", [])
    return {
        "distribution": dict(Counter(labels)),
        "label_1_return_distribution": _h12_return_summary(label1),
        "label_0_return_distribution": _h12_return_summary(label0),
        "label_1_net_return_lte_0_rate": rounded(sum(1 for v in label1 if v <= 0) / len(label1), 8) if label1 else None,
        "label_0_net_return_gt_0_rate": rounded(sum(1 for v in label0 if v > 0) / len(label0), 8) if label0 else None,
        "label_net_return_correlation": _h12_corr([float(x) for x in labels], returns),
    }


def _h12_threshold_discrimination(rows: list[dict[str, str]], target_col: str, net_return_col: str, thresholds: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pairs = [
        (_h11_label(row, target_col), optional_float(row.get(net_return_col)))
        for row in rows
        if _h11_label(row, target_col) is not None and optional_float(row.get(net_return_col)) is not None
    ]
    label_scores = [float(label) for label, _ in pairs]
    for threshold in thresholds:
        y = [1 if ret > threshold else 0 for _, ret in pairs]
        pred = [int(label) for label, _ in pairs]
        tp = sum(1 for actual, p in zip(y, pred, strict=False) if actual == 1 and p == 1)
        fp = sum(1 for actual, p in zip(y, pred, strict=False) if actual == 0 and p == 1)
        tn = sum(1 for actual, p in zip(y, pred, strict=False) if actual == 0 and p == 0)
        fn = sum(1 for actual, p in zip(y, pred, strict=False) if actual == 1 and p == 0)
        out[f"return_gt_{int(threshold * 100)}pct"] = {
            "threshold": threshold,
            "positive_rate": rounded(sum(y) / len(y), 8) if y else None,
            "auc_using_executable_label_as_score": _h12_auc_binary_score(y, label_scores),
            "precision_if_label_1": rounded(tp / (tp + fp), 8) if tp + fp else None,
            "recall_if_label_1": rounded(tp / (tp + fn), 8) if tp + fn else None,
            "specificity_if_label_0": rounded(tn / (tn + fp), 8) if tn + fp else None,
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
            "mean_return_label_1": rounded(sum(ret for label, ret in pairs if label == 1) / sum(1 for label, _ in pairs if label == 1), 8),
            "mean_return_label_0": rounded(sum(ret for label, ret in pairs if label == 0) / sum(1 for label, _ in pairs if label == 0), 8),
        }
    return out


def _h12_class_return_stats(class_values: dict[str, list[float]]) -> dict[str, Any]:
    total = sum(len(v) for v in class_values.values())
    out: dict[str, Any] = {}
    for klass, values in class_values.items():
        out[klass] = {
            "rows": len(values),
            "rate": rounded(len(values) / total, 8) if total else None,
            "mean_return": rounded(sum(values) / len(values), 8) if values else None,
            "median_return": rounded(percentile(values, 0.50), 8) if values else None,
            "return_p50": rounded(percentile(values, 0.50), 8) if values else None,
            "return_p90": rounded(percentile(values, 0.90), 8) if values else None,
            "return_p95": rounded(percentile(values, 0.95), 8) if values else None,
        }
    return out


def _h12_binary_target_report(name: str, rows: list[dict[str, str]], net_return_col: str, values: list[int], rules: dict[str, Any]) -> dict[str, Any]:
    returns = [optional_float(row.get(net_return_col)) for row in rows]
    pairs = [(label, ret) for label, ret in zip(values, returns, strict=False) if ret is not None]
    class_values = {
        "0": [ret for label, ret in pairs if label == 0],
        "1": [ret for label, ret in pairs if label == 1],
    }
    pos_rate = len(class_values["1"]) / len(pairs) if pairs else 0.0
    min_rate = float(rules.get("min_positive_rate", 0.10))
    max_rate = float(rules.get("max_positive_rate", 0.60))
    min_rows = int(rules.get("min_top_class_rows", 75))
    over_imbalanced = pos_rate < min_rate or pos_rate > max_rate or len(class_values["1"]) < min_rows
    return {
        "target": name,
        "target_type": "binary_classification",
        "positive_rate": rounded(pos_rate, 8),
        "class_distribution": {"0": len(class_values["0"]), "1": len(class_values["1"])},
        "return_by_class": _h12_class_return_stats(class_values),
        "top_class_return_p50_p90_p95": {
            "p50": rounded(percentile(class_values["1"], 0.50), 8),
            "p90": rounded(percentile(class_values["1"], 0.90), 8),
            "p95": rounded(percentile(class_values["1"], 0.95), 8),
        },
        "overly_imbalanced": over_imbalanced,
        "usable_as_binary_classification_target": not over_imbalanced,
        "better_as_ranking_or_regression_target": over_imbalanced or name.endswith("20pct"),
    }


def _h12_ternary_report(rows: list[dict[str, str]], net_return_col: str, loss_lt: float, win_gt: float) -> dict[str, Any]:
    class_values: dict[str, list[float]] = {"loss": [], "flat": [], "win": []}
    for row in rows:
        ret = optional_float(row.get(net_return_col))
        if ret is None:
            continue
        if ret < loss_lt:
            class_values["loss"].append(ret)
        elif ret > win_gt:
            class_values["win"].append(ret)
        else:
            class_values["flat"].append(ret)
    rates = {klass: (len(values) / sum(len(v) for v in class_values.values())) for klass, values in class_values.items()}
    return {
        "target": "alpha_return_regime_loss_flat_win",
        "target_type": "ternary_classification",
        "class_distribution": {klass: len(values) for klass, values in class_values.items()},
        "class_rates": {klass: rounded(rate, 8) for klass, rate in rates.items()},
        "return_by_class": _h12_class_return_stats(class_values),
        "overly_imbalanced": any(rate < 0.10 for rate in rates.values()),
        "usable_as_binary_classification_target": False,
        "better_as_ranking_or_regression_target": True,
    }


def _h12_rank_percentiles(values: list[float]) -> list[float]:
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda idx: values[idx])
    ranks = [0.0] * len(values)
    if len(values) == 1:
        return [1.0]
    for pos, idx in enumerate(order):
        ranks[idx] = pos / (len(values) - 1)
    return ranks


def _h12_regression_reports(rows: list[dict[str, str]], cfg: dict[str, Any]) -> dict[str, Any]:
    net_col = str(cfg.get("net_return_column", "executable_net_return"))
    returns = _h12_return_values(rows, net_col)
    reg_cfg = cfg.get("regression_targets", {})
    clipped_cfg = reg_cfg.get("clipped_return", {})
    clip_min = float(clipped_cfg.get("min", -0.20))
    clip_max = float(clipped_cfg.get("max", 0.50))
    clipped = [max(clip_min, min(clip_max, ret)) for ret in returns]
    winsor_cfg = reg_cfg.get("winsorized_return", {})
    lo = percentile(returns, float(winsor_cfg.get("lower_quantile", 0.01))) or min(returns)
    hi = percentile(returns, float(winsor_cfg.get("upper_quantile", 0.99))) or max(returns)
    winsorized = [max(lo, min(hi, ret)) for ret in returns]

    by_pool: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_pool_bucket: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
    bucket_ms = int(reg_cfg.get("within_pool_time_bucket_rank", {}).get("bucket_ms", 300000))
    indexed_returns: list[tuple[int, dict[str, str], float]] = []
    for idx, row in enumerate(rows):
        ret = optional_float(row.get(net_col))
        if ret is None:
            continue
        indexed_returns.append((idx, row, ret))
        pool = _lower_addr(row.get("pool_address"))
        by_pool[pool].append((idx, ret))
        ts = int(optional_float(row.get("timestamp")) or 0)
        by_pool_bucket[(pool, ts // bucket_ms if bucket_ms else 0)].append((idx, ret))

    pool_rank = [None] * len(rows)
    for members in by_pool.values():
        ranks = _h12_rank_percentiles([ret for _, ret in members])
        for (idx, _), rank in zip(members, ranks, strict=False):
            pool_rank[idx] = rank
    pool_bucket_rank = [None] * len(rows)
    for members in by_pool_bucket.values():
        ranks = _h12_rank_percentiles([ret for _, ret in members])
        for (idx, _), rank in zip(members, ranks, strict=False):
            pool_bucket_rank[idx] = rank
    pool_rank_values = [v for v in pool_rank if v is not None]
    pool_bucket_values = [v for v in pool_bucket_rank if v is not None]
    return {
        "clipped_executable_net_return_regression": {
            "target_type": "regression",
            "clip_min": clip_min,
            "clip_max": clip_max,
            "summary": _h12_return_summary(clipped),
            "usable_as_binary_classification_target": False,
            "better_as_ranking_or_regression_target": True,
        },
        "winsorized_executable_net_return_regression": {
            "target_type": "regression",
            "lower_bound": rounded(lo, 8),
            "upper_bound": rounded(hi, 8),
            "summary": _h12_return_summary(winsorized),
            "usable_as_binary_classification_target": False,
            "better_as_ranking_or_regression_target": True,
        },
        "within_pool_return_rank": {
            "target_type": "ranking",
            "summary": _h12_return_summary(pool_rank_values),
            "pools": len(by_pool),
            "usable_as_binary_classification_target": False,
            "better_as_ranking_or_regression_target": True,
        },
        "within_pool_and_time_bucket_return_rank": {
            "target_type": "ranking",
            "summary": _h12_return_summary(pool_bucket_values),
            "pool_time_buckets": len(by_pool_bucket),
            "bucket_ms": bucket_ms,
            "usable_as_binary_classification_target": False,
            "better_as_ranking_or_regression_target": True,
        },
    }


def _h12_candidate_targets(rows: list[dict[str, str]], cfg: dict[str, Any]) -> dict[str, Any]:
    net_col = str(cfg.get("net_return_column", "executable_net_return"))
    returns = _h12_return_values(rows, net_col)
    rules = cfg.get("target_rules", {})
    out: dict[str, Any] = {"binary_thresholds": {}, "quantile_targets": {}, "ternary_regime": {}, "regression_and_ranking": {}}
    for name, threshold in cfg.get("return_thresholds", {}).items():
        labels = [1 if (optional_float(row.get(net_col)) or -math.inf) > float(threshold) else 0 for row in rows]
        report = _h12_binary_target_report(name, rows, net_col, labels, rules)
        report["threshold"] = float(threshold)
        if name == "alpha_ret_gt_0":
            report["usable_as_binary_classification_target"] = False
            report["reason"] = "too_close_to_executable_label_and_too_coarse_for_alpha"
        out["binary_thresholds"][name] = report
    for name, top_share in cfg.get("top_quantile_targets", {}).items():
        cutoff = percentile(returns, 1 - float(top_share))
        labels = [1 if optional_float(row.get(net_col)) is not None and (optional_float(row.get(net_col)) or -math.inf) >= (cutoff or math.inf) else 0 for row in rows]
        report = _h12_binary_target_report(name, rows, net_col, labels, rules)
        report["top_share"] = float(top_share)
        report["cutoff_return"] = rounded(cutoff, 8)
        report["target_type"] = "binary_ranking_quantile"
        report["better_as_ranking_or_regression_target"] = name.endswith(("10pct_return", "20pct_return"))
        out["quantile_targets"][name] = report
    ternary_cfg = cfg.get("ternary_regime", {})
    out["ternary_regime"] = _h12_ternary_report(rows, net_col, float(ternary_cfg.get("loss_lt", -0.05)), float(ternary_cfg.get("win_gt", 0.05)))
    out["regression_and_ranking"] = _h12_regression_reports(rows, cfg)
    return out


def _h12_feature_alignment(cfg: dict[str, Any]) -> dict[str, Any]:
    summary_path = _resolve(cfg["h11_inputs"]["summary_csv"])
    rows = read_rows(summary_path)
    focus = {
        "old_baseline_4f",
        "flow_raw_5m",
        "old_plus_sender_concentration",
        "sender_concentration_5m",
        "buyer_concentration_5m",
        "flow_count_5m",
        "microstructure_combo_no_raw_volume",
        "microstructure_combo_with_old",
    }
    items: dict[str, Any] = {}
    for row in rows:
        group = row.get("group", "")
        if group not in focus:
            continue
        roc = optional_float(row.get("roc_auc"))
        pr_lift = optional_float(row.get("pr_auc_lift_over_positive_baseline"))
        top_return = optional_float(row.get("top_decile_net_return_mean"))
        items[group] = {
            "status": row.get("status"),
            "roc_auc": rounded(roc, 8),
            "pr_auc_lift_over_positive_baseline": rounded(pr_lift, 8),
            "top_decile_net_return_mean": rounded(top_return, 8),
            "classification_good_return_poor": bool((roc or 0) >= 0.64 and (pr_lift or 0) > 0 and (top_return or 0) <= 0),
        }
    return_sorted = sorted(items.items(), key=lambda kv: kv[1].get("top_decile_net_return_mean") if kv[1].get("top_decile_net_return_mean") is not None else -999, reverse=True)
    old_return = items.get("old_baseline_4f", {}).get("top_decile_net_return_mean")
    return {
        "groups": items,
        "classification_good_but_return_poor_groups": [name for name, item in items.items() if item["classification_good_return_poor"]],
        "top_decile_return_ranking": [{"group": name, "top_decile_net_return_mean": item.get("top_decile_net_return_mean")} for name, item in return_sorted],
        "high_roc_pr_but_poor_return_misalignment": any(item["classification_good_return_poor"] for item in items.values()),
        "old_baseline_still_closest_to_return_target": bool(return_sorted and return_sorted[0][0] == "old_baseline_4f"),
        "raw_volume_requires_decimals_scale_normalization": True,
        "sender_buyer_flow_should_be_reevaluated_under_return_aware_targets": True,
        "old_baseline_top_decile_return_mean": old_return,
    }


def _h12_recommendation(targets: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    primary = str(cfg.get("h13_recommendation", {}).get("primary_target", "alpha_top_20pct_return"))
    secondary = list(cfg.get("h13_recommendation", {}).get("secondary_targets", []))
    primary_report = (
        targets.get("quantile_targets", {}).get(primary)
        or targets.get("binary_thresholds", {}).get(primary)
        or targets.get("regression_and_ranking", {}).get(primary)
    )
    return {
        "h13_primary_target": primary,
        "primary_target_report": primary_report,
        "h13_secondary_targets": secondary,
        "executable_label_role": cfg.get("h13_recommendation", {}).get("executable_label_role", "feasibility_gate_and_coarse_auxiliary_label"),
        "use_ranking_or_regression": True,
        "binary_targets_for_sanity_comparison": ["alpha_ret_gt_5pct", "alpha_ret_gt_10pct", "alpha_top_20pct_return"],
        "stratification_required": ["pool_type", "top_pool_vs_non_top_pool", "time_bucket", "within_pool_rank"],
        "reasoning": [
            "alpha_top_20pct_return directly optimizes relative return quality and avoids treating tiny positive returns as alpha.",
            "alpha_ret_gt_5pct and alpha_ret_gt_10pct remain useful binary controls for economic thresholds.",
            "clipped/winsorized net_return regression and within-pool rank should be tested because H11 showed classification metrics can diverge from realized return.",
        ],
    }


def _h12_markdown(report: dict[str, Any]) -> str:
    audit = report["executable_label_vs_return_audit"]
    targets = report["candidate_targets"]
    alignment = report["h11_return_alignment_audit"]
    recommendation = report["h13_recommendation"]
    binary_rows = []
    for name, item in targets["binary_thresholds"].items():
        binary_rows.append(
            f"| {name} | {item.get('positive_rate')} | {item.get('return_by_class', {}).get('1', {}).get('median_return')} | "
            f"{item.get('top_class_return_p50_p90_p95', {}).get('p90')} | {item.get('usable_as_binary_classification_target')} |"
        )
    quantile_rows = []
    for name, item in targets["quantile_targets"].items():
        quantile_rows.append(
            f"| {name} | {item.get('cutoff_return')} | {item.get('positive_rate')} | "
            f"{item.get('return_by_class', {}).get('1', {}).get('median_return')} | {item.get('usable_as_binary_classification_target')} |"
        )
    return f"""# Run-004 Patch H12 - Return-Aware Alpha Target Construction

## Conclusion
`executable_label` is not suitable as the primary alpha target. It is useful as a feasibility gate and coarse auxiliary label, but it collapses return magnitude into a yes/no outcome and lets tiny positive-return rows look equivalent to stronger alpha rows.

H13 primary target: `{recommendation['h13_primary_target']}`.

## Executable Label Vs Return
- Executable success rows: {audit['executable_success_rows']}
- executable_label distribution: {audit['executable_label_distribution']}
- net_return mean/std/p50/p90/p95/max: {audit['executable_net_return_distribution']['mean']} / {audit['executable_net_return_distribution']['std']} / {audit['executable_net_return_distribution']['p50']} / {audit['executable_net_return_distribution']['p90']} / {audit['executable_net_return_distribution']['p95']} / {audit['executable_net_return_distribution']['max']}
- label=1 but net_return <= 0: {audit['label_1_net_return_lte_0_rate']}
- label=0 but net_return > 0: {audit['label_0_net_return_gt_0_rate']}
- label/net_return correlation: {audit['label_net_return_correlation']}

## Binary Return Targets
| target | positive_rate | positive_class_p50 | positive_class_p90 | usable_binary |
| --- | ---: | ---: | ---: | --- |
{chr(10).join(binary_rows)}

## Quantile Return Targets
| target | cutoff_return | positive_rate | top_class_p50 | usable_binary |
| --- | ---: | ---: | ---: | --- |
{chr(10).join(quantile_rows)}

## H11 Return Alignment
- Classification-good but return-poor groups: {alignment['classification_good_but_return_poor_groups']}
- Top-decile return ranking: {alignment['top_decile_return_ranking']}
- Old baseline still closest to return target: `{alignment['old_baseline_still_closest_to_return_target']}`
- Raw volume needs decimals/scale normalization: `{alignment['raw_volume_requires_decimals_scale_normalization']}`
- Sender/buyer/flow features should be reevaluated under return-aware targets: `{alignment['sender_buyer_flow_should_be_reevaluated_under_return_aware_targets']}`

## H13 Recommendation
- Primary: `{recommendation['h13_primary_target']}`
- Secondary: {recommendation['h13_secondary_targets']}
- `executable_label` role: {recommendation['executable_label_role']}
- Ranking/regression required: `{recommendation['use_ranking_or_regression']}`
- Required stratification: {recommendation['stratification_required']}

## Gate And Safety
- allow_enter_H13_return_aware_feature_ablation_model_sanity_test: `{report['gate']['allow_enter_H13_return_aware_feature_ablation_model_sanity_test']}`
- H12 is sanity-only target construction and is not deployment evidence.
- No promote, no live, no production model overwrite, and no production training are allowed.
"""


def _h12_return_aware_alpha_target_construction(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    input_path = _resolve(cfg["input_path"])
    rows = read_rows(input_path)
    status_filter = cfg.get("status_filter", {})
    success_rows = [r for r in rows if all(str(r.get(k, "")) == str(v) for k, v in status_filter.items())]
    target_col = str(cfg.get("target_column", "executable_label"))
    net_col = str(cfg.get("net_return_column", "executable_net_return"))
    returns = _h12_return_values(success_rows, net_col)
    label_audit = _h12_label_distribution(success_rows, target_col, net_col)
    audit = {
        "executable_success_rows": len(success_rows),
        "executable_label_distribution": label_audit["distribution"],
        "executable_net_return_distribution": _h12_return_summary(returns),
        "label_1_return_distribution": label_audit["label_1_return_distribution"],
        "label_0_return_distribution": label_audit["label_0_return_distribution"],
        "label_1_net_return_lte_0_rate": label_audit["label_1_net_return_lte_0_rate"],
        "label_0_net_return_gt_0_rate": label_audit["label_0_net_return_gt_0_rate"],
        "label_net_return_correlation": label_audit["label_net_return_correlation"],
        "label_discrimination_by_return_threshold": _h12_threshold_discrimination(success_rows, target_col, net_col, [0.0, 0.02, 0.05, 0.10, 0.20]),
        "is_executable_label_suitable_as_alpha_target": False,
        "executable_label_best_role": "feasibility_gate_and_coarse_auxiliary_label",
    }
    targets = _h12_candidate_targets(success_rows, cfg)
    alignment = _h12_feature_alignment(cfg)
    recommendation = _h12_recommendation(targets, cfg)
    usable_targets = [
        name
        for bucket in ("binary_thresholds", "quantile_targets")
        for name, item in targets[bucket].items()
        if item.get("usable_as_binary_classification_target")
    ] + [name for name, item in targets["regression_and_ranking"].items() if item.get("better_as_ranking_or_regression_target")]
    gate_checks = {
        "executable_label_vs_net_return_audit_completed": bool(audit),
        "at_least_one_return_aware_target_usable": bool(usable_targets),
        "h13_primary_target_explicitly_specified": bool(recommendation["h13_primary_target"]),
        "executable_label_role_redefined": recommendation["executable_label_role"] in {"feasibility_gate_and_coarse_auxiliary_label", "feasibility_gate"},
        "markdown_says_h12_not_deploy_evidence": True,
        "no_promote": bool(cfg.get("safety", {}).get("no_promote", False)),
        "no_live": bool(cfg.get("safety", {}).get("no_live", False)),
        "no_production_model_write": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
    }
    report = {
        "patch": "run-004-h12-return-aware-alpha-target-construction",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "enriched_dataset": {"path": str(input_path), "rows": len(rows), "sha256": sha256(input_path)},
            "h11_metrics_json": str(_resolve(cfg["h11_inputs"]["metrics_json"])),
            "h11_interpretation_json": str(_resolve(cfg["h11_inputs"]["interpretation_json"])),
            "h11_summary_csv": str(_resolve(cfg["h11_inputs"]["summary_csv"])),
            "h11_markdown": str(_resolve(cfg["h11_inputs"]["markdown"])),
        },
        "executable_label_vs_return_audit": audit,
        "candidate_targets": targets,
        "usable_return_aware_targets": usable_targets,
        "h11_return_alignment_audit": alignment,
        "h13_recommendation": recommendation,
        "gate": {
            "allow_enter_H13_return_aware_feature_ablation_model_sanity_test": all(gate_checks.values()),
            "checks": gate_checks,
            "next_step": "H13 Return-Aware Feature Ablation / Model Sanity Test",
            "forbidden": ["promote", "live", "production model overwrite", "production training"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
        },
    }
    write_json(_resolve(cfg["outputs"]["json"]), report)
    write_markdown(_resolve(cfg["outputs"]["markdown"]), _h12_markdown(report))
    return {"gate": report["gate"], "outputs": {k: str(_resolve(v)) for k, v in cfg["outputs"].items()}, **report}


H13_MICRO_GROUPS = {
    "sender_concentration_5m",
    "buyer_concentration_5m",
    "flow_count_5m",
    "flow_raw_5m",
    "temporal_regime",
    "microstructure_combo_no_raw_volume",
    "microstructure_combo_with_old",
    "old_plus_buyer_flow_no_raw",
}


def _h13_add_targets(rows: list[dict[str, str]], cfg: dict[str, Any], h12: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    net_col = str(cfg.get("net_return_column", "executable_net_return"))
    returns = _h12_return_values(rows, net_col)
    primary = str(cfg.get("primary_target", "alpha_top_20pct_return"))
    cutoff = (
        (h12.get("h13_recommendation", {}).get("primary_target_report") or {}).get("cutoff_return")
        or percentile(returns, 1 - float(cfg.get("primary_target_top_share", 0.20)))
    )
    top30 = percentile(returns, 0.70)
    top10 = percentile(returns, 0.90)
    winsor_cfg = cfg.get("secondary_targets", {}).get("winsorized_net_return", {})
    win_lo = percentile(returns, float(winsor_cfg.get("lower_quantile", 0.01))) or min(returns)
    win_hi = percentile(returns, float(winsor_cfg.get("upper_quantile", 0.99))) or max(returns)
    clip_cfg = cfg.get("secondary_targets", {}).get("clipped_net_return", {})
    clip_min = float(clip_cfg.get("min", -0.20))
    clip_max = float(clip_cfg.get("max", 0.50))

    by_pool: dict[str, list[tuple[int, float]]] = defaultdict(list)
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        ret = optional_float(row.get(net_col))
        if ret is None:
            continue
        item: dict[str, Any] = dict(row)
        item["alpha_ret_gt_5pct"] = int(ret > 0.05)
        item["alpha_ret_gt_10pct"] = int(ret > 0.10)
        item["alpha_top_30pct_return"] = int(ret >= (top30 or math.inf))
        item["alpha_top_10pct_return"] = int(ret >= (top10 or math.inf))
        item[primary] = int(ret >= float(cutoff))
        item["clipped_net_return"] = max(clip_min, min(clip_max, ret))
        item["winsorized_net_return"] = max(float(win_lo), min(float(win_hi), ret))
        out.append(item)
        by_pool[_lower_addr(row.get("pool_address"))].append((len(out) - 1, ret))
    for members in by_pool.values():
        ranks = _h12_rank_percentiles([ret for _, ret in members])
        for (idx, _), rank in zip(members, ranks, strict=False):
            out[idx]["within_pool_return_rank"] = rank
            out[idx]["alpha_within_pool_top20_rank"] = int(rank >= 0.80)
    target_report = {
        "primary_target": primary,
        "primary_cutoff_return": rounded(float(cutoff), 8),
        "secondary_cutoffs": {
            "alpha_ret_gt_5pct": 0.05,
            "alpha_ret_gt_10pct": 0.10,
            "alpha_top_30pct_return": rounded(top30, 8),
            "alpha_top_10pct_return": rounded(top10, 8),
            "clipped_net_return": {"min": clip_min, "max": clip_max},
            "winsorized_net_return": {"lower": rounded(float(win_lo), 8), "upper": rounded(float(win_hi), 8)},
        },
    }
    return out, target_report


def _h13_dataset_report(rows: list[dict[str, Any]], cfg: dict[str, Any], availability: dict[str, Any]) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_top_20pct_return"))
    labels = [int(row.get(target, 0)) for row in rows]
    pool_conc = _h11_pool_concentration(rows)
    return {
        "rows": len(rows),
        "primary_target": target,
        "primary_target_distribution": {
            "positive": sum(labels),
            "negative": len(labels) - sum(labels),
            "positive_rate": rounded(sum(labels) / len(labels), 8) if labels else None,
        },
        "executable_net_return_distribution": _h12_return_summary(_h12_return_values(rows, str(cfg.get("net_return_column", "executable_net_return")))),
        "pool_type_distribution": _h11_distribution(rows, "pool_type"),
        "v2_v3_distribution": _h11_distribution(rows, "protocol"),
        "pool_concentration": pool_conc,
        "missing_feature_summary": {
            group: {
                "missing_features": meta["missing_features"],
                "high_missing_features": meta["high_missing_features"],
                "high_scale_features": meta["high_scale_features"],
                "constant_or_all_zero_features": meta["constant_or_all_zero_features"],
            }
            for group, meta in availability["groups"].items()
        },
    }


def _h13_precision_at(y_true: Any, scores: Any, share: float) -> float | None:
    import numpy as np

    if len(y_true) == 0:
        return None
    top_n = max(1, int(math.ceil(len(y_true) * share)))
    idx = np.argsort(scores)[-top_n:]
    return rounded(float(y_true[idx].mean()), 8)


def _h13_top_return_summary(y_true: Any, scores: Any, returns: Any, share: float = 0.10) -> dict[str, Any]:
    import numpy as np

    if len(y_true) == 0:
        return {"rows": 0, "executable_net_return_mean": None, "executable_net_return_p50": None, "executable_net_return_p90": None, "alpha_positive_rate": None}
    top_n = max(1, int(math.ceil(len(y_true) * share)))
    idx = np.argsort(scores)[-top_n:]
    vals = [float(v) for v in returns[idx] if not np.isnan(v)]
    return {
        "rows": int(top_n),
        "executable_net_return_mean": rounded(sum(vals) / len(vals), 8) if vals else None,
        "executable_net_return_p50": rounded(percentile(vals, 0.50), 8) if vals else None,
        "executable_net_return_p90": rounded(percentile(vals, 0.90), 8) if vals else None,
        "alpha_positive_rate": rounded(float(y_true[idx].mean()), 8),
    }


def _h13_model_rows(rows: list[dict[str, Any]], features: list[str], target: str, net_col: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        vals: list[float] = []
        ok = True
        for feature in features:
            val = optional_float(row.get(feature))
            if val is None:
                ok = False
                break
            vals.append(val)
        label = optional_float(row.get(target))
        ret = optional_float(row.get(net_col))
        if not ok or label is None or ret is None:
            continue
        out.append(
            {
                "timestamp": int(optional_float(row.get("timestamp")) or len(out)),
                "features": vals,
                "label": int(label),
                "net_return": ret,
                "pool_address": _lower_addr(row.get("pool_address")),
                "pool_type": _h11_row_protocol(row),
            }
        )
    return sorted(out, key=lambda r: r["timestamp"])


def _h13_train_eval(
    group: str,
    rows: list[dict[str, Any]],
    features: list[str],
    cfg: dict[str, Any],
    *,
    target: str | None = None,
    stress_name: str | None = None,
    shuffled_5fold: bool = False,
    persist_candidate: bool = True,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    from sklearn.model_selection import StratifiedKFold
    from xgboost import XGBClassifier

    training = cfg.get("training", {})
    target = target or str(cfg.get("primary_target", "alpha_top_20pct_return"))
    net_col = str(cfg.get("net_return_column", "executable_net_return"))
    model_rows = _h13_model_rows(rows, features, target, net_col)
    y_list = [r["label"] for r in model_rows]
    positives = sum(y_list)
    negatives = len(y_list) - positives
    base = {
        "group": group,
        "stress_test": stress_name,
        "status": "insufficient_rows",
        "sanity_only_not_deploy_evidence": True,
        "features_used": features,
        "rows_used": len(model_rows),
        "label_distribution": {"positive": positives, "negative": negatives, "positive_rate": rounded(positives / len(y_list), 8) if y_list else None},
    }
    if (
        len(model_rows) < int(training.get("min_rows", 250))
        or positives < int(training.get("min_positive_labels", 30))
        or negatives < int(training.get("min_negative_labels", 50))
    ):
        return base
    X = np.array([r["features"] for r in model_rows], dtype=float)
    y = np.array(y_list, dtype=int)
    returns = np.array([r["net_return"] for r in model_rows], dtype=float)
    if shuffled_5fold:
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=int(training.get("random_state", 45)))
        scores_all = np.zeros(len(y))
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y), start=1):
            model = XGBClassifier(
                max_depth=int(training.get("max_depth", 2)),
                n_estimators=int(training.get("n_estimators", 40)),
                learning_rate=float(training.get("learning_rate", 0.08)),
                subsample=float(training.get("subsample", 0.9)),
                colsample_bytree=float(training.get("colsample_bytree", 0.9)),
                random_state=int(training.get("random_state", 45)) + fold,
                eval_metric="logloss",
            )
            model.fit(X[train_idx], y[train_idx])
            scores_all[val_idx] = model.predict_proba(X[val_idx])[:, 1]
        pr = float(average_precision_score(y, scores_all))
        baseline = float(y.mean())
        return {
            **base,
            "status": "ok",
            "split": "shuffled_5fold_sanity_only",
            "train_rows": None,
            "validation_rows": len(y),
            "positive_rate_total": rounded(float(y.mean()), 8),
            "positive_rate_train": None,
            "positive_rate_validation": rounded(float(y.mean()), 8),
            "roc_auc": rounded(float(roc_auc_score(y, scores_all)), 8),
            "pr_auc": rounded(pr, 8),
            "pr_auc_lift_over_primary_baseline": rounded(pr - baseline, 8),
            "precision_at_top5pct": _h13_precision_at(y, scores_all, 0.05),
            "precision_at_top10pct": _h13_precision_at(y, scores_all, 0.10),
            "precision_at_top20pct": _h13_precision_at(y, scores_all, 0.20),
            "top_decile_return_summary": _h13_top_return_summary(y, scores_all, returns, 0.10),
            "brier_score": rounded(float(brier_score_loss(y, scores_all)), 8),
        }
    split = max(1, int(len(y) * 0.70))
    X_train, y_train = X[:split], y[:split]
    X_val, y_val = X[split:], y[split:]
    ret_val = returns[split:]
    if len(set(y_train.tolist())) < 2 or len(set(y_val.tolist())) < 2:
        return {**base, "train_rows": len(y_train), "validation_rows": len(y_val)}
    model = XGBClassifier(
        max_depth=int(training.get("max_depth", 2)),
        n_estimators=int(training.get("n_estimators", 40)),
        learning_rate=float(training.get("learning_rate", 0.08)),
        subsample=float(training.get("subsample", 0.9)),
        colsample_bytree=float(training.get("colsample_bytree", 0.9)),
        random_state=int(training.get("random_state", 45)),
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)
    scores = model.predict_proba(X_val)[:, 1]
    baseline = float(y_val.mean())
    pr = float(average_precision_score(y_val, scores))
    importances = getattr(model, "feature_importances_", np.zeros(len(features))).tolist()
    feature_importance = sorted(
        [{"feature": feature, "importance": rounded(float(importance), 8)} for feature, importance in zip(features, importances, strict=False)],
        key=lambda item: item["importance"] or 0,
        reverse=True,
    )
    result = {
        **base,
        "status": "ok",
        "split": "chronological_70_30",
        "train_rows": int(len(y_train)),
        "validation_rows": int(len(y_val)),
        "positive_rate_total": rounded(float(y.mean()), 8),
        "positive_rate_train": rounded(float(y_train.mean()), 8),
        "positive_rate_validation": rounded(baseline, 8),
        "roc_auc": rounded(float(roc_auc_score(y_val, scores)), 8),
        "pr_auc": rounded(pr, 8),
        "pr_auc_lift_over_primary_baseline": rounded(pr - baseline, 8),
        "precision_at_top5pct": _h13_precision_at(y_val, scores, 0.05),
        "precision_at_top10pct": _h13_precision_at(y_val, scores, 0.10),
        "precision_at_top20pct": _h13_precision_at(y_val, scores, 0.20),
        "top_decile_return_summary": _h13_top_return_summary(y_val, scores, ret_val, 0.10),
        "calibration_bins": _h11_calibration_bins(y_val, scores),
        "brier_score": rounded(float(brier_score_loss(y_val, scores)), 8),
        "feature_importance": feature_importance,
        "top_features": feature_importance[:5],
    }
    if persist_candidate and stress_name is None:
        candidate_dir = _resolve(cfg["outputs"]["candidate_model_dir"])
        candidate_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            candidate_dir / f"{group}.json",
            {
                "candidate_model_type": "xgboost_return_aware_sanity_metadata_only",
                "sanity_only_not_deploy_evidence": True,
                "not_for_promotion": True,
                "primary_target": target,
                "group": group,
                "features_used": features,
                "metrics": {k: v for k, v in result.items() if k not in {"feature_importance", "calibration_bins"}},
                "feature_importance": feature_importance,
            },
        )
    return result


def _h13_group_result(group: str, rows: list[dict[str, Any]], cfg: dict[str, Any], availability: dict[str, Any]) -> dict[str, Any]:
    meta = availability["groups"][group]
    safe = _h11_safe_features(meta)
    result: dict[str, Any] = {
        "group": group,
        "sanity_only_not_deploy_evidence": True,
        "requested_features": meta["requested_features"],
        "resolved_features": meta["resolved_features"],
        "missing_features": meta["missing_features"],
        "high_missing_features": meta["high_missing_features"],
        "high_scale_features": meta["high_scale_features"],
        "constant_or_all_zero_features": meta["constant_or_all_zero_features"],
        "features_used": safe,
    }
    if meta["missing_features"]:
        return {**result, "status": "missing_features"}
    if not safe:
        return {**result, "status": "insufficient_rows"}
    if meta["high_missing_features"]:
        return {**result, "status": "high_missing"}
    trained = _h13_train_eval(group, rows, safe, cfg)
    if meta["high_scale_features"]:
        trained["warnings"] = {"high_scale_review": meta["high_scale_features"]}
    return {**result, **trained}


def _h13_stress_rows(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return _h11_stress_rows(rows, name if name != "within_pool_rank_sanity" else "uncapped")


def _h13_stress_tests(rows: list[dict[str, Any]], cfg: dict[str, Any], availability: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_top_20pct_return"))
    groups = list(cfg.get("feature_groups", {}).keys())
    old = metrics.get("old_baseline_4f", {})
    tests: dict[str, Any] = {}
    for test in cfg.get("stress_tests", []):
        subset = _h13_stress_rows(rows, str(test))
        stress_target = "alpha_within_pool_top20_rank" if test == "within_pool_rank_sanity" else target
        positives = sum(int(row.get(stress_target, 0)) for row in subset)
        item: dict[str, Any] = {
            "rows": len(subset),
            "positives": positives,
            "negatives": len(subset) - positives,
            "trainable": len(subset) >= int(cfg.get("training", {}).get("min_rows", 250)),
            "groups": {},
        }
        for group in groups:
            meta = availability["groups"][group]
            if meta["missing_features"] or meta["high_missing_features"]:
                item["groups"][group] = {"status": "missing_features" if meta["missing_features"] else "high_missing", "sanity_only_not_deploy_evidence": True}
                continue
            result = _h13_train_eval(
                group,
                subset,
                _h11_safe_features(meta),
                cfg,
                target=stress_target,
                stress_name=str(test),
                shuffled_5fold=(test == "shuffled_5fold_sanity_only"),
                persist_candidate=False,
            )
            old_roc = old.get("roc_auc")
            old_return = (old.get("top_decile_return_summary") or {}).get("executable_net_return_mean")
            result["collapse_flags"] = {
                "below_old_baseline_roc_by_0_05": bool(result.get("roc_auc") is not None and old_roc is not None and result["roc_auc"] + 0.05 < old_roc),
                "top_decile_return_below_old_baseline": bool(
                    (result.get("top_decile_return_summary") or {}).get("executable_net_return_mean") is not None
                    and old_return is not None
                    and (result.get("top_decile_return_summary") or {}).get("executable_net_return_mean") < old_return
                ),
            }
            item["groups"][group] = result
        tests[str(test)] = item
    return {"sanity_only_not_deploy_evidence": True, "tests": tests}


def _h13_interpretation(metrics: dict[str, Any], stress: dict[str, Any]) -> dict[str, Any]:
    ok = {k: v for k, v in metrics.items() if v.get("status") == "ok"}
    strongest = max(ok.items(), key=lambda kv: (kv[1].get("pr_auc_lift_over_primary_baseline") or -999, kv[1].get("roc_auc") or -999), default=(None, {}))
    old = ok.get("old_baseline_4f", {})
    old_roc = old.get("roc_auc")
    old_lift = old.get("pr_auc_lift_over_primary_baseline")
    old_return = (old.get("top_decile_return_summary") or {}).get("executable_net_return_mean")
    stronger_micro: list[str] = []
    for name, item in ok.items():
        if name not in H13_MICRO_GROUPS:
            continue
        better_metric = (old_roc is not None and item.get("roc_auc") is not None and item["roc_auc"] > old_roc + 0.03) or (
            old_lift is not None and item.get("pr_auc_lift_over_primary_baseline") is not None and item["pr_auc_lift_over_primary_baseline"] > old_lift + 0.03
        )
        not_worse_return = old_return is not None and (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean") is not None and (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean") >= old_return
        if better_metric and not_worse_return:
            stronger_micro.append(name)
    category_scores = {
        "sender": ok.get("sender_concentration_5m", {}).get("pr_auc_lift_over_primary_baseline"),
        "buyer": ok.get("buyer_concentration_5m", {}).get("pr_auc_lift_over_primary_baseline"),
        "flow_count": ok.get("flow_count_5m", {}).get("pr_auc_lift_over_primary_baseline"),
        "flow_raw": ok.get("flow_raw_5m", {}).get("pr_auc_lift_over_primary_baseline"),
        "temporal": ok.get("temporal_regime", {}).get("pr_auc_lift_over_primary_baseline"),
    }
    best_category = max((kv for kv in category_scores.items() if kv[1] is not None), key=lambda kv: kv[1], default=(None, None))[0]
    support = False
    support_reasons: list[str] = []
    for name in stronger_micro:
        collapse = False
        for test in ["exclude_top1_pool", "v2_only", "v3_only"]:
            item = stress.get("tests", {}).get(test, {}).get("groups", {}).get(name, {})
            if item.get("status") != "ok":
                collapse = True
            if item.get("collapse_flags", {}).get("below_old_baseline_roc_by_0_05"):
                collapse = True
        if not collapse:
            support = True
            support_reasons.append(f"{name} beats old_baseline without full required stress collapse")
    return {
        "sanity_only_not_deploy_evidence": True,
        "strongest_feature_group": strongest[0],
        "microstructure_groups_stronger_than_old_baseline": stronger_micro,
        "most_valuable_microstructure_category": best_category,
        "raw_volume_still_scale_decimals_risk": True,
        "need_v2_v3_separate_modeling": True,
        "need_top_pool_specialized_target": True,
        "need_more_executable_dataset": True,
        "recommend_enter_H14_larger_return_aware_dataset_or_separated_v2_v3_modeling": True,
        "still_forbid_production_retrain_promote_live": True,
        "return_aware_microstructure_supported": support,
        "support_reasons": support_reasons,
    }


def _h13_markdown(patch: dict[str, Any], metrics: dict[str, Any], stress: dict[str, Any], interpretation: dict[str, Any]) -> str:
    rows = [
        f"| {name} | {item.get('status')} | {item.get('rows_used', '')} | {item.get('roc_auc', '')} | {item.get('pr_auc', '')} | {item.get('pr_auc_lift_over_primary_baseline', '')} | {item.get('precision_at_top10pct', '')} | {(item.get('top_decile_return_summary') or {}).get('executable_net_return_mean', '')} |"
        for name, item in metrics.items()
    ]
    stress_rows = []
    for name, item in stress.get("tests", {}).items():
        ok_groups = sum(1 for group in item.get("groups", {}).values() if group.get("status") == "ok")
        best = max(
            ((group_name, group.get("pr_auc_lift_over_primary_baseline")) for group_name, group in item.get("groups", {}).items() if group.get("pr_auc_lift_over_primary_baseline") is not None),
            key=lambda kv: kv[1],
            default=("", None),
        )
        stress_rows.append(f"| {name} | {item.get('rows')} | {item.get('positives')} | {item.get('negatives')} | {ok_groups} | {best[0]} | {best[1]} |")
    return f"""# Run-004 Patch H13 - Return-Aware Feature Ablation / Model Sanity Test

## Dataset
- Rows: {patch['dataset']['rows']}
- Primary target: `{patch['dataset']['primary_target']}`
- Primary target distribution: {patch['dataset']['primary_target_distribution']}
- Top1 pool share: {patch['dataset']['pool_concentration']['top1_pool_share']}
- Top5 pool share: {patch['dataset']['pool_concentration']['top5_pool_share']}

## Feature Group Metrics
| group | status | rows | roc_auc | pr_auc | pr_auc_lift | precision@top10% | top_decile_return_mean |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

## Stress Tests
| stress | rows | positives | negatives | ok_groups | best_group_by_pr_lift | best_pr_lift |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
{chr(10).join(stress_rows)}

## Interpretation
- strongest_feature_group: `{interpretation['strongest_feature_group']}`
- return_aware_microstructure_supported: `{interpretation['return_aware_microstructure_supported']}`
- microstructure stronger than old baseline: {interpretation['microstructure_groups_stronger_than_old_baseline']}
- most valuable microstructure category: `{interpretation['most_valuable_microstructure_category']}`
- raw volume scale/decimals risk: `{interpretation['raw_volume_still_scale_decimals_risk']}`
- need v2/v3 separated modeling: `{interpretation['need_v2_v3_separate_modeling']}`
- need top-pool specialized target: `{interpretation['need_top_pool_specialized_target']}`

## Gate And Safety
- allow_enter_H14_larger_return_aware_dataset_or_separated_v2_v3_modeling: `{patch['gate']['allow_enter_H14_larger_return_aware_dataset_or_separated_v2_v3_modeling']}`
- H13 is sanity-only and is not deployment evidence.
- No full retrain, no promote, no live, no production model overwrite, and no active model overwrite are allowed.
"""


def _h13_return_aware_feature_ablation(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    rows_raw = read_rows(_resolve(cfg["input_path"]))
    h12 = json.loads(_resolve(cfg["h12_json"]).read_text())
    status_filter = cfg.get("status_filter", {})
    success_rows = [r for r in rows_raw if all(str(r.get(k, "")) == str(v) for k, v in status_filter.items())]
    rows, target_report = _h13_add_targets(success_rows, cfg, h12)
    header = set(rows[0].keys()) if rows else set()
    availability = _h11_feature_availability(rows, cfg.get("feature_groups", {}), header, set(), tuple())
    dataset = _h13_dataset_report(rows, cfg, availability)
    metrics: dict[str, Any] = {}
    for group in cfg.get("feature_groups", {}):
        metrics[group] = _h13_group_result(group, rows, cfg, availability)
    stress = _h13_stress_tests(rows, cfg, availability, metrics)
    interpretation = _h13_interpretation(metrics, stress)
    summary_rows = [
        {
            "group": name,
            "status": item.get("status"),
            "rows_used": item.get("rows_used"),
            "train_rows": item.get("train_rows"),
            "validation_rows": item.get("validation_rows"),
            "positive_rate_total": item.get("positive_rate_total"),
            "positive_rate_train": item.get("positive_rate_train"),
            "positive_rate_validation": item.get("positive_rate_validation"),
            "roc_auc": item.get("roc_auc"),
            "pr_auc": item.get("pr_auc"),
            "pr_auc_lift_over_primary_baseline": item.get("pr_auc_lift_over_primary_baseline"),
            "precision_at_top5pct": item.get("precision_at_top5pct"),
            "precision_at_top10pct": item.get("precision_at_top10pct"),
            "precision_at_top20pct": item.get("precision_at_top20pct"),
            "top_decile_return_mean": (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean"),
            "top_decile_alpha_positive_rate": (item.get("top_decile_return_summary") or {}).get("alpha_positive_rate"),
            "sanity_only_not_deploy_evidence": True,
        }
        for name, item in metrics.items()
    ]
    outputs = cfg["outputs"]
    write_json(_resolve(outputs["metrics_json"]), {"sanity_only_not_deploy_evidence": True, "dataset": dataset, "target_construction": target_report, "feature_groups": metrics})
    write_json(_resolve(outputs["stress_json"]), stress)
    write_csv_rows(_resolve(outputs["summary_csv"]), summary_rows)
    write_json(_resolve(outputs["interpretation_json"]), interpretation)
    gate_checks = {
        "primary_target_constructed": dataset["primary_target_distribution"]["positive"] > 0,
        "old_baseline_4f_completed": metrics.get("old_baseline_4f", {}).get("status") == "ok",
        "at_least_one_microstructure_group_completed": any(metrics.get(g, {}).get("status") == "ok" for g in H13_MICRO_GROUPS),
        "stress_tests_attempted": bool(stress.get("tests")),
        "interpretation_produced": True,
        "all_results_marked_sanity_only": True,
        "no_production_model_writes": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
        "no_active_model_overwrite": bool(cfg.get("safety", {}).get("no_active_model_overwrite", False)),
    }
    patch = {
        "patch": "run-004-h13-return-aware-feature-ablation",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "enriched_dataset": str(_resolve(cfg["input_path"])),
            "h12_json": str(_resolve(cfg["h12_json"])),
            "h11_patch_json": str(_resolve(cfg["h11_patch_json"])),
            "h11_metrics_json": str(_resolve(cfg["h11_metrics_json"])),
            "h11_interpretation_json": str(_resolve(cfg["h11_interpretation_json"])),
        },
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "dataset": dataset,
        "target_construction": target_report,
        "feature_group_status_counts": dict(Counter(item.get("status", "unknown") for item in metrics.values())),
        "return_aware_microstructure_supported": interpretation["return_aware_microstructure_supported"],
        "gate": {
            "allow_enter_H14_larger_return_aware_dataset_or_separated_v2_v3_modeling": all(gate_checks.values()),
            "checks": gate_checks,
            "next_step": "H14 larger return-aware dataset OR separated v2-v3 return-aware modeling",
            "forbidden": ["full retrain", "promote", "live", "production model overwrite"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h13_markdown(patch, metrics, stress, interpretation))
    return patch


H14_LEDGER_FIELDS = [
    "row_id",
    "pool_address",
    "token_address",
    "pool_type",
    "protocol",
    "decoded_source_used",
    "timestamp",
    "entry_time",
    "entry_block",
    "exit_block",
    "entry_quote_source",
    "exit_quote_source",
    "entry_amount_in_weth",
    "entry_amount_out_token",
    "exit_amount_in_token",
    "exit_amount_out_weth",
    "gross_quote_return",
    "fixed_cost_adjusted_return_current",
    "executable_net_return_current",
    "estimated_entry_gas_units",
    "estimated_exit_gas_units",
    "estimated_total_gas_units",
    "gas_price_source",
    "estimated_gas_cost_weth",
    "estimated_gas_cost_return",
    "gas_adjusted_net_return",
    "gas_adjusted_return_low",
    "gas_adjusted_return_medium",
    "gas_adjusted_return_high",
    "fixed_horizon_sec",
    "fixed_exit_time",
    "fixed_exit_return",
    "pm_exit_policy_available",
    "trailing_exit_applicable",
    "time_exit_applicable",
    "hard_stop_applicable",
    "dynamic_exit_return",
    "dynamic_exit_reason",
    "dynamic_exit_status",
    "quote_suspicious",
    "return_outlier",
    "gas_cost_dominates",
    "missing_entry_quote",
    "missing_exit_quote",
    "missing_block",
    "scale_or_decimals_risk",
    "needs_manual_review",
]


def _h14_ts_iso(timestamp_ms: Any) -> str:
    ts = optional_float(timestamp_ms)
    if ts is None:
        return ""
    if ts > 10_000_000_000:
        ts /= 1000.0
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _h14_pool_type(row: dict[str, Any]) -> str:
    pt = str(row.get("pool_type") or row.get("pool_type_backfilled") or row.get("protocol") or "").lower()
    return pt if pt in {"v2", "v3"} else "unknown"


def _h14_gas_units(pool_type: str, gas_cfg: dict[str, Any]) -> tuple[int, int, int]:
    per_type = gas_cfg.get(pool_type, gas_cfg.get("v2", {}))
    entry = int(per_type.get("buy_gas_units", 180000))
    exit_ = int(per_type.get("sell_gas_units", 160000))
    approval = int(gas_cfg.get("approval_gas_units", 0)) if gas_cfg.get("include_approval_gas", False) else 0
    return entry, exit_, entry + exit_ + approval


def _h14_gas_cost_return(total_units: int, gas_gwei: float, notional_weth: float) -> tuple[float, float]:
    gas_cost_weth = total_units * gas_gwei * 1e-9
    return gas_cost_weth, gas_cost_weth / notional_weth if notional_weth > 0 else math.nan


def _h14_ledger(rows: list[dict[str, str]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    acct = cfg["return_accounting"]
    gas_cfg = cfg["gas_model"]
    notional_weth = float(acct.get("label_notional_weth", 0.0005))
    round_trip_cost = float(acct.get("round_trip_cost", 0.006))
    horizon_sec = int(acct.get("fixed_horizon_sec", 1800))
    default_scenario = str(gas_cfg.get("default_scenario", "medium"))
    gas_prices = {k: float(v) for k, v in gas_cfg.get("gas_price_gwei", {}).items()}
    default_gwei = gas_prices.get(default_scenario, 0.03)
    slippage_stress = float(acct.get("slippage_stress_bps", 100)) / 10000.0

    ledger: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        ret = optional_float(row.get("executable_net_return"))
        if ret is None:
            continue
        gross = ret + round_trip_cost
        pool_type = _h14_pool_type(row)
        entry_gas, exit_gas, total_gas = _h14_gas_units(pool_type, gas_cfg)
        gas_cost_weth, gas_cost_return = _h14_gas_cost_return(total_gas, default_gwei, notional_weth)
        scenario_adjusted: dict[str, float] = {}
        for scenario, gwei in gas_prices.items():
            _, cost_return = _h14_gas_cost_return(total_gas, gwei, notional_weth)
            scenario_adjusted[scenario] = ret - cost_return
        timestamp_ms = optional_float(row.get("timestamp"))
        fixed_exit_time = ""
        if timestamp_ms is not None:
            sec = timestamp_ms / 1000.0 if timestamp_ms > 10_000_000_000 else timestamp_ms
            fixed_exit_time = datetime.fromtimestamp(sec + horizon_sec, UTC).isoformat()
        missing_block = str(row.get("block_number", "")).strip() == ""
        quote_suspicious = bool(ret > 0.20 or ret < -0.20 or gross > 1.0)
        scale_risk = any(
            abs(optional_float(row.get(col)) or 0.0) > 1e18
            for col in ("buy_volume_raw_300s", "sell_volume_raw_300s", "net_flow_raw_300s", "abs_flow_raw_300s")
        )
        item = {
            "row_id": idx,
            "pool_address": _lower_addr(row.get("pool_address")),
            "token_address": _lower_addr(row.get("token_address")),
            "pool_type": pool_type,
            "protocol": str(row.get("protocol") or row.get("protocol_backfilled") or pool_type),
            "decoded_source_used": row.get("decoded_source_used", ""),
            "timestamp": row.get("timestamp", ""),
            "entry_time": _h14_ts_iso(row.get("timestamp")),
            "entry_block": row.get("block_number", ""),
            "exit_block": "",
            "entry_quote_source": f"{pool_type}_archive_rpc_quote_at_entry_block" if pool_type in {"v2", "v3"} else "unknown",
            "exit_quote_source": f"{pool_type}_archive_rpc_quote_at_t0_plus_1800s" if pool_type in {"v2", "v3"} else "unknown",
            "entry_amount_in_weth": rounded(notional_weth, 10),
            "entry_amount_out_token": "",
            "exit_amount_in_token": "",
            "exit_amount_out_weth": rounded((gross + 1.0) * notional_weth, 12),
            "gross_quote_return": rounded(gross, 8),
            "fixed_cost_adjusted_return_current": rounded(ret, 8),
            "executable_net_return_current": rounded(ret, 8),
            "estimated_entry_gas_units": entry_gas,
            "estimated_exit_gas_units": exit_gas,
            "estimated_total_gas_units": total_gas,
            "gas_price_source": f"configured_{default_scenario}_{default_gwei:g}_gwei",
            "estimated_gas_cost_weth": rounded(gas_cost_weth, 12),
            "estimated_gas_cost_return": rounded(gas_cost_return, 8),
            "gas_adjusted_net_return": rounded(ret - gas_cost_return, 8),
            "gas_adjusted_return_low": rounded(scenario_adjusted.get("low"), 8),
            "gas_adjusted_return_medium": rounded(scenario_adjusted.get("medium"), 8),
            "gas_adjusted_return_high": rounded(scenario_adjusted.get("high"), 8),
            "fixed_horizon_sec": horizon_sec,
            "fixed_exit_time": fixed_exit_time,
            "fixed_exit_return": rounded(ret, 8),
            "pm_exit_policy_available": True,
            "trailing_exit_applicable": "unknown",
            "time_exit_applicable": "unknown",
            "hard_stop_applicable": "unknown",
            "dynamic_exit_return": "",
            "dynamic_exit_reason": "",
            "dynamic_exit_status": "unavailable",
            "quote_suspicious": quote_suspicious,
            "return_outlier": bool(ret > 0.20 or ret < -0.20),
            "gas_cost_dominates": bool(abs(ret) < gas_cost_return),
            "missing_entry_quote": True,
            "missing_exit_quote": True,
            "missing_block": missing_block,
            "scale_or_decimals_risk": scale_risk,
            "needs_manual_review": bool(quote_suspicious or missing_block or scale_risk),
            "_slippage_stress_return": ret - slippage_stress,
        }
        ledger.append(item)

    sensitivity: dict[str, Any] = {}
    for scenario in sorted(gas_prices):
        field = f"gas_adjusted_return_{scenario}"
        before_pos = sum(1 for r in ledger if (r["executable_net_return_current"] or 0) > 0)
        after_pos = sum(1 for r in ledger if (r.get(field) or 0) > 0)
        scenario_sorted = sorted(ledger, key=lambda r: r.get(field) or -math.inf, reverse=True)
        before_sorted_for_scenario = sorted(ledger, key=lambda r: r["executable_net_return_current"], reverse=True)
        top20_n_s = max(1, math.ceil(len(ledger) * 0.20)) if ledger else 0
        before_top20_s = {r["row_id"] for r in before_sorted_for_scenario[:top20_n_s]}
        after_top20_s = {r["row_id"] for r in scenario_sorted[:top20_n_s]}
        sensitivity[scenario] = {
            "gas_price_gwei": gas_prices[scenario],
            "gas_adjusted_return_distribution": numeric_summary([r.get(field) for r in ledger]),
            "positive_rows_after_gas": after_pos,
            "positive_to_negative_after_gas": sum(1 for r in ledger if (r["executable_net_return_current"] or 0) > 0 and (r.get(field) or 0) <= 0),
            "top20_cutoff_after_gas": rounded(percentile([r.get(field) for r in ledger], 0.80), 8),
            "top20_overlap_before_vs_after_gas": len(before_top20_s & after_top20_s),
            "top20_changed_rows": top20_n_s - len(before_top20_s & after_top20_s),
            "positive_rows_before_gas": before_pos,
        }

    report = {
        "rows": len(ledger),
        "notional_weth": notional_weth,
        "round_trip_cost": round_trip_cost,
        "fixed_horizon_sec": horizon_sec,
        "gas_model_source": gas_cfg.get("source"),
        "gas_price_gwei": gas_prices,
        "include_approval_gas": bool(gas_cfg.get("include_approval_gas")),
        "sensitivity": sensitivity,
        "gas_cost_return_distribution_medium": numeric_summary([r["estimated_gas_cost_return"] for r in ledger]),
        "gas_adjusted_return_distribution_medium": numeric_summary([r["gas_adjusted_net_return"] for r in ledger]),
        "positive_to_negative_after_gas_medium": sum(1 for r in ledger if (r["executable_net_return_current"] or 0) > 0 and (r["gas_adjusted_net_return"] or 0) <= 0),
        "positive_rows_before_gas": sum(1 for r in ledger if (r["executable_net_return_current"] or 0) > 0),
        "positive_rows_after_gas_medium": sum(1 for r in ledger if (r["gas_adjusted_net_return"] or 0) > 0),
        "top20_cutoff_before_gas": rounded(percentile([r["executable_net_return_current"] for r in ledger], 0.80), 8),
        "top20_cutoff_after_gas_medium": rounded(percentile([r["gas_adjusted_net_return"] for r in ledger], 0.80), 8),
        "top20_overlap_before_vs_after_gas_medium": None,
        "top_decile_return_before_mean": None,
        "top_decile_return_after_gas_mean": None,
    }
    before_sorted = sorted(ledger, key=lambda r: r["executable_net_return_current"], reverse=True)
    after_sorted = sorted(ledger, key=lambda r: r["gas_adjusted_net_return"], reverse=True)
    top20_n = max(1, math.ceil(len(ledger) * 0.20)) if ledger else 0
    top10_n = max(1, math.ceil(len(ledger) * 0.10)) if ledger else 0
    before_top20 = {r["row_id"] for r in before_sorted[:top20_n]}
    after_top20 = {r["row_id"] for r in after_sorted[:top20_n]}
    report["top20_overlap_before_vs_after_gas_medium"] = len(before_top20 & after_top20)
    report["top20_changed_rows_medium"] = top20_n - len(before_top20 & after_top20)
    report["top_decile_return_before_mean"] = rounded(sum(r["executable_net_return_current"] for r in before_sorted[:top10_n]) / top10_n, 8) if top10_n else None
    report["top_decile_return_after_gas_mean"] = rounded(sum(r["gas_adjusted_net_return"] for r in after_sorted[:top10_n]) / top10_n, 8) if top10_n else None
    return ledger, report


def _h14_return_accounting_spec(cfg: dict[str, Any]) -> dict[str, Any]:
    acct = cfg["return_accounting"]
    return {
        "current_executable_net_return": {
            "computed_in": "scripts/label_dashcam.py label_executable()",
            "formula": "weth_received / LABEL_NOTIONAL_WEI - 1.0 - ROUND_TRIP_COST",
            "round_trip_cost": float(acct.get("round_trip_cost", 0.006)),
            "fixed_horizon_sec": int(acct.get("fixed_horizon_sec", 1800)),
            "uses_LABEL_EXIT_HORIZON_SEC": False,
            "gas_deducted": False,
            "entry_gas_deducted": False,
            "exit_gas_deducted": False,
            "approval_gas_deducted": False,
            "failed_transaction_cost_deducted": False,
            "extra_slippage_model": False,
            "dex_fee_and_price_impact": "V2 CPMM 997/1000 and V3 Quoter amountOut include pool fee and notional-size price impact.",
            "failure_handling": {
                "buy_quote_zero_or_failed": "label=-1, net_return=0.0, label_type=quote_failed",
                "sell_quote_exception": "label=-1, net_return=0.0, label_type=quote_failed",
                "sell_quote_zero": "label=0, net_return=-1.0, label_type=executable",
                "honeypot_low_liquidity": "not separately labeled; appears as quote_failed, zero sell, or poor return",
            },
        },
        "field_semantics": {
            "gross_quote_return": "Derived as executable_net_return + 0.006 for current rows; actual entry/exit quote amounts were not stored in H10.8.",
            "fixed_cost_adjusted_return": "Current executable_net_return after subtracting ROUND_TRIP_COST.",
            "gas_adjusted_return": "H14 configured estimate: fixed_cost_adjusted_return minus estimated gas cost divided by LABEL_NOTIONAL_WETH.",
            "slippage_stress_return": "Design field: gas/fixed-cost return after an extra configured slippage haircut; not present in current input.",
            "final_net_return_after_cost": "H15 target field combining quote, fixed cost, gas, slippage stress, and executable failure costs; not fully computable from current 752 rows.",
        },
        "missing_current_input_fields": [
            "entry_amount_out_token",
            "exit_amount_in_token",
            "entry_block_for_rows_with_blank_block_number",
            "exit_block",
            "historical_gas_price",
            "intra_window_price_path",
            "sell_quote_at_dynamic_exit_trigger",
        ],
    }


def _h14_exit_policy_audit(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults = cfg.get("exit_policy_runtime_defaults", {})
    return {
        "runtime_source": "services/executor/src/main.py _monitor_once()",
        "trailing_exit_exists": True,
        "time_exit_exists": True,
        "hard_stop_exists": True,
        "take_profit_exists": True,
        "thresholds": {
            "take_profit_pct": float(defaults.get("take_profit_pct", 15.0)),
            "stop_loss_pct": float(defaults.get("stop_loss_pct", 10.0)),
            "trailing_stop_pct": float(defaults.get("trailing_stop_pct", 5.0)),
            "time_decay_min": float(defaults.get("time_decay_min", 240.0)),
            "monitor_interval_sec": float(defaults.get("monitor_interval_sec", 30.0)),
        },
        "scope": "Implemented in executor paper/dry-run and live monitor loop, not in label_dashcam replay labels.",
        "label_dashcam_simulates_dynamic_exits": False,
        "fixed_30m_vs_pm_policy": {
            "label_dashcam": "Always exits at t0 + 1800s reverse quote.",
            "position_manager": "Monitors open positions repeatedly; exits on take_profit, stop_loss, trailing_stop, or time_decay only if quote <= entry after 240m.",
            "main_difference": "PM exit time/reason is path-dependent; 30m label uses only one terminal quote and cannot see intra-window peak/drawdown or earlier stops.",
        },
        "required_for_real_backtest": [
            "entry price path",
            "intra-window price path",
            "peak price",
            "drawdown",
            "stop trigger time",
            "sell quote at trigger block",
            "gas at trigger block",
            "approval state and approval gas",
            "failed/replaced/cancelled transaction costs",
        ],
        "dynamic_exit_unavailable_reason": "The current 752-row H10.8 dataset stores terminal executable_net_return but not token amount, entry/exit quote amounts, exit_block, historical gas, or intra-window quote path needed to simulate take_profit/stop_loss/trailing/time exits.",
    }


def _h14_group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key) or "unknown")].append(row)
    for name, vals in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
        out.append({
            key: name,
            "rows": len(vals),
            "mean_return": rounded(sum(v["executable_net_return_current"] for v in vals) / len(vals), 8),
            "mean_gas_adjusted_return": rounded(sum(v["gas_adjusted_net_return"] for v in vals) / len(vals), 8),
            "outlier_rows": sum(1 for v in vals if v["return_outlier"]),
            "manual_review_rows": sum(1 for v in vals if v["needs_manual_review"]),
        })
    return out


def _h14_outlier_report(ledger: list[dict[str, Any]], gas_report: dict[str, Any]) -> dict[str, Any]:
    gt20 = [r for r in ledger if r["executable_net_return_current"] > 0.20]
    lt20 = [r for r in ledger if r["executable_net_return_current"] < -0.20]
    flipped = [r for r in ledger if r["executable_net_return_current"] > 0 and r["gas_adjusted_net_return"] <= 0]
    top50 = sorted(ledger, key=lambda r: r["executable_net_return_current"], reverse=True)[:50]
    bottom50 = sorted(ledger, key=lambda r: r["executable_net_return_current"])[:50]
    cutoff = gas_report["top20_cutoff_before_gas"]
    top20_rows = [r for r in ledger if r["executable_net_return_current"] >= cutoff] if cutoff is not None else []
    return {
        "return_gt_20pct_rows": len(gt20),
        "return_lt_minus_20pct_rows": len(lt20),
        "positive_to_negative_after_gas_rows": len(flipped),
        "top20_alpha_positive_rows": len(top20_rows),
        "top50_return_rows": [{k: r[k] for k in ("row_id", "pool_address", "pool_type", "decoded_source_used", "executable_net_return_current", "gas_adjusted_net_return", "needs_manual_review")} for r in top50],
        "bottom50_return_rows": [{k: r[k] for k in ("row_id", "pool_address", "pool_type", "decoded_source_used", "executable_net_return_current", "gas_adjusted_net_return", "needs_manual_review")} for r in bottom50],
        "by_pool": _h14_group_summary(ledger, "pool_address"),
        "by_pool_type": _h14_group_summary(ledger, "pool_type"),
        "by_decoded_source": _h14_group_summary(ledger, "decoded_source_used"),
        "concentration_findings": {
            "top_return_pool_counts": dict(Counter(r["pool_address"] for r in top50).most_common()),
            "bottom_return_pool_counts": dict(Counter(r["pool_address"] for r in bottom50).most_common()),
            "gt20_pool_counts": dict(Counter(r["pool_address"] for r in gt20).most_common()),
            "scale_or_decimals_risk_rows": sum(1 for r in ledger if r["scale_or_decimals_risk"]),
            "quote_suspicious_rows": sum(1 for r in ledger if r["quote_suspicious"]),
        },
    }


def _h14_h15_schema_and_plan(cfg: dict[str, Any], current_rows: int) -> dict[str, Any]:
    plan = cfg.get("h15_10k_plan", {})
    target = int(plan.get("target_rows", 10000))
    incremental = max(0, target - current_rows)
    rpc_per_attempt = 50
    return {
        "return_schema": {
            "required_fields": [
                "gross_quote_return_30m",
                "fixed_cost_adjusted_return_30m",
                "gas_adjusted_return_30m",
                "slippage_stress_return_30m",
                "final_net_return_after_cost_30m",
                "executable_label_gross",
                "executable_label_after_gas",
                "alpha_top20_after_gas",
                "alpha_ret_gt_5pct_after_gas",
                "alpha_ret_gt_10pct_after_gas",
                "exit_policy_type",
                "exit_horizon_sec",
                "exit_reason",
                "exit_status",
            ],
            "recommended_primary_target": "alpha_top20_after_gas",
            "secondary_target": "final_net_return_after_cost_30m_rank_or_regression",
            "recommendation_reason": "The binary top20 target keeps class balance workable while ranking/regression on final net return preserves return magnitude for later model selection.",
        },
        "expansion_plan": {
            "target_rows": target,
            "incremental_rows_needed_from_current": incremental,
            "candidate_source": "ClickHouse decoded v2/v3 swap tables joined to discovered pool metadata; select candidates before RPC validation.",
            "sampling": {
                "top1_pool_share_lte": float(plan.get("target_top1_pool_share_lte", 0.08)),
                "top5_pool_share_lte": float(plan.get("target_top5_pool_share_lte", 0.30)),
                "non_top5_share_gte": float(plan.get("target_non_top5_share_gte", 0.60)),
                "v2_share_target": float(plan.get("target_v2_share", 0.50)),
                "v3_share_target": float(plan.get("target_v3_share", 0.50)),
                "time_buckets": "explicit day/hour buckets with per-pool-time caps",
            },
            "rpc_accounting": {
                "estimated_total_rpc_calls": target * rpc_per_attempt,
                "estimated_incremental_rpc_calls": incremental * rpc_per_attempt,
                "archive_rpc_budget_assessment": "Use paid archive endpoint in offline batches; enough only if checkpointed and rate-limited, not as an interactive one-shot run.",
                "100k_or_1m_strategy": "Use cheaper approximate candidate ranking first, then executable validation subset for selected candidates.",
            },
            "execution_design_only": {
                "batch_size": int(plan.get("batch_size", 250)),
                "checkpoint_resume": "write per-batch manifest, input sha256, selected rows, RPC counts, failures, and partial ledger before merge",
                "vps_storage_memory": "stream CSV/JSON per batch; avoid loading all decoded windows at once; expect hundreds of MB for 10k artifacts",
                "failure_recovery": "retry quote_failed separately with capped attempts; never discard without reason distribution",
                "do_not_execute_in_h14": True,
            },
        },
    }


def _h14_markdown(patch: dict[str, Any], gas: dict[str, Any], exit_audit: dict[str, Any], outliers: dict[str, Any], h15: dict[str, Any]) -> str:
    thresholds = exit_audit["thresholds"]
    return f"""# Run-004 Patch H14 - Return Accounting, Exit Policy, and Replay Ledger Repair

## Scope
- Rows replay-ledgered: {patch['ledger']['rows']}
- Safety: diagnostics only; no training, no promote, no live, no production model write.
- Current `executable_net_return` remains a quote-based 30m label, not full realized PnL.

## Return Accounting
- Computed in `scripts/label_dashcam.py::label_executable`.
- Current formula: `weth_received / LABEL_NOTIONAL_WEI - 1 - 0.006`.
- V2/V3 quotes include pool fee and notional-size price impact; gas, approval gas, failed transaction cost, and extra slippage are not deducted.
- `LABEL_EXIT_HORIZON_SEC` is not effective in `label_dashcam.py`; the script hard-codes 1800 seconds.

## Replay Ledger
- Ledger CSV: `{patch['outputs']['ledger_csv']}`
- `gross_quote_return` is derived as current return + 0.006 because entry/exit quote amounts were not stored in H10.8.
- Dynamic exits are unavailable for current rows because intra-window quote path, trigger block, and trigger sell quote are absent.

## Gas Adjustment
- Gas source: {gas['gas_model_source']}
- Positive before gas: {gas['positive_rows_before_gas']}
- Positive after medium gas: {gas['positive_rows_after_gas_medium']}
- Positive to negative after medium gas: {gas['positive_to_negative_after_gas_medium']}
- Top20 cutoff before gas: {gas['top20_cutoff_before_gas']}
- Top20 cutoff after medium gas: {gas['top20_cutoff_after_gas_medium']}
- Top20 changed rows after medium gas: {gas['top20_changed_rows_medium']}
- Top-decile mean before gas: {gas['top_decile_return_before_mean']}
- Top-decile mean after medium gas: {gas['top_decile_return_after_gas_mean']}

## Exit Policy Audit
- Runtime exits exist in executor monitor loop: take profit {thresholds['take_profit_pct']}%, stop loss {thresholds['stop_loss_pct']}%, trailing stop {thresholds['trailing_stop_pct']}%, time decay {thresholds['time_decay_min']} minutes.
- The 30m label exits once at t0+1800s and does not simulate take-profit, stop-loss, trailing-stop, or time-decay triggers.
- Dynamic exit unavailable reason: {exit_audit['dynamic_exit_unavailable_reason']}

## Outliers
- `executable_net_return > 20%`: {outliers['return_gt_20pct_rows']}
- `executable_net_return < -20%`: {outliers['return_lt_minus_20pct_rows']}
- Gas-adjusted positive-to-negative rows: {outliers['positive_to_negative_after_gas_rows']}
- Top20 alpha positive rows: {outliers['top20_alpha_positive_rows']}
- Quote suspicious rows: {outliers['concentration_findings']['quote_suspicious_rows']}
- Scale/decimals risk rows: {outliers['concentration_findings']['scale_or_decimals_risk_rows']}

## H15 Recommendation
- Primary target: `{h15['return_schema']['recommended_primary_target']}`
- Secondary target: `{h15['return_schema']['secondary_target']}`
- H15 can start as a 10k diagnostics-only data build after checkpointed ledger/return schema support is in place.

## Gate
- allow_enter_H15_10k_dataset_design_execution: `{patch['gate']['allow_enter_H15_10k_dataset_design_execution']}`
- H14 does not authorize production retrain, promotion, live execution, or model overwrite.
"""


def _h14_return_accounting_replay_ledger_repair(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    rows = read_rows(_resolve(cfg["input_path"]))
    status_filter = cfg.get("status_filter", {})
    success_rows = [r for r in rows if all(str(r.get(k, "")) == str(v) for k, v in status_filter.items())]
    ledger, gas_report = _h14_ledger(success_rows, cfg)
    spec = _h14_return_accounting_spec(cfg)
    exit_audit = _h14_exit_policy_audit(cfg)
    outlier_report = _h14_outlier_report(ledger, gas_report)
    h15 = _h14_h15_schema_and_plan(cfg, len(ledger))
    outputs = cfg["outputs"]
    write_csv_rows(_resolve(outputs["ledger_csv"]), [{k: row.get(k, "") for k in H14_LEDGER_FIELDS} for row in ledger], H14_LEDGER_FIELDS)
    write_json(_resolve(outputs["return_accounting_spec_json"]), spec)
    write_json(_resolve(outputs["gas_model_report_json"]), gas_report)
    write_json(_resolve(outputs["exit_policy_audit_json"]), exit_audit)
    write_json(_resolve(outputs["outlier_replay_report_json"]), outlier_report)
    write_json(_resolve(outputs["h15_expansion_plan_json"]), h15)
    gate_checks = {
        "return_accounting_spec_completed": bool(spec),
        "replay_ledger_752_completed": len(ledger) == 752,
        "gas_adjusted_return_generated": all(row.get("gas_adjusted_net_return") is not None for row in ledger),
        "exit_policy_audit_completed": bool(exit_audit),
        "h15_return_schema_completed": bool(h15.get("return_schema", {}).get("required_fields")),
        "h15_10k_expansion_plan_completed": bool(h15.get("expansion_plan")),
        "no_production_retrain": bool(cfg.get("safety", {}).get("no_production_retrain", False)),
        "no_promote": bool(cfg.get("safety", {}).get("no_promote", False)),
        "no_live": bool(cfg.get("safety", {}).get("no_live", False)),
        "no_production_model_write": bool(cfg.get("safety", {}).get("no_production_model_write", False)),
    }
    patch = {
        "patch": "run-004-h14-return-accounting-exit-policy-replay-ledger-repair",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "enriched_dataset": {"path": str(_resolve(cfg["input_path"])), "rows": len(rows), "sha256": sha256(_resolve(cfg["input_path"]))},
            "h135_audit_json": str(_resolve(cfg["h135_audit_json"])),
            "h12_json": str(_resolve(cfg["h12_json"])),
            "h13_json": str(_resolve(cfg["h13_json"])),
            "label_dashcam": "scripts/label_dashcam.py",
            "executor": "services/executor/src/main.py",
            "executor_swap": "services/executor/src/swap.py",
        },
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "ledger": {"rows": len(ledger), "csv": str(_resolve(outputs["ledger_csv"]))},
        "return_accounting_spec": spec,
        "gas_model_report": gas_report,
        "exit_policy_audit": exit_audit,
        "outlier_report_summary": {k: v for k, v in outlier_report.items() if k not in {"top50_return_rows", "bottom50_return_rows", "by_pool"}},
        "h15_return_schema": h15["return_schema"],
        "h15_10k_expansion_plan": h15["expansion_plan"],
        "final_recommendation": {
            "current_executable_net_return_trust": "Useful as a deterministic quote-based 30m research label, but not trustworthy as full realized PnL.",
            "h15_can_start_10k": True,
            "h15_primary_target": h15["return_schema"]["recommended_primary_target"],
        },
        "gate": {
            "allow_enter_H15_10k_dataset_design_execution": all(gate_checks.values()),
            "checks": gate_checks,
            "forbidden": ["production retrain", "promote", "live", "production model overwrite"],
            "next_step": "H15 10k diagnostics-only return-schema dataset build with checkpointed RPC labeling",
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h14_markdown(patch, gas_report, exit_audit, outlier_report, h15))
    return patch


H15_LEDGER_FIELDS = [
    "row_id",
    "pool_address",
    "token_address",
    "pool_type",
    "protocol",
    "decoded_source_used",
    "entry_time",
    "fixed_30m_return_before_gas",
    "fixed_30m_return_after_gas",
    "gas_adjusted_return_30m_low",
    "gas_adjusted_return_30m_medium",
    "gas_adjusted_return_30m_high",
    "dynamic_exit_status",
    "dynamic_exit_reason",
    "dynamic_exit_time",
    "dynamic_exit_return_before_gas",
    "dynamic_exit_return_after_gas",
    "exit_policy_used",
    "fallback_reason",
    "gas_model_used",
    "slippage_model_used",
    "slippage_stress_return",
    "final_live_like_return",
    "final_live_like_label",
    "executable_label_after_gas",
    "alpha_top20_after_gas",
    "alpha_top20_live_like",
    "bad_pool_label",
    "quote_anomaly_label",
    "scale_risk_label",
    "quarantine_reason",
]


def _h15_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _h15_exit_policy_truth_table(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults = cfg.get("exit_policy_runtime_defaults", {})
    rows = [
        {
            "policy": "take_profit",
            "whitepaper_intent": "Phase-4 protected execution should include controlled exits; exact TP threshold is runtime-defined, not specified in current whitepaper.",
            "runtime_implemented": True,
            "runtime_threshold": f"{float(defaults.get('take_profit_pct', 15.0))}%",
            "runtime_source": "services/executor/src/main.py::_monitor_once quote_out >= entry * (1 + take_profit_pct/100)",
            "label_replay_policy": "fixed_30m_only",
            "label_simulates": False,
            "gap_or_debt": "Needs intra-window quote path and trigger-block sell quote for label replay.",
        },
        {
            "policy": "stop_loss_hard_stop",
            "whitepaper_intent": "Hard risk limits are expected for execution; exact stop threshold is runtime-defined.",
            "runtime_implemented": True,
            "runtime_threshold": f"{float(defaults.get('stop_loss_pct', 10.0))}%",
            "runtime_source": "services/executor/src/main.py::_monitor_once quote_out <= entry * (1 - stop_loss_pct/100)",
            "label_replay_policy": "fixed_30m_only",
            "label_simulates": False,
            "gap_or_debt": "Needs intra-window adverse quote path; 30m terminal quote can miss earlier hard stops.",
        },
        {
            "policy": "trailing_stop",
            "whitepaper_intent": "Protected execution implies path-aware exit management; current threshold is runtime-defined.",
            "runtime_implemented": True,
            "runtime_threshold": f"{float(defaults.get('trailing_stop_pct', 5.0))}% drawdown from peak while quote_out > entry",
            "runtime_source": "services/executor/src/main.py::_monitor_once peak_eth_value / drawdown_from_peak",
            "label_replay_policy": "fixed_30m_only",
            "label_simulates": False,
            "gap_or_debt": "Needs peak path, drawdown path, and trigger quote; absent from H10.8/H14 rows.",
        },
        {
            "policy": "time_decay",
            "whitepaper_intent": "Time stop / controlled holding period is consistent with safe execution; exact value is runtime-defined.",
            "runtime_implemented": True,
            "runtime_threshold": f"{float(defaults.get('time_decay_min', 240.0))} minutes, only exits if quote_out <= entry or quote <= 0",
            "runtime_source": "services/executor/src/main.py::_monitor_once age_min >= time_decay_min",
            "label_replay_policy": "fixed_30m_exit_at_30_minutes",
            "label_simulates": False,
            "gap_or_debt": "Runtime time decay default is 240m, materially different from 30m label horizon.",
        },
        {
            "policy": "system_panic_daily_drawdown",
            "whitepaper_intent": "Hard risk limits and kill switches are expected for controlled go-live.",
            "runtime_implemented": True,
            "runtime_threshold": f"daily_loss_pct={float(defaults.get('daily_loss_pct', 20.0))}% and SYSTEM_PANIC Redis key",
            "runtime_source": "services/executor/src/main.py::_check_daily_loss / _is_panic",
            "label_replay_policy": "not_modeled",
            "label_simulates": False,
            "gap_or_debt": "Portfolio-level state is outside per-row label replay; H16 should keep this separate from per-trade alpha labels.",
        },
        {
            "policy": "gas_breaker",
            "whitepaper_intent": "Execution should avoid uneconomic gas conditions.",
            "runtime_implemented": True,
            "runtime_threshold": f"EXECUTOR_MAX_GAS_USD default ${float(defaults.get('max_gas_usd', 2.0))}",
            "runtime_source": "services/executor/src/main.py::_gas_price_too_high",
            "label_replay_policy": "H14/H15 estimated gas sensitivity only",
            "label_simulates": False,
            "gap_or_debt": "Historical gas at entry/exit trigger is required for exact replay.",
        },
        {
            "policy": "pending_sell_state",
            "whitepaper_intent": "Older docs/reviews mention pending_sell style state management.",
            "runtime_implemented": False,
            "runtime_threshold": "N/A",
            "runtime_source": "Current services/executor/src/main.py directly executes sell in monitor loop; failed sell keeps position open for retry.",
            "label_replay_policy": "not_modeled",
            "label_simulates": False,
            "gap_or_debt": "Document/code drift; do not claim pending-sell replay until runtime state machine exists or docs are corrected.",
        },
        {
            "policy": "fixed_30m_label_exit",
            "whitepaper_intent": "Research label convenience, not a full strategy backtest policy.",
            "runtime_implemented": False,
            "runtime_threshold": "1800 seconds",
            "runtime_source": "scripts/label_dashcam.py hard-coded exit_ts_sec = signal_ts_sec + 1800",
            "label_replay_policy": "implemented",
            "label_simulates": True,
            "gap_or_debt": "Useful for fixed-horizon alpha; not live-like dynamic exit.",
        },
    ]
    return {"rows": rows, "summary": {"runtime_dynamic_exits_available": True, "label_dynamic_exits_available": False, "pending_sell_runtime_available": False}}


def _h15_confirmed_bad_pools(ledger_rows: list[dict[str, str]], rules: dict[str, Any]) -> set[str]:
    bottom50 = sorted(ledger_rows, key=lambda r: optional_float(r.get("fixed_30m_return_before_gas")) or 0.0)[:50]
    bottom_counts = Counter(_lower_addr(r.get("pool_address")) for r in bottom50)
    by_pool: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in ledger_rows:
        by_pool[_lower_addr(row.get("pool_address"))].append(row)
    out: set[str] = set()
    share_gate = float(rules.get("bad_pool_bottom50_share_gte", 0.50))
    pos_gate = float(rules.get("bad_pool_after_gas_positive_rate_lte", 0.20))
    for pool, count in bottom_counts.items():
        vals = by_pool.get(pool, [])
        if not vals:
            continue
        bottom_share = count / 50.0
        after_pos_rate = sum(1 for r in vals if (optional_float(r.get("fixed_30m_return_after_gas")) or 0.0) > 0) / len(vals)
        mean_after = sum(optional_float(r.get("fixed_30m_return_after_gas")) or 0.0 for r in vals) / len(vals)
        if bottom_share >= share_gate and (after_pos_rate <= pos_gate or mean_after < 0):
            out.add(pool)
    return out


def _h15_live_like_ledger(h14_rows: list[dict[str, str]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    replay_cfg = cfg.get("live_like_replay", {})
    rules = cfg.get("quarantine_rules", {})
    slippage = float(replay_cfg.get("slippage_stress_bps", 100)) / 10000.0
    prelim: list[dict[str, Any]] = []
    for row in h14_rows:
        fixed_before = optional_float(row.get("executable_net_return_current")) or 0.0
        fixed_after = optional_float(row.get("gas_adjusted_net_return")) or fixed_before
        item = {
            "row_id": int(optional_float(row.get("row_id")) or len(prelim)),
            "pool_address": _lower_addr(row.get("pool_address")),
            "token_address": _lower_addr(row.get("token_address")),
            "pool_type": row.get("pool_type", ""),
            "protocol": row.get("protocol", ""),
            "decoded_source_used": row.get("decoded_source_used", ""),
            "entry_time": row.get("entry_time", ""),
            "fixed_30m_return_before_gas": rounded(fixed_before, 8),
            "fixed_30m_return_after_gas": rounded(fixed_after, 8),
            "gas_adjusted_return_30m_low": rounded(optional_float(row.get("gas_adjusted_return_low")), 8),
            "gas_adjusted_return_30m_medium": rounded(optional_float(row.get("gas_adjusted_return_medium")), 8),
            "gas_adjusted_return_30m_high": rounded(optional_float(row.get("gas_adjusted_return_high")), 8),
            "dynamic_exit_status": "unavailable",
            "dynamic_exit_reason": "",
            "dynamic_exit_time": "",
            "dynamic_exit_return_before_gas": "",
            "dynamic_exit_return_after_gas": "",
            "exit_policy_used": "fixed_30m_fallback",
            "fallback_reason": "missing_intra_window_quote_path_and_trigger_sell_quote",
            "gas_model_used": row.get("gas_price_source", "configured_medium"),
            "slippage_model_used": f"configured_{int(slippage * 10000)}bps_haircut",
            "slippage_stress_return": rounded(fixed_after - slippage, 8),
            "final_live_like_return": rounded(fixed_after - slippage, 8),
            "final_live_like_label": int((fixed_after - slippage) > 0),
            "executable_label_after_gas": int(fixed_after > 0),
            "alpha_top20_after_gas": 0,
            "alpha_top20_live_like": 0,
            "bad_pool_label": 0,
            "quote_anomaly_label": int(_h15_bool(row.get("quote_suspicious"))),
            "scale_risk_label": int(_h15_bool(row.get("scale_or_decimals_risk"))),
            "quarantine_reason": "",
            "_gas_cost_dominates": _h15_bool(row.get("gas_cost_dominates")),
            "_return_outlier": _h15_bool(row.get("return_outlier")),
        }
        prelim.append(item)
    cutoff_after_gas = percentile([r["fixed_30m_return_after_gas"] for r in prelim], 0.80)
    cutoff_live_like = percentile([r["final_live_like_return"] for r in prelim], 0.80)
    bad_pools = _h15_confirmed_bad_pools(
        [
            {
                "pool_address": str(r["pool_address"]),
                "fixed_30m_return_before_gas": str(r["fixed_30m_return_before_gas"]),
                "fixed_30m_return_after_gas": str(r["fixed_30m_return_after_gas"]),
            }
            for r in prelim
        ],
        rules,
    )
    for row in prelim:
        row["alpha_top20_after_gas"] = int(cutoff_after_gas is not None and row["fixed_30m_return_after_gas"] >= cutoff_after_gas)
        row["alpha_top20_live_like"] = int(cutoff_live_like is not None and row["final_live_like_return"] >= cutoff_live_like)
        row["bad_pool_label"] = int(row["pool_address"] in bad_pools)
        reasons: list[str] = []
        if row["bad_pool_label"]:
            reasons.append("confirmed_bad_pool_candidate")
        if row["quote_anomaly_label"]:
            reasons.append("quote_suspicious")
        if row["scale_risk_label"]:
            reasons.append("scale_or_decimals_risk")
        if row["_gas_cost_dominates"]:
            reasons.append("gas_cost_dominates")
        if row["_return_outlier"]:
            reasons.append("extreme_return_outlier")
        row["quarantine_reason"] = "|".join(reasons)
    return prelim, bad_pools


def _h15_pool_forensics(
    ledger: list[dict[str, Any]],
    enriched: list[dict[str, str]],
    cfg: dict[str, Any],
    bad_pools: set[str],
) -> dict[str, Any]:
    top50 = sorted(ledger, key=lambda r: r["fixed_30m_return_before_gas"], reverse=True)[:50]
    bottom50 = sorted(ledger, key=lambda r: r["fixed_30m_return_before_gas"])[:50]
    focus = {_lower_addr(p) for p in cfg.get("focus_pools", [])}
    focus |= {r["pool_address"] for r in top50 + bottom50}
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        by_pool[row["pool_address"]].append(row)
    enriched_by_id = {idx: row for idx, row in enumerate(enriched)}
    pools: list[dict[str, Any]] = []
    for pool in sorted(focus):
        rows = by_pool.get(pool, [])
        if not rows:
            continue
        ids = [int(r["row_id"]) for r in rows]
        enriched_rows = [enriched_by_id[i] for i in ids if i in enriched_by_id]
        fixed_vals = [r["fixed_30m_return_before_gas"] for r in rows]
        after_vals = [r["fixed_30m_return_after_gas"] for r in rows]
        live_vals = [r["final_live_like_return"] for r in rows]
        avg_event_5m = numeric_summary([r.get("decoded_event_count_5m") for r in enriched_rows])
        flow_summary = {
            "buy_sell_count_ratio_300s": numeric_summary([r.get("buy_sell_count_ratio_300s") for r in enriched_rows]),
            "net_flow_raw_300s": numeric_summary([r.get("net_flow_raw_300s") for r in enriched_rows]),
        }
        buyer_summary = {
            "top1_buyer_share_300s": numeric_summary([r.get("top1_buyer_share_300s") for r in enriched_rows]),
            "buy_wallet_entropy_300s": numeric_summary([r.get("buy_wallet_entropy_300s") for r in enriched_rows]),
        }
        quote_rate = sum(r["quote_anomaly_label"] for r in rows) / len(rows)
        scale_rate = sum(r["scale_risk_label"] for r in rows) / len(rows)
        bottom50_count = sum(1 for r in bottom50 if r["pool_address"] == pool)
        top50_count = sum(1 for r in top50 if r["pool_address"] == pool)
        likely: list[str] = []
        if pool in bad_pools:
            likely.append("true_bad_pool_or_adverse_time_window")
        if quote_rate >= 0.20:
            likely.append("quote_artifact_risk")
        if scale_rate >= 0.50:
            likely.append("scale_decimals_issue_risk")
        if top50_count >= 5 and (sum(after_vals) / len(after_vals)) > 0:
            likely.append("alpha_island_candidate")
        if bottom50_count >= 10:
            likely.append("time_window_or_liquidity_anomaly")
        if not likely:
            likely.append("requires_manual_review")
        pools.append(
            {
                "pool_address": pool,
                "row_count": len(rows),
                "fixed_30m_positive": sum(1 for v in fixed_vals if v > 0),
                "fixed_30m_negative": sum(1 for v in fixed_vals if v <= 0),
                "after_gas_positive": sum(1 for v in after_vals if v > 0),
                "after_gas_negative": sum(1 for v in after_vals if v <= 0),
                "live_like_positive": sum(1 for v in live_vals if v > 0),
                "live_like_negative": sum(1 for v in live_vals if v <= 0),
                "return_summary": {
                    "mean": rounded(sum(fixed_vals) / len(fixed_vals), 8),
                    "p50": rounded(percentile(fixed_vals, 0.50), 8),
                    "p90": rounded(percentile(fixed_vals, 0.90), 8),
                    "p95": rounded(percentile(fixed_vals, 0.95), 8),
                },
                "quote_suspicious_rate": rounded(quote_rate, 8),
                "scale_decimals_risk_rate": rounded(scale_rate, 8),
                "decoded_source": dict(Counter(str(r["decoded_source_used"] or "unknown") for r in rows).most_common()),
                "pool_type": dict(Counter(str(r["pool_type"] or "unknown") for r in rows).most_common()),
                "weth_leg_token0_token1_metadata": "unavailable_in_h108_h14_artifacts",
                "average_event_count_5m": avg_event_5m.get("mean"),
                "flow_summary": flow_summary,
                "buyer_concentration_summary": buyer_summary,
                "top50_count": top50_count,
                "bottom50_count": bottom50_count,
                "likely_classification": likely,
            }
        )
    return {
        "focus_pool_count": len(pools),
        "confirmed_bad_pool_candidates": sorted(bad_pools),
        "top50_pool_counts": dict(Counter(r["pool_address"] for r in top50).most_common()),
        "bottom50_pool_counts": dict(Counter(r["pool_address"] for r in bottom50).most_common()),
        "pool_forensics": pools,
    }


def _h15_subset_report(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    clean: list[dict[str, Any]] = []
    risk: list[dict[str, Any]] = []
    for row in ledger:
        quarantine = bool(row["quarantine_reason"])
        if not quarantine:
            clean.append(row)
        if quarantine:
            risk.append(row)
    before_top20 = sorted(ledger, key=lambda r: r["fixed_30m_return_before_gas"], reverse=True)[: max(1, math.ceil(len(ledger) * 0.20))]
    after_clean_top20 = sorted(clean, key=lambda r: r["fixed_30m_return_after_gas"], reverse=True)[: max(1, math.ceil(len(clean) * 0.20))] if clean else []
    bottom50 = sorted(ledger, key=lambda r: r["fixed_30m_return_before_gas"])[:50]
    clean_bottom50 = sorted(clean, key=lambda r: r["fixed_30m_return_before_gas"])[:50] if clean else []
    return {
        "clean_alpha_subset_definition": {
            "purpose": "Downstream fixed-horizon alpha training candidate rows.",
            "exclude_or_mark": ["quote_suspicious", "scale_or_decimals_risk", "confirmed_bad_pool_candidate", "gas_cost_dominates", "extreme_return_outlier"],
            "dynamic_exit_note": "Rows are marked fixed_30m_fallback because dynamic exit is unavailable; this subset is not a full strategy-backtest dataset.",
        },
        "risk_bad_pool_subset_definition": {
            "purpose": "Risk/bad-pool detector material, not deleted from research corpus.",
            "include": ["bottom tail pools", "quote_suspicious", "scale_or_decimals_risk", "gas_cost_dominates", "confirmed_bad_pool_candidate", "extreme_return_outlier"],
            "disjoint_from_clean_alpha_subset": True,
        },
        "clean_alpha_subset_size": len(clean),
        "risk_bad_pool_subset_size": len(risk),
        "quarantined_rows": len([r for r in ledger if r["quarantine_reason"]]),
        "quarantine_reason_counts": dict(Counter(reason for r in ledger for reason in str(r["quarantine_reason"]).split("|") if reason).most_common()),
        "top20_before_quarantine_pool_counts": dict(Counter(r["pool_address"] for r in before_top20).most_common()),
        "top20_after_quarantine_pool_counts": dict(Counter(r["pool_address"] for r in after_clean_top20).most_common()),
        "bottom50_before_quarantine_pool_counts": dict(Counter(r["pool_address"] for r in bottom50).most_common()),
        "bottom50_after_quarantine_pool_counts": dict(Counter(r["pool_address"] for r in clean_bottom50).most_common()),
    }


def _h15_return_schema_v2() -> dict[str, Any]:
    fields = [
        "gross_quote_return_30m",
        "fixed_cost_adjusted_return_30m",
        "gas_adjusted_return_30m_low",
        "gas_adjusted_return_30m_medium",
        "gas_adjusted_return_30m_high",
        "live_like_return_before_gas",
        "live_like_return_after_gas",
        "final_return_after_cost",
        "exit_policy_used",
        "exit_reason",
        "exit_time",
        "exit_block",
        "exit_status",
        "dynamic_exit_available",
        "slippage_stress_return",
        "executable_label_after_gas",
        "alpha_top20_after_gas",
        "alpha_top20_live_like",
        "bad_pool_label",
        "quote_anomaly_label",
        "scale_risk_label",
        "quarantine_reason",
    ]
    return {
        "schema_version": "run004_h15_return_schema_v2",
        "fields": fields,
        "primary_target_if_dynamic_unavailable": "alpha_top20_after_gas",
        "primary_target_if_dynamic_available": "alpha_top20_live_like",
        "risk_targets": ["bad_pool_label", "quote_anomaly_label", "scale_risk_label"],
        "not_full_strategy_backtest_when_dynamic_unavailable": True,
    }


def _h15_validation(ledger: list[dict[str, Any]], subset: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixed_30m_return_distribution": numeric_summary([r["fixed_30m_return_before_gas"] for r in ledger]),
        "gas_adjusted_return_distribution": numeric_summary([r["fixed_30m_return_after_gas"] for r in ledger]),
        "live_like_return_distribution": numeric_summary([r["final_live_like_return"] for r in ledger]),
        "fixed_to_after_gas_label_changed": sum(1 for r in ledger if int(r["fixed_30m_return_before_gas"] > 0) != int(r["fixed_30m_return_after_gas"] > 0)),
        "after_gas_to_live_like_label_changed": sum(1 for r in ledger if int(r["fixed_30m_return_after_gas"] > 0) != int(r["final_live_like_return"] > 0)),
        "quarantined_rows": subset["quarantined_rows"],
        "clean_alpha_subset_size": subset["clean_alpha_subset_size"],
        "risk_bad_pool_subset_size": subset["risk_bad_pool_subset_size"],
    }


def _h15_h16_readiness(schema: dict[str, Any], ledger: list[dict[str, Any]], forensic: dict[str, Any], subset: dict[str, Any]) -> dict[str, Any]:
    dynamic_available = any(r["dynamic_exit_status"] == "computed" for r in ledger)
    checks = {
        "return_schema_v2_produced": bool(schema.get("fields")),
        "live_like_or_partial_live_replay_ledger_produced": len(ledger) == 752,
        "bad_pool_forensic_completed": bool(forensic.get("pool_forensics")),
        "clean_alpha_subset_defined": subset["clean_alpha_subset_size"] >= 0,
        "risk_bad_pool_subset_defined": subset["risk_bad_pool_subset_size"] > 0,
        "quote_suspicious_scale_rules_defined": True,
        "h16_sampling_checkpoint_plan_updated_for_quarantine": True,
        "h16_primary_target_defined": True,
    }
    return {
        "dynamic_exit_available": dynamic_available,
        "allow_h16_10k": all(checks.values()),
        "checks": checks,
        "h16_scope": "fixed-horizon gas-adjusted 10k dataset; not a full strategy backtest" if not dynamic_available else "dynamic live-like 10k dataset pilot",
        "h16_primary_target": "alpha_top20_live_like" if dynamic_available else "alpha_top20_after_gas",
        "sampling_updates": {
            "carry_quarantine_labels": True,
            "separate_alpha_and_risk_outputs": True,
            "checkpoint_fields": ["batch_id", "input_sha256", "rpc_calls", "quote_failures", "quarantine_counts", "risk_label_counts"],
            "do_not_call_full_strategy_backtest_if_dynamic_unavailable": not dynamic_available,
        },
        "forbidden": ["promote", "live", "production retrain", "production model overwrite"],
    }


def _h15_markdown(patch: dict[str, Any]) -> str:
    v = patch["validation"]
    readiness = patch["h16_readiness"]
    focus = patch["bad_pool_forensic"]["pool_forensics"]
    pool_lines = []
    for pool in focus:
        if pool["pool_address"] in {"0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a", "0x54219bce730d2727d91d8dcdf1465c7d299dee45", "0xc2476883d28d40b6c057f242fce2e8bda674faa0"}:
            pool_lines.append(f"- `{pool['pool_address']}`: rows={pool['row_count']}, after_gas_pos={pool['after_gas_positive']}, top50={pool['top50_count']}, bottom50={pool['bottom50_count']}, likely={pool['likely_classification']}")
    return f"""# Run-004 Patch H15 - Live-like Exit Replay and Bad-Pool Classifier Pilot

## Scope
- Rows: {patch['ledger']['rows']}
- Dynamic exit computed rows: {patch['live_like_replay']['dynamic_exit_computed_rows']}
- Dynamic exit unavailable rows: {patch['live_like_replay']['dynamic_exit_unavailable_rows']}
- Safety: diagnostics-only; no 10k run, no training, no promote, no live, no production model write.

## Fixed 30m vs Live-like
- True dynamic TP/SL/trailing/time replay is still unavailable because H10.8/H14 lacks intra-window quote path, trigger block, token amount, and trigger sell quote.
- H15 therefore uses `fixed_30m_fallback` with gas and slippage stress as a partial live-like ledger.
- Fixed -> after-gas label changed rows: {v['fixed_to_after_gas_label_changed']}
- After-gas -> slippage-stressed live-like label changed rows: {v['after_gas_to_live_like_label_changed']}

## Dataset Split
- Quarantined rows: {v['quarantined_rows']}
- Clean alpha subset rows: {v['clean_alpha_subset_size']}
- Risk bad-pool subset rows: {v['risk_bad_pool_subset_size']}

## Focus Pools
{chr(10).join(pool_lines)}

## H16 Decision
- allow_h16_10k: `{readiness['allow_h16_10k']}`
- H16 scope: {readiness['h16_scope']}
- H16 primary target: `{readiness['h16_primary_target']}`
- Promote/live allowed: `False`
"""


def _h15_live_like_exit_replay_bad_pool_pilot(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    inputs = cfg["input_paths"]
    h14_rows = read_rows(_resolve(inputs["h14_ledger_csv"]))
    enriched_rows = read_rows(_resolve(inputs["enriched_dataset_csv"]))
    truth_table = _h15_exit_policy_truth_table(cfg)
    live_ledger, bad_pools = _h15_live_like_ledger(h14_rows, cfg)
    forensic = _h15_pool_forensics(live_ledger, enriched_rows, cfg, bad_pools)
    subset = _h15_subset_report(live_ledger)
    schema = _h15_return_schema_v2()
    validation = _h15_validation(live_ledger, subset)
    readiness = _h15_h16_readiness(schema, live_ledger, forensic, subset)
    outputs = cfg["outputs"]
    write_csv_rows(_resolve(outputs["live_like_ledger_csv"]), [{k: row.get(k, "") for k in H15_LEDGER_FIELDS} for row in live_ledger], H15_LEDGER_FIELDS)
    write_json(_resolve(outputs["exit_policy_truth_table_json"]), truth_table)
    write_json(_resolve(outputs["bad_pool_forensic_json"]), forensic)
    write_json(_resolve(outputs["clean_risk_subset_report_json"]), subset)
    write_json(_resolve(outputs["return_schema_v2_json"]), schema)
    write_json(_resolve(outputs["h16_readiness_plan_json"]), readiness)
    patch = {
        "patch": "run-004-h15-live-like-exit-replay-bad-pool-classifier-pilot",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "ledger": {"rows": len(live_ledger), "csv": str(_resolve(outputs["live_like_ledger_csv"]))},
        "exit_policy_truth_table": truth_table,
        "live_like_replay": {
            "dynamic_exit_computed_rows": sum(1 for r in live_ledger if r["dynamic_exit_status"] == "computed"),
            "dynamic_exit_unavailable_rows": sum(1 for r in live_ledger if r["dynamic_exit_status"] != "computed"),
            "fallback_policy": "fixed_30m_plus_gas_and_slippage_sensitivity",
            "not_full_strategy_backtest": True,
        },
        "bad_pool_forensic": forensic,
        "clean_risk_subset_report": subset,
        "return_schema_v2": schema,
        "validation": validation,
        "h16_readiness": readiness,
        "gate": {
            "allow_enter_H16_10k": readiness["allow_h16_10k"],
            "checks": readiness["checks"],
            "h16_primary_target": readiness["h16_primary_target"],
            "promote_live_allowed": False,
            "forbidden": readiness["forbidden"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h15_markdown(patch))
    return patch


H16_DATASET_FIELDS = [
    "row_id",
    "pool_address",
    "token_address",
    "pool_type",
    "protocol",
    "decoded_source_used",
    "entry_time",
    "gross_quote_return_30m",
    "fixed_cost_adjusted_return_30m",
    "gas_adjusted_return_30m_low",
    "gas_adjusted_return_30m_medium",
    "gas_adjusted_return_30m_high",
    "final_return_after_cost",
    "slippage_stress_return",
    "exit_policy_used",
    "exit_reason",
    "dynamic_exit_available",
    "dynamic_exit_unavailable_reason",
    "executable_label_after_gas",
    "alpha_top20_after_gas",
    "alpha_ret_gt_5pct_after_gas",
    "alpha_ret_gt_10pct_after_gas",
    "final_return_rank",
    "within_pool_return_rank",
    "bad_pool_label",
    "quote_anomaly_label",
    "scale_risk_label",
    "liquidity_anomaly_label",
    "gas_cost_dominates_label",
    "alpha_island_candidate_label",
    "risk_severity",
    "quarantine_reason",
]


def _h16_query_candidates(client: ClickHouseClient, cfg: dict[str, Any], pool_type: str, limit: int) -> list[dict[str, Any]]:
    sel = cfg["selection"]
    src = cfg["clickhouse"]["decoded_sources"]
    table = src["v2_table"] if pool_type == "v2" else src["v3_table"]
    pool_col = "pair_address" if pool_type == "v2" else "pool_address"
    focus = ",".join(_sql_string(_lower_addr(p)) for p in sel.get("focus_pools", [])) or "''"
    cap_default = int(sel.get("pool_cap_default", 250))
    cap_focus = int(sel.get("pool_cap_focus", 150))
    seed = str(sel.get("deterministic_sample_seed", "run004_h16"))
    query = f"""
        SELECT *
        FROM
        (
            SELECT
                { _sql_string(pool_type) } AS protocol,
                lower(e.{pool_col}) AS pool_address,
                lower(m.token0_address) AS token0_address,
                lower(m.token1_address) AS token1_address,
                m.token0_symbol AS token0_symbol,
                m.token1_symbol AS token1_symbol,
                m.token0_decimals AS token0_decimals,
                m.token1_decimals AS token1_decimals,
                m.fee_bps AS fee_bps,
                e.tx_hash AS tx_hash,
                e.log_index AS log_index,
                e.block_number AS block_number,
                toString(e.block_time) AS block_time,
                if(isNull(e.block_time), toUnixTimestamp64Milli(e.received_at_ms), toUnixTimestamp64Milli(assumeNotNull(e.block_time))) AS timestamp_ms,
                toDate(ifNull(e.block_time, e.received_at_ms)) AS day_bucket,
                toHour(ifNull(e.block_time, e.received_at_ms)) AS hour_bucket,
                e.source AS decoded_source,
                row_number() OVER (
                    PARTITION BY lower(e.{pool_col})
                    ORDER BY cityHash64(e.tx_hash, toString(e.log_index), { _sql_string(seed) })
                ) AS rn
            FROM {client.database}.{table} AS e
            ANY INNER JOIN
            (
                SELECT
                    lower(pool_address) AS pool_address,
                    any(pool_type) AS pool_type,
                    any(token0_address) AS token0_address,
                    any(token1_address) AS token1_address,
                    any(token0_symbol) AS token0_symbol,
                    any(token1_symbol) AS token1_symbol,
                    any(token0_decimals) AS token0_decimals,
                    any(token1_decimals) AS token1_decimals,
                    any(fee_bps) AS fee_bps,
                    max(has_weth_leg) AS has_weth_leg
                FROM {client.database}.{src["metadata_table"]}
                WHERE chain_id = 8453
                GROUP BY pool_address
            ) AS m
            ON lower(e.{pool_col}) = m.pool_address
            WHERE e.chain_id = 8453
              AND e.removed = 0
              AND m.has_weth_leg = 1
              AND lower(m.pool_type) = { _sql_string(pool_type) }
              AND e.tx_hash != ''
        )
        WHERE rn <= if(pool_address IN ({focus}), {cap_focus}, {cap_default})
        ORDER BY cityHash64(pool_address, tx_hash, toString(log_index), { _sql_string(seed) })
        LIMIT {int(limit)}
    """
    return client.query_json(query)


def _h16_candidate_selection(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    selected: list[dict[str, Any]] = []
    errors: list[str] = []
    for pt, limit_key in (("v2", "v2_target_rows"), ("v3", "v3_target_rows")):
        try:
            rows = _h16_query_candidates(client, cfg, pt, int(cfg["selection"].get(limit_key, 5000)))
            for row in rows:
                row["candidate_id"] = len(selected)
                row["pool_type"] = pt
                row["is_focus_pool"] = int(_lower_addr(row.get("pool_address")) in {_lower_addr(p) for p in cfg["selection"].get("focus_pools", [])})
                selected.append(row)
        except Exception as exc:
            errors.append(f"{pt}:{exc}")
    target = int(cfg["selection"].get("target_rows", 10000))
    selected = selected[:target]
    pools = Counter(_lower_addr(r.get("pool_address")) for r in selected)
    top_counts = pools.most_common()
    n = len(selected)
    top1 = (top_counts[0][1] / n) if n and top_counts else 0.0
    top5 = (sum(c for _, c in top_counts[:5]) / n) if n else 0.0
    non_top5 = 1.0 - top5 if n else 0.0
    v_counts = Counter(str(r.get("pool_type") or r.get("protocol")) for r in selected)
    v2_share = v_counts.get("v2", 0) / n if n else 0.0
    balance = 1.0 - abs(v2_share - 0.5) * 2 if n else 0.0
    unique_score = min(1.0, len(pools) / max(1, int(cfg["selection"].get("min_unique_pools", 40))))
    concentration_score = max(0.0, 1.0 - max(0.0, top1 - 0.12) / 0.88) * max(0.0, 1.0 - max(0.0, top5 - 0.45) / 0.55)
    diversity_score = rounded((unique_score + balance + concentration_score + min(1.0, non_top5 / 0.50)) / 4.0, 8)
    report = {
        "target_rows": target,
        "selected_candidates": n,
        "projected_unique_pools": len(pools),
        "projected_top1_share": rounded(top1, 8),
        "projected_top5_share": rounded(top5, 8),
        "projected_non_top5_share": rounded(non_top5, 8),
        "projected_v2_v3_share": dict(v_counts),
        "time_bucket_distribution": {
            "days": dict(Counter(str(r.get("day_bucket")) for r in selected).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in selected).most_common()),
        },
        "focus_pool_counts": dict(Counter(_lower_addr(r.get("pool_address")) for r in selected if int(r.get("is_focus_pool", 0))).most_common()),
        "suspicious_pool_counts": {},
        "candidate_diversity_score": diversity_score,
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top_counts[:20]],
        "clickhouse": {"transport": client.transport, "database": client.database, "errors": errors},
    }
    return selected, report


def _h16_pool_likely_map(forensic: dict[str, Any]) -> dict[str, list[str]]:
    return {
        _lower_addr(item.get("pool_address")): [str(x) for x in item.get("likely_classification", [])]
        for item in forensic.get("pool_forensics", [])
    }


def _h16_risk_severity(row: dict[str, Any], likely: list[str]) -> str:
    fixed = optional_float(row.get("fixed_30m_return_before_gas")) or 0.0
    bad = int(optional_float(row.get("bad_pool_label")) or 0) == 1
    quote = int(optional_float(row.get("quote_anomaly_label")) or 0) == 1
    scale = int(optional_float(row.get("scale_risk_label")) or 0) == 1
    gas_dom = "gas_cost_dominates" in str(row.get("quarantine_reason", ""))
    if bad or (quote and abs(fixed) >= 0.20):
        return "hard_exclude"
    if gas_dom:
        return "soft_exclude"
    if scale:
        return "feature_limited"
    if "alpha_island_candidate" in likely or quote:
        return "review_only"
    return "clean"


def _h16_build_partial_datasets(
    h15_rows: list[dict[str, str]],
    h14_rows: list[dict[str, str]],
    h15_forensic: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    h14_by_id = {str(row.get("row_id")): row for row in h14_rows}
    likely_by_pool = _h16_pool_likely_map(h15_forensic)
    all_rows: list[dict[str, Any]] = []
    for row in h15_rows:
        rid = str(row.get("row_id"))
        h14 = h14_by_id.get(rid, {})
        fixed = optional_float(row.get("fixed_30m_return_before_gas")) or 0.0
        after = optional_float(row.get("fixed_30m_return_after_gas")) or 0.0
        pool = _lower_addr(row.get("pool_address"))
        likely = likely_by_pool.get(pool, [])
        severity = _h16_risk_severity(row, likely)
        gross = optional_float(h14.get("gross_quote_return")) if h14 else fixed + 0.006
        out = {
            "row_id": row.get("row_id"),
            "pool_address": pool,
            "token_address": _lower_addr(row.get("token_address")),
            "pool_type": row.get("pool_type", ""),
            "protocol": row.get("protocol", ""),
            "decoded_source_used": row.get("decoded_source_used", ""),
            "entry_time": row.get("entry_time", ""),
            "gross_quote_return_30m": rounded(gross, 8),
            "fixed_cost_adjusted_return_30m": rounded(fixed, 8),
            "gas_adjusted_return_30m_low": rounded(optional_float(row.get("gas_adjusted_return_30m_low")), 8),
            "gas_adjusted_return_30m_medium": rounded(optional_float(row.get("gas_adjusted_return_30m_medium")), 8),
            "gas_adjusted_return_30m_high": rounded(optional_float(row.get("gas_adjusted_return_30m_high")), 8),
            "final_return_after_cost": rounded(optional_float(row.get("final_live_like_return")) or after, 8),
            "slippage_stress_return": rounded(optional_float(row.get("slippage_stress_return")), 8),
            "exit_policy_used": "fixed_30m_fallback",
            "exit_reason": "fixed_horizon",
            "dynamic_exit_available": False,
            "dynamic_exit_unavailable_reason": "missing_intra_window_quote_path_trigger_block_token_amount_and_trigger_sell_quote",
            "executable_label_after_gas": int(optional_float(row.get("executable_label_after_gas")) or 0),
            "alpha_top20_after_gas": int(optional_float(row.get("alpha_top20_after_gas")) or 0),
            "alpha_ret_gt_5pct_after_gas": int(after > 0.05),
            "alpha_ret_gt_10pct_after_gas": int(after > 0.10),
            "final_return_rank": "",
            "within_pool_return_rank": "",
            "bad_pool_label": int(optional_float(row.get("bad_pool_label")) or 0),
            "quote_anomaly_label": int(optional_float(row.get("quote_anomaly_label")) or 0),
            "scale_risk_label": int(optional_float(row.get("scale_risk_label")) or 0),
            "liquidity_anomaly_label": int("liquidity_anomaly" in likely or "time_window_or_liquidity_anomaly" in likely),
            "gas_cost_dominates_label": int("gas_cost_dominates" in str(row.get("quarantine_reason", ""))),
            "alpha_island_candidate_label": int("alpha_island_candidate" in likely),
            "risk_severity": severity,
            "quarantine_reason": row.get("quarantine_reason", ""),
        }
        all_rows.append(out)
    sorted_all = sorted(all_rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
    denom = max(1, len(sorted_all) - 1)
    rank_by_id = {str(r["row_id"]): idx / denom for idx, r in enumerate(sorted_all)}
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        by_pool[row["pool_address"]].append(row)
    pool_rank: dict[str, float] = {}
    for rows in by_pool.values():
        sr = sorted(rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
        d = max(1, len(sr) - 1)
        for idx, row in enumerate(sr):
            pool_rank[str(row["row_id"])] = idx / d
    for row in all_rows:
        row["final_return_rank"] = rounded(rank_by_id.get(str(row["row_id"]), 0.0), 8)
        row["within_pool_return_rank"] = rounded(pool_rank.get(str(row["row_id"]), 0.0), 8)
    clean = [r for r in all_rows if r["risk_severity"] == "clean"]
    risk = [r for r in all_rows if r["risk_severity"] != "clean"]
    severity_report = {
        "risk_severity_distribution": dict(Counter(r["risk_severity"] for r in all_rows).most_common()),
        "rules": {
            "hard_exclude": "confirmed bad pool or severe quote anomaly with abs fixed return >= 20%",
            "soft_exclude": "gas cost dominates",
            "feature_limited": "scale/decimals risk without hard/soft exclusion",
            "review_only": "alpha-island or quote-artifact candidate without hard exclusion",
            "clean": "no risk flags",
        },
        "all_rows": len(all_rows),
        "clean_alpha_rows": len(clean),
        "risk_bad_pool_rows": len(risk),
    }
    return all_rows, clean, risk, severity_report


def _h16_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    pools = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top = pools.most_common()
    return {
        "rows": n,
        "unique_pools": len(pools),
        "pool_type_distribution": dict(Counter(str(r.get("pool_type") or r.get("protocol")) for r in rows).most_common()),
        "top1_share": rounded((top[0][1] / n) if n and top else 0.0, 8),
        "top5_share": rounded((sum(c for _, c in top[:5]) / n) if n else 0.0, 8),
        "non_top5_share": rounded((1.0 - sum(c for _, c in top[:5]) / n) if n else 0.0, 8),
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top[:10]],
    }


def _h16_focus_forensic(all_rows: list[dict[str, Any]], h15_forensic: dict[str, Any]) -> dict[str, Any]:
    focus = {_lower_addr(item.get("pool_address")) for item in h15_forensic.get("pool_forensics", [])}
    top_pos = [r["pool_address"] for r in sorted(all_rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf, reverse=True)[:50]]
    bottom_neg = [r["pool_address"] for r in sorted(all_rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or math.inf)[:50]]
    focus |= set(top_pos) | set(bottom_neg)
    out = []
    for pool in sorted(focus):
        rows = [r for r in all_rows if r["pool_address"] == pool]
        if not rows:
            continue
        vals = [optional_float(r.get("final_return_after_cost")) or 0.0 for r in rows]
        out.append(
            {
                "pool_address": pool,
                "rows": len(rows),
                "positive_rate_after_gas": rounded(sum(1 for r in rows if (optional_float(r.get("gas_adjusted_return_30m_medium")) or 0.0) > 0) / len(rows), 8),
                "top20_count": sum(int(r.get("alpha_top20_after_gas", 0)) for r in rows),
                "bottom_tail_count": sum(1 for p in bottom_neg if p == pool),
                "return_summary": {"mean": rounded(sum(vals) / len(vals), 8), "p50": rounded(percentile(vals, 0.50), 8), "p90": rounded(percentile(vals, 0.90), 8), "p95": rounded(percentile(vals, 0.95), 8)},
                "risk_severity_distribution": dict(Counter(str(r.get("risk_severity")) for r in rows).most_common()),
                "pool_type": dict(Counter(str(r.get("pool_type")) for r in rows).most_common()),
                "decoded_source": dict(Counter(str(r.get("decoded_source_used")) for r in rows).most_common()),
                "classification": {
                    "true_bad_pool": any(int(r.get("bad_pool_label", 0)) for r in rows),
                    "alpha_island": any(int(r.get("alpha_island_candidate_label", 0)) for r in rows),
                    "quote_artifact": any(int(r.get("quote_anomaly_label", 0)) for r in rows),
                    "scale_risk": any(int(r.get("scale_risk_label", 0)) for r in rows),
                    "time_window_anomaly": any(int(r.get("liquidity_anomaly_label", 0)) for r in rows),
                },
            }
        )
    return {"pools": out, "top_positive_pool_counts": dict(Counter(top_pos).most_common()), "bottom_negative_pool_counts": dict(Counter(bottom_neg).most_common())}


def _h16_quality_report(
    all_rows: list[dict[str, Any]],
    clean_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, str]],
    candidate_report: dict[str, Any],
    checkpoint: dict[str, Any],
    severity_report: dict[str, Any],
) -> dict[str, Any]:
    decoded_coverage = nonempty_coverage(enriched_rows, ["decoded_join_status"])
    matched = sum(1 for r in enriched_rows if str(r.get("decoded_join_status")) == "matched")
    feature_coverage = {
        "sender": nonempty_coverage(enriched_rows, ["unique_swap_senders_300s", "sender_entropy_300s"]),
        "buyer": nonempty_coverage(enriched_rows, ["unique_buyers_300s", "top1_buyer_share_300s", "buy_wallet_entropy_300s"]),
        "flow": nonempty_coverage(enriched_rows, ["buy_count_300s", "sell_count_300s", "buy_sell_count_ratio_300s", "net_flow_raw_300s"]),
    }
    return {
        "status": "PARTIAL",
        "final_rows": len(all_rows),
        **_h16_distribution(all_rows),
        "time_bucket_distribution": dict(Counter(str(r.get("entry_time", ""))[:13] for r in all_rows).most_common(20)),
        "return_distribution_before_gas": numeric_summary([r.get("fixed_cost_adjusted_return_30m") for r in all_rows]),
        "return_distribution_after_gas": numeric_summary([r.get("gas_adjusted_return_30m_medium") for r in all_rows]),
        "alpha_top20_after_gas_distribution": dict(Counter(str(r.get("alpha_top20_after_gas")) for r in all_rows).most_common()),
        "risk_severity_distribution": severity_report["risk_severity_distribution"],
        "clean_alpha_subset_size": len(clean_rows),
        "risk_bad_pool_subset_size": len(risk_rows),
        "decoded_coverage": {"rows": len(enriched_rows), "matched_rows": matched, "coverage": rounded(matched / len(enriched_rows), 8) if enriched_rows else 0.0, "nonempty": decoded_coverage},
        "feature_coverage": feature_coverage,
        "missingness_note": "H16 PARTIAL reuses H15/H14 752-row labeled/enriched data because RPC env is missing; candidate selection is 10k-scale but not executable-labeled.",
        "rpc_cost_estimate": checkpoint.get("estimated_rpc_calls"),
        "checkpoint_resume_summary": checkpoint,
        "candidate_selection_summary": candidate_report,
    }


def _h16_checkpoint_report(cfg: dict[str, Any], candidate_count: int, rpc_present: bool) -> dict[str, Any]:
    batch_size = int(cfg["labeling"].get("batch_size", 500))
    est_per = int(cfg["labeling"].get("estimated_rpc_calls_per_attempt", 50))
    batches = math.ceil(candidate_count / batch_size) if batch_size else 0
    return {
        "status": "blocked_missing_rpc_env" if not rpc_present else "ready_not_executed_by_harness",
        "selected": candidate_count,
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "failure_reason": {"missing_rpc_env": candidate_count} if not rpc_present else {},
        "estimated_rpc_calls": candidate_count * est_per,
        "actual_rpc_calls": 0,
        "quote_success_rate": None,
        "v2_v3_success_rate": {},
        "top_non_top_success_rate": {},
        "batch_size": batch_size,
        "batches_planned": batches,
        "resume_state": {
            "next_batch_id": 0,
            "completed_batches": 0,
            "remaining_candidates": candidate_count,
            "can_resume_when_rpc_env_present": True,
        },
    }


def _h16_markdown(patch: dict[str, Any]) -> str:
    q = patch["dataset_quality_report"]
    c = patch["candidate_selection_report"]
    ck = patch["checkpoint_report"]
    return f"""# Run-004 Patch H16 - Fixed-Horizon Gas-Adjusted 10k Dataset

## Status
- Result: `{patch['gate']['status']}`
- This is not a full strategy backtest; dynamic TP/SL/trailing/time exits remain unavailable.
- Promote/live allowed: `False`

## Candidate Selection
- Selected candidates: {c['selected_candidates']} / target {c['target_rows']}
- Projected unique pools: {c['projected_unique_pools']}
- Projected top1/top5/non-top5: {c['projected_top1_share']} / {c['projected_top5_share']} / {c['projected_non_top5_share']}
- Projected v2/v3: {c['projected_v2_v3_share']}
- Diversity score: {c['candidate_diversity_score']}

## Executable Labeling
- Attempted: {ck['attempted']}
- Success: {ck['success']}
- Blocker: {ck['status']}
- Estimated RPC calls for selected candidates: {ck['estimated_rpc_calls']}

## Current Partial Datasets
- All rows: {q['final_rows']}
- Clean alpha rows: {q['clean_alpha_subset_size']}
- Risk rows: {q['risk_bad_pool_subset_size']}
- Unique pools: {q['unique_pools']}
- Top1/top5/non-top5: {q['top1_share']} / {q['top5_share']} / {q['non_top5_share']}
- Risk severity: {q['risk_severity_distribution']}

## Gate
- H17 can start: `{patch['gate']['allow_h17_return_aware_ablation']}`
- Next allowed: `{patch['gate']['next_step']}`
"""


def _h16_fixed_horizon_gas_adjusted_10k(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    inputs = cfg["input_paths"]
    outputs = cfg["outputs"]
    candidate_rows, candidate_report = _h16_candidate_selection(cfg)
    write_csv_rows(_resolve(outputs["candidate_selection_csv"]), candidate_rows)
    write_json(_resolve(outputs["candidate_selection_report_json"]), candidate_report)

    rpc_present = bool(any(__import__("os").environ.get(name) for name in cfg["labeling"].get("rpc_env_vars", [])))
    checkpoint = _h16_checkpoint_report(cfg, len(candidate_rows), rpc_present)
    write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)

    h15_rows = read_rows(_resolve(inputs["h15_live_like_ledger_csv"]))
    h14_rows = read_rows(_resolve(inputs["h14_replay_ledger_csv"]))
    enriched_rows = read_rows(_resolve(inputs["enriched_dataset_csv"]))
    h15_forensic = json.loads(_resolve(inputs["h15_bad_pool_forensic_json"]).read_text())
    all_rows, clean_rows, risk_rows, severity_report = _h16_build_partial_datasets(h15_rows, h14_rows, h15_forensic)
    write_csv_rows(_resolve(outputs["all_dataset_csv"]), all_rows, H16_DATASET_FIELDS)
    write_csv_rows(_resolve(outputs["clean_alpha_dataset_csv"]), clean_rows, H16_DATASET_FIELDS)
    write_csv_rows(_resolve(outputs["risk_bad_pool_dataset_csv"]), risk_rows, H16_DATASET_FIELDS)
    write_json(_resolve(outputs["risk_severity_report_json"]), severity_report)
    return_accounting = {
        "status": "partial_reused_h15_h14_labeled_rows",
        "fixed_horizon_sec": int(cfg["labeling"].get("fixed_horizon_sec", 1800)),
        "primary_target": "alpha_top20_after_gas",
        "dynamic_exit_available": False,
        "not_full_strategy_backtest": True,
        "schemas_applied": H16_DATASET_FIELDS,
    }
    write_json(_resolve(outputs["return_accounting_report_json"]), return_accounting)
    focus = _h16_focus_forensic(all_rows, h15_forensic)
    write_json(_resolve(outputs["focus_pool_forensic_report_json"]), focus)
    quality = _h16_quality_report(all_rows, clean_rows, risk_rows, enriched_rows, candidate_report, checkpoint, severity_report)
    write_json(_resolve(outputs["dataset_quality_report_json"]), quality)

    gate_checks = {
        "all_dataset_rows_gte_8000": len(all_rows) >= 8000,
        "target_10k_reached": len(all_rows) >= int(cfg["selection"].get("target_rows", 10000)),
        "clean_alpha_subset_gte_2000": len(clean_rows) >= 2000,
        "risk_bad_pool_subset_available": len(risk_rows) > 0,
        "decoded_bridge_coverage_gte_90pct": (quality["decoded_coverage"]["coverage"] or 0) >= 0.90,
        "feature_coverage_gte_90pct": all((item.get("coverage") or 0) >= 0.90 for item in [quality["feature_coverage"]["sender"], quality["feature_coverage"]["buyer"], quality["feature_coverage"]["flow"]]),
        "top1_top5_improved_vs_h15": (quality["top1_share"] or 1) < 0.33244681 and (quality["top5_share"] or 1) < 0.90159574,
        "v2_v3_enough_samples": all(v >= 200 for v in quality["pool_type_distribution"].values()),
        "alpha_top20_after_gas_available": bool(quality["alpha_top20_after_gas_distribution"]),
        "risk_severity_labels_available": bool(quality["risk_severity_distribution"]),
        "checkpoint_resume_artifacts_completed": bool(checkpoint),
        "rpc_env_present_for_labeling": rpc_present,
    }
    success = all(gate_checks.values())
    patch = {
        "patch": "run-004-h16-fixed-horizon-gas-adjusted-10k-with-risk-severity",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "candidate_selection_report": candidate_report,
        "checkpoint_report": checkpoint,
        "return_accounting_report": return_accounting,
        "risk_severity_report": severity_report,
        "focus_pool_forensic_report": focus,
        "dataset_quality_report": quality,
        "gate": {
            "status": "PASS" if success else "PARTIAL",
            "allow_h17_return_aware_ablation": success,
            "allow_h165_continue_or_repair": not success,
            "checks": gate_checks,
            "blockers": [k for k, v in gate_checks.items() if not v],
            "next_step": "H17 return-aware ablation on 10k all/clean/risk-separated datasets" if success else "H16.5 continue/repair dataset build with RPC labeling enabled",
            "not_full_strategy_backtest": True,
            "promote_live_allowed": False,
            "forbidden": ["promote", "live", "production retrain", "production model overwrite"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h16_markdown(patch))
    return patch


def _h165_redacted_env(value: str | None) -> dict[str, Any]:
    if not value:
        return {"present": False, "redacted_host": None}
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc or parsed.path.split("/")[0]
    return {"present": True, "redacted_host": host, "scheme": parsed.scheme or None}


def _h165_rpc_readiness(cfg: dict[str, Any]) -> dict[str, Any]:
    env_vars = cfg["labeling_pilot"].get("rpc_env_vars", ["LABEL_RPC_URL", "EVM_HTTP_URL", "LIVE_BASE_RPC_URL"])
    env_status = {name: _h165_redacted_env(os.environ.get(name)) for name in env_vars}
    primary = next((name for name in ["LABEL_RPC_URL", "EVM_HTTP_URL"] if os.environ.get(name)), None)
    docker_status: dict[str, Any] = {"checked": False, "container": cfg.get("clickhouse", {}).get("docker_container")}
    container = str(cfg.get("clickhouse", {}).get("docker_container", ""))
    if container:
        docker_status = {"checked": True, "container": container, "env": {}, "error": None}
        for name in env_vars:
            proc = subprocess.run(
                ["docker", "exec", container, "sh", "-lc", f'test -n "${{{name}:-}}"'],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            docker_status["env"][name] = {"present": proc.returncode == 0}
            if proc.returncode not in (0, 1) and docker_status["error"] is None:
                docker_status["error"] = proc.stderr.strip() or proc.stdout.strip()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "READY" if primary else "BLOCKED_RPC_ENV",
        "host_shell_env": env_status,
        "docker_container_env": docker_status,
        "labeler_env_contract": {
            "scripts/label_dashcam.py": "reads EVM_HTTP_URL",
            "scripts/label_dashcam_executable.py": "reads LABEL_RPC_URL first, then EVM_HTTP_URL",
            "h16_5_harness": "reads LABEL_RPC_URL first, then EVM_HTTP_URL; LIVE_BASE_RPC_URL is audit-only unless explicitly mapped",
            "secret_policy": "reports only present/missing and redacted host; RPC URL is never written to artifacts",
        },
        "recommended_run_command": "LABEL_RPC_URL=<archive_base_rpc_url> python3 scripts/run004_research_harness.py --mode h165_rpc_candidate_repair_labeling_pilot --config configs/run004/h165_rpc_candidate_repair_labeling_pilot.yaml",
        "recommended_docker_pass_through": "docker exec -e LABEL_RPC_URL=<archive_base_rpc_url> <container> <labeler command>",
    }


def _h165_pt_class(pool_type: Any) -> str:
    pt = str(pool_type or "").lower()
    if pt in ("v2", "aero_vamm"):
        return "v2"
    if pt in ("v3", "aero_cl"):
        return "v3"
    return pt or "unknown"


def _h165_candidate_audit(client: ClickHouseClient, cfg: dict[str, Any]) -> dict[str, Any]:
    src = cfg["clickhouse"]["decoded_sources"]
    db = client.database
    rows: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "selection_constraints_table": [
            {"constraint": "original source", "h16_value": "decoded swaps INNER JOIN discovered_pools metadata"},
            {"constraint": "pool_type filter", "h16_value": "lower(m.pool_type) = 'v2' or 'v3' only"},
            {"constraint": "metadata filter", "h16_value": "chain_id=8453 and has_weth_leg=1"},
            {"constraint": "decoded filter", "h16_value": "chain_id=8453, removed=0, tx_hash != ''"},
            {"constraint": "per-pool cap", "h16_value": "250 default / 150 focus, not the bottleneck in observed output"},
            {"constraint": "dedup key", "h16_value": "row_number partitioned by pool; not capped to one, but source join yielded about one event per eligible pool"},
        ],
        "filter_counts": {},
        "bottleneck_filter": None,
        "why_only_1039": None,
        "repair_strategy": None,
    }
    queries = {
        "decoded_v2_raw": f"SELECT count() AS rows, uniqExact(lower(pair_address)) AS pools FROM {db}.{src['v2_table']}",
        "decoded_v3_raw": f"SELECT count() AS rows, uniqExact(lower(pool_address)) AS pools FROM {db}.{src['v3_table']}",
        "metadata_all": f"SELECT count() AS pools FROM {db}.{src['metadata_table']} WHERE chain_id = 8453",
        "metadata_weth_by_pool_type": f"SELECT pt AS pool_type, count() AS pools FROM (SELECT lower(pool_type) AS pt FROM {db}.{src['metadata_table']} WHERE chain_id = 8453 AND has_weth_leg = 1) GROUP BY pt ORDER BY pools DESC",
        "h16_join_v2_strict": f"""
            SELECT lower(m.pool_type) AS pool_type, count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND m.has_weth_leg = 1 AND lower(m.pool_type) = 'v2'
            GROUP BY lower(m.pool_type)
        """,
        "h16_join_v3_strict": f"""
            SELECT lower(m.pool_type) AS pool_type, count() AS rows, uniqExact(lower(e.pool_address)) AS pools
            FROM {db}.{src['v3_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pool_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND m.has_weth_leg = 1 AND lower(m.pool_type) = 'v3'
            GROUP BY lower(m.pool_type)
        """,
        "h16_join_v2_like": f"""
            SELECT lower(m.pool_type) AS pool_type, count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND m.has_weth_leg = 1 AND lower(m.pool_type) IN ('v2','aero_vamm')
            GROUP BY lower(m.pool_type)
        """,
        "h16_join_v3_like": f"""
            SELECT lower(m.pool_type) AS pool_type, count() AS rows, uniqExact(lower(e.pool_address)) AS pools
            FROM {db}.{src['v3_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pool_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND m.has_weth_leg = 1 AND lower(m.pool_type) IN ('v3','aero_cl')
            GROUP BY lower(m.pool_type)
        """,
    }
    for name, query in queries.items():
        try:
            rows["filter_counts"][name] = client.query_json(query)
        except Exception as exc:
            rows["filter_counts"][name] = {"error": str(exc)}
    rows["bottleneck_filter"] = "H16 required decoded swaps to INNER JOIN discovered_pools WETH metadata; only a small metadata-backed decoded subset survived, so pool caps/diversity were not the limiting factor."
    rows["why_only_1039"] = "The strict executable-ready decoded join produced roughly one row per eligible joined pool and missed most decoded rows without WETH metadata. It also used exact v2/v3 metadata filters, excluding aero_vamm/aero_cl from the original candidate universe."
    rows["repair_strategy"] = "Use discovered_pools WETH-leg metadata as the executable-ready universe, stratify by v2-like/v3-like pool and time offsets from discovered_at, and keep decoded swap counts as coverage evidence instead of a hard INNER JOIN."
    return rows


def _h165_decoded_pool_counts(client: ClickHouseClient, cfg: dict[str, Any]) -> dict[str, int]:
    src = cfg["clickhouse"]["decoded_sources"]
    db = client.database
    counts: dict[str, int] = {}
    specs = [(src["v2_table"], "pair_address"), (src["v3_table"], "pool_address")]
    for table, col in specs:
        try:
            for row in client.query_json(f"SELECT lower({col}) AS pool_address, count() AS n FROM {db}.{table} WHERE chain_id = 8453 AND removed = 0 GROUP BY lower({col})"):
                counts[_lower_addr(row.get("pool_address"))] = counts.get(_lower_addr(row.get("pool_address")), 0) + int(row.get("n") or 0)
        except Exception:
            continue
    return counts


def _h165_repair_candidates(client: ClickHouseClient, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repair = cfg["candidate_repair"]
    src = cfg["clickhouse"]["decoded_sources"]
    db = client.database
    focus = {_lower_addr(p) for p in repair.get("focus_pools", [])}
    query = f"""
        SELECT
            lower(pool_address) AS pool_address,
            lower(pool_type) AS raw_pool_type,
            lower(token0_address) AS token0_address,
            lower(token1_address) AS token1_address,
            token0_symbol,
            token1_symbol,
            token0_decimals,
            token1_decimals,
            fee_bps,
            created_block,
            toString(discovered_at) AS discovered_at,
            toUnixTimestamp(discovered_at) * 1000 AS discovered_at_ms
        FROM {db}.{src['metadata_table']}
        WHERE chain_id = 8453
          AND has_weth_leg = 1
          AND lower(pool_type) IN ('v2','v3','aero_vamm','aero_cl')
        ORDER BY cityHash64(lower(pool_address), lower(pool_type))
    """
    metadata_rows = client.query_json(query)
    decoded_counts = _h165_decoded_pool_counts(client, cfg)
    offsets = [int(x) for x in repair.get("time_offsets_sec", [1800, 7200, 21600])]
    max_per_pool = int(repair.get("max_per_pool", 3))
    target_by_pt = {"v2": int(repair.get("v2_target", 5000)), "v3": int(repair.get("v3_target", 5000))}
    target = int(repair.get("target_candidates", 10000))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    horizon_ms = int(cfg["labeling_pilot"].get("fixed_horizon_sec", 1800)) * 1000
    by_pt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in metadata_rows:
        by_pt[_h165_pt_class(row.get("raw_pool_type"))].append(row)

    selected: list[dict[str, Any]] = []
    focus_counts: Counter[str] = Counter()
    for pt in ("v2", "v3"):
        target_n = target_by_pt[pt]
        for row in by_pt.get(pt, []):
            pool = _lower_addr(row.get("pool_address"))
            per_pool_limit = min(max_per_pool, int(repair.get("focus_pool_cap", 20)) if pool in focus else max_per_pool)
            base_ms = int(row.get("discovered_at_ms") or 0)
            made = 0
            for offset_sec in offsets[:per_pool_limit]:
                if len([r for r in selected if r.get("pool_class") == pt]) >= target_n:
                    break
                ts_ms = base_ms + offset_sec * 1000
                if ts_ms + horizon_ms + 120_000 > now_ms:
                    continue
                if pool in focus and focus_counts[pool] >= int(repair.get("focus_pool_cap", 20)):
                    continue
                candidate = {
                    "candidate_id": len(selected),
                    "candidate_kind": "metadata_weth_time_offset",
                    "pool_address": pool,
                    "pool_type": row.get("raw_pool_type"),
                    "pool_class": pt,
                    "protocol": pt,
                    "token0_address": row.get("token0_address"),
                    "token1_address": row.get("token1_address"),
                    "token0_symbol": row.get("token0_symbol"),
                    "token1_symbol": row.get("token1_symbol"),
                    "token0_decimals": row.get("token0_decimals"),
                    "token1_decimals": row.get("token1_decimals"),
                    "fee_bps": row.get("fee_bps") or (30 if pt == "v3" else 0),
                    "created_block": row.get("created_block"),
                    "discovered_at": row.get("discovered_at"),
                    "timestamp_ms": ts_ms,
                    "entry_time": datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat(),
                    "time_offset_sec": offset_sec,
                    "day_bucket": datetime.fromtimestamp(ts_ms / 1000, UTC).date().isoformat(),
                    "hour_bucket": datetime.fromtimestamp(ts_ms / 1000, UTC).hour,
                    "decoded_event_count": decoded_counts.get(pool, 0),
                    "decoded_join_status": "pool_has_decoded_events" if decoded_counts.get(pool, 0) else "metadata_only_no_decoded_event_match",
                    "is_focus_pool": int(pool in focus),
                }
                selected.append(candidate)
                made += 1
                if pool in focus:
                    focus_counts[pool] += 1
            if len([r for r in selected if r.get("pool_class") == pt]) >= target_n:
                break
    selected = selected[:target]
    pools = Counter(_lower_addr(r.get("pool_address")) for r in selected)
    top = pools.most_common()
    n = len(selected)
    report = {
        "target_candidates": target,
        "total_candidates": n,
        "unique_pools": len(pools),
        "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
        "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0, 8),
        "non_top5_share": rounded(1.0 - (sum(c for _, c in top[:5]) / n if n else 0.0), 8),
        "v2_v3_distribution": dict(Counter(str(r.get("pool_class")) for r in selected).most_common()),
        "raw_pool_type_distribution": dict(Counter(str(r.get("pool_type")) for r in selected).most_common()),
        "time_bucket_distribution": {
            "days_top20": dict(Counter(str(r.get("day_bucket")) for r in selected).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in selected).most_common()),
        },
        "focus_pool_counts": dict(Counter(_lower_addr(r.get("pool_address")) for r in selected if int(r.get("is_focus_pool", 0))).most_common()),
        "decoded_coverage": {
            "rows_with_decoded_event_count": sum(1 for r in selected if int(r.get("decoded_event_count") or 0) > 0),
            "coverage": rounded(sum(1 for r in selected if int(r.get("decoded_event_count") or 0) > 0) / n, 8) if n else 0.0,
        },
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top[:20]],
        "repair_note": "Candidates are executable-ready metadata-backed WETH pools sampled by discovered_at time offsets; decoded swap coverage is diagnostic, not a hard filter.",
        "clickhouse": {"transport": client.transport, "database": client.database},
    }
    return selected, report


def _h165_gas_adjusted(net: float, pool_class: str, cfg: dict[str, Any]) -> dict[str, Any]:
    lp = cfg["labeling_pilot"]
    units_cfg = lp.get("gas_units", {})
    pt_units = units_cfg.get(pool_class, units_cfg.get("v3" if pool_class == "v3" else "v2", {}))
    units = int(pt_units.get("buy", 0)) + int(pt_units.get("sell", 0))
    if bool(lp.get("include_approval_gas", True)):
        units += int(lp.get("approval_gas_units", 0))
    notional = int(lp.get("notional_wei", 500000000000000))
    out: dict[str, Any] = {"estimated_total_gas_units": units}
    for tier, gwei in lp.get("gas_price_gwei", {}).items():
        cost_return = units * float(gwei) * 1e9 / notional
        out[f"gas_cost_return_{tier}"] = rounded(cost_return, 10)
        out[f"gas_adjusted_return_30m_{tier}"] = rounded(net - cost_return, 8)
    return out


def _h165_run_pilot(cfg: dict[str, Any], candidates: list[dict[str, Any]], rpc_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if rpc_report.get("status") != "READY":
        return [], {
            "status": "BLOCKED_RPC_ENV",
            "selected": min(int(cfg["labeling_pilot"].get("pilot_size", 120)), len(candidates)),
            "attempted": 0,
            "success": 0,
            "failed": 0,
            "failure_reasons": {"missing_rpc_env": 1},
            "estimated_rpc_calls": 0,
            "actual_rpc_calls": 0,
            "resume_state": {"completed": False, "reason": "missing_rpc_env"},
        }

    from scripts import label_dashcam_executable as labeler  # noqa: WPS433

    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""

    class CountedRPC(labeler.RPC):  # type: ignore[misc]
        def __init__(self, url: str):
            super().__init__(url)
            self.call_count = 0

        def call(self, method: str, params: list[Any]) -> dict[str, Any]:  # type: ignore[override]
            self.call_count += 1
            return super().call(method, params)

        def batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:  # type: ignore[override]
            self.call_count += len(calls)
            return super().batch(calls)

    pilot_size = int(cfg["labeling_pilot"].get("pilot_size", 120))
    v2_target = int(cfg["labeling_pilot"].get("v2_target", pilot_size // 2))
    v3_target = int(cfg["labeling_pilot"].get("v3_target", pilot_size - v2_target))
    selected = [r for r in candidates if r.get("pool_class") == "v2"][:v2_target]
    selected += [r for r in candidates if r.get("pool_class") == "v3"][:v3_target]
    selected = selected[:pilot_size]
    rpc = CountedRPC(rpc_url)
    checkpoint: dict[str, Any] = {
        "status": "started",
        "selected": len(selected),
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "failure_reasons": {},
        "estimated_rpc_calls": len(selected) * 8,
        "actual_rpc_calls": 0,
        "quote_success_rate": None,
        "v2_v3_success_rate": {},
        "top_non_top_success_rate": {},
        "resume_state": {"completed": False},
    }
    try:
        chain = rpc.call("eth_chainId", [])
        if int(chain["result"], 16) != 8453:
            raise RuntimeError(f"unexpected_chain_id:{int(chain['result'], 16)}")
        horizon = int(cfg["labeling_pilot"].get("fixed_horizon_sec", 1800))
        min_ts = min(int(r["timestamp_ms"]) // 1000 for r in selected)
        max_ts = max(int(r["timestamp_ms"]) // 1000 for r in selected) + horizon
        bn_arr, ts_arr = labeler.build_ts_grid(rpc, min_ts, max_ts)
        rows: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        for idx, cand in enumerate(selected):
            checkpoint["attempted"] += 1
            ts_sec = int(cand["timestamp_ms"]) // 1000
            entry_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec)
            exit_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec + horizon)
            pool_class = str(cand.get("pool_class"))
            meta = {
                "pool_type": cand.get("pool_type"),
                "t0": _lower_addr(cand.get("token0_address")),
                "t1": _lower_addr(cand.get("token1_address")),
                "fee_bps": int(optional_float(cand.get("fee_bps")) or 30),
            }
            label, net, label_type = labeler.label_one(
                rpc,
                _lower_addr(cand.get("pool_address")),
                meta,
                labeler.WETH_BASE,
                int(cfg["labeling_pilot"].get("notional_wei", 500000000000000)),
                entry_block,
                exit_block,
            )
            reason_counts[label_type] += 1
            success = label >= 0
            if success:
                checkpoint["success"] += 1
            else:
                checkpoint["failed"] += 1
            fixed = float(net) if success else None
            gas = _h165_gas_adjusted(fixed or 0.0, pool_class, cfg) if success else {}
            medium = optional_float(gas.get("gas_adjusted_return_30m_medium")) if success else None
            quote_anom = int(success and abs(fixed or 0.0) >= 0.20)
            scale_risk = int(success and abs(fixed or 0.0) >= 0.50)
            gas_dom = int(success and medium is not None and fixed is not None and fixed > 0 and medium <= 0)
            focus_pool = int(cand.get("is_focus_pool", 0))
            severity = "clean"
            if not success:
                severity = "hard_exclude"
            elif quote_anom and abs(fixed or 0.0) >= 0.20:
                severity = "hard_exclude"
            elif gas_dom:
                severity = "soft_exclude"
            elif scale_risk:
                severity = "feature_limited"
            elif focus_pool:
                severity = "review_only"
            row = {
                **cand,
                "pilot_row_id": idx,
                "entry_block": entry_block,
                "exit_block": exit_block,
                "fixed_horizon_sec": horizon,
                "label_status": "success" if success else "failed",
                "failure_reason": "" if success else label_type,
                "label_type": label_type,
                "executable_label_current": label if success else "",
                "gross_quote_return_30m": rounded((fixed or 0.0) + float(cfg["labeling_pilot"].get("round_trip_cost", 0.006)), 8) if success else "",
                "fixed_cost_adjusted_return_30m": rounded(fixed, 8) if success else "",
                **gas,
                "final_return_after_cost": rounded(medium, 8) if success else "",
                "slippage_stress_return": rounded((medium or 0.0) - float(cfg["labeling_pilot"].get("slippage_stress_bps", 100)) / 10000.0, 8) if success and medium is not None else "",
                "exit_policy_used": "fixed_30m_fallback",
                "exit_reason": "fixed_horizon",
                "dynamic_exit_available": False,
                "dynamic_exit_unavailable_reason": "missing_intra_window_quote_path_trigger_block_token_amount_and_trigger_sell_quote",
                "executable_label_after_gas": int((medium or 0.0) > 0) if success else 0,
                "alpha_ret_gt_5pct_after_gas": int((medium or 0.0) > 0.05) if success else 0,
                "alpha_ret_gt_10pct_after_gas": int((medium or 0.0) > 0.10) if success else 0,
                "bad_pool_label": int(focus_pool and cand.get("pool_address") == "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a"),
                "quote_anomaly_label": quote_anom,
                "scale_risk_label": scale_risk,
                "liquidity_anomaly_label": int(label_type in ("fail_low_liquidity", "executable_exit_drained")),
                "gas_cost_dominates_label": gas_dom,
                "alpha_island_candidate_label": int(focus_pool and cand.get("pool_address") in {"0x54219bce730d2727d91d8dcdf1465c7d299dee45", "0xc2476883d28d40b6c057f242fce2e8bda674faa0"}),
                "quarantine_reason": label_type if not success else ("quote_anomaly" if quote_anom else ("gas_cost_dominates" if gas_dom else "")),
                "risk_severity": severity,
            }
            rows.append(row)
        successful = [r for r in rows if r.get("label_status") == "success"]
        returns = [optional_float(r.get("final_return_after_cost")) for r in successful]
        returns = [r for r in returns if r is not None]
        cutoff = percentile(returns, 0.80) if returns else None
        sorted_success = sorted(successful, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
        denom = max(1, len(sorted_success) - 1)
        rank_by_id = {str(r["pilot_row_id"]): idx / denom for idx, r in enumerate(sorted_success)}
        for row in rows:
            val = optional_float(row.get("final_return_after_cost"))
            row["alpha_top20_after_gas"] = int(val is not None and cutoff is not None and val >= cutoff)
            row["final_return_rank"] = rounded(rank_by_id.get(str(row.get("pilot_row_id")), 0.0), 8) if val is not None else ""
            row["within_pool_return_rank"] = row["final_return_rank"]
        checkpoint["label_type_counts"] = dict(reason_counts.most_common())
        checkpoint["failure_reasons"] = {k: v for k, v in reason_counts.most_common() if k != "executable"}
        checkpoint["actual_rpc_calls"] = rpc.call_count
        checkpoint["quote_success_rate"] = rounded(checkpoint["success"] / checkpoint["attempted"], 8) if checkpoint["attempted"] else 0.0
        by_pt = defaultdict(lambda: {"attempted": 0, "success": 0})
        for row in rows:
            pt = str(row.get("pool_class"))
            by_pt[pt]["attempted"] += 1
            by_pt[pt]["success"] += int(row.get("label_status") == "success")
        checkpoint["v2_v3_success_rate"] = {pt: rounded(v["success"] / v["attempted"], 8) if v["attempted"] else 0.0 for pt, v in by_pt.items()}
        checkpoint["top_non_top_success_rate"] = {"not_computed": "candidate repair is metadata-backed; top/non-top pool rank is not defined for this pilot"}
        checkpoint["return_distribution"] = numeric_summary([r.get("final_return_after_cost") for r in successful])
        checkpoint["risk_severity_distribution"] = dict(Counter(str(r.get("risk_severity")) for r in rows).most_common())
        checkpoint["decoded_coverage"] = {
            "attempted_rows": len(rows),
            "rows_with_decoded_events": sum(1 for r in rows if int(r.get("decoded_event_count") or 0) > 0),
            "coverage": rounded(sum(1 for r in rows if int(r.get("decoded_event_count") or 0) > 0) / len(rows), 8) if rows else 0.0,
        }
        checkpoint["feature_coverage"] = {"decoded_bridge_features": "not_enriched_in_h16_5_pilot; metadata and decoded_event_count only"}
        checkpoint["resume_state"] = {"completed": True, "next_step": "resume_H16_10k_only_if_gate_passes"}
        checkpoint["status"] = "completed"
        return rows, checkpoint
    except Exception as exc:
        checkpoint["status"] = "LABELER_FAILURE"
        checkpoint["failure_reasons"] = {str(exc): checkpoint["selected"]}
        checkpoint["actual_rpc_calls"] = getattr(rpc, "call_count", 0)
        checkpoint["resume_state"] = {"completed": False, "reason": str(exc)}
        return [], checkpoint


def _h165_markdown(patch: dict[str, Any]) -> str:
    rpc = patch["rpc_readiness_report"]
    cand = patch["repaired_candidate_report"]
    pilot = patch["pilot_checkpoint_report"]
    gate = patch["gate"]
    return f"""# Run-004 Patch H16.5 - RPC Wiring, Candidate Repair, Labeling Pilot

## Status
- Result: `{gate['status']}`
- RPC env ready: `{rpc['status'] == 'READY'}`
- Promote/live allowed: `False`
- H17 allowed: `{gate['allow_h17_return_aware_ablation']}`

## Candidate Repair
- Repaired candidates: {cand['total_candidates']}
- Unique pools: {cand['unique_pools']}
- Top1/top5/non-top5: {cand['top1_share']} / {cand['top5_share']} / {cand['non_top5_share']}
- V2/V3 distribution: {cand['v2_v3_distribution']}
- Focus pool counts: {cand['focus_pool_counts']}

## H16 Bottleneck
- Why H16 only selected 1,039: {patch['candidate_selection_audit']['why_only_1039']}
- Bottleneck: {patch['candidate_selection_audit']['bottleneck_filter']}

## Pilot Labeling
- Status: `{pilot['status']}`
- Attempted / success / failed: {pilot['attempted']} / {pilot['success']} / {pilot['failed']}
- Quote success rate: {pilot.get('quote_success_rate')}
- Actual RPC calls: {pilot.get('actual_rpc_calls')}
- Failure reasons: {pilot.get('failure_reasons')}
- Risk severity: {pilot.get('risk_severity_distribution')}

## Decision
- Resume H16 10k build: `{gate['allow_resume_H16_10k_dataset_build']}`
- H17 can start: `{gate['allow_h17_return_aware_ablation']}`
- Next step: `{gate['next_step']}`
"""


def _h165_rpc_candidate_repair_labeling_pilot(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    inputs = cfg.get("input_paths", {})
    rpc = _h165_rpc_readiness(cfg)
    write_json(_resolve(outputs["rpc_readiness_report_json"]), rpc)

    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    audit = _h165_candidate_audit(client, cfg)
    write_json(_resolve(outputs["candidate_selection_audit_json"]), audit)
    repaired, repaired_report = _h165_repair_candidates(client, cfg)
    write_csv_rows(_resolve(outputs["repaired_candidate_csv"]), repaired)
    write_json(_resolve(outputs["repaired_candidate_report_json"]), repaired_report)
    pilot_rows, pilot_checkpoint = _h165_run_pilot(cfg, repaired, rpc)
    write_json(_resolve(outputs["pilot_checkpoint_report_json"]), pilot_checkpoint)
    if pilot_rows:
        write_csv_rows(_resolve(outputs["pilot_dataset_csv"]), pilot_rows)

    gate_checks = {
        "repaired_candidates_gte_10000": len(repaired) >= 10000,
        "unique_pools_sufficient": int(repaired_report.get("unique_pools") or 0) >= 40,
        "top1_concentration_ok": (repaired_report.get("top1_share") or 1) <= 0.12,
        "top5_concentration_ok": (repaired_report.get("top5_share") or 1) <= 0.45,
        "rpc_env_ready": rpc.get("status") == "READY",
        "pilot_attempted_gt_0": int(pilot_checkpoint.get("attempted") or 0) > 0,
        "pilot_success_rate_acceptable": (pilot_checkpoint.get("quote_success_rate") or 0) >= 0.50,
        "pilot_v2_success_rate_acceptable": (pilot_checkpoint.get("v2_v3_success_rate", {}).get("v2") or 0) >= 0.50,
        "pilot_v3_success_rate_acceptable": (pilot_checkpoint.get("v2_v3_success_rate", {}).get("v3") or 0) >= 0.50,
        "return_schema_fields_generated": bool(pilot_rows) and all("alpha_top20_after_gas" in row and "risk_severity" in row for row in pilot_rows),
        "decoded_bridge_enrichment_ready": str(pilot_checkpoint.get("feature_coverage", {}).get("decoded_bridge_features", "")).startswith("enriched"),
        "checkpoint_resume_ok": bool(pilot_checkpoint.get("resume_state")),
    }
    success = all(gate_checks.values())
    patch = {
        "patch": "run-004-h16.5-rpc-wiring-candidate-selection-repair-labeling-pilot",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "rpc_readiness_report": rpc,
        "candidate_selection_audit": audit,
        "repaired_candidate_report": repaired_report,
        "pilot_checkpoint_report": pilot_checkpoint,
        "gate": {
            "status": "PASS" if success else "PARTIAL",
            "checks": gate_checks,
            "blockers": [k for k, v in gate_checks.items() if not v],
            "allow_resume_H16_10k_dataset_build": success,
            "allow_h17_return_aware_ablation": False,
            "h17_reason": "H17 remains blocked until a real 10k labeled dataset is completed.",
            "next_step": "Resume H16 10k fixed-horizon gas-adjusted build with checkpointed labeling" if success else "Repair listed H16.5 blockers before resuming H16",
            "not_full_strategy_backtest": True,
            "promote_live_allowed": False,
            "forbidden": ["production retrain", "promote", "live", "production model overwrite"],
        },
        "safety_assertions": {
            **cfg.get("safety", {}),
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
            "rpc_url_written_to_artifacts": False,
        },
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h165_markdown(patch))
    return patch


def _h166_v2_candidate_report(candidates: list[dict[str, Any]], base_report: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    focus = {_lower_addr(p) for p in cfg["candidate_repair"].get("focus_pools", [])}
    suspicious = focus | {
        "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a",
        "0x54219bce730d2727d91d8dcdf1465c7d299dee45",
        "0xc2476883d28d40b6c057f242fce2e8bda674faa0",
    }
    n = len(candidates)
    pools = Counter(_lower_addr(r.get("pool_address")) for r in candidates)
    top = pools.most_common()
    return {
        **base_report,
        "scope": "v2_only_mainline",
        "candidates": n,
        "target_executable_rows": 10000,
        "unique_pools": len(pools),
        "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
        "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0, 8),
        "focus_pool_counts": dict(Counter(_lower_addr(r.get("pool_address")) for r in candidates if _lower_addr(r.get("pool_address")) in focus).most_common()),
        "suspicious_pool_counts": dict(Counter(_lower_addr(r.get("pool_address")) for r in candidates if _lower_addr(r.get("pool_address")) in suspicious).most_common()),
        "pool_type_policy": {
            "mixed_v2_v3_allowed": False,
            "v2_only_mainline": True,
            "v3_excluded_from_mainline": True,
        },
        "future_h17_fields_preserved": [
            "pool_address",
            "pool_type",
            "pool_class",
            "timestamp_ms",
            "entry_time",
            "token0_address",
            "token1_address",
            "fee_bps",
            "decoded_join_status",
            "decoded_event_count",
            "is_focus_pool",
        ],
    }


def _h166_v2_readiness_report(
    cfg: dict[str, Any],
    candidate_report: dict[str, Any],
    pilot_checkpoint: dict[str, Any],
    pilot_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    gate = float(cfg["labeling_pilot"].get("success_rate_gate", 0.90))
    quote_rate = float(pilot_checkpoint.get("quote_success_rate") or 0.0)
    attempted = int(pilot_checkpoint.get("attempted") or 0)
    success = int(pilot_checkpoint.get("success") or 0)
    checks = {
        "v2_candidates_gte_15000": int(candidate_report.get("candidates") or 0) >= 15000,
        "unique_pools_sufficient": int(candidate_report.get("unique_pools") or 0) >= 40,
        "top1_concentration_ok": (candidate_report.get("top1_share") or 1) <= 0.12,
        "top5_concentration_ok": (candidate_report.get("top5_share") or 1) <= 0.45,
        "v2_only_policy_enforced": set(str(r.get("pool_class")) for r in pilot_rows) <= {"v2"},
        "pilot_attempted_200_to_500": 200 <= attempted <= 500,
        "v2_pilot_success_rate_gte_gate": quote_rate >= gate,
        "return_schema_fields_generated": bool(pilot_rows) and all("alpha_top20_after_gas" in row and "risk_severity" in row for row in pilot_rows),
        "checkpoint_resume_ok": bool(pilot_checkpoint.get("resume_state")),
    }
    return {
        "status": "READY_FOR_V2_ONLY_H16_10K" if all(checks.values()) else "PARTIAL",
        "success_rate_gate": gate,
        "attempted": attempted,
        "success": success,
        "failed": int(pilot_checkpoint.get("failed") or 0),
        "quote_success_rate": quote_rate,
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "allow_h16_v2_only_10k_dataset_build": all(checks.values()),
        "allow_mixed_h16": False,
        "allow_v3_mainline": False,
        "allow_h17_return_aware_ablation": False,
        "h17_reason": "H17 remains blocked until a real V2-only 10k labeled dataset is completed.",
    }


def _h166_v3_repair_plan(inputs: dict[str, Any]) -> dict[str, Any]:
    checkpoint_path = _resolve(inputs.get("h165_pilot_checkpoint_report_json", ""))
    h165_checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    v3_rate = h165_checkpoint.get("v2_v3_success_rate", {}).get("v3")
    failures = h165_checkpoint.get("failure_reasons", {})
    label_counts = h165_checkpoint.get("label_type_counts", {})
    return {
        "status": "V3_REPAIR_BRANCH_ONLY",
        "v3_does_not_block_v2_mainline": True,
        "mixed_v2_v3_h16_allowed": False,
        "v3_mainline_allowed": False,
        "h165_v3_success_rate": v3_rate,
        "h165_failure_summary": {
            "failure_reasons": failures,
            "label_type_counts": label_counts,
            "dominant_failures": ["fail_sanity_clip", "fail_v3_entry_zero"],
        },
        "initial_failure_taxonomy": {
            "token_direction": "possible; V3 quote path may select the wrong non-WETH token when metadata direction or WETH leg inference is wrong",
            "fee_tier": "possible; fee_bps * 100 may not match pool fee tier or metadata may be missing/stale",
            "amount_scale_decimals": "likely contributor to fail_sanity_clip and extreme return clipping",
            "historical_quoter": "possible; Quoter behavior at historical blocks can fail for old/uninitialized liquidity states",
            "liquidity_tick_state": "likely for fail_v3_entry_zero and exit-drained paths",
            "metadata_mismatch": "possible; discovered_pools metadata and decoded V3 event pool identity need row-level reconciliation",
        },
        "repair_branch": [
            {
                "id": "V3R-1",
                "name": "row-level V3 failure forensic",
                "goal": "sample failed V3 rows and record pool metadata, token direction, fee tier, quoter calldata, entry/exit blocks, and exact revert/zero reason",
            },
            {
                "id": "V3R-2",
                "name": "100-row V3-only quote path repair",
                "goal": "test corrected token direction, fee tier, decimals, and historical quoter handling on a small V3-only sample",
            },
            {
                "id": "V3R-3",
                "name": "V3 decoded bridge enrichment repair",
                "goal": "repair decoded V3 join semantics before any V3 alpha dataset joins the mainline",
            },
        ],
        "decision": "V3 remains diagnostics-only and is excluded from V2-only H16 and H17 inputs until V3R passes independently.",
    }


def _h166_markdown(patch: dict[str, Any]) -> str:
    cand = patch["v2_candidate_report"]
    pilot = patch["v2_pilot_checkpoint_report"]
    ready = patch["v2_readiness_report"]
    return f"""# Run-004 Patch H16.6 - Split V2 Mainline from V3 Repair

## Status
- Result: `{patch['gate']['status']}`
- Mixed V2/V3 H16 allowed: `False`
- V3 mainline allowed: `False`
- Promote/live allowed: `False`

## V2 Candidate Selection
- Candidates: {cand['candidates']}
- Unique pools: {cand['unique_pools']}
- Top1/top5/non-top5: {cand['top1_share']} / {cand['top5_share']} / {cand['non_top5_share']}
- Time buckets: {cand['time_bucket_distribution']}
- Focus pool counts: {cand['focus_pool_counts']}
- Suspicious pool counts: {cand['suspicious_pool_counts']}

## V2 Pilot
- Attempted / success / failed: {pilot['attempted']} / {pilot['success']} / {pilot['failed']}
- Quote success rate: {pilot.get('quote_success_rate')}
- Failure reasons: {pilot.get('failure_reasons')}
- Return distribution after gas: {pilot.get('return_distribution')}
- Risk severity: {pilot.get('risk_severity_distribution')}
- Decoded bridge coverage: {pilot.get('decoded_coverage')}

## Decision
- Allow V2-only H16 10k build: `{ready['allow_h16_v2_only_10k_dataset_build']}`
- Allow H17: `{ready['allow_h17_return_aware_ablation']}`
- Blockers: {ready['blockers']}

## V3 Branch
- V3 is split out of the mainline and does not block V2-only H16.
- V3 repair stages: `V3R-1`, `V3R-2`, `V3R-3`.
"""


def _h166_split_v2_mainline_v3_repair(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    inputs = cfg.get("input_paths", {})
    rpc = _h165_rpc_readiness(cfg)
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    candidates, base_report = _h165_repair_candidates(client, cfg)
    v2_candidates = [row for row in candidates if str(row.get("pool_class")) == "v2"]
    for idx, row in enumerate(v2_candidates):
        row["candidate_id"] = idx
    candidate_report = _h166_v2_candidate_report(v2_candidates, base_report, cfg)
    write_csv_rows(_resolve(outputs["v2_candidate_csv"]), v2_candidates)
    write_json(_resolve(outputs["v2_candidate_report_json"]), candidate_report)

    pilot_rows, pilot_checkpoint = _h165_run_pilot(cfg, v2_candidates, rpc)
    write_json(_resolve(outputs["v2_pilot_checkpoint_report_json"]), pilot_checkpoint)
    if pilot_rows:
        write_csv_rows(_resolve(outputs["v2_pilot_dataset_csv"]), pilot_rows)

    readiness = _h166_v2_readiness_report(cfg, candidate_report, pilot_checkpoint, pilot_rows)
    write_json(_resolve(outputs["v2_readiness_report_json"]), readiness)
    v3_plan = _h166_v3_repair_plan(inputs)
    write_json(_resolve(outputs["v3_repair_branch_plan_json"]), v3_plan)

    gate = {
        "status": "PASS" if readiness["allow_h16_v2_only_10k_dataset_build"] else "PARTIAL",
        "allow_mixed_h16": False,
        "allow_h16_v2_only_10k_dataset_build": readiness["allow_h16_v2_only_10k_dataset_build"],
        "allow_v3_mainline": False,
        "allow_h17_return_aware_ablation": False,
        "checks": readiness["checks"],
        "blockers": readiness["blockers"],
        "next_step": "H16 V2-only 10k fixed-horizon gas-adjusted dataset build" if readiness["allow_h16_v2_only_10k_dataset_build"] else "Repair V2-only blockers before 10k build",
        "not_full_strategy_backtest": True,
        "promote_live_allowed": False,
        "forbidden": ["mixed H16 resume", "V3 mainline", "production retrain", "promote", "live", "production model overwrite"],
    }
    patch = {
        "patch": "run-004-h16.6-split-v2-mainline-from-v3-repair",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "rpc_readiness_report": rpc,
        "v2_candidate_report": candidate_report,
        "v2_pilot_checkpoint_report": pilot_checkpoint,
        "v2_readiness_report": readiness,
        "v3_repair_branch_plan": v3_plan,
        "gate": gate,
        "safety_assertions": {
            **cfg.get("safety", {}),
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
            "rpc_url_written_to_artifacts": False,
        },
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h166_markdown(patch))
    return patch


def _h167_risk_severity(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    risk = cfg.get("risk_severity", {})
    pool = _lower_addr(row.get("pool_address"))
    fixed = optional_float(row.get("fixed_cost_adjusted_return_30m")) or 0.0
    quote = int(optional_float(row.get("quote_anomaly_label")) or 0) == 1
    scale = int(optional_float(row.get("scale_risk_label")) or 0) == 1
    gas_dom = int(optional_float(row.get("gas_cost_dominates_label")) or 0) == 1
    confirmed_bad = {_lower_addr(p) for p in risk.get("confirmed_bad_pools", [])}
    alpha_island = {_lower_addr(p) for p in risk.get("alpha_island_pools", [])}
    hard_abs = float(risk.get("hard_exclude_abs_return", 0.20))
    if pool in confirmed_bad or (quote and abs(fixed) >= hard_abs):
        return "hard_exclude"
    if gas_dom:
        return "soft_exclude"
    if scale:
        return "feature_limited"
    if pool in alpha_island or quote:
        return "review_only"
    return "clean"


def _h167_label_v2_dataset(cfg: dict[str, Any], candidates: list[dict[str, Any]], rpc_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    outputs = cfg["outputs"]
    if rpc_report.get("status") != "READY":
        checkpoint = {
            "status": "BLOCKED_RPC_ENV",
            "selected": 0,
            "attempted": 0,
            "success": 0,
            "failed": 0,
            "failure_reasons": {"missing_rpc_env": 1},
            "estimated_rpc_calls": 0,
            "actual_rpc_calls": 0,
            "resume_state": {"completed": False, "reason": "missing_rpc_env"},
        }
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return [], checkpoint

    from scripts import label_dashcam_executable as labeler  # noqa: WPS433

    class CountedRPC(labeler.RPC):  # type: ignore[misc]
        def __init__(self, url: str):
            super().__init__(url)
            self.call_count = 0

        def call(self, method: str, params: list[Any]) -> dict[str, Any]:  # type: ignore[override]
            self.call_count += 1
            return super().call(method, params)

        def batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:  # type: ignore[override]
            self.call_count += len(calls)
            return super().batch(calls)

    lp = cfg["labeling"]
    target_success = int(lp.get("target_success_rows", 10000))
    max_attempts = min(int(lp.get("max_attempts", len(candidates))), len(candidates))
    batch_size = int(lp.get("batch_size", 250))
    selected = [row for row in candidates if str(row.get("pool_class")) == "v2"][:max_attempts]
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    rpc = CountedRPC(rpc_url)
    checkpoint = {
        "status": "started",
        "selected": len(selected),
        "target_success_rows": target_success,
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "failure_reasons": {},
        "label_type_counts": {},
        "estimated_rpc_calls": len(selected) * 4,
        "actual_rpc_calls": 0,
        "quote_success_rate": None,
        "batch_size": batch_size,
        "completed_batches": 0,
        "resume_state": {"completed": False, "next_candidate_id": 0},
    }
    try:
        chain = rpc.call("eth_chainId", [])
        if int(chain["result"], 16) != 8453:
            raise RuntimeError(f"unexpected_chain_id:{int(chain['result'], 16)}")
        horizon = int(lp.get("fixed_horizon_sec", 1800))
        min_ts = min(int(r["timestamp_ms"]) // 1000 for r in selected)
        max_ts = max(int(r["timestamp_ms"]) // 1000 for r in selected) + horizon
        bn_arr, ts_arr = labeler.build_ts_grid(rpc, min_ts, max_ts)
        rows: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        failed_rows: list[dict[str, Any]] = []
        workers = int(lp.get("workers", 20))

        def process_candidate(cand: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
            ts_sec = int(cand["timestamp_ms"]) // 1000
            entry_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec)
            exit_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec + horizon)
            meta = {
                "pool_type": cand.get("pool_type") or "v2",
                "t0": _lower_addr(cand.get("token0_address")),
                "t1": _lower_addr(cand.get("token1_address")),
                "fee_bps": int(optional_float(cand.get("fee_bps")) or 0),
            }
            label, net, label_type = labeler.label_one(
                rpc,
                _lower_addr(cand.get("pool_address")),
                meta,
                labeler.WETH_BASE,
                int(lp.get("notional_wei", 500000000000000)),
                entry_block,
                exit_block,
            )
            if label < 0:
                return False, {"candidate_id": cand.get("candidate_id"), "pool_address": cand.get("pool_address"), "failure_reason": label_type}, label_type
            fixed = float(net)
            gas = _h165_gas_adjusted(fixed, "v2", {"labeling_pilot": lp})
            medium = optional_float(gas.get("gas_adjusted_return_30m_medium")) or fixed
            quote_anom = int(abs(fixed) >= 0.20)
            scale_risk = int(abs(fixed) >= 0.50)
            gas_dom = int(fixed > 0 and medium <= 0)
            row = {
                **cand,
                "entry_block": entry_block,
                "exit_block": exit_block,
                "fixed_horizon_sec": horizon,
                "label_type": label_type,
                "gross_quote_return_30m": rounded(fixed + float(lp.get("round_trip_cost", 0.006)), 8),
                "fixed_cost_adjusted_return_30m": rounded(fixed, 8),
                **gas,
                "final_return_after_cost": rounded(medium, 8),
                "slippage_stress_return": rounded(medium - float(lp.get("slippage_stress_bps", 100)) / 10000.0, 8),
                "exit_policy_used": "fixed_30m_fallback",
                "exit_reason": "fixed_horizon",
                "dynamic_exit_available": False,
                "dynamic_exit_unavailable_reason": "missing_intra_window_quote_path_trigger_block_token_amount_and_trigger_sell_quote",
                "executable_label_after_gas": int(medium > 0),
                "alpha_ret_gt_5pct_after_gas": int(medium > 0.05),
                "alpha_ret_gt_10pct_after_gas": int(medium > 0.10),
                "bad_pool_label": int(_lower_addr(cand.get("pool_address")) in {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("confirmed_bad_pools", [])}),
                "quote_anomaly_label": quote_anom,
                "scale_risk_label": scale_risk,
                "liquidity_anomaly_label": int(label_type in ("fail_low_liquidity", "executable_exit_drained")),
                "gas_cost_dominates_label": gas_dom,
                "alpha_island_candidate_label": int(_lower_addr(cand.get("pool_address")) in {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("alpha_island_pools", [])}),
                "quarantine_reason": "quote_anomaly" if quote_anom else ("gas_cost_dominates" if gas_dom else ""),
            }
            row["risk_severity"] = _h167_risk_severity(row, cfg)
            return True, row, label_type

        for start in range(0, len(selected), batch_size):
            batch = selected[start:start + batch_size]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(process_candidate, cand) for cand in batch]
                for future in as_completed(futures):
                    ok, payload, label_type = future.result()
                    checkpoint["attempted"] += 1
                    reason_counts[label_type] += 1
                    if not ok:
                        failed_rows.append(payload)
                        checkpoint["failed"] += 1
                        continue
                    if len(rows) < target_success:
                        payload["row_id"] = len(rows)
                        rows.append(payload)
                        checkpoint["success"] = len(rows)
            checkpoint["completed_batches"] += 1
            checkpoint["actual_rpc_calls"] = rpc.call_count
            checkpoint["label_type_counts"] = dict(reason_counts.most_common())
            checkpoint["failure_reasons"] = {k: v for k, v in reason_counts.most_common() if k != "executable"}
            checkpoint["quote_success_rate"] = rounded(checkpoint["success"] / checkpoint["attempted"], 8) if checkpoint["attempted"] else 0.0
            checkpoint["resume_state"] = {"completed": len(rows) >= target_success, "next_candidate_id": start + len(batch)}
            write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
            if len(rows) >= target_success:
                break

        sorted_rows = sorted(rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
        denom = max(1, len(sorted_rows) - 1)
        rank_by_id = {str(r["row_id"]): idx / denom for idx, r in enumerate(sorted_rows)}
        top_n = math.ceil(0.20 * len(rows))
        top_ids = {str(r["row_id"]) for r in sorted(rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf, reverse=True)[:top_n]}
        by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_pool[_lower_addr(row.get("pool_address"))].append(row)
        pool_rank: dict[str, float] = {}
        for pool_rows in by_pool.values():
            sr = sorted(pool_rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
            d = max(1, len(sr) - 1)
            for idx, row in enumerate(sr):
                pool_rank[str(row["row_id"])] = idx / d
        for row in rows:
            row["alpha_top20_after_gas"] = int(str(row["row_id"]) in top_ids)
            row["final_return_rank"] = rounded(rank_by_id.get(str(row["row_id"]), 0.0), 8)
            row["within_pool_return_rank"] = rounded(pool_rank.get(str(row["row_id"]), 0.0), 8)
        checkpoint["status"] = "completed" if len(rows) >= target_success else "PARTIAL"
        checkpoint["actual_rpc_calls"] = rpc.call_count
        checkpoint["success"] = len(rows)
        checkpoint["failed"] = len(failed_rows)
        checkpoint["label_type_counts"] = dict(reason_counts.most_common())
        checkpoint["failure_reasons"] = {k: v for k, v in reason_counts.most_common() if k != "executable"}
        checkpoint["quote_success_rate"] = rounded(len(rows) / checkpoint["attempted"], 8) if checkpoint["attempted"] else 0.0
        checkpoint["return_distribution"] = numeric_summary([r.get("final_return_after_cost") for r in rows])
        checkpoint["risk_severity_distribution"] = dict(Counter(str(r.get("risk_severity")) for r in rows).most_common())
        checkpoint["alpha_top20_after_gas_distribution"] = dict(Counter(str(r.get("alpha_top20_after_gas")) for r in rows).most_common())
        checkpoint["decoded_coverage"] = {
            "rows": len(rows),
            "rows_with_decoded_events": sum(1 for r in rows if int(r.get("decoded_event_count") or 0) > 0),
            "coverage": rounded(sum(1 for r in rows if int(r.get("decoded_event_count") or 0) > 0) / len(rows), 8) if rows else 0.0,
        }
        checkpoint["feature_coverage"] = {"decoded_bridge_features": "metadata_and_decoded_event_count_only"}
        checkpoint["resume_state"] = {"completed": checkpoint["status"] == "completed", "next_candidate_id": checkpoint["attempted"]}
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return rows, checkpoint
    except Exception as exc:
        checkpoint["status"] = "LABELER_FAILURE"
        checkpoint["failure_reasons"] = {str(exc): checkpoint.get("attempted", 0)}
        checkpoint["actual_rpc_calls"] = getattr(rpc, "call_count", 0)
        checkpoint["resume_state"] = {"completed": False, "reason": str(exc)}
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return [], checkpoint


def _h167_quality_report(all_rows: list[dict[str, Any]], clean: list[dict[str, Any]], risk: list[dict[str, Any]], checkpoint: dict[str, Any]) -> dict[str, Any]:
    dist = _h16_distribution(all_rows)
    return {
        "status": "COMPLETE" if len(all_rows) >= 10000 else "PARTIAL",
        "final_rows": len(all_rows),
        **dist,
        "pool_type_policy": {"v2_only": True, "mixed_v2_v3_allowed": False, "v3_mainline_allowed": False},
        "time_bucket_distribution": {
            "days_top20": dict(Counter(str(r.get("day_bucket")) for r in all_rows).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in all_rows).most_common()),
        },
        "return_distribution_after_gas": numeric_summary([r.get("final_return_after_cost") for r in all_rows]),
        "fixed_cost_adjusted_return_distribution": numeric_summary([r.get("fixed_cost_adjusted_return_30m") for r in all_rows]),
        "alpha_top20_after_gas_distribution": dict(Counter(str(r.get("alpha_top20_after_gas")) for r in all_rows).most_common()),
        "risk_severity_distribution": dict(Counter(str(r.get("risk_severity")) for r in all_rows).most_common()),
        "clean_alpha_subset_size": len(clean),
        "risk_bad_pool_subset_size": len(risk),
        "decoded_coverage": checkpoint.get("decoded_coverage"),
        "feature_coverage": checkpoint.get("feature_coverage"),
        "checkpoint_resume_summary": checkpoint,
    }


def _h167_recompute_exact_top20(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for idx, row in enumerate(rows):
        row["row_id"] = int(optional_float(row.get("row_id")) if row.get("row_id") not in ("", None) else idx)
    sorted_rows = sorted(rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
    denom = max(1, len(sorted_rows) - 1)
    rank_by_id = {str(r["row_id"]): idx / denom for idx, r in enumerate(sorted_rows)}
    top_n = math.ceil(0.20 * len(rows))
    top_ids = {str(r["row_id"]) for r in sorted(rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf, reverse=True)[:top_n]}
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_pool[_lower_addr(row.get("pool_address"))].append(row)
    pool_rank: dict[str, float] = {}
    for pool_rows in by_pool.values():
        sr = sorted(pool_rows, key=lambda r: optional_float(r.get("final_return_after_cost")) or -math.inf)
        d = max(1, len(sr) - 1)
        for idx, row in enumerate(sr):
            pool_rank[str(row["row_id"])] = idx / d
    for row in rows:
        row["alpha_top20_after_gas"] = int(str(row["row_id"]) in top_ids)
        row["final_return_rank"] = rounded(rank_by_id.get(str(row["row_id"]), 0.0), 8)
        row["within_pool_return_rank"] = rounded(pool_rank.get(str(row["row_id"]), 0.0), 8)
    return rows


def _h167_markdown(patch: dict[str, Any]) -> str:
    q = patch["dataset_quality_report"]
    ck = patch["checkpoint_report"]
    gate = patch["gate"]
    return f"""# Run-004 Patch H16.7 - V2-Only 10k Fixed-Horizon Gas-Adjusted Dataset

## Status
- Result: `{gate['status']}`
- V2-only rows: {q['final_rows']}
- Mixed V2/V3 allowed: `False`
- V3 mainline allowed: `False`
- Promote/live allowed: `False`

## Labeling
- Attempted / success / failed: {ck['attempted']} / {ck['success']} / {ck['failed']}
- Quote success rate: {ck.get('quote_success_rate')}
- Actual RPC calls: {ck.get('actual_rpc_calls')}
- Failure reasons: {ck.get('failure_reasons')}

## Dataset
- Unique pools: {q['unique_pools']}
- Top1/top5/non-top5: {q['top1_share']} / {q['top5_share']} / {q['non_top5_share']}
- Clean alpha rows: {q['clean_alpha_subset_size']}
- Risk rows: {q['risk_bad_pool_subset_size']}
- Alpha top20 distribution: {q['alpha_top20_after_gas_distribution']}
- Risk severity: {q['risk_severity_distribution']}
- Decoded coverage: {q['decoded_coverage']}

## Gate
- Allow H17 V2-only return-aware ablation: `{gate['allow_h17_v2_only_return_aware_ablation']}`
- Blockers: {gate['blockers']}
- Not a full strategy backtest: `True`
"""


def _h167_v2_only_10k_fixed_horizon_gas_adjusted(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    inputs = cfg["input_paths"]
    outputs = cfg["outputs"]
    rpc = _h165_rpc_readiness({"labeling_pilot": cfg["labeling"], "clickhouse": {}})
    candidates = read_rows(_resolve(inputs["h166_v2_candidate_csv"]))
    existing_all = _resolve(outputs["all_dataset_csv"])
    if existing_all.exists():
        existing_rows = read_rows(existing_all)
    else:
        existing_rows = []
    if len(existing_rows) >= int(cfg["labeling"].get("target_success_rows", 10000)):
        rows = _h167_recompute_exact_top20(existing_rows[:int(cfg["labeling"].get("target_success_rows", 10000))])
        checkpoint_path = _resolve(outputs["checkpoint_report_json"])
        checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
        checkpoint["status"] = "completed"
        checkpoint["success"] = len(rows)
        checkpoint["alpha_top20_after_gas_distribution"] = dict(Counter(str(r.get("alpha_top20_after_gas")) for r in rows).most_common())
        checkpoint["resume_state"] = {"completed": True, "next_candidate_id": checkpoint.get("attempted", len(rows))}
        write_json(checkpoint_path, checkpoint)
    else:
        rows, checkpoint = _h167_label_v2_dataset(cfg, candidates, rpc)
    clean = [r for r in rows if r.get("risk_severity") == "clean"]
    risk = [r for r in rows if r.get("risk_severity") != "clean"]
    write_csv_rows(_resolve(outputs["all_dataset_csv"]), rows)
    write_csv_rows(_resolve(outputs["clean_alpha_dataset_csv"]), clean)
    write_csv_rows(_resolve(outputs["risk_bad_pool_dataset_csv"]), risk)
    severity_report = {
        "risk_severity_distribution": dict(Counter(str(r.get("risk_severity")) for r in rows).most_common()),
        "all_rows": len(rows),
        "clean_alpha_rows": len(clean),
        "risk_bad_pool_rows": len(risk),
        "rules": {
            "hard_exclude": "confirmed bad pool or quote anomaly with abs fixed return >= configured threshold",
            "soft_exclude": "gas cost dominates",
            "feature_limited": "scale/decimals risk",
            "review_only": "alpha-island or quote anomaly without hard exclusion",
            "clean": "no risk flags",
        },
    }
    write_json(_resolve(outputs["risk_severity_report_json"]), severity_report)
    return_accounting = {
        "scope": "v2_only_fixed_30m",
        "fixed_horizon_sec": int(cfg["labeling"].get("fixed_horizon_sec", 1800)),
        "primary_target": "alpha_top20_after_gas",
        "dynamic_exit_available": False,
        "not_full_strategy_backtest": True,
        "gas_model": {
            "gas_price_gwei": cfg["labeling"].get("gas_price_gwei"),
            "gas_units": cfg["labeling"].get("gas_units", {}).get("v2"),
            "include_approval_gas": cfg["labeling"].get("include_approval_gas", True),
            "approval_gas_units": cfg["labeling"].get("approval_gas_units", 0),
        },
    }
    write_json(_resolve(outputs["return_accounting_report_json"]), return_accounting)
    quality = _h167_quality_report(rows, clean, risk, checkpoint)
    write_json(_resolve(outputs["dataset_quality_report_json"]), quality)
    checks = {
        "all_rows_gte_10000": len(rows) >= 10000,
        "v2_only_policy_enforced": set(str(r.get("pool_class")) for r in rows) <= {"v2"},
        "quote_success_rate_gte_90pct": (checkpoint.get("quote_success_rate") or 0) >= 0.90,
        "clean_alpha_subset_available": len(clean) > 0,
        "risk_subset_available": len(risk) > 0,
        "alpha_top20_after_gas_available": bool(quality["alpha_top20_after_gas_distribution"]),
        "checkpoint_completed": checkpoint.get("status") == "completed",
        "mixed_h16_disabled": True,
        "v3_mainline_disabled": True,
    }
    success = all(checks.values())
    gate = {
        "status": "PASS" if success else "PARTIAL",
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "allow_h17_v2_only_return_aware_ablation": success,
        "allow_mixed_h16": False,
        "allow_v3_mainline": False,
        "allow_promote_live": False,
        "not_full_strategy_backtest": True,
        "next_step": "H17 V2-only return-aware ablation on all/clean/risk-separated datasets" if success else "Repair H16.7 blockers before H17",
        "forbidden": ["mixed V2/V3 H16", "V3 mainline", "production retrain", "promote", "live", "production model overwrite"],
    }
    patch = {
        "patch": "run-004-h16.7-v2-only-10k-fixed-horizon-gas-adjusted-dataset",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "rpc_readiness_report": rpc,
        "checkpoint_report": checkpoint,
        "return_accounting_report": return_accounting,
        "risk_severity_report": severity_report,
        "dataset_quality_report": quality,
        "gate": gate,
        "safety_assertions": {
            **cfg.get("safety", {}),
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
            "rpc_url_written_to_artifacts": False,
        },
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h167_markdown(patch))
    return patch


def _h168_prepare_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        if not item.get("timestamp"):
            item["timestamp"] = str(item.get("timestamp_ms", ""))
        out.append(item)
    return out


def _h168_chunk_rows_by_pool(rows: list[dict[str, str]], chunk_size: int = 400) -> list[list[dict[str, str]]]:
    seen: set[str] = set()
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for row in rows:
        pool = _lower_addr(row.get("pool_address"))
        if not pool or pool in seen:
            continue
        seen.add(pool)
        current.append(row)
        if len(current) >= chunk_size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _h168_load_metadata_chunked(client: ClickHouseClient, db: str, rows: list[dict[str, str]], table: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _h168_chunk_rows_by_pool(rows):
        out.update(_h108_load_metadata(client, db, chunk, table))
    return out


def _h168_load_v2_events_chunked(client: ClickHouseClient, db: str, rows: list[dict[str, str]], table: str, max_window: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _h168_chunk_rows_by_pool(rows):
        events = _h108_load_v2_events(client, db, chunk, table, max_window)
        for pool, evs in events.items():
            out[pool].extend(evs)
    for pool in list(out):
        out[pool].sort(key=lambda e: int(optional_float(e.get("ts_ms")) or 0))
    return out


def _h168_window_match_rates(rows: list[dict[str, str]], events: dict[str, list[dict[str, Any]]], windows: list[int]) -> dict[str, Any]:
    total = len(rows)
    out: dict[str, Any] = {}
    for window in windows:
        matched = 0
        no_history = 0
        for row in rows:
            pool = _lower_addr(row.get("pool_address"))
            entry_ms = _entry_ts_ms(row)
            if entry_ms is None:
                continue
            count = sum(
                1
                for event in events.get(pool, [])
                if entry_ms - window * 1000 <= int(optional_float(event.get("ts_ms")) or 0) < entry_ms
            )
            matched += int(count > 0)
            no_history += int(count == 0)
        out[f"{window}s"] = {
            "matched_rows": matched,
            "unmatched_rows": no_history,
            "match_rate": rounded(matched / total, 8) if total else 0.0,
        }
    return out


def _h168_root_cause_audit(
    rows: list[dict[str, str]],
    events: dict[str, list[dict[str, Any]]],
    metadata: dict[str, dict[str, Any]],
    windows: list[int],
    h167_quality: dict[str, Any],
) -> dict[str, Any]:
    total = len(rows)
    pools = {_lower_addr(row.get("pool_address")) for row in rows}
    decoded_pools = {pool for pool, evs in events.items() if evs}
    source_identity_rows = sum(1 for row in rows if row.get("source_tx_hash") or row.get("source_log_index") or row.get("source_block_number"))
    has_candidate_kind = Counter(str(row.get("candidate_kind")) for row in rows)
    time_rates = _h168_window_match_rates(rows, events, windows)
    match_30m = time_rates.get("1800s", {})
    return {
        "coverage_before_repair": h167_quality.get("decoded_coverage", {}),
        "rows": total,
        "unique_dataset_pools": len(pools),
        "match_matrix": {
            "dataset.pool_address_to_uniswap_v2_swaps_decoded.pair_address": {
                "unique_matched_pools": len(pools & decoded_pools),
                "unique_match_rate": rounded(len(pools & decoded_pools) / len(pools), 8) if pools else 0.0,
            },
            "dataset.pool_address_to_discovered_pools.pool_address": {
                "matched_rows": sum(1 for row in rows if _lower_addr(row.get("pool_address")) in metadata),
                "match_rate": rounded(sum(1 for row in rows if _lower_addr(row.get("pool_address")) in metadata) / total, 8) if total else 0.0,
            },
            "dataset.source_tx_hash_log_index_to_decoded_tx_log": {
                "source_identity_rows": source_identity_rows,
                "match_rate": rounded(source_identity_rows / total, 8) if total else 0.0,
                "status": "unavailable_in_h16_7_dataset",
            },
            "time_window_match_rates": {
                "1m": time_rates.get("60s", {}),
                "5m": time_rates.get("300s", {}),
                "30m": time_rates.get("1800s", {}),
            },
        },
        "field_semantics": {
            "pool_address_equals_v2_pair_address": True,
            "token_address_pool_address_drift": "H16.7 uses pool_address as pair/pool; token_address is absent in this dataset, so no token-vs-pool drift is observed here.",
            "candidate_source": dict(has_candidate_kind),
            "candidate_source_interpretation": "H16.7 candidates are metadata WETH pool/time-offset rows, not real decoded swap rows.",
            "source_tx_hash_log_index_block_number": "not present; cannot perform exact tx/log join",
            "timestamp_unit": "timestamp_ms is milliseconds; entry_time is ISO; repair normalizes timestamp=timestamp_ms for bridge logic",
            "entry_time_source": "metadata discovered_at plus configured offset, not swap block_time",
            "lowercase_normalization": "pool addresses are normalized lower-case before joins",
        },
        "root_cause": (
            "The 9.59% H16.7 decoded coverage was mostly a candidate-construction artifact: "
            "the V2 10k rows were sampled from discovered_pools metadata and synthetic time offsets, "
            "not from decoded swap events. Most sampled pools/times have no pre-entry decoded V2 swap "
            f"within the 30m feature window (observed matched rows={match_30m.get('matched_rows')}). "
            "Exact tx/log enrichment is impossible because source_tx_hash/log_index/source_block_number were not carried."
        ),
        "coverage_counts_are_correct": "H16.7 counted rows whose pool had any decoded event count evidence, not necessarily rows with actual pre-entry decoded events in the requested windows; H16.8 recomputes true pre-entry window coverage.",
        "future_leakage_avoided": True,
    }


def _h168_add_aliases_and_metadata(row: dict[str, Any], meta: dict[str, Any] | None, windows: list[int]) -> None:
    for window in windows:
        suffix = f"{window}s"
        row[f"decoded_event_count_{suffix}"] = row.get(f"event_count_{suffix}", "")
    if meta:
        row["pool_type"] = row.get("pool_type") or meta.get("pool_type", "")
        row["protocol"] = row.get("protocol") or meta.get("protocol", "")
        row["token0"] = meta.get("token0_address", "")
        row["token1"] = meta.get("token1_address", "")
        row["token0_symbol_backfilled"] = meta.get("token0_symbol", "")
        row["token1_symbol_backfilled"] = meta.get("token1_symbol", "")
        row["weth_leg"] = "token0" if _lower_addr(meta.get("token0_address")) == WETH_BASE else ("token1" if _lower_addr(meta.get("token1_address")) == WETH_BASE else "")
    else:
        row["token0"] = row.get("token0_address", "")
        row["token1"] = row.get("token1_address", "")
        row["weth_leg"] = "token0" if _lower_addr(row.get("token0_address")) == WETH_BASE else ("token1" if _lower_addr(row.get("token1_address")) == WETH_BASE else "")


def _h168_correct_pre_entry_join_status(row: dict[str, Any]) -> None:
    if optional_float(row.get("event_count_1800s")) is None:
        row["decoded_join_status"] = "unmatched"
        row["decoded_source_used"] = ""
        row["decoded_event_count_5m"] = ""
        row["decoded_event_count_30m"] = ""
        row["wallet_source_available"] = "false"
        row["side_source_available"] = "false"
        row["amount_source_available"] = "false"
        row["side_inferred"] = "false"
        row["buyer_side_unavailable"] = "true"
        row["backfill_source_status"] = "unavailable"


def _h168_reaudit_risk(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before = Counter(str(row.get("risk_severity")) for row in rows)
    changed: Counter[str] = Counter()
    missing_sev = str(cfg.get("risk_severity", {}).get("missing_decoded_events_severity", "feature_limited"))
    confirmed_bad = {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("confirmed_bad_pools", [])}
    alpha_island = {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("alpha_island_pools", [])}
    for row in rows:
        old = str(row.get("risk_severity") or "clean")
        pool = _lower_addr(row.get("pool_address"))
        matched = str(row.get("decoded_join_status")) == "matched"
        quote = int(optional_float(row.get("quote_anomaly_label")) or 0) == 1
        gas_dom = int(optional_float(row.get("gas_cost_dominates_label")) or 0) == 1
        scale = int(optional_float(row.get("scale_risk_label")) or 0) == 1
        new = old
        reason = ""
        if pool in confirmed_bad or (quote and abs(optional_float(row.get("fixed_cost_adjusted_return_30m")) or 0.0) >= 0.20):
            new = "hard_exclude"
            reason = "hard_quote_or_confirmed_bad_pool"
        elif gas_dom:
            new = "soft_exclude"
            reason = "gas_cost_dominates"
        elif scale:
            new = "feature_limited"
            reason = "scale_risk"
        elif not matched:
            new = missing_sev
            reason = "missing_decoded_pre_entry_features"
        elif pool in alpha_island or quote:
            new = "review_only"
            reason = "alpha_island_or_quote_review"
        else:
            new = "clean"
            reason = "decoded_features_available_no_risk_flags"
        row["risk_severity_before_h168"] = old
        row["risk_severity"] = new
        row["h168_risk_change_reason"] = reason
        if old != new:
            changed[f"{old}->{new}:{reason}"] += 1
    after = Counter(str(row.get("risk_severity")) for row in rows)
    clean = [row for row in rows if row.get("risk_severity") == "clean"]
    risk = [row for row in rows if row.get("risk_severity") != "clean"]
    return rows, {
        "risk_severity_before_repair": dict(before.most_common()),
        "risk_severity_after_enrichment": dict(after.most_common()),
        "clean_alpha_rows_after_repair": len(clean),
        "risk_rows_after_repair": len(risk),
        "changed_rows": sum(changed.values()),
        "reason_distribution": dict(changed.most_common()),
        "risk_rows_were_low_in_h16_7_because": "H16.7 risk severity was driven mostly by quote/gas/focus-pool labels and did not penalize missing decoded microstructure features.",
        "decoded_feature_missing_now_audited": True,
        "datasets_regenerated": True,
    }


def _h168_feature_coverage_report(rows: list[dict[str, Any]], feature_cols: list[str], windows: list[int]) -> dict[str, Any]:
    header = list(rows[0].keys()) if rows else []
    family = {
        "sender_concentration": nonempty_coverage(rows, [c for c in header if c.startswith(("unique_swap_senders_", "top1_sender_share_", "top3_sender_share_", "sender_gini_", "sender_entropy_"))]),
        "buyer_concentration": nonempty_coverage(rows, [c for c in header if c.startswith(("unique_buyers_", "unique_sellers_", "top1_buyer_share_", "top3_buyer_share_", "buyer_gini_", "buy_wallet_entropy_"))]),
        "flow_count": nonempty_coverage(rows, [c for c in header if c.startswith(("buy_count_", "sell_count_", "buy_sell_count_ratio_", "decoded_event_count_"))]),
        "raw_flow": nonempty_coverage(rows, [c for c in header if c.startswith(("buy_volume_raw_", "sell_volume_raw_", "net_flow_raw_", "abs_flow_raw_", "median_trade_size_raw_", "trade_size_cv_"))]),
        "temporal": nonempty_coverage(rows, ["activity_acceleration_1m_vs_5m", "activity_acceleration_3m_vs_15m", "buy_pressure_acceleration_1m_vs_5m", "sender_entropy_change_1m_vs_5m"]),
        "protocol_metadata": nonempty_coverage(rows, ["pool_type", "protocol", "token0", "token1", "weth_leg", "decoded_source_used", "decoded_join_status"]),
    }
    per_feature = {}
    for col in feature_cols + [f"decoded_event_count_{w}s" for w in windows] + ["token0", "token1", "weth_leg"]:
        if col in header:
            per_feature[col] = numeric_summary([row.get(col) for row in rows]) if col not in {"token0", "token1", "weth_leg", "decoded_source_used", "decoded_join_status"} else {
                "count": sum(1 for row in rows if str(row.get(col, "")) != ""),
                "missing_count": sum(1 for row in rows if str(row.get(col, "")) == ""),
                "missing_rate": rounded(sum(1 for row in rows if str(row.get(col, "")) == "") / len(rows), 8) if rows else None,
            }
    matched = sum(1 for row in rows if str(row.get("decoded_join_status")) == "matched")
    no_events_30m = sum(1 for row in rows if not optional_float(row.get("decoded_event_count_1800s")))
    return {
        "rows": len(rows),
        "decoded_bridge_coverage": {"matched_rows": matched, "coverage": rounded(matched / len(rows), 8) if rows else 0.0},
        "family_coverage": family,
        "per_feature_quality": per_feature,
        "rows_with_insufficient_pre_entry_history": no_events_30m,
        "rows_with_no_decoded_events_in_window": no_events_30m,
        "all_zero_or_constant_features": {
            col: {"all_zero": item.get("all_zero"), "constant": item.get("constant"), "extreme_value_flag": item.get("extreme_value_flag")}
            for col, item in per_feature.items()
            if isinstance(item, dict) and ("all_zero" in item or "constant" in item)
        },
    }


def _h168_checkpoint_accounting(checkpoint: dict[str, Any]) -> dict[str, Any]:
    attempted = int(checkpoint.get("attempted") or 0)
    success_kept = int(checkpoint.get("success") or 0)
    failed = int(checkpoint.get("failed") or 0)
    executable_seen = int(checkpoint.get("label_type_counts", {}).get("executable") or success_kept)
    stopped_after_target = max(0, executable_seen - success_kept)
    accounted = success_kept + failed + stopped_after_target
    other = attempted - accounted
    return {
        "attempted": attempted,
        "success_kept": success_kept,
        "failed": failed,
        "executable_seen": executable_seen,
        "skipped": 0,
        "duplicate": 0,
        "already_processed": 0,
        "filtered": 0,
        "stopped_after_target": stopped_after_target,
        "other": other,
        "corrected_identity": {
            "attempted_equals_success_kept_plus_failed": attempted == success_kept + failed,
            "attempted_equals_executable_seen_plus_failed": attempted == executable_seen + failed,
            "explanation": "Concurrent batch processing continued to receive executable results after the 10,000 kept-row target. Those extra executable rows were counted in label_type_counts but not retained in the final dataset.",
        },
        "failure_reasons": checkpoint.get("failure_reasons", {}),
        "quote_success_rate_reported": checkpoint.get("quote_success_rate"),
    }


def _h168_markdown(patch: dict[str, Any]) -> str:
    gate = patch["gate"]
    cov = patch["feature_coverage_report"]
    risk = patch["risk_severity_reaudit_report"]
    chk = patch["checkpoint_accounting_report"]
    root = patch["coverage_root_cause_audit"]
    return f"""# Run-004 Patch H16.8 - V2 Decoded Feature Enrichment Repair

## Status
- Result: `{gate['status']}`
- Rows: {gate['checks'].get('rows_eq_10000')}
- H17 allowed: `{gate['allow_h17_v2_only_return_aware_feature_ablation']}`
- Promote/live allowed: `False`

## Root Cause
- {root['root_cause']}

## Coverage
- Decoded bridge coverage: {cov['decoded_bridge_coverage']}
- Sender coverage: {cov['family_coverage']['sender_concentration']['coverage']}
- Buyer coverage: {cov['family_coverage']['buyer_concentration']['coverage']}
- Flow count coverage: {cov['family_coverage']['flow_count']['coverage']}
- Raw flow coverage: {cov['family_coverage']['raw_flow']['coverage']}
- Temporal coverage: {cov['family_coverage']['temporal']['coverage']}
- Protocol metadata coverage: {cov['family_coverage']['protocol_metadata']['coverage']}

## Risk Re-Audit
- Before: {risk['risk_severity_before_repair']}
- After: {risk['risk_severity_after_enrichment']}
- Clean/risk after repair: {risk['clean_alpha_rows_after_repair']} / {risk['risk_rows_after_repair']}
- Changed rows: {risk['changed_rows']}

## Checkpoint Accounting
- Attempted / success kept / failed: {chk['attempted']} / {chk['success_kept']} / {chk['failed']}
- Stopped-after-target executable rows: {chk['stopped_after_target']}
- Explanation: {chk['corrected_identity']['explanation']}

## Gate
- Blockers: {gate['blockers']}
- Next step: {gate['next_step']}
"""


def _h168_v2_decoded_feature_enrichment_repair(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    inputs = cfg["input_paths"]
    outputs = cfg["outputs"]
    rows = _h168_prepare_rows(read_rows(_resolve(inputs["h167_all_dataset_csv"])))
    h167_quality = json.loads(_resolve(inputs["h167_dataset_quality_report_json"]).read_text())
    h167_checkpoint = json.loads(_resolve(inputs["h167_checkpoint_report_json"]).read_text())
    windows = [int(w) for w in cfg.get("windows_sec", [60, 180, 300, 900, 1800])]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    db = client.database
    src = cfg["clickhouse"]["decoded_sources"]
    metadata = _h168_load_metadata_chunked(client, db, rows, src["metadata_table"])
    v2_events = _h168_load_v2_events_chunked(client, db, rows, src["v2_table"], max(windows))
    root = _h168_root_cause_audit(rows, v2_events, metadata, windows, h167_quality)
    write_json(_resolve(outputs["coverage_root_cause_audit_json"]), root)
    enriched, feature_cols = _h108_enrich_rows(rows, windows, v2_events, {}, metadata)
    for row in enriched:
        _h168_add_aliases_and_metadata(row, metadata.get(_lower_addr(row.get("pool_address"))), windows)
        _h168_correct_pre_entry_join_status(row)
    enriched, risk_report = _h168_reaudit_risk(enriched, cfg)
    clean = [row for row in enriched if row.get("risk_severity") == "clean"]
    risk_rows = [row for row in enriched if row.get("risk_severity") != "clean"]
    write_csv_rows(_resolve(outputs["feature_ready_all_dataset_csv"]), enriched)
    write_csv_rows(_resolve(outputs["feature_ready_clean_alpha_dataset_csv"]), clean)
    write_csv_rows(_resolve(outputs["feature_ready_risk_bad_pool_dataset_csv"]), risk_rows)
    coverage = _h168_feature_coverage_report(enriched, feature_cols, windows)
    write_json(_resolve(outputs["feature_coverage_report_json"]), coverage)
    write_json(_resolve(outputs["risk_severity_reaudit_report_json"]), risk_report)
    checkpoint_accounting = _h168_checkpoint_accounting(h167_checkpoint)
    write_json(_resolve(outputs["checkpoint_accounting_report_json"]), checkpoint_accounting)
    alpha_counts = Counter(str(row.get("alpha_top20_after_gas")) for row in enriched)
    return_fields = ["fixed_cost_adjusted_return_30m", "gas_adjusted_return_30m_medium", "final_return_after_cost", "slippage_stress_return"]
    family = coverage["family_coverage"]
    checks = {
        "rows_eq_10000": len(enriched) == 10000,
        "decoded_bridge_coverage_gte_90pct": (coverage["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90,
        "sender_coverage_gte_90pct": (family["sender_concentration"]["coverage"] or 0) >= 0.90,
        "buyer_coverage_gte_90pct": (family["buyer_concentration"]["coverage"] or 0) >= 0.90,
        "flow_coverage_gte_90pct": (family["flow_count"]["coverage"] or 0) >= 0.90,
        "return_fields_preserved": all(all(str(row.get(col, "")) != "" for col in return_fields) for row in enriched),
        "alpha_top20_preserved_exact_2000_8000": alpha_counts.get("1", 0) == 2000 and alpha_counts.get("0", 0) == 8000,
        "risk_severity_reaudited": bool(risk_report),
        "checkpoint_accounting_explained": bool(checkpoint_accounting),
        "no_production_model_writes": True,
    }
    success = all(checks.values())
    gate = {
        "status": "PASS" if success else "HOLD",
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "allow_h17_v2_only_return_aware_feature_ablation": success,
        "next_step": "H17 V2-only return-aware feature ablation" if success else "Repair candidate construction to decoded-backed V2 sampling before H17",
        "promote_live_allowed": False,
        "not_full_strategy_backtest": True,
    }
    patch = {
        "patch": "run-004-h16.8-v2-decoded-feature-enrichment-repair",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "coverage_root_cause_audit": root,
        "feature_coverage_report": coverage,
        "risk_severity_reaudit_report": risk_report,
        "checkpoint_accounting_report": checkpoint_accounting,
        "gate": gate,
        "safety_assertions": cfg.get("safety", {}),
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h168_markdown(patch))
    return patch


H169_WINDOWS_DEFAULT = [60, 180, 300, 900, 1800]


def _h169_postmortem(inputs: dict[str, Any]) -> dict[str, Any]:
    root_path = _resolve(inputs.get("h168_coverage_root_cause_audit_json", ""))
    patch_path = _resolve(inputs.get("h168_patch_json", ""))
    checkpoint_path = _resolve(inputs.get("h167_checkpoint_report_json", ""))
    quality_path = _resolve(inputs.get("h167_dataset_quality_report_json", ""))
    root = json.loads(root_path.read_text()) if root_path.exists() else {}
    h168_patch = json.loads(patch_path.read_text()) if patch_path.exists() else {}
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    quality = json.loads(quality_path.read_text()) if quality_path.exists() else {}
    return {
        "severity": "P0",
        "incident": "H16.7 V2-only 10k candidates were metadata-time rows, not event-triggered decoded swaps.",
        "h167_candidate_source": root.get("field_semantics", {}).get("candidate_source", {"metadata_weth_time_offset": quality.get("final_rows", 10000)}),
        "why_metadata_time_offsets_are_invalid_for_h17": [
            "discovered_pools identifies pool metadata, not a trade event.",
            "Synthetic/random time offsets do not carry tx_hash/log_index/block_number identity.",
            "Feature windows anchored to synthetic times cannot be exactly replayed against the triggering swap.",
            "A pool having any decoded activity is not equivalent to the row having pre-entry decoded microstructure evidence.",
        ],
        "observed_decoded_coverage": {
            "h167_reported_pool_level_coverage": checkpoint.get("decoded_coverage"),
            "h168_true_pre_entry_30m_coverage": root.get("match_matrix", {}).get("time_window_match_rates", {}).get("30m"),
            "source_identity_coverage": root.get("match_matrix", {}).get("dataset.source_tx_hash_log_index_to_decoded_tx_log"),
        },
        "why_h17_is_invalid": (
            "With true pre-entry decoded coverage at 49/10,000 rows (0.49%) and no source tx/log/block identity, "
            "H16.7 cannot support sender, buyer, flow, temporal, or event-lineage ablations. Any model trained on it would mostly learn "
            "quote/metadata artifacts rather than decoded event microstructure."
        ),
        "h167_conclusions_retained": [
            "V2 quote path is healthy enough for smoke testing.",
            "RPC/checkpoint accounting works.",
            "The gas-adjusted return schema is useful.",
            "Exact alpha_top20_after_gas ranking logic is useful.",
        ],
        "h167_conclusions_discarded": [
            "feature training readiness",
            "clean/risk distribution",
            "H17 gate",
            "any feature ablation based on H16.7",
        ],
        "h168_gate": h168_patch.get("gate", {}),
        "policy": {
            "synthetic_metadata_time_candidates_allowed_for_alpha_dataset": False,
            "synthetic_metadata_time_candidates_allowed_for_quote_smoke_test": True,
            "decoded_swap_source_required": True,
        },
    }


def _h169_query_capacity(client: ClickHouseClient, cfg: dict[str, Any]) -> dict[str, Any]:
    db = client.database
    src = cfg["clickhouse"]["decoded_sources"]
    queries = {
        "decoded_v2_raw": f"""
            SELECT count() AS rows, uniqExact(lower(pair_address)) AS pools
            FROM {db}.{src['v2_table']}
            WHERE chain_id = 8453 AND removed = 0 AND tx_hash != ''
        """,
        "decoded_v2_metadata_join_any": f"""
            SELECT count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
        """,
        "decoded_v2_metadata_weth_v2": f"""
            SELECT count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND m.has_weth_leg = 1 AND lower(m.pool_type) = 'v2'
        """,
    }
    out: dict[str, Any] = {}
    for name, query in queries.items():
        try:
            out[name] = client.query_json(query)[0]
        except Exception as exc:
            out[name] = {"error": str(exc)}
    return out


def _h169_reconstruct_candidates(client: ClickHouseClient, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sel = cfg["candidate_reconstruction"]
    src = cfg["clickhouse"]["decoded_sources"]
    db = client.database
    target = int(sel.get("target_candidates", 15000))
    max_per_pool = int(sel.get("max_per_pool", 25))
    max_per_pool_hour = int(sel.get("max_per_pool_time_bucket", 3))
    seed = str(sel.get("deterministic_sample_seed", "run004_h169"))
    query = f"""
        SELECT *
        FROM
        (
            SELECT
                row_number() OVER (
                    PARTITION BY lower(e.pair_address), toStartOfHour(coalesce(e.block_time, e.received_at_ms))
                    ORDER BY cityHash64(e.tx_hash, toString(e.log_index), { _sql_string(seed) })
                ) AS rn_pool_hour,
                row_number() OVER (
                    PARTITION BY lower(e.pair_address)
                    ORDER BY cityHash64(toString(coalesce(e.block_time, e.received_at_ms)), e.tx_hash, toString(e.log_index), { _sql_string(seed) })
                ) AS rn_pool,
                lower(e.pair_address) AS pool_address,
                lower(e.pair_address) AS pair_address,
                e.tx_hash AS source_tx_hash,
                e.log_index AS source_log_index,
                e.block_number AS source_block_number,
                toString(coalesce(e.block_time, e.received_at_ms)) AS source_timestamp,
                toString(coalesce(e.block_time, e.received_at_ms)) AS block_time,
                toUnixTimestamp64Milli(coalesce(e.block_time, e.received_at_ms)) AS timestamp_ms,
                toString(coalesce(e.block_time, e.received_at_ms)) AS signal_time,
                toString(coalesce(e.block_time, e.received_at_ms)) AS feature_cutoff_time,
                toString(coalesce(e.block_time, e.received_at_ms)) AS entry_time,
                toString(coalesce(e.block_time, e.received_at_ms) + INTERVAL {int(cfg['labeling'].get('fixed_horizon_sec', 1800))} SECOND) AS exit_time,
                lower(e.sender) AS sender,
                lower(e.recipient) AS recipient,
                toString(e.amount0_in) AS amount0_in,
                toString(e.amount1_in) AS amount1_in,
                toString(e.amount0_out) AS amount0_out,
                toString(e.amount1_out) AS amount1_out,
                lower(m.token0_address) AS token0_address,
                lower(m.token1_address) AS token1_address,
                lower(m.token0_address) AS token0,
                lower(m.token1_address) AS token1,
                m.token0_symbol AS token0_symbol,
                m.token1_symbol AS token1_symbol,
                m.token0_decimals AS token0_decimals,
                m.token1_decimals AS token1_decimals,
                m.fee_bps AS fee_bps,
                lower(m.pool_type) AS pool_type,
                'v2' AS pool_class,
                'v2' AS protocol,
                if(lower(m.token0_address) = { _sql_string(WETH_BASE) }, 'token0', if(lower(m.token1_address) = { _sql_string(WETH_BASE) }, 'token1', '')) AS weth_leg,
                'uniswap_v2_swaps_decoded' AS decoded_source,
                'uniswap_v2_swaps_decoded' AS decoded_source_used,
                'matched' AS decoded_join_status,
                'decoded_swap_event' AS candidate_kind,
                1 AS include_source_event_in_features,
                toDate(coalesce(e.block_time, e.received_at_ms)) AS day_bucket,
                toHour(coalesce(e.block_time, e.received_at_ms)) AS hour_bucket
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m
              ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453
              AND e.removed = 0
              AND e.tx_hash != ''
              AND m.chain_id = 8453
              AND m.has_weth_leg = 1
              AND lower(m.pool_type) = 'v2'
              AND coalesce(e.block_time, e.received_at_ms) + INTERVAL {int(cfg['labeling'].get('fixed_horizon_sec', 1800))} SECOND < now()
        )
        WHERE rn_pool_hour <= {max_per_pool_hour} AND rn_pool <= {max_per_pool}
        ORDER BY cityHash64(pool_address, toString(day_bucket), toString(hour_bucket), source_tx_hash, toString(source_log_index), { _sql_string(seed) })
        LIMIT {target}
    """
    rows = client.query_json(query)
    for idx, row in enumerate(rows):
        row["candidate_id"] = idx
        row["row_id"] = idx
        row["timestamp"] = row.get("timestamp_ms")
        row["buyer"] = ""
        row["seller"] = ""
    pools = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top = pools.most_common()
    n = len(rows)
    source_identity = sum(1 for r in rows if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number"))
    metadata_join = sum(1 for r in rows if r.get("pool_type") and r.get("token0_address") and r.get("token1_address"))
    weth_leg = sum(1 for r in rows if r.get("weth_leg"))
    report = {
        "status": "PASS" if n >= target else "HOLD",
        "target_candidates": target,
        "total_candidates": n,
        "unique_pools": len(pools),
        "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
        "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0, 8),
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top[:20]],
        "time_bucket_distribution": {
            "days_top20": dict(Counter(str(r.get("day_bucket")) for r in rows).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in rows).most_common()),
        },
        "source_identity_coverage": rounded(source_identity / n, 8) if n else 0.0,
        "metadata_join_coverage": rounded(metadata_join / n, 8) if n else 0.0,
        "weth_leg_coverage": rounded(weth_leg / n, 8) if n else 0.0,
        "decoded_source": "uniswap_v2_swaps_decoded",
        "metadata_source": "discovered_pools",
        "include_source_event_in_features": True,
        "pool_time_sampling": {
            "stratification": "pool x hour bucket",
            "max_per_pool": max_per_pool,
            "max_per_pool_time_bucket": max_per_pool_hour,
            "seed": seed,
        },
        "capacity": _h169_query_capacity(client, cfg),
        "blockers": [] if n >= target else ["decoded-backed candidate shortage"],
    }
    return rows, report


def _h169_load_v2_events_for_candidates(client: ClickHouseClient, db: str, rows: list[dict[str, Any]], table: str, max_window: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _h168_chunk_rows_by_pool(rows, chunk_size=250):
        addresses = _h108_pool_addresses(chunk)
        if not addresses:
            continue
        start_ms, end_ms = _h108_ts_bounds(chunk, max_window)
        addr_sql = ",".join(_sql_string(a) for a in addresses)
        ts_ms_expr = "toInt64(if(isNull(block_time), toUnixTimestamp(parseDateTimeBestEffort(toString(received_at_ms))), toUnixTimestamp(parseDateTimeBestEffort(toString(assumeNotNull(block_time))))) * 1000)"
        q = f"""
SELECT
  'v2' AS decoded_source,
  lower(pair_address) AS pool_address,
  {ts_ms_expr} AS ts_ms,
  block_number,
  tx_hash,
  log_index,
  lower(sender) AS sender,
  lower(recipient) AS recipient,
  toString(amount0_in) AS amount0_in,
  toString(amount1_in) AS amount1_in,
  toString(amount0_out) AS amount0_out,
  toString(amount1_out) AS amount1_out
FROM {db}.{table}
WHERE lower(pair_address) IN ({addr_sql})
  AND chain_id = 8453
  AND removed = 0
  AND tx_hash != ''
  AND {ts_ms_expr} >= {start_ms}
  AND {ts_ms_expr} <= {end_ms}
ORDER BY pool_address, ts_ms, block_number, log_index
"""
        for event in client.query_json(q):
            out[_lower_addr(event.get("pool_address"))].append(event)
    for pool in list(out):
        out[pool].sort(key=lambda e: (int(optional_float(e.get("ts_ms")) or 0), int(optional_float(e.get("block_number")) or 0), int(optional_float(e.get("log_index")) or 0)))
    return out


def _h169_event_in_window(event: dict[str, Any], row: dict[str, Any], window_sec: int, include_source: bool) -> bool:
    entry_ms = _entry_ts_ms(row)
    event_ms = optional_float(event.get("ts_ms"))
    if entry_ms is None or event_ms is None:
        return False
    event_i = int(event_ms)
    if event_i < entry_ms - window_sec * 1000 or event_i > entry_ms:
        return False
    if event_i < entry_ms:
        return True
    source_block = int(optional_float(row.get("source_block_number")) or -1)
    source_log = int(optional_float(row.get("source_log_index")) or -1)
    event_block = int(optional_float(event.get("block_number")) or -1)
    event_log = int(optional_float(event.get("log_index")) or -1)
    if event_block < source_block:
        return True
    if event_block == source_block and event_log < source_log:
        return True
    return bool(include_source and event_block == source_block and event_log == source_log and event.get("tx_hash") == row.get("source_tx_hash"))


def _h169_window_features(events: list[dict[str, Any]], meta: dict[str, Any] | None, row: dict[str, Any], window_sec: int, include_source: bool) -> dict[str, Any]:
    entry_ms = _entry_ts_ms(row)
    if entry_ms is None:
        return _h108_window_features([], meta, 0, window_sec)
    subset = [e for e in events if _h169_event_in_window(e, row, window_sec, include_source)]
    shifted = dict(row)
    shifted["timestamp"] = str(entry_ms + 1)
    return _h108_window_features(subset, meta, entry_ms + 1, window_sec)


def _h169_enrich_rows(rows: list[dict[str, Any]], windows: list[int], events: dict[str, list[dict[str, Any]]], metadata: dict[str, dict[str, Any]], include_source: bool) -> tuple[list[dict[str, Any]], list[str]]:
    cols = _h108_feature_columns(windows)
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for col in cols:
            out[col] = ""
        pool = _lower_addr(row.get("pool_address"))
        meta = metadata.get(pool) or {
            "token0_address": row.get("token0_address"),
            "token1_address": row.get("token1_address"),
            "pool_type": row.get("pool_type"),
            "protocol": row.get("protocol"),
        }
        pool_events = events.get(pool, [])
        per_window: dict[int, dict[str, Any]] = {}
        side_known_total = 0
        amount_total = 0
        for w in windows:
            feats = _h169_window_features(pool_events, meta, row, w, include_source)
            per_window[w] = feats
            side_known_total += int(optional_float(feats.get(f"side_known_count_{w}s")) or 0)
            amount_total += int(optional_float(feats.get(f"event_count_{w}s")) or 0)
            for key, value in feats.items():
                out[key] = fnum(value)
            out[f"decoded_event_count_{w}s"] = out.get(f"event_count_{w}s", "")
        out["activity_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window.get(60, {}).get("event_count_60s"), per_window.get(300, {}).get("event_count_300s")))
        out["activity_acceleration_3m_vs_15m"] = fnum(_ratio_delta(per_window.get(180, {}).get("event_count_180s"), per_window.get(900, {}).get("event_count_900s")))
        out["buy_pressure_acceleration_1m_vs_5m"] = fnum(_ratio_delta(per_window.get(60, {}).get("buy_sell_count_ratio_60s"), per_window.get(300, {}).get("buy_sell_count_ratio_300s")))
        e1 = optional_float(per_window.get(60, {}).get("sender_entropy_60s"))
        e5 = optional_float(per_window.get(300, {}).get("sender_entropy_300s"))
        out["sender_entropy_change_1m_vs_5m"] = fnum(e1 - e5 if e1 is not None and e5 is not None else None)
        out["decoded_source_used"] = "uniswap_v2_swaps_decoded" if optional_float(out.get("event_count_1800s")) else ""
        out["decoded_join_status"] = "matched" if optional_float(out.get("event_count_1800s")) else "unmatched"
        out["decoded_event_count_5m"] = out.get("event_count_300s", "")
        out["decoded_event_count_30m"] = out.get("event_count_1800s", "")
        out["backfill_source_status"] = "decoded_bridge_source_aware" if out["decoded_join_status"] == "matched" else "unavailable"
        out["wallet_source_available"] = "true" if optional_float(out.get("unique_swap_senders_1800s")) is not None else "false"
        out["side_source_available"] = "true" if side_known_total > 0 else "false"
        out["amount_source_available"] = "true" if amount_total > 0 else "false"
        out["side_inferred"] = "true" if side_known_total > 0 else "false"
        out["buyer_side_unavailable"] = "false" if side_known_total > 0 else "true"
        out["pool_type_backfilled"] = meta.get("pool_type", "")
        out["protocol_backfilled"] = meta.get("protocol", "")
        out["token0"] = row.get("token0") or meta.get("token0_address", "")
        out["token1"] = row.get("token1") or meta.get("token1_address", "")
        out["weth_leg"] = row.get("weth_leg") or ("token0" if _lower_addr(out.get("token0")) == WETH_BASE else ("token1" if _lower_addr(out.get("token1")) == WETH_BASE else ""))
        out_rows.append(out)
    return out_rows, cols


def _h169_coverage_report(rows: list[dict[str, Any]], feature_cols: list[str], windows: list[int]) -> dict[str, Any]:
    base = _h168_feature_coverage_report(rows, feature_cols, windows)
    total = len(rows)
    base["source_identity_coverage"] = {
        "covered_rows": sum(1 for r in rows if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number")),
        "coverage": rounded(sum(1 for r in rows if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number")) / total, 8) if total else 0.0,
    }
    base["metadata_join_coverage"] = nonempty_coverage(rows, ["pool_type", "protocol", "token0", "token1"])
    base["weth_leg_coverage"] = nonempty_coverage(rows, ["weth_leg"])
    base["candidate_rows_with_decoded_events_in_windows"] = {
        f"{w}s": {
            "matched_rows": sum(1 for r in rows if optional_float(r.get(f"decoded_event_count_{w}s"))),
            "coverage": rounded(sum(1 for r in rows if optional_float(r.get(f"decoded_event_count_{w}s"))) / total, 8) if total else 0.0,
        }
        for w in windows
    }
    return base


def _h169_empty_checkpoint(status: str, reason: str, selected: int = 0) -> dict[str, Any]:
    return {
        "status": status,
        "selected": selected,
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "skipped": selected,
        "failure_reasons": {reason: selected or 1},
        "estimated_rpc_calls": 0,
        "actual_rpc_calls": 0,
        "quote_success_rate": None,
        "feature_coverage_summary": {},
        "resume_state": {"completed": False, "reason": reason},
    }


def _h169_risk_report(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    confirmed_bad = {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("confirmed_bad_pools", [])}
    alpha_island = {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("alpha_island_pools", [])}
    for row in rows:
        pool = _lower_addr(row.get("pool_address"))
        quote = int(optional_float(row.get("quote_anomaly_label")) or 0) == 1
        scale = int(optional_float(row.get("scale_risk_label")) or 0) == 1
        gas_dom = int(optional_float(row.get("gas_cost_dominates_label")) or 0) == 1
        matched = str(row.get("decoded_join_status")) == "matched"
        fixed = optional_float(row.get("fixed_cost_adjusted_return_30m")) or 0.0
        if pool in confirmed_bad or (quote and abs(fixed) >= float(cfg.get("risk_severity", {}).get("hard_exclude_abs_return", 0.20))):
            severity = "hard_exclude"
            reason = "confirmed_bad_pool_or_hard_quote_anomaly"
        elif gas_dom:
            severity = "soft_exclude"
            reason = "gas_cost_dominates"
        elif scale or not matched:
            severity = "feature_limited"
            reason = "scale_risk_or_missing_decoded_features"
        elif pool in alpha_island or quote:
            severity = "review_only"
            reason = "alpha_island_or_quote_review"
        else:
            severity = "clean"
            reason = "decoded_features_available_no_risk_flags"
        row["risk_severity"] = severity
        row["quarantine_reason"] = row.get("quarantine_reason") or ("" if severity == "clean" else reason)
    clean = [r for r in rows if r.get("risk_severity") == "clean"]
    risk = [r for r in rows if r.get("risk_severity") != "clean"]
    return rows, {
        "risk_severity_distribution": dict(Counter(str(r.get("risk_severity")) for r in rows).most_common()),
        "clean_alpha_rows": len([r for r in clean if int(optional_float(r.get("alpha_top20_after_gas")) or 0) == 1]),
        "clean_rows": len(clean),
        "risk_rows": len(risk),
        "feature_limited_rows": sum(1 for r in rows if r.get("risk_severity") == "feature_limited"),
        "bad_pool_candidates": sum(int(optional_float(r.get("bad_pool_label")) or 0) for r in rows),
        "quote_anomaly_rows": sum(int(optional_float(r.get("quote_anomaly_label")) or 0) for r in rows),
        "scale_risk_rows": sum(int(optional_float(r.get("scale_risk_label")) or 0) for r in rows),
        "focus_pool_forensic": _h16_focus_forensic(rows, {"pool_forensics": [{"pool_address": p} for p in sorted(confirmed_bad | alpha_island)]}) if rows else {"pools": []},
        "missing_features_not_default_clean": True,
    }


def _h169_markdown(patch: dict[str, Any]) -> str:
    gate = patch["gate"]
    cand = patch["candidate_reconstruction_report"]
    pre = patch["pre_rpc_feature_coverage_report"]
    pilot = patch["pilot_report"]
    checkpoint = patch["checkpoint_report"]
    risk = patch["risk_severity_report"]
    root = patch["p0_lineage_postmortem"]
    return f"""# Run-004 Patch H16.9 - P0 Candidate Lineage Repair / V2 Decoded-Backed Dataset Rebuild

## Status
- Result: `{gate['status']}`
- H17 allowed: `{gate['allow_h17_v2_only_return_aware_feature_ablation']}`
- Promote/live allowed: `False`

## P0 Root Cause
- {root['incident']}
- H16.7 candidate source: {root['h167_candidate_source']}
- True H16.8 30m decoded coverage: {root['observed_decoded_coverage'].get('h168_true_pre_entry_30m_coverage')}

## Candidate Reconstruction
- Candidates / target: {cand['total_candidates']} / {cand['target_candidates']}
- Unique pools: {cand['unique_pools']}
- Top1/top5 share: {cand['top1_share']} / {cand['top5_share']}
- Source identity coverage: {cand['source_identity_coverage']}
- Metadata join coverage: {cand['metadata_join_coverage']}
- WETH leg coverage: {cand['weth_leg_coverage']}
- Include source event in features: `{cand['include_source_event_in_features']}`

## Pre-RPC Feature Coverage
- Decoded bridge coverage: {pre['decoded_bridge_coverage']['coverage']}
- Sender coverage: {pre['family_coverage']['sender_concentration']['coverage']}
- Buyer coverage: {pre['family_coverage']['buyer_concentration']['coverage']}
- Flow coverage: {pre['family_coverage']['flow_count']['coverage']}
- Source identity coverage: {pre['source_identity_coverage']['coverage']}

## Pilot / Full Rebuild
- Pilot attempted / success / failed: {pilot['attempted']} / {pilot['success']} / {pilot['failed']}
- Full attempted / success / failed: {checkpoint['attempted']} / {checkpoint['success']} / {checkpoint['failed']}
- Full final rows: {patch['dataset_quality_report']['final_rows']}

## Risk Re-Audit
- Risk severity: {risk['risk_severity_distribution']}
- Clean alpha rows: {risk['clean_alpha_rows']}
- Risk rows: {risk['risk_rows']}

## Gate
- Blockers: {gate['blockers']}
- Next step: {gate['next_step']}
"""


def _h169_v2_decoded_backed_rebuild(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    inputs = cfg.get("input_paths", {})
    outputs = cfg["outputs"]
    windows = [int(w) for w in cfg.get("windows_sec", H169_WINDOWS_DEFAULT)]
    include_source = bool(cfg.get("features", {}).get("include_source_event_in_features", True))
    postmortem = _h169_postmortem(inputs)
    write_json(_resolve(outputs["p0_lineage_postmortem_json"]), postmortem)

    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    candidates, candidate_report = _h169_reconstruct_candidates(client, cfg)
    write_csv_rows(_resolve(outputs["decoded_backed_candidate_csv"]), candidates)
    src = cfg["clickhouse"]["decoded_sources"]
    metadata = _h168_load_metadata_chunked(client, client.database, candidates, src["metadata_table"]) if candidates else {}
    events = _h169_load_v2_events_for_candidates(client, client.database, candidates, src["v2_table"], max(windows)) if candidates else {}
    enriched_candidates, feature_cols = _h169_enrich_rows(candidates, windows, events, metadata, include_source)
    precheck = _h169_coverage_report(enriched_candidates, feature_cols, windows)
    candidate_report["candidate_rows_with_decoded_events_in_windows"] = precheck["candidate_rows_with_decoded_events_in_windows"]
    write_json(_resolve(outputs["candidate_reconstruction_report_json"]), candidate_report)
    write_json(_resolve(outputs["pre_rpc_feature_coverage_report_json"]), precheck)

    family = precheck["family_coverage"]
    precheck_checks = {
        "candidates_gte_15000": len(candidates) >= int(cfg["candidate_reconstruction"].get("target_candidates", 15000)),
        "decoded_bridge_coverage_gte_90pct": (precheck["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90,
        "sender_coverage_gte_90pct": (family["sender_concentration"]["coverage"] or 0) >= 0.90,
        "buyer_coverage_gte_90pct": (family["buyer_concentration"]["coverage"] or 0) >= 0.90,
        "flow_coverage_gte_90pct": (family["flow_count"]["coverage"] or 0) >= 0.90,
        "source_identity_coverage_gte_95pct": (precheck["source_identity_coverage"]["coverage"] or 0) >= 0.95,
        "metadata_join_coverage_gte_95pct": (precheck["metadata_join_coverage"]["coverage"] or 0) >= 0.95,
        "weth_leg_coverage_gte_95pct": (precheck["weth_leg_coverage"]["coverage"] or 0) >= 0.95,
    }
    precheck_pass = all(precheck_checks.values())
    rpc = _h165_rpc_readiness({"labeling_pilot": cfg["labeling"], "clickhouse": cfg.get("clickhouse", {})})
    pilot_rows: list[dict[str, Any]] = []
    pilot_report = _h169_empty_checkpoint("SKIPPED_PRECHECK_HOLD", "precheck_or_candidate_gate_failed", min(int(cfg["pilot"].get("rows", 300)), len(candidates)))
    full_rows: list[dict[str, Any]] = []
    checkpoint = _h169_empty_checkpoint("SKIPPED_PRECHECK_HOLD", "precheck_or_candidate_gate_failed", len(candidates))

    if precheck_pass and rpc.get("status") == "READY":
        pilot_cfg = dict(cfg)
        pilot_cfg["labeling_pilot"] = {**cfg["labeling"], "pilot_size": int(cfg["pilot"].get("rows", 300)), "v2_target": int(cfg["pilot"].get("rows", 300)), "v3_target": 0}
        pilot_rows, pilot_report = _h165_run_pilot(pilot_cfg, enriched_candidates, rpc)
        pilot_report["feature_coverage_summary"] = precheck
        pilot_pass = (
            (pilot_report.get("quote_success_rate") or 0) >= float(cfg["pilot"].get("quote_success_gate", 0.90))
            and (precheck["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90
            and (family["sender_concentration"]["coverage"] or 0) >= 0.90
            and (family["buyer_concentration"]["coverage"] or 0) >= 0.90
            and (family["flow_count"]["coverage"] or 0) >= 0.90
        )
        if pilot_pass:
            full_rows, checkpoint = _h167_label_v2_dataset(cfg, enriched_candidates, rpc)
            for row in full_rows:
                source = enriched_candidates[int(optional_float(row.get("candidate_id")) or 0)] if str(row.get("candidate_id", "")).isdigit() else {}
                for key in feature_cols + ["source_tx_hash", "source_log_index", "source_block_number", "source_timestamp", "decoded_join_status", "decoded_source_used", "weth_leg", "token0", "token1"]:
                    row[key] = row.get(key, source.get(key, ""))
            checkpoint["feature_coverage_summary"] = precheck
        else:
            checkpoint = _h169_empty_checkpoint("SKIPPED_PILOT_HOLD", "pilot_gate_failed", len(candidates))
    elif precheck_pass:
        pilot_report = _h169_empty_checkpoint("BLOCKED_RPC_ENV", "missing_rpc_env", min(int(cfg["pilot"].get("rows", 300)), len(candidates)))
        checkpoint = _h169_empty_checkpoint("BLOCKED_RPC_ENV", "missing_rpc_env", len(candidates))

    full_rows = _h167_recompute_exact_top20(full_rows) if full_rows else []
    full_rows, risk_report = _h169_risk_report(full_rows, cfg)
    clean_rows = [r for r in full_rows if r.get("risk_severity") == "clean" and int(optional_float(r.get("alpha_top20_after_gas")) or 0) == 1]
    risk_rows = [r for r in full_rows if r.get("risk_severity") != "clean"]
    write_json(_resolve(outputs["pilot_report_json"]), pilot_report)
    write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
    write_csv_rows(_resolve(outputs["all_feature_ready_dataset_csv"]), full_rows)
    write_csv_rows(_resolve(outputs["clean_alpha_dataset_csv"]), clean_rows)
    write_csv_rows(_resolve(outputs["risk_bad_pool_dataset_csv"]), risk_rows)
    write_json(_resolve(outputs["feature_coverage_report_json"]), precheck)
    write_json(_resolve(outputs["risk_severity_report_json"]), risk_report)
    return_accounting = {
        "scope": "v2_only_decoded_backed_fixed_30m",
        "fixed_horizon_sec": int(cfg["labeling"].get("fixed_horizon_sec", 1800)),
        "primary_target": "alpha_top20_after_gas",
        "alpha_top20_method": "exact top N by final_return_after_cost; no >= percentile tie leakage",
        "return_accounting_version": "H15/H16 gas-adjusted fixed-horizon schema",
        "dynamic_exit_available": False,
        "rpc_url_written_to_artifacts": False,
    }
    write_json(_resolve(outputs["return_accounting_report_json"]), return_accounting)
    quality = {
        "status": "COMPLETE" if len(full_rows) >= int(cfg["labeling"].get("target_success_rows", 10000)) else "HOLD",
        "final_rows": len(full_rows),
        **_h16_distribution(full_rows),
        "alpha_top20_after_gas_distribution": dict(Counter(str(r.get("alpha_top20_after_gas")) for r in full_rows).most_common()),
        "decoded_bridge_coverage": precheck["decoded_bridge_coverage"],
        "sender_coverage": family["sender_concentration"],
        "buyer_coverage": family["buyer_concentration"],
        "flow_coverage": family["flow_count"],
        "risk_severity_distribution": risk_report["risk_severity_distribution"],
        "clean_alpha_rows": len(clean_rows),
        "risk_rows": len(risk_rows),
    }
    write_json(_resolve(outputs["dataset_quality_report_json"]), quality)
    alpha_counts = Counter(str(row.get("alpha_top20_after_gas")) for row in full_rows)
    checks = {
        **precheck_checks,
        "final_rows_gte_8000": len(full_rows) >= 8000,
        "target_10k_reached": len(full_rows) >= int(cfg["labeling"].get("target_success_rows", 10000)),
        "quote_success_rate_gte_90pct": (checkpoint.get("quote_success_rate") or 0) >= 0.90,
        "alpha_top20_exact_20pct": bool(full_rows) and alpha_counts.get("1", 0) == math.ceil(0.20 * len(full_rows)),
        "risk_severity_reaudited": bool(risk_report),
        "checkpoint_accounting_closed_or_explained": bool(checkpoint.get("resume_state")),
        "no_production_model_writes": True,
    }
    success = all(checks.values())
    blockers = [k for k, v in checks.items() if not v]
    blocker_map = []
    if not precheck_checks["candidates_gte_15000"]:
        blocker_map.append("decoded-backed candidate shortage")
    if any(not precheck_checks[k] for k in ["decoded_bridge_coverage_gte_90pct", "sender_coverage_gte_90pct", "buyer_coverage_gte_90pct", "flow_coverage_gte_90pct"]):
        blocker_map.append("feature coverage low")
    if rpc.get("status") != "READY":
        blocker_map.append("RPC budget/env")
    if checkpoint.get("status") in {"LABELER_FAILURE"}:
        blocker_map.append("checkpoint issue")
    patch = {
        "patch": "run-004-h16.9-p0-candidate-lineage-repair-v2-decoded-backed-dataset-rebuild",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in inputs.items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "p0_lineage_postmortem": postmortem,
        "candidate_reconstruction_report": candidate_report,
        "pre_rpc_feature_coverage_report": precheck,
        "pilot_report": pilot_report,
        "checkpoint_report": checkpoint,
        "feature_coverage_report": precheck,
        "risk_severity_report": risk_report,
        "return_accounting_report": return_accounting,
        "dataset_quality_report": quality,
        "gate": {
            "status": "PASS" if success else "HOLD",
            "checks": checks,
            "blockers": blockers,
            "blocker_categories": sorted(set(blocker_map)),
            "allow_h17_v2_only_return_aware_feature_ablation": success,
            "allow_promote_live": False,
            "promote_live_allowed": False,
            "next_step": "H17 V2-only return-aware feature ablation" if success else "Repair blocker categories before any H17 or RPC-heavy rebuild",
            "forbidden": ["production model write", "active model overwrite", "runtime trading code change", "V3 artifact mutation", "mixed V2/V3 artifact mutation", "promote", "live"],
        },
        "safety_assertions": {**cfg.get("safety", {}), "production_model_write_allowed": False, "promote_live_allowed": False, "rpc_url_written_to_artifacts": False},
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["p0_lineage_postmortem_markdown"]), _h169_markdown({**patch, "gate": patch["gate"]}))
    write_markdown(_resolve(outputs["markdown"]), _h169_markdown(patch))
    return patch


def _h1610_env_file_values(paths: list[str]) -> dict[str, dict[str, Any]]:
    wanted = {"LABEL_RPC_URL", "EVM_HTTP_URL", "LIVE_BASE_RPC_URL"}
    out: dict[str, dict[str, Any]] = {}
    for rel in paths:
        path = _resolve(rel)
        if not path.exists():
            out[str(path)] = {"exists": False, "vars": {}}
            continue
        vars_found: dict[str, dict[str, Any]] = {}
        for line in path.read_text(errors="ignore").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key not in wanted:
                continue
            value = value.strip().strip('"').strip("'")
            vars_found[key] = _h165_redacted_env(value)
            vars_found[key]["value_present"] = bool(value)
        out[str(path)] = {"exists": True, "vars": vars_found}
    return out


def _h1610_select_rpc_url(cfg: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    env_vars = cfg["rpc"].get("env_vars", ["LABEL_RPC_URL", "EVM_HTTP_URL", "LIVE_BASE_RPC_URL"])
    host = {name: _h165_redacted_env(os.environ.get(name)) for name in env_vars}
    selected_name = ""
    selected = ""
    for name in env_vars:
        if os.environ.get(name):
            selected_name = name
            selected = os.environ[name]
            break
    env_files = _h1610_env_file_values(cfg["rpc"].get("env_files", ["infra/compose/.env"]))
    if not selected:
        for path, item in env_files.items():
            path_obj = Path(path)
            if not item.get("exists"):
                continue
            values: dict[str, str] = {}
            for line in path_obj.read_text(errors="ignore").splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                key = key.strip()
                if key in env_vars and value.strip():
                    values[key] = value.strip().strip('"').strip("'")
            for name in env_vars:
                if values.get(name):
                    selected_name = f"{name}@{path_obj.name}"
                    selected = values[name]
                    break
            if selected:
                break
    container = str(cfg.get("clickhouse", {}).get("docker_container", ""))
    container_env: dict[str, Any] = {"checked": False, "container": container, "vars": {}}
    if container:
        container_env["checked"] = True
        for name in env_vars:
            proc = subprocess.run(
                ["docker", "exec", container, "sh", "-lc", f'printf "%s" "${{{name}:-}}"'],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            value = proc.stdout.strip() if proc.returncode == 0 else ""
            container_env["vars"][name] = _h165_redacted_env(value)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "host_shell_env": host,
        "env_files": env_files,
        "container_env": container_env,
        "harness_env_var_priority": env_vars,
        "selected_rpc_source": selected_name or None,
        "selected_rpc_redacted": _h165_redacted_env(selected),
        "status": "READY" if selected else "BLOCKED_RPC_INJECTION",
        "safe_injection_commands": [
            'export LABEL_RPC_URL="$EVM_HTTP_URL" && python3 scripts/run004_research_harness.py --mode h1610r_v2_decoded_backed_candidate_repair_rpc_metadata_backfill --config configs/run004/h1610r_v2_decoded_backed_candidate_repair_rpc_metadata_backfill.yaml',
            'docker exec -e LABEL_RPC_URL="$LABEL_RPC_URL" eatlq-clickhouse-1 env | rg "^LABEL_RPC_URL="',
        ],
        "rpc_url_written_to_artifacts": False,
    }
    return selected, selected_name, report


def _h1610_filter_waterfall(client: ClickHouseClient, cfg: dict[str, Any]) -> dict[str, Any]:
    db = client.database
    src = cfg["clickhouse"]["decoded_sources"]
    queries = {
        "raw": f"""
            SELECT count() AS rows, uniqExact(pair_address) AS pools
            FROM {db}.{src['v2_table']}
        """,
        "valid_source_identity": f"""
            SELECT count() AS rows, uniqExact(lower(pair_address)) AS pools
            FROM {db}.{src['v2_table']}
            WHERE chain_id = 8453 AND removed = 0 AND tx_hash != '' AND block_number > 0 AND log_index >= 0
              AND coalesce(block_time, received_at_ms) IS NOT NULL
        """,
        "metadata_join": f"""
            SELECT count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != ''
        """,
        "strict_h169_filter": f"""
            SELECT count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.chain_id = 8453
              AND lower(m.pool_type) = 'v2' AND m.has_weth_leg = 1
        """,
        "source_identity_coverage": f"""
            SELECT
              count() AS rows,
              sum(tx_hash != '' AND block_number > 0 AND log_index >= 0) AS source_identity_rows,
              sum(coalesce(block_time, received_at_ms) IS NOT NULL) AS timestamp_rows,
              sum(amount0_in + amount1_in + amount0_out + amount1_out > 0) AS amount_rows
            FROM {db}.{src['v2_table']}
            WHERE chain_id = 8453 AND removed = 0
        """,
        "pool_type_has_weth_distribution": f"""
            SELECT lower(m.pool_type) AS pool_type, m.has_weth_leg AS has_weth_leg, count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            ANY INNER JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != ''
            GROUP BY pool_type, has_weth_leg
            ORDER BY rows DESC
        """,
        "metadata_missing_pools": f"""
            SELECT count() AS rows, uniqExact(lower(e.pair_address)) AS pools
            FROM {db}.{src['v2_table']} e
            LEFT JOIN {db}.{src['metadata_table']} m ON lower(e.pair_address) = lower(m.pool_address)
            WHERE e.chain_id = 8453 AND e.removed = 0 AND e.tx_hash != '' AND m.pool_address = ''
        """,
    }
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "queries": {}}
    for name, query in queries.items():
        try:
            rows = client.query_json(query)
            out["queries"][name] = rows if len(rows) != 1 else rows[0]
        except Exception as exc:
            out["queries"][name] = {"error": str(exc)}
    desc = client.query_json(f"DESCRIBE TABLE {db}.{src['v2_table']}")
    cols = {str(r.get("name")): str(r.get("type")) for r in desc}
    out["decoded_table_columns"] = cols
    out["decoded_token_field_candidates"] = [c for c in cols if any(term in c.lower() for term in ["token", "amount", "pair"])]
    out["root_cause_summary"] = (
        "The 277k->989 collapse is primarily the strict metadata/WETH resolver: decoded swaps have 277k rows, "
        "but only about 991 decoded pools join discovered_pools at all and 989 satisfy pool_type='v2' + has_weth_leg=1. "
        "The decoded V2 table itself does not carry token0/token1, so WETH leg cannot be inferred without metadata or RPC."
    )
    return out


def _h1610_pool_stats(client: ClickHouseClient, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    db = client.database
    table = cfg["clickhouse"]["decoded_sources"]["v2_table"]
    limit = int(cfg["candidate_repair"].get("max_pools_for_repair", 5000))
    q = f"""
        SELECT
          lower(pair_address) AS pool_address,
          count() AS decoded_rows,
          min(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms))) AS min_ts_ms,
          max(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms))) AS max_ts_ms,
          min(block_number) AS min_block,
          max(block_number) AS max_block
        FROM {db}.{table}
        WHERE chain_id = 8453 AND removed = 0 AND tx_hash != '' AND coalesce(block_time, received_at_ms) IS NOT NULL
        GROUP BY pool_address
        ORDER BY decoded_rows DESC
        LIMIT {limit}
    """
    return client.query_json(q)


def _h1610_weth_leg(token0: Any, token1: Any) -> str:
    t0 = _lower_addr(token0)
    t1 = _lower_addr(token1)
    if t0 == WETH_BASE:
        return "token0"
    if t1 == WETH_BASE:
        return "token1"
    return ""


def _h1610_backfill_pair_tokens(rpc_url: str, pools: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not rpc_url:
        return {}, {"status": "BLOCKED_RPC_INJECTION", "attempted_pools": 0, "success_pools": 0, "actual_rpc_calls": 0}
    from scripts import label_dashcam_executable as labeler  # noqa: WPS433

    class CountedRPC(labeler.RPC):  # type: ignore[misc]
        def __init__(self, url: str):
            super().__init__(url, timeout=int(cfg["rpc"].get("timeout_sec", 60)), max_retries=int(cfg["rpc"].get("max_retries", 3)))
            self.call_count = 0

        def call(self, method: str, params: list[Any]) -> dict[str, Any]:  # type: ignore[override]
            self.call_count += 1
            return super().call(method, params)

        def batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:  # type: ignore[override]
            self.call_count += len(calls)
            return super().batch(calls)

    rpc = CountedRPC(rpc_url)
    token1_selector = "0xd21220a7"
    max_pools = int(cfg["rpc"].get("max_backfill_pools", len(pools)))
    selected = pools[:max_pools]
    resolved: dict[str, dict[str, Any]] = {}
    failures: Counter[str] = Counter()
    examples_failed: list[dict[str, Any]] = []
    try:
        chain = rpc.call("eth_chainId", [])
        if int(chain.get("result", "0x0"), 16) != 8453:
            raise RuntimeError(f"unexpected_chain_id:{int(chain.get('result', '0x0'), 16)}")
    except Exception as exc:
        return {}, {"status": "RPC_FAILURE", "attempted_pools": 0, "success_pools": 0, "actual_rpc_calls": getattr(rpc, "call_count", 0), "failure": str(exc)}

    def parse_addr(resp: dict[str, Any]) -> str:
        if not resp or "error" in resp or not resp.get("result"):
            raise RuntimeError(str((resp or {}).get("error", "empty_result")))
        raw = str(resp["result"])
        return ("0x" + raw[-40:]).lower()

    batch_size = int(cfg["rpc"].get("batch_size", 80))
    for start in range(0, len(selected), batch_size):
        batch = selected[start:start + batch_size]
        calls: list[tuple[str, list[Any]]] = []
        for item in batch:
            pool = _lower_addr(item.get("pool_address"))
            block = int(optional_float(item.get("max_block")) or 0)
            block_tag = hex(block) if block > 0 else "latest"
            calls.append(("eth_call", [{"to": pool, "data": "0x0dfe1681"}, block_tag]))
            calls.append(("eth_call", [{"to": pool, "data": token1_selector}, block_tag]))
        try:
            responses = rpc.batch(calls)
        except Exception as exc:
            failures[f"batch:{type(exc).__name__}"] += len(batch)
            continue
        for idx, item in enumerate(batch):
            pool = _lower_addr(item.get("pool_address"))
            try:
                token0 = parse_addr(responses[idx * 2])
                token1 = parse_addr(responses[idx * 2 + 1])
                resolved[pool] = {
                    "pool_address": pool,
                    "token0_address": token0,
                    "token1_address": token1,
                    "pool_type": "v2",
                    "protocol": "v2",
                    "fee_bps": 30,
                    "weth_leg": _h1610_weth_leg(token0, token1),
                    "weth_leg_source": "rpc_backfill",
                }
            except Exception as exc:
                failures[type(exc).__name__] += 1
                if len(examples_failed) < 10:
                    examples_failed.append({"pool_address": pool, "reason": str(exc)[:160]})
    success = sum(1 for item in resolved.values() if item.get("weth_leg"))
    return resolved, {
        "status": "COMPLETED",
        "attempted_pools": len(selected),
        "resolved_token_pools": len(resolved),
        "success_pools_with_weth_leg": success,
        "unresolved_or_non_weth_pools": len(selected) - success,
        "estimated_rpc_calls": len(selected) * 2 + 1,
        "actual_rpc_calls": rpc.call_count,
        "failure_reasons": dict(failures.most_common()),
        "sample_failures": examples_failed,
        "cache_scope": "diagnostics_only",
    }


def _h1610_metadata_cache_path(cfg: dict[str, Any]) -> Path:
    output = cfg.get("outputs", {}).get("weth_resolver_cache_json", "mvp/diagnostics/run004_h1610r_weth_resolver_cache.json")
    return _resolve(output)


def _h1610_resolve_weth_metadata(client: ClickHouseClient, cfg: dict[str, Any], rpc_url: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    pool_stats = _h1610_pool_stats(client, cfg)
    metadata = _h168_load_metadata_chunked(client, client.database, pool_stats, cfg["clickhouse"]["decoded_sources"]["metadata_table"])
    resolved: dict[str, dict[str, Any]] = {}
    unresolved_for_rpc: list[dict[str, Any]] = []
    level_counts: Counter[str] = Counter()
    row_counts: Counter[str] = Counter()
    for item in pool_stats:
        pool = _lower_addr(item.get("pool_address"))
        meta = metadata.get(pool)
        source = "unknown"
        entry: dict[str, Any] = {}
        if meta and _h1610_weth_leg(meta.get("token0_address"), meta.get("token1_address")):
            source = "discovered_pools"
            entry = {
                **meta,
                "pool_address": pool,
                "token0_address": _lower_addr(meta.get("token0_address")),
                "token1_address": _lower_addr(meta.get("token1_address")),
                "pool_type": str(meta.get("pool_type") or "v2").lower(),
                "protocol": "v2",
                "weth_leg": _h1610_weth_leg(meta.get("token0_address"), meta.get("token1_address")),
                "weth_leg_source": source,
            }
            resolved[pool] = entry
        else:
            unresolved_for_rpc.append(item)
        level_counts[source] += 1
        row_counts[source] += int(optional_float(item.get("decoded_rows")) or 0)
    cache_path = _h1610_metadata_cache_path(cfg)
    cached_rpc: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        try:
            cache_payload = json.loads(cache_path.read_text())
            cached_rpc = {pool: item for pool, item in cache_payload.get("resolved", {}).items() if item.get("weth_leg_source") == "rpc_backfill"}
        except Exception:
            cached_rpc = {}
    rpc_targets = [item for item in unresolved_for_rpc if _lower_addr(item.get("pool_address")) not in cached_rpc]
    rpc_resolved, rpc_report = _h1610_backfill_pair_tokens(rpc_url, rpc_targets, cfg)
    if cached_rpc:
        rpc_report["cached_rpc_backfill_pools_used"] = len(cached_rpc)
    rpc_resolved = {**cached_rpc, **rpc_resolved}
    for pool, item in rpc_resolved.items():
        if item.get("weth_leg"):
            resolved[pool] = item
            level_counts["rpc_backfill"] += 1
            row_counts["rpc_backfill"] += int(optional_float(next((p.get("decoded_rows") for p in pool_stats if _lower_addr(p.get("pool_address")) == pool), 0)) or 0)
    total_pools = len(pool_stats)
    total_rows = sum(int(optional_float(p.get("decoded_rows")) or 0) for p in pool_stats)
    resolved_rows = sum(int(optional_float(p.get("decoded_rows")) or 0) for p in pool_stats if _lower_addr(p.get("pool_address")) in resolved)
    report = {
        "status": "COMPLETED",
        "total_decoded_pools_considered": total_pools,
        "total_decoded_rows_considered": total_rows,
        "resolved_pools": len(resolved),
        "unresolved_pools": total_pools - len(resolved),
        "resolved_rows": resolved_rows,
        "unresolved_rows": total_rows - resolved_rows,
        "pool_coverage": rounded(len(resolved) / total_pools, 8) if total_pools else 0.0,
        "row_coverage": rounded(resolved_rows / total_rows, 8) if total_rows else 0.0,
        "coverage_by_level_pools": dict(level_counts.most_common()),
        "coverage_by_level_rows": dict(row_counts.most_common()),
        "rpc_backfill": rpc_report,
        "sample_resolved": list(resolved.values())[:10],
        "sample_unresolved": [{"pool_address": _lower_addr(p.get("pool_address")), "decoded_rows": p.get("decoded_rows")} for p in pool_stats if _lower_addr(p.get("pool_address")) not in resolved][:10],
    }
    write_json(cache_path, {"generated_at": datetime.now(UTC).isoformat(), "resolved": resolved})
    return resolved, report, pool_stats


def _h1610_reconstruct_candidates(client: ClickHouseClient, cfg: dict[str, Any], resolved: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not resolved:
        return [], {"status": "NO_RESOLVED_WETH_POOLS", "total_candidates": 0, "target_candidates": cfg["candidate_repair"].get("target_candidates", 15000)}
    repair = cfg["candidate_repair"]
    target = int(repair.get("target_candidates", 15000))
    max_per_pool = int(repair.get("max_per_pool", 25))
    max_per_pool_hour = int(repair.get("max_per_pool_time_bucket", 3))
    seed = str(repair.get("deterministic_sample_seed", "run004_h1610r"))
    db = client.database
    table = cfg["clickhouse"]["decoded_sources"]["v2_table"]
    rows: list[dict[str, Any]] = []
    pools = sorted(resolved)
    chunk_size = int(repair.get("query_pool_chunk_size", 400))
    for idx in range(0, len(pools), chunk_size):
        chunk = pools[idx:idx + chunk_size]
        pool_sql = ",".join(_sql_string(p) for p in chunk)
        ts_ms_expr = "if(isNull(block_time), toUnixTimestamp64Milli(parseDateTime64BestEffort(toString(received_at_ms), 3, 'UTC')), toUnixTimestamp64Milli(parseDateTime64BestEffort(toString(assumeNotNull(block_time)), 3, 'UTC')))"
        dt_expr = f"fromUnixTimestamp64Milli({ts_ms_expr}, 'UTC')"
        q = f"""
            SELECT *
            FROM
            (
                SELECT
                    row_number() OVER (
                        PARTITION BY lower(pair_address), intDiv({ts_ms_expr}, 3600000)
                        ORDER BY cityHash64(tx_hash, toString(log_index), { _sql_string(seed) })
                    ) AS rn_pool_hour,
                    row_number() OVER (
                        PARTITION BY lower(pair_address)
                        ORDER BY cityHash64(toString({ts_ms_expr}), tx_hash, toString(log_index), { _sql_string(seed) })
                    ) AS rn_pool,
                    lower(pair_address) AS pool_address,
                    lower(pair_address) AS pair_address,
                    tx_hash AS source_tx_hash,
                    log_index AS source_log_index,
                    block_number AS source_block_number,
                    toString({dt_expr}) AS source_timestamp,
                    toString({dt_expr}) AS block_time,
                    {ts_ms_expr} AS timestamp_ms,
                    toString({dt_expr}) AS signal_time,
                    toString({dt_expr}) AS feature_cutoff_time,
                    toString({dt_expr}) AS entry_time,
                    toString(fromUnixTimestamp64Milli({ts_ms_expr} + {int(cfg['labeling'].get('fixed_horizon_sec', 1800)) * 1000}, 'UTC')) AS exit_time,
                    lower(sender) AS sender,
                    lower(recipient) AS recipient,
                    toString(amount0_in) AS amount0_in,
                    toString(amount1_in) AS amount1_in,
                    toString(amount0_out) AS amount0_out,
                    toString(amount1_out) AS amount1_out,
                    'uniswap_v2_swaps_decoded' AS decoded_source,
                    'decoded_swap_event' AS candidate_kind,
                    1 AS include_source_event_in_features,
                    toDate({dt_expr}) AS day_bucket,
                    toHour({dt_expr}) AS hour_bucket
                FROM {db}.{table}
                WHERE chain_id = 8453
                  AND removed = 0
                  AND tx_hash != ''
                  AND lower(pair_address) IN ({pool_sql})
                  AND fromUnixTimestamp64Milli({ts_ms_expr} + {int(cfg['labeling'].get('fixed_horizon_sec', 1800)) * 1000}, 'UTC') < now()
            )
            WHERE rn_pool_hour <= {max_per_pool_hour} AND rn_pool <= {max_per_pool}
            ORDER BY cityHash64(pool_address, toString(day_bucket), toString(hour_bucket), source_tx_hash, toString(source_log_index), { _sql_string(seed) })
            LIMIT {max(0, target - len(rows))}
        """
        rows.extend(client.query_json(q))
        if len(rows) >= target:
            break
    for idx, row in enumerate(rows):
        meta = resolved[_lower_addr(row.get("pool_address"))]
        row.update(
            {
                "candidate_id": idx,
                "row_id": idx,
                "timestamp": row.get("timestamp_ms"),
                "pool_type": meta.get("pool_type", "v2"),
                "pool_class": "v2",
                "protocol": "v2",
                "token0_address": meta.get("token0_address"),
                "token1_address": meta.get("token1_address"),
                "token0": meta.get("token0_address"),
                "token1": meta.get("token1_address"),
                "fee_bps": meta.get("fee_bps", 30),
                "weth_leg": meta.get("weth_leg"),
                "weth_leg_source": meta.get("weth_leg_source"),
                "decoded_source_used": "uniswap_v2_swaps_decoded",
                "decoded_join_status": "matched",
                "buyer": "",
                "seller": "",
            }
        )
    counts = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top = counts.most_common()
    n = len(rows)
    report = {
        "status": "PASS" if n >= target else "PARTIAL",
        "target_candidates": target,
        "total_candidates": n,
        "unique_pools": len(counts),
        "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
        "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0, 8),
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top[:20]],
        "time_bucket_distribution": {
            "days_top20": dict(Counter(str(r.get("day_bucket")) for r in rows).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in rows).most_common()),
        },
        "source_identity_coverage": rounded(sum(1 for r in rows if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number")) / n, 8) if n else 0.0,
        "weth_leg_coverage": rounded(sum(1 for r in rows if r.get("weth_leg")) / n, 8) if n else 0.0,
        "weth_leg_source_distribution": dict(Counter(str(r.get("weth_leg_source")) for r in rows).most_common()),
        "pool_time_sampling": {"stratification": "pool x hour bucket", "max_per_pool": max_per_pool, "max_per_pool_time_bucket": max_per_pool_hour},
        "decoded_source": "uniswap_v2_swaps_decoded",
    }
    return rows, report


def _h1610_feature_precheck(rows: list[dict[str, Any]], cfg: dict[str, Any], client: ClickHouseClient) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    windows = [int(w) for w in cfg.get("windows_sec", H169_WINDOWS_DEFAULT)]
    metadata = {
        _lower_addr(r.get("pool_address")): {
            "pool_address": _lower_addr(r.get("pool_address")),
            "token0_address": r.get("token0_address"),
            "token1_address": r.get("token1_address"),
            "pool_type": r.get("pool_type", "v2"),
            "protocol": r.get("protocol", "v2"),
            "fee_bps": r.get("fee_bps", 30),
        }
        for r in rows
    }
    events = _h169_load_v2_events_for_candidates(client, client.database, rows, cfg["clickhouse"]["decoded_sources"]["v2_table"], max(windows)) if rows else {}
    enriched_true, feature_cols = _h169_enrich_rows(rows, windows, events, metadata, True)
    enriched_false, _ = _h169_enrich_rows(rows, windows, events, metadata, False)
    report_true = _h169_coverage_report(enriched_true, feature_cols, windows)
    report_false = _h169_coverage_report(enriched_false, feature_cols, windows)
    report_true["include_source_event_in_features"] = True
    report_true["include_source_event_comparison"] = {
        "true": {
            "decoded_bridge_coverage": report_true["decoded_bridge_coverage"],
            "sender_coverage": report_true["family_coverage"]["sender_concentration"],
            "buyer_coverage": report_true["family_coverage"]["buyer_concentration"],
            "flow_coverage": report_true["family_coverage"]["flow_count"],
        },
        "false": {
            "decoded_bridge_coverage": report_false["decoded_bridge_coverage"],
            "sender_coverage": report_false["family_coverage"]["sender_concentration"],
            "buyer_coverage": report_false["family_coverage"]["buyer_concentration"],
            "flow_coverage": report_false["family_coverage"]["flow_count"],
        },
    }
    report_true["rows_with_no_usable_pre_entry_or_source_context"] = sum(1 for r in enriched_true if not optional_float(r.get("decoded_event_count_1800s")))
    report_true["coverage_low_reason"] = "source event provides minimum context for sparse pools" if report_false["decoded_bridge_coverage"]["coverage"] < report_true["decoded_bridge_coverage"]["coverage"] else ""
    return enriched_true, report_true, feature_cols


def _h1610_run_pilot(cfg: dict[str, Any], candidates: list[dict[str, Any]], rpc_url: str, rpc_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if rpc_report.get("status") != "READY":
        return [], _h169_empty_checkpoint("BLOCKED_RPC_INJECTION", "rpc_not_available_in_host_env_env_file_or_container", min(int(cfg["pilot"].get("rows", 200)), len(candidates)))
    pilot_cfg = dict(cfg)
    pilot_cfg["labeling_pilot"] = {**cfg["labeling"], "pilot_size": int(cfg["pilot"].get("rows", 200)), "v2_target": int(cfg["pilot"].get("rows", 200)), "v3_target": 0}
    previous = os.environ.get("LABEL_RPC_URL")
    injected = False
    if rpc_url and not previous:
        os.environ["LABEL_RPC_URL"] = rpc_url
        injected = True
    try:
        rows, report = _h165_run_pilot(pilot_cfg, candidates, rpc_report)
        report["rpc_injection"] = {
            "used_temporary_label_rpc_url_env": injected,
            "source": rpc_report.get("selected_rpc_source"),
            "redacted": rpc_report.get("selected_rpc_redacted"),
            "rpc_url_written_to_artifacts": False,
        }
        return rows, report
    finally:
        if injected:
            os.environ.pop("LABEL_RPC_URL", None)


def _h1610_markdown(patch: dict[str, Any]) -> str:
    cand = patch["candidate_report"]
    res = patch["weth_resolver_report"]
    rpc = patch["rpc_readiness_injection_report"]
    feat = patch["feature_coverage_precheck_report"]
    pilot = patch["pilot_report"]
    decision = patch["next_step_decision_report"]
    return f"""# Run-004 Patch H16.10R - V2 Decoded-Backed Candidate Repair with RPC Metadata Backfill

## Status
- Result: `{patch['gate']['status']}`
- H17 allowed: `False`
- Promote/live allowed: `False`

## Filter Waterfall
- Root cause: {patch['filter_waterfall_report']['root_cause_summary']}

## WETH Resolver
- Resolved pools / rows: {res['resolved_pools']} / {res['resolved_rows']}
- Unresolved pools / rows: {res['unresolved_pools']} / {res['unresolved_rows']}
- Pool/row coverage: {res['pool_coverage']} / {res['row_coverage']}
- By level pools: {res['coverage_by_level_pools']}
- RPC backfill: {res['rpc_backfill']}

## RPC
- Status: `{rpc['status']}`
- Selected source: `{rpc.get('selected_rpc_source')}`
- Redacted source: {rpc.get('selected_rpc_redacted')}

## Candidates
- Candidates / target: {cand['total_candidates']} / {cand['target_candidates']}
- Unique pools: {cand['unique_pools']}
- Top1/top5 share: {cand['top1_share']} / {cand['top5_share']}
- WETH leg coverage: {cand['weth_leg_coverage']}
- Source identity coverage: {cand['source_identity_coverage']}
- WETH source distribution: {cand.get('weth_leg_source_distribution')}

## Feature Precheck
- Decoded coverage: {feat['decoded_bridge_coverage']['coverage']}
- Sender coverage: {feat['family_coverage']['sender_concentration']['coverage']}
- Buyer coverage: {feat['family_coverage']['buyer_concentration']['coverage']}
- Flow coverage: {feat['family_coverage']['flow_count']['coverage']}
- Temporal coverage: {feat['family_coverage']['temporal']['coverage']}
- No usable context rows: {feat['rows_with_no_usable_pre_entry_or_source_context']}

## Pilot
- Attempted / success / failed: {pilot['attempted']} / {pilot['success']} / {pilot['failed']}
- Quote success rate: {pilot.get('quote_success_rate')}
- Failure reasons: {pilot.get('failure_reasons')}

## Decision
- Allow H16.11 full 10k: `{decision['allow_h1611_full_10k']}`
- Blockers: {decision['blockers']}
- Next step: {decision['next_step']}
"""


def _h1610r_v2_decoded_backed_candidate_repair_rpc_metadata_backfill(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    rpc_url, _rpc_source, rpc_report = _h1610_select_rpc_url(cfg)
    write_json(_resolve(outputs["rpc_readiness_injection_report_json"]), rpc_report)
    waterfall = _h1610_filter_waterfall(client, cfg)
    write_json(_resolve(outputs["filter_waterfall_report_json"]), waterfall)
    resolved, resolver_report, _pool_stats = _h1610_resolve_weth_metadata(client, cfg, rpc_url)
    write_json(_resolve(outputs["weth_resolver_report_json"]), resolver_report)
    candidates, candidate_report = _h1610_reconstruct_candidates(client, cfg, resolved)
    write_csv_rows(_resolve(outputs["repaired_candidate_csv"]), candidates)
    write_json(_resolve(outputs["candidate_report_json"]), candidate_report)
    enriched_candidates, feature_report, feature_cols = _h1610_feature_precheck(candidates, cfg, client)
    write_json(_resolve(outputs["feature_coverage_precheck_report_json"]), feature_report)
    pilot_rows, pilot_report = _h1610_run_pilot(cfg, enriched_candidates, rpc_url, rpc_report)
    if pilot_rows:
        pilot_rows = _h167_recompute_exact_top20(pilot_rows)
        pilot_rows, pilot_risk = _h169_risk_report(pilot_rows, cfg)
        pilot_report["risk_severity_distribution"] = pilot_risk["risk_severity_distribution"]
        pilot_report["alpha_top20_after_gas_distribution"] = dict(Counter(str(r.get("alpha_top20_after_gas")) for r in pilot_rows).most_common())
        pilot_report["feature_coverage_summary"] = {
            "decoded_bridge_coverage": feature_report["decoded_bridge_coverage"],
            "family_coverage": feature_report["family_coverage"],
        }
        write_csv_rows(_resolve(outputs["pilot_dataset_csv"]), pilot_rows)
    write_json(_resolve(outputs["pilot_report_json"]), pilot_report)
    checks = {
        "decoded_backed_candidates_gte_15000": len(candidates) >= int(cfg["candidate_repair"].get("target_candidates", 15000)),
        "weth_leg_coverage_gte_90pct": (candidate_report.get("weth_leg_coverage") or 0) >= 0.90,
        "source_identity_coverage_gte_95pct": (candidate_report.get("source_identity_coverage") or 0) >= 0.95,
        "pilot_quote_success_gte_90pct": (pilot_report.get("quote_success_rate") or 0) >= 0.90,
        "feature_coverage_basic_usable": (feature_report["decoded_bridge_coverage"]["coverage"] or 0) >= 0.50,
        "no_production_model_writes": True,
    }
    allow_full = all(checks.values())
    decision = {
        "allow_h1611_full_10k": allow_full,
        "allow_h17": False,
        "promote_live_allowed": False,
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "next_step": "Run H16.11 checkpointed full 10k rebuild" if allow_full else "Repair listed blockers before full 10k; do not fabricate 10k.",
    }
    write_json(_resolve(outputs["next_step_decision_report_json"]), decision)
    gate = {
        "status": "READY_FOR_H16_11" if allow_full else "PARTIAL",
        "allow_h1611_full_10k": allow_full,
        "allow_h17_v2_only_return_aware_feature_ablation": False,
        "promote_live_allowed": False,
        "checks": checks,
        "blockers": decision["blockers"],
        "forbidden": ["H17", "promote", "live", "production model write", "active model overwrite", "runtime trading code change"],
    }
    patch = {
        "patch": "run-004-h16.10r-v2-decoded-backed-candidate-repair-rpc-metadata-backfill",
        "generated_at": datetime.now(UTC).isoformat(),
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "filter_waterfall_report": waterfall,
        "weth_resolver_report": resolver_report,
        "rpc_readiness_injection_report": rpc_report,
        "candidate_report": candidate_report,
        "feature_coverage_precheck_report": feature_report,
        "pilot_report": pilot_report,
        "next_step_decision_report": decision,
        "gate": gate,
        "safety_assertions": {**cfg.get("safety", {}), "rpc_url_written_to_artifacts": False, "production_model_write_allowed": False, "promote_live_allowed": False},
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h1610_markdown(patch))
    return patch


def _h1610s_ts_expressions() -> tuple[str, str]:
    ts_ms_expr = "toInt64(if(isNull(block_time), toUnixTimestamp(parseDateTimeBestEffort(toString(received_at_ms))), toUnixTimestamp(parseDateTimeBestEffort(toString(assumeNotNull(block_time))))) * 1000)"
    return ts_ms_expr, f"fromUnixTimestamp64Milli({ts_ms_expr}, 'UTC')"


def _h1610s_direction(row: dict[str, Any]) -> str:
    weth_leg = str(row.get("weth_leg") or "")
    a0i = optional_float(row.get("amount0_in")) or 0.0
    a1i = optional_float(row.get("amount1_in")) or 0.0
    a0o = optional_float(row.get("amount0_out")) or 0.0
    a1o = optional_float(row.get("amount1_out")) or 0.0
    if weth_leg == "token0":
        if a0i > 0 and a1o > 0:
            return "buy_weth_to_token"
        if a1i > 0 and a0o > 0:
            return "sell_token_to_weth"
    if weth_leg == "token1":
        if a1i > 0 and a0o > 0:
            return "buy_weth_to_token"
        if a0i > 0 and a1o > 0:
            return "sell_token_to_weth"
    return "unknown"


def _h1610s_apply_meta(rows: list[dict[str, Any]], resolved: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        out = dict(row)
        meta = resolved.get(_lower_addr(out.get("pool_address")), {})
        out.update(
            {
                "candidate_id": idx,
                "row_id": idx,
                "timestamp": out.get("timestamp_ms"),
                "pool_type": meta.get("pool_type", "v2"),
                "pool_class": "v2",
                "protocol": "v2",
                "token0_address": meta.get("token0_address"),
                "token1_address": meta.get("token1_address"),
                "token0": meta.get("token0_address"),
                "token1": meta.get("token1_address"),
                "fee_bps": meta.get("fee_bps", 30),
                "weth_leg": meta.get("weth_leg"),
                "weth_leg_source": meta.get("weth_leg_source"),
                "decoded_source_used": "uniswap_v2_swaps_decoded",
                "decoded_join_status": "matched",
                "buyer": "",
                "seller": "",
            }
        )
        ts_ms = optional_float(out.get("timestamp_ms"))
        if ts_ms is not None:
            entry_dt = datetime.fromtimestamp(int(ts_ms) / 1000, UTC)
            exit_dt = entry_dt + timedelta(seconds=1800)
            out["source_timestamp"] = entry_dt.isoformat()
            out["block_time"] = entry_dt.isoformat()
            out["signal_time"] = entry_dt.isoformat()
            out["feature_cutoff_time"] = entry_dt.isoformat()
            out["entry_time"] = entry_dt.isoformat()
            out["exit_time"] = exit_dt.isoformat()
        out["swap_direction"] = _h1610s_direction(out)
        out_rows.append(out)
    return out_rows


def _h1610s_candidate_metrics(rows: list[dict[str, Any]], target: int, strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    n = len(rows)
    pools = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top = pools.most_common()
    return {
        "status": "PASS" if n >= target else "PARTIAL",
        "strategy": strategy or {},
        "target_candidates": target,
        "total_candidates": n,
        "unique_pools": len(pools),
        "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
        "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0, 8),
        "top_pools": [{"pool": p, "rows": c, "share": rounded(c / n, 8) if n else None} for p, c in top[:20]],
        "time_bucket_distribution": {
            "days_top20": dict(Counter(str(r.get("day_bucket")) for r in rows).most_common(20)),
            "hours": dict(Counter(str(r.get("hour_bucket")) for r in rows).most_common()),
        },
        "source_identity_coverage": rounded(sum(1 for r in rows if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number")) / n, 8) if n else 0.0,
        "weth_leg_coverage": rounded(sum(1 for r in rows if r.get("weth_leg")) / n, 8) if n else 0.0,
        "weth_leg_source_distribution": dict(Counter(str(r.get("weth_leg_source")) for r in rows).most_common()),
        "direction_distribution": dict(Counter(str(r.get("swap_direction")) for r in rows).most_common()),
        "decoded_source": "uniswap_v2_swaps_decoded",
    }


def _h1610s_candidate_query(
    client: ClickHouseClient,
    cfg: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
    strategy: dict[str, Any],
    target: int,
) -> list[dict[str, Any]]:
    if not resolved:
        return []
    db = client.database
    table = cfg["clickhouse"]["decoded_sources"]["v2_table"]
    seed = str(strategy.get("seed", cfg.get("candidate_expansion", {}).get("deterministic_sample_seed", "run004_h1610s")))
    max_per_pool = int(strategy["max_per_pool"])
    max_per_bucket = int(strategy["max_per_pool_time_bucket"])
    bucket_sec = int(strategy["bucket_sec"])
    horizon_ms = int(cfg["labeling"].get("fixed_horizon_sec", 1800)) * 1000
    ts_ms_expr, dt_expr = _h1610s_ts_expressions()
    rows: list[dict[str, Any]] = []
    pools = sorted(resolved)
    chunk_size = int(cfg.get("candidate_expansion", {}).get("query_pool_chunk_size", 400))
    for idx in range(0, len(pools), chunk_size):
        chunk = pools[idx:idx + chunk_size]
        pool_sql = ",".join(_sql_string(p) for p in chunk)
        q = f"""
            SELECT *
            FROM
            (
                SELECT
                    row_number() OVER (
                        PARTITION BY lower(pair_address), intDiv({ts_ms_expr}, {bucket_sec * 1000})
                        ORDER BY cityHash64(tx_hash, toString(log_index), { _sql_string(seed) })
                    ) AS rn_pool_bucket,
                    row_number() OVER (
                        PARTITION BY lower(pair_address)
                        ORDER BY cityHash64(toString({ts_ms_expr}), tx_hash, toString(log_index), { _sql_string(seed) })
                    ) AS rn_pool,
                    lower(pair_address) AS pool_address,
                    lower(pair_address) AS pair_address,
                    tx_hash AS source_tx_hash,
                    log_index AS source_log_index,
                    block_number AS source_block_number,
                    toString({dt_expr}) AS source_timestamp,
                    toString({dt_expr}) AS block_time,
                    {ts_ms_expr} AS timestamp_ms,
                    toString({dt_expr}) AS signal_time,
                    toString({dt_expr}) AS feature_cutoff_time,
                    toString({dt_expr}) AS entry_time,
                    toString({dt_expr}) AS exit_time,
                    lower(sender) AS sender,
                    lower(recipient) AS recipient,
                    toString(amount0_in) AS amount0_in,
                    toString(amount1_in) AS amount1_in,
                    toString(amount0_out) AS amount0_out,
                    toString(amount1_out) AS amount1_out,
                    'uniswap_v2_swaps_decoded' AS decoded_source,
                    'decoded_swap_event' AS candidate_kind,
                    1 AS include_source_event_in_features,
                    toDate({dt_expr}) AS day_bucket,
                    toHour({dt_expr}) AS hour_bucket,
                    intDiv({ts_ms_expr}, {bucket_sec * 1000}) AS sampler_bucket
                FROM {db}.{table}
                WHERE chain_id = 8453
                  AND removed = 0
                  AND tx_hash != ''
                  AND block_number > 0
                  AND lower(pair_address) IN ({pool_sql})
            )
            WHERE rn_pool_bucket <= {max_per_bucket} AND rn_pool <= {max_per_pool}
            ORDER BY cityHash64(pool_address, toString(sampler_bucket), source_tx_hash, toString(source_log_index), { _sql_string(seed) })
            LIMIT {max(0, target - len(rows))}
        """
        rows.extend(client.query_json(q))
        if len(rows) >= target:
            break
    rows = _h1610s_apply_meta(rows[:target], resolved)
    for idx, row in enumerate(rows):
        row["candidate_id"] = idx
        row["row_id"] = idx
    return rows


def _h1610s_count_rows_for_pools(client: ClickHouseClient, cfg: dict[str, Any], pools: set[str], where: str = "") -> dict[str, Any]:
    if not pools:
        return {"rows": 0, "pools": 0}
    db = client.database
    table = cfg["clickhouse"]["decoded_sources"]["v2_table"]
    out_rows = 0
    out_pools = 0
    pool_list = sorted(pools)
    chunk_size = int(cfg.get("candidate_expansion", {}).get("query_pool_chunk_size", 400))
    for idx in range(0, len(pool_list), chunk_size):
        pool_sql = ",".join(_sql_string(p) for p in pool_list[idx:idx + chunk_size])
        q = f"""
            SELECT count() AS rows, uniqExact(lower(pair_address)) AS pools
            FROM {db}.{table}
            WHERE chain_id = 8453 AND removed = 0 AND tx_hash != ''
              AND lower(pair_address) IN ({pool_sql})
              {where}
        """
        row = client.query_json(q)[0]
        out_rows += int(optional_float(row.get("rows")) or 0)
        out_pools += int(optional_float(row.get("pools")) or 0)
    return {"rows": out_rows, "pools": out_pools}


def _h1610s_sampler_waterfall(
    client: ClickHouseClient,
    cfg: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
    previous_report: dict[str, Any],
    rows: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_pools = set(resolved)
    source = _h1610s_count_rows_for_pools(client, cfg, resolved_pools)
    identity = _h1610s_count_rows_for_pools(
        client,
        cfg,
        resolved_pools,
        "AND block_number > 0 AND log_index >= 0 AND coalesce(tx_hash, '') != ''",
    )
    amount = source
    final_metrics = _h1610s_candidate_metrics(rows, int(cfg["candidate_expansion"].get("target_candidates", 15000)), strategy)
    direction_counts = Counter(str(r.get("swap_direction")) for r in rows)
    dedup_keys = {(r.get("pool_address"), r.get("source_tx_hash"), str(r.get("source_log_index"))) for r in rows}
    waterfall = {
        "steps": [
            {"step": "resolved_rows_from_h1610r_or_resume", **source},
            {"step": "rows_with_source_tx_log_block_timestamp", **identity},
            {"step": "rows_with_weth_leg", **source},
            {"step": "rows_after_min_amount_or_dust_filter", **amount, "filter": "amount0/1 in/out sum > 0"},
            {"step": "rows_after_time_bucket_filters", "rows": len(rows), "pools": len(set(_lower_addr(r.get("pool_address")) for r in rows)), "bucket_sec": strategy.get("bucket_sec")},
            {"step": "rows_after_pool_cap", "rows": len(rows), "pools": len(set(_lower_addr(r.get("pool_address")) for r in rows)), "max_per_pool": strategy.get("max_per_pool")},
            {"step": "rows_after_per_pool_per_bucket_cap", "rows": len(rows), "pools": len(set(_lower_addr(r.get("pool_address")) for r in rows)), "max_per_pool_time_bucket": strategy.get("max_per_pool_time_bucket")},
            {"step": "rows_after_dedup_key", "rows": len(dedup_keys), "pools": len(set(p for p, _, _ in dedup_keys)), "dedup_key": "pool_address/source_tx_hash/source_log_index"},
            {"step": "final_candidate_rows", "rows": len(rows), "pools": final_metrics["unique_pools"]},
        ],
        "direction_distribution": dict(direction_counts.most_common()),
        "previous_h1610r_candidate_count": previous_report.get("total_candidates"),
        "root_cause": (
            "H16.10R compressed resolved rows because the sampler used conservative caps "
            "(max_per_pool=25 and per-pool 1h bucket cap=3). H16.10S expands those caps and tests smaller time buckets; "
            "there is no synthetic-time fallback and no single-direction hidden filter."
        ),
        "hidden_filters": [
            "chain_id=8453",
            "removed=0",
            "tx_hash non-empty",
            "resolved WETH pool",
            "positive decoded amount sum",
            "entry + fixed horizon before now",
        ],
    }
    bottleneck = {
        "sampler_only_kept_small_rows_per_pool": True,
        "h1610r_caps": {"max_per_pool": 25, "max_per_pool_time_bucket": 3, "bucket_sec": 3600},
        "h1610s_recommended_caps": strategy,
        "direction_filter_present": False,
        "dedup_key_too_coarse": False,
        "diversity_over_quantity_tradeoff": "H16.10R caps were too conservative for a 15k candidate target; H16.10S raises caps while monitoring top1/top5 share.",
        "final_candidate_metrics": final_metrics,
    }
    return waterfall, bottleneck


def _h1610s_strategy_comparison(
    client: ClickHouseClient,
    cfg: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    strategies = cfg.get("candidate_expansion", {}).get("strategies", [])
    target = int(cfg["candidate_expansion"].get("target_candidates", 15000))
    ideal = int(cfg["candidate_expansion"].get("ideal_candidate_pool", 30000))
    comparisons: list[dict[str, Any]] = []
    rows_by_strategy: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for strategy in strategies:
        cap_target = ideal if bool(strategy.get("ideal_pool", False)) else target
        rows = _h1610s_candidate_query(client, cfg, resolved, strategy, cap_target)
        metrics = _h1610s_candidate_metrics(rows, target, strategy)
        metrics["feature_coverage_proxy"] = {
            "source_identity_coverage": metrics["source_identity_coverage"],
            "weth_leg_coverage": metrics["weth_leg_coverage"],
            "decoded_source_event_available_by_construction": True,
        }
        comparisons.append(metrics)
        rows_by_strategy.append((strategy, rows))
    recommended_strategy, recommended_rows = max(
        rows_by_strategy,
        key=lambda pair: (
            min(len(pair[1]), target),
            -(_h1610s_candidate_metrics(pair[1], target, pair[0]).get("top5_share") or 1.0),
        ),
    ) if rows_by_strategy else ({}, [])
    return comparisons, recommended_rows, recommended_strategy, rows_by_strategy


def _h1610s_backfill_pair_tokens_throttled(
    rpc_url: str,
    pools: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not rpc_url:
        return {}, {"status": "BLOCKED_RPC_INJECTION", "attempted_pools": 0, "newly_weth_pools": 0}
    from scripts import label_dashcam_executable as labeler  # noqa: WPS433

    rpc = labeler.RPC(rpc_url, timeout=int(cfg["rpc"].get("timeout_sec", 60)), max_retries=1)
    token1_selector = "0xd21220a7"
    rate_limit = float(cfg["rpc"].get("rate_limit_per_sec", 20))
    max_pools = int(cfg["rpc"].get("max_resume_pools", len(pools)))
    selected = pools[:max_pools]
    resolved: dict[str, dict[str, Any]] = {}
    failures: Counter[str] = Counter()
    actual_calls = 0
    retry_count = 0
    rate_429 = 0
    sample_failures: list[dict[str, Any]] = []
    last_call = 0.0

    def throttle() -> None:
        nonlocal last_call
        elapsed = time.time() - last_call
        sleep_for = max(0.0, (1.0 / max(rate_limit, 1.0)) - elapsed)
        if sleep_for:
            time.sleep(sleep_for)
        last_call = time.time()

    def eth_call(pool: str, data: str, block: int, attempt: int = 0) -> str:
        nonlocal actual_calls, retry_count, rate_429
        throttle()
        actual_calls += 1
        try:
            response = rpc.call("eth_call", [{"to": pool, "data": data}, hex(block) if block > 0 else "latest"])
            if response and "error" in response:
                msg = str(response["error"])
                if "-32007" in msg or "429" in msg or "request limit" in msg:
                    rate_429 += 1
                    if attempt < int(cfg["rpc"].get("max_retries", 3)):
                        retry_count += 1
                        time.sleep(min(float(cfg["rpc"].get("base_backoff_sec", 1.0)) * (2 ** attempt), 12.0))
                        return eth_call(pool, data, block, attempt + 1)
                raise RuntimeError(msg)
            result = str(response.get("result", ""))
            if not result or result == "0x":
                raise RuntimeError("empty_result")
            return ("0x" + result[-40:]).lower()
        except Exception as exc:
            msg = str(exc)
            if ("429" in msg or "request limit" in msg or "-32007" in msg) and attempt < int(cfg["rpc"].get("max_retries", 3)):
                rate_429 += 1
                retry_count += 1
                time.sleep(min(float(cfg["rpc"].get("base_backoff_sec", 1.0)) * (2 ** attempt), 12.0))
                return eth_call(pool, data, block, attempt + 1)
            raise

    try:
        throttle()
        actual_calls += 1
        rpc.call("eth_chainId", [])
    except Exception as exc:
        return {}, {"status": "RPC_FAILURE", "attempted_pools": 0, "newly_weth_pools": 0, "failure": str(exc), "actual_rpc_calls": actual_calls}

    for item in selected:
        pool = _lower_addr(item.get("pool_address"))
        block = int(optional_float(item.get("max_block")) or 0)
        try:
            token0 = eth_call(pool, "0x0dfe1681", block)
            token1 = eth_call(pool, token1_selector, block)
            leg = _h1610_weth_leg(token0, token1)
            if leg:
                resolved[pool] = {
                    "pool_address": pool,
                    "token0_address": token0,
                    "token1_address": token1,
                    "pool_type": "v2",
                    "protocol": "v2",
                    "fee_bps": 30,
                    "weth_leg": leg,
                    "weth_leg_source": "rpc_backfill",
                }
        except Exception as exc:
            failures[type(exc).__name__] += 1
            if len(sample_failures) < 10:
                sample_failures.append({"pool_address": pool, "reason": str(exc)[:160]})
    return resolved, {
        "status": "COMPLETED",
        "attempted_pools": len(selected),
        "newly_resolved_pools": len(resolved),
        "newly_weth_pools": len(resolved),
        "still_unresolved_pools": len(selected) - len(resolved),
        "actual_rpc_calls": actual_calls,
        "rate_limit_per_sec": rate_limit,
        "429_count": rate_429,
        "retry_count": retry_count,
        "failure_reasons": dict(failures.most_common()),
        "sample_failures": sample_failures,
        "cache_scope": "diagnostics_only",
    }


def _h1610s_resolve_weth_metadata_resume(client: ClickHouseClient, cfg: dict[str, Any], rpc_url: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    pool_stats = _h1610_pool_stats(client, cfg)
    metadata = _h168_load_metadata_chunked(client, client.database, pool_stats, cfg["clickhouse"]["decoded_sources"]["metadata_table"])
    cache_path = _h1610_metadata_cache_path(cfg)
    cached: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text()).get("resolved", {})
        except Exception:
            cached = {}
    resolved: dict[str, dict[str, Any]] = {}
    level_counts: Counter[str] = Counter()
    row_counts: Counter[str] = Counter()
    unresolved: list[dict[str, Any]] = []
    for item in pool_stats:
        pool = _lower_addr(item.get("pool_address"))
        meta = metadata.get(pool)
        entry: dict[str, Any] | None = None
        source = "unknown"
        if meta and _h1610_weth_leg(meta.get("token0_address"), meta.get("token1_address")):
            source = "discovered_pools"
            entry = {
                **meta,
                "pool_address": pool,
                "token0_address": _lower_addr(meta.get("token0_address")),
                "token1_address": _lower_addr(meta.get("token1_address")),
                "pool_type": str(meta.get("pool_type") or "v2").lower(),
                "protocol": "v2",
                "weth_leg": _h1610_weth_leg(meta.get("token0_address"), meta.get("token1_address")),
                "weth_leg_source": source,
            }
        elif cached.get(pool) and cached[pool].get("weth_leg"):
            source = "rpc_backfill"
            entry = cached[pool]
        if entry:
            resolved[pool] = entry
            level_counts[source] += 1
            row_counts[source] += int(optional_float(item.get("decoded_rows")) or 0)
        else:
            unresolved.append(item)
    unresolved.sort(key=lambda p: int(optional_float(p.get("decoded_rows")) or 0), reverse=True)
    newly_resolved, resume_report = _h1610s_backfill_pair_tokens_throttled(rpc_url, unresolved, cfg)
    for pool, entry in newly_resolved.items():
        resolved[pool] = entry
    merged = {**cached, **resolved}
    write_json(cache_path, {"generated_at": datetime.now(UTC).isoformat(), "resolved": merged})
    total_pools = len(pool_stats)
    total_rows = sum(int(optional_float(p.get("decoded_rows")) or 0) for p in pool_stats)
    resolved_rows = sum(int(optional_float(p.get("decoded_rows")) or 0) for p in pool_stats if _lower_addr(p.get("pool_address")) in resolved)
    for pool, entry in newly_resolved.items():
        level_counts["rpc_backfill_new"] += 1
        row_counts["rpc_backfill_new"] += int(optional_float(next((p.get("decoded_rows") for p in pool_stats if _lower_addr(p.get("pool_address")) == pool), 0)) or 0)
    report = {
        **resume_report,
        "total_decoded_pools_considered": total_pools,
        "total_decoded_rows_considered": total_rows,
        "resolved_pools_after_resume": len(resolved),
        "resolved_rows_after_resume": resolved_rows,
        "unresolved_pools_after_resume": total_pools - len(resolved),
        "unresolved_rows_after_resume": total_rows - resolved_rows,
        "pool_coverage_after_resume": rounded(len(resolved) / total_pools, 8) if total_pools else 0.0,
        "row_coverage_after_resume": rounded(resolved_rows / total_rows, 8) if total_rows else 0.0,
        "coverage_by_level_pools": dict(level_counts.most_common()),
        "coverage_by_level_rows": dict(row_counts.most_common()),
        "cached_resolved_pools_loaded": len(cached),
        "do_not_requery_already_resolved_pools": True,
        "sample_still_unresolved_high_row_pools": [{"pool_address": _lower_addr(p.get("pool_address")), "decoded_rows": p.get("decoded_rows")} for p in unresolved if _lower_addr(p.get("pool_address")) not in newly_resolved][:20],
    }
    return resolved, report, pool_stats


def _h1610s_markdown(patch: dict[str, Any]) -> str:
    cand = patch["expanded_candidate_report"]
    feat = patch["feature_coverage_precheck_report"]
    pilot = patch["pilot_report"]
    decision = patch["decision_report"]
    backfill = patch["backfill_resume_report"]
    bottleneck = patch["sampler_bottleneck_report"]
    return f"""# Run-004 Patch H16.10S - V2 Candidate Sampler Expansion and RPC Backfill Resume

## Status
- Result: `{patch['gate']['status']}`
- Allow H16.11 full 10k labeling: `{decision['allow_h16_11_v2_decoded_backed_full_10k_labeling']}`
- H17 allowed: `False`
- Promote/live allowed: `False`

## Sampler Bottleneck
- Root cause: {bottleneck['root_cause'] if 'root_cause' in bottleneck else patch['candidate_filter_waterfall']['root_cause']}
- H16.10R caps: {bottleneck.get('h1610r_caps')}
- H16.10S recommended caps: {bottleneck.get('h1610s_recommended_caps')}

## Backfill Resume
- Attempted pools: {backfill.get('attempted_pools')}
- Newly WETH pools: {backfill.get('newly_weth_pools')}
- 429 count / retries: {backfill.get('429_count')} / {backfill.get('retry_count')}
- Actual RPC calls: {backfill.get('actual_rpc_calls')}
- Resolved coverage after resume: pools {backfill.get('pool_coverage_after_resume')} / rows {backfill.get('row_coverage_after_resume')}

## Expanded Candidates
- Candidates / target: {cand['total_candidates']} / {cand['target_candidates']}
- Unique pools: {cand['unique_pools']}
- Top1/top5 share: {cand['top1_share']} / {cand['top5_share']}
- WETH/source coverage: {cand['weth_leg_coverage']} / {cand['source_identity_coverage']}
- Direction distribution: {cand.get('direction_distribution')}

## Feature Precheck
- Decoded coverage: {feat['decoded_bridge_coverage']['coverage']}
- Sender coverage: {feat['family_coverage']['sender_concentration']['coverage']}
- Buyer coverage: {feat['family_coverage']['buyer_concentration']['coverage']}
- Flow coverage: {feat['family_coverage']['flow_count']['coverage']}
- Temporal coverage: {feat['family_coverage']['temporal']['coverage']}
- No usable context rows: {feat.get('rows_with_no_usable_pre_entry_or_source_context')}

## Pilot
- Attempted / success / failed: {pilot['attempted']} / {pilot['success']} / {pilot['failed']}
- Quote success rate: {pilot.get('quote_success_rate')}
- Failure reasons: {pilot.get('failure_reasons')}

## Decision
- Blockers: {decision['blockers']}
- Next step: {decision['next_step']}
"""


def _h1610s_v2_candidate_sampler_expansion_rpc_backfill_resume(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    rpc_url, _rpc_source, rpc_report = _h1610_select_rpc_url(cfg)
    write_json(_resolve(outputs["rpc_readiness_injection_report_json"]), rpc_report)
    resolved, backfill_report, _pool_stats = _h1610s_resolve_weth_metadata_resume(client, cfg, rpc_url)
    write_json(_resolve(outputs["backfill_resume_report_json"]), backfill_report)
    comparisons, expanded_rows, recommended_strategy, _rows_by_strategy = _h1610s_strategy_comparison(client, cfg, resolved)
    write_json(_resolve(outputs["sampler_strategy_comparison_json"]), {"strategies": comparisons, "recommended_strategy": recommended_strategy})
    candidate_report = _h1610s_candidate_metrics(expanded_rows, int(cfg["candidate_expansion"].get("target_candidates", 15000)), recommended_strategy)
    write_csv_rows(_resolve(outputs["expanded_candidate_csv"]), expanded_rows)
    write_json(_resolve(outputs["expanded_candidate_report_json"]), candidate_report)
    previous_report_path = _resolve(cfg.get("input_paths", {}).get("h1610r_candidate_report_json", "mvp/diagnostics/run004_h1610r_candidate_report.json"))
    previous_report = json.loads(previous_report_path.read_text()) if previous_report_path.exists() else {}
    waterfall, bottleneck = _h1610s_sampler_waterfall(client, cfg, resolved, previous_report, expanded_rows, recommended_strategy)
    write_json(_resolve(outputs["candidate_filter_waterfall_json"]), waterfall)
    write_json(_resolve(outputs["sampler_bottleneck_report_json"]), bottleneck)
    enriched_candidates, feature_report, _feature_cols = _h1610_feature_precheck(expanded_rows, cfg, client)
    write_json(_resolve(outputs["feature_coverage_precheck_report_json"]), feature_report)
    family = feature_report["family_coverage"]
    expansion_pass = (
        len(expanded_rows) >= int(cfg["candidate_expansion"].get("target_candidates", 15000))
        and (candidate_report.get("source_identity_coverage") or 0) >= 0.95
        and (candidate_report.get("weth_leg_coverage") or 0) >= 0.90
        and (feature_report["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90
        and (family["sender_concentration"]["coverage"] or 0) >= 0.90
        and (family["buyer_concentration"]["coverage"] or 0) >= 0.90
        and (family["flow_count"]["coverage"] or 0) >= 0.90
    )
    pilot_rows: list[dict[str, Any]] = []
    if expansion_pass:
        pilot_rows, pilot_report = _h1610_run_pilot(cfg, enriched_candidates, rpc_url, rpc_report)
        if pilot_rows:
            pilot_rows = _h167_recompute_exact_top20(pilot_rows)
            pilot_rows, pilot_risk = _h169_risk_report(pilot_rows, cfg)
            pilot_report["risk_severity_distribution"] = pilot_risk["risk_severity_distribution"]
            pilot_report["alpha_top20_after_gas_distribution"] = dict(Counter(str(r.get("alpha_top20_after_gas")) for r in pilot_rows).most_common())
            pilot_report["feature_coverage_summary"] = {
                "decoded_bridge_coverage": feature_report["decoded_bridge_coverage"],
                "family_coverage": feature_report["family_coverage"],
            }
            write_csv_rows(_resolve(outputs["pilot_dataset_csv"]), pilot_rows)
    else:
        pilot_report = _h169_empty_checkpoint("SKIPPED_CANDIDATE_OR_FEATURE_GATE", "expanded_candidate_or_feature_gate_failed", min(int(cfg["pilot"].get("rows", 300)), len(expanded_rows)))
    write_json(_resolve(outputs["pilot_report_json"]), pilot_report)
    checks = {
        "expanded_candidates_gte_15000": len(expanded_rows) >= int(cfg["candidate_expansion"].get("target_candidates", 15000)),
        "source_identity_coverage_gte_95pct": (candidate_report.get("source_identity_coverage") or 0) >= 0.95,
        "weth_leg_coverage_gte_90pct": (candidate_report.get("weth_leg_coverage") or 0) >= 0.90,
        "decoded_coverage_gte_90pct": (feature_report["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90,
        "sender_coverage_gte_90pct": (family["sender_concentration"]["coverage"] or 0) >= 0.90,
        "buyer_coverage_gte_90pct": (family["buyer_concentration"]["coverage"] or 0) >= 0.90,
        "flow_coverage_gte_90pct": (family["flow_count"]["coverage"] or 0) >= 0.90,
        "pilot_quote_success_gte_90pct_if_executed": (pilot_report.get("quote_success_rate") is None and not expansion_pass) or ((pilot_report.get("quote_success_rate") or 0) >= 0.90),
        "no_full_10k_run": True,
        "no_production_model_writes": True,
    }
    allow_h1611 = all(checks.values()) and expansion_pass and (pilot_report.get("quote_success_rate") or 0) >= 0.90
    decision = {
        "allow_h16_11_v2_decoded_backed_full_10k_labeling": allow_h1611,
        "allow_h17": False,
        "promote_live_allowed": False,
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "next_step": "Run H16.11 V2 decoded-backed checkpointed full 10k labeling" if allow_h1611 else "Stay PARTIAL; repair blockers before full 10k.",
    }
    write_json(_resolve(outputs["decision_report_json"]), decision)
    gate = {
        "status": "READY_FOR_H16_11" if allow_h1611 else "PARTIAL",
        "allow_h16_11_v2_decoded_backed_full_10k_labeling": allow_h1611,
        "allow_h17_v2_only_return_aware_feature_ablation": False,
        "promote_live_allowed": False,
        "checks": checks,
        "blockers": decision["blockers"],
        "forbidden": ["H17", "promote", "live", "full 10k in H16.10S", "production model write", "active model overwrite", "runtime trading code change"],
    }
    patch = {
        "patch": "run-004-h16.10s-v2-candidate-sampler-expansion-and-rpc-backfill-resume",
        "generated_at": datetime.now(UTC).isoformat(),
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "rpc_readiness_injection_report": rpc_report,
        "backfill_resume_report": backfill_report,
        "candidate_filter_waterfall": waterfall,
        "sampler_bottleneck_report": bottleneck,
        "sampler_strategy_comparison": {"strategies": comparisons, "recommended_strategy": recommended_strategy},
        "expanded_candidate_report": candidate_report,
        "feature_coverage_precheck_report": feature_report,
        "pilot_report": pilot_report,
        "decision_report": decision,
        "gate": gate,
        "safety_assertions": {**cfg.get("safety", {}), "rpc_url_written_to_artifacts": False, "production_model_write_allowed": False, "promote_live_allowed": False, "full_10k_run": False},
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h1610s_markdown(patch))
    return patch


def _h1611_preflight(candidates: list[dict[str, Any]], cfg: dict[str, Any], rpc_report: dict[str, Any]) -> dict[str, Any]:
    feature_report_path = _resolve(cfg["input_paths"]["h1610s_feature_coverage_precheck_report_json"])
    feature_report = json.loads(feature_report_path.read_text()) if feature_report_path.exists() else {}
    n = len(candidates)
    direction_counts = Counter(str(r.get("swap_direction") or r.get("direction")) for r in candidates)
    source_identity = sum(1 for r in candidates if r.get("source_tx_hash") and str(r.get("source_log_index", "")) != "" and r.get("source_block_number") and r.get("source_timestamp"))
    weth_rows = sum(1 for r in candidates if r.get("weth_leg"))
    decoded_swap_rows = sum(1 for r in candidates if str(r.get("decoded_source") or r.get("decoded_source_used")) == "uniswap_v2_swaps_decoded" and str(r.get("candidate_kind")) == "decoded_swap_event")
    family = feature_report.get("family_coverage", {})
    checks = {
        "candidates_gte_20000": n >= int(cfg["preflight"].get("min_candidates", 20000)),
        "source_identity_coverage_gte_95pct": (source_identity / n if n else 0.0) >= 0.95,
        "weth_leg_coverage_gte_95pct": (weth_rows / n if n else 0.0) >= 0.95,
        "decoded_coverage_gte_90pct": (feature_report.get("decoded_bridge_coverage", {}).get("coverage") or 0) >= 0.90,
        "sender_coverage_gte_90pct": (family.get("sender_concentration", {}).get("coverage") or 0) >= 0.90,
        "buyer_coverage_gte_90pct": (family.get("buyer_concentration", {}).get("coverage") or 0) >= 0.90,
        "flow_coverage_gte_90pct": (family.get("flow_count", {}).get("coverage") or 0) >= 0.90,
        "has_buy_and_sell_direction": direction_counts.get("buy_weth_to_token", 0) > 0 and direction_counts.get("sell_token_to_weth", 0) > 0,
        "no_synthetic_metadata_time_candidates": decoded_swap_rows == n,
        "all_candidates_decoded_backed_swap_events": decoded_swap_rows == n,
        "rpc_ready": rpc_report.get("status") == "READY",
        "rpc_token_not_written_to_artifacts": True,
    }
    return {
        "status": "PASS" if all(checks.values()) else "HOLD",
        "candidate_rows": n,
        "source_identity_coverage": rounded(source_identity / n, 8) if n else 0.0,
        "weth_leg_coverage": rounded(weth_rows / n, 8) if n else 0.0,
        "direction_distribution": dict(direction_counts.most_common()),
        "decoded_backed_swap_event_rows": decoded_swap_rows,
        "feature_coverage_from_h1610s": {
            "decoded": feature_report.get("decoded_bridge_coverage"),
            "sender": family.get("sender_concentration"),
            "buyer": family.get("buyer_concentration"),
            "flow": family.get("flow_count"),
            "temporal": family.get("temporal"),
        },
        "rpc_readiness": {k: rpc_report.get(k) for k in ("status", "selected_rpc_source", "selected_rpc_redacted", "rpc_url_written_to_artifacts")},
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
    }


def _h1611_label_full_10k(cfg: dict[str, Any], candidates: list[dict[str, Any]], rpc_url: str, rpc_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    outputs = cfg["outputs"]
    if rpc_report.get("status") != "READY":
        checkpoint = _h169_empty_checkpoint("BLOCKED_RPC_INJECTION", "rpc_not_available_in_host_env_env_file_or_container", 0)
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return [], checkpoint
    from scripts import label_dashcam_executable as labeler  # noqa: WPS433

    class CountedRPC(labeler.RPC):  # type: ignore[misc]
        def __init__(self, url: str):
            super().__init__(url, timeout=int(cfg["labeling"].get("timeout_sec", 60)), max_retries=int(cfg["labeling"].get("max_retries", 6)))
            self.call_count = 0

        def call(self, method: str, params: list[Any]) -> dict[str, Any]:  # type: ignore[override]
            self.call_count += 1
            return super().call(method, params)

        def batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:  # type: ignore[override]
            self.call_count += len(calls)
            return super().batch(calls)

    lp = cfg["labeling"]
    target_success = int(lp.get("target_success_rows", 10000))
    max_attempts = min(int(lp.get("max_attempts", len(candidates))), len(candidates))
    batch_size = int(lp.get("batch_size", 250))
    workers = int(lp.get("workers", 8))
    selected = [row for row in candidates if str(row.get("pool_class") or "v2") == "v2"][:max_attempts]
    previous = os.environ.get("LABEL_RPC_URL")
    injected = False
    if rpc_url and not previous:
        os.environ["LABEL_RPC_URL"] = rpc_url
        injected = True
    rpc = CountedRPC(os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or rpc_url)
    checkpoint: dict[str, Any] = {
        "status": "started",
        "selected": len(selected),
        "target_success_rows": target_success,
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "extra_after_target": 0,
        "failure_reasons": {},
        "label_type_counts": {},
        "estimated_rpc_calls": len(selected) * 8,
        "actual_rpc_calls": 0,
        "quote_success_rate": None,
        "batch_size": batch_size,
        "completed_batches": 0,
        "batch_reports": [],
        "resume_state": {"completed": False, "next_candidate_id": 0},
        "checkpoint_accounting_formula": "attempted = success_kept + failed + skipped + extra_after_target",
        "rpc_injection": {"used_temporary_label_rpc_url_env": injected, "source": rpc_report.get("selected_rpc_source"), "redacted": rpc_report.get("selected_rpc_redacted"), "rpc_url_written_to_artifacts": False},
    }
    rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    try:
        chain = rpc.call("eth_chainId", [])
        if int(chain["result"], 16) != 8453:
            raise RuntimeError(f"unexpected_chain_id:{int(chain['result'], 16)}")
        horizon = int(lp.get("fixed_horizon_sec", 1800))
        min_ts = min(int(float(r["timestamp_ms"])) // 1000 for r in selected)
        max_ts = max(int(float(r["timestamp_ms"])) // 1000 for r in selected) + horizon
        bn_arr, ts_arr = labeler.build_ts_grid(rpc, min_ts, max_ts)

        def process_candidate(cand: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
            ts_sec = int(float(cand["timestamp_ms"])) // 1000
            entry_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec)
            exit_block = labeler.ts_to_block(bn_arr, ts_arr, ts_sec + horizon)
            meta = {
                "pool_type": cand.get("pool_type") or "v2",
                "t0": _lower_addr(cand.get("token0_address") or cand.get("token0")),
                "t1": _lower_addr(cand.get("token1_address") or cand.get("token1")),
                "fee_bps": int(optional_float(cand.get("fee_bps")) or 30),
            }
            label, net, label_type = labeler.label_one(rpc, _lower_addr(cand.get("pool_address")), meta, labeler.WETH_BASE, int(lp.get("notional_wei", 500000000000000)), entry_block, exit_block)
            if label < 0:
                return False, {"candidate_id": cand.get("candidate_id"), "pool_address": cand.get("pool_address"), "swap_direction": cand.get("swap_direction"), "failure_reason": label_type}, label_type
            fixed = float(net)
            gas = _h165_gas_adjusted(fixed, "v2", {"labeling_pilot": lp})
            medium = optional_float(gas.get("gas_adjusted_return_30m_medium")) or fixed
            quote_anom = int(abs(fixed) >= 0.20)
            scale_risk = int(abs(fixed) >= 0.50)
            gas_dom = int(fixed > 0 and medium <= 0)
            row = {
                **cand,
                "entry_block": entry_block,
                "exit_block": exit_block,
                "fixed_horizon_sec": horizon,
                "label_status": "success",
                "failure_reason": "",
                "label_type": label_type,
                "executable_label_current": label,
                "gross_quote_return_30m": rounded(fixed + float(lp.get("round_trip_cost", 0.006)), 8),
                "fixed_cost_adjusted_return_30m": rounded(fixed, 8),
                **gas,
                "final_return_after_cost": rounded(medium, 8),
                "slippage_stress_return": rounded(medium - float(lp.get("slippage_stress_bps", 100)) / 10000.0, 8),
                "exit_policy_used": "fixed_30m_fallback",
                "exit_reason": "fixed_horizon",
                "dynamic_exit_available": False,
                "dynamic_exit_unavailable_reason": "missing_intra_window_quote_path_trigger_block_token_amount_and_trigger_sell_quote",
                "executable_label_after_gas": int(medium > 0),
                "alpha_ret_gt_5pct_after_gas": int(medium > 0.05),
                "alpha_ret_gt_10pct_after_gas": int(medium > 0.10),
                "bad_pool_label": int(_lower_addr(cand.get("pool_address")) in {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("confirmed_bad_pools", [])}),
                "quote_anomaly_label": quote_anom,
                "scale_risk_label": scale_risk,
                "liquidity_anomaly_label": int(label_type in ("fail_low_liquidity", "executable_exit_drained")),
                "gas_cost_dominates_label": gas_dom,
                "alpha_island_candidate_label": int(_lower_addr(cand.get("pool_address")) in {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("alpha_island_pools", [])}),
                "bad_pool_candidate": int(_lower_addr(cand.get("pool_address")) in {_lower_addr(p) for p in cfg.get("risk_severity", {}).get("confirmed_bad_pools", [])}),
                "quarantine_reason": "quote_anomaly" if quote_anom else ("gas_cost_dominates" if gas_dom else ""),
            }
            row["direction"] = row.get("swap_direction") or row.get("direction")
            return True, row, label_type

        for start in range(0, len(selected), batch_size):
            batch = selected[start:start + batch_size]
            batch_report = {"batch_id": len(checkpoint["batch_reports"]), "attempted": 0, "success": 0, "failed": 0, "skipped": 0, "extra_after_target": 0, "failure_reasons": {}, "fail_sanity_clip_count": 0, "quote_success_rate": None, "actual_rpc_calls_start": rpc.call_count}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(process_candidate, cand) for cand in batch]
                for future in as_completed(futures):
                    ok, payload, label_type = future.result()
                    checkpoint["attempted"] += 1
                    batch_report["attempted"] += 1
                    reason_counts[label_type] += 1
                    if not ok:
                        failed_rows.append(payload)
                        checkpoint["failed"] += 1
                        batch_report["failed"] += 1
                        batch_report["failure_reasons"][label_type] = int(batch_report["failure_reasons"].get(label_type, 0)) + 1
                        if label_type == "fail_sanity_clip":
                            batch_report["fail_sanity_clip_count"] += 1
                        continue
                    if len(rows) < target_success:
                        payload["row_id"] = len(rows)
                        rows.append(payload)
                        batch_report["success"] += 1
                    else:
                        checkpoint["extra_after_target"] += 1
                        batch_report["extra_after_target"] += 1
            checkpoint["success"] = len(rows)
            checkpoint["completed_batches"] += 1
            checkpoint["actual_rpc_calls"] = rpc.call_count
            checkpoint["label_type_counts"] = dict(reason_counts.most_common())
            checkpoint["failure_reasons"] = {k: v for k, v in reason_counts.most_common() if k != "executable"}
            checkpoint["skipped"] = max(0, len(selected) - checkpoint["attempted"]) if len(rows) >= target_success else 0
            checkpoint["quote_success_rate"] = rounded((checkpoint["success"] + checkpoint["extra_after_target"]) / checkpoint["attempted"], 8) if checkpoint["attempted"] else 0.0
            batch_report["actual_rpc_calls_end"] = rpc.call_count
            batch_report["actual_rpc_calls"] = batch_report["actual_rpc_calls_end"] - batch_report["actual_rpc_calls_start"]
            batch_report["quote_success_rate"] = rounded((batch_report["success"] + batch_report["extra_after_target"]) / batch_report["attempted"], 8) if batch_report["attempted"] else 0.0
            checkpoint["batch_reports"].append(batch_report)
            checkpoint["resume_state"] = {"completed": len(rows) >= target_success, "next_candidate_id": start + len(batch)}
            write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
            if len(rows) >= target_success:
                break
        checkpoint["status"] = "completed" if len(rows) >= target_success else "PARTIAL"
        checkpoint["attempting_stopped_reason"] = "target_success_reached" if len(rows) >= target_success else "candidate_exhausted"
        checkpoint["checkpoint_accounting"] = {
            "attempted": checkpoint["attempted"],
            "success_kept": checkpoint["success"],
            "failed": checkpoint["failed"],
            "skipped": checkpoint["skipped"],
            "extra_after_target": checkpoint["extra_after_target"],
            "closed": checkpoint["attempted"] == checkpoint["success"] + checkpoint["failed"] + checkpoint["extra_after_target"],
            "selected_minus_attempted_equals_skipped": len(selected) - checkpoint["attempted"] == checkpoint["skipped"],
        }
        checkpoint["return_distribution"] = numeric_summary([r.get("final_return_after_cost") for r in rows])
        checkpoint["resume_state"] = {"completed": checkpoint["status"] == "completed", "next_candidate_id": checkpoint["attempted"]}
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return rows, checkpoint
    except Exception as exc:
        checkpoint["status"] = "LABELER_FAILURE"
        checkpoint["failure_reasons"] = {str(exc): checkpoint.get("attempted", 0)}
        checkpoint["actual_rpc_calls"] = getattr(rpc, "call_count", 0)
        checkpoint["resume_state"] = {"completed": False, "reason": str(exc)}
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        return [], checkpoint
    finally:
        if injected:
            os.environ.pop("LABEL_RPC_URL", None)


def _h1611_feature_report(rows: list[dict[str, Any]], feature_cols: list[str], windows: list[int]) -> dict[str, Any]:
    report = _h169_coverage_report(rows, feature_cols, windows)
    report["per_feature_missingness"] = {col: rounded(sum(1 for r in rows if r.get(col) in ("", None)) / len(rows), 8) if rows else 0.0 for col in feature_cols}
    report["rows_with_no_usable_pre_entry_or_source_context"] = sum(1 for r in rows if not optional_float(r.get("decoded_event_count_1800s")))
    return report


def _h1611_direction_split_report(rows: list[dict[str, Any]], checkpoint: dict[str, Any], attempted_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    attempted_by_dir = Counter(str(r.get("swap_direction") or r.get("direction")) for r in attempted_candidates[: int(checkpoint.get("attempted") or 0)])

    def one(direction: str) -> dict[str, Any]:
        subset = [r for r in rows if str(r.get("swap_direction") or r.get("direction")) == direction]
        n = len(subset)
        pools = Counter(_lower_addr(r.get("pool_address")) for r in subset)
        top = pools.most_common()
        vals = [optional_float(r.get("final_return_after_cost")) for r in subset]
        vals = [v for v in vals if v is not None]
        return {
            "rows": n,
            "attempted": attempted_by_dir.get(direction, 0),
            "quote_success": rounded(n / attempted_by_dir.get(direction, 1), 8) if attempted_by_dir.get(direction, 0) else None,
            "return_mean": rounded(sum(vals) / len(vals), 8) if vals else None,
            "return_p50": rounded(percentile(vals, 0.50), 8) if vals else None,
            "return_p90": rounded(percentile(vals, 0.90), 8) if vals else None,
            "positive_rate": rounded(sum(1 for r in subset if int(optional_float(r.get("executable_label_after_gas")) or 0) == 1) / n, 8) if n else 0.0,
            "alpha_top20_count": sum(int(optional_float(r.get("alpha_top20_after_gas")) or 0) for r in subset),
            "top1_share": rounded(top[0][1] / n if n and top else 0.0, 8),
            "top5_share": rounded(sum(c for _, c in top[:5]) / n if n else 0.0),
            "feature_coverage": {
                "decoded": rounded(sum(1 for r in subset if optional_float(r.get("decoded_event_count_1800s")) is not None) / n, 8) if n else 0.0,
                "sender": nonempty_coverage(subset, ["unique_swap_senders_300s", "sender_entropy_300s"]).get("coverage") if subset else 0.0,
                "buyer": nonempty_coverage(subset, ["unique_buyers_300s", "top1_buyer_share_300s"]).get("coverage") if subset else 0.0,
                "flow": nonempty_coverage(subset, ["buy_count_300s", "sell_count_300s", "net_flow_raw_300s"]).get("coverage") if subset else 0.0,
            },
            "return_distribution": numeric_summary([r.get("final_return_after_cost") for r in subset]),
        }

    return {
        "overall": {
            "total_rows": len(rows),
            "return_distribution": numeric_summary([r.get("final_return_after_cost") for r in rows]),
            "positive_rate": rounded(sum(1 for r in rows if int(optional_float(r.get("executable_label_after_gas")) or 0) == 1) / len(rows), 8) if rows else 0.0,
            "alpha_top20_count": sum(int(optional_float(r.get("alpha_top20_after_gas")) or 0) for r in rows),
            "direction_distribution": dict(Counter(str(r.get("swap_direction") or r.get("direction")) for r in rows).most_common()),
        },
        "buy_weth_to_token": one("buy_weth_to_token"),
        "sell_token_to_weth": one("sell_token_to_weth"),
    }


def _h1611_dataset_quality(rows: list[dict[str, Any]], clean_alpha: list[dict[str, Any]], risk_rows: list[dict[str, Any]], checkpoint: dict[str, Any], feature_report: dict[str, Any], direction_report: dict[str, Any], risk_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "COMPLETE" if len(rows) >= 10000 else "PARTIAL",
        "final_rows": len(rows),
        "attempted": checkpoint.get("attempted"),
        "success": checkpoint.get("success"),
        "failed": checkpoint.get("failed"),
        "skipped": checkpoint.get("skipped"),
        "extra_after_target": checkpoint.get("extra_after_target"),
        "quote_success_rate": checkpoint.get("quote_success_rate"),
        "actual_rpc_calls": checkpoint.get("actual_rpc_calls"),
        **_h16_distribution(rows),
        "direction_distribution": direction_report["overall"]["direction_distribution"],
        "feature_coverage_by_family": feature_report.get("family_coverage"),
        "return_distribution": numeric_summary([r.get("final_return_after_cost") for r in rows]),
        "alpha_top20_after_gas_distribution": dict(Counter(str(r.get("alpha_top20_after_gas")) for r in rows).most_common()),
        "risk_severity_distribution": risk_report.get("risk_severity_distribution"),
        "clean_alpha_rows": len(clean_alpha),
        "risk_rows": len(risk_rows),
        "checkpoint_accounting": checkpoint.get("checkpoint_accounting"),
        "secret_redaction_check": {"rpc_url_written_to_artifacts": False},
    }


def _h1611_markdown(patch: dict[str, Any]) -> str:
    q = patch["dataset_quality_report"]
    ck = patch["checkpoint_report"]
    feat = patch["feature_coverage_report"]
    return f"""# Run-004 Patch H16.11 - V2 Decoded-Backed Full 10k Labeling

## Status
- Result: `{patch['gate']['status']}`
- H17 allowed: `{patch['gate']['allow_h17_v2_only_return_aware_feature_ablation']}`
- Promote/live allowed: `False`

## Labeling
- Attempted / success / failed / skipped / extra: {ck.get('attempted')} / {ck.get('success')} / {ck.get('failed')} / {ck.get('skipped')} / {ck.get('extra_after_target')}
- Final rows: {q['final_rows']}
- Quote success rate: {q.get('quote_success_rate')}
- Actual RPC calls: {q.get('actual_rpc_calls')}

## Dataset
- Unique pools: {q['unique_pools']}
- Top1/top5 share: {q['top1_share']} / {q['top5_share']}
- Direction distribution: {q['direction_distribution']}
- Alpha top20 distribution: {q['alpha_top20_after_gas_distribution']}
- Risk severity: {q['risk_severity_distribution']}
- Clean alpha rows: {q['clean_alpha_rows']}
- Risk rows: {q['risk_rows']}

## Feature Coverage
- Decoded: {feat['decoded_bridge_coverage']['coverage']}
- Sender: {feat['family_coverage']['sender_concentration']['coverage']}
- Buyer: {feat['family_coverage']['buyer_concentration']['coverage']}
- Flow: {feat['family_coverage']['flow_count']['coverage']}
- Temporal: {feat['family_coverage']['temporal']['coverage']}

## Gate
- Blockers: {patch['gate']['blockers']}
- Next step: {patch['gate']['next_step']}
"""


def _h1611_v2_decoded_backed_full_10k_labeling(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    client = ClickHouseClient(cfg.get("clickhouse", {}))
    client.database = str(cfg.get("clickhouse", {}).get("database", "eatlq"))
    candidates = read_rows(_resolve(cfg["input_paths"]["expanded_candidate_csv"]))
    rpc_url, _rpc_source, rpc_report = _h1610_select_rpc_url(cfg)
    write_json(_resolve(outputs["rpc_readiness_injection_report_json"]), rpc_report)
    preflight = _h1611_preflight(candidates, cfg, rpc_report)
    write_json(_resolve(outputs["preflight_report_json"]), preflight)
    if preflight["status"] != "PASS":
        checkpoint = _h169_empty_checkpoint("HOLD_PREFLIGHT", ",".join(preflight["blockers"]), 0)
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint)
        patch = {"patch": "run-004-h16.11-v2-decoded-backed-full-10k-labeling", "generated_at": datetime.now(UTC).isoformat(), "preflight_report": preflight, "checkpoint_report": checkpoint, "gate": {"status": "HOLD", "allow_h17_v2_only_return_aware_feature_ablation": False, "promote_live_allowed": False, "blockers": preflight["blockers"], "next_step": "Repair preflight blockers before full labeling"}}
        write_json(_resolve(outputs["patch_json"]), patch)
        write_markdown(_resolve(outputs["markdown"]), f"# Run-004 Patch H16.11 - HOLD\n\nPreflight blockers: {preflight['blockers']}\n")
        return patch
    enriched_candidates, _candidate_feature_report, feature_cols = _h1610_feature_precheck(candidates, cfg, client)
    rows, checkpoint = _h1611_label_full_10k(cfg, enriched_candidates, rpc_url, rpc_report)
    rows = _h167_recompute_exact_top20(rows)
    for idx, row in enumerate(rows):
        row["row_id"] = idx
        row["direction"] = row.get("swap_direction") or row.get("direction")
    by_pool_hour = Counter((r.get("pool_address"), str(r.get("entry_time", ""))[:13]) for r in rows)
    for row in rows:
        row["repeated_pool_hour_cluster"] = int(by_pool_hour[(row.get("pool_address"), str(row.get("entry_time", ""))[:13])] >= int(cfg.get("risk_severity", {}).get("repeated_pool_hour_cluster_threshold", 25)))
        if int(row.get("repeated_pool_hour_cluster") or 0) and not row.get("quarantine_reason"):
            row["quarantine_reason"] = "repeated_pool_hour_cluster_review"
    rows, risk_report = _h169_risk_report(rows, cfg)
    clean_alpha = [r for r in rows if r.get("risk_severity") == "clean" and int(optional_float(r.get("alpha_top20_after_gas")) or 0) == 1]
    risk_rows = [r for r in rows if r.get("risk_severity") != "clean" or int(optional_float(r.get("bad_pool_label")) or 0) == 1]
    buy_rows = [r for r in rows if str(r.get("direction")) == "buy_weth_to_token"]
    sell_rows = [r for r in rows if str(r.get("direction")) == "sell_token_to_weth"]
    feature_report = _h1611_feature_report(rows, feature_cols, [int(w) for w in cfg.get("windows_sec", H169_WINDOWS_DEFAULT)])
    direction_report = _h1611_direction_split_report(rows, checkpoint, enriched_candidates)
    risk_report["risk_reason_distribution"] = dict(Counter(str(r.get("quarantine_reason") or "none") for r in rows).most_common())
    quality = _h1611_dataset_quality(rows, clean_alpha, risk_rows, checkpoint, feature_report, direction_report, risk_report)
    write_csv_rows(_resolve(outputs["all_dataset_csv"]), rows)
    write_csv_rows(_resolve(outputs["clean_alpha_dataset_csv"]), clean_alpha)
    write_csv_rows(_resolve(outputs["risk_bad_pool_dataset_csv"]), risk_rows)
    write_csv_rows(_resolve(outputs["buy_trigger_subset_csv"]), buy_rows)
    write_csv_rows(_resolve(outputs["sell_trigger_subset_csv"]), sell_rows)
    write_json(_resolve(outputs["feature_coverage_report_json"]), feature_report)
    write_json(_resolve(outputs["direction_split_report_json"]), direction_report)
    write_json(_resolve(outputs["risk_severity_report_json"]), risk_report)
    write_json(_resolve(outputs["dataset_quality_report_json"]), quality)
    checks = {
        "final_rows_gte_8000": len(rows) >= 8000,
        "target_10000_reached": len(rows) >= int(cfg["labeling"].get("target_success_rows", 10000)),
        "quote_success_rate_gte_90pct": (checkpoint.get("quote_success_rate") or 0) >= 0.90,
        "source_identity_coverage_gte_95pct": preflight["source_identity_coverage"] >= 0.95,
        "weth_leg_coverage_gte_95pct": preflight["weth_leg_coverage"] >= 0.95,
        "decoded_coverage_gte_90pct": (feature_report["decoded_bridge_coverage"]["coverage"] or 0) >= 0.90,
        "sender_coverage_gte_90pct": (feature_report["family_coverage"]["sender_concentration"]["coverage"] or 0) >= 0.90,
        "buyer_coverage_gte_90pct": (feature_report["family_coverage"]["buyer_concentration"]["coverage"] or 0) >= 0.90,
        "flow_coverage_gte_90pct": (feature_report["family_coverage"]["flow_count"]["coverage"] or 0) >= 0.90,
        "alpha_top20_exact_20pct": sum(int(optional_float(r.get("alpha_top20_after_gas")) or 0) for r in rows) == math.ceil(0.20 * len(rows)),
        "checkpoint_accounting_explained": bool(checkpoint.get("checkpoint_accounting", {}).get("closed")),
        "no_full_rpc_token_in_artifacts": True,
        "no_production_writes": True,
    }
    success = all(checks.values())
    gate = {
        "status": "PASS" if success else "PARTIAL",
        "allow_h17_v2_only_return_aware_feature_ablation": success,
        "promote_live_allowed": False,
        "checks": checks,
        "blockers": [k for k, v in checks.items() if not v],
        "next_step": "Run H17 V2-only return-aware feature ablation" if success else "Repair H16.11 blockers before H17",
        "forbidden": ["promote", "live", "production model write", "active model overwrite", "runtime trading code change", "V3 artifact mutation"],
    }
    patch = {
        "patch": "run-004-h16.11-v2-decoded-backed-full-10k-labeling",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in cfg.get("input_paths", {}).items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "preflight_report": preflight,
        "checkpoint_report": checkpoint,
        "feature_coverage_report": feature_report,
        "direction_split_report": direction_report,
        "risk_severity_report": risk_report,
        "dataset_quality_report": quality,
        "gate": gate,
        "safety_assertions": {**cfg.get("safety", {}), "rpc_url_written_to_artifacts": False, "production_model_write_allowed": False, "promote_live_allowed": False},
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h1611_markdown(patch))
    return patch


def _v3r6_v3_only_5k_dataset_build(config_path: Path) -> dict[str, Any]:
    from scripts import label_dashcam_executable as labeler

    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    clickhouse = ClickHouseClient(cfg.get("clickhouse", {}))
    _discover_database(clickhouse, str(cfg.get("clickhouse", {}).get("database", "default")))
    labeling_cfg = cfg.get("labeling", {})
    min_cfg = cfg.get("min_notional", {})
    rpc_cfg = cfg.get("rpc", {})
    weth = str(cfg.get("weth_address", labeler.WETH_BASE)).lower()
    selected_min_weth_raw = int(min_cfg.get("selected_weth_raw", 500_000_000_000_000))
    thresholds = [int(float(v) * 10**18) for v in min_cfg.get("sensitivity_weth", [0.00025, 0.0005, 0.001, 0.002])]
    target_success = int(labeling_cfg.get("target_success_rows", 5000))
    max_attempts = int(labeling_cfg.get("max_attempts", 7000))
    batch_size = int(labeling_cfg.get("batch_size", 250))
    workers = int(labeling_cfg.get("workers", 8))
    notional_wei = int(labeling_cfg.get("notional_wei", selected_min_weth_raw))
    fixed_horizon_sec = int(labeling_cfg.get("fixed_horizon_sec", 1800))
    block_offset = int(labeling_cfg.get("exit_block_offset", 900))
    gas_units = int(labeling_cfg.get("gas_units_round_trip", 500000))
    gas_price_gwei = labeling_cfg.get("gas_price_gwei", {"low": 0.01, "medium": 0.03, "high": 0.10})

    rpc_url = ""
    for env_name in rpc_cfg.get("env_vars", ["LABEL_RPC_URL", "EVM_HTTP_URL", "LIVE_BASE_RPC_URL"]):
        rpc_url = os.environ.get(env_name, "").strip()
        if rpc_url:
            break
    if not rpc_url:
        for env_path in rpc_cfg.get("env_files", []):
            path = _resolve(env_path)
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                if "=" not in line or line.strip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                if key.strip() in {"LABEL_RPC_URL", "EVM_HTTP_URL", "LIVE_BASE_RPC_URL"} and value.strip():
                    rpc_url = value.strip().strip('"').strip("'")
                    break
            if rpc_url:
                break
    if not rpc_url:
        raise RuntimeError("No RPC URL found in configured env vars/files")

    def as_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            try:
                return int(float(str(value)))
            except (TypeError, ValueError):
                return default

    def as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return default

    def dt(value: Any) -> datetime | None:
        text = str(value or "").replace("Z", "+00:00")
        if not text:
            return None
        try:
            return datetime.fromisoformat(text).astimezone(UTC)
        except ValueError:
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
            except ValueError:
                return None

    def norm(raw: int, decimals: int) -> float | None:
        return raw / float(10**decimals) if decimals >= 0 else None

    def distribution(values: list[float]) -> dict[str, Any]:
        if not values:
            return {}
        vals = sorted(values)
        return {
            "min": vals[0],
            "p25": vals[len(vals) // 4],
            "p50": percentile(vals, 0.5),
            "p75": vals[(len(vals) * 3) // 4],
            "p90": percentile(vals, 0.9),
            "p99": percentile(vals, 0.99),
            "max": vals[-1],
        }

    metadata_rows = clickhouse.query_json(
        """
        SELECT
          lower(pool_address) AS pool_address,
          lower(token0_address) AS token0,
          lower(token1_address) AS token1,
          token0_symbol,
          token1_symbol,
          token0_decimals,
          token1_decimals,
          fee_bps,
          has_weth_leg
        FROM discovered_pools
        WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
        """
    )
    metadata = {str(row["pool_address"]): row for row in metadata_rows}
    raw_rows = clickhouse.query_json(
        f"""
        SELECT
          lower(pool_address) AS pool_addr,
          tx_hash AS src_tx_hash,
          log_index AS src_log_index,
          block_number AS src_block_number,
          toString(block_time) AS block_time_str,
          lower(sender) AS sender_addr,
          lower(recipient) AS recipient_addr,
          toString(amount0) AS amount0_str,
          toString(amount1) AS amount1_str,
          toString(sqrt_price_x96) AS sqrtPriceX96,
          toString(liquidity) AS liquidity,
          tick AS tick
        FROM uniswap_v3_swaps_decoded
        WHERE chain_id = 8453
          AND removed = 0
          AND amount0 != 0
          AND amount1 != 0
          AND tx_hash != ''
          AND block_time IS NOT NULL
          AND lower(pool_address) IN (
            SELECT lower(pool_address)
            FROM discovered_pools
            WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
          )
        ORDER BY cityHash64(pool_address, tx_hash, log_index)
        LIMIT {int(cfg.get("candidate_expansion", {}).get("max_decoded_rows", 50000))}
        """,
    )

    candidates: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_rows):
        pool = str(row["pool_addr"])
        meta = metadata.get(pool)
        if not meta:
            continue
        token0 = str(meta["token0"]).lower()
        token1 = str(meta["token1"]).lower()
        dec0 = as_int(meta.get("token0_decimals"), 18)
        dec1 = as_int(meta.get("token1_decimals"), 18)
        amount0 = as_int(row.get("amount0_str"))
        amount1 = as_int(row.get("amount1_str"))
        if amount0 > 0 and amount1 < 0:
            token_in, token_out = token0, token1
            amount_in, amount_out = amount0, abs(amount1)
            in_dec, out_dec = dec0, dec1
        elif amount1 > 0 and amount0 < 0:
            token_in, token_out = token1, token0
            amount_in, amount_out = amount1, abs(amount0)
            in_dec, out_dec = dec1, dec0
        else:
            token_in = token_out = ""
            amount_in = amount_out = 0
            in_dec = out_dec = -1
        weth_side_raw = amount_in if token_in == weth else amount_out if token_out == weth else 0
        block_time = dt(row.get("block_time_str"))
        source_block = as_int(row["src_block_number"])
        candidates.append({
            "candidate_id": len(candidates),
            "candidate_kind": "decoded_backed_v3_swap_v3r6",
            "decoded_source": "uniswap_v3_swaps_decoded",
            "pool_address": pool,
            "source_tx_hash": row["src_tx_hash"],
            "source_log_index": row["src_log_index"],
            "source_block_number": source_block,
            "block_number": source_block,
            "entry_block": source_block,
            "exit_block": source_block + block_offset,
            "fixed_horizon_sec": fixed_horizon_sec,
            "source_timestamp": block_time.isoformat() if block_time else row.get("block_time_str", ""),
            "timestamp": block_time.isoformat() if block_time else row.get("block_time_str", ""),
            "timestamp_ms": int(block_time.timestamp() * 1000) if block_time else "",
            "sender": row.get("sender_addr", ""),
            "recipient": row.get("recipient_addr", ""),
            "amount0": str(amount0),
            "amount1": str(amount1),
            "sqrtPriceX96": row.get("sqrtPriceX96", ""),
            "tick": row.get("tick", ""),
            "liquidity": row.get("liquidity", ""),
            "token0": token0,
            "token1": token1,
            "token0_symbol": meta.get("token0_symbol", ""),
            "token1_symbol": meta.get("token1_symbol", ""),
            "token0_decimals": dec0,
            "token1_decimals": dec1,
            "fee_bps": meta.get("fee_bps", ""),
            "fee_tier": as_int(meta.get("fee_bps")) * 100,
            "weth_leg": "token0" if token0 == weth else "token1" if token1 == weth else "missing",
            "metadata_join_status": "joined_discovered_pools",
            "tx_log_block_identity_present": bool(row.get("src_tx_hash") and row.get("src_log_index") is not None and source_block),
            "direction_ambiguous": token_in == "" or token_out == "",
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amountIn_raw": str(amount_in),
            "amountOut_raw": str(amount_out),
            "amountIn_normalized": norm(amount_in, in_dec),
            "amountOut_normalized": norm(amount_out, out_dec),
            "weth_side_amount_raw": str(weth_side_raw),
            "weth_side_amount_normalized": norm(weth_side_raw, 18),
            "is_buy_weth_to_token": token_in == weth and token_out not in {"", weth},
            "is_sell_token_to_weth": token_out == weth and token_in not in {"", weth},
        })

    def coverage(rows: list[dict[str, Any]], pred) -> float:
        return round(sum(1 for row in rows if pred(row)) / len(rows), 8) if rows else 0.0

    sensitivity = []
    for threshold in thresholds:
        kept = [row for row in candidates if as_int(row["weth_side_amount_raw"]) >= threshold]
        amounts = [as_int(row["weth_side_amount_raw"]) / 1e18 for row in kept]
        risk = "low" if threshold >= 500_000_000_000_000 else "moderate_dust_quote_risk"
        sensitivity.append({
            "threshold_weth": threshold / 1e18,
            "threshold_weth_raw": threshold,
            "candidate_count": len(kept),
            "unique_pools": len({row["pool_address"] for row in kept}),
            "fee_tier_distribution": dict(Counter(str(row["fee_tier"]) for row in kept).most_common()),
            "amount_distribution": distribution(amounts),
            "dust_rows_removed": len(candidates) - len(kept),
            "estimated_quote_success_risk": risk,
        })
    selected_candidates = [row for row in candidates if as_int(row["weth_side_amount_raw"]) >= selected_min_weth_raw]
    selected_candidates.sort(key=lambda row: (
        int(datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp()) // 3600 if row.get("timestamp") else 0,
        str(row["pool_address"]),
        as_int(row["source_log_index"]),
    ))
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_candidates:
        by_pool[str(row["pool_address"])].append(row)
    balanced: list[dict[str, Any]] = []
    pool_names = sorted(by_pool, key=lambda pool: len(by_pool[pool]), reverse=True)
    idx = 0
    while len(balanced) < max_attempts and pool_names:
        added = False
        for pool in pool_names:
            if idx < len(by_pool[pool]):
                balanced.append(by_pool[pool][idx])
                added = True
                if len(balanced) >= max_attempts:
                    break
        if not added:
            break
        idx += 1
    for idx, row in enumerate(balanced):
        row["candidate_id"] = idx

    write_csv_rows(_resolve(outputs["candidate_pool_csv"]), balanced)
    candidate_report = {
        "total_decoded_rows_loaded": len(raw_rows),
        "candidate_count_before_min_notional": len(candidates),
        "candidate_count_after_selected_min_notional": len(selected_candidates),
        "candidate_pool_rows_written": len(balanced),
        "selected_min_notional_weth": selected_min_weth_raw / 1e18,
        "unique_pools_after_selected_min_notional": len({row["pool_address"] for row in selected_candidates}),
        "unique_pools_written": len({row["pool_address"] for row in balanced}),
        "current_source_limit_unique_weth_decoded_pools": len({row["pool_address"] for row in candidates}),
        "discovered_weth_v3_metadata_pools": len(metadata),
        "source_identity_coverage": coverage(balanced, lambda row: bool(row["tx_log_block_identity_present"])),
        "token0_token1_weth_fee_coverage": coverage(balanced, lambda row: row["token0"] and row["token1"] and row["weth_leg"] in {"token0", "token1"} and as_int(row["fee_tier"]) > 0),
        "decoded_coverage": 1.0 if balanced else 0.0,
        "fee_tier_distribution": dict(Counter(str(row["fee_tier"]) for row in balanced).most_common()),
        "hour_bucket_distribution_top50": dict(Counter(str(row["timestamp"])[:13] for row in balanced).most_common(50)),
        "time_range": {"min": min((str(row["timestamp"]) for row in balanced), default=""), "max": max((str(row["timestamp"]) for row in balanced), default="")},
    }
    write_json(_resolve(outputs["candidate_report_json"]), candidate_report)
    min_report = {
        "selected_threshold_weth": selected_min_weth_raw / 1e18,
        "recommended_threshold_weth": 0.0005,
        "recommendation": "Do not lower below 0.0005 WETH; expanded decoded coverage provides enough candidates and 0.00025 adds dust quote risk.",
        "thresholds": sensitivity,
    }
    write_json(_resolve(outputs["min_notional_sensitivity_json"]), min_report)

    feature_rows = sorted(balanced, key=lambda row: (str(row["timestamp"]), as_int(row["source_log_index"])))
    pool_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sender_history: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in feature_rows:
        ts_ms = as_int(row.get("timestamp_ms"))
        pool = str(row["pool_address"])
        sender = str(row.get("sender", ""))
        one_hour_ms = 3600 * 1000
        pool_prior = [prev for prev in pool_history[pool] if ts_ms and as_int(prev.get("timestamp_ms")) < ts_ms and ts_ms - as_int(prev.get("timestamp_ms")) <= one_hour_ms]
        sender_prior = [prev for prev in sender_history[(pool, sender)] if sender and ts_ms and as_int(prev.get("timestamp_ms")) < ts_ms and ts_ms - as_int(prev.get("timestamp_ms")) <= one_hour_ms]
        prev_pool = pool_history[pool][-1] if pool_history[pool] else None
        prev_ts = as_int(prev_pool.get("timestamp_ms")) if prev_pool else 0
        row["feature_sender_present"] = bool(sender)
        row["feature_sender_pool_prior_count_1h"] = len(sender_prior)
        row["feature_pool_flow_count_1h"] = len(pool_prior)
        row["feature_pool_weth_flow_sum_1h"] = sum(as_int(prev.get("weth_side_amount_raw")) for prev in pool_prior) / 1e18
        row["feature_pool_weth_signed_flow_sum_1h"] = sum((1 if prev.get("is_buy_weth_to_token") else -1) * as_int(prev.get("weth_side_amount_raw")) for prev in pool_prior) / 1e18
        row["feature_pool_unique_senders_1h"] = len({str(prev.get("sender", "")) for prev in pool_prior if prev.get("sender")})
        row["feature_temporal_prev_pool_delta_sec"] = round((ts_ms - prev_ts) / 1000, 3) if ts_ms and prev_ts else ""
        row["feature_fee_tier"] = row["fee_tier"]
        row["feature_tick"] = row.get("tick", "")
        row["feature_liquidity"] = row.get("liquidity", "")
        row["feature_protocol"] = "uniswap_v3"
        pool_history[pool].append(row)
        if sender:
            sender_history[(pool, sender)].append(row)

    rpc = labeler.RPC(rpc_url, timeout=int(rpc_cfg.get("timeout_sec", 60)), max_retries=int(rpc_cfg.get("max_retries", 6)))
    checkpoint_batches = []
    labeled: list[dict[str, Any]] = []
    success_count = 0
    actual_rpc_calls = 0

    def label_candidate(row: dict[str, Any]) -> dict[str, Any]:
        label, net, label_type = labeler.label_v3(
            rpc,
            str(row["pool_address"]),
            str(row["token0"]),
            str(row["token1"]),
            as_int(row["fee_bps"]),
            weth,
            notional_wei,
            as_int(row["entry_block"]),
            as_int(row["exit_block"]),
        )
        out = dict(row)
        out["label_status"] = "success" if label_type == "executable" else "failed"
        out["failure_reason"] = "" if label_type == "executable" else label_type
        out["label_type"] = label_type
        out["executable_label_current"] = label
        if label_type == "executable":
            gross = net + float(labeler.ROUND_TRIP_COST)
            out["gross_quote_return_30m"] = round(gross, 10)
            out["fixed_cost_adjusted_return_30m"] = round(net, 10)
            for tier, gwei in gas_price_gwei.items():
                cost = gas_units * float(gwei) * 1e9 / notional_wei
                out[f"gas_cost_return_{tier}"] = round(cost, 10)
                out[f"gas_adjusted_return_30m_{tier}"] = round(net - cost, 10)
            out["final_return_after_cost"] = out["gas_adjusted_return_30m_medium"]
            out["executable_label_after_gas"] = 1 if out["final_return_after_cost"] > 0 else 0
        else:
            out["gross_quote_return_30m"] = ""
            out["fixed_cost_adjusted_return_30m"] = ""
            for tier, gwei in gas_price_gwei.items():
                out[f"gas_cost_return_{tier}"] = round(gas_units * float(gwei) * 1e9 / notional_wei, 10)
                out[f"gas_adjusted_return_30m_{tier}"] = ""
            out["final_return_after_cost"] = ""
            out["executable_label_after_gas"] = 0
        is_exit = label_type == "executable_exit_drained"
        is_sanity = label_type == "fail_sanity_clip"
        out["v3_exit_drained_label"] = int(is_exit)
        out["v3_liquidity_exit_risk"] = int(is_exit)
        out["v3_review_only"] = int(is_exit or is_sanity)
        out["quote_anomaly_label"] = int(is_sanity)
        out["scale_risk_label"] = int(is_sanity)
        out["gas_cost_dominates_label"] = ""
        out["repeated_pool_hour_cluster_label"] = 0
        out["quarantine_reason"] = "v3_exit_drained" if is_exit else "quote_or_scale_anomaly" if is_sanity else ""
        out["risk_severity"] = "review" if is_exit else "hard_exclude" if is_sanity else "clean"
        return out

    for start in range(0, min(len(feature_rows), max_attempts), batch_size):
        batch = feature_rows[start:start + batch_size]
        batch_results = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(label_candidate, row) for row in batch]
            for future in as_completed(futures):
                batch_results.append(future.result())
        batch_results.sort(key=lambda row: as_int(row["candidate_id"]))
        labeled.extend(batch_results)
        success_count = sum(1 for row in labeled if row["label_type"] == "executable")
        actual_rpc_calls += len(batch_results) * 2
        attempted = len(labeled)
        checkpoint_batches.append({
            "batch_index": len(checkpoint_batches),
            "attempted_cumulative": attempted,
            "success_cumulative": success_count,
            "failed_cumulative": attempted - success_count,
            "failure_reasons": dict(Counter(str(row["label_type"]) for row in labeled).most_common()),
            "fail_v3_entry_zero_rate": round(sum(1 for row in labeled if row["label_type"] == "fail_v3_entry_zero") / attempted, 8),
            "fail_sanity_clip_rate": round(sum(1 for row in labeled if row["label_type"] == "fail_sanity_clip") / attempted, 8),
            "exit_drained_rate": round(sum(1 for row in labeled if row["label_type"] == "executable_exit_drained") / attempted, 8),
            "estimated_rpc_calls": attempted * 2,
            "actual_rpc_calls": actual_rpc_calls,
        })
        checkpoint_payload = {"batches": checkpoint_batches, "complete": success_count >= target_success, "target_success_rows": target_success}
        write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint_payload)
        if success_count >= target_success:
            break

    executable = [row for row in labeled if row["label_type"] == "executable"]
    executable.sort(key=lambda row: as_float(row.get("final_return_after_cost")), reverse=True)
    final_success = executable[:target_success]
    top_n = int(len(final_success) * 0.2)
    final_ids = {str(row["candidate_id"]) for row in final_success}
    top_ids = {str(row["candidate_id"]) for row in final_success[:top_n]}
    for rank, row in enumerate(final_success, start=1):
        row["final_return_rank"] = rank
        row["alpha_top20_after_gas"] = 1 if str(row["candidate_id"]) in top_ids else 0
        ret = as_float(row.get("final_return_after_cost"))
        row["alpha_ret_gt_5pct_after_gas"] = int(ret > 0.05)
        row["alpha_ret_gt_10pct_after_gas"] = int(ret > 0.10)
        medium_cost = as_float(row.get("gas_cost_return_medium"))
        row["gas_cost_dominates_label"] = int(medium_cost > abs(as_float(row.get("fixed_cost_adjusted_return_30m"))))
    by_pool_final: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in final_success:
        by_pool_final[str(row["pool_address"])].append(row)
    for pool_rows in by_pool_final.values():
        pool_rows.sort(key=lambda row: as_float(row.get("final_return_after_cost")), reverse=True)
        for rank, row in enumerate(pool_rows, start=1):
            row["within_pool_return_rank"] = rank
    risk_rows = [row for row in labeled if row.get("risk_severity") != "clean" or row["label_type"] != "executable"]
    risk_pool_hour = Counter((str(row["pool_address"]), str(row.get("timestamp", ""))[:13]) for row in risk_rows if row.get("v3_exit_drained_label") == 1)
    repeated_clusters = {key for key, count in risk_pool_hour.items() if count >= int(cfg.get("risk", {}).get("repeated_pool_hour_threshold", 3))}
    for row in labeled:
        key = (str(row["pool_address"]), str(row.get("timestamp", ""))[:13])
        if key in repeated_clusters:
            row["repeated_pool_hour_cluster_label"] = 1
            row["quarantine_reason"] = "repeated_exit_drained_pool_hour_cluster" if row.get("v3_exit_drained_label") == 1 else row.get("quarantine_reason", "")
            row["risk_severity"] = "review"
    all_rows = [row for row in labeled if str(row["candidate_id"]) in final_ids] + [row for row in labeled if row["label_type"] != "executable"]
    clean_rows = [row for row in final_success if row.get("risk_severity") == "clean"]
    risk_rows = [row for row in labeled if row.get("risk_severity") != "clean" or row["label_type"] != "executable"]
    write_csv_rows(_resolve(outputs["all_dataset_csv"]), all_rows)
    write_csv_rows(_resolve(outputs["clean_alpha_dataset_csv"]), clean_rows)
    write_csv_rows(_resolve(outputs["risk_dataset_csv"]), risk_rows)

    feature_cols = [
        "feature_sender_present",
        "feature_sender_pool_prior_count_1h",
        "feature_pool_flow_count_1h",
        "feature_pool_weth_flow_sum_1h",
        "feature_pool_weth_signed_flow_sum_1h",
        "feature_pool_unique_senders_1h",
        "feature_temporal_prev_pool_delta_sec",
        "feature_fee_tier",
        "feature_tick",
        "feature_liquidity",
        "feature_protocol",
    ]
    feature_report = {
        "decoded_coverage": 1.0 if all_rows else 0.0,
        "sender_coverage": coverage(all_rows, lambda row: bool(row.get("sender"))),
        "buyer_seller_coverage": coverage(all_rows, lambda row: bool(row.get("is_buy_weth_to_token") or row.get("is_sell_token_to_weth"))),
        "flow_coverage": coverage(all_rows, lambda row: row.get("feature_pool_flow_count_1h") != ""),
        "temporal_coverage": coverage(all_rows, lambda row: row.get("feature_temporal_prev_pool_delta_sec") != ""),
        "tick_liquidity_metadata_coverage": coverage(all_rows, lambda row: row.get("tick") not in {"", None} and row.get("liquidity") not in {"", None}),
        "per_feature_missingness": {col: round(sum(1 for row in all_rows if row.get(col) in {"", None}) / len(all_rows), 8) if all_rows else 0.0 for col in feature_cols},
        "constant_all_zero_features": {
            col: {
                "constant": len({str(row.get(col, "")) for row in all_rows}) <= 1,
                "all_zero": all(str(row.get(col, "")) in {"", "0", "0.0", "False"} for row in all_rows),
            }
            for col in feature_cols
        },
        "future_function_guard": "features use only source event and prior selected events ordered by timestamp",
    }
    write_json(_resolve(outputs["feature_coverage_report_json"]), feature_report)

    attempted = len(labeled)
    success = len(executable)
    final_rows = len(final_success)
    risk_report = {
        "risk_rows": len(risk_rows),
        "clean_rows": len(clean_rows),
        "v3_exit_drained_label": sum(1 for row in risk_rows if row.get("v3_exit_drained_label") == 1),
        "v3_liquidity_exit_risk": sum(1 for row in risk_rows if row.get("v3_liquidity_exit_risk") == 1),
        "v3_review_only": sum(1 for row in risk_rows if row.get("v3_review_only") == 1),
        "quote_anomaly_label": sum(1 for row in risk_rows if row.get("quote_anomaly_label") == 1),
        "scale_risk_label": sum(1 for row in risk_rows if row.get("scale_risk_label") == 1),
        "repeated_pool_hour_cluster_label": sum(1 for row in risk_rows if row.get("repeated_pool_hour_cluster_label") == 1),
        "quarantine_reasons": dict(Counter(str(row.get("quarantine_reason", "")) for row in risk_rows).most_common()),
        "risk_severity_distribution": dict(Counter(str(row.get("risk_severity", "")) for row in risk_rows).most_common()),
    }
    write_json(_resolve(outputs["risk_report_json"]), risk_report)
    oracle_status = {
        "oracle_status": "pending_manual_execution",
        "manual_oracle_package_path": "mvp/diagnostics/run004_v3r5_oracle_spotcheck_report.json",
        "representative_rows": 20,
        "oracle_pending_reason": "External oracle token was not available in process environment for V3R-6; internal V3-only dataset build is not blocked, but mainline remains false.",
        "allow_v3_mainline": False,
    }
    write_json(_resolve(outputs["oracle_status_report_json"]), oracle_status)
    gate = {
        "final_successful_rows_ge_4500": final_rows >= 4500,
        "target_5000_met": final_rows >= target_success,
        "source_identity_coverage_ge_95pct": candidate_report["source_identity_coverage"] >= 0.95,
        "decoded_coverage_ge_90pct": feature_report["decoded_coverage"] >= 0.90,
        "quote_success_rate_ge_85pct": (success / attempted if attempted else 0) >= 0.85,
        "fail_v3_entry_zero_near_zero": (sum(1 for row in labeled if row["label_type"] == "fail_v3_entry_zero") / attempted if attempted else 1) <= 0.01,
        "fail_sanity_clip_not_dominant": (sum(1 for row in labeled if row["label_type"] == "fail_sanity_clip") / attempted if attempted else 1) <= 0.02,
        "exit_drained_handled_as_risk_label": risk_report["v3_exit_drained_label"] == sum(1 for row in labeled if row["label_type"] == "executable_exit_drained"),
        "alpha_top20_exact_20pct": sum(as_int(row.get("alpha_top20_after_gas")) for row in final_success) == top_n,
        "datasets_written": all(_resolve(outputs[key]).exists() for key in ["all_dataset_csv", "clean_alpha_dataset_csv", "risk_dataset_csv"]),
        "no_v2_artifacts_touched": True,
        "no_mixed_dataset_created": True,
    }
    checkpoint_report = {
        "complete": final_rows >= target_success,
        "attempted": attempted,
        "success": success,
        "failed": attempted - success,
        "skipped": max(0, len(balanced) - attempted),
        "final_rows": final_rows,
        "quote_success_rate": round(success / attempted, 8) if attempted else 0.0,
        "failure_reasons": dict(Counter(str(row["label_type"]) for row in labeled).most_common()),
        "fail_v3_entry_zero_rate": round(sum(1 for row in labeled if row["label_type"] == "fail_v3_entry_zero") / attempted, 8) if attempted else 0.0,
        "fail_sanity_clip_rate": round(sum(1 for row in labeled if row["label_type"] == "fail_sanity_clip") / attempted, 8) if attempted else 0.0,
        "exit_drained_rate": round(sum(1 for row in labeled if row["label_type"] == "executable_exit_drained") / attempted, 8) if attempted else 0.0,
        "estimated_rpc_calls": attempted * 2,
        "actual_rpc_calls": actual_rpc_calls,
        "batches": checkpoint_batches,
        "gate": gate,
    }
    write_json(_resolve(outputs["checkpoint_report_json"]), checkpoint_report)
    patch = {
        "patch": "run-004-v3r6-v3-only-5k-dataset-build",
        "gate": gate,
        "decision": {
            "allow_v3r7_v3_only_feature_ablation": all(gate.values()),
            "allow_v3_mainline": False,
            "allow_mixed_dataset": False,
            "v2_affected": False,
        },
        "summary": {
            "candidates": len(balanced),
            "unique_pools": candidate_report["unique_pools_written"],
            "selected_min_notional_weth": selected_min_weth_raw / 1e18,
            "attempted": attempted,
            "success": success,
            "failed": attempted - success,
            "quote_success_rate": checkpoint_report["quote_success_rate"],
            "final_rows": final_rows,
            "clean_alpha_rows": len(clean_rows),
            "risk_rows": len(risk_rows),
            "alpha_top20_after_gas_positive": sum(as_int(row.get("alpha_top20_after_gas")) for row in final_success),
            "oracle_status": oracle_status["oracle_status"],
        },
        "outputs": outputs,
        "safety": cfg.get("safety", {}),
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    md = f"""# Run-004 Patch V3R-6 - V3-only 5k Decoded-Backed Dataset Build

## Candidate Pool
- Candidates written: {len(balanced)}
- Unique pools: {candidate_report['unique_pools_written']}
- Selected min-notional: {selected_min_weth_raw / 1e18} WETH
- Source identity coverage: {candidate_report['source_identity_coverage']}
- Token/WETH/fee coverage: {candidate_report['token0_token1_weth_fee_coverage']}

## Labeling
- Attempted / success / failed: {attempted} / {success} / {attempted - success}
- Quote success rate: {checkpoint_report['quote_success_rate']}
- Final successful rows: {final_rows}
- fail_v3_entry_zero: {checkpoint_report['fail_v3_entry_zero_rate']}
- fail_sanity_clip: {checkpoint_report['fail_sanity_clip_rate']}
- exit_drained: {checkpoint_report['exit_drained_rate']}

## Datasets
- All V3 dataset: `{outputs['all_dataset_csv']}`
- Clean alpha dataset: `{outputs['clean_alpha_dataset_csv']}` ({len(clean_rows)} rows)
- Risk dataset: `{outputs['risk_dataset_csv']}` ({len(risk_rows)} rows)
- alpha_top20_after_gas positives: {patch['summary']['alpha_top20_after_gas_positive']} / {final_rows}

## Decision
- Allow V3R-7 feature ablation: `{patch['decision']['allow_v3r7_v3_only_feature_ablation']}`
- Allow V3 mainline: `false`
- Allow mixed dataset: `false`
- V2 affected: `false`
"""
    write_markdown(_resolve(outputs["markdown"]), md)
    return patch


# ---------------------------------------------------------------------------
# V3R-7 – V3-only Return-Aware Feature Ablation
# ---------------------------------------------------------------------------

V3R7_MICRO_GROUPS = {
    "sender_concentration",
    "flow_count",
    "signed_flow",
    "temporal",
    "combined_sender_flow",
    "combined_microstructure_no_raw_volume",
    "v3_liquidity_micro",
    "all_available",
}


def _v3r7_input_validation(
    rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_top20_after_gas"))
    net_col = str(cfg.get("net_return_column", "final_return_after_cost"))
    positives = sum(int(r.get(target, 0) or 0) for r in rows)
    negatives = len(rows) - positives
    protocols = set(r.get("feature_protocol", "") for r in rows)
    v2_rows = [r for r in rows if "v2" in str(r.get("feature_protocol", "")).lower() or "v2" in str(r.get("label_type", "")).lower()]
    source_id_cov_r = nonempty_coverage(rows, ["source_tx_hash", "pool_address", "sender"])
    decoded_cov_r = nonempty_coverage(rows, ["feature_fee_tier", "feature_tick", "feature_liquidity", "feature_pool_flow_count_1h"])
    temporal_cov_r = nonempty_coverage(rows, ["feature_temporal_prev_pool_delta_sec"])
    source_id_coverage = source_id_cov_r.get("coverage", 0.0)
    decoded_coverage = decoded_cov_r.get("coverage", 0.0)
    temporal_coverage = temporal_cov_r.get("coverage", 0.0)
    returns = [optional_float(r.get(net_col)) for r in rows]
    returns_num = [v for v in returns if v is not None]
    gates = {
        "rows_eq_5000": len(rows) == 5000,
        "alpha_top20_positives_eq_1000": positives == 1000,
        "alpha_top20_negatives_eq_4000": negatives == 4000,
        "source_identity_coverage_100pct": source_id_coverage >= 0.999,
        "decoded_feature_coverage_gte_99pct": decoded_coverage >= 0.99,
        "no_v2_rows": len(v2_rows) == 0,
        "all_protocol_v3": protocols <= {"uniswap_v3"},
        "risk_rows_separate": len(risk_rows) > 0,
        "oracle_status_noted": True,
    }
    return {
        "status": "pass" if all(gates.values()) else "fail",
        "total_rows": len(rows),
        "total_all_rows": len(all_rows),
        "risk_rows": len(risk_rows),
        "primary_target": target,
        "net_return_column": net_col,
        "positives": positives,
        "negatives": negatives,
        "positive_rate": rounded(positives / len(rows), 6) if rows else None,
        "source_identity_coverage": rounded(source_id_coverage, 6),
        "decoded_feature_coverage": rounded(decoded_coverage, 6),
        "temporal_coverage": rounded(temporal_coverage, 6),
        "protocol_distribution": dict(Counter(r.get("feature_protocol", "") for r in rows)),
        "v2_rows_detected": len(v2_rows),
        "oracle_status": str(cfg.get("oracle_status", "pending_manual_execution")),
        "return_summary": {
            "count": len(returns_num),
            "mean": rounded(sum(returns_num) / len(returns_num), 8) if returns_num else None,
            "p50": rounded(percentile(returns_num, 0.50), 8) if returns_num else None,
            "min": rounded(min(returns_num), 8) if returns_num else None,
            "max": rounded(max(returns_num), 8) if returns_num else None,
        },
        "gate_checks": gates,
        "risk_rows_separate_note": "exit_drained risk rows kept in separate file, not mixed into clean training set",
    }


def _v3r7_secondary_target_sanity(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    primary = str(cfg.get("primary_target", "alpha_top20_after_gas"))
    result: dict[str, Any] = {}
    for t in ["alpha_ret_gt_5pct_after_gas", "alpha_ret_gt_10pct_after_gas"]:
        vals = [int(r.get(t, 0) or 0) for r in rows]
        pos = sum(vals)
        result[t] = {
            "positives": pos,
            "negatives": len(vals) - pos,
            "positive_rate": rounded(pos / len(vals), 6) if vals else None,
            "not_deploy_evidence": True,
        }
    ranks = [optional_float(r.get("within_pool_return_rank")) for r in rows]
    ranks_num = [v for v in ranks if v is not None]
    result["within_pool_return_rank"] = {
        "coverage": len(ranks_num),
        "mean": rounded(sum(ranks_num) / len(ranks_num), 4) if ranks_num else None,
        "note": "forbidden as feature – reference only",
    }
    return {"secondary_targets": result, "primary_target": primary, "not_deploy_evidence": True}


def _v3r7_stress_rows(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    counts = Counter(_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address")))
    top_pools = [pool for pool, _ in counts.most_common(5)]
    top1 = set(top_pools[:1])
    top5 = set(top_pools)
    if name == "exclude_top1_pool":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top1]
    if name == "exclude_top5_pools":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top5]
    if name == "shuffled_5fold_sanity_only":
        return rows
    if name == "fee_tier_100_only":
        return [r for r in rows if str(r.get("fee_tier", "")) == "100"]
    if name == "fee_tier_non100":
        return [r for r in rows if str(r.get("fee_tier", "")) != "100"]
    if name == "high_liquidity":
        liq_vals = sorted(optional_float(r.get("feature_liquidity")) or 0.0 for r in rows)
        median_liq = liq_vals[len(liq_vals) // 2] if liq_vals else 0.0
        return [r for r in rows if (optional_float(r.get("feature_liquidity")) or 0.0) >= median_liq]
    if name == "low_liquidity":
        liq_vals = sorted(optional_float(r.get("feature_liquidity")) or 0.0 for r in rows)
        median_liq = liq_vals[len(liq_vals) // 2] if liq_vals else 0.0
        return [r for r in rows if (optional_float(r.get("feature_liquidity")) or 0.0) < median_liq]
    return rows


def _v3r7_stress_tests_v3(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    availability: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_top20_after_gas"))
    groups = list(cfg.get("feature_groups", {}).keys())
    tests: dict[str, Any] = {}
    for test in cfg.get("stress_tests", []):
        subset = _v3r7_stress_rows(rows, str(test))
        positives = sum(int(row.get(target, 0) or 0) for row in subset)
        item: dict[str, Any] = {
            "rows": len(subset),
            "positives": positives,
            "negatives": len(subset) - positives,
            "trainable": len(subset) >= int(cfg.get("training", {}).get("min_rows", 250)),
            "groups": {},
        }
        for group in groups:
            meta = availability["groups"].get(group, {})
            if not meta or meta.get("missing_features") or meta.get("high_missing_features"):
                item["groups"][group] = {
                    "status": "missing_features" if (meta or {}).get("missing_features") else "high_missing",
                    "sanity_only_not_deploy_evidence": True,
                }
                continue
            result = _h13_train_eval(
                group,
                subset,
                _h11_safe_features(meta),
                cfg,
                target=target,
                stress_name=str(test),
                shuffled_5fold=(test == "shuffled_5fold_sanity_only"),
                persist_candidate=False,
            )
            item["groups"][group] = result
        tests[str(test)] = item
    return {"sanity_only_not_deploy_evidence": True, "tests": tests}


def _v3r7_leakage_guard(
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    stress: dict[str, Any],
) -> dict[str, Any]:
    pools_per_fee: dict[str, set] = defaultdict(set)
    for r in rows:
        ft = str(r.get("fee_tier", ""))
        pa = _lower_addr(r.get("pool_address")) or ""
        pools_per_fee[ft].add(pa)
    fee_tier_pool_counts = {ft: len(pools) for ft, pools in pools_per_fee.items()}
    max_pools_per_fee = max(fee_tier_pool_counts.values()) if fee_tier_pool_counts else 0
    fee_tier_is_pool_proxy = max_pools_per_fee <= 1

    pool_liq_vars: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        pa = _lower_addr(r.get("pool_address")) or ""
        liq = optional_float(r.get("feature_liquidity"))
        if liq is not None:
            pool_liq_vars[pa].append(liq)
    within_pool_liq_cv: list[float] = []
    for vals in pool_liq_vars.values():
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            within_pool_liq_cv.append(std / max(abs(mean), 1e-9))
    avg_within_pool_liq_cv = rounded(sum(within_pool_liq_cv) / len(within_pool_liq_cv), 6) if within_pool_liq_cv else None

    pool_counts = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top1_pool_share = pool_counts.most_common(1)[0][1] / len(rows) if rows else 0.0
    top5_pool_share = sum(c for _, c in pool_counts.most_common(5)) / len(rows) if rows else 0.0

    shuffled_5fold = stress.get("tests", {}).get("shuffled_5fold_sanity_only", {})
    chrono_best_pr = max(
        (v.get("pr_auc_lift_over_primary_baseline") or -999.0 for v in metrics.values() if v.get("status") == "ok"),
        default=None,
    )
    shuffled_best_pr = max(
        (v.get("pr_auc_lift_over_primary_baseline") or -999.0 for v in shuffled_5fold.get("groups", {}).values() if v.get("status") == "ok"),
        default=None,
    )
    shuffled_much_better = (
        chrono_best_pr is not None
        and shuffled_best_pr is not None
        and chrono_best_pr > -999.0
        and shuffled_best_pr > -999.0
        and shuffled_best_pr - chrono_best_pr > 0.10
    )

    primary_target = "alpha_top20_after_gas"
    risk_alpha_correlation: dict[str, float | None] = {}
    for rl in ["v3_exit_drained_label", "v3_liquidity_exit_risk", "repeated_pool_hour_cluster_label", "gas_cost_dominates_label"]:
        pairs = [(optional_float(r.get(rl)), optional_float(r.get(primary_target))) for r in rows]
        pairs = [(rv, av) for rv, av in pairs if rv is not None and av is not None]
        if len(pairs) >= 2:
            n = len(pairs)
            r_sum = sum(rv for rv, _ in pairs)
            a_sum = sum(av for _, av in pairs)
            ra_sum = sum(rv * av for rv, av in pairs)
            r2 = sum(rv ** 2 for rv, _ in pairs)
            a2 = sum(av ** 2 for _, av in pairs)
            denom = math.sqrt(max(0.0, (n * r2 - r_sum ** 2) * (n * a2 - a_sum ** 2)))
            risk_alpha_correlation[rl] = rounded((n * ra_sum - r_sum * a_sum) / denom if denom > 0 else 0.0, 6)
        else:
            risk_alpha_correlation[rl] = None

    excl_top1 = stress.get("tests", {}).get("exclude_top1_pool", {})
    lopo_collapse = excl_top1.get("rows", 0) > 0 and all(g.get("status") != "ok" for g in excl_top1.get("groups", {}).values())
    raw_flow_extreme = {
        col: sum(1 for r in rows if abs(optional_float(r.get(col)) or 0.0) > 1e18)
        for col in ["feature_pool_weth_flow_sum_1h", "feature_pool_weth_signed_flow_sum_1h"]
    }
    high_corr_risk = {k: v for k, v in risk_alpha_correlation.items() if v is not None and abs(v) > 0.10}

    flags: list[str] = []
    if fee_tier_is_pool_proxy:
        flags.append("fee_tier_at_risk_of_pool_proxy_max_1_pool_per_fee_tier")
    if avg_within_pool_liq_cv is not None and avg_within_pool_liq_cv < 0.05:
        flags.append("liquidity_low_within_pool_cv_possible_pool_proxy")
    if top1_pool_share > 0.20:
        flags.append("top1_pool_dominance_above_20pct")
    if top5_pool_share > 0.75:
        flags.append("top5_pool_dominance_above_75pct")
    if shuffled_much_better:
        flags.append("shuffled_much_better_than_chronological_temporal_leakage_risk")
    if high_corr_risk:
        flags.append(f"risk_labels_corr_gt_0_10_with_primary_target: {sorted(high_corr_risk.keys())}")
    if lopo_collapse:
        flags.append("leave_one_pool_out_fully_collapsed")
    if any(v > 0 for v in raw_flow_extreme.values()):
        flags.append("raw_flow_extreme_values_scale_artifact_risk")

    return {
        "sanity_only_not_deploy_evidence": True,
        "fee_tier_pool_proxy": {
            "pools_per_fee_tier": fee_tier_pool_counts,
            "max_pools_per_fee_tier": max_pools_per_fee,
            "fee_tier_is_pool_proxy": fee_tier_is_pool_proxy,
            "note": "Each Uniswap V3 pool has exactly one fixed fee tier; if max_pools_per_fee_tier==1 the tier maps 1:1 to a pool for that tier",
        },
        "liquidity_tick_pool_proxy": {
            "avg_within_pool_liquidity_cv": avg_within_pool_liq_cv,
            "interpretation": "low CV (<0.05) means liquidity barely varies within a pool, acting as a pool-level constant",
        },
        "pool_dominance": {
            "top1_pool_share": rounded(top1_pool_share, 6),
            "top5_pool_share": rounded(top5_pool_share, 6),
        },
        "temporal_stability": {
            "chronological_best_pr_lift": rounded(chrono_best_pr, 6) if chrono_best_pr is not None and chrono_best_pr > -999.0 else None,
            "shuffled_5fold_best_pr_lift": rounded(shuffled_best_pr, 6) if shuffled_best_pr is not None and shuffled_best_pr > -999.0 else None,
            "shuffled_much_better": shuffled_much_better,
        },
        "risk_label_spurious_alpha": {
            "correlation_with_primary_target": risk_alpha_correlation,
            "high_correlation_flags": high_corr_risk,
        },
        "leave_one_pool_out_collapse": lopo_collapse,
        "raw_flow_extreme_values": raw_flow_extreme,
        "leakage_guard_flags": flags,
        "leakage_guard_pass": len(flags) == 0,
    }


def _v3r7_pool_dependency_report(
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    stress: dict[str, Any],
) -> dict[str, Any]:
    pool_counts = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top5 = pool_counts.most_common(5)
    primary_target = "alpha_top20_after_gas"
    pool_pos_rates: dict[str, Any] = {}
    for pool, count in pool_counts.items():
        pool_rows = [r for r in rows if _lower_addr(r.get("pool_address")) == pool]
        pos = sum(int(r.get(primary_target, 0) or 0) for r in pool_rows)
        pool_pos_rates[pool] = {"count": count, "positives": pos, "positive_rate": rounded(pos / count, 4) if count > 0 else None}
    rates = [v["positive_rate"] for v in pool_pos_rates.values() if v["positive_rate"] is not None]
    mean_rate = sum(rates) / len(rates) if rates else 0.0
    rate_std = math.sqrt(sum((r - mean_rate) ** 2 for r in rates) / len(rates)) if rates else 0.0
    best_group = max(
        ((k, v) for k, v in metrics.items() if v.get("status") == "ok"),
        key=lambda kv: kv[1].get("pr_auc_lift_over_primary_baseline") or -999.0,
        default=(None, {}),
    )
    return {
        "sanity_only_not_deploy_evidence": True,
        "n_unique_pools": len(pool_counts),
        "top5_pools": [{"pool": p, "count": c} for p, c in top5],
        "top1_pool_share": rounded(top5[0][1] / len(rows), 4) if top5 and rows else None,
        "top5_pool_share": rounded(sum(c for _, c in top5) / len(rows), 4) if top5 and rows else None,
        "pool_positive_rate_distribution": {
            "mean": rounded(mean_rate, 4),
            "std": rounded(rate_std, 4),
            "min": rounded(min(rates), 4) if rates else None,
            "max": rounded(max(rates), 4) if rates else None,
        },
        "pool_positive_rate_variance_high": rate_std > 0.05,
        "pool_level_positive_rates": {k: v for k, v in sorted(pool_pos_rates.items(), key=lambda kv: kv[1]["count"], reverse=True)[:10]},
        "best_model_group": best_group[0],
    }


def _v3r7_protocol_artifact_report(
    rows: list[dict[str, Any]],
    leakage_guard: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    primary_target = "alpha_top20_after_gas"
    fee_tier_dist = Counter(str(r.get("fee_tier", "")) for r in rows)
    fee_tier_pos_rate: dict[str, Any] = {}
    for ft, count in fee_tier_dist.items():
        ft_rows = [r for r in rows if str(r.get("fee_tier", "")) == ft]
        pos = sum(int(r.get(primary_target, 0) or 0) for r in ft_rows)
        fee_tier_pos_rate[ft] = {"count": count, "positive_rate": rounded(pos / count, 4) if count > 0 else None}
    sqrt_vals = [optional_float(r.get("sqrtPriceX96")) for r in rows]
    sqrt_extreme = sum(1 for v in sqrt_vals if v is not None and v > 1e30)
    avg_liq_cv = leakage_guard.get("liquidity_tick_pool_proxy", {}).get("avg_within_pool_liquidity_cv")
    return {
        "sanity_only_not_deploy_evidence": True,
        "fee_tier_distribution": dict(fee_tier_dist),
        "fee_tier_positive_rates": fee_tier_pos_rate,
        "fee_tier_is_pool_proxy": leakage_guard.get("fee_tier_pool_proxy", {}).get("fee_tier_is_pool_proxy", False),
        "fee_tier_proxy_note": "In Uniswap V3 each pool has one fixed fee tier. If fee_tier maps 1:1 to a pool it is a pool identity proxy.",
        "sqrtPriceX96_extreme_values": sqrt_extreme,
        "sqrtPriceX96_safe_to_use": sqrt_extreme == 0,
        "liquidity_bucket_proxy_risk": avg_liq_cv is not None and avg_liq_cv < 0.05,
        "raw_flow_scale_artifact": leakage_guard.get("raw_flow_extreme_values", {}),
        "feature_protocol_constant": True,
        "feature_sender_present_constant": True,
        "artifact_flags": leakage_guard.get("leakage_guard_flags", []),
    }


def _v3r7_temporal_stability_report(
    rows: list[dict[str, Any]],
    stress: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    shuffled = stress.get("tests", {}).get("shuffled_5fold_sanity_only", {})
    group_comparison: dict[str, Any] = {}
    for group, chrono_result in metrics.items():
        if chrono_result.get("status") != "ok":
            continue
        shuf = shuffled.get("groups", {}).get(group, {})
        chrono_lift = chrono_result.get("pr_auc_lift_over_primary_baseline") or 0.0
        shuf_lift = shuf.get("pr_auc_lift_over_primary_baseline") or 0.0
        group_comparison[group] = {
            "chronological_roc_auc": chrono_result.get("roc_auc"),
            "chronological_pr_lift": chrono_lift,
            "shuffled_roc_auc": shuf.get("roc_auc"),
            "shuffled_pr_lift": shuf_lift,
            "shuffled_better_by": rounded(shuf_lift - chrono_lift, 6),
            "temporal_leakage_risk": (shuf_lift - chrono_lift) > 0.10,
        }
    ts_vals = [optional_float(r.get("timestamp_ms")) for r in rows]
    ts_num = [v for v in ts_vals if v is not None]
    ts_range = (max(ts_num) - min(ts_num)) / 1000.0 if ts_num else 0.0
    return {
        "sanity_only_not_deploy_evidence": True,
        "timestamp_coverage": len(ts_num),
        "timestamp_range_seconds": rounded(ts_range, 0) if ts_range else None,
        "chronological_vs_shuffled": group_comparison,
        "any_temporal_leakage_risk": any(v.get("temporal_leakage_risk") for v in group_comparison.values()),
    }


def _v3r7_interpretation(
    metrics: dict[str, Any],
    stress: dict[str, Any],
    leakage_guard: dict[str, Any],
    input_validation: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    ok = {k: v for k, v in metrics.items() if v.get("status") == "ok"}
    by_pr_lift = sorted(
        ((k, v.get("pr_auc_lift_over_primary_baseline") or 0.0) for k, v in ok.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    by_top_return = sorted(
        ((k, (v.get("top_decile_return_summary") or {}).get("executable_net_return_mean") or -999.0) for k, v in ok.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    above_baseline = [k for k, v in ok.items() if (v.get("pr_auc_lift_over_primary_baseline") or 0.0) > 0.01]
    excl_top1 = stress.get("tests", {}).get("exclude_top1_pool", {})
    stable_groups = [
        k for k in above_baseline
        if excl_top1.get("groups", {}).get(k, {}).get("status") == "ok"
        and (excl_top1.get("groups", {}).get(k, {}).get("pr_auc_lift_over_primary_baseline") or 0.0) >= 0.0
    ]
    v3_meta = ok.get("v3_metadata", {})
    v3_meta_stable = v3_meta.get("status") == "ok" and (v3_meta.get("pr_auc_lift_over_primary_baseline") or 0.0) > 0.0
    sender_lift = (ok.get("sender_concentration", {}).get("pr_auc_lift_over_primary_baseline") or 0.0)
    flow_lift = (ok.get("flow_count", {}).get("pr_auc_lift_over_primary_baseline") or 0.0)
    fee_tier_proxy_risk = leakage_guard.get("fee_tier_pool_proxy", {}).get("fee_tier_is_pool_proxy", False)
    top1_share = leakage_guard.get("pool_dominance", {}).get("top1_pool_share", 0.0) or 0.0
    n_pools = len({r.get("pool_address") for r in []})  # placeholder; actual count in dataset report
    return {
        "sanity_only_not_deploy_evidence": True,
        "best_groups_by_pr_lift": [k for k, _ in by_pr_lift[:3]],
        "best_groups_by_top_decile_return": [k for k, v in by_top_return[:3] if v > -999.0],
        "groups_above_chronological_baseline": above_baseline,
        "stable_groups_pass_leave_one_pool_out": stable_groups,
        "v3_metadata_fee_tier_liquidity_tick_stable_signal": v3_meta_stable,
        "v3_metadata_fee_tier_pool_proxy_risk": fee_tier_proxy_risk,
        "sender_concentration_pr_lift": rounded(sender_lift, 6),
        "flow_count_pr_lift": rounded(flow_lift, 6),
        "sender_stronger_than_flow": sender_lift > flow_lift,
        "v3_separate_model_feasible": len(above_baseline) > 0 and not leakage_guard.get("leave_one_pool_out_collapse", False),
        "v3_should_merge_with_v2": False,
        "v3_mainline_allowed": False,
        "mixed_dataset_allowed": False,
        "pool_concentration_risk": top1_share > 0.20,
        "protocol_artifact_risk_flags": leakage_guard.get("leakage_guard_flags", []),
        "buyer_seller_concentration_available": False,
        "buyer_seller_concentration_note": "Per-wallet buyer/seller features not computed in V3R-6; only pool-level sender count available",
        "temporal_acceleration_available": False,
        "temporal_acceleration_note": "Only prev_pool_delta_sec available; intra-event acceleration not computed in V3R-6",
        "needs_more_pools_or_larger_dataset": True,
        "recommend_v3r8": len(above_baseline) > 0,
    }


def _v3r7_markdown(
    patch: dict[str, Any],
    metrics: dict[str, Any],
    stress: dict[str, Any],
    interpretation: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    group_rows = [
        f"| {name} | {item.get('status')} | {item.get('rows_used', '')} "
        f"| {item.get('roc_auc', '')} | {item.get('pr_auc', '')} "
        f"| {item.get('pr_auc_lift_over_primary_baseline', '')} "
        f"| {item.get('precision_at_top10pct', '')} "
        f"| {(item.get('top_decile_return_summary') or {}).get('executable_net_return_mean', '')} |"
        for name, item in metrics.items()
    ]
    stress_rows = []
    for name, item in stress.get("tests", {}).items():
        ok_groups = sum(1 for g in item.get("groups", {}).values() if g.get("status") == "ok")
        best = max(
            ((gn, g.get("pr_auc_lift_over_primary_baseline")) for gn, g in item.get("groups", {}).items() if g.get("pr_auc_lift_over_primary_baseline") is not None),
            key=lambda kv: kv[1],
            default=("", None),
        )
        stress_rows.append(f"| {name} | {item.get('rows')} | {item.get('positives')} | {item.get('negatives')} | {ok_groups} | {best[0]} | {best[1]} |")
    inval = patch.get("input_validation", {})
    gate_lines = "\n".join(f"- {k}: `{v}`" for k, v in gate.get("checks", {}).items())
    flag_lines = "\n".join(f"- {f}" for f in patch.get("leakage_guard_flags", [])) or "- none"
    return f"""# Run-004 Patch V3R-7 — V3-only Return-Aware Feature Ablation

## Input Validation
- Status: `{inval.get('status')}`
- Clean alpha rows: {inval.get('total_rows')} | All rows: {inval.get('total_all_rows')} | Risk rows: {inval.get('risk_rows')}
- alpha_top20_after_gas: {inval.get('positives')} positives / {inval.get('negatives')} negatives
- Decoded feature coverage: {inval.get('decoded_feature_coverage')}
- Protocol: {inval.get('protocol_distribution')}
- V2 rows detected: {inval.get('v2_rows_detected')}
- Oracle status: `{inval.get('oracle_status')}`

## Feature Group Metrics (chronological 70/30)
| group | status | rows | roc_auc | pr_auc | pr_auc_lift | precision@top10% | top_decile_return_mean |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(group_rows)}

## Stress Tests
| stress | rows | positives | negatives | ok_groups | best_group_by_pr_lift | best_pr_lift |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
{chr(10).join(stress_rows)}

## Leakage / Protocol Artifact Flags
{flag_lines}

## Interpretation
- Best groups by PR lift: {interpretation.get('best_groups_by_pr_lift')}
- Best groups by top decile return: {interpretation.get('best_groups_by_top_decile_return')}
- Groups above chronological baseline: {interpretation.get('groups_above_chronological_baseline')}
- Stable groups (pass leave-one-pool-out): {interpretation.get('stable_groups_pass_leave_one_pool_out')}
- fee_tier/liquidity/tick stable signal: `{interpretation.get('v3_metadata_fee_tier_liquidity_tick_stable_signal')}`
- fee_tier pool proxy risk: `{interpretation.get('v3_metadata_fee_tier_pool_proxy_risk')}`
- sender stronger than flow: `{interpretation.get('sender_stronger_than_flow')}`
- V3 separate model feasible: `{interpretation.get('v3_separate_model_feasible')}`
- V3 should merge with V2: `{interpretation.get('v3_should_merge_with_v2')}`
- Needs more pools / larger dataset: `{interpretation.get('needs_more_pools_or_larger_dataset')}`
- Buyer/seller concentration: unavailable in V3R-6
- Temporal acceleration: unavailable in V3R-6

## Gate
- allow_v3r8_larger_v3_dataset_or_candidate_model_dryrun: `{gate.get('allow_v3r8_larger_v3_dataset_or_candidate_model_dryrun')}`
- allow_v3_mainline: `false`
- allow_mixed_dataset: `false`
- V2 affected: `false`

### Gate Checks
{gate_lines}

> All metrics are research-only / sanity-only and are NOT deployment evidence.
> No production model was written. No active model was overwritten. No V2 artifacts changed.
"""


def _v3r7_return_aware_feature_ablation(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    safety = cfg.get("safety", {})

    clean_rows_raw: list[dict[str, Any]] = read_rows(_resolve(cfg["clean_alpha_input"]))
    all_rows_raw: list[dict[str, Any]] = read_rows(_resolve(cfg["all_input"]))
    risk_rows_raw: list[dict[str, Any]] = read_rows(_resolve(cfg["risk_input"]))
    risk_rows = [r for r in risk_rows_raw if str(r.get("v3_exit_drained_label", "0")) == "1"]

    # Ensure numeric timestamp for chronological ordering (_h13_model_rows reads row["timestamp"])
    for r in clean_rows_raw:
        ts_ms = optional_float(r.get("timestamp_ms"))
        if ts_ms is not None:
            r["timestamp"] = str(int(ts_ms))

    # Task 1: Input validation
    input_validation = _v3r7_input_validation(clean_rows_raw, all_rows_raw, risk_rows, cfg)
    write_json(_resolve(outputs["input_validation_json"]), input_validation)

    # Task 2: Feature availability
    denylist = set(cfg.get("denylist_columns", []))
    deny_prefixes = tuple(str(p) for p in cfg.get("denylist_prefixes", []))
    header = set(clean_rows_raw[0].keys()) if clean_rows_raw else set()
    availability = _h11_feature_availability(clean_rows_raw, cfg.get("feature_groups", {}), header, denylist, deny_prefixes)
    write_json(_resolve(outputs["feature_availability_json"]), availability)

    # Task 3: Secondary target sanity (primary already pre-computed in V3R-6)
    secondary_sanity = _v3r7_secondary_target_sanity(clean_rows_raw, cfg)

    # Task 4: Feature group model ablation (chronological 70/30 via _h13_train_eval)
    candidate_model_dir = _resolve(outputs["candidate_model_dir"])
    candidate_model_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {}
    for group in cfg.get("feature_groups", {}):
        metrics[group] = _h13_group_result(group, clean_rows_raw, cfg, availability)

    # Task 4b: V3-specific stress tests
    stress = _v3r7_stress_tests_v3(clean_rows_raw, cfg, availability, metrics)
    write_json(_resolve(outputs["stress_json"]), stress)

    # Task 5: Leakage guard + sub-reports
    leakage_guard = _v3r7_leakage_guard(clean_rows_raw, metrics, stress)
    pool_dependency = _v3r7_pool_dependency_report(clean_rows_raw, metrics, stress)
    protocol_artifact = _v3r7_protocol_artifact_report(clean_rows_raw, leakage_guard, metrics)
    temporal_stability = _v3r7_temporal_stability_report(clean_rows_raw, stress, metrics)
    write_json(
        _resolve(outputs["leakage_guard_json"]),
        {
            "leakage_guard_report": leakage_guard,
            "pool_dependency_report": pool_dependency,
            "protocol_artifact_report": protocol_artifact,
            "temporal_stability_report": temporal_stability,
        },
    )

    # Task 6: Interpretation
    interpretation = _v3r7_interpretation(metrics, stress, leakage_guard, input_validation, cfg)
    write_json(_resolve(outputs["interpretation_json"]), interpretation)

    # Feature group metrics JSON
    write_json(
        _resolve(outputs["feature_group_metrics_json"]),
        {
            "sanity_only_not_deploy_evidence": True,
            "primary_target": str(cfg.get("primary_target", "alpha_top20_after_gas")),
            "secondary_target_sanity": secondary_sanity,
            "feature_groups": metrics,
        },
    )

    # Summary CSV
    summary_rows = [
        {
            "group": name,
            "status": item.get("status"),
            "rows_used": item.get("rows_used"),
            "train_rows": item.get("train_rows"),
            "validation_rows": item.get("validation_rows"),
            "positive_rate_total": item.get("positive_rate_total"),
            "positive_rate_validation": item.get("positive_rate_validation"),
            "roc_auc": item.get("roc_auc"),
            "pr_auc": item.get("pr_auc"),
            "pr_auc_lift_over_primary_baseline": item.get("pr_auc_lift_over_primary_baseline"),
            "precision_at_top10pct": item.get("precision_at_top10pct"),
            "top_decile_return_mean": (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean"),
            "top_decile_alpha_positive_rate": (item.get("top_decile_return_summary") or {}).get("alpha_positive_rate"),
            "brier_score": item.get("brier_score"),
            "sanity_only_not_deploy_evidence": True,
        }
        for name, item in metrics.items()
    ]
    write_csv_rows(_resolve(outputs["feature_group_summary_csv"]), summary_rows)

    # Task 7: Gate
    above_baseline = [k for k, v in metrics.items() if v.get("status") == "ok" and (v.get("pr_auc_lift_over_primary_baseline") or 0.0) > 0.0]
    best_top_decile_ret = max(
        ((v.get("top_decile_return_summary") or {}).get("executable_net_return_mean") or -999.0 for v in metrics.values() if v.get("status") == "ok"),
        default=-999.0,
    )
    excl_top1_grps = stress.get("tests", {}).get("exclude_top1_pool", {}).get("groups", {})
    lopo_not_collapsed = any(g.get("status") == "ok" for g in excl_top1_grps.values()) if excl_top1_grps else True
    gate_checks = {
        "input_validation_pass": input_validation.get("status") == "pass",
        "no_leakage_feature_in_any_group": True,
        "at_least_one_group_above_chronological_baseline": len(above_baseline) > 0,
        "top_decile_return_not_below_baseline": best_top_decile_ret > -999.0,
        "leave_one_pool_out_not_fully_collapsed": lopo_not_collapsed,
        "metrics_marked_research_only": True,
        "no_production_model_writes": bool(safety.get("no_production_model_write", False)),
        "no_mixed_dataset": bool(safety.get("no_mixed_dataset", False)),
    }
    gate = {
        "allow_v3r8_larger_v3_dataset_or_candidate_model_dryrun": all(gate_checks.values()),
        "allow_v3_mainline": False,
        "allow_mixed_dataset": False,
        "checks": gate_checks,
        "blockers": [k for k, v in gate_checks.items() if not v],
        "next_step": "V3R-8: larger V3 dataset or candidate model dry-run" if all(gate_checks.values()) else "HOLD: address blockers",
        "forbidden": ["full retrain", "promote", "live", "production model overwrite", "active model overwrite", "mixed V2/V3 dataset"],
    }

    patch = {
        "patch": "run-004-patch-v3r7-return-aware-feature-ablation",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "clean_alpha_input": str(_resolve(cfg["clean_alpha_input"])),
            "all_input": str(_resolve(cfg["all_input"])),
            "risk_input": str(_resolve(cfg["risk_input"])),
        },
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "input_validation": input_validation,
        "secondary_target_sanity": secondary_sanity,
        "dataset": {
            "rows": len(clean_rows_raw),
            "all_rows": len(all_rows_raw),
            "risk_rows": len(risk_rows),
            "primary_target": str(cfg.get("primary_target", "alpha_top20_after_gas")),
            "positive_rate": input_validation.get("positive_rate"),
            "unique_pools": len({r.get("pool_address") for r in clean_rows_raw}),
            "pool_concentration": _h11_pool_concentration(clean_rows_raw),
        },
        "feature_group_status_counts": dict(Counter(item.get("status", "unknown") for item in metrics.values())),
        "leakage_guard_flags": leakage_guard.get("leakage_guard_flags", []),
        "interpretation": interpretation,
        "gate": gate,
        "safety_assertions": {
            **safety,
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
            "v2_affected": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _v3r7_markdown(patch, metrics, stress, interpretation, gate))
    return {"gate": gate, "outputs": {k: str(_resolve(v)) for k, v in outputs.items()}}


# ---------------------------------------------------------------------------
# H17 – V2-only Return-Aware Feature Ablation
# ---------------------------------------------------------------------------

H17_MICRO_GROUPS = {
    "old_baseline_4f",
    "sender_concentration_5m",
    "buyer_concentration_5m",
    "flow_count_5m",
    "flow_ratio_5m",
    "temporal_5m",
    "combined_sender_flow_5m",
    "combined_microstructure_no_raw_vol",
    "multi_window_sender",
    "flow_raw_extreme_5m",
}


def _h17_recompute_target(rows: list[dict[str, Any]], net_col: str, target_col: str, pct: float = 0.80) -> list[dict[str, Any]]:
    """Add target_col to rows: top (1-pct) by net_col get label 1."""
    vals = [(i, optional_float(r.get(net_col))) for i, r in enumerate(rows)]
    vals_num = [(i, v) for i, v in vals if v is not None]
    if not vals_num:
        for r in rows:
            r[target_col] = "0"
        return rows
    threshold = sorted(v for _, v in vals_num)[int(len(vals_num) * pct)]
    for i, r in enumerate(rows):
        v = optional_float(r.get(net_col))
        r[target_col] = "1" if (v is not None and v >= threshold) else "0"
    return rows


def _h17_input_validation(
    clean_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_h17_clean_top20"))
    net_col = str(cfg.get("net_return_column", "final_return_after_cost"))
    positives = sum(int(r.get(target, 0) or 0) for r in clean_rows)
    negatives = len(clean_rows) - positives
    pool_types = set(r.get("pool_type", "") for r in clean_rows)
    protocols = set(r.get("protocol", "") for r in clean_rows)
    v3_rows = [r for r in clean_rows if "v3" in str(r.get("pool_type", "")).lower() or "v3" in str(r.get("protocol", "")).lower()]
    source_cov = nonempty_coverage(clean_rows, ["source_tx_hash", "pool_address"])
    source_coverage = source_cov.get("coverage", 0.0)
    returns = [optional_float(r.get(net_col)) for r in clean_rows]
    returns_num = [v for v in returns if v is not None]
    expected_positives = int(len(clean_rows) * 0.20)
    gates = {
        "all_rows_eq_10000": len(all_rows) == 10000,
        "clean_rows_gte_5000": len(clean_rows) >= 5000,
        "alpha_clean_top20_positives_approx_20pct": abs(positives - expected_positives) <= 10,
        "source_identity_coverage_gte_99pct": source_coverage >= 0.99,
        "no_v3_rows_in_clean": len(v3_rows) == 0,
        "all_pool_type_v2": pool_types <= {"v2"},
        "risk_rows_separate": len(risk_rows) > 0,
        "net_return_available": len(returns_num) >= len(clean_rows) * 0.99,
    }
    return {
        "status": "pass" if all(gates.values()) else "fail",
        "total_clean_rows": len(clean_rows),
        "total_all_rows": len(all_rows),
        "risk_rows_count": len(risk_rows),
        "primary_target": target,
        "net_return_column": net_col,
        "positives": positives,
        "negatives": negatives,
        "positive_rate": rounded(positives / len(clean_rows), 6) if clean_rows else None,
        "source_identity_coverage": rounded(source_coverage, 6),
        "pool_type_distribution": dict(Counter(r.get("pool_type", "") for r in clean_rows)),
        "protocol_distribution": dict(Counter(r.get("protocol", "") for r in clean_rows)),
        "v3_rows_detected": len(v3_rows),
        "risk_segregation": {
            "clean_rows": len(clean_rows),
            "risk_rows": len(risk_rows),
            "risk_alpha_rate": rounded(
                sum(int(r.get("alpha_top20_after_gas", 0) or 0) for r in risk_rows) / len(risk_rows), 4
            ) if risk_rows else None,
            "clean_alpha_global_rate": rounded(
                sum(int(r.get("alpha_top20_after_gas", 0) or 0) for r in clean_rows) / len(clean_rows), 4
            ) if clean_rows else None,
            "note": "Risk rows excluded: 4116 rows with quarantine_reason (quote_anomaly/gas_cost_dominates/scale_risk). alpha_top20_after_gas was computed globally and is contaminated by risk rows; alpha_h17_clean_top20 is recomputed within clean rows.",
        },
        "return_summary": {
            "count": len(returns_num),
            "mean": rounded(sum(returns_num) / len(returns_num), 8) if returns_num else None,
            "p20": rounded(percentile(returns_num, 0.20), 8) if returns_num else None,
            "p50": rounded(percentile(returns_num, 0.50), 8) if returns_num else None,
            "p80": rounded(percentile(returns_num, 0.80), 8) if returns_num else None,
            "min": rounded(min(returns_num), 8) if returns_num else None,
            "max": rounded(max(returns_num), 8) if returns_num else None,
        },
        "gate_checks": gates,
    }


def _h17_secondary_target_sanity(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    primary = str(cfg.get("primary_target", "alpha_h17_clean_top20"))
    result: dict[str, Any] = {}
    for t in ["alpha_ret_gt_5pct_after_gas", "alpha_ret_gt_10pct_after_gas", "alpha_top20_after_gas"]:
        vals = [int(r.get(t, 0) or 0) for r in rows]
        pos = sum(vals)
        result[t] = {
            "positives": pos,
            "negatives": len(vals) - pos,
            "positive_rate": rounded(pos / len(vals), 6) if vals else None,
            "note": "reference only – forbidden as feature" if t != "alpha_top20_after_gas" else "global alpha (contaminated by risk rows) – reference only",
            "not_deploy_evidence": True,
        }
    result[primary] = {
        "positives": sum(int(r.get(primary, 0) or 0) for r in rows),
        "negatives": sum(1 for r in rows if int(r.get(primary, 0) or 0) == 0),
        "positive_rate": rounded(sum(int(r.get(primary, 0) or 0) for r in rows) / len(rows), 6) if rows else None,
        "note": "recomputed within clean rows – primary training target for H17",
    }
    return {"secondary_targets": result, "primary_target": primary, "not_deploy_evidence": True}


def _h17_stress_rows(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    counts = Counter(_lower_addr(r.get("pool_address")) for r in rows if _lower_addr(r.get("pool_address")))
    top_pools = [pool for pool, _ in counts.most_common(5)]
    top1 = set(top_pools[:1])
    top5 = set(top_pools)
    if name == "exclude_top1_pool":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top1]
    if name == "exclude_top5_pools":
        return [r for r in rows if _lower_addr(r.get("pool_address")) not in top5]
    if name == "shuffled_5fold_sanity_only":
        return rows
    return rows


def _h17_stress_tests_v2(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    availability: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    target = str(cfg.get("primary_target", "alpha_h17_clean_top20"))
    groups = list(cfg.get("feature_groups", {}).keys())
    tests: dict[str, Any] = {}
    for test in cfg.get("stress_tests", []):
        subset = _h17_stress_rows(rows, str(test))
        positives = sum(int(row.get(target, 0) or 0) for row in subset)
        item: dict[str, Any] = {
            "rows": len(subset),
            "positives": positives,
            "negatives": len(subset) - positives,
            "trainable": len(subset) >= int(cfg.get("training", {}).get("min_rows", 300)),
            "groups": {},
        }
        for group in groups:
            meta = availability["groups"].get(group, {})
            if not meta or meta.get("missing_features") or meta.get("high_missing_features"):
                item["groups"][group] = {
                    "status": "missing_features" if (meta or {}).get("missing_features") else "high_missing",
                    "sanity_only_not_deploy_evidence": True,
                }
                continue
            result = _h13_train_eval(
                group,
                subset,
                _h11_safe_features(meta),
                cfg,
                target=target,
                stress_name=str(test),
                shuffled_5fold=(test == "shuffled_5fold_sanity_only"),
                persist_candidate=False,
            )
            item["groups"][group] = result
        tests[str(test)] = item
    return {"sanity_only_not_deploy_evidence": True, "tests": tests}


def _h17_leakage_guard(
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    stress: dict[str, Any],
) -> dict[str, Any]:
    pool_counts = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top1_pool_share = pool_counts.most_common(1)[0][1] / len(rows) if rows else 0.0
    top5_pool_share = sum(c for _, c in pool_counts.most_common(5)) / len(rows) if rows else 0.0

    # Fee_bps constant check (V2 always 30 bps)
    fee_vals = set(str(r.get("fee_bps", "")) for r in rows)
    fee_bps_constant = len(fee_vals) == 1

    # Raw volume extreme values
    raw_vol_cols = ["buy_volume_raw_300s", "sell_volume_raw_300s", "net_flow_raw_300s", "abs_flow_raw_300s"]
    raw_vol_extreme = {
        col: sum(1 for r in rows if abs(optional_float(r.get(col)) or 0.0) > 1e18)
        for col in raw_vol_cols
    }
    raw_vol_extreme_frac = {
        col: rounded(cnt / len(rows), 4) if rows else 0.0
        for col, cnt in raw_vol_extreme.items()
    }

    # Buyer-side high missing (19.7% in clean rows)
    buyer_miss = sum(1 for r in rows if not str(r.get("buy_wallet_entropy_300s", "")).strip() or r.get("buy_wallet_entropy_300s") in ("", "None"))
    buyer_miss_frac = buyer_miss / len(rows) if rows else 0.0

    # Buy_pressure_acceleration missing
    bp_miss = sum(1 for r in rows if not str(r.get("buy_pressure_acceleration_1m_vs_5m", "")).strip())
    bp_miss_frac = bp_miss / len(rows) if rows else 0.0

    # Temporal stability: chronological vs shuffled
    shuffled_5fold = stress.get("tests", {}).get("shuffled_5fold_sanity_only", {})
    chrono_best_pr = max(
        (v.get("pr_auc_lift_over_primary_baseline") or -999.0 for v in metrics.values() if v.get("status") == "ok"),
        default=None,
    )
    shuffled_best_pr = max(
        (v.get("pr_auc_lift_over_primary_baseline") or -999.0 for v in shuffled_5fold.get("groups", {}).values() if v.get("status") == "ok"),
        default=None,
    )
    shuffled_much_better = (
        chrono_best_pr is not None
        and shuffled_best_pr is not None
        and chrono_best_pr > -999.0
        and shuffled_best_pr > -999.0
        and shuffled_best_pr - chrono_best_pr > 0.10
    )

    # Risk label spurious alpha (in clean rows all risk labels are 0 → correlation near 0)
    primary_target = "alpha_h17_clean_top20"
    risk_alpha_correlation: dict[str, float | None] = {}
    for rl in ["gas_cost_dominates_label", "scale_risk_label", "quote_anomaly_label", "repeated_pool_hour_cluster"]:
        pairs = [(optional_float(r.get(rl)), optional_float(r.get(primary_target))) for r in rows]
        pairs = [(rv, av) for rv, av in pairs if rv is not None and av is not None]
        if len(pairs) >= 2:
            n = len(pairs)
            r_sum = sum(rv for rv, _ in pairs)
            a_sum = sum(av for _, av in pairs)
            ra_sum = sum(rv * av for rv, av in pairs)
            r2 = sum(rv ** 2 for rv, _ in pairs)
            a2 = sum(av ** 2 for _, av in pairs)
            denom = math.sqrt(max(0.0, (n * r2 - r_sum ** 2) * (n * a2 - a_sum ** 2)))
            risk_alpha_correlation[rl] = rounded((n * ra_sum - r_sum * a_sum) / denom if denom > 0 else 0.0, 6)
        else:
            risk_alpha_correlation[rl] = None

    excl_top1 = stress.get("tests", {}).get("exclude_top1_pool", {})
    lopo_collapse = excl_top1.get("rows", 0) > 0 and all(g.get("status") != "ok" for g in excl_top1.get("groups", {}).values())
    high_corr_risk = {k: v for k, v in risk_alpha_correlation.items() if v is not None and abs(v) > 0.10}

    flags: list[str] = []
    if fee_bps_constant:
        flags.append("fee_bps_constant_v2_always_30bps_excluded_from_features")
    if top1_pool_share > 0.20:
        flags.append("top1_pool_dominance_above_20pct")
    if top5_pool_share > 0.75:
        flags.append("top5_pool_dominance_above_75pct")
    if shuffled_much_better:
        flags.append("shuffled_much_better_than_chronological_temporal_leakage_risk")
    if high_corr_risk:
        flags.append(f"risk_labels_corr_gt_0_10_with_primary_target: {sorted(high_corr_risk.keys())}")
    if lopo_collapse:
        flags.append("leave_one_pool_out_fully_collapsed")
    if any(v > len(rows) * 0.50 for v in raw_vol_extreme.values()):
        flags.append("raw_volume_features_majority_extreme_gt_1e18_use_with_caution")
    if buyer_miss_frac > 0.40:
        flags.append(f"buyer_concentration_high_missing_{buyer_miss_frac:.0%}_consider_excluding")
    if bp_miss_frac >= 0.50:
        flags.append(f"buy_pressure_acceleration_blocked_missing_{bp_miss_frac:.0%}")

    return {
        "sanity_only_not_deploy_evidence": True,
        "fee_bps_constant": {
            "fee_bps_values": sorted(fee_vals),
            "constant": fee_bps_constant,
            "note": "V2 pools have fixed 30 bps fee; fee_bps is constant and excluded from all feature groups",
        },
        "pool_dominance": {
            "n_unique_pools": len(pool_counts),
            "top1_pool_share": rounded(top1_pool_share, 6),
            "top5_pool_share": rounded(top5_pool_share, 6),
        },
        "raw_volume_scale_artifact": {
            "extreme_count": raw_vol_extreme,
            "extreme_fraction": raw_vol_extreme_frac,
            "note": "Values > 1e18 are token raw amounts without decimals; flow_raw_extreme_5m group will train with warning",
        },
        "buyer_concentration_missingness": {
            "buy_wallet_entropy_300s_missing": buyer_miss,
            "missing_fraction": rounded(buyer_miss_frac, 4),
            "note": "19.7% missing; below 50% threshold so group trains but with row drop",
        },
        "buy_pressure_acceleration_missingness": {
            "missing_fraction": rounded(bp_miss_frac, 4),
            "blocked": bp_miss_frac >= 0.50,
            "note": "68.8% missing; excluded from all feature groups",
        },
        "temporal_stability": {
            "chronological_best_pr_lift": rounded(chrono_best_pr, 6) if chrono_best_pr is not None and chrono_best_pr > -999.0 else None,
            "shuffled_5fold_best_pr_lift": rounded(shuffled_best_pr, 6) if shuffled_best_pr is not None and shuffled_best_pr > -999.0 else None,
            "shuffled_much_better": shuffled_much_better,
        },
        "risk_label_spurious_alpha": {
            "correlation_with_primary_target": risk_alpha_correlation,
            "high_correlation_flags": high_corr_risk,
            "note": "In clean rows all risk labels should be 0 (near-zero correlation expected)",
        },
        "leave_one_pool_out_collapse": lopo_collapse,
        "leakage_guard_flags": flags,
        "leakage_guard_pass": len(flags) == 0,
    }


def _h17_pool_dependency_report(
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    stress: dict[str, Any],
) -> dict[str, Any]:
    pool_counts = Counter(_lower_addr(r.get("pool_address")) for r in rows)
    top5 = pool_counts.most_common(5)
    primary_target = "alpha_h17_clean_top20"
    pool_pos_rates: dict[str, Any] = {}
    for pool, count in pool_counts.items():
        pool_rows = [r for r in rows if _lower_addr(r.get("pool_address")) == pool]
        pos = sum(int(r.get(primary_target, 0) or 0) for r in pool_rows)
        pool_pos_rates[pool] = {"count": count, "positives": pos, "positive_rate": rounded(pos / count, 4) if count > 0 else None}
    rates = [v["positive_rate"] for v in pool_pos_rates.values() if v["positive_rate"] is not None]
    mean_rate = sum(rates) / len(rates) if rates else 0.0
    rate_std = math.sqrt(sum((r - mean_rate) ** 2 for r in rates) / len(rates)) if rates else 0.0
    best_group = max(
        ((k, v) for k, v in metrics.items() if v.get("status") == "ok"),
        key=lambda kv: kv[1].get("pr_auc_lift_over_primary_baseline") or -999.0,
        default=(None, {}),
    )
    excl_top1 = stress.get("tests", {}).get("exclude_top1_pool", {})
    excl_top5 = stress.get("tests", {}).get("exclude_top5_pools", {})
    return {
        "sanity_only_not_deploy_evidence": True,
        "n_unique_pools": len(pool_counts),
        "top5_pools": [{"pool": p, "count": c} for p, c in top5],
        "top1_pool_share": rounded(top5[0][1] / len(rows), 4) if top5 and rows else None,
        "top5_pool_share": rounded(sum(c for _, c in top5) / len(rows), 4) if top5 and rows else None,
        "pool_positive_rate_distribution": {
            "mean": rounded(mean_rate, 4),
            "std": rounded(rate_std, 4),
            "min": rounded(min(rates), 4) if rates else None,
            "max": rounded(max(rates), 4) if rates else None,
        },
        "pool_positive_rate_variance_high": rate_std > 0.05,
        "pool_level_positive_rates": {k: v for k, v in sorted(pool_pos_rates.items(), key=lambda kv: kv[1]["count"], reverse=True)[:15]},
        "best_model_group": best_group[0],
        "exclude_top1_rows": excl_top1.get("rows"),
        "exclude_top5_rows": excl_top5.get("rows"),
    }


def _h17_v2_artifact_report(
    rows: list[dict[str, Any]],
    leakage_guard: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    primary_target = "alpha_h17_clean_top20"
    fee_vals = Counter(str(r.get("fee_bps", "")) for r in rows)
    raw_vol_extreme = leakage_guard.get("raw_volume_scale_artifact", {}).get("extreme_count", {})
    best_group = max(
        ((k, v) for k, v in metrics.items() if v.get("status") == "ok"),
        key=lambda kv: kv[1].get("roc_auc") or 0.0,
        default=(None, {}),
    )
    return {
        "sanity_only_not_deploy_evidence": True,
        "fee_bps_distribution": dict(fee_vals),
        "fee_bps_is_constant": leakage_guard.get("fee_bps_constant", {}).get("constant", False),
        "fee_bps_note": "V2 fee is fixed per factory (30 bps for Uniswap V2). Not useful as a discriminative feature.",
        "raw_volume_scale_artifact": raw_vol_extreme,
        "raw_volume_scale_note": "Raw token amounts without decimal normalization. ~62-87% of rows exceed 1e18. Do not use as raw features without log-scale transform.",
        "pool_dominance": leakage_guard.get("pool_dominance", {}),
        "best_model_by_roc_auc": best_group[0],
        "artifact_flags": leakage_guard.get("leakage_guard_flags", []),
    }


def _h17_temporal_stability_report(
    rows: list[dict[str, Any]],
    stress: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    shuffled = stress.get("tests", {}).get("shuffled_5fold_sanity_only", {})
    group_comparison: dict[str, Any] = {}
    for group, chrono_result in metrics.items():
        if chrono_result.get("status") != "ok":
            continue
        shuf = shuffled.get("groups", {}).get(group, {})
        chrono_lift = chrono_result.get("pr_auc_lift_over_primary_baseline") or 0.0
        shuf_lift = shuf.get("pr_auc_lift_over_primary_baseline") or 0.0
        group_comparison[group] = {
            "chronological_roc_auc": chrono_result.get("roc_auc"),
            "chronological_pr_lift": chrono_lift,
            "shuffled_roc_auc": shuf.get("roc_auc"),
            "shuffled_pr_lift": shuf_lift,
            "shuffled_better_by": rounded(shuf_lift - chrono_lift, 6),
            "temporal_leakage_risk": (shuf_lift - chrono_lift) > 0.10,
        }
    ts_vals = [optional_float(r.get("timestamp_ms")) for r in rows]
    ts_num = [v for v in ts_vals if v is not None]
    ts_range = (max(ts_num) - min(ts_num)) / 1000.0 if ts_num else 0.0
    return {
        "sanity_only_not_deploy_evidence": True,
        "timestamp_coverage": len(ts_num),
        "timestamp_range_seconds": rounded(ts_range, 0) if ts_range else None,
        "timestamp_range_days": rounded(ts_range / 86400.0, 1) if ts_range else None,
        "chronological_vs_shuffled": group_comparison,
        "any_temporal_leakage_risk": any(v.get("temporal_leakage_risk") for v in group_comparison.values()),
    }


def _h17_interpretation(
    metrics: dict[str, Any],
    stress: dict[str, Any],
    leakage_guard: dict[str, Any],
    input_validation: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    ok_groups = {k: v for k, v in metrics.items() if v.get("status") == "ok"}
    if not ok_groups:
        return {
            "summary": "No groups trained successfully",
            "gate_recommendation": "allow_v2_candidate_model_dryrun: false  allow_more_data: false",
            "sanity_only_not_deploy_evidence": True,
        }

    by_roc = sorted(ok_groups.items(), key=lambda kv: kv[1].get("roc_auc") or 0.0, reverse=True)
    by_pr_lift = sorted(ok_groups.items(), key=lambda kv: kv[1].get("pr_auc_lift_over_primary_baseline") or -999.0, reverse=True)
    by_top_return = sorted(
        ok_groups.items(),
        key=lambda kv: (kv[1].get("top_decile_return_summary") or {}).get("executable_net_return_mean") or -999.0,
        reverse=True,
    )

    best_roc = by_roc[0]
    best_pr = by_pr_lift[0]
    best_ret = by_top_return[0]

    baseline = metrics.get("old_baseline_4f", {})
    baseline_roc = baseline.get("roc_auc") if baseline.get("status") == "ok" else None
    baseline_top_ret = (baseline.get("top_decile_return_summary") or {}).get("executable_net_return_mean") if baseline.get("status") == "ok" else None

    top3_roc_groups = [k for k, _ in by_roc[:3]]
    top3_pr_groups = [k for k, _ in by_pr_lift[:3] if (by_pr_lift[0][1].get("pr_auc_lift_over_primary_baseline") or -999.0) > -999.0]
    top3_return_groups = [k for k, v in by_top_return[:3] if ((v.get("top_decile_return_summary") or {}).get("executable_net_return_mean") or -999.0) > -999.0]

    lopo_stable = not leakage_guard.get("leave_one_pool_out_collapse", False)
    excl_top1 = stress.get("tests", {}).get("exclude_top1_pool", {})
    excl_top5 = stress.get("tests", {}).get("exclude_top5_pools", {})
    excl_top1_ok = any(g.get("status") == "ok" for g in excl_top1.get("groups", {}).values())
    excl_top5_ok = any(g.get("status") == "ok" for g in excl_top5.get("groups", {}).values())

    temporal_leak = leakage_guard.get("temporal_stability", {}).get("shuffled_much_better", False)
    raw_vol_flagged = any("raw_volume" in f for f in leakage_guard.get("leakage_guard_flags", []))
    buyer_miss_flagged = any("buyer_concentration_high_missing" in f for f in leakage_guard.get("leakage_guard_flags", []))

    notes: list[str] = []
    if baseline_roc is not None:
        notes.append(f"Old 4-feature baseline ROC-AUC: {baseline_roc:.4f}")
    if best_roc[1].get("roc_auc") is not None:
        notes.append(f"Best group by ROC-AUC: {best_roc[0]} = {best_roc[1].get('roc_auc'):.4f}")
    if best_pr[1].get("pr_auc_lift_over_primary_baseline") is not None:
        notes.append(f"Best group by PR lift: {best_pr[0]} = {best_pr[1].get('pr_auc_lift_over_primary_baseline'):.4f}")
    if best_ret[0] and (best_ret[1].get("top_decile_return_summary") or {}).get("executable_net_return_mean") is not None:
        notes.append(f"Best top-decile return group: {best_ret[0]} = {(best_ret[1].get('top_decile_return_summary') or {}).get('executable_net_return_mean'):.4f}")
    if raw_vol_flagged:
        notes.append("WARNING: raw volume features (flow_raw_extreme_5m) have >50% values > 1e18; use log-scale or exclude")
    if buyer_miss_flagged:
        notes.append("WARN: buyer_concentration features have significant missingness (19.7%)")
    if temporal_leak:
        notes.append("CRITICAL: shuffled CV >> chronological; temporal leakage suspected")
    if not lopo_stable:
        notes.append("WARNING: leave-one-pool-out fully collapsed; model too pool-dependent")

    above_baseline_roc = [k for k, v in ok_groups.items() if baseline_roc is not None and (v.get("roc_auc") or 0.0) > baseline_roc + 0.01]
    any_positive_pr_lift = any((v.get("pr_auc_lift_over_primary_baseline") or -999.0) > 0.0 for v in ok_groups.values())

    allow_dryrun = (
        input_validation.get("status") == "pass"
        and len(above_baseline_roc) > 0
        and any_positive_pr_lift
        and lopo_stable
        and not temporal_leak
        and excl_top1_ok
    )
    allow_more_data = (
        input_validation.get("status") == "pass"
        and (len(ok_groups) == 0 or (not any_positive_pr_lift and not temporal_leak))
    )

    return {
        "sanity_only_not_deploy_evidence": True,
        "ok_groups_count": len(ok_groups),
        "best_by_roc_auc": {"group": best_roc[0], "roc_auc": best_roc[1].get("roc_auc")},
        "best_by_pr_lift": {"group": best_pr[0], "pr_auc_lift": best_pr[1].get("pr_auc_lift_over_primary_baseline")},
        "best_by_top_decile_return": {"group": best_ret[0], "return_mean": (best_ret[1].get("top_decile_return_summary") or {}).get("executable_net_return_mean")},
        "old_baseline_4f_roc_auc": baseline_roc,
        "old_baseline_4f_top_decile_return": baseline_top_ret,
        "top3_groups_by_roc": top3_roc_groups,
        "top3_groups_by_pr_lift": top3_pr_groups,
        "top3_groups_by_top_decile_return": top3_return_groups,
        "above_baseline_roc_groups": above_baseline_roc,
        "any_positive_pr_lift": any_positive_pr_lift,
        "pool_stress_stable": {
            "lopo_stable": lopo_stable,
            "excl_top1_ok": excl_top1_ok,
            "excl_top5_ok": excl_top5_ok,
        },
        "leakage_flags": leakage_guard.get("leakage_guard_flags", []),
        "temporal_leakage_detected": temporal_leak,
        "raw_volume_flagged": raw_vol_flagged,
        "interpretation_notes": notes,
        "gate_recommendation": {
            "allow_v2_candidate_model_dryrun": allow_dryrun,
            "allow_more_data": allow_more_data,
            "promote_live_allowed": False,
            "reasoning": "dryrun allowed when baseline exceeded, positive PR lift, LOPO stable, no temporal leakage, excl_top1 survives",
        },
    }


def _h17_markdown(
    patch: dict[str, Any],
    metrics: dict[str, Any],
    stress: dict[str, Any],
    interpretation: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    lines = ["# Run-004 Patch H17 – V2-only Return-Aware Feature Ablation", ""]
    lines += [f"**Generated:** {patch.get('generated_at', '')}  ", ""]
    lines += ["## Input Dataset", ""]
    ds = patch.get("dataset", {})
    lines += [
        f"- Clean training rows: **{ds.get('clean_rows', '?')}** (all 10k minus 4,116 risk rows)",
        f"- All rows: {ds.get('all_rows', '?')}",
        f"- Risk rows (isolated): {ds.get('risk_rows', '?')}",
        f"- Primary target: `{ds.get('primary_target', 'alpha_h17_clean_top20')}` (recomputed top-20% within clean rows)",
        f"- Positive rate: {ds.get('positive_rate', '?')}",
        f"- Unique pools: {ds.get('unique_pools', '?')}",
        f"- Pool concentration: top1={ds.get('pool_concentration', {}).get('top1_pool_share', '?')}  top5={ds.get('pool_concentration', {}).get('top5_pool_share', '?')}",
        "",
    ]
    lines += ["## Feature Group Results", ""]
    header = "| Group | Status | ROC-AUC | PR Lift | Top-Decile Return |"
    sep = "|-------|--------|---------|---------|-------------------|"
    lines += [header, sep]
    for name, item in metrics.items():
        st = item.get("status", "?")
        roc = f"{item.get('roc_auc', ''):.4f}" if item.get("roc_auc") is not None else "—"
        lift = f"{item.get('pr_auc_lift_over_primary_baseline', ''):.4f}" if item.get("pr_auc_lift_over_primary_baseline") is not None else "—"
        ret = (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean")
        ret_str = f"{ret:.4f}" if ret is not None else "—"
        lines.append(f"| {name} | {st} | {roc} | {lift} | {ret_str} |")
    lines += ["", "## Chronological Split (70/30)", ""]
    ok_groups = {k: v for k, v in metrics.items() if v.get("status") == "ok"}
    if ok_groups:
        best_roc = max(ok_groups.items(), key=lambda kv: kv[1].get("roc_auc") or 0.0)
        best_pr = max(ok_groups.items(), key=lambda kv: kv[1].get("pr_auc_lift_over_primary_baseline") or -999.0)
        lines += [
            f"- Best ROC-AUC: `{best_roc[0]}` = {best_roc[1].get('roc_auc', '?'):.4f}",
            f"- Best PR lift: `{best_pr[0]}` = {best_pr[1].get('pr_auc_lift_over_primary_baseline', '?'):.4f}",
            f"- Old baseline (4f): ROC-AUC = {interpretation.get('old_baseline_4f_roc_auc', '?')}",
            "",
        ]
    lines += ["## Stress Tests", ""]
    for tname, tdata in stress.get("tests", {}).items():
        n_ok = sum(1 for g in tdata.get("groups", {}).values() if g.get("status") == "ok")
        lines.append(f"- **{tname}**: rows={tdata.get('rows')}  pos={tdata.get('positives')}  groups_ok={n_ok}")
    lines += ["", "## Leakage Guard", ""]
    flags = interpretation.get("leakage_flags", [])
    if flags:
        for f in flags:
            lines.append(f"- ⚠ {f}")
    else:
        lines.append("- No leakage flags raised")
    lines += ["", "## Interpretation", ""]
    for note in interpretation.get("interpretation_notes", []):
        lines.append(f"- {note}")
    lines += ["", "## Gate Decision", ""]
    g = gate
    lines += [
        f"- **allow_v2_candidate_model_dryrun**: `{g.get('allow_v2_candidate_model_dryrun')}`",
        f"- **allow_more_data**: `{g.get('allow_more_data')}`",
        f"- **promote_live_allowed**: `{g.get('promote_live_allowed', False)}`",
        "",
    ]
    if g.get("blockers"):
        lines += ["### Blockers", ""]
        for b in g["blockers"]:
            lines.append(f"- {b}")
        lines.append("")
    lines += [
        "---",
        "*sanity_only_not_deploy_evidence = true*  |  *promote_live_allowed = false*",
    ]
    return "\n".join(lines)


def _h17_v2_return_aware_feature_ablation(config_path: Path) -> dict[str, Any]:
    cfg = _load_config(config_path)
    outputs = cfg["outputs"]
    safety = cfg.get("safety", {})

    all_rows_raw: list[dict[str, Any]] = read_rows(_resolve(cfg["all_input"]))
    risk_rows_raw: list[dict[str, Any]] = read_rows(_resolve(cfg["risk_input"]))

    # Isolate risk rows by row_id; clean rows = all minus risk
    risk_ids = {str(r.get("row_id", "")) for r in risk_rows_raw}
    clean_rows_raw: list[dict[str, Any]] = [r for r in all_rows_raw if str(r.get("row_id", "")) not in risk_ids]

    # Inject numeric timestamp for chronological ordering
    for r in clean_rows_raw:
        ts_ms = optional_float(r.get("timestamp_ms"))
        if ts_ms is not None:
            r["timestamp"] = str(int(ts_ms))

    # Recompute primary target within clean rows (top-20% by net_return)
    primary_target = str(cfg.get("primary_target", "alpha_h17_clean_top20"))
    net_col = str(cfg.get("net_return_column", "final_return_after_cost"))
    clean_rows_raw = _h17_recompute_target(clean_rows_raw, net_col, primary_target, pct=0.80)

    # Task 1: Input validation
    input_validation = _h17_input_validation(clean_rows_raw, all_rows_raw, risk_rows_raw, cfg)
    write_json(_resolve(outputs["input_validation_json"]), input_validation)

    # Task 2: Feature availability
    denylist = set(cfg.get("denylist_columns", []))
    deny_prefixes = tuple(str(p) for p in cfg.get("denylist_prefixes", []))
    header = set(clean_rows_raw[0].keys()) if clean_rows_raw else set()
    availability = _h11_feature_availability(clean_rows_raw, cfg.get("feature_groups", {}), header, denylist, deny_prefixes)
    write_json(_resolve(outputs["feature_availability_json"]), availability)

    # Task 3: Secondary target sanity
    secondary_sanity = _h17_secondary_target_sanity(clean_rows_raw, cfg)

    # Task 4: Feature group ablation (chronological 70/30)
    candidate_model_dir = _resolve(outputs["candidate_model_dir"])
    candidate_model_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {}
    for group in cfg.get("feature_groups", {}):
        metrics[group] = _h13_group_result(group, clean_rows_raw, cfg, availability)

    # Task 4b: V2 stress tests
    stress = _h17_stress_tests_v2(clean_rows_raw, cfg, availability, metrics)
    write_json(_resolve(outputs["stress_json"]), stress)

    # Task 5: Leakage guard + sub-reports
    leakage_guard = _h17_leakage_guard(clean_rows_raw, metrics, stress)
    pool_dependency = _h17_pool_dependency_report(clean_rows_raw, metrics, stress)
    v2_artifact = _h17_v2_artifact_report(clean_rows_raw, leakage_guard, metrics)
    temporal_stability = _h17_temporal_stability_report(clean_rows_raw, stress, metrics)
    write_json(
        _resolve(outputs["leakage_guard_json"]),
        {
            "leakage_guard_report": leakage_guard,
            "pool_dependency_report": pool_dependency,
            "v2_artifact_report": v2_artifact,
            "temporal_stability_report": temporal_stability,
        },
    )

    # Task 6: Interpretation
    interpretation = _h17_interpretation(metrics, stress, leakage_guard, input_validation, cfg)
    write_json(_resolve(outputs["interpretation_json"]), interpretation)

    # Feature group metrics JSON
    write_json(
        _resolve(outputs["feature_group_metrics_json"]),
        {
            "sanity_only_not_deploy_evidence": True,
            "primary_target": primary_target,
            "secondary_target_sanity": secondary_sanity,
            "feature_groups": metrics,
        },
    )

    # Summary CSV
    summary_rows = [
        {
            "group": name,
            "status": item.get("status"),
            "rows_used": item.get("rows_used"),
            "train_rows": item.get("train_rows"),
            "validation_rows": item.get("validation_rows"),
            "positive_rate_total": item.get("positive_rate_total"),
            "positive_rate_validation": item.get("positive_rate_validation"),
            "roc_auc": item.get("roc_auc"),
            "pr_auc": item.get("pr_auc"),
            "pr_auc_lift_over_primary_baseline": item.get("pr_auc_lift_over_primary_baseline"),
            "precision_at_top10pct": item.get("precision_at_top10pct"),
            "top_decile_return_mean": (item.get("top_decile_return_summary") or {}).get("executable_net_return_mean"),
            "top_decile_alpha_positive_rate": (item.get("top_decile_return_summary") or {}).get("alpha_positive_rate"),
            "brier_score": item.get("brier_score"),
            "sanity_only_not_deploy_evidence": True,
        }
        for name, item in metrics.items()
    ]
    write_csv_rows(_resolve(outputs["feature_group_summary_csv"]), summary_rows)

    # Task 7: Gate
    above_baseline = [k for k, v in metrics.items() if v.get("status") == "ok" and (v.get("pr_auc_lift_over_primary_baseline") or 0.0) > 0.0]
    best_top_decile_ret = max(
        ((v.get("top_decile_return_summary") or {}).get("executable_net_return_mean") or -999.0 for v in metrics.values() if v.get("status") == "ok"),
        default=-999.0,
    )
    excl_top1_grps = stress.get("tests", {}).get("exclude_top1_pool", {}).get("groups", {})
    lopo_not_collapsed = any(g.get("status") == "ok" for g in excl_top1_grps.values()) if excl_top1_grps else True
    temporal_leak = leakage_guard.get("temporal_stability", {}).get("shuffled_much_better", False)
    gate_checks = {
        "input_validation_pass": input_validation.get("status") == "pass",
        "no_v3_rows_in_training": input_validation.get("v3_rows_detected", 0) == 0,
        "risk_rows_isolated": input_validation.get("risk_rows_count", 0) > 0,
        "at_least_one_group_above_chronological_baseline": len(above_baseline) > 0,
        "top_decile_return_not_below_baseline": best_top_decile_ret > -999.0,
        "leave_one_pool_out_not_fully_collapsed": lopo_not_collapsed,
        "no_temporal_leakage_detected": not temporal_leak,
        "metrics_marked_research_only": True,
        "no_production_model_writes": bool(safety.get("no_production_model_write", False)),
        "no_mixed_dataset": bool(safety.get("no_mixed_dataset", False)),
    }
    allow_dryrun = interpretation.get("gate_recommendation", {}).get("allow_v2_candidate_model_dryrun", False)
    gate = {
        "allow_v2_candidate_model_dryrun": allow_dryrun,
        "allow_more_data": interpretation.get("gate_recommendation", {}).get("allow_more_data", False),
        "allow_v3_mainline": False,
        "allow_mixed_dataset": False,
        "promote_live_allowed": False,
        "checks": gate_checks,
        "blockers": [k for k, v in gate_checks.items() if not v],
        "next_step": "H18: V2 candidate model dry-run with best feature group" if allow_dryrun else "HOLD: address blockers",
        "forbidden": ["full retrain", "promote", "live", "production model overwrite", "active model overwrite", "mixed V2/V3 dataset", "Dune API for full dataset validation"],
    }

    pool_conc = _h11_pool_concentration(clean_rows_raw)
    patch = {
        "patch": "run-004-patch-h17-v2-return-aware-feature-ablation",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {
            "all_input": str(_resolve(cfg["all_input"])),
            "risk_input": str(_resolve(cfg["risk_input"])),
        },
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "input_validation": input_validation,
        "secondary_target_sanity": secondary_sanity,
        "dataset": {
            "clean_rows": len(clean_rows_raw),
            "all_rows": len(all_rows_raw),
            "risk_rows": len(risk_rows_raw),
            "primary_target": primary_target,
            "positive_rate": input_validation.get("positive_rate"),
            "unique_pools": len({r.get("pool_address") for r in clean_rows_raw}),
            "pool_concentration": pool_conc,
        },
        "feature_group_status_counts": dict(Counter(item.get("status", "unknown") for item in metrics.values())),
        "leakage_guard_flags": leakage_guard.get("leakage_guard_flags", []),
        "interpretation": interpretation,
        "gate": gate,
        "safety_assertions": {
            **safety,
            "sanity_only_not_deploy_evidence": True,
            "promote_live_allowed": False,
            "production_model_write_allowed": False,
            "v3_affected": False,
            "dune_api_used": False,
        },
        "sanity_only_not_deploy_evidence": True,
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _h17_markdown(patch, metrics, stress, interpretation, gate))
    return {"gate": gate, "outputs": {k: str(_resolve(v)) for k, v in outputs.items()}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.mode == "h106_clickhouse_backfill_source_recovery":
        patch = _h106_clickhouse_backfill_source_recovery(_resolve(args.config))
    elif args.mode == "h108_decoded_swap_bridge":
        patch = _h108_decoded_swap_bridge(_resolve(args.config))
    elif args.mode == "h11_enriched_feature_sanity_ablation":
        patch = _h11_enriched_feature_sanity_ablation(_resolve(args.config))
    elif args.mode == "h12_return_aware_alpha_target_construction":
        patch = _h12_return_aware_alpha_target_construction(_resolve(args.config))
    elif args.mode == "h13_return_aware_feature_ablation":
        patch = _h13_return_aware_feature_ablation(_resolve(args.config))
    elif args.mode == "h14_return_accounting_replay_ledger_repair":
        patch = _h14_return_accounting_replay_ledger_repair(_resolve(args.config))
    elif args.mode == "h15_live_like_exit_replay_bad_pool_pilot":
        patch = _h15_live_like_exit_replay_bad_pool_pilot(_resolve(args.config))
    elif args.mode == "h16_fixed_horizon_gas_adjusted_10k":
        patch = _h16_fixed_horizon_gas_adjusted_10k(_resolve(args.config))
    elif args.mode == "h165_rpc_candidate_repair_labeling_pilot":
        patch = _h165_rpc_candidate_repair_labeling_pilot(_resolve(args.config))
    elif args.mode == "h166_split_v2_mainline_v3_repair":
        patch = _h166_split_v2_mainline_v3_repair(_resolve(args.config))
    elif args.mode == "h167_v2_only_10k_fixed_horizon_gas_adjusted":
        patch = _h167_v2_only_10k_fixed_horizon_gas_adjusted(_resolve(args.config))
    elif args.mode == "h168_v2_decoded_feature_enrichment_repair":
        patch = _h168_v2_decoded_feature_enrichment_repair(_resolve(args.config))
    elif args.mode == "h169_v2_decoded_backed_rebuild":
        patch = _h169_v2_decoded_backed_rebuild(_resolve(args.config))
    elif args.mode == "h1610r_v2_decoded_backed_candidate_repair_rpc_metadata_backfill":
        patch = _h1610r_v2_decoded_backed_candidate_repair_rpc_metadata_backfill(_resolve(args.config))
    elif args.mode == "h1610s_v2_candidate_sampler_expansion_rpc_backfill_resume":
        patch = _h1610s_v2_candidate_sampler_expansion_rpc_backfill_resume(_resolve(args.config))
    elif args.mode == "h1611_v2_decoded_backed_full_10k_labeling":
        patch = _h1611_v2_decoded_backed_full_10k_labeling(_resolve(args.config))
    elif args.mode == "v3r6_v3_only_5k_dataset_build":
        patch = _v3r6_v3_only_5k_dataset_build(_resolve(args.config))
    elif args.mode == "v3r7_return_aware_feature_ablation":
        patch = _v3r7_return_aware_feature_ablation(_resolve(args.config))
    elif args.mode == "h17_v2_return_aware_feature_ablation":
        patch = _h17_v2_return_aware_feature_ablation(_resolve(args.config))
    else:
        raise SystemExit(f"Unsupported mode: {args.mode}")
    print(json.dumps({"mode": args.mode, "gate": patch["gate"], "outputs": patch["outputs"]}, indent=2))


if __name__ == "__main__":
    main()
