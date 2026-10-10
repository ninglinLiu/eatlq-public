"""Sprint035C — external discovery + manifest contracts (offline-safe)."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAND = REPO / "mvp/diagnostics/sprint035c_external_candidate_pool_summary.csv"
IDENT = REPO / "mvp/diagnostics/sprint035c_pool_identity_validation.csv"
ACT = REPO / "mvp/diagnostics/sprint035c_activity_viability_audit.csv"
MANIFEST = REPO / "mvp/diagnostics/sprint035c_external_live_active_smoke_manifest.json"
REPORT = REPO / "mvp/audits/sprint035c_external_live_active_pool_discovery_report.md"


def _load_script_shape_fn():
    p = REPO / "scripts/sprint035c_external_live_active_pool_discovery.py"
    spec = importlib.util.spec_from_file_location("sprint035c_ext", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing sprint035c script")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m.external_manifest_shape_ok


def _load_wrapper_shape_fn():
    p = REPO / "scripts/sprint030d_bounded_smoke_label.py"
    spec = importlib.util.spec_from_file_location("sprint030d_lbl", p)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sprint030d")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m.external_live_active_manifest_shape_ok


class TestSprint035cDiscovery(unittest.TestCase):
    def test_candidate_csv_schema_nonempty(self) -> None:
        self.assertTrue(CAND.is_file())
        with CAND.open(newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            rows = list(r)
        hdr = {"source", "pair_label", "pool_address_candidate", "preliminary_status"}
        self.assertTrue(hdr <= set(r.fieldnames or []))
        self.assertGreaterEqual(len(rows), 8)
        seen: set[str] = set()
        for row in rows:
            addr = row.get("pool_address_candidate", "").lower()
            self.assertTrue(addr.startswith("0x"))
            seen.add(addr)
        self.assertGreaterEqual(len(seen), 8)

    def test_identity_activity_headers(self) -> None:
        self.assertTrue(IDENT.is_file())
        self.assertTrue(ACT.is_file())
        with IDENT.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            self.assertIn("pool_address_candidate", r.fieldnames or [])
            self.assertIn("identity_status", r.fieldnames or [])
        with ACT.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            self.assertIn("pool_address_audit_key", r.fieldnames or [])
            self.assertIn("viability_status", r.fieldnames or [])

    def test_manifest_contract(self) -> None:
        self.assertTrue(MANIFEST.is_file())
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(d.get("kind"), "external_live_active_scoped_smoke_manifest")
        self.assertTrue(d.get("created_before_quote") is True)

    def test_report_exists(self) -> None:
        self.assertTrue(REPORT.is_file())

    def test_external_manifest_shape_ok_agrees(self) -> None:
        f1 = _load_script_shape_fn()
        f2 = _load_wrapper_shape_fn()
        pools = ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
        ctr = [(pools[i % 2], 0, 300) for i in range(120)]
        ok1, _w1 = f1(ctr)
        ok2, _w2 = f2(ctr)
        self.assertEqual(ok1, ok2)


if __name__ == "__main__":
    unittest.main()
