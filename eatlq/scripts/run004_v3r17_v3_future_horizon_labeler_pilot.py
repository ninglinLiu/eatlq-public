"""Run-004 Patch V3R-17 — V3 future horizon labeler semantics pilot.

Research-only/read-only. This fixes label semantics at pilot level by using a
future exit block for label_return. Same-block round-trip remains sanity only.
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

V3R16_JSON = ROOT / "mvp/diagnostics/run004_v3r16_v3_label_quality_audit_summary.json"
V3R15_CSV = ROOT / "mvp/diagnostics/run004_v3r15_bounded_v3_label_rows.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
V3R12_SCRIPT = ROOT / "scripts/run004_v3r12_v3_data_supply.py"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r17-v3-future-horizon-labeler.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r17_v3_future_horizon_labeler_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r17_v3_future_horizon_label_rows.csv"

SOURCE_RUN = "run004_v3r17_v3_future_horizon_labeler"
SELECTOR = "c6a5026a"
POOL_LIMIT = min(int(os.environ.get("V3R17_POOL_LIMIT", "50")), 50)
ROW_LIMIT = min(int(os.environ.get("V3R17_ROW_LIMIT", "100")), 100)
FIXED_WETH = 0.0005
FIXED_WETH_RAW = int(FIXED_WETH * 10**18)
FALLBACK_EXIT_BLOCK_OFFSET = 900


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


def load_v3r16_context() -> dict[str, Any]:
    if not V3R16_JSON.exists():
        return {"exists": False}
    data = json.loads(V3R16_JSON.read_text(encoding="utf-8"))
    flags = data.get("decision_flags", {})
    return {
        "exists": True,
        "require_labeler_semantics_fix": bool(flags.get("require_labeler_semantics_fix")),
        "v3r15_labels_trainable": bool(flags.get("v3r15_labels_trainable")),
        "allow_larger_v3_expansion": bool(flags.get("allow_larger_v3_expansion")),
        "near_total_loss_count": int(flags.get("near_total_loss_count", 0)),
        "near_total_loss_share": float(flags.get("near_total_loss_share", 0.0)),
    }


def audit_exit_block_policy() -> dict[str, Any]:
    text = V3R12_SCRIPT.read_text(encoding="utf-8") if V3R12_SCRIPT.exists() else ""
    return {
        "policy": "v3r12_candidate_exit_block",
        "description": "Use `exit_block` from V3R-12 novel candidate CSV. Source code sets `exit_block = block_number + EXIT_BLOCK_OFFSET`.",
        "source_file": "scripts/run004_v3r12_v3_data_supply.py",
        "source_constant_detected": "EXIT_BLOCK_OFFSET = 900" in text,
        "offset_blocks": FALLBACK_EXIT_BLOCK_OFFSET,
        "timestamp_semantics": "fixed block offset pilot, not final 30-minute timestamp mapping",
        "valid_for_pilot": "EXIT_BLOCK_OFFSET = 900" in text and NOVEL_CSV.exists(),
    }


def load_exit_block_map() -> dict[tuple[str, int], dict[str, Any]]:
    out: dict[tuple[str, int], dict[str, Any]] = {}
    rows = list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))
    for row in rows:
        pool = _lower(row.get("pool_address"))
        entry = int(row.get("entry_block") or row.get("block_number") or 0)
        if not pool or entry <= 0:
            continue
        out[(pool, entry)] = {
            "exit_block": int(row.get("exit_block") or entry + FALLBACK_EXIT_BLOCK_OFFSET),
            "token0": _lower(row.get("token0")),
            "token1": _lower(row.get("token1")),
            "fee_tier": int(row.get("fee_tier") or row.get("fee_bps") or 0),
            "candidate_source": "cache/run004_v3r12/v3r12_novel_pool_candidates.csv",
        }
    return out


def load_sample_rows(exit_map: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in csv.DictReader(V3R15_CSV.open("r", encoding="utf-8")):
        pool = _lower(row["pool_address"])
        entry_block = int(row["entry_block"])
        meta = exit_map.get((pool, entry_block), {})
        exit_block = int(meta.get("exit_block") or entry_block + FALLBACK_EXIT_BLOCK_OFFSET)
        rows.append(
            {
                "pool_address": pool,
                "token_address": _lower(row["token_address"]),
                "token_in": _lower(row["token_in"]),
                "token_out": _lower(row["token_out"]),
                "fee": int(row["fee"]),
                "amount_in_weth": float(row["amount_in_weth"]),
                "entry_block": entry_block,
                "exit_block": exit_block,
                "exit_block_source": "candidate_csv" if meta else "fallback_entry_plus_900",
                "v3r15_same_block_return": float(row["net_return"]) if row.get("net_return") else None,
                "candidate_fee_matches": int(meta.get("fee_tier", row["fee"])) == int(row["fee"]),
            }
        )
    return rows[: min(POOL_LIMIT, ROW_LIMIT)]


def quote_attempt(rpc: JsonRpcClient, row: dict[str, Any]) -> dict[str, Any]:
    entry = quote_exact_input_single_struct(
        rpc=rpc,
        token_in=row["token_in"],
        token_out=row["token_out"],
        amount_in=FIXED_WETH_RAW,
        fee=int(row["fee"]),
        block_identifier=int(row["entry_block"]),
    )
    same_exit = None
    horizon_exit = None
    same_block_return = None
    horizon_return = None
    failure_reason = ""
    if not entry.get("ok"):
        failure_reason = "entry_" + (entry.get("reason") or "quote_fail")
    else:
        token_amount = int(entry["amount_out"])
        same_exit = quote_exact_input_single_struct(
            rpc=rpc,
            token_in=row["token_out"],
            token_out=row["token_in"],
            amount_in=token_amount,
            fee=int(row["fee"]),
            block_identifier=int(row["entry_block"]),
        )
        if same_exit.get("ok"):
            same_block_return = round(int(same_exit["amount_out"]) / FIXED_WETH_RAW - 1.0, 8)
        horizon_exit = quote_exact_input_single_struct(
            rpc=rpc,
            token_in=row["token_out"],
            token_out=row["token_in"],
            amount_in=token_amount,
            fee=int(row["fee"]),
            block_identifier=int(row["exit_block"]),
        )
        if horizon_exit.get("ok"):
            horizon_return = round(int(horizon_exit["amount_out"]) / FIXED_WETH_RAW - 1.0, 8)
        else:
            failure_reason = "horizon_exit_" + (horizon_exit.get("reason") or "quote_fail")

    return {
        "pool_address": row["pool_address"],
        "token_address": row["token_address"],
        "token_in": row["token_in"],
        "token_out": row["token_out"],
        "fee": int(row["fee"]),
        "amount_in_weth": FIXED_WETH,
        "entry_block": int(row["entry_block"]),
        "exit_block": int(row["exit_block"]),
        "exit_block_source": row["exit_block_source"],
        "exit_block_offset": int(row["exit_block"]) - int(row["entry_block"]),
        "entry_quote_ok": bool(entry.get("ok")),
        "entry_amount_out": int(entry.get("amount_out") or 0),
        "same_block_exit_quote_ok": bool(same_exit and same_exit.get("ok")),
        "same_block_exit_amount_out_weth": int(same_exit.get("amount_out") or 0) if same_exit else 0,
        "same_block_roundtrip_return": same_block_return,
        "future_exit_quote_ok": bool(horizon_exit and horizon_exit.get("ok")),
        "future_exit_amount_out_weth": int(horizon_exit.get("amount_out") or 0) if horizon_exit else 0,
        "future_horizon_return": horizon_return,
        "label_return": horizon_return,
        "failure_reason": failure_reason,
        "quote_selector": SELECTOR,
        "same_block_is_label": False,
        "label_block_used": int(row["exit_block"]),
        "source_run": SOURCE_RUN,
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


def distribution(values: list[float]) -> dict[str, float | None]:
    vals = sorted(values)
    return {
        "min": quantile(vals, 0.0),
        "p1": quantile(vals, 0.01),
        "p5": quantile(vals, 0.05),
        "median": quantile(vals, 0.50),
        "p95": quantile(vals, 0.95),
        "p99": quantile(vals, 0.99),
        "max": quantile(vals, 1.0),
    }


def share_top(values: list[str]) -> dict[str, float]:
    if not values:
        return {"top1": 0.0, "top5": 0.0}
    cnt = Counter(values)
    n = len(values)
    top = cnt.most_common(5)
    return {
        "top1": round(top[0][1] / n, 4) if top else 0.0,
        "top5": round(sum(v for _, v in top) / n, 4),
    }


def duplicate_conflicts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = (
            f"{r['pool_address']}|{r['entry_block']}|{r['exit_block']}|"
            f"{r['amount_in_weth']}|{r['token_in']}|{r['token_out']}"
        )
        grouped.setdefault(key, []).append(r)
    duplicate_key_count = sum(1 for v in grouped.values() if len(v) > 1)
    conflict = 0
    for values in grouped.values():
        if len(values) <= 1:
            continue
        first = values[0]
        for other in values[1:]:
            if (
                other["entry_amount_out"] != first["entry_amount_out"]
                or other["future_exit_amount_out_weth"] != first["future_exit_amount_out_weth"]
                or other["label_return"] != first["label_return"]
                or other["failure_reason"] != first["failure_reason"]
            ):
                conflict += 1
                break
    return duplicate_key_count, conflict


def summarize(rows: list[dict[str, Any]], exit_policy: dict[str, Any]) -> dict[str, Any]:
    attempts = len(rows)
    same_returns = [float(r["same_block_roundtrip_return"]) for r in rows if r["same_block_roundtrip_return"] is not None]
    horizon_returns = [float(r["future_horizon_return"]) for r in rows if r["future_horizon_return"] is not None]
    horizon_success = len(horizon_returns)
    same_near = sum(1 for x in same_returns if x <= -0.95)
    horizon_near = sum(1 for x in horizon_returns if x <= -0.95)
    duplicate_key_count, duplicate_conflict_count = duplicate_conflicts(rows)
    pool_shares = share_top([r["pool_address"] for r in rows if r["future_horizon_return"] is not None])
    token_shares = share_top([r["token_address"] for r in rows if r["future_horizon_return"] is not None])
    horizon_success_rate = round(horizon_success / max(1, attempts), 4)
    same_near_share = round(same_near / max(1, len(same_returns)), 4)
    horizon_near_share = round(horizon_near / max(1, len(horizon_returns)), 4)
    exit_block_used = all(int(r["label_block_used"]) == int(r["exit_block"]) for r in rows)
    exit_policy_valid = bool(exit_policy["valid_for_pilot"]) and all(r["exit_block_source"] == "candidate_csv" for r in rows)
    horizon_distribution = distribution(horizon_returns)
    same_distribution = distribution(same_returns)
    horizon_differs = horizon_distribution != same_distribution and horizon_near_share < same_near_share
    candidate_trainable = (
        exit_block_used
        and exit_policy_valid
        and horizon_success_rate >= 0.90
        and horizon_near_share <= 0.30
        and duplicate_conflict_count == 0
    )
    return {
        "attempts": attempts,
        "entry_quote_success_rate": round(sum(1 for r in rows if r["entry_quote_ok"]) / max(1, attempts), 4),
        "horizon_label_available_count": horizon_success,
        "horizon_label_success_rate": horizon_success_rate,
        "same_block_near_total_loss_count": same_near,
        "same_block_near_total_loss_share": same_near_share,
        "horizon_near_total_loss_count": horizon_near,
        "horizon_near_total_loss_share": horizon_near_share,
        "same_block_return_distribution": same_distribution,
        "horizon_label_return_distribution": horizon_distribution,
        "horizon_distribution_differs_from_same_block": horizon_differs,
        "failure_reason_distribution": dict(Counter(r["failure_reason"] for r in rows if r["failure_reason"])),
        "duplicate_key_count": duplicate_key_count,
        "duplicate_conflict_count": duplicate_conflict_count,
        "top1_pool_share": pool_shares["top1"],
        "top5_pool_share": pool_shares["top5"],
        "top1_token_share": token_shares["top1"],
        "top5_token_share": token_shares["top5"],
        "exit_block_used_for_quote": exit_block_used,
        "exit_block_policy_valid": exit_policy_valid,
        "decision_flags": {
            "v3r17_completed": True,
            "future_exit_block_used": exit_block_used,
            "exit_block_policy_valid": exit_policy_valid,
            "same_block_return_only_sanity": True,
            "horizon_label_created": horizon_success > 0,
            "horizon_label_success_rate": horizon_success_rate,
            "same_block_near_total_loss_share": same_near_share,
            "horizon_near_total_loss_share": horizon_near_share,
            "v3r17_labels_candidate_trainable": candidate_trainable,
            "require_more_label_audit": True,
            "allow_v3r18_small_horizon_expansion": (
                exit_block_used
                and exit_policy_valid
                and horizon_success_rate >= 0.90
                and horizon_near_share <= 0.30
                and duplicate_conflict_count == 0
            ),
            "allow_model_research": False,
            "allow_mainline": False,
            "allow_live": False,
        },
    }


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "pool_address",
        "token_address",
        "token_in",
        "token_out",
        "fee",
        "amount_in_weth",
        "entry_block",
        "exit_block",
        "exit_block_source",
        "exit_block_offset",
        "entry_quote_ok",
        "entry_amount_out",
        "same_block_exit_quote_ok",
        "same_block_exit_amount_out_weth",
        "same_block_roundtrip_return",
        "future_exit_quote_ok",
        "future_exit_amount_out_weth",
        "future_horizon_return",
        "label_return",
        "failure_reason",
        "quote_selector",
        "same_block_is_label",
        "label_block_used",
        "source_run",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:ROW_LIMIT]:
            writer.writerow({k: row.get(k) for k in fields})


def render_markdown(summary: dict[str, Any]) -> str:
    metrics = summary["metrics"]
    flags = metrics["decision_flags"]
    same = metrics["same_block_return_distribution"]
    horizon = metrics["horizon_label_return_distribution"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    failure_lines = "\n".join(
        f"- `{k}`: {v}" for k, v in metrics["failure_reason_distribution"].items()
    ) or "- none"
    return f"""# Run-004 Patch V3R-17 — V3 Future Horizon Labeler Semantics Fix

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Exit Block Policy

- policy: `{summary['exit_block_policy']['policy']}`
- source: `{summary['exit_block_policy']['source_file']}`
- source constant detected: **{summary['exit_block_policy']['source_constant_detected']}**
- offset blocks: **{summary['exit_block_policy']['offset_blocks']}**
- timestamp semantics: **{summary['exit_block_policy']['timestamp_semantics']}**
- valid for pilot: **{summary['exit_block_policy']['valid_for_pilot']}**
- exit_block used for quote: **{metrics['exit_block_used_for_quote']}**

## 2. Pilot Scope

- input rows: `{summary['inputs']['v3r15_csv']}`
- sampled pools / attempts: **{summary['sampled_pools']} / {metrics['attempts']}**
- fixed notional: **0.0005 WETH**
- selector: `{summary['quote_selector']}`
- label_return source: **future_horizon_return only**
- same_block_roundtrip_return: **sanity only, not label**

## 3. Same-Block vs Horizon

- same_block near-total-loss share: **{metrics['same_block_near_total_loss_share']}** ({metrics['same_block_near_total_loss_count']} rows)
- horizon near-total-loss share: **{metrics['horizon_near_total_loss_share']}** ({metrics['horizon_near_total_loss_count']} rows)
- horizon label success rate: **{metrics['horizon_label_success_rate']}** ({metrics['horizon_label_available_count']} / {metrics['attempts']})
- same_block return distribution: min={same['min']}, p1={same['p1']}, p5={same['p5']}, median={same['median']}, p95={same['p95']}, p99={same['p99']}, max={same['max']}
- horizon return distribution: min={horizon['min']}, p1={horizon['p1']}, p5={horizon['p5']}, median={horizon['median']}, p95={horizon['p95']}, p99={horizon['p99']}, max={horizon['max']}
- horizon distribution differs from same-block: **{metrics['horizon_distribution_differs_from_same_block']}**
- failure reasons:
{failure_lines}

## 4. Quality Gates

- duplicate key count: **{metrics['duplicate_key_count']}**
- duplicate conflict count: **{metrics['duplicate_conflict_count']}**
- top1/top5 pool share: **{metrics['top1_pool_share']} / {metrics['top5_pool_share']}**
- top1/top5 token share: **{metrics['top1_token_share']} / {metrics['top5_token_share']}**
- exit_block_policy_valid: **{metrics['exit_block_policy_valid']}**
- v3r17_labels_candidate_trainable: **{flags['v3r17_labels_candidate_trainable']}**

## 5. Required Answers

1. V3R-17 是否真正使用 future exit block：**{flags['future_exit_block_used']}**。
2. exit_block policy：使用 V3R-12 candidate CSV 的 `exit_block = entry_block + 900`，这是 fixed-block pilot，不是最终 timestamp horizon。
3. 是否仍计算 same-block return：**是**，但只作为 sanity，`same_block_return_only_sanity = true`。
4. horizon label 成功率：**{metrics['horizon_label_success_rate']}**。
5. same_block near_total_loss_share：**{metrics['same_block_near_total_loss_share']}**。
6. horizon near_total_loss_share：**{metrics['horizon_near_total_loss_share']}**。
7. horizon_return 分布是否比 V3R-15 正常：**{metrics['horizon_distribution_differs_from_same_block']}**。
8. 是否还有 `-0.9985` 簇：**{metrics['horizon_near_total_loss_count'] > 0}**。
9. V3R-17 label rows 是否可作为候选训练数据：**{flags['v3r17_labels_candidate_trainable']}**。
10. 是否允许 V3R-18 小规模 horizon label expansion：**{flags['allow_v3r18_small_horizon_expansion']}**。
11. 是否允许 model research：**{flags['allow_model_research']}**。
12. 是否允许 mainline/live/promote：**False / False / False**。

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc = JsonRpcClient(_rpc_url())
    v3r16 = load_v3r16_context()
    exit_policy = audit_exit_block_policy()
    exit_map = load_exit_block_map()
    sample = load_sample_rows(exit_map)
    rows = []
    for idx, row in enumerate(sample, start=1):
        rows.append(quote_attempt(rpc, row))
        if idx % 10 == 0:
            print(f"[pilot] {idx}/{len(sample)}")
    metrics = summarize(rows, exit_policy)
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "quote_selector": SELECTOR,
        "inputs": {
            "v3r16_summary": str(V3R16_JSON.relative_to(ROOT)),
            "v3r15_csv": str(V3R15_CSV.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "v3r16_context": v3r16,
        "exit_block_policy": exit_policy,
        "sampled_pools": len(rows),
        "metrics": metrics,
        "runtime_seconds": round(time.time() - t0, 2),
        "rpc_calls_total": rpc.calls,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] rows={len(rows)} rpc_calls={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
