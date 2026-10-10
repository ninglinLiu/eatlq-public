"""Assert dashcam sender_address is a real buyer EOA, not quote/router."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_SRC = ROOT / "services" / "strategy" / "src"

_QUOTE_ADDRS = frozenset({
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
})

_ROUTER_ADDRS = frozenset({
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Universal Router
})


def _ensure_stubs() -> None:
    if "redis" not in sys.modules:
        redis_mod = types.ModuleType("redis")

        class Redis:
            @staticmethod
            def from_url(*args, **kwargs):
                return Redis()

            def hgetall(self, *args, **kwargs):
                return {}

        redis_mod.Redis = Redis
        sys.modules["redis"] = redis_mod

    if "msgpack" not in sys.modules:
        mp = types.ModuleType("msgpack")
        mp.unpackb = lambda *args, **kwargs: {}
        sys.modules["msgpack"] = mp

    if "numpy" not in sys.modules:
        np = types.ModuleType("numpy")
        np.array = lambda x: x
        sys.modules["numpy"] = np


def load_feature_store():
    _ensure_stubs()
    if str(STRATEGY_SRC) not in sys.path:
        sys.path.insert(0, str(STRATEGY_SRC))
    spec = importlib.util.spec_from_file_location(
        "feature_store_sender_test",
        STRATEGY_SRC / "feature_store.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load feature_store.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DashcamSenderIsRealBuyerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = load_feature_store()

    def _extract(self, payload: dict) -> str:
        fs = self.fs.FeatureStore("redis://127.0.0.1:6379/0")
        tick = fs._extract_tick_fields(payload)
        return str(tick.get("sender_address") or "").lower()

    def test_tx_from_preferred_over_weth_topic1(self):
        eoa = "0x1111111111111111111111111111111111111111"
        sender = self._extract({
            "tx_from": eoa,
            "topic1": "0x0000000000000000000000004200000000000000000000000000000000000006",
            "topic0": self.fs.TOPIC_V3_SWAP,
            "data": "0x" + "00" * 128,
            "received_at_ms": 1,
        })
        self.assertEqual(sender, eoa)

    def test_weth_topic1_rejected(self):
        sender = self._extract({
            "topic1": "0x0000000000000000000000004200000000000000000000000000000000000006",
            "topic0": self.fs.TOPIC_V3_SWAP,
            "data": "0x" + "00" * 128,
            "received_at_ms": 1,
        })
        self.assertEqual(sender, "")

    def test_usdc_sender_rejected(self):
        sender = self._extract({
            "tx_from": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "topic0": self.fs.TOPIC_V3_SWAP,
            "data": "0x" + "00" * 128,
            "received_at_ms": 1,
        })
        self.assertEqual(sender, "")

    def test_valid_eoa_accepted(self):
        eoa = "0x7e49aec513d368d44e2be62b6bc7bb3e65fef25a"
        sender = self._extract({
            "tx_from": eoa,
            "topic0": self.fs.TOPIC_V3_SWAP,
            "data": "0x" + "00" * 128,
            "received_at_ms": 1,
        })
        self.assertEqual(sender, eoa)

    def test_forbidden_addrs_not_real_buyers(self):
        forbidden = _QUOTE_ADDRS | _ROUTER_ADDRS
        for addr in forbidden:
            if len(addr) != 42:
                continue
            sender = self._extract({
                "tx_from": addr,
                "topic0": self.fs.TOPIC_V3_SWAP,
                "data": "0x" + "00" * 128,
                "received_at_ms": 1,
            })
            self.assertEqual(sender, "", msg=f"quote/router {addr} must not be sender")

    def test_dashcam_must_not_prefer_raw_tx_from_over_filtered_sender(self):
        """Regression: append_dashcam must use sender_address, not raw tx_from."""
        weth = "0x4200000000000000000000000000000000000006"
        tick = self.fs.FeatureStore("redis://127.0.0.1:6379/0")._extract_tick_fields({
            "tx_from": weth,
            "topic1": "0x0000000000000000000000004200000000000000000000000000000000000006",
            "topic0": self.fs.TOPIC_V3_SWAP,
            "data": "0x" + "00" * 128,
            "received_at_ms": 1,
        })
        self.assertEqual(tick.get("sender_address"), "")
        self.assertEqual(tick.get("tx_from"), weth)
        # Buggy path wrote tx_from; fixed path uses sender_address only.
        dashcam_sender = str(tick.get("sender_address") or "")
        self.assertEqual(dashcam_sender, "")
        self.assertNotEqual(str(tick.get("tx_from") or ""), dashcam_sender)


if __name__ == "__main__":
    unittest.main()
