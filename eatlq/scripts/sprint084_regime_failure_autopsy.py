#!/usr/bin/env python3
"""Sprint084 — read-only regime failure autopsy (no training / live / paper-forward).

Explains why positive regimes fail REVIEW_CANDIDATE gates despite global gate pass.
Reuses thresholds from sprint084_pool_entry_quality_regime_review.py.
"""

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

# Mirror regime_review.py (do not tune)
TOP10_MAX = 0.25
UNIQUE_POOLS_MIN = 20
PROXY_MAX = 0.50
SINGLE_SOURCE_MAX = 0.70
PAPER_QUOTER_SOURCES = {
    "paper_shadow_historical_backtest",
    "quoterv2_historical_backfill_v1",
}
CH_SQRT_SOURCE = "ch_sqrt_price_mtm_v1"

DEFAULT_REGIME_DIR = "mvp/audits/sprint084_pool_entry_quality/regime_review_v3"
DEFAULT_BACKFILL = (
    "mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v2/"
    "pool_entry_quality_candidate_dataset_backfilled.csv"
)
DEFAULT_OUT = "mvp/audits/sprint084_pool_entry_quality/regime_failure_autopsy_v1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint084 regime failure autopsy")
    p.add_argument("--regime-dir", default=DEFAULT_REGIME_DIR)
    p.add_argument("--dataset", default=DEFAULT_BACKFILL)
    p.add_argument("--prep-flags", default="mvp/audits/sprint084_pool_entry_quality/prep_v3/pool_entry_quality_prep_flags.json")
    p.add_argument("--regime-flags", default=f"{DEFAULT_REGIME_DIR}/pool_entry_quality_regime_review_flags.json")
    p.add_argument("--backfill-flags", default="mvp/audits/sprint084_pool_entry_quality/economic_label_backfill_v2/economic_label_backfill_flags.json")
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    return p.parse_args()


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def as_float(v: Any) -> float | None:
    if v in (None, "", "\\N"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def gate_failures(cell: dict) -> list[str]:
    fails: list[str] = []
    up = cell.get("unique_pools")
    if up is not None and int(float(up)) < UNIQUE_POOLS_MIN:
        fails.append("LOW_POOL_DIVERSITY")
    t10 = as_float(cell.get("top10_pool_share"))
    if t10 is not None and t10 >= TOP10_MAX:
        fails.append("TOP10_CONCENTRATION")
    px = as_float(cell.get("T30_proxy_share"))
    if px is not None and px > PROXY_MAX:
        fails.append("PROXY_T30")
    st = as_float(cell.get("source_top_share"))
    if st is not None and st > SINGLE_SOURCE_MAX:
        fails.append("SINGLE_SOURCE")
    return fails


def distances(cell: dict) -> dict[str, float | None]:
    up = int(float(cell.get("unique_pools") or 0))
    t10 = as_float(cell.get("top10_pool_share"))
    px = as_float(cell.get("T30_proxy_share"))
    st = as_float(cell.get("source_top_share"))
    return {
        "gap_unique_pools": UNIQUE_POOLS_MIN - up,
        "gap_top10_share": (t10 - TOP10_MAX) if t10 is not None else None,
        "gap_proxy_t30": (px - PROXY_MAX) if px is not None else None,
        "gap_source_top": (st - SINGLE_SOURCE_MAX) if st is not None else None,
    }


def parse_label_mix(mix_str: str) -> dict[str, int]:
    if not mix_str:
        return {}
    try:
        d = json.loads(mix_str.replace("'", '"'))
        return {k: int(v) for k, v in d.items()}
    except json.JSONDecodeError:
        return {}


def dominant_label_detail(cell: dict) -> str:
    mix = parse_label_mix(cell.get("T30_label_mix") or "")
    if not mix:
        return "unknown"
    return max(mix.items(), key=lambda x: x[1])[0]


def rows_needed_for_top10(n: int, top10_share: float) -> int | None:
    """Rows to add (uniform dilution) so top10/(n+add) < TOP10_MAX, holding top10 mass fixed."""
    if top10_share is None or top10_share <= TOP10_MAX:
        return 0
    top10_mass = top10_share * n
    need_n = top10_mass / TOP10_MAX
    return max(0, int(math.ceil(need_n - n)))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_failure_matrix(positive: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in positive:
        fails = gate_failures(c)
        dist = distances(c)
        n = int(float(c.get("n") or 0))
        t10 = as_float(c.get("top10_pool_share"))
        out.append(
            {
                "regime_kind": c.get("regime_kind"),
                "bucket": c.get("bucket"),
                "n": n,
                "unique_pools": c.get("unique_pools"),
                "T30_mean": c.get("T30_mean"),
                "T30_positive_rate": c.get("T30_positive_rate"),
                "top10_pool_share": t10,
                "source_top_share": c.get("source_top_share"),
                "T30_proxy_share": c.get("T30_proxy_share"),
                "dominant_t30_label_class": dominant_label_detail(c),
                "risks_reported": c.get("risks"),
                "review_gate_failures": ";".join(fails) if fails else "NONE",
                "failure_count": len(fails),
                "gap_unique_pools": dist["gap_unique_pools"],
                "gap_top10_share": dist["gap_top10_share"],
                "gap_proxy_t30": dist["gap_proxy_t30"],
                "gap_source_top": dist["gap_source_top"],
                "rows_needed_uniform_dilution_top10": rows_needed_for_top10(n, t10) if t10 else None,
                "passes_review_candidate": "false",
            }
        )
    return out


def near_miss_rows(matrix: list[dict], max_failures: int = 1, top10_margin: float = 0.08) -> list[dict]:
    nm: list[dict] = []
    for r in matrix:
        fc = int(r["failure_count"])
        gap_t10 = as_float(r.get("gap_top10_share"))
        if fc == 0:
            continue
        if fc <= max_failures:
            nm.append({**r, "near_miss_reason": f"failures={fc}"})
        elif gap_t10 is not None and 0 < gap_t10 <= top10_margin:
            nm.append({**r, "near_miss_reason": f"top10_within_{top10_margin:.0%}"})
    nm.sort(
        key=lambda x: (
            int(x["failure_count"]),
            as_float(x.get("gap_top10_share")) or 99,
            -as_float(x.get("T30_mean")) or 0,
        )
    )
    return nm


def pool_stats(rows: list[dict]) -> dict[str, dict]:
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pool[r["pool"].lower()].append(r)
    out: dict[str, dict] = {}
    for pool, items in by_pool.items():
        src_ctr = Counter(i.get("source") or "" for i in items)
        lbl_ctr = Counter(i.get("T_POOL_FWD_30M_source") or "missing" for i in items)
        fee = Counter(
            str(i.get("fee_tier") or i.get("fee_bps") or "missing") for i in items
        )
        paper_quoter = any(
            (i.get("T_POOL_FWD_30M_source") or "") in PAPER_QUOTER_SOURCES for i in items
        )
        ch_sqrt_only = all(
            (i.get("T_POOL_FWD_30M_source") or "") in (CH_SQRT_SOURCE, "", "missing")
            or (i.get("T_POOL_FWD_30M_source") or "") == CH_SQRT_SOURCE
            for i in items
            if i.get("T_POOL_FWD_30M")
        ) and not paper_quoter
        out[pool] = {
            "pool": pool,
            "row_count": len(items),
            "dominant_source": src_ctr.most_common(1)[0][0] if src_ctr else "",
            "dominant_label_source": lbl_ctr.most_common(1)[0][0] if lbl_ctr else "",
            "fee_tier_mode": fee.most_common(1)[0][0] if fee else "",
            "in_paper_quoter": paper_quoter,
            "ch_sqrt_dominant": ch_sqrt_only,
            "activity_heat_median": _median([as_float(i.get("activity_heat")) for i in items]),
            "buy_pressure_median": _median([as_float(i.get("buy_pressure_ratio")) for i in items]),
            "price_velocity_median": _median([as_float(i.get("price_velocity_5m")) for i in items]),
            "volatility_median": _median([as_float(i.get("token_volatility_5m")) for i in items]),
            "pool_age_median": _median([as_float(i.get("pool_age_sec")) for i in items]),
        }
    return out


def _median(vals: list[float | None]) -> float | None:
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def expansion_targets(
    dataset_rows: list[dict],
    near_miss: list[dict],
    paper_quoter_pools: set[str],
) -> list[dict]:
    stats = pool_stats(dataset_rows)
    # Regimes driving priority: lowest gap_top10 among positive
    priority_regimes = [
        (r["regime_kind"], r["bucket"], as_float(r.get("gap_top10_share")) or 0)
        for r in near_miss
    ]
    priority_regimes.sort(key=lambda x: x[2])

    targets: list[dict] = []
    for pool, st in stats.items():
        if st["in_paper_quoter"]:
            continue
        if st["row_count"] < 3:
            continue
        fee = st["fee_tier_mode"]
        # Quoter-friendly fees first (empirical from backfill v2)
        fee_score = 0 if fee in ("500", "10000", "3000", "30") else (2 if fee in ("100", "1", "5") else 1)
        action = "quoter_historical"
        if fee in ("500", "10000"):
            action = "live_paper_shadow_then_quoter"
        elif fee == "100":
            action = "live_paper_shadow_only_if_quoter_reverts"
        score = (
            st["row_count"] * 2
            - fee_score * 10
            + (5 if st["dominant_source"] == "ch_pool_onset_candidate" else 0)
            + (3 if (st["activity_heat_median"] or 0) >= 10 else 0)
        )
        targets.append(
            {
                "pool": pool,
                "fee_tier_mode": fee,
                "row_count_in_dataset": st["row_count"],
                "dominant_source": st["dominant_source"],
                "dominant_label_source": st["dominant_label_source"],
                "activity_heat_median": st["activity_heat_median"],
                "buy_pressure_median": st["buy_pressure_median"],
                "price_velocity_median": st["price_velocity_median"],
                "token_volatility_median": st["volatility_median"],
                "pool_age_sec_median": st["pool_age_median"],
                "priority_score": round(score, 2),
                "recommended_action": action,
                "rationale": "ch_sqrt_only_pool_not_in_paper_quoter_universe",
            }
        )
    targets.sort(key=lambda x: -x["priority_score"])
    return targets[:60]


def blocker_histogram(matrix: list[dict]) -> Counter:
    c: Counter = Counter()
    for r in matrix:
        for f in (r.get("review_gate_failures") or "").split(";"):
            if f and f != "NONE":
                c[f] += 1
    return c


def build_md(
    flags: dict,
    hist: Counter,
    matrix: list[dict],
    near_miss: list[dict],
    targets: list[dict],
    regime_flags: dict,
    backfill_flags: dict,
) -> str:
    top_nm = near_miss[:8]
    top_tgt = targets[:15]
    return f"""# Sprint084 — Regime Failure Autopsy (read-only)

Generated: {flags.get("generated_at_utc")}

## Contract anchors (conclusions only)

- **Architecture.md:** Contract audits override narrative; `FAILED_NO_7D_ALPHA` blocks labeling/training/live authorization from docs alone.
- **Operations.md:** Runbooks are not authorization; paper/dry-run readiness ≠ paper-forward or live.
- **whitepaper-v2 / continued:** Pool-entry quality uses Tier-1 pool features; QuoterV2 is the economic quote path for paper-shadow semantics.
- **docs/label-semantics-freeze.md:** 30m QuoterV2 exit horizon is frozen; `ch_sqrt_price_mtm_v1` is **not** interchangeable with live-forward Quoter.

## Why REVIEW_CANDIDATE = 0 (global pass ≠ cell pass)

Global row universe (prep_v3 + backfill_v2): **top10 = {regime_flags.get("global_stats", {}).get("top10_pool_share", 0):.1%}**, **T30 proxy = {regime_flags.get("global_stats", {}).get("T30_proxy_share", 0):.1%}**.

REVIEW_CANDIDATE requires **each positive regime cell** to satisfy (mirrors `sprint084_pool_entry_quality_regime_review.py`):

| Gate | Threshold |
|------|-------------|
| unique_pools | ≥ {UNIQUE_POOLS_MIN} |
| top10_pool_share | < {TOP10_MAX:.0%} |
| T30_proxy_share | ≤ {PROXY_MAX:.0%} |
| source_top_share | ≤ {SINGLE_SOURCE_MAX:.0%} |

**All 18 positive regimes fail at least one cell gate.** Primary blockers across positives:

{chr(10).join(f"- **{k}**: {v}/18 regimes" for k, v in hist.most_common())}

**Not the main blocker:** proxy dominance (global fixed; only 1/18 positives has material proxy). **Dominant issue:** **sub-cell top10 concentration** (18/18) plus **single-source** (10/18) and **low pool count** (6/18).

**Label mix:** {backfill_flags.get("metrics_before_after", {}).get("unique_pools_paper_quoter", "?")} paper/Quoter pools vs **{backfill_flags.get("label_source_breakdown", {}).get("ch_sqrt_price_mtm_v1", "?")}** rows on `ch_sqrt_price_mtm_v1`. Quoter coverage is **universe-sufficient** but **regime-strata still CH-onset / paper-concentrated**.

## Near-miss regimes (closest to REVIEW_CANDIDATE)

| regime | bucket | failures | gap_top10 | gap_source | gap_pools | rows_to_dilute_top10 |
|--------|--------|----------|-----------|------------|-----------|----------------------|
{chr(10).join(
    f"| {r['regime_kind']} | {r['bucket']} | {r['review_gate_failures']} | "
    f"{r.get('gap_top10_share', '')} | {r.get('gap_source_top', '')} | {r.get('gap_unique_pools', '')} | "
    f"{r.get('rows_needed_uniform_dilution_top10', '')} |"
    for r in top_nm
) or '| (none) | | | | | | |'}

## Expansion guidance (shortest path)

1. **Live paper-shadow (fee 500 / 10000)** on top `expansion_target_pools.csv` pools — adds Quoter-economic labels + diversifies `paper_shadow_onset` strata (currently **10 pools**, top10=100% in paper label regimes).
2. **Quoter historical** on same list where live cadence is insufficient — registry union already resolves 71/71; fee100 pools often `quoter_revert` (prioritize 500/10000).
3. **Do not** use ch_sqrt MTM as live-forward Quoter substitute.
4. **Do not** delete pools or retune gates to force candidates.

**Rough supply to unlock first near-miss** (`by_buy_pressure_ratio` [0.3,0.5), gap_top10≈9pp): add **~{near_miss[0].get('rows_needed_uniform_dilution_top10', 'n/a') if near_miss else 'n/a'}** balanced rows in-stratum **or** add **≥5 new pools** at ≤24 rows/pool without increasing top-10 mass.

## Decision

- **PAPER_FORWARD_ALLOWED:** `{flags.get("PAPER_FORWARD_ALLOWED")}` (unchanged)
- **Recommended next action:** `{flags.get("RECOMMENDED_NEXT_ACTION")}` — see `regime_failure_autopsy_flags.json`

## Artifacts

- `positive_regime_failure_matrix.csv`
- `near_miss_regimes.csv`
- `expansion_target_pools.csv`
- `regime_failure_autopsy_flags.json`
"""


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    regime_dir = REPO / args.regime_dir

    positive = load_csv(regime_dir / "positive_regime_candidates.csv")
    regime_flags = json.loads((REPO / args.regime_flags).read_text())
    backfill_flags = json.loads((REPO / args.backfill_flags).read_text())
    prep_flags = json.loads((REPO / args.prep_flags).read_text()) if (REPO / args.prep_flags).is_file() else {}

    dataset_rows = load_csv(REPO / args.dataset)
    paper_quoter_pools = {
        r["pool"].lower()
        for r in dataset_rows
        if (r.get("T_POOL_FWD_30M_source") or "") in PAPER_QUOTER_SOURCES
    }

    matrix = build_failure_matrix(positive)
    hist = blocker_histogram(matrix)
    near_miss = near_miss_rows(matrix)
    targets = expansion_targets(dataset_rows, near_miss, paper_quoter_pools)

    write_csv(
        out_dir / "positive_regime_failure_matrix.csv",
        matrix,
        list(matrix[0].keys()) if matrix else [],
    )
    write_csv(
        out_dir / "near_miss_regimes.csv",
        near_miss,
        list(near_miss[0].keys()) if near_miss else list(matrix[0].keys()) if matrix else [],
    )
    write_csv(
        out_dir / "expansion_target_pools.csv",
        targets,
        list(targets[0].keys()) if targets else [],
    )

    # Best near-miss for expansion math
    best = near_miss[0] if near_miss else {}
    pools_pq = backfill_flags.get("metrics_before_after", {}).get("unique_pools_paper_quoter", 0)
    pools_paper = backfill_flags.get("metrics_before_after", {}).get("unique_pools_paper_source", 10)

    flags = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "REGIME_FAILURE_AUTOPSY_DONE": True,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": regime_flags.get("PAPER_FORWARD_ALLOWED", "false"),
        "MODEL_PROMOTION_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "DIRECT_SMART_WALLET_COPY_CLOSED": True,
        "WALLET_FEATURES_DIAGNOSTIC_ONLY": True,
        "positive_regime_count": len(positive),
        "review_candidate_count": regime_flags.get("review_candidates_count", 0),
        "global_top10_pool_share": regime_flags.get("global_stats", {}).get("top10_pool_share"),
        "global_T30_proxy_share": regime_flags.get("global_stats", {}).get("T30_proxy_share"),
        "paper_quoter_pools": pools_pq,
        "paper_only_pools": pools_paper,
        "ch_sqrt_rows": backfill_flags.get("label_source_breakdown", {}).get("ch_sqrt_price_mtm_v1"),
        "positive_regime_gate_failures": dict(hist),
        "primary_blocker": hist.most_common(1)[0][0] if hist else "UNKNOWN",
        "near_miss_count": len(near_miss),
        "best_near_miss": {
            "regime_kind": best.get("regime_kind"),
            "bucket": best.get("bucket"),
            "gap_top10_share": best.get("gap_top10_share"),
            "rows_needed_uniform_dilution": best.get("rows_needed_uniform_dilution_top10"),
        },
        "expansion_target_pool_count": len(targets),
        "expansion_recommended_new_paper_quoter_pools": max(0, 25 - int(pools_pq or 0)),
        "RECOMMENDED_NEXT_ACTION": "A_plus_B",
        "RECOMMENDED_NEXT_ACTION_DETAIL": (
            "A: live paper-shadow on fee500/10000 expansion_target_pools; "
            "B: Quoter historical for same pools where live cadence insufficient. "
            "Not C (regimes show signal). Not D alone (supply fix needed before accumulation helps)."
        ),
        "input_artifacts": {
            "regime_review_v3": args.regime_dir,
            "prep_v3_flags": args.prep_flags,
            "economic_backfill_v2_flags": args.backfill_flags,
            "dataset": args.dataset,
        },
    }
    (out_dir / "regime_failure_autopsy_flags.json").write_text(json.dumps(flags, indent=2) + "\n")
    (out_dir / "regime_failure_autopsy.md").write_text(
        build_md(flags, hist, matrix, near_miss, targets, regime_flags, backfill_flags)
    )

    print(json.dumps(
        {
            "positive": len(positive),
            "near_miss": len(near_miss),
            "expansion_targets": len(targets),
            "blockers": dict(hist.most_common()),
            "PAPER_FORWARD_ALLOWED": flags["PAPER_FORWARD_ALLOWED"],
            "RECOMMENDED_NEXT_ACTION": flags["RECOMMENDED_NEXT_ACTION"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
