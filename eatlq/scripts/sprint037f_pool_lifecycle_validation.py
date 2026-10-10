#!/usr/bin/env python3
"""Sprint037F — Pool-level lifecycle validation (CH swaps + Sprint037C Q4 CSV labels).

Read-only ClickHouse; no RPC; labels reused from q4_candidates CSVs.

Writes:
  mvp/audits/sprint037f/pool_lifecycle_summary.json
  mvp/audits/sprint037f/pool_lifecycle_bucket_readout.csv
  mvp/audits/sprint037f/pool_lifecycle_holdout_readout.csv
  mvp/runs/run-037F-pool-lifecycle-holdout-readout.md

Usage:
  CLICKHOUSE_URL=... CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD=... \\
    python3 scripts/sprint037f_pool_lifecycle_validation.py
"""

from __future__ import annotations

import bisect
import csv
import json
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

REPO = pathlib.Path(__file__).resolve().parents[1]
Q4_DIR = REPO / "mvp/audits/sprint037c"
OUT_DIR = REPO / "mvp/audits/sprint037f"
RUNS_DIR = REPO / "mvp/runs"
SPRINT037D_SUMMARY = REPO / "mvp/diagnostics/sprint037d_summary.json"
SPRINT037E_SUMMARY = REPO / "mvp/audits/sprint037e/lifecycle_validation_summary.json"

CHAIN_ID = 8453
REGIME_SEC = 7 * 86400
HOLDOUT_FRAC = 0.30
HORIZONS = ["5m", "10m", "15m", "30m"]

REGIMES = ["2026-02-02", "2026-01-26", "2026-01-05", "2025-12-01"]

# Predeclared thresholds — NOT tuned on holdout
ONSET_BUCKET_SEC = 300
ONSET_COUNT_THRESH = 10
SWAPS_HIGH_ACTIVITY_15M = 60  # "high activity"
PRIOR_RUNUP_TICK_SMALL = 35  # abs delta threshold — scale ticks ~ price proxy
EXHAUSTION_CAP = 2.25  # anti-exhaustion gate
POOL_YOUNG_6H = 6 * 3600
POOL_YOUNG_24H = 24 * 3600


def _log(msg: str) -> None:
    print(f"[f {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _utc_week_start_ts(iso_week: str) -> float:
    return datetime.strptime(iso_week, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r}")


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 600.0) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def parse_tick_hex(raw: str | None) -> int | None:
    if not raw:
        return None
    h = raw[2:] if raw.startswith("0x") else raw
    if len(h) < 64:
        h = h.zfill(64)
    try:
        v = int(h[-64:], 16)
    except ValueError:
        return None
    if v >= 2**255:
        v -= 2**256
    return int(v)


def load_candidates() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {r: [] for r in REGIMES}
    for reg in REGIMES:
        path = Q4_DIR / f"q4_candidates_{reg}.csv"
        if not path.exists():
            continue
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                row["entry_time_sec"] = int(float(row["entry_time_sec"]))
                row["regime_week"] = reg
                row["pool_address_lower"] = str(row.get("pool_address") or "").lower()
                for k in ("activity_heat",):
                    if row.get(k) not in (None, ""):
                        row[k] = int(float(row[k]))
                for k in ("price_velocity_5m", "buy_pressure_ratio", "token_volatility_5m"):
                    if row.get(k) not in (None, ""):
                        row[k] = float(row[k])
                    else:
                        row[k] = None
                for h in HORIZONS:
                    key = f"net_return_{h}"
                    v = row.get(key)
                    row[key] = float(v) if v not in (None, "", "None") else None
                t0 = _utc_week_start_ts(reg)
                elapsed = row["entry_time_sec"] - t0
                row["regime_elapsed_pct"] = max(0.0, min(1.0, elapsed / REGIME_SEC))
                out[reg].append(row)
    return out


def regime_activity_q3_cut(rows: list[dict]) -> int:
    vals = sorted(int(r.get("activity_heat") or 0) for r in rows)
    return vals[max(0, 3 * len(vals) // 4 - 1)] if vals else 0


def attach_q3(all_by_regime: dict[str, list[dict]]) -> None:
    for reg, rows in all_by_regime.items():
        q3 = regime_activity_q3_cut(rows)
        for r in rows:
            r["_regime_activity_q3"] = q3


def fetch_pool_first_seen(
    ch_url: str, ch_user: str, ch_pwd: str, pools: list[str]
) -> dict[str, int]:
    if not pools:
        return {}
    pools = sorted(set(p.lower() for p in pools if p))
    chunk = 120
    out: dict[str, int] = {}
    for i in range(0, len(pools), chunk):
        part = pools[i : i + chunk]
        in_list = ",".join(f"'{p}'" for p in part)
        sql = f"""
SELECT lower(pool_address) AS pool_address,
       toUnixTimestamp(min(coalesce(block_time, received_at_ms))) AS fst
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND pool_address IN ({in_list})
GROUP BY pool_address
"""
        rows = _ch_query(ch_url, sql, ch_user, ch_pwd)
        for r in rows:
            out[str(r["pool_address"]).lower()] = int(r["fst"])
    return out


def fetch_swaps_regime_window(
    ch_url: str,
    ch_user: str,
    ch_pwd: str,
    pools: list[str],
    ts_min: int,
    ts_max: int,
) -> dict[str, list[tuple[int, int | None]]]:
    """Return pool -> sorted list of (ts_sec, tick_or_none)."""
    pools = sorted(set(p.lower() for p in pools if p))
    if not pools:
        return {}
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    res: dict[str, list[tuple[int, int | None]]] = {p: [] for p in pools}
    chunk = 80
    for i in range(0, len(pools), chunk):
        part = pools[i : i + chunk]
        in_list = ",".join(f"'{p}'" for p in part)
        sql = f"""
SELECT lower(pool_address) AS pool_address,
       toUnixTimestamp(coalesce(block_time, received_at_ms)) AS ts_sec,
       tick_raw AS tick_raw
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND pool_address IN ({in_list})
  AND coalesce(block_time, received_at_ms) >= toDateTime64('{dt_min}', 3, 'UTC')
  AND coalesce(block_time, received_at_ms) < toDateTime64('{dt_max}', 3, 'UTC')
"""
        rows = _ch_query(ch_url, sql, ch_user, ch_pwd)
        for r in rows:
            pool = str(r["pool_address"]).lower()
            ts = int(r["ts_sec"])
            tk = parse_tick_hex(r.get("tick_raw"))
            res.setdefault(pool, []).append((ts, tk))
    for pool in list(res.keys()):
        res[pool].sort(key=lambda x: x[0])
    return res


def last_tick_before(ts_list: list[int], ticks: list[int | None], cutoff: int) -> int | None:
    """Last tick where ts < cutoff."""
    idx = bisect.bisect_left(ts_list, cutoff)
    if idx == 0:
        return None
    i = idx - 1
    while i >= 0 and ticks[i] is None:
        i -= 1
    return ticks[i] if i >= 0 else None


def count_swaps_between(ts_list: list[int], lo: int, hi_excl: int) -> int:
    """Count ts where lo <= ts < hi_excl."""
    if hi_excl <= lo:
        return 0
    a = bisect.bisect_left(ts_list, lo)
    b = bisect.bisect_left(ts_list, hi_excl)
    return max(0, b - a)


def compute_activity_onset(ts_list: list[int], entry_ts: int, pool_first_seen: int) -> int | None:
    """First bucket start (multiple of ONSET_BUCKET_SEC from epoch...) such that
    trailing ONSET_BUCKET_SEC window ending at bucket_end has >= ONSET_COUNT_THRESH swaps."""
    if not ts_list:
        return None
    # Align buckets to absolute windows ending at entry for determinism
    earliest = max(pool_first_seen, ts_list[0])
    end_cap = entry_ts
    t = earliest + ONSET_BUCKET_SEC
    while t <= end_cap:
        c = count_swaps_between(ts_list, t - ONSET_BUCKET_SEC, t)
        if c >= ONSET_COUNT_THRESH:
            return t - ONSET_BUCKET_SEC  # onset bucket start
        t += ONSET_BUCKET_SEC
    return None


def enrich_candidate(
    row: dict[str, Any],
    ts_list: list[int],
    ticks: list[int | None],
    pool_global_first: dict[str, int],
) -> None:
    pool = row["pool_address_lower"]
    ent = int(row["entry_time_sec"])
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else ent
    row["pool_first_seen_ts_global"] = gfs
    row["pool_age_sec_at_entry"] = max(0, ent - gfs)

    row["swaps_last_5m"] = count_swaps_between(ts_list, ent - 300, ent)
    row["swaps_last_15m"] = count_swaps_between(ts_list, ent - 900, ent)
    row["swaps_last_1h"] = count_swaps_between(ts_list, ent - 3600, ent)
    row["swaps_last_3h"] = count_swaps_between(ts_list, ent - 10800, ent)
    row["cumulative_swaps_before_entry"] = count_swaps_between(ts_list, 0, ent)

    onset = compute_activity_onset(ts_list, ent, gfs)
    row["activity_onset_ts"] = onset
    row["time_since_activity_onset_sec"] = (
        ent - onset if onset is not None else None
    )

    tick_ent = last_tick_before(ts_list, ticks, ent)
    tick_m15 = last_tick_before(ts_list, ticks, ent - 900)
    tick_1h = last_tick_before(ts_list, ticks, ent - 3600)
    tick_3h = last_tick_before(ts_list, ticks, ent - 10800)

    def delta(a: int | None, b: int | None) -> int | None:
        if a is None or b is None:
            return None
        return a - b

    row["prior_runup_tick_15m"] = delta(tick_ent, tick_m15)
    row["prior_runup_tick_1h"] = delta(tick_ent, tick_1h)
    row["prior_runup_tick_3h"] = delta(tick_ent, tick_3h)

    mid_lo = ent - 3600
    mid_hi = ent - 1800
    tick_mid_start = last_tick_before(ts_list, ticks, mid_lo)
    tick_mid_end = last_tick_before(ts_list, ticks, mid_hi)
    dd_mid = (
        delta(tick_mid_end, tick_mid_start)
        if tick_mid_start is not None and tick_mid_end is not None
        else None
    )
    pull_recent = delta(tick_ent, tick_mid_end) if tick_mid_end is not None else None
    row["prior_drawdown_or_reversal_proxy"] = (
        float(dd_mid or 0) - float(pull_recent or 0)
        if dd_mid is not None or pull_recent is not None
        else None
    )

    prev15_lo = ent - 1800
    prev15_hi = ent - 900
    swaps_prev15 = count_swaps_between(ts_list, prev15_lo, prev15_hi)
    row["exhaustion_score"] = row["swaps_last_15m"] / (1.0 + swaps_prev15)

    # Bucket labels
    age = row["pool_age_sec_at_entry"]
    if age <= POOL_YOUNG_6H:
        row["pool_age_bucket"] = "0-6h"
    elif age <= POOL_YOUNG_24H:
        row["pool_age_bucket"] = "6h-24h"
    elif age <= 7 * 86400:
        row["pool_age_bucket"] = "1d-7d"
    else:
        row["pool_age_bucket"] = ">7d"

    toe = row["time_since_activity_onset_sec"]
    if toe is None:
        row["onset_bucket"] = "unknown"
    elif toe <= 3600:
        row["onset_bucket"] = "0-1h"
    elif toe <= 6 * 3600:
        row["onset_bucket"] = "1h-6h"
    elif toe <= 24 * 3600:
        row["onset_bucket"] = "6h-24h"
    else:
        row["onset_bucket"] = ">24h"


def split_chrono(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda x: x["entry_time_sec"])
    n = len(rows)
    cut = max(1, int(n * (1 - HOLDOUT_FRAC)))
    return rows[:cut], rows[cut:]


def stats_returns(rets: list[float]) -> dict[str, Any]:
    rs = [x for x in rets if x == x]
    if not rs:
        return {
            "n": 0,
            "label_1_share": None,
            "mean": None,
            "median": None,
            "trimmed_mean": None,
            "profit_factor": None,
            "p10": None,
            "p90": None,
        }
    rs.sort()
    n = len(rs)
    k = max(1, n // 10)
    tr = rs[k:-k] if n > 2 * k else rs
    pos = [x for x in rs if x > 0]
    neg = [x for x in rs if x < 0]
    gp, gl = sum(pos), abs(sum(neg))
    pf = (gp / gl) if gl > 1e-12 else None
    mean_v = sum(rs) / n
    return {
        "n": n,
        "label_1_share": round(len(pos) / n, 6),
        "mean": round(mean_v, 6),
        "median": round(rs[n // 2], 6),
        "trimmed_mean": round(sum(tr) / max(len(tr), 1), 6),
        "profit_factor": round(pf, 6) if pf is not None else None,
        "p10": round(rs[max(0, n // 10)], 6),
        "p90": round(rs[min(n - 1, 9 * n // 10)], 6),
    }


def pool_conc(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"unique_pools": 0, "top1": None, "top5": None}
    ctr = Counter(r["pool_address_lower"] for r in rows)
    n = len(rows)
    mv = ctr.most_common()
    return {
        "unique_pools": len(ctr),
        "top1": round(mv[0][1] / n, 6) if mv else None,
        "top5": round(sum(c for _, c in mv[:5]) / n, 6) if mv else None,
    }


def horizon_agg(rows: list[dict], h: str) -> dict[str, Any]:
    key = f"net_return_{h}"
    st = stats_returns([r[key] for r in rows if r.get(key) is not None])
    pc = pool_conc(rows)
    st.update(pc)
    return st


def evaluate_gate(
    all_by_regime: dict[str, list[dict]],
    pred: Callable[[dict], bool],
) -> dict[str, Any]:
    ho_all: list[dict] = []
    is_all: list[dict] = []
    per_reg_ho: dict[str, dict[str, Any]] = {}
    for reg in REGIMES:
        ins, ho = split_chrono(all_by_regime[reg])
        gi = [r for r in ins if pred(r)]
        gh = [r for r in ho if pred(r)]
        is_all.extend(gi)
        ho_all.extend(gh)
        per_reg_ho[reg] = {h: horizon_agg(gh, h) for h in HORIZONS}
    agg_hold = {h: horizon_agg(ho_all, h) for h in HORIZONS}
    h30 = agg_hold["30m"]
    regimes_pos = sum(
        1
        for reg in REGIMES
        if (per_reg_ho.get(reg, {}).get("30m", {}).get("mean") or -1) > 0
    )
    supersession = (
        (h30.get("mean") or -1) > 0
        and (h30.get("profit_factor") or 0) > 1.0
        and regimes_pos >= 3
    )
    return {
        "holdout_n_total": len(ho_all),
        "insample_n_total": len(is_all),
        "aggregate_holdout": agg_hold,
        "per_regime_holdout": per_reg_ho,
        "regimes_holdout_mean_positive_30m": regimes_pos,
        "supersession_candidate_30m": supersession,
    }


def patch_gate_defs() -> dict[str, Callable[[dict], bool]]:
    """Avoid lambda closure bugs — explicit dict."""
    def q4_ah_cut(r: dict) -> bool:
        return int(r.get("activity_heat") or 0) >= int(r.get("_regime_activity_q3") or 10**9)

    def regime_early40(r: dict) -> bool:
        return r["regime_elapsed_pct"] <= 0.40

    def flow_pos(r: dict) -> bool:
        return float(r.get("price_velocity_5m") or 0) > 1e-12

    return {
        "baseline_q4_all": lambda r: True,
        "repro037e_early40_activity_heat_q4": lambda r: regime_early40(r) and q4_ah_cut(r),
        "pool_age_young_le_6h": lambda r: r.get("pool_age_sec_at_entry", 10**12)
        <= POOL_YOUNG_6H,
        "pool_age_young_le_24h": lambda r: r.get("pool_age_sec_at_entry", 10**12)
        <= POOL_YOUNG_24H,
        "onset_early_le_1h": lambda r: (r.get("time_since_activity_onset_sec") or 10**12)
        <= 3600,
        "onset_early_le_6h": lambda r: (r.get("time_since_activity_onset_sec") or 10**12)
        <= 6 * 3600,
        "low_prior_runup_15m": lambda r: r.get("prior_runup_tick_15m") is not None
        and abs(int(r["prior_runup_tick_15m"])) <= PRIOR_RUNUP_TICK_SMALL,
        "high_activity_low_prior_runup": lambda r: int(r.get("swaps_last_15m") or 0)
        >= SWAPS_HIGH_ACTIVITY_15M
        and r.get("prior_runup_tick_15m") is not None
        and abs(int(r["prior_runup_tick_15m"])) <= PRIOR_RUNUP_TICK_SMALL + 15,
        "q4_young_pool_le24h": lambda r: q4_ah_cut(r)
        and r.get("pool_age_sec_at_entry", 10**12) <= POOL_YOUNG_24H,
        "q4_early_onset_le6h": lambda r: q4_ah_cut(r)
        and (r.get("time_since_activity_onset_sec") or 10**12) <= 6 * 3600,
        "q4_anti_exhaustion": lambda r: q4_ah_cut(r)
        and float(r.get("exhaustion_score") or 99) < EXHAUSTION_CAP,
        "q4_young_onset_flow": lambda r: q4_ah_cut(r)
        and r.get("pool_age_sec_at_entry", 10**12) <= POOL_YOUNG_24H
        and (r.get("time_since_activity_onset_sec") or 10**12) <= 6 * 3600
        and flow_pos(r),
        "young_pool_onset_flow": lambda r: r.get("pool_age_sec_at_entry", 10**12)
        <= POOL_YOUNG_24H
        and (r.get("time_since_activity_onset_sec") or 10**12) <= 3600
        and flow_pos(r),
        "q4_buy_pressure_hi": lambda r: q4_ah_cut(r)
        and float(r.get("buy_pressure_ratio") or 0) >= 0.52,
    }


def baseline_holdout_stats(all_flat: list[dict]) -> dict[str, Any]:
    hold_rows: list[dict] = []
    for reg in REGIMES:
        _, ho = split_chrono([r for r in all_flat if r["regime_week"] == reg])
        hold_rows.extend(ho)
    return horizon_agg(hold_rows, "30m")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    all_by_regime = load_candidates()
    attach_q3(all_by_regime)
    all_flat = [r for reg in REGIMES for r in all_by_regime[reg]]

    ref_mean = ref_n = None
    if SPRINT037D_SUMMARY.exists():
        sd = json.loads(SPRINT037D_SUMMARY.read_text())
        ref = sd.get("aggregate_holdout", {}).get("30m", {})
        ref_mean, ref_n = ref.get("mean"), ref.get("n")

    ref037e_mean = ref037e_n = None
    if SPRINT037E_SUMMARY.exists():
        se = json.loads(SPRINT037E_SUMMARY.read_text())
        g = se.get("gates", {}).get("early40_activity_heat_q4", {})
        h = g.get("aggregate_holdout", {}).get("30m", {})
        ref037e_mean, ref037e_n = h.get("mean"), h.get("n")

    GATES = patch_gate_defs()
    GATE_ORDER = list(GATES.keys())

    baseline_h30 = baseline_holdout_stats(all_flat)
    repro037d = (
        ref_mean is not None
        and baseline_h30.get("mean") is not None
        and abs(baseline_h30["mean"] - ref_mean) < 1e-4
        and baseline_h30.get("n") == ref_n
    )

    repro_gate_pre = evaluate_gate(all_by_regime, GATES["repro037e_early40_activity_heat_q4"])
    rg30 = repro_gate_pre["aggregate_holdout"]["30m"]
    repro037e = (
        ref037e_mean is not None
        and rg30.get("mean") is not None
        and abs(rg30["mean"] - ref037e_mean) < 0.002
        and abs(rg30.get("n", 0) - (ref037e_n or 0)) <= 5
    )

    _log(f"pre-CH baseline repro037d={repro037d} mean={baseline_h30.get('mean')} n={baseline_h30.get('n')}")
    _log(f"pre-CH repro037e thin gate mean={rg30.get('mean')} n={rg30.get('n')} match={repro037e}")

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset — cannot fetch swaps")
        sys.exit(2)

    pools_all = list({r["pool_address_lower"] for r in all_flat if r.get("pool_address_lower")})
    _log(f"fetch global first-seen for {len(pools_all)} pools …")
    pool_global_first = fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)

    for reg in REGIMES:
        rows = all_by_regime[reg]
        if not rows:
            continue
        pools = list({r["pool_address_lower"] for r in rows})
        ts_min = min(r["entry_time_sec"] for r in rows) - 72 * 3600
        ts_max = max(r["entry_time_sec"] for r in rows) + 600
        _log(f"regime {reg}: fetch swaps window [{ts_min},{ts_max}] pools={len(pools)} …")
        swaps_by_pool = fetch_swaps_regime_window(
            ch_url, ch_user, ch_pwd, pools, ts_min, ts_max
        )
        for r in rows:
            pool = r["pool_address_lower"]
            raw_sw = swaps_by_pool.get(pool, [])
            ts_list = [x[0] for x in raw_sw]
            ticks = [x[1] for x in raw_sw]
            enrich_candidate(r, ts_list, ticks, pool_global_first)

    baseline_h30 = baseline_holdout_stats(all_flat)
    repro037d = (
        ref_mean is not None
        and baseline_h30.get("mean") is not None
        and abs(baseline_h30["mean"] - ref_mean) < 1e-4
        and baseline_h30.get("n") == ref_n
    )
    repro_gate_post = evaluate_gate(all_by_regime, GATES["repro037e_early40_activity_heat_q4"])
    rg30 = repro_gate_post["aggregate_holdout"]["30m"]
    repro037e = (
        ref037e_mean is not None
        and rg30.get("mean") is not None
        and abs(rg30["mean"] - ref037e_mean) < 0.002
        and abs(rg30.get("n", 0) - (ref037e_n or 0)) <= 5
    )

    gate_results: dict[str, Any] = {}
    holdout_csv_rows: list[dict[str, Any]] = []

    for name in GATE_ORDER:
        gr = evaluate_gate(all_by_regime, GATES[name])
        gate_results[name] = gr
        row: dict[str, Any] = {
            "gate": name,
            "holdout_n": gr["holdout_n_total"],
            "insample_n": gr["insample_n_total"],
            "regimes_holdout_mean_pos_30m": gr["regimes_holdout_mean_positive_30m"],
            "supersession_candidate": gr["supersession_candidate_30m"],
        }
        for h in HORIZONS:
            a = gr["aggregate_holdout"][h]
            row[f"{h}_mean"] = a.get("mean")
            row[f"{h}_pf"] = a.get("profit_factor")
            row[f"{h}_label_1_share"] = a.get("label_1_share")
            row[f"{h}_median"] = a.get("median")
            row[f"{h}_trimmed_mean"] = a.get("trimmed_mean")
            row[f"{h}_p10"] = a.get("p10")
            row[f"{h}_p90"] = a.get("p90")
            row[f"{h}_unique_pools"] = a.get("unique_pools")
            row[f"{h}_pool_top1"] = a.get("top1")
            row[f"{h}_pool_top5"] = a.get("top5")
        holdout_csv_rows.append(row)

    ranked = sorted(
        (
            (
                gate_results[g]["aggregate_holdout"]["30m"]["mean"] or -999,
                gate_results[g]["aggregate_holdout"]["30m"]["profit_factor"] or 0,
                gate_results[g]["holdout_n_total"],
                g,
            )
            for g in GATE_ORDER
            if gate_results[g]["holdout_n_total"] >= 80
        ),
        reverse=True,
    )
    best_gate = ranked[0][3] if ranked else "none"

    any_super = any(gate_results[g]["supersession_candidate_30m"] for g in GATE_ORDER)

    scoped_only = (
        not any_super
        and best_gate != "none"
        and (
            gate_results[best_gate]["regimes_holdout_mean_positive_30m"] <= 2
            or gate_results[best_gate]["holdout_n_total"] < 250
        )
    )

    if not repro037d:
        status = "BLOCKED_BY_SCHEMA_OR_DATA"
    elif any_super:
        status = "POOL_LIFECYCLE_HOLDOUT_POSITIVE_REVIEW_REQUIRED"
    elif scoped_only:
        status = "SCOPED_POOL_LIFECYCLE_EDGE_ONLY"
    elif (
        (gate_results[best_gate]["aggregate_holdout"]["30m"]["mean"] or 0)
        > (gate_results["baseline_q4_all"]["aggregate_holdout"]["30m"]["mean"] or -9)
    ):
        status = "POOL_LIFECYCLE_IMPROVES_BUT_NOT_ENOUGH"
    elif (gate_results["baseline_q4_all"]["aggregate_holdout"]["30m"]["mean"] or 0) < 0:
        status = "ROUTE_PIVOT_OR_FEATURE_GAP"
    else:
        status = "HOLDOUT_STILL_NEGATIVE"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Lifecycle buckets — pool age & CH-derived onset buckets per regime
    bucket_rows: list[dict[str, Any]] = []
    onset_buckets = ["0-1h", "1h-6h", "6h-24h", ">24h", "unknown"]
    for reg in REGIMES:
        for bucket in ["0-6h", "6h-24h", "1d-7d", ">7d"]:
            sub = [r for r in all_by_regime[reg] if r.get("pool_age_bucket") == bucket]
            br: dict[str, Any] = {
                "regime_week": reg,
                "lifecycle_dimension": "pool_age",
                "bucket": bucket,
                "n": len(sub),
            }
            for h in HORIZONS:
                st = horizon_agg(sub, h)
                br[f"{h}_mean"] = st.get("mean")
                br[f"{h}_pf"] = st.get("profit_factor")
                br[f"{h}_label_1_share"] = st.get("label_1_share")
            pc = pool_conc(sub)
            br["unique_pools"] = pc["unique_pools"]
            br["pool_top1"] = pc["top1"]
            br["pool_top5"] = pc["top5"]
            bucket_rows.append(br)
        for bucket in onset_buckets:
            sub = [r for r in all_by_regime[reg] if r.get("onset_bucket") == bucket]
            br = {
                "regime_week": reg,
                "lifecycle_dimension": "activity_onset",
                "bucket": bucket,
                "n": len(sub),
            }
            for h in HORIZONS:
                st = horizon_agg(sub, h)
                br[f"{h}_mean"] = st.get("mean")
                br[f"{h}_pf"] = st.get("profit_factor")
                br[f"{h}_label_1_share"] = st.get("label_1_share")
            pc = pool_conc(sub)
            br["unique_pools"] = pc["unique_pools"]
            br["pool_top1"] = pc["top1"]
            br["pool_top5"] = pc["top5"]
            bucket_rows.append(br)

    summary = {
        "kind": "sprint037f_pool_lifecycle_validation",
        "created_at": now,
        "status": status,
        "repro037d_baseline_holdout_30m": repro037d,
        "repro037e_thin_gate": repro037e,
        "baseline_holdout_30m_ref": {"mean": ref_mean, "n": ref_n},
        "baseline_holdout_30m_post_ch": {
            "mean": baseline_h30.get("mean"),
            "n": baseline_h30.get("n"),
        },
        "repro037e_holdout_30m_ref": {"mean": ref037e_mean, "n": ref037e_n},
        "repro037e_holdout_30m_post_ch": {
            "mean": rg30.get("mean"),
            "n": rg30.get("n"),
        },
        "best_gate_min_holdout_n80": best_gate,
        "any_supersession_candidate_30m": any_super,
        "scoped_pool_edge_only_heuristic": scoped_only,
        "thin_holdout_best_gate_n_lt_300": gate_results.get(best_gate, {}).get(
            "holdout_n_total", 0
        )
        < 300,
        "notes": [
            "Formal Gate 0 supersession still requires docs/phase6-resolution-criteria.md "
            "human review; candidate economics alone does not lift FAILED_NO_7D_ALPHA.",
            "If supersession_candidate is true but holdout_n < 250, treat as review-required "
            "with thin-sample caveat.",
        ],
        "gates": gate_results,
        "predeclared_thresholds": {
            "ONSET_BUCKET_SEC": ONSET_BUCKET_SEC,
            "ONSET_COUNT_THRESH": ONSET_COUNT_THRESH,
            "SWAPS_HIGH_ACTIVITY_15M": SWAPS_HIGH_ACTIVITY_15M,
            "PRIOR_RUNUP_TICK_SMALL": PRIOR_RUNUP_TICK_SMALL,
            "EXHAUSTION_CAP": EXHAUSTION_CAP,
        },
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "TRAINING_ALLOWED": False,
            "TRAINING_REVIEW_ALLOWED": False,
            "RPC_BACKED_LABEL_VALIDATION_ALLOWED": True,
            "MULTI_HORIZON_RESEARCH_ALLOWED": True,
            "H12_ABLATION_ALLOWED": False,
            "PHASE6_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "NO_ALPHA_CLAIM": True,
        },
    }

    (OUT_DIR / "pool_lifecycle_summary.json").write_text(json.dumps(summary, indent=2))

    if bucket_rows:
        with (OUT_DIR / "pool_lifecycle_bucket_readout.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(bucket_rows[0].keys()))
            w.writeheader()
            w.writerows(bucket_rows)

    if holdout_csv_rows:
        with (OUT_DIR / "pool_lifecycle_holdout_readout.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(holdout_csv_rows[0].keys()))
            w.writeheader()
            w.writerows(holdout_csv_rows)

    md = [
        "# Run-037F — Pool lifecycle holdout readout",
        "",
        f"Generated: {now}",
        "",
        "## Status",
        "",
        status,
        "",
        "## Reproduction checks",
        "",
        f"- Sprint037D baseline holdout 30m reproduced: **{repro037d}**",
        f"- Sprint037E early40_activity_heat_q4 reproduced (~match): **{repro037e}**",
        "",
        "## Best gate (holdout 30m mean, holdout_n≥80)",
        "",
        f"`{best_gate}`",
        "",
        "## Supersession candidate",
        "",
        str(any_super),
        "",
        "### Thin-sample caveat",
        "",
        f"- Best gate holdout_n={gate_results.get(best_gate, {}).get('holdout_n_total')}",
        "- Aggregate PF/mean can hide tail risk; see per-regime rows and concentration.",
        "",
        "| Gate | hold_n | 30m mean | 30m PF | regimes mean>0 | super? |",
        "|------|--------|---------|--------|----------------|--------|",
    ]
    for name in GATE_ORDER:
        g = gate_results[name]
        a = g["aggregate_holdout"]["30m"]
        md.append(
            f"| {name} | {g['holdout_n_total']} | {a.get('mean')} | {a.get('profit_factor')} | "
            f"{g['regimes_holdout_mean_positive_30m']} | {g['supersession_candidate_30m']} |"
        )
    md += ["", "`NO_ALPHA_CLAIM=true` — research artifact only.", ""]
    (RUNS_DIR / "run-037F-pool-lifecycle-holdout-readout.md").write_text("\n".join(md))

    _log(f"status={status} best_gate={best_gate} supersession={any_super}")
    _log(f"wrote {OUT_DIR} + run-037F markdown")


if __name__ == "__main__":
    main()
