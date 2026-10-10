"""Sprint007: observability / archiver / dashboard / dead-letter static alignment."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVER_RS = ROOT / "services/archiver/src/redis_streams.rs"
DASHBOARD_PY = ROOT / "services/dashboard/server.py"
EXECUTOR_MAIN = ROOT / "services/executor/src/main.py"
OBSERVABILITY_DOC = ROOT / "docs/observability-stream-contract.md"


def _load_executor_main_module():
    """Reuse Sprint005 stubs so executor main loads without full web3/redis wheels."""
    sibling = Path(__file__).resolve().parent / "test_executor_signal_contract.py"
    spec = importlib.util.spec_from_file_location("_esc_sprint007_helper", sibling)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load test_executor_signal_contract.py")
    esc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(esc)
    return esc.load_executor_main()


class ObservabilityContractTests(unittest.TestCase):
    def test_archiver_decode_matches_four_field_strategy_order(self) -> None:
        body = ARCHIVER_RS.read_text(encoding="utf-8")
        self.assertIn('.get("data")', body)
        self.assertIn('.get("payload_msgpack")', body)
        self.assertIn('.get("payload")', body)
        self.assertIn('.get("tick")', body)
        self.assertIn("docs/market-ticks-stream-contract.md", body)

    def test_dashboard_msgpack_fields_data_first(self) -> None:
        text = DASHBOARD_PY.read_text(encoding="utf-8")
        start = text.index("_MSGPACK_PAYLOAD_FIELDS = (")
        snippet = text[start : start + 120]
        self.assertIn("b\"data\"", snippet)
        self.assertLess(snippet.index("b\"data\""), snippet.index("b\"payload_msgpack\""))

    def test_observability_doc_cross_links_contracts(self) -> None:
        doc = OBSERVABILITY_DOC.read_text(encoding="utf-8")
        for phrase in (
            "market-ticks-stream-contract.md",
            "executor-language-contract.md",
            "signals_dead_letter_ops_summary",
            "XINFO GROUPS",
            "SYSTEM_PANIC",
            "IMPLEMENTATION_ALLOWED=false",
        ):
            self.assertIn(phrase, doc)

    def test_operations_mentions_stream_lag_and_ops_summary(self) -> None:
        op = (ROOT / "Operations.md").read_text(encoding="utf-8")
        self.assertIn("流滞后", op)
        self.assertIn("signals_dead_letter_ops_summary", op)
        self.assertIn("回撤与紧急", op)

    def test_architecture_links_observability_doc(self) -> None:
        arch = (ROOT / "Architecture.md").read_text(encoding="utf-8")
        self.assertIn("observability-stream-contract.md", arch)

    def test_dead_letter_ops_summary_static(self) -> None:
        ex = _load_executor_main_module()
        summary = ex.signals_dead_letter_ops_summary()
        self.assertEqual(summary["default_stream"], "signals_dead_letter")
        self.assertEqual(
            summary["xadd_fields"],
            ("entry_id", "error", "payload"),
        )
        self.assertIn("observability-stream-contract.md", summary["observability_doc"])


if __name__ == "__main__":
    unittest.main()
