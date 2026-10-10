#!/usr/bin/env python3
"""Sprint035D — momentum-filtered Base Uniswap V3 pool discovery.

Extends Sprint035C by adding momentum/sentiment signals to candidate selection:
  - 1h / 6h price momentum (priceChange.h1, h6)
  - 1h buy/sell imbalance (txns.h1.buys / txns.h1.sells)
  - net-buy pressure score
  - 5m/15m close-over-open trend (GeckoTerminal OHLCV, optional)
  - recent 1h txn density floor
  - liquidity band filter (≥30k, ≤20M USD)
  - stablecoin-pair exclusion
  - post-peak dump exclusion (h1 < -8% or h6 < -20%)

Only pools that pass the momentum score gate are forwarded to identity and
viability validation, and ultimately to the scoped smoke manifest.

Read-only HTTP + CH + bounded RPC. Does not print RPC URLs/API keys/secrets.

Usage:
  python3 scripts/sprint035d_momentum_filtered_discovery.py --run
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

# Known Base stablecoins (both tokens being stablecoins → exclude pair)
BASE_STABLES: frozenset[str] = frozenset(
    x.lower()
    for x in (
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x4a3a6dd60a34bb2aba60d73b4c88d1e535f79e18",  # MIM
        "0x368181499736d0c0cc614dbb145e2ec1ac86b8c6",  # CUSD
        "0x04d5ddf5f3a8939889f11e97f8c4bb48317f1938",  # USDz
    )
)

# Large-cap reference pairs to use only as WETH/USDC sanity controls —
# exclude from meme-momentum cohort (too correlated with macro)
EXCLUDED_LARGE_CAP_POOLS: frozenset[str] = frozenset(
    x.lower()
    for x in (
        "0xd0b53d9277642d899df5c87a3966a349a798f224",  # USDC/WETH 0.05%
        "0x6c561b446416e1a00e8e93e221854d6ea4171372",  # WETH/USDC 0.3%
        "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",  # WETH/USDC 0.01%
        "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",  # cbBTC/USDC
        "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",  # cbBTC/WETH
    )
)

SPRINT034_DEAD: frozenset[str] = frozenset(
    x.lower()
    for x in (
        "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
        "0x8aed442e9c17746c0eb85dd22981ce8974182892",
        "0x1e98a167082332e5e89146662e34c3c99481e5e6",
    )
)

# ─── caps ──────────────────────────────────────────────────────────────────
MAX_EXTERNAL_HTTP = 200
MAX_CAND_POOLS_RAW = 80
MAX_RPC_IDENTITY_TOTAL = 5000
MAX_QUOTE_PROBES_VIABILITY = 2000
MIN_ROWS = 120
MAX_ROWS = 250
TARGET_ROWS = 200
PER_POOL_CAP = 90
TOP1_CAP = 0.45
TOP2_CAP_3POOL = 0.75
TOP3_CAP_MULTI = 0.70
BUCKET_MS = 300_000
BUCKET_SEC = 300

# ─── momentum thresholds ───────────────────────────────────────────────────
MOMENTUM_SCORE_MIN = 3          # pools below this are not forwarded to identity
LIQUIDITY_USD_MIN = 30_000      # dead-pool floor
LIQUIDITY_USD_MAX = 20_000_000  # exclude mega-pools (low per-trade alpha)
DUMP_H1_FLOOR = -8.0            # h1 price-change% below this → hard exclude
DUMP_H6_FLOOR = -20.0           # h6 below this → hard exclude

# 1h txn density floor: if both buys+sells < this, pool is essentially idle this hour
TXNS_1H_MIN = 5

# ─── output paths ──────────────────────────────────────────────────────────
OUT_CAND = REPO / "mvp/diagnostics/sprint035d_momentum_filtered_candidate_summary.csv"
OUT_IDENTITY = REPO / "mvp/diagnostics/sprint035d_pool_identity_validation.csv"
OUT_ACTIVITY = REPO / "mvp/diagnostics/sprint035d_activity_viability_audit.csv"
OUT_MANIFEST = REPO / "mvp/diagnostics/sprint035d_momentum_filtered_smoke_manifest.json"
OUT_REPORT = REPO / "mvp/audits/sprint035d_momentum_filtered_pool_discovery_report.md"

# ─── external API endpoints ────────────────────────────────────────────────
GECKO_DEX_POOLS = (
    "https://api.geckoterminal.com/api/v2/networks/base/dexes/uniswap-v3-base/pools"
    "?page={page}&include=base_token,quote_token"
)
DEX_PAIR_WETH = "https://api.dexscreener.com/token-pairs/v1/base/0x4200000000000000000000000000000000000006"
DEX_PAIR_USDC = "https://api.dexscreener.com/token-pairs/v1/base/0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"
# Batch pairs endpoint: up to 30 addresses per call
DEX_PAIRS_BATCH = "https://api.dexscreener.com/latest/dex/pairs/base/{addrs}"
# GeckoTerminal 5m OHLCV for a pool
GECKO_OHLCV = (
    "https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/minute"
    "?aggregate=5&limit=24"
)

SEARCH_QUERIES = (
    "CLANKER WETH Base",
    "VIRTUAL WETH Base",
    "TOSHI WETH Base",
    "DEGEN WETH Base",
    "SUP WETH Base",
    "LAND WETH Base",
    "BNKR Base",
    "DRB Base",
    "SEAM USDC Base",
    "ZORA USDC Base",
    "AERO USDC Base",
)

# ─── global counters ───────────────────────────────────────────────────────
http_req_used: int = 0
rpc_identity_used: int = 0
rpc_viability_used: int = 0


# ─── helpers ───────────────────────────────────────────────────────────────

def load_c035() -> Any:
    """Import sprint035c as a module for reused identity/viability logic."""
    p = REPO / "scripts/sprint035c_external_live_active_pool_discovery.py"
    spec = importlib.util.spec_from_file_location("sprint035c_core", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint035c")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def load_m33() -> Any:
    p = REPO / "scripts/sprint033a_ch_backed_hard_quota_pipeline.py"
    spec = importlib.util.spec_from_file_location("sprint033_gate", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("load sprint033a")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
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
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035D-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError:
        return {}


def http_get_raw(url: str) -> Any:
    global http_req_used
    if http_req_used >= MAX_EXTERNAL_HTTP:
        return {}
    http_req_used += 1
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035D-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError:
        return {}


def _float_safe(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _addr_from_gt_token_id(tok_id: str) -> str | None:
    if "_" not in str(tok_id):
        return None
    tail = str(tok_id).split("_", 1)[-1].lower()
    return tail if tail.startswith("0x") and len(tail) == 42 else None


def _int_txns(h24_obj: Any) -> int:
    if not h24_obj or not isinstance(h24_obj, dict):
        return 0
    return int(h24_obj.get("buys", 0) or 0) + int(h24_obj.get("sells", 0) or 0)


# ─── candidate fetching (same as 035C) ─────────────────────────────────────

def fetch_gecko_rows() -> list[dict[str, Any]]:
    included_by_id: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for page in range(1, 6):
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        blob = http_get_json(GECKO_DEX_POOLS.format(page=page))
        for x in blob.get("included") or []:
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
            bt_id = (((rel.get("base_token") or {}).get("data") or {}).get("id")) or ""
            qt_id = (((rel.get("quote_token") or {}).get("data") or {}).get("id")) or ""
            bt = _addr_from_gt_token_id(bt_id) or ""
            qt = _addr_from_gt_token_id(qt_id) or ""
            out.append(
                {
                    "source": "gecko_terminal",
                    "pair_label": str(name),
                    "pool_address_candidate": pool,
                    "base_token_address": bt.lower() if bt else "",
                    "quote_token_address": qt.lower() if qt else "",
                    "dex_id": (((rel.get("dex") or {}).get("data") or {}).get("id")) or "",
                    "liquidity_usd": str(liq) if liq is not None else "",
                    "volume_24h_usd": str(vol) if vol is not None else "",
                    "txns_24h": str(txs),
                    "url_or_source_ref": f"gt:uniswap-v3-base:p{page}:{pool}",
                    "preliminary_status": "FETCHED_GT",
                    # momentum fields (filled later)
                    "price_change_h1": "",
                    "price_change_h6": "",
                    "price_change_h24": "",
                    "txns_1h_buys": "",
                    "txns_1h_sells": "",
                    "volume_1h_usd": "",
                    "trend_5m_slope": "",
                    "momentum_score": "",
                    "momentum_gate": "",
                }
            )
    return out


def _dex_rows_from_pairs(pairs: list[dict[str, Any]], source_tag: str) -> list[dict[str, Any]]:
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
        liq_o = _float_safe((p.get("liquidity") or {}).get("usd") if isinstance(p.get("liquidity"), dict) else None)
        vobj = p.get("volume") or {}
        vol = _float_safe(vobj.get("h24")) if isinstance(vobj, dict) else None
        txo = p.get("txns") or {}
        txns = 0
        if isinstance(txo, dict):
            htx = txo.get("h24")
            if isinstance(htx, dict):
                txns = int(htx.get("buys", 0) or 0) + int(htx.get("sells", 0) or 0)
        # extract momentum fields from the DexScreener pair object
        pc = p.get("priceChange") or {}
        txns1h = (p.get("txns") or {}).get("h1") or {}
        rows.append(
            {
                "source": source_tag,
                "pair_label": str(tok0.get("symbol") or "") + "/" + str(tok1.get("symbol") or ""),
                "pool_address_candidate": pool,
                "base_token_address": bt,
                "quote_token_address": qt,
                "dex_id": dex_raw,
                "liquidity_usd": str(liq_o) if liq_o else "",
                "volume_24h_usd": str(vol) if vol is not None else "",
                "txns_24h": str(txns),
                "url_or_source_ref": str(p.get("url") or ""),
                "preliminary_status": "FETCHED_DEX",
                "price_change_h1": str(pc.get("h1") or ""),
                "price_change_h6": str(pc.get("h6") or ""),
                "price_change_h24": str(pc.get("h24") or ""),
                "txns_1h_buys": str(int(txns1h.get("buys", 0) or 0)),
                "txns_1h_sells": str(int(txns1h.get("sells", 0) or 0)),
                "volume_1h_usd": str(_float_safe((p.get("volume") or {}).get("h1")) or ""),
                "trend_5m_slope": "",
                "momentum_score": "",
                "momentum_gate": "",
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
        out.extend(_dex_rows_from_pairs(pairs if isinstance(pairs, list) else [], tag))
    for q in SEARCH_QUERIES:
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        jb = http_get_json(DEX_SEARCH.format(q=urlquote(q)))
        out.extend(_dex_rows_from_pairs(jb.get("pairs") or [], "dexscreener_search"))
    return out


def merge_and_cap_raw(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by pool address, remove zero-volume/txn, cap at MAX_CAND_POOLS_RAW."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in sorted(
        rows,
        key=lambda z: (
            -(_float_safe(z.get("volume_24h_usd") or 0) or 0.0),
            -(_float_safe(z.get("liquidity_usd") or 0) or 0.0),
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
        if (vol is not None and vol <= 0) or tx <= 0:
            continue
        r["preliminary_status"] = "CANDIDATE_ACTIVE"
        out.append(r)
        if len(out) >= MAX_CAND_POOLS_RAW:
            break
    return out


# ─── momentum enrichment ───────────────────────────────────────────────────

def _dex_pair_detail(pairs_blob: Any) -> dict[str, dict[str, Any]]:
    """Parse /latest/dex/pairs batch response into {pool_lower: pair_obj}."""
    result: dict[str, dict[str, Any]] = {}
    if not pairs_blob:
        return result
    pairs_list = []
    if isinstance(pairs_blob, dict):
        pairs_list = pairs_blob.get("pairs") or []
    elif isinstance(pairs_blob, list):
        pairs_list = pairs_blob
    for p in pairs_list:
        addr = str(p.get("pairAddress") or "").lower()
        if addr.startswith("0x"):
            result[addr] = p
    return result


def enrich_with_momentum(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch-fetch DexScreener pair detail for pools that lack momentum fields."""
    pools_needing_enrich = [
        r["pool_address_candidate"].lower()
        for r in candidates
        if not r.get("price_change_h1")
    ]
    # Fetch in batches of 30 (DexScreener cap)
    enrich_map: dict[str, dict[str, Any]] = {}
    batch_size = 30
    for i in range(0, len(pools_needing_enrich), batch_size):
        if http_req_used >= MAX_EXTERNAL_HTTP - 2:
            break
        batch = pools_needing_enrich[i : i + batch_size]
        url = DEX_PAIRS_BATCH.format(addrs=",".join(batch))
        blob = http_get_json(url, timeout_sec=30.0)
        enrich_map.update(_dex_pair_detail(blob))

    for r in candidates:
        pool = r["pool_address_candidate"].lower()
        detail = enrich_map.get(pool)
        if not detail:
            continue
        pc = detail.get("priceChange") or {}
        txns1h = (detail.get("txns") or {}).get("h1") or {}
        if isinstance(txns1h, dict):
            r["txns_1h_buys"] = str(int(txns1h.get("buys", 0) or 0))
            r["txns_1h_sells"] = str(int(txns1h.get("sells", 0) or 0))
        if not r.get("price_change_h1"):
            r["price_change_h1"] = str(pc.get("h1") or "")
        if not r.get("price_change_h6"):
            r["price_change_h6"] = str(pc.get("h6") or "")
        if not r.get("price_change_h24"):
            r["price_change_h24"] = str(pc.get("h24") or "")
        vobj = detail.get("volume") or {}
        if not r.get("volume_1h_usd") and isinstance(vobj, dict):
            r["volume_1h_usd"] = str(_float_safe(vobj.get("h1")) or "")
        # refresh liquidity from pair detail (more up-to-date)
        liq = _float_safe((detail.get("liquidity") or {}).get("usd"))
        if liq is not None and not r.get("liquidity_usd"):
            r["liquidity_usd"] = str(liq)

    return candidates


def _gt_ohlcv_trend(pool: str) -> float | None:
    """Fetch last 24 5m candles from GeckoTerminal and return close-slope sign.
    Returns +1 if last close > open across window, -1 if lower, 0 if flat, None on error.
    """
    if http_req_used >= MAX_EXTERNAL_HTTP - 2:
        return None
    url = GECKO_OHLCV.format(pool=pool.lower())
    blob = http_get_json(url, timeout_sec=20.0)
    ohlcv = (blob.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    if len(ohlcv) < 4:
        return None
    try:
        # ohlcv_list rows: [timestamp, open, high, low, close, volume]
        first_open = float(ohlcv[0][1])
        last_close = float(ohlcv[-1][4])
        if first_open <= 0:
            return None
        pct = (last_close - first_open) / first_open * 100.0
        return pct
    except (IndexError, TypeError, ValueError):
        return None


def _is_stable_pair(base_token: str, quote_token: str) -> bool:
    """True if both tokens are stablecoins (exclude from meme momentum cohort)."""
    return base_token.lower() in BASE_STABLES and quote_token.lower() in BASE_STABLES


def momentum_score_row(r: dict[str, Any]) -> tuple[int, str]:
    """
    Score a candidate pool on momentum/sentiment signals.
    Returns (score: int, gate: 'PASS'|'FAIL'|'EXCLUDE').
    """
    pool = r.get("pool_address_candidate", "").lower()

    # Hard exclusions
    if pool in EXCLUDED_LARGE_CAP_POOLS or pool in SPRINT034_DEAD:
        return 0, "EXCLUDE_HARDLIST"

    bt = r.get("base_token_address", "").lower()
    qt = r.get("quote_token_address", "").lower()
    if _is_stable_pair(bt, qt):
        return 0, "EXCLUDE_STABLE_PAIR"

    liq = _float_safe(r.get("liquidity_usd")) or 0.0
    if liq < LIQUIDITY_USD_MIN:
        return 0, "EXCLUDE_ILLIQUID"
    if liq > LIQUIDITY_USD_MAX:
        return 0, "EXCLUDE_MEGA_POOL"

    h1 = _float_safe(r.get("price_change_h1"))
    h6 = _float_safe(r.get("price_change_h6"))

    if h1 is not None and h1 < DUMP_H1_FLOOR:
        return 0, "EXCLUDE_DUMP_H1"
    if h6 is not None and h6 < DUMP_H6_FLOOR:
        return 0, "EXCLUDE_DUMP_H6"

    score = 0
    # 1h price momentum (most predictive short-term signal)
    if h1 is not None:
        if h1 > 5.0:
            score += 4
        elif h1 > 2.0:
            score += 3
        elif h1 > 0.5:
            score += 2
        elif h1 > 0:
            score += 1
        # h1 between -8 and 0: score += 0 (neutral)

    # 6h direction as trend confirmation
    if h6 is not None:
        if h6 > 5.0:
            score += 2
        elif h6 > 0:
            score += 1

    # Buy/sell imbalance (1h)
    buys1h = int(float(r.get("txns_1h_buys") or 0))
    sells1h = int(float(r.get("txns_1h_sells") or 0))
    txns1h = buys1h + sells1h
    if txns1h < TXNS_1H_MIN:
        return 0, "EXCLUDE_IDLE_1H"

    bsr = buys1h / max(sells1h, 1)
    if bsr > 2.0:
        score += 3
    elif bsr > 1.5:
        score += 2
    elif bsr > 1.1:
        score += 1

    # 1h txn density
    if txns1h >= 50:
        score += 2
    elif txns1h >= 20:
        score += 1

    # Liquidity band sweet-spot: 100k–5M (good liquidity without being a blue-chip)
    if 100_000 <= liq <= 5_000_000:
        score += 1

    # 1h volume
    vol1h = _float_safe(r.get("volume_1h_usd")) or 0.0
    if vol1h > 100_000:
        score += 1

    # 5m trend from GeckoTerminal OHLCV (optional enrichment)
    slope = _float_safe(r.get("trend_5m_slope"))
    if slope is not None:
        if slope > 1.0:
            score += 2
        elif slope > 0:
            score += 1

    gate = "PASS" if score >= MOMENTUM_SCORE_MIN else "FAIL"
    return score, gate


def apply_momentum_filter(
    candidates: list[dict[str, Any]],
    *,
    ohlcv_budget: int = 20,
) -> list[dict[str, Any]]:
    """Score all candidates, optionally enrich top-N with OHLCV, return sorted by score."""
    # First pass: score without OHLCV
    for r in candidates:
        sc, gate = momentum_score_row(r)
        r["momentum_score"] = str(sc)
        r["momentum_gate"] = gate

    # Optionally enrich top-N candidates (pre-pass score ≥ 1) with GT OHLCV
    pre_pass = sorted(
        [r for r in candidates if int(r.get("momentum_score", 0)) >= 1],
        key=lambda x: -int(x.get("momentum_score", 0)),
    )
    for r in pre_pass[:ohlcv_budget]:
        if http_req_used >= MAX_EXTERNAL_HTTP - 2:
            break
        pool = r["pool_address_candidate"].lower()
        slope = _gt_ohlcv_trend(pool)
        if slope is not None:
            r["trend_5m_slope"] = f"{slope:.4f}"
            # Re-score with OHLCV slope
            sc, gate = momentum_score_row(r)
            r["momentum_score"] = str(sc)
            r["momentum_gate"] = gate

    return sorted(candidates, key=lambda x: -int(x.get("momentum_score", 0) or 0))


# ─── identity validation (delegates to sprint035c) ─────────────────────────

def validate_pool_identity(c035: Any, rpc: Any, pool: str) -> dict[str, str]:
    global rpc_identity_used
    # c035.validate_pool_identity resets its own global counter too;
    # we capture the delta by reading c035's counter before/after.
    before = c035.rpc_identity_used
    result = c035.validate_pool_identity(None, rpc, pool)
    rpc_identity_used += c035.rpc_identity_used - before
    return result


# ─── viability (delegates to sprint035c) ───────────────────────────────────

def viability_for_pool(
    c035: Any,
    m33: Any,
    mod: Any,
    rpc: Any,
    pool: str,
    *,
    cand_row: dict[str, str],
    quote_budget: dict[str, int],
) -> tuple[dict[str, str], dict[str, int]]:
    global rpc_viability_used
    ar, qb = c035.viability_for_pool(m33, mod, rpc, pool, cand_row=cand_row, quote_budget=quote_budget)
    rpc_viability_used = qb.get("spent", 0)
    return ar, qb


# ─── manifest builder (delegates to sprint035c shape checks) ───────────────

def external_manifest_shape_ok(rows_tpl: list[tuple[str, int, int]]) -> tuple[bool, str]:
    """Mirror of sprint035c's shape checker (3–6 pools, 120–250 rows, per-pool ≤90, concentration caps)."""
    n = len(rows_tpl)
    pools = {p for p, _, _ in rows_tpl}
    np_ = len(pools)
    if n < MIN_ROWS or n > MAX_ROWS:
        return False, "rows_oob"
    if np_ < 3 or np_ > 6:
        return False, "pool_count_oob"
    ctr = Counter(p for p, _, _ in rows_tpl)
    if max(ctr.values()) > PER_POOL_CAP:
        return False, "per_pool_cap"
    sh = sorted(ctr.values(), reverse=True)
    if sh[0] / n > TOP1_CAP + 1e-12:
        return False, "top1"
    if np_ == 3:
        if (sh[0] + sh[1]) / n > TOP2_CAP_3POOL + 1e-12:
            return False, "top2_3pool"
    else:
        if sum(sh[:3]) / n > TOP3_CAP_MULTI + 1e-12:
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
    per_ct: Counter[str] = Counter()
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


def rpc_synthetic_bucket_manifest_inputs(
    rpc: Any,
    pool: str,
    *,
    per_pool_cap: int = PER_POOL_CAP,
    lookback_days: int = 7,
) -> tuple[list[int], dict[int, int]]:
    """Build deterministic 5m ladders from recent chain time.
    Uses 7-day lookback (vs 035C's 14-day) to keep synthetic buckets more recent.
    """
    pool = pool.lower()
    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    lt = int(latest["result"]["timestamp"], 16)
    jitter = (int(pool[2:10], 16) % 1000) * int(BUCKET_SEC)
    base_sec = ((lt - lookback_days * 86400) // BUCKET_SEC) * BUCKET_SEC - jitter
    bms = sorted({(base_sec + i * BUCKET_SEC) * 1000 for i in range(per_pool_cap)})
    bm_to_ts = {bm: int(bm + 120_000) for bm in bms}
    return bms, bm_to_ts


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ─── CH helpers ────────────────────────────────────────────────────────────

def ch_recent_buckets_sorted(m33: Any, pool: str, limit_bm: int = 160) -> list[int]:
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


# ─── main pipeline ─────────────────────────────────────────────────────────

def _render_report(
    status_tag: str,
    cand_merged: list[dict[str, Any]],
    momentum_pass_count: int,
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
    vpass = sum(1 for z in act_rows if z.get("viability_status") == "PASS")
    lines = [
        "# Sprint035D — Momentum-filtered Base Uniswap V3 pool discovery",
        "",
        "**Not dashboard truth.** On-chain factory identity required. Diagnostic only — **not** alpha.",
        "",
        f"## Run status: `{status_tag}`",
        "",
        "### Preflight (no secrets printed)",
        "",
        f"- public_http_ok: **{pub_http_ok}**",
        f"- clickhouse_reachable: **{ch_ok}**",
        f"- rpc_env_present: **{rpc_present}** (URL never printed)",
        f"- external_http_used: **{http_used}/{MAX_EXTERNAL_HTTP}**",
        f"- rpc_identity_used: **{rpc_ident}/{MAX_RPC_IDENTITY_TOTAL}**",
        f"- viability_quote_used: **{quote_budget}/{MAX_QUOTE_PROBES_VIABILITY}**",
        "",
        "### Funnel",
        "",
        f"- raw_candidates: **{len(cand_merged)}** (cap {MAX_CAND_POOLS_RAW})",
        f"- momentum_PASS: **{momentum_pass_count}**",
        f"- identity_PASS: **{nip}**",
        f"- viability_PASS: **{vpass}**",
        "",
        "### Momentum filter rules (Sprint035D)",
        "",
        f"- Liquidity band: [{LIQUIDITY_USD_MIN:,.0f}, {LIQUIDITY_USD_MAX:,.0f}] USD",
        f"- Dump exclusion: h1 < {DUMP_H1_FLOOR}% or h6 < {DUMP_H6_FLOOR}%",
        f"- Idle exclusion: txns_1h < {TXNS_1H_MIN}",
        f"- Stablecoin-pair and large-cap exclusion applied",
        f"- Score gate ≥ {MOMENTUM_SCORE_MIN}: price momentum + buy/sell imbalance + txn density + liq band + 5m OHLCV slope",
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
        "### Why this matters vs Sprint035C",
        "",
        "Sprint035C selected pools that were alive and could quote, but had no upward-structure gate.",
        "Sprint035D adds real-time momentum signals (1h/6h price change, buy/sell imbalance, 1h txn density,",
        "optional 5m OHLCV close-slope) to bias the cohort toward pools currently in upward-structure.",
        "This does **not** prove alpha; it narrows the diagnostic cohort to pools with better potential entry quality.",
        "",
    ]
    return "\n".join(lines)


def run_pipeline() -> str:
    global http_req_used, rpc_identity_used, rpc_viability_used
    http_req_used = 0
    rpc_identity_used = 0
    rpc_viability_used = 0

    # Load modules
    c035 = load_c035()
    m33 = load_m33()
    env = {**load_dotenv_files(), **dict(os.environ)}
    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    mod = m33.load_label_mod()
    rpc = mod.RPC(rpc_url, timeout=60, max_retries=4) if rpc_url else None

    # Preflight
    try:
        req = urllib.request.Request(
            "https://api.geckoterminal.com/api/v2/networks/base",
            headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035D-discovery/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15.0):
            pub_http_ok = True
    except OSError:
        pub_http_ok = False

    ch_ok = (m33.ch_docker_query("SELECT 1 FORMAT TabSeparated") or "").strip() == "1"

    # T2: Fetch candidates
    raw_rows = fetch_gecko_rows()
    raw_rows.extend(fetch_dexscreener_candidates())
    cand_merged = merge_and_cap_raw(raw_rows)

    # T3: Enrich with DexScreener pair detail (batch) + GT OHLCV (top candidates)
    cand_merged = enrich_with_momentum(cand_merged)

    # T4: Momentum scoring and filtering
    cand_merged = apply_momentum_filter(cand_merged, ohlcv_budget=20)

    cand_fieldnames = [
        "source", "pair_label", "pool_address_candidate",
        "base_token_address", "quote_token_address", "dex_id",
        "liquidity_usd", "volume_24h_usd", "txns_24h",
        "price_change_h1", "price_change_h6", "price_change_h24",
        "txns_1h_buys", "txns_1h_sells", "volume_1h_usd",
        "trend_5m_slope", "momentum_score", "momentum_gate",
        "url_or_source_ref", "preliminary_status",
    ]
    write_csv(OUT_CAND, cand_fieldnames, cand_merged)

    momentum_pass = [r for r in cand_merged if r.get("momentum_gate") == "PASS"]
    momentum_pass_count = len(momentum_pass)

    if momentum_pass_count < 3:
        stub = {
            "kind": "momentum_filtered_external_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "NO_MOMENTUM_CANDIDATES_PASS_FILTER",
            "no_training": True,
            "no_live": True,
            "momentum_pass_count": momentum_pass_count,
            "manifest_rows": [],
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            _render_report(
                "NO_MOMENTUM_CANDIDATES_PASS_FILTER",
                cand_merged, momentum_pass_count, [], [],
                ch_ok, pub_http_ok, bool(rpc_url), stub,
                http_req_used, rpc_identity_used, 0,
            ),
            encoding="utf-8",
        )
        return "NO_MOMENTUM_CANDIDATES_PASS_FILTER"

    # T5: Identity validation (on-chain factory check)
    id_headers = [
        "source", "pair_label", "pool_address_candidate",
        "eth_get_code_non_empty", "token0", "token1", "fee_raw_uint24",
        "factory_get_pool", "factory_matches", "slot0_sqrt_positive",
        "liquidity_nonzero", "identity_status", "failure_detail",
        "momentum_score", "momentum_gate",
    ]
    id_rows: list[dict[str, str]] = []
    identity_pass: list[str] = []

    if not rpc:
        write_csv(OUT_IDENTITY, id_headers, [])
    else:
        for cand in momentum_pass:
            if rpc_identity_used >= MAX_RPC_IDENTITY_TOTAL:
                break
            pool = cand["pool_address_candidate"].lower()
            vr = validate_pool_identity(c035, rpc, pool)
            vr["pair_label"] = cand.get("pair_label", "")
            vr["source"] = cand.get("source", "")
            vr["momentum_score"] = cand.get("momentum_score", "")
            vr["momentum_gate"] = cand.get("momentum_gate", "")
            id_rows.append(vr)
        identity_pass = [
            x["pool_address_candidate"]
            for x in id_rows
            if x.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS"
        ]
        write_csv(OUT_IDENTITY, id_headers, id_rows)

    if len(identity_pass) < 3:
        stub = {
            "kind": "momentum_filtered_external_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "NO_MOMENTUM_IDENTITY_PASS",
            "no_training": True,
            "no_live": True,
            "manifest_rows": [],
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            _render_report(
                "NO_MOMENTUM_IDENTITY_PASS",
                cand_merged, momentum_pass_count, id_rows, [],
                ch_ok, pub_http_ok, bool(rpc_url), stub,
                http_req_used, rpc_identity_used, 0,
            ),
            encoding="utf-8",
        )
        return "NO_MOMENTUM_IDENTITY_PASS"

    # T6: Activity + viability
    act_headers = [
        "pool_address_audit_key", "activity_status", "viability_status",
        "external_liquidity_usd", "external_volume_24h_usd", "external_txns_24h",
        "ch_distinct_recent_buckets", "current_quote_pass",
        "historical_pass_count", "historical_fail_count",
        "notes", "metadata_ptype", "rpc_liquidity_gate", "rpc_slot_gate",
        "momentum_score_at_selection",
    ]
    qb: dict[str, int] = {"spent": 0}
    act_rows: list[dict[str, str]] = []
    by_cand = {r["pool_address_candidate"].lower(): r for r in cand_merged}
    pools_rank: list[str] = []

    if not rpc:
        write_csv(OUT_ACTIVITY, act_headers, [{"pool_address_audit_key": "", "notes": "rpc_unavailable"}])
    else:
        for pool in sorted(
            identity_pass,
            key=lambda p: -(float(by_cand.get(p, {}).get("momentum_score") or 0)),
        ):
            if qb["spent"] >= MAX_QUOTE_PROBES_VIABILITY:
                break
            cr = by_cand.get(pool, {})
            ar, qb = viability_for_pool(c035, m33, mod, rpc, pool, cand_row=cr, quote_budget=qb)
            ar["momentum_score_at_selection"] = str(cr.get("momentum_score", ""))
            act_rows.append(ar)
            rpc_viability_used = qb.get("spent", 0)
            if ar.get("viability_status") == "PASS":
                pools_rank.append(pool)
        write_csv(OUT_ACTIVITY, act_headers, act_rows) if act_rows else write_csv(OUT_ACTIVITY, act_headers, [])

    if len(pools_rank) < 3:
        stub = {
            "kind": "momentum_filtered_external_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "READY_MOMENTUM_POOLS_BUT_NEED_BUCKET_BACKFILL",
            "no_training": True,
            "no_live": True,
            "viability_pass_pools_audit_only": pools_rank,
            "manifest_rows": [],
        }
        OUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            _render_report(
                "READY_MOMENTUM_POOLS_BUT_NEED_BUCKET_BACKFILL",
                cand_merged, momentum_pass_count, id_rows, act_rows,
                ch_ok, pub_http_ok, bool(rpc_url), stub,
                http_req_used, rpc_identity_used, qb.get("spent", 0),
            ),
            encoding="utf-8",
        )
        return "READY_MOMENTUM_POOLS_BUT_NEED_BUCKET_BACKFILL"

    # T7: Build manifest
    by_pool_buckets: dict[str, list[int]] = {}
    by_pool_bm_ts: dict[str, dict[int, int]] = {}
    for p in pools_rank[:6]:
        bms_ch = sorted(ch_recent_buckets_sorted(m33, p, limit_bm=260))
        use_ch = ch_ok and len(bms_ch) >= 40
        if use_ch:
            by_pool_buckets[p] = bms_ch
            by_pool_bm_ts[p] = ch_bucket_min_ts(m33, p, bms_ch[-160:])
        else:
            bms_syn, bm_map = rpc_synthetic_bucket_manifest_inputs(
                rpc, p, per_pool_cap=PER_POOL_CAP, lookback_days=7
            )
            by_pool_buckets[p] = bms_syn
            by_pool_bm_ts[p] = bm_map

    specs = build_manifest_specs(pools_rank, by_pool_buckets, by_pool_bm_ts)
    tpl = [(a, b, c_) for a, b, c_, _ in specs]
    ok_shape, why = external_manifest_shape_ok(tpl)

    if not specs or len(specs) < MIN_ROWS or not ok_shape:
        stub = {
            "kind": "momentum_filtered_external_scoped_smoke_manifest",
            "created_before_quote": True,
            "status": "TOO_SMALL_FOR_MOMENTUM_SMOKE",
            "no_training": True,
            "no_live": True,
            "balance_reason": why,
            "manifest_rows": [],
            "viability_pass_pools_audit_only": pools_rank,
            "constructed_row_count": len(specs),
        }
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2) + "\n")
        OUT_REPORT.write_text(
            _render_report(
                "TOO_SMALL_FOR_MOMENTUM_SMOKE",
                cand_merged, momentum_pass_count, id_rows, act_rows,
                ch_ok, pub_http_ok, bool(rpc_url), stub,
                http_req_used, rpc_identity_used, qb["spent"],
            ),
            encoding="utf-8",
        )
        return "TOO_SMALL_FOR_MOMENTUM_SMOKE"

    ctr = Counter(s[0] for s in specs)
    # Capture momentum scores at selection time
    momentum_scores_at_selection = {
        p: int(by_cand.get(p, {}).get("momentum_score") or 0) for p in ctr
    }
    obj_m = {
        "kind": "momentum_filtered_external_scoped_smoke_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "filter_version": "sprint035d_v1",
        "momentum_score_min_gate": MOMENTUM_SCORE_MIN,
        "selected_pool_count": len(ctr),
        "selected_rows_count": len(specs),
        "pool_address_audit_keys": sorted(ctr.keys()),
        "bucket_count_by_pool": dict(sorted(ctr.items())),
        "momentum_score_by_pool_at_selection": momentum_scores_at_selection,
        "identity_factory_viability_required": True,
        "no_post_label_pool_replacement": True,
        "manifest_rows": [
            {"pool_audit_only": p, "bucket_ms": bm, "bucket_sec": bs, "signal_ts_ms_audit_only": ts}
            for p, bm, bs, ts in specs
        ],
        "manifest_rows_preview_max15": [],
    }
    obj_m["manifest_rows_preview_max15"] = obj_m["manifest_rows"][:15]
    OUT_MANIFEST.write_text(json.dumps(obj_m, indent=2) + "\n")
    OUT_REPORT.write_text(
        _render_report(
            "MANIFEST_READY_MOMENTUM_FILTERED",
            cand_merged, momentum_pass_count, id_rows, act_rows,
            ch_ok, pub_http_ok, bool(rpc_url), obj_m,
            http_req_used, rpc_identity_used, qb["spent"],
        ),
        encoding="utf-8",
    )
    return "MANIFEST_READY_MOMENTUM_FILTERED"


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint035D momentum-filtered pool discovery")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        ap.print_help()
        return 1
    status = run_pipeline()
    caps_block = (
        rpc_identity_used > MAX_RPC_IDENTITY_TOTAL
        or http_req_used > MAX_EXTERNAL_HTTP
        or rpc_viability_used > MAX_QUOTE_PROBES_VIABILITY
    )
    if caps_block:
        print("BLOCKED_CAP_EXCEEDED", file=sys.stderr)
        return 3
    print(status)
    return 0 if status in (
        "MANIFEST_READY_MOMENTUM_FILTERED",
        "READY_MOMENTUM_POOLS_BUT_NEED_BUCKET_BACKFILL",
        "TOO_SMALL_FOR_MOMENTUM_SMOKE",
        "NO_MOMENTUM_IDENTITY_PASS",
        "NO_MOMENTUM_CANDIDATES_PASS_FILTER",
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
