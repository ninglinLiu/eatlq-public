#!/usr/bin/env python3
"""
Sprint091 — Sender-gated paper shadow (smart_money wallet gate).

Only records onset signals when a wallet:smart_money sender buys in the
onset window. Compares vs ungated baseline over a historical window.

Output:
  mvp/audits/wallet_profile_v0/sender_gated/sender_gated_paper_shadow.csv
  mvp/audits/wallet_profile_v0/sender_gated/baseline_comparison.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query, write_csv  # noqa: E402

log = logging.getLogger("sprint091")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sender_gated"
OUT_CSV = OUT_DIR / "sender_gated_paper_shadow.csv"
BASELINE_CSV = REPO / "logs" / "sprint087_expanded_paper_shadow.csv"
DEFAULT_LABELS = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "all_labels.csv"
_LABELS_PATH: Path | None = None


def _labels_path() -> Path:
    if _LABELS_PATH is not None:
        return _LABELS_PATH
    env = os.environ.get("WALLET_LABELS_CSV")
    if env:
        return Path(env)
    return DEFAULT_LABELS


READOUT_PASS = REPO / "mvp" / "runs" / "run-101-sender-gated-supersede-candidate-readout.md"
READOUT_FAIL = OUT_DIR / "sender_gated_failed_readout.md"

CHAIN_ID = 8453
ONSET_THRESH = 5
POOL_MAX_AGE = 172800
ONSET_MAX = 7200

# Repeat-loss sender from R5 loss analysis
DEFAULT_SENDER_BLACKLIST = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43",
}

_PIT_LABELS_DIR: Path | None = None
_USE_PIT_LABELS = False
_USE_SINGLE_TICK = False
_MIN_DISTINCT_SENDERS = 1
_SCORING_MODE = "binary"
_SENDER_SCORES: dict[str, float] = {}


def _sender_blacklist() -> set[str]:
    env = os.environ.get("SENDER_BLACKLIST", "")
    out = set(DEFAULT_SENDER_BLACKLIST)
    if env:
        out |= {a.strip().lower() for a in env.split(",") if a.strip()}
    return out


def load_smart_money_at(signal_ts: int | None = None) -> set[str]:
    """Load smart_money set, optionally point-in-time for signal_ts."""
    if _USE_PIT_LABELS and signal_ts is not None and _PIT_LABELS_DIR:
        day = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        pit_path = _PIT_LABELS_DIR / f"all_labels_pit_{day}.csv"
        strict_path = _PIT_LABELS_DIR / f"all_labels_pit_{day}_strict.csv"
        if strict_path.exists() and (_LABELS_PATH and "strict" in str(_LABELS_PATH)):
            pit_path = strict_path
        if not pit_path.exists():
            # Nearest prior snapshot (OOM host may have sparse coverage)
            best: Path | None = None
            for p in sorted(_PIT_LABELS_DIR.glob("all_labels_pit_*.csv"), reverse=True):
                snap_day = p.stem.replace("all_labels_pit_", "")
                if snap_day <= day:
                    best = p
                    break
            pit_path = best if best else pit_path
        if pit_path.exists():
            out: set[str] = set()
            with pit_path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("is_smart_money", "")).lower() in ("true", "1"):
                        out.add(row["sender_address"].lower())
            return out - _sender_blacklist()
    return load_smart_money()


def load_sender_scores() -> dict[str, float]:
    """Optional pnl z-score proxy from labels CSV columns."""
    out: dict[str, float] = {}
    path = _labels_path()
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addr = (row.get("sender_address") or "").lower()
            if not addr:
                continue
            for col in ("pnl_z_score", "avg_pnl_proxy", "smart_buys"):
                if row.get(col):
                    try:
                        out[addr] = float(row[col])
                        break
                    except ValueError:
                        pass
            if addr not in out and row.get("is_smart_money", "").lower() in ("true", "1"):
                out[addr] = 1.0
    return out


def load_smart_money() -> set[str]:
    out: set[str] = set()
    path = _labels_path()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("is_smart_money", "")).lower() in ("true", "1"):
                    out.add(row["sender_address"].lower())
    return out - _sender_blacklist()


def _pf(vals: list[float]) -> Optional[float]:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def discover_historical_onsets(
    days: int = 7,
    min_heat: int = ONSET_THRESH,
    max_pool_age_hours: int = POOL_MAX_AGE // 3600,
) -> list[dict]:
    max_age_sec = max_pool_age_hours * 3600
    sql = f"""
    WITH first_swap AS (
      SELECT lower(pool_address) AS pool, toUnixTimestamp(min(block_time)) AS first_ts
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id = {CHAIN_ID}
        AND block_time >= now() - INTERVAL {days + 1} DAY
      GROUP BY pool
    ),
    buckets AS (
      SELECT
        lower(s.pool_address) AS pool,
        toUnixTimestamp(toStartOfFiveMinutes(s.block_time)) AS wts,
        count() AS heat
      FROM eatlq.uniswap_v3_swaps_decoded AS s
      WHERE s.chain_id = {CHAIN_ID}
        AND s.block_time >= now() - INTERVAL {days} DAY
      GROUP BY pool, wts
      HAVING heat >= {min_heat}
    ),
    onsets AS (
      SELECT pool, min(wts) AS onset_wts
      FROM buckets
      GROUP BY pool
    )
    SELECT
      o.pool AS pool,
      o.onset_wts AS onset_wts,
      fs.first_ts AS first_ts,
      o.onset_wts - fs.first_ts AS onset_lag,
      b.heat AS onset_heat
    FROM onsets AS o
    INNER JOIN first_swap AS fs ON o.pool = fs.pool
    INNER JOIN buckets AS b ON o.pool = b.pool AND o.onset_wts = b.wts
    WHERE o.onset_wts - fs.first_ts <= {max_age_sec}
      AND b.heat >= {min_heat}
    ORDER BY o.onset_wts DESC
    LIMIT 500
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=120)


def onset_buyers(pool: str, onset_wts: int) -> list[str]:
    """Onset-window distinct buyers (tx.from aligned via swaps_with_buyer)."""
    dt = datetime.fromtimestamp(onset_wts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(onset_wts + 300, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT lower(buyer_address) AS buyer, count() AS n
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >= toDateTime('{dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
      AND amount0 < 0
      AND length(buyer_address) = 42
    GROUP BY buyer
    ORDER BY n DESC
    LIMIT 20
    FORMAT JSONEachRow
    """
    try:
        return [r["buyer"] for r in ch_query(sql, timeout=30)]
    except Exception:
        return []


def onset_senders(pool: str, onset_wts: int) -> list[str]:
    """Alias for onset_buyers (buyer = tx.from semantics)."""
    return onset_buyers(pool, onset_wts)


def forward_tick_return_single(pool: str, onset_wts: int, horizon_sec: int = 1800) -> Optional[float]:
    dt = datetime.fromtimestamp(onset_wts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fwd = datetime.fromtimestamp(onset_wts + horizon_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    WITH e AS (
      SELECT tick FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool.lower()}'
        AND block_time >= toDateTime('{dt}','UTC')
      ORDER BY block_time ASC LIMIT 1
    ),
    x AS (
      SELECT tick FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool.lower()}'
        AND block_time >= toDateTime('{fwd}','UTC')
      ORDER BY block_time ASC LIMIT 1
    )
    SELECT e.tick AS t0, x.tick AS t1 FROM e, x
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
        if rows and rows[0].get("t0") and rows[0].get("t1"):
            t0, t1 = float(rows[0]["t0"]), float(rows[0]["t1"])
            return (t1 - t0) / max(abs(t0), 1)
    except Exception:
        pass
    return None


def forward_tick_return_robust(pool: str, onset_wts: int, horizon_sec: int = 1800) -> Optional[float]:
    dt = datetime.fromtimestamp(onset_wts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(onset_wts + 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fwd_lo = datetime.fromtimestamp(onset_wts + horizon_sec - 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fwd_hi = datetime.fromtimestamp(onset_wts + horizon_sec + 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    WITH e AS (
      SELECT avg(tick) AS t0 FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool.lower()}'
        AND block_time BETWEEN toDateTime('{dt}','UTC') AND toDateTime('{end}','UTC')
    ), x AS (
      SELECT avg(tick) AS t1 FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool.lower()}'
        AND block_time BETWEEN toDateTime('{fwd_lo}','UTC') AND toDateTime('{fwd_hi}','UTC')
    )
    SELECT e.t0, x.t1 FROM e, x
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
        if rows and rows[0].get("t0") and rows[0].get("t1"):
            t0, t1 = float(rows[0]["t0"]), float(rows[0]["t1"])
            return (t1 - t0) / max(abs(t0), 1)
    except Exception:
        pass
    return None


def forward_tick_return(pool: str, onset_wts: int, horizon_sec: int = 1800) -> Optional[float]:
    if _USE_SINGLE_TICK:
        return forward_tick_return_single(pool, onset_wts, horizon_sec=horizon_sec)
    return forward_tick_return_robust(pool, onset_wts, horizon_sec=horizon_sec)


def run_backtest(
    days: int = 7,
    horizon_sec: int = 1800,
    min_heat: int = ONSET_THRESH,
    max_pool_age_hours: int = POOL_MAX_AGE // 3600,
    write_readout_file: bool = True,
    write_csv_file: bool = True,
) -> dict[str, Any]:
    smart = load_smart_money() if not _USE_PIT_LABELS else set()
    if not _USE_PIT_LABELS and not smart:
        raise RuntimeError(f"No smart_money labels loaded from {_labels_path()}")

    onsets = discover_historical_onsets(days, min_heat=min_heat, max_pool_age_hours=max_pool_age_hours)
    log.info(
        "onsets=%d smart_money=%d min_heat=%d max_pool_age_h=%d horizon_sec=%d",
        len(onsets), len(smart), min_heat, max_pool_age_hours, horizon_sec,
    )

    gated_rows: list[dict] = []
    skipped = Counter()
    for o in onsets:
        pool = o["pool"]
        wts = int(o["onset_wts"])
        heat = int(o.get("onset_heat") or 0)
        if heat < min_heat:
            skipped["heat_low"] += 1
            continue
        if int(o.get("onset_lag", 999999)) > ONSET_MAX:
            skipped["onset_lag"] += 1
            continue
        senders = onset_senders(pool, wts)
        gate = load_smart_money_at(wts) if _USE_PIT_LABELS else smart
        if not gate:
            skipped["no_pit_labels"] += 1
            continue
        sm_hits = [s for s in senders if s in gate]
        if not sm_hits:
            skipped["no_smart_money_sender"] += 1
            continue
        if len(set(sm_hits)) < _MIN_DISTINCT_SENDERS:
            skipped["cluster_gate"] += 1
            continue
        sender_score = 0.0
        if _SCORING_MODE == "continuous":
            sender_score = sum(_SENDER_SCORES.get(s, 1.0) for s in set(sm_hits))
        ret = forward_tick_return(pool, wts, horizon_sec=horizon_sec)
        if ret is None:
            skipped["no_forward_tick"] += 1
            continue
        row_out = {
            "signal_time": datetime.fromtimestamp(wts, tz=timezone.utc).isoformat(),
            "pool": pool,
            "onset_heat": heat,
            "smart_sender": sm_hits[0],
            "distinct_smart_senders": len(set(sm_hits)),
            "sender_score": round(sender_score, 4),
            "net_return_30m_tick": round(ret, 6),
            "wallet_gate": "smart_money",
            "scoring_mode": _SCORING_MODE,
            "min_distinct_senders": _MIN_DISTINCT_SENDERS,
        }
        gated_rows.append(row_out)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if write_csv_file:
        write_csv(
            OUT_CSV,
            gated_rows,
            [
                "signal_time", "pool", "onset_heat", "smart_sender",
                "distinct_smart_senders", "sender_score",
                "net_return_30m_tick", "wallet_gate", "scoring_mode", "min_distinct_senders",
            ],
        )

    returns = [float(r["net_return_30m_tick"]) for r in gated_rows]
    pf = _pf(returns)
    pools = [r["pool"] for r in gated_rows]
    top_share = Counter(pools).most_common(1)[0][1] / len(pools) if pools else 0.0

    baseline_pf = None
    baseline_n = 0
    if BASELINE_CSV.exists():
        base_rets = []
        with BASELINE_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                v = row.get("net_return_30m_ag") or row.get("net_return_30m_real")
                if v:
                    try:
                        base_rets.append(float(v))
                    except ValueError:
                        pass
        baseline_n = len(base_rets)
        baseline_pf = _pf(base_rets[-500:]) if base_rets else None

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backtest_days": days,
        "horizon_min": horizon_sec // 60,
        "min_heat": min_heat,
        "max_pool_age_hours": max_pool_age_hours,
        "labels_csv": str(_labels_path()),
        "pit_labels": _USE_PIT_LABELS,
        "single_tick": _USE_SINGLE_TICK,
        "min_distinct_senders": _MIN_DISTINCT_SENDERS,
        "scoring_mode": _SCORING_MODE,
        "sender_blacklist": sorted(_sender_blacklist()),
        "onsets_scanned": len(onsets),
        "gated_signals": len(gated_rows),
        "skipped": dict(skipped),
        "pf_gated_30m_tick": pf,
        "mean_return_gated": sum(returns) / len(returns) if returns else None,
        "unique_pools": len(set(pools)),
        "top_pool_share": top_share,
        "baseline_csv": str(BASELINE_CSV.relative_to(REPO)),
        "baseline_n": baseline_n,
        "baseline_pf_proxy": baseline_pf,
        "RESEARCH_ONLY": True,
    }
    if write_csv_file:
        (OUT_DIR / "baseline_comparison.json").write_text(json.dumps(summary, indent=2))
        if write_readout_file:
            write_readout(summary)
    return summary


def write_readout(summary: dict[str, Any]) -> Path:
    pf = summary.get("pf_gated_30m_tick")
    n = summary.get("gated_signals", 0)
    if pf is not None and pf > 1.5 and n >= 15:
        READOUT_PASS.parent.mkdir(parents=True, exist_ok=True)
        READOUT_PASS.write_text(
            f"# Supersede Candidate — Sender-Gated v0 (Sprint091)\n\n"
            f"**Status:** CANDIDATE ONLY — human review required\n"
            f"**Does NOT lift:** FAILED_NO_7D_ALPHA guard\n\n"
            f"## Economics (CH tick proxy)\n\n"
            f"- PF: {pf}\n"
            f"- n: {n}\n"
            f"- horizon_min: {summary.get('horizon_min')}\n"
            f"- unique_pools: {summary.get('unique_pools')}\n"
            f"- top_pool_share: {summary.get('top_pool_share')}\n"
            f"- baseline_pf_proxy: {summary.get('baseline_pf_proxy')}\n\n"
            f"Generated: {summary.get('generated_at')}\n"
        )
        return READOUT_PASS

    READOUT_FAIL.write_text(
        f"# Sender-Gated v0 — Failed Readout (Sprint091)\n\n"
        f"白皮书 sender-first 路线在 EATLQ 数据集上仍未过线。\n\n"
        f"- pf_gated_30m_tick: {pf}\n"
        f"- gated_signals: {n}\n"
        f"- horizon_min: {summary.get('horizon_min')}\n"
        f"- top_pool_share: {summary.get('top_pool_share')}\n"
        f"- baseline_pf_proxy: {summary.get('baseline_pf_proxy')}\n\n"
        f"Guard remains in place. This is an honest alignment readout.\n\n"
        f"Generated: {summary.get('generated_at')}\n"
    )
    return READOUT_FAIL


def main() -> None:
    global _LABELS_PATH, _PIT_LABELS_DIR, _USE_PIT_LABELS, _USE_SINGLE_TICK
    global _MIN_DISTINCT_SENDERS, _SCORING_MODE, _SENDER_SCORES
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Sprint091 sender-gated paper shadow")
    p.add_argument("--backtest-days", type=int, default=7)
    p.add_argument("--horizon-min", type=int, default=30)
    p.add_argument("--min-heat", type=int, default=ONSET_THRESH)
    p.add_argument("--max-pool-age-hours", type=int, default=POOL_MAX_AGE // 3600)
    p.add_argument("--labels-csv", type=str, default=None)
    p.add_argument("--pit-labels-dir", type=str, default=None, help="Point-in-time label snapshots dir")
    p.add_argument("--single-tick", action="store_true", help="Use single-tick forward return")
    p.add_argument("--min-distinct-senders", type=int, default=2, help="Cluster gate: min distinct labeled senders")
    p.add_argument("--scoring", choices=("binary", "continuous"), default="binary")
    p.add_argument("--no-readout", action="store_true")
    p.add_argument("--output-json", type=str, default=None)
    args = p.parse_args()

    if args.labels_csv:
        _LABELS_PATH = Path(args.labels_csv)
    elif os.environ.get("WALLET_LABELS_CSV"):
        _LABELS_PATH = Path(os.environ["WALLET_LABELS_CSV"])

    if args.pit_labels_dir:
        _PIT_LABELS_DIR = Path(args.pit_labels_dir)
        _USE_PIT_LABELS = True
    elif os.environ.get("PIT_LABELS_DIR"):
        _PIT_LABELS_DIR = Path(os.environ["PIT_LABELS_DIR"])
        _USE_PIT_LABELS = True

    _USE_SINGLE_TICK = args.single_tick
    _MIN_DISTINCT_SENDERS = max(1, args.min_distinct_senders)
    _SCORING_MODE = args.scoring
    _SENDER_SCORES = load_sender_scores() if _SCORING_MODE == "continuous" else {}

    summary = run_backtest(
        days=args.backtest_days,
        horizon_sec=args.horizon_min * 60,
        min_heat=args.min_heat,
        max_pool_age_hours=args.max_pool_age_hours,
        write_readout_file=not args.no_readout,
    )
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
