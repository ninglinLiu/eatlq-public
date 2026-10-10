"""Regression/unit tests for the alert + exposure audit fixes (B/D) and the
three historical bugs. All offline — no chain, no network, no real money.

Covered:
  P0-①  _panic_active fail-closed (read error -> paused + alert)
  P0-②  tx_sender RPC primary/fallback rate-limited + escalated alerts
  Bug1  receipt Transfer-log recovery of token_out (real BOT buy fixture)
  Bug3  OpenPosition.from_hash decodes bytes-mode redis hash (token_amount != 0)
  Bug2  live-mode 0-token terminal sends a CRITICAL Telegram alert (lock)
  P1-③  monitor consecutive-exception alert state machine (>=3, rate-limited)
  P1-④  startup Telegram validation (live mode rejects missing/failed send)
  P1-⑤  portfolio exposure gate (sum open stake <= wallet * fraction)
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services" / "executor" / "src"
FIXTURES = ROOT / "tests" / "fixtures"

if str(EXECUTOR_SRC) not in sys.path:
    sys.path.insert(0, str(EXECUTOR_SRC))

import main  # noqa: E402
import notify  # noqa: E402
import position  # noqa: E402
import swap  # noqa: E402
import tx_sender  # noqa: E402


# ── P0-① panic fail-closed ───────────────────────────────────────────────
class PanicFailClosedTests(unittest.TestCase):
    def test_read_error_returns_true_and_alerts(self):
        main._panic_read_fail_alerted = False
        r = MagicMock()
        r.get.side_effect = ConnectionError("redis down")
        with patch.object(main, "send_telegram") as tg:
            paused = main._panic_active(r, "SYSTEM_PANIC")
        self.assertTrue(paused, "panic read error must FAIL-CLOSED (paused=True)")
        tg.assert_called_once()
        self.assertIn("UNREADABLE", tg.call_args[0][0])

    def test_alert_is_rate_limited_to_once(self):
        main._panic_read_fail_alerted = False
        r = MagicMock()
        r.get.side_effect = ConnectionError("redis down")
        with patch.object(main, "send_telegram") as tg:
            main._panic_active(r, "SYSTEM_PANIC")
            main._panic_active(r, "SYSTEM_PANIC")
        self.assertEqual(tg.call_count, 1, "repeated read errors must alert only once")

    def test_no_panic_when_key_absent(self):
        main._panic_read_fail_alerted = False
        r = MagicMock()
        r.get.return_value = None
        self.assertFalse(main._panic_active(r, "SYSTEM_PANIC"))

    def test_panic_active_when_key_set(self):
        main._panic_read_fail_alerted = False
        r = MagicMock()
        r.get.return_value = b"PAUSED"
        self.assertTrue(main._panic_active(r, "SYSTEM_PANIC"))


# ── P0-② RPC alerts ──────────────────────────────────────────────────────
class RpcAlertTests(unittest.TestCase):
    def setUp(self):
        tx_sender._rpc_alert_last.clear()
        tx_sender._rpc_hard_active.clear()

    def test_soft_degrade_never_telegrams(self):
        with patch.object(tx_sender, "send_telegram") as tg:
            self.assertFalse(tx_sender._rpc_alert("read_primary", "x"))
            self.assertFalse(tx_sender._rpc_alert("send_primary", "x"))
        tg.assert_not_called()

    def test_hard_outage_pages_once_then_holds(self):
        with patch.object(tx_sender, "send_telegram") as tg:
            self.assertTrue(tx_sender._rpc_alert("read_both_down", "x", escalate=True))
            self.assertFalse(tx_sender._rpc_alert("read_both_down", "x", escalate=True))
        self.assertEqual(tg.call_count, 1)

    def test_hard_outage_pages_again_after_recovery(self):
        with patch.object(tx_sender, "send_telegram") as tg:
            self.assertTrue(tx_sender._rpc_alert("read_both_down", "x", escalate=True))
            tx_sender._rpc_mark_recovered("read_both_down")
            self.assertTrue(tx_sender._rpc_alert("read_both_down", "x", escalate=True))
        self.assertEqual(tg.call_count, 2)

    def test_send_lane_no_fallback_escalates_and_raises(self):
        primary = MagicMock()
        primary.eth.send_raw_transaction.side_effect = Exception("send down")
        with patch.object(tx_sender, "_send_w3", primary), \
             patch.object(tx_sender, "_send_w3_fallback", None), \
             patch.object(tx_sender, "send_telegram") as tg:
            with self.assertRaises(Exception):
                tx_sender.send_raw_transaction(b"\x01", MagicMock())
        self.assertTrue(any("ESCALATED" in c.args[0] for c in tg.call_args_list))

    def test_send_lane_primary_fail_fallback_ok(self):
        primary = MagicMock()
        primary.eth.send_raw_transaction.side_effect = Exception("primary down")
        fb = MagicMock()
        fb.eth.send_raw_transaction.return_value = b"hash"
        with patch.object(tx_sender, "_send_w3", primary), \
             patch.object(tx_sender, "_send_w3_fallback", fb), \
             patch.object(tx_sender, "send_telegram") as tg:
            out = tx_sender.send_raw_transaction(b"\x01", MagicMock())
        self.assertEqual(out, b"hash")
        tg.assert_not_called()

    def test_send_lane_both_down_escalates(self):
        primary = MagicMock()
        primary.eth.send_raw_transaction.side_effect = Exception("primary down")
        fb = MagicMock()
        fb.eth.send_raw_transaction.side_effect = Exception("fallback down")
        with patch.object(tx_sender, "_send_w3", primary), \
             patch.object(tx_sender, "_send_w3_fallback", fb), \
             patch.object(tx_sender, "send_telegram") as tg:
            with self.assertRaises(Exception):
                tx_sender.send_raw_transaction(b"\x01", MagicMock())
        self.assertTrue(any("ESCALATED" in c.args[0] and "both" in c.args[0].lower()
                            for c in tg.call_args_list))

    def test_read_lane_primary_fail_fallback_ok_alerts(self):
        prov = tx_sender._ReadFallbackHTTPProvider("http://primary.local", "http://fb.local")
        fb = MagicMock()
        fb.make_request.return_value = {"result": "ok"}
        fb.endpoint_uri = "http://fb.local"
        prov._fallback = fb
        prov._lanes = [fb]
        with patch.object(tx_sender.Web3.HTTPProvider, "make_request", side_effect=Exception("primary down")), \
             patch.object(tx_sender, "send_telegram") as tg:
            out = prov.make_request("eth_call", [])
        self.assertEqual(out, {"result": "ok"})
        tg.assert_not_called()

    def test_read_lane_both_down_escalates_and_raises(self):
        prov = tx_sender._ReadFallbackHTTPProvider("http://primary.local", "http://fb.local")
        fb = MagicMock()
        fb.make_request.side_effect = Exception("fallback down")
        fb.endpoint_uri = "http://fb.local"
        prov._fallback = fb
        prov._lanes = [fb]
        with patch.object(tx_sender.Web3.HTTPProvider, "make_request", side_effect=Exception("primary down")), \
             patch.object(tx_sender, "send_telegram") as tg:
            with self.assertRaises(Exception):
                prov.make_request("eth_call", [])
        self.assertTrue(any("ESCALATED" in c.args[0] for c in tg.call_args_list))

    def test_read_lane_retries_transient_disconnect_then_succeeds(self):
        prov = tx_sender._ReadFallbackHTTPProvider(
            "http://primary.local", "http://fb.local", retries=3
        )
        fallback = MagicMock()
        prov._fallback = fallback
        prov._lanes = [fallback]
        calls = {"n": 0}

        def flaky(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Connection aborted. Remote end closed connection without response")
            return {"result": "ok"}

        with patch.object(tx_sender.Web3.HTTPProvider, "make_request", side_effect=flaky), \
             patch.object(tx_sender, "send_telegram") as tg:
            out = prov.make_request("eth_getBalance", [])
        self.assertEqual(out, {"result": "ok"})
        self.assertEqual(calls["n"], 3)
        fallback.make_request.assert_not_called()
        tg.assert_not_called()

    def test_catchup_windows_fill_gap_then_cap(self):
        self.assertEqual(tx_sender.catchup_windows(None, 100), [(100, 100)])
        self.assertEqual(tx_sender.catchup_windows(100, 100), [])
        self.assertEqual(
            tx_sender.catchup_windows(100, 140, chunk=10, max_blocks=20),
            [(101, 110), (111, 120)],
        )
        self.assertEqual(tx_sender.catchup_windows(200, 150), [(150, 150)])


# ── Bug1 receipt Transfer-log recovery ───────────────────────────────────
class ReceiptTokenRecoveryTests(unittest.TestCase):
    def test_recovers_bot_amount_from_real_receipt(self):
        fx = json.load(open(FIXTURES / "bot_buy_receipt_0xea6a3062.json"))
        receipt = {"status": fx["status"], "logs": fx["logs"]}
        amount = swap._extract_tokens_from_receipt(receipt, fx["bot_token"], fx["wallet"])
        # On-chain truth: ~90146.69 BOT received on the buy.
        self.assertGreater(amount, 0, "must NOT compute 0 (the old balanceOf-delta bug)")
        self.assertEqual(amount, 90146692587553330993631)
        self.assertAlmostEqual(amount / 1e18, 90146.69, places=1)

    def test_unrelated_wallet_recovers_zero(self):
        fx = json.load(open(FIXTURES / "bot_buy_receipt_0xea6a3062.json"))
        receipt = {"status": fx["status"], "logs": fx["logs"]}
        other = "0x" + "11" * 20
        self.assertEqual(swap._extract_tokens_from_receipt(receipt, fx["bot_token"], other), 0)


# ── Bug3 from_hash bytes mode ────────────────────────────────────────────
class FromHashBytesTests(unittest.TestCase):
    def test_bytes_keyed_hash_decodes_token_amount(self):
        # decode_responses=False redis returns bytes keys AND bytes values.
        h = {
            b"pool_address": b"0x82321f3beb69f503380d6b233857d5c43562e2d0",
            b"pool_type": b"aero_cl",
            b"token_address": b"0x940181a94a35a4569e4529a3cdfb74e38fd98631",
            b"token_decimals": b"18",
            b"token_amount": b"90146692587553330993631",
            b"entry_eth_wei": b"14553000000000000",
            b"opened_at_ms": b"1700000000000",
            b"fee_tier": b"30",
        }
        pos = position.OpenPosition.from_hash(h)
        self.assertEqual(pos.token_amount, 90146692587553330993631)
        self.assertNotEqual(pos.token_amount, 0, "bytes-mode hash must not decode to 0")
        self.assertEqual(pos.entry_eth_wei, 14553000000000000)
        self.assertEqual(pos.pool_type, "aero_cl")

    def test_str_keyed_hash_still_works(self):
        h = {"token_amount": "123", "entry_eth_wei": "456", "pool_address": "0xabc"}
        pos = position.OpenPosition.from_hash(h)
        self.assertEqual(pos.token_amount, 123)
        self.assertEqual(pos.entry_eth_wei, 456)


# ── Bug2 live-mode 0-token alert (behaviour lock) ────────────────────────
class ZeroTokenAlertLockTests(unittest.TestCase):
    """Lock the verified-PASS behaviour: the zero-token terminal branch must
    send a CRITICAL Telegram alert, guarded by live_trading_mode. Driving the
    full live buy offline is infeasible (needs a chain), so we lock the source
    of the terminal branch against silent-regression."""

    def test_zero_token_branch_alerts_in_live_mode(self):
        src = inspect.getsource(main._handle_signal)
        # locate the zero-token anomaly terminal block
        self.assertIn("0 tokens", src)
        self.assertIn("buy_success_zero_tokens", src)
        idx = src.index("buy_success_zero_tokens")
        window = src[max(0, idx - 1200):idx]
        self.assertIn("cfg.live_trading_mode", window)
        self.assertIn("send_telegram", window)
        self.assertIn("MANUAL CHECK", window)


# ── P1-③ monitor failure alert state machine ─────────────────────────────
class MonitorFailureStateTests(unittest.TestCase):
    def test_clean_cycle_resets_streak(self):
        streak, last, alert = main._update_monitor_failure_state(False, 5, 0.0, 1000.0)
        self.assertEqual(streak, 0)
        self.assertFalse(alert)

    def test_no_alert_below_threshold(self):
        streak, last, alert = main._update_monitor_failure_state(True, 0, 0.0, 1000.0)
        self.assertEqual(streak, 1)
        self.assertFalse(alert)
        streak, last, alert = main._update_monitor_failure_state(True, 1, 0.0, 1000.0)
        self.assertEqual(streak, 2)
        self.assertFalse(alert)

    def test_alert_at_threshold(self):
        # 3rd consecutive failure crosses MONITOR_FAIL_ALERT_THRESHOLD=3
        streak, last, alert = main._update_monitor_failure_state(True, 2, 0.0, 1000.0)
        self.assertEqual(streak, 3)
        self.assertTrue(alert)
        self.assertEqual(last, 1000.0)

    def test_alert_rate_limited(self):
        # already alerted at t=1000; another failure 10s later must not re-alert
        streak, last, alert = main._update_monitor_failure_state(True, 3, 1000.0, 1010.0)
        self.assertEqual(streak, 4)
        self.assertFalse(alert)
        # after the interval elapses, it alerts again
        far = 1000.0 + main.MONITOR_FAIL_ALERT_INTERVAL_SEC + 1
        streak, last, alert = main._update_monitor_failure_state(True, 4, 1000.0, far)
        self.assertTrue(alert)


# ── P1-④ startup Telegram validation ─────────────────────────────────────
class TelegramStartupValidationTests(unittest.TestCase):
    def test_non_live_is_noop(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
            notify.validate_telegram_or_raise(False)  # must not raise

    def test_live_missing_env_rejects(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
            with self.assertRaises(notify.TelegramConfigError):
                notify.validate_telegram_or_raise(True)

    def test_live_failed_send_rejects(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, clear=False), \
             patch.object(notify, "send_telegram", return_value=False):
            with self.assertRaises(notify.TelegramConfigError):
                notify.validate_telegram_or_raise(True)

    def test_live_ok_passes(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, clear=False), \
             patch.object(notify, "send_telegram", return_value=True):
            notify.validate_telegram_or_raise(True)  # must not raise


# ── P1-⑤ portfolio exposure gate ─────────────────────────────────────────
def _mk_cfg(**over):
    base = dict(
        redis_url="", signals_stream="s", signals_dead_letter_stream="d",
        consumer_group="g", consumer_name="n", chain_id=8453, dry_run=False,
        live_trading_mode=True, eth_price_usd=3500.0, max_position_usd=100.0,
        max_balance_fraction=0.10, max_concurrent=10, min_confidence=0.5,
        panic_key="SYSTEM_PANIC", rpc_url="", daily_loss_pct=25.0,
        dynamic_sizing=True, size_ladder_usd=(1000.0,), friction_min=-0.05,
        size_min_usd=25.0, max_pool_share=0.05, max_volume_share=0.10,
        size_bisect_iters=7, max_position_eth=0.057, size_min_eth=0.057,
        canary_eth=0.0, max_portfolio_fraction=0.30,
    )
    base.update(over)
    return main.ExecutorConfig(**base)


class PortfolioExposureGateTests(unittest.TestCase):
    def _store_with_exposure(self, stakes_wei):
        store = MagicMock()
        store.list_open_pools.return_value = [f"0x{i:040x}" for i in range(len(stakes_wei))]
        positions = {f"0x{i:040x}": MagicMock(entry_eth_wei=s) for i, s in enumerate(stakes_wei)}
        store.get.side_effect = lambda p: positions[p]
        return store

    def _w3_account(self, balance_wei):
        w3 = MagicMock()
        w3.eth.get_balance.return_value = balance_wei
        return w3, MagicMock(address="0x" + "ab" * 20)

    def test_blocks_when_exposure_at_cap(self):
        cfg = _mk_cfg(max_portfolio_fraction=0.30)
        wallet = 10 * 10**18
        # 3 open * 1 ETH = 3 ETH exposure >= 30% of 10 ETH (=3 ETH)
        store = self._store_with_exposure([10**18, 10**18, 10**18])
        w3, acct = self._w3_account(wallet)
        self.assertTrue(main._portfolio_exposure_exceeded(store, cfg, w3, acct))

    def test_allows_when_under_cap(self):
        cfg = _mk_cfg(max_portfolio_fraction=0.30)
        wallet = 10 * 10**18
        # 2 open * 1 ETH = 2 ETH < 3 ETH budget
        store = self._store_with_exposure([10**18, 10**18])
        w3, acct = self._w3_account(wallet)
        self.assertFalse(main._portfolio_exposure_exceeded(store, cfg, w3, acct))

    def test_disabled_when_fraction_zero(self):
        cfg = _mk_cfg(max_portfolio_fraction=0.0)
        store = self._store_with_exposure([10**18] * 100)
        w3, acct = self._w3_account(10**18)
        self.assertFalse(main._portfolio_exposure_exceeded(store, cfg, w3, acct))

    def test_failopen_on_balance_error(self):
        cfg = _mk_cfg(max_portfolio_fraction=0.30)
        store = self._store_with_exposure([10**18] * 5)
        w3 = MagicMock()
        w3.eth.get_balance.side_effect = Exception("rpc down")
        acct = MagicMock(address="0x" + "ab" * 20)
        # cannot evaluate -> do not block (sizing/other gates still apply)
        self.assertFalse(main._portfolio_exposure_exceeded(store, cfg, w3, acct))

    def test_default_fraction_is_030(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXECUTOR_MAX_PORTFOLIO_FRACTION", None)
            cfg = main.load_config()
        self.assertAlmostEqual(cfg.max_portfolio_fraction, 0.30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
