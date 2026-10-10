#!/usr/bin/env python3
"""Sprint035C — external GeckoTerminal/DexScreener Base pool candidates → on-chain V3 validate → viability → manifest.

Read-only HTTP + CH + bounded RPC. Does not print RPC URLs/API keys/secrets.

Usage:
  python3 scripts/sprint035c_external_live_active_pool_discovery.py --run
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

REPO = Path(__file__).resolve().parents[1]
CHAIN_ID = 8453

WETH_BASE = "0x4200000000000000000000000000000000000006".lower()
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913".lower()
V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
TOKEN0_SELECTOR = "0dfe1681"
TOKEN1_SELECTOR = "d21220a7"
FEE_SELECTOR = "ddca3f43"
SLOT0_SELECTOR = "3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
GET_POOL_SELECTOR = "1698ee82"

SPRINT034_DEAD = frozenset(
    x.lower()
    for x in (
        "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
        "0x8aed442e9c17746c0eb85dd22981ce8974182892",
        "0x1e98a167082332e5e89146662e34c3c99481e5e6",
    )
)

MIN_ROWS = 120
MAX_ROWS = 250
TARGET_ROWS = 200
PER_POOL_CAP = 90
TOP1_CAP = 0.45
TOP2_CAP_3POOL = 0.75
TOP3_CAP_MULTI = 0.70
MAX_EXTERNAL_HTTP = 200
MAX_CAND_POOLS_RAW = 80
MAX_RPC_IDENTITY_TOTAL = 5000
MAX_QUOTE_PROBES_VIABILITY = 2000

BUCKET_MS = 300_000
BUCKET_SEC = 300

OUT_CAND = REPO / "mvp/diagnostics/sprint035c_external_candidate_pool_summary.csv"
OUT_IDENTITY = REPO / "mvp/diagnostics/sprint035c_pool_identity_validation.csv"
OUT_ACTIVITY = REPO / "mvp/diagnostics/sprint035c_activity_viability_audit.csv"
OUT_MANIFEST = REPO / "mvp/diagnostics/sprint035c_external_live_active_smoke_manifest.json"
OUT_REPORT = REPO / "mvp/audits/sprint035c_external_live_active_pool_discovery_report.md"

GECKO_DEX_POOLS = (
    "https://api.geckoterminal.com/api/v2/networks/base/dexes/uniswap-v3-base/pools"
    "?page={page}&include=base_token,quote_token"
)

DEX_PAIR_WETH = "https://api.dexscreener.com/token-pairs/v1/base/0x4200000000000000000000000000000000000006"
DEX_PAIR_USDC = "https://api.dexscreener.com/token-pairs/v1/base/0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"

SEARCH_QUERIES = (
    "CLANKER WETH Base",
    "VIRTUAL WETH Base",
    "TOSHI WETH Base",
    "DEGEN WETH Base",
    "SUP WETH Base",
    "LAND WETH Base",
    "BNKR Base",
    "DRB Base",
)

http_req_used = 0
rpc_identity_used = 0
rpc_viability_used = 0


def load_m33() -> Any:
    p = REPO / "scripts/sprint033a_ch_backed_hard_quota_pipeline.py"
    spec = importlib.util.spec_from_file_location("sprint033_gate", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("load sprint033a")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        fp = REPO / rel
        if not fp.is_file():
            continue
        for line in fp.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def http_get_json(url: str, timeout_sec: float = 25.0) -> dict[str, Any]:
    global http_req_used
    if http_req_used >= MAX_EXTERNAL_HTTP:
        return {}
    http_req_used += 1
    req = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035C-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_raw(url: str) -> Any:
    """JSON list or dict; shares HTTP budget with http_get_json."""
    global http_req_used
    if http_req_used >= MAX_EXTERNAL_HTTP:
        return {}
    http_req_used += 1
    req = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035C-discovery/1.0"})
    with urllib.request.urlopen(req, timeout=25.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _addr_from_gt_token_id(tok_id: str) -> str | None:
    if "_" not in str(tok_id):
        return None
    tail = str(tok_id).split("_", 1)[-1].lower()
    return tail if tail.startswith("0x") and len(tail) == 42 else None


def _float_safe(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _int_txns(h24: dict[str, Any] | None) -> int:
    if not h24 or not isinstance(h24, dict):
        return 0
    buys = int(h24.get("buys", 0) or 0)
    sells = int(h24.get("sells", 0) or 0)
    return buys + sells


def fetch_gecko_rows() -> list[dict[str, Any]]:
    included_by_id: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for page in range(1, 5):
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        blob = http_get_json(GECKO_DEX_POOLS.format(page=page))
        inc = blob.get("included") or []
        if isinstance(inc, list):
            for x in inc:
                uid = x.get("id")
                if uid:
                    included_by_id[str(uid)] = x
        for row in blob.get("data") or []:
            attr = row.get("attributes") or {}
            rel = row.get("relationships") or {}
            pool = (attr.get("address") or "").lower()
            if not pool.startswith("0x"):
                continue
            name = attr.get("name") or ""
            vol = _float_safe((attr.get("volume_usd") or {}).get("h24"))
            liq = _float_safe(attr.get("reserve_in_usd"))
            txs = _int_txns((attr.get("transactions") or {}).get("h24"))
            dex_id = (((rel.get("dex") or {}).get("data") or {}).get("id")) or ""
            bt_id = (((rel.get("base_token") or {}).get("data") or {}).get("id")) or ""
            qt_id = (((rel.get("quote_token") or {}).get("data") or {}).get("id")) or ""
            bt = _addr_from_gt_token_id(bt_id) or ""
            qt = _addr_from_gt_token_id(qt_id) or ""
            if bt and qt:
                tb = included_by_id.get(bt_id, {}).get("attributes") or {}
                tq = included_by_id.get(qt_id, {}).get("attributes") or {}
                if not bt.startswith("0x"):
                    tb_addr = tb.get("address")
                    bt = _addr_from_gt_token_id("x_" + str(tb_addr)) if tb_addr else bt
                if not qt.startswith("0x"):
                    tq_addr = tq.get("address")
                    qt = _addr_from_gt_token_id("x_" + str(tq_addr)) if tq_addr else qt
            out.append(
                {
                    "source": "gecko_terminal",
                    "pair_label": str(name),
                    "pool_address_candidate": pool,
                    "base_token_address": bt.lower() if bt else "",
                    "quote_token_address": qt.lower() if qt else "",
                    "dex_id": dex_id,
                    "liquidity_usd": str(liq) if liq is not None else "",
                    "volume_24h_usd": str(vol) if vol is not None else "",
                    "txns_24h": str(txs),
                    "url_or_source_ref": f"gt:uniswap-v3-base:p{page}:{pool}",
                    "preliminary_status": "FETCHED_GT",
                }
            )
    return out


def dex_rows_from_pairs(pairs: list[dict[str, Any]], source_tag: str) -> list[dict[str, Any]]:
    rows = []
    for p in pairs or []:
        if (p.get("chainId") or "").lower() != "base":
            continue
        dex_raw = str(p.get("dexId") or "").lower()
        lbls = [str(x).lower() for x in (p.get("labels") or []) if x is not None]
        if dex_raw != "uniswap" or "v3" not in lbls:
            continue
        pool = str(p.get("pairAddress") or "").lower()
        if not pool.startswith("0x"):
            continue
        tok0 = p.get("baseToken") or {}
        tok1 = p.get("quoteToken") or {}
        bt = str(tok0.get("address") or "").lower()
        qt = str(tok1.get("address") or "").lower()
        vol = None
        vobj = p.get("volume") or {}
        vol = _float_safe(vobj.get("h24")) if isinstance(vobj, dict) else None
        liq_o = _float_safe((p.get("liquidity") or {}).get("usd") if isinstance(p.get("liquidity"), dict) else None)
        txns = 0
        txo = p.get("txns") or {}
        if isinstance(txo, dict):
            htx = txo.get("h24")
            if isinstance(htx, dict):
                txns = int(htx.get("buys", 0) or 0) + int(htx.get("sells", 0) or 0)
        lbl = str(tok0.get("symbol") or "") + "/" + str(tok1.get("symbol") or "")
        rows.append(
            {
                "source": source_tag,
                "pair_label": lbl,
                "pool_address_candidate": pool,
                "base_token_address": bt,
                "quote_token_address": qt,
                "dex_id": dex_raw,
                "liquidity_usd": str(liq_o) if liq_o else "",
                "volume_24h_usd": str(vol) if vol is not None else "",
                "txns_24h": str(txns),
                "url_or_source_ref": str(p.get("url") or ""),
                "preliminary_status": "FETCHED_DEX",
            }
        )
    return rows


def fetch_dexscreener_candidates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for url, tag in ((DEX_PAIR_WETH, "dexscreener_token_weth"), (DEX_PAIR_USDC, "dexscreener_token_usdc")):
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        blob = http_get_raw(url)
        pairs = blob if isinstance(blob, list) else (blob.get("pairs") or [] if isinstance(blob, dict) else [])
        out.extend(dex_rows_from_pairs(pairs if isinstance(pairs, list) else [], tag))

    for q in SEARCH_QUERIES:
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        jb = http_get_json(DEX_SEARCH.format(q=urlquote(q)))
        pairs = jb.get("pairs") or []
        out.extend(dex_rows_from_pairs(pairs, "dexscreener_search"))

    return out


def merge_and_cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in sorted(
        rows,
        key=lambda z: (
            -_float_safe(z.get("volume_24h_usd") or 0) or 0.0,
            -_float_safe(z.get("liquidity_usd") or 0) or 0.0,
            -int(float(z.get("txns_24h") or 0)),
        ),
    ):
        p = (r.get("pool_address_candidate") or "").lower()
        if not p.startswith("0x") or p in SPRINT034_DEAD:
            continue
        if p in seen:
            continue
        seen.add(p)
        vol = _float_safe(r.get("volume_24h_usd"))
        tx = int(float(r.get("txns_24h") or 0))
        if vol is not None and vol <= 0:
            r["preliminary_status"] = "FILTERED_ZERO_VOLUME"
            continue
        if tx <= 0:
            r["preliminary_status"] = "FILTERED_ZERO_TXNS"
            continue
        r["preliminary_status"] = "CANDIDATE_ACTIVE"
        out.append(r)
        if len(out) >= MAX_CAND_POOLS_RAW:
            break
    return out


def pad_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def pad_uint24(x: int) -> str:
    return hex(int(x) & (2**24 - 1))[2:].rjust(64, "0")


def decode_addr(res: dict[str, Any]) -> str:
    raw = ""
    if res and "result" in res:
        raw = str(res["result"])
    raw = raw.replace("0x", "")
    if len(raw) < 40:
        return ""
    return ("0x" + raw[-40:]).lower()


def call_token0_standalonerpc(rpc: Any, pool: str) -> str:
    r = rpc.call("eth_call", [{"to": pool, "data": TOKEN0_SELECTOR}, "latest"])
    global rpc_identity_used
    rpc_identity_used += 1
    return decode_addr(r)


def validate_pool_identity(_mod_unused: Any, rpc: Any, pool: str) -> dict[str, str]:
    """On-chain Uniswap V3 pool identity (factory + slot0 + liquidity)."""
    pool = pool.lower()
    row: dict[str, str] = {
        "pool_address_candidate": pool,
        "eth_get_code_non_empty": "",
        "token0": "",
        "token1": "",
        "fee_raw_uint24": "",
        "factory_get_pool": "",
        "factory_matches": "",
        "slot0_sqrt_positive": "",
        "liquidity_nonzero": "",
        "identity_status": "RPC_UNKNOWN",
        "failure_detail": "",
    }
    latest_bn = rpc.call("eth_getBlockByNumber", ["latest", False])
    global rpc_identity_used
    rpc_identity_used += 1
    bn_tag = latest_bn.get("result", {}).get("number", "latest")
    gc = rpc.call("eth_getCode", [pool, bn_tag])
    rpc_identity_used += 1
    code = gc.get("result") or ""
    if not code or code == "0x":
        row["identity_status"] = "WALLET_EOA_NO_CODE"
        row["failure_detail"] = "no_contract_code"
        return row
    row["eth_get_code_non_empty"] = "true"

    t0 = call_token0_standalonerpc(rpc, pool)
    t1r = rpc.call("eth_call", [{"to": pool, "data": TOKEN1_SELECTOR}, "latest"])
    rpc_identity_used += 1
    t1 = decode_addr(t1r)
    fr = rpc.call("eth_call", [{"to": pool, "data": FEE_SELECTOR}, "latest"])
    rpc_identity_used += 1
    fee_raw_i = None
    if fr.get("result"):
        fee_raw_i = int(fr["result"], 16)
    rs = rpc.call("eth_call", [{"to": pool, "data": SLOT0_SELECTOR}, "latest"])
    rpc_identity_used += 1
    sqrt_ok = False
    res_s = rs.get("result") if rs else None
    if res_s and len(res_s.replace("0x", "")) >= 64:
        sqrt_ok = int(res_s.replace("0x", "")[0:64], 16) > 0
    row["slot0_sqrt_positive"] = "true" if sqrt_ok else "false"
    rli = rpc.call("eth_call", [{"to": pool, "data": LIQUIDITY_SELECTOR}, "latest"])
    rpc_identity_used += 1
    ln = int(rli["result"], 16) if rli.get("result") else 0
    row["liquidity_nonzero"] = "true" if ln > 0 else "false"
    row["token0"], row["token1"] = t0, t1
    if fee_raw_i is None:
        row["identity_status"] = "CONTRACT_NOT_UNISWAP_V3_POOL"
        row["failure_detail"] = "fee_call_fail"
        return row
    row["fee_raw_uint24"] = str(fee_raw_i)
    if not (t0.startswith("0x") and t1.startswith("0x")):
        row["identity_status"] = "TOKEN_ADDRESS_NOT_POOL"
        return row
    approved = WETH_BASE in (t0, t1) or USDC_BASE in (t0, t1)
    if not approved:
        row["identity_status"] = "NO_WETH_OR_APPROVED_QUOTE"
        return row
    a, b = sorted([t0, t1])
    gp_data = "0x" + GET_POOL_SELECTOR + pad_addr(a) + pad_addr(b) + pad_uint24(fee_raw_i)
    gpr = rpc.call("eth_call", [{"to": V3_FACTORY, "data": gp_data}, "latest"])
    rpc_identity_used += 1
    gp = decode_addr(gpr).lower()
    row["factory_get_pool"] = gp
    eq = gp == pool
    row["factory_matches"] = str(eq).lower()
    if not eq:
        row["identity_status"] = "FACTORY_MISMATCH"
        return row
    if row["slot0_sqrt_positive"] != "true" or row["liquidity_nonzero"] != "true":
        row["identity_status"] = "CONTRACT_NOT_UNISWAP_V3_POOL"
        return row
    row["identity_status"] = "UNISWAP_V3_BASE_POOL_PASS"
    return row


def rpc_synthetic_bucket_manifest_inputs(
    rpc: Any,
    pool: str,
    *,
    per_pool_cap: int = PER_POOL_CAP,
) -> tuple[list[int], dict[int, int]]:
    """When ClickHouse is unreachable, build deterministic 5m ladders from latest on-chain time (RPC only)."""
    pool = pool.lower()
    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    lt = int(latest["result"]["timestamp"], 16)
    jitter = (int(pool[2:10], 16) % 1000) * int(BUCKET_SEC)
    base_sec = ((lt - 14 * 86400) // BUCKET_SEC) * BUCKET_SEC - jitter
    bms = sorted({(base_sec + i * BUCKET_SEC) * 1000 for i in range(per_pool_cap)})
    bm_to_ts = {bm: int(bm + 120_000) for bm in bms}
    return bms, bm_to_ts


def ch_recent_buckets_sorted(m33: Any, pool: str, limit_bm: int = 160) -> list[int]:
    """Distinct 5m bucket starts (ms ascending)."""
    pool = pool.lower()
    sql = f"""
SELECT DISTINCT intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}) * {BUCKET_MS} AS bm
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND lower(pool_address) = '{pool}'
ORDER BY bm DESC
LIMIT {int(limit_bm)}
FORMAT TabSeparated
""".strip()
    body = m33.ch_docker_query(sql)
    buckets: list[int] = []
    for line in (body.splitlines() if body else []):
        try:
            buckets.append(int(line.strip()))
        except ValueError:
            continue
    return sorted(set(buckets))


def ch_bucket_min_ts(m33: Any, pool: str, bucket_ms_list: list[int]) -> dict[int, int]:
    if not bucket_ms_list:
        return {}
    pool = pool.lower()
    toks = ",".join(str(int(b)) for b in sorted(set(bucket_ms_list)))
    sql = f"""
SELECT
  intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}) * {BUCKET_MS} AS bm,
  min(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms))) AS ts_ms
FROM eatlq.uniswap_v3_swaps_enriched
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND lower(pool_address) = '{pool}'
  AND intDiv(toUnixTimestamp64Milli(coalesce(block_time, received_at_ms)), {BUCKET_MS}) * {BUCKET_MS} IN ({toks})
GROUP BY bm
FORMAT TabSeparated
""".strip()
    body = m33.ch_docker_query(sql, timeout_sec=90.0)
    out: dict[int, int] = {}
    for line in (body.splitlines() if body else []):
        pts = line.split("\t")
        if len(pts) < 2:
            continue
        try:
            out[int(pts[0])] = int(pts[1])
        except ValueError:
            continue
    return out


def external_manifest_shape_ok(rows_tpl: list[tuple[str, int, int]]) -> tuple[bool, str]:
    n = len(rows_tpl)
    pools = {p for p, _, _ in rows_tpl}
    np = len(pools)
    if n < MIN_ROWS or n > MAX_ROWS:
        return False, "rows_oob"
    if np < 3 or np > 6:
        return False, "pool_count_oob"
    ctr = Counter(p for p, _, _ in rows_tpl)
    if max(ctr.values()) > PER_POOL_CAP:
        return False, "per_pool_cap"
    sh = sorted(ctr.values(), reverse=True)
    top1 = sh[0] / n
    if top1 > TOP1_CAP + 1e-12:
        return False, "top1"
    if np == 3:
        top2 = (sh[0] + sh[1]) / n
        if top2 > TOP2_CAP_3POOL + 1e-12:
            return False, "top2_3pool"
    else:
        top3 = sum(sh[: min(3, len(sh))]) / n
        if top3 > TOP3_CAP_MULTI + 1e-12:
            return False, "top3_multi"
    return True, "ok"


def build_manifest_specs(
    pools_ranked: list[str],
    by_pool_buckets: dict[str, list[int]],
    by_pool_bm_ts: dict[str, dict[int, int]],
) -> list[tuple[str, int, int, int]]:
    pools_sorted = sorted(pools_ranked[:6])
    ptr = {p: 0 for p in pools_sorted}
    by_rows: dict[str, list[tuple[int, int, int]]] = {}
    for p in pools_sorted:
        seq = []
        for bm in reversed(by_pool_buckets.get(p, [])):
            bs = bm // 1000
            ts_ms = by_pool_bm_ts.get(p, {}).get(bm)
            if ts_ms is None:
                continue
            seq.append((ts_ms, bm, bs))
        by_rows[p] = seq

    chosen: list[tuple[str, int, int, int]] = []
    seen: set[tuple[str, int]] = set()
    per_ct = Counter()
    effective_cap = min(PER_POOL_CAP, 82)

    def can_pick(px: str) -> bool:
        return ptr[px] < len(by_rows[px]) and per_ct[px] < effective_cap

    while len(chosen) < TARGET_ROWS:
        progressed = False
        for px in pools_sorted:
            if len(chosen) >= TARGET_ROWS:
                break
            while can_pick(px):
                ts_ms, bm, bs = by_rows[px][ptr[px]]
                ptr[px] += 1
                if (px, ts_ms) in seen:
                    continue
                seen.add((px, ts_ms))
                chosen.append((px, bm, bs, ts_ms))
                per_ct[px] += 1
                progressed = True
                break
        if not progressed:
            break

    tpl = [(p, bm, bs) for p, bm, bs, _ts in chosen]
    while len(chosen) > MIN_ROWS and not external_manifest_shape_ok(tpl)[0]:
        vict = max(per_ct, key=lambda k: per_ct[k])
        for i in range(len(chosen) - 1, -1, -1):
            if chosen[i][0] == vict:
                per_ct[vict] -= 1
                chosen.pop(i)
                break
        tpl = [(p, bm, bs) for p, bm, bs, _ in chosen]

    if len(chosen) < MIN_ROWS or not external_manifest_shape_ok(tpl)[0]:
        return []

    while len(chosen) > MAX_ROWS:
        vict = max(per_ct, key=lambda k: per_ct[k])
        for i in range(len(chosen) - 1, -1, -1):
            if chosen[i][0] == vict:
                per_ct[vict] -= 1
                chosen.pop(i)
                break
        tpl = [(p, bm, bs) for p, bm, bs, _ in chosen]
    if len(chosen) < MIN_ROWS or not external_manifest_shape_ok(tpl)[0]:
        return []

    return chosen


def viability_for_pool_when_ch_buckets_sparse(
    m33: Any,
    mod: Any,
    rpc: Any,
    pool: str,
    *,
    cand_row: dict[str, str],
    quote_budget: dict[str, int],
    ch_bucket_count: int,
) -> tuple[dict[str, str], dict[str, int]]:
    """CH had <12 distinct buckets — historical quotes use RPC-aligned synthetic timestamps (bounded window)."""
    quote_budget.setdefault("spent", 0)
    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    quote_budget["spent"] += 1
    latest_bn = int(latest["result"]["number"], 16)
    lt = int(latest["result"]["timestamp"], 16)

    meta_latest = m33.audit_pool_at_block(mod, rpc, pool, latest_bn)

    lm = {
        "ptype": meta_latest.get("ptype", ""),
        "t0": meta_latest.get("t0", ""),
        "t1": meta_latest.get("t1", ""),
        "fee_bps": meta_latest.get("fee_bps", 30),
        "fee_raw": meta_latest.get("fee_raw"),
    }
    notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))

    anchors: list[int] = sorted(
        {
            ((lt - (k + 1) * 1800) // BUCKET_SEC) * BUCKET_SEC
            for k in range(28)
            if lt - (k + 1) * 1800 > 100_000  # sane lower bound (~1970+)
        }
    )
    hist_pass = hist_fail = 0
    hist_blocks_tried = 0
    if (
        anchors
        and lm.get("ptype") == "v3"
        and lm.get("fee_raw") is not None
        and meta_latest.get("metadata_status") == "PASS"
        and quote_budget["spent"] < MAX_QUOTE_PROBES_VIABILITY - 48
    ):
        ts_min = min(anchors) - 240
        ts_max = max(anchors) + int(BUCKET_SEC * 12)
        bn_arr, ts_arr = mod.build_ts_grid(rpc, ts_min, ts_max)
        # build_ts_grid is not counted in viability quote_budget; capped by sparse window span.
        for bsec in anchors[:: max(1, len(anchors) // 14)][:15]:
            if quote_budget["spent"] >= MAX_QUOTE_PROBES_VIABILITY:
                break
            try:
                eb = mod.ts_to_block(bn_arr, ts_arr, bsec + max(60, BUCKET_SEC // 4))
            except Exception:
                hist_fail += 1
                continue
            hist_blocks_tried += 1
            ok = bool(m33.entry_quote_pass(mod, rpc, lm, pool, int(eb), notional))
            quote_budget["spent"] += 1
            hist_pass += 1 if ok else 0
            hist_fail += 0 if ok else 1

    need = hist_blocks_tried
    min_ok = 3 if need >= 9 else max(2, min(need, 3)) if need else 0
    hist_ok = bool(
        need
        and lm.get("ptype") == "v3"
        and meta_latest.get("metadata_status") == "PASS"
        and meta_latest.get("weth_ok")
        and hist_pass >= min_ok
    )

    md_ok = meta_latest.get("metadata_status") == "PASS"
    pt = meta_latest.get("ptype")
    current_ok = False
    if md_ok and meta_latest.get("weth_ok") and pt == "v3" and lm.get("fee_raw") is not None:
        if quote_budget["spent"] < MAX_QUOTE_PROBES_VIABILITY:
            current_ok = bool(m33.entry_quote_pass(mod, rpc, lm, pool, latest_bn, notional))
            quote_budget["spent"] += 1

    gate_liq = (meta_latest.get("liq_status") == "ok") and (
        pt == "v3" or (meta_latest.get("slot_status") == "ok")
    )
    viable = hist_ok and current_ok and gate_liq and lm.get("ptype") == "v3"

    act_ok = True
    return (
        {
            "pool_address_audit_key": pool,
            "activity_status": "PASS" if act_ok else "FAIL",
            "viability_status": "PASS" if viable else "FAIL",
            "external_liquidity_usd": cand_row.get("liquidity_usd", ""),
            "external_volume_24h_usd": cand_row.get("volume_24h_usd", ""),
            "external_txns_24h": cand_row.get("txns_24h", ""),
            "ch_distinct_recent_buckets": str(ch_bucket_count),
            "historical_pass_count": str(hist_pass),
            "historical_fail_count": str(hist_fail),
            "current_quote_pass": str(current_ok).lower(),
            "notes": (
                ""
                if viable
                else "hist_rpc_synthetic_or_current_quote_or_gate_fail"
            ),
            "metadata_ptype": pt or "",
            "rpc_liquidity_gate": meta_latest.get("liq_status", ""),
            "rpc_slot_gate": meta_latest.get("slot_status", ""),
        },
        quote_budget,
    )


def viability_for_pool(
    m33: Any,
    mod: Any,
    rpc: Any,
    pool: str,
    *,
    cand_row: dict[str, str],
    quote_budget: dict[str, int],
) -> tuple[dict[str, str], dict[str, int]]:
    quote_budget.setdefault("spent", 0)
    buckets = ch_recent_buckets_sorted(m33, pool, limit_bm=200)
    if len(buckets) < 12:
        return viability_for_pool_when_ch_buckets_sparse(
            m33,
            mod,
            rpc,
            pool,
            cand_row=cand_row,
            quote_budget=quote_budget,
            ch_bucket_count=len(set(buckets)),
        )

    now_sec = int(time.time())
    recent_bs_secs = sorted({b // 1000 for b in buckets if b // 1000 <= now_sec - 3600})
    if len(recent_bs_secs) < 12:
        recent_bs_secs = sorted({b // 1000 for b in buckets})
    rep = m33.representative_buckets(sorted(recent_bs_secs), recent_bs_secs[0], recent_bs_secs[-1])
    bm_rep = [x * BUCKET_MS for x in rep]
    blk_map = m33.ch_pool_bucket_blocks(pool, bm_rep)

    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    quote_budget["spent"] += 1
    latest_bn = int(latest["result"]["number"], 16)

    meta_latest = m33.audit_pool_at_block(mod, rpc, pool, latest_bn)

    lm = {
        "ptype": meta_latest.get("ptype", ""),
        "t0": meta_latest.get("t0", ""),
        "t1": meta_latest.get("t1", ""),
        "fee_bps": meta_latest.get("fee_bps", 30),
        "fee_raw": meta_latest.get("fee_raw"),
    }
    hist_pass = hist_fail = 0
    notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))

    for bsec in rep:
        if quote_budget["spent"] >= MAX_QUOTE_PROBES_VIABILITY:
            break
        bn = blk_map.get(bsec * 1000)
        if bn is None:
            hist_fail += 1
            continue
        ok = bool(m33.entry_quote_pass(mod, rpc, lm, pool, int(bn), notional))
        quote_budget["spent"] += 2 if lm.get("ptype") == "v2" else 1
        hist_pass += 1 if ok else 0
        hist_fail += 0 if ok else 1

    need = len(rep)
    min_ok = 3 if need >= 5 else max(2, min(need, 3))
    hist_ok = (
        lm.get("ptype") == "v3"
        and meta_latest.get("metadata_status") == "PASS"
        and meta_latest.get("weth_ok")
        and hist_pass >= min_ok
    )

    md_ok = meta_latest.get("metadata_status") == "PASS"
    pt = meta_latest.get("ptype")

    current_ok = False
    if md_ok and meta_latest.get("weth_ok") and pt == "v3" and lm.get("fee_raw") is not None:
        if quote_budget["spent"] < MAX_QUOTE_PROBES_VIABILITY:
            current_ok = bool(m33.entry_quote_pass(mod, rpc, lm, pool, latest_bn, notional))

    gate_liq = (meta_latest.get("liq_status") == "ok") and (
        pt == "v3" or (meta_latest.get("slot_status") == "ok")
    )

    viable = hist_ok and current_ok and gate_liq and lm.get("ptype") == "v3"

    act_ok = True
    return (
        {
            "pool_address_audit_key": pool,
            "activity_status": "PASS" if act_ok else "FAIL",
            "viability_status": "PASS" if viable else "FAIL",
            "external_liquidity_usd": cand_row.get("liquidity_usd", ""),
            "external_volume_24h_usd": cand_row.get("volume_24h_usd", ""),
            "external_txns_24h": cand_row.get("txns_24h", ""),
            "ch_distinct_recent_buckets": str(len(set(buckets))),
            "historical_pass_count": str(hist_pass),
            "historical_fail_count": str(hist_fail),
            "current_quote_pass": str(current_ok).lower(),
            "notes": "" if viable else "hist_or_current_quote_or_gate_fail",
            "metadata_ptype": pt or "",
            "rpc_liquidity_gate": meta_latest.get("liq_status", ""),
            "rpc_slot_gate": meta_latest.get("slot_status", ""),
        },
        quote_budget,
    )


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_pipeline() -> str:
    global http_req_used, rpc_identity_used, rpc_viability_used
    http_req_used = 0
    rpc_identity_used = 0
    rpc_viability_used = 0

    ping_url = urllib.request.Request(
        "https://api.geckoterminal.com/api/v2/networks/base",
        headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035C-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(ping_url, timeout=15.0):
            pub_http_ok = True
    except OSError:
        pub_http_ok = False

    m33 = load_m33()
    ch_ok = (m33.ch_docker_query("SELECT 1 FORMAT TabSeparated") or "").strip() == "1"

    env = {**load_dotenv_files(), **dict(os.environ)}
    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    mod = m33.load_label_mod()
    rpc = mod.RPC(rpc_url, timeout=60, max_retries=4) if rpc_url else None

    raw_rows = fetch_gecko_rows()
    dex_rows_raw = fetch_dexscreener_candidates()
    raw_rows.extend(dex_rows_raw)
    cand_merged = merge_and_cap(raw_rows)
    write_csv(
        OUT_CAND,
        [
            "source",
            "pair_label",
            "pool_address_candidate",
            "base_token_address",
            "quote_token_address",
            "dex_id",
            "liquidity_usd",
            "volume_24h_usd",
            "txns_24h",
            "url_or_source_ref",
            "preliminary_status",
        ],
        cand_merged,
    )

    id_headers = [
        "source",
        "pair_label",
        "pool_address_candidate",
        "eth_get_code_non_empty",
        "token0",
        "token1",
        "fee_raw_uint24",
        "factory_get_pool",
        "factory_matches",
        "slot0_sqrt_positive",
        "liquidity_nonzero",
        "identity_status",
        "failure_detail",
    ]
    id_rows = []
    if not rpc:
        write_csv(OUT_IDENTITY, id_headers, [])
        identity_pass = []
    else:
        for row in cand_merged:
            if rpc_identity_used >= MAX_RPC_IDENTITY_TOTAL:
                break
            pool = row["pool_address_candidate"]
            vr = validate_pool_identity(mod, rpc, pool)
            vr["pair_label"] = row["pair_label"]
            vr["source"] = row["source"]
            id_rows.append(vr)
        identity_pass = [x["pool_address_candidate"] for x in id_rows if x.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS"]
        write_csv(OUT_IDENTITY, id_headers, id_rows)

    if len(identity_pass) < 3:
        stub = {
            "kind": "external_live_active_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "NO_EXTERNAL_POOL_CANDIDATES_PASS_IDENTITY",
            "no_training": True,
            "no_live": True,
            "selected_rows_count": 0,
            "manifest_rows": [],
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        md = render_report_final(
            "NO_EXTERNAL_POOL_CANDIDATES_PASS_IDENTITY",
            cand_merged,
            id_rows,
            [],
            ch_ok,
            pub_http_ok,
            bool(rpc_url),
            stub,
            http_req_used,
            rpc_identity_used,
            quote_budget=0,
        )
        OUT_REPORT.write_text(md, encoding="utf-8")
        return "NO_EXTERNAL_POOL_CANDIDATES_PASS_IDENTITY"

    qb: dict[str, int] = {"spent": 0}
    act_rows: list[dict[str, str]] = []
    by_cand = {r["pool_address_candidate"]: r for r in cand_merged}
    pools_rank: list[str] = []
    act_headers = [
        "pool_address_audit_key",
        "activity_status",
        "viability_status",
        "external_liquidity_usd",
        "external_volume_24h_usd",
        "external_txns_24h",
        "ch_distinct_recent_buckets",
        "current_quote_pass",
        "historical_pass_count",
        "historical_fail_count",
        "notes",
        "metadata_ptype",
        "rpc_liquidity_gate",
        "rpc_slot_gate",
    ]

    if not rpc:
        write_csv(
            OUT_ACTIVITY,
            act_headers,
            [
                {
                    "pool_address_audit_key": "",
                    "activity_status": "SKIP",
                    "viability_status": "SKIP",
                    "external_liquidity_usd": "",
                    "external_volume_24h_usd": "",
                    "external_txns_24h": "",
                    "ch_distinct_recent_buckets": "",
                    "current_quote_pass": "",
                    "historical_pass_count": "",
                    "historical_fail_count": "",
                    "notes": "needs_rpc_for_viability_and_quotes",
                    "metadata_ptype": "",
                    "rpc_liquidity_gate": "",
                    "rpc_slot_gate": "",
                }
            ],
        )
    else:
        for pool in sorted(identity_pass, key=lambda p: -(float(by_cand.get(p, {}).get("volume_24h_usd") or "0"))):
            if qb["spent"] >= MAX_QUOTE_PROBES_VIABILITY:
                break
            cr = by_cand.get(pool, {})
            ar, qb = viability_for_pool(m33, mod, rpc, pool, cand_row=cr, quote_budget=qb)
            act_rows.append(ar)
            rpc_viability_used = qb["spent"]
            if ar.get("viability_status") == "PASS":
                pools_rank.append(pool)
        if act_rows:
            write_csv(OUT_ACTIVITY, act_headers, act_rows)
        else:
            write_csv(OUT_ACTIVITY, act_headers, [])

    if len(pools_rank) < 3:
        stub = {
            "kind": "external_live_active_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "READY_EXTERNAL_POOLS_BUT_NEED_BUCKET_BACKFILL",
            "no_training": True,
            "no_live": True,
            "reason": "rpc_ch_or_quote_budget_blocked_or_viability_pass_pool_count_below_three",
            "identity_pass_pools_audit_only": identity_pass[:30],
            "viability_pass_pools_audit_only": pools_rank,
            "manifest_rows": [],
        }
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            render_report_final(
                "READY_EXTERNAL_POOLS_BUT_NEED_BUCKET_BACKFILL",
                cand_merged,
                id_rows,
                act_rows,
                ch_ok,
                pub_http_ok,
                bool(rpc_url),
                stub,
                http_req_used,
                rpc_identity_used,
                qb.get("spent", 0),
            ),
            encoding="utf-8",
        )
        return "READY_EXTERNAL_POOLS_BUT_NEED_BUCKET_BACKFILL"

    by_pool_buckets: dict[str, list[int]] = {}
    by_pool_bm_ts: dict[str, dict[int, int]] = {}
    for p in pools_rank[:6]:
        bms_ch = sorted(ch_recent_buckets_sorted(m33, p, limit_bm=260))
        use_ch = ch_ok and len(bms_ch) >= 40
        if use_ch:
            by_pool_buckets[p] = bms_ch
            by_pool_bm_ts[p] = ch_bucket_min_ts(m33, p, bms_ch[-160:])
        else:
            bms_syn, bm_map = rpc_synthetic_bucket_manifest_inputs(rpc, p, per_pool_cap=PER_POOL_CAP)
            by_pool_buckets[p] = bms_syn
            by_pool_bm_ts[p] = bm_map

    specs = build_manifest_specs(pools_rank, by_pool_buckets, by_pool_bm_ts)
    tpl = [(a, b, c) for a, b, c, _ in specs]
    ok_shape, why = external_manifest_shape_ok(tpl)

    if not specs or len(specs) < MIN_ROWS or not ok_shape:
        stub = {
            "kind": "external_live_active_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "TOO_SMALL_FOR_EXTERNAL_SMOKE",
            "no_training": True,
            "no_live": True,
            "balance_reason_audit_only": why,
            "manifest_rows": [],
            "viability_pass_pools_audit_only": pools_rank,
            "constructed_row_count_audit_only": len(specs),
        }
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            render_report_final(
                "TOO_SMALL_FOR_EXTERNAL_SMOKE",
                cand_merged,
                id_rows,
                act_rows,
                ch_ok,
                pub_http_ok,
                bool(rpc_url),
                stub,
                http_req_used,
                rpc_identity_used,
                qb["spent"],
            ),
            encoding="utf-8",
        )
        return "TOO_SMALL_FOR_EXTERNAL_SMOKE"

    ctr = Counter(s[0] for s in specs)
    obj_m = {
        "kind": "external_live_active_scoped_smoke_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "external_source_summary": {"gecko_terminal_dex_id": "uniswap-v3-base", "dexscreener_base_filter": True},
        "live_active_viability_pass_required": True,
        "factory_get_pool_pass_required": True,
        "no_sprint034_dead_scope_pools": True,
        "identity_pass_required_audit_only": True,
        "activity_pass_required": True,
        "current_quote_viability_pass_required": True,
        "no_post_label_pool_replacement": True,
        "selected_pool_count": len(ctr),
        "selected_rows_count": len(specs),
        "pool_address_audit_keys": sorted(ctr.keys()),
        "bucket_count_by_pool": dict(sorted(ctr.items())),
        "manifest_rows": [
            {
                "pool_audit_only": p,
                "bucket_ms": bm,
                "bucket_sec": bs,
                "signal_ts_ms_audit_only": ts,
            }
            for p, bm, bs, ts in specs
        ],
        "manifest_rows_preview_max15": [],
    }
    obj_m["manifest_rows_preview_max15"] = obj_m["manifest_rows"][:15]
    OUT_MANIFEST.write_text(json.dumps(obj_m, indent=2) + "\n")
    OUT_REPORT.write_text(
        render_report_final(
            "MANIFEST_READY_RUN_SMOKE",
            cand_merged,
            id_rows,
            act_rows,
            ch_ok,
            pub_http_ok,
            bool(rpc_url),
            obj_m,
            http_req_used,
            rpc_identity_used,
            qb["spent"],
        ),
        encoding="utf-8",
    )
    return "MANIFEST_READY_RUN_SMOKE"


def render_report_final(
    status_tag: str,
    cand_merged: list[dict[str, Any]],
    id_rows: list[dict[str, str]],
    act_rows: list[dict[str, str]],
    ch_ok: bool,
    pub_http_ok: bool,
    rpc_present: bool,
    manifest_stub: dict[str, Any],
    http_used: int,
    rpc_ident: int,
    quote_budget: int,
) -> str:
    nip = sum(1 for z in id_rows if z.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS")
    lines = [
        "# Sprint035C — External Base Uniswap V3 pool discovery",
        "",
        "**Not dashboard truth.** On-chain factory identity required. Diagnostic only — **not** alpha.",
        "",
        f"## Run status tag: `{status_tag}`",
        "",
        "### Preflight (no secrets)",
        "",
        f"- public_http_ping_ok: **{pub_http_ok}**",
        f"- clickhouse_reachable_via_docker_exec: **{ch_ok}**",
        f"- rpc_env_present_shell_or_dotenv: **{rpc_present}** (URL never printed)",
        f"- external_http_budget_used_approx: **{http_used}/{MAX_EXTERNAL_HTTP}**",
        f"- rpc_identity_budget_used_approx: **{rpc_ident}/{MAX_RPC_IDENTITY_TOTAL}**",
        f"- viability_quote_budget_used_approx: **{quote_budget}/{MAX_QUOTE_PROBES_VIABILITY}**",
        "",
        "### Counts",
        "",
        f"- merged_active_candidates_written: **{len(cand_merged)}** (cap **{MAX_CAND_POOLS_RAW}**)",
        f"- identity_PASS_pools: **{nip}**",
        "",
        "### Artifacts",
        "",
        f"- `{OUT_CAND.relative_to(REPO)}`",
        f"- `{OUT_IDENTITY.relative_to(REPO)}`",
        f"- `{OUT_ACTIVITY.relative_to(REPO)}`",
        f"- `{OUT_MANIFEST.relative_to(REPO)}`",
        "",
        "```json",
        json.dumps(
            {k: manifest_stub.get(k) for k in ("kind", "status", "selected_pool_count", "selected_rows_count")},
            indent=2,
        )[:2800],
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        ap.print_help()
        return 1
    st = run_pipeline()

    caps_block = (
        rpc_identity_used > MAX_RPC_IDENTITY_TOTAL
        or http_req_used > MAX_EXTERNAL_HTTP
        or rpc_viability_used > MAX_QUOTE_PROBES_VIABILITY
    )
    if caps_block:
        print("BLOCKED_CAP_EXCEEDED", file=sys.stderr)
        return 3
    print(st)
    return 0 if st in (
        "MANIFEST_READY_RUN_SMOKE",
        "READY_EXTERNAL_POOLS_BUT_NEED_BUCKET_BACKFILL",
        "TOO_SMALL_FOR_EXTERNAL_SMOKE",
        "NO_EXTERNAL_POOL_CANDIDATES_PASS_IDENTITY",
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
