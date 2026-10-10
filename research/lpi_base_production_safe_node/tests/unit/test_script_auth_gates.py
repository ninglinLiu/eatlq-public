from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from launchpad_intelligence.security.auth_gate import CAP_REDIS_HEALTH, env_cannot_authorize, is_capability_authorized


ROOT = Path("/root/lpi_base_production_safe_node")


class ScriptAuthGateTests(unittest.TestCase):
    def test_env_vars_do_not_authorize(self) -> None:
        self.assertTrue(env_cannot_authorize(CAP_REDIS_HEALTH))
        self.assertFalse(is_capability_authorized(CAP_REDIS_HEALTH, explicit_cli_flag=False))

    def test_symlink_token_or_ancestor_rejected(self) -> None:
        # Only denial paths; never create a valid authorization token.
        token_dir = ROOT / "var" / "auth_tokens"
        decoy = ROOT / "var" / "tmp" / "decoy_token_target"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text("AUTHORIZED:redis_health_observe\n", encoding="utf-8")
        link = token_dir / "redis_health_observe.authorized"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(decoy)
        try:
            self.assertFalse(is_capability_authorized(CAP_REDIS_HEALTH, explicit_cli_flag=True))
        finally:
            if link.is_symlink() or link.exists():
                link.unlink()
            if decoy.exists():
                decoy.unlink()

    def test_observe_script_exits_before_connect_by_default(self) -> None:
        script = ROOT / "scripts" / "observe_live_capacity.sh"
        # Even with ENABLE set, must deny without CLI+token.
        env = os.environ.copy()
        env["ENABLE_REDIS_HEALTH_OBSERVE"] = "1"
        env["ALLOW_REDIS_HEALTH_OBSERVE"] = "1"
        proc = subprocess.run(
            ["bash", str(script), str(ROOT / "var" / "tmp" / "should_not_create")],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("DENIED", proc.stderr)
        self.assertFalse((ROOT / "var" / "tmp" / "should_not_create").exists())

    def test_run_offline_tests_does_not_mention_home_reset_in_script(self) -> None:
        text = (ROOT / "scripts" / "run_offline_tests.sh").read_text(encoding="utf-8")
        self.assertNotIn("export HOME=", text)
        self.assertIn("XDG_CACHE_HOME", text)


if __name__ == "__main__":
    unittest.main()
