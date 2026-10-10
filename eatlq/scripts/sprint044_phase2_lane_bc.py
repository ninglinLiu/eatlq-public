"""Sprint044 Phase 2B+C — Velora-routed WETH end-user discovery.

We use eatlq.erc20_transfers to find addresses that:
  - receive WETH FROM Velora (post-sell unwrap)  (Lane B = Velora customers selling)
  - send WETH TO Velora (pre-buy fund)           (Lane C = Velora customers buying)

We also include a few additional public routers as senders/receivers, and we
exclude these same routers from the candidate pool so we don't count them as
end users.

Output:
  mvp/diagnostics/sprint044_lane_b_velora_recipients.csv
  mvp/diagnostics/sprint044_lane_c_velora_senders.csv
  mvp/diagnostics/sprint044_lane_bc_summary.json
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
    "&max_memory_usage=8000000000"
    "&max_execution_time=120"
)

ROUTERS = sorted({
    "0x59c7c832e96d2568bea6db468c1aadcbbda08a52",  # Velora v5 Augustus
    "0x9e0a538749a486bbf127f8848a6f2cf4e1e92dbd",  # alt aggregator
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap SwapRouter02
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap Universal Router
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router v2
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # KyberSwap
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    WETH,
})


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
    with urlopen(req, timeout=180) as r:
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
    routers_in = ",".join(repr(a) for a in ROUTERS)
    velora = "0x59c7c832e96d2568bea6db468c1aadcbbda08a52"
    aggregators_for_lane = (
        "'0x59c7c832e96d2568bea6db468c1aadcbbda08a52',"
        "'0x6ff5693b99212da76ad316178a184ab56d299b43',"
        "'0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad',"
        "'0x2626664c2603336e57b271c5c0b26f421741e481'"
    )

    t0 = time.time()
    sql_b = f"""
SELECT
  lower(recipient) AS recipient,
  count() AS n_inflows,
  uniqExact(lower(sender)) AS n_aggregators,
  sum(toFloat64(eatlq_hex_to_int256(amount_raw))/1e18) AS total_weth_in,
  median(toFloat64(eatlq_hex_to_int256(amount_raw))/1e18) AS p50_weth_in,
  toDateTime(min(block_time)) AS first_in,
  toDateTime(max(block_time)) AS last_in,
  uniqExact(toDate(block_time)) AS active_days_in
FROM eatlq.erc20_transfers
WHERE chain_id={CHAIN_ID} AND removed=0
  AND lower(token_address)='{WETH}'
  AND lower(sender) IN ({aggregators_for_lane})
  AND lower(recipient) NOT IN ({routers_in})
  AND block_time >= toDateTime('2026-02-23 00:00:00')
  AND block_time <  toDateTime('2026-05-01 00:00:00')
GROUP BY recipient
HAVING n_inflows >= 2
ORDER BY total_weth_in DESC
LIMIT 5000
"""
    rows_b = ch(sql_b)

    sql_c = f"""
SELECT
  lower(sender) AS sender,
  count() AS n_outflows,
  uniqExact(lower(recipient)) AS n_aggregators,
  sum(toFloat64(eatlq_hex_to_int256(amount_raw))/1e18) AS total_weth_out,
  median(toFloat64(eatlq_hex_to_int256(amount_raw))/1e18) AS p50_weth_out,
  toDateTime(min(block_time)) AS first_out,
  toDateTime(max(block_time)) AS last_out,
  uniqExact(toDate(block_time)) AS active_days_out
FROM eatlq.erc20_transfers
WHERE chain_id={CHAIN_ID} AND removed=0
  AND lower(token_address)='{WETH}'
  AND lower(recipient) IN ({aggregators_for_lane})
  AND lower(sender) NOT IN ({routers_in})
  AND block_time >= toDateTime('2026-02-23 00:00:00')
  AND block_time <  toDateTime('2026-05-01 00:00:00')
GROUP BY sender
HAVING n_outflows >= 2
ORDER BY total_weth_out DESC
LIMIT 5000
"""
    rows_c = ch(sql_c)
    elapsed = time.time() - t0

    out_b = DIAG_DIR / "sprint044_lane_b_velora_recipients.csv"
    out_c = DIAG_DIR / "sprint044_lane_c_velora_senders.csv"
    for path, rows in ((out_b, rows_b), (out_c, rows_c)):
        cols = list(rows[0].keys()) if rows else []
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    summary: dict[str, Any] = {
        "sprint": "044_phase2_lane_bc",
        "elapsed_seconds": round(elapsed, 2),
        "lane_b_n": len(rows_b),
        "lane_c_n": len(rows_c),
        "window": "2026-02-23 → 2026-05-01 (erc20_transfers coverage)",
        "aggregators_filtered_in": [
            velora,
            "0x6ff5693b99212da76ad316178a184ab56d299b43",
            "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
            "0x2626664c2603336e57b271c5c0b26f421741e481",
        ],
        "routers_excluded_count": len(ROUTERS),
        "lane_b_top10": rows_b[:10],
        "lane_c_top10": rows_c[:10],
        "positive_control_in_b": any(
            r.get("recipient", "").lower()
            == "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
            for r in rows_b
        ),
        "positive_control_in_c": any(
            r.get("sender", "").lower()
            == "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
            for r in rows_c
        ),
    }
    if summary["positive_control_in_b"]:
        for r in rows_b:
            if r["recipient"].lower() == "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a":
                summary["positive_control_b_row"] = r
                summary["positive_control_b_rank"] = rows_b.index(r) + 1
                break
    if summary["positive_control_in_c"]:
        for r in rows_c:
            if r["sender"].lower() == "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a":
                summary["positive_control_c_row"] = r
                summary["positive_control_c_rank"] = rows_c.index(r) + 1
                break

    (DIAG_DIR / "sprint044_lane_bc_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    print(f"lane B n={len(rows_b)} lane C n={len(rows_c)} elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
