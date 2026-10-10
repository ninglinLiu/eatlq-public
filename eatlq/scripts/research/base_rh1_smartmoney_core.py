#!/usr/bin/env python3
"""Pure helpers for the Base RH1 smart-money falsification replay."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Iterable


@dataclass(frozen=True)
class Trade:
    ts: float
    block: int
    tx_hash: str
    log_index: int
    pool: str
    token: str
    wallet: str
    side: str
    target_qty: float
    quote_weth: float
    price_weth: float

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.log_index)


@dataclass
class Episode:
    token: str
    opened_at: float
    closed_at: float
    cost_weth: float
    proceeds_weth: float

    @property
    def pnl_weth(self) -> float:
        return self.proceeds_weth - self.cost_weth

    @property
    def return_frac(self) -> float:
        return self.pnl_weth / self.cost_weth if self.cost_weth > 0 else -1.0


@dataclass
class _OpenEpisode:
    token: str
    opened_at: float
    qty: float = 0.0
    total_bought_qty: float = 0.0
    cost_weth: float = 0.0
    proceeds_weth: float = 0.0


@dataclass
class WalletState:
    closed: list[Episode] = field(default_factory=list)
    open_by_token: dict[str, _OpenEpisode] = field(default_factory=dict)
    buys: int = 0
    sells: int = 0
    quote_weth: float = 0.0
    last_ts: float | None = None


def parse_ch_time(value: str) -> float:
    value = value.replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def metrics(state: WalletState) -> dict[str, float]:
    eps = state.closed
    pnls = [x.pnl_weth for x in eps]
    gains = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]
    total = sum(pnls)
    profit_sum = sum(gains)
    span = (max((x.closed_at for x in eps), default=0.0) - min((x.opened_at for x in eps), default=0.0))
    return {
        "n_closed": float(len(eps)),
        "winrate": (sum(1 for x in pnls if x > 0) / len(pnls)) if pnls else 0.0,
        "profit_factor": (profit_sum / sum(losses)) if losses else (999.0 if profit_sum > 0 else 0.0),
        "realized_weth": total,
        "median_pnl_weth": median(pnls) if pnls else 0.0,
        "pnl_std_weth": pstdev(pnls) if len(pnls) >= 2 else 0.0,
        "top1_profit_share": (max(gains) / profit_sum) if gains and profit_sum > 0 else 1.0,
        "span_sec": span,
        "buys": float(state.buys),
        "sells": float(state.sells),
        "sell_buy_ratio": state.sells / state.buys if state.buys else 999.0,
        "quote_weth": state.quote_weth,
        "recency_sec": 0.0,
    }


def is_launch_era_smart(m: dict[str, float]) -> bool:
    """Frozen exploratory selector; never sufficient for canonical PASS."""
    return (
        m["n_closed"] >= 3
        and m["span_sec"] >= 2 * 3600
        and m["realized_weth"] > 0
        and m["winrate"] >= 0.55
        and m["profit_factor"] >= 1.20
        and m["top1_profit_share"] < 0.50
        and m["sell_buy_ratio"] <= 2.0
    )


def process_trade(state: WalletState, trade: Trade, dust_frac: float = 0.01) -> Episode | None:
    state.last_ts = trade.ts
    state.quote_weth += trade.quote_weth
    if trade.side == "buy":
        state.buys += 1
        ep = state.open_by_token.get(trade.token)
        if ep is None:
            ep = _OpenEpisode(token=trade.token, opened_at=trade.ts)
            state.open_by_token[trade.token] = ep
        ep.qty += trade.target_qty
        ep.total_bought_qty += trade.target_qty
        ep.cost_weth += trade.quote_weth
        return None

    state.sells += 1
    ep = state.open_by_token.get(trade.token)
    if ep is None or ep.qty <= 0 or trade.target_qty <= 0:
        return None
    matched = min(ep.qty, trade.target_qty)
    ep.proceeds_weth += trade.quote_weth * matched / trade.target_qty
    ep.qty -= matched
    if ep.qty > max(1e-18, ep.total_bought_qty * dust_frac):
        return None
    closed = Episode(
        token=ep.token,
        opened_at=ep.opened_at,
        closed_at=trade.ts,
        cost_weth=ep.cost_weth,
        proceeds_weth=ep.proceeds_weth,
    )
    state.closed.append(closed)
    del state.open_by_token[trade.token]
    return closed


def build_snapshots(trades: Iterable[Trade]) -> tuple[dict[tuple[str, int], dict[str, float]], dict[str, WalletState]]:
    states: dict[str, WalletState] = defaultdict(WalletState)
    snapshots: dict[tuple[str, int], dict[str, float]] = {}
    for trade in sorted(trades, key=lambda x: (x.ts, x.block, x.log_index, x.tx_hash)):
        state = states[trade.wallet]
        if trade.side == "buy":
            m = metrics(state)
            if state.last_ts is not None:
                m["recency_sec"] = max(0.0, trade.ts - state.last_ts)
            snapshots[trade.key] = m
        process_trade(state, trade)
    return snapshots, states


def source_lot_return(signal: Trade, wallet_trades: Iterable[Trade], horizon_end: float) -> float:
    remaining = signal.target_qty
    proceeds = 0.0
    for trade in sorted(wallet_trades, key=lambda x: (x.ts, x.log_index)):
        if trade.ts <= signal.ts or trade.ts > horizon_end or trade.side != "sell" or trade.token != signal.token:
            continue
        matched = min(remaining, trade.target_qty)
        proceeds += matched * trade.price_weth
        remaining -= matched
        if remaining <= 1e-18:
            break
    return proceeds / signal.quote_weth - 1.0 if signal.quote_weth > 0 else -1.0


def simulate_copy(
    signal: Trade,
    pool_trades: Iterable[Trade],
    delay_sec: int,
    notional_weth: float,
    gas_weth_per_leg: float,
    timeout_sec: int = 3600,
    final_sec: int = 7200,
) -> dict[str, float | int | str] | None:
    path = sorted(pool_trades, key=lambda x: (x.ts, x.block, x.log_index, x.tx_hash))
    entry = next(
        (
            x
            for x in path
            if x.ts >= signal.ts + delay_sec and x.quote_weth >= notional_weth and x.price_weth > 0
        ),
        None,
    )
    if entry is None:
        return None
    qty = notional_weth / entry.price_weth
    tranche = qty / 3.0
    remaining = qty
    proceeds = 0.0
    legs = 1
    targets = deque([1.30, 1.60, 2.20])
    for trade in path:
        if trade.ts <= entry.ts or trade.ts > entry.ts + timeout_sec or trade.side != "sell":
            continue
        capacity = trade.target_qty
        while targets and capacity >= tranche and trade.price_weth >= entry.price_weth * targets[0]:
            proceeds += tranche * trade.price_weth
            remaining -= tranche
            capacity -= tranche
            targets.popleft()
            legs += 1
    if remaining > 1e-18:
        final = next(
            (
                x
                for x in path
                if x.ts >= entry.ts + timeout_sec
                and x.ts <= entry.ts + final_sec
                and x.side == "sell"
                and x.target_qty >= remaining
            ),
            None,
        )
        if final is not None:
            proceeds += remaining * final.price_weth
            remaining = 0.0
            legs += 1
    net = proceeds - notional_weth - legs * gas_weth_per_leg
    return {
        "entry_ts": entry.ts,
        "entry_tx": entry.tx_hash,
        "entry_price_weth": entry.price_weth,
        "return_frac": net / notional_weth,
        "unfilled_frac": max(0.0, remaining / qty),
        "legs": legs,
    }


def summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "winrate": None, "top1_removed_mean": None}
    ordered = sorted(values, reverse=True)
    remove_n = max(1, int(len(values) * 0.01))
    trimmed = ordered[remove_n:]
    return {
        "n": len(values),
        "mean": mean(values),
        "median": median(values),
        "winrate": sum(1 for x in values if x > 0) / len(values),
        "top1_removed_mean": mean(trimmed) if trimmed else None,
    }
