"""Fetch end-window pool ``positions()`` via historical Multicall3.

The end liquidity combined with complete in-window Mint/Burn deltas identifies
opening liquidity: ``L_start = L_end - sum(Mint) + sum(Burn)``.  Calls are batched
through Multicall3 and each completed batch is checkpointed locally.
"""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import keccak

from src.backfill_collects import END_BLOCK, OUT, POOLS, RpcClient, resolve_rpc_url
from src.validate_ingestion import STAGING_TABLE


MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
AGGREGATE3_SELECTOR = keccak(text="aggregate3((address,bool,bytes)[])")[:4]
POSITIONS_SELECTOR = keccak(text="positions(bytes32)")[:4]
BATCH_SIZE = 150
PARTS = OUT / "position_snapshot_parts"
OUTPUT = OUT / "position_liquidity_boundaries.csv"
METRICS = OUT / "position_snapshot_metrics.json"


def position_key(owner: str, tick_lower: int, tick_upper: int) -> bytes:
    return keccak(
        bytes.fromhex(owner.removeprefix("0x"))
        + tick_lower.to_bytes(3, "big", signed=True)
        + tick_upper.to_bytes(3, "big", signed=True)
    )


def query_rows(sql: str) -> list[dict]:
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "eatlq-clickhouse-1",
            "clickhouse-client",
            "--format",
            "JSONEachRow",
            "-q",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in completed.stdout.splitlines() if line]


def position_boundaries() -> list[dict]:
    addresses = ",".join(f"'{pool}'" for pool in POOLS)
    rows = query_rows(
        f"""
        SELECT pool_address, owner, tick_lower, tick_upper,
               sumIf(toInt256(liquidity), event_type='mint') -
               sumIf(toInt256(liquidity), event_type='burn') AS net_liquidity_change
        FROM {STAGING_TABLE} FINAL
        WHERE chain_id=8453 AND removed=0
          AND block_number <= {END_BLOCK} AND pool_address IN ({addresses})
        GROUP BY pool_address, owner, tick_lower, tick_upper
        ORDER BY pool_address, owner, tick_lower, tick_upper
        """
    )
    return rows


def encode_multicall(pool: str, rows: list[dict]) -> str:
    calls = []
    for row in rows:
        key = position_key(row["owner"], int(row["tick_lower"]), int(row["tick_upper"]))
        calls.append((pool, False, POSITIONS_SELECTOR + encode(["bytes32"], [key])))
    return "0x" + (
        AGGREGATE3_SELECTOR + encode(["(address,bool,bytes)[]"], [calls])
    ).hex()


def fetch_batch(client: RpcClient, pool: str, rows: list[dict]) -> list[dict]:
    result = client.call(
        "eth_call",
        [{"to": MULTICALL3, "data": encode_multicall(pool, rows)}, hex(END_BLOCK)],
    )
    decoded = decode(["(bool,bytes)[]"], bytes.fromhex(result.removeprefix("0x")))[0]
    if len(decoded) != len(rows):
        raise RuntimeError("Multicall result length mismatch")
    output = []
    for row, (success, return_data) in zip(rows, decoded):
        if not success:
            raise RuntimeError(f"positions() failed for {row}")
        values = decode(
            ["uint128", "uint256", "uint256", "uint128", "uint128"], return_data
        )
        net = int(row["net_liquidity_change"])
        end_liquidity = int(values[0])
        output.append(
            {
                **row,
                "end_liquidity": end_liquidity,
                "initial_liquidity": end_liquidity - net,
                "end_tokens_owed0": int(values[3]),
                "end_tokens_owed1": int(values[4]),
            }
        )
    return output


def main() -> None:
    rows = position_boundaries()
    by_pool = {pool: [] for pool in POOLS}
    for row in rows:
        by_pool[row["pool_address"]].append(row)
    PARTS.mkdir(parents=True, exist_ok=True)
    client = RpcClient(resolve_rpc_url(), max_rps=2.0)
    all_rows = []
    for pool in POOLS:
        pool_rows = by_pool[pool]
        for offset in range(0, len(pool_rows), BATCH_SIZE):
            part = PARTS / f"{pool}_{offset // BATCH_SIZE:05d}.json"
            if part.exists():
                batch_rows = json.loads(part.read_text())
            else:
                batch_rows = fetch_batch(client, pool, pool_rows[offset : offset + BATCH_SIZE])
                temporary = part.with_suffix(".tmp")
                temporary.write_text(json.dumps(batch_rows) + "\n")
                temporary.replace(part)
            all_rows.extend(batch_rows)
            print(f"{pool}: {min(offset + BATCH_SIZE, len(pool_rows))}/{len(pool_rows)}", flush=True)
    with OUTPUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(all_rows)
    metrics = {
        "block_number": END_BLOCK,
        "position_keys": len(all_rows),
        "http_attempts_this_run": client.metrics.http_attempts,
        "json_rpc_responses_this_run": client.metrics.json_rpc_responses,
        "retries_this_run": client.metrics.retries,
        "assumed_base_eth_call_credits_per_response": 20,
        "observed_eth_call_credits_this_run": client.metrics.json_rpc_responses * 20,
        "negative_initial_liquidity_rows": sum(int(row["initial_liquidity"]) < 0 for row in all_rows),
        "nonzero_initial_liquidity_rows": sum(int(row["initial_liquidity"]) > 0 for row in all_rows),
        "output": str(OUTPUT),
    }
    METRICS.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
