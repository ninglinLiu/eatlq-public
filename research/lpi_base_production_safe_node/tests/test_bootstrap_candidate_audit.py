from __future__ import annotations

import sys
import socket
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path("/root/lpi_base_production_safe_node")
sys.path.insert(0, str(WORKSPACE / "scripts"))

from audit_bootstrap_candidate import audit  # noqa: E402


class BootstrapCandidateAuditTests(unittest.TestCase):
    def candidate(self):
        return tempfile.TemporaryDirectory(dir=WORKSPACE / "var" / "tmp")

    def test_safe_candidate(self):
        with self.candidate() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("pytest==8.2.0\npyyaml==6.0.1\n")
            result = audit(root)
            self.assertEqual(result["risk_assessment"], "LOW")
            self.assertEqual(result["dependency_count_lower_bound"], 2)

    def test_blocks_production_path_and_service_mutation(self):
        with self.candidate() as directory:
            root = Path(directory)
            (root / "install.sh").write_text("systemctl restart x\ncp x /opt/EATLQ/x\n")
            result = audit(root)
            self.assertEqual(result["risk_assessment"], "BLOCK")
            kinds = {item["kind"] for item in result["findings"]}
            self.assertIn("service_or_scheduler_mutation", kinds)
            self.assertIn("production_path", kinds)

    def test_blocks_escaping_symlink(self):
        with self.candidate() as directory:
            root = Path(directory)
            (root / "escape").symlink_to("/opt/EATLQ")
            result = audit(root)
            self.assertEqual(result["risk_assessment"], "BLOCK")
            self.assertIn("symlink_escape", {item["kind"] for item in result["findings"]})

    def test_records_network_host_without_connecting(self):
        with self.candidate() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Documentation: https://example.invalid/api\n")
            result = audit(root)
            self.assertEqual(result["network_hosts"], ["example.invalid"])

    def test_external_connections_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "disabled for offline tests"):
            socket.create_connection(("example.invalid", 443))


if __name__ == "__main__":
    unittest.main()
