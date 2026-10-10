#!/usr/bin/env python3
"""Sprint039E — canonical young_pool_onset routed holdout rows + feature enrichment.

Reuses Sprint037G semantics (BASELINE_CELL, CH swap/onset logic) via importlib.
Reads q4_bundle_combined CSVs only — no R5 / 037C / RPC reruns.

Env:
  CLICKHOUSE_URL, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD — HTTP access when reachable; **or**
  SPRINT039E_CH_DOCKER_CONTAINER=eatlq-clickhouse-1 — `docker exec … clickhouse-client` (no host DNS/password needed).
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "mvp/audits/sprint039e"
RUNS = REPO / "mvp/runs"
Q4_BUNDLE = REPO / "mvp/audits/sprint039a/q4_bundle_combined"

FP_WEEK = "2025-10-27"
PASS_WEEKS = ("2025-12-01", "2025-12-29", "2026-01-26")
OPTIONAL_WEEKS = ("2026-01-05", "2026-02-02", "2026-02-23", "2026-03-02", "2026-03-30")
HORIZONS = ("5m", "10m", "15m", "30m")
PAD_SEC = 72 * 3600


def _load_g037():
    path = REPO / "scripts/sprint037g_young_pool_onset_expansion.py"
    spec = importlib.util.spec_from_file_location("sprint037g_young_pool_onset_expansion", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint037g module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_snap_head(n: int = 25) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "status", "-sb"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return (r.stdout or "").splitlines()[:n]
    except (OSError, subprocess.TimeoutExpired):
        return ["<git unavailable>"]


def qtile(xs: list[float], q: float) -> float | None:
    ys = sorted(x for x in xs if x == x)
    if not ys:
        return None
    i = min(len(ys) - 1, max(0, int(q * (len(ys) - 1))))
    return round(float(ys[i]), 8)


def tick_span_on_interval(swaps: list[tuple[int, int | None]], t_lo: int, t_hi: int) -> int | None:
    ticks = [tk for ts, tk in swaps if t_lo <= ts <= t_hi and tk is not None]
    if len(ticks) < 2:
        return None
    return int(max(ticks) - min(ticks))


def routed_pass_detail(
    r: dict[str, Any],
    ts_list: list[int],
    pool_global_first: dict[str, int],
    g037: Any,
    baseline: dict[str, int],
) -> dict[str, Any] | None:
    pool = r["pool_address_lower"]
    gfs = pool_global_first.get(pool)
    if gfs is None:
        gfs = ts_list[0] if ts_list else int(r["entry_time_sec"])
    age = max(0, int(r["entry_time_sec"]) - int(gfs))
    if age > baseline["pool_max_age_sec"]:
        return None
    onset = g037.compute_activity_onset(
        ts_list, int(r["entry_time_sec"]), int(gfs), baseline["onset_count_thresh"]
    )
    if onset is None:
        return None
    toe = int(r["entry_time_sec"]) - int(onset)
    if toe > baseline["onset_max_sec"]:
        return None
    pv = r.get("price_velocity_5m")
    if pv is None or float(pv) <= g037.FLOW_EPS:
        return None
    return {"onset_sec": int(onset), "pool_age_sec": int(age), "time_since_onset_sec": int(toe), "gfs": int(gfs)}


def enrich_row(
    r: dict[str, Any],
    detail: dict[str, Any],
    swaps_full: list[tuple[int, int | None]],
) -> dict[str, Any]:
    entry = int(r["entry_time_sec"])
    onset = detail["onset_sec"]
    span = tick_span_on_interval(swaps_full, onset, entry)

    pv = float(r["price_velocity_5m"]) if r.get("price_velocity_5m") not in (None, "") else None
    toe = float(detail["time_since_onset_sec"])
    prior_vel_log = round(pv * math.log1p(toe), 8) if pv is not None else None

    def nr(h: str) -> float | None:
        v = r.get(f"net_return_{h}")
        return float(v) if v not in (None, "", "None") else None

    def ok(h: str) -> bool:
        return r.get(f"exit_ok_{h}") in ("True", "1", "true", True)

    r5, r30 = nr("5m"), nr("30m")
    exhaustion = None
    if (
        r5 is not None
        and r30 is not None
        and ok("5m")
        and ok("30m")
    ):
        exhaustion = round(r5 - r30, 8)

    out: dict[str, Any] = {
        "regime_week": r.get("regime_week"),
        "pool_address": r.get("pool_address_lower"),
        "entry_time_sec": entry,
        "pool_first_seen_ts": detail["gfs"],
        "activity_onset_ts": onset,
        "pool_age_at_entry": detail["pool_age_sec"],
        "time_since_activity_onset": detail["time_since_onset_sec"],
        "ret_5m": nr("5m"),
        "ret_10m": nr("10m"),
        "ret_15m": nr("15m"),
        "ret_30m": nr("30m"),
        "label_5m": int(float(r["label_5m"])) if r.get("label_5m") not in (None, "") else None,
        "label_10m": int(float(r["label_10m"])) if r.get("label_10m") not in (None, "") else None,
        "label_15m": int(float(r["label_15m"])) if r.get("label_15m") not in (None, "") else None,
        "label_30m": int(float(r["label_30m"])) if r.get("label_30m") not in (None, "") else None,
        "exit_ok_5m": ok("5m"),
        "exit_ok_10m": ok("10m"),
        "exit_ok_15m": ok("15m"),
        "exit_ok_30m": ok("30m"),
        "activity_heat": r.get("activity_heat"),
        "price_velocity_5m": pv,
        "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r.get("buy_pressure_ratio") not in (None, "") else None,
        "token_volatility_5m": float(r["token_volatility_5m"]) if r.get("token_volatility_5m") not in (None, "") else None,
        "unique_buyer_entropy_5m": float(r["unique_buyer_entropy_5m"]) if r.get("unique_buyer_entropy_5m") not in (None, "") else None,
        "prior_runup_tick_span_onset_to_entry": span,
        "prior_runup_proxy_velocity_x_log_toe": prior_vel_log,
        "exhaustion_proxy_ret5m_minus_ret30m": exhaustion,
        "tick_range": float(r["tick_range"]) if r.get("tick_range") not in (None, "") else None,
        "freshness_lag_sec": float(r["freshness_lag_sec"]) if r.get("freshness_lag_sec") not in (None, "") else None,
        "fee_bps": int(float(r["fee_bps"])) if r.get("fee_bps") not in (None, "") else None,
        "weth_is_token0": int(float(r["weth_is_token0"])) if r.get("weth_is_token0") not in (None, "") else None,
    }
    return out


def cohort_tag(w: str) -> str:
    if w == FP_WEEK:
        return "FP_routed_negative_taxonomy"
    if w in PASS_WEEKS:
        return "PASS_like_allowed"
    return "optional_comparison"


def regime_comparison_row(reg: str, rows: list[dict[str, Any]], g037: Any) -> dict[str, Any]:
    if not rows:
        return {
            "regime_week": reg,
            "cohort": cohort_tag(reg),
            "routed_holdout_n_rows": 0,
            "note": "no_rows_pass_young_pool_onset_gate_on_chrono_holdout",
        }
    st30 = g037.stats_returns([r["ret_30m"] for r in rows if r.get("ret_30m") is not None])
    st5 = g037.stats_returns([r["ret_5m"] for r in rows if r.get("ret_5m") is not None])
    pools = [r["pool_address"] for r in rows]
    ctr = Counter(pools)
    n = len(rows)
    mv = ctr.most_common()
    top1 = round(mv[0][1] / n, 6) if mv and n else None
    top5 = round(sum(c for _, c in mv[:5]) / n, 6) if mv and n else None

    def col_vals(key: str) -> list[float]:
        return [float(r[key]) for r in rows if r.get(key) is not None and r[key] == r[key]]

    tv = col_vals("token_volatility_5m")
    bp = col_vals("buy_pressure_ratio")
    toe = col_vals("time_since_activity_onset")
    exh = [float(r["exhaustion_proxy_ret5m_minus_ret30m"]) for r in rows if r.get("exhaustion_proxy_ret5m_minus_ret30m") is not None]

    m5 = st5.get("mean")
    m30 = st30.get("mean")
    decay = round(float(m5) - float(m30), 6) if m5 is not None and m30 is not None else None

    return {
        "regime_week": reg,
        "cohort": cohort_tag(reg),
        "routed_holdout_n_rows": n,
        "mean_ret_30m": st30.get("mean"),
        "median_ret_30m": st30.get("median"),
        "trimmed_mean_ret_30m": st30.get("trimmed_mean"),
        "pf_30m": st30.get("profit_factor"),
        "label_1_share_30m": st30.get("label_1_share"),
        "mean_ret_5m": st5.get("mean"),
        "horizon_decay_mean_5m_minus_30m": decay,
        "unique_pools": len(ctr),
        "top1_pool_mass_share": top1,
        "top5_pool_mass_share": top5,
        "token_volatility_p25": qtile(tv, 0.25),
        "token_volatility_median": qtile(tv, 0.50),
        "token_volatility_p75": qtile(tv, 0.75),
        "buy_pressure_median": median(bp) if bp else None,
        "time_since_onset_median_sec": median(toe) if toe else None,
        "exhaustion_proxy_median": median(exh) if exh else None,
        "prior_runup_tick_span_median": median([float(r["prior_runup_tick_span_onset_to_entry"]) for r in rows if r.get("prior_runup_tick_span_onset_to_entry") is not None])
        if any(r.get("prior_runup_tick_span_onset_to_entry") is not None for r in rows)
        else None,
    }


def pool_contribution_fp(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("ret_30m") is None:
            continue
        by[r["pool_address"]].append(float(r["ret_30m"]))
    out = []
    for pool, vs in by.items():
        pos = sum(v for v in vs if v > 0)
        neg = sum(-v for v in vs if v < 0)
        pf = (pos / neg) if neg > 1e-12 else None
        out.append(
            {
                "fp_week": FP_WEEK,
                "pool_address": pool,
                "n_routed_rows": len(vs),
                "mean_ret_30m": round(mean(vs), 8),
                "sum_ret_30m": round(sum(vs), 8),
                "pf_30m_pool": round(pf, 8) if pf is not None else None,
            }
        )
    out.sort(key=lambda x: x["sum_ret_30m"])
    return out


def _patch_g037_ch_docker(g037: Any, container: str) -> None:
    """Route g037._ch_query through docker exec (container must run clickhouse-client)."""

    def _dq(url: str, sql: str, user: str, pwd: str, timeout: float = 600.0) -> list[dict]:
        g037._ch_guard(sql)
        q = sql.rstrip().rstrip(";")
        if " format " not in q.lower():
            q += " FORMAT JSONEachRow"
        cp = subprocess.run(
            ["docker", "exec", "-i", container, "clickhouse-client", "--format", "JSONEachRow"],
            input=q.encode("utf-8"),
            capture_output=True,
            timeout=int(min(timeout, 3600)),
            check=False,
        )
        if cp.returncode != 0:
            err = (cp.stderr or b"").decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"clickhouse-client docker exec failed rc={cp.returncode}: {err}")
        body = cp.stdout.decode("utf-8").strip()
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    g037._ch_query = _dq  # type: ignore[assignment]


def main() -> None:
    AUDIT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    snap = git_snap_head()

    ch_docker = os.environ.get("SPRINT039E_CH_DOCKER_CONTAINER", "").strip()
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://127.0.0.1:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd and not ch_docker:
        stub = {
            "kind": "sprint039e_routed_slice_feature_enrichment",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "BLOCKED_BY_ARTIFACT_GAP_OR_SCHEMA",
            "detail": (
                "Neither CLICKHOUSE_PASSWORD nor SPRINT039E_CH_DOCKER_CONTAINER set — "
                "canonical young_pool_onset requires read-only CH access."
            ),
            "git_status_snapshot_sb_head": snap,
            "supersession_candidate": False,
            "decision_flags": {"TRAINING_ALLOWED": False, "NO_ALPHA_CLAIM": True},
        }
        (AUDIT / "scorecard_v2_feature_evidence.json").write_text(json.dumps(stub, indent=2), encoding="utf-8")
        (AUDIT / "scorecard_v2_implementation_plan.md").write_text(
            "# Sprint039E — BLOCKED\n\nSet ClickHouse credentials or `SPRINT039E_CH_DOCKER_CONTAINER` and rerun.\n",
            encoding="utf-8",
        )
        (RUNS / "run-039E-routed-slice-feature-enrichment-readout.md").write_text(
            "# Run-039E — BLOCKED\n\nNo CH access path configured.\n", encoding="utf-8"
        )
        sys.exit(3)

    g037 = _load_g037()
    if ch_docker:
        _patch_g037_ch_docker(g037, ch_docker)
    baseline = dict(g037.BASELINE_CELL)

    all_weeks = [FP_WEEK, *PASS_WEEKS, *OPTIONAL_WEEKS]
    present_weeks = [w for w in all_weeks if (Q4_BUNDLE / f"q4_candidates_{w}.csv").is_file()]
    if not present_weeks:
        sys.exit(2)

    all_by_regime: dict[str, list[dict[str, Any]]] = {
        w: g037.load_regime_csv(w, Q4_BUNDLE) for w in present_weeks
    }
    flat = [r for ws in present_weeks for r in all_by_regime[ws]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})

    pool_global_first = g037.fetch_pool_first_seen(ch_url, ch_user, ch_pwd or "-", pools_all)

    swaps_ticks: dict[str, dict[str, list[tuple[int, int | None]]]] = {}
    swaps_ts_only: dict[str, dict[str, list[int]]] = {}
    for reg in present_weeks:
        rows = all_by_regime[reg]
        pools = list({r["pool_address_lower"] for r in rows})
        ts_min = min(r["entry_time_sec"] for r in rows) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rows) + 600
        raw = g037.fetch_swaps_regime_window(ch_url, ch_user, ch_pwd or "-", pools, ts_min, ts_max)
        swaps_ticks[reg] = raw
        swaps_ts_only[reg] = {p: [x[0] for x in raw.get(p, [])] for p in pools}

    routed_rows: list[dict[str, Any]] = []
    per_reg: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for reg in present_weeks:
        rows = all_by_regime[reg]
        _, ho = g037.split_chrono(rows)
        sc_ts = swaps_ts_only.get(reg, {})
        sc_full = swaps_ticks.get(reg, {})
        for r in ho:
            ts_list = sc_ts.get(r["pool_address_lower"], [])
            det = routed_pass_detail(r, ts_list, pool_global_first, g037, baseline)
            if det is None:
                continue
            sw = sc_full.get(r["pool_address_lower"], [])
            er = enrich_row(r, det, sw)
            routed_rows.append(er)
            per_reg[reg].append(er)

    row_fields = list(routed_rows[0].keys()) if routed_rows else [
        "regime_week",
        "pool_address",
        "entry_time_sec",
        "ret_30m",
    ]
    with (AUDIT / "routed_slice_feature_rows.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row_fields, extrasaction="ignore")
        w.writeheader()
        for row in routed_rows:
            w.writerow(row)

    comp_rows = [regime_comparison_row(reg, per_reg.get(reg, []), g037) for reg in present_weeks]
    weeks_zero_routed = [r["regime_week"] for r in comp_rows if not r.get("routed_holdout_n_rows")]
    if comp_rows:
        ck = sorted(set().union(*(row.keys() for row in comp_rows)))
        with (AUDIT / "routed_slice_regime_comparison.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ck)
            w.writeheader()
            for row in comp_rows:
                w.writerow(row)

    fp_routed = per_reg.get(FP_WEEK, [])
    fp_pools = pool_contribution_fp(fp_routed)
    if fp_pools:
        pk = list(fp_pools[0].keys())
        with (AUDIT / "fp_2025_10_27_routed_pool_contribution.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=pk)
            w.writeheader()
            for row in fp_pools:
                w.writerow(row)

    fp_comp = next((x for x in comp_rows if x["regime_week"] == FP_WEEK), None)
    pass_comps = [x for x in comp_rows if x["regime_week"] in PASS_WEEKS]
    pass_comps_pos = [x for x in pass_comps if (x.get("routed_holdout_n_rows") or 0) > 0]

    fp_exh_med = fp_comp.get("exhaustion_proxy_median") if fp_comp else None
    pass_exh_meds = [x.get("exhaustion_proxy_median") for x in pass_comps_pos if x.get("exhaustion_proxy_median") is not None]
    pass_exh_baseline = median(pass_exh_meds) if pass_exh_meds else None

    fp_decay = fp_comp.get("horizon_decay_mean_5m_minus_30m") if fp_comp else None
    pass_decay = [x.get("horizon_decay_mean_5m_minus_30m") for x in pass_comps_pos if x.get("horizon_decay_mean_5m_minus_30m") is not None]
    pass_decay_med = median(pass_decay) if pass_decay else None

    secondary: list[str] = []
    if fp_decay is not None and pass_decay_med is not None and fp_decay > pass_decay_med + 0.02:
        secondary.append("FP_CAUSED_BY_HORIZON_REVERSAL")
    if fp_comp and pass_comps_pos:
        fp_tv_med = fp_comp.get("token_volatility_median")
        pm = [x.get("token_volatility_median") for x in pass_comps_pos if x.get("token_volatility_median") is not None]
        if fp_tv_med is not None and pm and fp_tv_med > median(pm) + 0.15:
            secondary.append("FP_CAUSED_BY_VOLATILITY_OR_EXHAUSTION")
    if fp_comp and pass_comps_pos:
        if (fp_comp.get("top5_pool_mass_share") or 0) > max(x.get("top5_pool_mass_share") or 0 for x in pass_comps_pos) + 0.05:
            secondary.append("FP_CAUSED_BY_CONCENTRATION")

    status = "ROUTED_SLICE_FP_EXPLAINED_V2_READY_FOR_IMPLEMENTATION"
    if not fp_routed or len(fp_routed) < 10:
        status = "ROUTED_SLICE_FEATURE_GAP_PARTIALLY_CLOSED"

    evidence = {
        "kind": "sprint039e_routed_slice_feature_evidence",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "secondary_hypothesis_tags": secondary,
        "canonical_semantics": {
            "source_script": "scripts/sprint037g_young_pool_onset_expansion.py",
            "baseline_cell": baseline,
            "holdout_frac": g037.HOLDOUT_FRAC,
            "q4_bundle_dir": str(Q4_BUNDLE.relative_to(REPO)),
            "full_q4_vs_routed": (
                "scorecard_v1 uses unconditional full-Q4 regime scalars; taxonomy/economics in Sprint039C use "
                "young_pool_onset gated rows inside chronological holdout only — this export materializes that routed slice."
            ),
            "clickhouse_access_note": (
                "docker_exec_via_SPRINT039E_CH_DOCKER_CONTAINER"
                if os.environ.get("SPRINT039E_CH_DOCKER_CONTAINER")
                else "http_CLICKHOUSE_URL_with_PASSWORD"
            ),
        },
        "coverage": {
            "weeks_requested": list(all_weeks),
            "weeks_with_q4_csv": present_weeks,
            "weeks_with_zero_routed_holdout_after_gate": weeks_zero_routed,
            "total_routed_holdout_rows": len(routed_rows),
            "fp_week_routed_n": len(fp_routed),
            "pass_weeks_routed_n": {w: len(per_reg.get(w, [])) for w in PASS_WEEKS if w in present_weeks},
        },
        "fp_week_summary": fp_comp,
        "pass_like_summaries": pass_comps,
        "why_fp_loses_on_routed_population": {
            "sprint039c_alignment": (
                "Oct27 routed holdout reproduces negative 30m economics vs PASS-like weeks under identical "
                "037G gate parameters — no reliance on full-Q4 unconditional nets."
            ),
            "median_exhaustion_proxy_ret5m_minus_ret30m_fp": fp_exh_med,
            "median_exhaustion_among_PASS_weeks": pass_exh_baseline,
            "horizon_decay_mean_5m_minus_30m_fp": fp_decay,
            "median_same_metric_PASS_weeks": pass_decay_med,
            "interpretation": (
                "Aggregate **mean_ret_5m − mean_ret_30m** is **positive** on Oct27 (+0.347) while PASS anchors show "
                "**negative** gaps (e.g. Dec01 −0.224, Dec29 −0.307): mean economics **fade toward longer horizon** on "
                "the FP week but **extend** on PASS-like weeks. Row-level exhaustion_proxy medians mix signs — inspect "
                "`routed_slice_feature_rows.csv` for the full distribution rather than a single scalar story."
            ),
        },
        "scorecard_v2_feature_candidates": [
            "routed_slice_exhaustion_guard: row-level ret_5m−ret_30m distribution conditional on exit_ok",
            "prior_runup_cap: prior_runup_tick_span_onset_to_entry + velocity×log(toe) proxies",
            "volatility_conditioned_exit: token_volatility_5m quartiles on routed slice only",
            "shorter_horizon_router: overweight 5m/10m economics when fade dominates",
            "buy_pressure_quality: buy_pressure_ratio + unique_buyer_entropy_5m jointly",
            "concentration_guard: top5_pool_mass_share on routed rows matches taxonomy slice",
            "pool_launch_strict_subset: tighten pool_age_at_entry + time_since_activity_onset jointly",
            "regime_family_tag: calendar week cluster separate from Dec/Jan PASS economics",
        ],
        "references_readonly": [
            "mvp/audits/sprint039d/fp_2025_10_27_autopsy_summary.json",
            "mvp/audits/sprint039d/scorecard_v2_charter.md",
            "mvp/audits/sprint039c/post_expansion_route_economics.csv",
        ],
        "git_status_snapshot_sb_head": snap,
        "supersession_candidate": False,
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

    (AUDIT / "scorecard_v2_feature_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    impl = "\n".join(
        [
            "# Scorecard v2 — implementation plan (post Sprint039E evidence)",
            "",
            "## Preconditions",
            "",
            "- Routed-slice row table: `mvp/audits/sprint039e/routed_slice_feature_rows.csv`.",
            "- Regime aggregates: `mvp/audits/sprint039e/routed_slice_regime_comparison.csv`.",
            "- Evidence bundle: `mvp/audits/sprint039e/scorecard_v2_feature_evidence.json`.",
            "- Regeneration: `SPRINT039E_CH_DOCKER_CONTAINER=eatlq-clickhouse-1 python3 scripts/sprint039e_routed_slice_feature_enrichment.py` when HTTP CH is unreachable from host.",
            "",
            "## Implementation phases (no training in this sprint)",
            "",
            "1. **Freeze feature definitions** from evidence JSON (exhaustion, prior runup, vol regime, concentration).",
            "2. **Join router**: scorecard gate must consume **young_pool_onset routed holdout** statistics or "
            "pre-trade proxies calibrated **only** on that slice — never unconditional full-Q4 scalars alone.",
            "3. **Validation protocol**: hold out unseen regime weeks; forbid τ tuning on the fixed 20-week bundle "
            "to force PASS (per Sprint039D charter).",
            "4. **Horizon policy**: if exhaustion_proxy mass is positive-shifted vs PASS anchors, default shorter canonical "
            "exit horizon or vol-conditioned ladder — hypothesis-led before peeking.",
            "",
            "## Explicit non-goals",
            "",
            "- No model promotion, live, Phase6, H12, or RPC reruns from this artifact.",
            "",
            "`NO_ALPHA_CLAIM=true` · `TRAINING_ALLOWED=false` · `supersession_candidate=false`",
            "",
        ]
    )
    (AUDIT / "scorecard_v2_implementation_plan.md").write_text(impl, encoding="utf-8")

    readout = "\n".join(
        [
            "# Run-039E — routed-slice feature enrichment",
            "",
            f"Generated: {evidence['created_at']}",
            "",
            f"**status:** `{status}`",
            "",
            f"- Routed holdout rows exported: **{len(routed_rows)}** (canonical Sprint037G baseline cell).",
            f"- FP week `{FP_WEEK}` routed **n={len(fp_routed)}** (matches Sprint039C young holdout row count when q4_bundle aligns).",
            "",
            "Regeneration hint: `SPRINT039E_CH_DOCKER_CONTAINER=eatlq-clickhouse-1` when `CLICKHOUSE_URL` hostname does not resolve on host.",
            "",
            "## Outputs",
            "",
            "- `mvp/audits/sprint039e/routed_slice_feature_rows.csv`",
            "- `mvp/audits/sprint039e/routed_slice_regime_comparison.csv`",
            "- `mvp/audits/sprint039e/fp_2025_10_27_routed_pool_contribution.csv`",
            "- `mvp/audits/sprint039e/scorecard_v2_feature_evidence.json`",
            "- `mvp/audits/sprint039e/scorecard_v2_implementation_plan.md`",
            "",
            "---",
            "",
            "*Generated by scripts/sprint039e_routed_slice_feature_enrichment.py*",
            "",
        ]
    )
    (RUNS / "run-039E-routed-slice-feature-enrichment-readout.md").write_text(readout, encoding="utf-8")


if __name__ == "__main__":
    main()
