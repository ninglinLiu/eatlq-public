"""
Run-004 Patch E: v2-only label dry-run (no prepare/retrain, no v3/aero).

Reads logs/run004_candidates_swaponly_full.csv, filters trainable_candidate + pool_type=v2
+ Uniswap v2 swap topic0, calls label_dashcam_executable.label_v2 per row (archive RPC + ts grid).

Env:
  RUN004_E2_MODE=1  Patch E2: default outputs *_full* + conclusion PATCH_E2_*
  RUN004_CANDIDATES_CSV, RUN004_E_OUT_CSV, RUN004_E_OUT_JSON, RUN004_E_OUT_MD
  RUN004_E_MAX_ROWS (0 = all rows after filter)
  RUN004_E_STRATIFY=1  reuse Patch D-style stratify (uses RUN004_D_MAX_PER_POOL, *_TOP_POOLS, *_SAMPLE_ROWS, *_SEED)
  RUN004_E_RESUME=1  resume from existing out_csv: skip rows whose (tx_hash, log_index)
                     already appear in the file, and seed counters from those rows so the
                     final report reflects old + new combined.
  RUN004_E_REPORT_ONLY=1
                     do not call any RPC: read existing out_csv, rebuild counters and emit
                     the preliminary JSON+MD report. Used when the underlying RPC plan is
                     exhausted but partial labeled rows already exist. Conclusion is forced
                     to HOLD_DUE_TO_RPC_DAILY_LIMIT_RECOVERABLE.
  LABEL_RPC_URL or EVM_HTTP_URL, CLICKHOUSE_*, REDIS_URL, CHAIN_ID
  LABEL_EXIT_HORIZON_SEC, LABEL_NOTIONAL_WEI
  RUN004_E_GO_MIN_SUCCESS_RATE, RUN004_E_MAX_TOP1_SHARE, RUN004_E_MAX_TOP5_SHARE
  RUN004_E_MIN_POS_RATE, RUN004_E_MAX_POS_RATE
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import label_dashcam_executable as le
import validate_run004_executable_labels as vr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("label_run004_e")

TOPIC_V2 = vr.TOPIC_V2


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    k = (n - 1) * p
    lo = int(k)
    hi = min(lo + 1, n - 1)
    w = k - lo
    return float(sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w)


def _is_aero_type(pt: str) -> bool:
    return "aero" in (pt or "").lower()


def _rel_repo(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


_KNOWN_FAIL_REASONS = (
    "fail_low_liquidity",
    "fail_zero_tok_reserve",
    "fail_notional_gt_1pct",
    "fail_entry_quote_zero",
    "executable_exit_drained",
    "fail_sanity_clip",
    "fail_rpc_exception",
)
_PRECHECK_REASONS = (
    "missing_pool_address",
    "skipped_aero",
    "missing_pool_meta",
    "bad_decimals",
    "entry_before_pool_created",
)


def _seed_from_existing_csv(path: Path) -> dict[str, Any]:
    """Reconstruct counters from a partial out_csv so resumed runs report the union.

    Duplicate (tx_hash, log_index) rows can exist from earlier concurrent writers.
    Only the first occurrence of each key is folded into stats; later duplicates
    are tracked in `dup_rows` so the report can distinguish raw rows vs. unique events.
    """
    keys: set[tuple[str, str]] = set()
    fail_reason: Counter[str] = Counter()
    pool_calls: Counter[str] = Counter()
    pos_by_pool: Counter[str] = Counter()
    neg_by_pool: Counter[str] = Counter()
    fail_by_pool: Counter[str] = Counter()
    nets_ok: list[float] = []
    label_attempted = 0
    raw_rows = 0
    dup_rows = 0
    tmin_x, tmax_x = 10**30, 0

    with path.open("r", encoding="utf-8") as rf:
        for r in csv.DictReader(rf):
            raw_rows += 1
            tx = r.get("tx_hash", "")
            li = r.get("log_index", "")
            k = (tx, li)
            keyed = bool(tx or li)
            if keyed and k in keys:
                dup_rows += 1
                continue
            if keyed:
                keys.add(k)
            try:
                ts = int(r.get("timestamp", 0) or 0)
                if ts > 0:
                    tmin_x = min(tmin_x, ts)
                    tmax_x = max(tmax_x, ts)
            except Exception:
                pass
            try:
                lb = int(r.get("label", "-1"))
            except Exception:
                lb = -1
            reason = (r.get("label_reason") or "").strip()
            pool = (r.get("pool_address") or "").lower()
            pre = (r.get("pre_check") or "").strip()
            if pre == "ok":
                label_attempted += 1
                if pool:
                    pool_calls[pool] += 1
                if reason == "executable" and lb in (0, 1):
                    fail_reason["ok"] += 1
                    nv = r.get("net_return", "")
                    if nv != "":
                        try:
                            nets_ok.append(float(nv))
                        except Exception:
                            pass
                    if lb == 1:
                        if pool:
                            pos_by_pool[pool] += 1
                    elif pool:
                        neg_by_pool[pool] += 1
                else:
                    b = reason if reason in _KNOWN_FAIL_REASONS else "other"
                    fail_reason[b] += 1
                    if pool:
                        fail_by_pool[pool] += 1
            elif pre in ("invalid", "aero_excluded", "meta", "time"):
                if reason in _PRECHECK_REASONS:
                    fail_reason[reason] += 1
                    if reason != "missing_pool_address" and pool:
                        fail_by_pool[pool] += 1
                else:
                    fail_reason["other"] += 1
                    if pool:
                        fail_by_pool[pool] += 1
    return {
        "keys": keys,
        "fail_reason": fail_reason,
        "pool_calls": pool_calls,
        "pos_by_pool": pos_by_pool,
        "neg_by_pool": neg_by_pool,
        "fail_by_pool": fail_by_pool,
        "nets_ok": nets_ok,
        "label_attempted": label_attempted,
        "raw_rows": raw_rows,
        "dup_rows": dup_rows,
        "tmin_x": tmin_x,
        "tmax_x": tmax_x,
    }


def patch_e_conclusion(
    n_success: int,
    n_attempted: int,
    success_rate: float,
    pos_rate: float | None,
    top1: float,
    top5: float,
    go_min: float,
    top1_max: float,
    top5_max: float,
    pr_min: float,
    pr_max: float,
) -> str:
    if n_attempted <= 0:
        return "HOLD_DUE_TO_LABEL_FAILURE_RATE"
    if n_success < 1:
        return "HOLD_DUE_TO_LABEL_FAILURE_RATE"
    if success_rate < go_min:
        return "HOLD_DUE_TO_LABEL_FAILURE_RATE"
    if top1 > top1_max or top5 > top5_max:
        return "HOLD_DUE_TO_SAMPLE_CONCENTRATION_AFTER_LABEL"
    if pos_rate is not None and (pos_rate < pr_min or pos_rate > pr_max):
        return "HOLD_DUE_TO_LABEL_DISTRIBUTION_RISK"
    return "PATCH_E_GO_TO_PREPARE_DRYRUN"


def patch_e2_conclusion(
    n_success: int,
    n_attempted: int,
    success_rate: float,
    pos_rate: float | None,
    top1: float,
    top5: float,
    go_min: float,
    top1_max: float,
    top5_max: float,
    pr_min: float,
    pr_max: float,
) -> str:
    if n_attempted <= 0:
        return "HOLD_DUE_TO_FULL_LABEL_FAILURE_RATE"
    if n_success < 1:
        return "HOLD_DUE_TO_FULL_LABEL_FAILURE_RATE"
    if success_rate < go_min:
        return "HOLD_DUE_TO_FULL_LABEL_FAILURE_RATE"
    if top1 > top1_max or top5 > top5_max:
        return "HOLD_DUE_TO_SAMPLE_CONCENTRATION_AFTER_FULL_LABEL"
    if pos_rate is not None and (pos_rate < pr_min or pos_rate > pr_max):
        return "HOLD_DUE_TO_FULL_LABEL_DISTRIBUTION_RISK"
    return "PATCH_E2_GO_TO_PREPARE_DRYRUN"


def main() -> int:
    e2 = (os.environ.get("RUN004_E2_MODE", "").strip().lower() in ("1", "true", "yes", "on"))
    if e2:
        def_c = str(REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv")
        def_j = str(REPO / "mvp" / "diagnostics" / "run004_patch_e2_v2only_label_dryrun_full_report.json")
        def_m = str(REPO / "mvp" / "runs" / "run-004-patch-e2-v2only-label-dryrun-full.md")
    else:
        def_c = str(REPO / "logs" / "run004_v2only_labeled_dryrun.csv")
        def_j = str(REPO / "mvp" / "diagnostics" / "run004_patch_e_v2only_label_dryrun_report.json")
        def_m = str(REPO / "mvp" / "runs" / "run-004-patch-e-v2only-label-dryrun.md")
    csv_in = Path(os.environ.get("RUN004_CANDIDATES_CSV", str(REPO / "logs" / "run004_candidates_swaponly_full.csv")))
    out_csv = Path(os.environ.get("RUN004_E_OUT_CSV", def_c))
    out_json = Path(os.environ.get("RUN004_E_OUT_JSON", def_j))
    out_md = Path(os.environ.get("RUN004_E_OUT_MD", def_m))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_u = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    report_only = (os.environ.get("RUN004_E_REPORT_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    if not rpc_url and not report_only:
        log.error("LABEL_RPC_URL or EVM_HTTP_URL required")
        return 1

    max_rows = int(os.environ.get("RUN004_E_MAX_ROWS", "0") or 0)
    strat = (os.environ.get("RUN004_E_STRATIFY") or "").strip().lower() in ("1", "true", "yes", "on")
    resume = (os.environ.get("RUN004_E_RESUME") or "").strip().lower() in ("1", "true", "yes", "on")
    exit_horizon = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))

    go_min = float(os.environ.get("RUN004_E_GO_MIN_SUCCESS_RATE", "0.72") or 0.72)
    top1_max = float(os.environ.get("RUN004_E_MAX_TOP1_SHARE", "0.35") or 0.35)
    top5_max = float(os.environ.get("RUN004_E_MAX_TOP5_SHARE", "0.65") or 0.65)
    pr_min = float(os.environ.get("RUN004_E_MIN_POS_RATE", "0.10") or 0.10)
    pr_max = float(os.environ.get("RUN004_E_MAX_POS_RATE", "0.90") or 0.90)
    d_max_per = int(os.environ.get("RUN004_D_MAX_PER_POOL", "5") or 5)
    d_top = int(os.environ.get("RUN004_D_TOP_POOLS", "60") or 60)
    d_max_total = int(os.environ.get("RUN004_D_MAX_SAMPLE_ROWS", "400") or 400)
    d_seed = int(os.environ.get("RUN004_D_STRATIFY_SEED", "42") or 42)

    if not csv_in.is_file():
        log.error("missing %s", csv_in)
        return 1

    le.TOKEN0_CACHE.clear()
    tv2 = vr._norm_topic(TOPIC_V2).lower()

    input_rows: list[dict[str, str]] = []
    with csv_in.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("candidate_status") or "") != "trainable_candidate":
                continue
            if (row.get("pool_type") or "").lower() != "v2":
                continue
            t0c = vr._norm_topic(row.get("topic0", "")).lower()
            if t0c != tv2:
                continue
            input_rows.append(row)

    if not input_rows:
        log.error("no input rows after v2+trainable+topic filter")
        return 1
    tss = [int(r.get("timestamp", 0) or 0) for r in input_rows if int(r.get("timestamp", 0) or 0) > 0]
    t_min, t_max = (min(tss), max(tss)) if tss else (0, 0)

    if strat:
        work = vr.stratified_sample(input_rows, d_max_per, d_top, d_max_total, d_seed)
        log.info("stratify(D-style): %d rows from %d input", len(work), len(input_rows))
    else:
        work = list(input_rows)
        if max_rows > 0:
            work = work[:max_rows]
        log.info("workset: %d rows (input after filter=%d, max_rows cap=%d)", len(work), len(input_rows), max_rows)
    if max_rows > 0 and len(work) > max_rows:
        work = work[:max_rows]

    if not work:
        log.error("empty workset")
        return 1

    if report_only:
        if not out_csv.is_file() or out_csv.stat().st_size <= 0:
            log.error("report_only requires existing %s with content", out_csv)
            return 1
        seed = _seed_from_existing_csv(out_csv)
        processed_keys = seed.get("keys", set()) or set()
        log.info(
            "report_only: reusing %s existing_rows=%d seeded(label_attempted=%d, ok=%d, fails=%d)",
            _rel_repo(out_csv),
            len(processed_keys),
            int(seed.get("label_attempted", 0)),
            int(seed.get("fail_reason", Counter()).get("ok", 0)),
            int(sum(seed.get("fail_by_pool", Counter()).values())),
        )
        skipped_resume = 0
        append_mode = True
        csv_f = None
        csv_w = None
    else:
        pools = {r.get("pool_address", "").lower() for r in work if r.get("pool_address")}
        chm = vr.load_pool_meta_full(ch_url, ch_db, ch_u, ch_pw, chain_id, pools)
        rm = vr.load_pool_meta_redis(redis_url, pools)
        meta = vr.merge_meta(chm, rm)
        weth = le.WETH_BASE

        ts_lo = min(int(r.get("timestamp", 0) or 0) for r in work) // 1000
        ts_hi = max(int(r.get("timestamp", 0) or 0) for r in work) // 1000 + exit_horizon
        rpc = le.RPC(rpc_url)
        r0 = rpc.call("eth_chainId", [])
        cid = int(r0["result"], 16)
        if cid != chain_id:
            log.error("RPC chain %d != expected %d", cid, chain_id)
            return 1
        log.info("building ts grid [%s..%s]", ts_lo, ts_hi)
        bn, ts_a = le.build_ts_grid(rpc, ts_lo, ts_hi)

        out_csv.parent.mkdir(parents=True, exist_ok=True)
        seed = {}
        processed_keys = set()
        append_mode = False
        if resume and out_csv.is_file() and out_csv.stat().st_size > 0:
            try:
                seed = _seed_from_existing_csv(out_csv)
                processed_keys = seed.get("keys", set()) or set()
                append_mode = bool(processed_keys)
                log.info(
                    "resume: out_csv=%s existing_rows=%d seeded(label_attempted=%d, ok=%d, fails=%d)",
                    _rel_repo(out_csv),
                    len(processed_keys),
                    int(seed.get("label_attempted", 0)),
                    int(seed.get("fail_reason", Counter()).get("ok", 0)),
                    int(sum(seed.get("fail_by_pool", Counter()).values())),
                )
            except Exception as exc:
                log.warning("resume disabled: failed to seed from %s: %r", out_csv, exc)
                seed = {}
                processed_keys = set()
                append_mode = False

        csv_f = out_csv.open("a" if append_mode else "w", encoding="utf-8", newline="")
        csv_w = None
        skipped_resume = 0

    def emit_row(row: dict[str, Any]) -> None:
        nonlocal csv_w
        if csv_w is None:
            csv_w = csv.DictWriter(csv_f, fieldnames=list(row.keys()), extrasaction="ignore")
            if not append_mode:
                csv_w.writeheader()
        csv_w.writerow(row)
        csv_f.flush()

    fail_reason: Counter[str] = seed.get("fail_reason") or Counter()
    label_attempted = int(seed.get("label_attempted", 0))
    pool_v2_calls: Counter[str] = seed.get("pool_calls") or Counter()
    pos_by_pool: Counter[str] = seed.get("pos_by_pool") or Counter()
    neg_by_pool: Counter[str] = seed.get("neg_by_pool") or Counter()
    fail_by_pool: Counter[str] = seed.get("fail_by_pool") or Counter()
    nets_ok: list[float] = list(seed.get("nets_ok") or [])
    tmin_x = int(seed.get("tmin_x", 10**30))
    tmax_x = int(seed.get("tmax_x", 0))
    row_i = 0
    n_work = len(work)
    for r in work if not report_only else []:
        row_i += 1
        if row_i == 1 or row_i % 2000 == 0 or row_i == n_work:
            log.info(
                "dry-run progress: %d / %d (skipped_resume=%d)", row_i, n_work, skipped_resume,
            )
        if processed_keys:
            k = (r.get("tx_hash", ""), r.get("log_index", ""))
            if (k[0] or k[1]) and k in processed_keys:
                skipped_resume += 1
                continue
        pool = (r.get("pool_address") or "").lower()
        if not pool:
            emit_row({**r, "label": -1, "net_return": "", "label_reason": "missing_pool_address", "entry_block": "", "exit_block": "", "pre_check": "invalid"})
            fail_reason["invalid_row"] += 1
            continue

        tmin_x = min(tmin_x, int(r.get("timestamp", 0) or 0))
        tmax_x = max(tmax_x, int(r.get("timestamp", 0) or 0))

        m = meta.get(pool) or {}
        ptype = (m.get("pool_type") or "v2").lower()
        if _is_aero_type(ptype) or ptype in ("aero_vamm", "aero_cl"):
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "skipped_aero", "entry_block": "", "exit_block": "", "pre_check": "aero_excluded"},
            )
            fail_reason["skipped_aero"] += 1
            continue
        t0, t1 = m.get("t0") or "", m.get("t1") or ""
        if not t0 or not t1:
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "missing_pool_meta", "entry_block": "", "exit_block": "", "pre_check": "meta"},
            )
            fail_reason["missing_pool_meta"] += 1
            fail_by_pool[pool] += 1
            continue
        t0d, t1d = int(m.get("t0_dec", 0) or 0), int(m.get("t1_dec", 0) or 0)
        if t0d > 36 or t1d > 36 or (t0d == 0 and t1d == 0):
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "bad_decimals", "entry_block": "", "exit_block": "", "pre_check": "meta"},
            )
            fail_reason["bad_decimals"] += 1
            fail_by_pool[pool] += 1
            continue
        cr = int(m.get("created_block", 0) or 0)
        if cr <= 0:
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "missing_pool_meta", "entry_block": "", "exit_block": "", "pre_check": "meta"},
            )
            fail_reason["missing_pool_meta"] += 1
            fail_by_pool[pool] += 1
            continue
        ts = int(r.get("timestamp", 0) or 0)
        sig = ts // 1000
        exs = sig + exit_horizon
        eb = le.ts_to_block(bn, ts_a, sig)
        xb = le.ts_to_block(bn, ts_a, exs)
        if eb < int(cr):
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "entry_before_pool_created", "entry_block": eb, "exit_block": xb, "pre_check": "time"},
            )
            fail_reason["entry_before_pool_created"] += 1
            fail_by_pool[pool] += 1
            continue
        if xb <= eb:
            emit_row(
                {**r, "label": -1, "net_return": "", "label_reason": "other", "entry_block": eb, "exit_block": xb, "pre_check": "time"},
            )
            fail_reason["other"] += 1
            fail_by_pool[pool] += 1
            continue

        label_attempted += 1
        pool_v2_calls[pool] += 1
        lb, net, reason = le.label_v2(rpc, pool, weth, notional_wei, eb, xb)
        if reason == "executable" and lb in (0, 1):
            fail_reason["ok"] += 1
            nets_ok.append(float(net))
            if lb == 1:
                pos_by_pool[pool] += 1
            else:
                neg_by_pool[pool] += 1
        else:
            b = reason if reason in _KNOWN_FAIL_REASONS else "other"
            fail_reason[b] += 1
            fail_by_pool[pool] += 1

        emit_row(
            {
                **r,
                "label": lb,
                "net_return": f"{net:.12g}" if isinstance(net, (int, float)) else net,
                "label_reason": reason,
                "entry_block": eb,
                "exit_block": xb,
                "pre_check": "ok",
            },
        )

    if csv_f is not None:
        csv_f.close()

    n_succ = int(fail_reason.get("ok", 0))
    ratesucc = n_succ / label_attempted if label_attempted else 0.0
    posc = int(sum(pos_by_pool.values()))
    negc = int(sum(neg_by_pool.values()))
    pos_rate = (posc / n_succ) if n_succ else None
    nets = sorted(nets_ok)
    if not nets:
        net_stats = {
            "min": None, "p25": None, "median": None, "p75": None, "p95": None,
            "max": None, "mean": None,
        }
    else:
        net_stats = {
            "min": round(_pct(nets, 0.0), 12),
            "p25": round(_pct(nets, 0.25), 12),
            "median": round(_pct(nets, 0.5), 12),
            "p75": round(_pct(nets, 0.75), 12),
            "p95": round(_pct(nets, 0.95), 12),
            "max": round(_pct(nets, 1.0), 12),
            "mean": round(sum(nets) / len(nets), 12),
        }

    pool_labeled = pos_by_pool + neg_by_pool
    labeled_pools = len([p for p, c in pool_labeled.items() if c > 0])
    total_labeled = n_succ
    counts = sorted([c for c in pool_labeled.values() if c > 0], reverse=True)
    top1_share = (counts[0] / total_labeled) if counts and total_labeled else 0.0
    top5_share = (sum(counts[:5]) / total_labeled) if total_labeled else 0.0

    top_pos = pos_by_pool.most_common(15)
    top_neg = neg_by_pool.most_common(15)
    top_fail = fail_by_pool.most_common(20)

    if report_only:
        concl = "HOLD_DUE_TO_RPC_DAILY_LIMIT_RECOVERABLE"
    else:
        concl = (
            patch_e2_conclusion(
                n_succ, label_attempted, ratesucc, pos_rate, top1_share, top5_share, go_min, top1_max, top5_max, pr_min, pr_max,
            )
            if e2
            else patch_e_conclusion(
                n_succ, label_attempted, ratesucc, pos_rate, top1_share, top5_share, go_min, top1_max, top5_max, pr_min, pr_max,
            )
        )

    report: dict[str, Any] = {
        "input_v2_trainable_rows": len(input_rows),
        "dryrun_config": {
            "run004_e2_mode": e2,
            "run004_e_stratify": strat,
            "run004_e_max_rows": max_rows or None,
            "run004_e_resume": resume,
            "run004_e_report_only": report_only,
            "report_only_existing_rows": (len(processed_keys) if report_only else 0),
            "resume_seeded_rows": len(processed_keys) if resume else 0,
            "resume_skipped_in_loop": skipped_resume if resume else 0,
            "existing_csv_raw_rows": int(seed.get("raw_rows", 0) or 0),
            "existing_csv_duplicate_rows_collapsed": int(seed.get("dup_rows", 0) or 0),
            "workset_target_rows": n_work,
            "labeled_coverage_vs_target": (
                round(len(processed_keys) / n_work, 6)
                if (report_only and n_work)
                else None
            ),
            "candidates_csv": _rel_repo(csv_in),
        },
        "label_attempted_rows": label_attempted,
        "label_success_rows": n_succ,
        "label_success_rate": round(ratesucc, 6),
        "failure_reason_distribution": dict(fail_reason),
        "positive_label_count": posc,
        "negative_label_count": negc,
        "positive_rate": (round(posc / n_succ, 6) if n_succ else None),
        "net_return_distribution": net_stats,
        "labeled_pool_count": labeled_pools,
        "top1_pool_share_after_label": round(float(top1_share), 6),
        "top5_pool_share_after_label": round(float(top5_share), 6),
        "top_failed_pools": [[p, n] for p, n in top_fail],
        "top_positive_pools": [[p, n] for p, n in top_pos],
        "top_negative_pools": [[p, n] for p, n in top_neg],
        "time_span": {"min_timestamp_ms": t_min, "max_timestamp_ms": t_max, "workset_min_ms": tmin_x, "workset_max_ms": tmax_x},
        "conclusion": concl,
        "conclusion_thresholds": {
            "go_min_success_rate": go_min,
            "max_top1_share": top1_max,
            "max_top5_share": top5_max,
            "pos_rate_min": pr_min,
            "pos_rate_max": pr_max,
        },
        "paths": {
            "out_csv": _rel_repo(out_csv),
            "out_json": _rel_repo(out_json),
            "out_md": _rel_repo(out_md),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    with out_md.open("w", encoding="utf-8") as f:
        f.write(
            _render_md(report, n_succ, posc, negc, concl, e2),
        )
    log.info("Wrote csv=%s json=%s conclusion=%s success_rate=%.4f e2=%s", out_csv, out_json, concl, ratesucc, e2)
    return 0


def _render_md(
    rep: dict[str, Any], n_succ: int, posc: int, negc: int, concl: str, e2: bool,
) -> str:
    d = rep.get("net_return_distribution") or {}
    cfg = rep.get("dryrun_config") or {}
    report_only = bool(cfg.get("run004_e_report_only"))
    tr = "Run-004 Patch E2" if e2 else "Run-004 Patch E"
    if report_only:
        work_target = cfg.get("workset_target_rows")
        existing = cfg.get("report_only_existing_rows")
        cov = cfg.get("labeled_coverage_vs_target")
        work = (
            f"**Workset (this run):** preliminary report from existing partial CSV — "
            f"`RUN004_E_REPORT_ONLY=1`. Existing rows={existing}, target={work_target}, "
            f"coverage={cov}. **No RPC calls were made.** Conclusion is forced to "
            f"`HOLD_DUE_TO_RPC_DAILY_LIMIT_RECOVERABLE` until the QuickNode daily quota "
            f"resets and the run can be resumed with `RUN004_E_RESUME=1`."
        )
    elif e2:
        work = (
            "**Workset (this run):** full population — `RUN004_E_STRATIFY=0` (or off), "
            "all v2+trainable+topic rows after filter (Patch E2)."
        )
    else:
        work = (
            "**Workset (this run):** if `RUN004_E_STRATIFY=1`, Patch D–style 400/seed 42; "
            "if unset, full population or `RUN004_E_MAX_ROWS` cap (Patch E/E2)."
        )
    art = (
        "see `paths` in `run004_patch_e2_v2only_label_dryrun_full_report.json`."
        if e2
        else "see `paths` in `run004_patch_e_v2only_label_dryrun_report.json`."
    )
    title_suffix = " (full population)" if e2 else ""
    if report_only:
        title_suffix += " — preliminary, RPC daily limit"
    return f"""# {tr} — v2-only label dry-run{title_suffix}

**Read-only:** no `services/*`, no strategy/ingestor/executor, no `prepare_dataset`, no `retrain`, no `logs/labeled_dataset.csv`, no promote/LIVE. **v2 only** (no v3, no aero in label path; aero in metadata is skipped). Topic0 must match the Uniswap v2 swap topic.

{work}

**Artifacts (paths as written in report):** {art}

**Metrics**

| Metric | Value |
|--------|------:|
| `input_v2_trainable_rows` | {rep.get("input_v2_trainable_rows")} |
| `label_attempted_rows` | {rep.get("label_attempted_rows")} |
| `label_success_rows` | {n_succ} |
| `label_success_rate` | {rep.get("label_success_rate")} |
| `positive_label_count` | {posc} |
| `negative_label_count` | {negc} |
| `positive_rate` (among successes) | {rep.get("positive_rate")} |
| `labeled_pool_count` | {rep.get("labeled_pool_count")} |
| `top1_pool_share_after_label` | {rep.get("top1_pool_share_after_label")} |
| `top5_pool_share_after_label` | {rep.get("top5_pool_share_after_label")} |

**`failure_reason_distribution`**

```json
{json.dumps(rep.get("failure_reason_distribution", {}), indent=2, sort_keys=True)}
```

**`net_return_distribution` (executable rows only)**

| stat | value |
|------|------:|
| min | {d.get("min")} |
| p25 | {d.get("p25")} |
| median | {d.get("median")} |
| p75 | {d.get("p75")} |
| p95 | {d.get("p95")} |
| max | {d.get("max")} |
| mean | {d.get("mean")} |

**`time_span` (ms)**

| | |
|-|-:|
| min (input filter) | {rep.get("time_span", {}).get("min_timestamp_ms")} |
| max (input filter) | {rep.get("time_span", {}).get("max_timestamp_ms")} |
| workset min | {rep.get("time_span", {}).get("workset_min_ms")} |
| workset max | {rep.get("time_span", {}).get("workset_max_ms")} |

**`top_failed_pools`** — see JSON for full 20; failures here are pre-check or non-`executable` / `fail_sanity_clip`.

**`top_positive_pools` / `top_negative_pools`** — see JSON.

**`conclusion`:** `{concl}`

**`dryrun_config`:**

```json
{json.dumps(rep.get("dryrun_config", {}), indent=2, sort_keys=True)}
```

---
*End.*
"""


if __name__ == "__main__":
    raise SystemExit(main())
