"""Run-004 Patch V3R-19 — direction-aware + notional-adaptive V3 label policy pilot.

Research-only/read-only. This script repairs V3 label policy at pilot level only:
- infer observed swap direction from verified candidate CSV fields;
- avoid forced WETH->token entries;
- use capped/adaptive notional;
- gate trainability on future-horizon quote and pool state change.
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

V3R18_JSON = ROOT / "mvp/diagnostics/run004_v3r18_v3_pool_route_forensic_summary.json"
V3R18_CSV = ROOT / "mvp/diagnostics/run004_v3r18_v3_pool_route_forensic_samples.csv"
V3R17_CSV = ROOT / "mvp/diagnostics/run004_v3r17_v3_future_horizon_label_rows.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r19-v3-label-policy-repair.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r19_v3_label_policy_repair_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r19_v3_label_policy_repair_rows.csv"

SOURCE_RUN = "run004_v3r19_v3_label_policy_repair"
ROW_LIMIT = 50
WETH_DECIMALS = 10**18
NOTIONAL_CAP_WETH = 0.00005
NOTIONAL_CAP_RAW = int(NOTIONAL_CAP_WETH * WETH_DECIMALS)
DEFAULT_NOTIONAL_WETH = 0.00001
DEFAULT_NOTIONAL_RAW = int(DEFAULT_NOTIONAL_WETH * WETH_DECIMALS)
LADDER_WETH = [0.000001, 0.000005, 0.00001, 0.00005]

LIQUIDITY_SELECTOR = "1a686502"
SLOT0_SELECTOR = "3850c7bd"
BALANCE_OF_SELECTOR = "70a08231"


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


def _decode_uint(res: str | None, word: int = 0, default: int = 0) -> int:
    if not res or res == "0x":
        return default
    raw = res[2:] if res.startswith("0x") else res
    chunk = raw[word * 64:word * 64 + 64]
    return int(chunk, 16) if len(chunk) == 64 else default


def _block_tag(block: int | str) -> str:
    return hex(block) if isinstance(block, int) else block


def eth_call(rpc: JsonRpcClient, to: str, data: str, block: int | str) -> str:
    return rpc.call("eth_call", [{"to": to, "data": data}, _block_tag(block)], timeout=25)


def read_csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def field_audit() -> dict[str, Any]:
    candidate_header = read_csv_header(NOVEL_CSV)
    v3r17_header = read_csv_header(V3R17_CSV)
    required_direction = [
        "token0",
        "token1",
        "amount0",
        "amount1",
        "tokenIn",
        "tokenOut",
        "weth_side_amount_raw",
        "is_buy_weth_to_token",
        "is_sell_token_to_weth",
        "tx_hash",
        "log_index",
    ]
    optional_direction = ["amount0_delta", "amount1_delta", "side", "direction"]
    return {
        "candidate_csv": str(NOVEL_CSV.relative_to(ROOT)),
        "candidate_header": candidate_header,
        "v3r17_header": v3r17_header,
        "required_direction_fields_present": {f: f in candidate_header for f in required_direction},
        "optional_direction_fields_present": {f: f in candidate_header for f in optional_direction},
        "direction_fields_sufficient": all(f in candidate_header for f in required_direction),
    }


def load_candidate_map() -> dict[tuple[str, int], dict[str, Any]]:
    out = {}
    for row in csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")):
        pool = _lower(row.get("pool_address"))
        entry = int(row.get("entry_block") or row.get("block_number") or 0)
        if pool and entry:
            out[(pool, entry)] = dict(row)
    return out


def load_v3r17_rows() -> list[dict[str, Any]]:
    rows = []
    for row in csv.DictReader(V3R17_CSV.open("r", encoding="utf-8")):
        r = dict(row)
        r["entry_block_int"] = int(r["entry_block"])
        r["exit_block_int"] = int(r["exit_block"])
        r["fee_int"] = int(r["fee"])
        rows.append(r)
    return rows[:ROW_LIMIT]


def infer_direction(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {"observed_direction": "UNKNOWN", "reason": "candidate_missing"}
    token_in = _lower(candidate.get("tokenIn"))
    token_out = _lower(candidate.get("tokenOut"))
    is_buy = str(candidate.get("is_buy_weth_to_token", "")).strip() == "1"
    is_sell = str(candidate.get("is_sell_token_to_weth", "")).strip() == "1"
    if is_buy or (token_in == WETH_BASE and token_out and token_out != WETH_BASE):
        return {"observed_direction": "WETH_TO_TOKEN", "reason": "explicit_or_tokenIn_tokenOut", "token_in": token_in, "token_out": token_out}
    if is_sell or (token_out == WETH_BASE and token_in and token_in != WETH_BASE):
        return {"observed_direction": "TOKEN_TO_WETH", "reason": "explicit_or_tokenIn_tokenOut", "token_in": token_in, "token_out": token_out}
    token0 = _lower(candidate.get("token0"))
    token1 = _lower(candidate.get("token1"))
    try:
        amount0 = int(candidate.get("amount0") or 0)
        amount1 = int(candidate.get("amount1") or 0)
        if token0 == WETH_BASE:
            return {"observed_direction": "WETH_TO_TOKEN" if amount0 > 0 else "TOKEN_TO_WETH", "reason": "amount0_sign"}
        if token1 == WETH_BASE:
            return {"observed_direction": "WETH_TO_TOKEN" if amount1 > 0 else "TOKEN_TO_WETH", "reason": "amount1_sign"}
    except Exception:
        pass
    return {"observed_direction": "UNKNOWN", "reason": "insufficient_direction_fields"}


def choose_notional(candidate: dict[str, Any] | None) -> dict[str, Any]:
    observed_raw = 0
    if candidate and str(candidate.get("weth_side_amount_raw") or "").strip():
        try:
            observed_raw = int(float(candidate.get("weth_side_amount_raw") or 0))
        except Exception:
            observed_raw = 0
    if observed_raw > 0:
        selected_raw = min(observed_raw, NOTIONAL_CAP_RAW)
        reason = "min_observed_weth_amount_and_cap"
    else:
        selected_raw = DEFAULT_NOTIONAL_RAW
        reason = "default_no_observed_weth_amount"
    return {
        "observed_weth_amount_raw": observed_raw,
        "observed_weth_amount": observed_raw / WETH_DECIMALS,
        "selected_notional_raw": selected_raw,
        "selected_notional_weth": selected_raw / WETH_DECIMALS,
        "selected_notional_reason": reason,
        "notional_cap_weth": NOTIONAL_CAP_WETH,
    }


def read_pool_state(rpc: JsonRpcClient, pool: str, token: str, block: int) -> dict[str, Any]:
    try:
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        weth_balance = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_balance = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {
            "ok": True,
            "sqrt_price_x96": _decode_uint(slot0, 0),
            "tick_raw": _decode_uint(slot0, 1),
            "liquidity": liquidity,
            "weth_balance": weth_balance,
            "token_balance": token_balance,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:220]}


def state_changed(entry_state: dict[str, Any], exit_state: dict[str, Any]) -> bool:
    fields = ["sqrt_price_x96", "tick_raw", "liquidity", "weth_balance", "token_balance"]
    if not entry_state.get("ok") or not exit_state.get("ok"):
        return False
    return any(entry_state.get(f) != exit_state.get(f) for f in fields)


def quote_long_label(
    rpc: JsonRpcClient,
    token: str,
    fee: int,
    selected_raw: int,
    entry_block: int,
    exit_block: int,
) -> dict[str, Any]:
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, selected_raw, fee, block_identifier=entry_block)
    same_exit = None
    horizon_exit = None
    same_return = None
    horizon_return = None
    failure_reason = ""
    if not entry["ok"]:
        failure_reason = "entry_" + (entry.get("reason") or "quote_fail")
    else:
        token_amount = int(entry["amount_out"])
        same_exit = quote_exact_input_single_struct(rpc, token, WETH_BASE, token_amount, fee, block_identifier=entry_block)
        horizon_exit = quote_exact_input_single_struct(rpc, token, WETH_BASE, token_amount, fee, block_identifier=exit_block)
        if same_exit["ok"]:
            same_return = round(int(same_exit["amount_out"]) / selected_raw - 1.0, 8)
        if horizon_exit["ok"]:
            horizon_return = round(int(horizon_exit["amount_out"]) / selected_raw - 1.0, 8)
        else:
            failure_reason = "horizon_exit_" + (horizon_exit.get("reason") or "quote_fail")
    return {
        "entry_quote_ok": bool(entry.get("ok")),
        "entry_amount_out": int(entry.get("amount_out") or 0),
        "same_block_sanity_return": same_return,
        "future_horizon_return": horizon_return,
        "label_return": horizon_return,
        "horizon_quote_ok": bool(horizon_exit and horizon_exit.get("ok")),
        "failure_reason": failure_reason,
    }


def ladder_audit(rpc: JsonRpcClient, token: str, fee: int, entry_block: int, exit_block: int) -> list[dict[str, Any]]:
    out = []
    for weth in LADDER_WETH:
        raw = int(weth * WETH_DECIMALS)
        q = quote_long_label(rpc, token, fee, raw, entry_block, exit_block)
        out.append(
            {
                "notional_weth": weth,
                "same_block_sanity_return": q["same_block_sanity_return"],
                "future_horizon_return": q["future_horizon_return"],
                "near_total_loss": q["future_horizon_return"] is not None and q["future_horizon_return"] <= -0.95,
            }
        )
    return out


def audit_row(rpc: JsonRpcClient, row: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    direction = infer_direction(candidate)
    notional = choose_notional(candidate)
    token = _lower(row["token_address"])
    entry_block = int(row["entry_block_int"])
    exit_block = int(row["exit_block_int"])
    entry_state = read_pool_state(rpc, row["pool_address"], token, entry_block)
    exit_state = read_pool_state(rpc, row["pool_address"], token, exit_block)
    changed = state_changed(entry_state, exit_state)

    observed_direction = direction["observed_direction"]
    selected_entry_direction = observed_direction if observed_direction != "UNKNOWN" else "UNKNOWN"
    direction_mismatch_avoided = observed_direction == "TOKEN_TO_WETH"
    quote = {
        "entry_quote_ok": False,
        "entry_amount_out": 0,
        "same_block_sanity_return": None,
        "future_horizon_return": None,
        "label_return": None,
        "horizon_quote_ok": False,
        "failure_reason": "",
    }
    ladder = []
    if observed_direction == "WETH_TO_TOKEN":
        quote = quote_long_label(
            rpc,
            token,
            int(row["fee_int"]),
            int(notional["selected_notional_raw"]),
            entry_block,
            exit_block,
        )
        ladder = ladder_audit(rpc, token, int(row["fee_int"]), entry_block, exit_block)

    non_trainable_reason = ""
    trainable = False
    if observed_direction == "UNKNOWN":
        non_trainable_reason = "direction_unknown"
    elif observed_direction == "TOKEN_TO_WETH":
        non_trainable_reason = "sell_observation_not_buy_entry"
    elif not changed:
        non_trainable_reason = "no_state_change"
    elif not quote["horizon_quote_ok"]:
        non_trainable_reason = quote["failure_reason"] or "horizon_quote_fail"
    else:
        trainable = True

    return {
        "pool_address": row["pool_address"],
        "token_address": token,
        "raw_candidate_direction": observed_direction,
        "direction_inference_reason": direction["reason"],
        "selected_entry_direction": selected_entry_direction,
        "direction_mismatch_avoided": direction_mismatch_avoided,
        "observed_weth_amount_raw": notional["observed_weth_amount_raw"],
        "observed_weth_amount": notional["observed_weth_amount"],
        "selected_notional": notional["selected_notional_weth"],
        "selected_notional_raw": notional["selected_notional_raw"],
        "selected_notional_reason": notional["selected_notional_reason"],
        "notional_cap_weth": notional["notional_cap_weth"],
        "entry_block": entry_block,
        "exit_block": exit_block,
        "state_changed": changed,
        "same_block_sanity_return": quote["same_block_sanity_return"],
        "future_horizon_return": quote["future_horizon_return"],
        "label_return": quote["label_return"],
        "entry_quote_ok": quote["entry_quote_ok"],
        "horizon_quote_ok": quote["horizon_quote_ok"],
        "trainable_candidate_flag": trainable,
        "non_trainable_reason": non_trainable_reason,
        "quote_selector": STRUCT_SELECTOR,
        "source_run": SOURCE_RUN,
        "candidate_tx_hash": candidate.get("tx_hash") if candidate else "",
        "candidate_log_index": candidate.get("log_index") if candidate else "",
        "notional_ladder_audit": ladder,
    }


def quantile(values: list[float], q: float) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    if q <= 0:
        return vals[0]
    if q >= 1:
        return vals[-1]
    idx = (len(vals) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    frac = idx - lo
    return round(vals[lo] * (1 - frac) + vals[hi] * frac, 8)


def distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "min": quantile(values, 0),
        "p5": quantile(values, 0.05),
        "median": quantile(values, 0.5),
        "p95": quantile(values, 0.95),
        "max": quantile(values, 1),
    }


def duplicate_conflicts(rows: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = f"{r['pool_address']}|{r['entry_block']}|{r['exit_block']}|{r['selected_notional_raw']}|{r['raw_candidate_direction']}"
        grouped.setdefault(key, []).append(r)
    conflicts = 0
    for values in grouped.values():
        if len(values) <= 1:
            continue
        first = values[0]
        for other in values[1:]:
            if (
                first["label_return"] != other["label_return"]
                or first["trainable_candidate_flag"] != other["trainable_candidate_flag"]
                or first["non_trainable_reason"] != other["non_trainable_reason"]
            ):
                conflicts += 1
                break
    return conflicts


def summarize(rows: list[dict[str, Any]], fields: dict[str, Any]) -> dict[str, Any]:
    total = len(rows)
    known = [r for r in rows if r["raw_candidate_direction"] != "UNKNOWN"]
    weth_to_token = [r for r in rows if r["raw_candidate_direction"] == "WETH_TO_TOKEN"]
    token_to_weth = [r for r in rows if r["raw_candidate_direction"] == "TOKEN_TO_WETH"]
    adaptive_rows = [r for r in rows if r["selected_notional_reason"] in {"min_observed_weth_amount_and_cap", "default_no_observed_weth_amount"}]
    changed = [r for r in rows if r["state_changed"]]
    no_change = [r for r in rows if not r["state_changed"]]
    attempted = [r for r in rows if r["raw_candidate_direction"] == "WETH_TO_TOKEN"]
    horizon_ok = [r for r in attempted if r["horizon_quote_ok"]]
    label_returns = [float(r["label_return"]) for r in horizon_ok if r["label_return"] is not None]
    near_share = round(sum(1 for x in label_returns if x <= -0.95) / max(1, len(label_returns)), 4)
    trainable = [r for r in rows if r["trainable_candidate_flag"]]
    non_trainable_reasons = Counter(r["non_trainable_reason"] for r in rows if not r["trainable_candidate_flag"])
    selected_notional_counter = Counter(str(r["selected_notional"]) for r in rows)
    duplicate_conflict_count = duplicate_conflicts(rows)
    require_new_source = (
        len(token_to_weth) + len(no_change) > total * 0.5
        or len(trainable) == 0
    )
    success = (
        near_share <= 0.30
        and len(trainable) > 0
        and duplicate_conflict_count == 0
        and fields["direction_fields_sufficient"]
    )
    return {
        "total_rows": total,
        "direction_known_count": len(known),
        "direction_unknown_count": total - len(known),
        "weth_to_token_count": len(weth_to_token),
        "token_to_weth_count": len(token_to_weth),
        "direction_mismatch_avoided_count": sum(1 for r in rows if r["direction_mismatch_avoided"]),
        "adaptive_notional_rows_count": len(adaptive_rows),
        "selected_notional_distribution": dict(selected_notional_counter),
        "state_changed_count": len(changed),
        "no_state_change_count": len(no_change),
        "horizon_quote_success_rate": round(len(horizon_ok) / max(1, len(attempted)), 4),
        "horizon_quote_success_numerator": len(horizon_ok),
        "horizon_quote_success_denominator": len(attempted),
        "label_return_distribution": distribution(label_returns),
        "near_total_loss_share_after_policy": near_share,
        "trainable_candidate_count": len(trainable),
        "non_trainable_reason_distribution": dict(non_trainable_reasons),
        "duplicate_conflict_count": duplicate_conflict_count,
        "decision_flags": {
            "v3r19_completed": True,
            "direction_policy_implemented": True,
            "no_forced_weth_to_token_entry": True,
            "notional_adaptive_policy_implemented": True,
            "horizon_state_change_gate_implemented": True,
            "near_total_loss_share_after_policy": near_share,
            "trainable_candidate_count": len(trainable),
            "v3_label_policy_repair_successful": success,
            "require_new_candidate_source": require_new_source,
            "allow_v3r20_policy_validation": len(trainable) > 0 and fields["direction_fields_sufficient"],
            "allow_larger_expansion": False,
            "allow_model_research": False,
            "allow_mainline": False,
            "allow_live": False,
        },
    }


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "pool_address",
        "token_address",
        "raw_candidate_direction",
        "selected_entry_direction",
        "direction_mismatch_avoided",
        "observed_weth_amount_raw",
        "selected_notional",
        "selected_notional_reason",
        "notional_cap_weth",
        "entry_block",
        "exit_block",
        "state_changed",
        "same_block_sanity_return",
        "future_horizon_return",
        "label_return",
        "entry_quote_ok",
        "horizon_quote_ok",
        "trainable_candidate_flag",
        "non_trainable_reason",
        "quote_selector",
        "source_run",
        "candidate_tx_hash",
        "candidate_log_index",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:100]:
            writer.writerow({k: row.get(k) for k in fields})


def render_markdown(summary: dict[str, Any]) -> str:
    m = summary["metrics"]
    flags = m["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    reason_lines = "\n".join(f"- `{k}`: {v}" for k, v in m["non_trainable_reason_distribution"].items()) or "- none"
    return f"""# Run-004 Patch V3R-19 — V3 Label Policy Repair Pilot

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Field Audit

- candidate CSV: `{summary['field_audit']['candidate_csv']}`
- direction fields sufficient: **{summary['field_audit']['direction_fields_sufficient']}**
- required fields present: `{summary['field_audit']['required_direction_fields_present']}`
- optional direction fields present: `{summary['field_audit']['optional_direction_fields_present']}`

## 2. Direction Policy

- total rows: **{m['total_rows']}**
- direction known / unknown: **{m['direction_known_count']} / {m['direction_unknown_count']}**
- WETH_TO_TOKEN / TOKEN_TO_WETH: **{m['weth_to_token_count']} / {m['token_to_weth_count']}**
- direction mismatch avoided: **{m['direction_mismatch_avoided_count']}**
- no forced WETH->token entry: **{flags['no_forced_weth_to_token_entry']}**

## 3. Adaptive Notional

- cap: **{NOTIONAL_CAP_WETH} WETH**
- default: **{DEFAULT_NOTIONAL_WETH} WETH**
- adaptive notional rows: **{m['adaptive_notional_rows_count']}**
- selected notional distribution: `{m['selected_notional_distribution']}`
- near_total_loss_share_after_policy: **{m['near_total_loss_share_after_policy']}**

## 4. Horizon State-Change Gate

- state_changed_count: **{m['state_changed_count']}**
- no_state_change_count: **{m['no_state_change_count']}**
- horizon quote success rate: **{m['horizon_quote_success_rate']}** ({m['horizon_quote_success_numerator']} / {m['horizon_quote_success_denominator']})
- label return distribution: `{m['label_return_distribution']}`

## 5. Trainable Candidate Result

- trainable_candidate_count: **{m['trainable_candidate_count']}**
- duplicate_conflict_count: **{m['duplicate_conflict_count']}**
- non-trainable reasons:
{reason_lines}

## 6. Required Answers

1. V3R-19 是否避免 V3R-17 强制 WETH->token mismatch：**{flags['no_forced_weth_to_token_entry']}**。
2. adaptive notional 是否消除 0.0005 WETH 大面积 near-total-loss：**near_total_loss_share_after_policy={m['near_total_loss_share_after_policy']}**。
3. 因 pool state static 被排除的样本：**{m['no_state_change_count']}**。
4. 最终 candidate trainable rows：**{m['trainable_candidate_count']}**。
5. 是否允许 V3R-20 policy validation：**{flags['allow_v3r20_policy_validation']}**。
6. 是否允许 larger expansion/model/mainline/live：**False / False / False / False**。

## 7. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc = JsonRpcClient(_rpc_url())
    fields = field_audit()
    candidate_map = load_candidate_map()
    v3r17_rows = load_v3r17_rows()
    out_rows = []
    for idx, row in enumerate(v3r17_rows, start=1):
        candidate = candidate_map.get((_lower(row["pool_address"]), int(row["entry_block_int"])))
        out_rows.append(audit_row(rpc, row, candidate))
        if idx % 10 == 0:
            print(f"[pilot] {idx}/{len(v3r17_rows)}")
    metrics = summarize(out_rows, fields)
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r18_summary": str(V3R18_JSON.relative_to(ROOT)),
            "v3r18_samples": str(V3R18_CSV.relative_to(ROOT)),
            "v3r17_rows": str(V3R17_CSV.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "field_audit": fields,
        "policy": {
            "direction_policy": "Use observed_direction; do not force TOKEN_TO_WETH observations into WETH_TO_TOKEN long labels.",
            "notional_policy": "selected_notional_raw = min(observed_weth_amount_raw, 0.00005 WETH cap), else 0.00001 WETH default.",
            "horizon_policy": "fixed-block candidate horizon from V3R-12 exit_block; trainability gated by state_changed.",
        },
        "metrics": metrics,
        "rows": out_rows,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out_rows)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] rows={len(out_rows)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
