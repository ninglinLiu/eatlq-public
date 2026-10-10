#!/usr/bin/env python3
"""Sprint039A — ClickHouse regime discovery, R5 preflight/budget gate, isolated bundle replay stubs.

Phase 0: weekly aggregate discovery (meme-pool WETH-leg universe, canonical R5 semantics).
Preflight: dry-run R5 CH candidate counts × conservative RPC multiplier vs hard caps.
Phase 1+: optional full R5/Q4/S037G/scorecard only when under RPC/time budgets.

No training, supersession, live, or model promotion.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "mvp/audits/sprint039a"
RUNS = REPO / "mvp/runs"

EXCLUDED_WEEKS = frozenset(
    {
        "2025-12-01",
        "2025-12-29",
        "2026-01-05",
        "2026-01-26",
        "2026-02-02",
        "2026-02-23",
        "2026-03-02",
        "2026-03-30",
    }
)

RPC_CAP = 60_000
RUNTIME_CAP_SEC = 45 * 60
RPC_PER_CH_CANDIDATE_CONSERVATIVE = 18
RPC_EFFECTIVE_PER_SEC_ASSUMED = 250
MIN_NEW_WEEKS = 6
MAX_NEW_WEEKS = 12

SPRINT038E_ATTR = REPO / "mvp/audits/sprint038b/regime_failure_attribution.csv"
SPRINT038_FEAT = REPO / "mvp/audits/sprint038b/feature_distribution_by_regime.csv"
Q4_LEGACY = REPO / "mvp/audits/sprint038a/q4_multi_horizon"

TAU_Y = 0.23472400000000002
TAU_AH = 95.0
LOOCV_MARGIN = 0.028


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[039a {ts}] {msg}", flush=True)


def _load_mod(path: Path, name: str = "mod") -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git_status_head(n: int = 50) -> list[str]:
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
        return ["<git status unavailable>"]


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 180.0) -> list[dict]:
    from urllib.request import Request, urlopen

    q = sql.rstrip().rstrip(";") + " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def discover_weekly_metrics(ch_url: str, ch_user: str, ch_pwd: str, r5mod: Any) -> list[dict[str, Any]]:
    """Monday-week aggregates for meme-pool swap activity on Base."""
    cte = r5mod._meme_pools_cte()
    te = r5mod._ts_expr("s")
    sql = f"""
WITH {cte}
SELECT
  formatDateTime(toMonday(toDate({te})), '%Y-%m-%d') AS week_start_iso,
  count() AS swap_cnt,
  uniqExact(lower(s.pool_address)) AS uniq_pools,
  countIf(toDate({te}) < toMonday(toDate({te})) + toIntervalDay(3)) AS swaps_first_3d_of_week
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = 8453
  AND s.removed = 0
  AND {te} >= now() - INTERVAL 200 DAY
  AND {te} < now() - INTERVAL 7 DAY
GROUP BY toMonday(toDate({te}))
ORDER BY swap_cnt DESC
LIMIT 80
"""
    rows = _ch_query(ch_url, sql, ch_user, ch_pwd, timeout=240.0)
    out: list[dict[str, Any]] = []
    for r in rows:
        ws = str(r.get("week_start_iso") or "")
        if ws in EXCLUDED_WEEKS:
            continue
        sc = int(r.get("swap_cnt") or 0)
        up = int(r.get("uniq_pools") or 0)
        burst = int(r.get("swaps_first_3d_of_week") or 0)
        burst_ratio = burst / max(sc, 1)
        score = sc + up * 40 + burst_ratio * sc * 0.1
        out.append(
            {
                "week_start_iso": ws,
                "swap_cnt": sc,
                "uniq_pools": up,
                "swaps_first_3d_of_week": burst,
                "burst_ratio": round(burst_ratio, 6),
                "rank_score": round(score, 4),
            }
        )
    out.sort(key=lambda x: -x["rank_score"])
    return out


def spread_pick_weeks(rows: list[dict[str, Any]], k_min: int, k_max: int) -> list[str]:
    """Greedy spread across YYYY-MM months (max 2/week-starts per month)."""
    picked: list[str] = []
    month_cnt: dict[str, int] = {}
    for r in rows:
        ws = r["week_start_iso"]
        if ws in EXCLUDED_WEEKS or ws in picked:
            continue
        mon = ws[:7]
        if month_cnt.get(mon, 0) >= 2:
            continue
        picked.append(ws)
        month_cnt[mon] = month_cnt.get(mon, 0) + 1
        if len(picked) >= k_max:
            break
    if len(picked) < k_min:
        for r in rows:
            ws = r["week_start_iso"]
            if ws in EXCLUDED_WEEKS or ws in picked:
                continue
            picked.append(ws)
            if len(picked) >= k_min:
                break
    return sorted(picked)


def week_end_exclusive(ws: str) -> str:
    t0 = datetime.strptime(ws, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=7)
    return t1.strftime("%Y-%m-%d")


def dry_run_ch_candidates(week: str) -> tuple[int, str]:
    """Run sprint037b dry-run for one week; parse CH candidate count from stdout."""
    reg = [{"week_start": week, "week_end": week_end_exclusive(week), "role": "positive", "label": f"Sprint039A {week}"}]
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as tf:
        json.dump(reg, tf)
        rj_path = tf.name
    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/sprint037b_r5_label_expansion.py"),
                "--dry-run",
                "--regimes-json",
                rj_path,
                "--out-dir",
                str(AUDIT / "r5_candidates"),
                "--no-global-artifacts",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
            check=False,
        )
        comb = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"CH candidates:\s*([\d,]+)", comb)
        n = int(m.group(1).replace(",", "")) if m else -1
        return n, comb[-4000:] if proc.returncode != 0 else ""
    finally:
        Path(rj_path).unlink(missing_ok=True)


def pivot_activity_p50() -> dict[str, float]:
    out: dict[str, float] = {}
    with SPRINT038_FEAT.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slice") != "q4_all" or row.get("variable") != "activity_heat":
                continue
            p50 = row.get("p50")
            if p50 not in (None, ""):
                out[row["regime_week"]] = float(p50)
    return out


def load_attr() -> list[dict[str, Any]]:
    with SPRINT038E_ATTR.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scorecard_allow_weeks(rows_attr: list[dict[str, Any]], ah50: dict[str, float], tau_y: float, tau_a: float) -> set[str]:
    allow: set[str] = set()
    for r in rows_attr:
        w = r["regime_week"]
        if w not in ah50:
            continue
        yf = float(r["young_frac_of_q4"])
        ah = ah50[w]
        if yf >= tau_y and ah >= tau_a:
            allow.add(w)
    return allow


def loocv_miss(pass_weeks: list[str], rows_attr: list[dict[str, Any]], ah50: dict[str, float], margin: float) -> list[str]:
    miss: list[str] = []
    for held in pass_weeks:
        train = [w for w in pass_weeks if w != held]
        if len(train) < 2:
            continue
        ytr = [float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == w)) for w in train]
        ahtr = [ah50[w] for w in train]
        tau_y = min(ytr) - margin
        tau_a = min(ahtr)
        yh = float(next(x["young_frac_of_q4"] for x in rows_attr if x["regime_week"] == held))
        ahh = ah50[held]
        if not (yh >= tau_y and ahh >= tau_a):
            miss.append(held)
    return miss


def copy_q4_bundle(dest: Path, extra_weeks: list[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    import shutil

    for p in Q4_LEGACY.glob("q4_candidates_*.csv"):
        shutil.copy2(p, dest / p.name)
    for w in extra_weeks:
        src = AUDIT / "q4_multi_horizon" / f"q4_candidates_{w}.csv"
        if src.is_file():
            shutil.copy2(src, dest / src.name)


def run_g037_bundle(q4_dir: Path, audit_out: Path, ch_url: str, ch_user: str, ch_pwd: str) -> dict[str, Any]:
    g037 = REPO / "scripts/sprint037g_young_pool_onset_expansion.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(g037),
            "--q4-dir",
            str(q4_dir),
            "--audit-dir",
            str(audit_out),
            "--no-run-readouts",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=7200,
        env={**os.environ},
        check=False,
    )
    summ_path = audit_out / "expansion_summary.json"
    if summ_path.is_file():
        return {"ok": proc.returncode == 0, "expansion_summary": json.loads(summ_path.read_text(encoding="utf-8"))}
    return {"ok": False, "stderr_tail": (proc.stderr or "")[-2000:]}


def parse_args() -> Any:
    p = argparse.ArgumentParser(
        description="Sprint039A — CH regime discovery, R5 preflight/budget gate, isolated bundles",
    )
    p.add_argument(
        "--rpc-per-ch-candidate",
        type=float,
        default=RPC_PER_CH_CANDIDATE_CONSERVATIVE,
        metavar="N",
        help=(
            "Phase 0 RPC estimate = sum(CH dry-run candidates) × N "
            f"(default {RPC_PER_CH_CANDIDATE_CONSERVATIVE}; tighten after calibration)"
        ),
    )
    p.add_argument(
        "--rpc-multiplier-json",
        type=str,
        default=None,
        metavar="PATH",
        help="JSON from sprint039a_r5_rpc_calibration.py (uses recommended_multiplier_budget_conservative)",
    )
    p.add_argument(
        "--phase1-weeks",
        type=str,
        default=None,
        metavar="CSV",
        help="Comma-separated ISO week starts; intersect with discovery pick for dry-run + Phase 1 only",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rpc_mult = float(args.rpc_per_ch_candidate)
    if args.rpc_multiplier_json:
        jp = Path(args.rpc_multiplier_json)
        jp = jp if jp.is_absolute() else REPO / jp
        cal = json.loads(jp.read_text(encoding="utf-8"))
        rpc_mult = float(cal["recommended_multiplier_budget_conservative"])
        _log(f"RPC multiplier from {jp.name}: {rpc_mult}")

    AUDIT.mkdir(parents=True, exist_ok=True)
    (AUDIT / "r5_candidates").mkdir(parents=True, exist_ok=True)
    (AUDIT / "q4_multi_horizon").mkdir(parents=True, exist_ok=True)
    (AUDIT / "g037").mkdir(parents=True, exist_ok=True)
    (AUDIT / "scorecard").mkdir(parents=True, exist_ok=True)

    git_snap = git_status_head()

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if not ch_pwd:
        _log("ERROR: CLICKHOUSE_PASSWORD unset")
        pre = {
            "kind": "sprint039a_preflight",
            "status": "BLOCKED_BY_CH_OR_SCHEMA",
            "git_status_snapshot_sb_head": git_snap,
        }
        (AUDIT / "regime_discovery_preflight.json").write_text(json.dumps(pre, indent=2), encoding="utf-8")
        sys.exit(3)

    r5path = REPO / "scripts/sprint037b_r5_label_expansion.py"
    r5mod = _load_mod(r5path, "sprint037b_r5")

    t0 = time.time()
    metrics = discover_weekly_metrics(ch_url, ch_user, ch_pwd, r5mod)
    selected_discovery = spread_pick_weeks(metrics, MIN_NEW_WEEKS, MAX_NEW_WEEKS)

    if args.phase1_weeks:
        want = [x.strip() for x in args.phase1_weeks.split(",") if x.strip()]
        disc_set = set(selected_discovery)
        selected = [w for w in want if w in disc_set]
        skipped = [w for w in want if w not in disc_set]
        if skipped:
            _log(f"WARNING: --phase1-weeks not in discovery pick (ignored): {skipped}")
        if want and not selected:
            _log("ERROR: --phase1-weeks ∩ discovery pick is empty — abort artifacts")
            pre = {
                "kind": "sprint039a_regime_discovery_preflight",
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "NEEDS_MORE_REGIME_DISCOVERY",
                "git_status_snapshot_sb_head": git_snap,
                "error": "phase1_weeks_disjoint_from_discovery",
                "phase1_weeks_requested": want,
                "discovery_pick": selected_discovery,
            }
            (AUDIT / "regime_discovery_preflight.json").write_text(json.dumps(pre, indent=2), encoding="utf-8")
            sys.exit(2)
    else:
        selected = list(selected_discovery)

    cand_csv = AUDIT / "regime_discovery_candidates.csv"
    if metrics:
        keys = list(metrics[0].keys())
        with cand_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(metrics)

    est_rows: list[dict[str, Any]] = []
    total_ch = 0
    for w in selected:
        _log(f"dry-run R5 CH sizing {w} …")
        n, err = dry_run_ch_candidates(w)
        est_rows.append({"week_start": w, "ch_candidates_dry_run": n, "stderr_tail_if_fail": err[:500] if err else ""})
        if n > 0:
            total_ch += n

    rpc_est = int(total_ch * rpc_mult)
    runtime_est_sec = int(rpc_est / max(RPC_EFFECTIVE_PER_SEC_ASSUMED, 1))

    phase1_ok = rpc_est <= RPC_CAP and runtime_est_sec <= RUNTIME_CAP_SEC
    phase1_ran = False
    phase2_ran = False
    phase1_skip_reason: str | None = None
    r5_summary: dict[str, Any] = {}
    score_payload: dict[str, Any] = {}
    g037_new: dict[str, Any] = {}
    g037_combo: dict[str, Any] = {}

    if phase1_ok and selected:
        _log("Preflight under caps — starting Phase 1 R5 --run (long-running) …")
        rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
        if not rpc_url:
            _log("LABEL_RPC_URL / EVM_HTTP_URL unset — skip Phase 1 full RPC")
            phase1_skip_reason = "no_rpc_env"
        else:
            reg_path = AUDIT / "selected_regimes_sprint039a.json"
            reg_objs = [
                {
                    "week_start": w,
                    "week_end": week_end_exclusive(w),
                    "role": "positive",
                    "label": f"Sprint039A discovery {w}",
                }
                for w in selected
            ]
            reg_path.write_text(json.dumps(reg_objs, indent=2), encoding="utf-8")
            pr = subprocess.run(
                [
                    sys.executable,
                    str(r5path),
                    "--run",
                    "--regimes-json",
                    str(reg_path),
                    "--out-dir",
                    str(AUDIT / "r5_candidates"),
                    "--no-global-artifacts",
                ],
                cwd=str(REPO),
                timeout=RUNTIME_CAP_SEC + 120,
                env={**os.environ},
                check=False,
            )
            phase1_ran = pr.returncode == 0
            if not phase1_ran:
                phase1_skip_reason = "phase1_subprocess_failed"
            r5_summary = {
                "phase1_exit_code": pr.returncode,
                "regimes": selected,
                "r5_out_dir": str(AUDIT / "r5_candidates"),
            }
            if phase1_ran:
                cpath = REPO / "scripts/sprint037c_multi_horizon.py"
                q4_out = AUDIT / "q4_multi_horizon"
                summ_json = AUDIT / "sprint039a_sprint037c_summary.json"
                pr2 = subprocess.run(
                    [
                        sys.executable,
                        str(cpath),
                        "--run",
                        "--r5-dir",
                        str(AUDIT / "r5_candidates"),
                        "--q4-out-dir",
                        str(q4_out),
                        "--summary-json",
                        str(summ_json),
                        "--regimes-json",
                        str(reg_path),
                    ],
                    cwd=str(REPO),
                    timeout=RUNTIME_CAP_SEC + 120,
                    env={**os.environ},
                    check=False,
                )
                phase2_ran = pr2.returncode == 0
                r5_summary["phase2_sprint037c_exit_code"] = pr2.returncode

                if phase2_ran:
                    g037_new = run_g037_bundle(q4_out, AUDIT / "g037" / "new_weeks_only", ch_url, ch_user, ch_pwd)
                    combo_dir = AUDIT / "q4_bundle_combined"
                    copy_q4_bundle(combo_dir, selected)
                    g037_combo = run_g037_bundle(combo_dir, AUDIT / "g037" / "combined_old_new", ch_url, ch_user, ch_pwd)

    if not phase1_ran:
        r5_dir = AUDIT / "r5_candidates"
        for stale in r5_dir.glob("candidates_*.csv"):
            stale.unlink(missing_ok=True)

    ah50 = pivot_activity_p50()
    rows_attr = load_attr()
    pass_weeks = [r["regime_week"] for r in rows_attr if r["regime_bucket"] == "PASS_LIKE"]
    lo_miss = loocv_miss(pass_weeks, rows_attr, ah50, LOOCV_MARGIN)
    allow_set = scorecard_allow_weeks(rows_attr, ah50, TAU_Y, TAU_AH)

    fp_w = [r["regime_week"] for r in rows_attr if r["regime_week"] in allow_set and r["regime_bucket"] != "PASS_LIKE"]
    fn_w = [r["regime_week"] for r in rows_attr if r["regime_week"] not in allow_set and r["regime_bucket"] == "PASS_LIKE"]

    new_weeks_with_labels = [w for w in selected if (AUDIT / "q4_multi_horizon" / f"q4_candidates_{w}.csv").is_file()]
    new_pass_like_estimated = "UNKNOWN_WITHOUT_TAXONOMY_LABELING"

    if not metrics:
        status = "BLOCKED_BY_CH_OR_SCHEMA"
    elif not selected:
        status = "NEEDS_MORE_REGIME_DISCOVERY"
    elif not phase1_ok:
        status = "PREFLIGHT_TOO_EXPENSIVE_STOPPED"
    elif phase1_ok and not phase1_ran:
        status = "NEEDS_MORE_REGIME_DISCOVERY"
    elif phase1_ran and phase2_ran and new_weeks_with_labels:
        status = "NEW_R5_COVERAGE_FOUND_SCORECARD_REVIEW_REQUIRED"
        if fp_w or fn_w:
            status = "SCORECARD_INVALIDATED_BY_NEW_REGIMES"
    elif phase1_ran:
        status = "NEEDS_MORE_REGIME_DISCOVERY"
    else:
        status = "PREFLIGHT_TOO_EXPENSIVE_STOPPED"

    preflight = {
        "kind": "sprint039a_regime_discovery_preflight",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "git_status_snapshot_sb_head": git_snap,
        "excluded_anchor_weeks": sorted(EXCLUDED_WEEKS),
        "discovery_row_count": len(metrics),
        "selected_disjoint_weeks": selected,
        "ch_discovery_seconds": round(time.time() - t0, 3),
        "dry_run_estimates_by_week": est_rows,
        "aggregate_preflight": {
            "sum_ch_candidates_dry_run": total_ch,
            "rpc_call_estimate_conservative": rpc_est,
            "rpc_cap": RPC_CAP,
            "runtime_estimate_seconds": runtime_est_sec,
            "runtime_cap_seconds": RUNTIME_CAP_SEC,
            "rpc_per_ch_candidate_assumption": rpc_mult,
            "legacy_conservative_multiplier_reference": RPC_PER_CH_CANDIDATE_CONSERVATIVE,
            "abort_threshold_note": "RPC_CAP=60000 or RUNTIME_CAP=45min stops Phase 1 full labeling.",
            "phase1_eligible_under_caps": phase1_ok,
        },
        "phase1_full_r5_run_executed": phase1_ran,
        "phase2_sprint037c_executed": phase2_ran,
        "phase1_skip_reason": phase1_skip_reason,
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
        "supersession_candidate": False,
    }
    if phase1_ok and not phase1_ran:
        if phase1_skip_reason == "no_rpc_env":
            preflight["preflight_notes"] = (
                "RPC/time budget OK under conservative estimate but LABEL_RPC_URL/EVM_HTTP_URL unset — Phase 1 not started."
            )
        elif phase1_skip_reason == "phase1_subprocess_failed":
            preflight["preflight_notes"] = "Phase 1 subprocess returned non-zero — inspect sprint037b logs."
        else:
            preflight["preflight_notes"] = "Budget OK but Phase 1 did not complete (unknown skip)."
    (AUDIT / "regime_discovery_preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")

    r5_cov = {
        "kind": "sprint039a_r5_coverage",
        "selected_weeks": selected,
        "r5_candidates_dir": str(AUDIT / "r5_candidates"),
        "phase1_executed": phase1_ran,
        "phase2_q4_dir": str(AUDIT / "q4_multi_horizon"),
        "summary": r5_summary,
    }
    (AUDIT / "r5_coverage_summary.json").write_text(json.dumps(r5_cov, indent=2), encoding="utf-8")

    score_payload = {
        "kind": "sprint039a_scorecard_expansion",
        "created_at": preflight["created_at"],
        "status": status,
        "scorecard_v1_frozen": {"tau_young_frac": TAU_Y, "tau_activity_heat_p50_q4_all": TAU_AH},
        "taxonomy_weeks_sprint038b_only": [r["regime_week"] for r in rows_attr],
        "fp_weeks_attributed": fp_w,
        "fn_weeks_attributed": fn_w,
        "loocv_miss_pass_weeks_margin_0_028": lo_miss,
        "loocv_improved_vs_sprint038e": False,
        "new_disjoint_weeks": selected,
        "new_weeks_q4_csv_present": new_weeks_with_labels,
        "new_pass_like_classification": new_pass_like_estimated,
        "interpretation_axes": {
            "no_coverage": "No q4_candidates CSV / R5 failure for new weeks.",
            "economics_negative": "Young_pool_onset universal mean<=0 or PF<=1 on bundle (see g037 summaries when present).",
            "scorecard_blocked_correctly": "Frozen scorecard denies weak-regime weeks while PASS-like retained.",
            "scorecard_false_positive": "Predict allow on non-PASS taxonomy week.",
            "scorecard_false_negative": "Predict deny on PASS-like taxonomy week.",
        },
        "sprint037g_bundles": {"new_weeks_only": g037_new, "combined_placeholder": g037_combo},
        "note": "PASS/FP/FN for new weeks requires dedicated taxonomy labeling sprint — not inferred here.",
    }
    (AUDIT / "scorecard_expansion_summary.json").write_text(json.dumps(score_payload, indent=2), encoding="utf-8")

    by_reg_legacy = []
    for r in rows_attr:
        w = r["regime_week"]
        by_reg_legacy.append(
            {
                "bundle": "legacy_sprint038b_attributed_only",
                "regime_week": w,
                "young_frac_of_q4": float(r["young_frac_of_q4"]),
                "activity_heat_p50_q4_all": ah50.get(w),
                "scorecard_allow": w in allow_set,
                "taxonomy": r["regime_bucket"],
            }
        )
    for w in selected:
        by_reg_legacy.append(
            {
                "bundle": "new_week_no_taxonomy",
                "regime_week": w,
                "young_frac_of_q4": "",
                "activity_heat_p50_q4_all": "",
                "scorecard_allow": "",
                "taxonomy": "NOT_LABELED",
            }
        )
    if by_reg_legacy:
        bk = list(by_reg_legacy[0].keys())
        with (AUDIT / "scorecard_by_regime.csv").open("w", newline="", encoding="utf-8") as f:
            ww = csv.DictWriter(f, fieldnames=bk)
            ww.writeheader()
            ww.writerows(by_reg_legacy)

    neigh = []
    for name, dy, da in [
        ("baseline", 0.0, 0.0),
        ("stricter_young+0.02", 0.02, 0.0),
        ("stricter_ah+5", 0.0, 5.0),
        ("looser_young-0.02", -0.02, 0.0),
        ("looser_ah-5", 0.0, -5.0),
    ]:
        allow = scorecard_allow_weeks(rows_attr, ah50, TAU_Y + dy, TAU_AH + da)
        fp = [r["regime_week"] for r in rows_attr if r["regime_week"] in allow and r["regime_bucket"] != "PASS_LIKE"]
        fn = [r["regime_week"] for r in rows_attr if r["regime_week"] not in allow and r["regime_bucket"] == "PASS_LIKE"]
        neigh.append(
            {
                "variant": name,
                "tau_young": TAU_Y + dy,
                "tau_ah": TAU_AH + da,
                "fp_weeks": ";".join(fp),
                "fn_weeks": ";".join(fn),
                "loocv_miss": ";".join(loocv_miss(pass_weeks, rows_attr, ah50, LOOCV_MARGIN)),
            }
        )
    with (AUDIT / "loocv_hardening.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(neigh[0].keys()))
        w.writeheader()
        w.writerows(neigh)

    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "run-039A-regime-discovery-r5-expansion-readout.md").write_text(
        "\n".join(
            [
                "# Run-039A — regime discovery + R5 expansion preflight",
                "",
                f"Generated: {preflight['created_at']}",
                "",
                f"**status:** `{status}`",
                "",
                f"- **selected disjoint weeks:** `{selected}`",
                f"- **RPC estimate (conservative):** `{rpc_est}` cap `{RPC_CAP}`",
                f"- **runtime estimate:** `{runtime_est_sec}s` cap `{RUNTIME_CAP_SEC}s`",
                f"- **Phase 1 R5 --run executed:** `{phase1_ran}`",
                f"- **Phase 2 Sprint037C executed:** `{phase2_ran}`",
                f"- **phase1_skip_reason:** `{phase1_skip_reason}`",
                "",
                "When Phase 1 does not run, CH-only dry-run `candidates_*.csv` files under "
                "`mvp/audits/sprint039a/r5_candidates/` are removed so the tree is not mistaken "
                "for canonical RPC-labeled R5 output.",
                "",
                "`NO_ALPHA_CLAIM=true` · canonical economics require Sprint037G on generated q4 CSVs.",
                "",
                "---",
                "",
                "*Generated by scripts/sprint039a_regime_discovery_r5_expansion.py*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _log(f"done status={status} weeks={len(selected)} rpc_est={rpc_est} phase1={phase1_ran}")


if __name__ == "__main__":
    main()
