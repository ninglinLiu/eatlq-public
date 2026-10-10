"""Sprint044 Phase 2A — Direct-V3 recipient aggregation (Jan-Apr 2026).

Reads `uniswap_v3_swaps` joined with `discovered_pools` (V3 + WETH-leg + meme).
Excludes a curated known-router set so router contracts don't pollute the
candidate universe.

Output:
  mvp/diagnostics/sprint044_lane_a_recipients.csv
  mvp/diagnostics/sprint044_lane_a_summary.json
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_bytes_before_external_sort=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=240"
)

KNOWN_ROUTERS = sorted({
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52",  # Velora v5 Augustus
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd",  # alt aggregator (Sprint041)
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap SwapRouter02 Base
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap Universal Router (Base)
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router v2
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0x6a000f20005980200259b80c5102003040001068",  # bebop
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
    "0x32400084c286cf3e17e7b677ea9583e60a000324",  # zkSync placeholder (extra noise)
    "0xb2cb6a04abc88b1ab394feec22ec43dd2b67f4ce",  # OKX dex placeholder
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # KyberSwap MetaAggregationRouterV2
    "0x0000000000001ff3684f28c67538d4d072c22734",  # MEV-related contract
    "0xa6b71e26c5e0845f74c812102ca7114b6a896ab2",  # Safe singleton (multisig)
    "0x4200000000000000000000000000000000000006",  # WETH itself (defensive)
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0x103b86a7fe783dc76086c6e03f3ce74cbcc4bd12",  # frequent aggregator from Sprint039B
    "0x498581ff718922c3f8e6a244956af099b2652b2b",  # frequent aggregator from Sprint039B
})

STABLES = sorted({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    WETH,
})

POSITIVE_CONTROL = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"


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


def main() -> None:
    t0 = time.time()
    routers_in = ",".join(repr(a) for a in KNOWN_ROUTERS)
    stables_in = ",".join(repr(a) for a in STABLES)

    # Aggregate Jan-Apr 2026 V3 WETH-leg meme swaps by recipient (proxy for end-user).
    # We keep router exclusion at recipient level, NOT at sender level (sender = pool's
    # msg.sender = router most of the time; that's expected).
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
  count() AS n_swaps,
  uniqExact(s.tx_hash) AS n_unique_tx,
  uniqExact(lower(s.pool_address)) AS n_pools,
  uniqExact(if(mp.weth_is_token0=1, mp.t1, mp.t0)) AS n_meme_tokens,
  uniqExact(lower(s.sender)) AS n_distinct_pool_senders,
  countIf(mp.fee_bps=500) AS n_fee500,
  countIf(mp.fee_bps=3000) AS n_fee3k,
  countIf(mp.fee_bps=10000) AS n_fee10k,
  sum(multiIf(mp.weth_is_token0=1,
       abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
       abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18)) AS gross_weth,
  median(multiIf(mp.weth_is_token0=1,
       abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
       abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18)) AS p50_weth,
  quantile(0.25)(multiIf(mp.weth_is_token0=1,
       abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
       abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18)) AS p25_weth,
  quantile(0.75)(multiIf(mp.weth_is_token0=1,
       abs(toFloat64(eatlq_hex_to_int256(s.amount0_raw)))/1e18,
       abs(toFloat64(eatlq_hex_to_int256(s.amount1_raw)))/1e18)) AS p75_weth,
  toDateTime(min(s.block_time)) AS first_active,
  toDateTime(max(s.block_time)) AS last_active,
  uniqExact(toDate(s.block_time)) AS active_days,
  countIf(
    (mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
    (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8')
  ) AS n_buys_proxy,
  countIf(
    NOT ((mp.weth_is_token0=0 AND lower(substring(s.amount0_raw,3,1))>='8') OR
         (mp.weth_is_token0=1 AND lower(substring(s.amount0_raw,3,1))<'8'))
  ) AS n_sells_proxy
FROM eatlq.uniswap_v3_swaps AS s
INNER JOIN meme_pools AS mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-05-01 00:00:00')
  AND lower(s.recipient) NOT IN ({routers_in})
GROUP BY recipient
HAVING n_swaps >= 4
ORDER BY gross_weth DESC
LIMIT 5000
"""
    rows = ch(sql)
    elapsed = time.time() - t0

    out_csv = DIAG_DIR / "sprint044_lane_a_recipients.csv"
    if rows:
        cols = list(rows[0].keys())
    else:
        cols = []
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary: dict[str, Any] = {
        "sprint": "044_phase2_lane_a",
        "n_recipients_returned": len(rows),
        "elapsed_seconds": round(elapsed, 2),
        "filter_window": "2026-01-01 → 2026-05-01",
        "fee_lanes": "500/3000/10000 (whitepaper canon)",
        "router_exclusion_count": len(KNOWN_ROUTERS),
        "row_cap_500k_breached": False,
        "positive_control_present": False,
        "top10_preview": rows[:10],
    }
    for r in rows:
        if r["recipient"].lower() == POSITIVE_CONTROL:
            summary["positive_control_present"] = True
            summary["positive_control_row"] = r
            break

    (DIAG_DIR / "sprint044_lane_a_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    print(f"wrote {out_csv} (n={len(rows)}, elapsed={elapsed:.1f}s)")


if __name__ == "__main__":
    main()
