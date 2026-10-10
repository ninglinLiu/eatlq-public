"""Sprint044 — Global Author-Scale Operator Rediscovery Preflight (read-only).

This module runs **only counting / schema** queries against ClickHouse so we can
budget the Sprint044 rediscovery before issuing any heavy aggregate.

Outputs:
  mvp/diagnostics/sprint044_preflight.json
"""

from __future__ import annotations

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
DIAG_DIR.mkdir(parents=True, exist_ok=True)

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")

_CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_bytes_before_external_sort=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=120"
)


def _ch(sql: str, fmt: str = "TSVWithNames") -> list[dict[str, str]]:
    url = CH_URL.rstrip("/") + f"/?{_CH_SETTINGS}"
    body = (sql + f"\nFORMAT {fmt}").encode()
    req = Request(
        url,
        data=body,
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
        vals = line.split("\t")
        rows.append(dict(zip(headers, vals)))
    return rows


def _scalar(sql: str) -> Any:
    rows = _ch(sql)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def main() -> None:
    out: dict[str, Any] = {
        "sprint": "044_preflight",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": "schema_and_count_only_no_heavy_aggregate",
        "abort_thresholds": {
            "row_cap_per_query": 500_000,
            "counterparty_cap": 200,
            "rpc_trace_cap": 20,
        },
    }

    out["tables_in_eatlq"] = [
        r["name"]
        for r in _ch("SELECT name FROM system.tables WHERE database='eatlq' ORDER BY name")
    ]

    schemas: dict[str, list[dict[str, str]]] = {}
    for table in [
        "uniswap_v3_swaps",
        "discovered_pools",
        "erc20_transfers",
    ]:
        if table not in out["tables_in_eatlq"]:
            schemas[table] = [{"missing": "true"}]
            continue
        schemas[table] = _ch(
            "SELECT name, type FROM system.columns "
            f"WHERE database='eatlq' AND table='{table}' ORDER BY position"
        )
    out["table_schemas"] = schemas

    swaps_total = _scalar(
        f"SELECT count() FROM eatlq.uniswap_v3_swaps "
        f"WHERE chain_id={CHAIN_ID} AND removed=0"
    )
    swaps_jan_may = _scalar(
        "SELECT count() FROM eatlq.uniswap_v3_swaps "
        f"WHERE chain_id={CHAIN_ID} AND removed=0 "
        "AND block_time >= toDateTime('2026-01-01 00:00:00') "
        "AND block_time <  toDateTime('2026-06-01 00:00:00')"
    )
    swaps_meta = _ch(
        "SELECT toDateTime(min(block_time)) AS t_min, "
        "       toDateTime(max(block_time)) AS t_max, "
        "       count() AS n FROM eatlq.uniswap_v3_swaps "
        f"WHERE chain_id={CHAIN_ID} AND removed=0"
    )
    out["uniswap_v3_swaps"] = {
        "row_count_chain": swaps_total,
        "row_count_jan_may_2026": swaps_jan_may,
        "block_time_range": swaps_meta[0] if swaps_meta else {},
    }

    pools_meta = _ch(
        "SELECT count() AS n_pools, "
        "       sum(has_weth_leg=1) AS n_pools_weth, "
        "       sum(has_weth_leg=1 AND lower(pool_type)='v3') AS n_pools_v3_weth "
        f"FROM eatlq.discovered_pools WHERE chain_id={CHAIN_ID}"
    )
    out["discovered_pools"] = pools_meta[0] if pools_meta else {}

    fee_dist = _ch(
        "SELECT toUInt32(fee_bps) AS fee_bps, count() AS n_pools "
        f"FROM eatlq.discovered_pools WHERE chain_id={CHAIN_ID} "
        "AND lower(pool_type)='v3' AND has_weth_leg=1 "
        "GROUP BY fee_bps ORDER BY n_pools DESC"
    )
    out["discovered_pools_v3_weth_fee_dist"] = fee_dist

    pool_types = _ch(
        "SELECT lower(pool_type) AS pool_type, count() AS n "
        f"FROM eatlq.discovered_pools WHERE chain_id={CHAIN_ID} "
        "GROUP BY pool_type ORDER BY n DESC"
    )
    out["discovered_pools_pool_types"] = pool_types

    stables = (
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
        "0x4200000000000000000000000000000000000006",
    )
    stables_sql = ",".join(f"'{s}'" for s in stables)

    universe_count = _scalar(
        f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa,
         toUInt32(fee_bps) AS fee_bps
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({stables_sql})
         AND lower(token1_address) IN ({stables_sql}))
)
SELECT count() FROM eatlq.uniswap_v3_swaps s
INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-06-01 00:00:00')
""".strip()
    )
    out["jan_may_v3_weth_meme_swaps"] = universe_count

    distinct_recipients = _scalar(
        f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({stables_sql})
         AND lower(token1_address) IN ({stables_sql}))
)
SELECT uniqExact(lower(s.recipient)) FROM eatlq.uniswap_v3_swaps s
INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-06-01 00:00:00')
""".strip()
    )
    out["jan_may_v3_weth_meme_distinct_recipients"] = distinct_recipients

    distinct_recipients_with_round_trips = _scalar(
        f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({stables_sql})
         AND lower(token1_address) IN ({stables_sql}))
),
recip_act AS (
  SELECT lower(s.recipient) AS recipient,
         count() AS n,
         uniqExact(lower(s.pool_address)) AS n_pools
  FROM eatlq.uniswap_v3_swaps s
  INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
  WHERE s.chain_id={CHAIN_ID} AND s.removed=0
    AND s.block_time >= toDateTime('2026-01-01 00:00:00')
    AND s.block_time <  toDateTime('2026-06-01 00:00:00')
  GROUP BY recipient
)
SELECT countIf(n >= 4 AND n_pools >= 1) FROM recip_act
""".strip()
    )
    out["jan_may_v3_weth_meme_recipients_min4_swaps"] = distinct_recipients_with_round_trips

    out["per_month_swaps"] = _ch(
        f"""
WITH meme_pools AS (
  SELECT lower(pool_address) AS pa
  FROM eatlq.discovered_pools
  WHERE chain_id={CHAIN_ID}
    AND lower(pool_type)='v3' AND has_weth_leg=1
    AND fee_bps IN (500,3000,10000)
    AND NOT (lower(token0_address) IN ({stables_sql})
         AND lower(token1_address) IN ({stables_sql}))
)
SELECT toStartOfMonth(s.block_time) AS month_utc,
       count() AS n_swaps,
       uniqExact(lower(s.recipient)) AS n_recipients
FROM eatlq.uniswap_v3_swaps s
INNER JOIN meme_pools mp ON lower(s.pool_address)=mp.pa
WHERE s.chain_id={CHAIN_ID} AND s.removed=0
  AND s.block_time >= toDateTime('2026-01-01 00:00:00')
  AND s.block_time <  toDateTime('2026-06-01 00:00:00')
GROUP BY month_utc ORDER BY month_utc
""".strip()
    )

    if "erc20_transfers" in out["tables_in_eatlq"]:
        erc_meta = _ch(
            "SELECT count() AS n, "
            "       toDateTime(min(block_time)) AS t_min, "
            "       toDateTime(max(block_time)) AS t_max "
            "FROM eatlq.erc20_transfers "
            f"WHERE chain_id={CHAIN_ID}"
        )
        out["erc20_transfers"] = erc_meta[0] if erc_meta else {}

    plan = {
        "phase_2_signer_expansion": {
            "kind": "ClickHouse aggregate by recipient (proxy for tx.from since v3_swaps lacks tx.from)",
            "expected_queries": 3,
            "expected_rows_per_query": (
                int(distinct_recipients) if distinct_recipients else None
            ),
            "row_cap_action": (
                "trim by min_swap_count if distinct recipients > 500k"
            ),
            "rpc_calls": 0,
        },
        "phase_3_signer_pnl": {
            "kind": "FIFO realized PnL per recipient (top-N filtered)",
            "expected_queries": 2,
            "expected_rows_per_query": "<= top_N (we will cap at 5_000)",
            "rpc_calls": 0,
        },
        "phase_4_cluster_recon": {
            "kind": "Funding/profit graph join with erc20_transfers",
            "expected_queries": 4,
            "expected_rows_per_query": "<= top_N counterparties (cap 200)",
            "rpc_calls": 0,
            "stop_if": (
                "counterparty fanout >= 200 or shared funding nodes >= 100"
            ),
        },
        "phase_5_scoring": {
            "kind": "Local pandas scoring after CH aggregates",
            "rpc_calls": 0,
        },
        "phase_6_optional_rpc": {
            "kind": "debug_traceTransaction on top 2-3 candidates only",
            "rpc_calls": "<= 20",
            "trigger": "only if Phase 3 finds >=1 candidate with realized PnL >= $5k",
        },
    }
    out["query_plan"] = plan

    out_path = DIAG_DIR / "sprint044_preflight.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
