from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import label_dashcam_executable as labeler  # noqa: E402
from scripts.run004_research_utils import rounded, write_csv_rows, write_json, write_markdown  # noqa: E402

WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
QUOTER_FUNCTION = "Uniswap V3 QuoterV2.quoteExactInputSingle((address,address,uint256,uint24,uint160))"
ROUND_TRIP_COST = 0.006
SANITY_MIN = -1.0
SANITY_MAX = 10.0
EXIT_BLOCK_OFFSET = 900
MIN_WETH_PROXY = 500_000_000_000_000  # 0.0005 WETH, matching current label notional.
TARGET_CANDIDATES = 5000
POOL_CAP = 75

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default


def _dt(value: Any) -> datetime | None:
    text = str(value or "").replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
        except ValueError:
            return None


def _ch_query(query: str, timeout: int = 180) -> list[dict[str, Any]]:
    q = query.rstrip(";")
    if " format " not in q.lower():
        q = f"{q} FORMAT JSONEachRow"
    cmd = [
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--database",
        "eatlq",
        "--query",
        q,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _weth_leg(token0: str, token1: str) -> str:
    if _lower(token0) == WETH_BASE:
        return "token0"
    if _lower(token1) == WETH_BASE:
        return "token1"
    return "missing"


def _norm(raw: int, decimals: int) -> float | None:
    if decimals < 0:
        return None
    return raw / float(10**decimals)


def _sanity(net: float) -> tuple[float, bool]:
    if not (net == net):
        return 0.0, True
    if net < SANITY_MIN or net > SANITY_MAX:
        return max(SANITY_MIN, min(SANITY_MAX, net)), True
    return net, False


def _metadata() -> dict[str, dict[str, Any]]:
    rows = _ch_query(
        """
        SELECT
          lower(pool_address) AS pool_address,
          lower(token0_address) AS token0,
          lower(token1_address) AS token1,
          token0_symbol,
          token1_symbol,
          token0_decimals,
          token1_decimals,
          fee_bps,
          has_weth_leg
        FROM eatlq.discovered_pools
        WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
        """
    )
    return {str(r["pool_address"]): r for r in rows}


def _decoded_rows() -> list[dict[str, Any]]:
    return _ch_query(
        """
        SELECT
          lower(pool_address) AS pool_addr,
          tx_hash AS src_tx_hash,
          log_index AS src_log_index,
          block_number AS src_block_number,
          toString(block_time) AS block_time_str,
          lower(sender) AS sender_addr,
          lower(recipient) AS recipient_addr,
          toString(amount0) AS amount0_str,
          toString(amount1) AS amount1_str,
          toString(sqrt_price_x96) AS sqrtPriceX96,
          toString(liquidity) AS liquidity,
          tick AS tick
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = 8453
          AND removed = 0
          AND amount0 != 0
          AND amount1 != 0
          AND tx_hash != ''
          AND block_time IS NOT NULL
          AND lower(pool_address) IN (
            SELECT lower(pool_address)
            FROM eatlq.discovered_pools
            WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
          )
        """
    )


def _infer(amount0: int, amount1: int, token0: str, token1: str, dec0: int, dec1: int) -> dict[str, Any]:
    if amount0 > 0 and amount1 < 0:
        token_in, token_out = token0, token1
        amount_in, amount_out = amount0, abs(amount1)
        in_dec, out_dec = dec0, dec1
    elif amount1 > 0 and amount0 < 0:
        token_in, token_out = token1, token0
        amount_in, amount_out = amount1, abs(amount0)
        in_dec, out_dec = dec1, dec0
    else:
        token_in, token_out = "", ""
        amount_in, amount_out = 0, 0
        in_dec, out_dec = -1, -1
    weth_side_raw = amount_in if token_in == WETH_BASE else amount_out if token_out == WETH_BASE else 0
    return {
        "direction_ambiguous": token_in == "" or token_out == "",
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amountIn_raw": str(amount_in),
        "amountOut_raw": str(amount_out),
        "amountIn_normalized": _norm(amount_in, in_dec),
        "amountOut_normalized": _norm(amount_out, out_dec),
        "weth_side_amount_raw": str(weth_side_raw),
        "weth_side_amount_normalized": _norm(weth_side_raw, 18),
        "is_buy_weth_to_token": token_in == WETH_BASE and token_out not in {"", WETH_BASE},
        "is_sell_token_to_weth": token_out == WETH_BASE and token_in not in {"", WETH_BASE},
    }


def _candidate_from_row(idx: int, row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    token0 = _lower(meta["token0"])
    token1 = _lower(meta["token1"])
    amount0 = _int(row["amount0_str"])
    amount1 = _int(row["amount1_str"])
    direction = _infer(amount0, amount1, token0, token1, _int(meta["token0_decimals"]), _int(meta["token1_decimals"]))
    block_time = _dt(row.get("block_time_str"))
    return {
        "candidate_id": idx,
        "candidate_kind": "decoded_backed_v3_swap_v3r3",
        "decoded_source": "uniswap_v3_swaps_decoded",
        "pool_address": row["pool_addr"],
        "source_tx_hash": row["src_tx_hash"],
        "source_log_index": row["src_log_index"],
        "source_block_number": row["src_block_number"],
        "block_number": row["src_block_number"],
        "entry_block": row["src_block_number"],
        "exit_block": _int(row["src_block_number"]) + EXIT_BLOCK_OFFSET,
        "timestamp": block_time.isoformat() if block_time else row.get("block_time_str", ""),
        "timestamp_ms": int(block_time.timestamp() * 1000) if block_time else "",
        "sender": row.get("sender_addr", ""),
        "recipient": row.get("recipient_addr", ""),
        "amount0": str(amount0),
        "amount1": str(amount1),
        "sqrtPriceX96": row.get("sqrtPriceX96", ""),
        "tick": row.get("tick", ""),
        "liquidity": row.get("liquidity", ""),
        "token0": token0,
        "token1": token1,
        "token0_symbol": meta.get("token0_symbol", ""),
        "token1_symbol": meta.get("token1_symbol", ""),
        "token0_decimals": meta.get("token0_decimals", ""),
        "token1_decimals": meta.get("token1_decimals", ""),
        "fee_bps": meta.get("fee_bps", ""),
        "fee_tier": _int(meta.get("fee_bps")) * 100,
        "weth_leg": _weth_leg(token0, token1),
        "metadata_join_status": "joined_discovered_pools",
        "tx_log_block_identity_present": True,
        **direction,
    }


def reconstruct_candidates() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta = _metadata()
    raw = _decoded_rows()
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw:
        pool = str(row["pool_addr"])
        if pool in meta:
            by_pool[pool].append(row)
    selected: list[dict[str, Any]] = []
    # Balance by pool first to prevent a handful of hot pools dominating.
    for pool in sorted(by_pool, key=lambda p: len(by_pool[p]), reverse=True):
        rows = sorted(by_pool[pool], key=lambda r: int(r["src_block_number"]))
        if not rows:
            continue
        step = max(1, len(rows) // POOL_CAP)
        selected.extend((pool, row) for row in rows[::step][:POOL_CAP])
    if len(selected) < TARGET_CANDIDATES:
        seen = {(pool, row["src_tx_hash"], row["src_log_index"]) for pool, row in selected}
        for pool, rows in by_pool.items():
            for row in rows:
                key = (pool, row["src_tx_hash"], row["src_log_index"])
                if key in seen:
                    continue
                selected.append((pool, row))
                seen.add(key)
                if len(selected) >= TARGET_CANDIDATES:
                    break
            if len(selected) >= TARGET_CANDIDATES:
                break
    selected = selected[:TARGET_CANDIDATES]
    candidates = [_candidate_from_row(i, row, meta[pool]) for i, (pool, row) in enumerate(selected)]
    write_csv_rows(OUT_DIR / "run004_v3r3_candidate_pool.csv", candidates)
    report = candidate_report(candidates, raw_pool_count=len(by_pool), raw_rows=len(raw))
    write_json(OUT_DIR / "run004_v3r3_candidate_report.json", report)
    return candidates, report


def coverage(rows: list[dict[str, Any]], pred: Any) -> float:
    return rounded(sum(1 for r in rows if pred(r)) / len(rows), 8) if rows else 0.0


def candidate_report(candidates: list[dict[str, Any]], raw_pool_count: int, raw_rows: int) -> dict[str, Any]:
    times = [_dt(c["timestamp"]) for c in candidates if _dt(c["timestamp"])]
    return {
        "total_candidates": len(candidates),
        "raw_decoded_joined_rows_available": raw_rows,
        "raw_unique_decoded_weth_pools_available": raw_pool_count,
        "unique_pools": len({c["pool_address"] for c in candidates}),
        "unique_pool_goal": "data_limited_below_100" if raw_pool_count < 100 else "supported",
        "time_range": {"min": min(times).isoformat() if times else "", "max": max(times).isoformat() if times else ""},
        "metadata_coverage": coverage(candidates, lambda c: c["metadata_join_status"] == "joined_discovered_pools"),
        "weth_leg_coverage": coverage(candidates, lambda c: c["weth_leg"] in {"token0", "token1"}),
        "fee_tier_coverage": coverage(candidates, lambda c: _int(c["fee_tier"]) > 0),
        "tx_log_block_identity_coverage": coverage(candidates, lambda c: bool(c["tx_log_block_identity_present"])),
        "amount0_amount1_non_null_coverage": coverage(candidates, lambda c: c["amount0"] not in {"", "0"} and c["amount1"] not in {"", "0"}),
        "direction_counts": dict(Counter("buy_weth_to_token" if c["is_buy_weth_to_token"] else "sell_token_to_weth" if c["is_sell_token_to_weth"] else "ambiguous" for c in candidates)),
        "fee_tier_distribution": dict(Counter(str(c["fee_bps"]) for c in candidates).most_common()),
        "hour_bucket_distribution_top20": dict(Counter((_dt(c["timestamp"]) or datetime(1970, 1, 1, tzinfo=UTC)).strftime("%Y-%m-%dT%H") for c in candidates).most_common(20)),
    }


def min_notional_report(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    thresholds = [0, 10**9, 10**12, 10**14, 5 * 10**14, 10**15, 5 * 10**15, 10**16]
    vals = sorted(_int(c["weth_side_amount_raw"]) for c in candidates)
    def pct(p: float) -> float:
        if not vals:
            return 0.0
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    coverage_by_threshold = []
    for t in thresholds:
        kept = [c for c in candidates if _int(c["weth_side_amount_raw"]) >= t]
        coverage_by_threshold.append(
            {
                "threshold_weth_raw": t,
                "threshold_weth": t / 1e18,
                "kept": len(kept),
                "removed": len(candidates) - len(kept),
                "unique_pools": len({c["pool_address"] for c in kept}),
            }
        )
    filtered = [c for c in candidates if _int(c["weth_side_amount_raw"]) >= MIN_WETH_PROXY]
    report = {
        "amount_usd_available": False,
        "notional_proxy": "WETH-side amount from signed amount0/amount1",
        "selected_min_weth_raw": MIN_WETH_PROXY,
        "selected_min_weth": MIN_WETH_PROXY / 1e18,
        "before_filter_candidates": len(candidates),
        "after_filter_candidates": len(filtered),
        "removed_dust_rows": len(candidates) - len(filtered),
        "after_filter_unique_pools": len({c["pool_address"] for c in filtered}),
        "weth_side_raw_distribution": {
            "min": vals[0] if vals else 0,
            "p25": pct(0.25),
            "p50": pct(0.50),
            "p75": pct(0.75),
            "p90": pct(0.90),
            "p99": pct(0.99),
            "max": vals[-1] if vals else 0,
        },
        "candidate_coverage_by_min_notional_threshold": coverage_by_threshold,
    }
    write_json(OUT_DIR / "run004_v3r3_min_notional_report.json", report)
    return filtered, report


def shared_patch_plan() -> dict[str, Any]:
    plan = {
        "shared_labeler_patched_this_round": False,
        "blocked_reason": "V3R-3 is producing a patch plan only; shared labeler patch should be applied as a separate minimal patch with V2 regression before V3 can join mainline.",
        "current_bug": {
            "file": "scripts/label_dashcam_executable.py",
            "function": "call_quoter_v3",
            "current_code": "raw = res.lstrip(\"0x\")",
            "problem": "str.lstrip(\"0x\") removes every leading 0 or x character, not just the 0x prefix; QuoterV2 ABI words lose leading zeros and 32-byte alignment.",
            "v2_path_affected": False,
            "v2_note": "V2 path does not call call_quoter_v3. call_get_reserves also uses lstrip(\"0x\"), but reserve return values are fixed two-word uint112; patch should be scoped carefully and regression-tested.",
        },
        "minimal_patch": [
            "Introduce helper: def _strip_0x(hexstr): return hexstr[2:] if hexstr.startswith('0x') else hexstr",
            "In call_quoter_v3, replace raw = res.lstrip(\"0x\") with raw = _strip_0x(res)",
            "Optionally decode QuoterV2 words via bytes.fromhex(raw) and 32-byte chunks.",
            "Do not change V2 quote math or V2 reserve path in the same patch unless regression tests cover it.",
        ],
        "required_regression_tests": [
            "Synthetic QuoterV2 return with leading-zero amountOut word decodes to expected integer.",
            "Known V3R-2/V3R-3 quote examples decode amountOut equal to source decoded amount within tolerance.",
            "V2 small regression: existing V2 pilot sample remains 100% quote success or unchanged labels.",
        ],
        "approval_needed_before_mainline": True,
    }
    write_json(OUT_DIR / "run004_v3r3_shared_labeler_patch_plan.json", plan)
    return plan


def quote_v3(rpc: labeler.RPC, token_in: str, token_out: str, amount_in: int, fee: int, block: int) -> dict[str, Any]:
    data = (
        labeler._SEL_QUOTE_V3
        + labeler._addr_pad(token_in)
        + labeler._addr_pad(token_out)
        + labeler._uint_pad(amount_in)
        + labeler._uint_pad(fee)
        + labeler._uint_pad(0)
    )
    try:
        res = labeler._eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception as exc:
        return {"success": False, "error": str(exc), "amount_out": 0}
    raw = res[2:] if res.startswith("0x") else res
    words = [raw[i : i + 64] for i in range(0, len(raw), 64)]
    amount_out = int(words[0], 16) if words and words[0] else 0
    return {"success": amount_out > 0, "error": "", "amount_out": amount_out, "raw_quote_output": res}


def select_stage_rows(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["is_buy_weth_to_token"] and _int(row["amountIn_raw"]) > 0:
            by_pool[row["pool_address"]].append(row)
    selected: list[dict[str, Any]] = []
    round_idx = 0
    pools = sorted(by_pool, key=lambda p: len(by_pool[p]), reverse=True)
    while len(selected) < n:
        added = False
        for pool in pools:
            if round_idx < len(by_pool[pool]):
                selected.append(by_pool[pool][round_idx])
                added = True
                if len(selected) >= n:
                    break
        if not added:
            break
        round_idx += 1
    return selected


def run_stage(rpc: labeler.RPC, rows: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    results = []
    for cand in rows:
        amount_in = _int(cand["amountIn_raw"])
        entry = quote_v3(rpc, cand["tokenIn"], cand["tokenOut"], amount_in, _int(cand["fee_tier"]), _int(cand["entry_block"]))
        if not entry["success"]:
            label_type = "quoter_revert" if entry["error"] else "fail_v3_entry_zero"
            exit_quote = {"amount_out": 0}
            net = None
        else:
            exit_quote = quote_v3(rpc, cand["tokenOut"], cand["tokenIn"], _int(entry["amount_out"]), _int(cand["fee_tier"]), _int(cand["exit_block"]))
            if not exit_quote["success"]:
                label_type = "exit_quoter_revert" if exit_quote["error"] else "executable_exit_drained"
                net = -1.0
            else:
                raw_net = _int(exit_quote["amount_out"]) / amount_in - 1.0 - ROUND_TRIP_COST
                net, bogus = _sanity(raw_net)
                label_type = "fail_sanity_clip" if bogus else "executable"
        success = label_type == "executable"
        results.append(
            {
                "stage": stage,
                "candidate_id": cand["candidate_id"],
                "pool_address": cand["pool_address"],
                "source_tx_hash": cand["source_tx_hash"],
                "source_log_index": cand["source_log_index"],
                "entry_block": cand["entry_block"],
                "exit_block": cand["exit_block"],
                "fee_tier": cand["fee_tier"],
                "fee_bps": cand["fee_bps"],
                "weth_side_amount_raw": cand["weth_side_amount_raw"],
                "weth_side_amount_normalized": cand["weth_side_amount_normalized"],
                "entry_quote_amount_out": entry["amount_out"],
                "exit_quote_amount_out": exit_quote["amount_out"],
                "label_type": label_type,
                "success": success,
                "net_return": net,
                "decoded_coverage": 1,
                "gas_adjusted_fields_available": success,
            }
        )
    attempted = len(results)
    success = sum(1 for r in results if r["success"])
    return {
        "stage": stage,
        "attempted": attempted,
        "success": success,
        "failed": attempted - success,
        "success_rate": rounded(success / attempted, 8) if attempted else 0.0,
        "unique_pools": len({r["pool_address"] for r in results}),
        "fee_tier_distribution": dict(Counter(str(r["fee_bps"]) for r in results).most_common()),
        "failure_reason_distribution": dict(Counter(str(r["label_type"]) for r in results).most_common()),
        "decoded_coverage": 1.0 if attempted else 0.0,
        "fail_v3_entry_zero_rate": rounded(sum(1 for r in results if r["label_type"] == "fail_v3_entry_zero") / attempted, 8) if attempted else 0.0,
        "fail_sanity_clip_rate": rounded(sum(1 for r in results if r["label_type"] == "fail_sanity_clip") / attempted, 8) if attempted else 0.0,
        "exit_drained_rate": rounded(sum(1 for r in results if r["label_type"] == "executable_exit_drained") / attempted, 8) if attempted else 0.0,
        "gas_adjusted_fields_availability": rounded(sum(1 for r in results if r["gas_adjusted_fields_available"]) / attempted, 8) if attempted else 0.0,
        "min_notional_distribution": {
            "min_weth": min((_int(r["weth_side_amount_raw"]) for r in results), default=0) / 1e18,
            "max_weth": max((_int(r["weth_side_amount_raw"]) for r in results), default=0) / 1e18,
        },
        "rows": results,
    }


def pilot(filtered: list[dict[str, Any]], rpc_url: str) -> dict[str, Any]:
    report = {"executed": False, "reason": "", "stages": [], "decision": {}}
    if not rpc_url:
        report["reason"] = "EVM_HTTP_URL or LABEL_RPC_URL missing"
    else:
        rpc = labeler.RPC(rpc_url)
        stage_a_rows = select_stage_rows(filtered, 300)
        if len(stage_a_rows) < 300:
            report["reason"] = f"not enough filtered candidates for Stage A: {len(stage_a_rows)}"
        else:
            report["executed"] = True
            a = run_stage(rpc, stage_a_rows, "A_300")
            report["stages"].append(a)
            if a["success_rate"] >= 0.8:
                b_rows = select_stage_rows(filtered, 1000)
                if len(b_rows) >= 1000:
                    b = run_stage(rpc, b_rows, "B_1000")
                    report["stages"].append(b)
                    if b["success_rate"] >= 0.75:
                        c_rows = select_stage_rows(filtered, 2000)
                        if len(c_rows) >= 2000:
                            report["stages"].append(run_stage(rpc, c_rows, "C_2000"))
    report["decision"] = decision(report)
    write_json(OUT_DIR / "run004_v3r3_pilot_checkpoint_report.json", report)
    return report


def decision(pilot_report: dict[str, Any]) -> dict[str, Any]:
    stages = pilot_report.get("stages", [])
    a = next((s for s in stages if s["stage"] == "A_300"), None)
    b = next((s for s in stages if s["stage"] == "B_1000"), None)
    if not a:
        return {"allow_v3r4": False, "allow_v3_mainline": False, "reason": "Stage A not executed"}
    if a["success_rate"] >= 0.8 and (b is None or b["success_rate"] >= 0.75):
        return {
            "allow_v3r4": True,
            "allow_v3_mainline": False,
            "reason": "larger pilot passed repair gates, but mainline still requires approved shared labeler patch and oracle spot-check completion",
        }
    return {"allow_v3r4": False, "allow_v3_mainline": False, "reason": "larger pilot did not meet success gates"}


def oracle_spotcheck(candidates: list[dict[str, Any]], pilot_report: dict[str, Any]) -> dict[str, Any]:
    token_status = {
        "DUNE_API_KEY": bool(os.environ.get("DUNE_API_KEY") or os.environ.get("DUNE_API_TOKEN")),
        "BITQUERY_API_KEY": bool(os.environ.get("BITQUERY_API_KEY") or os.environ.get("BITQUERY_TOKEN")),
        "THEGRAPH_API_KEY": bool(os.environ.get("GRAPH_API_KEY") or os.environ.get("THEGRAPH_API_KEY")),
    }
    stage_rows = []
    for stage in pilot_report.get("stages", []):
        stage_rows.extend(stage.get("rows", []))
    ids = [r["candidate_id"] for r in stage_rows[:10]]
    ids.extend(r["candidate_id"] for r in stage_rows[-10:])
    by_id = {c["candidate_id"]: c for c in candidates}
    reps = [by_id[i] for i in dict.fromkeys(ids) if i in by_id][:20]
    payloads = []
    for c in reps:
        ts = _dt(c["timestamp"]) or datetime.now(UTC)
        payloads.append(
            {
                "candidate_id": c["candidate_id"],
                "oracle_result_status": "oracle_result_pending",
                "source_identity": {
                    "pool_address": c["pool_address"],
                    "tx_hash": c["source_tx_hash"],
                    "log_index": c["source_log_index"],
                    "block_number": c["source_block_number"],
                    "timestamp": c["timestamp"],
                },
                "dune_sql_filter": {
                    "blockchain": "base",
                    "project_contract_address": c["pool_address"],
                    "tx_hash": c["source_tx_hash"],
                    "evt_index_or_log_index": c["source_log_index"],
                },
                "bitquery_variables": {"network": "base", "pool": c["pool_address"], "transactionHash": c["source_tx_hash"]},
                "subgraph_variables": {
                    "pool": c["pool_address"],
                    "transaction": c["source_tx_hash"],
                    "timestamp_gte": int((ts - timedelta(minutes=5)).timestamp()),
                    "timestamp_lt": int((ts + timedelta(minutes=5)).timestamp()),
                },
            }
        )
    report = {
        "api_token_status": token_status,
        "executed_external_api_calls": False,
        "reason": "API tokens unavailable; generated representative query payloads" if not any(token_status.values()) else "tokens present but automated oracle execution intentionally deferred",
        "representative_rows": len(payloads),
        "payloads": payloads,
        "comparison_schema": {
            "pool_match": None,
            "token_direction_match": None,
            "amount_scale_match": None,
            "fee_tier_match": None,
            "sender_trader_match": None,
            "amountUSD_sanity": None,
            "systematic_direction_or_scale_mismatch": None,
        },
    }
    write_json(OUT_DIR / "run004_v3r3_oracle_spotcheck_report.json", report)
    return report


def markdown(patch: dict[str, Any]) -> str:
    cr = patch["candidate_report"]
    nr = patch["min_notional_report"]
    pr = patch["pilot_checkpoint_report"]
    final = pr["stages"][-1] if pr.get("stages") else None
    return f"""# Run-004 Patch V3R-3 - Larger V3 Repair Pilot

## Scope
- V3 repair branch only; no mixed dataset.
- V2-only H16/H17 affected: `False`
- Production model / active model / runtime trading code affected: `False`

## Candidate Pool
- Candidates: {cr['total_candidates']}
- Unique pools: {cr['unique_pools']} (available WETH decoded pools: {cr['raw_unique_decoded_weth_pools_available']})
- Metadata coverage: {cr['metadata_coverage']}
- WETH-leg coverage: {cr['weth_leg_coverage']}
- Fee-tier coverage: {cr['fee_tier_coverage']}
- tx/log/block identity coverage: {cr['tx_log_block_identity_coverage']}

## Min Notional
- Proxy: {nr['notional_proxy']}
- Selected min WETH: {nr['selected_min_weth']}
- Before / after: {nr['before_filter_candidates']} / {nr['after_filter_candidates']}
- Removed dust rows: {nr['removed_dust_rows']}
- After-filter unique pools: {nr['after_filter_unique_pools']}

## Shared Labeler Patch Plan
- Shared labeler patched this round: `{patch['shared_labeler_patch_plan']['shared_labeler_patched_this_round']}`
- Bug: `call_quoter_v3` uses `raw = res.lstrip("0x")`; this must become prefix-only stripping.

## Pilot
- Executed: {pr['executed']}
- Final stage: {final['stage'] if final else ''}
- Final success rate: {final['success_rate'] if final else ''}
- Final unique pools: {final['unique_pools'] if final else ''}
- Decision: {pr['decision']}

## Oracle Spot Check
- External calls executed: `{patch['oracle_spotcheck_report']['executed_external_api_calls']}`
- Representative payloads: {patch['oracle_spotcheck_report']['representative_rows']}

## Gate
- Allow V3R-4: `{pr['decision'].get('allow_v3r4', False)}`
- Allow V3 mainline: `{pr['decision'].get('allow_v3_mainline', False)}`
- V2 mainline affected: `False`
"""


def main() -> None:
    candidates, cand_report = reconstruct_candidates()
    filtered, notional = min_notional_report(candidates)
    plan = shared_patch_plan()
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    pilot_report = pilot(filtered, rpc_url)
    oracle = oracle_spotcheck(filtered, pilot_report)
    patch = {
        "patch": "run-004-v3r3-larger-v3-repair-pilot",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_report": cand_report,
        "min_notional_report": notional,
        "shared_labeler_patch_plan": plan,
        "pilot_checkpoint_report": pilot_report,
        "oracle_spotcheck_report": oracle,
        "safety_assertions": {
            "v3_only": True,
            "no_mixed_v2_v3_dataset": True,
            "no_v2_mainline_outputs_modified": True,
            "no_production_model_write": True,
            "no_active_model_overwrite": True,
            "no_runtime_trading_code_change": True,
            "shared_labeler_patched": False,
        },
        "outputs": {
            "patch_json": str(OUT_DIR / "run004_patch_v3r3_larger_v3_repair_pilot.json"),
            "candidate_pool_csv": str(OUT_DIR / "run004_v3r3_candidate_pool.csv"),
            "candidate_report_json": str(OUT_DIR / "run004_v3r3_candidate_report.json"),
            "min_notional_json": str(OUT_DIR / "run004_v3r3_min_notional_report.json"),
            "shared_patch_plan_json": str(OUT_DIR / "run004_v3r3_shared_labeler_patch_plan.json"),
            "pilot_checkpoint_json": str(OUT_DIR / "run004_v3r3_pilot_checkpoint_report.json"),
            "oracle_spotcheck_json": str(OUT_DIR / "run004_v3r3_oracle_spotcheck_report.json"),
            "markdown": str(RUN_DIR / "run-004-patch-v3r3-larger-v3-repair-pilot.md"),
        },
    }
    write_json(OUT_DIR / "run004_patch_v3r3_larger_v3_repair_pilot.json", patch)
    write_markdown(RUN_DIR / "run-004-patch-v3r3-larger-v3-repair-pilot.md", markdown(patch))
    print(json.dumps({"candidate_report": cand_report, "min_notional": notional, "pilot_decision": pilot_report["decision"]}, indent=2))


if __name__ == "__main__":
    main()
