"""FIFO episode construction with CENSORED_TRANSFER_OUT truncation.

Adapted from RH1 smartmoney core — does NOT use abs-PnL ranking as a feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd


@dataclass
class Episode:
    actor: str
    token: str
    pool: str
    opened_at: float
    closed_at: float
    cost_weth: float
    proceeds_weth: float
    max_qty: float
    qty_at_close: float
    censored: bool = False
    censor_reason: str = ""

    @property
    def pnl_weth(self) -> float:
        if self.censored:
            return float("nan")
        return self.proceeds_weth - self.cost_weth

    @property
    def return_frac(self) -> float:
        if self.censored or self.cost_weth <= 0:
            return float("nan")
        return self.pnl_weth / self.cost_weth

    def retention_at(self, hold_s: float, sell_times_qty: list[tuple[float, float]]) -> float:
        """Fraction of max_qty still held at opened_at + hold_s."""
        if self.max_qty <= 0:
            return 0.0
        t_cut = self.opened_at + hold_s
        sold = sum(q for t, q in sell_times_qty if t <= t_cut)
        remaining = max(0.0, self.max_qty - sold)
        return min(1.0, remaining / self.max_qty)


@dataclass
class _Open:
    actor: str
    token: str
    pool: str
    opened_at: float
    qty: float = 0.0
    max_qty: float = 0.0
    cost_weth: float = 0.0
    proceeds_weth: float = 0.0
    sells: list = field(default_factory=list)
    censored: bool = False
    censor_reason: str = ""


def build_episodes(resolved: pd.DataFrame, dust_frac: float = 0.01) -> pd.DataFrame:
    """Build closed episodes from resolved buy/sell flow. Censored actors truncated."""
    d = resolved.sort_values(["economic_actor", "token", "ts", "log_index"]).copy()
    open_ep: dict[tuple[str, str], _Open] = {}
    closed: list[dict] = []

    for r in d.itertuples(index=False):
        actor = str(getattr(r, "economic_actor", "") or "")
        if len(actor) != 42:
            continue
        token = str(r.token)
        key = (actor, token)
        side = r.side
        ts = float(r.ts)
        qty = float(r.target_qty)
        quote = float(r.quote_weth)
        censored = bool(getattr(r, "censored", False))
        reason = str(getattr(r, "censor_reason", "") or "")

        if side == "buy":
            ep = open_ep.get(key)
            if ep is None:
                ep = _Open(actor=actor, token=token, pool=str(r.pool), opened_at=ts)
                open_ep[key] = ep
            ep.qty += qty
            ep.max_qty = max(ep.max_qty, ep.qty)
            ep.cost_weth += quote
            if censored:
                ep.censored = True
                ep.censor_reason = reason or "CENSORED_TRANSFER_OUT"
            continue

        if side != "sell":
            continue
        ep = open_ep.get(key)
        if ep is None or ep.qty <= 0 or qty <= 0:
            continue
        matched = min(ep.qty, qty)
        ep.proceeds_weth += quote * matched / qty
        ep.qty -= matched
        ep.sells.append((ts, matched))
        if censored:
            ep.censored = True
            ep.censor_reason = reason or "CENSORED_TRANSFER_OUT"
        if ep.qty > max(1e-18, ep.max_qty * dust_frac):
            continue
        closed.append({
            "actor": ep.actor,
            "token": ep.token,
            "pool": ep.pool,
            "opened_at": ep.opened_at,
            "closed_at": ts,
            "cost_weth": ep.cost_weth,
            "proceeds_weth": ep.proceeds_weth,
            "pnl_weth": (float("nan") if ep.censored else ep.proceeds_weth - ep.cost_weth),
            "return_frac": (
                float("nan") if ep.censored or ep.cost_weth <= 0
                else (ep.proceeds_weth - ep.cost_weth) / ep.cost_weth
            ),
            "max_qty": ep.max_qty,
            "hold_s": ts - ep.opened_at,
            "censored": ep.censored,
            "censor_reason": ep.censor_reason,
            "retention_30m": _retention(ep, 1800),
            "retention_4h": _retention(ep, 14400),
            "retention_24h": _retention(ep, 86400),
            "retention_72h": _retention(ep, 259200),
        })
        del open_ep[key]

    # Remaining open episodes that are censored — close as censored at last seen
    for ep in open_ep.values():
        if ep.censored:
            closed.append({
                "actor": ep.actor,
                "token": ep.token,
                "pool": ep.pool,
                "opened_at": ep.opened_at,
                "closed_at": ep.opened_at,
                "cost_weth": ep.cost_weth,
                "proceeds_weth": 0.0,
                "pnl_weth": float("nan"),
                "return_frac": float("nan"),
                "max_qty": ep.max_qty,
                "hold_s": 0.0,
                "censored": True,
                "censor_reason": ep.censor_reason or "CENSORED_TRANSFER_OUT",
                "retention_30m": _retention(ep, 1800),
                "retention_4h": _retention(ep, 14400),
                "retention_24h": _retention(ep, 86400),
                "retention_72h": _retention(ep, 259200),
            })

    return pd.DataFrame(closed)


def _retention(ep: _Open, hold_s: float) -> float:
    if ep.max_qty <= 0:
        return 0.0
    t_cut = ep.opened_at + hold_s
    sold = sum(q for t, q in ep.sells if t <= t_cut)
    return float(min(1.0, max(0.0, ep.max_qty - sold) / ep.max_qty))
