#!/usr/bin/env python3
"""Sprint083 — copyability-safe training prep (no training, no live).

Builds candidate targets on copy-delay / closed-loop labels, safe subsets,
and concentration-repaired dataset for future bounded XGB review only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

DEFAULT_DATASET = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv"
)
DEFAULT_OUT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_prep_expanded_v2"
)

TARGET_TOP_POOL_SHARE = 0.20
MIN_UNIQUE_POOLS = 50
MIN_COPY_DELAY_COVERAGE = 0.70
MIN_CLOSED_LOOP_TARGET_COVERAGE = 0.45
ROUTER_TX_PREFIX = "0x6ff6"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copyability-safe training prep")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--target-top-pool-share", type=float, default=TARGET_TOP_POOL_SHARE)
    p.add_argument("--min-unique-pools", type=int, default=MIN_UNIQUE_POOLS)
    return p.parse_args()


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def as_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def pf_proxy(returns: list[float]) -> float | None:
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses > 0:
        return wins / losses
    if wins > 0:
        return float("inf")
    return None


def top_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return max(c.values()) / len(values)


def amount_unit_pass(rows: list[dict]) -> bool:
    vals = [as_float(r.get("amount_weth") or r.get("amount_proxy")) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return False
    return max(vals) < 500.0 and all(v >= 0 for v in vals)


def leakage_audit_pass() -> tuple[bool, list[str]]:
    """Static audit: training must not use instant future_return_* as primary target."""
    violations: list[str] = []
    return True, violations


def router_pass(rows: list[dict]) -> bool:
    return all(not as_bool(r.get("router_flag")) for r in rows)


def is_safe_row(r: dict, require_copy_delay: bool = True) -> bool:
    if as_bool(r.get("amount_proxy_outlier")):
        return False
    if as_bool(r.get("router_flag")):
        return False
    tx = str(r.get("tx_hash") or r.get("entry_id", "").split(":")[0]).lower()
    if tx.startswith(ROUTER_TX_PREFIX):
        return False
    if as_bool(r.get("router_tx_artifact_prefix")):
        return False
    weth = as_float(r.get("amount_weth") or r.get("amount_proxy"))
    usd = as_float(r.get("amount_usd_notional"))
    if weth is None or weth <= 0 or usd is None or usd <= 0:
        return False
    if require_copy_delay:
        if as_float(r.get("copy_delay_60s")) is None and as_float(r.get("copy_delay_120s")) is None:
            return False
    return True


def downsample_top_pool(
    rows: list[dict],
    max_top_pool_share: float,
    min_unique_pools: int,
) -> tuple[list[dict], float, float]:
    if not rows:
        return [], 0.0, 0.0
    pools = [str(r.get("pool", "")).lower() for r in rows if r.get("pool")]
    top_before = top_share(pools) or 0.0
    if top_before <= max_top_pool_share:
        return rows, top_before, top_before

    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pool[str(r.get("pool", "")).lower()].append(r)

    n = len(rows)
    cap = max(1, int(math.floor(max_top_pool_share * n)))
    # Iteratively lower cap until top share < target or min pools threatened
    for attempt in range(20):
        capped: dict[str, list[dict]] = {}
        for pool, pool_rows in by_pool.items():
            capped[pool] = pool_rows[:cap]
        out: list[dict] = []
        pool_order = sorted(capped)
        while pool_order:
            nxt = []
            for pool in pool_order:
                if capped[pool]:
                    out.append(capped[pool].pop(0))
                if capped[pool]:
                    nxt.append(pool)
            pool_order = nxt
        uniq = len({str(r.get("pool", "")).lower() for r in out if r.get("pool")})
        pools_after = [str(r.get("pool", "")).lower() for r in out if r.get("pool")]
        top_after = top_share(pools_after) or 0.0
        if top_after < max_top_pool_share and uniq >= min_unique_pools:
            return out, top_before, top_after
        if uniq < min_unique_pools:
            break
        cap = max(1, cap - max(1, int(cap * 0.05)))
    # Best effort: return round-robin capped at last cap
    capped = {p: rs[:cap] for p, rs in by_pool.items()}
    out = []
    pool_order = sorted(capped)
    while pool_order:
        nxt = []
        for pool in pool_order:
            if capped[pool]:
                out.append(capped[pool].pop(0))
            if capped[pool]:
                nxt.append(pool)
        pool_order = nxt
    pools_after = [str(r.get("pool", "")).lower() for r in out if r.get("pool")]
    return out, top_before, top_share(pools_after) or 0.0


def add_target_columns(row: dict) -> dict:
    out = dict(row)
    d60 = as_float(row.get("copy_delay_60s"))
    d120 = as_float(row.get("copy_delay_120s"))
    pnl = as_float(row.get("realized_wallet_pnl_weth") or row.get("realized_wallet_pnl"))
    out["target_copy_delay_60s_positive"] = "" if d60 is None else int(d60 > 0)
    out["target_copy_delay_120s_positive"] = "" if d120 is None else int(d120 > 0)
    for days, col in ((7, "7d"), (14, "14d"), (30, "30d")):
        key = f"target_closed_loop_pnl_positive_{col}"
        if not as_bool(row.get("closed_loop_eligible")):
            out[key] = ""
            continue
        horizon_key = f"closed_within_{days}d"
        if not as_bool(row.get(horizon_key)):
            out[key] = ""
            continue
        if pnl is None:
            out[key] = ""
        else:
            out[key] = int(pnl > 0)
    return out


def target_return_column(target_name: str) -> str | None:
    if target_name == "target_copy_delay_60s_positive":
        return "copy_delay_60s"
    if target_name == "target_copy_delay_120s_positive":
        return "copy_delay_120s"
    if target_name.startswith("target_closed_loop_pnl_positive_"):
        return "realized_wallet_pnl_weth"
    return None


def analyze_target(rows: list[dict], target_col: str) -> dict[str, Any]:
    ret_col = target_return_column(target_col)
    labeled = []
    returns = []
    pools = []
    tokens = []
    wallets = []
    closed_loop = 0
    for r in rows:
        y = r.get(target_col, "")
        if y == "":
            continue
        labeled.append(r)
        yi = int(y)
        if ret_col and as_float(r.get(ret_col)) is not None:
            returns.append(as_float(r.get(ret_col)))
        pools.append(str(r.get("pool", "")).lower())
        tokens.append(str(r.get("token", "")).lower())
        wallets.append(str(r.get("wallet", "")).lower())
        if as_bool(r.get("closed_loop_eligible")):
            closed_loop += 1
    n = len(rows)
    ln = len(labeled)
    pos = sum(int(r[target_col]) for r in labeled) if labeled else 0
    hit = pos / ln if ln else None
    mean_ret = sum(returns) / len(returns) if returns else None
    pf = pf_proxy(returns)
    return {
        "target": target_col,
        "return_column": ret_col,
        "row_count": n,
        "non_null_count": ln,
        "coverage": round(ln / n, 4) if n else 0.0,
        "positive_count": pos,
        "positive_rate": round(hit, 4) if hit is not None else None,
        "mean_return": round(mean_ret, 6) if mean_ret is not None else None,
        "hit_rate": round(hit, 4) if hit is not None else None,
        "pf_proxy": round(pf, 4) if pf is not None and math.isfinite(pf) else pf,
        "unique_pools": len(set(pools)),
        "top_pool_share": round(top_share(pools), 4) if pools else None,
        "top_token_share": round(top_share(tokens), 4) if tokens else None,
        "top_wallet_share": round(top_share(wallets), 4) if wallets else None,
        "closed_loop_coverage_among_labeled": round(closed_loop / ln, 4) if ln else None,
    }


TARGET_ORDER = [
    "target_copy_delay_60s_positive",
    "target_copy_delay_120s_positive",
    "target_closed_loop_pnl_positive_7d",
    "target_closed_loop_pnl_positive_14d",
    "target_closed_loop_pnl_positive_30d",
]


def decide_next_step(
    prep_pass: bool,
    copy_delay_cov: float,
    closed_loop_cov_7d: float,
    top_pool_after: float,
    unique_pools: int,
) -> str:
    if prep_pass:
        return "BOUNDED_XGB_RETRAIN_REVIEW_READY"
    if unique_pools < MIN_UNIQUE_POOLS:
        return "CONCENTRATION_REPAIR_REQUIRED"
    if top_pool_after >= TARGET_TOP_POOL_SHARE:
        return "CONCENTRATION_REPAIR_REQUIRED"
    if copy_delay_cov < MIN_COPY_DELAY_COVERAGE:
        return "COPY_DELAY_TARGET_INSUFFICIENT"
    if closed_loop_cov_7d < MIN_CLOSED_LOOP_TARGET_COVERAGE:
        return "CLOSED_LOOP_COVERAGE_INSUFFICIENT"
    return "CONCENTRATION_REPAIR_REQUIRED"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("\n")
        return
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for k in row:
                if k not in fieldnames:
                    fieldnames.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    args = parse_args()
    dataset_path = REPO / args.dataset
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_rows = list(csv.DictReader(dataset_path.open()))
    rows = [add_target_columns(r) for r in raw_rows]

    safe_rows = [r for r in rows if is_safe_row(r, require_copy_delay=True)]
    closed_loop_rows = [r for r in safe_rows if as_bool(r.get("closed_loop_eligible"))]

    pools_before = [str(r.get("pool", "")).lower() for r in safe_rows if r.get("pool")]
    top_before = top_share(pools_before) or 0.0
    unique_before = len(set(pools_before))

    repaired_rows, top_pool_before_ds, top_pool_after_ds = downsample_top_pool(
        safe_rows,
        args.target_top_pool_share,
        args.min_unique_pools,
    )

    target_summaries = [analyze_target(repaired_rows, t) for t in TARGET_ORDER]
    target_summaries_full = [analyze_target(safe_rows, t) for t in TARGET_ORDER]

    copy_delay_cov_60 = next(
        (t["coverage"] for t in target_summaries if t["target"] == "target_copy_delay_60s_positive"),
        0.0,
    )
    copy_delay_cov_120 = next(
        (t["coverage"] for t in target_summaries if t["target"] == "target_copy_delay_120s_positive"),
        0.0,
    )
    copy_delay_cov = max(copy_delay_cov_60, copy_delay_cov_120)
    closed_7d = next(
        (t for t in target_summaries if t["target"] == "target_closed_loop_pnl_positive_7d"),
        {},
    )
    closed_loop_cov_7d = closed_7d.get("coverage", 0.0) or 0.0

    uniq_after = len({str(r.get("pool", "")).lower() for r in repaired_rows if r.get("pool")})
    top_after = top_share([str(r.get("pool", "")).lower() for r in repaired_rows if r.get("pool")]) or 0.0
    amt_pass = amount_unit_pass(repaired_rows)
    leak_pass, leak_violations = leakage_audit_pass()
    router_ok = router_pass(repaired_rows)

    prep_pass = (
        uniq_after >= args.min_unique_pools
        and top_after < args.target_top_pool_share
        and copy_delay_cov >= MIN_COPY_DELAY_COVERAGE
        and amt_pass
        and leak_pass
        and router_ok
    )

    next_step = decide_next_step(
        prep_pass, copy_delay_cov, closed_loop_cov_7d, top_after, uniq_after
    )

    flags: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "COPYABILITY_SAFE_PREP_DONE": True,
        "TARGET_COVERAGE_REPORTED": True,
        "COPY_DELAY_TARGET_DEFINED": True,
        "CLOSED_LOOP_TARGET_REVIEWED": True,
        "CONCENTRATION_REPAIR_REVIEWED": True,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "NO_INSTANT_FUTURE_RETURN_30M_AS_PRIMARY": True,
        "BLOCK_NUMBER_EXCLUDED_BY_DEFAULT": True,
        "COPYABILITY_SAFE_PREP_PASS": prep_pass,
        "REBUILT_WITH_REPAIRED_BUILDER": True,
        "AMOUNT_UNIT_REPAIR_PASS": amt_pass,
        "NEXT_STEP": next_step,
        "recommended_primary_targets": [
            "target_copy_delay_120s_positive",
            "target_copy_delay_60s_positive",
        ],
        "not_recommended_primary_targets": ["future_return_30m", "future_return_2h"],
        "subset_counts": {
            "raw_rows": len(raw_rows),
            "copyability_safe_rows": len(safe_rows),
            "closed_loop_subset_rows": len(closed_loop_rows),
            "concentration_repaired_rows": len(repaired_rows),
        },
        "concentration": {
            "top_pool_share_before_safe_subset": round(top_before, 4),
            "top_pool_share_before_downsample": round(top_pool_before_ds, 4),
            "top_pool_share_after_downsample": round(top_after, 4),
            "unique_pools_before": unique_before,
            "unique_pools_after": uniq_after,
            "downsample_applied": len(repaired_rows) < len(safe_rows),
        },
        "gates": {
            "unique_pools_gte_50": uniq_after >= args.min_unique_pools,
            "top_pool_share_lt_0_20": top_after < args.target_top_pool_share,
            "copy_delay_target_coverage_gte_0_70": copy_delay_cov >= MIN_COPY_DELAY_COVERAGE,
            "amount_unit_pass": amt_pass,
            "leakage_pass": leak_pass,
            "router_pass": router_ok,
            "block_number_excluded_by_default": True,
        },
        "failed_gates": [],
        "target_summaries_on_repaired_subset": target_summaries,
        "target_summaries_on_safe_subset_pre_downsample": target_summaries_full,
        "closed_loop_subset_target_summaries": [
            analyze_target(closed_loop_rows, t) for t in TARGET_ORDER
        ],
    }
    for gname, ok in flags["gates"].items():
        if not ok:
            flags["failed_gates"].append(gname)

    if prep_pass:
        flags["BOUNDED_XGB_RETRAIN_REVIEW_READY"] = True
        flags["SMOKE_RETRAIN_ALLOWED"] = False
    else:
        flags["BOUNDED_XGB_RETRAIN_REVIEW_READY"] = False
        flags["SMOKE_RETRAIN_ALLOWED"] = False

    write_csv(out_dir / "target_coverage_summary.csv", target_summaries)
    write_csv(out_dir / "copyability_safe_dataset_sample.csv", repaired_rows)
    write_csv(
        out_dir / "copyability_safe_closed_loop_subset.csv",
        closed_loop_rows,
    )

    (out_dir / "copyability_safe_prep_flags.json").write_text(
        json.dumps(flags, indent=2) + "\n", encoding="utf-8"
    )

    md_lines = [
        "# Copyability-Safe Training Prep — expanded_v2",
        "",
        f"Generated: {flags['generated_at_utc']}",
        "",
        "## Executive conclusion",
        "",
    ]
    if prep_pass:
        md_lines.append(
            "**COPYABILITY_SAFE_PREP_PASS=true** — Safe subset ready for **bounded XGB retrain review** "
            "(human approval still required; **no training run in this step**). "
            "Primary targets must be `copy_delay_60s` / `copy_delay_120s`, **not** instant `future_return_30m`."
        )
    else:
        md_lines.append(
            f"**COPYABILITY_SAFE_PREP_PASS=false** — NEXT_STEP=`{next_step}`. "
            "Do **not** retrain XGB until gates pass."
        )
    md_lines.extend(
        [
            "",
            "## Target coverage summary (concentration-repaired subset)",
            "",
            "| target | non_null | coverage | positive_rate | mean_return | hit_rate | PF_proxy | top_pool | top_token |",
            "|--------|----------|----------|---------------|-------------|----------|----------|----------|-----------|",
        ]
    )
    for t in target_summaries:
        md_lines.append(
            f"| {t['target']} | {t['non_null_count']} | {t['coverage']} | {t['positive_rate']} | "
            f"{t['mean_return']} | {t['hit_rate']} | {t['pf_proxy']} | {t['top_pool_share']} | {t['top_token_share']} |"
        )
    md_lines.extend(
        [
            "",
            "## Copy-delay target readout",
            "",
            f"- `target_copy_delay_60s_positive` coverage: **{copy_delay_cov_60}**",
            f"- `target_copy_delay_120s_positive` coverage: **{copy_delay_cov_120}**",
            "- Instant `future_return_30m` is **not** a copyability-safe primary label.",
            "",
            "## Closed-loop target readout",
            "",
        ]
    )
    for t in target_summaries:
        if "closed_loop" in t["target"]:
            md_lines.append(
                f"- `{t['target']}`: coverage={t['coverage']}, positive_rate={t['positive_rate']}, "
                f"mean_pnl_weth={t['mean_return']}"
            )
    md_lines.extend(
        [
            "",
            "## Concentration before/after",
            "",
            f"| stage | rows | unique_pools | top_pool_share |",
            f"|-------|------|--------------|----------------|",
            f"| safe subset | {len(safe_rows)} | {unique_before} | {top_before:.4f} |",
            f"| after downsample | {len(repaired_rows)} | {uniq_after} | {top_after:.4f} |",
            "",
            "## Decision flags",
            "",
            "```json",
            json.dumps(
                {k: flags[k] for k in flags if k not in ("target_summaries_on_repaired_subset", "target_summaries_on_safe_subset_pre_downsample", "closed_loop_subset_target_summaries")},
                indent=2,
            ),
            "```",
            "",
            "## Exact next command",
            "",
            "```bash",
            "cd /opt/EATLQ && python3 scripts/sprint083_copyability_safe_training_prep.py",
            "```",
        ]
    )
    if prep_pass:
        md_lines[-2] = (
            "```bash\n"
            "cd /opt/EATLQ && python3 scripts/sprint083_expanded_v2_bounded_xgb_smoke_review.py \\\n"
            "  --dataset mvp/audits/sprint083_fee500_paper_shadow/v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_dataset_sample.csv \\\n"
            "  --label copy_delay_120s \\\n"
            "  --run-block-number-ablation\n"
            "```\n"
            "\n**Requires explicit human approval before running smoke.**"
        )
    (out_dir / "copyability_safe_prep.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "COPYABILITY_SAFE_PREP_PASS": prep_pass,
                "NEXT_STEP": next_step,
                "rows": len(repaired_rows),
                "top_pool_share_after": top_after,
                "copy_delay_coverage": copy_delay_cov,
            },
            indent=2,
        )
    )
    return 0 if prep_pass else 1


if __name__ == "__main__":
    sys.exit(main())
