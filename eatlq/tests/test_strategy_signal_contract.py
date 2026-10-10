"""Sprint005: strategy → signals stream payload shape (static, no Redis)."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_MAIN = ROOT / "services/strategy/src/main.py"


class StrategySignalContractTests(unittest.TestCase):
    def test_smart_money_buy_payload_includes_pool_and_token_alias(self):
        src = STRATEGY_MAIN.read_text(encoding="utf-8")
        idx = src.index('"signal_source": "smart_money"')
        window = src[max(0, idx - 1600) : idx + 120]
        self.assertIn('"action": "BUY"', window)
        self.assertIn('"pool_address": token_address', window)
        self.assertIn('"token_address": token_address', window)
        self.assertIn("consumer.xadd(cfg.signal_stream", src)

    def test_pool_score_path_does_not_xadd_signals_stream(self):
        """Executable pool_score remains shadow-only (static contract)."""
        src = STRATEGY_MAIN.read_text(encoding="utf-8")
        start = src.index("# --- Pool-Level Score Path (rejection-gated) ---")
        slice_ = src[start : start + 9000]
        self.assertNotIn("cfg.signal_stream", slice_)
        self.assertIn("_write_shadow_signal", slice_)


if __name__ == "__main__":
    unittest.main()
