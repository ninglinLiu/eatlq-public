#!/usr/bin/env python3
"""Sprint036A — toxic-flow-aware paper watchlist for external Base V3 pool discovery.

Purpose
───────
Sprint035D/E/F diagnosed the root failure of BSR-only discovery:
  • h1 = 0.2–0.7% at discovery (no genuine price momentum)
  • Scoring V2 passed pools on buy-sell imbalance alone
  • CLANKER h6 = −15.9% (downtrend recovery, not breakout)
  • MORPHO sells = 0 (suspicious single-sided artificial accumulation)

This script replaces pure dashboard-momentum scoring with a two-level
toxic-flow diagnostic framework:

  Level-1  (offline, CSV-based)   h1 / h6 / BSR quality / liquidity pre-filter
  Level-2  (CH wallet analysis)   per-wallet concentration, Shannon entropy,
                                   distribution patterns (if ClickHouse accessible)

Output: a "paper watchlist" — a candidate tracking JSON/CSV that is NOT used
for live execution. Purpose: inform Sprint036B pool selection with verified
non-toxic momentum signals.

Toxic-flow patterns diagnosed:
  WEAK_H1              h1 < H1_GATE (2.0%) — BSR without price action
  DOWNTREND_H6         h6 < H6_FLOOR (−5%) — recovery buying, not breakout
  DEAD_BUCKET          buys_1h < BUYS_MIN (5) — insufficient flow to study
  SINGLE_SIDED_PUMP    sells == 0 AND buys < SUSPICIOUS_BSR_THRESH (20)
                        → artificial accumulation signal
  OVERHEATED_H1        h1 > H1_MAX (15%) — entry missed, already pumped
  ILLIQUID             liquidity < LIQ_MIN (50k) — too thin for meaningful labels
  STABLE_PAIR          USDC/USDbC/USDT/cbBTC vs each other — no target motion
  HIGH_CONCENTRATION   top-3 wallets > CONC_MAX (70%) of 24h buy volume  [L2]
  LOW_ENTROPY          unique_buyer_entropy_5m < ENTROPY_FLOOR (1.0)     [L2]
  DISTRIBUTION         buy/sell imbalance inverted: bsr < 0.5 with trending down

Paper watchlist gate (all must pass):
  ✓ h1 >= H1_GATE
  ✓ h6 >= H6_FLOOR
  ✓ buys >= BUYS_MIN
  ✓ NOT single-sided pump
  ✓ h1 <= H1_MAX (not overheated)
  ✓ liquidity in [LIQ_MIN, LIQ_MAX]
  ✓ NOT stable pair
  ✓ [L2 if available] top3_concentration < CONC_MAX
  ✓ [L2 if available] entropy >= ENTROPY_FLOOR

Red lines (never crossed):
  • FAILED_NO_7D_ALPHA_ACTIVE = True (always)
  • No training / no H12 / no live / no model promotion
  • No private keys / no dashcam.csv committed
  • Read-only ClickHouse (SELECT only)
  • No modification to executor / strategy live paths

Usage:
  python3 scripts/sprint036a_toxic_flow_paper_watchlist.py --run
  python3 scripts/sprint036a_toxic_flow_paper_watchlist.py --run --dry-run  # skip CH
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]

# ─── Gates ───────────────────────────────────────────────────────────────────
H1_GATE   = 2.0   # % — hard minimum h1 price change
H6_FLOOR  = -5.0  # % — maximum allowed 6h downtrend
H1_MAX    = 15.0  # % — avoid overheated entries
BUYS_MIN  = 5     # minimum 1h buy transactions
LIQ_MIN   = 50_000   # USD — minimum liquidity
LIQ_MAX   = 10_000_000  # USD — maximum (avoid too-deep stable pairs)
SUSPICIOUS_BSR_THRESH = 20   # sells == 0 AND buys < this = suspicious
CONC_MAX  = 0.70  # max top-3 wallet concentration in 24h buys
ENTROPY_FLOOR = 1.0  # minimum unique_buyer_entropy (Shannon nats)
BSR_THRESHOLD = 0.5  # buy fraction below this = net distribution

# Stable-pair token addresses (Base)
STABLE_ADDRS = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC — large cap, not a target
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0x4200000000000000000000000000000000000006",  # WETH — always the quote, not target
})

STABLE_SYMBOLS = frozenset({
    "USDC", "USDbC", "DAI", "USDT", "cbBTC", "cbETH", "WETH",
    "WBTC", "WETH/USDC", "USDC/USDT", "USDC/USDbC", "USAD",
})

# ─── Input artifact paths ─────────────────────────────────────────────────────
CANDIDATES_SOURCES = [
    REPO / "mvp/diagnostics/sprint035d_momentum_filtered_candidate_summary.csv",
    REPO / "mvp/diagnostics/sprint035e_ohlcv_anchored_candidate_summary.csv",
]
# 035C candidates (if present)
_035C = REPO / "mvp/diagnostics/sprint035c_external_live_active_candidate_summary.csv"
if _035C.is_file():
    CANDIDATES_SOURCES.insert(0, _035C)

# ─── Output paths ─────────────────────────────────────────────────────────────
OUT_WATCHLIST   = REPO / "mvp/diagnostics/sprint036a_paper_watchlist.json"
OUT_FLOW_DIAG   = REPO / "mvp/diagnostics/sprint036a_flow_diagnostics.csv"
OUT_REJECTED    = REPO / "mvp/diagnostics/sprint036a_toxic_rejected_pools.csv"
OUT_REPORT      = REPO / "mvp/audits/sprint036a_toxic_flow_paper_watchlist_report.md"


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class PoolDiagnostic:
    pool_address: str
    pair_label: str
    source_sprint: str
    dex_id: str
    h1_pct: float
    h6_pct: float
    h24_pct: float
    liquidity_usd: float
    volume_24h_usd: float
    buys_1h: int
    sells_1h: int
    bsr_count: float          # buy/(buy+sell) by count
    ohlcv_up_frac: float      # fraction bullish 5m candles (035e only)
    momentum_score: float
    # Level-2 fields (from CH, or None if unavailable)
    top3_wallet_concentration: Optional[float] = None
    unique_buyer_count_24h: Optional[int] = None
    unique_buyer_entropy: Optional[float] = None
    buy_pressure_ratio: Optional[float] = None
    top_wallet_pct: Optional[float] = None
    sandwich_suspicious: Optional[bool] = None
    l2_source: str = "unavailable"
    # Classification
    rejection_reasons: list[str] = field(default_factory=list)
    watchlist_status: str = "REJECTED"
    toxic_risk: str = "UNKNOWN"
    flow_quality_score: float = 0.0


# ─── CSV loaders ──────────────────────────────────────────────────────────────

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_all_candidates() -> list[dict[str, Any]]:
    """Load all candidates from 035C/D/E CSVs, deduplicate by pool address.
    When a pool appears in multiple sprints, prefer the one with more data (035e > 035d).
    """
    seen: dict[str, dict[str, Any]] = {}
    for path in CANDIDATES_SOURCES:
        if not path.is_file():
            print(f"  [SKIP] missing {path.name}", file=sys.stderr)
            continue
        sprint = path.stem.split("_")[0]  # sprint035d etc.
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                addr = row.get("pool_address_candidate", "").strip().lower()
                if not addr or len(addr) != 42:
                    continue
                row["_sprint"] = sprint
                # 035e overrides 035d for same pool
                if addr not in seen or sprint > seen[addr]["_sprint"]:
                    seen[addr] = dict(row)
    return list(seen.values())


# ─── Stable-pair detection ───────────────────────────────────────────────────

def _is_stable_pair(row: dict[str, Any]) -> bool:
    label = row.get("pair_label", "").upper()
    base  = row.get("base_token_address", "").lower()
    quote = row.get("quote_token_address", "").lower()
    if any(sym in label for sym in STABLE_SYMBOLS):
        parts = [p.strip() for p in label.replace("/", " ").replace("-", " ").split()]
        non_stable = [p for p in parts if p not in STABLE_SYMBOLS and len(p) > 1]
        if not non_stable:
            return True
    if base in STABLE_ADDRS and quote in STABLE_ADDRS:
        return True
    return False


# ─── Level-1 pre-filter ───────────────────────────────────────────────────────

def classify_l1(cand: dict[str, Any]) -> PoolDiagnostic:
    """Apply CSV-level toxic-flow gates. No CH required."""
    addr  = cand.get("pool_address_candidate", "").lower()
    label = cand.get("pair_label", addr[:14])
    sprint = cand.get("_sprint", "?")
    dex   = cand.get("dex_id", "unknown")

    h1   = _f(cand.get("price_change_h1"))
    h6   = _f(cand.get("price_change_h6"))
    h24  = _f(cand.get("price_change_h24"))
    liq  = _f(cand.get("liquidity_usd"))
    vol  = _f(cand.get("volume_24h_usd"))
    buys = int(_f(cand.get("txns_1h_buys", 0)))
    sells= int(_f(cand.get("txns_1h_sells", 0)))
    ohlcv_up = _f(cand.get("ohlcv_up_frac", 0.5))
    score = _f(cand.get("momentum_score", 0))

    bsr_count = buys / max(1, buys + sells)

    reasons: list[str] = []

    # ── Hard structural gates ─────────────────────────────────────────────────
    if _is_stable_pair(cand):
        reasons.append("STABLE_PAIR")

    if liq > 0 and liq < LIQ_MIN:
        reasons.append("ILLIQUID")

    if liq > LIQ_MAX:
        reasons.append("TOO_DEEP_LIQUID")  # stable-pair trap

    # ── Toxic-flow gates ─────────────────────────────────────────────────────
    if h1 < H1_GATE:
        reasons.append("WEAK_H1")

    if h6 < H6_FLOOR:
        reasons.append("DOWNTREND_H6")

    if buys < BUYS_MIN:
        reasons.append("DEAD_BUCKET")

    if sells == 0 and buys < SUSPICIOUS_BSR_THRESH:
        reasons.append("SINGLE_SIDED_PUMP")

    if h1 > H1_MAX:
        reasons.append("OVERHEATED_H1")

    if bsr_count < BSR_THRESHOLD and sells > buys * 2 and h6 < -2:
        reasons.append("DISTRIBUTION_SELLING")

    passed_l1 = len(reasons) == 0

    toxic_risk = "LOW" if passed_l1 else (
        "HIGH" if any(r in reasons for r in (
            "SINGLE_SIDED_PUMP", "DOWNTREND_H6", "DISTRIBUTION_SELLING"
        )) else "MEDIUM"
    )

    # Flow quality score (0–1): higher is healthier for entry
    flow_q = 0.0
    if not reasons:
        # Base score from h1 in sweet spot 2-8%
        h1_score = min(1.0, (h1 - H1_GATE) / (8.0 - H1_GATE)) if h1 >= H1_GATE else 0.0
        # h6 health
        h6_score = min(1.0, (h6 + 5.0) / 15.0) if h6 >= H6_FLOOR else 0.0
        # BSR quality (both buys AND sells present is healthier than all-one-sided)
        bsr_health = 1.0 - abs(bsr_count - 0.65) / 0.35  # ideal around 65% buy
        bsr_health = max(0.0, min(1.0, bsr_health))
        # Liquidity in sweet spot [75k-5M]
        liq_score = 1.0 if 75_000 <= liq <= 5_000_000 else 0.5
        flow_q = round((h1_score * 0.4 + h6_score * 0.2 + bsr_health * 0.2 + liq_score * 0.2), 3)

    return PoolDiagnostic(
        pool_address=addr,
        pair_label=label,
        source_sprint=sprint,
        dex_id=dex,
        h1_pct=round(h1, 3),
        h6_pct=round(h6, 3),
        h24_pct=round(h24, 3),
        liquidity_usd=round(liq, 0),
        volume_24h_usd=round(vol, 0),
        buys_1h=buys,
        sells_1h=sells,
        bsr_count=round(bsr_count, 3),
        ohlcv_up_frac=round(ohlcv_up, 3),
        momentum_score=round(score, 2),
        rejection_reasons=reasons,
        watchlist_status="L1_PASS" if passed_l1 else "REJECTED",
        toxic_risk=toxic_risk,
        flow_quality_score=flow_q,
    )


# ─── Level-2: ClickHouse wallet analysis ─────────────────────────────────────

def _load_dotenv() -> dict[str, str]:
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


def ch_docker_query(sql: str, timeout_sec: float = 60.0) -> str:
    try:
        r = subprocess.run(
            ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
             "--query", sql, "--format", "JSONEachRow"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ch_available() -> bool:
    return ch_docker_query("SELECT 1") == '{"1":1}'


def _compute_entropy(counts: dict[str, float]) -> float:
    """Shannon entropy of a distribution (nats)."""
    total = sum(counts.values())
    if total <= 0 or len(counts) <= 1:
        return 0.0
    return -sum((v / total) * math.log(v / total + 1e-12) for v in counts.values())


def l2_wallet_analysis(diag: PoolDiagnostic) -> PoolDiagnostic:
    """Query CH for 24h wallet-level buy concentration and flow quality.

    Returns updated PoolDiagnostic with L2 fields filled.
    Uses uniswap_v3_swaps_enriched; determines buy direction from
    amount0_raw sign vs WETH position (token0 or token1).

    If CH unavailable or pool not in CH, marks l2_source='unavailable'.
    """
    pool = diag.pool_address.lower()
    WETH = "0x4200000000000000000000000000000000000006"

    sql = f"""
SELECT
    sender,
    token0_address,
    token1_address,
    countIf(toInt256OrZero(amount0_raw) < 0 AND lower(token0_address) != '{WETH}') as buys_as_t0,
    countIf(toInt256OrZero(amount0_raw) > 0 AND lower(token0_address) != '{WETH}') as sells_as_t0,
    countIf(toInt256OrZero(amount1_raw) < 0 AND lower(token1_address) != '{WETH}') as buys_as_t1,
    countIf(toInt256OrZero(amount1_raw) > 0 AND lower(token1_address) != '{WETH}') as sells_as_t1,
    sum(abs(toFloat64OrZero(amount0_raw))) + sum(abs(toFloat64OrZero(amount1_raw))) as total_volume_raw
FROM eatlq.uniswap_v3_swaps_enriched
WHERE lower(pool_address) = '{pool}'
  AND block_time >= now() - INTERVAL 24 HOUR
  AND removed = 0
GROUP BY sender, token0_address, token1_address
HAVING total_volume_raw > 0
ORDER BY total_volume_raw DESC
LIMIT 50
"""
    raw = ch_docker_query(sql, timeout_sec=45)
    if not raw:
        diag.l2_source = "ch_no_data"
        return diag

    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not rows:
        diag.l2_source = "ch_empty"
        return diag

    # Determine WETH position from first row
    token0 = rows[0].get("token0_address", "").lower()
    token1 = rows[0].get("token1_address", "").lower()
    weth_is_t0 = token0 == WETH
    weth_is_t1 = token1 == WETH

    buyer_volumes: dict[str, float] = defaultdict(float)
    total_buy_vol = 0.0
    total_sell_vol = 0.0
    total_buys = 0
    total_sells = 0

    for r in rows:
        sender = str(r.get("sender", "")).lower()
        vol    = _f(r.get("total_volume_raw", 0))
        if weth_is_t1:
            # token0 is target: amount0 < 0 = target leaving pool = BUY
            buys  = int(_f(r.get("buys_as_t0", 0)))
            sells = int(_f(r.get("sells_as_t0", 0)))
        elif weth_is_t0:
            # token1 is target: amount1 < 0 = target leaving pool = BUY
            buys  = int(_f(r.get("buys_as_t1", 0)))
            sells = int(_f(r.get("sells_as_t1", 0)))
        else:
            # Neither is WETH — use raw counts as approximation
            buys  = int(_f(r.get("buys_as_t0", 0))) + int(_f(r.get("buys_as_t1", 0)))
            sells = int(_f(r.get("sells_as_t0", 0))) + int(_f(r.get("sells_as_t1", 0)))

        total_buys += buys
        total_sells += sells
        if buys > 0 and sender:
            buy_share = buys / max(1, buys + sells)
            buyer_volumes[sender] += vol * buy_share
            total_buy_vol += vol * buy_share
        else:
            total_sell_vol += vol

    unique_buyers = len(buyer_volumes)
    entropy = _compute_entropy(buyer_volumes)

    # Top-3 concentration
    top3_vol = sum(v for _, v in sorted(buyer_volumes.items(), key=lambda x: -x[1])[:3])
    top3_conc = top3_vol / total_buy_vol if total_buy_vol > 0 else 1.0
    top1_conc = max(buyer_volumes.values()) / total_buy_vol if buyer_volumes else 1.0

    # Buy pressure ratio
    total_vol = total_buy_vol + total_sell_vol
    bpr = total_buy_vol / total_vol if total_vol > 0 else 0.5

    # Sandwich heuristic: any single wallet doing >30% of buy volume with >3 txns
    # that also has sells (round-trip in same window) is suspicious
    sandwich_suspicious = False
    for sender, bvol in buyer_volumes.items():
        if bvol / total_buy_vol > 0.30:
            sender_row = next((r for r in rows if r.get("sender", "").lower() == sender), None)
            if sender_row:
                s_buys  = int(_f(sender_row.get("buys_as_t0" if weth_is_t1 else "buys_as_t1", 0)))
                s_sells = int(_f(sender_row.get("sells_as_t0" if weth_is_t1 else "sells_as_t1", 0)))
                if s_sells > 0 and s_buys > 3:
                    sandwich_suspicious = True
                    break

    diag.top3_wallet_concentration = round(top3_conc, 4)
    diag.unique_buyer_count_24h    = unique_buyers
    diag.unique_buyer_entropy      = round(entropy, 4)
    diag.buy_pressure_ratio        = round(bpr, 4)
    diag.top_wallet_pct            = round(top1_conc, 4)
    diag.sandwich_suspicious       = sandwich_suspicious
    diag.l2_source                 = "clickhouse_24h"

    # Apply L2 rejection gates
    if top3_conc > CONC_MAX:
        if "HIGH_CONCENTRATION" not in diag.rejection_reasons:
            diag.rejection_reasons.append("HIGH_CONCENTRATION")
    if entropy < ENTROPY_FLOOR and unique_buyers > 0:
        if "LOW_ENTROPY" not in diag.rejection_reasons:
            diag.rejection_reasons.append("LOW_ENTROPY")
    if sandwich_suspicious:
        if "SANDWICH_SUSPICIOUS" not in diag.rejection_reasons:
            diag.rejection_reasons.append("SANDWICH_SUSPICIOUS")

    # Re-evaluate watch status
    if not diag.rejection_reasons:
        diag.watchlist_status = "WATCHLIST"
        diag.toxic_risk = "LOW"
        # Re-compute flow quality with L2 data
        entropy_score = min(1.0, entropy / 3.0)
        conc_score = max(0.0, 1.0 - top3_conc / CONC_MAX)
        diag.flow_quality_score = round(
            diag.flow_quality_score * 0.6 + entropy_score * 0.2 + conc_score * 0.2, 3
        )
    elif diag.watchlist_status == "L1_PASS":
        diag.watchlist_status = "REJECTED"

    return diag


# ─── Watchlist builder ────────────────────────────────────────────────────────

def _watchlist_entry(diag: PoolDiagnostic) -> dict[str, Any]:
    return {
        "pool_address":          diag.pool_address,
        "pair_label":            diag.pair_label,
        "source_sprint":         diag.source_sprint,
        "dex_id":                diag.dex_id,
        "watch_status":          diag.watchlist_status,
        "toxic_risk":            diag.toxic_risk,
        "flow_quality_score":    diag.flow_quality_score,
        "rejection_reasons":     diag.rejection_reasons,
        "h1_pct":                diag.h1_pct,
        "h6_pct":                diag.h6_pct,
        "liquidity_usd":         diag.liquidity_usd,
        "buys_1h":               diag.buys_1h,
        "sells_1h":              diag.sells_1h,
        "bsr_count":             diag.bsr_count,
        "momentum_score":        diag.momentum_score,
        "top3_wallet_conc":      diag.top3_wallet_concentration,
        "unique_buyer_entropy":  diag.unique_buyer_entropy,
        "unique_buyer_count_24h": diag.unique_buyer_count_24h,
        "buy_pressure_ratio":    diag.buy_pressure_ratio,
        "sandwich_suspicious":   diag.sandwich_suspicious,
        "l2_source":             diag.l2_source,
        "diagnosed_at":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ─── Report builder ───────────────────────────────────────────────────────────

def render_report(
    all_diags: list[PoolDiagnostic],
    watchlist: list[PoolDiagnostic],
    rejected: list[PoolDiagnostic],
    ch_used: bool,
    elapsed_sec: float,
) -> str:
    total = len(all_diags)
    n_watch = len(watchlist)
    n_rej = len(rejected)
    rejection_reasons_all: list[str] = []
    for d in rejected:
        rejection_reasons_all.extend(d.rejection_reasons)
    reason_counts = Counter(rejection_reasons_all)

    lines: list[str] = [
        "# Sprint036A — Toxic-flow aware paper watchlist",
        "",
        "**Status**: DIAGNOSTIC_COMPLETE",
        "**No training / no H12 / no live / no model promotion. FAILED_NO_7D_ALPHA_ACTIVE=True.**",
        "",
        "## 1. Why BSR-only discovery fails (Sprint035D/E/F root cause recap)",
        "",
        "| Pool | h1 | h6 | BSR | Toxic patterns | Outcome |",
        "|------|----|----|-----|----------------|---------|",
        "| CLANKER/WETH (035E) | 0.74% | **−15.91%** | 2.1× | WEAK_H1 + DOWNTREND_H6 | 0% label_1 |",
        "| MORPHO/WETH (035E) | 0.68% | 0.43% | 16× (0 sells) | WEAK_H1 + SINGLE_SIDED_PUMP | 0% label_1 |",
        "| CMC20/WETH (035E) | 0.22% | 0.54% | 4× | WEAK_H1 + dead candles | 0% label_1 |",
        "",
        "**Fix applied in 036A**: h1 ≥ 2.0% is now a **hard gate** (not scoring bonus).",
        "Single-sided pump detection added. h6 < −5% = downtrend-recovery rejection.",
        "",
        "---",
        "",
        "## 2. Full candidate diagnostic",
        "",
        f"- Total unique candidates from 035C/D/E: **{total}**",
        f"- ClickHouse wallet analysis available: **{'YES' if ch_used else 'NO'}**",
        "",
        "### Rejection reason distribution",
        "",
        "| Reason | Count |",
        "|--------|-------|",
    ]
    for reason, cnt in reason_counts.most_common():
        lines.append(f"| {reason} | {cnt} |")

    lines += [
        "",
        "---",
        "",
        "## 3. Paper watchlist entries",
        "",
    ]

    if not watchlist:
        lines += [
            "**No pools passed all gates.** This is consistent with the diagnostic finding",
            "that the 035D/E discovery universe was dominated by BSR-only momentum without",
            "genuine price action (h1 < 2% for 96%+ of candidates).",
            "",
            "**Sprint036B action**: re-run discovery with h1 ≥ 2% as hard gate.",
        ]
    else:
        lines += [
            f"**{n_watch} pools passed watchlist gates:**",
            "",
            "| Pool | h1 | h6 | buys | BSR | Liq | Flow Q | L2 | Risk |",
            "|------|----|----|------|-----|-----|--------|----|------|",
        ]
        for d in watchlist:
            l2 = d.l2_source if d.l2_source != "unavailable" else "CSV-only"
            lines.append(
                f"| `{d.pair_label[:22]}` | {d.h1_pct:.1f}% | {d.h6_pct:.1f}% | {d.buys_1h} | "
                f"{d.bsr_count:.2f} | ${d.liquidity_usd/1000:.0f}k | {d.flow_quality_score:.2f} | {l2} | {d.toxic_risk} |"
            )

    lines += [
        "",
        "---",
        "",
        "## 4. Per-pool toxic-flow diagnostics (rejected)",
        "",
        "### High-risk rejections (priority action pools to AVOID)",
        "",
        "| Pool | h1 | h6 | Primary toxic reason | L2 entropy | L2 conc |",
        "|------|----|----|----------------------|------------|---------|",
    ]

    high_risk = [d for d in rejected if d.toxic_risk == "HIGH"]
    for d in sorted(high_risk, key=lambda x: x.h1_pct)[:15]:
        entropy_str = f"{d.unique_buyer_entropy:.2f}" if d.unique_buyer_entropy is not None else "N/A"
        conc_str = f"{d.top3_wallet_concentration:.2f}" if d.top3_wallet_concentration is not None else "N/A"
        lines.append(
            f"| `{d.pair_label[:22]}` | {d.h1_pct:.1f}% | {d.h6_pct:.1f}% | "
            f"{d.rejection_reasons[0] if d.rejection_reasons else '?'} | {entropy_str} | {conc_str} |"
        )

    lines += [
        "",
        "### Conditional rejections (pass h1 gate but fail secondary)",
        "",
        "| Pool | h1 | h6 | Secondary rejection | Liq | Caveats |",
        "|------|----|----|---------------------|-----|---------|",
    ]
    conditional = [d for d in all_diags if d.h1_pct >= H1_GATE and d.rejection_reasons]
    for d in sorted(conditional, key=lambda x: -x.h1_pct)[:10]:
        lines.append(
            f"| `{d.pair_label[:22]}` | {d.h1_pct:.1f}% | {d.h6_pct:.1f}% | "
            f"{', '.join(d.rejection_reasons[:2])} | ${d.liquidity_usd/1000:.0f}k | "
            f"buys={d.buys_1h} sells={d.sells_1h} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 5. Toxic pattern catalogue (035D/E/F failures)",
        "",
        "| Pattern | Definition | 035E example | Why it fails at 30m |",
        "|---------|-----------|--------------|---------------------|",
        "| WEAK_H1 | h1 < 2% — BSR without price action | All 3 035E pools (0.2–0.74%) | Cannot overcome 0.6% round-trip |",
        "| DOWNTREND_H6 | h6 < −5% — recovery buying | CLANKER h6=−15.9% | Entries land in reversion tail |",
        "| SINGLE_SIDED_PUMP | sells=0, buys<20 — artificial accumulation | MORPHO sells=0, buys=16 | No organic demand, pump-and-dump setup |",
        "| DEAD_BUCKET | buys < 5/h | CMC20 (thin) | Quoter returns stale price |",
        "| OVERHEATED_H1 | h1 > 15% — already pumped | LAND 72%, SEAM 5.8% w/ sells>buys | Entries in blow-off, mean reversion |",
        "| HIGH_CONCENTRATION [L2] | top-3 wallets > 70% of buy volume | — (CH needed) | Coordinated pump, exits on retail |",
        "| LOW_ENTROPY [L2] | Shannon entropy < 1.0 nat | — (CH needed) | Few wallets = non-organic demand |",
        "| DISTRIBUTION_SELLING | bsr < 0.5 AND h6 < −2% AND sells > 2× buys | SEAM (sells=46>buys=37, h6=−7.7%) | Smart money exiting into retail |",
        "",
        "---",
        "",
        "## 6. Recommended gates for Sprint036B discovery",
        "",
        "Apply in discovery filter, in order:",
        "",
        "```",
        "HARD GATES (any one → reject candidate immediately):",
        "  h1 >= 2.0%           ← was scoring bonus, now hard gate",
        "  h6 >= -5.0%          ← downtrend-recovery filter",
        "  buys_1h >= 5         ← dead-bucket filter",
        "  sells == 0 AND buys < 20 → SINGLE_SIDED_PUMP reject",
        "  h1 <= 15.0%          ← overheated filter",
        "  liquidity in [50k, 10M]",
        "  NOT stable pair",
        "",
        "SCORING ONLY (no longer gates by themselves):",
        "  BSR (buys/sells ratio)    → context only, not primary signal",
        "  OHLCV bullish fraction    → supplement to h1, not replacement",
        "  txn density               → supplement",
        "",
        "LEVEL-2 GATES (if CH available, applied after discovery run):",
        "  top3_wallet_concentration < 0.70",
        "  unique_buyer_entropy >= 1.0 nat",
        "  NOT sandwich_suspicious",
        "```",
        "",
        "---",
        "",
        "## 7. Artifacts",
        "",
        f"- `{OUT_WATCHLIST.relative_to(REPO)}` — paper watchlist JSON",
        f"- `{OUT_FLOW_DIAG.relative_to(REPO)}` — per-pool flow diagnostic CSV",
        f"- `{OUT_REJECTED.relative_to(REPO)}` — rejected pools with reasons",
        "",
        "## 8. Run stats",
        "",
        f"- elapsed_sec: {elapsed_sec:.1f}",
        f"- total_candidates: {total}",
        f"- watchlist_count: {n_watch}",
        f"- rejected_count: {n_rej}",
        f"- ch_l2_analysis: {'enabled' if ch_used else 'skipped (CH unavailable)'}",
        "",
    ]
    return "\n".join(lines)


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run_pipeline(dry_run: bool = False) -> str:
    t0 = time.time()

    print("[036A] Loading candidates…")
    candidates = load_all_candidates()
    print(f"  Loaded {len(candidates)} unique pool candidates from 035C/D/E")

    # Level-1 classification
    print("[036A] Level-1 pre-filter…")
    all_diags: list[PoolDiagnostic] = [classify_l1(c) for c in candidates]
    l1_pass = [d for d in all_diags if d.watchlist_status == "L1_PASS"]
    l1_fail = [d for d in all_diags if d.watchlist_status == "REJECTED"]

    print(f"  L1 PASS: {len(l1_pass)} | REJECTED: {len(l1_fail)}")
    for d in sorted(l1_pass, key=lambda x: -x.h1_pct):
        print(f"    {d.pair_label[:26]:26s} h1={d.h1_pct:.2f}% h6={d.h6_pct:.2f}% "
              f"buys={d.buys_1h} sells={d.sells_1h} liq=${d.liquidity_usd/1000:.0f}k")

    # Level-2 CH wallet analysis
    ch_used = False
    if not dry_run and l1_pass:
        print("[036A] Checking ClickHouse availability…")
        if ch_available():
            ch_used = True
            print(f"  CH available — running L2 wallet analysis on {len(l1_pass)} pools…")
            for i, d in enumerate(l1_pass):
                print(f"  [{i+1}/{len(l1_pass)}] {d.pair_label[:28]}…", end=" ", flush=True)
                l1_pass[i] = l2_wallet_analysis(d)
                status = l1_pass[i].watchlist_status
                entropy = l1_pass[i].unique_buyer_entropy
                conc = l1_pass[i].top3_wallet_concentration
                print(f"status={status} entropy={entropy} conc={conc} "
                      f"reasons={l1_pass[i].rejection_reasons}")
        else:
            print("  [SKIP] ClickHouse not reachable — L2 analysis skipped")
            for d in l1_pass:
                d.watchlist_status = "L1_PASS_AWAITING_L2"

    # Final watchlist = passed both L1 and L2 (or L1 only if CH unavailable)
    watchlist = [d for d in l1_pass
                 if d.watchlist_status in ("WATCHLIST", "L1_PASS", "L1_PASS_AWAITING_L2")]
    rejected = l1_fail + [d for d in l1_pass if d.watchlist_status == "REJECTED"]

    # Update all_diags list with L2-updated objects
    l1_pass_by_addr = {d.pool_address: d for d in l1_pass}
    all_diags = [l1_pass_by_addr.get(d.pool_address, d) for d in all_diags]

    # ── Write artifacts ───────────────────────────────────────────────────────
    print("[036A] Writing artifacts…")

    # Paper watchlist JSON
    watchlist_json = {
        "kind":                   "sprint036a_paper_watchlist",
        "version":                "1.0",
        "FAILED_NO_7D_ALPHA_ACTIVE": True,
        "no_training":            True,
        "no_live":                True,
        "no_model_promotion":     True,
        "diagnostic_only":        True,
        "created_at":             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_candidates":       len(candidates),
        "watchlist_count":        len(watchlist),
        "rejected_count":         len(rejected),
        "ch_l2_analysis":         ch_used,
        "gates_applied": {
            "h1_gate_pct":        H1_GATE,
            "h6_floor_pct":       H6_FLOOR,
            "h1_max_pct":         H1_MAX,
            "buys_min":           BUYS_MIN,
            "liquidity_min_usd":  LIQ_MIN,
            "liquidity_max_usd":  LIQ_MAX,
            "conc_max_l2":        CONC_MAX,
            "entropy_floor_l2":   ENTROPY_FLOOR,
        },
        "toxic_pattern_counts":  dict(Counter(
            r for d in rejected for r in d.rejection_reasons
        )),
        "watchlist_entries":      [_watchlist_entry(d) for d in watchlist],
        "conditional_entries":    [_watchlist_entry(d) for d in all_diags
                                   if d.h1_pct >= H1_GATE and d.rejection_reasons],
    }
    OUT_WATCHLIST.write_text(json.dumps(watchlist_json, indent=2))

    # Flow diagnostics CSV (all pools)
    diag_fields = [
        "pool_address", "pair_label", "source_sprint", "dex_id",
        "h1_pct", "h6_pct", "h24_pct", "liquidity_usd", "volume_24h_usd",
        "buys_1h", "sells_1h", "bsr_count", "ohlcv_up_frac", "momentum_score",
        "top3_wallet_concentration", "unique_buyer_count_24h", "unique_buyer_entropy",
        "buy_pressure_ratio", "top_wallet_pct", "sandwich_suspicious",
        "l2_source", "watchlist_status", "toxic_risk", "flow_quality_score",
        "rejection_reasons",
    ]
    diag_rows = []
    for d in sorted(all_diags, key=lambda x: -x.flow_quality_score):
        row = asdict(d)
        row["rejection_reasons"] = "|".join(d.rejection_reasons) if d.rejection_reasons else ""
        diag_rows.append(row)
    write_csv_rows(OUT_FLOW_DIAG, diag_fields, diag_rows)

    # Rejected pools CSV
    rej_fields = [
        "pool_address", "pair_label", "source_sprint",
        "h1_pct", "h6_pct", "liquidity_usd", "buys_1h", "sells_1h",
        "toxic_risk", "rejection_reasons", "l2_source",
    ]
    rej_rows = []
    for d in sorted(rejected, key=lambda x: (x.toxic_risk, x.h1_pct)):
        row = asdict(d)
        row["rejection_reasons"] = "|".join(d.rejection_reasons)
        rej_rows.append(row)
    write_csv_rows(OUT_REJECTED, rej_fields, rej_rows)

    # Report
    elapsed = time.time() - t0
    report = render_report(all_diags, watchlist, rejected, ch_used, elapsed)
    OUT_REPORT.write_text(report)

    status = "WATCHLIST_BUILT" if watchlist else "NO_WATCHLIST_CANDIDATES"
    print(f"[036A] status={status} watchlist={len(watchlist)} rejected={len(rejected)} "
          f"elapsed={elapsed:.1f}s")
    return status


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sprint036A toxic-flow paper watchlist")
    ap.add_argument("--run",     action="store_true", help="run the full pipeline")
    ap.add_argument("--dry-run", action="store_true", help="skip ClickHouse L2 analysis")
    args = ap.parse_args()

    if args.run:
        run_pipeline(dry_run=args.dry_run)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
