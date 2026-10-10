import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"


class B8HonestExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = json.loads((DIAG / "base_b8_honest_execution_20260714.json").read_text())
        cls.rpc = json.loads((DIAG / "base_b8_rpc_evidence_20260714.json").read_text())
        cls.trades = json.loads((DIAG / "base_b8_honest_execution_per_trade_20260714.json").read_text())["rows"]
        cls.fixture = json.loads((ROOT / "tests/fixtures/base_b8_golden_20260714.json").read_text())

    def test_preregistration_is_frozen_and_matches_rpc(self):
        prereg = ROOT / "configs/research/base_b8_honest_execution_preregistration_20260714.json"
        digest = hashlib.sha256(prereg.read_bytes()).hexdigest()
        self.assertEqual(digest, self.fixture["preregistration_sha256"])
        self.assertEqual(digest, self.result["preregistration_sha256"])
        self.assertEqual(digest, self.rpc["preregistration_sha256"])

    def test_rpc_is_read_only_and_capped(self):
        self.assertEqual(set(self.rpc["rpc"]["methods"]), {"eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionReceipt", "eth_call"})
        self.assertLessEqual(self.rpc["rpc"]["http_attempts"], self.rpc["rpc"]["hard_cap_attempts"])
        self.assertEqual(self.rpc["rpc"]["api_credits"], 20 * self.rpc["rpc"]["http_attempts"])
        source = (ROOT / "scripts/research/base_b8/b8_rpc_evidence.py").read_text()
        self.assertNotIn("eth_sendRawTransaction", source)
        self.assertNotIn("PRIVATE_KEY", source)

    def test_exact_universe_and_atomic_coverage(self):
        self.assertEqual(self.rpc["summary"]["rows"], 431)
        self.assertEqual(self.rpc["summary"]["selected"], 116)
        self.assertEqual(self.rpc["summary"]["entered_selected"], 113)
        self.assertEqual(self.rpc["summary"]["atomic_measured"], {"0": 431, "30": 431, "60": 431})

    def test_only_true_rug_receives_minus_100pct_gross(self):
        for row in self.trades:
            for result in row["results"].values():
                if result["gross"] == -1.0:
                    self.assertEqual(result["evidence"], "true_rug")
                if result["evidence"] == "no_entry_flat":
                    self.assertEqual(result["ret"], 0.0)
                self.assertTrue(result["resolved"])

    def test_exact_evidence_counts_and_delay_decay(self):
        self.assertEqual(self.result["evidence_distribution"], self.fixture["evidence_counts"])
        means = [self.result["performance"][str(x)]["primary"]["pooled"]["mean"] for x in (0, 30, 60)]
        self.assertGreater(means[0], means[1])
        self.assertGreater(means[1], means[2])

    def test_exact_primary_30m_golden_metrics(self):
        got = self.result["performance"]["30"]["primary"]["pooled"]
        for key, value in self.fixture["primary_30m"].items():
            self.assertEqual(got[key], value)
        self.assertEqual(self.result["preregistered_right_tail"]["evidence_status"], "FAIL")
        self.assertEqual(self.result["go_decision"], "NOT_MADE")

    def test_helper_address_sample(self):
        helper = self.result["helper_address_risk"]
        self.assertEqual(helper["sample_n"], 20)
        self.assertEqual(helper["transfer_to_pool_ok"], 20)
        self.assertEqual(helper["transfer_to_pool_failed"], 0)

    def test_chain_integer_golden_exact(self):
        compact = []
        for row in self.result["golden"]:
            compact.append({
                "pool": row["pool"],
                "entry_block": row["entry"]["block"],
                "entry_tx": row["entry"]["tx"],
                "entry_weth_raw": row["entry"]["weth_raw"],
                "entry_token_raw": row["entry"]["token_raw"],
                "position_qty": row["entry"]["position_qty"],
                "scheduled_block": row["scheduled_block"],
                "scheduled_block_hash": row["scheduled_block_hash"],
                "exit_evidence": row["exit_result"]["evidence"],
                "exit_block": row["exit_result"].get("exit_block"),
                "atomic_received_target": row["atomic"].get("received_target"),
                "atomic_recovered_weth": row["atomic"].get("recovered_weth"),
            })
        self.assertEqual(compact, self.fixture["chain_integer_golden"])


if __name__ == "__main__":
    unittest.main()
