#!/usr/bin/env python3
"""Install and audit the research-only Base V3 resolved-wallet view."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from base_attribution import CHAIN_ID, EXCLUSION_ROWS, MANIFEST
from ch_helper import DEFAULT_CH_CONTAINER, ch_query

REPO = Path(__file__).resolve().parents[2]
DDL = REPO / "infra" / "clickhouse" / "init" / "041_base_v3_resolved_wallet_swaps.sql"


def _run_client(sql: str, *, multiquery: bool = False, timeout: int = 180) -> None:
    command = ["docker", "exec", "-i", DEFAULT_CH_CONTAINER, "clickhouse-client"]
    if multiquery:
        command.append("--multiquery")
    result = subprocess.run(
        command,
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1000])


def install_view() -> None:
    """Apply research DDL and exactly sync the manifest into its CH authority table."""
    _run_client(DDL.read_text(encoding="utf-8"), multiquery=True)
    _run_client("TRUNCATE TABLE eatlq.base_wallet_attribution_exclusions")
    values = []
    for row in EXCLUSION_ROWS:
        label = row["label"].replace("'", "\\'")
        values.append(
            f"({CHAIN_ID},'{row['address']}','{row['category']}','{label}',"
            f"{int(MANIFEST['schema_version'])})"
        )
    _run_client(
        "INSERT INTO eatlq.base_wallet_attribution_exclusions "
        "(chain_id,address,category,label,schema_version) VALUES " + ",".join(values)
    )


def validate_block_window(start_block: int, end_block: int) -> None:
    if start_block < 0 or end_block < start_block:
        raise ValueError("invalid block-number window")


def attribution_summary(start_block: int, end_block: int) -> dict[str, Any]:
    validate_block_window(start_block, end_block)
    bounds = f"block_number BETWEEN {start_block} AND {end_block}"
    summary = ch_query(
        f"""
        SELECT
            min(resolved_block_time) AS min_block_time,
            max(resolved_block_time) AS max_block_time,
            count() AS swaps,
            uniqExact(tx_hash) AS txs,
            uniqExact(lower(sender)) AS old_sender_wallets_raw,
            uniqExactIf(lower(sender), old_sender_status != 'excluded_infrastructure')
                AS old_sender_wallets_noninfra,
            uniqExactIf(resolved_eoa, resolved_eoa != '') AS resolved_wallets,
            countIf(target_trade_side = 'buy') AS all_buys,
            countIf(target_trade_side = 'sell') AS all_sells,
            countIf(target_trade_side = 'buy' AND old_sender_status != 'excluded_infrastructure')
                AS old_noninfra_buys,
            countIf(target_trade_side = 'sell' AND old_sender_status != 'excluded_infrastructure')
                AS old_noninfra_sells,
            countIf(target_trade_side = 'buy' AND resolved_eoa != '') AS resolved_buys,
            countIf(target_trade_side = 'sell' AND resolved_eoa != '') AS resolved_sells,
            countIf(old_sender_status = 'excluded_infrastructure') AS router_polluted_swaps,
            countIf(old_sender_status = 'different_from_resolved') AS changed_nonrouter_swaps,
            countIf(attribution_source = 'tx_from') AS tx_from_swaps,
            countIf(attribution_source = 'recipient') AS recipient_fallback_swaps,
            countIf(attribution_source = 'sender') AS sender_fallback_swaps,
            countIf(attribution_source = 'unresolved') AS unresolved_swaps,
            countIf(block_time_source = 'missing') AS missing_real_block_time
        FROM eatlq.base_v3_wallet_swaps_resolved
        WHERE {bounds}
        """,
        timeout=600,
    )[0]
    sets = ch_query(
        f"""
        WITH
          old AS (
            SELECT DISTINCT lower(sender) AS wallet
            FROM eatlq.base_v3_wallet_swaps_resolved
            WHERE {bounds} AND old_sender_status != 'excluded_infrastructure'
          ),
          resolved AS (
            SELECT DISTINCT resolved_eoa AS wallet
            FROM eatlq.base_v3_wallet_swaps_resolved
            WHERE {bounds} AND resolved_eoa != ''
          )
        SELECT
          countIf(o.wallet = '') AS resolved_wallets_missed_by_old,
          countIf(o.wallet != '') AS wallets_shared_with_old
        FROM resolved r
        LEFT JOIN old o ON r.wallet = o.wallet
        """,
        timeout=600,
    )[0]
    top_router = ch_query(
        f"""
        SELECT
          lower(sender) AS sender,
          anyIf(ex.label, ex.address = lower(sender)) AS label,
          count() AS swaps,
          uniqExact(tx_hash) AS txs,
          uniqExactIf(resolved_eoa, resolved_eoa != '') AS resolved_wallets
        FROM eatlq.base_v3_wallet_swaps_resolved s
        LEFT JOIN (
          SELECT chain_id, address, anyLast(label) AS label
          FROM eatlq.base_wallet_attribution_exclusions FINAL
          GROUP BY chain_id, address
        ) ex
          ON ex.chain_id = s.chain_id AND ex.address = lower(s.sender)
        WHERE {bounds} AND old_sender_status = 'excluded_infrastructure'
        GROUP BY sender
        ORDER BY swaps DESC
        LIMIT 20
        """,
        timeout=600,
    )
    high_activity = ch_query(
        f"""
        WITH ranked AS (
          SELECT lower(sender) AS sender, count() AS swaps
          FROM eatlq.base_v3_wallet_swaps_resolved
          WHERE {bounds}
          GROUP BY sender ORDER BY swaps DESC LIMIT 100
        )
        SELECT
          count() AS top100_senders,
          countIf(ex.address != '') AS top100_infrastructure,
          sum(swaps) AS top100_swaps,
          sumIf(swaps, ex.address != '') AS top100_infrastructure_swaps
        FROM ranked r
        LEFT JOIN (
          SELECT chain_id, address
          FROM eatlq.base_wallet_attribution_exclusions FINAL
          GROUP BY chain_id, address
        ) ex
          ON ex.chain_id = {CHAIN_ID} AND ex.address = r.sender
        """,
        timeout=600,
    )[0]
    summary.update(sets)
    summary["top_router_senders"] = top_router
    summary["high_activity_sender_audit"] = high_activity
    swaps = int(summary["swaps"])
    resolved_wallets = int(summary["resolved_wallets"])
    summary["router_polluted_swap_pct"] = round(
        100 * int(summary["router_polluted_swaps"]) / swaps, 4
    ) if swaps else 0.0
    summary["old_wallet_miss_pct"] = round(
        100 * int(summary["resolved_wallets_missed_by_old"]) / resolved_wallets, 4
    ) if resolved_wallets else 0.0
    return summary


def golden_candidates(start_block: int, end_block: int, limit: int = 10) -> list[dict[str, Any]]:
    validate_block_window(start_block, end_block)
    return ch_query(
        f"""
        SELECT
          resolved_eoa AS wallet,
          count() AS swaps,
          countIf(target_trade_side = 'buy') AS buys,
          countIf(target_trade_side = 'sell') AS sells,
          uniqExact(pool_address) AS pools,
          uniqExact(target_token_address) AS target_tokens,
          min(resolved_block_time) AS first_time,
          max(resolved_block_time) AS last_time,
          groupUniqArray(3)(lower(sender)) AS routed_via,
          groupUniqArray(3)(tx_hash) AS sample_tx_hashes
        FROM eatlq.base_v3_wallet_swaps_resolved
        WHERE block_number BETWEEN {start_block} AND {end_block}
          AND attribution_source = 'tx_from'
          AND target_trade_side IN ('buy', 'sell')
        GROUP BY wallet
        HAVING buys > 0 AND sells > 0
        ORDER BY swaps DESC
        LIMIT {int(limit)}
        """,
        timeout=600,
    )


def wallet_holdings(wallet: str, start_block: int, end_block: int) -> list[dict[str, Any]]:
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError("invalid wallet")
    return ch_query(
        f"""
        SELECT
          target_token_address AS token,
          any(target_token_decimals) AS decimals,
          countIf(target_trade_side = 'buy') AS buys,
          countIf(target_trade_side = 'sell') AS sells,
          toString(sum(resolved_wallet_target_delta_raw)) AS net_raw,
          sum(toFloat64(resolved_wallet_target_delta_raw) / pow(10, target_token_decimals))
            AS net_tokens,
          groupUniqArray(5)(tx_hash) AS tx_hashes
        FROM eatlq.base_v3_wallet_swaps_resolved
        WHERE block_number BETWEEN {start_block} AND {end_block}
          AND resolved_eoa = '{wallet.lower()}'
          AND target_token_address != ''
        GROUP BY token
        ORDER BY buys + sells DESC
        """,
        timeout=600,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--golden-limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.install:
        install_view()
    result = {
        "chain_id": CHAIN_ID,
        "start_block": args.start_block,
        "end_block": args.end_block,
        "summary": attribution_summary(args.start_block, args.end_block),
        "golden_candidates": golden_candidates(
            args.start_block, args.end_block, args.golden_limit
        ),
    }
    rendered = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
