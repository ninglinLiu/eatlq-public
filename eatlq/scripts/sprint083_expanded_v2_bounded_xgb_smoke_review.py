#!/usr/bin/env python3
"""Sprint083 expanded_v2 — bounded XGB smoke training review (research-only).

Does NOT promote models, touch live, or read secrets.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ALLOWED_MANIFEST_TAGS = frozenset({"ENTRY_TIME_VISIBLE", "AS_OF_WALLET_FEATURE"})
TIME_REGIME_FEATURES = frozenset({"block_number"})
SELECTED_TOP10_POOL_SHARE_WARN = 0.40
SELECTED_TOP10_POOL_SHARE_FAIL = 0.50
SELECTED_TOP10_TOKEN_SHARE_WARN = 0.50
SELECTED_TOP10_TOKEN_SHARE_FAIL = 0.65
SELECTED_TOP10_WALLET_SHARE_WARN = 0.25
FEATURE_DENY_EXPLICIT = frozenset(
    {
        "trade_size_bucket",
        "amount_usd_notional",
        "amount_weth",
        "realized_wallet_pnl",
        "tx_hash",
        "wallet",
        "pool",
        "token",
        "entry_id",
        "router_flag",
        "router_reason",
        "contract_flag",
        "deployer_flag",
        "mev_flag",
    }
)
LABEL_DENY_PREFIXES = ("future_return_", "copy_delay_")
SPLIT_TIME_COL = "entry_time"
PRIMARY_LABEL_COL = "future_return_30m"
OPTIONAL_LABEL_COL = "future_return_2h"
OPTIONAL_LABEL_MIN_COVERAGE = 0.70

DEFAULT_DATASET = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/dataset_sample.csv"
)
DEFAULT_MANIFEST = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/feature_manifest.json"
)
DEFAULT_PREP_FLAGS = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_dataset_builder_expanded_v2/training_prep_expanded_v2_flags.json"
)
DEFAULT_OUT = (
    "mvp/audits/sprint083_fee500_paper_shadow/"
    "v3_smart_wallet_bounded_xgb_smoke_expanded_v2"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bounded XGB smoke review (research-only)")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--prep-flags", default=DEFAULT_PREP_FLAGS)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--label", default=PRIMARY_LABEL_COL, help="return column for primary target")
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--n-estimators", type=int, default=50)
    p.add_argument("--learning-rate", type=float, default=0.05)
    p.add_argument("--train-frac", type=float, default=0.70)
    p.add_argument("--valid-frac", type=float, default=0.15)
    p.add_argument(
        "--include-block-number",
        action="store_true",
        help="Include block_number (TIME_REGIME_PROXY); default excludes for ablation-safe smoke.",
    )
    p.add_argument(
        "--run-block-number-ablation",
        action="store_true",
        help="Train/eval twice: without and with block_number (research-only).",
    )
    return p.parse_args()


def as_float(v) -> float | None:
    if v in (None, "", "\\N", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def load_manifest(path: Path) -> dict[str, str]:
    return json.loads(path.read_text())


def select_features(
    manifest: dict[str, str],
    include_block_number: bool = False,
) -> list[str]:
    feats: list[str] = []
    for col, tag in manifest.items():
        if tag not in ALLOWED_MANIFEST_TAGS:
            if not (include_block_number and col in TIME_REGIME_FEATURES):
                continue
        if col in TIME_REGIME_FEATURES and not include_block_number:
            continue
        if col in FEATURE_DENY_EXPLICIT or col == SPLIT_TIME_COL:
            continue
        if any(col.startswith(p) for p in LABEL_DENY_PREFIXES):
            continue
        if col in {PRIMARY_LABEL_COL, OPTIONAL_LABEL_COL}:
            continue
        feats.append(col)
    return sorted(feats)


def leakage_audit(
    manifest: dict[str, str],
    selected_features: list[str],
    all_columns: list[str],
) -> dict:
    violations: list[str] = []
    for f in selected_features:
        tag = manifest.get(f, "MISSING")
        if tag not in ALLOWED_MANIFEST_TAGS:
            violations.append(f"{f}: tag={tag}")
        if f in FEATURE_DENY_EXPLICIT:
            violations.append(f"{f}: explicit deny")
        if any(f.startswith(p) for p in LABEL_DENY_PREFIXES):
            violations.append(f"{f}: label prefix deny")
    for col in all_columns:
        if col in selected_features:
            continue
        if col.startswith("future_return_") or col.startswith("copy_delay_"):
            if col in selected_features:
                violations.append(f"label in features: {col}")
        if col in ("realized_wallet_pnl", "hold_time_sec", "max_drawdown"):
            if col in selected_features:
                violations.append(f"outcome in features: {col}")

    for col in all_columns:
        tag = manifest.get(col, "UNLISTED")
        if col in selected_features and tag == "FUTURE_LABEL":
            violations.append(f"FUTURE_LABEL used as feature: {col}")

    passed = len(violations) == 0
    return {
        "LEAKAGE_AUDIT_PASS": passed,
        "violations": violations,
        "selected_feature_count": len(selected_features),
        "selected_features": selected_features,
    }


def chrono_split(rows: list[dict], train_frac: float, valid_frac: float) -> tuple[list, list, list]:
    ordered = sorted(
        rows,
        key=lambda r: datetime.fromisoformat(r[SPLIT_TIME_COL].replace("Z", "+00:00")),
    )
    n = len(ordered)
    i_train = int(n * train_frac)
    i_valid = int(n * (train_frac + valid_frac))
    return ordered[:i_train], ordered[i_train:i_valid], ordered[i_valid:]


def label_coverage(rows: list[dict], col: str) -> float:
    if not rows:
        return 0.0
    ok = sum(1 for r in rows if as_float(r.get(col)) is not None)
    return ok / len(rows)


def build_matrix(
    rows: list[dict], features: list[str], label_col: str
) -> tuple[list[list[float]], list[int], list[float], list[str], list[str], list[str]]:
    x_rows: list[list[float]] = []
    y_bin: list[int] = []
    y_ret: list[float] = []
    pools: list[str] = []
    wallets: list[str] = []
    tokens: list[str] = []
    for r in rows:
        ret = as_float(r.get(label_col))
        if ret is None:
            continue
        vals: list[float] = []
        skip = False
        for f in features:
            v = as_float(r.get(f))
            if v is None:
                skip = True
                break
            vals.append(v)
        if skip:
            continue
        x_rows.append(vals)
        y_bin.append(1 if ret > 0 else 0)
        y_ret.append(ret)
        pools.append(str(r.get("pool", "")).lower())
        wallets.append(str(r.get("wallet", "")).lower())
        tokens.append(str(r.get("token", "")).lower())
    return x_rows, y_bin, y_ret, pools, wallets, tokens


def accuracy_at_threshold(y_true: list[int], scores: list[float], thr: float = 0.5) -> float:
    if not y_true:
        return 0.0
    correct = sum(1 for yt, s in zip(y_true, scores) if (1 if s >= thr else 0) == yt)
    return correct / len(y_true)


def top10_metrics(
    y_true: list[int],
    y_ret: list[float],
    scores: list[float],
    pools: list[str],
    wallets: list[str] | None = None,
    tokens: list[str] | None = None,
) -> dict:
    n = len(scores)
    if n == 0:
        return {
            "precision_at_top10pct": None,
            "mean_return_at_top10pct": None,
            "pf_proxy_at_top10pct": None,
            "selected_top10_n": 0,
            "selected_top10_top_pool_share": None,
        }
    k = max(1, int(math.ceil(n * 0.10)))
    idx = sorted(range(n), key=lambda i: scores[i], reverse=True)[:k]
    sel_y = [y_true[i] for i in idx]
    sel_ret = [y_ret[i] for i in idx]
    sel_pools = [pools[i] for i in idx if pools[i]]
    sel_wallets = [wallets[i] for i in idx if wallets and wallets[i]] if wallets else []
    sel_tokens = [tokens[i] for i in idx if tokens and tokens[i]] if tokens else []
    prec = sum(sel_y) / len(sel_y) if sel_y else None
    mean_ret = sum(sel_ret) / len(sel_ret) if sel_ret else None
    wins = sum(r for r in sel_ret if r > 0)
    losses = abs(sum(r for r in sel_ret if r < 0))
    pf = wins / losses if losses > 0 else (float("inf") if wins > 0 else None)
    pool_counts = Counter(sel_pools)
    top_pool_share = max(pool_counts.values()) / len(sel_pools) if sel_pools else None
    wallet_counts = Counter(sel_wallets)
    top_wallet_share = max(wallet_counts.values()) / len(sel_wallets) if sel_wallets else None
    token_counts = Counter(sel_tokens)
    top_token_share = max(token_counts.values()) / len(sel_tokens) if sel_tokens else None
    conc_status = concentration_gate_status(top_pool_share, top_token_share, top_wallet_share)
    return {
        "precision_at_top10pct": round(prec, 4) if prec is not None else None,
        "mean_return_at_top10pct": round(mean_ret, 6) if mean_ret is not None else None,
        "pf_proxy_at_top10pct": round(pf, 4) if pf is not None and math.isfinite(pf) else pf,
        "selected_top10_n": k,
        "selected_top10_top_pool_share": round(top_pool_share, 4) if top_pool_share is not None else None,
        "selected_top10_top_wallet_share": round(top_wallet_share, 4) if top_wallet_share is not None else None,
        "selected_top10_top_token_share": round(top_token_share, 4) if top_token_share is not None else None,
        "selected_top10_concentration_status": conc_status,
    }


def concentration_gate_status(
    top_pool: float | None,
    top_token: float | None,
    top_wallet: float | None,
) -> str:
    if top_pool is not None and top_pool >= SELECTED_TOP10_POOL_SHARE_FAIL:
        return "FAIL_REGIME_SPECIFIC"
    if top_token is not None and top_token >= SELECTED_TOP10_TOKEN_SHARE_FAIL:
        return "FAIL_REGIME_SPECIFIC"
    if top_pool is not None and top_pool >= SELECTED_TOP10_POOL_SHARE_WARN:
        return "WARN_REGIME_SPECIFIC"
    if top_token is not None and top_token >= SELECTED_TOP10_TOKEN_SHARE_WARN:
        return "WARN_REGIME_SPECIFIC"
    if top_wallet is not None and top_wallet >= SELECTED_TOP10_WALLET_SHARE_WARN:
        return "WARN_WALLET_CONCENTRATED"
    return "PASS"


def pf_proxy(returns: list[float]) -> float | None:
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses > 0:
        return wins / losses
    if wins > 0:
        return float("inf")
    return None


def train_eval_xgb(
    x_train,
    y_train,
    x_valid,
    y_valid,
    ret_valid,
    pools_valid,
    wallets_valid,
    tokens_valid,
    x_test,
    y_test,
    ret_test,
    pools_test,
    wallets_test,
    tokens_test,
    features: list[str],
    max_depth: int,
    n_estimators: int,
    learning_rate: float,
) -> dict:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    import xgboost as xgb

    xtr = np.array(x_train, dtype=np.float64)
    xva = np.array(x_valid, dtype=np.float64)
    xte = np.array(x_test, dtype=np.float64)
    ytr = np.array(y_train, dtype=np.int32)
    yva = np.array(y_valid, dtype=np.int32)
    yte = np.array(y_test, dtype=np.int32)

    if len(set(ytr.tolist())) < 2:
        raise ValueError("train set has single class")

    clf = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        verbosity=0,
        n_jobs=1,
    )
    clf.fit(xtr, ytr, eval_set=[(xva, yva)], verbose=False)

    def eval_split(x, y, ret, pools, wallets, tokens, prefix: str) -> dict:
        proba = clf.predict_proba(x)[:, 1].tolist()
        y_list = y.tolist()
        auc = None
        if len(set(y_list)) > 1:
            auc = float(roc_auc_score(y, proba))
        acc = accuracy_at_threshold(y_list, proba, 0.5)
        t10 = top10_metrics(y_list, ret, proba, pools, wallets, tokens)
        return {
            f"{prefix}_n": len(y_list),
            f"{prefix}_auc": round(auc, 4) if auc is not None else None,
            f"{prefix}_accuracy": round(acc, 4),
            **{f"{prefix}_{k}": v for k, v in t10.items()},
        }

    fi = {f: round(float(v), 6) for f, v in zip(features, clf.feature_importances_)}
    out = {
        "train_n": len(y_train),
        "valid_n": len(y_valid),
        "test_n": len(y_test),
        "feature_importance": fi,
    }
    out.update(eval_split(xva, yva, ret_valid, pools_valid, wallets_valid, tokens_valid, "valid"))
    out.update(eval_split(xte, yte, ret_test, pools_test, wallets_test, tokens_test, "test"))
    # Flatten primary test metrics for JSON contract
    out["auc"] = out.get("test_auc")
    out["accuracy"] = out.get("test_accuracy")
    out["precision_at_top10pct"] = out.get("test_precision_at_top10pct")
    out["mean_return_at_top10pct"] = out.get("test_mean_return_at_top10pct")
    out["pf_proxy_at_top10pct"] = out.get("test_pf_proxy_at_top10pct")
    out["selected_top10_n"] = out.get("test_selected_top10_n")
    out["selected_top10_top_pool_share"] = out.get("test_selected_top10_top_pool_share")
    return out


def write_split_csv(path: Path, train, valid, test) -> None:
    fields = ["split", "entry_time", "pool", "wallet", PRIMARY_LABEL_COL, OPTIONAL_LABEL_COL]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for name, part in (("train", train), ("valid", valid), ("test", test)):
            for r in part:
                w.writerow(
                    {
                        "split": name,
                        "entry_time": r.get(SPLIT_TIME_COL, ""),
                        "pool": r.get("pool", ""),
                        "wallet": r.get("wallet", ""),
                        PRIMARY_LABEL_COL: r.get(PRIMARY_LABEL_COL, ""),
                        OPTIONAL_LABEL_COL: r.get(OPTIONAL_LABEL_COL, ""),
                    }
                )


def write_readout(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def executive_conclusion(metrics: dict, leakage_pass: bool, smoke_ran: bool) -> str:
    if not leakage_pass:
        return "LEAKAGE_BLOCKED — 未执行训练。"
    if not smoke_ran:
        return "SMOKE_NOT_RUN — 数据或依赖不足。"
    pri = metrics.get("primary", {})
    test_auc = pri.get("test_auc") or pri.get("auc")
    prec10 = pri.get("test_precision_at_top10pct") or pri.get("precision_at_top10pct")
    mean10 = pri.get("test_mean_return_at_top10pct") or pri.get("mean_return_at_top10pct")
    if test_auc is None:
        return "INCONCLUSIVE — test AUC 无法计算（单类或样本不足）。"
    if test_auc < 0.52 and (prec10 or 0) <= 0.55:
        return (
            "WEAK_OR_NO_SIGNAL — test AUC 接近随机且 top10% 经济读数无优势；"
            "仅作 research clue，不可 promote / 不可 live。"
        )
    if test_auc >= 0.55 and (mean10 or 0) > 0:
        return (
            "MARGINAL_CLUE_ONLY — 存在弱可学习线索，但 smoke 非 OOS 签核；"
            "需 OOS 扩展或 paper forward，仍 NO_ALPHA_CLAIM。"
        )
    return (
        "MIXED — 分类指标一般；top10% 经济读数需结合集中度审慎解读；"
        "不可 promote / 不可 live。"
    )


def main() -> int:
    args = parse_args()
    dataset_path = REPO / args.dataset
    manifest_path = REPO / args.manifest
    prep_path = REPO / args.prep_flags
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(manifest_path)
    features = select_features(manifest, include_block_number=args.include_block_number)
    rows = list(csv.DictReader(dataset_path.open()))
    all_cols = list(rows[0].keys()) if rows else []

    audit = leakage_audit(manifest, features, all_cols)
    (out_dir / "selected_features.json").write_text(
        json.dumps(
            {
                "features": features,
                "manifest_tags": {f: manifest[f] for f in features},
                "leakage_audit": audit,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    decision = {
        "SMOKE_TRAINING_RAN": False,
        "LEAKAGE_AUDIT_PASS": audit["LEAKAGE_AUDIT_PASS"],
        "MODEL_PROMOTION_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "NEXT_STEP": "OOS_EXPANSION_OR_FEATURE_REPAIR_OR_PAPER_FORWARD",
        "LEAKAGE_BLOCKED": not audit["LEAKAGE_AUDIT_PASS"],
    }

    if not audit["LEAKAGE_AUDIT_PASS"]:
        decision["NEXT_STEP"] = "LEAKAGE_BLOCKED"
        (out_dir / "xgb_smoke_metrics.json").write_text(
            json.dumps({"decision": decision, "leakage_audit": audit}, indent=2) + "\n"
        )
        write_readout(
            out_dir / "xgb_smoke_readout.md",
            "# XGB Smoke Readout (expanded_v2)\n\n**LEAKAGE_BLOCKED** — training not run.\n\n"
            + json.dumps(audit, indent=2),
        )
        print(json.dumps(decision, indent=2))
        return 2

    train_rows, valid_rows, test_rows = chrono_split(rows, args.train_frac, args.valid_frac)
    write_split_csv(out_dir / "train_valid_test_split_summary.csv", train_rows, valid_rows, test_rows)

    cov_2h = label_coverage(rows, OPTIONAL_LABEL_COL)
    run_optional_2h = cov_2h >= OPTIONAL_LABEL_MIN_COVERAGE

    metrics_out: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset_path.relative_to(REPO)),
        "prep_flags": json.loads(prep_path.read_text()) if prep_path.exists() else {},
        "model": {
            "type": "xgboost",
            "max_depth": args.max_depth,
            "n_estimators": args.n_estimators,
            "learning_rate": args.learning_rate,
        },
        "split": {
            "method": "chronological_only",
            "train_frac": args.train_frac,
            "valid_frac": args.valid_frac,
            "train_n_raw": len(train_rows),
            "valid_n_raw": len(valid_rows),
            "test_n_raw": len(test_rows),
        },
        "primary_label": f"{args.label} > 0",
        "optional_label": f"{OPTIONAL_LABEL_COL} > 0" if run_optional_2h else None,
        "optional_label_coverage": round(cov_2h, 4),
        "metrics": {},
        "decision": decision,
    }

    try:
        def run_primary(feature_list: list[str], key: str) -> dict:
            x_tr, y_tr, _, _, _, _ = build_matrix(train_rows, feature_list, args.label)
            x_va, y_va, ret_va, pools_va, wal_va, tok_va = build_matrix(
                valid_rows, feature_list, args.label
            )
            x_te, y_te, ret_te, pools_te, wal_te, tok_te = build_matrix(
                test_rows, feature_list, args.label
            )
            if min(len(y_tr), len(y_va), len(y_te)) < 10:
                raise ValueError("insufficient labeled rows after feature null drop")
            return train_eval_xgb(
                x_tr,
                y_tr,
                x_va,
                y_va,
                ret_va,
                pools_va,
                wal_va,
                tok_va,
                x_te,
                y_te,
                ret_te,
                pools_te,
                wal_te,
                tok_te,
                feature_list,
                args.max_depth,
                args.n_estimators,
                args.learning_rate,
            )

        primary = run_primary(features, "primary")
        metrics_out["metrics"]["primary"] = primary
        metrics_out["feature_set"] = {
            "include_block_number": args.include_block_number,
            "features": features,
        }
        decision["SMOKE_TRAINING_RAN"] = True
        decision["SELECTED_TOP10_CONCENTRATION_STATUS"] = primary.get(
            "test_selected_top10_concentration_status"
        )
        if primary.get("test_selected_top10_concentration_status", "").startswith("FAIL"):
            decision["NEXT_STEP"] = "FEATURE_REPAIR_OR_REGIME_SPLIT_REQUIRED"
            decision["NO_ALPHA_CLAIM"] = True

        if args.run_block_number_ablation:
            feats_no_block = select_features(manifest, include_block_number=False)
            feats_with_block = select_features(manifest, include_block_number=True)
            metrics_out["metrics"]["ablation_without_block_number"] = run_primary(
                feats_no_block, "ablation_without_block_number"
            )
            metrics_out["metrics"]["ablation_with_block_number"] = run_primary(
                feats_with_block, "ablation_with_block_number"
            )
            no_blk = metrics_out["metrics"]["ablation_without_block_number"]
            with_blk = metrics_out["metrics"]["ablation_with_block_number"]
            metrics_out["block_number_ablation"] = {
                "test_auc_without": no_blk.get("test_auc"),
                "test_auc_with": with_blk.get("test_auc"),
                "auc_delta_with_minus_without": round(
                    (with_blk.get("test_auc") or 0) - (no_blk.get("test_auc") or 0), 4
                ),
                "regime_proxy_likely": (with_blk.get("test_auc") or 0)
                - (no_blk.get("test_auc") or 0)
                > 0.08,
            }

        if run_optional_2h:
            x_tr2, y_tr2, _, _, _, _ = build_matrix(train_rows, features, OPTIONAL_LABEL_COL)
            x_va2, y_va2, ret_va2, pools_va2, wal_va2, tok_va2 = build_matrix(
                valid_rows, features, OPTIONAL_LABEL_COL
            )
            x_te2, y_te2, ret_te2, pools_te2, wal_te2, tok_te2 = build_matrix(
                test_rows, features, OPTIONAL_LABEL_COL
            )
            if min(len(y_tr2), len(y_va2), len(y_te2)) >= 10 and len(set(y_tr2)) > 1:
                opt = train_eval_xgb(
                    x_tr2,
                    y_tr2,
                    x_va2,
                    y_va2,
                    ret_va2,
                    pools_va2,
                    wal_va2,
                    tok_va2,
                    x_te2,
                    y_te2,
                    ret_te2,
                    pools_te2,
                    wal_te2,
                    tok_te2,
                    features,
                    args.max_depth,
                    args.n_estimators,
                    args.learning_rate,
                )
                metrics_out["metrics"]["optional_future_return_2h"] = opt
    except Exception as e:
        metrics_out["error"] = str(e)
        metrics_out["traceback"] = traceback.format_exc()
        decision["SMOKE_TRAINING_RAN"] = False
        decision["NEXT_STEP"] = "SMOKE_RUN_FAILED"

    metrics_out["decision"] = decision
    metrics_out["executive_conclusion"] = executive_conclusion(
        metrics_out.get("metrics", {}),
        audit["LEAKAGE_AUDIT_PASS"],
        decision["SMOKE_TRAINING_RAN"],
    )

    (out_dir / "xgb_smoke_metrics.json").write_text(
        json.dumps(metrics_out, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    pri = metrics_out.get("metrics", {}).get("primary", {})
    md = f"""# XGB Smoke Readout — expanded_v2 (research-only)

Generated: {metrics_out['generated_at_utc']}

## Executive conclusion

{metrics_out['executive_conclusion']}

## Constraints

- Research-only smoke; **not** production training.
- `MODEL_PROMOTION_ALLOWED=false`, `LIVE_ALLOWED=false`, `NO_ALPHA_CLAIM=true`.

## Dataset / split

- Dataset: `{metrics_out['dataset']}`
- Split: chronological {args.train_frac:.0%} / {args.valid_frac:.0%} / test remainder
- Raw rows: train={len(train_rows)}, valid={len(valid_rows)}, test={len(test_rows)}
- Modeled rows (primary): train={pri.get('train_n')}, valid={pri.get('valid_n')}, test={pri.get('test_n')}

## Selected features ({len(features)})

{', '.join(features)}

## Primary target: `{args.label} > 0`

| metric | valid | test |
|--------|-------|------|
| AUC | {pri.get('valid_auc')} | {pri.get('test_auc')} |
| accuracy | {pri.get('valid_accuracy')} | {pri.get('test_accuracy')} |
| precision@top10% | {pri.get('valid_precision_at_top10pct')} | {pri.get('test_precision_at_top10pct')} |
| mean_return@top10% | {pri.get('valid_mean_return_at_top10pct')} | {pri.get('test_mean_return_at_top10pct')} |
| PF_proxy@top10% | {pri.get('valid_pf_proxy_at_top10pct')} | {pri.get('test_pf_proxy_at_top10pct')} |
| selected_top10_n | {pri.get('valid_selected_top10_n')} | {pri.get('test_selected_top10_n')} |
| top_pool_share@top10 | {pri.get('valid_selected_top10_top_pool_share')} | {pri.get('test_selected_top10_top_pool_share')} |

## Feature importance (primary, test model)

```json
{json.dumps(pri.get('feature_importance', {}), indent=2)}
```

## Decision flags

```json
{json.dumps(decision, indent=2)}
```
"""
    write_readout(out_dir / "xgb_smoke_readout.md", md)
    print(json.dumps({"decision": decision, "executive_conclusion": metrics_out["executive_conclusion"], "test_auc": pri.get("test_auc")}, indent=2))
    return 0 if decision["SMOKE_TRAINING_RAN"] else 1


if __name__ == "__main__":
    sys.exit(main())
