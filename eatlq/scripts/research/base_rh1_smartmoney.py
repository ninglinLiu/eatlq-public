#!/usr/bin/env python3
"""Base reproduction of the RH point-in-time smart-money copy falsification.

Research-only. Reads the bounded B1 high-confidence tx.from window from ClickHouse,
never writes ClickHouse/Redis and never calls RPC or any execution service.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from base_rh1_smartmoney_core import (
    Trade,
    WalletState,
    build_snapshots,
    is_launch_era_smart,
    metrics,
    parse_ch_time,
    process_trade,
    simulate_copy,
    source_lot_return,
    summary,
)

REPO = Path(__file__).resolve().parents[2]
CH = "eatlq-clickhouse-1"
WETH = "0x4200000000000000000000000000000000000006"
DEFAULT_START = 46_389_458
DEFAULT_END = 46_417_105
SEED = 20_260_714
DELAYS = (0, 10, 30, 60)
NOTIONAL_WETH = 0.001
GAS_WETH_PER_LEG = 0.0000215  # $0.075/leg at about $3,488/ETH; B6 frozen cost.


def ch_json(sql: str, timeout: int = 180) -> list[dict]:
    proc = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql, "--max_threads", "2"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def sql_quote(values: list[str]) -> str:
    return ",".join("'" + x.replace("'", "''") + "'" for x in values)


def load_exclusions() -> tuple[set[str], set[str]]:
    payload = json.loads((REPO / "configs/research/base_attribution_exclusions.json").read_text())
    excluded = {x["address"].lower() for x in payload["addresses"]}
    quotes = {x["address"].lower() for x in payload["addresses"] if x["category"] == "quote_token"}
    return excluded, quotes


def load_rows(start_block: int, end_block: int) -> tuple[list[Trade], dict, dict]:
    swap_sql = f"""
    SELECT
      toString(block_time) AS block_time, block_number, lower(tx_hash) AS tx_hash, log_index,
      lower(pool_address) AS pool_address, toString(amount0) AS amount0, toString(amount1) AS amount1
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id=8453 AND block_number BETWEEN {start_block} AND {end_block}
      AND block_time IS NOT NULL AND removed=0
    ORDER BY block_number, log_index, tx_hash FORMAT JSONEachRow
    """
    swaps = ch_json(swap_sql)
    tx_rows = ch_json(
        "SELECT lower(tx_hash) AS tx_hash, lower(tx_from) AS tx_from "
        "FROM eatlq.tx_from_by_hash WHERE chain_id=8453 FORMAT JSONEachRow"
    )
    tx_from = {x["tx_hash"]: x["tx_from"] for x in tx_rows}
    pools = sorted({x["pool_address"] for x in swaps})
    pool_rows = ch_json(
        "SELECT lower(pool_address) AS pool_key, "
        "argMax(lower(token0_address), discovered_at) AS token0_address, "
        "argMax(lower(token1_address), discovered_at) AS token1_address, "
        "argMax(token0_decimals, discovered_at) AS token0_decimals, "
        "argMax(token1_decimals, discovered_at) AS token1_decimals, "
        "argMax(fee_bps, discovered_at) AS fee_bps, argMax(created_block, discovered_at) AS created_block "
        "FROM eatlq.discovered_pools WHERE chain_id=8453 AND lower(pool_address) IN ("
        + sql_quote(pools)
        + ") GROUP BY lower(pool_address) FORMAT JSONEachRow"
    )
    pool_meta = {x["pool_key"]: x for x in pool_rows}
    first_rows = ch_json(
        "SELECT lower(pool_address) AS pool_key, toString(min(block_time)) AS first_swap_time "
        "FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND block_time IS NOT NULL "
        "AND lower(pool_address) IN ("
        + sql_quote(pools)
        + ") GROUP BY lower(pool_address) FORMAT JSONEachRow",
        timeout=300,
    )
    first_swap = {x["pool_key"]: parse_ch_time(x["first_swap_time"]) for x in first_rows}
    excluded, quotes = load_exclusions()

    out: list[Trade] = []
    reasons: dict[str, int] = defaultdict(int)
    for row in swaps:
        wallet = tx_from.get(row["tx_hash"], "").lower()
        if not wallet:
            reasons["missing_tx_from"] += 1
            continue
        if wallet in excluded:
            reasons["excluded_wallet"] += 1
            continue
        meta = pool_meta.get(row["pool_address"])
        if not meta:
            reasons["missing_pool_meta"] += 1
            continue
        t0, t1 = meta["token0_address"], meta["token1_address"]
        if t0 == WETH and t1 not in quotes:
            quote_raw, target_raw = int(row["amount0"]), int(row["amount1"])
            target, decimals = t1, int(meta["token1_decimals"])
        elif t1 == WETH and t0 not in quotes:
            quote_raw, target_raw = int(row["amount1"]), int(row["amount0"])
            target, decimals = t0, int(meta["token0_decimals"])
        else:
            reasons["not_weth_memecoin_pair"] += 1
            continue
        side = "buy" if quote_raw > 0 and target_raw < 0 else "sell" if quote_raw < 0 and target_raw > 0 else "unknown"
        if side == "unknown":
            reasons["unknown_side"] += 1
            continue
        quote_weth = abs(quote_raw) / 1e18
        target_qty = abs(target_raw) / (10**decimals)
        if quote_weth <= 0 or target_qty <= 0:
            reasons["zero_amount"] += 1
            continue
        out.append(
            Trade(
                ts=parse_ch_time(row["block_time"]),
                block=int(row["block_number"]),
                tx_hash=row["tx_hash"],
                log_index=int(row["log_index"]),
                pool=row["pool_address"],
                token=target,
                wallet=wallet,
                side=side,
                target_qty=target_qty,
                quote_weth=quote_weth,
                price_weth=quote_weth / target_qty,
            )
        )
    coverage = {
        "raw_swaps": len(swaps),
        "tx_from_rows": len(tx_rows),
        "usable_memecoin_trades": len(out),
        "wallets": len({x.wallet for x in out}),
        "pools": len({x.pool for x in out}),
        "tokens": len({x.token for x in out}),
        "first_time": datetime.fromtimestamp(min(x.ts for x in out), timezone.utc).isoformat() if out else None,
        "last_time": datetime.fromtimestamp(max(x.ts for x in out), timezone.utc).isoformat() if out else None,
        "drop_reasons": dict(sorted(reasons.items())),
    }
    return sorted(out, key=lambda x: (x.ts, x.block, x.log_index, x.tx_hash)), first_swap, coverage


def distance(a: dict[str, float], b: dict[str, float]) -> float:
    fields = ("n_closed", "buys", "sells", "quote_weth", "recency_sec")
    total = 0.0
    for name in fields:
        scale = max(1.0, abs(a.get(name, 0.0)), abs(b.get(name, 0.0)))
        total += ((a.get(name, 0.0) - b.get(name, 0.0)) / scale) ** 2
    return total


def aggregate_records(records: list[dict], key: str = "return_frac") -> dict:
    return summary([float(x[key]) for x in records])


def run(start_block: int, end_block: int) -> tuple[dict, list[dict]]:
    trades, first_swap, coverage = load_rows(start_block, end_block)
    snapshots, final_states = build_snapshots(trades)
    by_pool: dict[str, list[Trade]] = defaultdict(list)
    by_wallet: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        by_pool[trade.pool].append(trade)
        by_wallet[trade.wallet].append(trade)
    t0, t1 = min(x.ts for x in trades), max(x.ts for x in trades)
    split = t0 + 0.60 * (t1 - t0)
    eval_end = t1 - 7200

    train_states: dict[str, WalletState] = defaultdict(WalletState)
    for trade in trades:
        if trade.ts >= split:
            break
        process_trade(train_states[trade.wallet], trade)
    train_metrics = {wallet: metrics(state) for wallet, state in train_states.items()}
    rh_ranked = sorted(
        (
            (wallet, m)
            for wallet, m in train_metrics.items()
            if m["n_closed"] >= 2
            and m["realized_weth"] > 0
            and m["top1_profit_share"] < 0.50
            and m["sell_buy_ratio"] <= 2.0
        ),
        key=lambda item: (-item[1]["realized_weth"], item[0]),
    )
    rh_wallets = {wallet for wallet, _ in rh_ranked[:30]}
    abs_ranked = sorted(
        (
            (wallet, m)
            for wallet, m in train_metrics.items()
            if m["n_closed"] >= 2 and m["realized_weth"] > 0 and m["sell_buy_ratio"] <= 2.0
        ),
        key=lambda item: (-item[1]["realized_weth"], item[0]),
    )
    abs_wallets = {wallet for wallet, _ in abs_ranked[:30]}
    winrate_ranked = sorted(
        ((w, m) for w, m in train_metrics.items() if m["n_closed"] >= 15 and m["sell_buy_ratio"] <= 2.0),
        key=lambda item: (-item[1]["winrate"], -item[1]["realized_weth"], item[0]),
    )
    median_ranked = sorted(
        ((w, m) for w, m in train_metrics.items() if m["n_closed"] >= 2 and m["sell_buy_ratio"] <= 2.0),
        key=lambda item: (-item[1]["median_pnl_weth"], -item[1]["realized_weth"], item[0]),
    )
    risk_ranked = sorted(
        (
            (w, m)
            for w, m in train_metrics.items()
            if m["n_closed"] >= 2
            and m["realized_weth"] > 0
            and m["pnl_std_weth"] > 0
            and m["sell_buy_ratio"] <= 2.0
        ),
        key=lambda item: (-(item[1]["realized_weth"] / item[1]["pnl_std_weth"]), item[0]),
    )
    selector_wallets = {
        "abs_pnl": abs_wallets,
        "winrate": {wallet for wallet, _ in winrate_ranked[:30]},
        "median_pnl": {wallet for wallet, _ in median_ranked[:30]},
        "deconcentrated": rh_wallets,
        "risk_adjusted": {wallet for wallet, _ in risk_ranked[:30]},
    }

    eligible_buys: list[Trade] = []
    candidate_buys: list[Trade] = []
    for trade in trades:
        if trade.side != "buy" or trade.ts < split or trade.ts > eval_end:
            continue
        candidate_buys.append(trade)
        if is_launch_era_smart(snapshots.get(trade.key, {})):
            eligible_buys.append(trade)

    def dedup(items: list[Trade], new_only: bool) -> list[Trade]:
        picked: list[Trade] = []
        seen: set[str] = set()
        for trade in items:
            if trade.token in seen:
                continue
            if new_only and trade.ts - first_swap.get(trade.pool, -1e30) > 86400:
                continue
            seen.add(trade.token)
            picked.append(trade)
        return picked

    launch_signals = dedup(eligible_buys, new_only=True)
    launch_all_pool_signals = dedup(eligible_buys, new_only=False)
    rh_buys = [trade for trade in candidate_buys if trade.wallet in rh_wallets]
    signals = dedup(rh_buys, new_only=True)
    rh_all_pool_signals = dedup(rh_buys, new_only=False)
    battery_signals = {
        name: dedup([trade for trade in candidate_buys if trade.wallet in wallets], new_only=True)
        for name, wallets in selector_wallets.items()
    }
    rows: list[dict] = []
    results: dict[str, dict] = {}
    for delay in DELAYS:
        recs = []
        for signal in signals:
            sim = simulate_copy(signal, by_pool[signal.pool], delay, NOTIONAL_WETH, GAS_WETH_PER_LEG)
            if sim is None:
                continue
            own = source_lot_return(signal, by_wallet[signal.wallet], signal.ts + 7200)
            rec = {
                "kind": "smart",
                "delay_sec": delay,
                "signal_ts": signal.ts,
                "signal_wallet": signal.wallet,
                "token": signal.token,
                "pool": signal.pool,
                "source_tx": signal.tx_hash,
                "source_return_frac": own,
                "delay_tax_frac": own - float(sim["return_frac"]),
                **sim,
            }
            recs.append(rec)
            rows.append(rec)
        results[str(delay)] = aggregate_records(recs)
        results[str(delay)]["source_wallet_lot_proxy"] = summary([x["source_return_frac"] for x in recs])
        results[str(delay)]["delay_tax"] = summary([x["delay_tax_frac"] for x in recs])

    rng = random.Random(SEED)
    random_group_means: list[float] = []
    smart10 = [x for x in rows if x["kind"] == "smart" and x["delay_sec"] == 10]
    for group in range(100):
        placebo_returns: list[float] = []
        for smart in signals:
            sm = train_metrics.get(smart.wallet, snapshots.get(smart.key, {}))
            candidates = [
                x
                for x in candidate_buys
                if x.pool == smart.pool
                and x.wallet != smart.wallet
                and abs(x.ts - smart.ts) <= 1800
                and not is_launch_era_smart(snapshots.get(x.key, {}))
            ]
            candidates.sort(key=lambda x: distance(sm, snapshots.get(x.key, {})))
            if not candidates:
                continue
            random_signal = rng.choice(candidates[: min(5, len(candidates))])
            sim = simulate_copy(random_signal, by_pool[random_signal.pool], 10, NOTIONAL_WETH, GAS_WETH_PER_LEG)
            if sim is not None:
                placebo_returns.append(float(sim["return_frac"]))
        if placebo_returns:
            random_group_means.append(mean(placebo_returns))

    random_group_means.sort()
    random_control = {
        "groups_requested": 100,
        "groups_observed": len(random_group_means),
        "mean_of_group_means": mean(random_group_means) if random_group_means else None,
        "p95_group_mean": random_group_means[min(len(random_group_means) - 1, int(0.95 * len(random_group_means)))] if random_group_means else None,
        "smart_10s_mean": mean([float(x["return_frac"]) for x in smart10]) if smart10 else None,
    }

    battery_results = {}
    for name, selector_signals in battery_signals.items():
        selector_recs = []
        for signal in selector_signals:
            sim = simulate_copy(signal, by_pool[signal.pool], 10, NOTIONAL_WETH, GAS_WETH_PER_LEG)
            if sim is not None:
                rec = {
                    "kind": "selector_battery",
                    "selector": name,
                    "delay_sec": 10,
                    "signal_ts": signal.ts,
                    "signal_wallet": signal.wallet,
                    "token": signal.token,
                    "pool": signal.pool,
                    "source_tx": signal.tx_hash,
                    **sim,
                }
                selector_recs.append(rec)
                rows.append(rec)
        battery_results[name] = {
            "selected_wallets": len(selector_wallets[name]),
            "signals": len(selector_signals),
            **aggregate_records(selector_recs),
        }

    window_rows = []
    if eval_end > split:
        width = (eval_end - split) / 3
        for i in range(3):
            lo, hi = split + i * width, split + (i + 1) * width
            vals = [float(x["return_frac"]) for x in smart10 if lo <= float(x["signal_ts"]) < hi]
            window_rows.append({"window": i + 1, "start": lo, "end": hi, **summary(vals)})

    canonical_eligible = [
        wallet
        for wallet, state in final_states.items()
        if len(state.closed) >= 20
        and state.closed
        and max(x.closed_at for x in state.closed) - min(x.opened_at for x in state.closed) >= 30 * 86400
    ]
    s10 = results["10"]
    powered = int(s10["n"] or 0) >= 20
    exploratory_fail = powered and (float(s10["mean"] or 0.0) <= 0 or float(s10["top1_removed_mean"] or 0.0) <= 0)
    verdict = "FAIL" if exploratory_fail else "INCONCLUSIVE_SMALL_N" if not powered else "NEEDS_REVIEW"
    payload = {
        "research_only": True,
        "no_go": True,
        "chain_id": 8453,
        "method": "RH method reproduced on Base high-confidence tx.from window",
        "params": {
            "start_block": start_block,
            "end_block": end_block,
            "split_fraction": 0.60,
            "delays_sec": list(DELAYS),
            "notional_weth": NOTIONAL_WETH,
            "gas_weth_per_leg": GAS_WETH_PER_LEG,
            "timeout_sec": 3600,
            "final_exit_sec": 7200,
            "random_seed": SEED,
        },
        "coverage": coverage,
        "time_split": {
            "train_start": datetime.fromtimestamp(t0, timezone.utc).isoformat(),
            "oos_start": datetime.fromtimestamp(split, timezone.utc).isoformat(),
            "oos_evaluable_end": datetime.fromtimestamp(eval_end, timezone.utc).isoformat(),
            "data_end": datetime.fromtimestamp(t1, timezone.utc).isoformat(),
        },
        "selector": {
            "canonical_30d_eligible_wallets": len(canonical_eligible),
            "launch_era_rule": "n_closed>=3, span>=2h, pnl>0, win>=55%, PF>=1.2, top1<50%, sell/buy<=2",
            "eligible_oos_buys": len(eligible_buys),
            "launch_era_dedup_new_24h_signals": len(launch_signals),
            "launch_era_dedup_all_pool_signals": len(launch_all_pool_signals),
            "rh_deconcentrated_rule": "cut-time pnl>0, n_closed>=2, top1_profit_share<50%, sell/buy<=2; rank realized_weth top30",
            "rh_deconcentrated_eligible_wallets": len(rh_ranked),
            "rh_deconcentrated_selected_wallets": len(rh_wallets),
            "rh_oos_buys": len(rh_buys),
            "rh_dedup_new_24h_signals": len(signals),
            "rh_dedup_all_pool_signals": len(rh_all_pool_signals),
            "train_wallet_episode_diagnostics": {
                "wallets": len(train_metrics),
                "n_closed_ge_1": sum(1 for m in train_metrics.values() if m["n_closed"] >= 1),
                "n_closed_ge_2": sum(1 for m in train_metrics.values() if m["n_closed"] >= 2),
                "n_closed_ge_3": sum(1 for m in train_metrics.values() if m["n_closed"] >= 3),
                "n_closed_ge_2_and_pnl_positive": sum(
                    1 for m in train_metrics.values() if m["n_closed"] >= 2 and m["realized_weth"] > 0
                ),
                "plus_top1_profit_share_lt_50pct": sum(
                    1
                    for m in train_metrics.values()
                    if m["n_closed"] >= 2
                    and m["realized_weth"] > 0
                    and m["top1_profit_share"] < 0.50
                ),
                "plus_sell_buy_ratio_le_2": len(rh_ranked),
                "max_n_closed": max((m["n_closed"] for m in train_metrics.values()), default=0),
            },
        },
        "delay_results": results,
        "rh_selector_battery_10s": battery_results,
        "matched_random_control_10s": random_control,
        "oos_subwindows_10s": window_rows,
        "verdict": {
            "canonical": "INCONCLUSIVE_COVERAGE_AND_HISTORY",
            "rh_method_exploratory": verdict,
            "powered_n_ge_20": powered,
            "reason": "canonical wallet qualification needs 30d; high-confidence Base tx.from covers 15.4h and 34 pools",
        },
    }
    return payload, rows


def write_outputs(payload: dict, rows: list[dict], output: Path, trades_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    fields = sorted({key for row in rows for key in row})
    with trades_output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-block", type=int, default=DEFAULT_START)
    parser.add_argument("--end-block", type=int, default=DEFAULT_END)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / "mvp/diagnostics/base_rh1_smartmoney_20260714.json",
    )
    parser.add_argument(
        "--trades-output",
        type=Path,
        default=REPO / "mvp/diagnostics/base_rh1_smartmoney_trades_20260714.csv",
    )
    args = parser.parse_args()
    payload, rows = run(args.start_block, args.end_block)
    write_outputs(payload, rows, args.output, args.trades_output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
