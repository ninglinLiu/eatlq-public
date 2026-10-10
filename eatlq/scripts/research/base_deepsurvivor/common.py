"""Shared utilities for Base deep-survivor offline research."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Optional

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/EATLQ/scripts/lib")
sys.path.insert(0, os.path.join(HERE, "..", "base_v4"))

from research_rpc import ResearchRpc, RpcBudgetExceeded, RPC_COUNTER_PATH  # noqa: E402
from v3_quoterv2_struct import (  # noqa: E402
    WETH_BASE,
    quote_exact_input_single_struct,
)

# --- constants (shallow filters only; no post-hoc gates) ---
WETH = WETH_BASE.lower()
BLOCK_SEC = 2.0
MIN_AGE_DAYS = 7
MIN_DEPTH_ETH = 3.0
MIN_SWAPS_PRE = 50  # optional activity floor (pre-entry rolling 7d)
NOTIONAL_USD = 100.0
ETH_USD = 3000.0
AMT_IN = int(NOTIONAL_USD / ETH_USD * 1e18)
DATE_LO = "2025-10-01"
DATE_HI = "2026-06-25"
TRAIN_Q = 0.60
FEE_SELECTOR = "0xddca3f43"
WETH_BAL_SELECTOR = "0x70a08231"

# Hold windows (blocks, Base ~2s)
HOLD_BLOCKS = {
    "1h": 1800,
    "4h": 7200,
    "24h": 43200,
    "72h": 129600,
}
DELAY_BLOCKS = [0, 1, 5, 30, 300]

# RPC budget (this round)
SESSION_COUNTER = os.path.join(HERE, "_session_rpc.json")
ROUND_RPC_BUDGET = 1500
CUMULATIVE_STOP = 18000
QN_CREDITS_PER_CALL = 20

CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]


def ch_query(sql: str, timeout: int = 600) -> str:
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:2000] or r.stdout[:2000])
    return r.stdout


class BudgetRpc:
    """ResearchRpc wrapper with per-round session budget + cumulative stop."""

    def __init__(self):
        self.rpc = ResearchRpc()
        self._session = self._load_session()
        self._fee_cache: dict[tuple[str, int], Optional[int]] = {}

    def _load_session(self) -> dict:
        if os.path.exists(SESSION_COUNTER):
            try:
                return json.load(open(SESSION_COUNTER))
            except Exception:
                pass
        global_n = 0
        if os.path.exists(RPC_COUNTER_PATH):
            try:
                global_n = json.load(open(RPC_COUNTER_PATH)).get("eth_calls", 0)
            except Exception:
                pass
        return {"session_start_global": global_n, "session_added": 0, "stopped": False}

    def _save_session(self) -> None:
        json.dump(self._session, open(SESSION_COUNTER, "w"))

    def session_added(self) -> int:
        return self._session.get("session_added", 0)

    def global_count(self) -> int:
        return self.rpc.count()

    def credits_m(self) -> float:
        return self.global_count() * QN_CREDITS_PER_CALL / 1e6

    def budget_remaining(self) -> int:
        return max(0, ROUND_RPC_BUDGET - self.session_added())

    def call(self, method: str, params: list[Any], timeout: int = 90) -> Any:
        if self._session.get("stopped"):
            raise RpcBudgetExceeded("Session RPC budget exhausted")
        if self.global_count() >= CUMULATIVE_STOP:
            self._session["stopped"] = True
            self._save_session()
            raise RpcBudgetExceeded(f"Cumulative RPC limit {CUMULATIVE_STOP} reached")
        if self.session_added() >= ROUND_RPC_BUDGET:
            self._session["stopped"] = True
            self._save_session()
            raise RpcBudgetExceeded(f"Round budget {ROUND_RPC_BUDGET} reached")
        result = self.rpc.call(method, params)
        self._session["session_added"] = self.session_added() + 1
        if self.global_count() >= CUMULATIVE_STOP or self.session_added() >= ROUND_RPC_BUDGET:
            self._session["stopped"] = True
        self._save_session()
        return result

    def eth_call(self, to: str, data: str, block: int) -> Optional[str]:
        try:
            return self.call("eth_call", [{"to": to, "data": data}, hex(int(block))])
        except RpcBudgetExceeded:
            raise
        except Exception:
            return None

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def pool_fee(self, pool: str, block: int) -> Optional[int]:
        key = (pool.lower(), block)
        if key in self._fee_cache:
            return self._fee_cache[key]
        res = self.eth_call(pool, FEE_SELECTOR, block)
        fee = int(res, 16) if res and res != "0x" else None
        self._fee_cache[key] = fee
        return fee

    def weth_depth_eth(self, pool: str, block: int) -> Optional[float]:
        data = WETH_BAL_SELECTOR + "0" * 24 + pool[2:].lower()
        res = self.eth_call(WETH, data, block)
        if res is None:
            return None
        return int(res, 16) / 1e18

    def quote(self, tin: str, tout: str, amt: int, fee: int, block: int) -> Optional[int]:
        q = quote_exact_input_single_struct(self, tin, tout, amt, fee, block_identifier=block)
        return q["amount_out"] if q["ok"] else None

    def roundtrip(
        self, pool: str, alt: str, fee: int, entry_block: int, exit_block: int
    ) -> dict:
        """Real-cost roundtrip: buy WETH->alt @ entry, sell alt->WETH @ exit."""
        buy = self.quote(WETH_BASE, alt, AMT_IN, fee, entry_block)
        if buy is None or buy <= 0:
            return {"net": None, "fric": None, "mid": None, "why": "entry_fail"}
        fric_out = self.quote(alt, WETH_BASE, buy, fee, entry_block)
        fric = -1.0 if fric_out is None else fric_out / AMT_IN - 1.0
        sell = self.quote(alt, WETH_BASE, buy, fee, exit_block)
        net = -1.0 if sell is None else sell / AMT_IN - 1.0
        return {"net": net, "fric": fric, "mid": None, "why": "ok" if sell else "sell_fail"}


def load_json(path: str, default: Any = None) -> Any:
    if os.path.exists(path):
        return json.load(open(path))
    return default if default is not None else {}


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


def time_split(rows: list[dict], ts_key: str = "entry_ts") -> tuple[list, list, float]:
    ts_vals = sorted(r[ts_key] for r in rows if r.get(ts_key))
    if not ts_vals:
        return [], [], 0.0
    cut_ts = ts_vals[int(len(ts_vals) * TRAIN_Q)]
    train = [r for r in rows if r.get(ts_key, 0) <= cut_ts]
    test = [r for r in rows if r.get(ts_key, 0) > cut_ts]
    return train, test, cut_ts


def pnl_stats(rets: list[float]) -> dict:
    r = np.array([x for x in rets if x is not None and x == x])
    if len(r) == 0:
        return {"n": 0}
    return {
        "n": int(len(r)),
        "mean": float(r.mean()),
        "median": float(np.median(r)),
        "wr": float((r > 0).mean()),
        "fatal_50": float((r <= -0.5).mean()),
        "fatal_90": float((r <= -0.9).mean()),
        "right_50": float((r >= 0.5).mean()),
        "right_100": float((r >= 1.0).mean()),
        "best": float(r.max()),
        "worst": float(r.min()),
        "p90": float(np.quantile(r, 0.9)),
    }


def beat_baseline(signal: dict, baseline: dict, min_n: int = 30) -> Optional[bool]:
    if signal.get("n", 0) < min_n or baseline.get("n", 0) < min_n:
        return None
    return signal["mean"] > baseline["mean"] and signal["wr"] > baseline["wr"]


def rpc_ledger() -> dict:
    sess = load_json(SESSION_COUNTER, {"session_added": 0, "session_start_global": 0})
    global_n = 0
    if os.path.exists(RPC_COUNTER_PATH):
        global_n = json.load(open(RPC_COUNTER_PATH)).get("eth_calls", 0)
    return {
        "round_added": sess.get("session_added", 0),
        "round_budget": ROUND_RPC_BUDGET,
        "cumulative": global_n,
        "cumulative_stop": CUMULATIVE_STOP,
        "credits_m": round(global_n * QN_CREDITS_PER_CALL / 1e6, 3),
        "credits_round_m": round(sess.get("session_added", 0) * QN_CREDITS_PER_CALL / 1e6, 3),
    }


def ts_fmt(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
