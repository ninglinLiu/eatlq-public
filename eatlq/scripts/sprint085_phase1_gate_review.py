#!/usr/bin/env python3
"""Sprint085 Phase 1 gate review — trade-level dataset + rolling freeze windows."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint085 Phase 1 gate review")
    p.add_argument(
        "--dataset",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "copyability_safe_dataset_sample.csv"
        ),
    )
    p.add_argument(
        "--prep-flags",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "copyability_safe_prep_flags.json"
        ),
    )
    p.add_argument(
        "--builder-flags",
        default=(
            "mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset/"
            "decision_flags.json"
        ),
    )
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint085_wallet_conditional_entry/phase1_dataset",
    )
    p.add_argument("--min-freeze-windows", type=int, default=2)
    p.add_argument("--min-rows-per-window", type=int, default=100)
    return p.parse_args()


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def iso_week_key(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def top_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return max(c.values()) / len(values)


def exclude_top_n_still_positive(
    rows: list[dict], key: str, label_col: str, n: int = 10
) -> dict[str, Any]:
    vals = [as_float(r.get(label_col)) for r in rows]
    vals = [v for v in vals if v is not None]
    baseline_mean = sum(vals) / len(vals) if vals else None
    c = Counter(r.get(key, "") for r in rows if r.get(key))
    top_keys = {k for k, _ in c.most_common(n)}
    kept = [r for r in rows if r.get(key) not in top_keys]
    kept_vals = [as_float(r.get(label_col)) for r in kept]
    kept_vals = [v for v in kept_vals if v is not None]
    kept_mean = sum(kept_vals) / len(kept_vals) if kept_vals else None
    return {
        "excluded_top_n": n,
        "baseline_mean": baseline_mean,
        "kept_rows": len(kept),
        "kept_mean": kept_mean,
        "still_positive": kept_mean is not None and kept_mean > 0,
    }


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = REPO / args.dataset
    rows = load_csv(dataset_path)
    prep_flags = json.loads((REPO / args.prep_flags).read_text()) if (REPO / args.prep_flags).exists() else {}
    builder_flags = json.loads((REPO / args.builder_flags).read_text()) if (REPO / args.builder_flags).exists() else {}

    freeze_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        dt = parse_dt(r.get("entry_time", ""))
        if dt:
            freeze_counts[iso_week_key(dt)] += 1
    qualifying_windows = {
        w: c for w, c in freeze_counts.items() if c >= args.min_rows_per_window
    }

    delay_cols = ["copy_delay_60s", "copy_delay_120s", "copy_delay_30s"]
    delay_coverage: dict[str, float | None] = {}
    for col in delay_cols:
        non_null = sum(1 for r in rows if as_float(r.get(col)) is not None)
        delay_coverage[col] = (non_null / len(rows)) if rows else None

    closed_loop_rows = [r for r in rows if str(r.get("closed_loop_eligible", "")).lower() in ("true", "1", "yes")]
    failure_rows = [r for r in rows if as_float(r.get("copy_delay_120s")) is not None and as_float(r.get("copy_delay_120s")) <= 0]

    concentration = {
        "top_pool_share": top_share([r.get("pool", "") for r in rows]),
        "top_token_share": top_share([r.get("token", "") for r in rows]),
        "top_wallet_share": top_share([r.get("wallet", "") for r in rows]),
        "unique_pools": len({r.get("pool") for r in rows if r.get("pool")}),
        "unique_wallets": len({r.get("wallet") for r in rows if r.get("wallet")}),
    }

    exclude_checks = {
        "exclude_top10_pool": exclude_top_n_still_positive(rows, "pool", "copy_delay_120s", 10),
        "exclude_top10_token": exclude_top_n_still_positive(rows, "token", "copy_delay_120s", 10),
        "exclude_top10_wallet": exclude_top_n_still_positive(rows, "wallet", "copy_delay_120s", 10),
    }

    gates = {
        "dataset_rows_gt_0": len(rows) > 0,
        "rolling_freeze_windows_gte_min": len(qualifying_windows) >= args.min_freeze_windows,
        "delay_60s_coverage_gte_0_70": (delay_coverage.get("copy_delay_60s") or 0) >= 0.70,
        "delay_120s_coverage_gte_0_70": (delay_coverage.get("copy_delay_120s") or 0) >= 0.70,
        "closed_loop_subset_present": len(closed_loop_rows) > 0,
        "failure_samples_present": len(failure_rows) > 0,
        "failure_sample_rate_gte_0_20": (len(failure_rows) / len(rows) >= 0.20) if rows else False,
        "top_pool_share_lt_0_25": (concentration["top_pool_share"] or 1) < 0.25,
        "exclude_top10_pool_still_positive": exclude_checks["exclude_top10_pool"]["still_positive"],
        "exclude_top10_token_still_positive": exclude_checks["exclude_top10_token"]["still_positive"],
        "exclude_top10_wallet_still_positive": exclude_checks["exclude_top10_wallet"]["still_positive"],
        "copyability_safe_prep_pass": prep_flags.get("COPYABILITY_SAFE_PREP_PASS", False),
        "builder_leakage_pass": builder_flags.get("LEAKAGE_GUARD_PASS", False),
    }
    # copy_delay_300s not in builder v0 — tracked separately, not a Phase 1 hard gate
    delay_300s_present = any(r.get("copy_delay_300s") for r in rows)
    gates_review = {
        **gates,
        "delay_300s_present": delay_300s_present,
        "delay_300s_pending_builder_extension": not delay_300s_present,
    }
    failed = [k for k, ok in gates.items() if not ok]
    phase1_pass = len(failed) == 0

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "PHASE1_GATE_REVIEW_DONE": True,
        "PHASE1_PASS": phase1_pass,
        "row_count": len(rows),
        "closed_loop_rows": len(closed_loop_rows),
        "failure_rows_copy_delay_120s_le_zero": len(failure_rows),
        "failure_sample_rate": (len(failure_rows) / len(rows)) if rows else None,
        "freeze_windows_total": len(freeze_counts),
        "freeze_windows_qualifying": len(qualifying_windows),
        "freeze_window_counts": dict(sorted(freeze_counts.items())),
        "delay_coverage": delay_coverage,
        "concentration": concentration,
        "exclude_top10_sensitivity": exclude_checks,
        "gates": gates_review,
        "failed_gates": failed,
        "NEXT_STEP": "phase2_rolling_oos" if phase1_pass else "remain_research_fix_gates",
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
    }
    (out_dir / "phase1_gate_flags.json").write_text(json.dumps(flags, indent=2) + "\n", encoding="utf-8")

    md = [
        "# Sprint085 Phase 1 Gate Review",
        "",
        f"Generated: {flags['generated_at_utc']}",
        "",
        f"**PHASE1_PASS={phase1_pass}** — rows={len(rows)}, qualifying freeze windows={len(qualifying_windows)}",
        "",
        "## Freeze windows (ISO week)",
        "",
        "| week | rows | qualifies |",
        "|------|------|-----------|",
    ]
    for w, c in sorted(freeze_counts.items()):
        md.append(f"| {w} | {c} | {'yes' if c >= args.min_rows_per_window else 'no'} |")
    md.extend(
        [
            "",
            "## Delay label coverage",
            "",
        ]
    )
    for col, cov in delay_coverage.items():
        md.append(f"- `{col}`: {cov}")
    md.append("- `copy_delay_300s`: **not in builder v0** — pending builder extension")
    md.extend(
        [
            "",
            "## Concentration",
            "",
            f"- top_pool_share: {concentration['top_pool_share']}",
            f"- top_token_share: {concentration['top_token_share']}",
            f"- top_wallet_share: {concentration['top_wallet_share']}",
            "",
            "## Exclude top-10 sensitivity (copy_delay_120s mean)",
            "",
        ]
    )
    for name, chk in exclude_checks.items():
        md.append(
            f"- {name}: kept_rows={chk['kept_rows']}, kept_mean={chk['kept_mean']}, "
            f"still_positive={chk['still_positive']}"
        )
    md.extend(
        [
            "",
            "## Failed gates",
            "",
            ", ".join(failed) if failed else "(none)",
            "",
            "## Decision flags",
            "",
            "```json",
            json.dumps(flags, indent=2),
            "```",
        ]
    )
    (out_dir / "phase1_gate_readout.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"PHASE1_PASS": phase1_pass, "failed_gates": failed, "rows": len(rows)}, indent=2))
    return 0 if phase1_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
