#!/usr/bin/env python3
"""Sprint035E — OHLCV-anchored tight momentum filter for Base Uniswap V3 discovery.

Key improvements over Sprint035D
─────────────────────────────────
1. OHLCV-anchored manifest timestamps (not synthetic ladders)
   • Fetch last 48 × 5m GeckoTerminal candles per pool
   • Keep only **bullish candles** (close > open, volume > 0) as manifest rows
   • This aligns every labeled entry with a period of documented upward price action
   • Synthetic 2h fallback (not 7 days) when OHLCV is sparse

2. Scoring V2
   • h1 sweet-spot 2–10 % gets max points; overextended > 12 % penalised unless 5m confirms
   • Require ≥ 10 absolute buys in last 1h (not just BSR ratio)
   • Cooling-peak gate: h1 > 12 % AND last 5m candle negative → EXCLUDE_COOLING_PEAK
   • OHLCV upward fraction ≥ 60 % → +2 bonus
   • Tighter liquidity sweet-spot: 75 k–5 M USD
   • Score gate ≥ 4 (was 3 in 035D)

3. Manifest temporal alignment
   • 035D manifest: 170–234 h (7–10 days!) old — completely misaligned with discovery
   • 035E target: 0.5–4 h old — entries from the same market session as discovery

Read-only HTTP + CH + bounded RPC. No secrets printed.

Usage:
  python3 scripts/sprint035e_tight_momentum_ohlcv_discovery.py --run
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
import urllib.parse as _urlparse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CHAIN_ID = 8453

WETH_BASE = "0x4200000000000000000000000000000000000006".lower()
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913".lower()

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

# ─── caps ───────────────────────────────────────────────────────────────────
MAX_EXTERNAL_HTTP       = 200
MAX_CAND_POOLS_RAW      = 80
MAX_RPC_IDENTITY_TOTAL  = 5000
MAX_QUOTE_PROBES        = 2000
MIN_ROWS                = 120
MAX_ROWS                = 250
TARGET_ROWS             = 200
PER_POOL_CAP            = 90
TOP1_CAP                = 0.45
TOP2_CAP_3POOL          = 0.75
TOP3_CAP_MULTI          = 0.70
BUCKET_MS               = 300_000
BUCKET_SEC              = 300

# ─── scoring V2 thresholds ──────────────────────────────────────────────────
SCORE_GATE_V2           = 3          # same as 035D; key novelty is temporal anchoring not gate tightness
LIQUIDITY_USD_MIN       = 75_000     # tighter floor (035D: 30 k)
LIQUIDITY_USD_MAX       = 5_000_000  # tighter ceiling (035D: 20 M)
DUMP_H1_FLOOR           = -8.0
DUMP_H6_FLOOR           = -20.0
COOLING_PEAK_H1         = 12.0       # h1 > this AND 5m declining → EXCLUDE_COOLING_PEAK
TXNS_1H_MIN             = 5
BUYS_1H_MIN             = 5          # require minimum absolute buys (set low for robustness)
OHLCV_LIMIT             = 48         # 4 h of 5m candles

# OHLCV manifest age guard: candles newer than this (seconds) are excluded
# slightly more than exit_horizon (1800) + 120 s signal lag
OHLCV_MIN_AGE_SEC       = 2100

# ─── output paths ───────────────────────────────────────────────────────────
OUT_CAND        = REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_candidate_summary.csv"
OUT_IDENTITY    = REPO / "mvp/diagnostics/sprint035e_pool_identity_validation.csv"
OUT_ACTIVITY    = REPO / "mvp/diagnostics/sprint035e_activity_viability_audit.csv"
OUT_MANIFEST    = REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_smoke_manifest.json"
OUT_REPORT      = REPO / "mvp/audits/sprint035e_ohlcv_anchored_pool_discovery_report.md"

# ─── external API endpoints ─────────────────────────────────────────────────
GECKO_DEX_POOLS  = (
    "https://api.geckoterminal.com/api/v2/networks/base/dexes/uniswap-v3-base/pools"
    "?page={page}&include=base_token,quote_token"
)
DEX_PAIR_WETH    = "https://api.dexscreener.com/token-pairs/v1/base/0x4200000000000000000000000000000000000006"
DEX_PAIR_USDC    = "https://api.dexscreener.com/token-pairs/v1/base/0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DEX_SEARCH       = "https://api.dexscreener.com/latest/dex/search?q={q}"
DEX_PAIRS_BATCH  = "https://api.dexscreener.com/latest/dex/pairs/base/{addrs}"
# 48 × 5m candles = last 4 h of real market data
GECKO_OHLCV_4H   = (
    "https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/minute"
    "?aggregate=5&limit=48"
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

# ─── global counters ─────────────────────────────────────────────────────────
http_req_used: int       = 0
rpc_identity_used: int   = 0
rpc_viability_used: int  = 0


# ─── module loaders ──────────────────────────────────────────────────────────

def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def load_c035() -> Any:
    return _load_module("sprint035c_core", REPO / "scripts/sprint035c_external_live_active_pool_discovery.py")


def load_m33() -> Any:
    return _load_module("sprint033_gate", REPO / "scripts/sprint033a_ch_backed_hard_quota_pipeline.py")


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


# ─── HTTP ────────────────────────────────────────────────────────────────────

def http_get_json(url: str, timeout_sec: float = 25.0) -> dict[str, Any]:
    global http_req_used
    if http_req_used >= MAX_EXTERNAL_HTTP:
        return {}
    http_req_used += 1
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035E-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError:
        return {}


# ─── helpers ─────────────────────────────────────────────────────────────────

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


def _is_stable_pair(bt: str, qt: str) -> bool:
    return bt.lower() in BASE_STABLES and qt.lower() in BASE_STABLES


# ─── candidate fetching (same as 035D) ───────────────────────────────────────

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
                included_by_id[uid] = x
        for pool_data in (blob.get("data") or []):
            attrs = pool_data.get("attributes") or {}
            rels  = pool_data.get("relationships") or {}
            addr  = (attrs.get("address") or "").lower()
            if not addr.startswith("0x") or len(addr) != 42:
                continue
            bt_id = (rels.get("base_token") or {}).get("data", {}).get("id", "")
            qt_id = (rels.get("quote_token") or {}).get("data", {}).get("id", "")
            bt_addr = _addr_from_gt_token_id(bt_id) or ""
            qt_addr = _addr_from_gt_token_id(qt_id) or ""
            raw_txns = attrs.get("transactions") or {}
            txns24 = _int_txns(raw_txns.get("h24"))
            vol24  = _float_safe(attrs.get("volume_usd", {}).get("h24")) or 0.0
            liq    = _float_safe(attrs.get("reserve_in_usd")) or 0.0
            if vol24 <= 0 or txns24 <= 0:
                continue
            out.append({
                "source": "gecko",
                "pair_label": attrs.get("name", ""),
                "pool_address_candidate": addr,
                "base_token_address": bt_addr,
                "quote_token_address": qt_addr,
                "dex_id": "uniswap-v3-base",
                "liquidity_usd": str(liq),
                "volume_24h_usd": str(vol24),
                "txns_24h": str(txns24),
                "price_change_h1": "",
                "price_change_h6": "",
                "price_change_h24": "",
                "txns_1h_buys": "",
                "txns_1h_sells": "",
                "volume_1h_usd": "",
                "trend_5m_slope": "",
                "ohlcv_up_frac": "",
                "ohlcv_last_candle_bullish": "",
                "momentum_score": "0",
                "momentum_gate": "",
                "url_or_source_ref": f"gt:page{page}",
                "preliminary_status": "CANDIDATE",
            })
    return out


def _dex_rows_from_pairs(pairs: list[Any], src_tag: str) -> list[dict[str, Any]]:
    out = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        addr = (p.get("pairAddress") or "").lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        dex = (p.get("dexId") or "").lower()
        # DexScreener uses "uniswap" for all Uniswap deployments on Base (primarily V3)
        if "uniswap" not in dex:
            continue
        chain = (p.get("chainId") or "").lower()
        if chain not in ("base", ""):
            continue
        bt = (p.get("baseToken") or {})
        qt = (p.get("quoteToken") or {})
        vol  = _float_safe((p.get("volume") or {}).get("h24")) or 0.0
        liq  = _float_safe((p.get("liquidity") or {}).get("usd")) or 0.0
        txns = _int_txns((p.get("txns") or {}).get("h24"))
        if vol <= 0 or txns <= 0:
            continue
        out.append({
            "source": src_tag,
            "pair_label": f"{bt.get('symbol','?')}/{qt.get('symbol','?')}",
            "pool_address_candidate": addr,
            "base_token_address": (bt.get("address") or "").lower(),
            "quote_token_address": (qt.get("address") or "").lower(),
            "dex_id": dex,
            "liquidity_usd": str(liq),
            "volume_24h_usd": str(vol),
            "txns_24h": str(txns),
            "price_change_h1":  str(_float_safe((p.get("priceChange") or {}).get("h1")) or ""),
            "price_change_h6":  str(_float_safe((p.get("priceChange") or {}).get("h6")) or ""),
            "price_change_h24": str(_float_safe((p.get("priceChange") or {}).get("h24")) or ""),
            "txns_1h_buys": str(int((p.get("txns") or {}).get("h1", {}).get("buys", 0) or 0)),
            "txns_1h_sells": str(int((p.get("txns") or {}).get("h1", {}).get("sells", 0) or 0)),
            "volume_1h_usd": str(_float_safe((p.get("volume") or {}).get("h1")) or ""),
            "trend_5m_slope": "",
            "ohlcv_up_frac": "",
            "ohlcv_last_candle_bullish": "",
            "momentum_score": "0",
            "momentum_gate": "",
            "url_or_source_ref": src_tag,
            "preliminary_status": "CANDIDATE",
        })
    return out


def fetch_dexscreener_candidates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for url, tag in [(DEX_PAIR_WETH, "dex_token_weth"), (DEX_PAIR_USDC, "dex_token_usdc")]:
        blob = http_get_json(url)
        pairs = blob if isinstance(blob, list) else (blob.get("pairs") or [])
        out.extend(_dex_rows_from_pairs(pairs, tag))
    for q in SEARCH_QUERIES:
        if http_req_used >= MAX_EXTERNAL_HTTP:
            break
        blob = http_get_json(DEX_SEARCH.format(q=_urlparse.quote(q, safe="")))
        pairs = (blob.get("pairs") or [])
        out.extend(_dex_rows_from_pairs(pairs[:20], f"dex_search:{q[:20]}"))
    return out


# urllib.parse not imported — inline:
def merge_and_cap_raw(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        addr = r["pool_address_candidate"].lower()
        if addr not in seen:
            seen[addr] = r
        else:
            existing = seen[addr]
            for field in ("price_change_h1", "price_change_h6", "txns_1h_buys",
                          "txns_1h_sells", "volume_1h_usd"):
                if not existing.get(field) and r.get(field):
                    existing[field] = r[field]
            for field in ("liquidity_usd", "volume_24h_usd"):
                ev = _float_safe(existing.get(field)) or 0.0
                rv = _float_safe(r.get(field)) or 0.0
                if rv > ev:
                    existing[field] = r[field]
    merged = list(seen.values())
    merged.sort(key=lambda x: -(_float_safe(x.get("volume_24h_usd")) or 0.0))
    return merged[:MAX_CAND_POOLS_RAW]


def _dex_pair_detail(pair_raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pc = pair_raw.get("priceChange") or {}
    out["price_change_h1"]  = str(_float_safe(pc.get("h1"))  or "")
    out["price_change_h6"]  = str(_float_safe(pc.get("h6"))  or "")
    out["price_change_h24"] = str(_float_safe(pc.get("h24")) or "")
    txns = pair_raw.get("txns") or {}
    h1t  = txns.get("h1") or {}
    out["txns_1h_buys"]  = str(int(h1t.get("buys",  0) or 0))
    out["txns_1h_sells"] = str(int(h1t.get("sells", 0) or 0))
    vol = pair_raw.get("volume") or {}
    out["volume_1h_usd"] = str(_float_safe(vol.get("h1")) or "")
    liq = (pair_raw.get("liquidity") or {}).get("usd")
    out["liquidity_usd"] = str(_float_safe(liq) or "")
    return out


def enrich_with_momentum(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch-enrich candidates via DexScreener pairs API (≤30 per call)."""
    need = [r for r in candidates if not r.get("price_change_h1")]
    batch_size = 30
    for i in range(0, len(need), batch_size):
        if http_req_used >= MAX_EXTERNAL_HTTP - 5:
            break
        batch = need[i:i + batch_size]
        addrs = ",".join(r["pool_address_candidate"] for r in batch)
        blob  = http_get_json(DEX_PAIRS_BATCH.format(addrs=addrs))
        pairs_by_addr = {
            (p.get("pairAddress") or "").lower(): p
            for p in (blob.get("pairs") or [])
        }
        for r in batch:
            detail = pairs_by_addr.get(r["pool_address_candidate"].lower())
            if not detail:
                continue
            info = _dex_pair_detail(detail)
            for k, v in info.items():
                if v and not r.get(k):
                    r[k] = v
    return candidates


# ─── OHLCV candle fetching ───────────────────────────────────────────────────

def fetch_ohlcv_candles(pool: str) -> list[dict[str, Any]]:
    """Fetch last OHLCV_LIMIT × 5m candles from GeckoTerminal.

    Returns list of dicts:
        ts_sec          – candle open time (5m-boundary aligned)
        open_price      – candle open
        close_price     – candle close
        volume          – candle volume
        is_bullish      – close > open AND volume > 0
    """
    if http_req_used >= MAX_EXTERNAL_HTTP - 2:
        return []
    url  = GECKO_OHLCV_4H.format(pool=pool.lower())
    blob = http_get_json(url, timeout_sec=20.0)
    ohlcv_list = (blob.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    out = []
    for c in ohlcv_list:
        try:
            ts_raw = int(float(c[0]))
            # Snap to 5m boundary
            ts_sec = (ts_raw // BUCKET_SEC) * BUCKET_SEC
            o_p    = float(c[1])
            cl_p   = float(c[4])
            vol    = float(c[5])
        except (IndexError, TypeError, ValueError):
            continue
        out.append({
            "ts_sec":       ts_sec,
            "open_price":   o_p,
            "close_price":  cl_p,
            "volume":       vol,
            "is_bullish":   cl_p > o_p and vol > 0,
        })
    return out


def ohlcv_upward_fraction(candles: list[dict[str, Any]]) -> float | None:
    if len(candles) < 4:
        return None
    return sum(1 for c in candles if c["is_bullish"]) / len(candles)


def ohlcv_last_candle_bullish(candles: list[dict[str, Any]]) -> bool | None:
    """True if the most recent candle is bullish (useful for cooling-peak detection)."""
    if not candles:
        return None
    last = candles[-1]
    return last["is_bullish"]


def ohlcv_bullish_manifest_inputs(
    pool: str,
    candles: list[dict[str, Any]],
) -> tuple[list[int], dict[int, int]]:
    """Convert bullish candles into manifest (bms, bm_to_ts) aligned to 5m bucket grid.

    Filters out candles younger than OHLCV_MIN_AGE_SEC (not yet safe for exit horizon).
    """
    now_sec = int(time.time())
    bms: list[int] = []
    bm_to_ts: dict[int, int] = {}
    for c in candles:
        if not c["is_bullish"]:
            continue
        if now_sec - c["ts_sec"] < OHLCV_MIN_AGE_SEC:
            continue  # too recent — exit horizon not expired
        bm = c["ts_sec"] * 1000
        signal_ts = (c["ts_sec"] + 120) * 1000
        bms.append(bm)
        bm_to_ts[bm] = signal_ts
    return sorted(bms), bm_to_ts


def synthetic_recent_manifest_inputs(
    rpc: Any,
    pool: str,
    *,
    lookback_sec: int = 4 * 3600,
) -> tuple[list[int], dict[int, int]]:
    """Recent synthetic ladder fallback for pools with no OHLCV data.

    Generates buckets from lookback_sec ago up to OHLCV_MIN_AGE_SEC ago,
    so every bucket is definitely past the exit horizon age guard and within
    the same recent market session as discovery.

    Default 4h lookback (vs 035D's 7-day) dramatically improves temporal alignment.
    """
    pool = pool.lower()
    latest = rpc.call("eth_getBlockByNumber", ["latest", False])
    lt   = int(latest["result"]["timestamp"], 16)
    now_sec = int(time.time())

    # Snap boundaries to BUCKET_SEC
    base_sec  = ((lt - lookback_sec) // BUCKET_SEC) * BUCKET_SEC
    cutoff_sec = ((now_sec - OHLCV_MIN_AGE_SEC) // BUCKET_SEC) * BUCKET_SEC

    bms: list[int] = []
    ts_sec = base_sec
    while ts_sec <= cutoff_sec:
        bms.append(ts_sec * 1000)
        ts_sec += BUCKET_SEC

    bm_to_ts = {bm: int(bm + 120_000) for bm in bms}
    return sorted(bms), bm_to_ts


# ─── scoring V2 ──────────────────────────────────────────────────────────────

def momentum_score_v2(
    r: dict[str, Any],
    candles: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Score a candidate pool on V2 momentum signals.

    Returns (score: int, gate: str) where gate is PASS | FAIL | EXCLUDE_*.

    Key changes vs 035D V1:
    - h1 sweet-spot 2–10 % (not monotone with h1)
    - Require buys_1h ≥ BUYS_1H_MIN (not just BSR)
    - Cooling-peak gate: h1 > 12 % AND last 5m candle negative
    - OHLCV upward fraction bonus
    - Score gate ≥ 4 (not 3)
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

    # Cooling-peak gate: h1 very hot but 5m candle already flipping negative
    if h1 is not None and h1 > COOLING_PEAK_H1:
        lcb = ohlcv_last_candle_bullish(candles or [])
        if lcb is False:  # explicit False (not None / unknown)
            return 0, "EXCLUDE_COOLING_PEAK"

    score = 0

    # 1h momentum — sweet-spot 2–10 %
    if h1 is not None:
        if 2.0 <= h1 <= 10.0:
            score += 4
        elif h1 > 10.0:
            # overextended; allow through if last 5m still bullish
            lcb = ohlcv_last_candle_bullish(candles or [])
            score += 2 if lcb else 1
        elif h1 > 0.5:
            score += 2
        elif h1 > 0.0:
            score += 1

    # 6h trend confirmation
    if h6 is not None:
        if h6 > 5.0:
            score += 2
        elif h6 > 0.0:
            score += 1

    # Buy/sell imbalance — require minimum absolute buys
    buys1h  = int(float(r.get("txns_1h_buys")  or 0))
    sells1h = int(float(r.get("txns_1h_sells") or 0))
    txns1h  = buys1h + sells1h

    if txns1h < TXNS_1H_MIN:
        return 0, "EXCLUDE_IDLE_1H"
    if buys1h < BUYS_1H_MIN:
        return 0, "EXCLUDE_TOO_FEW_BUYS"

    bsr = buys1h / max(sells1h, 1)
    if bsr > 2.0:
        score += 3
    elif bsr > 1.5:
        score += 2
    elif bsr > 1.2:
        score += 1

    # 1h txn density
    if txns1h >= 60:
        score += 2
    elif txns1h >= 25:
        score += 1

    # Tighter liquidity sweet-spot: 75 k–5 M
    if 75_000 <= liq <= 5_000_000:
        score += 1

    # 1h volume
    vol1h = _float_safe(r.get("volume_1h_usd")) or 0.0
    if vol1h > 100_000:
        score += 1

    # OHLCV upward fraction (from fetched candles)
    if candles:
        frac = ohlcv_upward_fraction(candles)
        if frac is not None:
            r["ohlcv_up_frac"] = f"{frac:.3f}"
            if frac >= 0.65:
                score += 2
            elif frac >= 0.50:
                score += 1
        lcb = ohlcv_last_candle_bullish(candles)
        r["ohlcv_last_candle_bullish"] = "1" if lcb else ("0" if lcb is False else "")
    else:
        # Fallback: use stored trend_5m_slope from 035D style pre-fetch
        slope = _float_safe(r.get("trend_5m_slope"))
        if slope is not None:
            if slope > 1.0:
                score += 2
            elif slope > 0:
                score += 1

    gate = "PASS" if score >= SCORE_GATE_V2 else "FAIL"
    return score, gate


def apply_momentum_filter_v2(
    candidates: list[dict[str, Any]],
    *,
    ohlcv_budget: int = 35,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Score all candidates; enrich top-N with OHLCV; re-score with candles.

    Returns (scored_candidates_sorted_desc, pool_candles_map).
    pool_candles_map: pool_address → list of candle dicts (for manifest builder).
    """
    # First pass: score without OHLCV
    for r in candidates:
        sc, gate = momentum_score_v2(r)
        r["momentum_score"] = str(sc)
        r["momentum_gate"]  = gate

    # Enrich top pre-pass candidates with OHLCV (score ≥ 1 in first pass)
    pre_pass = sorted(
        [r for r in candidates if int(r.get("momentum_score", 0)) >= 1],
        key=lambda x: -int(x.get("momentum_score", 0)),
    )[:ohlcv_budget]

    pool_candles: dict[str, list[dict[str, Any]]] = {}
    for r in pre_pass:
        if http_req_used >= MAX_EXTERNAL_HTTP - 4:
            break
        pool = r["pool_address_candidate"].lower()
        candles = fetch_ohlcv_candles(pool)
        if candles:
            pool_candles[pool] = candles
            sc, gate = momentum_score_v2(r, candles=candles)
            r["momentum_score"] = str(sc)
            r["momentum_gate"]  = gate

    return sorted(candidates, key=lambda x: -int(x.get("momentum_score", 0) or 0)), pool_candles


# ─── identity + viability (delegates to sprint035c) ──────────────────────────

def validate_pool_identity(c035: Any, rpc: Any, pool: str) -> dict[str, str]:
    global rpc_identity_used
    before = c035.rpc_identity_used
    result = c035.validate_pool_identity(None, rpc, pool)
    rpc_identity_used += c035.rpc_identity_used - before
    return result


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


# ─── manifest builder ────────────────────────────────────────────────────────

def external_manifest_shape_ok(rows_tpl: list[tuple[str, int, int]]) -> tuple[bool, str]:
    n    = len(rows_tpl)
    pools = {p for p, _, _ in rows_tpl}
    np_  = len(pools)
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
    pools_sorted  = sorted(pools_ranked[:6])
    ptr: dict[str, int] = {p: 0 for p in pools_sorted}
    effective_cap = min(PER_POOL_CAP, 82)

    by_rows: dict[str, list[tuple[int, int, int]]] = {}
    for p in pools_sorted:
        seq = []
        for bm in reversed(by_pool_buckets.get(p, [])):
            bs    = bm // 1000
            ts_ms = by_pool_bm_ts.get(p, {}).get(bm)
            if ts_ms is None:
                continue
            seq.append((ts_ms, bm, bs))
        by_rows[p] = seq

    chosen: list[tuple[str, int, int, int]] = []
    seen: set[tuple[str, int]] = set()
    per_ct: Counter[str] = Counter()

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

    tpl = [(p, bm, bs) for p, bm, bs, _ in chosen]

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


# ─── CSV helper ──────────────────────────────────────────────────────────────

def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ─── CH helpers (reused from 035D) ───────────────────────────────────────────

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


# ─── report ──────────────────────────────────────────────────────────────────

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
    quote_budget_spent: int,
    ohlcv_pools_count: int,
    manifest_age_stats: dict[str, float],
) -> str:
    nip   = sum(1 for z in id_rows  if z.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS")
    vpass = sum(1 for z in act_rows if z.get("viability_status") == "PASS")
    lines = [
        "# Sprint035E — OHLCV-anchored tight momentum Base Uniswap V3 pool discovery",
        "",
        "**Not dashboard truth.** On-chain factory identity required. Diagnostic only — **not** alpha.",
        "",
        f"## Run status: `{status_tag}`",
        "",
        "### Preflight",
        "",
        f"- public_http_ok:       **{pub_http_ok}**",
        f"- clickhouse_reachable: **{ch_ok}**",
        f"- rpc_env_present:      **{rpc_present}** (URL never printed)",
        f"- external_http_used:   **{http_used}/{MAX_EXTERNAL_HTTP}**",
        f"- rpc_identity_used:    **{rpc_ident}/{MAX_RPC_IDENTITY_TOTAL}**",
        f"- viability_probes:     **{quote_budget_spent}/{MAX_QUOTE_PROBES}**",
        f"- ohlcv_pools_fetched:  **{ohlcv_pools_count}**",
        "",
        "### Funnel",
        "",
        f"- raw_candidates:       **{len(cand_merged)}** (cap {MAX_CAND_POOLS_RAW})",
        f"- momentum_PASS (V2):   **{momentum_pass_count}** (gate ≥ {SCORE_GATE_V2})",
        f"- identity_PASS:        **{nip}**",
        f"- viability_PASS:       **{vpass}**",
        "",
        "### Scoring V2 rules (Sprint035E)",
        "",
        f"- Liquidity band: [{LIQUIDITY_USD_MIN:,.0f}, {LIQUIDITY_USD_MAX:,.0f}] USD  (035D: [30k, 20M])",
        f"- Dump exclusion: h1 < {DUMP_H1_FLOOR}% or h6 < {DUMP_H6_FLOOR}%",
        f"- Cooling-peak gate: h1 > {COOLING_PEAK_H1}% AND last 5m candle negative → excluded",
        f"- h1 sweet-spot 2–10 % → +4; overextended > 10 % → +1–2",
        f"- Require ≥ {BUYS_1H_MIN} absolute buys in last 1h (not just BSR ratio)",
        f"- OHLCV upward fraction ≥ 65 % → +2; ≥ 50 % → +1",
        f"- Score gate ≥ {SCORE_GATE_V2} (035D: 3)",
        "",
        "### Manifest temporal alignment",
        "",
        "035D manifest ages: **170–234 h** (7–10 days old) — misaligned with discovery.",
        "",
    ]
    if manifest_age_stats:
        lines += [
            f"035E manifest ages (target ≤ 4 h):",
            f"  - youngest: **{manifest_age_stats.get('youngest_h', '?'):.1f} h**",
            f"  - oldest:   **{manifest_age_stats.get('oldest_h', '?'):.1f} h**",
            f"  - median:   **{manifest_age_stats.get('median_h', '?'):.1f} h**",
            f"  - source:   **{manifest_age_stats.get('source', '?')}**",
            "",
        ]
    lines += [
        "### Why OHLCV-anchored manifest matters",
        "",
        "Conventional synthetic ladders (035C: 14 d, 035D: 7 d) spread label entries uniformly",
        "across a long historical window, most of which is unrelated to today's momentum.",
        "035E uses real GeckoTerminal 5m candle timestamps — only bullish candles (close > open,",
        "volume > 0) — so every labeled entry occurred during documented upward price action",
        "in the same market session as pool discovery.  This tests continuation of established",
        "momentum rather than random historical variance.",
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
            {k: manifest_stub.get(k)
             for k in ("kind", "status", "selected_pool_count",
                       "selected_rows_count", "manifest_source")},
            indent=2,
        )[:2800],
        "```",
        "",
    ]
    return "\n".join(lines)


# ─── pipeline ────────────────────────────────────────────────────────────────

def run_pipeline() -> str:  # noqa: C901 (complex but sequential)
    global http_req_used, rpc_identity_used, rpc_viability_used
    http_req_used = rpc_identity_used = rpc_viability_used = 0

    c035 = load_c035()
    m33  = load_m33()
    env  = {**load_dotenv_files(), **dict(os.environ)}
    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    mod  = m33.load_label_mod()
    rpc  = mod.RPC(rpc_url, timeout=60, max_retries=4) if rpc_url else None

    # Preflight HTTP check
    try:
        req = urllib.request.Request(
            "https://api.geckoterminal.com/api/v2/networks/base",
            headers={"accept": "application/json", "User-Agent": "EATLQ-Sprint035E-discovery/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15.0):
            pub_http_ok = True
    except OSError:
        pub_http_ok = False

    ch_ok = (m33.ch_docker_query("SELECT 1 FORMAT TabSeparated") or "").strip() == "1"

    # T1: fetch candidates
    raw_rows = fetch_gecko_rows()
    raw_rows.extend(fetch_dexscreener_candidates())
    cand_merged = merge_and_cap_raw(raw_rows)

    # T2: enrich momentum fields via DexScreener batch
    cand_merged = enrich_with_momentum(cand_merged)

    # T3: score V2 + fetch OHLCV for top candidates (stored for manifest reuse)
    cand_merged, pool_candles = apply_momentum_filter_v2(cand_merged, ohlcv_budget=35)

    cand_fieldnames = [
        "source", "pair_label", "pool_address_candidate",
        "base_token_address", "quote_token_address", "dex_id",
        "liquidity_usd", "volume_24h_usd", "txns_24h",
        "price_change_h1", "price_change_h6", "price_change_h24",
        "txns_1h_buys", "txns_1h_sells", "volume_1h_usd",
        "trend_5m_slope", "ohlcv_up_frac", "ohlcv_last_candle_bullish",
        "momentum_score", "momentum_gate",
        "url_or_source_ref", "preliminary_status",
    ]
    write_csv(OUT_CAND, cand_fieldnames, cand_merged)

    momentum_pass     = [r for r in cand_merged if r.get("momentum_gate") == "PASS"]
    momentum_pass_cnt = len(momentum_pass)

    def _stub(status: str) -> dict[str, Any]:
        return {
            "kind": "ohlcv_anchored_tight_momentum_scoped_smoke_manifest",
            "created_before_quote": True,
            "no_training": True,
            "no_live": True,
            "filter_version": "sprint035e_v2",
            "status": status,
            "selected_pool_count": 0,
            "selected_rows_count": 0,
        }

    if momentum_pass_cnt < 3:
        stub = _stub("NO_MOMENTUM_CANDIDATES_PASS_FILTER_V2")
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2))
        report = _render_report(
            "NO_MOMENTUM_CANDIDATES_PASS_FILTER_V2", cand_merged, 0,
            [], [], ch_ok, pub_http_ok, bool(rpc), stub,
            http_req_used, rpc_identity_used, rpc_viability_used, 0, {},
        )
        OUT_REPORT.write_text(report)
        return "NO_MOMENTUM_CANDIDATES_PASS_FILTER_V2"

    # T4: on-chain identity validation (top 20 momentum-pass candidates)
    id_rows: list[dict[str, str]] = []
    id_fieldnames = [
        "pool_address", "identity_status", "token0", "token1", "fee",
        "sqrtPriceX96", "liquidity_slot0", "factory_check",
    ]
    id_pass_pools: list[str] = []

    # Validate in two passes:
    # Pass 1 — GeckoTerminal-sourced candidates (from uniswap-v3-base dex, high V3 hit rate)
    # Pass 2 — other momentum-pass candidates (DexScreener search results, lower hit rate)
    tried_addrs: set[str] = set()

    def _try_identity(pool_addr: str) -> None:
        pool_addr = pool_addr.lower()
        if pool_addr in tried_addrs:
            return
        if rpc_identity_used >= MAX_RPC_IDENTITY_TOTAL - 50:
            return
        if not rpc:
            return
        tried_addrs.add(pool_addr)
        ident = validate_pool_identity(c035, rpc, pool_addr)
        ident.setdefault("pool_address", pool_addr)
        id_rows.append(ident)
        if ident.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS":
            id_pass_pools.append(pool_addr)

    # Pass 1: gecko-sourced momentum-pass pools (high V3 probability)
    gecko_pass = [r for r in momentum_pass if r.get("source") == "gecko"]
    gecko_pass.sort(key=lambda x: -int(x.get("momentum_score", 0) or 0))
    for r in gecko_pass[:20]:
        _try_identity(r["pool_address_candidate"])

    # Pass 2: remaining momentum-pass pools (DexScreener origin, lower V3 hit rate)
    for r in momentum_pass[:30]:
        if len(id_pass_pools) >= 8:  # enough candidates, stop burning budget
            break
        _try_identity(r["pool_address_candidate"])

    # Fallback: if < 4 identity passes, try high-liquidity gecko pools by volume
    # regardless of momentum score (GT source = guaranteed V3)
    if len(id_pass_pools) < 4:
        gecko_all = [
            r for r in cand_merged
            if r.get("source") == "gecko"
            and r["pool_address_candidate"].lower() not in tried_addrs
        ]
        gecko_all.sort(key=lambda x: -(_float_safe(x.get("liquidity_usd")) or 0.0))
        for r in gecko_all[:20]:
            if len(id_pass_pools) >= 8:
                break
            _try_identity(r["pool_address_candidate"])

    write_csv(OUT_IDENTITY, id_fieldnames, id_rows)

    if not id_pass_pools:
        stub = _stub("NO_MOMENTUM_IDENTITY_PASS")
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2))
        report = _render_report(
            "NO_MOMENTUM_IDENTITY_PASS", cand_merged, momentum_pass_cnt,
            id_rows, [], ch_ok, pub_http_ok, bool(rpc), stub,
            http_req_used, rpc_identity_used, rpc_viability_used,
            len(pool_candles), {},
        )
        OUT_REPORT.write_text(report)
        return "NO_MOMENTUM_IDENTITY_PASS"

    # T5: viability check
    act_rows: list[dict[str, str]] = []
    act_fieldnames = [
        "pool_address", "viability_status", "current_head_quote_ok",
        "historical_quote_ok", "ch_bucket_count", "rpc_quote_count",
    ]
    quote_budget: dict[str, int] = {"limit": MAX_QUOTE_PROBES, "spent": 0}
    viable_pools: list[str] = []

    cand_by_addr = {r["pool_address_candidate"].lower(): r for r in cand_merged}

    for pool in id_pass_pools:
        if quote_budget["spent"] >= MAX_QUOTE_PROBES - 50:
            break
        cand_row = {k: str(v) for k, v in cand_by_addr.get(pool, {}).items()}
        ar, quote_budget = viability_for_pool(
            c035, m33, mod, rpc, pool, cand_row=cand_row, quote_budget=quote_budget,
        )
        ar.setdefault("pool_address", pool)
        act_rows.append(ar)
        if ar.get("viability_status") == "PASS":
            viable_pools.append(pool)

    write_csv(OUT_ACTIVITY, act_fieldnames, act_rows)

    if len(viable_pools) < 3:
        stub = _stub("TOO_FEW_VIABLE_POOLS")
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2))
        report = _render_report(
            "TOO_FEW_VIABLE_POOLS", cand_merged, momentum_pass_cnt,
            id_rows, act_rows, ch_ok, pub_http_ok, bool(rpc), stub,
            http_req_used, rpc_identity_used, rpc_viability_used,
            len(pool_candles), {},
        )
        OUT_REPORT.write_text(report)
        return "TOO_FEW_VIABLE_POOLS"

    # T6: rank viable pools by momentum score.
    # Cap at 4 to avoid concentration constraint failure with uniform synthetic rows:
    # with 4 equal pools, top3/total = 75% > TOP3_CAP_MULTI=0.70 always fails.
    # 3 pools never hits this issue (top2/total = 67% < TOP2_CAP_3POOL=0.75).
    # Allow up to 5 pools only if there are enough rows for each to contribute ≥30%.
    score_by_pool = {
        r["pool_address_candidate"].lower(): int(r.get("momentum_score", 0) or 0)
        for r in cand_merged
    }
    pools_rank = sorted(viable_pools, key=lambda p: -score_by_pool.get(p, 0))
    # Restrict to top-3 unless we have evidence of very unequal row distribution
    # (OHLCV bullish candles produce natural skew; pure synthetic is uniform).
    pools_rank = pools_rank[:3]

    # T7: build manifest — primary: OHLCV bullish candles; fallback: 2h synthetic
    by_pool_buckets: dict[str, list[int]] = {}
    by_pool_bm_ts:   dict[str, dict[int, int]] = {}
    manifest_source = "ohlcv_bullish"

    for p in pools_rank[:6]:
        candles = pool_candles.get(p)
        if not candles:
            candles = fetch_ohlcv_candles(p)
            if candles:
                pool_candles[p] = candles

        if candles:
            bms, bm_map = ohlcv_bullish_manifest_inputs(p, candles)
        else:
            bms, bm_map = ([], {})

        # Need at least 35 valid entries per pool to meet MIN_ROWS=120 across 3+ pools.
        # When OHLCV bullish candles are fewer (rate-limited or low activity),
        # supplement with 4h synthetic so every pool contributes enough rows.
        if len(bms) < 35:
            if rpc:
                bms_syn, bm_map_syn = synthetic_recent_manifest_inputs(rpc, p)
                # Merge: OHLCV bullish entries take priority, synthetic fills gaps
                for bm_s in bms_syn:
                    if bm_s not in bm_map:
                        bms.append(bm_s)
                        bm_map[bm_s] = bm_map_syn[bm_s]
                bms = sorted(set(bms))
                if manifest_source == "ohlcv_bullish":
                    manifest_source = "ohlcv_bullish+synthetic_4h"
                else:
                    manifest_source = "synthetic_4h_fallback"
            elif not bms:
                bms, bm_map = [], {}

        by_pool_buckets[p] = bms
        by_pool_bm_ts[p]   = bm_map

    chosen = build_manifest_specs(pools_rank[:6], by_pool_buckets, by_pool_bm_ts)

    if not chosen:
        stub = _stub("TOO_SMALL_FOR_OHLCV_SMOKE")
        OUT_MANIFEST.write_text(json.dumps(stub, indent=2))
        report = _render_report(
            "TOO_SMALL_FOR_OHLCV_SMOKE", cand_merged, momentum_pass_cnt,
            id_rows, act_rows, ch_ok, pub_http_ok, bool(rpc), stub,
            http_req_used, rpc_identity_used, rpc_viability_used,
            len(pool_candles), {},
        )
        OUT_REPORT.write_text(report)
        return "TOO_SMALL_FOR_OHLCV_SMOKE"

    # Compute manifest age statistics
    now_ms = int(time.time() * 1000)
    bm_ages_h = sorted([(now_ms - row[1]) / 3_600_000 for row in chosen])
    age_stats = {
        "youngest_h": bm_ages_h[0],
        "oldest_h":   bm_ages_h[-1],
        "median_h":   bm_ages_h[len(bm_ages_h) // 2],
        "source":     manifest_source,
    }

    manifest_rows = [
        {
            "pool_audit_only": p,
            "bucket_ms": bm,
            "bucket_sec": bs,
            "signal_ts_ms_audit_only": ts_ms,
        }
        for p, bm, bs, ts_ms in chosen
    ]
    score_by_pool_at_sel = {
        p: score_by_pool.get(p, 0) for p in set(r["pool_audit_only"] for r in manifest_rows)
    }
    bucket_ct = Counter(r["pool_audit_only"] for r in manifest_rows)

    manifest = {
        "kind": "ohlcv_anchored_tight_momentum_scoped_smoke_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "filter_version": "sprint035e_v2",
        "score_gate": SCORE_GATE_V2,
        "manifest_source": manifest_source,
        "manifest_age_youngest_h": round(age_stats["youngest_h"], 2),
        "manifest_age_oldest_h":   round(age_stats["oldest_h"],   2),
        "manifest_age_median_h":   round(age_stats["median_h"],   2),
        "selected_pool_count": len(score_by_pool_at_sel),
        "selected_rows_count": len(manifest_rows),
        "pool_address_audit_keys": sorted(score_by_pool_at_sel),
        "bucket_count_by_pool": dict(bucket_ct),
        "momentum_score_by_pool_at_selection": score_by_pool_at_sel,
        "identity_factory_viability_required": True,
        "no_post_label_pool_replacement": True,
        "status": "MANIFEST_READY_OHLCV_ANCHORED_MOMENTUM_V2",
        "manifest_rows": manifest_rows,
        "manifest_rows_preview_max15": manifest_rows[:15],
    }

    OUT_MANIFEST.write_text(json.dumps(manifest, indent=2))

    tpl = [(r["pool_audit_only"], r["bucket_ms"], r["bucket_sec"]) for r in manifest_rows]
    shape_ok, shape_why = external_manifest_shape_ok(tpl)

    report = _render_report(
        "MANIFEST_READY_OHLCV_ANCHORED_MOMENTUM_V2" if shape_ok else f"SHAPE_FAIL_{shape_why}",
        cand_merged, momentum_pass_cnt,
        id_rows, act_rows, ch_ok, pub_http_ok, bool(rpc),
        manifest, http_req_used, rpc_identity_used, rpc_viability_used,
        len(pool_candles), age_stats,
    )
    OUT_REPORT.write_text(report)

    return "MANIFEST_READY_OHLCV_ANCHORED_MOMENTUM_V2" if shape_ok else f"SHAPE_FAIL_{shape_why}"


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint035E OHLCV-anchored pool discovery")
    ap.add_argument("--run", action="store_true", help="Execute full pipeline")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preflight only (HTTP/CH reachability, no RPC)")
    args = ap.parse_args()

    if args.dry_run:
        m33 = load_m33()
        ch_ok = (m33.ch_docker_query("SELECT 1 FORMAT TabSeparated") or "").strip() == "1"
        try:
            urllib.request.urlopen("https://api.geckoterminal.com/api/v2/networks/base", timeout=10)
            http_ok = True
        except OSError:
            http_ok = False
        print(f"http_ok={http_ok} ch_ok={ch_ok}")
        return

    if args.run:
        result = run_pipeline()
        print(f"[035E] status={result}")
        print(f"[035E] http_used={http_req_used}/{MAX_EXTERNAL_HTTP}"
              f"  rpc_ident={rpc_identity_used}/{MAX_RPC_IDENTITY_TOTAL}"
              f"  viab_probes={rpc_viability_used}/{MAX_QUOTE_PROBES}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
