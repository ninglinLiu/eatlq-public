"""
Run-004: swap-only candidate builder — row `candidate_status` separate from
batch `gate_status` and JSON `decision` (C1+); Patch C2 adds full-scan diversity tag.

Omit or set RUN004_MAX_INPUT_ROWS=0 to use full dashcam (C2). Patch C2 admission tag:
  HOLD_DUE_TO_INSUFFICIENT_TRAINABLE_POOL_DIVERSITY if distinct_trainable_pools_after_cap
  is below RUN004_MIN_TRAINABLE_POOLS (default 12);
  CANDIDATE_GATE_GO_FOR_PATCH_D if decision is GO; else
  HOLD_DUE_TO_SAMPLE_CONCENTRATION.

Env:
  DASHCAM_PATH, REDIS_URL, CLICKHOUSE_*, CHAIN_ID, REPO.
  JOIN_WINDOW_SEC, MAX_ROWS_PER_POOL_24H, RUN004_TOP1_MAX, RUN004_TOP5_MAX,
  AERO_CL_MAX, AERO_VAMM_MAX, RUN004_MAX_INPUT_ROWS, RUN004_TOP_POOLS_N,
  RUN004_MIN_TRAINABLE_POOLS
"""

from __future__ import annotations

import bisect
import csv
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("build_run004_dataset")

# --- topic0 (lowercase) ---
# Allowed swap
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_AERO_VAMM = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"

SWAP_TOPICS: set[str] = {TOPIC_V2_SWAP, TOPIC_V3_SWAP, TOPIC_AERO_VAMM}

# Explicit non-swap (reject if this is the chosen join; no guessing)
TOPIC_ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_V2_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e162b0e833fc95b1d8"
TOPIC_V3_MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
TOPIC_V3_BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

EXCLUDED_EVENT_TOPICS: set[str] = {
    TOPIC_ERC20_TRANSFER,
    TOPIC_V2_SYNC,
    TOPIC_V3_MINT,
    TOPIC_V3_BURN,
}

WETH_BASE = "0x4200000000000000000000000000000000000006".lower()
REPO = Path(__file__).resolve().parents[1]


def ch_query(url: str, db: str, user: str, password: str, sql: str) -> list[dict]:
    params = urlencode({
        "database": db, "user": user, "password": password,
        "default_format": "JSONEachRow",
    })
    req = Request(f"{url}/?{params}", data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8").strip()
    except Exception as e:
        log.warning("ClickHouse query failed: %s", e)
        return []
    if not body:
        return []
    return [json.loads(ln) for ln in body.split("\n") if ln.strip()]


def load_pool_meta_ch(
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
            argMax(fee_bps, discovered_at) AS fee_bps
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
        }
    return out


def load_pool_meta_redis(redis_url: str, pools: set[str]) -> dict[str, dict[str, Any]]:
    if not pools:
        return {}
    if not (redis_url or "").strip():
        return {}
    try:
        import redis  # type: ignore
    except ImportError:
        return {}
    r = redis.from_url(redis_url, decode_responses=True)
    out: dict[str, dict[str, Any]] = {}
    for p in pools:
        pl = p.lower()
        h = r.hgetall(f"pool:discovered:{pl}")
        if not h:
            continue
        try:
            fee = int(h.get("fee_bps") or 0)
        except (TypeError, ValueError):
            fee = 0
        out[pl] = {
            "pool_type": (h.get("pool_type") or "").lower(),
            "t0": (h.get("token0_address") or "").lower(),
            "t1": (h.get("token1_address") or "").lower(),
            "fee_bps": fee,
        }
    return out


def merge_pool_meta(
    ch_m: dict[str, dict[str, Any]], redis_m: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in set(ch_m) | set(redis_m):
        if p in ch_m:
            out[p] = ch_m[p]
        else:
            out[p] = redis_m[p]
    return out


def _non_weth(t0: str, t1: str) -> Optional[str]:
    a, b, w = t0.lower(), t1.lower(), WETH_BASE
    if not a or not b:
        return None
    if a == w and b != w:
        return b
    if b == w and a != w:
        return a
    return None


def _norm_topic(t: str) -> str:
    t = t.strip().lower()
    t = t[2:] if t.startswith("0x") else t
    t = t.zfill(64)[:64]
    return "0x" + t


def _event_kind_for_topic(t0: str) -> str:
    t0 = _norm_topic(t0)
    if t0 == TOPIC_V2_SWAP:
        return "Swap (UniswapV2)"
    if t0 == TOPIC_V3_SWAP:
        return "Swap (UniswapV3_or_CL)"
    if t0 == TOPIC_AERO_VAMM:
        return "Swap (Aerodrome_vAMM)"
    if t0 in EXCLUDED_EVENT_TOPICS:
        return f"excluded_not_swap:{t0[:10]}.."
    return "unknown_event"


def load_raw_evm_in_window(
    ch_url: str, ch_db: str, ch_user: str, ch_password: str,
    chain_id: int,
    pools: set[str],
    t_min_ms: int, t_max_ms: int,
) -> list[dict[str, Any]]:
    if not pools:
        return []
    pool_list = ",".join(f"'{p}'" for p in sorted(pools))
    tmin = t_min_ms / 1000.0
    tmax = t_max_ms / 1000.0
    sql = f"""
        SELECT
            lower(address) AS address,
            lower(topic0) AS topic0,
            tx_hash,
            log_index,
            toUnixTimestamp64Milli(received_at_ms) AS rms
        FROM eatlq.raw_evm_logs
        WHERE chain_id = {int(chain_id)}
          AND lower(address) IN ({pool_list})
          AND toUnixTimestamp64Milli(received_at_ms) >= {t_min_ms}
          AND toUnixTimestamp64Milli(received_at_ms) <= {t_max_ms}
    """
    return ch_query(ch_url, ch_db, ch_user, ch_password, sql)


def pick_join_for_row(
    pool: str,
    ts_ms: int,
    window_ms: int,
    by_pool: list[dict[str, Any]],
    rms_sorted: list[int] | None = None,
) -> tuple[Optional[dict], str, str]:
    """Returns (log_row or None, status, reason_code). status in ok_swap|no_log|non_swap|unknown_topic.

    by_pool: logs for this address only, sorted by received_at_ms. When rms_sorted is provided
    (same length, non-decreasing), the join window is found via bisect — required for full dashcam.
    """
    pl = pool.lower()
    lo, hi = ts_ms - window_ms, ts_ms + window_ms
    if rms_sorted is not None and len(rms_sorted) == len(by_pool) and rms_sorted:
        i = bisect.bisect_left(rms_sorted, lo)
        j = bisect.bisect_right(rms_sorted, hi)
        cand = by_pool[i:j]
    else:
        cand = [r for r in by_pool if r["address"] == pl and lo <= int(r["rms"]) <= hi]
    if not cand:
        return None, "no_log", "cannot_join_topic0"
    by_dist = sorted(cand, key=lambda r: abs(int(r["rms"]) - ts_ms))
    swap_picks = [r for r in by_dist if _norm_topic(r.get("topic0", "")) in SWAP_TOPICS]
    if swap_picks:
        return swap_picks[0], "ok_swap", ""
    r0 = by_dist[0]
    t0 = _norm_topic(r0.get("topic0", ""))
    if t0 in EXCLUDED_EVENT_TOPICS:
        return r0, "non_swap", "non_swap_excluded_event"
    return r0, "unknown_topic", "unknown_topic0"


def pool_day_bucket(ts_ms: int) -> int:
    return ts_ms // 86_400_000


def top1_top5_shares(pools: list[str]) -> tuple[float, float]:
    if not pools:
        return 0.0, 0.0
    c = Counter(pools)
    n = len(pools)
    top = c.most_common(5)
    top1 = top[0][1] / n
    t5 = sum(x[1] for x in top) / n
    return top1, t5


def theoretical_min_top5_uniform(K: int) -> float:
    """Best-case (uniform across K pools) top-5 pool share."""
    if K <= 0:
        return 1.0
    return min(5, K) / K


def uniform_distribution_feasibility(
    K: int, top1_max: float, top5_max: float,
) -> tuple[float, float, bool, bool, str]:
    """
    theoretical_min_top1 = 1/K; theoretical_min_top5 = min(5,K)/K under uniform split.
    """
    if K <= 0:
        return 0.0, 1.0, False, False, "no_distinct_trainable_pools_after_cap"
    t1 = 1.0 / K
    t5 = theoretical_min_top5_uniform(K)
    ok1 = t1 <= top1_max + 1e-12
    ok5 = t5 <= top5_max + 1e-12
    reasons: list[str] = []
    if not ok1:
        k_need = int(math.ceil(1.0 / top1_max))
        reasons.append(
            f"uniform_top1_min={t1:.6f}>top1_max={top1_max} (need_at_least_{k_need}_pools_equal_split)",
        )
    if not ok5:
        k5_min = int(math.ceil(min(5, K) / top5_max)) if top5_max > 0 else 999999
        reasons.append(
            f"uniform_top5_min={t5:.6f}>top5_max={top5_max} (rough_need_at_least_{k5_min}_pools_for_bound)",
        )
    return t1, t5, ok1, ok5, "; ".join(reasons) if reasons else ""


def top_pools_table(rows: list[dict[str, Any]], n: int = 15) -> list[list[Any]]:
    c = Counter(r.get("pool_address", "") for r in rows if r.get("pool_address"))
    return [[p, int(c[p])] for p, _ in c.most_common(n)]


def assign_gate_status(
    built: list[dict[str, Any]],
    kept_24: set[str],
    mkeep: set[str],
    kept_conc: set[str],
    concentration_batch_fail: bool,
) -> None:
    """
    concentration_batch_fail: true if no admitted training subset (math infeasible
    or greedy could not / did not pass top1/top5 for the full batch).
    """
    for r in built:
        cs = r.get("candidate_status", "")
        sid = r["source_row_id"]
        if cs == "rejected":
            r["gate_status"] = "not_applicable"
            continue
        if cs == "monitor_only":
            if sid not in kept_24:
                r["gate_status"] = "per_pool_capped"
            elif sid not in mkeep:
                r["gate_status"] = "aero_ratio_hold"
            else:
                r["gate_status"] = "pass"
            continue
        if cs == "trainable_candidate":
            if sid not in kept_24:
                r["gate_status"] = "per_pool_capped"
            elif concentration_batch_fail:
                r["gate_status"] = "concentration_hold"
            elif sid in kept_conc:
                r["gate_status"] = "pass"
            else:
                r["gate_status"] = "concentration_hold"
            continue
        r["gate_status"] = "not_applicable"


def apply_per_pool_24h_cap(
    rows: list[dict[str, Any]],
    max_n: int,
) -> tuple[list[dict[str, Any]], int]:
    """
    groups by (pool, day); keeps up to max_n highest final_score per group.
    Returns (kept_rows, dropped_count).
    """
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r["pool_address"], pool_day_bucket(int(r["timestamp"])))
        groups[key].append(r)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for _k, grp in groups.items():
        grp.sort(key=lambda r: -float(r.get("final_score", 0) or 0))
        take = grp[:max_n]
        rest = grp[max_n:]
        kept.extend(take)
        dropped += len(rest)
    return kept, dropped


def downsample_aero(
    monitor_rows: list[dict[str, Any]], total_cap_rows: int,
    cl_max: float, vamm_max: float,
) -> tuple[list[dict[str, Any]], int]:
    """Cap aero_cl / aero_vamm count vs total_cap_rows. Keeps highest final_score. Returns kept, dropped_n."""
    if not monitor_rows:
        return monitor_rows, 0
    n = max(0, int(total_cap_rows))
    ocl = [r for r in monitor_rows if r.get("pool_type") == "aero_cl"]
    ov = [r for r in monitor_rows if r.get("pool_type") == "aero_vamm"]
    other = [r for r in monitor_rows if r.get("pool_type") not in ("aero_cl", "aero_vamm")]
    m_cl = min(len(ocl), max(0, int(math.floor(n * cl_max + 1e-12)))) if n else 0
    m_vm = min(len(ov), max(0, int(math.floor(n * vamm_max + 1e-12)))) if n else 0
    ocl2 = sorted(ocl, key=lambda r: -float(r.get("final_score", 0) or 0))[:m_cl] if ocl else []
    ov2 = sorted(ov, key=lambda r: -float(r.get("final_score", 0) or 0))[:m_vm] if ov else []
    dropped = (len(ocl) - len(ocl2)) + (len(ov) - len(ov2))
    return ocl2 + ov2 + other, dropped


def adjust_concentration(
    train_rows: list[dict[str, Any]],
    top1_max: float, top5_max: float,
) -> tuple[list[dict[str, Any]], str]:
    """Greedy removal from largest pool (lowest score first) until top1/top5 met, or HOLD."""
    if not train_rows:
        return [], "GO"
    rows = [dict(r) for r in train_rows]
    max_iter = max(len(rows) * 3 + 5, 50)
    for _ in range(max_iter):
        if not rows:
            return [], "GO"
        pools = [r["pool_address"] for r in rows]
        uniq = len(set(pools))
        t1, t5 = top1_top5_shares(pools)
        if uniq == 1 and t1 > top1_max:
            return [], "HOLD"
        if t1 <= top1_max and t5 <= top5_max:
            return rows, "GO"
        c = Counter(pools)
        biggest, _ = c.most_common(1)[0]
        idxs = [i for i, p in enumerate(pools) if p == biggest]
        worst = min(idxs, key=lambda i: float(rows[i].get("final_score", 0) or 0))
        del rows[worst]
    return [], "HOLD"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    )
    dashcam = Path(os.environ.get("DASHCAM_PATH", str(REPO / "logs" / "dashcam.csv")))
    out_csv = Path(os.environ.get("RUN004_OUT_CSV", str(REPO / "logs" / "run004_candidates_swaponly.csv")))
    out_json = Path(os.environ.get("RUN004_OUT_JSON", str(REPO / "mvp" / "diagnostics" / "run004_dataset_report.json")))

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()

    win_sec = int(os.environ.get("JOIN_WINDOW_SEC", "120"))
    window_ms = win_sec * 1000
    max_24h = int(os.environ.get("MAX_ROWS_PER_POOL_24H", "200"))
    top1_max = float(os.environ.get("RUN004_TOP1_MAX", "0.12"))
    top5_max = float(os.environ.get("RUN004_TOP5_MAX", "0.45"))
    aero_cl_max = float(os.environ.get("RUN004_AERO_CL_MAX", "0.35"))
    aero_vamm_max = float(os.environ.get("RUN004_AERO_VAMM_MAX", "0.25"))
    top_pools_n = max(1, int(os.environ.get("RUN004_TOP_POOLS_N", "20") or 20))
    min_diverse_pools = int(os.environ.get("RUN004_MIN_TRAINABLE_POOLS", "12") or 12)

    if not dashcam.is_file():
        log.error("missing %s", dashcam)
        return 1

    raw: list[dict[str, str]] = []
    with dashcam.open("r", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            row["_i"] = str(i)
            raw.append(row)

    max_in = int(os.environ.get("RUN004_MAX_INPUT_ROWS", "0") or 0)
    if max_in > 0 and len(raw) > max_in:
        log.info("RUN004_MAX_INPUT_ROWS=%d: using last %d rows", max_in, max_in)
        raw = raw[-max_in:]

    total_dashcam = len(raw)
    pools_set = {r.get("token_address", "").strip().lower() for r in raw if (r.get("token_address") or "").strip()}

    chm = load_pool_meta_ch(ch_url, ch_db, ch_user, ch_password, chain_id, pools_set)
    rm = load_pool_meta_redis(redis_url, pools_set)
    meta = merge_pool_meta(chm, rm)
    log.info("Pool meta: CH=%d Redis=%d merged=%d", len(chm), len(rm), len(meta))

    tss = [int(r.get("timestamp", 0) or 0) for r in raw]
    t_min = min(tss) - window_ms if tss else 0
    t_max = max(tss) + window_ms if tss else 0
    ch_logs = load_raw_evm_in_window(
        ch_url, ch_db, ch_user, ch_password, chain_id, pools_set, t_min, t_max,
    )
    by_paddr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lg in ch_logs:
        by_paddr[lg.get("address", "").lower()].append(lg)
    rms_index: dict[str, list[int]] = {}
    for paddr, logs in list(by_paddr.items()):
        sl = sorted(logs, key=lambda r: int(r["rms"]))
        by_paddr[paddr] = sl
        rms_index[paddr] = [int(x["rms"]) for x in sl]
    log.info("EVM logs by pool: total=%d pools_in_index=%d", len(ch_logs), len(by_paddr))

    built: list[dict[str, Any]] = []
    joined_topic_rows = 0
    non_swap = 0
    unknown_top = 0
    swap_n = 0

    for r in raw:
        i = r["_i"]
        pool = (r.get("token_address", "") or "").strip().lower()
        ts = int(r.get("timestamp", 0) or 0)
        m = meta.get(pool)
        if not m:
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": "",
                "pool_type": "", "topic0": "", "event_kind": "", "tx_hash": "",
                "log_index": "", "final_score": r.get("final_score", "0"), "source_row_id": i,
                "candidate_status": "rejected", "reject_reason": "unresolved_meta",
            })
            continue

        ptype = (m.get("pool_type") or "").lower()
        tok = _non_weth(m.get("t0", ""), m.get("t1", "")) or ""

        if not ptype or ptype == "unknown":
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": tok,
                "pool_type": ptype, "topic0": "", "event_kind": "", "tx_hash": "",
                "log_index": "", "final_score": r.get("final_score", "0"), "source_row_id": i,
                "candidate_status": "rejected", "reject_reason": "unknown_pool_type",
            })
            continue

        jlog, st, reas = pick_join_for_row(
            pool, ts, window_ms, by_paddr[pool], rms_index.get(pool),
        )
        if st != "no_log":
            joined_topic_rows += 1

        if st == "no_log":
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": tok, "pool_type": ptype,
                "topic0": "", "event_kind": "", "tx_hash": "", "log_index": "",
                "final_score": r.get("final_score", "0"), "source_row_id": i,
                "candidate_status": "rejected", "reject_reason": reas,
            })
            continue

        assert jlog is not None
        t0 = _norm_topic(jlog.get("topic0", ""))
        ek = _event_kind_for_topic(t0)
        th = str(jlog.get("tx_hash", ""))
        li = str(jlog.get("log_index", ""))
        rms = int(jlog["rms"])

        if st == "ok_swap":
            swap_n += 1
            if ptype in ("aero_vamm", "aero_cl"):
                status = "monitor_only"
                rr = ""
                tok_out = tok
            elif ptype in ("v2", "v3"):
                tok_out = _non_weth(m.get("t0", ""), m.get("t1", ""))
                if not tok_out:
                    status = "rejected"
                    rr = "missing_token_or_pool_address"
                elif ptype == "v3" and int(m.get("fee_bps", 0) or 0) <= 0:
                    status = "rejected"
                    rr = "missing_fee_tier"
                else:
                    status = "trainable_candidate"
                    rr = ""
            else:
                status = "rejected"
                rr = f"pool_type_not_trainable_{ptype}"
                tok_out = tok
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": tok_out, "pool_type": ptype,
                "topic0": t0, "event_kind": ek, "tx_hash": th, "log_index": li,
                "final_score": r.get("final_score", "0"), "source_row_id": i, "chain_rms": rms,
                "candidate_status": status, "reject_reason": rr,
            })
            continue

        if st == "non_swap":
            non_swap += 1
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": tok, "pool_type": ptype,
                "topic0": t0, "event_kind": ek, "tx_hash": th, "log_index": li,
                "final_score": r.get("final_score", "0"), "source_row_id": i, "chain_rms": rms,
                "candidate_status": "rejected", "reject_reason": reas,
            })
            continue

        if st == "unknown_topic":
            unknown_top += 1
            built.append({
                "timestamp": ts, "pool_address": pool, "token_address": tok, "pool_type": ptype,
                "topic0": t0, "event_kind": ek, "tx_hash": th, "log_index": li,
                "final_score": r.get("final_score", "0"), "source_row_id": i, "chain_rms": rms,
                "candidate_status": "rejected", "reject_reason": reas,
            })
            continue

    pre_cap = [
        r for r in built
        if r.get("candidate_status") in ("trainable_candidate", "monitor_only")
    ]
    trainable_pre_24h = [r for r in built if r.get("candidate_status") == "trainable_candidate"]
    t1_before_24, t5_before_24 = top1_top5_shares(
        [r["pool_address"] for r in trainable_pre_24h],
    )
    t2a_before: dict[str, Any] = {}
    for ptn in ("v2", "v3"):
        tr = [r for r in trainable_pre_24h if r.get("pool_type") == ptn]
        t1, t5 = top1_top5_shares([r["pool_address"] for r in tr])
        t2a_before[ptn] = {
            "rows": len(tr), "pools": len({r["pool_address"] for r in tr}),
            "top1_pool_share": round(t1, 6), "top5_pool_share": round(t5, 6),
        }

    tcb = len(trainable_pre_24h)
    dtp_before = len({r["pool_address"] for r in trainable_pre_24h})

    pre_cap_kept, drop_cap = apply_per_pool_24h_cap(pre_cap, max_24h)
    kept_24: set[str] = {r["source_row_id"] for r in pre_cap_kept}

    tc_after_cap = [
        r for r in pre_cap_kept
        if r.get("candidate_status") == "trainable_candidate"
    ]
    tca = len(tc_after_cap)
    dtp_after = len({r["pool_address"] for r in tc_after_cap})
    t1_after_24, t5_after_24 = top1_top5_shares(
        [r["pool_address"] for r in tc_after_cap],
    )
    t2a_after_24: dict[str, Any] = {}
    for ptn in ("v2", "v3"):
        tr = [r for r in tc_after_cap if r.get("pool_type") == ptn]
        t1, t5 = top1_top5_shares([r["pool_address"] for r in tr])
        t2a_after_24[ptn] = {
            "rows": len(tr), "pools": len({r["pool_address"] for r in tr}),
            "top1_pool_share": round(t1, 6), "top5_pool_share": round(t5, 6),
        }

    monitor = [r for r in pre_cap_kept if r.get("candidate_status") == "monitor_only"]
    n_tr = tca + len(monitor)
    monitor2, aero_dropped = downsample_aero(
        [dict(x) for x in monitor], n_tr, aero_cl_max, aero_vamm_max,
    )
    mkeep = {r["source_row_id"] for r in monitor2}

    t_min1, t_min5, top1_f_ok, top5_f_ok, feas_reason = uniform_distribution_feasibility(
        dtp_after, top1_max, top5_max,
    )
    math_infeasible = not (top1_f_ok and top5_f_ok)

    if dtp_after == 0 or not tc_after_cap:
        tr_adj, cdec = [], "HOLD"
        if dtp_after == 0:
            math_infeasible = True
            if not feas_reason:
                feas_reason = "no_trainable_rows_after_24h_cap"
    elif math_infeasible:
        tr_adj, cdec = [], "HOLD"
    else:
        tr_adj, cdec = adjust_concentration([dict(r) for r in tc_after_cap], top1_max, top5_max)

    kept_t = {r["source_row_id"] for r in tr_adj} if cdec == "GO" else set()
    t1a, t5a = top1_top5_shares([r["pool_address"] for r in tr_adj])
    t2a_final: dict[str, Any] = {}
    for ptn in ("v2", "v3"):
        trx = [r for r in tr_adj if r.get("pool_type") == ptn]
        t1, t5 = top1_top5_shares([r["pool_address"] for r in trx])
        t2a_final[ptn] = {
            "rows": len(trx), "pools": len({r["pool_address"] for r in trx}),
            "top1_pool_share": round(t1, 6), "top5_pool_share": round(t5, 6),
        }

    th_min_top1 = (1.0 / dtp_after) if dtp_after else 0.0
    th_min_top5u = theoretical_min_top5_uniform(dtp_after) if dtp_after else 1.0

    decision: str = "HOLD" if cdec == "HOLD" or math_infeasible else "GO"
    post_greedy_fail = False
    if decision == "GO" and tr_adj and (t1a > top1_max + 1e-9 or t5a > top5_max + 1e-9):
        decision = "HOLD"
        post_greedy_fail = True

    reason_infeas = feas_reason
    if dtp_after and not math_infeasible and cdec == "HOLD" and not reason_infeas:
        reason_infeas = "concentration_greedy_could_not_meet_top1_top5_on_this_sample"
    if math_infeasible and dtp_after and not reason_infeas:
        reason_infeas = feas_reason or "uniform_split_cannot_satisfy_thresholds"
    if post_greedy_fail and not reason_infeas:
        reason_infeas = "post_greedy_subset_top1_top5_still_violate_thresholds"

    all_swap_train = (len(tr_adj) == 0) or all(
        _norm_topic(r.get("topic0", "")) in SWAP_TOPICS
        for r in tr_adj
    )

    assign_gate_status(built, kept_24, mkeep, kept_t, decision == "HOLD")

    rows_by_type = Counter(
        (r.get("pool_type") or "none") for r in built
    )
    tr_by = Counter(
        (r.get("pool_type") or "none")
        for r in built
        if r.get("candidate_status") == "trainable_candidate"
    )
    mon_by = Counter(
        (r.get("pool_type") or "none")
        for r in built
        if r.get("candidate_status") == "monitor_only"
    )
    gdist = Counter(r.get("gate_status", "unknown") for r in built)
    cand_status = Counter((r.get("candidate_status") or "none") for r in built)
    unres_meta = sum(1 for r in built if r.get("reject_reason") == "unresolved_meta")
    unres_un = sum(1 for r in built if r.get("reject_reason") == "unknown_pool_type")
    rejection_dist = Counter(
        (r.get("reject_reason") or "unspecified")
        for r in built
        if r.get("candidate_status") == "rejected"
    )
    cannot_join = int(rejection_dist.get("cannot_join_topic0", 0))
    unknown_t0 = int(rejection_dist.get("unknown_topic0", 0))
    if dtp_after < min_diverse_pools:
        c2_admission = "HOLD_DUE_TO_INSUFFICIENT_TRAINABLE_POOL_DIVERSITY"
    elif decision == "GO":
        c2_admission = "CANDIDATE_GATE_GO_FOR_PATCH_D"
    else:
        c2_admission = "HOLD_DUE_TO_SAMPLE_CONCENTRATION"

    report: dict[str, Any] = {
        "decision": decision,
        "total_dashcam_rows": total_dashcam,
        "trainable_candidate_rows_before_cap": tcb,
        "trainable_candidate_rows_after_cap": tca,
        "trainable_candidate_rows_after_concentration": len(tr_adj),
        "distinct_trainable_pools_before_cap": dtp_before,
        "distinct_trainable_pools_after_cap": dtp_after,
        "theoretical_min_top1": round(th_min_top1, 8),
        "theoretical_min_top5_uniform": round(th_min_top5u, 8),
        "top1_threshold_feasible": bool(dtp_after and top1_f_ok),
        "top5_threshold_feasible": bool(dtp_after and top5_f_ok),
        "reason_if_infeasible": (reason_infeas or "")
        if decision == "HOLD"
        else "",
        "merged_pool_meta_count": len(meta),
        "joined_topic_rows": joined_topic_rows,
        "swap_rows": swap_n,
        "non_swap_rejected_rows": non_swap,
        "unknown_topic_rejected_rows": unknown_top,
        "unresolved_meta_rows": unres_meta,
        "unknown_pool_type_rows": unres_un,
        "cannot_join_topic0_rows": cannot_join,
        "unknown_topic0_rejected_rows": unknown_t0,
        "rows_by_pool_type": dict(rows_by_type),
        "trainable_candidate_rows_by_pool_type": dict(tr_by),
        "monitor_only_rows_by_pool_type": dict(mon_by),
        "candidate_status_distribution": dict(cand_status),
        "run004_patch_c2_admission_tag": c2_admission,
        "per_pool_cap_applied_count": int(drop_cap),
        "aero_subrows_dropped": aero_dropped,
        "rejection_reason_distribution": dict(rejection_dist),
        "gate_status_distribution": dict(gdist),
        "concentration": {
            "top1_top5_trainable_v2v3_before_24h_pool_cap": {
                "top1": round(t1_before_24, 6), "top5": round(t5_before_24, 6),
            },
            "top1_top5_trainable_v2v3_after_24h_pool_cap": {
                "top1": round(t1_after_24, 6), "top5": round(t5_after_24, 6),
            },
            "top1_top5_admitted_trainable_subset_after_concentration": {
                "top1": round(t1a, 6), "top5": round(t5a, 6),
            },
            "v2_v3_strata_before_24h": t2a_before,
            "v2_v3_strata_after_24h": t2a_after_24,
            "v2_v3_strata_after_concentration": t2a_final,
        },
        "top_pools": {
            "by_trainable_row_count_before_cap": top_pools_table(
                trainable_pre_24h, top_pools_n,
            ),
            "by_trainable_row_count_after_cap": top_pools_table(
                tc_after_cap, top_pools_n,
            ),
            "admitted_trainable_subset_after_concentration": top_pools_table(
                tr_adj, top_pools_n,
            ),
        },
        "all_trainable_admitted_rows_are_swap": bool(all_swap_train),
        "batch_hold_due_to_math_infeasible": bool(
            dtp_after and (not (top1_f_ok and top5_f_ok)),
        ),
        "thresholds": {
            "top1_max": top1_max, "top5_max": top5_max,
            "aero_cl_max": aero_cl_max, "aero_vamm_max": aero_vamm_max,
            "max_24h_per_pool": max_24h, "join_window_sec": win_sec,
        },
    }
    if decision == "HOLD":
        report["concentration"]["note"] = (
            "HOLD: no batch training admission; candidate rows keep semantic status; "
            "see gate_status and reason_if_infeasible. Do not run label or Patch D. "
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp", "pool_address", "token_address", "pool_type", "topic0", "event_kind",
                "tx_hash", "log_index", "final_score", "source_row_id",
                "candidate_status", "reject_reason", "gate_status",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in built:
            row = {k: r.get(k, "") for k in w.fieldnames or []}  # type: ignore
            for k2 in list(row):
                if row[k2] is None:
                    row[k2] = ""
            w.writerow(row)

    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    log.info("Wrote %s and %s decision=%s", out_csv, out_json, decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
