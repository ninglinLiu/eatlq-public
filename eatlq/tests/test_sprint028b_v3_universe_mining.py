"""Static contract tests for scripts/sprint028b_v3_universe_mining.py.

Safe for CI: no Ethereum RPC, no Redis, no training. Offline fixture uses --offline-fixture.
"""

from __future__ import annotations

import ast
import csv
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sprint028b_v3_universe_mining.py"

FORBIDDEN_REDIS_ATTRS = frozenset({
    "xadd", "set", "setex", "delete", "hset", "zadd", "publish",
})

FORBIDDEN_IMPORTS = frozenset({"redis", "web3", "eth_account"})


def _load_ast() -> ast.Module:
    return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH))


def _attr_call_names(tree: ast.Module) -> frozenset:
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def _imported_modules(tree: ast.Module) -> set:
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


class Sprint028BMiningContractTests(unittest.TestCase):

    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), msg=f"Missing: {SCRIPT_PATH}")

    def test_no_redis_primitives_in_ast(self) -> None:
        tree = _load_ast()
        called = _attr_call_names(tree)
        offenders = sorted(called & FORBIDDEN_REDIS_ATTRS)
        self.assertEqual(offenders, [], msg=f"Unexpected Redis-like calls: {offenders}")

    def test_no_forbidden_imports(self) -> None:
        tree = _load_ast()
        imported = _imported_modules(tree)
        offenders = sorted(imported & FORBIDDEN_IMPORTS)
        self.assertEqual(offenders, [], msg=f"Forbidden imports: {offenders}")

    def test_windows_match_sprint027c_family(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_m028b", SCRIPT_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(set(mod.WINDOWS_SEC), {60, 180, 300, 900, 1800})

    def test_decision_flags_charter(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_m028b2", SCRIPT_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        f = mod.DECISION_FLAGS
        self.assertTrue(f.get("FAILED_NO_7D_ALPHA_ACTIVE"))
        self.assertFalse(f.get("TRAINING_ALLOWED"))
        self.assertFalse(f.get("LABELING_ALLOWED"))
        self.assertFalse(f.get("LIVE_ALLOWED"))
        self.assertFalse(f.get("PHASE6_ALLOWED"))
        self.assertFalse(f.get("H12_ABLATION_ALLOWED"))
        self.assertTrue(f.get("ACTIVE_UNIVERSE_MINING_ALLOWED"))
        self.assertTrue(f.get("SCOPED_MICRO_ALPHA_CHARTER_ALLOWED"))
        self.assertTrue(f.get("MICRO_LIVE_CALIBRATION_CHARTER_ALLOWED"))
        self.assertFalse(f.get("MICRO_LIVE_EXECUTION_ALLOWED"))
        self.assertFalse(f.get("SMART_MONEY_GENERAL_LINE_OPEN"))
        self.assertTrue(f.get("SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC"))

    def test_dry_run_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIn("decision_flags", data)
        self.assertTrue(data.get("clickhouse_required_for_run"))
        self.assertIn("eatlq.discovered_pools", str(data.get("ch_tables")))

    def test_blocked_missing_clickhouse_without_fixture(self) -> None:
        td = ROOT / ".tmp_sprint028b_ch_block"
        td.mkdir(exist_ok=True)
        j = td / "cov.json"
        m = td / "rep.md"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "CLICKHOUSE_URL": "",
                "SPRINT028B_OUT_JSON": str(j),
                "SPRINT028B_OUT_MD": str(m),
            },
        )
        self.assertEqual(proc.returncode, 5, msg=proc.stdout + proc.stderr)
        self.assertIn("BLOCKED_BY_MISSING_CLICKHOUSE_URL", proc.stderr)
        fj = json.loads(j.read_text(encoding="utf-8"))
        self.assertEqual(fj.get("universe_gate_verdict"), "BLOCKED_BY_MISSING_CLICKHOUSE_URL")

    def test_rescope_grid_rejects_offline_fixture(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--rescope-grid",
                "--offline-fixture",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 7, msg=proc.stdout + proc.stderr)
        self.assertIn("BLOCKED_PRECHECK", proc.stderr)
        self.assertIn("--rescope-grid", proc.stderr)

    def test_offline_fixture_pass(self) -> None:
        """Synthetic dashcam: 32 V3 pools × 35 buckets outside a synthetic top-5."""
        work = ROOT / ".tmp_sprint028b_fixture"
        work.mkdir(exist_ok=True)
        join_p = work / "join.csv"
        dash_p = work / "dashcam.csv"
        out_csv = work / "out.csv"
        out_json = work / "out.json"
        out_md = work / "out.md"

        with join_p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pool_address"])
            for i in range(5):
                pa = f"0x{'%040x' % i}"
                for _ in range(12):
                    w.writerow([pa])

        base_ts = 1_700_000_000_000
        with dash_p.open("w", newline="", encoding="utf-8") as f:
            fields = [
                "timestamp",
                "pool_address",
                "pool_type",
                "momentum_val",
                "whale_val",
                "activity_val",
                "risk_val",
                "dex",
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for n in range(32):
                pa = f"0x{'%040x' % (0x100 + n)}"
                for j in range(35):
                    w.writerow(
                        {
                            "timestamp": str(base_ts + n * 50_000_000 + j * 310_000),
                            "pool_address": pa,
                            "pool_type": "v3",
                            "momentum_val": "0.1",
                            "whale_val": "0.2",
                            "activity_val": "0.3",
                            "risk_val": "0.0",
                            "dex": "uni",
                        }
                    )

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--offline-fixture",
                "--max-scan-lines",
                "50000",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "CLICKHOUSE_URL": "",
                "DASHCAM_PATH": str(dash_p),
                "H108_JOIN_SAMPLE": str(join_p),
                "SPRINT028B_OUT_CSV": str(out_csv),
                "SPRINT028B_OUT_JSON": str(out_json),
                "SPRINT028B_OUT_MD": str(out_md),
                "SPRINT028B_MAX_OUTPUT_ROWS": "5000",
            },
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        summ = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(summ.get("universe_gate_verdict"), "PASS")
        self.assertTrue(summ.get("sprint028c_can_start"))
        m = summ["concentration_post_cap"]
        self.assertGreaterEqual(m["distinct_pools"], 20)
        self.assertGreaterEqual(m["pools_with_gte_30_rows"], 20)


if __name__ == "__main__":
    unittest.main()
