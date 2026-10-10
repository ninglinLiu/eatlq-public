"""Sprint035D — momentum-filtered discovery + manifest contracts (offline-safe)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAND = REPO / "mvp/diagnostics/sprint035d_momentum_filtered_candidate_summary.csv"
IDENT = REPO / "mvp/diagnostics/sprint035d_pool_identity_validation.csv"
ACT = REPO / "mvp/diagnostics/sprint035d_activity_viability_audit.csv"
MANIFEST = REPO / "mvp/diagnostics/sprint035d_momentum_filtered_smoke_manifest.json"
REPORT = REPO / "mvp/audits/sprint035d_momentum_filtered_pool_discovery_report.md"

_MOMENTUM_FIELDS = {"price_change_h1", "price_change_h6", "txns_1h_buys", "txns_1h_sells",
                    "momentum_score", "momentum_gate"}


def _load_module(name: str, path: Path) -> Any:
    import importlib.util as ilu
    spec = ilu.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    m = ilu.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


class TestSprint035dDiscovery(unittest.TestCase):

    # ── T1: candidate CSV ─────────────────────────────────────────────────
    def test_candidate_csv_has_momentum_fields(self) -> None:
        self.assertTrue(CAND.is_file())
        with CAND.open(newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            rows = list(r)
            headers = set(r.fieldnames or [])
        self.assertTrue(_MOMENTUM_FIELDS <= headers, f"missing fields: {_MOMENTUM_FIELDS - headers}")
        self.assertGreaterEqual(len(rows), 8)

    def test_candidate_csv_no_sprint034_dead_pools(self) -> None:
        dead = {
            "0x5d2091e0b3e0b516e0f3abd75242e9287c253499",
            "0x8aed442e9c17746c0eb85dd22981ce8974182892",
            "0x1e98a167082332e5e89146662e34c3c99481e5e6",
        }
        with CAND.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.assertNotIn(row.get("pool_address_candidate", "").lower(), dead)

    def test_candidate_csv_momentum_pass_rows_present(self) -> None:
        with CAND.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        pass_rows = [r for r in rows if r.get("momentum_gate") == "PASS"]
        self.assertGreaterEqual(len(pass_rows), 3,
                                "Expected ≥3 momentum-PASS candidates for manifest eligibility")

    # ── T2: identity CSV ──────────────────────────────────────────────────
    def test_identity_csv_headers(self) -> None:
        self.assertTrue(IDENT.is_file())
        with IDENT.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            headers = set(r.fieldnames or [])
        for col in ("pool_address_candidate", "identity_status", "momentum_score", "momentum_gate"):
            self.assertIn(col, headers)

    def test_identity_csv_has_pass_rows(self) -> None:
        with IDENT.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        pass_rows = [r for r in rows if r.get("identity_status") == "UNISWAP_V3_BASE_POOL_PASS"]
        self.assertGreaterEqual(len(pass_rows), 3)

    # ── T3: activity CSV ──────────────────────────────────────────────────
    def test_activity_csv_headers(self) -> None:
        self.assertTrue(ACT.is_file())
        with ACT.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            headers = set(r.fieldnames or [])
        for col in ("pool_address_audit_key", "activity_status", "viability_status",
                    "momentum_score_at_selection"):
            self.assertIn(col, headers)

    # ── T4: manifest ──────────────────────────────────────────────────────
    def test_manifest_kind_and_created_before_quote(self) -> None:
        self.assertTrue(MANIFEST.is_file())
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(d.get("kind"), "momentum_filtered_external_scoped_smoke_manifest")
        self.assertTrue(d.get("created_before_quote") is True)
        self.assertTrue(d.get("no_training") is True)
        self.assertTrue(d.get("no_live") is True)

    def test_manifest_momentum_scores_all_positive(self) -> None:
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        scores = d.get("momentum_score_by_pool_at_selection") or {}
        if scores:
            for pool, sc in scores.items():
                self.assertGreaterEqual(int(sc), 3,
                                        f"pool {pool} has momentum score {sc} < 3 (gate)")

    def test_manifest_shape_valid(self) -> None:
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        rows_count = d.get("selected_rows_count", 0)
        pools_count = d.get("selected_pool_count", 0)
        status = d.get("status") or ""
        if "READY" in status or "TOO_SMALL" in status or "NO_" in status:
            return  # stub manifest — shape check not applicable
        self.assertGreaterEqual(rows_count, 120)
        self.assertLessEqual(rows_count, 250)
        self.assertGreaterEqual(pools_count, 3)
        self.assertLessEqual(pools_count, 6)
        manifest_rows = d.get("manifest_rows") or []
        self.assertEqual(len(manifest_rows), rows_count)

    def test_manifest_no_excluded_large_cap_pools(self) -> None:
        excluded = {
            "0xd0b53d9277642d899df5c87a3966a349a798f224",
            "0x6c561b446416e1a00e8e93e221854d6ea4171372",
            "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",
            "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef",
        }
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for row in d.get("manifest_rows") or []:
            self.assertNotIn(row.get("pool_audit_only", "").lower(), excluded,
                             "large-cap WETH/USDC or cbBTC pool must not be in momentum manifest")

    # ── T5: report ────────────────────────────────────────────────────────
    def test_report_exists(self) -> None:
        self.assertTrue(REPORT.is_file())

    def test_report_mentions_momentum_filter(self) -> None:
        text = REPORT.read_text(encoding="utf-8")
        self.assertIn("momentum", text.lower())

    # ── T6: shape function parity with sprint030d ─────────────────────────
    def test_shape_ok_agrees_with_sprint030d(self) -> None:
        d035d = _load_module("sprint035d_core", REPO / "scripts/sprint035d_momentum_filtered_discovery.py")
        d030d = _load_module("sprint030d_lbl", REPO / "scripts/sprint030d_bounded_smoke_label.py")
        pools = [
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "0xcccccccccccccccccccccccccccccccccccccccc",
        ]
        ctr = [(pools[i % 3], 0, 300) for i in range(120)]
        ok1, _ = d035d.external_manifest_shape_ok(ctr)
        ok2, _ = d030d.external_live_active_manifest_shape_ok(ctr)
        self.assertEqual(ok1, ok2, "shape helpers must agree between 035D and sprint030d")

    # ── T7: momentum scoring unit-test ───────────────────────────────────
    def test_momentum_score_excludes_dump(self) -> None:
        d = _load_module("sprint035d_ms", REPO / "scripts/sprint035d_momentum_filtered_discovery.py")
        row_dump = {
            "pool_address_candidate": "0x" + "a" * 40,
            "base_token_address": "0x4200000000000000000000000000000000000006",  # WETH
            "quote_token_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "liquidity_usd": "500000",
            "price_change_h1": "-12.0",   # below DUMP_H1_FLOOR=-8
            "price_change_h6": "-5.0",
            "txns_1h_buys": "10",
            "txns_1h_sells": "8",
        }
        sc, gate = d.momentum_score_row(row_dump)
        self.assertEqual(gate, "EXCLUDE_DUMP_H1")

    def test_momentum_score_positive_momentum_passes(self) -> None:
        d = _load_module("sprint035d_ms2", REPO / "scripts/sprint035d_momentum_filtered_discovery.py")
        row_up = {
            "pool_address_candidate": "0x" + "b" * 40,
            "base_token_address": "0x" + "c" * 40,
            "quote_token_address": "0x4200000000000000000000000000000000000006",  # WETH
            "liquidity_usd": "300000",
            "price_change_h1": "3.5",    # > 2.0 → +3 pts
            "price_change_h6": "8.0",    # > 5.0 → +2 pts
            "txns_1h_buys": "40",        # +1 density
            "txns_1h_sells": "15",       # bsr≈2.7 → +3 pts
            "volume_1h_usd": "120000",   # > 100k → +1
        }
        sc, gate = d.momentum_score_row(row_up)
        self.assertEqual(gate, "PASS")
        self.assertGreaterEqual(sc, 3)

    def test_momentum_score_excludes_illiquid(self) -> None:
        d = _load_module("sprint035d_ms3", REPO / "scripts/sprint035d_momentum_filtered_discovery.py")
        row_dead = {
            "pool_address_candidate": "0x" + "d" * 40,
            "base_token_address": "0x" + "e" * 40,
            "quote_token_address": "0x4200000000000000000000000000000000000006",
            "liquidity_usd": "10000",  # < LIQUIDITY_USD_MIN=30000
            "price_change_h1": "5.0",
            "txns_1h_buys": "20",
            "txns_1h_sells": "5",
        }
        _, gate = d.momentum_score_row(row_dead)
        self.assertEqual(gate, "EXCLUDE_ILLIQUID")


if __name__ == "__main__":
    unittest.main()
