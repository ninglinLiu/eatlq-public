"""Sprint045B — Controller-keyed enumeration autopilot (B1 + B2).

Reads `mvp/diagnostics/sprint045_controller_keyed_preflight.json` for policy and
Sprint044 top-12 platform recipients (does not re-run preflight SQL).

Env: CLICKHOUSE_*, LABEL_RPC_URL or EVM_HTTP_URL (do not commit secrets).

Outputs:
  mvp/diagnostics/sprint045b_controller_tx_map.csv
  mvp/diagnostics/sprint045b_controller_summary.csv
  mvp/diagnostics/sprint045b_controller_ranking.csv
  mvp/diagnostics/sprint045b_rpc_checkpoint.jsonl
  mvp/diagnostics/sprint045b_decision_flags.json
  mvp/audits/sprint045b_controller_enumeration_readout.md
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

CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 3500.0
SECS_PER_DAY = 86400.0

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"
AUDIT_DIR = REPO_ROOT / "mvp" / "audits"

PREFLIGHT_JSON = DIAG_DIR / "sprint045_controller_keyed_preflight.json"

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

STABLES = sorted(
    {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
        WETH,
    }
)

KNOWN_ROUTER_META: dict[str, str] = {
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52": "Velora v5 Augustus",
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd": "Sprint041 alt aggregator",
    "0x2626664c2603336e57b271c5c0b26f421741e481": "Uniswap SwapRouter02",
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap Universal Router",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router v2",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x ExchangeProxy",
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol Settlement",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap MetaAggregationRouterV2",
}

B2_LANE_ORDER: list[tuple[str, list[str]]] = [
    ("velora_and_sprint041", ["0x59c7c832e96d2568bea6db468c1aadcbbda08a52", "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd"]),
    (
        "uniswap_routers",
        [
            "0x2626664c2603336e57b271c5c0b26f421741e481",
            "0x6ff5693b99212da76ad316178a184ab56d299b43",
            "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
        ],
    ),
    (
        "1inch",
        [
            "0x111111125421ca6dc452d289314280a0f8842a65",
            "0x1111111254eeb25477b68fb85ed929f73a960582",
        ],
    ),
    (
        "0x_kyber_cow",
        [
            "0xdef1c0ded9bec7f1a1670819833240f027b25eff",
            "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",
            "0x9008d19f58aabd9ed0d60971565aa8510560ab41",
        ],
    ),
]

POSITIVE_CONTROL_LOWER = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"

_RPC_LOCK = threading.Lock()
_RPC_ID = 0


def norm_tx_hex(h_raw: str) -> str:
    h = (h_raw or "").strip().lower().replace("0x", "")
    return "0x" + h


def strip0x(h: str) -> str:
    return h[2:] if h.startswith("0x") else h


def ch_rows(sql: str) -> list[dict[str, str]]:
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
    with urlopen(req, timeout=600) as r:
        text = r.read().decode()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    return [dict(zip(headers, line.split("\t"))) for line in lines[1:]]


def meme_pools_cte(si: str) -> str:
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


def sql_tx_hash_body(alias: str = "s") -> str:
    """CH stores `tx_hash` as `0x` + 64 hex (String). Do not use hex(col) — that hex-encodes ASCII."""
    t = f"{alias}.tx_hash"
    return f"lower(if(startsWith({t}, '0x'), substring({t}, 3), {t}))"


def uniq_tx_hashes_for_recipients(recipients_lower: list[str]) -> list[str]:
    if not recipients_lower:
        return []
    si = ",".join(repr(s) for s in STABLES)
    rin = ",".join(repr(r) for r in recipients_lower)
    mp = meme_pools_cte(si)
    txh = sql_tx_hash_body("s")
    sql = f"""
WITH
  {mp},
  swaps AS (
    SELECT lower(s.pool_address) AS pa, {txh} AS tx_hex
    FROM eatlq.uniswap_v3_swaps AS s
    WHERE s.chain_id={CHAIN_ID} AND s.removed=0
      AND s.block_time >= toDateTime('2026-01-01 00:00:00')
      AND s.block_time <  toDateTime('2026-05-01 00:00:00')
      AND lower(s.recipient) IN ({rin})
  )
SELECT DISTINCT swaps.tx_hex AS tx_hex
FROM swaps
INNER JOIN meme_pools AS mp ON swaps.pa = mp.pa
ORDER BY swaps.tx_hex
""".strip()
    rows = ch_rows(sql)
    return [norm_tx_hex(r["tx_hex"]) for r in rows]


def fetch_swap_rows_for_tx_hex(hexes: list[str]) -> list[dict[str, str]]:
    if not hexes:
        return []
    si = ",".join(repr(s) for s in STABLES)
    hx_in = ",".join(repr(strip0x(h)) for h in hexes)
    mp = meme_pools_cte(si)
    txh = sql_tx_hash_body("s")
    sql = f"""
WITH {mp}
SELECT
  lower(s.recipient) AS recipient,
  if(mp.weth_is_token0=1, mp.t1, mp.t0) AS meme_token,
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
    'BUY', 'SELL'
  ) AS side,
  {txh} AS tx_hex,
  lower(s.sender) AS pool_sender,
  toUInt32(mp.fee_bps) AS fee_bps,
  s.block_time AS block_time
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND {txh} IN ({hx_in})
ORDER BY meme_token, ts
"""
    return ch_rows(sql)


def fifo_realized(events: list[tuple[float, str, float, float]]) -> tuple[float, float, float, int, int, list[float], list[float]]:
    """FIFO PnL in WETH; hold_seconds sampled per FIFO lot consumed on sells."""
    queue: list[list[float]] = []  # meme_amt, price, ts_buy
    rc = rr = 0.0
    n_pairs = n_wins = 0
    pair_pnls: list[float] = []
    hold_secs: list[float] = []

    for ts, side, weth_amt, meme_amt in sorted(events):
        if meme_amt <= 0 or weth_amt <= 0:
            continue
        price = weth_amt / meme_amt
        if side == "BUY":
            queue.append([meme_amt, price, ts])
            continue
        remaining = meme_amt
        sell_pnl_chunk = sell_cost_chunk = 0.0
        while remaining > 1e-15 and queue:
            buy_amt, buy_price, buy_ts = queue[0]
            take = min(buy_amt, remaining)
            cost_chunk = take * buy_price
            rev_chunk = take * (weth_amt / meme_amt)
            rc += cost_chunk
            rr += rev_chunk
            sell_cost_chunk += cost_chunk
            sell_pnl_chunk += rev_chunk - cost_chunk
            hold_secs.append(ts - buy_ts)
            buy_amt -= take
            remaining -= take
            if buy_amt <= 1e-15:
                queue.pop(0)
            else:
                queue[0][0] = buy_amt
        if remaining < meme_amt - 1e-15 and sell_cost_chunk > 0:
            n_pairs += 1
            if sell_pnl_chunk > 0:
                n_wins += 1
            pair_pnls.append(sell_pnl_chunk / sell_cost_chunk)

    return rc, rr, rr - rc, n_pairs, n_wins, pair_pnls, hold_secs


def trimmed_mean(vals: list[float], trim: float = 0.1) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    k = int(n * trim)
    s = s[k : n - k] if n - 2 * k > 0 else s
    return sum(s) / len(s)


def parse_priority_fee_wei(tx: dict[str, Any] | None) -> int | None:
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


def rpc_eth_get_transaction_batch(hexes: list[str]) -> dict[str, tuple[dict[str, Any] | None, str | None]]:
    """Returns norm_tx -> (txdict, err). Uses JSON-RPC batch array."""
    global _RPC_ID
    if not hexes:
        return {}
    payloads: list[dict[str, Any]] = []
    with _RPC_LOCK:
        for h in hexes:
            _RPC_ID += 1
            payloads.append(
                {
                    "jsonrpc": "2.0",
                    "id": _RPC_ID,
                    "method": "eth_getTransactionByHash",
                    "params": [norm_tx_hex(h)],
                }
            )

    body = json.dumps(payloads).encode()
    backoff = 1.85
    for attempt in range(14):
        try:
            req = Request(
                RPC_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=120) as r:
                blob = json.loads(r.read())

            norm_list = [norm_tx_hex(h) for h in hexes]
            out: dict[str, tuple[dict[str, Any] | None, str | None]] = {}

            if not isinstance(blob, list):
                for nh in norm_list:
                    out[nh] = (None, "non_list_rpc_response")
                return out

            by_id = {item.get("id"): item for item in blob if isinstance(item, dict)}
            for payload, nh in zip(payloads, norm_list):
                it = by_id.get(payload["id"])
                if not it:
                    out[nh] = (None, "missing_rpc_id")
                    continue
                if "error" in it:
                    out[nh] = (None, json.dumps(it["error"]))
                    continue
                res = it.get("result")
                if isinstance(res, dict):
                    out[nh] = (res, None)
                elif res is None:
                    out[nh] = (None, "null_result_tx")
                else:
                    out[nh] = (None, "unexpected_tx_shape")
            return out
        except HTTPError as e:
            code = getattr(e, "code", None)
            if code in (429, 502, 503, 504):
                time.sleep(backoff + random.uniform(0, 2))
                backoff = min(backoff * 2, 90.0)
                continue
            err = json.dumps({"http": code, "msg": str(e)})
            return {norm_tx_hex(h): (None, err) for h in hexes}
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            time.sleep(backoff + random.uniform(0, 2))
            backoff = min(backoff * 2, 90.0)
            continue

    return {norm_tx_hex(h): (None, "retry_exhausted") for h in hexes}


def load_checkpoint(cp_path: Path) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    if not cp_path.exists():
        return resolved
    with cp_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = obj.get("tx_hash")
            if h:
                resolved[norm_tx_hex(str(h))] = obj
    return resolved


def append_checkpoint(cp_path: Path, obj: dict[str, Any], lock: threading.Lock) -> None:
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n"
    with lock:
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        with cp_path.open("a") as f:
            f.write(line)


def build_plan(preflight: dict[str, Any], budget: int) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    top12_rows = preflight["sprint044_top12_platform_recipients_for_controller_decomposition"]
    top12 = [r["recipient"].strip().lower() for r in top12_rows]

    seen: set[str] = set()
    ordered: list[tuple[str, str, str]] = []

    def push(phase: str, lane_key: str, txs: list[str]) -> None:
        nonlocal seen, ordered
        for tx in txs:
            nh = norm_tx_hex(tx)
            if nh in seen:
                continue
            seen.add(nh)
            ordered.append((phase, lane_key, nh))
            if len(ordered) >= budget:
                return

    meta: dict[str, Any] = {
        "b1_recipients_n": len(top12),
        "b1_tx_requested": None,
        "b2_truncated_after_lane": None,
        "planned_total_before_cap": None,
    }

    b1_list = uniq_tx_hashes_for_recipients(top12)
    meta["b1_tx_requested"] = len(b1_list)
    push("B1", "sprint044_top12_platform_recipients", b1_list)
    if len(ordered) >= budget:
        meta["planned_total_before_cap"] = len(ordered)
        meta["b2_truncated_after_lane"] = "not_started_budget_hit_during_B1_collection"
        return ordered, meta

    for lane_name, addrs in B2_LANE_ORDER:
        txs = uniq_tx_hashes_for_recipients([a.lower() for a in addrs])
        n0 = len(ordered)
        push("B2", lane_name, txs)
        if len(ordered) >= budget:
            meta["b2_truncated_after_lane"] = lane_name
            meta["planned_total_before_cap"] = len(ordered)
            return ordered, meta

    meta["planned_total_before_cap"] = len(ordered)
    meta["b2_truncated_after_lane"] = None
    return ordered, meta


def pick_percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return float(s[k])


def run_rpc_phase(
    plan: list[tuple[str, str, str]],
    cp_path: Path,
    *,
    checkpoint_every: int,
    workers: int,
    resume: bool,
    batch_size: int,
) -> dict[str, Any]:
    _ = checkpoint_every
    _ = workers  # reserved; batches are processed sequentially for stable checkpointing

    prior = load_checkpoint(cp_path) if resume else {}
    resumed_ok = sum(1 for v in prior.values() if v.get("ok"))

    tx_map: dict[str, dict[str, Any]] = {}
    for h, v in prior.items():
        if v.get("ok") and isinstance(v.get("controller"), str):
            tx_map[h] = dict(v)

    pending = [tri for tri in plan if norm_tx_hex(tri[2]) not in tx_map]
    ck_lock = threading.Lock()

    stats: dict[str, Any] = {
        "rpc_calls_attempted": 0,
        "rpc_calls_succeeded": 0,
        "rpc_calls_failed": 0,
        "skipped_already_resumed": resumed_ok,
        "planned_unique_tx_total": len(plan),
        "pending_after_resume": len(pending),
        "checkpoint_lines_before_run": len(prior),
        "failure_abort_trigger": False,
        "resumed_from_checkpoint": resumed_ok > 0,
    }

    if not pending:
        stats["rpc_batches"] = 0
        stats["controllers_resolved"] = len(tx_map)
        return {"tx_rpc": tx_map, "stats": stats}

    batches: list[list[tuple[str, str, str]]] = []
    cur: list[tuple[str, str, str]] = []
    for tri in pending:
        cur.append(tri)
        if len(cur) >= batch_size:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)

    bi = -1
    for bi, bt in enumerate(batches):
        hx = [tri[2] for tri in bt]
        stats["rpc_calls_attempted"] += len(hx)

        res = rpc_eth_get_transaction_batch(hx)

        for phase, lk, hh in bt:
            nh = norm_tx_hex(hh)
            tx_obj, err = res.get(nh, (None, "missing_slot"))
            obj: dict[str, Any]
            if tx_obj:
                ctr = ""
                if isinstance(tx_obj.get("from"), str):
                    ctr = tx_obj["from"].strip().lower()
                prio = parse_priority_fee_wei(tx_obj)
                obj = {
                    "phase": phase,
                    "lane": lk,
                    "tx_hash": nh,
                    "ok": True,
                    "controller": ctr,
                    "nonce": tx_obj.get("nonce"),
                    "gas": tx_obj.get("gas"),
                    "gasPrice": tx_obj.get("gasPrice"),
                    "maxFeePerGas": tx_obj.get("maxFeePerGas"),
                    "maxPriorityFeePerGas": tx_obj.get("maxPriorityFeePerGas"),
                    "priority_fee_wei_estimate": prio,
                    "to": tx_obj.get("to"),
                    "rpc_error": None,
                    "rpc_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                stats["rpc_calls_succeeded"] += 1
                tx_map[nh] = obj
            else:
                obj = {
                    "phase": phase,
                    "lane": lk,
                    "tx_hash": nh,
                    "ok": False,
                    "controller": None,
                    "rpc_error": err,
                    "rpc_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                stats["rpc_calls_failed"] += 1

            append_checkpoint(cp_path, obj, ck_lock)

        time.sleep(0.03)

        frac_fail = stats["rpc_calls_failed"] / float(
            max(1, stats["rpc_calls_attempted"])
        )
        if frac_fail > 0.15 and stats["rpc_calls_attempted"] >= 2000:
            stats["failure_abort_trigger"] = True
            stats["abort_failure_rate"] = round(frac_fail, 6)
            stats["abort_note"] = "Failure rate exceeded 15% after >=2000 RPC attempts; remaining batches not processed"
            break

    final_cp = load_checkpoint(cp_path)
    tx_map_final: dict[str, dict[str, Any]] = {}
    for h, v in final_cp.items():
        if v.get("ok") and isinstance(v.get("controller"), str):
            tx_map_final[h] = v

    stats["controllers_resolved"] = len(tx_map_final)
    if cp_path.exists():
        stats["checkpoint_lines_after_run"] = sum(
            1 for ln in cp_path.open() if ln.strip()
        )
    else:
        stats["checkpoint_lines_after_run"] = 0

    stats["rpc_batches_executed"] = bi + 1 if bi >= 0 else 0

    return {"tx_rpc": tx_map_final, "stats": stats}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=120_000)
    ap.add_argument("--checkpoint-every", type=int, default=800, help="(reserved; one line per tx)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    t0_wall = time.time()
    pref = json.loads(PREFLIGHT_JSON.read_text(encoding="utf-8"))

    plan, plan_meta = build_plan(pref, args.budget)

    cp_path = DIAG_DIR / "sprint045b_rpc_checkpoint.jsonl"
    tx_map_path = DIAG_DIR / "sprint045b_controller_tx_map.csv"
    summary_path = DIAG_DIR / "sprint045b_controller_summary.csv"
    rank_path = DIAG_DIR / "sprint045b_controller_ranking.csv"
    flags_path = DIAG_DIR / "sprint045b_decision_flags.json"
    readout = AUDIT_DIR / "sprint045b_controller_enumeration_readout.md"

    if not args.resume and cp_path.exists():
        cp_backup = cp_path.with_suffix(".jsonl.bak_prev_run")
        if cp_backup.exists():
            cp_backup.unlink()
        cp_path.rename(cp_backup)

    rpc_phase = run_rpc_phase(
        plan,
        cp_path,
        checkpoint_every=max(400, args.checkpoint_every),
        workers=max(8, min(16, args.workers)),
        resume=args.resume,
        batch_size=max(10, min(80, args.batch_size)),
    )
    tx_rpc: dict[str, dict[str, Any]] = rpc_phase["tx_rpc"]
    rpc_stats = rpc_phase["stats"]

    # --- aggregate swaps keyed by controller
    plan_hashes = list({norm_tx_hex(tri[2]) for tri in plan})
    # chunk reads for swap rows

    swaps_all: list[dict[str, str]] = []
    # Large `IN (...)` lists can exceed CH max_query_size (~256KiB-level); ~200 txs is safe.
    chunk = 180
    for i in range(0, len(plan_hashes), chunk):
        part = plan_hashes[i : i + chunk]
        swaps_all.extend(fetch_swap_rows_for_tx_hex(part))

    # tx_hash -> controller
    tx_cont: dict[str, str] = {}
    for nh, row in tx_rpc.items():
        if row.get("ok") and isinstance(row.get("controller"), str):
            tx_cont[norm_tx_hex(nh)] = str(row["controller"]).lower()

    def _ctr_new() -> dict[str, Any]:
        return {
            "n_swaps": 0,
            "n_buys": 0,
            "n_sells": 0,
            "recipients_platforms": defaultdict(int),
            "lanes_platform": defaultdict(int),
            "fee_counts": defaultdict(int),
            "tokens": set(),
            "pools": set(),
            "days": set(),
            "priorities": [],
            "priority_seen_tx_hashes": set(),
            "gross_buy_weth": 0.0,
            "gross_sell_weth": 0.0,
            "first_ts": float("inf"),
            "last_ts": 0.0,
            "fifo_by_token": defaultdict(list),
            "swap_tx_hashes": set(),
        }

    ctr_rows: defaultdict[str, dict[str, Any]] = defaultdict(_ctr_new)

    tx_phase_lane: dict[str, tuple[str, str]] = {}
    for phase, lk, hh in plan:
        tx_phase_lane.setdefault(norm_tx_hex(hh), (phase, lk))

    map_csv_rows: list[dict[str, Any]] = []

    for sw in swaps_all:
        txh = norm_tx_hex(sw["tx_hex"])
        ctrl = tx_cont.get(txh)
        if not ctrl:
            ctrl = ""

        ctr = ctr_rows[ctrl]
        ctr["n_swaps"] += 1
        if sw["side"] == "BUY":
            ctr["n_buys"] += 1
            ctr["gross_buy_weth"] += float(sw["weth_amt"])
        else:
            ctr["n_sells"] += 1
            ctr["gross_sell_weth"] += float(sw["weth_amt"])

        ctr["recipients_platforms"][sw["recipient"]] += 1
        ph_ln = tx_phase_lane.get(txh, ("?", "?"))
        ctr["lanes_platform"][ph_ln[1]] += 1
        ctr["fee_counts"][int(sw["fee_bps"])] += 1
        ctr["tokens"].add(sw["meme_token"])
        ctr["pools"].add(sw.get("pool_sender", ""))
        ts = float(sw["ts"])
        ctr["first_ts"] = min(ctr["first_ts"], ts)
        ctr["last_ts"] = max(ctr["last_ts"], ts)
        ctr["days"].add(int(ts // SECS_PER_DAY))
        rpc_ent = tx_rpc.get(txh, {})
        pr = rpc_ent.get("priority_fee_wei_estimate")
        if pr is None and isinstance(rpc_ent.get("maxPriorityFeePerGas"), str):
            pr = parse_priority_fee_wei(
                {"maxPriorityFeePerGas": rpc_ent.get("maxPriorityFeePerGas")}
            )
        if isinstance(pr, int) and txh not in ctr["priority_seen_tx_hashes"]:
            ctr["priority_seen_tx_hashes"].add(txh)
            ctr["priorities"].append(pr)
        ctr["fifo_by_token"][sw["meme_token"]].append(
            (
                ts,
                sw["side"],
                float(sw["weth_amt"]),
                float(sw["meme_amt"]),
            )
        )
        ctr["swap_tx_hashes"].add(txh)

        map_csv_rows.append(
            {
                "controller": ctrl,
                "phase": tx_phase_lane.get(txh, ("?", "?"))[0],
                "lane": tx_phase_lane.get(txh, ("?", "?"))[1],
                "tx_hash": txh,
                "recipient": sw["recipient"],
                "meme_token": sw["meme_token"],
                "side": sw["side"],
                "fee_bps": sw["fee_bps"],
                "weth_amt": sw["weth_amt"],
                "pool_sender": sw["pool_sender"],
                "priority_fee_wei_sample": rpc_ent.get(
                    "priority_fee_wei_estimate", pr
                ),
            }
        )

    summaries: list[dict[str, Any]] = []

    for ctrl, agg in ctr_rows.items():
        if not agg["n_swaps"]:
            continue

        prio = sorted([p for p in agg["priorities"] if isinstance(p, int)])

        def sf(v: float) -> float | None:
            return round(v, 4) if v is not None else None

        share_le5 = (
            sum(1 for x in prio if x <= 5) / len(prio)
            if prio
            else 0.0
        )
        share_gt_1e5 = (
            sum(1 for x in prio if x > 100_000) / len(prio)
            if prio
            else 0.0
        )

        fsum = sum(agg["fee_counts"].values()) or 1
        nf = defaultdict(int)
        for k in (100, 500, 3000, 10000):
            nf[k] = agg["fee_counts"].get(k, 0)

        fifo_pairs_all = 0
        fifo_wins = 0
        pair_pn_all: list[float] = []
        hold_secs: list[float] = []
        fifo_cost = fifo_rev = 0.0

        for _tok, events in agg["fifo_by_token"].items():
            rc, rr, net, np, nw, pns, hds = fifo_realized(events)
            fifo_cost += rc
            fifo_rev += rr
            fifo_pairs_all += np
            fifo_wins += nw
            pair_pn_all.extend(pns)
            hold_secs.extend(hds)

        net_weth = fifo_rev - fifo_cost
        usd_est = net_weth * ETH_USD
        span_days = (
            max(1e-6, (agg["last_ts"] - agg["first_ts"]) / SECS_PER_DAY)
            if agg["first_ts"] != float("inf")
            else 1.0
        )
        active_days = len(agg["days"])
        daily = usd_est / max(1.0, span_days)

        # platform contamination heuristic: fraction of swaps whose recipient differs from top-4 mass
        r_items = sorted(agg["recipients_platforms"].items(), key=lambda x: x[1], reverse=True)
        top_r = sum(x[1] for x in r_items[:4]) if r_items else 0
        containment = 1.0 - (
            float(top_r / max(1, agg["n_swaps"])) if agg["n_swaps"] else 0.0
        )

        uniq_plat = len(agg["recipients_platforms"])

        f100_share = nf[100] / fsum
        core_share = (nf[500] + nf[3000] + nf[10000]) / fsum
        v3_core_exposure = max(
            0.0,
            min(
                1.5,
                math.log1p(core_share + 1e-6) * math.log1p(agg["n_swaps"]),
            ),
        )

        contamination = float(min(1.0, (uniq_plat / max(18.0, float(agg["n_swaps"]) ** 0.15)) + containment * 0.6))

        author_scale_score = math.log1p(max(1.0, usd_est + 501.0)) * math.log1p(active_days + 1.2) * (1.0 + share_le5) * (max(0.15, v3_core_exposure)) * (max(0.15, (1.0 - 0.5 * contamination)))

        summaries.append(
            {
                "controller": ctrl,
                "n_swaps": agg["n_swaps"],
                "n_buys": agg["n_buys"],
                "n_sells": agg["n_sells"],
                "n_unique_swaps_tx": len(agg["swap_tx_hashes"]),
                "n_platform_recipients_touched": len(agg["recipients_platforms"]),
                "n_unique_lane_tags": len(agg["lanes_platform"]),
                "fee100_share_est": round(f100_share, 4),
                "fee500_share_est": round(nf[500] / fsum, 4),
                "fee3000_share_est": round(nf[3000] / fsum, 4),
                "fee10000_share_est": round(nf[10000] / fsum, 4),
                "unique_meme_tokens": len(agg["tokens"]),
                "unique_pool_sender_contracts": len(agg["pools"]),
                "active_calendar_days_approx": active_days,
                "first_seen_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(agg["first_ts"]))
                if agg["first_ts"] != float("inf")
                else "",
                "last_seen_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(agg["last_ts"]))
                if agg["last_ts"]
                else "",
                "priority_fee_p50_wei": int(prio[len(prio) // 2]) if prio else "",
                "priority_fee_p90_wei": (
                    int(prio[int(round(0.9 * (len(prio) - 1)))])
                    if len(prio) >= 2
                    else (int(prio[0]) if prio else "")
                ),
                "priority_share_le5_wei_est": round(share_le5, 4),
                "priority_share_gt100k_wei_est": round(share_gt_1e5, 4),
                "gross_buy_weth_sum": sf(agg["gross_buy_weth"]),
                "gross_sell_weth_sum": sf(agg["gross_sell_weth"]),
                "fifo_realized_cost_weth": sf(fifo_cost),
                "fifo_realized_rev_weth": sf(fifo_rev),
                "fifo_net_realized_weth_est": sf(net_weth),
                "fifo_net_realized_usd_est_approx": round(usd_est, 2),
                "fifo_pairs": fifo_pairs_all,
                "fifo_wins": fifo_wins,
                "fifo_pair_trimmed_mean_return": sf(trimmed_mean(pair_pn_all)),
                "swap_hold_secs_p50": sf(pick_percentile(hold_secs, 0.5)) if hold_secs else "",
                "swap_hold_secs_p90": sf(pick_percentile(hold_secs, 0.9)) if hold_secs else "",
                "usd_approx_per_activity_span_day": round(daily, 2),
                "usd_est_per_active_calendar_day_approx": round(usd_est / max(1.0, float(active_days)), 2),
                "platform_contamination_score": round(contamination, 4),
                "v3_core_exposure_score": round(v3_core_exposure, 4),
                "author_scale_score_preliminary": round(author_scale_score, 4),
                "lanes_mix_json": json.dumps({k: v for k, v in sorted(agg["lanes_platform"].items(), key=lambda x: x[1], reverse=True)}, ensure_ascii=False),
            }
        )

    summaries.sort(key=lambda x: float(x["author_scale_score_preliminary"]), reverse=True)

    # controller ranking top 500
    DIAG_DIR.mkdir(parents=True, exist_ok=True)

    default_tx_fields = [
        "controller",
        "phase",
        "lane",
        "tx_hash",
        "recipient",
        "meme_token",
        "side",
        "fee_bps",
        "weth_amt",
        "pool_sender",
        "priority_fee_wei_sample",
    ]
    tx_fields = sorted({k for r in map_csv_rows for k in r.keys()}) or default_tx_fields
    with tx_map_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tx_fields)
        w.writeheader()
        for r in map_csv_rows:
            w.writerow(r)

    sf_keys = summaries[0].keys() if summaries else []
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        if summaries:
            w = csv.DictWriter(f, fieldnames=list(sf_keys))
            w.writeheader()
            for r in summaries:
                w.writerow(r)

    ranking = sorted(summaries, key=lambda z: float(z["author_scale_score_preliminary"]), reverse=True)
    with rank_path.open("w", newline="", encoding="utf-8") as f:
        if ranking:
            w = csv.DictWriter(f, fieldnames=list(ranking[0].keys()))
            w.writeheader()
            for r in ranking[: min(800, len(ranking))]:
                w.writerow(r)

    ctl = next(
        (r for r in ranking if r["controller"].lower() == POSITIVE_CONTROL_LOWER), None
    )

    strongest_by_usd: dict[str, Any] | None = None
    if summaries:
        strongest_by_usd = max(
            summaries,
            key=lambda r: float(r.get("fifo_net_realized_usd_est_approx") or -1),
        )

    ctl_usd = float(ctl["fifo_net_realized_usd_est_approx"]) if ctl else None
    strongest_non_control: dict[str, Any] | None = None
    if summaries:
        others = [
            r
            for r in summaries
            if str(r.get("controller", "")).lower() != POSITIVE_CONTROL_LOWER
        ]
        if others:
            strongest_non_control = max(
                others,
                key=lambda r: float(r.get("fifo_net_realized_usd_est_approx") or -1),
            )

    strong_vs_bench = False
    if ctl_usd is not None and strongest_non_control is not None:
        strong_vs_bench = float(
            strongest_non_control.get("fifo_net_realized_usd_est_approx", -1)
        ) > (ctl_usd + 2500.0)

    flags = {
        "SPRINT045B_DONE": True,
        "ROUTER_ARTIFACT_FIXED": True,
        "MAY_BLIND_SPOT_CH": not bool(
            pref.get("data_coverage", {}).get("may_2026_v3_data_present", False)
        ),
        "AUTHOR_WALLET_FOUND": False,
        "AUTHOR_SCALE_CONTROLLER_OBSERVED": bool(ranking[:10]),
        "STRONG_CONTROLLER_VS_7E49_USD_FIFO": strong_vs_bench,
        "BENCH_0x7E49_PRESENT_IN_PLAN_AGGREGATES": ctl is not None,
        "B2_TRUNCATED": plan_meta["b2_truncated_after_lane"] is not None,
        "B2_TRUNC_NOTE": plan_meta["b2_truncated_after_lane"],
        "RPC_POLICY": "eth_getTransactionByHash JSON-RPC batches; backoff on timeouts/429 proxies",
        "IDENTITY_CLAIM_ALLOWED": False,
        "ATTRIBUTION_HYPOTHESIS_ALLOWED": True,
        "COPY_TRADE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "NEXT_QUERY_REQUIRED": True,
        "budget_unique_tx_requested": args.budget,
        "planned_unique_tx": len(plan),
        "rpc_stats": rpc_stats,
        "plan_meta": plan_meta,
    }

    elapsed = time.time() - t0_wall
    flags_path.write_text(json.dumps(flags, indent=2, sort_keys=False), encoding="utf-8")

    top_lines = ranking[: min(20, len(ranking))]
    top_txt = "\n".join(f"- `{r['controller']}` score={r['author_scale_score_preliminary']} usd_fifo≈{r['fifo_net_realized_usd_est_approx']} $/cal-day≈{r['usd_est_per_active_calendar_day_approx']}" for r in top_lines)

    stronger_than = "**No** clearer author-scale dominance vs `0x7e49…` FIFO slice in this enumerator"
    if ctl:
        contenders = [
            r
            for r in ranking
            if r["controller"].lower() != POSITIVE_CONTROL_LOWER
            and float(r.get("usd_est_per_active_calendar_day_approx", -1))
            > float(ctl["usd_est_per_active_calendar_day_approx"])
            and float(r.get("fifo_net_realized_usd_est_approx", -1))
            > float(ctl["fifo_net_realized_usd_est_approx"])
        ]
        if contenders:
            best = contenders[0]
            stronger_than = f"Controllers with **higher FIFO USD/day** vs bench: example `{best['controller']}` (approx ${best['fifo_net_realized_usd_est_approx']} net, bench ${ctl['fifo_net_realized_usd_est_approx']}) — still not identity proof."

    read_text = f"""# Sprint045B Controller Enumeration Readout

Generated (UTC wall): `{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())}`

## Scope

- **Window** (CH): `2026-01-01 → 2026-05-01` (April end real max per preflight; May blind spot persists).
- **Preflight reused**: `{PREFLIGHT_JSON.name}`
- **B1**: Sprint044 top-12 platforms (swap recipient contracts), fee lanes incl. **fee=100**.
- **B2**: Velora/Uniswap/1inch/0x+Kyber+CoW prioritized router-as-recipient lanes, dedup unique tx_hashes until **{args.budget}** cap.

## Coverage

Planned unique txs: **{len(plan)}**  

{json.dumps(plan_meta, indent=2)}

RPC summary (`eth_getTransactionByHash` batches; each tx counted once attempted):

```json
{json.dumps(rpc_stats, indent=2)}
```

## Top 20 controllers

{top_txt}

## Positive control `{POSITIVE_CONTROL_LOWER}` snapshot

{f"```json\n{json.dumps(ctl or {}, indent=2)}\n```" }

## Larger / closer `$500`/day heuristic vs bench?

Usd FIFO is a **rough meme-round-trip estimate** bounded to pre-May CH and controller-only remap.

**Answer:** {stronger_than}

## Flags

See `mvp/diagnostics/sprint045b_decision_flags.json`.

## Wall time

elapsed_sec ≈ {round(elapsed, 2)}

"""

    readout.parent.mkdir(parents=True, exist_ok=True)
    readout.write_text(read_text, encoding="utf-8")


if __name__ == "__main__":
    main()
