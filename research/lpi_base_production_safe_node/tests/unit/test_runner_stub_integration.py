from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.errors import AccessDeniedError
from launchpad_intelligence.health.command_executor import ExternalCommandExecutor
from launchpad_intelligence.query.metadata_inventory import (
    MetadataInventoryPlan,
    build_default_schema_inventory_plans,
    build_health_system_plans,
)
from launchpad_intelligence.query.runner_status import RunnerExitCode, derive_round_status
from launchpad_intelligence.security.execution_seal import (
    DEFAULT_CRITICAL_PATHS,
    assert_seal_unchanged,
    register_run_owned_outputs,
    require_clean_sealed_worktree,
)

ROOT = Path("/root/lpi_base_production_safe_node")


def _load_runner():
    path = ROOT / "scripts" / "run_metadata_inventory.py"
    spec = importlib.util.spec_from_file_location("run_metadata_inventory_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_executor() -> ExternalCommandExecutor:
    def stub(argv: list[str]):
        joined = " ".join(argv)
        # docker inspect
        if "docker" in argv and "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running 0\n", "")
        # redis
        if "XINFO" in argv:
            return subprocess.CompletedProcess(argv, 0, "name\nc1\npending\n0\n", "")
        # clickhouse health/schema
        if "clickhouse-client" in joined:
            sql = ""
            if "--query" in argv:
                sql = argv[argv.index("--query") + 1]
            if "system.processes" in sql:
                return subprocess.CompletedProcess(
                    argv, 0, "query_id\telapsed\tread_rows\tmemory_usage\nq1\t0.1\t0\t0\n", ""
                )
            if "system.merges" in sql:
                # after marker distinguished by query_id containing health_merges_after in command_id — stub always empty then one row for second call
                return subprocess.CompletedProcess(
                    argv, 0, "database\ttable\telapsed\tprogress\tnum_parts\n", ""
                )
            if "system.databases" in sql:
                return subprocess.CompletedProcess(argv, 0, "name\tengine\neatlq\tAtomic\n", "")
            if "system.columns" in sql and "table = 'tables'" in sql:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "database\ttable\tname\ttype\tposition\n"
                    "system\ttables\tdatabase\tString\t1\n"
                    "system\ttables\tname\tString\t2\n"
                    "system\ttables\tengine\tString\t3\n"
                    "system\ttables\tpartition_key\tString\t4\n",
                    "",
                )
            if "system.columns" in sql and "table = 'columns'" in sql:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "database\ttable\tname\ttype\tposition\n"
                    "system\tcolumns\tdatabase\tString\t1\n"
                    "system\tcolumns\ttable\tString\t2\n"
                    "system\tcolumns\tname\tString\t3\n"
                    "system\tcolumns\ttype\tString\t4\n",
                    "",
                )
            if "FROM system.tables" in sql:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "database\tname\tengine\tpartition_key\tsorting_key\tprimary_key\tsampling_key\tstorage_policy\n"
                    "eatlq\traw_evm_logs\tMergeTree\ttoYYYYMM(received_at_ms)\tchain_id, received_at_ms\tchain_id, received_at_ms\t\tdefault\n",
                    "",
                )
            if "FROM system.columns" in sql and "database = 'eatlq'" in sql:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "database\ttable\tname\ttype\tposition\tdefault_kind\tdefault_expression\t"
                    "is_in_partition_key\tis_in_sorting_key\tis_in_primary_key\tis_in_sampling_key\n"
                    "eatlq\traw_evm_logs\tblock_number\tUInt64\t1\t\t\t0\t0\t0\t0\n"
                    "eatlq\traw_evm_logs\tlog_index\tUInt32\t2\t\t\t0\t0\t0\t0\n"
                    "eatlq\traw_evm_logs\taddress\tString\t3\t\t\t0\t0\t0\t0\n"
                    "eatlq\traw_evm_logs\ttopic0\tString\t4\t\t\t0\t0\t0\t0\n"
                    "eatlq\traw_evm_logs\tdata\tString\t5\t\t\t0\t0\t0\t0\n",
                    "",
                )
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    return ExternalCommandExecutor(stub=stub, source_commit="abc", source_tree_hash="def", authorized_capability="metadata_schema_inventory")


class RunnerStubIntegrationTests(unittest.TestCase):
    def test_all_production_commands_go_through_executor(self) -> None:
        mod = _load_runner()
        ex = _stub_executor()
        before = ex.call_count
        mod.docker_inspect_containers(("eatlq-clickhouse-1",), ex)
        mod.redis_executor_pending(ex)
        plans = build_health_system_plans(execution_authorized=True)
        mod.clickhouse_query(plans[0], self_query_marker="t", executor=ex)
        self.assertGreaterEqual(ex.call_count - before, 3)
        classes = {r.command_class for r in ex.records}
        self.assertIn("docker_inspect", classes)
        self.assertIn("redis_health", classes)
        self.assertIn("clickhouse_system", classes)

    def test_pre_health_blocked_schema_calls_zero(self) -> None:
        # Control-flow: when pre-health not SAFE, schema_succeeded stays 0 and exit=10.
        status, code, state = derive_round_status(pre_health_status="BLOCKED_LIVE_PRIORITY")
        self.assertEqual(int(code), 10)
        self.assertFalse(state.any_schema_query_executed)

    def test_post_health_blocked_exit_30_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # minimal git repo + critical paths for seal
            subprocess.check_call(["git", "init"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "t@e.com"], cwd=root)
            subprocess.check_call(["git", "config", "user.name", "t"], cwd=root)
            (root / "evidence" / ".keep").parent.mkdir(parents=True)
            (root / "evidence" / ".keep").write_text("k\n")
            for rel in DEFAULT_CRITICAL_PATHS:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x\n")
            subprocess.check_call(["git", "add", "-A"], cwd=root, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "commit", "-m", "i"], cwd=root, stdout=subprocess.DEVNULL)

            seal = require_clean_sealed_worktree(root)
            ev = root / "evidence" / "base_metadata_inventory_stub"
            results = ev / "query_results"
            results.mkdir(parents=True)
            for name in (
                "health_before_processes.tsv",
                "health_before_merges.tsv",
                "health_after_processes.tsv",
                "health_after_merges.tsv",
                "q_app_tables.tsv",
                "q_app_columns.tsv",
            ):
                (results / name).write_text("h\n", encoding="utf-8")
            seal2 = register_run_owned_outputs(seal, evidence_dir=ev)
            assert_seal_unchanged(seal2)

            # Simulate five schema successes + post blocked
            status, code, state = derive_round_status(
                pre_health_status="SAFE_TO_PROCEED",
                schema_planned=5,
                schema_succeeded=5,
                schema_complete=True,
                post_health_status="BLOCKED_LIVE_PRIORITY",
                post_collected=True,
            )
            self.assertEqual(int(code), 30)
            self.assertEqual(status.value, "SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED")
            self.assertTrue(state.next_stage_blocked)
            for name in (
                "health_before_processes.tsv",
                "health_before_merges.tsv",
                "health_after_processes.tsv",
                "health_after_merges.tsv",
            ):
                self.assertTrue((results / name).is_file())

    def test_post_health_unknown_exit_31(self) -> None:
        _, code, _ = derive_round_status(
            pre_health_status="SAFE_TO_PROCEED",
            schema_planned=5,
            schema_succeeded=5,
            schema_complete=True,
            post_health_status="BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY",
            post_collected=True,
        )
        self.assertEqual(int(code), 31)

    def test_schema_failure_not_complete(self) -> None:
        status, code, _ = derive_round_status(
            pre_health_status="SAFE_TO_PROCEED",
            schema_planned=5,
            schema_succeeded=2,
            schema_complete=False,
        )
        self.assertIn(int(code), (20, 21))
        self.assertNotEqual(status.value, "SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE")

    def test_stubbed_clickhouse_records_source_identity(self) -> None:
        mod = _load_runner()
        ex = _stub_executor()
        ex.source_commit = "cafebabe"
        ex.source_tree_hash = "deadbeef"
        plan = build_health_system_plans(execution_authorized=True)[0]
        mod.clickhouse_query(plan, self_query_marker="health", executor=ex)
        rec = ex.records[-1]
        self.assertEqual(rec.source_commit, "cafebabe")
        self.assertEqual(rec.source_tree_hash, "deadbeef")
        self.assertEqual(rec.command_class, "clickhouse_system")


if __name__ == "__main__":
    unittest.main()
