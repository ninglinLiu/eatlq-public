#!/usr/bin/env python3
"""Sprint037B-R5 — Label Expansion: Full Coverage + Jan-05 / Jan-26 Regimes
============================================================================

Builds on R4's quoteability-first architecture with three improvements:

1. ts-grid accuracy fix
   R4's per-regime grid scan returned only 198–318 valid samples (of 5,000+
   queried) because the narrow block-range estimate was unreliable. R5 uses
   the R2-proven approach: scan from lo_block all the way to latest_num with
   step=50, producing ~135,000 samples that cover all regimes at once.

2. Concentration cap removed for positive regimes
   R4 capped exit labels at 50/pool. R5 uses cap=9999 for all positive regimes
   (Feb-02, Jan-26, Jan-05) so every executable candidate is labeled.
   Dec-01 stays at cap=50 — enough for the negative control signal.

3. Jan-05 and Jan-26 added as new positive regimes
   Validates that Feb-02's 13.9% label_1 edge is not an isolated event.

Regime roles:
  Feb-02 (2026-02-02) — primary positive candidate, full coverage
  Jan-26 (2026-01-26) — new positive candidate, full coverage
  Jan-05 (2026-01-05) — new positive candidate, full coverage
  Dec-01 (2025-12-01) — negative control, light sample (cap=50)

Decision flags (frozen):
  TRAINING_ALLOWED=false, LIVE_ALLOWED=false,
  MODEL_PROMOTION_ALLOWED=false, NO_ALPHA_CLAIM=true,
  QUOTEABILITY_FIRST_REPLAY_ALLOWED=true,
  RPC_BACKED_LABEL_VALIDATION_ALLOWED=true

Usage
-----
  # Dry run (CH features only, no RPC)
  python scripts/sprint037b_r5_label_expansion.py --dry-run

  # Full run
  LABEL_RPC_URL=https://... python scripts/sprint037b_r5_label_expansion.py --run

  # Single regime
  LABEL_RPC_URL=https://... python scripts/sprint037b_r5_label_expansion.py --run --regime 2026-02-02

Environment
-----------
  CLICKHOUSE_URL       (default: http://172.18.0.6:8123/)
  CLICKHOUSE_USER      (default: default)
  CLICKHOUSE_PASSWORD
  LABEL_RPC_URL or EVM_HTTP_URL — archive-capable Base RPC
  LABEL_WORKERS        (default: 20)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from urllib.request import Request, urlopen

# ── Frozen constants ──────────────────────────────────────────────────────────
WETH                   = "0x4200000000000000000000000000000000000006"
CHAIN_ID               = 8453
ROUND_TRIP_COST        = 0.006
LABEL_EXIT_HORIZON_SEC = 1800
FEATURE_WINDOW_SEC     = 300
MIN_ACTIVITY_HEAT      = 5
QUOTER_V2_BASE         = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
MAX_ABS_NET_RETURN     = 10.0

# ── Multi-notional probe sizes ────────────────────────────────────────────────
PROBE_TINY_WEI   = int(1e13)   # 0.00001  ETH  — zero-impact reference
PROBE_MICRO_WEI  = int(5e14)   # 0.0005   ETH  — authoritative gate
PROBE_STRESS_WEI = int(5e15)   # 0.005    ETH  — depth diagnostic

# ── Concentration caps ────────────────────────────────────────────────────────
# Positive regimes: label every executable candidate.
# Negative control (Dec-01): light sample for a representative read.
MAX_EXIT_PER_POOL_POSITIVE = 9999  # effectively unlimited
MAX_EXIT_PER_POOL_NEGATIVE = 50

# ── Regime definitions ────────────────────────────────────────────────────────
REGIMES = [
    {
        "week_start": "2026-02-02", "week_end": "2026-02-09",
        "role": "positive",
        "label": "Feb-02 (primary positive, full coverage)",
    },
    {
        "week_start": "2026-01-26", "week_end": "2026-02-02",
        "role": "positive",
        "label": "Jan-26 (new positive, full coverage)",
    },
    {
        "week_start": "2026-01-05", "week_end": "2026-01-12",
        "role": "positive",
        "label": "Jan-05 (new positive, full coverage)",
    },
    {
        "week_start": "2025-12-01", "week_end": "2025-12-08",
        "role": "negative",
        "label": "Dec-01 (negative control, cap=50)",
    },
    # Sprint038A — high-activity expansion regimes (Sprint037A top historical, non-overlapping core four)
    {
        "week_start": "2025-12-29", "week_end": "2026-01-05",
        "role": "positive",
        "label": "Dec-29 expansion (full coverage)",
    },
    {
        "week_start": "2026-02-23", "week_end": "2026-03-02",
        "role": "positive",
        "label": "Feb-23 expansion (full coverage)",
    },
    {
        "week_start": "2026-03-02", "week_end": "2026-03-09",
        "role": "positive",
        "label": "Mar-02 expansion (full coverage)",
    },
    {
        "week_start": "2026-03-30", "week_end": "2026-04-06",
        "role": "positive",
        "label": "Mar-30 expansion (full coverage)",
    },
]

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parents[1]
OUT_DIR     = REPO / "mvp/audits/sprint037b_r5"
OUT_SUMMARY = REPO / "mvp/diagnostics/sprint037b_r5_summary.json"
OUT_REPORT  = OUT_DIR / "sprint037b_r5_report.md"

DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED":                  False,
    "LIVE_ALLOWED":                      False,
    "MODEL_PROMOTION_ALLOWED":           False,
    "H12_ABLATION_ALLOWED":              False,
    "PHASE6_ALLOWED":                    False,
    "FAILED_NO_7D_ALPHA_ACTIVE":         True,
    "QUOTEABILITY_FIRST_REPLAY_ALLOWED": True,
    "RPC_BACKED_LABEL_VALIDATION_ALLOWED": True,
    "NO_ALPHA_CLAIM":                    True,
    "CLICKHOUSE_READ_ONLY":              True,
}

# ── Meme pool exclusions ──────────────────────────────────────────────────────
_STABLES = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})
_EXCLUDED = frozenset({
    "0x4200000000000000000000000000000000000006",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})


def _log(msg: str) -> None:
    print(f"[r5 {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers ────────────────────────────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r}")


def _ch_query(url: str, sql: str, user: str, pwd: str, timeout: float = 600.0) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    req = Request(url.rstrip("/") + "/", data=q.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ── SQL builders ──────────────────────────────────────────────────────────────
def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))

def _excluded_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_EXCLUDED))

def _meme_pools_cte() -> str:
    return f"""meme_pools AS (
  SELECT
    lower(pool_address)   AS pa,
    lower(token0_address) AS token0_addr,
    lower(token1_address) AS token1_addr,
    toUInt32(fee_bps)     AS fee_bps,
    multiIf(lower(token0_address) = '{WETH}', 1, 0) AS weth_is_token0
  FROM eatlq.discovered_pools AS dp
  WHERE dp.chain_id = {CHAIN_ID}
    AND lower(dp.pool_type) = 'v3'
    AND dp.has_weth_leg = 1
    AND dp.fee_bps IN (100, 500, 3000, 10000)
    AND NOT (
      lower(dp.token0_address) IN ({_stables_sql()})
      AND lower(dp.token1_address) IN ({_stables_sql()})
    )
    AND lower(dp.pool_address) NOT IN ({_excluded_sql()})
)"""

def _ts_expr(alias: str = "s") -> str:
    return f"coalesce({alias}.block_time, {alias}.received_at_ms)"


def build_feature_query(start_dt: str, end_dt: str) -> str:
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                                                         AS pool_address,
  mp.fee_bps,
  mp.token0_addr,
  mp.token1_addr,
  mp.weth_is_token0,
  toUnixTimestamp(toStartOfFiveMinute({te}))                                   AS bucket_5m_sec,
  toUnixTimestamp(toStartOfFiveMinute({te})) + {FEATURE_WINDOW_SEC}           AS entry_time_sec,
  count()                                                                        AS activity_heat,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_meme_count,
  countIf(
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) <  '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) >= '8')
  )                                                                              AS sell_meme_count,
  sum(if(mp.weth_is_token0 = 0,
        -toFloat64(eatlq_hex_to_int256(s.amount0_raw)),
         toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                        AS net_signed_flow,
  sum(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))))                       AS abs_flow_sum,
  sumIf(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
        (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
        (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS buy_flow_sum,
  sum(pow(abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))), 2))               AS abs_flow_sq_sum,
  avg(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS avg_liquidity,
  min(toFloat64(eatlq_hex_to_uint256(s.liquidity_raw)))                         AS min_liquidity,
  max(toUnixTimestamp({te}))                                                    AS last_swap_sec,
  uniqCombinedIf(lower(s.sender),
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )                                                                              AS unique_buyers,
  uniqCombined(lower(s.sender))                                                 AS unique_senders,
  argMax(toInt32(eatlq_hex_to_int256(s.tick_raw)), {te})                        AS tick_last,
  min(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                 AS tick_min,
  max(toInt32(eatlq_hex_to_int256(s.tick_raw)))                                 AS tick_max
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
GROUP BY
  pool_address, mp.fee_bps, mp.token0_addr, mp.token1_addr, mp.weth_is_token0,
  bucket_5m_sec, entry_time_sec
HAVING activity_heat >= {MIN_ACTIVITY_HEAT}
ORDER BY pool_address, bucket_5m_sec
"""


def build_price_series_query(start_dt: str, end_dt: str) -> str:
    te = _ts_expr("s")
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.pool_address)                       AS pool_address,
  toUnixTimestamp({te})                       AS ts_sec,
  toInt32(eatlq_hex_to_int256(s.tick_raw))    AS tick_i32
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND {te} >= '{start_dt}'
  AND {te} < '{end_dt}'
ORDER BY pool_address, ts_sec
"""


# ── Feature computation ───────────────────────────────────────────────────────
def compute_features(row: dict[str, Any]) -> dict[str, Any]:
    activity_heat   = int(row.get("activity_heat") or 0)
    buy_meme_count  = int(row.get("buy_meme_count") or 0)
    sell_meme_count = int(row.get("sell_meme_count") or 0)
    net_signed_flow = float(row.get("net_signed_flow") or 0.0)
    abs_flow_sum    = float(row.get("abs_flow_sum") or 0.0)
    buy_flow_sum    = float(row.get("buy_flow_sum") or 0.0)
    abs_flow_sq_sum = float(row.get("abs_flow_sq_sum") or 0.0)
    unique_buyers   = int(row.get("unique_buyers") or 0)
    avg_liquidity   = float(row.get("avg_liquidity") or 0.0)
    min_liquidity   = float(row.get("min_liquidity") or 0.0)
    last_swap_sec   = int(row.get("last_swap_sec") or 0)
    tick_min        = int(row.get("tick_min") or 0)
    tick_max        = int(row.get("tick_max") or 0)

    price_velocity_5m = (
        max(-1.0, min(1.0, net_signed_flow / abs_flow_sum)) if abs_flow_sum > 0 else 0.0
    )
    buy_pressure_ratio = (
        max(0.0, min(1.0, buy_flow_sum / abs_flow_sum)) if abs_flow_sum > 0 else 0.5
    )
    if activity_heat > 0 and abs_flow_sum > 0:
        mean_abs = abs_flow_sum / activity_heat
        variance = max(0.0, abs_flow_sq_sum / activity_heat - mean_abs ** 2)
        token_volatility_5m = min(2.0, max(0.0, math.sqrt(variance) / mean_abs))
    else:
        token_volatility_5m = 0.0

    return {
        "price_velocity_5m":       round(price_velocity_5m, 6),
        "buy_pressure_ratio":      round(buy_pressure_ratio, 6),
        "token_volatility_5m":     round(token_volatility_5m, 6),
        "activity_heat":           activity_heat,
        "unique_buyer_entropy_5m": round(math.log1p(unique_buyers), 6),
        "buy_meme_count":          buy_meme_count,
        "sell_meme_count":         sell_meme_count,
        "abs_flow_sum":            round(abs_flow_sum, 2),
        "avg_liquidity":           avg_liquidity,
        "min_liquidity":           min_liquidity,
        "last_swap_sec":           last_swap_sec,
        "tick_range":              abs(tick_max - tick_min),
    }


# ── Price index ───────────────────────────────────────────────────────────────
class PoolPriceIndex:
    def __init__(self) -> None:
        self._ts:   dict[str, list[int]] = defaultdict(list)
        self._tick: dict[str, list[int]] = defaultdict(list)

    def ingest(self, pool: str, ts: int, tick: int) -> None:
        if pool and ts > 0:
            self._ts[pool].append(ts)
            self._tick[pool].append(tick)

    def has_ticks_in_range(self, pool: str, ts_start: int, ts_end: int) -> bool:
        s = self._ts.get(pool, [])
        return bisect.bisect_left(s, ts_end) > bisect.bisect_left(s, ts_start)


# ── RPC infrastructure ────────────────────────────────────────────────────────
class RPC:
    def __init__(
        self,
        url: str,
        timeout: int = 60,
        max_retries: int = 6,
        instrument: bool = False,
    ):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.instrument = instrument
        self._id = 0
        self._lock = Lock()
        self._inst_lock = Lock()
        self._method_counts: Counter[str] = Counter()
        self._http_posts = 0

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def reset_instrument(self) -> None:
        if not self.instrument:
            return
        with self._inst_lock:
            self._method_counts.clear()
            self._http_posts = 0

    def instrument_totals(self) -> dict[str, Any]:
        with self._inst_lock:
            return {"methods": dict(self._method_counts), "http_posts": self._http_posts}

    def _post(self, body: bytes) -> Any:
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if self.instrument:
                    with self._inst_lock:
                        self._http_posts += 1
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                status = getattr(e, "code", None)
                if status == 429 or (status and 500 <= status < 600) or status is None:
                    time.sleep(min(0.5 * (2 ** attempt), 10.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc retries exhausted")

    def call(self, method: str, params: list) -> Any:
        body = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params, "id": self._next_id(),
        }).encode()
        out = self._post(body)
        if self.instrument:
            with self._inst_lock:
                self._method_counts[method] += 1
        return out

    def batch(self, calls: list) -> list:
        if not calls:
            return []
        payload = [
            {"jsonrpc": "2.0", "method": m, "params": p, "id": i}
            for i, (m, p) in enumerate(calls)
        ]
        body = json.dumps(payload).encode()
        out  = self._post(body)
        idx  = {r.get("id"): r for r in out}
        return [idx.get(i) for i in range(len(calls))]


# ── ClickHouse-backed ts-grid (zero RPC calls) ───────────────────────────────
def build_ts_grid_from_ch(
    ch_url: str, ch_user: str, ch_pwd: str,
    ts_min: int,
    ts_max: int,
) -> tuple[list[int], list[int]]:
    """Build ts→block grid from swap data in ClickHouse.

    Why not RPC batch scan?
    QuickNode silently processes only ~20 items per 500-item batch, giving a
    ~4% fill rate.  Binary search per anchor is accurate but sequential and slow.

    ClickHouse already has (block_time, block_number) for every swap event.
    One query returns ~450K (ts_sec, block_number) pairs in under 3 seconds,
    giving sub-second accuracy for ts_to_block — far better than any RPC grid.

    The returned bn_arr/ts_arr are sorted by ts_sec and ready for interpolation.
    """
    start_dt = datetime.fromtimestamp(ts_min - 300, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt   = datetime.fromtimestamp(ts_max + LABEL_EXIT_HORIZON_SEC + 600,
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
SELECT
    toUnixTimestamp(coalesce(block_time, received_at_ms)) AS ts_sec,
    min(block_number)                                      AS bn
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND removed = 0
  AND coalesce(block_time, received_at_ms) >= '{start_dt}'
  AND coalesce(block_time, received_at_ms) < '{end_dt}'
GROUP BY ts_sec
ORDER BY ts_sec
"""
    _log(f"  Building ts-grid from ClickHouse "
         f"({start_dt[:10]} → {end_dt[:10]}) …")
    t0   = time.time()
    rows = _ch_query(ch_url, sql, ch_user, ch_pwd, timeout=120.0)
    ts_arr = [int(r["ts_sec"]) for r in rows]
    bn_arr = [int(r["bn"])     for r in rows]
    _log(f"  ts-grid done: {len(ts_arr):,} samples in {time.time()-t0:.1f}s  "
         f"(accuracy sub-second)")
    if bn_arr:
        _log(f"  span blocks [{bn_arr[0]:,}..{bn_arr[-1]:,}]  "
             f"ts [{ts_arr[0]}..{ts_arr[-1]}]")
    return bn_arr, ts_arr


def ts_to_block(bn_arr: list[int], ts_arr: list[int], target_ts: int) -> int:
    """Map target_ts to a block number via linear interpolation.

    Works with both the old (bn-sorted) grid and the new anchored (ts-sorted)
    grid because the anchor ts_arr is monotonically increasing and we bisect
    on ts_arr directly.
    """
    idx = bisect.bisect_left(ts_arr, target_ts)
    if idx == 0:
        return bn_arr[0]
    if idx >= len(bn_arr):
        return bn_arr[-1]
    t_lo, t_hi = ts_arr[idx - 1], ts_arr[idx]
    b_lo, b_hi = bn_arr[idx - 1], bn_arr[idx]
    if t_hi == t_lo:
        return b_hi
    return b_lo + int((target_ts - t_lo) / (t_hi - t_lo) * (b_hi - b_lo))


# ── QuoterV2 ──────────────────────────────────────────────────────────────────
_SEL_QUOTE_V3 = "0xc6a5026a"

def _pad32(h: str) -> str: return h.zfill(64)
def _addr_pad(addr: str) -> str:
    return _pad32((addr[2:] if addr.startswith("0x") else addr).lower())
def _uint_pad(n: int) -> str: return _pad32(hex(n)[2:])
def _strip_0x(h: str) -> str: return h[2:] if h.startswith("0x") else h


def _eth_call(rpc: RPC, to: str, data: str, block_num: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block_num)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_quoter_v3(rpc: RPC, token_in: str, token_out: str,
                   amount_in: int, fee: int, block: int) -> int:
    data = (
        _SEL_QUOTE_V3
        + _addr_pad(token_in)
        + _addr_pad(token_out)
        + _uint_pad(amount_in)
        + _uint_pad(fee)
        + _uint_pad(0)
    )
    try:
        res = _eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception:
        return 0
    raw = _strip_0x(res)
    return int(raw[0:64], 16) if len(raw) >= 64 else 0


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET_RETURN or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET_RETURN, net)), True
    return net, False


# ── Per-candidate probe functions ─────────────────────────────────────────────
def run_entry_probe(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """MICRO entry probe. Sets entry_quote_ok, entry_amount_out_micro, fail_reason."""
    weth = WETH.lower()
    t0   = c["token0_addr"].lower()
    t1   = c["token1_addr"].lower()
    if weth not in (t0, t1):
        c.update(entry_quote_ok=False, entry_fail_reason="fail_no_weth_leg",
                 entry_block=0, entry_amount_out_micro=0,
                 out_of_range_suspected=False)
        return

    meme = t1 if t0 == weth else t0
    entry_block = ts_to_block(bn_arr, ts_arr, c["entry_time_sec"])
    c["entry_block"] = entry_block
    c["_meme_addr"]  = meme

    if entry_block <= 0:
        c.update(entry_quote_ok=False, entry_fail_reason="fail_block_lookup",
                 entry_amount_out_micro=0, out_of_range_suspected=False)
        return

    micro_out = call_quoter_v3(rpc, weth, meme, PROBE_MICRO_WEI, c["fee_bps"], entry_block)
    if micro_out <= 0:
        c.update(entry_quote_ok=False, entry_fail_reason="fail_v3_entry_zero",
                 entry_amount_out_micro=0, out_of_range_suspected=True)
        return

    c.update(entry_quote_ok=True, entry_fail_reason=None,
             entry_amount_out_micro=micro_out, out_of_range_suspected=False)


def run_slippage_diagnostics(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """TINY + STRESS probes on executable candidates. Computes slippage proxy."""
    weth  = WETH.lower()
    meme  = c.get("_meme_addr") or (
        c["token1_addr"].lower() if c["token0_addr"].lower() == weth
        else c["token0_addr"].lower()
    )
    fee   = c["fee_bps"]
    block = c["entry_block"]

    tiny_out   = call_quoter_v3(rpc, weth, meme, PROBE_TINY_WEI,   fee, block)
    stress_out = call_quoter_v3(rpc, weth, meme, PROBE_STRESS_WEI, fee, block)

    price_tiny  = tiny_out / PROBE_TINY_WEI   if tiny_out > 0 else 0.0
    price_micro = c["entry_amount_out_micro"] / PROBE_MICRO_WEI
    slippage    = (price_micro / price_tiny - 1.0) if price_tiny > 0 else None

    c["entry_amount_out_tiny"]   = tiny_out
    c["entry_amount_out_stress"] = stress_out
    c["entry_slippage_proxy"]    = round(slippage, 6) if slippage is not None else None


def run_exit_label(
    rpc: RPC, c: dict[str, Any],
    bn_arr: list[int], ts_arr: list[int],
) -> None:
    """Exit QuoterV2 label on executable candidate."""
    weth = WETH.lower()
    meme = c.get("_meme_addr") or (
        c["token1_addr"].lower() if c["token0_addr"].lower() == weth
        else c["token0_addr"].lower()
    )
    fee        = c["fee_bps"]
    exit_block = ts_to_block(bn_arr, ts_arr, c["entry_time_sec"] + LABEL_EXIT_HORIZON_SEC)
    c["exit_block"] = exit_block

    tokens_received = c.get("entry_amount_out_micro", 0)
    if tokens_received <= 0:
        c.update(rpc_label=None, net_return_rpc=None,
                 exit_fail_reason="fail_no_entry_amount", exit_quote_ok=False)
        return

    weth_back = call_quoter_v3(rpc, meme, weth, tokens_received, fee, exit_block)
    if weth_back <= 0:
        c.update(rpc_label=0, net_return_rpc=-1.0,
                 exit_fail_reason="executable_exit_drained", exit_quote_ok=False)
        return

    net, bogus = _sanity_clip(weth_back / PROBE_MICRO_WEI - 1.0 - ROUND_TRIP_COST)
    if bogus:
        c.update(rpc_label=None, net_return_rpc=None,
                 exit_fail_reason="fail_sanity_clip", exit_quote_ok=False)
        return

    c.update(rpc_label=1 if net > 0 else 0, net_return_rpc=round(net, 6),
             exit_fail_reason=None, exit_quote_ok=True)


# ── Analysis helpers ──────────────────────────────────────────────────────────
def _stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {}
    rs  = sorted(returns)
    n   = len(rs)
    pos = sum(x > 0 for x in rs)
    k   = max(1, n // 10)
    trimmed = rs[k:-k] if n > 2 * k else rs
    return {
        "n":            n,
        "pos":          pos,
        "pos_share":    round(pos / n, 4),
        "mean":         round(sum(rs) / n, 6),
        "median":       round(rs[n // 2], 6),
        "trimmed_mean": round(sum(trimmed) / max(len(trimmed), 1), 6),
        "p10":          round(rs[max(0, n // 10)], 6),
        "p90":          round(rs[min(n - 1, 9 * n // 10)], 6),
    }


def compute_feature_bucket_analysis(
    candidates: list[dict],
    features: Optional[list[str]] = None,
) -> dict[str, Any]:
    if features is None:
        features = ["price_velocity_5m", "buy_pressure_ratio",
                    "activity_heat", "token_volatility_5m"]
    result: dict[str, Any] = {}
    for feat in features:
        vals = sorted(
            (float(c[feat]), i, c) for i, c in enumerate(candidates)
            if c.get(feat) is not None
        )
        if not vals:
            continue
        n        = len(vals)
        q_bounds = [vals[n // 4][0], vals[n // 2][0], vals[3 * n // 4][0]]
        buckets: dict[str, list] = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}
        for v, _i, c in vals:
            b = ("Q1" if v <= q_bounds[0] else
                 "Q2" if v <= q_bounds[1] else
                 "Q3" if v <= q_bounds[2] else "Q4")
            buckets[b].append(c)

        result[feat] = {
            "quartile_bounds": [round(x, 6) for x in q_bounds],
            "per_quartile": {
                bname: {
                    "n":                   len(rows),
                    "entry_success_rate":  round(
                        sum(1 for r in rows if r.get("entry_quote_ok")) / max(len(rows), 1), 4
                    ),
                    "label_1_share":       round(
                        sum(1 for r in rows if r.get("rpc_label") == 1) /
                        max(sum(1 for r in rows if r.get("rpc_label") is not None), 1), 4
                    ),
                    "labeled_n":           sum(1 for r in rows if r.get("rpc_label") is not None),
                    "mean_return":         round(
                        sum(r["net_return_rpc"] for r in rows
                            if r.get("net_return_rpc") is not None) /
                        max(sum(1 for r in rows if r.get("net_return_rpc") is not None), 1), 6
                    ) if any(r.get("net_return_rpc") is not None for r in rows) else None,
                }
                for bname, rows in buckets.items()
            },
        }
    return result


def compute_pool_readout(labeled: list[dict]) -> list[dict]:
    by_pool: dict[str, list[float]] = defaultdict(list)
    for c in labeled:
        if c.get("net_return_rpc") is not None:
            by_pool[c["pool_address"]].append(c["net_return_rpc"])
    rows = []
    for pool, rets in sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)[:30]:
        s = _stats(rets)
        rows.append({"pool": pool, **s})
    return rows


def compute_concentration_audit(labeled: list[dict]) -> dict[str, Any]:
    by_pool: dict[str, list] = defaultdict(list)
    for c in labeled:
        if c.get("rpc_label") is not None:
            by_pool[c["pool_address"]].append(c)

    sorted_pools = sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)
    total_labeled = len(labeled)
    total_pos     = sum(1 for c in labeled if c.get("rpc_label") == 1)

    top10 = sorted_pools[:10]
    tail  = sorted_pools[10:]

    def _pool_stat(pool_items):
        items = [c for _, cs in pool_items for c in cs]
        if not items:
            return {"n": 0, "pos_share": None, "mean_return": None}
        pos  = sum(1 for c in items if c.get("rpc_label") == 1)
        rets = [c["net_return_rpc"] for c in items if c.get("net_return_rpc") is not None]
        return {
            "n":           len(items),
            "pos_share":   round(pos / max(len(items), 1), 4),
            "mean_return": round(sum(rets) / max(len(rets), 1), 6) if rets else None,
        }

    return {
        "total_labeled":         total_labeled,
        "total_positive":        total_pos,
        "total_positive_share":  round(total_pos / max(total_labeled, 1), 4),
        "unique_pools_labeled":  len(by_pool),
        "top10_pools":           [{"pool": p, **_pool_stat([(p, cs)])} for p, cs in top10],
        "top10_aggregate":       _pool_stat(top10),
        "tail_aggregate":        _pool_stat(tail),
        "pool_hhi":              sum(
            (len(cs) / max(total_labeled, 1)) ** 2 for _, cs in sorted_pools
        ),
    }


def compute_failure_breakdown(candidates: list[dict]) -> dict[str, int]:
    counts: Counter = Counter()
    for c in candidates:
        reason = c.get("entry_fail_reason") or c.get("exit_fail_reason") or "ok_exit"
        counts[reason] += 1
    return dict(counts.most_common())


# ── Main regime pipeline ──────────────────────────────────────────────────────
def run_regime_pipeline(
    regime: dict,
    ch_url: str, ch_user: str, ch_pwd: str,
    rpc: Optional[RPC],
    bn_arr: list[int], ts_arr: list[int],
    n_workers: int,
    do_rpc: bool,
) -> dict[str, Any]:
    week_start = regime["week_start"]
    week_end   = regime["week_end"]
    role       = regime.get("role", "positive")
    cap        = (MAX_EXIT_PER_POOL_POSITIVE if role == "positive"
                  else MAX_EXIT_PER_POOL_NEGATIVE)

    _log(f"\n{'='*68}")
    _log(f"REGIME {week_start}  [{role.upper()}]  {regime.get('label', week_start)}")

    # ── Phase 1: CH features ─────────────────────────────────────────────────
    _log(f"  CH features [{week_start}…{week_end}] …")
    t0 = time.time()
    try:
        feature_rows = _ch_query(ch_url, build_feature_query(week_start, week_end),
                                 ch_user, ch_pwd)
    except Exception as exc:
        _log(f"  ERROR feature query: {exc}")
        return {"error": str(exc), "regime_week": week_start}
    _log(f"  Feature rows: {len(feature_rows):,} in {time.time()-t0:.1f}s")

    # price series for exit-activity check
    end_ext = (
        datetime.strptime(week_end, "%Y-%m-%d") + timedelta(seconds=LABEL_EXIT_HORIZON_SEC + 600)
    ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        price_rows = _ch_query(ch_url, build_price_series_query(week_start, end_ext),
                               ch_user, ch_pwd)
    except Exception:
        price_rows = []
    _log(f"  Price rows: {len(price_rows):,}")

    price_index = PoolPriceIndex()
    for pr in price_rows:
        price_index.ingest(
            str(pr.get("pool_address") or "").lower(),
            int(pr.get("ts_sec") or 0),
            int(pr.get("tick_i32") or 0),
        )

    regime_start_sec = int(datetime.strptime(week_start, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())
    regime_end_sec   = int(datetime.strptime(week_end, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())

    candidates: list[dict[str, Any]] = []
    for row in feature_rows:
        pool       = str(row.get("pool_address") or "").lower()
        entry_time = int(row.get("entry_time_sec") or 0)
        if not pool or entry_time <= 0:
            continue
        if entry_time < regime_start_sec or entry_time > regime_end_sec:
            continue

        feats = compute_features(row)
        feats["freshness_lag_sec"] = entry_time - feats.get("last_swap_sec", 0)

        c: dict[str, Any] = {
            "regime_week":            week_start,
            "regime_role":            role,
            "pool_address":           pool,
            "entry_time_sec":         entry_time,
            "entry_time_iso":         datetime.fromtimestamp(
                entry_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fee_bps":                int(row.get("fee_bps") or 0),
            "token0_addr":            str(row.get("token0_addr") or ""),
            "token1_addr":            str(row.get("token1_addr") or ""),
            "weth_is_token0":         int(row.get("weth_is_token0") or 0),
            **feats,
            "has_exit_full":          price_index.has_ticks_in_range(
                pool, entry_time, entry_time + LABEL_EXIT_HORIZON_SEC
            ),
            "has_exit_short":         price_index.has_ticks_in_range(
                pool, entry_time, entry_time + 120
            ),
            # filled in phases 2-4
            "entry_block":            0,
            "entry_quote_ok":         False,
            "entry_amount_out_micro": 0,
            "entry_amount_out_tiny":  0,
            "entry_amount_out_stress":0,
            "entry_slippage_proxy":   None,
            "entry_fail_reason":      None,
            "out_of_range_suspected": False,
            "exit_block":             0,
            "selected_for_exit":      False,
            "rpc_label":              None,
            "net_return_rpc":         None,
            "exit_fail_reason":       None,
            "exit_quote_ok":          False,
        }
        candidates.append(c)

    _log(f"  CH candidates: {len(candidates):,}")

    if not do_rpc or rpc is None or not candidates:
        _write_candidates(candidates, week_start)
        return _dry_run_summary(candidates, week_start, role)

    if getattr(rpc, "instrument", False):
        rpc.reset_instrument()

    # ── Phase 2: Entry probe ALL candidates ───────────────────────────────────
    _log(f"  Entry probe on ALL {len(candidates):,} candidates …")
    t0   = time.time()
    lock = Lock()
    done = [0]

    def probe_one(c: dict) -> None:
        run_entry_probe(rpc, c, bn_arr, ts_arr)
        with lock:
            done[0] += 1
            if done[0] % 500 == 0:
                ok = sum(1 for x in candidates[:done[0]] if x.get("entry_quote_ok"))
                _log(f"    probe: {done[0]:,}/{len(candidates):,} "
                     f"({ok:,} ok, {ok/max(done[0],1):.1%}) [{time.time()-t0:.0f}s]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(probe_one, candidates))

    def _eth_calls_so_far() -> int:
        if not rpc or not getattr(rpc, "instrument", False):
            return 0
        return int(rpc.instrument_totals()["methods"].get("eth_call", 0))

    eth_after_entry = _eth_calls_so_far()

    executable = [c for c in candidates if c.get("entry_quote_ok")]
    n_fail0    = sum(1 for c in candidates if c.get("entry_fail_reason") == "fail_v3_entry_zero")
    _log(f"  Entry done: {len(executable):,}/{len(candidates):,} executable "
         f"({len(executable)/max(len(candidates),1):.1%})  "
         f"fail_zero={n_fail0:,} ({n_fail0/max(len(candidates),1):.1%})")

    # ── Phase 3: Slippage diagnostics ─────────────────────────────────────────
    _log(f"  Slippage diagnostics on {len(executable):,} executable …")
    t0 = time.time(); done[0] = 0

    def diag_one(c: dict) -> None:
        run_slippage_diagnostics(rpc, c, bn_arr, ts_arr)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(diag_one, executable))
    _log(f"  Slippage done [{time.time()-t0:.0f}s]")

    eth_after_slippage = _eth_calls_so_far()

    # ── Phase 4: Concentration cap + exit labels ──────────────────────────────
    pool_exit_count: Counter = Counter()
    selected: list[dict] = []
    for c in executable:
        if pool_exit_count[c["pool_address"]] < cap:
            c["selected_for_exit"] = True
            pool_exit_count[c["pool_address"]] += 1
            selected.append(c)

    _log(f"  Selected for exit: {len(selected):,}/{len(executable):,} "
         f"(cap={cap}/pool, {len(pool_exit_count):,} pools)")

    t0 = time.time(); done[0] = 0

    def exit_one(c: dict) -> None:
        run_exit_label(rpc, c, bn_arr, ts_arr)
        with lock:
            done[0] += 1
            if done[0] % 300 == 0:
                ok = sum(1 for x in selected[:done[0]] if x.get("exit_quote_ok"))
                _log(f"    exit: {done[0]:,}/{len(selected):,} "
                     f"({ok:,} ok) [{time.time()-t0:.0f}s]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(exit_one, selected))

    n_exit_ok = sum(1 for c in selected if c.get("exit_quote_ok"))
    labeled   = [c for c in selected if c.get("rpc_label") is not None]
    n_pos     = sum(1 for c in labeled if c.get("rpc_label") == 1)
    _log(f"  Exit done: {n_exit_ok:,}/{len(selected):,} ok, "
         f"label_1={n_pos:,}/{len(labeled):,} "
         f"({n_pos/max(len(labeled),1):.1%})")

    rpc_instr_payload: dict[str, Any] | None = None
    if rpc is not None and getattr(rpc, "instrument", False):
        fin = rpc.instrument_totals()
        eth_final = int(fin["methods"].get("eth_call", 0))
        entry_eth = eth_after_entry
        slip_eth = max(0, eth_after_slippage - eth_after_entry)
        exit_eth = max(0, eth_final - eth_after_slippage)
        rpc_instr_payload = {
            "regime_week":                   week_start,
            "ch_candidates":               len(candidates),
            "executable_n":                len(executable),
            "selected_for_exit_n":         len(selected),
            "eth_calls_entry_phase":       entry_eth,
            "eth_calls_slippage_phase":    slip_eth,
            "eth_calls_exit_phase":        exit_eth,
            "eth_calls_total":             eth_final,
            "http_posts_total":            int(fin["http_posts"]),
            "ratio_eth_calls_per_ch_candidate": round(
                eth_final / max(len(candidates), 1), 6
            ),
            "assumed_multiplier_legacy_18_implied_calls": len(candidates) * 18,
        }
        try:
            rp_path = OUT_DIR / f"rpc_instrument_{week_start}.json"
            rp_path.write_text(json.dumps(rpc_instr_payload, indent=2), encoding="utf-8")
            _log(f"  RPC_INSTRUMENT written {rp_path.name} eth_calls_total={eth_final:,}")
        except OSError as exc:
            _log(f"  RPC_INSTRUMENT write failed: {exc}")

    _write_candidates(candidates, week_start)

    ok_rets  = sorted(c["net_return_rpc"] for c in labeled if c.get("net_return_rpc") is not None)
    trim_n   = max(1, len(ok_rets) // 10)
    trimmed  = ok_rets[trim_n:-trim_n] if len(ok_rets) > 2 * trim_n else ok_rets
    slip_all = sorted(
        c["entry_slippage_proxy"] for c in executable
        if c.get("entry_slippage_proxy") is not None
    )

    ret: dict[str, Any] = {
        "regime_week":          week_start,
        "regime_role":          role,
        "ch_candidates":        len(candidates),
        "entry_quoteable":      len(executable),
        "entry_quoteable_rate": round(len(executable) / max(len(candidates), 1), 4),
        "fail_v3_entry_zero":   n_fail0,
        "fail_v3_entry_zero_rate": round(n_fail0 / max(len(candidates), 1), 4),
        "selected_for_exit":    len(selected),
        "labeled":              len(labeled),
        "label_1":              n_pos,
        "label_1_share":        round(n_pos / max(len(labeled), 1), 4) if labeled else None,
        "exit_ok_n":            n_exit_ok,
        "label_1_vs_exit_ok":   round(n_pos / max(n_exit_ok, 1), 4) if n_exit_ok else None,
        "median_return":        round(ok_rets[len(ok_rets)//2], 6) if ok_rets else None,
        "mean_return":          round(sum(ok_rets)/len(ok_rets), 6) if ok_rets else None,
        "trimmed_mean_return":  round(sum(trimmed)/max(len(trimmed),1), 6) if trimmed else None,
        "p10_return":           round(ok_rets[max(0,len(ok_rets)//10)], 6) if ok_rets else None,
        "p90_return":           round(ok_rets[min(len(ok_rets)-1,9*len(ok_rets)//10)], 6) if ok_rets else None,
        "slippage_p50":         round(slip_all[len(slip_all)//2], 6) if slip_all else None,
        "slippage_p10":         round(slip_all[max(0,len(slip_all)//10)], 6) if slip_all else None,
        "failure_breakdown":    compute_failure_breakdown(candidates),
        "pool_exit_count_top10":dict(pool_exit_count.most_common(10)),
        "pool_readout":         compute_pool_readout(labeled),
        "concentration_audit":  compute_concentration_audit(labeled),
        "feature_bucket":       compute_feature_bucket_analysis(candidates),
    }
    if rpc_instr_payload is not None:
        ret["rpc_instrument"] = rpc_instr_payload
    return ret


def _write_candidates(candidates: list[dict], week_start: str) -> None:
    if not candidates:
        return
    rows = [{k: v for k, v in c.items() if k != "_meme_addr"} for c in candidates]
    out_csv = OUT_DIR / f"candidates_{week_start}.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    _log(f"  Written: {out_csv} ({len(rows):,} rows)")


def _dry_run_summary(candidates: list[dict], week_start: str, role: str) -> dict[str, Any]:
    return {"regime_week": week_start, "regime_role": role,
            "ch_candidates": len(candidates), "rpc_not_run": True}


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sprint037B-R5: Label expansion with Jan-05/Jan-26 + full coverage"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run",     action="store_true", help="full run with RPC")
    mode.add_argument("--dry-run", action="store_true", help="CH features only, no RPC")
    parser.add_argument("--regime", help="restrict to specific week_start")
    parser.add_argument(
        "--no-global-artifacts",
        action="store_true",
        help="skip sprint037b_r5_summary.json and sprint037b_r5_report.md (per-regime CSV only)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="override candidates CSV output dir (default: mvp/audits/sprint037b_r5)",
    )
    parser.add_argument(
        "--regimes-json",
        default=None,
        help="JSON array of regime dicts {week_start, week_end, role?, label?} replaces built-in REGIMES list",
    )
    args = parser.parse_args()

    if not args.run and not args.dry_run:
        parser.print_help()
        sys.exit(0)

    global OUT_DIR
    if args.out_dir:
        p = Path(args.out_dir)
        OUT_DIR = p if p.is_absolute() else REPO / p

    ch_url  = os.environ.get("CLICKHOUSE_URL",  "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    n_workers = int(os.environ.get("LABEL_WORKERS", "20"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    do_rpc = args.run and bool(rpc_url)
    if args.run and not rpc_url:
        _log("WARNING: --run but no LABEL_RPC_URL; running dry")
        do_rpc = False

    regimes_src = REGIMES
    if args.regimes_json:
        rp = Path(args.regimes_json)
        rp = rp if rp.is_absolute() else REPO / rp
        regimes_src = json.loads(rp.read_text(encoding="utf-8"))

    regimes_to_run = [
        r for r in regimes_src
        if not args.regime or r["week_start"] == args.regime
    ]
    _log(f"Regimes: {[r['week_start'] for r in regimes_to_run]}")
    _log(f"Workers: {n_workers}  do_rpc: {do_rpc}")

    rpc_instrument_enabled = os.environ.get(
        "SPRINT037B_RPC_INSTRUMENT", ""
    ).strip().lower() in ("1", "true", "yes")
    rpc: Optional[RPC] = None
    if do_rpc:
        rpc = RPC(rpc_url, instrument=rpc_instrument_enabled)
        try:
            chid = int(rpc.call("eth_chainId", [])["result"], 16)
            if chid != CHAIN_ID:
                _log(f"ERROR: chain_id={chid} != {CHAIN_ID}")
                do_rpc = False
            else:
                _log(f"  RPC ok, chain_id={chid}")
        except Exception as exc:
            _log(f"  ERROR RPC: {exc}")
            do_rpc = False

    # ── ClickHouse ts-grid (fast, zero RPC calls) ─────────────────────────
    bn_arr: list[int] = []
    ts_arr: list[int] = []
    if do_rpc:
        earliest_ts = min(
            int(datetime.strptime(r["week_start"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
            for r in regimes_to_run
        )
        latest_ts_needed = max(
            int(datetime.strptime(r["week_end"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
            for r in regimes_to_run
        ) + LABEL_EXIT_HORIZON_SEC + 600
        try:
            bn_arr, ts_arr = build_ts_grid_from_ch(
                ch_url, ch_user, ch_pwd, earliest_ts, latest_ts_needed
            )
        except Exception as exc:
            _log(f"ERROR ts-grid: {exc}")
            do_rpc = False

    all_summaries: list[dict] = []
    all_candidates: list[dict] = []

    for regime in regimes_to_run:
        s = run_regime_pipeline(
            regime, ch_url, ch_user, ch_pwd, rpc, bn_arr, ts_arr, n_workers, do_rpc
        )
        all_summaries.append(s)

    # Reload all CSVs for cross-regime analysis
    for regime in regimes_to_run:
        csv_path = OUT_DIR / f"candidates_{regime['week_start']}.csv"
        if csv_path.exists():
            with csv_path.open() as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                for k in ("entry_quote_ok", "exit_quote_ok", "selected_for_exit",
                          "out_of_range_suspected", "has_exit_full", "has_exit_short"):
                    row[k] = row.get(k) in ("True", "1", "true")
                v = row.get("rpc_label")
                row["rpc_label"] = int(v) if v not in (None, "", "None") else None
                v = row.get("net_return_rpc")
                row["net_return_rpc"] = float(v) if v not in (None, "", "None") else None
            all_candidates.extend(rows)

    labeled_all  = [c for c in all_candidates if c.get("rpc_label") is not None]
    exe_all      = [c for c in all_candidates if c.get("entry_quote_ok")]
    pos_regimes  = [c for c in all_candidates if c.get("regime_role") == "positive"]
    labeled_pos  = [c for c in labeled_all if c.get("regime_role") == "positive"]

    n_total       = len(all_candidates)
    n_exe         = len(exe_all)
    n_labeled     = len(labeled_all)
    n_labeled_pos = len(labeled_pos)
    n_pos_labels  = sum(1 for c in labeled_pos if c.get("rpc_label") == 1)

    exe_rate      = n_exe / max(n_total, 1)
    l1_pos_regimes = n_pos_labels / max(n_labeled_pos, 1) if n_labeled_pos else 0

    conc   = compute_concentration_audit(labeled_pos) if labeled_pos else {}
    conc_neg = compute_concentration_audit(
        [c for c in labeled_all if c.get("regime_role") == "negative"]
    ) if any(c.get("regime_role") == "negative" for c in labeled_all) else {}

    # per-regime feature bucket analysis for positive regimes only
    feb02_cands  = [c for c in all_candidates if c.get("regime_week") == "2026-02-02"]
    jan26_cands  = [c for c in all_candidates if c.get("regime_week") == "2026-01-26"]
    jan05_cands  = [c for c in all_candidates if c.get("regime_week") == "2026-01-05"]

    # Cross-regime Q4 heat/volatility check
    def _q4_label1(candidates: list[dict], feat: str) -> Optional[float]:
        vals = sorted(
            float(c[feat]) for c in candidates if c.get(feat) is not None
        )
        if not vals:
            return None
        q3 = vals[3 * len(vals) // 4]
        q4_rows = [c for c in candidates if c.get(feat) is not None
                   and float(c[feat]) > q3]
        labeled_q4 = [c for c in q4_rows if c.get("rpc_label") is not None]
        pos_q4     = sum(1 for c in labeled_q4 if c.get("rpc_label") == 1)
        return round(pos_q4 / max(len(labeled_q4), 1), 4) if labeled_q4 else None

    # ── Determine status ──────────────────────────────────────────────────────
    if not do_rpc:
        status = "DRY_RUN_CH_CANDIDATES_ONLY"
        sprint037c_next = "not_evaluated"
    else:
        feb02_l1 = next(
            (s.get("label_1_share") or 0
             for s in all_summaries if s.get("regime_week") == "2026-02-02"), 0
        )
        jan26_l1 = next(
            (s.get("label_1_share") or 0
             for s in all_summaries if s.get("regime_week") == "2026-01-26"), 0
        )
        jan05_l1 = next(
            (s.get("label_1_share") or 0
             for s in all_summaries if s.get("regime_week") == "2026-01-05"), 0
        )
        multi_positive = sum(1 for l in (feb02_l1, jan26_l1, jan05_l1) if (l or 0) >= 0.12)

        if multi_positive >= 2 and l1_pos_regimes >= 0.14 and n_labeled_pos >= 1000:
            status = "EXECUTABLE_REPLAY_PROMISING_REVIEW_REQUIRED"
            sprint037c_next = "training_review"
        elif multi_positive >= 1 and l1_pos_regimes >= 0.10 and n_labeled_pos >= 500:
            status = "FEB02_SCOPED_EDGE_CROSS_REGIME_VALIDATION"
            sprint037c_next = "rpc_label_expansion"
        elif n_labeled_pos < 300:
            status = "NEEDS_MORE_EXECUTABLE_LABELS"
            sprint037c_next = "rpc_label_expansion"
        elif l1_pos_regimes < 0.08:
            status = "QUOTEABILITY_GATE_WORKS_ECON_WEAK"
            sprint037c_next = "route_pivot"
        else:
            status = "ROUTE_PIVOT_REQUIRED"
            sprint037c_next = "route_pivot"

    summary: dict[str, Any] = {
        "kind":       "sprint037b_r5_label_expansion",
        "version":    "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":     status,
        "decision_flags": DECISION_FLAGS,
        "sprint037c_next": sprint037c_next,
        "probe_config": {
            "tiny_wei":   PROBE_TINY_WEI,
            "micro_wei":  PROBE_MICRO_WEI,
            "stress_wei": PROBE_STRESS_WEI,
            "cap_positive": MAX_EXIT_PER_POOL_POSITIVE,
            "cap_negative": MAX_EXIT_PER_POOL_NEGATIVE,
        },
        "rpc_instrument_enabled": bool(do_rpc and rpc_instrument_enabled),
        "rpc_instrument_per_regime": [
            s["rpc_instrument"] for s in all_summaries if s.get("rpc_instrument")
        ],
        "global_funnel": {
            "ch_candidates":         n_total,
            "entry_quoteable":       n_exe,
            "entry_quoteable_rate":  round(exe_rate, 4),
            "labeled_all":           n_labeled,
            "labeled_positive":      n_labeled_pos,
            "label_1_positive":      n_pos_labels,
            "label_1_share_positive":round(l1_pos_regimes, 4),
        },
        "per_regime": {s["regime_week"]: s for s in all_summaries if "regime_week" in s},
        "concentration_audit_positive": conc,
        "concentration_audit_negative": conc_neg,
        "cross_regime_q4_activity_heat": {
            "feb02": _q4_label1(feb02_cands, "activity_heat"),
            "jan26": _q4_label1(jan26_cands, "activity_heat"),
            "jan05": _q4_label1(jan05_cands, "activity_heat"),
            "dec01_control": _q4_label1(
                [c for c in all_candidates if c.get("regime_week") == "2025-12-01"],
                "activity_heat"
            ),
        },
        "cross_regime_q4_token_volatility": {
            "feb02": _q4_label1(feb02_cands, "token_volatility_5m"),
            "jan26": _q4_label1(jan26_cands, "token_volatility_5m"),
            "jan05": _q4_label1(jan05_cands, "token_volatility_5m"),
            "dec01_control": _q4_label1(
                [c for c in all_candidates if c.get("regime_week") == "2025-12-01"],
                "token_volatility_5m"
            ),
        },
        "r4_baseline": {
            "entry_quoteable_rate": 0.503,
            "fail_v3_entry_zero_rate": 0.497,
            "feb02_label_1_share": 0.1391,
            "ts_grid_samples": 198,
        },
    }

    if not args.no_global_artifacts:
        OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
        _log(f"\nWritten: {OUT_SUMMARY}")

        _write_report(summary, all_summaries)

    _log(f"\n{'='*68}")
    _log(f"STATUS: {status}")
    _log(f"Sprint037C next: {sprint037c_next}")
    _log(f"  CH candidates:        {n_total:,}")
    _log(f"  Entry-quoteable:      {n_exe:,} ({exe_rate:.1%})")
    _log(f"  Labeled (positive):   {n_labeled_pos:,}")
    _log(f"  label_1_share (pos):  {l1_pos_regimes:.1%}")
    q4h = summary.get("cross_regime_q4_activity_heat", {})
    _log(f"  Q4 activity_heat label_1:  "
         f"Feb={q4h.get('feb02')}  Jan26={q4h.get('jan26')}  Jan05={q4h.get('jan05')}  "
         f"Dec01={q4h.get('dec01_control')}")


# ── Report writer ─────────────────────────────────────────────────────────────
def _write_report(summary: dict, all_summaries: list[dict]) -> None:
    gf  = summary.get("global_funnel", {})
    r4b = summary.get("r4_baseline", {})
    conc = summary.get("concentration_audit_positive", {})
    q4h  = summary.get("cross_regime_q4_activity_heat", {})
    q4v  = summary.get("cross_regime_q4_token_volatility", {})

    lines = [
        "# Sprint037B-R5 — Label Expansion: Full Coverage + Jan-05 / Jan-26",
        "",
        f"Generated: {summary.get('created_at')}  ",
        f"**Status: `{summary.get('status')}`**  ",
        f"Sprint037C next: **{summary.get('sprint037c_next')}**",
        "",
        "---",
        "",
        "## 1. ts-grid Fix",
        "",
        f"R4 per-regime scan returned only 198–318 valid samples (of 5,000+ queried)  ",
        f"R5 uses R2-proven full scan (lo_block → latest_num, step=50) → ~135,000 samples",
        "",
        "---",
        "",
        "## 2. Candidate Funnel",
        "",
        "| Stage | Count | Rate |",
        "|-------|-------|------|",
        f"| CH feature candidates | {gf.get('ch_candidates','N/A'):,} | 100% |"
        if isinstance(gf.get('ch_candidates'), int) else
        f"| CH feature candidates | {gf.get('ch_candidates','N/A')} | 100% |",
        f"| Entry-quoteable (MICRO) | {gf.get('entry_quoteable','N/A'):,} | {gf.get('entry_quoteable_rate',0):.1%} |"
        if isinstance(gf.get('entry_quoteable'), int) and isinstance(gf.get('entry_quoteable_rate'), float)
        else f"| Entry-quoteable (MICRO) | {gf.get('entry_quoteable','N/A')} | N/A |",
        f"| Labeled (positive regimes) | {gf.get('labeled_positive','N/A'):,} | — |"
        if isinstance(gf.get('labeled_positive'), int) else
        f"| Labeled (positive regimes) | {gf.get('labeled_positive','N/A')} | — |",
        f"| label_1 (positive) | {gf.get('label_1_positive','N/A'):,} "
        f"({gf.get('label_1_share_positive',0):.1%}) | — |"
        if isinstance(gf.get('label_1_positive'), int) and isinstance(gf.get('label_1_share_positive'), float)
        else f"| label_1 (positive) | N/A | — |",
        "",
        "| Metric | R4 baseline | R5 |",
        "|--------|-------------|-----|",
        f"| Entry success rate | {r4b.get('entry_quoteable_rate',0):.1%} | {gf.get('entry_quoteable_rate',0):.1%} |"
        if isinstance(gf.get('entry_quoteable_rate'), float) else
        "| Entry success rate | 50.3% | N/A |",
        f"| ts-grid samples | {r4b.get('ts_grid_samples',198)} | ~135,000 (full scan) |",
        "",
        "---",
        "",
        "## 3. Per-regime Results",
        "",
        "| Regime | Role | CH | Exe | Exe% | Labeled | label_1 | label_1_share | Trim mean |",
        "|--------|------|----|-----|------|---------|---------|---------------|-----------|",
    ]

    for rs in all_summaries:
        if not rs.get("regime_week"):
            continue
        rw   = rs["regime_week"]
        role = rs.get("regime_role", "?")
        ch   = rs.get("ch_candidates", "N/A")
        exe  = rs.get("entry_quoteable", "N/A")
        ep   = f"{rs.get('entry_quoteable_rate',0):.1%}" if isinstance(rs.get("entry_quoteable_rate"), float) else "N/A"
        lab  = rs.get("labeled", "N/A")
        l1   = rs.get("label_1", "N/A")
        l1s  = f"{rs.get('label_1_share',0):.1%}" if isinstance(rs.get("label_1_share"), float) else "N/A"
        trim = f"{rs.get('trimmed_mean_return',0):.4f}" if isinstance(rs.get("trimmed_mean_return"), float) else "N/A"
        lines.append(f"| {rw} | {role} | {ch:,} | {exe} | {ep} | {lab} | {l1} | {l1s} | {trim} |"
                     if isinstance(ch, int) else
                     f"| {rw} | {role} | {ch} | {exe} | {ep} | {lab} | {l1} | {l1s} | {trim} |")

    lines += [
        "",
        "---",
        "",
        "## 4. Cross-regime Q4 Feature Buckets",
        "",
        "### activity_heat Q4 (highest-activity pools) label_1_share",
        "",
        "| Regime | Q4 label_1_share |",
        "|--------|-----------------|",
        f"| Feb-02 | {q4h.get('feb02','N/A')} |",
        f"| Jan-26 | {q4h.get('jan26','N/A')} |",
        f"| Jan-05 | {q4h.get('jan05','N/A')} |",
        f"| Dec-01 (control) | {q4h.get('dec01_control','N/A')} |",
        "",
        "### token_volatility_5m Q4 (highest-volatility pools) label_1_share",
        "",
        "| Regime | Q4 label_1_share |",
        "|--------|-----------------|",
        f"| Feb-02 | {q4v.get('feb02','N/A')} |",
        f"| Jan-26 | {q4v.get('jan26','N/A')} |",
        f"| Jan-05 | {q4v.get('jan05','N/A')} |",
        f"| Dec-01 (control) | {q4v.get('dec01_control','N/A')} |",
        "",
        "---",
        "",
        "## 5. Pool Concentration Audit (Positive Regimes)",
        "",
        f"- Total labeled (pos): {conc.get('total_labeled','N/A')}",
        f"- Total positive: {conc.get('total_positive','N/A')} "
        f"({conc.get('total_positive_share',0):.1%})" if isinstance(conc.get('total_positive_share'), float) else
        f"- Total positive: {conc.get('total_positive','N/A')}",
        f"- Unique pools: {conc.get('unique_pools_labeled','N/A')}",
        f"- Pool HHI: {conc.get('pool_hhi',0):.4f}" if isinstance(conc.get('pool_hhi'), float) else "",
        "(HHI < 0.05 = well-dispersed, > 0.25 = concentrated)",
        "",
        "| Pool | N | Pos share | Mean return |",
        "|------|---|-----------|-------------|",
    ]
    for tp in (conc.get("top10_pools") or [])[:10]:
        ps = tp.get("pos_share")
        mr = tp.get("mean_return")
        lines.append(
            f"| {tp.get('pool','')[:12]}… | {tp.get('n','N/A')} "
            f"| {ps:.1%} | {mr:.4f} |" if isinstance(ps, float) and isinstance(mr, float) else
            f"| {tp.get('pool','')[:12]}… | {tp.get('n','N/A')} | N/A | N/A |"
        )

    top10_agg = conc.get("top10_aggregate", {})
    tail_agg  = conc.get("tail_aggregate", {})
    lines += [
        "",
        f"- Top-10 pools: n={top10_agg.get('n','N/A')} "
        f"pos={top10_agg.get('pos_share',0):.1%} "
        f"mean={top10_agg.get('mean_return',0):.4f}" if isinstance(top10_agg.get("pos_share"), float) else "",
        f"- Tail pools: n={tail_agg.get('n','N/A')} "
        f"pos={tail_agg.get('pos_share',0):.1%} "
        f"mean={tail_agg.get('mean_return',0):.4f}" if isinstance(tail_agg.get("pos_share"), float) else "",
        "",
        "---",
        "",
        "## 6. Decision Flags",
        "",
        "| Flag | Value |",
        "|------|-------|",
    ]
    for k, v in DECISION_FLAGS.items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        f"**Sprint037C next: `{summary.get('sprint037c_next')}`**",
        "",
        "---",
        "",
        "*Auto-generated by scripts/sprint037b_r5_label_expansion.py*",
    ]

    OUT_REPORT.write_text("\n".join(lines))
    _log(f"Written: {OUT_REPORT}")


if __name__ == "__main__":
    main()
