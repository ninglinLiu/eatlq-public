"""Sprint009: static paper/dry-run preflight (no Docker, Redis, RPC)."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "infra" / "compose" / "compose.yaml"
ENV_EXAMPLE = ROOT / "infra" / "compose" / ".env.example"


def _load_readiness():
    path = ROOT / "scripts" / "paper_dryrun_readiness.py"
    spec = importlib.util.spec_from_file_location("paper_dryrun_readiness", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


R = _load_readiness()


class TestPaperDryrunReadiness(unittest.TestCase):
    def test_env_example_matches_canonical_keys(self) -> None:
        declared = R.parse_dotenv_file_keys(ENV_EXAMPLE)
        self.assertEqual(declared, R.PAPER_READINESS_REQUIRED_ENV_EXAMPLE_KEYS)

    def test_parse_dotenv_keys_skips_comments(self) -> None:
        keys = R.parse_dotenv_keys(
            [
                "# FOO=1",
                "BAR=2",
                "",
                "export BAZ=x",
            ]
        )
        self.assertEqual(set(keys), {"BAR", "BAZ"})

    def test_live_trading_and_dry_run_conflict_matrix(self) -> None:
        self.assertFalse(R.live_trading_and_dry_run_conflict(None, None))
        self.assertFalse(R.live_trading_and_dry_run_conflict("0", "1"))
        self.assertFalse(R.live_trading_and_dry_run_conflict("false", "true"))
        self.assertTrue(R.live_trading_and_dry_run_conflict("true", "1"))

    def test_read_safe_env_pairs_only_guard_keys(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".env", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                "EXECUTOR_PRIVATE_KEY=secret_should_not_appear\n"
                "LIVE_TRADING_MODE=true\n"
                "EXECUTOR_DRY_RUN=1\n"
                "OTHER=x\n"
            )
            f.flush()
            p = Path(f.name)
        try:
            pairs = R.read_safe_env_pairs(p)
            self.assertEqual(set(pairs.keys()), {"LIVE_TRADING_MODE", "EXECUTOR_DRY_RUN"})
            self.assertTrue(R.live_trading_and_dry_run_conflict(pairs.get("LIVE_TRADING_MODE"), pairs.get("EXECUTOR_DRY_RUN")))
        finally:
            p.unlink(missing_ok=True)

    def test_compose_dashboard_strategy_executor_profiles(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        ok, errs = R.validate_compose_profiles(body)
        self.assertTrue(ok, msg="; ".join(errs))

    def test_compose_core_service_names(self) -> None:
        body = COMPOSE.read_text(encoding="utf-8")
        for name in ("redis:", "ingestor:", "strategy:", "executor:", "dashboard:"):
            self.assertIn(name, body)

    def test_laszlo_enables_three_profiles(self) -> None:
        body = (ROOT / "scripts" / "laszlo.sh").read_text(encoding="utf-8")
        ok, errs = R.validate_laszlo_profiles(body)
        self.assertTrue(ok, msg="; ".join(errs))

    def test_gitignore_excludes_runtime_csv_patterns(self) -> None:
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        miss = R.gitignore_covers_runtime_outputs(gi)
        self.assertEqual(miss, [])

    def test_preflight_rejects_truthy_live_trading_mode_alone(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("LIVE_TRADING_MODE=true\nEXECUTOR_DRY_RUN=0\n")
            f.flush()
            p = Path(f.name)
        try:
            buf_err = io.StringIO()
            buf_out = io.StringIO()
            with redirect_stderr(buf_err), redirect_stdout(buf_out):
                self.assertEqual(R.run_preflight(ROOT, p), 1)
        finally:
            p.unlink(missing_ok=True)

    def test_preflight_reports_conflict_on_tmp_env(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("LIVE_TRADING_MODE=true\nEXECUTOR_DRY_RUN=1\n")
            f.flush()
            p = Path(f.name)
        try:
            buf_err = io.StringIO()
            buf_out = io.StringIO()
            with redirect_stderr(buf_err), redirect_stdout(buf_out):
                rc = R.run_preflight(ROOT, p)
            self.assertEqual(rc, 1)
        finally:
            p.unlink(missing_ok=True)

    def test_preflight_ok_without_optional_env(self) -> None:
        buf_err = io.StringIO()
        buf_out = io.StringIO()
        with redirect_stderr(buf_err), redirect_stdout(buf_out):
            self.assertEqual(R.run_preflight(ROOT, None), 0)

    def test_compose_config_validate_skips_without_docker(self) -> None:
        with mock.patch.object(R.shutil, "which", return_value=None):
            ok, msgs, skipped = R.run_compose_config_validate(ROOT, ENV_EXAMPLE)
        self.assertTrue(ok)
        self.assertTrue(skipped)
        self.assertTrue(any("SKIP" in m for m in msgs))

    def test_compose_config_validate_nonzero_exit(self) -> None:
        proc = mock.Mock(returncode=1, stderr="boom", stdout="")
        with mock.patch.object(R.shutil, "which", return_value="/docker"):
            with mock.patch.object(R.subprocess, "run", return_value=proc):
                ok, msgs, skipped = R.run_compose_config_validate(ROOT, ENV_EXAMPLE)
        self.assertFalse(ok)
        self.assertFalse(skipped)
        self.assertTrue(any("FAIL" in m for m in msgs))

    def test_compose_config_validate_zero_exit(self) -> None:
        proc = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(R.shutil, "which", return_value="/docker"):
            with mock.patch.object(R.subprocess, "run", return_value=proc):
                ok, msgs, skipped = R.run_compose_config_validate(ROOT, ENV_EXAMPLE)
        self.assertTrue(ok)
        self.assertFalse(skipped)


if __name__ == "__main__":
    unittest.main()
