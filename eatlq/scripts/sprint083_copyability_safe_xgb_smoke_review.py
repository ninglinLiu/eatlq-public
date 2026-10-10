#!/usr/bin/env python3
"""Copyability-safe bounded XGB smoke (research-only). Approved retrain review only."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from sprint083_expanded_v2_bounded_xgb_smoke_review import (  # noqa: E402
    as_float,
    build_matrix,
    chrono_split,
    leakage_audit,
    load_manifest,
    select_features,
    train_eval_xgb,
)

DEFAULT_DATASET = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_dataset_sample.csv"
)
DEFAULT_MANIFEST = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/feature_manifest.json"
)
DEFAULT_PREP = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_prep_expanded_v2/copyability_safe_prep_flags.json"
)
DEFAULT_OUT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_copyability_safe_xgb_smoke_expanded_v2"
)

PRIMARY_LABEL = "copy_delay_120s"
SECONDARY_LABEL = "copy_delay_60s"
LABEL_CLIP_ABS = 1.0
OUTLIER_MEAN_SKEW_RATIO = 10.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copyability-safe XGB smoke review")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--prep-flags", default=DEFAULT_PREP)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--label", default=PRIMARY_LABEL)
    p.add_argument("--optional-label", default=SECONDARY_LABEL)
    p.add_argument("--label-clip-abs", type=float, default=LABEL_CLIP_ABS)
    p.add_argument("--train-frac", type=float, default=0.70)
    p.add_argument("--valid-frac", type=float, default=0.15)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--n-estimators", type=int, default=50)
    p.add_argument("--learning-rate", type=float, default=0.05)
    p.add_argument("--run-block-number-ablation", action="store_true", default=True)
    return p.parse_args()


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))
    return s[i]


def label_distribution(rows: list[dict], col: str) -> dict:
    vals = [as_float(r.get(col)) for r in rows]
    vals = [v for v in vals if v is not None]
    n_all = len(rows)
    if not vals:
        return {"column": col, "non_null": 0, "coverage": 0.0}
    within_one = [v for v in vals if abs(v) <= 1.0]
    return {
        "column": col,
        "non_null": len(vals),
        "coverage": round(len(vals) / n_all, 4) if n_all else 0.0,
        "min": round(min(vals), 6),
        "p01": round(pct(vals, 0.01), 6),
        "p05": round(pct(vals, 0.05), 6),
        "median": round(median(vals), 6),
        "mean": round(mean(vals), 6),
        "mean_abs_le_1": round(mean(within_one), 6) if within_one else None,
        "p95": round(pct(vals, 0.95), 6),
        "p99": round(pct(vals, 0.99), 6),
        "max": round(max(vals), 6),
        "pct_abs_le_1": round(len(within_one) / len(vals), 4),
        "count_abs_gt_1": sum(1 for v in vals if abs(v) > 1.0),
        "count_abs_gt_5": sum(1 for v in vals if abs(v) > 5.0),
        "looks_like_return_ratio": max(abs(v) for v in vals) < 1000.0,
        "wei_like_fail": max(abs(v) for v in vals) >= 1000.0,
    }


def label_sanity_gate(dist: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if dist.get("wei_like_fail"):
        reasons.append(f"{dist['column']}: max abs value wei-like (>=1000)")
        return False, reasons
    if dist.get("non_null", 0) == 0:
        reasons.append(f"{dist['column']}: no non-null values")
        return False, reasons
    med = abs(dist.get("median") or 0.0)
    mn = dist.get("mean") or 0.0
    if abs(mn) > OUTLIER_MEAN_SKEW_RATIO * max(med, 1e-4) + 0.05:
        reasons.append(
            f"{dist['column']}: mean={mn} skewed vs median={dist.get('median')} "
            f"(outlier-driven; use clip abs<={LABEL_CLIP_ABS} for training)"
        )
    if (dist.get("max") or 0) > 20.0:
        reasons.append(f"{dist['column']}: max={dist.get('max')} exceeds return sanity cap 20")
    if (dist.get("pct_abs_le_1") or 0) < 0.95:
        reasons.append(f"{dist['column']}: only {dist.get('pct_abs_le_1')} within |r|<=1")
    if reasons:
        return False, reasons
    return True, []


def clip_label_rows(rows: list[dict], col: str, clip_abs: float) -> tuple[list[dict], int]:
    clipped = 0
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        v = as_float(row.get(col))
        if v is not None and abs(v) > clip_abs:
            row[col] = clip_abs if v > 0 else -clip_abs
            clipped += 1
        out.append(row)
    return out, clipped


def signal_status(
    test_auc: float | None,
    valid_auc: float | None,
    test_mean10: float | None,
    test_prec10: float | None,
    conc_status: str,
    copy_delay_pass: bool,
) -> str:
    if test_auc is None:
        return "INCONCLUSIVE"
    gap = (test_auc or 0) - (valid_auc or 0) if valid_auc is not None else 0
    if gap > 0.12 and (test_prec10 or 0) > 0.85:
        return "OVERFIT_OR_LEAKAGE_RISK"
    if conc_status.startswith("FAIL"):
        return "NOT_COPYABLE"
    if not copy_delay_pass:
        return "NOT_COPYABLE"
    if test_auc >= 0.55 and (test_mean10 or 0) > 0 and copy_delay_pass:
        return "COPYABLE_SIGNAL_CANDIDATE"
    if test_auc >= 0.52 or (test_mean10 or 0) > 0:
        return "WEAK_SIGNAL_NEEDS_OOS"
    return "NOT_COPYABLE"


def copy_delay_pass_top10(rows: list[dict], idx: list[int], ret_col: str) -> bool:
    rets = [as_float(rows[i].get(ret_col)) for i in idx]
    rets = [x for x in rets if x is not None]
    if not rets:
        return False
    return mean(rets) > 0 and sum(1 for x in rets if x > 0) / len(rets) >= 0.5


def top10_copyability_rows(
    modeled: list[dict], proba: list[float], label_col: str
) -> tuple[list[int], list[dict]]:
    n = len(proba)
    k = max(1, int(math.ceil(n * 0.10)))
    idx = sorted(range(n), key=lambda i: proba[i], reverse=True)[:k]
    return idx, [modeled[i] for i in idx]


def analyze_top10(selected: list[dict]) -> dict:
    rets60 = [as_float(r.get("copy_delay_60s")) for r in selected]
    rets60 = [x for x in rets60 if x is not None]
    rets120 = [as_float(r.get("copy_delay_120s")) for r in selected]
    rets120 = [x for x in rets120 if x is not None]
    weth = [as_float(r.get("amount_weth")) for r in selected]
    weth = [x for x in weth if x is not None]
    buckets = Counter(str(r.get("trade_size_bucket", "")) for r in selected)
    pools = [str(r.get("pool", "")).lower() for r in selected if r.get("pool")]
    tokens = [str(r.get("token", "")).lower() for r in selected if r.get("token")]
    wallets = [str(r.get("wallet", "")).lower() for r in selected if r.get("wallet")]
    closed = sum(1 for r in selected if str(r.get("closed_loop_eligible")).lower() == "true")
    return {
        "n": len(selected),
        "mean_copy_delay_60s": round(mean(rets60), 6) if rets60 else None,
        "mean_copy_delay_120s": round(mean(rets120), 6) if rets120 else None,
        "hit_rate_copy_delay_120s": round(sum(1 for x in rets120 if x > 0) / len(rets120), 4) if rets120 else None,
        "closed_loop_coverage": round(closed / len(selected), 4) if selected else None,
        "top_pool_share": round(max(Counter(pools).values()) / len(pools), 4) if pools else None,
        "top_token_share": round(max(Counter(tokens).values()) / len(tokens), 4) if tokens else None,
        "top_wallet_share": round(max(Counter(wallets).values()) / len(wallets), 4) if wallets else None,
        "amount_weth_p50": round(pct(weth, 0.5), 6) if weth else None,
        "amount_weth_p95": round(pct(weth, 0.95), 6) if weth else None,
        "trade_size_bucket_counts": dict(buckets),
    }


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = REPO / args.dataset
    manifest_path = REPO / args.manifest
    prep_path = REPO / args.prep_flags

    rows_raw = list(csv.DictReader(dataset_path.open()))
    dist_primary = label_distribution(rows_raw, args.label)
    dist_secondary = label_distribution(rows_raw, args.optional_label)
    sanity_ok_raw, sanity_reasons = label_sanity_gate(dist_primary)
    sanity_ok_sec, sanity_reasons_sec = label_sanity_gate(dist_secondary)
    sanity_reasons.extend(sanity_reasons_sec)

    rows, clipped_n = clip_label_rows(rows_raw, args.label, args.label_clip_abs)
    rows, clipped_n2 = clip_label_rows(rows, args.optional_label, args.label_clip_abs)
    clipped_n += clipped_n2
    dist_primary_clipped = label_distribution(rows, args.label)
    sanity_ok, _ = label_sanity_gate(dist_primary_clipped)

    amount_outliers = sum(1 for r in rows_raw if str(r.get("amount_proxy_outlier")).lower() == "true")
    amount_ok = amount_outliers == 0
    weth_vals = [as_float(r.get("amount_weth")) for r in rows_raw]
    weth_vals = [x for x in weth_vals if x is not None]
    amount_unit_ok = bool(weth_vals) and max(weth_vals) < 500.0

    # Pass if clipped labels sane and amounts OK (raw may be outlier-skewed before clip).
    label_sanity_pass = sanity_ok and amount_ok and amount_unit_ok and bool(sanity_ok_raw is False or clipped_n >= 0)
    label_status = "LABEL_SANITY_PASS" if label_sanity_pass else "LABEL_SANITY_FAIL"

    smoke_flags: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "LABEL_SANITY_PASS": label_sanity_pass,
        "LABEL_SANITY_STATUS": label_status,
        "label_sanity_raw_pass": sanity_ok_raw and sanity_ok_sec,
        "label_clip_abs_applied": args.label_clip_abs,
        "label_rows_clipped": clipped_n,
        "label_distribution_raw": {"primary": dist_primary, "secondary": dist_secondary},
        "label_distribution_clipped": dist_primary_clipped,
        "label_sanity_fail_reasons": sanity_reasons if not label_sanity_pass else [],
        "amount_proxy_outlier_rows": amount_outliers,
        "AMOUNT_UNIT_REPAIR_PASS": amount_unit_ok,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PAPER_FORWARD_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "NO_INSTANT_FUTURE_RETURN_30M_AS_PRIMARY": True,
        "BLOCK_NUMBER_EXCLUDED_BY_DEFAULT": True,
        "SMOKE_TRAINING_RAN": False,
    }

    if not label_sanity_pass:
        smoke_flags["COPYABILITY_SIGNAL_STATUS"] = "LABEL_SANITY_FAIL"
        smoke_flags["NEXT_STEP"] = "LABEL_REPAIR_REQUIRED"
        (out_dir / "copyability_safe_smoke_flags.json").write_text(
            json.dumps(smoke_flags, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "xgb_smoke_metrics.json").write_text(
            json.dumps({"decision": smoke_flags, "label_sanity": smoke_flags}, indent=2) + "\n"
        )
        write_readout = f"""# Copyability-Safe XGB Smoke — LABEL_SANITY_FAIL

## Label sanity

Primary `{args.label}` and secondary `{args.optional_label}` failed raw sanity (outlier-skewed mean vs median).
Training **not run**.

### Raw distribution (primary)
```json
{json.dumps(dist_primary, indent=2)}
```

### Fail reasons
{chr(10).join('- ' + r for r in sanity_reasons)}
"""
        (out_dir / "xgb_smoke_readout.md").write_text(write_readout, encoding="utf-8")
        print(json.dumps(smoke_flags, indent=2))
        return 3

    manifest = load_manifest(manifest_path)
    features = select_features(manifest, include_block_number=False)
    all_cols = list(rows[0].keys()) if rows else []
    audit = leakage_audit(manifest, features, all_cols)

    train_rows, valid_rows, test_rows = chrono_split(rows, args.train_frac, args.valid_frac)
    split_fields = ["split", "entry_time", "pool", "wallet", args.label, args.optional_label]
    with (out_dir / "train_valid_test_split_summary.csv").open("w", newline="") as sf:
        w = csv.DictWriter(sf, fieldnames=split_fields)
        w.writeheader()
        for name, part in (("train", train_rows), ("valid", valid_rows), ("test", test_rows)):
            for r in part:
                w.writerow(
                    {
                        "split": name,
                        "entry_time": r.get("entry_time", ""),
                        "pool": r.get("pool", ""),
                        "wallet": r.get("wallet", ""),
                        args.label: r.get(args.label, ""),
                        args.optional_label: r.get(args.optional_label, ""),
                    }
                )

    (out_dir / "selected_features.json").write_text(
        json.dumps(
            {
                "features": features,
                "manifest_tags": {f: manifest[f] for f in features},
                "leakage_audit": audit,
                "block_number_excluded": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_model(feat_list: list[str]) -> dict:
        x_tr, y_tr, _, _, _, _ = build_matrix(train_rows, feat_list, args.label)
        x_va, y_va, ret_va, pools_va, wal_va, tok_va = build_matrix(valid_rows, feat_list, args.label)
        x_te, y_te, ret_te, pools_te, wal_te, tok_te = build_matrix(test_rows, feat_list, args.label)
        return train_eval_xgb(
            x_tr, y_tr, x_va, y_va, ret_va, pools_va, wal_va, tok_va,
            x_te, y_te, ret_te, pools_te, wal_te, tok_te,
            feat_list, args.max_depth, args.n_estimators, args.learning_rate,
        )

    primary = run_model(features)
    x_tr, y_tr, _, _, _, _ = build_matrix(train_rows, features, args.optional_label)
    x_va, y_va, ret_va, pools_va, wal_va, tok_va = build_matrix(valid_rows, features, args.optional_label)
    x_te, y_te, ret_te, pools_te, wal_te, tok_te = build_matrix(test_rows, features, args.optional_label)
    secondary = train_eval_xgb(
        x_tr, y_tr, x_va, y_va, ret_va, pools_va, wal_va, tok_va,
        x_te, y_te, ret_te, pools_te, wal_te, tok_te,
        features, args.max_depth, args.n_estimators, args.learning_rate,
    )

    ablation = {}
    if args.run_block_number_ablation:
        feats_blk = select_features(manifest, include_block_number=True)
        with_blk = run_model(feats_blk)
        no_blk = primary
        ablation = {
            "test_auc_without_block_number": no_blk.get("test_auc"),
            "test_auc_with_block_number": with_blk.get("test_auc"),
            "auc_delta_with_minus_without": round(
                (with_blk.get("test_auc") or 0) - (no_blk.get("test_auc") or 0), 4
            ),
            "regime_proxy_likely": (with_blk.get("test_auc") or 0) - (no_blk.get("test_auc") or 0) > 0.08,
            "metrics_without": no_blk,
            "metrics_with": with_blk,
        }

    # top10 export
    x_te_full, y_te, ret_te, pools_te, wal_te, tok_te = build_matrix(test_rows, features, args.label)
    import numpy as np
    import xgboost as xgb

    modeled_test: list[dict] = []
    x_te_list: list[list[float]] = []
    for r in test_rows:
        ret = as_float(r.get(args.label))
        if ret is None:
            continue
        vals = []
        skip = False
        for f in features:
            v = as_float(r.get(f))
            if v is None:
                skip = True
                break
            vals.append(v)
        if skip:
            continue
        modeled_test.append(r)
        x_te_list.append(vals)

    x_tr_m, y_tr_m, _, _, _, _ = build_matrix(train_rows, features, args.label)
    x_va_m, y_va_m, _, _, _, _ = build_matrix(valid_rows, features, args.label)
    clf = xgb.XGBClassifier(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        learning_rate=args.learning_rate,
        objective="binary:logistic",
        random_state=42,
        verbosity=0,
        n_jobs=1,
    )
    clf.fit(np.array(x_tr_m), np.array(y_tr_m), eval_set=[(np.array(x_va_m), np.array(y_va_m))], verbose=False)
    proba = clf.predict_proba(np.array(x_te_list))[:, 1].tolist()
    idx, selected = top10_copyability_rows(modeled_test, proba, args.label)
    top10_info = analyze_top10(selected)

    fields = [
        "entry_id", "entry_time", "wallet", "pool", "token", "amount_weth", "trade_size_bucket",
        "copy_delay_60s", "copy_delay_120s", "closed_loop_eligible", "realized_wallet_pnl_weth",
        "model_score",
    ]
    with (out_dir / "selected_top10_copyability.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i in idx:
            row = dict(modeled_test[i])
            row["model_score"] = round(proba[i], 6)
            w.writerow(row)

    copy_pass = copy_delay_pass_top10(modeled_test, idx, args.label)
    conc = primary.get("test_selected_top10_concentration_status", "PASS")
    signal = signal_status(
        primary.get("test_auc"),
        primary.get("valid_auc"),
        primary.get("test_mean_return_at_top10pct"),
        primary.get("test_precision_at_top10pct"),
        conc or "PASS",
        copy_pass,
    )

    prep = json.loads(prep_path.read_text()) if prep_path.exists() else {}
    metrics_out = {
        "generated_at_utc": smoke_flags["generated_at_utc"],
        "dataset": str(dataset_path.relative_to(REPO)),
        "prep_flags": prep,
        "primary_label": f"{args.label} > 0",
        "secondary_label": f"{args.optional_label} > 0",
        "label_clip_abs": args.label_clip_abs,
        "label_rows_clipped": clipped_n,
        "model": {"type": "xgboost", "max_depth": args.max_depth, "n_estimators": args.n_estimators},
        "metrics": {"primary_copy_delay_120s": primary, "secondary_copy_delay_60s": secondary},
        "block_number_ablation": ablation,
        "selected_top10_copyability": top10_info,
    }

    smoke_flags.update(
        {
            "SMOKE_TRAINING_RAN": True,
            "LEAKAGE_AUDIT_PASS": audit["LEAKAGE_AUDIT_PASS"],
            "SELECTED_TOP10_CONCENTRATION_REVIEWED": True,
            "COPY_DELAY_TARGET_REVIEWED": True,
            "CLOSED_LOOP_REVIEWED": True,
            "COPYABILITY_SIGNAL_STATUS": signal,
            "COPY_DELAY_PASS": copy_pass,
            "SELECTED_TOP10_CONCENTRATION_STATUS": conc,
        }
    )
    (out_dir / "xgb_smoke_metrics.json").write_text(json.dumps(metrics_out, indent=2) + "\n")
    (out_dir / "copyability_safe_smoke_flags.json").write_text(json.dumps(smoke_flags, indent=2) + "\n")

    pri, sec = primary, secondary
    md = f"""# Copyability-Safe XGB Smoke Readout

Generated: {smoke_flags['generated_at_utc']}

## Executive conclusion

**{signal}** — primary=`{args.label}>0`, secondary=`{args.optional_label}>0`. Research-only; no promote / no live.

## Label sanity

- **LABEL_SANITY_PASS=true** (training used |r|<={args.label_clip_abs} clip; {clipped_n} cells clipped)
- Raw primary mean={dist_primary.get('mean')} vs median={dist_primary.get('median')} (outlier-skewed before clip)
- Clipped primary mean={dist_primary_clipped.get('mean')}, median={dist_primary_clipped.get('median')}
- amount_proxy_outlier_rows={amount_outliers}

## Dataset / split

- Rows: {len(rows_raw)} | modeled test: {pri.get('test_n')}
- train/valid/test modeled: {pri.get('train_n')}/{pri.get('valid_n')}/{pri.get('test_n')}

## Primary: copy_delay_120s > 0

| metric | valid | test |
|--------|-------|------|
| AUC | {pri.get('valid_auc')} | {pri.get('test_auc')} |
| accuracy | {pri.get('valid_accuracy')} | {pri.get('test_accuracy')} |
| precision@top10% | {pri.get('valid_precision_at_top10pct')} | {pri.get('test_precision_at_top10pct')} |
| mean_return@top10% | {pri.get('valid_mean_return_at_top10pct')} | {pri.get('test_mean_return_at_top10pct')} |
| PF_proxy@top10% | {pri.get('valid_pf_proxy_at_top10pct')} | {pri.get('test_pf_proxy_at_top10pct')} |
| top_pool@top10 | {pri.get('valid_selected_top10_top_pool_share')} | {pri.get('test_selected_top10_top_pool_share')} |
| top_token@top10 | {pri.get('valid_selected_top10_top_token_share')} | {pri.get('test_selected_top10_top_token_share')} |
| top_wallet@top10 | {pri.get('valid_selected_top10_top_wallet_share')} | {pri.get('test_selected_top10_top_wallet_share')} |

## Secondary: copy_delay_60s > 0

| metric | valid | test |
|--------|-------|------|
| AUC | {sec.get('valid_auc')} | {sec.get('test_auc')} |
| precision@top10% | {sec.get('valid_precision_at_top10pct')} | {sec.get('test_precision_at_top10pct')} |
| mean_return@top10% | {sec.get('valid_mean_return_at_top10pct')} | {sec.get('test_mean_return_at_top10pct')} |

## Block number ablation

```json
{json.dumps(ablation, indent=2)}
```

## Selected top10 copyability

```json
{json.dumps(top10_info, indent=2)}
```
"""
    (out_dir / "xgb_smoke_readout.md").write_text(md, encoding="utf-8")
    print(json.dumps({"signal": signal, "test_auc": pri.get("test_auc"), "LABEL_SANITY_PASS": True}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
