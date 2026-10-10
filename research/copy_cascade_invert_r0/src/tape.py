"""Load copied Base tapes. No RPC."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.common import ROOT, load_exclusions, norm_addr, parse_iso, safe_float

OFFLINE = ROOT / "data/raw/base_offline"
WETH = "0x4200000000000000000000000000000000000006"


def load_closed_trades() -> list[dict]:
    path = OFFLINE / "closed_trades_sample.csv"
    if not path.exists():
        return []
    excluded = load_exclusions()
    by_key: dict[tuple, dict] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            wallet = norm_addr(row.get("wallet"))
            token = norm_addr(row.get("token"))
            pool = norm_addr(row.get("pool"))
            if not wallet or wallet in excluded or token == WETH:
                continue
            key = (wallet, row.get("entry_tx_hash"), row.get("exit_tx_hash"), token)
            cost = safe_float(row.get("cost_weth")) or 0.0
            prev = by_key.get(key)
            if prev and abs(prev.get("_cost") or 0) >= abs(cost):
                continue
            pnl = safe_float(row.get("pnl_weth"))
            # sprint083 stores WETH legs in wei-scale floats
            if pnl is not None and abs(pnl) > 1e6:
                pnl = pnl / 1e18
            if cost and abs(cost) > 1e6:
                cost = cost / 1e18
            proceeds = safe_float(row.get("proceeds_weth"))
            if proceeds is not None and abs(proceeds) > 1e6:
                proceeds = proceeds / 1e18
            by_key[key] = {
                "wallet": wallet,
                "token": token,
                "pool": pool,
                "entry_time": parse_iso(row.get("entry_time")),
                "exit_time": parse_iso(row.get("exit_time")),
                "entry_tx": row.get("entry_tx_hash"),
                "exit_tx": row.get("exit_tx_hash"),
                "cost_weth": cost,
                "proceeds_weth": proceeds,
                "pnl_weth": pnl,
                "roi": safe_float(row.get("roi")),
                "_cost": cost,
            }
    return list(by_key.values())


def wallet_fifo(closed: list[dict] | None = None) -> dict[str, dict]:
    closed = closed if closed is not None else load_closed_trades()
    agg: dict[str, dict] = {}
    for tr in closed:
        w = tr["wallet"]
        a = agg.setdefault(
            w,
            {
                "wallet": w,
                "n_closed": 0,
                "pnl_weth": 0.0,
                "wins": 0,
                "tokens": set(),
                "pnls": [],
            },
        )
        pnl = tr.get("pnl_weth") or 0.0
        a["n_closed"] += 1
        a["pnl_weth"] += pnl
        a["pnls"].append(pnl)
        if pnl > 0:
            a["wins"] += 1
        if tr.get("token"):
            a["tokens"].add(tr["token"])
    out = {}
    for w, a in agg.items():
        pnls = a["pnls"]
        top = max(pnls) if pnls else 0.0
        pos = sum(x for x in pnls if x > 0)
        out[w] = {
            "wallet": w,
            "n_closed": a["n_closed"],
            "pnl_weth": a["pnl_weth"],
            "winrate": a["wins"] / a["n_closed"] if a["n_closed"] else None,
            "n_tokens": len(a["tokens"]),
            "top1_pnl_share": (top / pos) if pos > 0 else None,
            "sign": 1 if a["pnl_weth"] > 0 else (-1 if a["pnl_weth"] < 0 else 0),
        }
    return out


def load_entries() -> list[dict]:
    path = OFFLINE / "copyability_safe_dataset_sample.csv"
    if not path.exists():
        path = OFFLINE / "dataset_sample.csv"
    if not path.exists():
        return []
    excluded = load_exclusions()
    rows = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            wallet = norm_addr(row.get("wallet"))
            if not wallet or wallet in excluded or row.get("router_flag") in ("True", "true", "1"):
                continue
            rows.append(
                {
                    "wallet": wallet,
                    "token": norm_addr(row.get("token")),
                    "pool": norm_addr(row.get("pool")),
                    "tx_hash": row.get("tx_hash"),
                    "block_number": int(float(row["block_number"])) if row.get("block_number") else None,
                    "entry_time": parse_iso(row.get("entry_time")),
                    "amount_weth": safe_float(row.get("amount_weth")),
                    "wallet_age_days": safe_float(row.get("wallet_age_days")),
                    "copy_delay_30s": safe_float(row.get("copy_delay_30s")),
                    "copy_delay_60s": safe_float(row.get("copy_delay_60s")),
                    "copy_delay_120s": safe_float(row.get("copy_delay_120s")),
                    "future_return_5m": safe_float(row.get("future_return_5m")),
                    "future_return_30m": safe_float(row.get("future_return_30m")),
                    "realized_wallet_pnl_weth": safe_float(row.get("realized_wallet_pnl_weth")),
                }
            )
    rows.sort(key=lambda r: r["entry_time"] or datetime(1970, 1, 1, tzinfo=timezone.utc))
    return rows


def load_b5_for_pools(pools: set[str]) -> dict[str, list[tuple[int, int, int]]]:
    """pool -> list of (ts_ms, block, tick)."""
    path = OFFLINE / "base_b5_real_swaps_20260714.tsv"
    if not path.exists() or not pools:
        return {}
    want = {p.lower() for p in pools}
    out: dict[str, list] = defaultdict(list)
    with path.open() as fh:
        for line in fh:
            z = line.rstrip("\n").split("\t")
            if len(z) < 8:
                continue
            pool = z[0].lower()
            if pool not in want:
                continue
            try:
                ts_ms = int(z[2])
                block = int(z[1])
                tick = int(z[7])
            except ValueError:
                continue
            out[pool].append((ts_ms, block, tick))
    for pool in out:
        out[pool].sort()
    return out


def next_tick_return(series: list[tuple[int, int, int]], t0: datetime, delay_sec: float) -> float | None:
    if not series or t0 is None:
        return None
    t0_ms = int(t0.timestamp() * 1000)
    target = t0_ms + int(delay_sec * 1000)
    # find last tick at/before t0 and first at/after target
    pre = None
    post = None
    for ts, _block, tick in series:
        if ts <= t0_ms:
            pre = tick
        if ts >= target:
            post = tick
            break
    if pre is None or post is None:
        return None
    return (1.0001 ** (post - pre)) - 1.0
