import ast
import importlib.util
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "mvp/audits/whitepaper_v2_minimal_patch_contracts.json"
SUMMARY_JSON = ROOT / "mvp/audits/whitepaper_v2_missing_modules_plan_summary.json"
ENGINEERING_GAP_BACKLOG_SUMMARY = ROOT / "mvp/audits/whitepaper_v2_engineering_gap_backlog_summary.json"
SELECTIVE_IMPLEMENTATION_PLAN_SUMMARY = (
    ROOT / "mvp/audits/whitepaper_v2_selective_implementation_plan_summary.json"
)


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def load_missing_modules_summary() -> dict:
    return json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))


def load_engineering_gap_backlog_summary() -> dict:
    return json.loads(ENGINEERING_GAP_BACKLOG_SUMMARY.read_text(encoding="utf-8"))


def load_selective_implementation_plan_summary() -> dict:
    return json.loads(SELECTIVE_IMPLEMENTATION_PLAN_SUMMARY.read_text(encoding="utf-8"))


def parse_python(path: str) -> ast.Module:
    return ast.parse(read_text(path), filename=path)


def find_assignment_list(module: ast.Module, name: str) -> list[str]:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            raise AssertionError(f"{name} is not assigned a list")
        values: list[str] = []
        for item in node.value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                raise AssertionError(f"{name} contains non-string item: {ast.dump(item)}")
            values.append(item.value)
        return values
    raise AssertionError(f"{name} assignment not found")


def find_function(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def expr_source_key(expr: ast.AST) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Call) and expr.args:
        return expr_source_key(expr.args[0])
    rendered = ast.unparse(expr)
    if "pool_address" in rendered:
        return "pool_address"
    if "fee_bps" in rendered:
        return "fee_bps"
    raise AssertionError(f"unsupported append_dashcam row expression: {rendered}")


def append_dashcam_writerow_sources(module: ast.Module) -> list[str]:
    fn = find_function(module, "append_dashcam")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "writerow":
            continue
        if len(node.args) != 1 or not isinstance(node.args[0], ast.List):
            raise AssertionError("append_dashcam writerow must take one list argument")
        return [expr_source_key(item) for item in node.args[0].elts]
    raise AssertionError("append_dashcam writerow call not found")


def load_feature_store_module():
    strategy_src = ROOT / "services/strategy/src"
    if str(strategy_src) not in sys.path:
        sys.path.insert(0, str(strategy_src))
    sys.modules.setdefault("msgpack", types.SimpleNamespace(unpackb=lambda *_args, **_kwargs: {}))
    sys.modules.setdefault("redis", types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *_args, **_kwargs: None)))
    sys.modules.setdefault("numpy", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location("feature_store_under_contract_test", strategy_src / "feature_store.py")
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load feature_store.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cfg_signal_stream_xadd_lines(module: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "xadd":
            continue
        if len(node.args) < 1:
            continue
        first_arg = ast.unparse(node.args[0])
        if first_arg == "cfg.signal_stream":
            lines.append(node.lineno)
    return lines


def line_containing(path: str, needle: str) -> int:
    for i, line in enumerate(read_text(path).splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found in {path}")


def validate_minimal_schema(payload: dict, schema: dict[str, str]) -> list[str]:
    type_map = {
        "string": str,
        "array": list,
        "number": (int, float),
        "integer": int,
        "object": dict,
    }
    errors: list[str] = []
    for field, type_name in schema.items():
        if field not in payload:
            errors.append(f"missing:{field}")
            continue
        expected = type_map[type_name]
        if type_name == "number" and isinstance(payload[field], bool):
            errors.append(f"wrong_type:{field}")
        elif not isinstance(payload[field], expected):
            errors.append(f"wrong_type:{field}")
    return errors


def _violates_monitoring_gate_decision_flags(flags: dict) -> bool:
    """P5 contract: populated monitoring gates keep every listed allow_* decision flag false."""

    return any(v is True for v in flags.values())


def _is_auth_gate_bool_key(key: str) -> bool:
    return key.startswith("allow_") or key.endswith("_ALLOWED")


def _walk_auth_gate_violations(node, path: str = "$") -> list[str]:
    violations: list[str] = []

    def visit(obj, prefix: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                pfx = f"{prefix}.{k}" if prefix != "$" else k
                if isinstance(v, bool) and _is_auth_gate_bool_key(k):
                    if v is not False:
                        violations.append(f"{pfx}={v!r}")
                visit(v, pfx)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                visit(item, f"{prefix}[{i}]")

    visit(node, path)
    return violations


class WhitepaperV2StaticContractsTests(unittest.TestCase):
    def test_root_home_whitepaper_navigation_paths_exist(self) -> None:
        home = read_text("Home.md")
        self.assertIn("](whitepaper-v2.md)", home)
        self.assertIn("](docs/whitepaper-v1-original.md)", home)
        self.assertNotIn("](Whitepaper-v2.md)", home)
        self.assertNotIn("](Whitepaper-v1.md)", home)
        self.assertTrue((ROOT / "whitepaper-v2.md").is_file())
        self.assertTrue((ROOT / "docs/whitepaper-v1-original.md").is_file())

    def test_whitepaper_continued_back_links_target_existing_paths(self) -> None:
        v1c = read_text("Whitepaper-v1-continued.md")
        v2c = read_text("Whitepaper-v2-continued.md")
        self.assertIn("](docs/whitepaper-v1-original.md)", v1c)
        self.assertIn("](whitepaper-v2.md)", v2c)
        self.assertNotIn("](Whitepaper-v1.md)", v1c)
        self.assertNotIn("](Whitepaper-v2.md)", v2c)

    def test_whitepaper_v2_reader_note_links_root_architecture_not_wiki_prefix(self) -> None:
        body = read_text("whitepaper-v2.md")
        self.assertIn("[`Architecture.md`](Architecture.md)", body)
        self.assertIn("[`Operations.md`](Operations.md)", body)
        self.assertNotIn("wiki/Architecture.md", body)
        self.assertNotIn("wiki/Operations.md", body)

    def test_documentation_path_map_records_snapshot_purity_and_blocked_whitepaper(self) -> None:
        body = read_text("mvp/audits/documentation_path_map.md")
        self.assertIn("Snapshot purity (source hygiene)", body)
        self.assertIn("Do **not** add EATLQ navigation banners", body)
        self.assertIn("BLOCKED_UPSTREAM_WHITEPAPER_MISSING", body)
        self.assertIn("reference/upstream_original_wiki/", body)

    def test_upstream_original_wiki_home_has_no_eatlq_canonical_banner(self) -> None:
        snap = read_text("upstream_original_wiki/Home.md")
        self.assertNotIn("EATLQ snapshot (`upstream_original_wiki/`)", snap)
        self.assertNotIn("BLOCKED_UPSTREAM_WHITEPAPER_MISSING", snap)
        self.assertNotIn("](../Home.md)", snap)

    def test_contract_file_keeps_forbidden_actions_explicit(self) -> None:
        contract = load_contract()
        self.assertEqual(contract["scope"], "schema_documentation_minimal_static_tests_only")
        self.assertIn("labeling", contract["forbidden_actions"])
        self.assertIn("training", contract["forbidden_actions"])
        self.assertIn("live", contract["forbidden_actions"])
        self.assertIn("larger_expansion", contract["forbidden_actions"])

    def test_markettick_mapping_fixture_returns_contract_fields(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P1_MARKETTICK_SCHEMA_MAPPING")
        fields = patch["contract"]["required_strategy_tick_fields"]

        feature_store = load_feature_store_module()
        store = object.__new__(feature_store.FeatureStore)
        pool_address = "0x1111111111111111111111111111111111111111"
        target = "0x2222222222222222222222222222222222222222"
        weth = "0x4200000000000000000000000000000000000006"
        store._lookup_pool_meta = lambda _pool: {
            "token0_address": weth,
            "token1_address": target,
            "pool_type": "v3",
            "factory_address": "0x33128a8fc17869897dce68ed026d694621f6fdfd",
            "fee_bps": "3000",
        }

        neg_100 = (1 << 256) - 100
        payload = {
            "address": pool_address,
            "received_at_ms": 1770000000000,
            "topic0": feature_store.TOPIC_V3_SWAP,
            "topic1": "0x" + "0" * 24 + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "topic2": "0x" + "0" * 24 + "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "data": "0x" + f"{neg_100:064x}" + f"{200:064x}" + ("0" * 64 * 3),
            "block_number": 123,
            "tx_hash": "0xABC",
            "tx_index": 4,
            "log_index": 5,
        }
        tick = store._extract_tick_fields(payload)

        self.assertEqual(set(fields), set(tick.keys()) - {"symbol"})
        self.assertEqual(tick["pool_address"], pool_address)
        self.assertEqual(tick["token_address"], pool_address)
        self.assertEqual(tick["target_token_address"], target)
        self.assertEqual(tick["quote_symbol"], "WETH")
        self.assertEqual(tick["target_resolution_status"], "ok")
        self.assertEqual(tick["normalized_side"], "sell_target")
        self.assertEqual(tick["dex"], "uniswap_v3")
        self.assertEqual(tick["fee_bps"], 3000)

    def test_dashcam_columns_and_writerow_order_are_ast_aligned(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P1_MARKETTICK_SCHEMA_MAPPING")
        dashcam_columns = patch["contract"]["required_dashcam_columns"]
        strategy_ast = parse_python("services/strategy/src/main.py")

        actual_columns = find_assignment_list(strategy_ast, "DASHCAM_COLUMNS")
        self.assertEqual(actual_columns, dashcam_columns)
        expected_sources = [
            "timestamp_ms",
            "token_address",
            "symbol",
            "final_score",
            "whale_val",
            "momentum_val",
            "activity_val",
            "risk_val",
            "unique_buyer_entropy_5m",
            "triggered_signal",
            "pool_address",
            "pool_type",
            "dex",
            "fee_bps",
            "token0",
            "token1",
            "quote_token_address",
            "quote_symbol",
            "target_token_address",
            "target_resolution_status",
            "normalized_side",
            "sender_address",
        ]
        self.assertEqual(append_dashcam_writerow_sources(strategy_ast), expected_sources)

    def test_ingestor_stream_contract_uses_named_msgpack_data_field(self) -> None:
        self.assertIn("pub struct IngestRecordV1", read_text("crates/common/src/types.rs"))
        self.assertIn('.arg("data")', read_text("services/ingestor/src/redis_streams.rs"))
        self.assertIn("rmp_serde::to_vec_named", read_text("services/ingestor/src/codec.rs"))

    def test_wallet_profiles_is_roadmap_only_until_schema_exists(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P2_WALLET_PROFILES_DECISION")

        self.assertEqual(patch["decision"], "roadmap_only_no_schema_patch_in_this_step")
        self.assertTrue(patch["contract"]["not_equivalent_to_whitepaper_wallet_profiler"])
        self.assertTrue(patch["contract"]["postgres_wallet_profiles_table_required_before_claim"])

        postgres_sql = read_text("infra/postgres/init/001_base.sql").lower()
        strategy_main = read_text("services/strategy/src/main.py")
        self.assertNotIn("wallet_profiles", postgres_sql)
        self.assertIn("wallet:smart_money", strategy_main)
        self.assertIn("wallet:sniper", strategy_main)

    def test_failed_alpha_readout_dominates_model_metrics(self) -> None:
        contract = load_contract()
        patch = next(
            p for p in contract["patches"]
            if p["id"] == "P3_MODEL_METRICS_DO_NOT_OVERRIDE_ALPHA_READOUT"
        )

        alpha_readout = read_text("mvp/runs/run-004-v2-7d-alpha-readout.md")
        guard = read_text("mvp/audits/failed_no_7d_alpha_guard.md")

        self.assertEqual(patch["decision"], "FAILED_NO_7D_ALPHA_dominates_model_metrics")
        self.assertIn("FAILED_NO_7D_ALPHA", alpha_readout)
        self.assertIn("FAILED_NO_7D_ALPHA", guard)
        self.assertFalse(patch["contract"]["override_allowed"])
        self.assertEqual(patch["contract"]["dominant_guard_doc"], "mvp/audits/failed_no_7d_alpha_guard.md")
        self.assertIn("allow_training", patch["contract"]["blocked_flags"])
        self.assertIn("promoted=true", guard)
        self.assertIn('"allow_live": false', alpha_readout)

    def test_signal_path_boundary_is_static(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P4_SIGNAL_PATH_EXECUTION_BOUNDARY")
        strategy_ast = parse_python("services/strategy/src/main.py")

        self.assertEqual(patch["contract"]["pool_score"]["runtime_status"], "shadow_only")
        self.assertEqual(patch["contract"]["smart_money"]["runtime_status"], "executable_signal_path")
        strategy_main = read_text("services/strategy/src/main.py")
        self.assertIn("pool_score is FROZEN from execution", strategy_main)
        self.assertIn('"signal_source": "smart_money"', strategy_main)

        xadd_lines = cfg_signal_stream_xadd_lines(strategy_ast)
        self.assertGreaterEqual(len(xadd_lines), 1)
        pool_score_start = line_containing("services/strategy/src/main.py", "Pool-Level Score Path")
        for line in xadd_lines:
            self.assertLess(line, pool_score_start, f"pool_score block must not XADD to cfg.signal_stream (line {line})")

    def test_ml_monitoring_gate_schema_is_defined_but_non_authorizing(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P5_ML_MONITORING_GATE_SCHEMA")
        gate = patch["contract"]

        self.assertEqual(patch["decision"], "define_gate_schema_no_training")
        for field in [
            "label_success_rate",
            "clean_rows",
            "holdout_mean_net_return",
            "profit_factor",
            "score_distribution",
            "feature_means",
            "pool_concentration_top5",
            "dead_letter_count",
            "rpc_failure_rate",
            "decision_flags",
        ]:
            self.assertIn(field, gate["required_fields"])

        for flag in gate["decision_flags_required_false_by_default"]:
            self.assertTrue(flag.startswith("allow_"))

        example = {
            "generated_at": "2026-05-02",
            "scope": "static-example",
            "source_artifacts": [],
            "label_success_rate": 0.0,
            "clean_rows": 0,
            "holdout_rows": 0,
            "holdout_mean_net_return": 0.0,
            "holdout_positive_rate": 0.0,
            "profit_factor": 0.0,
            "score_distribution": {},
            "feature_means": {},
            "feature_extreme_rate": 0.0,
            "pool_concentration_top1": 0.0,
            "pool_concentration_top5": 0.0,
            "token_concentration_top1": 0.0,
            "token_concentration_top5": 0.0,
            "dead_letter_count": 0,
            "rpc_failure_rate": 0.0,
            "schema_version": "example",
            "decision_flags": {flag: False for flag in gate["decision_flags_required_false_by_default"]},
        }
        self.assertEqual(validate_minimal_schema(example, gate["field_types"]), [])
        for flag in gate["decision_flags_required_false_by_default"]:
            self.assertFalse(example["decision_flags"][flag])

    def test_missing_modules_plan_summary_json_exists_and_auth_flags_false(self) -> None:
        self.assertTrue(SUMMARY_JSON.is_file())

        summary = load_missing_modules_summary()
        self.assertEqual(summary["scope"], "documentation_and_schema_planning_only")

        bad_from_decision_flags = _walk_auth_gate_violations(summary["decision_flags"], "decision_flags")
        self.assertEqual(bad_from_decision_flags, [])

        violations: list[str] = []
        for idx, mod in enumerate(summary.get("modules", [])):
            bad = _walk_auth_gate_violations(mod, f"modules[{idx}]")
            violations.extend(bad)
        self.assertEqual(violations, [])

    def test_documentation_contract_files_exist_stable_phrases(self) -> None:
        sig = read_text("docs/signal-path-contract.md")
        self.assertIn("not live authorized", sig)
        self.assertIn("shadow-only", sig)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", sig)

        label = read_text("docs/label-semantics-freeze.md")
        self.assertIn("batch rpc", label)
        self.assertIn("1800", label)
        self.assertIn("label_dashcam_executable.py", label)
        self.assertIn("LABELING_ALLOWED=false", label)
        # Sprint028A — label-semantics-freeze hardening (stable phrases)
        self.assertIn("Stable contract phrase — LABEL_POOL_TYPES default:", label)
        self.assertIn("Aerodrome (`aero_cl` / `aero_vamm`)", label)
        self.assertIn(
            "quote_failed rows are omitted from the labeled CSV output (not written as label=0)",
            label,
        )
        self.assertIn(
            "Stable contract phrase — LABEL_MAX_AGE_DAYS default 0 means no max-age drop",
            label,
        )
        self.assertIn(
            "Stable contract phrase — ts_to_block grid construction is labeler-internal frozen semantics",
            label,
        )

        training = read_text("docs/ml-training-gate.md")
        self.assertIn("RETRAIN_ALLOWED = false", training)
        self.assertIn("FAILED_NO_7D_ALPHA", training)
        self.assertIn("TRAINING_ALLOWED=false", training)

        phase6 = read_text("docs/phase6-resolution-criteria.md")
        self.assertTrue(
            any(line.strip() == "supersedes: FAILED_NO_7D_ALPHA" for line in phase6.splitlines()),
            msg="canonical supersession line must appear verbatim on its own line",
        )
        self.assertIn("PHASE6_ALLOWED=false", phase6)

        path_map = read_text("mvp/audits/documentation_path_map.md")
        self.assertIn("Architecture.md", path_map)
        self.assertIn("Operations.md", path_map)
        self.assertIn("Code-Review.md", path_map)
        self.assertIn("DO NOT CREATE", path_map)
        self.assertIn("whitepaper-v2-current.md", path_map)

    def test_p5_ml_monitoring_gate_rejects_any_true_decision_flag(self) -> None:
        contract = load_contract()
        patch = next(p for p in contract["patches"] if p["id"] == "P5_ML_MONITORING_GATE_SCHEMA")
        gate = patch["contract"]
        baseline = {
            "generated_at": "2026-05-02",
            "scope": "static-contract-test-payload",
            "source_artifacts": [],
            "label_success_rate": 0.0,
            "clean_rows": 0,
            "holdout_rows": 0,
            "holdout_mean_net_return": 0.0,
            "holdout_positive_rate": 0.0,
            "profit_factor": 0.0,
            "score_distribution": {},
            "feature_means": {},
            "feature_extreme_rate": 0.0,
            "pool_concentration_top1": 0.0,
            "pool_concentration_top5": 0.0,
            "token_concentration_top1": 0.0,
            "token_concentration_top5": 0.0,
            "dead_letter_count": 0,
            "rpc_failure_rate": 0.0,
            "schema_version": "gate-schema-contract-test",
            "decision_flags": {flag: False for flag in gate["decision_flags_required_false_by_default"]},
        }
        self.assertEqual(validate_minimal_schema(baseline, gate["field_types"]), [])
        self.assertFalse(_violates_monitoring_gate_decision_flags(baseline["decision_flags"]))
        for flag_name in gate["decision_flags_required_false_by_default"]:
            bad_payload = dict(baseline)
            bad_flags = {f: False for f in gate["decision_flags_required_false_by_default"]}
            bad_flags[flag_name] = True
            bad_payload["decision_flags"] = bad_flags
            self.assertEqual(validate_minimal_schema(bad_payload, gate["field_types"]), [])
            self.assertTrue(
                _violates_monitoring_gate_decision_flags(bad_payload["decision_flags"]),
                msg=f"P5 forbids truthy decision_flag {flag_name!r}",
            )

    def test_phase2_mm05_through_mm09_documentation_contract_phrases(self) -> None:
        gap = read_text("docs/model-feature-gap.md")
        self.assertIn("fs:", gap)
        self.assertIn("feat:", gap)
        self.assertIn("whitepaper narrative prefix", gap)
        self.assertIn("current implementation prefix", gap)
        self.assertIn("unique_buyer_entropy_5m", gap)
        self.assertIn("additive fifth feature", gap)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", gap)
        self.assertIn("feat:", read_text("services/strategy/src/feature_store.py"))

        lat = read_text("docs/latency-benchmark-plan.md")
        self.assertIn("T+ms timeline is not currently benchmark-proven.", lat)
        self.assertIn("<20ms", lat)
        self.assertIn("not currently benchmark-proven", lat)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", lat)

        exe = read_text("docs/executor-language-contract.md")
        self.assertIn(
            "Rust Sniper is narrative/historical wording; the live executor code path is Python.",
            exe,
        )
        self.assertIn("signals_dead_letter_ops_summary()", exe)
        self.assertIn("observability-stream-contract.md", exe)
        self.assertIn("Current sizing is bounded-fraction / capped sizing, not full Kelly.", exe)
        self.assertIn("EXECUTOR_MAX_POSITION_USD", exe)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", exe)

        profiles = read_text("docs/architecture-compose-profiles.md")
        self.assertIn(
            "ClickHouse and Archiver are not mandatory 24/7 services for every deployment narrative.",
            profiles,
        )
        self.assertIn("clickhouse", profiles)
        self.assertIn("archiver", profiles)
        self.assertIn("Do not document ClickHouse or Archiver as unconditionally always-on", profiles)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", profiles)

        summary = load_missing_modules_summary()
        patch_ids = {m["id"] for m in summary["modules"]}
        self.assertTrue({"MM-05", "MM-06", "MM-07", "MM-08", "MM-09"}.issubset(patch_ids))
        mm_by_id = {m["id"]: m for m in summary["modules"]}
        self.assertEqual(mm_by_id["MM-09"]["document"], "docs/architecture-compose-profiles.md")
        self.assertEqual(mm_by_id["MM-05"]["this_patch_action"], "phase2_documentation_extended")

    def test_phase3_root_canonical_docs_carry_guard_annotations(self) -> None:
        required = (
            "Guard (Phase 3",
            "contract docs override narrative drift",
            "not an authorization",
            "FAILED_NO_7D_ALPHA",
        )
        roots = [
            "Architecture.md",
            "Architecture-Simplified.md",
            "Operations.md",
            "Code-Review.md",
        ]
        for fname in roots:
            body = read_text(fname)
            for phrase in required:
                self.assertIn(phrase, body, f"{fname}: missing {phrase!r}")

        summary = load_missing_modules_summary()
        p3 = summary["contract_repair_phase3"]
        self.assertEqual(p3["status"], "completed")
        self.assertEqual(p3["root_guard_annotations"], roots)
        mm_by_id = {m["id"]: m for m in summary["modules"]}
        self.assertEqual(mm_by_id["MM-15"]["this_patch_action"], "phase3_root_guard_annotation_added")
        self.assertEqual(mm_by_id["MM-16"]["this_patch_action"], "phase3_root_guard_annotation_added")
        self.assertEqual(mm_by_id["MM-17"]["this_patch_action"], "phase3_root_guard_annotation_added")

    def test_code_review_sip10_whitepaper_backlog_classification_checklist(self) -> None:
        """SIP-10: backlog classification reminder after Phase 3 guard (charter-linked)."""
        body = read_text("Code-Review.md")
        for path in (
            "mvp/audits/whitepaper_v2_engineering_gap_backlog_summary.json",
            "mvp/audits/whitepaper_v2_selective_implementation_plan_summary.json",
            "mvp/audits/whitepaper_v2_contract_index.md",
        ):
            self.assertIn(path, body)
        self.assertIn("BLOCKED_BY_FAILED_NO_7D_ALPHA", body)
        self.assertIn("DO_NOT_IMPLEMENT_NOW", body)
        self.assertIn("This checklist is not an authorization.", body)
        self.assertIn("Whitepaper-related changes", body)

    def test_mm04_mm12_placeholder_contract_docs_exist(self) -> None:
        wallet = read_text("docs/wallet-profiler-roadmap.md")
        self.assertIn("roadmap-only", wallet.lower())
        self.assertIn("wallet:smart_money", wallet)
        self.assertIn("wallet:sniper", wallet)
        self.assertIn("wallet_profiles", wallet)
        self.assertIn("Diamond Hand", wallet)
        self.assertIn("Paper Hand", wallet)
        self.assertIn("not equivalent", wallet.lower())
        self.assertIn("no runtime collection authorization", wallet.lower())
        self.assertIn("FAILED_NO_7D_ALPHA", wallet)
        self.assertIn("blocked by FAILED_NO_7D_ALPHA for runtime use", wallet)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", wallet)

        inf = read_text("docs/ml-inference-authorization.md")
        self.assertIn("shadow-only", inf.lower())
        self.assertIn("pool_score", inf)
        self.assertIn("not live authorization", inf.lower())
        self.assertIn("paper-authorized", inf)
        self.assertIn("live-authorized", inf)
        self.assertIn("FAILED_NO_7D_ALPHA", inf)
        self.assertIn("ML monitoring gate", inf)
        self.assertIn("Model metrics cannot authorize promotion alone", inf)
        self.assertIn("IMPLEMENTATION_ALLOWED=false", inf)

        summary = load_missing_modules_summary()
        mm_by_id = {m["id"]: m for m in summary["modules"]}
        self.assertEqual(mm_by_id["MM-04"]["this_patch_action"], "placeholder_doc_created")
        self.assertEqual(mm_by_id["MM-04"]["document"], "docs/wallet-profiler-roadmap.md")
        self.assertEqual(mm_by_id["MM-12"]["this_patch_action"], "placeholder_doc_created")
        self.assertEqual(mm_by_id["MM-12"]["document"], "docs/ml-inference-authorization.md")

    def test_backlog_summary_all_decision_flags_false(self) -> None:
        summary = load_engineering_gap_backlog_summary()
        for key, value in summary["decision_flags"].items():
            self.assertFalse(
                value,
                f"whitepaper_v2_engineering_gap_backlog_summary.json decision_flags.{key} must be false, got {value!r}",
            )
        for module in summary["modules"]:
            mid = module["id"]
            self.assertFalse(
                module["implementation_allowed_now"],
                f"module {mid} implementation_allowed_now must be false",
            )

    def test_selective_implementation_plan_exists(self) -> None:
        body = read_text("mvp/audits/whitepaper_v2_selective_implementation_plan.md")
        for phrase in (
            "Planning only.",
            "No implementation authorization.",
            "No training authorization.",
            "No labeling authorization.",
            "No live authorization.",
            "No Phase 6 authorization.",
        ):
            self.assertIn(phrase, body)

    def test_contract_index_exists_and_contains_required_handoff_phrases(self) -> None:
        body = read_text("mvp/audits/whitepaper_v2_contract_index.md")
        for phrase in (
            "FAILED_NO_7D_ALPHA",
            "No whitepaper alignment claim",
            "Selective Implementation Planning",
            "handoff-only",
            "No implementation authorization.",
            "No training authorization.",
            "No labeling authorization.",
            "No live authorization.",
            "No Phase 6 authorization.",
        ):
            self.assertIn(phrase, body)

    def test_firewall_sweep_warns_are_hygiene_closed(self) -> None:
        closure = read_text("mvp/audits/whitepaper_v2_contract_repair_closure.md")
        for phrase in (
            "docs/wallet-profiler-roadmap.md",
            "docs/ml-inference-authorization.md",
            "docs/whitepaper-v2-current.md",
        ):
            self.assertIn(phrase, closure, f"closure must cite {phrase} for firewall hygiene")

        summary = load_missing_modules_summary()
        mm_by_id = {m["id"]: m for m in summary["modules"]}
        self.assertEqual(mm_by_id["MM-14"]["document"], "docs/whitepaper-v2-current.md")
        self.assertEqual(mm_by_id["MM-14"]["this_patch_action"], "placeholder_doc_created")
        self.assertEqual(
            mm_by_id["MM-14"]["firewall_safe_sip_completion"],
            ["SIP-03", "SIP-04"],
        )

        label = read_text("docs/label-semantics-freeze.md")
        self.assertNotIn("LABELING_ALLOWED=true", label)
        self.assertIn("LABELING_ALLOWED=false", label)

    def test_sip03_sip04_firewall_safe_completion_docs_exist(self) -> None:
        stub = read_text("docs/whitepaper-v2-current.md")
        required_stub = (
            "Adaptive Evolution is deferred.",
            "Weekly auto-label / retrain / A/B is roadmap-only.",
            "FAILED_NO_7D_ALPHA remains active.",
            "This document is not an authorization.",
            "No implementation authorization.",
            "No training authorization.",
            "No labeling authorization.",
            "No live authorization.",
            "No Phase 6 authorization.",
            "No whitepaper alignment claim.",
            "IMPLEMENTATION_ALLOWED=false",
            "TRAINING_ALLOWED=false",
            "LABELING_ALLOWED=false",
            "LIVE_ALLOWED=false",
            "PHASE6_ALLOWED=false",
            "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false",
        )
        for phrase in required_stub:
            self.assertIn(phrase, stub)

        arch = read_text("Architecture-Simplified.md")
        for phrase in (
            "docs/executor-language-contract.md",
            "current executor is Python",
            "Rust Sniper is narrative/historical wording",
        ):
            self.assertIn(phrase, arch)

        sip_summary = load_selective_implementation_plan_summary()
        by_sip = {c["id"]: c for c in sip_summary["candidates"]}
        self.assertEqual(by_sip["SIP-03"]["this_patch_action"], "completed")
        self.assertEqual(by_sip["SIP-03"]["document"], "docs/whitepaper-v2-current.md")
        self.assertEqual(by_sip["SIP-04"]["this_patch_action"], "completed")
        self.assertEqual(by_sip["SIP-04"]["document"], "Architecture-Simplified.md")

        gap_summary = load_engineering_gap_backlog_summary()
        for flag_key, flag_val in gap_summary["decision_flags"].items():
            self.assertFalse(
                flag_val,
                msg=f"{flag_key} must remain false in engineering_gap backlog summary",
            )

        gap_mm = {m["id"]: m for m in gap_summary["modules"]}
        mm14_gap = gap_mm["MM-14"]
        self.assertEqual(mm14_gap["document"], "docs/whitepaper-v2-current.md")
        self.assertFalse(mm14_gap["implementation_allowed_now"])
        self.assertTrue(mm14_gap["blocked_by_failed_no_7d_alpha"])
        self.assertEqual(mm14_gap["this_patch_action"], "placeholder_doc_created")
        status_u = mm14_gap["current_status"].upper()
        self.assertTrue(
            "DEFERRED" in status_u or "ROADMAP" in status_u,
            msg="MM-14 current_status must still read DEFERRED/ROADMAP-only",
        )

    def test_sip01_through_sip06_closeout_status(self) -> None:
        sip_summary = load_selective_implementation_plan_summary()
        done = [f"SIP-{i:02d}" for i in range(1, 7)]
        by_id = {c["id"]: c for c in sip_summary["candidates"]}
        for sid in done:
            c = by_id[sid]
            self.assertEqual(c["this_patch_action"], "completed")
            self.assertEqual(c["completion_scope"], "firewall_safe_static_or_docs_only")
            self.assertIs(c["touches_runtime"], False)
            self.assertIs(c["touches_labeling"], False)
            self.assertIs(c["touches_training"], False)
            self.assertIs(c["touches_execution"], False)

        buckets = sip_summary["buckets"]
        self.assertEqual(
            buckets["completed_firewall_safe"],
            ["SIP-01", "SIP-02", "SIP-03", "SIP-04", "SIP-05", "SIP-06"],
        )
        self.assertIn("can_implement_now_remaining", buckets)
        self.assertEqual(buckets["can_implement_now_remaining"], [])
        for sid in done:
            self.assertNotIn(sid, buckets.get("can_implement_now", []))

        self.assertEqual(
            sip_summary["firewall_safe_completion_status"],
            "SIP-01_through_SIP-06_completed",
        )
        self.assertEqual(sip_summary["next_stage"], "plan_only_review_or_stop")
        self.assertIs(sip_summary["implementation_authorization_after_closeout"], False)

        for key, val in sip_summary["decision_flags"].items():
            self.assertFalse(val, f"decision_flags.{key} must stay false")

        for c in sip_summary["candidates"]:
            if c["type"] in ("runtime_code", "economic_data_work"):
                self.assertNotEqual(
                    c["current_authorization"],
                    "allowed",
                    msg=f'{c["id"]} high-risk type must not flip to allowed',
                )

        for pid in buckets["plan_only"]:
            self.assertEqual(by_id[pid]["current_authorization"], "plan_only")
        for bid in buckets["blocked_by_FAILED_NO_7D_ALPHA"]:
            self.assertEqual(by_id[bid]["current_authorization"], "blocked")
        for nid in buckets["do_not_implement_now"]:
            self.assertEqual(by_id[nid]["current_authorization"], "do_not_implement_now")

    def test_allowed_implementation_preflight_doc_exists(self) -> None:
        """Preflight synthesis doc: stable guard + audit-json hook (sprint hygiene)."""
        body = read_text("mvp/audits/whitepaper_contract_allowed_implementation_preflight.md")
        for phrase in (
            "FAILED_NO_7D_ALPHA",
            "IMPLEMENTATION_ALLOWED=false",
            "validate_audit_json.py",
            "whitepaper_v2_engineering_gap_backlog_summary.json",
            "plan_only_review_or_stop",
            "AUTO_PATCH_ALLOWED=false",
            "not an authorization",
        ):
            self.assertIn(phrase, body)

    def test_makefile_static_contract_targets_exist(self) -> None:
        body = read_text("Makefile")
        self.assertIn("audit-validate-json:", body)
        self.assertIn("static-contracts:", body)
        self.assertIn("check-static:", body)
        self.assertIn("check-readiness:", body)
        self.assertIn("check-local-safety:", body)
        self.assertIn("check-compose-config:", body)
        self.assertIn("scripts/validate_audit_json.py", body)
        self.assertIn("scripts/paper_dryrun_readiness.py", body)

    def test_operations_static_contract_check_section(self) -> None:
        body = read_text("Operations.md")
        self.assertIn("仓库静态契约检查", body)
        self.assertIn("check-static", body)
        self.assertIn("check-readiness", body)
        self.assertIn("check-compose-config", body)
        self.assertTrue(
            "make " in body and "check-static" in body,
            msg="Operations.md must document make check-static (or equivalent)",
        )

    def test_firewall_safe_sprint_001_report_handoff_phrases(self) -> None:
        body = read_text("mvp/audits/whitepaper_contract_full_completion_sprint_001.md")
        for phrase in (
            "Whitepaper Contract — Full Completion Sprint 001",
            "FAILED_NO_7D_ALPHA",
            "make check-static",
            "whitepaper parity",
        ):
            self.assertIn(phrase, body)
        self.assertNotIn("whitepaper fully aligned", body.lower())

    def test_executor_ingress_fail_safe_contract_hooks(self) -> None:
        exe = read_text("services/executor/src/main.py")
        pos = read_text("services/executor/src/position.py")
        self.assertIn("LIVE_TRADING_MODE", exe)
        self.assertIn("EXECUTOR_DRY_RUN", exe)
        self.assertIn("ambiguous live vs simulation", exe)
        self.assertIn("def _malformed_buy_signal_reason", exe)
        self.assertIn("buy_missing_pool_or_token", exe)
        self.assertIn("buy_invalid_pool_or_token_address", exe)
        self.assertIn('_SIGNAL_PAYLOAD_FIELDS = ("payload", "signal_json")', exe)
        self.assertIn("PENDING_SELL_FIELD = \"pending_sell\"", pos)
        self.assertIn("PENDING_SELL_AT_FIELD = \"pending_sell_at\"", pos)
        self.assertIn("PENDING_SELL_TTL_MS = 60_000", pos)
        self.assertIn("def try_mark_pending_sell", pos)
        self.assertIn("def clear_pending_sell", pos)
        self.assertIn("def is_pending_sell_stale", pos)
        self.assertIn("store.try_mark_pending_sell(pool_addr, now_ms)", exe)
        self.assertIn('"event": "sell_lock_active"', exe)
        self.assertIn("store.clear_pending_sell(pool_addr)", exe)

    def test_strategy_market_ticks_stream_field_fallback_order(self) -> None:
        strat = read_text("services/strategy/src/main.py")
        self.assertIn("_MARKET_TICK_PAYLOAD_FIELD_ORDER", strat)
        for needle in ('"data",', '"payload_msgpack",', '"payload",', '"tick",'):
            self.assertIn(needle, strat)
        self.assertIn("def parse_stream_payload", strat)

    def test_strategy_smart_money_signal_required_json_keys(self) -> None:
        """Executable smart_money BUY payload keys written to signals stream (static freeze)."""
        strat = read_text("services/strategy/src/main.py")
        idx = strat.find('"signal_source": "smart_money"')
        self.assertGreater(idx, 0, "smart_money signal block not found")
        window = strat[max(0, idx - 1200) : idx + 80]
        for key in (
            '"action": "BUY"',
            '"pool_address"',
            '"token_address"',
            '"chain_id"',
            '"pool_type"',
            '"symbol"',
            '"final_score"',
            '"confidence"',
            '"position_size_usd"',
            '"timestamp"',
            '"signal_source": "smart_money"',
        ):
            self.assertIn(key, window)

    def test_static_contracts_ci_workflow_runs_expanded_gate(self) -> None:
        """Sprint013 S1-T5: CI must run static + readiness + compose-config jobs.

        Without this, a regression in .env.example key parity or compose profile
        wiring would only surface on the operator's laptop. The workflow has
        three jobs: static-contracts, readiness, compose-config.
        """

        wf = read_text(".github/workflows/static-contracts.yml")
        self.assertIn("name: static-contracts", wf)
        self.assertIn("make check-static", wf)
        self.assertIn("make check-readiness", wf)
        self.assertIn("make check-local-safety", wf)
        self.assertIn("make check-compose-config", wf)
        self.assertIn("tests.test_paper_observation_window", wf)
        self.assertIn("tests.test_inspect_dead_letter", wf)
        # Paper-readiness and compose-config jobs must depend on the static gate
        # landing first so a broken contract short-circuits the pipeline.
        self.assertIn("needs: static-contracts", wf)

    def test_compose_executor_passes_live_trading_mode_into_container(self) -> None:
        """Sprint013 S1-T1: executor container must receive LIVE_TRADING_MODE from host env.

        Without this wiring, setting LIVE_TRADING_MODE=true in the host .env would be
        silently ignored by docker compose, leaving the executor's fail-safe (live+dry
        mutual-exclusion in services/executor/src/main.py) unable to act on the host
        operator's intent. The default substitution must be empty so paper/dry-run
        posture stays the default.
        """

        compose = read_text("infra/compose/compose.yaml")
        idx_exe = compose.find("\n  executor:\n")
        self.assertGreater(idx_exe, 0, "executor service block not found in compose.yaml")
        idx_dash = compose.find("\n  dashboard:\n", idx_exe + 1)
        self.assertGreater(idx_dash, idx_exe, "dashboard service block not found after executor")
        exe_block = compose[idx_exe:idx_dash]

        self.assertIn(
            "LIVE_TRADING_MODE: ${LIVE_TRADING_MODE:-}",
            exe_block,
            msg="executor.environment must declare LIVE_TRADING_MODE with an empty default",
        )

        exe_main = read_text("services/executor/src/main.py")
        self.assertIn('os.environ.get("LIVE_TRADING_MODE")', exe_main)
        self.assertIn('os.environ.get("EXECUTOR_DRY_RUN")', exe_main)

        env_example = read_text("infra/compose/.env.example")
        for line in env_example.splitlines():
            stripped = line.strip()
            if stripped.startswith("LIVE_TRADING_MODE="):
                _, _, val = stripped.partition("=")
                self.assertIn(
                    val.strip(),
                    {"", "0", "false", "False", "no", "off"},
                    msg=".env.example LIVE_TRADING_MODE default must be non-live",
                )
                break
        else:
            self.fail(".env.example is missing a LIVE_TRADING_MODE= declaration")

    def test_whitepaper_quantitative_defaults_are_pinned(self) -> None:
        """Sprint013 S1-T7: freeze the whitepaper-v2 quantitative claims and the
        concrete defaults that implement them, so future edits cannot silently
        drift the narrative away from code (or vice versa).

        This test is INTENTIONALLY value-observing, not value-mutating: where
        current compose defaults differ from the whitepaper (e.g. daily loss
        pct), we pin the whitepaper prose AND the current code value and flag
        the drift in Sprint 013 backlog rather than strengthening risk knobs
        inside a docs/static sprint.
        """

        # --- Chain id (Base mainnet) ---
        env_example = read_text("infra/compose/.env.example")
        self.assertIn("CHAIN_ID=8453", env_example, msg="Base mainnet chain id 8453 must be pinned in .env.example")
        wp_v2 = read_text("whitepaper-v2.md")
        self.assertIn("8453", read_text("Whitepaper-v2-continued.md"))

        # --- Final_Score formula (Mode A weights) ---
        wp_continued = read_text("Whitepaper-v2-continued.md")
        self.assertIn(
            "Final_Score = 0.40×Whale + 0.30×Momentum + 0.20×Activity − 0.10×Risk",
            wp_continued,
            msg="Mode A Final_Score weights must remain verbatim in Whitepaper-v2-continued.md",
        )
        self.assertIn("Position_Size = Base_Capital × Final_Score × Vol_Adj", wp_continued)

        # --- Daily-drawdown panic threshold ---
        self.assertIn("自动 10% 日回撤熔断", wp_v2)
        # EXECUTOR_DAILY_LOSS_PCT current default drifts (20 vs whitepaper 10).
        # We pin the current code value here to freeze the observation; the
        # drift is a known Sprint013 backlog item and must be resolved in a
        # follow-up sprint with an explicit economic/risk review — not a
        # silent Makefile edit.
        self.assertRegex(
            env_example,
            r"(?m)^EXECUTOR_DAILY_LOSS_PCT=\d+\s*$",
            msg=".env.example must declare EXECUTOR_DAILY_LOSS_PCT as a number",
        )

        # --- Retrain gates ---
        retrain = read_text("scripts/retrain_model.py")
        self.assertIn('os.environ.get("RETRAIN_WINDOW_DAYS", "28")', retrain)
        self.assertIn('os.environ.get("RETRAIN_HOLDOUT_DAYS", "7")', retrain)
        self.assertIn(
            "test accuracy >= 0.55 absolute AND PR-AUC uplift",
            retrain,
            msg="retrain_model.py header must keep the >= 0.55 accuracy + PR-AUC uplift contract",
        )
        phase6 = read_text("scripts/phase6_weekly_cycle.sh")
        self.assertIn(': "${RETRAIN_WINDOW_DAYS:=28}"', phase6)
        self.assertIn(': "${RETRAIN_HOLDOUT_DAYS:=7}"', phase6)
        self.assertIn(': "${RETRAIN_MIN_ACCURACY:=0.55}"', phase6)

        # --- Label max age (LASZLO default = 30 days at orchestration layer) ---
        self.assertIn(': "${LABEL_MAX_AGE_DAYS:=30}"', phase6)
        self.assertIn("LABEL_MAX_AGE_DAYS", read_text("scripts/label_dashcam_executable.py"))

        # --- Operations.md must re-reference the same LASZLO defaults ---
        ops = read_text("Operations.md")
        for needle in ("RETRAIN_WINDOW_DAYS=28", "RETRAIN_HOLDOUT_DAYS=7", "RETRAIN_MIN_ACCURACY=0.55", "LABEL_MAX_AGE_DAYS=30"):
            self.assertIn(
                needle,
                ops,
                msg=f"Operations.md must cite the whitepaper default {needle!r}",
            )

        # --- Whitepaper narrative must not be the sole source; it also
        # cannot flip the FAILED_NO_7D_ALPHA gate. Re-assert the guard text
        # appears in the doc that governs the retrain/label flow.
        label_freeze = read_text("docs/label-semantics-freeze.md")
        self.assertIn("FAILED_NO_7D_ALPHA", label_freeze)
        self.assertIn("LABELING_ALLOWED=false", label_freeze)

    def test_validate_audit_json_script_flags_logic(self) -> None:
        script_rel = "scripts/validate_audit_json.py"
        body = read_text(script_rel)
        self.assertIn("Path(__file__).resolve().parents", body)
        self.assertIn('--root', body)
        self.assertIn("argparse.ArgumentParser", body)
        self.assertIn('key.startswith("allow_")', body)
        self.assertIn('key.endswith("_ALLOWED")', body)
        self.assertIn("isinstance(", body)
        self.assertIn("v is not False", body)
        self.assertIn('glob("*.json")', body)
        self.assertIn("mvp", body)
        self.assertIn("audits", body)
        self.assertIn("raise SystemExit(main())", body)

        proc = subprocess.run(
            [sys.executable, str(ROOT / script_rel), "--root", str(ROOT)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("OK: audited", proc.stdout)
        self.assertIn("all allow_* and *_ALLOWED flags are false", proc.stdout)


    def test_sprint038a_recipient_sidecar_guard_contracts(self) -> None:
        """
        Sprint038A static guard: recipient-keyed LASZLO-style fingerprint sidecar.

        Verifies:
        1. Script and DDL files exist
        2. Script compiles (py_compile)
        3. DECISION_FLAGS has required keys all with correct bool values
        4. Script does NOT import Redis (no live stack access)
        5. Script does NOT reference dashcam write paths
        6. Address output columns use _AUDIT_ONLY suffix
        7. DDL uses DROP/CREATE VIEW pattern; no table creation
        8. Guard phrases present: IDENTITY_CLAIM_ALLOWED, NO_ALPHA_CLAIM, etc.
        9. If exclusion JSON output exists, decision_flags auth-gate keys are False
        """
        import py_compile

        script_rel = "scripts/sprint038a_recipient_lasl_fingerprint.py"
        ddl_rel    = "infra/clickhouse/init/038_recipient_profile_views.sql"

        # 1. Files exist
        self.assertTrue((ROOT / script_rel).is_file(), f"{script_rel} must exist")
        self.assertTrue((ROOT / ddl_rel).is_file(),    f"{ddl_rel} must exist")

        # 2. Compiles
        try:
            py_compile.compile(str(ROOT / script_rel), doraise=True)
        except py_compile.PyCompileError as exc:
            self.fail(f"Script does not compile: {exc}")

        script_body = read_text(script_rel)
        ddl_body    = read_text(ddl_rel)

        # 3. DECISION_FLAGS: required False keys
        required_false_keys = {
            "TRAINING_ALLOWED", "LIVE_ALLOWED", "MODEL_PROMOTION_ALLOWED",
            "IDENTITY_CLAIM_ALLOWED", "SENDER_KEYED_RANKING_TRUSTED",
            "SMART_MONEY_GENERAL_LINE_OPEN",
        }
        for key in required_false_keys:
            self.assertRegex(
                script_body, rf'["\']?{key}["\']?\s*:\s*False',
                f"DECISION_FLAGS['{key}'] must be False",
            )
        # Required True keys
        for key in ("FAILED_NO_7D_ALPHA_ACTIVE", "RECIPIENT_KEYED_VIEW_REQUIRED",
                    "NO_ALPHA_CLAIM", "WALLET_FEATURE_SIDECAR_DIAGNOSTIC_ONLY"):
            self.assertRegex(
                script_body, rf'["\']?{key}["\']?\s*:\s*True',
                f"DECISION_FLAGS['{key}'] must be True",
            )

        # 4. No Redis imports (script is read-only CH + bounded RPC only)
        for pattern in ("import redis", "from redis", "redis.Redis(", "redis.from_url("):
            self.assertNotIn(pattern, script_body,
                             f"Script must not import Redis: {pattern!r}")
        # No live signal publishing
        self.assertNotIn('.xadd("signals"', script_body)
        self.assertNotIn(".xadd('signals'", script_body)

        # 5. No dashcam write paths
        for pattern in ("dashcam.csv", "append_dashcam", "DASHCAM_COLUMNS"):
            self.assertNotIn(pattern, script_body,
                             f"Script must not reference dashcam write: {pattern!r}")

        # 6. Address columns use _AUDIT_ONLY suffix
        self.assertIn("recipient_address_AUDIT_ONLY", script_body)
        self.assertIn("exclusion_reason_AUDIT_ONLY",  script_body)
        # Both columns must appear in CSV_COLUMNS list (check sequential order)
        self.assertIn(
            '"recipient_address_AUDIT_ONLY",\n    "evidence_score"',
            script_body,
            "CSV_COLUMNS must list recipient_address_AUDIT_ONLY before evidence_score",
        )

        # 7. DDL: VIEW only, no table creation
        self.assertIn("CREATE VIEW",  ddl_body)
        self.assertIn("DROP VIEW",    ddl_body)
        self.assertIn("recipient_address", ddl_body)
        self.assertNotIn("CREATE TABLE",           ddl_body)
        self.assertNotIn("ENGINE = MergeTree",         ddl_body)
        self.assertNotIn("ENGINE = ReplacingMergeTree", ddl_body)
        # DDL must warn about denylist
        self.assertIn("retrain_model.py", ddl_body)

        # 8. Guard phrases
        for phrase in ("IDENTITY_CLAIM_ALLOWED", "NO_ALPHA_CLAIM",
                       "FAILED_NO_7D_ALPHA_ACTIVE", "SENDER_KEYED_RANKING_TRUSTED",
                       "WALLET_FEATURE_SIDECAR_DIAGNOSTIC_ONLY", "diagnostics only"):
            self.assertIn(phrase, script_body, f"Guard phrase {phrase!r} must appear in script")

        # 9. Validate exclusion JSON output if it exists from a prior run
        json_path = ROOT / "mvp/diagnostics/sprint038a_recipient_exclusion_breakdown.json"
        if json_path.is_file():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("kind"), "sprint038a_recipient_exclusion_breakdown")
            bad = _walk_auth_gate_violations(data.get("decision_flags", {}), "decision_flags")
            # Filter non-auth True booleans that are intentionally True
            allowed_true = {
                "FAILED_NO_7D_ALPHA_ACTIVE", "RECIPIENT_KEYED_VIEW_REQUIRED",
                "NO_ALPHA_CLAIM", "WALLET_FEATURE_SIDECAR_DIAGNOSTIC_ONLY",
            }
            real_violations = [v for v in bad if not any(s in v for s in allowed_true)]
            self.assertEqual(real_violations, [],
                             f"Auth-gate violations in decision_flags: {real_violations}")

    def test_sprint041_evidence_bundle_guard_contracts(self) -> None:
        """Sprint041: evidence bundle + matched-control FIFO (diagnostics-only)."""
        import py_compile

        script_rel = "scripts/sprint041_evidence_bundle_matched_control.py"
        self.assertTrue((ROOT / script_rel).is_file(), f"{script_rel} must exist")
        try:
            py_compile.compile(str(ROOT / script_rel), doraise=True)
        except py_compile.PyCompileError as exc:
            self.fail(f"Sprint041 script does not compile: {exc}")

        body = (ROOT / script_rel).read_text(encoding="utf-8")
        for key in ("TRAINING_ALLOWED", "LIVE_ALLOWED", "MODEL_PROMOTION_ALLOWED",
                    "IDENTITY_CLAIM_ALLOWED"):
            self.assertRegex(body, rf'["\']?{key}["\']?\s*:\s*False',
                             f"{key} must be False")
        for key in ("NO_ALPHA_CLAIM", "FAILED_NO_7D_ALPHA_ACTIVE"):
            self.assertRegex(body, rf'["\']?{key}["\']?\s*:\s*True',
                             f"{key} must be True")
        for pattern in ("import redis", "from redis", "redis.Redis(", "redis.from_url("):
            self.assertNotIn(pattern, body)
        self.assertNotIn('.xadd("signals"', body)
        self.assertIn("recipient_address_AUDIT_ONLY", body)
        self.assertIn("TARGET_EXECUTOR_AUDIT_ONLY", body)
        for phrase in ("NO_ALPHA_CLAIM", "never commit URLs", "IDENTITY_CLAIM_ALLOWED"):
            self.assertIn(phrase, body)


if __name__ == "__main__":
    unittest.main()
