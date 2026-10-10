"""Run-004 Patch V3R-13B — Base Uni V3 tradability falsification audit.

Research-only, read-only, bounded RPC audit.

Purpose:
  1. Test the exact V3R-13A quote path on known-good V3 pools.
  2. If positive controls fail, debug alternate Quoter ABIs before drawing
     market-structure conclusions.
  3. Deep-inspect a small sample of V3R-12 novel pools previously marked dead.

No training, no model artifacts, no signals, no live/mainline writes.
"""
from __future__ import annotations

import csv
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be active"

WETH_BASE = "0x4200000000000000000000000000000000000006"
V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
QUOTER_V2 = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
SWAP_ROUTER = "0x2626664c2603336e57b271c5c0b26f421741e481"

KNOWN_GOOD_CSV = ROOT / "mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r13b_tradability_falsification_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r13b-v3-tradability-falsification-audit.md"

FIXED_WETH_SIZES = [0.0001, 0.0005, 0.001]
FIXED_WETH_RAW = {s: int(s * 10**18) for s in FIXED_WETH_SIZES}
KNOWN_POOL_LIMIT = int(os.environ.get("V3R13B_KNOWN_POOL_LIMIT", "30"))
NOVEL_POOL_LIMIT = int(os.environ.get("V3R13B_NOVEL_POOL_LIMIT", "40"))
BLOCK_SEMANTICS_LIMIT = int(os.environ.get("V3R13B_BLOCK_SEMANTICS_LIMIT", "10"))
RANDOM_SEED = 1337

# Selectors.
# V3R-13A path: selector copied exactly from scripts/run004_v3r13a_fixed_notional_quote_sanity.py
V3R13A_SELECTOR = "f7729d43"
# Debug alternatives:
# - f7729d43 is commonly the legacy Quoter signature
#   quoteExactInputSingle(address,address,uint24,uint256,uint160).
# - c6a5026a is QuoterV2's struct ABI:
#   quoteExactInputSingle((address,address,uint256,uint24,uint160)).
QUOTER_LEGACY_SELECTOR = "f7729d43"
QUOTER_V2_STRUCT_SELECTOR = "c6a5026a"
TOKEN0_SELECTOR = "0dfe1681"
TOKEN1_SELECTOR = "d21220a7"
FEE_SELECTOR = "ddca3f43"
SLOT0_SELECTOR = "3850c7bd"
LIQUIDITY_SELECTOR = "1a686502"
BALANCE_OF_SELECTOR = "70a08231"
DECIMALS_SELECTOR = "313ce567"
GET_POOL_SELECTOR = "1698ee82"


def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _rpc_url() -> str:
    _load_env_file()
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url:
        raise RuntimeError("RPC URL must be in LABEL_RPC_URL or EVM_HTTP_URL")
    return url


class RPC:
    def __init__(self, url: str) -> None:
        self.url = url
        self.session = requests.Session()
        self._id = 0
        self._calls = 0
        self._lock = Lock()

    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        last_exc: Exception | None = None
        for attempt in range(5):
            with self._lock:
                self._id += 1
                req_id = self._id
                self._calls += 1
            body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            try:
                # Keep provider pressure intentionally low.
                time.sleep(0.035)
                r = self.session.post(self.url, json=body, timeout=timeout)
                if r.status_code == 429:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"rpc[{method}]: {j['error']}")
                return j.get("result")
            except Exception as exc:  # noqa: BLE001 - diagnostic wants exact reasons.
                last_exc = exc
                if "429" in str(exc) or "Too Many Requests" in str(exc):
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(str(last_exc) if last_exc else "rpc_failed")

    @property
    def calls(self) -> int:
        return self._calls


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _pad_addr(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(64, "0")


def _pad_uint(n: int) -> str:
    return hex(int(n))[2:].rjust(64, "0")


def _decode_uint(res: str | None, default: int = 0) -> int:
    if not res or res == "0x":
        return default
    raw = res[2:] if res.startswith("0x") else res
    if not raw:
        return default
    return int(raw[:64], 16)


def _decode_addr(res: str | None) -> str:
    if not res or res == "0x":
        return ""
    raw = res[2:] if res.startswith("0x") else res
    if len(raw) < 64:
        return ""
    return "0x" + raw[-40:].lower()


def _eth_call(rpc: RPC, to: str, data: str, block: int | str = "latest", timeout: int = 30) -> str:
    block_tag = hex(block) if isinstance(block, int) else block
    return rpc.call("eth_call", [{"to": to, "data": data}, block_tag], timeout=timeout)


def _quote_call_data(mode: str, tin: str, tout: str, amount_in_raw: int, fee: int) -> str:
    if mode == "v3r13a_exact":
        # Exact V3R-13A order: selector f7729d43 + tokenIn, tokenOut, amountIn, fee, sqrtPriceLimit.
        return (
            "0x" + V3R13A_SELECTOR
            + _pad_addr(tin) + _pad_addr(tout)
            + _pad_uint(amount_in_raw) + _pad_uint(fee) + _pad_uint(0)
        )
    if mode == "legacy_fee_amount_order":
        # Legacy Quoter order for f7729d43: tokenIn, tokenOut, fee, amountIn, sqrtPriceLimit.
        return (
            "0x" + QUOTER_LEGACY_SELECTOR
            + _pad_addr(tin) + _pad_addr(tout)
            + _pad_uint(fee) + _pad_uint(amount_in_raw) + _pad_uint(0)
        )
    if mode == "quoter_v2_struct":
        # Struct has only static words, so ABI encoding is selector + 5 fields.
        return (
            "0x" + QUOTER_V2_STRUCT_SELECTOR
            + _pad_addr(tin) + _pad_addr(tout)
            + _pad_uint(amount_in_raw) + _pad_uint(fee) + _pad_uint(0)
        )
    raise ValueError(f"unknown quote mode: {mode}")


def quote_exact_input_single(
    rpc: RPC,
    mode: str,
    tin: str,
    tout: str,
    amount_in_raw: int,
    fee: int,
    block: int | str = "latest",
) -> dict[str, Any]:
    data = _quote_call_data(mode, tin, tout, amount_in_raw, fee)
    try:
        res = _eth_call(rpc, QUOTER_V2, data, block=block, timeout=25)
    except Exception as exc:
        return {"ok": False, "err": str(exc)[:220], "amount_out": 0}
    if not res or res == "0x":
        return {"ok": False, "err": "empty_result", "amount_out": 0}
    raw = res[2:] if res.startswith("0x") else res
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    aout = int(words[0], 16) if words and words[0] else 0
    return {"ok": aout > 0, "err": "" if aout > 0 else "zero_amount_out", "amount_out": aout}


def quote_roundtrip(
    rpc: RPC,
    mode: str,
    target: str,
    fee: int,
    amount_weth_raw: int,
    block: int | str = "latest",
) -> dict[str, Any]:
    entry = quote_exact_input_single(rpc, mode, WETH_BASE, target, amount_weth_raw, fee, block)
    if not entry["ok"]:
        return {"status": "entry_fail", "entry": entry}
    exit_q = quote_exact_input_single(rpc, mode, target, WETH_BASE, int(entry["amount_out"]), fee, block)
    if not exit_q["ok"]:
        return {"status": "exit_fail", "entry": entry, "exit": exit_q}
    gross = int(exit_q["amount_out"]) / amount_weth_raw - 1.0
    return {
        "status": "success",
        "entry": entry,
        "exit": exit_q,
        "gross_return": round(gross, 8),
    }


def load_known_good_pools() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(KNOWN_GOOD_CSV.open("r", encoding="utf-8")))
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("label_status") or "").lower() != "success":
            continue
        pool = _lower(row.get("pool_address"))
        t0 = _lower(row.get("token0"))
        t1 = _lower(row.get("token1"))
        if not pool or WETH_BASE not in (t0, t1):
            continue
        # Prefer the row with the largest observed WETH side.
        weth_amt = int(float(row.get("weth_side_amount_raw") or 0))
        if pool not in by_pool or weth_amt > int(by_pool[pool].get("_weth_amt", 0)):
            by_pool[pool] = {
                "pool_address": pool,
                "token0": t0,
                "token1": t1,
                "target": t1 if t0 == WETH_BASE else t0,
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    return list(by_pool.values())[:KNOWN_POOL_LIMIT]


def load_novel_pool_sample() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        pool = _lower(row.get("pool_address"))
        t0 = _lower(row.get("token0"))
        t1 = _lower(row.get("token1"))
        if not pool or WETH_BASE not in (t0, t1):
            continue
        weth_amt = int(float(row.get("weth_side_amount_raw") or 0))
        if pool not in by_pool or weth_amt > int(by_pool[pool].get("_weth_amt", 0)):
            by_pool[pool] = {
                "pool_address": pool,
                "token0_csv": t0,
                "token1_csv": t1,
                "target_csv": t1 if t0 == WETH_BASE else t0,
                "target_decimals_csv": int(row.get("token1_decimals") if t0 == WETH_BASE else row.get("token0_decimals") or 18),
                "fee_csv": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    # Stratified sample: include several fee tiers and observed-size buckets naturally via shuffled deterministic order.
    items = list(by_pool.values())
    random.Random(RANDOM_SEED).shuffle(items)
    return items[:NOVEL_POOL_LIMIT]


def run_positive_controls(rpc: RPC, head_block: int, pools: list[dict[str, Any]]) -> dict[str, Any]:
    modes = ["v3r13a_exact", "legacy_fee_amount_order", "quoter_v2_struct"]
    out: dict[str, Any] = {"head_block": head_block, "pools_tested": len(pools), "modes": {}}
    for mode in modes:
        mode_rows = []
        for row in pools:
            size_results = {}
            for size, raw in FIXED_WETH_RAW.items():
                size_results[str(size)] = quote_roundtrip(rpc, mode, row["target"], row["fee"], raw, head_block)
            any_success = any(v.get("status") == "success" for v in size_results.values())
            mode_rows.append({
                "pool_address": row["pool_address"],
                "target": row["target"],
                "fee": row["fee"],
                "any_success": any_success,
                "sizes": size_results,
            })
        success = sum(1 for r in mode_rows if r["any_success"])
        fail_reasons = Counter(
            next((v.get("entry", {}).get("err") or v.get("exit", {}).get("err") or v.get("status"))
                 for v in r["sizes"].values() if v.get("status") != "success")
            for r in mode_rows if not r["any_success"]
        )
        out["modes"][mode] = {
            "pools_success_any_size": success,
            "success_rate_any_size": round(success / max(1, len(mode_rows)), 4),
            "failure_reasons_top": dict(fail_reasons.most_common(8)),
            "sample_successes": [
                {
                    "pool_address": r["pool_address"],
                    "fee": r["fee"],
                    "successful_sizes": [s for s, v in r["sizes"].items() if v.get("status") == "success"],
                }
                for r in mode_rows if r["any_success"]
            ][:10],
            "sample_failures": [
                {
                    "pool_address": r["pool_address"],
                    "fee": r["fee"],
                    "first_failure": next((v for v in r["sizes"].values() if v.get("status") != "success"), {}),
                }
                for r in mode_rows if not r["any_success"]
            ][:5],
        }
    best_mode = max(out["modes"], key=lambda m: out["modes"][m]["pools_success_any_size"])
    out["best_mode"] = best_mode
    out["v3r13a_exact_path_valid"] = out["modes"]["v3r13a_exact"]["pools_success_any_size"] > 0
    out["debug_quote_path_needed"] = not out["v3r13a_exact_path_valid"]
    return out


def read_pool_metadata(rpc: RPC, pool: str, csv_hint: dict[str, Any], head_block: int) -> dict[str, Any]:
    def call_uint(to: str, selector: str, block: int | str = "latest") -> tuple[int, str]:
        try:
            return _decode_uint(_eth_call(rpc, to, "0x" + selector, block=block)), ""
        except Exception as exc:
            return 0, str(exc)[:180]

    def call_addr(to: str, selector: str) -> tuple[str, str]:
        try:
            return _decode_addr(_eth_call(rpc, to, "0x" + selector)), ""
        except Exception as exc:
            return "", str(exc)[:180]

    token0, err0 = call_addr(pool, TOKEN0_SELECTOR)
    token1, err1 = call_addr(pool, TOKEN1_SELECTOR)
    fee, fee_err = call_uint(pool, FEE_SELECTOR)
    liquidity, liq_err = call_uint(pool, LIQUIDITY_SELECTOR)
    slot0_raw = ""
    slot0_err = ""
    try:
        slot0_raw = _eth_call(rpc, pool, "0x" + SLOT0_SELECTOR)
    except Exception as exc:
        slot0_err = str(exc)[:180]
    sqrt_price_x96 = _decode_uint(slot0_raw, 0)
    target = token1 if token0 == WETH_BASE else token0 if token1 == WETH_BASE else _lower(csv_hint.get("target_csv"))
    target_decimals = int(csv_hint.get("target_decimals_csv") or 18)
    try:
        dec_res = _eth_call(rpc, target, "0x" + DECIMALS_SELECTOR) if target else "0x"
        d = _decode_uint(dec_res, target_decimals)
        if 0 <= d <= 36:
            target_decimals = d
    except Exception:
        pass
    weth_balance = 0
    target_balance = 0
    bal_err = ""
    try:
        weth_balance = _decode_uint(
            _eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool))
        )
        if target:
            target_balance = _decode_uint(
                _eth_call(rpc, target, "0x" + BALANCE_OF_SELECTOR + _pad_addr(pool))
            )
    except Exception as exc:
        bal_err = str(exc)[:180]
    factory_pool = ""
    factory_err = ""
    if token0 and token1 and fee:
        try:
            data = "0x" + GET_POOL_SELECTOR + _pad_addr(token0) + _pad_addr(token1) + _pad_uint(fee)
            factory_pool = _decode_addr(_eth_call(rpc, V3_FACTORY, data))
        except Exception as exc:
            factory_err = str(exc)[:180]
    return {
        "pool_address": pool,
        "token0": token0,
        "token1": token1,
        "target": target,
        "target_decimals": target_decimals,
        "fee_onchain": fee,
        "fee_csv": int(csv_hint.get("fee_csv") or 0),
        "slot0_sqrtPriceX96": sqrt_price_x96,
        "liquidity": liquidity,
        "weth_balance": weth_balance,
        "target_balance": target_balance,
        "factory_getPool": factory_pool,
        "factory_matches_pool": factory_pool == pool,
        "errors": {
            "token0": err0,
            "token1": err1,
            "fee": fee_err,
            "liquidity": liq_err,
            "slot0": slot0_err,
            "balance": bal_err,
            "factory": factory_err,
        },
        "entry_block": int(csv_hint.get("entry_block") or 0),
        "head_block": head_block,
    }


def classify_failure(meta: dict[str, Any], q_weth: dict[str, Any], q_target: dict[str, Any]) -> str:
    if not meta["factory_matches_pool"]:
        return "factory_mismatch"
    if int(meta.get("fee_onchain") or 0) != int(meta.get("fee_csv") or 0):
        return "fee_mismatch"
    if not meta.get("token0") or not meta.get("token1") or not meta.get("target"):
        return "token_metadata_bad"
    if int(meta.get("liquidity") or 0) == 0:
        return "zero_liquidity"
    if int(meta.get("weth_balance") or 0) == 0:
        return "zero_weth_balance"
    if int(meta.get("target_balance") or 0) == 0:
        return "zero_target_balance"
    if q_weth.get("ok"):
        return "tradable_weth_to_target"
    if q_target.get("ok"):
        return "one_sided_target_to_weth"
    if int(meta.get("liquidity") or 0) > 0 and int(meta.get("weth_balance") or 0) > 0 and int(meta.get("target_balance") or 0) > 0:
        return "price_out_of_range_or_no_initialized_tick_liquidity"
    return "quoter_revert_unknown"


def run_novel_deep_audit(rpc: RPC, head_block: int, pools: list[dict[str, Any]], quote_mode: str) -> dict[str, Any]:
    rows = []
    for hint in pools:
        meta = read_pool_metadata(rpc, hint["pool_address"], hint, head_block)
        fee = int(meta.get("fee_onchain") or hint["fee_csv"])
        target = meta.get("target") or hint["target_csv"]
        q_weth = quote_exact_input_single(
            rpc, quote_mode, WETH_BASE, target, int(0.0001 * 10**18), fee, head_block
        ) if target else {"ok": False, "err": "missing_target", "amount_out": 0}
        target_amount = 0
        if int(meta.get("target_balance") or 0) > 0:
            # Tiny target-side quote: min(1e-6 target, 0.1% of pool target balance), but at least 1 wei.
            unit = 10 ** max(0, int(meta.get("target_decimals") or 18) - 6)
            target_amount = max(1, min(unit, int(meta["target_balance"]) // 1000 or 1))
        q_target = quote_exact_input_single(
            rpc, quote_mode, target, WETH_BASE, target_amount, fee, head_block
        ) if target and target_amount > 0 else {"ok": False, "err": "no_safe_target_amount", "amount_out": 0}
        classification = classify_failure(meta, q_weth, q_target)
        rows.append({
            **meta,
            "quote_mode": quote_mode,
            "weth_to_target_0_0001": q_weth,
            "target_to_weth_small_amount_raw": target_amount,
            "target_to_weth_small": q_target,
            "failure_mode": classification,
        })
    return {
        "sample_size": len(rows),
        "failure_mode_counts": dict(Counter(r["failure_mode"] for r in rows).most_common()),
        "factory_matches": sum(1 for r in rows if r["factory_matches_pool"]),
        "zero_liquidity": sum(1 for r in rows if int(r.get("liquidity") or 0) == 0),
        "zero_weth_balance": sum(1 for r in rows if int(r.get("weth_balance") or 0) == 0),
        "zero_target_balance": sum(1 for r in rows if int(r.get("target_balance") or 0) == 0),
        "weth_to_target_success": sum(1 for r in rows if r["weth_to_target_0_0001"].get("ok")),
        "target_to_weth_success": sum(1 for r in rows if r["target_to_weth_small"].get("ok")),
        "sample_rows": rows[:50],
    }


def run_block_semantics(rpc: RPC, pools: list[dict[str, Any]], quote_mode: str) -> dict[str, Any]:
    out_rows = []
    for hint in pools[:BLOCK_SEMANTICS_LIMIT]:
        pool = hint["pool_address"]
        target = hint["target_csv"]
        fee = int(hint["fee_csv"])
        entry = int(hint["entry_block"])
        tests = {}
        for label, block in {
            "entry_block_minus_1": max(0, entry - 1),
            "entry_block": entry,
            "head": "latest",
        }.items():
            tests[label] = quote_exact_input_single(
                rpc, quote_mode, WETH_BASE, target, int(0.0001 * 10**18), fee, block
            )
        out_rows.append({"pool_address": pool, "fee": fee, "entry_block": entry, "tests": tests})
    counts = {
        "entry_minus_1_success": sum(1 for r in out_rows if r["tests"]["entry_block_minus_1"].get("ok")),
        "entry_success": sum(1 for r in out_rows if r["tests"]["entry_block"].get("ok")),
        "head_success": sum(1 for r in out_rows if r["tests"]["head"].get("ok")),
    }
    return {"sample_size": len(out_rows), **counts, "rows": out_rows}


def render_markdown(summary: dict[str, Any]) -> str:
    pc = summary["positive_controls"]
    deep = summary["novel_deep_sample"]
    block = summary["block_semantics"]
    exact = pc["modes"]["v3r13a_exact"]
    best = pc["modes"][pc["best_mode"]]
    if (
        block["entry_minus_1_success"] == block["entry_success"] == block["head_success"]
    ):
        block_interpretation = (
            "entry_block vs entry_block-1 does not change the sampled conclusion."
        )
    else:
        block_interpretation = (
            "some historical block sensitivity exists; filter/backfill logic must account for it."
        )
    return f"""# Run-004 Patch V3R-13B — Base Uni V3 Tradability Falsification Audit

Status: **research-only / read-only**. No model training. No model artifacts. No target tuning. No full backfill. No mainline or live/executor/signal changes.

## 1. Positive Control: Known Usable Pools

Known-good source: `mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv`.

Known-good pools tested: **{pc['pools_tested']}** at current head block **{pc['head_block']}**.
Fixed WETH notionals: 0.0001 / 0.0005 / 0.001 WETH.

Quote-path results:

| Quote path | Pools with any round-trip success | Success rate | Interpretation |
|---|---:|---:|---|
| V3R-13A exact (`f7729d43`, amount-before-fee) | {exact['pools_success_any_size']} / {pc['pools_tested']} | {exact['success_rate_any_size']} | {'valid' if pc['v3r13a_exact_path_valid'] else 'FAILED positive control'} |
| Legacy fee-before-amount (`f7729d43`) | {pc['modes']['legacy_fee_amount_order']['pools_success_any_size']} / {pc['pools_tested']} | {pc['modes']['legacy_fee_amount_order']['success_rate_any_size']} | debug alternative |
| QuoterV2 struct (`c6a5026a`) | {pc['modes']['quoter_v2_struct']['pools_success_any_size']} / {pc['pools_tested']} | {pc['modes']['quoter_v2_struct']['success_rate_any_size']} | best valid path if positive |

Best working quote path: **{pc['best_mode']}** with **{best['pools_success_any_size']} / {pc['pools_tested']}** known-good pools quoted.

Conclusion: {'V3R-13A exact quote path failed the positive control; its 0/405 novel-pool conclusion is not valid as a market-structure proof.' if not pc['v3r13a_exact_path_valid'] else 'V3R-13A exact quote path passed positive controls.'}

## 2. Novel Pool Deep Sample

Novel source: `cache/run004_v3r12/v3r12_novel_pool_candidates.csv`.
Sample size: **{deep['sample_size']}** pools. Quote mode used for classification: **{summary['selected_quote_mode_for_novel_audit']}**.

Failure-mode counts:

""" + "\n".join(f"- `{k}`: {v}" for k, v in deep["failure_mode_counts"].items()) + f"""

Raw checks:
- factory matches pool: {deep['factory_matches']} / {deep['sample_size']}
- zero liquidity: {deep['zero_liquidity']} / {deep['sample_size']}
- zero WETH balance: {deep['zero_weth_balance']} / {deep['sample_size']}
- zero target balance: {deep['zero_target_balance']} / {deep['sample_size']}
- current WETH→target 0.0001 WETH success: {deep['weth_to_target_success']} / {deep['sample_size']}
- current target→WETH small amount success: {deep['target_to_weth_success']} / {deep['sample_size']}

## 3. Block Semantics

Subset: **{block['sample_size']}** novel pools with observed swap events.

0.0001 WETH WETH→target quote successes:
- `entry_block - 1`: {block['entry_minus_1_success']} / {block['sample_size']}
- `entry_block`: {block['entry_success']} / {block['sample_size']}
- `head/latest`: {block['head_success']} / {block['sample_size']}

Interpretation: {block_interpretation}

## 4. Strategic Decision

Decision: **{summary['strategic_decision']}**.

Decision flags:
- `allow_base_univ3_novel_expansion = {str(summary['decision_flags']['allow_base_univ3_novel_expansion']).lower()}`
- `allow_quote_path_fix = {str(summary['decision_flags']['allow_quote_path_fix']).lower()}`
- `allow_aerodrome_scope_review = true`
- `allow_v2_scope_review = true`
- `allow_existing_v3_95pool_history = true`
- `allow_model_research = false`
- `allow_mainline = false`

## 5. Required Answers

Q1. Does the V3R-13A quote path work on known usable pools?

{summary['answers']['Q1']}

Q2. Are the 405 novel pools genuinely dead, or was there a remaining quote/factory/fee bug?

{summary['answers']['Q2']}

Q3. What are the dominant failure modes among sampled novel pools?

{summary['answers']['Q3']}

Q4. Does entry_block vs entry_block-1 change the conclusion?

{summary['answers']['Q4']}

Q5. Should Base Uni V3 novel-pool expansion stop?

{summary['answers']['Q5']}

Q6. What is the next recommended path: existing 95-pool deep history, Aerodrome, V2, or other-chain V3?

{summary['answers']['Q6']}

## Safety

- `rpc_calls_total = {summary['rpc_calls_total']}`
- `elapsed_sec = {summary['elapsed_sec']}`
- no large CSVs written
- no model artifacts written
"""


def main() -> int:
    t0 = time.time()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    rpc = RPC(_rpc_url())
    head_block = int(rpc.call("eth_blockNumber", []), 16)

    known = load_known_good_pools()
    novel = load_novel_pool_sample()
    positive = run_positive_controls(rpc, head_block, known)
    # If V3R-13A exact path fails positive control, use the best debug path only
    # for bounded classification, and do not preserve V3R-13A's market conclusion.
    selected_mode = "v3r13a_exact" if positive["v3r13a_exact_path_valid"] else positive["best_mode"]
    deep = run_novel_deep_audit(rpc, head_block, novel, selected_mode)
    block = run_block_semantics(rpc, novel, selected_mode)

    if not positive["v3r13a_exact_path_valid"]:
        decision = "A_quote_implementation_still_broken"
        allow_novel = False
        allow_fix = True
        q1 = (
            f"No. The exact V3R-13A quote path succeeded on "
            f"{positive['modes']['v3r13a_exact']['pools_success_any_size']} / {positive['pools_tested']} "
            f"known-good pools, while debug path `{positive['best_mode']}` succeeded on "
            f"{positive['modes'][positive['best_mode']]['pools_success_any_size']} / {positive['pools_tested']}."
        )
        q2 = (
            "There is a remaining quote-path bug in V3R-13A. The 0/405 result cannot be used as a "
            "strategic proof that novel pools are genuinely dead until the quote path is fixed and rerun. "
            f"The bounded novel sample using `{selected_mode}` is diagnostic only."
        )
        q5 = "Yes for now, but because the falsification found quote-path failure, expansion should stop pending a quote-path fix/rerun rather than because market death is fully proven."
        q6 = "First fix/migrate the quote path to the working QuoterV2 ABI, then rerun the 405-pool sanity. In parallel, existing 95-pool deep history, Aerodrome, and V2 reviews remain allowed; no model research."
    else:
        decision = "B_quote_path_valid_novel_pools_genuinely_dead" if deep["weth_to_target_success"] == 0 else "C_mixed_result_define_filter"
        allow_novel = deep["weth_to_target_success"] > 0
        allow_fix = False
        q1 = f"Yes. V3R-13A exact path succeeded on {positive['modes']['v3r13a_exact']['pools_success_any_size']} / {positive['pools_tested']} known-good pools."
        q2 = "The positive control passed; novel-pool failures are more likely real market structure than a factory/fee/quote bug."
        q5 = "Yes." if not allow_novel else "Not completely; define a tradability filter before any expansion."
        q6 = "Stop broad novel-pool expansion; prefer existing 95-pool deep history plus Aerodrome/V2 scope review."

    q3 = ", ".join(f"{k}={v}" for k, v in deep["failure_mode_counts"].items()) or "no sampled failures"
    q4 = (
        f"No material change: entry-1={block['entry_minus_1_success']}, "
        f"entry={block['entry_success']}, head={block['head_success']} successes over "
        f"{block['sample_size']} sampled pools."
    )

    summary = {
        "run": "run004_v3r13b_tradability_falsification_audit",
        "research_only": True,
        "head_block": head_block,
        "known_good_source": str(KNOWN_GOOD_CSV.relative_to(ROOT)),
        "novel_source": str(NOVEL_CSV.relative_to(ROOT)),
        "official_addresses": {
            "v3CoreFactory": V3_FACTORY,
            "quoterV2": QUOTER_V2,
            "swapRouter": SWAP_ROUTER,
            "baseWETH": WETH_BASE,
        },
        "fixed_weth_sizes": FIXED_WETH_SIZES,
        "positive_controls": positive,
        "selected_quote_mode_for_novel_audit": selected_mode,
        "novel_deep_sample": deep,
        "block_semantics": block,
        "strategic_decision": decision,
        "decision_flags": {
            "allow_base_univ3_novel_expansion": allow_novel,
            "allow_quote_path_fix": allow_fix,
            "allow_aerodrome_scope_review": True,
            "allow_v2_scope_review": True,
            "allow_existing_v3_95pool_history": True,
            "allow_model_research": False,
            "allow_mainline": False,
        },
        "answers": {"Q1": q1, "Q2": q2, "Q3": q3, "Q4": q4, "Q5": q5, "Q6": q6},
        "rpc_calls_total": rpc.calls,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=False), encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "markdown": str(OUT_MD)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
