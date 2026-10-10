"""
Run-004 Patch F (B): v2-only prepare dry-run — no RPC, no re-label, no official prepare_dataset / retrain.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    k = (n - 1) * q
    lo = int(k)
    hi = min(lo + 1, n - 1)
    w = k - lo
    return float(sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w)


def _short_tx(tx: str) -> str:
    t = (tx or "").strip()
    if len(t) > 16:
        return f"{t[:10]}..{t[-4:]}"
    return t or "(empty)"


def is_succ_row(r: dict[str, str]) -> bool:
    try:
        lb = int(r.get("label", "-1"))
    except (TypeError, ValueError):
        return False
    return (r.get("label_reason") or "") == "executable" and lb in (0, 1)


def pnet(r: dict[str, str]) -> float | None:
    try:
        v = float((r.get("net_return") or "").strip() or "nan")
        if v != v:  # nan
            return None
        return v
    except (TypeError, ValueError):
        return None


def detect_key_conflicts(vs: list[dict[str, str]], net_eps: float) -> bool:
    succs = [v for v in vs if is_succ_row(v)]
    for i in range(len(succs)):
        for j in range(i + 1, len(succs)):
            a, b = succs[i], succs[j]
            if int(a["label"]) != int(b["label"]):
                return True
            na, nb = pnet(a), pnet(b)
            if na is None or nb is None:
                return True
            if abs(na - nb) > net_eps:
                return True
    return False


def _pick_row_for_key(vs: list[dict[str, str]], net_eps: float) -> tuple[dict[str, str] | None, str, bool]:
    """Return (chosen_row, status, has_conflict)."""
    if detect_key_conflicts(vs, net_eps):
        return None, "conflict", True
    succs = [v for v in vs if is_succ_row(v)]
    if succs:
        return succs[0], "use_first_success", False
    return vs[0], "failure_only", False  # for audit; not used in prepared set


REQUIRED_STR = ("pool_address", "token_address", "tx_hash", "log_index")
REQUIRED_NUM = ("final_score", "timestamp")


def row_features_ok(
    r: dict[str, str],
) -> tuple[bool, str | None]:
    for c in REQUIRED_STR:
        if not (r.get(c) or "").strip():
            return False, f"empty_{c}"
    try:
        ts = int(r.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return False, "parse_timestamp"
    if ts == 0:
        return False, "bad_ts"
    try:
        float(r.get("final_score", 0) or 0)
    except (TypeError, ValueError):
        return False, "parse_final_score"
    if not is_succ_row(r):
        return False, "not_success"
    nr = pnet(r)
    if nr is None or not math.isfinite(nr):
        return False, "bad_net"
    return True, None


def _prepare_concl(
    recon_ok: bool,
    prep_dup_conflicts: int,
    exception: str | None,
    schema_err: str | None,
    gates: dict[str, bool],
) -> str:
    if exception:
        return "HOLD_DUE_TO_PREPARE_EXCEPTION"
    if schema_err:
        return "HOLD_DUE_TO_SCHEMA_MISMATCH"
    if prep_dup_conflicts > 0:
        return "HOLD_DUE_TO_DUPLICATE_CONFLICT"
    if not recon_ok:
        return "HOLD_DUE_TO_RECONCILIATION_FAILURE"
    g = gates
    if not (g.get("null_ok") and g.get("finite_ok")):
        return "HOLD_DUE_TO_FEATURE_QUALITY"
    if not g.get("usable_rows_ok"):
        return "HOLD_DUE_TO_FEATURE_QUALITY"
    if not g.get("pos_rate_ok"):
        return "HOLD_DUE_TO_LABEL_DISTRIBUTION"
    if not (g.get("top1_ok") and g.get("top5_ok")):
        return "HOLD_DUE_TO_POOL_CONCENTRATION"
    if not g.get("holdout_ok"):
        return "HOLD_DUE_TO_HOLDOUT_INSUFFICIENT"
    return "PATCH_F_GO_TO_RETRAIN_DRYRUN"


def main() -> int:
    csv_in = Path(os.environ.get("RUN004_F_E2_CSV", str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv")))
    recon_path = Path(
        os.environ.get("RUN004_F_RECON_JSON", str(REPO / "mvp" / "diagnostics" / "run004_patch_f_e2_reconciliation.json")),
    )
    out_csv = Path(
        os.environ.get("RUN004_F_PREPARE_OUT_CSV", str(REPO / "logs" / "run004_v2only_prepared_dryrun.csv")),
    )
    out_json = Path(
        os.environ.get(
            "RUN004_F_PREPARE_OUT_JSON",
            str(REPO / "mvp" / "diagnostics" / "run004_patch_f_v2only_prepare_dryrun_report.json"),
        ),
    )
    out_md = Path(
        os.environ.get("RUN004_F_PREPARE_OUT_MD", str(REPO / "mvp" / "runs" / "run-004-patch-f-v2only-prepare-dryrun.md")),
    )
    net_eps = float(os.environ.get("RUN004_F_DUP_NET_EPS", "1e-4") or 1e-4)
    ms = int(86400 * 7 * 1000)  # 7d ms

    min_use = int(os.environ.get("RUN004_F_MIN_USABLE", "10000") or 10000)
    pr_lo = float(os.environ.get("RUN004_F_MIN_POS", "0.10") or 0.1)
    pr_hi = float(os.environ.get("RUN004_F_MAX_POS", "0.90") or 0.9)
    top1_m = float(os.environ.get("RUN004_F_MAX_TOP1", "0.35") or 0.35)
    top5_m = float(os.environ.get("RUN004_F_MAX_TOP5", "0.65") or 0.65)
    max_null = float(os.environ.get("RUN004_F_MAX_NULL_RATE", "0.02") or 0.02)
    holdout_min = int(os.environ.get("RUN004_F_HOLDOUT_MIN", "500") or 500)
    hpos = int(os.environ.get("RUN004_F_HOLDOUT_MIN_POS", "50") or 50)
    hneg = int(os.environ.get("RUN004_F_HOLDOUT_MIN_NEG", "50") or 50)

    recon_ok = True
    recon_c = "UNKNOWN"
    if recon_path.is_file():
        try:
            rj = json.loads(recon_path.read_text(encoding="utf-8"))
            recon_c = str(rj.get("conclusion", ""))
            recon_ok = recon_c == "PATCH_F_RECON_GO"
        except Exception:
            recon_ok = False
            recon_c = "HOLD"
    else:
        recon_ok = False

    exception: str | None = None
    schema_err: str | None = None
    prep_conflicts = 0
    out_rows: list[dict[str, str]] = []
    dropped: Counter[str] = Counter()
    audit_failure_rows = 0

    rows: list[dict[str, str]] = []
    by: dict[tuple[str, str], list[dict[str, str]]] = {}
    raw_input = 0
    uq = 0
    dup_collapsed = 0
    try:
        with csv_in.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                schema_err = "empty_header"
            else:
                rows = list(reader)
    except Exception as e:
        exception = f"read_input:{e!r}"
        rows = []

    if exception:
        # write minimal
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out = {"conclusion": "HOLD_DUE_TO_PREPARE_EXCEPTION", "error": exception}
        out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return 1

    if not exception and not rows:
        schema_err = schema_err or "no_rows"
    if not schema_err and not exception:
        b = defaultdict(list)
        for r in rows:
            b[(r.get("tx_hash", ""), r.get("log_index", ""))].append(r)
        by = dict(b)
        raw_input = len(rows)
        uq = len(by)
        dup_collapsed = max(0, raw_input - uq)
        for _k, vs in sorted(by.items(), key=lambda x: (x[0][0], x[0][1])):
            chosen, st, cfl = _pick_row_for_key(vs, net_eps)
            if cfl:
                prep_conflicts += 1
                dropped["duplicate_conflict"] += len(vs)
                continue
            if st == "failure_only":
                audit_failure_rows += 1
                dropped["all_failure_key"] += 1
                continue
            if chosen and is_succ_row(chosen):
                out_rows.append(chosen)
            else:
                dropped["not_success"] += 1

    # Feature filtering on out_rows
    prepared: list[dict[str, str]] = []
    for r in out_rows:
        ok, why = row_features_ok(r)
        if ok:
            prepared.append(r)
        else:
            if why in ("not_success",):
                dropped["dropped_not_success"] += 1
            elif "empty" in (why or ""):
                dropped["dropped_missing_feature"] += 1
            else:
                dropped["dropped_non_finite"] += 1

    def _ilab(r: dict[str, str]) -> int:
        try:
            return int(r.get("label", -1) or -1)
        except (TypeError, ValueError):
            return -1

    usable = len(prepared)
    posc = sum(1 for r in prepared if _ilab(r) == 1)
    negc = sum(1 for r in prepared if _ilab(r) == 0)
    prate = (posc / usable) if usable else None

    nets = sorted(pnet(r) for r in prepared if pnet(r) is not None)
    net_d: dict[str, Any] = (
        {
            "min": round(_pct(nets, 0.0), 12) if nets else None,
            "p25": round(_pct(nets, 0.25), 12) if nets else None,
            "median": round(_pct(nets, 0.5), 12) if nets else None,
            "p75": round(_pct(nets, 0.75), 12) if nets else None,
            "p95": round(_pct(nets, 0.95), 12) if nets else None,
            "max": round(_pct(nets, 1.0), 12) if nets else None,
            "mean": round(sum(nets) / len(nets), 12) if nets else None,
        }
        if nets
        else {k: None for k in ("min", "p25", "median", "p75", "p95", "max", "mean")}
    )

    # per-feature null / non-finite (non-finite only for numeric training fields)
    feat_cols = list({*REQUIRED_STR, *REQUIRED_NUM, "net_return"})
    null_rate: dict[str, float] = {}
    nonfinite: dict[str, int] = {}
    for c in feat_cols:
        nnull = 0
        nbad = 0
        for r in prepared:
            v = r.get(c, "")
            if v is None or (isinstance(v, str) and not str(v).strip()):
                nnull += 1
            elif c in ("tx_hash", "pool_address", "token_address"):
                pass
            else:
                try:
                    if c == "timestamp":
                        x = float(int(r.get("timestamp", 0) or 0))
                    elif c == "log_index":
                        x = float(int(str(r.get("log_index", "0")).split(".", 1)[0]))
                    else:
                        x = float(r.get(c, 0) or 0)
                    if not math.isfinite(x):
                        nbad += 1
                except (TypeError, ValueError):
                    nnull += 1
        null_rate[c] = round(nnull / max(1, len(prepared)), 6)
        nonfinite[c] = nbad

    # constant / near-constant: std dev of numeric features
    near_const: list[str] = []
    for c in ("final_score", "net_return", "timestamp"):
        vals: list[float] = []
        for r in prepared:
            try:
                if c == "timestamp":
                    vals.append(float(int(r.get("timestamp", 0) or 0)))
                else:
                    vals.append(float(r.get(c, 0) or 0))
            except (TypeError, ValueError):
                pass
        if len(set(vals)) <= 1 and vals:
            near_const.append(f"{c}_constant")
        elif vals:
            m = sum(vals) / len(vals)
            v = sum((x - m) ** 2 for x in vals) / len(vals)
            if (v**0.5) < 1e-12 and c != "timestamp":
                near_const.append(f"{c}_near_constant")

    pool_c = Counter((r.get("pool_address") or "").lower() for r in prepared)
    tok_c = Counter((r.get("token_address") or "").lower() for r in prepared)
    pos_p = Counter()
    neg_p = Counter()
    for r in prepared:
        p = (r.get("pool_address") or "").lower()
        if _ilab(r) == 1:
            pos_p[p] += 1
        else:
            neg_p[p] += 1

    counts = sorted([c for c in pool_c.values() if c > 0], reverse=True)
    top1s = (counts[0] / usable) if counts and usable else 0.0
    top5s = (sum(counts[:5]) / usable) if usable else 0.0

    top_pools = pool_c.most_common(20)
    top_tok = tok_c.most_common(20)
    tss = [int(r.get("timestamp", 0) or 0) for r in prepared if int(r.get("timestamp", 0) or 0) > 0]
    tmin, tmax = (min(tss), max(tss)) if tss else (0, 0)
    h_lo = tmax - ms if tmax else 0
    hold = [r for r in prepared if tmax and int(r.get("timestamp", 0) or 0) >= h_lo]
    train = [r for r in prepared if int(r.get("timestamp", 0) or 0) < h_lo]
    hposc = sum(1 for r in hold if _ilab(r) == 1)
    hnegc = len(hold) - hposc
    tposc = sum(1 for r in train if _ilab(r) == 1)
    tnegc = len(train) - tposc

    hpools = {r.get("pool_address", "").lower() for r in hold}
    tpools = {r.get("pool_address", "").lower() for r in train}
    htoks = {r.get("token_address", "").lower() for r in hold}
    ttoks = {r.get("token_address", "").lower() for r in train}
    pool_ol = len(hpools & tpools) / max(1, len(hpools | tpools)) if (hpools or tpools) else 0.0
    tok_ol = len(htoks & ttoks) / max(1, len(htoks | ttoks)) if (htoks or ttoks) else 0.0

    h_pp = Counter((r.get("pool_address") or "").lower() for r in hold if _ilab(r) == 1)
    h_top1p = 0.0
    if hposc > 0 and h_pp:
        h_top1p = h_pp.most_common(1)[0][1] / hposc
    one_pool_dominates_holdout_pos = bool(hposc and h_top1p > 0.5)

    req_features = [c for c in ("final_score", "timestamp", "net_return", "token_address", "pool_address") if c in feat_cols]
    null_ok = all(null_rate.get(c, 0) <= max_null for c in req_features) if prepared else False
    finite_ok = all(nonfinite.get(c, 0) == 0 for c in req_features) if prepared else False
    if not prepared:
        null_ok, finite_ok = True, True

    gates: dict[str, bool] = {
        "usable_rows_ok": usable >= min_use,
        "pos_rate_ok": (prate is not None) and (pr_lo <= prate <= pr_hi),
        "null_ok": null_ok,
        "finite_ok": finite_ok,
        "top1_ok": top1s <= top1_m,
        "top5_ok": top5s <= top5_m,
        "holdout_ok": (len(hold) >= holdout_min) and (hposc >= hpos) and (hnegc >= hneg),
    }

    concl = _prepare_concl(recon_ok, prep_conflicts, None, schema_err, gates)

    def _wcsv(path: Path, p_list: list[dict[str, str]]) -> None:
        if not p_list:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = list(p_list[0].keys())
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(p_list)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if prepared:
        _wcsv(out_csv, prepared)
    elif rows:
        fields = list(rows[0].keys())
        with out_csv.open("w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()

    report: dict[str, Any] = {
        "conclusion": concl,
        "reconciliation_input_conclusion": recon_c,
        "reconciliation_ok": recon_ok,
        "raw_input_rows": raw_input,
        "unique_input_keys": uq,
        "duplicate_rows_collapsed": dup_collapsed,
        "duplicate_conflict_count": prep_conflicts,
        "usable_prepared_rows": usable,
        "dropped_failure_rows": int(
            dropped.get("all_failure_key", 0)
            + dropped.get("not_success", 0)
            + dropped.get("dropped_not_success", 0)
        ),
        "dropped_missing_feature_rows": int(dropped.get("dropped_missing_feature", 0)),
        "dropped_non_finite_rows": int(dropped.get("dropped_non_finite", 0)),
        "drop_counter": {k: int(v) for k, v in dropped.items()},
        "positive_count": posc,
        "negative_count": negc,
        "positive_rate": round(float(prate), 6) if prate is not None else None,
        "net_return_distribution": net_d,
        "feature_null_rate": null_rate,
        "feature_non_finite_count": {k: nonfinite.get(k, 0) for k in feat_cols},
        "feature_constant_or_near_constant_warnings": near_const,
        "top1_pool_share": round(float(top1s), 6),
        "top5_pool_share": round(float(top5s), 6),
        "top_pools_by_total_rows": [[p, n] for p, n in top_pools],
        "top_pools_by_positive": [[p, n] for p, n in pos_p.most_common(20)],
        "top_pools_by_negative": [[p, n] for p, n in neg_p.most_common(20)],
        "token_concentration_top20": [[t, n] for t, n in top_tok],
        "time_range_ms": {"min": tmin, "max": tmax},
        "holdout_7d_ms": {"lo": h_lo, "hi": tmax},
        "latest_7d_holdout_candidate_rows": len(hold),
        "latest_7d_positive_count": hposc,
        "latest_7d_negative_count": hnegc,
        "train_candidate_rows": len(train),
        "train_positive_count": tposc,
        "train_negative_count": tnegc,
        "train_holdout_pool_overlap_rate": round(pool_ol, 6),
        "train_holdout_token_overlap_rate": round(tok_ol, 6),
        "one_pool_dominates_holdout_positives": one_pool_dominates_holdout_pos,
        "holdout_pos_top1_pool_share": round(float(h_top1p), 6),
        "gates": gates,
        "gate_values": {
            "min_usable": min_use, "pr_band": (pr_lo, pr_hi), "max_null": max_null, "max_top1": top1_m, "max_top5": top5_m,
            "holdout_min_rows": holdout_min, "holdout_min_pos": hpos, "holdout_min_neg": hneg,
        },
        "paths": {
            "input_csv": _rel(csv_in),
            "recon_json": _rel(recon_path) if recon_path.is_file() else str(recon_path),
            "out_csv": _rel(out_csv),
            "out_json": _rel(out_json),
            "out_md": _rel(out_md),
        },
    }
    if schema_err:
        report["schema_error"] = schema_err
        report["conclusion"] = "HOLD_DUE_TO_SCHEMA_MISMATCH"
    if exception:
        report["conclusion"] = "HOLD_DUE_TO_PREPARE_EXCEPTION"

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    md = f"""# Run-004 Patch F — v2-only prepare dry-run

**Conclusion:** `{report['conclusion']}` (recon input: `{recon_c}`)

**Gates & metrics:** see JSON: `{_rel(out_json)}`

**Output CSV:** `{_rel(out_csv)}` (usable rows: {usable})

**No RPC, no retrain, no promote, no LIVE, no `logs/labeled_dataset.csv`.**
"""
    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        p = REPO / "mvp" / "diagnostics" / "run004_patch_f_v2only_prepare_dryrun_report.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "conclusion": "HOLD_DUE_TO_PREPARE_EXCEPTION",
                    "error": str(e), "trace": traceback.format_exc()
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(traceback.format_exc(), file=sys.stderr)
        raise SystemExit(1) from e
