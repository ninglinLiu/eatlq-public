"""Run-004 Patch V3R-14 — research-only V3 labeler bridge pilot.

Small, reversible pilot only. It reads V3R-13C artifacts and the existing
V3R-12 novel-pool candidate cache, then writes one JSON and one Markdown report.
No production signals, model artifacts, live mode, or promotion paths.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.v3_quoterv2_struct import (  # noqa: E402
    STRUCT_SELECTOR,
    WETH_BASE,
    JsonRpcClient,
    quote_exact_input_single_struct,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

CANONICAL_MD = ROOT / "mvp/runs/run-004-patch-v3r13c-quoterv2-struct-fix-405-rerun.md"
CANONICAL_JSON = ROOT / "mvp/diagnostics/run004_v3r13c_quoterv2_struct_fix_summary.json"
ALIAS_MD = ROOT / "mvp/runs/run-004-patch-v3r13c-quoter-v2-abi-rerun.md"
ALIAS_JSON = ROOT / "mvp/diagnostics/run004_v3r13c_quoter_v2_abi_rerun_summary.json"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r14_v3_labeler_bridge_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r14-v3-labeler-bridge.md"

PILOT_POOL_LIMIT = int(os.environ.get("V3R14_PILOT_POOL_LIMIT", "50"))
BLOCK_SEMANTICS_LIMIT = int(os.environ.get("V3R14_BLOCK_SEMANTICS_LIMIT", "10"))
FIXED_WETH_RAW = int(float(os.environ.get("V3R14_FIXED_WETH", "0.0005")) * 10**18)
LIQUIDITY_SELECTOR = "1a686502"


def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _rpc_url() -> str:
    _load_env_file()
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url:
        raise RuntimeError("RPC URL must be in LABEL_RPC_URL or EVM_HTTP_URL")
    return url


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _pad_addr(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(64, "0")


def _decode_first_uint(res: str | None) -> int:
    if not res or res == "0x":
        return 0
    raw = res[2:] if res.startswith("0x") else res
    return int(raw[:64], 16) if len(raw) >= 64 else 0


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def artifact_audit() -> dict[str, Any]:
    canonical = load_json(CANONICAL_JSON)
    alias = load_json(ALIAS_JSON)
    quote_result_fields = [
        "quote_selector",
        "fixed_weth_sizes",
        "quote_path_audit",
        "positive_control",
        "novel_405_rerun",
        "block_semantics",
    ]
    compared_fields = [
        *quote_result_fields,
        "decision_flags",
    ]
    comparisons = {}
    if canonical and alias:
        for field in compared_fields:
            comparisons[field] = canonical.get(field) == alias.get(field)
    inconsistent_fields = [field for field, ok in comparisons.items() if not ok]
    return {
        "canonical_paths": {
            "markdown": str(CANONICAL_MD.relative_to(ROOT)),
            "json": str(CANONICAL_JSON.relative_to(ROOT)),
        },
        "paths_exist": {
            str(CANONICAL_MD.relative_to(ROOT)): CANONICAL_MD.exists(),
            str(CANONICAL_JSON.relative_to(ROOT)): CANONICAL_JSON.exists(),
            str(ALIAS_MD.relative_to(ROOT)): ALIAS_MD.exists(),
            str(ALIAS_JSON.relative_to(ROOT)): ALIAS_JSON.exists(),
        },
        "both_artifact_sets_exist": all([CANONICAL_MD.exists(), CANONICAL_JSON.exists(), ALIAS_MD.exists(), ALIAS_JSON.exists()]),
        "duplicate_artifact_mapping": {
            "alias_markdown": str(ALIAS_MD.relative_to(ROOT)),
            "alias_json": str(ALIAS_JSON.relative_to(ROOT)),
            "canonical_markdown": str(CANONICAL_MD.relative_to(ROOT)),
            "canonical_json": str(CANONICAL_JSON.relative_to(ROOT)),
        },
        "core_field_comparison": comparisons,
        "quote_result_fields_consistent": bool(comparisons) and all(comparisons.get(field) for field in quote_result_fields),
        "inconsistent_fields": inconsistent_fields,
        "non_core_differences": {
            "run": [alias.get("run") if alias else None, canonical.get("run") if canonical else None],
            "head_block": [alias.get("head_block") if alias else None, canonical.get("head_block") if canonical else None],
        },
    }


def quote_path_audit() -> dict[str, Any]:
    return {
        "production_runtime": [
            {
                "path": "services/strategy/src/main.py",
                "classification": "production/runtime",
                "status": "Web3 tuple QuoterV2 struct ABI; no V3R-14 behavior change",
                "old_manual_selector_active": False,
            },
            {
                "path": "services/executor/src/main.py",
                "classification": "production/runtime",
                "status": "Web3 tuple QuoterV2 struct ABI; no V3R-14 behavior change",
                "old_manual_selector_active": False,
            },
            {
                "path": "services/executor/src/swap.py",
                "classification": "production/runtime",
                "status": "Web3 tuple QuoterV2 struct ABI; no V3R-14 behavior change",
                "old_manual_selector_active": False,
            },
        ],
        "labeler_data_supply": [
            {
                "path": "scripts/run004_v3r12_v3_data_supply.py::_call_quoter",
                "classification": "labeler/data-supply",
                "status": "migrated to manual struct selector c6a5026a",
                "old_manual_selector_active": False,
            },
            {
                "path": "scripts/label_dashcam_executable.py::call_quoter_v3",
                "classification": "labeler/data-supply",
                "status": "manual struct selector 0xc6a5026a",
                "old_manual_selector_active": False,
            },
            {
                "path": "scripts/label_dashcam.py::_quote_v3_buy/_quote_v3_sell",
                "classification": "labeler/data-supply",
                "status": "Web3 tuple QuoterV2 struct ABI",
                "old_manual_selector_active": False,
            },
        ],
        "diagnostic_research": [
            {
                "path": "scripts/run004_v3r13a_fixed_notional_quote_sanity.py::_quote",
                "classification": "diagnostic/research",
                "status": "migrated to manual struct selector c6a5026a",
                "old_manual_selector_active": False,
            },
            {
                "path": "scripts/run004_v3r13b_tradability_falsification.py::v3r13a_exact",
                "classification": "legacy comparison only",
                "status": "falsified f7729d43 comparison/control path retained only for diagnosis",
                "old_manual_selector_active": True,
            },
            {
                "path": "scripts/run004_v3r13b_tradability_falsification.py::legacy_fee_amount_order",
                "classification": "legacy comparison only",
                "status": "legacy f7729d43 fee-before-amount comparison/control path retained only for diagnosis",
                "old_manual_selector_active": True,
            },
        ],
        "historical_artifacts": [
            "mvp/runs/* and mvp/diagnostics/* contain historical mentions of f7729d43/V3R-13A as falsified evidence.",
        ],
    }


def load_novel_unique() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        pool = _lower(row.get("pool_address"))
        token0 = _lower(row.get("token0"))
        token1 = _lower(row.get("token1"))
        if not pool or WETH_BASE not in (token0, token1):
            continue
        weth_amt = int(float(row.get("weth_side_amount_raw") or 0))
        if pool not in by_pool or weth_amt > by_pool[pool].get("_weth_amt", 0):
            by_pool[pool] = {
                "pool_address": pool,
                "token0": token0,
                "token1": token1,
                "target": token1 if token0 == WETH_BASE else token0,
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    return list(by_pool.values())


def read_liquidity(rpc: JsonRpcClient, pool: str, block: int | str = "latest") -> int:
    block_tag = hex(block) if isinstance(block, int) else block
    try:
        res = rpc.call("eth_call", [{"to": pool, "data": "0x" + LIQUIDITY_SELECTOR}, block_tag], timeout=20)
        return _decode_first_uint(res)
    except Exception:
        return -1


def quote_roundtrip(rpc: JsonRpcClient, row: dict[str, Any], block: int | str) -> dict[str, Any]:
    buy = quote_exact_input_single_struct(rpc, WETH_BASE, row["target"], FIXED_WETH_RAW, int(row["fee"]), block)
    if not buy["ok"]:
        reason = buy.get("reason") or "entry_quote_fail"
        liquidity = read_liquidity(rpc, row["pool_address"], block)
        if liquidity == 0:
            reason = "zero_liquidity"
        return {"status": "entry_quote_fail", "entry": buy, "exit": None, "reason": reason}
    sell = quote_exact_input_single_struct(rpc, row["target"], WETH_BASE, int(buy["amount_out"]), int(row["fee"]), block)
    if not sell["ok"]:
        reason = sell.get("reason") or "exit_quote_fail"
        liquidity = read_liquidity(rpc, row["pool_address"], block)
        if liquidity == 0:
            reason = "zero_liquidity"
        return {"status": "exit_quote_fail", "entry": buy, "exit": sell, "reason": reason}
    return {
        "status": "roundtrip_success",
        "entry": buy,
        "exit": sell,
        "reason": "",
        "gross_return": round(int(sell["amount_out"]) / FIXED_WETH_RAW - 1.0, 8),
    }


def run_pilot(rpc: JsonRpcClient, pools: list[dict[str, Any]]) -> dict[str, Any]:
    sample = pools[:PILOT_POOL_LIMIT]
    rows = []
    for idx, row in enumerate(sample, start=1):
        result = quote_roundtrip(rpc, row, "latest")
        rows.append(
            {
                "idx": idx,
                "pool_address": row["pool_address"],
                "target": row["target"],
                "fee": row["fee"],
                "entry_block": row["entry_block"],
                "status": result["status"],
                "reason": result.get("reason", ""),
                "entry_ok": bool(result["entry"]["ok"]),
                "exit_ok": bool(result["exit"] and result["exit"]["ok"]),
                "entry_amount_out": result["entry"].get("amount_out", 0),
                "exit_amount_out": result["exit"].get("amount_out", 0) if result["exit"] else 0,
                "gross_return": result.get("gross_return"),
            }
        )
        if idx % 10 == 0:
            print(f"[pilot] {idx}/{len(sample)} pools")
    entry_success = sum(1 for r in rows if r["entry_ok"])
    roundtrip_success = sum(1 for r in rows if r["status"] == "roundtrip_success")
    reason_counts = Counter(r["reason"] or r["status"] for r in rows if r["status"] != "roundtrip_success")
    return {
        "sample_source": str(NOVEL_CSV.relative_to(ROOT)),
        "sample_note": "First 50 unique WETH-paired novel pools from V3R-12 candidate cache, bounded by V3R-13C aggregate 402/405 success evidence; no 405 rerun.",
        "fixed_weth": 0.0005,
        "amount_in_raw": FIXED_WETH_RAW,
        "pools": len(rows),
        "entry_quote_success": entry_success,
        "entry_quote_success_rate": round(entry_success / max(1, len(rows)), 4),
        "roundtrip_success": roundtrip_success,
        "roundtrip_success_rate": round(roundtrip_success / max(1, len(rows)), 4),
        "failure_reason_counts": dict(reason_counts),
        "rows": rows,
    }


def run_block_semantics(rpc: JsonRpcClient, pools: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for row in pools[:BLOCK_SEMANTICS_LIMIT]:
        tests = {}
        for label, block in {
            "entry_block_minus_1": max(0, int(row["entry_block"]) - 1),
            "entry_block": int(row["entry_block"]),
            "head": "latest",
        }.items():
            q = quote_roundtrip(rpc, row, block)
            tests[label] = {
                "status": q["status"],
                "reason": q.get("reason", ""),
                "entry_ok": bool(q["entry"]["ok"]),
                "exit_ok": bool(q["exit"] and q["exit"]["ok"]),
            }
        statuses = {k: v["status"] for k, v in tests.items()}
        rows.append(
            {
                "pool_address": row["pool_address"],
                "fee": row["fee"],
                "entry_block": row["entry_block"],
                "tests": tests,
                "block_sensitive_failure": len(set(statuses.values())) > 1,
            }
        )
    return {
        "sample_size": len(rows),
        "block_sensitive_count": sum(1 for r in rows if r["block_sensitive_failure"]),
        "entry_block_minus_1_roundtrip_success": sum(1 for r in rows if r["tests"]["entry_block_minus_1"]["status"] == "roundtrip_success"),
        "entry_block_roundtrip_success": sum(1 for r in rows if r["tests"]["entry_block"]["status"] == "roundtrip_success"),
        "head_roundtrip_success": sum(1 for r in rows if r["tests"]["head"]["status"] == "roundtrip_success"),
        "rows": rows,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    art = summary["v3r13c_artifact_audit"]
    pilot = summary["pilot"]
    block = summary["block_semantics"]
    flags = summary["decision_flags"]
    audit = summary["quote_path_audit"]
    labeler_lines = "\n".join(
        f"- `{item['path']}`: {item['status']}" for item in audit["labeler_data_supply"]
    )
    diag_lines = "\n".join(
        f"- `{item['path']}`: {item['status']}" for item in audit["diagnostic_research"]
    )
    runtime_lines = "\n".join(
        f"- `{item['path']}`: {item['status']}" for item in audit["production_runtime"]
    )
    failure_counts = pilot["failure_reason_counts"] or {}
    failure_lines = "\n".join(f"- `{k}`: {v}" for k, v in failure_counts.items()) or "- none"
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-14 — V3 Labeler Bridge Pilot

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor runtime behavior change. No H19. No retrain.

## 1. V3R-13C Artifact Canonicalization

- canonical markdown: `{art['canonical_paths']['markdown']}`
- canonical JSON: `{art['canonical_paths']['json']}`
- both artifact sets exist: **{art['both_artifact_sets_exist']}**
- quote/result fields consistent: **{art['quote_result_fields_consistent']}**
- inconsistent fields: **{art['inconsistent_fields']}**
- duplicate alias markdown: `{art['duplicate_artifact_mapping']['alias_markdown']}`
- duplicate alias JSON: `{art['duplicate_artifact_mapping']['alias_json']}`
- differences: `run/head_block` differ by artifact naming/run timing; `decision_flags` use different schemas.

Core field comparison:
```json
{json.dumps(art['core_field_comparison'], indent=2, ensure_ascii=False)}
```

## 2. Quote Path Audit

Production/runtime paths were audited only; V3R-14 did not change runtime behavior.

{runtime_lines}

Labeler/data-supply active paths:

{labeler_lines}

Diagnostic / legacy comparison paths:

{diag_lines}

Conclusion: active labeler/data-supply has no old `f7729d43` manual ABI path. The remaining old path is retained only as V3R-13B falsified/legacy comparison evidence.

## 3. 50-Pool Labeler Bridge Pilot

- sample source: `{pilot['sample_source']}`
- sample size: **{pilot['pools']}**
- fixed notional: **0.0005 WETH**
- selector: `{summary['quote_selector']}`
- entry quote success: **{pilot['entry_quote_success']} / {pilot['pools']}** (`{pilot['entry_quote_success_rate']}`)
- round-trip success: **{pilot['roundtrip_success']} / {pilot['pools']}** (`{pilot['roundtrip_success_rate']}`)
- failure counts:
{failure_lines}

Block semantics sample:
- sample size: **{block['sample_size']}**
- entry_block-1 round-trip success: **{block['entry_block_minus_1_roundtrip_success']} / {block['sample_size']}**
- entry_block round-trip success: **{block['entry_block_roundtrip_success']} / {block['sample_size']}**
- head round-trip success: **{block['head_roundtrip_success']} / {block['sample_size']}**
- block-sensitive failure count: **{block['block_sensitive_count']}**

## 4. Required Answers

1. V3R-13C artifact 命名存在重复；canonical 路径为 `{art['canonical_paths']['markdown']}` 与 `{art['canonical_paths']['json']}`。quote/result 字段一致，但 `decision_flags` 字段不一致，V3R-14 以 canonical 路径为准并记录 alias mapping。
2. 旧错误 quote path 仍存在于 `scripts/run004_v3r13b_tradability_falsification.py`，仅为 falsified / legacy comparison only；历史报告也保留引用。
3. 当前 labeler/data-supply 已迁移到 struct ABI；`old_quote_path_in_active_labeler = false`。
4. 50-pool pilot entry quote 成功率为 `{pilot['entry_quote_success_rate']}`。
5. 50-pool pilot round-trip 成功率为 `{pilot['roundtrip_success_rate']}`。
6. block-sensitive failure count 为 `{block['block_sensitive_count']}`。
7. 允许进入 V3R-15 bounded V3 dataset expansion: **{flags['allow_v3r15_bounded_dataset_expansion']}**。
8. 允许 model research: **False**。
9. 允许 mainline / live / promote: **False**。

## 5. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    artifacts = artifact_audit()
    audit = quote_path_audit()
    rpc = JsonRpcClient(_rpc_url())
    pools = load_novel_unique()
    pilot = run_pilot(rpc, pools)
    block = run_block_semantics(rpc, pools)

    zero_liquidity_count = int(pilot["failure_reason_counts"].get("zero_liquidity", 0))
    quoter_revert_count = int(pilot["failure_reason_counts"].get("quoter_revert", 0))
    old_active_labeler = any(item["old_manual_selector_active"] for item in audit["labeler_data_supply"])
    old_only_legacy = (
        not old_active_labeler
        and all(not item["old_manual_selector_active"] for item in audit["production_runtime"])
        and any(item["old_manual_selector_active"] for item in audit["diagnostic_research"])
    )
    labeler_bridge_ready = (
        artifacts["quote_result_fields_consistent"]
        and not old_active_labeler
        and pilot["entry_quote_success_rate"] >= 0.98
        and pilot["roundtrip_success_rate"] >= 0.98
    )

    flags = {
        "v3r13c_artifacts_canonicalized": bool(artifacts["both_artifact_sets_exist"] and CANONICAL_JSON.exists() and CANONICAL_MD.exists()),
        "old_quote_path_in_active_labeler": bool(old_active_labeler),
        "old_quote_path_only_legacy_comparison": bool(old_only_legacy),
        "v3_struct_helper_available": True,
        "labeler_bridge_ready": bool(labeler_bridge_ready),
        "allow_v3r15_bounded_dataset_expansion": bool(labeler_bridge_ready),
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": "run004_v3r14_v3_labeler_bridge",
        "research_only": True,
        "quote_selector": STRUCT_SELECTOR,
        "helper": "scripts/lib/v3_quoterv2_struct.py",
        "script": "scripts/run004_v3r14_v3_labeler_bridge_pilot.py",
        "v3r13c_artifact_audit": artifacts,
        "quote_path_audit": audit,
        "pilot": pilot,
        "block_semantics": block,
        "labeler_bridge_assessment": {
            "labeler_bridge_ready": bool(labeler_bridge_ready),
            "reason": (
                "50-pool pilot passed; active labeler/data-supply is on struct ABI. Block-sensitive cases must keep explicit block policy."
                if labeler_bridge_ready
                else "Bridge not ready because one or more gates failed; see pilot/block/audit counters."
            ),
            "quote_success_rate": pilot["entry_quote_success_rate"],
            "roundtrip_success_rate": pilot["roundtrip_success_rate"],
            "zero_liquidity_count": zero_liquidity_count,
            "quoter_revert_count": quoter_revert_count,
            "block_sensitive_count": block["block_sensitive_count"],
        },
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
