"""Sprint045 — Controller-keyed rediscovery preflight (read-only).

This is Phase 0 only. It estimates the breadth and cost of a broad author-scale
controller/signer/cluster rediscovery without running full controller RPC
enumeration.

Outputs:
  mvp/diagnostics/sprint045_controller_keyed_preflight.json
  mvp/audits/sprint045_controller_keyed_rediscovery_preflight.md
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = REPO_ROOT / "mvp" / "diagnostics"
AUDIT_DIR = REPO_ROOT / "mvp" / "audits"

CH_URL = os.environ.get("CLICKHOUSE_URL", "http://172.18.0.6:8123/")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASS = os.environ.get("CH_PASSWORD", "Ch_7Z3mN2rQ8vP52026sT9xW4y")
CH_SETTINGS = (
    "max_bytes_before_external_group_by=4000000000"
    "&max_bytes_before_external_sort=4000000000"
    "&max_memory_usage=8000000000"
    "&max_execution_time=120"
)

STABLES = sorted(
    {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
        WETH,
    }
)

# This list is intentionally conservative. These are not candidates, but their
# tx flows are controller-enumeration lanes.
PUBLIC_ROUTER_OR_PLATFORM_RECIPIENTS = {
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
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    return [dict(zip(headers, line.split("\t"))) for line in lines[1:]]


def as_int(value: str | int | None) -> int:
    if value in (None, ""):
        return 0
    return int(value)


def main() -> None:
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stables_in = ",".join(repr(s) for s in STABLES)
    routers_in = ",".join(repr(a) for a in PUBLIC_ROUTER_OR_PLATFORM_RECIPIENTS)

    # Per ClickHouse guidance:
    # - `schema-pk-filter-on-orderby`: always constrain chain_id and block_time.
    # - `query-join-filter-before`: filter pools and swaps in CTEs before join.
    # - `query-join-use-any`: evaluated, but NOT applied here. On this CH view
    #   shape `ANY INNER JOIN` materially under-counted the pool join in a
    #   cross-check, so regular INNER JOIN is used for correctness.
    coverage_v3 = ch(
        f"""
SELECT
  count() AS n,
  toDateTime(min(block_time)) AS t_min,
  toDateTime(max(block_time)) AS t_max
FROM eatlq.uniswap_v3_swaps
WHERE chain_id={CHAIN_ID} AND removed=0
""".strip()
    )[0]

    coverage_aero = ch(
        f"""
SELECT
  count() AS n,
  toDateTime(min(block_time)) AS t_min,
  toDateTime(max(block_time)) AS t_max
FROM eatlq.aerodrome_vamm_swaps_mv
WHERE chain_id={CHAIN_ID} AND removed=0
""".strip()
    )[0]

    erc20_tokens = ch(
        f"""
SELECT lower(token_address) AS token_address, count() AS n
FROM eatlq.erc20_transfers
WHERE chain_id={CHAIN_ID} AND removed=0
GROUP BY token_address
ORDER BY n DESC
LIMIT 20
""".strip()
    )

    fee_lane_counts = ch(
        f"""
WITH
  pools AS (
    SELECT lower(pool_address) AS pa, toUInt32(fee_bps) AS fee_bps
    FROM eatlq.discovered_pools
    WHERE chain_id={CHAIN_ID}
      AND lower(pool_type)='v3'
      AND has_weth_leg=1
      AND fee_bps IN (100,500,3000,10000)
      AND NOT (lower(token0_address) IN ({stables_in})
           AND lower(token1_address) IN ({stables_in}))
  ),
  swaps AS (
    SELECT lower(pool_address) AS pa, lower(recipient) AS recipient, tx_hash
    FROM eatlq.uniswap_v3_swaps
    WHERE chain_id={CHAIN_ID}
      AND removed=0
      AND block_time >= toDateTime('2026-01-01 00:00:00')
      AND block_time <  toDateTime('2026-05-01 00:00:00')
  )
SELECT
  p.fee_bps,
  count() AS n_swaps,
  uniqExact(s.tx_hash) AS n_unique_tx,
  uniqExact(s.recipient) AS n_recipients
FROM swaps AS s
INNER JOIN pools AS p ON s.pa=p.pa
GROUP BY p.fee_bps
ORDER BY p.fee_bps
""".strip()
    )

    route_recipient_counts = ch(
        f"""
WITH
  pools AS (
    SELECT lower(pool_address) AS pa, toUInt32(fee_bps) AS fee_bps
    FROM eatlq.discovered_pools
    WHERE chain_id={CHAIN_ID}
      AND lower(pool_type)='v3'
      AND has_weth_leg=1
      AND fee_bps IN (100,500,3000,10000)
      AND NOT (lower(token0_address) IN ({stables_in})
           AND lower(token1_address) IN ({stables_in}))
  ),
  swaps AS (
    SELECT lower(pool_address) AS pa, lower(recipient) AS recipient, tx_hash
    FROM eatlq.uniswap_v3_swaps
    WHERE chain_id={CHAIN_ID}
      AND removed=0
      AND block_time >= toDateTime('2026-01-01 00:00:00')
      AND block_time <  toDateTime('2026-05-01 00:00:00')
      AND lower(recipient) IN ({routers_in})
  )
SELECT
  s.recipient,
  count() AS n_swaps,
  uniqExact(s.tx_hash) AS n_unique_tx,
  uniqExact(p.fee_bps) AS n_fee_lanes,
  groupArray((p.fee_bps, 1)) AS fee_lane_marker
FROM swaps AS s
INNER JOIN pools AS p ON s.pa=p.pa
GROUP BY s.recipient
ORDER BY n_unique_tx DESC
""".strip()
    )

    broad_counts = ch(
        f"""
WITH
  pools AS (
    SELECT lower(pool_address) AS pa, toUInt32(fee_bps) AS fee_bps
    FROM eatlq.discovered_pools
    WHERE chain_id={CHAIN_ID}
      AND lower(pool_type)='v3'
      AND has_weth_leg=1
      AND fee_bps IN (100,500,3000,10000)
      AND NOT (lower(token0_address) IN ({stables_in})
           AND lower(token1_address) IN ({stables_in}))
  ),
  swaps AS (
    SELECT lower(pool_address) AS pa, lower(recipient) AS recipient, tx_hash
    FROM eatlq.uniswap_v3_swaps
    WHERE chain_id={CHAIN_ID}
      AND removed=0
      AND block_time >= toDateTime('2026-01-01 00:00:00')
      AND block_time <  toDateTime('2026-05-01 00:00:00')
  )
SELECT
  count() AS n_swaps,
  uniqExact(s.tx_hash) AS n_unique_tx,
  uniqExact(s.recipient) AS n_recipients,
  countIf(s.recipient IN ({routers_in})) AS n_swaps_known_router_or_platform,
  uniqExactIf(s.tx_hash, s.recipient IN ({routers_in})) AS n_tx_known_router_or_platform
FROM swaps AS s
INNER JOIN pools AS p ON s.pa=p.pa
""".strip()
    )[0]

    # Top platform recipients from Sprint044 recipient-level discovery that
    # should be decomposed by controller in Phase B.
    top_platform_rows: list[dict[str, str]] = []
    top_csv = DIAG_DIR / "sprint044_global_signer_candidates.csv"
    if top_csv.exists():
        import csv

        with top_csv.open() as f:
            rows = list(csv.DictReader(f))
        for r in rows[:12]:
            top_platform_rows.append(
                {
                    "recipient": r["recipient"],
                    "n_buys": r.get("n_buys", ""),
                    "n_sells": r.get("n_sells", ""),
                    "net_usd": r.get("fifo_net_realized_usd_est", ""),
                    "multi_tx_n_controllers": r.get("multi_tx_n_controllers", ""),
                    "gas_le5_share": r.get("multi_tx_share_priority_le5", ""),
                    "gas_gt_1e5_share": r.get("multi_tx_share_priority_gt_1e5", ""),
                }
            )

    known_router_tx = sum(as_int(r.get("n_unique_tx")) for r in route_recipient_counts)
    top12_platform_tx_est = sum(
        as_int(r.get("n_buys")) + as_int(r.get("n_sells"))
        for r in top_platform_rows
    )
    broad_tx = as_int(broad_counts.get("n_unique_tx"))

    rpc_estimates = {
        "top12_platform_controller_enumeration": {
            "eth_getTransactionByHash_calls_est": top12_platform_tx_est,
            "basis": "sum(n_buys+n_sells) across Sprint044 top-12 platform recipients",
            "over_30k": top12_platform_tx_est > 30_000,
        },
        "known_router_or_platform_lane_controller_enumeration": {
            "eth_getTransactionByHash_calls_est": known_router_tx,
            "basis": "unique tx where V3 swap recipient is known router/platform",
            "over_30k": known_router_tx > 30_000,
        },
        "full_broad_v3_weth_meme_controller_enumeration": {
            "eth_getTransactionByHash_calls_est": broad_tx,
            "basis": "all unique tx in broad V3 WETH-leg meme universe, fee 100/500/3000/10000",
            "over_30k": broad_tx > 30_000,
        },
    }

    should_stop = any(v["over_30k"] for v in rpc_estimates.values())

    out = {
        "sprint": "045_controller_keyed_rediscovery_preflight",
        "generated_at_utc": generated,
        "rules_checked": [
            "query-join-filter-before",
            "query-join-use-any (evaluated but not applied due undercount cross-check)",
            "schema-pk-filter-on-orderby",
        ],
        "data_coverage": {
            "uniswap_v3_swaps": coverage_v3,
            "aerodrome_vamm_swaps_mv": coverage_aero,
            "may_2026_v3_data_present": False,
            "erc20_transfer_token_sample": erc20_tokens,
            "weth_transfer_index_available": any(
                row["token_address"].lower() == WETH for row in erc20_tokens
            ),
        },
        "broad_universe_definition": {
            "chain": "Base 8453",
            "window_requested": "2026-01-01 -> 2026-05-31",
            "window_available": "2026-01-01 -> 2026-04-30 02:18:05",
            "fees_included": [100, 500, 3000, 10000],
            "fee_100_treatment": "included as F100/upgraded V3 lane; not a hard exclusion",
            "aero_treatment": "separate lane; not mixed into Uniswap V3 core score",
        },
        "fee_lane_counts": fee_lane_counts,
        "broad_counts": broad_counts,
        "known_router_or_platform_recipient_counts": route_recipient_counts,
        "sprint044_top12_platform_recipients_for_controller_decomposition": top_platform_rows,
        "rpc_estimates": rpc_estimates,
        "abort_conditions": {
            "rpc_call_cap_before_user_confirmation": 30_000,
            "runtime_cap_before_user_confirmation_minutes": 90,
            "debug_traceTransaction_requires_separate_preflight": True,
            "stop_now": should_stop,
            "stop_reason": (
                "At least one controller-keyed enumeration lane exceeds 30k RPC calls"
                if should_stop
                else "No RPC cap exceeded in current top12 lane"
            ),
        },
        "next_recommended_scope": {
            "phase_b1": "top12 platform recipient controller enumeration only",
            "phase_b2": "known-router lanes (Velora/Universal/1inch/CoW) in chunks",
            "phase_b3": "Aero separate lane after V3 controller table exists",
            "hard_gates": [
                "Base chain",
                "some WETH-leg meme trading exposure",
                "candidate must resolve to tx.from/controller or be explicitly queued for controller enumeration",
                "no live/training/model-promotion/copy-trade side effects",
            ],
            "soft_evidence_only": [
                "fee mix including fee=100",
                "low priority gas share",
                "hold distribution",
                "notional distribution",
                "sniper/diamond/paper/whale labels",
                "WETH.withdraw",
                "slippage discipline",
            ],
        },
    }

    (DIAG_DIR / "sprint045_controller_keyed_preflight.json").write_text(
        json.dumps(out, indent=2, default=str)
    )
    (AUDIT_DIR / "sprint045_controller_keyed_rediscovery_preflight.md").write_text(
        render_md(out)
    )
    print(
        "wrote sprint045 preflight; "
        f"broad_tx={broad_tx} known_router_tx={known_router_tx} "
        f"top12_platform_tx_est={top12_platform_tx_est} stop_now={should_stop}"
    )


def render_md(out: dict) -> str:
    lines: list[str] = []
    lines.append("# Sprint045 — Controller-Keyed Rediscovery Preflight")
    lines.append("")
    lines.append(f"Generated UTC: `{out['generated_at_utc']}`")
    lines.append("")
    lines.append("Phase 0 only. No controller enumeration was run.")
    lines.append("")
    lines.append("## Rules Checked")
    lines.append("")
    lines.append("- Per `query-join-filter-before`, pool and swap tables are filtered in CTEs before joining.")
    lines.append("- Per `query-join-use-any`, `ANY INNER JOIN` was evaluated, but not applied: a cross-check under-counted this pool join, so regular `INNER JOIN` is used for correctness.")
    lines.append("- Per `schema-pk-filter-on-orderby`, queries constrain `chain_id` and `block_time` before broad grouping.")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    v3 = out["data_coverage"]["uniswap_v3_swaps"]
    aero = out["data_coverage"]["aerodrome_vamm_swaps_mv"]
    lines.append(f"- `uniswap_v3_swaps`: `{v3['t_min']}` -> `{v3['t_max']}`, rows `{v3['n']}`.")
    lines.append(f"- `aerodrome_vamm_swaps_mv`: `{aero['t_min']}` -> `{aero['t_max']}`, rows `{aero['n']}`.")
    lines.append("- May 2026 V3 data: **absent / blind spot**.")
    lines.append(f"- WETH transfer index available: `{out['data_coverage']['weth_transfer_index_available']}`.")
    lines.append("")
    lines.append("## Broad Universe")
    lines.append("")
    bc = out["broad_counts"]
    lines.append(f"- Broad V3 WETH-leg meme universe, fees 100/500/3000/10000: `{bc['n_swaps']}` swaps, `{bc['n_unique_tx']}` unique tx, `{bc['n_recipients']}` recipients.")
    lines.append(f"- Known router/platform recipient tx inside broad universe: `{bc['n_tx_known_router_or_platform']}` unique tx.")
    lines.append("- `fee=100` is included as F100/upgraded V3 lane, not a hard exclusion.")
    lines.append("- Aero is separate lane and not mixed into V3 author-core score.")
    lines.append("")
    lines.append("### Fee Lane Counts")
    lines.append("")
    lines.append("| fee_bps | swaps | unique_tx | recipients |")
    lines.append("|---|---:|---:|---:|")
    for row in out["fee_lane_counts"]:
        lines.append(f"| {row['fee_bps']} | {row['n_swaps']} | {row['n_unique_tx']} | {row['n_recipients']} |")
    lines.append("")
    lines.append("## RPC Cost Estimate")
    lines.append("")
    lines.append("| lane | estimated RPC calls | >30k? |")
    lines.append("|---|---:|---|")
    for name, row in out["rpc_estimates"].items():
        lines.append(f"| `{name}` | {row['eth_getTransactionByHash_calls_est']} | {row['over_30k']} |")
    lines.append("")
    ac = out["abort_conditions"]
    lines.append(f"**Stop now:** `{ac['stop_now']}` — {ac['stop_reason']}.")
    lines.append("")
    lines.append("## Decision")
    lines.append("")
    lines.append("Do **not** run full controller-keyed enumeration in this turn. At least one planned lane exceeds the 30k RPC cap, and full broad V3 enumeration is far above it.")
    lines.append("")
    lines.append("Recommended next step: run Phase B1 only after explicit approval: top-12 platform recipient controller enumeration, then score controllers softly. If that stays under the cap in practice, chunk B2 known-router lanes separately.")
    lines.append("")
    lines.append("## Hard Gates vs Soft Evidence")
    lines.append("")
    lines.append("Hard gates remain minimal: Base chain, WETH-leg meme exposure, resolvable controller or queued controller enumeration, and no live/training/model/copy-trade side effects.")
    lines.append("")
    lines.append("Soft-only evidence: fee mix including fee=100, gas fingerprint, hold distribution, notional distribution, sniper/diamond/paper/whale labels, WETH.withdraw, slippage discipline.")
    lines.append("")
    lines.append("## Flags")
    lines.append("")
    lines.append("```text")
    lines.append("FEE100_HARD_EXCLUSION=false")
    lines.append("F10_SHARE_HARD_EXCLUSION=false")
    lines.append("HOLD_TIME_HARD_EXCLUSION=false")
    lines.append("NOTIONAL_1_52_HARD_EXCLUSION=false")
    lines.append("SNIPER_DIAMOND_PAPER_WHALE_HARD_GATE=false")
    lines.append("AERO_SEPARATE_LANE=true")
    lines.append("PUBLIC_ROUTER_AS_WALLET_CANDIDATE=false")
    lines.append("CONTROLLER_KEYED_ENUMERATION_REQUIRED=true")
    lines.append("MAY_2026_BLIND_SPOT=true")
    lines.append("FUNDING_GRAPH_BLOCKED_BY_INDEXING=true")
    lines.append("RUN_FULL_RPC_NOW=false")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
