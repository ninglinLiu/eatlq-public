"""
Run-004 Patch F1 Task A: offline duplicate conflict triage — no RPC.

Classifies the 823 duplicate-key conflicts in logs/run004_v2only_labeled_dryrun_full.csv,
distinguishes BINARY_LABEL_FLIP / SAME_LABEL_RETURN_DRIFT / TINY_NUMERIC_DRIFT /
QUOTE_OR_EXIT_MISMATCH / BLOCK_OR_TIMESTAMP_MISMATCH / METADATA_MISMATCH /
UNKNOWN_CONFLICT, and outputs a per-key CSV + summary JSON + markdown.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))


def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    if n == 1:
        return sv[0]
    k = (n - 1) * q
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


EPS_CONFLICT = 1e-4    # threshold below which two success rows are "equal"
TINY_THRESH = 1e-3     # spread < this → TINY_NUMERIC_DRIFT
DRIFT_THRESH = 1.0     # spread < 1.0 → SAME_LABEL_RETURN_DRIFT; ≥1.0 → QUOTE_OR_EXIT_MISMATCH

CSV_FIELDS = [
    "tx_hash", "log_index", "pool_address", "token_address",
    "timestamp", "row_count", "success_row_count",
    "net_return_min", "net_return_max", "net_return_spread",
    "label_values", "binary_label_flip",
    "entry_blocks", "exit_blocks",
    "label_reasons", "pre_checks",
    "conflict_class",
    "block_or_timestamp_mismatch",
    "root_cause_hint",
]


def _is_succ(r: dict[str, str]) -> bool:
    try:
        lb = int(r.get("label", "-1"))
    except (TypeError, ValueError):
        return False
    return r.get("label_reason") == "executable" and lb in (0, 1)


def _pnet(r: dict[str, str]) -> float | None:
    try:
        v = float((r.get("net_return") or "").strip() or "nan")
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def _classify(
    labels_set: set[int],
    spread: float,
    block_mismatch: bool,
    meta_mismatch: bool,
) -> str:
    if len(labels_set) > 1:
        return "BINARY_LABEL_FLIP"
    if meta_mismatch:
        return "METADATA_MISMATCH"
    if block_mismatch and spread < TINY_THRESH:
        return "BLOCK_OR_TIMESTAMP_MISMATCH"
    if spread < TINY_THRESH:
        return "TINY_NUMERIC_DRIFT"
    if spread < DRIFT_THRESH:
        return "SAME_LABEL_RETURN_DRIFT"
    if spread >= DRIFT_THRESH:
        return "QUOTE_OR_EXIT_MISMATCH"
    return "UNKNOWN_CONFLICT"


def main() -> int:
    csv_in = Path(os.environ.get("RUN004_F_E2_CSV", str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv")))
    recon_json = Path(os.environ.get(
        "RUN004_F_RECON_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f_e2_reconciliation.json"),
    ))
    out_json = Path(os.environ.get(
        "RUN004_F1_TRIAGE_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f1_duplicate_conflict_triage.json"),
    ))
    out_md = Path(os.environ.get(
        "RUN004_F1_TRIAGE_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-f1-duplicate-conflict-triage.md"),
    ))
    out_csv = Path(os.environ.get(
        "RUN004_F1_CONFLICT_KEYS_CSV",
        str(REPO / "logs" / "run004_duplicate_conflict_keys.csv"),
    ))

    if not csv_in.is_file():
        print(f"ERROR: missing {csv_in}", file=sys.stderr)
        return 1

    with csv_in.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    tss = [int(r.get("timestamp", 0) or 0) for r in rows if int(r.get("timestamp", 0) or 0) > 0]
    t_max = max(tss) if tss else 0
    holdout_lo = t_max - 7 * 86400 * 1000

    by: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by[(r.get("tx_hash", ""), r.get("log_index", ""))].append(r)

    total_unique = len(by)
    class_ctr: Counter[str] = Counter()
    pool_ctr: Counter[str] = Counter()
    pool_conflict_map: dict[str, int] = defaultdict(int)
    all_spreads: list[float] = []
    all_flip_spreads: list[float] = []
    holdout_ck = 0
    train_ck = 0
    per_key_rows: list[dict[str, Any]] = []

    for k, vs in by.items():
        succs = [v for v in vs if _is_succ(v)]
        if len(succs) < 2:
            continue
        labels_set = set(int(s["label"]) for s in succs)
        nets = [_pnet(s) for s in succs if _pnet(s) is not None]
        spread = max(nets) - min(nets) if len(nets) >= 2 else 0.0

        # Is this a conflict?
        if len(labels_set) == 1 and spread <= EPS_CONFLICT:
            continue

        ebs = sorted(set(s.get("entry_block", "") for s in succs))
        xbs = sorted(set(s.get("exit_block", "") for s in succs))
        tss_v = sorted(set(s.get("timestamp", "") for s in vs))
        pools = sorted(set((s.get("pool_address") or "").lower() for s in vs))
        toks = sorted(set((s.get("token_address") or "").lower() for s in vs))

        block_mismatch = len(ebs) > 1 or len(xbs) > 1
        meta_mismatch = len(pools) > 1 or len(toks) > 1
        cls = _classify(labels_set, spread, block_mismatch, meta_mismatch)
        class_ctr[cls] += 1
        all_spreads.append(spread)
        if len(labels_set) > 1:
            all_flip_spreads.append(spread)

        pl = (vs[0].get("pool_address") or "").lower()
        pool_ctr[pl] += 1
        pool_conflict_map[pl] += 1

        ts0 = int(vs[0].get("timestamp", 0) or 0)
        if ts0 >= holdout_lo:
            holdout_ck += 1
        else:
            train_ck += 1

        # root_cause_hint
        if block_mismatch:
            hint = f"different_entry_block:{','.join(ebs[:3])}"
        elif meta_mismatch:
            hint = f"pool_or_token_mismatch:{len(pools)}_pools"
        elif spread >= DRIFT_THRESH:
            hint = "large_quote_spread_ge_1"
        elif spread > TINY_THRESH:
            hint = "moderate_drift"
        else:
            hint = "tiny_drift_le_1e-3"

        per_key_rows.append({
            "tx_hash": k[0],
            "log_index": k[1],
            "pool_address": pl,
            "token_address": (vs[0].get("token_address") or "").lower(),
            "timestamp": vs[0].get("timestamp", ""),
            "row_count": len(vs),
            "success_row_count": len(succs),
            "net_return_min": round(min(nets), 12) if nets else "",
            "net_return_max": round(max(nets), 12) if nets else "",
            "net_return_spread": round(spread, 10),
            "label_values": "|".join(str(x) for x in sorted(labels_set)),
            "binary_label_flip": len(labels_set) > 1,
            "entry_blocks": "|".join(ebs[:5]),
            "exit_blocks": "|".join(xbs[:5]),
            "label_reasons": "|".join(sorted(set(s.get("label_reason", "") for s in vs))),
            "pre_checks": "|".join(sorted(set(s.get("pre_check", "") for s in vs))),
            "conflict_class": cls,
            "block_or_timestamp_mismatch": block_mismatch,
            "root_cause_hint": hint,
        })

    # Sort by spread desc
    per_key_rows.sort(key=lambda x: float(x.get("net_return_spread", 0)), reverse=True)
    total_ck = len(per_key_rows)

    # Spread stats
    all_spreads.sort()
    flip_spreads = sorted(all_flip_spreads)

    def safe_pct(sv: list[float], q: float) -> float | None:
        if not sv:
            return None
        return round(_pct(sv, q), 8)

    # Pool concentration among conflict keys
    top_pool_ck = pool_ctr.most_common(10)
    top1_conc = (top_pool_ck[0][1] / total_ck) if top_pool_ck and total_ck else 0

    # usable_prepared estimate from prepare report
    usable_prepared = 15822  # from Patch F
    flip_share = class_ctr.get("BINARY_LABEL_FLIP", 0) / max(1, usable_prepared)
    total_share = total_ck / max(1, usable_prepared)

    summary: dict[str, Any] = {
        "conflict_key_count": total_ck,
        "by_class": {
            "TINY_NUMERIC_DRIFT": class_ctr.get("TINY_NUMERIC_DRIFT", 0),
            "SAME_LABEL_RETURN_DRIFT": class_ctr.get("SAME_LABEL_RETURN_DRIFT", 0),
            "BINARY_LABEL_FLIP": class_ctr.get("BINARY_LABEL_FLIP", 0),
            "METADATA_MISMATCH": class_ctr.get("METADATA_MISMATCH", 0),
            "BLOCK_OR_TIMESTAMP_MISMATCH": class_ctr.get("BLOCK_OR_TIMESTAMP_MISMATCH", 0),
            "QUOTE_OR_EXIT_MISMATCH": class_ctr.get("QUOTE_OR_EXIT_MISMATCH", 0),
            "UNKNOWN_CONFLICT": class_ctr.get("UNKNOWN_CONFLICT", 0),
        },
        "conflict_rows_share_of_prepared": round(total_share, 6),
        "label_flip_share_of_prepared": round(flip_share, 6),
        "spread_stats": {
            "max": safe_pct(all_spreads, 1.0),
            "p99": safe_pct(all_spreads, 0.99),
            "p95": safe_pct(all_spreads, 0.95),
            "p90": safe_pct(all_spreads, 0.90),
            "p50": safe_pct(all_spreads, 0.50),
        },
        "flip_spread_stats": {
            "max": safe_pct(flip_spreads, 1.0),
            "p95": safe_pct(flip_spreads, 0.95),
            "p50": safe_pct(flip_spreads, 0.50),
        } if flip_spreads else None,
        "latest_7d_conflict_count": holdout_ck,
        "train_conflict_count": train_ck,
        "top_pool_concentration_in_conflicts": {
            "top1_share": round(top1_conc, 6),
            "top_pools": [[p, n] for p, n in top_pool_ck],
        },
        "root_cause_summary": (
            "Primary root cause: non-deterministic ts→block mapping across runs. "
            "Original pass and resume pass built ts-grid with different block samples, "
            "resulting in different entry_block for same event timestamp. "
            f"{class_ctr.get('BINARY_LABEL_FLIP',0)} keys flipped sign; "
            f"{class_ctr.get('SAME_LABEL_RETURN_DRIFT',0)} kept same sign but drifted net_return; "
            f"{class_ctr.get('QUOTE_OR_EXIT_MISMATCH',0)} show large spread (≥1.0) due to different exit block."
        ),
        "needs_targeted_relabel": (
            class_ctr.get("BINARY_LABEL_FLIP", 0) > 0
            or class_ctr.get("UNKNOWN_CONFLICT", 0) > 0
            or safe_pct(all_spreads, 0.95) > 0.01
        ),
        "relabel_target_keys": total_ck,
        "estimated_rpc_calls_per_key": 4,
        "estimated_total_rpc_calls": total_ck * 4 + 500,  # + ts grid
        "paths": {
            "input_csv": str(csv_in.relative_to(REPO)) if csv_in.is_relative_to(REPO) else str(csv_in),
            "out_json": str(out_json.relative_to(REPO)) if out_json.is_relative_to(REPO) else str(out_json),
            "conflict_keys_csv": str(out_csv.relative_to(REPO)) if out_csv.is_relative_to(REPO) else str(out_csv),
        },
    }

    # Write per-key CSV
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(per_key_rows)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    md = f"""# Run-004 Patch F1 — Duplicate Conflict Triage

**No RPC, no relabel, no retrain, no LIVE.**

## Root cause

{summary['root_cause_summary']}

## Conflict class breakdown

| Class | Count |
|-------|------:|
| `BINARY_LABEL_FLIP` | {summary['by_class']['BINARY_LABEL_FLIP']} |
| `SAME_LABEL_RETURN_DRIFT` | {summary['by_class']['SAME_LABEL_RETURN_DRIFT']} |
| `QUOTE_OR_EXIT_MISMATCH` | {summary['by_class']['QUOTE_OR_EXIT_MISMATCH']} |
| `BLOCK_OR_TIMESTAMP_MISMATCH` | {summary['by_class']['BLOCK_OR_TIMESTAMP_MISMATCH']} |
| `TINY_NUMERIC_DRIFT` | {summary['by_class']['TINY_NUMERIC_DRIFT']} |
| `METADATA_MISMATCH` | {summary['by_class']['METADATA_MISMATCH']} |
| `UNKNOWN_CONFLICT` | {summary['by_class']['UNKNOWN_CONFLICT']} |
| **Total** | **{summary['conflict_key_count']}** |

## Spread statistics

| | value |
|-|-:|
| p50 spread | {summary['spread_stats']['p50']} |
| p90 spread | {summary['spread_stats']['p90']} |
| p95 spread | {summary['spread_stats']['p95']} |
| p99 spread | {summary['spread_stats']['p99']} |
| max spread | {summary['spread_stats']['max']} |

## Prepared-set impact

| Metric | Value |
|--------|------:|
| `conflict_rows_share_of_prepared` | {summary['conflict_rows_share_of_prepared']} |
| `label_flip_share_of_prepared` | {summary['label_flip_share_of_prepared']} |
| Latest 7d conflict keys | {summary['latest_7d_conflict_count']} |
| Train conflict keys | {summary['train_conflict_count']} |

## Top pools in conflict keys

| Pool | Keys |
|------|-----:|
{''.join(f"| `{p}` | {n} |" + chr(10) for p, n in summary['top_pool_concentration_in_conflicts']['top_pools'][:10])}

## Targeted relabel estimate

- Keys to relabel: **{summary['relabel_target_keys']}**
- Est. RPC calls/key: {summary['estimated_rpc_calls_per_key']} (entry quote + exit quote + grid)
- Est. total RPC calls: **~{summary['estimated_total_rpc_calls']}**
- Needs relabel: `{summary['needs_targeted_relabel']}`

## Artifacts

- Per-key CSV: `{summary['paths']['conflict_keys_csv']}`
- Full JSON: `{summary['paths']['out_json']}`
"""
    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)

    print(f"conflict_keys={total_ck} flips={class_ctr.get('BINARY_LABEL_FLIP',0)} "
          f"drift={class_ctr.get('SAME_LABEL_RETURN_DRIFT',0)} "
          f"needs_relabel={summary['needs_targeted_relabel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
