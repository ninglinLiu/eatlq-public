import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
LEDGER = D / "base_gl4_live_truth_ledger_20260716.json"
CORPUS = D / "base_gl4_corpus_manifest_20260716.json"
RPC = D / "base_gl4_rpc_ledger_20260716.json"
PREREG = ROOT / "configs/research/base_gl4_live_reconciliation_preregistration_20260716.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GL4LiveReconciliationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(LEDGER.read_text())
        cls.c = json.loads(CORPUS.read_text())
        cls.r = json.loads(RPC.read_text())

    def test_preregistration_and_safety_are_exact(self):
        self.assertEqual(sha(PREREG), "6cff51454d40b7623cfcd3cf2ed2e10ac461019c67a88e4c921a33547e4ad7b7")
        self.assertEqual(self.d["preregistration"]["sha256"], sha(PREREG))
        self.assertTrue(self.d["research_only"])
        self.assertEqual(self.d["go_decision"], "NOT_MADE")
        self.assertFalse(self.d["safety"]["training_allowed"])
        self.assertFalse(self.d["safety"]["live_allowed"])
        self.assertFalse(self.d["safety"]["self_go_allowed"])
        self.assertEqual(self.d["safety"]["send_sign_broadcast_methods"], [])
        self.assertEqual(self.r["send_sign_broadcast_methods"], [])
        self.assertEqual(set(self.r["methods_attempted"]), {"eth_call", "eth_chainId", "eth_getBlockByNumber", "eth_getTransactionByHash", "eth_getTransactionReceipt"})

    def test_full_corpus_manifest_is_not_sampling_and_hashes_match(self):
        self.assertTrue(self.c["exhaustive_not_sampled"])
        self.assertEqual(self.c["summary"]["files"], 633)
        self.assertEqual(self.c["summary"]["bytes"], 71817534)
        self.assertEqual(self.c["summary"]["path_sha256_stream_sha256"], "498d86564ba8366cd14406ff1d943f63e0e5c67b0dce97064a736d6096c3bfe2")
        self.assertGreaterEqual(self.c["summary"]["files"], self.d["independent_full_corpus_read"]["files"])
        for row in self.c["files"]:
            path = ROOT / row["path"]
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_size, row["bytes"])
            self.assertEqual(sha(path), row["sha256"])

    def test_receipt_classification_and_counts_are_exact(self):
        a = self.d["aggregate"]
        self.assertEqual(a["outgoing_transactions"], 73)
        self.assertEqual(a["successful_swap_fill_legs"], 11)
        self.assertEqual((a["successful_buy_fills"], a["successful_sell_fills"]), (8, 3))
        self.assertEqual(a["approvals_non_fills"], 3)
        self.assertEqual(a["failed_sell_attempts_non_fills"], 48)
        fills = self.d["real_successful_fill_legs"]
        self.assertTrue(all(x["receipt_status"] == 1 for x in fills))
        self.assertTrue(all(x["swap_pools"] for x in fills))
        self.assertTrue(all(x["wallet_relevant_transfers"] for x in fills))
        failed = [x for x in self.d["transactions"] if x["classification"] == "FAILED_SELL_ATTEMPT_NON_FILL"]
        self.assertTrue(all(x["receipt_status"] == 0 for x in failed))

    def test_principal_realized_pnl_and_win_rate_are_exact(self):
        a = self.d["aggregate"]
        self.assertEqual(a["cumulative_invested_weth"], 0.16991997829845873)
        self.assertEqual(a["cumulative_invested_usd_at_buy_oracle"], 288.1624918243946)
        self.assertEqual(a["closed_roundtrips"], 3)
        self.assertEqual(a["closed_realized_pnl_weth_all_associated"], -1.1451891750165853e-05)
        self.assertEqual(a["closed_win_rate"], 0.0)
        self.assertEqual(a["worst_closed_trade_weth"], -4.9942355199008875e-06)
        self.assertTrue(all(not x["win_after_all_associated_gas"] for x in self.d["closed_roundtrips"]))

    def test_open_positions_are_not_mislabeled_as_realized_sells(self):
        positions = self.d["open_positions"]
        self.assertEqual(len(positions), 5)
        self.assertTrue(all(x["cash_realized_pnl_weth"] is None for x in positions))
        self.assertTrue(all(x["full_purchased_token_balance_retained"] for x in positions))
        self.assertTrue(all(x["liquidity_collapsed_ge_99pct_by_window_end"] for x in positions))
        para = [x for x in positions if x["trade_name"].startswith("PARADAENGI")]
        self.assertEqual(len(para), 3)
        self.assertTrue(all(x["end_exact_size_qv2"]["status"] == "EXPLICIT_EXECUTION_REVERT" for x in para))
        self.assertTrue(all(x["end_executable_mark_weth"] == 0 for x in para))
        self.assertGreater(next(x for x in positions if x["trade_name"] == "JOTCHUA")["end_executable_mark_weth"], 0)
        self.assertGreater(next(x for x in positions if x["trade_name"] == "BOT")["end_executable_mark_weth"], 0)

    def test_economic_mark_and_attribution_identity_are_exact(self):
        a = self.d["aggregate"]
        self.assertEqual(a["receipt_plus_end_executable_mark_economic_pnl_weth"], -0.1602358622293912)
        self.assertEqual(a["receipt_time_oracle_economic_pnl_usd"], -271.5114116236298)
        self.assertEqual(a["resolved_entry_outcomes"], 8)
        self.assertEqual(a["resolved_entry_win_rate"], 0.0)
        w = self.d["attribution_waterfall"]
        add = (w["execution_gap"]["identified_amount"] + w["rug_realization"]["amount"] +
               w["regime_decay"]["identified_amount"] + w["selection_timing"]["identified_amount"] +
               w["small_sample_residual"]["amount"])
        self.assertEqual(add, w["actual"])
        self.assertEqual(w["identity_check"], w["actual"])

    def test_strategy_and_estimand_mismatch_is_explicit(self):
        self.assertFalse(self.d["model_artifacts"]["runtime_strategy_unchanged"])
        self.assertTrue(self.d["model_artifacts"]["entry_model_evidenced_unchanged_20260622_20260629"])
        self.assertEqual(self.d["model_artifacts"]["post_20260629_runtime"], "UNKNOWN")
        self.assertTrue(all(not x["same"] for x in self.d["actual_vs_plus56_estimand"]))
        self.assertEqual(self.d["adjudication_proposal_pending_independent_review"]["GL1_02"], "FAIL_AT_OBSERVED_TINY_LIVE_SCALE")

    def test_report_and_plain_language_deliverables_exist(self):
        report = ROOT / "reports/GL4_live_track_record_reconciliation_20260716.md"
        plain = ROOT / "reports/GL4_live_plain_language_20260716.md"
        self.assertTrue(report.exists())
        self.assertTrue(plain.exists())
        text = report.read_text()
        for required in ("## A. 真金战绩", "## B. 开盘 / 暂停 / 重启", "## C. 实际配置 pin", "## D. 预注册归因分桶", "## E. 对质裁定"):
            self.assertIn(required, text)
        self.assertIn("GL1-02", plain.read_text())


if __name__ == "__main__":
    unittest.main()
