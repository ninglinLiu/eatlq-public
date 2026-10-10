from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter
from launchpad_intelligence.evidence.manifest import build_manifest
from launchpad_intelligence.errors import ContractError, PreflightError
from launchpad_intelligence.query.preflight import BoundedQueryPlan, build_preflight, validate_plan_sql_shape
from launchpad_intelligence.security.network import NetworkFailClosed
from launchpad_intelligence.security.paths import assert_under_workspace, is_under_workspace
from launchpad_intelligence.security.secrets import scan_text_for_secrets


class PreflightManifestBundleTests(unittest.TestCase):
    def test_preflight_builds_without_execution(self) -> None:
        plan = BoundedQueryPlan(
            candidate_table="logical.logs_candidate",
            explicit_columns=("block_number", "transaction_hash", "log_index", "topic0"),
            partition_key="block_number",
            start_block=10_000_000,
            end_block=10_001_000 - 1,
            limit=1000,
            dry_run=True,
            explain_only=True,
        )
        report = build_preflight(plan)
        self.assertEqual(report.status, "PREFLIGHT_READY_NOT_EXECUTED")
        self.assertFalse(report.execution_authorized)
        self.assertIn("LIMIT_1000", " ".join(report.full_scan_prevention))

    def test_preflight_rejects_select_star_and_execute_flags(self) -> None:
        with self.assertRaises(PreflightError):
            BoundedQueryPlan(
                candidate_table="logical.t",
                explicit_columns=("*",),
                partition_key="block_number",
                start_block=1,
                end_block=2,
                limit=1,
            ).validate()
        with self.assertRaises(PreflightError):
            BoundedQueryPlan(
                candidate_table="logical.t",
                explicit_columns=("block_number",),
                partition_key="block_number",
                start_block=1,
                end_block=2,
                limit=1,
                dry_run=False,
                explain_only=True,
            ).validate()

    def test_sql_shape_validator(self) -> None:
        bad = validate_plan_sql_shape("SELECT * FROM t")
        self.assertIn("select_star_forbidden", bad)

    def test_manifest_and_bundle_export(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        with tempfile.TemporaryDirectory(dir=root / "var" / "tmp") as tmp:
            staging = Path(tmp) / "stage"
            staging.mkdir()
            (staging / "reports").mkdir()
            (staging / "reports" / "note.txt").write_text("ok\n", encoding="utf-8")
            exporter = RemoteEvidenceBundleExporter(root)
            out = Path(tmp) / "bundle.tar.gz"
            manifest = exporter.export(
                staging_dir=staging,
                output_tar=out,
                bundle_id="base-test-bundle",
                source_code_commit="0" * 40,
                source_tree_hash="1" * 40,
                unknown_count=1,
                golden_results={"sample": "UNKNOWN", "kind": "synthetic_contract_case"},
            )
            self.assertTrue(out.exists())
            self.assertEqual(manifest.chain_id, 8453)
            self.assertFalse(manifest.production_state_mutated)
            self.assertGreaterEqual(manifest.file_count, 1)
            self.assertNotIn("manifest.json", manifest.sha256_per_file)

    def test_path_isolation(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        self.assertTrue(is_under_workspace(root / "export" / "x", root))
        with self.assertRaises(ContractError):
            assert_under_workspace(Path("/tmp/escape"), root)

    def test_secret_scan(self) -> None:
        hits = scan_text_for_secrets("password = 'supersecretvalue'")
        self.assertTrue(hits)
        self.assertEqual(scan_text_for_secrets("block_number=123"), [])

    def test_network_fail_closed(self) -> None:
        import socket

        with NetworkFailClosed():
            with self.assertRaises(RuntimeError):
                socket.socket()

    def test_database_fail_closed_via_defaults_json(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        defaults = json.loads((root / "config" / "defaults.json").read_text(encoding="utf-8"))
        self.assertFalse(defaults["database_access_enabled"])
        self.assertFalse(defaults["business_table_query_enabled"])

    def test_build_manifest_direct(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        with tempfile.TemporaryDirectory(dir=root / "var" / "tmp") as tmp:
            f = Path(tmp) / "a.json"
            f.write_text("{}\n", encoding="utf-8")
            m = build_manifest(
                bundle_id="x",
                source_code_commit="a" * 40,
                source_tree_hash="b" * 40,
                files={"a.json": f},
            )
            self.assertEqual(m.schema_version.startswith("lpi.schema."), True)


if __name__ == "__main__":
    unittest.main()
