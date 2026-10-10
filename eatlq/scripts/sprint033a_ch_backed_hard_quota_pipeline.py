#!/usr/bin/env python3
"""Sprint033A — CH-backed universe mining, pre-label viability gate, hard-quota manifest.

Read-only ClickHouse via `docker exec eatlq-clickhouse-1 clickhouse-client` when host URL
is unreachable. Bounded RPC (metadata + historical entry quotes). Does not print secrets.

Usage:
  python3 scripts/sprint033a_ch_backed_hard_quota_pipeline.py --write
"""

from __future__ import annotations

import argparse
import base64
import csv
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BUCKET_SEC = 300
BUCKET_MS = BUCKET_SEC * 1000
CHAIN_ID = 8453
WINDOW_DAYS = 60
MIN_SWAPS = 15
MIN_BUCKETS_5M = 5
MIN_ACTIVE_DAYS = 2
MAX_CH_SCAN_POOLS = 300
DASHCAM_POOL_SEED_CAP = 900
CH_POOL_CHUNK = 100
MAX_VIABILITY_POOLS = 120
WETH = "0x4200000000000000000000000000000000000006"

OUT_UNIVERSE = REPO / "mvp/diagnostics/sprint033a_ch_backed_universe_summary.json"
OUT_VIAB_CSV = REPO / "mvp/diagnostics/sprint033a_prelabel_viability_audit.csv"
OUT_MANIFEST = REPO / "mvp/diagnostics/sprint033a_hard_quota_viability_smoke_manifest.json"
OUT_CAND_CSV = REPO / "mvp/diagnostics/sprint033a_candidate_pool_summary.csv"
OUT_REPORT = REPO / "mvp/audits/sprint033a_ch_backed_hard_quota_smoke_report.md"

_SEL_TOKEN1 = "0xd21220a7"
_SEL_FEE_V3 = "0xddca3f43"
_SEL_LIQ = "0x1a686502"
_SEL_SLOT0 = "0x3850c7bd"


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        p = REPO / rel
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_label_mod() -> Any:
    path = REPO / "scripts" / "label_dashcam_executable.py"
    spec = importlib.util.spec_from_file_location("label_dashcam_executable", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load label_dashcam_executable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ch_host_ping(url: str, user: str, password: str) -> bool:
    if not url:
        return False
    q = url.rstrip("/") + "/?query=" + urllib.parse.quote("SELECT 1 FORMAT TabSeparated")
    try:
        req = urllib.request.Request(q, method="GET")
        if user:
            tok = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read().strip() == b"1"
    except Exception:
        return False


def ch_docker_query(sql: str, timeout_sec: float = 180.0) -> str:
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "-q", sql],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def build_exclusions() -> set[str]:
    ex: set[str] = set()
    manifest_paths = (
        "mvp/diagnostics/sprint030e_top10_dryrun_manifest.json",
        "mvp/diagnostics/sprint031a_new_universe_smoke_manifest.json",
        "mvp/diagnostics/sprint031b_new_lineage_dryrun_manifest.json",
        "mvp/diagnostics/sprint032a_hard_quota_smoke_manifest.json",
        "mvp/diagnostics/sprint032b_metadata_verified_smoke_manifest.json",
        "mvp/diagnostics/sprint032c_viability_verified_smoke_manifest.json",
    )
    for rel in manifest_paths:
        p = REPO / rel
        if not p.is_file():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        for k in (
            "pool_address_audit_keys",
            "excluded_old_cohort_pools_audit_only",
            "excluded_old_closed_cohort_pools_audit_only",
        ):
            for x in j.get(k) or []:
                ex.add(str(x).lower())
    ex.add("0xdf349842e5f5b0e9dc7f96638b6c0eb372545b23")
    ex.add("0x09591f786a2e724ed46ccc85293ee0e8dd73f9ba")
    ex.add("0x82321f3beb69f503380d6b233857d5c43562e2d0")
    ex.add("0x147c3c7133c76bd0a5f4840629888c216140cfb3")
    via = REPO / "mvp/diagnostics/sprint032c_historical_viability_audit.csv"
    if via.is_file():
        with via.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("viability_status") or "").strip().upper() == "FAIL":
                    pk = (row.get("pool_address_audit_key") or "").strip().lower()
                    if pk:
                        ex.add(pk)
    for p in (
        "0x87b73c76c4cffe0610da8efb8ecb8e04f0a4ca62",
        "0xb73f7f979e1cd2a5377da0e89e1682279cc71c27",
        "0x5486225f16e1c92b8f1d7f7dfd27d93a9de9f1e6",
    ):
        ex.add(p)
    return ex


def ch_pool_candidates_docker(exclude: set[str]) -> tuple[list[dict[str, Any]], str]:
    sql = f"""
SELECT
  lower(dp.pool_address) AS pa,
  toUInt64(sc.swap_cnt) AS swap_cnt,
  toUInt32(sc.buckets_5m) AS buckets_5m,
  toUInt32(sc.active_days) AS active_days
FROM eatlq.discovered_pools AS dp
INNER JOIN (
  SELECT
    lower(pool_address) AS pa,
    count() AS swap_cnt,
    uniqExact(intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS})) AS buckets_5m,
    uniqExact(toDate(coalesce(block_time, received_at_ms))) AS active_days
  FROM eatlq.uniswap_v3_swaps_enriched
  WHERE chain_id = {CHAIN_ID}
    AND removed = 0
    AND coalesce(block_time, received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
  GROUP BY pa
  HAVING swap_cnt >= {MIN_SWAPS}
    AND buckets_5m >= {MIN_BUCKETS_5M}
    AND active_days >= {MIN_ACTIVE_DAYS}
) AS sc ON lower(dp.pool_address) = sc.pa
WHERE dp.chain_id = {CHAIN_ID}
  AND lower(dp.pool_type) = 'v3'
  AND dp.has_weth_leg = 1
ORDER BY sc.swap_cnt DESC
LIMIT {MAX_CH_SCAN_POOLS * 2}
FORMAT TabSeparated
""".strip()
    body = ch_docker_query(sql)
    rows: list[dict[str, Any]] = []
    if not body:
        return rows, sql
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pa = parts[0].lower()
        if pa in exclude:
            continue
        rows.append(
            {
                "pool_address": pa,
                "swap_count_window": int(parts[1]),
                "bucket_count_5m_ch": int(parts[2]),
                "active_days_ch": int(parts[3]),
            }
        )
        if len(rows) >= MAX_CH_SCAN_POOLS:
            break
    return rows, sql


def dashcam_activity_pools_ordered(dashcam: Path, cap: int, exclude: set[str]) -> list[str]:
    ctr: Counter[str] = Counter()
    with dashcam.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = (row.get("token_address") or "").strip().lower()
            if not p.startswith("0x") or p in exclude:
                continue
            ctr[p] += 1
    return [p for p, _ in ctr.most_common(cap)]


def ch_pool_candidates_seed_dashcam(
    dashcam_path: Path, exclude: set[str]
) -> tuple[list[dict[str, Any]], str]:
    """Swap-density from dashcam-active pools; discovered_pools typed in a second light query."""
    seeds = dashcam_activity_pools_ordered(dashcam_path, DASHCAM_POOL_SEED_CAP, exclude)
    by_pa: dict[str, dict[str, Any]] = {}
    for i in range(0, len(seeds), CH_POOL_CHUNK):
        chunk = [p for p in seeds[i : i + CH_POOL_CHUNK] if p not in exclude]
        if not chunk:
            continue
        plist = "', '".join(chunk)
        sql = f"""
SELECT
  lower(pool_address) AS pa,
  toUInt64(count()) AS swap_cnt,
  toUInt32(uniqExact(intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}))) AS buckets_5m,
  toUInt32(uniqExact(toDate(coalesce(block_time, received_at_ms)))) AS active_days
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND coalesce(block_time, received_at_ms) >= now() - INTERVAL {WINDOW_DAYS} DAY
  AND lower(pool_address) IN ('{plist}')
GROUP BY pa
HAVING swap_cnt >= {MIN_SWAPS}
  AND buckets_5m >= {MIN_BUCKETS_5M}
  AND active_days >= {MIN_ACTIVE_DAYS}
FORMAT TabSeparated
""".strip()
        body = ch_docker_query(sql, timeout_sec=120.0)
        if not body:
            continue
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            pa = parts[0].lower()
            if pa in exclude:
                continue
            row = {
                "pool_address": pa,
                "swap_count_window": int(parts[1]),
                "bucket_count_5m_ch": int(parts[2]),
                "active_days_ch": int(parts[3]),
            }
            prev = by_pa.get(pa)
            if prev is None or row["swap_count_window"] > prev["swap_count_window"]:
                by_pa[pa] = row
            if len(by_pa) >= MAX_CH_SCAN_POOLS * 3:
                break
        if len(by_pa) >= MAX_CH_SCAN_POOLS * 3:
            break

    merged_swaps = sorted(by_pa.values(), key=lambda r: (-r["swap_count_window"], r["pool_address"]))
    typed: dict[str, dict[str, Any]] = {}
    for i in range(0, len(merged_swaps), CH_POOL_CHUNK):
        chunk_pa = [r["pool_address"] for r in merged_swaps[i : i + CH_POOL_CHUNK]]
        if not chunk_pa:
            continue
        plist2 = "', '".join(chunk_pa)
        dsql = f"""
SELECT lower(pool_address) AS pa
FROM eatlq.discovered_pools
WHERE chain_id = {CHAIN_ID}
  AND lower(pool_type) = 'v3'
  AND has_weth_leg = 1
  AND lower(pool_address) IN ('{plist2}')
FORMAT TabSeparated
""".strip()
        allowed = set()
        bod = ch_docker_query(dsql, timeout_sec=45.0)
        if bod:
            for ln in bod.splitlines():
                p = ln.strip().lower()
                if p.startswith("0x"):
                    allowed.add(p)
        for r in merged_swaps[i : i + CH_POOL_CHUNK]:
            if r["pool_address"] in allowed:
                typed[r["pool_address"]] = r
        if len(typed) >= MAX_CH_SCAN_POOLS * 2:
            break

    out = sorted(typed.values(), key=lambda z: (-z["swap_count_window"], z["pool_address"]))
    seen_out: set[str] = {r["pool_address"] for r in out}
    for r in merged_swaps:
        if len(out) >= MAX_CH_SCAN_POOLS:
            break
        pa = r["pool_address"]
        if pa in exclude or pa in seen_out:
            continue
        seen_out.add(pa)
        out.append(r)
    return out[:MAX_CH_SCAN_POOLS], "seed=dashcam_rank_swaps_dense_then_discovered_v3_weth_filter_plus_untyped_backfill"

def ch_pool_bucket_blocks(pool: str, bucket_ms_list: list[int]) -> dict[int, int]:
    """Map bucket start ms -> block_number at that 5m bucket (min swap block in bucket)."""
    if not bucket_ms_list:
        return {}
    pool = pool.lower()
    toks = ",".join(str(int(bm)) for bm in sorted(set(bucket_ms_list)))
    sql = f"""
SELECT
  intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}) * {BUCKET_MS} AS bm,
  min(block_number) AS bn
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND lower(pool_address) = '{pool}'
  AND intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}) * {BUCKET_MS} IN ({toks})
GROUP BY bm
FORMAT TabSeparated
""".strip()
    body = ch_docker_query(sql, timeout_sec=60.0)
    out: dict[int, int] = {}
    for line in (body.splitlines() if body else []):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            out[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return out


def dashcam_buckets_for_pools(pools: set[str], dashcam: Path) -> dict[str, set[int]]:
    byp: dict[str, set[int]] = defaultdict(set)
    with dashcam.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = (row.get("token_address") or "").strip().lower()
            if p not in pools:
                continue
            try:
                ts = int(float(row.get("timestamp", 0)))
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            bsec = (ts // 1000 // BUCKET_SEC) * BUCKET_SEC
            byp[p].add(bsec)
    return dict(byp)


def audit_pool_at_block(mod: Any, rpc: Any, pool: str, bn: int) -> dict[str, Any]:
    pool = pool.lower()
    bh = hex(bn)
    row: dict[str, Any] = {"pool": pool}
    try:
        t0 = mod.call_token0(rpc, pool, bn).lower()
        r1 = rpc.call("eth_call", [{"to": pool, "data": _SEL_TOKEN1}, bh])
        raw_t1 = r1.get("result") if r1 and "error" not in r1 else None
        t1 = ("0x" + raw_t1[-40:]).lower() if raw_t1 and len(raw_t1) >= 42 else None
        rf = rpc.call("eth_call", [{"to": pool, "data": _SEL_FEE_V3}, bh])
        fee_raw = None
        if rf and "error" not in rf and rf.get("result"):
            fee_raw = int(rf["result"], 16)
        rliq = rpc.call("eth_call", [{"to": pool, "data": _SEL_LIQ}, bh])
        liqu = None
        if rliq and "error" not in rliq and rliq.get("result"):
            liqu = int(rliq["result"], 16)
        rs = rpc.call("eth_call", [{"to": pool, "data": _SEL_SLOT0}, bh])
        res_s = rs.get("result") if rs and "error" not in rs else None
        sqrt_ok = False
        if res_s and len(res_s.replace("0x", "")) >= 64:
            sqrt_ok = int(res_s.replace("0x", "")[0:64], 16) > 0

        rr = rpc.call("eth_call", [{"to": pool, "data": mod._SEL_GET_RESERVES}, bh])
        v2_ok = bool(rr and "error" not in rr and rr.get("result") not in ("0x", "0x0", None))

        weth_leg = WETH.lower() in (t0, t1 or "")
        fee_ok = fee_raw is not None and fee_raw > 0
        liq_status = "ok" if liqu not in (None, 0) else ("missing_or_zero" if liqu == 0 else "missing_or_short")
        slot_status = "ok" if sqrt_ok else "missing_or_short"
        if not weth_leg:
            meta = {"metadata_status": "FAIL", "weth_ok": False, "fee_ok": fee_ok, "liq_status": liq_status, "slot_status": slot_status, "ptype": "non_weth", "t0": t0 or "", "t1": t1 or ""}
            return meta
        if v2_ok and not fee_ok:
            meta = {
                "metadata_status": "PASS",
                "weth_ok": True,
                "fee_ok": True,
                "liq_status": "missing_or_zero",
                "slot_status": "missing_or_short",
                "ptype": "v2",
                "t0": t0,
                "t1": t1 or "",
                "fee_raw": "",
            }
            return meta
        if not v2_ok and fee_ok:
            meta = {
                "metadata_status": "PASS",
                "weth_ok": True,
                "fee_ok": True,
                "liq_status": liq_status,
                "slot_status": slot_status,
                "ptype": "v3",
                "t0": t0,
                "t1": t1 or "",
                "fee_raw": fee_raw,
                "fee_bps": max(1, fee_raw // 100),
            }
            return meta
        meta = {"metadata_status": "UNKNOWN", "weth_ok": weth_leg, "fee_ok": fee_ok, "liq_status": liq_status, "slot_status": slot_status, "ptype": "ambiguous", "t0": t0 or "", "t1": t1 or ""}
        return meta
    except Exception:
        return {
            "metadata_status": "FAIL",
            "weth_ok": False,
            "fee_ok": False,
            "liq_status": "exception",
            "slot_status": "exception",
            "ptype": "",
            "t0": "",
            "t1": "",
        }


def entry_quote_pass(mod: Any, rpc: Any, meta: dict[str, Any], pool: str, bn: int, notional: int) -> bool:
    ptype = meta.get("ptype")
    pool = pool.lower()
    weth_l = WETH.lower()
    try:
        if ptype == "v2":
            t0 = mod.call_token0(rpc, pool, bn).lower()
            r0, r1 = mod.call_get_reserves(rpc, pool, bn)
            if t0 == weth_l:
                r_weth_e, r_tok_e = r0, r1
            else:
                r_weth_e, r_tok_e = r1, r0
            if r_weth_e < mod._MIN_WETH_RESERVE_WEI or r_tok_e <= 0:
                return False
            if notional * 100 > r_weth_e:
                return False
            tokens = mod.quote_v2_out(r_weth_e, r_tok_e, notional)
            return tokens > 0
        if ptype == "v3":
            t0 = (meta.get("t0") or "").lower()
            t1 = (meta.get("t1") or "").lower()
            fr = meta.get("fee_raw")
            if fr is None:
                return False
            fee_raw_i = int(fr)
            tok = t1 if t0 == weth_l else t0
            out = mod.call_quoter_v3(rpc, weth_l, tok, notional, fee_raw_i, bn)
            return int(out) > 0
    except Exception:
        return False
    return False


def representative_buckets(sorted_bsecs: list[int], ov_lo: int, ov_hi: int) -> list[int]:
    """Up to five deterministic buckets: earliest, median, latest, overlap start, overlap end."""
    if not sorted_bsecs:
        return []
    B = sorted_bsecs
    picks = [B[0], B[len(B) // 2], B[-1]]
    bs = next((x for x in B if ov_lo <= x <= ov_hi), B[0])
    be = next((x for x in reversed(B) if ov_lo <= x <= ov_hi), B[-1])
    picks.extend([bs, be])
    out: list[int] = []
    seen: set[int] = set()
    for x in picks:
        if x not in seen:
            seen.add(x)
            out.append(x)
    for x in B:
        if len(out) >= 5:
            break
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:5]


def hard_quota_manifest_rows(
    pass_pools: list[str],
    dash_sec_by_pool: dict[str, set[int]],
    overlap_lo: int,
    overlap_hi: int,
) -> list[tuple[str, int, int]] | None:
    pools = pass_pools[:6]
    if len(pools) < 4:
        return None
    keys_sec: dict[str, list[int]] = {}
    for p in pools:
        xs = sorted(x for x in dash_sec_by_pool.get(p, ()) if overlap_lo <= x <= overlap_hi)
        if len(xs) < 4:
            return None
        keys_sec[p] = xs
    inter = set(keys_sec[pools[0]])
    for p in pools[1:]:
        inter &= set(keys_sec[p])
    if len(inter) < 12:
        return None
    inter_sorted = sorted(inter)
    flat: list[tuple[int, str]] = []
    for bm in inter_sorted:
        for p in pools:
            flat.append((bm, p))

    sel: list[tuple[str, int, int]] = []
    c: Counter[str] = Counter()
    for bm, p in flat:
        if c[p] >= 60:
            continue
        sel.append((p, bm * 1000, bm))
        c[p] += 1
        if len(sel) >= 250:
            break
    if len(sel) < 120:
        return None
    for target in range(min(250, len(sel)), 119, -1):
        chunk = sel[:target]
        ctr = Counter(p for p, _, _ in chunk)
        if len(ctr) < 4:
            continue
        top = sorted(ctr.values(), reverse=True)
        if max(top) > 60:
            continue
        tot = sum(top)
        top1 = top[0] / tot
        top3 = sum(top[:3]) / tot
        if top1 <= 0.30 + 1e-12 and top3 <= 0.70 + 1e-12:
            return chunk
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if not args.write:
        print("pass --write", file=sys.stderr)
        return 1

    t_wall0 = time.time()
    env = {**load_dotenv_files(), **dict(os.environ)}
    ch_url = (env.get("CLICKHOUSE_URL") or "").strip()
    ch_u = env.get("CLICKHOUSE_USER") or ""
    ch_p = env.get("CLICKHOUSE_PASSWORD") or ""

    mining_source = "clickhouse_docker_exec"
    ch_reachable_host = ch_host_ping(ch_url, ch_u, ch_p)
    if ch_reachable_host:
        mining_source = "clickhouse_http_host"

    docker_smoke = ch_docker_query("SELECT 1 FORMAT TabSeparated").strip()
    if docker_smoke != "1":
        mining_source = "none"

    excl = build_exclusions()
    candidates: list[dict[str, Any]] = []
    ch_sql_audit = ""

    dashcam_path = REPO / "logs/dashcam.csv"

    if docker_smoke == "1":
        candidates, ch_sql_audit = ch_pool_candidates_seed_dashcam(dashcam_path, excl)
        mining_source = "clickhouse_docker_exec_seed_dashcam"
        if len(candidates) < 24:
            extra, aux_sql = ch_pool_candidates_docker(excl)
            ch_sql_audit = ch_sql_audit + ";" + aux_sql
            idx = {c["pool_address"] for c in candidates}
            for row in extra:
                if row["pool_address"] not in idx:
                    idx.add(row["pool_address"])
                    candidates.append(row)
            candidates = sorted(
                candidates,
                key=lambda z: (-z["swap_count_window"], z["pool_address"]),
            )[:MAX_CH_SCAN_POOLS]

    coverage_limited = False
    if not candidates:
        if docker_smoke != "1":
            mining_source = "none"
        else:
            coverage_limited = True

    cand_pools_ordered = [c["pool_address"] for c in candidates]
    pool_set = set(cand_pools_ordered)
    dash_secs = dashcam_buckets_for_pools(pool_set if pool_set else set(), dashcam_path)

    scored: list[dict[str, Any]] = []
    for c in candidates:
        p = c["pool_address"]
        ds = dash_secs.get(p, set())
        ch_b = int(c["bucket_count_5m_ch"])
        overlap_cov = len(ds)
        overlap_window = ""
        if ds:
            overlap_window = f"{min(ds)}_{max(ds)}"
        scored.append({**c, "dashcam_bucket_hits": overlap_cov, "overlap_window_bounds_sec_audit": overlap_window})

    scored = [r for r in scored if r["dashcam_bucket_hits"] >= 5]

    scored.sort(key=lambda r: (-r["dashcam_bucket_hits"], -r["swap_count_window"], r["pool_address"]))
    top_try = scored[:MAX_VIABILITY_POOLS]

    if not top_try:
        uni = {
            "kind": "sprint033a_ch_backed_universe_mining",
            "mining_sources_used": [mining_source],
            "clickhouse_reachable": docker_smoke == "1" or ch_reachable_host,
            "coverage_limited": True,
            "candidate_pools_returned": 0,
            "final_decision_status": "NEEDS_DATA_SOURCE_REPAIR",
            "decision_flags": {
                "FAILED_NO_7D_ALPHA_ACTIVE": True,
                "CH_BACKED_UNIVERSE_MINING_ALLOWED": True,
                "PRELABEL_VIABILITY_GATE_ALLOWED": True,
                "HARD_QUOTA_SMOKE_LABEL_ALLOWED": "bounded_only",
                "TRAINING_ALLOWED": False,
                "H12_ABLATION_ALLOWED": False,
                "PHASE6_ALLOWED": False,
                "LIVE_ALLOWED": False,
                "MODEL_PROMOTION_ALLOWED": False,
            },
            "elapsed_sec": round(time.time() - t_wall0, 2),
        }
        OUT_UNIVERSE.parent.mkdir(parents=True, exist_ok=True)
        OUT_UNIVERSE.write_text(json.dumps(uni, indent=2), encoding="utf-8")
        OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
        OUT_REPORT.write_text(
            "# Sprint033A — CH-backed hard-quota viability smoke\\n\\nNEEDS_DATA_SOURCE_REPAIR — no candidates.\\n",
            encoding="utf-8",
        )
        print("NEEDS_DATA_SOURCE_REPAIR")
        return 0

    union_secs: set[int] = set()
    for r in scored:
        union_secs |= dash_secs.get(r["pool_address"], set())
    overlap_relaxed = False
    if union_secs:
        overlap_lo = min(union_secs)
        overlap_hi = max(union_secs)
        strict_mins = [
            min(dash_secs[r["pool_address"]])
            for r in scored
            if dash_secs.get(r["pool_address"]) and r["dashcam_bucket_hits"] >= 5
        ]
        strict_maxs = [
            max(dash_secs[r["pool_address"]])
            for r in scored
            if dash_secs.get(r["pool_address"]) and r["dashcam_bucket_hits"] >= 5
        ]
        pair_lo = max(strict_mins) if strict_mins else overlap_lo
        pair_hi = min(strict_maxs) if strict_maxs else overlap_hi
        if pair_lo <= pair_hi:
            overlap_lo, overlap_hi = pair_lo, pair_hi
        else:
            overlap_relaxed = True
        overlap_valid = overlap_lo <= overlap_hi
    else:
        overlap_lo = 0
        overlap_hi = 0
        overlap_valid = False

    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    mod = load_label_mod()
    notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    pass_list_for_audit: list[str] = []

    viab_rows_out: list[dict[str, str]] = []
    pass_pools: list[str] = []
    rpc_trips = 0

    if not rpc_url:
        uni = {
            "kind": "sprint033a_ch_backed_universe_mining",
            "mining_sources_used": [mining_source, "dashcam_logs"],
            "clickhouse_reachable": docker_smoke == "1" or ch_reachable_host,
            "coverage_limited": coverage_limited,
            "preflight_rpc_url_present": False,
            "candidate_pools_returned": len(candidates),
            "final_decision_status": "NEW_COHORT_FOUND_NO_RPC",
            "decision_flags": {
                "FAILED_NO_7D_ALPHA_ACTIVE": True,
                "CH_BACKED_UNIVERSE_MINING_ALLOWED": True,
                "TRAINING_ALLOWED": False,
            },
            "elapsed_sec": round(time.time() - t_wall0, 2),
        }
        OUT_UNIVERSE.parent.mkdir(parents=True, exist_ok=True)
        OUT_UNIVERSE.write_text(json.dumps(uni, indent=2), encoding="utf-8")
        with OUT_CAND_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "pool_address",
                    "swap_count_window",
                    "bucket_count_5m_ch",
                    "active_days_ch",
                    "dashcam_bucket_hits",
                    "overlap_window_bounds_sec_audit",
                ],
            )
            w.writeheader()
            for r in scored[:80]:
                w.writerow({k: r.get(k, "") for k in w.fieldnames if w.fieldnames})
        print("NEW_COHORT_FOUND_NO_RPC")
        return 0

    rpc_plain = mod.RPC(rpc_url)
    latest_bn = int(rpc_plain.call("eth_getBlockByNumber", ["latest", False])["result"]["number"], 16)
    rpc_trips += 1

    quote_probes = 0
    for rec in top_try:
        pool = rec["pool_address"]
        bset = dash_secs.get(pool, set())
        if not overlap_valid:
            viab_rows_out.append(
                {
                    "pool_address_audit_key": pool,
                    "metadata_status": "UNKNOWN",
                    "weth_leg_verified": "false",
                    "fee_status": "UNKNOWN",
                    "liquidity_status": "UNKNOWN",
                    "slot0_status": "UNKNOWN",
                    "representative_bucket_count": "0",
                    "entry_quote_pass_count": "0",
                    "entry_quote_fail_count": "0",
                    "viability_status": "FAIL",
                    "failure_reason": "invalid_overlap_calendar_union",
                    "source": "CH+dashcam",
                }
            )
            continue
        if not bset:
            viab_rows_out.append(
                {
                    "pool_address_audit_key": pool,
                    "metadata_status": "UNKNOWN",
                    "weth_leg_verified": "false",
                    "fee_status": "UNKNOWN",
                    "liquidity_status": "UNKNOWN",
                    "slot0_status": "UNKNOWN",
                    "representative_bucket_count": "0",
                    "entry_quote_pass_count": "0",
                    "entry_quote_fail_count": "0",
                    "viability_status": "FAIL",
                    "failure_reason": "no_dashcam_buckets_for_pool",
                    "source": "CH+dashcam",
                }
            )
            continue
        Bsorted = sorted(bset)
        if len(Bsorted) < 4:
            viab_rows_out.append(
                {
                    "pool_address_audit_key": pool,
                    "metadata_status": "UNKNOWN",
                    "weth_leg_verified": "false",
                    "fee_status": "UNKNOWN",
                    "liquidity_status": "UNKNOWN",
                    "slot0_status": "UNKNOWN",
                    "representative_bucket_count": str(len(Bsorted)),
                    "entry_quote_pass_count": "0",
                    "entry_quote_fail_count": "0",
                    "viability_status": "FAIL",
                    "failure_reason": "insufficient_distinct_dashcam_buckets_lt_4",
                    "source": "CH+dashcam",
                }
            )
            continue
        rep = representative_buckets(Bsorted, overlap_lo, overlap_hi)
        bm_list = [x * 1000 for x in rep]
        blk_map = ch_pool_bucket_blocks(pool, bm_list)

        passes = 0
        fails = 0
        meta_latest = audit_pool_at_block(mod, rpc_plain, pool, latest_bn)
        rpc_trips += 6
        md_ok = meta_latest.get("metadata_status") == "PASS"
        weth_ok = meta_latest.get("weth_ok")
        fee_st = "PASS" if meta_latest.get("fee_ok") else "FAIL"
        liq_raw = str(meta_latest.get("liq_status") or "")
        slot_raw = str(meta_latest.get("slot_status") or "")
        pt = meta_latest.get("ptype")
        if pt == "v2":
            liq_csv = "PASS" if liq_raw == "missing_or_zero" else "FAIL"
            slot_csv = "PASS" if slot_raw == "missing_or_short" else "FAIL"
        else:
            liq_csv = "PASS" if liq_raw == "ok" else "FAIL"
            slot_csv = "PASS" if slot_raw == "ok" else "FAIL"

        lm = {
            "ptype": meta_latest.get("ptype", ""),
            "t0": meta_latest.get("t0", ""),
            "t1": meta_latest.get("t1", ""),
            "fee_bps": meta_latest.get("fee_bps", 30),
            "fee_raw": meta_latest.get("fee_raw"),
        }

        for bsec in rep:
            bm = bsec * 1000
            bn = blk_map.get(bm)
            if bn is None:
                fails += 1
                continue
            if quote_probes >= 2000:
                fails += 1
                continue
            ok = entry_quote_pass(mod, rpc_plain, lm, pool, int(bn), notional)
            quote_probes += 1
            rpc_trips += 2 if lm.get("ptype") == "v2" else 1
            if ok:
                passes += 1
            else:
                fails += 1

        need = len(rep)
        if need >= 5:
            min_entry_ok = 3
        elif need == 4:
            min_entry_ok = 3
        else:
            min_entry_ok = need
        viab = (
            "PASS"
            if md_ok
            and weth_ok
            and passes >= min_entry_ok
            and (passes + fails) == need
            else "FAIL"
        )
        if viab == "PASS":
            pass_pools.append(pool)

        viab_rows_out.append(
            {
                "pool_address_audit_key": pool,
                "metadata_status": meta_latest.get("metadata_status", "UNKNOWN"),
                "weth_leg_verified": str(bool(weth_ok)).lower(),
                "fee_status": fee_st,
                "liquidity_status": liq_csv,
                "slot0_status": slot_csv,
                "representative_bucket_count": str(len(rep)),
                "entry_quote_pass_count": str(passes),
                "entry_quote_fail_count": str(fails),
                "viability_status": viab,
                "failure_reason": ""
                if viab == "PASS"
                else (
                    "metadata_or_entry_quote_failed"
                    if not md_ok
                    else "historical_entry_quote_incomplete_or_zero"
                ),
                "source": "CH",
            }
        )

    OUT_VIAB_CSV.parent.mkdir(parents=True, exist_ok=True)
    vfields = [
        "pool_address_audit_key",
        "metadata_status",
        "weth_leg_verified",
        "fee_status",
        "liquidity_status",
        "slot0_status",
        "representative_bucket_count",
        "entry_quote_pass_count",
        "entry_quote_fail_count",
        "viability_status",
        "failure_reason",
        "source",
    ]
    with OUT_VIAB_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=vfields)
        w.writeheader()
        for row in viab_rows_out:
            w.writerow({k: row.get(k, "") for k in vfields})

    swap_rank = {r["pool_address"]: int(r["swap_count_window"]) for r in scored}
    pass_list = sorted(
        pass_pools,
        key=lambda p: (
            -len({x for x in dash_secs.get(p, ()) if overlap_lo <= x <= overlap_hi}),
            -swap_rank.get(p, 0),
            p,
        ),
    )

    man_rows: list[tuple[str, int, int]] | None = None
    for n in (6, 5, 4):
        found = False
        for combo in itertools.combinations(pass_list, n):
            mr = hard_quota_manifest_rows(list(combo), dash_secs, overlap_lo, overlap_hi)
            if mr is not None:
                man_rows = mr
                found = True
                break
        if found:
            break
    pass_list_for_audit = list(pass_list)
    final_status = "NO_VIABILITY_BALANCED_COHORT_FOUND"
    if man_rows is not None:
        final_status = "PENDING_SMOKE_EXECUTION"

    hard_gates = {}
    if man_rows:
        ctrm = Counter(p for p, _, _ in man_rows)
        tot = sum(ctrm.values())
        sh = sorted(ctrm.values(), reverse=True)
        hard_gates = {
            "top1_row_share_cap": 0.3,
            "top3_row_share_cap": 0.7,
            "computed_top1_row_share": round(sh[0] / tot, 6),
            "computed_top3_row_share": round(sum(sh[:3]) / tot, 6),
        }

    manifest_obj: dict[str, Any] | None = None
    if man_rows:
        ctrm = Counter(p for p, _, _ in man_rows)
        manifest_obj = {
            "kind": "ch_backed_hard_quota_viability_smoke_manifest",
            "created_before_quote": True,
            "no_training": True,
            "no_live": True,
            "source_artifacts": [
                "mvp/audits/sprint032d_residual_sanity_clip_and_robustness_report.md",
                "mvp/diagnostics/sprint032d_robust_candidate_readout_summary.json",
                "mvp/audits/sprint032c_historical_viability_repair_report.md",
                "mvp/diagnostics/sprint032c_historical_viability_audit.csv",
                "mvp/diagnostics/sprint032c_viability_verified_smoke_label_summary.json",
                "mvp/audits/sprint032b_label_semantics_repair_report.md",
                "mvp/diagnostics/sprint032b_pool_metadata_audit.csv",
                "mvp/audits/sprint031c_rebalanced_lineage_readout.md",
                "mvp/diagnostics/sprint031c_rebalanced_lineage_readout_summary.json",
                "mvp/audits/sprint030h_retimed_manifest_readout.md",
                "scripts/sprint033a_ch_backed_hard_quota_pipeline.py",
                "scripts/sprint030d_bounded_smoke_label.py",
                "docs/label-semantics-freeze.md",
                "docs/ml-training-gate.md",
                "mvp/audits/failed_no_7d_alpha_guard.md",
                "logs/dashcam.csv",
            ],
            "selected_pool_count": len(ctrm),
            "selected_rows_count": len(man_rows),
            "label_limit": 250,
            "per_pool_row_cap_enforced": 60,
            "hard_quota_gates": hard_gates,
            "metadata_status_required": "PASS",
            "historical_entry_viability_required": "PASS",
            "pool_selection_rule": (
                "sprint033a_ch_discovered_v3_weth_dense ∩ dashcam_calendar "
                "deterministic_viability_5_bucket_all_pass_then_swap_count_order "
                "hard_quota_round_robin_overlap_window"
            ),
            "bucket_selection_rule": "overlap_window_round_robin_all_pools_min_120_rows",
            "pool_address_audit_keys": sorted(ctrm.keys()),
            "bucket_count_by_pool": dict(ctrm),
            "overlap_window_ms_audit_only": {"start_ms": overlap_lo * 1000, "end_ms": overlap_hi * 1000},
            "manifest_rows": [{"pool_audit_only": p, "bucket_ms": bm, "bucket_sec": bs} for p, bm, bs in man_rows],
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(manifest_obj, indent=2), encoding="utf-8")

    uni = {
        "kind": "sprint033a_ch_backed_universe_mining",
        "mining_sources_used": [mining_source, "logs_dashcam_csv"],
        "clickhouse_reachable": docker_smoke == "1" or ch_reachable_host,
        "coverage_limited": coverage_limited,
        "ch_query_fingerprint_audit_only": "discovered_pools∩uniswap_v3_swaps_enriched",
        "exclusion_pool_count": len(excl),
        "candidate_pools_returned": len(candidates),
        "viability_pass_pool_count": len(pass_pools),
        "overlap_window_sec_audit_only": {
            "lo": overlap_lo,
            "hi": overlap_hi,
            "valid": overlap_valid,
            "pairwise_relaxed": overlap_relaxed,
        },
        "rpc_probes_viability_entry_quotes": quote_probes,
        "approx_rpc_round_trips_pre_smoke": rpc_trips,
        "hard_quota_manifest_gate": {
            "passed": man_rows is not None,
            "manifest_path": str(OUT_MANIFEST.relative_to(REPO)) if man_rows else "",
            "strategy_audit_only": (
                "pairwise_bucket_intersection_then_hard_quota_top_contraction_120_to_250_rows"
                if len(pass_list_for_audit) >= 4
                else "not_applicable"
            ),
            "combinatorial_search_audit_only": {"pool_order": pass_list_for_audit[:12], "n_sized_sets_tried_order": ["6", "5", "4"]},
        },
        "final_decision_status": final_status,
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "CH_BACKED_UNIVERSE_MINING_ALLOWED": True,
            "PRELABEL_VIABILITY_GATE_ALLOWED": True,
            "HARD_QUOTA_SMOKE_LABEL_ALLOWED": "bounded_only",
            "TRAINING_ALLOWED": False,
            "H12_ABLATION_ALLOWED": False,
            "PHASE6_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "MICRO_LIVE_EXECUTION_ALLOWED": False,
            "SMART_MONEY_GENERAL_LINE_OPEN": False,
            "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
        },
        "elapsed_sec": round(time.time() - t_wall0, 2),
    }
    OUT_UNIVERSE.parent.mkdir(parents=True, exist_ok=True)
    OUT_UNIVERSE.write_text(json.dumps(uni, indent=2), encoding="utf-8")

    with OUT_CAND_CSV.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "pool_address",
            "swap_count_window",
            "bucket_count_5m_ch",
            "active_days_ch",
            "dashcam_bucket_hits",
            "overlap_window_bounds_sec_audit",
            "overlap_hits_in_window_audit",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in scored[:120]:
            p = r["pool_address"]
            hits = len([x for x in dash_secs.get(p, ()) if overlap_lo <= x <= overlap_hi]) if overlap_valid else 0
            row = {k: r.get(k, "") for k in fields if k != "overlap_hits_in_window_audit"}
            row["overlap_hits_in_window_audit"] = hits
            w.writerow(row)

    print(final_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
