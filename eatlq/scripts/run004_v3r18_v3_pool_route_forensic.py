"""Run-004 Patch V3R-18 — V3 pool state / route semantics forensic.

Research-only/read-only. Small-sample forensic only; no expansion, no training,
no production signals, no runtime behavior changes.
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
    QUOTER_V2_BASE,
    STRUCT_SELECTOR,
    WETH_BASE,
    JsonRpcClient,
    build_quote_exact_input_single_calldata,
)

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active"

V3R17_JSON = ROOT / "mvp/diagnostics/run004_v3r17_v3_future_horizon_labeler_summary.json"
V3R17_CSV = ROOT / "mvp/diagnostics/run004_v3r17_v3_future_horizon_label_rows.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r18-v3-pool-route-forensic.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r18_v3_pool_route_forensic_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r18_v3_pool_route_forensic_samples.csv"

SOURCE_RUN = "run004_v3r18_v3_pool_route_forensic"
V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

NOTIONALS_WETH = [0.000001, 0.000005, 0.00001, 0.00005, 0.0001, 0.0005]
FEE_TIERS = [500, 3000, 10000]
NEAR_TOTAL_LOSS = -0.95

TOKEN0_SELECTOR = "0dfe1681"
TOKEN1_SELECTOR = "d21220a7"
FEE_SELECTOR = "ddca3f43"
LIQUIDITY_SELECTOR = "1a686502"
SLOT0_SELECTOR = "3850c7bd"
BALANCE_OF_SELECTOR = "70a08231"
GET_POOL_SELECTOR = "1698ee82"


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


def _pad_uint(n: int) -> str:
    return hex(int(n))[2:].rjust(64, "0")


def _block_tag(block: int | str) -> str:
    return hex(block) if isinstance(block, int) else block


def _decode_uint(res: str | None, word: int = 0, default: int = 0) -> int:
    if not res or res == "0x":
        return default
    raw = res[2:] if res.startswith("0x") else res
    start = word * 64
    chunk = raw[start:start + 64]
    return int(chunk, 16) if len(chunk) == 64 else default


def _decode_int24_word(res: str | None, word: int = 1) -> int:
    value = _decode_uint(res, word=word)
    if value >= 2**255:
        value -= 2**256
    return int(value)


def _decode_addr(res: str | None) -> str:
    if not res or res == "0x":
        return ""
    raw = res[2:] if res.startswith("0x") else res
    return "0x" + raw[-40:].lower()


def eth_call(rpc: JsonRpcClient, to: str, data: str, block: int | str, timeout: int = 25) -> str:
    return rpc.call("eth_call", [{"to": to, "data": data}, _block_tag(block)], timeout=timeout)


def quote_detailed(
    rpc: JsonRpcClient,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    block: int | str,
) -> dict[str, Any]:
    data = build_quote_exact_input_single_calldata(token_in, token_out, amount_in, fee)
    out = {
        "ok": False,
        "amount_out": 0,
        "sqrt_price_x96_after": 0,
        "initialized_ticks_crossed": None,
        "gas_estimate": 0,
        "reason": "",
    }
    try:
        res = eth_call(rpc, QUOTER_V2_BASE, data, block)
    except Exception as exc:
        out["reason"] = "quoter_revert" if "revert" in str(exc).lower() else "rpc_error"
        out["error"] = str(exc)[:220]
        return out
    amount_out = _decode_uint(res, 0)
    out.update(
        {
            "ok": amount_out > 0,
            "amount_out": amount_out,
            "sqrt_price_x96_after": _decode_uint(res, 1),
            "initialized_ticks_crossed": _decode_uint(res, 2),
            "gas_estimate": _decode_uint(res, 3),
            "reason": "" if amount_out > 0 else "zero_amount_out",
        }
    )
    return out


def load_v3r17_rows() -> list[dict[str, Any]]:
    rows = []
    for r in csv.DictReader(V3R17_CSV.open("r", encoding="utf-8")):
        row = dict(r)
        row["future_horizon_return_float"] = float(r["future_horizon_return"]) if r.get("future_horizon_return") else None
        row["same_block_return_float"] = float(r["same_block_roundtrip_return"]) if r.get("same_block_roundtrip_return") else None
        row["entry_block_int"] = int(r["entry_block"])
        row["exit_block_int"] = int(r["exit_block"])
        row["fee_int"] = int(r["fee"])
        rows.append(row)
    return rows


def select_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    near = [r for r in rows if (r["future_horizon_return_float"] is not None and r["future_horizon_return_float"] <= NEAR_TOTAL_LOSS)]
    non_near = [r for r in rows if r not in near]
    selected = []
    for r in near[:10]:
        selected.append({**r, "forensic_group": "near_total_loss"})
    for r in non_near[:5]:
        selected.append({**r, "forensic_group": "non_near_total_loss"})
    return selected[:20]


def load_candidate_map() -> dict[tuple[str, int], dict[str, Any]]:
    out = {}
    for r in csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")):
        pool = _lower(r.get("pool_address"))
        entry = int(r.get("entry_block") or r.get("block_number") or 0)
        if pool and entry:
            out[(pool, entry)] = dict(r)
    return out


def direction_audit(row: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {"status": "direction_unknown", "reason": "candidate_not_found"}
    token_in = _lower(candidate.get("tokenIn"))
    token_out = _lower(candidate.get("tokenOut"))
    is_buy = str(candidate.get("is_buy_weth_to_token", "")).strip() == "1"
    is_sell = str(candidate.get("is_sell_token_to_weth", "")).strip() == "1"
    expected_token = _lower(row["token_out"])
    original_is_weth_buy = is_buy or (token_in == WETH_BASE and token_out == expected_token)
    status = "direction_match" if original_is_weth_buy else "direction_mismatch"
    return {
        "status": status,
        "original_is_weth_buy_token": original_is_weth_buy,
        "is_buy_weth_to_token": is_buy,
        "is_sell_token_to_weth": is_sell,
        "candidate_token_in": token_in,
        "candidate_token_out": token_out,
        "candidate_weth_side_amount_raw": candidate.get("weth_side_amount_raw"),
        "amount0": candidate.get("amount0"),
        "amount1": candidate.get("amount1"),
        "tx_hash": candidate.get("tx_hash"),
        "log_index": candidate.get("log_index"),
    }


def read_pool_state(rpc: JsonRpcClient, pool: str, token: str, block: int) -> dict[str, Any]:
    try:
        token0 = _decode_addr(eth_call(rpc, pool, "0x" + TOKEN0_SELECTOR, block))
        token1 = _decode_addr(eth_call(rpc, pool, "0x" + TOKEN1_SELECTOR, block))
        fee = _decode_uint(eth_call(rpc, pool, "0x" + FEE_SELECTOR, block))
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        weth_balance = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_balance = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {
            "ok": True,
            "token0": token0,
            "token1": token1,
            "fee": fee,
            "liquidity": liquidity,
            "sqrt_price_x96": _decode_uint(slot0, 0),
            "tick": _decode_int24_word(slot0, 1),
            "weth_balance": weth_balance,
            "token_balance": token_balance,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:220]}


def state_change(entry: dict[str, Any], exit_state: dict[str, Any]) -> dict[str, Any]:
    fields = ["sqrt_price_x96", "tick", "liquidity", "weth_balance", "token_balance"]
    changed = {f: entry.get(f) != exit_state.get(f) for f in fields}
    return {
        "changed": changed,
        "any_changed": any(changed.values()),
        "all_static": not any(changed.values()),
    }


def run_ladder(rpc: JsonRpcClient, row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for notional in NOTIONALS_WETH:
        amount_in = int(notional * 10**18)
        entry = quote_detailed(rpc, WETH_BASE, row["token_out"], amount_in, int(row["fee_int"]), int(row["entry_block_int"]))
        same_ret = None
        horizon_ret = None
        same_exit = {"ok": False, "amount_out": 0, "initialized_ticks_crossed": None}
        horizon_exit = {"ok": False, "amount_out": 0, "initialized_ticks_crossed": None}
        if entry["ok"]:
            token_amount = int(entry["amount_out"])
            same_exit = quote_detailed(rpc, row["token_out"], WETH_BASE, token_amount, int(row["fee_int"]), int(row["entry_block_int"]))
            horizon_exit = quote_detailed(rpc, row["token_out"], WETH_BASE, token_amount, int(row["fee_int"]), int(row["exit_block_int"]))
            if same_exit["ok"]:
                same_ret = round(int(same_exit["amount_out"]) / amount_in - 1.0, 8)
            if horizon_exit["ok"]:
                horizon_ret = round(int(horizon_exit["amount_out"]) / amount_in - 1.0, 8)
        out.append(
            {
                "notional_weth": notional,
                "amount_in_raw": amount_in,
                "entry_amount_out": entry["amount_out"],
                "same_block_return": same_ret,
                "horizon_return": horizon_ret,
                "same_block_near_total_loss": same_ret is not None and same_ret <= NEAR_TOTAL_LOSS,
                "horizon_near_total_loss": horizon_ret is not None and horizon_ret <= NEAR_TOTAL_LOSS,
                "price_impact_like_loss": horizon_ret,
                "entry_initialized_ticks_crossed": entry.get("initialized_ticks_crossed"),
                "same_exit_initialized_ticks_crossed": same_exit.get("initialized_ticks_crossed"),
                "horizon_exit_initialized_ticks_crossed": horizon_exit.get("initialized_ticks_crossed"),
            }
        )
    return out


def get_pool(rpc: JsonRpcClient, token: str, fee: int, block: int) -> str:
    data = "0x" + GET_POOL_SELECTOR + _pad_addr(WETH_BASE) + _pad_addr(token) + _pad_uint(fee)
    return _decode_addr(eth_call(rpc, V3_FACTORY, data, block))


def best_fee_route(rpc: JsonRpcClient, row: dict[str, Any], token_amount: int) -> dict[str, Any]:
    routes = []
    for fee in FEE_TIERS:
        try:
            pool = get_pool(rpc, row["token_out"], fee, int(row["exit_block_int"]))
            if not pool or pool == ZERO_ADDR:
                routes.append({"fee": fee, "pool": pool or ZERO_ADDR, "exists": False})
                continue
            liq = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, int(row["exit_block_int"])))
            quote = quote_detailed(rpc, row["token_out"], WETH_BASE, token_amount, fee, int(row["exit_block_int"]))
            routes.append(
                {
                    "fee": fee,
                    "pool": pool,
                    "exists": True,
                    "liquidity": liq,
                    "exit_ok": quote["ok"],
                    "exit_amount_out_weth": quote["amount_out"],
                    "initialized_ticks_crossed": quote.get("initialized_ticks_crossed"),
                }
            )
        except Exception as exc:
            routes.append({"fee": fee, "exists": False, "error": str(exc)[:160]})
    best = max((r for r in routes if r.get("exit_ok")), key=lambda x: int(x.get("exit_amount_out_weth", 0)), default=None)
    return {"routes": routes, "best": best}


def classify_sample(
    direction: dict[str, Any],
    ladder: list[dict[str, Any]],
    state: dict[str, Any],
    best_route: dict[str, Any],
    original_horizon_amount: int,
    amount_in_raw: int,
) -> str:
    if direction["status"] == "direction_mismatch":
        return "direction_mismatch"
    smallest = ladder[0]
    largest = ladder[-1]
    if not smallest["horizon_near_total_loss"] and largest["horizon_near_total_loss"]:
        return "notional_too_large"
    best = best_route.get("best")
    if best and int(best.get("exit_amount_out_weth", 0)) > max(original_horizon_amount * 2, int(amount_in_raw * 0.5)):
        return "single_pool_route_bad"
    if state.get("all_static"):
        return "no_pool_state_change"
    if smallest["horizon_near_total_loss"]:
        return "best_route_also_bad" if not best else "token_or_pool_toxic"
    return "unresolved"


def audit_sample(rpc: JsonRpcClient, row: dict[str, Any], candidate: dict[str, Any] | None, idx: int) -> dict[str, Any]:
    direction = direction_audit(row, candidate)
    entry_state = read_pool_state(rpc, row["pool_address"], row["token_out"], int(row["entry_block_int"]))
    exit_state = read_pool_state(rpc, row["pool_address"], row["token_out"], int(row["exit_block_int"]))
    state = state_change(entry_state, exit_state) if entry_state.get("ok") and exit_state.get("ok") else {"all_static": False, "any_changed": None}
    ladder = run_ladder(rpc, row)
    largest = ladder[-1]
    token_amount = int(largest["entry_amount_out"] or row.get("entry_amount_out", 0) or 0)
    best_route = best_fee_route(rpc, row, token_amount) if token_amount > 0 else {"routes": [], "best": None}
    classification = classify_sample(
        direction,
        ladder,
        state,
        best_route,
        int(row.get("future_exit_amount_out_weth", 0) or 0),
        int(0.0005 * 10**18),
    )
    return {
        "idx": idx,
        "group": row["forensic_group"],
        "pool_address": row["pool_address"],
        "token_address": row["token_out"],
        "fee": int(row["fee_int"]),
        "entry_block": int(row["entry_block_int"]),
        "exit_block": int(row["exit_block_int"]),
        "original_candidate": candidate,
        "direction_audit": direction,
        "entry_state": entry_state,
        "exit_state": exit_state,
        "state_change": state,
        "notional_ladder": ladder,
        "best_fee_route": best_route,
        "classification": classification,
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(samples)
    classes = Counter(s["classification"] for s in samples)
    direction_mismatch_count = classes.get("direction_mismatch", 0)
    static_count = sum(1 for s in samples if s["state_change"].get("all_static"))
    best_improves_count = 0
    smallest_near_count = 0
    single_pool_count = classes.get("single_pool_route_bad", 0)
    notional_count = 0
    for s in samples:
        ladder = s["notional_ladder"]
        if ladder and ladder[0]["horizon_near_total_loss"]:
            smallest_near_count += 1
        if ladder and not ladder[0]["horizon_near_total_loss"] and ladder[-1]["horizon_near_total_loss"]:
            notional_count += 1
        best = s["best_fee_route"].get("best")
        if best:
            best_return = int(best.get("exit_amount_out_weth", 0)) / int(0.0005 * 10**18) - 1.0
            orig_return = s["notional_ladder"][-1].get("horizon_return")
            if orig_return is not None and best_return > float(orig_return) + 0.2:
                best_improves_count += 1
    smallest_share = round(smallest_near_count / max(1, n), 4)
    direction_dom = direction_mismatch_count / max(1, n) > 0.5
    notional_dom = notional_count / max(1, n) > 0.5
    static_dom = static_count / max(1, n) > 0.5
    single_pool_dom = single_pool_count / max(1, n) > 0.5
    best_route_improves = best_improves_count > 0
    return {
        "sample_count": n,
        "classification_counts": dict(classes),
        "direction_mismatch_count": direction_mismatch_count,
        "notional_too_large_count": notional_count,
        "pool_state_static_count": static_count,
        "single_pool_route_bad_count": single_pool_count,
        "best_route_improves_count": best_improves_count,
        "smallest_notional_near_total_loss_count": smallest_near_count,
        "smallest_notional_near_total_loss_share": smallest_share,
        "decision_flags": {
            "v3r18_completed": True,
            "direction_mismatch_dominant": direction_dom,
            "notional_size_issue_dominant": notional_dom,
            "pool_state_static_dominant": static_dom,
            "single_pool_route_issue_dominant": single_pool_dom,
            "best_route_improves_exit": best_route_improves,
            "smallest_notional_near_total_loss_share": smallest_share,
            "require_direction_aware_labeler": direction_mismatch_count > 0,
            "require_notional_adaptive_labeler": notional_count > 0,
            "require_best_route_exit_policy": best_route_improves,
            "v3_label_policy_repairable": True,
            "allow_v3r19_label_policy_fix": True,
            "allow_larger_expansion": False,
            "allow_model_research": False,
            "allow_mainline": False,
            "allow_live": False,
        },
    }


def write_ladder_csv(samples: list[dict[str, Any]]) -> None:
    fields = [
        "idx",
        "group",
        "classification",
        "pool_address",
        "token_address",
        "fee",
        "direction_status",
        "entry_block",
        "exit_block",
        "notional_weth",
        "entry_amount_out",
        "same_block_return",
        "horizon_return",
        "horizon_near_total_loss",
        "entry_initialized_ticks_crossed",
        "horizon_exit_initialized_ticks_crossed",
        "pool_state_all_static",
        "best_fee",
        "best_exit_amount_out_weth",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        count = 0
        for s in samples:
            best = s["best_fee_route"].get("best") or {}
            for ladder in s["notional_ladder"]:
                if count >= 100:
                    return
                writer.writerow(
                    {
                        "idx": s["idx"],
                        "group": s["group"],
                        "classification": s["classification"],
                        "pool_address": s["pool_address"],
                        "token_address": s["token_address"],
                        "fee": s["fee"],
                        "direction_status": s["direction_audit"]["status"],
                        "entry_block": s["entry_block"],
                        "exit_block": s["exit_block"],
                        "notional_weth": ladder["notional_weth"],
                        "entry_amount_out": ladder["entry_amount_out"],
                        "same_block_return": ladder["same_block_return"],
                        "horizon_return": ladder["horizon_return"],
                        "horizon_near_total_loss": ladder["horizon_near_total_loss"],
                        "entry_initialized_ticks_crossed": ladder["entry_initialized_ticks_crossed"],
                        "horizon_exit_initialized_ticks_crossed": ladder["horizon_exit_initialized_ticks_crossed"],
                        "pool_state_all_static": s["state_change"].get("all_static"),
                        "best_fee": best.get("fee"),
                        "best_exit_amount_out_weth": best.get("exit_amount_out_weth"),
                    }
                )
                count += 1


def render_markdown(summary: dict[str, Any]) -> str:
    m = summary["metrics"]
    f = m["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in f.items())
    class_lines = "\n".join(f"- `{k}`: {v}" for k, v in m["classification_counts"].items()) or "- none"
    return f"""# Run-004 Patch V3R-18 — V3 Pool State / Route Semantics Forensic

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No expansion.

## 1. Scope

- V3R-17 input: `{summary['inputs']['v3r17_rows']}`
- samples: **{m['sample_count']}** (near-total-loss first 10, non-near-total-loss up to 5)
- notional ladder: `{NOTIONALS_WETH}`
- route fees checked: `{FEE_TIERS}`
- selector: `{STRUCT_SELECTOR}`

## 2. Root Cause Classification

{class_lines}

- direction mismatch count: **{m['direction_mismatch_count']}**
- notional-too-large count: **{m['notional_too_large_count']}**
- pool-state-static count: **{m['pool_state_static_count']}**
- single-pool-route-bad count: **{m['single_pool_route_bad_count']}**
- best-route-improves count: **{m['best_route_improves_count']}**

## 3. Notional Ladder

- smallest notional near-total-loss share: **{m['smallest_notional_near_total_loss_share']}**
- interpretation: if this remains high, lowering notional alone does not fix the labels.

## 4. Pool State / Route

- pool_state_static_dominant: **{f['pool_state_static_dominant']}**
- best_route_improves_exit: **{f['best_route_improves_exit']}**
- single_pool_route_issue_dominant: **{f['single_pool_route_issue_dominant']}**

## 5. Required Answers

1. V3R-17 的 `-0.9985` 是否主要由方向错误导致：**{f['direction_mismatch_dominant']}**。
2. 是否由 `0.0005 WETH` notional 过大导致：**{f['notional_size_issue_dominant']}**。
3. entry_block 到 exit_block 池状态是否基本不变：**{f['pool_state_static_dominant']}**。
4. 单池路线是否过窄：**{f['single_pool_route_issue_dominant']}**。
5. best-of-fee route 是否改善：**{f['best_route_improves_exit']}**。
6. near-total-loss 是否仍大面积存在于最小 notional：**{m['smallest_notional_near_total_loss_share']}**。
7. 当前 V3 labeler 应改为：direction-aware + notional-adaptive；best-route exit policy 仅在 best_route_improves_exit=true 时强制。
8. 是否允许 V3R-19 修 labeler policy：**{f['allow_v3r19_label_policy_fix']}**。
9. 是否允许 expansion：**{f['allow_larger_expansion']}**。
10. 是否允许 model research：**{f['allow_model_research']}**。
11. 是否允许 mainline/live：**False / False**。

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc = JsonRpcClient(_rpc_url())
    v3r17_rows = load_v3r17_rows()
    selected = select_samples(v3r17_rows)
    candidate_map = load_candidate_map()
    samples = []
    for idx, row in enumerate(selected, start=1):
        candidate = candidate_map.get((_lower(row["pool_address"]), int(row["entry_block_int"])))
        samples.append(audit_sample(rpc, row, candidate, idx))
        print(f"[sample] {idx}/{len(selected)} {samples[-1]['classification']}")
    metrics = summarize(samples)
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r17_summary": str(V3R17_JSON.relative_to(ROOT)),
            "v3r17_rows": str(V3R17_CSV.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "metrics": metrics,
        "samples": samples,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_ladder_csv(samples)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] samples={len(samples)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
