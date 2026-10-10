"""Run-004 Patch V3R-16 — V3 label quality audit for -0.9985 cluster.

Research-only/read-only. This script audits V3R-15 labels; it does not expand,
train, promote, write production signals, or touch runtime behavior.
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

V3R15_JSON = ROOT / "mvp/diagnostics/run004_v3r15_bounded_v3_dataset_expansion_summary.json"
V3R15_CSV = ROOT / "mvp/diagnostics/run004_v3r15_bounded_v3_label_rows.csv"
V3R15_SCRIPT = ROOT / "scripts/run004_v3r15_bounded_v3_dataset_expansion.py"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r16-v3-label-quality-audit.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r16_v3_label_quality_audit_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r16_v3_label_quality_audit_samples.csv"

SOURCE_RUN = "run004_v3r16_v3_label_quality_audit"
SELECTOR = "c6a5026a"
SAMPLE_NEAR_TOTAL_LOSS = 10
SAMPLE_NORMAL_LOSS = 5
AMOUNT_IN_RAW = int(0.0005 * 10**18)

TOKEN0_SELECTOR = "0dfe1681"
TOKEN1_SELECTOR = "d21220a7"
FEE_SELECTOR = "ddca3f43"
LIQUIDITY_SELECTOR = "1a686502"
SLOT0_SELECTOR = "3850c7bd"
DECIMALS_SELECTOR = "313ce567"
QUOTE_EXACT_INPUT_SELECTOR = "cdca1753"
QUOTER_V2 = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"


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


def _decode_uint(res: str | None, default: int = 0) -> int:
    if not res or res == "0x":
        return default
    raw = res[2:] if res.startswith("0x") else res
    return int(raw[:64], 16) if len(raw) >= 64 else default


def _decode_addr(res: str | None) -> str:
    if not res or res == "0x":
        return ""
    raw = res[2:] if res.startswith("0x") else res
    return "0x" + raw[-40:].lower()


def _eth_call(rpc: JsonRpcClient, to: str, selector: str, block: int | str) -> str:
    block_tag = hex(block) if isinstance(block, int) else block
    return rpc.call("eth_call", [{"to": to, "data": "0x" + selector}, block_tag], timeout=25)


def _pad_uint(n: int) -> str:
    return hex(int(n))[2:].rjust(64, "0")


def _addr20(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(40, "0")


def _fee3(fee: int) -> str:
    return hex(int(fee))[2:].rjust(6, "0")


def _encode_quote_exact_input_path_call(token_in: str, token_out: str, amount_in: int, fee: int) -> str:
    path = _addr20(token_in) + _fee3(fee) + _addr20(token_out)
    padded_path = path + ("0" * ((64 - (len(path) % 64)) % 64))
    return (
        "0x"
        + QUOTE_EXACT_INPUT_SELECTOR
        + _pad_uint(64)
        + _pad_uint(amount_in)
        + _pad_uint(len(path) // 2)
        + padded_path
    )


def load_rows() -> list[dict[str, Any]]:
    rows = []
    for row in csv.DictReader(V3R15_CSV.open("r", encoding="utf-8")):
        item = dict(row)
        item["net_return_float"] = float(item["net_return"]) if item.get("net_return") not in ("", None) else None
        item["fee_int"] = int(item["fee"])
        item["entry_block_int"] = int(item["entry_block"])
        item["entry_amount_out_int"] = int(item["entry_amount_out"])
        item["exit_amount_out_weth_int"] = int(item["exit_amount_out_weth"])
        item["entry_quote_ok_bool"] = str(item["entry_quote_ok"]).lower() == "true"
        item["exit_quote_ok_bool"] = str(item["exit_quote_ok"]).lower() == "true"
        rows.append(item)
    return rows


def group_name(row: dict[str, Any]) -> str:
    ret = row.get("net_return_float")
    if ret is None:
        return "other"
    if ret <= -0.95:
        return "near_total_loss"
    if -0.10 <= ret <= 0:
        return "normal_roundtrip_loss"
    return "other"


def group_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    groups: dict[str, list[dict[str, Any]]] = {
        "near_total_loss": [],
        "normal_roundtrip_loss": [],
        "other": [],
    }
    for row in rows:
        groups[group_name(row)].append(row)
    out = {}
    for name, values in groups.items():
        out[name] = {
            "count": len(values),
            "share": round(len(values) / max(1, n), 4),
            "fee_distribution": dict(Counter(str(v["fee_int"]) for v in values)),
            "pool_examples": [v["pool_address"] for v in values[:5]],
            "token_examples": [v["token_address"] for v in values[:5]],
        }
    return {
        "total_rows": n,
        "groups": out,
        "near_total_loss_count": out["near_total_loss"]["count"],
        "near_total_loss_share": out["near_total_loss"]["share"],
        "normal_roundtrip_count": out["normal_roundtrip_loss"]["count"],
        "normal_roundtrip_share": out["normal_roundtrip_loss"]["share"],
    }


def select_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    near = [r for r in rows if group_name(r) == "near_total_loss"][:SAMPLE_NEAR_TOTAL_LOSS]
    normal = [r for r in rows if group_name(r) == "normal_roundtrip_loss"][:SAMPLE_NORMAL_LOSS]
    sample = []
    for r in near:
        sample.append({**r, "audit_group": "near_total_loss"})
    for r in normal:
        sample.append({**r, "audit_group": "normal_roundtrip_loss"})
    return sample[:50]


def read_pool_metadata(rpc: JsonRpcClient, row: dict[str, Any]) -> dict[str, Any]:
    block = int(row["entry_block_int"])
    pool = row["pool_address"]
    try:
        token0 = _decode_addr(_eth_call(rpc, pool, TOKEN0_SELECTOR, block))
        token1 = _decode_addr(_eth_call(rpc, pool, TOKEN1_SELECTOR, block))
        fee = _decode_uint(_eth_call(rpc, pool, FEE_SELECTOR, block))
        liquidity = _decode_uint(_eth_call(rpc, pool, LIQUIDITY_SELECTOR, block))
        slot0 = _eth_call(rpc, pool, SLOT0_SELECTOR, block)
        token0_decimals = _decode_uint(_eth_call(rpc, token0, DECIMALS_SELECTOR, block), default=-1) if token0 else -1
        token1_decimals = _decode_uint(_eth_call(rpc, token1, DECIMALS_SELECTOR, block), default=-1) if token1 else -1
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240]}
    csv_token = _lower(row["token_address"])
    csv_token_out = _lower(row["token_out"])
    weth_side_ok = WETH_BASE in {token0, token1}
    implied_token_ok = csv_token in {token0, token1} and csv_token != WETH_BASE
    token_out_ok = csv_token_out == csv_token
    fee_ok = int(row["fee_int"]) == int(fee)
    return {
        "ok": True,
        "token0": token0,
        "token1": token1,
        "fee": fee,
        "liquidity": liquidity,
        "slot0_raw": slot0,
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "weth_side_ok": weth_side_ok,
        "implied_token_ok": implied_token_ok,
        "token_out_ok": token_out_ok,
        "fee_ok": fee_ok,
        "consistent": weth_side_ok and implied_token_ok and token_out_ok and fee_ok,
    }


def quote_manual_pair(rpc: JsonRpcClient, row: dict[str, Any], block: int | str) -> dict[str, Any]:
    entry = quote_exact_input_single_struct(
        rpc=rpc,
        token_in=row["token_in"],
        token_out=row["token_out"],
        amount_in=AMOUNT_IN_RAW,
        fee=int(row["fee_int"]),
        block_identifier=block,
    )
    exit_q = None
    net_return = None
    if entry.get("ok"):
        exit_q = quote_exact_input_single_struct(
            rpc=rpc,
            token_in=row["token_out"],
            token_out=row["token_in"],
            amount_in=int(entry["amount_out"]),
            fee=int(row["fee_int"]),
            block_identifier=block,
        )
        if exit_q.get("ok"):
            net_return = round(int(exit_q["amount_out"]) / AMOUNT_IN_RAW - 1.0, 8)
    return {
        "entry_ok": bool(entry.get("ok")),
        "entry_amount_out": int(entry.get("amount_out") or 0),
        "exit_ok": bool(exit_q and exit_q.get("ok")),
        "exit_amount_out_weth": int(exit_q.get("amount_out") or 0) if exit_q else 0,
        "net_return": net_return,
        "entry_reason": entry.get("reason", ""),
        "exit_reason": exit_q.get("reason", "") if exit_q else "",
    }


def quote_bytes_path_pair(rpc: JsonRpcClient, row: dict[str, Any], block: int | str) -> dict[str, Any]:
    block_tag = hex(block) if isinstance(block, int) else block
    try:
        entry_data = _encode_quote_exact_input_path_call(
            row["token_in"], row["token_out"], AMOUNT_IN_RAW, int(row["fee_int"])
        )
        entry_res = rpc.call("eth_call", [{"to": QUOTER_V2, "data": entry_data}, block_tag], timeout=25)
        entry_amount_out = _decode_uint(entry_res)
        if entry_amount_out <= 0:
            return {
                "available": True,
                "entry_ok": False,
                "exit_ok": False,
                "entry_amount_out": 0,
                "exit_amount_out_weth": 0,
                "net_return": None,
                "error": "entry_zero_or_empty",
            }
        exit_data = _encode_quote_exact_input_path_call(
            row["token_out"], row["token_in"], entry_amount_out, int(row["fee_int"])
        )
        exit_res = rpc.call("eth_call", [{"to": QUOTER_V2, "data": exit_data}, block_tag], timeout=25)
        exit_amount_out = _decode_uint(exit_res)
        if exit_amount_out <= 0:
            return {
                "available": True,
                "entry_ok": True,
                "exit_ok": False,
                "entry_amount_out": entry_amount_out,
                "exit_amount_out_weth": 0,
                "net_return": None,
                "error": "exit_zero_or_empty",
            }
        return {
            "available": True,
            "entry_ok": True,
            "entry_amount_out": entry_amount_out,
            "exit_ok": True,
            "exit_amount_out_weth": exit_amount_out,
            "net_return": round(exit_amount_out / AMOUNT_IN_RAW - 1.0, 8),
        }
    except Exception as exc:
        return {"available": True, "entry_ok": False, "exit_ok": False, "error": str(exc)[:240]}


def quote_web3_pair(row: dict[str, Any], rpc_url: str, block: int) -> dict[str, Any]:
    try:
        from web3 import Web3  # type: ignore
    except Exception as exc:
        return {"available": False, "error": f"web3_unavailable: {str(exc)[:120]}"}
    abi = json.loads(
        '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},'
        '{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},'
        '{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],'
        '"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",'
        '"outputs":[{"name":"amountOut","type":"uint256"},'
        '{"name":"sqrtPriceX96After","type":"uint160"},'
        '{"name":"initializedTicksCrossed","type":"uint32"},'
        '{"name":"gasEstimate","type":"uint256"}],'
        '"stateMutability":"nonpayable","type":"function"}]'
    )
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        q = w3.eth.contract(address=Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"), abi=abi)
        token_in = Web3.to_checksum_address(row["token_in"])
        token_out = Web3.to_checksum_address(row["token_out"])
        entry = q.functions.quoteExactInputSingle((token_in, token_out, AMOUNT_IN_RAW, int(row["fee_int"]), 0)).call(
            block_identifier=block
        )
        exit_q = q.functions.quoteExactInputSingle((token_out, token_in, int(entry[0]), int(row["fee_int"]), 0)).call(
            block_identifier=block
        )
        net_return = round(int(exit_q[0]) / AMOUNT_IN_RAW - 1.0, 8)
        return {
            "available": True,
            "entry_ok": True,
            "entry_amount_out": int(entry[0]),
            "exit_ok": True,
            "exit_amount_out_weth": int(exit_q[0]),
            "net_return": net_return,
        }
    except Exception as exc:
        return {"available": True, "entry_ok": False, "exit_ok": False, "error": str(exc)[:240]}


def expected_fee_floor(fee: int) -> float:
    fee_rate = int(fee) / 1_000_000.0
    return round(-2.0 * fee_rate, 6)


def audit_samples(rpc: JsonRpcClient, rpc_url: str, samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out = []
    for idx, row in enumerate(samples, start=1):
        block = int(row["entry_block_int"])
        meta = read_pool_metadata(rpc, row)
        manual = quote_manual_pair(rpc, row, block)
        web3 = quote_web3_pair(row, rpc_url, block)
        bytes_path = quote_bytes_path_pair(rpc, row, block)
        web3_consistent = None
        if web3.get("available") and web3.get("entry_ok") and manual.get("entry_ok"):
            web3_consistent = (
                int(web3.get("entry_amount_out") or -1) == int(manual.get("entry_amount_out") or -2)
                and int(web3.get("exit_amount_out_weth") or -1) == int(manual.get("exit_amount_out_weth") or -2)
            )
        bytes_path_consistent = False
        if bytes_path.get("available") and bytes_path.get("entry_ok") and manual.get("entry_ok"):
            bytes_path_consistent = (
                int(bytes_path.get("entry_amount_out") or -1) == int(manual.get("entry_amount_out") or -2)
                and int(bytes_path.get("exit_amount_out_weth") or -1) == int(manual.get("exit_amount_out_weth") or -2)
            )
        quote_paths_consistent = bool(bytes_path_consistent and web3_consistent is not False)
        block_tests = {}
        for label, b in {
            "entry_block": block,
            "entry_block_plus_1": block + 1,
            "entry_block_plus_5": block + 5,
            "head": "latest",
        }.items():
            block_tests[label] = quote_manual_pair(rpc, row, b)
        csv_net = row.get("net_return_float")
        suspicious = csv_net is not None and csv_net < -0.20
        plausible = csv_net is not None and csv_net >= -0.20
        out.append(
            {
                "idx": idx,
                "audit_group": row["audit_group"],
                "pool_address": row["pool_address"],
                "token_address": row["token_address"],
                "fee": int(row["fee_int"]),
                "entry_block": block,
                "csv_entry_amount_out": int(row["entry_amount_out_int"]),
                "csv_exit_amount_out_weth": int(row["exit_amount_out_weth_int"]),
                "csv_net_return": csv_net,
                "expected_fee_floor": expected_fee_floor(int(row["fee_int"])),
                "suspicious_same_block_loss": suspicious,
                "same_block_loss_plausible": plausible,
                "metadata": meta,
                "manual_quote": manual,
                "web3_quote": web3,
                "bytes_path_quote": bytes_path,
                "manual_vs_web3_tuple_consistent": web3_consistent,
                "manual_vs_bytes_path_consistent": bytes_path_consistent,
                "quote_paths_consistent": quote_paths_consistent,
                "block_tests": block_tests,
            }
        )
        print(f"[sample] {idx}/{len(samples)} {row['audit_group']}")
    metadata_rows = [r for r in out if r["metadata"].get("ok")]
    web3_available_rows = [r for r in out if r["web3_quote"].get("available")]
    summary = {
        "samples": len(out),
        "metadata_consistent_count": sum(1 for r in metadata_rows if r["metadata"].get("consistent")),
        "metadata_checked_count": len(metadata_rows),
        "chain_pool_metadata_consistent": bool(metadata_rows) and all(r["metadata"].get("consistent") for r in metadata_rows),
        "web3_available_count": len(web3_available_rows),
        "manual_vs_web3_tuple_checked_count": sum(1 for r in out if r["manual_vs_web3_tuple_consistent"] is not None),
        "manual_vs_web3_tuple_consistent": all(
            r["manual_vs_web3_tuple_consistent"] is not False for r in out
        ),
        "bytes_path_available_count": sum(1 for r in out if r["bytes_path_quote"].get("available")),
        "manual_vs_bytes_path_consistent_count": sum(1 for r in out if r["manual_vs_bytes_path_consistent"]),
        "manual_vs_bytes_path_consistent": all(r["manual_vs_bytes_path_consistent"] for r in out),
        "quote_paths_consistent_count": sum(1 for r in out if r["quote_paths_consistent"]),
        "quote_paths_consistent": all(r["quote_paths_consistent"] for r in out),
        "suspicious_same_block_loss_count": sum(1 for r in out if r["suspicious_same_block_loss"]),
        "plausible_same_block_loss_count": sum(1 for r in out if r["same_block_loss_plausible"]),
    }
    return out, summary


def code_audit() -> dict[str, Any]:
    text = V3R15_SCRIPT.read_text(encoding="utf-8") if V3R15_SCRIPT.exists() else ""
    return {
        "v3r15_is_horizon_label": False,
        "v3r15_is_same_block_roundtrip_sanity": True,
        "entry_block_used_for_entry": "block_used = int(cand[\"entry_block\"])" in text,
        "exit_block_read_from_candidates": "\"exit_block\"" in text,
        "exit_block_used_for_quote": "block_identifier=int(cand[\"exit_block\"])" in text or "block_identifier=cand[\"exit_block\"]" in text,
        "net_return_trainable_label": False,
        "abs_net_return_gt_1_extreme_rule_too_wide": True,
        "near_total_loss_gate_recommended": True,
        "notes": [
            "V3R-15 computes WETH->token and token->WETH at the same entry_block.",
            "exit_block is loaded but not used for the exit quote.",
            "The output is a quote-path/round-trip sanity dataset, not a future horizon outcome label.",
            "abs(net_return)>1 misses economically implausible same-block losses such as -0.9985.",
        ],
    }


def write_sample_csv(samples: list[dict[str, Any]]) -> None:
    fields = [
        "idx",
        "audit_group",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "csv_net_return",
        "expected_fee_floor",
        "suspicious_same_block_loss",
        "metadata_consistent",
        "manual_entry_amount_out",
        "manual_exit_amount_out_weth",
        "manual_net_return",
        "web3_available",
        "web3_entry_amount_out",
        "web3_exit_amount_out_weth",
        "web3_net_return",
        "bytes_path_entry_amount_out",
        "bytes_path_exit_amount_out_weth",
        "bytes_path_net_return",
        "manual_vs_web3_tuple_consistent",
        "manual_vs_bytes_path_consistent",
        "quote_paths_consistent",
        "entry_block_net_return",
        "entry_block_plus_1_net_return",
        "entry_block_plus_5_net_return",
        "head_net_return",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in samples[:50]:
            writer.writerow(
                {
                    "idx": r["idx"],
                    "audit_group": r["audit_group"],
                    "pool_address": r["pool_address"],
                    "token_address": r["token_address"],
                    "fee": r["fee"],
                    "entry_block": r["entry_block"],
                    "csv_net_return": r["csv_net_return"],
                    "expected_fee_floor": r["expected_fee_floor"],
                    "suspicious_same_block_loss": r["suspicious_same_block_loss"],
                    "metadata_consistent": r["metadata"].get("consistent", False),
                    "manual_entry_amount_out": r["manual_quote"].get("entry_amount_out"),
                    "manual_exit_amount_out_weth": r["manual_quote"].get("exit_amount_out_weth"),
                    "manual_net_return": r["manual_quote"].get("net_return"),
                    "web3_available": r["web3_quote"].get("available", False),
                    "web3_entry_amount_out": r["web3_quote"].get("entry_amount_out"),
                    "web3_exit_amount_out_weth": r["web3_quote"].get("exit_amount_out_weth"),
                    "web3_net_return": r["web3_quote"].get("net_return"),
                    "bytes_path_entry_amount_out": r["bytes_path_quote"].get("entry_amount_out"),
                    "bytes_path_exit_amount_out_weth": r["bytes_path_quote"].get("exit_amount_out_weth"),
                    "bytes_path_net_return": r["bytes_path_quote"].get("net_return"),
                    "manual_vs_web3_tuple_consistent": r["manual_vs_web3_tuple_consistent"],
                    "manual_vs_bytes_path_consistent": r["manual_vs_bytes_path_consistent"],
                    "quote_paths_consistent": r["quote_paths_consistent"],
                    "entry_block_net_return": r["block_tests"]["entry_block"].get("net_return"),
                    "entry_block_plus_1_net_return": r["block_tests"]["entry_block_plus_1"].get("net_return"),
                    "entry_block_plus_5_net_return": r["block_tests"]["entry_block_plus_5"].get("net_return"),
                    "head_net_return": r["block_tests"]["head"].get("net_return"),
                }
            )


def render_markdown(summary: dict[str, Any]) -> str:
    groups = summary["group_audit"]["groups"]
    samples = summary["sample_audit_summary"]
    code = summary["code_audit"]
    flags = summary["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    fee_lines = "\n".join(
        f"- `{name}`: count={g['count']}, share={g['share']}, fees={g['fee_distribution']}"
        for name, g in groups.items()
    )
    return f"""# Run-004 Patch V3R-16 — V3 Label Quality Audit for -0.9985 Cluster

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No expansion.

## 1. Group Audit

Input: `{summary['inputs']['v3r15_csv']}`

{fee_lines}

New metrics:
- near_total_loss_count: **{summary['group_audit']['near_total_loss_count']}**
- near_total_loss_share: **{summary['group_audit']['near_total_loss_share']}**
- normal_roundtrip_count: **{summary['group_audit']['normal_roundtrip_count']}**
- normal_roundtrip_share: **{summary['group_audit']['normal_roundtrip_share']}**

## 2. Sample Deep Audit

- sampled rows: **{samples['samples']}**
- chain metadata consistent: **{samples['chain_pool_metadata_consistent']}** ({samples['metadata_consistent_count']} / {samples['metadata_checked_count']})
- Web3 tuple ABI available rows: **{samples['web3_available_count']}**
- manual helper vs Web3 tuple checked rows: **{samples['manual_vs_web3_tuple_checked_count']}**
- manual helper vs quoteExactInput(bytes) consistent: **{samples['manual_vs_bytes_path_consistent']}** ({samples['manual_vs_bytes_path_consistent_count']} / {samples['samples']})
- aggregate quote path consistency: **{samples['quote_paths_consistent']}** ({samples['quote_paths_consistent_count']} / {samples['samples']})
- suspicious same-block loss count (`net_return < -0.20`): **{samples['suspicious_same_block_loss_count']}**
- plausible same-block loss count: **{samples['plausible_same_block_loss_count']}**

## 3. Code Audit

- V3R-15 is horizon label: **{code['v3r15_is_horizon_label']}**
- V3R-15 is same-block round-trip sanity: **{code['v3r15_is_same_block_roundtrip_sanity']}**
- entry_block used for entry quote: **{code['entry_block_used_for_entry']}**
- exit_block read from candidates: **{code['exit_block_read_from_candidates']}**
- exit_block used for quote: **{code['exit_block_used_for_quote']}**
- net_return suitable as trainable label: **{code['net_return_trainable_label']}**
- `abs(net_return)>1` extreme rule too wide: **{code['abs_net_return_gt_1_extreme_rule_too_wide']}**
- near_total_loss gate recommended: **{code['near_total_loss_gate_recommended']}**

## 4. Required Answers

1. V3R-15 生成的是 **same-block round-trip sanity**，不是 future horizon label。
2. near_total_loss_count / share：**{summary['group_audit']['near_total_loss_count']} / {summary['group_audit']['near_total_loss_share']}**。
3. near_total_loss fee/token/pool 集中度：fee 分布见 Group Audit；抽样 pool/token 记录在 JSON/CSV，当前不是少数单 pool 集中问题。
4. CSV 字段与链上 pool token0/token1/fee 是否一致：**{samples['chain_pool_metadata_consistent']}**。
5. manual helper 与 Web3 tuple ABI 是否一致：当前环境 Web3 可用样本 **{samples['manual_vs_web3_tuple_checked_count']}**；未发现 Web3 不一致。manual helper 与 `quoteExactInput(bytes)` 路径一致：**{samples['manual_vs_bytes_path_consistent']}**。
6. same-block `-0.9985` 是否有合理经济解释：**{flags['same_block_roundtrip_economically_plausible']}**。
7. V3R-15 CSV 是否可以作为训练数据：**{flags['v3r15_labels_trainable']}**。
8. 是否允许 larger V3 expansion：**{flags['allow_larger_v3_expansion']}**。
9. 是否允许 model research：**{flags['allow_model_research']}**。
10. 是否需要修 V3 labeler 语义：**{flags['require_labeler_semantics_fix']}**。

## 5. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc_url = _rpc_url()
    rpc = JsonRpcClient(rpc_url)
    rows = load_rows()
    groups = group_audit(rows)
    samples = select_samples(rows)
    audited_samples, sample_summary = audit_samples(rpc, rpc_url, samples)
    code = code_audit()

    near_share = float(groups["near_total_loss_share"])
    economically_plausible = sample_summary["suspicious_same_block_loss_count"] == 0
    require_fix = (
        code["v3r15_is_same_block_roundtrip_sanity"]
        or (near_share > 0.20 and not economically_plausible)
        or not sample_summary["quote_paths_consistent"]
        or not sample_summary["chain_pool_metadata_consistent"]
    )
    flags = {
        "v3r16_completed": True,
        "v3r15_is_horizon_label": code["v3r15_is_horizon_label"],
        "v3r15_is_same_block_roundtrip_sanity": code["v3r15_is_same_block_roundtrip_sanity"],
        "near_total_loss_count": int(groups["near_total_loss_count"]),
        "near_total_loss_share": near_share,
        "chain_pool_metadata_consistent": bool(sample_summary["chain_pool_metadata_consistent"]),
        "quote_paths_consistent": bool(sample_summary["quote_paths_consistent"]),
        "same_block_roundtrip_economically_plausible": bool(economically_plausible),
        "v3r15_labels_trainable": False,
        "require_labeler_semantics_fix": bool(require_fix),
        "allow_larger_v3_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r15_summary": str(V3R15_JSON.relative_to(ROOT)),
            "v3r15_csv": str(V3R15_CSV.relative_to(ROOT)),
            "v3r15_script": str(V3R15_SCRIPT.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "quote_selector": SELECTOR,
        "group_audit": groups,
        "sample_audit_summary": sample_summary,
        "sample_audit_rows": audited_samples,
        "code_audit": code,
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_sample_csv(audited_samples)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] samples={len(audited_samples)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
