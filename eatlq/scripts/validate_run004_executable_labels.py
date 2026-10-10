"""
Run-004 Patch D: read-only executable label validation (no prepare/retrain, no full label_dashcam).

Reads logs/run004_candidates_swaponly_full.csv, filters trainable_candidate v2/v3, stratified sample,
reuses label_dashcam_executable quote path (v2/v3) with archive RPC + ts→block grid.

Env:
  RUN004_CANDIDATES_CSV (default: logs/run004_candidates_swaponly_full.csv)
  RUN004_D_OUT_JSON (default: mvp/diagnostics/run004_patch_d_executable_report.json, or
    mvp/diagnostics/run004_patch_d4_v2only_executable_report.json when RUN004_D_V2_ONLY=1)
  RUN004_D_V2_ONLY=1  Patch D4: only trainable_candidate pool_type=v2 (stratify unchanged)
  CLICKHOUSE_*, CHAIN_ID, REDIS_URL, LABEL_RPC_URL or EVM_HTTP_URL (archive)
  RUN004_D_MAX_PER_POOL, RUN004_D_TOP_POOLS, RUN004_D_MAX_SAMPLE_ROWS, RUN004_D_STRATIFY_SEED
  LABEL_EXIT_HORIZON_SEC, LABEL_NOTIONAL_WEI
  RUN004_D_GO_MIN_RATE, RUN004_D_RPC_META_FAILURE_RATE (conclusion thresholds)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import label_dashcam_executable as le  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("validate_run004_exec")

# Same swap topic allowlist as build_run004_dataset
TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_TOPICS = {TOPIC_V2.lower(), TOPIC_V3.lower()}


def _norm_topic(t: str) -> str:
    t = (t or "").strip().lower()
    t = t[2:] if t.startswith("0x") else t
    t = t.zfill(64)[:64]
    return "0x" + t


def ch_query(
    ch_url: str, ch_db: str, ch_user: str, ch_password: str, sql: str,
) -> list[dict[str, Any]]:
    params = urlencode({
        "database": ch_db, "user": ch_user, "password": ch_password,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{ch_url}/?{params}", data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8").strip()
    except Exception as e:
        log.warning("ClickHouse query failed: %s", e)
        return []
    if not body:
        return []
    return [json.loads(ln) for ln in body.split("\n") if ln.strip()]


def load_pool_meta_full(
    ch_url: str, ch_db: str, ch_user: str, ch_password: str,
    chain_id: int, pools: set[str],
) -> dict[str, dict[str, Any]]:
    if not pools:
        return {}
    pool_list = ",".join(f"'{p}'" for p in sorted(pools))
    sql = f"""
        SELECT
            lower(pool_address) AS pool,
            argMax(pool_type, discovered_at) AS pool_type,
            argMax(lower(token0_address), discovered_at) AS t0,
            argMax(lower(token1_address), discovered_at) AS t1,
            argMax(fee_bps, discovered_at) AS fee_bps,
            argMax(created_block, discovered_at) AS created_block,
            argMax(token0_decimals, discovered_at) AS t0_dec,
            argMax(token1_decimals, discovered_at) AS t1_dec
        FROM eatlq.discovered_pools
        WHERE chain_id = {int(chain_id)}
          AND lower(pool_address) IN ({pool_list})
        GROUP BY pool
    """
    rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = (r.get("pool") or "").lower()
        if not p:
            continue
        out[p] = {
            "pool_type": (r.get("pool_type") or "").lower(),
            "t0": (r.get("t0") or "").lower(),
            "t1": (r.get("t1") or "").lower(),
            "fee_bps": int(r.get("fee_bps", 0) or 0),
            "created_block": int(r.get("created_block", 0) or 0),
            "t0_dec": int(r.get("t0_dec", 0) or 0),
            "t1_dec": int(r.get("t1_dec", 0) or 0),
        }
    return out


def load_pool_meta_redis(redis_url: str, pools: set[str]) -> dict[str, dict[str, Any]]:
    if not (redis_url or "").strip() or not pools:
        return {}
    try:
        import redis  # type: ignore
    except ImportError:
        return {}
    r = redis.from_url(redis_url, decode_responses=True)
    out: dict[str, dict[str, Any]] = {}
    for p in pools:
        h = r.hgetall(f"pool:discovered:{p.lower()}")
        if not h:
            continue
        try:
            fee = int(h.get("fee_bps") or 0)
        except (TypeError, ValueError):
            fee = 0
        try:
            cb = int(h.get("created_block") or 0)
        except (TypeError, ValueError):
            cb = 0
        try:
            t0d = int(h.get("token0_decimals") or 0)
        except (TypeError, ValueError):
            t0d = 0
        try:
            t1d = int(h.get("token1_decimals") or 0)
        except (TypeError, ValueError):
            t1d = 0
        out[p.lower()] = {
            "pool_type": (h.get("pool_type") or "").lower(),
            "t0": (h.get("token0_address") or "").lower(),
            "t1": (h.get("token1_address") or "").lower(),
            "fee_bps": fee,
            "created_block": cb,
            "t0_dec": t0d,
            "t1_dec": t1d,
        }
    return out


def merge_meta(
    chm: dict[str, dict[str, Any]], rdm: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out = dict(chm)
    for p, d in rdm.items():
        if p not in out:
            out[p] = d
        else:
            b = out[p]
            for k, v in d.items():
                if v and (not b.get(k) or b.get(k) in (0, "", "0")):
                    b[k] = v
    return out


def map_labeler_reason(
    r: str, label: int,
) -> tuple[str, bool]:
    """
    Returns (bucket, success) where success means executable path produced a net label.
    """
    if r == "executable" and label in (0, 1):
        return ("ok", True)
    if r in ("fail_low_liquidity", "fail_zero_tok_reserve", "fail_notional_gt_1pct"):
        return ("no_liquidity", False)
    if r in ("fail_entry_quote_zero", "fail_v3_entry_zero"):
        return ("quote_entry_failed", False)
    if r == "executable_exit_drained":
        return ("quote_exit_failed", False)
    if r == "fail_rpc_exception":
        return ("rpc_error", False)
    if r == "fail_sanity_clip":
        return ("other", False)
    return ("other", False)


REASON_SET = {
    "missing_pool_meta", "entry_before_pool_created", "quote_entry_failed", "quote_exit_failed",
    "no_liquidity", "rpc_error", "unsupported_pool_type", "bad_decimals", "other",
}


def _fail_dist_fractions(fail_dist: Counter, sampled: int) -> dict[str, float]:
    if sampled <= 0:
        return {}
    keys = set(fail_dist.keys()) | REASON_SET | {"ok"}
    return {k: float(fail_dist.get(k, 0)) / float(sampled) for k in keys}


def patch_d_conclusion(
    sampled: int,
    fail_dist: Counter,
    go_min: float,
    rpc_meta_max: float,
) -> str:
    """Classify at most one terminal recommendation (Patch D). Counts are normalized in-process."""
    if sampled <= 0:
        return "HOLD_DUE_TO_RPC_OR_METADATA_FAILURE"
    fr = _fail_dist_fractions(fail_dist, sampled)
    rpc_m = fr.get("rpc_error", 0) + fr.get("missing_pool_meta", 0)
    if rpc_m > rpc_meta_max:
        return "HOLD_DUE_TO_RPC_OR_METADATA_FAILURE"
    ok_r = 1.0 - sum(fr.get(k, 0) for k in REASON_SET if k != "ok")
    if fr.get("ok", 0) >= go_min and ok_r >= go_min - 0.01:
        return "PATCH_D_GO_TO_LABEL_DRYRUN"
    return "HOLD_DUE_TO_EXECUTABLE_LABEL_FAILURE"


def patch_d4_v2only_conclusion(
    sampled: int,
    fail_dist: Counter,
    go_min: float,
    rpc_meta_max: float,
) -> str:
    """
    Run-004 Patch D4: v2-only gate (same go/meta thresholds as Patch D, normalized rates).
    """
    if sampled <= 0:
        return "HOLD_DUE_TO_RPC_OR_METADATA_FAILURE"
    fr = _fail_dist_fractions(fail_dist, sampled)
    rpc_m = fr.get("rpc_error", 0) + fr.get("missing_pool_meta", 0)
    if rpc_m > rpc_meta_max:
        return "HOLD_DUE_TO_RPC_OR_METADATA_FAILURE"
    ok_r = 1.0 - sum(fr.get(k, 0) for k in REASON_SET if k != "ok")
    if fr.get("ok", 0) >= go_min and ok_r >= go_min - 0.01:
        return "PATCH_D4_V2ONLY_GO_TO_LABEL_DRYRUN"
    return "HOLD_DUE_TO_V2_EXECUTABLE_LABEL_FAILURE"


def stratified_sample(
    rows: list[dict[str, Any]],
    max_per_pool: int,
    top_pools: int,
    max_total: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        p = (r.get("pool_address") or "").lower()
        if p:
            by[p].append(r)
    for p in by:
        by[p].sort(key=lambda x: int(x.get("timestamp", 0) or 0))
    pool_order = sorted(by.keys(), key=lambda p: -len(by[p]))
    out: list[dict[str, Any]] = []
    for i, p in enumerate(pool_order):
        grp = by[p]
        if i < top_pools:
            n = min(max_per_pool, len(grp))
        else:
            n = min(1, len(grp))
        if n <= 0:
            continue
        if n >= len(grp):
            take = list(grp)
        else:
            idxs = [int(j * (len(grp) - 1) / max(n - 1, 1)) for j in range(n)]
            if len(set(idxs)) < n:
                idxs = sorted(rng.sample(range(len(grp)), n))
            take = [grp[j] for j in idxs]
        out.extend(take)
        if len(out) >= max_total:
            return out[:max_total]
    return out[:max_total]


def _env_v2_only() -> bool:
    return (os.environ.get("RUN004_D_V2_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    csv_path = Path(os.environ.get("RUN004_CANDIDATES_CSV", str(REPO / "logs" / "run004_candidates_swaponly_full.csv")))
    v2_only = _env_v2_only()
    default_d_out = str(
        REPO
        / "mvp"
        / "diagnostics"
        / (
            "run004_patch_d4_v2only_executable_report.json"
            if v2_only
            else "run004_patch_d_executable_report.json"
        )
    )
    out_json = Path(os.environ.get("RUN004_D_OUT_JSON", default_d_out))

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    if not rpc_url:
        log.error("LABEL_RPC_URL or EVM_HTTP_URL required (archive RPC)")
        return 1

    exit_horizon = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional_wei = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    max_per = int(os.environ.get("RUN004_D_MAX_PER_POOL", "5") or 5)
    top_tier = int(os.environ.get("RUN004_D_TOP_POOLS", "60") or 60)
    max_total = int(os.environ.get("RUN004_D_MAX_SAMPLE_ROWS", "500") or 500)
    seed = int(os.environ.get("RUN004_D_STRATIFY_SEED", "42") or 42)
    go_min = float(os.environ.get("RUN004_D_GO_MIN_RATE", "0.72") or 0.72)
    rpc_meta_max = float(os.environ.get("RUN004_D_RPC_META_FAILURE_RATE", "0.15") or 0.15)

    if not csv_path.is_file():
        log.error("missing %s", csv_path)
        return 1

    le.TOKEN0_CACHE.clear()

    trainable: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("candidate_status") or "") != "trainable_candidate":
                continue
            pt = (row.get("pool_type") or "").lower()
            if v2_only:
                if pt != "v2":
                    continue
            elif pt not in ("v2", "v3"):
                continue
            trainable.append(row)

    pool_ids = {r.get("pool_address", "").lower() for r in trainable if r.get("pool_address")}
    tss = [int(r.get("timestamp", 0) or 0) for r in trainable if int(r.get("timestamp", 0) or 0) > 0]
    t_min, t_max = (min(tss), max(tss)) if tss else (0, 0)
    weth = le.WETH_BASE

    if not trainable:
        log.error("no trainable %s rows", "v2" if v2_only else "v2/v3")
        return 1

    samples = stratified_sample(trainable, max_per, top_tier, max_total, seed)
    log.info("stratified sample: %d rows from %d trainable (pools in input=%d) v2_only=%s",
             len(samples), len(trainable), len(pool_ids), v2_only)

    pools_s = {r.get("pool_address", "").lower() for r in samples}
    chm = load_pool_meta_full(ch_url, ch_db, ch_user, ch_pw, chain_id, pools_s)
    rm = load_pool_meta_redis(redis_url, pools_s)
    meta = merge_meta(chm, rm)
    log.info("pool meta merged: ch=%d redis=%d merged=%d for sampled pools", len(chm), len(rm), len(meta))

    ts_min = min(int(r.get("timestamp", 0) or 0) for r in samples) // 1000
    ts_max = max(int(r.get("timestamp", 0) or 0) for r in samples) // 1000 + exit_horizon
    rpc = le.RPC(rpc_url)
    r0 = rpc.call("eth_chainId", [])
    cid = int(r0["result"], 16)
    if cid != chain_id:
        log.error("RPC chain %d != expected %d", cid, chain_id)
        return 1
    log.info("building ts→block grid [%s … %s] sec", ts_min, ts_max)
    bn_arr, ts_arr = le.build_ts_grid(rpc, ts_min, ts_max)

    per_row_reason: list[tuple[str, str]] = []
    pool_fail: Counter[str] = Counter()
    success_by_type: Counter[str] = Counter()
    labeler_subreason: Counter[str] = Counter()

    for r in samples:
        pool = (r.get("pool_address") or "").lower()
        raw_pt = (r.get("pool_type") or "").lower()
        t0c = _norm_topic(r.get("topic0", ""))

        if raw_pt not in ("v2", "v3"):
            per_row_reason.append((pool, "unsupported_pool_type"))
            pool_fail[pool] += 1
            continue
        if t0c.lower() not in SWAP_TOPICS:
            per_row_reason.append((pool, "other"))
            pool_fail[pool] += 1
            continue

        m = meta.get(pool) or {}
        ptype = (m.get("pool_type") or raw_pt).lower()
        if ptype not in ("v2", "v3"):
            per_row_reason.append((pool, "unsupported_pool_type"))
            pool_fail[pool] += 1
            continue
        t0, t1 = m.get("t0") or "", m.get("t1") or ""
        if not t0 or not t1 or (ptype == "v3" and int(m.get("fee_bps", 0) or 0) <= 0):
            per_row_reason.append((pool, "missing_pool_meta"))
            pool_fail[pool] += 1
            continue
        t0d, t1d = int(m.get("t0_dec", 0) or 0), int(m.get("t1_dec", 0) or 0)
        if t0d > 36 or t1d > 36 or (t0d == 0 and t1d == 0):
            per_row_reason.append((pool, "bad_decimals"))
            pool_fail[pool] += 1
            continue

        cr = int(m.get("created_block", 0) or 0)
        if cr <= 0:
            per_row_reason.append((pool, "missing_pool_meta"))
            pool_fail[pool] += 1
            continue

        ts = int(r.get("timestamp", 0) or 0)
        sig_sec, ex_sec = ts // 1000, ts // 1000 + exit_horizon
        entry_b = le.ts_to_block(bn_arr, ts_arr, sig_sec)
        exit_b = le.ts_to_block(bn_arr, ts_arr, ex_sec)
        if entry_b < int(cr):
            per_row_reason.append((pool, "entry_before_pool_created"))
            pool_fail[pool] += 1
            continue
        if exit_b <= entry_b:
            per_row_reason.append((pool, "other"))
            pool_fail[pool] += 1
            continue

        fee_b = int(m.get("fee_bps", 30) or 30)
        if ptype == "v2":
            lb, net, reason = le.label_v2(
                rpc, pool, weth, notional_wei, entry_b, exit_b,
            )
        else:
            lb, net, reason = le.label_v3(
                rpc, pool, t0, t1, fee_b, weth, notional_wei,
                entry_b, exit_b,
            )
        bucket, success = map_labeler_reason(reason, int(lb))
        if success:
            per_row_reason.append((pool, "ok"))
            success_by_type[ptype] += 1
            continue
        b = bucket
        if b == "other" and "rpc" in str(reason).lower():
            b = "rpc_error"
        if b not in REASON_SET:
            b = "other"
        if b == "other":
            labeler_subreason[reason] = labeler_subreason.get(reason, 0) + 1
        per_row_reason.append((pool, b))
        pool_fail[pool] += 1

    fail_dist = Counter(x[1] for x in per_row_reason)
    sampled = len(per_row_reason)
    ok = int(fail_dist.get("ok", 0))
    ok_rate = ok / sampled if sampled else 0.0

    top_failed = [[p, n] for p, n in pool_fail.most_common(20)]
    concl_d = patch_d_conclusion(sampled, fail_dist, go_min, rpc_meta_max)
    concl_d4 = patch_d4_v2only_conclusion(sampled, fail_dist, go_min, rpc_meta_max)

    report: dict[str, Any] = {
        "time_span_ms": {"min": t_min, "max": t_max},
        "sampling": {
            "stratify_max_per_pool": max_per,
            "stratify_top_pools_tier": top_tier,
            "stratify_max_total_rows": max_total,
            "stratify_seed": seed,
            "v2_only": v2_only,
        },
        "sampled_rows": sampled,
        "executable_success_rows": ok,
        "executable_success_rate": round(ok_rate, 6),
        "distinct_pools_sampled": len(pools_s),
        "success_by_pool_type": dict(success_by_type),
        "failure_reason_distribution": dict(fail_dist),
        "labeler_subreason_when_bucket_other": dict(labeler_subreason) if labeler_subreason else {},
        "top_failed_pools": top_failed,
        "conclusion_thresholds": {
            "go_min_rate": go_min,
            "rpc_or_meta_max_share": rpc_meta_max,
        },
        "paths": {
            "candidates_csv": str(csv_path),
            "report_json": str(out_json),
        },
    }
    if v2_only:
        report["input_trainable_v2_only_total_rows"] = len(trainable)
        report["input_trainable_v2_only_distinct_pools"] = len(pool_ids)
        report["conclusion"] = concl_d4
        report["patch_d4_v2only_conclusion"] = concl_d4
    else:
        report["input_trainable_v2v3_total_rows"] = len(trainable)
        report["input_trainable_v2v3_distinct_pools"] = len(pool_ids)
        report["patch_d_conclusion"] = concl_d
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    log_cl = concl_d4 if v2_only else concl_d
    log.info("Wrote %s conclusion=%s ok_rate=%.3f", out_json, log_cl, ok_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
