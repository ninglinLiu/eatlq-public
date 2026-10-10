"""Lookahead-free assertions for LARGE_BUY_ABSORPTION_v1 timing."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path("/root/base_large_buy_absorption_v1")
sys.path.insert(0, str(ROOT))

from src.signals import assert_lookahead_free, attach_signal_times  # noqa: E402


def test_absorption_arm_waits_300s():
    buys = pd.DataFrame({
        "pool": ["0xabc"] * 3,
        "token": ["0xt"] * 3,
        "whale_buy_t": [1_000_000, 1_000_100, 1_000_200],
        "ts": [1_000_000, 1_000_100, 1_000_200],
        "economic_actor": ["0x" + "11" * 20] * 3,
        "quote_weth": [0.1, 0.2, 0.3],
        "price_weth": [1e-10, 1e-10, 1e-10],
        "liquidity": [1e12, 1e12, 1e12],
        "pool_age_s": [2000, 2100, 2200],
        "absorbed": [1.0, 0.0, 1.0],
        "rel_size": [1.0, 2.0, 0.5],
        "lba_score": [1.0, 0.0, 0.5],
        "large_buy_score": [1.0, 2.0, 0.5],
        "absorption_score": [1.0, 0.0, 1.0],
        "pool_only_score": [0.5, 0.5, 0.5],
    })
    wait = attach_signal_times(buys, "LARGE_BUY_ABSORPTION")
    assert (wait["signal_t"] == wait["whale_buy_t"] + 300).all()
    assert (wait["decision_time"] == wait["signal_t"]).all()
    assert (wait["entry_t"] == wait["signal_t"] + 30).all()
    assert (wait["decision_time"] != wait["whale_buy_t"]).all()
    res = assert_lookahead_free(wait)
    assert res["pass"], res

    imm = attach_signal_times(buys, "LARGE_BUY_ONLY")
    assert (imm["signal_t"] == imm["whale_buy_t"]).all()
    assert (imm["entry_t"] == imm["whale_buy_t"] + 30).all()
    res2 = assert_lookahead_free(imm)
    assert res2["pass"], res2


if __name__ == "__main__":
    test_absorption_arm_waits_300s()
    print("OK")
