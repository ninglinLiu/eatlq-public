#!/usr/bin/env python3
"""Unit tests for model manifest gate (offline, no Redis)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from xgboost import XGBClassifier

REPO = Path(__file__).resolve().parents[1]
STRATEGY_SRC = REPO / "services" / "strategy" / "src"
sys.path.insert(0, str(STRATEGY_SRC))

from main import ModelScorer, RUNTIME_FEATURE_ORDER  # noqa: E402


class ModelManifestGateTests(unittest.TestCase):
    def _train_tiny_model(self, path: Path, n_features: int) -> None:
        rng = np.random.default_rng(0)
        x = rng.random((40, n_features))
        y = (rng.random(40) > 0.6).astype(int)
        model = XGBClassifier(
            max_depth=2,
            n_estimators=10,
            objective="binary:logistic",
            tree_method="hist",
            random_state=0,
        )
        model.fit(x, y)
        model.save_model(str(path))

    def test_matching_manifest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            model_path = model_dir / "signal_model_realtime_v1.json"
            self._train_tiny_model(model_path, 4)
            order = list(RUNTIME_FEATURE_ORDER[:4])
            manifest = {
                "model_file": model_path.name,
                "feature_order": order,
                "num_features": 4,
            }
            model_path.with_name(f"{model_path.stem}.manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            scorer = ModelScorer(model_dir, chain_id=8453)
            scorer._reload_if_needed()
            self.assertIsNotNone(scorer.model)
            self.assertIsNone(scorer.last_error)

    def test_mismatched_manifest_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            model_path = model_dir / "signal_model_realtime_v1.json"
            self._train_tiny_model(model_path, 4)
            bad_order = list(reversed(RUNTIME_FEATURE_ORDER[:4]))
            manifest = {
                "model_file": model_path.name,
                "feature_order": bad_order,
                "num_features": 4,
            }
            model_path.with_name(f"{model_path.stem}.manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            scorer = ModelScorer(model_dir, chain_id=8453)
            scorer._reload_if_needed()
            self.assertIsNone(scorer.model)
            self.assertIn("manifest_rejected", scorer.last_error or "")


if __name__ == "__main__":
    unittest.main()
