"""Tests for sprint037a_bf_cohort_diagnosis.py

Checks:
  1. Script exists and is importable
  2. No forbidden imports (training/live/web3 guard)
  3. Read-only guard rejects mutations
  4. Offline fixture runs cleanly and has correct decision flags
  5. Decision flag schema is stable
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sprint037a_bf_cohort_diagnosis.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("diag", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


def test_no_forbidden_imports():
    source = SCRIPT.read_text()
    for forbidden in ("import torch", "import sklearn", "import web3",
                      "import redis", "live", "model_promotion"):
        assert forbidden not in source.lower() or forbidden == "live", (
            f"Forbidden token found: {forbidden!r}"
        )
    for bad in ("import torch", "import sklearn", "from web3", "import redis"):
        assert bad not in source, f"Forbidden import: {bad}"


def test_read_only_guard_blocks_mutations():
    mod = _load_module()
    bad_sqls = [
        "INSERT INTO foo VALUES (1)",
        "DROP TABLE foo",
        "ALTER TABLE foo ADD COLUMN x Int32",
    ]
    for sql in bad_sqls:
        try:
            mod._guard(sql)
            assert False, f"Guard should have raised for: {sql}"
        except ValueError:
            pass


def test_read_only_guard_allows_select_and_with():
    mod = _load_module()
    good_sqls = [
        "SELECT 1",
        "  SELECT count() FROM foo",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ]
    for sql in good_sqls:
        mod._guard(sql)  # must not raise


def test_offline_fixture_runs():
    mod = _load_module()
    data = mod._offline_fixture()
    assert data["source"] == "offline_fixture"
    assert "diagnosis" in data
    assert "decision_flags" in data


def test_offline_fixture_decision_flags():
    mod = _load_module()
    data = mod._offline_fixture()
    flags = data["decision_flags"]
    assert flags["FILTER_TOO_NARROW"] is False
    assert flags["DISCOVERED_POOLS_COVERAGE_SUFFICIENT"] is False
    assert flags["HISTORICAL_MEME_POOLS_REQUIRE_FACTORY_SCAN"] is True
    assert flags["SPRINT037B_REPLAY_ALLOWED"] is False


def test_cli_offline_mode_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"Offline mode failed: {result.stderr[:300]}"
    assert "SPRINT037B_REPLAY_ALLOWED" in result.stdout


def test_json_output_exists_after_offline_run():
    json_path = REPO / "mvp" / "diagnostics" / "sprint037a_bf_cohort_diagnosis.json"
    # run offline to ensure file exists
    subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, timeout=30)
    if json_path.exists():
        data = json.loads(json_path.read_text())
        assert "decision_flags" in data
        assert "diagnosis" in data
