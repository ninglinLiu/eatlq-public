"""Sprint044 Phase 4 — true FIFO + EOA classification + final outputs.

For each of the top author-style candidates from Phase 3:
  1. Pull every (recipient, meme_token) swap from CH.
  2. Compute proper sell-by-sell FIFO realized PnL in WETH.
  3. RPC eth_getCode classifies signer/recipient as EOA / contract / EIP-7702.
  4. Apply additional known-router / settlement exclusion.
  5. Pull a sample tx_hash and RPC eth_getTransactionByHash for the recipient
     candidate to confirm whether tx.from == recipient (true direct caller)
     or tx.from differs (recipient is delegated/contract receiver).
  6. Add 0x7e49 as positive control via the existing Sprint042 evidence.
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ETH_USD = 3500.0
SECS_PER_DAY = 86400.0

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"
AUDIT_DIR = REPO_ROOT / "mvp" / "audits"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=180"
)

RPC_URL = os.environ.get(
    "EVM_HTTP_URL",
    "https://orbital-purple-sea.base-mainnet.quiknode.pro/"
    "0123456789abcdef0123456789abcdef01234567/",
)

KNOWN_BOTS_OR_ROUTERS = {
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "cow_protocol_settlement_v2",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "kyberswap_meta_aggregation",
    "0x6a000f20005980200259b80c5102003040001068": "bebop_settlement",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "permit2",
    "0x4200000000000000000000000000000000000006": "weth",
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52": "velora_v5_augustus",
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd": "alt_aggregator_sprint041",
    "0x2626664c2603336e57b271c5c0b26f421741e481": "uniswap_swaprouter02",
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "uniswap_universal_router",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "uniswap_universal_router_v2",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch_v6",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch_v5",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x_exchange_proxy",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "uniswap_swap_router_v1",
    "0x103b86a7fe783dc76086c6e03f3ce74cbcc4bd12": "frequent_aggregator",
    "0x498581ff718922c3f8e6a244956af099b2652b2b": "frequent_aggregator",
}


def ch(sql: str) -> list[dict[str, str]]:
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
    with urlopen(req, timeout=300) as r:
        text = r.read().decode()
    rows: list[dict[str, str]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows
    headers = lines[0].split("\t")
    for line in lines[1:]:
        rows.append(dict(zip(headers, line.split("\t"))))
    return rows


_rpc_id = 0


def rpc(method: str, params: list[Any]) -> Any:
    global _rpc_id
    _rpc_id += 1
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}
    ).encode()
    req = Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    if "error" in d:
        return None
    return d.get("result")


def classify_code(code_hex: str | None) -> str:
    if not code_hex or code_hex in ("0x", "0X"):
        return "eoa"
    c = code_hex.lower().replace("0x", "")
    if c.startswith("ef0100"):
        return "eip7702"
    return "contract"


def true_fifo(events: list[tuple[float, str, float, float]]) -> dict[str, float]:
    """events sorted by ts: (ts, side, weth_amt, meme_amt).
    Returns realized cost/rev WETH on FIFO basis using meme amount as the
    fungible unit and per-event price = weth_amt / meme_amt."""
    queue: list[list[float]] = []
    realized_cost = 0.0
    realized_rev = 0.0
    n_pairs = 0
    n_wins = 0
    pair_pnls: list[float] = []
    for ts, side, weth_amt, meme_amt in events:
        if meme_amt <= 0 or weth_amt <= 0:
            continue
        if side == "BUY":
            queue.append([meme_amt, weth_amt / meme_amt])
        else:  # SELL
            remaining = meme_amt
            sell_price = weth_amt / meme_amt
            sell_pnl = 0.0
            sell_cost = 0.0
            while remaining > 1e-15 and queue:
                buy_amt, buy_price = queue[0]
                take = min(buy_amt, remaining)
                cost_chunk = take * buy_price
                rev_chunk = take * sell_price
                realized_cost += cost_chunk
                realized_rev += rev_chunk
                sell_cost += cost_chunk
                sell_pnl += rev_chunk - cost_chunk
                buy_amt -= take
                remaining -= take
                if buy_amt <= 1e-15:
                    queue.pop(0)
                else:
                    queue[0][0] = buy_amt
            if remaining < meme_amt - 1e-15:
                n_pairs += 1
                if sell_pnl > 0:
                    n_wins += 1
                if sell_cost > 0:
                    pair_pnls.append(sell_pnl / sell_cost)
    return {
        "realized_cost_weth": realized_cost,
        "realized_rev_weth": realized_rev,
        "net_realized_weth": realized_rev - realized_cost,
        "n_pairs": n_pairs,
        "n_wins": n_wins,
        "win_rate": (n_wins / n_pairs) if n_pairs > 0 else 0.0,
        "pair_pnls": pair_pnls,
    }


def trimmed_mean(values: list[float], trim: float = 0.1) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    k = int(n * trim)
    s = s[k : n - k] if n - 2 * k > 0 else s
    return sum(s) / len(s)


def main() -> None:
    cand_csv = DIAG_DIR / "sprint044_phase3_authorstyle_filtered.csv"
    if not cand_csv.exists():
        raise SystemExit(f"missing {cand_csv}")
    with cand_csv.open() as f:
        cands = list(csv.DictReader(f))

    surviving = [r for r in cands if r["recipient"].lower() not in KNOWN_BOTS_OR_ROUTERS]

    addr_in = ",".join(repr(c["recipient"]) for c in surviving)
    stables_in = (
        "'0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',"
        "'0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca',"
        "'0x50c5725949a6f0c72e6c4a641f24049a917db0cb',"
        f"'{WETH}'"
    )

    sql = f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa,
    lower(token0_address) AS t0, lower(token1_address) AS t1,
    toUInt32(fee_bps) AS fee_bps,
    multiIf(lower(token0_address)='{WETH}',1,0) AS weth_is_token0
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({stables_in})
         AND lower(token1_address) IN ({stables_in}))
)
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
  lower(s.tx_hash) AS tx_hash,
  lower(s.sender) AS pool_sender,
  toUInt32(mp.fee_bps) AS fee_bps
FROM eatlq.uniswap_v3_swaps s
INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-05-01 00:00:00')
  AND lower(s.recipient) IN ({addr_in})
ORDER BY s.recipient, meme_token, ts
"""
    t0 = time.time()
    swap_rows = ch(sql)
    fetch_elapsed = time.time() - t0

    grouped: dict[str, dict[str, list[tuple[float, str, float, float]]]] = {}
    sample_tx: dict[str, str] = {}
    pool_senders: dict[str, set[str]] = {}
    fee_dist: dict[str, dict[int, int]] = {}
    for r in swap_rows:
        recip = r["recipient"]
        token = r["meme_token"]
        ts = float(r["ts"])
        side = r["side"]
        weth_amt = float(r["weth_amt"])
        meme_amt = float(r["meme_amt"])
        grouped.setdefault(recip, {}).setdefault(token, []).append(
            (ts, side, weth_amt, meme_amt)
        )
        if recip not in sample_tx:
            sample_tx[recip] = r["tx_hash"]
        pool_senders.setdefault(recip, set()).add(r["pool_sender"])
        fee_dist.setdefault(recip, {}).setdefault(int(r["fee_bps"]), 0)
        fee_dist[recip][int(r["fee_bps"])] += 1

    final_rows: list[dict[str, Any]] = []
    for recip in grouped:
        agg_cost = 0.0
        agg_rev = 0.0
        n_pairs_total = 0
        n_wins_total = 0
        pair_pnls: list[float] = []
        first_ts = float("inf")
        last_ts = 0.0
        n_buys = 0
        n_sells = 0
        n_tokens_with_rt = 0
        for token, ev in grouped[recip].items():
            ev.sort()
            res = true_fifo(ev)
            agg_cost += res["realized_cost_weth"]
            agg_rev += res["realized_rev_weth"]
            n_pairs_total += res["n_pairs"]
            n_wins_total += res["n_wins"]
            pair_pnls.extend(res["pair_pnls"])
            if res["n_pairs"] > 0:
                n_tokens_with_rt += 1
            for ts, side, _, _ in ev:
                first_ts = min(first_ts, ts)
                last_ts = max(last_ts, ts)
                if side == "BUY":
                    n_buys += 1
                else:
                    n_sells += 1

        net = agg_rev - agg_cost
        span = max(1.0, (last_ts - first_ts) / SECS_PER_DAY) if first_ts != float("inf") else 1.0
        usd = net * ETH_USD
        per_day = usd / span
        wr = (n_wins_total / n_pairs_total) if n_pairs_total > 0 else 0.0
        tm = trimmed_mean(pair_pnls)

        final_rows.append(
            {
                "recipient": recip,
                "tokens_traded": len(grouped[recip]),
                "tokens_with_round_trip": n_tokens_with_rt,
                "fifo_pairs": n_pairs_total,
                "fifo_wins": n_wins_total,
                "fifo_win_rate": round(wr, 4),
                "fifo_pair_trimmed_mean_return": round(tm, 4),
                "fifo_realized_cost_weth": round(agg_cost, 6),
                "fifo_realized_rev_weth": round(agg_rev, 6),
                "fifo_net_realized_weth": round(net, 6),
                "fifo_net_realized_usd_est": round(usd, 2),
                "active_span_days": round(span, 2),
                "usd_per_active_day": round(per_day, 2),
                "n_buys": n_buys,
                "n_sells": n_sells,
                "n_distinct_pool_senders_routers": len(pool_senders.get(recip, set())),
                "fee_dist_500_3k_10k": "/".join(
                    str(fee_dist.get(recip, {}).get(f, 0)) for f in (500, 3000, 10000)
                ),
                "first_active": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.gmtime(first_ts)
                ),
                "last_active": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.gmtime(last_ts)
                ),
                "sample_tx_hash": sample_tx.get(recip),
            }
        )

    final_rows.sort(key=lambda r: r["fifo_net_realized_weth"], reverse=True)

    rpc_results: dict[str, dict[str, Any]] = {}
    for row in final_rows[:40]:
        addr = row["recipient"]
        try:
            code = rpc("eth_getCode", [addr, "latest"])
        except Exception:
            code = None
        cls = classify_code(code)
        impl = None
        if cls == "eip7702" and code and len(code) >= 46:
            impl = "0x" + code.lower().replace("0x", "")[6:46]
        try:
            tx = rpc("eth_getTransactionByHash", [row["sample_tx_hash"]]) if row["sample_tx_hash"] else None
        except Exception:
            tx = None
        tx_from = (tx or {}).get("from") if tx else None
        tx_to = (tx or {}).get("to") if tx else None
        max_priority = None
        if tx and isinstance(tx, dict):
            mp = tx.get("maxPriorityFeePerGas")
            if mp is not None:
                try:
                    max_priority = int(mp, 16)
                except Exception:
                    max_priority = None
        rpc_results[addr] = {
            "code_class": cls,
            "code_prefix": (code or "0x")[:18] if code else None,
            "eip7702_impl": impl,
            "sample_tx_from": (tx_from or "").lower() if tx_from else None,
            "sample_tx_to": (tx_to or "").lower() if tx_to else None,
            "sample_max_priority_fee_wei": max_priority,
            "is_self_caller": (
                bool(tx_from and tx_from.lower() == addr.lower())
            ),
        }

    for row in final_rows:
        info = rpc_results.get(row["recipient"], {})
        row.update(
            {
                "code_class": info.get("code_class", "?"),
                "eip7702_impl": info.get("eip7702_impl"),
                "sample_tx_from": info.get("sample_tx_from"),
                "sample_tx_to": info.get("sample_tx_to"),
                "sample_max_priority_fee_wei": info.get("sample_max_priority_fee_wei"),
                "is_self_caller": info.get("is_self_caller"),
            }
        )

    out_csv = DIAG_DIR / "sprint044_global_signer_candidates.csv"
    cols = list(final_rows[0].keys()) if final_rows else []
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in final_rows:
            w.writerow(r)

    positive_control_row = {
        "recipient": "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a",
        "tokens_traded": 12,
        "tokens_with_round_trip": 12,
        "fifo_pairs": 23,
        "fifo_wins": 20,
        "fifo_win_rate": 0.8696,
        "fifo_pair_trimmed_mean_return": 0.12742,
        "fifo_realized_cost_weth": None,
        "fifo_realized_rev_weth": None,
        "fifo_net_realized_weth": 0.01281,
        "fifo_net_realized_usd_est": 44.83,
        "active_span_days": 14.18,
        "usd_per_active_day": 3.16,
        "n_buys": 21,
        "n_sells": 23,
        "n_distinct_pool_senders_routers": 1,
        "fee_dist_500_3k_10k": "0/0/44",
        "first_active": "2026-02-14 13:30:21",
        "last_active": "2026-02-28 17:41:23",
        "sample_tx_hash": "0xc3fc633d7744938fbe79351a1b499ad31b487c4d21db187b75cad0c193a792d6",
        "code_class": "eip7702",
        "eip7702_impl": "0xa845c74344fc9405b1fcf712f04668979573c1bf",
        "sample_tx_from": "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a",
        "sample_tx_to": "0x59c7c832e96d2568bea6db468c1aadcbbda08a52",
        "sample_max_priority_fee_wei": 0,
        "is_self_caller": True,
        "lane": "B_velora_routed_known_positive_control",
    }
    for r in final_rows:
        r["lane"] = "A_direct_v3_recipient"

    summary: dict[str, Any] = {
        "sprint": "044_phase4",
        "fetch_elapsed_seconds": round(fetch_elapsed, 2),
        "n_swap_rows_pulled": len(swap_rows),
        "n_candidates_after_known_router_exclusion": len(surviving),
        "n_candidates_with_fifo_results": len(final_rows),
        "rpc_calls_used": _rpc_id,
        "rpc_endpoint_host": "orbital-purple-sea.base-mainnet.quiknode.pro",
        "user_priors": {
            "author_dev_start": "2026-01 (USER_PRIOR)",
            "author_active_window": "2026-01/02 → 2026-05 (USER_PRIOR)",
            "claimed_usd_per_day": 500,
            "may_2026_data_in_ch": False,
        },
        "ch_data_coverage": {
            "uniswap_v3_swaps_t_max": "2026-04-30 02:18:05",
            "erc20_transfers_useful": False,
            "erc20_transfers_indexed_tokens": 2,
        },
        "n_with_net_usd_ge_5k": sum(
            1 for r in final_rows if r["fifo_net_realized_usd_est"] >= 5000
        ),
        "n_with_usd_per_day_ge_200": sum(
            1 for r in final_rows if r["usd_per_active_day"] >= 200
        ),
        "n_with_usd_per_day_ge_500": sum(
            1 for r in final_rows if r["usd_per_active_day"] >= 500
        ),
        "top10_by_fifo_net_realized_weth": final_rows[:10],
        "top10_by_usd_per_active_day": sorted(
            final_rows, key=lambda r: r["usd_per_active_day"], reverse=True
        )[:10],
        "positive_control_0x7e49": positive_control_row,
        "all_candidates_count": len(final_rows),
    }

    (DIAG_DIR / "sprint044_author_scale_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    cluster_rows: list[dict[str, Any]] = []
    summary["cluster_recon_status"] = (
        "cluster reconstruction blocked by erc20_transfers index gap "
        "(only 2 tokens indexed; WETH absent). RPC trace fan-out budget "
        "(< 200 calls) is too small to enumerate funding sources for all "
        "candidates."
    )
    out_clusters = DIAG_DIR / "sprint044_operator_clusters.csv"
    cluster_cols = [
        "cluster_id",
        "members",
        "rationale",
        "shared_evidence",
        "members_total_net_realized_weth",
        "members_total_usd_per_day_est",
        "status",
    ]
    cluster_rows.append(
        {
            "cluster_id": "C0_data_constrained",
            "members": "n/a",
            "rationale": "no_funding_graph_due_to_missing_weth_transfer_index",
            "shared_evidence": "0",
            "members_total_net_realized_weth": 0,
            "members_total_usd_per_day_est": 0,
            "status": "DEFERRED_pending_RPC_trace_or_external_indexing",
        }
    )
    with out_clusters.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cluster_cols)
        w.writeheader()
        for r in cluster_rows:
            w.writerow(r)

    print(
        f"swap_rows={len(swap_rows)} candidates={len(final_rows)} "
        f"rpc_calls={_rpc_id} elapsed_fetch={fetch_elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
