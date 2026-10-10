#!/usr/bin/env python3
"""Sprint037A — Historical regime mining for Base Uniswap V3 (WETH meme-oriented universe).

Charter
───────
- **Regime discovery only**: identify past windows with dense, multi-pool swap activity
  suitable for *replay study* (Sprint037B). No training, no live routing, no promotion.
- **No outcome cherry-picking**: ranks windows by *contemporaneous* on-chain activity
  metrics only (swap counts, active pool counts, tx cardinality). No realized returns,
  no “top winners” post-filter.
- **Lookahead / leakage guard**: aggregates use `block_time` (fallback: `received_at_ms`
  as `DateTime64` ingest clock). Pool set is *current* `discovered_pools` metadata intersected with
  historical swaps → survivorship / backfilled-metadata bias possible; surfaced in outputs.
- **Bounded I/O**: read-only ClickHouse SELECT; optional read of local `logs/dashcam.csv`
  for coarse “current vs historical activity” context (never committed). No full-chain
  archive scan loops.

Inputs (artifact references, not loaded as secrets)
──────────────────────────────────────────────────
- Sprint035C–F / 036A diagnostics under `mvp/diagnostics/` and `mvp/audits/`
- `docs/label-semantics-freeze.md`, whitepaper v2 paths, leakage notes in `mvp/audits/sprint029c_retrain_guard_design.md`

Outputs
───────
- `mvp/diagnostics/sprint037a_regime_mining_summary.json`
- `mvp/audits/sprint037a_regime_windows.csv`
- `mvp/audits/sprint037a_historical_regime_mining_report.md`

Usage
─────
  CLICKHOUSE_URL=http://... python3 scripts/sprint037a_historical_regime_mining.py --run
  python3 scripts/sprint037a_historical_regime_mining.py --dry-run
  python3 scripts/sprint037a_historical_regime_mining.py --offline-fixture
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

CHAIN_ID_BASE = 8453

# Stable token addresses (Base) — exclude stable–stable; aligned with Sprint035D / 036A spirit.
_STABLES = tuple(
    sorted(
        {
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
            "0x4a3a6dd60a34bb2aba60d73b4c88d1e535f79e18",  # MIM
            "0x368181499736d0c0cc614dbb145e2ec1ac86b8c6",  # CUSD
            "0x04d5ddf5f3a8939889f11e97f8c4bb48317f1938",  # USDz
            "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
        }
    )
)

# Large-cap / reference pools to exclude from “meme-like” regime mining (same spirit as Sprint035D).
_EXCLUDED_LARGE_CAP = (
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0x6c561b446416e1a00e8e93e221854d6ea4171372",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
    "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",
    "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
)

OUT_JSON_DEFAULT = REPO / "mvp/diagnostics/sprint037a_regime_mining_summary.json"
OUT_CSV_DEFAULT = REPO / "mvp/audits/sprint037a_regime_windows.csv"
OUT_MD_DEFAULT = REPO / "mvp/audits/sprint037a_historical_regime_mining_report.md"
DASHCAM_DEFAULT = REPO / "logs/dashcam.csv"

DECISION_FLAGS: dict[str, Any] = {
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "TRAINING_ALLOWED": False,
    "H12_ABLATION_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "MODEL_PROMOTION_ALLOWED": False,
    "SPRINT037B_REPLAY_ALLOWED": False,
    "REGIME_DISCOVERY_ONLY": True,
    "NO_ALPHA_CLAIM": True,
    "CLICKHOUSE_READ_ONLY": True,
    "RPC_REQUIRED": False,
}

ARTIFACT_REFS = [
    "mvp/diagnostics/sprint035c_external_candidate_pool_summary.csv",
    "mvp/diagnostics/sprint035d_momentum_filtered_candidate_summary.csv",
    "mvp/diagnostics/sprint035e_ohlcv_anchored_candidate_summary.csv",
    "mvp/diagnostics/sprint035f_ohlcv_multihorizon_diagnostic.csv",
    "mvp/diagnostics/sprint036a_paper_watchlist.json",
    "mvp/audits/sprint035d_momentum_filtered_pool_discovery_report.md",
    "mvp/audits/sprint036a_toxic_flow_paper_watchlist_report.md",
    "docs/label-semantics-freeze.md",
    "whitepaper-v2.md",
    "mvp/audits/sprint029c_retrain_guard_design.md",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ch_guard_readonly(sql: str) -> None:
    s_lr = sql.strip().lower()
    if not s_lr.startswith("select") and not s_lr.startswith("with"):
        raise ValueError("only read-only CH queries allowed")
    banned = (" insert ", " alter ", " drop detach ", " create ", " truncate ")
    padded = f" {s_lr} "
    for b in banned:
        if b in padded:
            raise ValueError(f"forbidden fragment in query: {b!r}")


def _ch_http_json_each_row(
    ch_url: str,
    sql: str,
    ch_user: str,
    ch_password: str,
    timeout_sec: float = 180.0,
) -> list[dict[str, Any]]:
    _ch_guard_readonly(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q = f"{q} FORMAT JSONEachRow"
    # POST body avoids HTTP 414 / proxy limits on long aggregate queries.
    url = f"{ch_url.rstrip('/')}/"
    req = Request(url, data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if ch_user:
        import base64

        token = base64.b64encode(f"{ch_user}:{ch_password}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    with urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _meme_pools_sql_pred(alias: str = "dp") -> str:
    st = ", ".join(f"'{a.lower()}'" for a in _STABLES)
    ex = ", ".join(f"'{a.lower()}'" for a in _EXCLUDED_LARGE_CAP)
    a = alias
    return f"""
{a}.chain_id = {CHAIN_ID_BASE}
  AND lower({a}.pool_type) = 'v3'
  AND {a}.has_weth_leg = 1
  AND {a}.fee_bps IN (100, 500, 3000, 10000)
  AND NOT (
    lower({a}.token0_address) IN ({st})
    AND lower({a}.token1_address) IN ({st})
  )
  AND lower({a}.pool_address) NOT IN ({ex})
""".strip()


def _meme_pool_filter_sql() -> str:
    return f"""
meme_pools AS (
  SELECT lower(pool_address) AS pa
  FROM eatlq.discovered_pools AS dp
  WHERE {_meme_pools_sql_pred('dp')}
)
""".strip()


def _ts_expr(table_alias: str = "s") -> str:
    # block_time (chain) preferred; received_at_ms is DateTime64 ingest clock in eatlq.uniswap_v3_swaps_*.
    return f"coalesce({table_alias}.block_time, {table_alias}.received_at_ms)"


def query_universe_count(ch_url: str, user: str, password: str) -> int:
    sql = f"""
SELECT toUInt64(count()) AS cnt
FROM eatlq.discovered_pools AS dp
WHERE {_meme_pools_sql_pred('dp')}
""".strip()
    rows = _ch_http_json_each_row(ch_url, sql, user, password, timeout_sec=60.0)
    if not rows:
        return 0
    return int(rows[0].get("cnt") or 0)


def query_coverage(ch_url: str, user: str, password: str, lookback_months: int) -> dict[str, Any]:
    te = _ts_expr("s")
    sql = f"""
WITH {_meme_pool_filter_sql()}
SELECT
  min({te}) AS min_ts,
  max({te}) AS max_ts,
  toUInt64(count()) AS swap_rows,
  toUInt64(uniqExact(lower(s.pool_address))) AS pools_with_swaps
FROM eatlq.uniswap_v3_swaps_enriched AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID_BASE}
  AND s.removed = 0
  AND {te} >= now() - INTERVAL {int(lookback_months)} MONTH
""".strip()
    rows = _ch_http_json_each_row(ch_url, sql, user, password, timeout_sec=240.0)
    return rows[0] if rows else {}


def query_weekly_rollup(
    ch_url: str, user: str, password: str, lookback_months: int, min_dense_swaps: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    te = _ts_expr("s")
    sql_main = f"""
WITH {_meme_pool_filter_sql()}
SELECT
  toStartOfWeek({te}, 1) AS wk,
  toUInt64(count()) AS total_swaps,
  toUInt64(uniqExact(lower(s.pool_address))) AS active_pools,
  toUInt64(uniqExact(s.tx_hash)) AS uniq_tx,
  toFloat64(avg(length(coalesce(s.liquidity_raw, '')))) AS liquidity_raw_hex_len_avg
FROM eatlq.uniswap_v3_swaps_enriched AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID_BASE}
  AND s.removed = 0
  AND {te} >= now() - INTERVAL {int(lookback_months)} MONTH
GROUP BY wk
ORDER BY wk ASC
""".strip()

    sql_dense = f"""
WITH {_meme_pool_filter_sql()},
per AS (
  SELECT
    toStartOfWeek({te}, 1) AS wk,
    lower(s.pool_address) AS pa,
    toUInt64(count()) AS c
  FROM eatlq.uniswap_v3_swaps_enriched AS s
  INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
  WHERE s.chain_id = {CHAIN_ID_BASE}
    AND s.removed = 0
    AND {te} >= now() - INTERVAL {int(lookback_months)} MONTH
  GROUP BY wk, pa
  HAVING c >= {int(min_dense_swaps)}
)
SELECT wk, toUInt64(uniqExact(pa)) AS dense_pools
FROM per
GROUP BY wk
ORDER BY wk ASC
""".strip()

    main = _ch_http_json_each_row(ch_url, sql_main, user, password, timeout_sec=600.0)
    dense = _ch_http_json_each_row(ch_url, sql_dense, user, password, timeout_sec=600.0)
    return main, dense


def _coverage_span_days(cov: dict[str, Any]) -> Optional[float]:
    mn = _parse_ch_datetime(cov.get("min_ts"))
    mx = _parse_ch_datetime(cov.get("max_ts"))
    if mn is None or mx is None or mx <= mn:
        return None
    return (mx - mn) / 86400.0


def _parse_ch_datetime(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _merge_weekly(
    main: list[dict[str, Any]], dense_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    dense_map: dict[str, int] = {}
    for r in dense_rows:
        wk = r.get("wk")
        if wk is None:
            continue
        dense_map[str(wk)] = int(r.get("dense_pools") or 0)

    out: list[dict[str, Any]] = []
    for r in main:
        wk = r.get("wk")
        if wk is None:
            continue
        wk_s = str(wk)
        ts = _parse_ch_datetime(wk_s.split(".")[0])
        total_swaps = int(r.get("total_swaps") or 0)
        active = int(r.get("active_pools") or 0)
        uniq_tx = int(r.get("uniq_tx") or 0)
        dense_n = dense_map.get(wk_s, 0)
        sec_week = 7 * 24 * 3600
        swap_per_sec = total_swaps / sec_week if sec_week else 0.0
        tx_per_sec = uniq_tx / sec_week if sec_week else 0.0
        # Priority: emphasize many concurrent “busy” pools, not single-pool spikes.
        burst_score = (math.log1p(dense_n) * math.log1p(total_swaps)) if total_swaps else 0.0
        out.append(
            {
                "week_start": wk_s[:10] if len(wk_s) >= 10 else wk_s,
                "week_start_unix_approx": ts,
                "total_swaps": total_swaps,
                "active_pools": active,
                "dense_pools_ge_min": dense_n,
                "uniq_tx": uniq_tx,
                "swap_rate_per_sec": round(swap_per_sec, 8),
                "uniq_tx_rate_per_sec": round(tx_per_sec, 8),
                "liquidity_raw_hex_len_avg": float(r.get("liquidity_raw_hex_len_avg") or 0.0),
                "regime_burst_score": round(burst_score, 6),
            }
        )
    return out


def _rank_regimes(weekly: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    scored = sorted(weekly, key=lambda x: x.get("regime_burst_score") or 0.0, reverse=True)
    out: list[dict[str, Any]] = []
    for i, row in enumerate(scored[:top_n], start=1):
        w = dict(row)
        w["rank"] = i
        w["sprint037b_replay_candidate"] = (
            w.get("dense_pools_ge_min", 0) >= 5 and w.get("total_swaps", 0) >= 5000
        )
        w["disclaimer"] = (
            "Activity-only ranking; not PnL, not alpha. "
            "Universe = current discovered_pools ∩ history (survivorship bias possible)."
        )
        out.append(w)
    return out


def _recent_window_compare(weekly: list[dict[str, Any]], recent_weeks: int = 4) -> dict[str, Any]:
    if len(weekly) < recent_weeks + 1:
        return {"status": "insufficient_weeks", "recent_weeks": recent_weeks}
    tail = weekly[-recent_weeks:]
    prior = weekly[: -recent_weeks]
    def avg(key: str) -> float:
        return sum(float(x.get(key) or 0) for x in prior) / len(prior) if prior else 0.0

    a_sw = sum(float(x.get("total_swaps") or 0) for x in tail) / len(tail)
    a_dense = sum(float(x.get("dense_pools_ge_min") or 0) for x in tail) / len(tail)
    m_sw = avg("total_swaps")
    m_dense = avg("dense_pools_ge_min")
    return {
        "trailing_weeks": recent_weeks,
        "mean_total_swaps_recent": round(a_sw, 2),
        "mean_total_swaps_prior": round(m_sw, 2),
        "ratio_total_swaps_recent_vs_prior": round(a_sw / m_sw, 4) if m_sw > 0 else None,
        "mean_dense_pools_recent": round(a_dense, 2),
        "mean_dense_pools_prior": round(m_dense, 2),
        "ratio_dense_recent_vs_prior": round(a_dense / m_dense, 4) if m_dense > 0 else None,
        "interpretation": (
            "Recent trailing window vs earlier portion of the selected lookback "
            "(same meme-oriented CH definition; not an alpha claim)."
        ),
    }


def _optional_dashcam_context(path: Path, lookback_months: int) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "absent", "path": str(path)}
    try:
        weeks: dict[str, int] = defaultdict(int)
        total = 0
        cutoff_ms = None
        # Approximate cutoff from months
        import time as _time

        cutoff_ms = int((_time.time() - lookback_months * 30 * 86400) * 1000)
        with path.open(newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                ts_raw = row.get("timestamp") or row.get("bucket_ms") or ""
                try:
                    ts_ms = int(str(ts_raw).strip())
                except ValueError:
                    continue
                if ts_ms < cutoff_ms:
                    continue
                # ISO week bucket from ms (UTC)
                dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                iso = dt.strftime("%G-W%V")
                weeks[iso] += 1
                total += 1
        return {
            "status": "read",
            "path": str(path),
            "note": (
                "Token-trigger dashcam rows (not pool-aligned). "
                "Use only as coarse ‘desk activity’ vs historical CH regime densities."
            ),
            "rows_in_lookback_approx": total,
            "rows_per_iso_week_top": sorted(weeks.items(), key=lambda x: -x[1])[:8],
        }
    except OSError as e:
        return {"status": "error", "error": str(e)}


def _offline_fixture() -> dict[str, Any]:
    weekly = [
        {
            "week_start": "2025-08-04",
            "week_start_unix_approx": 1754265600.0,
            "total_swaps": 120_000,
            "active_pools": 400,
            "dense_pools_ge_min": 80,
            "uniq_tx": 45_000,
            "swap_rate_per_sec": 0.1984,
            "uniq_tx_rate_per_sec": 0.0744,
            "liquidity_raw_hex_len_avg": 78.0,
            "regime_burst_score": 12.5,
        },
        {
            "week_start": "2025-11-10",
            "total_swaps": 200_000,
            "active_pools": 620,
            "dense_pools_ge_min": 140,
            "uniq_tx": 72_000,
            "swap_rate_per_sec": 0.3307,
            "uniq_tx_rate_per_sec": 0.1190,
            "liquidity_raw_hex_len_avg": 78.0,
            "regime_burst_score": 18.2,
        },
        {
            "week_start": "2026-01-12",
            "total_swaps": 45_000,
            "active_pools": 180,
            "dense_pools_ge_min": 22,
            "uniq_tx": 18_000,
            "swap_rate_per_sec": 0.0744,
            "uniq_tx_rate_per_sec": 0.0298,
            "liquidity_raw_hex_len_avg": 76.0,
            "regime_burst_score": 5.1,
        },
    ]
    ranked = _rank_regimes(weekly, top_n=5)
    flags = dict(DECISION_FLAGS)
    # Synthetic rows illustrate ranking only; do not unlock Sprint037B without CH.
    flags["SPRINT037B_REPLAY_ALLOWED"] = False
    flags["SPRINT037B_REPLAY_BLOCKER"] = "offline_fixture_not_clickhouse_backed"
    return {
        "kind": "sprint037a_regime_mining",
        "version": "1.0",
        "created_at": _utc_now_iso(),
        "offline_fixture": True,
        "lookback_months": 12,
        "min_dense_swaps_per_pool_week": 200,
        "candidate_universe_pools_meme_weth_v3": 3500,
        "coverage": {
            "min_ts": "2025-05-01 00:00:00",
            "max_ts": "2026-05-01 00:00:00",
            "swap_rows": 450_000,
            "pools_with_swaps": 900,
        },
        "weekly_regimes": weekly,
        "top_historical_regimes": ranked,
        "current_vs_historical_within_lookback": _recent_window_compare(weekly),
        "dashcam_context": {"status": "absent", "path": str(DASHCAM_DEFAULT)},
        "data_gaps": [
            "Fixture data only — run with CLICKHOUSE_URL for production regimes.",
            "discovered_at in discovered_pools is ingestion time — not used for regime clocks.",
        ],
        "cost_estimate": {
            "clickhouse": "Two bounded aggregate queries over lookback window + one universe count.",
            "rpc": "None in this module.",
        },
        "decision_flags": flags,
        "historical_depth": {
            "swap_event_span_days_observed": 366.0,
            "min_span_days_required_for_sprint037b": 180.0,
            "lookback_months_requested": 12,
        },
        "regime_verdict": "OFFLINE_FIXTURE_ONLY",
        "artifact_refs_present": [p for p in ARTIFACT_REFS if (REPO / p).is_file()],
    }


def _write_csv(path: Path, weekly: list[dict[str, Any]], top: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top_starts = {t.get("week_start") for t in top}
    fields = [
        "week_start",
        "total_swaps",
        "active_pools",
        "dense_pools_ge_min",
        "uniq_tx",
        "swap_rate_per_sec",
        "uniq_tx_rate_per_sec",
        "liquidity_raw_hex_len_avg",
        "regime_burst_score",
        "in_top_regimes",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in weekly:
            r = {k: row.get(k) for k in fields if k != "in_top_regimes"}
            r["in_top_regimes"] = row.get("week_start") in top_starts
            w.writerow(r)


def _write_md(
    path: Path,
    summary: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top = summary.get("top_historical_regimes") or []
    lines = [
        "# Sprint037A — Historical regime mining (Base Uniswap V3)",
        "",
        f"- Generated: `{summary.get('created_at')}`",
        f"- Lookback: **{summary.get('lookback_months')}** months",
        f"- Min swaps / pool / week (dense threshold): **{summary.get('min_dense_swaps_per_pool_week')}**",
        f"- Meme-oriented universe: V3 + WETH leg + standard fee tiers + exclude stable–stable + Sprint035-style large-cap pool denies",
        "",
        "## Leakage / lookahead posture",
        "",
        "- Metrics use **event-time** (`block_time`, else ingest `received_at_ms` as `DateTime64`).",
        "- **No** outcome labels, **no** winner-conditioned pool selection.",
        "- Pool metadata comes from **current** `discovered_pools`; historical swaps from pools absent today are **missing** (survivorship).",
        "",
        "## Data coverage",
        "",
        f"```json\n{json.dumps(summary.get('coverage'), indent=2, sort_keys=True)}\n```",
        "",
        "## Top historical regimes (activity-ranked)",
        "",
    ]
    for r in top:
        lines.append(
            f"{r.get('rank')}. **{r.get('week_start')}** — "
            f"dense_pools≥min={r.get('dense_pools_ge_min')}, "
            f"active_pools={r.get('active_pools')}, swaps={r.get('total_swaps')}, "
            f"uniq_tx={r.get('uniq_tx')}, "
            f"037B_candidate={r.get('sprint037b_replay_candidate')}"
        )
    lines.extend(
        [
            "",
            "## Current vs historical (within lookback)",
            "",
            f"```json\n{json.dumps(summary.get('current_vs_historical_within_lookback'), indent=2, sort_keys=True)}\n```",
            "",
            "## Sprint037B",
            "",
            f"- **regime_verdict**: `{summary.get('regime_verdict')}`",
            f"- **historical_depth**:",
            f"```json\n{json.dumps(summary.get('historical_depth'), indent=2, sort_keys=True)}\n```",
            f"- **SPRINT037B_REPLAY_ALLOWED** (gate): `{summary.get('decision_flags', {}).get('SPRINT037B_REPLAY_ALLOWED')}`",
            "- Proceed only for weeks flagged `sprint037b_replay_candidate=true` **and** acceptable CH timestamp coverage for that week.",
            "",
            "## Data gaps & cost",
            "",
        ]
    )
    for g in summary.get("data_gaps") or []:
        lines.append(f"- {g}")
    lines.append("")
    lines.append(f"```json\n{json.dumps(summary.get('cost_estimate'), indent=2, sort_keys=True)}\n```")
    lines.append("")
    lines.append("## Decision flags")
    lines.append("")
    lines.append(f"```json\n{json.dumps(summary.get('decision_flags'), indent=2, sort_keys=True)}\n```")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint037A historical regime mining")
    ap.add_argument("--run", action="store_true", help="Run ClickHouse-backed mining")
    ap.add_argument("--dry-run", action="store_true", help="Print JSON plan only")
    ap.add_argument("--offline-fixture", action="store_true", help="Write fixture outputs (CI-safe)")
    ap.add_argument("--lookback-months", type=int, default=int(os.environ.get("SPRINT037A_LOOKBACK_MONTHS", "12")))
    ap.add_argument("--min-dense-swaps", type=int, default=int(os.environ.get("SPRINT037A_MIN_DENSE_SWAPS", "200")))
    ap.add_argument("--min-history-span-days", type=float, default=float(os.environ.get("SPRINT037A_MIN_HISTORY_SPAN_DAYS", "180")))
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--dashcam", type=Path, default=Path(os.environ.get("DASHCAM_PATH", str(DASHCAM_DEFAULT))))
    args = ap.parse_args()

    out_json = Path(os.environ.get("SPRINT037A_OUT_JSON", str(OUT_JSON_DEFAULT)))
    out_csv = Path(os.environ.get("SPRINT037A_OUT_CSV", str(OUT_CSV_DEFAULT)))
    out_md = Path(os.environ.get("SPRINT037A_OUT_MD", str(OUT_MD_DEFAULT)))

    ch_url = (os.environ.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_HTTP_URL") or "").strip()
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD") or ""

    if args.dry_run:
        print(
            json.dumps(
                {
                    "kind": "sprint037a_regime_mining_plan",
                    "lookback_months": args.lookback_months,
                    "min_dense_swaps_per_pool_week": args.min_dense_swaps,
                    "clickhouse_configured": bool(ch_url),
                    "outputs": {
                        "json": str(out_json),
                        "csv": str(out_csv),
                        "md": str(out_md),
                    },
                    "decision_flags": DECISION_FLAGS,
                },
                indent=2,
            )
        )
        return 0

    if args.offline_fixture:
        fix = _offline_fixture()
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(fix, indent=2, sort_keys=True), encoding="utf-8")
        _write_csv(out_csv, fix["weekly_regimes"], fix["top_historical_regimes"])
        _write_md(out_md, fix)
        print(json.dumps({"status": "ok", "wrote": [str(out_json), str(out_csv), str(out_md)]}))
        return 0

    if not args.run:
        print("pass --run, --dry-run, or --offline-fixture", file=sys.stderr)
        return 2

    if not ch_url:
        print("BLOCKED: CLICKHOUSE_URL not set", file=sys.stderr)
        return 5

    try:
        u_cnt = query_universe_count(ch_url, ch_user, ch_password)
        cov = query_coverage(ch_url, ch_user, ch_password, args.lookback_months)
        main, dense = query_weekly_rollup(ch_url, ch_user, ch_password, args.lookback_months, args.min_dense_swaps)
        weekly = _merge_weekly(main, dense)
        top = _rank_regimes(weekly, top_n=args.top_n)
        compare = _recent_window_compare(weekly)
        dash = _optional_dashcam_context(args.dashcam, args.lookback_months)
    except URLError as e:
        print(f"BLOCKED: ClickHouse unreachable: {e}", file=sys.stderr)
        return 6
    except (ValueError, OSError, KeyError) as e:
        print(f"BLOCKED: query/parse error: {e}", file=sys.stderr)
        return 7

    flags = dict(DECISION_FLAGS)
    span_days = _coverage_span_days(cov)
    min_ts_ok = cov.get("min_ts")
    weeks_ok = len(weekly) >= 4
    strong = any(r.get("sprint037b_replay_candidate") for r in top)
    span_ok = span_days is not None and span_days >= float(args.min_history_span_days)

    flags["SPRINT037B_REPLAY_ALLOWED"] = bool(min_ts_ok and weeks_ok and strong and span_ok)
    if not flags["SPRINT037B_REPLAY_ALLOWED"]:
        if not span_ok:
            flags["SPRINT037B_REPLAY_BLOCKER"] = (
                "insufficient_clickhouse_swap_history_span_"
                f"(observed_days={round(float(span_days), 4) if span_days is not None else None}, "
                f"required={args.min_history_span_days})"
            )
        elif not strong:
            flags["SPRINT037B_REPLAY_BLOCKER"] = "no_week_passes_dense_and_swap_floor"
        elif not weeks_ok:
            flags["SPRINT037B_REPLAY_BLOCKER"] = "insufficient_distinct_calendar_weeks"
        else:
            flags["SPRINT037B_REPLAY_BLOCKER"] = "unknown_gate_failure"
    if flags["SPRINT037B_REPLAY_ALLOWED"]:
        flags.pop("SPRINT037B_REPLAY_BLOCKER", None)
    regime_verdict = (
        "REPLAY_CANDIDATES_IDENTIFIED"
        if flags["SPRINT037B_REPLAY_ALLOWED"]
        else "NO_REPLAYABLE_HISTORICAL_REGIME_FOUND"
    )

    data_gaps: list[str] = []
    if span_days is not None and span_days < float(args.lookback_months) * 28.0:
        data_gaps.insert(
            0,
            f"Per-universe swap timestamps span ~{span_days:.1f} days in ClickHouse — "
            f"below ~{int(args.lookback_months * 28)}d implied by {args.lookback_months}m lookback; "
            "cannot answer full 6–12m regime question from CH alone without deeper backfill.",
        )
    if not min_ts_ok:
        data_gaps.append("Missing min_ts in coverage aggregate — inspect CH swap timestamps.")
    elif len(weekly) < 2:
        data_gaps.append("Fewer than 2 ISO weeks with activity — regime comparison underpowered.")
    pools_sw = int(cov.get("pools_with_swaps") or 0)
    if pools_sw < 50:
        data_gaps.append("Fewer than 50 distinct pools with swaps in lookback under meme filter — regime mining low power.")

    data_gaps.append(
        "Survivorship: only pools present in discovered_pools today contribute; "
        "dead/rug pools missing from metadata are excluded from historical counts."
    )
    if cov.get("max_ts") and cov.get("min_ts"):
        data_gaps.append(
            "If max_ts lags `now()` materially, recent regime estimates are truncated (ingest delay)."
        )

    summary: dict[str, Any] = {
        "kind": "sprint037a_regime_mining",
        "version": "1.0",
        "created_at": _utc_now_iso(),
        "offline_fixture": False,
        "lookback_months": args.lookback_months,
        "min_dense_swaps_per_pool_week": args.min_dense_swaps,
        "candidate_universe_pools_meme_weth_v3": u_cnt,
        "coverage": cov,
        "weekly_regimes": weekly,
        "top_historical_regimes": top,
        "current_vs_historical_within_lookback": compare,
        "dashcam_context": dash,
        "historical_depth": {
            "swap_event_span_days_observed": round(span_days, 4) if span_days is not None else None,
            "min_span_days_required_for_sprint037b": float(args.min_history_span_days),
            "lookback_months_requested": args.lookback_months,
        },
        "regime_verdict": regime_verdict,
        "data_gaps": data_gaps,
        "cost_estimate": {
            "clickhouse": (
                "3 SELECT aggregations: universe count, coverage row, weekly main + dense pool rollups; "
                f"bounded to {args.lookback_months} month window on meme-filtered join."
            ),
            "rpc": "None (Sprint037A).",
        },
        "decision_flags": flags,
        "artifact_refs_present": [p for p in ARTIFACT_REFS if (REPO / p).is_file()],
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(out_csv, weekly, top)
    _write_md(out_md, summary)

    print(
        json.dumps(
            {
                "status": "ok",
                "summary_path": str(out_json),
                "037b_gate": flags["SPRINT037B_REPLAY_ALLOWED"],
                "regime_verdict": regime_verdict,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
