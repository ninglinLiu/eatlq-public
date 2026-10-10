"""Pure B3 research functions; deliberately independent of live code."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable

WETH = "0x4200000000000000000000000000000000000006"
NOTIONAL_WETH = 0.001
GAS_USD_PER_LEG = 0.075


def net_balances(events: Iterable[dict], cutoff: int) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in events:
        if int(e["block_number"]) > cutoff:
            continue
        n = int(e["amount"])
        out[str(e["sender"]).lower()] -= n
        out[str(e["recipient"]).lower()] += n
    return dict(out)


def reserve_net(events: Iterable[dict], pool: str, cutoff: int) -> int:
    pool = pool.lower()
    total = 0
    for e in events:
        if int(e["block_number"]) <= cutoff:
            n = int(e["amount"])
            total += n if str(e["recipient"]).lower() == pool else 0
            total -= n if str(e["sender"]).lower() == pool else 0
    return total


def newly_positive(events: Iterable[dict], b0: int, b1: int, b2: int,
                   excluded: set[str]) -> tuple[set[str], set[str]]:
    x = {a.lower() for a in excluded}
    n0, n1, n2 = (net_balances(events, b) for b in (b0, b1, b2))
    addresses = (set(n0) | set(n1) | set(n2)) - x
    h1 = {a for a in addresses if n0.get(a, 0) <= 0 < n1.get(a, 0)}
    h2 = {a for a in addresses if n1.get(a, 0) <= 0 < n2.get(a, 0)}
    return h1, h2


def accelerated(second: float, first: float, threshold: float = 2.0) -> bool:
    return first > 0 and second / first >= threshold


def classify_verdict(window_rows: dict[str, dict]) -> str:
    """Sample adequacy outranks signs; FAIL is powered-only."""
    powered = {w: r for w, r in window_rows.items() if int(r.get("n", 0)) >= 20}
    if not powered:
        return "NEEDS_REVIEW/INCONCLUSIVE"
    for w, r in powered.items():
        if float(r["mean"]) <= 0 or float(r["alpha_delta"]) <= 0:
            return "FAIL:W3_COLLAPSE" if w == "W3" else "FAIL:POWERED_WINDOW"
        if float(r.get("top1_removed_mean", r["mean"])) <= 0:
            return "FAIL:TAIL_COLLAPSE"
        if float(r.get("top1_removed_alpha", r["alpha_delta"])) <= 0:
            return "FAIL:TAIL_COLLAPSE"
    if len(powered) < 2 or any(int(r.get("n", 0)) < 20 for r in window_rows.values()):
        return "NEEDS_REVIEW/INCONCLUSIVE"
    return "PASS_RESEARCH"


def execute_path(swaps: list[dict], eligibility_ts: float, eth_usd: float,
                 ladder: bool = True) -> dict:
    """ITT real-swap matcher. Prices are WETH/target average execution prices."""
    entries = [s for s in swaps if s["ts"] >= eligibility_ts]
    if not entries or entries[0]["ts"] > eligibility_ts + 1800 or entries[0]["weth"] < NOTIONAL_WETH:
        return {"filled": False, "gross_return": 0.0, "net_return": 0.0, "gas_legs": 0}
    entry = entries[0]
    qty = NOTIONAL_WETH / entry["price"]
    exits = [s for s in swaps if s["side"] == "sell" and s["ts"] > entry["ts"]]
    proceeds = 0.0
    sold = 0.0
    legs = 1
    fills: list[dict] = []
    if ladder:
        after_ts = entry["ts"]
        for multiple in (1.3, 1.6, 2.2):
            hit = next((s for s in exits if after_ts < s["ts"] <= entry["ts"] + 3600
                        and s["price"] >= entry["price"] * multiple
                        and s not in fills), None)
            if hit is not None:
                part = qty / 3.0
                proceeds += part * hit["price"]
                sold += part
                legs += 1
                fills.append(hit)
                after_ts = hit["ts"]
    remainder = max(0.0, qty - sold)
    if remainder > 1e-30:
        terminal = next((s for s in exits if entry["ts"] + 3600 <= s["ts"] <= entry["ts"] + 7200
                         and s not in fills), None)
        legs += 1  # attempted terminal sell, successful or failed
        if terminal is not None:
            proceeds += remainder * terminal["price"]
            fills.append(terminal)
    gross = proceeds / NOTIONAL_WETH - 1.0
    gas_drag = GAS_USD_PER_LEG * legs / (NOTIONAL_WETH * eth_usd)
    return {"filled": True, "gross_return": gross, "net_return": gross - gas_drag,
            "gas_legs": legs, "gas_usd": GAS_USD_PER_LEG * legs,
            "entry": entry, "exits": fills}


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "top1pct_positive_contribution": None,
                "top1_removed_mean": None}
    k = max(1, math.ceil(0.01 * len(values)))
    order = sorted(values, reverse=True)
    positive = sum(max(0.0, v) for v in values)
    contribution = sum(max(0.0, v) for v in order[:k]) / positive if positive else 0.0
    remain = order[k:]
    return {"n": len(values), "mean": sum(values) / len(values),
            "top1pct_positive_contribution": contribution,
            "top1_removed_mean": sum(remain) / len(remain) if remain else None}


def monte_carlo(by_window: dict[str, list[float]], paths: int = 10_000,
                seed: int = 20260714) -> dict:
    rng = random.Random(seed)
    terminals, drawdowns = [], []
    for _ in range(paths):
        equity = peak = 100.0
        max_dd = 0.0
        for window in sorted(by_window):
            vals = by_window[window]
            for _ in vals:
                equity += rng.choice(vals) if vals else 0.0
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
        terminals.append(equity)
        drawdowns.append(max_dd)
    terminals.sort(); drawdowns.sort()
    return {"paths": paths, "seed": seed, "terminal_p5": terminals[int(.05*paths)],
            "max_drawdown_p95": drawdowns[int(.95*paths)]}
