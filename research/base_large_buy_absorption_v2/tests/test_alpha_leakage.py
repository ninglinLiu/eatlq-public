"""Unit tests for Alpha Hunt v1 leakage discipline."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.alpha.pit_actor_features import build_closed_round_trips, _expanding_actor_stats, _asof_actor


def test_asof_excludes_close_at_or_after_T():
    trips = pd.DataFrame({
        "actor": ["a", "a", "a"],
        "token": ["t", "t", "t"],
        "pool": ["p", "p", "p"],
        "cluster": ["a", "a", "a"],
        "entry_ts": [1, 10, 20],
        "close_ts": [5, 15, 25],
        "hold_s": [4, 5, 5],
        "ret": [0.1, -0.1, 0.2],
        "pool_age_at_entry": [100, 200, 300],
        "win": [1, 0, 1],
    })
    stats = _expanding_actor_stats(trips)
    signals = pd.DataFrame({
        "_row": [0, 1],
        "economic_actor": ["a", "a"],
        "decision_time": [15, 16],  # at 15: only close_ts=5 allowed (exact 15 excluded)
    })
    m = _asof_actor(signals, stats)
    # decision_time=15 → allow_exact_matches=False → close_ts < 15 → only trip close=5
    row0 = m[m["_row"] == 0].iloc[0]
    assert int(row0["n_trips"]) == 1
    # decision_time=16 → close_ts=15 allowed
    row1 = m[m["_row"] == 1].iloc[0]
    assert int(row1["n_trips"]) == 2


def test_expanding_stats_monotonic_n():
    trips = pd.DataFrame({
        "actor": ["a"] * 5,
        "token": ["t"] * 5,
        "pool": ["p"] * 5,
        "cluster": ["a"] * 5,
        "entry_ts": range(5),
        "close_ts": range(10, 15),
        "hold_s": [1] * 5,
        "ret": [0.1, -0.2, 0.05, 0.0, 0.3],
        "pool_age_at_entry": [10] * 5,
        "win": [1, 0, 1, 0, 1],
    })
    stats = _expanding_actor_stats(trips)
    assert list(stats["n_trips"]) == [1, 2, 3, 4, 5]
    assert abs(stats["win_rate"].iloc[-1] - 0.6) < 1e-9


def test_forbidden_lifetime_tables_not_imported():
    """Guard: alpha modules must not import lifetime aggregate CH tables as features."""
    import src.alpha.pit_actor_features as a
    import src.alpha.pit_genesis_features as b
    import inspect
    src_a = inspect.getsource(a)
    src_b = inspect.getsource(b)
    for bad in ["sender_pnl_summary_v1", "recipient_pnl_summary_v1", "sender_feature_store_v1"]:
        assert bad not in src_a
        assert bad not in src_b
