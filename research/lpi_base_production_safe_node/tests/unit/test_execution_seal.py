from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from launchpad_intelligence.errors import AccessDeniedError
from launchpad_intelligence.health.command_executor import ExternalCommandExecutor
from launchpad_intelligence.security.execution_seal import (
    DEFAULT_CRITICAL_PATHS,
    assert_seal_unchanged,
    register_run_owned_outputs,
    require_clean_sealed_worktree,
)


class ExecutionSealTests(unittest.TestCase):
    def _init_repo(self, root: Path) -> None:
        subprocess.check_call(["git", "init"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)
        # Pre-track evidence/ so new stamp dirs appear as nested untracked paths (production shape).
        (root / "evidence" / ".keep").parent.mkdir(parents=True, exist_ok=True)
        (root / "evidence" / ".keep").write_text("keep\n", encoding="utf-8")
        for rel in DEFAULT_CRITICAL_PATHS:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# fixture {rel}\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "-A"], cwd=root, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "commit", "-m", "fixture"], cwd=root, stdout=subprocess.DEVNULL)

    def test_clean_repo_initial_seal_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            self.assertTrue(seal.head_commit)
            self.assertIn("launchpad_intelligence/health/command_executor.py", seal.critical_blob_sha256)
            self.assertIn("launchpad_intelligence/query/runner_status.py", seal.critical_blob_sha256)

    def test_unrelated_untracked_rejected_at_zero_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            (root / "stray.txt").write_text("x\n", encoding="utf-8")
            ex = ExternalCommandExecutor()
            with self.assertRaises(AccessDeniedError):
                require_clean_sealed_worktree(root)
            self.assertEqual(ex.call_count, 0)

    def test_allowlisted_evidence_after_seal_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            ev = root / "evidence" / "base_metadata_inventory_fixture"
            ev.mkdir()
            (ev / "run_metadata.json").write_text("{}\n", encoding="utf-8")
            token = root / "var" / "auth_tokens" / "metadata_schema_inventory.authorized"
            token.parent.mkdir(parents=True)
            token.write_text("AUTHORIZED:metadata_schema_inventory\n", encoding="utf-8")
            seal2 = register_run_owned_outputs(seal, evidence_dir=ev, token_path=token)
            assert_seal_unchanged(seal2)

    def test_critical_file_change_after_seal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            crit = root / "scripts" / "run_metadata_inventory.py"
            # Keep porcelain clean by matching... actually modifying tracked file dirties status.
            crit.write_text("# mutated\n", encoding="utf-8")
            with self.assertRaises(AccessDeniedError):
                assert_seal_unchanged(seal)

    def test_blob_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            # Change file then restore index to match HEAD path listing while bytes differ:
            # Use update-index --skip-worktree is messy. Instead: seal, then replace bytes and
            # `git add` then `git commit` would change HEAD. For blob mismatch with clean porcelain:
            # checkout-index can't help. Simulate by seal then writing same git status via
            # assuming we detect digest mismatch even when somehow status clean — force by
            # temporarily patching file and using update-index --assume-unchanged.
            seal = require_clean_sealed_worktree(root)
            crit = root / "launchpad_intelligence" / "security" / "execution_seal.py"
            subprocess.check_call(["git", "update-index", "--assume-unchanged", str(crit.relative_to(root))], cwd=root)
            crit.write_text("# blob mismatch body\n", encoding="utf-8")
            with self.assertRaises(AccessDeniedError) as ctx:
                assert_seal_unchanged(seal)
            self.assertIn("critical file changed", str(ctx.exception))

    def test_head_change_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            (root / "extra.md").write_text("e\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "extra.md"], cwd=root, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "commit", "-m", "move head"], cwd=root, stdout=subprocess.DEVNULL)
            with self.assertRaises(AccessDeniedError) as ctx:
                assert_seal_unchanged(seal)
            self.assertIn("HEAD/tree", str(ctx.exception))

    def test_outside_allowlist_untracked_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            ev = root / "evidence" / "base_metadata_inventory_fixture"
            ev.mkdir()
            (ev / "ok.txt").write_text("1\n", encoding="utf-8")
            seal2 = register_run_owned_outputs(seal, evidence_dir=ev)
            (root / "not_allowed.txt").write_text("x\n", encoding="utf-8")
            with self.assertRaises(AccessDeniedError):
                assert_seal_unchanged(seal2)

    def test_symlink_in_allowlisted_output_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            seal = require_clean_sealed_worktree(root)
            ev = root / "evidence" / "base_metadata_inventory_fixture"
            ev.mkdir()
            target = root / "evidence" / ".keep"
            link = ev / "evil_link"
            link.symlink_to(target)
            seal2 = register_run_owned_outputs(seal, evidence_dir=ev)
            with self.assertRaises(AccessDeniedError):
                assert_seal_unchanged(seal2)


if __name__ == "__main__":
    unittest.main()
