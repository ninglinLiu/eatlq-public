#!/usr/bin/env python3
"""Sprint041 — 0x59 Evidence Bundle + Matched-Control FIFO Validation
=========================================================================

Single-wallet deep-dive for `0x59c7c832e96d2568bea6db468c1aadcbbda08a52`.

Goals (diagnostics-only):
  1. Frozen reproducible artifacts (round trips, receipts, bytecode hash, gas,
     EIP-7702 proofs, WETH.withdraw logs, FIFO PnL detail).
  2. Matched-control FIFO on the **same pool**, overlapping **time window**,
     **fee tier** fixed by pool, **notional band** stratum vs target median buy.
  3. Caller / funding cluster facts bounded by CH + RPC (no identity claim).
  4. Whitepaper fingerprint aggregates (withdraw rate, gas tiers, holds,
     token universe, bytecode drift).
  5. Explicit non-promotion guards (no training / live / alpha claim).

Optional: `pycryptodome` provides Ethereum Keccak-256 of runtime bytecode.
If unavailable, `runtime_bytecode_keccak256_hex` is null (install hint in audit).

Usage:
  python3 scripts/sprint041_evidence_bundle_matched_control.py --preflight
  LABEL_RPC_URL=... python3 scripts/sprint041_evidence_bundle_matched_control.py --run

Outputs:
  mvp/diagnostics/sprint041_evidence_bundle.json
  mvp/diagnostics/sprint041_matched_control_fifo.csv
  mvp/diagnostics/sprint041_target_fifo_pairs.csv
  mvp/diagnostics/sprint041_weth_withdraw_logs.json
  mvp/audits/sprint041_evidence_bundle_matched_control.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

# ── Frozen flags ──────────────────────────────────────────────────────────────
DECISION_FLAGS: dict[str, Any] = {
    "IDENTITY_CLAIM_ALLOWED":          False,
    "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
    "TRAINING_ALLOWED":               False,
    "LIVE_ALLOWED":                   False,
    "MODEL_PROMOTION_ALLOWED":        False,
    "NO_ALPHA_CLAIM":                 True,
    "FAILED_NO_7D_ALPHA_ACTIVE":      True,
    "SENDER_KEYED_RANKING_TRUSTED":   False,
}

TARGET_EXECUTOR_AUDIT_ONLY = "0x59c7c832e96d2568bea6db468c1aadcbbda08a52"
PRIMARY_POOL_AUDIT_ONLY    = "0x3b4a73fff0618d5b5888526b7a35f905bf0a14e1"
PRIMARY_MEME_TOKEN         = "0x50d2280441372486beecdd328c1854743ebacb07"
KNOWN_CALLER_7702_AUDIT    = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
KNOWN_LEGACY_CALLER_AUDIT  = "0xb3afb6db38a8e72905165c1fbb96772e63560790"

CHAIN_ID = 8453
WETH     = "0x4200000000000000000000000000000000000006"
WETH_WITHDRAW_TOPIC = (
    "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
)

_STABLES = (
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
)
_ROUTERS = (
    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc",
    "0xbe5c953e26c4d16c3c77d4d372b9ad5c48f8cbfc",
    "0xaee2b8d4a154e36f479daece3fb3e6c3c03d396e",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
)

# Bounds
MAX_CONTROL_WALLETS   = 120
MAX_WITHDRAW_SCAN_TX  = 60
MAX_GAS_SAMPLE_TX     = 40
MIN_CONTROL_FIFO_PAIRS = 2
NOTIONAL_BAND_TIERS: list[tuple[float, float]] = [
    (0.45, 2.50),
    (0.25, 4.00),
    (0.10, 10.0),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR  = REPO_ROOT / "mvp" / "diagnostics"
AUDIT_DIR = REPO_ROOT / "mvp" / "audits"

CH_URL  = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
RPC_URL = os.environ.get(
    "LABEL_RPC_URL",
    os.environ.get(
        "EVM_HTTP_URL",
        "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
        "0123456789abcdef0123456789abcdef01234567/",
    ),
)

_CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_bytes_before_external_sort=4000000000"
    "&max_memory_usage=8000000000"
    "&query_plan_max_optimizations_to_apply=0"
)


def _ch_query(sql: str) -> list[dict]:
    url = CH_URL.rstrip("/") + f"/?{_CH_SETTINGS}"
    data = sql.encode()
    req = Request(
        url,
        data=data,
        headers={
            "X-ClickHouse-User": CH_USER,
            "X-ClickHouse-Key": CH_PASS,
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    with urlopen(req, timeout=180) as r:
        body = r.read().decode()
    rows: list[dict] = []
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    for line in lines[1:]:
        vals = line.split("\t")
        rows.append(dict(zip(headers, vals)))
    return rows


def _meme_pools_cte() -> str:
    st = ",".join(f"'{s}'" for s in _STABLES)
    return f"""meme_pools AS (
  SELECT lower(pool_address) AS pa,
    lower(token0_address) AS t0, lower(token1_address) AS t1,
    toUInt32(fee_bps) AS fee_bps,
    multiIf(lower(token0_address)='{WETH.lower()}',1,0) AS weth_is_token0,
    created_block AS pool_created_block
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({st},{WETH!r})
         AND lower(token1_address) IN ({st},{WETH!r}))
)"""


class RPC:
    def __init__(self, url: str):
        self.url = url
        self._id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        ).encode()
        req = Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(req, timeout=45) as r:
            d = json.loads(r.read())
        if "error" in d:
            return None
        return d.get("result")

    def get_tx(self, txh: str) -> dict[str, Any]:
        return self.call("eth_getTransactionByHash", [txh]) or {}

    def get_receipt(self, txh: str) -> dict[str, Any]:
        return self.call("eth_getTransactionReceipt", [txh]) or {}

    def get_code(self, addr: str, block_tag: str = "latest") -> str:
        return self.call("eth_getCode", [addr, block_tag]) or "0x"


def runtime_bytecode_hashes(code_hex: str) -> dict[str, Any]:
    if not code_hex or code_hex in ("0x", "0X"):
        return {"runtime_bytecode_bytes": 0, "runtime_bytecode_keccak256_hex": None}
    raw = bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)
    out: dict[str, Any] = {
        "runtime_bytecode_bytes": len(raw),
        "runtime_bytecode_sha256_hex": "0x" + hashlib.sha256(raw).hexdigest(),
    }
    try:
        from Crypto.Hash import keccak  # type: ignore

        h = keccak.new(digest_bits=256, data=raw)
        out["runtime_bytecode_keccak256_hex"] = "0x" + h.hexdigest()
    except ImportError:
        out["runtime_bytecode_keccak256_hex"] = None
        out["runtime_bytecode_keccak256_note"] = (
            "Install pycryptodome for Ethereum-standard Keccak-256 "
            "(empty-field means SHA256-only fingerprint is still in "
            "runtime_bytecode_sha256_hex)."
        )
    return out


def parse_eip7702_delegation(code_hex: str) -> dict[str, Any]:
    c = (code_hex or "").lower().replace("0x", "")
    if len(c) < 6 + 40:
        return {"is_eip7702_delegation_format": False}
    if not c.startswith("ef0100"):
        return {"is_eip7702_delegation_format": False}
    impl = "0x" + c[6:46]
    return {
        "is_eip7702_delegation_format": True,
        "delegation_designation_implementation_AUDIT_ONLY": impl,
        "eip7702_proof_hex_prefix": "0x" + c[:6],
    }


def query_executor_swaps(w: str) -> list[dict[str, Any]]:
    cte = _meme_pools_cte()
    sql = f"""
WITH {cte}
SELECT
  lower(s.pool_address) AS pool_AUDIT_ONLY,
  if(mp.weth_is_token0=1, mp.t1, mp.t0) AS meme_token_AUDIT_ONLY,
  mp.fee_bps,
  multiIf(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'), 'BUY', 'SELL'
  ) AS side,
  toDateTime(coalesce(s.block_time, s.received_at_ms)) AS block_ts,
  s.block_number,
  lower(s.tx_hash) AS tx_hash,
  multiIf(mp.weth_is_token0=1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) AS weth_amount,
  multiIf(mp.weth_is_token0=0,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))) AS meme_amount
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND lower(s.recipient)='{w.lower()}'
ORDER BY block_ts
FORMAT TSVWithNames"""
    rows = _ch_query(sql)
    for r in rows:
        for k in ("fee_bps", "block_number"):
            if k in r and r[k].isdigit():
                r[k] = int(r[k])
        for k in ("weth_amount", "meme_amount"):
            if k in r:
                try:
                    r[k] = float(r[k])
                except ValueError:
                    r[k] = 0.0
    return rows


def query_pool_window_bounds(pool: str, recip: str) -> dict[str, Any]:
    sql = f"""
SELECT
  min(toDateTime(coalesce(block_time, received_at_ms))) AS t0,
  max(toDateTime(coalesce(block_time, received_at_ms))) AS t1,
  min(block_number) AS bn0,
  max(block_number) AS bn1,
  count() AS n_events
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID} AND removed=0
  AND lower(pool_address)='{pool.lower()}'
  AND lower(recipient)='{recip.lower()}'
FORMAT TSVWithNames"""
    r = _ch_query(sql)
    return r[0] if r else {}


def query_buyers_medians_primary_pool(
    pool: str,
    t0: str,
    t1: str,
    exclude: str,
) -> list[dict[str, Any]]:
    cte = _meme_pools_cte()
    rt = ",".join(f"'{x}'" for x in _ROUTERS)
    sql = f"""
WITH {cte},
buys AS (
  SELECT
    lower(s.recipient) AS recipient_address_AUDIT_ONLY,
    multiIf(mp.weth_is_token0=1,
      abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
      abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) AS weth_in
  FROM eatlq.uniswap_v3_swaps s
  INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
  WHERE s.chain_id={CHAIN_ID} AND s.removed=0
    AND lower(s.pool_address)='{pool.lower()}'
    AND lower(s.recipient) NOT IN ({rt})
    AND lower(s.recipient) != '{exclude.lower()}'
    AND lower(s.sender) != lower(s.recipient)
    AND (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8'
      OR mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
    AND toDateTime(coalesce(s.block_time, s.received_at_ms))
        BETWEEN toDateTime('{t0}') AND toDateTime('{t1}')
)
SELECT recipient_address_AUDIT_ONLY,
  count() AS n_buys,
  quantileTDigest(0.5)(weth_in) AS median_buy_weth,
  avg(weth_in) AS mean_buy_weth
FROM buys
GROUP BY recipient_address_AUDIT_ONLY
HAVING n_buys >= 5
ORDER BY n_buys DESC
LIMIT {MAX_CONTROL_WALLETS * 15}
FORMAT TSVWithNames"""
    rows = _ch_query(sql)
    for r in rows:
        r["n_buys"] = int(r["n_buys"])
        r["median_buy_weth"] = float(r["median_buy_weth"])
        r["mean_buy_weth"] = float(r["mean_buy_weth"])
    return rows


def query_fifo_pool_recipient(
    pool: str, recip: str, t0: str | None = None, t1: str | None = None
) -> list[dict[str, Any]]:
    cte = _meme_pools_cte()
    win = ""
    if t0 and t1:
        win = (
            f"  AND toDateTime(coalesce(s.block_time, s.received_at_ms)) "
            f"BETWEEN toDateTime('{t0}') AND toDateTime('{t1}')\n"
        )
    sql = f"""
WITH {cte}
SELECT
  multiIf(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'), 'BUY', 'SELL'
  ) AS side,
  toDateTime(coalesce(s.block_time, s.received_at_ms)) AS block_ts,
  s.block_number,
  lower(s.tx_hash) AS tx_hash,
  multiIf(mp.weth_is_token0=1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) AS weth_amount,
  multiIf(mp.weth_is_token0=0,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))) AS meme_amount
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND lower(s.recipient)='{recip.lower()}'
  AND lower(s.pool_address)='{pool.lower()}'
{win}ORDER BY block_ts
FORMAT TSVWithNames"""
    rows = _ch_query(sql)
    for r in rows:
        r["block_number"] = int(r["block_number"])
        r["weth_amount"] = float(r["weth_amount"])
        r["meme_amount"] = float(r["meme_amount"])
    return rows


def compute_fifo_pnl(events: list[dict[str, Any]]) -> dict[str, Any]:
    buys: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []

    for ev in sorted(events, key=lambda x: x.get("block_ts", "")):
        side = ev["side"]
        weth = float(ev.get("weth_amount", 0) or 0)
        meme = float(ev.get("meme_amount", 0) or 0)
        ts = ev.get("block_ts", "")

        if side == "BUY" and meme > 0:
            buys.append({"weth_in": weth, "meme_out": meme, "ts": ts})
        elif side == "SELL" and meme > 0 and buys:
            sell_meme = meme
            sell_weth = weth
            cost = 0.0
            consumed = 0.0
            while buys and sell_meme > 1e-9:
                b = buys[0]
                use = min(b["meme_out"], sell_meme)
                fraction = use / max(b["meme_out"], 1e-30)
                cost += b["weth_in"] * fraction
                b["meme_out"] -= use
                b["weth_in"] -= b["weth_in"] * fraction
                sell_meme -= use
                consumed += use
                if b["meme_out"] < 1e-9:
                    buys.pop(0)
            if consumed > 1e-9:
                net = sell_weth - cost
                net_pct = net / max(cost, 1e-18) * 100
                matched.append(
                    {
                        "sell_ts": ts,
                        "cost_weth": round(cost, 8),
                        "revenue_weth": round(sell_weth, 8),
                        "net_weth": round(net, 8),
                        "net_pct": round(net_pct, 4),
                        "label_1": 1 if net > 0 else 0,
                    }
                )

    n = len(matched)
    wins = sum(1 for m in matched if m["label_1"])
    nets = [m["net_pct"] for m in matched]
    if n >= 5:
        k = max(1, n // 10)
        trimmed = sorted(nets)[k:-k]
    else:
        trimmed = nets
    return {
        "n_matched_pairs": n,
        "label_1_share": round(wins / n, 4) if n else None,
        "mean_net_pct": round(sum(nets) / n, 4) if n else None,
        "trimmed_mean_net_pct": round(sum(trimmed) / len(trimmed), 4) if trimmed else None,
        "pairs": matched,
    }


def first_round_trips_per_pool(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First BUY then first SELL-after-BUY per pool (Sprint039B-style 4 RTs)."""
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pool[r["pool_AUDIT_ONLY"]].append(r)
    out: list[dict[str, Any]] = []
    for pool, evs in by_pool.items():
        evs.sort(key=lambda x: (x["block_ts"], x["tx_hash"]))
        first_buy = next((e for e in evs if e["side"] == "BUY"), None)
        if not first_buy:
            continue
        first_sell = next(
            (
                e
                for e in evs
                if e["side"] == "SELL" and str(e["block_ts"]) > str(first_buy["block_ts"])
            ),
            None,
        )
        if not first_sell:
            continue
        hold_sec = _hold_seconds(first_buy["block_ts"], first_sell["block_ts"])
        out.append(
            {
                "pool_AUDIT_ONLY": pool,
                "meme_token_AUDIT_ONLY": first_buy.get("meme_token_AUDIT_ONLY", ""),
                "fee_bps": first_buy.get("fee_bps"),
                "buy_tx_hash": first_buy["tx_hash"],
                "buy_block_number": first_buy["block_number"],
                "buy_block_ts": first_buy["block_ts"],
                "sell_tx_hash": first_sell["tx_hash"],
                "sell_block_number": first_sell["block_number"],
                "sell_block_ts": first_sell["block_ts"],
                "hold_seconds": hold_sec,
                "hold_minutes": round(hold_sec / 60.0, 4) if hold_sec is not None else None,
            }
        )
    out.sort(key=lambda x: str(x["buy_block_ts"]))
    return out


def _hold_seconds(t_buy: str, t_sell: str) -> int | None:
    try:
        # ClickHouse returns "2026-02-14 13:30:21"
        fmt = "%Y-%m-%d %H:%M:%S"
        a = datetime.strptime(str(t_buy)[:19], fmt).replace(tzinfo=timezone.utc)
        b = datetime.strptime(str(t_sell)[:19], fmt).replace(tzinfo=timezone.utc)
        return int((b - a).total_seconds())
    except Exception:
        return None


def collect_weth_withdrawals(
    rpc: RPC, tx_hashes: list[str], limit: int
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for txh in tx_hashes[:limit]:
        rec = rpc.get_receipt(txh)
        if not rec:
            time.sleep(0.03)
            continue
        bn = rec.get("blockNumber")
        for li, log in enumerate(rec.get("logs") or []):
            addr = (log.get("address") or "").lower()
            topics = log.get("topics") or []
            t0 = (topics[0] if topics else "").lower()
            if addr != WETH.lower() or t0 != WETH_WITHDRAW_TOPIC:
                continue
            t1 = topics[1] if len(topics) > 1 else ""
            src = (
                "0x" + t1[-40:]
                if t1 and len(t1) >= 42
                else t1
            )
            found.append(
                {
                    "tx_hash": txh,
                    "log_index": li,
                    "block_number": int(bn, 16) if isinstance(bn, str) and bn.startswith("0x") else bn,
                    "weth_withdraw_src_AUDIT_ONLY": src.lower(),
                    "data": log.get("data"),
                    "topics": topics,
                }
            )
        time.sleep(0.03)
    return found


def gas_distribution(rpc: RPC, tx_hashes: list[str]) -> dict[str, Any]:
    buckets = {"0": 0, "1": 0, "2_to_1e5": 0, "gt_1e5": 0, "missing": 0}
    samples: list[dict[str, Any]] = []
    seen = set()
    for txh in tx_hashes:
        if txh in seen:
            continue
        seen.add(txh)
        tx = rpc.get_tx(txh)
        if not tx:
            buckets["missing"] += 1
            time.sleep(0.02)
            continue
        mp = tx.get("maxPriorityFeePerGas")
        mpv = int(mp, 16) if mp else 0
        if mpv == 0:
            buckets["0"] += 1
        elif mpv == 1:
            buckets["1"] += 1
        elif mpv <= 100_000:
            buckets["2_to_1e5"] += 1
        else:
            buckets["gt_1e5"] += 1
        samples.append(
            {
                "tx_hash": txh,
                "tx_from_AUDIT_ONLY": (tx.get("from") or "").lower(),
                "tx_to_AUDIT_ONLY": (tx.get("to") or "").lower(),
                "maxPriorityFeePerGas_wei": mpv,
            }
        )
        time.sleep(0.02)
        if len(samples) >= MAX_GAS_SAMPLE_TX:
            break
    n = sum(buckets[k] for k in buckets if k != "missing") + buckets["missing"]
    return {
        "n_tx_sampled": len(samples),
        "bucket_counts": buckets,
        "share_priority_0_or_1_wei": round(
            (buckets["0"] + buckets["1"]) / max(n, 1), 4
        ),
        "sample_tx_fields_AUDIT_ONLY": samples[:25],
    }


def tx_to_counterparty_histogram(rpc: RPC, tx_hashes: list[str], topn: int = 12) -> list[dict[str, Any]]:
    ctr: dict[str, int] = defaultdict(int)
    for txh in tx_hashes[:80]:
        tx = rpc.get_tx(txh)
        if tx and tx.get("to"):
            ctr[(tx.get("to") or "").lower()] += 1
        time.sleep(0.02)
    return [
        {"tx_to_AUDIT_ONLY": k, "n_tx": v}
        for k, v in sorted(ctr.items(), key=lambda x: -x[1])[:topn]
    ]


def erc20_funding_hints(addr: str, limit: int = 30) -> dict[str, Any]:
    sql = f"""
SELECT lower(token_address) AS token_AUDIT_ONLY,
  lower(sender) AS erc20_from_AUDIT_ONLY,
  amount_raw,
  toDateTime(coalesce(block_time, received_at_ms)) AS block_ts,
  lower(tx_hash) AS tx_hash
FROM eatlq.erc20_transfers
WHERE chain_id={CHAIN_ID}
  AND lower(recipient)='{addr.lower()}'
ORDER BY block_ts ASC
LIMIT {limit}
FORMAT TSVWithNames"""
    rows = _ch_query(sql)
    return {
        "erc20_table_coverage_note": (
            "eatlq.erc20_transfers may be a partial window on this deployment; "
            "absence of rows is not proof of no inbound funding."
        ),
        "earliest_rows": rows[:10],
        "row_count_returned": len(rows),
    }


def fingerprint_summary(
    all_swaps: list[dict[str, Any]],
    primary_fifo: dict[str, Any],
    withdraw_logs: list[dict[str, Any]],
    gas_info: dict[str, Any],
    bytecode_drift: dict[str, Any],
    round_trips: list[dict[str, Any]],
) -> dict[str, Any]:
    tokens = sorted({r.get("meme_token_AUDIT_ONLY", "") for r in all_swaps if r.get("meme_token_AUDIT_ONLY")})
    pools = sorted({r["pool_AUDIT_ONLY"] for r in all_swaps})
    buys = [r for r in all_swaps if r["side"] == "BUY"]
    w_ins = [float(r["weth_amount"]) for r in buys if float(r.get("weth_amount", 0) or 0) > 0]
    w_ins.sort()
    med = w_ins[len(w_ins) // 2] if w_ins else None
    hold_mins = [
        float(r["hold_minutes"])
        for r in round_trips
        if r.get("hold_minutes") is not None
    ]
    under30 = sum(1 for h in hold_mins if h < 30.0)
    return {
        "distinct_meme_tokens_count": len(tokens),
        "meme_tokens_AUDIT_ONLY_sample": tokens[:25],
        "distinct_pools_count": len(pools),
        "pools_AUDIT_ONLY_sample": pools[:20],
        "all_time_buys": len(buys),
        "buy_weth_median_all_pools": med,
        "buy_weth_min": min(w_ins) if w_ins else None,
        "buy_weth_max": max(w_ins) if w_ins else None,
        "weth_withdraw_tx_count": len({x["tx_hash"] for x in withdraw_logs}),
        "weth_withdraw_log_count": len(withdraw_logs),
        "gas_low_priority_share_0_or_1_wei": gas_info.get("share_priority_0_or_1_wei"),
        "primary_pool_fifo_pairs": primary_fifo.get("n_matched_pairs"),
        "first_rt_per_pool_hold_minutes": hold_mins,
        "share_first_rt_pools_with_hold_under_30min": round(
            under30 / max(len(hold_mins), 1), 4
        ),
        "bytecode_drift_vs_latest": bytecode_drift,
        "whitepaper_alignment_notes": (
            "Aggregates are descriptive only; not a live-executable policy. "
            "30min proxy = share of first round-trips (per pool) with hold < 30 minutes."
        ),
    }


def matched_control_verdict(
    target_stats: dict[str, Any],
    controls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Heuristic: if many controls match target win rate, single-pool illusion risk."""
    t_l1 = target_stats.get("label_1_share")
    t_n = target_stats.get("n_matched_pairs") or 0
    eligible = [
        c
        for c in controls
        if (c.get("n_matched_pairs") or 0) >= MIN_CONTROL_FIFO_PAIRS
        and c.get("label_1_share") is not None
    ]
    if not eligible or t_l1 is None:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "Insufficient control wallets with FIFO pairs",
            "n_control_eligible": len(eligible),
            "n_control_raw_stratified": len(controls),
        }
    if len(eligible) < 5:
        l1s_s = sorted(float(c["label_1_share"]) for c in eligible)
        med = l1s_s[len(l1s_s) // 2]
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "n_control_eligible < 5 (matched-control sample too small for stable comparison)",
            "n_control_eligible": len(eligible),
            "n_control_raw_stratified": len(controls),
            "target_label_1_share": t_l1,
            "control_label_1_share_median_preview": round(med, 4),
        }
    l1s = [float(c["label_1_share"]) for c in eligible]
    l1s.sort()
    median_ctrl = l1s[len(l1s) // 2]
    mean_ctrl = sum(l1s) / len(l1s)
    above = sum(1 for x in l1s if x >= float(t_l1) - 1e-9)
    frac_at_or_above_target = above / len(l1s)
    return {
        "verdict": "LIKELY_POOL_LEVEL" if frac_at_or_above_target >= 0.25 else "TARGET_OUTLIER",
        "target_label_1_share": t_l1,
        "target_n_pairs": t_n,
        "n_control_eligible": len(eligible),
        "control_n_wallets": len(eligible),
        "control_label_1_share_median": round(median_ctrl, 4),
        "control_label_1_share_mean": round(mean_ctrl, 4),
        "fraction_controls_at_or_above_target_share": round(frac_at_or_above_target, 4),
        "interpretation": (
            "If LIKELY_POOL_LEVEL: many non-target wallets show similar FIFO win rates "
            "in the same pool/window/notional stratum — edge may be common to the pool regime. "
            "If TARGET_OUTLIER: target sits above most controls — more consistent with wallet/executor-specific "
            "flow; still not tradeable (`NO_ALPHA_CLAIM=true`)."
        ),
    }


def enrich_round_trips_rpc(rpc: RPC, rts: list[dict[str, Any]]) -> None:
    """Add tx_from / maxPriorityFee / tx_to for each RT leg (bounded RPC)."""
    for rt in rts:
        for leg in ("buy", "sell"):
            key = f"{leg}_tx_hash"
            txh = rt.get(key)
            if not txh:
                continue
            tx = rpc.get_tx(txh)
            time.sleep(0.02)
            if not tx:
                rt[f"{leg}_tx_audit"] = None
                continue
            mp = tx.get("maxPriorityFeePerGas")
            mpv = int(mp, 16) if mp else 0
            rt[f"{leg}_tx_audit"] = {
                "tx_from_AUDIT_ONLY": (tx.get("from") or "").lower(),
                "tx_to_AUDIT_ONLY": (tx.get("to") or "").lower(),
                "maxPriorityFeePerGas_wei": mpv,
            }


def preflight() -> None:
    swaps = query_executor_swaps(TARGET_EXECUTOR_AUDIT_ONLY)
    bounds = query_pool_window_bounds(PRIMARY_POOL_AUDIT_ONLY, TARGET_EXECUTOR_AUDIT_ONLY)
    buyers = query_buyers_medians_primary_pool(
        PRIMARY_POOL_AUDIT_ONLY,
        bounds.get("t0", "1970-01-01 00:00:00"),
        bounds.get("t1", "2099-01-01 00:00:00"),
        TARGET_EXECUTOR_AUDIT_ONLY,
    )
    print("Preflight Sprint041")
    print(f"  executor swaps (all routes): {len(swaps)}")
    print(f"  primary pool window: {bounds}")
    print(f"  candidate control wallets (5+ buys): {len(buyers)}")
    print("Run with --run (RPC bounded).")


def run() -> None:
    rpc = RPC(RPC_URL)
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    swaps = query_executor_swaps(TARGET_EXECUTOR_AUDIT_ONLY)
    round_trips = first_round_trips_per_pool(swaps)
    enrich_round_trips_rpc(rpc, round_trips)

    bounds = query_pool_window_bounds(PRIMARY_POOL_AUDIT_ONLY, TARGET_EXECUTOR_AUDIT_ONLY)
    t0, t1 = bounds.get("t0", ""), bounds.get("t1", "")

    # Target median buy in primary pool window — for notional matching
    pevents = query_fifo_pool_recipient(
        PRIMARY_POOL_AUDIT_ONLY, TARGET_EXECUTOR_AUDIT_ONLY, t0, t1
    )
    pe_window = [e for e in pevents if e["side"] == "BUY"]
    tgt_med = 0.0
    if pe_window:
        wv = sorted(float(e["weth_amount"]) for e in pe_window)
        tgt_med = wv[len(wv) // 2]

    buyers = query_buyers_medians_primary_pool(PRIMARY_POOL_AUDIT_ONLY, t0, t1, TARGET_EXECUTOR_AUDIT_ONLY)
    band_lo = band_hi = 0.0
    band_meta: dict[str, Any] = {}
    control_rows: list[dict[str, Any]] = []

    for lo_m, hi_m in NOTIONAL_BAND_TIERS:
        band_lo, band_hi = tgt_med * lo_m, tgt_med * hi_m
        stratified_tier = [
            b
            for b in buyers
            if band_lo <= float(b["median_buy_weth"]) <= band_hi
        ][:MAX_CONTROL_WALLETS]
        tier_rows: list[dict[str, Any]] = []
        for b in stratified_tier:
            w = b["recipient_address_AUDIT_ONLY"]
            ev = query_fifo_pool_recipient(PRIMARY_POOL_AUDIT_ONLY, w, t0, t1)
            fifo = compute_fifo_pnl(ev)
            if fifo["n_matched_pairs"] < MIN_CONTROL_FIFO_PAIRS:
                continue
            tier_rows.append(
                {
                    "recipient_address_AUDIT_ONLY": w,
                    "cohort": "matched_control",
                    "in_notional_band": True,
                    "n_buys_proxy": b["n_buys"],
                    "median_buy_weth_in_window": round(float(b["median_buy_weth"]), 6),
                    "n_matched_pairs": fifo["n_matched_pairs"],
                    "label_1_share": fifo["label_1_share"],
                    "trimmed_mean_net_pct": fifo["trimmed_mean_net_pct"],
                    "mean_net_pct": fifo["mean_net_pct"],
                }
            )
            time.sleep(0.02)
        band_meta = {
            "tier_mult_low": lo_m,
            "tier_mult_high": hi_m,
            "band_lo_weth": round(band_lo, 8),
            "band_hi_weth": round(band_hi, 8),
            "n_candidates_in_band_before_fifo_filter": len(stratified_tier),
            "n_controls_after_fifo_pair_floor": len(tier_rows),
        }
        control_rows = tier_rows
        if len(tier_rows) >= 5:
            break

    target_fifo = compute_fifo_pnl(pevents)
    target_row = {
        "recipient_address_AUDIT_ONLY": TARGET_EXECUTOR_AUDIT_ONLY.lower(),
        "cohort": "target",
        "in_notional_band": True,
        "n_buys_proxy": len([e for e in pevents if e["side"] == "BUY"]),
        "n_sells_proxy": len([e for e in pevents if e["side"] == "SELL"]),
        "n_matched_pairs": target_fifo["n_matched_pairs"],
        "label_1_share": target_fifo["label_1_share"],
        "trimmed_mean_net_pct": target_fifo["trimmed_mean_net_pct"],
        "mean_net_pct": target_fifo["mean_net_pct"],
        "median_buy_weth_in_window": round(tgt_med, 6),
    }

    verdict_mc = matched_control_verdict(
        {
            "label_1_share": target_fifo["label_1_share"],
            "n_matched_pairs": target_fifo["n_matched_pairs"],
        },
        control_rows,
    )
    uq_tx: list[str] = []
    seen = set()
    for r in reversed(swaps):
        h = r.get("tx_hash", "")
        if h and h not in seen:
            seen.add(h)
            uq_tx.append(h)
    withdraw_logs = collect_weth_withdrawals(rpc, uq_tx, MAX_WITHDRAW_SCAN_TX)
    gas_info = gas_distribution(rpc, uq_tx)

    code_latest = rpc.get_code(TARGET_EXECUTOR_AUDIT_ONLY, "latest")
    first_bn = min((r["block_number"] for r in swaps), default=0)
    code_first = (
        rpc.get_code(TARGET_EXECUTOR_AUDIT_ONLY, hex(int(first_bn))) if first_bn else "0x"
    )
    bc_latest = runtime_bytecode_hashes(code_latest)
    bc_first = runtime_bytecode_hashes(code_first) if code_first not in (None, "0x") else {}
    bytecode_drift = {
        "first_observed_swap_block": int(first_bn),
        "runtime_bytecode_keccak256_latest": bc_latest.get("runtime_bytecode_keccak256_hex"),
        "runtime_bytecode_keccak256_at_first_swap_block": bc_first.get(
            "runtime_bytecode_keccak256_hex"
        ),
        "keccak_equal_first_vs_latest": (
            bc_latest.get("runtime_bytecode_keccak256_hex")
            == bc_first.get("runtime_bytecode_keccak256_hex")
            if bc_first
            else None
        ),
        "runtime_bytecode_sha256_latest": bc_latest.get("runtime_bytecode_sha256_hex"),
    }

    caller7702_code = rpc.get_code(KNOWN_CALLER_7702_AUDIT, "latest")
    legacy_code = rpc.get_code(KNOWN_LEGACY_CALLER_AUDIT, "latest")

    cluster = {
        "executor_contract_AUDIT_ONLY": {
            "address": TARGET_EXECUTOR_AUDIT_ONLY.lower(),
            **runtime_bytecode_hashes(code_latest),
            "code_prefix_latest": (code_latest or "")[:18],
            "is_eip7702_format": parse_eip7702_delegation(code_latest).get(
                "is_eip7702_delegation_format", False
            ),
        },
        "caller_7702_AUDIT_ONLY": {
            "address": KNOWN_CALLER_7702_AUDIT.lower(),
            "eth_getCode_latest_prefix": (caller7702_code or "")[:18],
            **parse_eip7702_delegation(caller7702_code),
            **runtime_bytecode_hashes(caller7702_code),
        },
        "legacy_caller_AUDIT_ONLY": {
            "address": KNOWN_LEGACY_CALLER_AUDIT.lower(),
            "eth_getCode_latest_prefix": (legacy_code or "")[:18],
            **runtime_bytecode_hashes(legacy_code),
        },
        "funding_7702_erc20_hint": erc20_funding_hints(KNOWN_CALLER_7702_AUDIT),
        "funding_legacy_erc20_hint": erc20_funding_hints(KNOWN_LEGACY_CALLER_AUDIT),
        "executor_tx_to_histogram": tx_to_counterparty_histogram(rpc, uq_tx, 15),
        "note": (
            "Profit sink and full multi-wallet graph require expanded indexing / traces "
            "(`Sprint041C cluster graph`). No identity claim."
        ),
    }

    fp = fingerprint_summary(
        swaps,
        target_fifo,
        withdraw_logs,
        gas_info,
        bytecode_drift,
        round_trips,
    )
    fp["primary_pool_hold_minutes_from_round_trips"] = [
        r.get("hold_minutes")
        for r in round_trips
        if PRIMARY_POOL_AUDIT_ONLY.lower()
        in (r.get("pool_AUDIT_ONLY") or "").lower()
    ]

    bundle: dict[str, Any] = {
        "kind": "sprint041_evidence_bundle",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "chain_id": CHAIN_ID,
        "decision_flags": DECISION_FLAGS,
        "matched_window_primary_pool_AUDIT_ONLY": {
            "t0": t0,
            "t1": t1,
            "bounds_meta": bounds,
            "target_median_buy_weth_est": round(tgt_med, 8),
            "notional_match_tier_APPLIED": band_meta,
            "notional_band_abs_weth_final": [round(band_lo, 8), round(band_hi, 8)],
        },
        "round_trips_first_buy_first_sell_per_pool": round_trips,
        "weth_withdrawals_detected": withdraw_logs,
        "fifo_primary_pool_target": {
            k: v for k, v in target_fifo.items() if k != "pairs"
        },
        "fifo_methodology": (
            "FIFO PnL for target + matched controls uses only swaps in the primary pool "
            "whose block_time falls inside [t0,t1] — target's first/last observed swap in "
            "that pool (recipient=executor). This can yield few control wallets with ≥2 "
            "FIFO pairs if most wallets are net buy-only or exit later outside the slice."
        ),
        "matched_control_verdict": verdict_mc,
        "target_row": target_row,
        "cluster_facts_AUDIT_ONLY": cluster,
        "fingerprints": fp,
        "gas_and_tx_audit": gas_info,
        "rpc_configuration_AUDIT_ONLY": {
            "endpoint_description": "Set via LABEL_RPC_URL or EVM_HTTP_URL (never commit URLs)",
        },
        "bytecode": bytecode_drift,
    }

    (DIAG_DIR / "sprint041_evidence_bundle.json").write_text(
        json.dumps(bundle, indent=2), encoding="utf-8"
    )
    (DIAG_DIR / "sprint041_weth_withdraw_logs.json").write_text(
        json.dumps(withdraw_logs, indent=2), encoding="utf-8"
    )

    # FIFO pairs CSV (target)
    pair_path = DIAG_DIR / "sprint041_target_fifo_pairs.csv"
    with pair_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "sell_ts",
                "cost_weth",
                "revenue_weth",
                "net_weth",
                "net_pct",
                "label_1",
            ],
        )
        w.writeheader()
        for row in target_fifo.get("pairs") or []:
            w.writerow(row)

    mc_path = DIAG_DIR / "sprint041_matched_control_fifo.csv"
    with mc_path.open("w", newline="", encoding="utf-8") as f:
        cols = [
            "recipient_address_AUDIT_ONLY",
            "cohort",
            "in_notional_band",
            "n_buys_proxy",
            "median_buy_weth_in_window",
            "n_matched_pairs",
            "label_1_share",
            "trimmed_mean_net_pct",
            "mean_net_pct",
        ]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerow(target_row)
        for row in control_rows:
            w.writerow(row)

    # Audit markdown
    md_lines = [
        "# Sprint041 — Evidence Bundle + Matched-Control FIFO",
        "",
        f"Generated UTC: `{bundle['generated_at_utc']}`",
        "",
        "## Guards",
        "",
        "| Flag | Value |",
        "|------|-------|",
    ]
    for fk, fv in DECISION_FLAGS.items():
        md_lines.append(f"| {fk} | {fv} |")
    md_lines += [
        "",
        "## Repro: round trips (first buy → first sell) per pool",
        "",
        "| Pool | Buy tx | Sell tx | Hold (min) |",
        "|------|--------|---------|------------|",
    ]
    for rt in round_trips:
        md_lines.append(
            f"| `{rt['pool_AUDIT_ONLY'][:10]}..` | `{rt['buy_tx_hash'][:10]}..` | "
            f"`{rt['sell_tx_hash'][:10]}..` | {rt.get('hold_minutes')} |"
        )
    md_lines += [
        "",
        "## Matched control (same pool / time / fee tier implicit in pool / notional band)",
        "",
        f"- Window: `{t0}` → `{t1}`",
        f"- Target median buy WETH (window): `{tgt_med:.6f}`",
        f"- Band (final tier {band_meta.get('tier_mult_low')}×–{band_meta.get('tier_mult_high')}× median): "
        f"`{band_lo:.6f}` – `{band_hi:.6f}`",
        "",
        "### Verdict",
        "",
        f"```json\n{json.dumps(verdict_mc, indent=2)}\n```",
        "",
        "## Single-pool illusion checklist",
        "",
        "If `LIKELY_POOL_LEVEL`, the strong target FIFO may be **consistent with** pool-level "
        "mean reversion / liquidity bounce shared by many wallets. "
        "If `TARGET_OUTLIER`, the executor is **less explained** by the marginal control "
        "distribution — still **not** `IDENTITY_CLAIM_ALLOWED` and not `alpha` (see flags).",
        "",
        "## Cluster (caller / funding) — bounded",
        "",
        f"- EIP-7702 caller code prefix: `{cluster['caller_7702_AUDIT_ONLY'].get('eth_getCode_latest_prefix')}` "
        f"delegation={cluster['caller_7702_AUDIT_ONLY'].get('is_eip7702_delegation_format')}",
        f"- Executor runtime Keccak-256: `{bc_latest.get('runtime_bytecode_keccak256_hex')}`",
    ]
    md_lines += [
        f"- Bytecode drift first swap block vs latest: `{bytecode_drift.get('keccak_equal_first_vs_latest')}`",
        f"- WETH withdraw logs found: `{len(withdraw_logs)}` (see `mvp/diagnostics/sprint041_weth_withdraw_logs.json`)",
        "",
        "## Artifacts",
        "",
        "- `mvp/diagnostics/sprint041_evidence_bundle.json`",
        "- `mvp/diagnostics/sprint041_matched_control_fifo.csv`",
        "- `mvp/diagnostics/sprint041_target_fifo_pairs.csv`",
        "- `mvp/diagnostics/sprint041_weth_withdraw_logs.json`",
        "",
    ]
    (AUDIT_DIR / "sprint041_evidence_bundle_matched_control.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    print("Sprint041 complete.")
    print(f"  round_trips: {len(round_trips)}")
    print(f"  matched_control_eligible: {verdict_mc.get('n_control_eligible', len(control_rows))}")
    print(f"  matched_control_verdict: {verdict_mc.get('verdict')}")
    print(f"  weth_withdraw_logs: {len(withdraw_logs)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if args.preflight:
        preflight()
    elif args.run:
        run()
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
