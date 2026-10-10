from strategies.F01_OFS_SIGN_RUNLEN.runner import run_scaffold, signal_fn, entry_fn
from orchestration.common import Bar


def test_run_scaffold_empty():
    out = run_scaffold([])
    assert out["strategy_id"] == "F01_OFS_SIGN_RUNLEN"
    assert out["n_signals"] == 0
    assert out["backtest_enabled"] is False


def test_synthetic_trigger():
    bars = [
        Bar(
            pool_id="0xpool",
            event_time=100,
            known_at=100,
            feature_available_at=100,
            features={"trigger": 1.0, "strength": 0.8},
        )
    ]
    sigs = signal_fn(bars, {})
    assert len(sigs) == 1
    intent = entry_fn(sigs[0], {"notional_usd": 50, "execution_delay_bars": 1})
    assert intent is not None
    assert intent.side == "BUY"
