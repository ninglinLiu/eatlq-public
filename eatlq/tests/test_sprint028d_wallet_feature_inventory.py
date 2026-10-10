"""Static checks for Sprint028D wallet feature inventory (documentation-only sprint)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV_MD = ROOT / "mvp" / "audits" / "sprint028d_wallet_feature_inventory.md"
SUMMARY = ROOT / "mvp" / "diagnostics" / "sprint028d_wallet_feature_inventory_summary.json"


class Sprint028DWalletInventoryTests(unittest.TestCase):

    def test_md_and_json_exist(self) -> None:
        self.assertTrue(INV_MD.is_file(), msg=f"Missing {INV_MD}")
        self.assertTrue(SUMMARY.is_file(), msg=f"Missing {SUMMARY}")

    def test_json_valid_and_booleans(self) -> None:
        data = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(data.get("kind"), "sprint028d_wallet_feature_inventory_summary")
        self.assertIs(data.get("smart_money_general_line_open"), False)
        self.assertIs(data.get("smart_money_as_input_or_diagnostic"), True)
        self.assertIs(data.get("canon_parity"), False)
        self.assertIs(data.get("postgres_wallet_profiles_exists"), False)

    def test_md_flags_and_canon(self) -> None:
        t = INV_MD.read_text(encoding="utf-8")
        self.assertIn("SMART_MONEY_GENERAL_LINE_OPEN=false", t)
        self.assertIn("CANON_PARITY=false", t)
        self.assertIn("POSTGRES_WALLET_PROFILES_EXISTS=false", t)
        self.assertNotIn("alpha proof", t.lower())
        self.assertNotIn("live authorized", t.lower())

    def test_candidate_features_all_future(self) -> None:
        data = json.loads(SUMMARY.read_text(encoding="utf-8"))
        cands = data.get("candidate_features") or self.fail("missing candidate_features")
        self.assertGreaterEqual(len(cands), 10)
        for c in cands:
            self.assertEqual(
                c.get("status"),
                "FUTURE/CANDIDATE",
                msg=f"bad status for {c.get('feature_name')}",
            )

    def test_md_each_feature_section_future(self) -> None:
        """Each ### feature block must include Status: FUTURE/CANDIDATE."""
        t = INV_MD.read_text(encoding="utf-8")
        names = [
            "per_pool_buyer_archetype_mix",
            "repeat_buyer_ratio",
            "wallet_age_proxy",
            "sender_entropy_by_archetype",
            "smart_wallet_share_shadow",
            "sniper_share_shadow",
            "diamond_hand_proxy_shadow",
            "paper_hand_proxy_shadow",
            "tx_origin_repeat_rate",
            "new_wallet_burst_proxy",
        ]
        for n in names:
            self.assertIn(f"### {n}", t)
            chunk = t.split(f"### {n}", 1)[1].split("###", 1)[0]
            self.assertIn("FUTURE/CANDIDATE", chunk, msg=f"section {n}")


if __name__ == "__main__":
    unittest.main()
