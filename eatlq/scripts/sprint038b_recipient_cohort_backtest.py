#!/usr/bin/env python3
"""Sprint038B — Recipient cohort Quoter-backed backtest
========================================================

Validates Sprint038A evidence_score cohorts against historical Quoter labels
and actual round-trip returns.  Compared against Sprint037B/R3/R4 baselines.

Cohorts (from mvp/diagnostics/sprint038a_recipient_candidate_summary.csv):
  top3       : evidence_score >= 0.6  (3 wallets)
  top9       : evidence_score >= 0.5  (9 wallets)
  mid        : 0.4 <= score < 0.5    (60 wallets)
  weak       : score < 0.3           (20 random-sampled control wallets)

Label types produced (separated clearly per gate requirements):
  actual_rt  : actual WETH_in/out from CH data (FIFO-matched buy→sell pairs)
  q5m        : Quoter 5-minute horizon  (entry_block + 150 blocks ≈ 5 min)
  q30m       : Quoter 30-minute horizon (entry_block + 900 blocks ≈ 30 min,
               matches Sprint037B/R3/R4 label semantics)

Baselines (frozen from prior sprints):
  R3 ungated:  label_1_share=0.0931  trimmed_mean=-0.3090
  R4 Feb-02:   label_1_share=0.1391  trimmed_mean=-0.1393

Decision flags (frozen — never override from CLI):
  TRAINING_ALLOWED            = false
  LIVE_ALLOWED                = false
  MODEL_PROMOTION_ALLOWED     = false
  FAILED_NO_7D_ALPHA_ACTIVE   = true
  IDENTITY_CLAIM_ALLOWED      = false
  NO_ALPHA_CLAIM              = true
  COHORT_BACKTEST_ALLOWED     = true

Outputs:
  mvp/diagnostics/sprint038b_recipient_cohort_backtest_summary.json
  mvp/diagnostics/sprint038b_recipient_cohort_backtest_rows.csv
  mvp/audits/sprint038b_recipient_cohort_backtest.md

Usage:
  python scripts/sprint038b_recipient_cohort_backtest.py --preflight
  LABEL_RPC_URL=... python scripts/sprint038b_recipient_cohort_backtest.py --run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.request import Request, urlopen

# ── Frozen constants ──────────────────────────────────────────────────────────
WETH           = "0x4200000000000000000000000000000000000006"
CHAIN_ID       = 8453
ROUND_TRIP_COST= 0.006
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
MAX_ABS_NET    = 10.0

# Quoter probe size — matches label-semantics-freeze.md
PROBE_MICRO_WEI = int(5e14)    # 0.0005 ETH

# Block offsets on Base L2 (≈2 s/block)
BLOCKS_5M  = 150   # 5 min  = 300s / 2s
BLOCKS_30M = 900   # 30 min = 1800s / 2s

# ── Baselines (frozen from sprint037b_r3/r4) ──────────────────────────────────
BASELINES = {
    "r3_ungated": {
        "label_1_share": 0.0931,
        "trimmed_mean": -0.3090,
        "source": "sprint037b_r3 ungated tier",
    },
    "r4_feb02": {
        "label_1_share": 0.1391,
        "trimmed_mean": -0.1393,
        "source": "sprint037b_r4 Feb-02 regime",
    },
}

# Verdict thresholds (vs r3_ungated baseline)
_BASE_L1 = BASELINES["r3_ungated"]["label_1_share"]
THRESH_STRONG = _BASE_L1 * 1.50   # ≥ 0.140
THRESH_PROMISING = _BASE_L1 * 1.25  # ≥ 0.116

# ── Per-cohort buy caps (to bound RPC calls) ──────────────────────────────────
CAP_TOP3   = 60   # all buys (top3 has ~59)
CAP_TOP9   = 50   # per wallet
CAP_MID    = 15   # per wallet
CAP_WEAK   = 15   # per wallet
MAX_RPC_CALLS = 5000   # abort preflight if estimate exceeds

# ── Decision flags ────────────────────────────────────────────────────────────
DECISION_FLAGS: dict[str, Any] = {
    "TRAINING_ALLOWED":           False,
    "LIVE_ALLOWED":               False,
    "MODEL_PROMOTION_ALLOWED":    False,
    "FAILED_NO_7D_ALPHA_ACTIVE":  True,
    "IDENTITY_CLAIM_ALLOWED":     False,
    "SENDER_KEYED_RANKING_TRUSTED":False,
    "NO_ALPHA_CLAIM":             True,
    "COHORT_BACKTEST_ALLOWED":    True,
    "SMART_MONEY_GENERAL_LINE_OPEN": False,
}

# ── Stables / exclusions (mirrors sprint037b_r3/r4) ──────────────────────────
_STABLES = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    "0x4a3a6dd60a34bb2aba60d73b4c88f9a0de3e84a",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})
_EXCLUDED = frozenset({
    WETH,
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
})

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO         = Path(__file__).resolve().parents[1]
SPRINT038A_CSV = REPO / "mvp/diagnostics/sprint038a_recipient_candidate_summary.csv"
OUT_SUMMARY  = REPO / "mvp/diagnostics/sprint038b_recipient_cohort_backtest_summary.json"
OUT_CSV      = REPO / "mvp/diagnostics/sprint038b_recipient_cohort_backtest_rows.csv"
OUT_MD       = REPO / "mvp/audits/sprint038b_recipient_cohort_backtest.md"


def _log(msg: str) -> None:
    print(f"[s038b {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── ClickHouse helpers (from sprint037b_r4) ───────────────────────────────────
def _ch_guard(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise ValueError(f"read-only guard: {sql[:60]!r}")
    for b in (" insert ", " alter ", " drop ", " create ", " truncate "):
        if b in f" {s} ":
            raise ValueError(f"forbidden fragment {b!r}")


def _ch_query(
    url: str, sql: str, user: str, pwd: str, timeout: float = 600.0,
) -> list[dict]:
    _ch_guard(sql)
    q = sql.rstrip().rstrip(";")
    if " format " not in q.lower():
        q += " FORMAT JSONEachRow"
    settings = (
        "max_bytes_before_external_group_by=5368709120"
        "&max_bytes_before_external_sort=5368709120"
        "&max_memory_usage=6500000000"
        "&query_plan_max_optimizations_to_apply=0"
    )
    req = Request(f"{url.rstrip('/')}/?{settings}", data=q.encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    req.add_header("X-ClickHouse-User", user)
    req.add_header("X-ClickHouse-Key", pwd)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8").strip()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _stables_sql() -> str:
    return ", ".join(f"'{a}'" for a in sorted(_STABLES))


def _meme_pools_cte() -> str:
    return f"""meme_pools AS (
  SELECT
    lower(pool_address)   AS pa,
    lower(token0_address) AS t0,
    lower(token1_address) AS t1,
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
)"""


def _addr_list_sql(addrs: list[str]) -> str:
    return ", ".join(f"'{a.lower()}'" for a in addrs)


# ── SQL: BUY events per cohort wallet ────────────────────────────────────────
def build_buy_events_query(wallet_addrs: list[str]) -> str:
    """Per-wallet BUY events (WETH → meme), ordered by wallet and timestamp."""
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)                                                          AS recipient_AUDIT_ONLY,
  lower(s.tx_hash)                                                            AS tx_hash,
  lower(s.pool_address)                                                       AS pool_address,
  mp.fee_bps,
  if(mp.weth_is_token0 = 1, mp.t1, mp.t0)                                   AS meme_token,
  mp.weth_is_token0,
  toUInt64(s.block_number)                                                    AS block_number,
  toUnixTimestamp(coalesce(s.block_time, s.received_at_ms))                  AS ts_sec,
  multiIf(
    mp.weth_is_token0 = 1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))) / 1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw))) / 1e18
  )                                                                           AS weth_in_eth
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.recipient) IN ({_addr_list_sql(wallet_addrs)})
  AND lower(s.sender) != lower(s.recipient)
  AND (
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )
ORDER BY recipient_AUDIT_ONLY, ts_sec
"""


# ── SQL: SELL events per cohort wallet (for actual RT pairing) ────────────────
def build_sell_events_query(wallet_addrs: list[str]) -> str:
    """Per-wallet SELL events (meme → WETH), ordered by wallet + pool + ts."""
    return f"""
WITH {_meme_pools_cte()}
SELECT
  lower(s.recipient)                                                          AS recipient_AUDIT_ONLY,
  lower(s.pool_address)                                                       AS pool_address,
  mp.weth_is_token0,
  toUInt64(s.block_number)                                                    AS block_number,
  toUnixTimestamp(coalesce(s.block_time, s.received_at_ms))                  AS ts_sec,
  multiIf(
    mp.weth_is_token0 = 1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))) / 1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw))) / 1e18
  )                                                                           AS weth_out_eth
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address) = mp.pa
WHERE s.chain_id = {CHAIN_ID}
  AND s.removed = 0
  AND lower(s.recipient) IN ({_addr_list_sql(wallet_addrs)})
  AND lower(s.sender) != lower(s.recipient)
  AND NOT (
    (mp.weth_is_token0 = 0 AND lower(substring(s.amount0_raw, 3, 1)) >= '8') OR
    (mp.weth_is_token0 = 1 AND lower(substring(s.amount0_raw, 3, 1)) <  '8')
  )
ORDER BY recipient_AUDIT_ONLY, pool_address, ts_sec
"""


# ── Actual round-trip matching (FIFO per wallet×pool) ────────────────────────
def compute_actual_roundtrips(
    buy_events: list[dict],
    sell_events: list[dict],
) -> list[dict]:
    """FIFO match buys and sells per (wallet, pool).
    Returns one dict per matched round trip with actual_net_return."""
    # Group sells by (rec, pool)
    sell_map: dict[tuple[str, str], deque] = defaultdict(deque)
    for e in sell_events:
        key = (e["recipient_AUDIT_ONLY"], e["pool_address"])
        sell_map[key].append(e)

    results: list[dict] = []
    for be in buy_events:
        key = (be["recipient_AUDIT_ONLY"], be["pool_address"])
        sells = sell_map[key]
        # Find first sell AFTER this buy
        while sells and sells[0]["ts_sec"] <= be["ts_sec"]:
            sells.popleft()
        if not sells:
            continue
        se = sells.popleft()
        weth_in  = float(be.get("weth_in_eth")  or 0.0)
        weth_out = float(se.get("weth_out_eth") or 0.0)
        if weth_in <= 1e-12:
            continue
        hold_sec = int(se["ts_sec"]) - int(be["ts_sec"])
        net, clipped = _sanity_clip(weth_out / weth_in - 1.0 - ROUND_TRIP_COST)
        if clipped:
            continue
        results.append({
            "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
            "pool_address":         be["pool_address"],
            "buy_ts":               int(be["ts_sec"]),
            "sell_ts":              int(se["ts_sec"]),
            "hold_sec":             hold_sec,
            "weth_in_eth":          round(weth_in,  8),
            "weth_out_eth":         round(weth_out, 8),
            "actual_net_return":    round(net, 6),
            "actual_label":         1 if net > 0 else 0,
            "label_type":           "actual_rt",
        })
    return results


# ── RPC infrastructure (reused from sprint037b_r4) ───────────────────────────
class RPC:
    def __init__(self, url: str, timeout: int = 60, max_retries: int = 5):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._lock = Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: list) -> Any:
        body = json.dumps({
            "jsonrpc": "2.0", "method": method,
            "params": params, "id": self._next_id(),
        }).encode()
        req = Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except Exception as e:
                last_exc = e
                code = getattr(e, "code", None)
                if code == 429 or (code and 500 <= code < 600) or code is None:
                    time.sleep(min(0.3 * (2 ** attempt), 8.0))
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("rpc exhausted")


# ── Quoter helpers (from sprint037b_r4) ───────────────────────────────────────
_SEL_QUOTE_V3 = "0xc6a5026a"


def _pad32(h: str) -> str: return h.zfill(64)
def _addr_pad(a: str) -> str:
    return _pad32((a[2:] if a.startswith("0x") else a).lower())
def _uint_pad(n: int) -> str: return _pad32(hex(n)[2:])
def _strip_0x(h: str) -> str: return h[2:] if h.startswith("0x") else h


def _eth_call(rpc: RPC, to: str, data: str, block: int) -> str:
    r = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    if "error" in r:
        raise RuntimeError(r["error"].get("message", "eth_call err"))
    return r["result"]


def call_quoter(
    rpc: RPC, token_in: str, token_out: str, amount_in: int, fee: int, block: int,
) -> int:
    """QuoterV2.quoteExactInputSingle; returns amountOut, 0 on failure."""
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
        raw = _strip_0x(res)
        return int(raw[:64], 16) if len(raw) >= 64 else 0
    except Exception:
        return 0


def _sanity_clip(net: float) -> tuple[float, bool]:
    if net != net:
        return 0.0, True
    if net > MAX_ABS_NET or net < -1.0:
        return max(-1.0, min(MAX_ABS_NET, net)), True
    return net, False


# ── Per-buy Quoter labeling (5m + 30m horizons) ───────────────────────────────
def label_buy_event(
    rpc: RPC,
    be: dict,
    cohort_tag: str,
) -> list[dict]:
    """Run entry + 5m + 30m Quoter on a single BUY event.
    Returns 0–2 labeled rows (5m and/or 30m, only if entry succeeds)."""
    pool     = be["pool_address"]
    meme     = be["meme_token"]
    fee      = int(be["fee_bps"])
    entry_bn = int(be["block_number"])

    entry_out = call_quoter(rpc, WETH, meme, PROBE_MICRO_WEI, fee, entry_bn)
    if entry_out <= 0:
        return [{
            "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
            "pool_address": pool,
            "buy_ts": be["ts_sec"],
            "entry_block": entry_bn,
            "entry_quote_ok": False,
            "entry_fail": "fail_v3_entry_zero",
            "label_type": "q30m",
            "rpc_label": None,
            "net_return": None,
            "cohort": cohort_tag,
            "weth_in_eth": float(be.get("weth_in_eth") or 0),
        }]

    rows = []
    for label_type, exit_offset in (("q5m", BLOCKS_5M), ("q30m", BLOCKS_30M)):
        exit_bn  = entry_bn + exit_offset
        exit_out = call_quoter(rpc, meme, WETH, entry_out, fee, exit_bn)
        if exit_out <= 0:
            rows.append({
                "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
                "pool_address": pool,
                "buy_ts": be["ts_sec"],
                "entry_block": entry_bn,
                "entry_quote_ok": True,
                "entry_fail": None,
                "label_type": label_type,
                "rpc_label": 0,
                "net_return": -1.0,
                "exit_fail": "executable_exit_drained",
                "cohort": cohort_tag,
                "weth_in_eth": float(be.get("weth_in_eth") or 0),
            })
            continue
        net, bogus = _sanity_clip(exit_out / PROBE_MICRO_WEI - 1.0 - ROUND_TRIP_COST)
        if bogus:
            continue
        rows.append({
            "recipient_AUDIT_ONLY": be["recipient_AUDIT_ONLY"],
            "pool_address": pool,
            "buy_ts": be["ts_sec"],
            "entry_block": entry_bn,
            "exit_block": exit_bn,
            "entry_quote_ok": True,
            "entry_fail": None,
            "exit_fail": None,
            "label_type": label_type,
            "rpc_label": 1 if net > 0 else 0,
            "net_return": round(net, 6),
            "cohort": cohort_tag,
            "weth_in_eth": float(be.get("weth_in_eth") or 0),
        })
    return rows


# ── Statistics helpers ────────────────────────────────────────────────────────
def _stats(returns: list[float]) -> dict:
    if not returns:
        return {"n": 0}
    rs  = sorted(returns)
    n   = len(rs)
    pos = sum(x > 0 for x in rs)
    k   = max(1, n // 10)
    tr  = rs[k:-k] if n > 2 * k else rs
    return {
        "n":            n,
        "label_1":      pos,
        "label_1_share":round(pos / n, 4),
        "mean":         round(sum(rs) / n, 6),
        "median":       round(rs[n // 2], 6),
        "trimmed_mean": round(sum(tr) / max(len(tr), 1), 6),
        "p10":          round(rs[max(0, n // 10)], 6),
        "p90":          round(rs[min(n - 1, 9 * n // 10)], 6),
    }


def _concentration(rows: list[dict], key: str) -> dict:
    """Compute top-1 / top-3 concentration by trade count for given key."""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.get(key, "?")] += 1
    total = sum(counts.values())
    if total == 0:
        return {}
    sorted_items = sorted(counts.items(), key=lambda kv: -kv[1])
    top1 = sorted_items[0][1] / total if sorted_items else 0
    top3 = sum(v for _, v in sorted_items[:3]) / total if sorted_items else 0
    return {
        "total_trades":    total,
        "unique":          len(counts),
        "top1_share":      round(top1, 4),
        "top3_share":      round(top3, 4),
        "top3_items":      [k for k, _ in sorted_items[:3]],
    }


def cohort_summary(
    label_rows: list[dict],
    label_type: str,
    actual_rt_rows: list[dict] | None = None,
    n_wallets: int = 0,
    n_total_buys: int = 0,
) -> dict:
    """Aggregate metrics for one cohort + label type."""
    subset = [r for r in label_rows if r.get("label_type") == label_type]
    labeled = [r for r in subset if r.get("rpc_label") is not None]
    entry_ok = [r for r in subset if r.get("entry_quote_ok")]

    returns = [r["net_return"] for r in labeled if r.get("net_return") is not None]
    stats = _stats(returns)

    result: dict[str, Any] = {
        "label_type":         label_type,
        "n_wallets":          n_wallets,
        "n_total_buys":       n_total_buys,
        "n_sampled_events":   len(subset),
        "n_entry_quoteable":  len(entry_ok),
        "n_labeled":          len(labeled),
        "quote_success_rate": round(len(entry_ok) / max(len(subset), 1), 4),
        **stats,
        "wallet_conc":  _concentration(labeled, "recipient_AUDIT_ONLY"),
        "pool_conc":    _concentration(labeled, "pool_address"),
    }

    if actual_rt_rows is not None and label_type == "actual_rt":
        ar = [r["actual_net_return"] for r in actual_rt_rows
              if r.get("actual_net_return") is not None]
        as_ = _stats(ar)
        result.update({
            "n_actual_rt":       len(actual_rt_rows),
            "actual_rt_stats":   as_,
        })
    return result


def verdict(
    top9_q30m: dict,
    top3_q30m: dict,
    baselines: dict,
) -> str:
    """Determine STRONG_PROMISING / PROMISING / WEAK / FAIL / INCONCLUSIVE."""
    bl = baselines["r3_ungated"]["label_1_share"]
    l1  = top9_q30m.get("label_1_share", 0) or 0
    n   = top9_q30m.get("n_labeled", 0) or 0
    tm  = top9_q30m.get("trimmed_mean", -99) or -99
    w1  = (top9_q30m.get("wallet_conc", {}).get("top1_share", 1.0)) or 1.0

    if n < 10:
        return "INCONCLUSIVE"
    if l1 >= THRESH_STRONG and tm > BASELINES["r3_ungated"]["trimmed_mean"] and w1 <= 0.35:
        return "STRONG_PROMISING"
    if l1 >= THRESH_PROMISING:
        return "PROMISING"
    if l1 > bl:
        return "WEAK"
    return "FAIL"


# ── Cohort loading ────────────────────────────────────────────────────────────
def load_cohorts(csv_path: Path, rng_seed: int = 42) -> dict[str, list[dict]]:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    scored = [r for r in rows if not r["exclusion_reason_AUDIT_ONLY"]]
    scored.sort(key=lambda r: -float(r["evidence_score"]))

    top3   = [r for r in scored if float(r["evidence_score"]) >= 0.6]
    top6e  = [r for r in scored if 0.5 <= float(r["evidence_score"]) < 0.6]
    top9   = top3 + top6e
    mid    = [r for r in scored if 0.4 <= float(r["evidence_score"]) < 0.5]
    weak_pool = [r for r in scored if float(r["evidence_score"]) < 0.3]
    random.seed(rng_seed)
    weak = random.sample(weak_pool, min(20, len(weak_pool)))
    return {
        "top3":    top3,
        "top9":    top9,
        "mid":     mid,
        "weak":    weak,
    }


def apply_buy_cap(
    events: list[dict],
    cohort_wallets: list[dict],
    cap_per_wallet: int,
) -> list[dict]:
    """Keep at most cap_per_wallet buy events per wallet, newest first."""
    addrs = {r["recipient_address_AUDIT_ONLY"] for r in cohort_wallets}
    by_addr: dict[str, list] = defaultdict(list)
    for e in events:
        if e["recipient_AUDIT_ONLY"] in addrs:
            by_addr[e["recipient_AUDIT_ONLY"]].append(e)
    result = []
    for addr, evts in by_addr.items():
        evts.sort(key=lambda x: -int(x.get("ts_sec", 0)))  # newest first
        result.extend(evts[:cap_per_wallet])
    return result


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    m = ap.add_mutually_exclusive_group(required=True)
    m.add_argument("--preflight", action="store_true")
    m.add_argument("--run",       action="store_true")
    args = ap.parse_args()

    ch_url  = os.environ.get("CLICKHOUSE_URL",      "http://172.18.0.6:8123/")
    ch_user = os.environ.get("CLICKHOUSE_USER",     "default")
    ch_pwd  = os.environ.get("CLICKHOUSE_PASSWORD", "")
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")

    if args.run and not rpc_url:
        print("ERROR: --run requires LABEL_RPC_URL or EVM_HTTP_URL", file=sys.stderr)
        return 1

    if not SPRINT038A_CSV.is_file():
        print(f"ERROR: {SPRINT038A_CSV} not found. Run sprint038a first.", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    _log("=== Sprint038B recipient cohort backtest ===")
    _log(f"Mode: {'PREFLIGHT' if args.preflight else 'FULL RUN'}")

    # ── Load cohorts ──────────────────────────────────────────────────────────
    cohorts = load_cohorts(SPRINT038A_CSV)
    all_wallets = {r["recipient_address_AUDIT_ONLY"]
                   for c in cohorts.values() for r in c}
    _log(f"Cohorts loaded: top3={len(cohorts['top3'])} top9={len(cohorts['top9'])} "
         f"mid={len(cohorts['mid'])} weak={len(cohorts['weak'])} "
         f"unique_wallets={len(all_wallets)}")

    # ── CH queries ────────────────────────────────────────────────────────────
    all_addr_list = list(all_wallets)
    _log(f"CH query: BUY events for {len(all_addr_list)} wallets…")
    buy_rows = _ch_query(ch_url, build_buy_events_query(all_addr_list), ch_user, ch_pwd)
    _log(f"  BUY events: {len(buy_rows)}")

    _log("CH query: SELL events…")
    sell_rows = _ch_query(ch_url, build_sell_events_query(all_addr_list), ch_user, ch_pwd)
    _log(f"  SELL events: {len(sell_rows)}")

    # ── Apply per-wallet caps ─────────────────────────────────────────────────
    CAPS = {"top3": CAP_TOP3, "top9": CAP_TOP9, "mid": CAP_MID, "weak": CAP_WEAK}
    capped: dict[str, list[dict]] = {}
    for tag, wlist in cohorts.items():
        capped[tag] = apply_buy_cap(buy_rows, wlist, CAPS[tag])
        _log(f"  {tag}: {len(capped[tag])} buy events after cap={CAPS[tag]}")

    # ── Estimate RPC calls ────────────────────────────────────────────────────
    # Each buy event: 1 entry + up to 2 exits = up to 3 calls
    # weak is subset of others-like buys but cap applied
    total_est = sum(len(v) * 3 for v in capped.values())
    # Subtract double-counting (top3 ⊂ top9): already handled since capped separately
    _log(f"Estimated RPC calls: ~{total_est} (max budget: {MAX_RPC_CALLS})")

    if total_est > MAX_RPC_CALLS:
        _log(f"ABORT: estimated calls {total_est} > {MAX_RPC_CALLS}")
        _log("  Reduce caps: CAP_MID or CAP_WEAK")
        return 2

    if args.preflight:
        _log("Preflight complete — no RPC calls made")
        for tag in ("top3", "top9", "mid", "weak"):
            n_buys = len(capped[tag])
            _log(f"  {tag}: {n_buys} buy events → ~{n_buys*3} RPC calls")
        _log(f"Total: {total_est} calls / {MAX_RPC_CALLS} budget")
        return 0

    # ── Actual round-trip matching (CH data only, no RPC) ────────────────────
    _log("Computing actual round-trip returns from CH data…")
    actual_rt_by_cohort: dict[str, list[dict]] = {}
    for tag, wlist in cohorts.items():
        addrs = {r["recipient_address_AUDIT_ONLY"] for r in wlist}
        buys_c  = [e for e in buy_rows  if e["recipient_AUDIT_ONLY"] in addrs]
        sells_c = [e for e in sell_rows if e["recipient_AUDIT_ONLY"] in addrs]
        rt = compute_actual_roundtrips(buys_c, sells_c)
        # Annotate cohort tag
        for r in rt:
            r["cohort"] = tag
        actual_rt_by_cohort[tag] = rt
        pos = sum(1 for r in rt if r.get("actual_label") == 1)
        _log(f"  {tag}: {len(rt)} actual round trips, {pos} positive "
             f"({pos/max(len(rt),1)*100:.0f}%)")

    # ── RPC: Quoter labels (threaded, 20 workers) ─────────────────────────────
    _log("Running Quoter label pipeline (5m + 30m horizons)…")
    rpc = RPC(rpc_url)
    all_label_rows: list[dict] = []
    rpc_counter = 0
    lock = Lock()

    def _label_one(args_tuple: tuple) -> list[dict]:
        nonlocal rpc_counter
        tag, be = args_tuple
        result = label_buy_event(rpc, be, tag)
        with lock:
            rpc_counter += 1
            if rpc_counter % 100 == 0:
                _log(f"  RPC: {rpc_counter} buy events processed")
        return result

    work_items = []
    for tag in ("top3", "top9", "mid", "weak"):
        for be in capped[tag]:
            work_items.append((tag, be))

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = {pool.submit(_label_one, item): item for item in work_items}
        for fut in as_completed(futs):
            try:
                rows = fut.result()
                all_label_rows.extend(rows)
            except Exception as exc:
                _log(f"  WARNING worker error: {exc}")

    _log(f"RPC complete: {len(all_label_rows)} label rows across all cohorts/horizons")

    # ── Cohort summaries ──────────────────────────────────────────────────────
    _log("Aggregating cohort summaries…")
    summaries: dict[str, dict] = {}
    for tag, wlist in cohorts.items():
        tag_rows = [r for r in all_label_rows if r.get("cohort") == tag]
        art = actual_rt_by_cohort.get(tag, [])
        tag_buys_all = sum(int(r["n_buys"]) for r in wlist)
        s = {
            "cohort":    tag,
            "n_wallets": len(wlist),
            "actual_rt": cohort_summary([], "actual_rt",
                                        actual_rt_rows=art,
                                        n_wallets=len(wlist),
                                        n_total_buys=tag_buys_all),
            "q5m":  cohort_summary(tag_rows, "q5m",
                                   n_wallets=len(wlist),
                                   n_total_buys=tag_buys_all),
            "q30m": cohort_summary(tag_rows, "q30m",
                                   n_wallets=len(wlist),
                                   n_total_buys=tag_buys_all),
        }
        summaries[tag] = s
        l1_30  = s["q30m"].get("label_1_share", 0) or 0
        l1_5   = s["q5m"].get("label_1_share", 0) or 0
        l1_art = s["actual_rt"].get("actual_rt_stats", {}).get("label_1_share", "N/A")
        _log(f"  {tag}: q30m_label1={l1_30:.3f} q5m_label1={l1_5:.3f} art_label1={l1_art}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    top9_q30m = summaries.get("top9", {}).get("q30m", {})
    top3_q30m = summaries.get("top3", {}).get("q30m", {})
    final_verdict = verdict(top9_q30m, top3_q30m, BASELINES)
    _log(f"Verdict: {final_verdict}")

    # ── Concentration checks ──────────────────────────────────────────────────
    def _conc_flag(cohort_tag: str) -> dict:
        s = summaries.get(cohort_tag, {})
        q30 = s.get("q30m", {})
        w1  = (q30.get("wallet_conc", {}) or {}).get("top1_share", 0) or 0
        p1  = (q30.get("pool_conc",   {}) or {}).get("top1_share", 0) or 0
        return {
            "wallet_top1_share": round(w1, 4),
            "pool_top1_share":   round(p1, 4),
            "concentration_risk": w1 > 0.5 or p1 > 0.5,
        }

    # ── Outputs ───────────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t0
    full_summary = {
        "kind":          "sprint038b_recipient_cohort_backtest",
        "schema_version":"sprint038b_v1",
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "elapsed_sec":   round(elapsed, 1),
        "decision_flags":DECISION_FLAGS,
        "baselines":     BASELINES,
        "verdict":       final_verdict,
        "verdict_thresholds": {
            "STRONG_PROMISING": round(THRESH_STRONG, 4),
            "PROMISING":        round(THRESH_PROMISING, 4),
            "basis":            "vs R3 ungated label_1_share=0.0931",
        },
        "cohort_sizes": {
            tag: {"n_wallets": len(wlist), "n_sampled_buys": len(capped.get(tag, []))}
            for tag, wlist in cohorts.items()
        },
        "summaries":     summaries,
        "concentration_flags": {
            tag: _conc_flag(tag) for tag in ("top3", "top9", "mid")
        },
        "rpc_config": {
            "probe_micro_wei":  PROBE_MICRO_WEI,
            "blocks_5m":        BLOCKS_5M,
            "blocks_30m":       BLOCKS_30M,
            "round_trip_cost":  ROUND_TRIP_COST,
        },
    }

    OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    OUT_SUMMARY.write_text(json.dumps(full_summary, indent=2), encoding="utf-8")
    _log(f"JSON written: {OUT_SUMMARY}")

    # ── CSV output (all label rows + actual RT) ───────────────────────────────
    all_csv_rows = []
    for r in all_label_rows:
        all_csv_rows.append({
            "recipient_AUDIT_ONLY": r.get("recipient_AUDIT_ONLY", ""),
            "cohort":               r.get("cohort", ""),
            "label_type":           r.get("label_type", ""),
            "pool_address":         r.get("pool_address", ""),
            "buy_ts":               r.get("buy_ts", ""),
            "entry_block":          r.get("entry_block", ""),
            "entry_quote_ok":       r.get("entry_quote_ok", ""),
            "rpc_label":            r.get("rpc_label", ""),
            "net_return":           r.get("net_return", ""),
            "entry_fail":           r.get("entry_fail", ""),
            "exit_fail":            r.get("exit_fail", ""),
            "weth_in_eth":          r.get("weth_in_eth", ""),
        })
    for r in [rr for rtlist in actual_rt_by_cohort.values() for rr in rtlist]:
        all_csv_rows.append({
            "recipient_AUDIT_ONLY": r.get("recipient_AUDIT_ONLY", ""),
            "cohort":               r.get("cohort", ""),
            "label_type":           "actual_rt",
            "pool_address":         r.get("pool_address", ""),
            "buy_ts":               r.get("buy_ts", ""),
            "entry_block":          "",
            "entry_quote_ok":       True,
            "rpc_label":            r.get("actual_label", ""),
            "net_return":           r.get("actual_net_return", ""),
            "entry_fail":           "",
            "exit_fail":            "",
            "weth_in_eth":          r.get("weth_in_eth", ""),
        })

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if all_csv_rows:
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_csv_rows[0].keys()),
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(all_csv_rows)
    _log(f"CSV written: {OUT_CSV} ({len(all_csv_rows)} rows)")

    # ── Markdown audit ────────────────────────────────────────────────────────
    _write_md(full_summary)
    _log(f"Markdown written: {OUT_MD}")

    _log(f"=== Sprint038B complete in {elapsed:.1f}s ===")
    _log(f"Verdict: {final_verdict}")
    return 0


def _write_md(d: dict) -> None:
    summaries = d.get("summaries", {})
    verdict_str = d.get("verdict", "INCONCLUSIVE")
    bl = BASELINES["r3_ungated"]

    def _cohort_row(tag: str, ltype: str) -> str:
        s = (summaries.get(tag) or {}).get(ltype) or {}
        n_w = s.get("n_wallets", 0)
        n_l = s.get("n_labeled", 0) or (s.get("actual_rt_stats") or {}).get("n", 0)
        l1  = s.get("label_1_share") or (s.get("actual_rt_stats") or {}).get("label_1_share") or 0
        tm  = s.get("trimmed_mean") or (s.get("actual_rt_stats") or {}).get("trimmed_mean") or 0
        qsr = s.get("quote_success_rate", "N/A")
        w1  = (s.get("wallet_conc") or {}).get("top1_share", "—")
        p1  = (s.get("pool_conc") or {}).get("top1_share", "—")
        vs_bl = ""
        if isinstance(l1, float) and l1 > 0:
            ratio = l1 / bl["label_1_share"]
            vs_bl = f"×{ratio:.2f}baseline"
        return (f"| {tag:6} | {ltype:10} | {n_w:>8} | {n_l:>8} | "
                f"{l1!s:>10} | {tm!s:>12} | {qsr!s:>8} | {w1!s:>10} | {p1!s:>10} | {vs_bl} |")

    lines = [
        "# Sprint038B — Recipient Cohort Quoter Backtest",
        "",
        f"**Generated:** {d.get('created_at', '')[:16]}Z  ",
        f"**Verdict: `{verdict_str}`**",
        "",
        "## Decision Flags",
        "",
        "```",
    ]
    for k, v in DECISION_FLAGS.items():
        lines.append(f"{k:45s} = {v}")
    lines += [
        "```",
        "",
        "## Baselines",
        "",
        f"- **R3 ungated** (sprint037b_r3): label_1_share={bl['label_1_share']:.4f}  trimmed_mean={bl['trimmed_mean']:.4f}",
        f"- **R4 Feb-02** (sprint037b_r4):  label_1_share={BASELINES['r4_feb02']['label_1_share']:.4f}  trimmed_mean={BASELINES['r4_feb02']['trimmed_mean']:.4f}",
        f"- **STRONG_PROMISING** gate: label_1_share >= {d['verdict_thresholds']['STRONG_PROMISING']:.4f}",
        f"- **PROMISING** gate:        label_1_share >= {d['verdict_thresholds']['PROMISING']:.4f}",
        "",
        "## Cohort Comparison Table",
        "",
        "| cohort | label_type | n_wallets | n_labeled | label_1_share | trimmed_mean | qsr | top1_wallet% | top1_pool% | vs_baseline |",
        "|--------|-----------|-----------|-----------|---------------|--------------|-----|-------------|-----------|-------------|",
    ]
    for tag in ("top3", "top9", "mid", "weak"):
        for lt in ("q5m", "q30m", "actual_rt"):
            lines.append(_cohort_row(tag, lt))

    lines += [
        "",
        "## Verdict Analysis",
        "",
        f"### {verdict_str}",
        "",
    ]

    top9_q30m = (summaries.get("top9") or {}).get("q30m") or {}
    l1_top9 = top9_q30m.get("label_1_share", 0) or 0
    tm_top9 = top9_q30m.get("trimmed_mean", 0) or 0
    n_top9  = top9_q30m.get("n_labeled", 0) or 0
    lines += [
        f"- **top-9 q30m:** n_labeled={n_top9}  label_1_share={l1_top9:.4f}  trimmed_mean={tm_top9:.4f}",
        f"- **R3 baseline:** label_1_share={bl['label_1_share']:.4f}  trimmed_mean={bl['trimmed_mean']:.4f}",
        f"- **Improvement:** {l1_top9/max(bl['label_1_share'],1e-9):.2f}× baseline",
        "",
    ]

    # Concentration flags
    conc = d.get("concentration_flags", {})
    lines += ["## Concentration / Leakage Risks", ""]
    for tag in ("top3", "top9", "mid"):
        cf = conc.get(tag, {})
        flag = "⚠ CONCENTRATION_RISK" if cf.get("concentration_risk") else "OK"
        lines.append(f"- {tag}: wallet_top1={cf.get('wallet_top1_share','?')}  "
                     f"pool_top1={cf.get('pool_top1_share','?')}  {flag}")

    lines += [
        "",
        "## Guard Statements",
        "",
        "1. evidence_score and label outcomes are NOT alpha claims.",
        "2. No candidate address may be added to wallet:smart_money / wallet:sniper tiers.",
        "3. IDENTITY_CLAIM_ALLOWED = false — labels do not identify any individual.",
        "4. FAILED_NO_7D_ALPHA_ACTIVE = true — this backtest does not supersede the guard.",
        "5. F10=0 is a gas-alignment diagnostic only, not an exclusion criterion.",
        "",
        f"*Sprint038B — sprint038b_v1  elapsed={d.get('elapsed_sec','?')}s*",
    ]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
