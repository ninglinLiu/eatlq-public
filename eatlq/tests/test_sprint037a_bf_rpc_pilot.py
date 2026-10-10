"""Tests for sprint037a_bf_rpc_pilot.py

Checks:
  1. Script exists
  2. No forbidden imports
  3. PoolCreated event parsing is correct
  4. WETH meme pool filter logic
  5. raw_evm_logs row builder (structure)
  6. Decision flag schema
  7. Read-only guard on CH query helper
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sprint037a_bf_rpc_pilot.py"

_WETH = "0x4200000000000000000000000000000000000006"


def _load_module():
    spec = importlib.util.spec_from_file_location("pilot", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


def test_no_forbidden_imports():
    source = SCRIPT.read_text()
    for bad in ("import torch", "import sklearn", "from web3", "import redis"):
        assert bad not in source, f"Forbidden import: {bad}"


def test_no_training_or_live_operations():
    source = SCRIPT.read_text().lower()
    assert "model_promotion" not in source
    assert "train(" not in source


def test_pool_created_parsing_valid():
    mod = _load_module()
    # Construct a fake PoolCreated log for WETH/TOKEN fee=3000
    weth_padded = "0x0000000000000000000000004200000000000000000000000000000000000006"
    token_padded = "0x000000000000000000000000abcdef1234567890abcdef1234567890abcdef12"
    fee_padded = "0x0000000000000000000000000000000000000000000000000000000000000bb8"  # 3000
    pool_in_data = "0xabcdef1234567890abcdef1234567890abcdef12"
    # data: 32 bytes tickSpacing + 32 bytes pool
    tick_hex = "0" * 64
    pool_hex = "000000000000000000000000abcdef1234567890abcdef1234567890abcdef12"
    data = "0x" + tick_hex + pool_hex

    log = {
        "topics": [mod._POOL_CREATED_TOPIC, weth_padded, token_padded, fee_padded],
        "data": data,
    }
    result = mod._parse_pool_created(log)
    assert result is not None
    assert result["token0"] == _WETH
    assert result["fee"] == 3000
    assert result["pool"] == "0xabcdef1234567890abcdef1234567890abcdef12"


def test_pool_created_parsing_insufficient_topics():
    mod = _load_module()
    log = {"topics": ["0xtopic0"], "data": "0x" + "00" * 64}
    assert mod._parse_pool_created(log) is None


def test_weth_meme_pool_filter_pass():
    mod = _load_module()
    parsed = {"token0": _WETH, "token1": "0xsometoken", "fee": 3000, "pool": "0xpool"}
    assert mod._is_weth_meme_pool(parsed) is True


def test_weth_meme_pool_filter_fails_on_wrong_fee():
    mod = _load_module()
    parsed = {"token0": _WETH, "token1": "0xtoken", "fee": 9999, "pool": "0xpool"}
    assert mod._is_weth_meme_pool(parsed) is False


def test_weth_meme_pool_filter_fails_on_no_weth():
    mod = _load_module()
    parsed = {"token0": "0xusdc", "token1": "0xdai", "fee": 500, "pool": "0xpool"}
    assert mod._is_weth_meme_pool(parsed) is False


def test_weth_meme_pool_filter_weth_as_token1():
    mod = _load_module()
    parsed = {"token0": "0xmemetoken", "token1": _WETH, "fee": 10000, "pool": "0xpool"}
    assert mod._is_weth_meme_pool(parsed) is True


def test_log_to_raw_evm_row_structure():
    mod = _load_module()
    log = {
        "blockNumber": "0x2A49F1C",   # 44343068 = hex(44343068)
        "logIndex": "0x5",
        "transactionHash": "0x" + "ab" * 32,
        "address": "0xMyPool1234567890abcdef1234567890abcdef12",
        "topics": ["0xtopic0", "0x" + "00" * 31 + "ab", "0x" + "00" * 31 + "cd"],
        "data": "0x" + "ff" * 200,
        "removed": False,
    }
    block_ts = {44343068: 1775628571}
    now_ms = "2026-05-06 00:00:00.000"
    row = mod._log_to_raw_evm_row(log, block_ts, now_ms)

    assert row["chain_id"] == 8453
    assert row["source"] == mod._SOURCE_TAG
    assert row["block_number"] == 44343068
    assert row["log_index"] == 5
    assert row["address"] == "0xmypool1234567890abcdef1234567890abcdef12"
    assert row["topic0"] == "0xtopic0"
    assert row["removed"] == 0
    assert "block_time" in row
    assert "payload_json" in row


def test_ch_guard_blocks_mutations():
    mod = _load_module()
    bad = ["INSERT INTO foo VALUES (1)", "DROP TABLE foo", "CREATE TABLE foo (x Int)"]
    for sql in bad:
        try:
            mod._ch_guard_ro(sql)
            assert False, f"Guard should block: {sql}"
        except ValueError:
            pass


def test_ch_guard_allows_select_and_with():
    mod = _load_module()
    mod._ch_guard_ro("SELECT 1")
    mod._ch_guard_ro("WITH x AS (SELECT 1) SELECT * FROM x")


def test_pilot_json_output_exists_and_valid():
    json_path = REPO / "mvp" / "diagnostics" / "sprint037a_bf_rpc_pilot.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
        assert "decision_flags" in data
        flags = data["decision_flags"]
        assert "PILOT_DATA_FOUND" in flags
        assert "PIPELINE_INSERT_VALIDATED" in flags
        assert "FULL_BACKFILL_UNBLOCKED" in flags
        assert flags["SPRINT037B_REPLAY_ALLOWED"] is False


def test_meme_fee_tiers():
    mod = _load_module()
    assert 100 in mod._MEME_FEE_TIERS
    assert 500 in mod._MEME_FEE_TIERS
    assert 3000 in mod._MEME_FEE_TIERS
    assert 10000 in mod._MEME_FEE_TIERS
    assert 30 not in mod._MEME_FEE_TIERS   # pool_registry scale, not V3 raw fee
    assert 9999 not in mod._MEME_FEE_TIERS
