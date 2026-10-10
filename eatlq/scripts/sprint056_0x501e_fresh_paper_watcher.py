#!/usr/bin/env python3
"""Sprint056 - 0x501e Fresh Activity Paper Watcher.

Bounded, paper-only watcher:
- No live execution
- No production signal writes
- No training / model promotion
- No author attribution claim

It checks post-2026-05-01 fresh activity for 0x501e and emits Sprint056 artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import msgpack
import redis


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "mvp" / "audits" / "sprint056"
DIAG_DIR = ROOT / "mvp" / "diagnostics"

TARGET = "0x501ef174b66883ba40f92e6fb24bfb49331d4d4c"
START_ISO = "2026-05-01T00:00:00+00:00"
START_DT = datetime.fromisoformat(START_ISO)
START_MS = int(START_DT.timestamp() * 1000)
WETH = "0x4200000000000000000000000000000000000006"


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def to_float(v: Any, default: float = 0.0) -> float:
    if v in ("", None):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_lower(v: Any) -> str:
    if v is None:
        return ""
    return str(v).lower()


def dt_to_iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


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


def ch_row(sql: str) -> dict[str, str]:
    out = ch_query(sql + " FORMAT TSVWithNames")
    if out.startswith("CH_QUERY_FAILED"):
        return {"error": out}
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    cols = lines[0].split("\t")
    vals = lines[1].split("\t")
    return {cols[i]: vals[i] if i < len(vals) else "" for i in range(len(cols))}


def ch_rows(sql: str) -> list[dict[str, str]]:
    out = ch_query(sql + " FORMAT TSVWithNames")
    if out.startswith("CH_QUERY_FAILED"):
        return []
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    cols = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        vals = line.split("\t")
        rows.append({cols[i]: vals[i] if i < len(vals) else "" for i in range(len(cols))})
    return rows


def fetch_ch_fresh_events(limit_rows: int = 2000) -> list[dict[str, Any]]:
    sql = f"""
    SELECT
      toString(block_time) AS block_time,
      toString(block_number) AS block_number,
      lower(tx_hash) AS tx_hash,
      lower(pool_address) AS pool_address,
      lower(bought_token_address) AS bought_token_address,
      toString(fee_bps) AS fee_bps,
      toString(sold_amount_normalized) AS sold_amount_normalized
    FROM uniswap_v3_swaps_trade_semantic
    WHERE chain_id = 8453
      AND block_time >= toDateTime64('2026-05-01 00:00:00', 3, 'UTC')
      AND lower(sender) = '{TARGET}'
      AND lower(sold_token_address) = '{WETH}'
      AND lower(bought_token_address) != '{WETH}'
      AND has_clear_direction = 1
    ORDER BY block_time ASC
    LIMIT {int(limit_rows)}
    """
    rows = ch_rows(sql)
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "observed_at": str(r.get("block_time", "")).replace(" ", "T") + "+00:00"
                if r.get("block_time")
                else "",
                "tx_hash": safe_lower(r.get("tx_hash", "")),
                "block_number": r.get("block_number", ""),
                "pool": safe_lower(r.get("pool_address", "")),
                "token": safe_lower(r.get("bought_token_address", "")),
                "fee_tier": r.get("fee_bps", ""),
                "weth_leg": "yes",
                "amount_in_weth": r.get("sold_amount_normalized", ""),
                "route_or_dominant_to": "",
                "detection_latency_ms": "",
                "quoteable_at_observe": "yes",
                "quoteable_after_1_block": "yes",
                "quoteable_after_5s": "yes",
                "quoteable_after_15s": "yes",
                "quoteable_after_30s": "yes",
                "simulated_entry_price": "",
                "pnl_5m": "",
                "pnl_30m": "",
                "pnl_gt1h_or_exit_bucket": "",
                "fail_reason": "",
                "event_day": str(r.get("block_time", ""))[:10],
                "source": "ch_uniswap_v3_trade_semantic",
            }
        )
    return out


def parse_stream_payload(fields: dict[Any, Any]) -> dict[str, Any] | None:
    keys = (b"data", b"payload_msgpack", b"payload", b"tick", "data", "payload_msgpack", "payload", "tick")
    for key in keys:
        raw = fields.get(key)
        if not raw:
            continue
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        try:
            payload = msgpack.unpackb(raw, raw=False)
            return payload if isinstance(payload, dict) else None
        except Exception:
            continue
    return None


def get_stream_tx_from(fields: dict[Any, Any], payload: dict[str, Any] | None) -> str:
    for key in (b"tx_from", "tx_from"):
        raw = fields.get(key)
        if raw:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                return raw.decode("utf-8", errors="replace").lower()
            return str(raw).lower()
    if payload:
        for key in ("tx_from", "wallet_address", "sender", "from"):
            val = payload.get(key)
            if val:
                return str(val).lower()
    return ""


def get_payload_field(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not payload:
        return None
    for key in keys:
        if key in payload and payload.get(key) not in ("", None):
            return payload.get(key)
    return None


@dataclass
class FreshEvent:
    observed_at: str
    tx_hash: str
    block_number: str
    pool: str
    token: str
    fee_tier: str
    weth_leg: str
    amount_in_weth: str
    route_or_dominant_to: str
    detection_latency_ms: str
    quoteable_at_observe: str
    quoteable_after_1_block: str
    quoteable_after_5s: str
    quoteable_after_15s: str
    quoteable_after_30s: str
    simulated_entry_price: str
    pnl_5m: str
    pnl_30m: str
    pnl_gt1h_or_exit_bucket: str
    fail_reason: str
    event_day: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def fetch_stream_fresh_events(max_scan: int, redis_url: str) -> list[dict[str, Any]]:
    client = redis.from_url(redis_url, decode_responses=False)
    stream = "market_ticks"
    last_id = f"{START_MS}-0"
    scanned = 0
    rows: list[dict[str, Any]] = []

    while scanned < max_scan:
        batch = client.xrange(stream, min=last_id, max="+", count=500)
        if not batch:
            break
        for entry_id, fields in batch:
            scanned += 1
            if scanned > max_scan:
                break
            sid = entry_id.decode() if isinstance(entry_id, (bytes, bytearray)) else str(entry_id)
            sid_ms = int(sid.split("-")[0])
            if sid_ms < START_MS:
                continue
            payload = parse_stream_payload(fields)
            tx_from = get_stream_tx_from(fields, payload)
            if tx_from != TARGET:
                continue
            tx_hash = str(get_payload_field(payload, "tx_hash") or "").lower()
            block_number = str(get_payload_field(payload, "block_number") or "")
            pool = safe_lower(get_payload_field(payload, "pool_address"))
            token = safe_lower(get_payload_field(payload, "token_address", "bought_token", "bought_token_address"))
            side = safe_lower(get_payload_field(payload, "side", "swap_direction"))
            sold_token = safe_lower(get_payload_field(payload, "sold_token", "sold_token_address"))
            bought_token = safe_lower(get_payload_field(payload, "bought_token", "bought_token_address"))
            sold_amt = to_float(get_payload_field(payload, "sold_amount", "sold_amount_normalized"), 0.0)
            bought_amt = to_float(get_payload_field(payload, "bought_amount", "bought_amount_normalized"), 0.0)
            fee_tier = str(get_payload_field(payload, "fee_tier", "fee_bps") or "")
            block_time = get_payload_field(payload, "block_time", "timestamp")

            is_buy = side == "buy" or (sold_token == WETH and bought_token and bought_token != WETH)
            if not is_buy:
                continue
            amount_in_weth = sold_amt if sold_token == WETH else 0.0
            weth_leg = "yes" if (sold_token == WETH or bought_token == WETH) else "no"
            if not token and bought_token and bought_token != WETH:
                token = bought_token

            latency_ms = ""
            if block_time:
                try:
                    bt = datetime.fromisoformat(str(block_time).replace("Z", "+00:00"))
                    latency_ms = str(max(0, sid_ms - int(bt.timestamp() * 1000)))
                except Exception:
                    latency_ms = ""

            rows.append(
                {
                    "observed_at": dt_to_iso_utc(sid_ms),
                    "tx_hash": tx_hash,
                    "block_number": block_number,
                    "pool": pool,
                    "token": token,
                    "fee_tier": fee_tier,
                    "weth_leg": weth_leg,
                    "amount_in_weth": f"{amount_in_weth:.8f}" if amount_in_weth else "",
                    "route_or_dominant_to": "",
                    "detection_latency_ms": latency_ms,
                    "quoteable_at_observe": "yes" if pool and token and weth_leg == "yes" else "no",
                    "quoteable_after_1_block": "yes" if pool and token and weth_leg == "yes" else "no",
                    "quoteable_after_5s": "yes" if pool and token and weth_leg == "yes" else "no",
                    "quoteable_after_15s": "yes" if pool and token and weth_leg == "yes" else "no",
                    "quoteable_after_30s": "yes" if pool and token and weth_leg == "yes" else "no",
                    "simulated_entry_price": "",
                    "pnl_5m": "",
                    "pnl_30m": "",
                    "pnl_gt1h_or_exit_bucket": "",
                    "fail_reason": "",
                    "event_day": dt_to_iso_utc(sid_ms)[:10],
                    "source": "market_ticks_stream",
                }
            )
            last_id = sid
        if len(batch) < 500:
            break
        last_id = (batch[-1][0].decode() if isinstance(batch[-1][0], (bytes, bytearray)) else str(batch[-1][0]))
    return rows


def enrich_with_route(rows: list[dict[str, Any]], to_diag: dict[str, str]) -> None:
    dominant_to = to_diag.get("dominant_to", "")
    for row in rows:
        row["route_or_dominant_to"] = dominant_to


def build_return_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        reason = ev.get("fail_reason", "")
        if not ev.get("quoteable_at_observe") == "yes":
            reason = reason or "NOT_QUOTEABLE_AT_OBSERVE"
        if not ev.get("pnl_5m") and not ev.get("pnl_30m"):
            reason = reason or "NO_EXIT_OR_MARKOUT_DATA_IN_BOUNDED_SCOPE"
        out.append(
            {
                "observed_at": ev.get("observed_at", ""),
                "tx_hash": ev.get("tx_hash", ""),
                "token": ev.get("token", ""),
                "pool": ev.get("pool", ""),
                "amount_in_weth": ev.get("amount_in_weth", ""),
                "quoteable_at_observe": ev.get("quoteable_at_observe", ""),
                "simulated_entry_price": ev.get("simulated_entry_price", ""),
                "pnl_5m": ev.get("pnl_5m", ""),
                "pnl_30m": ev.get("pnl_30m", ""),
                "pnl_gt1h_or_exit_bucket": ev.get("pnl_gt1h_or_exit_bucket", ""),
                "fail_reason": reason,
            }
        )
        ev["fail_reason"] = reason
    return out


def summarize_fail_reasons(events: list[dict[str, Any]], global_reasons: Iterable[str]) -> list[dict[str, Any]]:
    c = Counter()
    for ev in events:
        reason = ev.get("fail_reason", "")
        if reason:
            c[reason] += 1
    for reason in global_reasons:
        c[reason] += 1
    return [{"fail_reason": k, "count": v} for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    n = len(vals)
    if n % 2 == 1:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2


def evaluate(events: list[dict[str, Any]], ch_v3_max: str) -> dict[str, Any]:
    n = len(events)
    quoteable = sum(1 for e in events if e.get("quoteable_at_observe") == "yes")
    quoteable_share = (quoteable / n) if n else 0.0

    pnl_5m = [to_float(e.get("pnl_5m"), float("nan")) for e in events if e.get("pnl_5m", "") not in ("", None)]
    pnl_30m = [to_float(e.get("pnl_30m"), float("nan")) for e in events if e.get("pnl_30m", "") not in ("", None)]
    pnl_5m = [v for v in pnl_5m if v == v]
    pnl_30m = [v for v in pnl_30m if v == v]

    token_count: Counter[str] = Counter()
    day_count: Counter[str] = Counter()
    pool_count: Counter[str] = Counter()
    for e in events:
        if e.get("token"):
            token_count[e["token"]] += 1
        if e.get("event_day"):
            day_count[e["event_day"]] += 1
        if e.get("pool"):
            pool_count[e["pool"]] += 1

    top1_token_share = (max(token_count.values()) / n) if n and token_count else 0.0
    top1_day_share = (max(day_count.values()) / n) if n and day_count else 0.0
    top1_pool_share = (max(pool_count.values()) / n) if n and pool_count else 0.0

    median_5m = median(pnl_5m) if pnl_5m else 0.0
    median_30m = median(pnl_30m) if pnl_30m else 0.0

    verdict = "INCONCLUSIVE"
    if n >= 20 and quoteable_share >= 0.7 and (median_5m > 0 or median_30m > 0) and top1_token_share <= 0.5 and top1_day_share <= 0.8 and top1_pool_share <= 0.8:
        verdict = "PASS"
    elif 5 <= n <= 19 and (median_5m > 0 or median_30m > 0):
        verdict = "WEAK_PASS"
    elif n < 5:
        verdict = "INCONCLUSIVE"
    elif n >= 5 and (quoteable_share < 0.5 or (median_5m <= 0 and median_30m <= 0)):
        verdict = "FAIL"

    if ch_v3_max and ch_v3_max < "2026-05-01":
        verdict = "INCONCLUSIVE"

    return {
        "fresh_sample_count": n,
        "quoteable_count": quoteable,
        "quoteable_share": round(quoteable_share, 4),
        "median_pnl_5m": round(median_5m, 6),
        "median_pnl_30m": round(median_30m, 6),
        "top1_token_share": round(top1_token_share, 4),
        "top1_day_share": round(top1_day_share, 4),
        "top1_pool_share": round(top1_pool_share, 4),
        "verdict": verdict,
    }


def write_readout(metrics: dict[str, Any], flags: dict[str, Any]) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    md = f"""# Sprint056 — Fresh Activity Paper Watcher (`0x501e`)

Generated: `{datetime.now(timezone.utc).isoformat()}`

## Executive

Verdict: **{metrics['verdict']}**.

- Fresh sample count: `{metrics['fresh_sample_count']}`
- Quoteable share: `{metrics['quoteable_share']}`
- Median paper return (5m): `{metrics['median_pnl_5m']}`
- Median paper return (30m): `{metrics['median_pnl_30m']}`
- Top1 token share: `{metrics['top1_token_share']}`
- Top1 day share: `{metrics['top1_day_share']}`
- Top1 pool share: `{metrics['top1_pool_share']}`

## Coverage / Boundaries

- CH `uniswap_v3_swaps_trade_semantic` max block time: `{flags['ch_v3_max_block_time']}`
- CH `swap_trade_events_all` max block time: `{flags['ch_all_max_block_time']}`
- Stream scan enabled: `{flags['stream_scan_enabled']}` (bounded)
- No live / no training / no model promotion / no production signals.

## Decision

- If fresh samples stay `<5` or May coverage missing, keep **INCONCLUSIVE**.
- Continue paper-only watcher; do not expand historical wallet search in this sprint.
"""
    (AUDIT_DIR / "executive_readout.md").write_text(md, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis-url", default="redis://localhost:6379/0")
    ap.add_argument("--max-stream-scan", type=int, default=30000)
    ap.add_argument("--disable-stream-scan", action="store_true")
    ap.add_argument("--ch-fresh-limit", type=int, default=2000)
    args = ap.parse_args()

    scorecard_rows = read_csv(ROOT / "mvp/audits/sprint054/candidate_scorecard.csv")
    to_diag_rows = read_csv(DIAG_DIR / "sprint045d_to_field_diagnostics.csv")
    to_diag_map = {row.get("controller", "").lower(): row for row in to_diag_rows}
    to_diag = to_diag_map.get(TARGET, {})

    ch_v3 = ch_row("SELECT max(block_time) AS max_block_time FROM uniswap_v3_swaps_trade_semantic WHERE chain_id=8453")
    ch_all = ch_row("SELECT max(block_time) AS max_block_time FROM swap_trade_events_all WHERE chain_id=8453")
    ch_v3_max = ch_v3.get("max_block_time", "")
    ch_all_max = ch_all.get("max_block_time", "")

    fresh_events: list[dict[str, Any]] = []
    global_reasons: list[str] = []

    ch_fresh_rows = 0
    if ch_v3_max and ch_v3_max >= "2026-05-01":
        try:
            ch_events = fetch_ch_fresh_events(args.ch_fresh_limit)
            ch_fresh_rows = len(ch_events)
            fresh_events.extend(ch_events)
        except Exception as exc:
            global_reasons.append(f"CH_FRESH_SCAN_FAILED:{type(exc).__name__}")
    else:
        global_reasons.append("CH_COVERAGE_MAX_BEFORE_2026_05_01")
    if (not fresh_events) and (not args.disable_stream_scan):
        try:
            fresh_events.extend(fetch_stream_fresh_events(args.max_stream_scan, args.redis_url))
        except Exception as exc:
            global_reasons.append(f"STREAM_SCAN_FAILED:{type(exc).__name__}")
    else:
        global_reasons.append("STREAM_SCAN_DISABLED")

    # Deduplicate by tx_hash + observed_at
    uniq = {}
    for ev in fresh_events:
        key = (ev.get("tx_hash", ""), ev.get("observed_at", ""))
        uniq[key] = ev
    fresh_events = list(uniq.values())

    enrich_with_route(fresh_events, to_diag)
    return_rows = build_return_rows(fresh_events)

    if not fresh_events:
        global_reasons.append("NO_FRESH_BUY_EVENTS_FOUND_POST_2026_05_01")

    fail_rows = summarize_fail_reasons(fresh_events, global_reasons)
    metrics = evaluate(fresh_events, ch_v3_max)

    if metrics["fresh_sample_count"] < 5:
        metrics["verdict"] = "INCONCLUSIVE"

    decision_flags = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_wallet": TARGET,
        "window_start_utc": START_ISO,
        "fresh_sample_count": metrics["fresh_sample_count"],
        "quoteable_share": metrics["quoteable_share"],
        "median_pnl_5m": metrics["median_pnl_5m"],
        "median_pnl_30m": metrics["median_pnl_30m"],
        "top1_token_share": metrics["top1_token_share"],
        "top1_day_share": metrics["top1_day_share"],
        "top1_pool_share": metrics["top1_pool_share"],
        "sprint056_verdict": metrics["verdict"],
        "ch_v3_max_block_time": ch_v3_max,
        "ch_all_max_block_time": ch_all_max,
        "ch_fresh_rows": ch_fresh_rows,
        "stream_scan_enabled": (not args.disable_stream_scan),
        "shared_tx_to_operator_identity_allowed": False,
        "author_wallet_found": False,
        "live_allowed": False,
        "training_allowed": False,
        "model_promotion_allowed": False,
        "production_signals_written": False,
    }

    fields_fresh = [
        "observed_at",
        "tx_hash",
        "block_number",
        "pool",
        "token",
        "fee_tier",
        "weth_leg",
        "amount_in_weth",
        "route_or_dominant_to",
        "detection_latency_ms",
        "quoteable_at_observe",
        "quoteable_after_1_block",
        "quoteable_after_5s",
        "quoteable_after_15s",
        "quoteable_after_30s",
        "simulated_entry_price",
        "pnl_5m",
        "pnl_30m",
        "pnl_gt1h_or_exit_bucket",
        "fail_reason",
    ]
    fields_returns = [
        "observed_at",
        "tx_hash",
        "token",
        "pool",
        "amount_in_weth",
        "quoteable_at_observe",
        "simulated_entry_price",
        "pnl_5m",
        "pnl_30m",
        "pnl_gt1h_or_exit_bucket",
        "fail_reason",
    ]
    fields_fail = ["fail_reason", "count"]

    write_csv(DIAG_DIR / "sprint056_0x501e_fresh_events.csv", fresh_events, fields_fresh)
    write_csv(DIAG_DIR / "sprint056_0x501e_paper_returns.csv", return_rows, fields_returns)
    write_csv(DIAG_DIR / "sprint056_0x501e_fail_reasons.csv", fail_rows, fields_fail)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "decision_flags.json").write_text(json.dumps(decision_flags, indent=2), encoding="utf-8")
    write_readout(metrics, decision_flags)

    print(
        json.dumps(
            {
                "verdict": metrics["verdict"],
                "fresh_sample_count": metrics["fresh_sample_count"],
                "quoteable_share": metrics["quoteable_share"],
                "ch_v3_max": ch_v3_max,
                "stream_scan": (not args.disable_stream_scan),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
