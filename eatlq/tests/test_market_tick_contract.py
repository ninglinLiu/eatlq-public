"""Sprint006: market_ticks stream field contract (no Redis / Docker / RPC)."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_SRC = ROOT / "services/strategy/src"
CONTRACT_DOC = ROOT / "docs/market-ticks-stream-contract.md"


def _stub_web3_xgboost() -> None:
    if "web3" not in sys.modules:
        w3 = types.ModuleType("web3")

        class Web3:
            class HTTPProvider:
                def __init__(self, *args, **kwargs):
                    pass

            def __init__(self, *args, **kwargs):
                pass

            def is_connected(self):
                return False

        w3.Web3 = Web3
        sys.modules["web3"] = w3

    if "xgboost" not in sys.modules:
        xgb = types.ModuleType("xgboost")
        xgb.XGBClassifier = type("XGBClassifier", (), {})
        sys.modules["xgboost"] = xgb


def _ensure_msgpack_stub() -> None:
    """Real CI ``python3`` may not have msgpack; strategy imports require a stub at load time."""
    if "msgpack" not in sys.modules:
        mp = types.ModuleType("msgpack")
        mp.unpackb = lambda *args, **kwargs: {}  # overridden in tests as needed
        sys.modules["msgpack"] = mp


def load_strategy_main():
    _stub_web3_xgboost()
    _ensure_msgpack_stub()
    if str(STRATEGY_SRC) not in sys.path:
        sys.path.insert(0, str(STRATEGY_SRC))
    spec = importlib.util.spec_from_file_location(
        "strategy_main_market_tick_contract_test",
        STRATEGY_SRC / "main.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load strategy main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MarketTickStreamContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_strategy_main()

    def test_payload_field_order_constant_matches_doc(self):
        self.assertEqual(
            self.main._MARKET_TICK_PAYLOAD_FIELD_ORDER,
            ("data", "payload_msgpack", "payload", "tick"),
        )

    def test_data_wins_over_payload_msgpack(self):
        p_data = b"AAA-first"
        p_legacy = b"BBB-second"
        got = self.main.parse_stream_payload(
            {b"data": p_data, b"payload_msgpack": p_legacy}
        )
        self.assertEqual(got, p_data)

    def test_str_keys_supported(self):
        got = self.main.parse_stream_payload({"data": b"str-key-payload"})
        self.assertEqual(got, b"str-key-payload")

    def test_empty_data_falls_through_to_payload_msgpack(self):
        got = self.main.parse_stream_payload(
            {b"data": b"", b"payload_msgpack": b"fallback-bytes"}
        )
        self.assertEqual(got, b"fallback-bytes")

    def test_memoryview_on_data_returns_bytes(self):
        got = self.main.parse_stream_payload(
            {b"data": memoryview(b"mv-payload")}
        )
        self.assertEqual(got, b"mv-payload")

    def test_missing_all_fields_returns_none(self):
        self.assertIsNone(self.main.parse_stream_payload({b"tx_from": b"0xabc"}))
        self.assertIsNone(self.main.parse_stream_payload({}))
        self.assertIsNone(self.main.parse_stream_payload("not-a-dict"))

    def test_malformed_msgpack_raises_in_normalize(self):
        mp = sys.modules["msgpack"]

        def boom(_data, raw=False):
            raise ValueError("bad msgpack")

        old = mp.unpackb
        mp.unpackb = boom
        try:
            with self.assertRaises(ValueError):
                self.main.FeatureStore._normalize_msgpack_payload(b"\xffnotmsgpack")
        finally:
            mp.unpackb = old

    def test_main_loop_ack_batch_comment_present(self):
        src = (STRATEGY_SRC / "main.py").read_text(encoding="utf-8")
        self.assertIn("Missing blob: skip processing", src)
        self.assertIn("entry still XACK'd", src)

    def test_contract_doc_lists_priority_and_xack(self):
        body = CONTRACT_DOC.read_text(encoding="utf-8")
        for phrase in (
            "data",
            "payload_msgpack",
            "XACK",
            "parse_stream_payload",
            "_MARKET_TICK_PAYLOAD_FIELD_ORDER",
            "market-ticks-stream-contract.md",
        ):
            self.assertIn(phrase, body)

    def test_ingestor_comment_points_at_contract_doc(self):
        rs = (ROOT / "services/ingestor/src/redis_streams.rs").read_text(encoding="utf-8")
        self.assertIn("docs/market-ticks-stream-contract.md", rs)


if __name__ == "__main__":
    unittest.main()
