from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import label_dashcam_executable as labeler  # noqa: E402
from scripts.run004_research_utils import (  # noqa: E402
    optional_float,
    read_rows,
    rounded,
    write_csv_rows,
    write_json,
    write_markdown,
)

WETH = labeler.WETH_BASE
QUOTER_FN = "QuoterV2.quoteExactInputSingle((address,address,uint256,uint24,uint160))"
SEL_LIQUIDITY = "0x1a686502"


class CountedRPC(labeler.RPC):  # type: ignore[misc]
    def __init__(self, url: str):
        super().__init__(url)
        self.call_count = 0

    def call(self, method: str, params: list[Any]) -> dict[str, Any]:  # type: ignore[override]
        self.call_count += 1
        return super().call(method, params)

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:  # type: ignore[override]
        self.call_count += len(calls)
        return super().batch(calls)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _load_config(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ch_query(cfg: dict[str, Any], query: str) -> list[dict[str, Any]]:
    ch = cfg["clickhouse"]
    q = query.rstrip(";")
    if " format " not in q.lower():
        q = f"{q} FORMAT JSONEachRow"
    cmd = [
        "docker",
        "exec",
        str(ch.get("docker_container", "eatlq-clickhouse-1")),
        "clickhouse-client",
        "--database",
        str(ch.get("database", "eatlq")),
        "--query",
        q,
    ]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=int(ch.get("query_timeout_sec", 60)) + 5,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _quote_call(
    rpc: CountedRPC,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee_param: int,
    block: int,
) -> dict[str, Any]:
    data = (
        labeler._SEL_QUOTE_V3  # noqa: SLF001
        + labeler._addr_pad(token_in)  # noqa: SLF001
        + labeler._addr_pad(token_out)  # noqa: SLF001
        + labeler._uint_pad(amount_in)  # noqa: SLF001
        + labeler._uint_pad(fee_param)  # noqa: SLF001
        + labeler._uint_pad(0)  # noqa: SLF001
    )
    out: dict[str, Any] = {
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "fee_param": fee_param,
        "block": block,
        "quoter_function_used": QUOTER_FN,
        "raw_result": "",
        "amount_out": 0,
        "ok": False,
        "error": "",
    }
    try:
        res = rpc.call("eth_call", [{"to": labeler.QUOTER_V2_BASE, "data": data}, hex(block)])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:500]
        return out
    if isinstance(res, dict) and res.get("error"):
        err = res.get("error") or {}
        out["error"] = str(err.get("message", err) if isinstance(err, dict) else err)[:500]
        return out
    raw = str((res or {}).get("result") or "")
    out["raw_result"] = raw
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) >= 64:
        out["amount_out"] = int(body[:64], 16)
        out["ok"] = out["amount_out"] > 0
    return out


def _eth_call_uint(rpc: CountedRPC, to: str, data: str, block: int) -> dict[str, Any]:
    try:
        res = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "value": None, "error": str(exc)[:500], "raw": ""}
    if isinstance(res, dict) and res.get("error"):
        err = res.get("error") or {}
        return {"ok": False, "value": None, "error": str(err.get("message", err) if isinstance(err, dict) else err)[:500], "raw": ""}
    raw = str((res or {}).get("result") or "")
    body = raw[2:] if raw.startswith("0x") else raw
    return {"ok": len(body) >= 64, "value": int(body[:64], 16) if len(body) >= 64 else None, "error": "", "raw": raw}


def _pool_code_present(rpc: CountedRPC, pool: str, block: int) -> bool:
    try:
        res = rpc.call("eth_getCode", [pool, hex(block)])
    except Exception:  # noqa: BLE001
        return False
    return str((res or {}).get("result") or "0x") not in {"", "0x"}


def _fee_candidates(fee_bps: int, cfg: dict[str, Any]) -> list[int]:
    vals: list[int] = []
    mapped = fee_bps * 100 if fee_bps > 0 else 3000
    for fee in [mapped, fee_bps, *cfg["pilot"].get("standard_fee_params", [100, 500, 3000, 10000])]:
        if fee > 0 and fee not in vals:
            vals.append(int(fee))
    return vals


def _weth_leg(row: dict[str, Any]) -> tuple[str, str, str]:
    t0 = _lower(row.get("token0_address"))
    t1 = _lower(row.get("token1_address"))
    if t0 == WETH and t1:
        return "token0", WETH, t1
    if t1 == WETH and t0:
        return "token1", WETH, t0
    return "missing", WETH, t1 or t0


def _gas_adjusted(net: float, cfg: dict[str, Any]) -> dict[str, Any]:
    lp = cfg["labeling"]
    units_cfg = lp.get("gas_units", {}).get("v3", {})
    units = int(units_cfg.get("buy", 0)) + int(units_cfg.get("sell", 0))
    if bool(lp.get("include_approval_gas", True)):
        units += int(lp.get("approval_gas_units", 0))
    notional = int(lp.get("notional_wei", 500000000000000))
    out: dict[str, Any] = {"estimated_total_gas_units": units}
    for tier, gwei in lp.get("gas_price_gwei", {}).items():
        cost_return = units * float(gwei) * 1e9 / notional
        out[f"gas_cost_return_{tier}"] = rounded(cost_return, 10)
        out[f"gas_adjusted_return_30m_{tier}"] = rounded(net - cost_return, 8)
    return out


def _classify_net(net: float, cfg: dict[str, Any]) -> tuple[str, bool]:
    max_abs = float(cfg["labeling"].get("max_abs_net", 10.0))
    if not (net == net):
        return "nan", True
    if net > max_abs:
        return "too_high_roi", True
    if net < -1.0:
        return "too_low_roi", True
    return "pass", False


def _round_trip(
    rpc: CountedRPC,
    row: dict[str, Any],
    cfg: dict[str, Any],
    entry_block: int,
    exit_block: int,
    *,
    repair_fallback: bool,
) -> dict[str, Any]:
    leg, token_in_entry, token_out_entry = _weth_leg(row)
    notional = int(cfg["labeling"].get("notional_wei", 500000000000000))
    fee_bps = _int(row.get("fee_bps"), 30)
    current_fee = fee_bps * 100
    fees = _fee_candidates(fee_bps, cfg) if repair_fallback else [current_fee]
    attempts: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for fee_param in fees:
        entry = _quote_call(rpc, token_in_entry, token_out_entry, notional, fee_param, entry_block)
        exit_quote: dict[str, Any] = {}
        net: float | None = None
        sanity = "entry_zero"
        bogus = True
        if entry.get("amount_out", 0) > 0:
            exit_quote = _quote_call(rpc, token_out_entry, token_in_entry, int(entry["amount_out"]), fee_param, exit_block)
            if exit_quote.get("amount_out", 0) > 0:
                net = float(exit_quote["amount_out"]) / notional - 1.0 - float(cfg["labeling"].get("round_trip_cost", 0.006))
                sanity, bogus = _classify_net(net, cfg)
            else:
                sanity = "exit_zero"
        attempt = {
            "fee_param": fee_param,
            "entry": entry,
            "exit": exit_quote,
            "net_return_before_sanity": net,
            "sanity_detail": sanity,
            "passes_sanity": not bogus,
        }
        attempts.append(attempt)
        if selected is None:
            selected = attempt
        if not bogus:
            selected = attempt
            break
    selected = selected or attempts[0]
    entry_sel = selected["entry"]
    exit_sel = selected.get("exit") or {}
    net_sel = selected.get("net_return_before_sanity")
    if entry_sel.get("amount_out", 0) <= 0:
        label_type = "fail_v3_entry_zero"
        label = -1
        net = 0.0
    elif exit_sel.get("amount_out", 0) <= 0:
        label_type = "executable_exit_drained"
        label = 0
        net = -1.0
    elif selected.get("passes_sanity"):
        label_type = "executable"
        net = float(net_sel)
        label = int(net > 0)
    else:
        label_type = "fail_sanity_clip"
        label = -1
        net = 0.0
    liquidity = _eth_call_uint(rpc, _lower(row.get("pool_address")), SEL_LIQUIDITY, entry_block)
    code_present = _pool_code_present(rpc, _lower(row.get("pool_address")), entry_block)
    return {
        "weth_leg": leg,
        "token_in_entry": token_in_entry,
        "token_out_entry": token_out_entry,
        "token_in_exit": token_out_entry,
        "token_out_exit": token_in_entry,
        "amount_in_before_decimals_conversion": str(notional),
        "amount_in_after_decimals_conversion": notional / 1e18,
        "quoter_function_used": QUOTER_FN,
        "fee_param_used": selected["fee_param"],
        "current_labeler_fee_param": current_fee,
        "entry_quote_raw": entry_sel.get("raw_result", ""),
        "entry_quote_amount_out": entry_sel.get("amount_out", 0),
        "entry_quote_error": entry_sel.get("error", ""),
        "exit_quote_raw": exit_sel.get("raw_result", ""),
        "exit_quote_amount_out": exit_sel.get("amount_out", 0),
        "exit_quote_error": exit_sel.get("error", ""),
        "raw_quote_result": entry_sel.get("raw_result", "") or entry_sel.get("error", ""),
        "sanity_clip_threshold": f"[-1.0,{cfg['labeling'].get('max_abs_net', 10.0)}]",
        "net_return_before_sanity": net_sel,
        "sanity_detail": selected.get("sanity_detail"),
        "failure_reason": "" if label_type == "executable" else label_type,
        "label_type": label_type,
        "label": label,
        "net_return": net,
        "amount_became_zero": bool(entry_sel.get("amount_out", 0) <= 0),
        "pool_liquidity_at_block": liquidity.get("value"),
        "pool_liquidity_error": liquidity.get("error", ""),
        "historical_pool_code_present": code_present,
        "quote_attempts": attempts,
        "token_direction_reversed_likely": "unlikely" if leg in {"token0", "token1"} else "likely",
        "fee_tier_mismatch_likely": "likely" if selected["fee_param"] != current_fee and selected.get("passes_sanity") else "unlikely",
        "historical_block_state_unavailable_likely": "likely" if not code_present or liquidity.get("error") else "unlikely",
    }


def _row_base(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "pool_address": _lower(row.get("pool_address")),
        "token0": _lower(row.get("token0_address")),
        "token1": _lower(row.get("token1_address")),
        "token0_symbol": row.get("token0_symbol", ""),
        "token1_symbol": row.get("token1_symbol", ""),
        "token0_decimals": row.get("token0_decimals", ""),
        "token1_decimals": row.get("token1_decimals", ""),
        "fee_tier": row.get("fee_bps", ""),
        "timestamp": row.get("entry_time", ""),
        "timestamp_ms": row.get("timestamp_ms", ""),
        "entry_block": _int(row.get("entry_block")),
        "exit_block": _int(row.get("exit_block")),
        "decoded_event_count": _int(row.get("decoded_event_count")),
        "decoded_join_status": row.get("decoded_join_status", ""),
    }


def _with_blocks(rpc: CountedRPC, rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    missing = [r for r in rows if not _int(r.get("entry_block")) or not _int(r.get("exit_block"))]
    if not missing:
        return rows
    horizon = int(cfg["labeling"].get("fixed_horizon_sec", 1800))
    min_ts = min(_int(r.get("timestamp_ms")) // 1000 for r in missing)
    max_ts = max(_int(r.get("timestamp_ms")) // 1000 for r in missing) + horizon
    bn_arr, ts_arr = labeler.build_ts_grid(rpc, min_ts, max_ts)
    for row in missing:
        ts = _int(row.get("timestamp_ms")) // 1000
        row["entry_block"] = labeler.ts_to_block(bn_arr, ts_arr, ts)
        row["exit_block"] = labeler.ts_to_block(bn_arr, ts_arr, ts + horizon)
    return rows


def _forensic_h165_v3(rpc: CountedRPC, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_rows(_resolve(cfg["input_paths"]["h165_pilot_dataset_csv"]))
    v3_rows = [r for r in rows if str(r.get("pool_class")) == "v3"]
    out_rows: list[dict[str, Any]] = []
    for row in v3_rows:
        base = _row_base(row)
        detail = _round_trip(
            rpc,
            row,
            cfg,
            _int(row.get("entry_block")),
            _int(row.get("exit_block")),
            repair_fallback=True,
        )
        out_rows.append({**base, **detail, "original_h165_failure_reason": row.get("failure_reason", "")})
    failures = Counter(str(r.get("original_h165_failure_reason") or r.get("label_type")) for r in out_rows)
    repaired = Counter(str(r.get("label_type")) for r in out_rows)
    summary = {
        "attempted_v3_rows": len(out_rows),
        "h165_failure_reasons": dict(failures.most_common()),
        "v3r_replayed_label_types": dict(repaired.most_common()),
        "raw_quote_extreme_rows": sum(1 for r in out_rows if (optional_float(r.get("net_return_before_sanity")) or 0) > float(cfg["labeling"].get("max_abs_net", 10.0))),
        "entry_zero_rows": sum(1 for r in out_rows if r.get("label_type") == "fail_v3_entry_zero"),
        "decoded_coverage": {
            "rows_with_decoded_event_count": sum(1 for r in out_rows if _int(r.get("decoded_event_count")) > 0),
            "coverage": rounded(sum(1 for r in out_rows if _int(r.get("decoded_event_count")) > 0) / len(out_rows), 8) if out_rows else 0.0,
        },
    }
    return out_rows, summary


def _coverage_report(cfg: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    pools = sorted({_lower(r.get("pool_address")) for r in rows if _lower(r.get("pool_address"))})
    if not pools:
        return {"rows": 0, "join_coverage": 0.0, "metadata_coverage": 0.0, "decoded_event_count_coverage": 0.0}
    pool_sql = ",".join(_sql_string(p) for p in pools)
    db = cfg["clickhouse"].get("database", "eatlq")
    meta_table = cfg["clickhouse"]["decoded_sources"]["metadata_table"]
    v3_table = cfg["clickhouse"]["decoded_sources"]["v3_table"]
    meta_rows = _ch_query(cfg, f"""
        SELECT lower(pool_address) AS pool, any(lower(token0_address)) AS token0, any(lower(token1_address)) AS token1,
               any(fee_bps) AS fee_bps, count() AS rows
        FROM {db}.{meta_table}
        WHERE chain_id = 8453 AND lower(pool_address) IN ({pool_sql})
        GROUP BY pool
    """)
    dec_rows = _ch_query(cfg, f"""
        SELECT lower(pool_address) AS pool, count() AS decoded_events,
               min(block_number) AS min_block, max(block_number) AS max_block
        FROM {db}.{v3_table}
        WHERE chain_id = 8453 AND lower(pool_address) IN ({pool_sql})
        GROUP BY pool
    """)
    meta = {r["pool"]: r for r in meta_rows}
    decoded = {r["pool"]: r for r in dec_rows}
    per_pool = []
    for pool in pools:
        per_pool.append({
            "pool_address": pool,
            "metadata_join": pool in meta,
            "decoded_join": pool in decoded,
            "decoded_events": _int((decoded.get(pool) or {}).get("decoded_events")),
            "metadata_fee_bps": (meta.get(pool) or {}).get("fee_bps"),
            "metadata_token0": (meta.get(pool) or {}).get("token0"),
            "metadata_token1": (meta.get(pool) or {}).get("token1"),
        })
    n = len(per_pool)
    return {
        "candidate_rows": len(rows),
        "unique_pools": n,
        "join_coverage": rounded(sum(1 for r in per_pool if r["metadata_join"] and r["decoded_join"]) / n, 8) if n else 0.0,
        "metadata_coverage": rounded(sum(1 for r in per_pool if r["metadata_join"]) / n, 8) if n else 0.0,
        "decoded_event_count_coverage": rounded(sum(1 for r in per_pool if r["decoded_events"] > 0) / n, 8) if n else 0.0,
        "per_pool": per_pool,
    }


def _select_v3_candidates(cfg: dict[str, Any], size: int) -> list[dict[str, Any]]:
    rows = read_rows(_resolve(cfg["input_paths"]["h165_repaired_candidate_csv"]))
    v3 = [r for r in rows if str(r.get("pool_class")) == "v3"]
    if bool(cfg["pilot"].get("prefer_decoded_backed_candidates", True)):
        v3.sort(key=lambda r: (_int(r.get("decoded_event_count")) <= 0, _lower(r.get("pool_address")), _int(r.get("timestamp_ms"))))
    return v3[:size]


def _run_pilot(rpc: CountedRPC, cfg: dict[str, Any], size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = _with_blocks(rpc, _select_v3_candidates(cfg, size), cfg)
    rows: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        detail = _round_trip(
            rpc,
            cand,
            cfg,
            _int(cand.get("entry_block")),
            _int(cand.get("exit_block")),
            repair_fallback=True,
        )
        success = detail["label_type"] == "executable"
        fixed = float(detail["net_return"]) if success else None
        gas = _gas_adjusted(fixed or 0.0, cfg) if success else {}
        medium = optional_float(gas.get("gas_adjusted_return_30m_medium")) if success else None
        row = {
            **cand,
            "pilot_row_id": idx,
            "entry_block": _int(cand.get("entry_block")),
            "exit_block": _int(cand.get("exit_block")),
            "label_status": "success" if success else "failed",
            "failure_reason": "" if success else detail["label_type"],
            "label_type": detail["label_type"],
            "executable_label_current": detail["label"] if success else "",
            "gross_quote_return_30m": rounded((fixed or 0.0) + float(cfg["labeling"].get("round_trip_cost", 0.006)), 8) if success else "",
            "fixed_cost_adjusted_return_30m": rounded(fixed, 8) if success else "",
            **gas,
            "final_return_after_cost": rounded(medium, 8) if success and medium is not None else "",
            "slippage_stress_return": rounded((medium or 0.0) - float(cfg["labeling"].get("slippage_stress_bps", 100)) / 10000.0, 8) if success and medium is not None else "",
            "decoded_join_status": cand.get("decoded_join_status", ""),
            "decoded_event_count": cand.get("decoded_event_count", ""),
            "weth_leg": detail["weth_leg"],
            "fee_param_used": detail["fee_param_used"],
            "current_labeler_fee_param": detail["current_labeler_fee_param"],
            "entry_quote_amount_out": detail["entry_quote_amount_out"],
            "exit_quote_amount_out": detail["exit_quote_amount_out"],
            "net_return_before_sanity": detail["net_return_before_sanity"],
            "sanity_detail": detail["sanity_detail"],
            "pool_liquidity_at_block": detail["pool_liquidity_at_block"],
            "token_direction_reversed_likely": detail["token_direction_reversed_likely"],
            "fee_tier_mismatch_likely": detail["fee_tier_mismatch_likely"],
            "historical_block_state_unavailable_likely": detail["historical_block_state_unavailable_likely"],
        }
        rows.append(row)
    reason_counts = Counter(str(r.get("label_type")) for r in rows)
    success_rows = [r for r in rows if r.get("label_status") == "success"]
    gas_available = sum(1 for r in success_rows if str(r.get("final_return_after_cost", "")) != "")
    checkpoint = {
        "status": "completed",
        "selected": len(candidates),
        "attempted": len(rows),
        "success": len(success_rows),
        "failed": len(rows) - len(success_rows),
        "success_rate": rounded(len(success_rows) / len(rows), 8) if rows else 0.0,
        "failure_reasons": {k: v for k, v in reason_counts.most_common() if k != "executable"},
        "label_type_counts": dict(reason_counts.most_common()),
        "decoded_coverage": {
            "attempted_rows": len(rows),
            "rows_with_decoded_events": sum(1 for r in rows if _int(r.get("decoded_event_count")) > 0),
            "coverage": rounded(sum(1 for r in rows if _int(r.get("decoded_event_count")) > 0) / len(rows), 8) if rows else 0.0,
        },
        "gas_adjusted_return_fields_availability": {
            "successful_rows": len(success_rows),
            "rows_with_final_return_after_cost": gas_available,
            "coverage": rounded(gas_available / len(success_rows), 8) if success_rows else 0.0,
        },
        "actual_rpc_calls_cumulative": rpc.call_count,
    }
    return rows, checkpoint


def _quote_path_audit(forensic_rows: list[dict[str, Any]], pilot: dict[str, Any]) -> dict[str, Any]:
    return {
        "truth_table": [
            {
                "weth_leg": "token0",
                "entry_token_in": "WETH/token0",
                "entry_token_out": "token1",
                "exit_token_in": "token1",
                "exit_token_out": "WETH/token0",
                "signed_v3_amount_interpretation": "amount0 > 0 means token0 paid into pool; amount1 < 0 means token1 out",
            },
            {
                "weth_leg": "token1",
                "entry_token_in": "WETH/token1",
                "entry_token_out": "token0",
                "exit_token_in": "token0",
                "exit_token_out": "WETH/token1",
                "signed_v3_amount_interpretation": "amount1 > 0 means token1 paid into pool; amount0 < 0 means token0 out",
            },
        ],
        "quoter_version": {
            "address": labeler.QUOTER_V2_BASE,
            "function": QUOTER_FN,
            "selector": labeler._SEL_QUOTE_V3,  # noqa: SLF001
            "historical_block_eth_call": True,
        },
        "suspected_bug_list": [
            "H16.5 V3 samples were metadata time-offset rows, not swap-backed execution rows; decoded coverage was near zero.",
            "The executable labeler collapses V3 quoter reverts, zero liquidity, and no-pool-at-fee into fail_v3_entry_zero.",
            "fail_sanity_clip is dominated by raw QuoterV2 amountOut values that imply unphysical round-trip returns; these should remain quarantined, not clipped into training labels.",
            "Fee fallback can diagnose metadata fee mismatch, but H16.5 evidence does not show a broad fee-tier fix that makes V3 safe.",
        ],
        "required_code_changes": [
            "Keep V3 diagnostics separate from V2 mainline outputs.",
            "Persist raw entry/exit quote, fee_param, pool liquidity, and historical code/liquidity checks for V3 failures.",
            "Before V3 rejoins any mainline, require a V3-only decoded-backed selection and a success-rate gate.",
        ],
        "no_code_workaround": "Use decoded-backed V3-only candidates and quarantine rows with entry zero, exit drained, unphysical raw quote return, missing historical pool code, or zero liquidity.",
        "summary_inputs": {
            "forensic_rows": len(forensic_rows),
            "pilot_100_success_rate": pilot.get("success_rate"),
        },
    }


def _markdown(patch: dict[str, Any]) -> str:
    p100 = patch["pilot_100_checkpoint"]
    p300 = patch.get("pilot_300_checkpoint")
    return f"""# Run-004 Patch V3R-1 - V3 Executable Labeler Repair Branch

## Scope
- V3-only diagnostics and pilot.
- Mixed V2/V3 dataset created: `False`
- V2-only H16.6 outputs modified: `False`
- Production / active model / runtime trading changes: `False`

## H16.5 V3 Forensic
- Attempted V3 rows replayed: {patch['forensic_summary']['attempted_v3_rows']}
- Original H16.5 failure reasons: {patch['forensic_summary']['h165_failure_reasons']}
- V3R replay label types: {patch['forensic_summary']['v3r_replayed_label_types']}
- Decoded coverage: {patch['forensic_summary']['decoded_coverage']}

## V3-Only Pilot 100
- Attempted / success / failed: {p100['attempted']} / {p100['success']} / {p100['failed']}
- Success rate: {p100['success_rate']}
- Failure reasons: {p100['failure_reasons']}
- Decoded coverage: {p100['decoded_coverage']}
- Gas-adjusted return availability: {p100['gas_adjusted_return_fields_availability']}

## V3-Only Pilot 300
- Result: {p300 if p300 else 'not run; 100-row gate did not pass'}

## Decision
- Allow V3R-2 larger V3 pilot: `{patch['gate']['allow_v3r2_larger_v3_pilot']}`
- Allow V3 mainline: `{patch['gate']['allow_v3_mainline']}`
- Allow future mixed dataset consideration: `{patch['gate']['allow_future_mixed_dataset_consideration']}`
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = _load_config(_resolve(args.config))
    rpc_url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or ""
    if not rpc_url:
        raise SystemExit("LABEL_RPC_URL or EVM_HTTP_URL is required")
    rpc = CountedRPC(rpc_url)
    chain = rpc.call("eth_chainId", [])
    if int(chain["result"], 16) != 8453:
        raise SystemExit(f"unexpected chain id {int(chain['result'], 16)}")

    outputs = cfg["outputs"]
    forensic_rows, forensic_summary = _forensic_h165_v3(rpc, cfg)
    write_csv_rows(_resolve(outputs["forensic_csv"]), forensic_rows)
    write_json(_resolve(outputs["forensic_json"]), {"summary": forensic_summary, "rows": forensic_rows})

    bridge = _coverage_report(cfg, _select_v3_candidates(cfg, int(cfg["pilot"].get("first_size", 100))))
    write_json(_resolve(outputs["bridge_coverage_json"]), bridge)

    pilot_100_rows, pilot_100 = _run_pilot(rpc, cfg, int(cfg["pilot"].get("first_size", 100)))
    write_csv_rows(_resolve(outputs["pilot_100_csv"]), pilot_100_rows)
    write_json(_resolve(outputs["pilot_100_json"]), pilot_100)

    pilot_300: dict[str, Any] | None = None
    pilot_300_rows: list[dict[str, Any]] = []
    if float(pilot_100.get("success_rate") or 0.0) >= float(cfg["pilot"].get("larger_gate_success_rate", 0.60)):
        pilot_300_rows, pilot_300 = _run_pilot(rpc, cfg, int(cfg["pilot"].get("larger_size", 300)))
        write_csv_rows(_resolve(outputs["pilot_300_csv"]), pilot_300_rows)
        write_json(_resolve(outputs["pilot_300_json"]), pilot_300)

    audit = _quote_path_audit(forensic_rows, pilot_100)
    write_json(_resolve(outputs["quote_path_audit_json"]), audit)

    decoded_cov = float(pilot_100.get("decoded_coverage", {}).get("coverage") or 0.0)
    success_rate = float(pilot_100.get("success_rate") or 0.0)
    gate = {
        "allow_v3r2_larger_v3_pilot": success_rate >= 0.60,
        "allow_v3_mainline": False,
        "allow_future_mixed_dataset_consideration": success_rate >= 0.80 and decoded_cov >= 0.90,
        "reason": "V3 remains quarantined unless V3-only success and decoded coverage gates are strong.",
    }
    patch = {
        "patch": "run-004-v3r1-executable-labeler-repair-branch",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {k: str(_resolve(v)) for k, v in cfg.get("input_paths", {}).items()},
        "outputs": {k: str(_resolve(v)) for k, v in outputs.items()},
        "forensic_summary": forensic_summary,
        "quote_path_audit": audit,
        "bridge_coverage": bridge,
        "pilot_100_checkpoint": pilot_100,
        "pilot_300_checkpoint": pilot_300,
        "gate": gate,
        "safety_assertions": {
            **cfg.get("safety", {}),
            "rpc_url_written_to_artifacts": False,
            "v2_mainline_affected": False,
        },
    }
    write_json(_resolve(outputs["patch_json"]), patch)
    write_markdown(_resolve(outputs["markdown"]), _markdown(patch))
    print(json.dumps({"gate": gate, "pilot_100": pilot_100, "outputs": patch["outputs"]}, indent=2))


if __name__ == "__main__":
    main()
