"""Sprint005: executor signal ingress contract (no Redis / RPC)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services/executor/src"
CONTRACT_DOC = ROOT / "docs/executor-language-contract.md"


def _stub_executor_import_deps() -> None:
    """Allow loading executor main in CI images without web3/eth_* wheels."""
    import types

    if "eth_account" not in sys.modules:
        eth_m = types.ModuleType("eth_account")

        class _Account:
            pass

        eth_m.Account = _Account
        sys.modules["eth_account"] = eth_m

    if "eth_abi" not in sys.modules:
        abi_m = types.ModuleType("eth_abi")
        abi_m.encode = lambda *args, **kwargs: b""
        sys.modules["eth_abi"] = abi_m

    if "web3" not in sys.modules:
        web3_m = types.ModuleType("web3")

        class Web3:
            @staticmethod
            def HTTPProvider(*_a, **_k):
                return None

            def __init__(self, *_a, **_k):
                pass

            def is_connected(self):
                return False

        web3_m.Web3 = Web3
        sys.modules["web3"] = web3_m

    if "web3.types" not in sys.modules:
        wt = types.ModuleType("web3.types")
        wt.TxReceipt = dict
        sys.modules["web3.types"] = wt


def load_executor_main():
    _stub_executor_import_deps()
    if str(EXECUTOR_SRC) not in sys.path:
        sys.path.insert(0, str(EXECUTOR_SRC))
    spec = importlib.util.spec_from_file_location(
        "executor_main_signal_contract_test",
        EXECUTOR_SRC / "main.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load executor main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExecutorSignalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_executor_main()

    def test_malformed_buy_missing_pool(self):
        self.assertEqual(
            self.m._malformed_buy_signal_reason({"action": "BUY", "confidence": 0.9}),
            "buy_missing_pool_or_token",
        )

    def test_malformed_buy_empty_pool_fields(self):
        self.assertEqual(
            self.m._malformed_buy_signal_reason(
                {"action": "BUY", "pool_address": "  ", "token_address": ""}
            ),
            "buy_missing_pool_or_token",
        )

    def test_malformed_buy_invalid_address(self):
        self.assertEqual(
            self.m._malformed_buy_signal_reason(
                {
                    "action": "BUY",
                    "pool_address": "not-an-address",
                }
            ),
            "buy_invalid_pool_or_token_address",
        )
        self.assertEqual(
            self.m._malformed_buy_signal_reason(
                {
                    "action": "BUY",
                    "pool_address": "0x1234",
                }
            ),
            "buy_invalid_pool_or_token_address",
        )

    def test_valid_buy_passes_malformed_gate(self):
        pool = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
        self.assertIsNone(
            self.m._malformed_buy_signal_reason(
                {
                    "action": "BUY",
                    "pool_address": pool,
                    "confidence": 0.75,
                    "signal_source": "smart_money",
                }
            )
        )

    def test_token_address_fallback_for_pool(self):
        pool = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
        self.assertIsNone(
            self.m._malformed_buy_signal_reason(
                {
                    "action": "BUY",
                    "token_address": pool,
                }
            )
        )

    def test_non_buy_not_malformed(self):
        self.assertIsNone(
            self.m._malformed_buy_signal_reason({"action": "SELL", "pool_address": "0x" + "0" * 40})
        )

    def test_action_case_sensitive_buy_only_uppercase(self):
        """Lowercase ``buy`` is not treated as malformed; executor skips in ``_handle_signal``."""
        pool = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
        self.assertIsNone(self.m._malformed_buy_signal_reason({"action": "buy", "pool_address": pool}))

    def test_parse_signal_accepts_payload_and_signal_json(self):
        payload = {"action": "BUY", "pool_address": "0x" + "ab" * 20}
        raw_b = {b"payload": json.dumps(payload).encode()}
        raw_s = {"payload": json.dumps(payload)}
        raw_legacy = {b"signal_json": json.dumps(payload).encode()}
        self.assertEqual(self.m._parse_signal(raw_b), payload)
        self.assertEqual(self.m._parse_signal(raw_s), payload)
        self.assertEqual(self.m._parse_signal(raw_legacy), payload)

    def test_executor_contract_doc_lists_schema_and_dead_letter_codes(self):
        body = CONTRACT_DOC.read_text(encoding="utf-8")
        for phrase in (
            "## Signals stream — JSON payload contract",
            "signals_dead_letter",
            "buy_missing_pool_or_token",
            "buy_invalid_pool_or_token_address",
            "parse_failed",
            "action",
            "Must be exactly **`BUY`**",
            "pool_address",
            "token_address",
        ):
            self.assertIn(phrase, body)

    def test_handle_signal_skips_non_buy_unchanged(self):
        main_src = (EXECUTOR_SRC / "main.py").read_text(encoding="utf-8")
        self.assertIn("skip non-BUY signal", main_src)
        h = main_src.index("def _handle_signal")
        self.assertIn(
            'if sig.get("action") != "BUY":',
            main_src[h : h + 800],
        )


if __name__ == "__main__":
    unittest.main()
