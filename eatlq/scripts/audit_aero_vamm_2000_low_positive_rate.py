#!/usr/bin/env python3
"""
Static audit: low positive-rate drivers for Aero vAMM 2000-row dry-run output.

No RPC, no label changes, no CSV mutation.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "logs" / "aero_vamm_canonical_label_dryrun_2000.csv"
OUT_JSON = REPO / "mvp" / "diagnostics" / "aero_vamm_2000_low_positive_rate_audit.json"
OUT_MD = REPO / "mvp" / "runs" / "aero-vamm-2000-low-positive-rate-audit.md"

CANONICAL_ROUND_TRIP = 0.006


def norm_addr(v: str) -> str:
    t = (v or "").strip().lower()
    if not t:
        return ""
    return t if t.startswith("0x") else f"0x{t}"


def parse_float(s: str) -> float | None:
    try:
        return float((s or "").strip())
    except ValueError:
        return None


def parse_int(s: str) -> int | None:
    try:
        return int((s or "").strip())
    except ValueError:
        return None


def quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {k: None for k in ("min", "p05", "p25", "median", "p75", "p95", "max", "mean")}
    xs = sorted(vals)
    n = len(xs)

    def q(p: float) -> float:
        i = min(n - 1, max(0, int(round((n - 1) * p))))
        return xs[i]

    return {
        "min": xs[0],
        "p05": q(0.05),
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "p95": q(0.95),
        "max": xs[-1],
        "mean": sum(xs) / n,
    }


def positive_rate_mask(rows: list[dict[str, str]], predicate) -> float:
    ok = [r for r in rows if (r.get("failure_reason") or "").strip() == "ok"]
    if not ok:
        return 0.0
    return sum(1 for r in ok if predicate(r)) / len(ok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-md", type=Path, default=OUT_MD)
    args = ap.parse_args()

    with args.csv.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    ok_rows = [r for r in rows if (r.get("failure_reason") or "").strip() == "ok"]
    n_ok = len(ok_rows)

    net_returns: list[float] = []
    gross_returns: list[float] = []
    for r in ok_rows:
        nv = parse_float(r.get("net_return", ""))
        if nv is None:
            continue
        net_returns.append(nv)
        gross_returns.append(nv + CANONICAL_ROUND_TRIP)

    after_cost_positive = sum(1 for x in net_returns if x > 0)
    gross_positive = sum(1 for x in gross_returns if x > 0)

    positive_rate_after_cost = after_cost_positive / n_ok if n_ok else 0.0
    gross_positive_rate = gross_positive / n_ok if n_ok else 0.0

    cost_grid = [0.0, 0.002, 0.004, 0.006, 0.008]
    cost_sensitivity = {}
    for c in cost_grid:
        # return_after_c = gross_return - c = net_return + CANONICAL_ROUND_TRIP - c
        cnt = sum(1 for g in gross_returns if (g - c) > 0)
        cost_sensitivity[str(c)] = {
            "positive_count": cnt,
            "positive_rate": round(cnt / n_ok, 6) if n_ok else 0.0,
        }

    by_pool: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in ok_rows:
        by_pool[norm_addr(r.get("pool_address", ""))].append(r)

    pool_stats = {}
    pool_positive_rates = {}
    pool_net_dist = {}
    for pool, pr in sorted(by_pool.items()):
        nets = [parse_float(x.get("net_return", "")) for x in pr]
        nets = [x for x in nets if x is not None]
        pool_net_dist[pool] = {k: (round(v, 12) if v is not None else None) for k, v in quantiles(nets).items()}
        pos = sum(1 for x in nets if x > 0)
        pool_positive_rates[pool] = round(pos / len(pr), 6) if pr else 0.0
        pool_stats[pool] = {"row_count": len(pr), "positive_count": pos, "positive_rate": pool_positive_rates[pool]}

    # timestamp quartiles (ms)
    ts_vals = []
    for r in ok_rows:
        t = parse_int(r.get("timestamp", ""))
        if t is not None:
            ts_vals.append(t)
    ts_sorted = sorted(ts_vals)
    n_ts = len(ts_sorted)

    def ts_qcut(i: int) -> tuple[int | None, int | None]:
        if n_ts == 0:
            return None, None
        lo = int(round((n_ts - 1) * (i / 4)))
        hi = int(round((n_ts - 1) * ((i + 1) / 4)))
        return ts_sorted[lo], ts_sorted[hi]

    quartile_labels = ["Q1", "Q2", "Q3", "Q4"]
    time_strat = {}
    for qi, lab in enumerate(quartile_labels):
        lo_b, hi_b = ts_qcut(qi)
        if lo_b is None:
            time_strat[lab] = {"positive_rate": None, "row_count": 0}
            continue
        sub = [
            r
            for r in ok_rows
            if parse_int(r.get("timestamp", "")) is not None
            and lo_b <= parse_int(r.get("timestamp", "")) <= hi_b  # type: ignore[arg-type]
        ]
        # Widen to true quartile bins by rank for cleaner split
        pass

    # Proper quartile bins by rank
    indexed = [(parse_int(r.get("timestamp", "")), r) for r in ok_rows]
    indexed = [(t, r) for t, r in indexed if t is not None]
    indexed.sort(key=lambda x: x[0])
    time_strat = {}
    m = len(indexed)
    for qi, lab in enumerate(quartile_labels):
        if m == 0:
            time_strat[lab] = {"row_count": 0, "positive_rate": None, "ts_min": None, "ts_max": None}
            continue
        a = int(qi * m / 4)
        b = int((qi + 1) * m / 4) - 1 if qi < 3 else m - 1
        chunk = indexed[a : b + 1]
        nets_c = [parse_float(r.get("net_return", "")) for _, r in chunk]
        nets_c = [x for x in nets_c if x is not None]
        pos_c = sum(1 for x in nets_c if x > 0)
        time_strat[lab] = {
            "row_count": len(chunk),
            "positive_rate": round(pos_c / len(chunk), 6) if chunk else None,
            "ts_min": chunk[0][0] if chunk else None,
            "ts_max": chunk[-1][0] if chunk else None,
        }

    # entry_block quartiles
    blk_indexed = [(parse_int(r.get("entry_block", "")), r) for r in ok_rows]
    blk_indexed = [(b, r) for b, r in blk_indexed if b is not None]
    blk_indexed.sort(key=lambda x: x[0])
    m_b = len(blk_indexed)
    block_strat = {}
    for qi, lab in enumerate(quartile_labels):
        if m_b == 0:
            block_strat[lab] = {"row_count": 0, "positive_rate": None, "entry_block_min": None, "entry_block_max": None}
            continue
        a = int(qi * m_b / 4)
        b = int((qi + 1) * m_b / 4) - 1 if qi < 3 else m_b - 1
        chunk = blk_indexed[a : b + 1]
        nets_c = [parse_float(r.get("net_return", "")) for _, r in chunk]
        nets_c = [x for x in nets_c if x is not None]
        pos_c = sum(1 for x in nets_c if x > 0)
        block_strat[lab] = {
            "row_count": len(chunk),
            "positive_rate": round(pos_c / len(chunk), 6) if chunk else None,
            "entry_block_min": chunk[0][0] if chunk else None,
            "entry_block_max": chunk[-1][0] if chunk else None,
        }

    rates = list(pool_positive_rates.values())
    pool_spread = max(rates) - min(rates) if len(rates) >= 2 else 0.0
    tr = [time_strat[q]["positive_rate"] for q in quartile_labels if time_strat[q]["positive_rate"] is not None]
    time_spread = max(tr) - min(tr) if len(tr) >= 2 else 0.0
    br = [block_strat[q]["positive_rate"] for q in quartile_labels if block_strat[q]["positive_rate"] is not None]
    block_spread = max(br) - min(br) if len(br) >= 2 else 0.0

    # Narrative flags (thresholds: heuristic for audit doc)
    cost_driven = positive_rate_after_cost < 0.10 <= gross_positive_rate
    pool_driven = pool_spread >= 0.04  # ~4pp between two pools
    time_driven = time_spread >= 0.04
    globally_weak = (
        gross_positive_rate < 0.10
        or (max(gross_returns) if gross_returns else 0) < 0.02
    )

    design_notes = {
        "gross_return_target_future": (
            "Canonical label uses net after 0.006; gross_return could be a separate research target "
            "without changing this run."
        ),
        "lower_cost_assumption_future": "Sensitivity table shows cost strongly moves positive_rate; justify externally.",
        "horizon_future": (
            "Exit uses fixed 900 blocks (~1800s at 2s); mis-specified horizon would bias net_return — "
            "requires dedicated horizon sweep (new RPC job), not inferred here."
        ),
        "ranking_regression_future": "If binary rate stays low, ranking or regression on continuous return may help.",
    }

    # Primary conclusion enum (stacked heuristics)
    conclusions: list[str] = []
    if cost_driven:
        conclusions.append("AERO_2000_COST_ASSUMPTION_SENSITIVITY_REQUIRED")
    if pool_driven:
        conclusions.append("AERO_2000_POOL_STRATIFICATION_REQUIRED")
    if time_driven:
        conclusions.append("AERO_2000_TEMPORAL_STRATIFICATION_REQUIRED")
    # Horizon: we cannot prove from static CSV alone; flag if cost + pool + time all weak
    if not cost_driven and not pool_driven and not time_driven and positive_rate_after_cost < 0.10:
        conclusions.append("AERO_2000_HORIZON_SENSITIVITY_REQUIRED")
    if gross_positive_rate < 0.08 and pool_spread < 0.02 and time_spread < 0.02:
        conclusions.append("HOLD_DUE_TO_AERO_LOW_SIGNAL")

    if not conclusions:
        conclusions.append("HOLD_DUE_TO_AERO_LOW_SIGNAL")

    primary_conclusion = conclusions[0]

    audit = {
        "title": "Aero vAMM 2000 low positive-rate audit",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_csv": str(args.csv.relative_to(REPO)),
        "ok_row_count": n_ok,
        "canonical_round_trip_cost": CANONICAL_ROUND_TRIP,
        "positive_rate_after_cost": round(positive_rate_after_cost, 6),
        "after_cost_positive_count": after_cost_positive,
        "gross_positive_rate": round(gross_positive_rate, 6),
        "gross_positive_count": gross_positive,
        "gross_return_definition": "gross_return = net_return + canonical_round_trip_cost",
        "cost_sensitivity_positive_rate": cost_sensitivity,
        "pool_level": pool_stats,
        "net_return_distribution_by_pool": pool_net_dist,
        "positive_rate_by_timestamp_quartile": time_strat,
        "positive_rate_by_entry_block_quartile": block_strat,
        "spreads": {
            "pool_positive_rate_spread": round(pool_spread, 6),
            "timestamp_quartile_positive_rate_spread": round(time_spread, 6),
            "entry_block_quartile_positive_rate_spread": round(block_spread, 6),
        },
        "interpretation_flags": {
            "cost_driven_low_positive": cost_driven,
            "pool_driven_dispersion": pool_driven,
            "time_driven_dispersion": time_driven,
            "globally_weak_signal_heuristic": globally_weak,
        },
        "horizon_note": (
            "Static audit cannot validate 1800s exit; mean net_return negative with current horizon/cost — "
            "horizon sensitivity needs a new labeled experiment."
        ),
        "cost_conservatism_note": (
            f"At canonical cost {CANONICAL_ROUND_TRIP}, positive_rate={positive_rate_after_cost:.4f}; "
            f"at cost 0, positive_rate={cost_sensitivity['0.0']['positive_rate']:.4f}."
        ),
        "future_label_design_options": design_notes,
        "conclusion_enums_recommended": conclusions,
        "conclusion_enum_primary": primary_conclusion,
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    md = [
        "# Aero vAMM 2000 Low Positive-Rate Audit",
        "",
        f"- positive_rate after cost (net_return > 0): **{positive_rate_after_cost:.4f}**",
        f"- gross_positive_rate (gross_return > 0): **{gross_positive_rate:.4f}**",
        "",
        "## Cost sensitivity (positive if gross_return > cost)",
        "```json",
        json.dumps(cost_sensitivity, indent=2, sort_keys=True),
        "```",
        "",
        "## Pool-level positive_rate",
        "```json",
        json.dumps(pool_positive_rates, indent=2, sort_keys=True),
        "```",
        "",
        "## net_return distribution by pool",
        "```json",
        json.dumps(pool_net_dist, indent=2, sort_keys=True),
        "```",
        "",
        "## Timestamp quartiles",
        "```json",
        json.dumps(time_strat, indent=2, sort_keys=True),
        "```",
        "",
        "## Entry block quartiles",
        "```json",
        json.dumps(block_strat, indent=2, sort_keys=True),
        "```",
        "",
        "## Interpretation",
        f"- cost-driven (after < 10% gross >= 10%): **{cost_driven}**",
        f"- pool-driven (spread >= 4pp): **{pool_driven}**",
        f"- time-driven (quartile spread >= 4pp): **{time_driven}**",
        "",
        "## Conclusions (recommended)",
        ", ".join(f"`{c}`" for c in conclusions),
        "",
        f"**Primary:** `{primary_conclusion}`",
    ]
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(json.dumps({"wrote_json": str(args.out_json), "wrote_md": str(args.out_md)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
