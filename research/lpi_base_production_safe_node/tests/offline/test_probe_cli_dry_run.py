from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import bounded_db_probe as probe


class ProbeCliDryRunTests(unittest.TestCase):
    def test_cli_dry_run_writes_preflight(self) -> None:
        root = Path("/root/lpi_base_production_safe_node")
        with tempfile.TemporaryDirectory(dir=root / "var" / "tmp") as tmp:
            out = Path(tmp) / "preflight.json"
            rc = probe.main(
                [
                    "--candidate-table",
                    "logical.base_logs_candidate",
                    "--columns",
                    "block_number,transaction_hash,log_index,address,topic0,data",
                    "--partition-key",
                    "block_number",
                    "--start-block",
                    "20000000",
                    "--end-block",
                    "20000999",
                    "--limit",
                    "1000",
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(data["execution_authorized"])
            self.assertEqual(data["end_block"] - data["start_block"] + 1, 1000)

    def test_cli_execute_denied(self) -> None:
        rc = probe.main(
            [
                "--candidate-table",
                "logical.t",
                "--columns",
                "block_number",
                "--start-block",
                "1",
                "--end-block",
                "2",
                "--execute",
            ]
        )
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
