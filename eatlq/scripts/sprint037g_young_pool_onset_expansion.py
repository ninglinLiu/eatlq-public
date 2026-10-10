#!/usr/bin/env python3
"""Sprint037G — Expansion validation for young_pool_onset_flow (Sprint037F).

- Discovers all mvp/audits/sprint037c/q4_candidates_*.csv (variable regime count).
- Reuses CH swap streams + labels (no RPC exit relabel).
- Predeclared 3×3×3 parameter neighborhood around Sprint037F defaults:
    pool_max_age_sec ∈ {18h, 24h, 30h}
    onset_max_sec   ∈ {45m, 60m, 75m}
    onset_swaps_thresh ∈ {8, 10, 12}  (in trailing ONSET_BUCKET_SEC window)

Strict sprint gates (NOT tuned on holdout — thresholds chosen before inspection):
  * Aggregate holdout 30m mean > 0 AND PF > 1.
  * At least `required_floor_regimes` regimes each have gate holdout_30m n ≥ MIN_HOLDOUT_PER_REGIME
    AND holdout 30m mean > 0 in those regimes.
  * Stability: fraction of grid combos passing strict_ok ≥ STABILITY_FRAC_MIN.

If baseline-equivalent combo passes strict checks AND stability OK, writes formal Gate 0 draft:
  mvp/runs/run-037G-young-pool-onset-supersession-readout.md
Otherwise same path documents withheld reasons.

Artifacts:
  mvp/audits/sprint037g/expansion_summary.json
  mvp/audits/sprint037g/param_stability_grid.csv
  mvp/audits/sprint037g/per_regime_floor_audit.csv
  mvp/runs/run-037G-young-pool-onset-expansion-readout.md

Env:
  CLICKHOUSE_URL / CLICKHOUSE_USER / CLICKHOUSE_PASSWORD — required for enrichment.
  SPRINT037G_MIN_HOLDOUT_PER_REGIME (default 35)
  SPRINT037G_STABILITY_FRAC (default 0.67)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from urllib.request import Request, urlopen

REPO = pathlib.Path(__file__).resolve().parents[1]
Q4_DIR = REPO / "mvp/audits/sprint037c"
OUT_DIR = REPO / "mvp/audits/sprint037g"
RUNS_DIR = REPO / "mvp/runs"

CHAIN_ID = 8453
REGIME_SEC = 7 * 86400
HOLDOUT_FRAC = 0.30
HORIZONS = ["5m", "10m", "15m", "30m"]
ONSET_BUCKET_SEC = 300
FLOW_EPS = 1e-12

# Parameter neighborhood (fixed Cartesian grid — no holdout tuning)
GRID_POOL_MAX_AGE_SEC = sorted({18 * 3600, 24 * 3600, 30 * 3600})
GRID_ONSET_MAX_SEC = sorted({45 * 60, 60 * 60, 75 * 60})
GRID_ONSET_COUNT_THRESH = sorted({8, 10, 12})

# Baseline-equivalent / Sprint037F anchor cell
BASELINE_CELL = {
    "pool_max_age_sec": 24 * 3600,
    "onset_max_sec": 60 * 60,
    "onset_count_thresh": 10,
}


def _log(msg: str) -> None:
    print(f"[g {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _utc_week_start_ts(iso_week: str) -> float:
    return datetime.strptime(iso_week, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def discover_regimes(q4_base: pathlib.Path | None = None) -> list[str]:
    base = q4_base or Q4_DIR
    paths = sorted(base.glob("q4_candidates_*.csv"))
    reg = [p.stem.replace("q4_candidates_", "") for p in paths]
    return reg


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


def load_regime_csv(reg: str, q4_base: pathlib.Path | None = None) -> list[dict[str, Any]]:
    base = q4_base or Q4_DIR
    path = base / f"q4_candidates_{reg}.csv"
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            row["entry_time_sec"] = int(float(row["entry_time_sec"]))
            row["regime_week"] = reg
            row["pool_address_lower"] = str(row.get("pool_address") or "").lower()
            if row.get("activity_heat") not in (None, ""):
                row["activity_heat"] = int(float(row["activity_heat"]))
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
            rows.append(row)
    return rows


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
        for r in _ch_query(ch_url, sql, ch_user, ch_pwd):
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
        for r in _ch_query(ch_url, sql, ch_user, ch_pwd):
            pool = str(r["pool_address"]).lower()
            ts = int(r["ts_sec"])
            tk = parse_tick_hex(r.get("tick_raw"))
            res.setdefault(pool, []).append((ts, tk))
    for pool in res:
        res[pool].sort(key=lambda x: x[0])
    return res


def count_swaps_between(ts_list: list[int], lo: int, hi_excl: int) -> int:
    if hi_excl <= lo:
        return 0
    a = bisect.bisect_left(ts_list, lo)
    b = bisect.bisect_left(ts_list, hi_excl)
    return max(0, b - a)


def compute_activity_onset(
    ts_list: list[int],
    entry_ts: int,
    pool_first_seen: int,
    count_thresh: int,
) -> int | None:
    if not ts_list:
        return None
    earliest = max(pool_first_seen, ts_list[0])
    end_cap = entry_ts
    t = earliest + ONSET_BUCKET_SEC
    while t <= end_cap:
        c = count_swaps_between(ts_list, t - ONSET_BUCKET_SEC, t)
        if c >= count_thresh:
            return t - ONSET_BUCKET_SEC
        t += ONSET_BUCKET_SEC
    return None


def pool_age_sec(pool: str, ent: int, pool_global_first: dict[str, int], ts_list: list[int]) -> int:
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else ent
    return max(0, ent - gfs)


def split_chrono(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda x: x["entry_time_sec"])
    n = len(rows)
    cut = max(1, int(n * (1 - HOLDOUT_FRAC)))
    return rows[:cut], rows[cut:]


def stats_returns(rets: list[float]) -> dict[str, Any]:
    rs = [x for x in rets if x == x]
    if not rs:
        return {"n": 0, "mean": None, "profit_factor": None}
    rs.sort()
    n = len(rs)
    k = max(1, n // 10)
    tr = rs[k:-k] if n > 2 * k else rs
    pos = [x for x in rs if x > 0]
    neg = [x for x in rs if x < 0]
    gp, gl = sum(pos), abs(sum(neg))
    pf = (gp / gl) if gl > 1e-12 else None
    return {
        "n": n,
        "mean": round(sum(rs) / n, 6),
        "profit_factor": round(pf, 6) if pf is not None else None,
        "median": round(rs[n // 2], 6),
        "trimmed_mean": round(sum(tr) / max(len(tr), 1), 6),
        "label_1_share": round(len(pos) / n, 6),
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
    st = stats_returns([r[f"net_return_{h}"] for r in rows if r.get(f"net_return_{h}") is not None])
    st.update(pool_conc(rows))
    return st


def evaluate_young_pool_onset_combo(
    all_by_regime: dict[str, list[dict]],
    swaps_cache: dict[str, dict[str, list[int]]],
    pool_global_first: dict[str, int],
    regimes: list[str],
    pool_max_age_sec: int,
    onset_max_sec: int,
    onset_count_thresh: int,
    min_holdout_floor: int,
    required_floor_regimes: int,
) -> dict[str, Any]:
    """swaps_cache[reg][pool] -> sorted ts_list only."""

    def passes(r: dict, ts_list: list[int]) -> bool:
        gfs = pool_global_first.get(r["pool_address_lower"])
        if gfs is None:
            gfs = ts_list[0] if ts_list else r["entry_time_sec"]
        age = max(0, int(r["entry_time_sec"]) - gfs)
        if age > pool_max_age_sec:
            return False
        onset = compute_activity_onset(
            ts_list, int(r["entry_time_sec"]), gfs, onset_count_thresh
        )
        if onset is None:
            return False
        toe = int(r["entry_time_sec"]) - onset
        if toe > onset_max_sec:
            return False
        pv = r.get("price_velocity_5m")
        if pv is None or float(pv) <= FLOW_EPS:
            return False
        return True

    ho_all: list[dict] = []
    per_reg: dict[str, dict[str, Any]] = {}

    for reg in regimes:
        rows = all_by_regime.get(reg, [])
        _, ho = split_chrono(rows)
        sc_reg = swaps_cache.get(reg, {})
        gh = [
            r
            for r in ho
            if passes(r, sc_reg.get(r["pool_address_lower"], []))
        ]
        ho_all.extend(gh)
        per_reg[reg] = {h: horizon_agg(gh, h) for h in HORIZONS}

    agg = {h: horizon_agg(ho_all, h) for h in HORIZONS}
    h30 = agg["30m"]

    regimes_floor_ok = []
    for reg in regimes:
        pr = per_reg[reg]["30m"]
        n30 = pr.get("n") or 0
        m30 = pr.get("mean")
        if n30 >= min_holdout_floor and m30 is not None and m30 > 0:
            regimes_floor_ok.append(reg)

    regimes_pos_all = sum(
        1 for reg in regimes if (per_reg.get(reg, {}).get("30m", {}).get("mean") or -1) > 0
    )

    strict_core = (
        (h30.get("mean") or -1) > 0
        and (h30.get("profit_factor") or 0) > 1.0
        and regimes_pos_all >= min(3, len(regimes))
    )

    strict_floor = len(regimes_floor_ok) >= required_floor_regimes

    strict_ok = strict_core and strict_floor

    return {
        "params": {
            "pool_max_age_sec": pool_max_age_sec,
            "onset_max_sec": onset_max_sec,
            "onset_count_thresh": onset_count_thresh,
        },
        "holdout_n_total": len(ho_all),
        "aggregate_holdout": agg,
        "per_regime_holdout": per_reg,
        "regimes_floor_positive_mean_count": len(regimes_floor_ok),
        "regimes_positive_any_mean": regimes_pos_all,
        "strict_ok": strict_ok,
        "strict_core": strict_core,
        "strict_floor": strict_floor,
        "required_floor_regimes": required_floor_regimes,
        "min_holdout_floor": min_holdout_floor,
        "regimes_meeting_floor_positive_list": regimes_floor_ok,
    }


MIN_HOLDOUT_PER_REGIME = int(os.environ.get("SPRINT037G_MIN_HOLDOUT_PER_REGIME", "35"))
STABILITY_FRAC_MIN = float(os.environ.get("SPRINT037G_STABILITY_FRAC", "0.67"))
MIN_REGIMES_FLOOR_OK_DEFAULT = int(os.environ.get("SPRINT037G_MIN_REGIMES_FLOOR_OK", "4"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint037G young_pool_onset_flow expansion grid")
    parser.add_argument(
        "--q4-dir",
        default=None,
        help="Directory containing q4_candidates_*.csv (default: mvp/audits/sprint037c)",
    )
    parser.add_argument(
        "--audit-dir",
        default=None,
        help="Directory for expansion_summary.json, param_stability_grid.csv, per_regime_floor_audit.csv",
    )
    parser.add_argument(
        "--no-run-readouts",
        action="store_true",
        help="Do not write mvp/runs/run-037G-*.md",
    )
    args = parser.parse_args()

    q4_base = pathlib.Path(args.q4_dir) if args.q4_dir else Q4_DIR
    if not q4_base.is_absolute():
        q4_base = REPO / q4_base
    audit_out = pathlib.Path(args.audit_dir) if args.audit_dir else OUT_DIR
    if not audit_out.is_absolute():
        audit_out = REPO / audit_out

    audit_out.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    regimes = discover_regimes(q4_base)
    if not regimes:
        _log(f"ERROR: no q4_candidates_*.csv under {q4_base}")
        sys.exit(2)

    required_floor_regimes = min(len(regimes), max(4, MIN_REGIMES_FLOOR_OK_DEFAULT))

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        sys.exit(2)

    all_by_regime: dict[str, list[dict]] = {r: load_regime_csv(r, q4_base) for r in regimes}
    all_flat = [r for reg in regimes for r in all_by_regime[reg]]
    pools_all = sorted({r["pool_address_lower"] for r in all_flat})

    _log(f"regimes={len(regimes)} candidates={len(all_flat)} pools={len(pools_all)}")
    _log("fetch pool global first-seen …")
    pool_global_first = fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)

    swaps_cache: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for reg in regimes:
        rows = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rows})
        ts_min = min(r["entry_time_sec"] for r in rows) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rows) + 600
        _log(f"  swaps {reg} pools={len(pools)} …")
        raw = fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_cache[reg] = {p: [x[0] for x in raw.get(p, [])] for p in pools}

    grid_rows: list[dict[str, Any]] = []
    stability_hits = 0
    total_grid = 0
    baseline_result: dict[str, Any] | None = None

    for pmax in GRID_POOL_MAX_AGE_SEC:
        for omax in GRID_ONSET_MAX_SEC:
            for othr in GRID_ONSET_COUNT_THRESH:
                total_grid += 1
                res = evaluate_young_pool_onset_combo(
                    all_by_regime,
                    swaps_cache,
                    pool_global_first,
                    regimes,
                    pmax,
                    omax,
                    othr,
                    MIN_HOLDOUT_PER_REGIME,
                    required_floor_regimes,
                )
                if res["strict_ok"]:
                    stability_hits += 1
                pr = res["params"]
                row = {
                    **pr,
                    **res["aggregate_holdout"]["30m"],
                    "holdout_n_total": res["holdout_n_total"],
                    "strict_ok": res["strict_ok"],
                    "strict_core": res["strict_core"],
                    "strict_floor": res["strict_floor"],
                    "regimes_floor_ok_count": res["regimes_floor_positive_mean_count"],
                    "regimes_pos_any": res["regimes_positive_any_mean"],
                }
                grid_rows.append(row)
                if (
                    pr["pool_max_age_sec"] == BASELINE_CELL["pool_max_age_sec"]
                    and pr["onset_max_sec"] == BASELINE_CELL["onset_max_sec"]
                    and pr["onset_count_thresh"] == BASELINE_CELL["onset_count_thresh"]
                ):
                    baseline_result = res

    stab_frac = stability_hits / max(total_grid, 1)

    baseline_strict = bool(baseline_result and baseline_result["strict_ok"])
    supersession_submit = baseline_strict and stab_frac >= STABILITY_FRAC_MIN

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    floor_audit: list[dict[str, Any]] = []
    if baseline_result:
        for reg in regimes:
            pr = baseline_result["per_regime_holdout"][reg]["30m"]
            floor_audit.append(
                {
                    "regime_week": reg,
                    "holdout_n_30m": pr.get("n"),
                    "holdout_mean_30m": pr.get("mean"),
                    "holdout_pf_30m": pr.get("profit_factor"),
                    "meets_floor_n": (pr.get("n") or 0) >= MIN_HOLDOUT_PER_REGIME,
                    "mean_positive": (pr.get("mean") or -1) > 0,
                }
            )

    summary = {
        "kind": "sprint037g_young_pool_onset_expansion",
        "created_at": now,
        "regimes": regimes,
        "n_regimes": len(regimes),
        "n_candidates": len(all_flat),
        "min_holdout_per_regime": MIN_HOLDOUT_PER_REGIME,
        "min_regimes_meeting_floor_positive": required_floor_regimes,
        "grid_size": total_grid,
        "stability_hits_strict_ok": stability_hits,
        "stability_fraction": round(stab_frac, 6),
        "stability_frac_required": STABILITY_FRAC_MIN,
        "baseline_cell": BASELINE_CELL,
        "baseline_strict_ok": baseline_strict,
        "supersession_readout_submitted": supersession_submit,
        "baseline_aggregate_holdout_30m": (
            baseline_result["aggregate_holdout"]["30m"] if baseline_result else None
        ),
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "TRAINING_ALLOWED": False,
            "TRAINING_REVIEW_ALLOWED": supersession_submit,
            "NO_ALPHA_CLAIM": True,
        },
        "notes": [
            "Formal FAILED_NO_7D_ALPHA lift requires human Gate 0 approval per docs/phase6-resolution-criteria.md.",
            "Gate 5 label lineage may remain PARTIAL vs label_dashcam_executable.py until production label pass.",
        ],
    }

    (audit_out / "expansion_summary.json").write_text(json.dumps(summary, indent=2))

    if grid_rows:
        keys = list(grid_rows[0].keys())
        with (audit_out / "param_stability_grid.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(grid_rows)

    if floor_audit:
        with (audit_out / "per_regime_floor_audit.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(floor_audit[0].keys()))
            w.writeheader()
            w.writerows(floor_audit)

    if args.no_run_readouts:
        _log(
            f"done baseline_strict={baseline_strict} stab={stab_frac:.2%} "
            f"supersession_draft={supersession_submit}"
        )
        return

    md_path = RUNS_DIR / "run-037G-young-pool-onset-expansion-readout.md"
    lines = [
        "# Run-037G — young_pool_onset_flow expansion + parameter stability",
        "",
        f"Generated: {now}",
        "",
        f"- Regimes discovered: **{len(regimes)}** (`{', '.join(regimes)}`)",
        f"- Candidates total: **{len(all_flat)}**",
        f"- Grid: **{total_grid}** combos; strict_ok hits: **{stability_hits}** ({stab_frac:.1%})",
        f"- MIN_HOLDOUT_PER_REGIME: **{MIN_HOLDOUT_PER_REGIME}**",
        f"- required regimes meeting floor (positive mean, n≥ floor): **{required_floor_regimes}**",
        "",
        "## Baseline cell (matches Sprint037F anchor)",
        "",
        json.dumps(BASELINE_CELL, indent=2),
        "",
        f"- baseline strict_ok: **{baseline_strict}**",
        "",
        "## Supersession draft submission",
        "",
        f"`supersession_readout_submitted` = **{supersession_submit}** "
        f"(requires baseline strict_ok AND stability ≥ {STABILITY_FRAC_MIN:.0%})",
        "",
        "`NO_ALPHA_CLAIM=true`",
    ]
    md_path.write_text("\n".join(lines))

    super_path = RUNS_DIR / "run-037G-young-pool-onset-supersession-readout.md"
    if supersession_submit and baseline_result:
        h30 = baseline_result["aggregate_holdout"]["30m"]
        lines_s = [
            "# Run-037G — Gate 0 supersession candidate (young_pool_onset_flow)",
            "",
            f"Generated: {now}",
            "",
            "```",
            "supersedes: FAILED_NO_7D_ALPHA",
            "```",
            "",
            "**Scope:** Sprint037G expansion validation — pool-level lifecycle gate "
            "`young_pool_onset_flow` with predeclared parameter neighborhood stability.",
            "",
            "## Preconditions (automated checks passed)",
            "",
            f"- Regimes in evidence: **{len(regimes)}**",
            f"- Baseline cell strict_ok: **{baseline_strict}**",
            f"- Parameter grid stability (strict_ok fraction): **{stab_frac:.2%}** (≥ {STABILITY_FRAC_MIN:.0%})",
            "",
            "## Holdout economics — baseline cell aggregate (30m)",
            "",
            f"- n: **{h30.get('n')}**",
            f"- mean net return: **{h30.get('mean')}**",
            f"- profit factor: **{h30.get('profit_factor')}**",
            f"- label_1_share: **{h30.get('label_1_share')}**",
            "",
            "## Caveats",
            "",
            "- Human review required per `docs/phase6-resolution-criteria.md` and `docs/ml-training-gate.md`.",
            "- Labels originate from `scripts/sprint037c_multi_horizon.py` research replay; Gate 1 "
            "production labeler parity may still be required.",
            "- This artifact alone does not authorize training or live deployment.",
            "",
            "---",
            "",
            "*Machine-generated draft — requires reviewer sign-off.*",
        ]
        super_path.write_text("\n".join(lines_s))
    else:
        lines_w = [
            "# Run-037G — Gate 0 supersession WITHHELD",
            "",
            f"Generated: {now}",
            "",
            "Formal `supersedes: FAILED_NO_7D_ALPHA` **not** issued automatically.",
            "",
            f"- baseline_strict_ok: **{baseline_strict}**",
            f"- stability_fraction: **{stab_frac:.2%}** (required ≥ {STABILITY_FRAC_MIN:.0%})",
            "",
            "See `mvp/audits/sprint037g/expansion_summary.json` and `param_stability_grid.csv`.",
        ]
        super_path.write_text("\n".join(lines_w))

    _log(
        f"done baseline_strict={baseline_strict} stab={stab_frac:.2%} "
        f"supersession_draft={supersession_submit} audit_dir={audit_out}"
    )


if __name__ == "__main__":
    main()
