"""
Run-004 Patch F1 Task D: canonical merge + final gates.

No RPC. Reads:
  logs/run004_v2only_labeled_dryrun_full.csv    (original E2 output — untouched)
  logs/run004_patch_f1_conflict_relabel_output.csv (targeted relabel of 823 conflict keys)

Produces:
  logs/run004_v2only_prepared_dryrun_canonical.csv
  mvp/diagnostics/run004_patch_f1_canonical_prepare_report.json
  mvp/runs/run-004-patch-f1-canonical-prepare.md

Merge rules:
  1. For non-conflict clean keys: keep first-success row from original E2 CSV.
  2. For conflict keys with successful relabel: use relabel result.
  3. For conflict keys with failed relabel: drop from canonical, report count.
  4. Each (tx_hash, log_index) appears at most once.
  5. duplicate_conflict_count must be 0.
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

EPS_CONFLICT = 1e-4

REQUIRED_FEATURES = ("timestamp", "final_score", "net_return", "pool_address",
                     "token_address", "tx_hash", "log_index")


def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    k = (n - 1) * q
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


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


def _ilab(r: dict[str, str]) -> int:
    try:
        return int(r.get("label", -1) or -1)
    except (TypeError, ValueError):
        return -1


def _identify_conflict_keys(
    by: dict[tuple[str, str], list[dict[str, str]]],
) -> set[tuple[str, str]]:
    ck: set[tuple[str, str]] = set()
    for k, vs in by.items():
        succs = [v for v in vs if _is_succ(v)]
        if len(succs) < 2:
            continue
        labels = {int(s["label"]) for s in succs}
        nets = [_pnet(s) for s in succs if _pnet(s) is not None]
        spread = max(nets) - min(nets) if len(nets) >= 2 else 0.0
        if len(labels) > 1 or spread > EPS_CONFLICT:
            ck.add(k)
    return ck


def _verify_no_remaining_conflicts(
    canonical: list[dict[str, str]],
) -> int:
    """Verify every key appears once. Returns conflict_count (must be 0)."""
    seen: dict[tuple[str, str], int] = {}
    for i, r in enumerate(canonical):
        k = (r.get("tx_hash", ""), r.get("log_index", ""))
        if k in seen:
            return i  # found duplicate
        seen[k] = i
    return 0


def metrics_for(rows: list[dict[str, str]], holdout_lo: int) -> dict[str, Any]:
    usable = len(rows)
    pos = sum(1 for r in rows if _ilab(r) == 1)
    neg = sum(1 for r in rows if _ilab(r) == 0)
    pos_rate = pos / usable if usable else None

    nets = sorted(v for r in rows if (v := _pnet(r)) is not None)
    nd: dict[str, Any] = {
        "min": round(_pct(nets, 0.0), 10) if nets else None,
        "p25": round(_pct(nets, 0.25), 10) if nets else None,
        "median": round(_pct(nets, 0.5), 10) if nets else None,
        "p75": round(_pct(nets, 0.75), 10) if nets else None,
        "p95": round(_pct(nets, 0.95), 10) if nets else None,
        "max": round(_pct(nets, 1.0), 10) if nets else None,
        "mean": round(sum(nets) / len(nets), 10) if nets else None,
    }

    pc = Counter((r.get("pool_address") or "").lower() for r in rows)
    counts = sorted(pc.values(), reverse=True)
    top1 = (counts[0] / usable) if counts and usable else 0.0
    top5 = (sum(counts[:5]) / usable) if usable else 0.0

    tss = [int(r.get("timestamp", 0) or 0) for r in rows if int(r.get("timestamp", 0) or 0) > 0]
    hold = [r for r in rows if int(r.get("timestamp", 0) or 0) >= holdout_lo]
    train = [r for r in rows if int(r.get("timestamp", 0) or 0) < holdout_lo]

    hpos = sum(1 for r in hold if _ilab(r) == 1)
    hneg = len(hold) - hpos
    tpos = sum(1 for r in train if _ilab(r) == 1)
    tneg = len(train) - tpos

    # one_pool_dominates_holdout_positives
    hp_pools = Counter((r.get("pool_address") or "").lower() for r in hold if _ilab(r) == 1)
    top_hp = hp_pools.most_common(1)
    opdp = bool(top_hp and hpos > 0 and top_hp[0][1] / hpos > 0.5)

    # Feature checks
    null_rates: dict[str, float] = {}
    nonfinite: dict[str, int] = {}
    for c in REQUIRED_FEATURES:
        null_c = bad_c = 0
        for r in rows:
            v = r.get(c, "")
            if not (v or "").strip():
                null_c += 1
            elif c not in ("pool_address", "token_address", "tx_hash", "log_index"):
                try:
                    x = float(v)
                    if not math.isfinite(x):
                        bad_c += 1
                except (TypeError, ValueError):
                    null_c += 1
        null_rates[c] = round(null_c / max(1, usable), 6)
        nonfinite[c] = bad_c

    gates = {
        "usable_rows_ok": usable >= 10000,
        "pos_rate_ok": pos_rate is not None and 0.10 <= pos_rate <= 0.90,
        "duplicate_conflict_ok": True,       # confirmed by caller
        "null_ok": all(v <= 0.02 for v in null_rates.values()),
        "nonfinite_ok": sum(nonfinite.values()) == 0,
        "top1_ok": top1 <= 0.35,
        "top5_ok": top5 <= 0.65,
        "holdout_rows_ok": len(hold) >= 500,
        "holdout_pos_ok": hpos >= 50,
        "holdout_neg_ok": hneg >= 50,
        "one_pool_dominates_holdout_ok": not opdp,
    }

    return {
        "usable_rows": usable,
        "positive_count": pos,
        "negative_count": neg,
        "positive_rate": round(float(pos_rate), 6) if pos_rate is not None else None,
        "net_return_distribution": nd,
        "top1_pool_share": round(top1, 6),
        "top5_pool_share": round(top5, 6),
        "latest_7d_holdout_rows": len(hold),
        "latest_7d_positive": hpos,
        "latest_7d_negative": hneg,
        "train_rows": len(train),
        "train_positive": tpos,
        "train_negative": tneg,
        "one_pool_dominates_holdout_positives": opdp,
        "feature_null_rate": null_rates,
        "feature_non_finite_count": nonfinite,
        "gates": gates,
    }


def _conclude(gates: dict[str, bool], duplicate_conflict_count: int) -> str:
    if duplicate_conflict_count > 0:
        return "HOLD_DUE_TO_REMAINING_DUPLICATE_CONFLICT"
    if not gates.get("duplicate_conflict_ok", True):
        return "HOLD_DUE_TO_REMAINING_DUPLICATE_CONFLICT"
    if not gates.get("null_ok") or not gates.get("nonfinite_ok"):
        return "HOLD_DUE_TO_FEATURE_QUALITY"
    if not gates.get("pos_rate_ok"):
        return "HOLD_DUE_TO_LABEL_DISTRIBUTION"
    if not gates.get("top1_ok") or not gates.get("top5_ok"):
        return "HOLD_DUE_TO_POOL_CONCENTRATION"
    if not (gates.get("holdout_rows_ok") and gates.get("holdout_pos_ok") and gates.get("holdout_neg_ok")):
        return "HOLD_DUE_TO_HOLDOUT_INSUFFICIENT"
    if not gates.get("one_pool_dominates_holdout_ok"):
        return "HOLD_DUE_TO_HOLDOUT_INSUFFICIENT"
    if not gates.get("usable_rows_ok"):
        return "HOLD_DUE_TO_FEATURE_QUALITY"
    return "PATCH_F1_GO_TO_PATCH_G_RETRAIN_DRYRUN"


def main() -> int:
    e2_csv = Path(os.environ.get(
        "RUN004_F_E2_CSV",
        str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv"),
    ))
    relabel_csv = Path(os.environ.get(
        "RUN004_F1_RELABEL_OUTPUT",
        str(REPO / "logs" / "run004_patch_f1_conflict_relabel_output.csv"),
    ))
    out_csv = Path(os.environ.get(
        "RUN004_F1_CANONICAL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
    ))
    out_json = Path(os.environ.get(
        "RUN004_F1_CANONICAL_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f1_canonical_prepare_report.json"),
    ))
    out_md = Path(os.environ.get(
        "RUN004_F1_CANONICAL_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-f1-canonical-prepare.md"),
    ))

    for p in (e2_csv, relabel_csv):
        if not p.is_file():
            print(f"ERROR: missing {p}", file=sys.stderr)
            return 1

    # ── Load E2 labeled CSV ──
    with e2_csv.open("r", encoding="utf-8") as f:
        e2_rows = list(csv.DictReader(f))
    e2_fieldnames = list(csv.DictReader(open(e2_csv, encoding="utf-8")).fieldnames or [])

    by: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in e2_rows:
        by[(r.get("tx_hash", ""), r.get("log_index", ""))].append(r)

    # Identify all conflict keys in original E2 data
    conflict_keys = _identify_conflict_keys(by)
    print(f"conflict_keys_in_e2={len(conflict_keys)}")

    # ── Load targeted relabel output ──
    with relabel_csv.open("r", encoding="utf-8") as f:
        relabel_rows = list(csv.DictReader(f))

    relabel_map: dict[tuple[str, str], dict[str, str]] = {}
    for r in relabel_rows:
        k = (r.get("tx_hash", ""), r.get("log_index", ""))
        relabel_map[k] = r

    relabel_success = {
        k: r for k, r in relabel_map.items()
        if r.get("label") in ("0", "1")
    }
    relabel_failed = {
        k: r for k, r in relabel_map.items()
        if r.get("label") not in ("0", "1")
    }

    print(f"relabel_success={len(relabel_success)} relabel_failed={len(relabel_failed)}")

    # ── Timestamps for holdout split ──
    tss = [int(r.get("timestamp", 0) or 0) for r in e2_rows if int(r.get("timestamp", 0) or 0) > 0]
    t_max = max(tss) if tss else 0
    holdout_lo = t_max - 7 * 86400 * 1000

    # ── Build canonical dataset ──
    canonical: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    stats = Counter()

    for k, vs in sorted(by.items()):
        if k in seen_keys:
            continue
        seen_keys.add(k)

        if k in conflict_keys:
            if k in relabel_success:
                # Merge: take relabel result, fill missing fields from E2 first row
                rr = dict(relabel_success[k])
                e2_ref = vs[0]
                # Keep E2 candidate metadata fields not in relabel output
                for col in ("topic0", "event_kind", "final_score", "source_row_id",
                            "candidate_status", "reject_reason", "gate_status"):
                    if not rr.get(col):
                        rr[col] = e2_ref.get(col, "")
                canonical.append(rr)
                stats["conflict_relabeled"] += 1
            elif k in relabel_failed:
                # Drop from canonical — relabel failed
                stats["conflict_relabel_failed_dropped"] += 1
            else:
                # Conflict key not in relabel output (unexpected) — drop
                stats["conflict_key_missing_from_relabel"] += 1
        else:
            # Non-conflict key: keep first success row
            for r in vs:
                if _is_succ(r):
                    canonical.append(r)
                    stats["clean_key_kept"] += 1
                    break
            else:
                stats["clean_key_no_success_skipped"] += 1

    print(f"canonical_stats={dict(stats)}")

    # ── Verify no duplicates ──
    dup_check: dict[tuple[str, str], int] = {}
    dup_found = 0
    for r in canonical:
        k = (r.get("tx_hash", ""), r.get("log_index", ""))
        if k in dup_check:
            dup_found += 1
        else:
            dup_check[k] = 1
    print(f"duplicate_conflict_count_in_canonical={dup_found}")

    # ── Compute metrics ──
    m = metrics_for(canonical, holdout_lo)
    conclusion = _conclude(m["gates"], dup_found)

    # ── Write canonical CSV ──
    # Use relabel output fields merged with E2 fields
    all_fields = list(e2_fieldnames)
    for rf in relabel_rows[0].keys() if relabel_rows else []:
        if rf not in all_fields:
            all_fields.append(rf)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        for r in canonical:
            w.writerow(r)

    print(f"canonical_csv written: {len(canonical)} rows -> {out_csv}")

    # ── Report ──
    report: dict[str, Any] = {
        "conclusion": conclusion,
        "conclusion_pass": conclusion == "PATCH_F1_GO_TO_PATCH_G_RETRAIN_DRYRUN",
        "merge_stats": dict(stats),
        "duplicate_conflict_count": dup_found,
        "conflict_keys_in_e2": len(conflict_keys),
        "relabel_success_count": len(relabel_success),
        "relabel_failed_count": len(relabel_failed),
        "conflict_keys_dropped": len(relabel_failed) + stats.get("conflict_key_missing_from_relabel", 0),
        **m,
        "holdout_lo_ms": holdout_lo,
        "paths": {
            "canonical_csv": str(out_csv.relative_to(REPO)) if out_csv.is_relative_to(REPO) else str(out_csv),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    gate_rows = "\n".join(
        f"| `{k}` | {'✔' if v else '✘'} |"
        for k, v in m["gates"].items()
    )
    nr = m.get("net_return_distribution", {})
    md = f"""# Run-004 Patch F1 — Canonical Prepare

**Conclusion**: `{conclusion}`

## Merge stats

| Source | Count |
|--------|------:|
| conflict keys relabeled → kept | {stats.get('conflict_relabeled', 0)} |
| conflict keys relabel failed → dropped | {stats.get('conflict_relabel_failed_dropped', 0)} |
| conflict keys missing from relabel | {stats.get('conflict_key_missing_from_relabel', 0)} |
| clean keys kept | {stats.get('clean_key_kept', 0)} |
| clean keys (no success) skipped | {stats.get('clean_key_no_success_skipped', 0)} |
| **canonical total** | **{len(canonical)}** |
| `duplicate_conflict_count` | **{dup_found}** |

## Canonical dataset metrics

| Metric | Value |
|--------|------:|
| usable_rows | {m['usable_rows']} |
| positive_count | {m['positive_count']} |
| negative_count | {m['negative_count']} |
| positive_rate | {m['positive_rate']} |
| top1_pool_share | {m['top1_pool_share']} |
| top5_pool_share | {m['top5_pool_share']} |
| latest_7d_holdout_rows | {m['latest_7d_holdout_rows']} |
| latest_7d_positive | {m['latest_7d_positive']} |
| latest_7d_negative | {m['latest_7d_negative']} |
| train_rows | {m['train_rows']} |
| train_positive | {m['train_positive']} |
| train_negative | {m['train_negative']} |
| one_pool_dominates_holdout | {m['one_pool_dominates_holdout_positives']} |

## Net return distribution

| stat | value |
|------|------:|
| min | {nr.get('min')} |
| p25 | {nr.get('p25')} |
| median | {nr.get('median')} |
| p75 | {nr.get('p75')} |
| p95 | {nr.get('p95')} |
| max | {nr.get('max')} |
| mean | {nr.get('mean')} |

## Final gates

| Gate | Pass |
|------|:----:|
{gate_rows}

## Patch G eligibility

| Condition | Value |
|-----------|-------|
| `duplicate_conflict_count == 0` | {dup_found == 0} |
| All gates pass | {all(m['gates'].values())} |
| **Conclusion** | `{conclusion}` |
"""
    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)

    print(f"conclusion={conclusion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
