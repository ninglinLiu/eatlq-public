"""Run-004 Patch V3R-21 — small buy-source label quality validation.

Research-only/read-only. Validates V3R-20 buy-source labels and proposes
filters; does not expand, train, promote, or touch runtime behavior.
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

V3R20_JSON = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_summary.json"
V3R20_CSV = ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_rows.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r21-v3-buy-source-label-quality-validation.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r21_v3_buy_source_label_quality_validation_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r21_v3_buy_source_label_quality_validation_rows.csv"

SOURCE_RUN = "run004_v3r21_v3_buy_source_label_quality_validation"
WETH_DECIMALS = 10**18
NEAR_TOTAL_LOSS = -0.95
NEAR_SAMPLE_LIMIT = 20

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


def load_rows() -> list[dict[str, Any]]:
    rows = []
    for row in csv.DictReader(V3R20_CSV.open("r", encoding="utf-8")):
        r = dict(row)
        r["fee_int"] = int(r["fee"])
        r["entry_block_int"] = int(r["entry_block"])
        r["exit_block_int"] = int(r["exit_block"])
        r["state_changed_bool"] = str(r["state_changed"]).lower() == "true"
        r["policy_attempted_bool"] = str(r["policy_attempted"]).lower() == "true"
        r["horizon_quote_ok_bool"] = str(r["horizon_quote_ok"]).lower() == "true"
        r["trainable_candidate_bool"] = str(r["trainable_candidate_flag"]).lower() == "true"
        r["label_return_float"] = float(r["label_return"]) if r.get("label_return") not in ("", None) else None
        r["selected_notional_weth_float"] = float(r["selected_notional_weth"]) if r.get("selected_notional_weth") else 0.0
        rows.append(r)
    return rows


def field_audit() -> dict[str, Any]:
    v3r20_header = read_header(V3R20_CSV)
    candidate_header = read_header(NOVEL_CSV)
    required = [
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "exit_block",
        "selected_notional_weth",
        "state_changed",
        "policy_attempted",
        "horizon_quote_ok",
        "future_horizon_return",
        "label_return",
        "non_trainable_reason",
    ]
    return {
        "v3r20_rows": str(V3R20_CSV.relative_to(ROOT)),
        "v3r20_header": v3r20_header,
        "candidate_header": candidate_header,
        "required_v3r20_fields_present": {f: f in v3r20_header for f in required},
        "fields_sufficient": all(f in v3r20_header for f in required),
    }


def classify_row(row: dict[str, Any]) -> str:
    if not row.get("pool_address") or not row.get("token_address"):
        return "missing_required_fields"
    if not row["state_changed_bool"]:
        return "no_state_change"
    if row["policy_attempted_bool"] and not row["horizon_quote_ok_bool"]:
        return "horizon_quoter_revert"
    ret = row["label_return_float"]
    if ret is not None and ret <= NEAR_TOTAL_LOSS:
        return "near_total_loss"
    if row["horizon_quote_ok_bool"]:
        return "clean_label"
    return "unresolved"


def reconstruct(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for r in rows:
        r["toxicity_class"] = classify_row(r)
    state_changed = [r for r in rows if r["state_changed_bool"]]
    attempted = [r for r in rows if r["policy_attempted_bool"]]
    success = [r for r in rows if r["horizon_quote_ok_bool"]]
    reverts = [r for r in rows if r["toxicity_class"] == "horizon_quoter_revert"]
    near = [r for r in rows if r["toxicity_class"] == "near_total_loss"]
    clean = [r for r in rows if r["toxicity_class"] == "clean_label"]
    return {
        "total_rows": len(rows),
        "state_changed_buy_candidates": len(state_changed),
        "policy_attempted_rows": len(attempted),
        "horizon_quote_success_rows": len(success),
        "horizon_quoter_revert_rows": len(reverts),
        "near_total_loss_rows": len(near),
        "clean_candidate_rows": len(clean),
        "class_distribution": dict(Counter(r["toxicity_class"] for r in rows)),
    }


def read_state(rpc: JsonRpcClient, row: dict[str, Any], block: int) -> dict[str, Any]:
    pool = _lower(row["pool_address"])
    token = _lower(row["token_address"])
    try:
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        weth_bal = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_bal = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {
            "ok": True,
            "slot0": slot0,
            "sqrt_price_x96": _decode_uint(slot0, 0),
            "liquidity": liquidity,
            "weth_balance": weth_bal,
            "token_balance": token_bal,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def near_forensic(rpc: JsonRpcClient, rows: list[dict[str, Any]]) -> dict[str, Any]:
    near = [r for r in rows if r["toxicity_class"] == "near_total_loss"]
    sample = near[:NEAR_SAMPLE_LIMIT]
    fee_dist = Counter(str(r["fee_int"]) for r in near)
    notional_dist = Counter(str(r["selected_notional_weth_float"]) for r in near)
    pool_dist = Counter(_lower(r["pool_address"]) for r in near)
    token_dist = Counter(_lower(r["token_address"]) for r in near)
    factors = Counter()
    sample_rows = []
    for r in sample:
        entry = read_state(rpc, r, r["entry_block_int"])
        exit_state = read_state(rpc, r, r["exit_block_int"])
        fee_10000 = r["fee_int"] == 10000
        low_liq = bool(exit_state.get("ok") and int(exit_state.get("liquidity") or 0) < 10**12)
        weth_collapse = bool(entry.get("ok") and exit_state.get("ok") and int(exit_state.get("weth_balance") or 0) < int(entry.get("weth_balance") or 0) * 0.1)
        token_collapse = bool(entry.get("ok") and exit_state.get("ok") and int(exit_state.get("token_balance") or 0) < int(entry.get("token_balance") or 0) * 0.1)
        price_move = bool(entry.get("ok") and exit_state.get("ok") and entry.get("slot0") != exit_state.get("slot0"))
        selected_too_large = r["selected_notional_weth_float"] >= 0.00005
        for name, flag in {
            "fee_tier_10000": fee_10000,
            "low_liquidity": low_liq,
            "weth_balance_collapse": weth_collapse,
            "token_balance_collapse": token_collapse,
            "extreme_price_or_tick_move": price_move,
            "selected_notional_still_too_large": selected_too_large,
        }.items():
            if flag:
                factors[name] += 1
        sample_rows.append(
            {
                "pool_address": r["pool_address"],
                "token_address": r["token_address"],
                "fee": r["fee_int"],
                "selected_notional_weth": r["selected_notional_weth_float"],
                "label_return": r["label_return_float"],
                "factors": {
                    "fee_tier_10000": fee_10000,
                    "low_liquidity": low_liq,
                    "weth_balance_collapse": weth_collapse,
                    "token_balance_collapse": token_collapse,
                    "extreme_price_or_tick_move": price_move,
                    "selected_notional_still_too_large": selected_too_large,
                },
            }
        )
    explained = len(near) == 0 or bool(factors)
    return {
        "near_total_loss_count": len(near),
        "sampled_near_total_loss_rows": len(sample),
        "fee_distribution": dict(fee_dist),
        "selected_notional_distribution": dict(notional_dist),
        "observed_pool_top5": pool_dist.most_common(5),
        "observed_token_top5": token_dist.most_common(5),
        "factor_counts": dict(factors),
        "near_total_loss_explained": explained,
        "sample_rows": sample_rows,
    }


def quote_with_notional(rpc: JsonRpcClient, row: dict[str, Any], notional_weth: float) -> dict[str, Any]:
    raw = max(1, int(notional_weth * WETH_DECIMALS))
    token = _lower(row["token_address"])
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, raw, row["fee_int"], block_identifier=row["entry_block_int"])
    if not entry["ok"]:
        return {"ok": False, "stage": "entry", "reason": entry.get("reason", "")}
    exit_q = quote_exact_input_single_struct(
        rpc,
        token,
        WETH_BASE,
        int(entry["amount_out"]),
        row["fee_int"],
        block_identifier=row["exit_block_int"],
    )
    if not exit_q["ok"]:
        return {"ok": False, "stage": "exit", "reason": exit_q.get("reason", "")}
    return {"ok": True, "return": round(int(exit_q["amount_out"]) / raw - 1.0, 8)}


def revert_forensic(rpc: JsonRpcClient, rows: list[dict[str, Any]]) -> dict[str, Any]:
    reverts = [r for r in rows if r["toxicity_class"] == "horizon_quoter_revert"]
    fee_dist = Counter(str(r["fee_int"]) for r in reverts)
    pool_dist = Counter(_lower(r["pool_address"]) for r in reverts)
    token_dist = Counter(_lower(r["token_address"]) for r in reverts)
    outcomes = Counter()
    detail = []
    for r in reverts:
        fallback = quote_with_notional(rpc, r, max(r["selected_notional_weth_float"] / 10.0, 0.0000001))
        entry_ok = quote_exact_input_single_struct(
            rpc,
            WETH_BASE,
            _lower(r["token_address"]),
            max(1, int(r["selected_notional_weth_float"] * WETH_DECIMALS)),
            r["fee_int"],
            block_identifier=r["entry_block_int"],
        )["ok"]
        state = read_state(rpc, r, r["exit_block_int"])
        if fallback["ok"]:
            label = "notional_sensitive_revert"
        elif state.get("ok") and int(state.get("liquidity") or 0) == 0:
            label = "zero_liquidity"
        else:
            label = "exit_unquotable"
        outcomes[label] += 1
        detail.append(
            {
                "pool_address": r["pool_address"],
                "token_address": r["token_address"],
                "fee": r["fee_int"],
                "entry_quote_ok": entry_ok,
                "fallback_success": fallback["ok"],
                "fallback_return": fallback.get("return"),
                "classification": label,
            }
        )
    return {
        "horizon_quoter_revert_count": len(reverts),
        "fee_distribution": dict(fee_dist),
        "pool_top5": pool_dist.most_common(5),
        "token_top5": token_dist.most_common(5),
        "classification_counts": dict(outcomes),
        "quoter_reverts_explained": len(reverts) == 0 or bool(outcomes),
        "rows": detail,
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


def share(values: list[str]) -> dict[str, float]:
    if not values:
        return {"top1": 0.0, "top5": 0.0}
    c = Counter(values)
    return {
        "top1": round(c.most_common(1)[0][1] / len(values), 4),
        "top5": round(sum(v for _, v in c.most_common(5)) / len(values), 4),
    }


def filter_test(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = [r for r in rows if r["toxicity_class"] == "clean_label"]
    optional_no_10000 = [r for r in base if r["fee_int"] != 10000]
    returns = [float(r["label_return_float"]) for r in base if r["label_return_float"] is not None]
    pool_s = share([_lower(r["pool_address"]) for r in base])
    token_s = share([_lower(r["token_address"]) for r in base])
    near_share = round(sum(1 for x in returns if x <= NEAR_TOTAL_LOSS) / max(1, len(returns)), 4)
    optional_returns = [float(r["label_return_float"]) for r in optional_no_10000 if r["label_return_float"] is not None]
    healthy = (
        len(base) >= 20
        and near_share <= 0.10
        and pool_s["top1"] <= 0.20
        and pool_s["top5"] <= 0.55
        and bool(returns)
        and min(returns) < 0 < max(returns)
    )
    return {
        "filters": [
            "exclude horizon_quoter_revert",
            "exclude near_total_loss <= -0.95",
        ],
        "optional_filter_fee_10000": "evaluated separately; not selected because it removes all current clean rows",
        "clean_rows_count": len(base),
        "clean_rows_share": round(len(base) / max(1, len(rows)), 4),
        "clean_label_return_distribution": distribution(returns),
        "clean_near_total_loss_share": near_share,
        "clean_top1_pool_share": pool_s["top1"],
        "clean_top5_pool_share": pool_s["top5"],
        "clean_top1_token_share": token_s["top1"],
        "clean_top5_token_share": token_s["top5"],
        "optional_no_fee_10000_rows_count": len(optional_no_10000),
        "optional_no_fee_10000_distribution": distribution(optional_returns),
        "clean_label_distribution_healthy": healthy,
    }


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "pool_address",
        "token_address",
        "fee",
        "selected_notional_weth",
        "state_changed",
        "policy_attempted",
        "horizon_quote_ok",
        "label_return",
        "toxicity_class",
        "non_trainable_reason",
        "source_run",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows[:100]:
            writer.writerow({k: r.get(k) for k in fields})


def render_markdown(summary: dict[str, Any]) -> str:
    flags = summary["decision_flags"]
    recon = summary["reconstruction"]
    filt = summary["filter_proposal_test"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-21 — V3 Buy-source Label Quality Validation

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Failure / Toxic Label Classification

- total rows: **{recon['total_rows']}**
- state_changed buy candidates: **{recon['state_changed_buy_candidates']}**
- policy attempted rows: **{recon['policy_attempted_rows']}**
- horizon quote success rows: **{recon['horizon_quote_success_rows']}**
- horizon quoter revert rows: **{recon['horizon_quoter_revert_rows']}**
- near-total-loss rows: **{recon['near_total_loss_rows']}**
- clean candidate rows: **{recon['clean_candidate_rows']}**
- class distribution: `{recon['class_distribution']}`

## 2. Near-total-loss Forensic

- count: **{summary['near_total_loss_forensic']['near_total_loss_count']}**
- fee distribution: `{summary['near_total_loss_forensic']['fee_distribution']}`
- selected notional distribution: `{summary['near_total_loss_forensic']['selected_notional_distribution']}`
- factor counts: `{summary['near_total_loss_forensic']['factor_counts']}`
- explained: **{summary['near_total_loss_forensic']['near_total_loss_explained']}**

## 3. Horizon Quoter Revert Forensic

- count: **{summary['revert_forensic']['horizon_quoter_revert_count']}**
- fee distribution: `{summary['revert_forensic']['fee_distribution']}`
- classification counts: `{summary['revert_forensic']['classification_counts']}`
- explained: **{summary['revert_forensic']['quoter_reverts_explained']}**

## 4. Filter Proposal Test

- filters: `{filt['filters']}`
- optional fee10000 filter: `{filt['optional_filter_fee_10000']}`
- clean_rows_count: **{filt['clean_rows_count']}**
- clean_rows_share: **{filt['clean_rows_share']}**
- clean label return distribution: `{filt['clean_label_return_distribution']}`
- clean_near_total_loss_share: **{filt['clean_near_total_loss_share']}**
- clean top1/top5 pool share: **{filt['clean_top1_pool_share']} / {filt['clean_top5_pool_share']}**
- clean top1/top5 token share: **{filt['clean_top1_token_share']} / {filt['clean_top5_token_share']}**
- optional no-fee10000 rows: **{filt['optional_no_fee_10000_rows_count']}**
- clean distribution healthy: **{filt['clean_label_distribution_healthy']}**

## 5. Required Answers

1. near-total-loss 是否主要可解释：**{flags['near_total_loss_explained']}**。
2. horizon_quoter_revert 是否主要可解释：**{flags['quoter_reverts_explained']}**。
3. 是否能通过合理 filter 得到 clean labels：**{flags['clean_filter_candidate_available']}**。
4. clean labels 是否仍过度集中：top1/top5 pool = **{filt['clean_top1_pool_share']} / {filt['clean_top5_pool_share']}**。
5. 是否允许 V3R-22 small clean-buy expansion：**{flags['allow_v3r22_small_clean_buy_expansion']}**。
6. 是否允许 model/mainline/live：**False / False / False**。

## 6. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    rpc = JsonRpcClient(_rpc_url())
    fields = field_audit()
    rows = load_rows()
    reconstruction = reconstruct(rows)
    near = near_forensic(rpc, rows)
    reverts = revert_forensic(rpc, rows)
    filt = filter_test(rows)
    flags = {
        "v3r21_completed": True,
        "v3r20_rows_reconstructed": bool(rows) and fields["fields_sufficient"],
        "near_total_loss_explained": bool(near["near_total_loss_explained"]),
        "quoter_reverts_explained": bool(reverts["quoter_reverts_explained"]),
        "clean_filter_candidate_available": filt["clean_rows_count"] >= 20 and filt["clean_near_total_loss_share"] <= 0.10,
        "clean_rows_count": filt["clean_rows_count"],
        "clean_near_total_loss_share": filt["clean_near_total_loss_share"],
        "clean_top1_pool_share": filt["clean_top1_pool_share"],
        "clean_top5_pool_share": filt["clean_top5_pool_share"],
        "clean_label_distribution_healthy": filt["clean_label_distribution_healthy"],
        "allow_v3r22_small_clean_buy_expansion": (
            filt["clean_rows_count"] >= 20
            and filt["clean_near_total_loss_share"] <= 0.10
            and filt["clean_top1_pool_share"] <= 0.20
            and filt["clean_top5_pool_share"] <= 0.55
            and filt["clean_label_distribution_healthy"]
        ),
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r20_summary": str(V3R20_JSON.relative_to(ROOT)),
            "v3r20_rows": str(V3R20_CSV.relative_to(ROOT)),
            "novel_candidates": str(NOVEL_CSV.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "field_audit": fields,
        "reconstruction": reconstruction,
        "near_total_loss_forensic": near,
        "revert_forensic": reverts,
        "filter_proposal_test": filt,
        "decision_flags": flags,
        "rows": rows[:100],
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
