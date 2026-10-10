"""Sprint029C — Static guards for retrain_model.py economic-gate posture (AST only).

Does not import retrain_model or call main(). No training, labeling, RPC, or Redis.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRAIN = ROOT / "scripts" / "retrain_model.py"
DESIGN_029C = ROOT / "mvp" / "audits" / "sprint029c_retrain_guard_design.md"
DESIGN_029A = ROOT / "mvp" / "audits" / "sprint029a_scoped_validation_design.md"
ML_GATE = ROOT / "docs" / "ml-training-gate.md"
FAILED_GUARD = ROOT / "mvp" / "audits" / "failed_no_7d_alpha_guard.md"
THIS_FILE = Path(__file__).resolve()

APPROVED_FEATURE_COLUMNS = (
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
)

REQUIRED_DENYLIST_COLUMNS = frozenset(
    {
        "executable_label",
        "executable_net_return",
        "executable_status",
        "failure_reason",
        "approximate_label",
        "approximate_net_return",
        "entry_price",
        "exit_price",
    }
)

REQUIRED_DENYLIST_PREFIXES = (
    "future_",
    "exit_",
    "entry_",
    "quote_",
    "executable_",
    "approximate_",
    "label_",
)

FORBIDDEN_THIS_IMPORTS = frozenset({"redis", "web3", "requests", "clickhouse_driver"})


def _parse_retrain_ast() -> ast.Module:
    return ast.parse(RETRAIN.read_text(encoding="utf-8"), filename=str(RETRAIN))


def _assign_str_list(module: ast.Module, name: str) -> list[str]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            raise AssertionError(f"{name} is not assigned a list")
        out: list[str] = []
        for elt in node.value.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise AssertionError(f"{name} contains non-string element: {ast.dump(elt)}")
            out.append(elt.value)
        return out
    raise AssertionError(f"{name} assignment not found")


def _assign_str_set(module: ast.Module, name: str) -> set[str]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.Set):
            raise AssertionError(f"{name} is not assigned a set")
        out: set[str] = set()
        for elt in node.value.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise AssertionError(f"{name} contains non-string element: {ast.dump(elt)}")
            out.add(elt.value)
        return out
    raise AssertionError(f"{name} assignment not found")


def _assign_str_tuple(module: ast.Module, name: str) -> tuple[str, ...]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.Tuple):
            raise AssertionError(f"{name} is not assigned a tuple")
        out: list[str] = []
        for elt in node.value.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise AssertionError(f"{name} contains non-string element: {ast.dump(elt)}")
            out.append(elt.value)
        return tuple(out)
    raise AssertionError(f"{name} assignment not found")


class Sprint029CRetrainGuardTests(unittest.TestCase):
    def test_feature_columns_not_silently_expanded(self) -> None:
        module = _parse_retrain_ast()
        cols = _assign_str_list(module, "FEATURE_COLUMNS")
        self.assertEqual(tuple(cols), APPROVED_FEATURE_COLUMNS)

    def test_denylist_columns_and_prefixes(self) -> None:
        module = _parse_retrain_ast()
        dcols = _assign_str_set(module, "DENYLIST_COLUMNS")
        for c in REQUIRED_DENYLIST_COLUMNS:
            self.assertIn(c, dcols, msg=f"retrain_model DENYLIST_COLUMNS missing {c}")
        prefixes = _assign_str_tuple(module, "DENYLIST_PREFIXES")
        self.assertEqual(prefixes, REQUIRED_DENYLIST_PREFIXES)

    def test_design_note_exists_and_flags(self) -> None:
        self.assertTrue(DESIGN_029C.is_file(), msg=f"Missing {DESIGN_029C}")
        text = DESIGN_029C.read_text(encoding="utf-8")
        for needle in (
            "FAILED_NO_7D_ALPHA_ACTIVE=true",
            "RETRAIN_ALLOWED=false",
            "RETRAIN_ALWAYS_SAVE_ALLOWED=false",
        ):
            self.assertIn(needle, text, msg=f"Sprint029C design missing: {needle}")

    def test_ml_training_gate_metrics_cannot_override(self) -> None:
        t = ML_GATE.read_text(encoding="utf-8")
        self.assertIn("No model metric", t)
        self.assertIn("can override this gate", t)

    def test_failed_guard_artifact_exists(self) -> None:
        self.assertTrue(FAILED_GUARD.is_file(), msg=f"Missing {FAILED_GUARD}")

    def test_retrain_no_misleading_always_save_override_claim(self) -> None:
        src = RETRAIN.read_text(encoding="utf-8")
        self.assertNotRegex(
            src,
            r"(?is)retrain_always_save.*(supersed|override).*failed_no_7d",
            msg="retrain_model must not claim RETRAIN_ALWAYS_SAVE supersedes economic guard",
        )

    def test_sprint029a_no_training_authorization_misleading(self) -> None:
        self.assertTrue(DESIGN_029A.is_file(), msg=f"Missing {DESIGN_029A}")
        a = DESIGN_029A.read_text(encoding="utf-8")
        self.assertNotIn("TRAINING_ALLOWED=true", a)
        self.assertNotIn("RETRAIN_ALLOWED=true", a)
        self.assertRegex(
            a,
            r"(?i)does\s+not\s+authorize.*training|no\s+training|TRAINING_ALLOWED=false",
        )

    def test_this_module_has_no_forbidden_imports(self) -> None:
        tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"), filename=str(THIS_FILE))
        seen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])
        bad = seen & FORBIDDEN_THIS_IMPORTS
        self.assertEqual(bad, set(), msg=f"Forbidden imports in test file: {bad}")


if __name__ == "__main__":
    unittest.main()
