"""Tests for sprint037a_bf180_chunked_backfill.py

Validates:
  1. Script exists, no forbidden imports
  2. Block ↔ timestamp conversion accuracy
  3. PoolCreated parsing
  4. WETH meme pool filter
  5. ERC20 symbol decoding (ABI string + bytes32)
  6. raw_evm_logs row structure (TTL-safe received_at_ms = now)
  7. discovered_pools row structure
  8. CH read-only guard
  9. Chunk calendar completeness
 10. State save/load round-trip
 11. No training / live / promotion references
"""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sprint037a_bf180_chunked_backfill.py"
_WETH = "0x4200000000000000000000000000000000000006"


def _load():
    spec = importlib.util.spec_from_file_location("bf180", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists():
    assert SCRIPT.exists()


def test_no_forbidden_imports():
    src = SCRIPT.read_text()
    for bad in ("import torch", "import sklearn", "from web3", "import redis"):
        assert bad not in src, f"Forbidden import: {bad}"


def test_no_training_or_promotion():
    src = SCRIPT.read_text().lower()
    assert "model_promotion" not in src
    assert "train(" not in src
    assert "live_trading" not in src


def test_block_ts_roundtrip():
    mod = _load()
    anchor_ts = mod._ANCHOR_TS
    anchor_block = mod._ANCHOR_BLOCK
    assert mod._block_for_ts(anchor_ts) == anchor_block
    recovered_ts = mod._ts_for_block(anchor_block)
    assert abs(recovered_ts - anchor_ts) < 2


def test_block_for_date():
    mod = _load()
    # 2025-11-01 is 158 days before 2026-04-08
    ts_nov1 = mod._ts_for_date("2025-11-01")
    block = mod._block_for_ts(ts_nov1)
    expected = mod._ANCHOR_BLOCK - int(158 * 86400 / 2)
    assert abs(block - expected) < 15000  # allow ±8h of block estimation drift


def test_pool_created_parsing():
    mod = _load()
    weth_pad = "0x0000000000000000000000004200000000000000000000000000000000000006"
    token_pad = "0x000000000000000000000000aabbccdd00000000000000000000000000001234"
    fee_pad = "0x0000000000000000000000000000000000000000000000000000000000000bb8"  # 3000
    pool_data = "0x" + "00" * 32 + "000000000000000000000000deadbeef1234567890abcdef1234567890abcdef"
    log = {
        "topics": [mod._POOL_CREATED_TOPIC, weth_pad, token_pad, fee_pad],
        "data": pool_data,
        "blockNumber": "0x2A49F1C",
        "transactionHash": "0x" + "aa" * 32,
    }
    result = mod._parse_pool_created(log)
    assert result is not None
    assert result["token0"] == _WETH
    assert result["fee"] == 3000
    assert result["block_number"] == 44343068


def test_weth_meme_filter_all_fee_tiers():
    mod = _load()
    for fee in [100, 500, 3000, 10000]:
        p = {"token0": _WETH, "token1": "0xtok", "fee": fee, "pool": "0xpool"}
        assert mod._is_weth_meme_pool(p), f"should pass for fee={fee}"


def test_weth_meme_filter_rejects_non_meme_fee():
    mod = _load()
    p = {"token0": _WETH, "token1": "0xtok", "fee": 30, "pool": "0xpool"}
    assert not mod._is_weth_meme_pool(p)


def test_decode_erc20_str_dynamic():
    mod = _load()
    # ABI-encode string "PEPE"
    # offset=32, length=4, data='PEPE' padded to 32 bytes
    offset = "0000000000000000000000000000000000000000000000000000000000000020"
    length = "0000000000000000000000000000000000000000000000000000000000000004"
    data = "5045504500000000000000000000000000000000000000000000000000000000"
    hex_result = "0x" + offset + length + data
    assert mod._decode_erc20_str(hex_result) == "PEPE"


def test_decode_erc20_str_bytes32():
    mod = _load()
    # bytes32-style: "MEME" padded
    raw = b"MEME" + b"\x00" * 28
    hex_result = "0x" + raw.hex()
    result = mod._decode_erc20_str(hex_result)
    assert result == "MEME"


def test_swap_log_row_received_at_ms_is_now():
    """Key TTL constraint: received_at_ms must be NOW, not historical block_time."""
    mod = _load()
    log = {
        "blockNumber": hex(mod._ANCHOR_BLOCK - 1_000_000),  # ~23 days ago
        "logIndex": "0x1",
        "transactionHash": "0x" + "ff" * 32,
        "address": "0xpooladdr000000000000000000000000000000012",
        "topics": ["0xtop0", "0x" + "00" * 31 + "ab", "0x" + "00" * 31 + "cd"],
        "data": "0x" + "ff" * 200,
        "removed": False,
    }
    before = time.time()
    now_ms = mod._now_dt_str()
    row = mod._swap_log_to_raw_row(log, "bf180_test", now_ms)
    after = time.time()

    # block_time is historical (far in the past)
    bt = datetime.strptime(row["block_time"], "%Y-%m-%d %H:%M:%S.000").replace(
        tzinfo=timezone.utc
    ).timestamp()
    assert bt < before - 86400, "block_time should be in the past"

    # received_at_ms is current time → TTL won't expire this row for 180d
    rat = datetime.strptime(row["received_at_ms"], "%Y-%m-%d %H:%M:%S.000").replace(
        tzinfo=timezone.utc
    ).timestamp()
    # Allow 2s tolerance for string truncation (no sub-second precision in format)
    assert (before - 2) <= rat <= (after + 2), "received_at_ms must be approximately NOW"

    assert row["source"] == "bf180_test"
    assert row["chain_id"] == mod._CHAIN_ID


def test_discovered_pools_row_structure():
    mod = _load()
    parsed = {
        "token0": _WETH,
        "token1": "0xmemetoken000000000000000000000000000001",
        "fee": 10000,
        "pool": "0xpooladdr000000000000000000000000000000001",
        "block_number": 40000000,
        "tx_hash": "0x" + "bb" * 32,
    }
    sym_cache = {
        _WETH: {"symbol": "WETH", "decimals": 18},
        "0xmemetoken000000000000000000000000000001": {"symbol": "MEME", "decimals": 18},
    }
    row = mod._build_pool_row(parsed, sym_cache, "2025-11-15 00:00:00")
    assert row["chain_id"] == mod._CHAIN_ID
    assert row["pool_address"] == "0xpooladdr000000000000000000000000000000001"
    assert row["pool_type"] == "v3"
    assert row["fee_bps"] == 10000
    assert row["has_weth_leg"] == 1
    assert row["token0_symbol"] == "WETH"
    assert row["token1_symbol"] == "MEME"
    assert row["created_block"] == 40000000
    assert row["factory_address"] == mod._V3_FACTORY


def test_ch_guard_blocks_writes():
    mod = _load()
    for bad in ["INSERT INTO foo", "DROP TABLE foo", "ALTER TABLE foo"]:
        try:
            mod._ch_guard_ro(bad)
            assert False, f"Should have blocked: {bad}"
        except ValueError:
            pass


def test_ch_guard_allows_select():
    mod = _load()
    mod._ch_guard_ro("SELECT count() FROM eatlq.raw_evm_logs")
    mod._ch_guard_ro("WITH x AS (SELECT 1) SELECT * FROM x")


def test_chunk_calendar_spans_180_days():
    mod = _load()
    cal = mod._CHUNK_CALENDAR
    assert len(cal) >= 6, "Need at least 6 chunks for 180d"
    starts = [mod._ts_for_date(sd) for _, sd, _ in cal]
    ends = [mod._ts_for_date(ed) for _, _, ed in cal]
    total_days = (max(ends) - min(starts)) / 86400
    assert total_days >= 150, f"Calendar spans only {total_days:.0f} days"


def test_chunk_calendar_no_overlaps():
    mod = _load()
    cal = mod._CHUNK_CALENDAR
    for i in range(len(cal) - 1):
        _, _start, end_i = cal[i]
        _, start_next, _ = cal[i + 1]
        # end of chunk i should equal start of chunk i+1 (contiguous)
        assert end_i == start_next, (
            f"Gap between chunk {cal[i][0]} end={end_i} and {cal[i+1][0]} start={start_next}"
        )


def test_state_roundtrip(tmp_path):
    mod = _load()
    orig_state_file = mod._STATE_FILE
    mod._STATE_FILE = tmp_path / "test_state.json"
    try:
        state = {"chunks": {"2025-11": {"status": "done", "rows_inserted": 42}},
                 "token_symbol_cache": {"0xaddr": {"symbol": "TEST", "decimals": 18}}}
        mod._save_state(state)
        loaded = mod._load_state()
        assert loaded["chunks"]["2025-11"]["rows_inserted"] == 42
        assert loaded["token_symbol_cache"]["0xaddr"]["symbol"] == "TEST"
    finally:
        mod._STATE_FILE = orig_state_file


def test_source_tag_format():
    mod = _load()
    for chunk_id, _, _ in mod._CHUNK_CALENDAR:
        tag = f"bf180_{chunk_id.replace('-', '_')}"
        assert len(tag) <= 32, f"source tag too long: {tag}"
        assert " " not in tag


def pytest_approx(value, abs_tol=1e-6):
    """Simple inline approx for environments without pytest."""
    class Approx:
        def __init__(self, v, a): self.v = v; self.a = a
        def __eq__(self, other):
            import builtins
            return builtins.abs(other - self.v) <= self.a
    return Approx(value, abs_tol)
