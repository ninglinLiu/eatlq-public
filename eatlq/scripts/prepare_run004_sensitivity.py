"""
Run-004 Patch F1 Task B: sensitivity datasets — no RPC.

Produces two diagnostic prepared CSVs from E2 labeled output:
  1. first_success: keep file-order first success row per (tx_hash, log_index)
  2. conflict_excluded: drop all 823 conflict keys, keep first success elsewhere

For each, computes the full metric set for comparison.
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


EPS_CONFLICT = 1e-4

FEAT_COLS = ("timestamp", "final_score", "pool_address", "token_address",
             "tx_hash", "log_index", "net_return")


def metrics_for(prepared: list[dict[str, str]], t_holdout_lo: int) -> dict[str, Any]:
    usable = len(prepared)
    posc = sum(1 for r in prepared if _ilab(r) == 1)
    negc = sum(1 for r in prepared if _ilab(r) == 0)
    prate = (posc / usable) if usable else None

    nets = sorted(v for r in prepared if (v := _pnet(r)) is not None)
    nd: dict[str, Any] = (
        {
            "min": round(_pct(nets, 0.0), 10),
            "p25": round(_pct(nets, 0.25), 10),
            "median": round(_pct(nets, 0.5), 10),
            "p75": round(_pct(nets, 0.75), 10),
            "p95": round(_pct(nets, 0.95), 10),
            "max": round(_pct(nets, 1.0), 10),
            "mean": round(sum(nets) / len(nets), 10),
        }
        if nets
        else {k: None for k in ("min", "p25", "median", "p75", "p95", "max", "mean")}
    )

    pc = Counter((r.get("pool_address") or "").lower() for r in prepared)
    counts = sorted(pc.values(), reverse=True)
    top1 = (counts[0] / usable) if counts and usable else 0.0
    top5 = (sum(counts[:5]) / usable) if usable else 0.0

    tss = [int(r.get("timestamp", 0) or 0) for r in prepared if int(r.get("timestamp", 0) or 0) > 0]
    tmin = min(tss) if tss else 0
    tmax = max(tss) if tss else 0
    hold = [r for r in prepared if tmax and int(r.get("timestamp", 0) or 0) >= t_holdout_lo]
    train = [r for r in prepared if not tmax or int(r.get("timestamp", 0) or 0) < t_holdout_lo]
    hpos = sum(1 for r in hold if _ilab(r) == 1)
    hneg = len(hold) - hpos
    tpos = sum(1 for r in train if _ilab(r) == 1)
    tneg = len(train) - tpos

    # Feature null/finite
    null_rate: dict[str, float] = {}
    nonfinite: dict[str, int] = {}
    for c in FEAT_COLS:
        null_c = 0
        bad_c = 0
        for r in prepared:
            v = r.get(c, "")
            if not (v or "").strip():
                null_c += 1
            elif c not in ("pool_address", "token_address", "tx_hash"):
                try:
                    x = float(int(r.get("timestamp", 0) or 0) if c == "timestamp" else r.get(c, 0) or 0)
                    if not math.isfinite(x):
                        bad_c += 1
                except (TypeError, ValueError):
                    null_c += 1
        null_rate[c] = round(null_c / max(1, usable), 6)
        nonfinite[c] = bad_c

    return {
        "usable_rows": usable,
        "positive_count": posc,
        "negative_count": negc,
        "positive_rate": round(float(prate), 6) if prate is not None else None,
        "net_return_distribution": nd,
        "top1_pool_share": round(top1, 6),
        "top5_pool_share": round(top5, 6),
        "time_range_ms": {"min": tmin, "max": tmax},
        "latest_7d_holdout_rows": len(hold),
        "latest_7d_positive": hpos,
        "latest_7d_negative": hneg,
        "train_rows": len(train),
        "train_positive": tpos,
        "train_negative": tneg,
        "feature_null_rate": null_rate,
        "feature_non_finite_count": nonfinite,
        "gates": {
            "usable_rows_ok": usable >= 10000,
            "pos_rate_ok": prate is not None and 0.10 <= prate <= 0.90,
            "null_ok": all(v <= 0.02 for v in null_rate.values() if isinstance(v, float)),
            "top1_ok": top1 <= 0.35,
            "top5_ok": top5 <= 0.65,
            "holdout_ok": len(hold) >= 500 and hpos >= 50 and hneg >= 50,
        },
    }


def _identify_conflicts(
    by: dict[tuple[str, str], list[dict[str, str]]],
) -> set[tuple[str, str]]:
    conflicts: set[tuple[str, str]] = set()
    for k, vs in by.items():
        succs = [v for v in vs if _is_succ(v)]
        if len(succs) < 2:
            continue
        labels = set(int(s["label"]) for s in succs)
        nets = [_pnet(s) for s in succs if _pnet(s) is not None]
        spread = max(nets) - min(nets) if len(nets) >= 2 else 0.0
        if len(labels) > 1 or spread > EPS_CONFLICT:
            conflicts.add(k)
    return conflicts


def _make_first_success_dataset(
    by: dict[tuple[str, str], list[dict[str, str]]],
    exclude_keys: set[tuple[str, str]] | None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for k, vs in sorted(by.items(), key=lambda x: x[0]):
        if exclude_keys and k in exclude_keys:
            continue
        for r in vs:
            if _is_succ(r):
                out.append(r)
                break
    return out


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    csv_in = Path(os.environ.get("RUN004_F_E2_CSV", str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv")))
    out_json = Path(os.environ.get(
        "RUN004_F1_SENSITIVITY_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_f1_sensitivity_report.json"),
    ))
    out_md = Path(os.environ.get(
        "RUN004_F1_SENSITIVITY_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-f1-sensitivity.md"),
    ))
    out_fs = Path(os.environ.get(
        "RUN004_F1_FIRST_SUCCESS_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_first_success_sensitivity.csv"),
    ))
    out_ce = Path(os.environ.get(
        "RUN004_F1_CONFLICT_EXCL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_conflict_excluded_sensitivity.csv"),
    ))

    with csv_in.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    tss = [int(r.get("timestamp", 0) or 0) for r in rows if int(r.get("timestamp", 0) or 0) > 0]
    t_max = max(tss) if tss else 0
    holdout_lo = t_max - 7 * 86400 * 1000

    by: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by[(r.get("tx_hash", ""), r.get("log_index", ""))].append(r)

    conflict_keys = _identify_conflicts(by)

    # Dataset 1: first success, including conflict keys (picks file-order winner)
    ds_fs = _make_first_success_dataset(by, exclude_keys=None)
    # Dataset 2: conflict excluded (823 keys dropped, first success for clean keys)
    ds_ce = _make_first_success_dataset(by, exclude_keys=conflict_keys)

    m_fs = metrics_for(ds_fs, holdout_lo)
    m_ce = metrics_for(ds_ce, holdout_lo)

    _write_csv(out_fs, ds_fs)
    _write_csv(out_ce, ds_ce)

    def _mp(v: Any) -> str:
        return "—" if v is None else str(v)

    report: dict[str, Any] = {
        "first_success": {"description": "file-order first success per key (includes conflict keys)", **m_fs},
        "conflict_excluded": {"description": "drop all 823 conflict keys (most conservative)", **m_ce},
        "delta_analysis": {
            "usable_rows_delta": m_fs["usable_rows"] - m_ce["usable_rows"],
            "positive_rate_delta": (
                round((m_fs["positive_rate"] or 0) - (m_ce["positive_rate"] or 0), 6)
                if m_fs.get("positive_rate") and m_ce.get("positive_rate") else None
            ),
            "top1_delta": round(m_fs["top1_pool_share"] - m_ce["top1_pool_share"], 6),
            "top5_delta": round(m_fs["top5_pool_share"] - m_ce["top5_pool_share"], 6),
            "holdout_delta": m_fs["latest_7d_holdout_rows"] - m_ce["latest_7d_holdout_rows"],
            "material_distribution_shift": (
                abs((m_fs.get("positive_rate") or 0) - (m_ce.get("positive_rate") or 0)) > 0.03
                or abs(m_fs["top1_pool_share"] - m_ce["top1_pool_share"]) > 0.02
            ),
        },
        "conflict_key_count": len(conflict_keys),
        "holdout_lo_ms": holdout_lo,
        "paths": {
            "first_success_csv": str(out_fs.relative_to(REPO)) if out_fs.is_relative_to(REPO) else str(out_fs),
            "conflict_excluded_csv": str(out_ce.relative_to(REPO)) if out_ce.is_relative_to(REPO) else str(out_ce),
            "out_json": str(out_json.relative_to(REPO)) if out_json.is_relative_to(REPO) else str(out_json),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    def _gate_row(label: str, m: dict[str, Any]) -> str:
        g = m.get("gates", {})
        return (
            f"| {label} | {m.get('usable_rows')} | {m.get('positive_rate')} |"
            f" {m.get('top1_pool_share')} / {m.get('top5_pool_share')} |"
            f" {m.get('latest_7d_holdout_rows')} | {m.get('latest_7d_positive')} / {m.get('latest_7d_negative')} |"
            f" {all(g.values())} |\n"
        )

    net_fs = m_fs.get("net_return_distribution", {})
    net_ce = m_ce.get("net_return_distribution", {})

    md = f"""# Run-004 Patch F1 — Sensitivity datasets

**No RPC. These CSVs are diagnostic only, not final training input.**

| Dataset | Usable | pos_rate | top1/top5 | holdout rows | holdout pos/neg | all_gates |
|---------|-------:|----------:|----------:|-------------:|:--------------:|:---------:|
{_gate_row("first_success", m_fs)}{_gate_row("conflict_excluded", m_ce)}
## Net return comparison

| stat | first_success | conflict_excluded |
|------|------:|------:|
| min | {net_fs.get('min')} | {net_ce.get('min')} |
| p25 | {net_fs.get('p25')} | {net_ce.get('p25')} |
| median | {net_fs.get('median')} | {net_ce.get('median')} |
| p75 | {net_fs.get('p75')} | {net_ce.get('p75')} |
| p95 | {net_fs.get('p95')} | {net_ce.get('p95')} |
| mean | {net_fs.get('mean')} | {net_ce.get('mean')} |

## Delta analysis

| Metric | Value |
|--------|------:|
| usable_rows_delta | {report['delta_analysis']['usable_rows_delta']} |
| positive_rate_delta | {report['delta_analysis']['positive_rate_delta']} |
| top1_delta | {report['delta_analysis']['top1_delta']} |
| top5_delta | {report['delta_analysis']['top5_delta']} |
| holdout_delta | {report['delta_analysis']['holdout_delta']} |
| material_distribution_shift | {report['delta_analysis']['material_distribution_shift']} |

## Recommendation

{"⚠ Material distribution shift detected: targeted relabel recommended before training."
if report['delta_analysis']['material_distribution_shift']
else "✔ No material distribution shift between first_success and conflict_excluded. "
     "first_success approach is usable as dry-run proxy, but targeted relabel recommended for canonical dataset."}
"""
    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)

    print(
        f"first_success rows={m_fs['usable_rows']} pos_rate={m_fs['positive_rate']} "
        f"top1={m_fs['top1_pool_share']} holdout={m_fs['latest_7d_holdout_rows']}"
    )
    print(
        f"conflict_excluded rows={m_ce['usable_rows']} pos_rate={m_ce['positive_rate']} "
        f"top1={m_ce['top1_pool_share']} holdout={m_ce['latest_7d_holdout_rows']}"
    )
    print(f"material_shift={report['delta_analysis']['material_distribution_shift']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
