"""Static checks for the Sprint029A scoped validation design artifact."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "mvp" / "audits" / "sprint029a_scoped_validation_design.md"
THIS_FILE = Path(__file__).resolve()


class Sprint029ADesignArtifactTests(unittest.TestCase):
    def test_design_file_exists(self) -> None:
        self.assertTrue(DESIGN.is_file(), msg=f"Missing: {DESIGN}")

    def test_design_contains_required_contract_phrases(self) -> None:
        text = DESIGN.read_text(encoding="utf-8")
        for needle in (
            "028B PASS ≠ alpha",
            "028C ranking ≠ predictive",
            "within-pool temporal",
            "walk-forward",
            "cost-adjusted net PnL",
            "ROUND_TRIP_COST=0.006",
            "stop condition",
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "SCOPED_VALIDATION_EXECUTION_ALLOWED=false",
        ):
            self.assertIn(needle, text, msg=f"design missing: {needle}")

    def test_design_keeps_blocking_flags_false(self) -> None:
        text = DESIGN.read_text(encoding="utf-8")
        for needle in (
            "TRAINING_ALLOWED=false",
            "LABELING_ALLOWED=false",
            "LIVE_ALLOWED=false",
            "PHASE6_ALLOWED=false",
            "H12_ABLATION_ALLOWED=false",
            "MICRO_LIVE_EXECUTION_ALLOWED=false",
            "SMART_MONEY_GENERAL_LINE_OPEN=false",
        ):
            self.assertIn(needle, text, msg=f"flag missing or flipped: {needle}")

    def test_this_static_test_does_not_import_forbidden_runtime_modules(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        forbidden = {
            "scripts.retrain_model",
            "retrain_model",
            "scripts.label_dashcam_executable",
            "label_dashcam_executable",
            "redis",
            "clickhouse_driver",
            "requests",
            "web3",
        }
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module)
        self.assertTrue(seen.isdisjoint(forbidden), msg=f"forbidden imports: {seen & forbidden}")

    def test_design_forbids_external_execution_paths(self) -> None:
        text = DESIGN.read_text(encoding="utf-8")
        for needle in (
            "no RPC",
            "no Dune",
            "no Redis writes",
            "no ClickHouse query",
            "no labeling",
            "no training",
            "no H12",
            "no Phase6",
            "no live",
        ):
            self.assertIn(needle, text, msg=f"execution guard missing: {needle}")


if __name__ == "__main__":
    unittest.main()
