"""Offline regression tests for the honeypot/rug defenses (no chain, no money).

Covers:
  F (cluster blacklist)  — bytecode-skeleton clone detection on the REAL
                           PARADAENGI incident bytecode; seed + record + check.
  ① deep-pool floor      — WETH-depth gate accept/reject/disable.
  abandon → blacklist     — abandoning a position feeds the cluster blacklist so
                           the next repeat from the same source is blocked.

All inputs are fixtures / fakes; nothing touches the network.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services" / "executor" / "src"
FIXTURE = ROOT / "tests" / "fixtures" / "paradaengi_bytecode.json"


def _stub_web3() -> None:
    """Minimal web3 stub: Web3.to_checksum_address + bytes helpers used by the
    modules under test. No network."""
    if "web3" in sys.modules:
        return
    web3_m = types.ModuleType("web3")

    class Web3:
        @staticmethod
        def to_checksum_address(a):
            return a

        @staticmethod
        def HTTPProvider(*_a, **_k):
            return None

        def __init__(self, *_a, **_k):
            pass

    web3_m.Web3 = Web3
    sys.modules["web3"] = web3_m
    wt = types.ModuleType("web3.types")
    wt.TxReceipt = dict
    sys.modules["web3.types"] = wt


_MAIN = None  # executor main module, loaded once with all real sibling modules


def _load_main():
    """Load executor main.py once (with eth_abi/web3/eth_account stubbed via the
    dry-run test loader). This imports the REAL swap/external_risk/preflight_trade/
    cluster_blacklist so tests exercise production code paths, not fakes."""
    global _MAIN
    if _MAIN is not None:
        return _MAIN
    # Prefer the REAL web3/eth_* (installed in the venv) so tx_sender can subclass
    # Web3.HTTPProvider; only then fall back to the dry-run loader's stubs.
    for _dep in ("web3", "web3.types", "eth_abi", "eth_account"):
        try:
            __import__(_dep)
        except Exception:
            pass
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "executor_main_dryrun_test", ROOT / "tests" / "test_executor_main_dryrun.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MAIN = mod.load_executor_main()
    _MAIN._dryrun_test_mod = mod  # keep FakeRedis accessible
    return _MAIN


def _load_cluster_blacklist():
    _load_main()
    return sys.modules["cluster_blacklist"]


class FakeSetRedis:
    """Just enough Redis: SET membership + hashes, in-memory."""

    def __init__(self):
        self.sets: dict = {}
        self.hashes: dict = {}

    def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def hset(self, key, mapping=None, **kwargs):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        h.update(kwargs)


class FakeEth:
    def __init__(self, code_map):
        self._code = code_map  # addr(lower) -> bytes
        self._bal = {}         # (token, holder) -> int

    def get_code(self, addr):
        return self._code.get(str(addr).lower(), b"")

    def set_balance(self, token, holder, amount):
        self._bal[(str(token).lower(), str(holder).lower())] = amount


class FakeW3:
    def __init__(self, code_map=None):
        self.eth = FakeEth(code_map or {})


def _bytecode_fixture():
    raw = json.loads(FIXTURE.read_text())
    return {k: bytes.fromhex(v[2:]) for k, v in raw.items() if not k.startswith("_")}


class _Pool:
    """Stand-in for PoolInfo (only the fields the gates touch)."""

    def __init__(self, pool_address, non_weth_token):
        self.pool_address = pool_address
        self.non_weth_token = non_weth_token


class TestSkeletonNormalization(unittest.TestCase):
    def test_push_operands_masked(self):
        cb = _load_cluster_blacklist()
        # Same opcode structure, different PUSH1 operand -> identical skeleton.
        a = bytes([0x60, 0x11, 0x60, 0x00, 0x55])  # PUSH1 0x11; PUSH1 0x00; SSTORE
        b = bytes([0x60, 0xAB, 0x60, 0xCD, 0x55])  # PUSH1 0xAB; PUSH1 0xCD; SSTORE
        self.assertEqual(cb.skeleton_hash_from_code(a), cb.skeleton_hash_from_code(b))
        # Different structure -> different skeleton.
        c = bytes([0x60, 0x11, 0x01, 0x55])        # PUSH1; ADD; SSTORE
        self.assertNotEqual(cb.skeleton_hash_from_code(a), cb.skeleton_hash_from_code(c))

    def test_paradaengi_clones_share_skeleton_but_not_full(self):
        cb = _load_cluster_blacklist()
        fx = _bytecode_fixture()
        from eth_utils import keccak
        # Full bytecode differs (embedded constants) but skeleton is identical.
        self.assertNotEqual(keccak(fx["token_A"]).hex(), keccak(fx["token_B"]).hex())
        self.assertEqual(
            cb.skeleton_hash_from_code(fx["token_A"]),
            cb.skeleton_hash_from_code(fx["token_B"]),
        )
        # And it equals the seeded constant.
        self.assertEqual(cb.skeleton_hash_from_code(fx["token_A"]), cb.PARADAENGI_SKELETON)
        # Clean tokens are distinct (no false collision).
        self.assertNotEqual(cb.skeleton_hash_from_code(fx["weth"]), cb.PARADAENGI_SKELETON)
        self.assertNotEqual(cb.skeleton_hash_from_code(fx["usdc"]), cb.PARADAENGI_SKELETON)


class TestClusterBlacklist(unittest.TestCase):
    def setUp(self):
        self.cb = _load_cluster_blacklist()
        self.fx = _bytecode_fixture()
        self.r = FakeSetRedis()
        self.w3 = FakeW3({
            "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38": self.fx["token_A"],
            "0x56c6ee3498ca94b7cd2fc363e465b75828f12738": self.fx["token_B"],
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": self.fx["usdc"],
        })

    def test_second_paradaengi_blocked_after_seed(self):
        # Seed deployer-only (skeleton dropped — FP risk); block via known EOA.
        self.cb.seed_paradaengi(self.r)
        pool_b = _Pool("0x2fd5596d9c57d7d8670202c204f56b3e166b5160",
                       "0x56c6ee3498ca94b7cd2fc363e465b75828f12738")
        blocked, reason = self.cb.is_blacklisted(
            self.r, self.w3, pool_b, deployer="0x715c98cedbd0c242e257986535bbf6e1fba764be")
        self.assertTrue(blocked, "2nd PARADAENGI (same deployer) must be blocked pre-entry")
        self.assertIn("deployer_blacklist", reason)

    def test_safe_rug_skeletons_seeded(self):
        n = self.cb.seed_safe_rug_skeletons(self.r)
        self.assertEqual(n, 33)
        skels = self.r.sets.get(self.cb.BYTECODE_SET, set())
        self.assertEqual(len(skels), 33)
        self.assertNotIn(self.cb.PARADAENGI_SKELETON, skels)

    def test_safe_rug_deployers_seeded(self):
        n = self.cb.seed_safe_rug_deployers(self.r)
        self.assertGreaterEqual(n, 200)
        deps = self.r.sets.get(self.cb.DEPLOYER_SET, set())
        self.assertGreaterEqual(len(deps), n)

    def test_seeded_deployer_blocked(self):
        self.cb.seed_paradaengi(self.r)
        pool = _Pool("0xdead", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
        blocked, reason = self.cb.is_blacklisted(
            self.r, self.w3, pool, deployer="0x715c98cedbd0c242e257986535bbf6e1fba764be")
        self.assertTrue(blocked)
        self.assertIn("deployer_blacklist", reason)

    def test_clean_token_passes(self):
        self.cb.seed_paradaengi(self.r)
        pool = _Pool("0xclean", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
        blocked, _ = self.cb.is_blacklisted(self.r, self.w3, pool)
        self.assertFalse(blocked)

    def test_record_then_block(self):
        # Abandon pool A -> its skeleton recorded -> the clone B is then blocked.
        pool_a = _Pool("0x7f72018c642630a998e9d63b87d356d7fc63d93d",
                       "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38")
        rec = self.cb.record(self.r, self.w3, pool_a, "abandon:postbuy_escape:reverted")
        self.assertEqual(rec["bytecode"], self.cb.PARADAENGI_SKELETON)
        pool_b = _Pool("0x2fd5596d9c57d7d8670202c204f56b3e166b5160",
                       "0x56c6ee3498ca94b7cd2fc363e465b75828f12738")
        blocked, _ = self.cb.is_blacklisted(self.r, self.w3, pool_b)
        self.assertTrue(blocked)

    def test_fail_open_no_redis(self):
        pool = _Pool("0x1", "0x2")
        blocked, reason = self.cb.is_blacklisted(None, self.w3, pool)
        self.assertFalse(blocked)
        self.assertEqual(reason, "no_redis")


class TestDepthGate(unittest.TestCase):
    """① deep-pool floor — exercise the gate logic directly via a fake w3."""

    def _load_preflight(self, min_weth):
        _load_main()
        pf = sys.modules["preflight_trade"]
        pf.MIN_POOL_WETH_ETH = float(min_weth)  # override env-derived default
        return pf

    def test_reject_shallow_accept_deep(self):
        pf = self._load_preflight(3.0)
        pool = _Pool("0xpool", "0xtok")
        w3 = FakeW3()
        # Shallow: 1.2 ETH < 3 -> reject
        pf._get_token_balance = lambda _w, _t, _p: int(1.2e18)
        res = pf.pool_depth_gate(w3, pool, min_weth_eth=3.0)
        self.assertFalse(res.ok)
        self.assertIn("shallow_pool", res.reason)
        # Deep: 5 ETH >= 3 -> accept
        pf._get_token_balance = lambda _w, _t, _p: int(5e18)
        res = pf.pool_depth_gate(w3, pool, min_weth_eth=3.0)
        self.assertTrue(res.ok)

    def test_disabled_when_zero(self):
        pf = self._load_preflight(0)
        pool = _Pool("0xpool", "0xtok")
        res = pf.pool_depth_gate(FakeW3(), pool, min_weth_eth=0)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, "depth_gate_disabled")

    def test_read_fail_open_by_default(self):
        pf = self._load_preflight(3.0)
        pool = _Pool("0xpool", "0xtok")

        def _boom(*_a, **_k):
            raise RuntimeError("rpc down")

        pf._get_token_balance = _boom
        res = pf.pool_depth_gate(FakeW3(), pool, min_weth_eth=3.0)
        self.assertTrue(res.ok)  # fail-open
        self.assertEqual(res.reason, "depth_read_skip")


class TestPostBuyEscapeE(unittest.TestCase):
    """③ E — post-buy delayed recheck: a minute-level LP pull inside the window
    must ABANDON fast + blacklist the cluster + fire a CRITICAL alert."""

    @classmethod
    def setUpClass(cls):
        cls.m = _load_main()
        cls.FakeRedis = cls.m._dryrun_test_mod.FakeRedis
        # The dry-run FakePipeline only models a few ops; add srem/expire so the
        # real PositionStore.close_position / clear_pending_buy work here.
        FP = cls.m._dryrun_test_mod.FakePipeline
        if not hasattr(FP, "srem"):
            def _srem(self, key, *members):
                for mb in members:
                    self.ops.append(("srem", (key, mb), {}))
                return self
            FP.srem = _srem
        if not hasattr(FP, "expire"):
            FP.expire = lambda self, *a, **k: self
        if not hasattr(cls.FakeRedis, "expire"):
            cls.FakeRedis.expire = lambda self, *a, **k: None

    def test_postbuy_cadence_is_tighter_than_steady_state(self):
        m = self.m
        self.assertGreater(m.POSTBUY_RECHECK_WINDOW_SEC, 0)
        self.assertLess(m.POSTBUY_RECHECK_MS, m._REALGAS_RECHECK_MS,
                        "post-buy probe must be tighter than the 60s steady-state cadence")

    def test_lp_pull_triggers_abandon_blacklist_and_alert(self):
        import time as _t
        m = self.m
        fx = _bytecode_fixture()
        tokenA = "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38"
        weth = "0x4200000000000000000000000000000000000006"
        pool_addr = "0x7f72018c642630a998e9d63b87d356d7fc63d93d"

        fake_r = self.FakeRedis()
        # Wire the cluster-blacklist redis handle (records go here on abandon).
        m.set_redis(fake_r)

        store = m.PositionStore(fake_r)
        now_ms = int(_t.time() * 1000)
        from position import OpenPosition
        pos = OpenPosition(
            pool_address=pool_addr, pool_type="v3", token_address=tokenA,
            token_decimals=18, token_amount=10**18,
            entry_eth_wei=int(0.057e18), entry_quote_eth_wei=int(0.057e18),
            opened_at_ms=now_ms - 8000,  # 8s old: inside post-buy window
            buy_tx_hash="0x" + "1" * 64, peak_eth_value=int(0.057e18), fee_tier=30,
            size_usd=25.0, token0_address=weth, token0_symbol="WETH", token0_decimals=18,
            token1_address=tokenA, token1_symbol="PARADAENGI", token1_decimals=18,
        )
        store.open_position(pos)

        from registry import PoolInfo, UNISWAP_V3_ROUTER
        pinfo = PoolInfo(pool_address=pool_addr, pool_type="v3", router=UNISWAP_V3_ROUTER,
                         token0=weth, token0_symbol="WETH", token0_decimals=18,
                         token1=tokenA, token1_symbol="PARADAENGI", token1_decimals=18, fee_bps=30)

        w3 = FakeW3({tokenA: fx["token_A"]})  # get_code(token) -> real PARADAENGI bytecode
        account = MagicMock()
        account.address = "0x" + "ab" * 20

        cfg = MagicMock()
        cfg.dry_run = False
        cfg.live_trading_mode = True
        cfg.panic_key = "SYSTEM_PANIC"

        alerts = []
        from unittest.mock import patch
        with patch.object(m, "lookup", return_value=pinfo), \
             patch.object(m, "check_honeypot", return_value=False), \
             patch.object(m, "simulate_real_sell", return_value=(False, "reverted_lp_pulled")), \
             patch.object(m, "quote_sell_eth_wei", return_value=0), \
             patch.object(m, "_pool_weth_reserve_wei", return_value=int(0.05e18)), \
             patch.object(m, "send_telegram", side_effect=lambda msg: alerts.append(msg)):
            m._monitor_once(w3, account, fake_r, store, cfg, {"REAL_TX_SENT": False})

        # Position abandoned (no longer open).
        self.assertFalse(store.has_open(pool_addr), "LP-pulled position must be abandoned")
        # Cluster blacklisted by skeleton hash.
        self.assertTrue(fake_r.sismember("executor:blacklist:bytecode",
                                         _load_cluster_blacklist().PARADAENGI_SKELETON),
                        "abandon must blacklist the bytecode cluster")
        # A CRITICAL alert mentioning the escape reason fired.
        joined = "\n".join(alerts)
        self.assertIn("CRITICAL", joined)
        self.assertIn("postbuy_escape", joined)


class TestExecQualityInstrumentation(unittest.TestCase):
    """0.5 execution-quality telemetry: deviation/delay/gas + explicit minOut."""

    @classmethod
    def setUpClass(cls):
        cls.m = _load_main()
        cls.FakeRedis = cls.m._dryrun_test_mod.FakeRedis
        FP = cls.m._dryrun_test_mod.FakePipeline
        if not hasattr(FP, "srem"):
            def _srem(self, key, *members):
                for mb in members:
                    self.ops.append(("srem", (key, mb), {}))
                return self
            FP.srem = _srem
        if not hasattr(FP, "expire"):
            FP.expire = lambda self, *a, **k: self
        if not hasattr(cls.FakeRedis, "expire"):
            cls.FakeRedis.expire = lambda self, *a, **k: None

    def test_parse_signal_enqueue_ms(self):
        self.assertEqual(self.m._parse_signal_enqueue_ms("1718000000000-0"), 1718000000000)
        self.assertIsNone(self.m._parse_signal_enqueue_ms(None))
        self.assertIsNone(self.m._parse_signal_enqueue_ms("not-an-id"))

    def test_swapresult_has_instrumentation_fields(self):
        from swap import SwapResult
        sr = SwapResult(success=True, tx_hash="0x1", amount_in=1, amount_out=2,
                        gas_used=3, gas_cost_wei=4)
        self.assertIsNone(sr.expected_out)   # backward-compatible default
        self.assertIsNone(sr.block_number)
        sr2 = SwapResult(success=True, tx_hash="0x1", amount_in=1, amount_out=2,
                         gas_used=3, gas_cost_wei=4, expected_out=100, block_number=42)
        self.assertEqual((sr2.expected_out, sr2.block_number), (100, 42))

    def test_buy_minout_has_explicit_cap_no_infinite_slippage(self):
        import swap
        # Default buy tolerance = 3% -> minOut = expected * 0.97 (never 0/unbounded).
        self.assertEqual(swap._SLIPPAGE_TOLERANCE["buy"], 0.03)
        self.assertEqual(swap._compute_min_amount_out(1000, "buy"), 970)
        # Non-emergency quote failure fails closed (minOut floor = 1, not 0).
        self.assertEqual(swap._compute_min_amount_out(0, "buy"), 1)
        self.assertEqual(swap._compute_min_amount_out(0, "take_profit"), 1)
        # Emergency sells may pass 0 to escape a collapsing position.
        self.assertEqual(swap._compute_min_amount_out(0, "hard_stop"), 0)

    def test_exec_quality_deviation_delay_gas_and_xadd(self):
        m = self.m
        r = self.FakeRedis()
        pinfo = _Pool("0xpoolabc", "0xtok")
        pinfo.non_weth_symbol = "FOO"
        # actual < expected -> negative slippage; 4s + 2 blocks delay; gas $X.
        txt = m._record_exec_quality(
            r, kind="buy", pool_info=pinfo,
            expected_out=1000, actual_out=950,
            signal_ms=1_000_000, fill_ms=1_004_000,
            sig_block=100, fill_block=102,
            reverted=False, gas_used=120000, gas_cost_wei=int(0.0003e18),
            eth_price=1658.0, tx_hash="0xabc", size_usd=50.0,
        )
        # Telegram text block sanity.
        self.assertIn("-5.00%", txt)        # (950-1000)/1000
        self.assertIn("4.0s/2blk", txt)
        self.assertIn("revert=N", txt)
        # Persisted to the exec:quality stream.
        rec = r.streams.get("exec:quality")
        self.assertTrue(rec, "exec_quality must be XADDed")
        payload = json.loads(rec[-1]["json"])
        self.assertEqual(payload["deviation_pct"], -5.0)
        self.assertEqual(payload["delay_sec"], 4.0)
        self.assertEqual(payload["delay_blocks"], 2)
        self.assertAlmostEqual(payload["gas_usd"], 0.0003 * 1658.0, places=3)
        self.assertEqual(payload["reverted"], False)

    def test_sell_side_deviation_recorded_in_finalize_close(self):
        from unittest.mock import patch
        m = self.m
        r = self.FakeRedis()
        store = m.PositionStore(r)
        from position import OpenPosition
        store.open_position(OpenPosition(
            pool_address="0xp", pool_type="v3", token_address="0xt",
            token_decimals=18, token_amount=1, entry_eth_wei=int(0.03e18),
            entry_quote_eth_wei=int(0.03e18), opened_at_ms=1, buy_tx_hash="0x"))
        from registry import PoolInfo, UNISWAP_V3_ROUTER
        weth = "0x4200000000000000000000000000000000000006"
        pinfo = PoolInfo(pool_address="0xp", pool_type="v3", router=UNISWAP_V3_ROUTER,
                         token0=weth, token0_symbol="WETH", token0_decimals=18,
                         token1="0xt", token1_symbol="FOO", token1_decimals=18, fee_bps=30)
        alerts = []
        with patch.object(m, "send_telegram", side_effect=lambda msg: alerts.append(msg)):
            # Realized 0.0285 ETH vs QuoterV2 sell quote 0.03 ETH -> -5% slip.
            m._finalize_close(
                store, "0xp", int(0.03e18), int(0.0285e18), "trailing", False, "FOO", "0xsell",
                r=r, pool_info=pinfo, expected_out=int(0.03e18),
                gas_used=100000, gas_cost_wei=int(0.0002e18), fill_block=200, eth_price=1658.0)
        rec = json.loads(r.streams["exec:quality"][-1]["json"])
        self.assertEqual(rec["kind"], "sell")
        self.assertEqual(rec["deviation_pct"], -5.0)
        self.assertEqual(rec["reverted"], False)
        joined = "\n".join(alerts)
        self.assertIn("Keystone SELL", joined)
        self.assertIn("slip(quote→fill)=-5.00%", joined)

    def test_blind_panic_sell_records_na_deviation(self):
        from unittest.mock import patch
        m = self.m
        r = self.FakeRedis()
        store = m.PositionStore(r)
        from position import OpenPosition
        store.open_position(OpenPosition(
            pool_address="0xq", pool_type="v3", token_address="0xt",
            token_decimals=18, token_amount=1, entry_eth_wei=int(0.03e18),
            entry_quote_eth_wei=int(0.03e18), opened_at_ms=1, buy_tx_hash="0x"))
        with patch.object(m, "send_telegram", side_effect=lambda _m: None):
            # Blind panic: no reliable quote -> expected_out=None -> deviation n/a.
            m._finalize_close(
                store, "0xq", int(0.03e18), int(0.03e18), "blind_mark_panic", False, "FOO", "0xs",
                r=r, pool_info=None, expected_out=None, eth_price=1658.0)
        rec = json.loads(r.streams["exec:quality"][-1]["json"])
        self.assertIsNone(rec["deviation_pct"])

    def test_exec_quality_handles_missing_quote_gracefully(self):
        m = self.m
        r = self.FakeRedis()
        pinfo = _Pool("0xpool", "0xtok")
        pinfo.non_weth_symbol = "BAR"
        txt = m._record_exec_quality(
            r, kind="buy", pool_info=pinfo,
            expected_out=None, actual_out=0,
            signal_ms=None, fill_ms=None, sig_block=None, fill_block=None,
            reverted=False, gas_used=0, gas_cost_wei=0,
            eth_price=1658.0, tx_hash="0x0", size_usd=50.0,
        )
        self.assertIn("slip(quote→fill)=n/a", txt)
        self.assertIn("delay=n/a", txt)
        payload = json.loads(r.streams["exec:quality"][-1]["json"])
        self.assertIsNone(payload["deviation_pct"])
        self.assertIsNone(payload["delay_sec"])


if __name__ == "__main__":
    unittest.main()
