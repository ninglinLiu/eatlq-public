from __future__ import annotations

import ast
import csv
import json
import re
import unittest
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


class IntegrityTests(unittest.TestCase):
    def test_no_execution_capability_in_research_sources(self):
        forbidden=re.compile(r"eth_send(?:raw)?transaction|send_raw_transaction|sign_transaction|web3|private_key|mnemonic",re.I)
        for path in (ROOT/"src").glob("*.py"):
            if path.name=="safety.py":
                continue
            self.assertIsNone(forbidden.search(path.read_text()),path)

    def test_every_cli_entry_has_guard(self):
        for path in (ROOT/"src").glob("*.py"):
            tree=ast.parse(path.read_text())
            if 'if __name__' in path.read_text():
                self.assertIn("research_entry_guard",path.read_text(),path)

    def test_required_artifacts(self):
        names=["MARKET_MAP.md","ALPHA_ATLAS.csv","ALPHA_LEADERBOARD.md","WEIRD_FINDINGS.md","FINAL_WILDCAT_REPORT.md","KILL_PANEL_FULL.json","INDEPENDENT_RECOMPUTE.json","PROVENANCE.json"]
        for name in names:
            self.assertTrue((ROOT/name).is_file(),name)
        self.assertTrue((ROOT/"EXPERIMENTS").is_dir())

    def test_atlas_scope_and_statuses(self):
        with (ROOT/"ALPHA_ATLAS.csv").open() as fh:
            rows=list(csv.DictReader(fh))
        self.assertGreaterEqual(len(rows),40)
        self.assertGreaterEqual(len({r['family'] for r in rows}),10)
        allowed={"ORACLE_ONLY","DISCOVERY_ONLY","GROSS_STRUCTURE","MECHANISM_PLAUSIBLE","REPLAY_SURVIVOR","SHADOW_CANDIDATE","KILLED","DATA_INSUFFICIENT","NOT_YET_TESTED","IMPLEMENTATION_BLOCKED","DATA_INVALID"}
        self.assertFalse({r['status'] for r in rows}-allowed)

    def test_independent_recompute(self):
        result=json.loads((ROOT/"INDEPENDENT_RECOMPUTE.json").read_text())
        self.assertTrue(result["all_match"])

    def test_final_safety_footer(self):
        text=(ROOT/"FINAL_WILDCAT_REPORT.md").read_text()
        for line in ("LIVE_TRADING = FORBIDDEN","REAL_ORDERS_SENT = 0","REAL_TRANSACTIONS_SIGNED = 0","REAL_CAPITAL_AT_RISK = 0"):
            self.assertIn(line,text)
        self.assertNotIn("LIVE"+"_GO",text)
        self.assertNotIn("DEPLOY"+"_NOW",text)
        self.assertNotIn("START"+"_REAL_TRADING",text)


if __name__=="__main__": unittest.main()
