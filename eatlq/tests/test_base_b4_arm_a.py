import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/research/base_b4/b4_arm_a.py"
spec = importlib.util.spec_from_file_location("b4_arm_a", SCRIPT)
B4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B4)


class TestBaseB4ArmA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = B4.build()

    def test_frozen_universe_integers_equal(self):
        inv = self.result["invariants"]
        self.assertEqual(inv["n_shard_rows"], 644)
        self.assertEqual(inv["n_universe_rows"], 644)
        self.assertEqual(inv["labels"], {"clean": 378, "dead": 4, "delayed_rug": 262})
        self.assertEqual(inv["set131_set217_overlap_pools"], 124)

    def test_full_offline_golden_integers_equal(self):
        golden = json.loads((ROOT / "tests/fixtures/base_b4_arm_a_golden_20260714.json").read_text())
        self.assertEqual(self.result["invariants"]["labels"], golden["labels"])
        actual_entry = {x["label"]: [x["n_labeled"], x["n_qv2_call_ok"], x["n_binary_status_mismatch"]]
                        for x in self.result["entry_consistency"]}
        self.assertEqual(actual_entry, golden["entry_rows"])
        self.assertEqual(self.result["time_sensitivity"]["flip_offset_blocks"], golden["flip_offset_blocks"])
        self.assertEqual(self.result["time_sensitivity"]["flip_timing_vs_our_exit"], golden["flip_timing"])
        actual_surv = {x["stratum"]: [x["n_all"], x["n_enterable"], x["n_rug"]]
                       for x in self.result["survivorship"]}
        self.assertEqual(actual_surv, golden["survivorship_counts"])
        ex = self.result["exit_point_replay"]
        actual_exit = {
            "set217": [ex["primary_set217"]["n_valid_pair"], ex["primary_set217"]["n_qv2_healthy"], ex["primary_set217"]["n_disagreement"]],
            "set131": [ex["replication_set131"]["n_valid_pair"], ex["replication_set131"]["n_qv2_healthy"], ex["replication_set131"]["n_disagreement"]],
            "deduplicated_union": [ex["deduplicated_union_descriptive"]["n_valid_pair"], ex["deduplicated_union_descriptive"]["n_qv2_healthy"], ex["deduplicated_union_descriptive"]["n_disagreement"]],
        }
        self.assertEqual(actual_exit, golden["exit_replay"])

    def test_preregistered_instant_hp_is_unmeasured(self):
        row = next(x for x in self.result["entry_consistency"] if x["label"] == "instant_hp")
        self.assertEqual(row["n_labeled"], 0)
        self.assertEqual(row["interpretation"], "UNTESTED_NO_SUBSET")
        self.assertEqual(row["false_positive_wilson95"], [None, None])

    def test_time_table_exact_conservation(self):
        t = self.result["time_sensitivity"]
        self.assertEqual(t["flip_offset_blocks"], {"30": 3, "300": 19, "1800": 111, "7200": 129})
        self.assertEqual(sum(t["flip_offset_blocks"].values()), 262)
        self.assertEqual(sum(t["flip_timing_vs_our_exit"].values()), 262)
        self.assertEqual(sum(x["n"] for x in t["flip_offset_x_exit_timing"]), 262)

    def test_survivorship_exact_counts(self):
        rows = {x["stratum"]: x for x in self.result["survivorship"]}
        self.assertEqual((rows["traded_survivor"]["n_all"], rows["traded_survivor"]["n_rug"]), (384, 240))
        self.assertEqual((rows["hidden_by_quoterv2_friction"]["n_all"], rows["hidden_by_quoterv2_friction"]["n_rug"]), (260, 22))
        self.assertEqual((rows["full_candidate"]["n_all"], rows["full_candidate"]["n_rug"]), (644, 262))

    def test_set_overlap_is_not_double_counted(self):
        exit_rows = self.result["exit_point_replay"]
        self.assertEqual(exit_rows["deduplicated_union_descriptive"]["n_unique"], 224)
        self.assertNotEqual(224, 131 + 217)
        self.assertEqual(exit_rows["primary_set217"]["n_disagreement"], 1)
        self.assertEqual(exit_rows["replication_set131"]["n_disagreement"], 1)

    def test_qv2_zero_is_not_missing(self):
        row = {"qv2_net": 0.0, "real_tradeable_at_exit": False, "pool": "x"}
        self.assertIsNotNone(row.get("qv2_net"))
        self.assertGreater(row["qv2_net"], -0.5)

    def test_runner_has_no_rpc_or_transaction_path(self):
        source = SCRIPT.read_text()
        for forbidden in ("send_raw_transaction", "sign_transaction", "eth_sendRawTransaction",
                          "eth_sendTransaction", "subprocess", "anvil", "eth_call"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
