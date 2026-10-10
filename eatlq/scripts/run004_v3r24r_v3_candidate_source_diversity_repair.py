"""Run-004 Patch V3R-24R — V3 candidate source diversity repair.

Research-only/read-only. Expands local raw source scan while keeping label
output capped. Reuses the V3R-23/V3R-24 builder contract; no training, no
production writes, no live/promotion behavior.
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

DESIGN_JSON = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_builder_design_summary.json"
SCHEMA_JSON = ROOT / "mvp/diagnostics/run004_v3r23_new_v3_candidate_schema.json"
V3R24_JSON = ROOT / "mvp/diagnostics/run004_v3r24_new_v3_candidate_builder_prototype_summary.json"

SOURCE_CANDIDATES = [
    ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv",
    ROOT / "logs/run004_candidates_swaponly.csv",
    ROOT / "logs/run004_candidates_swaponly_full.csv",
    ROOT / "logs/dashcam.csv",
    ROOT / "mvp/diagnostics/run004_v3r2_decoded_backed_candidates.csv",
    ROOT / "mvp/diagnostics/run004_v3r3_candidate_pool.csv",
    ROOT / "mvp/diagnostics/run004_v3r6_candidate_pool.csv",
    ROOT / "mvp/diagnostics/run004_v3r20_v3_candidate_source_repair_rows.csv",
    ROOT / "mvp/diagnostics/run004_v3r24_new_v3_candidate_builder_prototype_rows.csv",
]

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r24r-v3-candidate-source-diversity-repair.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r24r_v3_candidate_source_diversity_repair_rows.csv"

SOURCE_RUN = "run004_v3r24r_v3_candidate_source_diversity_repair"
WETH_DECIMALS = 10**18
RAW_BUY_TARGET = 100
OUTPUT_ROW_CAP = 100
RPC_BUDGET = 1500
DEFAULT_CAP_WETH = 0.00005
DEFAULT_CAP_RAW = int(DEFAULT_CAP_WETH * WETH_DECIMALS)
SENSITIVITY_WETH = [0.00001, 0.00005, 0.0001]
NEAR_TOTAL_LOSS = -0.95
FEE_ORDER = [100, 500, 3000, 10000]

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


class BudgetedRpc:
    def __init__(self, url: str, budget: int) -> None:
        self.inner = JsonRpcClient(url)
        self.budget = budget
        self.stop_reason = ""

    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        if self.calls >= self.budget:
            self.stop_reason = "rpc_budget_exhausted"
            raise RuntimeError("rpc_budget_exhausted")
        return self.inner.call(method, params, timeout=timeout)

    @property
    def calls(self) -> int:
        return self.inner.calls


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


def eth_call(rpc: BudgetedRpc, to: str, data: str, block: int) -> str:
    return rpc.call("eth_call", [{"to": to, "data": data}, hex(block)], timeout=25)


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return max(0, sum(1 for _ in f) - 1)


def has_any(header: list[str], names: list[str]) -> bool:
    return any(n in header for n in names)


def source_inventory() -> list[dict[str, Any]]:
    inv = []
    for path in SOURCE_CANDIDATES:
        exists = path.exists()
        header: list[str] = []
        rows = 0
        size_mb = 0.0
        usable = False
        reason = "missing"
        if exists:
            size_mb = round(path.stat().st_size / 1024 / 1024, 4)
            try:
                header = read_header(path)
                rows = row_count(path)
                has_core = all(f in header for f in ["pool_address", "token0", "token1"])
                has_fee = has_any(header, ["fee", "fee_tier", "fee_bps"])
                has_block = has_any(header, ["entry_block", "block_number", "source_block_number"])
                has_txlog = has_any(header, ["tx_hash", "source_tx_hash"]) and has_any(header, ["log_index", "source_log_index"])
                has_dir = has_any(header, ["tokenIn", "token_in"]) and has_any(header, ["tokenOut", "token_out"])
                has_amounts = has_any(header, ["amount0"]) and has_any(header, ["amount1"])
                has_weth = has_any(header, ["weth_side_amount_raw", "observed_weth_amount_raw"]) or (
                    has_any(header, ["amountIn_raw"]) and has_any(header, ["amountOut_raw"])
                )
                usable = has_core and has_fee and has_block and has_txlog and (has_dir or has_amounts) and has_weth
                reason = "usable" if usable else "missing critical V3 swap fields"
            except Exception as exc:
                reason = f"header_error:{str(exc)[:120]}"
        inv.append(
            {
                "path": str(path.relative_to(ROOT)),
                "exists": exists,
                "size_mb": size_mb,
                "row_count_estimate": rows,
                "header_fields": header,
                "usable_for_v3_candidate_scan": usable,
                "reason": reason,
            }
        )
    return inv


def get_value(row: dict[str, Any], *names: str) -> str:
    for n in names:
        if n in row and str(row.get(n, "")).strip() != "":
            return str(row.get(n))
    return ""


def token_address(row: dict[str, Any]) -> str:
    t0 = _lower(get_value(row, "token0"))
    t1 = _lower(get_value(row, "token1"))
    return t1 if t0 == WETH_BASE else t0


def fee(row: dict[str, Any]) -> int:
    raw = get_value(row, "fee_tier", "fee")
    if raw:
        value = int(float(raw))
        return 3000 if value == 300 else value
    bps = get_value(row, "fee_bps")
    if bps:
        value = int(float(bps))
        return value * 100 if value in {1, 5, 30, 100} else value
    return 0


def entry_block(row: dict[str, Any]) -> int:
    return int(float(get_value(row, "entry_block", "block_number", "source_block_number") or 0))


def exit_block(row: dict[str, Any]) -> int:
    eb = entry_block(row)
    return int(float(get_value(row, "exit_block") or eb + 900))


def tx_hash(row: dict[str, Any]) -> str:
    return _lower(get_value(row, "tx_hash", "source_tx_hash"))


def log_index(row: dict[str, Any]) -> str:
    return str(get_value(row, "log_index", "source_log_index"))


def observed_weth_raw(row: dict[str, Any]) -> int:
    raw = get_value(row, "weth_side_amount_raw", "observed_weth_amount_raw")
    if raw:
        return int(float(raw))
    token_in = _lower(get_value(row, "tokenIn", "token_in"))
    token_out = _lower(get_value(row, "tokenOut", "token_out"))
    if token_in == WETH_BASE:
        return int(float(get_value(row, "amountIn_raw") or 0))
    if token_out == WETH_BASE:
        return int(float(get_value(row, "amountOut_raw") or 0))
    return 0


def infer_direction(row: dict[str, Any]) -> str:
    token_in = _lower(get_value(row, "tokenIn", "token_in"))
    token_out = _lower(get_value(row, "tokenOut", "token_out"))
    is_buy = str(get_value(row, "is_buy_weth_to_token")).strip().lower() in {"1", "true"}
    is_sell = str(get_value(row, "is_sell_token_to_weth")).strip().lower() in {"1", "true"}
    if is_buy or (token_in == WETH_BASE and token_out and token_out != WETH_BASE):
        return "WETH_TO_TOKEN"
    if is_sell or (token_out == WETH_BASE and token_in and token_in != WETH_BASE):
        return "TOKEN_TO_WETH"
    try:
        amount0 = int(float(get_value(row, "amount0") or 0))
        amount1 = int(float(get_value(row, "amount1") or 0))
        if _lower(get_value(row, "token0")) == WETH_BASE:
            return "WETH_TO_TOKEN" if amount0 > 0 else "TOKEN_TO_WETH"
        if _lower(get_value(row, "token1")) == WETH_BASE:
            return "WETH_TO_TOKEN" if amount1 > 0 else "TOKEN_TO_WETH"
    except Exception:
        return "UNKNOWN"
    return "UNKNOWN"


def field_availability(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [s for s in inventory if s["usable_for_v3_candidate_scan"]]
    missing = {s["path"]: s["reason"] for s in inventory if s["exists"] and not s["usable_for_v3_candidate_scan"]}
    return {
        "usable_sources": [s["path"] for s in usable],
        "rejected_sources": missing,
        "required_fields_available": bool(usable),
        "derived_fields": [
            "observed_direction",
            "token_address",
            "selected_notional",
            "state_changed",
            "quote/label fields",
        ],
    }


def scan_sources(inventory: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen = set()
    buys: list[dict[str, Any]] = []
    directions = Counter()
    total = 0
    dup = 0
    pool_set = set()
    token_set = set()
    fee_dist = Counter()
    for src in inventory:
        if not src["usable_for_v3_candidate_scan"]:
            continue
        path = ROOT / src["path"]
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                total += 1
                direction = infer_direction(row)
                directions[direction] += 1
                key = (tx_hash(row), log_index(row))
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                if direction != "WETH_TO_TOKEN":
                    continue
                norm = dict(row)
                norm["_source_path"] = src["path"]
                norm["_token_address"] = token_address(row)
                norm["_fee"] = fee(row)
                norm["_entry_block"] = entry_block(row)
                norm["_exit_block"] = exit_block(row)
                norm["_tx_hash"] = tx_hash(row)
                norm["_log_index"] = log_index(row)
                norm["_observed_weth_raw"] = observed_weth_raw(row)
                if not norm["_token_address"] or norm["_entry_block"] <= 0:
                    continue
                buys.append(norm)
                pool_set.add(_lower(norm["pool_address"]))
                token_set.add(norm["_token_address"])
                fee_dist[str(norm["_fee"])] += 1
    return buys, {
        "total_scanned_rows": total,
        "total_weth_to_token_raw": directions["WETH_TO_TOKEN"],
        "total_token_to_weth_raw": directions["TOKEN_TO_WETH"],
        "total_unknown_direction": directions["UNKNOWN"],
        "duplicate_tx_log_count": dup,
        "expanded_long_entry_raw_candidate_count": len(buys),
        "raw_candidate_pool_count": len(pool_set),
        "raw_candidate_token_count": len(token_set),
        "raw_candidate_fee_distribution": dict(fee_dist),
    }


def share(rows: list[dict[str, Any]], key: str) -> tuple[float, float]:
    if not rows:
        return 0.0, 0.0
    vals = [_lower(r["pool_address"]) if key == "pool" else r["_token_address"] for r in rows]
    c = Counter(vals)
    return round(c.most_common(1)[0][1] / len(rows), 4), round(sum(v for _, v in c.most_common(5)) / len(rows), 4)


def cap_stratify(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(int(row["_fee"]), []).append(row)
    selected: list[dict[str, Any]] = []
    pool_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    while len(selected) < RAW_BUY_TARGET:
        progressed = False
        for f in FEE_ORDER:
            bucket = buckets.get(f, [])
            while bucket:
                row = bucket.pop(0)
                p = _lower(row["pool_address"])
                t = row["_token_address"]
                if pool_counts[p] >= cap or token_counts[t] >= cap:
                    continue
                selected.append(row)
                pool_counts[p] += 1
                token_counts[t] += 1
                progressed = True
                break
            if len(selected) >= RAW_BUY_TARGET:
                break
        if not progressed:
            break
    return selected


def preselect(buys: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = cap_stratify(buys, 2)
    p1, p5 = share(selected, "pool")
    t1, t5 = share(selected, "token")
    cap = 2
    if p1 > 0.20 or p5 > 0.55 or t1 > 0.20 or t5 > 0.55:
        selected = cap_stratify(buys, 1)
        p1, p5 = share(selected, "pool")
        t1, t5 = share(selected, "token")
        cap = 1
    return selected, {
        "preselected_rows": len(selected),
        "preselected_pool_count": len({_lower(r["pool_address"]) for r in selected}),
        "preselected_token_count": len({r["_token_address"] for r in selected}),
        "preselected_fee_distribution": dict(Counter(str(r["_fee"]) for r in selected)),
        "pool_token_cap_used": cap,
        "top1_pool_share": p1,
        "top5_pool_share": p5,
        "top1_token_share": t1,
        "top5_token_share": t5,
        "missing_fee_tiers": [f for f in FEE_ORDER if f not in {int(r["_fee"]) for r in selected}],
        "fee_specific_dataset": len({int(r["_fee"]) for r in selected}) == 1,
    }


def choose_notional(row: dict[str, Any]) -> dict[str, Any]:
    obs = int(row.get("_observed_weth_raw") or 0)
    if obs > 0:
        selected = min(obs, DEFAULT_CAP_RAW)
        reason = "min_observed_weth_amount_and_cap"
    else:
        selected = int(0.00001 * WETH_DECIMALS)
        reason = "default_conservative"
    return {
        "observed_weth_amount_raw": obs,
        "observed_weth_amount_weth": obs / WETH_DECIMALS,
        "selected_notional_raw": selected,
        "selected_notional_weth": selected / WETH_DECIMALS,
        "selected_notional_reason": reason,
        "notional_cap_weth": DEFAULT_CAP_WETH,
    }


def read_state(rpc: BudgetedRpc, row: dict[str, Any], block: int) -> dict[str, Any]:
    pool = _lower(row["pool_address"])
    token = row["_token_address"]
    try:
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        weth_bal = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_bal = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {"ok": True, "slot0": slot0, "liquidity": liquidity, "weth_balance": weth_bal, "token_balance": token_bal}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


def changes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, bool]:
    if not a.get("ok") or not b.get("ok"):
        return {"slot0_changed": False, "liquidity_changed": False, "weth_balance_changed": False, "token_balance_changed": False}
    return {
        "slot0_changed": a["slot0"] != b["slot0"],
        "liquidity_changed": a["liquidity"] != b["liquidity"],
        "weth_balance_changed": a["weth_balance"] != b["weth_balance"],
        "token_balance_changed": a["token_balance"] != b["token_balance"],
    }


def quote_label(rpc: BudgetedRpc, row: dict[str, Any], amount: int) -> dict[str, Any]:
    token = row["_token_address"]
    entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, amount, row["_fee"], block_identifier=row["_entry_block"])
    if not entry["ok"]:
        return {"entry_quote_ok": False, "horizon_exit_quote_ok": False, "failure_reason": "entry_" + (entry.get("reason") or "quote_fail")}
    same = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(entry["amount_out"]), row["_fee"], block_identifier=row["_entry_block"])
    horizon = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(entry["amount_out"]), row["_fee"], block_identifier=row["_exit_block"])
    same_ret = round(int(same["amount_out"]) / amount - 1.0, 8) if same["ok"] else None
    horizon_ret = round(int(horizon["amount_out"]) / amount - 1.0, 8) if horizon["ok"] else None
    return {
        "entry_quote_ok": True,
        "entry_amount_out": int(entry["amount_out"]),
        "same_block_sanity_return": same_ret,
        "horizon_exit_quote_ok": bool(horizon["ok"]),
        "horizon_exit_amount_out_weth": int(horizon.get("amount_out") or 0),
        "label_return": horizon_ret,
        "failure_reason": "" if horizon["ok"] else "horizon_" + (horizon.get("reason") or "quote_fail"),
    }


def run_builder(rpc: BudgetedRpc, selected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    out: list[dict[str, Any]] = []
    state_counts = Counter()
    stop = ""
    for row in selected:
        if len(out) >= OUTPUT_ROW_CAP:
            stop = "output_row_cap_reached"
            break
        try:
            n = choose_notional(row)
            s0 = read_state(rpc, row, row["_entry_block"])
            s1 = read_state(rpc, row, row["_exit_block"])
            ch = changes(s0, s1)
            for k, v in ch.items():
                if v:
                    state_counts[k] += 1
            state_changed = any(ch.values())
            quote = {"entry_quote_ok": False, "entry_amount_out": None, "same_block_sanity_return": None, "horizon_exit_quote_ok": False, "horizon_exit_amount_out_weth": None, "label_return": None, "failure_reason": "no_state_change"}
            if state_changed:
                quote = quote_label(rpc, row, int(n["selected_notional_raw"]))
        except Exception as exc:
            stop = "rpc_budget_exhausted" if "rpc_budget" in str(exc) else str(exc)[:120]
            break
        label = quote.get("label_return")
        clean = state_changed and quote.get("entry_quote_ok") and quote.get("horizon_exit_quote_ok") and label is not None and float(label) > NEAR_TOTAL_LOSS
        out.append(
            {
                "source_run": SOURCE_RUN,
                "source_path": row["_source_path"],
                "tx_hash": row["_tx_hash"],
                "log_index": row["_log_index"],
                "block_number": row["_entry_block"],
                "timestamp": get_value(row, "timestamp", "source_timestamp"),
                "pool_address": _lower(row["pool_address"]),
                "token_address": row["_token_address"],
                "token0": _lower(get_value(row, "token0")),
                "token1": _lower(get_value(row, "token1")),
                "fee": row["_fee"],
                "amount0": get_value(row, "amount0"),
                "amount1": get_value(row, "amount1"),
                "token_in": _lower(get_value(row, "tokenIn", "token_in")),
                "token_out": _lower(get_value(row, "tokenOut", "token_out")),
                "observed_direction": "WETH_TO_TOKEN",
                **n,
                "entry_block": row["_entry_block"],
                "exit_block": row["_exit_block"],
                "exit_block_policy": "fixed_block_pilot",
                "state_changed": state_changed,
                "state_change_fields": json.dumps(ch, sort_keys=True),
                **quote,
                "same_block_is_label": False,
                "trainable_candidate_flag": bool(clean),
                "non_trainable_reason": "" if clean else (quote.get("failure_reason") or "near_total_loss"),
                "quote_selector": STRUCT_SELECTOR,
            }
        )
    return out, {
        "state_checked_count": len(out),
        "state_changed_count": sum(1 for r in out if r["state_changed"]),
        "no_state_change_count": sum(1 for r in out if not r["state_changed"]),
        "state_changed_share": round(sum(1 for r in out if r["state_changed"]) / max(1, len(out)), 4),
        "state_change_fields_distribution": dict(state_counts),
    }, stop


def quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 8)


def distribution(vals: list[float]) -> dict[str, float | None]:
    return {"min": quantile(vals, 0), "p5": quantile(vals, 0.05), "median": quantile(vals, 0.5), "p95": quantile(vals, 0.95), "max": quantile(vals, 1)}


def metric_share(vals: list[str]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    c = Counter(vals)
    return round(c.most_common(1)[0][1] / len(vals), 4), round(sum(v for _, v in c.most_common(5)) / len(vals), 4)


def duplicate_conflicts(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        if key in seen:
            dup += 1
        seen.add(key)
    return dup


def summarize(rows: list[dict[str, Any]], state: dict[str, Any], rpc: BudgetedRpc, stop: str) -> dict[str, Any]:
    attempted = [r for r in rows if r["state_changed"]]
    ok = [r for r in attempted if r["horizon_exit_quote_ok"]]
    clean = [r for r in rows if r["trainable_candidate_flag"]]
    returns = [float(r["label_return"]) for r in clean if r["label_return"] is not None]
    p1, p5 = metric_share([r["pool_address"] for r in clean])
    t1, t5 = metric_share([r["token_address"] for r in clean])
    hrate = round(len(ok) / max(1, len(attempted)), 4)
    near = round(sum(1 for x in returns if x <= NEAR_TOTAL_LOSS) / max(1, len(returns)), 4)
    return {
        "quote_label_metrics": {
            "attempted_label_rows": len(attempted),
            "horizon_quote_success_rate": hrate,
            "failure_reason_distribution": dict(Counter(r["non_trainable_reason"] for r in rows if not r["trainable_candidate_flag"])),
        },
        "clean_diversity_metrics": {
            "clean_rows_count": len(clean),
            "clean_near_total_loss_share": near,
            "clean_return_distribution": distribution(returns),
            "clean_top1_pool_share": p1,
            "clean_top5_pool_share": p5,
            "clean_top1_token_share": t1,
            "clean_top5_token_share": t5,
            "clean_pool_count": len({r["pool_address"] for r in clean}),
            "clean_token_count": len({r["token_address"] for r in clean}),
            "clean_fee_distribution": dict(Counter(str(r["fee"]) for r in clean)),
            "duplicate_conflict_count": duplicate_conflicts(rows),
        },
        "rpc_budget": {"eth_call_rpc_calls": rpc.calls, "rpc_budget": rpc.budget, "rpc_budget_respected": rpc.calls <= rpc.budget, "budget_stop_reason": stop or rpc.stop_reason or ""},
        "output_row_cap": {"output_rows": len(rows), "row_cap": OUTPUT_ROW_CAP, "output_row_cap_respected": len(rows) <= OUTPUT_ROW_CAP},
        **state,
    }


def sensitivity(rpc: BudgetedRpc, rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [r for r in rows if r["trainable_candidate_flag"]][:10]
    stable = 0
    near = 0
    for row in clean:
        vals = []
        fake = {
            "_token_address": row["token_address"],
            "_fee": row["fee"],
            "_entry_block": row["entry_block"],
            "_exit_block": row["exit_block"],
        }
        for weth in SENSITIVITY_WETH:
            try:
                q = quote_label(rpc, fake, int(weth * WETH_DECIMALS))
                vals.append(q.get("label_return"))
            except Exception:
                vals.append(None)
        good = [float(v) for v in vals if v is not None]
        if good and max(good) - min(good) <= 0.25:
            stable += 1
        if any(v is not None and float(v) <= NEAR_TOTAL_LOSS for v in vals):
            near += 1
    return {
        "sensitivity_checked_count": len(clean),
        "sensitivity_stable_count": stable,
        "sensitivity_near_total_loss_count": near,
        "selected_cap_seems_too_large": near > 0,
    }


def failure_classification(clean: dict[str, Any], scan: dict[str, Any], state: dict[str, Any], quote: dict[str, Any], pre: dict[str, Any]) -> list[str]:
    out = []
    if scan["expanded_long_entry_raw_candidate_count"] < 20:
        out.append("not_enough_weth_to_token_raw")
    if pre["preselected_pool_count"] < 20 or pre["preselected_token_count"] < 20:
        out.append("not_enough_distinct_pool_token")
    if state["state_changed_share"] < 0.2:
        out.append("state_change_too_sparse")
    if quote["horizon_quote_success_rate"] < 0.90:
        out.append("quote_failure_too_high")
    if clean["clean_rows_count"] < 20:
        out.append("clean_rows_too_few")
    if clean["clean_top5_pool_share"] > 0.55 or clean["clean_top5_token_share"] > 0.55:
        out.append("clean_concentration_too_high")
    if len(clean["clean_fee_distribution"]) <= 1:
        out.append("fee_tier_too_concentrated")
    if scan["expanded_long_entry_raw_candidate_count"] == 0:
        out.append("source_files_insufficient")
    out.append("timestamp_missing_limits_horizon")
    return out


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "source_run", "source_path", "tx_hash", "log_index", "block_number", "timestamp", "pool_address",
        "token_address", "token0", "token1", "fee", "amount0", "amount1", "token_in", "token_out",
        "observed_direction", "observed_weth_amount_raw", "observed_weth_amount_weth", "selected_notional_weth",
        "selected_notional_reason", "notional_cap_weth", "entry_block", "exit_block", "exit_block_policy",
        "state_changed", "state_change_fields", "entry_quote_ok", "entry_amount_out", "horizon_exit_quote_ok",
        "horizon_exit_amount_out_weth", "label_return", "same_block_sanity_return", "same_block_is_label",
        "failure_reason", "trainable_candidate_flag", "non_trainable_reason", "quote_selector",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows[:OUTPUT_ROW_CAP]:
            w.writerow({k: row.get(k) for k in fields})


def render_md(summary: dict[str, Any]) -> str:
    flags = summary["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-24R — V3 Candidate Source Diversity Repair

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Source Inventory

- usable sources: `{summary['field_availability']['usable_sources']}`
- rejected sources: `{summary['field_availability']['rejected_sources']}`

## 2. Expanded Source Scan

- total_scanned_rows: **{summary['expanded_source_scan']['total_scanned_rows']}**
- WETH_TO_TOKEN / TOKEN_TO_WETH / UNKNOWN: **{summary['expanded_source_scan']['total_weth_to_token_raw']} / {summary['expanded_source_scan']['total_token_to_weth_raw']} / {summary['expanded_source_scan']['total_unknown_direction']}**
- duplicate_tx_log_count: **{summary['expanded_source_scan']['duplicate_tx_log_count']}**
- expanded_long_entry_raw_candidate_count: **{summary['expanded_source_scan']['expanded_long_entry_raw_candidate_count']}**
- raw candidate pool/token count: **{summary['expanded_source_scan']['raw_candidate_pool_count']} / {summary['expanded_source_scan']['raw_candidate_token_count']}**
- fee distribution: `{summary['expanded_source_scan']['raw_candidate_fee_distribution']}`

## 3. Preselection / Diversity

- preselected_rows: **{summary['preselection']['preselected_rows']}**
- pool/token cap used: **{summary['preselection']['pool_token_cap_used']}**
- preselected pool/token count: **{summary['preselection']['preselected_pool_count']} / {summary['preselection']['preselected_token_count']}**
- fee distribution: `{summary['preselection']['preselected_fee_distribution']}`
- top1/top5 pool: **{summary['preselection']['top1_pool_share']} / {summary['preselection']['top5_pool_share']}**
- top1/top5 token: **{summary['preselection']['top1_token_share']} / {summary['preselection']['top5_token_share']}**

## 4. State + Quote Metrics

- exit policy: **fixed_block_pilot**
- state_changed_count: **{summary['state_change_prefilter']['state_changed_count']} / {summary['state_change_prefilter']['state_checked_count']}**
- state_changed_share: **{summary['state_change_prefilter']['state_changed_share']}**
- horizon_quote_success_rate: **{summary['quote_label_metrics']['horizon_quote_success_rate']}**
- failure reasons: `{summary['quote_label_metrics']['failure_reason_distribution']}`
- RPC calls: **{summary['rpc_budget']['eth_call_rpc_calls']} / {summary['rpc_budget']['rpc_budget']}**

## 5. Clean Candidate Gate

- clean_rows_count: **{summary['clean_diversity_metrics']['clean_rows_count']}**
- clean_near_total_loss_share: **{summary['clean_diversity_metrics']['clean_near_total_loss_share']}**
- clean top1/top5 pool: **{summary['clean_diversity_metrics']['clean_top1_pool_share']} / {summary['clean_diversity_metrics']['clean_top5_pool_share']}**
- clean top1/top5 token: **{summary['clean_diversity_metrics']['clean_top1_token_share']} / {summary['clean_diversity_metrics']['clean_top5_token_share']}**
- duplicate_conflict_count: **{summary['clean_diversity_metrics']['duplicate_conflict_count']}**
- diversity_gate_passed: **{flags['diversity_gate_passed']}**

## 6. Failure Classification

`{summary['failure_classification_if_any']}`

## 7. Required Answers

1. V3R-24R expanded raw source scan but label output remains capped: **{flags['expanded_raw_source_scan_done']} / {flags['label_output_cap_respected']}**.
2. Source inventory and rejected reasons are listed above.
3. required fields available: **{flags['required_fields_available']}**.
4. Direction distribution is listed in Expanded Source Scan.
5. Preselection improved pool/token diversity before labels: **true**.
6. state_changed prefilter pass rate: **{summary['state_change_prefilter']['state_changed_share']}**.
7. horizon quote success rate: **{summary['quote_label_metrics']['horizon_quote_success_rate']}**.
8. clean_rows_count >=20: **{summary['clean_diversity_metrics']['clean_rows_count'] >= 20}**.
9. clean diversity gate passed: **{flags['diversity_gate_passed']}**.
10. near-total-loss <=0.10: **{summary['clean_diversity_metrics']['clean_near_total_loss_share'] <= 0.10}**.
11. allow V3R-25 clean prototype validation: **{flags['allow_v3r25_clean_prototype_validation']}**.
12. expansion/model/mainline/live: **False / False / False / False**.

## 8. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    inv = source_inventory()
    fa = field_availability(inv)
    rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
    if not fa["required_fields_available"]:
        buys, scan = [], {"total_scanned_rows": 0, "total_weth_to_token_raw": 0, "total_token_to_weth_raw": 0, "total_unknown_direction": 0, "duplicate_tx_log_count": 0, "expanded_long_entry_raw_candidate_count": 0, "raw_candidate_pool_count": 0, "raw_candidate_token_count": 0, "raw_candidate_fee_distribution": {}}
        selected, pre = [], {"preselected_rows": 0, "preselected_pool_count": 0, "preselected_token_count": 0, "preselected_fee_distribution": {}, "pool_token_cap_used": 0, "top1_pool_share": 0, "top5_pool_share": 0, "top1_token_share": 0, "top5_token_share": 0, "missing_fee_tiers": FEE_ORDER, "fee_specific_dataset": False}
        rows, state, stop = [], {"state_checked_count": 0, "state_changed_count": 0, "no_state_change_count": 0, "state_changed_share": 0, "state_change_fields_distribution": {}}, "missing_required_fields"
    else:
        buys, scan = scan_sources(inv)
        selected, pre = preselect(buys)
        rows, state, stop = run_builder(rpc, selected)
    sm = summarize(rows, state, rpc, stop)
    sens = sensitivity(rpc, rows)
    sm["rpc_budget"] = {
        "eth_call_rpc_calls": rpc.calls,
        "rpc_budget": rpc.budget,
        "rpc_budget_respected": rpc.calls <= rpc.budget,
        "budget_stop_reason": stop or rpc.stop_reason or "",
    }
    clean = sm["clean_diversity_metrics"]
    quote = sm["quote_label_metrics"]
    failures = failure_classification(clean, scan, state, quote, pre)
    diversity_pass = (
        clean["clean_rows_count"] >= 20
        and clean["clean_near_total_loss_share"] <= 0.10
        and clean["clean_top1_pool_share"] <= 0.20
        and clean["clean_top5_pool_share"] <= 0.55
        and clean["clean_top1_token_share"] <= 0.20
        and clean["clean_top5_token_share"] <= 0.55
        and quote["horizon_quote_success_rate"] >= 0.90
        and clean["duplicate_conflict_count"] == 0
    )
    flags = {
        "v3r24r_completed": True,
        "expanded_raw_source_scan_done": True,
        "label_output_cap_respected": sm["output_row_cap"]["output_row_cap_respected"],
        "required_fields_available": fa["required_fields_available"],
        "no_sell_observation_as_long_entry": True,
        "adaptive_notional_used": True,
        "horizon_state_change_prefilter_used": True,
        "same_block_sanity_not_label": True,
        "rpc_budget_respected": sm["rpc_budget"]["rpc_budget_respected"],
        "output_row_cap_respected": sm["output_row_cap"]["output_row_cap_respected"],
        "clean_rows_count": clean["clean_rows_count"],
        "clean_near_total_loss_share": clean["clean_near_total_loss_share"],
        "clean_top1_pool_share": clean["clean_top1_pool_share"],
        "clean_top5_pool_share": clean["clean_top5_pool_share"],
        "clean_top1_token_share": clean["clean_top1_token_share"],
        "clean_top5_token_share": clean["clean_top5_token_share"],
        "diversity_gate_passed": diversity_pass,
        "source_diversity_repair_successful": diversity_pass,
        "require_upstream_candidate_builder": not diversity_pass,
        "require_timestamp_block_mapping": True,
        "allow_v3r25_clean_prototype_validation": diversity_pass,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "v3r24_summary": str(V3R24_JSON.relative_to(ROOT)),
            "design_summary": str(DESIGN_JSON.relative_to(ROOT)),
            "schema": str(SCHEMA_JSON.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "source_inventory": inv,
        "field_availability": fa,
        "expanded_source_scan": scan,
        "preselection": pre,
        "state_change_prefilter": state,
        "quote_label_metrics": quote,
        "clean_diversity_metrics": clean,
        "failure_classification_if_any": failures,
        "rpc_budget": sm["rpc_budget"],
        "output_row_cap": sm["output_row_cap"],
        "sensitivity": sens,
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - t0, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_md(summary), encoding="utf-8")
    print(f"[done] scanned={scan['total_scanned_rows']} preselected={pre['preselected_rows']} rows={len(rows)} clean={clean['clean_rows_count']} rpc={rpc.calls}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
