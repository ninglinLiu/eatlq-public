"""Run-004 Patch V3R-15 — bounded V3 dataset expansion (research-only).

Goal:
- Reuse V3R-14 validated QuoterV2 struct ABI helper.
- Build a bounded, auditable, rollback-safe V3 label dataset pilot.
- No model training, no production signals, no live/promotion behavior.
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

V3R14_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r14_v3_labeler_bridge_summary.json"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r15-bounded-v3-dataset-expansion.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r15_bounded_v3_dataset_expansion_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r15_bounded_v3_label_rows.csv"

POOL_LIMIT = min(int(os.environ.get("V3R15_POOL_LIMIT", "150")), 150)
ROW_LIMIT = min(int(os.environ.get("V3R15_ROW_LIMIT", "800")), 800)
BLOCK_EVIDENCE_LIMIT = min(int(os.environ.get("V3R15_BLOCK_EVIDENCE_LIMIT", "10")), 20)
FIXED_NOTIONAL_WETH = float(os.environ.get("V3R15_FIXED_WETH", "0.0005"))
FIXED_NOTIONAL_RAW = int(FIXED_NOTIONAL_WETH * 10**18)

SELECTOR = "c6a5026a"
LIQUIDITY_SELECTOR = "1a686502"
SOURCE_RUN = "run004_v3r15_bounded_v3_dataset_expansion"


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


def _decode_first_uint(res: str | None) -> int:
    if not res or res == "0x":
        return 0
    raw = res[2:] if res.startswith("0x") else res
    return int(raw[:64], 16) if len(raw) >= 64 else 0


def _pad_addr(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(64, "0")


def load_v3r14_context() -> dict[str, Any]:
    if not V3R14_SUMMARY.exists():
        return {"exists": False}
    data = json.loads(V3R14_SUMMARY.read_text(encoding="utf-8"))
    return {
        "exists": True,
        "labeler_bridge_ready": bool(data.get("decision_flags", {}).get("labeler_bridge_ready")),
        "allow_v3r15_bounded_dataset_expansion": bool(
            data.get("decision_flags", {}).get("allow_v3r15_bounded_dataset_expansion")
        ),
        "old_quote_path_in_active_labeler": bool(data.get("decision_flags", {}).get("old_quote_path_in_active_labeler")),
        "pilot_pools": int(data.get("pilot", {}).get("pools", 0)),
        "pilot_quote_success_rate": float(data.get("pilot", {}).get("entry_quote_success_rate", 0.0)),
        "pilot_roundtrip_success_rate": float(data.get("pilot", {}).get("roundtrip_success_rate", 0.0)),
    }


def load_candidates() -> list[dict[str, Any]]:
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
                "token_address": token1 if token0 == WETH_BASE else token0,
                "token_in": WETH_BASE,
                "token_out": token1 if token0 == WETH_BASE else token0,
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "exit_block": int(row.get("exit_block") or row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    candidates = list(by_pool.values())
    # deterministic bounded sample: first N from canonical V3R-12 novel corpus
    return candidates[: min(POOL_LIMIT, ROW_LIMIT)]


def read_liquidity(rpc: JsonRpcClient, pool: str, block: int) -> int:
    try:
        res = rpc.call("eth_call", [{"to": pool, "data": "0x" + LIQUIDITY_SELECTOR}, hex(block)], timeout=20)
        return _decode_first_uint(res)
    except Exception:
        return -1


def classify_failure(entry: dict[str, Any], exit_q: dict[str, Any] | None, liquidity_hint: int) -> str:
    if liquidity_hint == 0:
        return "zero_liquidity"
    if entry.get("reason") in {"rpc_error"} or (exit_q and exit_q.get("reason") in {"rpc_error"}):
        return "rpc_error"
    if entry.get("reason") in {"quoter_revert"} or (exit_q and exit_q.get("reason") in {"quoter_revert"}):
        return "quoter_revert"
    if not entry.get("ok"):
        return f"entry_{entry.get('reason') or 'quote_fail'}"
    if exit_q and not exit_q.get("ok"):
        return f"exit_{exit_q.get('reason') or 'quote_fail'}"
    return "unknown"


def run_attempts(rpc: JsonRpcClient, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates, start=1):
        block_used = int(cand["entry_block"])
        entry = quote_exact_input_single_struct(
            rpc=rpc,
            token_in=cand["token_in"],
            token_out=cand["token_out"],
            amount_in=FIXED_NOTIONAL_RAW,
            fee=int(cand["fee"]),
            block_identifier=block_used,
        )
        exit_q = None
        net_return = None
        failure_reason = ""
        if entry.get("ok"):
            exit_q = quote_exact_input_single_struct(
                rpc=rpc,
                token_in=cand["token_out"],
                token_out=cand["token_in"],
                amount_in=int(entry.get("amount_out") or 0),
                fee=int(cand["fee"]),
                block_identifier=block_used,
            )
            if exit_q.get("ok"):
                net_return = round(int(exit_q["amount_out"]) / FIXED_NOTIONAL_RAW - 1.0, 8)
            else:
                liquidity_hint = read_liquidity(rpc, cand["pool_address"], block_used)
                failure_reason = classify_failure(entry, exit_q, liquidity_hint)
        else:
            liquidity_hint = read_liquidity(rpc, cand["pool_address"], block_used)
            failure_reason = classify_failure(entry, None, liquidity_hint)

        rows.append(
            {
                "pool_address": cand["pool_address"],
                "token_address": cand["token_address"],
                "token_in": cand["token_in"],
                "token_out": cand["token_out"],
                "fee": int(cand["fee"]),
                "amount_in_weth": FIXED_NOTIONAL_WETH,
                "entry_block": int(cand["entry_block"]),
                "entry_quote_ok": bool(entry.get("ok")),
                "entry_amount_out": int(entry.get("amount_out") or 0),
                "exit_quote_ok": bool(exit_q and exit_q.get("ok")),
                "exit_amount_out_weth": int(exit_q.get("amount_out") or 0) if exit_q else 0,
                "net_return": net_return,
                "failure_reason": failure_reason,
                "quote_selector": SELECTOR,
                "block_used": block_used,
                "source_run": SOURCE_RUN,
            }
        )
        if idx % 25 == 0:
            print(f"[attempt] {idx}/{len(candidates)}")
    return rows


def run_block_side_evidence(rpc: JsonRpcClient, rows: list[dict[str, Any]]) -> dict[str, Any]:
    sample = rows[:BLOCK_EVIDENCE_LIMIT]
    evidence_rows = []
    block_sensitive = 0
    for row in sample:
        block_minus_1 = max(0, int(row["entry_block"]) - 1)
        q_minus_1 = quote_exact_input_single_struct(
            rpc=rpc,
            token_in=row["token_in"],
            token_out=row["token_out"],
            amount_in=FIXED_NOTIONAL_RAW,
            fee=int(row["fee"]),
            block_identifier=block_minus_1,
        )
        status_minus_1 = "entry_ok" if q_minus_1.get("ok") else (q_minus_1.get("reason") or "entry_fail")
        status_main = "entry_ok" if row["entry_quote_ok"] else row["failure_reason"]
        sensitive = status_minus_1 != status_main
        if sensitive:
            block_sensitive += 1
        evidence_rows.append(
            {
                "pool_address": row["pool_address"],
                "entry_block": row["entry_block"],
                "entry_block_minus_1": block_minus_1,
                "entry_main_status": status_main,
                "entry_minus_1_status": status_minus_1,
                "block_sensitive": sensitive,
            }
        )
    return {
        "sample_size": len(sample),
        "block_sensitive_count": block_sensitive,
        "rows": evidence_rows,
    }


def quantile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    idx = (len(sorted_values) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac, 8)


def compute_duplicates(rows: list[dict[str, Any]]) -> tuple[int, int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = f"{r['pool_address']}|{r['entry_block']}|{r['amount_in_weth']}|{r['token_in']}|{r['token_out']}"
        grouped.setdefault(key, []).append(r)
    duplicate_key_count = sum(1 for vs in grouped.values() if len(vs) > 1)
    duplicate_conflict_count = 0
    for values in grouped.values():
        if len(values) <= 1:
            continue
        first = values[0]
        for other in values[1:]:
            if (
                other["entry_quote_ok"] != first["entry_quote_ok"]
                or other["exit_quote_ok"] != first["exit_quote_ok"]
                or other["failure_reason"] != first["failure_reason"]
                or other["entry_amount_out"] != first["entry_amount_out"]
                or other["exit_amount_out_weth"] != first["exit_amount_out_weth"]
            ):
                duplicate_conflict_count += 1
                break
    return duplicate_key_count, duplicate_conflict_count


def share_metrics(values: list[str]) -> dict[str, float]:
    if not values:
        return {"top1": 0.0, "top5": 0.0}
    cnt = Counter(values)
    n = len(values)
    top = cnt.most_common(5)
    top1 = round(top[0][1] / n, 4) if top else 0.0
    top5 = round(sum(v for _, v in top) / n, 4)
    return {"top1": top1, "top5": top5}


def summarize(rows: list[dict[str, Any]], block_side: dict[str, Any]) -> dict[str, Any]:
    attempts = len(rows)
    entry_success = sum(1 for r in rows if r["entry_quote_ok"])
    roundtrip_success = sum(1 for r in rows if r["entry_quote_ok"] and r["exit_quote_ok"])
    failure_reasons = Counter(r["failure_reason"] for r in rows if r["failure_reason"])

    zero_liquidity_count = int(failure_reasons.get("zero_liquidity", 0))
    quoter_revert_count = sum(v for k, v in failure_reasons.items() if "quoter_revert" in k)
    rpc_error_count = sum(v for k, v in failure_reasons.items() if "rpc_error" in k)

    duplicate_key_count, duplicate_conflict_count = compute_duplicates(rows)
    labelable_rows = [r for r in rows if r["entry_quote_ok"] and r["exit_quote_ok"] and r["net_return"] is not None]
    labelable_rows_count = len(labelable_rows)
    net_returns = sorted(float(r["net_return"]) for r in labelable_rows)
    extreme_return_count = sum(1 for x in net_returns if abs(x) > 1.0)
    trainable_candidate_rows_count = sum(1 for x in net_returns if abs(x) <= 1.0)

    pool_shares = share_metrics([r["pool_address"] for r in labelable_rows])
    token_shares = share_metrics([r["token_address"] for r in labelable_rows])

    net_return_distribution = {
        "min": quantile(net_returns, 0.0),
        "p1": quantile(net_returns, 0.01),
        "p5": quantile(net_returns, 0.05),
        "median": quantile(net_returns, 0.50),
        "p95": quantile(net_returns, 0.95),
        "p99": quantile(net_returns, 0.99),
        "max": quantile(net_returns, 1.0),
    }

    quote_success_rate = round(entry_success / max(1, attempts), 4)
    roundtrip_success_rate = round(roundtrip_success / max(1, attempts), 4)
    flags = {
        "v3r15_completed": True,
        "old_quote_path_in_active_labeler": False,
        "bounded_dataset_created": attempts > 0,
        "label_rows_count": attempts,
        "quote_success_rate": quote_success_rate,
        "roundtrip_success_rate": roundtrip_success_rate,
        "duplicate_conflict_count": duplicate_conflict_count,
        "top1_pool_share": pool_shares["top1"],
        "top5_pool_share": pool_shares["top5"],
        "extreme_return_count": extreme_return_count,
        "allow_v3r16_label_quality_audit": quote_success_rate >= 0.95 and roundtrip_success_rate >= 0.95,
        "allow_larger_v3_expansion": quote_success_rate >= 0.95 and roundtrip_success_rate >= 0.95 and duplicate_conflict_count == 0,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    return {
        "attempts": attempts,
        "entry_quote_success": entry_success,
        "quote_success_rate": quote_success_rate,
        "roundtrip_success": roundtrip_success,
        "roundtrip_success_rate": roundtrip_success_rate,
        "failure_reason_distribution": dict(failure_reasons),
        "zero_liquidity_count": zero_liquidity_count,
        "quoter_revert_count": quoter_revert_count,
        "rpc_error_count": rpc_error_count,
        "duplicate_key_count": duplicate_key_count,
        "duplicate_conflict_count": duplicate_conflict_count,
        "top1_pool_share": pool_shares["top1"],
        "top5_pool_share": pool_shares["top5"],
        "top1_token_share": token_shares["top1"],
        "top5_token_share": token_shares["top5"],
        "net_return_distribution": net_return_distribution,
        "extreme_return_rule": "abs(net_return) > 1.0",
        "extreme_return_count": extreme_return_count,
        "labelable_rows_count": labelable_rows_count,
        "trainable_candidate_rows_count": trainable_candidate_rows_count,
        "block_side_evidence": block_side,
        "decision_flags": flags,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    ctx = summary["v3r14_context"]
    met = summary["metrics"]
    flags = met["decision_flags"]
    failure_lines = "\n".join(
        f"- `{k}`: {v}" for k, v in met["failure_reason_distribution"].items()
    ) or "- none"
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    net = met["net_return_distribution"]
    return f"""# Run-004 Patch V3R-15 — Bounded V3 Dataset Expansion

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain.

## 1. Input Context

- V3R-14 summary exists: **{ctx['exists']}**
- V3R-14 labeler bridge ready: **{ctx.get('labeler_bridge_ready', False)}**
- V3R-14 allow V3R-15 bounded expansion: **{ctx.get('allow_v3r15_bounded_dataset_expansion', False)}**
- old quote path in active labeler (from V3R-14): **{ctx.get('old_quote_path_in_active_labeler', True)}**
- quote selector: `{summary['quote_selector']}`

## 2. Bounded Expansion Scope

- candidate source: `{summary['candidate_source']}`
- pool cap: **{summary['config']['pool_limit']}**
- row cap: **{summary['config']['row_limit']}**
- actual sampled pools: **{summary['sampled_pools']}**
- actual label attempts: **{met['attempts']}**
- fixed notional: **{summary['config']['fixed_notional_weth']} WETH**
- block policy: main label uses `entry_block`; `entry_block-1` only side evidence.

## 3. Required Quality Audit

- quote success rate: **{met['quote_success_rate']}** ({met['entry_quote_success']} / {met['attempts']})
- round-trip success rate: **{met['roundtrip_success_rate']}** ({met['roundtrip_success']} / {met['attempts']})
- failure reason distribution:
{failure_lines}
- zero_liquidity count: **{met['zero_liquidity_count']}**
- quoter_revert count: **{met['quoter_revert_count']}**
- rpc_error count: **{met['rpc_error_count']}**
- duplicate key count: **{met['duplicate_key_count']}**
- duplicate conflict count: **{met['duplicate_conflict_count']}**
- top1_pool_share: **{met['top1_pool_share']}**
- top5_pool_share: **{met['top5_pool_share']}**
- top1_token_share: **{met['top1_token_share']}**
- top5_token_share: **{met['top5_token_share']}**
- net_return distribution: min={net['min']}, p1={net['p1']}, p5={net['p5']}, median={net['median']}, p95={net['p95']}, p99={net['p99']}, max={net['max']}
- extreme return count (`{met['extreme_return_rule']}`): **{met['extreme_return_count']}**
- labelable rows count: **{met['labelable_rows_count']}**
- trainable candidate rows count: **{met['trainable_candidate_rows_count']}** (for future audit only, not training authorization)

## 4. Side Evidence (entry_block-1)

- side-evidence sample size: **{met['block_side_evidence']['sample_size']}**
- block-sensitive count: **{met['block_side_evidence']['block_sensitive_count']}**

## 5. Required Answers

1. V3R-15 实际抽样 pools / attempts：**{summary['sampled_pools']} / {met['attempts']}**。
2. entry quote 成功率：**{met['quote_success_rate']}**。
3. round-trip 成功率：**{met['roundtrip_success_rate']}**。
4. 失败主因：**{max(met['failure_reason_distribution'], key=met['failure_reason_distribution'].get) if met['failure_reason_distribution'] else 'none'}**。
5. 旧 `f7729d43` active path：**否**（active labeler/data-supply）。
6. duplicate label conflict：**{met['duplicate_conflict_count']}**。
7. pool/token 集中度：top1/top5 pool = **{met['top1_pool_share']}/{met['top5_pool_share']}**，top1/top5 token = **{met['top1_token_share']}/{met['top5_token_share']}**。
8. net_return 分布：见上方分位数；extreme count = **{met['extreme_return_count']}**。
9. 是否允许进入 V3R-16 label quality audit / larger expansion：**{flags['allow_v3r16_label_quality_audit']} / {flags['allow_larger_v3_expansion']}**。
10. 是否允许 model research：**{flags['allow_model_research']}**。
11. 是否允许 mainline/live/promote：**False / False / False**。

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    context = load_v3r14_context()
    rpc = JsonRpcClient(_rpc_url())
    candidates = load_candidates()
    rows = run_attempts(rpc, candidates)
    block_side = run_block_side_evidence(rpc, rows)
    metrics = summarize(rows, block_side)

    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "quote_selector": SELECTOR,
        "source_run": SOURCE_RUN,
        "helper": "scripts/lib/v3_quoterv2_struct.py",
        "v3r14_context": context,
        "candidate_source": str(NOVEL_CSV.relative_to(ROOT)),
        "config": {
            "pool_limit": POOL_LIMIT,
            "row_limit": ROW_LIMIT,
            "fixed_notional_weth": FIXED_NOTIONAL_WETH,
            "block_policy_main": "entry_block",
            "block_policy_side_evidence": "entry_block_minus_1",
            "block_evidence_limit": BLOCK_EVIDENCE_LIMIT,
        },
        "sampled_pools": len(candidates),
        "metrics": metrics,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)

    ordered_fields = [
        "pool_address",
        "token_address",
        "token_in",
        "token_out",
        "fee",
        "amount_in_weth",
        "entry_block",
        "entry_quote_ok",
        "entry_amount_out",
        "exit_quote_ok",
        "exit_amount_out_weth",
        "net_return",
        "failure_reason",
        "quote_selector",
        "block_used",
        "source_run",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ordered_fields)
        writer.writeheader()
        for row in rows[:ROW_LIMIT]:
            writer.writerow({k: row.get(k) for k in ordered_fields})

    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")

    print(f"[done] rows={len(rows)} pools={len(candidates)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
