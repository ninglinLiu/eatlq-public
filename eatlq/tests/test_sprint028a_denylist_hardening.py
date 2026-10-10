"""Sprint028A — Static tests for outcome-leakage denylist parity (retrain + sidecar).

No Redis, RPC, Dune, or training. Mirrors contract in scripts/retrain_model.py and
scripts/sprint027c_enriched_feature_sidecar.py.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRAIN = ROOT / "scripts" / "retrain_model.py"
SIDECAR = ROOT / "scripts" / "sprint027c_enriched_feature_sidecar.py"

FORBIDDEN_IMPORTS = frozenset({"redis", "web3", "eth_account", "dune_client"})
FORBIDDEN_REDIS_ATTRS = frozenset({
    "xadd", "xdel", "xtrim", "xclaim", "xautoclaim", "xreadgroup",
    "set", "setex", "setnx", "mset", "delete", "unlink",
    "hset", "hmset", "hdel", "sadd", "srem", "spop", "zadd", "zrem",
    "publish", "flushdb", "flushall",
})
REQUIRED_DENYLIST_COLUMNS = frozenset({
    "executable_label",
    "executable_net_return",
    "executable_status",
    "failure_reason",
    "approximate_label",
    "approximate_net_return",
    "entry_price",
    "exit_price",
})
REQUIRED_PREFIXES = (
    "future_",
    "exit_",
    "entry_",
    "quote_",
    "executable_",
    "approximate_",
    "label_",
)


def _load_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attr_calls(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _feature_would_be_denied(name: str, cols: set[str], prefixes: tuple[str, ...]) -> bool:
    if name in cols:
        return True
    return any(name.startswith(p) for p in prefixes)


class Sprint028ADenylistHardeningTests(unittest.TestCase):
    def test_retrain_denylist_contains_required_columns(self) -> None:
        spec = importlib.util.spec_from_file_location("_retrain028a", RETRAIN)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for c in REQUIRED_DENYLIST_COLUMNS:
            self.assertIn(c, mod.DENYLIST_COLUMNS, msg=f"retrain_model missing deny column {c}")
        self.assertEqual(mod.DENYLIST_PREFIXES, REQUIRED_PREFIXES)

    def test_sidecar_denylist_contains_required_columns(self) -> None:
        spec = importlib.util.spec_from_file_location("_side028a", SIDECAR)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for c in REQUIRED_DENYLIST_COLUMNS:
            self.assertIn(c, mod.DENYLIST_COLUMNS, msg=f"sidecar missing deny column {c}")
        self.assertEqual(mod.DENYLIST_PREFIXES, REQUIRED_PREFIXES)
        self.assertTrue(callable(mod._column_denied))

    def test_representative_leakage_columns_rejected_for_features(self) -> None:
        spec = importlib.util.spec_from_file_location("_retrain028b", RETRAIN)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cols = set(mod.DENYLIST_COLUMNS)
        prefixes = mod.DENYLIST_PREFIXES
        for name in ("executable_net_return", "quote_out_amount", "exit_weth", "label_smoke"):
            self.assertTrue(
                _feature_would_be_denied(name, cols, prefixes),
                msg=f"{name} should be denied as a feature name",
            )
        # Canonical feature names must not be denied
        for name in mod.FEATURE_COLUMNS:
            self.assertFalse(
                _feature_would_be_denied(name, cols, prefixes),
                msg=f"FEATURE_COLUMNS {name} must not match denylist",
            )

    def test_sidecar_column_denied_matches_leakage(self) -> None:
        spec = importlib.util.spec_from_file_location("_side028c", SIDECAR)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        deny = mod._column_denied
        self.assertTrue(deny("executable_label"))
        self.assertTrue(deny("quote_exact_out"))
        self.assertTrue(deny("future_barrier"))
        self.assertFalse(deny("buy_count_300s"))

    def test_sidecar_no_forbidden_imports_or_redis_writes(self) -> None:
        tree = _load_ast(SIDECAR)
        found_imp = _imports(tree) & FORBIDDEN_IMPORTS
        self.assertEqual(found_imp, set(), msg=f"Forbidden imports in sidecar: {found_imp}")
        bad = sorted(_attr_calls(tree) & FORBIDDEN_REDIS_ATTRS)
        self.assertEqual(bad, [], msg=f"Forbidden Redis-like calls in sidecar: {bad}")

    def test_retrain_no_redis_in_ast(self) -> None:
        tree = _load_ast(RETRAIN)
        found_imp = _imports(tree) & FORBIDDEN_IMPORTS
        self.assertEqual(found_imp, set(), msg=f"Forbidden imports in retrain_model: {found_imp}")


if __name__ == "__main__":
    unittest.main()
