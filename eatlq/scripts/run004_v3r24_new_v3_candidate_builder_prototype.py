"""Run-004 Patch V3R-24 — new V3 candidate builder bounded prototype.

Research-only/read-only. Executes the V3R-23 candidate-builder contract on a
bounded source slice. No training, no production writes, no runtime changes.
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
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"

OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r24-new-v3-candidate-builder-prototype.md"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r24_new_v3_candidate_builder_prototype_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r24_new_v3_candidate_builder_prototype_rows.csv"

SOURCE_RUN = "run004_v3r24_new_v3_candidate_builder_prototype"
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


def preflight() -> dict[str, Any]:
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    design = json.loads(DESIGN_JSON.read_text(encoding="utf-8"))
    header = read_header(NOVEL_CSV)
    source_fields = set(header)
    required_critical = [
        "tx_hash",
        "log_index",
        "pool_address",
        "token0",
        "token1",
        "amount0",
        "amount1",
        "tokenIn",
        "tokenOut",
        "weth_side_amount_raw",
        "entry_block",
        "exit_block",
    ]
    fee_available = "fee_tier" in source_fields or "fee" in source_fields or "fee_bps" in source_fields
    block_available = "block_number" in source_fields or "entry_block" in source_fields
    derived = [
        "source_run",
        "token_address",
        "observed_direction",
        "observed_weth_amount_weth",
        "selected_notional_weth",
        "selected_notional_reason",
        "notional_cap_weth",
        "exit_block_policy",
        "state_changed",
        "state_change_fields",
        "entry_quote_ok",
        "entry_amount_out",
        "horizon_exit_quote_ok",
        "horizon_exit_amount_out_weth",
        "label_return",
        "same_block_sanity_return",
        "same_block_is_label",
        "failure_reason",
        "trainable_candidate_flag",
        "non_trainable_reason",
    ]
    missing = [f for f in required_critical if f not in source_fields]
    if not fee_available:
        missing.append("fee/fee_tier/fee_bps")
    if not block_available:
        missing.append("block_number/entry_block")
    unknown = ["timestamp"] if "timestamp" not in source_fields else []
    return {
        "schema": str(SCHEMA_JSON.relative_to(ROOT)),
        "design": str(DESIGN_JSON.relative_to(ROOT)),
        "candidate_source": str(NOVEL_CSV.relative_to(ROOT)),
        "present_fields": [f for f in header],
        "missing_fields": missing,
        "derived_fields": derived,
        "unknown_fields": unknown,
        "required_fields_available": not missing,
        "allow_v3r24_from_design": bool(design.get("decision_flags", {}).get("allow_v3r24_bounded_prototype")),
        "schema_field_count": len(schema.get("fields", [])),
    }


def load_candidates() -> list[dict[str, Any]]:
    return list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))


def token_address(row: dict[str, Any]) -> str:
    t0 = _lower(row.get("token0"))
    t1 = _lower(row.get("token1"))
    return t1 if t0 == WETH_BASE else t0


def fee(row: dict[str, Any]) -> int:
    return int(row.get("fee_tier") or row.get("fee") or row.get("fee_bps") or 0)


def observed_weth_raw(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("weth_side_amount_raw") or 0))
    except Exception:
        return 0


def infer_direction(row: dict[str, Any]) -> str:
    token_in = _lower(row.get("tokenIn"))
    token_out = _lower(row.get("tokenOut"))
    is_buy = str(row.get("is_buy_weth_to_token", "")).strip() == "1"
    is_sell = str(row.get("is_sell_token_to_weth", "")).strip() == "1"
    if is_buy or (token_in == WETH_BASE and token_out and token_out != WETH_BASE):
        return "WETH_TO_TOKEN"
    if is_sell or (token_out == WETH_BASE and token_in and token_in != WETH_BASE):
        return "TOKEN_TO_WETH"
    try:
        amount0 = int(row.get("amount0") or 0)
        amount1 = int(row.get("amount1") or 0)
        if _lower(row.get("token0")) == WETH_BASE:
            return "WETH_TO_TOKEN" if amount0 > 0 else "TOKEN_TO_WETH"
        if _lower(row.get("token1")) == WETH_BASE:
            return "WETH_TO_TOKEN" if amount1 > 0 else "TOKEN_TO_WETH"
    except Exception:
        return "UNKNOWN"
    return "UNKNOWN"


def source_scan(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen = set()
    dup = 0
    directions = Counter()
    buys = []
    for row in rows:
        direction = infer_direction(row)
        directions[direction] += 1
        key = (row.get("tx_hash"), row.get("log_index"))
        duplicated = key in seen
        if duplicated:
            dup += 1
        else:
            seen.add(key)
        if direction == "WETH_TO_TOKEN" and not duplicated:
            buys.append(row)
    return buys, {
        "total_source_rows": len(rows),
        "direction_known_count": directions["WETH_TO_TOKEN"] + directions["TOKEN_TO_WETH"],
        "weth_to_token_count": directions["WETH_TO_TOKEN"],
        "token_to_weth_count": directions["TOKEN_TO_WETH"],
        "unknown_direction_count": directions["UNKNOWN"],
        "duplicate_tx_log_count": dup,
        "long_entry_raw_candidate_count": len(buys),
    }


def concentration(rows: list[dict[str, Any]]) -> dict[str, float]:
    def _share(values: list[str]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        c = Counter(values)
        return (
            round(c.most_common(1)[0][1] / len(values), 4),
            round(sum(v for _, v in c.most_common(5)) / len(values), 4),
        )

    p1, p5 = _share([_lower(r["pool_address"]) for r in rows])
    t1, t5 = _share([token_address(r) for r in rows])
    return {"top1_pool_share": p1, "top5_pool_share": p5, "top1_token_share": t1, "top5_token_share": t5}


def cap_and_stratify(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {f: [] for f in FEE_ORDER}
    for row in rows:
        buckets.setdefault(fee(row), []).append(row)
    selected = []
    pool_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    progressed = True
    while progressed and len(selected) < RAW_BUY_TARGET:
        progressed = False
        for f in FEE_ORDER:
            bucket = buckets.get(f, [])
            while bucket:
                row = bucket.pop(0)
                pool = _lower(row["pool_address"])
                token = token_address(row)
                if pool_counts[pool] >= cap or token_counts[token] >= cap:
                    continue
                selected.append(row)
                pool_counts[pool] += 1
                token_counts[token] += 1
                progressed = True
                break
            if len(selected) >= RAW_BUY_TARGET:
                break
    return selected


def preselect(buys: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cap3 = cap_and_stratify(buys, 3)
    conc3 = concentration(cap3)
    use_cap = 3
    selected = cap3
    if conc3["top1_pool_share"] > 0.20 or conc3["top5_pool_share"] > 0.55 or conc3["top1_token_share"] > 0.20 or conc3["top5_token_share"] > 0.55:
        selected = cap_and_stratify(buys, 2)
        use_cap = 2
    conc = concentration(selected)
    return selected, {
        "raw_buy_observation_target": RAW_BUY_TARGET,
        "pool_token_cap_used": use_cap,
        "preselected_rows": len(selected),
        "fee_distribution": dict(Counter(str(fee(r)) for r in selected)),
        **conc,
        "fee_specific_dataset": len({fee(r) for r in selected}) == 1,
        "missing_fee_tiers": [f for f in FEE_ORDER if f not in {fee(r) for r in selected}],
    }


def choose_notional(row: dict[str, Any]) -> dict[str, Any]:
    observed_raw = observed_weth_raw(row)
    if observed_raw > 0:
        selected = min(observed_raw, DEFAULT_CAP_RAW)
        reason = "min_observed_weth_amount_and_cap"
        source = "observed_capped"
    else:
        selected = int(0.00001 * WETH_DECIMALS)
        reason = "default_conservative_no_observed_weth"
        source = "default_conservative"
    return {
        "observed_weth_amount_raw": observed_raw,
        "observed_weth_amount_weth": observed_raw / WETH_DECIMALS,
        "selected_notional_raw": selected,
        "selected_notional_weth": selected / WETH_DECIMALS,
        "selected_notional_reason": reason,
        "notional_source": source,
        "notional_cap_weth": DEFAULT_CAP_WETH,
    }


def read_state(rpc: BudgetedRpc, row: dict[str, Any], block: int) -> dict[str, Any]:
    pool = _lower(row["pool_address"])
    token = token_address(row)
    try:
        slot0 = eth_call(rpc, pool, "0x" + SLOT0_SELECTOR, block)
        liquidity = _decode_uint(eth_call(rpc, pool, "0x" + LIQUIDITY_SELECTOR, block))
        weth_bal = _decode_uint(eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        token_bal = _decode_uint(eth_call(rpc, token, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool), block))
        return {"ok": True, "slot0": slot0, "liquidity": liquidity, "weth_balance": weth_bal, "token_balance": token_bal}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


def state_changes(entry: dict[str, Any], exit_state: dict[str, Any]) -> dict[str, bool]:
    if not entry.get("ok") or not exit_state.get("ok"):
        return {"slot0_changed": False, "liquidity_changed": False, "weth_balance_changed": False, "token_balance_changed": False}
    return {
        "slot0_changed": entry["slot0"] != exit_state["slot0"],
        "liquidity_changed": entry["liquidity"] != exit_state["liquidity"],
        "weth_balance_changed": entry["weth_balance"] != exit_state["weth_balance"],
        "token_balance_changed": entry["token_balance"] != exit_state["token_balance"],
    }


def quote_label(rpc: BudgetedRpc, row: dict[str, Any], notional_raw: int) -> dict[str, Any]:
    token = token_address(row)
    entry_block = int(row.get("entry_block") or row.get("block_number") or 0)
    exit_block = int(row.get("exit_block") or entry_block + 900)
    q_entry = quote_exact_input_single_struct(rpc, WETH_BASE, token, notional_raw, fee(row), block_identifier=entry_block)
    if not q_entry["ok"]:
        return {"entry_quote_ok": False, "horizon_exit_quote_ok": False, "failure_reason": "entry_" + (q_entry.get("reason") or "quote_fail")}
    same = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(q_entry["amount_out"]), fee(row), block_identifier=entry_block)
    horizon = quote_exact_input_single_struct(rpc, token, WETH_BASE, int(q_entry["amount_out"]), fee(row), block_identifier=exit_block)
    same_ret = round(int(same["amount_out"]) / notional_raw - 1.0, 8) if same["ok"] else None
    horizon_ret = round(int(horizon["amount_out"]) / notional_raw - 1.0, 8) if horizon["ok"] else None
    reason = "" if horizon["ok"] else "horizon_" + (horizon.get("reason") or "quote_fail")
    return {
        "entry_quote_ok": True,
        "entry_amount_out": int(q_entry["amount_out"]),
        "same_block_sanity_return": same_ret,
        "horizon_exit_quote_ok": bool(horizon["ok"]),
        "horizon_exit_amount_out_weth": int(horizon.get("amount_out") or 0),
        "label_return": horizon_ret,
        "failure_reason": reason,
    }


def run_builder(rpc: BudgetedRpc, selected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    out = []
    state_counts = Counter()
    budget_stop = ""
    for row in selected:
        if len(out) >= OUTPUT_ROW_CAP:
            budget_stop = "output_row_cap_reached"
            break
        entry_block = int(row.get("entry_block") or row.get("block_number") or 0)
        exit_block = int(row.get("exit_block") or entry_block + 900)
        notional = choose_notional(row)
        try:
            entry_state = read_state(rpc, row, entry_block)
            exit_state = read_state(rpc, row, exit_block)
        except Exception as exc:
            budget_stop = str(exc)
            break
        changes = state_changes(entry_state, exit_state)
        for k, v in changes.items():
            if v:
                state_counts[k] += 1
        changed = any(changes.values())
        quote = {
            "entry_quote_ok": False,
            "entry_amount_out": None,
            "same_block_sanity_return": None,
            "horizon_exit_quote_ok": False,
            "horizon_exit_amount_out_weth": None,
            "label_return": None,
            "failure_reason": "no_state_change" if not changed else "",
        }
        if changed:
            try:
                quote = quote_label(rpc, row, int(notional["selected_notional_raw"]))
            except Exception as exc:
                quote["failure_reason"] = str(exc)[:120]
                if "rpc_budget" in str(exc):
                    budget_stop = "rpc_budget_exhausted"
                    break
        label = quote.get("label_return")
        trainable = (
            changed
            and quote.get("entry_quote_ok")
            and quote.get("horizon_exit_quote_ok")
            and label is not None
            and float(label) > NEAR_TOTAL_LOSS
            and notional["notional_source"] != "default_conservative"
        )
        out.append(
            {
                "source_run": SOURCE_RUN,
                "tx_hash": row.get("tx_hash"),
                "log_index": row.get("log_index"),
                "block_number": int(row.get("block_number") or entry_block),
                "timestamp": "",
                "pool_address": _lower(row["pool_address"]),
                "token_address": token_address(row),
                "token0": _lower(row.get("token0")),
                "token1": _lower(row.get("token1")),
                "fee": fee(row),
                "amount0": row.get("amount0"),
                "amount1": row.get("amount1"),
                "token_in": _lower(row.get("tokenIn")),
                "token_out": _lower(row.get("tokenOut")),
                "observed_direction": "WETH_TO_TOKEN",
                **notional,
                "entry_block": entry_block,
                "exit_block": exit_block,
                "exit_block_policy": "fixed_block_pilot",
                "state_changed": changed,
                "state_change_fields": json.dumps(changes, sort_keys=True),
                **quote,
                "same_block_is_label": False,
                "trainable_candidate_flag": bool(trainable),
                "non_trainable_reason": "" if trainable else (quote.get("failure_reason") or "near_total_loss"),
                "quote_selector": STRUCT_SELECTOR,
            }
        )
    return out, {
        "state_checked_count": len(out),
        "state_changed_count": sum(1 for r in out if r["state_changed"]),
        "no_state_change_count": sum(1 for r in out if not r["state_changed"]),
        "state_changed_share": round(sum(1 for r in out if r["state_changed"]) / max(1, len(out)), 4),
        "state_change_fields_distribution": dict(state_counts),
    }, budget_stop


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
    return {"min": quantile(values, 0), "p5": quantile(values, 0.05), "median": quantile(values, 0.5), "p95": quantile(values, 0.95), "max": quantile(values, 1)}


def share(values: list[str]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    c = Counter(values)
    return round(c.most_common(1)[0][1] / len(values), 4), round(sum(v for _, v in c.most_common(5)) / len(values), 4)


def duplicate_conflicts(rows: list[dict[str, Any]]) -> int:
    seen = set()
    dup = 0
    for r in rows:
        key = (r.get("tx_hash"), r.get("log_index"))
        if key in seen:
            dup += 1
        seen.add(key)
    return dup


def metrics(rows: list[dict[str, Any]], state: dict[str, Any], rpc: BudgetedRpc, budget_stop: str) -> dict[str, Any]:
    attempted = [r for r in rows if r["state_changed"]]
    horizon_ok = [r for r in attempted if r["horizon_exit_quote_ok"]]
    clean = [r for r in rows if r["trainable_candidate_flag"]]
    rets = [float(r["label_return"]) for r in clean if r["label_return"] is not None]
    p1, p5 = share([r["pool_address"] for r in clean])
    t1, t5 = share([r["token_address"] for r in clean])
    near = round(sum(1 for x in rets if x <= NEAR_TOTAL_LOSS) / max(1, len(rets)), 4)
    dup = duplicate_conflicts(rows)
    hrate = round(len(horizon_ok) / max(1, len(attempted)), 4)
    return {
        "quote_label_metrics": {
            "attempted_label_rows": len(attempted),
            "entry_quote_success_rate": round(sum(1 for r in attempted if r["entry_quote_ok"]) / max(1, len(attempted)), 4),
            "horizon_quote_success_rate": hrate,
            "failure_reason_distribution": dict(Counter(r["non_trainable_reason"] for r in rows if not r["trainable_candidate_flag"])),
        },
        "clean_diversity_metrics": {
            "clean_rows_count": len(clean),
            "clean_near_total_loss_share": near,
            "clean_return_distribution": distribution(rets),
            "clean_top1_pool_share": p1,
            "clean_top5_pool_share": p5,
            "clean_top1_token_share": t1,
            "clean_top5_token_share": t5,
            "clean_fee_distribution": dict(Counter(str(r["fee"]) for r in clean)),
            "duplicate_conflict_count": dup,
        },
        "rpc_budget": {
            "rpc_calls": rpc.calls,
            "rpc_budget": rpc.budget,
            "rpc_budget_respected": rpc.calls <= rpc.budget,
            "budget_stop_reason": budget_stop or rpc.stop_reason or "",
        },
        "output_row_cap": {
            "output_rows": len(rows),
            "row_cap": OUTPUT_ROW_CAP,
            "output_row_cap_respected": len(rows) <= OUTPUT_ROW_CAP,
        },
        **state,
    }


def sensitivity(rpc: BudgetedRpc, rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [r for r in rows if r["trainable_candidate_flag"]][:10]
    stable = 0
    near_count = 0
    detail = []
    for r in clean:
        vals = []
        for weth in SENSITIVITY_WETH:
            try:
                q = quote_label(
                    rpc,
                    {
                        "token0": r["token0"],
                        "token1": r["token1"],
                        "tokenIn": WETH_BASE,
                        "tokenOut": r["token_address"],
                        "entry_block": r["entry_block"],
                        "exit_block": r["exit_block"],
                    },
                    int(weth * WETH_DECIMALS),
                )
                vals.append(q.get("label_return"))
            except Exception:
                vals.append(None)
        valid = [float(v) for v in vals if v is not None]
        if valid and max(valid) - min(valid) <= 0.25:
            stable += 1
        if any(v is not None and float(v) <= NEAR_TOTAL_LOSS for v in vals):
            near_count += 1
        detail.append({"pool_address": r["pool_address"], "returns": vals})
    return {
        "sensitivity_checked_count": len(clean),
        "sensitivity_stable_count": stable,
        "sensitivity_near_total_loss_count": near_count,
        "selected_cap_seems_too_large": near_count > 0,
        "rows": detail,
    }


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "source_run", "tx_hash", "log_index", "block_number", "timestamp", "pool_address", "token_address",
        "token0", "token1", "fee", "amount0", "amount1", "token_in", "token_out", "observed_direction",
        "observed_weth_amount_raw", "observed_weth_amount_weth", "selected_notional_weth", "selected_notional_reason",
        "notional_cap_weth", "entry_block", "exit_block", "exit_block_policy", "state_changed", "state_change_fields",
        "entry_quote_ok", "entry_amount_out", "horizon_exit_quote_ok", "horizon_exit_amount_out_weth", "label_return",
        "same_block_sanity_return", "same_block_is_label", "failure_reason", "trainable_candidate_flag",
        "non_trainable_reason", "quote_selector",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows[:OUTPUT_ROW_CAP]:
            writer.writerow({k: row.get(k) for k in fields})


def render_markdown(summary: dict[str, Any]) -> str:
    flags = summary["decision_flags"]
    flag_lines = "\n".join(f"- `{k}`: `{str(v).lower()}`" for k, v in flags.items())
    return f"""# Run-004 Patch V3R-24 — New V3 Candidate Builder Bounded Prototype

Status: **research-only / read-only**. No LIVE. No promote. No production signals. No executor/runtime behavior change. No H19. No retrain. No larger expansion.

## 1. Field / Schema Check

- required_fields_available: **{summary['field_availability']['required_fields_available']}**
- missing_fields: `{summary['field_availability']['missing_fields']}`
- derived_fields: `{summary['field_availability']['derived_fields']}`
- unknown_fields: `{summary['field_availability']['unknown_fields']}`

## 2. Source Scan

- total_source_rows: **{summary['source_scan']['total_source_rows']}**
- WETH_TO_TOKEN / TOKEN_TO_WETH / UNKNOWN: **{summary['source_scan']['weth_to_token_count']} / {summary['source_scan']['token_to_weth_count']} / {summary['source_scan']['unknown_direction_count']}**
- duplicate_tx_log_count: **{summary['source_scan']['duplicate_tx_log_count']}**
- long_entry_raw_candidate_count: **{summary['source_scan']['long_entry_raw_candidate_count']}**

## 3. Preselection / Diversity

- preselected_rows: **{summary['preselection']['preselected_rows']}**
- pool/token cap used: **{summary['preselection']['pool_token_cap_used']}**
- fee_distribution: `{summary['preselection']['fee_distribution']}`
- top1/top5 pool: **{summary['preselection']['top1_pool_share']} / {summary['preselection']['top5_pool_share']}**
- top1/top5 token: **{summary['preselection']['top1_token_share']} / {summary['preselection']['top5_token_share']}**
- fee_specific_dataset: **{summary['preselection']['fee_specific_dataset']}**

## 4. State + Quote Metrics

- exit_block_policy: **fixed_block_pilot**
- state_changed_count: **{summary['state_change_prefilter']['state_changed_count']} / {summary['state_change_prefilter']['state_checked_count']}**
- horizon_quote_success_rate: **{summary['quote_label_metrics']['horizon_quote_success_rate']}**
- failure_reason_distribution: `{summary['quote_label_metrics']['failure_reason_distribution']}`
- rpc_calls: **{summary['rpc_budget']['rpc_calls']} / {summary['rpc_budget']['rpc_budget']}**

## 5. Clean Candidate Gate

- clean_rows_count: **{summary['clean_diversity_metrics']['clean_rows_count']}**
- clean_near_total_loss_share: **{summary['clean_diversity_metrics']['clean_near_total_loss_share']}**
- clean_return_distribution: `{summary['clean_diversity_metrics']['clean_return_distribution']}`
- clean top1/top5 pool: **{summary['clean_diversity_metrics']['clean_top1_pool_share']} / {summary['clean_diversity_metrics']['clean_top5_pool_share']}**
- clean top1/top5 token: **{summary['clean_diversity_metrics']['clean_top1_token_share']} / {summary['clean_diversity_metrics']['clean_top5_token_share']}**
- duplicate_conflict_count: **{summary['clean_diversity_metrics']['duplicate_conflict_count']}**
- diversity_gate_passed: **{flags['diversity_gate_passed']}**

## 6. Sensitivity Mini-check

- checked: **{summary['sensitivity']['sensitivity_checked_count']}**
- stable: **{summary['sensitivity']['sensitivity_stable_count']}**
- near-total-loss count: **{summary['sensitivity']['sensitivity_near_total_loss_count']}**
- selected cap seems too large: **{summary['sensitivity']['selected_cap_seems_too_large']}**

## 7. Required Answers

1. V3R-24 是否严格执行 V3R-23 builder contract：**{flags['v3r23_contract_followed']}**。
2. required fields 是否齐全：**{flags['required_fields_available']}**。
3. source scan 分布见 Source Scan。
4. preselection 已实现 pool/token cap 和 fee stratification：**true**。
5. selected notional 使用 adaptive cap：**{flags['adaptive_notional_used']}**。
6. exit_block policy: **fixed_block_pilot**。
7. state_changed prefilter 通过率：**{summary['state_change_prefilter']['state_changed_share']}**。
8. horizon quote success rate：**{summary['quote_label_metrics']['horizon_quote_success_rate']}**。
9. clean rows count 是否 >=20：**{summary['clean_diversity_metrics']['clean_rows_count'] >= 20}**。
10. clean diversity 是否过 gate：**{flags['diversity_gate_passed']}**。
11. near-total-loss 是否 <=0.10：**{summary['clean_diversity_metrics']['clean_near_total_loss_share'] <= 0.10}**。
12. 是否允许 V3R-25 clean prototype validation：**{flags['allow_v3r25_clean_prototype_validation']}**。
13. expansion/model/mainline/live：**False / False / False / False**。

## 8. Decision Flags

{flag_lines}
"""


def main() -> None:
    t0 = time.time()
    field_availability = preflight()
    if not field_availability["required_fields_available"]:
        rows_out: list[dict[str, Any]] = []
        source_metrics = {}
        preselection = {}
        state = {"state_checked_count": 0, "state_changed_count": 0, "no_state_change_count": 0, "state_changed_share": 0.0, "state_change_fields_distribution": {}}
        met = metrics(rows_out, state, BudgetedRpc(_rpc_url(), RPC_BUDGET), "missing_required_fields")
        sens = {"sensitivity_checked_count": 0, "sensitivity_stable_count": 0, "sensitivity_near_total_loss_count": 0, "selected_cap_seems_too_large": False, "rows": []}
    else:
        rpc = BudgetedRpc(_rpc_url(), RPC_BUDGET)
        source_rows = load_candidates()
        buys, source_metrics = source_scan(source_rows)
        selected, preselection = preselect(buys)
        rows_out, state, stop = run_builder(rpc, selected)
        met = metrics(rows_out, state, rpc, stop)
        sens = sensitivity(rpc, rows_out)
    clean = met["clean_diversity_metrics"]
    q = met["quote_label_metrics"]
    flags = {
        "v3r24_completed": True,
        "v3r23_contract_followed": bool(field_availability.get("allow_v3r24_from_design")),
        "required_fields_available": field_availability["required_fields_available"],
        "no_sell_observation_as_long_entry": True,
        "adaptive_notional_used": True,
        "horizon_state_change_prefilter_used": True,
        "same_block_sanity_not_label": True,
        "rpc_budget_respected": met["rpc_budget"]["rpc_budget_respected"],
        "output_row_cap_respected": met["output_row_cap"]["output_row_cap_respected"],
        "clean_rows_count": clean["clean_rows_count"],
        "clean_near_total_loss_share": clean["clean_near_total_loss_share"],
        "clean_top1_pool_share": clean["clean_top1_pool_share"],
        "clean_top5_pool_share": clean["clean_top5_pool_share"],
        "clean_top1_token_share": clean["clean_top1_token_share"],
        "clean_top5_token_share": clean["clean_top5_token_share"],
        "diversity_gate_passed": (
            clean["clean_rows_count"] >= 20
            and clean["clean_near_total_loss_share"] <= 0.10
            and clean["clean_top1_pool_share"] <= 0.20
            and clean["clean_top5_pool_share"] <= 0.55
            and clean["clean_top1_token_share"] <= 0.20
            and clean["clean_top5_token_share"] <= 0.55
            and clean["duplicate_conflict_count"] == 0
            and q["horizon_quote_success_rate"] >= 0.90
        ),
        "prototype_successful": False,
        "allow_v3r25_clean_prototype_validation": False,
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
    }
    flags["prototype_successful"] = bool(
        flags["required_fields_available"]
        and flags["no_sell_observation_as_long_entry"]
        and flags["adaptive_notional_used"]
        and flags["horizon_state_change_prefilter_used"]
        and flags["same_block_sanity_not_label"]
        and flags["rpc_budget_respected"]
        and flags["output_row_cap_respected"]
        and flags["diversity_gate_passed"]
    )
    flags["allow_v3r25_clean_prototype_validation"] = flags["prototype_successful"]
    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "inputs": {
            "design_summary": str(DESIGN_JSON.relative_to(ROOT)),
            "schema": str(SCHEMA_JSON.relative_to(ROOT)),
            "candidate_source": str(NOVEL_CSV.relative_to(ROOT)),
            "helper": "scripts/lib/v3_quoterv2_struct.py",
        },
        "field_availability": field_availability,
        "source_scan": source_metrics,
        "preselection": preselection,
        "state_change_prefilter": {
            "state_checked_count": met["state_checked_count"],
            "state_changed_count": met["state_changed_count"],
            "no_state_change_count": met["no_state_change_count"],
            "state_changed_share": met["state_changed_share"],
            "state_change_fields_distribution": met["state_change_fields_distribution"],
        },
        "quote_label_metrics": q,
        "clean_diversity_metrics": clean,
        "rpc_budget": met["rpc_budget"],
        "output_row_cap": met["output_row_cap"],
        "sensitivity": sens,
        "decision_flags": flags,
        "runtime_seconds": round(time.time() - t0, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows_out)
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[done] rows={len(rows_out)} clean={clean['clean_rows_count']} rpc={summary['rpc_budget']['rpc_calls']}")
    print(f"[done] wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_JSON.relative_to(ROOT)}")
    print(f"[done] wrote {OUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
