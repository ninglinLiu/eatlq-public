#!/usr/bin/env python3
"""Sprint038C — regime-family routing scorecard for young_pool_onset_flow.

Route economics must use Sprint037G canonical replay + Sprint038D parity; inline CH replay here is diagnostic-only.

Reads Sprint038A/B artifacts + isolated Q4 CSVs; optional ClickHouse for swap replay.
No ML training, no label RPC, no baseline sprint037c writes.

Outputs scorecard rule, LOOCV diagnostics, universal vs routed holdout simulation.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

SPRINT038A = REPO / "mvp/audits/sprint038a"
SPRINT038B = REPO / "mvp/audits/sprint038b"
OUT = REPO / "mvp/audits/sprint038c"
RUNS = REPO / "mvp/runs"

Q4_DIR = SPRINT038A / "q4_multi_horizon"
ATTR_CSV = SPRINT038B / "regime_failure_attribution.csv"
FEAT_CSV_REAL = SPRINT038B / "feature_distribution_by_regime.csv"
R5_SUMMARY = SPRINT038A / "r5_candidate_expansion_summary.json"
G037_SUMMARY = SPRINT038A / "g037" / "expansion_summary.json"

REGIME_SEC = 7 * 86400
YOUNG_FRAC_MARGIN = 0.028  # below min PASS young_frac to reject THIN weeks without peeking outcomes

CHAIN_ID = 8453
HOLDOUT_FRAC = 0.30
FLOW_EPS = 1e-12


def _ch_http_get_body(
    ch_url: str,
    sql: str,
    ch_user: str,
    ch_password: str,
    timeout_sec: float = 420.0,
) -> str | None:
    if not ch_url:
        return None
    params = urlencode({"query": sql})
    url = f"{ch_url.rstrip('/')}/?{params}"
    try:
        req = Request(url, method="GET")
        if ch_user:
            token = base64.b64encode(f"{ch_user}:{ch_password}".encode()).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        with urlopen(req, timeout=timeout_sec) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, TimeoutError, ValueError):
        return None


def discover_q4_regimes(q4_dir: Path) -> list[str]:
    names: list[str] = []
    for p in sorted(q4_dir.glob("q4_candidates_*.csv")):
        rw = p.stem.replace("q4_candidates_", "")
        names.append(rw)
    return names


def load_q4_regime_csv(regime_week: str, q4_dir: Path) -> list[dict[str, Any]]:
    path = q4_dir / f"q4_candidates_{regime_week}.csv"
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pool = (row.get("pool_address") or "").strip().lower()
            row["pool_address_lower"] = pool
            row["entry_time_sec"] = int(float(row["entry_time_sec"]))
            rows.append(row)
    return rows


def fetch_pool_first_seen(ch_url: str, ch_user: str, ch_pwd: str, pools: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    chunk_size = 400
    for i in range(0, len(pools), chunk_size):
        chunk = pools[i : i + chunk_size]
        plist = "', '".join(chunk)
        sql = f"""
SELECT lower(pool_address) AS pa,
  min(toUInt32(toUnixTimestamp(coalesce(block_time, received_at_ms)))) AS first_ts
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND lower(pool_address) IN ('{plist}')
GROUP BY pa
FORMAT TabSeparated
""".strip()
        body = _ch_http_get_body(ch_url, sql, ch_user, ch_pwd)
        if body is None:
            continue
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            pa = parts[0].strip().lower()
            try:
                fts = int(parts[1])
            except ValueError:
                continue
            out[pa] = fts
    return out


def fetch_swaps_regime_window(
    ch_url: str,
    ch_user: str,
    ch_pwd: str,
    pools: list[str],
    ts_min: int,
    ts_max: int,
) -> dict[str, list[int]]:
    """Pool address -> sorted swap timestamps (sec) in [ts_min, ts_max]."""
    out: dict[str, list[int]] = {}
    chunk_size = 120
    for i in range(0, len(pools), chunk_size):
        chunk = pools[i : i + chunk_size]
        plist = "', '".join(chunk)
        sql = f"""
SELECT lower(pool_address) AS pa,
  toUInt32(toUnixTimestamp(coalesce(block_time, received_at_ms))) AS ts_sec
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND lower(pool_address) IN ('{plist}')
  AND toUnixTimestamp(coalesce(block_time, received_at_ms)) >= {int(ts_min)}
  AND toUnixTimestamp(coalesce(block_time, received_at_ms)) <= {int(ts_max)}
ORDER BY pa, ts_sec
FORMAT TabSeparated
""".strip()
        body = _ch_http_get_body(ch_url, sql, ch_user, ch_pwd, timeout_sec=720.0)
        if body is None:
            continue
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            pa = parts[0].strip().lower()
            try:
                ts_sec = int(parts[1])
            except ValueError:
                continue
            out.setdefault(pa, []).append(ts_sec)
    for pa in out:
        out[pa].sort()
    return out


def split_chrono_rows(
    rows: list[dict[str, Any]],
    holdout_frac: float = HOLDOUT_FRAC,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows_sorted = sorted(rows, key=lambda r: int(r["entry_time_sec"]))
    n = len(rows_sorted)
    k = max(0, min(n, int(n * (1.0 - holdout_frac))))
    return rows_sorted[:k], rows_sorted[k:]


def compute_activity_onset(ts_list: list[int], entry_ts: int, gfs: int, thresh: int) -> int | None:
    """Earliest time T in [gfs, entry_ts] with ≥thresh swaps in [T, entry_ts]."""
    xs = sorted(t for t in ts_list if gfs <= int(t) <= entry_ts)
    if len(xs) < thresh:
        return None
    return xs[len(xs) - thresh]


def stats_returns(vals: list[Any]) -> dict[str, Any]:
    xs: list[float] = []
    for v in vals:
        if v is None or v == "":
            continue
        try:
            xs.append(float(v))
        except (TypeError, ValueError):
            continue
    if not xs:
        return {"mean": None, "median": None, "profit_factor": None, "label_1_share": None}
    n = len(xs)
    mean = sum(xs) / n
    sx = sorted(xs)
    mid = n // 2
    median = sx[mid] if n % 2 else 0.5 * (sx[mid - 1] + sx[mid])
    gains = sum(x for x in xs if x > 0)
    losses = sum(-x for x in xs if x < 0)
    if losses > 0:
        pf: float | None = gains / losses
    elif gains > 0:
        pf = None
    else:
        pf = None
    label_1_share = sum(1 for x in xs if x > 0) / n
    return {"mean": mean, "median": median, "profit_factor": pf, "label_1_share": label_1_share}


def pool_conc_rows(slice_rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(str(r.get("pool_address_lower") or "") for r in slice_rows)
    if not c:
        return {"top1": None, "top5": None, "unique_pools": 0}
    n = sum(c.values())
    tops = c.most_common()
    top1 = tops[0][1] / n
    top5 = sum(x for _, x in tops[:5]) / n
    return {"top1": round(top1, 6), "top5": round(top5, 6), "unique_pools": len(c)}


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[038c {ts}] {msg}", flush=True)


def load_attribution_rows() -> list[dict[str, Any]]:
    with ATTR_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pivot_q4_all_features() -> dict[str, dict[str, dict[str, Any]]]:
    """regime_week -> variable -> {mean, p50, ...} for slice q4_all."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    with FEAT_CSV_REAL.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slice") != "q4_all":
                continue
            if row.get("variable") in ("fee_bps_top3", "weth_is_token0_counts"):
                continue
            rw = row["regime_week"]
            var = row["variable"]
            def _f(k: str) -> Any:
                v = row.get(k)
                if v is None or v == "":
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            out.setdefault(rw, {})[var] = {
                "n": _f("n"),
                "mean": _f("mean"),
                "p10": _f("p10"),
                "p25": _f("p25"),
                "p50": _f("p50"),
                "p75": _f("p75"),
                "p90": _f("p90"),
            }
    return out


def tax_neg_like(rows_attr: list[dict[str, Any]], w: str) -> bool:
    return next((x["regime_bucket"] for x in rows_attr if x["regime_week"] == w), "") in (
        "NEGATIVE",
        "EMPTY_LOW_INTERSECTION",
    )


def frozen_route_allow(
    young_frac: float,
    ah_p50: float | None,
    tau_young: float,
    tau_ah: float,
) -> bool:
    if ah_p50 is None:
        return False
    return young_frac >= tau_young and ah_p50 >= tau_ah


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        sys.exit(2)

    rows_attr = load_attribution_rows()
    regimes = sorted({r["regime_week"] for r in rows_attr})
    feat_q4 = pivot_q4_all_features()

    pass_weeks = [r["regime_week"] for r in rows_attr if r["regime_bucket"] == "PASS_LIKE"]

    yf_pass = [float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == w)) for w in pass_weeks]
    ah_pass = [feat_q4[w]["activity_heat"]["p50"] for w in pass_weeks if w in feat_q4 and "activity_heat" in feat_q4[w]]
    tau_young_global = min(yf_pass) - YOUNG_FRAC_MARGIN
    tau_ah_global = min(ah_pass) if ah_pass else 95.0

    proposed_rule = {
        "name": "young_pool_route_scorecard_v1",
        "expr": f"(young_frac_of_q4 >= {tau_young_global:.6f}) AND (activity_heat_p50_q4_all >= {tau_ah_global:.6f})",
        "tau_young_frac": tau_young_global,
        "tau_activity_heat_p50_q4_all": tau_ah_global,
        "rationale": (
            "Structural separation: Feb/Mar failure weeks show low Q4 activity_heat median and/or low "
            "young_pool share vs Dec/Jan PASS_LIKE cluster; Mar-30 NEGATIVE (high young_frac) is excluded "
            "by activity_heat median gate."
        ),
        "derived_from_pass_weeks_only": pass_weeks,
        "evidence_n_pass_weeks": len(pass_weeks),
    }

    # regime_family_features.csv
    feat_rows: list[dict[str, Any]] = []
    r5 = json.loads(R5_SUMMARY.read_text(encoding="utf-8"))

    for r in rows_attr:
        w = r["regime_week"]
        yf = float(r["young_frac_of_q4"])
        q4n = int(float(r["q4_candidates"]))
        ypa = int(float(r["young_pool_q4_all"]))
        fq = feat_q4.get(w, {})
        ah = fq.get("activity_heat", {})
        pv = fq.get("price_velocity_5m", {})
        bp = fq.get("buy_pressure_ratio", {})
        tv = fq.get("token_volatility_5m", {})
        pa = fq.get("pool_age_sec", {})
        to = fq.get("time_since_activity_onset_sec", {})
        pr = fq.get("prior_runup_proxy", {})

        r5p = (r5.get("per_regime") or {}).get(w, {})
        pred_allow = frozen_route_allow(yf, ah.get("p50"), tau_young_global, tau_ah_global)

        feat_rows.append(
            {
                "regime_week": w,
                "taxonomy_bucket": r["regime_bucket"],
                "q4_candidate_density_per_sec": round(q4n / REGIME_SEC, 12),
                "young_pool_candidate_share": round(yf, 8),
                "young_pool_intersection_rate": round(ypa / max(q4n, 1), 8),
                "activity_heat_q4_all_mean": ah.get("mean"),
                "activity_heat_q4_all_p50": ah.get("p50"),
                "price_velocity_q4_all_p50": pv.get("p50"),
                "buy_pressure_q4_all_p50": bp.get("p50"),
                "token_volatility_q4_all_p50": tv.get("p50"),
                "pool_age_sec_q4_all_p50": pa.get("p50"),
                "time_since_onset_q4_all_p50": to.get("p50"),
                "prior_runup_proxy_q4_all_p50": pr.get("p50"),
                "ch_candidates_r5": r5p.get("ch_candidates"),
                "executable_r5": r5p.get("executable_candidates"),
                "q4_count": q4n,
                "young_holdout_n_baseline": int(float(r["young_pool_holdout_n"])) if r["young_pool_holdout_n"] else 0,
                "holdout_mean_30m_young_baseline": r.get("holdout_mean_30m") or "",
                "holdout_top1_30m_young": r.get("holdout_top1_30m") or "",
                "holdout_top5_30m_young": r.get("holdout_top5_30m") or "",
                "scorecard_predict_allow_young_pool_route": pred_allow,
                "gold_pass_like": r["regime_bucket"] == "PASS_LIKE",
            }
        )

    keys = list(feat_rows[0].keys())
    with (OUT / "regime_family_features.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(feat_rows)

    # LOOCV: refit tau_ah from PASS weeks excluding one PASS week at a time
    loocv_pass: list[dict[str, Any]] = []
    for held in pass_weeks:
        train = [w for w in pass_weeks if w != held]
        if len(train) < 2:
            loocv_pass.append(
                {
                    "held_out_pass_week": held,
                    "train_pass_weeks": train,
                    "weak_evidence": True,
                    "note": "<2 PASS weeks in train fold",
                }
            )
            continue
        ytr = [float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == w)) for w in train]
        ahtr = [feat_q4[w]["activity_heat"]["p50"] for w in train]
        tau_y = min(ytr) - YOUNG_FRAC_MARGIN
        tau_a = min(ahtr)
        yf_h = float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == held))
        ah_h = feat_q4[held]["activity_heat"]["p50"]
        pred = frozen_route_allow(yf_h, ah_h, tau_y, tau_a)
        loocv_pass.append(
            {
                "held_out_pass_week": held,
                "train_pass_weeks": train,
                "tau_young_frac_fold": tau_y,
                "tau_activity_heat_p50_fold": tau_a,
                "held_young_frac": yf_h,
                "held_activity_heat_p50": ah_h,
                "predict_allow_on_held": pred,
                "weak_evidence": not pred,
                "note": "boundary_pass_week" if not pred else "",
            }
        )

    # Confusion vs taxonomy PASS_LIKE
    confusion: list[dict[str, Any]] = []
    for r in rows_attr:
        w = r["regime_week"]
        yf = float(r["young_frac_of_q4"])
        ah50 = feat_q4[w]["activity_heat"]["p50"]
        pred = frozen_route_allow(yf, ah50, tau_young_global, tau_ah_global)
        gold_pass = r["regime_bucket"] == "PASS_LIKE"
        confusion.append(
            {
                "regime_week": w,
                "taxonomy": r["regime_bucket"],
                "predict_allow": pred,
                "gold_pass_like": gold_pass,
                "tp": pred and gold_pass,
                "fp": pred and not gold_pass,
                "fn": not pred and gold_pass,
                "tn": not pred and not gold_pass,
            }
        )

    fp_weeks = [c["regime_week"] for c in confusion if c["fp"]]
    fn_weeks = [c["regime_week"] for c in confusion if c["fn"]]

    weak_loocv = any(x.get("weak_evidence") for x in loocv_pass)
    loocv_pass_miss = [
        x["held_out_pass_week"] for x in loocv_pass if x.get("predict_allow_on_held") is False
    ]

    g037 = json.loads(G037_SUMMARY.read_text(encoding="utf-8")) if G037_SUMMARY.exists() else {}
    bc = g037.get("baseline_cell") or {
        "pool_max_age_sec": 86400,
        "onset_max_sec": 3600,
        "onset_count_thresh": 10,
    }

    # Route simulation (young_pool holdout, baseline cell) — inline replay (no sprint037g module)
    regimes_sorted = discover_q4_regimes(Q4_DIR)
    all_by_regime = {rg: load_q4_regime_csv(rg, Q4_DIR) for rg in regimes_sorted}
    flat = [x for rg in regimes_sorted for x in all_by_regime[rg]]
    pools_all = sorted({r["pool_address_lower"] for r in flat})
    pool_global_first = fetch_pool_first_seen(ch_url, ch_user, ch_pwd, pools_all)
    if pools_all and not pool_global_first:
        _log("ERROR: ClickHouse first-seen query returned zero pools — check CH connectivity/schema")
        sys.exit(3)

    swaps_cache: dict[str, dict[str, list[int]]] = {}
    PAD_SEC = 72 * 3600
    for rg in regimes_sorted:
        rr = all_by_regime[rg]
        pools = list({r["pool_address_lower"] for r in rr})
        ts_min = min(r["entry_time_sec"] for r in rr) - PAD_SEC
        ts_max = max(r["entry_time_sec"] for r in rr) + 600
        raw = fetch_swaps_regime_window(ch_url, ch_user, ch_pwd, pools, ts_min, ts_max)
        swaps_cache[rg] = {p: raw.get(p, []) for p in pools}
    p_max = int(bc["pool_max_age_sec"])
    o_max = int(bc["onset_max_sec"])
    o_thr = int(bc["onset_count_thresh"])

    allowed_weeks = {
        r["regime_week"]
        for r in rows_attr
        if frozen_route_allow(
            float(r["young_frac_of_q4"]),
            feat_q4[r["regime_week"]]["activity_heat"]["p50"],
            tau_young_global,
            tau_ah_global,
        )
    }

    def collect_holdout_young(reg_subset: set[str]) -> list[dict[str, Any]]:
        ho_y: list[dict[str, Any]] = []
        for rg in regimes_sorted:
            if rg not in reg_subset:
                continue
            rows = all_by_regime[rg]
            _, ho = split_chrono_rows(rows)
            sc = swaps_cache.get(rg, {})
            for row in ho:
                ts_list = sc.get(row["pool_address_lower"], [])
                gfs = pool_global_first.get(row["pool_address_lower"])
                if gfs is None:
                    gfs = ts_list[0] if ts_list else row["entry_time_sec"]
                age = max(0, int(row["entry_time_sec"]) - gfs)
                if age > p_max:
                    continue
                onset = compute_activity_onset(ts_list, int(row["entry_time_sec"]), gfs, o_thr)
                if onset is None:
                    continue
                if int(row["entry_time_sec"]) - onset > o_max:
                    continue
                pv = row.get("price_velocity_5m")
                if pv is None or float(pv) <= FLOW_EPS:
                    continue
                ho_y.append(row)
        return ho_y

    uni_weeks = set(regimes_sorted)
    universal_ho = collect_holdout_young(uni_weeks)
    routed_ho = collect_holdout_young(allowed_weeks)

    def pack_agg(label: str, slice_rows: list[dict[str, Any]]) -> dict[str, Any]:
        st30 = stats_returns([r["net_return_30m"] for r in slice_rows if r.get("net_return_30m") is not None])
        pc = pool_conc_rows(slice_rows)
        return {
            "scenario": label,
            "holdout_young_pool_n": len(slice_rows),
            "mean_30m": st30.get("mean"),
            "median_30m": st30.get("median"),
            "profit_factor_30m": st30.get("profit_factor"),
            "label_1_share_30m": st30.get("label_1_share"),
            **{f"pools_{k}": v for k, v in pc.items()},
        }

    sim_universal = pack_agg("universal_all_8_weeks", universal_ho)
    sim_routed = pack_agg("routed_scorecard_allow_only", routed_ho)

    per_regime_sim: list[dict[str, Any]] = []
    for rg in regimes_sorted:
        sub = collect_holdout_young({rg})
        st = stats_returns([r["net_return_30m"] for r in sub if r.get("net_return_30m") is not None])
        tax = next(x["regime_bucket"] for x in rows_attr if x["regime_week"] == rg)
        pr_allow = rg in allowed_weeks
        per_regime_sim.append(
            {
                "regime_week": rg,
                "taxonomy": tax,
                "scorecard_allow": pr_allow,
                "young_holdout_n": len(sub),
                "mean_30m": st.get("mean"),
                "median_30m": st.get("median"),
                "profit_factor_30m": st.get("profit_factor"),
                "label_1_share_30m": st.get("label_1_share"),
            }
        )

    excluded_negative = [w for w in regimes_sorted if w not in allowed_weeks and tax_neg_like(rows_attr, w)]

    sim_csv_rows = [sim_universal, sim_routed] + per_regime_sim

    with (OUT / "route_simulation_readout.csv").open("w", newline="", encoding="utf-8") as f:
        keys_sim = sorted({k for row in sim_csv_rows for k in row.keys()})
        w = csv.DictWriter(f, fieldnames=keys_sim, extrasaction="ignore")
        w.writeheader()
        for row in sim_csv_rows:
            w.writerow(row)

    # Status heuristics
    if fp_weeks:
        route_status = "CANNOT_ROUTE_YET_RESEARCH_ONLY"
        route_note = f"False positives on non-PASS weeks: {fp_weeks}"
    elif fn_weeks:
        route_status = "REGIME_ROUTING_IMPROVES_BUT_WEAK_EVIDENCE"
        route_note = f"Missed PASS weeks with frozen global rule: {fn_weeks}"
    elif loocv_pass_miss:
        route_status = "REGIME_ROUTING_IMPROVES_BUT_WEAK_EVIDENCE"
        route_note = (
            "PASS-only LOOCV refit misses boundary PASS weeks — fragile evidence "
            f"(missed when τ_ah refit on train PASS subset: {loocv_pass_miss}); "
            "global frozen rule retained for operational simulation."
        )
    elif weak_loocv or len(pass_weeks) < 4:
        route_status = "REGIME_ROUTING_IMPROVES_BUT_WEAK_EVIDENCE"
        route_note = "Small PASS cluster / LOOCV weakness — thresholds indicative only."
    else:
        route_status = "REGIME_ROUTING_RULE_FOUND_REVIEW_REQUIRED"
        route_note = "Frozen scorecard separates PASS_LIKE vs observed failure buckets on static replay."

    summary = {
        "kind": "sprint038c_regime_family_routing",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": route_status,
        "status_notes": route_note,
        "young_pool_onset_flow_strategy_class": "route_gated_scoped_strategy",
        "supersession_candidate": False,
        "cannot_lower_min_holdout": True,
        "no_ml_training": True,
        "proposed_route_rule": proposed_rule,
        "loocv_pass_weeks": loocv_pass,
        "confusion_vs_pass_like": confusion,
        "false_positive_weeks": fp_weeks,
        "false_negative_weeks": fn_weeks,
        "loocv_pass_week_missed_when_refit_tau_on_train_pass_only": loocv_pass_miss,
        "simulation_universal": sim_universal,
        "simulation_routed": sim_routed,
        "excluded_weeks_predicted_mismatch": sorted(set(regimes_sorted) - allowed_weeks),
        "negative_taxonomy_weeks_excluded": [
            r["regime_week"] for r in rows_attr if r["regime_bucket"] in ("NEGATIVE", "EMPTY_LOW_INTERSECTION")
        ],
        "excluded_negative_or_empty_weeks_scorecard_blocked": excluded_negative,
        "reference_sprint038a_universal_holdout": g037.get("baseline_aggregate_holdout_30m"),
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

    (OUT / "regime_family_routing_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    md = RUNS / "run-038C-regime-family-routing-readout.md"
    md.write_text(
        "\n".join(
            [
                "# Run-038C — regime-family routing (young_pool_onset_flow)",
                "",
                f"Generated: {summary['created_at']}",
                "",
                f"**status:** `{route_status}`",
                "",
                "**young_pool_onset_flow:** route-gated scoped strategy (not universal calendar gate).",
                "",
                "## Proposed frozen scorecard",
                "",
                f"- Rule: `{proposed_rule['expr']}`",
                f"- Evidence: derived from PASS_LIKE weeks only (`n={len(pass_weeks)}`) — **weak statistical evidence**.",
                "",
                "## LOOCV",
                "",
                "See `mvp/audits/sprint038c/regime_family_routing_summary.json` → `loocv_pass_weeks`.",
                "",
                "## Simulation (baseline young_pool cell)",
                "",
                f"- Universal holdout young n: **{sim_universal.get('holdout_young_pool_n')}**",
                f"- Routed holdout young n: **{sim_routed.get('holdout_young_pool_n')}**",
                f"- Routed mean_30m: **{sim_routed.get('mean_30m')}** vs universal **{sim_universal.get('mean_30m')}**",
                "",
                "`NO_ALPHA_CLAIM=true` · `supersession_candidate=false`",
                "",
                "---",
                "",
                "*Generated by scripts/sprint038c_regime_family_routing.py*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _log(f"done status={route_status} fp={fp_weeks} fn={fn_weeks} loocv_miss={loocv_pass_miss}")


if __name__ == "__main__":
    main()