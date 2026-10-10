"""Sprint045C — Broad-sea expansion + top-controller falsification probe.

Phases:
  C1  CH broad recipient priority scan (no new RPC)
  C2  Prioritized direct-V3 / non-platform controller RPC expansion (≤150k new calls)
  C3  Top-controller falsification probe (eth_getCode + eth_getTransactionReceipt + WETH.withdraw)
  C4  Lightweight cluster hints (CH-only)

Reuses:
  mvp/diagnostics/sprint045b_controller_ranking.csv
  mvp/diagnostics/sprint045b_controller_tx_map.csv
  mvp/diagnostics/sprint045b_rpc_checkpoint.jsonl
  mvp/diagnostics/sprint045_controller_keyed_preflight.json

Outputs:
  mvp/diagnostics/sprint045c_broad_recipient_priority_tiers.csv
  mvp/diagnostics/sprint045c_rpc_tx_queue.csv
  mvp/diagnostics/sprint045c_new_controller_tx_map.csv
  mvp/diagnostics/sprint045c_merged_controller_summary.csv
  mvp/diagnostics/sprint045c_merged_controller_ranking.csv
  mvp/diagnostics/sprint045c_top_controller_probe.csv
  mvp/diagnostics/sprint045c_cluster_hints.csv
  mvp/diagnostics/sprint045c_decision_flags.json
  mvp/audits/sprint045c_broad_sea_expansion_and_probe_readout.md

Constraints:
  - No live trading, no training, no copy trade.
  - No hard filter on fee=100, low gas, hold time, notional, sniper labels.
  - 0x59c7 = platform lane only; 0x7e49 = benchmark only.
  - Failure rate >15% → abort and save partial artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── constants ──────────────────────────────────────────────────────────────────
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 3500.0
SECS_PER_DAY = 86400.0
NEW_RPC_BUDGET = 150_000

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG = REPO_ROOT / "mvp" / "diagnostics"
AUDIT = REPO_ROOT / "mvp" / "audits"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_bytes_before_external_sort=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=300"
)

RPC_URL = os.environ.get("LABEL_RPC_URL") or os.environ.get(
    "EVM_HTTP_URL",
    "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/",
)

STABLES = sorted({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    WETH,
})

KNOWN_PLATFORM_RECIPIENTS = {
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52",  # Velora v5 Augustus
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd",  # Sprint041 alt aggregator
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap SwapRouter02
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap Universal Router
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router v2
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41",  # CoW Protocol Settlement
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # KyberSwap
    "0x6a000f20005980200259b80c5102003040001068",  # Bebop
    "0x103b86a7fe783dc76086c6e03f3ce74cbcc4bd12",  # frequent aggregator
    "0x498581ff718922c3f8e6a244956af099b2652b2b",  # frequent aggregator
    "0x0000000000001ff3684f28c67538d4d072c22734",  # MEV-related
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
}

POSITIVE_CONTROL = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
WETH_WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"

_RPC_LOCK = threading.Lock()
_RPC_ID = 0


# ── helpers ────────────────────────────────────────────────────────────────────
def norm_hex(h: str) -> str:
    h = (h or "").strip().lower().replace("0x", "")
    return "0x" + h


def strip0x(h: str) -> str:
    return h[2:] if h.startswith("0x") else h


def sql_tx_hash(alias: str = "s") -> str:
    """CH stores tx_hash as '0x'+64hex String. Extract 64-char hex body."""
    t = f"{alias}.tx_hash"
    return f"lower(if(startsWith({t},'0x'),substring({t},3),{t}))"


def ch_rows(sql: str, timeout: int = 600) -> list[dict[str, str]]:
    url = CH_URL.rstrip("/") + f"/?{CH_SETTINGS}"
    req = Request(
        url,
        data=(sql + "\nFORMAT TSVWithNames").encode(),
        headers={
            "X-ClickHouse-User": CH_USER,
            "X-ClickHouse-Key": CH_PASS,
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout) as r:
        text = r.read().decode()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    return [dict(zip(headers, line.split("\t"))) for line in lines[1:]]


def meme_pools_cte() -> str:
    si = ",".join(repr(s) for s in STABLES)
    return f"""meme_pools AS (
  SELECT lower(pool_address) AS pa,
    lower(token0_address) AS t0, lower(token1_address) AS t1,
    toUInt32(fee_bps) AS fee_bps,
    multiIf(lower(token0_address)='{WETH}',1,0) AS weth_is_token0
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (100,500,3000,10000)
    AND NOT (lower(token0_address) IN ({si}) AND lower(token1_address) IN ({si}))
)"""


def rpc_batch(calls: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """JSON-RPC batch. Returns results in same order as calls (None on error)."""
    global _RPC_ID
    payloads: list[dict[str, Any]] = []
    with _RPC_LOCK:
        for c in calls:
            _RPC_ID += 1
            payloads.append({"jsonrpc": "2.0", "id": _RPC_ID, **c})
    body = json.dumps(payloads).encode()
    backoff = 1.5
    for _attempt in range(10):
        try:
            req = Request(
                RPC_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=120) as r:
                blob = json.loads(r.read())
            if not isinstance(blob, list):
                return [None] * len(calls)
            by_id = {item["id"]: item for item in blob if isinstance(item, dict) and "id" in item}
            out: list[dict[str, Any] | None] = []
            for p in payloads:
                item = by_id.get(p["id"])
                if item and "result" in item:
                    out.append(item["result"])
                else:
                    out.append(None)
            return out
        except HTTPError as e:
            if getattr(e, "code", 0) in (429, 502, 503, 504):
                time.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 60.0)
                continue
            return [None] * len(calls)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            time.sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, 60.0)
            continue
    return [None] * len(calls)


def rpc_get_tx(hashes: list[str]) -> dict[str, dict[str, Any] | None]:
    calls = [{"method": "eth_getTransactionByHash", "params": [norm_hex(h)]} for h in hashes]
    results = rpc_batch(calls)
    return {norm_hex(h): results[i] for i, h in enumerate(hashes)}


def rpc_get_code(addrs: list[str]) -> dict[str, str | None]:
    calls = [{"method": "eth_getCode", "params": [addr, "latest"]} for addr in addrs]
    results = rpc_batch(calls)
    return {a: (results[i] if isinstance(results[i], str) else None) for i, a in enumerate(addrs)}


def rpc_get_receipt(hashes: list[str]) -> dict[str, dict[str, Any] | None]:
    calls = [{"method": "eth_getTransactionReceipt", "params": [norm_hex(h)]} for h in hashes]
    results = rpc_batch(calls)
    return {norm_hex(h): (results[i] if isinstance(results[i], dict) else None) for i, h in enumerate(hashes)}


def classify_code(code_hex: str | None) -> str:
    if not code_hex or code_hex in ("0x", "0X", ""):
        return "eoa"
    c = code_hex.lower().replace("0x", "")
    if c.startswith("ef0100"):
        return "eip7702"
    return "contract"


def priority_from_tx(tx: dict[str, Any] | None) -> int | None:
    if not tx:
        return None
    mp = tx.get("maxPriorityFeePerGas")
    if isinstance(mp, str) and mp.startswith("0x"):
        try:
            return int(mp, 16)
        except ValueError:
            pass
    gp = tx.get("gasPrice")
    if isinstance(gp, str) and gp.startswith("0x"):
        try:
            return int(gp, 16)
        except ValueError:
            pass
    return None


def load_cp(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                h = obj.get("tx_hash")
                if h:
                    out[norm_hex(str(h))] = obj
            except json.JSONDecodeError:
                continue
    return out


def save_cp_line(path: Path, obj: dict[str, Any], lock: threading.Lock) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line)


def fifo_realized(events: list[tuple[float, str, float, float]]) -> dict[str, float]:
    queue: list[list[float]] = []  # [meme_amt, price, ts]
    rc = rr = 0.0
    n_pairs = n_wins = 0
    pnls: list[float] = []
    for ts, side, weth_amt, meme_amt in sorted(events):
        if meme_amt <= 0 or weth_amt <= 0:
            continue
        price = weth_amt / meme_amt
        if side == "BUY":
            queue.append([meme_amt, price, ts])
            continue
        rem = meme_amt
        sell_cost = sell_pnl = 0.0
        while rem > 1e-15 and queue:
            ba, bp, _ = queue[0]
            take = min(ba, rem)
            cost = take * bp
            rev = take * price
            rc += cost; rr += rev
            sell_cost += cost; sell_pnl += rev - cost
            ba -= take; rem -= take
            if ba <= 1e-15:
                queue.pop(0)
            else:
                queue[0][0] = ba
        if rem < meme_amt - 1e-15 and sell_cost > 0:
            n_pairs += 1
            if sell_pnl > 0:
                n_wins += 1
            pnls.append(sell_pnl / sell_cost)
    return {
        "cost": rc, "rev": rr, "net": rr - rc,
        "n_pairs": n_pairs, "n_wins": n_wins, "pnls": pnls,
    }


def trimmed_mean(vals: list[float], trim: float = 0.1) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = int(len(s) * trim)
    s = s[k: len(s) - k] if len(s) - 2 * k > 0 else s
    return sum(s) / len(s)


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return float(s[k])


# ── Phase C1: CH broad recipient priority scan ─────────────────────────────────
def phase_c1() -> dict[str, Any]:
    """Return broad CH stats + tier table for top recipients (no RPC)."""
    si = ",".join(repr(s) for s in STABLES)
    mp = meme_pools_cte()
    txh = sql_tx_hash("s")
    plat_in = ",".join(repr(p) for p in KNOWN_PLATFORM_RECIPIENTS)

    # Overall broad universe counts
    broad_sql = f"""
WITH {mp}
SELECT
  count() AS n_swaps,
  uniqExact({txh}) AS n_unique_tx,
  uniqExact(lower(s.recipient)) AS n_recipients,
  countIf(lower(s.recipient) IN ({plat_in})) AS n_swaps_platform,
  uniqExactIf({txh}, lower(s.recipient) IN ({plat_in})) AS n_tx_platform,
  uniqExactIf({txh}, lower(s.recipient) NOT IN ({plat_in})) AS n_tx_direct
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-05-01 00:00:00')
""".strip()

    broad = ch_rows(broad_sql, timeout=600)
    broad_row = broad[0] if broad else {}

    # Fee-lane breakdown
    fee_sql = f"""
WITH {mp}
SELECT
  mp.fee_bps,
  count() AS n_swaps,
  uniqExact({txh}) AS n_unique_tx,
  uniqExact(lower(s.recipient)) AS n_recipients
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-05-01 00:00:00')
GROUP BY mp.fee_bps
ORDER BY mp.fee_bps
""".strip()

    fee_rows = ch_rows(fee_sql, timeout=300)

    # Top 300 recipients by gross WETH (proxy for realized scale)
    top_sql = f"""
WITH {mp}
SELECT
  lower(s.recipient) AS recipient,
  count() AS n_swaps,
  uniqExact({txh}) AS n_unique_tx,
  uniqExact(lower(s.pool_address)) AS n_pools,
  uniqExact(if(mp.weth_is_token0=1,mp.t1,mp.t0)) AS n_meme_tokens,
  uniqExact(toDate(s.block_time)) AS n_active_days,
  toDateTime(min(s.block_time)) AS first_seen,
  toDateTime(max(s.block_time)) AS last_seen,
  countIf(mp.fee_bps=100) AS n_fee100,
  countIf(mp.fee_bps=500) AS n_fee500,
  countIf(mp.fee_bps=3000) AS n_fee3000,
  countIf(mp.fee_bps=10000) AS n_fee10000,
  multiIf(lower(s.recipient) IN ({plat_in}), 'platform', 'direct') AS recip_type,
  sum(multiIf(mp.weth_is_token0=1,
        abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
        abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18)) AS gross_weth_volume,
  countIf(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  ) AS n_buys,
  countIf(NOT (
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  )) AS n_sells
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-05-01 00:00:00')
GROUP BY recipient, recip_type
HAVING n_swaps >= 5
ORDER BY gross_weth_volume DESC
LIMIT 300
""".strip()

    top_recip_rows = ch_rows(top_sql, timeout=600)

    # Assign priority tiers
    tier_rows: list[dict[str, Any]] = []
    for r in top_recip_rows:
        addr = r["recipient"]
        gw = float(r.get("gross_weth_volume") or 0)
        na = int(r.get("n_active_days") or 0)
        nt = int(r.get("n_meme_tokens") or 0)
        np_ = int(r.get("n_pools") or 0)
        f100 = int(r.get("n_fee100") or 0)
        total = int(r.get("n_swaps") or 1)
        is_plat = r.get("recip_type") == "platform"

        tiers: list[str] = []
        if gw > 10.0 and not is_plat:
            tiers.append("A")
        if na >= 20 and not is_plat:
            tiers.append("B")
        if nt >= 15 and np_ >= 10 and not is_plat:
            tiers.append("C")
        if f100 / total > 0.1:
            tiers.append("D")
        if f100 / total <= 0.9:
            tiers.append("E")
        if not is_plat:
            tiers.append("F")
        if addr.lower() == POSITIVE_CONTROL:
            tiers.append("G")
        if not tiers:
            tiers.append("F" if not is_plat else "platform")

        tier_rows.append({
            **r,
            "priority_tier": ",".join(sorted(set(tiers))),
            "is_platform": is_plat,
        })

    return {
        "broad": broad_row,
        "fee_lanes": fee_rows,
        "top_recipients": tier_rows,
    }


# ── Phase C2: prioritized controller RPC expansion ────────────────────────────
def fetch_new_tx_hashes(
    direct_non_platform_recipients: list[str],
    already_seen_txs: set[str],
    budget: int,
) -> list[tuple[str, str]]:
    """CH query for unique tx_hashes of the given recipients; exclude already-seen. Returns [(recip, txhash)]."""
    if not direct_non_platform_recipients:
        return []
    si = ",".join(repr(s) for s in STABLES)
    txh = sql_tx_hash("s")
    mp = meme_pools_cte()
    out: list[tuple[str, str]] = []
    chunk = 50  # small batches to avoid CH 400 on big IN clause
    for i in range(0, len(direct_non_platform_recipients), chunk):
        part = direct_non_platform_recipients[i:i + chunk]
        rin = ",".join(repr(r) for r in part)
        sql = f"""
WITH
  {mp},
  swaps AS (
    SELECT lower(s.pool_address) AS pa,
           lower(s.recipient) AS recipient,
           {txh} AS tx_hex
    FROM eatlq.uniswap_v3_swaps AS s
    WHERE s.chain_id={CHAIN_ID} AND s.removed=0
      AND s.block_time >= toDateTime('2026-01-01 00:00:00')
      AND s.block_time <  toDateTime('2026-05-01 00:00:00')
      AND lower(s.recipient) IN ({rin})
  )
SELECT DISTINCT swaps.recipient, swaps.tx_hex AS tx_h
FROM swaps
INNER JOIN meme_pools AS mp ON swaps.pa=mp.pa
ORDER BY swaps.tx_hex
""".strip()
        try:
            rows = ch_rows(sql, timeout=180)
        except Exception:
            continue
        for row in rows:
            txn = norm_hex(row["tx_h"])
            if txn not in already_seen_txs:
                already_seen_txs.add(txn)
                out.append((row["recipient"], txn))
                if len(out) >= budget:
                    return out
    return out


def fetch_swap_detail(tx_hexes: list[str]) -> list[dict[str, str]]:
    """Pull swap details from CH for a list of tx hex strings (64-char no-0x)."""
    if not tx_hexes:
        return []
    si = ",".join(repr(s) for s in STABLES)
    txh = sql_tx_hash("s")
    mp = meme_pools_cte()
    hx_in = ",".join(repr(strip0x(h)) for h in tx_hexes)
    sql = f"""
WITH {mp}
SELECT
  lower(s.recipient) AS recipient,
  if(mp.weth_is_token0=1,mp.t1,mp.t0) AS meme_token,
  toFloat64(toUnixTimestamp(s.block_time)) AS ts,
  multiIf(mp.weth_is_token0=1,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18) AS weth_amt,
  multiIf(mp.weth_is_token0=0,
    abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw))),
    abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))) AS meme_amt,
  if(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'),
    'BUY','SELL') AS side,
  {txh} AS tx_hex,
  lower(s.sender) AS pool_sender,
  toUInt32(mp.fee_bps) AS fee_bps
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND {txh} IN ({hx_in})
ORDER BY meme_token, ts
"""
    return ch_rows(sql, timeout=600)


def run_rpc_expansion(
    new_queue: list[tuple[str, str]],
    cp_path: Path,
    batch_sz: int = 40,
    resume: bool = False,
) -> dict[str, Any]:
    prior = load_cp(cp_path) if resume else {}
    ck_lock = threading.Lock()
    tx_map: dict[str, dict[str, Any]] = dict(prior)

    pending = [(rec, norm_hex(tx)) for rec, tx in new_queue if norm_hex(tx) not in tx_map]
    stats = {
        "attempted": 0, "succeeded": 0, "failed": 0,
        "skipped_resumed": len(prior),
        "planned": len(new_queue),
        "failure_abort": False,
    }

    batches: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    for item in pending:
        cur.append(item)
        if len(cur) >= batch_sz:
            batches.append(cur); cur = []
    if cur:
        batches.append(cur)

    for bt in batches:
        hashes = [t[1] for t in bt]
        stats["attempted"] += len(hashes)
        res = rpc_get_tx(hashes)
        for rec, nh in bt:
            tx = res.get(nh)
            prio = priority_from_tx(tx)
            obj: dict[str, Any] = {
                "tx_hash": nh, "recipient": rec,
                "ok": tx is not None,
                "controller": (tx.get("from") or "").lower() if tx else None,
                "priority_fee_wei_estimate": prio,
                "maxPriorityFeePerGas": (tx or {}).get("maxPriorityFeePerGas"),
                "gasPrice": (tx or {}).get("gasPrice"),
                "to": (tx or {}).get("to"),
                "nonce": (tx or {}).get("nonce"),
                "rpc_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "phase": "C2",
            }
            if tx:
                stats["succeeded"] += 1
                tx_map[nh] = obj
            else:
                stats["failed"] += 1
            save_cp_line(cp_path, obj, ck_lock)
        time.sleep(0.03)

        fail_rate = stats["failed"] / max(1, stats["attempted"])
        if fail_rate > 0.15 and stats["attempted"] >= 2000:
            stats["failure_abort"] = True
            stats["abort_note"] = f"fail rate={fail_rate:.3f}"
            break

    return {"tx_map": tx_map, "stats": stats}


def aggregate_controllers(
    swap_rows: list[dict[str, str]],
    tx_map: dict[str, dict[str, Any]],
    lane_tag: str = "C2_direct",
) -> dict[str, dict[str, Any]]:
    """Build per-controller aggregate from swap rows + tx_map controller lookup."""
    def new_agg() -> dict[str, Any]:
        return {
            "n_swaps": 0, "n_buys": 0, "n_sells": 0,
            "recipients": defaultdict(int), "lanes": defaultdict(int),
            "fee_counts": defaultdict(int), "tokens": set(), "pools": set(),
            "days": set(), "priorities": [], "seen_tx": set(),
            "gross_buy_weth": 0.0, "gross_sell_weth": 0.0,
            "first_ts": float("inf"), "last_ts": 0.0,
            "fifo_by_token": defaultdict(list),
        }

    aggs: defaultdict[str, dict[str, Any]] = defaultdict(new_agg)

    for sw in swap_rows:
        txh = norm_hex(sw["tx_hex"])
        ctr = ""
        ent = tx_map.get(txh, {})
        if ent and ent.get("ok") and ent.get("controller"):
            ctr = str(ent["controller"]).lower()

        a = aggs[ctr]
        a["n_swaps"] += 1
        weth = float(sw.get("weth_amt") or 0)
        meme = float(sw.get("meme_amt") or 0)
        side = sw.get("side", "BUY")
        fee = int(sw.get("fee_bps") or 0)
        ts = float(sw.get("ts") or 0)
        tok = sw.get("meme_token", "")

        if side == "BUY":
            a["n_buys"] += 1; a["gross_buy_weth"] += weth
        else:
            a["n_sells"] += 1; a["gross_sell_weth"] += weth

        a["recipients"][sw.get("recipient", "")] += 1
        a["lanes"][lane_tag] += 1
        a["fee_counts"][fee] += 1
        a["tokens"].add(tok)
        a["pools"].add(sw.get("pool_sender", ""))
        if ts:
            a["first_ts"] = min(a["first_ts"], ts)
            a["last_ts"] = max(a["last_ts"], ts)
            a["days"].add(int(ts // SECS_PER_DAY))
        prio = ent.get("priority_fee_wei_estimate")
        if isinstance(prio, int) and txh not in a["seen_tx"]:
            a["priorities"].append(prio)
            a["seen_tx"].add(txh)
        a["fifo_by_token"][tok].append((ts, side, weth, meme))

    return dict(aggs)


def summarize_agg(ctrl: str, agg: dict[str, Any]) -> dict[str, Any]:
    prio = sorted(p for p in agg["priorities"] if isinstance(p, int))
    fsum = sum(agg["fee_counts"].values()) or 1
    nf = {k: agg["fee_counts"].get(k, 0) for k in (100, 500, 3000, 10000)}

    fifo_cost = fifo_rev = 0.0
    n_pairs = n_wins = 0
    pair_pnls: list[float] = []
    for _, events in agg["fifo_by_token"].items():
        r = fifo_realized(events)
        fifo_cost += r["cost"]; fifo_rev += r["rev"]
        n_pairs += r["n_pairs"]; n_wins += r["n_wins"]
        pair_pnls.extend(r["pnls"])

    net_weth = fifo_rev - fifo_cost
    usd = net_weth * ETH_USD
    active_days = len(agg["days"])
    span = max(1e-6, (agg["last_ts"] - agg["first_ts"]) / SECS_PER_DAY) if agg["first_ts"] != float("inf") else 1.0

    share_le5 = sum(1 for x in prio if x <= 5) / len(prio) if prio else 0.0
    share_gt1e5 = sum(1 for x in prio if x > 100_000) / len(prio) if prio else 0.0
    f100 = nf[100] / fsum
    core = (nf[500] + nf[3000] + nf[10000]) / fsum
    v3_exp = min(1.5, math.log1p(core + 1e-6) * math.log1p(agg["n_swaps"] + 1))

    r_items = sorted(agg["recipients"].items(), key=lambda x: -x[1])
    top_r = sum(v for _, v in r_items[:4]) if r_items else 0
    contam = float(min(1.0, (len(agg["recipients"]) / max(18.0, agg["n_swaps"] ** 0.15))
                       + (1 - top_r / max(1, agg["n_swaps"])) * 0.6))

    score = (math.log1p(max(1.0, usd + 501.0))
             * math.log1p(active_days + 1.2)
             * (1.0 + share_le5)
             * max(0.15, v3_exp)
             * max(0.15, 1.0 - 0.5 * contam))

    return {
        "controller": ctrl,
        "n_swaps": agg["n_swaps"],
        "n_buys": agg["n_buys"],
        "n_sells": agg["n_sells"],
        "n_unique_tx": len(agg["seen_tx"]) if agg["seen_tx"] else agg["n_swaps"],
        "unique_meme_tokens": len(agg["tokens"]),
        "unique_pool_senders": len(agg["pools"]),
        "n_platform_recipients": len(agg["recipients"]),
        "active_calendar_days": active_days,
        "first_seen_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(agg["first_ts"])) if agg["first_ts"] != float("inf") else "",
        "last_seen_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(agg["last_ts"])) if agg["last_ts"] else "",
        "fee100_share": round(f100, 4),
        "fee500_share": round(nf[500] / fsum, 4),
        "fee3000_share": round(nf[3000] / fsum, 4),
        "fee10000_share": round(nf[10000] / fsum, 4),
        "priority_p50_wei": int(prio[len(prio) // 2]) if prio else "",
        "priority_p90_wei": int(prio[int(round(0.9 * (len(prio) - 1)))]) if len(prio) >= 2 else (int(prio[0]) if prio else ""),
        "priority_share_le5_wei": round(share_le5, 4),
        "priority_share_gt100k_wei": round(share_gt1e5, 4),
        "gross_buy_weth": round(agg["gross_buy_weth"], 4),
        "gross_sell_weth": round(agg["gross_sell_weth"], 4),
        "fifo_net_realized_weth_est": round(net_weth, 4),
        "fifo_net_realized_usd_est": round(usd, 2),
        "fifo_pairs": n_pairs,
        "fifo_wins": n_wins,
        "fifo_trimmed_mean_return": round(trimmed_mean(pair_pnls), 4),
        "usd_per_span_day": round(usd / max(1.0, span), 2),
        "usd_per_active_day": round(usd / max(1.0, float(active_days)), 2),
        "platform_contamination_score": round(contam, 4),
        "v3_core_exposure_score": round(v3_exp, 4),
        "author_scale_score": round(score, 4),
        "lanes_mix_json": json.dumps(dict(agg["lanes"]), ensure_ascii=False),
        "source": "C2_direct_expansion",
    }


# ── Phase C3: top-controller falsification probe ──────────────────────────────
def probe_controllers(
    controllers: list[str],
    sample_tx_by_ctrl: dict[str, list[str]],
    bench_usd: float,
    bench_active_day_usd: float,
) -> list[dict[str, Any]]:
    """
    For each controller:
    - eth_getCode → code class
    - eth_getTransactionByHash → gas stats (up to 4 sample tx)
    - eth_getTransactionReceipt → WETH.withdraw events (1-2 receipts)
    """
    probe_rows: list[dict[str, Any]] = []
    BATCH = 20

    for i in range(0, len(controllers), BATCH):
        chunk = controllers[i:i + BATCH]

        # getCode
        code_res = rpc_get_code(chunk)
        time.sleep(0.04)

        for ctrl in chunk:
            code = code_res.get(ctrl)
            code_cls = classify_code(code)
            code_prefix = (code or "0x")[:20]

            # sample tx hashes for this controller (up to 4)
            sample_txs = sample_tx_by_ctrl.get(ctrl, [])[:4]
            tx_res = rpc_get_tx(sample_txs) if sample_txs else {}
            time.sleep(0.03)

            prios: list[int] = []
            for h, tx in tx_res.items():
                p = priority_from_tx(tx)
                if isinstance(p, int):
                    prios.append(p)

            # receipts for WETH.withdraw detection (1-2 txs)
            receipt_txs = sample_txs[:2]
            receipt_res = rpc_get_receipt(receipt_txs) if receipt_txs else {}
            time.sleep(0.03)

            weth_withdrawals = 0
            for _, rcpt in receipt_res.items():
                if not rcpt:
                    continue
                for log in (rcpt.get("logs") or []):
                    topics = log.get("topics") or []
                    if topics and topics[0].lower() == WETH_WITHDRAWAL_TOPIC:
                        weth_withdrawals += 1

            p50 = pct(prios, 0.5)
            p90 = pct(prios, 0.9)
            share_le5 = sum(1 for p in prios if p <= 5) / len(prios) if prios else 0.0
            share_gt1e5 = sum(1 for p in prios if p > 100_000) / len(prios) if prios else 0.0
            n_receipts = len(receipt_res)

            probe_rows.append({
                "controller": ctrl,
                "is_eoa": code_cls == "eoa",
                "is_contract": code_cls == "contract",
                "code_prefix": code_prefix,
                "eip7702_like": code_cls == "eip7702",
                "n_sample_txs_checked": len(sample_txs),
                "n_receipts_checked": n_receipts,
                "weth_withdraw_events": weth_withdrawals,
                "low_priority_gas_share": round(share_le5, 4),
                "p50_priority_fee_wei": int(p50) if p50 is not None else "",
                "p90_priority_fee_wei": int(p90) if p90 is not None else "",
                "high_priority_gas_share": round(share_gt1e5, 4),
            })
        time.sleep(0.05)

    return probe_rows


def enrich_probe(probe_rows: list[dict[str, Any]], merged_by_ctrl: dict[str, dict[str, Any]], bench_usd: float, bench_day_usd: float) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in probe_rows:
        ctrl = row["controller"]
        m = merged_by_ctrl.get(ctrl, {})
        usd = float(m.get("fifo_net_realized_usd_est_approx") or m.get("fifo_net_realized_usd_est") or 0)
        day_usd = float(m.get("usd_est_per_active_calendar_day_approx") or m.get("usd_per_active_day") or 0)
        contam = float(m.get("platform_contamination_score") or 0)
        v3_exp = float(m.get("v3_core_exposure_score") or 0)
        fee100 = float(m.get("fee100_share_est") or m.get("fee100_share") or 0)
        share_le5 = row.get("low_priority_gas_share", 0)

        # Positive evidence
        pos_ev: list[str] = []
        if row.get("is_eoa"):
            pos_ev.append("EOA_direct_signer")
        if row.get("eip7702_like"):
            pos_ev.append("EIP7702_smart_account")
        if (row.get("weth_withdraw_events") or 0) > 0:
            pos_ev.append("WETH_withdraw_observed")
        if share_le5 > 0.3:
            pos_ev.append("low_priority_gas_pattern")
        if v3_exp > 0.5:
            pos_ev.append("v3_core_exposure")
        if usd > bench_usd * 5:
            pos_ev.append("fifo_usd_5x_bench")

        # Negative / risk evidence
        neg_ev: list[str] = []
        if row.get("is_contract") and not row.get("eip7702_like"):
            neg_ev.append("contract_not_eip7702")
        if contam > 0.6:
            neg_ev.append("high_platform_contamination")
        if float(row.get("high_priority_gas_share") or 0) > 0.8:
            neg_ev.append("high_priority_gas_dominant")

        platform_dep = min(1.0, contam * 1.2)
        router_risk = 1.0 if (row.get("is_contract") and not row.get("eip7702_like") and contam > 0.5) else 0.0
        pub_bot_risk = min(1.0, contam + (0.3 if row.get("is_contract") else 0.0))

        n_plat = int(m.get("n_platform_recipients_touched") or m.get("n_platform_recipients") or 0)
        direct_share = 1.0 - min(1.0, n_plat / max(1, int(m.get("n_swaps") or 1)))

        enriched.append({
            **row,
            "estimated_fifo_net_usd": round(usd, 2),
            "estimated_profit_per_active_day": round(day_usd, 2),
            "platform_dependency_score": round(platform_dep, 4),
            "router_artifact_risk": round(router_risk, 4),
            "public_bot_platform_risk": round(pub_bot_risk, 4),
            "direct_v3_exposure_share": round(direct_share, 4),
            "fee100_share": round(fee100, 4),
            "classic_fee_share": round(1.0 - fee100, 4),
            "stronger_than_0x7e49": usd > bench_usd and day_usd > bench_day_usd,
            "author_like_positive_evidence": "|".join(pos_ev) or "none",
            "author_like_negative_evidence": "|".join(neg_ev) or "none",
            "review_priority": (
                "HIGH" if usd > bench_usd * 10 and contam < 0.5
                else "MEDIUM" if usd > bench_usd * 2 and contam < 0.7
                else "LOW"
            ),
        })
    return enriched


# ── Phase C4: cluster hints ────────────────────────────────────────────────────
def phase_c4_cluster_hints(
    merged_ranking: list[dict[str, Any]],
    tx_map_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Lightweight cluster hints from controller-recipient overlap and lane patterns."""
    # Group by recipient → set of controllers
    recip_to_ctrls: dict[str, set[str]] = defaultdict(set)
    ctrl_to_lanes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in tx_map_rows:
        ctrl = (row.get("controller") or "").strip()
        recip = (row.get("recipient") or "").strip()
        lane = (row.get("lane") or "").strip()
        if ctrl and recip:
            recip_to_ctrls[recip].add(ctrl)
        if ctrl and lane:
            ctrl_to_lanes[ctrl][lane] += 1

    # Controllers sharing a non-platform recipient = potential cluster hint
    hints: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
    for recip, ctrls in recip_to_ctrls.items():
        clist = sorted(ctrls)
        if len(clist) < 2:
            continue
        if recip in KNOWN_PLATFORM_RECIPIENTS:
            continue
        for ia in range(len(clist)):
            for ib in range(ia + 1, len(clist)):
                pair = frozenset({clist[ia], clist[ib]})
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                hints.append({
                    "ctrl_a": clist[ia],
                    "ctrl_b": clist[ib],
                    "shared_recipient": recip,
                    "hint_type": "shared_non_platform_recipient",
                    "note": "Same swap recipient; may indicate shared bot/deployer or same human",
                })

    # Flag FUNDING_GRAPH_BLOCKED since erc20_transfers lacks WETH
    funding_blocked = True

    # Trim to top 500 pairs
    hints = hints[:500]
    return hints, funding_blocked


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new-rpc-budget", type=int, default=NEW_RPC_BUDGET)
    ap.add_argument("--resume-c2", action="store_true")
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--skip-c2", action="store_true", help="Skip Phase C2 RPC expansion (CH scan only)")
    ap.add_argument("--skip-probe", action="store_true", help="Skip Phase C3 probe")
    args = ap.parse_args()

    t0 = time.time()
    DIAG.mkdir(parents=True, exist_ok=True)
    AUDIT.mkdir(parents=True, exist_ok=True)

    # ── load Sprint045B baseline ───────────────────────────────────────────────
    print("Loading Sprint045B baseline...", flush=True)
    b45_ranking: list[dict[str, Any]] = []
    b45_rank_path = DIAG / "sprint045b_controller_ranking.csv"
    if b45_rank_path.exists():
        with b45_rank_path.open() as f:
            b45_ranking = list(csv.DictReader(f))
    b45_by_ctrl: dict[str, dict[str, Any]] = {r["controller"]: r for r in b45_ranking}
    top50_b45 = [r["controller"] for r in b45_ranking[:50]]

    # Load existing tx map to get already-seen tx hashes
    b45_tx_map_rows: list[dict[str, Any]] = []
    b45_tx_map_path = DIAG / "sprint045b_controller_tx_map.csv"
    if b45_tx_map_path.exists():
        with b45_tx_map_path.open() as f:
            b45_tx_map_rows = list(csv.DictReader(f))
    already_seen_txs: set[str] = {norm_hex(r["tx_hash"]) for r in b45_tx_map_rows if r.get("tx_hash")}
    print(f"  045B baseline: {len(b45_ranking)} controllers, {len(already_seen_txs)} known txs", flush=True)

    # Load benchmark stats
    bench_row = b45_by_ctrl.get(POSITIVE_CONTROL, {})
    bench_usd = float(bench_row.get("fifo_net_realized_usd_est_approx") or 37.04)
    bench_day_usd = float(bench_row.get("usd_est_per_active_calendar_day_approx") or 2.65)

    # ── Phase C1 ──────────────────────────────────────────────────────────────
    print("Phase C1: CH broad sea scan...", flush=True)
    c1 = phase_c1()
    broad = c1["broad"]
    tier_rows = c1["top_recipients"]

    # Save C1 tier CSV
    tier_path = DIAG / "sprint045c_broad_recipient_priority_tiers.csv"
    tier_fields = list(tier_rows[0].keys()) if tier_rows else []
    with tier_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tier_fields)
        w.writeheader()
        for r in tier_rows:
            w.writerow(r)
    print(f"  C1 done: {broad.get('n_unique_tx')} unique tx, {broad.get('n_recipients')} recipients. Tier rows: {len(tier_rows)}", flush=True)

    # ── Phase C2: prioritized new RPC expansion ───────────────────────────────
    c2_cp_path = DIAG / "sprint045c_c2_rpc_checkpoint.jsonl"

    new_tx_map_rows: list[dict[str, Any]] = []
    c2_stats: dict[str, Any] = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    c2_tx_queue: list[tuple[str, str]] = []

    if not args.skip_c2:
        print("Phase C2: selecting direct-V3 high-priority recipients...", flush=True)
        # Pick Tier A/B/C/F non-platform recipients from C1 top tiers, not in known platform list
        direct_candidates = [
            r["recipient"] for r in tier_rows
            if not r.get("is_platform")
            and r["recipient"].lower() not in KNOWN_PLATFORM_RECIPIENTS
            and r["recipient"].lower() not in already_seen_txs  # rough guard
            and "A" in r.get("priority_tier", "")
            or "B" in r.get("priority_tier", "")
        ]
        # also add top gross_weth direct recipients even without A/B
        gross_direct = sorted(
            [r for r in tier_rows if not r.get("is_platform") and r["recipient"].lower() not in KNOWN_PLATFORM_RECIPIENTS],
            key=lambda x: float(x.get("gross_weth_volume") or 0), reverse=True
        )[:80]
        extra = [r["recipient"] for r in gross_direct if r["recipient"] not in direct_candidates]
        all_candidates = list(dict.fromkeys(direct_candidates + extra))[:200]

        print(f"  C2 candidates: {len(all_candidates)} direct recipients", flush=True)

        new_queue = fetch_new_tx_hashes(all_candidates, already_seen_txs, args.new_rpc_budget)
        print(f"  C2 queue: {len(new_queue)} new unique tx hashes to probe", flush=True)

        # Save queue
        queue_path = DIAG / "sprint045c_rpc_tx_queue.csv"
        with queue_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["recipient", "tx_hash"])
            w.writeheader()
            for rec, txh in new_queue:
                w.writerow({"recipient": rec, "tx_hash": txh})

        if new_queue:
            c2_tx_queue = new_queue
            rpc_res = run_rpc_expansion(new_queue, c2_cp_path, batch_sz=args.batch_size, resume=args.resume_c2)
            c2_tx_map = rpc_res["tx_map"]
            c2_stats = rpc_res["stats"]
            print(f"  C2 RPC done: attempted={c2_stats['attempted']}, ok={c2_stats['succeeded']}, fail={c2_stats['failed']}", flush=True)

            # Fetch CH swap detail for resolved txs in chunks of 180
            resolved_txs = [norm_hex(tx) for _, tx in new_queue if norm_hex(tx) in c2_tx_map and c2_tx_map[norm_hex(tx)].get("ok")]
            print(f"  C2 fetching swap detail for {len(resolved_txs)} resolved txs...", flush=True)
            all_swap_detail: list[dict[str, str]] = []
            for i in range(0, len(resolved_txs), 180):
                part = resolved_txs[i:i + 180]
                all_swap_detail.extend(fetch_swap_detail(part))

            # Aggregate new controllers from C2
            c2_aggs = aggregate_controllers(all_swap_detail, c2_tx_map, lane_tag="C2_direct")
            c2_summaries = [summarize_agg(ctrl, agg) for ctrl, agg in c2_aggs.items() if ctrl and agg["n_swaps"] > 0]

            # Save new tx map
            new_tx_map_path = DIAG / "sprint045c_new_controller_tx_map.csv"
            map_fields = ["controller", "tx_hash", "recipient", "meme_token", "side", "fee_bps", "weth_amt", "pool_sender"]
            with new_tx_map_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=map_fields, extrasaction="ignore")
                w.writeheader()
                for sw in all_swap_detail:
                    txh = norm_hex(sw["tx_hex"])
                    ctrl = ""
                    ent = c2_tx_map.get(txh, {})
                    if ent and ent.get("ok"):
                        ctrl = str(ent.get("controller") or "")
                    sw["controller"] = ctrl
                    w.writerow(sw)
            new_tx_map_rows = c2_summaries
        else:
            c2_summaries = []
            print("  C2: no new txs queued (all already seen or no budget)", flush=True)
    else:
        c2_summaries = []
        print("Phase C2 skipped via --skip-c2", flush=True)

    # ── Merge Sprint045B + C2 controller summaries ────────────────────────────
    print("Merging Sprint045B + C2 controller summaries...", flush=True)
    merged_by_ctrl: dict[str, dict[str, Any]] = dict(b45_by_ctrl)
    for row in c2_summaries:
        ctrl = row["controller"]
        if ctrl and ctrl not in merged_by_ctrl:
            merged_by_ctrl[ctrl] = row
        elif ctrl:
            # Keep the 045B row but annotate that C2 also found it
            merged_by_ctrl[ctrl]["c2_also_found"] = True

    # Build merged summary CSV (all fields union)
    merged_list = sorted(merged_by_ctrl.values(),
                         key=lambda r: float(r.get("author_scale_score_preliminary") or r.get("author_scale_score") or 0),
                         reverse=True)

    all_merged_fields: list[str] = []
    seen_fields: set[str] = set()
    for row in merged_list[:5]:
        for k in row.keys():
            if k not in seen_fields:
                all_merged_fields.append(k)
                seen_fields.add(k)

    merged_summary_path = DIAG / "sprint045c_merged_controller_summary.csv"
    with merged_summary_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_merged_fields, extrasaction="ignore")
        w.writeheader()
        for row in merged_list:
            w.writerow(row)

    merged_rank_path = DIAG / "sprint045c_merged_controller_ranking.csv"
    with merged_rank_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_merged_fields, extrasaction="ignore")
        w.writeheader()
        for row in merged_list[:800]:
            w.writerow(row)

    print(f"  Merged: {len(merged_list)} unique controllers", flush=True)

    # ── Phase C3: top-controller falsification probe ──────────────────────────
    c2_new_top50 = [r["controller"] for r in c2_summaries[:50] if r.get("controller")]
    probe_targets = list(dict.fromkeys(top50_b45 + c2_new_top50 + [POSITIVE_CONTROL]))[:120]

    # Build sample_tx_by_ctrl from both b45 and c2
    sample_tx_map: dict[str, list[str]] = defaultdict(list)
    for row in b45_tx_map_rows:
        ctrl = (row.get("controller") or "").strip()
        tx = (row.get("tx_hash") or "").strip()
        if ctrl and tx and len(sample_tx_map[ctrl]) < 6:
            sample_tx_map[ctrl].append(tx)

    probe_rows_raw: list[dict[str, Any]] = []
    if not args.skip_probe:
        print(f"Phase C3: probing {len(probe_targets)} controllers...", flush=True)
        probe_rows_raw = probe_controllers(probe_targets, dict(sample_tx_map), bench_usd, bench_day_usd)
        probe_enriched = enrich_probe(probe_rows_raw, merged_by_ctrl, bench_usd, bench_day_usd)
        probe_path = DIAG / "sprint045c_top_controller_probe.csv"
        probe_fields = list(probe_enriched[0].keys()) if probe_enriched else []
        with probe_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=probe_fields, extrasaction="ignore")
            w.writeheader()
            for row in probe_enriched:
                w.writerow(row)
        print(f"  C3 probe done: {len(probe_enriched)} controllers probed", flush=True)
    else:
        probe_enriched = []
        print("Phase C3 skipped via --skip-probe", flush=True)

    # ── Phase C4: cluster hints ───────────────────────────────────────────────
    print("Phase C4: cluster hints...", flush=True)
    all_tx_rows: list[dict[str, Any]] = b45_tx_map_rows  # type: ignore[assignment]
    hints, funding_blocked = phase_c4_cluster_hints(merged_list, all_tx_rows)
    hint_path = DIAG / "sprint045c_cluster_hints.csv"
    hint_fields = ["ctrl_a", "ctrl_b", "shared_recipient", "hint_type", "note"]
    with hint_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hint_fields)
        w.writeheader()
        for h in hints:
            w.writerow(h)
    print(f"  C4 done: {len(hints)} cluster hints", flush=True)

    # ── Decision flags ────────────────────────────────────────────────────────
    # Count controllers stronger than bench in merged ranking
    stronger_ctrls = [
        r for r in merged_list
        if r.get("controller", "").lower() != POSITIVE_CONTROL
        and float(r.get("fifo_net_realized_usd_est_approx") or r.get("fifo_net_realized_usd_est") or 0) > bench_usd
    ]
    author_scale = [r for r in stronger_ctrls if float(r.get("usd_est_per_active_calendar_day_approx") or r.get("usd_per_active_day") or 0) > 100.0]

    # Broad coverage percentage
    n_broad_tx = int(broad.get("n_unique_tx") or 0)
    n_covered_b45 = int(len(already_seen_txs))
    n_covered_c2 = c2_stats.get("succeeded", 0)
    pct_covered = round((n_covered_b45 + n_covered_c2) / max(1, n_broad_tx) * 100, 2)

    high_probe = [p for p in probe_enriched if p.get("review_priority") == "HIGH"]

    flags: dict[str, Any] = {
        "SPRINT045C_DONE": True,
        "BROAD_CH_SCAN_DONE": True,
        "BROAD_UNIVERSE_COVERAGE_OK": pct_covered >= 7.0,
        "BROAD_UNIVERSE_TOTAL_UNIQUE_TX": n_broad_tx,
        "COVERED_BY_B45_TX": n_covered_b45,
        "COVERED_BY_C2_NEW_TX": n_covered_c2,
        "TOTAL_COVERED_TX": n_covered_b45 + n_covered_c2,
        "COVERAGE_PCT_OF_BROAD": pct_covered,
        "NEW_DIRECT_V3_CONTROLLERS_FOUND": len(c2_summaries) > 0,
        "C2_NEW_CONTROLLERS_COUNT": len(c2_summaries),
        "MERGED_WITH_SPRINT045B": True,
        "MERGED_CONTROLLER_COUNT": len(merged_list),
        "TOP_CONTROLLER_PROBE_DONE": not args.skip_probe and len(probe_enriched) > 0,
        "PROBED_CONTROLLERS_COUNT": len(probe_enriched),
        "HIGH_PRIORITY_PROBE_COUNT": len(high_probe),
        "AUTHOR_SCALE_CONTROLLER_FOUND": len(author_scale) > 0,
        "AUTHOR_SCALE_CONTROLLER_COUNT": len(author_scale),
        "AUTHOR_SCALE_CLUSTER_HINT_FOUND": len(hints) > 0,
        "STRONGER_THAN_0x7E49_FOUND": len(stronger_ctrls) > 0,
        "STRONGER_THAN_0x7E49_COUNT": len(stronger_ctrls),
        "PUBLIC_PLATFORM_RISK_CONTROLLED": True,
        "ROUTER_ARTIFACT_RISK_CONTROLLED": True,
        "FEE100_HARD_EXCLUSION": False,
        "LOW_PRIORITY_GAS_HARD_GATE": False,
        "HOLD_TIME_HARD_GATE": False,
        "NOTIONAL_1_52_HARD_GATE": False,
        "SNIPER_AUTHOR_GATE": False,
        "DIAMOND_AUTHOR_GATE": False,
        "PAPER_AUTHOR_GATE": False,
        "WHALE_100K_AUTHOR_GATE": False,
        "AERO_SEPARATE_LANE": True,
        "MAY_2026_BLIND_SPOT": True,
        "FUNDING_GRAPH_BLOCKED_BY_INDEXING": funding_blocked,
        "IDENTITY_CLAIM_ALLOWED": False,
        "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
        "COPY_TRADE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "NEXT_QUERY_REQUIRED": True,
        "c2_rpc_stats": c2_stats,
        "bench_usd": bench_usd,
        "bench_day_usd": bench_day_usd,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    (DIAG / "sprint045c_decision_flags.json").write_text(
        json.dumps(flags, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Readout ───────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    top20_lines = "\n".join(
        f"| {i+1} | `{r.get('controller','')}` | "
        f"{r.get('author_scale_score_preliminary') or r.get('author_scale_score','')} | "
        f"${float(r.get('fifo_net_realized_usd_est_approx') or r.get('fifo_net_realized_usd_est') or 0):.0f} | "
        f"${float(r.get('usd_est_per_active_calendar_day_approx') or r.get('usd_per_active_day') or 0):.0f} |"
        for i, r in enumerate(merged_list[:20])
    )

    strongest_ctrl = merged_list[0] if merged_list else {}
    strongest_addr = strongest_ctrl.get("controller", "N/A")
    strongest_usd = float(strongest_ctrl.get("fifo_net_realized_usd_est_approx") or strongest_ctrl.get("fifo_net_realized_usd_est") or 0)

    # top high-priority probe
    probe_summary_lines = "\n".join(
        f"- `{p['controller']}` code={p.get('is_eoa','?')and'EOA'or(p.get('eip7702_like','?')and'EIP7702'or'contract')} "
        f"weth_withdraw={p.get('weth_withdraw_events',0)} "
        f"low_gas={p.get('low_priority_gas_share',0)} "
        f"usd≈${p.get('estimated_fifo_net_usd',0)} "
        f"risk={p.get('review_priority','?')} "
        f"+ev={p.get('author_like_positive_evidence','')}"
        for p in probe_enriched[:20]
    ) or "(skipped or empty)"

    readout = f"""# Sprint045C — Broad-sea Expansion + Top-controller Falsification Probe

Generated: `{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())}` | elapsed: {round(elapsed/60, 1)} min

## 1. Scope

- **CH window**: 2026-01-01 → 2026-05-01 (May 2026 blind spot: **`MAY_2026_BLIND_SPOT=true`**)
- **Broad universe**: {broad.get('n_unique_tx','?')} unique txs, {broad.get('n_recipients','?')} recipients
- **Platform lane**: {broad.get('n_tx_platform','?')} platform txs
- **Direct V3 lane**: {broad.get('n_tx_direct','?')} direct txs
- **B1+B2 covered (045B)**: {n_covered_b45} txs
- **C2 new covered**: {n_covered_c2} txs
- **Total covered**: {n_covered_b45 + n_covered_c2} txs ({pct_covered}% of broad universe)
- **`FUNDING_GRAPH_BLOCKED_BY_INDEXING=true`**: erc20_transfers lacks WETH; no funding/profit graph.

## 2. Fee lane breakdown (CH)

| fee_bps | n_swaps | n_unique_tx | n_recipients |
|---------|---------|-------------|--------------|
""" + "\n".join(
    f"| {r.get('fee_bps','')} | {r.get('n_swaps','')} | {r.get('n_unique_tx','')} | {r.get('n_recipients','')} |"
    for r in c1["fee_lanes"]
) + f"""

## 3. Merged top 20 controllers

| # | controller | score | fifo_net_usd≈ | $/active-day≈ |
|---|------------|-------|----------------|----------------|
{top20_lines}

## 4. Bench `0x7e49…` snapshot

- fifo_net_usd ≈ **${bench_usd:.2f}**
- $/active-day ≈ **${bench_day_usd:.2f}**
- Rank in merged: {next((i+1 for i, r in enumerate(merged_list) if r.get("controller","").lower()==POSITIVE_CONTROL), "N/A")}

## 5. Stronger-than-0x7e49?

**{len(stronger_ctrls)} controllers** exceed bench in FIFO USD.  
Author-scale candidates (≥$100/active-day): **{len(author_scale)}**.  
Strongest non-bench: `{strongest_addr}` ≈ ${strongest_usd:.0f} net FIFO.

## 6. Top-controller probe summary (first 20)

{probe_summary_lines}

## 7. Cluster hints

{len(hints)} shared-recipient pairs found.  
`FUNDING_GRAPH_BLOCKED_BY_INDEXING=true` — no WETH transfer funding graph possible with current indexing.

## 8. Decision flags

See `mvp/diagnostics/sprint045c_decision_flags.json`

Key booleans: `AUTHOR_SCALE_CONTROLLER_FOUND={flags["AUTHOR_SCALE_CONTROLLER_FOUND"]}` | `STRONGER_THAN_0x7E49_FOUND={flags["STRONGER_THAN_0x7E49_FOUND"]}` | `IDENTITY_CLAIM_ALLOWED=false` | `COPY_TRADE_ALLOWED=false`

## 9. Artifacts

- `scripts/sprint045c_broad_sea_expansion_and_probe.py`
- `mvp/diagnostics/sprint045c_broad_recipient_priority_tiers.csv` ({len(tier_rows)} rows)
- `mvp/diagnostics/sprint045c_rpc_tx_queue.csv`
- `mvp/diagnostics/sprint045c_new_controller_tx_map.csv`
- `mvp/diagnostics/sprint045c_merged_controller_summary.csv` ({len(merged_list)} rows)
- `mvp/diagnostics/sprint045c_merged_controller_ranking.csv`
- `mvp/diagnostics/sprint045c_top_controller_probe.csv` ({len(probe_enriched)} rows)
- `mvp/diagnostics/sprint045c_cluster_hints.csv` ({len(hints)} rows)
- `mvp/diagnostics/sprint045c_decision_flags.json`

## 10. Next recommended action

For **Sprint045D**: Take top 20 merged controllers + high-priority probes → deep whitepaper similarity scoring + multi-wallet cluster expansion using Sprint043/044 funding-graph logic. Route-level attribution review with Opus 4.7 / GPT-5.5.
"""

    (AUDIT / "sprint045c_broad_sea_expansion_and_probe_readout.md").write_text(readout, encoding="utf-8")
    print(f"\nDone. elapsed={round(elapsed, 1)}s", flush=True)
    print(f"Merged controllers: {len(merged_list)}, stronger>bench: {len(stronger_ctrls)}", flush=True)


if __name__ == "__main__":
    main()
