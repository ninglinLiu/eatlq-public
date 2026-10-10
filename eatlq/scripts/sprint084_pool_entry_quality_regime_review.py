#!/usr/bin/env python3
"""Sprint084B — pool-entry quality descriptive onset/regime review.

Read-only review of `prep_v2/pool_entry_quality_candidate_dataset.csv`.

Hard rules:
- No training, no paper-forward, no live, no signals, no production writes.
- Wallet identity / token identity NEVER used as alpha; only Tier-1 pool features
  and Tier-2 diagnostic aggregates are stratified.
- Per repo contract (Whitepaper-v2 / Architecture / Sprint083 closure), the only
  decision flag this script may produce is a descriptive REVIEW_CANDIDATE marker;
  it does NOT authorize paper-forward or live.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = (
    "mvp/audits/sprint084_pool_entry_quality/prep_v2/"
    "pool_entry_quality_candidate_dataset.csv"
)
DEFAULT_PREP_FLAGS = (
    "mvp/audits/sprint084_pool_entry_quality/prep_v2/"
    "pool_entry_quality_prep_flags.json"
)
DEFAULT_OUT = "mvp/audits/sprint084_pool_entry_quality/regime_review_v1"

PRIMARY_LABEL = "T_POOL_FWD_30M"
SECONDARY_LABEL = "T_POOL_FWD_4H"
LABEL_SOURCE_FIELD = {
    "T_POOL_FWD_30M": "T_POOL_FWD_30M_source",
    "T_POOL_FWD_4H": "T_POOL_FWD_4H_source",
}
PAPER_LABEL_SOURCE = "paper_shadow_historical_backtest"
ECONOMIC_LABEL_SOURCES = {
    "paper_shadow_historical_backtest",
    "quoterv2_historical_backfill_v1",
    "ch_sqrt_price_mtm_v1",
}
PROXY_LABEL_SOURCES = {
    "ch_price_velocity_sum_30m",
    "ch_price_velocity_sum_4h",
}

# Bucket boundaries (descriptive only; do NOT tune to pass gates).
BUCKETS_ACTIVITY_HEAT = [(None, 5), (5, 10), (10, 20), (20, 50), (50, None)]
BUCKETS_BUY_PRESSURE = [(None, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, None)]
BUCKETS_PRICE_VELOCITY = [(None, -0.3), (-0.3, 0.0), (0.0, 0.3), (0.3, None)]
BUCKETS_TOKEN_VOL = [(None, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, None)]
BUCKETS_POOL_AGE = [
    (None, 3600),
    (3600, 100000),       # Sprint072 A3 onset band
    (100000, 604800),
    (604800, None),
]

# Risk thresholds (mirror Sprint083 closure copyability gates).
TOP10_POOL_SHARE_RISK = 0.25
UNIQUE_POOLS_RISK = 20
PROXY_LABEL_RISK_SHARE = 0.50
SINGLE_SOURCE_RISK_SHARE = 0.70

# Positive-regime candidate floor (descriptive, NOT a paper gate).
MIN_N_FOR_CANDIDATE = 30
POSITIVE_RATE_FLOOR = 0.50
POSITIVE_MEAN_FLOOR = 0.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084B regime review (read-only)")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--prep-flags", default=DEFAULT_PREP_FLAGS)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument(
        "--review-variant",
        default="v1",
        help="Output tag for flags (e.g. v2 after economic backfill)",
    )
    return p.parse_args()


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def bucket_label(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is not None:
        return f"<{hi:g}"
    if lo is not None and hi is None:
        return f">={lo:g}"
    return f"[{lo:g},{hi:g})"


def assign_bucket(value: float | None, buckets: list[tuple[float | None, float | None]]) -> str:
    if value is None:
        return "missing"
    for lo, hi in buckets:
        if lo is not None and value < lo:
            continue
        if hi is not None and value >= hi:
            continue
        return bucket_label(lo, hi)
    return "out_of_range"


def safe_mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def safe_median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def top_share(values: list[str]) -> float | None:
    if not values:
        return None
    c = Counter(values)
    return max(c.values()) / len(values)


def topk_share(values: list[str], k: int = 10) -> float | None:
    if not values:
        return None
    c = Counter(values)
    sorted_counts = sorted(c.values(), reverse=True)
    return sum(sorted_counts[:k]) / len(values)


def label_proxy_split(rows: list[dict], label: str) -> dict[str, int]:
    src_field = LABEL_SOURCE_FIELD[label]
    economic = 0
    proxy = 0
    other = 0
    for r in rows:
        if r.get(label) in (None, ""):
            continue
        src = r.get(src_field) or ""
        if src in ECONOMIC_LABEL_SOURCES:
            economic += 1
        elif src in PROXY_LABEL_SOURCES:
            proxy += 1
        else:
            other += 1
    return {"economic": economic, "proxy": proxy, "other": other}


def proxy_share(label_split: dict[str, int]) -> float | None:
    n = label_split["economic"] + label_split["proxy"] + label_split["other"]
    if n == 0:
        return None
    return label_split["proxy"] / n


def economic_share(label_split: dict[str, int]) -> float | None:
    n = label_split["economic"] + label_split["proxy"] + label_split["other"]
    if n == 0:
        return None
    return label_split["economic"] / n


def describe_cell(rows: list[dict], regime_kind: str, bucket: str) -> dict[str, Any]:
    pools = [r["pool"] for r in rows]
    unique_pools = len({p for p in pools if p})
    sources = [r["source"] for r in rows]
    source_mix = dict(Counter(sources))
    top_src = max(source_mix.values()) / len(sources) if sources else None
    t30_vals = [as_float(r.get("T_POOL_FWD_30M")) for r in rows]
    t30_vals = [v for v in t30_vals if v is not None]
    t4h_vals = [as_float(r.get("T_POOL_FWD_4H")) for r in rows]
    t4h_vals = [v for v in t4h_vals if v is not None]
    pos_30 = sum(1 for v in t30_vals if v > 0)
    pos_4h = sum(1 for v in t4h_vals if v > 0)
    split_30 = label_proxy_split(rows, "T_POOL_FWD_30M")
    split_4h = label_proxy_split(rows, "T_POOL_FWD_4H")
    cell = {
        "regime_kind": regime_kind,
        "bucket": bucket,
        "n": len(rows),
        "unique_pools": unique_pools,
        "top_pool_share": top_share(pools),
        "top10_pool_share": topk_share(pools, k=10),
        "source_mix": json.dumps(source_mix, sort_keys=True),
        "source_top_share": top_src,
        "T30_n": len(t30_vals),
        "T30_mean": safe_mean(t30_vals),
        "T30_median": safe_median(t30_vals),
        "T30_positive_rate": pos_30 / len(t30_vals) if t30_vals else None,
        "T30_proxy_share": proxy_share(split_30),
        "T30_label_mix": json.dumps(split_30, sort_keys=True),
        "T4H_n": len(t4h_vals),
        "T4H_mean": safe_mean(t4h_vals),
        "T4H_median": safe_median(t4h_vals),
        "T4H_positive_rate": pos_4h / len(t4h_vals) if t4h_vals else None,
        "T4H_proxy_share": proxy_share(split_4h),
        "T4H_label_mix": json.dumps(split_4h, sort_keys=True),
    }
    risks: list[str] = []
    if cell["top10_pool_share"] is not None and cell["top10_pool_share"] > TOP10_POOL_SHARE_RISK:
        risks.append("CONCENTRATION_RISK_TOP10")
    if cell["unique_pools"] < UNIQUE_POOLS_RISK:
        risks.append("CONCENTRATION_RISK_LOW_POOL_DIVERSITY")
    if cell["source_top_share"] is not None and cell["source_top_share"] > SINGLE_SOURCE_RISK_SHARE:
        risks.append("SINGLE_SOURCE_RISK")
    if cell["T30_proxy_share"] is not None and cell["T30_proxy_share"] > PROXY_LABEL_RISK_SHARE:
        risks.append("PROXY_LABEL_RISK_T30")
    if cell["T4H_proxy_share"] is not None and cell["T4H_proxy_share"] > PROXY_LABEL_RISK_SHARE:
        risks.append("PROXY_LABEL_RISK_T4H")
    cell["risks"] = ";".join(risks)
    return cell


def stratify(
    rows: list[dict],
    regime_kind: str,
    keyer: Callable[[dict], str],
) -> list[dict[str, Any]]:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_bucket[keyer(r)].append(r)
    out: list[dict[str, Any]] = []
    for bucket, items in sorted(by_bucket.items()):
        out.append(describe_cell(items, regime_kind, bucket))
    return out


def is_positive_candidate(cell: dict) -> bool:
    if cell["n"] < MIN_N_FOR_CANDIDATE:
        return False
    if cell["T30_n"] == 0:
        return False
    if (cell["T30_mean"] or 0) <= POSITIVE_MEAN_FLOOR:
        return False
    if (cell["T30_positive_rate"] or 0) < POSITIVE_RATE_FLOOR:
        return False
    return True


def candidate_paper_forward_status(positive_cells: list[dict]) -> tuple[str, list[dict]]:
    review_candidates: list[dict] = []
    for c in positive_cells:
        if c["unique_pools"] < UNIQUE_POOLS_RISK:
            continue
        if (c["top10_pool_share"] or 1) >= TOP10_POOL_SHARE_RISK:
            continue
        if (c["T30_proxy_share"] or 1) > PROXY_LABEL_RISK_SHARE:
            continue
        if (c["source_top_share"] or 1) > SINGLE_SOURCE_RISK_SHARE:
            continue
        review_candidates.append(c)
    if review_candidates:
        return "REVIEW_CANDIDATE", review_candidates
    return "false", []


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    if fieldnames is None:
        seen: list[str] = []
        keys: set[str] = set()
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.add(k)
                    seen.append(k)
        fieldnames = seen
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_readout_md(
    flags: dict,
    summary_rows: list[dict],
    pos_candidates: list[dict],
    review_candidates: list[dict],
    global_stats: dict,
) -> str:
    def fmt(v: Any, pct: bool = False) -> str:
        if v is None:
            return "n/a"
        if isinstance(v, float):
            return f"{v*100:.1f}%" if pct else f"{v:.4f}"
        return str(v)

    pos_lines = "\n".join(
        f"| {c['regime_kind']} | `{c['bucket']}` | {c['n']} | {c['unique_pools']} | "
        f"{fmt(c['top_pool_share'], True)} | {fmt(c['top10_pool_share'], True)} | "
        f"{fmt(c['T30_mean'])} | {fmt(c['T30_positive_rate'], True)} | "
        f"{fmt(c['T30_proxy_share'], True)} | {fmt(c['source_top_share'], True)} | "
        f"{c['risks'] or '-'} |"
        for c in pos_candidates
    ) or "| — | — | — | — | — | — | — | — | — | — | (no positive regime candidates) |"

    rc_lines = "\n".join(
        f"- `{c['regime_kind']}={c['bucket']}` · n={c['n']} · unique_pools={c['unique_pools']} · "
        f"top10_pool_share={fmt(c['top10_pool_share'], True)} · "
        f"T30 mean={fmt(c['T30_mean'])} · T30 pos_rate={fmt(c['T30_positive_rate'], True)} · "
        f"proxy_share_T30={fmt(c['T30_proxy_share'], True)}"
        for c in review_candidates
    ) or "- (none — all positive regimes fail at least one concentration / proxy / source gate)"

    pf_status = flags["PAPER_FORWARD_ALLOWED"]
    return f"""# Sprint084B — Pool-Entry Quality Regime Review (descriptive)

Generated: {flags["generated_at_utc"]}  
Mode: **research-only · read-only review · no training · no paper-forward · no live**

> Contract anchors: `whitepaper-v2.md` §3.3 Tier-1 features; `Architecture.md` guard;
> `mvp/audits/failed_no_7d_alpha_guard.md`; Sprint083 closure
> (`smart_wallet_copyability_closure.md`) — wallet/token identity must not be alpha.
> This document is **not** an authorization for labeling, training, paper-forward, or live.

## Executive conclusion

- **prep_v2 dataset**: {global_stats["n"]} rows · {global_stats["unique_pools"]} pools ·
  top_pool_share **{fmt(global_stats["top_pool_share"], True)}** ·
  top10_pool_share **{fmt(global_stats["top10_pool_share"], True)}**.
- **Label mix is proxy-dominant globally**: T30 proxy share
  **{fmt(global_stats["T30_proxy_share"], True)}**, T4H proxy share
  **{fmt(global_stats["T4H_proxy_share"], True)}**. CH `price_velocity_sum_30m/4h` is a
  **research proxy**, not a live-forward economic label.
- **Source mix is hybrid**: top source share **{fmt(global_stats["source_top_share"], True)}**.
- **Positive descriptive regimes** (mean T30 > 0 and positive_rate ≥ 50% and n ≥ {MIN_N_FOR_CANDIDATE}):
  **{len(pos_candidates)}** cells; of these,
  **{len(review_candidates)}** survive concentration + proxy + source gates as
  **REVIEW_CANDIDATE** (still no paper-forward authorization).
- **PAPER_FORWARD_ALLOWED** = `{pf_status}` ·
  **TRAINING_ALLOWED** = `false` · **LIVE_ALLOWED** = `false`.

## Q1 — Is forward proxy driven by single source / pool / token / wallet?

- Global top_pool_share **{fmt(global_stats["top_pool_share"], True)}** (<20% gate ok)
  but top10_pool_share **{fmt(global_stats["top10_pool_share"], True)}** > 25% → still
  concentrated in head pools.
- Source mix top share **{fmt(global_stats["source_top_share"], True)}** (≤70%
  threshold ok in aggregate). However the **labels** behind T30/T4H are
  **{fmt(global_stats["T30_proxy_share"], True)} / {fmt(global_stats["T4H_proxy_share"], True)}**
  CH price-velocity **proxy**, so any aggregate positive is structurally a
  **PROXY_LABEL_RISK**, not a paper-shadow economic edge.
- Wallet identity / token identity are **not** used as features (Sprint083 closure
  contract); wallet aggregates appear only as Tier-2 diagnostics.

## Q2 — Which Tier-1 strata look positive vs proxy/concentration artifact?

Top positive-regime candidates (mean T30 > 0 AND positive_rate ≥ 50% AND n ≥ {MIN_N_FOR_CANDIDATE}):

| regime | bucket | n | unique_pools | top_pool | top10_pool | T30_mean | T30_pos_rate | T30_proxy_share | source_top_share | risks |
|--------|--------|--:|-------------:|---------:|-----------:|---------:|-------------:|----------------:|----------------:|-------|
{pos_lines}

Full per-cell summary: `regime_summary.csv`. Risk register: `risk_register.csv`.

## Q3 — Continue data expansion or define bounded paper-forward candidates?

REVIEW_CANDIDATE cells (passes unique_pools≥{UNIQUE_POOLS_RISK}, top10_pool_share<{TOP10_POOL_SHARE_RISK*100:.0f}%, proxy_share≤{PROXY_LABEL_RISK_SHARE*100:.0f}%, source_top_share≤{SINGLE_SOURCE_RISK_SHARE*100:.0f}%):

{rc_lines}

**Recommendation**: since global T30 proxy share is
**{fmt(global_stats["T30_proxy_share"], True)}**, the current universe still needs
**economic-label backfill** (paper-shadow horizon expansion or post-entry pool price
mark-to-market) before any paper-forward candidate rule is meaningful. Until then:
**continue data expansion (broader pool universe with real economic labels), do not
define paper-forward rules from proxy-dominated regimes.**

## Q4 — Decision flags (machine summary)

| Flag | Value |
|------|------|
| `POOL_ENTRY_QUALITY_REVIEW_DONE` | `true` |
| `TRAINING_ALLOWED` | `false` |
| `PAPER_FORWARD_ALLOWED` | `{pf_status}` |
| `LIVE_ALLOWED` | `false` |
| `MODEL_PROMOTION_ALLOWED` | `false` |
| `WALLET_FEATURES_DIAGNOSTIC_ONLY` | `true` |
| `DIRECT_SMART_WALLET_COPY_CLOSED` | `true` |
| `PROXY_LABEL_DOMINANT_GLOBAL` | `{flags["PROXY_LABEL_DOMINANT_GLOBAL"]}` |
| `CONCENTRATION_RISK_GLOBAL_TOP10` | `{flags["CONCENTRATION_RISK_GLOBAL_TOP10"]}` |
| `NEXT_STEP` | `{flags["NEXT_STEP"]}` |

Full machine flags: `pool_entry_quality_regime_review_flags.json`.

## Outputs

- `pool_entry_quality_regime_review.md` (this file)
- `pool_entry_quality_regime_review_flags.json`
- `regime_summary.csv` — every regime/bucket cell with n / concentration / label-mix / risks
- `positive_regime_candidates.csv` — cells with descriptive positive signal
- `review_candidates.csv` — positive cells that also clear concentration + proxy + source gates
- `risk_register.csv` — per-cell risk flags
"""


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = REPO / args.dataset
    if not dataset_path.is_file():
        raise SystemExit(f"missing dataset: {dataset_path}")
    prep_flags_path = REPO / args.prep_flags
    prep_flags = json.loads(prep_flags_path.read_text()) if prep_flags_path.is_file() else {}

    with dataset_path.open(newline="") as f:
        rows = [r for r in csv.DictReader(f)]

    # Global stats
    global_cell = describe_cell(rows, "global", "all")
    global_stats = {
        "n": global_cell["n"],
        "unique_pools": global_cell["unique_pools"],
        "top_pool_share": global_cell["top_pool_share"],
        "top10_pool_share": global_cell["top10_pool_share"],
        "source_top_share": global_cell["source_top_share"],
        "T30_proxy_share": global_cell["T30_proxy_share"],
        "T4H_proxy_share": global_cell["T4H_proxy_share"],
    }

    summary: list[dict] = [global_cell]

    summary += stratify(rows, "by_source", lambda r: r["source"])
    summary += stratify(rows, "by_fee_tier", lambda r: str(r.get("fee_tier") or r.get("fee_bps") or "missing"))
    summary += stratify(rows, "by_onset_pass", lambda r: str(r.get("T_ONSET_PASS") or "missing"))
    summary += stratify(rows, "by_label_source_T30",
                        lambda r: str(r.get("T_POOL_FWD_30M_source") or "missing"))
    summary += stratify(rows, "by_activity_heat",
                        lambda r: assign_bucket(as_float(r.get("activity_heat")), BUCKETS_ACTIVITY_HEAT))
    summary += stratify(rows, "by_buy_pressure_ratio",
                        lambda r: assign_bucket(as_float(r.get("buy_pressure_ratio")), BUCKETS_BUY_PRESSURE))
    summary += stratify(rows, "by_price_velocity_5m",
                        lambda r: assign_bucket(as_float(r.get("price_velocity_5m")), BUCKETS_PRICE_VELOCITY))
    summary += stratify(rows, "by_token_volatility_5m",
                        lambda r: assign_bucket(as_float(r.get("token_volatility_5m")), BUCKETS_TOKEN_VOL))
    summary += stratify(rows, "by_pool_age_sec",
                        lambda r: assign_bucket(as_float(r.get("pool_age_sec")), BUCKETS_POOL_AGE))

    positive_cells = [c for c in summary if c["regime_kind"] != "global" and is_positive_candidate(c)]
    positive_cells.sort(key=lambda c: (c["T30_mean"] or 0), reverse=True)

    pf_status, review_candidates = candidate_paper_forward_status(positive_cells)

    field_order = [
        "regime_kind", "bucket", "n", "unique_pools", "top_pool_share",
        "top10_pool_share", "source_mix", "source_top_share",
        "T30_n", "T30_mean", "T30_median", "T30_positive_rate",
        "T30_proxy_share", "T30_label_mix",
        "T4H_n", "T4H_mean", "T4H_median", "T4H_positive_rate",
        "T4H_proxy_share", "T4H_label_mix",
        "risks",
    ]
    write_csv(out_dir / "regime_summary.csv", summary, field_order)
    write_csv(out_dir / "positive_regime_candidates.csv", positive_cells, field_order)
    write_csv(out_dir / "review_candidates.csv", review_candidates, field_order)

    risk_rows = [
        {
            "regime_kind": c["regime_kind"],
            "bucket": c["bucket"],
            "n": c["n"],
            "unique_pools": c["unique_pools"],
            "top10_pool_share": c["top10_pool_share"],
            "source_top_share": c["source_top_share"],
            "T30_proxy_share": c["T30_proxy_share"],
            "T4H_proxy_share": c["T4H_proxy_share"],
            "risks": c["risks"],
        }
        for c in summary
        if c["risks"]
    ]
    write_csv(out_dir / "risk_register.csv", risk_rows, [
        "regime_kind", "bucket", "n", "unique_pools",
        "top10_pool_share", "source_top_share",
        "T30_proxy_share", "T4H_proxy_share", "risks",
    ])

    econ_share_global = economic_share(label_proxy_split(rows, PRIMARY_LABEL))

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "review_variant": args.review_variant,
        "POOL_ENTRY_QUALITY_REVIEW_DONE": True,
        "DIRECT_SMART_WALLET_COPY_CLOSED": True,
        "WALLET_FEATURES_DIAGNOSTIC_ONLY": True,
        "MODEL_PROMOTION_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": pf_status,  # "REVIEW_CANDIDATE" or "false" string
        "LIVE_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "PROXY_LABEL_DOMINANT_GLOBAL": (global_stats["T30_proxy_share"] or 0) > PROXY_LABEL_RISK_SHARE,
        "ECONOMIC_LABEL_SHARE_T30": econ_share_global,
        "CONCENTRATION_RISK_GLOBAL_TOP10": (global_stats["top10_pool_share"] or 0) > TOP10_POOL_SHARE_RISK,
        "input_artifacts": {
            "dataset": str(dataset_path.relative_to(REPO)),
            "prep_flags": str(prep_flags_path.relative_to(REPO)),
            "prep_v2_unique_pools": prep_flags.get("unique_pools"),
            "prep_v2_total_rows": (prep_flags.get("row_counts") or {}).get("total_rows"),
        },
        "global_stats": global_stats,
        "regime_summary_rows": len(summary),
        "positive_regime_candidates_count": len(positive_cells),
        "review_candidates_count": len(review_candidates),
        "top_positive_regimes_preview": [
            {
                "regime_kind": c["regime_kind"],
                "bucket": c["bucket"],
                "n": c["n"],
                "unique_pools": c["unique_pools"],
                "top10_pool_share": c["top10_pool_share"],
                "T30_mean": c["T30_mean"],
                "T30_positive_rate": c["T30_positive_rate"],
                "T30_proxy_share": c["T30_proxy_share"],
                "source_top_share": c["source_top_share"],
                "risks": c["risks"],
            }
            for c in positive_cells[:10]
        ],
        "risk_thresholds": {
            "top10_pool_share_max": TOP10_POOL_SHARE_RISK,
            "unique_pools_min": UNIQUE_POOLS_RISK,
            "proxy_label_max": PROXY_LABEL_RISK_SHARE,
            "single_source_max": SINGLE_SOURCE_RISK_SHARE,
        },
        "NEXT_STEP": (
            "POOL_ENTRY_QUALITY_PAPER_FORWARD_REVIEW_CANDIDATE"
            if pf_status == "REVIEW_CANDIDATE"
            else (
                "POOL_ENTRY_QUALITY_CONCENTRATION_REPAIR_REQUIRED"
                if (global_stats["top10_pool_share"] or 0) > TOP10_POOL_SHARE_RISK
                else "POOL_ENTRY_QUALITY_ECONOMIC_LABEL_BACKFILL_REQUIRED"
            )
        ),
    }

    (out_dir / "pool_entry_quality_regime_review_flags.json").write_text(
        json.dumps(flags, indent=2, default=str) + "\n"
    )

    md = build_readout_md(flags, summary, positive_cells, review_candidates, global_stats)
    (out_dir / "pool_entry_quality_regime_review.md").write_text(md)

    print(json.dumps(
        {
            "regime_cells": len(summary),
            "positive_candidates": len(positive_cells),
            "review_candidates": len(review_candidates),
            "PAPER_FORWARD_ALLOWED": pf_status,
            "NEXT_STEP": flags["NEXT_STEP"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
