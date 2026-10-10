"""Lookahead-free assertions for v2 primary timing (5s / 0s)."""
import pandas as pd

from src.signals import assert_lookahead_free, attach_signal_times


def test_absorption_wait_and_zero_latency():
    buys = pd.DataFrame({
        "pool": ["0xabc"] * 3,
        "whale_buy_t": [1_000_000, 1_000_100, 1_000_200],
        "economic_actor": ["0x1", "0x2", "0x3"],
        "absorbed_5s": [1.0, 0.0, 1.0],
        "large_buy_score": [1.0, 1.0, 1.0],
    })
    panel = attach_signal_times(buys, "LARGE_BUY_ABSORPTION")
    assert (panel["signal_t"] == panel["whale_buy_t"] + 5).all()
    assert (panel["entry_t"] == panel["signal_t"]).all()  # 0s latency
    assert (panel["decision_time"] == panel["signal_t"]).all()
    res = assert_lookahead_free(panel)
    assert res["pass"], res


def test_immediate_arm_no_wait():
    buys = pd.DataFrame({
        "pool": ["0xabc"],
        "whale_buy_t": [1_000_000],
        "economic_actor": ["0x1"],
    })
    panel = attach_signal_times(buys, "LARGE_BUY_ONLY")
    assert int(panel["signal_t"].iloc[0]) == 1_000_000
    res = assert_lookahead_free(panel)
    assert res["pass"], res
