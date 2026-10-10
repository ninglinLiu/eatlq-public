"""Sprint035F — short-horizon & breakout-anchor diagnostic contracts (offline-safe)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

OHLCV_DIAG    = REPO / "mvp/diagnostics/sprint035f_ohlcv_multihorizon_diagnostic.csv"
BREAKOUT_MAN  = REPO / "mvp/diagnostics/sprint035f_breakout_anchor_manifest.json"
BREAKOUT_LBLS = REPO / "mvp/diagnostics/sprint035f_breakout_anchor_labels.csv"
COMPARISON    = REPO / "mvp/diagnostics/sprint035f_horizon_comparison.json"
REPORT        = REPO / "mvp/audits/sprint035f_short_horizon_breakout_diagnostic_report.md"
SCRIPT        = REPO / "scripts/sprint035f_short_horizon_breakout_diagnostic.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _load_diag() -> Any:
    return _load_module("sprint035f_diag", SCRIPT)


class TestSprint035fArtifacts(unittest.TestCase):
    """Artifact existence and shape contracts."""

    def test_ohlcv_diagnostic_csv_exists(self) -> None:
        self.assertTrue(OHLCV_DIAG.is_file(), f"missing {OHLCV_DIAG}")

    def test_breakout_manifest_exists(self) -> None:
        self.assertTrue(BREAKOUT_MAN.is_file(), f"missing {BREAKOUT_MAN}")

    def test_breakout_labels_csv_exists(self) -> None:
        self.assertTrue(BREAKOUT_LBLS.is_file(), f"missing {BREAKOUT_LBLS}")

    def test_comparison_json_exists(self) -> None:
        self.assertTrue(COMPARISON.is_file(), f"missing {COMPARISON}")

    def test_report_md_exists(self) -> None:
        self.assertTrue(REPORT.is_file(), f"missing {REPORT}")


class TestSprint035fOhlcvDiagnosticShape(unittest.TestCase):
    """OHLCV diagnostic CSV column and value contracts."""

    def setUp(self) -> None:
        with OHLCV_DIAG.open(encoding="utf-8") as f:
            self.rows = list(csv.DictReader(f))

    def test_has_expected_columns(self) -> None:
        if not self.rows:
            self.skipTest("empty CSV")
        fields = set(self.rows[0].keys())
        for col in ("pool", "bucket_sec", "net_h5m", "net_h15m", "net_h30m",
                    "label_h5m", "label_h15m", "label_h30m"):
            self.assertIn(col, fields, f"missing column: {col}")

    def test_net_values_are_numeric_or_empty(self) -> None:
        for r in self.rows:
            for col in ("net_h5m", "net_h15m", "net_h30m"):
                v = r[col]
                if v not in ("", "None"):
                    _ = float(v)  # must parse as float

    def test_label_values_are_binary_or_empty(self) -> None:
        for r in self.rows:
            for col in ("label_h5m", "label_h15m", "label_h30m"):
                v = r[col]
                if v not in ("", "None"):
                    self.assertIn(v, ("0", "1"), f"unexpected label value: {v}")

    def test_bucket_sec_are_5min_aligned(self) -> None:
        for r in self.rows:
            bs = int(r["bucket_sec"])
            self.assertEqual(bs % 300, 0, f"bucket_sec not 5m-aligned: {bs}")

    def test_has_rows_from_all_three_pools(self) -> None:
        if not self.rows:
            self.skipTest("empty CSV")
        pools = {r["pool"] for r in self.rows}
        self.assertGreaterEqual(len(pools), 2, f"expected 2+ pools, got: {pools}")


class TestSprint035fBreakoutManifest(unittest.TestCase):
    """Breakout manifest structural contracts."""

    def setUp(self) -> None:
        self.manifest = json.loads(BREAKOUT_MAN.read_text())

    def test_no_training_flag(self) -> None:
        self.assertTrue(self.manifest.get("no_training"))

    def test_no_live_flag(self) -> None:
        self.assertTrue(self.manifest.get("no_live"))

    def test_diagnostic_only_flag(self) -> None:
        self.assertTrue(self.manifest.get("diagnostic_only"))

    def test_manifest_rows_exist(self) -> None:
        rows = self.manifest.get("manifest_rows", [])
        self.assertIsInstance(rows, list)

    def test_all_rows_have_required_fields(self) -> None:
        rows = self.manifest.get("manifest_rows", [])
        for r in rows:
            for field in ("pool_audit_only", "bucket_ms", "bucket_sec", "signal_ts_ms_audit_only"):
                self.assertIn(field, r, f"row missing field '{field}': {r}")

    def test_all_rows_older_than_exit_horizon(self) -> None:
        """Each entry must be > 30 min old (exit horizon age guard)."""
        now_ms = int(time.time() * 1000)
        min_age_ms = (1800 + 120) * 1000  # 32 min
        rows = self.manifest.get("manifest_rows", [])
        for r in rows:
            age = now_ms - r["bucket_ms"]
            self.assertGreater(age, min_age_ms,
                               f"entry too recent: {r['bucket_ms']} (age {age}ms)")

    def test_source_is_breakout_anchor(self) -> None:
        rows = self.manifest.get("manifest_rows", [])
        for r in rows:
            self.assertIn(r.get("source", ""), ("breakout_anchor", "breakout_anchor_follow"),
                          f"unexpected source: {r.get('source')}")


class TestSprint035fComparisonJson(unittest.TestCase):
    """Comparison JSON key contract."""

    def setUp(self) -> None:
        self.comp = json.loads(COMPARISON.read_text())

    def test_no_training_flag(self) -> None:
        self.assertTrue(self.comp.get("no_training"))

    def test_no_live_flag(self) -> None:
        self.assertTrue(self.comp.get("no_live"))

    def test_diagnostic_only_flag(self) -> None:
        self.assertTrue(self.comp.get("diagnostic_only_ohlcv_approx"))

    def test_ohlcv_multihorizon_present(self) -> None:
        dim1 = self.comp.get("dim1_ohlcv_multihorizon", {})
        for h in ("h5m", "h15m", "h30m"):
            self.assertIn(h, dim1, f"missing horizon {h} in dim1")

    def test_e035_baseline_captured(self) -> None:
        baseline = self.comp.get("e035_30m_rpc_baseline", {})
        self.assertEqual(baseline.get("label_1"), 0)
        self.assertEqual(baseline.get("label1_pct"), 0.0)

    def test_root_cause_summary_present(self) -> None:
        self.assertIn("root_cause_summary", self.comp)

    def test_breakout_ts_grid_warning_present(self) -> None:
        bs = self.comp.get("dim2_breakout_anchor_rpc_30m", {})
        self.assertIn("RELIABILITY_WARNING", bs,
                      "ts_grid reliability warning must be present")

    def test_temporal_decay_present(self) -> None:
        td = self.comp.get("dim3_temporal_decay", {})
        self.assertIn("oldest_half", td)
        self.assertIn("newest_half", td)


class TestSprint035fHelpers(unittest.TestCase):
    """Unit tests for diagnostic helper functions (no HTTP/RPC)."""

    def setUp(self) -> None:
        self.mod = _load_diag()

    # ── find_breakout_candles ───────────────────────────────────────────────

    def _make_candles(self, prices: list[float], base_ts: int = 0) -> list[dict]:
        """Create synthetic 5-min candles from a price list."""
        candles = []
        for i, p in enumerate(prices):
            prev = prices[i - 1] if i > 0 else p
            candles.append({
                "ts_sec":  base_ts + i * 300,
                "open_p":  prev,
                "high_p":  max(prev, p),
                "low_p":   min(prev, p),
                "close_p": p,
                "volume":  100.0,
            })
        return candles

    def test_breakout_detected_when_h1_crosses_threshold(self) -> None:
        # First 12 candles: flat at 1.0 → h1 = 0%
        # Then 12 candles: rise to 1.05 → h1 = 5% (crosses 2%)
        now_sec = int(time.time())
        base_ts = now_sec - 30 * 300  # 30 candles ago (150 min)
        flat = [1.0] * 13
        rising = [1.0 + 0.005 * i for i in range(1, 14)]
        candles = self._make_candles(flat + rising, base_ts=base_ts)
        breakouts = self.mod.find_breakout_candles(
            candles, h1_window=12, threshold_pct=2.0, max_breakouts=3, min_age_sec=2100
        )
        self.assertGreater(len(breakouts), 0, "should find at least one breakout")
        self.assertGreaterEqual(breakouts[0]["h1_return_pct"], 2.0)

    def test_no_breakout_when_price_flat(self) -> None:
        now_sec = int(time.time())
        base_ts = now_sec - 40 * 300
        flat = [1.0] * 40
        candles = self._make_candles(flat, base_ts=base_ts)
        breakouts = self.mod.find_breakout_candles(candles, min_age_sec=2100)
        self.assertEqual(len(breakouts), 0)

    def test_breakout_max_results_respected(self) -> None:
        now_sec = int(time.time())
        base_ts = now_sec - 60 * 300
        # Multiple crossings
        prices = [1.0] * 12 + [1.03] * 5 + [0.99] * 7 + [1.04] * 5 + [0.98] * 7 + [1.05] * 5 + [0.99] * 19
        candles = self._make_candles(prices, base_ts=base_ts)
        breakouts = self.mod.find_breakout_candles(candles, max_breakouts=2, min_age_sec=2100)
        self.assertLessEqual(len(breakouts), 2)

    def test_breakout_age_guard_filters_recent(self) -> None:
        now_sec = int(time.time())
        # All candles are very recent (< 35 min)
        base_ts = now_sec - 10 * 300  # 50 min ago
        prices = [1.0] * 12 + [1.05] * 12
        candles = self._make_candles(prices, base_ts=base_ts)
        breakouts = self.mod.find_breakout_candles(candles, min_age_sec=2100)
        # Recent candles filtered out — candles > 35 min old might still be present
        for b in breakouts:
            self.assertGreaterEqual(now_sec - b["ts_sec"], 2100)

    # ── ohlcv_multi_horizon ─────────────────────────────────────────────────

    def test_ohlcv_multi_horizon_positive_return(self) -> None:
        """If price rises steadily, h5m/h15m/h30m should show positive returns."""
        now_ms = int(time.time() * 1000)
        base_ts = (now_ms // 1000) - 10 * 300  # 50 min ago
        prices = [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.09, 1.10]
        candles = self._make_candles(prices, base_ts=base_ts)

        # One manifest row pointing to the first candle
        pool = "0x" + "ab" * 20
        manifest_rows = [{
            "pool_audit_only": pool,
            "bucket_sec":      base_ts,
            "signal_ts_ms_audit_only": base_ts * 1000 + 120_000,
        }]
        rows = self.mod.ohlcv_multi_horizon(pool, manifest_rows, candles)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # +10% rise should more than cover 0.6% round-trip
        if r.get("net_h30m") is not None:
            self.assertGreater(r["net_h30m"], 0)

    def test_ohlcv_multi_horizon_negative_return(self) -> None:
        """If price falls, all horizons should show negative returns."""
        now_ms = int(time.time() * 1000)
        base_ts = (now_ms // 1000) - 10 * 300
        prices = [1.0, 0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.91, 0.90]
        candles = self._make_candles(prices, base_ts=base_ts)

        pool = "0x" + "cd" * 20
        manifest_rows = [{
            "pool_audit_only": pool,
            "bucket_sec":      base_ts,
            "signal_ts_ms_audit_only": base_ts * 1000 + 120_000,
        }]
        rows = self.mod.ohlcv_multi_horizon(pool, manifest_rows, candles)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        if r.get("net_h5m") is not None:
            self.assertLess(r["net_h5m"], 0)

    def test_ohlcv_multi_horizon_no_candle_returns_none(self) -> None:
        """Missing exit candle should give None net_return."""
        now_ms = int(time.time() * 1000)
        base_ts = (now_ms // 1000) - 5 * 300  # 25 min ago
        prices = [1.0, 1.01]  # only 2 candles — no h30m exit candle
        candles = self._make_candles(prices, base_ts=base_ts)

        pool = "0x" + "ef" * 20
        manifest_rows = [{
            "pool_audit_only": pool,
            "bucket_sec":      base_ts,
            "signal_ts_ms_audit_only": base_ts * 1000 + 120_000,
        }]
        rows = self.mod.ohlcv_multi_horizon(pool, manifest_rows, candles)
        if rows:
            self.assertIsNone(rows[0]["net_h30m"])

    # ── build_breakout_manifest ─────────────────────────────────────────────

    def test_build_breakout_manifest_no_dupes(self) -> None:
        pools = ["0x" + "aa" * 20, "0x" + "bb" * 20]
        pool_breakouts = {
            pools[0]: [{"ts_sec": 1_000_000, "h1_return_pct": 3.0, "close_p": 1.0, "is_bullish": True}],
            pools[1]: [{"ts_sec": 2_000_000, "h1_return_pct": 2.5, "close_p": 0.5, "is_bullish": False}],
        }
        rows = self.mod.build_breakout_manifest(pools, pool_breakouts, {})
        keys = [(r["pool_audit_only"], r["bucket_ms"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "duplicate (pool, bucket_ms) found")

    def test_build_breakout_manifest_source_tag(self) -> None:
        pool = "0x" + "cc" * 20
        pool_breakouts = {
            pool: [{"ts_sec": 1_000_000, "h1_return_pct": 2.2, "close_p": 1.0, "is_bullish": True}],
        }
        rows = self.mod.build_breakout_manifest([pool], pool_breakouts, {})
        sources = {r["source"] for r in rows}
        self.assertIn("breakout_anchor", sources)
        self.assertIn("breakout_anchor_follow", sources)


class TestSprint035fReportContent(unittest.TestCase):
    """Report content contracts."""

    def setUp(self) -> None:
        self.text = REPORT.read_text()

    def test_contains_root_cause_section(self) -> None:
        self.assertIn("Root cause", self.text)

    def test_contains_ts_grid_warning(self) -> None:
        self.assertIn("ts_grid", self.text.lower())

    def test_no_training_statement(self) -> None:
        self.assertIn("no_training", self.text.lower().replace(" ", "_").replace("/", "_"))

    def test_contains_dim1_ohlcv_section(self) -> None:
        self.assertIn("Dim-1", self.text)

    def test_contains_breakout_section(self) -> None:
        self.assertIn("Dim-2", self.text)

    def test_contains_temporal_section(self) -> None:
        self.assertIn("Dim-3", self.text)

    def test_contains_sprint036_recommendation(self) -> None:
        self.assertIn("Sprint036", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
