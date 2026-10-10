from __future__ import annotations

import csv
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
SEL_LIQUIDITY = "0x1a686502"

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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ch_query(query: str, timeout: int = 90) -> list[dict[str, Any]]:
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


def _reconstruct_candidates(limit: int = 600) -> list[dict[str, Any]]:
    meta_rows = _ch_query(
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
        WHERE chain_id = 8453
          AND pool_type = 'v3'
          AND has_weth_leg = 1
        """
    )
    meta = {str(r["pool_address"]): r for r in meta_rows}
    rows = _ch_query(
        f"""
        SELECT
          lower(s.pool_address) AS pool_address,
          s.tx_hash AS source_tx_hash,
          s.log_index AS source_log_index,
          s.block_number AS source_block_number,
          toString(s.block_time) AS block_time,
          toUnixTimestamp64Milli(s.block_time) AS timestamp_ms,
          lower(s.sender) AS sender,
          lower(s.recipient) AS recipient,
          toString(s.amount0) AS amount0,
          toString(s.amount1) AS amount1,
          toString(s.sqrt_price_x96) AS sqrtPriceX96,
          toString(s.liquidity) AS liquidity,
          s.tick AS tick
        FROM eatlq.uniswap_v3_swaps_decoded s
        WHERE s.chain_id = 8453
          AND s.removed = 0
          AND s.amount0 != 0
          AND s.amount1 != 0
          AND s.tx_hash != ''
          AND lower(s.pool_address) IN (
            SELECT lower(pool_address)
            FROM eatlq.discovered_pools
            WHERE chain_id = 8453 AND pool_type = 'v3' AND has_weth_leg = 1
          )
          AND s.block_time >= toDateTime64('2026-04-20 00:00:00', 3, 'UTC')
        ORDER BY s.block_number DESC, s.log_index DESC
        LIMIT {limit}
        """
    )
    candidates: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        pool_meta = meta[_lower(row["pool_address"])]
        token0 = _lower(pool_meta["token0"])
        token1 = _lower(pool_meta["token1"])
        leg = _weth_leg(token0, token1)
        amount0 = _int(row.get("amount0"))
        amount1 = _int(row.get("amount1"))
        direction = _infer_direction_fields(amount0, amount1, token0, token1, _int(pool_meta["token0_decimals"]), _int(pool_meta["token1_decimals"]))
        block_time = _dt(row.get("block_time"))
        candidates.append(
            {
                "candidate_id": idx,
                "candidate_kind": "decoded_backed_v3_swap",
                "decoded_source": "uniswap_v3_swaps_decoded",
                "pool_address": _lower(row["pool_address"]),
                "source_tx_hash": row.get("source_tx_hash", ""),
                "source_log_index": row.get("source_log_index", ""),
                "source_block_number": row.get("source_block_number", ""),
                "block_number": row.get("source_block_number", ""),
                "entry_block": row.get("source_block_number", ""),
                "exit_block": _int(row.get("source_block_number")) + EXIT_BLOCK_OFFSET,
                "timestamp": block_time.isoformat() if block_time else str(row.get("block_time", "")),
                "timestamp_ms": row.get("timestamp_ms", ""),
                "entry_time": block_time.isoformat() if block_time else str(row.get("block_time", "")),
                "sender": row.get("sender", ""),
                "recipient": row.get("recipient", ""),
                "amount0": str(amount0),
                "amount1": str(amount1),
                "sqrtPriceX96": row.get("sqrtPriceX96", ""),
                "tick": row.get("tick", ""),
                "liquidity": row.get("liquidity", ""),
                "token0": token0,
                "token1": token1,
                "token0_symbol": pool_meta.get("token0_symbol", ""),
                "token1_symbol": pool_meta.get("token1_symbol", ""),
                "token0_decimals": pool_meta.get("token0_decimals", ""),
                "token1_decimals": pool_meta.get("token1_decimals", ""),
                "fee_bps": pool_meta.get("fee_bps", ""),
                "fee_tier": _int(pool_meta.get("fee_bps")) * 100,
                "weth_leg": leg,
                "metadata_join_status": "joined_discovered_pools",
                "tx_log_block_identity_present": bool(row.get("source_tx_hash") and str(row.get("source_log_index")) != "" and row.get("source_block_number")),
                **direction,
            }
        )
    write_csv_rows(OUT_DIR / "run004_v3r2_decoded_backed_candidates.csv", candidates)
    return candidates


def _infer_direction_fields(amount0: int, amount1: int, token0: str, token1: str, dec0: int, dec1: int) -> dict[str, Any]:
    ambiguous = not ((amount0 > 0 and amount1 < 0) or (amount1 > 0 and amount0 < 0))
    if amount0 > 0 and amount1 < 0:
        token_in = token0
        token_out = token1
        amount_in_raw = amount0
        amount_out_raw = abs(amount1)
        in_dec = dec0
        out_dec = dec1
    elif amount1 > 0 and amount0 < 0:
        token_in = token1
        token_out = token0
        amount_in_raw = amount1
        amount_out_raw = abs(amount0)
        in_dec = dec1
        out_dec = dec0
    else:
        token_in = ""
        token_out = ""
        amount_in_raw = 0
        amount_out_raw = 0
        in_dec = -1
        out_dec = -1
    return {
        "direction_ambiguous": ambiguous,
        "zero_amount_row": amount0 == 0 or amount1 == 0,
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amountIn_raw": str(amount_in_raw),
        "amountOut_raw": str(amount_out_raw),
        "amountIn_normalized": _norm(amount_in_raw, in_dec),
        "amountOut_normalized": _norm(amount_out_raw, out_dec),
        "is_buy_weth_to_token": token_in == WETH_BASE and token_out not in {"", WETH_BASE},
        "is_sell_token_to_weth": token_out == WETH_BASE and token_in not in {"", WETH_BASE},
        "signed_amount_interpretation": "positive amount means pool received token; negative amount means pool sent token",
    }


def _candidate_report(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    times = [_dt(c.get("timestamp")) for c in candidates if _dt(c.get("timestamp"))]
    report = {
        "total_candidates": len(candidates),
        "unique_pools": len({c["pool_address"] for c in candidates}),
        "time_range": {
            "min": min(times).isoformat() if times else "",
            "max": max(times).isoformat() if times else "",
        },
        "metadata_join_coverage": _coverage(candidates, lambda c: c.get("metadata_join_status") == "joined_discovered_pools"),
        "weth_leg_coverage": _coverage(candidates, lambda c: c.get("weth_leg") in {"token0", "token1"}),
        "fee_tier_coverage": _coverage(candidates, lambda c: _int(c.get("fee_tier")) > 0),
        "amount0_amount1_non_null_coverage": _coverage(candidates, lambda c: c.get("amount0") not in {"", "0"} and c.get("amount1") not in {"", "0"}),
        "tx_log_block_identity_coverage": _coverage(candidates, lambda c: bool(c.get("tx_log_block_identity_present"))),
        "direction_counts": dict(Counter("buy_weth_to_token" if c["is_buy_weth_to_token"] else "sell_token_to_weth" if c["is_sell_token_to_weth"] else "ambiguous" for c in candidates)),
        "fee_bps_counts": dict(Counter(str(c.get("fee_bps")) for c in candidates).most_common()),
        "decoded_source": "uniswap_v3_swaps_decoded",
        "metadata_source": "discovered_pools",
    }
    write_json(OUT_DIR / "run004_v3r2_candidate_reconstruction_report.json", report)
    return report


def _coverage(rows: list[dict[str, Any]], pred: Any) -> float:
    return rounded(sum(1 for r in rows if pred(r)) / len(rows), 8) if rows else 0.0


def _direction_report(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    ambiguous = [c for c in candidates if c["direction_ambiguous"]]
    zero = [c for c in candidates if c["zero_amount_row"]]
    missing = [c for c in candidates if c["weth_leg"] == "missing"]
    mismatch = [
        c
        for c in candidates
        if not c["direction_ambiguous"]
        and not (
            (c["is_buy_weth_to_token"] and c["tokenIn"] == WETH_BASE)
            or (c["is_sell_token_to_weth"] and c["tokenOut"] == WETH_BASE)
        )
    ]
    report = {
        "rows": len(candidates),
        "direction_counts": dict(Counter("buy_weth_to_token" if c["is_buy_weth_to_token"] else "sell_token_to_weth" if c["is_sell_token_to_weth"] else "ambiguous" for c in candidates)),
        "ambiguous_rows": len(ambiguous),
        "zero_amount_rows": len(zero),
        "weth_leg_missing_rows": len(missing),
        "suspected_direction_mismatch_rows": len(mismatch),
        "direction_inference_reliable": len(ambiguous) == 0 and len(zero) == 0 and len(missing) == 0,
        "manual_review_examples": _slim_examples(ambiguous + zero + missing + mismatch, limit=20) or _slim_examples(candidates, limit=10),
    }
    write_json(OUT_DIR / "run004_v3r2_direction_inference_report.json", report)
    return report


def _slim_examples(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    fields = [
        "candidate_id",
        "pool_address",
        "source_tx_hash",
        "source_log_index",
        "source_block_number",
        "token0",
        "token1",
        "amount0",
        "amount1",
        "tokenIn",
        "tokenOut",
        "amountIn_raw",
        "amountOut_raw",
        "weth_leg",
        "fee_tier",
    ]
    return [{f: r.get(f, "") for f in fields} for r in rows[:limit]]


def _quote_v3_detailed(rpc: labeler.RPC, token_in: str, token_out: str, amount_in: int, fee: int, block: int) -> dict[str, Any]:
    data = (
        labeler._SEL_QUOTE_V3
        + labeler._addr_pad(token_in)
        + labeler._addr_pad(token_out)
        + labeler._uint_pad(amount_in)
        + labeler._uint_pad(fee)
        + labeler._uint_pad(0)
    )
    try:
        raw = labeler._eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception as exc:
        return {"success": False, "error": str(exc), "raw_quote_output": "", "amount_out": 0}
    # QuoterV2 returns ABI words with significant leading zeros. Do not use
    # str.lstrip("0x"), which strips zero bytes and misaligns 32-byte words.
    body = raw[2:] if raw.startswith("0x") else raw
    words = [body[i : i + 64] for i in range(0, len(body), 64)]
    amount_out = int(words[0], 16) if words and words[0] else 0
    return {
        "success": amount_out > 0,
        "error": "",
        "raw_quote_output": raw,
        "amount_out": amount_out,
        "sqrtPriceX96After": int(words[1], 16) if len(words) > 1 and words[1] else "",
        "initializedTicksCrossed": int(words[2], 16) if len(words) > 2 and words[2] else "",
        "gasEstimate": int(words[3], 16) if len(words) > 3 and words[3] else "",
    }


def _eth_call_uint(rpc: labeler.RPC, to: str, data: str, block: int) -> tuple[int | None, str]:
    try:
        res = labeler._eth_call(rpc, to, data, block)
        raw = res[2:] if res.startswith("0x") else res
        return int(raw[:64], 16), ""
    except Exception as exc:
        return None, str(exc)


def _classify_quote_failure(cand: dict[str, Any], quote: dict[str, Any], liquidity: int | None, liq_error: str) -> str:
    if _int(cand.get("amountIn_raw")) <= 0:
        return "zero_amount"
    if cand.get("direction_ambiguous") or not cand.get("tokenIn") or not cand.get("tokenOut"):
        return "direction_error"
    if _int(cand.get("fee_tier")) <= 0:
        return "fee_tier_error"
    if liq_error:
        return "block_state_unavailable"
    if liquidity == 0:
        return "no_liquidity_at_block"
    if quote.get("error"):
        return "quoter_revert"
    if _int(quote.get("amount_out")) <= 0:
        return "zero_quote"
    expected = _int(cand.get("amountOut_raw"))
    if expected > 0:
        ratio = _int(quote.get("amount_out")) / expected
        if ratio > 1000 or ratio < 0.001:
            return "decimals_scale_error"
    return ""


def _select_diagnostic_rows(candidates: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in sorted(candidates, key=lambda r: (str(r.get("fee_bps")), str(r.get("is_buy_weth_to_token")), int(r.get("candidate_id", 0)))):
        key = (str(row.get("pool_address")), str(row.get("fee_bps")), "buy" if row["is_buy_weth_to_token"] else "sell")
        if key in seen:
            continue
        selected.append(row)
        seen.add(key)
        if len(selected) >= limit:
            return selected
    for row in candidates:
        if row not in selected:
            selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _quote_diagnostic(candidates: list[dict[str, Any]], rpc_url: str) -> dict[str, Any]:
    rows = _select_diagnostic_rows(candidates, 20)
    if not rpc_url:
        report = {
            "executed": False,
            "reason": "EVM_HTTP_URL or LABEL_RPC_URL missing",
            "rows": [],
            "summary": {"attempted": 0, "success": 0, "success_rate": 0.0},
        }
        write_json(OUT_DIR / "run004_v3r2_quote_path_diagnostic.json", report)
        return report
    rpc = labeler.RPC(rpc_url)
    out = []
    for cand in rows:
        amount_in = _int(cand.get("amountIn_raw"))
        block = _int(cand.get("source_block_number"))
        liquidity, liq_error = _eth_call_uint(rpc, cand["pool_address"], SEL_LIQUIDITY, block)
        quote = _quote_v3_detailed(rpc, cand["tokenIn"], cand["tokenOut"], amount_in, _int(cand["fee_tier"]), block)
        expected = _int(cand.get("amountOut_raw"))
        failure = _classify_quote_failure(cand, quote, liquidity, liq_error)
        ratio = (_int(quote.get("amount_out")) / expected) if expected > 0 else None
        out.append(
            {
                "candidate_id": cand["candidate_id"],
                "pool_address": cand["pool_address"],
                "source_tx_hash": cand["source_tx_hash"],
                "source_log_index": cand["source_log_index"],
                "block_number": block,
                "tokenIn": cand["tokenIn"],
                "tokenOut": cand["tokenOut"],
                "fee_tier": cand["fee_tier"],
                "amountIn_before_decimals": str(amount_in),
                "amountIn_after_decimals": cand.get("amountIn_normalized"),
                "quoter_contract": QUOTER_V2_BASE,
                "quoter_function": QUOTER_FUNCTION,
                "call_success": bool(quote.get("success")),
                "failure_class": failure,
                "raw_quote_output": quote.get("raw_quote_output", ""),
                "decoded_quote_output": quote,
                "source_amountOut_raw": str(expected),
                "quote_to_source_amount_ratio": rounded(ratio, 8) if ratio is not None else None,
                "pool_liquidity_at_block": liquidity,
                "pool_liquidity_error": liq_error,
                "sanity_clip_result": "not_applied_entry_quote_only",
            }
        )
    summary = {
        "attempted": len(out),
        "success": sum(1 for r in out if r["call_success"]),
        "failed": sum(1 for r in out if not r["call_success"]),
        "success_rate": rounded(sum(1 for r in out if r["call_success"]) / len(out), 8) if out else 0.0,
        "failure_reason_distribution": dict(Counter(r["failure_class"] or "success" for r in out).most_common()),
    }
    report = {"executed": True, "summary": summary, "rows": out}
    write_json(OUT_DIR / "run004_v3r2_quote_path_diagnostic.json", report)
    return report


def _run_pilot_stage(rpc: labeler.RPC, rows: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    results = []
    for cand in rows:
        amount_in = _int(cand.get("amountIn_raw"))
        entry_block = _int(cand.get("entry_block"))
        exit_block = _int(cand.get("exit_block"))
        entry = _quote_v3_detailed(rpc, cand["tokenIn"], cand["tokenOut"], amount_in, _int(cand["fee_tier"]), entry_block)
        if amount_in <= 0:
            label_type = "zero_amount"
            net = None
            exit_quote = {"amount_out": 0}
        elif not entry.get("success"):
            label_type = "fail_v3_entry_zero" if not entry.get("error") else "quoter_revert"
            net = None
            exit_quote = {"amount_out": 0}
        else:
            exit_quote = _quote_v3_detailed(rpc, cand["tokenOut"], cand["tokenIn"], _int(entry["amount_out"]), _int(cand["fee_tier"]), exit_block)
            if not exit_quote.get("success"):
                label_type = "executable_exit_drained" if not exit_quote.get("error") else "exit_quoter_revert"
                net = -1.0
            else:
                net_raw = _int(exit_quote["amount_out"]) / amount_in - 1.0 - ROUND_TRIP_COST
                clipped, bogus = _sanity(net_raw)
                net = clipped
                label_type = "fail_sanity_clip" if bogus else "executable"
        success = label_type == "executable"
        results.append(
            {
                "stage": stage,
                "candidate_id": cand["candidate_id"],
                "pool_address": cand["pool_address"],
                "source_tx_hash": cand["source_tx_hash"],
                "source_log_index": cand["source_log_index"],
                "entry_block": entry_block,
                "exit_block": exit_block,
                "tokenIn": cand["tokenIn"],
                "tokenOut": cand["tokenOut"],
                "fee_tier": cand["fee_tier"],
                "amountIn_raw": cand["amountIn_raw"],
                "entry_quote_amount_out": entry.get("amount_out", 0),
                "exit_quote_amount_out": exit_quote.get("amount_out", 0),
                "label_type": label_type,
                "success": success,
                "net_return": net,
                "decoded_coverage": 1,
                "gas_adjusted_fields_available": success,
                "gas_adjusted_return_30m_low": rounded((net or 0) - 0.011, 10) if success else None,
                "gas_adjusted_return_30m_medium": rounded((net or 0) - 0.033, 10) if success else None,
                "gas_adjusted_return_30m_high": rounded((net or 0) - 0.11, 10) if success else None,
            }
        )
    return _pilot_summary(stage, results)


def _pilot_summary(stage: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = len(results)
    success = sum(1 for r in results if r["success"])
    reason_counts = Counter(str(r["label_type"]) for r in results)
    return {
        "stage": stage,
        "attempted": attempted,
        "success": success,
        "failed": attempted - success,
        "failure_reason_distribution": dict(reason_counts.most_common()),
        "success_rate": rounded(success / attempted, 8) if attempted else 0.0,
        "decoded_coverage": 1.0 if attempted else 0.0,
        "quote_zero_rate": rounded(sum(1 for r in results if r["label_type"] == "fail_v3_entry_zero") / attempted, 8) if attempted else 0.0,
        "sanity_clip_rate": rounded(sum(1 for r in results if r["label_type"] == "fail_sanity_clip") / attempted, 8) if attempted else 0.0,
        "exit_drained_rate": rounded(sum(1 for r in results if r["label_type"] == "executable_exit_drained") / attempted, 8) if attempted else 0.0,
        "gas_adjusted_fields_availability": rounded(sum(1 for r in results if r["gas_adjusted_fields_available"]) / attempted, 8) if attempted else 0.0,
        "clean_control_rows_exist": success > 0,
        "rows": results,
    }


def _pilot(candidates: list[dict[str, Any]], dry: dict[str, Any], rpc_url: str) -> dict[str, Any]:
    buy_rows = [c for c in candidates if c["is_buy_weth_to_token"] and _int(c.get("amountIn_raw")) > 0]
    report: dict[str, Any] = {
        "executed": False,
        "reason": "",
        "candidate_pool": "decoded-backed WETH->token V3 swaps only",
        "stages": [],
        "decision": {},
    }
    if not rpc_url:
        report["reason"] = "EVM_HTTP_URL or LABEL_RPC_URL missing"
    elif not dry.get("executed") or dry.get("summary", {}).get("success_rate", 0.0) < 0.5:
        report["reason"] = "dry diagnostic success below 50%; pilot skipped"
    elif len(buy_rows) < 50:
        report["reason"] = f"not enough WETH->token decoded-backed candidates for stage A: {len(buy_rows)}"
    else:
        rpc = labeler.RPC(rpc_url)
        report["executed"] = True
        stage_a = _run_pilot_stage(rpc, buy_rows[:50], "A_50")
        report["stages"].append(stage_a)
        if stage_a["success_rate"] >= 0.5 and len(buy_rows) >= 100:
            stage_b = _run_pilot_stage(rpc, buy_rows[:100], "B_100")
            report["stages"].append(stage_b)
            if stage_b["success_rate"] >= 0.6 and len(buy_rows) >= 300:
                stage_c = _run_pilot_stage(rpc, buy_rows[:300], "C_300")
                report["stages"].append(stage_c)
    final_stage = report["stages"][-1] if report["stages"] else None
    report["decision"] = _decision(final_stage)
    write_json(OUT_DIR / "run004_v3r2_pilot_checkpoint_report.json", report)
    return report


def _decision(final_stage: dict[str, Any] | None) -> dict[str, Any]:
    if not final_stage:
        return {
            "allow_v3r3_larger_repair_pilot": False,
            "allow_v3_mainline": False,
            "reason": "pilot not executed or no passing stage",
        }
    sr = final_stage["success_rate"]
    decoded = final_stage["decoded_coverage"]
    clean = bool(final_stage["clean_control_rows_exist"])
    sanity_ok = final_stage["sanity_clip_rate"] < 0.2
    entry_zero_ok = final_stage["quote_zero_rate"] < 0.2
    if sr >= 0.8 and decoded >= 0.9 and clean and sanity_ok and entry_zero_ok:
        return {
            "allow_v3r3_larger_repair_pilot": True,
            "allow_v3_mainline": False,
            "mainline_reconsideration_candidate": True,
            "reason": "V3R-2 pilot metrics passed, but mainline remains blocked pending larger diverse V3R-3, min-notional filtering, external oracle spot checks, and shared labeler ABI parse repair.",
        }
    if 0.6 <= sr < 0.8:
        return {
            "allow_v3r3_larger_repair_pilot": True,
            "allow_v3_mainline": False,
            "mainline_reconsideration_candidate": False,
            "reason": "60%-80% allows only larger V3 repair pilot",
        }
    return {
        "allow_v3r3_larger_repair_pilot": False,
        "allow_v3_mainline": False,
        "mainline_reconsideration_candidate": False,
        "reason": "<60% or quality gates failed; keep V3 isolated",
    }


def _oracle_template(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    reps = candidates[:10]
    payloads = []
    for c in reps:
        ts = _dt(c.get("timestamp")) or datetime.now(UTC)
        payloads.append(
            {
                "candidate_id": c["candidate_id"],
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
                    "block_time_window": [(ts - timedelta(minutes=5)).isoformat(), (ts + timedelta(minutes=5)).isoformat()],
                },
                "bitquery_graphql_variables": {
                    "network": "base",
                    "pool": c["pool_address"],
                    "transactionHash": c["source_tx_hash"],
                    "since": (ts - timedelta(minutes=5)).isoformat(),
                    "till": (ts + timedelta(minutes=5)).isoformat(),
                },
                "uniswap_v3_subgraph_variables": {
                    "pool": c["pool_address"],
                    "transaction": c["source_tx_hash"],
                    "timestamp_gte": int((ts - timedelta(minutes=5)).timestamp()),
                    "timestamp_lt": int((ts + timedelta(minutes=5)).timestamp()),
                },
            }
        )
    template = {
        "representative_payloads": payloads,
        "manual_oracle_result_schema": {
            "candidate_id": "int",
            "source": "dune|bitquery|uniswap_v3_subgraph|manual_decoder",
            "pool_address": "string",
            "tx_hash": "string",
            "log_index": "int|string",
            "token_bought_or_amount0": "string",
            "token_sold_or_amount1": "string",
            "amount_bought": "number|string",
            "amount_sold": "number|string",
            "amount_usd": "number|string|null",
            "fee_tier": "int|string|null",
            "sender_or_trader": "string|null",
        },
        "comparison_template": {
            "pool_match": None,
            "token_direction_match": None,
            "amount_scale_match": None,
            "fee_tier_match": None,
            "sender_trader_match": None,
            "amountUSD_sanity": None,
            "notes": "",
        },
    }
    write_json(OUT_DIR / "run004_v3r2_oracle_comparison_template.json", template)
    return template


def _markdown(patch: dict[str, Any]) -> str:
    recon = patch["candidate_reconstruction_report"]
    direction = patch["direction_inference_report"]
    dry = patch["quote_path_diagnostic"]["summary"]
    pilot = patch["pilot_checkpoint_report"]
    final_stage = pilot["stages"][-1] if pilot.get("stages") else None
    return f"""# Run-004 Patch V3R-2 - Decoded-Backed V3 Candidate Reconstruction

## Scope
- V3-only decoded-backed candidate reconstruction.
- Mixed V2/V3 dataset created: `False`
- V2-only H16/H17 affected: `False`
- Production model / active model / runtime trading code affected: `False`

## Candidate Reconstruction
- Candidates: {recon['total_candidates']}
- Unique pools: {recon['unique_pools']}
- Time range: {recon['time_range']}
- Metadata coverage: {recon['metadata_join_coverage']}
- WETH-leg coverage: {recon['weth_leg_coverage']}
- Fee-tier coverage: {recon['fee_tier_coverage']}
- tx/log/block identity coverage: {recon['tx_log_block_identity_coverage']}

## Direction Inference
- Reliable: {direction['direction_inference_reliable']}
- Direction counts: {direction['direction_counts']}
- Ambiguous rows: {direction['ambiguous_rows']}

## Quote Diagnostic
- Attempted: {dry.get('attempted', 0)}
- Success rate: {dry.get('success_rate', 0.0)}
- Failure distribution: {dry.get('failure_reason_distribution', {})}

## Pilot
- Executed: {pilot.get('executed')}
- Reason: {pilot.get('reason', '')}
- Final stage: {final_stage['stage'] if final_stage else ''}
- Final success rate: {final_stage['success_rate'] if final_stage else ''}
- Decision: {pilot.get('decision', {})}

## Gate
- V3 mainline allowed: `{pilot.get('decision', {}).get('allow_v3_mainline', False)}`
- V3R-3 allowed: `{pilot.get('decision', {}).get('allow_v3r3_larger_repair_pilot', False)}`
- V2-only H16/H17 do not wait for V3.
"""


def main() -> None:
    candidates = _reconstruct_candidates()
    reconstruction_report = _candidate_report(candidates)
    direction_report = _direction_report(candidates)
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    quote_path_diagnostic = _quote_diagnostic(candidates, rpc_url)
    pilot_report = _pilot(candidates, quote_path_diagnostic, rpc_url)
    oracle_template = _oracle_template(candidates)
    patch = {
        "patch": "run-004-v3r2-decoded-backed-candidate-reconstruction",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_reconstruction_report": reconstruction_report,
        "direction_inference_report": direction_report,
        "quote_path_diagnostic": quote_path_diagnostic,
        "pilot_checkpoint_report": pilot_report,
        "oracle_comparison_template": oracle_template,
        "outputs": {
            "patch_json": str(OUT_DIR / "run004_patch_v3r2_decoded_backed_candidate_reconstruction.json"),
            "candidates_csv": str(OUT_DIR / "run004_v3r2_decoded_backed_candidates.csv"),
            "candidate_report_json": str(OUT_DIR / "run004_v3r2_candidate_reconstruction_report.json"),
            "direction_report_json": str(OUT_DIR / "run004_v3r2_direction_inference_report.json"),
            "quote_diagnostic_json": str(OUT_DIR / "run004_v3r2_quote_path_diagnostic.json"),
            "pilot_checkpoint_json": str(OUT_DIR / "run004_v3r2_pilot_checkpoint_report.json"),
            "oracle_template_json": str(OUT_DIR / "run004_v3r2_oracle_comparison_template.json"),
            "markdown": str(RUN_DIR / "run-004-patch-v3r2-decoded-backed-candidate-reconstruction.md"),
        },
        "safety_assertions": {
            "v3_only": True,
            "diagnostics_only": True,
            "decoded_backed_only": True,
            "no_metadata_time_offset_candidates": True,
            "no_mixed_v2_v3_dataset": True,
            "no_v2_only_h16_h17_outputs_modified": True,
            "no_training": True,
            "no_production_retrain": True,
            "no_promote": True,
            "no_live": True,
            "no_runtime_trading_code_change": True,
        },
    }
    write_json(OUT_DIR / "run004_patch_v3r2_decoded_backed_candidate_reconstruction.json", patch)
    write_markdown(RUN_DIR / "run-004-patch-v3r2-decoded-backed-candidate-reconstruction.md", _markdown(patch))
    print(
        json.dumps(
            {
                "candidates": reconstruction_report,
                "direction": direction_report,
                "quote_diagnostic": quote_path_diagnostic.get("summary", {}),
                "pilot_decision": pilot_report.get("decision", {}),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
