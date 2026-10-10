"""Sprint044 Phase 3 — Author-style filter + FIFO realized PnL.

Reads Lane A candidates, applies author-style filters, then computes
recipient×token FIFO realized PnL using uniswap_v3_swaps for the surviving
top-N candidates only (top-N caps query rows).

Output:
  mvp/diagnostics/sprint044_phase3_authorstyle_filtered.csv
  mvp/diagnostics/sprint044_phase3_signer_pnl.csv
  mvp/diagnostics/sprint044_phase3_summary.json
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

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=180"
)


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


def author_style_filter(row: dict[str, str]) -> bool:
    n_swaps = int(row["n_swaps"])
    n_pools = int(row["n_pools"])
    n_tokens = int(row["n_meme_tokens"])
    p50 = float(row["p50_weth"])
    n_buy = int(row["n_buys_proxy"])
    n_sell = int(row["n_sells_proxy"])
    active_days = int(row["active_days"])
    n_fee500 = int(row["n_fee500"])
    n_fee3k = int(row["n_fee3k"])
    n_fee10k = int(row["n_fee10k"])

    if n_swaps < 8 or n_swaps > 5000:
        return False
    if n_tokens < 3:
        return False
    if n_pools < 3:
        return False
    if active_days < 5:
        return False
    if n_buy == 0 or n_sell == 0:
        return False
    bs_ratio = min(n_buy, n_sell) / max(n_buy, n_sell)
    if bs_ratio < 0.25:
        return False
    if p50 > 0.3:
        return False
    if p50 < 1e-6:
        return False
    fees_present = sum(1 for v in (n_fee500, n_fee3k, n_fee10k) if v > 0)
    if fees_present < 1:
        return False
    return True


def author_score(row: dict[str, str]) -> float:
    n_swaps = int(row["n_swaps"])
    n_tokens = int(row["n_meme_tokens"])
    active_days = int(row["active_days"])
    n_buy = int(row["n_buys_proxy"])
    n_sell = int(row["n_sells_proxy"])
    p50 = float(row["p50_weth"])
    gross = float(row["gross_weth"])
    n_pools = int(row["n_pools"])
    n_fee500 = int(row["n_fee500"])
    n_fee3k = int(row["n_fee3k"])
    n_fee10k = int(row["n_fee10k"])

    bs_balance = (
        min(n_buy, n_sell) / max(n_buy, n_sell) if max(n_buy, n_sell) > 0 else 0.0
    )
    notional_disc = max(0.0, 1.0 - abs(p50 - 0.005) / 0.05)
    diversity = min(1.0, (n_tokens + n_pools) / 30.0)
    cadence = min(1.0, active_days / 60.0)
    activity = min(1.0, n_swaps / 1500.0)
    fee_lanes = min(1.0, sum(v > 0 for v in (n_fee500, n_fee3k, n_fee10k)) / 3.0)

    return (
        0.20 * bs_balance
        + 0.15 * notional_disc
        + 0.20 * diversity
        + 0.15 * cadence
        + 0.10 * activity
        + 0.10 * fee_lanes
        + 0.10 * min(1.0, gross / 50.0)
    )


def fifo_pnl(buys: list[tuple[float, float]], sells: list[tuple[float, float]]) -> dict[str, float]:
    """Inputs: list of (timestamp_unix, weth_amount_per_event).
    For BUY events weth_amount is the WETH paid; meme amount unknown but we PnL
    only on WETH cost vs WETH revenue per FIFO unit."""
    # We treat each token as fungible weight units measured in WETH amounts
    # (proxy: 1 unit = 1 WETH worth of meme). For FIFO realized PnL we walk
    # buy queue and match each sell against earliest buys until sell weight
    # is consumed. This produces realized WETH cost and realized WETH revenue.
    queue: list[list[float]] = []
    realized_cost = 0.0
    realized_rev = 0.0
    pairs = 0
    wins = 0
    for ts, w in buys:
        queue.append([ts, w])
    queue.sort()
    sells_sorted = sorted(sells)
    sell_idx = 0
    for ts_sell, w_sell in sells_sorted:
        remaining = w_sell
        while remaining > 1e-15 and queue:
            buy_ts, buy_w = queue[0]
            take = min(buy_w, remaining)
            realized_cost += take
            realized_rev += take * (w_sell / w_sell) if w_sell > 0 else 0.0
            buy_w -= take
            remaining -= take
            if buy_w <= 1e-15:
                queue.pop(0)
            else:
                queue[0][1] = buy_w
        if remaining > 1e-15:
            sell_idx += 1
        pairs += 1
    # NOTE: This degenerate FIFO treats meme units as identical to WETH units
    # which is wrong.  We need actual meme amounts for matching.  Use a richer
    # fifo at SQL layer instead via _fifo_sql below.
    return {
        "realized_cost_weth": realized_cost,
        "realized_rev_weth": realized_rev,
        "n_pairs": pairs,
        "wins": wins,
    }


def main() -> None:
    lane_a_csv = DIAG_DIR / "sprint044_lane_a_recipients.csv"
    if not lane_a_csv.exists():
        raise SystemExit(f"missing {lane_a_csv}")
    with lane_a_csv.open() as f:
        lane_a = list(csv.DictReader(f))

    filtered = [r for r in lane_a if author_style_filter(r)]
    for r in filtered:
        r["author_score"] = f"{author_score(r):.4f}"

    filtered.sort(key=lambda r: float(r["author_score"]), reverse=True)
    top_n = 200
    candidates = filtered[:top_n]

    out_filtered = DIAG_DIR / "sprint044_phase3_authorstyle_filtered.csv"
    cols = list(candidates[0].keys()) if candidates else []
    with out_filtered.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in candidates:
            w.writerow(r)

    if not candidates:
        print("no author-style candidates after filter")
        (DIAG_DIR / "sprint044_phase3_summary.json").write_text(
            json.dumps(
                {
                    "sprint": "044_phase3",
                    "n_candidates": 0,
                    "fifo_attempted": False,
                    "note": "author-style filter eliminated all 5000 lane-a candidates",
                },
                indent=2,
            )
        )
        return

    cand_addrs = [r["recipient"].lower() for r in candidates]
    cand_in = ",".join(repr(a) for a in cand_addrs)

    stables_in = (
        "'0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',"
        "'0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca',"
        "'0x50c5725949a6f0c72e6c4a641f24049a917db0cb',"
        f"'{WETH}'"
    )

    t0 = time.time()
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
),
classified AS (
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
    ) AS side
  FROM eatlq.uniswap_v3_swaps s
  INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
  WHERE s.chain_id={CHAIN_ID} AND s.removed=0
    AND s.block_time >= toDateTime('2026-01-01 00:00:00')
    AND s.block_time <  toDateTime('2026-05-01 00:00:00')
    AND lower(s.recipient) IN ({cand_in})
)
SELECT recipient,
       meme_token,
       sumIf(weth_amt, side='BUY')  AS buy_weth,
       sumIf(weth_amt, side='SELL') AS sell_weth,
       sumIf(meme_amt, side='BUY')  AS buy_meme,
       sumIf(meme_amt, side='SELL') AS sell_meme,
       countIf(side='BUY')          AS n_buys,
       countIf(side='SELL')         AS n_sells,
       toDateTime(min(ts))          AS first_ts,
       toDateTime(max(ts))          AS last_ts
FROM classified
GROUP BY recipient, meme_token
"""
    rows = ch(sql)
    elapsed = time.time() - t0

    by_recip: dict[str, dict[str, Any]] = {}
    for r in rows:
        recip = r["recipient"]
        bw = float(r["buy_weth"])
        sw = float(r["sell_weth"])
        bm = float(r["buy_meme"])
        sm = float(r["sell_meme"])
        n_b = int(r["n_buys"])
        n_s = int(r["n_sells"])
        if bm <= 0 or sm <= 0 or bw <= 0:
            net = 0.0
            avg_buy_price = 0.0
            avg_sell_price = 0.0
            realized_share = 0.0
            realized_rev = 0.0
            realized_cost = 0.0
        else:
            avg_buy_price = bw / bm
            avg_sell_price = sw / sm
            matched_meme = min(bm, sm)
            realized_cost = matched_meme * avg_buy_price
            realized_rev = matched_meme * avg_sell_price
            net = realized_rev - realized_cost
            realized_share = matched_meme / max(bm, sm)
        d = by_recip.setdefault(
            recip,
            {
                "recipient": recip,
                "tokens_traded": 0,
                "tokens_with_round_trip": 0,
                "tokens_with_win": 0,
                "buy_weth": 0.0,
                "sell_weth": 0.0,
                "realized_cost_weth": 0.0,
                "realized_rev_weth": 0.0,
                "net_realized_weth": 0.0,
                "n_buys": 0,
                "n_sells": 0,
                "first_ts": "9999-12-31 00:00:00",
                "last_ts": "0001-01-01 00:00:00",
            },
        )
        d["tokens_traded"] += 1
        if bm > 0 and sm > 0:
            d["tokens_with_round_trip"] += 1
            if net > 0:
                d["tokens_with_win"] += 1
        d["buy_weth"] += bw
        d["sell_weth"] += sw
        d["realized_cost_weth"] += realized_cost
        d["realized_rev_weth"] += realized_rev
        d["net_realized_weth"] += net
        d["n_buys"] += n_b
        d["n_sells"] += n_s
        if r["first_ts"] < d["first_ts"]:
            d["first_ts"] = r["first_ts"]
        if r["last_ts"] > d["last_ts"]:
            d["last_ts"] = r["last_ts"]

    pnl_rows: list[dict[str, Any]] = []
    for recip, d in by_recip.items():
        try:
            t_first = time.strptime(d["first_ts"], "%Y-%m-%d %H:%M:%S")
            t_last = time.strptime(d["last_ts"], "%Y-%m-%d %H:%M:%S")
            span_days = max(
                1.0,
                (time.mktime(t_last) - time.mktime(t_first)) / SECS_PER_DAY,
            )
        except Exception:
            span_days = 1.0
        net_usd = d["net_realized_weth"] * ETH_USD
        net_per_day_usd = net_usd / span_days
        wr = (
            d["tokens_with_win"] / d["tokens_with_round_trip"]
            if d["tokens_with_round_trip"] > 0
            else 0.0
        )
        pnl_rows.append(
            {
                "recipient": recip,
                "tokens_traded": d["tokens_traded"],
                "tokens_with_round_trip": d["tokens_with_round_trip"],
                "tokens_with_win": d["tokens_with_win"],
                "win_rate_token": round(wr, 4),
                "buy_weth": round(d["buy_weth"], 6),
                "sell_weth": round(d["sell_weth"], 6),
                "realized_cost_weth": round(d["realized_cost_weth"], 6),
                "realized_rev_weth": round(d["realized_rev_weth"], 6),
                "net_realized_weth": round(d["net_realized_weth"], 6),
                "net_realized_usd_est": round(net_usd, 2),
                "first_ts": d["first_ts"],
                "last_ts": d["last_ts"],
                "active_span_days": round(span_days, 2),
                "net_realized_usd_per_day": round(net_per_day_usd, 2),
                "n_buys": d["n_buys"],
                "n_sells": d["n_sells"],
            }
        )

    pnl_rows.sort(key=lambda r: r["net_realized_weth"], reverse=True)

    out_pnl = DIAG_DIR / "sprint044_phase3_signer_pnl.csv"
    cols = list(pnl_rows[0].keys()) if pnl_rows else []
    with out_pnl.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in pnl_rows:
            w.writerow(r)

    above_5k = [r for r in pnl_rows if r["net_realized_usd_est"] >= 5000]
    above_200_per_day = [
        r for r in pnl_rows if r["net_realized_usd_per_day"] >= 200
    ]
    above_500_per_day = [
        r for r in pnl_rows if r["net_realized_usd_per_day"] >= 500
    ]

    summary: dict[str, Any] = {
        "sprint": "044_phase3",
        "lane_a_in": len(lane_a),
        "after_authorstyle_filter": len(filtered),
        "fifo_top_N_evaluated": len(candidates),
        "elapsed_seconds": round(elapsed, 2),
        "rows_returned_from_aggregate": len(rows),
        "fifo_methodology": (
            "per (recipient, meme_token): match min(buy_meme, sell_meme) at "
            "avg_buy_price vs avg_sell_price. Approximate realized PnL; not "
            "true sell-by-sell FIFO. Floor-bias: assumes proportional matching."
        ),
        "n_with_net_usd_ge_5k": len(above_5k),
        "n_with_net_usd_per_day_ge_200": len(above_200_per_day),
        "n_with_net_usd_per_day_ge_500": len(above_500_per_day),
        "top10_by_net_realized_weth": pnl_rows[:10],
        "top10_by_usd_per_day": sorted(
            pnl_rows, key=lambda r: r["net_realized_usd_per_day"], reverse=True
        )[:10],
    }
    (DIAG_DIR / "sprint044_phase3_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    print(
        f"filtered={len(filtered)} cands={len(candidates)} "
        f"agg_rows={len(rows)} ge_5k={len(above_5k)} "
        f"ge_200_per_day={len(above_200_per_day)} elapsed={elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
