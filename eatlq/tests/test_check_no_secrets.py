"""Unit tests for scripts/check_no_secrets.py (Sprint013 S1-T6).

The lint itself iterates over ``git ls-files`` so the real-repo scan is
covered by CI (`make check-secrets-hygiene`). Here we exercise the pure
``scan_file`` / ``scan_env_example_values`` functions against synthetic
fixtures inside a tmp repo, proving:

- each sentinel pattern is detected
- allowlisted paths do NOT fire
- the .env.example empty-value rule fires only for secret-named keys
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_no_secrets.py"


def _load_script():
    name = "_check_no_secrets_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e.st"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)


class CheckNoSecretsTests(unittest.TestCase):
    def test_provider_rpc_token_is_flagged(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_git_repo(repo)
            f = repo / "notes.md"
            f.write_text(
                "see https://example-endpoint.base-mainnet.quiknode.pro// for ops",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            # scan_file operates via git-tracked state but reads files from disk;
            # we do not need to commit for scan_file itself.
            findings = mod.scan_file(repo, "notes.md")
            rules = {rule for _ln, rule, _ex in findings}
            self.assertIn("provider_rpc_token", rules)

    def test_url_embedded_credential_flagged_outside_env_example_template(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            f = repo / "readme.md"
            f.write_text("connect redis://host/0", encoding="utf-8")
            findings = mod.scan_file(repo, "readme.md")
            rules = {rule for _ln, rule, _ex in findings}
            self.assertIn("url_embedded_credential", rules)

    def test_env_example_template_allows_url_embedded_credential(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "infra" / "compose").mkdir(parents=True)
            f = repo / "infra" / "compose" / ".env.example"
            f.write_text(
                "BACKFILL_STATE_POSTGRES_URL=postgres://postgres:5432/eatlq\n",
                encoding="utf-8",
            )
            findings = mod.scan_file(repo, "infra/compose/.env.example")
            rules = {rule for _ln, rule, _ex in findings}
            self.assertNotIn("url_embedded_credential", rules)

    def test_privkey_assignment_flagged(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            f = repo / "leak.sh"
            f.write_text('export PRIVATE_KEY=0x' + ("d" * 64) + '\n', encoding="utf-8")
            findings = mod.scan_file(repo, "leak.sh")
            rules = {rule for _ln, rule, _ex in findings}
            self.assertIn("privkey_assignment", rules)

    def test_bearer_token_flagged(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            f = repo / "config.py"
            f.write_text('HDR = "Bearer ' + ("e" * 40) + '"\n', encoding="utf-8")
            findings = mod.scan_file(repo, "config.py")
            rules = {rule for _ln, rule, _ex in findings}
            self.assertIn("bearer_token_literal", rules)

    def test_env_example_secret_keys_must_be_empty(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "infra" / "compose").mkdir(parents=True)
            env = repo / "infra" / "compose" / ".env.example"
            env.write_text(
                textwrap.dedent(
                    """
                    EXECUTOR_PRIVATE_KEY=0x1234
                    TELEGRAM_BOT_TOKEN=
                    DASHBOARD_BASIC_PASS=hunter2
                    DASHBOARD_ACTION_TOKEN=
                    EXECUTOR_RPC_URL=
                    OTHER_KEY=not_secret
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            findings = mod.scan_env_example_values(repo, "infra/compose/.env.example")
            rule_keys = [excerpt.split("=", 1)[0] for _ln, rule, excerpt in findings if rule == "env_example_secret_has_value"]
            self.assertIn("EXECUTOR_PRIVATE_KEY", rule_keys)
            self.assertIn("DASHBOARD_BASIC_PASS", rule_keys)
            # Empty-valued keys must not be flagged.
            self.assertNotIn("TELEGRAM_BOT_TOKEN", rule_keys)
            self.assertNotIn("EXECUTOR_RPC_URL", rule_keys)

    def test_allowlisted_files_are_not_scanned(self) -> None:
        mod = _load_script()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "scripts").mkdir()
            # The lint itself is on its own allowlist.
            shadow = repo / "scripts" / "check_no_secrets.py"
            shadow.write_text(
                'Bearer ' + ("z" * 30) + ' PRIVATE_KEY=0x' + ("f" * 64) + '\n',
                encoding="utf-8",
            )
            findings = mod.scan_file(repo, "scripts/check_no_secrets.py")
            self.assertEqual(findings, [])

    def test_ci_workflow_runs_check_static(self) -> None:
        """S1-T6: CI must invoke make check-static, which chains check-secrets-hygiene."""

        wf = (ROOT / ".github" / "workflows" / "static-contracts.yml").read_text(encoding="utf-8")
        self.assertIn("make check-static", wf)

        mk = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("check-secrets-hygiene", mk)
        # check-static must chain check-secrets-hygiene so the default CI gate
        # enforces it.
        self.assertRegex(mk, r"check-static:\s*[^\n]*check-secrets-hygiene")


if __name__ == "__main__":
    unittest.main()
