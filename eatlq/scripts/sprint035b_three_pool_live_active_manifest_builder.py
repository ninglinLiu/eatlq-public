#!/usr/bin/env python3
"""Sprint035B — deterministic 3-pool live-active smoke manifest from Sprint035A viability + dashcam.

When unique 5m buckets are sparse, rows use explicit `signal_ts_ms_audit_only`
(resolved by sprint030d for this profile).

Usage:
  python3 scripts/sprint035b_three_pool_live_active_manifest_builder.py --write-manifest [--dashcam PATH]
  python3 scripts/sprint035b_three_pool_live_active_manifest_builder.py --write-audit
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MIN_ROWS = 120
MAX_ROWS = 250
PER_POOL_CAP = 90
TARGET_ROWS = 230
TOP1_CAP = 0.45
TOP2_CAP = 0.75

SPRINT034_DEAD = frozenset(
    {
        "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
        "0x8aed442e9c17746c0eb85dd22981ce8974182892",
        "0x1e98a167082332e5e89146662e34c3c99481e5e6",
    }
)

OUT_MANIFEST = REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_manifest.json"
OUT_AUDIT = REPO / "mvp/audits/sprint035b_three_pool_live_active_smoke_report.md"
SUMMARY_JSON = REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_label_summary.json"
LABELS_CSV = REPO / "mvp/diagnostics/sprint035b_three_pool_live_active_smoke_labels.csv"

MIN_SUMMARY = REPO / "mvp/diagnostics/sprint035a_live_active_mining_summary.json"
VIABILITY_CSV = REPO / "mvp/diagnostics/sprint035a_live_active_viability_audit.csv"


def load_three_pools_strict() -> list[str]:
    pools = sorted(
        p.strip().lower()
        for p in (json.loads(MIN_SUMMARY.read_text(encoding="utf-8")).get("viability_pass_pool_addresses") or [])
        if str(p).strip().lower().startswith("0x")
    )
    if len(pools) != 3:
        raise RuntimeError(f"expected_3_viability_pools,got_{len(pools)}")
    for p in pools:
        if p in SPRINT034_DEAD:
            raise RuntimeError(f"sprint034_dead_pool_in_list:{p}")
    # Cross-check viability CSV PASS rows
    pass_csv: set[str] = set()
    with VIABILITY_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("viability_status") or "").strip().upper() != "PASS":
                continue
            pass_csv.add((row.get("pool_address_audit_key") or "").strip().lower())
    if set(pools) != pass_csv:
        raise RuntimeError("viability_pool_set_mismatch_summary_vs_csv")
    return pools


def max_feasible_row_count_under_share_caps(avail_by_pool: dict[str, int], top1_cap: float, top2_cap: float) -> int:
    """Upper bound on row count respecting per-pool availability and concentration caps (brute force)."""
    keys = tuple(sorted(avail_by_pool.keys()))
    if len(keys) != 3:
        return 0
    a_max, b_max, c_max = (avail_by_pool[k] for k in keys)
    best = 0
    for na in range(a_max + 1):
        for nb in range(b_max + 1):
            for nc in range(c_max + 1):
                n = na + nb + nc
                if n <= best:
                    continue
                vals = sorted([na, nb, nc], reverse=True)
                if vals[0] / n <= top1_cap + 1e-12 and (vals[0] + vals[1]) / n <= top2_cap + 1e-12:
                    best = n
    return best


def three_pool_manifest_shape_ok(rows_triples: list[tuple[str, int, int]]) -> tuple[bool, str]:
    n = len(rows_triples)
    if n < MIN_ROWS or n > MAX_ROWS:
        return False, f"rows_{n}_not_in_{MIN_ROWS}_{MAX_ROWS}"
    pools_all = [p for p, _, _ in rows_triples]
    if len(set(pools_all)) != 3:
        return False, "pool_count_not_three"
    ctr = Counter(pools_all)
    if max(ctr.values()) > PER_POOL_CAP:
        return False, "per_pool_row_cap_gt_90"
    sh = sorted(ctr.values(), reverse=True)
    tot = float(n)
    top1 = sh[0] / tot
    top2 = (sh[0] + sh[1]) / tot
    if top1 > TOP1_CAP + 1e-12:
        return False, "top1_share_gt_cap"
    if top2 > TOP2_CAP + 1e-12:
        return False, "top2_share_gt_cap"
    return True, "ok"


def collect_dashcam_rows_for_pools(dashcam_path: Path, pools: list[str]) -> dict[str, list[tuple[int, int, int]]]:
    pool_set = set(pools)
    by_ts: dict[str, dict[int, tuple[int, int, int]]] = {p: {} for p in pools}
    with dashcam_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            tok = (row.get("token_address") or "").strip().lower()
            if tok not in pool_set:
                continue
            try:
                ts_ms = int(float(row.get("timestamp", 0)))
            except (TypeError, ValueError):
                continue
            if ts_ms <= 0:
                continue
            bsec = (ts_ms // 1000 // 300) * 300
            bms = bsec * 1000
            by_ts[tok][ts_ms] = (ts_ms, bms, bsec)
    out: dict[str, list[tuple[int, int, int]]] = {}
    for p in pools:
        rows = sorted(by_ts[p].values(), key=lambda z: (-z[0], z[2]))
        out[p] = rows
    return out


def build_manifest_rows_balanced(by_pool_rows: dict[str, list[tuple[int, int, int]]]) -> list[tuple[str, int, int, int]]:
    pools = sorted(by_pool_rows.keys())
    ptr = {p: 0 for p in pools}
    per_ct: Counter[str] = Counter()
    chosen: list[tuple[str, int, int, int]] = []
    seen_ts: set[tuple[str, int]] = set()
    effective_cap = min(PER_POOL_CAP, 83)

    def can_pick(p: str) -> bool:
        return ptr[p] < len(by_pool_rows[p]) and per_ct[p] < effective_cap

    while len(chosen) < TARGET_ROWS:
        progressed = False
        for p in pools:
            if len(chosen) >= TARGET_ROWS:
                break
            while can_pick(p):
                ts_ms, bms, bsec = by_pool_rows[p][ptr[p]]
                ptr[p] += 1
                if (p, ts_ms) in seen_ts:
                    continue
                seen_ts.add((p, ts_ms))
                chosen.append((p, bms, bsec, ts_ms))
                per_ct[p] += 1
                progressed = True
                break
        if not progressed:
            break

    if len(chosen) < MIN_ROWS:
        return []

    def triples(seq: list[tuple[str, int, int, int]]) -> list[tuple[str, int, int]]:
        return [(a, b, c) for a, b, c, _ in seq]

    def shape_ok(seq: list[tuple[str, int, int, int]]) -> bool:
        return three_pool_manifest_shape_ok(triples(seq))[0]

    while len(chosen) > MIN_ROWS and not shape_ok(chosen):
        victim = max(per_ct, key=lambda pp: per_ct[pp])
        for i in range(len(chosen) - 1, -1, -1):
            if chosen[i][0] == victim:
                per_ct[victim] -= 1
                chosen.pop(i)
                break

    if not shape_ok(chosen):
        return []

    while len(chosen) > MAX_ROWS:
        victim = max(per_ct, key=lambda pp: per_ct[pp])
        for i in range(len(chosen) - 1, -1, -1):
            if chosen[i][0] == victim:
                per_ct[victim] -= 1
                chosen.pop(i)
                break

    if len(chosen) < MIN_ROWS or not shape_ok(chosen):
        return []
    return chosen


def write_manifest(by_pool_rows: dict[str, list[tuple[int, int, int]]], pools: list[str]) -> tuple[str, dict[str, Any]]:
    avail_counts = {p: len(by_pool_rows.get(p) or ()) for p in pools}
    max_feasible = max_feasible_row_count_under_share_caps(avail_counts, TOP1_CAP, TOP2_CAP)
    feasibility_block = {
        "dashcam_distinct_timestamp_rows_by_pool": dict(sorted(avail_counts.items())),
        "max_feasible_rows_under_top1_top2_share_caps_given_availability": max_feasible,
        "concentration_caps": {"top1_row_share_max": TOP1_CAP, "top2_row_share_max": TOP2_CAP},
    }
    specs = build_manifest_rows_balanced(by_pool_rows)
    triples = [(p, b, s) for p, b, s, _ in specs]
    if len(specs) < MIN_ROWS:
        infeasible_caps = max_feasible < MIN_ROWS and sum(avail_counts.values()) >= MIN_ROWS
        stub = {
            "kind": "three_pool_live_active_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "TOO_SMALL_FOR_THREE_POOL_SMOKE",
            "reason": (
                "distinct_signal_timestamps_insufficient_under_concentration_caps_top1_le_045_top2_le_075; "
                f"max_feasible_rows_given_caps_and_availability={max_feasible}_minimum_required_{MIN_ROWS}"
                if infeasible_caps
                else (
                    "insufficient_distinct_timestamps_available_in_logs_dashcam_csv_for_three_viability_pass_pools; "
                    f"constructed_{len(specs)}_preview_rows_below_{MIN_ROWS}"
                )
            ),
            "balance_feasibility_audit_only": feasibility_block,
            "selected_pool_count": 3,
            "selected_rows_count": len(specs),
            "manifest_rows": [],
            "live_active_viability_pass_required": True,
            "historical_entry_viability_pass_required": True,
            "current_quote_viability_pass_required": True,
            "no_training": True,
            "no_live": True,
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n", encoding="utf-8")
        return "TOO_SMALL_FOR_THREE_POOL_SMOKE", stub

    ok, why = three_pool_manifest_shape_ok(triples)
    if not ok:
        stub = {
            "kind": "three_pool_live_active_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "TOO_SMALL_FOR_THREE_POOL_SMOKE",
            "reason": "balance_or_cap_checks_failed_" + why,
            "balance_feasibility_audit_only": feasibility_block,
            "manifest_rows": [],
            "live_active_viability_pass_required": True,
            "historical_entry_viability_pass_required": True,
            "current_quote_viability_pass_required": True,
            "no_training": True,
            "no_live": True,
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n", encoding="utf-8")
        return "TOO_SMALL_FOR_THREE_POOL_SMOKE", stub

    ctr = Counter(p for p, _, _, _ in specs)
    obj: dict[str, Any] = {
        "kind": "three_pool_live_active_scoped_smoke_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "live_active_viability_pass_required": True,
        "historical_entry_viability_pass_required": True,
        "current_quote_viability_pass_required": True,
        "sprint_scope": "sprint035b_three_pool_live_active_scoped_only",
        "no_sprint034_dead_scope_pools": True,
        "no_post_label_pool_replacement": True,
        "no_closed_lineage_rescue_sprint030_032": True,
        "selected_pool_count": 3,
        "selected_rows_count": len(specs),
        "per_pool_row_cap": PER_POOL_CAP,
        "distribution_caps_audit_only": {"top1_row_share_max": TOP1_CAP, "top2_row_share_max": TOP2_CAP},
        "deterministic_bucket_and_ts_selection_rule": (
            "round_robin_recent_timestamps_explicit_signal_ts_fallback_when_bucket_cardinality_below_min_rows "
            "(prefers_recent_ms_sort_within_pool_balanced_circulation)"
        ),
        "source_artifacts": [
            str(MIN_SUMMARY.relative_to(REPO)),
            str(VIABILITY_CSV.relative_to(REPO)),
            "logs/dashcam.csv",
            "scripts/sprint035b_three_pool_live_active_manifest_builder.py",
            "docs/label-semantics-freeze.md",
            "docs/ml-training-gate.md",
            "mvp/audits/failed_no_7d_alpha_guard.md",
            "scripts/sprint035a_live_active_pool_mining.py",
        ],
        "pool_address_audit_keys": pools,
        "bucket_count_by_pool": dict(sorted(ctr.items())),
        "manifest_rows": [
            {
                "pool_audit_only": p,
                "bucket_ms": bm,
                "bucket_sec": bs,
                "signal_ts_ms_audit_only": ts,
            }
            for p, bm, bs, ts in specs
        ],
        "manifest_rows_preview_max15": [],
    }
    obj["manifest_rows_preview_max15"] = obj["manifest_rows"][:15]
    OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    OUT_MANIFEST.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    return "MANIFEST_READY", obj


def _winsor_mean(xs: list[float], pct: float) -> float | None:
    if not xs:
        return None
    srt = sorted(xs)
    k = max(1, int(len(srt) * pct))
    core = srt[k : len(srt) - k] if len(srt) > 2 * k else srt
    return float(statistics.mean(core)) if core else None


def _trimmed_mean(xs: list[float], trim_frac: float) -> float | None:
    if not xs:
        return None
    srt = sorted(xs)
    k = int(len(srt) * trim_frac)
    sl = srt[k : len(srt) - k] if k * 2 < len(srt) else srt
    return float(statistics.mean(sl)) if sl else None


def write_audit() -> str:
    if not SUMMARY_JSON.is_file() or not LABELS_CSV.is_file():
        OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        OUT_AUDIT.write_text(
            "# Sprint035B — three-pool live-active smoke\n\nLabels or summary missing — run manifest builder + "
            "`sprint030d_bounded_smoke_label` first.\n",
            encoding="utf-8",
        )
        return "FAIL"

    sj = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    with LABELS_CSV.open(newline="", encoding="utf-8") as fh:
        lab = list(csv.DictReader(fh))

    nets = [float(r["net_return"]) for r in lab if r.get("label") in ("0", "1")]
    labels_1 = sum(1 for r in lab if r.get("label") == "1")
    labels_0 = sum(1 for r in lab if r.get("label") == "0")

    def pct_pos(arr: list[float]) -> float:
        return float(sum(1 for x in arr if x > 0)) / len(arr) if arr else 0.0

    clean = float(sj.get("clean_rate", 0))
    failr = float(sj.get("failure_rate", 1))
    mean_n = float(statistics.mean(nets)) if nets else 0.0
    med_n = float(statistics.median(nets)) if nets else 0.0
    p25 = float(statistics.quantiles(nets, n=4)[0]) if len(nets) >= 4 else (min(nets) if nets else 0.0)
    p75 = float(statistics.quantiles(nets, n=4)[2]) if len(nets) >= 4 else (max(nets) if nets else 0.0)

    # balance
    ap = Counter((r["pool_address"] or "").lower() for r in lab)
    tot = len(lab) or 1
    sh = sorted(ap.values(), reverse=True)
    top1s = sh[0] / tot if sh else 0.0
    top2s = (sh[0] + sh[1]) / tot if len(sh) >= 2 else top1s

    # LOO by pool
    pools_u = sorted(ap.keys())
    loo: dict[str, float] = {}
    for ex in pools_u:
        sub = [float(r["net_return"]) for r in lab if (r["pool_address"] or "").lower() != ex]
        loo[ex] = float(statistics.mean(sub)) if sub else 0.0

    half = len(lab) // 2
    first = [float(r["net_return"]) for r in lab[:half]]
    second = [float(r["net_return"]) for r in lab[half:]]

    tm5 = _trimmed_mean(nets, 0.05) or 0.0
    win5 = _winsor_mean(nets, 0.05)

    pool_means = {p: float(statistics.mean([float(r["net_return"]) for r in lab if (r["pool_address"] or "").lower() == p])) for p in pools_u}

    status = "THREE_POOL_LIVE_ACTIVE_SMOKE_PASS"
    if clean < 0.95 or failr > 0.05:
        status = "NEEDS_LABEL_SEMANTICS_REPAIR"
    elif clean >= 0.95 and failr <= 0.05:
        econ_weak = mean_n <= 0 or med_n < 0 or tm5 <= 0
        if econ_weak:
            status = "PIPELINE_OK_ECONOMICALLY_WEAK"
        else:
            pos_ok = min(pool_means.values()) > float("-inf")
            temporal_ok = (not first or not second) or (statistics.mean(first) * statistics.mean(second) >= 0)
            loo_ok = all(v > 0 for v in loo.values())
            concentrated_ok = top1s <= 0.45
            promising = (
                mean_n > 0
                and med_n >= 0
                and tm5 > 0
                and (win5 is None or win5 > 0)
                and labels_1 > 0
                and min(ap.values()) > 0
                and temporal_ok
                and loo_ok
                and concentrated_ok
                and pos_ok
            )
            if promising:
                status = "THREE_POOL_LIVE_ACTIVE_SMOKE_PROMISING_REVIEW_REQUIRED"
            else:
                status = "THREE_POOL_LIVE_ACTIVE_SMOKE_PASS"

    lines = [
        "",
        "Bounded diagnostic over **three** Sprint035A viability PASS pools. **Not** general model evidence, **not** alpha proof, **not** training/live/promotion.",
        "",
        f"## Decision (automated heuristic): `{status}`",
        "",
        "## Label pipeline metrics (from summary JSON)",
        "",
        f"- attempted_rows: **{sj.get('attempted_rows')}**",
        f"- labeled_rows: **{sj.get('labeled_rows')}**",
        f"- quote_failed_count: **{sj.get('quote_failed_count')}**",
        f"- clean_rate: **{sj.get('clean_rate')}**",
        f"- failure_rate: **{sj.get('failure_rate')}**",
        f"- conceptual_entry_exit_pairs_budget: **{sj.get('conceptual_entry_exit_pairs_budget')}**",
        f"- approx_eth_rpc_round_trips_total: **{sj.get('approx_eth_rpc_round_trips_total')}**",
        f"- wall_time_sec: **{sj.get('wall_time_sec')}**",
        f"- summary status: **{sj.get('status')}** / acceptance_gates_pass=**{sj.get('acceptance_gates_pass')}**",
        "",
        "## Balance / concentration (labeled rows only)",
        "",
        f"- per-pool labeled counts: **{dict(ap)}**",
        f"- top1 row share: **{round(top1s, 4)}**",
        f"- top2 row share: **{round(top2s, 4)}**",
        f"- zero-row pool check: **{min(ap.values()) if ap else 0}** (min count)",
        "",
        "## Economics (labeled rows; descriptive only)",
        "",
        f"- label_0 / label_1: **{labels_0}** / **{labels_1}**",
        f"- label_1 share: **{round(labels_1 / len(lab), 4) if lab else 0}**",
        f"- mean / median net_return: **{round(mean_n, 6)}** / **{round(med_n, 6)}**",
        f"- p25 / p75 net_return: **{round(p25, 6)}** / **{round(p75, 6)}**",
        f"- positive share (net>0): **{round(pct_pos(nets), 4)}**",
        f"- winsorized mean (~1% each tail): **{_winsor_mean(nets, 0.01)}**",
        f"- winsorized mean (~5% each tail): **{_winsor_mean(nets, 0.05)}**",
        f"- trimmed mean (5% each tail): **{_trimmed_mean(nets, 0.05)}**",
        f"- first-half mean / second-half mean: **{round(statistics.mean(first),6) if first else 0}** / "
        f"**{round(statistics.mean(second),6) if second else 0}**",
        "",
        "### Per-pool means (net_return)",
        "",
    ]
    for p in pools_u:
        sub = [float(r["net_return"]) for r in lab if (r["pool_address"] or "").lower() == p]
        lines.append(
            f"- `{p}`: n={len(sub)} mean={round(statistics.mean(sub),6) if sub else 0} "
            f"median={round(statistics.median(sub),6) if sub else 0} "
            f"pos_share={round(pct_pos(sub),4)}"
        )
    lines.extend(
        [
            "",
            "### Leave-one-pool-out mean net_return",
            "",
            *[f"- exclude `{k}`: mean **{round(v, 6)}**" for k, v in sorted(loo.items())],
            "",
            "## Artifacts",
            "",
            f"- `{OUT_MANIFEST.relative_to(REPO)}`",
            f"- `{LABELS_CSV.relative_to(REPO)}`",
            f"- `{SUMMARY_JSON.relative_to(REPO)}`",
            "",
        ]
    )
    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text("\n".join(lines), encoding="utf-8")
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-manifest", action="store_true")
    ap.add_argument("--write-audit", action="store_true")
    ap.add_argument("--dashcam", type=Path, default=REPO / "logs/dashcam.csv")
    args = ap.parse_args()
    if args.write_manifest:
        pools = load_three_pools_strict()
        by_pool_rows = collect_dashcam_rows_for_pools(Path(args.dashcam), pools)
        st, _ = write_manifest(by_pool_rows, pools)
        print(st)
        return 0 if st == "MANIFEST_READY" else 2
    if args.write_audit:
        print(write_audit())
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
