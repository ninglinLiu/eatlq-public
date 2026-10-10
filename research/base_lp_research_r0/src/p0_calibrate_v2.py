"""Rerun P0.2 against Collect-minus-Burn fee truth.

The simulator reconstructs each core-pool ``(owner, tickLower, tickUpper)``
position from RPC Mint/Burn events.  For every swap it assigns the positive
input-token fee pro rata by raw active liquidity.  A Fenwick tree over ticks
allows fee growth inside an arbitrary range to be queried in O(log ticks).

Truth is paired at core-pool ``(owner, tickLower, tickUpper)`` granularity.
The first Collect for a key is a non-scored clearing anchor, eliminating stale
pre-window ``tokensOwed``.  Thereafter Burn principal is carried across partial
Collect calls and only the residual ``sum(Collect)-sum(Burn)`` is labelled fee.
Simulated fee is carried over across the same partial-collection episode.  Final
statistics aggregate those clean episodes back to one observation per range and
token.
"""

from __future__ import annotations

import csv
import heapq
import json
import math
import subprocess
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from src.backfill_collects import END_BLOCK, OUT, POOLS, START_BLOCK
from src.calibration import exact_two_sided_sign_test
from src.validate_ingestion import STAGING_TABLE


ROOT = Path(__file__).resolve().parents[1]
MIN_TICK = -887_272
MAX_TICK = 887_272
TREE_SIZE = MAX_TICK - MIN_TICK + 1
POOL_META = {
    "0xd0b53d9277642d899df5c87a3966a349a798f224": {
        "venue": "v3",
        "pair": "WETH/USDC",
        "fee_pips": 500,
    },
    "0x82321f3beb69f503380d6b233857d5c43562e2d0": {
        "venue": "aero_cl",
        "pair": "WETH/AERO",
        "fee_pips": 500,
    },
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5": {
        "venue": "v3",
        "pair": "WETH/USDC",
        "fee_pips": 100,
    },
}
RANGE_OBSERVATIONS = OUT / "p0_calibration_range_observations_v2.csv"
LIQUIDITY_BOUNDARIES = OUT / "position_liquidity_boundaries.csv"
SUMMARY = OUT / "p0_calibration_summary_v2.json"


class Fenwick:
    def __init__(self) -> None:
        self.values = array("d", [0.0]) * (TREE_SIZE + 1)

    @staticmethod
    def _index(tick: int) -> int:
        if not MIN_TICK <= tick <= MAX_TICK:
            raise ValueError(f"tick out of bounds: {tick}")
        return tick - MIN_TICK + 1

    def add(self, tick: int, value: float) -> None:
        index = self._index(tick)
        while index <= TREE_SIZE:
            self.values[index] += value
            index += index & -index

    def prefix(self, tick: int) -> float:
        if tick < MIN_TICK:
            return 0.0
        tick = min(tick, MAX_TICK)
        index = self._index(tick)
        total = 0.0
        while index:
            total += self.values[index]
            index -= index & -index
        return total

    def range_sum(self, lower: int, upper: int) -> float:
        return self.prefix(upper - 1) - self.prefix(lower - 1)


@dataclass
class PositionState:
    known: bool
    liquidity: int = 0
    last_growth0: float = 0.0
    last_growth1: float = 0.0
    simulated_total0: float = 0.0
    simulated_total1: float = 0.0
    episode_start_block: int | None = None
    simulated_at_collect0: float = 0.0
    simulated_at_collect1: float = 0.0
    unmatched_simulated0: float = 0.0
    unmatched_simulated1: float = 0.0
    pending_burn_principal0: int = 0
    pending_burn_principal1: int = 0
    anchored: bool = False


@dataclass
class Stats:
    relative_errors: list[float] = field(default_factory=list)
    positive: int = 0
    negative: int = 0
    ties: int = 0

    def add(self, simulated: float, truth: int) -> None:
        self.relative_errors.append(abs(simulated - truth) / truth)
        if simulated > truth:
            self.positive += 1
        elif simulated < truth:
            self.negative += 1
        else:
            self.ties += 1

    def summary(self) -> dict:
        n = len(self.relative_errors)
        p_value = exact_two_sided_sign_test(self.positive, self.negative)
        median_error = median(self.relative_errors) if self.relative_errors else None
        return {
            "n_positive_truth_token_intervals": n,
            "median_relative_error": median_error,
            "positive_errors": self.positive,
            "negative_errors": self.negative,
            "ties": self.ties,
            "sign_test_p_value": p_value,
            "passed": bool(
                n and median_error is not None and median_error < 0.15 and p_value > 0.05
            ),
        }


def _query_stream(sql: str):
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "eatlq-clickhouse-1",
            "clickhouse-client",
            "-q",
            sql,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        if line.strip():
            yield json.loads(line)
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args)


def stream_events(pool: str):
    """Merge swap, nonzero-liquidity Mint/Burn, and Collect streams."""

    common = (
        f"chain_id=8453 AND removed=0 AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK} "
        f"AND pool_address='{pool}'"
    )
    queries = [
        f"""
        SELECT 'swap' AS event_type, block_number, log_index, '' AS owner,
               0 AS tick_lower, 0 AS tick_upper, toString(0) AS liquidity,
               toString(swaps.amount0) AS amount0,
               toString(swaps.amount1) AS amount1,
               swaps.tick AS tick, toString(swaps.liquidity) AS active_liquidity
        FROM eatlq.uniswap_v3_swaps_decoded AS swaps
        WHERE {common}
        ORDER BY block_number, log_index FORMAT JSONEachRow
        """,
        *[
            f"""
            SELECT '{event_type}' AS event_type, block_number, log_index, owner,
                   tick_lower, tick_upper, toString(position_events.liquidity) AS liquidity,
                   toString(position_events.amount0) AS amount0,
                   toString(position_events.amount1) AS amount1,
                   0 AS tick, toString(0) AS active_liquidity
            FROM {STAGING_TABLE} AS position_events FINAL
            WHERE {common} AND position_events.event_type='{event_type}'
              AND position_events.liquidity > 0
            ORDER BY block_number, log_index FORMAT JSONEachRow
            """
            for event_type in ("mint", "burn")
        ],
        f"""
        SELECT 'collect' AS event_type, block_number, log_index, owner,
               tick_lower, tick_upper, toString(0) AS liquidity,
               toString(amount0) AS amount0, toString(amount1) AS amount1,
               0 AS tick, toString(0) AS active_liquidity
        FROM eatlq.uniswap_v3_collects FINAL
        WHERE {common}
        ORDER BY block_number, log_index FORMAT JSONEachRow
        """,
    ]
    streams = [_query_stream(sql) for sql in queries]
    yield from heapq.merge(
        *streams,
        key=lambda row: (int(row["block_number"]), int(row["log_index"])),
    )


def accrue(state: PositionState, lower: int, upper: int, growth0: Fenwick, growth1: Fenwick) -> None:
    current0 = growth0.range_sum(lower, upper)
    current1 = growth1.range_sum(lower, upper)
    if state.known and state.liquidity:
        state.simulated_total0 += state.liquidity * (current0 - state.last_growth0)
        state.simulated_total1 += state.liquidity * (current1 - state.last_growth1)
    state.last_growth0 = current0
    state.last_growth1 = current1


def settle_collect(collect: int, pending_burn_principal: int) -> tuple[int, int, int]:
    """Return (principal_recovered, fee_residual, remaining_principal)."""

    if collect < 0 or pending_burn_principal < 0:
        raise ValueError("collect and pending principal must be nonnegative")
    principal_recovered = min(collect, pending_burn_principal)
    return (
        principal_recovered,
        collect - principal_recovered,
        pending_burn_principal - principal_recovered,
    )


def load_truth(pool: str) -> dict[tuple[str, int, int], dict]:
    """Load full-window Collect/Burn sums plus the first lifecycle event."""

    sql = f"""
    SELECT owner, tick_lower, tick_upper,
           argMin(event_type, tuple(block_number, log_index)) AS first_event,
           sumIf(amount0, event_type='collect') AS collect0,
           sumIf(amount1, event_type='collect') AS collect1,
           sumIf(amount0, event_type='burn') AS burn0,
           sumIf(amount1, event_type='burn') AS burn1
    FROM
    (
        SELECT event_type, block_number, log_index, owner, tick_lower, tick_upper,
               amount0, amount1
        FROM {STAGING_TABLE} FINAL
        WHERE chain_id=8453 AND removed=0
          AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
          AND pool_address='{pool}'
        UNION ALL
        SELECT 'collect' AS event_type, block_number, log_index, owner,
               tick_lower, tick_upper, amount0, amount1
        FROM eatlq.uniswap_v3_collects FINAL
        WHERE chain_id=8453 AND removed=0
          AND block_number BETWEEN {START_BLOCK} AND {END_BLOCK}
          AND pool_address='{pool}'
    )
    GROUP BY owner, tick_lower, tick_upper
    FORMAT JSONEachRow
    """
    completed = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "-q", sql],
        text=True,
        capture_output=True,
        check=True,
    )
    result = {}
    for line in completed.stdout.splitlines():
        row = json.loads(line)
        result[(row["owner"], int(row["tick_lower"]), int(row["tick_upper"]))] = {
            "first_event": row["first_event"],
            "truth0": int(row["collect0"]) - int(row["burn0"]),
            "truth1": int(row["collect1"]) - int(row["burn1"]),
        }
    return result


def load_initial_liquidity(pool: str) -> dict[tuple[str, int, int], int]:
    """Load v3 opening liquidity inferred from the historical end snapshot."""

    if POOL_META[pool]["venue"] != "v3":
        return {}
    if not LIQUIDITY_BOUNDARIES.exists():
        raise RuntimeError(f"missing position boundary snapshot: {LIQUIDITY_BOUNDARIES}")
    result = {}
    with LIQUIDITY_BOUNDARIES.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["pool_address"] != pool:
                continue
            initial = int(row["initial_liquidity"])
            if initial < 0:
                raise RuntimeError(f"negative v3 initial liquidity: {row}")
            result[(row["owner"], int(row["tick_lower"]), int(row["tick_upper"]))] = initial
    return result


def process_pool(
    pool: str,
    aggregates: dict[tuple[str, str, int, int, int], list[float | int]],
) -> dict:
    meta = POOL_META[pool]
    fee_rate = meta["fee_pips"] / 1_000_000
    initial_liquidity = load_initial_liquidity(pool)
    growth0, growth1 = Fenwick(), Fenwick()
    positions: dict[tuple[str, int, int], PositionState] = {}
    counters = {
        "events": 0,
        "swaps": 0,
        "mints": 0,
        "burns": 0,
        "unknown_start_positions": 0,
        "positions_invalidated_by_excess_burn": 0,
        "open_lifecycles_excluded": 0,
        "nonpositive_truth_token_sides": 0,
        "scored_range_token_observations": 0,
        "snapshot_position_keys": len(initial_liquidity),
        "nonzero_initial_liquidity_keys": sum(value > 0 for value in initial_liquidity.values()),
    }
    for event in stream_events(pool):
        counters["events"] += 1
        kind = event["event_type"]
        if kind == "swap":
            counters["swaps"] += 1
            active = int(event["active_liquidity"])
            if active <= 0:
                continue
            tick = int(event["tick"])
            amount0, amount1 = int(event["amount0"]), int(event["amount1"])
            if amount0 > 0:
                growth0.add(tick, amount0 * fee_rate / active)
            if amount1 > 0:
                growth1.add(tick, amount1 * fee_rate / active)
            continue

        owner = event["owner"]
        lower, upper = int(event["tick_lower"]), int(event["tick_upper"])
        key = (owner, lower, upper)
        state = positions.get(key)
        if state is None:
            if meta["venue"] == "v3" and key in initial_liquidity:
                known = True
                opening_liquidity = initial_liquidity[key]
            else:
                known = kind == "mint"
                opening_liquidity = 0
            state = PositionState(known=known, liquidity=opening_liquidity)
            positions[key] = state
            state.episode_start_block = int(event["block_number"])
            if not known:
                counters["unknown_start_positions"] += 1
        accrue(state, lower, upper, growth0, growth1)

        if kind == "mint":
            counters["mints"] += 1
            if state.known:
                state.liquidity += int(event["liquidity"])
        elif kind == "burn":
            counters["burns"] += 1
            burned = int(event["liquidity"])
            if state.known and burned > state.liquidity:
                state.known = False
                counters["positions_invalidated_by_excess_burn"] += 1
            elif state.known:
                state.liquidity -= burned
            if state.anchored:
                state.pending_burn_principal0 += int(event["amount0"])
                state.pending_burn_principal1 += int(event["amount1"])
        elif kind == "collect":
            counters.setdefault("collects", 0)
            counters["collects"] += 1
            if not state.known:
                continue
            if not state.anchored:
                state.anchored = True
                state.simulated_at_collect0 = state.simulated_total0
                state.simulated_at_collect1 = state.simulated_total1
                state.pending_burn_principal0 = 0
                state.pending_burn_principal1 = 0
                counters.setdefault("first_collect_anchors", 0)
                counters["first_collect_anchors"] += 1
                continue
            collect = (int(event["amount0"]), int(event["amount1"]))
            simulated_deltas = (
                state.simulated_total0 - state.simulated_at_collect0,
                state.simulated_total1 - state.simulated_at_collect1,
            )
            state.unmatched_simulated0 += simulated_deltas[0]
            state.unmatched_simulated1 += simulated_deltas[1]
            settlements = (
                settle_collect(collect[0], state.pending_burn_principal0),
                settle_collect(collect[1], state.pending_burn_principal1),
            )
            state.pending_burn_principal0 = settlements[0][2]
            state.pending_burn_principal1 = settlements[1][2]
            simulations = (state.unmatched_simulated0, state.unmatched_simulated1)
            for token, (settlement, simulated) in enumerate(zip(settlements, simulations)):
                true_fee = settlement[1]
                if true_fee <= 0 or not math.isfinite(simulated):
                    counters["nonpositive_truth_token_sides"] += 1
                    continue
                aggregate = aggregates[(pool, owner, lower, upper, token)]
                aggregate[0] += simulated
                aggregate[1] += true_fee
                aggregate[2] += 1
                counters["scored_range_token_observations"] += 1
                if token == 0:
                    state.unmatched_simulated0 = 0.0
                else:
                    state.unmatched_simulated1 = 0.0
            state.simulated_at_collect0 = state.simulated_total0
            state.simulated_at_collect1 = state.simulated_total1
        else:
            raise ValueError(f"unknown event type: {kind}")
    counters["open_lifecycles_excluded"] = sum(
        state.known and state.liquidity != 0 for state in positions.values()
    )
    counters["position_keys"] = len(positions)
    counters["known_position_keys_at_end"] = sum(state.known for state in positions.values())
    return counters


def main() -> None:
    OUT.mkdir(exist_ok=True)
    aggregates: dict[tuple[str, str, int, int, int], list[float | int]] = defaultdict(
        lambda: [0.0, 0, 0]
    )
    counters = {}
    for pool in POOLS:
        print(f"calibrating {pool}", flush=True)
        counters[pool] = process_pool(pool, aggregates)
        print(json.dumps(counters[pool], indent=2), flush=True)
    stats = {key: Stats() for key in [*POOLS, "v3", "aero_cl", "all"]}
    range_fields = [
        "pool_address",
        "venue",
        "owner",
        "tick_lower",
        "tick_upper",
        "token",
        "collection_episodes",
        "true_fee_raw",
        "simulated_fee_raw",
        "relative_error",
        "error_sign",
    ]
    with RANGE_OBSERVATIONS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=range_fields)
        writer.writeheader()
        for (pool, owner, lower, upper, token), values in sorted(aggregates.items()):
            simulated, truth, episodes = float(values[0]), int(values[1]), int(values[2])
            if truth <= 0:
                continue
            venue = POOL_META[pool]["venue"]
            row = {
                "pool_address": pool,
                "venue": venue,
                "owner": owner,
                "tick_lower": lower,
                "tick_upper": upper,
                "token": token,
                "collection_episodes": episodes,
                "true_fee_raw": truth,
                "simulated_fee_raw": simulated,
                "relative_error": abs(simulated - truth) / truth,
                "error_sign": "positive" if simulated > truth else ("negative" if simulated < truth else "tie"),
            }
            writer.writerow(row)
            stats[pool].add(simulated, truth)
            stats[venue].add(simulated, truth)
            stats["all"].add(simulated, truth)
    summaries = {key: value.summary() for key, value in stats.items()}
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "chain_id": 8453,
        "block_window_inclusive": [START_BLOCK, END_BLOCK],
        "truth_definition": "per (pool,owner,tickLower,tickUpper,token): first Collect is a clearing anchor; thereafter Burn principal carries across partial Collects and only residual cumulative Collect-minus-Burn is fee; clean episodes aggregate by range/token",
        "simulation_definition": "positive swap input * fee tier * reconstructed position liquidity / swap event active liquidity; final event tick used for crossing swaps",
        "boundary_rule": "for v3, reconstruct opening liquidity from an end-block positions() snapshot minus in-window net Mint/Burn; for aero_cl, require an in-window Mint before scoring because gauge stake/unstake transfers liquidity without Mint/Burn; exclude invalid underflow paths, pre-anchor observations, and unmatched trailing principal",
        "gate": {"median_relative_error_lt": 0.15, "sign_test_p_gt": 0.05},
        "event_counters": counters,
        "summaries": summaries,
        "overall_passed": summaries["all"]["passed"],
        "v3_passed": summaries["v3"]["passed"],
        "aero_cl_passed": summaries["aero_cl"]["passed"],
        "range_observations_csv": str(RANGE_OBSERVATIONS),
    }
    SUMMARY.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
