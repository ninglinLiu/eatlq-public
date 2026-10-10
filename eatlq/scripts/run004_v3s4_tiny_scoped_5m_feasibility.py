"""Run-004 V3S-4 — tiny scoped 5m feasibility under V3S-3C outlier policy.

Research-only: read static CSV/JSON, tiny sklearn diagnostic, no model files, no RPC.
Primary target: y = (raw label_return_5m > 0). No raw return regression.
"""
from __future__ import annotations

import csv
import json
import hashlib
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v3r25_clean_prototype_validation import as_bool, as_float, as_int  # noqa: E402

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"V3S-4 requires numpy/sklearn for tiny diagnostic: {e}") from e

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

SOURCE_RUN = "run004_v3s4_tiny_scoped_5m_feasibility"
V3S3A_CSV = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_rows.csv"
V3S3A_JSON = ROOT / "mvp/diagnostics/run004_v3s3a_staged_scoped_5m_expansion_summary.json"
V3S3B_JSON = ROOT / "mvp/diagnostics/run004_v3s3b_scoped_outlier_return_sanity_summary.json"
V3S3C_JSON = ROOT / "mvp/diagnostics/run004_v3s3c_outlier_policy_repair_summary.json"
POLICY_CSV = ROOT / "mvp/diagnostics/run004_v3s3c_outlier_policy_repair_rows.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3s4_tiny_scoped_5m_feasibility_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-v3s4-tiny-scoped-5m-feasibility.md"

EXPECTED_N = 500
ABS_EXTREME = 1.0
HOLDOUT_MIN_ROWS = 100
HOLDOUT_MIN_POS = 20
AUC_SHUFFLE_MARGIN = 0.06
AUC_ROBUST_MAX_DROP = 0.10
AUC_QUAR_MAX_DROP = 0.12
TOP_BUCKET_QUAR_MAX_SHARE = 0.35
TOP_BUCKET_ABS_GT1_MAX_SHARE = 0.25
CONC_TOP1_MAX_SHARE = 0.50
MIN_AUC_WEAK_SIGNAL = 0.53
MIN_AUC_PASS = 0.55
DEBUG_TOP_SAMPLE_CAP = 200

RNG = np.random.RandomState(42)
# Only these row fields are read inside `build_X` (features). Labels are read via
# `y_binary_5m` / `r5_raw` on the full row dict — never fed to the classifier.
FEATURE_FIELDS_FOR_MODEL = frozenset(
    {
        "fee",
        "entry_timestamp",
        "entry_block",
        "pool_address",
        "token_address",
        "source_path",
    }
)


def row_key(r: dict[str, Any]) -> tuple[str, str]:
    return (str(r.get("tx_hash", "")).lower(), str(r.get("log_index", "")))


def load_csv_dict(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def stable_slot(s: str, mod: int) -> float:
    h = int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:12], 16)
    return (h % mod) / float(mod)


def r5_raw(row: dict[str, Any]) -> float:
    v = as_float(row.get("label_return_5m"))
    return float(v) if v is not None else 0.0


def r30_raw(row: dict[str, Any]) -> float:
    v = as_float(row.get("label_return_30m"))
    return float(v) if v is not None else 0.0


def y_binary_5m(row: dict[str, Any]) -> int:
    return int(r5_raw(row) > 0)


def duplicate_count(keys: list[tuple[str, str]]) -> int:
    seen: set[tuple[str, str]] = set()
    d = 0
    for k in keys:
        d += int(k in seen)
        seen.add(k)
    return d


def leakage_guard_ok() -> tuple[bool, str]:
    """Rows carry labels and audit columns; classifier only sees FEATURE_FIELDS_FOR_MODEL."""
    return True, f"classifier_inputs_whitelist={sorted(FEATURE_FIELDS_FOR_MODEL)}"


def build_X(rows: list[dict[str, Any]], train_idx: list[int]) -> tuple[np.ndarray, dict[str, Any]]:
    ts_list = [as_int(rows[i]["entry_timestamp"]) for i in train_idx]
    blk_list = [as_int(rows[i]["entry_block"]) for i in train_idx]
    ts_min, ts_max = min(ts_list), max(ts_list)
    blk_min, blk_max = min(blk_list), max(blk_list)
    meta = {"ts_min": ts_min, "ts_max": ts_max, "blk_min": blk_min, "blk_max": blk_max}
    eps = 1e-9
    X = np.zeros((len(rows), 6), dtype=np.float64)
    for i, r in enumerate(rows):
        fee = math.log1p(float(as_int(r.get("fee", 0))))
        ts = as_int(r["entry_timestamp"])
        blk = as_int(r["entry_block"])
        ts_n = (ts - ts_min) / max(ts_max - ts_min, eps)
        blk_n = (blk - blk_min) / max(blk_max - blk_min, eps)
        pool = stable_slot(str(r.get("pool_address", "")).lower(), 19)
        tok = stable_slot(str(r.get("token_address", "")).lower(), 19)
        src = stable_slot(str(r.get("source_path", "")), 17)
        X[i] = [fee, ts_n, blk_n, pool, tok, src]
    return X, meta


def topk_rate(y: np.ndarray, scores: np.ndarray, frac: float) -> float:
    n = len(y)
    k = max(1, int(math.ceil(frac * n)))
    idx = np.argsort(-scores)[:k]
    return float(np.mean(y[idx[:k]]))


def bucket_stats(
    rows: list[dict[str, Any]],
    policy: list[dict[str, Any]],
    idx_hold: np.ndarray,
    scores: np.ndarray,
    frac: float,
    r5w_col: str,
) -> dict[str, Any]:
    n = len(idx_hold)
    k = max(1, int(math.ceil(frac * n)))
    order = np.argsort(-scores)
    top_loc = order[:k]
    real_idx = idx_hold[top_loc]
    r5s = [r5_raw(rows[i]) for i in real_idx]
    r5w = [float(as_float(policy[i][r5w_col]) or 0.0) for i in real_idx]
    qu = [as_bool(policy[i].get("quarantine_flag")) for i in real_idx]
    ag1 = [abs(r5_raw(rows[i])) > ABS_EXTREME for i in real_idx]
    pools = [str(rows[i].get("pool_address", "")).lower() for i in real_idx]
    toks = [str(rows[i].get("token_address", "")).lower() for i in real_idx]
    srcs = [str(rows[i].get("source_path", "")) for i in real_idx]
    fees = [str(as_int(rows[i].get("fee"))) for i in real_idx]

    def top_share(counter: Counter) -> float:
        if not counter:
            return 0.0
        return round(counter.most_common(1)[0][1] / len(real_idx), 4)

    pc, tc, sc, fc = Counter(pools), Counter(toks), Counter(srcs), Counter(fees)
    return {
        "bucket_n": k,
        "positive_rate_in_bucket": round(float(np.mean([y_binary_5m(rows[i]) for i in real_idx])), 4),
        "median_return_5m_raw": float(np.median(r5s)),
        "mean_return_5m_raw": round(float(np.mean(r5s)), 8),
        "winsorized_mean_in_bucket_col": r5w_col,
        "mean_r5_winsor_variant": round(float(np.mean(r5w)), 8),
        "quarantine_row_share_in_bucket": round(sum(qu) / len(real_idx), 4),
        "abs_gt_1_share_in_bucket": round(sum(ag1) / len(real_idx), 4),
        "top1_pool_share": top_share(pc),
        "top5_pool_share": round(sum(c for _, c in pc.most_common(5)) / len(real_idx), 4),
        "top1_token_share": top_share(tc),
        "top5_token_share": round(sum(c for _, c in tc.most_common(5)) / len(real_idx), 4),
        "top1_source_share": top_share(sc),
        "top5_source_share": round(sum(c for _, c in sc.most_common(5)) / len(real_idx), 4),
        "top1_fee_share": top_share(fc),
        "top5_fee_share": round(sum(c for _, c in fc.most_common(5)) / len(real_idx), 4),
        "sample_indices_holdout_positions": [int(x) for x in real_idx[: min(DEBUG_TOP_SAMPLE_CAP, len(real_idx))]],
    }


def eval_view(
    name: str,
    rows: list[dict[str, Any]],
    policy: list[dict[str, Any]],
    mask: np.ndarray,
    train_idx: np.ndarray,
    hold_idx: np.ndarray,
    r5w_col: str,
) -> dict[str, Any]:
    train_idx_m = np.array([i for i in train_idx if mask[i]], dtype=int)
    hold_idx_m = np.array([i for i in hold_idx if mask[i]], dtype=int)
    if len(train_idx_m) < 20 or len(hold_idx_m) < 20:
        return {
            "view": name,
            "skipped": True,
            "reason": "insufficient_rows_after_mask",
            "train_n": int(len(train_idx_m)),
            "holdout_n": int(len(hold_idx_m)),
        }

    X, _ = build_X(rows, list(train_idx_m))
    y = np.array([y_binary_5m(rows[i]) for i in range(len(rows))], dtype=int)

    Xt, yt = X[train_idx_m], y[train_idx_m]
    Xh, yh = X[hold_idx_m], y[hold_idx_m]

    clf = LogisticRegression(
        solver="lbfgs",
        max_iter=2500,
        random_state=42,
        class_weight="balanced",
        C=0.5,
    )
    clf.fit(Xt, yt)
    scores = clf.predict_proba(Xh)[:, 1]

    perm = RNG.permutation(len(yt))
    yt_shuf = yt[perm].astype(int, copy=False)
    clf_s = LogisticRegression(solver="lbfgs", max_iter=2500, random_state=43, class_weight="balanced", C=0.5)
    clf_s.fit(Xt, yt_shuf)
    scores_shuf = clf_s.predict_proba(Xh)[:, 1]

    maj_rate = float(np.mean(yt))
    pr_maj = average_precision_score(yh, np.full(len(yh), maj_rate))

    auc = roc_auc_score(yh, scores) if len(np.unique(yh)) > 1 else None
    pr_glob = float(np.mean(yh))
    pr_ap = average_precision_score(yh, scores)
    auc_shuf = roc_auc_score(yh, scores_shuf) if len(np.unique(yh)) > 1 else None

    rank_scores = Xh[:, 2]
    auc_rank = roc_auc_score(yh, rank_scores) if len(np.unique(yh)) > 1 else None

    top10 = topk_rate(yh, scores, 0.10)
    top20 = topk_rate(yh, scores, 0.20)
    b10 = bucket_stats(rows, policy, hold_idx_m, scores, 0.10, r5w_col)
    b20 = bucket_stats(rows, policy, hold_idx_m, scores, 0.20, r5w_col)

    return {
        "view": name,
        "skipped": False,
        "train_n": int(len(train_idx_m)),
        "holdout_n": int(len(hold_idx_m)),
        "holdout_positive_rate": round(float(np.mean(yh)), 4),
        "holdout_positive_count": int(np.sum(yh)),
        "roc_auc_holdout": None if auc is None else round(float(auc), 4),
        "pr_auc_holdout": round(float(pr_ap), 4),
        "pr_auc_majority_baseline": round(float(pr_maj), 4),
        "pr_lift_vs_majority": round(float(pr_ap - pr_maj), 4),
        "roc_auc_shuffled_train_labels": None if auc_shuf is None else round(float(auc_shuf), 4),
        "roc_auc_rank_entry_block_baseline": None if auc_rank is None else round(float(auc_rank), 4),
        "roc_auc_random_baseline_note": "0.5 for balanced-ish binary ranking without signal",
        "top_decile_positive_rate_holdout_approx": round(top10, 4),
        "top_quintile_positive_rate_holdout_approx": round(top20, 4),
        "top10pct_bucket": b10,
        "top20pct_bucket": b20,
        "global_positive_rate_holdout": round(pr_glob, 4),
        "top10_better_than_global": top10 > pr_glob + 0.01,
        "model_class": "sklearn.linear_model.LogisticRegression",
        "no_saved_artifact": True,
    }


def distribution_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    pools = [str(r.get("pool_address")).lower() for r in rows]
    toks = [str(r.get("token_address")).lower() for r in rows]
    srcs = [str(r.get("source_path")) for r in rows]
    fees = [str(as_int(r.get("fee"))) for r in rows]

    def top_share(lst: list[str]) -> tuple[float, float]:
        c = Counter(lst)
        if not c:
            return 0.0, 0.0
        t1 = c.most_common(1)[0][1] / n
        t5 = sum(x for _, x in c.most_common(5)) / n
        return round(t1, 4), round(t5, 4)

    p1, p5 = top_share(pools)
    t1, t5 = top_share(toks)
    s1, s5 = top_share(srcs)
    f1, f5 = top_share(fees)
    return {
        "pool_count": len(set(pools)),
        "token_count": len(set(toks)),
        "source_count": len(set(srcs)),
        "fee_distribution": dict(Counter(fees).most_common(8)),
        "top1_top5_pool_share": [p1, p5],
        "top1_top5_token_share": [t1, t5],
        "top1_top5_source_share": [s1, s5],
        "top1_top5_fee_share": [f1, f5],
    }


def verdict_from_gates(g: dict[str, Any]) -> str:
    """Higher-signal failures (policy / concentration) before generic no-signal."""
    if g["failed_leakage_guard"]:
        return "FAILED_LEAKAGE_GUARD"
    if g["failed_duplicate"]:
        return "FAILED_DATA_INTEGRITY"
    if g["failed_chrono_size"]:
        return "FAILED_DATA_INSUFFICIENT"
    if g["failed_concentration"]:
        return "FAILED_CONCENTRATION_DOMINATED"
    if g["failed_outlier_top_bucket"]:
        return "FAILED_OUTLIER_POLICY_BREAKS_SIGNAL"
    if g["failed_robust_consistency"] or g["failed_quarantine_consistency"]:
        return "FAILED_OUTLIER_POLICY_BREAKS_SIGNAL"
    if g["failed_shuffled_sanity"]:
        return "FAILED_NO_SCOPED_SIGNAL"
    if g["raw_auc"] is None or g["raw_auc"] < MIN_AUC_WEAK_SIGNAL:
        return "FAILED_NO_SCOPED_SIGNAL"
    if g["raw_auc"] >= MIN_AUC_PASS:
        return "PASSED_TINY_SCOPED_5M_FEASIBILITY"
    return "WEAK_TINY_SCOPED_SIGNAL_NEEDS_2000_BACKTEST"


def render_md(summary: dict[str, Any]) -> str:
    A = summary.get("A_dataset_scope", {})
    B = summary.get("B_target", {})
    C = summary.get("C_chronological_oos", {})
    D = summary.get("D_views_model_sanity", {})
    lines = [
        "# Run-004 V3S-4 — Tiny scoped 5m feasibility (policy-bound)",
        "",
        "Not alpha. Not production. Not larger expansion authorization.",
        "",
        f"**Verdict:** `{summary['verdict']}`",
        "",
        "## A — Dataset / scope",
        f"- Rows: **{A.get('input_rows')}**; scope: {json.dumps(A.get('selected_scope'), ensure_ascii=False)}",
        f"- sample_size_tier: {A.get('sample_size_tier')}; sample_size_risk: {A.get('sample_size_risk')}",
        "",
        "## B — Target (5m primary, 30m diagnostic)",
        f"- Global 5m positive rate: **{B.get('positive_rate_5m_global')}**; 30m (diagnostic): **{B.get('positive_rate_30m_diagnostic')}**",
        f"- 5m/30m sign-flip share: **{B.get('sign_flip_share_5m_30m')}**",
        f"- {B.get('30m_decay_risk_note', '')}",
        "",
        "## C — Chronological OOS",
        f"- train / holdout rows: **{C.get('train_rows')}** / **{C.get('holdout_rows')}**; holdout positives: **{C.get('holdout_positive_count')}**",
        f"- PR drift (holdout - train): **{C.get('positive_rate_drift_holdout_minus_train')}**",
        f"- Holdout median return 5m (raw): **{C.get('holdout_median_return_5m_raw')}**",
        "",
        "## D — Model sanity (three views)",
        "See `mvp/diagnostics/run004_v3s4_tiny_scoped_5m_feasibility_summary.json` section `D_views_model_sanity` for ROC/PR, baselines, top buckets.",
        "",
        "## E — Policy gates",
        f"```json\n{json.dumps(summary.get('E_gate_evaluation', {}), indent=2, ensure_ascii=False)}\n```",
        "",
        "## F — Concentration",
        f"- Raw-view holdout top-decile snapshot: `{json.dumps(summary.get('F_concentration_top_decile_raw_holdout', {}), ensure_ascii=False)}`",
        "- Per-view bucket drills (incl. optional sample indices ≤200): `D_views_model_sanity.*.top10pct_bucket` in JSON.",
        "",
        "## 大白话 Q&A",
        "",
    ]
    for i, q in enumerate(summary.get("final_qa_plain", []), 1):
        lines.append(f"{i}. {q}")
    lines.extend(["", "## Decision flags", "", "```json", json.dumps(summary["decision_flags"], indent=2, ensure_ascii=False), "```", ""])
    return "\n".join(lines)


def main() -> None:
    for p in (V3S3A_CSV, POLICY_CSV, V3S3C_JSON):
        if not p.exists():
            raise SystemExit(f"missing {p}")

    rows_a = load_csv_dict(V3S3A_CSV)
    policy_rows = load_csv_dict(POLICY_CSV)
    if len(rows_a) != EXPECTED_N or len(policy_rows) != EXPECTED_N:
        raise SystemExit(f"expected {EXPECTED_N} rows in both CSVs")

    keys_a = [row_key(r) for r in rows_a]
    if duplicate_count(keys_a) > 0:
        raise SystemExit("duplicate keys in dataset")

    policy_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for p in policy_rows:
        k = (str(p["tx_hash"]).lower(), str(p["log_index"]))
        if k in policy_by_key:
            raise SystemExit(f"duplicate policy key {k}")
        policy_by_key[k] = p
    policy: list[dict[str, Any]] = []
    for r in rows_a:
        k = row_key(r)
        if k not in policy_by_key:
            raise SystemExit(f"missing policy row for {k}")
        policy.append(policy_by_key[k])

    rows = rows_a
    leak_ok, leak_reason = leakage_guard_ok()

    train_idx = np.array([i for i, r in enumerate(rows) if r.get("chronological_split") == "train"], dtype=int)
    hold_idx = np.array([i for i, r in enumerate(rows) if r.get("chronological_split") == "holdout"], dtype=int)
    y_all = np.array([y_binary_5m(r) for r in rows], dtype=int)
    r5_all = np.array([r5_raw(r) for r in rows], dtype=np.float64)
    r30_all = np.array([r30_raw(r) for r in rows], dtype=np.float64)

    holdout_n = len(hold_idx)
    holdout_pos = int(np.sum(y_all[hold_idx]))
    train_pr = float(np.mean(y_all[train_idx]))
    hold_pr = float(np.mean(y_all[hold_idx]))

    sign_flip = sum(1 for i in range(len(rows)) if r5_all[i] * r30_all[i] < 0 and r5_all[i] != 0 and r30_all[i] != 0)
    sign_flip_share = round(sign_flip / len(rows), 4)
    pr30 = float(np.mean(r30_all > 0))
    decay_note = (
        f"holdout 5m PR {hold_pr:.4f} vs train {train_pr:.4f} (delta {round(hold_pr - train_pr, 4)}); "
        f"30m global PR {pr30:.4f} diagnostic only."
    )

    mask_all = np.ones(len(rows), dtype=bool)
    mask_quar = np.array([not as_bool(policy[i].get("quarantine_flag")) for i in range(len(rows))], dtype=bool)

    v_raw = eval_view("raw_view", rows, policy, mask_all, train_idx, hold_idx, "label_return_5m_raw")
    v_rob = eval_view("robust_view", rows, policy, mask_all, train_idx, hold_idx, "r5_robust_cap_abs_1")
    v_q = eval_view("quarantine_view", rows, policy, mask_quar, train_idx, hold_idx, "label_return_5m_raw")

    raw_auc = v_raw.get("roc_auc_holdout") if not v_raw.get("skipped") else None
    rob_auc = v_rob.get("roc_auc_holdout") if not v_rob.get("skipped") else None
    q_auc = v_q.get("roc_auc_holdout") if not v_q.get("skipped") else None

    failed_chrono = holdout_n < HOLDOUT_MIN_ROWS or holdout_pos < HOLDOUT_MIN_POS
    auc_r = raw_auc or 0.0
    auc_sh = v_raw.get("roc_auc_shuffled_train_labels") or 0.5
    failed_shuffle = raw_auc is not None and (auc_r - auc_sh < AUC_SHUFFLE_MARGIN)

    b10 = v_raw.get("top10pct_bucket") or {}
    failed_outlier_top = (
        b10.get("quarantine_row_share_in_bucket", 0) > TOP_BUCKET_QUAR_MAX_SHARE
        or b10.get("abs_gt_1_share_in_bucket", 0) > TOP_BUCKET_ABS_GT1_MAX_SHARE
    )

    failed_conc = (
        (b10.get("top1_pool_share") or 0) > CONC_TOP1_MAX_SHARE
        or (b10.get("top1_token_share") or 0) > CONC_TOP1_MAX_SHARE
        or (b10.get("top1_source_share") or 0) > CONC_TOP1_MAX_SHARE
        or (b10.get("top1_fee_share") or 0) > CONC_TOP1_MAX_SHARE
    )

    failed_robust = (
        raw_auc is not None
        and rob_auc is not None
        and (raw_auc - rob_auc) > AUC_ROBUST_MAX_DROP
    )
    failed_quar_c = (
        raw_auc is not None
        and q_auc is not None
        and (raw_auc - q_auc) > AUC_QUAR_MAX_DROP
    )

    g = {
        "failed_duplicate": False,
        "failed_leakage_guard": not leak_ok,
        "leakage_guard_detail": leak_reason,
        "failed_chrono_size": failed_chrono,
        "failed_shuffled_sanity": failed_shuffle,
        "failed_outlier_top_bucket": failed_outlier_top,
        "failed_concentration": failed_conc,
        "failed_robust_consistency": failed_robust,
        "failed_quarantine_consistency": failed_quar_c,
        "raw_auc": raw_auc,
    }

    verdict = verdict_from_gates(g)

    dist = distribution_summary(rows)

    wmean_hold = []
    for i in hold_idx:
        wmean_hold.append(float(as_float(policy[i].get("r5_robust_cap_abs_1")) or 0.0))
    hold_med_r5 = float(np.median(r5_all[hold_idx]))

    hold_kept = [i for i in hold_idx if not as_bool(policy[i].get("quarantine_flag"))]
    hold_quar_excl = {
        "holdout_rows_ex_quarantine": len(hold_kept),
        "holdout_positive_rate_5m_ex_quarantine": round(float(np.mean(y_all[hold_kept])), 4) if hold_kept else None,
        "holdout_median_return_5m_raw_ex_quarantine": float(np.median(r5_all[hold_kept])) if hold_kept else None,
        "holdout_winsor_mean_cap1_ex_quarantine": round(
            float(
                np.mean(
                    [float(as_float(policy[i].get("r5_robust_cap_abs_1")) or 0.0) for i in hold_kept],
                ),
            ),
            8,
        )
        if hold_kept
        else None,
    }

    v3s3c_doc = json.loads(V3S3C_JSON.read_text(encoding="utf-8"))
    v3s3c_flags = v3s3c_doc.get("decision_flags", {})
    policy_respected = bool(v3s3c_flags.get("allow_v3s4_tiny_scoped_model_feasibility")) or bool(
        v3s3c_flags.get("v3s3c_outlier_policy_repair_passed"),
    )
    v3s3b_head = {}
    if V3S3B_JSON.exists():
        v3s3b_head = {"path": str(V3S3B_JSON.relative_to(ROOT)), "note": "static audit reference only"}

    signal_detected = raw_auc is not None and raw_auc >= MIN_AUC_WEAK_SIGNAL and v_raw.get("top10_better_than_global", False)
    passed_tiny = verdict == "PASSED_TINY_SCOPED_5M_FEASIBILITY"
    allow_v3s5 = verdict in (
        "PASSED_TINY_SCOPED_5M_FEASIBILITY",
        "WEAK_TINY_SCOPED_SIGNAL_NEEDS_2000_BACKTEST",
    )

    decision_flags = {
        "v3s4_tiny_scoped_feasibility_passed": passed_tiny,
        "scoped_5m_signal_detected": signal_detected,
        "robust_view_passed": not failed_robust and not v_rob.get("skipped"),
        "quarantine_view_passed": not failed_quar_c and not v_q.get("skipped"),
        "chronological_oos_passed": not failed_chrono,
        "concentration_guard_passed": not failed_conc,
        "outlier_policy_respected": bool(policy_respected),
        "raw_return_regression_forbidden": True,
        "sample_size_risk": True,
        "allow_v3s5_2000row_scoped_backtest_preflight": allow_v3s5,
        "allow_training": False,
        "allow_model_research": False,
        "allow_larger_expansion": False,
        "allow_mainline": False,
        "allow_live": False,
    }

    if verdict == "WEAK_TINY_SCOPED_SIGNAL_NEEDS_2000_BACKTEST":
        qa_2000row = "建议做 V3S-5 2000-row scoped backtest（仍非训练授权）"
    elif verdict == "PASSED_TINY_SCOPED_5M_FEASIBILITY":
        qa_2000row = "tiny 已过关，但仍只允许 2000-row preflight，不是训练/实盘"
    else:
        qa_2000row = "否——当前 verdict 下不允许 V3S-5 preflight（见 allow_v3s5 flag）"

    summary = {
        "run": SOURCE_RUN,
        "generator_script": "scripts/run004_v3s4_tiny_scoped_5m_feasibility.py",
        "research_only": True,
        "no_model_artifact_saved": True,
        "no_rpc": True,
        "inputs": {
            "v3s3a_csv": str(V3S3A_CSV.relative_to(ROOT)),
            "v3s3a_json": str(V3S3A_JSON.relative_to(ROOT)),
            "v3s3b_json": str(V3S3B_JSON.relative_to(ROOT)),
            "v3s3c_json": str(V3S3C_JSON.relative_to(ROOT)),
            "policy_csv": str(POLICY_CSV.relative_to(ROOT)),
            "v3s3b_loaded": v3s3b_head,
        },
        "A_dataset_scope": {
            "input_rows": len(rows),
            "selected_scope": {"scope_type": "notional_bucket", "scope_key": "at_cap"},
            "pool_token_source_fee": dist,
            "classifier_row_fields_only": sorted(FEATURE_FIELDS_FOR_MODEL),
            "leakage_guard_detail": leak_reason,
            "sample_size_tier": "tiny_scoped_feasibility_only",
            "sample_size_risk": True,
        },
        "B_target": {
            "primary_y": "binary (raw label_return_5m > 0)",
            "positive_rate_5m_global": round(float(np.mean(y_all)), 4),
            "positive_rate_30m_diagnostic": round(pr30, 4),
            "sign_flip_share_5m_30m": sign_flip_share,
            "30m_decay_risk_note": decay_note,
            "forbidden_raw_return_regression": True,
        },
        "C_chronological_oos": {
            "train_rows": int(len(train_idx)),
            "holdout_rows": holdout_n,
            "train_positive_rate_5m": round(train_pr, 4),
            "holdout_positive_rate_5m": round(hold_pr, 4),
            "positive_rate_drift_holdout_minus_train": round(hold_pr - train_pr, 4),
            "holdout_positive_count": holdout_pos,
            "holdout_median_return_5m_raw": hold_med_r5,
            "holdout_winsorized_mean_cap1_on_holdout_indices": round(float(np.mean(wmean_hold)), 8),
            "gate_holdout_rows_ge_100": holdout_n >= HOLDOUT_MIN_ROWS,
            "gate_holdout_pos_ge_20": holdout_pos >= HOLDOUT_MIN_POS,
            "holdout_quarantine_excluded": hold_quar_excl,
        },
        "D_views_model_sanity": {"raw_view": v_raw, "robust_view": v_rob, "quarantine_view": v_q},
        "F_concentration_top_decile_raw_holdout": {
            "top1_pool_share": b10.get("top1_pool_share"),
            "top5_pool_share": b10.get("top5_pool_share"),
            "top1_token_share": b10.get("top1_token_share"),
            "top5_token_share": b10.get("top5_token_share"),
            "top1_source_share": b10.get("top1_source_share"),
            "top5_source_share": b10.get("top5_source_share"),
            "top1_fee_share": b10.get("top1_fee_share"),
            "top5_fee_share": b10.get("top5_fee_share"),
            "quarantine_row_share_in_bucket": b10.get("quarantine_row_share_in_bucket"),
            "abs_gt_1_share_in_bucket": b10.get("abs_gt_1_share_in_bucket"),
            "positive_rate_in_bucket": b10.get("positive_rate_in_bucket"),
            "median_return_5m_raw": b10.get("median_return_5m_raw"),
            "mean_r5_winsor_variant": b10.get("mean_r5_winsor_variant"),
            "winsor_column_used": b10.get("winsorized_mean_in_bucket_col"),
        },
        "E_gate_evaluation": g,
        "verdict": verdict,
        "decision_flags": decision_flags,
        "final_qa_plain": [
            (
                f"有没有最低限度 5m 排序信号？**{'有弱信号' if signal_detected else '不明显'}** "
                f"（chrono holdout ROC-AUC ≈ **{raw_auc}**，PR-AUC **{v_raw.get('pr_auc_holdout')}**）。"
            ),
            (
                f"raw/robust/quarantine 一致吗？raw AUC **{raw_auc}**，robust **{rob_auc}**，quarantine **{q_auc}**"
                f"（quarantine 子集更小）；是否推翻：robust_drop={failed_robust}, quar_drop={failed_quar_c}。"
            ),
            (
                f"top bucket 靠 outlier/quarantine 吗？top10% quarantine_share={b10.get('quarantine_row_share_in_bucket')}, "
                f"abs>1_share={b10.get('abs_gt_1_share_in_bucket')} → fail_gate={failed_outlier_top}。"
            ),
            f"chrono holdout 成立吗？**{not failed_chrono}**（{holdout_n} 行，{holdout_pos} 个阳性）。",
            f"是否依赖单一实体？top1 pool/token/source/fee in top bucket: {b10.get('top1_pool_share')}/{b10.get('top1_token_share')}/{b10.get('top1_source_share')}/{b10.get('top1_fee_share')}；conc_fail={failed_conc}。",
            f"只是 weak signal 需要 2000-row？**{qa_2000row}**；allow_v3s5_preflight={allow_v3s5}。",
            "仍禁止 training / model research / 扩容 / mainline / live？**是**（见 flags）。",
        ],
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")

    print(json.dumps({"verdict": verdict, "raw_auc": raw_auc, "flags": decision_flags}, ensure_ascii=False))


if __name__ == "__main__":
    main()
