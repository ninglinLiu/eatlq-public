"""Sprint036A — toxic-flow paper watchlist contracts (offline-safe)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

WATCHLIST_JSON  = REPO / "mvp/diagnostics/sprint036a_paper_watchlist.json"
FLOW_DIAG_CSV   = REPO / "mvp/diagnostics/sprint036a_flow_diagnostics.csv"
REJECTED_CSV    = REPO / "mvp/diagnostics/sprint036a_toxic_rejected_pools.csv"
REPORT_MD       = REPO / "mvp/audits/sprint036a_toxic_flow_paper_watchlist_report.md"
SCRIPT          = REPO / "scripts/sprint036a_toxic_flow_paper_watchlist.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _load_script() -> Any:
    return _load_module("sprint036a_watchlist", SCRIPT)


class TestSprint036aArtifactPresence(unittest.TestCase):
    """All output files must exist."""

    def test_watchlist_json_exists(self) -> None:
        self.assertTrue(WATCHLIST_JSON.is_file())

    def test_flow_diagnostics_csv_exists(self) -> None:
        self.assertTrue(FLOW_DIAG_CSV.is_file())

    def test_rejected_csv_exists(self) -> None:
        self.assertTrue(REJECTED_CSV.is_file())

    def test_report_md_exists(self) -> None:
        self.assertTrue(REPORT_MD.is_file())


class TestSprint036aWatchlistJson(unittest.TestCase):
    """Watchlist JSON schema and red-line flags."""

    def setUp(self) -> None:
        self.wl = json.loads(WATCHLIST_JSON.read_text())

    def test_failed_no_7d_alpha_flag(self) -> None:
        self.assertTrue(self.wl["FAILED_NO_7D_ALPHA_ACTIVE"])

    def test_no_training_flag(self) -> None:
        self.assertTrue(self.wl["no_training"])

    def test_no_live_flag(self) -> None:
        self.assertTrue(self.wl["no_live"])

    def test_no_model_promotion_flag(self) -> None:
        self.assertTrue(self.wl["no_model_promotion"])

    def test_diagnostic_only_flag(self) -> None:
        self.assertTrue(self.wl["diagnostic_only"])

    def test_gates_present(self) -> None:
        gates = self.wl.get("gates_applied", {})
        self.assertIn("h1_gate_pct", gates)
        self.assertIn("h6_floor_pct", gates)
        self.assertIn("buys_min", gates)
        self.assertEqual(gates["h1_gate_pct"], 2.0)

    def test_watchlist_count_consistent(self) -> None:
        self.assertEqual(
            self.wl["watchlist_count"],
            len(self.wl["watchlist_entries"]),
        )

    def test_rejected_count_consistent(self) -> None:
        self.assertEqual(
            self.wl["rejected_count"],
            self.wl["total_candidates"] - self.wl["watchlist_count"],
        )

    def test_watchlist_entries_have_required_fields(self) -> None:
        for e in self.wl["watchlist_entries"]:
            for field in ("pool_address", "pair_label", "h1_pct", "h6_pct",
                          "toxic_risk", "flow_quality_score", "rejection_reasons"):
                self.assertIn(field, e, f"entry missing '{field}': {e}")

    def test_watchlist_entries_pass_h1_gate(self) -> None:
        """All watchlist entries must have h1 >= H1_GATE."""
        for e in self.wl["watchlist_entries"]:
            self.assertGreaterEqual(
                e["h1_pct"], 2.0,
                f"watchlist entry violates h1 gate: {e['pair_label']} h1={e['h1_pct']}",
            )

    def test_watchlist_entries_pass_h6_gate(self) -> None:
        for e in self.wl["watchlist_entries"]:
            self.assertGreaterEqual(
                e["h6_pct"], -5.0,
                f"watchlist entry violates h6 gate: {e['pair_label']} h6={e['h6_pct']}",
            )

    def test_watchlist_entries_have_no_active_rejections(self) -> None:
        """Entries that appear in watchlist must have empty rejection_reasons."""
        for e in self.wl["watchlist_entries"]:
            self.assertEqual(
                e["rejection_reasons"], [],
                f"watchlist entry has rejection reasons: {e['pair_label']} {e['rejection_reasons']}",
            )

    def test_toxic_pattern_counts_present(self) -> None:
        self.assertIn("toxic_pattern_counts", self.wl)
        self.assertIn("WEAK_H1", self.wl["toxic_pattern_counts"])

    def test_weak_h1_is_dominant_rejection(self) -> None:
        """WEAK_H1 should be the most common rejection reason in 035D/E universe."""
        tpc = self.wl["toxic_pattern_counts"]
        max_count = max(tpc.values()) if tpc else 0
        self.assertEqual(
            tpc.get("WEAK_H1", 0),
            max_count,
            "WEAK_H1 should be the most frequent rejection reason",
        )


class TestSprint036aFlowDiagnosticsCsv(unittest.TestCase):
    """Flow diagnostics CSV shape and value contracts."""

    def setUp(self) -> None:
        with FLOW_DIAG_CSV.open(encoding="utf-8") as f:
            self.rows = list(csv.DictReader(f))

    def test_has_rows(self) -> None:
        self.assertGreater(len(self.rows), 0)

    def test_has_expected_columns(self) -> None:
        fields = set(self.rows[0].keys())
        for col in ("pool_address", "pair_label", "h1_pct", "h6_pct",
                    "watchlist_status", "toxic_risk", "flow_quality_score",
                    "rejection_reasons"):
            self.assertIn(col, fields, f"missing column: {col}")

    def test_pool_addresses_are_valid_eth_format(self) -> None:
        for r in self.rows:
            addr = r["pool_address"]
            self.assertTrue(
                addr.startswith("0x") and len(addr) == 42,
                f"invalid address: {addr}",
            )

    def test_flow_quality_score_in_range(self) -> None:
        for r in self.rows:
            if r["watchlist_status"] in ("WATCHLIST", "L1_PASS", "L1_PASS_AWAITING_L2"):
                score = float(r["flow_quality_score"])
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)

    def test_h1_pct_numeric(self) -> None:
        for r in self.rows:
            _ = float(r["h1_pct"])  # must be numeric

    def test_covers_all_candidates(self) -> None:
        """Must have at least 80 rows (from 87 candidates)."""
        self.assertGreaterEqual(len(self.rows), 80)


class TestSprint036aRejectedCsv(unittest.TestCase):
    """Rejected CSV must cover all non-watchlist pools."""

    def setUp(self) -> None:
        with REJECTED_CSV.open(encoding="utf-8") as f:
            self.rej_rows = list(csv.DictReader(f))
        self.wl = json.loads(WATCHLIST_JSON.read_text())

    def test_rejected_count_matches_json(self) -> None:
        self.assertEqual(
            len(self.rej_rows),
            self.wl["rejected_count"],
            "rejected CSV row count must match watchlist JSON rejected_count",
        )

    def test_all_rejected_have_reason(self) -> None:
        for r in self.rej_rows:
            self.assertTrue(
                r.get("rejection_reasons", "").strip(),
                f"rejected pool has empty rejection_reasons: {r.get('pool_address')}",
            )

    def test_no_watchlist_pool_in_rejected(self) -> None:
        wl_addrs = {e["pool_address"].lower() for e in self.wl["watchlist_entries"]}
        for r in self.rej_rows:
            addr = r["pool_address"].lower()
            self.assertNotIn(
                addr, wl_addrs,
                f"watchlist pool appears in rejected CSV: {r.get('pair_label')}",
            )


class TestSprint036aReportContent(unittest.TestCase):
    """Report MD content contracts."""

    def setUp(self) -> None:
        self.text = REPORT_MD.read_text()

    def test_contains_root_cause_section(self) -> None:
        self.assertIn("BSR-only discovery fails", self.text)

    def test_contains_failed_no_7d_alpha(self) -> None:
        self.assertIn("FAILED_NO_7D_ALPHA_ACTIVE", self.text)

    def test_contains_rejection_table(self) -> None:
        self.assertIn("WEAK_H1", self.text)

    def test_contains_watchlist_section(self) -> None:
        self.assertIn("Paper watchlist", self.text)

    def test_contains_toxic_pattern_catalogue(self) -> None:
        self.assertIn("Toxic pattern catalogue", self.text)

    def test_contains_sprint036b_gates(self) -> None:
        self.assertIn("Sprint036B", self.text)

    def test_no_training_mentioned(self) -> None:
        self.assertIn("No training", self.text)


class TestSprint036aHelpers(unittest.TestCase):
    """Unit tests for L1 classification and entropy helpers (no HTTP/RPC/CH)."""

    def setUp(self) -> None:
        self.mod = _load_script()

    def _make_cand(self, h1: float, h6: float, buys: int, sells: int,
                   liq: float = 200_000, pair: str = "TOKEN/WETH 0.3%") -> dict:
        return {
            "pool_address_candidate": "0x" + "ab" * 20,
            "pair_label": pair,
            "price_change_h1": str(h1),
            "price_change_h6": str(h6),
            "price_change_h24": "0",
            "liquidity_usd": str(liq),
            "volume_24h_usd": "100000",
            "txns_1h_buys": str(buys),
            "txns_1h_sells": str(sells),
            "txns_24h": str(buys + sells),
            "ohlcv_up_frac": "0.5",
            "momentum_score": "3",
            "dex_id": "uniswap-v3",
            "_sprint": "test",
        }

    # ── L1 PASS conditions ─────────────────────────────────────────────────

    def test_l1_pass_clean_breakout(self) -> None:
        c = self._make_cand(h1=3.5, h6=5.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertEqual(d.watchlist_status, "L1_PASS")
        self.assertEqual(d.rejection_reasons, [])

    def test_l1_pass_minimum_h1(self) -> None:
        c = self._make_cand(h1=2.0, h6=0.0, buys=10, sells=5)
        d = self.mod.classify_l1(c)
        self.assertEqual(d.watchlist_status, "L1_PASS")

    # ── WEAK_H1 ────────────────────────────────────────────────────────────

    def test_weak_h1_rejected(self) -> None:
        c = self._make_cand(h1=1.5, h6=2.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertIn("WEAK_H1", d.rejection_reasons)

    def test_zero_h1_rejected(self) -> None:
        c = self._make_cand(h1=0.0, h6=0.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertIn("WEAK_H1", d.rejection_reasons)

    def test_negative_h1_rejected(self) -> None:
        c = self._make_cand(h1=-2.0, h6=-1.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertIn("WEAK_H1", d.rejection_reasons)

    # ── DOWNTREND_H6 ───────────────────────────────────────────────────────

    def test_downtrend_h6_rejected(self) -> None:
        c = self._make_cand(h1=3.0, h6=-10.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertIn("DOWNTREND_H6", d.rejection_reasons)

    def test_border_h6_passes(self) -> None:
        c = self._make_cand(h1=3.0, h6=-5.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertNotIn("DOWNTREND_H6", d.rejection_reasons)

    # ── DEAD_BUCKET ────────────────────────────────────────────────────────

    def test_dead_bucket_rejected(self) -> None:
        c = self._make_cand(h1=3.0, h6=1.0, buys=3, sells=2)
        d = self.mod.classify_l1(c)
        self.assertIn("DEAD_BUCKET", d.rejection_reasons)

    # ── SINGLE_SIDED_PUMP ──────────────────────────────────────────────────

    def test_single_sided_pump_rejected(self) -> None:
        c = self._make_cand(h1=3.0, h6=1.0, buys=10, sells=0)
        d = self.mod.classify_l1(c)
        self.assertIn("SINGLE_SIDED_PUMP", d.rejection_reasons)

    def test_sells_one_does_not_trigger_ssp(self) -> None:
        """sells=1 is suspicious but should not trigger SSP gate (sells != 0)."""
        c = self._make_cand(h1=3.0, h6=1.0, buys=10, sells=1)
        d = self.mod.classify_l1(c)
        self.assertNotIn("SINGLE_SIDED_PUMP", d.rejection_reasons)

    def test_ssp_needs_low_buy_count(self) -> None:
        """sells=0 but buys>=20 should NOT trigger SSP (might be asymmetric but active)."""
        c = self._make_cand(h1=3.0, h6=1.0, buys=25, sells=0)
        d = self.mod.classify_l1(c)
        self.assertNotIn("SINGLE_SIDED_PUMP", d.rejection_reasons)

    # ── OVERHEATED_H1 ──────────────────────────────────────────────────────

    def test_overheated_h1_rejected(self) -> None:
        c = self._make_cand(h1=20.0, h6=30.0, buys=50, sells=30)
        d = self.mod.classify_l1(c)
        self.assertIn("OVERHEATED_H1", d.rejection_reasons)

    def test_border_h1_max_passes(self) -> None:
        c = self._make_cand(h1=14.9, h6=10.0, buys=30, sells=15)
        d = self.mod.classify_l1(c)
        self.assertNotIn("OVERHEATED_H1", d.rejection_reasons)

    # ── ILLIQUID ────────────────────────────────────────────────────────────

    def test_illiquid_rejected(self) -> None:
        c = self._make_cand(h1=3.0, h6=1.0, buys=20, sells=10, liq=10_000)
        d = self.mod.classify_l1(c)
        self.assertIn("ILLIQUID", d.rejection_reasons)

    def test_border_liquidity_passes(self) -> None:
        c = self._make_cand(h1=3.0, h6=1.0, buys=20, sells=10, liq=50_000)
        d = self.mod.classify_l1(c)
        self.assertNotIn("ILLIQUID", d.rejection_reasons)

    # ── Stable pair ────────────────────────────────────────────────────────

    def test_usdc_usdt_pair_rejected(self) -> None:
        c = self._make_cand(h1=3.0, h6=1.0, buys=20, sells=10, pair="USDC / USDT 0.01%")
        c["base_token_address"]  = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        c["quote_token_address"] = "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2"
        d = self.mod.classify_l1(c)
        self.assertIn("STABLE_PAIR", d.rejection_reasons)

    # ── Flow quality score ─────────────────────────────────────────────────

    def test_flow_quality_nonzero_on_pass(self) -> None:
        c = self._make_cand(h1=5.0, h6=3.0, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertEqual(d.watchlist_status, "L1_PASS")
        self.assertGreater(d.flow_quality_score, 0.0)

    def test_flow_quality_zero_on_reject(self) -> None:
        c = self._make_cand(h1=0.5, h6=0.5, buys=20, sells=10)
        d = self.mod.classify_l1(c)
        self.assertEqual(d.rejection_reasons[0], "WEAK_H1")
        self.assertEqual(d.flow_quality_score, 0.0)

    # ── Entropy helper ─────────────────────────────────────────────────────

    def test_entropy_uniform_dist(self) -> None:
        counts = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0}
        entropy = self.mod._compute_entropy(counts)
        self.assertAlmostEqual(entropy, 1.3862, places=3)  # ln(4)

    def test_entropy_single_wallet(self) -> None:
        counts = {"a": 100.0}
        entropy = self.mod._compute_entropy(counts)
        self.assertAlmostEqual(entropy, 0.0, places=6)

    def test_entropy_empty(self) -> None:
        self.assertEqual(self.mod._compute_entropy({}), 0.0)


class TestSprint036aEndToEnd(unittest.TestCase):
    """Light end-to-end pipeline test (no CH, no HTTP)."""

    def setUp(self) -> None:
        self.mod = _load_script()

    def test_pipeline_runs_dry(self) -> None:
        """run_pipeline with dry_run=True must complete without error."""
        # Uses existing 035D/E CSV artifacts, no CH or HTTP calls
        status = self.mod.run_pipeline(dry_run=True)
        self.assertIn(status, ("WATCHLIST_BUILT", "NO_WATCHLIST_CANDIDATES"))

    def test_pipeline_produces_watchlist_json(self) -> None:
        self.assertTrue(WATCHLIST_JSON.is_file())
        wl = json.loads(WATCHLIST_JSON.read_text())
        self.assertIn("watchlist_entries", wl)
        self.assertTrue(wl.get("FAILED_NO_7D_ALPHA_ACTIVE"))

    def test_pipeline_watchlist_entries_pass_gates(self) -> None:
        wl = json.loads(WATCHLIST_JSON.read_text())
        for e in wl["watchlist_entries"]:
            self.assertGreaterEqual(e["h1_pct"], 2.0)
            self.assertGreaterEqual(e["h6_pct"], -5.0)
            self.assertEqual(e["rejection_reasons"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
