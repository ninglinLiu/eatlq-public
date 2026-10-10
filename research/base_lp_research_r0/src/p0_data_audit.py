"""Reproduce the P0 data audit using read-only ClickHouse queries.

The script writes only under this research directory.  Every database query
contains the Base chain predicate where Base evidence is claimed.
"""

from __future__ import annotations

import csv
import json
import subprocess
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from src.lp_simulator import (
    liquidity_for_value_quote,
    raw_liquidity_scale,
    raw_position_amounts,
    sqrt_price_x96_to_price,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
CONTAINER = "eatlq-clickhouse-1"
AUDIT_DATE = "2026-03-14"
COUNT_AS_OF = "2026-08-07 06:00:00"
POOLS = {
    "0xd0b53d9277642d899df5c87a3966a349a798f224": {
        "venue": "uniswap_v3",
        "pair": "WETH/USDC",
        "decimals0": 18,
        "decimals1": 6,
        "fee_bps": 5,
    },
    "0x82321f3beb69f503380d6b233857d5c43562e2d0": {
        "venue": "aero_cl",
        "pair": "WETH/AERO",
        "decimals0": 18,
        "decimals1": 18,
        "fee_bps": 5,
    },
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5": {
        "venue": "uniswap_v3",
        "pair": "WETH/USDC",
        "decimals0": 18,
        "decimals1": 6,
        "fee_bps": 1,
    },
}
COLLECT_TOPIC = "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0"


def query_rows(sql: str) -> list[dict]:
    command = [
        "docker",
        "exec",
        CONTAINER,
        "clickhouse-client",
        "--format",
        "JSONEachRow",
        "-q",
        sql,
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def event_counts(table: str) -> list[dict]:
    addresses = ",".join(f"'{address}'" for address in POOLS)
    return query_rows(
        f"""
        SELECT pool_address, count() AS dedup_events, min(block_time) AS min_time,
               max(block_time) AS max_time, sum(duplicate_rows) AS source_rows
        FROM eatlq.{table}_decoded
        WHERE chain_id = 8453 AND removed = 0
          AND block_time <= toDateTime64('{COUNT_AS_OF}', 3, 'UTC')
          AND pool_address IN ({addresses})
        GROUP BY pool_address ORDER BY pool_address
        """
    )


def fetch_day_swaps(pool: str) -> list[dict]:
    return query_rows(
        f"""
        SELECT block_number, log_index, toString(sqrt_price_x96) AS sqrt_price_x96,
               tick, toString(liquidity) AS liquidity,
               toString(amount0) AS amount0, toString(amount1) AS amount1
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = 8453 AND removed = 0
          AND pool_address = '{pool}' AND toDate(block_time) = '{AUDIT_DATE}'
        ORDER BY block_number, log_index
        """
    )


def fetch_day_burns(pool: str) -> list[dict]:
    return query_rows(
        f"""
        SELECT block_number, log_index, tick_lower, tick_upper,
               toString(liquidity) AS liquidity, toString(amount0) AS amount0,
               toString(amount1) AS amount1
        FROM eatlq.uniswap_v3_burns_decoded
        WHERE chain_id = 8453 AND removed = 0
          AND pool_address = '{pool}' AND toDate(block_time) = '{AUDIT_DATE}'
        ORDER BY block_number, log_index
        """
    )


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def reconcile_burn_principal(pool: str, swaps: list[dict]) -> tuple[dict, list[dict]]:
    """Compare Burn amounts to v3 principal formulas at the preceding price."""

    burns = fetch_day_burns(pool)
    swap_keys = [(int(row["block_number"]), int(row["log_index"])) for row in swaps]
    samples: list[dict] = []
    errors: list[float] = []
    exact_zero_sides = 0
    for burn in burns:
        key = (int(burn["block_number"]), int(burn["log_index"]))
        swap_index = bisect_left(swap_keys, key) - 1
        if swap_index < 0:
            continue
        previous_swap = swaps[swap_index]
        observed0, observed1 = int(burn["amount0"]), int(burn["amount1"])
        predicted0, predicted1 = raw_position_amounts(
            int(burn["liquidity"]),
            int(previous_swap["sqrt_price_x96"]),
            int(burn["tick_lower"]),
            int(burn["tick_upper"]),
        )
        side_errors = []
        for observed, predicted in ((observed0, predicted0), (observed1, predicted1)):
            if observed == 0 and abs(predicted) < 1:
                exact_zero_sides += 1
                continue
            if observed > 0:
                error = abs(predicted - observed) / observed
                errors.append(error)
                side_errors.append(error)
        if len(samples) < 100:
            samples.append(
                {
                    "pool_address": pool,
                    "block_number": key[0],
                    "log_index": key[1],
                    "observed_amount0_raw": observed0,
                    "predicted_principal0_raw": round(predicted0),
                    "observed_amount1_raw": observed1,
                    "predicted_principal1_raw": round(predicted1),
                    "max_nonzero_relative_error": max(side_errors) if side_errors else 0.0,
                }
            )
    return (
        {
            "pool_address": pool,
            "date": AUDIT_DATE,
            "burn_events": len(burns),
            "events_with_preceding_price": sum(
                bisect_left(swap_keys, (int(row["block_number"]), int(row["log_index"]))) > 0
                for row in burns
            ),
            "nonzero_amount_sides_compared": len(errors),
            "exact_zero_sides": exact_zero_sides,
            "principal_formula_relative_error_p50": quantile(errors, 0.5),
            "principal_formula_relative_error_p99": quantile(errors, 0.99),
        },
        samples,
    )


def capacity_audit(all_swaps: dict[str, list[dict]]) -> list[dict]:
    """Audit a predeclared $500, +/-15%, static one-day position per pool."""

    reference_pool = "0xd0b53d9277642d899df5c87a3966a349a798f224"
    reference_meta = POOLS[reference_pool]
    weth_usdc = sqrt_price_x96_to_price(
        int(all_swaps[reference_pool][0]["sqrt_price_x96"]),
        reference_meta["decimals0"],
        reference_meta["decimals1"],
    )
    output = []
    for pool, meta in POOLS.items():
        swaps = all_swaps[pool]
        if not swaps:
            continue
        initial_price = sqrt_price_x96_to_price(
            int(swaps[0]["sqrt_price_x96"]), meta["decimals0"], meta["decimals1"]
        )
        # Both selected pair orientations have token1 as quote.  Convert $500
        # into AERO for WETH/AERO via the simultaneous WETH/USDC price.
        quote_value = (
            500.0
            if meta["pair"] == "WETH/USDC"
            else 500.0 / weth_usdc * initial_price
        )
        lower, upper = initial_price * 0.85, initial_price * 1.15
        my_l = liquidity_for_value_quote(quote_value, initial_price, lower, upper)
        my_raw = my_l * raw_liquidity_scale(meta["decimals0"], meta["decimals1"])
        shares = []
        for swap in swaps:
            price = sqrt_price_x96_to_price(
                int(swap["sqrt_price_x96"]), meta["decimals0"], meta["decimals1"]
            )
            if lower <= price < upper:
                shares.append(my_raw / (my_raw + int(swap["liquidity"])))
        violation_pct = (
            100.0 * sum(share > 0.01 for share in shares) / len(shares) if shares else 0.0
        )
        output.append(
            {
                "pool_address": pool,
                "pair": meta["pair"],
                "date": AUDIT_DATE,
                "capital_usd": 500.0,
                "band_half_width_pct": 15.0,
                "in_range_swaps": len(shares),
                "implied_share_p50": median(shares) if shares else None,
                "implied_share_p99": quantile(shares, 0.99),
                "capacity_violation_pct": violation_pct,
                "status": "CAPACITY_VIOLATION" if violation_pct else "OK",
                "pricing_note": (
                    "USDC is USD quote"
                    if meta["pair"] == "WETH/USDC"
                    else "AERO quote value cross-converted through simultaneous WETH/USDC"
                ),
            }
        )
    return output


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    counts = {
        name: event_counts(name)
        for name in ("uniswap_v3_swaps", "uniswap_v3_mints", "uniswap_v3_burns")
    }
    # Keep the Base and mainnet diagnostics as two explicitly chain-pure
    # queries.  Never infer Base coverage from a mixed-chain aggregation.
    base_collect_counts = query_rows(
        f"""
        SELECT 8453 AS requested_chain_id, count() AS rows,
               uniqExact((tx_hash, log_index, address)) AS events,
               uniqExact(address) AS pools, min(block_time) AS min_time,
               max(block_time) AS max_time
        FROM eatlq.raw_evm_logs
        WHERE raw_evm_logs.chain_id = 8453 AND lower(topic0) = '{COLLECT_TOPIC}'
        """
    )
    mainnet_collect_counts = query_rows(
        f"""
        SELECT 1 AS requested_chain_id, count() AS rows,
               uniqExact((tx_hash, log_index, address)) AS events,
               uniqExact(address) AS pools, min(block_time) AS min_time,
               max(block_time) AS max_time
        FROM eatlq.raw_evm_logs
        WHERE raw_evm_logs.chain_id = 1 AND lower(topic0) = '{COLLECT_TOPIC}'
        """
    )
    collect_counts = base_collect_counts + mainnet_collect_counts
    all_swaps = {pool: fetch_day_swaps(pool) for pool in POOLS}
    reconciliations = []
    burn_samples = []
    for pool in POOLS:
        summary, samples = reconcile_burn_principal(pool, all_swaps[pool])
        reconciliations.append(summary)
        burn_samples.extend(samples)
    capacity = capacity_audit(all_swaps)
    artifact = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "audit_date": AUDIT_DATE,
        "event_count_as_of_utc": COUNT_AS_OF,
        "chain_filter_for_base": "chain_id = 8453",
        "specified_pool_event_counts": counts,
        "collect_topic": COLLECT_TOPIC,
        "collect_counts_by_chain": collect_counts,
        "burn_principal_reconciliation": reconciliations,
        "capacity_audit": capacity,
        "conclusion": (
            "Base Collect truth is absent; Burn amounts reconcile to principal formulas and "
            "cannot be used as fee truth. P0.2 calibration is unidentified."
        ),
    }
    (OUT / "p0_data_audit.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"
    )
    write_csv(OUT / "p0_capacity_audit.csv", capacity)
    write_csv(OUT / "p0_burn_principal_samples.csv", burn_samples)
    print(json.dumps(artifact, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
