"""Run-004 Patch V3R-20 — V3 candidate source repair.

Research-only/read-only. Audits the full V3R-12 candidate source, filters true
WETH->token buy observations, applies a state-change prefilter, then runs a
small V3R-19-style policy label test on at most 50 rows.
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

NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
V3R19_JSON = ROOT / "mvp/diagnostics/run004_v3r19_v3_label_policy_repair_summary.json"
V3R19_CSV = ROOT / "mvp/diagnostics/run004_v3r19_v3_label_policy_repair_rows.csv"
V3R19_SCRIPT = ROOT / "scripts/run004_v3r19_v3_label_policy_repair_pilot.py"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r20-v3-candidate-source-repair.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_rows.csv"

SOURCE_RUN = "run004_v3r20_v3_candidate_source_repair"
ROW_LIMIT = 50
CSV_LIMIT = 100
WETH_DECIMALS = 10**18
NOTIONAL_CAP_WETH = 0.00005
NOTIONAL_CAP_RAW = int(NOTIONAL_CAP_WETH * WETH_DECIMALS)
DEFAULT_NOTIONAL_WETH = 0.00001
DEFAULT_NOTIONAL_RAW = int(DEFAULT_NOTIONAL_WETH * WETH_DECIMALS)

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


def eth_call(rpc: JsonRpcClient, to: str, data: str, block: int) -> str:
    return rpc.call("eth_call", [{"to": to, "data": data}, hex(block)], timeout=25)


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def field_audit() -> dict[str, Any]:
    header = read_header(NOVEL_CSV)
    required = [
        "pool_address",
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
        "entry_block",
        "exit_block",
        "fee_tier",
    ]
    return {
        "candidate_csv": str(NOVEL_CSV.relative_to(ROOT)),
        "header": header,
        "required_fields_present": {f: f in header for f in required},
        "fields_sufficient": all(f in header for f in required),
    }


def load_candidates() -> list[dict[str, Any]]:
    return list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))


def infer_direction(row: dict[str, Any]) -> str:
    token_in = _lower(row.get("tokenIn"))
    token_out = _lower(row.get("tokenOut"))
    is_buy = str(row.get("is_buy_weth_to_token", "")).strip() == "1"
    is_sell = str(row.get("is_sell_token_to_weth", "")).strip() == "1"
    if is_buy or (token_in == WETH_BASE and token_out and token_out != WETH_BASE):
        return "WETH_TO_TOKEN"
    if is_sell or (token_out == WETH_BASE and token_in and token_in != WETH_BASE):
        return "TOKEN_TO_WETH"
    token0 = _lower(row.get("token0"))
    token1 = _lower(row.get("token1"))
    try:
        amount0 = int(row.get("amount0") or 0)
        amount1 = int(row.get("amount1") or 0)
        if token0 == WETH_BASE:
            return "WETH_TO_TOKEN" if amount0 > 0 else "TOKEN_TO_WETH"
        if token1 == WETH_BASE:
            return "WETH_TO_TOKEN" if amount1 > 0 else "TOKEN_TO_WETH"
    except Exception:
        return "UNKNOWN"
    return "UNKNOWN"


def observed_weth_raw(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("weth_side_amount_raw") or 0))
    except Exception:
        return 0


def token_address(row: dict[str, Any]) -> str:
    t0 = _lower(row.get("token0"))
    t1 = _lower(row.get("token1"))
    return t1 if t0 == WETH_BASE else t0


def source_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    directions = [infer_direction(r) for r in rows]
    counts = Counter(directions)
    total = len(rows)
    buy_rows = [r for r, d in zip(rows, directions) if d == "WETH_TO_TOKEN"]
    fee_dist = Counter(str(int(r.get("fee_tier") or r.get("fee_bps") or 0)) for r in buy_rows)
    pool_counts = Counter(_lower(r.get("pool_address")) for r in buy_rows)
    token_counts = Counter(token_address(r) for r in buy_rows)
    observed = sorted(observed_weth_raw(r) / WETH_DECIMALS for r in buy_rows if observed_weth_raw(r) > 0)
    return {
        "total_candidate_rows": total,
        "direction_known_count": counts["WETH_TO_TOKEN"] + counts["TOKEN_TO_WETH"],
        "direction_unknown_count": counts["UNKNOWN"],
        "weth_to_token_count": counts["WETH_TO_TOKEN"],
        "token_to_weth_count": counts["TOKEN_TO_WETH"],
        "weth_to_token_share": round(counts["WETH_TO_TOKEN"] / max(1, total), 4),
        "token_to_weth_share": round(counts["TOKEN_TO_WETH"] / max(1, total), 4),
        "buy_candidate_count": len(buy_rows),
        "buy_candidate_share": round(len(buy_rows) / max(1, total), 4),
        "observed_weth_amount_distribution": {
            "min": observed[0] if observed else None,
            "median": observed[len(observed) // 2] if observed else None,
            "max": observed[-1] if observed else None,
        },
        "fee_distribution": dict(fee_dist),
        "top1_pool_share": round(pool_counts.most_common(1)[0][1] / max(1, len(buy_rows)), 4) if buy_rows else 0.0,
        "top5_pool_share": round(sum(v for _, v in pool_counts.most_common(5)) / max(1, len(buy_rows)), 4) if buy_rows else 0.0,
        "top1_token_share": round(token_counts.most_common(1)[0][1] / max(1, len(buy_rows)), 4) if buy_rows else 0.0,
        "top5_token_share": round(sum(v for _, v in token_counts.most_common(5)) / max(1, len(buy_rows)), 4) if buy_rows else 0.0,
        "buy_rows": buy_rows,
    }


def read_state(rpc: JsonRpcClient, row: dict[str, Any], block: int) -> dict[str, Any]:
    pool = _lower(row["pool_address"])
    token = token_address(row)
    try:
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        weth_bal = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_bal = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {
            "ok": True,
            "slot0": slot0,
            "liquidity": liquidity,
            "weth_balance": weth_bal,
            "token_balance": token_bal,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def state_changes(entry: dict[str, Any], exit_state: dict[str, Any]) -> dict[str, bool]:
    if not entry.get("ok") or not exit_state.get("ok"):
        return {"slot0_changed": False, "liquidity_changed": False, "weth_balance_changed": False, "token_balance_changed": False}
    return {
        "slot0_changed": entry.get("slot0") != exit_state.get("slot0"),
        "liquidity_changed": entry.get("liquidity") != exit_state.get("liquidity"),
        "weth_balance_changed": entry.get("weth_balance") != exit_state.get("weth_balance"),
        "token_balance_changed": entry.get("token_balance") != exit_state.get("token_balance"),
    }


def choose_notional(row: dict[str, Any]) -> dict[str, Any]:
    obs = observed_weth_raw(row)
    if obs > 0:
        selected = min(obs, NOTIONAL_CAP_RAW)
        reason = "min_observed_weth_amount_and_cap"
    else:
        selected = DEFAULT_NOTIONAL_RAW
        reason = "default_no_observed_weth_amount"
    return {
        "observed_weth_amount_raw": obs,
        "selected_notional_raw": selected,
        "selected_notional_weth": selected / WETH_DECIMALS,
        "selected_notional_reason": reason,
    }


def quote_policy(rpc: JsonRpcClient, row: dict[str, Any], selected_raw: int) -> dict[str, Any]:
    token = token_address(row)
    fee = int(row.get("fee_tier") or row.get("fee_bps") or 0)
    entry_block = int(row.get("entry_block") or row.get("block_number") or 0)
    exit_block = int(row.get("exit_block") or entry_block + 900)
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, selected_raw, fee, block_identifier=entry_block)
    if not entry["ok"]:
        return {"horizon_quote_ok": False, "failure_reason": "entry_" + (entry.get("reason") or "quote_fail")}
    same = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(entry["amount_out"]), fee, block_identifier=entry_block)
    horizon = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(entry["amount_out"]), fee, block_identifier=exit_block)
    same_ret = round(int(same["amount_out"]) / selected_raw - 1.0, 8) if same["ok"] else None
    horizon_ret = round(int(horizon["amount_out"]) / selected_raw - 1.0, 8) if horizon["ok"] else None
    return {
        "entry_quote_ok": True,
        "entry_amount_out": int(entry["amount_out"]),
        "same_block_sanity_return": same_ret,
        "future_horizon_return": horizon_ret,
        "label_return": horizon_ret,
        "horizon_quote_ok": bool(horizon["ok"]),
        "failure_reason": "" if horizon["ok"] else "horizon_" + (horizon.get("reason") or "quote_fail"),
    }


def quantile(values: list[float], q: float) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
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
        key = f"{r['pool_address']}|{r['entry_block']}|{r['exit_block']}|{r['selected_notional_raw']}"
        grouped.setdefault(key, []).append(r)
    conflicts = 0
    for vals in grouped.values():
        if len(vals) <= 1:
            continue
        first = vals[0]
        if any(v.get("label_return") != first.get("label_return") or v.get("non_trainable_reason") != first.get("non_trainable_reason") for v in vals[1:]):
            conflicts += 1
    return conflicts


def run_state_prefilter_and_policy(rpc: JsonRpcClient, buy_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out = []
    state_counts = Counter()
    for idx, row in enumerate(buy_rows, start=1):
        entry_block = int(row.get("entry_block") or row.get("block_number") or 0)
        exit_block = int(row.get("exit_block") or entry_block + 900)
        entry_state = read_state(rpc, row, entry_block)
        exit_state = read_state(rpc, row, exit_block)
        changes = state_changes(entry_state, exit_state)
        for k, v in changes.items():
            if v:
                state_counts[k] += 1
        changed = any(changes.values())
        notional = choose_notional(row)
        quote = {"horizon_quote_ok": False, "failure_reason": ""}
        trainable = False
        reason = ""
        if changed and len([r for r in out if r.get("policy_attempted")]) < ROW_LIMIT:
            quote = quote_policy(rpc, row, int(notional["selected_notional_raw"]))
            trainable = bool(quote.get("horizon_quote_ok"))
            reason = "" if trainable else quote.get("failure_reason", "horizon_quote_fail")
        elif not changed:
            reason = "no_state_change"
        else:
            reason = "policy_row_limit"
        out.append(
            {
                "pool_address": _lower(row["pool_address"]),
                "token_address": token_address(row),
                "tx_hash": row.get("tx_hash", ""),
                "log_index": row.get("log_index", ""),
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": entry_block,
                "exit_block": exit_block,
                "observed_direction": "WETH_TO_TOKEN",
                "observed_weth_amount_raw": notional["observed_weth_amount_raw"],
                "selected_notional_raw": notional["selected_notional_raw"],
                "selected_notional_weth": notional["selected_notional_weth"],
                "selected_notional_reason": notional["selected_notional_reason"],
                "state_changed": changed,
                **changes,
                "policy_attempted": changed and reason != "policy_row_limit",
                "horizon_quote_ok": bool(quote.get("horizon_quote_ok")),
                "same_block_sanity_return": quote.get("same_block_sanity_return"),
                "future_horizon_return": quote.get("future_horizon_return"),
                "label_return": quote.get("label_return"),
                "trainable_candidate_flag": trainable,
                "non_trainable_reason": "" if trainable else reason,
                "quote_selector": STRUCT_SELECTOR,
                "source_run": SOURCE_RUN,
            }
        )
        if idx % 25 == 0:
            print(f"[state] {idx}/{len(buy_rows)}")
    state_checked = len(buy_rows)
    changed_count = sum(1 for r in out if r["state_changed"])
    return out, {
        "state_checked_count": state_checked,
        "state_changed_count": changed_count,
        "no_state_change_count": state_checked - changed_count,
        "state_changed_share": round(changed_count / max(1, state_checked), 4),
        "state_change_fields_distribution": dict(state_counts),
    }


def summarize(source: dict[str, Any], state: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = [r for r in rows if r["policy_attempted"]]
    ok = [r for r in attempted if r["horizon_quote_ok"]]
    returns = [float(r["label_return"]) for r in ok if r["label_return"] is not None]
    trainable = [r for r in rows if r["trainable_candidate_flag"]]
    non_trainable = Counter(r["non_trainable_reason"] for r in rows if not r["trainable_candidate_flag"])
    near_share = round(sum(1 for x in returns if x <= -0.95) / max(1, len(returns)), 4)
    enough_buy = source["buy_candidate_count"] >= 50 or source["buy_candidate_count"] >= 20
    require_new = state["state_changed_share"] < 0.2 or len(trainable) == 0
    flags = {
        "v3r20_completed": True,
        "candidate_source_audited": True,
        "source_sell_biased": source["token_to_weth_share"] > source["weth_to_token_share"],
        "enough_weth_to_token_candidates": enough_buy,
        "state_change_prefilter_implemented": True,
        "direction_aware_policy_reused": True,
        "notional_adaptive_policy_reused": True,
        "trainable_candidate_count": len(trainable),
        "require_new_ingestor_or_candidate_builder": require_new,
        "allow_v3r21_small_buy_source_validation": len(trainable) > 0,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    return {
        **{k: v for k, v in source.items() if k != "buy_rows"},
        **state,
        "sampled_buy_candidates": min(len(rows), CSV_LIMIT),
        "attempted_policy_rows": len(attempted),
        "horizon_quote_success_rate": round(len(ok) / max(1, len(attempted)), 4),
        "near_total_loss_share_after_policy": near_share,
        "label_return_distribution": distribution(returns),
        "trainable_candidate_count": len(trainable),
        "non_trainable_reason_distribution": dict(non_trainable),
        "duplicate_conflict_count": duplicate_conflicts(rows),
        "decision_flags": flags,
    }


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "pool_address",
        "token_address",
        "tx_hash",
        "log_index",
        "fee",
        "entry_block",
        "exit_block",
        "observed_direction",
        "observed_weth_amount_raw",
        "selected_notional_weth",
        "selected_notional_reason",
        "state_changed",
        "slot0_changed",
        "liquidity_changed",
        "weth_balance_changed",
        "token_balance_changed",
        "policy_attempted",
        "horizon_quote_ok",
        "same_block_sanity_return",
        "future_horizon_return",
        "label_return",
        "trainable_candidate_flag",
        "non_trainable_reason",
        "quote_selector",
        "source_run",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:CSV_LIMIT]:
            writer.writerow({k: row.get(k) for k in fields})


def render_markdown(summary: dict[str, Any]) -> str:
    m = summary["metrics"]
    flags = m["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-20 — V3 Candidate Source Repair

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Candidate Source Audit

- total_candidate_rows: **{m['total_candidate_rows']}**
- direction_known_count: **{m['direction_known_count']}**
- direction_unknown_count: **{m['direction_unknown_count']}**
- WETH_TO_TOKEN / TOKEN_TO_WETH: **{m['weth_to_token_count']} / {m['token_to_weth_count']}**
- WETH_TO_TOKEN share: **{m['weth_to_token_share']}**
- TOKEN_TO_WETH share: **{m['token_to_weth_share']}**
- source_sell_biased: **{flags['source_sell_biased']}**

## 2. Buy Candidate Filtering

- buy_candidate_count: **{m['buy_candidate_count']}**
- buy_candidate_share: **{m['buy_candidate_share']}**
- sampled_buy_candidates: **{m['sampled_buy_candidates']}**
- observed_weth_amount_distribution: `{m['observed_weth_amount_distribution']}`
- fee_distribution: `{m['fee_distribution']}`
- top1/top5 pool share: **{m['top1_pool_share']} / {m['top5_pool_share']}**
- top1/top5 token share: **{m['top1_token_share']} / {m['top5_token_share']}**

## 3. State-Change Prefilter

- state_checked_count: **{m['state_checked_count']}**
- state_changed_count: **{m['state_changed_count']}**
- no_state_change_count: **{m['no_state_change_count']}**
- state_changed_share: **{m['state_changed_share']}**
- state_change_fields_distribution: `{m['state_change_fields_distribution']}`

## 4. Policy Label Test

- attempted_policy_rows: **{m['attempted_policy_rows']}**
- horizon_quote_success_rate: **{m['horizon_quote_success_rate']}**
- near_total_loss_share_after_policy: **{m['near_total_loss_share_after_policy']}**
- label_return_distribution: `{m['label_return_distribution']}`
- trainable_candidate_count: **{m['trainable_candidate_count']}**
- non_trainable_reason_distribution: `{m['non_trainable_reason_distribution']}`
- duplicate_conflict_count: **{m['duplicate_conflict_count']}**

## 5. Required Answers

1. 当前 V3R-12 source 是否偏向 sell observations：**{flags['source_sell_biased']}**。
2. 是否存在足够 WETH_TO_TOKEN buy observations：**{flags['enough_weth_to_token_candidates']}**。
3. buy observations 中 horizon 内状态变化数量：**{m['state_changed_count']} / {m['state_checked_count']}**。
4. V3R-19 policy 在修复 source 上 trainable count：**{m['trainable_candidate_count']}**。
5. 如果 trainable 仍低，是否需要新采集逻辑：**{flags['require_new_ingestor_or_candidate_builder']}**。

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc = JsonRpcClient(_rpc_url())
    fields = field_audit()
    rows = load_candidates()
    source = source_audit(rows)
    policy_rows, state = run_state_prefilter_and_policy(rpc, source["buy_rows"])
    metrics = summarize(source, state, policy_rows)
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "v3r19_summary": str(V3R19_JSON.relative_to(ROOT)),
            "v3r19_rows": str(V3R19_CSV.relative_to(ROOT)),
            "v3r19_script": str(V3R19_SCRIPT.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "field_audit": fields,
        "metrics": metrics,
        "rows": policy_rows[:CSV_LIMIT],
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(policy_rows)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] candidates={len(rows)} buy={source['buy_candidate_count']} policy_rows={len(policy_rows)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
