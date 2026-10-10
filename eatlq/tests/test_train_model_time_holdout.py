"""P2-⑥ regression: live model training must use a TIME-ORDERED holdout, with
p_cut learned on TRAIN only and holdout metrics reported. No shuffle split.
Offline — no ClickHouse, no network (run_sql is never called)."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SRC = ROOT / "services" / "signal_bridge"


def _load_train_model():
    if str(BRIDGE_SRC) not in sys.path:
        sys.path.insert(0, str(BRIDGE_SRC))
    spec = importlib.util.spec_from_file_location("keystone_train_model_test", BRIDGE_SRC / "train_model.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TrainModelHoldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = _load_train_model()

    def test_no_shuffle_split_in_source(self):
        src = (BRIDGE_SRC / "train_model.py").read_text()
        self.assertNotIn("train_test_split", src, "must not use sklearn random split")
        self.assertNotIn("shuffle=True", src)
        self.assertIn("time_ordered_split", src)

    def test_time_ordered_split_is_chronological(self):
        # 10 pools, ascending t0; quantile(0.6) -> first 6 train, last 4 holdout.
        t0 = pd.to_datetime(["2026-01-%02d" % d for d in range(1, 11)])
        df = pd.DataFrame({"t0": t0, "x": range(10)})
        train_mask, holdout_mask, cut = self.tm.time_ordered_split(df, 0.6)
        # train must be the EARLIEST rows; holdout the latest — no interleave.
        train_idx = np.where(train_mask)[0]
        holdout_idx = np.where(holdout_mask)[0]
        self.assertTrue(train_idx.max() < holdout_idx.min(),
                        "train rows must all precede holdout rows (chronological)")
        self.assertEqual(train_mask.sum() + holdout_mask.sum(), 10)
        self.assertGreater(train_mask.sum(), 0)
        self.assertGreater(holdout_mask.sum(), 0)
        # cut is the 60th percentile of t0
        self.assertEqual(cut, pd.Series(pd.to_datetime(t0)).quantile(0.6))

    def test_p_cut_uses_train_only(self):
        # End-to-end on synthetic data, stubbing run_sql so no ClickHouse needed.
        rng = np.random.default_rng(0)
        n = 200
        t0 = pd.to_datetime("2026-01-01") + pd.to_timedelta(np.arange(n), unit="h")
        df = pd.DataFrame({
            "pool": [f"0x{i:040x}" for i in range(n)],
            "orient": rng.choice([-1, 1], n),
            "t0": t0,
            "ns_pre": rng.integers(5, 50, n),
            "buyers_pre": rng.integers(1, 20, n),
            "senders_pre": rng.integers(1, 20, n),
            "first_tick": rng.integers(-1000, 1000, n),
            "last_tick_pre": rng.integers(-1000, 1000, n),
            "min_tick_pre": rng.integers(-2000, 0, n),
            "max_tick_pre": rng.integers(0, 2000, n),
            "liq_first": rng.integers(1, 10**6, n),
            "liq_last_pre": rng.integers(1, 10**6, n),
            "liq_min_pre": rng.integers(1, 10**6, n),
            "ns_h1": rng.integers(1, 25, n),
            "ns_h2": rng.integers(1, 25, n),
            "last_tick_h1": rng.integers(-1000, 1000, n),
            "ns_fwd": rng.integers(0, 50, n),
            "min_tick_fwd": rng.integers(-3000, 0, n),
            "max_tick_fwd": rng.integers(0, 3000, n),
            "last_tick_fwd": rng.integers(-1000, 1000, n),
        })

        import json as _json
        captured = {}
        orig_dump = self.tm.json.dump

        def _capture_dump(obj, fh, **kw):
            captured.update(obj)
            return orig_dump(obj, fh, **kw)

        from unittest.mock import patch
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        with patch.object(self.tm, "run_sql", return_value=df), \
             patch.object(self.tm, "OUT_DIR", tmpdir), \
             patch.object(self.tm.json, "dump", side_effect=_capture_dump):
            self.tm.main()

        # params reflect the time-ordered split, not full-history fit
        self.assertEqual(captured.get("split"), "time_ordered_t0")
        self.assertIn("holdout_n", captured)
        self.assertGreater(captured["holdout_n"], 0)
        self.assertLess(captured["train_n"], captured["total_n"],
                        "train must be a strict subset (holdout carved out)")
        self.assertIn("p_cut", captured)
        self.assertTrue(os.path.exists(os.path.join(tmpdir, "_model.pkl")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
